# -*- coding: utf-8 -*-
"""手模型历史里的状态注记：按键没泵进去，下一轮要能看见（设计 03 §C.4）。

旧 record() 只收两个位置参数，执行器传 pressed/step/skipped_reason 会 TypeError 退回
两参版本——那样 history 里永远没有「这一步被吞了」，模型只会照同一个打法重来。
"""
from __future__ import annotations

import pytest

import gamescaling_request as request_mod
import motor as motor_mod

FRAME = 'data:image/jpeg;base64,AAAA'
ACTION = '<|action_start|>0 0 0 ; Z ; Z<|action_end|>'


def _client() -> 'motor_mod.GamescalingClient':
    return motor_mod.GamescalingClient(endpoint="http://127.0.0.1:1")


def test_record_without_status_is_byte_identical() -> None:
    client = _client()
    client.record(FRAME, ACTION)
    assert client.history == [request_mod.history_turn(FRAME, ACTION)]
    assert client.history[0]["action"] == ACTION


def test_record_with_pressed_ok_appends_note() -> None:
    client = _client()
    client.record(FRAME, ACTION, pressed=True, step=3)
    assert client.history[0]["action"].endswith(ACTION + " # step3 pressed: ok")


def test_record_with_skipped_reason_appends_note() -> None:
    client = _client()
    client.record(FRAME, ACTION, pressed=False, step=4, skipped_reason='no_window_focus')
    assert client.history[0]["action"].endswith(ACTION + " # step4 skipped: no_window_focus")


def test_history_keeps_only_last_n_turns(monkeypatch) -> None:
    monkeypatch.setenv('BETTERAGI_GAMESCALING_HISTORY', '2')
    client = _client()
    for i in range(4):
        client.record(FRAME + str(i), ACTION, pressed=True, step=1)
    assert len(client.history) == 2
    assert client.history[-1]["frame_url"].endswith("3")


def test_executor_reaches_status_aware_record() -> None:
    """执行器的 kwargs 调用必须命中真 record()，不再靠 TypeError 退回。"""

    client = _client()
    client.record(FRAME, ACTION, pressed=False, step=2, skipped_reason='unknown_key:Q')
    assert " # step2 skipped: unknown_key:Q" in client.history[0]["action"]