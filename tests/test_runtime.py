import json
import threading
import time

import numpy as np
import pytest
from PIL import Image

from campix import runtime
from campix.capture import Backend, CaptureError
from campix.config import Settings
from campix.db import Database
from campix.models import CameraConfig


class Bus:
    def publish(self, *args, **kwargs):
        pass


@pytest.fixture
def manager(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, demo=True)
    db = Database(tmp_path / "campix.sqlite3")
    monkeypatch.setattr(runtime, "encoder_probe", lambda ffmpeg: {"encoders": []})
    manager = runtime.CameraManager(db, settings, None, Bus())
    try:
        yield manager
    finally:
        manager.stop()
        db.close()


@pytest.mark.parametrize("operation", ["save", "remove"])
def test_profile_changes_refuse_a_capture_worker_that_has_not_stopped(manager, monkeypatch, operation):
    entered, release = threading.Event(), threading.Event()
    opened = []

    class StuckDriver(Backend):
        def open(self):
            opened.append(self)

        def read(self):
            entered.set()
            release.wait(5)
            return Image.new("RGB", (160, 120))

    monkeypatch.setattr(runtime, "make_backend", lambda config, ffmpeg: StuckDriver(config))
    config = CameraConfig(id="camera", name="Camera", backend="synthetic", width=160, height=120)
    manager.save(config)
    previous = manager.get(config.id)
    assert entered.wait(2)
    real_join = previous.capture_thread.join
    monkeypatch.setattr(previous.capture_thread, "join", lambda timeout: real_join(timeout=0.05))
    try:
        with pytest.raises(CaptureError, match="stop"):
            if operation == "save":
                manager.save(config.model_copy(update={"name": "Changed"}))
            else:
                manager.remove(config.id)
        assert manager.get(config.id) is previous
        assert json.loads(manager.db.one("SELECT config FROM cameras")["config"])["name"] == "Camera"
        assert len(opened) == 1
    finally:
        release.set()
        real_join(timeout=2)
    assert not previous.capture_thread.is_alive()
    assert previous.sequence == 0
    if operation == "save":
        manager.save(config.model_copy(update={"name": "Changed"}))
        assert manager.get(config.id) is not previous
        assert manager.get(config.id).config.name == "Changed"
    else:
        manager.remove(config.id)
        with pytest.raises(KeyError):
            manager.get(config.id)


def test_controls_and_profile_changes_preserve_the_latest_configuration(manager, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(runtime.Camera, "start", lambda self: None)
    config = CameraConfig(id="camera", name="Camera", backend="synthetic", width=160, height=120)
    manager.save(config)
    previous = manager.get(config.id)

    def apply(values):
        entered.set()
        assert release.wait(5)
        previous.config.controls.update(values)

    monkeypatch.setattr(previous, "apply_controls", apply)
    errors = []
    saved = threading.Event()
    save_started = threading.Event()

    def controls():
        try:
            manager.controls(config.id, {"brightness": 0.5})
        except Exception as exc:
            errors.append(exc)

    def save():
        save_started.set()
        try:
            manager.save(config.model_copy(update={"name": "Changed", "controls": {}}))
        except Exception as exc:
            errors.append(exc)
        finally:
            saved.set()

    control_thread = threading.Thread(target=controls)
    save_thread = threading.Thread(target=save)
    control_thread.start()
    assert entered.wait(2)
    save_thread.start()
    try:
        assert save_started.wait(2)
        # A profile update must wait until the old control operation has persisted.
        assert not saved.wait(0.1)
    finally:
        release.set()
        control_thread.join(timeout=2)
        save_thread.join(timeout=2)
    assert not control_thread.is_alive() and not save_thread.is_alive()
    assert not errors
    persisted = json.loads(manager.db.one("SELECT config FROM cameras")["config"])
    assert persisted["name"] == "Changed"
    assert persisted["controls"] == {}
    assert manager.get(config.id).config.name == "Changed"


def test_motion_retains_the_previous_frame_when_capture_is_slower_than_analysis(manager, monkeypatch):
    config = CameraConfig(
        id="slow", name="Slow camera", backend="synthetic", fps=1,
        motion_enabled=True, motion_frames=1,
    )
    camera = runtime.Camera(config, manager.settings, None, Bus(), {"encoders": []})
    recordings = []
    monkeypatch.setattr(camera, "start_recording", lambda **kwargs: recordings.append(kwargs))
    updates = iter([0, None, None, None, 255])

    class AnalysisTicks:
        def wait(self, timeout):
            try:
                value = next(updates)
            except StopIteration:
                return True
            if value is not None:
                camera.analysis = np.full((90, 160), value, dtype=np.uint8)
                camera.analysis_sequence += 1
            return False

    camera.halt = AnalysisTicks()
    camera._motion()
    assert recordings == [{"trigger": "motion", "duration": config.motion_postroll}]


@pytest.mark.parametrize("age,sequence", [(2, 1), (6, 0)])
def test_capture_age_preserves_pipe_receipt_time_after_processing(manager, monkeypatch, age, sequence):
    config = CameraConfig(id="aged", name="Aged frame", backend="synthetic", width=160, height=120)
    camera = runtime.Camera(config, manager.settings, None, Bus(), {"encoders": []})

    class ReceivedFrame(Backend):
        def read(self):
            self.frame_received_at = time.monotonic() - age
            return Image.new("RGB", (160, 120))

    backend = ReceivedFrame(config)
    monkeypatch.setattr(runtime, "make_backend", lambda config, ffmpeg: backend)
    monkeypatch.setattr(camera.bus, "publish", lambda *args: camera.halt.set())
    camera._capture()
    assert camera.sequence == sequence
    if sequence:
        assert time.monotonic() - camera.frame_times[-1] >= age
    else:
        assert not camera.frame_times and camera.jpeg is None
        assert camera.error == "Capture unavailable (CaptureError); retrying"
