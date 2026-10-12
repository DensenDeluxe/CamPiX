import io
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import replace
from types import SimpleNamespace

import psutil
import pytest
from PIL import Image

from campix import shared_encoder
from campix.capture import CaptureError
from campix.encoded import EncodedHub, EncodedRestart
from campix.encoding import EncoderSelection, encoder_args
from campix.shared_encoder import SharedEncoder


def jpeg_frame(width=160, height=90):
    image = Image.new("RGB", (width, height), (35, 100, 170))
    output = io.BytesIO()
    image.save(output, "JPEG", quality=85)
    return output.getvalue()


class Camera:
    def __init__(self, ffmpeg="ffmpeg", priming=5):
        self.config = SimpleNamespace(
            id="test-shared", width=160, height=90, fps=10, encoder="libx264"
        )
        self.settings = SimpleNamespace(ffmpeg=ffmpeg)
        self.encoders = ("libx264",)
        self.condition = threading.Condition()
        self.halt = threading.Event()
        self.encoded_hub = EncodedHub()
        self.jpeg = jpeg_frame()
        now = time.monotonic()
        self.preroll = deque((now - (priming - index) / 10, self.jpeg) for index in range(priming))
        self.frame_times = deque([self.preroll[-1][0]] if priming else [now], maxlen=120)
        self.sequence = priming
        self.seen_sequence = 0
        self.events = []
        self.bus = SimpleNamespace(
            publish=lambda *args, **kwargs: self.events.append((args, kwargs))
        )
        self.encoding_selection = None

    def frame(self, sequence=0, timeout=1):
        with self.condition:
            self.condition.wait_for(
                lambda: self.sequence != sequence or self.halt.is_set(), timeout=timeout
            )
            self.seen_sequence = self.sequence
            self.condition.notify_all()
            return self.sequence, self.jpeg

    def emit(self):
        with self.condition:
            self.sequence += 1
            self.frame_times.append(time.monotonic())
            self.condition.notify_all()
            return self.sequence

    def wait_seen(self, sequence):
        with self.condition:
            return self.condition.wait_for(lambda: self.seen_sequence >= sequence, timeout=2)

    def software_frame_args(self, codec):
        return encoder_args(codec, self.config.width, self.config.height, self.config.fps)


def selection(camera):
    inputs, outputs = camera.software_frame_args("libx264")
    return EncoderSelection("libx264", False, "libx264", outputs, inputs, input_codec="mjpeg")


def await_condition(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_actual_single_ffmpeg_encoder_fans_out_identical_units_and_priming_times():
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg unavailable: actual shared software encoder integration")
    camera = Camera(ffmpeg)
    live = camera.encoded_hub.subscribe()
    recording = camera.encoded_hub.subscribe("recording", pre_roll_seconds=1)
    encoder = SharedEncoder(camera, selection(camera))
    process = None
    try:
        encoder.start()
        for _ in range(12):
            sequence = camera.emit()
            assert camera.wait_seen(sequence)
            time.sleep(0.01)
        await_condition(lambda: camera.encoded_hub.stats()["published_frames"] >= 8)
        process = encoder.proc
        assert process is not None and process.poll() is None
        assert encoder.sessions_started == 1
        assert encoder.error is None
        first = live.read(timeout=2)
        assert first is not None and first.keyframe
        assert first.captured_at == camera.preroll[0][0]
        assert first is recording.read(timeout=2)
        for _ in range(7):
            assert live.read(timeout=2) is recording.read(timeout=2)
        assert camera.encoded_hub.stats()["subscribers"] == 2
        assert encoder.frames_encoded >= 8
    finally:
        encoder.stop()
        live.close()
        recording.close()
    assert not encoder.thread.is_alive()
    assert encoder.reader_thread is not None and not encoder.reader_thread.is_alive()
    assert process is not None and process.poll() is not None
    assert not psutil.pid_exists(process.pid)


def test_stop_interrupts_blocked_input_output_without_orphan_and_invalidates_epoch(monkeypatch):
    original_popen = subprocess.Popen
    children = []

    def unresponsive_encoder(command, **options):
        process = original_popen([sys.executable, "-c", "import time; time.sleep(60)"], **options)
        children.append(process)
        return process

    monkeypatch.setattr(shared_encoder.subprocess, "Popen", unresponsive_encoder)
    camera = Camera(priming=100)
    old_epoch = camera.encoded_hub.epoch
    subscription = camera.encoded_hub.subscribe("recording")
    encoder = SharedEncoder(camera, selection(camera))
    encoder.start()
    try:
        await_condition(lambda: encoder.proc is not None and len(encoder.pending) >= 3)
        began = time.monotonic()
        encoder.stop()
        assert time.monotonic() - began < 3
        assert not encoder.thread.is_alive()
        assert encoder.reader_thread is not None and not encoder.reader_thread.is_alive()
        assert camera.encoded_hub.epoch > old_epoch
        with pytest.raises(EncodedRestart):
            subscription.read(timeout=0)
    finally:
        encoder.stop()
        subscription.close()
        for process in children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
    assert len(children) == 1
    await_condition(lambda: not psutil.pid_exists(children[0].pid), timeout=3)


def test_camera_shutdown_reaps_blocked_encoder_without_restarting_closed_hub(monkeypatch):
    original_popen = subprocess.Popen

    def unresponsive_encoder(command, **options):
        return original_popen([sys.executable, "-c", "import time; time.sleep(60)"], **options)

    monkeypatch.setattr(shared_encoder.subprocess, "Popen", unresponsive_encoder)
    camera = Camera(priming=100)
    encoder = SharedEncoder(camera, selection(camera))
    process = None
    encoder.start()
    try:
        await_condition(lambda: encoder.proc is not None and len(encoder.pending) >= 3)
        process = encoder.proc
        camera.halt.set()
        camera.encoded_hub.close()
    finally:
        encoder.stop()
    assert not encoder.thread.is_alive()
    assert encoder.reader_thread is not None and not encoder.reader_thread.is_alive()
    assert process is not None and process.poll() is not None
    assert encoder.proc is None


def test_reader_handles_closed_hub_without_uncaught_runtime_exception():
    start = b"\x00\x00\x00\x01"
    aud = start + b"\x09\xf0"
    config = start + b"\x67\x42\x00\x1e\xc0" + start + b"\x68\xe0"
    stream = aud + config + start + b"\x65\xbc" + aud + start + b"\x41\xf0"
    camera = Camera()
    encoder = SharedEncoder(camera, selection(camera))
    encoder.pending.append(time.monotonic())
    terminated = threading.Event()
    process = SimpleNamespace(
        stdout=io.BytesIO(stream),
        poll=lambda: 0 if terminated.is_set() else None,
        terminate=terminated.set,
        wait=lambda timeout: 0,
    )
    encoder.proc = process
    camera.encoded_hub.close()
    encoder._read(process, camera.encoded_hub.epoch)
    assert encoder.reader_error == "EncodedStreamEnded"
    assert terminated.is_set()


def test_pending_frame_bound_prevents_unbounded_input_without_writing():
    camera = Camera()
    encoder = SharedEncoder(camera, selection(camera))
    encoder.pending.extend(range(240))
    process = SimpleNamespace(stdin=io.BytesIO())
    with pytest.raises(CaptureError, match="pending frame bound"):
        encoder._offer(process, camera.jpeg, 241)
    assert len(encoder.pending) == 240
    assert process.stdin.getvalue() == b""


def test_late_reader_cannot_consume_new_generation_timestamps_or_set_new_error():
    start = b"\x00\x00\x00\x01"
    aud = start + b"\x09\xf0"
    config = start + b"\x67\x42\x00\x1e\xc0" + start + b"\x68\xe0"
    stream = aud + config + start + b"\x65\xbc" + aud + start + b"\x41\xf0"
    camera = Camera()
    encoder = SharedEncoder(camera, selection(camera))
    old_epoch = camera.encoded_hub.epoch
    camera.encoded_hub.restart()
    captured = time.monotonic()
    encoder.pending.append(captured)
    stopped = threading.Event()
    old_process = SimpleNamespace(
        stdout=io.BytesIO(stream),
        poll=lambda: 0 if stopped.is_set() else None,
        terminate=stopped.set,
        wait=lambda timeout: 0,
    )
    encoder.proc = SimpleNamespace(stdout=io.BytesIO())
    encoder._read(old_process, old_epoch)
    assert list(encoder.pending) == [captured]
    assert encoder.reader_error is None
    assert encoder.frames_encoded == 0


def stop_after_first_retry(encoder):
    original_wait = encoder.halt.wait

    def wait(timeout):
        if timeout >= 1:
            encoder.halt.set()
            return True
        return original_wait(timeout)

    encoder.halt.wait = wait


def test_source_disconnect_does_not_exclude_or_degrade_a_healthy_gpu(monkeypatch):
    camera = Camera(priming=0)
    camera.config.encoder = "auto"
    inputs, outputs = encoder_args("h264_nvenc", 160, 90, 10)
    chosen = EncoderSelection(
        "h264_nvenc",
        True,
        "auto",
        outputs,
        inputs,
        input_codec="mjpeg",
        zero_copy_raw_frames=True,
    )
    encoder = SharedEncoder(camera, chosen)
    stop_after_first_retry(encoder)

    def disconnected(*args, **kwargs):
        raise CaptureError("Camera source disconnected")

    camera.frame_packet = disconnected

    class Owner:
        def __init__(self, command):
            self.done = threading.Event()
            self.process = SimpleNamespace(
                stdin=io.BytesIO(),
                stdout=SimpleNamespace(read1=lambda size: b"" if self.done.is_set() else None),
                poll=lambda: 0 if self.done.is_set() else None,
                terminate=self.done.set,
                wait=lambda timeout: 0,
            )

        def start(self):
            return self.process

        def close(self):
            self.done.set()

    monkeypatch.setattr(shared_encoder, "_ProcessOwner", Owner)
    monkeypatch.setattr(
        shared_encoder,
        "select_encoder",
        lambda *args, **kwargs: pytest.fail("Capture failure must not reclassify the GPU"),
    )
    encoder._run()
    assert encoder.selection is chosen
    assert "_SourceUnavailable" in encoder.error
    assert len(camera.events) == 1
    assert not encoder.reader_thread.is_alive()


@pytest.mark.parametrize(
    "codec,device", [("h264_nvenc", None), ("h264_vaapi", "/dev/dri/renderD128")]
)
def test_auto_degrades_hardware_decoding_before_replacing_hardware_encoder(
    monkeypatch, codec, device
):
    camera = Camera(priming=0)
    camera.config.encoder = "auto"
    inputs, outputs = encoder_args(codec, 160, 90, 10, device=device)
    chosen = replace(
        EncoderSelection(codec, True, "auto", outputs, inputs),
        zero_copy_raw_frames=True,
        input_codec="mjpeg",
    )
    encoder = SharedEncoder(camera, chosen)
    stop_after_first_retry(encoder)

    class Owner:
        def __init__(self, command):
            pass

        def start(self):
            raise OSError("Test-only private driver detail")

        def close(self):
            pass

    monkeypatch.setattr(shared_encoder, "_ProcessOwner", Owner)
    encoder._run()
    assert encoder.selection.codec == codec
    assert encoder.selection.hardware
    assert not encoder.selection.zero_copy_raw_frames
    assert encoder.selection.zero_copy_reason == "runtime-hardware-decoder-failed"
    assert encoder.selection.input_args == inputs
    assert encoder.selection.output_args == outputs
    assert "private driver detail" not in encoder.error
    assert len(camera.events) == 1
