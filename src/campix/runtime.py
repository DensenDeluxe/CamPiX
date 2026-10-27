from __future__ import annotations

import io
import json
import logging
import os
import queue
import threading
import time
from collections import deque
from concurrent.futures import Future, TimeoutError
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image

from .audio import AudioCapture, AudioRecording
from .budget import admission_reason, shed_level_for
from .capture import CaptureError, make_backend
from .encoded import AnnexBParser, EncodedHub, h264_dimensions
from .encoding import EncoderSelection, EncoderUnavailable, jpeg_dimensions, select_encoder
from .models import CameraConfig
from .motion import MASK_CELLS, BackgroundModel
from .overlay import stamp_jpeg
from .recording import EncodedRecorder, encoder_probe
from .shared_encoder import SharedEncoder

LOG = logging.getLogger(__name__)
JPEG_PREROLL_BYTES = 32 * 1024 * 1024


def trim_jpeg_history(frames, now, seconds, hub_ready, max_bytes=JPEG_PREROLL_BYTES):
    """Keep the newest JPEG. Drop history once the encoded hub has a keyframe."""
    total = sum(len(payload) for _, payload in frames)
    while len(frames) > 1 and (hub_ready or now - frames[0][0] > seconds or total > max_bytes):
        total -= len(frames.popleft()[1])
    return total


def downscale_jpeg(payload: bytes, max_width: int = 960, max_height: int = 540) -> bytes:
    """Build one small baseline JPEG for the live fallback. Callers stay off the capture thread."""
    with Image.open(io.BytesIO(payload)) as image:
        image.draft("RGB", (max_width, max_height))
        frame = image.convert("RGB")
        frame.thumbnail((max_width, max_height))
        width, height = frame.size
        even = (max(2, width - width % 2), max(2, height - height % 2))
        if even != frame.size:
            frame = frame.resize(even)
        out = io.BytesIO()
        frame.save(out, "JPEG", quality=70)
        return out.getvalue()


def motion_score(previous, gray, pixel_delta: int, regions, cells: str = "") -> float:
    """Fraction of changed samples. Retained for callers that compare two frames directly."""
    from .motion import motion_score as score_frames

    return score_frames(previous, gray, pixel_delta, regions, cells)


def host_load_ratio() -> float | None:
    try:
        load = float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return load / max(1, os.cpu_count() or 1)


def host_temperature() -> float | None:
    thermal = Path("/sys/class/thermal/thermal_zone0/temp")
    if not thermal.is_file():
        return None
    try:
        return float(thermal.read_text()) / 1000
    except (OSError, ValueError):
        return None


class Camera:
    def __init__(self, config, settings, store, bus, encoders):
        self.config, self.settings, self.store, self.bus = config, settings, store, bus
        self.encoders = encoders
        self.encoding_selection = None
        self.encoding_error = None
        self.encoding_ready = threading.Event()
        self.backend_ready = threading.Event()
        self.encoding_lock = threading.RLock()
        self.encoded_hub = EncodedHub(max_seconds=config.preroll_seconds)
        self.shared_encoder = None
        self.native_disabled = False
        self.native_retry_at = 0.0
        self.native_fallback_reason = None
        self.snapshot_requests = queue.Queue(maxsize=4)
        self.stopping = False
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
        self.live_jpeg = None
        self.live_preview_at = 0.0
        self.shed_level = 0
        self.shed_checked = 0.0
        self.analysis = None
        self.analysis_sequence = 0
        self.motion_score = 0.0
        self.motion_cells = "0" * MASK_CELLS
        self.recorder = None
        self.audio = None
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
            if self.config.audio_enabled:
                self.audio = AudioCapture(self.config, self.settings, self.bus)
                self.audio.start()

    def prepare_encoding(self, *, require_backend=False):
        self.require_encoding_backend = require_backend
        self.encoder_thread.start()

    def _prepare_encoder(self):
        # Standalone preparation remains useful for diagnostics. Live cameras always
        # wait for their actual source before choosing a compressed-input profile.
        if not self.require_encoding_backend:
            self._select_encoding()
            self.encoding_ready.set()
            return
        while not self.halt.is_set():
            if not self.backend_ready.wait(0.2):
                continue
            backend = self.backend
            epoch = self.encoded_hub.epoch
            try:
                if backend.native_encoded:
                    capabilities = backend.capabilities
                    selection = EncoderSelection(
                        codec=capabilities["encoder_implementation"],
                        hardware=capabilities["native_encoder_hardware"],
                        requested=self.config.encoder,
                        output_args=(),
                        probe_verified=False,
                        zero_copy_raw_frames=capabilities["capture_to_encoder_zero_copy"],
                        input_codec="h264",
                    )
                else:
                    _, sample = self.frame(timeout=3)
                    selection = self._select_encoding(sample, adopt=False)
                with self.encoding_lock:
                    if (
                        self.halt.is_set()
                        or backend is not self.backend
                        or not self.backend_ready.is_set()
                        or epoch != self.encoded_hub.epoch
                    ):
                        continue
                    if selection:
                        self.encoding_selection = selection
                        self.encoding_error = None
                        if not backend.native_encoded:
                            self.shared_encoder = SharedEncoder(self, selection)
                            self.shared_encoder.start()
                    self.encoding_ready.set()
            except CaptureError:
                if self.halt.wait(0.2):
                    break
                continue
            except EncoderUnavailable as exc:
                with self.encoding_lock:
                    if (
                        not self.halt.is_set()
                        and backend is self.backend
                        and self.backend_ready.is_set()
                        and epoch == self.encoded_hub.epoch
                    ):
                        self.encoding_error = str(exc)
                        self.bus.publish(
                            "encoding.unavailable", self.encoding_error, self.config.id
                        )
                        self.encoding_ready.set()
            while (
                not self.halt.wait(0.2) and backend is self.backend and self.backend_ready.is_set()
            ):
                pass
        self.encoding_ready.set()

    def _select_encoding(self, sample=None, *, adopt=True):
        try:
            selection = select_encoder(
                self.settings.ffmpeg,
                self.config.encoder,
                width=self.config.width,
                height=self.config.height,
                fps=self.config.fps,
                capabilities=self.encoders,
                cancel=self.halt,
                input_codec="mjpeg" if sample is not None else None,
                input_sample=sample,
            )
            with self.condition:
                if self.halt.is_set():
                    return
                if adopt:
                    self.encoding_selection = selection
                    self.encoding_error = None
                return selection
        except EncoderUnavailable as exc:
            if not adopt:
                raise
            if not self.halt.is_set():
                self.encoding_error = str(exc)
                self.bus.publish("encoding.unavailable", self.encoding_error, self.config.id)

    def _native_sink(self, backend, epoch):
        parser, timestamps = AnnexBParser(), deque()
        lock = threading.Lock()
        sensor_origin = None
        arrival_origin = None
        validated_config = None

        def offer(data, timestamp_us=None, keyframe=None, frame_complete=False):
            nonlocal sensor_origin, arrival_origin, validated_config
            with lock:
                if (
                    self.halt.is_set()
                    or backend is not self.backend
                    or epoch != self.encoded_hub.epoch
                ):
                    return
                now = time.monotonic()
                if timestamp_us is not None and data.startswith(
                    (b"\x00\x00\x00\x01\x09", b"\x00\x00\x01\x09")
                ):
                    if sensor_origin is None:
                        sensor_origin, arrival_origin = timestamp_us, now
                    timestamps.append(arrival_origin + (timestamp_us - sensor_origin) / 1_000_000)
                    if len(timestamps) > 240:
                        raise CaptureError("Native encoder exceeded its timestamp bound")
                packets = parser.feed(data)
                if frame_complete:
                    packets.extend(parser.end_access_unit())
                for packet in packets:
                    if packet.decoder_config != validated_config:
                        if h264_dimensions(packet.decoder_config) != (
                            self.config.width,
                            self.config.height,
                        ):
                            raise CaptureError(
                                "Native H.264 dimensions do not match the camera profile"
                            )
                        validated_config = packet.decoder_config
                    captured = timestamps.popleft() if timestamps else now
                    if captured > now + 2 or now - captured > 5:
                        raise CaptureError("Native H.264 clock is outside the live capture window")
                    self.encoded_hub.publish(packet, captured, epoch=epoch)

        return offer

    def _reset_encoding(self, backend):
        with self.encoding_lock:
            if backend is not self.backend:
                return
            self.backend_ready.clear()
            if self.shared_encoder:
                self.shared_encoder.stop()
                if self.shared_encoder.thread.is_alive():
                    raise CaptureError("Shared encoder worker did not stop")
            if not self.halt.is_set():
                self.encoding_selection = None
                self.encoding_ready.clear()
                self.encoded_hub.restart()

    def _capture(self):
        self.prepare_encoding(require_backend=True)
        failures = 0
        while not self.halt.is_set():
            backend = make_backend(self.config, self.settings.ffmpeg)
            self.backend = backend
            backend.native_disabled = (
                self.native_disabled and time.monotonic() < self.native_retry_at
            )
            backend.set_encoded_sink(self._native_sink(backend, self.encoded_hub.epoch))
            self.status = "connecting"
            try:
                backend.open()
                if backend.native_encoded:
                    self.native_fallback_reason = None
                self.backend_ready.set()
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
                        # A SOF header check does not decode pixels. Pillow runs only when
                        # the frame is not a recognizable JPEG of the expected size.
                        expected = (
                            tuple(backend.preview_size)
                            if backend.native_encoded
                            else (self.config.width, self.config.height)
                        )
                        size = jpeg_dimensions(jpeg)
                        if size is None:
                            with Image.open(io.BytesIO(jpeg)) as encoded:
                                if encoded.format != "JPEG":
                                    raise CaptureError("Capture adapter returned an invalid JPEG")
                                size = encoded.size
                                if size != expected:
                                    image = encoded.convert("RGB")
                        elif size != expected:
                            with Image.open(io.BytesIO(jpeg)) as encoded:
                                image = encoded.convert("RGB")
                    if image is not None:
                        expected = (
                            tuple(backend.preview_size)
                            if backend.native_encoded
                            else (self.config.width, self.config.height)
                        )
                        if image.size != expected:
                            image = image.resize(expected)
                        preview = io.BytesIO()
                        image.save(preview, "JPEG", quality=self.config.quality)
                        jpeg = preview.getvalue()
                    now = time.monotonic()
                    hub_ready = bool(self.encoded_hub.stats().get("ready"))
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
                        self.preroll_bytes = trim_jpeg_history(
                            self.preroll, now, self.config.preroll_seconds, hub_ready
                        )
                        self.condition.notify_all()
                    self._save_snapshots(backend, jpeg)
                    failures = 0
                    # JPEG pipe adapters already sample at the profile rate and block
                    # for their next arrival; a second timer can discard that cadence.
                    if not native_jpeg:
                        self._pace_capture(
                            max(0, 1 / self.config.fps - (time.monotonic() - started))
                        )
            except Exception as exc:
                if self.halt.is_set():
                    break
                self.status = "error"
                self.error = f"Capture unavailable ({type(exc).__name__}); retrying"
                self.bus.publish("camera.error", self.error, self.config.id)
                LOG.warning("Camera %s capture failed: %s", self.config.id, type(exc).__name__)
                failures += 1
                if backend.native_attempted:
                    self.native_disabled = True
                    self.native_retry_at = time.monotonic() + 30
                    self.native_fallback_reason = (
                        "Native compressed capture failed; using JPEG capture"
                    )
                    self.bus.publish(
                        "encoding.fallback",
                        "Native capture failed; retrying the JPEG capture path",
                        self.config.id,
                    )
                self._fail_snapshots()
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
                try:
                    self._reset_encoding(backend)
                except CaptureError as exc:
                    self.status, self.error = "error", str(exc)
                    self.bus.publish("camera.stop_failed", self.error, self.config.id)
                    return
            self.halt.wait(min(30, 2 ** min(failures, 5)))
        self.status = "stopped"

    def _pace_capture(self, delay):
        if os.name != "nt" or delay >= 0.05:
            self.halt.wait(delay)
            return
        # Windows Event.wait rounds short waits to the scheduler tick. Python's
        # high-resolution sleep avoids halving a 60 FPS source's cadence, while
        # these small intervals retain bounded shutdown cancellation.
        deadline = time.perf_counter() + delay
        while not self.halt.is_set():
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.005))

    def _motion_gray(self, jpeg):
        with Image.open(io.BytesIO(jpeg)) as image:
            # JPEG draft downsamples during decoding, avoiding a full-resolution RGB
            # allocation for a 160x90 analysis sample, including on 4K sources.
            # Native preview JPEGs are already small; draft keeps that path cheap.
            image.draft("L", (160, 90))
            return np.array(image.convert("L").resize((160, 90)))

    def _update_live_preview(self, jpeg):
        now = time.monotonic()
        if now - self.live_preview_at < 0.5:
            return
        size = jpeg_dimensions(jpeg)
        try:
            preview = jpeg if size and size[0] <= 960 and size[1] <= 540 else downscale_jpeg(jpeg)
            if self.config.timestamp_overlay:
                preview = stamp_jpeg(preview, datetime.now(UTC))
        except (OSError, ValueError):
            return
        with self.condition:
            self.live_jpeg = preview
            self.live_preview_at = now

    def _refresh_shed(self):
        now = time.monotonic()
        if now - self.shed_checked < 2:
            return
        self.shed_checked = now
        # Explicit demo mode skips load-average shedding so synthetic tests stay timed
        # by the profile. Temperature still applies on every host.
        load = None if self.settings.demo else host_load_ratio()
        level = shed_level_for(host_temperature(), load)
        if level != self.shed_level:
            self.shed_level = level
            self.bus.publish(
                "camera.load",
                f"Capture load shed level {level}",
                self.config.id,
                {"level": level},
            )

    def _motion(self):
        sequence, active_frames = 0, 0
        last_motion = 0
        model = BackgroundModel()
        signature = None
        while True:
            self._refresh_shed()
            interval = 0.2 if self.shed_level < 1 else 0.5 if self.shed_level == 1 else 1
            if self.halt.wait(interval):
                break
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
                if jpeg is not None and current != sequence:
                    self._update_live_preview(jpeg)
                if self.config.motion_enabled and jpeg is not None and current != sequence:
                    gray = self._motion_gray(jpeg)
                    sequence = current
                    current_signature = (
                        self.config.motion_pixel_delta,
                        self.config.motion_mask,
                        tuple(
                            (region.x, region.y, region.w, region.h)
                            for region in self.config.motion_regions
                        ),
                    )
                    if current_signature != signature:
                        model.reset()
                        signature = current_signature
                    score = model.observe(
                        gray,
                        self.config.motion_pixel_delta,
                        self.config.motion_regions,
                        self.config.motion_mask,
                    )
                    with self.condition:
                        self.motion_score = round(score, 4)
                        self.motion_cells = model.last_cells
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
                elif not self.config.motion_enabled or jpeg is None:
                    model.reset()
                    signature = None
                    active_frames = 0
                    with self.condition:
                        self.motion_score = 0.0
                        self.motion_cells = model.last_cells
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

    def audio_wav(self, after=0):
        if not self.config.audio_enabled or self.audio is None:
            raise CaptureError("Audio is off for this camera")
        return self.audio.next_wav(after)

    def live_frame(self):
        """Small JPEG for the browser fallback. Full frame until the first preview exists."""
        with self.condition:
            if self.live_jpeg and time.monotonic() - self.live_preview_at < 2:
                return self.live_jpeg
        return self.frame()[1]

    def frame(self, sequence=None, timeout=10):
        current, jpeg, _ = self.frame_packet(sequence, timeout)
        return current, jpeg

    def frame_packet(self, sequence=None, timeout=10):
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
                self.halt.is_set()
                or self.stopping
                or self.jpeg is None
                or self.status != "online"
                or not self.frame_times
                or time.monotonic() - self.frame_times[-1] > 5
            ):
                raise CaptureError("No current frame available")
            return self.sequence, self.jpeg, self.frame_times[-1]

    def _save_snapshots(self, backend, jpeg):
        while True:
            try:
                request = self.snapshot_requests.get_nowait()
            except queue.Empty:
                return
            if not request.set_running_or_notify_cancel():
                continue
            try:
                photo = backend.snapshot_jpeg()
                if photo is None:
                    if backend.native_encoded:
                        raise CaptureError("A full-resolution camera photo is unavailable")
                    photo = jpeg
                if len(photo) > 16 * 1024 * 1024:
                    raise CaptureError("Camera photo exceeds the supported size")
                with Image.open(io.BytesIO(photo)) as image:
                    if image.format != "JPEG" or image.size != (
                        self.config.width,
                        self.config.height,
                    ):
                        raise CaptureError("Camera photo does not match its capture profile")
                    image.load()
                request.set_result(photo)
            except Exception as exc:
                request.set_exception(
                    CaptureError(f"Camera photo unavailable ({type(exc).__name__})")
                )

    def _fail_snapshots(self):
        while True:
            try:
                request = self.snapshot_requests.get_nowait()
            except queue.Empty:
                return
            if request.set_running_or_notify_cancel():
                request.set_exception(CaptureError("Camera is unavailable"))

    def snapshot(self, trigger="manual"):
        self.frame()
        request = Future()
        with self.operation_lock:
            if self.halt.is_set() or self.stopping:
                raise CaptureError("Camera is stopping")
            try:
                self.snapshot_requests.put_nowait(request)
            except queue.Full:
                raise CaptureError("Camera photo queue is full; retry shortly") from None
        try:
            jpeg = request.result(timeout=8)
        except TimeoutError:
            request.cancel()
            raise CaptureError("Camera photo did not arrive in time") from None
        if self.config.timestamp_overlay:
            try:
                jpeg = stamp_jpeg(jpeg, datetime.now(UTC))
            except (OSError, ValueError) as exc:
                raise CaptureError("Timestamp overlay could not be applied") from exc
        return self.store.snapshot(self.config.id, jpeg, trigger)

    def start_recording(self, duration=None, trigger="manual"):
        with self.operation_lock:
            if self.stopping:
                raise CaptureError("Camera is stopping")
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
            stats = self.encoded_hub.stats()
            if (
                not stats["ready"]
                or stats["last_frame_age_seconds"] is None
                or stats["last_frame_age_seconds"] > 5
            ):
                raise CaptureError("Shared H.264 stream is still preparing; retry shortly")
            encoder = selection.codec
            if self.config.audio_enabled:
                if self.audio is None or not self.audio.ready.is_set():
                    raise CaptureError("Audio stream is not ready")
                recorder = AudioRecording(
                    self.config,
                    self.settings,
                    self.store,
                    self.encoded_hub,
                    self.audio,
                    trigger,
                    duration,
                    selection=selection,
                )
            else:
                recorder = EncodedRecorder(
                    self.config,
                    self.settings,
                    self.store,
                    self.encoded_hub,
                    trigger,
                    duration,
                    selection=selection,
                )
            with self.condition:
                self.recorder = recorder
            self.bus.publish(
                "recording.started",
                "Recording started",
                self.config.id,
                {"trigger": trigger, "encoder": encoder, "audio": self.config.audio_enabled},
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
            encoded = self.encoded_hub.stats()
            packet_ready = bool(
                encoded["ready"]
                and encoded["last_frame_age_seconds"] is not None
                and encoded["last_frame_age_seconds"] < 5
            )
            native = bool(self.backend and self.backend.native_encoded)
            preview_fps = (
                round((len(times) - 1) / (times[-1] - times[0]), 2)
                if len(times) > 1 and times[-1] > times[0] and frame_age < 5
                else 0
            )
            info = {
                "id": self.config.id,
                "name": self.config.name,
                "backend": self.config.backend,
                "status": self.status,
                "error": self.error,
                "width": self.config.width,
                "height": self.config.height,
                "fps_target": self.config.fps,
                "fps": round(encoded["fps"], 2)
                if native
                and encoded["last_frame_age_seconds"] is not None
                and encoded["last_frame_age_seconds"] < 5
                else (0 if native else preview_fps),
                "preview_fps": preview_fps,
                "preview_size": list(self.backend.preview_size)
                if native
                else [self.config.width, self.config.height],
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
                "encoded_buffered_bytes": encoded["buffered_bytes"],
                "encoded_subscribers": encoded["subscribers"],
                "encoded_dropped_frames": encoded["dropped_frames"],
                "encoded_frame_age_seconds": encoded["last_frame_age_seconds"],
                "shed_level": self.shed_level,
                "recording_write_bytes": getattr(recorder, "bytes_written", 0) if recorder else 0,
                "motion_enabled": self.config.motion_enabled,
                "motion_threshold": self.config.motion_threshold,
                "motion_mask": self.config.motion_mask,
                "motion_regions": [region.model_dump() for region in self.config.motion_regions],
                "motion_score": self.motion_score,
                "motion_cells": self.motion_cells,
                "timestamp_overlay": self.config.timestamp_overlay,
                "viewer_users": list(self.config.viewer_users),
                "onvif_ptz": self.config.onvif_ptz,
                "onvif_port": self.config.onvif_port,
                "onvif_username": self.config.onvif_username,
                "onvif_password_set": bool(self.config.onvif_password),
                "audio_enabled": self.config.audio_enabled,
                "audio_source": self.config.audio_source,
                "audio_status": self.audio.status if self.audio else "off",
                "audio_error": self.audio.error if self.audio else None,
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
                    "profile_tested": bool(
                        self.encoding_selection
                        and (self.encoding_selection.probe_verified or encoded["ready"])
                    ),
                    "shared": True,
                    "packet_ready": packet_ready,
                    "packet_stream": encoded,
                    "source": self.backend.capabilities.get("encoded_source", "shared-ffmpeg")
                    if self.backend
                    else None,
                    "zero_copy_raw_frames": bool(
                        self.encoding_selection and self.encoding_selection.zero_copy_raw_frames
                    ),
                    "zero_copy_scope": self.backend.capabilities.get("zero_copy_scope")
                    if native
                    else (
                        "hardware-decoder-to-encoder"
                        if self.encoding_selection and self.encoding_selection.zero_copy_raw_frames
                        else None
                    ),
                    "zero_copy_reason": self.encoding_selection.zero_copy_reason
                    if self.encoding_selection
                    else None,
                    "native_fallback_reason": self.native_fallback_reason,
                    "encoder_sessions_started": self.shared_encoder.sessions_started
                    if self.shared_encoder
                    else (1 if native else 0),
                    "runtime_error": self.shared_encoder.error if self.shared_encoder else None,
                    "fallback_reason": self.encoding_selection.fallback_reason
                    if self.encoding_selection
                    else None,
                    "error": self.encoding_error,
                },
            }
        info["preroll_seconds"] = self.config.preroll_seconds
        return info

    def stop(self):
        with self.operation_lock:
            self.stopping = True
            if self.recorder:
                try:
                    self.stop_recording()
                except RuntimeError:
                    pass
            self.halt.set()
        self._fail_snapshots()
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
        if self.audio:
            self.audio.stop()
        if self.encoder_thread.is_alive():
            self.encoder_thread.join(timeout=15)
        if self.shared_encoder:
            self.shared_encoder.stop()
        self.encoded_hub.close()


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
        audio_alive = bool(camera.audio and camera.audio.thread.is_alive())
        encoder_alive = bool(
            camera.shared_encoder
            and (
                camera.shared_encoder.thread.is_alive()
                or camera.shared_encoder.reader_thread
                and camera.shared_encoder.reader_thread.is_alive()
            )
        )
        if (
            camera.capture_thread.is_alive()
            or camera.motion_thread.is_alive()
            or audio_alive
            or camera.encoder_thread.is_alive()
            or encoder_alive
        ):
            message = "Camera worker did not stop; retry after the driver releases the device"
            camera.status, camera.error = "error", message
            self.bus.publish("camera.stop_failed", message, camera.config.id)
            raise CaptureError(message)

    def save(self, config):
        with self.lock:
            if config.backend == "synthetic" and not self.settings.demo:
                raise CaptureError("Synthetic cameras require explicit --demo mode")
            camera_count = len(self.cameras) + (0 if config.id in self.cameras else 1)
            try:
                import psutil

                memory_total = int(psutil.virtual_memory().total)
            except (OSError, ValueError):
                memory_total = 0
            model = ""
            device_model = Path("/proc/device-tree/model")
            if device_model.is_file():
                try:
                    model = device_model.read_text().strip("\x00\n")
                except OSError:
                    model = ""
            reason = admission_reason(
                width=config.width,
                height=config.height,
                fps=config.fps,
                memory_total=memory_total,
                camera_count=camera_count,
                model=model,
            )
            if reason:
                raise CaptureError(reason)
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
