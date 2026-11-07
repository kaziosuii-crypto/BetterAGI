"""WindowsSapiProvider（本机 System.Speech 离线合成）特有行为：配置默认值、命令构造、临时文件生命周期、非零退出/超时/取消的异常路径

真实声音不发出：PowerShell 子进程与 soundfile 读取全部用替身，声卡管理器也是
替身；涉及 setup 的用例把平台与 powershell 探测替身化，测试机不依赖 Windows。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from src.modules.events.event_bus import EventBus
from src.modules.events.names import CoreEvents
from src.modules.tts.backends.windows_sapi import provider as provider_module
from src.modules.tts.backends.windows_sapi.provider import WindowsSapiProvider
from tests.modules.tts.helpers import make_audio_manager, record_lifecycle, setup_with_audio_manager


def _provider(**config: Any) -> WindowsSapiProvider:
    return WindowsSapiProvider(config={"type": "windows_sapi", **config})


def _fake_process(*, returncode: int | None = None, stderr: bytes = b"") -> MagicMock:
    process = MagicMock()
    process.returncode = returncode
    process.communicate = AsyncMock(return_value=(b"", stderr))
    process.wait = AsyncMock()
    process.kill = MagicMock()
    return process


def _install_windows_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """setup 的平台与 powershell 探测替身化：只验证顺序与结果，不依赖宿主环境。"""
    monkeypatch.setattr(provider_module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        provider_module, "shutil", SimpleNamespace(which=lambda _name: "powershell.exe")
    )


def _install_soundfile(monkeypatch: pytest.MonkeyPatch, samplerate: int = 16000, samples: int = 1600) -> None:
    """soundfile 读取替身：返回固定 PCM，不解析真实 WAV。"""
    monkeypatch.setattr(
        provider_module,
        "sf",
        SimpleNamespace(read=lambda _path, dtype=None: (np.zeros(samples, dtype=np.float32), samplerate)),
    )


def test_config_defaults_and_overrides() -> None:
    default = _provider()
    assert (default.voice, default.rate, default.volume, default.typed_config.synth_timeout_ms) == (
        "Microsoft Huihui Desktop",
        0,
        100,
        15000,
    )

    custom = _provider(voice="Microsoft Yaoyao Desktop", rate=3, volume=80, synth_timeout_ms=5000)
    assert (custom.voice, custom.rate, custom.volume, custom.synth_timeout) == (
        "Microsoft Yaoyao Desktop",
        3,
        80,
        5.0,
    )


async def test_setup_rejects_non_windows_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_module, "sys", SimpleNamespace(platform="linux"))
    with pytest.raises(RuntimeError, match="Windows"):
        await _provider().setup()


async def test_setup_reports_missing_powershell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(provider_module, "shutil", SimpleNamespace(which=lambda _name: None))
    with pytest.raises(RuntimeError, match="powershell"):
        await _provider().setup()


async def test_setup_is_idempotent_and_initializes_audio_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_windows_environment(monkeypatch)
    provider = _provider()
    mgr = await setup_with_audio_manager(provider)
    assert provider._has_started and provider._is_connected
    assert provider.audio_manager is mgr

    await provider.setup()  # 二次调用早退，不重复初始化
    assert provider.audio_manager is mgr


def test_command_and_environment_carry_voice_rate_and_volume_without_shell_quoting() -> None:
    provider = _provider(voice="Microsoft Huihui Desktop", rate=-2, volume=90)
    script = Path("C:/tmp/work/sapi_synth.ps1")
    text = Path("C:/tmp/work/speech.txt")
    wav = Path("C:/tmp/work/speech.wav")

    command = provider._build_command("powershell.exe", script, text, wav)
    assert command[0] == "powershell.exe"
    assert "-NoProfile" in command and "-NonInteractive" in command
    assert command[command.index("-File") + 1] == str(script)
    assert command[command.index("-TextPath") + 1] == str(text)
    assert command[command.index("-OutputPath") + 1] == str(wav)

    env = provider._build_env()
    assert (env["AMAIDESU_SAPI_VOICE"], env["AMAIDESU_SAPI_RATE"], env["AMAIDESU_SAPI_VOLUME"]) == (
        "Microsoft Huihui Desktop",
        "-2",
        "90",
    )


async def test_synthesis_writes_text_through_file_and_cleans_temp_workdir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """台词经临时文件交给 PowerShell（不做 shell 拼接）；合成结束临时目录整体回收。"""
    provider = _provider()
    provider._powershell_path = "powershell.exe"
    seen: dict[str, Any] = {}

    async def fake_exec(*argv: str, **kwargs: Any) -> MagicMock:
        text_path = Path(argv[argv.index("-TextPath") + 1])
        wav_path = Path(argv[argv.index("-OutputPath") + 1])
        script_path = Path(argv[argv.index("-File") + 1])
        seen["text"] = text_path.read_text(encoding="utf-8")
        seen["paths"] = (script_path, text_path, wav_path)
        seen["env"] = kwargs["env"]
        wav_path.write_bytes(b"RIFF-fake")
        return _fake_process(returncode=0)

    monkeypatch.setattr(provider_module.asyncio, "create_subprocess_exec", fake_exec)
    _install_soundfile(monkeypatch, samplerate=16000, samples=1600)

    audio, samplerate = await provider._synthesize_audio("你好～(鼓掌)")

    assert seen["text"] == "你好～(鼓掌)"  # 原文透传，脚本内按 UTF-8 读取
    assert seen["env"]["AMAIDESU_SAPI_VOICE"] == "Microsoft Huihui Desktop"
    assert (samplerate, audio.shape, audio.dtype) == (16000, (1600,), np.float32)
    assert not any(path.exists() for path in seen["paths"]), "临时目录应在合成结束后整体清理"


async def test_nonzero_exit_raises_with_stderr_detail(monkeypatch: pytest.MonkeyPatch, loguru_capture) -> None:
    provider = _provider()
    provider._powershell_path = "powershell.exe"
    process = _fake_process(returncode=3, stderr="Cannot find voice Microsoft Huihui Desktop".encode())

    async def fake_exec(*_argv: str, **_kwargs: Any) -> MagicMock:
        return process

    monkeypatch.setattr(provider_module.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="退出码 3.*Cannot find voice"):
        await provider._synthesize_audio("你好")
    assert any(r["level"] == "ERROR" and "退出码 3" in r["message"] for r in loguru_capture.records)


async def test_timeout_terminates_process_and_raises(monkeypatch: pytest.MonkeyPatch, loguru_capture) -> None:
    provider = _provider(synth_timeout_ms=1000)
    provider._powershell_path = "powershell.exe"
    process = _fake_process()
    process.communicate = AsyncMock(side_effect=asyncio.TimeoutError())

    async def fake_exec(*_argv: str, **_kwargs: Any) -> MagicMock:
        return process

    monkeypatch.setattr(provider_module.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="超时"):
        await provider._synthesize_audio("你好")
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()
    assert any("超时" in r["message"] for r in loguru_capture.records)


async def test_cancellation_terminates_process_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider()
    provider._powershell_path = "powershell.exe"
    process = _fake_process()
    process.communicate = AsyncMock(side_effect=asyncio.CancelledError())

    async def fake_exec(*_argv: str, **_kwargs: Any) -> MagicMock:
        return process

    monkeypatch.setattr(provider_module.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(asyncio.CancelledError):
        await provider._synthesize_audio("你好")
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


async def test_missing_wav_output_is_a_synthesis_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider()
    provider._powershell_path = "powershell.exe"

    async def fake_exec(*_argv: str, **_kwargs: Any) -> MagicMock:
        return _fake_process(returncode=0)  # 退出码 0 但没有写 WAV

    monkeypatch.setattr(provider_module.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="WAV"):
        await provider._synthesize_audio("你好")


async def test_handle_speech_plays_pcm_and_emits_lifecycle(
    monkeypatch: pytest.MonkeyPatch, event_bus: EventBus
) -> None:
    _install_windows_environment(monkeypatch)
    provider = WindowsSapiProvider(config={"type": "windows_sapi"}, event_bus=event_bus)
    mgr = await setup_with_audio_manager(provider, make_audio_manager())
    synth = AsyncMock(return_value=(np.zeros(16000, dtype=np.float32), 16000))
    monkeypatch.setattr(provider, "_synthesize_audio", synth)
    captured = record_lifecycle(event_bus)

    await provider.handle_speech("大家好～(鼓掌)", utterance_id="utt_1")
    await event_bus.cleanup()

    assert [name for name, _ in captured] == [CoreEvents.TTS_UTTERANCE_STARTED, CoreEvents.TTS_UTTERANCE_FINISHED]
    started, finished = (payload for _, payload in captured)
    assert (started.speech_text, started.engine, started.duration_ms) == ("大家好～(鼓掌)", "windows_sapi", 1000)
    assert (finished.engine, finished.duration_ms) == ("windows_sapi", 1000)
    assert "～" not in synth.call_args.args[0]  # 送合成的是规范化文本
    mgr.play_audio.assert_awaited_once()
    assert (provider.render_count, provider.error_count) == (1, 0)


async def test_blank_text_skips_synthesis(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_windows_environment(monkeypatch)
    provider = _provider()
    await setup_with_audio_manager(provider)
    synth = AsyncMock()
    monkeypatch.setattr(provider, "_synthesize_audio", synth)

    await provider.handle_speech("   \n")

    synth.assert_not_awaited()
    assert provider.render_count == 0
