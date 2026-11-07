"""MiniCPM-o 4.5 Realtime 官方 WebSocket 实时多模态客户端。

协议规范基于 ModelBest 平台文档：
https://platform.modelbest.cn/console/docs/api/realtime
端点: wss://api.modelbest.cn/v1/realtime?mode=chat&model=MiniCPM-o-4.5-Realtime
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import time
from typing import Any, AsyncGenerator, Callable, Dict, Optional
import websockets

DEFAULT_API_URL = "wss://api.modelbest.cn/v1/realtime?mode=chat&model=MiniCPM-o-4.5-Realtime"
POSSIBLE_KEY_PATHS = [
    Path(__file__).resolve().parent / "keys" / "minicpm.key",
    Path(__file__).resolve().parent.parent / "keys" / "minicpm.key",
]
FALLBACK_API_KEY = "sk-live-l8dH04OnSnTrMIgtptpt9jXh6zczN7USdNVS4xce_34"

DEFAULT_SYSTEM_PROMPT = """你是一位富有激情、反应敏锐、幽默风趣的游戏与桌面实时实况主播。
你正在盯着玩家当前的电脑屏幕。你的任务是：
1. 仔细观察屏幕画面的动态变化（角色动作、战斗、地图探索、菜单点击、操作失误、胜利等细节）；
2. 用 1 到 3 句口语化、接地气、生动且富有节目效果的中文进行实时解说；
3. 直奔主题，不要废话，不要复述死板规则，像一位真正的主播一样实时跟观众互动吐槽或喝彩。
"""


def load_api_key(explicit_key: str = "") -> str:
    """读取 API Key：优先显式传参 > 环境变量 > keys/minicpm.key > 内置 fallback。"""
    if explicit_key and explicit_key.strip():
        return explicit_key.strip()
    
    env_key = os.environ.get("MINICPM_API_KEY", "").strip()
    if env_key:
        return env_key
        
    for key_path in POSSIBLE_KEY_PATHS:
        if key_path.exists():
            try:
                k = key_path.read_text(encoding="utf-8").strip()
                if k:
                    return k
            except Exception:
                pass
            
    return FALLBACK_API_KEY


class MiniCPMRealtimeClient:
    """MiniCPM-o 4.5 Realtime 异步客户端。"""

    def __init__(
        self,
        api_key: str = "",
        url: str = DEFAULT_API_URL,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_new_tokens: int = 120
    ):
        self.api_key = load_api_key(api_key)
        self.url = url
        self.system_prompt = system_prompt
        self.max_new_tokens = max_new_tokens
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.session_id: str = ""
        self._connected: bool = False
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """建立连接并完成握手。"""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        self.ws = await websockets.connect(
            self.url,
            additional_headers=headers,
            open_timeout=20,
            ping_interval=20,
            ping_timeout=20,
            max_size=None
        )
        
        # 握手等待 session.queue_done -> session.init -> session.created
        created = False
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            msg = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
            if isinstance(msg, bytes):
                msg = msg.decode("utf-8", "replace")
            evt = json.loads(msg)
            etype = evt.get("type")
            
            if etype == "session.queue_done":
                await self.ws.send(json.dumps({"type": "session.init", "payload": {}}))
            elif etype == "session.created":
                self.session_id = evt.get("session_id", "")
                created = True
                break
            elif etype == "error":
                err = evt.get("error", {})
                raise RuntimeError(f"MiniCPM 握手错误: {err.get('message', evt)}")

        if not created:
            raise TimeoutError("MiniCPM 握手超时：未收到 session.created")

        self._connected = True

    async def ensure_connected(self) -> None:
        """检查并确保连接存活，若断开则自愈重连。"""
        if self.ws is None or not self._connected:
            await self.reconnect()

    async def reconnect(self) -> None:
        """断开并重新建立连接。"""
        await self.close()
        await self.connect()

    async def close(self) -> None:
        """优雅关闭会话。"""
        self._connected = False
        ws = self.ws
        self.ws = None
        if ws is not None:
            try:
                await asyncio.wait_for(
                    ws.send(json.dumps({"type": "session.close", "reason": "user_exit"})),
                    timeout=1.0
                )
            except Exception:
                pass
            try:
                await asyncio.wait_for(ws.close(), timeout=2.0)
            except Exception:
                pass

    async def generate_commentary_stream(
        self,
        jpeg_bytes: bytes,
        user_prompt: str = "看着当前画面，给出最新解说。"
    ) -> AsyncGenerator[str, None]:
        """向模型发送单帧画面，并异步流式 yield 解说文本片段。"""
        async with self._lock:
            # 每次请求前确保连接可用
            await self.ensure_connected()

            jpeg_b64 = base64.b64encode(jpeg_bytes).decode("utf-8")
            messages = [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image", "data": jpeg_b64}
                    ]
                }
            ]

            payload = {
                "type": "input.append",
                "input": {
                    "messages": messages,
                    "streaming": True,
                    "generation": {
                        "max_new_tokens": self.max_new_tokens,
                        "temperature": 0.7
                    },
                    "image": {"max_slice_nums": 1},
                    "omni_mode": False,
                    "tts": {"enabled": False}
                }
            }

            try:
                assert self.ws is not None
                await self.ws.send(json.dumps(payload, ensure_ascii=False))

                while True:
                    msg = await asyncio.wait_for(self.ws.recv(), timeout=30.0)
                    if isinstance(msg, bytes):
                        msg = msg.decode("utf-8", "replace")
                    evt = json.loads(msg)
                    etype = evt.get("type")

                    if etype == "response.output.delta":
                        text_delta = evt.get("text", "")
                        if text_delta:
                            yield text_delta
                    elif etype == "response.done":
                        # 该轮解说结束。为防止服务端历史上下文冗余污染下一轮，标记或按官方推荐重置
                        break
                    elif etype == "error":
                        err = evt.get("error", {})
                        raise RuntimeError(f"MiniCPM 生成错误: {err.get('message', evt)}")

            except (websockets.ConnectionClosed, asyncio.TimeoutError) as ex:
                self._connected = False
                raise RuntimeError(f"MiniCPM 连接异常: {ex}") from ex
            finally:
                # 轮次结束后换新 session，保持每一帧解说的即时性与敏锐度
                await self.reconnect()
