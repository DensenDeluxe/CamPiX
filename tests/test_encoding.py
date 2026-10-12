import shutil
import subprocess
import sys
import threading
import time

import psutil
import pytest

from campix import encoding

PACKETS = b"\x00\x00\x00\x01\x67abc\x00\x00\x01\x68def\x00\x00\x01\x65ghi"


@pytest.fixture(autouse=True)
def clean_probe_cache():
    encoding.invalidate_encoder_cache()
    yield
    encoding.invalidate_encoder_cache()


def fake_inventory(monkeypatch, listed=("libx264", "h264_nvenc")):
    monkeypatch.setattr(encoding, "_binary", lambda _: ("ffmpeg", ("ffmpeg", 1, 2)))
    monkeypatch.setattr(encoding, "_inventory", lambda *args, **kwargs: listed)


def test_encoder_inventory_matches_rows_not_descriptions_or_substrings():
    inventory = """Encoders:
 V..... = Video
 V....D libx264rgb software mentions h264_nvenc
 V....D h264_nvenc_extra unrelated encoder
 A..... libx264 wrong media type
 V....D libx264 H.264 / AVC
 V....D h264_qsv Quick Sync
 V....D h264_nvenc NVIDIA NVENC
 Some prose: h264_amf
"""
    assert encoding.parse_encoder_list(inventory) == ("libx264", "h264_nvenc", "h264_qsv")


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("win32", ("h264_nvenc", "h264_qsv", "h264_amf", "libx264")),
        ("darwin", ("h264_videotoolbox", "libx264")),
        ("linux", ("h264_nvenc", "h264_qsv", "h264_vaapi", "h264_v4l2m2m", "libx264")),
        ("unsupported", ("libx264",)),
    ],
)
def test_platform_candidates_use_native_backends(platform, expected):
    assert encoding.platform_encoders(platform) == expected


def test_nvenc_4k60_uses_low_latency_rate_control_without_a_low_level_cap():
    inputs, outputs = encoding.encoder_args("h264_nvenc", 3840, 2160, 60)
    options = dict(zip(outputs[::2], outputs[1::2], strict=True))
    assert inputs == ()
    assert options["-pix_fmt"] == "nv12"
    assert options["-preset"] == "p1"
    assert options["-tune"] == "ull"
    assert options["-bf"] == "0"
    assert options["-rc-lookahead"] == "0"
    assert options["-g"] == "60"
    assert options["-b:v"] == "49766400"
    assert "-level" not in options


def test_qsv_and_videotoolbox_do_not_reuse_software_pixel_format_or_fallback():
    qsv_inputs, qsv = encoding.encoder_args("h264_qsv", 1920, 1080, 60)
    _, videotoolbox = encoding.encoder_args("h264_videotoolbox", 1920, 1080, 60)
    assert "nv12" in qsv and "yuv420p" not in qsv
    assert qsv_inputs == ("-init_hw_device", "qsv=campix:hw_any")
    assert qsv[qsv.index("-async_depth") + 1] == "1"
    assert videotoolbox[videotoolbox.index("-allow_sw") + 1] == "0"


def test_vaapi_uploads_software_frames_to_a_discovered_device():
    inputs, outputs = encoding.encoder_args(
        "h264_vaapi",
        1920,
        1080,
        60,
        device="/dev/dri/renderD128",
    )
    assert inputs == ("-vaapi_device", "/dev/dri/renderD128")
    assert outputs[outputs.index("-vf") + 1] == "format=nv12,hwupload"
    assert "-pix_fmt" not in outputs


@pytest.mark.parametrize(
    "device", ["/tmp/renderD128", "/dev/dri/renderD128;whoami", "../renderD128"]
)
def test_device_arguments_reject_arbitrary_paths(device):
    with pytest.raises(ValueError, match="Unsupported encoder device"):
        encoding.encoder_args("h264_vaapi", 640, 480, 30, device=device)


@pytest.mark.parametrize(
    ("width", "height", "fps"),
    [(0, 480, 30), (641, 480, 30), (640, 481, 30), (8192, 2160, 60), (640, 480, 120)],
)
def test_profile_bounds_precede_subprocess_arguments(width, height, fps):
    with pytest.raises(ValueError, match="Encoder profile"):
        encoding.encoder_args("h264_nvenc", width, height, fps)


def test_untrusted_codec_name_never_reaches_a_process():
    with pytest.raises(encoding.EncoderUnavailable, match="Unsupported H.264 encoder"):
        encoding.select_encoder("ffmpeg", "h264_nvenc;whoami", width=640, height=480, fps=30)


def test_listed_hardware_is_not_usable_until_real_encoding_succeeds(monkeypatch):
    fake_inventory(monkeypatch)

    def smoke(ffmpeg, codec, *args, **kwargs):
        return encoding._ProbeResult(
            codec, codec == "libx264", None if codec == "libx264" else "encode-failed"
        )

    monkeypatch.setattr(encoding, "_smoke", smoke)
    report = encoding.probe_encoders("ffmpeg", platform="win32")
    assert report["encoders"] == ["libx264", "h264_nvenc"]
    assert report["usable_encoders"] == ["libx264"]
    assert report["details"]["h264_nvenc"]["reason"] == "encode-failed"
    assert report["hardware_verified"] is False
    assert report["profile"] == {"width": 320, "height": 240, "fps": 30}


def test_auto_verifies_exact_profile_and_reports_software_fallback(monkeypatch):
    fake_inventory(monkeypatch)
    profiles = []

    def smoke(ffmpeg, codec, width, height, fps, **kwargs):
        profiles.append((codec, width, height, fps))
        usable = codec == "libx264" or width <= 1920
        return encoding._ProbeResult(codec, usable, None if usable else "encode-failed")

    monkeypatch.setattr(encoding, "_smoke", smoke)
    report = encoding.probe_encoders("ffmpeg", platform="win32")
    assert "h264_nvenc" in report["usable_encoders"]
    selection = encoding.select_encoder(
        "ffmpeg",
        "auto",
        width=3840,
        height=2160,
        fps=60,
        capabilities=report,
        platform="win32",
    )
    assert selection.codec == "libx264"
    assert not selection.hardware
    assert selection.probe_verified and not selection.hardware_verified
    assert "h264_nvenc: encode-failed" in selection.fallback_reason
    assert ("h264_nvenc", 3840, 2160, 60) in profiles
    assert ("libx264", 3840, 2160, 60) in profiles


def test_auto_selects_usable_native_hardware_without_qualification_claim(monkeypatch):
    fake_inventory(monkeypatch, ("libx264", "h264_nvenc", "h264_qsv"))
    called = []

    def smoke(ffmpeg, codec, *args, **kwargs):
        called.append(codec)
        return encoding._ProbeResult(codec, True)

    monkeypatch.setattr(encoding, "_smoke", smoke)
    selection = encoding.select_encoder(
        "ffmpeg",
        "auto",
        width=3840,
        height=2160,
        fps=60,
        platform="win32",
    )
    assert selection.codec == "h264_nvenc" and selection.hardware
    assert selection.fallback_reason is None
    assert selection.as_dict()["hardware_verified"] is False
    assert called == ["h264_nvenc"]


def test_explicit_hardware_failure_does_not_silently_select_software(monkeypatch):
    fake_inventory(monkeypatch)
    monkeypatch.setattr(
        encoding,
        "_smoke",
        lambda ffmpeg, codec, *args, **kwargs: encoding._ProbeResult(codec, False, "encode-failed"),
    )
    with pytest.raises(encoding.EncoderUnavailable, match="h264_nvenc: encode-failed"):
        encoding.select_encoder(
            "ffmpeg",
            "h264_nvenc",
            width=3840,
            height=2160,
            fps=60,
            platform="win32",
        )


def test_auto_excludes_runtime_failed_hardware_and_observes_fallback(monkeypatch):
    fake_inventory(monkeypatch)
    called = []

    def smoke(ffmpeg, codec, *args, **kwargs):
        called.append(codec)
        return encoding._ProbeResult(codec, True)

    monkeypatch.setattr(encoding, "_smoke", smoke)
    selection = encoding.select_encoder(
        "ffmpeg",
        "auto",
        width=1920,
        height=1080,
        fps=60,
        platform="win32",
        excluded={"h264_nvenc"},
    )
    assert selection.codec == "libx264"
    assert called == ["libx264"]
    assert "runtime-encoding-failed" in selection.fallback_reason


def test_cache_keys_include_profile_and_are_bounded(monkeypatch):
    fake_inventory(monkeypatch, ("libx264",))
    called = []
    monkeypatch.setattr(encoding, "PROBE_CACHE_LIMIT", 2)

    def smoke(ffmpeg, codec, width, height, fps, **kwargs):
        called.append((width, height, fps))
        return encoding._ProbeResult(codec, True)

    monkeypatch.setattr(encoding, "_smoke", smoke)
    for width in (640, 640, 1280, 1920, 640):
        encoding.select_encoder("ffmpeg", "libx264", width=width, height=480, fps=30)
    assert called == [(640, 480, 30), (1280, 480, 30), (1920, 480, 30), (640, 480, 30)]
    assert len(encoding._cache) == 2


def test_negative_probe_cache_expires_and_can_observe_recovery(monkeypatch):
    fake_inventory(monkeypatch)
    moment = [100.0]
    monkeypatch.setattr(encoding.time, "monotonic", lambda: moment[0])
    successes = [False, True]

    def smoke(ffmpeg, codec, *args, **kwargs):
        usable = successes.pop(0)
        return encoding._ProbeResult(codec, usable, None if usable else "encode-failed")

    monkeypatch.setattr(encoding, "_smoke", smoke)
    for _ in range(2):
        with pytest.raises(encoding.EncoderUnavailable):
            encoding.select_encoder(
                "ffmpeg",
                "h264_nvenc",
                width=1280,
                height=720,
                fps=60,
                platform="win32",
            )
    moment[0] += encoding.PROBE_FAILURE_TTL_SECONDS + 1
    selection = encoding.select_encoder(
        "ffmpeg",
        "h264_nvenc",
        width=1280,
        height=720,
        fps=60,
        platform="win32",
    )
    assert selection.codec == "h264_nvenc"
    assert not successes


def test_smoke_requires_packets_and_removes_private_temporary_media(monkeypatch):
    paths = []

    def run(command, *, media_path, **kwargs):
        paths.append(media_path)
        assert command[command.index("-frames:v") + 1] == "4"
        assert command[command.index("-fs") + 1] == str(encoding.PROBE_MEDIA_LIMIT)
        media_path.write_bytes(PACKETS)
        return 0, b""

    monkeypatch.setattr(encoding, "_run_bounded", run)
    result = encoding._smoke(
        "ffmpeg",
        "h264_nvenc",
        640,
        480,
        30,
        device=None,
        timeout=1,
        cancel=None,
    )
    assert result.usable
    assert paths and not paths[0].exists() and not paths[0].parent.exists()


@pytest.mark.parametrize("packet_data", [b"", b"arbitrary output", b"\x00\x00\x01\x67SPS-only"])
def test_success_exit_without_decodable_packet_types_is_not_readiness(monkeypatch, packet_data):
    def run(command, *, media_path, **kwargs):
        media_path.write_bytes(packet_data)
        return 0, b""

    monkeypatch.setattr(encoding, "_run_bounded", run)
    result = encoding._smoke(
        "ffmpeg",
        "h264_nvenc",
        640,
        480,
        30,
        device=None,
        timeout=1,
        cancel=None,
    )
    assert not result.usable


def test_probe_errors_do_not_return_binary_paths_or_diagnostics(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("rtsp://secret-user:secret-password@host/private-secret/path")

    monkeypatch.setattr(encoding, "_run_bounded", fail)
    result = encoding._smoke(
        "ffmpeg",
        "h264_nvenc",
        640,
        480,
        30,
        device=None,
        timeout=1,
        cancel=None,
    )
    assert result.reason == "probe-process-failed"
    assert "secret" not in str(result)


def test_probe_failure_rejects_packets_from_an_unsuccessful_process(monkeypatch):
    def fail(command, *, media_path, **kwargs):
        media_path.write_bytes(PACKETS)
        return 1, b"private diagnostic data"

    monkeypatch.setattr(encoding, "_run_bounded", fail)
    result = encoding._smoke(
        "ffmpeg",
        "h264_nvenc",
        640,
        480,
        30,
        device=None,
        timeout=1,
        cancel=None,
    )
    assert not result.usable and result.reason == "encode-failed"


def test_unavailable_binary_preserves_truthful_report(monkeypatch):
    monkeypatch.setattr(encoding, "_binary", lambda _: None)
    report = encoding.probe_encoders("private-missing-binary")
    assert not report["available"] and not report["encoders"] and not report["usable_encoders"]
    assert report["reason"] == "ffmpeg-unavailable" and not report["hardware_verified"]
    with pytest.raises(encoding.EncoderUnavailable, match="FFmpeg is unavailable"):
        encoding.select_encoder("private-missing-binary", "auto", width=640, height=480, fps=30)


def test_cross_platform_inventory_does_not_probe_an_unavailable_native_api(monkeypatch):
    fake_inventory(monkeypatch, ("h264_videotoolbox",))

    def unexpected_probe(*args, **kwargs):
        pytest.fail("Unsupported platform reached an encoder process")

    monkeypatch.setattr(encoding, "_smoke", unexpected_probe)
    report = encoding.probe_encoders("ffmpeg", platform="win32")
    assert report["encoders"] == ["h264_videotoolbox"]
    assert not report["usable_encoders"]
    assert report["details"]["h264_videotoolbox"]["reason"] == "platform-unsupported"


def test_vaapi_without_a_local_render_device_is_not_usable(monkeypatch):
    fake_inventory(monkeypatch, ("h264_vaapi",))
    monkeypatch.setattr(encoding, "_render_devices", lambda: ())
    with pytest.raises(encoding.EncoderUnavailable, match="render-device-unavailable"):
        encoding.select_encoder(
            "ffmpeg",
            "h264_vaapi",
            width=640,
            height=480,
            fps=30,
            platform="linux",
        )


def child_command(pid_path, body):
    return [
        # Windows venv python.exe is a launcher; exercise the actual owned child.
        getattr(sys, "_base_executable", sys.executable),
        "-c",
        "import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        + body,
        str(pid_path),
    ]


def assert_child_reaped(pid_path):
    assert pid_path.exists()
    pid = int(pid_path.read_text())
    deadline = time.monotonic() + 1
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not psutil.pid_exists(pid)


def test_bounded_probe_timeout_reaps_actual_child(tmp_path):
    pid_path = tmp_path / "child.pid"
    with pytest.raises(subprocess.TimeoutExpired):
        encoding._run_bounded(child_command(pid_path, "time.sleep(30)"), timeout=0.5)
    assert_child_reaped(pid_path)


def test_bounded_probe_output_overflow_reaps_actual_child(tmp_path):
    pid_path = tmp_path / "child.pid"
    command = child_command(
        pid_path, "sys.stdout.buffer.write(b'x'*1024); sys.stdout.flush(); time.sleep(30)"
    )
    with pytest.raises(encoding._OutputLimit, match="diagnostic output"):
        encoding._run_bounded(command, timeout=2, limit=512)
    assert_child_reaped(pid_path)


def test_bounded_probe_media_overflow_reaps_actual_child(tmp_path, monkeypatch):
    pid_path, media_path = tmp_path / "child.pid", tmp_path / "synthetic.h264"
    monkeypatch.setattr(encoding, "PROBE_MEDIA_LIMIT", 512)
    command = child_command(
        pid_path,
        "pathlib.Path(sys.argv[2]).write_bytes(b'x'*1024); time.sleep(30)",
    ) + [str(media_path)]
    with pytest.raises(encoding._OutputLimit, match="probe media"):
        encoding._run_bounded(command, timeout=2, media_path=media_path)
    assert_child_reaped(pid_path)


def test_canceled_selection_starts_no_child_and_caches_no_failure(monkeypatch):
    fake_inventory(monkeypatch)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(encoding.EncoderUnavailable, match="canceled"):
        encoding.select_encoder(
            "ffmpeg",
            "auto",
            width=640,
            height=480,
            fps=30,
            capabilities={"encoders": ["libx264", "h264_nvenc"]},
            cancel=cancel,
        )
    assert not encoding._cache


def test_bounded_probe_cancellation_reaps_actual_child(tmp_path):
    pid_path = tmp_path / "child.pid"
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set)
    timer.start()
    try:
        started = time.monotonic()
        with pytest.raises(encoding._ProbeCancelled):
            encoding._run_bounded(
                child_command(pid_path, "time.sleep(30)"), timeout=30, cancel=cancel
            )
        assert time.monotonic() - started < 2
        assert_child_reaped(pid_path)
    finally:
        timer.cancel()
        timer.join(timeout=2)


def test_cancellation_interrupts_wait_for_a_probe_slot(monkeypatch):
    fake_inventory(monkeypatch)
    cancel = threading.Event()
    blocked = threading.BoundedSemaphore(1)
    blocked.acquire()
    monkeypatch.setattr(encoding, "_probe_slots", blocked)
    timer = threading.Timer(0.05, cancel.set)
    timer.start()
    try:
        started = time.monotonic()
        with pytest.raises(encoding.EncoderUnavailable, match="canceled"):
            encoding.select_encoder(
                "ffmpeg",
                "auto",
                width=1280,
                height=720,
                fps=60,
                platform="win32",
                cancel=cancel,
            )
        assert time.monotonic() - started < 1
        assert not encoding._cache
    finally:
        blocked.release()
        timer.cancel()
        timer.join(timeout=2)


def test_concurrent_identical_cache_misses_share_one_probe(monkeypatch):
    fake_inventory(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    calls, results = [], []

    def smoke(ffmpeg, codec, *args, **kwargs):
        calls.append(codec)
        entered.set()
        assert release.wait(2)
        return encoding._ProbeResult(codec, True)

    monkeypatch.setattr(encoding, "_smoke", smoke)

    def select():
        results.append(
            encoding.select_encoder(
                "ffmpeg",
                "h264_nvenc",
                width=1280,
                height=720,
                fps=60,
                platform="win32",
            )
        )

    first, second = threading.Thread(target=select), threading.Thread(target=select)
    first.start()
    assert entered.wait(1)
    second.start()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()
    assert len(results) == 2 and calls == ["h264_nvenc"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_real_ffmpeg_software_probe_produces_h264_packets():
    selection = encoding.select_encoder("ffmpeg", "libx264", width=640, height=480, fps=60)
    assert selection.codec == "libx264"
    assert selection.probe_verified and not selection.hardware_verified
