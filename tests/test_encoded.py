import dataclasses
import shutil
import subprocess
import threading
import time

import pytest

from campix.encoded import (
    MAX_ACCESS_UNIT_BYTES,
    MAX_CONFIG_BYTES,
    MAX_SUBSCRIBERS,
    AnnexBParser,
    EncodedFormatError,
    EncodedHub,
    EncodedOverrun,
    EncodedRestart,
    EncodedStreamEnded,
    ParsedAccessUnit,
    h264_dimensions,
)

START = b"\x00\x00\x00\x01"
AUD = START + b"\x09\xf0"


def rbsp(*values):
    bits = ""
    for value in values:
        encoded = f"{value + 1:b}"
        bits += "0" * (len(encoded) - 1) + encoded
    bits += "1"
    bits += "0" * (-len(bits) % 8)
    return bytes(int(bits[index : index + 8], 2) for index in range(0, len(bits), 8))


def sps(identifier=0):
    return START + b"\x67\x42\x00\x1e" + rbsp(identifier)


def pps(identifier=0, sps_id=0):
    return START + b"\x68" + rbsp(identifier, sps_id)


def picture(keyframe=False, *, pps_id=0, slice_type=None, payload=b""):
    slice_type = (2 if keyframe else 0) if slice_type is None else slice_type
    return START + (b"\x65" if keyframe else b"\x41") + rbsp(0, slice_type, pps_id) + payload


CONFIG = sps() + pps()


def packet(keyframe=False, size=40):
    data = AUD + (CONFIG if keyframe else b"") + picture(keyframe, payload=b"x" * size)
    return ParsedAccessUnit(data, keyframe, CONFIG)


def publish(hub, at, keyframe=False, size=40):
    return hub.publish(packet(keyframe, size), at, epoch=hub.epoch)


def drain(subscription):
    result = []
    while (unit := subscription.read(timeout=0)) is not None:
        result.append(unit)
    return result


def test_annexb_accepts_every_boundary_and_repeats_cached_headers_on_idr():
    stream = AUD + CONFIG + picture(True) + AUD + picture() + AUD + picture(True)
    expected = None
    for split in range(len(stream) + 1):
        parser = AnnexBParser()
        units = parser.feed(stream[:split]) + parser.feed(stream[split:]) + parser.finish()
        assert [unit.keyframe for unit in units] == [True, False, True]
        assert all(unit.decoder_config == CONFIG for unit in units)
        assert units[0].data == units[2].data == AUD + CONFIG + picture(True)
        assert units[1].data == AUD + picture()
        expected = units if expected is None else expected
        assert units == expected
    parser = AnnexBParser()
    one_byte_units = []
    for value in stream:
        one_byte_units.extend(parser.feed(bytes((value,))))
    assert one_byte_units + parser.finish() == expected


def test_trusted_frame_boundary_emits_final_slice_and_retains_decoder_config():
    parser = AnnexBParser()
    packets = [AUD + CONFIG + picture(True), AUD + picture(), AUD + picture(True)]
    for index, framed in enumerate(packets):
        for value in framed:
            assert parser.feed(bytes((value,))) == []
        units = parser.end_access_unit()
        assert len(units) == 1
        assert units[0].keyframe == (index != 1)
        assert units[0].decoder_config == CONFIG
        assert units[0].data == AUD + (CONFIG if index != 1 else b"") + picture(index != 1)
    assert parser.finish() == []
    with pytest.raises(EncodedFormatError, match="closed"):
        parser.end_access_unit()


def test_trusted_frame_boundary_preserves_strict_delimiter_and_header_validation():
    parser = AnnexBParser()
    parser.feed(AUD + CONFIG + picture(True))
    assert len(parser.end_access_unit()) == 1
    parser.feed(picture())
    with pytest.raises(EncodedFormatError, match="delimiters"):
        parser.end_access_unit()
    with pytest.raises(EncodedFormatError, match="closed"):
        parser.feed(AUD + picture())
    parser = AnnexBParser()
    parser.feed(b"\x00\x00")
    with pytest.raises(EncodedFormatError, match="Incomplete"):
        parser.end_access_unit()


def test_annexb_accepts_mixed_start_lengths_without_changing_config():
    short = b"\x00\x00\x01"
    parser = AnnexBParser()
    stream = AUD + CONFIG + picture(True) + AUD + CONFIG.replace(START, short) + picture(True)
    units = parser.feed(stream) + parser.finish()
    assert len(units) == 2
    assert units[0].decoder_config == units[1].decoder_config == CONFIG
    assert units[0].data == units[1].data


def test_annexb_selects_parameter_sets_referenced_by_the_slice():
    config = sps(1) + pps(2, 1)
    parser = AnnexBParser()
    stream = AUD + CONFIG + config + picture(True, pps_id=2) + AUD + picture(True, pps_id=0)
    units = parser.feed(stream) + parser.finish()
    assert units[0].decoder_config == config
    assert units[1].decoder_config == CONFIG
    assert units[1].data == AUD + CONFIG + picture(True)


@pytest.mark.parametrize(
    ("stream", "message"),
    [
        (b"garbage", "Annex B"),
        (START + START, "Empty or malformed"),
        (AUD + CONFIG + START + b"\xe5\x80", "Forbidden"),
        (AUD + START + b"\x78\x80", "Unsupported"),
        (CONFIG + picture(True), "delimiters"),
        (AUD + picture(True), "unavailable picture"),
        (AUD + pps() + picture(True), "unavailable sequence"),
        (AUD + CONFIG + picture(slice_type=1), "B slices"),
        (AUD + CONFIG + picture(slice_type=6), "B slices"),
        (AUD + CONFIG + picture(slice_type=10), "Invalid H.264 slice"),
        (AUD + CONFIG + picture(pps_id=256), "Invalid H.264 slice"),
        (AUD + CONFIG + START + b"\x41\x00", "Truncated"),
        (AUD + CONFIG + picture(True) + picture(), "mixes IDR"),
        (AUD + CONFIG + picture(True) + sps(1), "parameter sets must precede"),
        (AUD + START + b"\x67\x42", "Truncated"),
        (AUD + sps(32), "sequence parameter set identifier"),
        (AUD + pps(256), "picture parameter set identifier"),
        (AUD + START + b"\x68\x00\x00\x03\x04", "emulation-prevention"),
        (AUD + START + b"\x68" + b"\x00" * 8, "exponential-Golomb"),
        (START + b"\x09", "delimiter"),
        (b"\x00\x00", "Incomplete"),
    ],
)
def test_annexb_rejects_malformed_or_unusable_streams(stream, message):
    parser = AnnexBParser()
    with pytest.raises(EncodedFormatError, match=message):
        parser.feed(stream)
        parser.finish()
    with pytest.raises(EncodedFormatError, match="closed|failure"):
        parser.feed(AUD + CONFIG + picture(True))


def test_annexb_rejects_picture_parameter_changes_inside_access_unit():
    parser = AnnexBParser()
    stream = AUD + CONFIG + pps(1) + picture(True) + picture(True, pps_id=1)
    with pytest.raises(EncodedFormatError, match="mixes picture"):
        parser.feed(stream)
        parser.finish()


def test_annexb_bounds_chunk_nal_access_unit_config_and_result_memory():
    with pytest.raises(EncodedFormatError, match="chunk"):
        AnnexBParser(max_chunk_bytes=32).feed(b"x" * 33)
    parser = AnnexBParser(max_nal_bytes=32)
    with pytest.raises(EncodedFormatError, match="NAL"):
        for _ in range(20):
            parser.feed(START if not parser._buffer else b"x" * 8)
    parser = AnnexBParser(max_access_unit_bytes=64)
    with pytest.raises(EncodedFormatError, match="access unit"):
        parser.feed(AUD + CONFIG + picture(True, payload=b"x" * 64) + AUD)
        parser.finish()
    parser = AnnexBParser(max_units_per_feed=1)
    with pytest.raises(EncodedFormatError, match="too many access units"):
        parser.feed(AUD + CONFIG + picture(True) + (AUD + picture()) * 3 + AUD)
    parser = AnnexBParser()
    with pytest.raises(EncodedFormatError, match="decoder configuration"):
        parser.feed(AUD + sps() + pps() + sps(1) + b"x" * MAX_CONFIG_BYTES + AUD)
        parser.finish()


def test_annexb_does_not_scan_or_copy_entire_slice_header():
    parser = AnnexBParser()
    # Invalid emulation bytes far into the coded slice are the decoder's job;
    # parser work is limited to the initial slice header and packet bounds.
    stream = AUD + CONFIG + picture(True, payload=b"x" * 512 + b"\x00\x00\x03\x04")
    assert len(parser.feed(stream) + parser.finish()) == 1


def test_packets_are_immutable_and_shared_between_independent_consumers():
    hub = EncodedHub()
    live, recording = hub.subscribe(), hub.subscribe("recording")
    unit = publish(hub, 1, True)
    assert live.read(0) is recording.read(0) is unit
    with pytest.raises(dataclasses.FrozenInstanceError):
        unit.data = b"changed"
    assert live.read(0) is None
    assert recording.read(0) is None
    live.close()
    recording.close()
    assert hub.stats()["subscribers"] == 0


def test_pre_roll_starts_at_preceding_idr_and_live_starts_latest_gop():
    hub = EncodedHub()
    for index in range(8):
        publish(hub, index, index in (0, 3, 6))
    recording, live = hub.subscribe("recording", pre_roll_seconds=3), hub.subscribe()
    assert [unit.captured_at for unit in drain(recording)] == [3, 4, 5, 6, 7]
    assert [unit.captured_at for unit in drain(live)] == [6, 7]
    assert hub.stats()["buffered_frames"] == 8


@pytest.mark.parametrize("limits", [{"max_frames": 3}, {"max_bytes": 240}, {"max_seconds": 2}])
def test_pre_roll_is_bounded_and_never_retains_orphaned_p_frames(limits):
    hub = EncodedHub(**limits)
    for index in range(10):
        publish(hub, index, index in (0, 5))
        stats = hub.stats()
        assert stats["buffered_frames"] <= hub.max_frames
        assert stats["buffered_bytes"] <= hub.max_bytes
        if hub._ring:
            assert hub._ring[0].keyframe
            assert hub._ring[-1].captured_at - hub._ring[0].captured_at <= hub.max_seconds
    assert not hub._ring


def test_live_overrun_discards_dependencies_then_rejoins_at_configured_idr():
    hub = EncodedHub()
    slow, fast = hub.subscribe(max_frames=2), hub.subscribe()
    first = publish(hub, 0, True)
    assert fast.read(0) is first
    for index in range(1, 6):
        unit = publish(hub, index, index == 4)
        assert fast.read(0) is unit
    received = drain(slow)
    assert [unit.captured_at for unit in received] == [4, 5]
    assert received[0].keyframe and CONFIG in received[0].data
    assert slow.dropped_frames == 4
    assert fast.dropped_frames == 0


def test_recording_overrun_fails_instead_of_delivering_incomplete_success():
    hub = EncodedHub()
    recording = hub.subscribe("recording", max_frames=2)
    for index in range(3):
        publish(hub, index, index == 0)
    with pytest.raises(EncodedOverrun, match="retain partial"):
        recording.read(0)
    assert recording.dropped_frames == 0
    # Another consumer is independent of the failed writer.
    assert len(drain(hub.subscribe())) == 3


def test_finish_detaches_recording_atomically_and_drains_without_post_stop_overrun():
    hub = EncodedHub()
    subscription = hub.subscribe("recording", max_frames=2)
    first, second = publish(hub, 0, True), publish(hub, 1)
    assert subscription.finish() == second.sequence
    assert hub.stats()["subscribers"] == 0
    for index in range(2, 10):
        publish(hub, index, index == 5)
    assert subscription.read(0) is first
    assert subscription.read(0) is second
    assert subscription.read() is None
    subscription.close()


def test_finish_never_clears_an_existing_recording_overrun():
    hub = EncodedHub()
    subscription = hub.subscribe("recording", max_frames=1)
    publish(hub, 0, True)
    publish(hub, 1)
    subscription.finish()
    with pytest.raises(EncodedOverrun):
        subscription.read(0)


def test_pre_roll_that_exceeds_recording_capacity_fails_before_first_read():
    hub = EncodedHub()
    for index in range(3):
        publish(hub, index, index == 0)
    subscription = hub.subscribe("recording", pre_roll_seconds=3, max_frames=2)
    with pytest.raises(EncodedOverrun):
        subscription.read(0)


def test_live_packet_larger_than_capacity_never_escapes_its_byte_bound():
    hub = EncodedHub()
    subscription = hub.subscribe(max_bytes=64)
    publish(hub, 0, True, size=128)
    assert subscription.read(0) is None
    publish(hub, 1)
    assert subscription.read(0) is None
    small = publish(hub, 2, True, size=0)
    assert subscription.read(0) is small
    assert subscription.dropped_frames == 2


def test_recording_waits_for_initial_idr_without_treating_prefix_as_data_loss():
    hub = EncodedHub()
    subscription = hub.subscribe("recording")
    publish(hub, 0)
    assert subscription.read(0) is None
    keyframe = publish(hub, 1, True)
    assert subscription.read(0) is keyframe
    assert hub.stats()["buffered_frames"] == 1
    assert subscription.dropped_frames == 0


def test_recording_without_pre_roll_starts_at_next_idr_after_request():
    hub = EncodedHub()
    publish(hub, 1, True)
    publish(hub, 2)
    subscription = hub.subscribe("recording", pre_roll_seconds=0)
    assert subscription.read(0) is None
    publish(hub, 3)
    assert subscription.read(0) is None
    unit = publish(hub, 4, True)
    assert subscription.read(0) is unit
    assert subscription.dropped_frames == 0


def test_readiness_fps_and_pre_roll_duration_describe_current_encoded_epoch():
    hub = EncodedHub(max_seconds=0)
    assert not hub.stats()["ready"]
    assert hub.stats()["last_frame_age_seconds"] is None
    started = time.monotonic()
    publish(hub, started)
    assert not hub.stats()["ready"]
    for index in range(1, 150):
        publish(hub, started + index / 60, index % 60 == 0)
    stats = hub.stats()
    assert stats["ready"]
    assert stats["buffered_frames"] == 0
    assert stats["fps"] == pytest.approx(60, rel=0.001)
    assert stats["last_sequence"] == stats["published_frames"] == 150
    assert stats["last_timestamp"] == started + 149 / 60
    assert stats["last_frame_age_seconds"] == 0
    assert len(hub._timestamps) == 120
    hub.restart()
    assert not hub.stats()["ready"]
    assert hub.stats()["fps"] == 0
    assert hub.stats()["last_timestamp"] is None
    assert hub.stats()["last_frame_age_seconds"] is None
    # The sequence remains monotonic across restarts, while epoch changes.
    assert hub.stats()["last_sequence"] == 150
    hub = EncodedHub()
    publish(hub, 0, True)
    publish(hub, 1)
    assert hub.stats()["buffered_duration_seconds"] == 1
    hub.close()
    assert not hub.stats()["ready"]


def test_concurrent_lossless_consumers_receive_identical_ordered_objects():
    hub = EncodedHub()
    subscriptions = [hub.subscribe("recording") for _ in range(3)]
    received = [[] for _ in subscriptions]
    barrier = threading.Barrier(4)

    def reader(index):
        barrier.wait(timeout=1)
        for _ in range(500):
            unit = subscriptions[index].read(timeout=2)
            if unit is None:
                break
            received[index].append(unit)

    threads = [threading.Thread(target=reader, args=(index,)) for index in range(3)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=1)
    units = [publish(hub, index / 60, index % 60 == 0) for index in range(500)]
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    for index, subscription in enumerate(subscriptions):
        assert len(received[index]) == len(units)
        assert all(
            original is consumed for original, consumed in zip(units, received[index], strict=True)
        )
        assert subscription.dropped_frames == 0
        subscription.close()
    assert hub.stats()["subscribers"] == 0


def test_restart_invalidates_consumers_pre_roll_and_stale_producer_callbacks():
    hub = EncodedHub()
    old_epoch = hub.epoch
    old = hub.subscribe("recording")
    publish(hub, 100, True)
    assert hub.restart() == old_epoch + 1
    with pytest.raises(EncodedRestart, match="restarted"):
        old.read(0)
    with pytest.raises(EncodedRestart, match="Stale"):
        hub.publish(packet(True), 101, epoch=old_epoch)
    fresh = hub.subscribe()
    assert fresh.read(0) is None
    unit = publish(hub, 1, True)
    assert fresh.read(0) is unit
    assert unit.epoch == old_epoch + 1
    assert hub.stats()["buffered_frames"] == 1


def test_config_change_cannot_silently_mix_one_recording_generation():
    hub = EncodedHub()
    publish(hub, 1, True)
    with pytest.raises(EncodedFormatError, match="configuration changed"):
        hub.publish(ParsedAccessUnit(b"new", True, sps(1) + pps(1, 1)), 2, epoch=hub.epoch)
    assert hub.stats()["published_frames"] == 1


def test_close_wakes_waiters_and_external_cancellation_is_bounded():
    hub = EncodedHub()
    subscription = hub.subscribe()
    results = []
    waiting = threading.Event()

    def reader():
        waiting.set()
        results.append(subscription.read())

    thread = threading.Thread(target=reader)
    thread.start()
    assert waiting.wait(1)
    subscription.close()
    thread.join(1)
    assert not thread.is_alive()
    assert results == [None]
    cancellation = threading.Event()
    subscription = hub.subscribe()
    timer = threading.Timer(0.01, cancellation.set)
    timer.start()
    began = time.monotonic()
    assert subscription.read(cancel_event=cancellation) is None
    assert time.monotonic() - began < 0.5
    timer.join(1)
    hub.close()
    with pytest.raises(EncodedStreamEnded):
        subscription.read(0)
    with pytest.raises(EncodedStreamEnded):
        hub.subscribe()
    with pytest.raises(EncodedStreamEnded):
        hub.publish(packet(True), epoch=hub.epoch)


def test_consumer_count_is_bounded_and_close_releases_capacity():
    hub = EncodedHub()
    subscriptions = [hub.subscribe() for _ in range(MAX_SUBSCRIBERS)]
    with pytest.raises(EncodedOverrun, match="consumer limit"):
        hub.subscribe()
    subscriptions[0].close()
    hub.subscribe()
    assert hub.stats()["subscribers"] == MAX_SUBSCRIBERS


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_invalid_packet_timestamps_are_rejected(value):
    hub = EncodedHub()
    with pytest.raises(ValueError, match="timestamp"):
        publish(hub, value, True)
    publish(hub, 2, True)
    with pytest.raises(ValueError, match="backwards"):
        publish(hub, 1)


def test_oversized_packet_and_mutable_bytes_are_rejected():
    hub = EncodedHub()
    with pytest.raises(EncodedFormatError, match="access-unit"):
        hub.publish(
            ParsedAccessUnit(b"x" * (MAX_ACCESS_UNIT_BYTES + 1), True, CONFIG), epoch=hub.epoch
        )
    with pytest.raises(TypeError, match="immutable"):
        hub.publish(ParsedAccessUnit(bytearray(b"bad"), True, CONFIG), epoch=hub.epoch)


def test_annexb_real_ffmpeg_packets_can_be_joined_and_decoded_independently():
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg unavailable: real Annex B encoder/decoder integration")
    encoded = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10",
            "-frames:v",
            "9",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-bf",
            "0",
            "-g",
            "3",
            "-bsf:v",
            "h264_metadata=aud=insert",
            "-f",
            "h264",
            "pipe:1",
        ],
        capture_output=True,
        timeout=10,
        check=True,
    ).stdout
    parser = AnnexBParser()
    units = []
    for index in range(0, len(encoded), 97):
        units.extend(parser.feed(encoded[index : index + 97]))
    units.extend(parser.finish())
    assert len(units) == 9
    assert [index for index, unit in enumerate(units) if unit.keyframe] == [0, 3, 6]
    for first in (0, 3, 6):
        decoded = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "h264",
                "-i",
                "pipe:0",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            input=b"".join(unit.data for unit in units[first:]),
            capture_output=True,
            timeout=10,
            check=True,
        ).stdout
        assert len(decoded) == (9 - first) * 160 * 90 * 3


def geometry_sps(
    *,
    width_mbs=40,
    height_units=23,
    profile=66,
    chroma=1,
    interlaced=False,
    crop=(0, 0, 0, 4),
    scaling=False,
    order_cycle=None,
):
    def ue(value):
        code = f"{value + 1:b}"
        return "0" * (len(code) - 1) + code

    bits = f"{profile:08b}" + "00000000" + "00101000" + ue(0)
    if profile == 100:
        bits += ue(chroma) + ("0" if chroma == 3 else "")
        bits += ue(0) + ue(0) + "0" + str(int(scaling))
        if scaling:
            for index in range(12 if chroma == 3 else 8):
                bits += "1" + ue(0) * (16 if index < 6 else 64)
    bits += ue(0)
    if order_cycle is None:
        bits += ue(0) + ue(0)
    else:
        bits += ue(1) + "0" + ue(0) + ue(0) + ue(order_cycle)
        bits += ue(0) * min(order_cycle, 255)
    bits += ue(1) + "0" + ue(width_mbs - 1) + ue(height_units - 1)
    bits += "00" if interlaced else "1"
    bits += "1" + str(int(crop is not None))
    if crop is not None:
        bits += "".join(ue(value) for value in crop)
    bits += "01"  # No VUI; RBSP stop bit.
    bits += "0" * (-len(bits) % 8)
    payload = bytes(int(bits[index : index + 8], 2) for index in range(0, len(bits), 8))
    escaped = bytearray()
    zeros = 0
    for value in payload:
        if zeros >= 2 and value <= 3:
            escaped.append(3)
            zeros = 0
        escaped.append(value)
        zeros = zeros + 1 if value == 0 else 0
    return START + b"\x67" + bytes(escaped)


@pytest.mark.parametrize(
    "options,expected",
    [
        ({}, (640, 360)),
        ({"profile": 77}, (640, 360)),
        ({"profile": 100, "scaling": True}, (640, 360)),
        ({"profile": 100, "chroma": 3, "scaling": True, "crop": (1, 3, 2, 6)}, (636, 360)),
        ({"profile": 100, "chroma": 0, "crop": (1, 3, 2, 6)}, (636, 360)),
        ({"height_units": 12, "interlaced": True, "crop": (0, 0, 0, 6)}, (640, 360)),
        ({"crop": None}, (640, 368)),
        ({"order_cycle": 3}, (640, 360)),
    ],
)
def test_sps_geometry_handles_profiles_crop_interlace_and_scaling_lists(options, expected):
    assert h264_dimensions(geometry_sps(**options) + pps()) == expected


@pytest.mark.parametrize(
    "config,message",
    [
        (b"bad", "Annex B"),
        (pps(), "sequence parameter set"),
        (sps(), "Truncated|Invalid"),
        (geometry_sps(profile=1), "profile"),
        (geometry_sps(profile=100, chroma=4), "chroma"),
        (geometry_sps(width_mbs=257), "allocation bounds"),
        (geometry_sps(height_units=137), "allocation bounds"),
        (geometry_sps(crop=(0, 400, 0, 0)), "display dimensions"),
        (geometry_sps(order_cycle=256), "cycle"),
        (START + b"\x67" + b"x" * MAX_CONFIG_BYTES, "byte limit"),
    ],
    ids=[
        "bad-annexb",
        "missing-sps",
        "truncated",
        "profile",
        "chroma",
        "coded-width",
        "coded-height",
        "crop",
        "cycle",
        "oversized-config",
    ],
)
def test_sps_geometry_rejects_malformed_or_unbounded_dimensions(config, message):
    with pytest.raises(EncodedFormatError, match=message):
        h264_dimensions(config)


@pytest.mark.parametrize("width,height", [(320, 240), (640, 360), (3840, 2160)])
def test_actual_ffmpeg_sps_geometry_including_cropped_and_4k_profiles(width, height):
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg unavailable: actual H.264 SPS geometry integration")
    output = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=size={width}x{height}:rate=1",
            "-frames:v",
            "1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-threads",
            "1",
            "-bsf:v",
            "h264_metadata=aud=insert",
            "-f",
            "h264",
            "pipe:1",
        ],
        capture_output=True,
        timeout=10,
        check=True,
    ).stdout
    parser = AnnexBParser()
    units = parser.feed(output) + parser.finish()
    assert len(units) == 1
    assert h264_dimensions(units[0].decoder_config) == (width, height)
