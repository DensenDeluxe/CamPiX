"""Copy-only media publishing from the independent bounded H.264 subscription."""

import io
import json
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from campix.config import Settings
from campix.encoded import AnnexBParser, EncodedHub, ParsedAccessUnit
from campix.encoding import EncoderSelection
from campix.mediamtx import MediaPlane, Publisher
from campix.models import CameraConfig

DATA = b"\x00\x00\x00\x01\x09\xf0\x00\x00\x00\x01\x65\xb8\x80"


def hardware_selection():
    return EncoderSelection(
        codec="h264_nvenc",
        hardware=True,
        requested="auto",
        output_args=("-c:v", "h264_nvenc", "-pix_fmt", "+cuda"),
        input_args=("-hwaccel", "cuda", "-c:v", "mjpeg_cuvid"),
        input_codec="mjpeg",
        zero_copy_raw_frames=True,
    )


def camera(tmp_path, *, prime=True, selection=True):
    hub = EncodedHub()
    if prime:
        hub.publish(ParsedAccessUnit(DATA, True, b"config"), 100.0, epoch=hub.epoch)
    ready = threading.Event()
    ready.set()
    return SimpleNamespace(
        config=CameraConfig(
            id="test-only-shared",
            name="Test-only shared packets",
            backend="synthetic",
            width=160,
            height=120,
            fps=30,
        ),
        halt=threading.Event(),
        encoded_hub=hub,
        encoding_ready=ready,
        encoding_selection=hardware_selection() if selection else None,
        frame=lambda *args, **kwargs: pytest.fail("Copy publisher requested JPEG capture"),
    )


def publisher_for(camera, tmp_path):
    publisher = Publisher.__new__(Publisher)
    publisher.camera, publisher.settings = camera, Settings(data_dir=tmp_path)
    publisher.halt = threading.Event()
    publisher.spawn_lock = threading.Lock()
    publisher.proc, publisher.error, publisher.online = None, None, False
    publisher.selection = None
    publisher.failed_encoders, publisher.failed_until = set(), {}
    return publisher


def forbid_encoder_selection(monkeypatch):
    monkeypatch.setattr(
        "campix.mediamtx.select_encoder",
        lambda *args, **kwargs: pytest.fail(
            "Publishing transport tried to reselect the camera encoder"
        ),
    )


def fake_child(pipe):
    return SimpleNamespace(stdin=pipe, terminated=False, poll=lambda: None)


def reap(child):
    child.terminated = True


@pytest.mark.parametrize("selection", [True, False])
def test_shared_and_uvc_publishers_copy_packets_without_starting_an_encoder(
    tmp_path, monkeypatch, selection
):
    source = camera(tmp_path, selection=selection)
    publisher = publisher_for(source, tmp_path)
    commands, received, children = [], [], []

    class Pipe(io.BytesIO):
        def write(self, data):
            received.append(data)
            publisher.halt.set()
            return super().write(data)

    def launch(command, **kwargs):
        commands.append(command)
        child = fake_child(Pipe())
        children.append(child)
        return child

    forbid_encoder_selection(monkeypatch)
    monkeypatch.setattr("campix.mediamtx.subprocess.Popen", launch)
    monkeypatch.setattr("campix.mediamtx.terminate_process", reap)
    publisher._run()
    command = commands[0]
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-f") + 1] == "mpegts"
    assert "-framerate" not in command and "-use_wallclock_as_timestamps" not in command
    assert "-hwaccel" not in command and "-pix_fmt" not in command and "-vf" not in command
    assert command[-1] == publisher.settings.mediamtx_rtsp.rstrip("/") + "/" + source.config.id
    assert received and received[0].startswith(b"\x47") and len(received[0]) % 188 == 0
    assert publisher.selection is source.encoding_selection
    assert publisher.packet_passthrough and publisher.packets_sent == 1
    assert source.encoded_hub.stats()["subscribers"] == 0
    assert all(child.terminated and child.stdin.closed for child in children)
    assert publisher.online is False


def test_rtsp_failure_reconnects_without_disabling_healthy_gpu(tmp_path, monkeypatch):
    source = camera(tmp_path)
    publisher = publisher_for(source, tmp_path)
    selection = source.encoding_selection
    children, errors, backoffs = [], [], []

    class Pipe(io.BytesIO):
        def write(self, data):
            if len(children) == 1:
                raise BrokenPipeError("rtsp://test-secret-user:test-secret-password@host/private")
            publisher.halt.set()
            return super().write(data)

    def launch(*args, **kwargs):
        child = fake_child(Pipe())
        children.append(child)
        return child

    def backoff(timeout):
        backoffs.append(timeout)
        errors.append(publisher.error)
        return publisher.halt.is_set()

    forbid_encoder_selection(monkeypatch)
    monkeypatch.setattr("campix.mediamtx.subprocess.Popen", launch)
    monkeypatch.setattr("campix.mediamtx.terminate_process", reap)
    monkeypatch.setattr(publisher.halt, "wait", backoff)
    publisher._run()
    assert len(children) == 2 and all(child.terminated and child.stdin.closed for child in children)
    assert errors[0] == "Media packet publisher disconnected; retrying"
    assert backoffs == [3.0, 3.0]
    assert source.encoding_selection is selection and publisher.selection is selection
    assert not publisher.failed_encoders and not publisher.failed_until
    assert "secret" not in str(errors)


def test_encoder_epoch_restart_reconnects_with_fresh_transport_clock(tmp_path, monkeypatch):
    source = camera(tmp_path)
    publisher = publisher_for(source, tmp_path)
    children, payloads, backoffs = [], [], []

    class Pipe(io.BytesIO):
        def write(self, data):
            payloads.append(data)
            if len(children) == 1:
                epoch = source.encoded_hub.restart()
                source.encoded_hub.publish(
                    ParsedAccessUnit(DATA, True, b"config"), 200.0, epoch=epoch
                )
            else:
                publisher.halt.set()
            return super().write(data)

    def launch(*args, **kwargs):
        child = fake_child(Pipe())
        children.append(child)
        return child

    def backoff(timeout):
        backoffs.append(timeout)
        return publisher.halt.is_set()

    monkeypatch.setattr("campix.mediamtx.subprocess.Popen", launch)
    monkeypatch.setattr("campix.mediamtx.terminate_process", reap)
    monkeypatch.setattr(publisher.halt, "wait", backoff)
    publisher._run()
    assert len(children) == 2 and payloads[0] == payloads[1]
    assert backoffs == [0.1, 3.0]
    assert all(child.terminated and child.stdin.closed for child in children)
    assert source.encoded_hub.stats()["subscribers"] == 0


def test_stalled_network_consumer_resyncs_at_idr_without_blocking_other_consumers(
    tmp_path, monkeypatch
):
    source = camera(tmp_path)
    publisher = publisher_for(source, tmp_path)
    recording = source.encoded_hub.subscribe("recording", pre_roll_seconds=1)
    writing, release = threading.Event(), threading.Event()
    received, errors = [], []

    class Pipe(io.BytesIO):
        def write(self, data):
            received.append(data)
            if len(received) == 1:
                writing.set()
                assert release.wait(5), "Test did not release the stalled RTSP pipe"
            else:
                publisher.halt.set()
            return super().write(data)

    child = fake_child(Pipe())
    monkeypatch.setattr("campix.mediamtx.subprocess.Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr("campix.mediamtx.terminate_process", reap)

    def run():
        try:
            publisher._run()
        except Exception as error:
            errors.append(error)

    publisher.thread = threading.Thread(target=run)
    publisher.thread.start()
    try:
        assert writing.wait(3)
        started = time.monotonic()
        for index in range(150):
            source.encoded_hub.publish(
                ParsedAccessUnit(DATA, False, b"config"),
                100.0 + (index + 1) / 60,
                epoch=source.encoded_hub.epoch,
            )
        source.encoded_hub.publish(
            ParsedAccessUnit(DATA, True, b"config"), 103.0, epoch=source.encoded_hub.epoch
        )
        assert time.monotonic() - started < 1
        assert source.encoded_hub.stats()["published_frames"] == 152
        assert recording.read(timeout=0).sequence == 1
        release.set()
        publisher.thread.join(timeout=3)
        assert len(received) == 2 and not publisher.thread.is_alive()
        assert not errors and child.terminated and child.stdin.closed
    finally:
        release.set()
        publisher.stop()
        recording.close()


def test_shutdown_before_first_idr_launches_no_remux_process(tmp_path, monkeypatch):
    source = camera(tmp_path, prime=False)
    monkeypatch.setattr(
        "campix.mediamtx.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("Publisher spawned before packets"),
    )
    publisher = Publisher(source, Settings(data_dir=tmp_path))
    try:
        deadline = time.monotonic() + 2
        while source.encoded_hub.stats()["subscribers"] != 1 and time.monotonic() < deadline:
            time.sleep(0.005)
    finally:
        publisher.stop()
    assert not publisher.thread.is_alive() and publisher.proc is None
    assert source.encoded_hub.stats()["subscribers"] == 0


def test_shutdown_after_late_subscription_result_cannot_launch_a_child(tmp_path, monkeypatch):
    source = camera(tmp_path)
    publisher = publisher_for(source, tmp_path)
    original_subscribe = source.encoded_hub.subscribe

    def subscribe(*args, **kwargs):
        subscription = original_subscribe(*args, **kwargs)
        original_read = subscription.read

        def read(*args, **kwargs):
            packet = original_read(*args, **kwargs)
            publisher.halt.set()
            return packet

        subscription.read = read
        return subscription

    monkeypatch.setattr(source.encoded_hub, "subscribe", subscribe)
    monkeypatch.setattr(
        "campix.mediamtx.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("Stopped publisher adopted a late packet and spawned"),
    )
    publisher._run()
    assert source.encoded_hub.stats()["subscribers"] == 0
    assert publisher.proc is None


def test_shutdown_unblocks_real_copy_process_pipe_and_reaps_child(tmp_path, monkeypatch):
    source = camera(tmp_path, prime=False)
    source.encoded_hub.publish(
        ParsedAccessUnit(DATA + b"x" * (1024 * 1024), True, b"config"),
        100.0,
        epoch=source.encoded_hub.epoch,
    )
    original_spawn = subprocess.Popen
    writing = threading.Event()

    def launch(command, **kwargs):
        child = original_spawn(
            [getattr(sys, "_base_executable", sys.executable), "-c", "import time; time.sleep(30)"],
            **kwargs,
        )
        original_pipe = child.stdin

        class Pipe:
            def write(self, data):
                writing.set()
                return original_pipe.write(data)

            def flush(self):
                return original_pipe.flush()

            @property
            def closed(self):
                return original_pipe.closed

            def close(self):
                original_pipe.close()

        child.stdin = Pipe()
        return child

    monkeypatch.setattr("campix.mediamtx.subprocess.Popen", launch)
    publisher = Publisher(source, Settings(data_dir=tmp_path))
    try:
        assert writing.wait(5)
    finally:
        publisher.stop()
    assert publisher.proc.poll() is not None and publisher.proc.stdin.closed
    assert not publisher.thread.is_alive() and not publisher.online
    assert source.encoded_hub.stats()["subscribers"] == 0


def test_media_status_distinguishes_packet_readiness_from_inventory(tmp_path):
    source = camera(tmp_path)
    publisher = publisher_for(source, tmp_path)
    publisher.packet_passthrough = True
    publisher.selection = source.encoding_selection
    publisher.packets_sent = 2
    publisher.last_packet_at = time.monotonic()
    publisher.online = True
    plane = MediaPlane(Settings(data_dir=tmp_path), None, None)
    plane.publishers = {source.config.id: publisher}
    plane.ready_paths = {source.config.id}
    status = plane.status()["publishers"][source.config.id]
    assert status["packet_passthrough"] and status["packet_ready"]
    assert status["encoder"] == "h264_nvenc" and status["zero_copy_raw_frames"]
    publisher.last_packet_at -= 4
    assert not plane.status()["publishers"][source.config.id]["packet_ready"]


def test_shutdown_clears_packet_readiness_before_waiting_for_child_cleanup(tmp_path, monkeypatch):
    source = camera(tmp_path)
    publisher = publisher_for(source, tmp_path)
    publisher.proc = fake_child(io.BytesIO())
    publisher.online, publisher.packet_passthrough = True, True
    publisher.last_packet_at = time.monotonic()
    publisher.thread = SimpleNamespace(join=lambda timeout: None)
    entered, release = threading.Event(), threading.Event()

    def stalled_cleanup(child):
        entered.set()
        assert release.wait(3), "Test did not release the child cleanup"
        child.terminated = True

    monkeypatch.setattr("campix.mediamtx.terminate_process", stalled_cleanup)
    stopper = threading.Thread(target=publisher.stop)
    stopper.start()
    try:
        assert entered.wait(2)
        assert publisher.halt.is_set() and not publisher.online
        assert publisher.last_packet_at is None
    finally:
        release.set()
        stopper.join(timeout=3)
        publisher.proc.stdin.close()
    assert publisher.proc.terminated and not stopper.is_alive()


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg and FFprobe required"
)
def test_real_copy_publisher_preserves_slow_capture_clock_without_a_second_encoder(
    tmp_path, monkeypatch
):
    source_path, target = tmp_path / "test-only.h264", tmp_path / "test-only.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x120:r=30",
            "-frames:v",
            "11",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-bf",
            "0",
            "-g",
            "30",
            "-bsf:v",
            "h264_metadata=aud=insert",
            "-f",
            "h264",
            str(source_path),
        ],
        capture_output=True,
        check=True,
        timeout=10,
    )
    parser = AnnexBParser()
    parsed = parser.feed(source_path.read_bytes()) + parser.finish()
    source = camera(tmp_path, prime=False)
    for index, packet in enumerate(parsed):
        source.encoded_hub.publish(packet, 100.0 + index / 5, epoch=source.encoded_hub.epoch)
    publisher = publisher_for(source, tmp_path)
    original_spawn = subprocess.Popen
    commands, children = [], []

    def local_output(command, **kwargs):
        commands.append(command)
        last_format = max(index for index, value in enumerate(command) if value == "-f")
        child = original_spawn(
            [*command[:last_format], "-movflags", "+faststart", "-f", "mp4", str(target)], **kwargs
        )
        children.append(child)
        return child

    class RecordedPipe:
        def __init__(self, pipe):
            self.pipe = pipe

        def write(self, data):
            result = self.pipe.write(data)
            return result

        def flush(self):
            self.pipe.flush()
            if publisher.packets_sent == len(parsed) - 1:
                self.pipe.close()
                children[-1].wait(timeout=5)
                publisher.halt.set()

        @property
        def closed(self):
            return self.pipe.closed

        def close(self):
            self.pipe.close()

    def launch(command, **kwargs):
        child = local_output(command, **kwargs)
        child.stdin = RecordedPipe(child.stdin)
        return child

    forbid_encoder_selection(monkeypatch)
    with monkeypatch.context() as publishing:
        publishing.setattr("campix.mediamtx.subprocess.Popen", launch)
        publisher._run()
    assert commands[0][commands[0].index("-c:v") + 1] == "copy"
    assert all(child.poll() is not None and child.stdin.closed for child in children)
    report = json.loads(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_packets", "-show_format", "-of", "json", str(target)],
            timeout=10,
        )
    )
    times = [float(packet["pts_time"]) for packet in report["packets"]]
    assert times == pytest.approx([index / 5 for index in range(11)], abs=1 / 90000)
    assert 2.0 <= float(report["format"]["duration"]) <= 2.21
    assert publisher.packets_sent == 11
