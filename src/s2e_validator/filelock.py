"""An exclusive lock on a file, released by the operating system if we die.

A lock row in a database would survive a crash and need clearing by hand. An
OS file lock does not: when the process ends, however it ends, the lock goes
with it. Used to guarantee one writer per store across processes.
"""

from __future__ import annotations

import os
from pathlib import Path


class LockBusy(RuntimeError):
    """Another process (or another handle in this one) holds the lock."""


class FileLock:
    def __init__(self, path: Path):
        self._path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock(fd)
        except OSError:
            os.close(fd)
            raise LockBusy(f"{self._path} is locked by another process") from None
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            _unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


if os.name == "nt":
    import msvcrt

    def _lock(fd: int) -> None:
        # One byte is enough: the lock is on the range, not the contents.
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
