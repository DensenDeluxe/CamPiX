import base64
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import psutil
import pytest


@pytest.fixture
def startup():
    path = Path(__file__).resolve().parents[1] / "scripts/install_startup.py"
    spec = importlib.util.spec_from_file_location("campix_install_startup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_occupied_api_port_is_rejected_without_service_commands(startup):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(RuntimeError, match="occupied"):
            startup.check_ports({"port": listener.getsockname()[1]})


@pytest.mark.parametrize("managed_pid", [False, 12345])
def test_foreign_http_health_is_not_enough_for_managed_service(startup, managed_pid):
    class Healthy(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok","process_id":99999}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Healthy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(RuntimeError, match="did not become live"):
            startup.wait_for_listener(server.server_port, lambda: managed_pid, timeout=0.1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_existing_user_startup_is_preserved(startup, monkeypatch, tmp_path):
    monkeypatch.setattr(startup.Path, "home", lambda: tmp_path)
    unit = tmp_path / ".config/systemd/user/campix.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("operator configuration", encoding="utf-8")
    monkeypatch.setattr(startup, "run", lambda *args: pytest.fail("Must not invoke systemd"))
    with pytest.raises(RuntimeError, match="user service exists"):
        startup.check_startup_available("linux")
    assert unit.read_text(encoding="utf-8") == "operator configuration"


def test_systemd_arguments_escape_expansion_and_reject_newlines(startup):
    assert startup.systemd_argument('/home/test/%n/"$HOME"') == '"/home/test/%%n/\\"$$HOME\\""'
    with pytest.raises(RuntimeError, match="Control characters"):
        startup.systemd_argument("/tmp/user\nExecStart=/bin/false")


def test_startup_command_failures_do_not_expose_arguments_or_output(startup, monkeypatch):
    def failure(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], output="private-value")

    monkeypatch.setattr(startup.subprocess, "Popen", failure)
    with pytest.raises(RuntimeError) as error:
        startup.run("systemctl", "private-value")
    assert "private-value" not in str(error.value)
    assert "systemctl" in str(error.value)


def test_failed_user_service_start_is_disabled_and_state_retained(startup, monkeypatch, tmp_path):
    monkeypatch.setattr(startup.Path, "home", lambda: tmp_path)
    config = tmp_path / "campix.json"
    config.write_text(json.dumps({"data_dir": str(tmp_path / "data")}), encoding="utf-8")
    monkeypatch.setattr(startup, "check_ports", lambda *args: None)
    monkeypatch.setattr(startup, "check_startup_available", lambda *args: None)
    monkeypatch.setattr(
        startup,
        "service_paths",
        lambda *args: (tmp_path / "data", ["/safe/python", "/safe/run-service.py"]),
    )
    commands = []

    def run(*command):
        commands.append(command)
        if "enable" in command:
            raise RuntimeError("Service activation failed")
        return ""

    monkeypatch.setattr(startup, "run", run)
    with pytest.raises(RuntimeError, match="activation failed"):
        startup.activate_portable(tmp_path / "campixd", config, tmp_path / "release", "linux")
    assert ("systemctl", "--user", "disable", "--now", "campix.service") in commands
    assert config.exists()
    assert (tmp_path / ".config/systemd/user/campix.service").exists()


def test_failed_task_start_disables_partial_registration(startup, monkeypatch, tmp_path):
    config = tmp_path / "campix.json"
    config.write_text(json.dumps({"data_dir": str(tmp_path / "data")}), encoding="utf-8")
    monkeypatch.setattr(startup, "check_ports", lambda *args: None)
    monkeypatch.setattr(startup, "check_startup_available", lambda *args: None)
    monkeypatch.setattr(
        startup,
        "service_paths",
        lambda *args: (tmp_path / "data", ["pythonw.exe", "run-service.py"]),
    )
    commands = []

    def run(*command):
        commands.append(command)
        if "-EncodedCommand" in command:
            raise RuntimeError("Task start failed")
        return ""

    monkeypatch.setattr(startup, "run", run)
    with pytest.raises(RuntimeError, match="Task start failed"):
        startup.activate_portable(tmp_path / "campixd", config, tmp_path / "release", "win32")
    assert any("Disable-ScheduledTask" in command[-1] for command in commands)
    assert config.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell invocation regression")
def test_helper_invocation_preserves_literals_without_changing_execution_policy(startup, tmp_path):
    helper = tmp_path / "argument probe.ps1"
    helper.write_text(
        "param([string]$PythonExecutable,[string]$ServiceArguments,[string]$WorkingDirectory,[switch]$CheckOnly)\n"
        "@{PythonExecutable=$PythonExecutable; ServiceArguments=$ServiceArguments; WorkingDirectory=$WorkingDirectory; CheckOnly=[bool]$CheckOnly} | ConvertTo-Json -Compress\n",
        encoding="utf-8",
    )
    parameters = {
        "PythonExecutable": "C:\\User's Folder\\pythonw.exe",
        "ServiceArguments": '"C:\\space folder\\run-service.py" --config "C:\\quote\'&$(throw 123)\\config.json"',
        "WorkingDirectory": "C:\\space folder\\",
        "CheckOnly": True,
    }
    command = startup.powershell_helper(helper, parameters)
    assert "-ExecutionPolicy" not in command
    assert json.loads(startup.run(*command)) == parameters
    decoded = base64.b64decode(command[-1]).decode("utf-16le")
    assert "ReadAllText" in decoded


@pytest.mark.skipif(sys.platform != "win32", reason="Windows venv/task process identity regression")
def test_task_status_identifies_venv_child_without_accepting_foreign_process(startup, tmp_path):
    helper = Path(__file__).resolve().parents[1] / "scripts/install-startup.ps1"
    shim = tmp_path / "scheduler fixture.ps1"
    quoted_helper = "'" + str(helper).replace("'", "''") + "'"
    shim.write_text(
        "param([string]$PythonExecutable,[string]$ServiceArguments,[string]$WorkingDirectory,[switch]$StatusOnly)\n"
        "function Get-ScheduledTask { [pscustomobject]@{State='Running'} }\n"
        "function Get-CimInstance {\n"
        "[pscustomobject]@{ProcessId=111;ParentProcessId=100;ExecutablePath='C:\\venv\\pythonw.exe';CommandLine='pythonw.exe service-marker'}\n"
        "[pscustomobject]@{ProcessId=222;ParentProcessId=111;ExecutablePath='C:\\base\\pythonw.exe';CommandLine='pythonw.exe service-marker'}\n"
        "[pscustomobject]@{ProcessId=333;ParentProcessId=100;ExecutablePath='C:\\other\\pythonw.exe';CommandLine='pythonw.exe service-marker'}\n"
        "}\n"
        "& ([scriptblock]::Create([IO.File]::ReadAllText("
        + quoted_helper
        + "))) @PSBoundParameters\n",
        encoding="utf-8",
    )
    command = startup.powershell_helper(
        shim,
        {
            "PythonExecutable": "C:\\venv\\pythonw.exe",
            "ServiceArguments": "service-marker",
            "WorkingDirectory": str(tmp_path),
            "StatusOnly": True,
        },
    )
    assert startup.run(*command) == "222"


def test_background_entrypoint_uses_config_and_writes_operational_log(tmp_path):
    from campix.config import Settings
    from campix.db import Database
    from campix.security import Auth

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    settings = Settings(data_dir=tmp_path / "data", port=port, min_free_mb=32)
    settings.prepare()
    db = Database(settings.data_dir / "campix.sqlite3")
    Auth(db, settings).create_user("admin", "test-only-startup-password")
    db.close()
    config = tmp_path / "campix.json"
    config.write_text(settings.model_dump_json(), encoding="utf-8")
    log = settings.data_dir / "service.log"
    wrapper = Path(__file__).resolve().parents[1] / "scripts/run-service.py"
    environment = {
        **os.environ,
        "CAMPIX_DATA_DIR": str(tmp_path / "unexpected"),
        "CAMPIX_DEMO": "true",
    }
    # Preserve the installed-package test runner's explicit import paths on Linux.
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in sys.path if path)
    process = subprocess.Popen(
        [sys.executable, str(wrapper), "--config", str(config), "--log", str(log)],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        deadline = time.monotonic() + 10
        payload = None
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with client.open(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                    payload = json.loads(response.read())
                break
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        process_ids = {
            process.pid,
            *(child.pid for child in psutil.Process(process.pid).children(recursive=True)),
        }
        assert payload and payload["process_id"] in process_ids
        assert not (tmp_path / "unexpected").exists()
        assert "Application startup complete" in log.read_text(encoding="utf-8")
        assert "test-only-startup-password" not in log.read_text(encoding="utf-8")
    finally:
        if process.poll() is None:
            for child in psutil.Process(process.pid).children(recursive=True):
                child.terminate()
            process.terminate()
        process.wait(timeout=15)
