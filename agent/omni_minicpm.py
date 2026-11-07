"""MiniCPM-o 4.5 Realtime（chat 模式）快系统客户端。

用户 2026-11-06 要求把 omni（快系统/嘴）从 Qwen-Omni 换成
MiniCPM-o-4.5-Realtime，端点按官方文档
https://minicpmo45.modelbest.cn/docs/zh/realtime-api/overview/
给的 chat 模式协议：

    wss://api.modelbest.cn/v1/realtime?mode=chat&model=MiniCPM-o-4.5-Realtime
    Authorization: Bearer <API_KEY>

生命周期（与 Qwen 的 session.update / input_image_buffer 完全不同）：

    session.queue_done -> 客户端 session.init -> session.created
    -> input.append(messages + image) -> response.output.delta(kind=text)*
    -> response.done -> session.close -> session.closed

本类把上面这套事件翻译成 FastStream / FastObserver 已经在用的那套方法
（connect / seed_audio / send_frame / commit / create_response / collect /
cancel_inflight / ask / close / reconnect），上层一行都不用改。

两处刻意的语义差异（都因为 chat 模式是 turn-based）：

* seed_audio 是空操作：chat 模式没有音频缓冲，也不需要「先垫音频再发图」。
* 每轮回答完把会话标脏，下一次 create_response 先重连（约 160ms）。
  chat 会话是有状态的（服务端 kv_cache 会累加），而我们的 messages 每轮
  都是重新拼的；不换会话就会把上一轮的问答也带进上下文，直播间里表现为
  「嘴在回答上一个画面」。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from typing import Any, Callable, Dict, Optional

import omni

try:  # websockets 是本模块唯一的硬依赖
    import websockets
except Exception as error:  # pragma: no cover - 缺依赖时给出可读错误
    websockets = None  # type: ignore
    _IMPORT_ERROR: Optional[BaseException] = error
else:
    _IMPORT_ERROR = None

#: 默认端点（chat 模式）。model 查询串在 OmniConfig.resolve_url 里补。
MINICPM_URL = "wss://api.modelbest.cn/v1/realtime?mode=chat&model=MiniCPM-o-4.5-Realtime"
#: 一轮回答的默认上限（token）。观众要的是短句，不是小作文。
DEFAULT_MAX_NEW_TOKENS = 96


def _read_key_file() -> str:
    """从 keys/minicpm.key 读 key（不打印、不抛）。"""

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "keys", "minicpm.key")
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:  # noqa: BLE001 - 读不到就当没有
        return ""


def resolve_key(config: Optional["omni.OmniConfig"] = None) -> str:
    """MiniCPM 的 key：显式配置 > 环境变量 > keys/minicpm.key。"""

    if config is not None and config.api_key:
        return config.api_key
    env = (os.environ.get("MINICPM_API_KEY") or "").strip()
    if env:
        return env
    return _read_key_file()


class MiniCPMClient:
    """MiniCPM-o chat 模式客户端，接口与 omni.OmniClient 对齐。"""

    def __init__(self, config: Optional["omni.OmniConfig"] = None,
                 transport: Any = None,
                 *, transport_factory: Optional[Callable[[], Any]] = None):
        self.config = config or omni.OmniConfig()
        #: 注入的传输（测试用）：需要 async send_json/recv_json/close。
        self._transport = transport
        self._transport_factory = transport_factory
        self._owns_transport = transport is None
        self._ws: Any = None
        self._session_id = ""
        #: 最近一帧（base64 原始 JPEG）。chat 模式一次 input.append 只发一张。
        self._frame_b64: Optional[str] = None
        self._inflight = False
        #: 上一轮已经产生过回答：下一轮要换一条干净会话，别把旧问答带进来。
        self._need_reset = False
        self._broken = False
        self._broken_reason = ""
        self._closed = False

    # ------------------------------------------------------------ 状态
    @property
    def broken(self) -> bool:
        return self._broken or self._closed

    @property
    def broken_reason(self) -> str:
        if self._broken_reason:
            return self._broken_reason
        return "客户端已关闭" if self._closed else ""

    # ------------------------------------------------------------ 连接
    async def connect(self) -> "MiniCPMClient":
        if _IMPORT_ERROR is not None:
            raise omni.OmniError_("需要 websockets 才能连 MiniCPM-o：%r" % (_IMPORT_ERROR,))
        url = self.config.resolve_url()
        key = resolve_key(self.config)
        if not key:
            raise omni.OmniError_(
                "缺少 MiniCPM-o 的 key：设置 MINICPM_API_KEY，或写 keys/minicpm.key"
            )
        headers = {"Authorization": "Bearer " + key}
        try:
            self._ws = await websockets.connect(
                url, additional_headers=headers, max_size=None, open_timeout=20
            )
        except Exception as error:  # noqa: BLE001 - 建链失败要给出可读原因
            self._broken = True
            self._broken_reason = "MiniCPM 建链失败：%r" % (error,)
            raise omni.OmniError_("MiniCPM 建链失败：%r" % (error,)) from error
        await self._handshake()
        self._closed = False
        self._broken = False
        self._broken_reason = ""
        self._need_reset = False
        self._inflight = False
        return self

    async def _handshake(self) -> None:
        """等 queue_done → session.init → session.created。"""

        assert self._ws is not None
        deadline = time.monotonic() + 20.0
        created = False
        while time.monotonic() < deadline:
            event = await self._recv(timeout=deadline - time.monotonic())
            etype = (event or {}).get("type", "")
            if etype == "session.created":
                created = True
                self._session_id = str(event.get("session_id") or "")
                break
            if etype == "error":
                err = event.get("error") or {}
                raise omni.OmniError_("MiniCPM 会话错误：%s %s" % (
                    err.get("code", "?"), err.get("message", "")))
            if etype == "session.queue_done":
                await self._send({"type": "session.init", "payload": {}})
        if not created:
            raise omni.OmniError_("MiniCPM 握手超时：没等到 session.created")

    async def close(self) -> None:
        self._closed = True
        ws = self._ws
        if ws is None:
            return
        try:
            await asyncio.wait_for(ws.send(json.dumps(
                {"type": "session.close", "reason": "turn_done"})), timeout=1.0)
        except Exception:  # noqa: BLE001 - 关不掉就硬关
            pass
        try:
            await asyncio.wait_for(ws.close(), timeout=2.0)
        except Exception:  # noqa: BLE001
            pass
        self._ws = None

    async def reconnect(self) -> "MiniCPMClient":
        old = self._ws
        self._ws = None
        if old is not None:
            try:
                await asyncio.wait_for(old.close(), timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
        self._closed = False
        self._broken = False
        self._broken_reason = ""
        return await self.connect()

    # ------------------------------------------------------------ 发送
    async def _send(self, payload: Dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise omni.OmniError_("MiniCPM 客户端未连接：先 await connect()")
        await ws.send(json.dumps(payload, ensure_ascii=False))

    async def _recv(self, timeout: float) -> Dict[str, Any]:
        ws = self._ws
        if ws is None:
            raise omni.OmniError_("MiniCPM 客户端未连接：先 await connect()")
        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, float(timeout)))
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"type": "raw", "text": raw}

    async def seed_audio(self, pcm: bytes = b"") -> None:  # noqa: ARG002 - chat 模式无音频
        """chat 模式不需要音频垫片：保留方法只为兼容调用面。"""

    async def send_frame(self, jpeg: bytes) -> None:
        """记下最新一帧；真正发送发生在 create_response（一次一问）。"""

        if not jpeg:
            raise omni.OmniError_("MiniCPM 收到空帧")
        self._frame_b64 = omni.encode_jpeg_frame(jpeg)

    async def commit(self) -> None:
        """chat 模式没有 commit 阶段。"""

    async def create_response(self, instructions: str = "") -> None:
        """发起一轮 input.append（图片只能一起发）。"""

        if self._closed:
            raise omni.OmniError_("客户端已关闭：先 await reconnect() 再继续")
        if self._need_reset or self._broken or self._ws is None:
            await self.reconnect()
        messages = []
        system = self.config.instructions or ""
        if system:
            messages.append({"role": "system", "content": system})
        content = [{
            "type": "text",
            "text": instructions or "Look at the current game screen and answer briefly.",
        }]
        if self._frame_b64:
            content.append({"type": "image", "data": self._frame_b64})
        messages.append({"role": "user", "content": content})
        self._inflight = True
        await self._send({
            "type": "input.append",
            "input": {
                "messages": messages,
                "streaming": True,
                "generation": {"max_new_tokens": DEFAULT_MAX_NEW_TOKENS},
                "image": {"max_slice_nums": 1},
                "omni_mode": False,
                "tts": {"enabled": False},
                "use_tts_template": False,
                "enable_thinking": False,
            },
        })

    # ------------------------------------------------------------ 收
    async def collect(self, max_events: int = 512, *,
                      first_token_timeout_s: Optional[float] = None,
                      step_timeout_s: Optional[float] = None) -> "omni.OmniReply":
        first_budget = self.config.first_token_timeout_s if first_token_timeout_s is None \
            else float(first_token_timeout_s)
        step_budget = self.config.step_timeout_s if step_timeout_s is None \
            else float(step_timeout_s)
        reply = omni.OmniReply()
        started = time.monotonic()
        while reply.raw_events < max_events:
            budget = first_budget if reply.raw_events == 0 else step_budget
            if budget is not None and budget <= 0:
                budget = None
            try:
                event = await self._recv(timeout=budget if budget else 3600.0)
            except (asyncio.TimeoutError, TimeoutError):
                waited = "%.2fs" % float(budget) if budget else "不限"
                self._inflight = False
                self._broken = True
                self._broken_reason = "MiniCPM 收包超时"
                await self.close()
                raise omni.OmniTimeout("MiniCPM 收包超时：预算 " + waited) from None
            reply.raw_events += 1
            etype = (event or {}).get("type", "")
            if etype == "response.output.delta":
                if (event.get("kind") or "text") == "text":
                    reply.text += event.get("text") or ""
                continue
            if etype == "response.done":
                if not reply.text:
                    reply.text = event.get("text") or ""
                self._inflight = False
                self._need_reset = True
                break
            if etype == "error":
                err = event.get("error") or {}
                self._inflight = False
                self._broken = True
                self._broken_reason = "MiniCPM 服务端错误"
                raise omni.OmniError_("MiniCPM 服务端错误：%s %s" % (
                    err.get("code", "?"), err.get("message", "")))
        else:
            reply.truncated = True
        reply.latency_ms = (time.monotonic() - started) * 1000.0
        return reply

    async def cancel_inflight(self, budget_s: float = 8.0) -> None:  # noqa: ARG002
        """还没等到 response.done：直接换一条会话，迟到的包投递不进来。"""

        if not self._inflight:
            return
        self._inflight = False
        self._need_reset = True
        self._broken = True
        self._broken_reason = "上一轮没等到 response.done，已换会话"
        try:
            await self.close()
        except Exception:  # noqa: BLE001
            pass

    async def ask(self, jpeg: bytes, *, instructions: str = "",
                  audio: Optional[bytes] = None,  # noqa: ARG002
                  extra_frames: Any = None,  # noqa: ARG002
                  max_total_s: Optional[float] = None) -> "omni.OmniReply":
        """看一帧、给一句指令、收一轮文本（与 OmniClient.ask 同形）。"""

        if max_total_s is None:
            await self.send_frame(jpeg)
            await self.create_response(instructions)
            return await self.collect()
        budget = float(max_total_s)
        try:
            return await asyncio.wait_for(self._ask_round(jpeg, instructions), timeout=budget)
        except (asyncio.TimeoutError, TimeoutError):
            self._broken = True
            self._broken_reason = "MiniCPM ask 总预算超时"
            await self.close()
            raise omni.OmniTimeout("MiniCPM ask 总预算超时：%.2fs" % budget)

    async def _ask_round(self, jpeg: bytes, instructions: str) -> "omni.OmniReply":
        await self.send_frame(jpeg)
        await self.create_response(instructions)
        return await self.collect()

    async def __aenter__(self) -> "MiniCPMClient":
        return await self.connect()

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
