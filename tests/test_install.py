"""Installer preflight and failure tests never change real host/application state."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from dataclasses import replace
from pathlib import Path

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
        installer.native_install(source, host, installer.install_plan(host, True), scratch, args)
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
        installer.native_install(source, host, installer.install_plan(host, True), scratch, args)
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
