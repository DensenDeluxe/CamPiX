"""Child-process policy for the long-running service."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable

# The Windows background service runs under windowless pythonw.exe. Each console child
# (MediaMTX, FFmpeg) would otherwise open its own visible console window; closing that
# window terminates the child, and its supervisor relaunches it with a new window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


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
    """Return a short-lived child's diagnostic output, stopping it on overrun or timeout."""
    output = bytearray()
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=NO_WINDOW,
    )
    try:
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
    finally:
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        finally:
            process.stderr.close()


def contain_children() -> bool:
    """Make Windows terminate remaining children when this service process exits.

    Hidden children cannot be closed by the user. A child that outlived a crashed or
    killed service would keep the managed loopback ports and block the next start.
    Python launchers let grandchildren break away from their own jobs, so the service
    joins a nested job without breakaway. systemd and launchd already stop the service's
    children on Linux and macOS. Raises OSError when Windows refuses the job.
    """
    if sys.platform != "win32":
        return False
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
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not (
        kernel32.SetInformationJobObject(
            job,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise ctypes.WinError(error)
    # The non-inheritable handle intentionally stays open: Windows closes it when this
    # process exits, which terminates every child still assigned to the job.
    return True
