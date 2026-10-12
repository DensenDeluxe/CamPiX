"""Native PES timestamps survive pipe fragmentation, bursts and slow sources."""

import shutil
import subprocess
from dataclasses import replace

import pytest

from campix import tsdemux
from campix.encoded import AnnexBParser, EncodedAccessUnit
from campix.tsmux import TIMESTAMP_MASK, TransportStreamMuxer

from .test_encoded import packet


def transport(times=(100.0, 100.2, 100.4), *, data=None):
    muxer = TransportStreamMuxer()
    packets = []
    for index, at in enumerate(times):
        parsed = packet(True)
        unit = EncodedAccessUnit(data or parsed.data, at, index + 1, 1, True, parsed.decoder_config)
        packets.append(muxer.mux(unit))
    return packets


def read(data, *, chunk_size=65536):
    demuxer = tsdemux.NativeTransportStreamDemuxer()
    result = []
    for offset in range(0, len(data), chunk_size):
        result.extend(demuxer.feed(data[offset : offset + chunk_size]))
    return result + demuxer.finish()


@pytest.mark.parametrize("chunk_size", [1, 2, 187, 188, 189, 65536])
def test_fragmented_native_pipe_preserves_source_clock_and_payload(chunk_size):
    units = read(b"".join(transport((100.0, 100.2, 100.7, 101.1))), chunk_size=chunk_size)
    assert [unit.timestamp_us for unit in units] == [0, 200000, 700000, 1100000]
    assert all(unit.data == packet(True).data and unit.keyframe for unit in units)


def test_burst_delivery_uses_embedded_pts_instead_of_pipe_receipt_time():
    units = read(b"".join(transport(tuple(100.0 + index / 5 for index in range(11)))))
    assert [unit.timestamp_us for unit in units] == [index * 200000 for index in range(11)]


def test_native_clock_unwraps_33_bit_pts_at_wrap_boundary():
    muxer = TransportStreamMuxer()
    parsed = packet(True)
    first = EncodedAccessUnit(parsed.data, 0.0, 1, 1, True, parsed.decoder_config)
    # Shift the initial transport clock near the wrap without advancing source time.
    muxer._last_ticks = TIMESTAMP_MASK - 1
    units = read(muxer.mux(first) + muxer.mux(replace(first, sequence=2, captured_at=1 / 90000)))
    assert [unit.timestamp_us for unit in units] == [0, 11]


def test_payload_over_64_kib_is_bounded_and_not_limited_by_pes_length():
    data = packet(True, size=70000).data
    units = read(b"".join(transport((100.0, 100.2), data=data)))
    assert [unit.data for unit in units] == [data, data]


@pytest.mark.parametrize(
    ("offset", "value", "reason"),
    [
        (0, 0, "synchronization"),
        (1, 0xC0, "synchronization"),
        (3, 0xD0, "scrambling"),
        (3, 0x00, "adaptation control"),
        (20, 0, "CRC"),
    ],
)
def test_invalid_transport_header_or_program_crc_poison_the_parser(offset, value, reason):
    data = bytearray(transport()[0])
    data[offset] = value
    demuxer = tsdemux.NativeTransportStreamDemuxer()
    with pytest.raises(tsdemux.TransportDemuxError, match=reason):
        demuxer.feed(bytes(data))
    with pytest.raises(tsdemux.TransportDemuxError, match="closed"):
        demuxer.feed(b"")


def test_video_before_verified_program_tables_and_unknown_pid_are_rejected():
    data = transport()[0]
    with pytest.raises(tsdemux.TransportDemuxError, match="program tables"):
        tsdemux.NativeTransportStreamDemuxer().feed(data[376:])
    invalid = bytearray(data)
    invalid[1:3] = b"\x40\x20"
    with pytest.raises(tsdemux.TransportDemuxError, match="unsupported PID"):
        tsdemux.NativeTransportStreamDemuxer().feed(bytes(invalid))


def test_dropped_video_packet_fails_instead_of_silently_corrupting_source():
    data = b"".join(transport(data=packet(True, size=5000).data))
    # Remove the second video TS packet, preserving complete TS packet alignment.
    invalid = data[: 3 * 188] + data[4 * 188 :]
    with pytest.raises(tsdemux.TransportDemuxError, match="continuity"):
        read(invalid)


def test_backward_native_pts_is_rejected_even_when_transport_continuity_is_valid():
    first, second, third = transport()
    invalid = bytearray(second)
    # Repeat the first PES PTS/DTS while retaining the second packet counters.
    first_video = first[376:564]
    second_video = second[376:564]
    first_payload = 5 + first_video[4]
    second_payload = 5 + second_video[4]
    invalid[376 + second_payload + 9 : 376 + second_payload + 19] = first_video[
        first_payload + 9 : first_payload + 19
    ]
    with pytest.raises(tsdemux.TransportDemuxError, match="backwards or repeated"):
        read(first + bytes(invalid) + third)


def test_native_h264_byte_limit_is_enforced_before_unbounded_pes_allocation(monkeypatch):
    monkeypatch.setattr(tsdemux, "MAX_ACCESS_UNIT_BYTES", 128)
    with pytest.raises(tsdemux.TransportDemuxError, match="byte limit"):
        read(b"".join(transport(data=packet(True, size=500).data)))


def test_input_chunk_bound_and_truncated_final_packet_are_observable():
    demuxer = tsdemux.NativeTransportStreamDemuxer()
    with pytest.raises(tsdemux.TransportDemuxError, match="chunk exceeds"):
        demuxer.feed(b"x" * 65537)
    demuxer = tsdemux.NativeTransportStreamDemuxer()
    demuxer.feed(transport()[0][:-1])
    with pytest.raises(tsdemux.TransportDemuxError, match="within a TS packet"):
        demuxer.finish()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_actual_ffmpeg_native_copy_transport_preserves_variable_pts_and_decodes(tmp_path):
    h264, native_ts = tmp_path / "test-only.h264", tmp_path / "test-only-native.ts"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
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
            str(h264),
        ],
        capture_output=True,
        check=True,
        timeout=10,
    )
    parser = AnnexBParser()
    parsed = parser.feed(h264.read_bytes()) + parser.finish()
    muxer = TransportStreamMuxer()
    source_ts = tmp_path / "test-only-source.ts"
    source_ts.write_bytes(
        b"".join(
            muxer.mux(
                EncodedAccessUnit(
                    item.data, 100.0 + index / 5, index + 1, 1, item.keyframe, item.decoder_config
                )
            )
            for index, item in enumerate(parsed)
        )
    )
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-f",
            "mpegts",
            "-i",
            str(source_ts),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
            "-bsf:v",
            "h264_metadata=aud=insert",
            "-fps_mode",
            "passthrough",
            "-mpegts_start_pid",
            "256",
            "-mpegts_pmt_start_pid",
            "4096",
            "-flush_packets",
            "1",
            "-f",
            "mpegts",
            str(native_ts),
        ],
        capture_output=True,
        check=True,
        timeout=10,
    )
    units = read(native_ts.read_bytes(), chunk_size=187)
    assert len(units) == 11
    assert [unit.timestamp_us for unit in units] == [index * 200000 for index in range(11)]
    output = tmp_path / "test-only-demuxed.h264"
    output.write_bytes(b"".join(unit.data for unit in units))
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(output), "-f", "framemd5", "-"],
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert not result.stderr
    assert len([line for line in result.stdout.splitlines() if not line.startswith(b"#")]) == 11
