# -*- coding: utf-8 -*-
'''节奏换算：200ms 是**游戏时间**，不是墙钟预算。

2026-11-04 在 5090 上实测（E:/BetterAGI/_diag/gs_5090_latency.py，训练骨架提示词 +
官方 Doom 示例帧，5 次/档，全部 parsed）：

    开思考   p50 1155ms  p95 1484ms  min 708ms  max 1484ms
    关思考   p50  696ms  p95  735ms  min 673ms  max 735ms

也就是说：一次决策产出「200ms 的游戏输入」，但墙钟要 0.7-1.5s 才拿得到。
把 deadline 设成 200ms 会让**每一步都 MotorTimeout**（实测：8s 预算下偶发一次 8s
卡死是隧道/队列问题，不是常态）。

这里的职责只有一个：把「chunk 多长」和「墙钟预算多少」分开命名，并算清楚占空比，
让上层能诚实地选择「游戏变慢 / 降决策频率 / 排动作队列」，而不是假装 200ms 到达。
'''
from __future__ import annotations

import os
from typing import Dict, Optional

GAME_CHUNK_MS = 200          # 一次决策覆盖的游戏时间窗口（2 秒 = 10 组）
KEYED_MS = 198               # 真正按键的时间：6 步 × 33ms，比窗口少 2ms
STEP_MS = 33
STEPS_PER_CHUNK = 6

# 实测值（p95），留一点余量给隧道抖动与 vLLM 排队。
MEASURED_THINKING_P95_MS = 1490
MEASURED_NO_THINKING_P95_MS = 740
BUDGET_ENV = 'BETTERAGI_MOTOR_STEP_MS'


def step_budget_ms(thinking: bool = False, *, env: Optional[Dict[str, str]] = None) -> int:
    '''这一决策允许花多少**墙钟**毫秒。env 优先，其次实测 p95 + 余量。'''

    source = os.environ if env is None else env
    raw = str(source.get(BUDGET_ENV, '') or '').strip()
    if raw:
        try:
            value = int(float(raw))
        except ValueError:
            value = 0
        if value > 0:
            return value
    return MEASURED_THINKING_P95_MS if thinking else MEASURED_NO_THINKING_P95_MS


def duty_cycle(wall_ms: float, chunk_ms: float = KEYED_MS) -> float:
    '''真正按键的时间 / 墙钟时间：0.27 = 每 1 秒只有 270ms 在按键，其余在等模型。'''

    if wall_ms is None or wall_ms <= 0:
        return 0.0
    return max(0.0, min(1.0, float(chunk_ms) / float(wall_ms)))


def pacing(thinking: bool = False, wall_ms: Optional[float] = None) -> Dict[str, float]:
    '''给上层算账：这一档的墙钟、空闲、占空比，以及「几倍慢动作」。'''

    wall = float(wall_ms if wall_ms is not None else step_budget_ms(thinking))
    duty = duty_cycle(wall)
    return {
        'chunk_ms': GAME_CHUNK_MS,
        'keyed_ms': KEYED_MS,
        'wall_ms': wall,
        'idle_ms': max(0.0, wall - KEYED_MS),
        'duty_cycle': round(duty, 3),
        'slowdown_x': round(wall / GAME_CHUNK_MS, 2),
        'thinking': 1.0 if thinking else 0.0,
    }


def describe(thinking: bool = False) -> str:
    '''人话版，用于日志/预检/报告。'''

    p = pacing(thinking)
    return ('200ms 游戏输入需要 %.0fms 墙钟（占空比 %.0f%%，等效 %.1f 倍慢动作）'
            % (p['wall_ms'], p['duty_cycle'] * 100.0, p['slowdown_x']))
