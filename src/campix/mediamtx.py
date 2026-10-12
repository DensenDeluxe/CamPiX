from __future__ import annotations

import asyncio
import json
import re
import secrets
import socket
import subprocess
import threading
import time
from dataclasses import replace
from urllib.parse import urlsplit

import httpx

from .capture import CaptureError, terminate_process
from .encoded import EncodedOverrun, EncodedRestart, EncodedStreamEnded
from .encoding import EncoderUnavailable, encoder_args, select_encoder
from .processes import NO_WINDOW
from .tsmux import TransportStreamError, TransportStreamMuxer

MEDIAMTX_VERSION = "1.21.2"
HLS_PROXY_MAX_BYTES = 8 * 1024 * 1024  # Matches the managed hlsSegmentMaxSize of 8M.
WHEP_PROXY_MAX_BYTES = 64 * 1024
MEDIA_PROXY_TIMEOUT_SECONDS = 20


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
        self.selection = None
        self.failed_encoders = set()
        self.failed_until = {}
        self.spawn_lock = threading.Lock()
        self.subscription = None
        self.packet_passthrough = getattr(camera, "encoded_hub", None) is not None
        self.packets_sent = 0
        self.last_packet_at = None
        self.thread = threading.Thread(
            target=self._run, name=f"publish-{camera.config.id}", daemon=True
        )
        self.thread.start()

    def _run(self):
        if getattr(self.camera, "encoded_hub", None) is not None:
            return self._run_shared()
        self.failed_encoders = getattr(self, "failed_encoders", set())
        self.failed_until = getattr(self, "failed_until", {})
        self.spawn_lock = getattr(self, "spawn_lock", threading.Lock())
        while not self.halt.is_set():
            try:
                for failed, expires in list(self.failed_until.items()):
                    if time.monotonic() >= expires:
                        self.failed_encoders.discard(failed)
                        del self.failed_until[failed]
                cfg = self.camera.config
                ready = getattr(self.camera, "encoding_ready", None)
                if ready is not None and not ready.is_set():
                    self.halt.wait(0.1)
                    continue
                self.selection = getattr(self.camera, "encoding_selection", None)
                if ready is not None and self.selection is None:
                    raise CaptureError("No usable encoder for the live profile")
                if self.failed_encoders and cfg.encoder == "auto":
                    self.selection = select_encoder(
                        self.settings.ffmpeg,
                        "auto",
                        width=cfg.width,
                        height=cfg.height,
                        fps=cfg.fps,
                        capabilities=self.camera.encoders,
                        excluded=self.failed_encoders,
                        cancel=self.halt,
                    )
                    self.selection = replace(
                        self.selection,
                        fallback_reason="Previous publishing session failed; using "
                        + self.selection.codec,
                    )
                if self.halt.is_set():
                    break
                codec = (
                    self.selection.codec
                    if self.selection
                    else ("libx264" if cfg.encoder == "auto" else cfg.encoder)
                )
                input_args, output_args = (
                    (self.selection.input_args, self.selection.output_args)
                    if self.selection
                    else encoder_args(
                        codec, getattr(cfg, "width", 640), getattr(cfg, "height", 480), cfg.fps
                    )
                )
                command = [
                    self.settings.ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    *input_args,
                    # This pipe has a known codec; probing a live frame backlog delays startup.
                    "-probesize",
                    "32",
                    "-analyzeduration",
                    "0",
                    "-f",
                    "image2pipe",
                    "-framerate",
                    str(cfg.fps),
                    "-c:v",
                    "mjpeg",
                    # Latest-frame consumers may skip frames or run below the target rate.
                    # Frame-count timestamps would fall progressively behind the live clock.
                    "-use_wallclock_as_timestamps",
                    "1",
                    "-i",
                    "pipe:0",
                    "-an",
                    *output_args,
                ]
                command += [
                    "-force_key_frames",
                    "expr:gte(t,n_forced*1)",
                    "-fps_mode",
                    "vfr",
                    "-flush_packets",
                    "1",
                    "-f",
                    "rtsp",
                    "-rtsp_transport",
                    "tcp",
                    self.settings.mediamtx_rtsp.rstrip("/") + "/" + cfg.id,
                ]
                with self.spawn_lock:
                    if self.halt.is_set():
                        break
                    self.proc = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=NO_WINDOW,
                    )
                sequence = -1
                while not self.halt.is_set():
                    current, jpeg = self.camera.frame(sequence, timeout=3)
                    if self.halt.is_set():
                        break
                    if current == sequence:
                        continue
                    sequence = current
                    self.proc.stdin.write(jpeg)
                    self.proc.stdin.flush()
                    self.online, self.error = True, None
            except (CaptureError, EncoderUnavailable, OSError) as exc:
                self.online, self.error = False, "Media publisher disconnected; retrying"
                if (
                    isinstance(exc, OSError)
                    and self.selection
                    and self.selection.hardware
                    and cfg.encoder == "auto"
                    and not self.halt.is_set()
                ):
                    self.failed_encoders.add(self.selection.codec)
                    self.failed_until[self.selection.codec] = time.monotonic() + 30
                    self.error = "Hardware publishing session failed; retrying another encoder"
            finally:
                self.online = False
                if self.proc:
                    terminate_process(self.proc)
                    if self.proc.stdin and not self.proc.stdin.closed:
                        try:
                            self.proc.stdin.close()
                        except OSError:
                            pass
            self.halt.wait(3)

    def _run_shared(self):
        """Copy packets from the sole camera encoder; transport failures cannot reselect it."""
        self.spawn_lock = getattr(self, "spawn_lock", threading.Lock())
        self.packet_passthrough = True
        self.packets_sent = getattr(self, "packets_sent", 0)
        self.last_packet_at = None
        self.subscription = None
        while not self.halt.is_set():
            subscription = None
            process = None
            retry = 3.0
            try:
                source_halt = getattr(self.camera, "halt", None)
                if source_halt is not None and source_halt.is_set():
                    break
                subscription = self.camera.encoded_hub.subscribe(
                    "live", max_bytes=8 * 1024 * 1024, max_frames=120
                )
                self.subscription = subscription
                first = None
                while first is None and not self.halt.is_set():
                    first = subscription.read(timeout=0.2, cancel_event=self.halt)
                    if source_halt is not None and source_halt.is_set():
                        return
                if self.halt.is_set():
                    break
                self.selection = getattr(self.camera, "encoding_selection", None)
                cfg = self.camera.config
                command = [
                    self.settings.ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    # MPEG-TS supplies the capture clock, including pre-roll and skipped frames.
                    "-probesize",
                    "32",
                    "-analyzeduration",
                    "0",
                    "-f",
                    "mpegts",
                    "-i",
                    "pipe:0",
                    "-map",
                    "0:v:0",
                    "-an",
                    "-c:v",
                    "copy",
                    "-fps_mode",
                    "passthrough",
                    "-flush_packets",
                    "1",
                    "-f",
                    "rtsp",
                    "-rtsp_transport",
                    "tcp",
                    self.settings.mediamtx_rtsp.rstrip("/") + "/" + cfg.id,
                ]
                with self.spawn_lock:
                    if self.halt.is_set() or (source_halt is not None and source_halt.is_set()):
                        break
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=NO_WINDOW,
                    )
                    self.proc = process
                muxer = TransportStreamMuxer(epoch=first.epoch)
                current = first
                while not self.halt.is_set():
                    if process.poll() is not None:
                        raise CaptureError("Media packet publisher stopped")
                    if current is None:
                        if (
                            self.last_packet_at is not None
                            and time.monotonic() - self.last_packet_at > 3
                        ):
                            self.online = False
                            self.error = "Waiting for shared encoded camera packets"
                        current = subscription.read(timeout=0.2, cancel_event=self.halt)
                        continue
                    if self.halt.is_set() or (source_halt is not None and source_halt.is_set()):
                        break
                    process.stdin.write(muxer.mux(current))
                    process.stdin.flush()
                    self.packets_sent += 1
                    if self.halt.is_set():
                        break
                    if process.poll() is not None:
                        raise CaptureError("Media packet publisher stopped")
                    self.last_packet_at = time.monotonic()
                    self.online, self.error = True, None
                    current = None
            except EncodedRestart:
                self.online, self.error = (
                    False,
                    "Shared encoder restarted; reconnecting media packets",
                )
                retry = 0.1
            except (
                CaptureError,
                EncodedOverrun,
                EncodedStreamEnded,
                TransportStreamError,
                OSError,
            ):
                self.online, self.error = False, "Media packet publisher disconnected; retrying"
            finally:
                self.online = False
                self.last_packet_at = None
                if subscription is not None:
                    subscription.close()
                self.subscription = None
                if process is not None:
                    terminate_process(process)
                    if process.stdin is not None and not process.stdin.closed:
                        try:
                            process.stdin.close()
                        except OSError:
                            pass
            self.halt.wait(retry)

    def stop(self):
        with self.spawn_lock:
            self.halt.set()
            self.online = False
            self.last_packet_at = None
            subscription = getattr(self, "subscription", None)
            if subscription is not None:
                subscription.close()
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
            creationflags=NO_WINDOW,
        )
        if result.stdout.strip() != "v" + MEDIAMTX_VERSION:
            raise RuntimeError(f"Managed MediaMTX requires v{MEDIAMTX_VERSION}")
        self.proc = subprocess.Popen(
            [self.settings.mediamtx_binary, str(cfg)],
            cwd=self.settings.data_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
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
                    if (
                        camera_id not in self.publishers
                        and camera.encoding_ready.is_set()
                        and (
                            camera.encoding_selection is not None
                            or getattr(camera, "encoded_hub", None) is not None
                        )
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
                key: {
                    "online": pub.online and key in self.ready_paths,
                    "error": pub.error,
                    "encoder": pub.selection.codec if pub.selection else None,
                    "hardware": bool(pub.selection and pub.selection.hardware),
                    "fallback_reason": pub.selection.fallback_reason if pub.selection else None,
                    "packet_passthrough": bool(getattr(pub, "packet_passthrough", False)),
                    "packets_sent": getattr(pub, "packets_sent", 0),
                    "packet_ready": bool(
                        getattr(pub, "last_packet_at", None) is not None
                        and time.monotonic() - pub.last_packet_at <= 3
                        and pub.online
                    ),
                    "zero_copy_raw_frames": bool(
                        pub.selection and pub.selection.zero_copy_raw_frames
                    ),
                }
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
        host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
        origin = f"{url.scheme}://{host}:{8889 if protocol == 'webrtc' else 8888}"
        upstream = f"{origin}/{camera_id}/{path}"
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() in {"content-type", "if-match", "range"}
        }
        # Compressed responses could allocate an oversized decoded chunk before its
        # length can be checked. Media upstreams must honor this identity request.
        headers["accept-encoding"] = "identity"
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
        limit = HLS_PROXY_MAX_BYTES if protocol == "hls" else WHEP_PROXY_MAX_BYTES
        content = bytearray()
        try:
            # HTTPX's I/O timeout resets whenever bytes arrive; also bound the entire
            # exchange so a peer cannot keep a request alive with a slow trickle.
            async with asyncio.timeout(MEDIA_PROXY_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(
                    timeout=MEDIA_PROXY_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
                ) as client:
                    async with client.stream(
                        request.method,
                        upstream,
                        params=params,
                        headers=headers,
                        content=await request.body() if request.method != "GET" else None,
                    ) as response:
                        if (
                            response.headers.get("content-encoding", "identity").lower()
                            != "identity"
                        ):
                            raise CaptureError(
                                "MediaMTX response uses unsupported content encoding"
                            )
                        async for chunk in response.aiter_bytes():
                            if len(content) + len(chunk) > limit:
                                raise CaptureError("MediaMTX response exceeds its size limit")
                            content.extend(chunk)
        except TimeoutError:
            raise httpx.ReadTimeout("MediaMTX exchange exceeded its time limit") from None
        result_headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() in {"content-type", "etag", "accept-ranges", "content-range"}
        }
        if "location" in response.headers and protocol == "webrtc":
            try:
                location = urlsplit(response.headers["location"]).path
            except ValueError:
                raise CaptureError("Unexpected media session location") from None
            suffix = location.removeprefix("/" + camera_id + "/")
            if not re.fullmatch(r"whep/[a-fA-F0-9-]{1,64}", suffix):
                raise CaptureError("Unexpected media session location")
            result_headers["location"] = f"/api/v1/streams/{camera_id}/webrtc/{suffix}"
        elif 300 <= response.status_code < 400:
            raise CaptureError("Unexpected media redirect; check the MediaMTX configuration")
        return response.status_code, bytes(content), result_headers

    def stop(self):
        self.halt.set()
        if self.thread.is_alive():
            self.thread.join(timeout=15)
        for publisher in list(self.publishers.values()):
            publisher.stop()
        if self.proc:
            terminate_process(self.proc)
