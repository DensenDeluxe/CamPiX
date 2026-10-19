import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from campix.audio import audio_command, finalize_with_audio, mp4_handler_types, wav_bytes
from campix.capture import CaptureError
from campix.models import CameraConfig

from .conftest import signin
from .test_service import wait_packet_ready


def test_audio_command_keeps_each_device_in_one_argument():
    directshow = audio_command("ffmpeg", "dshow", "", "Room Microphone")
    assert directshow[directshow.index("-i") + 1] == "audio=Room Microphone"
    assert ":" not in directshow[directshow.index("-i") + 1].removeprefix("audio=")
    alsa = audio_command("ffmpeg", "v4l2", "/dev/video0", "hw:1,0")
    assert alsa[alsa.index("-i") + 1] == "hw:1,0"
    synthetic = audio_command("ffmpeg", "synthetic", "0", "")
    assert "sine=frequency=440:sample_rate=16000" in synthetic
    assert "-f" in synthetic and "lavfi" in synthetic
    network = audio_command("ffmpeg", "network", "rtsp://camera.example/stream", "")
    assert network[network.index("-i") + 1] == "rtsp://camera.example/stream"
    assert "-vn" in network and "0:a:0" in network
    with pytest.raises(CaptureError):
        audio_command("ffmpeg", "legacy", "/tmp/preview.jpg", "")


def test_wav_and_rejected_profiles_do_not_start_a_stream():
    payload = wav_bytes(b"\x00\x00" * 160)
    assert payload.startswith(b"RIFF") and b"WAVE" in payload
    profile = CameraConfig(id="cam", name="Cam", backend="synthetic", audio_enabled=False)
    assert profile.audio_enabled is False
    assert profile.audio_source == ""


def test_audio_mux_failure_does_not_index_the_recording(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"not a movie")
    pcm = tmp_path / "audio.pcm"
    pcm.write_bytes(b"\x00\x01")
    registered = []

    class Store:
        def register(self, *args):
            registered.append(args)

    with pytest.raises(RuntimeError, match="no samples"):
        finalize_with_audio(
            "ffmpeg",
            Store(),
            CameraConfig(id="cam", name="Cam", backend="synthetic"),
            video,
            "media",
            pcm,
            "manual",
            1,
            5,
        )
    assert registered == []
    assert video.is_file()
    assert pcm.is_file()


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg and FFprobe required",
)
def test_enabled_audio_is_a_separate_stream_and_disabled_audio_is_absent(client, application):
    signin(client)
    assert application.state.cameras.get("demo").audio is None
    assert client.get("/api/v1/cameras/demo/audio.wav").status_code == 409
    response = client.patch("/api/v1/cameras/demo", json={"audio_enabled": True})
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 20
    info = {}
    while time.monotonic() < deadline:
        info = next(item for item in client.get("/api/v1/cameras").json() if item["id"] == "demo")
        if info["audio_status"] == "online" and info["encoding"]["packet_ready"]:
            break
        time.sleep(0.05)
    else:
        pytest.fail(json.dumps(info))
    audio = client.get("/api/v1/cameras/demo/audio.wav")
    assert audio.status_code == 200, audio.text
    assert audio.headers["content-type"].startswith("audio/wav")
    assert audio.content.startswith(b"RIFF")
    assert int(audio.headers["x-audio-sequence"]) >= 1
    wait_packet_ready(client)
    started = client.post("/api/v1/cameras/demo/recording", json={"duration": 1})
    assert started.status_code == 200, started.text
    finished = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        rows = client.get("/api/v1/media?kind=video").json()
        if rows["total"]:
            finished = rows["items"][0]
            break
        time.sleep(0.1)
    assert finished, "Audio recording was not indexed after finalization"
    path = Path(application.state.store.path(finished["filename"]))
    probed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    streams = json.loads(probed.stdout)["streams"]
    codecs = {stream["codec_type"]: stream["codec_name"] for stream in streams}
    assert codecs["video"] == "h264"
    assert codecs["audio"] == "aac"
    assert {b"vide", b"soun"} <= mp4_handler_types(path)
    assert not list(path.parent.glob("*.partial"))
    assert client.patch("/api/v1/cameras/demo", json={"audio_enabled": False}).status_code == 200
    again = next(item for item in client.get("/api/v1/cameras").json() if item["id"] == "demo")
    assert again["audio_enabled"] is False
    assert again["audio_status"] == "off"
