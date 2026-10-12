from __future__ import annotations

import io
import math
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance

from .models import CameraConfig, ControlPatch
from .processes import NO_WINDOW, bounded_output

DSHOW_MODE = re.compile(
    r"(?:(?P<format_type>pixel_format|vcodec)=(?P<format>\w+)\s+)?"
    r"min s=(?P<low>\d+x\d+) fps=(?P<min_fps>\S+) "
    r"max s=(?P<high>\d+x\d+) fps=(?P<max_fps>\S+)"
)
DSHOW_CLOCK_TOLERANCE_SECONDS = 0.005


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

    def read_jpeg(self) -> bytes | None:
        """Return an already encoded frame when the capture adapter supplies JPEG."""
        return None

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

    def output_arguments(self):
        return []

    def codec_arguments(self):
        # Apply profile quality once in the pipe encoder; the runtime passes those
        # JPEG bytes through. FFmpeg's quantizer scale differs from Pillow quality.
        quantizer = max(2, min(31, round((100 - self.config.quality) / 6)))
        return ["-c:v", "mjpeg", "-q:v", str(quantizer)]

    def open(self):
        video_filter = self.video_filter()
        command = [
            self.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            *self.input_arguments(),
            "-an",
            *(["-vf", video_filter] if video_filter else []),
            "-f",
            "image2pipe",
            *self.codec_arguments(),
            *self.output_arguments(),
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
        self.last_data = time.monotonic()
        self.capabilities.update(model=self.model, controls=[], audio=False)

    def _read_jpeg(self, process=None, buffer=None):
        process = process if process is not None else self.proc
        buffer = buffer if buffer is not None else self.buffer
        start = -1
        scan_from = 0
        while True:
            if start < 0:
                start = buffer.find(b"\xff\xd8", scan_from)
            end = buffer.find(b"\xff\xd9", max(start + 2, scan_from)) if start >= 0 else -1
            if start >= 0 and end > start:
                data = bytes(buffer[start : end + 2])
                del buffer[: end + 2]
                return data
            # Re-scan only the final overlap byte and new input. Searching the entire
            # growing JPEG on every pipe chunk makes large 4K frames quadratic work.
            scan_from = max(0, len(buffer) - 1)
            # read(n) waits to fill n bytes. read1() returns bytes already delivered,
            # including small low-FPS images, without waiting for a later frame.
            read = getattr(process.stdout, "read1", process.stdout.read)
            chunk = read(65536)
            if not chunk:
                raise CaptureError(f"{self.model} is unavailable")
            self.last_data = time.monotonic()
            buffer.extend(chunk)
            if len(buffer) > 16 * 1024 * 1024:
                raise CaptureError(f"{self.model} frame exceeds the supported size")

    def read_jpeg(self):
        jpeg = self._read_jpeg()
        self.frame_received_at = time.monotonic()
        return jpeg

    def read(self):
        with Image.open(io.BytesIO(self.read_jpeg())) as image:
            return image.convert("RGB")

    def close(self):
        if hasattr(self, "proc"):
            terminate_process(self.proc)
            if self.proc.stdout:
                self.proc.stdout.close()


class DirectShowBackend(NetworkBackend):
    """Windows USB or integrated camera captured through FFmpeg's DirectShow input."""

    model = "DirectShow camera"
    stall_seconds = 10
    modes = None
    source_size = None

    def __init__(self, config, ffmpeg="ffmpeg"):
        super().__init__(config, ffmpeg)
        self.frames = threading.Condition()
        self.closed = threading.Event()
        self.reader_stop = threading.Event()
        self.reader_thread = None
        self.watch_thread = None
        self.latest_jpeg = None
        self.reader_error = None
        self.dropped_frames = 0
        self.native_profiles = []
        self.source_fps = None
        self.source_format = None
        self.source_format_type = None
        self.jpeg_copy = False

    def native_modes(self):
        """Return the device's discrete capture sizes; listing them opens no capture stream."""
        self.native_profiles.clear()
        command = [self.ffmpeg, "-hide_banner", "-nostdin", "-list_options", "true", "-f", "dshow"]
        try:
            listing = bounded_output(
                [*command, "-i", "video=" + self.config.source], limit=65536, timeout=10
            ).decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        sizes = set()
        for match in DSHOW_MODE.finditer(listing):
            if match["low"] != match["high"]:
                return None  # A continuous range: any profile size may be native.
            size = tuple(int(part) for part in match["low"].split("x"))
            sizes.add(size)
            try:
                low, high = float(match["min_fps"]), float(match["max_fps"])
            except ValueError:
                continue
            if 0 < low <= high <= 240 and math.isfinite(high):
                self.native_profiles.append({
                    "size": size, "min_fps": low, "max_fps": high,
                    "format_type": match["format_type"], "format": match["format"],
                })
        return sorted(sizes, key=lambda size: (size[0] * size[1], size)) or None

    def _negotiate_source(self):
        profiles = [p for p in self.native_profiles if p["size"] == self.source_size]
        if not profiles:
            return

        def preferred(profile):
            rate = min(max(self.config.fps, profile["min_fps"]), profile["max_fps"])
            pixel_cost = 0 if profile["format"] == "mjpeg" else (
                1 if profile["format"] == "nv12" else 2
            )
            return (max(0, self.config.fps - rate), abs(rate - self.config.fps), pixel_cost)

        selected = min(profiles, key=preferred)
        self.source_fps = min(
            max(self.config.fps, selected["min_fps"]), selected["max_fps"]
        )
        self.source_format_type, self.source_format = selected["format_type"], selected["format"]
        self.jpeg_copy = (
            self.source_format_type == "vcodec" and self.source_format == "mjpeg"
            and self.source_size == (self.config.width, self.config.height)
            and self.source_fps == self.config.fps
        )

    def source_mode(self, width, height):
        """Use the profile size natively, else the smallest mode covering it."""
        covering = [mode for mode in self.modes if mode[0] >= width and mode[1] >= height]
        same_shape = [mode for mode in covering if mode[0] * height == mode[1] * width]
        # A profile beyond every mode is upscaled from the largest one; the API rejects such
        # profiles once modes are known, but older profiles must keep working.
        return (same_shape or covering or [self.modes[-1]])[0]

    def input_arguments(self):
        # Webcams often default to raw full HD at a fixed 30 fps, so request a native mode.
        mode = ["-video_size", "{}x{}".format(*self.source_size)] if self.source_size else []
        # One argument and no shell; validation rejects ":" so no audio device can be added.
        source = ["-i", "video=" + self.config.source]
        rate = ["-framerate", f"{self.source_fps:g}"] if self.source_fps else []
        native_format = (
            ["-" + self.source_format_type, self.source_format]
            if self.source_format_type and self.source_format else []
        )
        return ["-f", "dshow", "-rtbufsize", "32M", *mode, *rate, *native_format, *source]

    def video_filter(self):
        if self.jpeg_copy:
            return None
        # Requesting a frame rate the device does not list would fail, so a filter reduces
        # it before scaling. A source mode can differ in shape: pad rather than distort.
        width, height = self.config.width, self.config.height
        return (
            # The fps filter fills timestamp gaps by duplicating old images. Sample
            # actual arrivals in clock buckets. Comparing with the previous selected
            # timestamp would lose cadence whenever device timestamps jitter slightly.
            "select='isnan(prev_selected_t)+lt(t,prev_selected_t)"
            f"+gt(floor((t+{DSHOW_CLOCK_TOLERANCE_SECONDS})*{self.config.fps}),"
            f"floor((prev_selected_t+{DSHOW_CLOCK_TOLERANCE_SECONDS})*{self.config.fps}))',"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
        )

    def output_arguments(self):
        return ["-fps_mode", "passthrough", "-flush_packets", "1"]

    def codec_arguments(self):
        return ["-c:v", "copy"] if self.jpeg_copy else super().codec_arguments()

    def open(self):
        self.modes = self.native_modes()
        profile = (self.config.width, self.config.height)
        self.source_size = self.source_mode(*profile) if self.modes else profile
        self._negotiate_source()
        self._spawn()
        self.capabilities["modes"] = [f"{width}x{height}" for width, height in self.modes or ()]
        self.capabilities.update(
            source_fps=self.source_fps, source_format=self.source_format,
            jpeg_passthrough=self.jpeg_copy,
            jpeg_quality="source" if self.jpeg_copy else self.config.quality,
        )
        if self.modes:
            return
        try:
            # Keep the first complete JPEG in the same bounded slot used by read().
            with self.frames:
                self.frames.wait_for(
                    lambda: self.closed.is_set() or self.reader_stop.is_set()
                    or self.latest_jpeg is not None or self.reader_error
                )
                if self.closed.is_set() or self.reader_stop.is_set():
                    raise CaptureError("DirectShow camera is closed")
                if self.latest_jpeg is None:
                    raise CaptureError(self.reader_error)
        except CaptureError:
            if self.closed.is_set():
                raise
            # Unknown modes and no frame at the profile size: use the device's default mode.
            self._stop_stream()
            self.source_size = None
            self.source_fps = self.source_format = self.source_format_type = None
            self.jpeg_copy = False
            self.capabilities.update(
                source_fps=None, source_format=None, jpeg_passthrough=False,
                jpeg_quality=self.config.quality,
            )
            self._spawn()

    def _spawn(self):
        with self.lock:
            if self.closed.is_set():
                raise CaptureError("DirectShow camera is closed")
            super().open()
            with self.frames:
                self.latest_jpeg, self.reader_error = None, None
                self.last_frame = time.monotonic()
                self.reader_stop = threading.Event()
            self.capabilities["frame_delivery"] = "latest"
            self.reader_thread = threading.Thread(
                target=self._drain, args=(self.proc, self.buffer, self.reader_stop),
                name=f"dshow-pipe-{self.config.id}", daemon=True,
            )
            self.watch_thread = threading.Thread(
                target=self._watch, args=(self.proc, self.reader_stop),
                name=f"dshow-watch-{self.config.id}", daemon=True,
            )
            self.reader_thread.start()
            self.watch_thread.start()

    def _drain(self, process, buffer, stopped):
        try:
            while not stopped.is_set():
                jpeg = self._read_jpeg(process, buffer)
                with self.frames:
                    if stopped.is_set():
                        break
                    if self.latest_jpeg is not None:
                        self.dropped_frames += 1
                    self.latest_jpeg = jpeg
                    self.last_frame = time.monotonic()
                    self.frames.notify_all()
        except (CaptureError, OSError, ValueError) as exc:
            with self.frames:
                self.reader_error = (
                    str(exc) if isinstance(exc, CaptureError) else "DirectShow camera is unavailable"
                )
                self.frames.notify_all()

    def _watch(self, process, stopped):
        # DirectShow input has no I/O timeout: a stalled device would block read() forever.
        # Stopping FFmpeg turns the stall into a normal reconnect by the capture loop.
        while process.poll() is None:
            if stopped.is_set():
                return
            if time.monotonic() - self.last_frame > self.stall_seconds:
                try:
                    process.kill()
                except OSError:
                    if process.poll() is None:
                        raise
                return
            stopped.wait(0.5)

    def read_jpeg(self):
        stopped = self.reader_stop
        with self.frames:
            self.frames.wait_for(
                lambda: stopped.is_set() or self.latest_jpeg is not None or self.reader_error
            )
            if stopped.is_set() or self.latest_jpeg is None:
                raise CaptureError(self.reader_error or "DirectShow camera is unavailable")
            jpeg, self.latest_jpeg = self.latest_jpeg, None
            self.frame_received_at = self.last_frame
            if time.monotonic() - self.last_frame > 5:
                raise CaptureError("DirectShow camera has no current frame")
        return jpeg

    def close(self):
        # A profile/service cancellation is durable; a mode-negotiation retry only
        # stops its current subprocess and must not undo a concurrent close().
        self.closed.set()
        self._stop_stream()

    def _stop_stream(self):
        with self.lock:
            self.reader_stop.set()
            with self.frames:
                self.latest_jpeg = None
                self.frames.notify_all()
            if hasattr(self, "proc"):
                terminate_process(self.proc)
            for worker in (self.reader_thread, self.watch_thread):
                if worker and worker.is_alive():
                    worker.join(timeout=5)
                    if worker.is_alive():
                        raise CaptureError("DirectShow worker did not stop")
            if hasattr(self, "proc") and self.proc.stdout:
                self.proc.stdout.close()


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
