import json
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from campix.app import create_app
from campix.config import Settings
from campix.db import Database
from campix.process_lock import ProcessLock
from campix.processes import owned_process
from campix.recording import Recorder
from campix.recovery import _media_id

from .conftest import signin
from .test_service import wait_online, wait_packet_ready


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
    exported = application.state.db.all(
        "SELECT kind, camera_id FROM events WHERE kind LIKE 'export.%' ORDER BY id"
    )
    assert exported
    assert {row["camera_id"] for row in exported} == {"demo"}
    media_id = job["media_id"]
    assert client.patch("/api/v1/cameras/demo", json={"viewer_users": ["admin"]}).status_code == 200
    assert client.post("/api/v1/auth/logout").status_code == 200
    signin(client, "viewer")
    visible = {event["kind"] for event in client.get("/api/v1/events").json()}
    assert "export.queued" not in visible and "export.completed" not in visible
    assert client.get(f"/api/v1/jobs/{job_id}").status_code == 404
    assert client.get(f"/api/v1/media/{media_id}/file").status_code == 404


def test_timelapse_rejects_snapshots_from_more_than_one_camera(client, application):
    signin(client)
    if "libx264" not in application.state.cameras.encoders["encoders"]:
        pytest.skip("libx264 required")
    now = time.time()
    ids = ("a" * 32, "b" * 32)
    for media_id, camera_id in zip(ids, ("demo", "secret"), strict=True):
        application.state.db.execute(
            "INSERT INTO media(id,camera_id,kind,filename,created,size,trigger) VALUES (?,?,?,?,?,?,?)",
            (media_id, camera_id, "snapshot", f"{media_id}.jpg", now, 10, "manual"),
        )
    response = client.post("/api/v1/timelapse", json={"media_ids": list(ids)})
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Timelapse snapshots must belong to one camera"
    assert application.state.db.one("SELECT id FROM export_jobs") is None


def test_media_search_covers_every_page_and_keeps_wildcards_literal(client, application):
    assert client.get("/api/v1/media", params={"q": "needle"}).status_code == 401
    signin(client, "viewer")
    now = time.time()
    rows = [
        (f"{index:032x}", "demo", "snapshot", f"20261018_{index:06d}.jpg", now - index, 10, "manual")
        for index in range(30)
    ]
    rows.append(("a" * 32, "demo", "snapshot", "special_100%_name.jpg", now - 100, 10, "manual"))
    rows.append(("b" * 32, "demo", "snapshot", "special_100Xname.jpg", now - 101, 10, "manual"))
    rows.append(("c" * 32, "other", "video", "special_100%_name.mp4", now - 102, 10, "manual"))
    for row in rows:
        application.state.db.execute(
            "INSERT INTO media(id,camera_id,kind,filename,created,size,trigger) VALUES (?,?,?,?,?,?,?)",
            row,
        )

    first_page = client.get("/api/v1/media", params={"limit": 24, "offset": 0}).json()
    assert first_page["total"] == 33
    assert "20261018_000029.jpg" not in {item["filename"] for item in first_page["items"]}
    found = client.get("/api/v1/media", params={"q": "  000029  ", "limit": 24}).json()
    assert found["total"] == 1 and found["items"][0]["filename"] == "20261018_000029.jpg"
    literal = client.get("/api/v1/media", params={"q": "100%_n", "kind": "snapshot"}).json()
    assert [item["filename"] for item in literal["items"]] == ["special_100%_name.jpg"]
    camera = client.get("/api/v1/media", params={"q": "100%", "camera_id": "other"}).json()
    assert [item["filename"] for item in camera["items"]] == ["special_100%_name.mp4"]
    assert client.get("/api/v1/media", params={"q": "%"}).json()["total"] == 2
    assert client.get("/api/v1/media", params={"q": "' OR 1=1 --"}).json()["total"] == 0
    assert client.get("/api/v1/media", params={"q": "   "}).json()["total"] == 33

    private = "test-only-private-search\nmarker"
    rejected = client.get("/api/v1/media", params={"q": private})
    assert rejected.status_code == 422
    assert private not in rejected.text and "marker" not in rejected.text
    assert rejected.json()["detail"][0]["loc"] == ["query", "q"]
    assert client.get("/api/v1/media", params={"q": "a" * 81}).status_code == 422


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


def test_retention_revokes_share_links(client, application):
    signin(client)
    wait_online(client)
    item = client.post("/api/v1/cameras/demo/snapshot").json()
    shared = client.post(f"/api/v1/media/{item['id']}/share", json={"hours": 1})
    assert shared.status_code == 200, shared.text
    assert client.get(shared.json()["path"]).status_code == 200
    application.state.db.execute("UPDATE media SET created=0 WHERE id=?", (item["id"],))
    application.state.store.retention()
    assert client.get(shared.json()["path"]).status_code == 404
    row = application.state.db.one(
        "SELECT revoked FROM share_links WHERE id=?", (shared.json()["id"],)
    )
    assert row["revoked"]


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


def _wait_recovery(client):
    deadline = time.monotonic() + 20
    status = {"state": "running"}
    while time.monotonic() < deadline:
        status = client.get("/api/v1/media/recovery").json()
        if status["state"] == "idle":
            return status
        time.sleep(0.05)
    raise AssertionError(status)


def test_recovery_indexes_finalized_files_missing_from_the_library(client, application):
    signin(client)
    root = application.state.store.root
    jpeg_id = "ab" * 16
    assert _media_id(f"20261103_010203_{jpeg_id}.mp4.partial") == jpeg_id
    assert _media_id(f"20261103_010203_{jpeg_id}.jpg") == jpeg_id
    assert _media_id("interrupted.mp4.partial") is None
    jpeg_name = f"20261103_010203_{jpeg_id}.jpg"
    (root / jpeg_name).write_bytes(b"\xff\xd8\xff\xd9")
    bad_id = "cd" * 16
    bad_name = f"20261103_010204_{bad_id}.mp4"
    (root / bad_name).write_bytes(b"not a movie")
    loose = root / "notes.mp4"
    loose.write_bytes(b"notes")
    link_name = f"20261103_010205_{'ef' * 16}.jpg"
    (root / link_name).symlink_to(root / jpeg_name)
    video_id = None
    video_name = ""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        video_id = "12" * 16
        video_name = f"20261103_010206_{video_id}.mp4"
        encoded = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=16x16:d=0.2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(root / video_name),
            ],
            capture_output=True,
            timeout=20,
        )
        assert encoded.returncode == 0, encoded.stderr
    started = client.post("/api/v1/media/recovery")
    assert started.status_code == 202, started.text
    status = _wait_recovery(client)
    library = {item["id"]: item for item in client.get("/api/v1/media").json()["items"]}
    assert library[jpeg_id]["trigger"] == "recovery"
    assert library[jpeg_id]["kind"] == "snapshot"
    assert library[jpeg_id]["camera_id"] == "recovered"
    assert bad_id not in library
    assert (root / bad_name).is_file()
    assert loose.is_file()
    names = {item["filename"] for item in library.values()}
    assert link_name not in names
    results = {item["file"]: item for item in status["results"]}
    assert results[jpeg_name]["ok"] is True
    assert results[bad_name]["ok"] is False
    if video_id:
        assert library[video_id]["kind"] == "video"
        assert library[video_id]["duration"] > 0
        assert results[video_name]["ok"] is True


def test_recovered_file_keeps_its_source_camera_and_hides_an_unknown_source(client, application):
    signin(client)
    root = application.state.store.root
    owned_id = "ab" * 16
    owned_name = f"20261103_020304_{owned_id}.jpg"
    owned = root / owned_name
    owned.write_bytes(b"\xff\xd8\xff\xd9")
    application.state.store.remember_camera(owned, "demo")
    loose_id = "cd" * 16
    loose_name = f"20261103_020305_{loose_id}.jpg"
    (root / loose_name).write_bytes(b"\xff\xd8\xff\xd9")
    assert client.post("/api/v1/media/recovery").status_code == 202
    _wait_recovery(client)
    library = {item["id"]: item for item in client.get("/api/v1/media").json()["items"]}
    assert library[owned_id]["camera_id"] == "demo"
    assert library[loose_id]["camera_id"] == "recovered"
    assert not (root / f"{owned_name}.camera").exists()
    signin(client, "viewer")
    visible = {item["id"] for item in client.get("/api/v1/media").json()["items"]}
    assert owned_id in visible
    assert loose_id not in visible
    assert client.get(f"/api/v1/media/{loose_id}/file").status_code == 404
    signin(client, "admin")
    assert client.patch("/api/v1/cameras/demo", json={"viewer_users": ["admin"]}).status_code == 200
    signin(client, "viewer")
    hidden = {item["id"] for item in client.get("/api/v1/media").json()["items"]}
    assert owned_id not in hidden
    assert client.get(f"/api/v1/media/{owned_id}/file").status_code == 404


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
    wait_packet_ready(client)
    camera = application.state.cameras.get("demo")
    time.sleep(0.6)
    before = camera.encoded_hub.stats()["buffered_duration_seconds"]
    assert before >= 0.2
    response = client.post("/api/v1/cameras/demo/recording", json={"duration": 1})
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not application.state.db.one(
        "SELECT id FROM media WHERE kind='video'"
    ):
        time.sleep(0.05)
    item = client.get("/api/v1/media?kind=video").json()["items"][0]
    assert item["duration"] >= before + 0.7
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(application.state.store.path(item["filename"])),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert float(json.loads(result.stdout)["format"]["duration"]) == pytest.approx(
        item["duration"], abs=0.2
    )


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_encoder_failure_never_indexes_success(client, application, monkeypatch):
    signin(client)
    wait_online(client)
    wait_packet_ready(client)
    monkeypatch.setattr(
        "campix.recording.owned_process",
        lambda *args, **kwargs: owned_process(
            [sys.executable, "-c", "raise SystemExit(1)"], **kwargs
        ),
    )
    response = client.post("/api/v1/cameras/demo/recording", json={})
    assert response.status_code == 200
    recorder = application.state.cameras.get("demo").recorder
    assert recorder.done.wait(5)
    assert recorder.error is not None
    assert client.get("/api/v1/media").json()["total"] == 0
    assert application.state.db.one("SELECT id FROM events WHERE kind='recording.error'")


def test_legacy_recorder_does_not_index_an_exit_zero_file_without_a_container(tmp_path, monkeypatch):
    class _Stdin:
        closed = False

        def write(self, data):
            return len(data)

        def close(self):
            self.closed = True

    class _Process:
        def __init__(self, path):
            self.path = Path(path)
            self.stdin = _Stdin()
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.path.write_bytes(b"not-a-valid-mp4")
            self.returncode = 0
            return 0

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = 0

    monkeypatch.setattr(
        "campix.recording.subprocess.Popen",
        lambda command, **kwargs: _Process(command[-1]),
    )
    events, saved = [], []
    store = SimpleNamespace(
        reserve=lambda extension, camera_id=None: ("legacyfile", tmp_path / "legacy.mp4"),
        bus=SimpleNamespace(publish=lambda *args: events.append(args)),
        register=lambda *args: saved.append(args),
        check_space=lambda: None,
    )
    recorder = Recorder(
        SimpleNamespace(id="legacy", fps=10, preroll_seconds=0, encoder="libx264", width=16, height=16),
        SimpleNamespace(ffmpeg="ffmpeg"),
        store,
        [],
    )
    recorder.offer(b"\xff\xd8\xff\xd9")
    with pytest.raises(RuntimeError, match="Recording failed"):
        recorder.stop()
    assert not saved
    assert events and events[0][0] == "recording.error"
    assert (tmp_path / "legacy.mp4").exists() is False


def test_encoder_failure_without_incoming_frames_is_observed(tmp_path, monkeypatch):
    spawn = subprocess.Popen
    monkeypatch.setattr(
        "campix.recording.subprocess.Popen",
        lambda *args, **kwargs: spawn([sys.executable, "-c", "raise SystemExit(1)"], **kwargs),
    )
    events, saved = [], []
    store = SimpleNamespace(
        reserve=lambda extension, camera_id=None: ("interrupted", tmp_path / "interrupted.mp4"),
        bus=SimpleNamespace(publish=lambda *args: events.append(args)),
        register=lambda *args: saved.append(args),
    )
    recorder = Recorder(
        SimpleNamespace(id="idle", fps=10, preroll_seconds=0, encoder="auto"),
        SimpleNamespace(ffmpeg="ffmpeg"),
        store,
        [],
    )
    try:
        assert recorder.done.wait(3), "Encoder exit was ignored while the frame queue was empty"
        assert recorder.error is not None
        assert recorder.proc.poll() is not None
        assert not saved
        assert events and events[0][0] == "recording.error"
    finally:
        recorder.stop_requested.set()
        assert recorder.done.wait(3)


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
