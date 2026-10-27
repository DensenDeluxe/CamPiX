from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 4


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=15)
        self.conn.row_factory = sqlite3.Row
        if self.conn.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
            self.conn.close()
            raise RuntimeError(
                "Database schema is newer than this CamPiX release; downgrade refused"
            )
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=15000")
        self.conn.executescript("""
          CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','operator','viewer')),
            disabled INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            csrf TEXT NOT NULL, expires REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS cameras (id TEXT PRIMARY KEY, config TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS media (
            id TEXT PRIMARY KEY, camera_id TEXT NOT NULL, kind TEXT NOT NULL,
            filename TEXT UNIQUE NOT NULL, created REAL NOT NULL, size INTEGER NOT NULL,
            duration REAL, locked INTEGER NOT NULL DEFAULT 0, trigger TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS media_created ON media(created DESC);
          CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, created REAL NOT NULL, kind TEXT NOT NULL,
            camera_id TEXT, message TEXT NOT NULL, data TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS events_created ON events(created DESC);
          CREATE TABLE IF NOT EXISTS schedules (
            id TEXT PRIMARY KEY, config TEXT NOT NULL, next_run REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS export_jobs (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, created REAL NOT NULL,
            media_id TEXT, error TEXT);
          CREATE TABLE IF NOT EXISTS notification_outbox (
            id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, created REAL NOT NULL,
            kind TEXT NOT NULL, camera_id TEXT, message TEXT NOT NULL,
            channel TEXT NOT NULL CHECK(channel IN ('webhook','mail')),
            status TEXT NOT NULL CHECK(status IN ('queued','delivered','failed')),
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL,
            last_error TEXT NOT NULL DEFAULT '');
          CREATE INDEX IF NOT EXISTS notification_due ON notification_outbox(status, next_attempt);
          CREATE TABLE IF NOT EXISTS share_links (
            id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, media_id TEXT NOT NULL,
            expires REAL NOT NULL, created REAL NOT NULL, created_by TEXT NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0);
          CREATE INDEX IF NOT EXISTS share_media ON share_links(media_id);
          PRAGMA user_version=4;
        """)
        path.chmod(0o600)

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                yield self.conn
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def execute(self, sql, args=()):
        with self.transaction() as conn:
            return conn.execute(sql, args).lastrowid

    def one(self, sql, args=()):
        with self.lock:
            row = self.conn.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        self.execute(
            "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def event(self, kind, message, camera_id=None, data=None):
        now = time.time()
        event_id = self.execute(
            "INSERT INTO events(created,kind,camera_id,message,data) VALUES (?,?,?,?,?)",
            (now, kind, camera_id, message, json.dumps(data or {})),
        )
        return {
            "id": event_id,
            "created": now,
            "kind": kind,
            "camera_id": camera_id,
            "message": message,
            "data": data or {},
        }

    def close(self):
        with self.lock:
            self.conn.close()
