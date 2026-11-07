#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoDL Lumine 玩家推理服务器。

链路：

    Bridge --WebSocket(/ws/player)--> 本服务 --HTTP--> vLLM OpenAI 兼容接口

本服务只接收 JPEG 画面与指导文本，返回结构化 Lumine 动作块；不读取游戏内存，
不抓包，也不执行任意脚本。协议见同目录 PROTOCOL.md。

示例::

    LUMINE_SERVER_TOKEN=secret python autodl_server.py \\
        --vllm-url http://127.0.0.1:8000 --model lumine-agent-vl-7b

没有 GPU 时可用 ``--mock`` 返回按帧序号确定的脚本动作，便于联调。
"""

import argparse
import asyncio
import base64
import binascii
import copy
import hmac
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

try:
    import aiohttp
    from aiohttp import WSMsgType, web

    HAS_AIOHTTP = True
except ImportError:  # 纯函数测试不依赖 aiohttp
    aiohttp = None
    WSMsgType = None
    web = None
    HAS_AIOHTTP = False

try:  # 作为脚本运行（python autodl_server.py）
    from lumine_protocol import (
        append_image,
        build_image_only_messages,
        build_messages,
        motor_attribution,
        parse_action,
    )
except ImportError:  # 作为包导入
    from .lumine_protocol import (
        append_image,
        build_image_only_messages,
        build_messages,
        motor_attribution,
        parse_action,
    )


LOG = logging.getLogger("lumine.autodl")

PROTOCOL_NAME = "lumine-live-v1"
PLAYER_FPS = 5
ACTION_INTERVAL_MS = 33
CHUNKS_PER_ACTION = 6

# 单帧 base64 上限：2 MiB。超过时直接拒绝，避免内存与带宽被滥用。
MAX_FRAME_BASE64_CHARS = 2 * 1024 * 1024
MAX_WS_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_GUIDANCE_CHARS = 4000

JPEG_SOI = b"\xff\xd8\xff"

# Mock 模式默认延迟，模拟一次 7B 模型推理的耗时（秒）。
DEFAULT_MOCK_LATENCY = 0.04

# 官方 Runner 的训练几何：1280x720 原图，底部补 16 像素黑边成 1280x736。
TRAINING_FRAME_WIDTH = 1280
TRAINING_FRAME_HEIGHT = 720
TRAINING_PADDED_HEIGHT = 736

# 官方 Runner 的采样参数：温度 0、最多 192 token、保留特殊 token。
TRAINING_MAX_TOKENS = 192
TRAINING_TEMPERATURE = 0.0
TRAINING_TOP_P = 1.0

# 推理协议：``image`` 是 stage-1 训练格式（唯一正确用法），
# ``text`` 只用于将来指令微调过的检查点。
INFERENCE_PROTOCOLS = ("image", "text")


# ----------------------------------------------------------------------
# 配置与异常
# ----------------------------------------------------------------------


@dataclass
class ServerConfig:
    """服务器配置。"""

    host: str = "0.0.0.0"
    port: int = 8010
    vllm_url: str = "http://127.0.0.1:8000"
    model: str = "lumine-agent-vl-7b"
    token: str = ""
    mock: bool = False
    mock_latency: float = DEFAULT_MOCK_LATENCY
    max_tokens: int = 128
    temperature: float = 0.7
    top_p: float = 0.95
    prompt_mode: str = "pretrain"
    protocol: str = "image"
    request_timeout: float = 60.0
    log_level: str = "INFO"

    def chat_completions_url(self) -> str:
        """返回 vLLM Chat Completions 地址；兼容传入 base、/v1 或完整地址。"""
        url = (self.vllm_url or "").strip().rstrip("/")
        if url.endswith("/v1/chat/completions"):
            return url
        if url.endswith("/v1"):
            return url + "/chat/completions"
        return url + "/v1/chat/completions"


class FrameError(ValueError):
    """画面帧校验失败（客户端错误，不重试）。"""


class InferenceError(RuntimeError):
    """推理链路错误；``code`` 会返回给 Bridge。"""

    def __init__(self, message: str, code: str = "inference_failed"):
        super().__init__(message)
        self.code = code


# ----------------------------------------------------------------------
# 画面校验
# ----------------------------------------------------------------------


def normalize_jpeg_base64(value: Any) -> str:
    """校验并规范化客户端传入的 JPEG base64。

    接受纯 base64 或 ``data:image/jpeg;base64,...`` URL，返回补齐 padding 的
    纯 base64 字符串。大小、base64 或 JPEG SOI 不合法时抛出 :class:`FrameError`。
    """
    if not isinstance(value, str):
        raise FrameError("frame jpeg must be a base64 string")
    text = value.strip()
    if not text:
        raise FrameError("frame jpeg is empty")
    if len(text) > MAX_FRAME_BASE64_CHARS:
        raise FrameError(
            "frame jpeg is too large: {0} base64 chars (limit {1})".format(
                len(text), MAX_FRAME_BASE64_CHARS
            )
        )

    if text[:5].lower() == "data:":
        comma = text.find(",")
        if comma < 0:
            raise FrameError("malformed JPEG data URL")
        metadata = text[5:comma].lower()
        if "image/jpeg" not in metadata and "image/jpg" not in metadata:
            raise FrameError("data URL is not image/jpeg")
        text = text[comma + 1:].strip()
        if not text:
            raise FrameError("frame jpeg is empty")
        if len(text) > MAX_FRAME_BASE64_CHARS:
            raise FrameError("frame jpeg is too large")

    compact = "".join(text.split())
    if not compact:
        raise FrameError("frame jpeg is empty")
    compact += "=" * (-len(compact) % 4)
    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FrameError("frame jpeg is not valid base64") from exc
    if len(raw) < 4 or not raw.startswith(JPEG_SOI):
        raise FrameError("frame data is not a JPEG image")
    return compact


def decode_jpeg_base64(value: Any) -> bytes:
    """校验并解码 JPEG base64，返回原始字节。"""
    return base64.b64decode(normalize_jpeg_base64(value), validate=True)


def prepare_training_frame_data_url(value: Any) -> str:
    """把 Bridge 的 JPEG 帧转成 stage-1 训练几何的 PNG data URL。

    官方 Runner（``Runner/client.go`` 的 ``PaddedFrame``）要求帧必须是
    1280x720，然后底部补 16 像素黑边成 1280x736；训练时的像素几何就是
    1280x736。Bridge 送来的是任意尺寸的 JPEG，直接喂给模型会让视觉输入
    偏离训练分布（并且 vLLM 侧还会再做一次缩放），因此这里统一转码。

    :param value: Bridge 传入的 JPEG base64 或 data URL。
    :raises FrameError: 帧本身不合法。
    :raises InferenceError: 环境缺少图像库，无法转码。
    """
    raw = decode_jpeg_base64(value)
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError as exc:  # 训练镜像一定带 Pillow；缺了就明确失败
        raise InferenceError(
            "Pillow 不可用，无法把帧转成训练几何 1280x736 PNG", "frame_unsupported"
        ) from exc

    with Image.open(BytesIO(raw)) as image:
        rgb = image.convert("RGB")
        if rgb.size != (TRAINING_FRAME_WIDTH, TRAINING_FRAME_HEIGHT):
            # 训练几何是 1280x720；16:9 客户区按比例缩放到这个尺寸。
            rgb = rgb.resize(
                (TRAINING_FRAME_WIDTH, TRAINING_FRAME_HEIGHT), Image.LANCZOS
            )
        padded = Image.new("RGB", (TRAINING_FRAME_WIDTH, TRAINING_PADDED_HEIGHT), "black")
        padded.paste(rgb, (0, 0))
        buffer = BytesIO()
        padded.save(buffer, format="PNG", compress_level=1)

    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


# ----------------------------------------------------------------------
# Mock 动作脚本
# ----------------------------------------------------------------------

# 每个阶段：(名称, 持续帧数, 动作文本, 思考文本)。5 fps 下 40 帧约 8 秒。
_MOCK_PHASES = (
    (
        "walk",
        10,
        "<|action_start|>0 0 0 ; W ; W ; W ; W ; W ; W<|action_end|>",
        "Walk forward along the path.",
    ),
    (
        "camera_right",
        6,
        "<|action_start|>10 0 0 ; ; ; ; ; ;<|action_end|>",
        "Scan the area to the right.",
    ),
    (
        "camera_left",
        6,
        "<|action_start|>-10 0 0 ; ; ; ; ; ;<|action_end|>",
        "Scan back to the left.",
    ),
    (
        "jump",
        5,
        "<|action_start|>0 0 0 ; Space ; Space ; W ; W ; W ; W<|action_end|>",
        "Jump over the ledge.",
    ),
    (
        "attack",
        5,
        "<|action_start|>0 0 0 ; LB ; LB ; LB ; ; ;<|action_end|>",
        "Attack the nearby enemy.",
    ),
    (
        "absolute",
        3,
        "<|action_start|><use_absolute> 640 360 0 ; LB ; ; ; ; ;<|action_end|>",
        "Select the target at the center of the screen.",
    ),
    (
        "idle",
        5,
        "<|action_start|>0 0 0 ; ; ; ; ; ;<|action_end|>",
        "Wait and observe.",
    ),
)

MOCK_CYCLE_FRAMES = sum(phase[1] for phase in _MOCK_PHASES)


def mock_phase(seq: int) -> Dict[str, Any]:
    """按帧序号返回确定性的 mock 阶段。"""
    index = int(seq) % MOCK_CYCLE_FRAMES
    for name, duration, action, thought in _MOCK_PHASES:
        if index < duration:
            return {"name": name, "action": action, "thought": thought}
        index -= duration
    # 理论上不可达；保留安全回退。
    name, _, action, thought = _MOCK_PHASES[-1]
    return {"name": name, "action": action, "thought": thought}


def mock_response_text(seq: int) -> str:
    """返回带可选思考块的 mock 模型输出。"""
    phase = mock_phase(seq)
    thought = phase["thought"]
    if thought:
        return "<|thought_start|>{0}<|thought_end|>{1}".format(thought, phase["action"])
    return phase["action"]


def movement_on_first_chunk(chunks: Any) -> Any:
    """确保移动/滚轮前缀只出现在第一个键块。

    ``parse_action`` 已按此规则输出；这里再在服务端边界兜底，避免任何
    解析器实现把前缀复制到 6 个键块而导致 6 倍位移。
    """
    if not isinstance(chunks, list):
        return chunks
    normalized = []
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            return chunks
        item = dict(chunk)
        if index > 0:
            item["dx"] = 0
            item["dy"] = 0
            item["scroll"] = 0
        normalized.append(item)
    return normalized


# ----------------------------------------------------------------------
# 单个 WebSocket 会话（每个 Bridge 连接一个）
# ----------------------------------------------------------------------


@dataclass
class PendingFrame:
    """等待推理的最新一帧。"""

    seq: int
    jpeg: str
    ts_ms: Optional[int]


class PlayerSession:
    """一个 Bridge 连接的画面槽位与单推理工作线程。"""

    def __init__(self, session_id: str, ws: Any, server: "LumineServer"):
        self.session_id = session_id
        self.ws = ws
        self.server = server

        # 最新帧；推理忙时旧帧被覆盖并计入 dropped。
        self.pending: Optional[PendingFrame] = None
        self.latest_seq = 0
        self.latest_ts_ms: Optional[int] = None
        self.guidance = ""
        self.intent: Optional[Dict[str, Any]] = None
        self.motor_attribution: Optional[Dict[str, Any]] = None
        self.motor_generation = -1
        self.protocol_v2 = False
        # Serialize v2 publication with cancellation/intent acknowledgements.
        self._motor_lock = asyncio.Lock()

        # 单工作线程状态。
        self.inflight = False
        self.worker: Optional[asyncio.Task] = None
        self.inference_task: Optional[asyncio.Task] = None

        # 统计。
        self.received_frames = 0
        self.dropped = 0
        self.actions = 0
        self.errors = 0
        self.last_latency_ms: Optional[int] = None
        self.last_latency_at = 0.0

        # reset 时递增；推理返回后若代际变化则丢弃结果。
        self.generation = 0
        self.closed = False
        self.started_at = time.monotonic()

    # -- 发送 -----------------------------------------------------------

    async def send(self, payload: Dict[str, Any]) -> bool:
        """发送一条 JSON 消息；连接已关闭时返回 False。"""
        if self.closed:
            return False
        try:
            await self.ws.send_json(payload)
            return True
        except Exception as exc:  # 连接可能已断开，避免工作线程崩溃
            LOG.debug("session %s send failed: %s", self.session_id, exc)
            self.closed = True
            return False

    async def send_hello(self) -> None:
        """发送服务器 hello。"""
        await self.send(
            {
                "type": "hello",
                "model": self.server.config.model,
                "fps": PLAYER_FPS,
                "protocol": PROTOCOL_NAME,
                "mock": self.server.config.mock,
                "chunksPerAction": CHUNKS_PER_ACTION,
                "actionIntervalMs": ACTION_INTERVAL_MS,
            }
        )

    async def send_status(self) -> None:
        """发送一条玩家会话状态。"""
        await self.send(
            {
                "type": "status",
                "queue": 1 if self.pending is not None else 0,
                "inflight": self.inflight,
                "dropped": self.dropped,
                "lastLatencyMs": self.last_latency_ms,
            }
        )

    async def send_error(
        self, code: str, message: str, for_frame_seq: Optional[int] = None,
        attribution: Optional[Dict[str, Any]] = None,
    ) -> None:
        """发送错误消息；推理错误携带产生它的意图归属。"""
        payload = {"type": "error", "code": code, "message": message}
        if for_frame_seq is not None:
            payload["forFrameSeq"] = for_frame_seq
        if attribution:
            payload.update(attribution)
        await self.send(payload)

    async def _send_current_action(self, payload: Dict[str, Any], generation: int) -> Optional[bool]:
        """Publish before a cancellation ack, or suppress a superseded result."""
        async with self._motor_lock:
            if self.closed or generation != self.generation:
                return None
            return await self.send(payload)

    # -- 输入 -----------------------------------------------------------

    def set_guidance(self, text: Any) -> None:
        """更新当前指导文本（超长时截断）。"""
        if text is None:
            return
        value = text if isinstance(text, str) else str(text)
        self.guidance = value[:MAX_GUIDANCE_CHARS]

    def _invalidate_inference(self) -> None:
        """失效并取消本地 HTTP 等待；远端 GPU 是否中止无法保证。"""
        self.generation += 1
        self.pending = None
        if self.inference_task is not None and not self.inference_task.done():
            self.inference_task.cancel()

    def set_intent(self, intent: Any, attribution: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """替换意图并使旧帧和旧推理失效；v2 代际只能递增。"""
        if not isinstance(intent, dict):
            return None
        goal = intent.get("goal")
        intent_id = intent.get("intent_id")
        if not isinstance(goal, str) or not goal.strip() or not isinstance(intent_id, str) or not intent_id.strip():
            return None
        if self.protocol_v2:
            attribution = motor_attribution(attribution)
            if (attribution is None or attribution["intentId"] != intent_id
                    or attribution["motorGeneration"] <= self.motor_generation
                    or ("actionGroupId" in intent and
                        ("actionGroupId" not in attribution or
                         intent["actionGroupId"] != attribution["actionGroupId"]))):
                return None
            self.motor_generation = attribution["motorGeneration"]
        self._invalidate_inference()
        self.intent = copy.deepcopy(intent)
        self.motor_attribution = dict(attribution) if attribution else None
        return self.intent

    def cancel_intent(self, generation: Any) -> bool:
        """撤销当前代际及等待帧；不保证上游推理服务停止计算。"""
        if (isinstance(generation, bool) or not isinstance(generation, int)
                or generation < 0 or generation <= self.motor_generation):
            return False
        self.motor_generation = generation
        self._invalidate_inference()
        self.intent = None
        self.motor_attribution = None
        return True

    def enqueue_frame(self, seq: int, jpeg: str, ts_ms: Optional[int]) -> None:
        """保存最新帧；忙时覆盖旧帧，空闲时启动工作线程。"""
        self.received_frames += 1
        self.latest_seq = seq
        self.latest_ts_ms = ts_ms
        if self.pending is not None:
            self.dropped += 1
        self.pending = PendingFrame(seq=seq, jpeg=jpeg, ts_ms=ts_ms)
        if not self.inflight:
            self.inflight = True
            self.worker = asyncio.create_task(self._run_worker())

    def reset(self) -> None:
        """清空画面槽位、指导与统计，并让在途推理结果失效。"""
        self._invalidate_inference()
        self.latest_seq = 0
        self.latest_ts_ms = None
        self.guidance = ""
        self.intent = None
        self.motor_attribution = None
        self.dropped = 0
        self.actions = 0
        self.errors = 0
        self.last_latency_ms = None
        self.last_latency_at = 0.0
        # 上一次模型自己给出的移动动作；用于 idle 时的限时沿用。
        self.last_motion: Optional[Dict[str, Any]] = None
        self.last_motion_at = 0.0

    # -- 工作线程 -------------------------------------------------------

    async def _run_worker(self) -> None:
        """串行消费最新帧；同一会话同一时刻最多一个推理请求。"""
        try:
            while self.pending is not None and not self.closed:
                frame = self.pending
                self.pending = None
                generation = self.generation
                intent = copy.deepcopy(self.intent)
                attribution = dict(self.motor_attribution) if self.motor_attribution else None
                # v2 没有有效意图时不能运行自主模式或发送无归属动作。
                if self.protocol_v2 and (intent is None or attribution is None):
                    continue
                started = time.monotonic()
                try:
                    if intent is None:
                        request = self.server.infer(frame.jpeg, self.guidance, frame.seq)
                    else:
                        request = self.server.infer(frame.jpeg, self.guidance, frame.seq, intent)
                    self.inference_task = asyncio.create_task(request)
                    try:
                        parsed = await self.inference_task
                    finally:
                        self.inference_task = None
                except asyncio.CancelledError:
                    if not self.closed and generation != self.generation:
                        continue
                    raise
                except Exception as exc:  # noqa: BLE001 - 需要把任何推理错误转成协议消息
                    if self.closed or generation != self.generation:
                        continue
                    self.errors += 1
                    code = getattr(exc, "code", "inference_failed")
                    LOG.warning(
                        "session %s inference failed for seq=%s: %s",
                        self.session_id,
                        frame.seq,
                        exc,
                    )
                    await self.send_error(code, str(exc), frame.seq, attribution)
                    await self.send_status()
                    continue

                latency_ms = int(round((time.monotonic() - started) * 1000))
                if intent is not None:
                    parsed, carried_ms = apply_motion_carry(
                        parsed, intent, self.last_motion, self.last_motion_at,
                        time.monotonic(),
                    )
                    if carried_ms is None and _action_keys(parsed):
                        self.last_motion = dict(parsed)
                        self.last_motion_at = time.monotonic()
                if self.closed or generation != self.generation:
                    # reset 或断线后丢弃过期结果。
                    continue

                LOG.debug(
                    "session %s seq=%s action in %sms",
                    self.session_id,
                    frame.seq,
                    latency_ms,
                )
                sent = await self._send_current_action(
                    {
                        "type": "action",
                        "forFrameSeq": frame.seq,
                        "action": parsed.get("action"),
                        "chunks": movement_on_first_chunk(parsed.get("chunks")),
                        "absolute": parsed.get("absolute"),
                        "thought": parsed.get("thought"),
                        "latencyMs": latency_ms,
                        "raw": parsed.get("raw"),
                        **(attribution or {}),
                    }, generation
                )
                if sent is None:
                    continue
                if not sent:
                    break
                self.actions += 1
                self.last_latency_ms = latency_ms
                self.last_latency_at = time.monotonic()
                await self.send_status()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 工作线程异常只记录，不能影响连接
            LOG.warning("session %s worker failed: %s", self.session_id, exc)
        finally:
            self.inflight = False
            self.worker = None

    async def close(self) -> None:
        """停止工作线程并标记连接已关闭。"""
        self.closed = True
        worker = self.worker
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):  # 清理阶段吞掉所有异常
                pass
        self.worker = None
        self.inflight = False
        self.pending = None

    # -- 状态 -----------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """返回可 JSON 序列化的会话状态。"""
        return {
            "sessionId": self.session_id,
            "latestSeq": self.latest_seq,
            "latestTsMs": self.latest_ts_ms,
            "queue": 1 if self.pending is not None else 0,
            "inflight": self.inflight,
            "dropped": self.dropped,
            "receivedFrames": self.received_frames,
            "actions": self.actions,
            "errors": self.errors,
            "lastLatencyMs": self.last_latency_ms,
            "guidance": self.guidance,
        "intentId": (self.intent or {}).get("intent_id", ""),
        }


# ----------------------------------------------------------------------
# 服务器
# ----------------------------------------------------------------------


class LumineServer:
    """持有配置、共享 HTTP 会话与所有玩家会话。"""

    def __init__(self, config: ServerConfig):
        self.config = config
        self.http: Optional[Any] = None
        self.sessions: Dict[str, PlayerSession] = {}
        self.started_at = time.monotonic()
        self._session_counter = 0

    # -- 会话管理 -------------------------------------------------------

    def register_session(self, ws: Any) -> PlayerSession:
        """创建并登记一个玩家会话。"""
        self._session_counter += 1
        session_id = "player-{0}-{1}".format(int(time.time()), self._session_counter)
        session = PlayerSession(session_id, ws, self)
        self.sessions[session_id] = session
        return session

    def unregister_session(self, session: PlayerSession) -> None:
        """移除会话。"""
        self.sessions.pop(session.session_id, None)

    # -- 推理 -----------------------------------------------------------

    async def infer(
        self,
        jpeg_base64: str,
        guidance: str,
        seq: int,
        intent: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """对一帧执行推理，返回 :func:`parse_action` 的结构化结果。"""
        if self.config.mock:
            # 确定性脚本动作；sleep 模拟 GPU 推理耗时。
            await asyncio.sleep(max(0.0, self.config.mock_latency))
            return parse_action(mock_response_text(seq))

        if self.http is None or self.http.closed:
            raise InferenceError("HTTP client is not ready")

        # 携带意图时不得落回自主选目标的 pretrain/instruct 提示词。
        if self.config.protocol == "image":
            # stage-1 检查点只在"单张图像、无文本"的输入上训练过；任何 system
            # 提示或指令文本都属于分布外输入，会让策略退化成一直 idle。
            messages = build_image_only_messages(prepare_training_frame_data_url(jpeg_base64))
            payload = {
                "model": self.config.model,
                "messages": messages,
                "max_tokens": TRAINING_MAX_TOKENS,
                "temperature": TRAINING_TEMPERATURE,
                "top_p": TRAINING_TOP_P,
                "stream": False,
                "skip_special_tokens": False,
            }
        else:
            mode = "motor" if intent is not None else self.config.prompt_mode
            messages = build_messages(guidance, mode, intent=intent)
            append_image(messages, jpeg_base64)
            payload = {
                "model": self.config.model,
                "messages": messages,
                "max_tokens": self.config.max_tokens,
                "temperature": self.config.temperature,
                "top_p": self.config.top_p,
                "stream": False,
            }

        try:
            async with self.http.post(self.config.chat_completions_url(), json=payload) as response:
                body = await response.text()
                status = response.status
        except asyncio.TimeoutError as exc:
            raise InferenceError("vLLM request timed out", "vllm_timeout") from exc
        except aiohttp.ClientError as exc:
            raise InferenceError("vLLM request failed: {0}".format(exc), "vllm_unreachable") from exc

        if status != 200:
            raise InferenceError(
                "vLLM returned HTTP {0}: {1}".format(status, body[:300]), "vllm_http_error"
            )

        try:
            data = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise InferenceError("vLLM returned invalid JSON", "vllm_bad_response") from exc

        raw = _extract_message_content(data)
        try:
            result = parse_action(raw)
        except ValueError as exc:
            raise InferenceError(
                "model output has no parsable action: {0}; raw={1!r}".format(exc, raw[:300]),
                "bad_action",
            ) from exc

        # 预训练模型对同一帧只有部分概率输出移动键，单次采样会让角色走走停停。
        # 这里只对"要求移动却给了 idle"的结果做有限次重采样，动作仍全部来自模型。
        # image 协议用温度 0（训练同款采样），重采样不会改变结果，因此跳过。
        resamples = 0
        move_intent = wants_motion(intent) and self.config.protocol != "image"
        while move_intent and resamples < _MAX_IDLE_RESAMPLES and not _action_keys(result):
            resamples += 1
            payload["temperature"] = min(1.0, float(self.config.temperature) + 0.2 * resamples)
            try:
                async with self.http.post(
                    self.config.chat_completions_url(), json=payload
                ) as response:
                    body = await response.text()
                    status = response.status
            except asyncio.TimeoutError as exc:
                raise InferenceError("vLLM request timed out", "vllm_timeout") from exc
            except aiohttp.ClientError as exc:
                raise InferenceError(
                    "vLLM request failed: {0}".format(exc), "vllm_unreachable"
                ) from exc
            if status != 200:
                break
            try:
                retry_raw = _extract_message_content(json.loads(body))
                retry_result = parse_action(retry_raw)
            except (TypeError, ValueError):
                break
            result = retry_result
        if resamples:
            result["resamples"] = resamples
        return result

    # -- 状态 -----------------------------------------------------------

    def status_snapshot(self) -> Dict[str, Any]:
        """汇总所有会话状态，用于 /status。"""
        sessions = list(self.sessions.values())
        snapshots = [session.snapshot() for session in sessions]
        latest_latency_session = None
        for session in sessions:
            if session.last_latency_at <= 0:
                continue
            if latest_latency_session is None or session.last_latency_at > latest_latency_session.last_latency_at:
                latest_latency_session = session
        return {
            "ok": True,
            "model": self.config.model,
            "mock": self.config.mock,
            "vllmUrl": self.config.vllm_url,
            "promptMode": self.config.prompt_mode,
            "protocol": self.config.protocol,
            "clients": len(sessions),
            "inflight": any(session.inflight for session in sessions),
            "queue": sum(1 for session in sessions if session.pending is not None),
            "latestSeq": max((session.latest_seq for session in sessions), default=0),
            "dropped": sum(session.dropped for session in sessions),
            "lastLatencyMs": (
                latest_latency_session.last_latency_ms
                if latest_latency_session is not None
                else None
            ),
            "uptimeSec": round(time.monotonic() - self.started_at, 3),
            "sessions": snapshots,
        }


# 实测（2026-11-03，真云端 4090）：每次动作平均触发 3.75 次完整推理，explore 模式
# P50 1608ms / P90 2647ms，重采样是最大瓶颈。收紧到 1 次：保留一次纠偏能力，
# 其余交给桥接的 200ms 续按（held）与 MOTION_CARRY_MS 兜住空窗。
_MAX_IDLE_RESAMPLES = 1

# 模型偶发 idle 时，最多沿用上一次移动动作这么久，避免角色一步一停。
MOTION_CARRY_MS = 1500

_MOTION_MODES = ("navigate", "explore")


def wants_motion(intent: Optional[Dict[str, Any]]) -> bool:
    """意图是否要求角色主动位移。"""
    return (
        intent is not None
        and str(intent.get("motor_mode") or "navigate") in _MOTION_MODES
    )


def apply_motion_carry(
    result: Dict[str, Any],
    intent: Optional[Dict[str, Any]],
    last_motion: Optional[Dict[str, Any]],
    last_motion_at: float,
    now: float,
    hold_ms: int = MOTION_CARRY_MS,
) -> Tuple[Dict[str, Any], Optional[int]]:
    """模型给出 idle 时，限时沿用上一次移动动作。

    返回 ``(结果, 沿用毫秒数)``；没有沿用返回 ``None``。沿用只在意图要求移动、
    当前动作没有任何按键、且上一次移动仍在 ``hold_ms`` 之内时发生。
    """
    if not wants_motion(intent) or _action_keys(result):
        return result, None
    if last_motion is None or last_motion_at <= 0 or hold_ms <= 0:
        return result, None
    age_ms = int(round((now - last_motion_at) * 1000))
    if age_ms < 0 or age_ms > hold_ms:
        return result, None
    carried = dict(last_motion)
    carried["carriedOverMs"] = age_ms
    return carried, age_ms


def _action_keys(result: Dict[str, Any]) -> List[str]:
    """取出动作里出现的全部按键，用于判断模型是否只给了 idle。"""
    keys: List[str] = []
    for chunk in result.get("chunks") or []:
        if isinstance(chunk, dict):
            keys.extend(str(key) for key in (chunk.get("keys") or []))
    return keys


def _extract_message_content(data: Any) -> str:
    """从 OpenAI Chat Completions 响应中取出文本内容。"""
    if not isinstance(data, dict):
        raise InferenceError("vLLM response is not a JSON object", "vllm_bad_response")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise InferenceError("vLLM response has no choices", "vllm_bad_response")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None

    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        content = "".join(parts)

    if not isinstance(content, str) or not content.strip():
        raise InferenceError("vLLM response has empty content", "vllm_bad_response")
    return content


# ----------------------------------------------------------------------
# aiohttp 入口
# ----------------------------------------------------------------------


def _extract_token(request: Any) -> str:
    """从查询参数或 Authorization 头读取令牌。"""
    token = request.query.get("token") or request.query.get("access_token") or ""
    if token:
        return token
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def _authorized(server: LumineServer, request: Any) -> bool:
    """令牌为空时放行；配置令牌后必须匹配。"""
    expected = server.config.token
    if not expected:
        return True
    return hmac.compare_digest(
        _extract_token(request).encode("utf-8"), expected.encode("utf-8")
    )


async def _handle_health(request: Any) -> Any:
    """GET /health：进程存活与基本配置。"""
    server: LumineServer = request.app["lumine"]
    return web.json_response(
        {
            "ok": True,
            "model": server.config.model,
            "mock": server.config.mock,
            "uptimeSec": round(time.monotonic() - server.started_at, 3),
        }
    )


async def _handle_status(request: Any) -> Any:
    """GET /status：推理与会话统计。"""
    server: LumineServer = request.app["lumine"]
    return web.json_response(server.status_snapshot())


async def _handle_player(request: Any) -> Any:
    """WebSocket /ws/player：单个 Bridge 的画面与动作通道。"""
    server: LumineServer = request.app["lumine"]
    if not _authorized(server, request):
        LOG.warning("rejected player connection from %s", request.remote)
        return web.Response(status=401, text="unauthorized")

    ws = web.WebSocketResponse(
        max_msg_size=MAX_WS_MESSAGE_BYTES,
        heartbeat=30.0,
        compress=False,
    )
    await ws.prepare(request)
    session = server.register_session(ws)
    LOG.info("player connected: %s from %s", session.session_id, request.remote)
    await session.send_hello()

    try:
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                await _handle_player_message(session, message.data)
            elif message.type == WSMsgType.BINARY:
                await session.send_error("bad_message", "binary messages are not supported")
            elif message.type == WSMsgType.ERROR:
                LOG.warning("player %s websocket error: %s", session.session_id, ws.exception())
                break
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 连接级错误只记录
        LOG.warning("player %s connection failed: %s", session.session_id, exc)
    finally:
        await session.close()
        server.unregister_session(session)
        LOG.info("player disconnected: %s", session.session_id)
    return ws


async def _handle_player_message(session: PlayerSession, data: str) -> None:
    """处理一条 Bridge → 服务端 JSON 消息。"""
    try:
        message = json.loads(data)
    except (TypeError, ValueError):
        await session.send_error("bad_json", "message is not valid JSON")
        return
    if not isinstance(message, dict):
        await session.send_error("bad_message", "message must be a JSON object")
        return

    msg_type = message.get("type")
    if not isinstance(msg_type, str):
        await session.send_error("bad_message", "message type is required")
        return
    msg_type = msg_type.strip().lower()

    if msg_type == "hello":
        # v2 是连接级 fail-closed 模式，不允许中途降级。
        if message.get("protocol") == "lumine-live-v2":
            async with session._motor_lock:
                session.protocol_v2 = True
                session.reset()
        client_session = message.get("sessionId")
        if isinstance(client_session, str) and client_session:
            LOG.info("player %s hello sessionId=%s", session.session_id, client_session)
        return

    if msg_type == "motor_capability_query":
        nonce, generation = message.get("nonce"), message.get("connectionGeneration")
        if (set(message) != {"type", "nonce", "connectionGeneration"}
                or not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{32}", nonce) is None
                or type(generation) is not int or generation < 1):
            await session.send_error("bad_motor_capability_query", "invalid capability challenge")
            return
        async with session._motor_lock:
            if (session.protocol_v2 and not session.server.config.mock
                    and isinstance(session.server.config.token, str) and session.server.config.token.strip()):
                await session.send({"type": "motor_capability_ack", "nonce": nonce,
                                    "connectionGeneration": generation, "protocol": "lumine-live-v2",
                                    "intentRequired": True, "intentForcesMotor": True, "mock": False})
            else:
                await session.send_error("motor_capability_unavailable", "capability is unavailable")
        return

    if msg_type == "frame":
        await _handle_frame(session, message)
        return

    if msg_type == "guidance":
        session.set_guidance(message.get("text"))
        return

    if msg_type == "intent":
        # 兼容 {"intent": {...}} 与扁平消息两种形态。
        intent_payload = message.get("intent")
        if not isinstance(intent_payload, dict):
            intent_payload = message
        async with session._motor_lock:
            attribution = motor_attribution(message) if session.protocol_v2 else None
            intent = session.set_intent(intent_payload, attribution)
            if intent is None:
                await session.send_error("bad_intent", "invalid intent or attribution")
            else:
                await session.send(
                    {"type": "intent_ack", "intent_id": intent.get("intent_id"),
                     "accepted": True, **(attribution or {})}
                )
        return

    if msg_type == "cancel_intent":
        async with session._motor_lock:
            if not session.protocol_v2 or not session.cancel_intent(message.get("motorGeneration")):
                await session.send_error("bad_cancel", "invalid or stale cancellation generation")
            else:
                await session.send({"type": "cancel_ack", "motorGeneration": session.motor_generation})
        return

    if msg_type == "reset":
        async with session._motor_lock:
            session.reset()
            await session.send_status()
        return

    if msg_type == "ping":
        await session.send({"type": "pong", "ts": int(time.time() * 1000)})
        return

    await session.send_error("unknown_type", "unknown message type: {0}".format(msg_type))


async def _handle_frame(session: PlayerSession, message: Dict[str, Any]) -> None:
    """校验并提交一帧。"""
    seq = message.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        await session.send_error("bad_frame", "frame seq must be an integer")
        return

    jpeg = message.get("jpeg")
    if jpeg is None:
        jpeg = message.get("image")
    try:
        normalized = normalize_jpeg_base64(jpeg)
    except FrameError as exc:
        await session.send_error("bad_frame", str(exc), seq)
        return

    inline_guidance = message.get("guidance")
    if isinstance(inline_guidance, str):
        # 每帧携带的指导优先；空字符串表示清除旧指导。
        session.set_guidance(inline_guidance)

    ts_ms = message.get("tsMs")
    if not isinstance(ts_ms, int) or isinstance(ts_ms, bool):
        ts_ms = None

    session.enqueue_frame(seq, normalized, ts_ms)


def create_app(config: ServerConfig) -> Any:
    """创建 aiohttp Application。"""
    if not HAS_AIOHTTP:
        raise RuntimeError("aiohttp is required; install it with: pip install -r requirements.txt")

    server = LumineServer(config)
    app = web.Application()
    app["lumine"] = server

    async def on_startup(_app: Any) -> None:
        """创建共享 HTTP 客户端并打印启动信息。"""
        timeout = aiohttp.ClientTimeout(total=max(1.0, float(config.request_timeout)))
        server.http = aiohttp.ClientSession(timeout=timeout)
        LOG.info(
            "Lumine server ready: model=%s mock=%s vllm=%s prompt=%s",
            config.model,
            config.mock,
            config.vllm_url,
            config.prompt_mode,
        )

    async def on_cleanup(_app: Any) -> None:
        """关闭全部玩家会话与 HTTP 客户端。"""
        for session in list(server.sessions.values()):
            await session.close()
        server.sessions.clear()
        if server.http is not None and not server.http.closed:
            await server.http.close()
        server.http = None

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/health", _handle_health)
    app.router.add_get("/status", _handle_status)
    app.router.add_get("/ws/player", _handle_player)
    return app


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def _env_flag(name: str) -> bool:
    """把常见真值环境变量转换为布尔值。"""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    """读取浮点环境变量；非法值回退默认值。"""
    try:
        return float(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    """读取整数环境变量；非法值回退默认值。"""
    try:
        return int(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default


def build_arg_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。CLI 优先于环境变量。"""
    parser = argparse.ArgumentParser(
        description="AutoDL Lumine player server (WebSocket -> vLLM -> actions)"
    )
    parser.add_argument("--host", default=os.environ.get("LUMINE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=_env_int("LUMINE_PORT", 8010))
    parser.add_argument(
        "--vllm-url",
        default=os.environ.get("LUMINE_VLLM_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("LUMINE_MODEL", "lumine-agent-vl-7b"),
    )
    parser.add_argument("--token", default=os.environ.get("LUMINE_SERVER_TOKEN", ""))
    parser.add_argument(
        "--mock",
        action="store_true",
        default=_env_flag("LUMINE_MOCK"),
        help="return deterministic scripted actions without vLLM",
    )
    parser.add_argument(
        "--mock-latency",
        type=float,
        default=_env_float("LUMINE_MOCK_LATENCY", DEFAULT_MOCK_LATENCY),
        help="simulated inference latency in seconds for --mock",
    )
    parser.add_argument("--max-tokens", type=int, default=_env_int("LUMINE_MAX_TOKENS", 128))
    parser.add_argument(
        "--temperature",
        type=float,
        default=_env_float("LUMINE_TEMPERATURE", 0.7),
    )
    parser.add_argument("--top-p", type=float, default=_env_float("LUMINE_TOP_P", 0.95))
    parser.add_argument(
        "--prompt-mode",
        choices=("pretrain", "instruct", "motor"),
        default=os.environ.get("LUMINE_PROMPT_MODE", "pretrain"),
    )
    parser.add_argument(
        "--protocol",
        choices=INFERENCE_PROTOCOLS,
        default=os.environ.get("LUMINE_PROTOCOL", "image"),
        help="image：stage-1 训练格式（单张图像、无文本）；text：仅用于指令微调检查点",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=_env_float("LUMINE_REQUEST_TIMEOUT", 60.0),
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LUMINE_LOG_LEVEL", "INFO"),
        help="DEBUG, INFO, WARNING or ERROR",
    )
    return parser


def parse_args(argv: Optional[List[str]] = None) -> ServerConfig:
    """解析命令行并返回配置。"""
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.port < 1 or args.port > 65535:
        parser.error("--port must be in [1, 65535]")
    if args.max_tokens < 1:
        parser.error("--max-tokens must be positive")
    if args.temperature < 0:
        parser.error("--temperature must be non-negative")
    if not 0 < args.top_p <= 1:
        parser.error("--top-p must be in (0, 1]")
    if args.mock_latency < 0:
        parser.error("--mock-latency must be non-negative")

    return ServerConfig(
        host=args.host,
        port=args.port,
        vllm_url=args.vllm_url,
        model=args.model,
        token=args.token,
        mock=args.mock,
        mock_latency=args.mock_latency,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        top_p=args.top_p,
        prompt_mode=args.prompt_mode,
        protocol=args.protocol,
        request_timeout=args.request_timeout,
        log_level=str(args.log_level).upper(),
    )


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。"""
    try:
        config = parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not HAS_AIOHTTP:
        LOG.error("aiohttp is required; install it with: pip install -r requirements.txt")
        return 1

    if config.token:
        LOG.info("WebSocket token authentication enabled")
    else:
        LOG.warning("LUMINE_SERVER_TOKEN is empty; WebSocket authentication is disabled")
    if config.mock:
        LOG.info("mock mode: vLLM is not required")

    app = create_app(config)
    web.run_app(app, host=config.host, port=config.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
