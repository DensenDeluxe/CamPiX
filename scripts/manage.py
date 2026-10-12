"""Manage an installed CamPiX: start, stop, status, update, repair, or uninstall.

The `campix` command runs this file with the installation's base Python and the standard
library only, so it keeps working when the application environment itself is damaged.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
# `python -I` omits the script directory; import only the reviewed installer modules here.
sys.path.insert(0, str(HERE))

import install  # noqa: E402
import install_startup as startup  # noqa: E402

NATIVE_MANAGER = Path("/usr/lib/campix/manager")
REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


class ManageError(RuntimeError):
    """An actionable management failure whose message contains no secrets."""


class Portable:
    """A per-user installation in the platform's private application folder."""

    native = False

    def __init__(self, root: Path):
        self.root = root
        self.marker = install.read_marker(root / "install.json")
        self.config_file = root / "campix.json"
        self.windows = sys.platform == "win32"
        self.release = root / "releases" / self.marker["revision"]
        self.daemon = install.release_daemon(self.release, self.windows)
        self.lock = root.with_name(root.name + ".lock")

    def settings(self) -> dict:
        return json.loads(self.config_file.read_text(encoding="utf-8"))

    def pid(self) -> int:
        return startup.portable_pid(self.daemon, self.config_file, self.release)

    def start(self) -> bool:
        return startup.start_portable(self.daemon, self.config_file, self.release)

    def stop(self) -> None:
        startup.stop_portable()
        # Task Scheduler returns before the processes end; report only a finished stop.
        startup.wait_for_free_ports(self.settings(), timeout=30)


class Native:
    """The Debian/Raspberry Pi OS package with its `campix` system service."""

    native = True

    def __init__(self):
        self.marker = install.read_marker(install.NATIVE_MARKER)
        self.lock = Path.home() / ".cache" / "campix-manager.lock"

    def settings(self) -> dict:
        try:
            return json.loads(install.NATIVE_CONFIG.read_text(encoding="utf-8"))
        except PermissionError:
            # Readable by root and the service account only; never prompt just for status.
            output = install.run(
                ["sudo", "-n", "cat", install.NATIVE_CONFIG], capture=True, check=False
            )
            try:
                return json.loads(output.stdout) if output.returncode == 0 else {}
            except ValueError:
                return {}
        except (OSError, ValueError):
            return {}

    def pid(self) -> int:
        return install.native_pid()

    def start(self) -> bool:
        if self.pid() > 0:
            return False
        install.run(["sudo", "systemctl", "start", "campix"])
        startup.wait_for_listener(self.settings().get("port", 8080), install.native_pid, timeout=45)
        return True

    def stop(self) -> None:
        install.run(["sudo", "systemctl", "stop", "campix"])


@contextmanager
def exclusive(path: Path):
    """Allow one update, repair, or uninstall at a time; the OS releases it on exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        stream.close()
        raise ManageError("Another campix command is already running") from exc
    try:
        yield
    finally:
        stream.close()


def url(context) -> str:
    return f"http://127.0.0.1:{context.settings().get('port', 8080)}"


def ensure_running(context, actions: list[str]) -> None:
    """Start a stopped service and restart one that runs but does not answer."""
    if context.start():
        actions.append("started the service")
        return
    try:
        startup.wait_for_listener(context.settings().get("port", 8080), context.pid, timeout=15)
    except RuntimeError:
        context.stop()
        context.start()
        actions.append("restarted the unresponsive service")


def status(context, args) -> int:
    revision = context.marker["revision"][:12]
    try:
        pid = context.pid()
    except (RuntimeError, install.InstallError):
        print(f"CamPiX {revision} looks damaged; run campix repair.")
        return 1
    if not pid:
        print(f"CamPiX {revision} is stopped. Start it with campix start.")
        return 3
    print(f"CamPiX {revision} is running (process {pid}): {url(context)}")
    return 0


def start(context, args) -> int:
    try:
        started = context.start()
    except (RuntimeError, install.InstallError) as exc:
        raise ManageError(f"{exc}; run campix repair if this persists") from exc
    print(f"CamPiX {'started' if started else 'is already running'}: {url(context)}")
    return 0


def stop(context, args) -> int:
    context.stop()
    later = "boot" if context.native else "login"
    print(f"CamPiX stopped. It starts again at the next {later} or with campix start.")
    return 0


def fetch_source(repository: str, reference: str, stage: Path) -> tuple[Path, str]:
    if not REFERENCE.fullmatch(reference) or ".." in reference:
        raise ManageError("Invalid --ref; use a branch, tag, or commit.")
    if not (repository.startswith("https://") or Path(repository).is_dir()):
        raise ManageError("Updates use an HTTPS Git repository or a local clone.")
    git = shutil.which("git")
    if not git:
        raise ManageError("Git is required for campix update and repair; install it and retry.")
    source = stage / "source"
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_HTTP_LOW_SPEED_LIMIT": "1000",
        "GIT_HTTP_LOW_SPEED_TIME": "60",
    }
    try:
        install.run([git, "init", "--quiet", source], env=env)
        install.run([git, "-C", source, "remote", "add", "origin", repository], env=env)
        install.run(
            [git, "-C", source, "-c", "credential.interactive=false", "fetch", "--quiet"]
            + ["--depth", "1", "origin", reference],
            env=env,
            timeout=600,
        )
        install.run([git, "-C", source, "checkout", "--quiet", "--detach", "FETCH_HEAD"], env=env)
        revision = install.run([git, "-C", source, "rev-parse", "HEAD"], capture=True, env=env)
    except install.InstallError as exc:
        raise ManageError(
            f"Could not fetch {reference}; check the network connection and the reference."
        ) from exc
    return source, revision.stdout.strip()


def hand_over(source: Path, mode: str, repository: str) -> int:
    """Run the fetched revision's installer, which knows how to apply its own layout."""
    installer = source / "scripts" / "install.py"
    if not installer.is_file():
        raise ManageError("The fetched revision has no installer; choose a newer --ref.")
    command = [sys.executable, "-E", "-s", str(installer), mode, "--source", str(source)]
    sys.stdout.flush()  # Keep this command's messages ahead of the installer's output.
    return subprocess.run([*command, "--repository", repository], check=False).returncode


def repository_for(context, args) -> str:
    return args.repository or context.marker.get("repository") or install.REPOSITORY


def update(context, args) -> int:
    current = context.marker["revision"]
    repository = repository_for(context, args)
    with tempfile.TemporaryDirectory(prefix="campix-update-") as stage:
        source, revision = fetch_source(repository, args.ref, Path(stage))
        if revision == current:
            print(f"CamPiX is up to date ({current[:12]}).")
            return 0
        print(f"Updating CamPiX {current[:12]} to {revision[:12]} ...")
        return hand_over(source, "--update", repository)


def rebuild(context, args) -> int:
    revision = context.marker["revision"]
    print(f"Rebuilding CamPiX {revision[:12]}; accounts and media stay untouched ...")
    repository = repository_for(context, args)
    with tempfile.TemporaryDirectory(prefix="campix-repair-") as stage:
        source, fetched = fetch_source(repository, revision, Path(stage))
        if fetched != revision:
            raise ManageError("The repository returned a different revision than requested.")
        return hand_over(source, "--repair", repository)


def database_note(database: Path, backups: Path) -> str | None:
    if not database.is_file():
        return None
    uri = database.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.Error as exc:
        result = type(exc).__name__
    if result == "ok":
        return None
    return (
        f"The database reports damage ({str(result)[:80]}). Stop CamPiX and restore a copy "
        f"from {backups}; repair never overwrites accounts or media."
    )


def release_problem(context) -> str | None:
    """Explain why the installed release cannot run, or return None."""
    python = context.daemon.with_name("python.exe" if context.windows else "python")
    if not python.is_file() or not context.daemon.is_file():
        return "the application environment is missing"
    if not (context.release / "scripts" / "run-service.py").is_file():
        return "the service entry point is missing"
    try:
        check = subprocess.run(
            [str(python), "-c", "import campix.app"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "the application environment cannot start"
    if check.returncode:
        return "the application environment cannot load CamPiX"
    media = context.settings().get("mediamtx_binary")
    if media:
        try:
            version = install.run([media, "--version"], capture=True, timeout=10).stdout
        except install.InstallError:
            version = ""
        if version.strip() != f"v{install.MEDIAMTX_VERSION}":
            return "the managed MediaMTX executable is missing or unexpected"
    return None


def repair(context, args) -> int:
    if context.native:
        return repair_native(context, args)
    actions, notes = [], []
    command = startup.command_path(context.root)
    before = command.read_bytes() if command.is_file() else None
    startup.install_command(context.root, sys.executable)
    if command.read_bytes() != before:
        actions.append("restored the campix command")
    settings = context.settings()
    data = Path(settings.get("data_dir", context.root / "data"))
    if data.is_dir() and os.name != "nt" and data.stat().st_mode & 0o077:
        data.chmod(0o700)
        actions.append("made the data folder private again")
    note = database_note(data / "campix.sqlite3", context.root / "backups")
    if note:
        notes.append(note)
    problem = release_problem(context)
    if problem:
        print(f"CamPiX needs a rebuild: {problem}.")
        code = rebuild(context, args)
        for item in notes:
            print(item)
        return code
    registered = startup.startup_command() or ""
    expected = str(context.release)
    if context.windows:
        registered, expected = registered.lower(), expected.lower()
    if expected not in registered or context.marker.get("status") != "ready":
        startup.replace_portable(context.daemon, context.config_file, context.release)
        install.replace_json(context.root / "install.json", {**context.marker, "status": "ready"})
        actions.append("re-registered automatic startup")
    else:
        ensure_running(context, actions)
    print("Repair finished: " + ("; ".join(actions) if actions else "no problems were found") + ".")
    for item in notes:
        print(item)
    print(f"CamPiX is running: {url(context)}")
    return 1 if notes else 0


def repair_native(context, args) -> int:
    actions = []
    install.run(["sudo", "systemctl", "enable", "campix"])
    data = str(install.NATIVE_DATA)
    install.run(["sudo", "install", "-d", "-m", "0700", "-o", "campix", "-g", "campix", data])
    verify = install.run(["dpkg", "--verify", "campix"], capture=True, check=False)
    # Configuration files change by design; any other report means damaged package files.
    damaged = [line for line in verify.stdout.splitlines() if line.strip() and " c " not in line]
    if damaged:
        stored = install.NATIVE_PACKAGE_STORE / f"campix-{context.marker['revision']}.deb"
        if not stored.is_file():
            return rebuild(context, args)
        install.native_reinstall(stored)
        actions.append("reinstalled the damaged package files")
    ensure_running(context, actions)
    print("Repair finished: " + ("; ".join(actions) if actions else "no problems were found") + ".")
    print(f"CamPiX is running: {url(context)}")
    return 0


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        raise ManageError("Confirmation needs a terminal; add --yes to uninstall unattended.")
    return input(f"{question} [y/N] ").strip().lower() in {"y", "yes", "j", "ja"}


def removable_root(root: Path) -> None:
    """Refuse anything but this user's own installation folder."""
    expected = install.portable_root(install.detect_host())
    if root.is_symlink() or root.resolve() != expected.resolve():
        raise ManageError(f"Refusing to remove {root}: it is not this user's CamPiX folder.")
    if os.name == "nt" and os.stat(root, follow_symlinks=False).st_file_attributes & 0x400:
        raise ManageError(f"Refusing to remove {root}: it is a junction.")
    if not (root / "install.json").is_file():
        raise ManageError(f"Refusing to remove {root}: it has no CamPiX installation metadata.")


def stop_leftovers(root: Path) -> None:
    """Stop processes still running from the installation, such as a foreground launch."""
    if os.name != "nt":
        return
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and "
        "$_.ExecutablePath.StartsWith($env:CAMPIX_REMOVE_ROOT + '\\', "
        "[StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { "
        "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "CAMPIX_REMOVE_ROOT": str(root)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )


def keep_data(root: Path) -> Path:
    backup = root.with_name(f"{root.name}-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    backup.mkdir(mode=0o700)
    for name in ("data", "backups", "campix.json"):
        if (root / name).exists():
            (root / name).rename(backup / name)
    return backup


def uninstall(context, args) -> int:
    if context.native:
        return uninstall_native(context, args)
    root = context.root
    removable_root(root)
    if args.purge:
        question = (
            f"Permanently delete CamPiX with all accounts, recordings, and settings in {root}?"
        )
    else:
        question = "Remove CamPiX? Accounts, recordings, and settings move to a backup folder."
    if not args.yes and not confirm(question):
        print("Nothing was changed.")
        return 1
    settings = context.settings() if context.config_file.is_file() else {}
    external = Path(settings.get("data_dir", root / "data")).resolve()
    if root.resolve() not in (external, *external.parents):
        external_note = f"Data outside the installation stays untouched: {external}"
    else:
        external_note = None
    startup.remove_portable()
    stop_leftovers(root)
    backup = None if args.purge else keep_data(root)
    install.remove_tree(root)
    # Last, so a failed removal can be retried with the same command.
    startup.remove_command(root)
    print("CamPiX was removed: automatic startup, the campix command, and the program.")
    if backup:
        print(f"Accounts, recordings, and settings were kept in {backup}")
        print("Delete that folder for a complete removal, or reuse its data after reinstalling.")
    else:
        print("All accounts, recordings, and settings were deleted.")
    if external_note:
        print(external_note)
    print("Shared tools such as Python, Git, and FFmpeg stay installed for other programs.")
    return 0


def uninstall_native(context, args) -> int:
    if args.purge:
        question = "Permanently delete CamPiX with all accounts, recordings, and settings?"
    else:
        question = "Remove CamPiX? Accounts, recordings, and settings move to /var/backups."
    if not args.yes and not confirm(question):
        print("Nothing was changed.")
        return 1
    install.run(["sudo", "systemctl", "disable", "--now", "campix"], check=False)
    backup = None
    if not args.purge:
        backup = Path(f"/var/backups/campix-{time.strftime('%Y%m%d-%H%M%S')}")
        install.run(["sudo", "install", "-d", "-m", "0700", backup])
        for item in (install.NATIVE_DATA, install.NATIVE_CONFIG.parent):
            if item.exists():
                install.run(["sudo", "mv", item, backup / item.name])
        # The service account is removed below, so the copy must not keep its numeric owner.
        install.run(["sudo", "chown", "-R", "root:root", backup])
    install.run(["sudo", "apt-get", "purge", "-y", "campix"], timeout=1800)
    leftovers = ["/etc/campix", "/usr/local/lib/campix", str(install.NATIVE_PACKAGE_STORE)]
    if args.purge:
        leftovers.append(str(install.NATIVE_DATA))
    install.run(["sudo", "rm", "-rf", "--", *leftovers])
    install.run(["sudo", "deluser", "--system", "campix"], check=False)
    install.run(["sudo", "delgroup", "--system", "--only-if-empty", "campix"], check=False)
    print("CamPiX was removed: the service, the campix command, the package, and its account.")
    if backup:
        print(f"Accounts, recordings, and settings were kept in {backup} (root only).")
    else:
        print("All accounts, recordings, and settings were deleted.")
    print("Shared packages such as FFmpeg and Python stay installed for other programs.")
    return 0


COMMANDS = {
    "start": start,
    "stop": stop,
    "status": status,
    "update": update,
    "repair": repair,
    "uninstall": uninstall,
}


def main(argv: list[str] | None = None, context=None) -> int:
    parser = argparse.ArgumentParser(prog="campix", description="Manage this CamPiX installation.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    commands.add_parser("start", help="start the CamPiX service")
    commands.add_parser("stop", help="stop the service until the next start, login, or boot")
    commands.add_parser("status", help="show the installed revision and whether it runs")
    update_command = commands.add_parser("update", help="install the newest revision from the web")
    update_command.add_argument(
        "--ref", default="main", help="branch, tag, or commit to install (default: main)"
    )
    update_command.add_argument("--repository", help=argparse.SUPPRESS)
    repair_command = commands.add_parser("repair", help="check the installation and fix damage")
    repair_command.add_argument("--repository", help=argparse.SUPPRESS)
    remove = commands.add_parser("uninstall", help="remove CamPiX; data moves to a backup folder")
    remove.add_argument(
        "--purge", action="store_true", help="also delete accounts, recordings, and settings"
    )
    remove.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)
    try:
        if context is None:
            context = Native() if HERE == NATIVE_MANAGER else Portable(HERE.parent)
        handler = COMMANDS[args.command]
        if args.command not in {"update", "repair", "uninstall"}:
            return handler(context, args)
        with exclusive(context.lock):
            code = handler(context, args)
        if args.command == "uninstall" and code == 0:
            context.lock.unlink(missing_ok=True)
        return code
    except KeyboardInterrupt:
        print(f"campix {args.command} was interrupted.", file=sys.stderr)
        return 130
    except (ManageError, install.InstallError, RuntimeError, OSError, ValueError) as exc:
        print(f"campix {args.command}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
