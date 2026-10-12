import asyncio
import gzip
import io
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from starlette.requests import Request

from campix.app import create_app
from campix.capture import CaptureError
from campix.config import Settings
from campix.mediamtx import MediaPlane, Publisher, managed_config

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


def test_publisher_shutdown_clears_online_state(monkeypatch):
    publisher = Publisher.__new__(Publisher)
    publisher.halt = threading.Event()
    publisher.online = False
    publisher.error = None
    publisher.settings = Settings()

    def frame(sequence, timeout):
        publisher.halt.set()
        return 1, b"jpeg"

    publisher.camera = SimpleNamespace(
        config=SimpleNamespace(id="demo", encoder="auto", fps=10), frame=frame
    )
    monkeypatch.setattr(
        "campix.mediamtx.subprocess.Popen",
        lambda *args, **kwargs: SimpleNamespace(stdin=io.BytesIO()),
    )
    monkeypatch.setattr("campix.mediamtx.terminate_process", lambda process: None)
    publisher._run()
    assert publisher.proc.stdin.closed
    assert publisher.online is False


def test_publisher_flushes_each_new_frame_and_skips_timeout_duplicates(monkeypatch):
    publisher = Publisher.__new__(Publisher)
    publisher.halt = threading.Event()
    publisher.settings = Settings()
    publisher.online, publisher.error = False, None
    events = []

    class Pipe(io.BytesIO):
        def write(self, data):
            events.append(("write", data))
            return super().write(data)

        def flush(self):
            events.append(("flush", None))
            return super().flush()

    frames = iter(((1, b"first"), (1, b"first"), (3, b"latest")))

    def frame(sequence, timeout):
        try:
            return next(frames)
        except StopIteration:
            publisher.halt.set()
            return 4, b"stopped"

    publisher.camera = SimpleNamespace(
        config=SimpleNamespace(id="demo", encoder="auto", fps=10), frame=frame
    )
    pipe = Pipe()
    monkeypatch.setattr(
        "campix.mediamtx.subprocess.Popen",
        lambda *args, **kwargs: SimpleNamespace(stdin=pipe),
    )
    monkeypatch.setattr("campix.mediamtx.terminate_process", lambda process: None)
    publisher._run()
    assert events == [("write", b"first"), ("flush", None), ("write", b"latest"), ("flush", None)]
    assert pipe.closed and publisher.online is False


def test_publisher_recovers_a_failed_flush_with_a_new_process(monkeypatch):
    publisher = Publisher.__new__(Publisher)
    publisher.halt = threading.Event()
    publisher.settings = Settings()
    publisher.online, publisher.error = False, None
    processes, backoffs, written = [], [], []

    class Pipe(io.BytesIO):
        def flush(self):
            if len(processes) == 1:
                raise BrokenPipeError("private-upstream-token")
            written.append(self.getvalue())
            return super().flush()

    def launch(*args, **kwargs):
        process = SimpleNamespace(stdin=Pipe())
        processes.append(process)
        return process

    def frame(sequence, timeout):
        if written:
            publisher.halt.set()
        return sequence + 1, b"latest"

    def wait(timeout):
        backoffs.append(timeout)
        return publisher.halt.is_set()

    publisher.camera = SimpleNamespace(
        config=SimpleNamespace(id="demo", encoder="auto", fps=10), frame=frame
    )
    monkeypatch.setattr(publisher.halt, "wait", wait)
    monkeypatch.setattr("campix.mediamtx.subprocess.Popen", launch)
    monkeypatch.setattr("campix.mediamtx.terminate_process", lambda process: None)
    publisher._run()
    assert len(processes) == 2 and all(process.stdin.closed for process in processes)
    assert written == [b"latest"] and backoffs == [3, 3]
    assert publisher.error is None and publisher.online is False


def test_publisher_shutdown_unblocks_a_stalled_encoder_pipe(monkeypatch):
    original_popen = subprocess.Popen
    writing = threading.Event()

    def stalled_encoder(command, **kwargs):
        process = original_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        stdin = process.stdin

        class Pipe:
            def write(self, data):
                writing.set()
                return stdin.write(data)

            def flush(self):
                return stdin.flush()

            @property
            def closed(self):
                return stdin.closed

            def close(self):
                stdin.close()

        process.stdin = Pipe()
        return process

    camera = SimpleNamespace(
        config=SimpleNamespace(id="stall-test", encoder="auto", fps=10),
        frame=lambda sequence, timeout: (sequence + 1, b"jpeg" * 256 * 1024),
    )
    monkeypatch.setattr("campix.mediamtx.subprocess.Popen", stalled_encoder)
    publisher = Publisher(camera, Settings())
    try:
        assert writing.wait(5), "The publisher did not attempt to send a frame"
    finally:
        publisher.stop()
    assert publisher.proc.poll() is not None
    assert not publisher.thread.is_alive()
    assert publisher.proc.stdin.closed and publisher.online is False


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_live_publisher_starts_promptly_and_preserves_elapsed_time_with_slow_frames(monkeypatch):
    publisher = Publisher.__new__(Publisher)
    publisher.halt = threading.Event()
    publisher.settings = Settings()
    publisher.online, publisher.error = False, None
    jpeg = io.BytesIO()
    Image.effect_noise((256, 144), 32).convert("RGB").save(jpeg, "JPEG", quality=85)
    sample = jpeg.getvalue()
    assert len(sample) > 4096  # Avoid tiny-frame buffering affecting the timing measurement.
    received, readers = [], []
    original_popen = subprocess.Popen
    started = time.monotonic()

    def local_output(command, **kwargs):
        # Exercise the actual publisher encoder/timestamps without a server or fixed ports.
        output = max(index for index, value in enumerate(command) if value == "-f")
        command = [*command[:output], "-f", "framecrc", "pipe:1"]
        process = original_popen(command, **{**kwargs, "stdout": subprocess.PIPE})

        def read_packets():
            with process.stdout:
                for line in process.stdout:
                    if not line.startswith(b"#"):
                        received.append(
                            (
                                time.monotonic() - started,
                                int(line.split(b",")[2]),
                                b"F=0x0" not in line,
                            )
                        )

        reader = threading.Thread(target=read_packets, daemon=True)
        readers.append(reader)
        reader.start()
        return process

    delivered = []

    def frame(sequence, timeout):
        if len(delivered) == 12:
            publisher.halt.set()
            return sequence, sample
        publisher.halt.wait(0.2)  # Actual input rate is half the configured encoder rate.
        delivered.append(time.monotonic())
        return len(delivered), sample

    publisher.camera = SimpleNamespace(
        config=SimpleNamespace(id="latency-test", encoder="auto", fps=10), frame=frame
    )
    monkeypatch.setattr("campix.mediamtx.subprocess.Popen", local_output)
    try:
        publisher._run()
    finally:
        publisher.halt.set()
        for reader in readers:
            reader.join(timeout=5)
    assert publisher.proc.poll() is not None
    assert readers and all(not reader.is_alive() for reader in readers)
    assert received, "The publisher buffered every live frame until shutdown"
    assert received[0][0] < 1.5, received
    elapsed = delivered[-1] - delivered[0]
    timestamp_span = (received[-1][1] - received[0][1]) / publisher.camera.config.fps
    assert timestamp_span >= elapsed - 0.6, (elapsed, timestamp_span)
    keyframes = [packet[1] / publisher.camera.config.fps for packet in received if packet[2]]
    assert len(keyframes) >= 2, keyframes
    assert keyframes[1] - keyframes[0] <= 1.2, keyframes


def proxy_request(method="GET", body=b""):
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/api/v1/streams/demo/webrtc/whep",
            "query_string": b"",
            "headers": [],
        },
        receive=lambda: asyncio.sleep(
            0, result={"type": "http.request", "body": body, "more_body": False}
        ),
    )


def proxy_plane(api):
    return MediaPlane(
        Settings(mediamtx_api=api),
        SimpleNamespace(get=lambda camera_id: None),
        SimpleNamespace(publish=lambda *args: None),
    )


def mock_proxy(monkeypatch, handler):
    client = httpx.AsyncClient
    clients = []

    def create_client(**kwargs):
        instance = client(**kwargs, transport=httpx.MockTransport(handler))
        clients.append(instance)
        return instance

    monkeypatch.setattr(
        "campix.mediamtx.httpx.AsyncClient",
        create_client,
    )
    return clients


class ProxyByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks, self.delay = chunks, delay
        self.closed = False
        self.started = asyncio.Event()
        self.delivered = 0

    async def __aiter__(self):
        self.started.set()
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.delivered += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("protocol,limit", [("hls", 8 * 1024 * 1024), ("webrtc", 65536)])
def test_media_proxy_stops_oversized_responses_and_closes_streams(monkeypatch, protocol, limit):
    chunk = b"private-test-token" + b"x" * (65536 - len(b"private-test-token"))
    stream = ProxyByteStream([chunk] * (limit // len(chunk)) + [b"x", b"unread"])
    clients = mock_proxy(monkeypatch, lambda request: httpx.Response(200, stream=stream))
    plane = proxy_plane("http://127.0.0.1:9997")
    plane.hls_token = "private-test-token"
    path = "segment.m4s" if protocol == "hls" else "whep"
    with pytest.raises(CaptureError, match="MediaMTX response exceeds its size limit") as caught:
        asyncio.run(plane.proxy("demo", protocol, path, proxy_request()))
    assert "private-test-token" not in str(caught.value)
    assert stream.delivered == limit // len(chunk) + 1
    assert stream.closed and all(client.is_closed for client in clients)


@pytest.mark.parametrize("protocol,limit", [("hls", 8 * 1024 * 1024), ("webrtc", 65536)])
def test_media_proxy_accepts_responses_at_the_limit(monkeypatch, protocol, limit):
    chunk = b"x" * 65536
    stream = ProxyByteStream([chunk] * (limit // len(chunk)))
    clients = mock_proxy(
        monkeypatch,
        lambda request: httpx.Response(
            206,
            stream=stream,
            headers={
                "content-type": "video/mp4",
                "content-range": f"bytes 0-{limit - 1}/{limit}",
                "etag": '"test-media"',
                "x-upstream-private": "private-test-token",
            },
        ),
    )
    plane = proxy_plane("http://127.0.0.1:9997")
    plane.hls_token = "private-test-token"
    path = "segment.m4s" if protocol == "hls" else "whep"
    status, body, headers = asyncio.run(plane.proxy("demo", protocol, path, proxy_request()))
    assert status == 206 and body == chunk * (limit // len(chunk))
    assert headers == {
        "content-type": "video/mp4",
        "content-range": f"bytes 0-{limit - 1}/{limit}",
        "etag": '"test-media"',
    }
    assert stream.closed and all(client.is_closed for client in clients)


def test_media_proxy_bounds_total_exchange_time_despite_response_progress(monkeypatch):
    monkeypatch.setattr("campix.mediamtx.MEDIA_PROXY_TIMEOUT_SECONDS", 0.05, raising=False)
    stream = ProxyByteStream([b"private-test-token"] * 10, delay=0.02)
    clients = mock_proxy(monkeypatch, lambda request: httpx.Response(200, stream=stream))
    with pytest.raises(httpx.ReadTimeout, match="MediaMTX exchange exceeded its time limit") as caught:
        asyncio.run(
            proxy_plane("http://127.0.0.1:9997").proxy("demo", "webrtc", "whep", proxy_request())
        )
    assert "private-test-token" not in str(caught.value)
    assert stream.delivered < 10
    assert stream.closed and all(client.is_closed for client in clients)


def test_media_proxy_cancellation_closes_upstream_stream_and_client(monkeypatch):
    stream = ProxyByteStream([b"sdp"], delay=10)
    clients = mock_proxy(monkeypatch, lambda request: httpx.Response(200, stream=stream))

    async def cancel_proxy():
        task = asyncio.create_task(
            proxy_plane("http://127.0.0.1:9997").proxy("demo", "webrtc", "whep", proxy_request())
        )
        await asyncio.wait_for(stream.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_proxy())
    assert stream.delivered == 0
    assert stream.closed and all(client.is_closed for client in clients)


def test_media_proxy_does_not_read_or_forward_get_request_bodies(monkeypatch):
    def upstream(request):
        assert request.content == b""
        return httpx.Response(200, content=b"sdp")

    mock_proxy(monkeypatch, upstream)
    request = proxy_request()

    async def unexpected_body():
        raise AssertionError("GET body must not be buffered")

    request.body = unexpected_body
    status, body, _ = asyncio.run(
        proxy_plane("http://127.0.0.1:9997").proxy("demo", "webrtc", "whep", request)
    )
    assert status == 200 and body == b"sdp"


def test_media_proxy_rejects_compressed_responses_before_decoding(monkeypatch):
    compressed = gzip.compress(b"private-test-token" * 65536)
    stream = ProxyByteStream([compressed])

    def upstream(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, stream=stream, headers={"content-encoding": "gzip"})

    clients = mock_proxy(monkeypatch, upstream)
    with pytest.raises(CaptureError, match="unsupported content encoding") as caught:
        asyncio.run(
            proxy_plane("http://127.0.0.1:9997").proxy("demo", "webrtc", "whep", proxy_request())
        )
    assert "private-test-token" not in str(caught.value)
    assert stream.delivered == 0
    assert stream.closed and all(client.is_closed for client in clients)


def test_media_proxy_preserves_whep_signaling_body_and_allowed_headers(monkeypatch):
    offer, answer = b"v=0\r\no=offer\r\n", b"v=0\r\no=answer\r\n"

    def upstream(request):
        assert request.method == "POST" and request.content == offer
        assert request.headers["content-type"] == "application/sdp"
        assert request.headers["authorization"] == "Bearer private-upstream-token"
        assert "cookie" not in request.headers
        return httpx.Response(
            201,
            content=answer,
            headers={
                "content-type": "application/sdp",
                "location": "/demo/whep/abc-123",
                "set-cookie": "private-upstream-token",
            },
        )

    mock_proxy(monkeypatch, upstream)
    plane = proxy_plane("http://127.0.0.1:9997")
    plane.settings.mediamtx_token = "private-upstream-token"
    request = proxy_request(method="POST", body=offer)
    request.scope["headers"] = [
        (b"content-type", b"application/sdp"),
        (b"cookie", b"private-browser-session"),
    ]
    status, body, headers = asyncio.run(plane.proxy("demo", "webrtc", "whep", request))
    assert status == 201 and body == answer
    assert headers == {
        "content-type": "application/sdp",
        "location": "/api/v1/streams/demo/webrtc/whep/abc-123",
    }


@pytest.mark.parametrize("failure,status", [("oversize", 409), ("deadline", 503)])
def test_media_proxy_api_errors_do_not_expose_upstream_tokens(
    client, application, monkeypatch, failure, status
):
    signin(client)
    plane = application.state.media_plane
    plane.api = "http://127.0.0.1:9997"
    plane.settings.mediamtx_token = "private-upstream-token"
    if failure == "deadline":
        monkeypatch.setattr("campix.mediamtx.MEDIA_PROXY_TIMEOUT_SECONDS", 0.02)
        stream = ProxyByteStream([b"private-upstream-token"], delay=1)
    else:
        stream = ProxyByteStream([b"private-upstream-token" * 65536])
    clients = mock_proxy(monkeypatch, lambda request: httpx.Response(200, stream=stream))
    response = client.get("/api/v1/streams/demo/webrtc/whep")
    assert response.status_code == status
    assert "private-upstream-token" not in response.text
    assert stream.closed and all(upstream.is_closed for upstream in clients)


def test_media_proxy_preserves_ipv6_host_brackets(monkeypatch):
    seen = []

    def upstream(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=b"sdp")

    mock_proxy(monkeypatch, upstream)
    status, body, _ = asyncio.run(
        proxy_plane("http://[::1]:9997").proxy("demo", "webrtc", "whep", proxy_request())
    )
    assert status == 200 and body == b"sdp"
    assert seen == ["http://[::1]:8889/demo/whep"]


@pytest.mark.parametrize(
    "location",
    [
        "/demo/whepublish",
        "/demo/whep/../../other/hls/index.m3u8",
        "/demo/whep/%2e%2e/session",
        "/other/whep/abc",
        "http://[private-test-token]/demo/whep/abc",
    ],
)
def test_media_proxy_rejects_unexpected_whep_session_locations(monkeypatch, location):
    mock_proxy(monkeypatch, lambda request: httpx.Response(201, headers={"location": location}))
    with pytest.raises(CaptureError, match="Unexpected media session location") as caught:
        asyncio.run(
            proxy_plane("http://127.0.0.1:9997").proxy(
                "demo", "webrtc", "whep", proxy_request()
            )
        )
    assert "private-test-token" not in str(caught.value)


def test_media_proxy_rewrites_valid_whep_session_location(monkeypatch):
    mock_proxy(
        monkeypatch,
        lambda request: httpx.Response(201, headers={"location": "/demo/whep/abc-123"}),
    )
    status, _, headers = asyncio.run(
        proxy_plane("http://127.0.0.1:9997").proxy("demo", "webrtc", "whep", proxy_request())
    )
    assert status == 201
    assert headers["location"] == "/api/v1/streams/demo/webrtc/whep/abc-123"


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
