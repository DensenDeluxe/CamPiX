"""Child-process policy for the long-running service."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

# The Windows background service runs under windowless pythonw.exe. Each console child
# (MediaMTX, FFmpeg) would otherwise open its own visible console window; closing that
# window terminates the child, and its supervisor relaunches it with a new window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CREATE_SUSPENDED = 0x00000004
NO_WINDOW_SUSPENDED = NO_WINDOW | (CREATE_SUSPENDED if sys.platform == "win32" else 0)

JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
THREAD_SNAPSHOT = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002


def _output_reader(descriptor: int) -> Callable[[int], bytes | None]:
    """Read available pipe bytes without waiting for a child or inherited writer."""
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        peek = kernel32.PeekNamedPipe
        peek.argtypes = (
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        )
        peek.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(descriptor)

        def read(size: int) -> bytes | None:
            available = wintypes.DWORD()
            if not peek(handle, None, 0, None, ctypes.byref(available), None):
                error = ctypes.get_last_error()
                if error in {109, 232}:  # ERROR_BROKEN_PIPE, ERROR_NO_DATA
                    return b""
                raise ctypes.WinError(error)
            return os.read(descriptor, min(size, available.value)) if available.value else None

    else:
        import select

        def read(size: int) -> bytes | None:
            ready, _, _ = select.select([descriptor], [], [], 0)
            return os.read(descriptor, size) if ready else None

    return read


def bounded_output(command: list[str], *, limit: int, timeout: float) -> bytes:
    """Return bounded diagnostics and stop the complete owned process tree."""
    output = bytearray()
    with owned_process(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    ) as process:
        read = _output_reader(process.stderr.fileno())
        deadline = time.monotonic() + timeout
        while True:
            exited = process.poll() is not None
            chunk = read(min(8192, max(1, limit + 1 - len(output))))
            if chunk:
                output.extend(chunk)
                if len(output) > limit:
                    raise ValueError("Child process output exceeds its size limit")
            elif exited:
                return bytes(output)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            if not chunk:
                time.sleep(min(0.01, remaining))


@contextmanager
def owned_process(command: list[str], **options) -> Iterator[subprocess.Popen]:
    """Contain a short-lived process tree before it can execute, then reap it.

    Launchers can delegate execution and leave inherited pipe writers running when
    only the parent process is terminated. Each Windows operation gets
    its own nested job before the suspended child is resumed. POSIX children get a
    private process group, so cleanup never targets the caller's group.
    """
    job = _WindowsJob() if sys.platform == "win32" else None
    process = None
    try:
        process = subprocess.Popen(
            command,
            creationflags=NO_WINDOW_SUSPENDED,
            start_new_session=sys.platform != "win32",
            **options,
        )
        if job is not None:
            job.assign(process._handle)
            _resume_windows_process(process.pid)
        yield process
    finally:
        try:
            if job is not None:
                job.close(terminate=True)
            elif process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            if process is not None:
                try:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                    if sys.platform == "win32":
                        process._handle.Close()
                finally:
                    _close_process_pipes(process)


def _close_process_pipes(process: subprocess.Popen) -> None:
    """Close every owned pipe even when a killed child's buffered stdin cannot flush."""
    errors = []
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except Exception as exc:
            if not isinstance(exc, BrokenPipeError):
                errors.append(exc)
            # BufferedWriter.close normally closes its raw handle even on EPIPE.
            # Preserve ownership if another I/O error interrupted that cleanup.
            raw = getattr(stream, "raw", None)
            if raw is not None and not raw.closed:
                try:
                    raw.close()
                except Exception as raw_error:
                    errors.append(raw_error)
    if errors:
        raise ExceptionGroup("Child process pipe cleanup failed", errors)


class _WindowsJob:
    """A non-inheritable job that forbids descendant breakaway."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.QueryInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
        )
        self.kernel32 = kernel32
        self.handle = kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            self.handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(self.handle)
            self.handle = None
            raise ctypes.WinError(error)

    def assign(self, process_handle):
        import ctypes

        if not self.kernel32.AssignProcessToJobObject(self.handle, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self, *, terminate=False):
        import ctypes
        from ctypes import wintypes

        if self.handle is None:
            return
        try:
            if terminate:
                if not self.kernel32.TerminateJobObject(self.handle, 1):
                    raise ctypes.WinError(ctypes.get_last_error())

                class Accounting(ctypes.Structure):
                    _fields_ = [
                        ("TotalUserTime", ctypes.c_int64),
                        ("TotalKernelTime", ctypes.c_int64),
                        ("ThisPeriodTotalUserTime", ctypes.c_int64),
                        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                        ("TotalPageFaultCount", wintypes.DWORD),
                        ("TotalProcesses", wintypes.DWORD),
                        ("ActiveProcesses", wintypes.DWORD),
                        ("TotalTerminatedProcesses", wintypes.DWORD),
                    ]

                accounting = Accounting()
                deadline = time.monotonic() + 5
                while True:
                    if not self.kernel32.QueryInformationJobObject(
                        self.handle,
                        JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                        ctypes.byref(accounting),
                        ctypes.sizeof(accounting),
                        None,
                    ):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if accounting.ActiveProcesses == 0:
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Owned child process tree did not stop in time")
                    time.sleep(0.005)
        finally:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


def _resume_windows_process(pid: int) -> None:
    """Resume the sole primary thread after assigning a suspended child to its job."""
    import ctypes
    from ctypes import wintypes

    class ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry))
    kernel32.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry))
    kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot = kernel32.CreateToolhelp32Snapshot(THREAD_SNAPSHOT, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        available = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        examined = 0
        while available and examined < 100_000:
            if entry.th32OwnerProcessID == pid:
                thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if not thread:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if kernel32.ResumeThread(thread) != 1:
                        raise OSError("Owned child primary thread could not be resumed")
                    return
                finally:
                    kernel32.CloseHandle(thread)
            examined += 1
            available = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        raise OSError("Owned child primary thread was not found")
    finally:
        kernel32.CloseHandle(snapshot)


def contain_children() -> bool:
    """Make Windows terminate remaining children when this service process exits.

    Python launchers let grandchildren break away from their own jobs, so the service
    joins a nested job without breakaway. systemd and launchd already stop service
    children on POSIX. Refused Windows containment raises OSError for visible logging.
    """
    if sys.platform != "win32":
        return False
    job = _WindowsJob()
    try:
        job.assign(job.kernel32.GetCurrentProcess())
    except OSError:
        job.close()
        raise
    # The non-inheritable handle intentionally stays open: Windows closes it when this
    # process exits, which terminates every child still assigned to the job.
    return True
