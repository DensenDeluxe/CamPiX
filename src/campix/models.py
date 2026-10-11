from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CameraConfig(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$")
    name: str = Field(min_length=1, max_length=80)
    backend: Literal["synthetic", "picamera2", "v4l2", "network", "gstreamer", "legacy"]
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
    encoder: Literal["auto", "libx264", "h264_v4l2m2m"] = "auto"
    controls: dict[str, float | int | bool] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def source_valid(self):
        if self.backend == "picamera2" and not self.source.isdecimal():
            raise ValueError("Picamera2 source must be a camera index")
        if self.backend == "v4l2" and not __import__("re").fullmatch(r"/dev/video\d+", self.source):
            raise ValueError("V4L2 source must be /dev/videoN")
        if self.backend == "network":
            url = urlsplit(self.source)
            if url.scheme not in {"rtsp", "rtsps", "http", "https"} or not url.hostname:
                raise ValueError("Network source must be an RTSP or HTTP(S) camera URL")
        if self.backend == "gstreamer":
            raise ValueError(
                "GStreamer vendor pipelines require an installed adapter; arbitrary pipelines are disabled"
            )
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
    encoder: Literal["auto", "libx264", "h264_v4l2m2m"] | None = None


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
    end_hour: int = Field(24, ge=0, le=24)
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
