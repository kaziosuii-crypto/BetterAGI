# -*- coding: utf-8 -*-
'''画面冻结泵（设计 01 §6「慢系统思考期画面完全冻结」的落地件）。

为什么需要它：慢系统规划时主线程是阻塞的（最多 2s，卡关开到 10s），这段时间没有任何
人抓帧 —— 录播（image2pipe + 固定 fps）会以为那一段没有时间流过，剪辑时音画就对不上；
下游预览也不知道「现在应该停住」。所以冻结窗口里必须有人以固定节奏继续喂帧：

1. 帧内容 = 最后一张真实画面（不许凭空生成）；
2. 超过 warn_ms（默认 900ms）就把「大肥鱼CPU烧了」贴上去（设计 01 §6 的降级口径）；
3. 全程不阻塞主线程：泵跑在后台线程里，退出时把帧数/贴图次数交回去。

不做的事：不改判定逻辑（冻结多久由编排层决定）、不抓新帧（抓帧是 adapter 的事）。
'''
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

DEFAULT_FPS = 4.0
WARN_MS = 900
PAINT_REFRESH_MS = 500


class FreezePump:
    '''冻结窗口里持续喂最后一帧；超时后贴上降级文案。'''

    def __init__(self, tap: Optional[Callable[[bytes], Any]] = None,
                 painter: Any = None, fps: float = DEFAULT_FPS, text: str = '',
                 warn_ms: int = WARN_MS, refresh_ms: int = PAINT_REFRESH_MS) -> None:
        self.tap = tap
        self.painter = painter
        self.fps = max(0.5, float(fps or DEFAULT_FPS))
        self.text = str(text or '')
        self.warn_ms = max(0, int(warn_ms))
        self.refresh_ms = max(0, int(refresh_ms))
        self.last_frame = b''
        self._painted = b''
        self._painted_ms = 0
        self.stats: Dict[str, Any] = {'holds': 0, 'frames': 0, 'painted': 0,
                                      'errors': 0, 'last_error': ''}

    # ---- 状态 ----------------------------------------------------------

    def set_frame(self, jpeg: bytes) -> None:
        '''记录最近一张真实画面（采集旁路会顺手调它）。'''

        if isinstance(jpeg, (bytes, bytearray)) and jpeg:
            self.last_frame = bytes(jpeg)
            self._painted = b''

    def available(self) -> bool:
        return bool(self.last_frame) and callable(self.tap)

    def _paint(self, jpeg: bytes) -> bytes:
        painter = self.painter
        if painter is None:
            return jpeg
        try:
            # 真实贴图件（freeze_overlay.FreezePainter）的方法叫 paint，早期约定的假件叫 draw：
            # 两个都认，否则真链路会「安静地不贴图」（painter 不是可调用对象 → 原样透传）。
            draw = getattr(painter, 'paint', None) or getattr(painter, 'draw', None)
            if draw is None:
                draw = painter if callable(painter) else None
            out = draw(jpeg) if callable(draw) else jpeg
            return out if isinstance(out, (bytes, bytearray)) and out else jpeg
        except Exception as error:  # noqa: BLE001 - 贴图失败也要继续喂帧
            self._error('paint', error)
            return jpeg

    def _error(self, where: str, error: BaseException) -> None:
        self.stats['errors'] = int(self.stats.get('errors') or 0) + 1
        self.stats['last_error'] = '%s: %s: %s' % (where, type(error).__name__, error)

    def _write(self, jpeg: bytes) -> bool:
        tap = self.tap
        if not jpeg or not callable(tap):
            return False
        try:
            tap(jpeg)
        except Exception as error:  # noqa: BLE001 - 下游挂了不能把冻结泵打断
            self._error('tap', error)
            return False
        self.stats['frames'] = int(self.stats.get('frames') or 0) + 1
        return True

    # ---- 冻结窗口 ------------------------------------------------------

    def hold_once(self, elapsed_ms: int = 0) -> bool:
        '''喂一帧：过 warn_ms 才贴图，贴好的结果按 refresh_ms 复用（贴图不是免费的）。'''

        frame = self.last_frame
        if not frame:
            return False
        if self.painter is not None and elapsed_ms >= self.warn_ms:
            now = int(time.monotonic() * 1000)
            if not self._painted or (now - self._painted_ms) >= self.refresh_ms:
                self._painted = self._paint(frame)
                self._painted_ms = now
                self.stats['painted'] = int(self.stats.get('painted') or 0) + 1
            return self._write(self._painted or frame)
        return self._write(frame)

    def holding(self) -> '_Hold':
        '''with pump.holding(): <阻塞的慢系统调用>；退出时自动停泵。'''

        return _Hold(self)


class _Hold:
    '''上下文管理器：进入时起后台线程喂帧，退出时停泵并结算。'''

    def __init__(self, pump: FreezePump) -> None:
        self.pump = pump
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.started_ms = 0
        self.elapsed_ms = 0

    def __enter__(self) -> '_Hold':
        self.started_ms = int(time.monotonic() * 1000)
        self.pump.stats['holds'] = int(self.pump.stats.get('holds') or 0) + 1
        if self.pump.available():
            self._thread = threading.Thread(target=self._loop, name='freeze-pump', daemon=True)
            self._thread.start()
        return self

    def _loop(self) -> None:
        interval = 1.0 / self.pump.fps
        while not self._stop.is_set():
            elapsed = int(time.monotonic() * 1000) - self.started_ms
            self.pump.hold_once(elapsed)
            if self._stop.wait(interval):
                break

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self.elapsed_ms = int(time.monotonic() * 1000) - self.started_ms
        return False

    def summary(self) -> Dict[str, Any]:
        return {'elapsed_ms': self.elapsed_ms, 'pumped': self._thread is not None}


class FrameTee:
    '''采集旁路的分发器：一份帧喂给多个下游，并记住最后一张（冻结泵要用）。'''

    def __init__(self, *sinks: Optional[Callable[[bytes], Any]]) -> None:
        self.sinks = [sink for sink in sinks if callable(sink)]
        self.last_frame = b''
        self.errors: list = []

    def __call__(self, jpeg: bytes) -> bool:
        if not isinstance(jpeg, (bytes, bytearray)) or not jpeg:
            return False
        self.last_frame = bytes(jpeg)
        ok = False
        for sink in self.sinks:
            try:
                sink(self.last_frame)
                ok = True
            except Exception as error:  # noqa: BLE001 - 一个下游坏了不影响其它下游
                self.errors.append('%s: %s' % (type(error).__name__, error))
        return ok
