from __future__ import annotations

import io
import json
import logging
import threading
import time
from collections import deque

import numpy as np

from .capture import CaptureError, make_backend
from .models import CameraConfig
from .recording import Recorder, encoder_probe

LOG = logging.getLogger(__name__)


class Camera:
    def __init__(self, config, settings, store, bus, encoders):
        self.config, self.settings, self.store, self.bus = config, settings, store, bus
        self.encoders = encoders
        self.backend = None
        self.condition = threading.Condition(threading.RLock())
        self.operation_lock = threading.RLock()
        self.halt = threading.Event()
        self.jpeg = None
        self.sequence = 0
        self.status = "stopped"
        self.error = None
        self.frame_times = deque(maxlen=120)
        self.preroll = deque()
        self.preroll_bytes = 0
        self.analysis = None
        self.analysis_sequence = 0
        self.recorder = None
        self.capture_thread = threading.Thread(
            target=self._capture, name=f"capture-{config.id}", daemon=True
        )
        self.motion_thread = threading.Thread(
            target=self._motion, name=f"motion-{config.id}", daemon=True
        )

    def start(self):
        if self.config.enabled:
            self.capture_thread.start()
            self.motion_thread.start()

    def _capture(self):
        failures = 0
        while not self.halt.is_set():
            backend = make_backend(self.config, self.settings.ffmpeg)
            self.backend = backend
            self.status = "connecting"
            try:
                backend.open()
                self.error = None
                while not self.halt.is_set():
                    started = time.monotonic()
                    image = backend.read()
                    if image.size != (self.config.width, self.config.height):
                        image = image.resize((self.config.width, self.config.height))
                    preview = io.BytesIO()
                    image.save(preview, "JPEG", quality=self.config.quality)
                    jpeg = preview.getvalue()
                    now = time.monotonic()
                    # Analysis works on a small grayscale copy, outside the capture worker.
                    gray = np.array(image.resize((160, 90)).convert("L"))
                    with self.condition:
                        if self.status != "online":
                            self.status, self.error = "online", None
                            self.bus.publish(
                                "camera.online",
                                "Camera connected",
                                self.config.id,
                                {"backend": self.config.backend},
                            )
                        self.jpeg = jpeg
                        self.sequence += 1
                        self.frame_times.append(now)
                        self.analysis, self.analysis_sequence = gray, self.sequence
                        self.preroll.append((now, jpeg))
                        self.preroll_bytes += len(jpeg)
                        while self.preroll and (
                            now - self.preroll[0][0] > self.config.preroll_seconds
                            or self.preroll_bytes > 32 * 1024 * 1024
                        ):
                            _, old = self.preroll.popleft()
                            self.preroll_bytes -= len(old)
                        if self.recorder:
                            self.recorder.offer(jpeg)
                        self.condition.notify_all()
                    failures = 0
                    self.halt.wait(max(0, 1 / self.config.fps - (time.monotonic() - started)))
            except Exception as exc:
                self.status = "error"
                self.error = f"Capture unavailable ({type(exc).__name__}); retrying"
                self.bus.publish("camera.error", self.error, self.config.id)
                LOG.warning("Camera %s capture failed: %s", self.config.id, type(exc).__name__)
                failures += 1
                with self.condition:
                    self.jpeg, self.analysis = None, None
                    self.preroll.clear()
                    self.preroll_bytes = 0
                    self.frame_times.clear()
                    self.condition.notify_all()
            finally:
                try:
                    backend.close()
                except Exception:
                    LOG.warning("Camera %s close failed", self.config.id)
            self.halt.wait(min(30, 2 ** min(failures, 5)))
        self.status = "stopped"

    def _motion(self):
        previous, sequence, active_frames = None, 0, 0
        last_motion = 0
        while not self.halt.wait(0.2):
            try:
                with self.condition:
                    gray, current = self.analysis, self.analysis_sequence
                    if (
                        self.status == "online"
                        and self.frame_times
                        and time.monotonic() - self.frame_times[-1] > 5
                    ):
                        self.status = "stalled"
                        self.error = "Frame delivery stalled; waiting for the camera driver"
                        self.bus.publish("camera.stalled", self.error, self.config.id)
                if self.config.motion_enabled and gray is not None and current != sequence:
                    sequence = current
                    if previous is not None:
                        score = float(
                            np.mean(
                                np.abs(gray.astype(np.int16) - previous.astype(np.int16))
                                > self.config.motion_pixel_delta
                            )
                        )
                        active_frames = (
                            active_frames + 1 if score >= self.config.motion_threshold else 0
                        )
                        if active_frames >= self.config.motion_frames:
                            now = time.monotonic()
                            if now - last_motion > 2:
                                self.bus.publish(
                                    "motion.detected",
                                    "Motion detected",
                                    self.config.id,
                                    {"score": round(score, 4)},
                                )
                            last_motion = now
                            with self.operation_lock:
                                if self.recorder is None:
                                    self.start_recording(
                                        trigger="motion", duration=self.config.motion_postroll
                                    )
                                elif self.recorder.trigger == "motion":
                                    self.recorder.deadline = now + self.config.motion_postroll
                            active_frames = 0
                    previous = gray
                else:
                    previous = None
                with self.operation_lock:
                    recorder = self.recorder
                    if recorder and (
                        recorder.done.is_set()
                        or recorder.deadline
                        and time.monotonic() >= recorder.deadline
                    ):
                        try:
                            recorder.stop()
                        finally:
                            with self.condition:
                                self.recorder = None
            except Exception as exc:
                LOG.warning(
                    "Camera %s analysis/recording failed: %s", self.config.id, type(exc).__name__
                )
                self.halt.wait(2)

    def frame(self, sequence=None, timeout=10):
        with self.condition:
            self.condition.wait_for(
                lambda: (
                    self.halt.is_set()
                    or self.jpeg is not None
                    and (sequence is None or self.sequence != sequence)
                ),
                timeout=timeout,
            )
            if (
                self.jpeg is None
                or self.status != "online"
                or not self.frame_times
                or time.monotonic() - self.frame_times[-1] > 5
            ):
                raise CaptureError("No current frame available")
            return self.sequence, self.jpeg

    def snapshot(self, trigger="manual"):
        _, jpeg = self.frame()
        return self.store.snapshot(self.config.id, jpeg, trigger)

    def start_recording(self, duration=None, trigger="manual"):
        with self.operation_lock:
            if self.recorder:
                raise CaptureError("A recording is already in progress")
            self.frame()
            encoder = "libx264" if self.config.encoder == "auto" else self.config.encoder
            if encoder not in self.encoders["encoders"]:
                raise CaptureError("Requested encoder is unavailable; install FFmpeg with libx264")
            with self.condition:
                recorder = Recorder(
                    self.config,
                    self.settings,
                    self.store,
                    [f for _, f in self.preroll],
                    trigger,
                    duration,
                )
                self.recorder = recorder
            self.bus.publish(
                "recording.started",
                "Recording started",
                self.config.id,
                {"trigger": trigger, "encoder": encoder},
            )
            return {"recording": True, "media_id": recorder.media_id}

    def stop_recording(self):
        with self.operation_lock:
            with self.condition:
                recorder, self.recorder = self.recorder, None
            if not recorder:
                raise CaptureError("No recording is active")
            result = recorder.stop()
            self.bus.publish("recording.stopped", "Recording finalized", self.config.id)
            return result

    def apply_controls(self, values):
        with self.operation_lock:
            if self.status != "online" or not self.backend:
                raise CaptureError("Camera is offline")
            self.backend.apply(values)
            self.config.controls.update(values)

    def info(self):
        with self.condition:
            times = list(self.frame_times)
            recorder = self.recorder
            frame_age = time.monotonic() - times[-1] if times else None
            info = {
                "id": self.config.id,
                "name": self.config.name,
                "backend": self.config.backend,
                "status": self.status,
                "error": self.error,
                "width": self.config.width,
                "height": self.config.height,
                "fps_target": self.config.fps,
                "fps": round((len(times) - 1) / (times[-1] - times[0]), 2)
                if len(times) > 1 and times[-1] > times[0] and frame_age < 5
                else 0,
                "frame_age_seconds": round(frame_age, 2) if frame_age is not None else None,
                "recording": bool(recorder and not recorder.done.is_set()),
                "dropped_frames": recorder.dropped if recorder else 0,
                "preroll_bytes": self.preroll_bytes,
                "motion_enabled": self.config.motion_enabled,
                "motion_threshold": self.config.motion_threshold,
                "controls": self.config.controls.copy(),
                "capabilities": self.backend.capabilities.copy()
                if self.backend
                else {"controls": [], "test_status": "unverified"},
                "encoding": self.encoders,
            }
        info["preroll_seconds"] = self.config.preroll_seconds
        return info

    def stop(self):
        self.halt.set()
        with self.condition:
            self.condition.notify_all()
        if self.backend:
            try:
                self.backend.close()
            except Exception:
                pass
        if self.capture_thread.is_alive():
            self.capture_thread.join(timeout=10)
        if self.motion_thread.is_alive():
            self.motion_thread.join(timeout=30)
        if self.recorder:
            try:
                self.stop_recording()
            except RuntimeError:
                pass


class CameraManager:
    def __init__(self, db, settings, store, bus):
        self.db, self.settings, self.store, self.bus = db, settings, store, bus
        self.cameras = {}
        self.lock = threading.RLock()
        self.encoders = encoder_probe(settings.ffmpeg)

    def start(self):
        with self.lock:
            configs = [
                CameraConfig(**json.loads(row["config"]))
                for row in self.db.all("SELECT config FROM cameras")
            ]
            if self.settings.demo and not configs:
                configs = [
                    CameraConfig(
                        id="demo",
                        name="Synthetic test camera",
                        backend="synthetic",
                        source="0",
                        width=640,
                        height=360,
                        fps=10,
                    )
                ]
                self.save(configs[0])
            for config in configs:
                if config.backend == "synthetic" and not self.settings.demo:
                    config.enabled = False
                    self.bus.publish(
                        "camera.disabled",
                        "Synthetic source disabled outside explicit demo mode",
                        config.id,
                    )
                if config.id not in self.cameras:
                    camera = Camera(config, self.settings, self.store, self.bus, self.encoders)
                    self.cameras[config.id] = camera
                    camera.start()

    def get(self, camera_id):
        with self.lock:
            if camera_id not in self.cameras:
                raise KeyError(camera_id)
            return self.cameras[camera_id]

    def list(self):
        with self.lock:
            return [camera.info() for camera in self.cameras.values()]

    def save(self, config):
        with self.lock:
            if config.backend == "synthetic" and not self.settings.demo:
                raise CaptureError("Synthetic cameras require explicit --demo mode")
            if config.id not in self.cameras and len(self.cameras) >= self.settings.max_cameras:
                raise CaptureError("Camera limit reached")
            if any(
                c.config.id != config.id
                and c.config.backend == config.backend
                and c.config.source == config.source
                for c in self.cameras.values()
            ):
                raise CaptureError("This capture source is already configured")
            previous = self.cameras.get(config.id)
            if previous:
                if previous.info()["recording"]:
                    raise CaptureError("Stop recording before changing the camera profile")
                same_device = (previous.config.backend, previous.config.source) == (
                    config.backend,
                    config.source,
                )
                modes = previous.backend.capabilities.get("modes") if previous.backend else None
                # Accept only reported native sizes: anything larger would be upscaled at a
                # high CPU cost without adding detail. The running profile stays untouched.
                if same_device and modes and f"{config.width}x{config.height}" not in modes:
                    raise CaptureError("This camera captures only " + ", ".join(modes))
                previous.stop()
            self.db.execute(
                "INSERT INTO cameras VALUES (?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config",
                (config.id, config.model_dump_json()),
            )
            camera = Camera(config, self.settings, self.store, self.bus, self.encoders)
            self.cameras[config.id] = camera
            camera.start()
            self.bus.publish("camera.configured", "Camera profile saved", config.id)
            return camera.info()

    def remove(self, camera_id):
        with self.lock:
            camera = self.get(camera_id)
            if camera.info()["recording"]:
                raise CaptureError("Stop recording before removing a camera")
            camera.stop()
            del self.cameras[camera_id]
            self.db.execute("DELETE FROM cameras WHERE id=?", (camera_id,))
            self.db.execute(
                "DELETE FROM schedules WHERE json_extract(config,'$.camera_id')=?", (camera_id,)
            )
            self.bus.publish("camera.removed", "Camera removed", camera_id)

    def controls(self, camera_id, values):
        camera = self.get(camera_id)
        camera.apply_controls(values)
        self.db.execute(
            "UPDATE cameras SET config=? WHERE id=?", (camera.config.model_dump_json(), camera_id)
        )
        return camera.info()

    def stop(self):
        for camera in list(self.cameras.values()):
            camera.stop()
