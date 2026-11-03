"""Remux interrupted recordings and index them only after ffprobe accepts the file."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path

from .processes import owned_process

_PARTIAL = ".mp4.partial"
_LIMIT = 8


class RecoveryService:
    def __init__(self, store, settings, bus):
        self.store, self.settings, self.bus = store, settings, bus
        self.halt = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        self.status = {"state": "idle", "results": []}

    def start(self) -> dict:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return dict(self.status)
            self.halt.clear()
            self.status = {"state": "running", "results": []}
            self.thread = threading.Thread(target=self._run, name="media-recovery", daemon=True)
            self.thread.start()
            return dict(self.status)

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.status)

    def stop(self) -> None:
        self.halt.set()
        thread = self.thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

    def _run(self) -> None:
        results = []
        try:
            for path in self._partials():
                if self.halt.is_set():
                    break
                results.append(self._recover(path))
            if not self.halt.is_set():
                for path in self._unindexed():
                    if self.halt.is_set():
                        break
                    results.append(self._index_final(path))
        finally:
            with self.lock:
                self.status = {"state": "idle", "results": results}
            self.bus.publish(
                "storage.recovery",
                "Interrupted recording recovery finished",
                data={"files": len(results)},
            )

    def _partials(self) -> list[Path]:
        known = {row["filename"] for row in self.store.db.all("SELECT filename FROM media")}
        found = []
        for path in sorted(self.store.root.iterdir()):
            if path.is_symlink() or not path.is_file() or not path.name.endswith(_PARTIAL):
                continue
            final_name = path.name.removesuffix(".partial")
            if final_name in known or (self.store.root / final_name).exists():
                continue
            found.append(path)
            if len(found) >= _LIMIT:
                break
        return found

    def _unindexed(self) -> list[Path]:
        """Finalized files that never received a library row."""
        known = {row["filename"] for row in self.store.db.all("SELECT filename FROM media")}
        found = []
        for path in sorted(self.store.root.iterdir()):
            if path.is_symlink() or not path.is_file():
                continue
            if path.suffix not in {".mp4", ".jpg"} or path.name in known:
                continue
            if _media_id(path.name) is None:
                continue
            found.append(path)
            if len(found) >= _LIMIT:
                break
        return found

    def _index_final(self, path: Path) -> dict:
        media_id = _media_id(path.name)
        if not media_id:
            return {"file": path.name, "ok": False, "error": "Unrecognized file name"}
        if path.suffix == ".jpg":
            if not _jpeg_complete(path):
                return {"file": path.name, "ok": False, "error": "Recovered file is not a JPEG"}
            kind, duration = "snapshot", None
        else:
            ffmpeg = shutil.which(self.settings.ffmpeg) or self.settings.ffmpeg
            ffprobe = _ffprobe_path(ffmpeg)
            if not ffprobe:
                return {"file": path.name, "ok": False, "error": "ffprobe is not available"}
            try:
                duration = _duration(ffprobe, path)
            except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
                duration = None
            if not duration or duration <= 0:
                return {"file": path.name, "ok": False, "error": "Recovered file has no duration"}
            kind = "video"
        try:
            self.store.register(media_id, path, "recovered", kind, "recovery", duration)
        except Exception:
            # The finalized file is the only copy. A failed insert must not delete it.
            existing = self.store.db.one("SELECT id FROM media WHERE id=?", (media_id,))
            if existing:
                return {"file": path.name, "ok": True, "media_id": media_id}
            return {"file": path.name, "ok": False, "error": "Recovered file was not indexed"}
        return {"file": path.name, "ok": True, "media_id": media_id}

    def _recover(self, partial: Path) -> dict:
        media_id = _media_id(partial.name)
        if not media_id:
            return {"file": partial.name, "ok": False, "error": "Unrecognized partial name"}
        ffmpeg = shutil.which(self.settings.ffmpeg) or self.settings.ffmpeg
        ffprobe = _ffprobe_path(ffmpeg)
        if not ffprobe:
            return {"file": partial.name, "ok": False, "error": "ffprobe is not available"}
        target = partial.with_name(partial.name.removesuffix(".partial"))
        repair = target.with_name(target.stem + ".repair.mp4")
        command = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(partial),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(repair),
        ]
        try:
            code = _run(command, 20, self.halt)
        except (OSError, subprocess.SubprocessError):
            repair.unlink(missing_ok=True)
            return {"file": partial.name, "ok": False, "error": "Remux did not finish"}
        if code != 0 or not repair.is_file() or self.halt.is_set():
            repair.unlink(missing_ok=True)
            return {"file": partial.name, "ok": False, "error": "Remux did not produce a file"}
        try:
            duration = _duration(ffprobe, repair)
        except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
            duration = None
        if not duration or duration <= 0:
            repair.unlink(missing_ok=True)
            return {"file": partial.name, "ok": False, "error": "Recovered file has no duration"}
        repair.replace(target)
        try:
            self.store.register(media_id, target, "recovered", "video", "recovery", duration)
        except Exception:
            target.unlink(missing_ok=True)
            return {"file": partial.name, "ok": False, "error": "Recovered file was not indexed"}
        partial.unlink(missing_ok=True)
        return {"file": target.name, "ok": True, "media_id": media_id}


def _media_id(name: str) -> str | None:
    # Partial recordings are named "<stamp>_<id>.mp4.partial".
    stem = name.removesuffix(".partial") if name.endswith(".partial") else name
    for suffix in (".mp4", ".jpg"):
        if not stem.endswith(suffix):
            continue
        marker = stem[: -len(suffix)].rpartition("_")[2]
        if len(marker) == 32 and all(char in "0123456789abcdef" for char in marker):
            return marker
        return None
    return None


def _jpeg_complete(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            if stream.read(3) != b"\xff\xd8\xff":
                return False
            stream.seek(0, 2)
            return stream.tell() >= 4
    except OSError:
        return False


def _ffprobe_path(ffmpeg: str) -> str | None:
    sibling = Path(ffmpeg).with_name("ffprobe.exe" if ffmpeg.lower().endswith(".exe") else "ffprobe")
    if sibling.is_file():
        return str(sibling)
    return shutil.which("ffprobe")


def _duration(ffprobe: str, path: Path) -> float | None:
    output = _capture(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        15,
    )
    payload = json.loads(output or "{}")
    duration = float(payload.get("format", {}).get("duration", 0))
    return duration if duration > 0 else None


def _run(command: list[str], timeout: float, halt: threading.Event) -> int:
    with owned_process(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ) as process:
        deadline = __import__("time").monotonic() + timeout
        while process.poll() is None:
            if halt.is_set() or __import__("time").monotonic() >= deadline:
                return 1
            halt.wait(0.05)
        return process.returncode or 0


def _capture(command: list[str], timeout: float) -> bytes:
    output = bytearray()
    with owned_process(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ) as process:
        deadline = __import__("time").monotonic() + timeout
        while len(output) <= 65536:
            chunk = process.stdout.read(8192)
            if chunk:
                output.extend(chunk)
                continue
            if process.poll() is not None:
                return bytes(output)
            if __import__("time").monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            __import__("time").sleep(0.01)
    raise ValueError("ffprobe output exceeds its size limit")
