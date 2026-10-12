"""Timestamp-preserving MPEG-TS framing for the bounded low-delay H.264 hub.

One immutable access unit is wrapped per call. The hub's Annex B parser rejects
B slices and supplies decoder configuration before IDRs; no pixels are decoded
and no encoder or camera is created by this transport adapter.
"""

from __future__ import annotations

import math

from .encoded import MAX_ACCESS_UNIT_BYTES, MAX_CONFIG_BYTES, EncodedAccessUnit

TS_PACKET_BYTES = 188
VIDEO_PID = 256
PMT_PID = 4096
CLOCK_RATE = 90_000
TABLE_PERIOD_SECONDS = 0.5
TIMESTAMP_MASK = (1 << 33) - 1


class TransportStreamError(ValueError):
    """Malformed packets or clock changes require a fresh media session."""


def _crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for value in data:
        crc ^= value << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
    return crc


def _section(data: bytes) -> bytes:
    return data + _crc32_mpeg(data).to_bytes(4, "big")


_PAT = _section(b"\x00\xb0\x0d\x00\x01\xc1\x00\x00\x00\x01\xf0\x00")
_PMT = _section(b"\x02\xb0\x12\x00\x01\xc1\x00\x00\xe1\x00\xf0\x00\x1b\xe1\x00\xf0\x00")


def _timestamp(value: int, prefix: int) -> bytes:
    value &= TIMESTAMP_MASK
    return bytes(
        (
            (prefix << 4) | (((value >> 30) & 7) << 1) | 1,
            (value >> 22) & 255,
            (((value >> 15) & 127) << 1) | 1,
            (value >> 7) & 255,
            ((value & 127) << 1) | 1,
        )
    )


def _pcr(value: int) -> bytes:
    value &= TIMESTAMP_MASK
    return bytes(
        (
            (value >> 25) & 255,
            (value >> 17) & 255,
            (value >> 9) & 255,
            (value >> 1) & 255,
            ((value & 1) << 7) | 0x7E,
            0,
        )
    )


class TransportStreamMuxer:
    """Frame one epoch into MPEG-TS with source-clock PTS/DTS and PCR.

    Start at one second to leave a nonnegative decode clock. Preserve elapsed
    capture time even when source FPS differs from its configured profile or a
    retained pre-roll is delivered in a burst. Only sub-tick ties are advanced.
    Each instance belongs to one consumer and must not be called concurrently.
    """

    def __init__(self, epoch: int | None = None):
        if epoch is not None and (type(epoch) is not int or epoch < 1):
            raise ValueError("Transport stream epoch must be a positive integer")
        self.epoch = epoch
        self._continuity = {0: 0, PMT_PID: 0, VIDEO_PID: 0}
        self._origin: float | None = None
        self._last_captured_at: float | None = None
        self._last_ticks: int | None = None
        self._last_sequence = 0
        self._last_tables_at: float | None = None
        self._configuration = b""

    def _packet(
        self,
        pid: int,
        payload: bytes | memoryview,
        *,
        start: bool = False,
        clock: int | None = None,
        keyframe: bool = False,
    ) -> bytes:
        capacity = 176 if clock is not None else 184
        if not 0 < len(payload) <= capacity:
            raise TransportStreamError("Transport payload exceeds its packet bounds")
        adaptation_length = 183 - len(payload)
        adaptation = clock is not None or len(payload) < 184
        control = 0x30 if adaptation else 0x10
        counter = self._continuity[pid]
        self._continuity[pid] = (counter + 1) & 15
        header = bytes((0x47, (pid >> 8) | (0x40 if start else 0), pid & 255, control | counter))
        if not adaptation:
            return header + payload
        field = bytes((adaptation_length,))
        if adaptation_length:
            flags = (0x10 if clock is not None else 0) | (0x40 if keyframe else 0)
            contents = bytes((flags,)) + (_pcr(clock) if clock is not None else b"")
            field += contents + b"\xff" * (adaptation_length - len(contents))
        return header + field + payload

    def _tables(self) -> bytes:
        return b"".join(
            self._packet(pid, b"\x00" + section + b"\xff" * (183 - len(section)), start=True)
            for pid, section in ((0, _PAT), (PMT_PID, _PMT))
        )

    def mux(self, unit: EncodedAccessUnit) -> bytes:
        if not isinstance(unit, EncodedAccessUnit):
            raise TypeError("Transport input must be an encoded access unit")
        if (
            not isinstance(unit.data, bytes)
            or not 0 < len(unit.data) <= MAX_ACCESS_UNIT_BYTES
            or not unit.data.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01"))
        ):
            raise TransportStreamError("Transport input is not a bounded Annex B access unit")
        if (
            not isinstance(unit.decoder_config, bytes)
            or not 0 < len(unit.decoder_config) <= MAX_CONFIG_BYTES
        ):
            raise TransportStreamError("Transport input requires bounded decoder configuration")
        if type(unit.epoch) is not int or unit.epoch < 1:
            raise TransportStreamError("Transport input epoch must be a positive integer")
        if self.epoch is not None and unit.epoch != self.epoch:
            raise TransportStreamError("Encoder epoch changed; start a new transport stream")
        if type(unit.sequence) is not int or unit.sequence <= self._last_sequence:
            raise TransportStreamError(
                "Transport access units must have increasing sequence numbers"
            )
        try:
            valid_clock = (
                type(unit.captured_at) in (int, float)
                and math.isfinite(unit.captured_at)
                and unit.captured_at >= 0
            )
        except OverflowError:
            valid_clock = False
        if not valid_clock:
            raise TransportStreamError("Transport timestamps must be finite and nonnegative")
        if self._last_captured_at is not None and unit.captured_at < self._last_captured_at:
            raise TransportStreamError("Transport capture clock moved backwards")
        if self._configuration and unit.decoder_config != self._configuration:
            raise TransportStreamError("Decoder configuration changed within the transport stream")
        if self._origin is None and not unit.keyframe:
            raise TransportStreamError(
                "Transport stream must begin with an independently decodable IDR"
            )
        origin = unit.captured_at if self._origin is None else self._origin
        elapsed_ticks = (unit.captured_at - origin) * CLOCK_RATE
        if not math.isfinite(elapsed_ticks):
            raise TransportStreamError("Transport timestamp exceeds supported clock bounds")
        ticks = CLOCK_RATE + round(elapsed_ticks)
        if self._last_ticks is not None:
            ticks = max(ticks, self._last_ticks + 1)
        # Video PES may omit packet length; this avoids a 64-KiB access-unit cap.
        pes_header = (
            b"\x00\x00\x01\xe0\x00\x00\x80\xc0\x0a" + _timestamp(ticks, 3) + _timestamp(ticks, 1)
        )
        result = bytearray()
        if (
            self._last_tables_at is None
            or unit.keyframe
            or unit.captured_at - self._last_tables_at >= TABLE_PERIOD_SECONDS
        ):
            result.extend(self._tables())
            self._last_tables_at = unit.captured_at
        # Slice directly from the immutable access unit. Do not duplicate the
        # entire compressed payload into another PES-sized intermediate buffer.
        view = memoryview(unit.data)
        first_payload = pes_header + view[: 176 - len(pes_header)]
        result.extend(
            self._packet(VIDEO_PID, first_payload, start=True, clock=ticks, keyframe=unit.keyframe)
        )
        offset = len(first_payload) - len(pes_header)
        while offset < len(view):
            chunk = view[offset : offset + 184]
            result.extend(self._packet(VIDEO_PID, chunk))
            offset += len(chunk)
        self.epoch = unit.epoch
        self._origin = origin
        self._last_captured_at = unit.captured_at
        self._last_ticks = ticks
        self._last_sequence = unit.sequence
        self._configuration = unit.decoder_config
        return bytes(result)
