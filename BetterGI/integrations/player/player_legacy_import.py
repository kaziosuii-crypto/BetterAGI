#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将旧导演记忆复制到独立隔离区，不将其作为玩家已知事实召回。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Dict

from player_io import atomic_write_json, utc_now_iso
from shadow_recorder import _sanitize

MAX_BYTES = 1_048_576


def quarantine_legacy_file(source: Path | str, directory: Path | str) -> tuple[Path, bool]:
    """只读源文件；按原始内容哈希幂等复制，输出永不写入玩家知识库。"""
    source_path = Path(source)
    with source_path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("旧记忆文件超过 1 MiB")
    data: Any = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("旧记忆必须是 JSON 对象")
    digest = hashlib.sha256(raw).hexdigest()
    path = Path(directory) / (digest + ".json")
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict) or existing.get("content_sha256") != digest or existing.get("visibility") != "quarantined":
            raise ValueError("隔离记录已存在但内容不匹配，拒绝覆盖")
        return path, False
    record: Dict[str, Any] = {
        "schema_version": 1,
        "kind": "legacy_director_memory",
        "visibility": "quarantined",
        "verification": "unverified",
        "content_sha256": digest,
        "imported_at": utc_now_iso(),
        "content": _sanitize(data),
    }
    atomic_write_json(path, record)
    return path, True


def main() -> int:
    parser = argparse.ArgumentParser(description="复制旧导演 JSON 到不可召回的隔离区")
    parser.add_argument("--input", required=True, help="旧导演 memory.json")
    parser.add_argument("--quarantine-dir", required=True, help="独立隔离目录")
    args = parser.parse_args()
    try:
        path, created = quarantine_legacy_file(args.input, args.quarantine_dir)
    except (OSError, ValueError, UnicodeError) as exc:
        parser.exit(2, "隔离导入失败：{0}\n".format(type(exc).__name__))
    print("已隔离" if created else "已存在", path.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
