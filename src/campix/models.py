from __future__ import annotations

import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EncoderName = Literal[
    "auto",
    "libx264",
    "h264_nvenc",
    "h264_qsv",
    "h264_amf",
    "h264_videotoolbox",
    "h264_vaapi",
    "h264_v4l2m2m",
]


def directshow_source_valid(source: str) -> bool:
    """FFmpeg splits DirectShow input on ":"; a name must not add audio or other devices."""
    return bool(re.fullmatch(r'[^\x00-\x1f\x7f":]{1,256}', source)) and source == source.strip()


def motion_mask_valid(value: str) -> str:
    """A 16×9 ignore mask. 1 skips a cell. An empty mask analyzes the whole frame."""
    if value == "":
        return value
    if len(value) != 144 or any(cell not in "01" for cell in value):
        raise ValueError("Motion mask must be empty or 144 characters of 0 and 1")
    if set(value) == {"1"}:
        raise ValueError("Motion mask must leave at least one cell active")
    if set(value) == {"0"}:
        return ""
    return value


def audio_source_valid(backend: str, source: str, enabled: bool) -> None:
    """Reject any audio name that could add a second FFmpeg input. Empty means off."""
    if backend in {"legacy", "gstreamer"}:
        if enabled or source:
            raise ValueError("This camera backend has no separate audio stream")
        return
    if backend == "synthetic":
        if source:
            raise ValueError("Synthetic audio uses the generated stream and accepts no device")
        return
    if backend == "network":
        if source:
            raise ValueError("Network audio uses the camera URL and accepts no separate device")
        return
    if not enabled and source == "":
        return
    if backend == "dshow":
        if not directshow_source_valid(source):
            raise ValueError(
                "DirectShow audio source must be a device name without quotes, colons, or control characters"
            )
        return
    if backend in {"v4l2", "picamera2"}:
        if not re.fullmatch(
            r"(?:default|hw:\d{1,2}(?:,\d{1,2})?|plughw:\d{1,2}(?:,\d{1,2})?)", source
        ):
            raise ValueError("ALSA audio source must be default, hw:N, or plughw:N,M")
        return
    if backend == "avfoundation":
        if not re.fullmatch(r"\d{1,2}", source) or int(source) > 32:
            raise ValueError("AVFoundation audio source must be a device index from 0 to 32")
        return
    raise ValueError("This camera backend has no separate audio stream")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class MotionRegion(StrictModel):
    """Normalized rectangle in the analysis frame. Origin is the top left."""

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)


class CameraConfig(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$")
    name: str = Field(min_length=1, max_length=80)
    backend: Literal[
        "synthetic", "picamera2", "v4l2", "dshow", "avfoundation", "network", "gstreamer", "legacy"
    ]
    source: str = Field("0", max_length=2048)
    width: int = Field(1280, ge=160, le=4096, multiple_of=2)
    height: int = Field(720, ge=120, le=2160, multiple_of=2)
    fps: int = Field(10, ge=1, le=60)
    quality: int = Field(80, ge=20, le=95)
    enabled: bool = True
    preroll_seconds: int = Field(3, ge=0, le=10)
    motion_enabled: bool = False
    motion_threshold: float = Field(0.08, ge=0.005, le=0.9)
    motion_pixel_delta: int = Field(25, ge=5, le=200)
    motion_frames: int = Field(3, ge=1, le=30)
    motion_postroll: int = Field(10, ge=1, le=300)
    motion_regions: list[MotionRegion] = Field(default_factory=list, max_length=8)
    motion_mask: str = ""
    audio_enabled: bool = False
    audio_source: str = Field("", max_length=256)
    encoder: EncoderName = "auto"
    controls: dict[str, float | int | bool] = Field(default_factory=dict, max_length=32)
    timestamp_overlay: bool = False
    viewer_users: list[str] = Field(default_factory=list, max_length=16)
    onvif_ptz: bool = False
    onvif_port: int = Field(80, ge=1, le=65535)
    onvif_username: str = Field("", max_length=64)
    onvif_password: str = Field("", max_length=128)

    @model_validator(mode="after")
    def source_valid(self):
        if self.backend == "picamera2" and not self.source.isdecimal():
            raise ValueError("Picamera2 source must be a camera index")
        if self.backend == "v4l2" and not __import__("re").fullmatch(r"/dev/video\d+", self.source):
            raise ValueError("V4L2 source must be /dev/videoN")
        if self.backend == "dshow" and not directshow_source_valid(self.source):
            raise ValueError(
                "DirectShow source must be a device name without quotes, colons, or control characters"
            )
        if self.backend == "avfoundation" and (
            not self.source.isdecimal() or int(self.source) > 32
        ):
            raise ValueError("AVFoundation source must be a video device index from 0 to 32")
        if self.backend == "network":
            url = urlsplit(self.source)
            if url.scheme not in {"rtsp", "rtsps", "http", "https"} or not url.hostname:
                raise ValueError("Network source must be an RTSP or HTTP(S) camera URL")
        if self.backend == "gstreamer":
            raise ValueError(
                "GStreamer vendor pipelines require an installed adapter; arbitrary pipelines are disabled"
            )
        for region in self.motion_regions:
            if region.x + region.w > 1 or region.y + region.h > 1:
                raise ValueError("Motion region must stay inside the frame")
        self.motion_mask = motion_mask_valid(self.motion_mask)
        audio_source_valid(self.backend, self.audio_source, self.audio_enabled)
        if len(set(self.viewer_users)) != len(self.viewer_users) or any(
            not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", name) for name in self.viewer_users
        ):
            raise ValueError("Viewer name is not allowed")
        if self.onvif_ptz and self.backend != "network":
            raise ValueError("PTZ is available only for a network camera")
        if self.onvif_username and not re.fullmatch(r"[A-Za-z0-9._@-]{1,64}", self.onvif_username):
            raise ValueError("ONVIF username is not allowed")
        if any(char in self.onvif_password for char in "\r\n\x00"):
            raise ValueError("ONVIF password is not allowed")
        if self.backend == "legacy":
            from pathlib import Path

            if not Path(self.source).is_absolute():
                raise ValueError("Legacy source must be an absolute preview JPEG path")
        return self

    @field_validator("controls")
    @classmethod
    def valid_controls(cls, values):
        import math

        allowed = {"brightness", "contrast", "saturation", "exposure", "autofocus", "lens_position"}
        if set(values) - allowed or any(not math.isfinite(float(v)) for v in values.values()):
            raise ValueError("Unsupported or non-finite control")
        return values


class Login(StrictModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class CameraPatch(StrictModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    width: int | None = Field(None, ge=160, le=4096, multiple_of=2)
    height: int | None = Field(None, ge=120, le=2160, multiple_of=2)
    fps: int | None = Field(None, ge=1, le=60)
    enabled: bool | None = None
    motion_enabled: bool | None = None
    motion_threshold: float | None = Field(None, ge=0.005, le=0.9)
    preroll_seconds: int | None = Field(None, ge=0, le=10)
    encoder: EncoderName | None = None
    motion_regions: list[MotionRegion] | None = Field(None, max_length=8)
    motion_mask: str | None = None
    audio_enabled: bool | None = None
    audio_source: str | None = Field(None, max_length=256)
    timestamp_overlay: bool | None = None
    viewer_users: list[str] | None = Field(None, max_length=16)
    onvif_ptz: bool | None = None
    onvif_port: int | None = Field(None, ge=1, le=65535)
    onvif_username: str | None = Field(None, max_length=64)
    onvif_password: str | None = Field(None, max_length=128)


class UserCreate(StrictModel):
    username: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    password: str = Field(min_length=12, max_length=128)
    role: Literal["admin", "operator", "viewer"] = "viewer"


class ControlPatch(StrictModel):
    brightness: float | None = Field(None, ge=-1, le=1)
    contrast: float | None = Field(None, ge=0, le=4)
    saturation: float | None = Field(None, ge=0, le=4)
    exposure: float | None = Field(None, ge=-8, le=8)
    autofocus: bool | None = None
    lens_position: float | None = Field(None, ge=0, le=32)


class RecordingRequest(StrictModel):
    duration: int | None = Field(None, ge=1, le=86400)


class TimelapseRequest(StrictModel):
    media_ids: list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]] = Field(
        min_length=2, max_length=500
    )
    fps: int = Field(10, ge=1, le=30)

    @field_validator("media_ids")
    @classmethod
    def unique_ids(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Each snapshot may be selected once")
        return values


class Schedule(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$")
    name: str = Field(min_length=1, max_length=80)
    camera_id: str
    action: Literal["snapshot", "record"] = "snapshot"
    interval_seconds: int = Field(3600, ge=5, le=604800)
    duration_seconds: int = Field(30, ge=1, le=3600)
    enabled: bool = True
    days: list[int] = Field(default_factory=lambda: list(range(7)), max_length=7)
    start_hour: int = Field(0, ge=0, le=23)
    start_minute: int = Field(0, ge=0, le=59)
    end_hour: int = Field(24, ge=0, le=24)
    end_minute: int = Field(0, ge=0, le=59)
    timezone: str = "Europe/Berlin"

    @field_validator("days")
    @classmethod
    def valid_days(cls, value):
        if not value or any(day < 0 or day > 6 for day in value) or len(set(value)) != len(value):
            raise ValueError("days must contain distinct weekday numbers 0–6")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unknown IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def recording_fits_interval(self, info):
        # Stored rows may predate this rule. The scheduler still runs them and
        # postpones an overlapping take instead of rejecting the saved rule.
        if info.context and info.context.get("allow_overlap"):
            return self
        if self.action == "record" and self.duration_seconds >= self.interval_seconds:
            raise ValueError("Recording duration must be shorter than the schedule interval")
        if self.end_hour == 24 and self.end_minute != 0:
            raise ValueError("End minute must be 0 when the window ends at hour 24")
        return self
