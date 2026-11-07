#!/usr/bin/env python3
"""OS-held single-writer lease for a player home; never steals a stale lock file."""
from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO, Optional


class InstanceLockError(RuntimeError):
    """Another player process owns the home or locking is unavailable."""


class PlayerInstanceLock:
    """Hold a kernel lock until exit; crashes release it without deleting the file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._file: Optional[BinaryIO] = None
        self._pid: Optional[int] = None

    def __enter__(self) -> "PlayerInstanceLock":
        try:
            handle = self.path.open("a+b")
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._file = handle
            self._pid = os.getpid()
            return self
        except OSError as exc:
            if "handle" in locals():
                handle.close()
            raise InstanceLockError("玩家数据目录正被使用或无法安全加锁") from exc

    @property
    def is_held(self) -> bool:
        """只接受当前进程仍持有且路径未被替换的锁文件。"""
        handle = self._file
        if handle is None or handle.closed or self._pid != os.getpid():
            return False
        try:
            opened = os.fstat(handle.fileno())
            current = self.path.stat()
            return bool(opened.st_ino and current.st_ino and
                        (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino))
        except (OSError, ValueError):
            return False

    def __exit__(self, *_: object) -> None:
        handle = self._file
        self._file = None
        self._pid = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
