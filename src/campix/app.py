from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .capture import CaptureError, discover
from .config import Settings
from .db import Database
from .events import EventBus
from .exports import ExportService
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
from .process_lock import ProcessLock
from .runtime import CameraManager
from .scheduler import Scheduler
from .security import Auth, digest
from .system import Telemetry

LOG = logging.getLogger(__name__)


class MediaLock(StrictModel):
    locked: bool


class UserPatch(StrictModel):
    role: str | None = Field(None, pattern="^(admin|operator|viewer)$")
    disabled: bool | None = None
    password: str | None = Field(None, min_length=12, max_length=128)


class StorageSettings(StrictModel):
    retention_days: int = Field(30, ge=1, le=3650)
    min_free_mb: int = Field(256, ge=32, le=1048576)


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
    exports = ExportService(db, store, settings, bus)
    process_lock = ProcessLock(settings.data_dir)
    saved_storage = db.setting("storage")
    if saved_storage:
        storage_settings = StorageSettings(**saved_storage)
        settings.retention_days = storage_settings.retention_days
        settings.min_free_mb = storage_settings.min_free_mb

    @asynccontextmanager
    async def lifespan(app):
        try:
            process_lock.acquire()
        except BaseException:
            db.close()
            raise
        bus.start()
        try:
            store.reconcile()
            cameras.start()
            scheduler.start()
            media_plane.start()
            exports.start()
            bus.publish("system.started", "CamPiX service started")
            yield
        finally:
            await asyncio.to_thread(scheduler.stop)
            await asyncio.to_thread(exports.stop)
            await asyncio.to_thread(media_plane.stop)
            await asyncio.to_thread(cameras.stop)
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

    @app.middleware("http")
    async def boundary(request, call_next):
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
        if request.url.path.startswith(("/api", "/cam_", "/status_", "/cmd_")):
            response.headers["Cache-Control"] = "no-store"
        if settings.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def role(level="viewer"):
        def dependency(request: Request):
            return auth.require(request, level)

        return dependency

    viewer, operator, admin = role(), role("operator"), role("admin")

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
        db.execute(
            "DELETE FROM sessions WHERE token=?", (digest(request.cookies["campix_session"]),)
        )
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

    @app.get("/api/v1/cameras", dependencies=[Depends(viewer)])
    def list_cameras():
        return cameras.list()

    @app.get("/api/v1/discovery", dependencies=[Depends(admin)])
    def discovery():
        return {"devices": discover(settings.ffmpeg), "encoders": cameras.encoders}

    @app.post("/api/v1/cameras", dependencies=[Depends(admin)], status_code=201)
    def save_camera(payload: CameraConfig):
        return cameras.save(payload)

    @app.get("/api/v1/cameras/{camera_id}/config", dependencies=[Depends(admin)])
    def camera_config(camera_id: str):
        config = cameras.get(camera_id).config.model_dump()
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
        return cameras.save(CameraConfig(**{**previous, **payload.model_dump(exclude_none=True)}))

    @app.patch("/api/v1/cameras/{camera_id}/controls", dependencies=[Depends(operator)])
    def controls(camera_id: str, payload: ControlPatch):
        return cameras.controls(camera_id, payload.model_dump(exclude_none=True))

    @app.post(
        "/api/v1/cameras/{camera_id}/snapshot", dependencies=[Depends(operator)], status_code=201
    )
    def snapshot(camera_id: str):
        return cameras.get(camera_id).snapshot()

    @app.post("/api/v1/cameras/{camera_id}/recording", dependencies=[Depends(operator)])
    def start_recording(camera_id: str, payload: RecordingRequest):
        return cameras.get(camera_id).start_recording(payload.duration)

    @app.delete("/api/v1/cameras/{camera_id}/recording", dependencies=[Depends(operator)])
    def stop_recording(camera_id: str):
        return cameras.get(camera_id).stop_recording()

    @app.get("/api/v1/cameras/{camera_id}/preview.jpg", dependencies=[Depends(viewer)])
    def preview(camera_id: str):
        return Response(cameras.get(camera_id).frame()[1], media_type="image/jpeg")

    async def mjpeg(camera_id, token):
        camera = cameras.get(camera_id)
        sequence = -1
        while auth.session(token):
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

    @app.get("/api/v1/cameras/{camera_id}/stream.mjpg", dependencies=[Depends(viewer)])
    def stream(camera_id: str, request: Request):
        cameras.get(camera_id).frame()
        return StreamingResponse(
            mjpeg(camera_id, request.cookies["campix_session"]),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    @app.api_route(
        "/api/v1/streams/{camera_id}/{protocol}/{path:path}",
        methods=["GET", "POST", "PATCH", "DELETE"],
        dependencies=[Depends(viewer)],
    )
    async def media_proxy(camera_id: str, protocol: str, path: str, request: Request):
        try:
            status, content, headers = await media_plane.proxy(camera_id, protocol, path, request)
        except httpx.HTTPError:
            raise HTTPException(503, "MediaMTX unavailable; use MJPEG") from None
        return Response(content, status_code=status, headers=headers)

    @app.get("/api/v1/media", dependencies=[Depends(viewer)])
    def media(
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        kind: str | None = None,
        camera_id: str | None = None,
    ):
        return store.list(limit, offset, kind, camera_id)

    @app.get("/api/v1/media/{media_id}/file", dependencies=[Depends(viewer)])
    def media_file(media_id: str, download: bool = False):
        item = store.get(media_id)
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
    def events(limit: int = Query(50, ge=1, le=200)):
        rows = db.all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        for row in rows:
            row["data"] = json.loads(row["data"])
        return rows

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

    @app.get("/api/v1/export", dependencies=[Depends(admin)])
    def export():
        profiles = []
        for row in db.all("SELECT config FROM cameras"):
            profile = json.loads(row["config"])
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

    @app.get("/metrics", dependencies=[Depends(viewer)])
    def metrics():
        system = telemetry.read()
        lines = [
            f"campix_uptime_seconds {system['uptime_seconds']}",
            f"campix_process_memory_bytes {system['process_memory_bytes']}",
            f"campix_disk_free_bytes {system['disk_free_bytes']}",
        ]
        for camera in cameras.list():
            for metric, value in (
                ("fps", camera["fps"]),
                ("dropped_frames", camera["dropped_frames"]),
                ("online", int(camera["status"] == "online")),
            ):
                lines.append(f'campix_camera_{metric}{{camera="{camera["id"]}"}} {value}')
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
                if not auth.session(token):
                    break
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
    def primary_camera():
        items = cameras.list()
        if not items:
            raise CaptureError("No camera configured")
        return cameras.get(items[0]["id"])

    @app.get("/cam_pic.php", dependencies=[Depends(viewer)])
    @app.get("/cam_get.php", dependencies=[Depends(viewer)])
    def legacy_preview():
        return Response(primary_camera().frame()[1], media_type="image/jpeg")

    @app.get("/status_mjpeg.php", dependencies=[Depends(viewer)])
    def legacy_status():
        info = primary_camera().info()
        return Response(
            "video" if info["recording"] else "ready" if info["status"] == "online" else "halted",
            media_type="text/plain",
        )

    @app.post("/cmd_pipe.php", dependencies=[Depends(operator)])
    def legacy_command(cmd: str = Query(max_length=80)):
        camera = primary_camera()
        if cmd == "im":
            return camera.snapshot(trigger="legacy")
        if cmd == "ca 1":
            return camera.start_recording(trigger="legacy")
        if cmd == "ca 0":
            return camera.stop_recording()
        raise HTTPException(400, "Only im, ca 1 and ca 0 are supported by this migration facade")

    web_root = settings.resolved_web_root()
    if not (web_root / "index.html").is_file():
        raise RuntimeError(
            "Web assets missing; install the built CamPiX wheel or set CAMPIX_WEB_ROOT"
        )
    app.mount("/", StaticFiles(directory=web_root, html=True), name="web")
    return app
