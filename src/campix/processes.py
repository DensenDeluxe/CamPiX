"""Child-process policy for the long-running service."""

from __future__ import annotations

import subprocess
import sys
import threading

# The Windows background service runs under windowless pythonw.exe. Each console child
# (MediaMTX, FFmpeg) would otherwise open its own visible console window; closing that
# window terminates the child, and its supervisor relaunches it with a new window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


def bounded_output(command: list[str], *, limit: int, timeout: float) -> bytes:
    """Return a short-lived child's diagnostic output, stopping it on overrun or timeout."""
    output = bytearray()
    with subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=NO_WINDOW,
    ) as process:

        def collect():
            while chunk := process.stderr.read(8192):
                output.extend(chunk)
                if len(output) > limit:
                    process.kill()
                    return

        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        try:
            process.wait(timeout=timeout)
        finally:
            if process.poll() is None:
                process.kill()
            reader.join(timeout=5)
    if len(output) > limit:
        raise ValueError("Child process output exceeds its size limit")
    return bytes(output)


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
