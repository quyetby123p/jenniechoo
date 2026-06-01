from __future__ import annotations

from contextlib import contextmanager
import msvcrt
from pathlib import Path
from typing import Iterator


@contextmanager
def single_instance_lock(lock_file: Path) -> Iterator[None]:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = open(lock_file, "a+", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            "Bot da dang chay o mot process khac. Hay tat instance cu truoc."
        ) from exc
    try:
        # Always lock from byte 0 so every process contends on the same region.
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError(
                "Bot da dang chay o mot process khac. Hay tat instance cu truoc."
            ) from exc
        yield
    finally:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        handle.close()
