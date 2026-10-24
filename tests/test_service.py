import io
import json
import os
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from starlette.websockets import WebSocketDisconnect

from campix.app import create_app
from campix.config import Settings
from campix.models import Schedule
from campix.scheduler import in_window

from .conftest import signin


def wait_online(client):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get("/api/v1/cameras")
        if response.status_code == 200 and response.json() and response.json()[0]["fps"] > 0:
            return response.json()[0]
        time.sleep(0.05)
    raise AssertionError("Synthetic camera did not deliver frames")


def wait_packet_ready(client):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get("/api/v1/cameras")
        if (
            response.status_code == 200
            and response.json()
            and response.json()[0]["encoding"]["packet_ready"]
        ):
            return response.json()[0]
        time.sleep(0.05)
    raise AssertionError("Camera did not produce a configured H.264 keyframe")


def test_health_identifies_the_responding_service_process(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["process_id"] == os.getpid()
    assert response.json()["status"] == "ok"


def test_auth_csrf_roles_and_session_revocation(client):
    assert client.get("/api/v1/cameras").status_code == 401
    signin(client, "viewer")
    assert (
        "HttpOnly" in client.cookies.jar._cookies["testserver.local"]["/"]["campix_session"]._rest
    )
    assert client.post("/api/v1/cameras/demo/snapshot").status_code == 403
    assert client.get("/api/v1/users").status_code == 403
    signin(client, "operator")
    client.headers.pop("X-CSRF-Token")
    assert client.post("/api/v1/cameras/demo/snapshot").status_code == 403
    signin(client, "admin")
    assert (
        client.post(
            "/api/v1/cameras/demo/snapshot", headers={"Origin": "https://attacker.invalid"}
        ).status_code
        == 403
    )
    assert client.post("/api/v1/auth/logout").status_code == 200
    assert client.get("/api/v1/cameras").status_code == 401


def test_snapshot_media_lock_delete_and_path_boundary(client):
    signin(client)
    wait_online(client)
    response = client.post("/api/v1/cameras/demo/snapshot")
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["size"] > 100
    image_response = client.get(f"/api/v1/media/{item['id']}/file")
    image = Image.open(io.BytesIO(image_response.content))
    assert image.size == (640, 360)
    assert image.format == "JPEG"
    assert client.patch(f"/api/v1/media/{item['id']}", json={"locked": True}).status_code == 200
    listed = client.get("/api/v1/media").json()["items"][0]
    assert listed["locked"] is True
    assert client.delete(f"/api/v1/media/{item['id']}").status_code == 409
    assert client.get("/api/v1/media/../../campix.sqlite3/file").status_code == 404
    client.patch(f"/api/v1/media/{item['id']}", json={"locked": False})
    assert client.delete(f"/api/v1/media/{item['id']}").status_code == 200
    assert client.get("/api/v1/media").json()["total"] == 0


@pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="FFmpeg integration requires the runtime dependency"
)
def test_recording_produces_playable_h264_and_rejects_overlap(client, application):
    signin(client)
    wait_online(client)
    wait_packet_ready(client)
    assert client.post("/api/v1/cameras/demo/recording", json={}).status_code == 200
    assert client.post("/api/v1/cameras/demo/recording", json={}).status_code == 409
    time.sleep(0.6)
    response = client.delete("/api/v1/cameras/demo/recording")
    assert response.status_code == 200, response.text
    item = response.json()
    assert item["duration"] > 0
    path = application.state.store.path(item["filename"])
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    stream = json.loads(result.stdout)["streams"][0]
    assert stream["codec_name"] == "h264"
    assert stream["width"] == 640
    assert not list(path.parent.glob("*.partial"))


def test_controls_are_applied_and_persisted(client, application):
    signin(client)
    wait_online(client)
    result = client.patch("/api/v1/cameras/demo/controls", json={"brightness": 0.2})
    assert result.status_code == 200, result.text
    assert result.json()["controls"]["brightness"] == 0.2
    assert (
        json.loads(
            application.state.db.one("SELECT config FROM cameras WHERE id='demo'")["config"]
        )["controls"]["brightness"]
        == 0.2
    )
    assert (
        client.patch("/api/v1/cameras/demo/controls", json={"autofocus": True}).status_code == 409
    )
    assert (
        client.patch("/api/v1/cameras/demo/controls", json={"brightness": 100}).status_code == 422
    )


def test_scheduler_claims_once_and_survives_restart(client, application):
    signin(client)
    wait_online(client)
    schedule = {"id": "hourly", "name": "Hourly still", "camera_id": "demo", "interval_seconds": 5}
    assert client.post("/api/v1/schedules", json=schedule).status_code == 201
    application.state.db.execute(
        "UPDATE schedules SET next_run=? WHERE id='hourly'", (time.time() - 1,)
    )
    application.state.scheduler.tick()
    application.state.scheduler.tick()
    assert client.get("/api/v1/media").json()["total"] == 1
    assert client.get("/api/v1/schedules").json()[0]["next_run"] > time.time()


def _quiet_scheduler(application):
    application.state.scheduler.halt.set()
    application.state.scheduler.thread.join(timeout=2)


def test_schedule_edits_keep_next_run_and_weekdays(client, application):
    signin(client)
    _quiet_scheduler(application)
    created = client.post(
        "/api/v1/schedules",
        json={
            "id": "week",
            "name": "Week",
            "camera_id": "demo",
            "days": [0, 2, 4],
            "interval_seconds": 3600,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    original = body["next_run"]
    assert body["days"] == [0, 2, 4]
    paused = client.post(
        "/api/v1/schedules",
        json={key: value for key, value in body.items() if key != "next_run"}
        | {"enabled": False, "name": "Paused"},
    )
    assert paused.status_code == 201, paused.text
    assert paused.json()["next_run"] == original
    changed = client.post(
        "/api/v1/schedules",
        json={key: value for key, value in paused.json().items() if key != "next_run"}
        | {"interval_seconds": 120},
    )
    assert changed.status_code == 201, changed.text
    assert changed.json()["next_run"] > time.time() + 60
    assert changed.json()["next_run"] != original
    application.state.db.execute("UPDATE schedules SET next_run=? WHERE id='week'", (time.time() - 30,))
    stored = client.get("/api/v1/schedules").json()[0]
    resumed = client.post(
        "/api/v1/schedules",
        json={key: value for key, value in stored.items() if key != "next_run"} | {"enabled": True},
    )
    assert resumed.status_code == 201, resumed.text
    assert resumed.json()["enabled"] is True
    assert resumed.json()["next_run"] <= time.time() + 1
    rejected = client.post(
        "/api/v1/schedules",
        json={
            "id": "long",
            "name": "Long",
            "camera_id": "demo",
            "action": "record",
            "interval_seconds": 30,
            "duration_seconds": 30,
        },
    )
    assert rejected.status_code == 422
    assert "Recording duration must be shorter than the schedule interval" in rejected.text


def test_overlapping_recording_is_skipped_until_the_take_can_finish(client, application):
    from campix.capture import CaptureError

    signin(client)
    _quiet_scheduler(application)
    saved = client.post(
        "/api/v1/schedules",
        json={
            "id": "rec",
            "name": "Rec",
            "camera_id": "demo",
            "action": "record",
            "interval_seconds": 60,
            "duration_seconds": 30,
        },
    )
    assert saved.status_code == 201, saved.text
    camera = application.state.cameras.get("demo")

    def busy(*_args, **_kwargs):
        raise CaptureError("A recording is already in progress")

    camera.start_recording = busy
    application.state.db.execute(
        "UPDATE schedules SET next_run=? WHERE id='rec'", (time.time() - 1,)
    )
    application.state.scheduler.tick()
    application.state.scheduler.tick()
    application.state.db.execute(
        "INSERT INTO schedules VALUES (?,?,?)",
        (
            "legacy",
            json.dumps(
                {
                    "id": "legacy",
                    "name": "Legacy",
                    "camera_id": "demo",
                    "action": "record",
                    "interval_seconds": 5,
                    "duration_seconds": 30,
                    "enabled": True,
                    "days": [0, 1, 2, 3, 4, 5, 6],
                    "start_hour": 0,
                    "end_hour": 24,
                    "timezone": "Europe/Berlin",
                }
            ),
            time.time() - 1,
        ),
    )
    before = time.time()
    application.state.scheduler.tick()
    application.state.scheduler.tick()
    kinds = [
        row["kind"]
        for row in application.state.db.all(
            "SELECT kind FROM events WHERE kind LIKE 'schedule.%' ORDER BY id"
        )
    ]
    assert kinds == ["schedule.skipped", "schedule.skipped"]
    legacy = application.state.db.one("SELECT next_run FROM schedules WHERE id='legacy'")["next_run"]
    assert legacy >= before + 29


def test_overnight_window_uses_previous_weekday():
    import datetime as dt
    from zoneinfo import ZoneInfo

    schedule = Schedule(
        id="night", name="Night", camera_id="demo", days=[4], start_hour=22, end_hour=6
    )
    saturday_2 = dt.datetime(2026, 10, 10, 2, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()
    saturday_22 = dt.datetime(2026, 10, 10, 22, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()
    assert in_window(schedule, saturday_2)
    assert not in_window(schedule, saturday_22)


def test_last_admin_and_disabled_users(client):
    result = signin(client)
    user_id = result["user"]["id"]
    assert client.patch(f"/api/v1/users/{user_id}", json={"disabled": True}).status_code == 409
    assert client.patch(f"/api/v1/users/{user_id}", json={"role": "viewer"}).status_code == 409
    viewer = next(
        user for user in client.get("/api/v1/users").json() if user["username"] == "viewer"
    )
    assert client.patch(f"/api/v1/users/{viewer['id']}", json={"disabled": True}).status_code == 200
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"username": "viewer", "password": "test-only-viewer-password"},
        ).status_code
        == 401
    )


def test_export_contains_no_credentials(client):
    signin(client)
    response = client.post(
        "/api/v1/cameras",
        json={
            "id": "network",
            "name": "Unreachable network",
            "backend": "network",
            "source": "rtsp://secret-user:secret-pass@127.0.0.1:65530/example",
            "enabled": False,
        },
    )
    assert response.status_code == 201, response.text
    exported = client.get("/api/v1/export").text
    assert "secret-user" not in exported and "secret-pass" not in exported
    assert "password" not in exported and "csrf" not in exported


def test_no_fake_camera_in_production(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, demo=False))
    app.state.auth.create_user("admin", "test-only-admin-password")
    with TestClient(app) as client:
        signin(client)
        assert client.get("/api/v1/cameras").json() == []
        assert (
            client.post(
                "/api/v1/cameras", json={"id": "fake", "name": "fake", "backend": "synthetic"}
            ).status_code
            == 409
        )


def test_input_bounds_security_headers_and_legacy(client):
    signin(client)
    assert (
        client.post(
            "/api/v1/cameras", json={"id": "../bad", "name": "Bad", "backend": "synthetic"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/cameras",
            json={
                "id": "file",
                "name": "File",
                "backend": "network",
                "source": "file:///etc/shadow",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/cameras",
            json={"id": "gst", "name": "Shell", "backend": "gstreamer", "source": "shell"},
        ).status_code
        == 422
    )
    assert client.get("/healthz").headers["X-Content-Type-Options"] == "nosniff"
    assert client.post("/api/v1/auth/login", content=b"x" * 70000).status_code == 413
    assert client.get("/cmd_pipe.php?cmd=im").status_code != 200
    assert client.post("/cmd_pipe.php?cmd=sy%20reboot").status_code == 400
    assert client.post("/cmd_pipe.php?cmd=im").status_code == 200


def test_websocket_auth_and_events(client, application):
    signin(client)
    with client.websocket_connect(
        "/api/v1/events/ws", headers={"Origin": "http://testserver"}
    ) as ws:
        application.state.bus.publish("test.event", "Round trip")
        event = ws.receive_json()
        assert event["kind"] == "test.event"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/v1/events/ws", headers={"Origin": "https://evil.invalid"}
        ):
            pass


def test_login_rate_limit(client):
    for _ in range(10):
        assert (
            client.post(
                "/api/v1/auth/login", json={"username": "unknown", "password": "incorrect"}
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "unknown", "password": "incorrect"}
        ).status_code
        == 429
    )
