"""Small cross-process file lock and atomic text write (no third-party deps).

Used by the registry writer (REQ-012) and the requirements index (REQ-002).
The lock is a sibling `<file>.lock` created with O_EXCL; a lock older than
`stale_after` seconds (a killed writer) is broken so the next reconcile can
repair state instead of hanging.
"""
from __future__ import annotations

import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class LockTimeout(RuntimeError):
    pass


@contextmanager
def file_lock(target: Path, *, timeout: float = 10.0, stale_after: float = 30.0) -> Iterator[None]:
    lock = target.with_name(target.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale_after:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise LockTimeout(f"could not lock {target.name}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def write_text_atomic(path: Path, text: str) -> None:
    """Write via a temp file in the same folder, then os.replace (retry briefly on Windows sharing errors)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    with open(temp, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    for attempt in range(20):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 19:
                temp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)
