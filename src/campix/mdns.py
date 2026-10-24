"""DNS-SD announcer for Home Assistant. Stdlib sockets only.

Answers queries for `_campix._tcp.local` and repeats a small announcement.
The TXT record carries the version, a stable service id, and the catalog path.
Camera URLs, passwords, and API tokens are never advertised.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import secrets
import socket
import struct
import threading
import time

LOG = logging.getLogger(__name__)

SERVICE_TYPE = "_campix._tcp.local"
CATALOG_PATH = "/api/v1/integrations/homeassistant"
MDNS_GROUP = ("224.0.0.251", 5353)
TYPE_A = 1
TYPE_PTR = 12
TYPE_TXT = 16
TYPE_SRV = 33
TYPE_ANY = 255


def discovery_id(db) -> str:
    current = db.setting("discovery_id")
    if isinstance(current, str) and re.fullmatch(r"[a-f0-9]{16}", current):
        return current
    value = secrets.token_hex(8)
    db.set_setting("discovery_id", value)
    return value


def advertise_ipv4(host: str) -> str:
    """Return a private, global, or loopback IPv4 address. Never link-local."""
    if host in {"127.0.0.1", "localhost"}:
        return "127.0.0.1"
    if host not in {"0.0.0.0", "::", ""}:
        try:
            candidate = ipaddress.ip_address(host)
        except ValueError:
            candidate = None
        if isinstance(candidate, ipaddress.IPv4Address) and _advertisable(candidate):
            return str(candidate)
    discovered = _outbound_ipv4()
    if discovered is not None and _advertisable(discovered):
        return str(discovered)
    return "127.0.0.1"


def _advertisable(ip: ipaddress.IPv4Address) -> bool:
    return bool(
        (ip.is_private or ip.is_global or ip.is_loopback)
        and not (ip.is_multicast or ip.is_unspecified or ip.is_link_local or ip.is_reserved)
    )


def _outbound_ipv4():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        try:
            sock.connect(("10.255.255.255", 1))
            address = ipaddress.ip_address(sock.getsockname()[0])
        except OSError:
            return None
    finally:
        sock.close()
    return address if isinstance(address, ipaddress.IPv4Address) else None


class DiscoveryAnnouncer:
    def __init__(self, port, version, service_id, address, group=MDNS_GROUP):
        self.port = int(port)
        self.version = version if re.fullmatch(r"[A-Za-z0-9._-]{1,32}", version) else "unknown"
        self.service_id = service_id
        self.address = address
        self.group = group
        self.service_type = SERVICE_TYPE
        self.instance = f"CamPiX-{service_id}.{SERVICE_TYPE}"
        self.host = f"campix-{service_id}.local"
        self.halt = threading.Event()
        self.sock = None
        self.thread = threading.Thread(target=self._run, name="campix-mdns", daemon=True)

    def start(self):
        try:
            self.sock = self._open_socket()
        except OSError:
            LOG.warning("Home Assistant discovery is off because mDNS port 5353 is unavailable")
            self.sock = None
            return
        self.thread.start()

    def stop(self):
        self.halt.set()
        sock = self.sock
        if sock is not None:
            try:
                sock.sendto(self.announcement(ttl=0), self.group)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def announcement(self, ttl=120) -> bytes:
        return _message(0, [self._ptr(ttl), *self._unique(ttl)])

    def response_for(self, packet: bytes):
        try:
            header, questions = _questions(packet)
        except ValueError:
            return None
        if header["qr"] or not questions:
            return None
        answers = []
        additionals = []
        instance = _normal(self.instance)
        host = _normal(self.host)
        service = _normal(self.service_type)
        for question in questions:
            name = _normal(question["name"])
            qtype = question["type"]
            if name == service and qtype in {TYPE_PTR, TYPE_ANY}:
                answers.append(self._ptr(120))
                additionals.extend(self._unique(120))
            elif name == instance and qtype in {TYPE_SRV, TYPE_ANY}:
                answers.append(self._srv(120))
            elif name == instance and qtype in {TYPE_TXT, TYPE_ANY}:
                answers.append(self._txt(120))
            elif name == host and qtype in {TYPE_A, TYPE_ANY}:
                answers.append(self._a(120))
            elif name == "_services._dns-sd._udp.local" and qtype in {TYPE_PTR, TYPE_ANY}:
                answers.append(self._service_pointer(120))
        if not answers and not additionals:
            return None
        return _message(header["id"], answers, additionals)

    def _ptr(self, ttl):
        return _record(self.service_type, TYPE_PTR, _encode_name(self.instance), ttl, flush=False)

    def _service_pointer(self, ttl):
        return _record(
            "_services._dns-sd._udp.local",
            TYPE_PTR,
            _encode_name(self.service_type),
            ttl,
            flush=False,
        )

    def _srv(self, ttl):
        rdata = struct.pack("!HHH", 0, 0, self.port) + _encode_name(self.host)
        return _record(self.instance, TYPE_SRV, rdata, ttl, flush=True)

    def _txt(self, ttl):
        rdata = _txt(
            (
                ("id", self.service_id),
                ("version", self.version),
                ("path", CATALOG_PATH),
            )
        )
        return _record(self.instance, TYPE_TXT, rdata, ttl, flush=True)

    def _a(self, ttl):
        return _record(self.host, TYPE_A, socket.inet_aton(self.address), ttl, flush=True)

    def _unique(self, ttl):
        return [self._srv(ttl), self._txt(ttl), self._a(ttl)]

    def _open_socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            sock.bind(("", self.group[1]))
            membership = struct.pack(
                "4s4s", socket.inet_aton(self.group[0]), socket.inet_aton("0.0.0.0")
            )
            if self.group[0] == "224.0.0.251":
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            sock.settimeout(0.5)
        except BaseException:
            sock.close()
            raise
        return sock

    def _run(self):
        next_announce = 0.0
        while not self.halt.is_set():
            now = time.monotonic()
            if now >= next_announce and self.sock is not None:
                try:
                    self.sock.sendto(self.announcement(), self.group)
                except OSError:
                    LOG.warning("mDNS announcement failed")
                next_announce = now + 60
            try:
                packet, _address = self.sock.recvfrom(9000)
            except TimeoutError:
                continue
            except OSError:
                if self.halt.is_set():
                    return
                continue
            reply = self.response_for(packet)
            if not reply or self.sock is None:
                continue
            try:
                # Multicast only. A unicast reply to the packet source can be spoofed.
                self.sock.sendto(reply, self.group)
            except OSError:
                LOG.warning("mDNS reply failed")


def _normal(name: str) -> str:
    return name.rstrip(".").lower()


def _encode_name(name: str) -> bytes:
    labels = name.rstrip(".").split(".")
    if not labels or any(not label or len(label) > 63 for label in labels):
        raise ValueError("name")
    return b"".join(bytes([len(label)]) + label.encode("ascii") for label in labels) + b"\x00"


def _txt(pairs) -> bytes:
    chunks = []
    for key, value in pairs:
        item = f"{key}={value}".encode("ascii")
        if not item or len(item) > 255:
            raise ValueError("txt")
        chunks.append(bytes([len(item)]) + item)
    return b"".join(chunks)


def _record(name, rtype, rdata, ttl, flush) -> bytes:
    clas = 0x8001 if flush else 1
    return _encode_name(name) + struct.pack("!HHIH", rtype, clas, ttl, len(rdata)) + rdata


def _message(qid, answers, additionals=()) -> bytes:
    def unique(records):
        seen = []
        for record in records:
            if record not in seen:
                seen.append(record)
        return seen

    answers = unique(answers)
    additionals = unique(record for record in additionals if record not in answers)
    header = struct.pack("!HHHHHH", qid & 0xFFFF, 0x8400, 0, len(answers), 0, len(additionals))
    return header + b"".join(answers) + b"".join(additionals)


def _questions(packet: bytes):
    if len(packet) < 12 or len(packet) > 9000:
        raise ValueError("size")
    qid, flags, qdcount, _an, _ns, _ar = struct.unpack("!HHHHHH", packet[:12])
    if qdcount > 8:
        raise ValueError("questions")
    offset = 12
    questions = []
    for _index in range(qdcount):
        name, offset = _read_name(packet, offset)
        if offset + 4 > len(packet):
            raise ValueError("question")
        qtype, qclass = struct.unpack("!HH", packet[offset : offset + 4])
        offset += 4
        questions.append({"name": name, "type": qtype, "class": qclass & 0x7FFF})
    return {"id": qid, "qr": bool(flags & 0x8000)}, questions


def _read_name(data: bytes, offset: int, depth: int = 0):
    if depth > 8:
        raise ValueError("name")
    labels = []
    jumped = False
    end = offset
    while True:
        if offset >= len(data):
            raise ValueError("truncated")
        length = data[offset]
        if length == 0:
            if not jumped:
                end = offset + 1
            return ".".join(labels), end
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("truncated")
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if pointer >= len(data):
                raise ValueError("pointer")
            if not jumped:
                end = offset + 2
            rest, _rest_end = _read_name(data, pointer, depth + 1)
            if labels and rest:
                return ".".join(labels) + "." + rest, end
            return rest or ".".join(labels), end
        if length & 0xC0:
            raise ValueError("label")
        offset += 1
        if length > 63 or offset + length > len(data):
            raise ValueError("label")
        try:
            labels.append(data[offset : offset + length].decode("ascii"))
        except UnicodeDecodeError as exc:
            raise ValueError("label") from exc
        offset += length
