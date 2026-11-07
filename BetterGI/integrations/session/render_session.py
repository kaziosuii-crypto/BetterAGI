#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""会话离线后期渲染器：把一场录制渲染成带配音与字幕的成片。

输入是一个会话目录（格式见 ``session_format.py``）::

    sessions/<session_id>/
        session.jsonl    # 唯一权威记录（本脚本只读，绝不修改）
        gameplay.mp4     # 可选；没有时使用黑色背景

输出写在会话目录内::

        audio/base-<n>.wav    # SAPI 合成的基础配音
        audio/<n>.wav         # SVC 音色转换结果（可选）
        audio/<n>-aligned.wav # 按事件 dur 拉伸后的最终配音（可选）
        audio/mix.m4a         # 按统一时间轴合成的音轨
        subtitles.ass         # 字幕文件
        render.json           # 渲染报告
        final.mp4             # 成片（H.264 + AAC）

设计要点：

* 声音与口型对齐：``speech.dur`` 是录制时预估的朗读时长，口型包络也按
  它生成。后期合成真实语音（飞鱼 CosyVoice3 / SAPI / SVC）后，再用
  ffmpeg ``atempo`` 把实际时长拉伸到 ``dur`` 附近，口型因此天然同步。
* 飞鱼 TTS 走批量接口：一次加载模型，合成全部台词，避免逐句重启。
* 时间轴统一：所有声音按事件 ``t`` 用 ``adelay`` 放进同一段静音底，
  字幕使用同一个 ``t``；会话总时长是成片长度。
* 只写派生文件：不向 ``session.jsonl`` 追加或修改任何内容。

用法::

    python render_session.py sessions/20261027-120000-ab12cd   # 默认飞鱼 cute
    python render_session.py sessions/xxx --tts feiyu --feiyu-style gentle
    python render_session.py sessions/xxx --tts sapi
    python render_session.py sessions/xxx --tts none --no-subtitles
    python render_session.py sessions/xxx --svc-model E:/betteragi/svc/models/myvoice

依赖：Python 3.10+ 标准库、ffmpeg/ffprobe；SAPI 需要 Windows PowerShell；
飞鱼 TTS 需要 ``E:\BetterAGI\cosyvoice`` 环境；SVC 需要 ``--svc-python``
指向已安装 ``so_vits_svc_fork`` 的解释器。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from session_format import (
    Session,
    epoch_ms_to_iso,
    estimate_speech_ms,
    read_session,
)

# ---------------------------------------------------------------------------
# 默认值与常量
# ---------------------------------------------------------------------------

# 默认 SAPI 语音：Windows 10/11 简体中文系统自带。
DEFAULT_VOICE = "Microsoft Huihui Desktop"
# SVC 解释器默认路径（本机 so-vits-svc 的虚拟环境）。
DEFAULT_SVC_PYTHON = r"E:\BetterAGI\svc\.venv\Scripts\python.exe"
# 飞鱼 CosyVoice3 离线 TTS：独立环境与批量推理脚本。
DEFAULT_FEIYU_PYTHON = r"E:\BetterAGI\cosyvoice\.venv\Scripts\python.exe"
DEFAULT_FEIYU_SCRIPT = r"E:\BetterAGI\cosyvoice\feiyu_tts.py"
DEFAULT_FEIYU_MODEL_ROOT = r"E:\BetterAGI\cosyvoice\model"
FEIYU_TIMEOUT_S = 7200.0
# SVC 命令模板：{python} / {input} / {output} / {model} 会被替换。
# 模型目录约定为「G_*.pth + config.json」，因此显式把 config 传给 infer。
DEFAULT_SVC_CMD = (
    '"{python}" -m so_vits_svc_fork infer "{input}" '
    '-m "{model}" -c "{model}/config.json" -fm crepe -o "{output}"'
)
DEFAULT_FONT = "Microsoft YaHei"

# 没有 gameplay.mp4 时的黑底画布参数。
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
DEFAULT_FPS = 30.0

# 拉伸比允许范围：超出时保留原速，避免声音失真到不可用。
TEMPO_MIN = 0.75
TEMPO_MAX = 1.5
# 拉伸比与 1 的差值小于该值时视为已经对齐，不再重采样。
TEMPO_EPSILON = 0.005

# 字幕最短显示时长（毫秒）。
MIN_SUBTITLE_MS = 400
# 两条配音之间的最小间隔；排程时避免重叠。
SPEECH_GAP_MS = 120

# 外部命令超时（秒）。
FFPROBE_TIMEOUT_S = 30.0
SAPI_TIMEOUT_S = 120.0
SVC_TIMEOUT_S = 600.0
FFMPEG_TIMEOUT_S = 1800.0

# 音频统一采样率与声道数，便于 adelay/amix 叠加。
AUDIO_RATE = 48000

# 与 ``render_vtuber.py`` 保持一致的配色（本脚本不 import 它）。
KIND_COLORS: Dict[str, str] = {
    "reaction": "#ff6b9d",
    "plan": "#4fc3f7",
    "emotion": "#ffd166",
    "lore": "#b388ff",
    "chat": "#7cf5c0",
    "result": "#ffa94d",
    "guidance": "#9bd7ff",
}

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

ASS_STYLE_FORMAT = (
    "Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
    "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, "
    "Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
)
ASS_EVENT_FORMAT = (
    "Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
)


class RenderError(RuntimeError):
    """渲染流程中的可预期错误（CLI 会打印友好提示并以非零码退出）。"""


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class MediaInfo:
    """ffprobe 探测到的媒体信息。"""

    duration_ms: int = 0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_video: bool = False
    has_audio: bool = False


@dataclass
class SpeechResult:
    """单条 ``speech`` 事件的后期处理结果。"""

    index: int
    t_ms: int
    text: str
    kind: str
    emotion: str
    dur_ms: int
    source: Optional[str] = None
    thought_id: Optional[str] = None
    intent_id: Optional[str] = None
    generated_at_ms: Optional[int] = None
    frame_seq: Optional[int] = None
    start_ms: Optional[int] = None
    shift_ms: int = 0
    base_audio: Optional[Path] = None
    svc_audio: Optional[Path] = None
    final_audio: Optional[Path] = None
    actual_ms: Optional[int] = None
    final_ms: Optional[int] = None
    tempo_ratio: Optional[float] = None
    tempo_applied: bool = False
    svc_applied: bool = False
    volume: float = 1.0
    pause_before_ms: int = 0
    pace: str = "normal"
    interruptible: bool = True
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 外部命令辅助
# ---------------------------------------------------------------------------


def _no_window_flags() -> int:
    """Windows 下隐藏子进程控制台窗口；其他平台返回 0。"""

    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


def _tail(value: str, limit: int = 800) -> str:
    """截取命令输出末尾，避免错误信息过长。"""

    text = (value or "").strip()
    return text[-limit:] if len(text) > limit else text


def _run(
    command: Sequence[str],
    *,
    timeout: float,
    description: str,
    cwd: Optional[Path] = None,
) -> subprocess.CompletedProcess[str]:
    """运行外部命令并捕获输出。

    ``subprocess.run`` 的超时会直接终止子进程；启动失败与超时都转换成
    ``RenderError``，由调用方决定是否回退。
    """

    try:
        return subprocess.run(
            [str(part) for part in command],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_no_window_flags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"{description}超时（{timeout:.0f} 秒），进程已终止") from exc
    except OSError as exc:
        raise RenderError(f"{description}无法启动：{exc}") from exc


@contextmanager
def _filter_script(
    directory: Path, prefix: str, lines: Sequence[str]
) -> Iterator[Path]:
    """把 filter_complex 写入临时脚本文件，退出上下文后删除。

    ffmpeg 的滤镜图可能很长（几百条解说），放进文件既避免命令行超长，
    也更容易排查问题。
    """

    directory.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=prefix, suffix=".filter", dir=str(directory))
    os.close(handle)
    path = Path(name)
    try:
        # ffmpeg 9 从文件读取滤镜图时不再把换行当作滤镜链分隔符，
        # 统一使用分号分隔，老版本的 -filter_complex_script 同样接受。
        path.write_text(";\n".join(lines) + "\n", encoding="utf-8")
        yield path
    finally:
        path.unlink(missing_ok=True)


def _filter_script_option(ffmpeg: str) -> str:
    """返回当前 ffmpeg 支持的「从文件读取滤镜图」选项名。

    ffmpeg 8/9 移除了 ``-filter_complex_script``，改为 ``-/filter_complex``；
    老版本仍然使用前者。这里探测一次帮助文本，兼容两种版本。
    """

    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-h", "full"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=FFPROBE_TIMEOUT_S,
            creationflags=_no_window_flags(),
        )
        help_text = (result.stdout or "") + (result.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        help_text = ""
    return (
        "-filter_complex_script"
        if "filter_complex_script" in help_text
        else "-/filter_complex"
    )


def _probe_media(path: Path, ffprobe: str) -> MediaInfo:
    """用 ffprobe 读取时长与首条视频流的尺寸/帧率。"""

    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,width,height,r_frame_rate,duration",
        "-of",
        "json",
        str(path),
    ]
    result = _run(
        command, timeout=FFPROBE_TIMEOUT_S, description=f"ffprobe 读取 {path.name}"
    )
    if result.returncode != 0:
        raise RenderError(
            f"ffprobe 读取 {path.name} 失败（退出码 {result.returncode}）：{_tail(result.stderr)}"
        )
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RenderError(f"ffprobe 输出不是合法 JSON：{exc}") from exc

    duration_s = _to_float((data.get("format") or {}).get("duration"))
    info = MediaInfo()
    for stream in data.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        codec_type = str(stream.get("codec_type") or "")
        if codec_type == "video" and not info.has_video:
            info.has_video = True
            info.width = int(stream.get("width") or 0)
            info.height = int(stream.get("height") or 0)
            info.fps = _parse_fps(stream.get("r_frame_rate"))
            if duration_s <= 0:
                duration_s = _to_float(stream.get("duration"))
        elif codec_type == "audio":
            info.has_audio = True
            if duration_s <= 0:
                duration_s = _to_float(stream.get("duration"))
    info.duration_ms = int(round(duration_s * 1000.0))
    return info


def _probe_duration_ms(path: Path, ffprobe: str) -> int:
    """读取媒体时长（毫秒）；用于配音文件与成片校验。"""

    info = _probe_media(path, ffprobe)
    if info.duration_ms <= 0:
        raise RenderError(f"无法读取音频时长：{path}")
    return info.duration_ms


def _to_float(value: Any) -> float:
    """把 ffprobe 的字符串数值安全转换为 float。"""

    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_fps(value: Any) -> float:
    """解析 ffprobe 的 ``30000/1001`` 形式帧率。"""

    text = str(value or "")
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        top = _to_float(numerator)
        bottom = _to_float(denominator)
        return top / bottom if bottom else 0.0
    return _to_float(text)


# ---------------------------------------------------------------------------
# SAPI 合成
# ---------------------------------------------------------------------------


def _ps_quote(value: str) -> str:
    """把字符串转义成 PowerShell 单引号字面量。"""

    return "'" + str(value).replace("'", "''") + "'"


def synthesize_sapi(text: str, out_path: Path, voice: str, rate: int) -> None:
    """用 Windows SAPI 把一段文本合成为 WAV。

    台词先写入 UTF-8 临时文件，PowerShell 脚本再按 UTF-8 读取；脚本本身
    以 UTF-16LE + Base64 传入 ``-EncodedCommand``，因此台词和路径里的引号、
    换行、中文都不会破坏命令行，也不存在拼接注入问题。
    """

    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    if not powershell:
        raise RenderError("找不到 PowerShell，无法调用 Windows SAPI 语音合成")
    if not -10 <= int(rate) <= 10:
        raise RenderError(f"SAPI Rate 必须在 -10 到 10 之间，收到 {rate}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)

    # 临时文本文件：供 PowerShell 用 UTF-8 读取，避免长台词放进脚本文本。
    handle, name = tempfile.mkstemp(
        prefix="sapi-", suffix=".txt", dir=str(out_path.parent)
    )
    os.close(handle)
    text_file = Path(name)
    try:
        text_file.write_text(str(text), encoding="utf-8", newline="\n")
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
            "Add-Type -AssemblyName System.Speech\n"
            "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
            "try {\n"
            f"    $synth.SelectVoice({_ps_quote(voice)})\n"
            f"    $synth.Rate = {int(rate)}\n"
            f"    $synth.SetOutputToWaveFile({_ps_quote(str(out_path))})\n"
            f"    $text = [System.IO.File]::ReadAllText({_ps_quote(str(text_file))}, "
            "[System.Text.Encoding]::UTF8)\n"
            "    $synth.Speak($text)\n"
            "} catch {\n"
            "    [Console]::Error.WriteLine($_.Exception.Message)\n"
            "    exit 1\n"
            "} finally {\n"
            "    $synth.Dispose()\n"
            "}\n"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        result = _run(
            [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            timeout=SAPI_TIMEOUT_S,
            description="SAPI 语音合成",
        )
        if result.returncode != 0:
            detail = _tail(result.stderr or result.stdout)
            raise RenderError(
                f"SAPI 合成失败（语音 {voice!r}，退出码 {result.returncode}）："
                f"{detail or '无错误输出'}"
            )
    finally:
        text_file.unlink(missing_ok=True)

    if not out_path.is_file() or out_path.stat().st_size <= 0:
        raise RenderError(f"SAPI 未生成有效音频文件：{out_path}")


# ---------------------------------------------------------------------------
# SVC 音色转换
# ---------------------------------------------------------------------------


def _split_command_template(template: str) -> List[str]:
    """把命令模板按空白拆分，同时尊重单/双引号。

    模板里用引号包住含空格的参数即可；占位符替换在拆分之后进行，因此
    替换进去的路径即使含空格也仍然是一个完整参数。
    """

    tokens: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    for char in str(template):
        if quote is not None:
            if char == quote:
                quote = None
            else:
                current.append(char)
        elif char in ("'", '"'):
            quote = char
        elif char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(char)
    if quote is not None:
        raise RenderError("--svc-cmd 模板存在未闭合的引号")
    if current:
        tokens.append("".join(current))
    return tokens


def build_svc_command(
    template: str,
    *,
    python: str,
    input_path: Path,
    model: str,
    output_path: Path,
) -> List[str]:
    """按模板生成 SVC 命令参数列表。"""

    if "{input}" not in template or "{output}" not in template:
        raise RenderError("--svc-cmd 模板必须包含 {input} 与 {output} 占位符")
    replacements: Dict[str, str] = {
        "{python}": str(python),
        "{input}": str(input_path),
        "{model}": str(model),
        "{output}": str(output_path),
    }
    command: List[str] = []
    for token in _split_command_template(template):
        for placeholder, value in replacements.items():
            token = token.replace(placeholder, value)
        command.append(token)
    return command


def run_svc(
    *,
    svc_python: str,
    template: str,
    base_wav: Path,
    model: str,
    out_wav: Path,
) -> None:
    """调用 so-vits-svc 把基础 WAV 转成目标音色。"""

    if not Path(svc_python).is_file():
        raise RenderError(f"找不到 SVC Python 解释器：{svc_python}")
    if not Path(model).exists():
        raise RenderError(f"找不到 SVC 模型目录：{model}")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    out_wav.unlink(missing_ok=True)
    command = build_svc_command(
        template,
        python=svc_python,
        input_path=base_wav,
        model=model,
        output_path=out_wav,
    )
    result = _run(command, timeout=SVC_TIMEOUT_S, description="SVC 音色转换")
    if result.returncode != 0:
        raise RenderError(
            f"SVC 转换失败（退出码 {result.returncode}）：{_tail(result.stderr or result.stdout)}"
        )
    if not out_wav.is_file() or out_wav.stat().st_size <= 0:
        raise RenderError(f"SVC 未生成有效音频文件：{out_wav}")


def run_atempo(ffmpeg: str, source: Path, target: Path, ratio: float) -> None:
    """用 ffmpeg ``atempo`` 改变语速，使时长接近目标。"""

    target.unlink(missing_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-filter:a",
        f"atempo={ratio:.6f}",
        "-ac",
        "2",
        "-ar",
        str(AUDIO_RATE),
        str(target),
    ]
    result = _run(command, timeout=FFMPEG_TIMEOUT_S, description="音频拉伸")
    if result.returncode != 0:
        raise RenderError(
            f"音频拉伸失败（退出码 {result.returncode}）：{_tail(result.stderr)}"
        )
    if not target.is_file() or target.stat().st_size <= 0:
        raise RenderError(f"ffmpeg 未生成拉伸后的音频：{target}")


# ---------------------------------------------------------------------------
# ASS 字幕
# ---------------------------------------------------------------------------


def ass_color(hex_color: str, alpha: int = 0) -> str:
    """把 ``#RRGGBB`` 转换为 ASS 的 ``&HAABBGGRR``。"""

    value = str(hex_color or "").lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        value = "ffffff"
    red = int(value[0:2], 16)
    green = int(value[2:4], 16)
    blue = int(value[4:6], 16)
    return "&H{:02X}{:02X}{:02X}{:02X}".format(
        max(0, min(255, alpha)), blue, green, red
    )


def escape_ass_text(text: str) -> str:
    """转义 ASS 特殊字符，并把换行转换为 ``\\N``。"""

    value = str(text).replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\\", "\\\\")
    value = value.replace("{", "\\{").replace("}", "\\}")
    value = value.replace("\n", "\\N")
    value = value.replace("\t", " ")
    return value


def format_ass_time(seconds: float) -> str:
    """把秒转换为 ASS 的 ``H:MM:SS.cc``。"""

    total_centiseconds = int(round(max(0.0, float(seconds)) * 100))
    hours, remainder = divmod(total_centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, centiseconds = divmod(remainder, 100)
    return "{0}:{1:02d}:{2:02d}.{3:02d}".format(hours, minutes, secs, centiseconds)


def _style_name(kind: str, emotion: str) -> str:
    """生成合法且稳定的 ASS 样式名。"""

    raw = "{}_{}".format(kind or "reaction", emotion or "none")
    safe = re.sub(r"[^0-9A-Za-z_]+", "_", raw).strip("_")
    return "S_" + (safe or "speech")


def _pace_factor(event: Dict[str, Any]) -> float:
    """把 speech.pace 映射为飞鱼的单句语速倍率。"""

    pace = str(event.get("pace") or "normal").lower()
    return {"slow": 0.92, "fast": 1.08}.get(pace, 1.0)


def build_ass(
    entries: Sequence[Tuple[SpeechResult, int]],
    *,
    font: str,
    width: int,
    height: int,
    title: str,
) -> str:
    """生成完整 ASS 字幕文档。

    ``entries`` 是 ``(处理结果, 字幕结束毫秒)`` 列表；每条 ``speech`` 对应
    一条 Dialogue，样式按 ``kind``（主色）与 ``emotion``（描边色）区分。
    """

    font_name = str(font or DEFAULT_FONT).replace(",", " ").replace("\n", " ").strip()
    if not font_name:
        font_name = DEFAULT_FONT
    safe_title = str(title or "").replace("\n", " ").strip()

    style_lines: List[str] = [
        "Style: Default,{font},34,{primary},{primary},{outline},{back},"
        "0,0,0,0,100,100,0,0,1,3.2,1.6,2,150,150,46,1".format(
            font=font_name,
            primary=ass_color("#FFFFFF"),
            outline=ass_color("#10131A"),
            back=ass_color("#000000", 128),
        )
    ]
    style_map: Dict[Tuple[str, str], str] = {}
    dialogue_lines: List[str] = []

    for result, end_ms in entries:
        key = (result.kind or "reaction", result.emotion or "")
        style = style_map.get(key)
        if style is None:
            style = _style_name(*key)
            style_map[key] = style
            primary = ass_color(KIND_COLORS.get(key[0], "#E8F1FF"))
            outline = ass_color(EMOTION_COLORS.get(key[1], "#10131A"))
            style_lines.append(
                "Style: {name},{font},34,{primary},{primary},{outline},{back},"
                "0,0,0,0,100,100,0,0,1,3.2,1.6,2,150,150,46,1".format(
                    name=style,
                    font=font_name,
                    primary=primary,
                    outline=outline,
                    back=ass_color("#000000", 128),
                )
            )
        dialogue_lines.append(
            "Dialogue: 0,{start},{end},{style},,0,0,0,,{{\\fad(160,220)}}{text}".format(
                start=format_ass_time((result.start_ms or result.t_ms) / 1000.0),
                end=format_ass_time(end_ms / 1000.0),
                style=style,
                text=escape_ass_text(result.text),
            )
        )

    lines = [
        "[Script Info]",
        "; Generated by render_session.py",
        f"Title: {safe_title or 'BetterGI Session'}",
        "ScriptType: v4.00+",
        f"PlayResX: {int(width)}",
        f"PlayResY: {int(height)}",
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


def escape_filter_path(path: Path | str) -> str:
    """转义 ffmpeg 滤镜中使用的文件路径（Windows 盘符冒号等）。

    路径统一改为正斜杠，冒号、单引号、方括号、逗号、分号都按 ffmpeg
    滤镜语法转义；调用方再在外层加单引号。
    """

    value = str(path).replace("\\", "/")
    value = value.replace("'", "\\'")
    value = value.replace(":", "\\:")
    value = value.replace("[", "\\[").replace("]", "\\]")
    value = value.replace(",", "\\,").replace(";", "\\;")
    return value


# ---------------------------------------------------------------------------
# 渲染器
# ---------------------------------------------------------------------------


class SessionRenderer:
    """把单个会话目录渲染为 ``final.mp4``。"""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        session_dir = Path(args.session_dir)
        if not session_dir.exists():
            raise RenderError(f"会话目录不存在：{session_dir}")
        try:
            self.session: Session = read_session(session_dir)
        except (OSError, ValueError) as exc:
            raise RenderError(f"读取会话失败：{exc}") from exc

        self.session_dir: Path = self.session.directory
        out = Path(args.out)
        self.out_path: Path = out if out.is_absolute() else self.session_dir / out
        self.audio_dir: Path = self.session_dir / "audio"
        self.mix_path: Path = self.audio_dir / "mix.m4a"
        self.subtitle_path: Path = self.session_dir / "subtitles.ass"
        self.report_path: Path = self.session_dir / "render.json"

        self.ffmpeg: str = shutil.which("ffmpeg") or ""
        self.ffprobe: str = shutil.which("ffprobe") or ""
        self.script_option: str = ""
        self.warnings: List[str] = []

        # 视频探测结果（在 _inspect_video 中填充）。
        self.use_video: bool = False
        self.video_media: Optional[MediaInfo] = None
        self.video_offset_ms: int = 0
        self.canvas_width: int = DEFAULT_WIDTH
        self.canvas_height: int = DEFAULT_HEIGHT
        self.canvas_fps: float = DEFAULT_FPS

        # 解析实际引擎：auto 在有完整飞鱼环境时优先飞鱼 cute，否则 SAPI。
        requested_tts = args.tts
        if requested_tts == "auto":
            feiyu_ready = (
                Path(args.feiyu_python).is_file()
                and Path(args.feiyu_script).is_file()
                and (
                    Path(args.feiyu_model_root)
                    / "models"
                    / "Fun-CosyVoice3-0.5B-2512"
                    / "cosyvoice3.yaml"
                ).is_file()
            )
            self.effective_tts = "feiyu" if feiyu_ready else "sapi"
        else:
            self.effective_tts = requested_tts

        # 参数组合校验（基于实际使用的引擎）。
        if self.effective_tts == "none" and args.svc_model:
            raise RenderError(
                "--svc-model 需要 --tts sapi：SVC 必须基于 SAPI 合成的基础音频"
            )
        if self.effective_tts == "feiyu" and args.svc_model:
            raise RenderError("飞鱼 TTS 直接生成角色音色，不需要再叠加 SVC")
        if self.effective_tts == "sapi" and not -10 <= int(args.rate) <= 10:
            raise RenderError("--rate 必须在 -10 到 10 之间")
        if args.avatar_image and not Path(args.avatar_image).is_file():
            raise RenderError(f"找不到头像图片：{args.avatar_image}")

        # 实际使用的音色引擎；飞鱼失败回退 SAPI 时会在 render() 中改写。
        self.feiyu_load_seconds: Optional[float] = None

    # -- 主流程 ----------------------------------------------------------

    def render(self) -> Dict[str, Any]:
        """执行完整渲染流程并返回写入 ``render.json`` 的报告。"""

        if not self.ffmpeg or not self.ffprobe:
            raise RenderError("找不到 ffmpeg/ffprobe，请安装并加入 PATH")
        self.script_option = _filter_script_option(self.ffmpeg)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)

        # 会话可能异常中断（缺少 end）；校验失败只记录警告，尽量继续渲染。
        try:
            self.session.validate()
        except ValueError as exc:
            self.warnings.append(f"会话校验警告：{exc}")

        self._inspect_video()

        speeches = self.session.speeches
        feiyu_map: Dict[int, Path] = {}
        if self.effective_tts == "feiyu" and speeches:
            try:
                feiyu_map = self._prepare_feiyu_batch(speeches)
            except RenderError as exc:
                if not self.args.allow_tts_fallback:
                    raise
                self.warnings.append(f"飞鱼 TTS 失败，已回退 SAPI：{exc}")
                self.effective_tts = "sapi"

        results: List[SpeechResult] = []
        for index, event in enumerate(speeches, start=1):
            preview = str(event.get("text") or "")[:24]
            print(f"[{index}/{len(speeches)}] 处理解说：{preview}", flush=True)
            results.append(self._prepare_speech(index, event, feiyu_map))

        self._schedule(results)
        total_ms = self._total_duration_ms(results)
        subtitle_path = self._write_subtitles(results)
        mix_path = self._mix_audio(results, total_ms)
        self._render_video(total_ms, mix_path, subtitle_path)

        report = self._build_report(results, total_ms, subtitle_path, mix_path)
        self.report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        for warning in self.warnings:
            print(f"警告：{warning}", file=sys.stderr, flush=True)
        return report

    # -- 视频探测 --------------------------------------------------------

    def _game_audio_path(self) -> Optional[Path]:
        """可选游戏原音：显式路径优先，否则自动找 audio/game_audio.*。"""
        if getattr(self.args, "no_game_audio", False):
            return None
        explicit = getattr(self.args, "game_audio", "") or ""
        if explicit:
            path = Path(explicit)
            if not path.is_absolute():
                path = self.session_dir / path
            return path if path.is_file() else None
        for suffix in (".m4a", ".wav", ".mp3", ".flac", ".ogg"):
            candidate = self.audio_dir / ("game_audio" + suffix)
            if candidate.is_file():
                return candidate
        return None

    def _game_audio_offset_ms(self, path: Path) -> int:
        """只套用与所选音轨路径相同的事件；无对应事件时沿用零偏移。"""
        selected = path.resolve()
        for event in self.session.of_type("audio"):
            name = event.get("path")
            if not isinstance(name, str) or not name:
                continue
            recorded = Path(name)
            if not recorded.is_absolute():
                recorded = self.session_dir / recorded
            if recorded.resolve() != selected:
                continue
            value = event.get("start_offset_ms", event.get("t", 0))
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return 0

    def _inspect_video(self) -> None:
        """探测 gameplay.mp4；不可用时回退到黑底。"""

        path = self.session.video_path
        if path is None:
            return
        try:
            media = _probe_media(path, self.ffprobe)
        except RenderError as exc:
            self.warnings.append(f"无法解析画面 {path.name}，改用黑色背景：{exc}")
            return
        if not media.has_video:
            self.warnings.append(f"画面文件 {path.name} 不含视频流，改用黑色背景")
            return
        self.use_video = True
        self.video_media = media
        self.canvas_width = media.width or DEFAULT_WIDTH
        self.canvas_height = media.height or DEFAULT_HEIGHT
        self.canvas_fps = media.fps or DEFAULT_FPS
        self.video_offset_ms = self._video_offset_ms()

    def _video_offset_ms(self) -> int:
        """画面相对会话起点的开始偏移；取最早一条 ``video`` 事件的 ``t``。"""

        events = self.session.of_type("video")
        return min((max(0, int(event.get("t") or 0)) for event in events), default=0)

    # -- 飞鱼 TTS --------------------------------------------------------

    def _prepare_feiyu_batch(
        self, speeches: Sequence[Dict[str, Any]]
    ) -> Dict[int, Path]:
        """一次加载模型、批量合成所有台词。

        逐句调用会把 4GB 级模型反复加载；批量请求只加载一次，句子之间
        共享 KV/ONNX 会话。返回 ``{序号: wav 路径}``。
        """

        python = Path(self.args.feiyu_python)
        script = Path(self.args.feiyu_script)
        if not python.is_file():
            raise RenderError(f"找不到飞鱼 TTS 解释器：{python}")
        if not script.is_file():
            raise RenderError(f"找不到飞鱼 TTS 脚本：{script}")

        request_path = self.audio_dir / "feiyu-request.json"
        result_path = self.audio_dir / "feiyu-result.json"
        request = {
            "style": self.args.feiyu_style,
            "speed": float(self.args.feiyu_speed),
            "seed": int(self.args.feiyu_seed),
            "output_dir": str(self.audio_dir),
            "texts": [
                {
                    "index": index,
                    "text": str(event.get("text") or ""),
                    "speed": round(
                        float(self.args.feiyu_speed) * _pace_factor(event), 3
                    ),
                }
                for index, event in enumerate(speeches, start=1)
            ],
        }
        request_path.write_text(
            json.dumps(request, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        command = [
            str(python),
            str(script),
            "--request",
            str(request_path),
            "--result",
            str(result_path),
            "--style",
            str(self.args.feiyu_style),
            "--speed",
            str(float(self.args.feiyu_speed)),
            "--seed",
            str(int(self.args.feiyu_seed)),
            "--model-root",
            str(self.args.feiyu_model_root),
        ]
        if self.args.feiyu_fp16:
            command.append("--fp16")
        print(
            f"飞鱼 TTS：{len(speeches)} 句，首次加载模型可能需要一两分钟…",
            flush=True,
        )
        completed = _run(command, timeout=FEIYU_TIMEOUT_S, description="飞鱼 TTS")
        if completed.returncode != 0:
            raise RenderError(
                f"飞鱼 TTS 退出码 {completed.returncode}：{_tail(completed.stderr or completed.stdout)}"
            )
        if not result_path.is_file():
            raise RenderError("飞鱼 TTS 没有生成 result.json")
        data = json.loads(result_path.read_text(encoding="utf-8-sig"))
        self.feiyu_load_seconds = float(data.get("load_seconds") or 0.0) or None
        mapping: Dict[int, Path] = {}
        for item in data.get("results") or []:
            index = int(item.get("index") or 0)
            if "error" in item:
                self.warnings.append(f"飞鱼 TTS 第 {index} 句失败：{item.get('error')}")
                continue
            path = Path(str(item.get("path") or ""))
            if not path.is_file() or path.stat().st_size <= 0:
                self.warnings.append(f"飞鱼 TTS 第 {index} 句产物缺失：{path}")
                continue
            mapping[index] = path
        if not mapping:
            raise RenderError("飞鱼 TTS 没有生成任何可用音频")
        if len(mapping) < len(speeches):
            self.warnings.append(
                f"飞鱼 TTS 只成功 {len(mapping)}/{len(speeches)} 句，其余按失败处理"
            )
        print(
            f"飞鱼 TTS 完成：{len(mapping)} 句，模型加载 {self.feiyu_load_seconds or 0:.1f}s",
            flush=True,
        )
        return mapping

    # -- 单条解说 --------------------------------------------------------

    def _prepare_speech(
        self,
        index: int,
        event: Dict[str, Any],
        feiyu: Optional[Dict[int, Path]] = None,
    ) -> SpeechResult:
        """准备一条解说的音频并按 ``dur`` 对齐到目标时长。"""

        text = str(event.get("text") or "")
        dur_ms = int(event.get("dur") or 0)
        if dur_ms <= 0:
            dur_ms = estimate_speech_ms(text)
        result = SpeechResult(
            index=index,
            t_ms=max(0, int(event.get("t") or 0)),
            text=text,
            kind=str(event.get("kind") or "reaction"),
            emotion=str(event.get("emotion") or "calm"),
            dur_ms=dur_ms,
            source=event.get("source"),
            thought_id=event.get("thought_id"),
            intent_id=event.get("intent_id"),
            generated_at_ms=event.get("generated_at_ms"),
            frame_seq=event.get("frame_seq"),
            start_ms=max(0, int(event.get("t") or 0)),
        )
        # prosody 只影响表现，不改变当时 speech 的含义。
        try:
            volume = str(event.get("volume") or "normal").lower()
            result.volume = 0.8 if volume == "soft" else (1.15 if volume == "loud" else 1.0)
        except (TypeError, ValueError):
            result.volume = 1.0
        try:
            result.pause_before_ms = max(0, min(2000, int(event.get("pause_before_ms") or 0)))
        except (TypeError, ValueError):
            result.pause_before_ms = 0
        result.pace = str(event.get("pace") or "normal").lower()
        if result.pace not in ("slow", "normal", "fast"):
            result.pace = "normal"
        result.interruptible = bool(event.get("interruptible", True))

        # 纯字幕模式：不生成任何音频，字幕按预估时长显示。
        if self.effective_tts == "none":
            return result

        if self.effective_tts == "feiyu":
            produced = (feiyu or {}).get(index)
            if produced is not None:
                result.base_audio = produced
                actual_ms = _probe_duration_ms(produced, self.ffprobe)
                result.actual_ms = actual_ms
                source = produced
            else:
                if not self.args.allow_tts_fallback:
                    raise RenderError(f"飞鱼 TTS 缺少第 {index} 句音频")
                self.warnings.append(f"第 {index} 句飞鱼音频缺失，回退 SAPI")
                result.warnings.append("飞鱼音频缺失，回退 SAPI")
                base_audio = self.audio_dir / f"base-{index}.wav"
                synthesize_sapi(text, base_audio, self.args.voice, int(self.args.rate))
                result.base_audio = base_audio
                actual_ms = _probe_duration_ms(base_audio, self.ffprobe)
                result.actual_ms = actual_ms
                source = base_audio
        else:
            base_audio = self.audio_dir / f"base-{index}.wav"
            synthesize_sapi(text, base_audio, self.args.voice, int(self.args.rate))
            result.base_audio = base_audio
            actual_ms = _probe_duration_ms(base_audio, self.ffprobe)
            result.actual_ms = actual_ms
            source = base_audio

        # 可选 SVC：把 SAPI 基础音色转换成目标音色（飞鱼已有角色音色，跳过）。
        if self.args.svc_model and self.effective_tts == "sapi":
            svc_audio = self.audio_dir / f"{index}.wav"
            try:
                run_svc(
                    svc_python=self.args.svc_python,
                    template=self.args.svc_cmd,
                    base_wav=base_audio,
                    model=self.args.svc_model,
                    out_wav=svc_audio,
                )
                svc_duration = _probe_duration_ms(svc_audio, self.ffprobe)
            except RenderError as exc:
                if not self.args.allow_tts_fallback:
                    raise
                svc_audio.unlink(missing_ok=True)
                result.warnings.append(f"SVC 失败，已回退到 SAPI 基础音频：{exc}")
            else:
                result.svc_audio = svc_audio
                result.svc_applied = True
                result.actual_ms = svc_duration
                actual_ms = svc_duration
                source = svc_audio

        # 后期对齐：ratio = 实际 / 目标；超出范围保留原速并记录。
        ratio = actual_ms / float(dur_ms) if dur_ms > 0 else 1.0
        result.tempo_ratio = round(ratio, 4)
        if TEMPO_MIN <= ratio <= TEMPO_MAX:
            if abs(ratio - 1.0) > TEMPO_EPSILON:
                aligned = self.audio_dir / f"{index}-aligned.wav"
                run_atempo(self.ffmpeg, source, aligned, ratio)
                result.final_audio = aligned
                result.final_ms = _probe_duration_ms(aligned, self.ffprobe)
                result.tempo_applied = True
            else:
                result.final_audio = source
                result.final_ms = actual_ms
        else:
            result.final_audio = source
            result.final_ms = actual_ms
            result.warnings.append(
                f"实际 {actual_ms}ms 与目标 {dur_ms}ms 的拉伸比 {ratio:.2f} "
                f"超出 {TEMPO_MIN}–{TEMPO_MAX}，保留原速"
            )

        for warning in result.warnings:
            print(f"    警告：{warning}", file=sys.stderr, flush=True)
        return result

    # -- 时长 ------------------------------------------------------------

    # -- 排程 ------------------------------------------------------------

    def _schedule(self, results: Sequence[SpeechResult]) -> None:
        """按时间顺序排开配音，避免两句声音叠在一起。

        正常情况下每句都落在自己的 ``t``；只有前一句实际时长压到下一句
        的起点时才顺延，并把顺延量写进警告与 ``render.json``。顺延不修改
        ``session.jsonl``，字幕与音轨都以 ``start_ms`` 为准。
        """

        cursor = 0
        for result in sorted(results, key=lambda item: item.t_ms):
            pause = max(0, int(getattr(result, "pause_before_ms", 0) or 0))
            start = max(result.t_ms, cursor) + pause
            result.start_ms = start
            result.shift_ms = max(0, start - result.t_ms)
            if result.shift_ms > 50:
                self.warnings.append(
                    f"第 {result.index} 句为避让上一句延后 {result.shift_ms}ms"
                )
                result.warnings.append(f"排程延后 {result.shift_ms}ms")
            length = result.final_ms if result.final_ms is not None else result.dur_ms
            cursor = start + max(0, length) + SPEECH_GAP_MS

    def _total_duration_ms(self, results: Sequence[SpeechResult]) -> int:
        """成片总时长：以会话时长为准，最后一句配音超出时补尾而不是截断。

        画面来源会比会话短时由 ``tpad`` 复制最后一帧补足，因此让成片多出
        一两秒比切掉半句台词更自然。
        """

        total = int(self.session.duration_ms or 0)
        # 异常中断没有 end 时，事件时间不包含录音尾部；用文件时长恢复。
        if not self.session.of_type("end"):
            game_path = self._game_audio_path()
            if game_path is not None:
                try:
                    media = _probe_media(game_path, self.ffprobe)
                    if media.has_audio and media.duration_ms > 0:
                        total = max(total, self._game_audio_offset_ms(game_path) + media.duration_ms)
                except RenderError as exc:
                    self.warnings.append(f"无法探测游戏原音时长：{exc}")
        ends = [
            int(result.start_ms or 0)
            + max(
                0,
                result.final_ms if result.final_ms is not None else result.dur_ms,
            )
            for result in results
        ]
        needed = max(ends, default=0)
        if total <= 0:
            # 会话缺少 end 或时长为 0：用最后事件时间兜底。
            total = needed or 1000
        elif needed > total:
            self.warnings.append(
                f"最后一句解说到 {needed}ms，超出会话总时长 {total}ms，"
                f"片尾补 {needed - total}ms 静止画面"
            )
            total = needed
        return max(1000, total)

    # -- 字幕 ------------------------------------------------------------

    def _write_subtitles(self, results: Sequence[SpeechResult]) -> Path:
        """生成带 BOM 的 UTF-8 ASS 字幕。"""

        entries: List[Tuple[SpeechResult, int]] = []
        for result in results:
            # 优先使用最终音频时长（拉伸后），纯字幕模式退回预估时长。
            audio_ms = result.final_ms if result.final_ms is not None else result.dur_ms
            start = int(result.start_ms or 0)
            end_ms = start + max(MIN_SUBTITLE_MS, audio_ms)
            entries.append((result, end_ms))
        document = build_ass(
            entries,
            font=self.args.font,
            width=self.canvas_width,
            height=self.canvas_height,
            title=f"BetterGI Session {self.session.session_id}",
        )
        with self.subtitle_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("\ufeff")
            handle.write(document)
        return self.subtitle_path

    # -- 音轨 ------------------------------------------------------------

    def _mix_audio(self, results: Sequence[SpeechResult], total_ms: int) -> Path:
        """生成语音底、可选游戏原音，并按 ``t`` 叠加；有语音时压低游戏音。"""

        total_s = max(0.001, total_ms / 1000.0)
        command: List[str] = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={AUDIO_RATE}:cl=stereo",
        ]
        lines: List[str] = [
            f"[0:a]aresample={AUDIO_RATE},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"atrim=0:{total_s:.3f},asetpts=PTS-STARTPTS[base]"
        ]
        labels: List[str] = ["[base]"]
        input_index = 0
        for result in results:
            if result.final_audio is None:
                continue
            input_index += 1
            command.extend(["-i", str(result.final_audio)])
            delay_ms = max(0, int(result.start_ms or 0))
            lines.append(
                f"[{input_index}:a]aresample={AUDIO_RATE},"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"volume={max(0.0, float(getattr(result, 'volume', 1.0) or 1.0)):.3f},"
                f"adelay={delay_ms}|{delay_ms}[s{input_index}]"
            )
            labels.append(f"[s{input_index}]")

        has_speech = len(labels) > 1
        if not has_speech:
            lines.append("[base]anull[speech]")
        else:
            lines.append(
                "".join(labels)
                + f"amix=inputs={len(labels)}:normalize=0:duration=longest,"
                + f"atrim=0:{total_s:.3f},asetpts=PTS-STARTPTS[speech]"
            )

        game_path = self._game_audio_path()
        if game_path is not None:
            input_index += 1
            command.extend(["-i", str(game_path)])
            offset_ms = self._game_audio_offset_ms(game_path)
            # 负偏移裁掉会话前的源音频；正偏移在会话开头补静音。
            trim_start = max(0, -offset_ms) / 1000.0
            delay_ms = max(0, offset_ms)
            lines.append(
                f"[{input_index}:a]aresample={AUDIO_RATE},"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"atrim=start={trim_start:.3f},asetpts=PTS-STARTPTS,"
                f"adelay={delay_ms}|{delay_ms}[game]"
            )
            if has_speech:
                # 语音出现时压低游戏原音；无语音时保留原音。
                lines.append("[speech]asplit=2[sp][sc]")
                lines.append(
                    "[game][sc]sidechaincompress="
                    "threshold=0.05:ratio=2:attack=20:release=300[ducked]"
                )
                lines.append(
                    "[ducked][sp]amix=inputs=2:normalize=0:duration=longest,"
                    f"atrim=0:{total_s:.3f},asetpts=PTS-STARTPTS[aout]"
                )
            else:
                # 静音底贯穿整个会话，即使原音提前结束也保留尾部音轨。
                lines.append(
                    "[game][speech]amix=inputs=2:normalize=0:duration=longest,"
                    f"atrim=0:{total_s:.3f},asetpts=PTS-STARTPTS[aout]"
                )
        else:
            lines.append("[speech]anull[aout]")

        with _filter_script(self.audio_dir, "mix-", lines) as script_path:
            command.extend(
                [
                    self.script_option,
                    str(script_path),
                    "-map",
                    "[aout]",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    str(self.mix_path),
                ]
            )
            result = _run(command, timeout=FFMPEG_TIMEOUT_S, description="合成音轨")
        if result.returncode != 0:
            raise RenderError(
                f"合成音轨失败（退出码 {result.returncode}）：{_tail(result.stderr)}"
            )
        if not self.mix_path.is_file() or self.mix_path.stat().st_size <= 0:
            raise RenderError("ffmpeg 未生成音频混音文件")
        return self.mix_path

    # -- 画面 ------------------------------------------------------------

    def _render_video(self, total_ms: int, mix_path: Path, subtitle_path: Path) -> None:
        """合成画面与音轨，输出 H.264 + AAC 成片。"""

        total_s = max(0.001, total_ms / 1000.0)
        command: List[str] = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]

        if self.use_video:
            command.extend(["-i", str(self.session.video_path)])
        else:
            command.extend(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    f"color=c=black:s={int(self.canvas_width)}x{int(self.canvas_height)}"
                    f":r={self.canvas_fps:g}",
                ]
            )
        # 输入 1 永远是混音音轨。
        command.extend(["-i", str(mix_path)])

        avatar_index: Optional[int] = None
        if self.args.avatar_image:
            avatar_index = 2
            command.extend(["-i", str(Path(self.args.avatar_image).resolve())])

        # 基础视频链：补头、补尾、裁剪到会话总时长。
        video_filters: List[str] = []
        if self.use_video:
            if self.video_offset_ms > 0:
                video_filters.append(
                    f"tpad=start_duration={self.video_offset_ms / 1000.0:.3f}:start_mode=clone"
                )
            if self.video_media is not None and self.video_media.duration_ms > 0:
                pad_stop = (
                    total_s
                    - self.video_offset_ms / 1000.0
                    - self.video_media.duration_ms / 1000.0
                )
                if pad_stop > 0.001:
                    video_filters.append(
                        f"tpad=stop_mode=clone:stop_duration={pad_stop:.3f}"
                    )
        video_filters.extend(
            [
                f"trim=duration={total_s:.3f}",
                "setpts=PTS-STARTPTS",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            ]
        )

        lines: List[str] = ["[0:v]" + ",".join(video_filters) + "[v0]"]
        current = "[v0]"
        if avatar_index is not None:
            # 头像缩放到宽 400，叠在右下角，留 24px 边距。
            lines.append(f"[{avatar_index}:v]scale=400:-2[avatar]")
            lines.append(f"{current}[avatar]overlay=W-w-24:H-h-24:format=auto[v1]")
            current = "[v1]"

        if not self.args.no_subtitles:
            lines.append(f"{current}ass='{escape_filter_path(subtitle_path)}'[vout]")
        else:
            lines.append(f"{current}null[vout]")

        with _filter_script(self.session_dir, "video-", lines) as script_path:
            command.extend(
                [
                    self.script_option,
                    str(script_path),
                    "-map",
                    "[vout]",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "20",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(self.out_path),
                ]
            )
            result = _run(command, timeout=FFMPEG_TIMEOUT_S, description="合成成片")
        if result.returncode != 0:
            raise RenderError(
                f"合成成片失败（退出码 {result.returncode}）：{_tail(result.stderr)}"
            )
        if not self.out_path.is_file() or self.out_path.stat().st_size <= 0:
            raise RenderError("ffmpeg 未生成成片文件")

    # -- 报告 ------------------------------------------------------------

    def _relative(self, path: Optional[Path]) -> Optional[str]:
        """把会话内路径转成相对 posix 字符串，便于阅读和迁移。"""

        if path is None:
            return None
        try:
            return path.resolve().relative_to(self.session_dir.resolve()).as_posix()
        except ValueError:
            return str(path)

    def _build_report(
        self,
        results: Sequence[SpeechResult],
        total_ms: int,
        subtitle_path: Path,
        mix_path: Path,
    ) -> Dict[str, Any]:
        """构造 ``render.json`` 内容。"""

        return {
            "session_id": self.session.session_id,
            "session_dir": str(self.session_dir.resolve()),
            "created_at": epoch_ms_to_iso(int(time.time() * 1000)),
            "output": str(self.out_path.resolve()),
            "duration_ms": total_ms,
            "tts": self.effective_tts,
            "requested_tts": self.args.tts,
            "voice": self.args.voice if self.effective_tts == "sapi" else None,
            "rate": int(self.args.rate) if self.effective_tts == "sapi" else None,
            "feiyu": (
                {
                    "style": self.args.feiyu_style,
                    "speed": float(self.args.feiyu_speed),
                    "seed": int(self.args.feiyu_seed),
                    "load_seconds": self.feiyu_load_seconds,
                }
                if self.args.tts in ("auto", "feiyu")
                else None
            ),
            "svc": {
                "model": self.args.svc_model or None,
                "applied": any(item.svc_applied for item in results),
                "command": self.args.svc_cmd if self.args.svc_model else None,
            },
            "subtitles": {
                "path": self._relative(subtitle_path),
                "burned": not self.args.no_subtitles,
                "font": self.args.font,
                "lines": len(results),
            },
            "audio_mix": self._relative(mix_path),
            "game_audio": self._relative(self._game_audio_path()),
            "game_audio_offset_ms": (
                self._game_audio_offset_ms(self._game_audio_path())
                if self._game_audio_path() is not None else None
            ),
            "video": {
                "source": "gameplay" if self.use_video else "black",
                "offset_ms": self.video_offset_ms,
                "width": self.canvas_width,
                "height": self.canvas_height,
                "fps": self.canvas_fps,
            },
            "speeches": [
                {
                    "index": item.index,
                    "t": item.t_ms,
                    "start_ms": item.start_ms,
                    "shift_ms": item.shift_ms,
                    "text": item.text,
                    "source": item.source,
                    "thought_id": item.thought_id,
                    "intent_id": item.intent_id,
                    "generated_at_ms": item.generated_at_ms,
                    "frame_seq": item.frame_seq,
                    "kind": item.kind,
                    "emotion": item.emotion,
                    "dur_ms": item.dur_ms,
                    "base_audio": self._relative(item.base_audio),
                    "svc_audio": self._relative(item.svc_audio),
                    "final_audio": self._relative(item.final_audio),
                    "actual_ms": item.actual_ms,
                    "final_ms": item.final_ms,
                    "tempo_ratio": item.tempo_ratio,
                    "tempo_applied": item.tempo_applied,
                    "svc_applied": item.svc_applied,
                    "volume": item.volume,
                    "pause_before_ms": item.pause_before_ms,
                    "pace": item.pace,
                    "interruptible": item.interruptible,
                    "warnings": list(item.warnings),
                }
                for item in results
            ],
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""

    parser = argparse.ArgumentParser(
        description="离线渲染会话目录：后期 TTS、口型对齐、字幕与成片",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("session_dir", help="会话目录（包含 session.jsonl）")
    parser.add_argument(
        "--out", default="final.mp4", help="成片路径；相对路径基于会话目录"
    )
    parser.add_argument(
        "--tts",
        choices=["auto", "feiyu", "sapi", "none"],
        default="auto",
        help="配音方式：auto = 有飞鱼环境时用飞鱼 cute，否则 SAPI；feiyu；sapi；none",
    )
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="SAPI 语音名称")
    parser.add_argument("--rate", type=int, default=0, help="SAPI 语速（-10 到 10）")
    parser.add_argument(
        "--game-audio", default="",
        help="游戏原音文件（相对会话目录）；留空时自动查找 audio/game_audio.*",
    )
    parser.add_argument(
        "--no-game-audio", action="store_true",
        help="不混入游戏原音，只输出 AI 语音",
    )
    parser.add_argument(
        "--feiyu-python", default=DEFAULT_FEIYU_PYTHON, help="飞鱼 TTS 解释器"
    )
    parser.add_argument(
        "--feiyu-script", default=DEFAULT_FEIYU_SCRIPT, help="飞鱼 TTS 脚本"
    )
    parser.add_argument(
        "--feiyu-model-root",
        default=DEFAULT_FEIYU_MODEL_ROOT,
        help="飞鱼模型根目录（含 models/ 与 references/）",
    )
    parser.add_argument(
        "--feiyu-style",
        choices=["cute", "gentle"],
        default="cute",
        help="飞鱼音色：cute = F6 可爱；gentle = C3 温柔",
    )
    parser.add_argument("--feiyu-speed", type=float, default=1.0, help="飞鱼语速")
    parser.add_argument("--feiyu-seed", type=int, default=42, help="飞鱼随机种子")
    parser.add_argument(
        "--feiyu-fp16",
        action="store_true",
        help="飞鱼模型使用 fp16；显存不足时再开",
    )
    parser.add_argument(
        "--svc-model", default="", help="SVC 模型目录；留空表示不做音色转换"
    )
    parser.add_argument(
        "--svc-cmd",
        default=DEFAULT_SVC_CMD,
        help="SVC 命令模板，支持 {python}/{input}/{model}/{output} 占位符",
    )
    parser.add_argument(
        "--svc-python",
        default=DEFAULT_SVC_PYTHON,
        help="SVC 使用的 Python 解释器（填入模板的 {python}）",
    )
    parser.add_argument("--avatar-image", default="", help="可选头像 PNG，叠在右下角")
    parser.add_argument(
        "--no-subtitles", action="store_true", help="生成字幕但不烧录到画面"
    )
    parser.add_argument(
        "--allow-tts-fallback",
        action="store_true",
        help="SVC 或飞鱼失败时回退到 SAPI 基础音频，而不是整体报错退出",
    )
    parser.add_argument("--font", default=DEFAULT_FONT, help="ASS 字幕字体")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI 入口。"""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        renderer = SessionRenderer(args)
        report = renderer.render()
    except RenderError as exc:
        print(f"渲染失败：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消渲染", file=sys.stderr)
        return 130

    print(
        f"成片已生成：{report['output']}（{report['duration_ms'] / 1000.0:.2f}s，"
        f"{len(report['speeches'])} 条解说）",
        flush=True,
    )
    print(
        f"字幕：{report['subtitles']['path']}；报告：render.json",
        flush=True,
    )
    if report["warnings"]:
        print(f"共 {len(report['warnings'])} 条警告，详见 render.json", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
