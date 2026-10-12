import threading
import time
from types import SimpleNamespace

import pytest

from campix import runtime
from campix.capture import CaptureError
from campix.config import Settings
from campix.encoded import ParsedAccessUnit
from campix.encoding import EncoderSelection
from campix.models import CameraConfig
from campix.runtime import Camera


def camera_stub(tmp_path):
    events = []
    camera = Camera(
        CameraConfig(id="concurrency", name="Test-only stub", backend="synthetic"),
        Settings(data_dir=tmp_path),
        None,
        SimpleNamespace(publish=lambda *args, **kwargs: events.append(args)),
        {"encoders": ["libx264"]},
    )
    camera.status, camera.jpeg, camera.sequence = "online", b"test-only-jpeg", 1
    camera.frame_times.append(time.monotonic())
    camera.encoding_ready.set()
    camera.encoding_selection = EncoderSelection("libx264", False, "auto", ())
    camera.encoded_hub.publish(
        ParsedAccessUnit(b"test-only-coded-packet", True, b"test-only-config"),
        epoch=camera.encoded_hub.epoch,
    )
    return camera, events


def test_recording_process_start_does_not_hold_capture_condition(tmp_path, monkeypatch):
    camera, _ = camera_stub(tmp_path)
    entered, release, captured = threading.Event(), threading.Event(), threading.Event()
    results, errors = [], []

    class BlockedRecorder:
        def __init__(self, *args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Test-only recording constructor was not released")
            self.media_id = "a" * 32

    monkeypatch.setattr(runtime, "EncodedRecorder", BlockedRecorder)

    def record():
        try:
            results.append(camera.start_recording())
        except Exception as exc:
            errors.append(exc)

    def capture():
        with camera.condition:
            camera.sequence += 1
            camera.frame_times.append(time.monotonic())
            camera.condition.notify_all()
            captured.set()

    request = threading.Thread(target=record)
    capture_worker = threading.Thread(target=capture)
    request.start()
    try:
        assert entered.wait(1)
        capture_worker.start()
        capture_progressed = captured.wait(0.5)
        if capture_progressed:
            started = time.monotonic()
            frame = camera.frame(timeout=0.05)
            frame_elapsed = time.monotonic() - started
        else:
            frame, frame_elapsed = None, None
    finally:
        release.set()
        request.join(3)
        if capture_worker.ident is not None:
            capture_worker.join(3)
    assert not request.is_alive() and not capture_worker.is_alive()
    assert not errors
    assert capture_progressed, "Recording startup blocked the capture worker's condition"
    assert frame == (2, b"test-only-jpeg")
    assert frame_elapsed < 0.2
    assert results == [{"recording": True, "media_id": "a" * 32}]


def test_stopping_camera_rejects_new_recording_before_constructor(tmp_path, monkeypatch):
    camera, _ = camera_stub(tmp_path)
    camera.stopping = True
    monkeypatch.setattr(
        runtime, "EncodedRecorder", lambda *args, **kwargs: pytest.fail("Camera is stopping")
    )
    with pytest.raises(CaptureError, match="stopping"):
        camera.start_recording()


@pytest.mark.parametrize("failure", ["halt", "stopping", "stalled", "stale", "missing"])
def test_frame_packet_never_returns_stale_or_stopped_source(tmp_path, failure):
    camera, _ = camera_stub(tmp_path)
    if failure == "halt":
        camera.halt.set()
    elif failure == "stopping":
        camera.stopping = True
    elif failure == "stalled":
        camera.status = "stalled"
    elif failure == "stale":
        camera.frame_times[-1] = time.monotonic() - 6
    else:
        camera.jpeg = None
    with pytest.raises(CaptureError, match="current frame"):
        camera.frame_packet(timeout=0)


def test_failed_recording_stop_never_publishes_finalized_event(tmp_path):
    camera, events = camera_stub(tmp_path)

    def failed_stop():
        raise RuntimeError("Test-only recording failure; partial retained")

    camera.recorder = SimpleNamespace(stop=failed_stop)
    with pytest.raises(RuntimeError, match="partial retained"):
        camera.stop_recording()
    assert camera.recorder is None
    assert not any(event[0] == "recording.stopped" for event in events)
