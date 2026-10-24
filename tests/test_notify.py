import json
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from campix.db import SCHEMA_VERSION, Database
from campix.notify import (
    DeliveryError,
    catalog_message,
    deliver_mail,
    deliver_webhook,
    notification_body,
    signature_valid,
    validate_webhook_url,
)
from campix.provision import _admin_state

from .conftest import signin


class _Webhook(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(
            (self.path, {key.lower(): value for key, value in self.headers.items()}, body)
        )
        if self.server.redirect:
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest")
            self.end_headers()
            return
        self.send_response(204)
        self.end_headers()

    def log_message(self, format, *args):
        return


@pytest.fixture
def webhook():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Webhook)
    server.requests = []
    server.redirect = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _configure(client, url, mail=None):
    payload = {
        "webhook": {"enabled": True, "url": url},
        "mail": mail or {"enabled": False, "recipient": "", "port": 25},
    }
    response = client.patch("/api/v1/notifications", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _wait_requests(server, count=1):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if len(server.requests) >= count:
            return server.requests
        time.sleep(0.05)
    raise AssertionError(server.requests)


def test_schema_2_gains_the_notification_outbox_and_keeps_rows(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,"
            " password TEXT NOT NULL, role TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute("INSERT INTO users(username,password,role) VALUES ('kept','hash','admin')")
        conn.execute("PRAGMA user_version=2")
    database = Database(path)
    try:
        assert database.one("SELECT username FROM users")["username"] == "kept"
        assert database.one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='notification_outbox'"
        )
        assert database.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        database.close()
    assert _admin_state(path, "kept") == (True, True)


def test_catalog_rejects_unlisted_events_and_signs_the_fixed_body(client, application, webhook):
    signin(client)
    port = webhook.server_address[1]
    state = _configure(client, f"http://127.0.0.1:{port}/hooks/campix")
    secret = state["secret"]
    assert secret
    application.state.notifications.cooldown = 0
    application.state.bus.publish("auth.login", "User signed in", data={"token": "hidden"})
    application.state.bus.publish(
        "export.failed",
        "Export failed (RuntimeError); inputs were not modified",
        data={"path": "/secret/video.mp4"},
    )
    path, headers, body = _wait_requests(webhook)[0]
    assert path == "/hooks/campix"
    payload = json.loads(body)
    assert payload["kind"] == "export.failed"
    assert payload["message"] == "Export failed (RuntimeError); inputs were not modified"
    assert "path" not in payload and "token" not in body.decode()
    assert signature_valid(
        secret,
        headers["campix-timestamp"],
        body,
        headers["campix-signature"],
        time.time(),
    )
    exported = client.get("/api/v1/export").text
    assert secret not in exported and "/hooks/campix" not in exported
    rows = application.state.db.all("SELECT kind FROM notification_outbox")
    assert [row["kind"] for row in rows] == ["export.failed"]


def test_webhook_redirects_and_forbidden_targets_are_not_followed(client, application, webhook):
    signin(client)
    port = webhook.server_address[1]
    webhook.redirect = True
    _configure(client, f"http://127.0.0.1:{port}/hooks/campix")
    application.state.notifications.cooldown = 0
    application.state.bus.publish(
        "camera.stalled", "Frame delivery stalled; waiting for the camera driver"
    )
    _wait_requests(webhook)
    time.sleep(0.4)
    assert [item[0] for item in webhook.requests] == ["/hooks/campix"]
    deliveries = client.get("/api/v1/notifications").json()["deliveries"]
    assert deliveries[0]["status"] in {"queued", "failed"}
    assert "169.254.169.254" not in json.dumps(deliveries)
    for url in (
        "http://169.254.169.254/latest",
        "http://10.0.0.8/hook",
        "file:///etc/passwd",
        "https://user:secret-pass@127.0.0.1/hook",
        "http://metadata.google.internal/hook",
    ):
        response = client.patch(
            "/api/v1/notifications",
            json={
                "webhook": {"enabled": True, "url": url},
                "mail": {"enabled": False, "recipient": "", "port": 25},
            },
        )
        assert response.status_code == 422, url
        assert "secret-pass" not in response.text
        assert "169.254.169.254" not in response.text
    assert client.get("/api/v1/notifications", headers={"X-CSRF-Token": ""}).status_code == 200


def test_viewer_cannot_read_notification_settings(client):
    signin(client, "viewer")
    assert client.get("/api/v1/notifications").status_code == 403


def test_local_mail_uses_loopback_and_rejects_header_injection(client, application):
    received = []

    def serve(sock):
        connection, _ = sock.accept()
        with connection:
            connection.sendall(b"220 localhost\r\n")
            mode = "command"
            data = b""
            pending = b""
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                pending += chunk
                if mode == "command":
                    while b"\r\n" in pending and mode == "command":
                        line, pending = pending.split(b"\r\n", 1)
                        data += line + b"\r\n"
                        if line.upper().startswith(b"DATA"):
                            connection.sendall(b"354 go\r\n")
                            mode = "body"
                        else:
                            connection.sendall(b"250 ok\r\n")
                if mode == "body":
                    data += pending
                    pending = b""
                    if b"\r\n.\r\n" in data:
                        connection.sendall(b"250 ok\r\n")
                        received.append(data)
                        return

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        thread = threading.Thread(target=serve, args=(sock,), daemon=True)
        thread.start()
        signin(client)
        response = client.patch(
            "/api/v1/notifications",
            json={
                "webhook": {"enabled": False, "url": ""},
                "mail": {"enabled": True, "recipient": "operator@localhost", "port": port},
            },
        )
        assert response.status_code == 200, response.text
        application.state.notifications.cooldown = 0
        application.state.bus.publish(
            "storage.reserve", "Storage reserve reached; free space before recording"
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not received:
            time.sleep(0.05)
    assert received
    message = received[0].decode()
    assert "operator@localhost" in message
    assert "Storage reserve reached" in message
    assert "Bcc" not in message
    rejected = client.patch(
        "/api/v1/notifications",
        json={
            "webhook": {"enabled": False, "url": ""},
            "mail": {
                "enabled": True,
                "recipient": "operator@localhost\nBcc: attacker@example",
                "port": port,
            },
        },
    )
    assert rejected.status_code == 422
    assert "attacker@example" not in rejected.text
    with pytest.raises(DeliveryError):
        deliver_mail("operator@localhost", 1, "should not leave the host")


def test_storage_reserve_is_notified_once(client, application, monkeypatch, webhook):
    signin(client)
    port = webhook.server_address[1]
    _configure(client, f"http://127.0.0.1:{port}/reserve")
    monkeypatch.setattr(
        "campix.media.shutil.disk_usage",
        lambda path: SimpleNamespace(total=0, used=0, free=0),
    )
    with pytest.raises(Exception, match="Storage reserve"):
        application.state.store.check_space()
    with pytest.raises(Exception, match="Storage reserve"):
        application.state.store.check_space()
    _wait_requests(webhook)
    time.sleep(0.2)
    assert len(webhook.requests) == 1
    kinds = [
        row["kind"]
        for row in application.state.db.all("SELECT kind FROM events WHERE kind='storage.reserve'")
    ]
    assert kinds == ["storage.reserve"]


def test_webhook_tries_a_later_address_and_stops_after_rejection(monkeypatch):
    from campix import notify

    received = []

    def serve(sock, status):
        connection, _address = sock.accept()
        with connection:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                data += chunk
            received.append(data)
            connection.sendall(
                f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
            )

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        thread = threading.Thread(target=serve, args=(sock, "204 No Content"), daemon=True)
        thread.start()
        monkeypatch.setattr(
            notify, "webhook_targets", lambda url, timeout=5: ["127.0.0.2", "127.0.0.1"]
        )
        deliver_webhook(f"http://127.0.0.1:{port}/hook", b"{}", "secret", timeout=2)
        thread.join(2)
    assert received and b"POST /hook" in received[0]

    received.clear()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        thread = threading.Thread(target=serve, args=(sock, "500 No"), daemon=True)
        thread.start()
        monkeypatch.setattr(
            notify, "webhook_targets", lambda url, timeout=5: ["127.0.0.1", "127.0.0.2"]
        )
        with pytest.raises(DeliveryError, match="rejected"):
            deliver_webhook(f"http://127.0.0.1:{port}/hook", b"{}", "secret", timeout=2)
        thread.join(2)
    assert len(received) == 1


def test_signature_and_catalog_helpers_reject_stale_or_foreign_text():
    body = notification_body(7, 10.0, "motion.detected", "demo", "Motion detected")
    assert (
        catalog_message("motion.detected", "Motion detected\nrtsp://secret@camera")
        == "Motion detected"
    )
    assert catalog_message("export.failed", "Export failed at /tmp/secret.mp4") == (
        "Export failed; inputs were not modified"
    )
    with pytest.raises(ValueError, match="Webhook URL is not allowed"):
        validate_webhook_url("http://192.168.1.20/hook")
    assert signature_valid("secret", "10", body, "v1=nope", 10) is False
    assert signature_valid("secret", "10", body, "v1=nope", 10_000) is False
