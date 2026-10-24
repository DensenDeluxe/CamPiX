import io
import threading
import time
from collections import deque
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from campix import capture
from campix.budget import admission_reason, shed_level_for
from campix.encoding import encoder_args, jpeg_dimensions
from campix.models import CameraConfig, MotionRegion
from campix.runtime import downscale_jpeg, motion_score, trim_jpeg_history


def jpeg(width, height):
    image = Image.new("RGB", (width, height), (20, 40, 60))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=80)
    image.close()
    return buffer.getvalue()


def test_jpeg_dimensions_read_the_header_without_accepting_a_truncated_marker():
    payload = jpeg(32, 16)
    assert jpeg_dimensions(payload) == (32, 16)
    assert jpeg_dimensions(b"\xff\xd8\xff") is None
    assert jpeg_dimensions(b"not-a-jpeg") is None


def test_trim_jpeg_history_keeps_one_frame_after_the_encoded_hub_is_ready():
    frames = deque((index, b"x" * 10) for index in range(4))
    total = trim_jpeg_history(frames, now=10, seconds=3, hub_ready=True, max_bytes=100)
    assert list(frames) == [(3, b"x" * 10)]
    assert total == 10


def test_trim_jpeg_history_drops_old_frames_while_the_hub_is_not_ready():
    frames = deque([(0, b"abcd"), (1, b"efgh"), (9, b"ijkl")])
    total = trim_jpeg_history(frames, now=10, seconds=3, hub_ready=False, max_bytes=100)
    assert [stamp for stamp, _ in frames] == [9]
    assert total == 4


def test_motion_score_uses_only_the_normalized_region():
    previous = np.zeros((10, 10), dtype=np.uint8)
    gray = np.zeros((10, 10), dtype=np.uint8)
    gray[:, 5:] = 255
    full = motion_score(previous, gray, 25, [])
    region = motion_score(previous, gray, 25, [MotionRegion(x=0, y=0, w=0.4, h=1)])
    assert full == pytest.approx(0.5)
    assert region == 0


def test_motion_region_must_stay_inside_the_frame():
    with pytest.raises(ValueError, match="Motion region must stay inside the frame"):
        CameraConfig(
            id="cam",
            name="Cam",
            backend="synthetic",
            source="0",
            motion_regions=[MotionRegion(x=0.8, y=0, w=0.3, h=0.2)],
        )


def test_libx264_uses_a_short_gop_two_threads_and_a_bitrate_ceiling():
    _, outputs = encoder_args("libx264", 1920, 1080, 30)
    options = dict(zip(outputs[::2], outputs[1::2], strict=True))
    assert options["-g"] == "15"
    assert options["-threads"] == "2"
    assert options["-bf"] == "0"
    assert options["-preset"] == "veryfast"
    assert int(options["-maxrate"]) == int(1920 * 1080 * 30 * 0.07)
    assert int(options["-bufsize"]) == int(options["-maxrate"]) // 2


def test_shed_levels_follow_temperature_and_load():
    assert shed_level_for(70, 1) == 0
    assert shed_level_for(75, None) == 1
    assert shed_level_for(None, 3) == 1
    assert shed_level_for(82, 0) == 2
    assert shed_level_for(None, 4) == 2


def test_admission_reason_protects_small_hosts_and_pi_5_software_encoding():
    assert (
        admission_reason(
            width=1920, height=1080, fps=30, memory_total=512 * 1024 * 1024, camera_count=1
        )
        == "This host memory budget allows one camera up to 1280x720 at 15 FPS"
    )
    assert (
        admission_reason(
            width=1920,
            height=1080,
            fps=30,
            memory_total=8 * 1024**3,
            camera_count=2,
            model="Raspberry Pi 5 Model B",
        )
        == "Pi 5 software H.264 budget starts at two 1280x720 streams; reduce this profile"
    )
    assert (
        admission_reason(
            width=1280,
            height=720,
            fps=30,
            memory_total=8 * 1024**3,
            camera_count=1,
            model="Raspberry Pi 5 Model B",
        )
        is None
    )


def test_downscale_jpeg_stays_within_the_live_preview_bounds():
    payload = downscale_jpeg(jpeg(1920, 1080))
    assert jpeg_dimensions(payload)[0] <= 960
    assert jpeg_dimensions(payload)[1] <= 540


def test_network_h264_copy_command_keeps_the_url_as_one_argument():
    source = "rtsp://192.0.2.10/live"
    backend = capture.NetworkBackend(
        CameraConfig(id="ip", name="IP", backend="network", source=source), "ffmpeg"
    )
    command = backend.h264_copy_command()
    assert command[command.index("-i") + 1] == source
    assert command[command.index("-c:v") + 1] == "copy"
    assert "mpegts" in command
    assert command[-1] == "pipe:1"
    assert all(";" not in part and "|" not in part for part in command)


def test_abandoned_h264_copy_does_not_publish():
    published = []
    backend = capture.NetworkBackend(
        CameraConfig(id="ip", name="IP", backend="network", source="rtsp://192.0.2.10/live"),
        "ffmpeg",
    )
    backend.set_encoded_sink(lambda *args, **kwargs: published.append(args))
    session = capture.H264CopySession(backend, ["ffmpeg"])
    session.stopped.set()
    unit = SimpleNamespace(data=b"\x00\x00\x00\x01\x65", timestamp_us=0, keyframe=True)
    session._publish(unit)
    assert published == []
    assert session.matched.is_set() is False


class _StallProcess:
    def __init__(self):
        self.killed = threading.Event()
        self.returncode = None
        self.stdout = self

    def read1(self, _size):
        self.killed.wait(5)
        return b""

    read = read1

    def close(self):
        return None

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9
        self.killed.set()

    def terminate(self):
        if self.returncode is None:
            self.returncode = 1
        self.killed.set()

    def wait(self, timeout=None):
        self.killed.wait(timeout)
        return self.returncode


def test_stalled_network_jpeg_is_stopped_so_capture_can_reconnect(monkeypatch):
    process = _StallProcess()
    monkeypatch.setattr(capture.subprocess, "Popen", lambda command, **kwargs: process)
    backend = capture.NetworkBackend(
        CameraConfig(id="ip", name="IP", backend="network", source="rtsp://192.0.2.10/live"),
        "ffmpeg",
    )
    backend.stall_seconds = 0.25
    backend.open()
    try:
        with pytest.raises(capture.CaptureError):
            backend.read_jpeg()
        assert process.killed.is_set()
    finally:
        backend.close()


def test_network_h264_watchdog_stops_a_silent_copy(monkeypatch):
    process = _StallProcess()
    backend = capture.NetworkBackend(
        CameraConfig(id="ip", name="IP", backend="network", source="rtsp://192.0.2.10/live"),
        "ffmpeg",
    )
    session = capture.H264CopySession(backend, ["ffmpeg"])
    session.proc = process
    session.stall_seconds = 0.25
    session.start_watch()
    try:
        assert process.killed.wait(1.5)
        with pytest.raises(capture.CaptureError, match="stalled"):
            session.read_jpeg()
    finally:
        session.close()


def test_network_jpeg_watchdog_ignores_bytes_that_never_form_a_frame(monkeypatch):
    class Trickle(_StallProcess):
        def read1(self, _size):
            if self.killed.is_set():
                return b""
            time.sleep(0.02)
            return b"\x00" * 1024

    process = Trickle()
    monkeypatch.setattr(capture.subprocess, "Popen", lambda command, **kwargs: process)
    backend = capture.NetworkBackend(
        CameraConfig(id="ip", name="IP", backend="network", source="rtsp://192.0.2.10/live"),
        "ffmpeg",
    )
    backend.stall_seconds = 0.25
    backend.open()
    try:
        with pytest.raises(capture.CaptureError):
            backend.read_jpeg()
        assert process.killed.is_set()
    finally:
        backend.close()


def test_prompt_network_jpeg_is_not_treated_as_stalled(monkeypatch):
    payload = jpeg(320, 180)
    released = threading.Event()

    class Prompt:
        def __init__(self):
            self.killed = False
            self.returncode = None
            self.sent = False
            self.stdout = self

        def read1(self, _size):
            if not self.sent:
                self.sent = True
                return payload
            released.wait(2)
            return b""

        read = read1

        def close(self):
            return None

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9
            released.set()

        def terminate(self):
            self.returncode = 1
            released.set()

        def wait(self, timeout=None):
            return self.returncode if self.returncode is not None else 0

    process = Prompt()
    monkeypatch.setattr(capture.subprocess, "Popen", lambda command, **kwargs: process)
    backend = capture.NetworkBackend(
        CameraConfig(id="ip", name="IP", backend="network", source="rtsp://192.0.2.10/live"),
        "ffmpeg",
    )
    backend.stall_seconds = 1
    backend.open()
    try:
        assert backend.read_jpeg() == payload
        assert process.killed is False
    finally:
        backend.close()


def test_v4l2_mjpeg_with_shared_sink_does_not_open_a_network_h264_probe(monkeypatch):
    probes = []
    launched = []

    def spy(self, timeout):
        probes.append(self.command)
        return False

    def popen(command, **kwargs):
        launched.append(command)
        return SimpleNamespace(
            stdout=io.BytesIO(),
            stdin=None,
            poll=lambda: None,
            terminate=lambda: None,
            kill=lambda: None,
            wait=lambda timeout=None: 0,
        )

    monkeypatch.setattr(capture.H264CopySession, "start_matching", spy)
    monkeypatch.setattr(capture.subprocess, "Popen", popen)
    monkeypatch.setattr(capture.V4L2Backend, "_load_controls", lambda self: None)

    def modes(self):
        self.native_profiles = [
            {
                "size": (640, 480),
                "min_fps": 30,
                "max_fps": 30,
                "format_type": "vcodec",
                "format": "mjpeg",
            }
        ]
        return [(640, 480)]

    monkeypatch.setattr(capture.V4L2CompressedBackend, "native_modes", modes)
    backend = capture.make_backend(
        CameraConfig(
            id="usb",
            name="USB",
            backend="v4l2",
            source="/dev/video0",
            width=640,
            height=480,
            fps=10,
        ),
        "ffmpeg",
    )
    backend.set_encoded_sink(lambda *args, **kwargs: None)
    backend.open()
    try:
        assert probes == []
        assert len(launched) == 1
        command = launched[0]
        assert command[command.index("-f") + 1] == "v4l2"
        assert "mpegts" not in command
    finally:
        backend.close()


def test_network_copy_falls_back_to_jpeg_when_the_probe_does_not_match(monkeypatch):
    launched = []

    class Fake:
        def __init__(self):
            self.stdout = io.BytesIO()
            self.stdin = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            return None

        def kill(self):
            return None

    def popen(command, **kwargs):
        launched.append(command)
        return Fake()

    monkeypatch.setattr(capture.H264CopySession, "start_matching", lambda self, timeout: False)
    monkeypatch.setattr(capture.subprocess, "Popen", popen)
    backend = capture.NetworkBackend(
        CameraConfig(id="ip", name="IP", backend="network", source="rtsp://192.0.2.10/live"),
        "ffmpeg",
    )
    backend.set_encoded_sink(lambda *args, **kwargs: None)
    backend.open()
    try:
        assert backend.native_encoded is False
        assert launched[0][launched[0].index("-c:v") + 1] == "mjpeg"
    finally:
        backend.close()


def test_stream_slots_stop_at_four_readers():
    from campix.app import StreamSlots

    slots = StreamSlots()
    assert all(slots.acquire("cam") for _ in range(4))
    assert slots.acquire("cam") is False
    slots.release("cam")
    assert slots.acquire("cam") is True


def test_metrics_export_cpu_temperature_and_camera_stages(client):
    from .conftest import signin

    signin(client)
    body = client.get("/metrics").text
    assert "campix_cpu_percent " in body
    assert "campix_process_memory_bytes " in body
    assert 'campix_camera_shed_level{camera="demo"}' in body
    assert 'campix_camera_preroll_bytes{camera="demo"}' in body


def test_v4l2_uses_a_compressed_owner_when_the_profile_size_is_not_exact(monkeypatch):
    def modes(self):
        self.native_profiles = [
            {
                "size": (1280, 720),
                "min_fps": 30,
                "max_fps": 30,
                "format_type": "vcodec",
                "format": "mjpeg",
            }
        ]
        return [(1280, 720)]

    monkeypatch.setattr(capture.V4L2CompressedBackend, "native_modes", modes)
    monkeypatch.setattr(capture.V4L2CompressedBackend, "open", lambda self: None)
    monkeypatch.setattr(capture.V4L2Backend, "_load_controls", lambda self: None)
    monkeypatch.setattr(
        capture.V4L2Backend,
        "_open_opencv",
        lambda self: pytest.fail("MJPEG device opened a second pixel capture"),
    )
    backend = capture.make_backend(
        CameraConfig(
            id="usb",
            name="USB",
            backend="v4l2",
            source="/dev/video9",
            width=640,
            height=480,
            fps=30,
        ),
        "ffmpeg",
    )
    backend.open()
    try:
        assert backend.pipe is not None
    finally:
        backend.close()
