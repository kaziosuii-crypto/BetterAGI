# -*- coding: utf-8 -*-
'''卡住检测：把「我卡住了」从一句主观自述变成程序也能看见的事实。

为什么需要它：设计 01 §7 的交棒触发条件里，「卡关」目前只有一条路——快系统（realtime）
自己说「我卡住了」。model 若不自知（最常见：被墙/门/地形挡住，画面十几帧几乎不动，
它还在按同一个方向键），慢系统就永远等不到交棒，玩家看到的就是「AI 对着墙一直走」。
真人主播在这种时候会说「哎我卡住了」，然后换个思路——这正是这里要补的一层判据。

两条判据，任一成立即算卡住（宁可多交棒一次，也别让 AI 对着墙走十分钟）：
1. 画面：最近 window 帧每帧之间的 8x8 灰度平均绝对差都 < min_change（几乎没变）；
2. 动作：同一套动作重复了 min_repeats 次，且画面在这期间也没怎么变。

硬约束：解不了帧（没有 Pillow / 数据不是 JPEG）就一律返回「没卡住」——看不清的时候
绝不能凭猜判卡住，那等于把正常游玩打断。检测自身出任何异常也只记 stats，不抛。
'''
from __future__ import annotations

import io
from typing import Any, Dict, Optional, Tuple

DEFAULT_SIZE = 8
DEFAULT_WINDOW = 12
DEFAULT_MIN_CHANGE = 1.5
DEFAULT_MIN_REPEATS = 4


class StuckError(RuntimeError):
    '''卡住检测的接线错误（不是运行故障）。'''


def _pil():
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - 没有 Pillow 就是「看不出来」
        return None
    return Image


def signature(jpeg: Any, size: int = DEFAULT_SIZE) -> Optional[Tuple[int, ...]]:
    '''把一帧压成 size*size 的灰度指纹；解不了就返回 None（不抛）。'''

    Image = _pil()
    if Image is None or not jpeg:
        return None
    try:
        img = Image.open(io.BytesIO(bytes(jpeg))).convert('L').resize((size, size))
        return tuple(img.tobytes())
    except Exception:  # noqa: BLE001 - 半截 JPEG / 非图片数据：当作看不清
        return None


def distance(a: Optional[Tuple[int, ...]], b: Optional[Tuple[int, ...]]) -> Optional[float]:
    '''两张指纹的平均绝对差（0-255）；形状不对返回 None。'''

    if not a or not b or len(a) != len(b):
        return None
    return sum(abs(int(x) - int(y)) for x, y in zip(a, b)) / float(len(a))


class StuckDetector:
    '''逐帧喂（可选带上这一轮的动作串），返回「卡没卡」。'''

    def __init__(self, window: int = DEFAULT_WINDOW, min_change: float = DEFAULT_MIN_CHANGE,
                 size: int = DEFAULT_SIZE, min_repeats: int = DEFAULT_MIN_REPEATS) -> None:
        self.window = max(2, int(window))
        self.min_change = max(0.0, float(min_change))
        self.size = max(2, int(size))
        self.min_repeats = max(2, int(min_repeats))
        self.stats: Dict[str, Any] = {'frames': 0, 'static_frames': 0, 'stuck': 0,
                                     'unknown': 0, 'errors': 0, 'last_error': ''}
        self._sig: Optional[Tuple[int, ...]] = None
        self._static = 0
        self._action = ''
        self._repeat = 0
        self.last: Dict[str, Any] = {'ok': False, 'stuck': False, 'static_streak': 0,
                                     'repeat': 0, 'why': ''}

    # ---- 对外 ----------------------------------------------------------

    def reset(self) -> None:
        '''换意图/换场景时清空：上一条命对着墙不动，不该算到这一条头上。'''

        self._sig = None
        self._static = 0
        self._action = ''
        self._repeat = 0
        self.last = {'ok': False, 'stuck': False, 'static_streak': 0, 'repeat': 0, 'why': ''}

    def observe(self, jpeg: Any, action: Any = None) -> Dict[str, Any]:
        '''喂一帧；action 是这一轮的动作串（同一条动作重复也能算卡住）。'''

        out = {'ok': False, 'stuck': False, 'static_streak': self._static,
               'repeat': self._repeat, 'why': '', 'frames_seen': self.stats['frames']}
        try:
            sig = signature(jpeg, self.size)
        except Exception as error:  # noqa: BLE001 - 检测坏了不能打断游玩
            self._error('signature', error)
            self.last = out
            return out
        if sig is None:
            # 看不清就不判断：解不了帧时绝不能报「卡住」。
            self.stats['unknown'] = int(self.stats['unknown']) + 1
            self.last = out
            return out
        out['ok'] = True
        self.stats['frames'] = int(self.stats['frames']) + 1
        previous = self._sig
        if previous is not None:
            gap = distance(previous, sig)
            if gap is not None and gap < self.min_change:
                self._static = int(self._static) + 1
                self.stats['static_frames'] = int(self.stats['static_frames']) + 1
            else:
                self._static = 0
        self._sig = sig
        text = str(action or '').strip()
        if text:
            self._repeat = int(self._repeat) + 1 if text == self._action else 0
            self._action = text
        out['static_streak'] = self._static
        out['repeat'] = self._repeat
        why = ''
        if self._static >= self.window:
            why = '画面连续 %d 帧几乎没变' % self._static
        if self._repeat >= self.min_repeats and self._static >= max(1, self.window // 3):
            tail = '同一套动作重复了 %d 次' % (self._repeat + 1)
            why = (why + '，' + tail) if why else tail + '且画面几乎没动'
        if why:
            out['stuck'] = True
            out['why'] = why
            self.stats['stuck'] = int(self.stats['stuck']) + 1
        self.last = out
        return out

    # ---- 内部 ----------------------------------------------------------

    def _error(self, where: str, error: BaseException) -> None:
        self.stats['errors'] = int(self.stats['errors']) + 1
        self.stats['last_error'] = '%s: %s: %s' % (where, type(error).__name__, error)


def build_stuck(**kwargs: Any) -> Optional[StuckDetector]:
    '''没有 Pillow 就返回 None（=不检测），接线层不用分支。'''

    if _pil() is None:
        return None
    try:
        return StuckDetector(**kwargs)
    except Exception:  # noqa: BLE001 - 参数写错不该让整场播不出去
        return None