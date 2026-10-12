"""One cancellable H.264 encoder per JPEG camera, with packet fanout."""

from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from dataclasses import replace

from .capture import CaptureError, terminate_process
from .encoded import AnnexBParser, EncodedFormatError, EncodedRestart, EncodedStreamEnded
from .encoding import PROBE_FAILURE_TTL_SECONDS, EncoderUnavailable, encoder_args, select_encoder
from .processes import _output_reader, owned_process

MAX_PENDING_FRAMES = 240
ENCODER_OUTPUT_TIMEOUT_SECONDS = 10.0


class _SourceUnavailable(CaptureError):
    """Capture failed independently of the selected encoder."""


class _ProcessOwner:
    """Allow shutdown to reap a blocked writer's complete child tree once."""

    def __init__(self, command):
        self.context = owned_process(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        self.lock = threading.Lock()
        self.process = None
        self.closed = False

    def start(self):
        self.process = self.context.__enter__()
        return self.process

    def close(self):
        with self.lock:
            if not self.closed and self.process is not None:
                self.closed = True
                # Kill the complete tree before closing pipes, releasing both a
                # blocked stdin writer and any inherited stdout writers.
                self.context.__exit__(None, None, None)


class SharedEncoder:
    def __init__(self, camera, selection):
        self.camera = camera
        self.selection = selection
        self.halt = threading.Event()
        self.spawn_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.pending = deque()
        self.proc = None
        self.owner = None
        self.reader_thread = None
        self.error = None
        self.reader_error = None
        self.sessions_started = 0
        self.frames_encoded = 0
        self.thread = threading.Thread(
            target=self._run, name=f"shared-encode-{camera.config.id}", daemon=True
        )

    def start(self):
        self.thread.start()

    def _read(self, process, epoch, pending=None):
        parser = AnnexBParser()
        pending = self.pending if pending is None else pending
        try:
            try:
                read = _output_reader(process.stdout.fileno())
            except (OSError, ValueError, AttributeError):
                read = process.stdout.read1  # Isolated in-memory test streams.
            last_packet = time.monotonic()
            while not self.halt.is_set() and not self.camera.halt.is_set():
                if self.proc is not process or self.camera.encoded_hub.epoch != epoch:
                    return
                chunk = read(65536)
                if chunk == b"":
                    raise CaptureError("Shared encoder output ended")
                if chunk:
                    for packet in parser.feed(chunk):
                        with self.pending_lock:
                            if self.proc is not process or self.camera.encoded_hub.epoch != epoch:
                                return
                            if not pending:
                                raise EncodedFormatError(
                                    "Encoder returned an unexpected access unit"
                                )
                            captured = pending.popleft()
                        self.camera.encoded_hub.publish(packet, captured, epoch=epoch)
                        self.frames_encoded += 1
                        last_packet = time.monotonic()
                        self.error = None
                with self.pending_lock:
                    stalled = (
                        bool(pending)
                        and time.monotonic() - last_packet > ENCODER_OUTPUT_TIMEOUT_SECONDS
                    )
                if stalled:
                    raise CaptureError("Shared encoder stopped returning complete access units")
                if chunk is None:
                    self.halt.wait(0.01)
        except (OSError, ValueError, CaptureError, EncodedRestart, EncodedStreamEnded) as exc:
            with self.pending_lock:
                current = self.proc is process and self.camera.encoded_hub.epoch == epoch
                if current and not self.halt.is_set() and not self.camera.halt.is_set():
                    self.reader_error = type(exc).__name__
            if current:
                try:
                    terminate_process(process)
                except (OSError, subprocess.TimeoutExpired):
                    pass  # Owned-process supervisor cleanup still reaps the tree.

    def _frame_packet(self, sequence=None, timeout=1):
        try:
            if hasattr(self.camera, "frame_packet"):
                return self.camera.frame_packet(sequence, timeout=timeout)
            current, jpeg = self.camera.frame(sequence, timeout=timeout)
            with self.camera.condition:
                captured = self.camera.frame_times[-1]
            return current, jpeg, captured
        except CaptureError as exc:
            raise _SourceUnavailable("Camera source is unavailable") from exc

    def _command(self):
        cfg = self.camera.config
        command = [
            self.camera.settings.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-probesize",
            "32",
            "-analyzeduration",
            "0",
            "-flags",
            "+low_delay",
            "-threads",
            "1",
            *self.selection.input_args,
            "-f",
            "image2pipe",
            "-framerate",
            str(cfg.fps),
        ]
        if not self.selection.zero_copy_raw_frames:
            command += ["-c:v", "mjpeg"]
        return command + [
            "-i",
            "pipe:0",
            "-an",
            *self.selection.output_args,
            "-fps_mode",
            "passthrough",
            "-bsf:v",
            "h264_metadata=aud=insert",
            "-flush_packets",
            "1",
            "-f",
            "h264",
            "pipe:1",
        ]

    def _run(self):
        excluded_until = {}
        retry_delay = 1.0
        while not self.halt.is_set() and not self.camera.halt.is_set():
            owner = None
            process = None
            epoch = self.camera.encoded_hub.epoch
            before = self.frames_encoded
            try:
                cfg = self.camera.config
                now = time.monotonic()
                excluded = {codec for codec, until in excluded_until.items() if until > now}
                if excluded_until:
                    _, sample, _ = self._frame_packet(timeout=3)
                    self.selection = select_encoder(
                        self.camera.settings.ffmpeg,
                        "auto",
                        width=cfg.width,
                        height=cfg.height,
                        fps=cfg.fps,
                        capabilities=self.camera.encoders,
                        excluded=excluded,
                        cancel=self.halt,
                        input_codec="mjpeg",
                        input_sample=sample,
                    )
                    self.selection = replace(
                        self.selection,
                        fallback_reason="Shared encoding session failed; using "
                        + self.selection.codec,
                    )
                self.camera.encoding_selection = self.selection
                owner = _ProcessOwner(self._command())
                with self.spawn_lock:
                    if self.halt.is_set() or self.camera.halt.is_set():
                        break
                    process = owner.start()
                    with self.pending_lock:
                        self.proc = process
                        self.owner = owner
                        self.pending = deque()
                        pending = self.pending
                        self.reader_error = None
                    self.sessions_started += 1
                self.reader_thread = threading.Thread(
                    target=self._read,
                    args=(process, epoch, pending),
                    name=f"h264-read-{cfg.id}",
                    daemon=True,
                )
                self.reader_thread.start()
                # Prime once using original timestamps. Replayed JPEGs are never
                # relabeled as newly captured frames or duplicated for output FPS.
                with self.camera.condition:
                    priming = list(self.camera.preroll)
                    sequence = self.camera.sequence
                for captured, jpeg in priming:
                    if self.halt.is_set() or self.camera.halt.is_set():
                        break
                    self._offer(process, jpeg, captured)
                while not self.halt.is_set() and not self.camera.halt.is_set():
                    if self.reader_error or process.poll() is not None:
                        raise CaptureError("Shared encoder stopped producing access units")
                    current, jpeg, captured = self._frame_packet(sequence, timeout=1)
                    if current == sequence:
                        continue
                    sequence = current
                    self._offer(process, jpeg, captured)
            except (
                CaptureError,
                OSError,
                EncoderUnavailable,
                ValueError,
                EncodedStreamEnded,
            ) as exc:
                if not self.halt.is_set() and not self.camera.halt.is_set():
                    self.error = f"Shared encoding failed ({type(exc).__name__}); retrying"
                    self.camera.bus.publish("encoding.error", self.error, self.camera.config.id)
                    if (
                        not isinstance(exc, _SourceUnavailable)
                        and self.camera.config.encoder == "auto"
                    ):
                        if self.selection.zero_copy_raw_frames:
                            device = None
                            if self.selection.codec == "h264_vaapi":
                                options = dict(
                                    zip(
                                        self.selection.input_args[::2],
                                        self.selection.input_args[1::2],
                                        strict=False,
                                    )
                                )
                                device = options.get("-vaapi_device")
                            inputs, outputs = encoder_args(
                                self.selection.codec, cfg.width, cfg.height, cfg.fps, device=device
                            )
                            self.selection = replace(
                                self.selection,
                                input_args=inputs,
                                output_args=outputs,
                                zero_copy_raw_frames=False,
                                zero_copy_reason="runtime-hardware-decoder-failed",
                            )
                        elif self.selection.hardware:
                            excluded_until[self.selection.codec] = (
                                time.monotonic() + PROBE_FAILURE_TTL_SECONDS
                            )
            finally:
                if owner is not None:
                    owner.close()
                if self.reader_thread is not None:
                    self.reader_thread.join(timeout=5)
                    if self.reader_thread.is_alive():
                        self.error = "Shared encoder reader did not stop; refusing another session"
                        self.camera.bus.publish("encoding.error", self.error, self.camera.config.id)
                        self.halt.set()
                with self.spawn_lock:
                    with self.pending_lock:
                        if self.proc is process:
                            self.proc = None
                            self.owner = None
                if not self.camera.halt.is_set():
                    try:
                        self.camera.encoded_hub.restart()
                    except EncodedStreamEnded:
                        self.halt.set()
            if self.frames_encoded > before:
                retry_delay = 1.0
            self.halt.wait(retry_delay)
            retry_delay = min(10.0, retry_delay * 2)

    def _offer(self, process, jpeg, captured):
        with self.pending_lock:
            if len(self.pending) >= MAX_PENDING_FRAMES:
                raise CaptureError("Shared encoder exceeded its pending frame bound")
            if self.reader_error:
                raise CaptureError("Shared encoder reader failed")
            self.pending.append(captured)
        process.stdin.write(jpeg)
        process.stdin.flush()

    def stop(self):
        self.halt.set()
        with self.spawn_lock:
            owner = self.owner
        if owner is not None:
            owner.close()
        if self.thread.is_alive():
            self.thread.join(timeout=15)
        if self.reader_thread is not None and self.reader_thread.is_alive():
            self.reader_thread.join(timeout=5)
        if self.thread.is_alive() or (
            self.reader_thread is not None and self.reader_thread.is_alive()
        ):
            raise CaptureError("Shared encoder did not stop within its shutdown deadline")
