from __future__ import annotations

import io
import math
import shutil
import subprocess
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance

from .models import CameraConfig, ControlPatch
from .processes import NO_WINDOW


class CaptureError(RuntimeError):
    pass


class Backend:
    def __init__(self, config: CameraConfig, ffmpeg="ffmpeg"):
        self.config = config
        self.ffmpeg = ffmpeg
        self.lock = threading.RLock()
        self.controls = dict(config.controls)
        self.capabilities = {
            "backend": config.backend,
            "model": config.backend,
            "controls": [],
            "raw": False,
            "test_status": "unverified",
        }

    def open(self):
        pass

    def read(self) -> Image.Image:
        raise NotImplementedError

    def apply(self, values):
        ControlPatch(**values)
        if set(values) - set(self.capabilities["controls"]):
            raise CaptureError("Requested controls are unavailable on this camera")
        self.controls.update(values)

    def close(self):
        pass


class SyntheticBackend(Backend):
    def open(self):
        self.started = time.monotonic()
        self.capabilities.update(
            model="Synthetic test camera",
            controls=["brightness", "contrast", "saturation", "exposure"],
            test_status="synthetic",
        )

    def read(self):
        width, height = self.config.width, self.config.height
        image = Image.new("RGB", (width, height), (7, 19, 33))
        draw = ImageDraw.Draw(image)
        for x in range(0, width, max(1, width // 12)):
            draw.line((x, 0, x, height), fill=(24, 49, 74))
        for y in range(0, height, max(1, height // 8)):
            draw.line((0, y, width, y), fill=(24, 49, 74))
        t = time.monotonic() - self.started
        radius = max(12, height // 12)
        x = int(width * (0.5 + 0.3 * math.sin(t * 0.8)))
        y = int(height * (0.5 + 0.3 * math.cos(t * 0.6)))
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=(7, 223, 241),
            outline=(155, 92, 255),
            width=5,
        )
        draw.text((20, 20), f"CamPiX / SYNTHETIC / {self.config.name}", fill=(238, 246, 255))
        draw.text(
            (20, height - 32),
            time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            fill=(142, 164, 193),
        )
        with self.lock:
            controls = dict(self.controls)
        image = ImageEnhance.Brightness(image).enhance(
            max(0.05, 1 + controls.get("brightness", 0)) * 2 ** controls.get("exposure", 0)
        )
        image = ImageEnhance.Contrast(image).enhance(controls.get("contrast", 1))
        return ImageEnhance.Color(image).enhance(controls.get("saturation", 1))


class PicameraBackend(Backend):
    def open(self):
        from picamera2 import Picamera2

        self.cam = Picamera2(int(self.config.source))
        try:
            config = self.cam.create_video_configuration(
                main={"size": (self.config.width, self.config.height), "format": "RGB888"},
                controls={"FrameRate": self.config.fps},
                buffer_count=4,
            )
            self.cam.configure(config)
            available = self.cam.camera_controls
            mapping = {
                "brightness": "Brightness",
                "contrast": "Contrast",
                "saturation": "Saturation",
                "exposure": "ExposureValue",
                "autofocus": "AfMode",
                "lens_position": "LensPosition",
            }
            self.capabilities.update(
                model=str(self.cam.camera_properties.get("Model", "libcamera")),
                controls=[key for key, native in mapping.items() if native in available],
                raw=False,
                native_controls=sorted(available),
                native_h264_candidate=True,
            )
            self.cam.start()
            if self.controls:
                self.apply(self.controls)
        except BaseException:
            self.cam.close()
            raise

    def read(self):
        with self.lock:
            return self.cam.capture_image("main").convert("RGB")

    def apply(self, values):
        with self.lock:
            ControlPatch(**values)
            if set(values) - set(self.capabilities["controls"]):
                raise CaptureError("Requested controls are unavailable on this camera")
            mapping = {
                "brightness": "Brightness",
                "contrast": "Contrast",
                "saturation": "Saturation",
                "exposure": "ExposureValue",
                "autofocus": "AfMode",
                "lens_position": "LensPosition",
            }
            native = {
                mapping[k]: (2 if v else 0) if k == "autofocus" else v for k, v in values.items()
            }
            for key, value in native.items():
                lower, upper, _ = self.cam.camera_controls[key]
                if isinstance(lower, (float, int)) and not lower <= value <= upper:
                    raise CaptureError("Control value exceeds the sensor's detected range")
            self.cam.set_controls(native)
            self.controls.update(values)

    def close(self):
        if hasattr(self, "cam"):
            # Do not wait on the read lock: closing the camera unblocks a stuck capture.
            try:
                self.cam.stop()
            finally:
                self.cam.close()


class V4L2Backend(Backend):
    def open(self):
        import cv2

        self.cv2 = cv2
        self.cam = cv2.VideoCapture(self.config.source, cv2.CAP_V4L2)
        if not self.cam.isOpened():
            self.cam.release()
            raise CaptureError("V4L2 device could not be opened")
        self.cam.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.cam.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cam.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
        self.cam.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
        self.cam.set(cv2.CAP_PROP_FPS, self.config.fps)
        self.native_controls = {}
        if shutil.which("v4l2-ctl"):
            try:
                result = subprocess.run(
                    ["v4l2-ctl", "-d", self.config.source, "--list-ctrls"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=True,
                    creationflags=NO_WINDOW,
                )
                import re

                for line in result.stdout.splitlines():
                    match = re.match(r"\s*(\w+)\s+0x\w+\s+\([^)]+\)\s*:\s*(.*)", line)
                    if match:
                        bounds = dict(re.findall(r"(min|max|default)=(-?\d+)", match[2]))
                        if "min" in bounds and "max" in bounds:
                            self.native_controls[match[1]] = {k: int(v) for k, v in bounds.items()}
            except (subprocess.SubprocessError, OSError):
                pass
        aliases = {
            "brightness": "brightness",
            "contrast": "contrast",
            "saturation": "saturation",
            "autofocus": "focus_automatic_continuous",
        }
        if "focus_auto" in self.native_controls:
            aliases["autofocus"] = "focus_auto"
        self.aliases = aliases
        self.capabilities.update(
            model=Path("/sys/class/video4linux", Path(self.config.source).name, "name")
            .read_text()
            .strip()
            if Path("/sys/class/video4linux", Path(self.config.source).name, "name").is_file()
            else "V4L2 camera",
            controls=[key for key, native in aliases.items() if native in self.native_controls],
            native_controls=self.native_controls,
        )
        if self.controls:
            self.apply(self.controls)

    def read(self):
        with self.lock:
            ok, frame = self.cam.read()
        if not ok:
            raise CaptureError("V4L2 device stopped delivering frames")
        return Image.fromarray(self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2RGB))

    def apply(self, values):
        ControlPatch(**values)
        if set(values) - set(self.capabilities["controls"]):
            raise CaptureError("Requested controls are unavailable on this camera")
        if not values:
            return
        assignments = []
        for name, value in values.items():
            native = self.aliases[name]
            bounds = self.native_controls[native]
            normalized = (value + 1) / 2 if name == "brightness" else value / 4
            actual = (
                int(bool(value))
                if name == "autofocus"
                else round(bounds["min"] + normalized * (bounds["max"] - bounds["min"]))
            )
            assignments.append(f"{native}={actual}")
        try:
            subprocess.run(
                ["v4l2-ctl", "-d", self.config.source, "--set-ctrl", ",".join(assignments)],
                capture_output=True,
                timeout=5,
                check=True,
                creationflags=NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CaptureError("Camera driver rejected these controls") from exc
        self.controls.update(values)

    def close(self):
        if hasattr(self, "cam"):
            self.cam.release()


class NetworkBackend(Backend):
    model = "Network camera"

    def input_arguments(self):
        arguments = [
            "-protocol_whitelist",
            "tcp,udp,rtp,rtsp,http,https,tls,crypto",
            "-rw_timeout",
            "10000000",
        ]
        if self.config.source.startswith(("rtsp://", "rtsps://")):
            arguments += ["-rtsp_transport", "tcp"]
        return arguments + ["-i", self.config.source]

    def video_filter(self):
        return f"scale={self.config.width}:{self.config.height},fps={self.config.fps}"

    def open(self):
        command = [
            self.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            *self.input_arguments(),
            "-an",
            "-vf",
            self.video_filter(),
            "-f",
            "image2pipe",
            "-c:v",
            "mjpeg",
            "pipe:1",
        ]
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
        self.buffer = bytearray()
        self.capabilities.update(model=self.model, controls=[], audio=False)

    def read(self):
        while True:
            start, end = self.buffer.find(b"\xff\xd8"), self.buffer.find(b"\xff\xd9")
            if start >= 0 and end > start:
                data = bytes(self.buffer[start : end + 2])
                del self.buffer[: end + 2]
                with Image.open(io.BytesIO(data)) as image:
                    return image.convert("RGB")
            chunk = self.proc.stdout.read(4096)
            if not chunk:
                raise CaptureError(f"{self.model} is unavailable")
            self.buffer.extend(chunk)
            if len(self.buffer) > 16 * 1024 * 1024:
                raise CaptureError(f"{self.model} frame exceeds the supported size")

    def close(self):
        if hasattr(self, "proc"):
            terminate_process(self.proc)
            if self.proc.stdout:
                self.proc.stdout.close()


class DirectShowBackend(NetworkBackend):
    """Windows USB or integrated camera captured through FFmpeg's DirectShow input."""

    model = "DirectShow camera"
    native_size = True
    pending = None

    def input_arguments(self):
        # Webcams often default to raw full HD at a fixed 30 fps. Request the profile size
        # natively; a requested frame rate outside the device's list would fail to open.
        mode = ["-video_size", f"{self.config.width}x{self.config.height}"]
        # One argument and no shell; validation rejects ":" so no audio device can be added.
        source = ["-i", "video=" + self.config.source]
        return ["-f", "dshow", "-rtbufsize", "32M", *(mode if self.native_size else []), *source]

    def video_filter(self):
        # A fallback mode can differ in aspect ratio: pad rather than distort the image.
        width, height = self.config.width, self.config.height
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={self.config.fps}"
        )

    def open(self):
        self.native_size, self.pending = True, None
        super().open()
        try:
            self.pending = super().read()
        except CaptureError:
            # No native mode of this size (or the device is busy): use its default mode.
            self.close()
            self.native_size = False
            super().open()

    def read(self):
        if self.pending is not None:
            image, self.pending = self.pending, None
            return image
        return super().read()


class LegacyBackend(Backend):
    def open(self):
        self.capabilities.update(
            model="External legacy preview", controls=[], test_status="legacy", preview_only=True
        )

    def read(self):
        path = Path(self.config.source)
        if path.stat().st_size > 16 * 1024 * 1024:
            raise CaptureError("Legacy preview exceeds the supported size")
        with Image.open(path) as image:
            return image.convert("RGB")


def terminate_process(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def make_backend(config, ffmpeg):
    cls = {
        "synthetic": SyntheticBackend,
        "picamera2": PicameraBackend,
        "v4l2": V4L2Backend,
        "dshow": DirectShowBackend,
        "network": NetworkBackend,
        "legacy": LegacyBackend,
    }.get(config.backend)
    if not cls:
        raise CaptureError("Capture adapter is not installed")
    return cls(config, ffmpeg)


def discover(ffmpeg="ffmpeg"):
    from .provision import discover_inventory

    return discover_inventory(ffmpeg=ffmpeg)["devices"]
