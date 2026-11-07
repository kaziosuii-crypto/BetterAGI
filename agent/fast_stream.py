"""快系统连续视频流：把 Omni 当实时双工模型用，而不是一帧一问的 VLM。

qwen3.8-omni-flash-realtime 的 input_image_buffer.append 本来就是流式入口
（官方建议 1 帧/秒，视频历史 240 秒）。一帧一问等于每次都重新喂一遍上下文，
既贵又把延迟摊在每次请求上；正确姿势是持续灌帧，需要结论时才让它开口。

用法：
    stream = FastStream(); stream.start()
    stream.push(jpeg_bytes)          # 非阻塞，只保留最新一帧
    stream.ask("现在该按什么键")      # 需要结论时才开口
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any, Optional

import omni
from fast_system import (
    INTENT_INSTRUCTIONS,
    MAX_FRAME_BYTES,
    FastSystemError_,
    data_url_to_bytes,
    parse_intent,
    resolve_dashscope_key,
    resolve_omni_key,
)
from intent import Intent


def _to_jpeg(raw: bytes) -> bytes:
    """PNG/其他格式 → JPEG（Omni 只收 FFD8FF 开头的 JPEG）。"""

    import io as _io

    try:
        from PIL import Image
    except Exception as error:  # noqa: BLE001
        raise FastSystemError_("需要 pillow 才能把 PNG 转成 JPEG：" + str(error))
    img = Image.open(_io.BytesIO(raw)).convert("RGB")
    buf = _io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


class FastStream:
    """连续视频流观察者：按帧率灌画面，按需开口。"""

    def __init__(self, config: Optional[omni.OmniConfig] = None, fps: float = 1.0,
                 pump: bool = True):
        self.config = config or omni.OmniConfig(
            api_key=resolve_omni_key(), model=omni.DEFAULT_MODEL
        )
        #: pump=False：由调用方自己控制灌帧节奏（快系统执行器里"嘴"和"手"并行，
        #: 灌帧必须跟着决策节拍走，不能让后台泵循环和 ask() 抢同一个缓冲）。
        self.pump = bool(pump)
        self.interval = 1.0 / max(0.2, float(fps))
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._client: Optional[omni.OmniClient] = None
        self._pending: Optional[bytes] = None
        self._pump: Optional[asyncio.Task] = None
        self.frames_sent = 0
        self.last_text = ""
        self.last_latency_ms = 0.0

    # ------------------------------------------------------------ 生命周期
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            return self._loop
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(target=run, name="fast-stream-loop", daemon=True)
        thread.start()
        ready.wait(5.0)
        self._loop, self._thread = loop, thread
        return loop

    def _submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())

    def start(self) -> None:
        async def _do() -> None:
            # 2026-11-06：MiniCPM-o 与 Qwen-Omni 共用这一套调用面，这里只按配置分叉。
            client = omni.build_client(self.config)
            await client.connect()
            self._client = client
            if self.pump:
                self._pump = asyncio.ensure_future(self._pump_loop())

        self._submit(_do()).result(timeout=30)

    async def _pump_loop(self) -> None:
        """按帧率把最新一帧灌进会话；没有新帧就不重复发送。"""

        while True:
            await asyncio.sleep(self.interval)
            frame, self._pending = self._pending, None
            if frame is None or self._client is None:
                continue
            try:
                await self._client.seed_audio()
                await self._client.send_frame(frame)
                self.frames_sent += 1
            except Exception:  # noqa: BLE001 - 断流等下次重连
                continue

    def push(self, image: bytes | str) -> None:
        """放进待发槽：只保留最新一帧（旧帧对决策没价值）。"""

        if isinstance(image, str):
            image = data_url_to_bytes(image)
        # 快系统只收 JPEG：游戏截帧是 PNG，必须先转码，别指望服务端宽容。
        if image and not image.startswith(b"\xff\xd8\xff"):
            image = _to_jpeg(image)
        if not image:
            raise FastSystemError_("空帧")
        if len(image) > MAX_FRAME_BYTES:
            raise FastSystemError_("帧太大：" + str(len(image)) + "B")
        self._pending = image

    def ask_reply(self, instructions: str = INTENT_INSTRUCTIONS, timeout: float = 45.0):
        """在已有视频流上下文上让模型开口，返回整条 Reply（要工具调用就拿这个）。"""

        if self._client is None:
            self.start()

        async def _do() -> omni.OmniReply:
            assert self._client is not None
            # 上一轮没等到 response.done（服务端 error / 提前抛）就得先撤掉，
            # 否则这一轮 create_response 会撞 "already has an active response"。
            await self._client.cancel_inflight()
            frame, self._pending = self._pending, None
            # 每轮都要垫音频：commit 会清空缓冲。没有新帧的那一轮（比如这一轮
            # 只有 suggest_intent 没跟新帧）也要垫，否则 commit 报
            # "buffer too small, or have no audio"（实测 2026-11-04）。
            await self._client.seed_audio()
            if frame is not None:
                await self._client.send_frame(frame)
            await self._client.commit()
            await self._client.create_response(instructions)
            return await self._client.collect()

        started = time.time()
        reply = self._submit(_do()).result(timeout=timeout)
        self.last_text = reply.text
        self.last_latency_ms = (time.time() - started) * 1000.0
        return reply

    def ask(self, instructions: str = INTENT_INSTRUCTIONS, timeout: float = 45.0) -> str:
        """只要文本的旧口子（执行器/慢系统都用它）。"""

        reply = self.ask_reply(instructions=instructions, timeout=timeout)
        return reply.text

    def suggest_intent(self, instructions: str = INTENT_INSTRUCTIONS) -> Optional[Intent]:
        intent = parse_intent(self.ask(instructions=instructions, timeout=45.0))
        if intent is not None:
            intent.meta["source"] = "fast-stream"
            intent.meta["frames_sent"] = self.frames_sent
            intent.meta["latency_ms"] = round(self.last_latency_ms, 1)
        return intent

    def stop(self) -> None:
        loop, client, pump = self._loop, self._client, self._pump
        if loop is None:
            return

        async def _do() -> None:
            if pump is not None:
                pump.cancel()
            if client is not None:
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass

        try:
            asyncio.run_coroutine_threadsafe(_do(), loop).result(timeout=15)
        except Exception:  # noqa: BLE001
            pass
        loop.call_soon_threadsafe(loop.stop)
        self._loop = self._thread = self._client = None
        self._pump = None


def build_fast_stream(fps: float = 1.0, pump: bool = True) -> Optional[FastStream]:
    """没 key 返回 None（慢系统单腿跑）。"""

    if os.environ.get("BETTERAGI_FAST", "1") in ("0", "false", "off"):
        return None
    key = resolve_omni_key()
    if not key:
        return None
    return FastStream(omni.OmniConfig(api_key=key, model=omni.DEFAULT_MODEL), fps=fps, pump=pump)
