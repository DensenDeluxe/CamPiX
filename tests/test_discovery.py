import ast
import json
import struct
from pathlib import Path

from fastapi.testclient import TestClient

from campix.app import create_app
from campix.cli import serve_advertises
from campix.config import Settings
from campix.db import Database
from campix.mdns import (
    CATALOG_PATH,
    TYPE_PTR,
    DiscoveryAnnouncer,
    advertise_ipv4,
    discovery_id,
)


def _question(name, qtype, qid=7):
    encoded = b"".join(bytes([len(part)]) + part.encode() for part in name.split(".")) + b"\x00"
    return struct.pack("!HHHHHH", qid, 0, 1, 0, 0, 0) + encoded + struct.pack("!HH", qtype, 1)


def test_discovery_id_and_announcement_hide_camera_details(tmp_path):
    database = Database(tmp_path / "campix.sqlite3")
    try:
        service_id = discovery_id(database)
        assert discovery_id(database) == service_id
    finally:
        database.close()
    announcer = DiscoveryAnnouncer(8080, "0.2.0", service_id, "192.168.1.20")
    packet = announcer.announcement()
    assert b"rtsp://" not in packet
    assert b"cx_" not in packet
    assert b"super-secret" not in packet
    path = f"path={CATALOG_PATH}".encode()
    assert bytes([len(path)]) + path in packet
    identity = f"id={service_id}".encode()
    assert bytes([len(identity)]) + identity in packet
    reply = announcer.response_for(_question(announcer.service_type, TYPE_PTR))
    assert reply is not None
    assert struct.pack("!H", 8080) in reply
    assert reply[2] & 0x80
    assert announcer.response_for(packet) is None
    assert announcer.response_for(_question("_other._tcp.local", TYPE_PTR)) is None
    loop = struct.pack("!HHHHHH", 1, 0, 1, 0, 0, 0) + b"\xc0\x0c" + struct.pack("!HH", TYPE_PTR, 1)
    assert announcer.response_for(loop) is None


def test_advertise_ipv4_skips_link_local_addresses(monkeypatch):
    import ipaddress

    from campix import mdns

    assert advertise_ipv4("192.168.1.20") == "192.168.1.20"
    assert advertise_ipv4("127.0.0.1") == "127.0.0.1"
    monkeypatch.setattr(mdns, "_outbound_ipv4", lambda: ipaddress.ip_address("169.254.1.1"))
    assert advertise_ipv4("0.0.0.0") == "127.0.0.1"
    assert advertise_ipv4("169.254.169.254") == "127.0.0.1"
    monkeypatch.setattr(mdns, "_outbound_ipv4", lambda: ipaddress.ip_address("10.1.2.3"))
    assert advertise_ipv4("0.0.0.0") == "10.1.2.3"
    assert advertise_ipv4("169.254.169.254") == "10.1.2.3"


def test_discovery_bind_failure_does_not_stop_the_service():
    announcer = DiscoveryAnnouncer(8080, "0.2.0", "ab" * 8, "127.0.0.1")

    def fail():
        raise OSError("busy")

    announcer._open_socket = fail
    announcer.start()
    assert announcer.sock is None
    announcer.stop()


def test_serve_announces_unless_discovery_is_disabled(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", mdns=False)
    assert serve_advertises(settings, None) is True
    monkeypatch.setenv("CAMPIX_MDNS", "false")
    disabled = Settings.load(data_dir=tmp_path / "env")
    assert disabled.mdns is False
    assert serve_advertises(disabled, None) is False
    config = tmp_path / "campix.json"
    config.write_text(json.dumps({"mdns": False}), encoding="utf-8")
    monkeypatch.delenv("CAMPIX_MDNS")
    from_file = Settings.load(config)
    assert serve_advertises(from_file, config) is False


def test_service_announces_only_when_enabled(tmp_path, monkeypatch):
    created = []

    class FakeAnnouncer:
        def __init__(self, port, version, service_id, address):
            created.append((port, version, service_id, address))

        def start(self):
            created.append("start")

        def stop(self):
            created.append("stop")

    monkeypatch.setattr("campix.app.DiscoveryAnnouncer", FakeAnnouncer)
    quiet = create_app(Settings(data_dir=tmp_path / "off", demo=True, min_free_mb=32))
    with TestClient(quiet):
        assert created == []
    announced = create_app(Settings(data_dir=tmp_path / "on", demo=True, min_free_mb=32, mdns=True))
    with TestClient(announced):
        assert "start" in created
        assert created[0][1] == "0.2.0"
        assert created[0][3]
    assert created[-1] == "stop"


def test_home_assistant_component_parses():
    root = Path("custom_components/campix")
    assert {path.name for path in Path("custom_components").iterdir() if path.is_dir()} == {"campix"}
    hacs = json.loads(Path("hacs.json").read_text(encoding="utf-8"))
    assert hacs["name"] == "CamPiX"
    sources = list(root.glob("*.py"))
    assert {path.name for path in sources} >= {
        "__init__.py",
        "camera.py",
        "config_flow.py",
        "coordinator.py",
        "const.py",
    }
    for path in sources:
        ast.parse(path.read_text(encoding="utf-8"))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == "campix"
    assert manifest["zeroconf"] == ["_campix._tcp.local."]
    assert manifest["config_flow"] is True
    assert manifest["issue_tracker"].endswith("/issues")
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    english = json.loads((root / "translations" / "en.json").read_text(encoding="utf-8"))
    assert "invalid_auth" in strings["config"]["error"]
    assert english == strings
    combined = "\n".join(path.read_text(encoding="utf-8") for path in sources)
    assert "stream_source" in combined
    assert "Authorization" in combined
    assert "import requests" not in combined
