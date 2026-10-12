"""MPEG-TS framing and real remux regressions for source-clock recordings."""

import json
import shutil
import subprocess
from dataclasses import replace

import pytest

from campix import tsmux
from campix.encoded import AnnexBParser, EncodedAccessUnit

DATA = b"\x00\x00\x00\x01\x09\xf0\x00\x00\x00\x01\x65\xb8\x80"


def unit(*, data=DATA, captured_at=100.0, sequence=1, epoch=1, keyframe=True, config=b"config"):
    return EncodedAccessUnit(data, captured_at, sequence, epoch, keyframe, config)


def packets(data):
    assert len(data) % 188 == 0
    result = [data[offset : offset + 188] for offset in range(0, len(data), 188)]
    assert all(packet[0] == 0x47 for packet in result)
    return result


def pid(packet):
    return ((packet[1] & 31) << 8) | packet[2]


def payload(packet):
    mode = (packet[3] >> 4) & 3
    assert mode in (1, 3)
    return packet[4 if mode == 1 else 5 + packet[4] :]


def read_timestamp(data):
    assert data[0] & 1 and data[2] & 1 and data[4] & 1
    return (
        (((data[0] >> 1) & 7) << 30)
        | (data[1] << 22)
        | ((data[2] >> 1) << 15)
        | (data[3] << 7)
        | (data[4] >> 1)
    )


def video_header(data):
    first = next(packet for packet in packets(data) if pid(packet) == 256)
    body = payload(first)
    assert first[1] & 0x40
    assert body.startswith(b"\x00\x00\x01\xe0\x00\x00\x80\xc0\x0a")
    pts, dts = read_timestamp(body[9:14]), read_timestamp(body[14:19])
    assert pts == dts
    pcr = first[6:12]
    base = (pcr[0] << 25) | (pcr[1] << 17) | (pcr[2] << 9) | (pcr[3] << 1) | (pcr[4] >> 7)
    assert base == pts and pcr[4] & 0x7E == 0x7E and pcr[5] == 0
    return pts


def test_crc32_mpeg_matches_standard_check_vector_and_tables():
    assert tsmux._crc32_mpeg(b"123456789") == 0x0376E6E7
    stream = tsmux.TransportStreamMuxer().mux(unit())
    pat, pmt = packets(stream)[:2]
    assert pid(pat) == 0 and pid(pmt) == 4096
    for packet, table_id in ((pat, 0), (pmt, 2)):
        body = payload(packet)
        assert body[0] == 0 and body[1] == table_id
        length = ((body[2] & 15) << 8) | body[3]
        section = body[1 : 4 + length]
        assert tsmux._crc32_mpeg(section) == 0
    assert payload(pat)[9:13] == b"\x00\x01\xf0\x00"
    assert payload(pmt)[9:18] == b"\xe1\x00\xf0\x00\x1b\xe1\x00\xf0\x00"


@pytest.mark.parametrize("size", [1, 157, 158, 340, 341, 342, 5000, 70000])
def test_packetization_preserves_access_unit_bytes_and_wraps_continuity(size):
    data = b"\x00\x00\x01" + bytes(index % 251 for index in range(size))
    stream = tsmux.TransportStreamMuxer().mux(unit(data=data))
    video = [packet for packet in packets(stream) if pid(packet) == 256]
    assert [packet[3] & 15 for packet in video] == [index % 16 for index in range(len(video))]
    assert video[0][5] & 0x50 == 0x50  # PCR and random-access indicator.
    assert all(not packet[1] & 0x40 for packet in video[1:])
    assert b"".join(payload(packet) for packet in video)[19:] == data


def test_real_capture_time_survives_pre_roll_burst_delivery_and_variable_fps():
    muxer = tsmux.TransportStreamMuxer()
    times = (100.0, 100.2, 100.4, 101.1, 101.15)
    ticks = [
        video_header(muxer.mux(unit(captured_at=at, sequence=index + 1, keyframe=index == 0)))
        for index, at in enumerate(times)
    ]
    assert ticks == [90000, 108000, 126000, 189000, 193500]


def test_sub_tick_ties_are_advanced_without_collapsing_longer_time_gaps():
    muxer = tsmux.TransportStreamMuxer()
    ticks = [
        video_header(muxer.mux(unit(captured_at=at, sequence=index + 1)))
        for index, at in enumerate((100.0, 100.0, 100.000001, 100.1))
    ]
    assert ticks == [90000, 90001, 90002, 99000]


def test_presentation_and_pcr_clocks_wrap_at_33_bits():
    muxer = tsmux.TransportStreamMuxer()
    first = video_header(muxer.mux(unit(captured_at=0.0)))
    second = video_header(muxer.mux(unit(captured_at=(1 << 33) / 90000 + 0.2, sequence=2)))
    assert first == 90000 and second == 108000


def test_program_tables_repeat_at_idr_and_half_second_with_independent_counters():
    muxer = tsmux.TransportStreamMuxer()
    streams = [
        muxer.mux(unit(captured_at=at, sequence=index + 1, keyframe=key))
        for index, (at, key) in enumerate(
            ((100.0, True), (100.1, False), (100.5, False), (100.6, True))
        )
    ]
    assert [[pid(packet) for packet in packets(stream)] for stream in streams] == [
        [0, 4096, 256],
        [256],
        [0, 4096, 256],
        [0, 4096, 256],
    ]
    pats = [packet for stream in streams for packet in packets(stream) if pid(packet) == 0]
    assert [packet[3] & 15 for packet in pats] == [0, 1, 2]
    video = [packet for stream in streams for packet in packets(stream) if pid(packet) == 256]
    assert [packet[3] & 15 for packet in video] == [0, 1, 2, 3]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0, True, 10**400])
def test_invalid_capture_clock_is_rejected_before_stream_state_changes(value):
    muxer = tsmux.TransportStreamMuxer()
    with pytest.raises(tsmux.TransportStreamError, match="finite and nonnegative"):
        muxer.mux(unit(captured_at=value))
    assert video_header(muxer.mux(unit())) == 90000


def test_finite_but_unrepresentable_clock_gap_does_not_corrupt_session():
    muxer = tsmux.TransportStreamMuxer()
    muxer.mux(unit(captured_at=0.0))
    with pytest.raises(tsmux.TransportStreamError, match="clock bounds"):
        muxer.mux(unit(captured_at=1e308, sequence=2))
    assert video_header(muxer.mux(unit(captured_at=0.2, sequence=2))) == 108000


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"epoch": 2}, "epoch changed"),
        ({"captured_at": 99.0}, "clock moved backwards"),
        ({"sequence": 1}, "increasing sequence"),
        ({"decoder_config": b"different"}, "configuration changed"),
    ],
)
def test_restart_clock_replay_and_configuration_changes_abort_session(change, reason):
    muxer = tsmux.TransportStreamMuxer()
    muxer.mux(unit())
    with pytest.raises(tsmux.TransportStreamError, match=reason):
        muxer.mux(replace(unit(captured_at=100.2, sequence=2), **change))
    assert video_header(muxer.mux(unit(captured_at=100.2, sequence=2))) == 108000


def test_stream_requires_initial_idr_and_bounded_annex_b_data(monkeypatch):
    muxer = tsmux.TransportStreamMuxer()
    with pytest.raises(tsmux.TransportStreamError, match="IDR"):
        muxer.mux(unit(keyframe=False))
    for data in (b"", b"arbitrary output"):
        with pytest.raises(tsmux.TransportStreamError, match="bounded Annex B"):
            muxer.mux(unit(data=data))
    monkeypatch.setattr(tsmux, "MAX_ACCESS_UNIT_BYTES", 128)
    with pytest.raises(tsmux.TransportStreamError, match="bounded Annex B"):
        muxer.mux(unit(data=b"\x00\x00\x01" + b"x" * 128))
    assert len(muxer.mux(unit())) == 188 * 3


def test_unsupported_decoder_configuration_and_epoch_are_rejected():
    for packet in (unit(config=b""), unit(epoch=0), unit(sequence=0)):
        with pytest.raises(tsmux.TransportStreamError):
            tsmux.TransportStreamMuxer().mux(packet)
    with pytest.raises(ValueError, match="positive integer"):
        tsmux.TransportStreamMuxer(epoch=0)
    with pytest.raises(tsmux.TransportStreamError, match="epoch changed"):
        tsmux.TransportStreamMuxer(epoch=2).mux(unit())


@pytest.fixture
def real_access_units(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and FFprobe required")
    source = tmp_path / "test-only.h264"
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x48:r=30",
            "-frames:v",
            "11",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-profile:v",
            "baseline",
            "-pix_fmt",
            "yuv420p",
            "-bf",
            "0",
            "-g",
            "5",
            "-bsf:v",
            "h264_metadata=aud=insert",
            "-f",
            "h264",
            str(source),
        ],
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert not result.stderr
    parser = AnnexBParser()
    parsed = parser.feed(source.read_bytes()) + parser.finish()
    assert len(parsed) == 11 and parsed[0].keyframe
    return [
        EncodedAccessUnit(
            packet.data, 100.0 + index / 5, index + 1, 1, packet.keyframe, packet.decoder_config
        )
        for index, packet in enumerate(parsed)
    ]


def probe(path, *options):
    return json.loads(
        subprocess.check_output(
            ["ffprobe", "-v", "error", *options, "-of", "json", str(path)],
            timeout=10,
        )
    )


def test_real_ffmpeg_copy_remux_preserves_five_fps_clock_from_thirty_fps_encoder(
    tmp_path, real_access_units
):
    transport = tmp_path / "test-only.ts"
    target = tmp_path / "test-only.mp4"
    muxer = tsmux.TransportStreamMuxer()
    # Deliver all retained packets immediately: frame-count-based timestamps
    # would compress the two-second capture into roughly one third of a second.
    transport.write_bytes(b"".join(muxer.mux(packet) for packet in real_access_units))
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "mpegts",
            "-i",
            str(transport),
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-an",
            "-movflags",
            "+faststart",
            str(target),
        ],
        capture_output=True,
        timeout=10,
        check=True,
    )
    report = probe(target, "-show_packets", "-show_streams", "-show_format", "-count_frames")
    stream = report["streams"][0]
    times = [float(packet["pts_time"]) for packet in report["packets"]]
    assert stream["codec_name"] == "h264" and stream["nb_read_frames"] == "11"
    assert (stream["width"], stream["height"]) == (64, 48)
    assert times == pytest.approx([index / 5 for index in range(11)], abs=1 / 90000)
    assert 2.0 <= float(report["format"]["duration"]) <= 2.21
    assert report["packets"][0]["flags"].startswith("K")
    decoded = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(target), "-f", "framemd5", "-"],
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert not decoded.stderr
    assert len([line for line in decoded.stdout.splitlines() if not line.startswith(b"#")]) == 11


def test_real_keyframe_join_contains_configuration_and_decodes_without_previous_gop(
    tmp_path, real_access_units
):
    retained = real_access_units[5:]
    assert retained[0].keyframe
    path = tmp_path / "test-only-preroll.ts"
    muxer = tsmux.TransportStreamMuxer()
    path.write_bytes(b"".join(muxer.mux(packet) for packet in retained))
    report = probe(path, "-show_packets", "-show_streams", "-count_frames")
    assert report["streams"][0]["nb_read_frames"] == "6"
    assert report["packets"][0]["flags"].startswith("K")
    times = [float(packet["pts_time"]) for packet in report["packets"]]
    assert times[-1] - times[0] == pytest.approx(1.0)
