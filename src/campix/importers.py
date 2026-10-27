"""Sanitized configuration import. Commands, macros, and secrets are not accepted."""

from __future__ import annotations

import re

from pydantic import ValidationError

from .models import CameraConfig, Schedule

_LINE = re.compile(r"^(?P<key>[A-Za-z0-9_]{1,40})(?:\s+(?P<value>[A-Za-z0-9._-]{1,32}))?\s*$")
_FORBIDDEN = ("cmd", "macro", "pipe", "script", "exec", "annotation")
_WIDTH = {"image_width", "width"}
_HEIGHT = {"image_height", "height"}
_FPS = {"fps_preview", "framerate", "fps"}
_MOTION = {"motion_detection", "motion_enabled"}


class ConfigImportError(ValueError):
    """A fixed import failure. The message does not include file contents."""


def parse_raspimjpeg(text: str) -> dict:
    """Map a classic size/rate/motion subset. Any command-like line rejects the file."""
    if not isinstance(text, str) or len(text) > 65536:
        raise ConfigImportError("Import is too large")
    proposal: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if any(mark in line for mark in ("`", "$(", "${", "|", ";", "&", "<", ">")):
            raise ConfigImportError("Import contains commands and was rejected")
        match = _LINE.fullmatch(line)
        if not match:
            raise ConfigImportError("Import contains a line that is not a simple setting")
        key = match.group("key").lower()
        value = match.group("value") or ""
        if key in _FORBIDDEN or key.startswith("cmd"):
            raise ConfigImportError("Import contains commands and was rejected")
        if key in _WIDTH:
            proposal["width"] = _even(value, "width")
        elif key in _HEIGHT:
            proposal["height"] = _even(value, "height")
        elif key in _FPS:
            proposal["fps"] = _bounded(value, 1, 60, "frame rate")
        elif key in _MOTION:
            proposal["motion_enabled"] = value.lower() in {"1", "true", "on", "yes"}
    if not proposal:
        raise ConfigImportError("Import did not contain a supported image setting")
    return proposal


def _even(value: str, label: str) -> int:
    high = 4096 if label == "width" else 2160
    low = 160 if label == "width" else 120
    number = _bounded(value, low, high, label)
    if number % 2:
        raise ConfigImportError(f"Import {label} must be an even number")
    return number


def _storage(value) -> dict:
    if not isinstance(value, dict) or set(value) - {"retention_days", "min_free_mb"}:
        raise ConfigImportError("Import storage settings are not valid")
    retention, reserve = value.get("retention_days"), value.get("min_free_mb")
    if type(retention) is not int or not 1 <= retention <= 3650:
        raise ConfigImportError("Import storage settings are not valid")
    if type(reserve) is not int or not 32 <= reserve <= 1_048_576:
        raise ConfigImportError("Import storage settings are not valid")
    return {"retention_days": retention, "min_free_mb": reserve}


def _bounded(value: str, low: int, high: int, label: str) -> int:
    if not value.isdecimal():
        raise ConfigImportError(f"Import {label} must be a whole number")
    number = int(value)
    if number < low or number > high:
        raise ConfigImportError(f"Import {label} is outside the supported range")
    return number


def prepare_import(document: dict, existing: dict[str, dict]) -> tuple[list[CameraConfig], list[Schedule], dict | None]:
    """Validate an export document before any camera or schedule is replaced."""
    if not isinstance(document, dict) or set(document) - {"version", "cameras", "schedules", "storage"}:
        raise ConfigImportError("Import document is not a CamPiX configuration export")
    cameras_in = document.get("cameras", [])
    schedules_in = document.get("schedules", [])
    if not isinstance(cameras_in, list) or not isinstance(schedules_in, list):
        raise ConfigImportError("Import document is not a CamPiX configuration export")
    if len(cameras_in) > 32 or len(schedules_in) > 64:
        raise ConfigImportError("Import contains more entries than this service accepts")
    cameras = []
    for raw in cameras_in:
        if not isinstance(raw, dict):
            raise ConfigImportError("Import camera is not an object")
        profile = dict(raw)
        previous = existing.get(str(profile.get("id", "")))
        if profile.get("source") == "[redacted]":
            if not previous:
                raise ConfigImportError("A redacted network source cannot be imported as a new camera")
            profile["source"] = previous["source"]
        if not profile.get("onvif_password") and previous:
            profile["onvif_password"] = previous.get("onvif_password", "")
        profile.pop("onvif_password_set", None)
        profile.pop("onvif_credentials_set", None)
        try:
            cameras.append(CameraConfig.model_validate(profile))
        except ValidationError as exc:
            raise ConfigImportError("Import camera is not valid") from exc
    schedules = []
    for raw in schedules_in:
        if not isinstance(raw, dict):
            raise ConfigImportError("Import schedule is not an object")
        payload = {key: value for key, value in raw.items() if key != "next_run"}
        try:
            schedules.append(Schedule.model_validate(payload))
        except ValidationError as exc:
            raise ConfigImportError("Import schedule is not valid") from exc
    storage = None
    if "storage" in document and document["storage"] is not None:
        storage = _storage(document["storage"])
    known = {camera.id for camera in cameras} | set(existing)
    if any(schedule.camera_id not in known for schedule in schedules):
        raise ConfigImportError("Import schedule names a camera that is not in this service")
    return cameras, schedules, storage
