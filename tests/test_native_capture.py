"""Native capture ownership, resource cleanup and truthful zero-copy capabilities."""

import contextlib
import io
import shutil
import subprocess
import sys
import threading
import time
from types import ModuleType

import pytest
from PIL import Image

from campix import capture
from campix.models import CameraConfig
from tests import test_tsmux

real_access_units = test_tsmux.real_access_units


def config(backend="picamera2", **values):
    return CameraConfig(
        id="native",
        name="Native test camera",
        backend=backend,
        source="0" if backend == "picamera2" else "Test webcam",
        **values,
    )


class FakeOutput:
    def __init__(self):
        self.recording = False

    def start(self):
        self.recording = True

    def stop(self):
        self.recording = False


class FakeV4L2Encoder:
    pass


class FakeHardwareEncoder(FakeV4L2Encoder):
    def __init__(self, **options):
        self.options = options


class FakeSoftwareEncoder:
    def __init__(self, **options):
        self.options = options


class Request:
    def __init__(self, camera):
        self.camera = camera
        self.released = False

    def save(self, name, buffer, format):
        self.camera.saved.append(name)
        Image.new("RGB", self.camera.configuration[name]["size"]).save(buffer, "JPEG")

    def release(self):
        self.released = True


class FakePicamera:
    def __init__(self, index):
        self.index = index
        self.camera_controls = {}
        self.camera_properties = {"Model": "Isolated CSI fixture"}
        self.options = {}
        self.saved = []
        self.stopped = self.closed = False
        self.encoder_stops = 0
        self.capture_calls = 0

    def create_video_configuration(self, **options):
        return options

    def configure(self, configuration):
        self.configuration = configuration

    def start_encoder(self, encoder, output, name):
        self.encoder, self.output, self.encode_name = encoder, output, name
        output.start()

    def stop_encoder(self, encoder):
        assert encoder is self.encoder
        self.encoder_stops += 1
        self.output.stop()

    def start(self):
        pass

    def capture_request(self, wait):
        assert wait == 5.0
        self.request = Request(self)
        return self.request

    def capture_image(self, name):
        self.capture_calls += 1
        return Image.new("RGB", self.configuration[name]["size"])

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def picamera_modules(monkeypatch, hardware=True):
    modules = {
        "picamera2": {"Picamera2": FakePicamera},
        "picamera2.encoders": {
            "H264Encoder": FakeHardwareEncoder if hardware else FakeSoftwareEncoder,
        },
        "picamera2.encoders.v4l2_encoder": {"V4L2Encoder": FakeV4L2Encoder},
        "picamera2.outputs": {"Output": FakeOutput},
    }
    for name, contents in modules.items():
        module = ModuleType(name)
        module.__dict__.update(contents)
        monkeypatch.setitem(sys.modules, name, module)


@pytest.mark.parametrize("hardware", [True, False])
def test_native_csi_owns_one_camera_and_distinguishes_software_alias(monkeypatch, hardware):
    picamera_modules(monkeypatch, hardware)
    backend = capture.make_backend(config(width=3840, height=2160, fps=60), "ffmpeg")
    packets = []
    backend.set_encoded_sink(lambda data, **metadata: packets.append((data, metadata)))
    backend.open()
    try:
        assert backend.cam.configuration["main"] == {"size": (3840, 2160), "format": "YUV420"}
        assert backend.cam.configuration["lores"] == {"size": (640, 360), "format": "YUV420"}
        assert backend.cam.configuration["buffer_count"] == 4
        assert backend.encoder.options == {
            "repeat": True,
            "iperiod": 60,
            "framerate": 60,
            "profile": "baseline",
        }
        backend.output.outputframe(b"encoded H264 fixture", keyframe=True, timestamp=123456)
        assert packets == [
            (
                b"\x00\x00\x00\x01\x09\xf0encoded H264 fixture",
                {"timestamp_us": 123456, "keyframe": True, "frame_complete": True},
            )
        ]
        jpeg = backend.read_jpeg()
        with Image.open(io.BytesIO(jpeg)) as image:
            assert image.size == (640, 360)
        assert backend.cam.request.released
        assert backend.cam.saved == ["lores"]
        assert backend.cam.capture_calls == 0
        with Image.open(io.BytesIO(backend.snapshot_jpeg())) as photo:
            assert photo.size == (3840, 2160)
        assert backend.cam.saved == ["lores", "main"]
        assert backend.cam.request.released
        assert backend.capabilities["native_encoder_hardware"] is hardware
        assert backend.capabilities["capture_to_encoder_zero_copy"] is hardware
        assert backend.capabilities["zero_copy_scope"] == (
            "camera-dmabuf-to-v4l2-encoder" if hardware else None
        )
    finally:
        backend.close()
    backend.output.outputframe(b"late frame", timestamp=999999)
    assert len(packets) == 1
    assert backend.cam.stopped and backend.cam.closed
    assert backend.cam.encoder_stops == 1
    backend.close()
    assert backend.cam.encoder_stops == 1


@pytest.mark.parametrize("encoder,native_disabled", [("libx264", False), ("auto", True)])
def test_native_csi_respects_explicit_encoder_and_disabled_fallback(
    monkeypatch, encoder, native_disabled
):
    picamera_modules(monkeypatch)
    backend = capture.make_backend(config(encoder=encoder), "ffmpeg")
    backend.set_encoded_sink(lambda *args, **kwargs: pytest.fail("Native capture bypassed policy"))
    backend.native_disabled = native_disabled
    backend.open()
    try:
        assert not backend.native_encoded
        assert not backend.native_attempted
        assert backend.read_jpeg() is None
        assert backend.read().size == (1280, 720)
        assert backend.cam.configuration["main"]["format"] == "RGB888"
        assert "lores" not in backend.cam.configuration
    finally:
        backend.close()


def test_native_csi_callback_failure_is_visible_and_does_not_unwind_driver_thread(monkeypatch):
    picamera_modules(monkeypatch)
    backend = capture.make_backend(config(), "ffmpeg")

    def failing_sink(*args, **kwargs):
        raise RuntimeError("Do not leak camera output or native driver details")

    backend.set_encoded_sink(failing_sink)
    backend.open()
    try:
        backend.output.outputframe(b"packet")
        with pytest.raises(capture.CaptureError, match="Native camera encoder output failed"):
            backend.read_jpeg()
    finally:
        backend.close()
    assert backend.cam.closed and backend.cam.encoder_stops == 1


def test_native_csi_releases_request_when_preview_encoding_fails(monkeypatch):
    picamera_modules(monkeypatch)
    backend = capture.make_backend(config(), "ffmpeg")
    backend.set_encoded_sink(lambda *args, **kwargs: None)
    backend.open()

    def fail(*args, **kwargs):
        raise ValueError("Preview encoding failed")

    monkeypatch.setattr(Request, "save", fail)
    try:
        with pytest.raises(ValueError, match="Preview encoding failed"):
            backend.read_jpeg()
        assert backend.cam.request.released
    finally:
        backend.close()


def test_native_csi_complete_frame_boundary_only_marks_last_bounded_chunk(monkeypatch):
    picamera_modules(monkeypatch)
    backend = capture.make_backend(config(), "ffmpeg")
    packets = []
    backend.set_encoded_sink(lambda data, **metadata: packets.append((data, metadata)))
    backend.open()
    try:
        frame = b"test-only-encoded" * 10000
        backend.output.outputframe(frame, timestamp=100)
        assert len(packets) > 1
        assert all(len(data) <= 65536 for data, _ in packets)
        assert all(not metadata["frame_complete"] for _, metadata in packets[:-1])
        assert packets[-1][1]["frame_complete"]
        assert b"".join(data for data, _ in packets) == b"\x00\x00\x00\x01\x09\xf0" + frame
    finally:
        backend.close()


def test_native_csi_start_failure_closes_encoder_and_camera(monkeypatch):
    picamera_modules(monkeypatch)

    def failed_start(self):
        raise OSError("Sensor disconnected")

    monkeypatch.setattr(FakePicamera, "start", failed_start)
    backend = capture.make_backend(config(), "ffmpeg")
    backend.set_encoded_sink(lambda *args, **kwargs: None)
    with pytest.raises(OSError, match="Sensor disconnected"):
        backend.open()
    assert backend.native_attempted
    assert backend.closed.is_set()
    assert backend.cam.closed and backend.cam.encoder_stops == 1


@pytest.mark.parametrize(
    "source,expected",
    [
        ((3840, 2160), (640, 360)),
        ((640, 480), (640, 480)),
        ((160, 120), (160, 120)),
        ((4096, 2160), (640, 336)),
    ],
)
def test_native_preview_dimensions_are_bounded_even_and_do_not_upscale(source, expected):
    assert capture.preview_size(*source) == expected


def h264_listing(backend):
    size = (backend.config.width, backend.config.height)
    backend.native_profiles = [
        {
            "size": size,
            "min_fps": 30,
            "max_fps": 30,
            "format_type": "vcodec",
            "format": "h264",
        }
    ]
    return [size]


@pytest.mark.parametrize(
    "encoder,disabled,rate",
    [("auto", False, 30), ("libx264", False, 30), ("auto", True, 30), ("auto", False, 15)],
)
def test_uvc_h264_passthrough_requires_auto_and_exact_native_size_rate(encoder, disabled, rate):
    backend = capture.make_backend(
        config("dshow", width=640, height=360, fps=rate, encoder=encoder), "ffmpeg"
    )
    backend.set_encoded_sink(lambda *args, **kwargs: None)
    backend.native_disabled = disabled
    backend.modes = h264_listing(backend)
    backend.source_size = (640, 360)
    backend._negotiate_source()
    assert backend.h264_copy is (encoder == "auto" and not disabled and rate == 30)
    assert backend.input_arguments()[backend.input_arguments().index("-vcodec") + 1] == "h264"
    assert not backend.jpeg_copy


@pytest.mark.parametrize(
    "codec,source_rate,requested,copy",
    [
        ("h264", 29.970, 30, True),
        ("h264", 59.940, 60, True),
        ("h264", 23.976, 24, True),
        ("mjpeg", 59.940, 60, True),
        ("h264", 29.9, 30, False),
        ("h264", 59, 60, False),
        ("h264", 30, 60, False),
        ("mjpeg", 60, 30, False),
    ],
)
def test_uvc_nominal_fractional_rates_keep_actual_device_rate(codec, source_rate, requested, copy):
    backend = capture.make_backend(config("dshow", width=640, height=360, fps=requested), "ffmpeg")
    backend.set_encoded_sink(lambda *args, **kwargs: None)
    backend.source_size = (640, 360)
    backend.native_profiles = [
        {
            "size": (640, 360),
            "min_fps": source_rate,
            "max_fps": source_rate,
            "format_type": "vcodec",
            "format": codec,
        }
    ]
    backend._negotiate_source()
    assert (backend.h264_copy if codec == "h264" else backend.jpeg_copy) is copy
    arguments = backend.input_arguments()
    assert float(arguments[arguments.index("-framerate") + 1]) == source_rate
    assert backend.source_fps == source_rate


def test_v4l2_fractional_nominal_mode_uses_compressed_owner_before_opencv(monkeypatch):
    def modes(self):
        self.native_profiles = [
            {
                "size": (640, 360),
                "min_fps": 59.940,
                "max_fps": 59.940,
                "format_type": "vcodec",
                "format": "h264",
            }
        ]
        return [(640, 360)]

    def opened(self):
        self.source_size = (640, 360)
        self._negotiate_source()
        self.native_encoded = self.native_attempted = True
        self.capabilities.update(source_fps=self.source_fps)

    monkeypatch.setattr(capture.V4L2CompressedBackend, "native_modes", modes)
    monkeypatch.setattr(capture.V4L2CompressedBackend, "open", opened)
    monkeypatch.setattr(capture.V4L2Backend, "_load_controls", lambda self: None)
    monkeypatch.setattr(
        capture.V4L2Backend,
        "_open_opencv",
        lambda self: pytest.fail("Nominal mode fell back to pixel capture"),
    )
    backend = capture.make_backend(
        CameraConfig(
            id="usb",
            name="USB fixture",
            backend="v4l2",
            source="/dev/video42",
            width=640,
            height=360,
            fps=60,
        ),
        "ffmpeg",
    )
    backend.set_encoded_sink(lambda *args, **kwargs: None)
    backend.open()
    try:
        assert backend.native_encoded and backend.pipe.h264_copy
        assert backend.capabilities["source_fps"] == 59.940
    finally:
        backend.close()


def test_v4l2_parses_discrete_compressed_formats_without_opening_capture(monkeypatch):
    listing = b"""
    [0]: 'MJPG' (Motion-JPEG, compressed)
        Size: Discrete 1280x720
            Interval: Discrete 0.033s (30.000 fps)
    [1]: 'H264' (H.264, compressed)
        Size: Discrete 3840x2160
            Interval: Discrete 0.017s (60.000 fps)
    [2]: 'YUYV' (YUYV 4:2:2)
        Size: Discrete 640x480
            Interval: Discrete 0.033s (30.000 fps)
    """
    commands = []
    monkeypatch.setattr(capture.shutil, "which", lambda binary: binary)

    def bounded(command, **limits):
        commands.append((command, limits))
        return listing

    monkeypatch.setattr(capture, "_bounded_stdout", bounded)
    backend = capture.V4L2CompressedBackend(
        CameraConfig(
            id="usb",
            name="USB fixture",
            backend="v4l2",
            source="/dev/video42",
            width=3840,
            height=2160,
            fps=60,
        ),
        "ffmpeg",
    )
    backend.set_encoded_sink(lambda *args, **kwargs: None)
    assert backend.native_modes() == [(1280, 720), (3840, 2160)]
    backend.source_size = (3840, 2160)
    backend._negotiate_source()
    assert backend.h264_copy
    assert backend.input_arguments() == [
        "-f",
        "v4l2",
        "-input_format",
        "h264",
        "-video_size",
        "3840x2160",
        "-framerate",
        "60",
        "-i",
        "/dev/video42",
    ]
    assert commands == [
        (
            ["v4l2-ctl", "-d", "/dev/video42", "--list-formats-ext"],
            {"limit": 65536, "timeout": 5},
        )
    ]


def test_native_uvc_compressed_copy_and_independent_preview_with_real_ffmpeg(monkeypatch, tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg is unavailable for the generated H.264 source integration")
    source = tmp_path / "generated-source.h264"
    generated = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=1280x720:rate=30",
            "-frames:v",
            "90",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-g",
            "30",
            "-bf",
            "0",
            "-f",
            "h264",
            str(source),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
    )
    assert generated.returncode == 0
    backend = capture.make_backend(config("dshow", width=1280, height=720, fps=30), ffmpeg)
    chunks = []
    backend.set_encoded_sink(lambda data, **metadata: chunks.append(data))
    monkeypatch.setattr(backend, "native_modes", lambda: h264_listing(backend))
    monkeypatch.setattr(
        backend,
        "input_arguments",
        lambda: [
            "-re",
            "-stream_loop",
            "-1",
            "-f",
            "h264",
            "-r",
            "30",
            "-i",
            str(source),
        ],
    )
    backend.open()
    try:
        frame = backend.read_jpeg()
        with Image.open(io.BytesIO(frame)) as image:
            assert image.size == (640, 360)
        assert backend.native_encoded
        assert backend.capabilities["encoded_source"] == "uvc-h264"
        assert not backend.capabilities["capture_to_encoder_zero_copy"]
        assert not backend.capabilities["native_encoder_hardware"]
        assert chunks
        with Image.open(io.BytesIO(backend.snapshot_jpeg())) as photo:
            assert photo.size == (1280, 720)
        first = time.monotonic()
        assert backend.read_jpeg()
        assert time.monotonic() - first < 2
    finally:
        backend.close()
    assert not backend.reader_thread.is_alive()
    assert not backend.watch_thread.is_alive()
    assert not backend.native_preview.writer.is_alive()
    assert not backend.native_preview.reader.is_alive()
    assert backend.proc.poll() is not None
    assert backend.native_preview.process.poll() is not None


def test_slow_native_preview_cannot_block_capture_or_grow_its_queue(monkeypatch):
    from campix.encoded import EncodedAccessUnit
    from campix.tsmux import TransportStreamMuxer
    from tests.test_directshow import LiveFFmpeg
    from tests.test_encoded import packet

    main, preview = LiveFFmpeg(), LiveFFmpeg()
    blocked, released = threading.Event(), threading.Event()

    class BlockedWriter:
        def write(self, data):
            blocked.set()
            released.wait(5)
            raise BrokenPipeError()

        def flush(self):
            pass

    preview.stdin = BlockedWriter()
    contexts = []

    @contextlib.contextmanager
    def owned(command, **options):
        process = preview if not contexts else main
        contexts.append((command, options))
        try:
            yield process
        finally:
            released.set()
            process.terminate()

    real_terminate = preview.terminate

    def terminate():
        released.set()
        real_terminate()

    preview.terminate = terminate
    monkeypatch.setattr(capture, "owned_process", owned)
    backend = capture.make_backend(config("dshow", width=640, height=360, fps=30), "ffmpeg")
    monkeypatch.setattr(backend, "native_modes", lambda: h264_listing(backend))
    seen = []
    backend.set_encoded_sink(lambda data, **metadata: seen.append(data))
    backend.open()
    muxer = TransportStreamMuxer()

    def transport(index):
        unit = packet(index % 30 == 0)
        return muxer.mux(
            EncodedAccessUnit(
                unit.data, 100 + index / 30, index + 1, 1, unit.keyframe, unit.decoder_config
            )
        )

    try:
        main.stdout.feed([b"".join(transport(index) for index in range(3))])
        assert blocked.wait(2)
        stream = b"".join(transport(index) for index in range(3, 603))
        main.stdout.feed(
            [stream[offset : offset + 65536] for offset in range(0, len(stream), 65536)]
        )
        assert main.stdout.drained.wait(2), "Preview backpressure blocked the physical capture pipe"
        assert len(seen) >= 600
        subscription = backend.native_preview.subscription
        assert subscription.dropped_frames > 0
        assert len(subscription._queue) <= 120
        assert subscription._bytes <= 4 * 1024 * 1024
        assert len(contexts) == 2
        preview_command = contexts[0][0]
        assert preview_command[preview_command.index("-threads") + 1] == "1"
        assert preview_command[preview_command.index("-flags") + 1] == "low_delay"
        camera_command = contexts[1][0]
        assert camera_command.count("-i") == 1
        assert camera_command[camera_command.index("-c:v") + 1] == "copy"
        assert "-vf" not in camera_command
        assert (
            camera_command[camera_command.index("-f", camera_command.index("-i")) + 1] == "mpegts"
        )
    finally:
        backend.close()
    assert not backend.reader_thread.is_alive()
    assert not backend.native_preview.writer.is_alive()
    assert not backend.native_preview.reader.is_alive()


def test_v4l2_native_mjpeg_retains_compressed_bytes_and_one_capture_owner(monkeypatch):
    from tests.test_directshow import LiveFFmpeg, jpeg

    process = LiveFFmpeg()
    opened = []

    def popen(command, **options):
        opened.append(command)
        return process

    def modes(self):
        self.native_profiles = [
            {
                "size": (640, 360),
                "min_fps": 30,
                "max_fps": 30,
                "format_type": "vcodec",
                "format": "mjpeg",
            }
        ]
        return [(640, 360)]

    monkeypatch.setattr(capture.subprocess, "Popen", popen)
    monkeypatch.setattr(capture.V4L2CompressedBackend, "native_modes", modes)
    monkeypatch.setattr(
        capture.V4L2Backend, "_open_opencv", lambda self: pytest.fail("Two capture owners")
    )
    monkeypatch.setattr(capture.V4L2Backend, "_load_controls", lambda self: None)
    backend = capture.make_backend(
        CameraConfig(
            id="usb",
            name="USB fixture",
            backend="v4l2",
            source="/dev/video42",
            width=640,
            height=360,
            fps=30,
        ),
        "ffmpeg",
    )
    backend.open()
    try:
        payload = jpeg((640, 360))
        process.stdout.feed([payload])
        assert backend.read_jpeg() == payload
        assert backend.capabilities["jpeg_passthrough"]
        assert not backend.native_encoded
        assert len(opened) == 1
        assert opened[0][opened[0].index("-input_format") + 1] == "mjpeg"
        assert opened[0][opened[0].index("-c:v") + 1] == "copy"
    finally:
        backend.close()
    assert process.poll() is not None
    assert not backend.pipe.reader_thread.is_alive()


def test_device_listing_stdout_is_bounded_and_timed_out_process_is_reaped():
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        capture._bounded_stdout(
            [sys.executable, "-c", "import time; time.sleep(30)"], limit=64, timeout=0.2
        )
    assert time.monotonic() - started < 5
    with pytest.raises(ValueError, match="listing exceeds"):
        capture._bounded_stdout(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 100000)"],
            limit=64,
            timeout=5,
        )


def test_closed_native_backends_cannot_create_a_new_capture_owner(monkeypatch):
    picamera_modules(monkeypatch)
    camera = capture.make_backend(config(), "ffmpeg")
    camera.close()
    with pytest.raises(capture.CaptureError, match="closed"):
        camera.open()
    assert not hasattr(camera, "cam")
    usb = capture.make_backend(
        CameraConfig(
            id="usb",
            name="USB fixture",
            backend="v4l2",
            source="/dev/video42",
        ),
        "ffmpeg",
    )
    usb.close()
    monkeypatch.setattr(
        capture.V4L2CompressedBackend,
        "native_modes",
        lambda self: pytest.fail("Reopened after close"),
    )
    with pytest.raises(capture.CaptureError, match="closed"):
        usb.open()


def test_native_uvc_batch_read_preserves_real_five_fps_source_pts(tmp_path, real_access_units):
    from types import SimpleNamespace

    from campix.tsdemux import NativeTransportStreamDemuxer

    source, transport = tmp_path / "generated-five-fps.h264", tmp_path / "generated-five-fps.ts"
    source.write_bytes(b"".join(unit.data for unit in real_access_units))
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-r",
            "5",
            "-f",
            "h264",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
            "-bsf:v",
            "h264_metadata=aud=insert",
            "-mpegts_start_pid",
            "256",
            "-mpegts_pmt_start_pid",
            "4096",
            "-muxdelay",
            "0",
            "-f",
            "mpegts",
            str(transport),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=True,
    )
    backend = capture.make_backend(config("dshow", width=640, height=360, fps=30), "ffmpeg")
    backend.preview_size = (640, 360)
    backend.native_preview = capture.NativePreview(backend)
    backend.native_demuxer = NativeTransportStreamDemuxer()
    frames = []
    backend.set_encoded_sink(lambda data, **metadata: frames.append((data, metadata)))
    try:
        backend._drain_native(
            SimpleNamespace(stdout=io.BytesIO(transport.read_bytes()), wait=lambda timeout: 0),
            threading.Event(),
        )
        assert len(frames) == 11
        stamps = [metadata["timestamp_us"] for _, metadata in frames]
        assert stamps[0] == 0
        assert stamps[-1] == pytest.approx(2000000, abs=100)
        assert all(metadata["frame_complete"] for _, metadata in frames)
        assert all(len(data) <= 65536 for data, _ in frames)
        assert backend.native_preview.hub.stats()["published_frames"] == 11
    finally:
        backend.native_preview.close()
