"""Activate and control a portable installation as the current, unprivileged user."""

from __future__ import annotations

import base64
import json
import os
import plistlib
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

TASK = "CamPiX"
LABEL = "org.campix.service"


def systemd_unit() -> Path:
    return Path.home() / ".config/systemd/user/campix.service"


def launch_agent() -> Path:
    return Path.home() / "Library/LaunchAgents/org.campix.service.plist"


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
        unit = systemd_unit()
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
        target = launch_agent()
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


def windows_task_parameters(executable: Path, config: Path, release: Path) -> dict[str, str]:
    data, command = service_paths(executable, config, release)
    return {
        "PythonExecutable": command[0],
        "ServiceArguments": subprocess.list2cmdline(command[1:]),
        "WorkingDirectory": str(data),
    }


def portable_pid(executable: Path, config: Path, release: Path, system: str | None = None) -> int:
    """Return the managed service process ID, or 0 when the service is not running."""
    system = system or sys.platform
    if system == "linux":
        return int(
            run("systemctl", "--user", "show", "campix.service", "--property=MainPID", "--value")
            or "0"
        )
    if system == "darwin":
        try:
            status = run("launchctl", "print", f"gui/{os.getuid()}/{LABEL}")
        except RuntimeError:
            return 0  # The agent is not loaded.
        match = re.search(r"^\s*pid = (\d+)\s*$", status, re.MULTILINE)
        return int(match[1]) if match else 0
    helper = Path(__file__).with_name("install-startup.ps1")
    parameters = {**windows_task_parameters(executable, config, release), "StatusOnly": True}
    return int(run(*powershell_helper(helper, parameters)) or "0")


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
    port = settings.get("port", 8080)

    def running():
        return portable_pid(executable, config, release, system)

    if system == "linux":
        unit = systemd_unit()
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
            wait_for_listener(port, running)
        except RuntimeError:
            run("systemctl", "--user", "disable", "--now", "campix.service")
            raise
        return {"startup": "systemd user service", "service": str(unit), "starts": "at user login"}
    if system == "darwin":
        target = launch_agent()
        target.parent.mkdir(parents=True, exist_ok=True)
        domain = f"gui/{os.getuid()}"
        with target.open("xb") as stream:
            plistlib.dump(
                {
                    "Label": LABEL,
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
            wait_for_listener(port, running)
        except RuntimeError:
            run("launchctl", "bootout", f"{domain}/{LABEL}")
            raise
        return {"startup": "LaunchAgent", "service": str(target), "starts": "at user login"}
    helper = Path(__file__).with_name("install-startup.ps1")
    try:
        run(*powershell_helper(helper, windows_task_parameters(executable, config, release)))
        wait_for_listener(port, running)
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
        "service": TASK,
        "starts": "at user login",
    }


def powershell(script: str) -> str:
    """Run a fixed PowerShell script; callers never interpolate paths or user input."""
    return run("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script)


def if_task(action: str) -> str:
    """Script an action on the CamPiX task; a missing task is not an error."""
    # A failed lookup leaves $? false, which `powershell -Command` reports as exit code 1
    # despite SilentlyContinue. Failures inside the action still stop the script.
    return (
        "$ErrorActionPreference = 'Stop'; "
        f"$task = Get-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue; "
        f"if ($task) {{ {action} }}; exit 0"
    )


def start_portable(
    executable: Path, config: Path, release: Path, system: str | None = None
) -> bool:
    """Start the registered service and wait for it; False when it was already running."""
    system = system or sys.platform
    settings = json.loads(config.read_text(encoding="utf-8"))

    def running():
        return portable_pid(executable, config, release, system)

    if running() > 0:
        return False
    if system == "linux":
        run("systemctl", "--user", "start", "campix.service")
    elif system == "darwin":
        domain = f"gui/{os.getuid()}"
        try:
            run("launchctl", "print", f"{domain}/{LABEL}")
            run("launchctl", "kickstart", f"{domain}/{LABEL}")
        except RuntimeError:
            run("launchctl", "bootstrap", domain, str(launch_agent()))
    else:
        powershell(f"Start-ScheduledTask -TaskName '{TASK}'")
    wait_for_listener(settings.get("port", 8080), running, timeout=45)
    return True


def stop_portable(system: str | None = None) -> None:
    """Stop the service until the next start or login; the definition stays registered."""
    system = system or sys.platform
    if system == "linux":
        run("systemctl", "--user", "stop", "campix.service")
    elif system == "darwin":
        try:
            run("launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}")
        except RuntimeError:
            pass  # Not loaded, so not running.
    else:
        powershell(if_task(f"Stop-ScheduledTask -TaskName '{TASK}'"))


def remove_portable(system: str | None = None) -> None:
    """Stop the service and delete its startup definition; a missing one is not an error."""
    system = system or sys.platform
    if system == "linux":
        unit = systemd_unit()
        if unit.exists() or unit.is_symlink():
            run("systemctl", "--user", "disable", "--now", "campix.service")
            unit.unlink()
            run("systemctl", "--user", "daemon-reload")
    elif system == "darwin":
        stop_portable(system)
        launch_agent().unlink(missing_ok=True)
    else:
        powershell(
            if_task(
                f"Stop-ScheduledTask -TaskName '{TASK}'; "
                f"Unregister-ScheduledTask -TaskName '{TASK}' -Confirm:$false"
            )
        )


def startup_command(system: str | None = None) -> str | None:
    """Return the registered service command line, or None without a definition."""
    system = system or sys.platform
    if system == "linux":
        unit = systemd_unit()
        if not unit.is_file():
            return None
        lines = unit.read_text(encoding="utf-8", errors="replace").splitlines()
        return next((line for line in lines if line.startswith("ExecStart=")), "")
    if system == "darwin":
        target = launch_agent()
        if not target.is_file():
            return None
        with target.open("rb") as stream:
            return " ".join(plistlib.load(stream).get("ProgramArguments", []))
    output = powershell(
        if_task("'task ' + $task.Actions[0].Execute + ' ' + $task.Actions[0].Arguments")
    )
    return output.removeprefix("task ") if output.startswith("task ") else None


def wait_for_free_ports(settings: dict, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            check_ports(settings)
            return
        except RuntimeError:
            # A stopped service can need a moment before Windows releases its sockets.
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def replace_portable(
    executable: Path, config: Path, release: Path, system: str | None = None
) -> dict:
    """Point the startup definition at another release, start it, and verify it."""
    remove_portable(system)
    wait_for_free_ports(json.loads(config.read_text(encoding="utf-8")))
    return activate_portable(executable, config, release, system)


def command_path(root: Path, system: str | None = None) -> Path:
    system = system or sys.platform
    if system == "win32":
        return root / "bin" / "campix.cmd"
    return Path.home() / ".local/bin/campix"


def install_command(root: Path, python: str, system: str | None = None) -> Path:
    """Install the `campix` command, which runs the stdlib-only manager with this Python."""
    system = system or sys.platform
    manager = str(root / "manager" / "manage.py")
    for value in (python, manager):
        if any(ord(character) < 32 for character in value) or '"' in value:
            raise RuntimeError("Unsupported characters in the CamPiX command path")
    target = command_path(root, system)
    if system == "win32":
        # (goto) ends the batch context before the manager runs: `campix uninstall` can
        # then delete this file, and the manager's exit status still reaches the caller.
        content = f"@(goto) 2>nul & {batch_path(python)} -I {batch_path(manager)} %*\r\n"
        try:
            # cmd.exe reads batch files in the OEM code page.
            encoded = content.encode(f"cp{_oem_code_page()}")
        except UnicodeEncodeError as exc:
            raise RuntimeError(
                "The Python path contains characters the console code page cannot represent"
            ) from exc
    else:
        content = f'#!/bin/sh\nexec {shlex.quote(python)} -I {shlex.quote(manager)} "$@"\n'
        encoded = content.encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(encoded)
    if system != "win32":
        temporary.chmod(0o755)
    os.replace(temporary, target)
    if system == "win32":
        add_user_path(target.parent)
    return target


def remove_command(root: Path, system: str | None = None) -> None:
    system = system or sys.platform
    target = command_path(root, system)
    if system == "win32":
        remove_user_path(target.parent)
    target.unlink(missing_ok=True)


def command_on_path(root: Path, system: str | None = None) -> bool:
    folder = os.path.normcase(os.path.normpath(command_path(root, system).parent))
    entries = os.environ.get("PATH", "").split(os.pathsep)
    return any(os.path.normcase(os.path.normpath(entry)) == folder for entry in entries if entry)


def batch_path(value: str) -> str:
    """Quote a path for a batch file; the user's profile path is resolved by cmd.exe itself."""
    escaped = value.replace("%", "%%")
    local = os.environ.get("LOCALAPPDATA", "")
    # %LOCALAPPDATA% keeps profiles with non-ASCII names independent of the code page.
    if local and value.lower().startswith(local.lower().rstrip("\\") + "\\"):
        escaped = "%LOCALAPPDATA%" + value[len(local.rstrip("\\")) :].replace("%", "%%")
    return '"' + escaped + '"'


def _oem_code_page() -> int:
    import ctypes

    return ctypes.WinDLL("kernel32").GetOEMCP()


def _same_folder(entry: str, folder: Path) -> bool:
    expanded = os.path.expandvars(entry.strip().strip('"'))
    return os.path.normcase(os.path.normpath(expanded)) == os.path.normcase(
        os.path.normpath(str(folder))
    )


def add_user_path(folder: Path) -> bool:
    """Append a folder to the user's PATH in the registry; new terminals pick it up."""
    return _edit_user_path(folder, add=True)


def remove_user_path(folder: Path) -> bool:
    return _edit_user_path(folder, add=False)


def _edit_user_path(folder: Path, *, add: bool) -> bool:
    import winreg

    access = winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, access) as key:
        try:
            value, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            value, kind = "", winreg.REG_EXPAND_SZ
        entries = [entry for entry in str(value).split(";") if entry.strip()]
        present = any(_same_folder(entry, folder) for entry in entries)
        if add == present:
            return False
        if add:
            entries.append(str(folder))
        else:
            entries = [entry for entry in entries if not _same_folder(entry, folder)]
        winreg.SetValueEx(key, "Path", 0, kind, ";".join(entries))
    _announce_environment_change()
    return True


def _announce_environment_change() -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageTimeoutW.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPCWSTR,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    )
    result = ctypes.c_size_t()
    # HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG: Explorer reloads the environment.
    user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))
