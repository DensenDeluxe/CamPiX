import errno
import json
import os
import runpy
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from campix import cli, provision
from campix.config import Settings
from campix.db import Database
from campix.models import CameraConfig
from campix.process_lock import ProcessLock
from campix.security import HASHER, Auth


def device(source="/dev/video0", identity="usb:camera-one", backend="v4l2", model="USB camera"):
    return {
        "backend": backend,
        "source": source,
        "model": model,
        "identity": identity,
        "status": "detected-not-opened",
        "hardware_qualified": False,
    }


def inventory(*devices):
    return {"devices": list(devices), "diagnostics": [], "hardware_qualified": False}


def with_admin(settings):
    settings.prepare()
    db = Database(settings.data_dir / "campix.sqlite3")
    Auth(db, settings).create_user("maintainer", "test-only-admin-password")
    return db


def snapshot_database(db):
    return list(db.conn.iterdump())


def test_querycap_uses_device_caps_and_closes_descriptor(monkeypatch):
    capture = provision.V4L2_CAP_VIDEO_CAPTURE | provision.V4L2_CAP_STREAMING
    payload = struct.pack(
        "16s32s32s6I",
        b"uvcvideo",
        b"USB camera",
        b"usb-port-one",
        0,
        provision.V4L2_CAP_DEVICE_CAPS | capture,
        provision.V4L2_CAP_META_CAPTURE,
        0,
        0,
        0,
    )
    calls = []

    def ioctl(descriptor, request, buffer, mutable):
        calls.append((descriptor, request, mutable))
        buffer[:] = payload

    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(ioctl=ioctl))
    monkeypatch.setattr(provision.os, "O_CLOEXEC", 0, raising=False)
    monkeypatch.setattr(provision.os, "O_NONBLOCK", 0, raising=False)
    monkeypatch.setattr(provision.os, "open", lambda *args: 123)
    monkeypatch.setattr(provision.os, "close", lambda descriptor: calls.append(descriptor))
    result = provision._query_v4l2("/dev/video0")
    assert result["capabilities"] == provision.V4L2_CAP_META_CAPTURE
    assert result["driver"] == "uvcvideo"
    assert calls == [(123, provision.VIDIOC_QUERYCAP, True), 123]


def test_querycap_closes_descriptor_on_driver_error(monkeypatch):
    def ioctl(*args):
        raise OSError(errno.EIO, "Driver failure")

    closed = []
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(ioctl=ioctl))
    monkeypatch.setattr(provision.os, "O_CLOEXEC", 0, raising=False)
    monkeypatch.setattr(provision.os, "O_NONBLOCK", 0, raising=False)
    monkeypatch.setattr(provision.os, "open", lambda *args: 123)
    monkeypatch.setattr(provision.os, "close", closed.append)
    with pytest.raises(OSError):
        provision._query_v4l2("/dev/video0")
    assert closed == [123]


def test_inventory_deduplicates_usb_and_csi_and_rejects_non_camera_nodes(tmp_path, monkeypatch):
    monkeypatch.setattr(provision.sys, "platform", "linux")
    capture = provision.V4L2_CAP_VIDEO_CAPTURE | provision.V4L2_CAP_STREAMING
    flags = [
        capture,
        provision.V4L2_CAP_META_CAPTURE | provision.V4L2_CAP_STREAMING,
        capture,
        capture,
        capture | provision.V4L2_CAP_VIDEO_M2M,
        capture,
        capture | provision.V4L2_CAP_VIDEO_OUTPUT,
        provision.V4L2_CAP_VIDEO_CAPTURE,
    ]
    for index in range(len(flags)):
        (tmp_path / f"video{index}").mkdir()

    def query(source):
        index = int(source.removeprefix("/dev/video"))
        return {
            "capabilities": flags[index],
            "model": f"Camera {index}",
            "driver": "uvcvideo" if index != 5 else "bcm2835-isp",
            "bus": f"usb-{0 if index == 2 else index}" if index != 5 else "platform:isp",
        }

    monkeypatch.setattr(provision, "_query_v4l2", query)
    monkeypatch.setattr(
        provision,
        "_picamera_info",
        lambda: [
            {"Num": 0, "Model": "imx219", "Id": "/base/i2c@0/imx219@10"},
            {"Num": 1, "Model": "imx708", "Id": "/base/i2c@1/imx708@1a"},
            {"Num": 2, "Model": "imx219 duplicate", "Id": "/base/i2c@0/imx219@10"},
            {"Num": 3, "Model": "USB camera", "Id": "/base/usb@0/1-0"},
            {"Num": 4, "Model": "USB camera", "Id": "\\_SB_.PCI0.XHC_.RHUB.HS01-1:1.0-046d:0825"},
        ],
    )
    result = provision.local_inventory(tmp_path)
    assert [(item["backend"], item["source"]) for item in result["devices"]] == [
        ("v4l2", "/dev/video0"),
        ("v4l2", "/dev/video3"),
        ("picamera2", "0"),
        ("picamera2", "1"),
    ]
    assert result["hardware_qualified"] is False
    assert result["devices"][0]["aliases"] == ["/dev/video0", "/dev/video2"]
    assert result["devices"][2]["aliases"] == ["0", "2"]
    assert all(item["hardware_qualified"] is False for item in result["devices"])
    assert result["diagnostics"][0]["code"] == "platform-node-not-registered"


def test_usb_sysfs_identity_groups_different_interfaces(tmp_path):
    physical = tmp_path / "usb1" / "1-1"
    physical.mkdir(parents=True)
    (physical / "idVendor").write_text("046d")
    (physical / "idProduct").write_text("0825")
    first = physical / "interface-one" / "video4linux" / "video0"
    second = physical / "interface-two" / "video4linux" / "video2"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    # The resolved device path may point into the interface rather than the USB device.
    assert provision._usb_identity(first, {"bus": "usb-first"}) == provision._usb_identity(
        second, {"bus": "usb-second"}
    )


def test_inventory_reports_permissions_and_libcamera_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(provision.sys, "platform", "linux")
    (tmp_path / "video0").mkdir()

    def query(source):
        raise PermissionError(errno.EACCES, "Permission denied")

    def picamera():
        raise RuntimeError("private untrusted driver details")

    monkeypatch.setattr(provision, "_query_v4l2", query)
    monkeypatch.setattr(provision, "_picamera_info", picamera)
    result = provision.local_inventory(tmp_path)
    assert result["devices"] == []
    assert [item["code"] for item in result["diagnostics"]] == [
        "v4l2-query-failed",
        "picamera2-enumeration-failed",
    ]
    assert "private untrusted" not in json.dumps(result)


def test_discovery_timeout_returns_diagnostic_and_removes_private_staging(monkeypatch):
    output_paths = []

    def run(command, **kwargs):
        output_paths.append(command[command.index("--inventory-worker") + 1])
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(provision.subprocess, "run", run)
    result = provision.discover_inventory(timeout=0.1)
    assert result["devices"] == []
    assert result["diagnostics"][0]["code"] == "discovery-timeout"
    assert not Path(output_paths[0]).parent.exists()


def test_discovery_worker_refuses_native_enumeration_without_child_containment(
    tmp_path, monkeypatch
):
    output = tmp_path / "inventory.json"
    calls = []

    def refused():
        calls.append("contain")
        raise OSError("test-only child containment refusal")

    def forbid_enumeration(frame, event, arg):
        if event == "call" and frame.f_code.co_name == "local_inventory":
            pytest.fail("Native discovery ran without child containment")
        return forbid_enumeration

    monkeypatch.setattr("campix.processes.contain_children", refused)
    monkeypatch.setattr(sys, "argv", ["campix.provision", "--inventory-worker", str(output), "unused"])
    previous_trace = sys.gettrace()
    try:
        sys.settrace(forbid_enumeration)
        with pytest.warns(RuntimeWarning, match="found in sys.modules"), pytest.raises(
            OSError, match="child containment refusal"
        ):
            runpy.run_module("campix.provision", run_name="__main__")
    finally:
        sys.settrace(previous_trace)
    assert calls == ["contain"]
    assert not output.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows discovery child containment")
def test_terminated_discovery_worker_stops_its_native_listing_child(tmp_path):
    worker = (
        "import runpy, subprocess, sys, time\n"
        "from campix.processes import NO_WINDOW\n"
        "def stall_native_listing(frame, event, arg):\n"
        "    if event == 'call' and frame.f_code.co_name == 'local_inventory':\n"
        "        sys.settrace(None)\n"
        "        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "                                 creationflags=NO_WINDOW)\n"
        "        print(child.pid, flush=True)\n"
        "        time.sleep(30)\n"
        "    return stall_native_listing\n"
        "sys.argv = ['campix.provision', '--inventory-worker', sys.argv[1], 'unused']\n"
        "sys.settrace(stall_native_listing)\n"
        "runpy.run_module('campix.provision', run_name='__main__')\n"
    )
    # Stall the real worker at the native inventory boundary, without opening hardware.
    process = subprocess.Popen(
        [sys.executable, "-c", worker, str(tmp_path / "inventory.json")],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(path for path in sys.path if path)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
        creationflags=provision.NO_WINDOW,
    )
    try:
        child = psutil.Process(int(process.stdout.readline()))
    finally:
        # subprocess.run uses the same abrupt termination on discovery timeout.
        process.kill()
        process.wait(timeout=5)
        process.stdout.close()
    try:
        child.wait(timeout=1)
    except psutil.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)
        pytest.fail("A native listing child outlived its terminated discovery worker")


def test_discovery_rejects_unbounded_worker_result(monkeypatch):
    def run(command, **kwargs):
        output = command[command.index("--inventory-worker") + 1]
        Path(output).write_bytes(b" " * (provision.INVENTORY_BYTES + 1))

    monkeypatch.setattr(provision.subprocess, "run", run)
    result = provision.discover_inventory()
    assert result["devices"] == []
    assert result["diagnostics"][0]["code"] == "discovery-failed"


def test_discovery_worker_preserves_native_wrapper_import_paths(tmp_path, monkeypatch):
    package = tmp_path / "native-prefix" / "campix"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "provision.py").write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[2]).write_text(json.dumps({"
        "'devices': [], 'diagnostics': [], 'hardware_qualified': False, "
        "'native_prefix': __file__}))\n"
    )
    # Native /usr/bin/campixd adds application/vendor paths not present in the
    # system interpreter's default module search path.
    monkeypatch.syspath_prepend(str(package.parent))
    result = provision.discover_inventory()
    assert result["native_prefix"] == str(package / "provision.py")


def test_noninteractive_without_admin_makes_no_state_changes(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "untouched")
    monkeypatch.setattr(
        provision,
        "discover_inventory",
        lambda **_: pytest.fail("No discovery before admin readiness"),
    )
    with pytest.raises(provision.ProvisionError, match="No enabled administrator"):
        provision.setup(settings, non_interactive=True)
    assert not settings.data_dir.exists()


def test_invalid_password_makes_no_state_changes(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "untouched")
    monkeypatch.setattr(
        provision,
        "discover_inventory",
        lambda **_: pytest.fail("No discovery before admin readiness"),
    )
    with pytest.raises(provision.ProvisionError, match="Invalid username/password"):
        provision.setup(settings, username="maintainer", password_reader=lambda: "short")
    assert not settings.data_dir.exists()


def test_setup_creates_secure_admin_and_multiple_conservative_cameras(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    devices = inventory(device(), device("1", "csi:i2c-camera", "picamera2", "imx708"))
    monkeypatch.setattr(provision, "discover_inventory", lambda **_: devices)
    result = provision.setup(
        settings, username="maintainer", password_reader=lambda: "test-only-setup-password"
    )
    assert result["admin_created"] is True
    assert result["hardware_qualified"] is False
    db = Database(settings.data_dir / "campix.sqlite3")
    try:
        admin = db.one("SELECT * FROM users")
        assert admin["role"] == "admin" and admin["disabled"] == 0
        assert HASHER.verify(admin["password"], "test-only-setup-password")
        assert admin["password"] not in json.dumps(result)
        cameras = [json.loads(row["config"]) for row in db.all("SELECT config FROM cameras")]
        assert len(cameras) == 2
        assert all(
            (camera["width"], camera["height"], camera["fps"]) == (640, 480, 10)
            for camera in cameras
        )
        assert all(camera["encoder"] == "auto" for camera in cameras)
    finally:
        db.close()


def test_setup_preserves_existing_accounts_profiles_settings_media_and_is_idempotent(
    tmp_path, monkeypatch
):
    settings = Settings(data_dir=tmp_path / "data")
    db = with_admin(settings)
    existing = CameraConfig(
        id="custom",
        name="Configured camera",
        backend="v4l2",
        source="/dev/video0",
        width=1920,
        height=1080,
        fps=25,
        controls={"brightness": 0.4},
    )
    db.execute("INSERT INTO cameras VALUES (?,?)", (existing.id, existing.model_dump_json()))
    db.set_setting("custom", {"keep": True})
    db.execute(
        "INSERT INTO media(id,camera_id,kind,filename,created,size,trigger) VALUES (?,?,?,?,?,?,?)",
        ("retained", "custom", "snapshot", "retained.jpg", 1, 7, "manual"),
    )
    before_accounts = db.all("SELECT * FROM users")
    before_config = db.one("SELECT config FROM cameras WHERE id='custom'")
    monkeypatch.setattr(
        provision,
        "discover_inventory",
        lambda **_: inventory(device(), device("/dev/video3", "usb:second")),
    )
    result = provision.setup(
        settings,
        non_interactive=True,
        password_reader=lambda: pytest.fail("Existing admin must not prompt"),
    )
    assert result["admin_created"] is False
    assert len(result["cameras_added"]) == 1
    assert result["cameras_skipped"] == [{"source": "/dev/video0", "reason": "already-configured"}]
    assert db.all("SELECT * FROM users") == before_accounts
    assert db.one("SELECT config FROM cameras WHERE id='custom'") == before_config
    assert db.setting("custom") == {"keep": True}
    assert db.one("SELECT filename FROM media")["filename"] == "retained.jpg"
    after = snapshot_database(db)
    assert provision.setup(settings, non_interactive=True)["cameras_added"] == []
    assert snapshot_database(db) == after
    db.close()


def test_setup_camera_limit_preserves_existing_and_never_adds_synthetic(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", max_cameras=1, demo=True)
    db = with_admin(settings)
    camera = CameraConfig(
        id="network", name="Existing network", backend="network", source="rtsp://127.0.0.1/live"
    )
    db.execute("INSERT INTO cameras VALUES (?,?)", (camera.id, camera.model_dump_json()))
    before = snapshot_database(db)
    monkeypatch.setattr(provision, "discover_inventory", lambda **_: inventory(device()))
    result = provision.setup(settings, non_interactive=True)
    assert result["cameras_added"] == []
    assert result["cameras_skipped"][0]["reason"] == "camera-limit"
    assert snapshot_database(db) == before
    db.close()


def test_setup_preserves_existing_secondary_usb_node_without_duplicate_owner(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    db = with_admin(settings)
    camera = CameraConfig(
        id="manual", name="Secondary capture node", backend="v4l2", source="/dev/video2"
    )
    db.execute("INSERT INTO cameras VALUES (?,?)", (camera.id, camera.model_dump_json()))
    before = snapshot_database(db)
    detected = device()
    detected["aliases"] = ["/dev/video0", "/dev/video2"]
    monkeypatch.setattr(provision, "discover_inventory", lambda **_: inventory(detected))
    result = provision.setup(settings, non_interactive=True)
    assert result["cameras_added"] == []
    assert result["cameras_skipped"][0]["reason"] == "already-configured"
    assert snapshot_database(db) == before
    db.close()


def test_empty_inventory_never_invents_a_demo_camera(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", demo=True)
    db = with_admin(settings)
    monkeypatch.setattr(provision, "discover_inventory", lambda **_: inventory())
    result = provision.setup(settings, non_interactive=True)
    assert result["cameras_added"] == [] and result["devices"] == []
    assert db.all("SELECT * FROM cameras") == []
    db.close()


def test_setup_excludes_running_service_without_database_mutation(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    db = with_admin(settings)
    before = snapshot_database(db)
    lock = ProcessLock(settings.data_dir)
    lock.acquire()
    monkeypatch.setattr(provision, "discover_inventory", lambda **_: inventory(device()))
    try:
        with pytest.raises(provision.ProvisionError, match="Stop the CamPiX service"):
            provision.setup(settings, non_interactive=True)
        assert snapshot_database(db) == before
    finally:
        lock.close()
        db.close()


def test_setup_rolls_back_admin_and_camera_inserts_on_invalid_inventory(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    monkeypatch.setattr(
        provision,
        "discover_inventory",
        lambda **_: inventory(device(), device("0", "fake:synthetic", "synthetic")),
    )
    with pytest.raises(provision.ProvisionError, match="unsupported local camera"):
        provision.setup(
            settings, username="maintainer", password_reader=lambda: "test-only-setup-password"
        )
    db = Database(settings.data_dir / "campix.sqlite3")
    assert db.all("SELECT * FROM users") == []
    assert db.all("SELECT * FROM cameras") == []
    db.close()
    # A failed transaction must release the service lock as well.
    lock = ProcessLock(settings.data_dir)
    lock.acquire()
    lock.close()


def test_setup_rejects_occupied_admin_name_without_changing_account(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    settings.prepare()
    db = Database(settings.data_dir / "campix.sqlite3")
    Auth(db, settings).create_user("maintainer", "test-only-viewer-password", "viewer")
    before = snapshot_database(db)
    with pytest.raises(provision.ProvisionError, match="username already exists"):
        provision.setup(
            settings,
            username="maintainer",
            password_reader=lambda: pytest.fail("Must not prompt"),
        )
    assert snapshot_database(db) == before
    db.close()


def test_setup_refuses_future_schema_before_discovery(tmp_path, monkeypatch):
    path = tmp_path / "campix.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=99")
    monkeypatch.setattr(
        provision, "discover_inventory", lambda **_: pytest.fail("Must reject before discovery")
    )
    with pytest.raises(provision.ProvisionError, match="downgrade refused"):
        provision.setup(Settings(data_dir=tmp_path), non_interactive=True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 99


def test_setup_cli_json_uses_config_and_preserves_existing_admin(tmp_path, monkeypatch, capsys):
    settings = Settings(data_dir=tmp_path / "data")
    db = with_admin(settings)
    db.close()
    config = tmp_path / "settings.json"
    config.write_text(settings.model_dump_json())
    monkeypatch.setattr(provision, "discover_inventory", lambda **_: inventory(device()))
    monkeypatch.setattr(
        sys, "argv", ["campixd", "--config", str(config), "setup", "--non-interactive", "--json"]
    )
    cli.main()
    output = json.loads(capsys.readouterr().out)
    assert len(output["cameras_added"]) == 1
    assert output["admin_created"] is False
    assert output["hardware_qualified"] is False


def test_setup_cli_noninteractive_no_admin_fails_without_creating_state(tmp_path):
    data_dir = tmp_path / "untouched"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "campix",
            "--data-dir",
            str(data_dir),
            "setup",
            "--non-interactive",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert "No enabled administrator" in result.stderr
    assert not data_dir.exists()


@pytest.mark.parametrize("name", ["admin", "Administrator", "ROOT"])
def test_first_admin_rejects_predictable_names_without_state_changes(tmp_path, monkeypatch, name):
    settings = Settings(data_dir=tmp_path / "untouched")
    monkeypatch.setattr(
        provision, "discover_inventory", lambda **_: pytest.fail("No discovery before admin")
    )
    with pytest.raises(provision.ProvisionError, match="predictable account name"):
        provision.setup(
            settings, username=name, password_reader=lambda: pytest.fail("Must not prompt")
        )
    assert not settings.data_dir.exists()


def test_first_admin_username_is_read_before_the_password(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    monkeypatch.setattr(provision, "discover_inventory", lambda **_: inventory())
    order = []

    def username():
        order.append("username")
        return "camera.keeper"

    def password():
        order.append("password")
        return "test-only-setup-password"

    result = provision.setup(settings, username_reader=username, password_reader=password)
    assert result["admin_created"] is True and order == ["username", "password"]
    db = Database(settings.data_dir / "campix.sqlite3")
    try:
        admin = db.one("SELECT username, role FROM users")
        assert (admin["username"], admin["role"]) == ("camera.keeper", "admin")
    finally:
        db.close()


def test_setup_cli_prompts_again_after_typing_mistakes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(provision, "discover_inventory", lambda **_: inventory())
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    names = iter(["admin", "bad name", "camera.keeper"])
    secrets = iter(
        [
            "short",
            "test-only-setup-password",
            "test-only-typo-password",
            "test-only-setup-password",
            "test-only-setup-password",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt: next(names))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(secrets))
    data = tmp_path / "data"
    monkeypatch.setattr(sys, "argv", ["campixd", "--data-dir", str(data), "setup"])
    cli.main()
    output = capsys.readouterr()
    assert "Administrator created." in output.out
    assert "predictable account name" in output.err
    assert "letters, digits" in output.err
    assert "12-128 characters" in output.err and "do not match" in output.err
    assert "test-only" not in output.out + output.err
    db = Database(data / "campix.sqlite3")
    try:
        assert db.one("SELECT username FROM users")["username"] == "camera.keeper"
    finally:
        db.close()
