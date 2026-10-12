from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    data_dir: Path = Path(".campix")
    host: str = "127.0.0.1"
    port: int = Field(8080, ge=1, le=65535)
    public_origin: str | None = None
    secure_cookies: bool = False
    demo: bool = False
    session_hours: int = Field(12, ge=1, le=168)
    min_free_mb: int = Field(256, ge=32)
    retention_days: int = Field(30, ge=1, le=3650)
    max_cameras: int = Field(8, ge=1, le=32)
    ffmpeg: str = "ffmpeg"
    mediamtx_api: str | None = None
    mediamtx_rtsp: str = "rtsp://127.0.0.1:8554"
    mediamtx_binary: str | None = None
    mediamtx_token: str | None = None
    mediamtx_hls_token: str | None = None
    trust_proxy: bool = False
    web_root: Path | None = None

    @field_validator("mediamtx_binary")
    @classmethod
    def executable(cls, value):
        if value:
            import shutil

            candidate = shutil.which(value) or value
            if not Path(candidate).is_file():
                raise ValueError("MediaMTX binary not found")
            return str(Path(candidate).resolve())
        return None

    @field_validator("public_origin")
    @classmethod
    def origin(cls, value):
        if value is None:
            return value
        from urllib.parse import urlsplit

        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
            or url.username
        ):
            raise ValueError("public_origin must be an HTTP(S) origin without a path")
        return value.rstrip("/")

    @field_validator("mediamtx_api")
    @classmethod
    def media_api(cls, value):
        if not value:
            return None
        from urllib.parse import urlsplit

        message = (
            "mediamtx_api must be an HTTP(S) origin with a valid host and port, without credentials"
        )
        try:
            url = urlsplit(value)
            port = url.port
        except ValueError:
            raise ValueError(message) from None
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or (port is not None and not 1 <= port <= 65535)
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
            or url.username is not None
            or "\\" in value
            or any(
                character.isspace() or ord(character) < 32 or ord(character) == 127
                for character in value
            )
        ):
            raise ValueError(message)
        return value.rstrip("/")

    @classmethod
    def load(cls, file: Path | None = None, **overrides):
        values = json.loads(file.read_text(encoding="utf-8")) if file else {}
        for name in cls.model_fields:
            key = "CAMPIX_" + name.upper()
            if key in os.environ:
                value = os.environ[key]
                if name in {"secure_cookies", "demo", "trust_proxy"}:
                    value = value.lower() in {"1", "true", "yes"}
                values[name] = value
        values.update({k: v for k, v in overrides.items() if v is not None})
        result = cls(**values)
        result.data_dir = result.data_dir.resolve()
        return result

    def resolved_web_root(self):
        if self.web_root:
            return self.web_root.resolve()
        checkout = Path(__file__).resolve().parents[2] / "dist"
        if (checkout / "index.html").is_file():
            return checkout
        return Path(sys.prefix) / "share" / "campix" / "web"

    def prepare(self):
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        (self.data_dir / "media").mkdir(mode=0o700, exist_ok=True)
