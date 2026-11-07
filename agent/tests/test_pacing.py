'''节奏换算（agent/pacing.py）：200ms 是游戏时间，不是墙钟预算。

实测来源：/BetterAGI/_diag/gs_5090_latency.py，5090 vLLM，训练骨架 + 官方 Doom 帧，
开思考 p50 1155 / p95 1484ms，关思考 p50 696 / p95 735ms，10/10 parsed。
'''

from __future__ import annotations

import pacing


def test_chunk_is_game_time_not_wall_budget() -> None:
    # 200ms 是游戏时间窗口；真正按键 6x33 = 198ms，差 2ms 是步长取整不是 bug。
    assert pacing.GAME_CHUNK_MS == 200
    assert pacing.STEPS_PER_CHUNK * pacing.STEP_MS == pacing.KEYED_MS == 198


def test_duty_cycle_says_how_often_we_actually_press() -> None:
    assert pacing.duty_cycle(1000) == 0.198
    assert pacing.duty_cycle(pacing.KEYED_MS) == 1.0
    assert pacing.duty_cycle(0) == 0.0
    assert pacing.duty_cycle(-5) == 0.0


def test_pacing_reports_slowdown_and_idle() -> None:
    p = pacing.pacing(thinking=False, wall_ms=740)
    assert p['chunk_ms'] == 200
    assert p['keyed_ms'] == 198
    assert p['wall_ms'] == 740
    assert p['idle_ms'] == 542
    assert p['slowdown_x'] == 3.7
    assert p['duty_cycle'] == 0.268


def test_pacing_200ms_would_be_full_duty() -> None:
    p = pacing.pacing(thinking=True, wall_ms=pacing.KEYED_MS)
    assert p['idle_ms'] == 0.0
    assert p['duty_cycle'] == 1.0


def test_describe_is_human_readable() -> None:
    text = pacing.describe(thinking=True)
    assert '200ms 游戏输入' in text
    assert '1490ms' in text
    assert '慢动作' in text