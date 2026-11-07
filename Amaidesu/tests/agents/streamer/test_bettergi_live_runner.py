"""独立运行器：不出 Agent、不碰 LLM，用替身 TTS / 字幕 / 虚拟形象 / WebSocket 验证装配与降级。

端到端用例起真实本地 WebSocket 替身服务（127.0.0.1 随机端口），经运行器的
``build_output_stack`` 装配出声道，断言"解说 → 语音/字幕/虚拟形象情绪"三条
链路，以及任一下游不可用时的降级。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

import src.bettergi_live_runner as runner
from src.agents.streamer.bettergi_live import TOKEN_ENV_VAR
from src.modules.events.names import CoreEvents
from src.modules.events.payloads.speech import StreamerSpeechPayload
from src.modules.tools.models import ToolExecutionResult, ToolInvocation, ToolSpec
from src.modules.tools.provider import BaseToolProvider
from tests.agents.streamer.helpers import FakeBetterGIServer


class _FakeTTS:
    """TTS 引擎替身：记录被要求播出的台词。"""

    def __init__(self) -> None:
        self.calls: List[str] = []
        self.cleanup_called = False

    async def handle_speech(self, text: str, utterance_id: Any = None) -> None:
        self.calls.append(text)

    async def cleanup(self) -> None:
        self.cleanup_called = True


class _FakeSubtitle:
    """字幕服务替身：记录直出字幕。"""

    def __init__(self) -> None:
        self.shown: List[str] = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def show(self, text: str, utterance_id: Optional[str] = None) -> None:
        self.shown.append(text)

    async def clear(self) -> None:
        pass


class _FakeAvatar(BaseToolProvider):
    """虚拟形象替身：setup 时订阅 streamer.speech（与真实 Provider 的被动半同形）。"""

    name = "vts"
    category = "avatar"

    def __init__(self, event_bus: Any, *, fail_setup: bool = False) -> None:
        self._bus = event_bus
        self._fail_setup = fail_setup
        self.emotions: List[tuple[str, float]] = []
        self.setup_called = False
        self.cleanup_called = False

    def list_tools(self) -> List[ToolSpec]:
        return []

    async def invoke(self, invocation: ToolInvocation) -> ToolExecutionResult:
        return ToolExecutionResult(tool_name=invocation.tool_name, success=False)

    async def setup(self) -> None:
        if self._fail_setup:
            raise ConnectionError("VTS 未启动")
        self.setup_called = True
        self._bus.on(CoreEvents.STREAMER_SPEECH, self._on_speech, model_class=StreamerSpeechPayload)

    async def _on_speech(self, event_name: str, payload: StreamerSpeechPayload, source: str = "") -> None:
        self.emotions.append((payload.emotion, payload.emotion_intensity))

    async def cleanup(self) -> None:
        self.cleanup_called = True


async def _until(condition: Any, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待条件超时")


def _config(url: str, *, tts_enabled: bool) -> Dict[str, Any]:
    return {
        "tts": {"enabled": tts_enabled, "provider": "edge_tts", "max_queue": 3},
        "subtitle": {"enabled": True},
        "lipsync": {"enabled": False},
        "tools": {},
        "platform": {"enabled": []},
        "agents": {"streamer": {"bettergi_live": {"enabled": False, "url": url, "queue_max": 5}}},
    }


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tts: Optional[_FakeTTS],
    subtitle: _FakeSubtitle,
    avatar_holder: Dict[str, _FakeAvatar],
    fail_avatar_setup: bool = False,
) -> Dict[str, Any]:
    """把运行器依赖的装配入口替换为替身；返回收集到的调用参数。"""
    captured: Dict[str, Any] = {}

    def fake_build_tts(config: Any, event_bus: Any = None, audio_sink: Any = None, llm_service: Any = None) -> Any:
        captured["tts"] = {"event_bus": event_bus, "audio_sink": audio_sink, "llm_service": llm_service}
        return tts

    def fake_build_subtitle(config: Any) -> _FakeSubtitle:
        captured["subtitle_config"] = config
        return subtitle

    def fake_bind_core_tools(
        registry: Any,
        config: Any = None,
        event_bus: Any = None,
        lipsync_analyzer: Any = None,
        avatar_platform: Any = None,
        subtitle_service: Any = None,
    ) -> Dict[str, int]:
        provider = _FakeAvatar(event_bus, fail_setup=fail_avatar_setup)
        avatar_holder["provider"] = provider
        registry.register_provider(provider)
        return {"vts": 1}

    monkeypatch.setattr(runner, "build_tts_infrastructure", fake_build_tts)
    monkeypatch.setattr(runner, "build_subtitle_infrastructure", fake_build_subtitle)
    monkeypatch.setattr(runner, "bind_core_tools", fake_bind_core_tools)
    monkeypatch.setattr(runner, "build_lipsync_analyzer", lambda config: None)
    return captured


def test_runner_config_forces_enabled_and_keeps_knobs() -> None:
    """独立运行器以进程启动为开关：配置段的 enabled 只控制主程序内嵌接入。"""
    cfg = runner.bettergi_live_config(
        {"agents": {"streamer": {"bettergi_live": {"enabled": False, "url": "ws://127.0.0.1:9/x", "queue_max": 7}}}}
    )
    assert cfg.enabled is True and cfg.url == "ws://127.0.0.1:9/x" and cfg.queue_max == 7

    assert runner.bettergi_live_config({}).url == "ws://127.0.0.1:30648/ws/v1/agent/live"


async def test_stack_without_tts_shows_subtitles_and_reflects_emotion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, "test-token")
    subtitle = _FakeSubtitle()
    avatars: Dict[str, _FakeAvatar] = {}
    captured = _install_fakes(monkeypatch, tts=None, subtitle=subtitle, avatar_holder=avatars)

    async with FakeBetterGIServer() as server:
        stack = runner.build_output_stack(_config(server.url, tts_enabled=False))
        assert stack.tts_engine is None and stack.subtitle_follows_playback is False
        await stack.start()
        try:
            await _until(lambda: bool(subtitle.shown) and bool(avatars["provider"].emotions))
        finally:
            await stack.stop()

        assert server.received == []  # 只读一方向
    assert captured["tts"]["llm_service"] is None  # 不接语言模型
    assert subtitle.shown == ["先贴着左边走"]
    assert avatars["provider"].emotions == [("neutral", 0.5)]  # calm → neutral


async def test_stack_with_tts_feeds_engine_and_avatar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, "test-token")
    tts = _FakeTTS()
    subtitle = _FakeSubtitle()
    avatars: Dict[str, _FakeAvatar] = {}
    _install_fakes(monkeypatch, tts=tts, subtitle=subtitle, avatar_holder=avatars)

    async with FakeBetterGIServer() as server:
        stack = runner.build_output_stack(_config(server.url, tts_enabled=True))
        assert stack.subtitle_follows_playback is True
        await stack.start()
        try:
            await _until(lambda: bool(tts.calls) and bool(avatars["provider"].emotions))
        finally:
            await stack.stop()

    assert tts.calls == ["先贴着左边走"]
    assert avatars["provider"].emotions == [("neutral", 0.5)]
    assert subtitle.shown == []  # TTS 在位时字幕跟随播放事件，不直出
    assert avatars["provider"].setup_called and avatars["provider"].cleanup_called
    assert tts.cleanup_called and subtitle.started and subtitle.stopped


async def test_stack_keeps_speaking_when_avatar_setup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """虚拟形象不可用（VTS 未启动）只让该 Provider 降级，语音照常播出。"""
    monkeypatch.setenv(TOKEN_ENV_VAR, "test-token")
    tts = _FakeTTS()
    subtitle = _FakeSubtitle()
    avatars: Dict[str, _FakeAvatar] = {}
    _install_fakes(monkeypatch, tts=tts, subtitle=subtitle, avatar_holder=avatars, fail_avatar_setup=True)

    async with FakeBetterGIServer() as server:
        stack = runner.build_output_stack(_config(server.url, tts_enabled=True))
        await stack.start()
        try:
            await _until(lambda: bool(tts.calls))
        finally:
            await stack.stop()

    assert tts.calls == ["先贴着左边走"]
    assert avatars["provider"].setup_called is False  # 连接失败，但不阻断其余


async def test_stack_without_token_still_runs_and_stops_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少 BetterGI 凭证：只停解说接入，语音/字幕链路仍可用，退出流程不报错。"""
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    tts = _FakeTTS()
    avatars: Dict[str, _FakeAvatar] = {}
    _install_fakes(monkeypatch, tts=tts, subtitle=_FakeSubtitle(), avatar_holder=avatars)

    stack = runner.build_output_stack(_config("ws://127.0.0.1:30648/ws/v1/agent/live", tts_enabled=True))
    await stack.start()
    assert stack.bridge._reader is None  # 未设置令牌 → 不开网络任务
    await stack.bridge.stop()  # 可重复停止
    await stack.stop()
    assert tts.cleanup_called is True
