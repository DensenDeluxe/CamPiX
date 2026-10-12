import ast
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from campix import cli, mediamtx, processes
from campix.config import Settings
from campix.db import Database
from campix.security import Auth

SOURCE = Path(__file__).resolve().parents[1] / "src" / "campix"
LAUNCHERS = {"Popen", "run", "call", "check_call", "check_output"}


def test_every_service_child_process_starts_without_a_console_window():
    launched, unguarded = set(), []
    for path in sorted(SOURCE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in LAUNCHERS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
            ):
                continue
            launched.add(path.name)
            flags = [keyword.value for keyword in node.keywords if keyword.arg == "creationflags"]
            if not (flags and isinstance(flags[0], ast.Name) and flags[0].id == "NO_WINDOW"):
                unguarded.append(f"{path.name}:{node.lineno}")
    # A windowless service would open one console window per unguarded child.
    assert {"capture.py", "exports.py", "mediamtx.py", "provision.py", "recording.py"} <= launched
    assert unguarded == []


def test_managed_mediamtx_starts_without_a_console_window(tmp_path, monkeypatch):
    calls = []

    def version(command, **kwargs):
        calls.append(("version", kwargs.get("creationflags")))
        return SimpleNamespace(stdout=f"v{mediamtx.MEDIAMTX_VERSION}\n")

    class Server:
        def __init__(self, command, **kwargs):
            calls.append(("server", kwargs.get("creationflags")))

        def poll(self):
            return None

    def free_port(*args, **kwargs):
        raise OSError("port is free")

    # The host may run its own CamPiX MediaMTX on the fixed managed ports.
    monkeypatch.setattr(mediamtx.socket, "create_connection", free_port)
    monkeypatch.setattr(mediamtx.subprocess, "run", version)
    monkeypatch.setattr(mediamtx.subprocess, "Popen", Server)
    binary = tmp_path / "mediamtx.exe"
    binary.write_bytes(b"")
    plane = mediamtx.MediaPlane(
        Settings(data_dir=tmp_path, mediamtx_binary=str(binary)),
        None,
        SimpleNamespace(publish=lambda *args, **kwargs: None),
    )
    plane._launch()
    assert calls == [("version", processes.NO_WINDOW), ("server", processes.NO_WINDOW)]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console allocation")
def test_windows_console_child_receives_no_console_window():
    assert processes.NO_WINDOW == subprocess.CREATE_NO_WINDOW
    result = subprocess.run(
        [sys.executable, "-c", "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        creationflags=processes.NO_WINDOW,
    )
    assert result.stdout.strip() == "0"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_windows_children_do_not_outlive_an_abruptly_terminated_service():
    service = (
        "import subprocess, sys, time\n"
        "from campix.processes import NO_WINDOW, contain_children\n"
        "assert contain_children()\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " creationflags=NO_WINDOW)\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", service],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(path for path in sys.path if path)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
        creationflags=processes.NO_WINDOW,
    )
    try:
        child = psutil.Process(int(parent.stdout.readline()))
    finally:
        # TerminateProcess: no Python cleanup or application shutdown runs.
        parent.kill()
        parent.wait(timeout=10)
        parent.stdout.close()
    try:
        child.wait(timeout=10)
    except psutil.TimeoutExpired:
        child.kill()
        pytest.fail("A hidden service child outlived its terminated parent")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX service managers own cleanup")
def test_child_containment_is_left_to_posix_service_managers():
    assert processes.contain_children() is False


def serve(tmp_path, monkeypatch, contain=lambda: True):
    """Run `campixd serve` up to the listener, which is replaced by a recorder."""
    settings = Settings(data_dir=tmp_path / "data", min_free_mb=32)
    settings.prepare()
    db = Database(settings.data_dir / "campix.sqlite3")
    Auth(db, settings).create_user("admin", "test-only-admin-password")
    db.close()
    events = []

    def contain_children():
        events.append("contain")
        return contain()

    monkeypatch.setattr(processes, "contain_children", contain_children)
    monkeypatch.setattr("campix.app.create_app", lambda settings: "app")
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: events.append("serve"))
    monkeypatch.setattr(sys, "argv", ["campixd", "--data-dir", str(settings.data_dir), "serve"])
    cli.main()
    return events


@pytest.mark.parametrize("available", [True, False])
def test_serve_contains_children_before_listening(tmp_path, monkeypatch, caplog, available):
    def contain():
        if not available:
            raise OSError(13, "Access is denied", None, 5)
        return True

    with caplog.at_level(logging.WARNING, logger="campix.service"):
        events = serve(tmp_path, monkeypatch, contain)
    # A refused job weakens crash cleanup only; it must be visible, not fatal.
    assert events == ["contain", "serve"]
    assert ("containment is unavailable" in caplog.text) == (not available)


def test_serve_logs_media_plane_polling_only_on_problems(tmp_path, monkeypatch, caplog):
    client_log = logging.getLogger("httpx")
    monkeypatch.setattr(client_log, "level", logging.NOTSET)
    serve(tmp_path, monkeypatch)
    with caplog.at_level(logging.INFO):
        client_log.info('HTTP Request: GET http://127.0.0.1:9997/v3/paths/list "HTTP/1.1 200 OK"')
        client_log.warning("MediaMTX API request failed")
    # One INFO line every two seconds would rotate real warnings out of service.log.
    assert "paths/list" not in caplog.text
    assert "MediaMTX API request failed" in caplog.text
