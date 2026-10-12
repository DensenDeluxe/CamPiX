"""Native packet, preview, photo and generation ownership regressions."""

import io
import threading
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from campix import runtime
from campix.capture import Backend, CaptureError
from campix.config import Settings
from campix.encoding import EncoderSelection
from campix.models import CameraConfig

from .test_encoded import AUD, geometry_sps, picture, pps


def native_data(width, height):
    config = geometry_sps(width_mbs=width // 16, height_units=height // 16, crop=None) + pps()
    return AUD + config + picture(True)


def jpeg(size):
    with Image.new("RGB", size, (16, 32, 48)) as image, io.BytesIO() as stream:
        image.save(stream, "JPEG")
        return stream.getvalue()


class TestNativeBackend(Backend):
    __test__ = False

    def __init__(self, config, photo):
        super().__init__(config)
        self.closed = threading.Event()
        self.preview_size = (160, 120)
        self.preview = jpeg(self.preview_size)
        self.photo = photo
        self.photo_threads = []

    def open(self):
        self.native_encoded, self.native_attempted = True, True
        self.started = time.monotonic()
        self.capabilities.update(
            test_status="synthetic",
            native_encoded=True,
            encoder_implementation="test-only-native-h264",
            native_encoder_hardware=True,
            capture_to_encoder_zero_copy=True,
            zero_copy_scope="test-only-native-buffer",
            encoded_source="test-only-native-h264",
        )

    def read_jpeg(self):
        if self.closed.wait(0.02):
            raise CaptureError("Test-only native backend closed")
        now = time.monotonic()
        self.encoded_sink(
            native_data(self.config.width, self.config.height),
            timestamp_us=round((now - self.started) * 1_000_000),
        )
        self.frame_received_at = now
        return self.preview

    def snapshot_jpeg(self):
        self.photo_threads.append(threading.get_ident())
        return self.photo

    def close(self):
        self.closed.set()


def native_camera(tmp_path, monkeypatch, *, photo=Ellipsis):
    config = CameraConfig(
        id="test-native-runtime",
        name="Test-only native packets",
        backend="dshow",
        width=640,
        height=480,
        fps=30,
        motion_enabled=False,
    )
    backend = TestNativeBackend(config, jpeg((640, 480)) if photo is Ellipsis else photo)
    stored, events = [], []
    store = SimpleNamespace(
        snapshot=lambda camera_id, data, trigger: stored.append(data) or {"id": "test-photo"}
    )
    camera = runtime.Camera(
        config,
        Settings(data_dir=tmp_path),
        store,
        SimpleNamespace(publish=lambda *args: events.append(args)),
        {"encoders": []},
    )
    monkeypatch.setattr(runtime, "make_backend", lambda *args: backend)
    monkeypatch.setattr(
        runtime,
        "select_encoder",
        lambda *args, **kwargs: pytest.fail("Native camera started FFmpeg encoding"),
    )
    monkeypatch.setattr(
        runtime,
        "SharedEncoder",
        lambda *args, **kwargs: pytest.fail("Native camera created a second encoder"),
    )
    return camera, backend, stored, events


def wait_online(camera):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if (
            camera.status == "online"
            and camera.encoding_ready.is_set()
            and camera.encoded_hub.stats()["ready"]
        ):
            return
        time.sleep(0.005)
    pytest.fail(f"Native camera did not become packet-ready: {camera.info()}")


def test_native_stream_uses_one_encoder_preserves_small_preview_and_worker_photo(
    tmp_path, monkeypatch
):
    camera, backend, stored, _ = native_camera(tmp_path, monkeypatch)
    camera.start()
    try:
        wait_online(camera)
        with Image.open(io.BytesIO(camera.frame()[1])) as preview:
            assert preview.size == (160, 120)
        info = camera.info()
        assert info["preview_size"] == [160, 120]
        assert info["encoding"]["selected"] == "test-only-native-h264"
        assert info["encoding"]["packet_ready"] and info["encoding"]["zero_copy_raw_frames"]
        assert camera.shared_encoder is None
        assert camera.snapshot() == {"id": "test-photo"}
        assert backend.photo_threads == [camera.capture_thread.ident]
        assert backend.photo_threads[0] != threading.get_ident()
        with Image.open(io.BytesIO(stored[0])) as photo:
            assert photo.size == (640, 480)
            photo.load()
    finally:
        camera.stop()
    assert backend.closed.is_set()
    assert not any(
        worker.is_alive()
        for worker in (camera.capture_thread, camera.motion_thread, camera.encoder_thread)
    )
    assert not camera.encoded_hub.stats()["ready"]


@pytest.mark.parametrize("photo", [None, jpeg((160, 120)), b"not a JPEG", jpeg((640, 480))[:1000]])
def test_invalid_or_truncated_full_resolution_photo_is_never_indexed(tmp_path, monkeypatch, photo):
    camera, backend, stored, _ = native_camera(tmp_path, monkeypatch, photo=photo)
    camera.start()
    try:
        wait_online(camera)
        with pytest.raises(CaptureError, match="photo"):
            camera.snapshot()
        assert not stored
        assert backend.photo_threads == [camera.capture_thread.ident]
        assert camera.status == "online"
    finally:
        camera.stop()


def test_stopped_camera_rejects_stale_frame_and_photo_immediately(tmp_path, monkeypatch):
    camera, _, stored, _ = native_camera(tmp_path, monkeypatch)
    camera.start()
    wait_online(camera)
    camera.stop()
    started = time.monotonic()
    with pytest.raises(CaptureError):
        camera.frame(timeout=0)
    with pytest.raises(CaptureError):
        camera.snapshot()
    assert time.monotonic() - started < 0.2
    assert not stored and camera.snapshot_requests.empty()


def test_stale_native_callbacks_cannot_publish_into_new_generation(tmp_path):
    config = CameraConfig(
        id="test-generation", name="Test callback generations", backend="synthetic"
    )
    camera = runtime.Camera(
        config, Settings(data_dir=tmp_path), None, SimpleNamespace(publish=lambda *args: None), {}
    )
    first, second = object(), object()
    camera.backend = first
    old = camera._native_sink(first, camera.encoded_hub.epoch)
    data = native_data(config.width, config.height)
    old(data, timestamp_us=1_000_000)
    old(data, timestamp_us=1_020_000)
    count = camera.encoded_hub.stats()["published_frames"]
    assert count >= 1
    camera.backend = second
    camera.encoded_hub.restart()
    old(data, timestamp_us=1_040_000)
    assert camera.encoded_hub.stats()["published_frames"] == count
    fresh = camera._native_sink(second, camera.encoded_hub.epoch)
    fresh(data, timestamp_us=2_000_000)
    fresh(data, timestamp_us=2_020_000)
    assert camera.encoded_hub.stats()["published_frames"] > count
    camera.halt.set()
    count = camera.encoded_hub.stats()["published_frames"]
    fresh(data, timestamp_us=2_040_000)
    assert camera.encoded_hub.stats()["published_frames"] == count
    camera.stop()


def test_native_controls_do_not_claim_packet_readiness_before_sps_pps_and_idr(
    tmp_path, monkeypatch
):
    camera, backend, _, _ = native_camera(tmp_path, monkeypatch)

    def preview_without_h264():
        if backend.closed.wait(0.02):
            raise CaptureError("Test-only backend stopped")
        backend.frame_received_at = time.monotonic()
        return backend.preview

    monkeypatch.setattr(backend, "read_jpeg", preview_without_h264)
    monkeypatch.setattr(
        runtime,
        "EncodedRecorder",
        lambda *args, **kwargs: pytest.fail("Unready source launched recording"),
    )
    camera.start()
    try:
        assert camera.encoding_ready.wait(2)
        camera.frame(timeout=2)
        state = camera.info()["encoding"]
        assert state["selected"] == "test-only-native-h264"
        assert not state["packet_ready"] and not state["profile_tested"]
        with pytest.raises(CaptureError, match="still preparing"):
            camera.start_recording()
    finally:
        camera.stop()


def test_three_byte_aud_native_timestamp_survives_single_burst(tmp_path, monkeypatch):
    config = CameraConfig(id="test-native-pts", name="Test native clock", backend="synthetic")
    camera = runtime.Camera(
        config, Settings(data_dir=tmp_path), None, SimpleNamespace(publish=lambda *args: None), {}
    )
    backend = object()
    camera.backend = backend
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    sink = camera._native_sink(backend, camera.encoded_hub.epoch)
    data = b"\x00\x00\x01" + native_data(config.width, config.height)[4:]
    sink(data, timestamp_us=0, frame_complete=True)
    sink(data, timestamp_us=200000, frame_complete=True)
    subscription = camera.encoded_hub.subscribe("live")
    assert subscription.read(timeout=0).captured_at == pytest.approx(1000.2)
    assert camera.encoded_hub.stats()["fps"] == pytest.approx(5.0)
    subscription.close()
    camera.stop()


def test_native_failure_restarts_generation_and_uses_one_shared_fallback_encoder(
    tmp_path, monkeypatch
):
    camera, native, _, events = native_camera(tmp_path, monkeypatch)
    calls = []

    class FailingNative(TestNativeBackend):
        def read_jpeg(self):
            raise CaptureError("Test-only native camera failure")

    failing = FailingNative(camera.config, native.photo)

    class JpegFallback(Backend):
        def __init__(self, config):
            super().__init__(config)
            self.closed = threading.Event()
            self.sample = jpeg((config.width, config.height))

        def read_jpeg(self):
            if self.closed.wait(0.02):
                raise CaptureError("Test fallback closed")
            self.frame_received_at = time.monotonic()
            return self.sample

        def close(self):
            self.closed.set()

    fallback = JpegFallback(camera.config)
    backends = iter((failing, fallback))
    selection = EncoderSelection(codec="libx264", hardware=False, requested="auto", output_args=())

    class SharedFallback:
        def __init__(self, owner, selected):
            self.camera, self.selection = owner, selected
            self.thread = threading.Thread(target=lambda: None)
            self.reader_thread = None
            self.sessions_started = 0
            self.error = None
            calls.append(self)

        def start(self):
            self.sessions_started += 1

        def stop(self):
            pass

    monkeypatch.setattr(runtime, "make_backend", lambda *args: next(backends))
    monkeypatch.setattr(runtime, "select_encoder", lambda *args, **kwargs: selection)
    monkeypatch.setattr(runtime, "SharedEncoder", SharedFallback)
    camera.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if camera.backend is fallback and camera.encoding_selection is selection:
                break
            time.sleep(0.005)
        assert camera.backend is fallback and camera.encoding_selection is selection
        assert failing.closed.is_set() and fallback.native_disabled
        assert camera.encoded_hub.epoch > 1
        assert len(calls) == 1 and calls[0].sessions_started == 1
        assert any(event[0] == "encoding.fallback" for event in events)
    finally:
        camera.stop()
    assert fallback.closed.is_set()
    assert not any(
        worker.is_alive()
        for worker in (camera.capture_thread, camera.motion_thread, camera.encoder_thread)
    )


def test_native_sps_mismatch_cannot_claim_4k_ready_and_enters_capture_fallback(
    tmp_path, monkeypatch
):
    camera, backend, _, events = native_camera(tmp_path, monkeypatch)
    camera.config = camera.config.model_copy(update={"width": 3840, "height": 2160})
    # The adapter still returns valid 640x480 SPS and 160x120 previews. Its native
    # mode/control metadata must not qualify the requested 3840x2160 profile.
    camera.start()
    try:
        deadline = time.monotonic() + 1
        while not camera.native_disabled and time.monotonic() < deadline:
            time.sleep(0.005)
        assert camera.native_disabled and backend.closed.is_set()
        state = camera.info()["encoding"]
        assert not state["packet_ready"] and not state["profile_tested"]
        assert camera.encoded_hub.stats()["published_frames"] == 0
        assert any(event[0] == "encoding.fallback" for event in events)
    finally:
        camera.stop()


@pytest.mark.parametrize("received_delta,timestamp_us", [(0.0, 10_000_000), (10.0, 20_000)])
def test_native_clock_outside_current_window_never_publishes_as_live(
    tmp_path, monkeypatch, received_delta, timestamp_us
):
    config = CameraConfig(
        id="test-clock-bounds", name="Test native clock bounds", backend="synthetic"
    )
    camera = runtime.Camera(
        config, Settings(data_dir=tmp_path), None, SimpleNamespace(publish=lambda *args: None), {}
    )
    backend = object()
    camera.backend = backend
    arrival = [1000.0]
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: arrival[0]))
    sink = camera._native_sink(backend, camera.encoded_hub.epoch)
    data = native_data(config.width, config.height)
    sink(data, timestamp_us=0, frame_complete=True)
    assert camera.encoded_hub.stats()["published_frames"] == 1
    arrival[0] += received_delta
    with pytest.raises(CaptureError, match="timestamp|clock"):
        sink(data, timestamp_us=timestamp_us, frame_complete=True)
    assert camera.encoded_hub.stats()["published_frames"] == 1
    camera.stop()
