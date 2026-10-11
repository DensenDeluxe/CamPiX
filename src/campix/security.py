from __future__ import annotations

import hashlib
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


class Auth:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.limiter = RateLimiter()

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
        if HASHER.check_needs_rehash(row["password"]):
            self.db.execute(
                "UPDATE users SET password=? WHERE id=?", (HASHER.hash(password), row["id"])
            )
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        self.db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?)",
            (digest(token), row["id"], csrf, time.time() + self.settings.session_hours * 3600),
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
        if not session:
            raise HTTPException(401, "Sign in required")
        if ROLES[session["role"]] < ROLES[role]:
            raise HTTPException(403, "Insufficient permissions")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not self.origin_allowed(request.headers.get("origin"), request.url):
                raise HTTPException(403, "Cross-site request rejected")
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
                raise HTTPException(403, "Invalid CSRF token")
            self.limiter.check("writes:" + str(session["id"]), 120, 60)
        return session
