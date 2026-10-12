from __future__ import annotations

import queue
import subprocess
import threading
import time

from .capture import terminate_process
from .encoding import encoder_args, probe_encoders
from .processes import NO_WINDOW


def encoder_probe(ffmpeg):
    return probe_encoders(ffmpeg)


class Recorder:
    """One bounded writer per camera; FFmpeg backpressure never stalls capture."""

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
