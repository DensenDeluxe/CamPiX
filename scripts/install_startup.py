"""Activate a portable installation as the current, unprivileged user."""

from __future__ import annotations

import base64
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def run(*command: str) -> str:
    output = bytearray()
    exceeded = threading.Event()
    try:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:

            def collect():
                while chunk := process.stdout.read(8192):
                    if len(output) + len(chunk) > 65536:
                        exceeded.set()
                        process.kill()
                        break
                    output.extend(chunk)

            reader = threading.Thread(target=collect, daemon=True)
            reader.start()
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                raise
            reader.join(timeout=10)
            if process.returncode or reader.is_alive() or exceeded.is_set():
                raise RuntimeError(f"Startup command failed: {Path(command[0]).name}")
    except (OSError, subprocess.SubprocessError) as exc:
        # Do not echo supplied arguments or subprocess output; they can contain private paths.
        raise RuntimeError(f"Startup command failed: {Path(command[0]).name}") from exc
    return output.decode("utf-8", errors="replace").strip()


def check_ports(config: dict) -> None:
    ports = [config.get("port", 8080)]
    if config.get("mediamtx_binary"):
        ports += [8554, 8888, 8889, 9997, 9998, 8189]
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                listener.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(f"Loopback port {port} is occupied; startup refused") from exc


def systemd_argument(value: str) -> str:
    if any(ord(character) < 32 for character in value):
        raise RuntimeError("Control characters are not supported in startup paths")
    return (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        + '"'
    )


def service_paths(executable: Path, config: Path, release: Path) -> tuple[Path, list[str]]:
    settings = json.loads(config.read_text(encoding="utf-8"))
    data = Path(settings["data_dir"]).resolve()
    python = executable.with_name("pythonw.exe" if sys.platform == "win32" else "python")
    wrapper = release / "scripts" / "run-service.py"
    if not python.is_file() or not wrapper.is_file():
        raise RuntimeError("Portable service Python or logging entry point is missing")
    return data, [
        str(python),
        str(wrapper),
        "--config",
        str(config),
        "--log",
        str(data / "service.log"),
    ]


def powershell_helper(helper: Path, parameters: dict[str, str | bool]) -> tuple[str, ...]:
    """Invoke reviewed local code without changing the host's script execution policy."""

    def literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    entries = []
    for name, value in parameters.items():
        if not re.fullmatch(r"[A-Za-z]+", name):
            raise RuntimeError("Invalid startup helper parameter")
        encoded = ("$true" if value else "$false") if isinstance(value, bool) else literal(value)
        entries.append(name + "=" + encoded)
    script = (
        "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; $parameters=@{"
        + ";".join(entries)
        + "}; "
        "& ([scriptblock]::Create([IO.File]::ReadAllText("
        + literal(str(helper))
        + "))) @parameters"
    )
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return "powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script


def check_startup_available(system: str | None = None) -> None:
    """Reject existing startup definitions or unavailable user managers before installation."""
    system = system or sys.platform
    if system == "linux":
        unit = Path.home() / ".config/systemd/user/campix.service"
        if unit.exists() or unit.is_symlink():
            raise RuntimeError(
                "A CamPiX user service exists; back up and follow the update procedure"
            )
        if not shutil.which("systemctl"):
            raise RuntimeError(
                "A working systemd user manager is required for portable Linux startup"
            )
        run("systemctl", "--user", "show-environment")
        # A system/global user unit must not be shadowed either.
        if (
            run("systemctl", "--user", "show", "campix.service", "--property=LoadState", "--value")
            != "not-found"
        ):
            raise RuntimeError("A CamPiX user service is already installed")
    elif system == "darwin":
        target = Path.home() / "Library/LaunchAgents/org.campix.service.plist"
        if target.exists() or target.is_symlink():
            raise RuntimeError(
                "A CamPiX LaunchAgent exists; back up and follow the update procedure"
            )
        run("launchctl", "print", f"gui/{os.getuid()}")
    elif system == "win32":
        helper = Path(__file__).with_name("install-startup.ps1")
        run(
            *powershell_helper(
                helper,
                {
                    "PythonExecutable": sys.executable,
                    "ServiceArguments": "check",
                    "WorkingDirectory": str(Path.home()),
                    "CheckOnly": True,
                },
            )
        )
    else:
        raise RuntimeError("No portable startup profile for this operating system")


def wait_for_listener(port: int, running, *, timeout: float = 20) -> None:
    """Require managed process state as well as HTTP liveness; no camera-readiness claim."""
    deadline = time.monotonic() + timeout
    # Ignore ambient proxy settings for this strictly local request.
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        expected_pid = running()
        if (
            isinstance(expected_pid, int)
            and not isinstance(expected_pid, bool)
            and expected_pid > 0
        ):
            try:
                with client.open(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                    payload = json.loads(response.read(16385))
                    if (
                        response.status == 200
                        and payload.get("status") == "ok"
                        and payload.get("process_id") == expected_pid
                    ):
                        return
            except (OSError, urllib.error.URLError, ValueError, AttributeError):
                pass
        time.sleep(0.25)
    raise RuntimeError(
        "Managed service did not become live; inspect service.log or the user service journal"
    )


def activate_portable(
    executable: Path, config: Path, release: Path, system: str | None = None
) -> dict:
    system = system or sys.platform
    settings = json.loads(config.read_text(encoding="utf-8"))
    if settings.get("host", "127.0.0.1") != "127.0.0.1":
        raise RuntimeError("Initial startup requires the loopback listener")
    check_ports(settings)
    check_startup_available(system)
    data, command = service_paths(executable, config, release)
    if system == "linux":
        unit = Path.home() / ".config/systemd/user/campix.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "[Unit]\nDescription=CamPiX camera service\n"
            "After=network-online.target\n\n[Service]\nType=simple\n"
            "ExecStart=" + " ".join(systemd_argument(arg) for arg in command) + "\n"
            "WorkingDirectory=" + systemd_argument(str(data)) + "\n"
            "UMask=0077\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=90\n"
            "KillMode=control-group\nNoNewPrivileges=yes\n\n[Install]\nWantedBy=default.target\n"
        )
        with unit.open("x", encoding="utf-8") as stream:
            stream.write(content)
        run("systemctl", "--user", "daemon-reload")
        try:
            run("systemctl", "--user", "enable", "--now", "campix.service")
            wait_for_listener(
                settings.get("port", 8080),
                lambda: int(
                    run(
                        "systemctl",
                        "--user",
                        "show",
                        "campix.service",
                        "--property=MainPID",
                        "--value",
                    )
                    or "0"
                ),
            )
        except RuntimeError:
            run("systemctl", "--user", "disable", "--now", "campix.service")
            raise
        return {"startup": "systemd user service", "service": str(unit), "starts": "at user login"}
    if system == "darwin":
        target = Path.home() / "Library/LaunchAgents/org.campix.service.plist"
        target.parent.mkdir(parents=True, exist_ok=True)
        domain = f"gui/{os.getuid()}"
        label = "org.campix.service"
        with target.open("xb") as stream:
            plistlib.dump(
                {
                    "Label": label,
                    "ProgramArguments": command,
                    "WorkingDirectory": str(data),
                    "RunAtLoad": True,
                    "KeepAlive": {"SuccessfulExit": False},
                    "ThrottleInterval": 5,
                    "ProcessType": "Background",
                    "StandardOutPath": "/dev/null",
                    "StandardErrorPath": "/dev/null",
                    "Umask": 0o077,
                },
                stream,
            )
        try:
            run("launchctl", "bootstrap", domain, str(target))

            def launch_pid():
                status = run("launchctl", "print", f"{domain}/{label}")
                match = re.search(r"^\s*pid = (\d+)\s*$", status, re.MULTILINE)
                return int(match[1]) if match else 0

            wait_for_listener(settings.get("port", 8080), launch_pid)
        except RuntimeError:
            run("launchctl", "bootout", f"{domain}/{label}")
            raise
        return {"startup": "LaunchAgent", "service": str(target), "starts": "at user login"}
    helper = Path(__file__).with_name("install-startup.ps1")
    parameters = {
        "PythonExecutable": command[0],
        "ServiceArguments": subprocess.list2cmdline(command[1:]),
        "WorkingDirectory": str(data),
    }
    try:
        run(*powershell_helper(helper, parameters))
        wait_for_listener(
            settings.get("port", 8080),
            lambda: int(run(*powershell_helper(helper, {**parameters, "StatusOnly": True})) or "0"),
        )
    except RuntimeError:
        try:
            run(
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Disable-ScheduledTask -TaskName 'CamPiX' | Out-Null; Stop-ScheduledTask -TaskName 'CamPiX'",
            )
        except RuntimeError:
            pass
        raise
    return {
        "startup": "limited user scheduled task",
        "service": "CamPiX",
        "starts": "at user login",
    }
