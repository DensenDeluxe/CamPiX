from __future__ import annotations

import io
import queue
import subprocess
import threading
import time
import uuid

from PIL import Image, ImageOps

from .capture import terminate_process
from .media import StorageError
from .processes import NO_WINDOW


class ExportService:
    """Single bounded export worker, recoverable job state, no user-supplied paths."""

    def __init__(self, db, store, settings, bus):
        self.db, self.store, self.settings, self.bus = db, store, settings, bus
        self.queue = queue.Queue(maxsize=4)
        self.lock = threading.Lock()
        self.halt = threading.Event()
        self.proc = None
        self.thread = threading.Thread(target=self._run, name="media-export", daemon=True)

    def start(self):
        self.db.execute(
            "UPDATE export_jobs SET status='failed',error='Export interrupted by service restart' WHERE status IN ('queued','running')"
        )
        self.thread.start()

    def submit(self, payload):
        with self.lock:
            if self.queue.full():
                raise StorageError("Export queue is full; retry when a job completes")
            items = [self.store.get(media_id) for media_id in payload.media_ids]
            if any(item["kind"] != "snapshot" for item in items):
                raise StorageError("Timelapse accepts only snapshots")
            job_id = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO export_jobs(id,status,created) VALUES (?,'queued',?)",
                (job_id, time.time()),
            )
            self.queue.put_nowait(
                (job_id, sorted(items, key=lambda item: item["created"]), payload.fps)
            )
        self.bus.publish("export.queued", "Timelapse export queued", data={"job_id": job_id})
        return self.get(job_id)

    def get(self, job_id):
        row = self.db.one("SELECT * FROM export_jobs WHERE id=?", (job_id,))
        if not row:
            raise KeyError(job_id)
        return row

    def _encode(self, items, fps):
        media_id, destination = self.store.reserve("mp4")
        partial = destination.with_suffix(".mp4.partial")
        command = [
            self.settings.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "image2pipe",
            "-framerate",
            str(fps),
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            str(partial),
        ]
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
        deadline = time.monotonic() + 120
        size = None
        try:
            for item in items:
                if self.halt.is_set() or time.monotonic() > deadline:
                    raise RuntimeError("Export cancelled or timed out")
                self.store.check_space()
                with self.store.lock, Image.open(self.store.path(item["filename"])) as source:
                    if size is None:
                        scale = min(1, 1920 / source.width, 1080 / source.height)
                        size = (
                            max(2, int(source.width * scale) // 2 * 2),
                            max(2, int(source.height * scale) // 2 * 2),
                        )
                    frame = ImageOps.pad(source.convert("RGB"), size, color="black")
                    buffer = io.BytesIO()
                    frame.save(buffer, "JPEG", quality=90)
                payload = buffer.getvalue()
                if self.proc.poll() is not None or self.proc.stdin.write(payload) != len(payload):
                    raise RuntimeError("Encoder failed")
            self.proc.stdin.close()
            while self.proc.poll() is None:
                if self.halt.wait(0.2) or time.monotonic() > deadline:
                    raise RuntimeError("Export cancelled or timed out")
                self.store.check_space()
            if self.proc.returncode != 0:
                raise RuntimeError("Encoder failed")
        finally:
            terminate_process(self.proc)
            self.proc = None
        partial.replace(destination)
        return self.store.register(
            media_id, destination, items[0]["camera_id"], "video", "timelapse", len(items) / fps
        )

    def _run(self):
        while not self.halt.is_set():
            try:
                job_id, items, fps = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.db.execute("UPDATE export_jobs SET status='running' WHERE id=?", (job_id,))
                result = self._encode(items, fps)
                self.db.execute(
                    "UPDATE export_jobs SET status='completed',media_id=? WHERE id=?",
                    (result["id"], job_id),
                )
                self.bus.publish(
                    "export.completed",
                    "Timelapse saved",
                    data={"job_id": job_id, "media_id": result["id"]},
                )
            except Exception as exc:
                message = f"Export failed ({type(exc).__name__}); inputs were not modified"
                self.db.execute(
                    "UPDATE export_jobs SET status='failed',error=? WHERE id=?", (message, job_id)
                )
                self.bus.publish("export.failed", message, data={"job_id": job_id})
        self.db.execute(
            "UPDATE export_jobs SET status='failed',error='Export cancelled on shutdown' WHERE status='queued'"
        )

    def stop(self):
        self.halt.set()
        if self.thread.is_alive():
            self.thread.join(timeout=10)
