"""Separate, optional audio capture. Video capture never shares this process.

The stream is off unless a camera profile enables it. Synthetic cameras use a
generated tone. DirectShow, ALSA, and network audio each use one fixed FFmpeg
argument list; device names stay a single argument and cannot add another
input. Recordings are indexed only after the video remux and the audio mux
both finish.
"""

from __future__ import annotations

import logging
import struct
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from .capture import CaptureError
from .processes import owned_process

LOG = logging.getLogger(__name__)

RATE = 16000
CHANNELS = 1
SAMPLE_BYTES = 2
CHUNK_SECONDS = 0.2
CHUNK_BYTES = int(RATE * CHANNELS * SAMPLE_BYTES * CHUNK_SECONDS)
RING_SECONDS = 10
RING_BYTES = RATE * CHANNELS * SAMPLE_BYTES * RING_SECONDS
SPOOL_LIMIT = 256 * 1024 * 1024


def audio_command(ffmpeg: str, backend: str, video_source: str, audio_source: str) -> list[str]:
    """Build the fixed PCM capture command. Callers have already validated names."""
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats"]
    if backend == "synthetic":
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000"]
    elif backend == "dshow":
        command += ["-f", "dshow", "-i", f"audio={audio_source}"]
    elif backend in {"v4l2", "picamera2"}:
        command += ["-f", "alsa", "-i", audio_source]
    elif backend == "network":
        command += [
            "-protocol_whitelist",
            "tcp,udp,rtp,rtsp,http,https,tls,crypto",
            "-rw_timeout",
            "10000000",
        ]
        if video_source.startswith(("rtsp://", "rtsps://")):
            command += ["-rtsp_transport", "tcp"]
        command += ["-i", video_source, "-vn", "-map", "0:a:0"]
    else:
        raise CaptureError("This camera backend has no separate audio stream")
    return command + [
        "-ac",
        "1",
        "-ar",
        str(RATE),
        "-f",
        "s16le",
        "-c:a",
        "pcm_s16le",
        "pipe:1",
    ]


def wav_bytes(pcm: bytes, rate: int = RATE, channels: int = CHANNELS) -> bytes:
    if len(pcm) % (channels * SAMPLE_BYTES):
        raise ValueError("PCM length is not a whole number of samples")
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm),
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        rate,
        rate * channels * SAMPLE_BYTES,
        channels * SAMPLE_BYTES,
        16,
        b"data",
        len(pcm),
    )
    return header + pcm


def mp4_handler_types(path: Path) -> set[bytes]:
    """Read MP4 handler types without decoding samples."""
    handlers = set()
    size = path.stat().st_size
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
                if kind in containers:
                    pending.append((position + header_size, position + length, depth + 1))
                elif kind == b"hdlr" and length >= header_size + 12:
                    payload = stream.read(12)
                    if len(payload) == 12:
                        handlers.add(payload[8:12])
                position += length
    return handlers


def mux_recording(ffmpeg: str, video: Path, pcm: Path, destination: Path, timeout: float) -> None:
    """Copy the finished video and encode its PCM sidecar. The caller indexes the result."""
    if pcm.stat().st_size < CHUNK_BYTES:
        raise RuntimeError("Audio recording produced no samples")
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "s16le",
        "-ar",
        str(RATE),
        "-ac",
        "1",
        "-i",
        str(pcm),
        "-i",
        str(video),
        "-map",
        "1:v:0",
        "-map",
        "0:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-shortest",
        "-movflags",
        "+frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        str(destination),
    ]
    with owned_process(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ) as process:
        code = process.wait(timeout=timeout)
    if code != 0 or not destination.is_file():
        destination.unlink(missing_ok=True)
        raise RuntimeError("Audio mux failed")
    if not {b"vide", b"soun"} <= mp4_handler_types(destination):
        destination.unlink(missing_ok=True)
        raise RuntimeError("Audio mux did not finalize both tracks")


def finalize_with_audio(ffmpeg, store, config, video, media_id, pcm, trigger, duration, timeout):
    destination = video.with_name(video.name + ".mux.partial")
    try:
        mux_recording(ffmpeg, video, pcm, destination, timeout)
        destination.replace(video)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    pcm.unlink(missing_ok=True)
    return store.register(media_id, video, config.id, "video", trigger, duration)


class _ProcessOwner:
    def __init__(self, command):
        self.context = owned_process(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.lock = threading.Lock()
        self.process = None
        self.closed = False

    def start(self):
        self.process = self.context.__enter__()
        return self.process

    def close(self):
        with self.lock:
            if self.closed or self.process is None:
                self.closed = True
                return
            self.closed = True
            self.context.__exit__(None, None, None)


class AudioCapture:
    """One PCM producer and a short ring for live playback and recording preroll."""

    def __init__(self, config, settings, bus):
        self.config = config
        self.settings = settings
        self.bus = bus
        self.halt = threading.Event()
        self.ready = threading.Event()
        self.condition = threading.Condition()
        self.status = "stopped"
        self.error = None
        self.sequence = 0
        self.ring = bytearray()
        self.pending = bytearray()
        self.chunk_pending = bytearray()
        self.chunks = deque(maxlen=8)
        self.record_file = None
        self.record_error = None
        self.recorded = 0
        self.owner = None
        self.owner_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name=f"audio-{config.id}", daemon=True)

    def start(self):
        self.status = "starting"
        self.thread.start()

    def stop(self):
        self.halt.set()
        with self.owner_lock:
            owner = self.owner
        if owner is not None:
            owner.close()
        with self.condition:
            self.condition.notify_all()
        if self.thread.is_alive():
            self.thread.join(timeout=8)

    def next_wav(self, after: int = 0, timeout: float = 2):
        deadline = time.monotonic() + timeout
        with self.condition:
            while not self.halt.is_set():
                newer = [item for item in self.chunks if item[0] > after]
                if newer:
                    return newer[-1] if len(newer) > 3 else newer[0]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)
        raise CaptureError("No current audio")

    def begin_spool(self, path: Path):
        with self.condition:
            if not self.ready.is_set():
                raise CaptureError("Audio stream is not ready")
            if self.record_file is not None:
                raise CaptureError("Audio recording is already active")
            self.spool_path = path
            handle = path.open("wb")
            preroll = self.config.preroll_seconds * RATE * CHANNELS * SAMPLE_BYTES
            payload = bytes(self.ring[-preroll:]) if preroll else b""
            handle.write(payload)
            self.record_file = handle
            self.recorded = len(payload)
            self.record_error = None
        return path

    def finish_spool(self) -> Path:
        with self.condition:
            handle = self.record_file
            self.record_file = None
            error = self.record_error
            path = getattr(self, "spool_path", None)
        if handle is not None:
            handle.close()
        if error or path is None or not path.is_file() or path.stat().st_size < CHUNK_BYTES:
            raise CaptureError("Audio recording produced no samples")
        return path

    def abort_spool(self):
        with self.condition:
            handle = self.record_file
            self.record_file = None
        if handle is not None:
            handle.close()

    def _run(self):
        delay = 0.5
        while not self.halt.is_set():
            command = audio_command(
                self.settings.ffmpeg,
                self.config.backend,
                self.config.source,
                self.config.audio_source,
            )
            owner = _ProcessOwner(command)
            process = None
            try:
                with self.owner_lock:
                    if self.halt.is_set():
                        break
                    process = owner.start()
                    self.owner = owner
                self._read(process)
            except (OSError, CaptureError, subprocess.TimeoutExpired) as exc:
                if not self.halt.is_set():
                    with self.condition:
                        self.ready.clear()
                        self.status = "error"
                        self.error = f"Audio capture failed ({type(exc).__name__})"
                        if self.record_file is not None:
                            self.record_error = "Audio capture stopped during recording"
                        self.condition.notify_all()
                    LOG.warning(
                        "Audio capture for %s failed: %s", self.config.id, type(exc).__name__
                    )
            finally:
                with self.owner_lock:
                    if self.owner is owner:
                        self.owner = None
                owner.close()
            if self.halt.is_set():
                break
            self.halt.wait(delay)
            delay = min(10.0, delay * 2)
        with self.condition:
            self.status = "stopped"
            self.condition.notify_all()

    def _read(self, process):
        from .processes import _output_reader

        read = _output_reader(process.stdout.fileno())
        last = time.monotonic()
        while not self.halt.is_set():
            chunk = read(8192)
            if chunk:
                last = time.monotonic()
                self._accept(chunk)
                continue
            if chunk == b"" or process.poll() is not None:
                raise CaptureError("Audio capture exited")
            if not self.ready.is_set() and time.monotonic() - last > 5:
                raise CaptureError("Audio capture produced no samples")
            self.halt.wait(0.01)

    def _accept(self, chunk: bytes):
        with self.condition:
            self.pending.extend(chunk)
            even = len(self.pending) - (len(self.pending) % SAMPLE_BYTES)
            if not even:
                return
            pcm = bytes(self.pending[:even])
            del self.pending[:even]
            self.ring.extend(pcm)
            overflow = len(self.ring) - RING_BYTES
            if overflow > 0:
                del self.ring[:overflow]
            if self.record_file is not None and self.record_error is None:
                self.recorded += len(pcm)
                if self.recorded > SPOOL_LIMIT:
                    self.record_error = "Audio recording exceeded its size limit"
                else:
                    self.record_file.write(pcm)
            self.chunk_pending.extend(pcm)
            while len(self.chunk_pending) >= CHUNK_BYTES:
                piece = bytes(self.chunk_pending[:CHUNK_BYTES])
                del self.chunk_pending[:CHUNK_BYTES]
                self.sequence += 1
                self.chunks.append((self.sequence, wav_bytes(piece)))
            if self.sequence:
                self.ready.set()
                self.status = "online"
                self.error = None
            self.condition.notify_all()


class AudioRecording:
    """Video remux plus a PCM sidecar. The library entry exists only after both succeed."""

    def __init__(
        self, config, settings, store, hub, audio, trigger="manual", duration=None, selection=None
    ):
        from .recording import EncodedRecorder

        self.config = config
        self.settings = settings
        self.store = store
        self.audio = audio
        self.trigger = trigger
        self.finished = threading.Event()
        self.error = None
        self.result = None
        self.pcm = store.root / f"{config.id}.audio.partial"
        audio.spool_path = self.pcm
        audio.begin_spool(self.pcm)
        try:
            self.inner = EncodedRecorder(
                config,
                settings,
                store,
                hub,
                trigger,
                duration,
                selection=selection,
                register=False,
            )
        except BaseException:
            audio.abort_spool()
            raise

    @property
    def media_id(self):
        return self.inner.media_id

    @property
    def done(self):
        return self.inner.done

    @property
    def deadline(self):
        return self.inner.deadline

    @deadline.setter
    def deadline(self, value):
        self.inner.deadline = value

    @property
    def dropped(self):
        return self.inner.dropped

    @property
    def bytes_written(self):
        return self.inner.bytes_written

    def stop(self):
        if self.finished.is_set():
            if self.error:
                raise RuntimeError(self.error)
            return self.result
        try:
            self.inner.stop()
        except Exception:
            self.audio.abort_spool()
            self.finished.set()
            raise
        timeout = (
            120
            if self.inner.deadline is None
            else max(30, self.inner.deadline - self.inner.started + 20)
        )
        try:
            pcm = self.audio.finish_spool()
            self.result = finalize_with_audio(
                self.settings.ffmpeg,
                self.store,
                self.config,
                self.inner.path,
                self.inner.media_id,
                pcm,
                self.trigger,
                self.inner.result["duration"],
                timeout,
            )
        except Exception as exc:
            self.audio.abort_spool()
            self.error = "Recording failed (audio); media retained for recovery"
            self.store.bus.publish("recording.error", self.error, self.config.id)
            LOG.warning("Audio finalize for %s failed: %s", self.config.id, type(exc).__name__)
            self.finished.set()
            raise RuntimeError(self.error) from exc
        self.finished.set()
        return self.result
