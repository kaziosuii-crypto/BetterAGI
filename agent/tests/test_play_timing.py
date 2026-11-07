# -*- coding: utf-8 -*-
"""play_steps 的按键时序（设计 03 §C.13 P1）。

锁四件事：
1. 每步 plan_t 按 step_ms 严格递增，actual_t 来自注入的单调时钟；
2. lag_ms 就是 actual_t - plan_t（不是分开各算一遍的口径）；
3. 窗口抢不到前台 / 键名全查不到 VK 时，这一步标 skipped 且 pressed=False；
4. 不传 clock 时退回 time.monotonic（注入假 monotonic 证明真的走了它）。

全部用假 harness + 假时钟，不碰真窗口、真键盘。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import adapter as adapter_mod  # noqa: E402

HWND = 0xABCD
OTHER_HWND = 0x999


class FakeHarness:
    """ut_harness 的最小替身：前景窗口 + VK 表 + 按键记账。"""

    def __init__(self) -> None:
        self.window = HWND
        self.focus_works = True
        self.focus_calls = 0
        self.downs = []
        self.ups = []
        self.table = {"z": 0x5A, "up": 0x26, "enter": 0x0D, "x": 0x58}
        self.UT_KEYS = ("z", "up", "enter", "x")
        self.u32 = SimpleNamespace(GetForegroundWindow=lambda: self.window)

    def sethwnd_foreground(self, hwnd):
        self.focus_calls += 1
        if self.focus_works:
            self.window = hwnd
        return bool(self.focus_works)

    def key_table(self):
        return self.table

    def key_down_vks(self, vks):
        self.downs.append(list(vks))

    def key_up_vks(self, vks):
        self.ups.append(list(vks))


class FakeProfile:
    game_id = "demo"

    def __init__(self, root: Path) -> None:
        self.dir = root
        self.dir.mkdir(parents=True, exist_ok=True)


class StepClock:
    """按调用顺序吐固定秒数的假单调时钟（play_steps 会先读一次基准）。"""

    def __init__(self, seconds) -> None:
        self.seconds = list(seconds)
        self.calls = 0

    def __call__(self) -> float:
        value = self.seconds[min(self.calls, len(self.seconds) - 1)]
        self.calls += 1
        return value


def _adapter(tmp_path, monkeypatch, fake):
    ad = adapter_mod.ProfileAdapter(FakeProfile(tmp_path / "profile"))
    ad.keymap = {"confirm": ["z"], "up": ["up"]}
    monkeypatch.setattr(ad, "main_hwnd", lambda *a, **k: HWND)
    monkeypatch.setattr(ad, "ensure_input",
                        lambda: {"safe": True, "after": {"layout": "en"}})
    monkeypatch.setattr(adapter_mod, "ut_harness", fake)
    monkeypatch.setattr(adapter_mod, "forbidden_vks", lambda: set())
    return ad


def test_plan_t_follows_step_ms_and_existing_keys_keep_order(tmp_path, monkeypatch):
    fake = FakeHarness()
    ad = _adapter(tmp_path, monkeypatch, fake)
    clock = StepClock([10.0, 10.0, 10.040, 10.080, 10.120])
    out = ad.play_steps([[], ["confirm"], [], []], step_ms=33, clock=clock)

    results = out["step_results"]
    assert [r["plan_t"] for r in results] == [0, 33, 66, 99]
    assert [r["actual_t"] for r in results] == [0, 40, 80, 120]
    assert [r["index"] for r in results] == [0, 1, 2, 3]
    # 既有键的语义和顺序不变；新字段只能追加在末尾。
    assert list(out)[:8] == ["pressed", "skipped_keys", "game", "steps",
                             "step_ms", "hovered", "input_safe", "layout"]
    assert list(out)[-1] == "step_results"
    assert out["steps"] == 4 and out["pressed"] == ["confirm->z"]


def test_lag_ms_is_actual_minus_plan(tmp_path, monkeypatch):
    fake = FakeHarness()
    ad = _adapter(tmp_path, monkeypatch, fake)
    clock = StepClock([0.0, 0.0, 0.100, 0.166])
    out = ad.play_steps([["confirm"], [], ["confirm"]], step_ms=33, clock=clock)

    results = out["step_results"]
    assert [r["actual_t"] for r in results] == [0, 100, 166]
    assert [r["plan_t"] for r in results] == [0, 33, 66]
    assert [r["lag_ms"] for r in results] == [0, 67, 100]
    for step in results:
        assert step["lag_ms"] == step["actual_t"] - step["plan_t"]


def test_swallowed_keys_are_marked_skipped_and_not_pressed(tmp_path, monkeypatch):
    fake = FakeHarness()
    fake.window = OTHER_HWND
    fake.focus_works = False
    ad = _adapter(tmp_path, monkeypatch, fake)
    out = ad.play_steps([["confirm"]], step_ms=33, clock=StepClock([0.0] * 8))

    step = out["step_results"][0]
    assert step["pressed"] is False
    assert step["skipped_reason"] == "no_window_focus"
    # 抢前台尝试过（不是没试就甩锅），但按键确实没落到游戏窗口。
    assert fake.focus_calls == 1
    assert "confirm->z" in out["pressed"]


def test_unknown_key_step_is_skipped_with_reason(tmp_path, monkeypatch):
    fake = FakeHarness()
    ad = _adapter(tmp_path, monkeypatch, fake)
    out = ad.play_steps([["bogus"]], step_ms=33, clock=StepClock([0.0] * 8))

    step = out["step_results"][0]
    assert step["pressed"] is False
    assert step["skipped_reason"].startswith("unknown_key:")
    assert "bogus" in step["skipped_reason"]
    assert out["skipped_keys"] == ["bogus"]


def test_hold_and_empty_steps_stay_ok(tmp_path, monkeypatch):
    fake = FakeHarness()
    ad = _adapter(tmp_path, monkeypatch, fake)
    out = ad.play_steps([["confirm"], ["confirm"], []], step_ms=33,
                        clock=StepClock([0.0] * 8))

    results = out["step_results"]
    # 第 2 步没有新按下（长按续持），但目标键仍在手里 -> 生效。
    assert [r["pressed"] for r in results] == [True, True, True]
    assert fake.downs == [[0x5A]]
    assert results[1]["skipped_reason"] == ""


def test_without_clock_falls_back_to_time_monotonic(tmp_path, monkeypatch):
    fake = FakeHarness()
    ad = _adapter(tmp_path, monkeypatch, fake)
    # 每次 play_steps 读 1 次基准 + 每步 3 次（actual / tick / wait）：7 次。
    ticks = iter([100.000, 100.010, 100.010, 100.010,
                  100.050, 100.050, 100.050])
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: next(ticks))

    out = ad.play_steps([["confirm"], ["confirm"]], step_ms=33)

    results = out["step_results"]
    assert [r["actual_t"] for r in results] == [10, 50]
    assert [r["plan_t"] for r in results] == [0, 33]
    assert [r["lag_ms"] for r in results] == [10, 17]
