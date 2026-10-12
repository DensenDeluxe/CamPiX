import io
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from campix import capture, provision, runtime
from campix.config import Settings
from campix.db import Database
from campix.models import CameraConfig
from campix.processes import NO_WINDOW
from campix.security import Auth

from .conftest import signin

INTEGRATED = (
    r"@device_pnp_\\?\usb#vid_04f2&pid_b831&mi_00#6&1a2b3c4d&0&0000"
    r"#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
)
CAPTURE_CARD = (
    r"@device_pnp_\\?\usb#vid_534d&pid_2109&mi_00#7&2b3c4d5e&0&0000"
    r"#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
)
VIRTUAL = (
    r"@device_sw_{860BB310-5D01-11D0-BD3B-00A0C911CE86}\{A3FCE0F5-3493-419F-958A-ABA1250EC20B}"
)
MICROPHONE = r"@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{8EB4AE13-3A48-4F50}"


def modern_listing(*devices):
    """FFmpeg 5+ device list format, as printed by FFmpeg 8.1.1 on Windows 11."""
    lines = []
    for name, kinds, alternative in devices:
        lines.append(f'[in#0 @ 000001e86a0b74c0] "{name}" ({kinds})')
        lines.append(f'[in#0 @ 000001e86a0b74c0]   Alternative name "{alternative}"')
    return "\n".join([*lines, "Error opening input file dummy."])


def windows_inventory(monkeypatch, listing):
    monkeypatch.setattr(provision.sys, "platform", "win32")
    requested = []

    def list_devices(ffmpeg):
        requested.append(ffmpeg)
        if isinstance(listing, Exception):
            raise listing
        return listing

    monkeypatch.setattr(provision, "_directshow_listing", list_devices)
    result = provision.local_inventory(ffmpeg=r"C:\tools\ffmpeg.exe")
    assert requested == [r"C:\tools\ffmpeg.exe"]
    return result


def camera(source="Integrated Camera", width=640, height=480):
    return CameraConfig(
        id="webcam", name="Webcam", backend="dshow", source=source, width=width, height=height
    )


@pytest.mark.parametrize("source", ["Integrated Camera", INTEGRATED])
def test_directshow_source_accepts_friendly_and_alternative_names(source):
    assert camera(source).source == source


@pytest.mark.parametrize(
    "source",
    ["Camera:audio=Microphone", 'Camera"', "Camera\n", " Camera", "Camera ", "", "x" * 257],
)
def test_directshow_source_rejects_device_list_injection_and_control_characters(source):
    with pytest.raises(ValueError, match="DirectShow source"):
        camera(source)


def test_device_list_parser_reads_modern_and_legacy_ffmpeg_formats():
    modern = modern_listing(
        ("Integrated Camera", "video", INTEGRATED),
        ("Microphone Array (Intel® Smart Sound)", "audio", MICROPHONE),
        ("USB Capture HDMI", "video, audio", CAPTURE_CARD),
    )
    assert provision._directshow_video_devices(modern) == [
        ("Integrated Camera", INTEGRATED),
        ("USB Capture HDMI", CAPTURE_CARD),
    ]
    legacy = "\n".join(
        [
            "[dshow @ 0000020f] DirectShow video devices (some may be both video and audio devices)",
            '[dshow @ 0000020f]  "Integrated Camera"',
            f'[dshow @ 0000020f]     Alternative name "{INTEGRATED}"',
            "[dshow @ 0000020f] DirectShow audio devices",
            '[dshow @ 0000020f]  "Microphone Array"',
            f'[dshow @ 0000020f]     Alternative name "{MICROPHONE}"',
            "dummy: Immediate exit requested",
        ]
    )
    assert provision._directshow_video_devices(legacy) == [("Integrated Camera", INTEGRATED)]


def test_windows_inventory_registers_only_physical_video_devices(monkeypatch):
    result = windows_inventory(
        monkeypatch,
        modern_listing(
            ("Integrated Camera", "video", INTEGRATED),
            ("OBS Virtual Camera", "video", VIRTUAL),
            ("Microphone Array", "audio", MICROPHONE),
        ),
    )
    assert result["devices"] == [
        {
            "backend": "dshow",
            "source": "Integrated Camera",
            "aliases": ["Integrated Camera", INTEGRATED],
            "model": "Integrated Camera",
            "identity": "dshow:" + INTEGRATED.lower(),
            "status": "detected-not-opened",
            "hardware_qualified": False,
        }
    ]
    assert result["hardware_qualified"] is False
    assert result["diagnostics"] == [
        {
            "code": "directshow-software-source-not-registered",
            "message": "Software DirectShow sources are not registered automatically; "
            "add one manually if intended.",
            "source": "OBS Virtual Camera",
        }
    ]


def test_identical_camera_models_use_distinct_port_specific_sources(monkeypatch):
    result = windows_inventory(
        monkeypatch,
        modern_listing(("USB Camera", "video", INTEGRATED), ("USB Camera", "video", CAPTURE_CARD)),
    )
    assert [item["source"] for item in result["devices"]] == [INTEGRATED, CAPTURE_CARD]
    # A manual profile using the shared name must block both, never add a second owner.
    assert all("USB Camera" in item["aliases"] for item in result["devices"])


def test_unsafe_device_names_fall_back_or_are_reported(monkeypatch):
    result = windows_inventory(
        monkeypatch,
        modern_listing(
            ("Front: Camera", "video", INTEGRATED),
            ("Rear: Camera", "video", r"@device_pnp_\\?\usb#rear:camera"),
        ),
    )
    assert [item["source"] for item in result["devices"]] == [INTEGRATED]
    assert result["devices"][0]["aliases"] == [INTEGRATED]
    assert [item["code"] for item in result["diagnostics"]] == ["directshow-name-unsupported"]


@pytest.mark.parametrize(
    ("listing", "code"),
    [
        (OSError("FFmpeg is missing"), "directshow-listing-failed"),
        (subprocess.TimeoutExpired("ffmpeg", 10), "directshow-listing-failed"),
        (ValueError("too much output"), "directshow-listing-failed"),
        ("Unknown input format: 'dshow'", "directshow-unavailable"),
        ("Error opening input file dummy.", "no-directshow-video-device"),
    ],
)
def test_windows_inventory_reports_truthful_empty_results(monkeypatch, listing, code):
    result = windows_inventory(monkeypatch, listing)
    assert result["devices"] == []
    assert [item["code"] for item in result["diagnostics"]] == [code]
    assert "FFmpeg is missing" not in json.dumps(result)


def test_device_list_output_is_bounded_and_windowless(monkeypatch):
    spawn = subprocess.Popen
    launched = []

    def flood(command, **kwargs):
        launched.append((command, kwargs))
        writer = "import sys; sys.stderr.write('x' * 70000)"
        return spawn([sys.executable, "-c", writer], **kwargs)

    monkeypatch.setattr(provision.subprocess, "Popen", flood)
    with pytest.raises(ValueError, match="size limit"):
        provision._directshow_listing("ffmpeg")
    command, kwargs = launched[0]
    assert command == [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-list_devices",
        "true",
        "-f",
        "dshow",
        "-i",
        "dummy",
    ]
    assert kwargs["creationflags"] == NO_WINDOW and "shell" not in kwargs


def test_hung_device_list_is_stopped(monkeypatch):
    spawn = subprocess.Popen
    children = []

    def hang(command, **kwargs):
        children.append(spawn([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs))
        return children[-1]

    monkeypatch.setattr(provision.subprocess, "Popen", hang)
    with pytest.raises(subprocess.TimeoutExpired):
        provision._directshow_listing("ffmpeg", timeout=0.5)
    assert children[0].poll() is not None


def test_discovery_worker_receives_the_configured_ffmpeg(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        output = Path(command[command.index("--inventory-worker") + 1])
        output.write_text(
            json.dumps({"devices": [], "diagnostics": [], "hardware_qualified": False})
        )

    monkeypatch.setattr(provision.subprocess, "run", run)
    provision.discover_inventory(ffmpeg=r"C:\tools\ffmpeg.exe")
    assert commands[0][-1] == r"C:\tools\ffmpeg.exe"


@pytest.mark.skipif(sys.platform != "win32", reason="DirectShow discovery runs on Windows")
def test_windows_discovery_worker_lists_with_the_configured_ffmpeg(tmp_path):
    result = provision.discover_inventory(ffmpeg=str(tmp_path / "missing-ffmpeg.exe"))
    assert result["devices"] == []
    assert [item["code"] for item in result["diagnostics"]] == ["directshow-listing-failed"]


def test_setup_registers_directshow_camera_once(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", ffmpeg=r"C:\tools\ffmpeg.exe")
    settings.prepare()
    db = Database(settings.data_dir / "campix.sqlite3")
    Auth(db, settings).create_user("maintainer", "test-only-admin-password")
    db.close()
    requested = []
    detected = {
        "backend": "dshow",
        "source": "Integrated Camera",
        "aliases": ["Integrated Camera", INTEGRATED],
        "model": "Integrated Camera",
        "identity": "dshow:" + INTEGRATED.lower(),
        "status": "detected-not-opened",
        "hardware_qualified": False,
    }

    def discover(**kwargs):
        requested.append(kwargs)
        return {"devices": [detected], "diagnostics": [], "hardware_qualified": False}

    monkeypatch.setattr(provision, "discover_inventory", discover)
    result = provision.setup(settings, non_interactive=True)
    assert requested == [{"ffmpeg": r"C:\tools\ffmpeg.exe"}]
    added = result["cameras_added"]
    assert [(item["backend"], item["source"], item["name"]) for item in added] == [
        ("dshow", "Integrated Camera", "Integrated Camera")
    ]
    # PC webcams start at the common native 720p mode instead of the board profile.
    assert (added[0]["width"], added[0]["height"], added[0]["fps"]) == (1280, 720, 10)
    assert result["hardware_qualified"] is False
    # A profile that uses the alternative name is the same physical camera.
    db = Database(settings.data_dir / "campix.sqlite3")
    db.execute("DELETE FROM cameras")
    manual = camera(INTEGRATED)
    db.execute("INSERT INTO cameras VALUES (?,?)", (manual.id, manual.model_dump_json()))
    db.close()
    rerun = provision.setup(settings, non_interactive=True)
    assert rerun["cameras_added"] == []
    assert rerun["cameras_skipped"] == [
        {"source": "Integrated Camera", "reason": "already-configured"}
    ]


def test_discovery_api_lists_with_the_configured_ffmpeg(client, application, monkeypatch):
    requested = []

    def discover(**kwargs):
        requested.append(kwargs)
        return {"devices": [], "diagnostics": [], "hardware_qualified": False}

    monkeypatch.setattr(provision, "discover_inventory", discover)
    signin(client)
    assert client.get("/api/v1/discovery").status_code == 200
    assert requested == [{"ffmpeg": application.state.settings.ffmpeg}]


MODES = [(640, 360), (640, 480), (1280, 720), (1920, 1080)]
# FFmpeg 8.1.1 -list_options output of the measured integrated webcam, in device order.
OPTIONS = "\n".join(
    [
        "[in#0 @ 000001e8] DirectShow video device options (from video devices)",
        '[in#0 @ 000001e8]  Pin "Capture" (alternative pin name "Capture")',
        *(
            f"[in#0 @ 000001e8]   pixel_format={pixel}  min s={size} fps=30 max s={size} fps=30"
            for pixel in ("nv12", "yuyv422")
            for size in ("1280x720", "1920x1080", "640x360", "640x480")
        ),
        "Error opening input file video=Integrated Camera.",
    ]
)


class FakeFFmpeg:
    def __init__(self, output):
        self.stdout = io.BytesIO(output)
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 1

    kill = terminate

    def wait(self, timeout=None):
        return self.returncode


class StalledFFmpeg(FakeFFmpeg):
    """A device that opens but delivers nothing until FFmpeg is stopped."""

    def __init__(self):
        super().__init__(b"")
        self.stopped = threading.Event()
        self.stdout = SimpleNamespace(read=self.stalled_read, close=lambda: None)

    def stalled_read(self, size):
        self.stopped.wait(10)
        return b""

    def terminate(self):
        self.returncode = 1
        self.stopped.set()

    kill = terminate


def jpeg(size=(640, 480)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (7, 19, 33)).save(buffer, "JPEG")
    return buffer.getvalue()


def launch(monkeypatch, *outputs, modes=MODES):
    launched = []

    def popen(command, **kwargs):
        launched.append((command, kwargs))
        output = outputs[len(launched) - 1]
        return output if isinstance(output, FakeFFmpeg) else FakeFFmpeg(output)

    monkeypatch.setattr(capture.subprocess, "Popen", popen)
    monkeypatch.setattr(capture.DirectShowBackend, "native_modes", lambda self: modes)
    return launched


def test_mode_list_reports_discrete_native_sizes_without_capture(monkeypatch):
    requested = []

    def listing(command, **kwargs):
        requested.append((command, kwargs))
        return OPTIONS.encode()

    monkeypatch.setattr(capture, "bounded_output", listing)
    assert capture.make_backend(camera(), r"C:\tools\ffmpeg.exe").native_modes() == MODES
    assert requested == [
        (
            [
                r"C:\tools\ffmpeg.exe",
                "-hide_banner",
                "-nostdin",
                "-list_options",
                "true",
                "-f",
                "dshow",
                "-i",
                "video=Integrated Camera",
            ],
            {"limit": 65536, "timeout": 10},
        )
    ]


@pytest.mark.parametrize(
    "listing",
    [
        b"[in#0 @ 0]   vcodec=mjpeg  min s=160x120 fps=5 max s=1920x1080 fps=30",
        b"Error opening input file video=Integrated Camera.",
        OSError("FFmpeg is missing"),
        subprocess.TimeoutExpired("ffmpeg", 10),
        ValueError("too much output"),
    ],
)
def test_unknown_or_continuous_mode_lists_leave_sizes_unrestricted(monkeypatch, listing):
    def list_options(command, **kwargs):
        if isinstance(listing, Exception):
            raise listing
        return listing

    monkeypatch.setattr(capture, "bounded_output", list_options)
    assert capture.make_backend(camera(), "ffmpeg").native_modes() is None


def test_source_mode_prefers_native_then_the_smallest_covering_mode():
    backend = capture.make_backend(camera(), "ffmpeg")
    backend.modes = [(640, 480), (1280, 720), (1280, 960), (1920, 1080)]
    assert backend.source_mode(1280, 720) == (1280, 720)
    assert backend.source_mode(800, 600) == (1280, 960)
    assert backend.source_mode(1024, 576) == (1280, 720)
    # Only older, unvalidated profiles exceed every mode; they scale from the largest.
    assert backend.source_mode(3840, 2160) == (1920, 1080)


def test_directshow_backend_requests_a_native_mode_and_reduces_the_rate_first(monkeypatch):
    launched = launch(monkeypatch, jpeg() * 2)
    backend = capture.make_backend(camera(), "ffmpeg")
    backend.open()
    try:
        assert [backend.read().size, backend.read().size] == [(640, 480), (640, 480)]
    finally:
        backend.close()
    assert len(launched) == 1
    command, kwargs = launched[0]
    assert command[command.index("-f") + 1] == "dshow"
    assert command[command.index("-video_size") + 1] == "640x480"
    assert command[command.index("-i") + 1] == "video=Integrated Camera"
    # Webcams with fixed frame-rate lists reject other rates; the filter reduces it
    # before any scaling, so FFmpeg scales 10 rather than 30 frames per second.
    assert "-framerate" not in command
    assert command[command.index("-vf") + 1].startswith("fps=10,scale=640:480")
    assert command[command.index("-q:v") + 1] == "3"
    assert kwargs["creationflags"] == NO_WINDOW and "shell" not in kwargs
    assert backend.capabilities["model"] == "DirectShow camera"
    assert backend.capabilities["modes"] == ["640x360", "640x480", "1280x720", "1920x1080"]


def test_profile_beyond_every_mode_uses_the_largest_native_mode(monkeypatch):
    launched = launch(monkeypatch, b"")
    backend = capture.make_backend(camera(width=3840, height=2160), "ffmpeg")
    backend.open()
    backend.close()
    command = launched[0][0]
    assert len(launched) == 1
    assert command[command.index("-video_size") + 1] == "1920x1080"


def test_unknown_modes_try_the_profile_size_then_the_default_mode(monkeypatch):
    launched = launch(monkeypatch, b"", jpeg(), modes=None)
    backend = capture.make_backend(camera(), "ffmpeg")
    backend.open()
    try:
        assert backend.read().size == (640, 480)
    finally:
        backend.close()
    native, fallback = (command for command, _ in launched)
    assert "-video_size" in native and "-video_size" not in fallback
    video_filter = fallback[fallback.index("-vf") + 1]
    assert video_filter.startswith("fps=10,")
    assert "force_original_aspect_ratio=decrease" in video_filter
    assert "pad=640:480" in video_filter
    assert backend.capabilities["modes"] == []


def test_unavailable_directshow_camera_reports_capture_error(monkeypatch):
    launch(monkeypatch, b"", b"", modes=None)
    backend = capture.make_backend(camera(), "ffmpeg")
    backend.open()
    try:
        with pytest.raises(capture.CaptureError, match="DirectShow camera is unavailable"):
            backend.read()
    finally:
        backend.close()


def test_stalled_directshow_stream_is_stopped_for_a_reconnect(monkeypatch):
    monkeypatch.setattr(capture.DirectShowBackend, "stall_seconds", 0.3)
    stalled = StalledFFmpeg()
    launch(monkeypatch, stalled)
    backend = capture.make_backend(camera(), "ffmpeg")
    backend.open()
    started = time.monotonic()
    try:
        # Without the watchdog this read would block for as long as the driver stalls.
        with pytest.raises(capture.CaptureError, match="DirectShow camera is unavailable"):
            backend.read()
    finally:
        backend.close()
    assert stalled.stopped.is_set()
    assert time.monotonic() - started < 5


def test_network_frames_cross_the_pipe_nearly_lossless(monkeypatch):
    launched = launch(monkeypatch, b"")
    source = CameraConfig(id="ip", name="IP", backend="network", source="rtsp://192.0.2.10/live")
    backend = capture.make_backend(source, "ffmpeg")
    backend.open()
    backend.close()
    command = launched[0][0]
    assert command[command.index("-q:v") + 1] == "3"


class ModeReportingBackend(capture.Backend):
    def open(self):
        self.capabilities.update(model="DirectShow camera", modes=["640x480", "1280x720"])

    def read(self):
        time.sleep(0.05)
        return Image.new("RGB", (self.config.width, self.config.height))


def test_directshow_profiles_accept_only_native_modes(client, application, monkeypatch):
    real = runtime.make_backend

    def make_backend(config, ffmpeg):
        if config.backend == "dshow":
            return ModeReportingBackend(config, ffmpeg)
        return real(config, ffmpeg)

    monkeypatch.setattr(runtime, "make_backend", make_backend)
    signin(client)
    profile = {"id": "webcam", "name": "Webcam", "backend": "dshow", "source": "Webcam"}
    created = client.post("/api/v1/cameras", json={**profile, "width": 640, "height": 480})
    assert created.status_code == 201, created.text
    camera = application.state.cameras.get("webcam")
    deadline = time.monotonic() + 5
    while camera.status != "online" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert camera.status == "online"
    rejected = client.patch("/api/v1/cameras/webcam", json={"width": 3840, "height": 2160})
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "This camera captures only 640x480, 1280x720"
    # A refused size must not interrupt the running capture or change the profile.
    assert application.state.cameras.get("webcam") is camera
    assert camera.status == "online"
    assert client.get("/api/v1/cameras/webcam/config").json()["width"] == 640
    accepted = client.patch("/api/v1/cameras/webcam", json={"width": 1280, "height": 720})
    assert accepted.status_code == 200, accepted.text
