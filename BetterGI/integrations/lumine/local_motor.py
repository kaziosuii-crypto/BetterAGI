#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机确定性 MotorController：只执行 Player 意图，不产生任何目标。

用途：在没有可用的 Lumine 视觉语言模型（例如本机显存不足、远程主机身份
未验证）时，仍然让 Player 的意图能够驱动真实游戏输入。它不是模型替代品，
也不做认知：这里只有一组固定的、可审计的动作脚本，完全由意图的
``motor_mode`` 决定；没有意图时一律不动作。

支持的意图模式：

* ``navigate`` / ``explore`` —— 向前行走固定时长（按住 W）。
* ``combat`` —— 普通攻击连打（按住 LB）。
* ``interact`` —— 单次交互键（F）。
* ``wait`` / ``dialogue`` —— 什么都不做，对话推进与选项点击只属于宿主。

协议与 :mod:`lumine_bridge` 的 v2 Motor 侧一致：握手后先应答
``motor_capability_query``，收到 ``intent`` 后回 ``intent_ack``，每个
200ms 动作块都带着与意图一致的 ``intentId`` / ``motorGeneration`` 归属，
收到 ``cancel_intent`` 立即停止并回 ``cancel_ack``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import signal
import sys
from typing import Any, Optional, Sequence

LOG = logging.getLogger("local_motor")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8010
DEFAULT_PATH = "/ws/player"

PROTOCOL = "lumine-live-v2"
ACTION_INTERVAL_SECONDS = 0.2
MAX_ROUTINE_SECONDS = 3.0
# 长时验证可以把脚本时长放大到意图截止时间，默认仍按 3 秒收敛。
MAX_ROUTINE_SECONDS_LIMIT = 120.0
MAX_KEYS_PER_CHUNK = 4
CHUNKS_PER_ACTION = 6

# 每个动作模式对应的固定键位脚本。脚本只描述"怎么按"，不描述"做什么"。
ROUTINE_KEYS = {
    "navigate": ("W",),
    "explore": ("W",),
    "combat": ("LB",),
    "interact": ("F",),
}

IDLE_MODES = {"wait", "dialogue", "ui"}


def build_chunks(keys: Sequence[str], *, single_press: bool = False) -> list[dict[str, Any]]:
    """构造六个 33ms 键块；``single_press`` 时只在第一块按下。"""
    chunks: list[dict[str, Any]] = []
    for index in range(CHUNKS_PER_ACTION):
        pressed = list(keys)[:MAX_KEYS_PER_CHUNK] if (index == 0 or not single_press) else []
        chunks.append({"dx": 0, "dy": 0, "scroll": 0, "keys": pressed})
    return chunks


def attribution_of(message: dict[str, Any]) -> Optional[dict[str, Any]]:
    """从意图消息提取桥要求的精确归属字段集合。"""
    intent_id = message.get("intentId")
    generation = message.get("motorGeneration")
    if (not isinstance(intent_id, str) or not intent_id.strip()
            or isinstance(generation, bool) or not isinstance(generation, int) or generation < 0):
        return None
    result: dict[str, Any] = {"intentId": intent_id, "motorGeneration": generation}
    group = message.get("actionGroupId")
    if isinstance(group, str) and group.strip():
        result["actionGroupId"] = group
    return result


class LocalMotor:
    """把单个意图转换为有限时长的固定输入脚本。"""

    def __init__(self, routine_seconds: float = MAX_ROUTINE_SECONDS) -> None:
        # 单个脚本最长时长；上限防止一次意图无限占住输入。
        self.routine_seconds = max(0.0, min(float(routine_seconds), MAX_ROUTINE_SECONDS_LIMIT))
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._intent: Optional[dict[str, Any]] = None
        self._attribution: Optional[dict[str, Any]] = None
        self._task: Optional[asyncio.Task[None]] = None

    async def send(self, payload: dict[str, Any]) -> bool:
        """串行发送一条 JSON 消息；连接已关闭时返回 False。"""
        ws = self._ws
        if ws is None:
            return False
        async with self._send_lock:
            try:
                await ws.send(json.dumps(payload, ensure_ascii=False))
                return True
            except Exception:  # 连接中断由读取循环负责清理
                return False

    async def stop_routine(self) -> None:
        """取消正在执行的动作脚本；不发送任何停止键，脚本本身会自然松开。"""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def run_routine(self, intent: dict[str, Any], attribution: dict[str, Any]) -> None:
        """按意图模式执行固定脚本，最多 MAX_ROUTINE_SECONDS 秒。"""
        mode = str(intent.get("motor_mode") or "navigate")
        keys = ROUTINE_KEYS.get(mode)
        if keys is None:
            # 等待、对话、未知模式都不产生输入。
            return
        deadline_ms = intent.get("deadline_ms")
        budget = self.routine_seconds
        if isinstance(deadline_ms, int) and not isinstance(deadline_ms, bool) and deadline_ms > 0:
            budget = min(budget, deadline_ms / 1000.0)
        single = mode == "interact"
        chunks = build_chunks(keys, single_press=single)
        loop = asyncio.get_running_loop()
        ends_at = loop.time() + budget
        try:
            while loop.time() < ends_at:
                if self._attribution != attribution:
                    return
                payload = {"type": "action", "chunks": chunks, "absolute": None}
                payload.update(attribution)
                if not await self.send(payload):
                    return
                await asyncio.sleep(ACTION_INTERVAL_SECONDS)
                if single:
                    return
        except asyncio.CancelledError:
            raise

    async def on_intent(self, message: dict[str, Any]) -> None:
        """接收新意图：先停旧脚本，再确认并启动固定脚本。"""
        attribution = attribution_of(message)
        if attribution is None:
            await self.send({"type": "error", "code": "bad_intent_attribution"})
            return
        await self.stop_routine()
        self._intent = message
        self._attribution = attribution
        await self.send({"type": "intent_ack", "accepted": True, **attribution})
        self._task = asyncio.create_task(self.run_routine(message, attribution))

    async def on_cancel(self, message: dict[str, Any]) -> None:
        """取消当前意图；只回执，不伪造已完成的语义结果。"""
        generation = message.get("motorGeneration")
        await self.stop_routine()
        self._intent = None
        self._attribution = None
        ack: dict[str, Any] = {"type": "cancel_ack"}
        if isinstance(generation, int) and not isinstance(generation, bool) and generation >= 0:
            ack["motorGeneration"] = generation
        await self.send(ack)

    async def handle(self, message: dict[str, Any]) -> None:
        """分发桥发来的消息；未知类型一律忽略。"""
        kind = message.get("type")
        if not isinstance(kind, str):
            return
        kind = kind.strip().lower()
        if kind == "motor_capability_query":
            nonce = message.get("nonce")
            generation = message.get("connectionGeneration")
            if not isinstance(nonce, str) or not nonce:
                return
            await self.send({
                "type": "motor_capability_ack",
                "nonce": nonce,
                "connectionGeneration": generation if isinstance(generation, int) else 0,
                "protocol": PROTOCOL,
                "intentRequired": True,
                "intentForcesMotor": True,
                # 本实现不是模型，也绝不冒充模型；mock 必须为 False 才允许上线。
                "mock": False,
            })
            return
        if kind == "intent":
            await self.on_intent(message)
            return
        if kind == "cancel_intent":
            await self.on_cancel(message)
            return
        if kind == "guidance":
            # 指导只作为提示，确定性脚本不据此改写目标。
            LOG.info("收到指导（不改变脚本）：%s", str(message.get("text"))[:80])
            return


def build_handler(token: str, expected_path: str, modern: bool = True,
                  routine_seconds: float = MAX_ROUTINE_SECONDS):
    """构造连接处理函数；测试可复用同一鉴权与分发逻辑。"""
    expected = "Bearer " + token

    async def handler(*handler_args: Any) -> None:
        if modern:
            connection = handler_args[0]
            request = getattr(connection, "request", None)
            path = getattr(request, "path", None)
            headers = getattr(request, "headers", {}) or {}
        else:  # pragma: no cover - 旧版签名
            connection, path = handler_args[0], handler_args[1]
            headers = getattr(connection, "request_headers", {}) or {}
        if path != expected_path or headers.get("Authorization") != expected:
            await connection.close(code=1008, reason="unauthorized")
            return
        motor = LocalMotor(routine_seconds)
        motor._ws = connection
        LOG.info("Motor 已连接：%s", path)
        try:
            async for raw in connection:
                if not isinstance(raw, str):
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    await motor.handle(message)
        except Exception as exc:  # 连接异常不终止服务
            LOG.warning("Motor 连接异常：%s", exc)
        finally:
            await motor.stop_routine()
            if motor._ws is connection:
                motor._ws = None
            LOG.info("Motor 已断开：%s", path)

    return handler


async def _serve(args: argparse.Namespace) -> int:
    """启动 WebSocket 服务；每个连接独立一个 LocalMotor 实例。"""
    try:
        from websockets.asyncio.server import serve as ws_serve
        modern = True
    except ImportError:  # pragma: no cover - 旧版 websockets 回退
        from websockets.server import serve as ws_serve
        modern = False

    handler = build_handler(args.token, args.path, modern, args.routine_seconds)
    stop = asyncio.Event()

    async with ws_serve(handler, args.host, args.port, max_size=4 * 1024 * 1024) as server:
        LOG.info("本机 Motor 监听 ws://%s:%d%s（只执行意图，不做认知）", args.host, args.port, args.path)
        serve_task = asyncio.create_task(server.serve_forever())
        stop_task = asyncio.create_task(stop.wait())
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows 限制
                pass
        await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        stop_task.cancel()
        serve_task.cancel()
        for task in (stop_task, serve_task):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="本机确定性 MotorController：只执行 Player 意图，不做认知、不挑选目标。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="仅支持本机回环地址")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口")
    parser.add_argument("--path", default=DEFAULT_PATH, help="WebSocket 路径，必须与桥配置一致")
    parser.add_argument("--token", default=os.environ.get("LOCAL_MOTOR_TOKEN", ""),
                        help="Bridge 连接所需的 Bearer 令牌；省略时读取 LOCAL_MOTOR_TOKEN")
    parser.add_argument("--token-file", help="从本机文件读取令牌，避免出现在命令行")
    parser.add_argument("--routine-seconds", type=float, default=MAX_ROUTINE_SECONDS,
                        help="单个意图的固定脚本最长秒数，默认 %(default)s")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    if args.token_file:
        try:
            args.token = open(args.token_file, "r", encoding="utf-8").read().strip()
        except OSError as exc:
            print(f"无法读取令牌文件：{exc}", file=sys.stderr)
            return 1
    if not args.token or len(args.token) < 16:
        print("必须提供至少 16 字符的 Bearer 令牌（--token 或 --token-file）。", file=sys.stderr)
        return 1
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("本机 Motor 只允许监听回环地址，避免把输入通道暴露到网络。", file=sys.stderr)
        return 1
    if not 1024 <= args.port <= 65535:
        print("端口必须在 1024–65535 之间。", file=sys.stderr)
        return 1
    try:
        return asyncio.run(_serve(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
