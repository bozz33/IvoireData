from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO


class LockTimeout(TimeoutError):
    pass


def _try_lock(handle: TextIO) -> bool:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(handle: TextIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(path: Path, *, timeout: float = 30.0, poll: float = 0.05) -> Iterator[None]:
    """Cross-process lock backed by a shared file.

    Docker services share `.ivoiredata`, so POSIX `flock` coordinates API, scheduler
    and one-shot containers. A Windows fallback keeps direct non-Docker use safe too.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep at least one byte so msvcrt.locking can lock a real byte range.
    handle = path.open("a+", encoding="utf-8")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write("0")
            handle.flush()
        deadline = time.monotonic() + max(0.0, float(timeout))
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise LockTimeout(f"timed out waiting for lock: {path}")
            time.sleep(max(0.005, float(poll)))
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()
