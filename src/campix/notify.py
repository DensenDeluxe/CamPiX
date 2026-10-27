"""Fixed-catalog notifications. No macros, shells, or caller-supplied commands.

Only motion, a stalled camera, the storage reserve, and a failed export leave
the service. Each delivery is either one signed HTTPS webhook or one SMTP
message to the local mailer. Payloads use the catalog text, never event data.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import re
import smtplib
import socket
import ssl
import threading
import time
from email.message import EmailMessage
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .models import StrictModel

LOG = logging.getLogger(__name__)

CATALOG = ("motion.detected", "camera.stalled", "storage.reserve", "export.failed")
MESSAGES = {
    "motion.detected": "Motion detected",
    "camera.stalled": "Frame delivery stalled; waiting for the camera driver",
    "storage.reserve": "Storage reserve reached; free space before recording",
    "export.failed": "Export failed; inputs were not modified",
}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
BLOCKED_HOSTS = {"metadata.google.internal"}
SIGNATURE_TOLERANCE = 300
MAX_QUEUED = 100
MAX_ATTEMPTS = 3
BACKOFF = (2, 8, 30)


class WebhookChannel(StrictModel):
    enabled: bool = False
    url: str = Field("", max_length=2048)


class MailChannel(StrictModel):
    enabled: bool = False
    recipient: str = Field("", max_length=320)
    port: int = Field(25, ge=1, le=65535)


class NotificationSettings(StrictModel):
    webhook: WebhookChannel = Field(default_factory=WebhookChannel)
    mail: MailChannel = Field(default_factory=MailChannel)
    kinds: list[str] = Field(default_factory=lambda: list(CATALOG), max_length=len(CATALOG))
    cameras: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def channels_valid(self):
        if self.webhook.url or self.webhook.enabled:
            validate_webhook_url(self.webhook.url)
        if self.mail.recipient or self.mail.enabled:
            if not mail_recipient_valid(self.mail.recipient):
                raise ValueError("Mail recipient is not allowed")
        if len(set(self.kinds)) != len(self.kinds) or any(kind not in CATALOG for kind in self.kinds):
            raise ValueError("Notification kind is not in the catalog")
        if any(not _camera_id_valid(camera_id) for camera_id in self.cameras):
            raise ValueError("Notification camera is not allowed")
        return self


def _camera_id_valid(value: str) -> bool:
    return bool(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}", value))


def catalog_message(kind: str, message: str) -> str:
    """Return the fixed catalog sentence. Export failures may name an exception type."""
    if kind == "export.failed":
        text = " ".join(str(message).split())
        prefix = "Export failed ("
        suffix = "); inputs were not modified"
        detail = text[len(prefix) : -len(suffix)] if text.startswith(prefix) else ""
        if text.endswith(suffix) and detail.isidentifier():
            return text
        return MESSAGES["export.failed"]
    return MESSAGES[kind]


def validate_webhook_url(url: str) -> str:
    if not isinstance(url, str) or not url or len(url) > 2048 or any(c in url for c in "\r\n\x00"):
        raise ValueError("Webhook URL is not allowed")
    parts = urlsplit(url)
    host = parts.hostname
    if (
        parts.username
        or parts.password
        or parts.fragment
        or not host
        or host.endswith(".")
        or host in BLOCKED_HOSTS
    ):
        raise ValueError("Webhook URL is not allowed")
    literal = _literal_address(host)
    if literal is not None and not address_allowed(literal):
        raise ValueError("Webhook URL is not allowed")
    loopback = host in LOOPBACK_HOSTS or (literal is not None and literal.is_loopback)
    if parts.scheme == "https" or (parts.scheme == "http" and loopback):
        return url
    raise ValueError("Webhook URL is not allowed")


def mail_recipient_valid(value: str) -> bool:
    if not isinstance(value, str) or len(value) > 320 or any(c in value for c in "\r\n\x00"):
        return False
    local, separator, domain = value.partition("@")
    if not separator or not local or not domain or "@" in domain:
        return False
    return local.isascii() and domain.isascii() and " " not in value and len(local) <= 64


def address_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        (ip.is_global or ip.is_private or ip.is_loopback)
        and not (ip.is_multicast or ip.is_unspecified or ip.is_link_local or ip.is_reserved)
    )


def webhook_targets(url: str, timeout: float = 5) -> list[str]:
    """Resolve the webhook and reject metadata, link-local, and unspecified addresses."""
    validate_webhook_url(url)
    parts = urlsplit(url)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    literal = _literal_address(host)
    if literal is not None:
        return [host]
    try:
        infos = _resolve(host, port, timeout)
    except (OSError, TimeoutError) as exc:
        raise ValueError("Webhook address is not allowed") from exc
    addresses = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not address_allowed(ip):
            raise ValueError("Webhook address is not allowed")
        if host in LOOPBACK_HOSTS and not ip.is_loopback:
            raise ValueError("Webhook address is not allowed")
        if info[4][0] not in addresses:
            addresses.append(info[4][0])
    if not addresses:
        raise ValueError("Webhook address is not allowed")
    return addresses


def notification_body(event_id: int, created: float, kind: str, camera_id, message: str) -> bytes:
    payload = {
        "camera_id": camera_id,
        "created": created,
        "id": event_id,
        "kind": kind,
        "message": message,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def sign_delivery(secret: str, timestamp: int, body: bytes) -> str:
    signed = str(int(timestamp)).encode() + b"." + body
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def signature_valid(secret: str, timestamp: str, body: bytes, signature: str, now: float) -> bool:
    try:
        issued = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(now - issued) > SIGNATURE_TOLERANCE or not signature.startswith("v1="):
        return False
    expected = sign_delivery(secret, issued, body)
    return hmac.compare_digest(signature.removeprefix("v1="), expected)


class DeliveryError(RuntimeError):
    pass


def deliver_webhook(url: str, body: bytes, secret: str, timeout: float = 5) -> None:
    parts = urlsplit(validate_webhook_url(url))
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    targets = webhook_targets(url, timeout)
    timestamp = int(time.time())
    signature = sign_delivery(secret, timestamp, body)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    headers = {
        "Host": _authority(host, port, parts.port is not None),
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "CamPiX-Timestamp": str(timestamp),
        "CamPiX-Signature": f"v1={signature}",
        "User-Agent": "CamPiX",
        "Connection": "close",
    }
    # A refused connection can be one of several resolved addresses. An HTTP
    # rejection is the server's answer and stops the attempt.
    for target in targets:
        try:
            status = _post_webhook(parts.scheme, host, port, target, path, body, headers, timeout)
        except (OSError, TimeoutError, http.client.HTTPException):
            continue
        if 200 <= status < 300:
            return
        raise DeliveryError("Webhook delivery was rejected")
    raise DeliveryError("Webhook delivery failed")


def _post_webhook(scheme, host, port, target, path, body, headers, timeout) -> int:
    sock = socket.create_connection((target, port), timeout)
    try:
        if scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        connection.sock = sock
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        status = response.status
        response.read(1024)
        response.close()
        return status
    finally:
        sock.close()


def deliver_mail(recipient: str, port: int, body: str, timeout: float = 5) -> None:
    if not mail_recipient_valid(recipient):
        raise DeliveryError("Mail recipient is not allowed")
    message = EmailMessage()
    message["From"] = "CamPiX <campix@localhost>"
    message["To"] = recipient
    message["Subject"] = "CamPiX notification"
    message.set_content(body)
    try:
        with smtplib.SMTP("127.0.0.1", port, timeout=timeout) as client:
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise DeliveryError("Local mail delivery failed") from exc


def _literal_address(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _authority(host: str, port: int, explicit: bool) -> str:
    formatted = f"[{host}]" if ":" in host else host
    if explicit:
        return f"{formatted}:{port}"
    return formatted


def _resolve(host: str, port: int, timeout: float):
    result: dict[str, list] = {}
    error: dict[str, OSError] = {}

    def work():
        try:
            result["infos"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            error["exc"] = exc

    thread = threading.Thread(target=work, name="webhook-resolve", daemon=True)
    thread.start()
    thread.join(timeout)
    if "exc" in error:
        raise error["exc"]
    if thread.is_alive() or "infos" not in result:
        raise TimeoutError("Webhook address is not allowed")
    return result["infos"]


class NotificationService:
    """One worker, a bounded outbox, and retries that survive a process restart."""

    def __init__(self, db, bus):
        self.db = db
        self.cooldown = 30.0
        self.halt = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.recent: dict[tuple, float] = {}
        self.dropped = 0
        self.settings = NotificationSettings()
        self.secret = None
        self._load()
        bus.add_listener(self.observe)
        self.thread = threading.Thread(target=self._run, name="notifications", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.halt.set()
        self.wake.set()
        if self.thread.is_alive():
            self.thread.join(timeout=8)

    def update(self, settings: NotificationSettings):
        if settings.webhook.url:
            webhook_targets(settings.webhook.url)
        with self.lock:
            self.settings = settings
            if settings.webhook.enabled and not self.secret:
                self.secret = _new_secret()
                self.db.set_setting("notification_secret", self.secret)
            self.db.set_setting("notifications", settings.model_dump())
        self.wake.set()

    def rotate_secret(self) -> str:
        secret = _new_secret()
        with self.lock:
            self.secret = secret
            self.db.set_setting("notification_secret", secret)
        return secret

    def state(self) -> dict:
        with self.lock:
            settings = self.settings.model_dump()
            secret = self.secret
            dropped = self.dropped
        deliveries = self.db.all(
            "SELECT id,event_id,kind,camera_id,channel,status,attempts,last_error "
            "FROM notification_outbox ORDER BY id DESC LIMIT 20"
        )
        return {**settings, "secret": secret, "dropped": dropped, "deliveries": deliveries}

    def observe(self, event):
        kind = event.get("kind")
        if kind not in CATALOG:
            return
        with self.lock:
            settings = self.settings
            if kind not in settings.kinds:
                return
            camera_id = event.get("camera_id")
            if settings.cameras and camera_id and camera_id not in settings.cameras:
                return
            key = (kind, event.get("camera_id"))
            now = time.monotonic()
            if now - self.recent.get(key, 0) < self.cooldown:
                return
            channels = []
            if settings.webhook.enabled:
                channels.append("webhook")
            if settings.mail.enabled:
                channels.append("mail")
            if not channels:
                return
            self.recent[key] = now
            if len(self.recent) > 256:
                self.recent = dict(list(self.recent.items())[-128:])
        queued = self.db.one(
            "SELECT COUNT(*) AS count FROM notification_outbox WHERE status='queued'"
        )["count"]
        if queued >= MAX_QUEUED:
            with self.lock:
                self.dropped += 1
            return
        message = catalog_message(kind, event.get("message", ""))
        created = float(event["created"])
        for channel in channels:
            self.db.execute(
                "INSERT INTO notification_outbox"
                "(event_id,created,kind,camera_id,message,channel,status,attempts,next_attempt,last_error)"
                " VALUES (?,?,?,?,?,?,'queued',0,?,'')",
                (event["id"], created, kind, event.get("camera_id"), message, channel, created),
            )
        self.wake.set()

    def _load(self):
        saved = self.db.setting("notifications")
        secret = self.db.setting("notification_secret")
        with self.lock:
            self.settings = NotificationSettings.model_validate(saved or {})
            self.secret = secret if isinstance(secret, str) and secret else None

    def _run(self):
        while not self.halt.is_set():
            self.wake.wait(1)
            self.wake.clear()
            if self.halt.is_set():
                break
            row = self.db.one(
                "SELECT * FROM notification_outbox WHERE status='queued' AND next_attempt<=? "
                "ORDER BY id LIMIT 1",
                (time.time(),),
            )
            if row:
                self._deliver(row)

    def _deliver(self, row):
        with self.lock:
            settings = self.settings
            secret = self.secret
        channel = row["channel"]
        if channel == "webhook" and not settings.webhook.enabled:
            self._finish(row, "failed", "Notification channel is off")
            return
        if channel == "mail" and not settings.mail.enabled:
            self._finish(row, "failed", "Notification channel is off")
            return
        body = notification_body(
            row["event_id"], row["created"], row["kind"], row["camera_id"], row["message"]
        )
        try:
            if channel == "webhook":
                if not secret:
                    raise DeliveryError("Webhook signing secret is not set")
                deliver_webhook(settings.webhook.url, body, secret)
            elif channel == "mail":
                deliver_mail(settings.mail.recipient, settings.mail.port, row["message"])
            else:
                raise DeliveryError("Notification channel is off")
        except (DeliveryError, ValueError, OSError, TimeoutError) as exc:
            attempts = row["attempts"] + 1
            if attempts >= MAX_ATTEMPTS:
                self._finish(row, "failed", "Delivery failed", attempts)
            else:
                self.db.execute(
                    "UPDATE notification_outbox SET attempts=?, next_attempt=?, last_error=? WHERE id=?",
                    (
                        attempts,
                        time.time() + BACKOFF[min(attempts - 1, len(BACKOFF) - 1)],
                        "Delivery failed",
                        row["id"],
                    ),
                )
            LOG.warning(
                "Notification %s via %s failed: %s", row["kind"], channel, type(exc).__name__
            )
            return
        self._finish(row, "delivered", "")

    def _finish(self, row, status, error, attempts=None):
        self.db.execute(
            "UPDATE notification_outbox SET status=?, attempts=?, last_error=? WHERE id=?",
            (status, attempts if attempts is not None else row["attempts"], error, row["id"]),
        )
        self.db.execute(
            "DELETE FROM notification_outbox WHERE status!='queued' AND id NOT IN "
            "(SELECT id FROM notification_outbox WHERE status!='queued' ORDER BY id DESC LIMIT 200)"
        )


def _new_secret() -> str:
    import secrets

    return secrets.token_urlsafe(32)
