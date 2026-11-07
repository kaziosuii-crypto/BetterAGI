"""把一条意图交给 Lumine，并让它在真实游戏里执行指定时长。

这个命令行是 DSH 插件（dsh-betteragi）调用 Lumine 的唯一入口：
插件负责"看"和"说"，本脚本负责走完本机 Live 输入租约链并把意图投给桥接。

安全边界：
* 只允许回环地址；token 只从文件或环境变量读取，不接受命令行明文。
* 必须先拿到租约、绑定到桥接、双方提交后才投意图；任何一步失败都直接退出，
  绝不降级为"无租约直接发键"。
* 退出时一定撤销租约；桥接与 BetterGI 会各自撤销仍然按住的输入。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

PLAYER_DIR = Path(__file__).resolve().parent
if str(PLAYER_DIR) not in sys.path:
    sys.path.insert(0, str(PLAYER_DIR))

import player_active_gate as gate  # noqa: E402

DEFAULT_BGI = "http://127.0.0.1:30648"
DEFAULT_BRIDGE = "http://127.0.0.1:18311"
DEFAULT_SESSION = "integration"
DEFAULT_BGI_TOKEN_FILE = r"E:\BetterAGI\tools\bettergi_token.txt"
DEFAULT_BRIDGE_TOKEN_FILE = r"E:\BetterAGI\tools\intent_token.txt"
MAX_SECONDS = 600
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _loopback(url: str, name: str) -> str:
    """只接受本机回环地址，避免把租约凭据发到别处。"""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS:
        raise SystemExit(f"{name} 必须是本机 http 回环地址，收到：{url}")
    return url.rstrip("/")


def _token(value: Optional[str], path: str, name: str) -> str:
    """优先读显式文件，其次读环境变量指向的文件；绝不回显内容。"""
    candidate = value or path
    try:
        token = Path(candidate).read_text(encoding="utf-8").strip()
    except OSError as error:
        raise SystemExit(f"读取 {name} 失败：{error}") from error
    if len(token) < 16:
        raise SystemExit(f"{name} 太短，拒绝启动")
    return token


async def wait_bridge(bridge: str, token: str, session_id: str, timeout: float = 12.0) -> Optional[dict]:
    """等桥接确认它与本次 Live 会话的连接，否则 /lease/bind 会 409。"""
    headers = {"Authorization": "Bearer " + token}
    deadline = time.monotonic() + timeout
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
        while time.monotonic() < deadline:
            try:
                async with session.get(bridge + "/feedback", headers=headers) as response:
                    if response.status == 200:
                        data = json.loads(await response.text())
                        if (data.get("verifiedLiveSessionId") == session_id
                                and data.get("bettergiConnectionActive") is True):
                            return data
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                pass
            await asyncio.sleep(0.4)
    return None


async def snapshot(bgi: str, bgi_token: str, bridge: str, bridge_token: str) -> Dict[str, Any]:
    """取一次宿主与桥接的状态，用于结尾汇报。"""
    result: Dict[str, Any] = {}
    timeout = aiohttp.ClientTimeout(total=3)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(bgi + "/api/v1/agent/live/status",
                                   headers={"Authorization": "Bearer " + bgi_token}) as response:
                if response.status == 200:
                    live = json.loads(await response.text())
                    result["appliedActions"] = live.get("appliedActions")
                    result["pressedKeys"] = live.get("pressedKeys")
                    result["starved"] = live.get("starved")
                    result["dialogueGate"] = (live.get("safety") or {}).get("dialogueGate")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            pass
        try:
            async with session.get(bridge + "/feedback",
                                   headers={"Authorization": "Bearer " + bridge_token}) as response:
                if response.status == 200:
                    feedback = json.loads(await response.text())
                    result["current"] = (feedback.get("current") or {}).get("intent_id")
                    result["motorConnectionActive"] = feedback.get("motorConnectionActive")
                    result["inputReleaseUnconfirmed"] = feedback.get("inputReleaseUnconfirmed")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            pass
    return result


async def run(args: argparse.Namespace) -> int:
    """走完租约链、投出意图、持续续期，最后如实汇报看到的状态。"""
    bgi = _loopback(args.bgi, "--bgi")
    bridge = _loopback(args.bridge, "--bridge")
    bgi_token = _token(args.bgi_token_file, DEFAULT_BGI_TOKEN_FILE, "BetterGI token")
    bridge_token = _token(args.bridge_token_file, DEFAULT_BRIDGE_TOKEN_FILE, "桥接 token")
    session_id = args.session

    def emit(payload: Dict[str, Any]) -> None:
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    if await wait_bridge(bridge, bridge_token, session_id) is None:
        emit({"ok": False, "stage": "bridge", "error": "桥接未确认当前 Live 会话（先启动桥接与 live 会话）"})
        return 2

    lease = gate.LiveInputLease(bgi, bgi_token, session_id=session_id)
    for _ in range(20):
        if await lease.acquire():
            break
        await asyncio.sleep(1.0)
    else:
        await lease.close()
        emit({"ok": False, "stage": "acquire", "error": "拿不到 Live 输入租约（严格安全未就绪或游戏不在前台）"})
        return 3

    stop = asyncio.Event()
    failures: list = []

    async def on_failure() -> None:
        failures.append(time.monotonic())

    # 租约 TTL 只有 3 秒：心跳必须在绑定与提交之前就开始。
    beats = asyncio.create_task(lease.heartbeat(stop, on_failure, interval=0.5))
    try:
        request_id = await lease.publish_binding(bridge, bridge_token)
        if request_id is None or not await lease.wait_for_binding(
                bridge, bridge_token, request_id, stop, status="prepared_bound"):
            emit({"ok": False, "stage": "bind", "error": "桥接没有接受这次租约绑定"})
            return 4
        if not await lease.commit_owner():
            emit({"ok": False, "stage": "commit_owner", "error": "宿主没有确认租约提交"})
            return 4
        if not await lease.commit_bridge(stop):
            emit({"ok": False, "stage": "commit_bridge", "error": "桥接没有完成提交（可能是上一次释放未确认）"})
            return 4

        intent = {
            "intent_id": args.intent_id or "dsl-{}".format(int(time.time() * 1000)),
            "goal": args.goal,
            "reason": args.reason,
            "motor_mode": args.motor_mode,
            "constraints": list(args.constraint or []),
            "interrupt_conditions": list(args.interrupt or []),
            "deadline_ms": int((args.seconds + 5) * 1000),
        }
        headers = {"Authorization": "Bearer " + bridge_token}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as session:
            async with session.post(bridge + "/intent", json=intent, headers=headers) as response:
                body = await response.text()
                if response.status != 200:
                    emit({"ok": False, "stage": "intent", "error": "桥接拒绝意图：{}".format(body[:200])})
                    return 5
                accepted = json.loads(body) if body else {}

            started = time.monotonic()
            samples = []
            while time.monotonic() - started < args.seconds:
                await asyncio.sleep(min(2.0, max(0.2, args.sample)))
                state = await snapshot(bgi, bgi_token, bridge, bridge_token)
                state["atSec"] = round(time.monotonic() - started, 1)
                samples.append(state)
                if failures:
                    break
        emit({
            "ok": not failures,
            "stage": "done" if not failures else "lease_lost",
            "intentId": intent["intent_id"],
            "motorGeneration": accepted.get("motorGeneration"),
            "heldSec": round(time.monotonic() - started, 1),
            "samples": samples,
            "error": None if not failures else "租约在途中失效（多半是游戏失去前台或识别到对话）",
        })
        return 0 if not failures else 7
    finally:
        stop.set()
        beats.cancel()
        try:
            await beats
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 心跳退出不应掩盖结果
            pass
        await lease.close()


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把意图交给 Lumine 并在真实游戏里执行")
    parser.add_argument("--goal", required=True, help="要执行的目标（自然语言，给策略模型看）")
    parser.add_argument("--reason", default="", help="为什么现在做这件事")
    parser.add_argument("--motor-mode", default="navigate",
                        choices=["navigate", "explore", "combat", "interact", "wait", "dialogue", "ui"])
    parser.add_argument("--seconds", type=float, default=30.0, help="执行时长（秒），上限 600")
    parser.add_argument("--sample", type=float, default=2.0, help="采样间隔（秒）")
    parser.add_argument("--constraint", action="append", help="约束，可重复")
    parser.add_argument("--interrupt", action="append", help="中断条件，可重复")
    parser.add_argument("--intent-id", default=None)
    parser.add_argument("--session", default=DEFAULT_SESSION)
    parser.add_argument("--bgi", default=os.environ.get("BETTERAGI_BGI", DEFAULT_BGI))
    parser.add_argument("--bridge", default=os.environ.get("BETTERAGI_BRIDGE", DEFAULT_BRIDGE))
    parser.add_argument("--bgi-token-file", default=os.environ.get("BETTERAGI_BGI_TOKEN_FILE"))
    parser.add_argument("--bridge-token-file", default=os.environ.get("BETTERAGI_BRIDGE_TOKEN_FILE"))
    args = parser.parse_args(argv)
    if not args.goal.strip():
        parser.error("--goal 不能为空")
    args.seconds = max(1.0, min(MAX_SECONDS, args.seconds))
    return args


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
