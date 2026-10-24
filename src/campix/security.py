from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from collections import deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request

HASHER = PasswordHasher(time_cost=3, memory_cost=32768, parallelism=2)
DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(32))
ROLES = {"viewer": 0, "operator": 1, "admin": 2}


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class RateLimiter:
    def __init__(self):
        self.entries: dict[str, deque] = {}
        self.lock = threading.Lock()

    def check(self, key, limit=10, window=60):
        now = time.monotonic()
        with self.lock:
            if len(self.entries) > 4096:
                self.entries = {k: v for k, v in self.entries.items() if v and v[-1] > now - 3600}
                if len(self.entries) > 4096:
                    raise HTTPException(429, "Rate limit capacity reached")
            times = self.entries.setdefault(key, deque())
            while times and times[0] < now - window:
                times.popleft()
            if len(times) >= limit:
                raise HTTPException(429, "Too many requests", headers={"Retry-After": str(window)})
            times.append(now)


class TokenError(RuntimeError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class ApiTokens:
    """Revocable viewer tokens for integrations. Only the SHA-256 digest is stored."""

    limit = 8

    def __init__(self, db):
        self.db = db

    def issue(self, name: str):
        cleaned = " ".join(str(name).split())
        if not cleaned or len(cleaned) > 40:
            raise TokenError("API token name is not allowed", 422)
        token = "cx_" + secrets.token_urlsafe(32)
        record = {
            "id": secrets.token_hex(8),
            "name": cleaned,
            "digest": digest(token),
            "role": "viewer",
            "created": time.time(),
        }
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            items = [item for item in self._read(conn) if self._valid(item)]
            if len(items) >= self.limit:
                raise TokenError("API token limit reached")
            items.append(record)
            self._write(conn, items)
        return token, self._public(record)

    def revoke(self, token_id: str) -> bool:
        if not isinstance(token_id, str) or not re.fullmatch(r"[a-f0-9]{16}", token_id):
            return False
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            items = self._read(conn)
            kept = []
            found = False
            for item in items:
                if isinstance(item, dict) and item.get("id") == token_id:
                    found = True
                    continue
                kept.append(item)
            if found:
                self._write(conn, kept)
            return found

    def public(self):
        return [self._public(item) for item in self._load()]

    def authenticate(self, header):
        if not isinstance(header, str) or not header or len(header) > 256:
            return None
        scheme, separator, value = header.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or value != value.strip():
            return None
        if not value.startswith("cx_") or len(value) > 128:
            return None
        hashed = digest(value)
        for item in self._load():
            if secrets.compare_digest(item["digest"], hashed):
                return {
                    "id": "token:" + item["id"],
                    "username": item["name"],
                    "role": "viewer",
                    "csrf": "",
                }
        return None

    def _load(self):
        raw = self.db.setting("api_tokens", [])
        if not isinstance(raw, list):
            return []
        return [item for item in raw if self._valid(item)]

    def _read(self, conn):
        row = conn.execute("SELECT value FROM settings WHERE key='api_tokens'").fetchone()
        if not row:
            return []
        try:
            raw = json.loads(row["value"])
        except json.JSONDecodeError as exc:
            raise TokenError("API tokens are unreadable") from exc
        if not isinstance(raw, list):
            raise TokenError("API tokens are unreadable")
        return raw

    def _write(self, conn, items):
        conn.execute(
            "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("api_tokens", json.dumps(items)),
        )

    def _valid(self, item) -> bool:
        if not isinstance(item, dict):
            return False
        created = item.get("created")
        return bool(
            item.get("role") == "viewer"
            and isinstance(item.get("id"), str)
            and re.fullmatch(r"[a-f0-9]{16}", item["id"])
            and isinstance(item.get("name"), str)
            and 1 <= len(item["name"]) <= 40
            and isinstance(item.get("digest"), str)
            and re.fullmatch(r"[a-f0-9]{64}", item["digest"])
            and isinstance(created, (int, float))
            and not isinstance(created, bool)
        )

    def _public(self, item):
        return {
            "id": item["id"],
            "name": item["name"],
            "role": "viewer",
            "created": item["created"],
        }


class Auth:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.limiter = RateLimiter()
        self.tokens = ApiTokens(db)

    def create_user(self, username, password, role="admin"):
        from .models import UserCreate

        user = UserCreate(username=username, password=password, role=role)
        return self.db.execute(
            "INSERT INTO users(username,password,role) VALUES (?,?,?)",
            (user.username, HASHER.hash(user.password), user.role),
        )

    def login(self, username, password, ip):
        self.limiter.check("login-ip:" + ip, 10, 60)
        self.limiter.check("login-user:" + username.lower(), 15, 300)
        row = self.db.one("SELECT * FROM users WHERE username=?", (username,))
        try:
            valid = HASHER.verify(row["password"] if row else DUMMY_HASH, password)
        except (VerificationError, InvalidHashError):
            valid = False
        if not valid or not row or row["disabled"]:
            raise HTTPException(401, "Invalid username or password")
        replacement_hash = (
            HASHER.hash(password) if HASHER.check_needs_rehash(row["password"]) else None
        )
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.db.transaction() as conn:
            # Serialize with account changes from both HTTP handlers and recovery CLI clients.
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
            if not current or any(
                current[key] != row[key] for key in ("password", "role", "disabled")
            ):
                raise HTTPException(401, "Invalid username or password")
            if replacement_hash is not None:
                conn.execute(
                    "UPDATE users SET password=? WHERE id=?", (replacement_hash, row["id"])
                )
            now = time.time()
            conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?)",
                (digest(token), row["id"], csrf, now + self.settings.session_hours * 3600),
            )
        return token, csrf, {"id": row["id"], "username": row["username"], "role": row["role"]}

    def session(self, token):
        if not token or len(token) > 256:
            return None
        return self.db.one(
            "SELECT u.id,u.username,u.role,s.csrf,s.expires FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND u.disabled=0",
            (digest(token), time.time()),
        )

    def origin_allowed(self, origin, request_url):
        if not origin:
            return True  # CLI clients still need the CSRF session secret for writes.
        from urllib.parse import urlsplit

        url = urlsplit(str(request_url))
        expected = (
            self.settings.public_origin or f"{url.scheme.replace('ws', 'http')}://{url.netloc}"
        )
        return secrets.compare_digest(origin.rstrip("/"), expected)

    def require(self, request: Request, role="viewer"):
        session = self.session(request.cookies.get("campix_session"))
        # A bearer token is not a cookie, so it cannot ride a browser form.
        # A valid session still wins and keeps the CSRF check.
        bearer = None if session else self.tokens.authenticate(request.headers.get("authorization"))
        principal = session or bearer
        if not principal:
            raise HTTPException(401, "Sign in required")
        if ROLES[principal["role"]] < ROLES[role]:
            raise HTTPException(403, "Insufficient permissions")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if session:
                if not self.origin_allowed(request.headers.get("origin"), request.url):
                    raise HTTPException(403, "Cross-site request rejected")
                if not secrets.compare_digest(
                    request.headers.get("x-csrf-token", ""), session["csrf"]
                ):
                    raise HTTPException(403, "Invalid CSRF token")
            self.limiter.check("writes:" + str(principal["id"]), 120, 60)
        return principal
