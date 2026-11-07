# -*- coding: utf-8 -*-
"""motor 严格失败口径的验收测试（设计 03 C.3/C.5、04 S1/T1）。

覆盖四件事：
1. 坏输出 / 截断 / 清洗后无有效动作 → 抛 MotorParseError（带 raw_text 等诊断字段），
   不再用 label_action 猜键位、也不再把失败伪装成空动作；
2. 只有显式 BETTERAGI_MOTOR_LEGACY_FALLBACK=1 才走旧 TUI 兼容口（fallback 标记）；
3. deadline_ms 真正生效：慢响应 → MotorTimeout，绝不返回空动作；
4. reset() 幂等 + reset_count，decide(new_intent=True) 先 reset；合法全空动作 → empty=True。

全部用 httpx.MockTransport 顶掉真实连接：不联网、不碰 18041 上的模型服务。
"""
import os
import sys
import time

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gamescaling_action as action_mod  # noqa: E402
import motor  # noqa: E402

DATA_URL = "data:image/jpeg;base64,AAAA"
#: 真实见过的坏输出形态：思考闭合了，但后面只有人话、没有动作块。
BAD_TEXT = "Let me think." + action_mod.THINK_CLOSE + " I will press Z."
#: 合法空动作（游戏里就是什么都不按）。
EMPTY_TEXT = (action_mod.ACTION_START + "0 0 0 ; ; ; ; ; ; "
              + action_mod.ACTION_END)
PRESS_Z_TEXT = (action_mod.ACTION_START + "0 0 0 ; Z ; ; ; ; ; "
                + action_mod.ACTION_END)
#: 真实回复形态：模型先闭掉前置填充的思考块，再给动作块。
#: （不带给前端 finalize 会补 <think> 而没处闭合，解析层会判成 truncated。）
THINK_HEAD = "ok." + action_mod.THINK_CLOSE


def _response(action_text):
    """把动作块包成模型真实返回的样子（思考闭合 + 动作块）。"""
    return THINK_HEAD + action_text


def _reply(content, finish_reason="stop"):
    """OpenAI 兼容端点的一轮回复。"""
    return {"choices": [{"message": {"content": content},
                         "finish_reason": finish_reason}]}


def _handler(content, finish_reason="stop"):
    def handler(request):
        return httpx.Response(200, json=_reply(content, finish_reason))
    return handler


def _make_client(monkeypatch, handler, **kwargs):
    """构造一个把 httpx.Client 换成 MockTransport 的 client（测试不联网）。"""
    real_client = httpx.Client

    def factory(*args, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kw)

    monkeypatch.setattr(motor.httpx, "Client", factory)
    return motor.GamescalingClient(endpoint="http://motor.test", **kwargs)


def test_bad_output_raises_motor_parse_error(monkeypatch):
    """坏输出必须是显式失败：raw_text 是模型原文，诊断字段齐全。"""
    monkeypatch.delenv(motor.LEGACY_FALLBACK_ENV, raising=False)
    client = _make_client(monkeypatch, _handler(BAD_TEXT))
    with pytest.raises(motor.MotorParseError) as excinfo:
        client.decide(DATA_URL, deadline_ms=1000)
    error = excinfo.value
    assert error.raw_text == BAD_TEXT  # finalize 之前的一字不改原文
    assert "<" + "think" + ">" not in error.raw_text
    assert error.reason == "unparsed"
    assert error.http_status == 200
    assert error.frame_url == DATA_URL
    assert error.cleaned  # finalize 后的文本留着做对照
    assert isinstance(error, motor.MotorError_)


def test_truncated_action_raises_motor_parse_error(monkeypatch):
    """回复被 max_tokens 截断、动作块没有收尾标签：不许当成功。"""
    monkeypatch.delenv(motor.LEGACY_FALLBACK_ENV, raising=False)
    content = ("I should press Z." + action_mod.THINK_CLOSE + chr(10)
               + action_mod.ACTION_START + "3 -2 0 ; Z ; ; ; ; ;")
    client = _make_client(monkeypatch, _handler(content, finish_reason="length"))
    with pytest.raises(motor.MotorParseError) as excinfo:
        client.decide(DATA_URL, deadline_ms=1000)
    error = excinfo.value
    assert error.reason == "truncated"
    assert error.raw_text == content
    assert error.dropped == []


def test_all_tokens_dropped_raises_no_valid_action(monkeypatch):
    """解析成功但清洗后一个有效 token 不剩（模型出了语义名）：显式失败，别静默空按。"""
    monkeypatch.delenv(motor.LEGACY_FALLBACK_ENV, raising=False)
    content = ("ok." + action_mod.THINK_CLOSE + action_mod.ACTION_START
               + "0 0 0 ; confirm ; ; ; ; ; " + action_mod.ACTION_END)
    client = _make_client(monkeypatch, _handler(content))
    with pytest.raises(motor.MotorParseError) as excinfo:
        client.decide(DATA_URL, deadline_ms=1000)
    error = excinfo.value
    assert error.reason == "no_valid_action"
    assert error.dropped == ["confirm"]
    assert error.raw_text == content


def test_legacy_env_restores_natural_language_fallback(monkeypatch):
    """旧 TUI 兼容口：置 1 时仍能把人话猜成键位（正式链路默认关闭）。"""
    monkeypatch.setenv(motor.LEGACY_FALLBACK_ENV, "1")
    client = _make_client(monkeypatch, _handler(BAD_TEXT))
    decision = client.decide(DATA_URL, deadline_ms=1000)
    assert decision["fallback"] == "natural_language"
    assert decision["keys"] == ["Z"]
    assert decision["steps"][0] == ["Z"]
    assert decision["parsed"] is False
    # 兼容口保持旧行为：坏输出写 no-op 占位，不写 <parse_failed>。
    assert decision["history_text"] == action_mod.NOOP_ACTION_TEXT


def test_legacy_env_must_be_exactly_one(monkeypatch):
    """兼容口只认 "1"：写成 true/yes 之类不能悄悄打开造假路径。"""
    monkeypatch.setenv(motor.LEGACY_FALLBACK_ENV, "true")
    client = _make_client(monkeypatch, _handler(BAD_TEXT))
    with pytest.raises(motor.MotorParseError):
        client.decide(DATA_URL, deadline_ms=1000)


def test_empty_action_is_legal_and_flagged(monkeypatch):
    """解析成功但全空是合法决策：返回 empty=True，history_text 是格式良好的全零动作。"""
    monkeypatch.delenv(motor.LEGACY_FALLBACK_ENV, raising=False)
    client = _make_client(monkeypatch, _handler(_response(EMPTY_TEXT)))
    decision = client.decide(DATA_URL, deadline_ms=1000)
    assert decision["parsed"] is True
    assert decision["empty"] is True
    assert decision["keys"] == []
    assert decision["steps"] == [[], [], [], [], [], []]
    # history_text 必须能被原样回放成合法全零动作，而不是 no-op 之外的占位。
    replayed = action_mod.parse_action(decision["history_text"])
    assert replayed.parsed is True
    assert replayed.is_noop is True
    assert decision["http_status"] == 200


def test_non_empty_action_is_not_flagged(monkeypatch):
    """有键按时 empty=False，且 history_text 可回放。"""
    monkeypatch.delenv(motor.LEGACY_FALLBACK_ENV, raising=False)
    client = _make_client(monkeypatch, _handler(_response(PRESS_Z_TEXT)))
    decision = client.decide(DATA_URL, deadline_ms=1000)
    assert decision["empty"] is False
    assert decision["keys"] == ["Z"]
    replayed = action_mod.parse_action(decision["history_text"])
    assert replayed.parsed is True
    assert replayed.steps[0] == ["Z"]


def test_default_deadline_comes_from_client_timeout(monkeypatch):
    """deadline_ms=None 时沿用 self.timeout（传输层上限），并回显到返回值。"""
    monkeypatch.delenv(motor.LEGACY_FALLBACK_ENV, raising=False)
    client = _make_client(monkeypatch, _handler(_response(EMPTY_TEXT)), timeout=3.0)
    decision = client.decide(DATA_URL)
    assert decision["deadline_ms"] == 3000.0


def test_slow_response_raises_motor_timeout(monkeypatch):
    """慢响应必须抛 MotorTimeout（含 elapsed_ms），不许把超时当空动作。"""
    monkeypatch.delenv(motor.LEGACY_FALLBACK_ENV, raising=False)

    def slow_handler(request):
        time.sleep(0.12)
        return httpx.Response(200, json=_reply(EMPTY_TEXT))

    client = _make_client(monkeypatch, slow_handler)
    with pytest.raises(motor.MotorTimeout) as excinfo:
        # 宽限显式给 0：软预算就是硬线（只有这一类调用才把「慢」当「坏」）。
        client.decide(DATA_URL, deadline_ms=50, grace_ms=0)
    error = excinfo.value
    assert error.deadline_ms == 50
    assert error.elapsed_ms >= 50
    assert error.frame_url == DATA_URL
    assert isinstance(error, motor.MotorError_)


def test_httpx_timeout_maps_to_motor_timeout(monkeypatch):
    """httpx 自己的 TimeoutException 也要翻译成 MotorTimeout，不能漏出去。"""
    monkeypatch.delenv(motor.LEGACY_FALLBACK_ENV, raising=False)

    def timeout_handler(request):
        raise httpx.ConnectTimeout("模拟连接超时")

    client = _make_client(monkeypatch, timeout_handler)
    with pytest.raises(motor.MotorTimeout) as excinfo:
        client.decide(DATA_URL, deadline_ms=200)
    assert "超时" in str(excinfo.value)


def test_reset_is_idempotent_and_counts():
    """reset 幂等：重复调用不抛错，reset_count 跟着涨。"""
    client = motor.GamescalingClient(endpoint="http://motor.test")
    client.set_history([{"frame_url": DATA_URL, "action": PRESS_Z_TEXT}])
    client.reset()
    client.reset()
    assert client.history == []
    assert client.reset_count == 2


def test_new_intent_true_resets_before_decide(monkeypatch):
    """new_intent=True：先清掉上一意图的 history（reset_count +1）。"""
    monkeypatch.delenv(motor.LEGACY_FALLBACK_ENV, raising=False)
    client = _make_client(monkeypatch, _handler(_response(PRESS_Z_TEXT)))
    client.set_history([{"frame_url": DATA_URL, "action": PRESS_Z_TEXT}])
    decision = client.decide(DATA_URL, deadline_ms=1000, new_intent=True)
    assert client.history == []
    assert client.reset_count == 1
    assert decision["keys"] == ["Z"]


def test_decide_does_not_reset_by_default(monkeypatch):
    """默认 new_intent=False：由 executor 决定何时 reset，decide 不擅自清历史。"""
    monkeypatch.delenv(motor.LEGACY_FALLBACK_ENV, raising=False)
    client = _make_client(monkeypatch, _handler(_response(PRESS_Z_TEXT)))
    client.set_history([{"frame_url": DATA_URL, "action": PRESS_Z_TEXT}])
    client.decide(DATA_URL, deadline_ms=1000)
    assert client.reset_count == 0
    assert len(client.history) == 1


def test_parse_failed_history_is_explicit_placeholder():
    """解析失败写 <parse_failed>，绝不写 no-op（否则模型会把坏输出学成正确做法）。"""
    act = action_mod.parse_action("junk, no action block here")
    assert act.parsed is False
    assert act.history_text == action_mod.PARSE_FAILED_TEXT
    assert act.history_text != action_mod.NOOP_ACTION_TEXT


def test_noop_action_is_explicitly_parsed():
    """noop_action() 代表"合法地不按"：parsed=True 且 is_noop=True。"""
    act = action_mod.noop_action()
    assert act.parsed is True
    assert act.is_noop is True
    assert act.history_text == action_mod.NOOP_ACTION_TEXT


def test_slow_but_within_grace_returns_a_usable_action(monkeypatch):
    '''慢一点不算失败：超软预算照收，只把迟到的毫秒数交出去。

    实测背景（2026-11-04，暴露端点）：单帧 p50 904ms、六帧 p50 1200ms，若按软预算
    硬掐，每一步都 MotorTimeout、整轮 0 步作废——把「慢」错判成「坏」。
    '''
    monkeypatch.delenv(motor.LEGACY_FALLBACK_ENV, raising=False)
    monkeypatch.delenv(motor.GRACE_ENV, raising=False)

    def slow_handler(request):
        time.sleep(0.12)
        return httpx.Response(200, json=_reply(_response(PRESS_Z_TEXT)))

    client = _make_client(monkeypatch, slow_handler)
    decision = client.decide(DATA_URL, deadline_ms=50)
    assert decision['steps'][0] == ['Z']
    assert decision['over_budget'] is True
    assert decision['late_ms'] >= 60
    assert decision['deadline_ms'] == 50


def test_grace_defaults_and_env_parsing(monkeypatch):
    '''宽限的默认值/环境变量：写错不外溢成 0（0 会让每步都可能判死）。'''
    monkeypatch.delenv(motor.GRACE_ENV, raising=False)
    assert motor._grace_ms() == motor.DEFAULT_GRACE_MS
    monkeypatch.setenv(motor.GRACE_ENV, '2500')
    assert motor._grace_ms() == 2500.0
    monkeypatch.setenv(motor.GRACE_ENV, 'nonsense')
    assert motor._grace_ms() == motor.DEFAULT_GRACE_MS
    monkeypatch.setenv(motor.GRACE_ENV, '-1')
    assert motor._grace_ms() == motor.DEFAULT_GRACE_MS
