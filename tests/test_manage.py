"""The `campix` command never touches real installations, startup definitions, or PATH."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
REVISION, NEWER = "a" * 40, "b" * 40
REPOSITORY = "https://example.invalid/campix.git"


@pytest.fixture
def manage(monkeypatch):
    monkeypatch.setattr(sys, "path", [str(SCRIPTS), *sys.path])
    spec = importlib.util.spec_from_file_location("campix_manage_test", SCRIPTS / "manage.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def installation(manage, tmp_path, *, status="ready", data=None):
    root = tmp_path / "CamPiX"
    (root / "releases" / REVISION / "scripts").mkdir(parents=True)
    (root / "data").mkdir()
    marker = {
        "format": 2,
        "profile": "portable-user",
        "revision": REVISION,
        "previous": None,
        "status": status,
        "repository": REPOSITORY,
    }
    (root / "install.json").write_text(json.dumps(marker), encoding="utf-8")
    config = {"data_dir": str(data or root / "data"), "port": 8123}
    (root / "campix.json").write_text(json.dumps(config), encoding="utf-8")
    return manage.Portable(root)


def arguments(**values):
    return SimpleNamespace(
        **{"repository": None, "ref": "main", "purge": False, "yes": True, **values}
    )


def test_status_reports_running_stopped_and_damaged(manage, tmp_path, capsys):
    context = installation(manage, tmp_path)
    context.pid = lambda: 4321
    assert manage.status(context, arguments()) == 0
    assert "running (process 4321): http://127.0.0.1:8123" in capsys.readouterr().out
    context.pid = lambda: 0
    assert manage.status(context, arguments()) == 3
    assert "stopped" in capsys.readouterr().out

    def damaged():
        raise RuntimeError("Portable service Python or logging entry point is missing")

    context.pid = damaged
    assert manage.status(context, arguments()) == 1
    assert "campix repair" in capsys.readouterr().out


def test_start_and_stop_use_the_registered_startup_definition(
    manage, tmp_path, monkeypatch, capsys
):
    context = installation(manage, tmp_path)
    calls = []
    monkeypatch.setattr(
        manage.startup, "start_portable", lambda *paths: calls.append(("start", paths)) or True
    )
    monkeypatch.setattr(manage.startup, "stop_portable", lambda: calls.append(("stop",)))
    monkeypatch.setattr(manage.startup, "wait_for_free_ports", lambda settings, timeout: None)
    assert manage.start(context, arguments()) == 0
    assert manage.stop(context, arguments()) == 0
    assert calls == [("start", (context.daemon, context.config_file, context.release)), ("stop",)]
    output = capsys.readouterr().out
    assert "CamPiX started: http://127.0.0.1:8123" in output
    assert "starts again at the next login" in output


def test_update_does_nothing_when_the_newest_revision_is_installed(
    manage, tmp_path, monkeypatch, capsys
):
    context = installation(manage, tmp_path)
    monkeypatch.setattr(manage, "fetch_source", lambda *args: (tmp_path / "source", REVISION))
    monkeypatch.setattr(manage, "hand_over", lambda *args: pytest.fail("Nothing to install"))
    assert manage.update(context, arguments()) == 0
    assert f"up to date ({REVISION[:12]})" in capsys.readouterr().out


def test_update_hands_over_to_the_fetched_revision(manage, tmp_path, monkeypatch):
    context = installation(manage, tmp_path)
    fetched, handed = [], []

    def fetch(repository, reference, stage):
        fetched.append((repository, reference))
        return stage / "source", NEWER

    monkeypatch.setattr(manage, "fetch_source", fetch)
    monkeypatch.setattr(manage, "hand_over", lambda *args: handed.append(args) or 0)
    assert manage.update(context, arguments(ref="v0.3.0")) == 0
    assert fetched == [(REPOSITORY, "v0.3.0")]
    assert handed[0][1:] == ("--update", REPOSITORY)


@pytest.mark.parametrize(
    ("repository", "reference", "message"),
    [
        (REPOSITORY, "../main", "Invalid --ref"),
        (REPOSITORY, "-u", "Invalid --ref"),
        ("http://example.invalid/campix.git", "main", "HTTPS Git repository"),
        ("ssh://example.invalid/campix.git", "main", "HTTPS Git repository"),
    ],
)
def test_fetch_source_rejects_unsafe_references_and_transports(
    manage, tmp_path, repository, reference, message
):
    with pytest.raises(manage.ManageError, match=message):
        manage.fetch_source(repository, reference, tmp_path)


def test_fetch_source_requires_git(manage, tmp_path, monkeypatch):
    monkeypatch.setattr(manage.shutil, "which", lambda name: None)
    with pytest.raises(manage.ManageError, match="Git is required"):
        manage.fetch_source(REPOSITORY, "main", tmp_path)


def git(*arguments, cwd):
    return subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.skipif(not shutil.which("git"), reason="Git is required for fetch tests")
def test_fetch_source_checks_out_a_branch_or_an_exact_commit(manage, tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "--quiet", "--initial-branch=main", cwd=origin)
    git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=origin)
    (origin / "marker.txt").write_text("first", encoding="utf-8")
    git("add", "marker.txt", cwd=origin)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "1",
        cwd=origin,
    )
    first = git("rev-parse", "HEAD", cwd=origin)
    (origin / "marker.txt").write_text("second", encoding="utf-8")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qam",
        "2",
        cwd=origin,
    )
    second = git("rev-parse", "HEAD", cwd=origin)
    source, revision = manage.fetch_source(str(origin), "main", tmp_path / "branch")
    assert revision == second and (source / "marker.txt").read_text(encoding="utf-8") == "second"
    source, revision = manage.fetch_source(str(origin), first, tmp_path / "commit")
    assert revision == first and (source / "marker.txt").read_text(encoding="utf-8") == "first"


def test_hand_over_runs_the_fetched_installer_without_ambient_python_settings(
    manage, tmp_path, monkeypatch
):
    source = tmp_path / "source"
    (source / "scripts").mkdir(parents=True)
    (source / "scripts/install.py").write_text("", encoding="utf-8")
    commands = []
    monkeypatch.setattr(
        manage.subprocess,
        "run",
        lambda command, check: commands.append(command) or SimpleNamespace(returncode=0),
    )
    assert manage.hand_over(source, "--repair", REPOSITORY) == 0
    assert commands == [
        [
            sys.executable,
            "-E",
            "-s",
            str(source / "scripts/install.py"),
            "--repair",
            "--source",
            str(source),
            "--repository",
            REPOSITORY,
        ]
    ]


@pytest.fixture
def quiet_command(manage, monkeypatch):
    def install_command(root, python):
        target = root / "bin" / "campix"
        target.parent.mkdir(exist_ok=True)
        target.write_text("campix", encoding="utf-8")
        return target

    monkeypatch.setattr(manage.startup, "install_command", install_command)
    monkeypatch.setattr(manage.startup, "command_path", lambda root: root / "bin" / "campix")


def test_repair_rebuilds_a_damaged_release_from_the_installed_revision(
    manage, tmp_path, monkeypatch, quiet_command, capsys
):
    context = installation(manage, tmp_path)
    fetched, handed = [], []

    def fetch(repository, reference, stage):
        fetched.append(reference)
        return stage / "source", reference

    monkeypatch.setattr(manage, "fetch_source", fetch)
    monkeypatch.setattr(manage, "hand_over", lambda *args: handed.append(args[1]) or 0)
    assert manage.repair(context, arguments()) == 0
    assert fetched == [REVISION] and handed == ["--repair"]
    assert "application environment is missing" in capsys.readouterr().out


def test_repair_re_registers_missing_automatic_startup(
    manage, tmp_path, monkeypatch, quiet_command
):
    context = installation(manage, tmp_path, status="updating")
    replaced = []
    monkeypatch.setattr(manage, "release_problem", lambda context: None)
    monkeypatch.setattr(manage.startup, "startup_command", lambda: None)
    monkeypatch.setattr(manage.startup, "replace_portable", lambda *paths: replaced.append(paths))
    assert manage.repair(context, arguments()) == 0
    assert replaced == [(context.daemon, context.config_file, context.release)]
    marker = json.loads((context.root / "install.json").read_text(encoding="utf-8"))
    assert marker["status"] == "ready" and marker["revision"] == REVISION


def test_repair_restarts_a_running_but_unresponsive_service(
    manage, tmp_path, monkeypatch, quiet_command, capsys
):
    context = installation(manage, tmp_path)
    events = []
    monkeypatch.setattr(manage, "release_problem", lambda context: None)
    monkeypatch.setattr(manage.startup, "startup_command", lambda: f"run {context.release} serve")
    context.start = lambda: events.append("start") or len(events) > 1
    context.stop = lambda: events.append("stop")

    def unresponsive(*args, **kwargs):
        raise RuntimeError("Managed service did not become live")

    monkeypatch.setattr(manage.startup, "wait_for_listener", unresponsive)
    assert manage.repair(context, arguments()) == 0
    assert events == ["start", "stop", "start"]
    assert "restarted the unresponsive service" in capsys.readouterr().out


def test_repair_reports_database_damage_without_overwriting_it(
    manage, tmp_path, monkeypatch, quiet_command, capsys
):
    context = installation(manage, tmp_path)
    database = context.root / "data" / "campix.sqlite3"
    database.write_bytes(b"not a database" * 64)
    monkeypatch.setattr(manage, "release_problem", lambda context: None)
    monkeypatch.setattr(manage.startup, "startup_command", lambda: f"run {context.release}")
    context.start = lambda: True
    assert manage.repair(context, arguments()) == 1
    assert "database reports damage" in capsys.readouterr().out
    assert database.read_bytes() == b"not a database" * 64


@pytest.fixture
def removal(manage, monkeypatch):
    calls = []
    monkeypatch.setattr(manage, "removable_root", lambda root: calls.append("checked"))
    monkeypatch.setattr(manage.startup, "remove_portable", lambda: calls.append("startup"))
    monkeypatch.setattr(manage, "stop_leftovers", lambda root: calls.append("leftovers"))
    monkeypatch.setattr(manage.startup, "remove_command", lambda root: calls.append("command"))
    return calls


def test_uninstall_keeps_accounts_and_media_in_a_backup_by_default(
    manage, tmp_path, removal, capsys
):
    context = installation(manage, tmp_path)
    (context.root / "data/campix.sqlite3").write_text("test-only-accounts", encoding="utf-8")
    (context.root / "backups").mkdir()
    assert manage.uninstall(context, arguments()) == 0
    assert removal == ["checked", "startup", "leftovers", "command"]
    assert not context.root.exists()
    (backup,) = tmp_path.glob("CamPiX-backup-*")
    assert (backup / "data/campix.sqlite3").read_text(encoding="utf-8") == "test-only-accounts"
    assert (backup / "campix.json").is_file() and (backup / "backups").is_dir()
    assert str(backup) in capsys.readouterr().out


def test_uninstall_purge_deletes_everything(manage, tmp_path, removal, capsys):
    context = installation(manage, tmp_path)
    assert manage.uninstall(context, arguments(purge=True)) == 0
    assert not context.root.exists() and not list(tmp_path.glob("CamPiX-backup-*"))
    assert "were deleted" in capsys.readouterr().out


def test_uninstall_without_confirmation_changes_nothing(
    manage, tmp_path, removal, monkeypatch, capsys
):
    context = installation(manage, tmp_path)
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda question: "n")
    assert manage.uninstall(context, arguments(yes=False)) == 1
    assert removal == ["checked"] and (context.root / "install.json").is_file()
    assert "Nothing was changed" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(manage.ManageError, match="--yes"):
        manage.uninstall(context, arguments(yes=False))


def test_uninstall_leaves_data_outside_the_installation_untouched(
    manage, tmp_path, removal, capsys
):
    external = tmp_path / "recordings"
    external.mkdir()
    (external / "clip.mp4").write_bytes(b"test-only-media")
    context = installation(manage, tmp_path, data=external)
    assert manage.uninstall(context, arguments(purge=True)) == 0
    assert (external / "clip.mp4").read_bytes() == b"test-only-media"
    assert f"stays untouched: {external.resolve()}" in capsys.readouterr().out


def test_uninstall_refuses_any_folder_but_this_users_installation(manage, tmp_path, monkeypatch):
    context = installation(manage, tmp_path)
    monkeypatch.setattr(manage.install, "portable_root", lambda host: tmp_path / "elsewhere")
    with pytest.raises(manage.ManageError, match="not this user's CamPiX folder"):
        manage.removable_root(context.root)
    monkeypatch.setattr(manage.install, "portable_root", lambda host: context.root)
    manage.removable_root(context.root)
    (context.root / "install.json").unlink()
    with pytest.raises(manage.ManageError, match="no CamPiX installation metadata"):
        manage.removable_root(context.root)


def test_only_one_changing_command_runs_at_a_time(manage, tmp_path):
    lock = tmp_path / "campix.lock"
    with manage.exclusive(lock):
        with pytest.raises(manage.ManageError, match="already running"):
            with manage.exclusive(lock):
                pass
    with manage.exclusive(lock):
        pass


def test_main_dispatches_and_reports_errors_without_tracebacks(
    manage, tmp_path, monkeypatch, capsys
):
    context = installation(manage, tmp_path)
    context.pid = lambda: 0
    assert manage.main(["status"], context=context) == 3

    def failing(context, args):
        raise manage.install.InstallError("uv download failed; check connectivity and retry.")

    monkeypatch.setitem(manage.COMMANDS, "update", failing)
    assert manage.main(["update"], context=context) == 1
    assert "campix update: uv download failed" in capsys.readouterr().err


@pytest.fixture
def native(manage, tmp_path, monkeypatch):
    etc, data, store = tmp_path / "etc/campix", tmp_path / "var/lib/campix", tmp_path / "store"
    etc.mkdir(parents=True)
    data.mkdir(parents=True)
    store.mkdir()
    marker = {"format": 2, "profile": "native-debian", "revision": REVISION, "status": "ready"}
    (etc / "install.json").write_text(json.dumps(marker), encoding="utf-8")
    monkeypatch.setattr(manage.install, "NATIVE_MARKER", etc / "install.json")
    monkeypatch.setattr(manage.install, "NATIVE_CONFIG", etc / "campix.json")
    monkeypatch.setattr(manage.install, "NATIVE_DATA", data)
    monkeypatch.setattr(manage.install, "NATIVE_PACKAGE_STORE", store)
    commands = []

    def run(command, **kwargs):
        argv = [str(part) for part in command]
        commands.append(argv)
        stdout = {"dpkg": "", "systemctl": "0"}.get(argv[0], "")
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(manage.install, "run", run)
    monkeypatch.setattr(manage.startup, "wait_for_listener", lambda *args, **kwargs: None)
    context = manage.Native()
    context.lock = tmp_path / "manager.lock"
    return SimpleNamespace(context=context, commands=commands, data=data, etc=etc, store=store)


def test_native_start_and_stop_use_the_system_service(manage, native):
    assert manage.start(native.context, arguments()) == 0
    assert manage.stop(native.context, arguments()) == 0
    assert ["sudo", "systemctl", "start", "campix"] in native.commands
    assert native.commands[-1] == ["sudo", "systemctl", "stop", "campix"]


def test_native_uninstall_moves_data_to_a_root_only_backup(manage, native):
    assert manage.uninstall(native.context, arguments()) == 0
    moved = [command for command in native.commands if command[:2] == ["sudo", "mv"]]
    assert [Path(command[2]) for command in moved] == [native.data, native.etc]
    assert all(Path(command[3]).as_posix().startswith("/var/backups/campix-") for command in moved)
    assert any(command[:3] == ["sudo", "chown", "-R"] for command in native.commands)
    assert ["sudo", "apt-get", "purge", "-y", "campix"] in native.commands
    removed = next(command for command in native.commands if command[:3] == ["sudo", "rm", "-rf"])
    assert str(native.data) not in removed
    assert ["sudo", "deluser", "--system", "campix"] in native.commands


def test_native_purge_deletes_data_and_never_moves_it(manage, native):
    assert manage.uninstall(native.context, arguments(purge=True)) == 0
    assert not any(command[:2] == ["sudo", "mv"] for command in native.commands)
    removed = next(command for command in native.commands if command[:3] == ["sudo", "rm", "-rf"])
    assert str(native.data) in removed


def test_native_repair_reinstalls_damaged_package_files_from_the_stored_package(
    manage, native, monkeypatch
):
    package = native.store / f"campix-{REVISION}.deb"
    package.write_bytes(b"test-only-package")
    original = manage.install.run

    def verify(command, **kwargs):
        if command[:2] == ["dpkg", "--verify"]:
            return subprocess.CompletedProcess(command, 1, "missing   /usr/lib/campix/app.py\n", "")
        return original(command, **kwargs)

    monkeypatch.setattr(manage.install, "run", verify)
    monkeypatch.setattr(native.context, "start", lambda: False)
    assert manage.repair(native.context, arguments()) == 0
    reinstall = [command for command in native.commands if "--reinstall" in command]
    assert reinstall and reinstall[0][-1] == str(package)


def test_posix_command_quotes_paths_for_the_shell(manage, tmp_path, monkeypatch):
    monkeypatch.setattr(manage.startup.Path, "home", lambda: tmp_path)
    root = tmp_path / "share/cam pix's"
    command = manage.startup.install_command(root, "/usr/bin/python3", "linux")
    assert command == tmp_path / ".local/bin/campix"
    script = command.read_text(encoding="utf-8")
    assert script.startswith("#!/bin/sh\nexec /usr/bin/python3 -I ")
    assert "'\"'\"'" in script and script.endswith('"$@"\n')
    if os.name != "nt":
        assert command.stat().st_mode & 0o777 == 0o755


def test_batch_paths_resolve_the_profile_folder_and_escape_percent(manage, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Jürgen\AppData\Local")
    assert (
        manage.startup.batch_path(r"C:\Users\Jürgen\AppData\Local\CamPiX\manager\manage.py")
        == r'"%LOCALAPPDATA%\CamPiX\manager\manage.py"'
    )
    assert manage.startup.batch_path(r"D:\100%\python.exe") == r'"D:\100%%\python.exe"'


def test_command_rejects_paths_that_would_break_the_shim(manage, tmp_path):
    with pytest.raises(RuntimeError, match="Unsupported characters"):
        manage.startup.install_command(tmp_path, 'C:\\py"thon.exe', "linux")


class FakeRegistry:
    HKEY_CURRENT_USER, KEY_QUERY_VALUE, KEY_SET_VALUE, REG_SZ, REG_EXPAND_SZ = 1, 1, 2, 1, 2

    def __init__(self, value):
        self.value = value

    def OpenKey(self, *args):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def QueryValueEx(self, key, name):
        if self.value is None:
            raise FileNotFoundError(name)
        return self.value

    def SetValueEx(self, key, name, reserved, kind, value):
        self.value = (value, kind)


def test_user_path_entry_is_added_once_and_removed_without_touching_others(
    manage, tmp_path, monkeypatch
):
    registry = FakeRegistry((r"%USERPROFILE%\bin;C:\Tools", 2))
    monkeypatch.setitem(sys.modules, "winreg", registry)
    announced = []
    monkeypatch.setattr(manage.startup, "_announce_environment_change", lambda: announced.append(1))
    folder = tmp_path / "CamPiX" / "bin"
    assert manage.startup.add_user_path(folder) is True
    assert manage.startup.add_user_path(folder) is False
    assert registry.value == (rf"%USERPROFILE%\bin;C:\Tools;{folder}", 2)
    assert manage.startup.remove_user_path(folder) is True
    assert registry.value == (r"%USERPROFILE%\bin;C:\Tools", 2)
    assert announced == [1, 1]


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe batch semantics")
def test_windows_command_can_delete_itself_and_keeps_the_exit_status(manage, tmp_path, monkeypatch):
    monkeypatch.setattr(manage.startup, "add_user_path", lambda folder: False)
    root = tmp_path / "CamPiX"
    (root / "manager").mkdir(parents=True)
    (root / "manager/manage.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).unlink()\nprint(sys.argv[2])\nraise SystemExit(7)\n",
        encoding="utf-8",
    )
    command = manage.startup.install_command(root, sys._base_executable, "win32")
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(command), str(command), "two words"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 7 and result.stdout.strip() == "two words"
    assert not command.exists() and result.stderr == ""


@pytest.mark.parametrize(
    ("system", "expected"),
    [
        ("linux", [("systemctl", "--user", "stop", "campix.service")]),
        ("win32", ["Stop-ScheduledTask -TaskName 'CamPiX'"]),
    ],
)
def test_stop_targets_only_the_campix_definition(manage, monkeypatch, system, expected):
    commands = []
    monkeypatch.setattr(manage.startup, "run", lambda *command: commands.append(command) or "")
    manage.startup.stop_portable(system)
    if system == "linux":
        assert commands == expected
    else:
        assert expected[0] in commands[0][-1] and commands[0][:4] == (
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
        )


def test_removing_a_user_service_disables_it_and_deletes_the_unit(manage, tmp_path, monkeypatch):
    monkeypatch.setattr(manage.startup.Path, "home", lambda: tmp_path)
    unit = tmp_path / ".config/systemd/user/campix.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/release/python run-service.py\n", encoding="utf-8")
    assert manage.startup.startup_command("linux") == "ExecStart=/release/python run-service.py"
    commands = []
    monkeypatch.setattr(manage.startup, "run", lambda *command: commands.append(command) or "")
    manage.startup.remove_portable("linux")
    assert commands == [
        ("systemctl", "--user", "disable", "--now", "campix.service"),
        ("systemctl", "--user", "daemon-reload"),
    ]
    assert not unit.exists() and manage.startup.startup_command("linux") is None


def test_replacing_the_startup_definition_waits_for_the_old_ports(manage, tmp_path, monkeypatch):
    config = tmp_path / "campix.json"
    config.write_text(json.dumps({"data_dir": str(tmp_path), "port": 8123}), encoding="utf-8")
    events = []
    monkeypatch.setattr(manage.startup, "remove_portable", lambda system: events.append("removed"))
    checks = iter([RuntimeError("Loopback port 8123 is occupied"), None])

    def ports(settings):
        error = next(checks)
        events.append("checked")
        if error:
            raise error

    monkeypatch.setattr(manage.startup, "check_ports", ports)
    monkeypatch.setattr(manage.startup.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        manage.startup, "activate_portable", lambda *args: events.append("activated") or {}
    )
    manage.startup.replace_portable(tmp_path / "campixd", config, tmp_path / "release", "linux")
    assert events == ["removed", "checked", "checked", "activated"]


def test_debian_package_ships_the_stdlib_manager(tmp_path):
    spec = importlib.util.spec_from_file_location("campix_build_deb", SCRIPTS / "build-deb.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lib, bin_dir = tmp_path / "usr/lib/campix", tmp_path / "usr/bin"
    lib.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    module.stage_manager(lib, bin_dir)
    assert sorted(path.name for path in (lib / "manager").iterdir()) == [
        "install.py",
        "install_startup.py",
        "manage.py",
    ]
    assert (bin_dir / "campix").read_text(encoding="utf-8") == (
        '#!/bin/sh\nexec /usr/bin/python3 -I /usr/lib/campix/manager/manage.py "$@"\n'
    )


def test_corrupt_database_detection_uses_a_read_only_connection(manage, tmp_path):
    database = tmp_path / "campix.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE users (name TEXT)")
    assert manage.database_note(database, tmp_path / "backups") is None
    assert manage.database_note(tmp_path / "missing.sqlite3", tmp_path) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Task Scheduler behavior")
def test_task_actions_treat_a_missing_task_as_absent(manage, monkeypatch):
    # A failed lookup used to end `powershell -Command` with exit code 1 and broke repair.
    missing = "CamPiX-test-never-registered-7f3c"
    monkeypatch.setattr(manage.startup, "TASK", missing)
    assert manage.startup.startup_command("win32") is None
    manage.startup.stop_portable("win32")
    manage.startup.remove_portable("win32")
    # Starting a missing task is a real failure and must still be reported.
    with pytest.raises(RuntimeError, match="powershell.exe"):
        manage.startup.powershell(f"Start-ScheduledTask -TaskName '{missing}'")
