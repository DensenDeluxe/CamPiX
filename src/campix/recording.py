from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time

from .capture import terminate_process
from .encoding import encoder_args, probe_encoders
from .processes import NO_WINDOW, owned_process
from .tsmux import TransportStreamMuxer


def _validate_remux(path, expected_frames):
    """Verify finalized MP4 structure and video sample count without decoding pixels."""
    size = path.stat().st_size
    types, samples, trun_samples, media_bytes = set(), [], [], 0
    containers = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"moof", b"traf"}
    with path.open("rb") as stream:
        pending = [(0, size, 0)]
        boxes = 0
        while pending:
            position, end, depth = pending.pop()
            if depth > 8:
                raise RuntimeError("Recording container nesting exceeds its supported limit")
            while position < end:
                stream.seek(position)
                header = stream.read(8)
                if len(header) != 8:
                    raise RuntimeError("Recording contains an incomplete media box")
                length, kind = int.from_bytes(header[:4], "big"), header[4:]
                header_size = 8
                if length == 1:
                    extended = stream.read(8)
                    if len(extended) != 8:
                        raise RuntimeError("Recording contains an incomplete extended media box")
                    length, header_size = int.from_bytes(extended, "big"), 16
                elif length == 0:
                    length = end - position
                if length < header_size or position + length > end:
                    raise RuntimeError("Recording contains invalid media box bounds")
                boxes += 1
                if boxes > 100000:
                    raise RuntimeError("Recording media box count exceeds its supported limit")
                if depth == 0:
                    types.add(kind)
                    if kind == b"mdat":
                        media_bytes += length - header_size
                if kind in containers:
                    pending.append((position + header_size, position + length, depth + 1))
                elif kind == b"stsz":
                    sample_header = stream.read(12)
                    if len(sample_header) != 12 or length < header_size + 12:
                        raise RuntimeError("Recording contains an incomplete sample table")
                    count = int.from_bytes(sample_header[8:12], "big")
                    constant_size = int.from_bytes(sample_header[4:8], "big")
                    if not constant_size and length < header_size + 12 + count * 4:
                        raise RuntimeError("Recording contains a truncated sample table")
                    samples.append(count)
                elif kind == b"trun":
                    sample_header = stream.read(8)
                    if len(sample_header) != 8 or length < header_size + 8:
                        raise RuntimeError("Recording contains an incomplete track run")
                    trun_samples.append(int.from_bytes(sample_header[4:8], "big"))
                position += length
    counted = sum(trun_samples) or sum(samples)
    if (
        not {b"ftyp", b"moov"}.issubset(types)
        or b"mdat" not in types
        or not media_bytes
        or counted != expected_frames
    ):
        raise RuntimeError("Remuxer did not finalize all recorded video frames")


class EncodedRecorder:
    """Remux one lossless subscription; stream consumers never own another encoder."""

    stop_timeout = 25.0
    write_timeout = 10.0
    startup_timeout = 6.0

    def __init__(
        self,
        config,
        settings,
        store,
        hub,
        trigger="manual",
        duration=None,
        *,
        selection=None,
        register=True,
    ):
        self.config, self.settings, self.store, self.hub = config, settings, store, hub
        self.trigger, self.selection = trigger, selection
        self.register = register
        self.started = time.monotonic()
        self.deadline = self.started + duration if duration else None
        self.stop_requested, self.done = threading.Event(), threading.Event()
        self.stop_lock = threading.Lock()
        self.frames = self.dropped = 0
        self.bytes_written = 0
        self.error = self.result = None
        self.cutoff = None
        self.first_timestamp = self.last_timestamp = None
        self.last_sequence = 0
        self.progress_at = self.started
        self.writing = False
        self.stop_at = None
        state = hub.stats()
        age = state.get("last_frame_age_seconds")
        if not state.get("ready") or age is None or age > 5:
            raise RuntimeError("Shared encoder has no current decodable stream")
        self.subscription = hub.subscribe("recording", pre_roll_seconds=config.preroll_seconds)
        self.context = None
        self.proc = None
        try:
            self.media_id, self.path = store.reserve("mp4")
            self.partial = self.path.with_suffix(".mp4.partial")
            self.muxer = TransportStreamMuxer(epoch=state["epoch"])
            command = [
                settings.ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-probesize",
                "65536",
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
                "-avoid_negative_ts",
                "make_zero",
                # Fragmented MP4 is playable as it is written. Finalization does not
                # rewrite the file to move the moov atom, and the library still indexes
                # the recording only after this remux succeeds.
                "-movflags",
                "+frag_keyframe+empty_moov+default_base_moof",
                "-f",
                "mp4",
                str(self.partial),
            ]
            self.context = owned_process(
                command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self.proc = self.context.__enter__()
            self.thread = threading.Thread(
                target=self._write, name=f"record-remux-{config.id}", daemon=True
            )
            self.watch_thread = threading.Thread(
                target=self._watch, name=f"record-remux-watch-{config.id}", daemon=True
            )
            self.thread.start()
            self.watch_thread.start()
        except BaseException:
            self.subscription.close()
            if self.context is not None:
                self.context.__exit__(*sys.exc_info())
            raise

    def _request_stop(self):
        with self.stop_lock:
            if not self.done.is_set() and not self.stop_requested.is_set():
                self.cutoff = self.subscription.finish()
                self.stop_at = time.monotonic()
                self.stop_requested.set()

    def _watch(self):
        while not self.done.wait(0.1):
            now = time.monotonic()
            if self.deadline is not None and now >= self.deadline:
                self._request_stop()
            if (self.writing and now - self.progress_at > self.write_timeout) or (
                self.stop_at is not None and now - self.stop_at > self.stop_timeout
            ):
                terminate_process(self.proc)
                return

    def _write(self):
        try:
            while True:
                if self.proc.poll() is not None:
                    raise RuntimeError("Remuxer exited before recording was stopped")
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    self._request_stop()
                with self.stop_lock:
                    if self.stop_requested.is_set() and self.last_sequence >= self.cutoff:
                        break
                packet = self.subscription.read(timeout=0.2)
                if packet is None:
                    if self.stop_requested.is_set():
                        break
                    reference = self.last_timestamp if self.frames else self.started
                    if time.monotonic() - reference > self.startup_timeout:
                        raise RuntimeError("Shared encoder stopped delivering decodable frames")
                    continue
                with self.stop_lock:
                    if self.stop_requested.is_set() and packet.sequence > self.cutoff:
                        break
                data = self.muxer.mux(packet)
                self.progress_at = time.monotonic()
                self.writing = True
                try:
                    written = self.proc.stdin.write(data)
                    if written != len(data):
                        raise RuntimeError("Remuxer accepted an incomplete video packet")
                    self.bytes_written += written
                finally:
                    self.writing = False
                self.frames += 1
                self.last_sequence = packet.sequence
                if self.first_timestamp is None:
                    self.first_timestamp = packet.captured_at
                self.last_timestamp = packet.captured_at
                if self.frames % self.config.fps == 0:
                    self.store.check_space()
            self.subscription.close()
            self.proc.stdin.close()
            if self.proc.wait(timeout=20) != 0 or self.frames == 0:
                raise RuntimeError("Remuxer did not finalize a recording")
            _validate_remux(self.partial, self.frames)
            self.partial.replace(self.path)
            duration = self.last_timestamp - self.first_timestamp + 1 / self.config.fps
            if self.register:
                self.result = self.store.register(
                    self.media_id, self.path, self.config.id, "video", self.trigger, duration
                )
            else:
                # The audio mux indexes this file only after its own finalization succeeds.
                self.result = {"id": self.media_id, "duration": duration, "deferred": True}
        except Exception as exc:
            self.error = f"Recording failed ({type(exc).__name__}); media retained for recovery"
            self.store.bus.publish("recording.error", self.error, self.config.id)
        finally:
            self.subscription.close()
            try:
                self.context.__exit__(None, None, None)
            finally:
                self.done.set()

    def stop(self):
        self._request_stop()
        if not self.done.wait(self.stop_timeout):
            terminate_process(self.proc)
            self.thread.join(timeout=5)
            if not self.done.is_set():
                raise RuntimeError("Recording remuxer did not stop in time")
        self.watch_thread.join(timeout=1)
        self.thread.join(timeout=1)
        if self.error:
            raise RuntimeError(self.error)
        return self.result


def encoder_probe(ffmpeg):
    return probe_encoders(ffmpeg)


class Recorder:
    """Legacy JPEG writer retained for the idle-encoder regression.

    Production recordings use EncodedRecorder and the shared H.264 hub.
    """

    def __init__(
        self, config, settings, store, preroll, trigger="manual", duration=None, *, selection=None
    ):
        self.config, self.settings, self.store = config, settings, store
        self.media_id, self.path = store.reserve("mp4")
        self.partial = self.path.with_suffix(".mp4.partial")
        self.queue = queue.Queue(maxsize=min(600, config.fps * (config.preroll_seconds + 4)))
        self.queue_lock = threading.Lock()
        self.queued_bytes = 0
        self.stop_requested = threading.Event()
        self.done = threading.Event()
        self.started = time.monotonic()
        self.deadline = self.started + duration if duration else None
        self.frames = 0
        self.dropped = 0
        self.error = None
        self.result = None
        self.trigger = trigger
        self.selection = selection
        codec = (
            selection.codec
            if selection
            else ("libx264" if config.encoder == "auto" else config.encoder)
        )
        width, height = getattr(config, "width", 640), getattr(config, "height", 480)
        input_args, output_args = (
            (selection.input_args, selection.output_args)
            if selection
            else encoder_args(codec, width, height, config.fps)
        )
        command = [
            settings.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            *input_args,
            "-f",
            "image2pipe",
            "-framerate",
            str(config.fps),
            "-c:v",
            "mjpeg",
            "-i",
            "pipe:0",
            "-an",
            *output_args,
        ]
        command += [
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(self.partial),
        ]
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
        for frame in preroll:
            self.offer(frame)
        self.thread = threading.Thread(target=self._write, name=f"record-{config.id}", daemon=True)
        self.thread.start()

    def offer(self, jpeg):
        if self.stop_requested.is_set() or self.done.is_set():
            return
        with self.queue_lock:
            if self.queued_bytes + len(jpeg) > 32 * 1024 * 1024:
                self.dropped += 1
                return
            try:
                self.queue.put_nowait(jpeg)
                self.queued_bytes += len(jpeg)
            except queue.Full:
                self.dropped += 1

    def _write(self):
        try:
            while not self.stop_requested.is_set() or not self.queue.empty():
                if self.proc.poll() is not None:
                    raise RuntimeError("Encoder exited before recording was stopped")
                try:
                    frame = self.queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                with self.queue_lock:
                    self.queued_bytes -= len(frame)
                self.proc.stdin.write(frame)
                self.frames += 1
                if self.frames % self.config.fps == 0:
                    self.store.check_space()
            self.proc.stdin.close()
            if self.proc.wait(timeout=20) != 0 or self.frames == 0:
                raise RuntimeError("Encoder did not finalize a recording")
            self.partial.replace(self.path)
            self.result = self.store.register(
                self.media_id,
                self.path,
                self.config.id,
                "video",
                self.trigger,
                self.frames / self.config.fps,
            )
        except Exception as exc:
            self.error = f"Recording failed ({type(exc).__name__}); partial retained for recovery"
            self.store.bus.publish("recording.error", self.error, self.config.id)
        finally:
            terminate_process(self.proc)
            if self.proc.stdin and not self.proc.stdin.closed:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
            self.done.set()

    def stop(self):
        self.stop_requested.set()
        if not self.done.wait(25):
            terminate_process(self.proc)
            self.thread.join(timeout=5)
            if not self.done.is_set():
                raise RuntimeError("Encoder did not stop in time")
        if self.error:
            raise RuntimeError(self.error)
        return self.result
