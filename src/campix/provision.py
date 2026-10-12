from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from .config import Settings
from .db import Database
from .models import CameraConfig, UserCreate, directshow_source_valid
from .process_lock import ProcessLock
from .processes import NO_WINDOW
from .security import HASHER

# QUERYCAP reads driver metadata; it does not allocate buffers or start capture.
VIDIOC_QUERYCAP = 0x80685600
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_VIDEO_CAPTURE_MPLANE = 0x00001000
V4L2_CAP_VIDEO_OUTPUT = 0x00000002
V4L2_CAP_VIDEO_OUTPUT_MPLANE = 0x00002000
V4L2_CAP_VIDEO_M2M = 0x00008000
V4L2_CAP_VIDEO_M2M_MPLANE = 0x00004000
V4L2_CAP_META_CAPTURE = 0x00800000
V4L2_CAP_READWRITE = 0x01000000
V4L2_CAP_STREAMING = 0x04000000
V4L2_CAP_DEVICE_CAPS = 0x80000000
INVENTORY_LIMIT = 64
INVENTORY_BYTES = 65536
DSHOW_DEVICE = re.compile(r'^\[[^\]]*\]\s+"(?P<name>[^"]+)"(?:\s+\((?P<kinds>[^)]*)\))?\s*$')
DSHOW_ALTERNATIVE = re.compile(r'^\[[^\]]*\]\s+Alternative name "(?P<name>[^"]+)"\s*$')


class ProvisionError(RuntimeError):
    pass


def _diagnostic(code: str, message: str, source: str | None = None) -> dict:
    result = {"code": code, "message": message[:512]}
    if source:
        result["source"] = source[:256]
    return result


def _query_v4l2(source: str) -> dict:
    import fcntl

    payload = bytearray(104)
    descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        fcntl.ioctl(descriptor, VIDIOC_QUERYCAP, payload, True)
    finally:
        os.close(descriptor)
    driver, card, bus, _version, capabilities, device_caps, *_reserved = struct.unpack(
        "16s32s32s6I", payload
    )
    return {
        "driver": driver.split(b"\0", 1)[0].decode("utf-8", "replace"),
        "model": card.split(b"\0", 1)[0].decode("utf-8", "replace"),
        "bus": bus.split(b"\0", 1)[0].decode("utf-8", "replace"),
        "capabilities": device_caps if capabilities & V4L2_CAP_DEVICE_CAPS else capabilities,
    }


def _usb_identity(path: Path, capabilities: dict) -> str | None:
    device = (path / "device").resolve()
    for parent in (device, *device.parents):
        if (parent / "idVendor").is_file() and (parent / "idProduct").is_file():
            return "usb:" + str(parent)
    # UVC bus information identifies the physical device, not its metadata node.
    if capabilities["bus"].lower().startswith("usb-"):
        return "usb:" + capabilities["bus"]
    return None


def _picamera_info() -> list[dict]:
    from picamera2 import Picamera2

    return Picamera2.global_camera_info()


def _is_usb_picamera(item: dict) -> bool:
    identifier = str(item.get("Id", "")).lower()
    pipeline = str(item.get("PipelineHandler", "")).lower()
    return (
        "usb" in identifier
        or "uvc" in pipeline
        or bool(re.search(r"-\d+:\d+\.\d+-[a-f0-9]{4}:[a-f0-9]{4}", identifier))
        or bool(re.search(r"/dev/video\d+", identifier))
    )


def _directshow_listing(ffmpeg: str, timeout: float = 10) -> str:
    """List DirectShow devices with bounded output; listing does not open a capture stream."""
    output = bytearray()
    with subprocess.Popen(
        [ffmpeg, "-hide_banner", "-nostdin", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=NO_WINDOW,
    ) as process:

        def collect():
            while chunk := process.stderr.read(8192):
                output.extend(chunk)
                if len(output) > INVENTORY_BYTES:
                    process.kill()
                    return

        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        try:
            process.wait(timeout=timeout)
        finally:
            if process.poll() is None:
                process.kill()
            reader.join(timeout=5)
    if len(output) > INVENTORY_BYTES:
        raise ValueError("DirectShow device list exceeds its size limit")
    # The list ends with an intentional "dummy" input error, so the exit status is ignored.
    return output.decode("utf-8", "replace")


def _directshow_video_devices(listing: str) -> list[tuple[str, str | None]]:
    """Return (friendly name, alternative name) pairs for listed DirectShow video devices."""
    devices, section, current = [], None, None
    for line in listing.splitlines():
        if "DirectShow video devices" in line or "DirectShow audio devices" in line:
            # FFmpeg before 5.0 grouped devices under headings instead of type suffixes.
            section = "video" if "video devices" in line else "audio"
        elif alternative := DSHOW_ALTERNATIVE.match(line):
            if current is not None:
                current[1] = alternative["name"]
            current = None
        elif device := DSHOW_DEVICE.match(line):
            kinds = device["kinds"]
            types = {kind.strip() for kind in kinds.split(",")} if kinds is not None else {section}
            current = [device["name"], None] if "video" in types else None
            if current is not None:
                devices.append(current)
    return [(name, alternative) for name, alternative in devices]


def _directshow_inventory(ffmpeg: str) -> dict:
    result = {"devices": [], "diagnostics": [], "hardware_qualified": False}
    try:
        listing = _directshow_listing(ffmpeg)
    except (OSError, subprocess.SubprocessError, ValueError):
        result["diagnostics"].append(
            _diagnostic(
                "directshow-listing-failed",
                "FFmpeg could not list DirectShow devices; check the configured FFmpeg executable.",
            )
        )
        return result
    if "Unknown input format" in listing:
        result["diagnostics"].append(
            _diagnostic(
                "directshow-unavailable",
                "The configured FFmpeg build lacks DirectShow input; install a Windows build with dshow.",
            )
        )
        return result
    listed = _directshow_video_devices(listing)
    if len(listed) > INVENTORY_LIMIT:
        result["diagnostics"].append(
            _diagnostic("inventory-limit", "Only the first 64 DirectShow devices were inspected.")
        )
    names = [name for name, _ in listed]
    for name, alternative in listed[:INVENTORY_LIMIT]:
        # Software filters, such as virtual cameras, are not physical capture devices.
        if not alternative or not alternative.lower().startswith("@device_pnp_"):
            result["diagnostics"].append(
                _diagnostic(
                    "directshow-software-source-not-registered",
                    "Software DirectShow sources are not registered automatically; add one manually if intended.",
                    name,
                )
            )
            continue
        forms = [form for form in (name, alternative) if directshow_source_valid(form)]
        # A unique friendly name survives moving a USB camera to another port; identical
        # models need the port-specific alternative name.
        if name in forms and names.count(name) == 1:
            source = name
        elif alternative in forms:
            source = alternative
        else:
            result["diagnostics"].append(
                _diagnostic(
                    "directshow-name-unsupported",
                    "DirectShow device names contain characters that cannot be passed to FFmpeg safely.",
                    name,
                )
            )
            continue
        result["devices"].append(
            {
                "backend": "dshow",
                "source": source,
                "aliases": forms,
                "model": name[:80],
                "identity": "dshow:" + alternative.lower(),
                "status": "detected-not-opened",
                "hardware_qualified": False,
            }
        )
    if not listed:
        result["diagnostics"].append(
            _diagnostic(
                "no-directshow-video-device",
                "FFmpeg listed no DirectShow video device; check that the camera is enabled in Windows.",
            )
        )
    return result


def local_inventory(sysfs: Path = Path("/sys/class/video4linux"), ffmpeg: str = "ffmpeg") -> dict:
    """Inventory physical sources without opening a capture stream or qualifying hardware."""
    if sys.platform == "win32":
        return _directshow_inventory(ffmpeg)
    result = {"devices": [], "diagnostics": [], "hardware_qualified": False}
    if sys.platform != "linux":
        result["diagnostics"].append(
            _diagnostic(
                "local-discovery-unavailable",
                "Automatic local camera discovery requires Linux or Windows; configure network cameras in the UI.",
            )
        )
        return result
    seen = {}
    paths = sorted(
        (path for path in sysfs.glob("video*") if re.fullmatch(r"video\d+", path.name)),
        key=lambda path: int(path.name[5:]),
    )
    if len(paths) > INVENTORY_LIMIT:
        result["diagnostics"].append(
            _diagnostic("inventory-limit", "Only the first 64 video nodes were inspected.")
        )
    for path in paths[:INVENTORY_LIMIT]:
        source = "/dev/" + path.name
        try:
            capabilities = _query_v4l2(source)
            flags = capabilities["capabilities"]
            capture = V4L2_CAP_VIDEO_CAPTURE | V4L2_CAP_VIDEO_CAPTURE_MPLANE
            excluded = (
                V4L2_CAP_VIDEO_OUTPUT
                | V4L2_CAP_VIDEO_OUTPUT_MPLANE
                | V4L2_CAP_VIDEO_M2M
                | V4L2_CAP_VIDEO_M2M_MPLANE
                | V4L2_CAP_META_CAPTURE
            )
            if (
                not flags & capture
                or flags & excluded
                or not flags & (V4L2_CAP_STREAMING | V4L2_CAP_READWRITE)
            ):
                continue
            identity = _usb_identity(path, capabilities)
            if identity is None:
                # Platform CSI/ISP nodes are not independent cameras. Picamera2 owns CSI.
                result["diagnostics"].append(
                    _diagnostic(
                        "platform-node-not-registered",
                        "Platform capture node requires a supported camera adapter; CSI is discovered through Picamera2.",
                        source,
                    )
                )
                continue
            if identity in seen:
                seen[identity]["aliases"].append(source)
                continue
            device = {
                "backend": "v4l2",
                "source": source,
                "aliases": [source],
                "model": capabilities["model"] or "USB camera",
                "driver": capabilities["driver"],
                "identity": identity,
                "status": "detected-not-opened",
                "hardware_qualified": False,
            }
            seen[identity] = device
            result["devices"].append(device)
        except OSError as exc:
            result["diagnostics"].append(
                _diagnostic(
                    "v4l2-query-failed",
                    f"Cannot inspect capture capabilities: {exc.strerror or type(exc).__name__}. Check device permissions and driver availability.",
                    source,
                )
            )
    try:
        cameras = _picamera_info()
        if len(cameras) > INVENTORY_LIMIT:
            result["diagnostics"].append(
                _diagnostic(
                    "inventory-limit", "Only the first 64 libcamera entries were inspected."
                )
            )
        for item in cameras[:INVENTORY_LIMIT]:
            if not isinstance(item, dict) or _is_usb_picamera(item):
                continue
            number = item.get("Num")
            if not isinstance(number, int) or isinstance(number, bool) or number < 0:
                result["diagnostics"].append(
                    _diagnostic(
                        "invalid-libcamera-entry", "Libcamera reported an invalid camera index."
                    )
                )
                continue
            source = str(number)
            identifier = str(item.get("Id") or source)[:256]
            identity = "csi:" + identifier
            if identity in seen:
                seen[identity]["aliases"].append(source)
                continue
            device = {
                "backend": "picamera2",
                "source": source,
                "aliases": [source],
                "model": str(item.get("Model") or "libcamera")[:80],
                "identity": identity,
                "status": "detected-not-opened",
                "hardware_qualified": False,
            }
            seen[identity] = device
            result["devices"].append(device)
    except ImportError:
        result["diagnostics"].append(
            _diagnostic(
                "picamera2-unavailable",
                "Picamera2 is unavailable; CSI discovery requires distribution-matched libcamera/Picamera2 packages.",
            )
        )
    except Exception as exc:
        # Native enumeration can fail independently of USB. Preserve a truthful diagnostic.
        result["diagnostics"].append(
            _diagnostic(
                "picamera2-enumeration-failed",
                f"Libcamera enumeration failed ({type(exc).__name__}); inspect firmware, sensor connections, and distribution camera packages.",
            )
        )
    result["devices"] = result["devices"][:INVENTORY_LIMIT]
    result["diagnostics"] = result["diagnostics"][:INVENTORY_LIMIT]
    return result


def discover_inventory(timeout: float = 15, ffmpeg: str = "ffmpeg") -> dict:
    """Bound native import/ioctl/device-list work in a child, including output and shutdown."""
    failure = {"devices": [], "diagnostics": [], "hardware_qualified": False}
    with tempfile.TemporaryDirectory(prefix="campix-discovery-") as directory:
        output = Path(directory) / "inventory.json"
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import json, runpy, sys; "
                    "sys.path[:] = json.loads(sys.argv.pop(1)); "
                    "runpy.run_module('campix.provision', run_name='__main__')",
                    json.dumps(sys.path),
                    "--inventory-worker",
                    str(output),
                    ffmpeg,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=True,
                creationflags=NO_WINDOW,
            )
            if output.stat().st_size > INVENTORY_BYTES:
                raise ValueError("Inventory exceeds its size limit")
            inventory = json.loads(output.read_text(encoding="utf-8"))
            if not isinstance(inventory, dict) or not isinstance(inventory.get("devices"), list):
                raise ValueError("Invalid inventory")
            return inventory
        except subprocess.TimeoutExpired:
            failure["diagnostics"].append(
                _diagnostic(
                    "discovery-timeout",
                    f"Camera discovery exceeded {timeout:g} seconds and was stopped. Inspect camera drivers before retrying setup.",
                )
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            failure["diagnostics"].append(
                _diagnostic(
                    "discovery-failed",
                    "Camera discovery could not complete. Check the installed Python environment and camera packages.",
                )
            )
    return failure


def _admin_state(path: Path, username: str) -> tuple[bool, bool]:
    if not path.exists():
        return False, False
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] > 2:
                raise ProvisionError(
                    "Database schema is newer than this release; downgrade refused"
                )
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchone():
                return False, False
            ready = bool(
                conn.execute("SELECT 1 FROM users WHERE role='admin' AND disabled=0").fetchone()
            )
            occupied = bool(
                conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
            )
            return ready, occupied
    except sqlite3.Error as exc:
        raise ProvisionError(
            "Cannot inspect the existing database; back it up before repair"
        ) from exc


def setup(
    settings: Settings,
    *,
    non_interactive: bool = False,
    username: str = "admin",
    password_reader: Callable[[], str] | None = None,
) -> dict:
    """Add missing physical camera profiles while preserving all existing application state."""
    path = settings.data_dir / "campix.sqlite3"
    ready, occupied = _admin_state(path, username)
    password_hash = None
    if not ready:
        if non_interactive or password_reader is None:
            raise ProvisionError(
                "No enabled administrator exists. Run campixd setup from an interactive terminal, "
                "or create one securely with campixd init-admin --password-stdin before non-interactive setup."
            )
        if occupied:
            raise ProvisionError(
                "The requested admin username already exists. Use setup --username with a new name "
                "or recover the existing administrator with campixd reset-password."
            )
        try:
            account = UserCreate(username=username, password=password_reader(), role="admin")
        except ValueError as exc:
            raise ProvisionError(
                "Invalid username/password; use safe username characters and a password of 12–128 characters"
            ) from exc
        password_hash = HASHER.hash(account.password)
    inventory = discover_inventory(ffmpeg=settings.ffmpeg)
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = ProcessLock(settings.data_dir)
    try:
        lock.acquire()
    except RuntimeError as exc:
        raise ProvisionError(
            "Stop the CamPiX service before running setup; it owns this data directory"
        ) from exc
    db = None
    try:
        settings.prepare()
        db = Database(path)
        added = []
        skipped = []
        admin_created = False
        with db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM users WHERE role='admin' AND disabled=0").fetchone():
                if password_hash is None:
                    raise ProvisionError("Administrator state changed; rerun interactive setup")
                if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                    raise ProvisionError("Administrator username became unavailable; rerun setup")
                conn.execute(
                    "INSERT INTO users(username,password,role) VALUES (?,?,'admin')",
                    (username, password_hash),
                )
                admin_created = True
            existing = [
                CameraConfig(**json.loads(row[0]))
                for row in conn.execute("SELECT config FROM cameras")
            ]
            sources = {(camera.backend, camera.source) for camera in existing}
            identifiers = {camera.id for camera in existing}
            for device in inventory["devices"]:
                source_key = (device["backend"], device["source"])
                aliases = device.get("aliases", [device["source"]])
                if source_key in sources or any(
                    (device["backend"], alias) in sources for alias in aliases
                ):
                    skipped.append({"source": device["source"], "reason": "already-configured"})
                    continue
                digest = hashlib.sha256(device["identity"].encode("utf-8")).hexdigest()[:12]
                camera_id = f"local-{device['backend']}-{digest}"
                if camera_id in identifiers:
                    skipped.append({"source": device["source"], "reason": "existing-id-preserved"})
                    continue
                if len(identifiers) >= settings.max_cameras:
                    skipped.append({"source": device["source"], "reason": "camera-limit"})
                    continue
                camera = CameraConfig(
                    id=camera_id,
                    name=(device["model"] or "Local camera")[:80],
                    backend=device["backend"],
                    source=device["source"],
                    width=640,
                    height=480,
                    fps=10,
                    encoder="auto",
                )
                if camera.backend not in {"picamera2", "v4l2", "dshow"}:
                    raise ProvisionError("Discovery reported an unsupported local camera backend")
                conn.execute(
                    "INSERT INTO cameras(id,config) VALUES (?,?)",
                    (camera.id, camera.model_dump_json()),
                )
                sources.add(source_key)
                identifiers.add(camera.id)
                added.append(camera.model_dump())
        return {
            **inventory,
            "admin_created": admin_created,
            "cameras_added": added,
            "cameras_skipped": skipped,
            "hardware_qualified": False,
        }
    finally:
        if db:
            db.close()
        lock.close()


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--inventory-worker":
        raise SystemExit("This module is an internal discovery worker; use campixd setup")
    encoded = json.dumps(local_inventory(ffmpeg=sys.argv[3])).encode("utf-8")
    if len(encoded) > INVENTORY_BYTES:
        raise SystemExit("Inventory exceeds its size limit")
    Path(sys.argv[2]).write_bytes(encoded)
