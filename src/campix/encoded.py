"""Bounded, independently consumed H.264 packets from one camera encoder.

The producer inserts access-unit delimiters and disables B frames. Packet bytes
are immutable and shared by reference; this is not a raw-frame zero-copy API.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

MAX_ACCESS_UNIT_BYTES = 16 * 1024 * 1024
MAX_CONFIG_BYTES = 128 * 1024
MAX_PRE_ROLL_BYTES = 32 * 1024 * 1024
MAX_PRE_ROLL_SECONDS = 10.0
MAX_PRE_ROLL_FRAMES = 600
MAX_SUBSCRIBERS = 32
_START_CODE = re.compile(b"\x00\x00(?:\x00)?\x01")


class EncodedFormatError(ValueError):
    """The encoder supplied malformed or unsupported H.264 packets."""


class EncodedOverrun(RuntimeError):
    """A lossless recording consumer could not keep up with its encoder."""


class EncodedRestart(RuntimeError):
    """The encoder generation changed; consumers must start a new stream."""


class EncodedStreamEnded(RuntimeError):
    """The source closed before a consumer finished."""


@dataclass(frozen=True, slots=True)
class ParsedAccessUnit:
    data: bytes
    keyframe: bool
    decoder_config: bytes


@dataclass(frozen=True, slots=True)
class EncodedAccessUnit:
    data: bytes
    captured_at: float
    sequence: int
    epoch: int
    keyframe: bool
    decoder_config: bytes


class _Bits:
    def __init__(self, escaped: bytes | memoryview, *, max_bytes: int = 128):
        self.data = bytearray()
        zeros = 0
        for index, value in enumerate(escaped):
            # Slice inspection uses a short prefix; SPS geometry opts into its
            # separately bounded configuration size, never a coded-slice copy.
            if index >= max_bytes:
                break
            if zeros >= 2 and value == 3:
                if index + 1 >= len(escaped) or escaped[index + 1] > 3:
                    raise EncodedFormatError("Invalid H.264 emulation-prevention byte")
                zeros = 0
                continue
            self.data.append(value)
            zeros = zeros + 1 if value == 0 else 0
        self.offset = 0

    def bit(self) -> int:
        if self.offset >= len(self.data) * 8:
            raise EncodedFormatError("Truncated H.264 parameter or slice header")
        result = (self.data[self.offset // 8] >> (7 - self.offset % 8)) & 1
        self.offset += 1
        return result

    def ue(self) -> int:
        zeros = 0
        while not self.bit():
            zeros += 1
            if zeros > 31:
                raise EncodedFormatError("Oversized H.264 exponential-Golomb value")
        result = 1
        for _ in range(zeros):
            result = (result << 1) | self.bit()
        return result - 1

    def read(self, count: int) -> int:
        result = 0
        for _ in range(count):
            result = (result << 1) | self.bit()
        return result

    def se(self) -> int:
        value = self.ue()
        return (value + 1) // 2 if value & 1 else -(value // 2)


def h264_dimensions(decoder_config: bytes) -> tuple[int, int]:
    """Read bounded H.264 SPS display dimensions, including crop/interlace.

    Geometry is distinct from hardware qualification or complete decoding. The
    source must still supply validated access units and a matching camera profile.
    """
    if not isinstance(decoder_config, bytes):
        raise TypeError("H.264 decoder configuration must be immutable bytes")
    if not 0 < len(decoder_config) <= MAX_CONFIG_BYTES:
        raise EncodedFormatError("H.264 decoder configuration exceeds its byte limit")
    matches = iter(_START_CODE.finditer(decoder_config))
    previous = next(matches, None)
    if previous is None or previous.start() != 0:
        raise EncodedFormatError("H.264 decoder configuration requires Annex B parameter sets")
    payload = None
    for following in matches:
        if previous.end() < following.start() and decoder_config[previous.end()] & 31 == 7:
            payload = memoryview(decoder_config)[previous.end() : following.start()]
            break
        previous = following
    if payload is None and previous.end() < len(decoder_config):
        if decoder_config[previous.end()] & 31 == 7:
            payload = memoryview(decoder_config)[previous.end() :]
    if payload is None or len(payload) < 5 or payload[0] & 0x80:
        raise EncodedFormatError("H.264 decoder configuration lacks a valid sequence parameter set")
    bits = _Bits(payload[1:], max_bytes=MAX_CONFIG_BYTES)
    profile, constraints = bits.read(8), bits.read(8)
    bits.read(8)  # level_idc does not determine the encoded dimensions.
    if constraints & 3 or bits.ue() > 31:
        raise EncodedFormatError("Invalid H.264 sequence parameter set header")
    high_profiles = {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}
    chroma_format, separate_colour_plane = 1, 0
    if profile in high_profiles:
        chroma_format = bits.ue()
        if chroma_format > 3:
            raise EncodedFormatError("Invalid H.264 SPS chroma format")
        if chroma_format == 3:
            separate_colour_plane = bits.bit()
        if bits.ue() > 6 or bits.ue() > 6:
            raise EncodedFormatError("Unsupported H.264 SPS bit depth")
        bits.bit()  # qpprime_y_zero_transform_bypass_flag
        if bits.bit():
            for index in range(12 if chroma_format == 3 else 8):
                if bits.bit():
                    last_scale = next_scale = 8
                    for _ in range(16 if index < 6 else 64):
                        if next_scale:
                            delta = bits.se()
                            if not -128 <= delta <= 127:
                                raise EncodedFormatError("Invalid H.264 SPS scaling-list delta")
                            next_scale = (last_scale + delta) % 256
                        last_scale = next_scale or last_scale
    elif profile not in {66, 77, 88}:
        raise EncodedFormatError("Unsupported H.264 SPS profile")
    if bits.ue() > 12:
        raise EncodedFormatError("Invalid H.264 SPS frame-number size")
    order_type = bits.ue()
    if order_type == 0:
        if bits.ue() > 12:
            raise EncodedFormatError("Invalid H.264 SPS picture-order size")
    elif order_type == 1:
        bits.bit()
        bits.se()
        bits.se()
        cycle = bits.ue()
        if cycle > 255:
            raise EncodedFormatError("H.264 SPS picture-order cycle exceeds its bound")
        for _ in range(cycle):
            bits.se()
    elif order_type != 2:
        raise EncodedFormatError("Invalid H.264 SPS picture-order type")
    if bits.ue() > 16:
        raise EncodedFormatError("H.264 SPS reference-frame count exceeds its bound")
    bits.bit()  # gaps_in_frame_num_value_allowed_flag
    width_mbs, height_map_units = bits.ue() + 1, bits.ue() + 1
    frame_mbs_only = bits.bit()
    if not frame_mbs_only:
        bits.bit()  # mb_adaptive_frame_field_flag
    bits.bit()  # direct_8x8_inference_flag
    width, height = width_mbs * 16, height_map_units * 16 * (2 - frame_mbs_only)
    # Interlaced 2160-row content can require 2176 coded rows before cropping.
    if width > 4096 or height > 2176:
        raise EncodedFormatError("H.264 coded dimensions exceed the supported allocation bounds")
    if bits.bit():
        left, right, top, bottom = (bits.ue() for _ in range(4))
        chroma_array = 0 if separate_colour_plane else chroma_format
        crop_x = 1 if chroma_array in (0, 3) else 2
        crop_y = (2 if chroma_array == 1 else 1) * (2 - frame_mbs_only)
        width -= (left + right) * crop_x
        height -= (top + bottom) * crop_y
    if not 0 < width <= 4096 or not 0 < height <= 2160:
        raise EncodedFormatError("H.264 display dimensions exceed their supported bounds")
    return width, height


class AnnexBParser:
    """Incrementally parse an AUD-delimited, low-delay Annex B stream.

    ``h264_metadata=aud=insert`` is required at the producer boundary. A single
    NAL may cross any number of reads, including reads inside its start code.
    Invalid input poisons this parser; create another one after source restart.
    """

    def __init__(
        self,
        *,
        max_nal_bytes: int = MAX_ACCESS_UNIT_BYTES,
        max_access_unit_bytes: int = MAX_ACCESS_UNIT_BYTES,
        max_chunk_bytes: int = 256 * 1024,
        max_units_per_feed: int = 1024,
    ):
        for value in (max_nal_bytes, max_access_unit_bytes, max_chunk_bytes, max_units_per_feed):
            if type(value) is not int or value < 1:
                raise ValueError("Parser limits must be positive integers")
        if max_nal_bytes > MAX_ACCESS_UNIT_BYTES or max_access_unit_bytes > MAX_ACCESS_UNIT_BYTES:
            raise ValueError("Parser packet limits exceed the shared stream maximum")
        if max_chunk_bytes > MAX_ACCESS_UNIT_BYTES or max_units_per_feed > 1024:
            raise ValueError("Parser input or result limit exceeds the shared stream maximum")
        self.max_nal_bytes = max_nal_bytes
        self.max_access_unit_bytes = max_access_unit_bytes
        self.max_chunk_bytes = max_chunk_bytes
        self.max_units_per_feed = max_units_per_feed
        self._buffer = bytearray()
        self._start_length = 0
        self._search_offset = 0
        self._nals: list[bytes] = []
        self._unit_bytes = 0
        self._aud = False
        self._vcl = False
        self._vcl_kind: int | None = None
        self._keyframe = False
        self._pps_id: int | None = None
        self._sps: dict[int, bytes] = {}
        self._pps: dict[int, tuple[int, bytes]] = {}
        self._failed = False
        self._finished = False

    def feed(self, data: bytes) -> list[ParsedAccessUnit]:
        if self._failed or self._finished:
            raise EncodedFormatError("H.264 parser is closed after completion or failure")
        if not isinstance(data, bytes):
            raise TypeError("H.264 parser input must be immutable bytes")
        try:
            if len(data) > self.max_chunk_bytes:
                raise EncodedFormatError("H.264 input chunk exceeds its byte limit")
            self._buffer.extend(data)
            results: list[ParsedAccessUnit] = []
            while True:
                match = _START_CODE.search(self._buffer, self._search_offset)
                if self._start_length == 0:
                    if match is None:
                        if any(self._buffer):
                            raise EncodedFormatError(
                                "H.264 input does not begin with an Annex B start code"
                            )
                        if len(self._buffer) > self.max_nal_bytes:
                            raise EncodedFormatError("H.264 NAL exceeds its byte limit")
                        self._search_offset = max(0, len(self._buffer) - 3)
                        break
                    if any(self._buffer[: match.start()]):
                        raise EncodedFormatError("Unexpected bytes before H.264 start code")
                    del self._buffer[: match.start()]
                    self._start_length = match.end() - match.start()
                    self._search_offset = self._start_length
                    continue
                if match is None:
                    if len(self._buffer) > self.max_nal_bytes:
                        raise EncodedFormatError("H.264 NAL exceeds its byte limit")
                    self._search_offset = max(self._start_length, len(self._buffer) - 3)
                    break
                nal = bytes(self._buffer[: match.start()])
                del self._buffer[: match.start()]
                self._start_length = match.end() - match.start()
                self._search_offset = self._start_length
                unit = self._consume_nal(nal)
                if unit is not None:
                    results.append(unit)
                    if len(results) > self.max_units_per_feed:
                        raise EncodedFormatError("H.264 chunk contains too many access units")
            return results
        except EncodedFormatError:
            self._failed = True
            self._buffer.clear()
            self._nals.clear()
            raise

    def finish(self) -> list[ParsedAccessUnit]:
        if self._failed:
            raise EncodedFormatError("H.264 parser is closed after failure")
        if self._finished:
            return []
        self._finished = True
        try:
            results = []
            if self._buffer:
                if not self._start_length:
                    raise EncodedFormatError("Incomplete H.264 Annex B start code")
                unit = self._consume_nal(bytes(self._buffer))
                if unit is not None:
                    results.append(unit)
            self._buffer.clear()
            unit = self._finish_unit()
            if unit is not None:
                results.append(unit)
            return results
        except EncodedFormatError:
            self._failed = True
            self._buffer.clear()
            self._nals.clear()
            raise

    def end_access_unit(self) -> list[ParsedAccessUnit]:
        """Flush a trusted complete-camera-frame boundary and retain headers.

        This is for Picamera2's documented complete-frame callback, never for
        ordinary UVC or subprocess read chunks with unknown picture boundaries.
        """
        if self._failed or self._finished:
            raise EncodedFormatError("H.264 parser is closed after completion or failure")
        try:
            results = []
            if self._buffer:
                if not self._start_length:
                    raise EncodedFormatError("Incomplete H.264 Annex B start code")
                unit = self._consume_nal(bytes(self._buffer))
                if unit is not None:
                    results.append(unit)
            self._buffer.clear()
            self._start_length = 0
            self._search_offset = 0
            unit = self._finish_unit()
            if unit is not None:
                results.append(unit)
            self._aud = False
            return results
        except EncodedFormatError:
            self._failed = True
            self._buffer.clear()
            self._nals.clear()
            raise

    def _consume_nal(self, nal: bytes) -> ParsedAccessUnit | None:
        if len(nal) > self.max_nal_bytes:
            raise EncodedFormatError("H.264 NAL exceeds its byte limit")
        match = _START_CODE.match(nal)
        if match is None or len(nal) <= match.end():
            raise EncodedFormatError("Empty or malformed H.264 NAL")
        payload = memoryview(nal)[match.end() :]
        if payload[0] & 0x80:
            raise EncodedFormatError("Forbidden H.264 NAL header bit")
        kind = payload[0] & 31
        if kind not in (1, 5, 6, 7, 8, 9, 10, 11, 12):
            raise EncodedFormatError("Unsupported H.264 NAL type")
        if kind in (7, 8) and self._vcl:
            raise EncodedFormatError("H.264 parameter sets must precede picture slices after AUD")
        result = None
        if kind == 9:
            if len(payload) < 2:
                raise EncodedFormatError("Truncated H.264 access-unit delimiter")
            result = self._finish_unit()
            self._aud = True
        elif kind == 7:
            if len(payload) < 5:
                raise EncodedFormatError("Truncated H.264 sequence parameter set")
            sps_id = _Bits(payload[4:]).ue()
            if sps_id > 31:
                raise EncodedFormatError("Invalid H.264 sequence parameter set identifier")
            nal = b"\x00\x00\x00\x01" + payload
            self._sps[sps_id] = nal
        elif kind == 8:
            bits = _Bits(payload[1:])
            pps_id, sps_id = bits.ue(), bits.ue()
            if pps_id > 255 or sps_id > 31:
                raise EncodedFormatError("Invalid H.264 picture parameter set identifier")
            nal = b"\x00\x00\x00\x01" + payload
            self._pps[pps_id] = (sps_id, nal)
        elif kind in (1, 5):
            if not self._aud:
                raise EncodedFormatError("H.264 access-unit delimiters are required")
            bits = _Bits(payload[1:])
            bits.ue()  # first_mb_in_slice
            slice_type, pps_id = bits.ue(), bits.ue()
            if slice_type > 9 or pps_id > 255:
                raise EncodedFormatError("Invalid H.264 slice header")
            if slice_type % 5 == 1:
                raise EncodedFormatError("B slices are unsupported by the low-delay shared stream")
            if self._pps_id is not None and self._pps_id != pps_id:
                raise EncodedFormatError("H.264 access unit mixes picture parameter sets")
            if self._vcl_kind is not None and self._vcl_kind != kind:
                raise EncodedFormatError("H.264 access unit mixes IDR and dependent picture slices")
            self._pps_id = pps_id
            self._vcl_kind = kind
            self._vcl = True
            self._keyframe = self._keyframe or kind == 5
        self._unit_bytes += len(nal)
        if self._unit_bytes > self.max_access_unit_bytes:
            raise EncodedFormatError("H.264 access unit exceeds its byte limit")
        if (
            sum(map(len, self._sps.values())) + sum(len(n) for _, n in self._pps.values())
            > MAX_CONFIG_BYTES
        ):
            raise EncodedFormatError("H.264 decoder configuration exceeds its byte limit")
        self._nals.append(nal)
        return result

    def _finish_unit(self) -> ParsedAccessUnit | None:
        result = None
        if self._vcl:
            if self._pps_id not in self._pps:
                raise EncodedFormatError(
                    "H.264 slice references an unavailable picture parameter set"
                )
            sps_id, pps = self._pps[self._pps_id]
            sps = self._sps.get(sps_id)
            if sps is None:
                raise EncodedFormatError(
                    "H.264 picture references an unavailable sequence parameter set"
                )
            config = sps + pps
            nals = self._nals
            if self._keyframe:
                missing = [nal for nal in (sps, pps) if nal not in nals]
                # Preserve AUD as the first NAL when cached headers are repeated.
                nals = [nals[0], *missing, *nals[1:]]
            data = b"".join(nals)
            if len(data) > self.max_access_unit_bytes:
                raise EncodedFormatError("Configured H.264 access unit exceeds its byte limit")
            result = ParsedAccessUnit(data, self._keyframe, config)
        self._nals = []
        self._unit_bytes = 0
        self._vcl = False
        self._vcl_kind = None
        self._keyframe = False
        self._pps_id = None
        return result


class EncodedSubscription:
    """An independent bounded queue; recording overflow is always an error."""

    def __init__(
        self, hub: EncodedHub, mode: Literal["live", "recording"], max_bytes: int, max_frames: int
    ):
        self._hub = hub
        self.mode = mode
        self.max_bytes = max_bytes
        self.max_frames = max_frames
        self._queue: deque[EncodedAccessUnit] = deque()
        self._bytes = 0
        self._waiting_keyframe = True
        self._closed = False
        self._sealed = False
        self._error: Exception | None = None
        self._dropped_frames = 0

    @property
    def dropped_frames(self) -> int:
        with self._hub._condition:
            return self._dropped_frames

    def read(
        self, timeout: float | None = None, cancel_event: threading.Event | None = None
    ) -> EncodedAccessUnit | None:
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Subscription timeout must be a finite nonnegative number")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._hub._condition:
            while True:
                if self._closed or (cancel_event is not None and cancel_event.is_set()):
                    return None
                if self._error is not None:
                    raise self._error
                if self._queue:
                    result = self._queue.popleft()
                    self._bytes -= len(result.data)
                    return result
                if self._sealed:
                    return None
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                # External cancellation cannot notify our condition directly.
                if cancel_event is not None:
                    remaining = 0.05 if remaining is None else min(remaining, 0.05)
                self._hub._condition.wait(remaining)

    def close(self) -> None:
        with self._hub._condition:
            self._closed = True
            self._queue.clear()
            self._bytes = 0
            self._hub._subscribers.discard(self)
            self._hub._condition.notify_all()

    def finish(self) -> int:
        """Atomically stop future offers while retaining queued recording data."""
        with self._hub._condition:
            self._sealed = True
            self._hub._subscribers.discard(self)
            self._hub._condition.notify_all()
            return self._hub._sequence

    def _fail(self, error: Exception) -> None:
        if self._error is None:
            self._error = error
        self._queue.clear()
        self._bytes = 0

    def _offer(self, unit: EncodedAccessUnit) -> None:
        if self._closed or self._sealed or self._error is not None:
            return
        if self._waiting_keyframe and not unit.keyframe:
            if self.mode == "live":
                self._dropped_frames += 1
            return
        overflow = (
            self._bytes + len(unit.data) > self.max_bytes or len(self._queue) >= self.max_frames
        )
        if overflow:
            if self.mode == "recording":
                self._fail(
                    EncodedOverrun(
                        "Shared encoder recording queue overflowed; retain partial media"
                    )
                )
                return
            self._dropped_frames += len(self._queue)
            self._queue.clear()
            self._bytes = 0
            self._waiting_keyframe = True
            if not unit.keyframe or len(unit.data) > self.max_bytes:
                self._dropped_frames += 1
                return
        self._waiting_keyframe = False
        self._queue.append(unit)
        self._bytes += len(unit.data)


class EncodedHub:
    """Fan out one encoder generation, retaining a keyframe-aligned pre-roll."""

    def __init__(
        self,
        *,
        max_bytes: int = MAX_PRE_ROLL_BYTES,
        max_seconds: float = MAX_PRE_ROLL_SECONDS,
        max_frames: int = MAX_PRE_ROLL_FRAMES,
    ):
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_PRE_ROLL_BYTES:
            raise ValueError("Encoded pre-roll byte limit exceeds its supported bounds")
        if type(max_frames) is not int or not 1 <= max_frames <= MAX_PRE_ROLL_FRAMES:
            raise ValueError("Encoded pre-roll frame limit exceeds its supported bounds")
        if not math.isfinite(max_seconds) or not 0 <= max_seconds <= MAX_PRE_ROLL_SECONDS:
            raise ValueError("Encoded pre-roll duration exceeds its supported bounds")
        self.max_bytes, self.max_seconds, self.max_frames = max_bytes, max_seconds, max_frames
        self._condition = threading.Condition()
        self._ring: deque[EncodedAccessUnit] = deque()
        self._ring_bytes = 0
        self._subscribers: set[EncodedSubscription] = set()
        self._epoch = 1
        self._sequence = 0
        self._config = b""
        self._last_timestamp: float | None = None
        self._timestamps: deque[float] = deque(maxlen=120)
        self._ready = False
        self._closed = False

    @property
    def epoch(self) -> int:
        with self._condition:
            return self._epoch

    def publish(
        self, packet: ParsedAccessUnit, captured_at: float | None = None, *, epoch: int
    ) -> EncodedAccessUnit:
        if not isinstance(packet, ParsedAccessUnit):
            raise TypeError("Shared encoder packets must be parsed access units")
        if not isinstance(packet.data, bytes) or not isinstance(packet.decoder_config, bytes):
            raise TypeError("Shared encoder packets must contain immutable bytes")
        if not packet.data or len(packet.data) > MAX_ACCESS_UNIT_BYTES:
            raise EncodedFormatError("Encoded packet exceeds the access-unit byte limit")
        if not packet.decoder_config or len(packet.decoder_config) > MAX_CONFIG_BYTES:
            raise EncodedFormatError("Encoded packet has no bounded decoder configuration")
        received = time.monotonic() if captured_at is None else captured_at
        if not math.isfinite(received) or received < 0:
            raise ValueError("Encoded packet timestamp must be finite and nonnegative")
        with self._condition:
            if self._closed:
                raise EncodedStreamEnded("Shared encoder stream is closed")
            if epoch != self._epoch:
                raise EncodedRestart("Stale shared encoder generation cannot publish packets")
            if self._last_timestamp is not None and received < self._last_timestamp:
                raise ValueError("Encoded packet timestamps must not move backwards")
            if self._config and packet.decoder_config != self._config:
                raise EncodedFormatError(
                    "Shared encoder decoder configuration changed within one generation"
                )
            self._last_timestamp = received
            self._timestamps.append(received)
            self._config = packet.decoder_config
            self._ready = self._ready or packet.keyframe
            self._sequence += 1
            unit = EncodedAccessUnit(
                packet.data,
                received,
                self._sequence,
                self._epoch,
                packet.keyframe,
                packet.decoder_config,
            )
            if self._ring or unit.keyframe:
                self._ring.append(unit)
                self._ring_bytes += len(unit.data)
                self._prune(received)
            for subscription in self._subscribers:
                subscription._offer(unit)
            self._condition.notify_all()
            return unit

    def _prune(self, received: float) -> None:
        while self._ring and (
            self._ring_bytes > self.max_bytes
            or len(self._ring) > self.max_frames
            or received - self._ring[0].captured_at > self.max_seconds
        ):
            self._ring_bytes -= len(self._ring.popleft().data)
        # A retained suffix must never begin with a dependent P frame.
        while self._ring and not self._ring[0].keyframe:
            self._ring_bytes -= len(self._ring.popleft().data)

    def subscribe(
        self,
        mode: Literal["live", "recording"] = "live",
        *,
        pre_roll_seconds: float = 0,
        max_bytes: int = MAX_PRE_ROLL_BYTES,
        max_frames: int = MAX_PRE_ROLL_FRAMES,
    ) -> EncodedSubscription:
        if mode not in ("live", "recording"):
            raise ValueError("Shared encoder consumer must be live or recording")
        if not math.isfinite(pre_roll_seconds) or not 0 <= pre_roll_seconds <= self.max_seconds:
            raise ValueError("Requested encoded pre-roll exceeds the retained duration")
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_PRE_ROLL_BYTES:
            raise ValueError("Encoded consumer byte limit exceeds its supported bounds")
        if type(max_frames) is not int or not 1 <= max_frames <= MAX_PRE_ROLL_FRAMES:
            raise ValueError("Encoded consumer frame limit exceeds its supported bounds")
        with self._condition:
            if self._closed:
                raise EncodedStreamEnded("Shared encoder stream is closed")
            if len(self._subscribers) >= MAX_SUBSCRIBERS:
                raise EncodedOverrun("Shared encoder consumer limit reached")
            subscription = EncodedSubscription(self, mode, max_bytes, max_frames)
            if self._ring and (mode == "live" or pre_roll_seconds > 0):
                cutoff = self._ring[-1].captured_at - pre_roll_seconds
                # The preceding IDR is necessary even when the requested cutoff
                # lies between IDRs. For live joins retain only the latest GOP.
                # Recording without pre-roll instead waits for the next IDR.
                start = 0
                for index, unit in enumerate(self._ring):
                    if unit.keyframe and unit.captured_at <= cutoff:
                        start = index
                for unit in list(self._ring)[start:]:
                    subscription._offer(unit)
            self._subscribers.add(subscription)
            return subscription

    def restart(self) -> int:
        with self._condition:
            if self._closed:
                raise EncodedStreamEnded("Shared encoder stream is closed")
            self._epoch += 1
            self._ring.clear()
            self._ring_bytes = 0
            self._config = b""
            self._last_timestamp = None
            self._timestamps.clear()
            self._ready = False
            for subscription in self._subscribers:
                subscription._fail(
                    EncodedRestart("Shared encoder restarted; start a new media session")
                )
            self._condition.notify_all()
            return self._epoch

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._ready = False
            self._ring.clear()
            self._ring_bytes = 0
            for subscription in self._subscribers:
                subscription._fail(EncodedStreamEnded("Shared encoder stream ended"))
            self._condition.notify_all()

    def stats(self) -> dict[str, int | float | bool | None]:
        with self._condition:
            elapsed = self._timestamps[-1] - self._timestamps[0] if self._timestamps else 0
            return {
                "epoch": self._epoch,
                "buffered_frames": len(self._ring),
                "buffered_bytes": self._ring_bytes,
                "subscribers": len(self._subscribers),
                "published_frames": self._sequence,
                "dropped_frames": sum(s._dropped_frames for s in self._subscribers),
                "ready": self._ready,
                "fps": (len(self._timestamps) - 1) / elapsed if elapsed > 0 else 0.0,
                "last_frame_age_seconds": (
                    max(0.0, time.monotonic() - self._last_timestamp)
                    if self._last_timestamp is not None
                    else None
                ),
                "buffered_duration_seconds": (
                    self._ring[-1].captured_at - self._ring[0].captured_at if self._ring else 0.0
                ),
                "last_sequence": self._sequence,
                "last_timestamp": self._last_timestamp,
            }
