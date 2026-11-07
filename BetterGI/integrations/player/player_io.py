#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""玩家数据的原子读写工具。

玩家档案与 resume 状态必须始终是「完整的旧版本」或「完整的新版本」：
进程被杀、断电或 Windows 杀毒软件短暂占用目标文件时，不能留下半截 JSON。
因此所有写入都走同目录临时文件、fsync，再用 ``os.replace`` 原子替换。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

# Windows 上 os.replace 可能因杀毒扫描/编辑器占用短暂失败，做有限重试。
ATOMIC_RETRIES = 3
ATOMIC_RETRY_DELAY = 0.05


def utc_now_iso(now_ms: Optional[int] = None) -> str:
    """返回 UTC ISO-8601 时间；测试可注入毫秒时间戳。"""
    if now_ms is None:
        moment = datetime.now(timezone.utc)
    else:
        moment = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_epoch_ms(value: Any) -> Optional[int]:
    """把 Live 的 ``timestampUtc`` 转成毫秒；无法解析时返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def clean_text(value: str) -> str:
    """移除孤立代理项；模型输出里的 ``\\ud800`` 会让 UTF-8 写入直接失败。"""
    if not isinstance(value, str):
        return value
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return value.encode("utf-8", errors="replace").decode("utf-8")
    return value


def clean_json(value: Any) -> Any:
    """递归清理 JSON 结构中的不可编码字符串。"""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        return {clean_text(str(key)): clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(item) for item in value]
    return value


def atomic_write_text(
    path: Path | str,
    text: str,
    *,
    retries: int = ATOMIC_RETRIES,
    retry_delay: float = ATOMIC_RETRY_DELAY,
) -> Path:
    """原子写入 UTF-8 文本；失败时清理临时文件并抛出最后一个 OSError。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    last_error: Optional[OSError] = None

    for attempt in range(max(1, retries)):
        # mkstemp 保证临时文件与目标同目录（同一卷），replace 才是原子的。
        handle_fd, tmp_name = tempfile.mkstemp(
            prefix="." + target.name + ".", suffix=".tmp", dir=str(target.parent)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, target)
            return target
        except OSError as exc:
            last_error = exc
            try:
                tmp_path.unlink()
            except OSError:
                pass
            # Windows 共享冲突（winerror 32/33）不一定映射为 PermissionError。
            retryable = isinstance(exc, PermissionError) or getattr(
                exc, "winerror", None
            ) in (5, 32, 33)
            if retryable and attempt + 1 < retries:
                time.sleep(retry_delay * (attempt + 1))
                continue
            raise
        except UnicodeError:
            # 不可编码文本不能留下半截临时文件；调用方应先用 clean_text。
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise

    # 循环至少要执行一次；这里只是让类型检查器与静态阅读更明确。
    if last_error is not None:
        raise last_error
    raise OSError("原子写入失败：{0}".format(target))


def atomic_write_json(path: Path | str, data: Any, *, indent: int = 2) -> Path:
    """原子写入 JSON；中文原样保存，便于直接阅读与手工修正。"""
    text = json.dumps(clean_json(data), ensure_ascii=False, indent=indent) + "\n"
    return atomic_write_text(path, text)


def read_json(path: Path | str) -> Any:
    """读取 JSON；容忍 Windows 编辑器写入的 UTF-8 BOM。"""
    with open(path, "r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def backup_corrupt_file(path: Path | str, now_ms: Optional[int] = None) -> Path:
    """把损坏文件改名保留，返回备份路径；绝不静默覆盖原内容。"""
    source = Path(path)
    moment = (
        datetime.now(timezone.utc)
        if now_ms is None
        else datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    )
    stamp = moment.strftime("%Y%m%d-%H%M%S")
    target = source.with_name("{0}.corrupt-{1}".format(source.name, stamp))
    counter = 1
    while target.exists():
        target = source.with_name(
            "{0}.corrupt-{1}-{2}".format(source.name, stamp, counter)
        )
        counter += 1
    os.replace(source, target)
    return target


@contextmanager
def file_lock(path: Path | str, *, timeout: float = 5.0) -> Iterator[None]:
    """跨进程排他锁，避免两个进程读改写同一状态文件时互相覆盖。

    使用 ``O_CREAT|O_EXCL`` 创建锁文件；超过 ``timeout`` 视为锁失效并强行
    接管，避免崩溃残留的锁文件永久阻塞写入。
    """
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + max(0.0, timeout)
    fd = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() >= deadline:
                try:
                    os.unlink(str(lock_path))
                except OSError:
                    pass
                deadline = time.time() + 1.0
                continue
            time.sleep(0.02)
        except OSError:
            # 无法创建锁文件时退化为无锁写入，调用方仍会做版本与冲突检查。
            yield
            return
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        fd = None
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(str(lock_path))
        except OSError:
            pass
