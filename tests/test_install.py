"""Installer preflight and failure tests never change real host/application state."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def installer(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("campix_installer_test", scripts / "install.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def host(installer):
    return installer.Host(
        system="linux",
        architecture="aarch64",
        distribution="debian",
        distribution_version="12",
        board="Generic ARM board",
        raspberry_pi=False,
        kernel="6.12",
        python="3.11.9",
        package_manager="apt-get",
    )


def completed(command=(), *, stdout="", code=0):
    return subprocess.CompletedProcess(command, code, stdout, "")


def test_os_release_is_data_and_does_not_execute_host_input(installer, tmp_path):
    marker = tmp_path / "must-not-exist"
    release = tmp_path / "os-release"
    release.write_text(
        f'ID=debian\nVERSION_ID="12"\nNAME="$(touch {marker})"\nBROKEN="unterminated\n',
        encoding="utf-8",
    )
    values = installer.os_release(release)
    assert values["ID"] == "debian"
    assert values["VERSION_ID"] == "12"
    assert values["NAME"].startswith("$(touch ")
    assert "BROKEN" not in values
    assert not marker.exists()


def test_native_camera_packages_require_detected_pi(installer, host):
    generic = installer.install_plan(host)
    assert generic["profile"] == "native-debian"
    assert "python3-picamera2" not in generic["packages"]
    pi = replace(host, board="Raspberry Pi 5", raspberry_pi=True, sensors=("imx500",))
    plan = installer.install_plan(pi)
    assert "python3-picamera2" in plan["packages"]
    assert "imx500-all" in plan["packages"]
    assert plan["hardware_qualified"] is False
    assert plan["mediamtx"] == "1.21.2"


@pytest.mark.parametrize(
    "system,manager", [("windows", "winget"), ("darwin", "brew"), ("linux", "dnf")]
)
def test_portable_profiles_do_not_claim_local_camera_drivers(installer, host, system, manager):
    plan = installer.install_plan(replace(host, system=system, package_manager=manager))
    assert plan["profile"] == "portable-user"
    assert plan["packages"] == ["ffmpeg"]
    assert plan["hardware_qualified"] is False


def test_unsupported_native_python_and_pi_portable_are_explicit(installer, host):
    old = installer.install_plan(replace(host, python="3.10.12"))
    assert any("3.11" in message for message in old["problems"])
    pi = installer.install_plan(replace(host, raspberry_pi=True, package_manager="dnf"))
    assert any("native" in message for message in pi["problems"])
    cpu = installer.install_plan(replace(host, architecture="mips64"))
    assert cpu["uv"]["asset"] is None
    assert cpu["problems"]


@pytest.mark.parametrize(
    "arch,expected",
    [
        ("armv6l", "uv-arm-unknown-linux-musleabihf.tar.gz"),
        ("armv7l", "uv-armv7-unknown-linux-gnueabihf.tar.gz"),
        ("aarch64", "uv-aarch64-unknown-linux-gnu.tar.gz"),
    ],
)
def test_uv_architecture_is_not_guessed_from_board_name(installer, host, arch, expected):
    asset, _ = installer.uv_asset(replace(host, architecture=arch))
    assert asset == expected


def test_dry_run_has_no_network_writes_or_elevation(installer, host, monkeypatch, capsys):
    monkeypatch.setattr(installer, "detect_host", lambda: host)

    def forbidden(*args, **kwargs):
        pytest.fail("Dry run attempted an installation action")

    for name in ("run", "download", "require_unprivileged", "source_revision", "native_install"):
        monkeypatch.setattr(installer, name, forbidden)
    monkeypatch.setattr(installer.tempfile, "TemporaryDirectory", forbidden)
    assert installer.main(["--dry-run"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["host"]["architecture"] == "aarch64"
    assert report["hardware_qualified"] is False


def test_noninteractive_fresh_install_fails_before_mutation(installer, host, monkeypatch, capsys):
    monkeypatch.setattr(installer, "detect_host", lambda: host)
    monkeypatch.setattr(installer, "require_unprivileged", lambda _: None)
    monkeypatch.setattr(
        installer, "source_revision", lambda _: pytest.fail("Source/install action happened")
    )
    assert installer.main(["--non-interactive"]) == 1
    assert "interactive secure administrator" in capsys.readouterr().err


def test_package_candidate_absence_stops_before_install(installer, host, monkeypatch):
    commands = []

    def fake(command, **kwargs):
        commands.append([str(value) for value in command])
        return completed(stdout="  Candidate: (none)\n")

    monkeypatch.setattr(installer, "run", fake)
    with pytest.raises(installer.InstallError, match="unavailable"):
        installer.install_host_packages(host, ["python3-picamera2"])
    assert commands == [["sudo", "apt-get", "update"], ["apt-cache", "policy", "python3-picamera2"]]


def test_checksum_failure_never_writes_or_executes_download(installer, host, tmp_path, monkeypatch):
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        installer, "download", lambda url, _: b"0" * 64 if url.endswith(".sha256") else b"tampered"
    )
    monkeypatch.setattr(
        installer, "run", lambda *args, **kwargs: pytest.fail("Unverified tool executed")
    )
    destination = tmp_path / "tools"
    with pytest.raises(installer.InstallError, match="SHA-256"):
        installer.provision_uv(host, destination)
    assert not destination.exists()


def test_verified_archive_extracts_only_expected_binary(installer, host, tmp_path, monkeypatch):
    _, prefix = installer.uv_asset(host)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, value in (
            (prefix + "/uv", b"verified-tool"),
            ("../../escape", b"must-not-extract"),
        ):
            item = tarfile.TarInfo(name)
            item.size = len(value)
            archive.addfile(item, io.BytesIO(value))
    data = buffer.getvalue()
    checksum = hashlib.sha256(data).hexdigest().encode("ascii")
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        installer, "download", lambda url, _: checksum if url.endswith(".sha256") else data
    )
    monkeypatch.setattr(installer, "run", lambda *args, **kwargs: completed(stdout="uv 0.13.0\n"))
    output = installer.provision_uv(host, tmp_path / "tools")
    assert output.read_bytes() == b"verified-tool"
    assert list((tmp_path / "tools").iterdir()) == [output]


def test_malformed_checksum_is_actionable(installer, host, tmp_path, monkeypatch):
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)
    monkeypatch.setattr(installer, "download", lambda *_: b"")
    with pytest.raises(installer.InstallError, match="manifest"):
        installer.provision_uv(host, tmp_path / "tools")


def test_native_existing_package_never_changes_accounts_or_files(installer, monkeypatch):
    calls = []

    def fake(command, **kwargs):
        calls.append(command)
        return completed(stdout="install ok installed")

    monkeypatch.setattr(installer, "run", fake)
    with pytest.raises(installer.InstallError, match="already installed"):
        installer.ensure_native_fresh()
    assert len(calls) == 1


def test_native_existing_system_account_is_not_repurposed(installer, monkeypatch):
    calls = []

    def fake(command, **kwargs):
        calls.append(command)
        return (
            completed(code=1)
            if command[0] == "dpkg-query"
            else completed(stdout="existing-account")
        )

    monkeypatch.setattr(installer, "run", fake)
    with pytest.raises(installer.InstallError, match="existing campix system account"):
        installer.ensure_native_fresh()
    assert [command[0] for command in calls] == ["dpkg-query", "getent"]


def test_existing_portable_state_and_path_escape_are_preserved(installer, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(installer.Path, "home", lambda: home)
    root = home / "campix"
    root.mkdir()
    media = root / "private-recording.mp4"
    media.write_bytes(b"original-media")
    with pytest.raises(installer.InstallError, match="Existing files"):
        installer.ensure_portable_fresh(root)
    with pytest.raises(installer.InstallError, match="private user directory"):
        installer.ensure_portable_fresh(tmp_path / "outside")
    assert media.read_bytes() == b"original-media"


def test_only_tracked_source_files_enter_release(installer, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("reviewed", encoding="utf-8")
    (source / ".env").write_text("PRIVATE_SECRET=do-not-copy", encoding="utf-8")
    monkeypatch.setattr(installer, "run", lambda *args, **kwargs: completed(stdout="README.md\0"))
    release = tmp_path / "release"
    installer.copy_source(source, release)
    assert list(release.iterdir()) == [release / "README.md"]
    assert not (release / ".env").exists()


def test_source_path_traversal_is_rejected(installer, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (tmp_path / "secret").write_text("do-not-copy", encoding="utf-8")
    monkeypatch.setattr(installer, "run", lambda *args, **kwargs: completed(stdout="../secret\0"))
    release = tmp_path / "release"
    with pytest.raises(installer.InstallError, match="unsafe path"):
        installer.copy_source(source, release)
    assert not list(release.iterdir())


def test_command_failure_does_not_expose_arguments_or_child_output(installer):
    secret = "private-test-secret"
    with pytest.raises(installer.InstallError) as error:
        installer.run(
            [sys.executable, "-c", "import sys; print(sys.argv[1]); sys.exit(7)", secret],
            capture=True,
        )
    assert secret not in str(error.value)
    assert "exit code 7" in str(error.value)


def test_subprocess_diagnostic_output_is_bounded(installer):
    with pytest.raises(installer.InstallError, match="output limit"):
        installer.run(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 2097152)"], capture=True
        )


def test_command_timeout_reaps_owned_process(installer, monkeypatch):
    processes = []
    popen = installer.subprocess.Popen

    def track(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(installer.subprocess, "Popen", track)
    with pytest.raises(installer.InstallError, match="Could not finish"):
        installer.run(
            [sys.executable, "-c", "import time; time.sleep(30)"], capture=True, timeout=0.1
        )
    assert len(processes) == 1 and processes[0].poll() is not None


def test_native_admin_failure_never_starts_service(installer, host, tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "packaging").mkdir(parents=True)
    (source / "packaging/campix.json").write_text(
        '{"data_dir":"/var/lib/campix","host":"127.0.0.1"}', encoding="utf-8"
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    commands = []

    def fake(command, **kwargs):
        argv = [str(value) for value in command]
        commands.append(argv)
        if any(value.endswith("build-deb.py") for value in argv):
            output = Path(argv[argv.index("--output-dir") + 1])
            output.mkdir()
            (output / "campix_test_arm64.deb").write_bytes(b"test-only-package")
        return completed(command)

    monkeypatch.setattr(installer, "run", fake)
    monkeypatch.setattr(installer, "ensure_native_fresh", lambda: None)
    monkeypatch.setattr(installer, "install_host_packages", lambda *_: None)
    monkeypatch.setattr(
        installer, "validate_encoder", lambda _: (tmp_path / "ffmpeg", tmp_path / "ffprobe")
    )
    monkeypatch.setattr(installer, "provision_uv", lambda *_: tmp_path / "uv")

    def admin_failure(*args, **kwargs):
        raise installer.InstallError("Secure administrator creation failed")

    monkeypatch.setattr(installer, "setup_command", admin_failure)
    args = argparse.Namespace(uv=None, without_mediamtx=True, non_interactive=False)
    with pytest.raises(installer.InstallError, match="administrator"):
        installer.native_install(
            source, "a" * 40, host, installer.install_plan(host, True), scratch, args
        )
    assert not any("systemctl" in command for command in commands)
    sync = next(command for command in commands if "sync" in command)
    assert sync[sync.index("--python") + 1] == "/usr/bin/python3"
    assert "--no-install-package" in sync
    assert "--extra" in sync and "v4l2" not in sync


@pytest.mark.parametrize("failure", ["enable", "listener"])
def test_native_activation_failure_disables_new_service(
    installer, host, tmp_path, monkeypatch, failure
):
    source = tmp_path / "source"
    (source / "packaging").mkdir(parents=True)
    (source / "packaging/campix.json").write_text('{"host":"127.0.0.1"}', encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    commands = []

    def fake(command, **kwargs):
        argv = [str(value) for value in command]
        commands.append(argv)
        if any(value.endswith("build-deb.py") for value in argv):
            output = Path(argv[argv.index("--output-dir") + 1])
            output.mkdir()
            (output / "campix_test_arm64.deb").write_bytes(b"test-only-package")
        if argv == ["sudo", "systemctl", "enable", "--now", "campix"] and failure == "enable":
            raise installer.InstallError("Service activation failed")
        return completed(command, stdout="123")

    def listener(port, running):
        assert running() == 123
        raise RuntimeError("Managed listener did not become live")

    monkeypatch.setattr(installer, "run", fake)
    monkeypatch.setattr(installer, "ensure_native_fresh", lambda: None)
    monkeypatch.setattr(installer, "install_host_packages", lambda *_: None)
    monkeypatch.setattr(
        installer, "validate_encoder", lambda _: (tmp_path / "ffmpeg", tmp_path / "ffprobe")
    )
    monkeypatch.setattr(installer, "provision_uv", lambda *_: tmp_path / "uv")
    monkeypatch.setattr(installer, "setup_command", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(installer, "check_ports", lambda *_: None)
    monkeypatch.setattr(installer, "wait_for_listener", listener)
    args = argparse.Namespace(uv=None, without_mediamtx=True, non_interactive=False)
    with pytest.raises(RuntimeError):
        installer.native_install(
            source, "a" * 40, host, installer.install_plan(host, True), scratch, args
        )
    assert commands[-1] == ["sudo", "systemctl", "disable", "--now", "campix"]


def test_portable_startup_failure_retains_private_config_and_data(
    installer, host, tmp_path, monkeypatch
):
    monkeypatch.setattr(installer.Path, "home", lambda: tmp_path)
    root = tmp_path / "campix"
    monkeypatch.setattr(installer, "portable_root", lambda _: root)
    monkeypatch.setattr(installer, "check_startup_available", lambda: None)
    monkeypatch.setattr(installer, "executable", lambda name, _: tmp_path / name)
    monkeypatch.setattr(
        installer, "validate_encoder", lambda _: (tmp_path / "ffmpeg", tmp_path / "ffprobe")
    )
    monkeypatch.setattr(installer, "provision_uv", lambda *_: tmp_path / "uv")
    monkeypatch.setattr(installer, "run", lambda *args, **kwargs: completed())
    monkeypatch.setattr(installer, "copy_source", lambda _, target: target.mkdir())

    def setup(*args, **kwargs):
        (root / "data").mkdir()
        (root / "data/account-marker").write_text("test-only-existing-account", encoding="utf-8")

    def startup_failure(*args, **kwargs):
        raise RuntimeError("Managed service did not become live")

    monkeypatch.setattr(installer, "setup_command", setup)
    monkeypatch.setattr(installer, "activate_portable", startup_failure)
    args = argparse.Namespace(uv=None, without_mediamtx=True, non_interactive=False)
    with pytest.raises(RuntimeError, match="did not become live"):
        installer.portable_install(
            tmp_path / "source", "a" * 40, replace(host, package_manager="dnf"), tmp_path, args
        )
    assert (root / "campix.json").is_file()
    assert (root / "data/account-marker").read_text(
        encoding="utf-8"
    ) == "test-only-existing-account"
    assert json.loads((root / "install.json").read_text(encoding="utf-8"))["status"] == "preparing"
    with pytest.raises(installer.InstallError, match="Existing files"):
        installer.ensure_portable_fresh(root)


def test_ambient_configuration_cannot_redirect_setup_to_user_state(
    installer, tmp_path, monkeypatch
):
    monkeypatch.setenv("CAMPIX_DATA_DIR", str(tmp_path / "unrelated-private-state"))
    monkeypatch.setenv("CAMPIX_DEMO", "true")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "unrelated-venv"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "unrelated-source"))
    env = installer.tool_environment(
        tmp_path / "uv", tmp_path / "isolated-env", tmp_path / "ffmpeg"
    )
    assert "CAMPIX_DATA_DIR" not in env and "CAMPIX_DEMO" not in env
    assert "PYTHONPATH" not in env
    assert env["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / "isolated-env")
    assert env["UV_PYTHON_DOWNLOADS"] == "never"


REVISION, NEWER, OLDER = "a" * 40, "b" * 40, "c" * 40
TEST_REPOSITORY = "https://example.invalid/campix.git"


def users(root):
    with closing(sqlite3.connect(root / "data/campix.sqlite3")) as database:
        return [row[0] for row in database.execute("SELECT name FROM users ORDER BY rowid")]


@pytest.fixture
def portable_installation(installer, tmp_path, monkeypatch):
    root = tmp_path / "CamPiX"
    (root / "releases" / REVISION).mkdir(parents=True)
    (root / "releases" / OLDER).mkdir()
    (root / "data").mkdir()
    with closing(sqlite3.connect(root / "data/campix.sqlite3")) as database:
        database.execute("CREATE TABLE users (name TEXT)")
        database.execute("INSERT INTO users VALUES ('before')")
        database.commit()
    marker = installer.installed_marker("portable-user", REVISION, OLDER, TEST_REPOSITORY)
    (root / "install.json").write_text(json.dumps(marker), encoding="utf-8")
    config = {"data_dir": str(root / "data"), "port": 8123, "ffmpeg": str(tmp_path / "ffmpeg")}
    (root / "campix.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(installer, "portable_root", lambda host: root)
    monkeypatch.setattr(
        installer, "validate_encoder", lambda host: (tmp_path / "ffmpeg", tmp_path / "ffprobe")
    )
    monkeypatch.setattr(installer, "provision_uv", lambda *args: tmp_path / "uv")
    monkeypatch.setattr(installer, "install_manager", lambda release, root: root / "bin/campix")
    monkeypatch.setattr(installer, "wait_for_free_ports", lambda settings: None)

    def build(source, release, uv, ffmpeg, windows, mediamtx):
        (release / ".venv").mkdir(parents=True)
        return installer.release_daemon(release, windows), None, {}

    monkeypatch.setattr(installer, "build_release", build)
    return root


def update_arguments():
    return argparse.Namespace(uv=None, repository=TEST_REPOSITORY)


def test_portable_update_switches_releases_and_keeps_one_previous(
    installer, host, portable_installation, tmp_path, monkeypatch
):
    root, events = portable_installation, []
    monkeypatch.setattr(installer, "stop_portable", lambda: events.append("stop"))
    monkeypatch.setattr(
        installer,
        "replace_portable",
        lambda daemon, config, release: events.append(release.name) or {"startup": "test"},
    )
    portable_host = replace(host, package_manager="dnf")
    result = installer.portable_update(
        tmp_path / "source", NEWER, portable_host, tmp_path, update_arguments()
    )
    marker = json.loads((root / "install.json").read_text(encoding="utf-8"))
    assert (marker["revision"], marker["previous"], marker["status"]) == (NEWER, REVISION, "ready")
    assert events == ["stop", NEWER]
    assert sorted(path.name for path in (root / "releases").iterdir()) == [REVISION, NEWER]
    (backup,) = (root / "backups").glob("campix-*.sqlite3")
    assert result["backup"] == str(backup) and users(root) == ["before"]


def test_failed_portable_update_restores_release_database_config_and_marker(
    installer, host, portable_installation, tmp_path, monkeypatch
):
    root, events = portable_installation, []
    marker_before = json.loads((root / "install.json").read_text(encoding="utf-8"))
    config_before = json.loads((root / "campix.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(installer, "stop_portable", lambda: None)

    def activate(daemon, config, release):
        events.append(release.name)
        if release.name == NEWER:
            # The new version migrates the database, then fails its health check.
            with closing(sqlite3.connect(root / "data/campix.sqlite3")) as database:
                database.execute("INSERT INTO users VALUES ('migrated')")
                database.commit()
            raise RuntimeError("Managed service did not become live")
        return {"startup": "test"}

    monkeypatch.setattr(installer, "replace_portable", activate)
    with pytest.raises(installer.InstallError, match=f"CamPiX {REVISION[:12]} was restored"):
        installer.portable_update(
            tmp_path / "source",
            NEWER,
            replace(host, package_manager="dnf"),
            tmp_path,
            update_arguments(),
        )
    assert events == [NEWER, REVISION]
    assert users(root) == ["before"]
    assert json.loads((root / "campix.json").read_text(encoding="utf-8")) == config_before
    assert json.loads((root / "install.json").read_text(encoding="utf-8")) == marker_before


@pytest.mark.parametrize(
    ("status", "revision", "message"),
    [("updating", NEWER, "did not finish"), ("ready", REVISION, "already installed")],
)
def test_portable_update_refuses_unfinished_or_repeated_updates(
    installer, host, portable_installation, tmp_path, monkeypatch, status, revision, message
):
    marker = json.loads((portable_installation / "install.json").read_text(encoding="utf-8"))
    marker["status"] = status
    (portable_installation / "install.json").write_text(json.dumps(marker), encoding="utf-8")
    monkeypatch.setattr(
        installer, "stop_portable", lambda: pytest.fail("Service must keep running")
    )
    with pytest.raises(installer.InstallError, match=message):
        installer.portable_update(tmp_path / "source", revision, host, tmp_path, update_arguments())


def test_failed_repair_puts_the_damaged_release_back(
    installer, host, portable_installation, tmp_path, monkeypatch
):
    release = portable_installation / "releases" / REVISION
    (release / "kept.txt").write_text("damaged but inspectable", encoding="utf-8")
    monkeypatch.setattr(installer, "stop_portable", lambda: None)

    def failing_build(*args):
        raise installer.InstallError("uv failed with exit code 2.")

    monkeypatch.setattr(installer, "build_release", failing_build)
    with pytest.raises(installer.InstallError, match="uv failed"):
        installer.portable_repair(tmp_path / "source", REVISION, host, tmp_path, update_arguments())
    assert (release / "kept.txt").read_text(encoding="utf-8") == "damaged but inspectable"
    assert not list((portable_installation / "releases").glob(".repair-*"))


def test_repair_rebuilds_the_installed_revision_without_touching_data(
    installer, host, portable_installation, tmp_path, monkeypatch
):
    release = portable_installation / "releases" / REVISION
    (release / "damaged.txt").write_text("broken", encoding="utf-8")
    activated = []
    monkeypatch.setattr(installer, "stop_portable", lambda: None)
    monkeypatch.setattr(
        installer,
        "replace_portable",
        lambda daemon, config, target: activated.append(target) or {"startup": "test"},
    )
    installer.portable_repair(tmp_path / "source", REVISION, host, tmp_path, update_arguments())
    assert activated == [release] and not (release / "damaged.txt").exists()
    assert (release / ".venv").is_dir() and users(portable_installation) == ["before"]
    assert not list((portable_installation / "releases").glob(".repair-*"))
    with pytest.raises(installer.InstallError, match="installed revision only"):
        installer.portable_repair(tmp_path / "source", NEWER, host, tmp_path, update_arguments())


def test_database_backups_keep_the_three_newest_copies(installer, portable_installation):
    backups = portable_installation / "backups"
    backups.mkdir()
    for age in (400, 300, 200):
        old = backups / f"campix-old-{age}.sqlite3"
        old.write_bytes(b"old")
        os.utime(old, (1_000_000 - age, 1_000_000 - age))
    newest = installer.backup_database(portable_installation, REVISION)
    assert sorted(path.name for path in backups.iterdir()) == sorted(
        ["campix-old-300.sqlite3", "campix-old-200.sqlite3", newest.name]
    )
    with closing(sqlite3.connect(newest)) as database:
        assert database.execute("SELECT name FROM users").fetchall() == [("before",)]


def test_restoring_a_backup_discards_newer_write_ahead_state(installer, portable_installation):
    backup = installer.backup_database(portable_installation, REVISION)
    data = portable_installation / "data"
    (data / "campix.sqlite3-wal").write_bytes(b"newer state")
    (data / "campix.sqlite3-shm").write_bytes(b"newer index")
    installer.restore_database(portable_installation, backup)
    assert not (data / "campix.sqlite3-wal").exists() and users(portable_installation) == ["before"]


def test_update_without_an_installation_is_rejected(installer, host, tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "NATIVE_MARKER", tmp_path / "missing/install.json")
    monkeypatch.setattr(installer, "portable_root", lambda host: tmp_path / "missing")
    args = argparse.Namespace(update=True, repair=False, uv=None, repository=TEST_REPOSITORY)
    with pytest.raises(installer.InstallError, match="No CamPiX installation"):
        installer.apply_to_installation(tmp_path, NEWER, host, {"packages": []}, args)


@pytest.fixture
def native_installation(installer, tmp_path, monkeypatch):
    marker_file, store = tmp_path / "etc/install.json", tmp_path / "store"
    marker_file.parent.mkdir()
    store.mkdir()
    (store / f"campix-{REVISION}.deb").write_bytes(b"test-only-previous-package")
    marker = installer.installed_marker("native-debian", REVISION, None, TEST_REPOSITORY)
    marker_file.write_text(json.dumps(marker), encoding="utf-8")
    monkeypatch.setattr(installer, "NATIVE_MARKER", marker_file)
    monkeypatch.setattr(installer, "NATIVE_PACKAGE_STORE", store)
    monkeypatch.setattr(installer, "install_host_packages", lambda *args: None)
    monkeypatch.setattr(
        installer, "validate_encoder", lambda host: (tmp_path / "ffmpeg", tmp_path / "ffprobe")
    )
    monkeypatch.setattr(installer, "provision_uv", lambda *args: tmp_path / "uv")
    monkeypatch.setattr(
        installer, "build_native_package", lambda *args: (tmp_path / "campix_new.deb", {})
    )
    commands = []

    def run(command, **kwargs):
        argv = [str(part) for part in command]
        commands.append(argv)
        stdout = '{"data_dir": "/var/lib/campix", "port": 8080}' if argv[1:2] == ["cat"] else "0"
        return completed(argv, stdout=stdout)

    monkeypatch.setattr(installer, "run", run)
    return SimpleNamespace(commands=commands, store=store, marker=marker_file)


def test_failed_native_update_reinstalls_the_previous_package_and_database(
    installer, host, native_installation, tmp_path, monkeypatch
):
    checks = iter([RuntimeError("Managed service did not become live"), None])

    def listener(port, running):
        error = next(checks)
        if error:
            raise error

    monkeypatch.setattr(installer, "wait_for_listener", listener)
    with pytest.raises(installer.InstallError, match=f"CamPiX {REVISION[:12]} was restored"):
        installer.native_update(
            tmp_path / "source",
            NEWER,
            host,
            {"packages": []},
            tmp_path,
            update_arguments(),
        )
    commands = native_installation.commands
    reinstalls = [command[-1] for command in commands if "--reinstall" in command]
    assert reinstalls == [
        str(tmp_path / "campix_new.deb"),
        str(native_installation.store / f"campix-{REVISION}.deb"),
    ]
    backup = next(command for command in commands if "backup" in command)
    restore = next(command for command in commands if command[3:4] == ["cp"])
    assert restore[4] == backup[-1]
    assert commands.index(backup) < commands.index(restore)
    marker = json.loads(native_installation.marker.read_text(encoding="utf-8"))
    assert marker["revision"] == REVISION


def test_successful_native_update_records_the_new_package_for_rollback(
    installer, host, native_installation, tmp_path, monkeypatch
):
    monkeypatch.setattr(installer, "wait_for_listener", lambda port, running: None)
    monkeypatch.setattr(installer, "NATIVE_MARKER", native_installation.marker)
    installer.native_update(
        tmp_path / "source", NEWER, host, {"packages": []}, tmp_path, update_arguments()
    )
    commands = native_installation.commands
    stored = native_installation.store / f"campix-{NEWER}.deb"
    assert [
        "sudo",
        "install",
        "-m",
        "0644",
        str(tmp_path / "campix_new.deb"),
        str(stored),
    ] in commands
    assert commands[-1][:4] == ["sudo", "install", "-m", "0644"]
    assert commands[-1][-1] == str(native_installation.marker)
