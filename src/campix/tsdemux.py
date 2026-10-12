"""Bounded video-only native MPEG-TS/PES demux preserving the camera's PTS.

This adapter accepts the service's fixed FFmpeg program/PID layout. It does not
execute pipelines or decode pixels. Start a new instance after discontinuity.
"""

from __future__ import annotations

from dataclasses import dataclass

from .encoded import MAX_ACCESS_UNIT_BYTES
from .tsmux import PMT_PID, TIMESTAMP_MASK, TS_PACKET_BYTES, VIDEO_PID, _crc32_mpeg

MAX_DEMUX_CHUNK_BYTES = 65536


class TransportDemuxError(ValueError):
    """Malformed or discontinuous native transport requires source restart."""


@dataclass(frozen=True, slots=True)
class NativeTransportAccessUnit:
    data: bytes
    timestamp_us: int
    keyframe: bool


class NativeTransportStreamDemuxer:
    """Accept one H.264 video program and unwrap its 90-kHz presentation clock."""

    def __init__(self, video_pid: int = VIDEO_PID):
        if video_pid != VIDEO_PID:
            raise ValueError("Native video PID must be 256")
        self.video_pid = video_pid
        self._buffer = bytearray()
        self._continuity: dict[int, int] = {}
        self._pat = False
        self._pmt = False
        self._pes = bytearray()
        self._pes_length: int | None = None
        self._pts: int | None = None
        self._first_pts: int | None = None
        self._last_pts: int | None = None
        self._unwrapped_pts = 0
        self._keyframe = False
        self._failed = False
        self._finished = False

    def feed(self, data: bytes) -> list[NativeTransportAccessUnit]:
        if self._failed or self._finished:
            raise TransportDemuxError("Native transport parser is closed")
        if not isinstance(data, bytes):
            raise TypeError("Native transport input must be immutable bytes")
        try:
            if len(data) > MAX_DEMUX_CHUNK_BYTES:
                raise TransportDemuxError("Native transport chunk exceeds its byte limit")
            self._buffer.extend(data)
            units = []
            offset = 0
            while len(self._buffer) - offset >= TS_PACKET_BYTES:
                packet = bytes(self._buffer[offset : offset + TS_PACKET_BYTES])
                offset += TS_PACKET_BYTES
                units.extend(self._packet(packet))
            del self._buffer[:offset]
            return units
        except TransportDemuxError:
            self._failed = True
            self._buffer.clear()
            self._pes.clear()
            raise

    def finish(self) -> list[NativeTransportAccessUnit]:
        """Flush only after successful FFmpeg EOF, never after a killed or failed source."""
        if self._failed:
            raise TransportDemuxError("Native transport parser is closed")
        if self._finished:
            return []
        self._finished = True
        if self._buffer:
            self._pes.clear()
            raise TransportDemuxError("Native transport ended within a TS packet")
        if self._pts is None:
            return []
        try:
            return [self._finish_pes()]
        finally:
            self._pes.clear()

    def _packet(self, packet: bytes) -> list[NativeTransportAccessUnit]:
        if packet[0] != 0x47 or packet[1] & 0x80 or packet[3] & 0xC0:
            raise TransportDemuxError("Native transport has invalid synchronization or scrambling")
        pid = ((packet[1] & 31) << 8) | packet[2]
        if pid not in {0, 17, PMT_PID, self.video_pid, 8191}:
            raise TransportDemuxError("Native transport contains an unsupported PID")
        control = (packet[3] >> 4) & 3
        if control == 0:
            raise TransportDemuxError("Native transport has invalid adaptation control")
        offset = 4
        random_access = False
        if control & 2:
            length = packet[4]
            offset = 5 + length
            if offset > TS_PACKET_BYTES or (control == 2 and offset != TS_PACKET_BYTES):
                raise TransportDemuxError("Native transport adaptation exceeds its packet")
            if length:
                if packet[5] & 0x80:
                    raise TransportDemuxError("Native transport clock is discontinuous")
                random_access = bool(packet[5] & 0x40)
        if not control & 1 or pid == 8191:
            return []
        counter = packet[3] & 15
        previous = self._continuity.get(pid)
        if previous is not None and counter != (previous + 1) & 15:
            raise TransportDemuxError("Native transport packet continuity changed")
        self._continuity[pid] = counter
        payload = packet[offset:]
        start = bool(packet[1] & 0x40)
        if pid in {0, PMT_PID}:
            self._program(pid, payload, start)
            return []
        if pid == 17:
            return []
        if not self._pat or not self._pmt:
            raise TransportDemuxError("Native video arrived before verified program tables")
        units = []
        if start:
            if self._pts is not None:
                units.append(self._finish_pes())
            self._begin_pes(payload, random_access)
        else:
            if self._pts is None:
                raise TransportDemuxError("Native video continuation has no PES header")
            self._append(payload)
        if self._pes_length is not None and len(self._pes) == self._pes_length:
            units.append(self._finish_pes())
        return units

    def _program(self, pid: int, payload: bytes, start: bool):
        if not start or not payload or payload[0] != 0 or len(payload) < 4:
            raise TransportDemuxError("Native program table has an unsupported pointer or split")
        section = payload[1:]
        length = ((section[1] & 15) << 8) | section[2]
        if length + 3 > len(section) or length < 9:
            raise TransportDemuxError("Native program table is truncated")
        section = section[: length + 3]
        if _crc32_mpeg(section) != 0 or not section[5] & 1 or section[6:8] != b"\x00\x00":
            raise TransportDemuxError("Native program table has invalid CRC or version")
        if pid == 0:
            if (
                section[0] != 0
                or length != 13
                or section[8:10] != b"\x00\x01"
                or ((section[10] & 31) << 8) | section[11] != PMT_PID
            ):
                raise TransportDemuxError("Native PAT does not identify the fixed video program")
            self._pat = True
        else:
            if (
                section[0] != 2
                or length != 18
                or section[3:5] != b"\x00\x01"
                or ((section[8] & 31) << 8) | section[9] != self.video_pid
                or section[10:13] != b"\xf0\x00\x1b"
                or ((section[13] & 31) << 8) | section[14] != self.video_pid
                or section[15:17] != b"\xf0\x00"
            ):
                raise TransportDemuxError("Native PMT does not identify one H.264 video stream")
            self._pmt = True

    @staticmethod
    def _read_timestamp(data: bytes, prefix: int) -> int:
        if (
            len(data) != 5
            or data[0] >> 4 != prefix
            or any(not data[index] & 1 for index in (0, 2, 4))
        ):
            raise TransportDemuxError("Native PES timestamp is invalid")
        return (
            (((data[0] >> 1) & 7) << 30)
            | (data[1] << 22)
            | ((data[2] >> 1) << 15)
            | (data[3] << 7)
            | (data[4] >> 1)
        )

    def _begin_pes(self, payload: bytes, random_access: bool):
        if len(payload) < 14 or payload[:4] != b"\x00\x00\x01\xe0" or payload[6] & 0xC0 != 0x80:
            raise TransportDemuxError("Native video PES header is invalid")
        flags, header_length = payload[7] >> 6, payload[8]
        if (
            flags not in {2, 3}
            or header_length < (10 if flags == 3 else 5)
            or 9 + header_length > len(payload)
        ):
            raise TransportDemuxError("Native video PES requires a complete presentation timestamp")
        pts = self._read_timestamp(payload[9:14], 3 if flags == 3 else 2)
        if flags == 3 and self._read_timestamp(payload[14:19], 1) != pts:
            raise TransportDemuxError("Native transport contains reordered video timestamps")
        length = int.from_bytes(payload[4:6], "big")
        expected = length - 3 - header_length if length else None
        if expected is not None and not 0 < expected <= MAX_ACCESS_UNIT_BYTES:
            raise TransportDemuxError("Native video PES exceeds its payload bounds")
        self._pts, self._pes_length, self._keyframe = pts, expected, random_access
        self._append(payload[9 + header_length :])

    def _append(self, payload: bytes):
        if len(self._pes) + len(payload) > MAX_ACCESS_UNIT_BYTES:
            raise TransportDemuxError("Native H.264 access unit exceeds its byte limit")
        if self._pes_length is not None and len(self._pes) + len(payload) > self._pes_length:
            raise TransportDemuxError("Native video PES exceeds its declared length")
        self._pes.extend(payload)

    def _finish_pes(self) -> NativeTransportAccessUnit:
        if (
            self._pts is None
            or not self._pes
            or not self._pes.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01"))
        ):
            raise TransportDemuxError("Native PES has no complete Annex B video payload")
        if self._pes_length is not None and len(self._pes) != self._pes_length:
            raise TransportDemuxError("Native video PES ended before its declared length")
        pts = self._pts
        if self._first_pts is None:
            self._first_pts = pts
        else:
            delta = (pts - self._last_pts) & TIMESTAMP_MASK
            if delta == 0 or delta > TIMESTAMP_MASK // 2:
                raise TransportDemuxError("Native presentation clock moved backwards or repeated")
            self._unwrapped_pts += delta
        self._last_pts = pts
        unit = NativeTransportAccessUnit(
            bytes(self._pes),
            round(self._unwrapped_pts * 1_000_000 / 90_000),
            self._keyframe,
        )
        self._pes.clear()
        self._pts, self._pes_length = None, None
        return unit
