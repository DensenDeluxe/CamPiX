"""Lossless shared-encoder recording, finite shutdown and transactional media visibility."""

import contextlib
import json
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from campix import recording
from campix.encoded import EncodedHub, ParsedAccessUnit
from tests import test_tsmux
from tests.test_encoded import packet

real_access_units = test_tsmux.real_access_units


def profile(preroll=3):
    return SimpleNamespace(id="generated", fps=30, preroll_seconds=preroll)


def store(tmp_path):
    saved, events, reserved = [], [], []
    path = tmp_path / "test-only.mp4"

    def reserve(extension):
        reserved.append(extension)
        return "test-only", path

    def register(*args):
        saved.append(args)
        return {"id": args[0], "duration": args[-1], "filename": args[1].name}

    return SimpleNamespace(
        reserve=reserve,
        register=register,
        check_space=lambda: None,
        bus=SimpleNamespace(publish=lambda *args: events.append(args)),
        saved=saved,
        events=events,
        reserved=reserved,
    )


def seed(hub, count=5):
    now = time.monotonic()
    for index in range(count):
        hub.publish(packet(index == 0), now - (count - index - 1) / 30, epoch=hub.epoch)


def box(kind, payload):
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def mp4(samples):
    table = box(b"stsz", b"\0" * 4 + (10).to_bytes(4, "big") + samples.to_bytes(4, "big"))
    for kind in (b"stbl", b"minf", b"mdia", b"trak", b"moov"):
        table = box(kind, table)
    return box(b"ftyp", b"isom\0\0\0\0isom") + table + box(b"mdat", b"test-only")


class Process:
    def __init__(self, path, block=None, failure=False, invalid=False):
        self.path, self.block, self.failure, self.invalid = path, block, failure, invalid
        self.returncode = None
        self.writes = 0
        self.entered = threading.Event()
        self.released = threading.Event()
        self.stdin = SimpleNamespace(write=self.write, close=self.close, closed=False)
        self.path.write_bytes(b"interrupted test-only media")

    def write(self, data):
        self.entered.set()
        if self.block:
            self.released.wait(5)
        if self.returncode is not None or self.failure:
            raise BrokenPipeError("Remuxer exited")
        self.writes += 1
        return len(data)

    def close(self):
        self.stdin.closed = True
        if self.returncode is None:
            self.path.write_bytes(b"invalid" if self.invalid else mp4(self.writes))
            self.returncode = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 1
        self.released.set()

    kill = terminate

    def wait(self, timeout=None):
        return self.returncode


def launcher(monkeypatch, **options):
    opened = []

    @contextlib.contextmanager
    def own(command, **kwargs):
        from pathlib import Path

        process = Process(Path(command[-1]), **options)
        opened.append((command, kwargs, process))
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()

    monkeypatch.setattr(recording, "owned_process", own)
    return opened


def test_recorder_only_remuxes_and_registers_after_all_samples_finalize(monkeypatch, tmp_path):
    opened = launcher(monkeypatch)
    hub, media = EncodedHub(), store(tmp_path)
    seed(hub, 10)
    recorder = recording.EncodedRecorder(profile(), SimpleNamespace(ffmpeg="ffmpeg"), media, hub)
    try:
        result = recorder.stop()
        assert result["id"] == "test-only"
        assert recorder.frames == 10
        assert recorder.dropped == 0
        assert recorder.path.exists() and not recorder.partial.exists()
        assert len(media.saved) == 1
        assert media.saved[0][-1] == pytest.approx(10 / 30)
        command = opened[0][0]
        assert command[command.index("-f") + 1] == "mpegts"
        assert command[command.index("-c:v") + 1] == "copy"
        assert "-vf" not in command and "-c:v" in command
        assert command[command.index("-i") + 1] == "pipe:0"
        assert hub.stats()["subscribers"] == 0
    finally:
        hub.close()


def test_zero_preroll_waits_for_next_idr_in_worker_and_observes_duration(monkeypatch, tmp_path):
    launcher(monkeypatch)
    hub, media = EncodedHub(), store(tmp_path)
    seed(hub)
    recorder = recording.EncodedRecorder(
        profile(preroll=0), SimpleNamespace(ffmpeg="ffmpeg"), media, hub, duration=0.4
    )
    try:
        assert recorder.frames == 0
        assert not recorder.done.is_set()
        hub.publish(packet(True), time.monotonic(), epoch=hub.epoch)
        assert recorder.done.wait(2)
        assert recorder.stop()["id"] == "test-only"
        assert recorder.frames == 1
        assert len(media.saved) == 1
    finally:
        if not recorder.done.is_set():
            recorder.stop()
        hub.close()


def test_stop_freezes_queue_and_drains_only_through_requested_boundary(monkeypatch, tmp_path):
    launcher(monkeypatch, block=True)
    hub, media = EncodedHub(), store(tmp_path)
    seed(hub, 15)
    recorder = recording.EncodedRecorder(profile(), SimpleNamespace(ffmpeg="ffmpeg"), media, hub)
    assert recorder.proc.entered.wait(2)
    recorder._request_stop()
    assert recorder.cutoff == 15
    for _ in range(1000):
        hub.publish(packet(), time.monotonic(), epoch=hub.epoch)
    recorder.proc.released.set()
    try:
        assert recorder.stop()["id"] == "test-only"
        assert recorder.frames == 15
        assert recorder.last_sequence == recorder.cutoff
        assert len(media.saved) == 1
    finally:
        hub.close()


@pytest.mark.parametrize("failure", ["restart", "overrun", "disk"])
def test_recorder_failure_keeps_partial_and_never_indexes(monkeypatch, tmp_path, failure):
    launcher(monkeypatch, block=True)
    hub, media = EncodedHub(), store(tmp_path)
    seed(hub, 35)
    recorder = recording.EncodedRecorder(profile(), SimpleNamespace(ffmpeg="ffmpeg"), media, hub)
    assert recorder.proc.entered.wait(2)
    if failure == "restart":
        hub.restart()
    elif failure == "overrun":
        for _ in range(700):
            hub.publish(packet(), time.monotonic(), epoch=hub.epoch)
    else:

        def no_space():
            raise OSError("Storage reserve reached")

        media.check_space = no_space
    recorder.proc.released.set()
    try:
        assert recorder.done.wait(3)
        with pytest.raises(RuntimeError, match="Recording failed"):
            recorder.stop()
        assert not media.saved
        assert recorder.partial.exists() and not recorder.path.exists()
        assert media.events[0][0] == "recording.error"
        assert hub.stats()["subscribers"] == 0
    finally:
        hub.close()


def test_idle_remuxer_exit_is_observed_without_more_capture_frames(monkeypatch, tmp_path):
    launcher(monkeypatch)
    hub, media = EncodedHub(), store(tmp_path)
    seed(hub)
    recorder = recording.EncodedRecorder(
        profile(preroll=0), SimpleNamespace(ffmpeg="ffmpeg"), media, hub
    )
    recorder.proc.terminate()
    try:
        assert recorder.done.wait(2)
        assert not media.saved
        assert recorder.partial.exists()
        with pytest.raises(RuntimeError, match="Recording failed"):
            recorder.stop()
    finally:
        hub.close()


def test_blocked_remuxer_is_killed_within_shutdown_deadline(monkeypatch, tmp_path):
    launcher(monkeypatch, block=True)
    monkeypatch.setattr(recording.EncodedRecorder, "stop_timeout", 0.25)
    hub, media = EncodedHub(), store(tmp_path)
    seed(hub)
    recorder = recording.EncodedRecorder(profile(), SimpleNamespace(ffmpeg="ffmpeg"), media, hub)
    assert recorder.proc.entered.wait(2)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="Recording failed"):
            recorder.stop()
        assert time.monotonic() - started < 2
        assert recorder.done.is_set() and not recorder.thread.is_alive()
        assert not recorder.watch_thread.is_alive()
        assert recorder.proc.poll() is not None
        assert recorder.partial.exists() and not media.saved
    finally:
        hub.close()


def test_invalid_finalized_media_is_retained_without_index(monkeypatch, tmp_path):
    launcher(monkeypatch, invalid=True)
    hub, media = EncodedHub(), store(tmp_path)
    seed(hub)
    recorder = recording.EncodedRecorder(profile(), SimpleNamespace(ffmpeg="ffmpeg"), media, hub)
    try:
        with pytest.raises(RuntimeError, match="Recording failed"):
            recorder.stop()
        assert recorder.partial.exists() and not recorder.path.exists()
        assert not media.saved
    finally:
        hub.close()


def test_unready_stream_and_failed_reservation_leave_no_subscription(monkeypatch, tmp_path):
    launcher(monkeypatch)
    hub, media = EncodedHub(), store(tmp_path)
    with pytest.raises(RuntimeError, match="decodable stream"):
        recording.EncodedRecorder(profile(), SimpleNamespace(ffmpeg="ffmpeg"), media, hub)
    assert not media.reserved
    seed(hub)

    def reserve(extension):
        raise OSError("No storage space")

    media.reserve = reserve
    with pytest.raises(OSError, match="storage space"):
        recording.EncodedRecorder(profile(), SimpleNamespace(ffmpeg="ffmpeg"), media, hub)
    assert hub.stats()["subscribers"] == 0


def test_real_recording_remux_preserves_preroll_clock_and_packet_count(tmp_path, real_access_units):
    hub, media = EncodedHub(max_seconds=3), store(tmp_path)
    start = time.monotonic() - 2
    for index, unit in enumerate(real_access_units):
        hub.publish(
            ParsedAccessUnit(unit.data, unit.keyframe, unit.decoder_config),
            start + index / 5,
            epoch=hub.epoch,
        )
    recorder = recording.EncodedRecorder(
        profile(), SimpleNamespace(ffmpeg=shutil.which("ffmpeg")), media, hub
    )
    try:
        result = recorder.stop()
        assert result and recorder.frames == 11
        report = json.loads(
            subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_packets",
                    "-show_streams",
                    "-of",
                    "json",
                    str(recorder.path),
                ],
                timeout=10,
            )
        )
        stream = report["streams"][0]
        assert stream["codec_name"] == "h264"
        packet_count = sum(item.get("codec_type") == "video" for item in report["packets"])
        assert stream.get("nb_frames", str(packet_count)) == "11"
        assert packet_count == 11
        stamps = [float(item["pts_time"]) for item in report["packets"]]
        assert stamps[-1] - stamps[0] == pytest.approx(2, abs=0.002)
        assert result["duration"] == pytest.approx(2 + 1 / 30, abs=0.002)
        assert not recorder.partial.exists()
    finally:
        hub.close()
