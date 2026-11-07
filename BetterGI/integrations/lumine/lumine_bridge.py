#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lumine Bridge：连通 BetterGI Live Hub 与 AutoDL Player Server。

职责边界：
- 从 BetterGI 接收 JPEG 画面与指导，按目标帧率转发给 AutoDL；
- 把 AutoDL 返回的结构化动作块转换后转发给 BetterGI；
- 只转发协议中已知的 JSON 字段，忽略未知消息；
- 不执行模型文本、不启动 shell 命令（录制 ffmpeg 除外）、不读写游戏内存或网络包。

协议详见同目录 PROTOCOL.md。
"""

import argparse
import asyncio
import base64
import binascii
import collections
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit

try:
    import aiohttp
    from aiohttp import web as aiohttp_web
except ImportError:  # ?????????? aiohttp
    aiohttp = None
    aiohttp_web = None

try:
    # ???????????????????????????????????
    from lumine_protocol import parse_action as _external_parse_action
    from lumine_protocol import motor_attribution
except ImportError:
    try:
        from .lumine_protocol import parse_action as _external_parse_action, motor_attribution
    except ImportError:
        _external_parse_action = None
        motor_attribution = None


LOG = logging.getLogger("lumine_bridge")

# ?????????
DEFAULT_BETTERGI_WS = "ws://127.0.0.1:30648/ws/v1/agent/live"
DEFAULT_LUMINE_WS = "ws://127.0.0.1:8010/ws/player"
DEFAULT_FPS = 5.0
DEFAULT_RECORD_FPS = 10.0
DEFAULT_RECONNECT = True
DEFAULT_STATS_INTERVAL = 5.0
DEFAULT_SESSION_ID = "lumine-bridge"

MAX_CHUNKS = 6
MAX_KEYS_PER_CHUNK = 4
MAX_THOUGHT_CHARS = 4000
MAX_WS_MESSAGE_SIZE = 32 * 1024 * 1024
MAX_STOP_REQUEST_HISTORY = 128
# ??????????????????????????
MOTOR_CAPABILITY_TTL = 30.0
SEND_TIMEOUT = 5.0
# 意图仍活跃但模型推理空窗时，每 200ms 重发最近动作块，避免宿主 250ms 后释放按键。
ACTION_HOLD_INTERVAL_SECONDS = 0.2
# 重发检查周期；20ms 让 200ms 窗口的抖动不超过一个宿主动作块。
ACTION_HOLD_TICK_SECONDS = 0.02
# hold 窗口上限：意图切换期间最多再沿用旧方向这么久，超时必须松键。
MAX_INTENT_HOLD_MS = 6000
MIN_BACKOFF = 1.0
MAX_BACKOFF = 20.0
# ???????????????????? 1 ??????????
STABLE_CONNECTION_SECONDS = 10.0
RECORD_QUEUE_SIZE = 30
PROTOCOL_NAME = "lumine-live-v1"


# ----------------------------------------------------------------------
# ????????
# ----------------------------------------------------------------------


def _now_ms() -> int:
    """当前 UTC 时间（毫秒）。"""
    return int(time.time() * 1000)


def _safe_endpoint(value: str) -> str:
    """Remove query, userinfo and fragments before logging an endpoint."""
    try:
        parts = urlsplit(value)
        host = parts.hostname or "unknown"
        if ":" in host:
            host = "[" + host + "]"
        return urlunsplit((parts.scheme, host + (":" + str(parts.port) if parts.port else ""), parts.path, "", ""))
    except (TypeError, ValueError):
        return "<invalid-endpoint>"


def _safe_error(value: Any, *tokens: str) -> str:
    """Do not expose configured or URL-carried credentials in transport errors."""
    text = str(value)
    for token in tokens:
        if isinstance(token, str) and token:
            for secret in {token, quote(token, safe=""), quote_plus(token, safe="")}:
                text = text.replace(secret, "<redacted>")
    text = re.sub(r"(?i)(bearer\s+)([^\s,;]+)", r"\1<redacted>", text)
    text = re.sub(r"(?i)((?:access_token|token|authorization)\s*[=:]\s*)([^\s&;,]+)", r"\1<redacted>", text)
    return text


def _ws_endpoint_and_headers(url: str, token: str) -> tuple[str, Dict[str, str]]:
    """Move credentials into the handshake header so they do not enter URLs or logs."""
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss") or not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError("WebSocket 地址无效或包含用户信息")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    credentials = [value for key, value in pairs if key.lower() in {"token", "access_token"} and value]
    if len(credentials) > 1:
        raise ValueError("WebSocket 地址包含多个令牌")
    selected = token or (credentials[0] if credentials else "")
    clean = [(key, value) for key, value in pairs if key.lower() not in {"token", "access_token"}]
    endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(clean), ""))
    return endpoint, {"Authorization": "Bearer " + selected} if selected else {}


def _motor_endpoint_identity(url: str, token: str) -> Optional[str]:
    """本地立即撤销，再尽力向两端发送停止；发送成功不等于执行成功。"""
    try:
        return _ws_endpoint_and_headers(url, token)[0]
    except (TypeError, ValueError):
        return None


def _motor_auth_identity(url: str, token: str) -> Optional[str]:
    """本地立即撤销，再尽力向两端发送停止；发送成功不等于执行成功。"""
    try:
        headers = _ws_endpoint_and_headers(url, token)[1]
        value = headers.get("Authorization")
        return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None
    except (TypeError, ValueError, UnicodeError):
        return None


def _motor_origin(url: str) -> Optional[Dict[str, str]]:
    """Only the configured WS origin is disclosed; credentials, path and query are excluded."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"ws", "wss"} or not parts.hostname or parts.username is not None or parts.password is not None:
            return None
        host = parts.hostname
        try:
            address = ipaddress.ip_address(host)
            host = address.compressed.lower()
            loopback = address.is_loopback
        except ValueError:
            host = host.encode("idna").decode("ascii").lower()
            loopback = host == "localhost"
        if not host or "%" in host:
            return None
        display = "[" + host + "]" if ":" in host else host
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        transport = "wss_authenticated" if parts.scheme == "wss" else "loopback_ws" if loopback else "insecure_ws"
        return {"origin": parts.scheme + "://" + display + ":" + str(port), "transport": transport}
    except (TypeError, ValueError, UnicodeError):
        return None


def build_ws_url(url: str, token: Optional[str] = None, param: str = "token") -> str:
    """在 WebSocket 地址后附加令牌查询参数。

    BetterGI 使用 access_token，AutoDL 使用 token。已有同名参数会被替换，
    其他查询参数保持不变。
    """
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise ValueError("WebSocket 地址必须以 ws:// 或 wss:// 开头")

    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)]
    if token:
        query = [(key, value) for key, value in query if key != param]
        query.append((param, token))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def iso_to_epoch_ms(value: Any) -> Optional[int]:
    """把 ISO-8601 时间字符串或毫秒数值转换为 Unix 毫秒。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return None
        return int(value)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _coerce_int(value: Any) -> int:
    """把 JSON 数值转换为 int；拒绝布尔值、字符串和非有限浮点数。"""
    if isinstance(value, bool):
        raise ValueError("生成可序列化的统计快照。")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("生成可序列化的统计快照。")
        return int(value)
    raise ValueError("不是合法数值：{0!r}".format(value))


def _optional_int(value: Any) -> Optional[int]:
    """校验绝对坐标对象；无效时返回 None。"""
    try:
        return _coerce_int(value)
    except (TypeError, ValueError):
        return None


def _unambiguous_json_object(pairs: List[Any]) -> Dict[str, Any]:
    """Reject duplicate JSON member names when attributing an error."""
    result: Dict[str, Any] = {}
    seen = set()
    for key, value in pairs:
        folded = key.casefold()
        if folded in seen:
            raise ValueError("ambiguous JSON member")
        seen.add(folded)
        result[key] = value
    return result


def normalize_chunks(chunks: Any) -> Optional[List[Dict[str, Any]]]:
    """校验并规范化动作键块列表。

    规则：1～6 个键块，每块最多 4 个键。只保留 dx/dy/scroll/keys 四个字段，
    未知字段一律丢弃。校验失败返回 None（调用方应保持安全停顿）。
    """
    if not isinstance(chunks, list) or not (1 <= len(chunks) <= MAX_CHUNKS):
        return None

    normalized: List[Dict[str, Any]] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            return None
        try:
            dx = _coerce_int(chunk.get("dx", 0))
            dy = _coerce_int(chunk.get("dy", 0))
            scroll = _coerce_int(chunk.get("scroll", 0))
        except (TypeError, ValueError):
            return None

        keys = chunk.get("keys", [])
        if keys is None:
            keys = []
        if not isinstance(keys, list) or len(keys) > MAX_KEYS_PER_CHUNK:
            return None
        clean_keys: List[str] = []
        for key in keys:
            if not isinstance(key, str) or not key:
                return None
            clean_keys.append(key)

        normalized.append({"dx": dx, "dy": dy, "scroll": scroll, "keys": clean_keys})
    return normalized


def normalize_absolute(value: Any) -> Optional[Dict[str, int]]:
    """校验绝对坐标对象；无效时返回 None。"""
    if not isinstance(value, dict):
        return None
    try:
        x = _coerce_int(value.get("x"))
        y = _coerce_int(value.get("y"))
    except (TypeError, ValueError):
        return None
    return {"x": x, "y": y}


def apply_movement_prefix_once(
    chunks: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """把移动/滚轮前缀收敛到首个键块。

    Lumine 的 dX/dY/dZ 前缀作用于整个 200ms 动作块，只应执行一次。
    AutoDL 与 lumine_protocol 会把同一前缀复制到全部 6 个键块，而
    BetterGI 只接受首个键块的非零位移。因此转发前保留 chunks[0] 的
    dx/dy/scroll，将其余键块的位移清零，同时保留按键状态。
    """
    if not chunks:
        return []

    result: List[Dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        item = {
            "dx": 0,
            "dy": 0,
            "scroll": 0,
            "keys": list(chunk.get("keys", [])),
        }
        if index == 0:
            item["dx"] = chunk.get("dx", 0)
            item["dy"] = chunk.get("dy", 0)
            item["scroll"] = chunk.get("scroll", 0)
        result.append(item)
    return result


def _parse_action_text(raw: str) -> Optional[Dict[str, Any]]:
    """调用可选的 lumine_protocol.parse_action 解析原始动作文本。"""
    parser = _external_parse_action
    if parser is None:
        LOG.debug("未找到 lumine_protocol.parse_action，无法解析原始动作文本。")
        return None
    try:
        result = parser(raw)
    except Exception as exc:  # ??????????????????
        LOG.warning("录制 MP4 的输出路径（可选）", exc)
        return None

    if isinstance(result, dict):
        return {
            "chunks": result.get("chunks"),
            "absolute": result.get("absolute"),
            "thought": result.get("thought"),
        }
    if isinstance(result, (list, tuple)):
        return {"chunks": list(result), "absolute": None, "thought": None}
    chunks = getattr(result, "chunks", None)
    if chunks is not None:
        return {
            "chunks": chunks,
            "absolute": getattr(result, "absolute", None),
            "thought": getattr(result, "thought", None),
        }
    return None


def build_actions_message(message: Any) -> Optional[Dict[str, Any]]:
    """把 AutoDL 的 action 消息转换为 BetterGI 的 actions 消息。

    优先使用服务端已结构化的 chunks；缺失时尝试用 lumine_protocol 解析
    action 原始文本。无法得到合法 chunks 时返回 None，绝不转发任意文本。
    Lumine 的移动前缀会复制到所有键块，转发前只保留首块位移，其余清零。
    """
    if not isinstance(message, dict):
        return None
    msg_type = message.get("type")
    if not isinstance(msg_type, str) or msg_type.strip().lower() != "action":
        return None

    chunks = normalize_chunks(message.get("chunks"))
    absolute = normalize_absolute(message.get("absolute"))
    thought = message.get("thought") if isinstance(message.get("thought"), str) else None

    if chunks is None:
        raw = message.get("action")
        if isinstance(raw, str) and raw.strip():
            parsed = _parse_action_text(raw)
            if parsed:
                chunks = normalize_chunks(parsed.get("chunks"))
                if absolute is None:
                    absolute = normalize_absolute(parsed.get("absolute"))
                parsed_thought = parsed.get("thought")
                if not thought and isinstance(parsed_thought, str):
                    thought = parsed_thought

    if chunks is None:
        return None

    # ??/???????? 200ms ??BetterGI ?????????????????
    chunks = apply_movement_prefix_once(chunks)

    seq = _optional_int(message.get("forFrameSeq"))
    if seq is None:
        seq = _optional_int(message.get("seq"))

    if isinstance(thought, str) and len(thought) > MAX_THOUGHT_CHARS:
        thought = thought[:MAX_THOUGHT_CHARS]

    # ??????????????????????raw/latencyMs ??????????
    return {
        "type": "actions",
        "seq": seq,
        "chunks": chunks,
        "absolute": absolute,
        "thought": thought,
    }


def action_matches_motor_mode(
    payload: Dict[str, Any], mode: str, dialogue_permissions: Any = None
) -> bool:
    """Enforce input limits, not visual-state or semantic-intent verification."

    Dialogue input remains blocked until the executor can verify a specific
    authorized operation against the same observed dialogue state.
    """
    if mode not in {"navigate", "combat", "interact", "explore", "ui", "wait", "dialogue"}:
        return False
    chunks = payload.get("chunks") or []
    idle = not payload.get("absolute") and all(
        not chunk.get("keys") and not chunk.get("dx") and not chunk.get("dy")
        and not chunk.get("scroll") for chunk in chunks
    )
    if mode == "wait":
        return idle
    if mode == "dialogue":
        return idle
    if mode == "navigate":
        return not payload.get("absolute") and all(
            set(chunk.get("keys") or []) <= {"W", "A", "S", "D", "Space", "Shift", "Ctrl"}
            for chunk in chunks
        )
    if mode == "combat":
        # BetterGI trims tokens and maps these keys case-insensitively.
        return not payload.get("absolute") and all(
            isinstance(key, str) and key.strip().casefold() not in {"f", "esc"}
            for chunk in chunks for key in (chunk.get("keys") or [])
        )
    return True


def build_frame_message(
    frame: Any, guidance: str = "", now_ms: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """把 BetterGI 的 frame 消息转换为 AutoDL 的 frame 消息。"""
    if not isinstance(frame, dict):
        return None
    seq = _optional_int(frame.get("seq"))
    if seq is None:
        return None
    jpeg = frame.get("jpeg")
    if not isinstance(jpeg, str) or not jpeg:
        return None

    inline_guidance = frame.get("guidance")
    if isinstance(guidance, str) and guidance:
        # ?????????????????????????????
        selected_guidance = guidance
    elif isinstance(inline_guidance, str):
        selected_guidance = inline_guidance
    else:
        selected_guidance = ""

    ts_ms = iso_to_epoch_ms(frame.get("timestampUtc") or frame.get("ts"))
    if ts_ms is None:
        ts_ms = int(now_ms) if now_ms is not None else _now_ms()

    return {
        "type": "frame",
        "seq": seq,
        "tsMs": ts_ms,
        "jpeg": jpeg,
        "guidance": selected_guidance,
    }


def _json_fps(value: float) -> Any:
    """整数值的帧率以 JSON 整数输出，其余保留小数。"""
    number = float(value)
    return int(number) if number.is_integer() else number


class FramePump:
    """只保留最新一帧的采样器。

    调用方把到达的帧放入槽位，由转发循环按目标帧率取出。槽位中已有的
    旧帧会被新帧覆盖并计入丢弃，从而避免排队造成的延迟累积。
    """

    def __init__(self, fps: float):
        rate = float(fps)
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("fps 必须为正数")
        self.interval = 1.0 / rate
        self._pending: Optional[Dict[str, Any]] = None
        self._last_sent_at: Optional[float] = None
        self._last_seq: Optional[int] = None
        self.dropped = 0
        #: 最近一帧原始 frame 消息（含 base64 jpeg），给 /frame 判定用。
        self.last_frame: Optional[Dict[str, Any]] = None

    @property
    def pending(self) -> Optional[Dict[str, Any]]:
        """放入最新帧；已有未取出的帧时丢弃旧帧。"""
        return self._pending

    def put(self, frame: Any) -> None:
        """调用方持有意图锁，保留被撤销意图的原始归属。"""
        if not isinstance(frame, dict):
            return
        if self._pending is not None:
            self.dropped += 1
        self._pending = frame
        self.last_frame = frame

    def latest(self) -> Optional[Dict[str, Any]]:
        """最近一帧（只读），供判定模型每 2 秒取全屏图。"""
        return self.last_frame

    def reset(self) -> None:
        """停止回执历史已达上限，丢弃最早记录；迟到回执不会授权操作"""
        self._pending = None
        self._last_sent_at = None
        self._last_seq = None
        self.last_frame = None

    def poll(self, now: float) -> Optional[Dict[str, Any]]:
        """取出最早的一帧；超时或关闭且为空时返回 None。"""
        if self._pending is None:
            return None
        if self._last_sent_at is not None and (now - self._last_sent_at) < self.interval:
            return None

        frame = self._pending
        self._pending = None

        # ???????????????????????????????????????????
        seq = _optional_int(frame.get("seq"))
        if (
            seq is not None
            and self._last_seq is not None
            and seq == self._last_seq
        ):
            self.dropped += 1
            return None

        self._last_sent_at = now
        if seq is not None:
            self._last_seq = seq
        return frame


class RecordingQueue:
    """ffmpeg 录制的有界帧队列：队列满时丢弃最旧帧，绝不阻塞转发。"""

    def __init__(self, maxsize: int = RECORD_QUEUE_SIZE):
        if int(maxsize) < 1:
            raise ValueError("队列容量必须大于 0")
        self._maxsize = int(maxsize)
        self._items: Deque[bytes] = collections.deque()
        self._dropped = 0
        self._closed = False
        self._condition = threading.Condition()

    @property
    def dropped(self) -> int:
        """放入最新帧；已有未取出的帧时丢弃旧帧。"""
        with self._condition:
            return self._dropped

    @property
    def closed(self) -> bool:
        """生成可序列化的统计快照。"""
        with self._condition:
            return self._closed

    def __len__(self) -> int:
        with self._condition:
            return len(self._items)

    def put(self, item: bytes) -> bool:
        """停止回执历史已达上限，丢弃最早记录；迟到回执不会授权操作"""
        with self._condition:
            if self._closed:
                return False
            if len(self._items) >= self._maxsize:
                self._items.popleft()
                self._dropped += 1
            self._items.append(item)
            self._condition.notify()
            return True

    def get(self, timeout: Optional[float] = None) -> Optional[bytes]:
        """退出前尽力撤销推理和释放按键；不把发送视为执行确认。"""
        with self._condition:
            if not self._items and not self._closed:
                self._condition.wait(timeout)
            if self._items:
                return self._items.popleft()
            return None

    def close(self) -> None:
        """放入最新帧；已有未取出的帧时丢弃旧帧。"""
        with self._condition:
            self._closed = True
            self._condition.notify_all()


class Recorder:
    """通过 ffmpeg image2pipe 把 JPEG 帧录制成 MP4。

    写盘发生在独立线程中，转发线程只做一次 Base64 解码和入队，因此
    ffmpeg 变慢也不会阻塞画面转发。
    """

    def __init__(
        self,
        path: str,
        fps: float = DEFAULT_RECORD_FPS,
        queue_size: int = RECORD_QUEUE_SIZE,
    ):
        rate = float(fps)
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("record fps 必须为正数")
        self.path = path
        self.fps = rate
        self.queue = RecordingQueue(queue_size)
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    @property
    def running(self) -> bool:
        """断线后自动重连（默认启用）"""
        return self._running

    def start(self) -> bool:
        """启动 ffmpeg；找不到 ffmpeg 时记录警告并返回 False。"""
        if not self.path:
            return False

        executable = shutil.which("ffmpeg")
        if not executable:
            LOG.warning("未找到 ffmpeg，录制已禁用（画面仍会正常转发）。")
            return False

        command = [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "image2pipe",
            "-framerate",
            str(self.fps),
            "-i",
            "-",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            self.path,
        ]
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            LOG.warning("未找到 ffmpeg，录制已禁用（画面仍会正常转发）。", exc)
            self._process = None
            return False

        self._running = True
        self._thread = threading.Thread(
            target=self._write_loop, name="lumine-recorder", daemon=True
        )
        self._thread.start()
        LOG.info("统计输出间隔秒数，默认 %(default)s")
        return True

    def submit_jpeg(self, jpeg_base64: Any) -> bool:
        """解码并提交一帧 Base64 JPEG；失败或录制未启动时返回 False。"""
        if not self._running or not isinstance(jpeg_base64, str) or not jpeg_base64:
            return False
        try:
            data = base64.b64decode(jpeg_base64)
        except (binascii.Error, ValueError, TypeError):
            return False
        if not data:
            return False
        return self.queue.put(data)

    def _write_loop(self) -> None:
        """录制线程：从队列取帧写入 ffmpeg stdin。"""
        process = self._process
        stdin = process.stdin if process is not None else None
        if stdin is None:
            self._running = False
            return

        while True:
            item = self.queue.get(timeout=0.1)
            if item is None:
                if self.queue.closed:
                    break
                continue
            try:
                stdin.write(item)
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                LOG.warning("录制写入失败，停止录制：%s", exc)
                break

        self._running = False
        try:
            stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def close(self) -> None:
        """退出前尽力撤销推理和释放按键；不把发送视为执行确认。"""
        if self._process is None and self._thread is None:
            return

        self.queue.close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

        process = self._process
        if process is not None:
            try:
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
            try:
                process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass

        self._process = None
        self._thread = None
        self._running = False
        LOG.info("桥接任务异常退出：%s", self.path)


class BridgeStats:
    """生成可序列化的统计快照。"""

    def __init__(self):
        self.frames_in = 0
        self.frames_out = 0
        self.actions_in = 0
        self.actions_out = 0
        # 意图空窗期按 5fps 重发最近动作块的次数，单独统计，不改变既有字段语义。
        self.actions_held = 0
        self.actions_rejected = 0
        self.guidance_in = 0
        self.commentary_in = 0
        self.action_state_in = 0
        self.reconnects_bettergi = 0
        self.reconnects_lumine = 0
        self.latency_last_ms: Optional[float] = None
        self._latency_count = 0
        self._latency_total = 0.0

    def record_latency(self, value: Any) -> None:
        """记录 AutoDL 返回的 latencyMs。"""
        try:
            latency = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(latency) or latency < 0:
            return
        self.latency_last_ms = latency
        self._latency_count += 1
        self._latency_total += latency

    def snapshot(self, frame_drops: int = 0, record_drops: int = 0) -> Dict[str, Any]:
        """关闭队列并唤醒等待中的写入线程。"""
        average = (
            self._latency_total / self._latency_count
            if self._latency_count
            else None
        )
        return {
            "framesIn": self.frames_in,
            "framesOut": self.frames_out,
            "actionsIn": self.actions_in,
            "actionsOut": self.actions_out,
            "actionsHeld": self.actions_held,
            "actionsRejected": self.actions_rejected,
            "guidanceIn": self.guidance_in,
            "commentaryIn": self.commentary_in,
            "actionStateIn": self.action_state_in,
            "latencyLastMs": self.latency_last_ms,
            "latencyAvgMs": round(average, 1) if average is not None else None,
            "frameDrops": int(frame_drops),
            "recordDrops": int(record_drops),
            "reconnectsBettergi": self.reconnects_bettergi,
            "reconnectsLumine": self.reconnects_lumine,
        }


def log_stats(
    stats: BridgeStats, frame_drops: int = 0, record_drops: int = 0
) -> None:
    """输出桥接统计。"""
    data = stats.snapshot(frame_drops, record_drops)
    last = data["latencyLastMs"]
    average = data["latencyAvgMs"]
    LOG.info(
        "统计：帧 入/出=%s/%s，动作 入/出/拒绝/保持=%s/%s/%s/%s，AutoDL 延迟 最近=%s ms 平均=%s ms，队列丢弃 帧=%s 录制=%s，重连 BetterGI=%s AutoDL=%s",
        data["framesIn"],
        data["framesOut"],
        data["actionsIn"],
        data["actionsOut"],
        data["actionsRejected"],
        data["actionsHeld"],
        "-" if last is None else last,
        "-" if average is None else average,
        data["frameDrops"],
        data["recordDrops"],
        data["reconnectsBettergi"],
        data["reconnectsLumine"],
    )


# ----------------------------------------------------------------------
# ????
# ----------------------------------------------------------------------


@dataclass
class BridgeConfig:
    """录制是否正在进行。"""

    bettergi_ws: str = DEFAULT_BETTERGI_WS
    bettergi_token: str = ""
    lumine_ws: str = DEFAULT_LUMINE_WS
    lumine_token: str = ""
    fps: float = DEFAULT_FPS
    record: Optional[str] = None
    record_fps: float = DEFAULT_RECORD_FPS
    reconnect: bool = DEFAULT_RECONNECT
    stats_interval: float = DEFAULT_STATS_INTERVAL
    session_id: str = DEFAULT_SESSION_ID
    motor_protocol: str = "v1"
    intent_host: str = "127.0.0.1"
    intent_port: int = 0
    intent_token: str = ""


class LumineBridge:
    """BetterGI Live Hub 与 AutoDL Player Server 之间的异步桥接。"""

    def __init__(self, config: BridgeConfig):
        self.config = config
        self.stats = BridgeStats()
        self.pump = FramePump(config.fps)
        self.latest_guidance = ""
        self.latest_guidance_at_ms = 0
        self._action_seq = 0
        self.session_id = config.session_id or DEFAULT_SESSION_ID
        self.latest_intent: Optional[Dict[str, Any]] = None
        self._motor_generation = 0
        self._release_unconfirmed = False
        # ??????????????????????????????
        self._mode_violations = 0
        # A successfully written cancellation is not an acknowledgement.
        self._pending_cancel_generation: Optional[int] = None
        # Keep a bounded stop-receipt history; eviction never authorizes release.
        self._stop_requests: Dict[str, Dict[str, Any]] = {}
        self._stop_request_connections: Dict[str, Any] = {}
        self._stop_request_motor_connections: Dict[str, Any] = {}
        self._verified_bettergi_session: Optional[str] = None
        self._verified_bettergi_ws: Optional[Any] = None
        self._verified_bettergi_feedback_protocol: Optional[str] = None
        self._lease_binding: Optional[Dict[str, Any]] = None
        self._lease_binding_ws: Optional[Any] = None
        # A lost host response must never cause the same decision to be resent.
        self._dialogue_requests: set[tuple[str, str]] = set()
        self._stop_request_prefix = uuid.uuid4().hex
        self._stop_request_counter = 0
        # Successfully written v2 action sequences for the current generation
        # form a contiguous interval; a write is not an execution receipt.
        self._first_forwarded_seq: Optional[int] = None
        self._last_forwarded_seq: Optional[int] = None
        # 意图空窗期用于续按的最近动作块（chunks/absolute/attribution），
        # 以及它最近一次写入宿主的 monotonic 时间。
        self._held_action: Optional[Dict[str, Any]] = None
        self._held_action_at: Optional[float] = None
        # v2 意图热切换的 hold 窗口：期间保留租约与按键，只让 Motor 停产出。
        # _hold_expires_at 为 monotonic 截止时刻；None 表示不处于 hold。
        self._hold_expires_at: Optional[float] = None
        self._hold_ms = 0
        self._intent_lock = asyncio.Lock()
        self.intent_feedback: Deque[Dict[str, Any]] = collections.deque(maxlen=20)
        self._intent_runner: Optional[Any] = None
        self._intent_site: Optional[Any] = None
        self._intent_started = False

        self._session: Optional[Any] = None
        self._bettergi_ws: Optional[Any] = None
        self._lumine_ws: Optional[Any] = None
        self._motor_connection_generation = 0
        self._motor_connection_authenticated = False
        self._motor_connected_origin: Optional[Dict[str, str]] = None
        self._motor_connected_endpoint: Optional[str] = None
        self._motor_connected_auth: Optional[str] = None
        self._motor_capability: Optional[Dict[str, Any]] = None
        self._bettergi_send_lock = asyncio.Lock()
        self._lumine_send_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._recorder: Optional[Recorder] = (
            Recorder(config.record, config.record_fps) if config.record else None
        )

    # ------------------------------------------------------------------
    # ????
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """本地立即撤销，再尽力向两端发送停止；发送成功不等于执行成功。"""
        if aiohttp is None:
            raise RuntimeError(
                "需要安装 aiohttp：python -m pip install aiohttp"
            )

        if not self.config.bettergi_token:
            LOG.warning(
                "未设置 BetterGI 令牌（--bettergi-token 或 BETTERGI_AGENT_TOKEN），连接可能被拒绝。"
            )
        if not self.config.lumine_token:
            LOG.warning(
                "未设置 AutoDL 令牌（--lumine-token 或 LUMINE_SERVER_TOKEN）。"
            )

        # ??????????????????????????????
        build_ws_url(
            self.config.bettergi_ws, self.config.bettergi_token, "access_token"
        )
        build_ws_url(self.config.lumine_ws, self.config.lumine_token, "token")

        self._running = True
        self._stop_event.clear()
        await self._start_intent_server()
        self._session = aiohttp.ClientSession()
        if self._recorder is not None:
            self._recorder.start()

        # ??????????????????????????stop_actions??
        loop = asyncio.get_running_loop()
        previous_handlers = {}

        def _handle_signal(signum, frame):
            loop.call_soon_threadsafe(self.stop)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous_handlers[sig] = signal.signal(sig, _handle_signal)
            except (ValueError, OSError):
                # ????????????????????????KeyboardInterrupt??
                pass

        self._tasks = [
            asyncio.create_task(self._bettergi_loop(), name="lumine-bettergi"),
            asyncio.create_task(self._lumine_loop(), name="lumine-autodl"),
            asyncio.create_task(self._forward_loop(), name="lumine-forward"),
            asyncio.create_task(self._action_hold_loop(), name="lumine-action-hold"),
            asyncio.create_task(self._stats_loop(), name="lumine-stats"),
        ]
        stop_task = asyncio.create_task(self._stop_event.wait(), name="lumine-stop")

        try:
            await asyncio.wait(
                [*self._tasks, stop_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            self._running = False
            await self._shutdown()
            await self._stop_intent_server()
            for task in [*self._tasks, stop_task]:
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                *self._tasks, stop_task, return_exceptions=True
            )
            for task in self._tasks:
                if task.cancelled():
                    continue
                error = task.exception()
                if error is not None:
                    LOG.error("桥接任务异常退出：%s", _safe_error(error, self.config.bettergi_token,
                                                                      self.config.lumine_token, self.config.intent_token))
            if self._session is not None:
                await self._session.close()
                self._session = None
            if self._recorder is not None:
                self._recorder.close()
            self._tasks = []
            for sig, handler in previous_handlers.items():
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass
            LOG.info("队列是否已关闭。")

    def stop(self) -> None:
        """本地立即撤销，再尽力向两端发送停止；发送成功不等于执行成功。"""
        self._running = False
        self._stop_event.set()

    # -- ???? -------------------------------------------------------

    @staticmethod
    def _normalize_intent_bus(data: Any) -> Optional[Dict[str, Any]]:
        """桥接侧的轻量意图校验；完整 schema 由 PlayerAgent 负责。"""
        if not isinstance(data, dict):
            return None
        payload = data.get("intent") if isinstance(data.get("intent"), dict) else data
        intent_id = payload.get("intent_id")
        goal = payload.get("goal")
        if not isinstance(intent_id, str) or not intent_id.strip():
            return None
        if not isinstance(goal, str) or not goal.strip():
            return None
        constraints = payload.get("constraints", [])
        interrupts = payload.get("interrupt_conditions", [])
        deadline = payload.get("deadline_ms", 15000)
        if (not isinstance(constraints, list) or not isinstance(interrupts, list)
                or isinstance(deadline, bool) or not isinstance(deadline, int)
                or deadline <= 0):
            return None
        intent = {
            "type": "intent",
            "intent_id": intent_id.strip()[:80],
            "goal": " ".join(goal.split())[:300],
            "reason": str(payload.get("reason") or "")[:400],
            "motor_mode": str(payload.get("motor_mode") or "navigate")[:20],
            "constraints": [
                str(item)[:200] for item in (payload.get("constraints") or [])[:8]
            ],
            "target": payload.get("target") if isinstance(payload.get("target"), dict) else {},
            "interrupt_conditions": [
                str(item)[:60]
                for item in (payload.get("interrupt_conditions") or [])[:8]
            ],
            "deadline_ms": int(payload.get("deadline_ms") or 15000),
        }
        permissions = payload.get("dialogue_permissions")
        if permissions is not None:
            if (not isinstance(permissions, dict)
                    or set(permissions) - {"allow_skip", "allow_select"}
                    or any(type(value) is not bool for value in permissions.values())):
                return None
            intent["dialogue_permissions"] = dict(permissions)
        group = payload.get("actionGroupId")
        if group is not None:
            if not isinstance(group, str) or not group.strip() or group != group.strip() or len(group) > 128:
                return None
            intent["actionGroupId"] = group
        return intent

    def _current_attribution(self) -> Optional[Dict[str, Any]]:
        """连接重建后清空采样状态，避免旧会话的序号造成永久丢弃。"""
        if self.latest_intent is None:
            return None
        result = {"intentId": self.latest_intent["intent_id"],
                  "motorGeneration": self._motor_generation}
        if "actionGroupId" in self.latest_intent:
            result["actionGroupId"] = self.latest_intent["actionGroupId"]
        return result

    async def set_intent(self, data: Any) -> Optional[Dict[str, Any]]:
        """连接重建后清空采样状态，避免旧会话的序号造成永久丢弃。"""
        intent = self._normalize_intent_bus(data)
        if intent is None:
            return None
        if self.config.motor_protocol == "v2":
            source = data.get("intent") if isinstance(data.get("intent"), dict) else data
            if (source.get("intent_id") != intent["intent_id"]
                    or intent["motor_mode"] not in {"navigate", "combat", "interact", "explore", "ui", "wait", "dialogue"}
                    or ("motor_mode" in source and source["motor_mode"] != intent["motor_mode"])):
                return None
        async with self._intent_lock:
            if self.config.motor_protocol == "v2" and (self._lease_binding is None
                    or self._lease_binding["status"] != "bound"
                    or self._lease_binding_ws is not self._bettergi_ws
                    or self._lease_binding["motor_ws"] is not self._lumine_ws
                    or self._lease_binding["motor_generation"] != self._motor_connection_generation
                    or _motor_origin(self.config.lumine_ws) != self._motor_connected_origin
                    or _motor_endpoint_identity(self.config.lumine_ws, self.config.lumine_token) != self._motor_connected_endpoint
                    or _motor_auth_identity(self.config.lumine_ws, self.config.lumine_token) != self._motor_connected_auth):
                return None
            if (self.config.motor_protocol == "v2" and self._lease_binding is not None
                    and time.monotonic() >= self._lease_binding["lease_deadline"]):
                await self._clear_intent_locked("lease_expired_or_unconfirmed")
                return None
            if self._release_unconfirmed or self._pending_cancel_generation is not None:
                LOG.warning("本地立即撤销，再尽力向两端发送停止；发送成功不等于执行成功。")
                return None
            if self._hold_expires_at is not None:
                # hold 窗口只能由持有租约动作凭据的 /intent/switch 接管，
                # 否则普通 /intent 会落到撤销分支，白丢一次无缝切换的机会。
                LOG.warning("hold 窗口内拒绝普通 /intent；请用 /intent/switch 接管。")
                return None
            if intent == self.latest_intent:
                return {**intent, **({"motorGeneration": self._motor_generation} if self.config.motor_protocol == "v2" else {})}
            replacing = self.latest_intent is not None
            if replacing and self.config.motor_protocol == "v2":
                # A stop write is not proof of release. Revoke first and refuse
                # the replacement until both sides have verifiable evidence.
                await self._clear_intent_locked("replaced_by_new_intent")
                return None
            self._motor_generation += 1
            self.latest_intent = None
            self._first_forwarded_seq = None
            self._last_forwarded_seq = None
            self._release_held_locked()
            self.pump.reset()
            if replacing and not await self._send_bettergi({"type": "stop_actions"}):
                self._release_unconfirmed = True
                LOG.warning("意图替换时无法发送 stop_actions；拒绝新意图")
                return None
            self.latest_intent = intent
            attribution = self._current_attribution()
            payload = {"type": "intent", "intent": intent}
            if self.config.motor_protocol == "v2":
                payload.update(attribution)
            if not await self._send_lumine(payload):
                self.latest_intent = None
                # ??????????????????????????????????
                if self.config.motor_protocol == "v2":
                    self._release_unconfirmed = True
                LOG.warning("意图未发送到 Motor；拒绝新意图")
                return None
            self._record_feedback("progress", "intent_sent", intent["intent_id"], attribution=attribution)
            return {**intent, **({"motorGeneration": self._motor_generation} if self.config.motor_protocol == "v2" else {})}

    def _lease_ready_locked(self, now: Optional[float] = None) -> bool:
        """当前 v2 租约是否可承载新动作（调用方须持有意图锁）。"""
        if self.config.motor_protocol != "v2":
            return False
        binding = self._lease_binding
        if binding is None or binding["status"] != "bound":
            return False
        moment = time.monotonic() if now is None else now
        return bool(moment < binding["lease_deadline"]
                    and self._lease_binding_ws is self._bettergi_ws
                    and binding["motor_ws"] is self._lumine_ws
                    and binding["motor_generation"] == self._motor_connection_generation
                    and self._verified_bettergi_session == binding["session_id"]
                    and self._motor_connected_origin is not None
                    and _motor_origin(self.config.lumine_ws) == self._motor_connected_origin
                    and _motor_endpoint_identity(self.config.lumine_ws, self.config.lumine_token) == self._motor_connected_endpoint
                    and _motor_auth_identity(self.config.lumine_ws, self.config.lumine_token) == self._motor_connected_auth)

    def _reattribute_held_locked(self, attribution: Optional[Dict[str, Any]]) -> None:
        """把最近动作块改挂到新归属，避免续按因代际不符被丢弃。"""
        if self._held_action is not None and attribution is not None:
            self._held_action["attribution"] = dict(attribution)

    @staticmethod
    def _lease_credentials_match(binding_credentials: Any, lease: Any) -> bool:
        """校验调用方提交的动作凭据与当前绑定完全一致（secret 常量时间比较）。"""
        if not isinstance(lease, dict) or set(lease) != {"secret", "instanceId", "epoch"}:
            return False
        if (not isinstance(lease.get("secret"), str) or re.fullmatch(r"[0-9a-fA-F]{64}", lease["secret"]) is None
                or not isinstance(lease.get("instanceId"), str) or re.fullmatch(r"[0-9a-f]{32}", lease["instanceId"]) is None
                or type(lease.get("epoch")) is not int or not 1 <= lease["epoch"] <= 2**63 - 1):
            return False
        if not isinstance(binding_credentials, dict):
            return False
        if (binding_credentials.get("instanceId") != lease["instanceId"]
                or binding_credentials.get("epoch") != lease["epoch"]):
            return False
        secret = binding_credentials.get("secret")
        return isinstance(secret, str) and hmac.compare_digest(secret.encode("utf-8"), lease["secret"].encode("utf-8"))

    async def switch_intent(self, data: Any, lease: Any) -> Dict[str, Any]:
        """v2 热切换：不松键、不撤销，把租约与最近动作块直接改挂到新意图。

        与 set_intent 的替换分支相反：不发 stop_actions、不 reset 帧泵、
        不清租约绑定；只有当前租约的动作凭据才能触发。发送失败时保持原状。
        """
        intent = self._normalize_intent_bus(data)
        if intent is None:
            return {"accepted": False, "error": "bad_intent"}
        if self.config.motor_protocol == "v2":
            source = data.get("intent") if isinstance(data.get("intent"), dict) else data
            if (source.get("intent_id") != intent["intent_id"]
                    or intent["motor_mode"] not in {"navigate", "combat", "interact", "explore", "ui", "wait", "dialogue"}
                    or ("motor_mode" in source and source["motor_mode"] != intent["motor_mode"])):
                return {"accepted": False, "error": "bad_intent"}
        async with self._intent_lock:
            binding = self._lease_binding
            if not self._lease_ready_locked():
                return {"accepted": False, "error": "lease_not_bound"}
            if not self._lease_credentials_match(binding["credentials"], lease):
                return {"accepted": False, "error": "lease_mismatch"}
            if self._release_unconfirmed or self._pending_cancel_generation is not None:
                return {"accepted": False, "error": "release_unconfirmed"}
            previous = self.latest_intent
            if previous is not None and previous == intent:
                return {"accepted": True, "switched": False, "replaced": False,
                        "intent_id": intent["intent_id"], "motorGeneration": self._motor_generation,
                        "attribution": self._current_attribution(), "intent": intent}
            generation = self._motor_generation + 1
            attribution = {"intentId": intent["intent_id"], "motorGeneration": generation}
            if "actionGroupId" in intent:
                attribution["actionGroupId"] = intent["actionGroupId"]
            if not await self._send_lumine({"type": "intent", "intent": intent, **attribution}):
                # 发送失败时保持原状：旧意图与续按都不动，交给 hold 到期兜底。
                return {"accepted": False, "error": "motor_unavailable"}
            self._motor_generation = generation
            self.latest_intent = intent
            self._hold_expires_at = None
            self._hold_ms = 0
            self._reattribute_held_locked(attribution)
            self._record_feedback("progress", "intent_switched", intent["intent_id"], attribution=attribution)
            return {"accepted": True, "switched": True, "replaced": previous is not None,
                    "intent_id": intent["intent_id"], "motorGeneration": generation,
                    "attribution": attribution, "intent": intent}

    async def hold_intent(self, hold_ms: Any) -> Dict[str, Any]:
        """hold：保留意图、租约与最近动作块，只让 Motor 停止产生新动作。

        期间续按继续按 5fps 写宿主（最多 hold_ms），窗口内到达的
        /intent/switch 直接接管；到期由 _action_hold_tick 松键撤销。
        """
        if type(hold_ms) is not int or not 1 <= hold_ms <= MAX_INTENT_HOLD_MS:
            return {"held": False, "error": "bad_hold_ms"}
        async with self._intent_lock:
            now = time.monotonic()
            if (not self._lease_ready_locked(now) or self.latest_intent is None
                    or self._release_unconfirmed or self._pending_cancel_generation is not None):
                return {"held": False, "error": "not_active"}
            self._motor_generation += 1
            self._hold_ms = hold_ms
            self._hold_expires_at = now + hold_ms / 1000.0
            attribution = self._current_attribution()
            self._reattribute_held_locked(attribution)
            sent = await self._send_lumine({"type": "cancel_intent", "motorGeneration": self._motor_generation})
            self._record_feedback("progress", "intent_held", self.latest_intent["intent_id"],
                                  attribution=attribution)
            return {"held": True, "holdMs": hold_ms, "motorGeneration": self._motor_generation,
                    "expiresInMs": hold_ms, "motorCancelSent": bool(sent),
                    "intent_id": self.latest_intent["intent_id"]}

    async def bind_live_lease(self, data: Any) -> Optional[str]:
        """Bind only action credentials to the current Live socket, never to Motor."""
        if not isinstance(data, dict) or set(data) not in ({"sessionId", "credentials"}, {"sessionId", "credentials", "requestId"}):
            return None
        credentials, session_id = data["credentials"], data["sessionId"]
        requested_id = data.get("requestId")
        if requested_id is not None and (not isinstance(requested_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", requested_id) is None):
            return None
        if (not isinstance(session_id, str) or not session_id or not isinstance(credentials, dict)
                or set(credentials) != {"secret", "instanceId", "epoch"}
                or not isinstance(credentials.get("secret"), str)
                or re.fullmatch(r"[0-9a-fA-F]{64}", credentials["secret"]) is None
                or not isinstance(credentials.get("instanceId"), str)
                or re.fullmatch(r"[0-9a-f]{32}", credentials["instanceId"]) is None
                or type(credentials.get("epoch")) is not int or not 1 <= credentials["epoch"] <= 2**63 - 1):
            return None
        async with self._intent_lock:
            ws, motor = self._bettergi_ws, self._lumine_ws
            if (self.config.motor_protocol != "v2" or self.latest_intent is not None
                    or self._lease_binding is not None or ws is None or ws.closed
                    or motor is None or motor.closed or not self._motor_connection_authenticated
                    or self._verified_bettergi_ws is not ws or self._verified_bettergi_session != session_id):
                return None
            request_id = requested_id or uuid.uuid4().hex
            self._lease_binding = {"request_id": request_id, "session_id": session_id,
                                   "credentials": dict(credentials), "status": "pending",
                                   "motor_ws": motor, "motor_generation": self._motor_connection_generation,
                                   "connection_id": None, "commit_requested": False,
                                   "probe_id": request_id, "probe_sent_at": time.monotonic(),
                                   "binding_started_at": time.monotonic(),
                                   "probe_pending": True, "lease_deadline": 0.0}
            self._lease_binding_ws = ws
            sent = await self._send_bettergi({"type": "lease.bind", "leaseRequestId": request_id,
                                             "lease": credentials}, expected_ws=ws, require_expected_ws=True)
            if not sent:
                self._lease_binding = None
                self._lease_binding_ws = None
                return None
            return request_id

    async def _refresh_release_evidence(self, session_id: str, ws: Any) -> None:
        """用 BetterGI 自己的安全状态补证输入已释放。

        停止回执可能因 WebSocket 断开而丢失；这里的判据是执行器报告"释放调用
        已返回且没有按下任何按键"。它仍然只证明系统层调用已完成，不证明游戏
        响应，因此只在会话一致、没有待确认取消时才解除阻塞。
        """
        if self._session is None or ws is None or ws.closed or ws is not self._bettergi_ws:
            LOG.info("补证跳过：binding 连接与当前 BetterGI 连接不一致")
            return
        parts = urlsplit(self.config.bettergi_ws)
        scheme = "https" if parts.scheme == "wss" else "http"
        url = "{0}://{1}/api/v1/agent/live/status".format(scheme, parts.netloc)
        headers = {"Authorization": "Bearer " + self.config.bettergi_token}
        try:
            async with self._session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=3)) as response:
                if response.status != 200:
                    return
                payload = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return
        if not isinstance(payload, dict) or payload.get("sessionId") != session_id:
            LOG.info("补证跳过：状态会话不匹配")
            return
        safety = payload.get("safety")
        if (payload.get("pressedKeys") != [] or not isinstance(safety, dict)
                or safety.get("inputReleaseCallsReturned") is not True):
            LOG.info("补证跳过：pressedKeys=%s 释放调用返回=%s",
                     payload.get("pressedKeys"),
                     safety.get("inputReleaseCallsReturned") if isinstance(safety, dict) else None)
            return
        # ?????????????????????inputReleaseConfirmed ??
        # ????????????????? false???????????"??????"
        if safety.get("trackedKeyCount") not in (None, 0) and safety.get("inputReleaseConfirmed") is not True:
            return
        if self._pending_cancel_generation is not None:
            # Motor ????????????BetterGI ????????????
            # ??????????????????????????
            self._pending_cancel_generation = None
        self._release_unconfirmed = False
        LOG.info("以 BetterGI 安全状态补证输入已释放（会话 %s）", session_id)

    async def clear_intent(self, reason: str = "session_reset") -> Dict[str, Any]:
        """本地立即撤销，再尽力向两端发送停止；发送成功不等于执行成功。"""
        async with self._intent_lock:
            return await self._clear_intent_locked(reason)

    async def _clear_intent_locked(self, reason: str) -> Dict[str, Any]:
        """配置文件中存在该键时返回其值，否则返回内置默认值。"""
        self._hold_expires_at = None
        self._hold_ms = 0
        old = self.latest_intent
        had_binding = self._lease_binding is not None
        attribution = self._current_attribution()
        if old is not None:
            self._record_feedback("cancelled", reason, intent_id=old["intent_id"], attribution=attribution)
        self._motor_generation += 1
        self.latest_intent = None
        self._lease_binding = None
        self._lease_binding_ws = None
        self._first_forwarded_seq = None
        self._last_forwarded_seq = None
        self._release_held_locked()
        self.pump.reset()
        if self.config.motor_protocol == "v2":
            # Neither a write nor Motor's cancellation ack proves that the
            # BetterGI input backend has actually released held inputs.
            self._release_unconfirmed = True
            # Set the expected generation before either send can yield.  An early
            # ack must not be lost or mistaken for a newer cancellation.
            self._pending_cancel_generation = self._motor_generation
            # Register before either send yields, so a fast ACK cannot be lost.
            self._stop_request_counter += 1
            stop_id = "{0}-{1}".format(self._stop_request_prefix, self._stop_request_counter)
            stop_ws = self._bettergi_ws
            motor_ws = self._lumine_ws
            if len(self._stop_requests) >= MAX_STOP_REQUEST_HISTORY:
                oldest = next(iter(self._stop_requests))
                self._stop_requests.pop(oldest)
                self._stop_request_connections.pop(oldest, None)
                self._stop_request_motor_connections.pop(oldest, None)
                LOG.warning("本地立即撤销，再尽力向两端发送停止；发送成功不等于执行成功。")
            self._stop_requests[stop_id] = {
                "stopRequestId": stop_id, "inputReleaseSent": None, "remoteStopSent": None,
                "motorCancelAcknowledged": False, "ack": None,
                "sessionId": self._verified_bettergi_session if stop_ws is self._verified_bettergi_ws else None,
                "motorGeneration": self._motor_generation,
            }
            if stop_ws is not None and not stop_ws.closed:
                self._stop_request_connections[stop_id] = stop_ws
            if motor_ws is not None and not motor_ws.closed:
                self._stop_request_motor_connections[stop_id] = motor_ws
            remote, released = await asyncio.gather(
                self._send_lumine({"type": "cancel_intent", "motorGeneration": self._motor_generation}),
                self._send_bettergi({"type": "stop_actions", "stopRequestId": stop_id},
                                    expected_ws=stop_ws, require_expected_ws=True),
            )
            self._stop_requests[stop_id]["inputReleaseSent"] = released
            self._stop_requests[stop_id]["remoteStopSent"] = remote
        else:
            remote = False
            released = await self._send_bettergi({"type": "stop_actions"})
            self._release_unconfirmed = not released
        if not remote or not released:
            LOG.warning("取消仅在本地生效；远端停止发送=%s，输入释放发送=%s", remote, released)
        result = {"remoteStopSent": remote, "inputReleaseSent": released}
        if self.config.motor_protocol == "v2":
            result.update(stopRequestId=stop_id, motorGeneration=self._motor_generation)
        return result

    def _record_feedback(
        self, status: str, reason: str, intent_id: str = "", observation: str = "",
        attribution: Optional[Dict[str, Any]] = None, seq: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        current = self.latest_intent
        if not intent_id and current is not None:
            intent_id = str(current.get("intent_id") or "")
        if not intent_id:
            return None
        if status == "done":
            status, reason = "progress", "motor_reported_done_unconfirmed"
            observation = "Motor 自报完成；必须由后续独立世界观察确认，当前结果未知。"
        if self.config.motor_protocol == "v2":
            if attribution is None:
                attribution = self._current_attribution()
            if (not attribution or type(attribution.get("motorGeneration")) is not int
                    or attribution != self._current_attribution()
                    or attribution.get("intentId") != intent_id):
                return None
            # A known failure or block for this exact generation dominates later
            # progress/cancellation, including when the intent ID is reused.
            known = [row for row in self.intent_feedback
                     if row.get("intent_id") == intent_id
                     and row.get("motorGeneration") == attribution["motorGeneration"]
                     and row.get("actionGroupId") == attribution.get("actionGroupId")
                     and row.get("status") in {"failed", "blocked"}]
            if known and (status != "failed" or any(row["status"] == "failed" for row in known)):
                return None
        feedback = {
            "type": "motor_feedback",
            "intent_id": intent_id,
            "status": status,
            "reason": reason[:200],
            "observation": observation[:400],
            "t": _now_ms(),
        }
        if self.config.motor_protocol == "v2":
            # Feedback reports bridge/protocol state, never verified game input.
            feedback["executionConfirmed"] = False
            feedback["motorGeneration"] = attribution["motorGeneration"]
            if "actionGroupId" in attribution:
                feedback["actionGroupId"] = attribution["actionGroupId"]
            if seq is not None:
                feedback["seq"] = seq
        if self.config.motor_protocol == "v2" and len(self.intent_feedback) == self.intent_feedback.maxlen:
            # Ordinary progress cannot evict a known failure, even from an
            # older generation. Bound memory if all retained rows are failures.
            removable = next((index for index, row in enumerate(self.intent_feedback)
                              if row.get("status") not in {"failed", "blocked"}), None)
            if removable is not None:
                del self.intent_feedback[removable]
            elif status not in {"failed", "blocked"}:
                return None
        self.intent_feedback.append(feedback)
        return feedback

    async def _request_motor_capability_locked(self) -> None:
        """Challenge the current connection; this only obtains a server self-report."""
        ws = self._lumine_ws
        origin = _motor_origin(self.config.lumine_ws)
        endpoint = _motor_endpoint_identity(self.config.lumine_ws, self.config.lumine_token)
        auth = _motor_auth_identity(self.config.lumine_ws, self.config.lumine_token)
        if (self.config.motor_protocol != "v2" or not self._motor_connection_authenticated
                or ws is None or ws.closed or self._motor_connection_generation < 1
                or origin is None or origin != self._motor_connected_origin
                or endpoint is None or endpoint != self._motor_connected_endpoint
                or auth is None or auth != self._motor_connected_auth):
            self._motor_capability = None
            return
        current = self._motor_capability
        now = time.monotonic()
        if (current is not None and current["ws"] is ws and current["origin"] == origin
                and current["endpoint"] == endpoint and current["auth"] == auth
                and current["generation"] == self._motor_connection_generation
                and now < current["deadline"]):
            return
        challenge = {"ws": ws, "generation": self._motor_connection_generation, "origin": origin,
                     "endpoint": endpoint, "auth": auth, "nonce": uuid.uuid4().hex, "deadline": now + MOTOR_CAPABILITY_TTL,
                     "confirmed": False}
        self._motor_capability = challenge
        try:
            sent = await self._send_lumine({"type": "motor_capability_query", "nonce": challenge["nonce"],
                                             "connectionGeneration": challenge["generation"]})
        except BaseException:
            if self._motor_capability is challenge:
                self._motor_capability = None
            raise
        if not sent and self._motor_capability is challenge:
            self._motor_capability = None

    def intent_snapshot(self) -> Dict[str, Any]:
        """启动本机 HTTP 意图总线；未配置端口时不启动。"""
        result = {
            "protocol": self.config.motor_protocol,
            "current": dict(self.latest_intent) if self.latest_intent else None,
            "feedback": list(self.intent_feedback),
        }
        if self.config.motor_protocol == "v2":
            # These records survive subsequent stops and disconnects in this process.
            result["stopRequests"] = list(self._stop_requests.values())
            result["inputReleaseUnconfirmed"] = self._release_unconfirmed
            result["activeMotorGeneration"] = self._motor_generation
            if self._hold_expires_at is not None:
                result["holdRemainingMs"] = max(0, int((self._hold_expires_at - time.monotonic()) * 1000))
                result["holdMs"] = self._hold_ms
            result["motorConnectionActive"] = self._lumine_ws is not None and not self._lumine_ws.closed
            result["bettergiConnectionActive"] = (self._bettergi_ws is not None and not self._bettergi_ws.closed
                                                   and self._verified_bettergi_ws is self._bettergi_ws
                                                   and bool(self._verified_bettergi_session))
            result["verifiedLiveSessionId"] = self._verified_bettergi_session
            capability = self._motor_capability
            capability_now = time.monotonic()
            if capability is not None and capability["deadline"] - capability_now < 0.001:
                self._motor_capability = None
                capability = None
            if (capability is not None and capability["confirmed"] and self._motor_connection_authenticated
                    and capability["ws"] is self._lumine_ws and self._lumine_ws is not None
                    and not self._lumine_ws.closed and capability["generation"] == self._motor_connection_generation
                    and capability["origin"] == self._motor_connected_origin
                    and capability["origin"] == _motor_origin(self.config.lumine_ws)
                    and capability["endpoint"] == self._motor_connected_endpoint
                    and capability["endpoint"] == _motor_endpoint_identity(self.config.lumine_ws, self.config.lumine_token)
                    and capability["auth"] == self._motor_connected_auth
                    and capability["auth"] == _motor_auth_identity(self.config.lumine_ws, self.config.lumine_token)
                    and time.monotonic() < capability["deadline"]):
                result["motorPromptProtocol"] = "v2_requires_motor_prompt"
                result["motorPromptEvidence"] = "server_self_report"
                result["motorCapability"] = {
                    "origin": capability["origin"]["origin"],
                    "transport": capability["origin"]["transport"],
                    "nonce": capability["nonce"],
                    "connectionGeneration": capability["generation"],
                    "expiresInMs": int((capability["deadline"] - capability_now) * 1000),
                    "declarations": dict(capability["declarations"]),
                    "evidence": "server_self_report",
                    "bridgeConstraintProtocol": "intent-v2-mode-enforcement-v1",
                }
            binding = self._lease_binding
            if binding is not None:
                result["liveLeaseBinding"] = {"requestId": binding["request_id"], "sessionId": binding["session_id"],
                                              "instanceId": binding["credentials"]["instanceId"],
                                              "epoch": binding["credentials"]["epoch"], "status": binding["status"],
                                              "connectionId": binding["connection_id"],
                                              "motorConnectionGeneration": binding["motor_generation"],
                                              "expiresInMs": max(0, int((binding["lease_deadline"] - time.monotonic()) * 1000))}
                if (binding["status"] in {"prepared_bound", "commit_pending", "bound"}
                        and time.monotonic() < binding["lease_deadline"]
                        and self._verified_bettergi_feedback_protocol == "intent-v2-system-input-v1"
                        and self._verified_bettergi_ws is self._bettergi_ws
                        and self._verified_bettergi_session == binding["session_id"]
                        and self._lease_binding_ws is self._bettergi_ws and self._bettergi_ws is not None
                        and not self._bettergi_ws.closed and binding["motor_ws"] is self._lumine_ws
                        and self._lumine_ws is not None and not self._lumine_ws.closed
                        and binding["motor_generation"] == self._motor_connection_generation
                        and self._motor_connection_authenticated and self._motor_connected_origin is not None
                        and _motor_origin(self.config.lumine_ws) == self._motor_connected_origin
                        and _motor_endpoint_identity(self.config.lumine_ws, self.config.lumine_token) == self._motor_connected_endpoint
                        and _motor_auth_identity(self.config.lumine_ws, self.config.lumine_token) == self._motor_connected_auth):
                    result["executionFeedbackProtocol"] = "intent-v2-system-input-v1"
                    result["executionFeedbackEvidence"] = "host_channel_declaration"
                    result["executionFeedbackGameResponseConfirmed"] = None
        return result

    async def _start_intent_server(self) -> None:
        """启动本机 HTTP 意图总线；未配置端口时不启动。"""
        if self.config.motor_protocol != "v2" or self.config.intent_port <= 0:
            return
        host = self.config.intent_host
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        expected = self.config.intent_token
        if (not loopback or not isinstance(expected, str) or not expected.strip()):
            raise ValueError("v2 intent bus requires a loopback IP and a non-empty bearer token")
        if aiohttp_web is None:
            raise RuntimeError("aiohttp.web is required for the v2 intent bus")

        def authorized(request: Any) -> bool:
            auth = request.headers.get("Authorization", "")
            return hmac.compare_digest(auth.encode("utf-8"), ("Bearer " + expected).encode("utf-8"))

        app = aiohttp_web.Application()

        async def post_intent(request: Any) -> Any:
            if not authorized(request):
                return aiohttp_web.json_response({"accepted": False, "error": "unauthorized"}, status=401)
            try:
                data = await request.json()
            except Exception:
                return aiohttp_web.json_response({"accepted": False, "error": "bad_json"}, status=400)
            if isinstance(data, dict) and data.get("cancel"):
                result = await self.clear_intent("cancelled_by_player")
                return aiohttp_web.json_response({"accepted": True, "cancelled": True,
                                                  "executionConfirmed": False, **result})
            if self._normalize_intent_bus(data) is None:
                return aiohttp_web.json_response({"accepted": False, "error": "bad_intent"}, status=400)
            intent = await self.set_intent(data)
            if intent is None:
                return aiohttp_web.json_response({"accepted": False, "error": "motor_unavailable"}, status=503)
            attribution = {"motorGeneration": intent["motorGeneration"]}
            if "actionGroupId" in intent:
                attribution["actionGroupId"] = intent["actionGroupId"]
            return aiohttp_web.json_response({"accepted": True, "intent_id": intent["intent_id"], **attribution})

        async def post_intent_switch(request: Any) -> Any:
            """热切换：同一租约下把新意图交给 Motor，保留绑定与最近动作块。"""
            if not authorized(request):
                return aiohttp_web.json_response({"accepted": False, "error": "unauthorized"}, status=401)
            try:
                data = json.loads(await request.text(), object_pairs_hook=_unambiguous_json_object)
            except (TypeError, ValueError):
                return aiohttp_web.json_response({"accepted": False, "error": "bad_json"}, status=400)
            if not isinstance(data, dict) or set(data) != {"intent", "lease"}:
                return aiohttp_web.json_response({"accepted": False, "error": "bad_request"}, status=400)
            result = await self.switch_intent({"intent": data["intent"]}, data["lease"])
            if result.get("accepted") is not True:
                error = result.get("error")
                status = (409 if error in {"lease_not_bound", "lease_mismatch", "release_unconfirmed"}
                          else 503 if error == "motor_unavailable" else 400)
                return aiohttp_web.json_response(result, status=status)
            return aiohttp_web.json_response({"accepted": True, "intent_id": result["intent_id"],
                                              "motorGeneration": result["motorGeneration"],
                                              "switched": result["switched"], "replaced": result["replaced"],
                                              "executionConfirmed": False})

        async def post_intent_hold(request: Any) -> Any:
            """hold：保留意图与动作块，只让 Motor 停止产生新动作。"""
            if not authorized(request):
                return aiohttp_web.json_response({"accepted": False, "error": "unauthorized"}, status=401)
            try:
                data = json.loads(await request.text(), object_pairs_hook=_unambiguous_json_object)
            except (TypeError, ValueError):
                return aiohttp_web.json_response({"accepted": False, "error": "bad_json"}, status=400)
            if not isinstance(data, dict) or set(data) != {"hold_ms"}:
                return aiohttp_web.json_response({"accepted": False, "error": "bad_request"}, status=400)
            result = await self.hold_intent(data["hold_ms"])
            if result.get("held") is not True:
                status = 409 if result.get("error") == "not_active" else 400
                return aiohttp_web.json_response({"accepted": False, **result}, status=status)
            return aiohttp_web.json_response({"accepted": True, "executionConfirmed": False, **result})

        async def get_feedback(request: Any) -> Any:
            if not authorized(request):
                return aiohttp_web.json_response({"error": "unauthorized"}, status=401)
            async with self._intent_lock:
                binding = self._lease_binding
                ws = self._lease_binding_ws
                now = time.monotonic()
                if binding is not None:
                    valid = (ws is not None and ws is self._bettergi_ws and not ws.closed
                             and self._verified_bettergi_ws is ws
                             and self._verified_bettergi_session == binding["session_id"]
                             and binding["motor_ws"] is self._lumine_ws and self._lumine_ws is not None
                             and not self._lumine_ws.closed
                             and binding["motor_generation"] == self._motor_connection_generation
                             and self._motor_connected_origin is not None
                             and _motor_origin(self.config.lumine_ws) == self._motor_connected_origin
                             and _motor_endpoint_identity(self.config.lumine_ws, self.config.lumine_token) == self._motor_connected_endpoint
                             and _motor_auth_identity(self.config.lumine_ws, self.config.lumine_token) == self._motor_connected_auth)
                    expired = (binding["status"] != "pending" and now >= binding["lease_deadline"]
                               or binding["status"] == "pending" and now - binding["binding_started_at"] >= 3)
                    if not valid or expired:
                        await self._clear_intent_locked("lease_expired_or_connection_changed")
                        binding = None
                should_probe = (binding is not None and binding["status"] in
                    {"pending", "prepared_bound", "commit_pending", "bound"}
                    and (not binding["probe_pending"] or now - binding["probe_sent_at"] >= 1))
                if should_probe:
                    probe_id = uuid.uuid4().hex
                    binding["probe_id"] = probe_id
                    binding["probe_sent_at"] = now
                    binding["probe_pending"] = True
                    request_id = binding["request_id"]
            if should_probe:
                sent = await self._send_bettergi({"type": "lease.probe", "leaseRequestId": request_id,
                                                  "leaseProbeId": probe_id},
                                                 expected_ws=ws, require_expected_ws=True)
                async with self._intent_lock:
                    if not sent and self._lease_binding is binding and binding["probe_id"] == probe_id:
                        await self._clear_intent_locked("lease_probe_send_unconfirmed")
            async with self._intent_lock:
                await self._request_motor_capability_locked()
                snapshot = self.intent_snapshot()
            return aiohttp_web.json_response(snapshot)

        async def post_dialogue_decision(request: Any) -> Any:
            if not authorized(request):
                return aiohttp_web.json_response({"error": "unauthorized"}, status=401)
            try:
                data = json.loads(await request.text(), object_pairs_hook=_unambiguous_json_object)
                intent = data["intent"]
                source = intent["source"]
                if (set(data) != {"intent"} or set(intent) != {"type", "intent_id", "action", "source",
                        "option_id", "option_index", "option_text", "execution"}
                        or set(source) != {"event_session_id", "capture_session_id", "frame_id", "frame_seq",
                            "frame_sha256", "captured_at_ms", "source_event_id", "thought_id", "decision_id",
                            "player_generation"}
                        or intent["type"] != "dialogue_intent" or intent["execution"] != "not_executed"
                        or source["decision_id"] != source["thought_id"]
                        or not isinstance(intent["intent_id"], str)
                        or re.fullmatch(r"di-[0-9a-f]{64}", intent["intent_id"]) is None
                        or not isinstance(source["decision_id"], str)
                        or not source["decision_id"] or len(source["decision_id"]) > 128
                        or not isinstance(source["capture_session_id"], str)
                        or not source["capture_session_id"] or len(source["capture_session_id"]) > 128):
                    raise ValueError("bad dialogue intent")
                ws_url, host_headers = _ws_endpoint_and_headers(self.config.bettergi_ws, self.config.bettergi_token)
                parts = urlsplit(ws_url)
                if parts.hostname != "localhost" and not ipaddress.ip_address(parts.hostname).is_loopback:
                    raise ValueError("dialogue host must be loopback")
                host_url = urlunsplit(("https" if parts.scheme == "wss" else "http", parts.netloc,
                                       "/api/v1/agent/live/dialogue/decision", "", ""))
            except (KeyError, TypeError, ValueError):
                return aiohttp_web.json_response({"error": "bad_dialogue_intent"}, status=400)

            async with self._intent_lock:
                binding = self._lease_binding
                ws = self._bettergi_ws
                key = (source["capture_session_id"], source["decision_id"])
                if (self.config.motor_protocol != "v2" or self.latest_intent is not None
                        or self._release_unconfirmed or self._pending_cancel_generation is not None
                        or binding is None or binding["status"] != "bound" or ws is None or ws.closed
                        or time.monotonic() >= binding["lease_deadline"]
                        or binding["motor_ws"] is not self._lumine_ws or self._lumine_ws is None
                        or self._lumine_ws.closed or binding["motor_generation"] != self._motor_connection_generation
                        or _motor_origin(self.config.lumine_ws) != self._motor_connected_origin
                        or _motor_endpoint_identity(self.config.lumine_ws, self.config.lumine_token) != self._motor_connected_endpoint
                        or _motor_auth_identity(self.config.lumine_ws, self.config.lumine_token) != self._motor_connected_auth
                        or self._lease_binding_ws is not ws or self._verified_bettergi_ws is not ws
                        or self._verified_bettergi_session != source["capture_session_id"]
                        or binding["session_id"] != source["capture_session_id"]):
                    return aiohttp_web.json_response({"intentId": intent["intent_id"], "executed": False,
                                                       "reason": "dialogue_lease_unavailable"})
                if key in self._dialogue_requests or len(self._dialogue_requests) >= 1024:
                    return aiohttp_web.json_response({"intentId": intent["intent_id"], "executed": False,
                                                       "reason": "dialogue_duplicate_or_capacity"})
                self._dialogue_requests.add(key)
                lease = dict(binding["credentials"])
            host_headers.update({"X-Bgi-Lease-Secret": lease["secret"],
                                 "X-Bgi-Lease-Instance": lease["instanceId"],
                                 "X-Bgi-Lease-Epoch": str(lease["epoch"])})
            payload = {"intentId": intent["intent_id"], "decisionId": source["decision_id"],
                       "eventSessionId": source["event_session_id"], "sessionId": source["capture_session_id"],
                       "frameId": source["frame_id"], "frameSeq": source["frame_seq"],
                       "frameSha256": source["frame_sha256"], "capturedAtMs": source["captured_at_ms"],
                       "sourceEventId": source["source_event_id"], "playerGeneration": source["player_generation"],
                       "action": intent["action"], "optionId": intent["option_id"],
                       "optionIndex": intent["option_index"], "optionText": intent["option_text"]}
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5), trust_env=False) as client:
                    async with client.post(host_url, json=payload, headers=host_headers, allow_redirects=False) as response:
                        if response.status != 200:
                            return aiohttp_web.json_response({"error": "dialogue_host_unconfirmed"}, status=503)
                        receipt = await response.json()
                if (not isinstance(receipt, dict) or receipt.get("intentId") != intent["intent_id"]
                        or receipt.get("executed") not in (True, False, None)
                        or type(receipt.get("executed")) not in (bool, type(None))
                        or type(receipt.get("systemInputAccepted")) not in (bool, type(None))
                        or receipt.get("gameResponseConfirmed") is not None
                        or (receipt.get("executed") is True and receipt.get("systemInputAccepted") is not True)
                        or not isinstance(receipt.get("reason"), str)):
                    return aiohttp_web.json_response({"error": "dialogue_host_unconfirmed"}, status=503)
                return aiohttp_web.json_response({"intentId": intent["intent_id"],
                    "executed": receipt.get("executed"), "systemInputAccepted": receipt.get("systemInputAccepted"),
                    "gameResponseConfirmed": None, "reason": receipt["reason"]})
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                return aiohttp_web.json_response({"error": "dialogue_host_unconfirmed"}, status=503)

        async def post_lease_binding(request: Any) -> Any:
            if not authorized(request):
                return aiohttp_web.json_response({"error": "unauthorized"}, status=401)
            try:
                data = json.loads(await request.text(), object_pairs_hook=_unambiguous_json_object)
            except (ValueError, TypeError):
                return aiohttp_web.json_response({"error": "bad_json"}, status=400)
            request_id = await self.bind_live_lease(data)
            if request_id is None:
                return aiohttp_web.json_response({"accepted": False, "error": "binding_unavailable"}, status=409)
            return aiohttp_web.json_response({"accepted": True, "requestId": request_id, "status": "pending"})

        async def commit_lease_binding(request: Any) -> Any:
            if not authorized(request):
                return aiohttp_web.json_response({"error": "unauthorized"}, status=401)
            try:
                data = json.loads(await request.text(), object_pairs_hook=_unambiguous_json_object)
                if (not isinstance(data, dict) or set(data) != {"requestId", "sessionId", "instanceId", "epoch",
                        "connectionId", "motorConnectionGeneration"}
                        or not isinstance(data["requestId"], str)
                        or re.fullmatch(r"[0-9a-f]{32}", data["requestId"]) is None
                        or type(data["epoch"]) is not int or type(data["motorConnectionGeneration"]) is not int):
                    raise ValueError("bad binding")
            except (TypeError, ValueError):
                return aiohttp_web.json_response({"error": "bad_request"}, status=400)
            async with self._intent_lock:
                binding, ws = self._lease_binding, self._lease_binding_ws
                if (binding is not None and binding["status"] == "prepared_bound"
                        and self._release_unconfirmed and self.latest_intent is None):
                    # ???????????WebSocket ????????????BetterGI ????
                    # ?????????????????????????????
                    # ?????????????????"??????"
                    await self._refresh_release_evidence(binding["session_id"], ws)
                if (binding is None or binding["status"] != "prepared_bound" or self.latest_intent is not None
                        or self._release_unconfirmed or self._pending_cancel_generation is not None
                        or time.monotonic() >= binding["lease_deadline"]
                        or ws is None or ws is not self._bettergi_ws or ws.closed
                        or self._verified_bettergi_ws is not ws
                        or self._verified_bettergi_session != binding["session_id"]
                        or binding["motor_ws"] is not self._lumine_ws or self._lumine_ws is None
                        or self._lumine_ws.closed or not self._motor_connection_authenticated
                        or binding["motor_generation"] != self._motor_connection_generation
                        or _motor_origin(self.config.lumine_ws) != self._motor_connected_origin
                        or _motor_endpoint_identity(self.config.lumine_ws, self.config.lumine_token) != self._motor_connected_endpoint
                        or _motor_auth_identity(self.config.lumine_ws, self.config.lumine_token) != self._motor_connected_auth
                        or data["requestId"] != binding["request_id"] or data["sessionId"] != binding["session_id"]
                        or data["instanceId"] != binding["credentials"]["instanceId"]
                        or data["epoch"] != binding["credentials"]["epoch"]
                        or data["connectionId"] != binding["connection_id"]
                        or data["motorConnectionGeneration"] != binding["motor_generation"]):
                    return aiohttp_web.json_response({"accepted": False, "error": "binding_unavailable"}, status=409)
                binding["commit_requested"] = True
                binding["status"] = "commit_pending"
                binding["probe_id"] = uuid.uuid4().hex
                binding["probe_sent_at"] = time.monotonic()
                binding["probe_pending"] = True
                probe_id = binding["probe_id"]
                sent = await self._send_bettergi({"type": "lease.activate", "leaseRequestId": binding["request_id"],
                                                  "leaseProbeId": probe_id},
                                                 expected_ws=ws, require_expected_ws=True)
                if not sent:
                    await self._clear_intent_locked("lease_activation_send_unconfirmed")
                    return aiohttp_web.json_response({"accepted": False, "error": "activation_unconfirmed"}, status=503)
                return aiohttp_web.json_response({"accepted": True, "requestId": binding["request_id"],
                                                  "status": "commit_pending"})

        async def abandon_lease_binding(request: Any) -> Any:
            if not authorized(request):
                return aiohttp_web.json_response({"error": "unauthorized"}, status=401)
            try:
                data = json.loads(await request.text(), object_pairs_hook=_unambiguous_json_object)
            except (ValueError, TypeError):
                return aiohttp_web.json_response({"error": "bad_json"}, status=400)
            request_id = data.get("requestId") if isinstance(data, dict) and set(data) == {"requestId"} else None
            if not isinstance(request_id, str) or re.fullmatch(r"[0-9a-f]{32}", request_id) is None:
                return aiohttp_web.json_response({"error": "bad_request"}, status=400)
            async with self._intent_lock:
                binding = self._lease_binding
                if binding is None or binding["request_id"] != request_id:
                    return aiohttp_web.json_response({"abandoned": False}, status=409)
                result = await self._clear_intent_locked("lease_binding_abandoned")
            return aiohttp_web.json_response({"abandoned": True, "requestId": request_id,
                                              "stopRequested": result.get("inputReleaseSent") is True})

        app.router.add_post("/intent", post_intent)
        app.router.add_post("/intent/switch", post_intent_switch)
        app.router.add_post("/intent/hold", post_intent_hold)
        app.router.add_post("/dialogue/decision", post_dialogue_decision)
        app.router.add_post("/lease/bind", post_lease_binding)
        app.router.add_post("/lease/commit", commit_lease_binding)
        app.router.add_post("/lease/abandon", abandon_lease_binding)
        async def get_latest_frame(request: Any) -> Any:
            """返回桥接收到的最近一帧（base64 jpeg），给判定模型用。

            Live 运行期间不能再去打宿主的 observe（会打断租约，实测 lease_lost），
            所以判定图必须取自这条已在流动的帧流。
            """
            if not authorized(request):
                return aiohttp_web.json_response({"error": "unauthorized"}, status=401)
            frame = self.pump.latest() if self.pump is not None else None
            jpeg = frame.get("jpeg") if isinstance(frame, dict) else None
            if not isinstance(jpeg, str) or not jpeg:
                return aiohttp_web.json_response({"ok": False, "reason": "no_frame"})
            return aiohttp_web.json_response(
                {
                    "ok": True,
                    "frameId": frame.get("seq"),
                    "jpegBase64": jpeg,
                    "capturedAt": frame.get("timestampUtc"),
                }
            )

        app.router.add_get("/feedback", get_feedback)
        app.router.add_get("/frame", get_latest_frame)
        runner = aiohttp_web.AppRunner(app)
        await runner.setup()
        site = aiohttp_web.TCPSite(runner, self.config.intent_host, self.config.intent_port)
        await site.start()
        self._intent_runner = runner
        self._intent_site = site
        self._intent_started = True
        LOG.info(
            "意图总线已启动：http://%s:%s/intent（protocol=%s）",
            self.config.intent_host,
            self.config.intent_port,
            self.config.motor_protocol,
        )

    async def _stop_intent_server(self) -> None:
        runner = self._intent_runner
        self._intent_runner = None
        self._intent_site = None
        self._intent_started = False
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception as exc:  # noqa: BLE001
                LOG.debug("；输入及游戏响应未知", exc)

    async def _shutdown(self) -> None:
        """本地立即撤销，再尽力向两端发送停止；发送成功不等于执行成功。"""
        if self.config.motor_protocol == "v2":
            await self.clear_intent("bridge_shutdown")
            return
        ws = self._bettergi_ws
        if ws is None or ws.closed:
            return
        text = json.dumps({"type": "stop_actions"}, ensure_ascii=False)
        try:
            async with self._bettergi_send_lock:
                await asyncio.wait_for(ws.send_str(text), timeout=2.0)
            LOG.info("已向 BetterGI 发送 stop_actions。")
        except Exception as exc:
            LOG.debug("意图替换时无法发送 stop_actions；拒绝新意图", exc)

    # ------------------------------------------------------------------
    # BetterGI ??
    # ------------------------------------------------------------------

    async def _bettergi_loop(self) -> None:
        """连接 BetterGI，断线后按 1～20 秒指数退避重连。"""
        assert self._session is not None
        url, headers = _ws_endpoint_and_headers(self.config.bettergi_ws, self.config.bettergi_token)

        backoff = MIN_BACKOFF
        while self._running:
            reason = "连接关闭"
            connected_at: Optional[float] = None
            try:
                async with self._session.ws_connect(
                    url,
                    headers=headers,
                    heartbeat=20.0,
                    max_msg_size=MAX_WS_MESSAGE_SIZE,
                ) as ws:
                    self._bettergi_ws = ws
                    connected_at = time.monotonic()
                    # ?????????????????????????
                    self.pump.reset()
                    self.latest_guidance = ""
                    self.latest_guidance_at_ms = 0
                    if self.config.motor_protocol == "v2":
                        # ????????????????????????
                        await self.clear_intent("bettergi_connected")
                    LOG.info("忽略无法解析的 BetterGI 消息。", _safe_endpoint(self.config.bettergi_ws))
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_bettergi_message(message.data, source_ws=ws)
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            reason = _safe_error(ws.exception(), self.config.bettergi_token,
                                                 self.config.lumine_token, self.config.intent_token)
                            break
                        elif message.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                        ):
                            reason = (
                                "关闭码 {0}".format(ws.close_code)
                                if ws.close_code is not None
                                else "连接关闭"
                            )
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = _safe_error(exc, self.config.bettergi_token, self.config.lumine_token, self.config.intent_token) or exc.__class__.__name__
                LOG.warning("BetterGI 动作处理错误：", reason)
            finally:
                self._bettergi_ws = None
                self._verified_bettergi_session = None
                self._verified_bettergi_ws = None
                self._verified_bettergi_feedback_protocol = None
                if connected_at is not None and self.config.motor_protocol == "v2":
                    # A late ACK from this connection can never match a later one.
                    for stop_id, stop_ws in list(self._stop_request_connections.items()):
                        if stop_ws is ws:
                            del self._stop_request_connections[stop_id]
                    await self.clear_intent("bettergi_disconnected")
                # ???????????????????????????1 ????????
                if (
                    connected_at is not None
                    and time.monotonic() - connected_at
                    >= STABLE_CONNECTION_SECONDS
                ):
                    backoff = MIN_BACKOFF

            if not self._running:
                break

            self.stats.reconnects_bettergi += 1
            if not self.config.reconnect:
                LOG.warning("BetterGI %s，已禁用重连。")
                self.stop()
                break
            LOG.warning("BetterGI %s；%.1f 秒后重连。")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def _handle_bettergi_message(self, data: Any, source_ws: Any = None) -> None:
        """处理一条 BetterGI 服务端消息；回执必须来自发出请求的连接。"""
        if self.config.motor_protocol == "v2" and source_ws is not None and source_ws is not self._bettergi_ws:
            return
        try:
            message = json.loads(data)
        except (TypeError, ValueError):
            LOG.debug("忽略无法解析的 BetterGI 消息。")
            return
        if not isinstance(message, dict):
            return

        msg_type = message.get("type")
        if not isinstance(msg_type, str):
            return
        msg_type = msg_type.strip().lower()

        if msg_type == "hello":
            try:
                hello = json.loads(data, object_pairs_hook=_unambiguous_json_object)
            except (TypeError, ValueError):
                return
            session_id = hello.get("sessionId")
            if (not isinstance(session_id, str) or not session_id or session_id != session_id.strip()
                    or len(session_id) > 128 or source_ws is None or source_ws is not self._bettergi_ws or source_ws.closed):
                self._verified_bettergi_session = None
                self._verified_bettergi_ws = None
                self._verified_bettergi_feedback_protocol = None
                if self.config.motor_protocol == "v2":
                    await self.clear_intent("live_identity_unverified")
                return
            self.session_id = session_id
            newly_verified = (self._verified_bettergi_ws is not source_ws
                              or self._verified_bettergi_session != session_id)
            self._verified_bettergi_session = session_id
            self._verified_bettergi_ws = source_ws
            self._verified_bettergi_feedback_protocol = (
                "intent-v2-system-input-v1" if hello.get("executionFeedbackProtocol")
                == "intent-v2-system-input-v1" else None)
            if (newly_verified and self.config.motor_protocol == "v2"
                    and (self._release_unconfirmed or self._lease_binding is not None or self.latest_intent is not None)):
                await self.clear_intent("live_session_verified")
            LOG.info(
                "BetterGI hello：session=%s fps=%s 画面=%sx%s 动作间隔=%sms 块数=%s",
                self.session_id,
                message.get("fps"),
                message.get("frameWidth"),
                message.get("frameHeight"),
                message.get("actionIntervalMs"),
                message.get("chunksPerAction"),
            )
            return

        if msg_type == "live.started" or msg_type == "live.stopped":
            self._verified_bettergi_session = None
            self._verified_bettergi_ws = None
            self._verified_bettergi_feedback_protocol = None
            if source_ws is not None:
                for stop_id, stop_ws in list(self._stop_request_connections.items()):
                    if stop_ws is source_ws:
                        del self._stop_request_connections[stop_id]
            if msg_type == "live.started" and source_ws is self._bettergi_ws and source_ws is not None and not source_ws.closed:
                try:
                    start = json.loads(data, object_pairs_hook=_unambiguous_json_object)
                    live_id = start.get("sessionId")
                    if start.get("type") == "live.started" and isinstance(live_id, str) and live_id:
                        self.session_id = live_id
                        self._verified_bettergi_session = live_id
                        self._verified_bettergi_ws = source_ws
                except (TypeError, ValueError):
                    pass
            # ??????????????????/????????????
            # ????????????
            self.pump.reset()
            self.latest_guidance = ""
            self.latest_guidance_at_ms = 0
            await self.clear_intent("live_session_changed")
            if msg_type == "live.started":
                # 新会话意味着执行器重新初始化，旧会话的按键跟踪与租约都不存在。
                # “输入未确认”只约束旧会话，不能跨会话把 commit 永久挡在 409。
                async with self._intent_lock:
                    self._release_unconfirmed = False
                    self._pending_cancel_generation = None
            LOG.info("BetterGI 实时会话状态：%s，已清空旧帧与指导")
            return

        if msg_type == "frame":
            jpeg = message.get("jpeg")
            if _optional_int(message.get("seq")) is None or not isinstance(
                jpeg, str
            ) or not jpeg:
                return
            self.stats.frames_in += 1
            inline_guidance = message.get("guidance")
            if isinstance(inline_guidance, str):
                frame_ts = iso_to_epoch_ms(message.get("timestampUtc"))
                if frame_ts is None:
                    frame_ts = _now_ms()
                if (
                    not self.latest_guidance
                    or frame_ts >= self.latest_guidance_at_ms
                ):
                    self.latest_guidance = inline_guidance
                    self.latest_guidance_at_ms = frame_ts
            self.pump.put(message)
            if self._recorder is not None:
                self._recorder.submit_jpeg(jpeg)
            return

        if msg_type == "guidance":
            text = message.get("text")
            if not isinstance(text, str):
                return
            self.stats.guidance_in += 1
            self.latest_guidance = text
            guidance_ts = iso_to_epoch_ms(message.get("timestampUtc"))
            self.latest_guidance_at_ms = (
                guidance_ts if guidance_ts is not None else _now_ms()
            )
            await self._send_lumine({"type": "guidance", "text": text})
            return

        if msg_type == "commentary":
            # ??????????????????????????????
            # meta.internal?????????????????
            meta = message.get("meta")
            if isinstance(meta, dict) and meta.get("internal"):
                LOG.debug("BetterGI 连接异常：%s", message.get("text", ""))
                return
            self.stats.commentary_in += 1
            LOG.info("BetterGI 动作处理错误：", message.get("text", ""))
            return

        if msg_type == "lease.bound":
            try:
                receipt = json.loads(data, object_pairs_hook=_unambiguous_json_object)
            except (TypeError, ValueError):
                return
            async with self._intent_lock:
                binding = self._lease_binding
                if (receipt.get("type") != "lease.bound"
                        or set(receipt) != {"type", "leaseRequestId", "leaseInstanceId", "leaseEpoch",
                                            "sessionId", "leaseConnectionId", "leaseStatus", "leaseProbeId",
                                            "leaseExpiresInMs"}
                        or binding is None or binding["status"] not in {"pending", "prepared_bound", "commit_pending", "bound"}
                        or not binding["probe_pending"] or source_ws is None
                        or source_ws is not self._bettergi_ws or source_ws is not self._lease_binding_ws
                        or source_ws.closed or self._verified_bettergi_ws is not source_ws
                        or self._verified_bettergi_session != binding["session_id"]
                        or binding["motor_ws"] is not self._lumine_ws or self._lumine_ws is None
                        or self._lumine_ws.closed or binding["motor_generation"] != self._motor_connection_generation
                        or receipt.get("sessionId") != binding["session_id"]
                        or receipt.get("leaseRequestId") != binding["request_id"]
                        or receipt.get("leaseProbeId") != binding["probe_id"]
                        or receipt.get("leaseInstanceId") != binding["credentials"]["instanceId"]
                        or type(receipt.get("leaseEpoch")) is not int
                        or receipt["leaseEpoch"] != binding["credentials"]["epoch"]
                        or type(receipt.get("leaseExpiresInMs")) is not int
                        or not 0 < receipt["leaseExpiresInMs"] <= 3000
                        or not isinstance(receipt.get("leaseConnectionId"), str)
                        or re.fullmatch(r"[0-9a-f]{32}", receipt["leaseConnectionId"]) is None
                        or receipt.get("leaseStatus") not in {"prepared", "owner_committed", "active"}
                        or (binding["connection_id"] is not None
                            and receipt["leaseConnectionId"] != binding["connection_id"])):
                    return
                deadline = binding["probe_sent_at"] + receipt["leaseExpiresInMs"] / 1000
                if deadline <= time.monotonic():
                    await self._clear_intent_locked("lease_probe_expired")
                    return
                if receipt["leaseStatus"] == "active":
                    if not binding["commit_requested"] or binding["status"] not in {"commit_pending", "bound"}:
                        await self._clear_intent_locked("lease_activation_unrequested")
                        return
                    binding["status"] = "bound"
                elif binding["status"] == "bound":
                    await self._clear_intent_locked("lease_activation_lost")
                    return
                elif binding["status"] == "pending":
                    binding["status"] = "prepared_bound"
                binding["connection_id"] = receipt["leaseConnectionId"]
                binding["lease_deadline"] = deadline
                binding["probe_pending"] = False
            return

        if msg_type == "stop_actions_ack":
            LOG.debug("录制是否正在进行。", data[:500])
            if self.config.motor_protocol != "v2":
                return
            try:
                message = json.loads(data, object_pairs_hook=_unambiguous_json_object)
            except (TypeError, ValueError):
                return
            stop_id = message.get("stopRequestId")
            safety = message.get("safety")
            if (not isinstance(stop_id, str) or type(message.get("stopCallReturned")) is not bool
                    or not isinstance(safety, dict)):
                return
            scope = safety.get("scope")
            if scope == "stop_atomic":
                if (any(key not in safety for key in ("inputReleaseConfirmed", "systemReleaseRequestsAccepted"))
                        or type(safety.get("epoch")) is not int or safety["epoch"] < 0
                        or type(safety.get("trackedKeyCount")) is not int or safety["trackedKeyCount"] < 0
                        or type(safety.get("inputReleaseCallsReturned")) is not bool
                        or safety["inputReleaseConfirmed"] is not None
                        or (safety["systemReleaseRequestsAccepted"] is not None
                            and safety["systemReleaseRequestsAccepted"] is not True)
                        or (safety["systemReleaseRequestsAccepted"] is True
                            and (not safety["inputReleaseCallsReturned"] or not message["stopCallReturned"]))):
                    return
            elif scope == "post_stop_unattributed":
                if (any(key not in safety or (safety[key] is not None and type(safety[key]) is not bool)
                        for key in ("inputReleaseCallsReturned", "inputReleaseConfirmed"))
                        or "systemReleaseRequestsAccepted" not in safety
                        or safety["systemReleaseRequestsAccepted"] is not None):
                    return
            else:
                return
            async with self._intent_lock:
                if (source_ws is None or source_ws is not self._bettergi_ws or source_ws.closed
                        or self._stop_request_connections.get(stop_id) is not source_ws):
                    return
                request = self._stop_requests[stop_id]
                if scope == "stop_atomic" and (not request.get("sessionId")
                        or message.get("sessionId") != request["sessionId"]
                        or self._verified_bettergi_ws is not source_ws
                        or self._verified_bettergi_session != request["sessionId"]):
                    return
                fields = ("scope", "inputReleaseCallsReturned", "inputReleaseConfirmed",
                          "systemReleaseRequestsAccepted")
                if scope == "stop_atomic":
                    fields += ("epoch", "trackedKeyCount")
                request["ack"] = {
                    "stopRequestId": stop_id,
                    "stopCallReturned": message["stopCallReturned"],
                    "safety": {key: safety[key] for key in fields},
                    "receivedAtMs": _now_ms(),
                }
                del self._stop_request_connections[stop_id]
                # ????????"?????????????????????????"
                # ????????????????????????????????????
                # ???????????????????????????????
                # ?????????????????????
                released_evidence = message["stopCallReturned"] and (
                    (scope == "stop_atomic" and safety.get("trackedKeyCount") == 0
                     and safety.get("inputReleaseCallsReturned") is True)
                    or safety.get("systemReleaseRequestsAccepted") is True)
                if (released_evidence and self.latest_intent is None
                        and self._pending_cancel_generation is None):
                    self._release_unconfirmed = False

            return

        if msg_type == "action_state":
            self.stats.action_state_in += 1
            LOG.debug(
                "BetterGI 动作状态：keys=%s queue=%s starved=%s",
                message.get("keys"),
                message.get("queueDepth"),
                message.get("starved"),
            )
            return

        if msg_type == "action":
            # ???????????????????????????????????
            return

        if msg_type == "input_evidence":
            evidence = message.get("inputEvidence")
            if self.config.motor_protocol != "v2" or not isinstance(evidence, dict):
                return
            attribution = motor_attribution(message) if motor_attribution else None
            async with self._intent_lock:
                seq = message.get("seq")
                if (attribution is None or attribution != self._current_attribution()
                        or type(seq) is not int or self._first_forwarded_seq is None
                        or self._last_forwarded_seq is None or not self._first_forwarded_seq <= seq <= self._last_forwarded_seq
                        or not isinstance(message.get("attemptId"), str) or not message["attemptId"]
                        or evidence.get("attemptId") != message["attemptId"]
                        or source_ws is None or source_ws is not self._bettergi_ws
                        or source_ws is not self._lease_binding_ws):
                    return
                if (any(key in evidence for key in ("intentId", "motorGeneration", "actionGroupId"))
                        and motor_attribution(evidence) != attribution):
                    return
                status = evidence.get("status")
                if status not in {"accepted", "rejected", "partial", "unsupported", "unknown"}:
                    return
                feedback_status = "failed" if status in {"rejected", "partial", "unsupported"} else "progress"
                self._record_feedback(feedback_status, "input_system_" + status,
                                      intent_id=attribution["intentId"], attribution=attribution, seq=seq,
                                      observation="连接重建后清空采样状态，避免旧会话的序号造成永久丢弃。")
            return

        if msg_type == "action_attempt":
            attempt = message.get("executionAttempt")
            if self.config.motor_protocol != "v2" or not isinstance(attempt, dict):
                return
            attribution = motor_attribution(attempt) if motor_attribution else None
            async with self._intent_lock:
                seq = attempt.get("seq")
                if (attribution is None or attribution != self._current_attribution()
                        or type(seq) is not int or message.get("seq") != seq
                        or self._first_forwarded_seq is None or self._last_forwarded_seq is None
                        or not self._first_forwarded_seq <= seq <= self._last_forwarded_seq
                        or not isinstance(attempt.get("attemptId"), str) or not attempt["attemptId"]
                        or message.get("attemptId") != attempt["attemptId"]
                        or source_ws is None or source_ws is not self._bettergi_ws
                        or source_ws is not self._lease_binding_ws):
                    return
                if (any(key in message for key in ("intentId", "motorGeneration", "actionGroupId"))
                        and motor_attribution(message) != attribution):
                    return
                status = attempt.get("status")
                if status not in {"unknown", "failed", "aborted", "dropped"}:
                    return
                feedback_status = "failed" if status == "failed" else "progress"
                self._record_feedback(feedback_status, "input_attempt_" + status,
                                      intent_id=attribution["intentId"], attribution=attribution, seq=seq,
                                      observation="仅为本地输入调用尝试；游戏是否响应未知")
            return

        if msg_type == "error":
            lease_secret = ((self._lease_binding or {}).get("credentials") or {}).get("secret", "")
            LOG.warning(
                "BetterGI 连接异常：%s",
                _safe_error(message.get("code"), self.config.bettergi_token, self.config.intent_token, lease_secret),
                _safe_error(message.get("message"), self.config.bettergi_token, self.config.intent_token, lease_secret),
            )
            if self.config.motor_protocol != "v2":
                return
            # Duplicate/case-colliding JSON members cannot establish ownership.
            try:
                message = json.loads(data, object_pairs_hook=_unambiguous_json_object)
            except (TypeError, ValueError):
                LOG.warning("处理一条 BetterGI 服务端消息；回执必须来自发出请求的连接。")
                return
            if (message.get("code") == "LEASE_REJECTED" and isinstance(message.get("leaseRequestId"), str)):
                async with self._intent_lock:
                    binding = self._lease_binding
                    if (binding is not None and binding["status"] in {"pending", "prepared_bound", "commit_pending", "bound"}
                            and message["leaseRequestId"] == binding["request_id"]
                            and source_ws is self._bettergi_ws and source_ws is self._lease_binding_ws
                            and source_ws is not None and not source_ws.closed):
                        await self._clear_intent_locked("lease_binding_rejected")
                return
            attribution = motor_attribution(message) if motor_attribution else None
            seq = message.get("seq")
            code = message.get("code")
            valid = (attribution is not None and "actionGroupId" in attribution
                     and type(seq) is int and 0 <= seq <= 2**63 - 1
                     and isinstance(code, str) and bool(code.strip())
                     and all(not any(unicodedata.category(c) in {"Cc", "Cs"} for c in attribution[key])
                             and sum(2 if ord(c) > 0xFFFF else 1 for c in attribution[key]) <= 128
                             for key in ("intentId", "actionGroupId")))
            async with self._intent_lock:
                if (not valid or attribution != self._current_attribution()
                        or self._first_forwarded_seq is None
                        or self._last_forwarded_seq is None
                        or not self._first_forwarded_seq <= seq <= self._last_forwarded_seq):
                    LOG.warning("BetterGI 错误归属未知；未推断意图结果")
                    return
                self._record_feedback(
                    "failed", "bettergi_action_error", intent_id=attribution["intentId"],
                    attribution=attribution, seq=seq,
                    observation=("BetterGI 动作处理错误：" + code + "；输入及游戏响应未知"),
                )
                await self._clear_intent_locked("bettergi_action_error")
            return

        if msg_type == "pong":
            return

        LOG.debug("忽略无法解析的 BetterGI 消息。", msg_type)

    # ------------------------------------------------------------------
    # AutoDL ??
    # ------------------------------------------------------------------

    async def _lumine_loop(self) -> None:
        """连接 AutoDL Player Server，断线后按 1～20 秒指数退避重连。"""
        assert self._session is not None
        url, headers = _ws_endpoint_and_headers(self.config.lumine_ws, self.config.lumine_token)

        backoff = MIN_BACKOFF
        while self._running:
            reason = "连接关闭"
            connected_at: Optional[float] = None
            try:
                async with self._session.ws_connect(
                    url,
                    headers=headers,
                    heartbeat=20.0,
                    max_msg_size=MAX_WS_MESSAGE_SIZE,
                ) as ws:
                    async with self._intent_lock:
                        self._lumine_ws = ws
                        self._motor_connection_generation += 1
                        self._motor_connection_authenticated = bool(headers.get("Authorization"))
                        self._motor_connected_origin = _motor_origin(url)
                        self._motor_connected_endpoint = url
                        auth = headers.get("Authorization")
                        self._motor_connected_auth = hashlib.sha256(auth.encode("utf-8")).hexdigest() if auth else None
                        self._motor_capability = None
                        connected_at = time.monotonic()
                        hello = {
                            "type": "hello",
                            "sessionId": self.session_id,
                            "fps": _json_fps(self.config.fps),
                            "protocol": "lumine-live-v2" if self.config.motor_protocol == "v2" else PROTOCOL_NAME,
                        }
                        if not await self._send_lumine(hello):
                            raise RuntimeError("发送 hello 失败")
                        # ???????????????????? AutoDL ????????
                        if self.latest_guidance:
                            await self._send_lumine(
                                {"type": "guidance", "text": self.latest_guidance}
                            )
                        if self.config.motor_protocol == "v2":
                            # Reconnects require a fresh cancellation/stop and a new intent.
                            await self._clear_intent_locked("motor_connected")
                            await self._request_motor_capability_locked()
                        elif self.latest_intent is not None:
                            await self._send_lumine({"type": "intent", "intent": self.latest_intent})
                    LOG.info("连接 AutoDL Player Server，断线后按 1～20 秒指数退避重连。", _safe_endpoint(self.config.lumine_ws))
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_lumine_message(message.data, source_ws=ws)
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            reason = _safe_error(ws.exception(), self.config.bettergi_token,
                                                 self.config.lumine_token, self.config.intent_token)
                            break
                        elif message.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                        ):
                            reason = (
                                "关闭码 {0}".format(ws.close_code)
                                if ws.close_code is not None
                                else "连接关闭"
                            )
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = _safe_error(exc, self.config.bettergi_token, self.config.lumine_token, self.config.intent_token) or exc.__class__.__name__
                LOG.warning("向 AutoDL 发送一条 JSON 消息。", reason)
            finally:
                self._lumine_ws = None
                self._motor_connection_authenticated = False
                self._motor_connected_origin = None
                self._motor_connected_endpoint = None
                self._motor_connected_auth = None
                self._motor_capability = None
                if connected_at is not None and self.config.motor_protocol == "v2":
                    for stop_id, stop_ws in list(self._stop_request_motor_connections.items()):
                        if stop_ws is ws:
                            del self._stop_request_motor_connections[stop_id]
                    await self.clear_intent("motor_disconnected")
                # ????????????????????????
                if (
                    connected_at is not None
                    and time.monotonic() - connected_at
                    >= STABLE_CONNECTION_SECONDS
                ):
                    backoff = MIN_BACKOFF

            if not self._running:
                break

            self.stats.reconnects_lumine += 1
            if not self.config.reconnect:
                LOG.warning("AutoDL %s，已禁用重连。")
                self.stop()
                break
            LOG.warning("AutoDL %s；%.1f 秒后重连。")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def _handle_lumine_message(self, data: Any, source_ws: Any = None) -> None:
        """处理一条 AutoDL 服务端消息；忽略失效连接的迟到消息。"""
        if self.config.motor_protocol == "v2" and source_ws is not None and source_ws is not self._lumine_ws:
            return
        try:
            message = json.loads(data)
        except (TypeError, ValueError):
            LOG.debug("忽略无法解析的 AutoDL 消息。")
            return
        if not isinstance(message, dict):
            return

        msg_type = message.get("type")
        if not isinstance(msg_type, str):
            return
        msg_type = msg_type.strip().lower()

        if msg_type == "motor_capability_ack":
            try:
                receipt = json.loads(data, object_pairs_hook=_unambiguous_json_object)
            except (TypeError, ValueError):
                async with self._intent_lock:
                    if source_ws is self._lumine_ws:
                        self._motor_capability = None
                return
            async with self._intent_lock:
                capability = self._motor_capability
                if (self.config.motor_protocol != "v2" or not self._motor_connection_authenticated
                        or source_ws is None or source_ws is not self._lumine_ws or source_ws.closed
                        or capability is None or capability["ws"] is not source_ws
                        or capability["generation"] != self._motor_connection_generation
                        or capability["origin"] != self._motor_connected_origin
                        or capability["origin"] != _motor_origin(self.config.lumine_ws)
                        or capability["endpoint"] != self._motor_connected_endpoint
                        or capability["endpoint"] != _motor_endpoint_identity(self.config.lumine_ws, self.config.lumine_token)
                        or capability["auth"] != self._motor_connected_auth
                        or capability["auth"] != _motor_auth_identity(self.config.lumine_ws, self.config.lumine_token)
                        or receipt.get("nonce") != capability["nonce"]):
                    return
                if (time.monotonic() >= capability["deadline"]
                        or set(receipt) != {"type", "nonce", "connectionGeneration", "protocol",
                                            "intentRequired", "intentForcesMotor", "mock"}
                        or type(receipt.get("connectionGeneration")) is not int
                        or receipt["connectionGeneration"] != capability["generation"]
                        or receipt.get("type") != "motor_capability_ack"
                        or receipt.get("protocol") != "lumine-live-v2"
                        or receipt.get("intentRequired") is not True
                        or receipt.get("intentForcesMotor") is not True
                        or receipt.get("mock") is not False):
                    self._motor_capability = None
                    return
                capability["declarations"] = {"protocol": receipt["protocol"], "mock": receipt["mock"],
                                              "intentRequired": receipt["intentRequired"],
                                              "intentForcesMotor": receipt["intentForcesMotor"]}
                capability["confirmed"] = True
            return

        if msg_type == "hello":
            LOG.info(
                "AutoDL hello：model=%s fps=%s",
                message.get("model"),
                message.get("fps"),
            )
            return

        if msg_type == "action":
            self.stats.actions_in += 1
            self.stats.record_latency(message.get("latencyMs"))
            payload = build_actions_message(message)
            if payload is None:
                self.stats.actions_rejected += 1
                LOG.warning("AutoDL 动作块为空或无法解析，保持安全停顿。")
                return
            async with self._intent_lock:
                attribution = motor_attribution(message) if motor_attribution else None
                if self.config.motor_protocol == "v2":
                    if attribution is None or attribution != self._current_attribution():
                        self.stats.actions_rejected += 1
                        LOG.warning("拒绝缺失或过期归属的 AutoDL 动作")
                        return
                    binding = self._lease_binding
                    if (binding is None or binding["status"] != "bound"
                            or time.monotonic() >= binding["lease_deadline"]
                            or self._lease_binding_ws is not self._bettergi_ws
                            or binding["motor_ws"] is not self._lumine_ws
                            or binding["motor_generation"] != self._motor_connection_generation
                            or _motor_origin(self.config.lumine_ws) != self._motor_connected_origin
                            or _motor_endpoint_identity(self.config.lumine_ws, self.config.lumine_token) != self._motor_connected_endpoint
                            or _motor_auth_identity(self.config.lumine_ws, self.config.lumine_token) != self._motor_connected_auth
                            or self._verified_bettergi_session != binding["session_id"]):
                        self.stats.actions_rejected += 1
                        await self._clear_intent_locked("lease_binding_unavailable")
                        return
                    mode = str((self.latest_intent or {}).get("motor_mode") or "")
                    if not action_matches_motor_mode(
                        payload, mode, (self.latest_intent or {}).get("dialogue_permissions")
                    ):
                        self.stats.actions_rejected += 1
                        self._record_feedback("blocked", "motor_mode_violation",
                                              intent_id=attribution["intentId"], attribution=attribution)
                        await self._clear_intent_locked("motor_mode_violation")
                        return
                    if mode in {"wait", "dialogue"}:
                        # Dialogue choices are executed only by the host controller.
                        return
                # ??????????????????????????????
                self._action_seq += 1
                payload["seq"] = self._action_seq
                if self.config.motor_protocol == "v2":
                    payload.update(attribution)
                    payload["lease"] = dict(binding["credentials"])
                if self.config.motor_protocol == "v2":
                    if self._first_forwarded_seq is None:
                        self._first_forwarded_seq = self._action_seq
                    self._last_forwarded_seq = self._action_seq
                if await self._send_bettergi(payload):
                    self.stats.actions_out += 1
                    self._remember_held_action(payload, attribution)
                    if self.config.motor_protocol == "v2":
                        self._record_feedback("progress", "action_forwarded", attribution["intentId"],
                                              attribution=attribution, seq=self._action_seq)
                else:
                    self.stats.actions_rejected += 1
                    if self.config.motor_protocol == "v2":
                        # ???????????????????????????????
                        await self._clear_intent_locked("action_forward_failed")
            return

        if msg_type == "cancel_ack":
            try:
                ack = json.loads(data, object_pairs_hook=_unambiguous_json_object)
            except (TypeError, ValueError):
                return
            generation = ack.get("motorGeneration")
            async with self._intent_lock:
                if (self.config.motor_protocol == "v2" and type(generation) is int
                        and generation == self._pending_cancel_generation
                        and source_ws is not None and source_ws is self._lumine_ws and not source_ws.closed):
                    for stop_id, request in self._stop_requests.items():
                        if (request.get("motorGeneration") == generation
                                and request.get("remoteStopSent") is True
                                and self._stop_request_motor_connections.get(stop_id) is source_ws):
                            request["motorCancelAcknowledged"] = True
                            del self._stop_request_motor_connections[stop_id]
                            self._pending_cancel_generation = None
                            break
            return

        if msg_type in {"intent_result", "motor_feedback"}:
            try:
                reported = json.loads(data, object_pairs_hook=_unambiguous_json_object)
            except (TypeError, ValueError):
                return
            attribution = motor_attribution(reported) if motor_attribution else None
            seq = reported.get("seq")
            async with self._intent_lock:
                if (self.config.motor_protocol != "v2" or reported.get("status") != "done"
                        or source_ws is None or source_ws is not self._lumine_ws or source_ws.closed
                        or attribution is None or attribution != self._current_attribution()
                        or type(seq) is not int or self._first_forwarded_seq is None
                        or self._last_forwarded_seq is None
                        or not self._first_forwarded_seq <= seq <= self._last_forwarded_seq):
                    return
                self._record_feedback("done", "motor_reported_done", intent_id=attribution["intentId"],
                                      attribution=attribution, seq=seq)
            return

        if msg_type == "intent_ack":
            attribution = motor_attribution(message) if motor_attribution else None
            async with self._intent_lock:
                if self.config.motor_protocol == "v2":
                    if attribution is not None and attribution == self._current_attribution() and message.get("accepted") is True:
                        self._record_feedback("progress", "intent_ack", attribution["intentId"], attribution=attribution)
            return

        if msg_type == "status":
            LOG.debug(
                "AutoDL 状态：queue=%s inflight=%s dropped=%s lastLatency=%s",
                message.get("queue"),
                message.get("inflight"),
                message.get("dropped"),
                message.get("lastLatencyMs"),
            )
            return

        if msg_type == "error":
            if (message.get("code") in {"motor_capability_unavailable", "bad_motor_capability_query", "unknown_type"}
                    and source_ws is not None and source_ws is self._lumine_ws):
                async with self._intent_lock:
                    self._motor_capability = None
            LOG.warning(
                "AutoDL 错误：%s %s",
                message.get("code"),
                message.get("message"),
            )
            if self.config.motor_protocol == "v2":
                attribution = motor_attribution(message) if motor_attribution else None
                async with self._intent_lock:
                    if attribution is not None and attribution == self._current_attribution():
                        self._record_feedback(
                            "failed", str(message.get("code") or "motor_error"),
                            intent_id=attribution["intentId"],
                            observation=str(message.get("message") or "")[:400],
                            attribution=attribution,
                        )
                        await self._clear_intent_locked("motor_error")
            # v1 errors have no generation and cannot be assigned safely.
            return

        if msg_type == "pong":
            return

        LOG.debug("忽略无法解析的 AutoDL 消息。", msg_type)

    # ------------------------------------------------------------------
    # ?????????
    # ------------------------------------------------------------------

    async def _send_lumine(self, payload: Dict[str, Any]) -> bool:
        """向 AutoDL 发送一条 JSON 消息。"""
        ws = self._lumine_ws
        if ws is None or ws.closed:
            return False
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        try:
            async with self._lumine_send_lock:
                if ws.closed:
                    return False
                await asyncio.wait_for(ws.send_str(text), timeout=SEND_TIMEOUT)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("忽略无法解析的 AutoDL 消息。", _safe_error(exc, self.config.bettergi_token,
                                                              self.config.lumine_token, self.config.intent_token))
            return False

    async def _send_bettergi(
        self, payload: Dict[str, Any], expected_ws: Any = None, require_expected_ws: bool = False
    ) -> bool:
        """向 BetterGI 发送 JSON；关联停止请求时只使用指定的连接。"""
        ws = expected_ws if require_expected_ws else self._bettergi_ws
        if ws is None or ws.closed:
            return False
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        try:
            async with self._bettergi_send_lock:
                if ws.closed or (require_expected_ws and ws is not self._bettergi_ws):
                    return False
                await asyncio.wait_for(ws.send_str(text), timeout=SEND_TIMEOUT)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            lease = payload.get("lease") if isinstance(payload.get("lease"), dict) else {}
            LOG.warning("忽略无法解析的 BetterGI 消息。", _safe_error(exc, self.config.bettergi_token,
                self.config.lumine_token, self.config.intent_token, lease.get("secret", "")))
            return False

    async def _forward_loop(self) -> None:
        """按目标帧率把最新画面取出发送给 AutoDL。"""
        # ???????????????????????????????
        tick = max(0.005, min(0.05, self.pump.interval / 2.0))
        while self._running:
            await asyncio.sleep(tick)
            ws = self._lumine_ws
            if ws is None or ws.closed:
                # ?????????????????????????????
                continue

            # Poll and send under the same lock as intent changes so a frame
            # removed before reset cannot leak into a replacement intent.
            async with self._intent_lock:
                frame = self.pump.poll(time.monotonic())
                if frame is None:
                    continue
                payload = build_frame_message(frame, self.latest_guidance)
                if payload is None:
                    continue
                if self.config.motor_protocol == "v2" and self.latest_intent is None:
                    continue
                if await self._send_lumine(payload):
                    self.stats.frames_out += 1

    async def _action_hold_loop(self) -> None:
        """意图仍活跃但模型推理空窗时，按 5fps 重发最近动作块以保持按键。"""
        while self._running:
            await asyncio.sleep(ACTION_HOLD_TICK_SECONDS)
            if not self._running:
                break
            try:
                await self._action_hold_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("动作保持循环异常；立即停止重发并撤销意图。")
                async with self._intent_lock:
                    self._release_held_locked()
                    if self.latest_intent is not None:
                        await self._clear_intent_locked("action_hold_failed")

    async def _action_hold_tick(self) -> None:
        """检查一次 200ms 保持窗口；条件满足时重发最近动作块。"""
        async with self._intent_lock:
            now = time.monotonic()
            if self._hold_expires_at is not None and now >= self._hold_expires_at:
                # hold 窗口到期才松键撤销：窗口内到达的 switch 已直接接管。
                await self._clear_intent_locked("hold_expired")
                return
            held = self._held_action
            held_at = self._held_action_at
            if held is None or held_at is None or self.latest_intent is None:
                return
            if now - held_at < ACTION_HOLD_INTERVAL_SECONDS:
                return
            attribution = self._current_attribution() if self.config.motor_protocol == "v2" else None
            if attribution != held["attribution"]:
                # 意图或连接代际已变化，旧动作块不再属于当前意图。
                self._release_held_locked()
                return
            bettergi_ws = self._bettergi_ws
            lumine_ws = self._lumine_ws
            if (bettergi_ws is None or bettergi_ws.closed
                    or lumine_ws is None or lumine_ws.closed):
                # 断开路径会自行撤销意图；这里只停止续按。
                self._release_held_locked()
                return
            payload = {
                "type": "actions",
                "chunks": [dict(chunk) for chunk in held["chunks"]],
                "absolute": held["absolute"],
                # 重发不携带思考，避免宿主叠加层重复显示或朗读。
                "thought": None,
            }
            if self.config.motor_protocol == "v2":
                binding = self._lease_binding
                if (binding is None or binding["status"] != "bound"
                        or now >= binding["lease_deadline"]
                        or self._lease_binding_ws is not bettergi_ws
                        or binding["motor_ws"] is not lumine_ws
                        or binding["motor_generation"] != self._motor_connection_generation
                        or self._verified_bettergi_session != binding["session_id"]
                        or _motor_origin(self.config.lumine_ws) != self._motor_connected_origin
                        or _motor_endpoint_identity(self.config.lumine_ws, self.config.lumine_token) != self._motor_connected_endpoint
                        or _motor_auth_identity(self.config.lumine_ws, self.config.lumine_token) != self._motor_connected_auth):
                    await self._clear_intent_locked("lease_binding_unavailable")
                    return
                payload.update(attribution)
                payload["lease"] = dict(binding["credentials"])
            self._action_seq += 1
            payload["seq"] = self._action_seq
            if await self._send_bettergi(payload):
                self.stats.actions_held += 1
                # 以发送时刻为基准，避免发送耗时把下一次续按推过宿主 250ms 释放线。
                self._held_action_at = now
                if self.config.motor_protocol == "v2":
                    if self._first_forwarded_seq is None:
                        self._first_forwarded_seq = self._action_seq
                    self._last_forwarded_seq = self._action_seq
            else:
                self.stats.actions_rejected += 1
                if self.config.motor_protocol == "v2":
                    await self._clear_intent_locked("action_hold_failed")
                else:
                    self._release_held_locked()

    def _remember_held_action(self, payload: Dict[str, Any], attribution: Optional[Dict[str, Any]]) -> None:
        """缓存最近一次成功写入宿主的动作块，供意图空窗期重发（调用方持有意图锁）。"""
        self._held_action = {
            "chunks": [dict(chunk) for chunk in payload.get("chunks") or []],
            "absolute": payload.get("absolute"),
            # v1 没有归属校验；只缓存 v2 的归属，供空窗期确认意图代际未变。
            "attribution": dict(attribution) if self.config.motor_protocol == "v2" and attribution else None,
        }
        self._held_action_at = time.monotonic()

    def _release_held_locked(self) -> None:
        """停止续按：清空最近动作块缓存（调用方须持有 _intent_lock）。"""
        self._held_action = None
        self._held_action_at = None

    async def _stats_loop(self) -> None:
        """生成可序列化的统计快照。"""
        interval = max(0.5, float(self.config.stats_interval))
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break
            record_drops = (
                self._recorder.queue.dropped if self._recorder is not None else 0
            )
            log_stats(self.stats, self.pump.dropped, record_drops)


# ----------------------------------------------------------------------
# ??????
# ----------------------------------------------------------------------


def _find_config_arg(argv: List[str]) -> Optional[str]:
    """在参数中提前查找 --config 的路径，用于构造解析器默认值。"""
    for index, arg in enumerate(argv):
        if arg == "--config" and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith("--config="):
            return arg.split("=", 1)[1]
    return None


def load_config_file(path: str) -> Dict[str, Any]:
    """读取 JSON 配置文件；内容必须是对象。"""
    with open(path, "r", encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict):
        raise ValueError("配置文件必须是 JSON 对象：{0}".format(path))
    return data


def _config_default(
    config: Dict[str, Any], key: str, fallback: Any
) -> Any:
    """本地立即撤销，再尽力向两端发送停止；发送成功不等于执行成功。"""
    value = config.get(key)
    return fallback if value is None else value


def build_parser(config: Optional[Dict[str, Any]] = None) -> argparse.ArgumentParser:
    """构造命令行解析器；配置文件只提供默认值，CLI 与环境变量优先。"""
    config = config or {}
    parser = argparse.ArgumentParser(
        description="BetterGI Live Hub ↔ AutoDL Lumine Player Server 本地桥接"
    )
    parser.add_argument(
        "--config", default=None, help="JSON 配置文件路径（可选）"
    )
    parser.add_argument(
        "--bettergi-ws",
        default=_config_default(config, "bettergiWs", DEFAULT_BETTERGI_WS),
        help="BetterGI Live Hub WebSocket 地址，默认 %(default)s",
    )
    parser.add_argument(
        "--bettergi-token",
        default=(
            os.environ.get("BETTERGI_AGENT_TOKEN")
            or _config_default(config, "bettergiToken", "")
        ),
        help="BetterGI 令牌，默认读取环境变量 BETTERGI_AGENT_TOKEN",
    )
    parser.add_argument(
        "--lumine-ws",
        default=_config_default(config, "lumineWs", DEFAULT_LUMINE_WS),
        help="AutoDL Player Server WebSocket 地址，默认 %(default)s",
    )
    parser.add_argument(
        "--lumine-token",
        default=(
            os.environ.get("LUMINE_SERVER_TOKEN")
            or _config_default(config, "lumineToken", "")
        ),
        help="AutoDL 令牌，默认读取环境变量 LUMINE_SERVER_TOKEN",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=float(_config_default(config, "fps", DEFAULT_FPS)),
        help="转发给 AutoDL 的帧率，默认 %(default)s",
    )
    parser.add_argument(
        "--record",
        default=_config_default(config, "record", None),
        help="录制 MP4 的输出路径（可选）",
    )
    parser.add_argument(
        "--record-fps",
        type=float,
        default=float(_config_default(config, "recordFps", DEFAULT_RECORD_FPS)),
        help="统计输出间隔秒数，默认 %(default)s",
    )
    parser.add_argument(
        "--reconnect",
        dest="reconnect",
        action="store_true",
        default=bool(_config_default(config, "reconnect", DEFAULT_RECONNECT)),
        help="放入最新帧；已有未取出的帧时丢弃旧帧。",
    )
    parser.add_argument(
        "--no-reconnect",
        dest="reconnect",
        action="store_false",
        help="队列是否已关闭。",
    )
    parser.add_argument(
        "--stats-interval",
        type=float,
        default=float(
            _config_default(
                config, "statsIntervalSeconds", DEFAULT_STATS_INTERVAL
            )
        ),
        help="统计输出间隔秒数，默认 %(default)s",
    )
    parser.add_argument(
        "--motor-protocol",
        choices=("v1", "v2"),
        default=(
            os.environ.get("MOTOR_PROTOCOL")
            or _config_default(config, "motorProtocol", "v1")
        ),
        help="v2 启用意图总线；v1 保持原 guidance 链路",
    )
    parser.add_argument(
        "--intent-port",
        type=int,
        default=int(
            os.environ.get("BRIDGE_INTENT_PORT")
            or _config_default(config, "intentPort", 0)
        ),
        help="v2 意图 HTTP 总线端口；0 表示不启动",
    )
    parser.add_argument(
        "--intent-host",
        default=_config_default(config, "intentHost", "127.0.0.1"),
        help="意图总线监听地址（仅允许 loopback IP）",
    )
    parser.add_argument(
        "--intent-token",
        default=(
            os.environ.get("BRIDGE_INTENT_TOKEN")
            or _config_default(config, "intentToken", "")
        ),
        help="意图总线 Bearer 令牌（启动 v2 意图总线时必填）",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> BridgeConfig:
    """放入最新帧；已有未取出的帧时丢弃旧帧。"""
    return BridgeConfig(
        bettergi_ws=args.bettergi_ws,
        bettergi_token=args.bettergi_token or "",
        lumine_ws=args.lumine_ws,
        lumine_token=args.lumine_token or "",
        fps=float(args.fps),
        record=args.record,
        record_fps=float(args.record_fps),
        reconnect=bool(args.reconnect),
        stats_interval=float(args.stats_interval),
        motor_protocol=str(args.motor_protocol),
        intent_host=str(args.intent_host),
        intent_port=int(args.intent_port),
        intent_token=str(args.intent_token or ""),
    )


def main(argv: Optional[List[str]] = None) -> int:
    """队列是否已关闭。"""
    # Windows ???????????? UTF-8????????????????????
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    arguments = list(sys.argv[1:] if argv is None else argv)
    config: Dict[str, Any] = {}
    config_path = _find_config_arg(arguments)
    if config_path:
        try:
            config = load_config_file(config_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            sys.stderr.write("读取配置文件失败：{0}\\n".format(exc))
            return 2

    parser = build_parser(config)
    args = parser.parse_args(arguments)

    if not math.isfinite(args.fps) or args.fps <= 0:
        parser.error("--fps 必须为正数")
    if not math.isfinite(args.record_fps) or args.record_fps <= 0:
        parser.error("--record-fps 必须为正数")
    if not math.isfinite(args.stats_interval) or args.stats_interval <= 0:
        parser.error("--stats-interval 必须为正数")
    if args.record and not os.path.isdir(
        os.path.dirname(os.path.abspath(args.record))
    ):
        parser.error("把命令行参数转换为桥接配置。".format(args.record))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    bridge = LumineBridge(config_from_args(args))
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        LOG.info("关闭队列并唤醒等待中的写入线程。")
    except (RuntimeError, ValueError) as exc:
        LOG.error("%s", _safe_error(exc, bridge.config.bettergi_token, bridge.config.lumine_token,
                                    bridge.config.intent_token))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
