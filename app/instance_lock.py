from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from typing import Iterator


LOCK_ERROR_MESSAGE = "Bot da dang chay o mot process khac. Hay tat instance cu truoc."


def _lock_handle(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_handle(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def single_instance_lock(lock_file: Path) -> Iterator[None]:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = open(lock_file, "a+", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(LOCK_ERROR_MESSAGE) from exc
    try:
        # Always lock from byte 0 so every process contends on the same region.
        try:
            _lock_handle(handle)
        except OSError as exc:
            raise RuntimeError(LOCK_ERROR_MESSAGE) from exc
        yield
    finally:
        try:
            _unlock_handle(handle)
        except OSError:
            pass
        handle.close()
