from __future__ import annotations

import io
import json
import logging
import threading
import time
from collections import deque

import numpy as np
from PIL import Image

from .capture import CaptureError, make_backend
from .encoding import EncoderUnavailable, select_encoder
from .models import CameraConfig
from .recording import Recorder, encoder_probe

LOG = logging.getLogger(__name__)


class Camera:
    def __init__(self, config, settings, store, bus, encoders):
        self.config, self.settings, self.store, self.bus = config, settings, store, bus
        self.encoders = encoders
        self.encoding_selection = None
        self.encoding_error = None
        self.encoding_ready = threading.Event()
        self.encoder_thread = threading.Thread(
            target=self._prepare_encoder, name=f"encoder-probe-{config.id}", daemon=True
        )
        self.backend = None
        self.condition = threading.Condition(threading.RLock())
        self.operation_lock = threading.RLock()
        self.halt = threading.Event()
        self.jpeg = None
        self.sequence = 0
        self.status = "stopped"
        self.error = None
        self.frame_times = deque(maxlen=120)
        self.processing_times = deque(maxlen=120)
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

    def prepare_encoding(self):
        self.encoder_thread.start()

    def _prepare_encoder(self):
        try:
            selection = select_encoder(
                self.settings.ffmpeg,
                self.config.encoder,
                width=self.config.width,
                height=self.config.height,
                fps=self.config.fps,
                capabilities=self.encoders,
                cancel=self.halt,
            )
            with self.condition:
                if self.halt.is_set():
                    return
                self.encoding_selection = selection
        except EncoderUnavailable as exc:
            if not self.halt.is_set():
                self.encoding_error = str(exc)
                self.bus.publish("encoding.unavailable", self.encoding_error, self.config.id)
        finally:
            self.encoding_ready.set()

    def _capture(self):
        self.prepare_encoding()
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
                    jpeg = backend.read_jpeg()
                    native_jpeg = jpeg is not None
                    image = None if native_jpeg else backend.read()
                    processing_started = time.perf_counter()
                    if self.halt.is_set():
                        break
                    if native_jpeg:
                        # Reading the JPEG header does not decode pixels. The adapter's
                        # correctly sized bytes can feed preview, encoding and pre-roll.
                        with Image.open(io.BytesIO(jpeg)) as encoded:
                            if encoded.format != "JPEG":
                                raise CaptureError("Capture adapter returned an invalid JPEG")
                            if encoded.size != (self.config.width, self.config.height):
                                image = encoded.convert("RGB")
                    if image is not None:
                        if image.size != (self.config.width, self.config.height):
                            image = image.resize((self.config.width, self.config.height))
                        preview = io.BytesIO()
                        image.save(preview, "JPEG", quality=self.config.quality)
                        jpeg = preview.getvalue()
                    now = time.monotonic()
                    frame_time = getattr(backend, "frame_received_at", now)
                    if now - frame_time > 5:
                        raise CaptureError("Capture processing cannot keep up with the live source")
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
                        self.frame_times.append(frame_time)
                        self.processing_times.append(time.perf_counter() - processing_started)
                        # One immutable latest-frame reference bounds analysis memory.
                        # Decode and resize belong exclusively to the analysis worker.
                        self.analysis = jpeg if self.config.motion_enabled else None
                        self.analysis_sequence = self.sequence
                        self.preroll.append((frame_time, jpeg))
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
                    # JPEG pipe adapters already sample at the profile rate and block
                    # for their next arrival; a second timer can discard that cadence.
                    if not native_jpeg:
                        self.halt.wait(max(0, 1 / self.config.fps - (time.monotonic() - started)))
            except Exception as exc:
                if self.halt.is_set():
                    break
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
                    self.processing_times.clear()
                    self.condition.notify_all()
            finally:
                try:
                    backend.close()
                except Exception:
                    LOG.warning("Camera %s close failed", self.config.id)
            self.halt.wait(min(30, 2 ** min(failures, 5)))
        self.status = "stopped"

    def _motion_gray(self, jpeg):
        with Image.open(io.BytesIO(jpeg)) as image:
            # JPEG draft downsamples during decoding, avoiding a full-resolution RGB
            # allocation for a 160x90 analysis sample, including on 4K sources.
            image.draft("L", (160, 90))
            return np.array(image.convert("L").resize((160, 90)))

    def _motion(self):
        previous, sequence, active_frames = None, 0, 0
        last_motion = 0
        while not self.halt.wait(0.2):
            try:
                with self.condition:
                    jpeg, current = self.analysis, self.analysis_sequence
                    if (
                        self.status == "online"
                        and self.frame_times
                        and time.monotonic() - self.frame_times[-1] > 5
                    ):
                        self.status = "stalled"
                        self.error = "Frame delivery stalled; waiting for the camera driver"
                        self.bus.publish("camera.stalled", self.error, self.config.id)
                if self.config.motion_enabled and jpeg is not None and current != sequence:
                    gray = self._motion_gray(jpeg)
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
                elif not self.config.motion_enabled or jpeg is None:
                    previous = None
                    active_frames = 0
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
            if not self.encoding_ready.is_set():
                raise CaptureError("Encoder profile is still being checked; retry shortly")
            selection = self.encoding_selection
            if not selection:
                raise CaptureError(
                    self.encoding_error or "No usable H.264 encoder for this profile"
                )
            encoder = selection.codec
            with self.condition:
                recorder = Recorder(
                    self.config,
                    self.settings,
                    self.store,
                    [f for _, f in self.preroll],
                    trigger,
                    duration,
                    selection=selection,
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
                "capture_dropped_frames": getattr(self.backend, "dropped_frames", 0),
                "capture_processing_ms": round(
                    sum(self.processing_times) / len(self.processing_times) * 1000, 3
                )
                if self.processing_times
                else None,
                "preroll_bytes": self.preroll_bytes,
                "motion_enabled": self.config.motion_enabled,
                "motion_threshold": self.config.motion_threshold,
                "controls": self.config.controls.copy(),
                "capabilities": self.backend.capabilities.copy()
                if self.backend
                else {"controls": [], "test_status": "unverified"},
                "encoding": {
                    **self.encoders,
                    "requested": self.config.encoder,
                    "preparing": not self.encoding_ready.is_set() and self.config.enabled,
                    "selected": self.encoding_selection.codec if self.encoding_selection else None,
                    "selected_hardware": bool(
                        self.encoding_selection and self.encoding_selection.hardware
                    ),
                    "profile_tested": bool(self.encoding_selection),
                    "fallback_reason": self.encoding_selection.fallback_reason
                    if self.encoding_selection
                    else None,
                    "error": self.encoding_error,
                },
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
        if self.encoder_thread.is_alive():
            self.encoder_thread.join(timeout=15)
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

    def _stop_for_update(self, camera):
        camera.stop()
        if (
            camera.capture_thread.is_alive()
            or camera.motion_thread.is_alive()
            or camera.encoder_thread.is_alive()
        ):
            message = "Camera worker did not stop; retry after the driver releases the device"
            camera.status, camera.error = "error", message
            self.bus.publish("camera.stop_failed", message, camera.config.id)
            raise CaptureError(message)

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
                self._stop_for_update(previous)
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
            self._stop_for_update(camera)
            del self.cameras[camera_id]
            self.db.execute("DELETE FROM cameras WHERE id=?", (camera_id,))
            self.db.execute(
                "DELETE FROM schedules WHERE json_extract(config,'$.camera_id')=?", (camera_id,)
            )
            self.bus.publish("camera.removed", "Camera removed", camera_id)

    def controls(self, camera_id, values):
        with self.lock:
            camera = self.get(camera_id)
            camera.apply_controls(values)
            self.db.execute(
                "UPDATE cameras SET config=? WHERE id=?",
                (camera.config.model_dump_json(), camera_id),
            )
            return camera.info()

    def stop(self):
        for camera in list(self.cameras.values()):
            camera.stop()
