#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把设定/攻略资料导入 OpenViking，作为导演的长期记忆。

示例::

    E:\\BetterAGI\\openviking\\.venv\\Scripts\\python.exe memory_ingest.py ^
        --path E:\\BetterAGI\\BetterGI\\lore ^
        --to viking://~/resources/genshin

需要 OpenViking 服务已启动（默认 http://127.0.0.1:1933），且运行环境
安装了 ``openviking-sdk``。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, List

from openviking_sdk import SyncHTTPClient

SUPPORTED = {".md", ".markdown", ".txt", ".json"}


def collect(path: Path) -> List[Path]:
    """收集目录下的可导入文件；单文件直接返回。"""
    if path.is_file():
        return [path] if path.suffix.lower() in SUPPORTED else []
    files: List[Path] = []
    for child in sorted(path.rglob("*")):
        if child.is_file() and child.suffix.lower() in SUPPORTED:
            files.append(child)
    return files


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入资料到 OpenViking")
    parser.add_argument("--path", required=True, help="文件或目录")
    parser.add_argument(
        "--to",
        default="viking://~/resources/genshin",
        help="OpenViking 目标资源 URI",
    )
    parser.add_argument("--url", default="http://127.0.0.1:1933")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--no-wait", action="store_true", help="不等待摘要/索引完成")
    args = parser.parse_args(list(argv) if argv is not None else None)

    source = Path(args.path)
    files = collect(source)
    if not files:
        print(f"没有可导入的文件：{source}", file=sys.stderr)
        return 2

    client = SyncHTTPClient(url=args.url, api_key=args.api_key or None)
    client.initialize()
    failures = 0
    try:
        for file_path in files:
            try:
                result = client.add_resource(
                    str(file_path),
                    to=args.to,
                    wait=not args.no_wait,
                )
                uri = result.get("uri") if isinstance(result, dict) else ""
                print(f"OK  {file_path.name} -> {uri or args.to}", flush=True)
            except Exception as exc:  # noqa: BLE001 - 单个文件失败继续
                failures += 1
                print(f"ERR {file_path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        client.close()
    print(f"完成：{len(files) - failures}/{len(files)} 个文件")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
