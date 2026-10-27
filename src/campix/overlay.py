"""Burn a fixed UTC clock into a JPEG. The text is not caller-supplied."""

from __future__ import annotations

import io
from datetime import UTC, datetime

from PIL import Image, ImageDraw

_STAMP_LIMIT = 8 * 1024 * 1024


def stamp_jpeg(payload: bytes, moment: datetime | None = None) -> bytes:
    """Return a new JPEG with a UTC timestamp. Recordings that copy H.264 stay unstamped."""
    if not isinstance(payload, bytes | bytearray) or not payload.startswith(b"\xff\xd8"):
        raise ValueError("Timestamp overlay expects a JPEG")
    if len(payload) > _STAMP_LIMIT:
        raise ValueError("Timestamp overlay JPEG exceeds its size limit")
    when = (moment or datetime.now(UTC)).astimezone(UTC)
    text = when.strftime("%Y-%m-%d %H:%M:%S UTC")
    with Image.open(io.BytesIO(payload)) as image:
        frame = image.convert("RGB")
        draw = ImageDraw.Draw(frame)
        left, top, right, bottom = draw.textbbox((0, 0), text)
        width, height = right - left + 16, bottom - top + 10
        draw.rectangle((8, 8, 8 + width, 8 + height), fill=(0, 0, 0))
        draw.text((16, 12), text, fill=(255, 255, 255))
        output = io.BytesIO()
        frame.save(output, format="JPEG", quality=90)
    stamped = output.getvalue()
    if not stamped.startswith(b"\xff\xd8"):
        raise ValueError("Timestamp overlay did not produce a JPEG")
    return stamped
