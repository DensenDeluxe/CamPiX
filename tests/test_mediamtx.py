import os
import socket
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from campix.app import create_app
from campix.config import Settings
from campix.mediamtx import MediaPlane, managed_config

from .conftest import signin


def test_managed_media_plane_is_loopback_and_read_facade_is_restricted(client):
    cfg = managed_config()
    assert all(
        value.startswith("127.0.0.1:")
        for key, value in cfg.items()
        if key.endswith("Address") and value
    )
    assert cfg["rtmp"] is False and cfg["srt"] is False and cfg["moq"] is False
    assert cfg["authInternalUsers"][0]["ips"] == ["127.0.0.1", "::1"]
    signin(client)
    assert client.get("/api/v1/streams/demo/hls/index.m3u8").status_code == 409


def test_managed_media_plane_rejects_foreign_port_ownership(tmp_path, monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    original = socket.create_connection
    monkeypatch.setattr(
        "campix.mediamtx.socket.create_connection",
        lambda address, **kwargs: original(listener.getsockname(), **kwargs),
    )
    try:
        plane = MediaPlane(
            Settings(data_dir=tmp_path), None, SimpleNamespace(publish=lambda *args, **kwargs: None)
        )
        with pytest.raises(RuntimeError, match="already occupied"):
            plane._launch()
        assert plane.proc is None
    finally:
        listener.close()


@pytest.mark.skipif(
    not os.environ.get("CAMPIX_TEST_MEDIAMTX_BINARY"),
    reason="Pinned MediaMTX integration binary not configured",
)
def test_managed_mediamtx_publishes_and_hls_proxy_requires_session(tmp_path):
    app = create_app(
        Settings(
            data_dir=tmp_path,
            demo=True,
            min_free_mb=32,
            mediamtx_binary=os.environ["CAMPIX_TEST_MEDIAMTX_BINARY"],
        )
    )
    app.state.auth.create_user("admin", "test-only-admin-password")
    with TestClient(app) as client:
        assert client.get("/api/v1/streams/demo/hls/index.m3u8").status_code == 401
        signin(client)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = client.get("/api/v1/system").json()["media_plane"]
            if status["publishers"].get("demo", {}).get("online"):
                break
            time.sleep(0.2)
        assert status["publishers"].get("demo", {}).get("online"), status
        response = client.get("/api/v1/streams/demo/hls/index.m3u8")
        assert response.status_code == 200, response.text
        assert "#EXTM3U" in response.text
        assert client.post("/api/v1/streams/demo/hls/index.m3u8").status_code == 409
        assert client.post("/api/v1/streams/demo/webrtc/whip").status_code == 409
        # Supervision must recover a terminated child, not just claim it is healthy.
        previous = app.state.media_plane.proc.pid
        app.state.media_plane.proc.terminate()
        app.state.media_plane.proc.wait(timeout=5)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            proc = app.state.media_plane.proc
            if proc.pid != previous and proc.poll() is None:
                break
            time.sleep(0.2)
        assert proc.pid != previous and proc.poll() is None
    assert app.state.media_plane.proc.poll() is not None
