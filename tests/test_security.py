import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from campix.config import Settings
from campix.db import Database
from campix.security import HASHER, Auth


@pytest.fixture
def auth(tmp_path):
    db = Database(tmp_path / "security.sqlite3")
    auth = Auth(db, Settings(data_dir=tmp_path))
    auth.create_user("audit", "test-only-original-password")
    try:
        yield auth
    finally:
        db.close()


def pause_verification(monkeypatch, needs_rehash=False):
    verified, resume = Event(), Event()

    def verify(encoded, password):
        valid = HASHER.verify(encoded, password)
        verified.set()
        assert resume.wait(5), "Password verification was not resumed"
        return valid

    monkeypatch.setattr(
        "campix.security.HASHER",
        SimpleNamespace(
            verify=verify,
            check_needs_rehash=lambda encoded: needs_rehash,
            hash=HASHER.hash,
        ),
    )
    return verified, resume


@pytest.mark.parametrize("change", ["password", "disabled", "role"])
def test_login_rejects_account_changes_during_verification(auth, monkeypatch, change):
    token, _, user = auth.login("audit", "test-only-original-password", "existing-client")
    verified, resume = pause_verification(monkeypatch)

    with ThreadPoolExecutor(max_workers=1) as workers:
        login = workers.submit(auth.login, "audit", "test-only-original-password", "new-client")
        try:
            assert verified.wait(5)
            changes = {
                "password": HASHER.hash("test-only-replacement-password"),
                "disabled": 1,
                "role": "viewer",
            }
            with auth.db.transaction() as conn:
                conn.execute(f"UPDATE users SET {change}=? WHERE id=?", (changes[change], user["id"]))
                conn.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        finally:
            resume.set()

        with pytest.raises(HTTPException) as failure:
            login.result(timeout=5)
        assert failure.value.status_code == 401

    assert auth.session(token) is None
    assert auth.db.one("SELECT COUNT(*) AS count FROM sessions")["count"] == 0


def test_stale_login_rehash_does_not_restore_a_rotated_password(auth, monkeypatch):
    rotated_hash = HASHER.hash("test-only-replacement-password")
    verified, resume = pause_verification(monkeypatch, needs_rehash=True)

    with ThreadPoolExecutor(max_workers=1) as workers:
        login = workers.submit(auth.login, "audit", "test-only-original-password", "new-client")
        try:
            assert verified.wait(5)
            with auth.db.transaction() as conn:
                conn.execute("UPDATE users SET password=? WHERE username='audit'", (rotated_hash,))
                conn.execute("DELETE FROM sessions")
        finally:
            resume.set()

        try:
            login.result(timeout=5)
        except HTTPException as failure:
            assert failure.status_code == 401

    assert auth.db.one("SELECT password FROM users WHERE username='audit'")["password"] == rotated_hash
    assert auth.db.one("SELECT COUNT(*) AS count FROM sessions")["count"] == 0


def test_login_serializes_session_creation_with_external_account_rotation(auth, monkeypatch):
    checked, resume = Event(), Event()
    original = auth.db.conn

    class ConnectionGate:
        def __getattr__(self, name):
            return getattr(original, name)

        def execute(self, sql, args=()):
            cursor = original.execute(sql, args)
            if sql == "SELECT * FROM users WHERE id=?":
                checked.set()
                assert resume.wait(5), "Account verification was not resumed"
            return cursor

    monkeypatch.setattr(auth.db, "conn", ConnectionGate())
    with ThreadPoolExecutor(max_workers=1) as workers:
        login = workers.submit(auth.login, "audit", "test-only-original-password", "new-client")
        try:
            assert checked.wait(5)
            with closing(sqlite3.connect(auth.db.path, timeout=0)) as external, external:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    external.execute("UPDATE users SET disabled=1 WHERE username='audit'")
        finally:
            resume.set()
        token, _, _ = login.result(timeout=5)

    with closing(sqlite3.connect(auth.db.path)) as external, external:
        external.execute(
            "UPDATE users SET password=? WHERE username='audit'",
            (HASHER.hash("test-only-replacement-password"),),
        )
        external.execute("DELETE FROM sessions")
    assert auth.session(token) is None
