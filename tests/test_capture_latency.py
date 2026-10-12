import io
import shutil
import subprocess
import threading

import pytest
from PIL import Image

from campix.capture import CaptureError, DirectShowBackend
from campix.models import CameraConfig
from campix.processes import NO_WINDOW

requires_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")


def test_jpeg_marker_search_is_linear_for_large_fragmented_frames():
    class CountedBuffer(bytearray):
        scanned_bytes = 0

        def find(self, marker, start=0, end=None):
            bound = len(self) if end is None else end
            self.scanned_bytes += max(0, bound - start)
            return super().find(marker, start, bound)

    payload = b"\xff\xd8" + b"x" * (8 * 1024 * 1024) + b"\xff\xd9"
    buffer = CountedBuffer()
    backend = unopened_backend()
    assert backend._read_jpeg(ControlledProcess(io.BytesIO(payload)), buffer) == payload
    # Searching every accumulated chunk again grows quadratically and throttles 4K.
    assert buffer.scanned_bytes < len(payload) * 2
    assert not buffer


def test_jpeg_parser_retains_markers_split_across_pipe_chunks():
    class FragmentedPipe:
        chunks = iter([b"prefix\xff", b"\xd8pixels\xff", b"\xd9\xff\xd8next\xff\xd9"])

        def read1(self, size):
            return next(self.chunks, b"")

        read = read1

    backend = unopened_backend()
    buffer = bytearray()
    process = ControlledProcess(FragmentedPipe())
    assert backend._read_jpeg(process, buffer) == b"\xff\xd8pixels\xff\xd9"
    assert backend._read_jpeg(process, buffer) == b"\xff\xd8next\xff\xd9"
    assert not buffer


def filtered_frames(*filters, fps=10, input_rate=1, duration=2):
    """Exercise the live adapter's filter with generated frames, without opening a device."""
    backend = DirectShowBackend(
        CameraConfig(
            id="latency",
            name="Generated latency regression",
            backend="dshow",
            source="Test-only camera",
            width=160,
            height=120,
            fps=fps,
        )
    )
    output_arguments = backend.output_arguments()
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=black:s=160x120:r={input_rate}:d={duration}",
        "-vf",
        ",".join([*filters, backend.video_filter()]),
        *output_arguments,
        # Bound output even when a regression recreates the two-minute timestamp gap.
        "-frames:v",
        "1201",
        "-f",
        "image2pipe",
        "-c:v",
        "mjpeg",
        "-q:v",
        "3",
        "pipe:1",
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        check=True,
        timeout=10,
        creationflags=NO_WINDOW,
    )
    packets = result.stdout.split(b"\xff\xd8")[1:]
    frames = []
    for packet in packets:
        with Image.open(io.BytesIO(b"\xff\xd8" + packet)) as frame:
            frames.append(frame.convert("RGB"))
    return frames


@requires_ffmpeg
def test_directshow_timestamp_gap_does_not_generate_a_backlog_of_repeated_jpegs():
    frames = filtered_frames(r"setpts=if(eq(N\,0)\,0\,120/TB)")
    # Two physical frames with a two-minute clock gap must remain two output frames.
    assert len(frames) == 2


@pytest.mark.parametrize(("fps", "expected"), [(1, 1), (10, 10), (30, 30), (60, 30)])
@requires_ffmpeg
def test_directshow_sampler_retains_the_native_or_requested_cadence_without_duplicates(fps, expected):
    frames = filtered_frames(fps=fps, input_rate=30, duration=1)
    assert len(frames) == expected


@requires_ffmpeg
def test_directshow_sampler_resumes_after_the_source_clock_moves_backwards():
    frames = filtered_frames(
        r"geq=lum=N*30:cb=128:cr=128",
        r"setpts=if(lt(N\,3)\,N/TB\,(N-3)/TB)",
        duration=6,
    )
    assert len(frames) == 6
    # The new clock epoch must retain each new image rather than waiting to catch up.
    levels = [frame.getpixel((80, 60))[0] for frame in frames]
    assert all(later - earlier > 10 for earlier, later in zip(levels, levels[1:], strict=False))


@pytest.mark.parametrize("fps,input_rate,expected", [(10, 30, 30), (30, 30, 90), (60, 60, 180)])
@requires_ffmpeg
def test_directshow_sampling_cadence_does_not_drift_with_small_clock_jitter(fps, input_rate, expected):
    frames = filtered_frames(
        "settb=1/1000000",
        f"setpts=N/({input_rate}*TB)+sin(N*2.39996)*0.002/TB",
        fps=fps,
        input_rate=input_rate,
        duration=3,
    )
    assert len(frames) == expected


@pytest.mark.parametrize("fps,expected", [(30, 30), (60, 60)])
@requires_ffmpeg
def test_directshow_sampler_supports_a_reported_60_fps_input_without_a_30_fps_cap(fps, expected):
    frames = filtered_frames(fps=fps, input_rate=60, duration=1)
    assert len(frames) == expected


class SilentPipe:
    def __init__(self):
        self.reading = threading.Event()
        self.stopped = threading.Event()

    def read1(self, size):
        self.reading.set()
        self.stopped.wait(5)
        return b""

    read = read1

    def close(self):
        self.stopped.set()


class ControlledProcess:
    def __init__(self, stdout=None):
        self.stdout = stdout if stdout is not None else SilentPipe()
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 1
        self.stdout.close()

    kill = terminate

    def wait(self, timeout=None):
        return self.returncode


def unopened_backend():
    return DirectShowBackend(
        CameraConfig(
            id="cancel",
            name="Generated cancellation regression",
            backend="dshow",
            source="Test-only camera",
            width=160,
            height=120,
        )
    )


def open_worker(backend):
    errors = []

    def open_camera():
        try:
            backend.open()
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=open_camera)
    worker.start()
    return worker, errors


def test_directshow_close_during_native_listing_prevents_camera_spawn(monkeypatch):
    backend = unopened_backend()
    entered, release = threading.Event(), threading.Event()
    opened = []

    def list_modes():
        entered.set()
        assert release.wait(5)
        return [(160, 120)]

    def spawn(*args, **kwargs):
        process = ControlledProcess()
        opened.append(process)
        return process

    monkeypatch.setattr(backend, "native_modes", list_modes)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    worker, errors = open_worker(backend)
    try:
        assert entered.wait(2)
        backend.close()
        release.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert not opened, "Closing a camera must prevent a later capture process"
        assert len(errors) == 1 and isinstance(errors[0], CaptureError)
    finally:
        release.set()
        backend.close()
        worker.join(timeout=2)


def test_directshow_close_interrupts_unknown_mode_wait_without_fallback(monkeypatch):
    backend = unopened_backend()
    opened = []
    process_created = threading.Event()

    def spawn(*args, **kwargs):
        process = ControlledProcess()
        opened.append(process)
        process_created.set()
        return process

    monkeypatch.setattr(backend, "native_modes", lambda: None)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    worker, errors = open_worker(backend)
    try:
        # The first process must exist and be waiting for camera data before cancellation.
        assert process_created.wait(2)
        assert opened[0].stdout.reading.wait(2)
        backend.close()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(opened) == 1, "Cancellation must not retry with the default device mode"
        assert opened[0].poll() is not None
        assert len(errors) == 1 and isinstance(errors[0], CaptureError)
    finally:
        backend.close()
        worker.join(timeout=2)


def test_directshow_close_before_fallback_spawn_prevents_a_second_camera_process(monkeypatch):
    backend = unopened_backend()
    entering_fallback, release = threading.Event(), threading.Event()
    opened = []
    original_spawn = backend._spawn
    spawn_calls = 0

    def spawn_process(*args, **kwargs):
        # Immediate EOF makes the first requested capture mode fail without a watchdog wait.
        process = ControlledProcess(io.BytesIO())
        opened.append(process)
        return process

    def pause_fallback():
        nonlocal spawn_calls
        spawn_calls += 1
        if spawn_calls == 2:
            entering_fallback.set()
            assert release.wait(5)
        original_spawn()

    monkeypatch.setattr(backend, "native_modes", lambda: None)
    monkeypatch.setattr(backend, "_spawn", pause_fallback)
    monkeypatch.setattr(subprocess, "Popen", spawn_process)
    worker, errors = open_worker(backend)
    try:
        assert entering_fallback.wait(2)
        backend.close()
        release.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(opened) == 1, "A queued fallback must respect cancellation"
        assert opened[0].poll() is not None
        assert len(errors) == 1 and isinstance(errors[0], CaptureError)
    finally:
        release.set()
        backend.close()
        worker.join(timeout=2)
