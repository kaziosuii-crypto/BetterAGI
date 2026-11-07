#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""往正在跑的 TUI 里插一句话，不抢游戏前台。

游戏必须保持前台（严格安全要求），所以直接切到终端打字会让角色停住。
这个脚本走 TUI 暴露的本机 HTTP 接口，效果等同于在终端里打字插话。

用法::

    python say.py "别往前了，回头看看"
    python say.py --state          # 只看它现在在干什么
    python say.py --stop           # 让它这一程收工
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = Path("E:/BetterAGI/_diag/tui-endpoint.json")


def load_endpoint() -> dict:
    if not ENDPOINT.exists():
        raise SystemExit("没找到 tui-endpoint.json：TUI 还没启动，或用了 --port 0")
    return json.loads(ENDPOINT.read_text(encoding="utf-8"))


def call(url: str, token: str, path: str, body: dict | None = None, timeout: float = 10.0) -> dict:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url + path,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="给 TUI 里的 lumi 插话 / 看状态")
    parser.add_argument("text", nargs="*", help="要说的话")
    parser.add_argument("--state", action="store_true", help="只看状态")
    parser.add_argument("--events", type=int, default=0, help="看最近 N 条事件")
    parser.add_argument("--stop", action="store_true", help="让它收工")
    args = parser.parse_args(argv)

    endpoint = load_endpoint()
    url, token = endpoint["url"], endpoint["token"]

    try:
        if args.stop:
            print(json.dumps(call(url, token, "/quit", {}), ensure_ascii=False))
            return 0
        if args.events:
            payload = call(url, token, f"/events?limit={max(1, args.events)}")
            for item in payload.get("events", []):
                print(f"{item['ts']} [{item['kind']}] {item['text']}")
            return 0
        text = " ".join(args.text).strip()
        if text:
            print(json.dumps(call(url, token, "/say", {"text": text}), ensure_ascii=False))
        state = call(url, token, "/state")
        line = (
            f"已玩 {state['elapsed']} · 第 {state['turn']} 轮 · {state['phase']}"
            f" · 工具 {state['tools']} · 上下文 {state['context']['text']}/{state['context']['keepText']}"
            f" · 插话队列 {state['queued']}"
        )
        print(line)
        if state.get("lastReply"):
            print(f"它刚才说：{state['lastReply'][:200]}")
        return 0
    except urllib.error.URLError as error:
        raise SystemExit(f"连不上 TUI（{url}）：{error}") from error


if __name__ == "__main__":
    raise SystemExit(main())
