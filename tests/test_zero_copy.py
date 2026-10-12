"""Compressed-input hardware-frame probes, fallback and ownership regressions."""

import io
import os
import shutil
import subprocess
import threading

import pytest
from PIL import Image

from campix import encoding

PACKETS = b"\x00\x00\x00\x01\x67abc\x00\x00\x01\x68def\x00\x00\x01\x65ghi"


@pytest.fixture(autouse=True)
def clean_probe_cache():
    encoding.invalidate_encoder_cache()
    yield
    encoding.invalidate_encoder_cache()


def jpeg(width=640, height=480, *, subsampling=2, progressive=False, color=(16, 32, 48)):
    with Image.new("RGB", (width, height), color) as image, io.BytesIO() as stream:
        image.save(stream, format="JPEG", subsampling=subsampling, progressive=progressive)
        return stream.getvalue()


def usable_encoder(monkeypatch, codec="h264_nvenc"):
    monkeypatch.setattr(encoding, "_binary", lambda _: ("ffmpeg", ("ffmpeg", 1, 2)))
    monkeypatch.setattr(encoding, "_inventory", lambda *args, **kwargs: (codec, "libx264"))
    monkeypatch.setattr(
        encoding,
        "_smoke",
        lambda ffmpeg, selected, *args, **kwargs: encoding._ProbeResult(selected, True),
    )


def select(**kwargs):
    return encoding.select_encoder(
        "ffmpeg", "auto", width=640, height=480, fps=60, platform="win32", **kwargs
    )


def test_ordinary_encoder_probe_never_certifies_hardware_frames(monkeypatch):
    usable_encoder(monkeypatch)
    ordinary = select()
    assert ordinary.hardware and not ordinary.zero_copy_raw_frames
    monkeypatch.setattr(
        encoding,
        "_smoke_zero_copy",
        lambda ffmpeg, codec, *args, **kwargs: encoding._ProbeResult(
            codec, False, "hardware-frame-encode-failed"
        ),
    )
    compressed = select(input_codec="mjpeg", input_sample=jpeg())
    assert compressed.codec == ordinary.codec
    assert compressed.hardware and not compressed.zero_copy_raw_frames
    assert compressed.input_args == ordinary.input_args
    assert compressed.output_args == ordinary.output_args
    assert compressed.zero_copy_reason == "hardware-frame-encode-failed"
    assert compressed.fallback_reason is None


def test_successful_compressed_probe_selects_strict_gpu_frames_without_qualification(monkeypatch):
    usable_encoder(monkeypatch)
    sample = jpeg()
    paths = []

    def run(command, *, media_path, **kwargs):
        source = media_path.parent / "input.mjpeg"
        paths.extend((media_path, source))
        assert source.read_bytes() == sample * 4
        assert command[command.index("-hwaccel") + 1] == "cuda"
        assert command[command.index("-hwaccel_output_format") + 1] == "cuda"
        assert command[command.index("-c:v") + 1] == "mjpeg_cuvid"
        assert command[command.index("-flags") + 1] == "+low_delay"
        assert command[command.index("-threads") + 1] == "1"
        assert command[command.index("-pix_fmt") + 1] == "+cuda"
        assert command[command.index("-fs") + 1] == str(encoding.PROBE_MEDIA_LIMIT)
        assert "-noautoscale" in command
        assert "lavfi" not in command and "-vf" not in command
        assert "hwupload" not in str(command) and "hwdownload" not in str(command)
        media_path.write_bytes(PACKETS)
        return 0, b""

    monkeypatch.setattr(encoding, "_run_bounded", run)
    result = select(input_codec="mjpeg", input_sample=sample)
    assert result.zero_copy_raw_frames
    assert result.hardware and result.probe_verified and not result.hardware_verified
    assert result.input_codec == "mjpeg" and result.zero_copy_reason is None
    assert paths and all(not path.exists() for path in paths)
    assert not paths[0].parent.exists()


@pytest.mark.parametrize(
    ("codec", "pixel_format", "device"),
    [
        ("h264_nvenc", "cuda", None),
        ("h264_qsv", "qsv", None),
        ("h264_vaapi", "vaapi", "/dev/dri/renderD128"),
    ],
)
def test_each_hardware_frame_pair_disables_cpu_conversion(codec, pixel_format, device):
    inputs, outputs = encoding._zero_copy_args(codec, 3840, 2160, 60, device=device)
    assert inputs[inputs.index("-hwaccel_output_format") + 1] == pixel_format
    assert outputs[outputs.index("-pix_fmt") + 1] == f"+{pixel_format}"
    assert "-noautoscale" in outputs and "-vf" not in outputs
    assert "hwdownload" not in str(outputs) and "hwupload" not in str(outputs)


@pytest.mark.parametrize(
    ("sample", "reason"),
    [
        (b"not a JPEG", "jpeg-input-invalid"),
        (b"\xff\xd8\xff\xe0\x00\x20\xff\xd9", "jpeg-input-invalid"),
        (jpeg(320, 240), "jpeg-profile-mismatch"),
        (jpeg(subsampling=0), "jpeg-chroma-unsupported"),
        (jpeg(subsampling=1), "jpeg-chroma-unsupported"),
        (jpeg(progressive=True), "jpeg-format-unsupported"),
    ],
)
def test_unsupported_jpeg_never_initializes_hardware_decoder(monkeypatch, sample, reason):
    usable_encoder(monkeypatch)
    monkeypatch.setattr(
        encoding, "_smoke_zero_copy", lambda *args, **kwargs: pytest.fail("Unexpected GPU probe")
    )
    result = select(input_codec="mjpeg", input_sample=sample)
    assert result.codec == "h264_nvenc" and result.hardware
    assert not result.zero_copy_raw_frames and result.zero_copy_reason == reason


def test_jpeg_header_preflight_does_not_decode_pixels(monkeypatch):
    sample = jpeg()
    monkeypatch.setattr(Image.Image, "load", lambda *args, **kwargs: pytest.fail("CPU JPEG decode"))
    assert encoding._jpeg_probe_sample(sample, 640, 480) == sample


def test_zero_copy_input_bytes_are_bounded_before_the_process(monkeypatch):
    usable_encoder(monkeypatch)
    monkeypatch.setattr(encoding, "PROBE_INPUT_LIMIT", 128)
    monkeypatch.setattr(
        encoding, "_smoke_zero_copy", lambda *args, **kwargs: pytest.fail("Unexpected GPU probe")
    )
    result = select(input_codec="mjpeg", input_sample=b"x" * 129)
    assert result.zero_copy_reason == "jpeg-input-size-invalid"
    assert not result.zero_copy_raw_frames


@pytest.mark.parametrize("data", [b"", b"arbitrary", b"\x00\x00\x01\x67SPS-only"])
def test_compressed_probe_success_requires_sps_pps_and_idr(monkeypatch, data):
    usable_encoder(monkeypatch)

    def run(command, *, media_path, **kwargs):
        media_path.write_bytes(data)
        return 0, b""

    monkeypatch.setattr(encoding, "_run_bounded", run)
    result = select(input_codec="mjpeg", input_sample=jpeg())
    assert result.hardware and not result.zero_copy_raw_frames
    assert result.zero_copy_reason in {"no-encoded-packets", "invalid-h264-packets"}


def test_compressed_probe_rejects_packets_from_failed_process(monkeypatch):
    usable_encoder(monkeypatch)

    def run(command, *, media_path, **kwargs):
        media_path.write_bytes(PACKETS)
        return 1, b"private camera credentials and path"

    monkeypatch.setattr(encoding, "_run_bounded", run)
    result = select(input_codec="mjpeg", input_sample=jpeg())
    assert not result.zero_copy_raw_frames
    assert result.zero_copy_reason == "hardware-frame-encode-failed"
    assert "private" not in str(result.as_dict())


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (subprocess.TimeoutExpired("private-path", 1), "probe-timed-out"),
        (encoding._OutputLimit("private-output"), "probe-output-limit"),
        (OSError("private-path"), "probe-process-failed"),
    ],
)
def test_compressed_probe_failure_is_sanitized_and_falls_back(monkeypatch, error, reason):
    usable_encoder(monkeypatch)
    paths = []

    def fail(command, *, media_path, **kwargs):
        paths.append(media_path)
        raise error

    monkeypatch.setattr(encoding, "_run_bounded", fail)
    result = select(input_codec="mjpeg", input_sample=jpeg())
    assert result.hardware and result.zero_copy_reason == reason
    assert "private" not in str(result.as_dict())
    assert not paths[0].parent.exists()


def test_cancelled_hardware_frame_probe_is_not_cached_or_software_fallback(monkeypatch):
    usable_encoder(monkeypatch)
    monkeypatch.setattr(
        encoding,
        "_run_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(encoding._ProbeCancelled()),
    )
    with pytest.raises(encoding.EncoderUnavailable, match="canceled"):
        select(input_codec="mjpeg", input_sample=jpeg(), cancel=threading.Event())
    assert all("mjpeg-hardware-frames" not in key for key in encoding._cache)


def test_hardware_frame_cache_is_separate_and_covers_actual_input_sample(monkeypatch):
    usable_encoder(monkeypatch)
    calls = []

    def smoke(ffmpeg, codec, *args, sample, **kwargs):
        calls.append(sample)
        return encoding._ProbeResult(codec, True)

    monkeypatch.setattr(encoding, "_smoke_zero_copy", smoke)
    first, second = jpeg(), jpeg(color=(64, 96, 128))
    for sample in (first, first, second):
        assert select(input_codec="mjpeg", input_sample=sample).zero_copy_raw_frames
    assert calls == [first, second]


def test_runtime_can_disable_hardware_frames_without_disabling_hardware_encoder(monkeypatch):
    usable_encoder(monkeypatch)
    monkeypatch.setattr(
        encoding, "_smoke_zero_copy", lambda *args, **kwargs: pytest.fail("Unexpected GPU probe")
    )
    result = select(input_codec="mjpeg", input_sample=jpeg(), zero_copy=False)
    assert result.hardware and not result.zero_copy_raw_frames
    assert result.zero_copy_reason == "hardware-frame-path-disabled"


def test_explicit_software_encoder_remains_software_when_zero_copy_requested(monkeypatch):
    usable_encoder(monkeypatch, "libx264")
    result = encoding.select_encoder(
        "ffmpeg",
        "libx264",
        width=640,
        height=480,
        fps=60,
        input_codec="mjpeg",
        input_sample=jpeg(),
    )
    assert result.codec == "libx264" and not result.hardware
    assert not result.zero_copy_raw_frames
    assert result.zero_copy_reason == "hardware-frame-path-unsupported"


@pytest.mark.parametrize("input_codec", ["h264;whoami", "cuda", "rawvideo"])
def test_decoder_input_codec_is_allowlisted_before_starting_process(input_codec, monkeypatch):
    monkeypatch.setattr(encoding, "_binary", lambda *args: pytest.fail("Unexpected binary lookup"))
    with pytest.raises(ValueError, match="Unsupported encoder input codec"):
        select(input_codec=input_codec)


@pytest.mark.skipif(
    os.environ.get("CAMPIX_TEST_GPU_ENCODER") != "h264_nvenc" or not shutil.which("ffmpeg"),
    reason="Explicit NVDEC/NVENC hardware test and FFmpeg required",
)
def test_real_nvdec_to_nvenc_4k60_probe_keeps_raw_frames_on_gpu():
    result = encoding.select_encoder(
        "ffmpeg",
        "h264_nvenc",
        width=3840,
        height=2160,
        fps=60,
        input_codec="mjpeg",
        input_sample=jpeg(3840, 2160),
    )
    assert result.zero_copy_raw_frames, result.zero_copy_reason
    assert result.hardware and not result.hardware_verified
    assert "+cuda" in result.output_args and "mjpeg_cuvid" in result.input_args
