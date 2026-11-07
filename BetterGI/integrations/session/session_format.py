#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话文件格式：一场实况的单一可读记录。

一场录制就是一个目录::

    sessions/<session_id>/
        session.jsonl    # 唯一权威记录：首行元信息，之后每行一个事件
        gameplay.mp4     # 实况画面（由录制器写入）
        final.mp4        # 后期渲染成片（由渲染器写入，可选）
        audio/           # 后期配音（由渲染器写入，可选）

``session.jsonl`` 的设计目标：

* 一行一个事件，UTF-8 无 BOM，任何人用文本编辑器或 ``jq`` 都能读；
* 所有事件用同一个时间轴 ``t``（毫秒，相对录制起点），配音、字幕、
  VTuber 参数与画面都靠它对齐；
* 画面另存为 MP4，但每个事件都能独立解释，不依赖视频文件。

事件类型（``type``）：

===========  ============================================================
``session``  元信息，仅首行
``thought``  智能体的内部思考，不朗读
``speech``   要朗读的解说；含预估时长与口型包络
``action``   模拟输入（按键/鼠标）
``avatar``   VTuber 参数操作（表情、眨眼、热键等）
``guidance`` 智能体发给操作模型的指导文本
``frame``    关键截图（相对路径）
``note``     人工或工具追加的备注
``video``    画面开始的偏移标记（首个画面到达时写入）
``audio``    后期生成的配音条目（渲染器写回）
``error``    录制期间的错误
``end``      会话结束与统计
===========  ============================================================

口型包络（``speech.avatar.mouth_open``）按 50ms 采样，值域 0..1，对应
VTube Studio 的 ``ParamMouthOpenY``。它由文本与预估时长推导，后期 TTS
生成后用同一时长做时间拉伸，因此声音与口型天然对齐。
"""

from __future__ import annotations

import json
import math
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA_VERSION = 1

EVENT_TYPES = frozenset(
    {
        "session",
        "thought",
        "speech",
        "action",
        "avatar",
        "guidance",
        "frame",
        "note",
        "video",
        "audio",
        "error",
        "end",
    }
)

# VTube Studio 的标准参数 ID；缺省姿态用于渲染器的基线。
DEFAULT_AVATAR_PARAMS: Dict[str, float] = {
    "ParamAngleX": 0.0,
    "ParamAngleY": 0.0,
    "ParamAngleZ": 0.0,
    "ParamEyeLOpen": 1.0,
    "ParamEyeROpen": 1.0,
    "ParamEyeBallX": 0.0,
    "ParamEyeBallY": 0.0,
    "ParamBrowLY": 0.0,
    "ParamBrowRY": 0.0,
    "ParamMouthForm": 0.0,
    "ParamMouthOpenY": 0.0,
    "ParamBodyAngleX": 0.0,
    "ParamBodyAngleY": 0.0,
    "ParamBodyAngleZ": 0.0,
    "ParamBreath": 0.0,
}

# 情绪 → Live2D 表情文件/热键的通用名；具体模型可在 meta.avatar.expressions
# 里覆盖成自己的文件名。
EMOTION_EXPRESSION: Dict[str, str] = {
    "calm": "neutral",
    "neutral": "neutral",
    "excited": "happy",
    "happy": "happy",
    "surprised": "surprised",
    "sad": "sad",
    "angry": "angry",
    "curious": "curious",
    "nervous": "scared",
    "proud": "proud",
    "thinking": "neutral",
    "amused": "happy",
    "worried": "sad",
    "determined": "determined",
    "tired": "tired",
    "confused": "surprised",
}

EMOTION_MOUTH_FORM: Dict[str, float] = {
    "calm": 0.0,
    "neutral": 0.0,
    "excited": 0.6,
    "happy": 0.55,
    "surprised": 0.15,
    "sad": -0.55,
    "angry": -0.4,
    "curious": 0.2,
    "nervous": -0.2,
    "proud": 0.4,
    "thinking": -0.15,
    "worried": -0.3,
    "determined": 0.1,
    "tired": -0.25,
    "confused": 0.1,
    "amused": 0.5,
}

EMOTION_BROW: Dict[str, float] = {
    "surprised": 0.7,
    "angry": -0.8,
    "sad": -0.4,
    "worried": -0.35,
    "thinking": -0.2,
    "excited": 0.3,
    "happy": 0.1,
}

_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")
_ASCII_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
_PAUSE_STRONG = set("。！？；：…")
_PAUSE_WEAK = set("，、,.!?;:")


def new_session_id(now: Optional[float] = None) -> str:
    """生成按时间排序、带随机后缀的会话标识。"""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def estimate_speech_ms(text: str) -> int:
    """按中文播报节奏预估一句话的朗读时长（毫秒）。

    中文约 4.3 字/秒，英文数字约 11 字符/秒，标点各留一次停顿。
    结果夹在 700ms..20s，既避免空文本产生 0 时长，也避免异常长文本
    把后续时间轴拖垮。
    """
    text = text or ""
    cjk = len(_CJK_RE.findall(text))
    ascii_alnum = len(_ASCII_ALNUM_RE.findall(text))
    pause_ms = sum(
        200 if ch in _PAUSE_STRONG else 120 if ch in _PAUSE_WEAK else 0 for ch in text
    )
    ms = cjk * 235 + ascii_alnum * 90 + pause_ms + 220
    return int(max(700, min(20000, ms)))


def mouth_envelope(
    text: str, dur_ms: int, seed: int = 0, step_ms: int = 50
) -> List[float]:
    """生成口型开合包络，值域 0..1，间隔 ``step_ms``。

    以约 4.4 音节/秒的正弦开合为骨架，叠加确定性抖动，避免听感上像
    节拍器；首尾各做淡入淡出，让闭嘴与开口不突兀。``seed`` 相同则结果
    完全可复现，便于测试与二次渲染。
    """
    if dur_ms <= 0:
        return [0.0]
    step_ms = max(10, int(step_ms))
    count = max(2, dur_ms // step_ms + 1)
    rnd = random.Random(seed)
    rate = 4.4
    values: List[float] = []
    for index in range(count):
        t_ms = index * step_ms
        t_s = t_ms / 1000.0
        base = abs(math.sin(math.pi * rate * t_s)) ** 0.7
        jitter = 0.72 + 0.28 * rnd.random()
        fade_in = min(1.0, t_ms / 100.0)
        fade_out = min(1.0, max(0.0, (dur_ms - t_ms) / 140.0))
        fade = min(fade_in, fade_out)
        value = 0.04 + 0.92 * base * jitter * fade
        values.append(round(max(0.0, min(1.0, value)), 3))
    return values


def avatar_for_speech(
    text: str, emotion: str, dur_ms: int, seed: int = 0
) -> Dict[str, Any]:
    """构造一句话朗读期间的 VTuber 操作。"""
    emotion = emotion or "calm"
    expression = EMOTION_EXPRESSION.get(emotion, "neutral")
    mouth_form = EMOTION_MOUTH_FORM.get(emotion, 0.0)
    brow = EMOTION_BROW.get(emotion, 0.0)
    params: Dict[str, float] = {
        "ParamMouthForm": mouth_form,
        "ParamBrowLY": brow,
        "ParamBrowRY": brow,
    }
    # 情绪带动轻微头部/身体摆动，口头表达更有生气。
    if emotion in {"excited", "happy", "amused"}:
        params["ParamAngleZ"] = 3.0
        params["ParamBodyAngleX"] = 2.0
    elif emotion in {"sad", "tired", "worried"}:
        params["ParamAngleZ"] = -2.0
        params["ParamAngleY"] = -3.0
    elif emotion == "thinking":
        params["ParamAngleX"] = 6.0
    return {
        "expression": expression,
        "params": params,
        "mouth_open": {
            "step_ms": 50,
            "values": mouth_envelope(text, dur_ms, seed=seed),
        },
    }


def iso_to_epoch_ms(value: Any) -> Optional[int]:
    """把 ISO-8601 时间转成 Unix 毫秒；无法解析时返回 ``None``。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def epoch_ms_to_iso(ms: int) -> str:
    """Unix 毫秒转 UTC ISO 字符串（毫秒精度）。"""
    return (
        datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _clamp_t(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("t 必须是整数毫秒，不能是布尔值")
    if isinstance(value, float):
        value = int(round(value))
    if not isinstance(value, int):
        raise ValueError(f"t 必须是整数毫秒，收到 {type(value).__name__}")
    if value < 0:
        raise ValueError(f"t 不能为负数：{value}")
    return value


class SessionWriter:
    """逐行追加写入 ``session.jsonl``。

    写入即 flush，录制进程异常退出时已写事件仍可读。
    """

    def __init__(
        self,
        directory: Path | str,
        *,
        session_id: Optional[str] = None,
        t0_unix_ms: Optional[int] = None,
        source: Optional[Dict[str, Any]] = None,
        video: Optional[Dict[str, Any]] = None,
        agent: Optional[Dict[str, Any]] = None,
        avatar: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or new_session_id()
        self.t0_unix_ms = int(
            t0_unix_ms if t0_unix_ms is not None else time.time() * 1000
        )
        self.path = self.directory / "session.jsonl"
        self._file = self.path.open("w", encoding="utf-8", newline="\n")
        self._closed = False
        self.event_count = 0

        meta: Dict[str, Any] = {
            "type": "session",
            "version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "created_at": epoch_ms_to_iso(self.t0_unix_ms),
            "t0_unix_ms": self.t0_unix_ms,
            "source": source or {},
            "video": video
            or {"path": "gameplay.mp4", "fps": 15, "width": 1280, "height": 720},
            "agent": agent or {},
            "avatar": avatar
            or {
                "defaults": dict(DEFAULT_AVATAR_PARAMS),
                "emotion_expressions": dict(EMOTION_EXPRESSION),
            },
        }
        if extra:
            meta.update(extra)
        self._write(meta)

    # -- 写入 -------------------------------------------------------------

    def _write(self, payload: Dict[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("会话文件已关闭")
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self._file.write(line + "\n")
        self._file.flush()

    def emit(self, t_ms: int, event_type: str, **fields: Any) -> Dict[str, Any]:
        """写入一条事件并返回落盘对象。"""
        if event_type not in EVENT_TYPES or event_type == "session":
            raise ValueError(f"未知事件类型：{event_type}")
        if "t" in fields or "type" in fields:
            raise ValueError("通不过 t/type 字段，使用参数传递")
        event: Dict[str, Any] = {"t": _clamp_t(t_ms), "type": event_type}
        event.update(fields)
        self._write(event)
        self.event_count += 1
        return event

    def emit_thought(
        self,
        t_ms: int,
        text: str,
        *,
        frame_seq: Optional[int] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        fields: Dict[str, Any] = {"text": str(text)[:4000]}
        if frame_seq is not None:
            fields["frame"] = int(frame_seq)
        if meta:
            fields["meta"] = meta
        return self.emit(t_ms, "thought", **fields)

    def emit_speech(
        self,
        t_ms: int,
        text: str,
        *,
        kind: str = "reaction",
        emotion: str = "calm",
        frame_seq: Optional[int] = None,
        dur_ms: Optional[int] = None,
        seed: Optional[int] = None,
        meta: Optional[Dict[str, Any]] = None,
        intensity: Optional[float] = None,
        pace: Optional[str] = None,
        volume: Optional[str] = None,
        pause_before_ms: Optional[int] = None,
        interruptible: Optional[bool] = None,
        thought_source: Optional[str] = None,
        source: Optional[str] = None,
        generated_at_ms: Optional[int] = None,
        thought_id: Optional[str] = None,
        intent_id: Optional[str] = None,
        prosody: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # 旧调用保持原有规范化行为；player 事件的文本必须逐字保留。
        if source is not None and source != "persistent_player":
            raise ValueError("speech source 必须是 persistent_player")
        if source == "persistent_player":
            if not isinstance(text, str) or not text.strip():
                raise ValueError("解说文本不能为空")
        else:
            text = str(text).strip()
            if not text:
                raise ValueError("解说文本不能为空")
        if generated_at_ms is not None and (
            isinstance(generated_at_ms, bool)
            or not isinstance(generated_at_ms, int)
            or generated_at_ms <= 0
        ):
            raise ValueError("generated_at_ms 必须是正整数毫秒")
        if source == "persistent_player" and (
            generated_at_ms is None or generated_at_ms - self.t0_unix_ms != t_ms
        ):
            raise ValueError("player speech 时间必须与生成时间和会话起点一致")
        for name, value in (("thought_id", thought_id), ("intent_id", intent_id)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} 必须是非空字符串")
        if source == "persistent_player" and frame_seq is not None and (
            isinstance(frame_seq, bool) or not isinstance(frame_seq, int) or frame_seq < 0
        ):
            raise ValueError("frame_seq 必须是非负整数")
        dur = int(dur_ms if dur_ms is not None else estimate_speech_ms(text))
        seed_value = int(seed if seed is not None else zlib_crc(text, t_ms))
        fields: Dict[str, Any] = {
            "text": text if source == "persistent_player" else text[:2000],
            "kind": kind or "reaction",
            "emotion": emotion or "calm",
            "dur": dur,
            "avatar": avatar_for_speech(text, emotion or "calm", dur, seed=seed_value),
        }
        if frame_seq is not None:
            fields["frame"] = int(frame_seq)
            if source == "persistent_player":
                fields["frame_seq"] = frame_seq
        for name, value in (
            ("source", source), ("generated_at_ms", generated_at_ms),
            ("thought_id", thought_id), ("intent_id", intent_id),
        ):
            if value is not None:
                fields[name] = value
        if prosody is not None:
            if source != "persistent_player" or not isinstance(prosody, dict):
                raise ValueError("prosody 必须是 player 的对象")
            fields["prosody"] = prosody
        # 可选 prosody：渲染器只按这些提示调整语速/音量/停顿，不改文本含义。
        if intensity is not None:
            try:
                fields["intensity"] = min(1.0, max(0.0, float(intensity)))
            except (TypeError, ValueError):
                pass
        if pace in ("slow", "normal", "fast"):
            fields["pace"] = pace
        if volume in ("soft", "normal", "loud"):
            fields["volume"] = volume
        if pause_before_ms is not None:
            fields["pause_before_ms"] = min(2000, max(0, int(pause_before_ms)))
        if interruptible is not None:
            fields["interruptible"] = bool(interruptible)
        if thought_source:
            fields["thought_source"] = (
                thought_source if source == "persistent_player" else str(thought_source)[:200]
            )
        if meta:
            fields["meta"] = meta
        return self.emit(t_ms, "speech", **fields)

    def emit_action(
        self,
        t_ms: int,
        *,
        keys: Optional[Iterable[str]] = None,
        mouse: Optional[Dict[str, int]] = None,
        scroll: int = 0,
        thought: Optional[str] = None,
    ) -> Dict[str, Any]:
        fields: Dict[str, Any] = {"keys": list(keys or [])}
        if mouse:
            fields["mouse"] = {
                "dx": int(mouse.get("dx", 0)),
                "dy": int(mouse.get("dy", 0)),
            }
        if scroll:
            fields["scroll"] = int(scroll)
        if thought:
            fields["thought"] = str(thought)[:2000]
        return self.emit(t_ms, "action", **fields)

    def emit_avatar(self, t_ms: int, op: str, **fields: Any) -> Dict[str, Any]:
        return self.emit(t_ms, "avatar", op=op, **fields)

    def emit_note(self, t_ms: int, text: str) -> Dict[str, Any]:
        return self.emit(t_ms, "note", text=str(text)[:4000])

    def emit_error(self, t_ms: int, code: str, message: str) -> Dict[str, Any]:
        return self.emit(t_ms, "error", code=code, message=str(message)[:2000])

    def emit_video_start(
        self, t_ms: int, path: str, *, width: int, height: int, fps: float
    ) -> Dict[str, Any]:
        return self.emit(
            t_ms,
            "video",
            path=path,
            width=int(width),
            height=int(height),
            fps=float(fps),
        )

    def close(
        self, duration_ms: Optional[int] = None, counts: Optional[Dict[str, Any]] = None
    ) -> None:
        if self._closed:
            return
        fields: Dict[str, Any] = {"events": self.event_count}
        if duration_ms is not None:
            fields["duration_ms"] = int(duration_ms)
        if counts:
            fields["counts"] = counts
        self._write({"t": _clamp_t(duration_ms or 0), "type": "end", **fields})
        self._closed = True
        self._file.close()

    def __enter__(self) -> "SessionWriter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def zlib_crc(text: str, salt: int = 0) -> int:
    """稳定的文本哈希，用作口型随机种子。"""
    import zlib

    return zlib.crc32(text.encode("utf-8"), salt & 0xFFFFFFFF) & 0xFFFFFFFF


@dataclass
class Session:
    """读取后的会话。"""

    directory: Path
    meta: Dict[str, Any]
    events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return self.directory / "session.jsonl"

    @property
    def session_id(self) -> str:
        return str(self.meta.get("session_id", self.directory.name))

    @property
    def duration_ms(self) -> int:
        for event in reversed(self.events):
            if event.get("type") == "end" and isinstance(event.get("duration_ms"), int):
                return int(event["duration_ms"])
        return max((int(event.get("t", 0)) for event in self.events), default=0)

    @property
    def video_path(self) -> Optional[Path]:
        video = self.meta.get("video") or {}
        name = video.get("path") if isinstance(video, dict) else None
        if not name:
            return None
        path = self.directory / str(name)
        return path if path.is_file() else None

    def of_type(self, *types: str) -> List[Dict[str, Any]]:
        wanted = set(types)
        return [event for event in self.events if event.get("type") in wanted]

    @property
    def speeches(self) -> List[Dict[str, Any]]:
        return self.of_type("speech")

    @property
    def thoughts(self) -> List[Dict[str, Any]]:
        return self.of_type("thought")

    def validate(self) -> None:
        """对已读事件做轻量一致性检查，发现问题直接抛 ``ValueError``。

        事件在文件中的先后只反映写入时机；对齐以 ``t`` 为准，因此不要求
        严格递增（不同来源的时钟抖动可能带来几十毫秒的回退）。
        """
        seen_end = False
        for index, event in enumerate(self.events):
            if "t" not in event or not isinstance(event["t"], int) or event["t"] < 0:
                raise ValueError(f"第 {index + 2} 行缺少合法的 t：{event!r}")
            if event.get("type") not in EVENT_TYPES:
                raise ValueError(
                    f"第 {index + 2} 行事件类型未知：{event.get('type')!r}"
                )
            if event.get("type") == "speech" and not str(event.get("text", "")).strip():
                raise ValueError(f"第 {index + 2} 行解说是空文本")
            if event.get("type") == "end":
                seen_end = True
        if not seen_end:
            raise ValueError("会话缺少 end 事件（录像可能异常中断）")


def read_session(path: Path | str) -> Session:
    """读取会话目录或 ``session.jsonl``。"""
    target = Path(path)
    if target.is_dir():
        directory = target
        file_path = target / "session.jsonl"
    else:
        file_path = target
        directory = target.parent
    if not file_path.is_file():
        raise FileNotFoundError(f"找不到会话文件：{file_path}")

    meta: Optional[Dict[str, Any]] = None
    events: List[Dict[str, Any]] = []
    with file_path.open("r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            line = raw.strip().lstrip("\N{ZERO WIDTH NO-BREAK SPACE}")
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{file_path}:{line_no} 不是合法 JSON：{exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"{file_path}:{line_no} 不是 JSON 对象")
            if payload.get("type") == "session":
                if meta is not None:
                    raise ValueError(f"{file_path}:{line_no} 重复的 session 元信息")
                meta = payload
                version = payload.get("version")
                if not isinstance(version, int) or version > SCHEMA_VERSION:
                    raise ValueError(
                        f"会话版本 {version!r} 高于当前支持的 {SCHEMA_VERSION}"
                    )
                continue
            if meta is None:
                raise ValueError(f"{file_path}:{line_no} 在元信息之前出现事件")
            events.append(payload)

    if meta is None:
        raise ValueError(f"{file_path} 缺少 session 元信息")
    return Session(directory=directory, meta=meta, events=events)
