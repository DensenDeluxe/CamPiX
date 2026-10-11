import io
import json
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
