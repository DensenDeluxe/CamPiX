"""Admission guards and load-shed levels.

These limits keep a host inside a memory and thermal budget. They do not
certify a board, sensor, or encoder.
"""

from __future__ import annotations

_MIB = 1024 * 1024


def shed_level_for(temperature_c: float | None, load_ratio: float | None) -> int:
    """Return 0, 1 or 2. Temperature and per-core load are optional observations."""
    temperature = temperature_c if isinstance(temperature_c, (int, float)) else None
    load = load_ratio if isinstance(load_ratio, (int, float)) else None
    if temperature is not None and temperature >= 82:
        return 2
    if load is not None and load >= 4:
        return 2
    if temperature is not None and temperature >= 75:
        return 1
    if load is not None and load >= 3:
        return 1
    return 0


def admission_reason(
    *,
    width: int,
    height: int,
    fps: int,
    memory_total: int,
    camera_count: int,
    model: str = "",
) -> str | None:
    """Return a conflict message when a profile exceeds the host budget."""
    if camera_count < 1:
        raise ValueError("camera_count must include the profile being saved")
    pixels = width * height
    if memory_total < 0:
        raise ValueError("memory_total must be nonnegative")
    # Rough resident set: a short JPEG window plus the encoded hub, per camera.
    estimate = pixels * 2 + 48 * _MIB
    if memory_total and estimate * camera_count + 96 * _MIB > memory_total:
        return "This profile exceeds the host memory budget for the configured cameras"
    if memory_total and memory_total <= 512 * _MIB:
        if camera_count > 1 or pixels > 1280 * 720 or fps > 15:
            return "This host memory budget allows one camera up to 1280x720 at 15 FPS"
    elif memory_total and memory_total <= 1024 * _MIB:
        if pixels > 1920 * 1080 or fps > 30:
            return "This host memory budget allows at most 1920x1080 at 30 FPS"
        if camera_count > 1 and (pixels > 1280 * 720 or fps > 30):
            return "With more than one camera this host budget allows 1280x720 at 30 FPS"
    pi5 = "Raspberry Pi 5" in model or "Compute Module 5" in model
    if pi5 and camera_count > 1 and (pixels > 1280 * 720 or fps > 30):
        return "Pi 5 software H.264 budget starts at two 1280x720 streams; reduce this profile"
    if pi5 and camera_count == 1 and (pixels > 1920 * 1080 or fps > 30):
        return "Pi 5 software H.264 budget for one camera is 1920x1080 at 30 FPS"
    return None
