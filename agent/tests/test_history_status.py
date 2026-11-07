# -*- coding: utf-8 -*-
"""history_turn 的按键状态短注（设计 03 §C.4 P1）。

手模型要能对"上一步没生效"自纠，历史里就必须带状态；但旧格式是训练分布的一部分，
不传 pressed 时必须逐字节保持原样。这两个要求互相拉扯，所以这里把两边都锁死。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import gamescaling_request as request_mod  # noqa: E402

ACTION = request_mod.ACTION_OPEN + "1 2 3 ; ; ; ; ; 0.5" + request_mod.ACTION_CLOSE


def _legacy(frame_url: str, action: str) -> str:
    """旧实现逐字复刻：用来比对"不传 pressed 时输出一致"。"""
    text = (action or "").strip()
    if not text or request_mod.ACTION_START not in text:
        text = request_mod.NOOP_ACTION
    return json.dumps({"frame_url": str(frame_url or ""), "action": text},
                      ensure_ascii=False, separators=(",", ":"))


def test_without_pressed_is_byte_identical_to_old_output():
    for frame_url, action in [
        ("http://x/f1.jpg", ACTION),
        ("", ACTION),
        ("http://x/f2.jpg", "   "),
        ("http://x/f3.jpg", "garbage without tags"),
        ("http://x/f4.jpg", "  " + ACTION + "  "),
    ]:
        got = request_mod.history_turn(frame_url, action)
        expected = json.loads(_legacy(frame_url, action))
        assert got == expected
        assert json.dumps(got, ensure_ascii=False, separators=(",", ":")) == _legacy(frame_url, action)
        assert " # " not in got["action"]


def test_pressed_false_appends_skipped_reason():
    got = request_mod.history_turn("u", ACTION, pressed=False, step=3,
                                   skipped_reason="no_window_focus")
    assert got["action"] == ACTION + " # step3 skipped: no_window_focus"
    assert got["frame_url"] == "u"


def test_pressed_true_appends_ok():
    got = request_mod.history_turn("u", ACTION, pressed=True, step=2)
    assert got["action"] == ACTION + " # step2 pressed: ok"
    assert got["action"].endswith("ok")


def test_pressed_false_without_reason_has_no_trailing_colon():
    got = request_mod.history_turn("u", ACTION, pressed=False)
    assert got["action"] == ACTION + " # step skipped"
    assert not got["action"].endswith(":")


def test_status_note_reaches_model_messages():
    turn = request_mod.history_turn("frame://a", ACTION, pressed=False, step=1,
                                    skipped_reason="unknown_key:bogus")
    messages = request_mod.build_messages("sys", [turn], "frame://current")
    dumped = json.dumps(messages, ensure_ascii=False)
    assert "# step1 skipped: unknown_key:bogus" in dumped
    assert dumped.count("frame://a") == 1
