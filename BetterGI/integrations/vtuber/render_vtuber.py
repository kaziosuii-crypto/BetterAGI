#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BetterGI 解说时间线离线渲染器。

从 BetterGI 的 ``/api/v1/agent/live/timeline`` 事件（或任意符合格式的 JSON 数组）
生成 ASS 字幕，并使用 ffmpeg 把字幕（可选头像）叠加到录屏视频上。

示例::

    python render_vtuber.py --video input.mp4 --timeline timeline.json --out out.mp4
    python render_vtuber.py --video input.mp4 --timeline timeline.json --out out.mp4 \
        --avatar avatar.png --start-ts 2026-10-26T14:00:00Z --tts

依赖：Python 3.9+ 标准库、ffmpeg。``ffprobe`` 与 ``edge-tts`` 均为可选项。
本脚本不读取游戏内存、不抓包，只处理用户提供的时间线与视频文件。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 配色与文案
# ---------------------------------------------------------------------------

# 解说类型对应的主色（与叠加层默认配色保持一致）。
KIND_COLORS: Dict[str, str] = {
    "reaction": "#ff6b9d",
    "plan": "#4fc3f7",
    "emotion": "#ffd166",
    "lore": "#b388ff",
    "chat": "#7cf5c0",
    "result": "#ffa94d",
    "guidance": "#9bd7ff",
}

# 情绪对应的描边色，让不同情绪在画面中更容易区分。
EMOTION_COLORS: Dict[str, str] = {
    "calm": "#7ad7f0",
    "excited": "#ffb703",
    "surprised": "#ff6b6b",
    "happy": "#ffd166",
    "sad": "#7aa2ff",
    "angry": "#ff5c5c",
    "curious": "#b388ff",
    "nervous": "#ff9f68",
    "proud": "#ffd166",
    "thinking": "#9aa7b8",
    "amused": "#7cf5c0",
    "worried": "#ff9f68",
    "determined": "#ff8fa3",
    "tired": "#8f9bb3",
    "neutral": "#7ad7f0",
}

# 字幕前缀使用的情绪中文名。
EMOTION_LABELS: Dict[str, str] = {
    "calm": "平静",
    "excited": "兴奋",
    "surprised": "惊讶",
    "happy": "开心",
    "sad": "难过",
    "angry": "生气",
    "curious": "好奇",
    "nervous": "紧张",
    "proud": "自豪",
    "thinking": "思考",
    "amused": "有趣",
    "worried": "担忧",
    "determined": "坚定",
    "tired": "疲惫",
    "neutral": "平静",
}

# 每条解说的显示时长：基础 2.5 秒 + 每字 0.12 秒，限制在 2～8 秒。
BASE_DURATION_SECONDS = 2.5
PER_CHAR_SECONDS = 0.12
MIN_DURATION_SECONDS = 2.0
MAX_DURATION_SECONDS = 8.0

# 字幕最短保留时长；被视频结尾裁掉后不足该值时直接丢弃。
MIN_VISIBLE_SECONDS = 0.5

# TTS 最多合成的条数，避免超长时间线生成过多临时文件。
MAX_TTS_LINES = 80

ASS_STYLE_FORMAT = (
    "Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
    "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, "
    "Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
)
ASS_EVENT_FORMAT = "Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimelineEvent:
    """归一化后的时间线事件。"""

    index: int
    seq: Optional[int]
    timestamp: Optional[datetime]
    event_type: str
    kind: str
    text: str
    emotion: str


@dataclass(frozen=True)
class Cue:
    """一帧字幕的排期结果。"""

    start: float
    end: float
    kind: str
    emotion: str
    text: str


@dataclass(frozen=True)
class MediaInfo:
    """ffprobe 探测到的媒体信息。"""

    duration: Optional[float]
    has_audio: bool
    width: Optional[int]
    height: Optional[int]


# ---------------------------------------------------------------------------
# 时间线解析
# ---------------------------------------------------------------------------


def _lookup(raw: Dict[str, Any], *names: str) -> Any:
    """按大小写/下划线不敏感的方式读取字段，兼容 ts 与 timestampUtc。"""

    normalized: Dict[str, Any] = {}
    for key, value in raw.items():
        normalized[str(key).lower().replace("_", "")] = value
    for name in names:
        key = name.lower().replace("_", "")
        if key in normalized and normalized[key] is not None:
            return normalized[key]
    return None


def parse_timestamp(value: Any) -> Optional[datetime]:
    """解析 ISO-8601、数字时间戳或常见日期格式，统一返回 UTC 时间。"""

    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        seconds = float(value)
        # 兼容毫秒时间戳。
        if abs(seconds) > 1e12:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    # 纯数字字符串按 Unix 时间戳处理。
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        try:
            return parse_timestamp(float(text))
        except ValueError:
            return None

    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    parsed: Optional[datetime] = None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in (
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_event(raw: Dict[str, Any], index: int) -> TimelineEvent:
    """把一条原始事件转换为内部结构，兼容 ws / timeline 两种字段命名。"""

    event_type = str(_lookup(raw, "type", "eventType") or "").strip().lower()
    kind = str(_lookup(raw, "kind", "commentKind") or "").strip().lower()

    # 某些服务端消息直接使用 type=reaction 之类的解说类型。
    if event_type in KIND_COLORS and event_type != "guidance":
        if not kind:
            kind = event_type
        event_type = "commentary"
    if event_type == "commentary" and not kind:
        kind = "reaction"

    text_value = _lookup(raw, "text", "content", "message")
    text = "" if text_value is None else str(text_value)

    emotion_value = _lookup(raw, "emotion", "mood")
    emotion = "" if emotion_value is None else str(emotion_value).strip().lower()

    timestamp = parse_timestamp(
        _lookup(raw, "ts", "timestampUtc", "timestamp", "time", "createdAt")
    )

    seq_value = _lookup(raw, "seq", "sequence")
    try:
        seq = int(seq_value) if seq_value is not None else None
    except (TypeError, ValueError):
        seq = None

    return TimelineEvent(
        index=index,
        seq=seq,
        timestamp=timestamp,
        event_type=event_type,
        kind=kind,
        text=text,
        emotion=emotion,
    )


def parse_timeline_payload(payload: Any) -> List[TimelineEvent]:
    """解析时间线 JSON：既支持数组，也支持包含 events 的对象。"""

    if isinstance(payload, dict):
        for key in ("events", "timeline", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                payload = value
                break
        else:
            raise ValueError("时间线 JSON 对象中缺少 events 数组")

    if not isinstance(payload, list):
        raise ValueError("时间线 JSON 必须是数组，或包含 events 数组的对象")

    return [
        normalize_event(item, index)
        for index, item in enumerate(payload)
        if isinstance(item, dict)
    ]


def load_timeline(path: str) -> List[TimelineEvent]:
    """从文件读取时间线。"""

    with open(path, "r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    return parse_timeline_payload(payload)


def resolve_start(events: Sequence[TimelineEvent], start_ts: Optional[str] = None) -> Optional[datetime]:
    """确定时间轴零点：命令行 > 最早的 live.started > 最早的事件时间。"""

    if start_ts:
        parsed = parse_timestamp(start_ts)
        if parsed is None:
            raise ValueError("无法解析 --start-ts：" + start_ts)
        return parsed

    started = [e.timestamp for e in events if e.event_type == "live.started" and e.timestamp]
    if started:
        return min(started)  # type: ignore[type-var]

    timestamps = [e.timestamp for e in events if e.timestamp]
    if timestamps:
        return min(timestamps)  # type: ignore[type-var]
    return None


def select_render_events(
    events: Sequence[TimelineEvent], include_guidance: bool = False
) -> List[TimelineEvent]:
    """筛选需要渲染的事件，并按时间与序号稳定排序。"""

    selected: List[TimelineEvent] = []
    for event in events:
        if event.event_type == "commentary" and event.text.strip():
            selected.append(event)
        elif include_guidance and event.event_type == "guidance" and event.text.strip():
            selected.append(event)

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    selected.sort(
        key=lambda e: (
            e.timestamp or epoch,
            e.seq if e.seq is not None else e.index,
            e.index,
        )
    )
    return selected


# ---------------------------------------------------------------------------
# 排期
# ---------------------------------------------------------------------------


def display_duration_seconds(text: str) -> float:
    """按文本长度计算显示时长。"""

    length = len(text.strip())
    return max(MIN_DURATION_SECONDS, min(MAX_DURATION_SECONDS, BASE_DURATION_SECONDS + PER_CHAR_SECONDS * length))


def build_cues(
    events: Sequence[TimelineEvent],
    start: Optional[datetime],
    video_duration: Optional[float] = None,
    include_guidance: bool = False,
) -> List[Cue]:
    """把事件转换为无重叠的字幕排期。

    - 相对时间 = 事件时间 - 时间轴零点，早于零点时钳制为 0。
    - 事件晚于视频结尾时丢弃。
    - 显示时长按文本长度计算；与前一条重叠时顺延到前一条结束。
    """

    cues: List[Cue] = []
    cursor = 0.0

    for event in select_render_events(events, include_guidance):
        if start is not None and event.timestamp is not None:
            relative = max(0.0, (event.timestamp - start).total_seconds())
        else:
            relative = 0.0

        start_at = max(relative, cursor)
        if video_duration is not None and start_at >= video_duration:
            continue

        end_at = start_at + display_duration_seconds(event.text)
        if video_duration is not None:
            end_at = min(end_at, video_duration)
            if end_at - start_at < MIN_VISIBLE_SECONDS:
                continue

        kind = "guidance" if event.event_type == "guidance" else (event.kind or "reaction")
        cues.append(
            Cue(
                start=round(start_at, 3),
                end=round(end_at, 3),
                kind=kind,
                emotion=event.emotion,
                text=event.text,
            )
        )
        cursor = end_at

    return cues


# ---------------------------------------------------------------------------
# ASS 生成
# ---------------------------------------------------------------------------


def ass_color(hex_color: str, alpha: int = 0) -> str:
    """把 #RRGGBB 转换为 ASS 的 &HAABBGGRR。"""

    value = str(hex_color or "").lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        value = "ffffff"
    red = int(value[0:2], 16)
    green = int(value[2:4], 16)
    blue = int(value[4:6], 16)
    return "&H{:02X}{:02X}{:02X}{:02X}".format(max(0, min(255, alpha)), blue, green, red)


def escape_ass_text(text: str) -> str:
    """转义 ASS 特殊字符，并把换行转换为 \\N。"""

    value = str(text).replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\\", "\\\\")
    value = value.replace("{", "\\{").replace("}", "\\}")
    value = value.replace("\n", "\\N")
    value = value.replace("\t", " ")
    return value


def format_ass_time(seconds: float) -> str:
    """把秒转换为 ASS 的 H:MM:SS.cc。"""

    total_centiseconds = int(round(max(0.0, float(seconds)) * 100))
    hours, remainder = divmod(total_centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, centiseconds = divmod(remainder, 100)
    return "{0}:{1:02d}:{2:02d}.{3:02d}".format(hours, minutes, secs, centiseconds)


def _style_name(kind: str, emotion: str) -> str:
    """生成合法且稳定的 ASS 样式名。"""

    raw = "{}_{}".format(kind or "reaction", emotion or "none")
    safe = re.sub(r"[^0-9A-Za-z_]+", "_", raw).strip("_")
    return "C_" + (safe or "cue")


def build_ass(
    cues: Sequence[Cue],
    font: str = "Microsoft YaHei",
    title: str = "BetterGI VTuber",
) -> str:
    """生成完整的 ASS 字幕文档，按 kind / emotion 生成独立样式。"""

    font_name = str(font or "Microsoft YaHei").replace(",", " ").replace("\n", " ").strip()
    safe_title = str(title or "BetterGI VTuber").replace("\n", " ").strip()

    style_lines: List[str] = [
        "Style: Default,{0},34,{1},{1},{2},{3},0,0,0,0,100,100,0,0,1,3,1.4,2,150,150,44,1".format(
            font_name,
            ass_color("#FFFFFF"),
            ass_color("#10131A"),
            ass_color("#000000", 128),
        )
    ]
    style_map: Dict[Tuple[str, str], str] = {}
    dialogue_lines: List[str] = []

    for cue in cues:
        key = (cue.kind, cue.emotion or "")
        if key not in style_map:
            style_map[key] = _style_name(cue.kind, cue.emotion)

            if cue.kind == "guidance":
                primary = ass_color("#EEF5FF")
                outline = ass_color("#0B1220")
                font_size = 26
                margin_v = 42
            else:
                primary = ass_color(KIND_COLORS.get(cue.kind, "#E8F1FF"))
                emotion_color = EMOTION_COLORS.get(cue.emotion or "", "#10131A")
                outline = ass_color(emotion_color)
                font_size = 34
                margin_v = 46

            style_lines.append(
                "Style: {name},{font},{size},{primary},{secondary},{outline},{back},"
                "0,0,0,0,100,100,0,0,1,3.2,1.6,2,150,150,{margin},1".format(
                    name=style_map[key],
                    font=font_name,
                    size=font_size,
                    primary=primary,
                    secondary=primary,
                    outline=outline,
                    back=ass_color("#000000", 128),
                    margin=margin_v,
                )
            )

        style_name = style_map[key]
        prefix = ""
        if cue.kind == "guidance":
            prefix = "【指导】"
        elif cue.emotion:
            prefix = "【{}】".format(EMOTION_LABELS.get(cue.emotion, cue.emotion))

        primary_color = (
            ass_color("#EEF5FF")
            if cue.kind == "guidance"
            else ass_color(KIND_COLORS.get(cue.kind, "#E8F1FF"))
        )
        prefix_color = (
            ass_color(KIND_COLORS["guidance"])
            if cue.kind == "guidance"
            else ass_color(EMOTION_COLORS.get(cue.emotion or "", "#10131A"))
        )

        if prefix:
            text = (
                "{\\fad(160,220)\\c" + prefix_color + "\\b1}"
                + escape_ass_text(prefix)
                + "{\\c" + primary_color + "\\b0}"
                + escape_ass_text(cue.text)
            )
        else:
            text = "{\\fad(160,220)}" + escape_ass_text(cue.text)

        dialogue_lines.append(
            "Dialogue: 0,{start},{end},{style},,0,0,0,,{text}".format(
                start=format_ass_time(cue.start),
                end=format_ass_time(cue.end),
                style=style_name,
                text=text,
            )
        )

    lines = [
        "[Script Info]",
        "; Generated by BetterGI VTuber renderer",
        "Title: " + safe_title,
        "ScriptType: v4.00+",
        "PlayResX: 1280",
        "PlayResY: 720",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: " + ASS_STYLE_FORMAT,
        *style_lines,
        "",
        "[Events]",
        "Format: " + ASS_EVENT_FORMAT,
        *dialogue_lines,
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ffmpeg 命令构建
# ---------------------------------------------------------------------------


def escape_filter_path(path: str) -> str:
    """转义 ffmpeg 滤镜参数中的文件路径（Windows 盘符、引号等）。"""

    value = str(path).replace("\\", "/")
    value = value.replace("'", "\\'")
    value = value.replace(":", "\\:")
    value = value.replace("[", "\\[").replace("]", "\\]")
    value = value.replace(",", "\\,").replace(";", "\\;")
    return value


def build_ffmpeg_command(
    ffmpeg: str,
    video: str,
    ass_path: str,
    output: str,
    avatar: Optional[str] = None,
    tts_files: Sequence[Tuple[float, str]] = (),
    input_has_audio: Optional[bool] = None,
    duration: Optional[float] = None,
    avatar_width: int = 280,
    copy_audio: bool = True,
) -> List[str]:
    """构建 ffmpeg 命令；抽成纯函数便于单元测试，不在此处执行。"""

    subtitle_filter = "subtitles=filename='{}'".format(escape_filter_path(ass_path))
    command: List[str] = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", video]

    uses_filter_complex = bool(avatar) or bool(tts_files)
    chains: List[str] = []
    tts_indexes: List[int] = []

    if uses_filter_complex:
        next_index = 1
        avatar_index: Optional[int] = None
        if avatar:
            avatar_index = next_index
            next_index += 1
            # 以 image2 循环单张图片，让淡入与浮动在整个视频期间持续生效。
            command.extend(["-f", "image2", "-loop", "1", "-i", avatar])

        for _start, tts_path in tts_files:
            tts_indexes.append(next_index)
            next_index += 1
            command.extend(["-i", tts_path])

        if avatar and avatar_index is not None:
            width = max(64, int(avatar_width))
            chains.append(
                "[{idx}:v]scale={width}:-1,format=rgba,"
                "fade=t=in:st=0:d=0.6:alpha=1[avatar]".format(idx=avatar_index, width=width)
            )
            # 用 sin 表达式让头像轻轻浮动；overlay 以主视频时间轴求值。
            chains.append(
                "[0:v][avatar]overlay=x=W-w-28:y=H-h-32+8*sin(2*PI*t/3):"
                "eval=frame:eof_action=repeat:shortest=1[with_avatar]"
            )
            chains.append("[with_avatar]{0}[vout]".format(subtitle_filter))
        else:
            chains.append("[0:v]{0}[vout]".format(subtitle_filter))

        audio_sources: List[str] = []
        if input_has_audio and tts_files:
            # 统一采样率与声道布局，避免游戏多声道音轨与单声道 TTS 混音失败。
            chains.append(
                "[0:a]aresample=48000,"
                "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[abase]"
            )
            audio_sources.append("[abase]")

        for order, (start, _tts_path) in enumerate(tts_files):
            delay_ms = max(0, int(round(float(start) * 1000)))
            chains.append(
                "[{idx}:a]aresample=48000,"
                "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                "adelay={delay}|{delay},volume=1.0[atts{order}]".format(
                    idx=tts_indexes[order], delay=delay_ms, order=order
                )
            )
            audio_sources.append("[atts{}]".format(order))

        audio_label: Optional[str] = None
        if len(audio_sources) == 1:
            chains.append("{0}anull[aout]".format(audio_sources[0]))
            audio_label = "[aout]"
        elif len(audio_sources) > 1:
            chains.append(
                "{0}amix=inputs={1}:duration=longest:dropout_transition=0:"
                "normalize=0,alimiter=limit=0.95[aout]".format(
                    "".join(audio_sources), len(audio_sources)
                )
            )
            audio_label = "[aout]"

        command.extend(["-filter_complex", ";".join(chains), "-map", "[vout]"])
        if audio_label:
            command.extend(["-map", audio_label, "-c:a", "aac", "-b:a", "192k"])
        elif input_has_audio is not False:
            command.extend(["-map", "0:a?", "-c:a", "copy" if copy_audio else "aac"])
    else:
        command.extend(["-vf", subtitle_filter, "-map", "0:v:0"])
        if input_has_audio is not False:
            command.extend(["-map", "0:a?", "-c:a", "copy" if copy_audio else "aac"])

    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ]
    )
    if duration is not None and duration > 0:
        command.extend(["-t", "{:.3f}".format(float(duration))])
    command.append(output)
    return command


# ---------------------------------------------------------------------------
# 媒体探测与 TTS
# ---------------------------------------------------------------------------


def probe_media(path: str, ffprobe: Optional[str] = None) -> Optional[MediaInfo]:
    """使用 ffprobe 探测时长与音轨；ffprobe 不存在时返回 None。"""

    executable = ffprobe or shutil.which("ffprobe")
    if not executable:
        return None

    command = [
        executable,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout.decode("utf-8", "replace"))
    except ValueError:
        return None

    streams = data.get("streams") or []
    duration: Optional[float] = None
    format_info = data.get("format") or {}
    for candidate in (format_info.get("duration"),):
        if candidate is None:
            continue
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue
    if duration is not None and duration <= 0:
        duration = None

    width: Optional[int] = None
    height: Optional[int] = None
    for stream in streams:
        if stream.get("codec_type") != "video":
            continue
        try:
            width = int(stream.get("width"))
            height = int(stream.get("height"))
        except (TypeError, ValueError):
            width = None
            height = None
        if duration is None:
            try:
                duration = float(stream.get("duration"))
            except (TypeError, ValueError):
                duration = None
        break

    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    return MediaInfo(duration=duration, has_audio=has_audio, width=width, height=height)


def synthesize_tts(
    cues: Sequence[Cue], voice: str, output_dir: str
) -> List[Tuple[float, str]]:
    """使用 edge-tts 合成解说音频；模块缺失或失败时记录并跳过。"""

    try:
        import edge_tts  # type: ignore
    except Exception:
        print("[提示] 未安装 edge-tts，已跳过 TTS。可执行 pip install edge-tts 后重试。", file=sys.stderr)
        return []

    items: List[Tuple[float, str, str]] = []
    for index, cue in enumerate(cues):
        text = cue.text.strip()
        if not text:
            continue
        if len(items) >= MAX_TTS_LINES:
            print("[提示] TTS 条数超过 {}，其余解说将被跳过。".format(MAX_TTS_LINES), file=sys.stderr)
            break
        path = str(Path(output_dir) / "tts_{:04d}.mp3".format(index))
        items.append((cue.start, path, text))

    if not items:
        return []

    async def synthesize_all() -> List[Optional[Tuple[float, str]]]:
        semaphore = asyncio.Semaphore(3)

        async def synthesize_one(start: float, path: str, text: str) -> Optional[Tuple[float, str]]:
            async with semaphore:
                try:
                    await edge_tts.Communicate(text, voice).save(path)
                    return (start, path)
                except Exception as exc:  # 网络/音色等问题不应中断整体渲染
                    print(
                        "[提示] TTS 合成失败（{:.2f}s）：{}".format(start, exc),
                        file=sys.stderr,
                    )
                    return None

        results = await asyncio.gather(
            *(synthesize_one(start, path, text) for start, path, text in items)
        )
        return list(results)

    try:
        results = asyncio.run(synthesize_all())
    except Exception as exc:
        print("[提示] TTS 初始化失败，已跳过：{}".format(exc), file=sys.stderr)
        return []

    return [item for item in results if item is not None]


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def run_ffmpeg(command: Sequence[str]) -> int:
    """运行 ffmpeg，返回退出码。"""

    try:
        return subprocess.run(list(command), check=False).returncode
    except OSError as exc:
        print("[错误] 无法启动 ffmpeg：{}".format(exc), file=sys.stderr)
        return 127


def build_arg_parser() -> argparse.ArgumentParser:
    """构建命令行参数。"""

    parser = argparse.ArgumentParser(
        description="把 BetterGI 解说时间线渲染为带字幕（可选头像 / TTS）的视频。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--video", required=True, help="输入视频（游戏录屏）")
    parser.add_argument("--timeline", required=True, help="时间线 JSON 文件")
    parser.add_argument("--out", required=True, help="输出 MP4 文件")
    parser.add_argument("--avatar", help="可选的头像 PNG（透明背景效果最佳）")
    parser.add_argument("--start-ts", help="时间轴零点（ISO 8601）；默认取最早的 live.started")
    parser.add_argument("--font", default="Microsoft YaHei", help="字幕字体名称")
    parser.add_argument("--tts", action="store_true", help="使用 edge-tts 合成并混入解说语音")
    parser.add_argument("--tts-voice", default="zh-CN-XiaoxiaoNeural", help="edge-tts 音色")
    parser.add_argument("--include-guidance", action="store_true", help="同时渲染 guidance 事件")
    parser.add_argument("--avatar-width", type=int, default=280, help="头像叠加宽度（像素）")
    parser.add_argument("--ffmpeg", help="ffmpeg 可执行文件路径；默认从 PATH 查找")
    parser.add_argument("--ffprobe", help="ffprobe 可执行文件路径；默认从 PATH 查找")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口。"""

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    video_path = Path(args.video)
    timeline_path = Path(args.timeline)
    output_path = Path(args.out)

    if not video_path.is_file():
        print("[错误] 找不到视频文件：{}".format(video_path), file=sys.stderr)
        return 2
    if not timeline_path.is_file():
        print("[错误] 找不到时间线文件：{}".format(timeline_path), file=sys.stderr)
        return 2
    if args.avatar and not Path(args.avatar).is_file():
        print("[错误] 找不到头像文件：{}".format(args.avatar), file=sys.stderr)
        return 2

    ffmpeg = args.ffmpeg or shutil.which("ffmpeg")
    if not ffmpeg:
        print("[错误] 未找到 ffmpeg，请安装后将其加入 PATH，或使用 --ffmpeg 指定。", file=sys.stderr)
        return 2

    try:
        events = load_timeline(str(timeline_path))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("[错误] 读取时间线失败：{}".format(exc), file=sys.stderr)
        return 2

    try:
        start = resolve_start(events, args.start_ts)
    except ValueError as exc:
        print("[错误] {}".format(exc), file=sys.stderr)
        return 2

    media = probe_media(str(video_path), args.ffprobe)
    video_duration = media.duration if media else None
    if media:
        print(
            "[信息] 视频时长 {:.2f}s，音轨：{}。".format(
                video_duration or 0.0, "有" if media.has_audio else "无"
            )
        )
    else:
        print("[提示] 未找到 ffprobe，跳过视频时长与音轨探测。", file=sys.stderr)

    cues = build_cues(events, start, video_duration, args.include_guidance)
    print(
        "[信息] 时间线事件 {} 条，待渲染字幕 {} 条，时间轴零点：{}。".format(
            len(events),
            len(cues),
            start.isoformat() if start else "未指定",
        )
    )
    if not cues:
        print("[提示] 没有可渲染的解说；将只输出原视频（可选头像）。", file=sys.stderr)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="bettergi-vtuber-") as temp_dir:
        ass_path = Path(temp_dir) / "vtuber.ass"
        with open(ass_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(build_ass(cues, font=args.font))

        tts_files: List[Tuple[float, str]] = []
        if args.tts:
            tts_files = synthesize_tts(cues, args.tts_voice, temp_dir)
            print("[信息] TTS 成功合成 {} 条。".format(len(tts_files)))

        command = build_ffmpeg_command(
            ffmpeg=ffmpeg,
            video=str(video_path),
            ass_path=str(ass_path),
            output=str(output_path),
            avatar=args.avatar,
            tts_files=tts_files,
            input_has_audio=media.has_audio if media else None,
            duration=video_duration,
            avatar_width=args.avatar_width,
        )
        print("[信息] 开始渲染：{}".format(output_path))
        code = run_ffmpeg(command)

        # 输入音轨若不是 MP4 兼容编码，copy 会失败；这里自动降级为 AAC。
        if code != 0 and not tts_files:
            print("[提示] 首次渲染失败，尝试把音频转码为 AAC 后重试…", file=sys.stderr)
            command = build_ffmpeg_command(
                ffmpeg=ffmpeg,
                video=str(video_path),
                ass_path=str(ass_path),
                output=str(output_path),
                avatar=args.avatar,
                tts_files=[],
                input_has_audio=media.has_audio if media else None,
                duration=video_duration,
                avatar_width=args.avatar_width,
                copy_audio=False,
            )
            code = run_ffmpeg(command)

        if code != 0:
            print("[错误] 渲染失败，ffmpeg 退出码 {}。".format(code), file=sys.stderr)
            return code

    if output_path.is_file():
        size_mb = output_path.stat().st_size / (1024 * 1024)
        print("[完成] 已输出：{}（{:.1f} MB）".format(output_path, size_mb))
    else:
        print("[完成] 渲染结束：{}".format(output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
