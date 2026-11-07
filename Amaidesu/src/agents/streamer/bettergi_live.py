"""BetterGI 实时解说的只读接入：把游戏侧已生成的解说接进主播的发言管线。

BetterGI 的实时会话 WebSocket 同时推送画面帧（base64 JPEG，高频）与解说文本。
本模块只取解说：画面帧与其它消息在类型白名单处就被丢掉，既不进任何下游，
更不会进语言模型。解说文本已经是成品台词，直接交给发言管线播出
（语音合成、字幕、业务事件，虚拟形象由 ``streamer.speech`` 订阅方反射情绪）。

组件归类：主播 Agent 的内部件（与嘴、命令解析同层），不是采集器也不注册为工具。
采集器产出的数据事件会被 Planner 当成输入重新决策，而这里的解说已经是最终成品，
再走一遍决策既无必要也会把游戏解说改写成主播的话；因此它与主播共用同一条发言
管线——同一条播放队列、同一个语音引擎、同一张嘴，主播自主发言与游戏解说不会叠音。

安全边界：
- 只读一个方向：连接建立后不向 BetterGI 发送任何应用消息（不发解说、不发动作、
  不发指导），不接触 BetterGI 的动作执行面。
- 凭证只从环境变量读（``BETTERGI_LIVE_TOKEN``），不落配置文件、不进 URL。
- 地址必须是本机回环地址，避免把凭证发到别处。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from collections import OrderedDict
from typing import Any, Optional
from urllib.parse import urlsplit

from websockets.asyncio.client import connect

from src.modules.logging import get_logger

from .config import BetterGILiveConfig
from .speech_dispatcher import SpeechDispatcher
from .tools.reply_tool import coerce_emotion

logger = get_logger("StreamerAgent.BetterGILive")

#: 接收环境变量名：BetterGI 外部访问令牌（配置文件与 URL 都不放凭证）
TOKEN_ENV_VAR = "BETTERGI_LIVE_TOKEN"

#: 单条消息的字符上限：超过即丢弃（画面帧就是这种量级，不必解析）
_MAX_MESSAGE_CHARS = 64 * 1024
#: 单条解说的字数上限
_MAX_TEXT_CHARS = 4_000
#: 判重表保留的条目数（防无限增长）
_DEDUP_LIMIT = 4096
#: 单条 WebSocket 消息的字节上限（服务端画面帧远小于此值）
_MAX_FRAME_BYTES = 16 * 1024 * 1024


def validate_live_url(url: str) -> None:
    """校验实时会话地址：必须是本机回环的 WebSocket 地址，且不含凭证与查询参数。

    Raises:
        ValueError: 地址不合规时抛出（接入启动时调用，不合法则本次接入不启动）。
    """
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise ValueError("BetterGI 实时地址必须以 ws:// 或 wss:// 开头")
    if not parts.hostname:
        raise ValueError("BetterGI 实时地址缺少主机名")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("BetterGI 实时地址不得携带用户名、密码、查询参数或片段")
    host = parts.hostname.lower()
    if host != "localhost":
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError as exc:
            raise ValueError("BetterGI 实时地址的主机名不是合法 IP") from exc
        if not is_loopback:
            raise ValueError("BetterGI 实时地址只能指向本机回环地址")


class BetterGILiveBridge:
    """BetterGI 解说的接收与播出：一条读取任务 + 一条发言消费任务。

    两个任务各司其职：读取任务只做"收消息 → 判类型 → 判重 → 入有界队列"，
    消费任务只做"出队 → 交给发言管线"。收发分离的意义是让网络抖动与语音合成的
    耗时互不拖累：网络慢时解说堆在有界队列里，队列满则丢最旧的一条并记日志，
    不会无限占用内存。

    与主播自主发言共用同一个 ``SpeechDispatcher``：发言管线自身用空档占位保证
    同一时刻只有一句话在播，两条来源因此天然串行，不需要额外的仲裁。
    """

    def __init__(self, config: BetterGILiveConfig, speech: SpeechDispatcher) -> None:
        self._config = config
        self._speech = speech
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=config.queue_max)
        self._reader: Optional[asyncio.Task[None]] = None
        self._consumer: Optional[asyncio.Task[None]] = None
        # 判重表：同一会话内 (序号, 时间戳) 只播一次。
        # 解说消息本身不带会话号，会话号来自连接建立时的 hello 消息。
        self._session_id: Optional[str] = None
        self._seen: OrderedDict[tuple[Optional[str], int, str], None] = OrderedDict()

    # ---------------- 只读状态（观察面） ----------------

    @property
    def url(self) -> str:
        """当前接入的实时会话地址（观察面）。"""
        return self._config.url

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        """启动接入：凭证缺失或地址不合法时不启动（fail-soft，原因写日志）。

        消费任务先于读取任务创建：读取任务一收到消息就入队，消费任务必须已经在
        等待取件，否则队列里的解说会等到下一次才有出口。
        """
        if self._reader is not None:
            return
        try:
            validate_live_url(self._config.url)
        except ValueError as exc:
            logger.error(f"BetterGI 实时地址不合法，接入未启动: {exc}")
            return
        token = os.environ.get(TOKEN_ENV_VAR, "").strip()
        if not token or "\r" in token or "\n" in token:
            logger.warning(f"未设置环境变量 {TOKEN_ENV_VAR}，BetterGI 解说接入未启动")
            return
        self._consumer = asyncio.create_task(self._consume(), name="BetterGILive.consumer")
        self._reader = asyncio.create_task(self._receive(token), name="BetterGILive.reader")
        logger.info(f"BetterGI 解说接入已启动（地址 {self._config.url}）")

    async def stop(self) -> None:
        """停止接入：断开连接、结束消费、清空待播队列。可重复调用。"""
        tasks = [task for task in (self._reader, self._consumer) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader = None
        self._consumer = None
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()

    # ---------------- 读取 ----------------

    async def _receive(self, token: str) -> None:
        """连接并持续读取服务端消息；断开后按固定间隔重连。

        只做读取：不发送任何应用消息。异常只记类型不记内容，避免凭证随异常文本
        落进日志。
        """
        while True:
            try:
                async with connect(
                    self._config.url,
                    additional_headers={"Authorization": f"Bearer {token}"},
                    proxy=None,
                    max_size=_MAX_FRAME_BYTES,
                    max_queue=4,
                ) as socket:
                    logger.info("BetterGI 实时会话已连接")
                    async for message in socket:
                        self._accept(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络异常后继续重连
                logger.warning(f"BetterGI 实时会话中断或失败（{type(exc).__name__}），稍后重连")
            await asyncio.sleep(self._config.reconnect_ms / 1000)

    def _accept(self, message: Any) -> None:
        """筛选一条服务端消息：类型白名单 → 字段校验 → 判重 → 入队。

        白名单只有解说一类：画面帧（base64）、状态、指导、动作状态等消息一律丢弃，
        画面内容不会进入任何下游，更不会进入语言模型。
        """
        if not isinstance(message, str):
            return
        if len(message) > _MAX_MESSAGE_CHARS:
            return
        try:
            payload = json.loads(message)
        except ValueError:
            logger.debug("忽略一条非 JSON 的 BetterGI 消息")
            return
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if kind == "hello":
            self._on_hello(payload.get("sessionId"))
            return
        if kind == "live.stopped":
            self._on_session_end()
            return
        if kind != "commentary":
            return
        self._on_commentary(payload)

    def _on_hello(self, session_id: Any) -> None:
        """记录会话号；换会话即清空判重表（不同会话的序号各自独立）。"""
        if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
            return
        if session_id != self._session_id:
            self._seen.clear()
        self._session_id = session_id

    def _on_session_end(self) -> None:
        """会话结束：清掉会话号与判重表，下一场从头计数。"""
        self._session_id = None
        self._seen.clear()

    def _on_commentary(self, payload: dict[str, Any]) -> None:
        """校验解说字段、判重、映射情绪后入队。"""
        # 导演的内部思考以 meta.internal 标记写回，只进会话文件，不朗读。
        meta = payload.get("meta")
        if isinstance(meta, dict) and meta.get("internal"):
            return
        if payload.get("kind") == "thought":
            return
        seq = payload.get("seq")
        timestamp = payload.get("timestampUtc")
        text = payload.get("text")
        if type(seq) is not int or seq < 0:
            return
        if not isinstance(timestamp, str) or not 1 <= len(timestamp) <= 64:
            return
        if not isinstance(text, str):
            return
        cleaned = text.strip()
        if not cleaned or len(cleaned) > _MAX_TEXT_CHARS:
            return
        key = (self._session_id, seq, timestamp)
        if key in self._seen:
            return
        self._seen[key] = None
        if len(self._seen) > _DEDUP_LIMIT:
            self._seen.popitem(last=False)
        # 情绪名收敛到项目的情绪词表（calm→neutral、worried→scared、未知→neutral）
        emotion, _note = coerce_emotion(payload.get("emotion"))
        item = {"speech": cleaned, "emotion": {"name": emotion.value, "intensity": 0.5}}
        if self._queue.full():
            self._queue.get_nowait()
            self._queue.task_done()
            logger.warning("BetterGI 解说待播队列已满，丢弃最旧的一条")
        self._queue.put_nowait(item)

    # ---------------- 播出 ----------------

    async def _consume(self) -> None:
        """把待播解说逐条交给发言管线（复用同一 utterance 队列与业务事件）。"""
        while True:
            item = await self._queue.get()
            try:
                await self._speech.dispatch(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单条失败不打断后续解说
                logger.warning(f"BetterGI 解说播出失败（{type(exc).__name__}）")
            finally:
                self._queue.task_done()


__all__ = ["BetterGILiveBridge", "TOKEN_ENV_VAR", "validate_live_url"]
