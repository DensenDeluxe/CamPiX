from __future__ import annotations


class ProcessLock:
    """OS-owned lock released even on SIGKILL; stale PID files cannot block boot."""

    def __init__(self, data_dir):
        self.path = data_dir / "campix.lock"
        self.stream = None

    def acquire(self):
        import os

        self.stream = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise RuntimeError("Another CamPiX service owns this data directory") from exc

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None
