"""Failure and ownership checks for background hardware-encoder preparation."""

import io
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from campix import runtime
from campix.app import create_app
from campix.capture import CaptureError
from campix.config import Settings
from campix.encoding import EncoderSelection, EncoderUnavailable, encoder_args
from campix.mediamtx import MediaPlane, Publisher
from campix.models import CameraConfig
from campix.processes import owned_process

from .conftest import signin
from .test_service import wait_online, wait_packet_ready


def selection_for(config):
    inputs, outputs = encoder_args("libx264", config.width, config.height, config.fps)
    return EncoderSelection(
        codec="h264_nvenc",
        hardware=True,
        requested=config.encoder,
        input_args=inputs,
        output_args=outputs,
    )


def pipeline_app(tmp_path, monkeypatch, *, encoder="auto"):
    # These tests inject preparation outcomes, never depend on installed GPU drivers.
    monkeypatch.setattr(
        runtime,
        "encoder_probe",
        lambda ffmpeg: {
            "available": True,
            "encoders": ["libx264", "h264_nvenc"],
            "hardware_verified": False,
        },
    )
    app = create_app(Settings(data_dir=tmp_path, demo=True, min_free_mb=32))
    app.state.auth.create_user("admin", "test-only-admin-password")
    config = CameraConfig(
        id="demo",
        name="Synthetic pipeline test",
        backend="synthetic",
        width=640,
        height=360,
        fps=10,
        encoder=encoder,
    )
    app.state.db.execute("INSERT INTO cameras VALUES (?,?)", (config.id, config.model_dump_json()))
    return app, config


def test_background_profile_probe_keeps_authenticated_jpeg_and_snapshots_responsive(
    tmp_path, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    profiles = []
    app, config = pipeline_app(tmp_path, monkeypatch)
    selection = selection_for(config)

    def blocked_probe(ffmpeg, requested, **kwargs):
        profiles.append((requested, kwargs["width"], kwargs["height"], kwargs["fps"]))
        entered.set()
        assert release.wait(10), "The test did not release the profile probe"
        return selection

    monkeypatch.setattr(runtime, "select_encoder", blocked_probe)
    with TestClient(app) as client:
        try:
            signin(client)
            assert entered.wait(3)
            wait_online(client)
            camera = app.state.cameras.get("demo")
            previous = camera.sequence
            started = time.monotonic()
            preview = client.get("/api/v1/cameras/demo/preview.jpg")
            snapshot = client.post("/api/v1/cameras/demo/snapshot")
            assert time.monotonic() - started < 2
            assert preview.status_code == 200 and preview.content.startswith(b"\xff\xd8")
            assert snapshot.status_code == 201
            assert camera.frame(previous, timeout=1)[0] > previous
            assert not camera.encoding_ready.is_set()
            status = client.get("/api/v1/cameras").json()[0]
            assert status["encoding"]["preparing"] is True
            assert status["encoding"]["selected"] is None
            recording = client.post("/api/v1/cameras/demo/recording", json={})
            assert recording.status_code == 409
            assert "still being checked" in recording.text
            assert client.get("/api/v1/media?kind=video").json()["total"] == 0
        finally:
            release.set()
        assert camera.encoding_ready.wait(3)
        assert camera.encoding_selection is selection
        assert profiles == [("auto", 640, 360, 10)]
    assert not camera.encoder_thread.is_alive()
    assert not camera.capture_thread.is_alive()
    assert not camera.motion_thread.is_alive()


@pytest.mark.parametrize("outcome", ["selection", "failure"])
def test_cancelled_camera_does_not_adopt_a_late_probe_result(tmp_path, monkeypatch, outcome):
    entered, cancelled, release = threading.Event(), threading.Event(), threading.Event()
    config = CameraConfig(id="late", name="Late result", backend="synthetic")
    selection = selection_for(config)
    events, stop_errors = [], []
    camera = runtime.Camera(
        config,
        Settings(data_dir=tmp_path),
        None,
        SimpleNamespace(publish=lambda *args: events.append(args)),
        {"encoders": ["h264_nvenc"]},
    )

    def late_probe(ffmpeg, requested, *, cancel, **kwargs):
        entered.set()
        assert cancel.wait(5), "Camera shutdown did not cancel encoder preparation"
        cancelled.set()
        assert release.wait(5)
        if outcome == "failure":
            raise EncoderUnavailable("A canceled GPU profile failed")
        return selection

    def stop():
        try:
            camera.stop()
        except Exception as exc:
            stop_errors.append(exc)

    monkeypatch.setattr(runtime, "select_encoder", late_probe)
    camera.prepare_encoding()
    worker = threading.Thread(target=stop)
    try:
        assert entered.wait(3)
        worker.start()
        assert cancelled.wait(3)
        assert camera.encoding_selection is None
        assert not camera.encoding_ready.is_set()
    finally:
        release.set()
        if worker.ident is not None:
            worker.join(timeout=3)
        camera.stop()
    assert not worker.is_alive() and not camera.encoder_thread.is_alive()
    assert camera.encoding_ready.is_set()
    assert camera.encoding_selection is None and camera.encoding_error is None
    assert not events and not stop_errors


@pytest.mark.parametrize("encoder", ["auto", "h264_nvenc"])
def test_unusable_encoder_profile_rejects_recording_before_media_reservation(
    tmp_path, monkeypatch, encoder
):
    app, _ = pipeline_app(tmp_path, monkeypatch, encoder=encoder)

    def unavailable(*args, **kwargs):
        raise EncoderUnavailable("No usable H.264 encoder for this camera profile")

    monkeypatch.setattr(runtime, "select_encoder", unavailable)
    with TestClient(app) as client:
        signin(client)
        wait_online(client)
        camera = app.state.cameras.get("demo")
        assert camera.encoding_ready.wait(3)
        assert camera.encoding_selection is None
        with monkeypatch.context() as blocked:
            blocked.setattr(
                app.state.store,
                "reserve",
                lambda extension: pytest.fail("Failed encoder reserved media"),
            )
            blocked.setattr(
                runtime,
                "EncodedRecorder",
                lambda *args, **kwargs: pytest.fail("Failed encoder launched recording"),
            )
            response = client.post("/api/v1/cameras/demo/recording", json={})
        assert response.status_code == 409 and "No usable H.264 encoder" in response.text
        assert camera.recorder is None
        assert client.get("/api/v1/cameras/demo/preview.jpg").status_code == 200
        assert client.get("/api/v1/media").json()["total"] == 0
        assert not list(app.state.store.root.iterdir())
        assert app.state.db.one("SELECT id FROM events WHERE kind='encoding.unavailable'")


def test_recording_and_publishing_share_the_prepared_selection(tmp_path, monkeypatch):
    app, config = pipeline_app(tmp_path, monkeypatch)
    selection = selection_for(config)
    monkeypatch.setattr(runtime, "select_encoder", lambda *args, **kwargs: selection)
    recordings = []

    def record(*args, selection, **kwargs):
        recorder = SimpleNamespace(
            selection=selection,
            media_id="selection-test",
            done=threading.Event(),
            deadline=None,
            dropped=0,
            offer=lambda jpeg: None,
            stop=lambda: {"id": "selection-test"},
        )
        recordings.append(recorder)
        return recorder

    with TestClient(app) as client:
        signin(client)
        wait_online(client)
        camera = app.state.cameras.get("demo")
        assert camera.encoding_ready.wait(3)
        wait_packet_ready(client)
        monkeypatch.setattr(runtime, "EncodedRecorder", record)
        assert client.post("/api/v1/cameras/demo/recording", json={}).status_code == 200
        assert recordings[0].selection is selection
        publisher = Publisher.__new__(Publisher)
        publisher.camera, publisher.settings = camera, app.state.settings
        publisher.halt = threading.Event()
        publisher.proc, publisher.error, publisher.online = None, None, False
        publisher.selection, publisher.failed_encoders = None, set()
        received = []

        class Pipe(io.BytesIO):
            def write(self, data):
                received.append(data)
                publisher.halt.set()
                return super().write(data)

        pipe = Pipe()
        # Preparation has completed before replacing the shared subprocess spawner.
        with monkeypatch.context() as publishing:
            publishing.setattr(
                "campix.mediamtx.subprocess.Popen",
                lambda *args, **kwargs: SimpleNamespace(stdin=pipe, poll=lambda: None),
            )
            publishing.setattr("campix.mediamtx.terminate_process", lambda process: None)
            publisher._run()
        assert publisher.selection is selection
        assert received and received[0].startswith(b"\x47")
        assert pipe.closed and publisher.online is False
        assert client.delete("/api/v1/cameras/demo/recording").status_code == 200
        event = app.state.db.one("SELECT data FROM events WHERE kind='recording.started'")
        assert '"encoder": "h264_nvenc"' in event["data"]


def test_selected_stream_remux_failure_retains_partial_without_indexing_success(
    tmp_path, monkeypatch
):
    app, config = pipeline_app(tmp_path, monkeypatch)
    selection = selection_for(config)
    monkeypatch.setattr(runtime, "select_encoder", lambda *args, **kwargs: selection)
    original_recorder = runtime.EncodedRecorder
    recordings = []

    def failed_hardware(command, **kwargs):
        # Simulate a selected driver failing after writing an incomplete file.
        return owned_process(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'interrupted synthetic encoder'); raise SystemExit(1)",
                command[-1],
            ],
            **kwargs,
        )

    def record(*args, **kwargs):
        recorder = original_recorder(*args, **kwargs)
        recordings.append(recorder)
        return recorder

    with TestClient(app) as client:
        signin(client)
        wait_online(client)
        camera = app.state.cameras.get("demo")
        assert camera.encoding_ready.wait(3)
        wait_packet_ready(client)
        monkeypatch.setattr(runtime, "EncodedRecorder", record)
        # This injection must not race any background encoder probe.
        monkeypatch.setattr("campix.recording.owned_process", failed_hardware)
        response = client.post("/api/v1/cameras/demo/recording", json={})
        assert response.status_code == 200
        recorder = recordings[0]
        assert recorder.selection is selection
        assert recorder.done.wait(3)
        assert recorder.error and recorder.result is None
        assert recorder.partial.read_bytes() == b"interrupted synthetic encoder"
        assert not recorder.path.exists()
        assert recorder.proc.poll() is not None and recorder.proc.stdin.closed
        assert client.get("/api/v1/media").json()["total"] == 0
        assert app.state.db.one("SELECT id FROM events WHERE kind='recording.error'")
        assert client.get("/api/v1/cameras/demo/preview.jpg").status_code == 200
        with pytest.raises(RuntimeError, match="Recording failed"):
            recorder.stop()


@pytest.mark.parametrize("requested", ["auto", "h264_nvenc"])
def test_publisher_reaps_failed_hardware_and_respects_auto_or_explicit_choice(
    tmp_path, monkeypatch, requested
):
    config = CameraConfig(
        id="fallback",
        name="Publisher recovery",
        backend="synthetic",
        encoder=requested,
        width=640,
        height=360,
        fps=30,
    )
    hardware = selection_for(config)
    inputs, outputs = encoder_args("libx264", config.width, config.height, config.fps)
    replacement = EncoderSelection(
        codec="libx264",
        hardware=False,
        requested="auto",
        input_args=inputs,
        output_args=outputs,
        fallback_reason="h264_nvenc: runtime-encoding-failed; selected tested libx264",
    )
    preparing, release, written, waiting = (threading.Event() for _ in range(4))
    selected_calls, processes, errors_at_retry = [], [], []
    original_spawn = subprocess.Popen
    publisher = Publisher.__new__(Publisher)

    class ImmediateBackoff(threading.Event):
        def wait(self, timeout=None):
            if timeout == 3:
                errors_at_retry.append(publisher.error)
                return self.is_set()
            return super().wait(timeout)

    def select(ffmpeg, encoder, **kwargs):
        selected_calls.append((threading.get_ident(), encoder, kwargs))
        preparing.set()
        assert release.wait(5)
        return replacement

    def frame(sequence, timeout):
        if written.is_set():
            waiting.set()
            publisher.halt.wait(5)
        return sequence + 1, b"explicit-synthetic-jpeg"

    def launch(command, **kwargs):
        # Real children expose leaked ownership even though these test streams are fake.
        process = original_spawn(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
            **kwargs,
        )
        stdin = process.stdin
        ordinal = len(processes)

        class Pipe:
            def write(self, data):
                if ordinal == 0:
                    raise BrokenPipeError("private-upstream-credential")
                return stdin.write(data)

            def flush(self):
                stdin.flush()
                written.set()

            @property
            def closed(self):
                return stdin.closed

            def close(self):
                stdin.close()

        process.stdin = Pipe()
        processes.append(process)
        return process

    publisher.settings = Settings(data_dir=tmp_path)
    ready = threading.Event()
    ready.set()
    publisher.camera = SimpleNamespace(
        config=config,
        encoding_ready=ready,
        encoding_selection=hardware,
        encoders={"encoders": ["h264_nvenc", "libx264"]},
        frame=frame,
    )
    publisher.halt = ImmediateBackoff()
    publisher.proc, publisher.error, publisher.online = None, None, False
    publisher.selection, publisher.failed_encoders = None, set()
    publisher.thread = threading.Thread(target=publisher._run)
    monkeypatch.setattr("campix.mediamtx.subprocess.Popen", launch)
    monkeypatch.setattr("campix.mediamtx.select_encoder", select)
    publisher.thread.start()
    try:
        if requested == "auto":
            assert preparing.wait(3)
            assert processes[0].poll() is not None and processes[0].stdin.closed
            assert not written.is_set()
            assert publisher.failed_encoders == {"h264_nvenc"}
            release.set()
        assert waiting.wait(3)
        assert len(processes) == 2
        assert processes[0].poll() is not None and processes[1].poll() is None
        plane = MediaPlane(publisher.settings, None, SimpleNamespace(publish=lambda *args: None))
        plane.publishers = {config.id: publisher}
        plane.ready_paths = {config.id}
        state = plane.status()["publishers"][config.id]
        assert state["online"] is True and state["error"] is None
        if requested == "auto":
            assert publisher.selection.codec == replacement.codec
            assert publisher.selection.output_args is replacement.output_args
            assert len(selected_calls) == 1
            worker_id, encoder, kwargs = selected_calls[0]
            assert worker_id == publisher.thread.ident and worker_id != threading.get_ident()
            assert encoder == "auto" and kwargs["excluded"] == {"h264_nvenc"}
            assert (kwargs["width"], kwargs["height"], kwargs["fps"]) == (640, 360, 30)
            assert kwargs["cancel"] is publisher.halt
            assert state["encoder"] == "libx264" and state["hardware"] is False
            assert "failed" in state["fallback_reason"] and "libx264" in state["fallback_reason"]
            assert "another encoder" in errors_at_retry[0]
        else:
            assert not selected_calls and not publisher.failed_encoders
            assert publisher.selection is hardware
            assert state["encoder"] == "h264_nvenc" and state["hardware"] is True
            assert state["fallback_reason"] is None
        assert "private-upstream-credential" not in repr(errors_at_retry)
    finally:
        release.set()
        publisher.stop()
    assert not publisher.thread.is_alive()
    assert all(process.poll() is not None and process.stdin.closed for process in processes)


def test_publisher_cancellation_during_fallback_does_not_launch_a_late_encoder(
    tmp_path, monkeypatch
):
    config = CameraConfig(id="cancel", name="Canceled fallback", backend="synthetic")
    hardware = selection_for(config)
    inputs, outputs = encoder_args("libx264", config.width, config.height, config.fps)
    replacement = EncoderSelection(
        codec="libx264",
        hardware=False,
        requested="auto",
        input_args=inputs,
        output_args=outputs,
    )
    entered, cancel_observed = threading.Event(), threading.Event()
    spawned, terminated = [], []
    publisher = Publisher.__new__(Publisher)

    class ImmediateBackoff(threading.Event):
        def wait(self, timeout=None):
            return self.is_set() if timeout == 3 else super().wait(timeout)

    class FailedPipe(io.BytesIO):
        def write(self, data):
            raise BrokenPipeError("Failed selected GPU")

    def launch(*args, **kwargs):
        process = SimpleNamespace(stdin=FailedPipe())
        spawned.append(process)
        return process

    def late_selection(ffmpeg, requested, *, cancel, **kwargs):
        entered.set()
        assert cancel.wait(5)
        cancel_observed.set()
        return replacement

    ready = threading.Event()
    ready.set()
    publisher.camera = SimpleNamespace(
        config=config,
        encoding_ready=ready,
        encoding_selection=hardware,
        encoders={"encoders": ["h264_nvenc", "libx264"]},
        frame=lambda sequence, timeout: (sequence + 1, b"synthetic"),
    )
    publisher.settings = Settings(data_dir=tmp_path)
    publisher.halt = ImmediateBackoff()
    publisher.proc, publisher.error, publisher.online = None, None, False
    publisher.selection, publisher.failed_encoders = None, set()
    publisher.thread = threading.Thread(target=publisher._run)
    monkeypatch.setattr("campix.mediamtx.subprocess.Popen", launch)
    monkeypatch.setattr("campix.mediamtx.select_encoder", late_selection)
    monkeypatch.setattr(
        "campix.mediamtx.terminate_process", lambda process: terminated.append(process)
    )
    publisher.thread.start()
    try:
        assert entered.wait(3)
    finally:
        publisher.stop()
    assert cancel_observed.is_set()
    assert len(spawned) == 1, "A canceled fallback reopened an encoder process"
    assert spawned[0] in terminated and spawned[0].stdin.closed
    assert not publisher.thread.is_alive() and publisher.online is False


def test_expired_publisher_hardware_failure_allows_retry_after_a_transient_outage(
    tmp_path, monkeypatch
):
    config = CameraConfig(id="retry", name="Transient failure", backend="synthetic")
    hardware = selection_for(config)
    inputs, outputs = encoder_args("libx264", config.width, config.height, config.fps)
    replacement = EncoderSelection(
        codec="libx264",
        hardware=False,
        requested="auto",
        input_args=inputs,
        output_args=outputs,
    )
    clock = [1000.0]
    phases, selections, children, retries = [], [], [], []
    publisher = Publisher.__new__(Publisher)

    class AdvancingBackoff(threading.Event):
        def wait(self, timeout=None):
            if timeout == 3 and not self.is_set():
                retries.append(publisher.selection.codec)
                expiration = publisher.failed_until["h264_nvenc"]
                # First restart must avoid the failing hardware; the later one may retry it.
                clock[0] = expiration - 1 if len(retries) == 1 else expiration + 1
            return self.is_set()

    def select(ffmpeg, requested, **kwargs):
        selections.append(frozenset(kwargs["excluded"]))
        return replacement

    def frame(sequence, timeout):
        if len(children) == 2 and sequence >= 0:
            raise CaptureError("Transient stream disruption")
        return sequence + 1, b"synthetic-frame"

    def launch(*args, **kwargs):
        ordinal = len(children)
        phases.append(publisher.selection.codec)

        class Pipe(io.BytesIO):
            def write(self, data):
                if ordinal == 0:
                    raise BrokenPipeError("Initial hardware session failed")
                result = super().write(data)
                if ordinal == 2:
                    publisher.halt.set()
                return result

        child = SimpleNamespace(stdin=Pipe(), terminated=False)
        children.append(child)
        return child

    ready = threading.Event()
    ready.set()
    publisher.camera = SimpleNamespace(
        config=config,
        encoding_ready=ready,
        encoding_selection=hardware,
        encoders={"encoders": ["h264_nvenc", "libx264"]},
        frame=frame,
    )
    publisher.settings = Settings(data_dir=tmp_path)
    publisher.halt = AdvancingBackoff()
    publisher.proc, publisher.error, publisher.online = None, None, False
    publisher.selection, publisher.failed_encoders, publisher.failed_until = None, set(), {}
    monkeypatch.setattr("campix.mediamtx.time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr("campix.mediamtx.subprocess.Popen", launch)
    monkeypatch.setattr("campix.mediamtx.select_encoder", select)
    monkeypatch.setattr(
        "campix.mediamtx.terminate_process", lambda child: setattr(child, "terminated", True)
    )
    publisher._run()
    assert phases == ["h264_nvenc", "libx264", "h264_nvenc"]
    assert selections == [frozenset({"h264_nvenc"})]
    assert not publisher.failed_encoders and not publisher.failed_until
    assert publisher.selection is hardware
    assert all(child.terminated and child.stdin.closed for child in children)
    assert publisher.online is False
