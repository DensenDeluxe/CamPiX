import json
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from campix.app import create_app
from campix.config import Settings
from campix.db import Database
from campix.process_lock import ProcessLock

from .conftest import signin
from .test_service import wait_online


def test_restart_preserves_users_media_controls_and_schedules(tmp_path):
    settings = Settings(data_dir=tmp_path, demo=True, min_free_mb=32)
    first = create_app(settings)
    first.state.auth.create_user("admin", "test-only-admin-password")
    with TestClient(first) as client:
        signin(client)
        wait_online(client)
        snapshot = client.post("/api/v1/cameras/demo/snapshot").json()
        client.patch("/api/v1/cameras/demo/controls", json={"brightness": 0.4})
        client.post(
            "/api/v1/schedules", json={"id": "persist", "name": "Persist", "camera_id": "demo"}
        )
        next_run = client.get("/api/v1/schedules").json()[0]["next_run"]
    second = create_app(settings)
    with TestClient(second) as client:
        signin(client)
        assert client.get("/api/v1/media").json()["items"][0]["id"] == snapshot["id"]
        assert client.get("/api/v1/cameras").json()[0]["controls"]["brightness"] == 0.4
        assert client.get("/api/v1/schedules").json()[0]["next_run"] == next_run


def test_expired_and_disabled_sessions_cannot_access_media(client, application):
    signin(client, "viewer")
    application.state.db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
    assert client.get("/api/v1/media").status_code == 401
    signin(client, "viewer")
    application.state.db.execute("UPDATE users SET disabled=1 WHERE username='viewer'")
    assert client.get("/api/v1/media").status_code == 401


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_timelapse_job_completes_and_rejects_invalid_selection(client, application):
    signin(client)
    wait_online(client)
    photos = [client.post("/api/v1/cameras/demo/snapshot").json()["id"] for _ in range(3)]
    assert (
        client.post("/api/v1/timelapse", json={"media_ids": [photos[0], photos[0]]}).status_code
        == 422
    )
    response = client.post("/api/v1/timelapse", json={"media_ids": photos, "fps": 2})
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.05)
    assert job["status"] == "completed", job
    item = application.state.store.get(job["media_id"])
    assert item["trigger"] == "timelapse" and item["duration"] == 1.5
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-of",
            "json",
            str(application.state.store.path(item["filename"])),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout)["streams"][0]["codec_name"] == "h264"
    assert client.get("/api/v1/media").json()["total"] == 4


def test_storage_reserve_and_retention_keep_locked_items(client, application, monkeypatch):
    signin(client)
    wait_online(client)
    locked = client.post("/api/v1/cameras/demo/snapshot").json()
    expired = client.post("/api/v1/cameras/demo/snapshot").json()
    client.patch(f"/api/v1/media/{locked['id']}", json={"locked": True})
    application.state.db.execute("UPDATE media SET created=0")
    application.state.store.retention()
    assert client.get("/api/v1/media").json()["items"][0]["id"] == locked["id"]
    assert not application.state.store.path(expired["filename"]).exists()
    monkeypatch.setattr(
        "campix.media.shutil.disk_usage", lambda path: shutil._ntuple_diskusage(1000, 999, 1)
    )
    assert client.post("/api/v1/cameras/demo/snapshot").status_code == 409
    assert client.post("/api/v1/cameras/demo/recording", json={}).status_code == 409


def test_interrupted_files_retained_and_delete_journal_recovers(client, application):
    signin(client)
    wait_online(client)
    item = client.post("/api/v1/cameras/demo/snapshot").json()
    path = application.state.store.path(item["filename"])
    path.replace(path.with_suffix(".jpg.deleted"))
    partial = application.state.store.root / "interrupted.mp4.partial"
    partial.write_bytes(b"incomplete")
    application.state.store.reconcile()
    assert path.exists() and partial.exists()
    assert client.get("/api/v1/media").json()["total"] == 1


def test_os_lock_excludes_second_process_and_recovers(tmp_path):
    lock = ProcessLock(tmp_path)
    lock.acquire()
    other = ProcessLock(tmp_path)
    with pytest.raises(RuntimeError, match="Another CamPiX"):
        other.acquire()
    lock.close()
    other.acquire()
    other.close()


def test_newer_database_is_not_downgraded(tmp_path):
    path = tmp_path / "newer.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="downgrade refused"):
        Database(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 99


def test_online_backup_is_consistent_and_revokes_copied_sessions(client, application, tmp_path):
    signin(client)
    destination = tmp_path / "backup.sqlite3"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "campix",
            "--data-dir",
            str(application.state.settings.data_dir),
            "backup",
            str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(destination) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3


def test_persisted_synthetic_camera_does_not_start_in_production(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, demo=True))
    app.state.auth.create_user("admin", "test-only-admin-password")
    with TestClient(app):
        pass
    app = create_app(Settings(data_dir=tmp_path, demo=False))
    with TestClient(app) as client:
        signin(client)
        camera = client.get("/api/v1/cameras").json()[0]
        assert camera["status"] == "stopped" and camera["fps"] == 0


def test_cli_invalid_bootstrap_password_is_never_echoed(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "campix",
            "--data-dir",
            str(tmp_path),
            "init-admin",
            "--password-stdin",
        ],
        input="too-short\n",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "too-short" not in result.stdout + result.stderr


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_duration_finalizes_with_real_preroll(client, application):
    signin(client)
    wait_online(client)
    camera = application.state.cameras.get("demo")
    time.sleep(0.6)
    before = len(camera.preroll)
    assert before >= 3
    response = client.post("/api/v1/cameras/demo/recording", json={"duration": 1})
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not application.state.db.one(
        "SELECT id FROM media WHERE kind='video'"
    ):
        time.sleep(0.05)
    item = client.get("/api/v1/media?kind=video").json()["items"][0]
    assert item["duration"] >= before / camera.config.fps + 0.8


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_encoder_failure_never_indexes_success(client, application, monkeypatch):
    signin(client)
    wait_online(client)
    spawn = subprocess.Popen
    monkeypatch.setattr(
        "campix.recording.subprocess.Popen",
        lambda *args, **kwargs: spawn([sys.executable, "-c", "raise SystemExit(1)"], **kwargs),
    )
    response = client.post("/api/v1/cameras/demo/recording", json={})
    assert response.status_code == 200
    recorder = application.state.cameras.get("demo").recorder
    assert recorder.done.wait(5)
    assert recorder.error is not None
    assert client.get("/api/v1/media").json()["total"] == 0
    assert application.state.db.one("SELECT id FROM events WHERE kind='recording.error'")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_motion_trigger_extends_postroll_and_finishes(client, application):
    signin(client)
    payload = client.get("/api/v1/cameras/demo/config").json()
    payload.update(
        motion_enabled=True,
        motion_threshold=0.005,
        motion_frames=1,
        motion_pixel_delta=5,
        motion_postroll=1,
    )
    assert client.post("/api/v1/cameras", json=payload).status_code == 201
    camera = application.state.cameras.get("demo")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not camera.recorder:
        time.sleep(0.05)
    assert camera.recorder and camera.recorder.trigger == "motion"
    recorder = camera.recorder
    first = recorder.deadline
    time.sleep(0.7)
    assert recorder.deadline > first
    camera.config.motion_enabled = False
    assert recorder.done.wait(5)
    item = application.state.db.one("SELECT * FROM media WHERE kind='video'")
    assert item and item["trigger"] == "motion"


def test_stalled_capture_never_returns_a_stale_success(client, application, monkeypatch):
    signin(client)
    wait_online(client)
    camera = application.state.cameras.get("demo")
    backend = camera.backend
    original = backend.read
    entered, release = threading.Event(), threading.Event()

    def blocked_read():
        entered.set()
        release.wait(5)
        return original()

    monkeypatch.setattr(backend, "read", blocked_read)
    try:
        assert entered.wait(2)
        with camera.condition:
            camera.frame_times.clear()
            camera.frame_times.append(time.monotonic() - 10)
        deadline = time.monotonic() + 2
        while camera.status != "stalled" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert camera.status == "stalled"
        assert client.get("/api/v1/cameras/demo/preview.jpg").status_code == 409
        assert client.post("/api/v1/cameras/demo/snapshot").status_code == 409
        assert client.get("/api/v1/media").json()["total"] == 0
    finally:
        release.set()
    wait_online(client)
    assert camera.error is None
