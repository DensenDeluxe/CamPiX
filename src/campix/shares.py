"""One-file share links. The raw token is returned once and only its digest is stored."""

from __future__ import annotations

import secrets
import time

from .security import digest


def issue(db, media_id: str, hours: int, created_by: str) -> dict:
    token = secrets.token_urlsafe(32)
    link_id = secrets.token_hex(8)
    created = time.time()
    expires = created + hours * 3600
    db.execute(
        "INSERT INTO share_links(id,token_hash,media_id,expires,created,created_by,revoked)"
        " VALUES (?,?,?,?,?,?,0)",
        (link_id, digest(token), media_id, expires, created, created_by),
    )
    return {
        "id": link_id,
        "token": token,
        "media_id": media_id,
        "expires": expires,
        "path": f"/s/{token}",
    }


def resolve(db, token: str):
    if not isinstance(token, str) or not token or len(token) > 128:
        return None
    row = db.one("SELECT * FROM share_links WHERE token_hash=?", (digest(token),))
    if not row or row["revoked"] or float(row["expires"]) <= time.time():
        return None
    return row


def revoke(db, link_id: str) -> bool:
    if not isinstance(link_id, str) or len(link_id) != 16:
        return False
    row = db.one("SELECT id FROM share_links WHERE id=? AND revoked=0", (link_id,))
    if not row:
        return False
    db.execute("UPDATE share_links SET revoked=1 WHERE id=?", (link_id,))
    return True


def revoke_media(db, media_id: str) -> None:
    db.execute("UPDATE share_links SET revoked=1 WHERE media_id=?", (media_id,))


def public(db) -> list[dict]:
    now = time.time()
    rows = db.all(
        "SELECT id,media_id,expires,created,created_by,revoked FROM share_links ORDER BY created DESC LIMIT 100"
    )
    for row in rows:
        row["revoked"] = bool(row["revoked"])
        row["expired"] = float(row["expires"]) <= now
    return rows
