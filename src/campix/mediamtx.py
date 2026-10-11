from __future__ import annotations

import json
import re
import secrets
import socket
import subprocess
import threading
from urllib.parse import urlsplit

import httpx

from .capture import CaptureError, terminate_process

MEDIAMTX_VERSION = "1.21.2"


def managed_config():
    return {
        "logLevel": "warn",
        "api": True,
        "apiAddress": "127.0.0.1:9997",
        "metrics": True,
        "metricsAddress": "127.0.0.1:9998",
        "pprof": False,
        "playback": False,
        "rtsp": True,
        "rtspAddress": "127.0.0.1:8554",
        "rtspTransports": ["tcp"],
        "rtmp": False,
        "srt": False,
        "moq": False,
        "hls": True,
        "hlsAddress": "127.0.0.1:8888",
        "hlsAllowOrigins": [],
        "webrtc": True,
        "webrtcAddress": "127.0.0.1:8889",
        "webrtcAllowOrigins": [],
        "webrtcLocalUDPAddress": "",
        "webrtcLocalTCPAddress": "127.0.0.1:8189",
        "authMethod": "internal",
        "authInternalUsers": [
            {
                "user": "any",
                "ips": ["127.0.0.1", "::1"],
                "permissions": [
                    {"action": action} for action in ("publish", "read", "api", "metrics")
                ],
            }
        ],
        "paths": {"all_others": {"source": "publisher"}},
    }


class Publisher:
    def __init__(self, camera, settings):
        self.camera, self.settings = camera, settings
        self.halt = threading.Event()
        self.proc = None
        self.error = None
        self.online = False
        self.thread = threading.Thread(
            target=self._run, name=f"publish-{camera.config.id}", daemon=True
        )
        self.thread.start()

    def _run(self):
        while not self.halt.is_set():
            try:
                cfg = self.camera.config
                codec = "libx264" if cfg.encoder == "auto" else cfg.encoder
                command = [
                    self.settings.ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "image2pipe",
                    "-framerate",
                    str(cfg.fps),
                    "-c:v",
                    "mjpeg",
                    "-i",
                    "pipe:0",
                    "-an",
                    "-c:v",
                    codec,
                ]
                if codec == "libx264":
                    command += [
                        "-preset",
                        "veryfast",
                        "-tune",
                        "zerolatency",
                        "-profile:v",
                        "baseline",
                    ]
                command += [
                    "-pix_fmt",
                    "yuv420p",
                    "-g",
                    str(cfg.fps),
                    "-f",
                    "rtsp",
                    "-rtsp_transport",
                    "tcp",
                    self.settings.mediamtx_rtsp.rstrip("/") + "/" + cfg.id,
                ]
                self.proc = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                sequence = -1
                while not self.halt.is_set():
                    sequence, jpeg = self.camera.frame(sequence, timeout=3)
                    self.proc.stdin.write(jpeg)
                    self.online, self.error = True, None
            except (CaptureError, OSError):
                self.online, self.error = False, "Media publisher disconnected; retrying"
            finally:
                if self.proc:
                    terminate_process(self.proc)
                    if self.proc.stdin and not self.proc.stdin.closed:
                        try:
                            self.proc.stdin.close()
                        except OSError:
                            pass
            self.halt.wait(3)

    def stop(self):
        self.halt.set()
        if self.proc:
            terminate_process(self.proc)
        self.thread.join(timeout=10)


class MediaPlane:
    def __init__(self, settings, cameras, bus):
        self.settings, self.cameras, self.bus = settings, cameras, bus
        self.proc = None
        self.publishers = {}
        self.halt = threading.Event()
        self.thread = threading.Thread(
            target=self._supervise, name="mediamtx-supervisor", daemon=True
        )
        self.api = settings.mediamtx_api or (
            "http://127.0.0.1:9997" if settings.mediamtx_binary else None
        )
        self.ready_paths = set()
        self.last_error = None
        self.hls_token = (
            secrets.token_urlsafe(32) if settings.mediamtx_binary else settings.mediamtx_hls_token
        )

    def _launch(self):
        # Do not accidentally publish into another service that owns these ports.
        for port in (8554, 8888, 8889, 9997, 9998, 8189):
            try:
                connection = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            except OSError:
                continue
            connection.close()
            raise RuntimeError(f"Managed MediaMTX port {port} is already occupied")
        cfg = self.settings.data_dir / "mediamtx.generated.json"
        config = managed_config()
        config.update(hlsCDNSecret=self.hls_token, hlsSegmentMaxSize="8M")
        cfg.write_text(json.dumps(config, indent=2), encoding="utf-8")
        cfg.chmod(0o600)
        result = subprocess.run(
            [self.settings.mediamtx_binary, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        if result.stdout.strip() != "v" + MEDIAMTX_VERSION:
            raise RuntimeError(f"Managed MediaMTX requires v{MEDIAMTX_VERSION}")
        self.proc = subprocess.Popen(
            [self.settings.mediamtx_binary, str(cfg)],
            cwd=self.settings.data_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if self.halt.wait(0.3) or self.proc.poll() is not None:
            raise RuntimeError("Managed MediaMTX did not start; check its ports and configuration")

    def start(self):
        if self.api:
            if self.settings.mediamtx_binary:
                self._launch()
            self.thread.start()

    def _supervise(self):
        while not self.halt.wait(2):
            try:
                if self.proc and self.proc.poll() is not None:
                    self.bus.publish("media_plane.restart", "Restarting MediaMTX")
                    self._launch()
                current = {
                    item["id"]: self.cameras.get(item["id"])
                    for item in self.cameras.list()
                    if item["status"] == "online"
                }
                for camera_id, publisher in list(self.publishers.items()):
                    if camera_id not in current or publisher.camera is not current[camera_id]:
                        publisher.stop()
                        del self.publishers[camera_id]
                for camera_id, camera in current.items():
                    codec = "libx264" if camera.config.encoder == "auto" else camera.config.encoder
                    if (
                        camera_id not in self.publishers
                        and codec in self.cameras.encoders["encoders"]
                    ):
                        self.publishers[camera_id] = Publisher(camera, self.settings)
                headers = (
                    {"authorization": "Bearer " + self.settings.mediamtx_token}
                    if self.settings.mediamtx_token
                    else {}
                )
                with httpx.Client(timeout=2, trust_env=False) as client:
                    response = client.get(self.api.rstrip("/") + "/v3/paths/list", headers=headers)
                    response.raise_for_status()
                    ready = {
                        path["name"]
                        for path in response.json().get("items", [])
                        if path.get("ready")
                    }
                    for camera_id in ready - self.ready_paths:
                        self.bus.publish("stream.ready", "Media stream ready", camera_id)
                    self.ready_paths = ready
                self.last_error = None
            except Exception:
                self.ready_paths = set()
                self.last_error = "Media plane is unavailable; MJPEG remains available"
                self.bus.publish(
                    "media_plane.error", "Media plane is unavailable; MJPEG remains available"
                )
                self.halt.wait(5)

    def status(self):
        return {
            "configured": bool(self.api),
            "managed": bool(self.settings.mediamtx_binary),
            "version": MEDIAMTX_VERSION if self.settings.mediamtx_binary else None,
            "error": self.last_error,
            "publishers": {
                key: {"online": pub.online and key in self.ready_paths, "error": pub.error}
                for key, pub in self.publishers.copy().items()
            },
            "network_note": "Managed WebRTC ICE listens on loopback; configure external MediaMTX with reachable ICE/TURN for LAN/WAN.",
        }

    async def proxy(self, camera_id, protocol, path, request):
        self.cameras.get(camera_id)
        if not self.api:
            raise CaptureError("MediaMTX is not configured; use MJPEG")
        if (
            protocol not in {"webrtc", "hls"}
            or ".." in path
            or any(c in path for c in ("\\", "?", "#", "%"))
        ):
            raise CaptureError("Invalid media endpoint")
        if protocol == "hls" and request.method != "GET":
            raise CaptureError("HLS is read-only")
        if protocol == "webrtc" and not re.fullmatch(r"whep(?:/[a-fA-F0-9-]{1,64})?", path):
            raise CaptureError("Only WHEP reads are exposed")
        if protocol == "hls" and not re.fullmatch(r"[a-zA-Z0-9_-]+\.(m3u8|mp4|m4s|ts)", path):
            raise CaptureError("Only HLS playlists and segments are exposed")
        url = urlsplit(self.api)
        origin = f"{url.scheme}://{url.hostname}:{8889 if protocol == 'webrtc' else 8888}"
        upstream = f"{origin}/{camera_id}/{path}"
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() in {"content-type", "if-match", "range"}
        }
        if self.settings.mediamtx_token:
            headers["authorization"] = "Bearer " + self.settings.mediamtx_token
        if protocol == "hls":
            if not self.hls_token:
                raise CaptureError(
                    "HLS proxy requires CAMPIX_MEDIAMTX_HLS_TOKEN matching the server's hlsCDNSecret"
                )
            headers["authorization"] = "Bearer " + self.hls_token
        params = {
            key: value
            for key, value in request.query_params.items()
            if key in {"_HLS_msn", "_HLS_part", "_HLS_skip"} and len(value) <= 32
        }
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            response = await client.request(
                request.method,
                upstream,
                params=params,
                headers=headers,
                content=await request.body(),
            )
        result_headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() in {"content-type", "etag", "accept-ranges", "content-range"}
        }
        if "location" in response.headers and protocol == "webrtc":
            location = urlsplit(response.headers["location"]).path
            suffix = location.removeprefix("/" + camera_id + "/")
            if not suffix.startswith("whep"):
                raise CaptureError("Unexpected media session location")
            result_headers["location"] = f"/api/v1/streams/{camera_id}/webrtc/{suffix}"
        elif 300 <= response.status_code < 400:
            raise CaptureError("Unexpected media redirect; check the MediaMTX configuration")
        return response.status_code, response.content, result_headers

    def stop(self):
        self.halt.set()
        if self.thread.is_alive():
            self.thread.join(timeout=15)
        for publisher in list(self.publishers.values()):
            publisher.stop()
        if self.proc:
            terminate_process(self.proc)
