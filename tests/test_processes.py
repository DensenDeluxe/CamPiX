import ast
import io
import logging
import os
import subprocess
import sys
import time
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


def wait_for_pid_exit(pid, timeout=1):
    # A Windows job can report zero active processes while a terminated process's
    # PID remains briefly visible. Wait for kernel bookkeeping, without killing it.
    deadline = time.monotonic() + timeout
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.005)


def test_bounded_output_preserves_binary_stderr_and_discards_stdout():
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(b'ignored'); sys.stderr.buffer.write(b'\\x00\\xff')",
    ]
    assert processes.bounded_output(command, limit=2, timeout=5) == b"\x00\xff"


def test_owned_process_closes_all_pipes_after_buffered_stdin_breaks():
    with processes.owned_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) as child:
        # Keep this small write in BufferedWriter. Killing the child makes its
        # implicit close-time flush fail with EPIPE on POSIX and Windows.
        assert child.stdin.write(b"unflushed test-only input") == 25
    assert child.poll() is not None
    assert child.stdin.closed and child.stdout.closed and child.stderr.closed
    wait_for_pid_exit(child.pid)
    assert not psutil.pid_exists(child.pid)


def test_pipe_cleanup_reports_other_errors_after_closing_remaining_owned_handles():
    raw, output, error = io.BytesIO(), io.BytesIO(), io.BytesIO()

    class FailedBufferedWriter:
        def __init__(self):
            self.raw = raw

        def close(self):
            raise OSError("Injected close-time I/O failure")

    child = SimpleNamespace(stdin=FailedBufferedWriter(), stdout=output, stderr=error)
    with pytest.raises(ExceptionGroup, match="pipe cleanup failed") as failure:
        processes._close_process_pipes(child)
    assert len(failure.value.exceptions) == 1
    assert isinstance(failure.value.exceptions[0], OSError)
    assert raw.closed and output.closed and error.closed


def test_bounded_output_stops_on_small_overrun_before_timeout(tmp_path):
    pid_path = tmp_path / "child.pid"
    command = [
        sys.executable,
        "-c",
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        "sys.stderr.buffer.write(b'x' * 1024); sys.stderr.flush(); time.sleep(30)",
        str(pid_path),
    ]
    with pytest.raises(ValueError, match="output exceeds"):
        processes.bounded_output(command, limit=512, timeout=2)
    wait_for_pid_exit(int(pid_path.read_text()))
    assert not psutil.pid_exists(int(pid_path.read_text()))


def test_bounded_output_timeout_reaps_the_child(tmp_path):
    pid_path = tmp_path / "child.pid"
    command = [
        sys.executable,
        "-c",
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)",
        str(pid_path),
    ]
    with pytest.raises(subprocess.TimeoutExpired):
        processes.bounded_output(command, limit=512, timeout=0.5)
    wait_for_pid_exit(int(pid_path.read_text()))
    assert not psutil.pid_exists(int(pid_path.read_text()))


def test_bounded_output_does_not_wait_for_an_inherited_stderr_pipe(tmp_path):
    pid_path = tmp_path / "grandchild.pid"
    command = [
        sys.executable,
        "-c",
        "import pathlib, subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "sys.stderr.buffer.write(b'done'); sys.stderr.flush()",
        str(pid_path),
    ]
    started = time.monotonic()
    try:
        assert processes.bounded_output(command, limit=512, timeout=1) == b"done"
        assert time.monotonic() - started < 1.5
    finally:
        if pid_path.exists():
            try:
                child = psutil.Process(int(pid_path.read_text()))
                child.kill()
                child.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher and nested job ownership")
@pytest.mark.parametrize("overrun", [False, True])
@pytest.mark.parametrize(
    "launcher", [sys.executable, getattr(sys, "_base_executable", sys.executable)]
)
def test_bounded_output_terminates_launcher_and_its_descendants(tmp_path, overrun, launcher):
    pid_path = tmp_path / "descendant.pid"
    child = (
        "import os,pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
    )
    parent = (
        "import pathlib,subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]]); "
        "deadline=time.monotonic()+2; "
        "\nwhile not pathlib.Path(sys.argv[1]).exists() and time.monotonic()<deadline:time.sleep(0.005)"
        "\nsys.stderr.buffer.write(b'x'*1024 if sys.argv[3]=='1' else b'');sys.stderr.flush();"
        "time.sleep(30)"
    )
    expected = ValueError if overrun else subprocess.TimeoutExpired
    with pytest.raises(expected):
        processes.bounded_output(
            [launcher, "-c", parent, str(pid_path), child, "1" if overrun else "0"],
            limit=512,
            timeout=0.5,
        )
    assert pid_path.exists(), "The nested child was never started"
    pid = int(pid_path.read_text())
    try:
        psutil.Process(pid).wait(timeout=1)
    except psutil.NoSuchProcess:
        pass
    wait_for_pid_exit(pid)
    assert not psutil.pid_exists(pid)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows inherited writer ownership")
def test_bounded_output_reaps_inherited_writer_after_parent_exits(tmp_path):
    pid_path = tmp_path / "inherited-writer.pid"
    child = (
        "import os,pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
    )
    parent = (
        "import pathlib,subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]]); "
        "deadline=time.monotonic()+2; "
        "\nwhile not pathlib.Path(sys.argv[1]).exists() and time.monotonic()<deadline:time.sleep(0.005)"
        "\nsys.stderr.buffer.write(b'done');sys.stderr.flush()"
    )
    started = time.monotonic()
    assert (
        processes.bounded_output(
            [getattr(sys, "_base_executable", sys.executable), "-c", parent, str(pid_path), child],
            limit=512,
            timeout=1,
        )
        == b"done"
    )
    assert time.monotonic() - started < 1.5
    assert pid_path.exists()
    pid = int(pid_path.read_text())
    wait_for_pid_exit(pid)
    assert not psutil.pid_exists(pid), "The inherited writer survived its owning operation"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows per-process ownership")
def test_bounded_process_containment_refusal_executes_no_child(monkeypatch):
    def refuse():
        raise OSError("Test-only job assignment refusal")

    def unexpected_spawn(*args, **kwargs):
        pytest.fail("Uncontained child was spawned")

    monkeypatch.setattr(processes, "_WindowsJob", refuse)
    monkeypatch.setattr(processes.subprocess, "Popen", unexpected_spawn)
    with pytest.raises(OSError, match="assignment refusal"):
        processes.bounded_output(
            [sys.executable, "-c", "raise SystemExit(0)"], limit=512, timeout=1
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows suspended process ownership")
@pytest.mark.parametrize("stage", ["assign", "resume"])
def test_bounded_process_containment_failure_cleans_suspended_child(tmp_path, monkeypatch, stage):
    executed, children = tmp_path / "should-not-exist", []
    spawn = subprocess.Popen

    def track(*args, **kwargs):
        child = spawn(*args, **kwargs)
        children.append(child)
        return child

    def refuse(*args):
        raise OSError("Test-only child containment refusal")

    monkeypatch.setattr(processes.subprocess, "Popen", track)
    if stage == "assign":
        monkeypatch.setattr(processes._WindowsJob, "assign", refuse)
    else:
        monkeypatch.setattr(processes, "_resume_windows_process", refuse)
    with pytest.raises(OSError, match="containment refusal"):
        processes.bounded_output(
            [
                sys.executable,
                "-c",
                "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('ran')",
                str(executed),
            ],
            limit=512,
            timeout=1,
        )
    assert len(children) == 1 and children[0].returncode is not None
    assert not executed.exists()
    wait_for_pid_exit(children[0].pid)
    assert not psutil.pid_exists(children[0].pid)


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
            if not (
                flags
                and isinstance(flags[0], ast.Name)
                and flags[0].id in {"NO_WINDOW", "NO_WINDOW_SUSPENDED"}
            ):
                unguarded.append(f"{path.name}:{node.lineno}")
    # A windowless service would open one console window per unguarded child.
    assert {"capture.py", "exports.py", "mediamtx.py", "provision.py", "recording.py"} <= launched
    assert unguarded == []
    if sys.platform == "win32":
        assert processes.NO_WINDOW_SUSPENDED & subprocess.CREATE_NO_WINDOW
        assert processes.NO_WINDOW_SUSPENDED & processes.CREATE_SUSPENDED


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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process group ownership")
def test_owned_process_group_cleanup_preserves_an_unrelated_group():
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    unrelated = subprocess.Popen(command, start_new_session=True)
    try:
        with processes.owned_process(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) as child:
            assert os.getpgid(child.pid) == child.pid
            assert os.getpgid(child.pid) != os.getpgrp()
            assert os.getpgid(child.pid) != os.getpgid(unrelated.pid)
            child_pid = child.pid
        assert child.returncode is not None
        assert not psutil.pid_exists(child_pid)
        assert unrelated.poll() is None, "Owned cleanup terminated an unrelated process group"
    finally:
        unrelated.kill()
        unrelated.wait(timeout=5)


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
