from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, ValidationError, field_validator
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .capture import CaptureError, discover
from .config import Settings, home_lan_host
from .db import Database
from .events import EventBus
from .exports import ExportService
from .importers import ConfigImportError, parse_raspimjpeg, prepare_import
from .mdns import DiscoveryAnnouncer, advertise_ipv4, discovery_id
from .media import MediaStore, StorageError
from .mediamtx import MediaPlane
from .models import (
    CameraConfig,
    CameraPatch,
    ControlPatch,
    Login,
    RecordingRequest,
    Schedule,
    StrictModel,
    TimelapseRequest,
    UserCreate,
)
from .notify import MailChannel, NotificationService, NotificationSettings, WebhookChannel
from .onvif import (
    OnvifError,
    OnvifSettings,
    action_name,
    device_information,
    media_rtsp,
    profile_token,
    snapshot_uri,
    stream_uri,
    system_date,
    video_sources,
)
from .onvif import (
    authenticate as onvif_authenticate,
)
from .onvif import (
    capabilities as onvif_capabilities,
)
from .onvif import (
    fault as onvif_fault,
)
from .onvif import (
    load_state as load_onvif,
)
from .onvif import (
    move as ptz_move,
)
from .onvif import (
    profiles as onvif_profiles,
)
from .onvif import (
    public_state as public_onvif,
)
from .onvif import (
    save_state as save_onvif,
)
from .onvif import (
    services as onvif_services,
)
from .process_lock import ProcessLock
from .provision import audio_devices
from .recovery import RecoveryService
from .runtime import CameraManager
from .scheduler import Scheduler
from .security import Auth, TokenError, digest
from .shares import issue as issue_share
from .shares import public as list_shares
from .shares import resolve as resolve_share
from .shares import revoke as revoke_share
from .shares import revoke_media
from .system import Telemetry

LOG = logging.getLogger(__name__)


def request_hostname(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 1 else ""
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


class StreamSlots:
    """Bound concurrent JPEG readers per camera. WebRTC viewers do not consume a slot."""

    def __init__(self, limit=4):
        self.limit = limit
        self.lock = threading.Lock()
        self.counts = {}

    def acquire(self, key):
        with self.lock:
            if self.counts.get(key, 0) >= self.limit:
                return False
            self.counts[key] = self.counts.get(key, 0) + 1
            return True

    def release(self, key):
        with self.lock:
            remaining = self.counts.get(key, 0) - 1
            if remaining > 0:
                self.counts[key] = remaining
            else:
                self.counts.pop(key, None)


def _metric(name, value, labels=""):
    if value is None:
        return None
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, float):
        value = f"{value:.4f}"
    return f"{name}{labels} {value}"


class IntegrationPair(StrictModel):
    name: str = Field("Home Assistant", min_length=1, max_length=40)


class MediaLock(StrictModel):
    locked: bool


class UserPatch(StrictModel):
    role: str | None = Field(None, pattern="^(admin|operator|viewer)$")
    disabled: bool | None = None
    password: str | None = Field(None, min_length=12, max_length=128)


class StorageSettings(StrictModel):
    retention_days: int = Field(30, ge=1, le=3650)
    min_free_mb: int = Field(256, ge=32, le=1048576)


class PasswordChange(StrictModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=128)


class MediaAction(StrictModel):
    ids: list[str] = Field(min_length=1, max_length=100)
    action: Literal["lock", "unlock", "delete"]

    @field_validator("ids")
    @classmethod
    def ids_valid(cls, values):
        if len(set(values)) != len(values) or any(
            __import__("re").fullmatch(r"[a-f0-9]{32}", value) is None for value in values
        ):
            raise ValueError("Media id is not allowed")
        return values


class ShareRequest(StrictModel):
    hours: int = Field(24, ge=1, le=720)


class OnvifUpdate(StrictModel):
    enabled: bool = False
    username: str = Field("", max_length=64)
    password: str | None = Field(None, max_length=128)


class PtzCommand(StrictModel):
    action: Literal["move", "stop"]
    pan: float = Field(0, ge=-1, le=1)
    tilt: float = Field(0, ge=-1, le=1)
    zoom: float = Field(0, ge=-1, le=1)


class RaspimjpegImport(StrictModel):
    text: str = Field(min_length=1, max_length=65536)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.load()
    settings.prepare()
    db = Database(settings.data_dir / "campix.sqlite3")
    auth = Auth(db, settings)
    bus = EventBus(db)
    store = MediaStore(db, settings, bus)
    cameras = CameraManager(db, settings, store, bus)
    scheduler = Scheduler(db, cameras, store, bus)
    media_plane = MediaPlane(settings, cameras, bus)
    telemetry = Telemetry(settings)
    preview_slots = StreamSlots()
    audio_slots = StreamSlots(limit=2)
    exports = ExportService(db, store, settings, bus)
    notifications = NotificationService(db, bus)
    recovery = RecoveryService(store, settings, bus)
    ptz_timers: dict[str, threading.Timer] = {}
    process_lock = ProcessLock(settings.data_dir)
    saved_storage = db.setting("storage")
    if saved_storage:
        storage_settings = StorageSettings(**saved_storage)
        settings.retention_days = storage_settings.retention_days
        settings.min_free_mb = storage_settings.min_free_mb

    @asynccontextmanager
    async def lifespan(app):
        announcer = None
        try:
            process_lock.acquire()
        except BaseException:
            db.close()
            raise
        if settings.mdns:
            announcer = DiscoveryAnnouncer(
                settings.port,
                __version__,
                discovery_id(db),
                advertise_ipv4(settings.host),
            )
            announcer.start()
        bus.start()
        try:
            store.reconcile()
            notifications.start()
            cameras.start()
            scheduler.start()
            media_plane.start()
            exports.start()
            bus.publish("system.started", "CamPiX service started")
            yield
        finally:
            if announcer is not None:
                await asyncio.to_thread(announcer.stop)
            await asyncio.to_thread(scheduler.stop)
            await asyncio.to_thread(exports.stop)
            for timer in ptz_timers.values():
                timer.cancel()
            await asyncio.to_thread(recovery.stop)
            await asyncio.to_thread(media_plane.stop)
            await asyncio.to_thread(cameras.stop)
            await asyncio.to_thread(notifications.stop)
            db.close()
            process_lock.close()

    app = FastAPI(
        title="CamPiX",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    for name, value in {
        "settings": settings,
        "db": db,
        "auth": auth,
        "bus": bus,
        "store": store,
        "cameras": cameras,
        "scheduler": scheduler,
        "media_plane": media_plane,
        "exports": exports,
        "notifications": notifications,
        "recovery": recovery,
    }.items():
        setattr(app.state, name, value)

    if settings.public_origin:
        from urllib.parse import urlsplit

        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=[
                urlsplit(settings.public_origin).hostname,
                "127.0.0.1",
                "localhost",
                "testserver",
            ],
        )
    elif settings.host in {"127.0.0.1", "::1", "localhost"}:
        app.add_middleware(
            TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "::1", "localhost", "testserver"]
        )
    lan_http = not settings.public_origin and settings.host not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }

    @app.middleware("http")
    async def boundary(request, call_next):
        if lan_http and not home_lan_host(request_hostname(request.headers.get("host"))):
            return JSONResponse({"detail": "Invalid host header"}, status_code=400)
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site request rejected"}, status_code=403)
            try:
                if int(request.headers.get("content-length", "0")) > 65536:
                    return JSONResponse({"detail": "Request too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 65536:
                    return JSONResponse({"detail": "Request too large"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "same-origin",
                "X-Frame-Options": "DENY",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        if request.url.path.startswith(("/api", "/cam_", "/status_", "/cmd_", "/s/", "/onvif")):
            response.headers["Cache-Control"] = "no-store"
        if settings.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def role(level="viewer"):
        def dependency(request: Request):
            return auth.require(request, level)

        return dependency

    viewer, operator, admin = role(), role("operator"), role("admin")

    def hidden_cameras(user) -> list[str]:
        if user["role"] != "viewer":
            return []
        return [
            item["id"]
            for item in cameras.list()
            if item.get("viewer_users") and user["username"] not in item["viewer_users"]
        ]

    def view_camera(user, camera_id):
        camera = cameras.get(camera_id)
        allowed = camera.config.viewer_users
        if user["role"] == "viewer" and allowed and user["username"] not in allowed:
            raise KeyError(camera_id)
        return camera

    def ensure_viewers(names) -> None:
        if not names:
            return
        known = {row["username"] for row in db.all("SELECT username FROM users")}
        if any(name not in known for name in names):
            raise HTTPException(422, "Viewer name is not an account")

    def service_root(request: Request) -> str:
        if settings.public_origin:
            return settings.public_origin.rstrip("/")
        host = request_hostname(request.headers.get("host"))
        if home_lan_host(host):
            return f"{request.url.scheme}://{request.headers.get('host')}"
        return f"http://127.0.0.1:{settings.port}"

    def connect_ptz(address, port, timeout, https):
        import http.client

        client = http.client.HTTPSConnection if https else http.client.HTTPConnection
        return client(address, port, timeout=timeout)

    def send_ptz(camera, action: str, pan: float, tilt: float, zoom: float) -> None:
        if camera.config.backend != "network" or not camera.config.onvif_ptz:
            raise CaptureError("PTZ is not enabled for this camera")
        if not camera.config.onvif_username or not camera.config.onvif_password:
            raise CaptureError("PTZ credentials are not configured")
        from urllib.parse import urlsplit

        parts = urlsplit(camera.config.source)
        if not parts.hostname:
            raise CaptureError("PTZ camera address is not allowed")
        try:
            ptz_move(
                connect_ptz,
                parts.hostname,
                camera.config.onvif_port,
                parts.scheme in {"https", "rtsps"},
                camera.config.onvif_username,
                camera.config.onvif_password,
                action,
                pan,
                tilt,
                zoom,
            )
        except OnvifError as exc:
            raise CaptureError(str(exc)) from exc

    def arm_ptz_stop(camera_id: str) -> None:
        previous = ptz_timers.pop(camera_id, None)
        if previous:
            previous.cancel()

        def stop_later():
            ptz_timers.pop(camera_id, None)
            try:
                send_ptz(cameras.get(camera_id), "stop", 0, 0, 0)
            except (CaptureError, KeyError):
                return

        timer = threading.Timer(1.0, stop_later)
        timer.daemon = True
        ptz_timers[camera_id] = timer
        timer.start()

    validation_locations = {
        "body",
        "query",
        "path",
        "header",
        "cookie",
        "float",
        "int",
        "bool",
        "str",
        "cmd",
        "limit",
        "offset",
        "kind",
        "q",
        "download",
        "user_id",
        "media_id",
        "job_id",
        "schedule_id",
        "protocol",
        "view",
        "after",
            "x",
            "y",
            "w",
            "h",
            "created_after",
            "created_before",
            "token",
            "camera_id",
        } | {
        field
        for model in (
            CameraConfig,
            CameraPatch,
            ControlPatch,
            Login,
            RecordingRequest,
            Schedule,
            TimelapseRequest,
            UserCreate,
            IntegrationPair,
            MediaLock,
            UserPatch,
            StorageSettings,
            NotificationSettings,
            WebhookChannel,
            MailChannel,
            PasswordChange,
            MediaAction,
            ShareRequest,
            OnvifUpdate,
            PtzCommand,
            RaspimjpegImport,
            OnvifSettings,
        )
        for field in model.model_fields
    }
    validator_messages = {
        f"Value error, {message}": f"Value error, {message}"
        for message in (
            "Picamera2 source must be a camera index",
            "V4L2 source must be /dev/videoN",
            "DirectShow source must be a device name without quotes, colons, or control characters",
            "Network source must be an RTSP or HTTP(S) camera URL",
            "GStreamer vendor pipelines require an installed adapter; arbitrary pipelines are disabled",
            "Legacy source must be an absolute preview JPEG path",
            "Unsupported or non-finite control",
            "Each snapshot may be selected once",
            "days must contain distinct weekday numbers 0–6",
            "Unknown IANA timezone",
            "Recording duration must be shorter than the schedule interval",
            "Motion region must stay inside the frame",
            "Motion mask must be empty or 144 characters of 0 and 1",
            "Motion mask must leave at least one cell active",
            "Synthetic audio uses the generated stream and accepts no device",
            "Network audio uses the camera URL and accepts no separate device",
            "This camera backend has no separate audio stream",
            "DirectShow audio source must be a device name without quotes, colons, or control characters",
            "ALSA audio source must be default, hw:N, or plughw:N,M",
            "AVFoundation audio source must be a device index from 0 to 32",
            "AVFoundation source must be a video device index from 0 to 32",
            "Viewer name is not allowed",
            "PTZ is available only for a network camera",
            "ONVIF username is not allowed",
            "ONVIF password is not allowed",
            "End minute must be 0 when the window ends at hour 24",
            "Media id is not allowed",
            "Notification kind is not in the catalog",
            "Notification camera is not allowed",
            "Webhook URL is not allowed",
            "Mail recipient is not allowed",
        )
    }

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Inputs, mapping keys and custom validator messages can contain passwords or URLs.
        messages = {
            "missing": "Field required",
            "extra_forbidden": "Extra inputs are not permitted",
            "string_type": "Input should be a string",
            "string_too_short": "String is shorter than the allowed length",
            "string_too_long": "String exceeds the allowed length",
            "string_pattern_mismatch": "String does not match the required format",
            "bool_type": "Input should be a boolean",
            "bool_parsing": "Input should be a valid boolean",
            "int_type": "Input should be an integer",
            "int_parsing": "Input should be a valid integer",
            "int_from_float": "Input should be an integer without a fractional part",
            "float_type": "Input should be a number",
            "float_parsing": "Input should be a valid number",
            "finite_number": "Input should be a finite number",
            "greater_than": "Input is below the allowed range",
            "greater_than_equal": "Input is below the allowed range",
            "less_than": "Input exceeds the allowed range",
            "less_than_equal": "Input exceeds the allowed range",
            "multiple_of": "Input is not an allowed multiple",
            "literal_error": "Input is not an allowed value",
            "list_type": "Input should be a list",
            "dict_type": "Input should be a dictionary",
            "too_short": "Input contains fewer items than allowed",
            "too_long": "Input contains more items than allowed",
            "json_invalid": "Malformed JSON",
        }
        return JSONResponse(
            {
                "detail": [
                    {
                        "type": error["type"],
                        "loc": [
                            part
                            if isinstance(part, int) or part in validation_locations
                            else "[redacted]"
                            for part in error["loc"]
                        ],
                        "msg": (
                            validator_messages.get(error.get("msg"), "Invalid value")
                            if error["type"] == "value_error"
                            else messages.get(error["type"], "Invalid value")
                        ),
                    }
                    for error in exc.errors()
                ]
            },
            status_code=422,
        )

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "Resource not found"}, status_code=404)

    @app.exception_handler(CaptureError)
    async def camera_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(StorageError)
    async def storage_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(request, exc):
        return JSONResponse(
            {"detail": "Resource already exists or violates a dependency"}, status_code=409
        )

    @app.exception_handler(Exception)
    async def internal(request, exc):
        LOG.error("Request %s failed: %s", request.url.path, type(exc).__name__)
        return JSONResponse({"detail": "Operation failed; check the service log"}, status_code=500)

    @app.get("/healthz")
    def health():
        return {"status": "ok", "version": __version__, "process_id": os.getpid()}

    @app.get("/api/v1/openapi", dependencies=[Depends(viewer)])
    def openapi():
        return app.openapi()

    @app.post("/api/v1/auth/login")
    def login(payload: Login, request: Request):
        if not auth.origin_allowed(request.headers.get("origin"), request.url):
            raise HTTPException(403, "Cross-site login rejected")
        token, csrf, user = auth.login(payload.username, payload.password, request.client.host)
        response = JSONResponse({"user": user, "csrf": csrf})
        response.set_cookie(
            "campix_session",
            token,
            max_age=settings.session_hours * 3600,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="strict",
            path="/",
        )
        bus.publish("auth.login", "User signed in", data={"user_id": user["id"]})
        return response

    @app.get("/api/v1/auth/me")
    def me(user=Depends(viewer)):
        return {"user": {k: user[k] for k in ("id", "username", "role")}, "csrf": user["csrf"]}

    @app.post("/api/v1/auth/logout", dependencies=[Depends(viewer)])
    def logout(request: Request):
        token = request.cookies.get("campix_session")
        if token:
            db.execute("DELETE FROM sessions WHERE token=?", (digest(token),))
        response = JSONResponse({"ok": True})
        response.delete_cookie(
            "campix_session",
            path="/",
            secure=settings.secure_cookies,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.post("/api/v1/auth/password")
    def change_password(payload: PasswordChange, request: Request, user=Depends(viewer)):
        from argon2.exceptions import InvalidHashError, VerificationError

        from .security import HASHER

        if str(user["id"]).startswith("token:"):
            raise HTTPException(403, "Sign in with a password to change it")
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id=? AND disabled=0", (user["id"],)
            ).fetchone()
            if not row:
                raise HTTPException(403, "Current password was not accepted")
            try:
                HASHER.verify(row["password"], payload.current_password)
            except (VerificationError, InvalidHashError):
                raise HTTPException(403, "Current password was not accepted") from None
            conn.execute(
                "UPDATE users SET password=? WHERE id=?",
                (HASHER.hash(payload.new_password), row["id"]),
            )
            conn.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
        response = JSONResponse({"ok": True})
        response.delete_cookie(
            "campix_session",
            path="/",
            secure=settings.secure_cookies,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/api/v1/users", dependencies=[Depends(admin)])
    def users():
        return db.all("SELECT id,username,role,disabled FROM users ORDER BY username")

    @app.post("/api/v1/users", dependencies=[Depends(admin)], status_code=201)
    def add_user(payload: UserCreate):
        user_id = auth.create_user(payload.username, payload.password, payload.role)
        return {"id": user_id, "username": payload.username, "role": payload.role}

    @app.patch("/api/v1/users/{user_id}", dependencies=[Depends(admin)])
    def edit_user(user_id: int, payload: UserPatch):
        from .security import HASHER

        with db.transaction() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                raise KeyError(user_id)
            if (
                row["role"] == "admin"
                and not row["disabled"]
                and (payload.disabled or payload.role and payload.role != "admin")
            ):
                if (
                    conn.execute(
                        "SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0"
                    ).fetchone()[0]
                    <= 1
                ):
                    raise HTTPException(409, "At least one active administrator is required")
            for key in ("role", "disabled", "password"):
                value = getattr(payload, key)
                if value is not None:
                    conn.execute(
                        f"UPDATE users SET {key}=? WHERE id=?",
                        (HASHER.hash(value) if key == "password" else value, user_id),
                    )
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        return {"ok": True}

    @app.get("/api/v1/system", dependencies=[Depends(viewer)])
    def system():
        return {
            **telemetry.read(),
            "version": __version__,
            "media_plane": media_plane.status(),
            "scheduler_alive": scheduler.thread.is_alive(),
        }

    @app.get("/api/v1/cameras")
    def list_cameras(user=Depends(viewer)):
        hidden = set(hidden_cameras(user))
        return [item for item in cameras.list() if item["id"] not in hidden]

    @app.get("/api/v1/discovery", dependencies=[Depends(admin)])
    def discovery():
        return {"devices": discover(settings.ffmpeg), "encoders": cameras.encoders}

    @app.get("/api/v1/discovery/audio", dependencies=[Depends(admin)])
    def discovery_audio():
        return audio_devices(settings.ffmpeg)

    @app.post("/api/v1/cameras", dependencies=[Depends(admin)], status_code=201)
    def save_camera(payload: CameraConfig):
        ensure_viewers(payload.viewer_users)
        return cameras.save(payload)

    @app.get("/api/v1/cameras/{camera_id}/config", dependencies=[Depends(admin)])
    def camera_config(camera_id: str):
        config = cameras.get(camera_id).config.model_dump()
        # A full replace uses this document. Keep the stored PTZ secret out of it
        # without adding a field the camera model would reject.
        config.pop("onvif_password", None)
        if config["backend"] == "network":
            config["source"] = "[redacted: set a source explicitly when updating this profile]"
        return config

    @app.delete("/api/v1/cameras/{camera_id}", dependencies=[Depends(admin)])
    def delete_camera(camera_id: str):
        cameras.remove(camera_id)
        return {"ok": True}

    @app.patch("/api/v1/cameras/{camera_id}", dependencies=[Depends(admin)])
    def patch_camera(camera_id: str, payload: CameraPatch):
        previous = cameras.get(camera_id).config.model_dump()
        if payload.viewer_users is not None:
            ensure_viewers(payload.viewer_users)
        try:
            config = CameraConfig(**{**previous, **payload.model_dump(exclude_none=True)})
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc
        return cameras.save(config)

    @app.patch("/api/v1/cameras/{camera_id}/controls", dependencies=[Depends(operator)])
    def controls(camera_id: str, payload: ControlPatch, user=Depends(operator)):
        view_camera(user, camera_id)
        return cameras.controls(camera_id, payload.model_dump(exclude_none=True))

    @app.post(
        "/api/v1/cameras/{camera_id}/snapshot", dependencies=[Depends(operator)], status_code=201
    )
    def snapshot(camera_id: str, user=Depends(operator)):
        return view_camera(user, camera_id).snapshot()

    @app.post("/api/v1/cameras/{camera_id}/recording", dependencies=[Depends(operator)])
    def start_recording(camera_id: str, payload: RecordingRequest, user=Depends(operator)):
        return view_camera(user, camera_id).start_recording(payload.duration)

    @app.delete("/api/v1/cameras/{camera_id}/recording", dependencies=[Depends(operator)])
    def stop_recording(camera_id: str, user=Depends(operator)):
        return view_camera(user, camera_id).stop_recording()

    @app.get("/api/v1/cameras/{camera_id}/preview.jpg", dependencies=[Depends(viewer)])
    def preview(camera_id: str, user=Depends(viewer), view: str = ""):
        if view not in {"", "live"}:
            raise HTTPException(400, "Unknown preview view")
        if not preview_slots.acquire(camera_id):
            raise HTTPException(429, "Too many live preview readers", headers={"Retry-After": "1"})
        try:
            camera = view_camera(user, camera_id)
            payload = camera.live_frame() if view == "live" else camera.frame()[1]
            return Response(payload, media_type="image/jpeg")
        finally:
            preview_slots.release(camera_id)

    def stream_credential(request: Request):
        cookie = request.cookies.get("campix_session")
        if auth.session(cookie):
            return ("session", cookie)
        header = request.headers.get("authorization")
        if auth.tokens.authenticate(header):
            return ("bearer", header)
        raise HTTPException(401, "Sign in required")

    def credential_alive(credential) -> bool:
        kind, value = credential
        if kind == "session":
            return bool(auth.session(value))
        return bool(auth.tokens.authenticate(value))

    async def mjpeg(camera_id, credential):
        if not preview_slots.acquire(camera_id):
            raise HTTPException(429, "Too many live preview readers", headers={"Retry-After": "1"})
        camera = cameras.get(camera_id)
        sequence = -1
        try:
            while credential_alive(credential):
                try:
                    current, frame = await asyncio.to_thread(camera.frame, sequence, 3)
                except CaptureError:
                    break
                if current == sequence:
                    await asyncio.sleep(0.1)
                    continue
                sequence = current
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode()
                    + b"\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
        finally:
            preview_slots.release(camera_id)

    @app.get("/api/v1/cameras/{camera_id}/audio.wav", dependencies=[Depends(viewer)])
    def audio(camera_id: str, user=Depends(viewer), after: int = Query(0, ge=0, le=2_147_483_647)):
        if not audio_slots.acquire(camera_id):
            raise HTTPException(429, "Too many audio readers", headers={"Retry-After": "1"})
        try:
            camera = view_camera(user, camera_id)
            if not camera.config.audio_enabled:
                raise CaptureError("Audio is off for this camera")
            sequence, payload = camera.audio_wav(after)
            return Response(
                payload,
                media_type="audio/wav",
                headers={"X-Audio-Sequence": str(sequence)},
            )
        finally:
            audio_slots.release(camera_id)

    @app.get("/api/v1/cameras/{camera_id}/stream.mjpg", dependencies=[Depends(viewer)])
    def stream(camera_id: str, request: Request, user=Depends(viewer)):
        credential = stream_credential(request)
        view_camera(user, camera_id).frame()
        return StreamingResponse(
            mjpeg(camera_id, credential),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    @app.api_route(
        "/api/v1/streams/{camera_id}/{protocol}/{path:path}",
        methods=["GET", "POST", "PATCH", "DELETE"],
        dependencies=[Depends(viewer)],
    )
    async def media_proxy(camera_id: str, protocol: str, path: str, request: Request, user=Depends(viewer)):
        view_camera(user, camera_id)
        try:
            status, content, headers = await media_plane.proxy(camera_id, protocol, path, request)
        except httpx.HTTPError:
            raise HTTPException(503, "MediaMTX unavailable; use MJPEG") from None
        return Response(content, status_code=status, headers=headers)

    @app.get("/api/v1/media", dependencies=[Depends(viewer)])
    def media(
        user=Depends(viewer),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        kind: str | None = None,
        camera_id: str | None = None,
        q: str = Query("", max_length=80, pattern=r"^[^\x00-\x1f\x7f]*$"),
        created_after: int | None = Query(None, ge=0, le=4_102_444_800),
        created_before: int | None = Query(None, ge=0, le=4_102_444_800),
    ):
        if camera_id and camera_id in hidden_cameras(user):
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
        return store.list(
            limit,
            offset,
            kind,
            camera_id,
            q.strip(),
            created_after,
            created_before,
            hidden_cameras(user),
        )

    @app.post("/api/v1/media/actions", dependencies=[Depends(operator)])
    def media_actions(payload: MediaAction):
        result = store.apply_many(payload.ids, payload.action)
        if payload.action == "delete":
            for item in result["results"]:
                if item["ok"]:
                    revoke_media(db, item["id"])
                    bus.publish("media.deleted", "Media deleted", data={"media_id": item["id"]})
        return result

    @app.post("/api/v1/media/recovery", dependencies=[Depends(admin)], status_code=202)
    def start_recovery():
        return recovery.start()

    @app.get("/api/v1/media/recovery", dependencies=[Depends(admin)])
    def recovery_status():
        return recovery.snapshot()

    @app.post("/api/v1/media/{media_id}/share", dependencies=[Depends(operator)])
    def share_media(media_id: str, payload: ShareRequest, user=Depends(operator)):
        item = store.get(media_id)
        if item["camera_id"] in hidden_cameras(user):
            raise KeyError(media_id)
        link = issue_share(db, media_id, payload.hours, user["username"])
        bus.publish("media.shared", "Media link created", data={"share_id": link["id"]})
        return link

    @app.get("/api/v1/shares", dependencies=[Depends(operator)])
    def shares():
        return list_shares(db)

    @app.delete("/api/v1/shares/{link_id}", dependencies=[Depends(operator)])
    def delete_share(link_id: str):
        if not revoke_share(db, link_id):
            raise KeyError(link_id)
        return {"ok": True}

    @app.get("/api/v1/media/{media_id}/file")
    def media_file(media_id: str, user=Depends(viewer), download: bool = False):
        item = store.get(media_id)
        if item["camera_id"] in hidden_cameras(user):
            raise KeyError(media_id)
        path = store.path(item["filename"])
        if not path.is_file():
            raise HTTPException(404, "Media file unavailable")
        return FileResponse(
            path,
            media_type="image/jpeg" if item["kind"] == "snapshot" else "video/mp4",
            filename=item["filename"] if download else None,
        )

    @app.patch("/api/v1/media/{media_id}", dependencies=[Depends(operator)])
    def media_lock(media_id: str, payload: MediaLock):
        return store.set_locked(media_id, payload.locked)

    @app.delete("/api/v1/media/{media_id}", dependencies=[Depends(operator)])
    def media_delete(media_id: str):
        store.delete(media_id)
        revoke_media(db, media_id)
        bus.publish("media.deleted", "Media deleted", data={"media_id": media_id})
        return {"ok": True}

    @app.post("/api/v1/timelapse", dependencies=[Depends(operator)], status_code=202)
    def timelapse(payload: TimelapseRequest):
        if "libx264" not in cameras.encoders["encoders"]:
            raise CaptureError("Timelapse requires FFmpeg with libx264")
        return exports.submit(payload)

    @app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(viewer)])
    def export_job(job_id: str):
        return exports.get(job_id)

    @app.get("/api/v1/schedules", dependencies=[Depends(viewer)])
    def list_schedules():
        return scheduler.list()

    @app.post("/api/v1/schedules", dependencies=[Depends(admin)], status_code=201)
    def save_schedule(payload: Schedule):
        return scheduler.save(payload)

    @app.delete("/api/v1/schedules/{schedule_id}", dependencies=[Depends(admin)])
    def delete_schedule(schedule_id: str):
        if not db.one("SELECT id FROM schedules WHERE id=?", (schedule_id,)):
            raise KeyError(schedule_id)
        db.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
        return {"ok": True}

    @app.get("/api/v1/events", dependencies=[Depends(viewer)])
    def events(user=Depends(viewer), limit: int = Query(50, ge=1, le=200)):
        rows = db.all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        hidden = set(hidden_cameras(user))
        visible = []
        for row in rows:
            row["data"] = json.loads(row["data"])
            if row["camera_id"] and row["camera_id"] in hidden:
                continue
            visible.append(row)
        return visible

    @app.get("/api/v1/settings", dependencies=[Depends(admin)])
    def read_settings():
        return {
            "retention_days": settings.retention_days,
            "min_free_mb": settings.min_free_mb,
            "demo": settings.demo,
            "secure_cookies": settings.secure_cookies,
            "public_origin": settings.public_origin,
            "media_plane": media_plane.status(),
        }

    @app.patch("/api/v1/settings", dependencies=[Depends(admin)])
    def update_settings(payload: StorageSettings):
        db.set_setting("storage", payload.model_dump())
        settings.retention_days, settings.min_free_mb = payload.retention_days, payload.min_free_mb
        bus.publish("settings.saved", "Storage settings saved")
        return read_settings()

    @app.get("/api/v1/notifications", dependencies=[Depends(admin)])
    def read_notifications():
        return notifications.state()

    @app.patch("/api/v1/notifications", dependencies=[Depends(admin)])
    def update_notifications(payload: NotificationSettings):
        try:
            notifications.update(payload)
        except ValueError as exc:
            raise HTTPException(422, "Webhook address is not allowed") from exc
        bus.publish("settings.saved", "Notification settings saved")
        return notifications.state()

    @app.post("/api/v1/notifications/secret", dependencies=[Depends(admin)])
    def rotate_notification_secret():
        secret = notifications.rotate_secret()
        bus.publish("settings.saved", "Notification signing secret rotated")
        return {"secret": secret}

    @app.post("/api/v1/integrations/homeassistant/pair", dependencies=[Depends(admin)])
    def pair_home_assistant(payload: IntegrationPair):
        try:
            token, public = auth.tokens.issue(payload.name)
        except TokenError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        bus.publish("integration.paired", "Home Assistant paired", data={"token_id": public["id"]})
        return {"token": token, "token_id": public["id"], "role": "viewer"}

    @app.get("/api/v1/integrations/tokens", dependencies=[Depends(admin)])
    def integration_tokens():
        return auth.tokens.public()

    @app.delete("/api/v1/integrations/tokens/{token_id}", dependencies=[Depends(admin)])
    def revoke_integration_token(token_id: str):
        if not auth.tokens.revoke(token_id):
            raise KeyError(token_id)
        bus.publish("integration.revoked", "API token revoked", data={"token_id": token_id})
        return {"ok": True}

    @app.get("/api/v1/integrations/homeassistant", dependencies=[Depends(viewer)])
    def home_assistant_catalog(user=Depends(viewer)):
        service_id = discovery_id(db)
        listed = []
        for item in list_cameras(user):
            camera_id = item["id"]
            listed.append(
                {
                    "id": camera_id,
                    "name": item["name"],
                    "unique_id": f"campix:{camera_id}",
                    "still_path": f"/api/v1/cameras/{camera_id}/preview.jpg?view=live",
                    "mjpeg_path": f"/api/v1/cameras/{camera_id}/stream.mjpg",
                    "online": item["status"] == "online",
                }
            )
        return {
            "version": __version__,
            "service": f"CamPiX-{service_id}",
            "service_id": service_id,
            "cameras": listed,
        }

    @app.post("/api/v1/cameras/{camera_id}/ptz", dependencies=[Depends(operator)])
    def camera_ptz(camera_id: str, payload: PtzCommand, user=Depends(operator)):
        camera = view_camera(user, camera_id)
        send_ptz(camera, payload.action, payload.pan, payload.tilt, payload.zoom)
        previous = ptz_timers.pop(camera_id, None)
        if previous:
            previous.cancel()
        if payload.action == "move":
            arm_ptz_stop(camera_id)
        return {"ok": True}

    @app.get("/api/v1/export", dependencies=[Depends(admin)])
    def export():
        profiles = []
        for row in db.all("SELECT config FROM cameras"):
            profile = json.loads(row["config"])
            profile["onvif_credentials_set"] = bool(profile.get("onvif_password"))
            profile.pop("onvif_password", None)
            if profile["backend"] == "network":
                profile["source"] = "[redacted]"
            profiles.append(profile)
        return {
            "version": __version__,
            "cameras": profiles,
            "schedules": scheduler.list(),
            "storage": {
                "retention_days": settings.retention_days,
                "min_free_mb": settings.min_free_mb,
            },
        }

    @app.post("/api/v1/import", dependencies=[Depends(admin)])
    def import_configuration(payload: dict):
        existing = {}
        for row in db.all("SELECT config FROM cameras"):
            profile = json.loads(row["config"])
            existing[profile["id"]] = profile
        try:
            imported, schedules, storage = prepare_import(payload, existing)
        except ConfigImportError as exc:
            raise HTTPException(422, str(exc)) from exc
        for camera in imported:
            ensure_viewers(camera.viewer_users)
        saved = [cameras.save(camera) for camera in imported]
        for schedule in schedules:
            scheduler.save(schedule)
        if storage:
            db.set_setting("storage", storage)
            settings.retention_days = storage["retention_days"]
            settings.min_free_mb = storage["min_free_mb"]
        bus.publish("settings.saved", "Configuration imported")
        return {"cameras": len(saved), "schedules": len(schedules)}

    @app.post("/api/v1/import/raspimjpeg", dependencies=[Depends(admin)])
    def import_raspimjpeg(payload: RaspimjpegImport):
        try:
            return parse_raspimjpeg(payload.text)
        except ConfigImportError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/backup", dependencies=[Depends(admin)])
    def download_backup():
        descriptor, name = tempfile.mkstemp(
            prefix="campix-backup-", suffix=".sqlite3", dir=settings.data_dir
        )
        os.close(descriptor)
        destination = settings.data_dir / os.path.basename(name)
        try:
            target = sqlite3.connect(destination)
            try:
                with db.lock:
                    db.conn.backup(target)
                target.execute("DELETE FROM sessions")
                target.commit()
                target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                target.close()
            destination.chmod(0o600)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return FileResponse(
            destination,
            media_type="application/vnd.sqlite3",
            filename="campix-backup.sqlite3",
            background=BackgroundTask(destination.unlink, missing_ok=True),
        )

    @app.get("/api/v1/integrations/onvif", dependencies=[Depends(admin)])
    def read_onvif():
        current, password_hash = load_onvif(db)
        return public_onvif(current, bool(password_hash))

    @app.put("/api/v1/integrations/onvif", dependencies=[Depends(admin)])
    def write_onvif(payload: OnvifUpdate):
        _current, previous = load_onvif(db)
        try:
            updated = OnvifSettings(enabled=payload.enabled, username=payload.username)
            password_hash = save_onvif(db, updated, payload.password, previous)
        except ValueError as exc:
            message = str(exc)
            allowed = {
                "ONVIF username is not allowed",
                "ONVIF password is not allowed",
                "ONVIF password is required",
            }
            raise HTTPException(422, message if message in allowed else "ONVIF settings were rejected") from exc
        bus.publish("settings.saved", "ONVIF settings saved")
        return public_onvif(updated, bool(password_hash))

    @app.get("/metrics", dependencies=[Depends(viewer)])
    def metrics(user=Depends(viewer)):
        system = telemetry.read()
        lines = [
            sample
            for sample in (
                _metric("campix_uptime_seconds", system["uptime_seconds"]),
                _metric("campix_process_memory_bytes", system["process_memory_bytes"]),
                _metric("campix_process_cpu_percent", system["process_cpu_percent"]),
                _metric("campix_cpu_percent", system["cpu_percent"]),
                _metric("campix_temperature_celsius", system["temperature_c"]),
                _metric("campix_disk_free_bytes", system["disk_free_bytes"]),
                _metric("campix_mediamtx_up", int(bool(media_plane.api))),
            )
            if sample
        ]
        for name, value in media_plane.metric_samples().items():
            sample = _metric(f"campix_mediamtx_{name}", value)
            if sample:
                lines.append(sample)
        for camera in list_cameras(user):
            labels = f'{{camera="{camera["id"]}"}}'
            for metric, value in (
                ("fps", camera["fps"]),
                ("dropped_frames", camera["dropped_frames"]),
                ("capture_dropped_frames", camera["capture_dropped_frames"]),
                ("frame_age_seconds", camera["frame_age_seconds"]),
                ("preroll_bytes", camera["preroll_bytes"]),
                ("encoded_buffered_bytes", camera["encoded_buffered_bytes"]),
                ("encoded_dropped_frames", camera["encoded_dropped_frames"]),
                ("encoded_frame_age_seconds", camera["encoded_frame_age_seconds"]),
                ("recording_write_bytes", camera["recording_write_bytes"]),
                ("shed_level", camera["shed_level"]),
                ("online", int(camera["status"] == "online")),
            ):
                sample = _metric(f"campix_camera_{metric}", value, labels)
                if sample:
                    lines.append(sample)
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.websocket("/api/v1/events/ws")
    async def event_socket(ws: WebSocket):
        token = ws.cookies.get("campix_session")
        if not auth.session(token) or not auth.origin_allowed(ws.headers.get("origin"), ws.url):
            await ws.close(code=1008)
            return
        await ws.accept()
        queue = bus.subscribe()
        try:
            while auth.session(token):
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10)
                except TimeoutError:
                    event = {"kind": "heartbeat", "created": time.time()}
                session = auth.session(token)
                if not session:
                    break
                camera_id = event.get("camera_id")
                if camera_id and camera_id in hidden_cameras(session):
                    continue
                await ws.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            bus.unsubscribe(queue)
            try:
                await ws.close(code=1000)
            except (RuntimeError, WebSocketDisconnect):
                pass

    # Restricted migration facade. No system commands, arbitrary macros or GET mutations.
    def primary_camera(user):
        items = list_cameras(user)
        if not items:
            raise CaptureError("No camera configured")
        return cameras.get(items[0]["id"])

    @app.get("/cam_pic.php", dependencies=[Depends(viewer)])
    @app.get("/cam_get.php", dependencies=[Depends(viewer)])
    def legacy_preview(user=Depends(viewer)):
        return Response(primary_camera(user).frame()[1], media_type="image/jpeg")

    @app.get("/status_mjpeg.php", dependencies=[Depends(viewer)])
    def legacy_status(user=Depends(viewer)):
        info = primary_camera(user).info()
        return Response(
            "video" if info["recording"] else "ready" if info["status"] == "online" else "halted",
            media_type="text/plain",
        )

    @app.post("/cmd_pipe.php", dependencies=[Depends(operator)])
    def legacy_command(cmd: str = Query(max_length=80), user=Depends(operator)):
        camera = primary_camera(user)
        if cmd == "im":
            return camera.snapshot(trigger="legacy")
        if cmd == "ca 1":
            return camera.start_recording(trigger="legacy")
        if cmd == "ca 0":
            return camera.stop_recording()
        raise HTTPException(400, "Only im, ca 1 and ca 0 are supported by this migration facade")

    def onvif_body(name: str, body: bytes, request: Request):
        root = service_root(request)
        listed = [
            {
                "id": item["id"],
                "name": item["name"],
                "width": item["width"],
                "height": item["height"],
                "fps": item["fps_target"],
            }
            for item in cameras.list()
        ]
        known = {item["id"] for item in listed}
        if name == "GetSystemDateAndTime":
            return system_date(), 200
        if name == "GetDeviceInformation":
            return device_information(__version__, discovery_id(db)), 200
        if name == "GetCapabilities":
            return onvif_capabilities(root), 200
        if name == "GetServices":
            return onvif_services(root), 200
        if name == "GetProfiles":
            return onvif_profiles(listed), 200
        if name == "GetVideoSources":
            return video_sources(listed), 200
        if name in {"GetStreamUri", "GetSnapshotUri"}:
            token = profile_token(body) or (listed[0]["id"] if listed else "")
            if token not in known:
                return onvif_fault("ONVIF profile was rejected"), 400
            if name == "GetStreamUri":
                return stream_uri(media_rtsp(settings.mediamtx_rtsp, token)), 200
            return snapshot_uri(f"{root}/onvif/snapshot/{token}"), 200
        return onvif_fault("ONVIF action is not implemented"), 400

    @app.post("/onvif/device_service")
    async def onvif_device(request: Request):
        current, password_hash = load_onvif(db)
        if not current.enabled or not password_hash:
            raise HTTPException(404, "Resource not found")
        body = await request.body()
        if len(body) > 65536:
            return Response(onvif_fault("ONVIF request was rejected"), status_code=413, media_type="application/soap+xml")
        try:
            name = action_name(body)
            if name != "GetSystemDateAndTime":
                host = request.client.host if request.client else "unknown"
                if not onvif_authenticate(body, request.headers.get("authorization"), current, password_hash):
                    auth.limiter.check("onvif:" + host, 30, 300)
                    return Response(
                        onvif_fault("ONVIF sign-in was rejected"),
                        status_code=401,
                        media_type="application/soap+xml",
                        headers={"WWW-Authenticate": 'Basic realm="CamPiX"'},
                    )
            payload, status = onvif_body(name, body, request)
        except OnvifError as exc:
            return Response(onvif_fault(str(exc)), status_code=exc.status, media_type="application/soap+xml")
        return Response(payload, status_code=status, media_type="application/soap+xml")

    @app.get("/onvif/snapshot/{camera_id}")
    def onvif_snapshot(camera_id: str, request: Request):
        current, password_hash = load_onvif(db)
        if not current.enabled or not password_hash:
            raise HTTPException(404, "Resource not found")
        if not onvif_authenticate(b"", request.headers.get("authorization"), current, password_hash):
            raise HTTPException(
                401,
                "ONVIF sign-in was rejected",
                headers={"WWW-Authenticate": 'Basic realm="CamPiX"'},
            )
        return Response(cameras.get(camera_id).frame()[1], media_type="image/jpeg")

    @app.get("/s/{token}")
    def shared_file(token: str, request: Request):
        import re

        if re.fullmatch(r"[A-Za-z0-9_-]{20,128}", token) is None:
            raise HTTPException(404, "Resource not found")
        host = request.client.host if request.client else "unknown"
        auth.limiter.check("share:" + host, 60, 60)
        row = resolve_share(db, token)
        if not row:
            raise HTTPException(404, "Resource not found")
        item = store.get(row["media_id"])
        path = store.path(item["filename"])
        if not path.is_file():
            raise HTTPException(404, "Media file unavailable")
        return FileResponse(
            path,
            media_type="image/jpeg" if item["kind"] == "snapshot" else "video/mp4",
            filename=item["filename"],
        )

    web_root = settings.resolved_web_root()
    if not (web_root / "index.html").is_file():
        raise RuntimeError(
            "Web assets missing; install the built CamPiX wheel or set CAMPIX_WEB_ROOT"
        )
    app.mount("/", StaticFiles(directory=web_root, html=True), name="web")
    return app
