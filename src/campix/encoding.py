"""Allowlisted H.264 encoders with bounded, profile-specific usability checks.

Synthetic packets prove that an encoder can initialize and encode this profile.
They do not qualify a physical camera, sustained throughput, or a deployment.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Collection
from dataclasses import asdict, dataclass, replace
from itertools import islice
from pathlib import Path
from typing import Any

from PIL import Image

from .processes import _output_reader, owned_process

ENCODER_HARDWARE_NAMES = (
    "h264_nvenc",
    "h264_qsv",
    "h264_amf",
    "h264_videotoolbox",
    "h264_vaapi",
    "h264_v4l2m2m",
)
ENCODER_NAMES = ("libx264", *ENCODER_HARDWARE_NAMES)
PROBE_TIMEOUT_SECONDS = 5.0
PROBE_OUTPUT_LIMIT = 128 * 1024
PROBE_MEDIA_LIMIT = 2 * 1024 * 1024
PROBE_CACHE_LIMIT = 128
PROBE_SUCCESS_TTL_SECONDS = 300.0
PROBE_FAILURE_TTL_SECONDS = 30.0
PROBE_INPUT_LIMIT = 16 * 1024 * 1024


class EncoderUnavailable(RuntimeError):
    """A sanitized, actionable encoder selection failure."""


class _OutputLimit(ValueError):
    pass


class _ProbeCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class EncoderSelection:
    codec: str
    hardware: bool
    requested: str
    output_args: tuple[str, ...]
    input_args: tuple[str, ...] = ()
    fallback_reason: str | None = None
    probe_verified: bool = True
    hardware_verified: bool = False
    input_codec: str | None = None
    zero_copy_raw_frames: bool = False
    zero_copy_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _ProbeResult:
    codec: str
    usable: bool
    reason: str | None = None
    device: str | None = None


_cache: OrderedDict[tuple[Any, ...], tuple[float, _ProbeResult]] = OrderedDict()
_cache_lock = threading.Lock()
_probe_slots = threading.BoundedSemaphore(1)


def platform_encoders(platform: str | None = None) -> tuple[str, ...]:
    """Prioritize native, low-latency hardware backends before software."""
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        return ("h264_nvenc", "h264_qsv", "h264_amf", "libx264")
    if platform == "darwin":
        return ("h264_videotoolbox", "libx264")
    if platform.startswith("linux"):
        return ("h264_nvenc", "h264_qsv", "h264_vaapi", "h264_v4l2m2m", "libx264")
    return ("libx264",)


def parse_encoder_list(output: bytes | str) -> tuple[str, ...]:
    """Match actual video inventory rows, never descriptions or name substrings."""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    listed = set(re.findall(r"^\s*V[FSXBD.]{5}\s+(\w+)\s", output, re.MULTILINE))
    return tuple(codec for codec in ENCODER_NAMES if codec in listed)


def _profile(width: int, height: int, fps: int) -> None:
    if (
        type(width) is not int
        or type(height) is not int
        or type(fps) is not int
        or not 16 <= width <= 4096
        or not 16 <= height <= 2160
        or width % 2
        or height % 2
        or not 1 <= fps <= 60
    ):
        raise ValueError("Encoder profile requires even dimensions up to 4096x2160 and 1-60 FPS")


def encoder_args(
    codec: str, width: int, height: int, fps: int, *, device: str | None = None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return input and output arguments; device paths come only from render-node discovery."""
    _profile(width, height, fps)
    if codec not in ENCODER_NAMES:
        raise ValueError("Unsupported H.264 encoder")
    if device is not None and (
        codec != "h264_vaapi" or not re.fullmatch(r"/dev/dri/renderD\d{1,5}", device)
    ):
        raise ValueError("Unsupported encoder device")
    inputs: tuple[str, ...] = ()
    output = ["-c:v", codec, "-profile:v", "baseline", "-bf", "0", "-g", str(fps)]
    if codec == "libx264":
        # Half-second IDR interval, two threads and a bitrate ceiling. Hardware
        # encoders keep a one-second GOP; their rate control is already explicit.
        output[output.index("-g") + 1] = str(max(1, (fps + 1) // 2))
        bitrate = int(max(500_000, min(8_000_000, width * height * fps * 0.07)))
        output += [
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-threads",
            "2",
            "-crf",
            "23",
            "-maxrate",
            str(bitrate),
            "-bufsize",
            str(bitrate // 2),
        ]
    else:
        bitrate = int(max(500_000, min(100_000_000, width * height * fps * 0.1)))
        output += ["-b:v", str(bitrate)]
        if codec != "h264_videotoolbox":
            output += ["-maxrate", str(bitrate), "-bufsize", str(bitrate // 2)]
        if codec == "h264_nvenc":
            output += [
                "-pix_fmt",
                "nv12",
                "-preset",
                "p1",
                "-tune",
                "ull",
                "-rc",
                "cbr",
                "-rc-lookahead",
                "0",
                "-zerolatency",
                "1",
                "-delay",
                "0",
                "-forced-idr",
                "1",
            ]
        elif codec == "h264_qsv":
            # The internal Media SDK session may otherwise choose a software implementation.
            inputs = ("-init_hw_device", "qsv=campix:hw_any")
            output += [
                "-pix_fmt",
                "nv12",
                "-preset",
                "veryfast",
                "-look_ahead",
                "0",
                "-async_depth",
                "1",
            ]
        elif codec == "h264_amf":
            output += [
                "-pix_fmt",
                "nv12",
                "-usage",
                "ultralowlatency",
                "-quality",
                "speed",
                "-rc",
                "cbr",
                "-async_depth",
                "1",
            ]
        elif codec == "h264_videotoolbox":
            # VideoToolbox otherwise permits an invisible software implementation.
            output += ["-pix_fmt", "nv12", "-realtime", "1", "-allow_sw", "0"]
        elif codec == "h264_vaapi":
            if device is None:
                raise ValueError("VAAPI requires a discovered render device")
            inputs = ("-vaapi_device", device)
            output += ["-vf", "format=nv12,hwupload"]
        else:
            output += ["-pix_fmt", "yuv420p"]
    return inputs, tuple(output)


def _run_bounded(
    command: list[str],
    *,
    timeout: float,
    limit: int = PROBE_OUTPUT_LIMIT,
    media_path: Path | None = None,
    cancel: threading.Event | None = None,
) -> tuple[int, bytes]:
    """Drain bounded diagnostics and reap the child on all terminal paths."""
    if cancel is not None and cancel.is_set():
        raise _ProbeCancelled("Encoder probe canceled")
    output = bytearray()
    with owned_process(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ) as process:
        read = _output_reader(process.stdout.fileno())
        deadline = time.monotonic() + timeout
        while True:
            if cancel is not None and cancel.is_set():
                raise _ProbeCancelled("Encoder probe canceled")
            exited = process.poll() is not None
            chunk = read(min(8192, max(1, limit + 1 - len(output))))
            if chunk:
                output.extend(chunk)
                if len(output) > limit:
                    raise _OutputLimit("Encoder diagnostic output exceeds its size limit")
            if media_path is not None and media_path.exists():
                if media_path.stat().st_size > PROBE_MEDIA_LIMIT:
                    raise _OutputLimit("Encoder probe media exceeds its size limit")
            if not chunk and exited:
                return process.returncode, bytes(output)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            if not chunk:
                time.sleep(min(0.01, remaining))


def _binary(ffmpeg: str) -> tuple[str, tuple[Any, ...]] | None:
    resolved = shutil.which(ffmpeg)
    if resolved is None:
        return None
    try:
        stat = Path(resolved).stat()
    except OSError:
        return None
    return resolved, (
        os.path.normcase(str(Path(resolved).resolve())),
        stat.st_mtime_ns,
        stat.st_size,
    )


def _render_devices() -> tuple[str, ...]:
    """Use a bounded inventory of local character devices, excluding symlink aliases."""
    try:
        nodes = sorted(islice(Path("/dev/dri").glob("renderD*"), 64))
        return tuple(
            str(node)
            for node in nodes
            if re.fullmatch(r"/dev/dri/renderD\d{1,5}", str(node))
            and not node.is_symlink()
            and node.is_char_device()
        )[:4]
    except OSError:
        return ()


def _cached(key: tuple[Any, ...]) -> _ProbeResult | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires, result = entry
        if expires <= time.monotonic():
            del _cache[key]
            return None
        _cache.move_to_end(key)
        return result


def _remember(key: tuple[Any, ...], result: _ProbeResult) -> _ProbeResult:
    ttl = PROBE_SUCCESS_TTL_SECONDS if result.usable else PROBE_FAILURE_TTL_SECONDS
    with _cache_lock:
        _cache[key] = (time.monotonic() + ttl, result)
        _cache.move_to_end(key)
        while len(_cache) > PROBE_CACHE_LIMIT:
            _cache.popitem(last=False)
    return result


def invalidate_encoder_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _has_h264_packets(data: bytes) -> bool:
    types = {
        data[match.end()] & 0x1F
        for match in re.finditer(b"\x00\x00(?:\x00)?\x01", data)
        if match.end() < len(data)
    }
    return {5, 7, 8}.issubset(types)


def _smoke(
    ffmpeg: str,
    codec: str,
    width: int,
    height: int,
    fps: int,
    *,
    device: str | None,
    timeout: float,
    cancel: threading.Event | None,
) -> _ProbeResult:
    inputs, outputs = encoder_args(codec, width, height, fps, device=device)
    try:
        with tempfile.TemporaryDirectory(prefix="campix-encoder-") as directory:
            Path(directory).chmod(0o700)
            path = Path(directory) / "synthetic.h264"
            command = [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                *inputs,
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={width}x{height}:r={fps}",
                "-an",
                "-frames:v",
                "4",
                *outputs,
                "-fs",
                str(PROBE_MEDIA_LIMIT),
                "-f",
                "h264",
                str(path),
            ]
            code, _ = _run_bounded(command, timeout=timeout, media_path=path, cancel=cancel)
            if code != 0:
                return _ProbeResult(codec, False, "encode-failed", device)
            if not path.exists() or not 0 < path.stat().st_size <= PROBE_MEDIA_LIMIT:
                return _ProbeResult(codec, False, "no-encoded-packets", device)
            with path.open("rb") as stream:
                packets = stream.read(PROBE_MEDIA_LIMIT + 1)
            if not _has_h264_packets(packets):
                return _ProbeResult(codec, False, "invalid-h264-packets", device)
            return _ProbeResult(codec, True, device=device)
    except subprocess.TimeoutExpired:
        return _ProbeResult(codec, False, "probe-timed-out", device)
    except _OutputLimit:
        return _ProbeResult(codec, False, "probe-output-limit", device)
    except OSError:
        return _ProbeResult(codec, False, "probe-process-failed", device)
    except _ProbeCancelled:
        return _ProbeResult(codec, False, "probe-canceled", device)


def jpeg_dimensions(sample: bytes) -> tuple[int, int] | None:
    """Read baseline, extended or progressive SOF size without decoding pixels."""
    if not isinstance(sample, bytes) or len(sample) < 4 or not sample.startswith(b"\xff\xd8"):
        return None
    offset = 2
    try:
        while offset + 4 <= len(sample):
            if sample[offset] != 0xFF:
                return None
            while offset < len(sample) and sample[offset] == 0xFF:
                offset += 1
            if offset >= len(sample):
                return None
            marker = sample[offset]
            offset += 1
            if marker == 0xDA:
                return None
            if marker in {0x01, 0xD8, 0xD9, *range(0xD0, 0xD8)}:
                continue
            if offset + 2 > len(sample):
                return None
            length = int.from_bytes(sample[offset : offset + 2], "big")
            if length < 2 or offset + length > len(sample):
                return None
            if marker in {0xC0, 0xC1, 0xC2}:
                header = sample[offset + 2 : offset + length]
                if len(header) < 5:
                    return None
                height = int.from_bytes(header[1:3], "big")
                width = int.from_bytes(header[3:5], "big")
                if width <= 0 or height <= 0 or width > 16384 or height > 16384:
                    return None
                return width, height
            offset += length
    except (IndexError, ValueError):
        return None
    return None


def _jpeg_probe_sample(sample: bytes | None, width: int, height: int) -> bytes:
    """Check bounded JPEG headers without decoding pixels or trusting camera metadata."""
    if sample is None:
        image = Image.new("RGB", (width, height), (16, 32, 48))
        with io.BytesIO() as stream:
            image.save(stream, format="JPEG", subsampling=2)
            sample = stream.getvalue()
        image.close()
    if not isinstance(sample, bytes) or not 4 <= len(sample) <= PROBE_INPUT_LIMIT:
        raise ValueError("jpeg-input-size-invalid")
    if not sample.startswith(b"\xff\xd8") or not sample.endswith(b"\xff\xd9"):
        raise ValueError("jpeg-input-invalid")
    offset = 2
    while offset + 4 <= len(sample):
        if sample[offset] != 0xFF:
            raise ValueError("jpeg-input-invalid")
        while offset < len(sample) and sample[offset] == 0xFF:
            offset += 1
        if offset >= len(sample):
            break
        marker = sample[offset]
        offset += 1
        if marker == 0xDA:
            break
        if marker in {0x01, 0xD8, 0xD9, *range(0xD0, 0xD8)}:
            continue
        if offset + 2 > len(sample):
            break
        length = int.from_bytes(sample[offset : offset + 2], "big")
        if length < 2 or offset + length > len(sample):
            raise ValueError("jpeg-input-invalid")
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            header = sample[offset + 2 : offset + length]
            if marker != 0xC0 or len(header) != 15 or header[0] != 8 or header[5] != 3:
                raise ValueError("jpeg-format-unsupported")
            actual_height = int.from_bytes(header[1:3], "big")
            actual_width = int.from_bytes(header[3:5], "big")
            if (actual_width, actual_height) != (width, height):
                raise ValueError("jpeg-profile-mismatch")
            # NVDEC can stall on unsupported chroma; never initialize it for those frames.
            if (header[7], header[10], header[13]) != (0x22, 0x11, 0x11):
                raise ValueError("jpeg-chroma-unsupported")
            return sample
        offset += length
    raise ValueError("jpeg-input-invalid")


def _zero_copy_args(
    codec: str, width: int, height: int, fps: int, *, device: str | None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Allow only decoder/encoder pairs sharing hardware frames, with strict pixel formats."""
    inputs, outputs = encoder_args(codec, width, height, fps, device=device)
    if codec == "h264_nvenc":
        inputs += (
            "-hwaccel",
            "cuda",
            "-hwaccel_output_format",
            "cuda",
            "-c:v",
            "mjpeg_cuvid",
            "-flags",
            "+low_delay",
            "-threads",
            "1",
        )
        pixel_format = "cuda"
    elif codec == "h264_qsv":
        inputs += (
            "-hwaccel",
            "qsv",
            "-hwaccel_output_format",
            "qsv",
            "-hwaccel_device",
            "campix",
            "-c:v",
            "mjpeg_qsv",
        )
        pixel_format = "qsv"
    elif codec == "h264_vaapi":
        inputs += ("-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi")
        pixel_format = "vaapi"
    else:
        raise ValueError("hardware-frame-path-unsupported")
    # Upload, software conversion and implicit filters invalidate the zero-copy boundary.
    cleaned: list[str] = []
    for key, value in zip(outputs[::2], outputs[1::2], strict=True):
        if key not in {"-pix_fmt", "-vf"}:
            cleaned.extend((key, value))
    cleaned.extend(("-noautoscale", "-pix_fmt", f"+{pixel_format}"))
    return inputs, tuple(cleaned)


def _smoke_zero_copy(
    ffmpeg: str,
    codec: str,
    width: int,
    height: int,
    fps: int,
    *,
    sample: bytes,
    device: str | None,
    timeout: float,
    cancel: threading.Event | None,
) -> _ProbeResult:
    inputs, outputs = _zero_copy_args(codec, width, height, fps, device=device)
    try:
        with tempfile.TemporaryDirectory(prefix="campix-hardware-frames-") as directory:
            Path(directory).chmod(0o700)
            source = Path(directory) / "input.mjpeg"
            path = Path(directory) / "output.h264"
            # Four bounded compressed packets, not a CPU-decoded raw video input.
            with source.open("wb") as stream:
                for _ in range(4):
                    stream.write(sample)
            command = [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                *inputs,
                "-f",
                "mjpeg",
                "-framerate",
                str(fps),
                "-i",
                str(source),
                "-an",
                "-frames:v",
                "4",
                "-fps_mode",
                "passthrough",
                *outputs,
                "-fs",
                str(PROBE_MEDIA_LIMIT),
                "-f",
                "h264",
                str(path),
            ]
            code, _ = _run_bounded(command, timeout=timeout, media_path=path, cancel=cancel)
            if code != 0:
                return _ProbeResult(codec, False, "hardware-frame-encode-failed", device)
            if not path.exists() or not 0 < path.stat().st_size <= PROBE_MEDIA_LIMIT:
                return _ProbeResult(codec, False, "no-encoded-packets", device)
            with path.open("rb") as stream:
                packets = stream.read(PROBE_MEDIA_LIMIT + 1)
            if not _has_h264_packets(packets):
                return _ProbeResult(codec, False, "invalid-h264-packets", device)
            return _ProbeResult(codec, True, device=device)
    except subprocess.TimeoutExpired:
        return _ProbeResult(codec, False, "probe-timed-out", device)
    except _OutputLimit:
        return _ProbeResult(codec, False, "probe-output-limit", device)
    except OSError:
        return _ProbeResult(codec, False, "probe-process-failed", device)
    except _ProbeCancelled:
        return _ProbeResult(codec, False, "probe-canceled", device)


def _probe_zero_copy(
    ffmpeg: str,
    identity: tuple[Any, ...],
    codec: str,
    width: int,
    height: int,
    fps: int,
    *,
    sample: bytes | None,
    device: str | None,
    platform: str,
    deadline: float,
    cancel: threading.Event | None,
) -> _ProbeResult:
    if cancel is not None and cancel.is_set():
        return _ProbeResult(codec, False, "probe-canceled", device)
    if codec not in {"h264_nvenc", "h264_qsv", "h264_vaapi"}:
        return _ProbeResult(codec, False, "hardware-frame-path-unsupported", device)
    try:
        sample = _jpeg_probe_sample(sample, width, height)
    except ValueError as error:
        return _ProbeResult(codec, False, str(error), device)
    key = (
        *identity,
        platform,
        codec,
        width,
        height,
        fps,
        "mjpeg-hardware-frames",
        device,
        hashlib.sha256(sample).digest(),
    )
    cached = _cached(key)
    if cached is not None:
        return cached
    while True:
        if cancel is not None and cancel.is_set():
            return _ProbeResult(codec, False, "probe-canceled", device)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _ProbeResult(codec, False, "probe-budget-exhausted", device)
        if _probe_slots.acquire(timeout=min(0.02, remaining)):
            break
    try:
        cached = _cached(key)
        if cached is not None:
            return cached
        remaining = min(PROBE_TIMEOUT_SECONDS, deadline - time.monotonic())
        if remaining <= 0:
            return _ProbeResult(codec, False, "probe-budget-exhausted", device)
        result = _smoke_zero_copy(
            ffmpeg,
            codec,
            width,
            height,
            fps,
            sample=sample,
            device=device,
            timeout=remaining,
            cancel=cancel,
        )
        if result.reason == "probe-canceled":
            return result
        return _remember(key, result)
    finally:
        _probe_slots.release()


def _probe_encoder(
    ffmpeg: str,
    identity: tuple[Any, ...],
    codec: str,
    width: int,
    height: int,
    fps: int,
    *,
    platform: str,
    deadline: float,
    cancel: threading.Event | None = None,
) -> _ProbeResult:
    if cancel is not None and cancel.is_set():
        return _ProbeResult(codec, False, "probe-canceled")
    key = (*identity, platform, codec, width, height, fps)
    cached = _cached(key)
    if cached is not None:
        return cached
    if codec not in platform_encoders(platform):
        return _remember(key, _ProbeResult(codec, False, "platform-unsupported"))
    while True:
        if cancel is not None and cancel.is_set():
            return _ProbeResult(codec, False, "probe-canceled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _ProbeResult(codec, False, "probe-budget-exhausted")
        if _probe_slots.acquire(timeout=min(0.02, remaining)):
            break
    try:
        cached = _cached(key)
        if cached is not None:
            return cached
        devices: tuple[str | None, ...] = _render_devices() if codec == "h264_vaapi" else (None,)
        if not devices:
            return _remember(key, _ProbeResult(codec, False, "render-device-unavailable"))
        result = _ProbeResult(codec, False, "probe-budget-exhausted")
        probe_deadline = min(deadline, time.monotonic() + PROBE_TIMEOUT_SECONDS)
        for device in devices:
            remaining = probe_deadline - time.monotonic()
            if remaining <= 0:
                break
            result = _smoke(
                ffmpeg,
                codec,
                width,
                height,
                fps,
                device=device,
                timeout=remaining,
                cancel=cancel,
            )
            if result.usable:
                break
        if result.reason in {"probe-budget-exhausted", "probe-canceled"}:
            return result
        return _remember(key, result)
    finally:
        _probe_slots.release()


def _inventory(
    ffmpeg: str,
    timeout: float,
    cancel: threading.Event | None = None,
) -> tuple[str, ...]:
    code, output = _run_bounded(
        [ffmpeg, "-hide_banner", "-encoders"],
        timeout=timeout,
        cancel=cancel,
    )
    return parse_encoder_list(output) if code == 0 else ()


def probe_encoders(
    ffmpeg: str,
    *,
    platform: str | None = None,
    width: int = 320,
    height: int = 240,
    fps: int = 30,
    budget: float = 20,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Report inventory separately from usability at a synthetic, named profile."""
    _profile(width, height, fps)
    if not 0 < budget <= 60:
        raise ValueError("Encoder probe budget must be between 0 and 60 seconds")
    platform = sys.platform if platform is None else platform
    report: dict[str, Any] = {
        "available": False,
        "encoders": [],
        "usable_encoders": [],
        "details": {},
        "hardware_verified": False,
        "synthetic_probe": True,
        "profile": {"width": width, "height": height, "fps": fps},
    }
    binary = _binary(ffmpeg)
    if binary is None:
        report["reason"] = "ffmpeg-unavailable"
        return report
    resolved, identity = binary
    deadline = time.monotonic() + budget
    try:
        listed = _inventory(resolved, min(PROBE_TIMEOUT_SECONDS, budget), cancel=cancel)
    except _ProbeCancelled:
        report["reason"] = "probe-canceled"
        return report
    except (OSError, subprocess.TimeoutExpired, _OutputLimit):
        report["reason"] = "encoder-inventory-failed"
        return report
    report["encoders"] = list(listed)
    # Reserve a tested software fallback before potentially slow hardware drivers.
    ordered = ("libx264", *(codec for codec in platform_encoders(platform) if codec != "libx264"))
    for codec in ordered:
        if codec not in listed:
            continue
        result = _probe_encoder(
            resolved,
            identity,
            codec,
            width,
            height,
            fps,
            platform=platform,
            deadline=deadline,
            cancel=cancel,
        )
        report["details"][codec] = {
            "usable": result.usable,
            "hardware": codec in ENCODER_HARDWARE_NAMES,
            "reason": result.reason,
            "hardware_verified": False,
        }
        if result.usable:
            report["usable_encoders"].append(codec)
    for codec in listed:
        if codec not in report["details"]:
            report["details"][codec] = {
                "usable": False,
                "hardware": codec in ENCODER_HARDWARE_NAMES,
                "reason": "platform-unsupported",
                "hardware_verified": False,
            }
    report["available"] = bool(report["usable_encoders"])
    return report


def select_encoder(
    ffmpeg: str,
    requested: str,
    *,
    width: int,
    height: int,
    fps: int,
    capabilities: dict[str, Any] | None = None,
    platform: str | None = None,
    excluded: Collection[str] = (),
    budget: float = 12,
    cancel: threading.Event | None = None,
    input_codec: str | None = None,
    input_sample: bytes | None = None,
    zero_copy: bool = True,
) -> EncoderSelection:
    """Choose a tested encoder and, optionally, a tested compressed hardware-frame path."""
    _profile(width, height, fps)
    if requested not in {"auto", *ENCODER_NAMES}:
        raise EncoderUnavailable("Unsupported H.264 encoder")
    if not 0 < budget <= 60:
        raise ValueError("Encoder selection budget must be between 0 and 60 seconds")
    if input_codec not in {None, "mjpeg"}:
        raise ValueError("Unsupported encoder input codec")
    if type(zero_copy) is not bool:
        raise ValueError("Zero-copy preference must be boolean")
    platform = sys.platform if platform is None else platform
    binary = _binary(ffmpeg)
    if binary is None:
        raise EncoderUnavailable("FFmpeg is unavailable")
    resolved, identity = binary
    deadline = time.monotonic() + budget
    try:
        listed = (
            capabilities.get("encoders", ())
            if capabilities is not None
            else _inventory(
                resolved,
                min(PROBE_TIMEOUT_SECONDS, budget),
                cancel=cancel,
            )
        )
    except _ProbeCancelled:
        raise EncoderUnavailable("Encoder selection canceled") from None
    except (OSError, subprocess.TimeoutExpired, _OutputLimit):
        raise EncoderUnavailable("FFmpeg encoder inventory is unavailable") from None
    choices = platform_encoders(platform) if requested == "auto" else (requested,)
    failures = []
    for codec in choices:
        if cancel is not None and cancel.is_set():
            raise EncoderUnavailable("Encoder selection canceled")
        if codec in excluded:
            failures.append(f"{codec}: runtime-encoding-failed")
            continue
        if codec not in listed:
            failures.append(f"{codec}: encoder-not-listed")
            continue
        # Leave part of the total budget for auto's mandatory tested software fallback.
        codec_deadline = deadline
        if requested == "auto" and codec != "libx264" and "libx264" in listed:
            codec_deadline = min(deadline, max(time.monotonic(), deadline - PROBE_TIMEOUT_SECONDS))
        result = _probe_encoder(
            resolved,
            identity,
            codec,
            width,
            height,
            fps,
            platform=platform,
            deadline=codec_deadline,
            cancel=cancel,
        )
        if result.reason == "probe-canceled":
            raise EncoderUnavailable("Encoder selection canceled")
        if not result.usable:
            failures.append(f"{codec}: {result.reason}")
            continue
        inputs, outputs = encoder_args(codec, width, height, fps, device=result.device)
        reason = None
        if requested == "auto" and failures:
            reason = "; ".join(failures) + f"; selected tested {codec}"
        selection = EncoderSelection(
            codec=codec,
            hardware=codec in ENCODER_HARDWARE_NAMES,
            requested=requested,
            input_args=inputs,
            output_args=outputs,
            fallback_reason=reason,
            input_codec=input_codec,
        )
        if input_codec == "mjpeg" and zero_copy:
            frames = _probe_zero_copy(
                resolved,
                identity,
                codec,
                width,
                height,
                fps,
                sample=input_sample,
                device=result.device,
                platform=platform,
                deadline=deadline,
                cancel=cancel,
            )
            if frames.reason == "probe-canceled":
                raise EncoderUnavailable("Encoder selection canceled")
            if frames.usable:
                frame_inputs, frame_outputs = _zero_copy_args(
                    codec,
                    width,
                    height,
                    fps,
                    device=result.device,
                )
                return replace(
                    selection,
                    input_args=frame_inputs,
                    output_args=frame_outputs,
                    zero_copy_raw_frames=True,
                )
            selection = replace(selection, zero_copy_reason=frames.reason)
        elif input_codec == "mjpeg":
            selection = replace(selection, zero_copy_reason="hardware-frame-path-disabled")
        return selection
    profile = f"{width}x{height} at {fps} FPS"
    detail = "; ".join(failures)
    raise EncoderUnavailable(f"No usable H.264 encoder for {profile} ({detail})")
