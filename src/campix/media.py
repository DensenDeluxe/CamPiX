from __future__ import annotations

import shutil
import threading
import time
import uuid


class StorageError(RuntimeError):
    pass


class MediaStore:
    def __init__(self, db, settings, bus):
        self.db, self.settings, self.bus = db, settings, bus
        self.root = (settings.data_dir / "media").resolve()
        self.lock = threading.RLock()
        self._reserve_notice = 0.0

    def path(self, filename):
        path = (self.root / filename).resolve()
        if path.parent != self.root or path.is_symlink():
            raise StorageError("Invalid media path")
        return path

    def reserve(self, extension):
        self.check_space()
        media_id = uuid.uuid4().hex
        filename = f"{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}_{media_id}.{extension}"
        return media_id, self.path(filename)

    def check_space(self):
        if shutil.disk_usage(self.root).free < self.settings.min_free_mb * 1024 * 1024:
            now = time.monotonic()
            if now - self._reserve_notice >= 60:
                self._reserve_notice = now
                self.bus.publish(
                    "storage.reserve",
                    "Storage reserve reached; free space before recording",
                )
            raise StorageError("Storage reserve reached; free space before recording")

    def register(self, media_id, path, camera_id, kind, trigger, duration=None):
        with self.lock:
            self.db.execute(
                "INSERT INTO media(id,camera_id,kind,filename,created,size,duration,trigger) VALUES (?,?,?,?,?,?,?,?)",
                (
                    media_id,
                    camera_id,
                    kind,
                    path.name,
                    time.time(),
                    path.stat().st_size,
                    duration,
                    trigger,
                ),
            )
        result = self.get(media_id)
        self.bus.publish(
            "media.saved", f"{kind.capitalize()} saved", camera_id, {"media_id": media_id}
        )
        return result

    def snapshot(self, camera_id, jpeg, trigger="manual"):
        media_id, path = self.reserve("jpg")
        partial = path.with_suffix(".partial")
        with partial.open("xb") as stream:
            stream.write(jpeg)
        partial.replace(path)
        return self.register(media_id, path, camera_id, "snapshot", trigger)

    def get(self, media_id):
        item = self.db.one("SELECT * FROM media WHERE id=?", (media_id,))
        if not item:
            raise KeyError(media_id)
        item["locked"] = bool(item["locked"])
        return item

    def list(
        self,
        limit=100,
        offset=0,
        kind=None,
        camera_id=None,
        query="",
        created_after=None,
        created_before=None,
        exclude_cameras=(),
    ):
        clauses, args = [], []
        for key, value in (("kind", kind), ("camera_id", camera_id)):
            if value:
                clauses.append(key + "=?")
                args.append(value)
        if created_after is not None:
            clauses.append("created>=?")
            args.append(created_after)
        if created_before is not None:
            clauses.append("created<=?")
            args.append(created_before)
        if exclude_cameras:
            clauses.append("camera_id NOT IN (" + ",".join("?" * len(exclude_cameras)) + ")")
            args.extend(exclude_cameras)
        if query:
            # User text is a literal substring. LIKE wildcards must not widen the match.
            literal = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("filename LIKE ? ESCAPE '\\'")
            args.append(f"%{literal}%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = self.db.one("SELECT COUNT(*) AS count FROM media" + where, args)["count"]
        items = self.db.all(
            "SELECT * FROM media" + where + " ORDER BY created DESC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        )
        for item in items:
            item["locked"] = bool(item["locked"])
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def delete(self, media_id):
        with self.lock:
            item = self.get(media_id)
            if item["locked"]:
                raise StorageError("Unlock this item before deleting it")
            path = self.path(item["filename"])
            trash = path.with_suffix(path.suffix + ".deleted")
            if path.exists():
                path.replace(trash)
            try:
                self.db.execute("DELETE FROM media WHERE id=?", (media_id,))
            except BaseException:
                if trash.exists():
                    trash.replace(path)
                raise
            trash.unlink(missing_ok=True)

    def apply_many(self, media_ids, action):
        if action not in {"lock", "unlock", "delete"}:
            raise StorageError("Media action is not allowed")
        results = []
        for media_id in media_ids:
            try:
                if action == "delete":
                    self.delete(media_id)
                else:
                    self.set_locked(media_id, action == "lock")
                results.append({"id": media_id, "ok": True})
            except KeyError:
                results.append({"id": media_id, "ok": False, "error": "Resource not found"})
            except StorageError as exc:
                results.append({"id": media_id, "ok": False, "error": str(exc)})
        return {"results": results}

    def set_locked(self, media_id, locked):
        with self.lock:
            self.get(media_id)
            self.db.execute("UPDATE media SET locked=? WHERE id=?", (locked, media_id))
            return self.get(media_id)

    def reconcile(self):
        # Never claim that an interrupted MP4 is a complete recording.
        known = {row["filename"] for row in self.db.all("SELECT filename FROM media")}
        for path in self.root.iterdir():
            if path.is_file() and path.name.endswith(".deleted"):
                original = path.name.removesuffix(".deleted")
                if original in known:
                    path.replace(self.path(original))
                else:
                    path.unlink()
        for row in self.db.all("SELECT id,filename FROM media"):
            if not self.path(row["filename"]).is_file():
                self.db.execute("DELETE FROM media WHERE id=?", (row["id"],))
        orphans = [p.name for p in self.root.iterdir() if p.is_file() and p.name not in known]
        if orphans:
            self.bus.publish(
                "storage.recovery",
                "Unindexed or interrupted files retained for recovery",
                data={"files": len(orphans)},
            )

    def retention(self):
        cutoff = time.time() - self.settings.retention_days * 86400
        for item in self.db.all("SELECT id FROM media WHERE created<? AND locked=0", (cutoff,)):
            try:
                self.delete(item["id"])
            except (OSError, StorageError):
                continue
        self.db.execute("DELETE FROM events WHERE created<?", (cutoff,))
        self.db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
        self.db.execute(
            "DELETE FROM notification_outbox WHERE status!='queued' AND id NOT IN "
            "(SELECT id FROM notification_outbox WHERE status!='queued' ORDER BY id DESC LIMIT 200)"
        )
