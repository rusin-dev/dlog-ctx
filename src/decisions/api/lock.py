"""Cross-platform advisory file lock (``docs/api/.lock``).

POSIX uses ``fcntl.flock``; Windows uses ``msvcrt.locking`` (POSIX file locks
do not exist there, and silently skipping locking would let a scheduled sync
and a manual ``indev finish`` race each other).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - platform dependent
    _fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - platform dependent
    _msvcrt = None  # type: ignore[assignment]


class LockUnavailable(RuntimeError):
    """Raised when a non-blocking lock attempt fails."""


class FileLock:
    """A tiny advisory lock backed by a 1-byte file lock."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self, *, blocking: bool = False, timeout: float = 0.0) -> bool:
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            deadline = time.monotonic() + max(timeout, 0.0)
            while True:
                if self._try_lock(fd):
                    self._fd = fd
                    return True
                if not blocking or time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(0.1)
        except OSError:
            os.close(fd)
            return False

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            elif _msvcrt is not None:  # pragma: no cover - Windows
                _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _try_lock(self, fd: int) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                return True
            if _msvcrt is not None:  # pragma: no cover - Windows
                _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
                return True
        except OSError:
            return False
        return True  # pragma: no cover - no locking primitive available

    # -- context manager -------------------------------------------------
    def __enter__(self) -> FileLock:
        if not self.acquire(blocking=True, timeout=30):
            raise LockUnavailable(f"cannot acquire {self.path}")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
