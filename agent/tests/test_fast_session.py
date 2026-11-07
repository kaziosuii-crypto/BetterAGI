"""快系统"一个意图一个会话"的验收：会话数能数出来，断了先重连，再断才说预置台词。"""

from __future__ import annotations

import types

import pytest

import fast_stream
import fast_session
import omni


class FakeClient:
    """只实现 FastStream 会用到的那几个协程；不联网。"""

    instances: list = []
    fail_ask = 0
    fail_connect = False
    text = "我看到了一个箱子，左边有个把手"
    calls: list = []

    def __init__(self, config):
        self.config = config
        self.closed = False
        self.seeded = 0
        self.frames = []
        FakeClient.instances.append(self)

    async def connect(self):
        if FakeClient.fail_connect:
            raise RuntimeError("连不上")

    async def close(self):
        self.closed = True

    async def cancel_inflight(self):
        return None

    async def seed_audio(self):
        self.seeded += 1

    async def send_frame(self, frame):
        self.frames.append(frame)

    async def commit(self):
        return None

    async def create_response(self, instructions):
        return None

    async def collect(self, *args, **kwargs):
        if FakeClient.fail_ask > 0:
            FakeClient.fail_ask -= 1
            raise omni.OmniTimeout("首包超时", waited_s=12.0, budget_s=12.0)
        return types.SimpleNamespace(text=FakeClient.text, function_calls=list(FakeClient.calls))


@pytest.fixture(autouse=True)
def fake_omni(monkeypatch):
    FakeClient.instances = []
    FakeClient.fail_ask = 0
    FakeClient.fail_connect = False
    FakeClient.calls = []
    monkeypatch.setattr(omni, "OmniClient", FakeClient)
    yield


def _session(**kwargs):
    return fast_session.IntentFastSession(fps=4.0, pump=False, **kwargs)


def test_one_intent_one_session():
    session = _session()
    session.start()
    assert session.ask("看看画面") == FakeClient.text
    first = FakeClient.instances[0]
    session.close_intent()
    assert first.closed is True

    session.start()
    assert session.ask("再看看") == FakeClient.text
    assert len(FakeClient.instances) == 2
    assert session.session_opens == 2
    assert session.reconnects == 0
    session.close_intent()
    assert FakeClient.instances[1].closed is True


def test_lazy_open_when_ask_without_start():
    session = _session()
    assert session.ask("开口") == FakeClient.text
    assert session.session_opens == 1
    assert len(FakeClient.instances) == 1
    session.close_intent()


def test_first_failure_reconnects_and_still_answers():
    FakeClient.fail_ask = 1
    session = _session()
    session.start()
    assert session.ask("看看画面") == FakeClient.text
    assert session.reconnects == 1
    assert session.session_opens == 2
    assert session.degraded is False
    assert "OmniTimeout" in session.last_error
    session.close_intent()


def test_second_failure_degrades_to_preset_line():
    FakeClient.fail_ask = 99
    session = _session()
    session.start()
    text = session.ask("看看画面")
    assert text in session.presets
    assert session.degraded is True
    assert session.presets_said == 1
    assert session.reconnects == 1
    session.close_intent()


def test_preset_lines_rotate():
    FakeClient.fail_ask = 99
    session = _session(presets=["甲", "乙"])
    session.start()
    first = session.ask("一")
    second = session.ask("二")
    sessions_before = len(FakeClient.instances)
    third = session.ask("三")
    assert first == "甲"
    assert second == "乙"
    assert third == "甲"
    assert session.presets_said == 3
    assert len(FakeClient.instances) > sessions_before or session.degraded is True
    session.close_intent()


def test_stats_exposes_counters():
    session = _session()
    session.start()
    session.ask("开口")
    stats = session.stats()
    session.close_intent()
    assert stats["session_opens"] == 1
    assert stats["reconnects"] == 0
    assert stats["degraded"] is False
    assert stats["presets_said"] == 0
    assert "last_error" in stats


def test_round_budget_env(monkeypatch):
    monkeypatch.setenv(fast_session.ROUND_ENV, "7.5")
    assert fast_session.round_budget_s() == 7.5
    monkeypatch.setenv(fast_session.ROUND_ENV, "不是数字")
    assert fast_session.round_budget_s() == fast_session.DEFAULT_ROUND_S
    monkeypatch.delenv(fast_session.ROUND_ENV, raising=False)
    assert fast_session.round_budget_s(3.0) == 3.0


def test_build_without_key_returns_none(monkeypatch):
    import fast_system

    monkeypatch.setattr(fast_system, "resolve_dashscope_key", lambda: "")
    assert fast_session.build_intent_session() is None

def test_handoff_tool_call_becomes_done_marker():
    FakeClient.calls = [{"name": "request_handoff", "call_id": "c1",
                          "arguments": {"reason": "箱子已经打开了，接下来该看主线"}}]
    session = _session()
    session.start()

    text = session.ask("继续")

    assert session.handoff_requested is True
    assert session.handoff_reason == "箱子已经打开了，接下来该看主线"
    assert "[DONE]" in text and "接下来该看主线" in text
    assert session.events[-1]["kind"] == "handoff"
    assert session.events[-1]["name"] == "request_handoff"


def test_handoff_without_reason_gets_default_reason():
    FakeClient.calls = [{"name": "request_handoff", "arguments": {}}]
    session = _session()
    session.start()

    text = session.ask("继续")

    assert "[DONE]" in text
    tail = text.split("[DONE]", 1)[1].strip()
    assert len(tail) >= 4


def test_blocked_and_reference_are_recorded_without_handoff():
    FakeClient.calls = [
        {"name": "report_blocked", "arguments": {"reason": "门锁着"}},
        {"name": "ask_slow_reference", "arguments": {"question": "该往哪走"}},
        {"name": "some_other_tool", "arguments": {}},
    ]
    session = _session()
    session.start()

    text = session.ask("继续")

    assert text == FakeClient.text
    assert session.handoff_requested is False
    assert [e["kind"] for e in session.events] == ["blocked", "reference"]
    assert session.stats()["events"] == 2


def test_new_session_clears_handoff_flag():
    FakeClient.calls = [{"name": "request_handoff", "arguments": {"reason": "做完了"}}]
    session = _session()
    session.start()
    session.ask("继续")
    assert session.handoff_requested is True

    FakeClient.calls = []
    session.close_intent()
    session.start()

    assert session.handoff_requested is False
    assert session.handoff_reason == ""
    assert session.ask("新意图") == FakeClient.text
