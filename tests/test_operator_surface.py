import io
import json
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from campix.capture import AvfoundationBackend
from campix.importers import ConfigImportError, parse_raspimjpeg
from campix.models import CameraConfig, Schedule
from campix.motion import activity_cells
from campix.overlay import stamp_jpeg
from campix.provision import _avfoundation_sections, _directshow_audio_devices
from campix.scheduler import in_window

from .conftest import signin


def test_schedule_window_honors_minutes_and_rejects_an_empty_span():
    schedule = Schedule(
        id="minutes",
        name="Minutes",
        camera_id="demo",
        days=[0],
        start_hour=9,
        start_minute=30,
        end_hour=10,
        end_minute=15,
        timezone="Europe/Berlin",
    )
    zone = ZoneInfo("Europe/Berlin")
    inside = datetime(2026, 10, 12, 9, 30, tzinfo=zone).timestamp()
    before = datetime(2026, 10, 12, 9, 29, tzinfo=zone).timestamp()
    after = datetime(2026, 10, 12, 10, 15, tzinfo=zone).timestamp()
    assert in_window(schedule, inside)
    assert not in_window(schedule, before)
    assert not in_window(schedule, after)
    empty = schedule.model_copy(update={"start_hour": 9, "start_minute": 0, "end_hour": 9, "end_minute": 0})
    assert not in_window(empty, inside)


def test_motion_activity_marks_only_changed_cells():
    import numpy as np

    changed = np.zeros((90, 160), dtype=bool)
    changed[:, :20] = True
    active = np.ones_like(changed)
    cells = activity_cells(changed, active)
    assert len(cells) == 144
    assert cells[0] == "1"
    assert cells[-1] == "0"


def test_timestamp_overlay_returns_a_different_jpeg():
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), "black").save(buffer, format="JPEG")
    stamped = stamp_jpeg(buffer.getvalue(), datetime(2026, 10, 27, 1, 2, 3, tzinfo=ZoneInfo("UTC")))
    assert stamped.startswith(b"\xff\xd8")
    assert stamped != buffer.getvalue()


def test_avfoundation_input_uses_the_index_and_no_audio_device():
    config = CameraConfig(id="mac", name="Mac", backend="avfoundation", source="2", width=640, height=480)
    backend = AvfoundationBackend(config, "ffmpeg")
    backend.source_size = (640, 480)
    arguments = backend.input_arguments()
    assert arguments[:2] == ["-f", "avfoundation"]
    assert arguments[-1] == "2:none"
    assert backend.native_modes() is None


def test_audio_listings_keep_only_named_devices():
    listing = """
[AVFoundation indev @ 0x1] AVFoundation video devices:
[AVFoundation indev @ 0x1] [0] FaceTime HD Camera
[AVFoundation indev @ 0x1] [1] Capture screen 0
[AVFoundation indev @ 0x1] AVFoundation audio devices:
[AVFoundation indev @ 0x1] [0] MacBook Microphone
"""
    sections = _avfoundation_sections(listing)
    assert sections["video"] == [("0", "FaceTime HD Camera")]
    assert sections["audio"] == [("0", "MacBook Microphone")]
    audio = """
[in#0] DirectShow audio devices
[in#0] "Microphone" (audio)
[in#0] DirectShow video devices
[in#0] "Integrated Camera" (video)
"""
    assert _directshow_audio_devices(audio)[0][0] == "Microphone"


def test_raspimjpeg_maps_size_and_rejects_commands():
    proposal = parse_raspimjpeg("image_width 1920\nimage_height 1080\nfps_preview 25\nmotion_detection true\n")
    assert proposal == {"width": 1920, "height": 1080, "fps": 25, "motion_enabled": True}
    with pytest.raises(ConfigImportError, match="commands"):
        parse_raspimjpeg("width 640\ncmd ls\n")


def test_password_change_revokes_the_session_and_rejects_the_old_password(client):
    signin(client)
    rejected = client.post(
        "/api/v1/auth/password",
        json={"current_password": "wrong-password-value", "new_password": "test-only-replacement-password"},
    )
    assert rejected.status_code == 403
    assert client.get("/api/v1/auth/me").status_code == 200
    changed = client.post(
        "/api/v1/auth/password",
        json={
            "current_password": "test-only-admin-password",
            "new_password": "test-only-replacement-password",
        },
    )
    assert changed.status_code == 200, changed.text
    assert client.get("/api/v1/auth/me").status_code == 401
    failed = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "test-only-admin-password"},
    )
    assert failed.status_code == 401
    restored = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "test-only-replacement-password"},
    )
    assert restored.status_code == 200


def test_viewer_allow_list_hides_cameras_media_and_events(client):
    signin(client)
    assert client.patch("/api/v1/cameras/demo", json={"viewer_users": ["admin"]}).status_code == 200
    assert client.post("/api/v1/auth/logout").status_code == 200
    signin(client, "viewer")
    assert client.get("/api/v1/cameras").json() == []
    assert client.get("/api/v1/cameras/demo/preview.jpg").status_code == 404
    assert client.get("/api/v1/media").json()["total"] == 0


def test_library_filters_bulk_actions_and_share_links(client):
    signin(client)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        cameras = client.get("/api/v1/cameras").json()
        if cameras and cameras[0]["fps"] > 0:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("Synthetic camera did not deliver frames")
    item = client.post("/api/v1/cameras/demo/snapshot").json()
    early = item["created"] - 10
    listed = client.get("/api/v1/media", params={"camera_id": "demo", "created_after": int(early)})
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert client.get("/api/v1/media", params={"created_before": int(early)}).json()["total"] == 0
    locked = client.post("/api/v1/media/actions", json={"ids": [item["id"]], "action": "lock"})
    assert locked.json()["results"] == [{"id": item["id"], "ok": True}]
    deleted = client.post("/api/v1/media/actions", json={"ids": [item["id"]], "action": "delete"})
    assert deleted.json()["results"][0]["ok"] is False
    client.post("/api/v1/media/actions", json={"ids": [item["id"]], "action": "unlock"})
    shared = client.post(f"/api/v1/media/{item['id']}/share", json={"hours": 1})
    assert shared.status_code == 200, shared.text
    token = shared.json()["token"]
    assert token not in client.get("/api/v1/shares").text
    saved_cookies = client.cookies
    client.cookies = saved_cookies.__class__()
    opened = client.get(shared.json()["path"])
    assert opened.status_code == 200
    assert opened.content.startswith(b"\xff\xd8")
    client.cookies = saved_cookies
    assert client.delete(f"/api/v1/shares/{shared.json()['id']}").status_code == 200
    client.cookies = saved_cookies.__class__()
    assert client.get(f"/s/{token}").status_code == 404


def test_notification_kinds_drop_unselected_events(client, application):
    signin(client)
    saved = client.patch(
        "/api/v1/notifications",
        json={
            "webhook": {"enabled": False, "url": ""},
            "mail": {"enabled": True, "recipient": "operator@example.test", "port": 2525},
            "kinds": ["storage.reserve"],
            "cameras": ["demo"],
        },
    )
    assert saved.status_code == 200, saved.text
    service = application.state.notifications
    service.observe(
        {"id": 1, "kind": "motion.detected", "camera_id": "demo", "message": "Motion detected", "created": time.time()}
    )
    service.observe(
        {
            "id": 2,
            "kind": "storage.reserve",
            "camera_id": None,
            "message": "Storage reserve reached; free space before recording",
            "created": time.time(),
        }
    )
    rows = application.state.db.all("SELECT kind FROM notification_outbox")
    assert [row["kind"] for row in rows] == ["storage.reserve"]


def test_configuration_import_keeps_redacted_sources_and_passwords(client, application):
    signin(client)
    added = client.post(
        "/api/v1/cameras",
        json={
            "id": "net",
            "name": "Network",
            "backend": "network",
            "source": "rtsp://camera.example/live",
            "enabled": False,
            "onvif_ptz": True,
            "onvif_username": "nvr",
            "onvif_password": "test-only-ptz-password",
        },
    )
    assert added.status_code == 201, added.text
    exported = client.get("/api/v1/export").json()
    assert "test-only-ptz-password" not in json.dumps(exported)
    network = next(item for item in exported["cameras"] if item["id"] == "net")
    assert network["source"] == "[redacted]"
    assert "onvif_password" not in network
    assert network["onvif_credentials_set"] is True
    network["name"] = "Renamed network"
    imported = client.post("/api/v1/import", json=exported)
    assert imported.status_code == 200, imported.text
    stored = json.loads(
        application.state.db.one("SELECT config FROM cameras WHERE id='net'")["config"]
    )
    assert stored["name"] == "Renamed network"
    assert stored["source"] == "rtsp://camera.example/live"
    assert stored["onvif_password"] == "test-only-ptz-password"


def test_backup_omits_sessions(client, tmp_path):
    signin(client)
    response = client.get("/api/v1/backup")
    assert response.status_code == 200, response.text
    assert "test-only-admin-password" not in response.text
    path = tmp_path / "backup.sqlite3"
    path.write_bytes(response.content)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 1
    finally:
        connection.close()


def test_onvif_device_service_requires_a_password_and_returns_profiles(client):
    signin(client)
    missing = client.post(
        "/onvif/device_service",
        content=b"<s:Envelope><s:Body><GetSystemDateAndTime/></s:Body></s:Envelope>",
        headers={"Content-Type": "application/soap+xml"},
    )
    assert missing.status_code == 404
    enabled = client.put(
        "/api/v1/integrations/onvif",
        json={"enabled": True, "username": "nvr", "password": "test-only-onvif-password"},
    )
    assert enabled.status_code == 200, enabled.text
    assert "password_hash" not in enabled.text
    clock = client.post(
        "/onvif/device_service",
        content=_soap("GetSystemDateAndTime"),
        headers={"Content-Type": "application/soap+xml"},
    )
    assert clock.status_code == 200
    assert b"GetSystemDateAndTimeResponse" in clock.content
    denied = client.post(
        "/onvif/device_service",
        content=_soap("GetProfiles"),
        headers={"Content-Type": "application/soap+xml"},
    )
    assert denied.status_code == 401
    profiles = client.post(
        "/onvif/device_service",
        content=_soap("GetProfiles", "nvr", "test-only-onvif-password"),
        headers={"Content-Type": "application/soap+xml"},
    )
    assert profiles.status_code == 200, profiles.text
    assert b'token="demo"' in profiles.content
    stream = client.post(
        "/onvif/device_service",
        content=_soap("GetStreamUri", "nvr", "test-only-onvif-password", "<trt:ProfileToken>demo</trt:ProfileToken>"),
        headers={"Content-Type": "application/soap+xml"},
    )
    assert stream.status_code == 200, stream.text
    assert b"rtsp://127.0.0.1:8554/demo" in stream.content
    digest = (
        b'<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
        b' xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        b"<s:Header><wsse:Security><wsse:UsernameToken><wsse:Username>nvr</wsse:Username>"
        b'<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
        b"x</wsse:Password></wsse:UsernameToken></wsse:Security></s:Header>"
        b"<s:Body><GetDeviceInformation/></s:Body></s:Envelope>"
    )
    rejected = client.post(
        "/onvif/device_service", content=digest, headers={"Content-Type": "application/soap+xml"}
    )
    assert rejected.status_code == 401


def test_ptz_uses_only_the_camera_host(client):
    signin(client)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            self.server.bodies.append(body)
            if b"GetProfiles" in body:
                payload = b'<trt:Profiles token="Profile_1"></trt:Profiles>'
            else:
                payload = b"<tptz:ContinuousMoveResponse/>"
            data = b"<?xml version='1.0'?><s:Envelope><s:Body>" + payload + b"</s:Body></s:Envelope>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.bodies = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        created = client.post(
            "/api/v1/cameras",
            json={
                "id": "ptz",
                "name": "PTZ",
                "backend": "network",
                "source": "rtsp://127.0.0.1/live",
                "enabled": False,
                "onvif_ptz": True,
                "onvif_port": port,
                "onvif_username": "nvr",
                "onvif_password": "test-only-ptz-password",
            },
        )
        assert created.status_code == 201, created.text
        moved = client.post("/api/v1/cameras/ptz/ptz", json={"action": "move", "pan": 0.2, "tilt": 0, "zoom": 0})
        assert moved.status_code == 200, moved.text
        assert any(b"ContinuousMove" in body for body in server.bodies)
        assert all(b"test-only-ptz-password" not in body for body in server.bodies)
        blocked = client.post(
            "/api/v1/cameras",
            json={
                "id": "meta",
                "name": "Meta",
                "backend": "network",
                "source": "http://169.254.169.254/latest",
                "enabled": False,
                "onvif_ptz": True,
                "onvif_username": "nvr",
                "onvif_password": "test-only-ptz-password",
            },
        )
        assert blocked.status_code == 201, blocked.text
        refused = client.post("/api/v1/cameras/meta/ptz", json={"action": "stop"})
        assert refused.status_code == 409
        assert "169.254.169.254" not in refused.text
    finally:
        server.shutdown()
        server.server_close()


def test_partial_recording_is_indexed_only_after_a_playable_remux(client, application, tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are required")
    signin(client)
    source = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x120:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        timeout=20,
    )
    media_id = "a" * 32
    partial = application.state.store.root / f"20261027_000000_{media_id}.mp4.partial"
    partial.write_bytes(source.read_bytes())
    broken = application.state.store.root / f"20261027_000001_{'b' * 32}.mp4.partial"
    broken.write_bytes(b"not a movie")
    started = client.post("/api/v1/media/recovery")
    assert started.status_code == 202
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = client.get("/api/v1/media/recovery").json()
        if status["state"] == "idle" and status["results"]:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(status)
    assert status["results"]
    assert any(item["ok"] and item.get("media_id") == media_id for item in status["results"]), status
    assert not any(item["ok"] and "b" * 32 in item.get("media_id", "") for item in status["results"])
    assert client.get(f"/api/v1/media/{media_id}/file").status_code == 200
    assert broken.is_file()


def _soap(action: str, username: str = "", password: str = "", inner: str = "") -> bytes:
    header = ""
    if username:
        header = (
            "<s:Header><wsse:Security "
            'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
            f"<wsse:UsernameToken><wsse:Username>{username}</wsse:Username>"
            '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-username-token-profile-1.0#PasswordText">'
            f"{password}</wsse:Password></wsse:UsernameToken></wsse:Security></s:Header>"
        )
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:trt="http://www.onvif.org/ver10/media/wsdl">'
        f"{header}<s:Body><trt:{action}>{inner}</trt:{action}></s:Body></s:Envelope>"
    ).encode()
