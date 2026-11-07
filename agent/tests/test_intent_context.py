# -*- coding: utf-8 -*-
"""IntentContext 单测：手嘴共信信箱（设计 03 §C.6 / D.2 的 P0 后半）。

只测内存语义，不联网、不抓帧、不碰 GPU：
空状态、最近一步、并发写不丢、解说截断、快照形状、空输入不抛。
"""
from __future__ import annotations

import threading

import intent_context


def test_empty_state_is_zero_and_none():
    ctx = intent_context.IntentContext()
    snap = ctx.snapshot()
    assert snap["action_count"] == 0
    assert snap["kept_actions"] == 0
    assert snap["last_action"] is None
    assert snap["recent_actions"] == []
    assert snap["commentary_count"] == 0
    assert snap["last_commentary"] is None
    assert snap["recent_commentary"] == []
    assert ctx.last_action() is None
    assert ctx.commentary() == []


def test_record_action_keeps_last_step_and_copies():
    ctx = intent_context.IntentContext()
    first = ctx.record_action(1, ["W"], 30, 100)
    second = ctx.record_action(2, ["W", "A"], 41, 200, note="按键失败")
    assert first["seq"] == 1 and second["seq"] == 2

    last = ctx.last_action()
    assert last["step_index"] == 2
    assert last["keys"] == ["W", "A"]
    assert last["decide_ms"] == 41
    assert last["t_ms"] == 200
    assert last["note"] == "按键失败"

    snap = ctx.snapshot()
    assert snap["action_count"] == 2
    assert [item["step_index"] for item in snap["recent_actions"]] == [1, 2]

    # 返回的是拷贝：调用方改了 keys / 快照，内部状态不受污染。
    last["keys"].append("Boom")
    snap["recent_actions"][1]["keys"].append("Boom")
    assert ctx.last_action()["keys"] == ["W", "A"]
    assert ctx.snapshot()["last_action"]["keys"] == ["W", "A"]


def test_concurrent_writes_do_not_lose_actions():
    ctx = intent_context.IntentContext()
    workers, per_worker = 8, 50
    total = workers * per_worker
    barrier = threading.Barrier(workers)
    action_seqs = []
    comment_seqs = []
    collect_lock = threading.Lock()

    def _write(worker: int) -> None:
        barrier.wait()
        for i in range(per_worker):
            action = ctx.record_action(worker * per_worker + i, ["K%d" % worker], i, i)
            comment = ctx.record_commentary("解说 %d-%d" % (worker, i), i)
            with collect_lock:
                action_seqs.append(action["seq"])
                comment_seqs.append(comment["seq"])

    threads = [threading.Thread(target=_write, args=(w,)) for w in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    snap = ctx.snapshot()
    assert snap["action_count"] == total
    assert snap["commentary_count"] == total
    # seq 在锁内分配：1..N 连续无重复 = 一条写都没丢、也没被覆盖。
    assert sorted(action_seqs) == list(range(1, total + 1))
    assert sorted(comment_seqs) == list(range(1, total + 1))
    # 保留区只留最近 max_kept 条，但最近一条必须是最后写进去的。
    assert len(snap["recent_actions"]) == 8
    assert [item["seq"] for item in snap["recent_actions"]] == sorted(
        item["seq"] for item in snap["recent_actions"])
    assert snap["last_action"]["seq"] == total
    assert snap["last_commentary"]["seq"] == total


def test_commentary_is_truncated_to_recent_limit():
    ctx = intent_context.IntentContext()
    for i in range(10):
        ctx.record_commentary("解说 %d" % i, i * 10)
    assert [item["text"] for item in ctx.commentary()] == [
        "解说 %d" % i for i in range(2, 10)
    ]
    assert [item["text"] for item in ctx.commentary(limit=3)] == [
        "解说 7", "解说 8", "解说 9"
    ]
    assert ctx.commentary(limit=0) == []
    assert ctx.commentary(limit=-1) == []
    snap = ctx.snapshot()
    assert snap["commentary_count"] == 10
    assert snap["last_commentary"]["text"] == "解说 9"
    assert len(snap["recent_commentary"]) == 8


def test_snapshot_shape_and_isolated_copies():
    ctx = intent_context.IntentContext()
    ctx.record_action(7, ["W"], 12, 34)
    ctx.record_commentary("画面向右移动", 40)
    snap = ctx.snapshot()
    assert {
        "action_count", "kept_actions", "last_action", "recent_actions",
        "commentary_count", "last_commentary", "recent_commentary",
    } <= set(snap)
    assert isinstance(snap["action_count"], int)
    assert isinstance(snap["recent_actions"], list)
    assert isinstance(snap["last_commentary"], dict)
    assert snap["action_count"] == 1
    assert snap["last_action"]["step_index"] == 7
    assert snap["last_commentary"]["text"] == "画面向右移动"
    assert snap["last_commentary"]["t_ms"] == 40


def test_empty_inputs_do_not_raise():
    ctx = intent_context.IntentContext()
    # 最空的一步：也记（空动作本身是信息），只是字段全空。
    assert ctx.record_action()["keys"] == []
    assert ctx.record_action(None, None, None, None, None)["keys"] == []
    # 单个字符串按一个键；数字字符串能转 int。
    single = ctx.record_action(3, "W", "41", "120")
    assert single["keys"] == ["W"]
    assert single["decide_ms"] == 41
    assert single["t_ms"] == 120
    # 空解说直接不记，也不抛。
    assert ctx.record_commentary(None) is None
    assert ctx.record_commentary("") is None
    assert ctx.record_commentary("   ", 5) is None
    assert ctx.snapshot()["commentary_count"] == 0
    assert ctx.commentary(limit=None) == []


def test_describe_action_is_one_chinese_sentence():
    assert intent_context.describe_action(None) == ""
    assert intent_context.describe_action({}) == ""
    assert intent_context.describe_action(
        {"step_index": 3, "keys": ["W", "A"], "decide_ms": 41}
    ) == "上一动作：第 3 步按键 W,A，耗时 41ms"
    assert intent_context.describe_action(
        {"step_index": 2, "keys": [], "decide_ms": 5}
    ) == "上一动作：第 2 步没有按键，耗时 5ms"
    # 失败/吞键备注附在句末，嘴能看见「手没按成」。
    assert intent_context.describe_action(
        {"step_index": 1, "keys": ["Z"], "decide_ms": 9, "note": "按键失败"}
    ) == "上一动作：第 1 步按键 Z，耗时 9ms，按键失败"
