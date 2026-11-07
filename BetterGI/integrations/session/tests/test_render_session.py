# -*- coding: utf-8 -*-
"""``render_session.py`` 的集成测试。

运行方式（Amaidesu 的虚拟环境已安装 pytest）::

    E:\\BetterAGI\\Amaidesu\\.venv\\Scripts\\python.exe -m pytest tests/test_render_session.py -q

测试自建一个 4 秒的合成会话（2 条 speech、1 条 thought、若干 avatar），
再用 ffmpeg ``testsrc`` 生成 4 秒 ``gameplay.mp4``，然后实际调用渲染器。
缺少 ffmpeg/ffprobe 时整个模块跳过；SAPI 需要 Windows 中文语音。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

# 让测试无需安装包就能 import 同目录的会话格式模块。
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import session_format  # noqa: E402  （必须在 sys.path 调整之后导入）
import render_session  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
RENDER = ROOT / "render_session.py"

pytestmark = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="需要 ffmpeg 与 ffprobe 才能进行集成测试"
)

SPEECH_ONE = "你好，这里是测试解说。"
SPEECH_TWO = "画面正在录制中。"
SESSION_DURATION_MS = 4000


def _make_session(directory: Path) -> Path:
    """生成一个 4 秒的合成会话目录。"""

    directory.mkdir(parents=True, exist_ok=True)
    # 注意：close 之后 __exit__ 会再次 close，SessionWriter 内部做了幂等处理。
    with session_format.SessionWriter(
        directory,
        session_id="test-render",
        t0_unix_ms=1_700_000_000_000,
        video={"path": "gameplay.mp4", "fps": 30, "width": 640, "height": 360},
    ) as writer:
        writer.emit_thought(200, "先观察一下环境。")
        writer.emit_avatar(300, "pose", params={"ParamAngleX": 1.5, "ParamBreath": 0.5})
        writer.emit_video_start(0, "gameplay.mp4", width=640, height=360, fps=30)
        writer.emit_speech(
            400,
            SPEECH_ONE,
            kind="reaction",
            emotion="happy",
            dur_ms=1600,
        )
        writer.emit_avatar(900, "blink", dur=120)
        writer.emit_speech(
            2000,
            SPEECH_TWO,
            kind="plan",
            emotion="calm",
            dur_ms=1700,
        )
        writer.emit_avatar(2600, "pose", params={"ParamAngleZ": -2.0})
        writer.close(
            duration_ms=SESSION_DURATION_MS, counts={"speeches": 2, "thoughts": 1}
        )
    return directory


def _make_video(directory: Path) -> Path:
    """用 ffmpeg testsrc 生成 4 秒测试画面。"""

    path = directory / "gameplay.mp4"
    subprocess.run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=4:size=640x360:rate=30",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        timeout=120,
    )
    return path


def _run_render(session_dir: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    """调用渲染器 CLI 并捕获输出（强制 UTF-8，避免 Windows 管道乱码）。"""

    command: List[str] = [sys.executable, str(RENDER), str(session_dir), *extra]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        env=env,
    )


def _probe_duration(path: Path) -> float:
    """读取媒体时长（秒）。"""

    result = subprocess.run(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return float(result.stdout.strip())


def _probe_streams(path: Path) -> dict:
    """读取流信息（编码、类型）。"""

    result = subprocess.run(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout or "{}")


def test_render_with_sapi_and_video(tmp_path: Path) -> None:
    """SAPI 配音 + gameplay 画面：产出成片、字幕与报告。"""

    session_dir = _make_session(tmp_path / "session")
    _make_video(session_dir)

    result = _run_render(session_dir, "--tts", "sapi")
    assert result.returncode == 0, (
        f"渲染失败：\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    final = session_dir / "final.mp4"
    subtitle = session_dir / "subtitles.ass"
    report_path = session_dir / "render.json"
    assert final.is_file()
    assert subtitle.is_file()
    assert report_path.is_file()

    # 成片时长以 4 秒会话为准（编码容器会有少量偏差）。
    duration = _probe_duration(final)
    assert 3.5 <= duration <= 6.0, f"成片时长异常：{duration}s"

    # 字幕包含两句台词，且每条 speech 一条 Dialogue。
    subtitle_text = subtitle.read_text(encoding="utf-8-sig")
    assert SPEECH_ONE in subtitle_text
    assert SPEECH_TWO in subtitle_text
    assert subtitle_text.count("Dialogue:") == 2

    # 流信息：H.264 视频 + AAC 音频。
    streams = _probe_streams(final).get("streams") or []
    codecs = {stream.get("codec_name") for stream in streams}
    assert "h264" in codecs
    assert "aac" in codecs

    # 报告记录每条解说的音频与拉伸信息。
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["session_id"] == "test-render"
    assert len(report["speeches"]) == 2
    for speech in report["speeches"]:
        assert speech["final_audio"], "SAPI 模式必须记录最终音频"
        assert (session_dir / speech["final_audio"]).is_file()
        assert speech["actual_ms"] and speech["actual_ms"] > 0

    # 确认不是静音：volumedetect 的平均音量应明显高于数字静音。
    volume = subprocess.run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-i",
            str(final),
            "-map",
            "0:a:0",
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    match = re.search(r"mean_volume:\s*(-?[\d.]+) dB", volume.stderr)
    assert match, f"volumedetect 未输出音量：{volume.stderr[-500:]}"
    assert float(match.group(1)) > -60.0, f"音轨疑似静音：{match.group(1)} dB"


def test_render_without_tts(tmp_path: Path) -> None:
    """纯字幕模式：成功产出静音成片与字幕。"""

    session_dir = _make_session(tmp_path / "session")
    _make_video(session_dir)

    result = _run_render(session_dir, "--tts", "none")
    assert result.returncode == 0, (
        f"渲染失败：\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    final = session_dir / "final.mp4"
    subtitle = session_dir / "subtitles.ass"
    report_path = session_dir / "render.json"
    assert final.is_file()
    assert subtitle.is_file()
    assert report_path.is_file()

    duration = _probe_duration(final)
    assert 3.5 <= duration <= 6.0, f"成片时长异常：{duration}s"

    subtitle_text = subtitle.read_text(encoding="utf-8-sig")
    assert SPEECH_ONE in subtitle_text
    assert SPEECH_TWO in subtitle_text

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["tts"] == "none"
    assert all(speech["final_audio"] is None for speech in report["speeches"])

    # 即使没有配音，成片也保留一条静音 AAC 音轨。
    streams = _probe_streams(final).get("streams") or []
    assert any(stream.get("codec_name") == "aac" for stream in streams)


def test_build_svc_command_includes_model_config(tmp_path: Path) -> None:
    """SVC 命令必须把模型目录与同目录 config.json 一起传给 infer。"""

    import render_session

    command = render_session.build_svc_command(
        render_session.DEFAULT_SVC_CMD,
        python=r"E:\BetterAGI\svc\.venv\Scripts\python.exe",
        input_path=tmp_path / "in.wav",
        model=r"E:\BetterAGI\svc\models\demo",
        output_path=tmp_path / "out.wav",
    )
    assert command[0].endswith("python.exe")
    assert "so_vits_svc_fork" in command
    assert "infer" in command
    assert "-c" in command
    config_index = command.index("-c")
    assert (
        command[config_index + 1].replace("\\", "/")
        == "E:/BetterAGI/svc/models/demo/config.json"
    )


FAKE_FEIYU = r"""
import argparse, json, math, struct, wave
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--request")
parser.add_argument("--result")
parser.add_argument("--style", default="cute")
parser.add_argument("--speed", type=float, default=1.0)
parser.add_argument("--seed", type=int, default=42)
parser.add_argument("--fp16", action="store_true")
parser.add_argument("--model-root")
args = parser.parse_args()
request = json.loads(Path(args.request).read_text(encoding="utf-8-sig"))
results = []
for item in request["texts"]:
    path = Path(request["output_dir"]) / f"tts-{int(item['index']):03d}.wav"
    rate = 24000
    frames = int(rate * 0.8)
    data = b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(frames)
    )
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(data)
    results.append({"index": int(item["index"]), "path": str(path), "seconds": 0.8, "peak": 0.24, "elapsed": 0.01})
Path(args.result).write_text(json.dumps({"ok": True, "style": args.style, "sample_rate": 24000, "load_seconds": 0.1, "results": results}, ensure_ascii=False), encoding="utf-8")
"""


def test_feiyu_tts_batch_integration(tmp_path: Path) -> None:
    """飞鱼 TTS 走批量接口：一次调用生成全部台词，再进入对齐与混流。"""

    session_dir = _make_session(tmp_path / "session-feiyu")
    _make_video(session_dir)
    fake_worker = tmp_path / "fake_feiyu.py"
    fake_worker.write_text(FAKE_FEIYU, encoding="utf-8")
    fake_model = tmp_path / "fake_model"
    (fake_model / "models" / "Fun-CosyVoice3-0.5B-2512").mkdir(parents=True)
    (fake_model / "models" / "Fun-CosyVoice3-0.5B-2512" / "cosyvoice3.yaml").write_text(
        "", encoding="utf-8"
    )
    # 不传 --tts：默认 auto 应检测到飞鱼环境并选 cute/gentle 批量接口。
    completed = _run_render(
        session_dir,
        "--feiyu-python",
        sys.executable,
        "--feiyu-script",
        str(fake_worker),
        "--feiyu-model-root",
        str(fake_model),
        "--feiyu-style",
        "gentle",
        "--out",
        "final_feiyu.mp4",
    )
    assert completed.returncode == 0, completed.stderr
    output = session_dir / "final_feiyu.mp4"
    assert output.is_file() and output.stat().st_size > 0
    report = json.loads((session_dir / "render.json").read_text(encoding="utf-8"))
    assert report["requested_tts"] == "auto"
    assert report["tts"] == "feiyu"
    assert report["feiyu"]["style"] == "gentle"
    assert report["feiyu"]["load_seconds"] == 0.1
    assert all(item["base_audio"] for item in report["speeches"])
    assert len(report["speeches"]) == 2


def test_auto_tts_falls_back_to_sapi_without_feiyu(tmp_path: Path) -> None:
    """飞鱼脚本缺失时，默认 auto 应回退 SAPI，而不是报错。"""

    session_dir = _make_session(tmp_path / "session-auto")
    _make_video(session_dir)
    completed = _run_render(
        session_dir,
        "--feiyu-python",
        sys.executable,
        "--feiyu-script",
        str(tmp_path / "missing_feiyu.py"),
        "--feiyu-model-root",
        str(tmp_path / "missing_model"),
        "--out",
        "final_auto.mp4",
    )
    assert completed.returncode == 0, completed.stderr
    assert (session_dir / "final_auto.mp4").is_file()
    report = json.loads((session_dir / "render.json").read_text(encoding="utf-8"))
    assert report["requested_tts"] == "auto"
    assert report["tts"] == "sapi"


def test_render_report_preserves_player_speech_provenance(tmp_path: Path) -> None:
    directory = tmp_path / "player-session"
    with session_format.SessionWriter(directory, session_id="player", t0_unix_ms=1000) as writer:
        writer.emit_speech(400, "我们去看看。", source="persistent_player", generated_at_ms=1400,
                           thought_id="thought-1", intent_id="intent-1", frame_seq=7, dur_ms=500)
        writer.close(duration_ms=1500, counts={"speeches": 1})
    result = _run_render(directory, "--tts", "none", "--no-subtitles", "--out", "player.mp4")
    assert result.returncode == 0, result.stderr
    speech = json.loads((directory / "render.json").read_text(encoding="utf-8"))["speeches"][0]
    assert speech["text"] == "我们去看看。"
    assert (speech["source"], speech["thought_id"], speech["intent_id"], speech["generated_at_ms"], speech["frame_seq"]) == (
        "persistent_player", "thought-1", "intent-1", 1400, 7)


def test_schedule_respects_pause_before_speech() -> None:
    class FakeRenderer:
        warnings = []

    result = render_session.SpeechResult(
        index=1, t_ms=100, text="x", kind="reaction", emotion="calm",
        dur_ms=500, start_ms=100, pause_before_ms=300,
    )
    render_session.SessionRenderer._schedule(FakeRenderer(), [result])
    assert result.start_ms == 400


def _make_tone(path: Path, duration: float = 1.0) -> None:
    """生成可测的恒定正弦音频，用来验证成片中真正的声音位置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
        "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}",
        "-c:a", "pcm_s16le", str(path),
    ], check=True, timeout=60)


def _audio_level(path: Path, start: float, duration: float = 0.15) -> float:
    """解码指定区间并计算 RMS，允许 AAC 编码边界附近有少量泄漏。"""
    import array
    import math
    result = subprocess.run([
        str(FFMPEG), "-hide_banner", "-loglevel", "error", "-ss", str(start),
        "-i", str(path), "-t", str(duration), "-map", "0:a:0", "-ac", "1",
        "-ar", "48000", "-f", "s16le", "-",
    ], capture_output=True, check=True, timeout=60)
    samples = array.array("h", result.stdout)
    assert samples, f"音轨在 {start}s 已结束"
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


@pytest.mark.parametrize("event_offset,explicit,expected_start", [
    (700, False, 0.7),
    (700, True, 0.0),  # 显式选择其他文件，不继承录制音轨的偏移
    (None, False, 0.0),  # 无 audio 事件的旧会话
    (-300, False, 0.0),  # 负偏移裁掉开头 300ms
])
def test_game_audio_timeline_real_ffmpeg(tmp_path, event_offset, explicit, expected_start):
    """检查成片音频起始、原音结束后的静音及输出长度。"""
    directory = tmp_path / "session"
    with session_format.SessionWriter(directory, session_id="audio", t0_unix_ms=1000) as writer:
        if event_offset is not None:
            writer.emit(max(0, event_offset), "audio", path="audio/game_audio.wav",
                        start_offset_ms=event_offset)
        writer.close(duration_ms=3000)
    _make_tone(directory / "audio" / "game_audio.wav")
    extra = []
    if explicit:
        _make_tone(directory / "other.wav")
        extra = ["--game-audio", "other.wav"]
    result = _run_render(directory, "--tts", "none", "--no-subtitles", *extra)
    assert result.returncode == 0, result.stderr
    final = directory / "final.mp4"
    report = json.loads((directory / "render.json").read_text(encoding="utf-8"))
    assert report["game_audio_offset_ms"] == (event_offset if not explicit and event_offset is not None else 0)
    assert 2.95 <= _probe_duration(final) <= 3.15
    if expected_start:
        assert _audio_level(final, 0.2) < 20
    assert _audio_level(final, expected_start + 0.2) > 500
    end = expected_start + (0.7 if event_offset == -300 and not explicit else 1.0)
    assert _audio_level(final, end + 0.2) < 20
    assert _audio_level(final, 2.7) < 20


def test_game_audio_with_speech_is_delayed_and_trimmed(tmp_path):
    """含配音的混音路径同样按事件延时，并裁剪到会话长度。"""
    directory = tmp_path / "session"
    with session_format.SessionWriter(directory, session_id="audio", t0_unix_ms=1000) as writer:
        writer.emit(600, "audio", path="audio/game_audio.wav")
        writer.close(duration_ms=3000)
    _make_tone(directory / "audio" / "game_audio.wav", duration=4.0)
    _make_tone(directory / "speech.wav", duration=0.4)
    args = render_session.build_parser().parse_args([str(directory), "--tts", "none"])
    renderer = render_session.SessionRenderer(args)
    renderer.script_option = render_session._filter_script_option(renderer.ffmpeg)
    speech = render_session.SpeechResult(index=1, t_ms=1800, start_ms=1800,
        text="x", kind="reaction", emotion="calm", dur_ms=400,
        final_audio=directory / "speech.wav")
    mixed = renderer._mix_audio([speech], 3000)
    assert 2.95 <= _probe_duration(mixed) <= 3.05
    assert _audio_level(mixed, 0.2) < 20
    assert _audio_level(mixed, 0.9) > 500
    assert _audio_level(mixed, 2.8) > 500


def test_audio_offset_matches_selected_path_only(tmp_path):
    directory = tmp_path / "session"
    with session_format.SessionWriter(directory, session_id="audio", t0_unix_ms=1000) as writer:
        writer.emit(800, "audio", path="audio/game_audio.wav")  # 旧事件只有 t
        writer.close(duration_ms=3000)
    _make_tone(directory / "audio" / "game_audio.wav")
    args = render_session.build_parser().parse_args([str(directory), "--tts", "none"])
    renderer = render_session.SessionRenderer(args)
    path = directory / "audio" / "game_audio.wav"
    assert renderer._game_audio_offset_ms(path) == 800
    args.game_audio = str(path)
    assert renderer._game_audio_offset_ms(renderer._game_audio_path()) == 800
    assert renderer._game_audio_offset_ms(directory / "other.wav") == 0


def test_interrupted_session_recovers_audio_duration(tmp_path):
    directory = tmp_path / "session"
    writer = session_format.SessionWriter(directory, session_id="audio", t0_unix_ms=1000)
    writer.emit(700, "audio", path="audio/game_audio.wav")
    writer._file.close()  # 模拟进程异常退出，文件没有 end 行
    _make_tone(directory / "audio" / "game_audio.wav", duration=2.0)
    result = _run_render(directory, "--tts", "none", "--no-subtitles")
    assert result.returncode == 0, result.stderr
    report = json.loads((directory / "render.json").read_text(encoding="utf-8"))
    assert report["duration_ms"] == 2700
    assert any("缺少 end" in warning for warning in report["warnings"])
    assert _audio_level(directory / "final.mp4", 2.3) > 500


def test_game_audio_auto_detection(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "game_audio.wav").write_bytes(b"x")
    args = type("Args", (), {"no_game_audio": False, "game_audio": ""})()
    fake = type("R", (), {"args": args, "session_dir": tmp_path, "audio_dir": audio_dir})()
    assert render_session.SessionRenderer._game_audio_path(fake) == audio_dir / "game_audio.wav"
    args.no_game_audio = True
    assert render_session.SessionRenderer._game_audio_path(fake) is None


def test_pace_factor_maps_speech_prosody() -> None:
    assert render_session._pace_factor({"pace": "slow"}) < 1.0
    assert render_session._pace_factor({"pace": "fast"}) > 1.0
    assert render_session._pace_factor({"pace": "normal"}) == 1.0
    assert render_session._pace_factor({}) == 1.0
