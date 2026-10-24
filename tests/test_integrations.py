import json
import threading
import time

from campix.app import create_app
from campix.config import Settings

from .conftest import signin


def _online(client):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get("/api/v1/cameras")
        if response.status_code == 200 and response.json() and response.json()[0]["fps"] > 0:
            return
        time.sleep(0.05)
    raise AssertionError("Synthetic camera did not deliver frames")


def _without_session(client):
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)


def test_pairing_requires_the_session_cookie(client):
    signed_in = signin(client)
    client.cookies.clear()
    denied = client.post(
        "/api/v1/integrations/homeassistant/pair",
        headers={"X-CSRF-Token": signed_in["csrf"]},
        json={"name": "Home Assistant"},
    )
    assert denied.status_code == 401


def test_home_assistant_pairs_a_viewer_and_hides_camera_urls(client, application):
    signin(client)
    _online(client)
    secret = "super-secret-pass"
    added = client.post(
        "/api/v1/cameras",
        json={
            "id": "door",
            "name": "Door",
            "backend": "network",
            "source": f"rtsp://user:{secret}@10.1.2.3/stream",
            "enabled": False,
        },
    )
    assert added.status_code == 201, added.text
    assert secret not in added.text
    paired = client.post(
        "/api/v1/integrations/homeassistant/pair", json={"name": "Home Assistant"}
    )
    assert paired.status_code == 200, paired.text
    token = paired.json()["token"]
    token_id = paired.json()["token_id"]
    assert token.startswith("cx_")
    assert token not in client.get("/api/v1/export").text
    assert token not in client.get("/api/v1/settings").text
    stored = json.dumps(application.state.db.all("SELECT message, data FROM events"))
    assert token not in stored
    listed = client.get("/api/v1/integrations/tokens").json()
    assert listed[0]["role"] == "viewer"
    assert token not in json.dumps(listed)
    assert "digest" not in json.dumps(listed)

    header = {"Authorization": f"Bearer {token}"}
    _without_session(client)
    denied = client.get("/api/v1/integrations/homeassistant")
    assert denied.status_code == 401
    catalog = client.get("/api/v1/integrations/homeassistant", headers=header)
    assert catalog.status_code == 200, catalog.text
    assert secret not in catalog.text
    assert "rtsp://" not in catalog.text
    assert "source" not in catalog.text
    body = catalog.json()
    assert {camera["id"] for camera in body["cameras"]} == {"demo", "door"}
    door = next(camera for camera in body["cameras"] if camera["id"] == "door")
    demo = next(camera for camera in body["cameras"] if camera["id"] == "demo")
    assert door["online"] is False
    assert door["unique_id"] == "campix:door"
    assert door["still_path"].endswith("/preview.jpg?view=live")
    assert demo["online"] is True
    image = client.get(demo["still_path"], headers=header)
    assert image.status_code == 200, image.text
    assert image.content.startswith(b"\xff\xd8")
    assert client.post("/api/v1/cameras/demo/snapshot", headers=header).status_code == 403
    assert client.post("/api/v1/auth/logout", headers=header).status_code == 200
    assert client.get("/api/v1/integrations/homeassistant", headers=header).status_code == 200

    def revoke_during_stream():
        time.sleep(0.3)
        application.state.auth.tokens.revoke(token_id)

    threading.Thread(target=revoke_during_stream, daemon=True).start()
    stream = client.get(demo["mjpeg_path"], headers=header)
    assert stream.status_code == 200
    assert b"\xff\xd8" in stream.content
    assert client.get("/api/v1/integrations/homeassistant", headers=header).status_code == 401

    signin(client, "viewer")
    assert client.post("/api/v1/integrations/homeassistant/pair", json={}).status_code == 403
    signin(client)
    for index in range(8):
        extra = client.post(
            "/api/v1/integrations/homeassistant/pair", json={"name": f"extra {index}"}
        )
        assert extra.status_code == 200, extra.text
        assert extra.json()["token"] not in client.get("/api/v1/export").text
    assert client.post("/api/v1/integrations/homeassistant/pair", json={}).status_code == 409
    assert client.delete(f"/api/v1/integrations/tokens/{token_id}").status_code == 404


def test_discovery_stays_off_for_the_test_app(tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "data", demo=True, min_free_mb=32))
    assert app.state.settings.mdns is False
