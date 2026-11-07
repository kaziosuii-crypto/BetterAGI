'''每步三段计时：编帧 / 上传+推理 / 执行按键，落 run/<sid>/steps.jsonl。

用户口径（2026-11-04）：不要再拿「单次上传秒数」当带宽 —— 一次决策的墙钟由三段组成：

    t_encode       截帧 + 编码 + base64（本地，和网络无关）
    t_upload_infer 把 data URL 发出去、等模型答完（上传 + 排队 + 推理）；
                   这一段才是「上传」，绝对值取决于帧数与帧大小
    t_act          真正按 6 组键（200ms 游戏时间 / 198ms 墙钟）

只有把三段分开记，才能回答「该优化哪一段」。落盘：一行一个 JSON 对象，追加写。
'''
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

DEFAULT_FILENAME = 'steps.jsonl'
PHASES = ('t_encode', 't_upload_infer', 't_act')


def _pct(sorted_values: List[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (len(sorted_values) - 1) * q
    low = int(pos)
    high = min(low + 1, len(sorted_values) - 1)
    frac = pos - low
    return float(sorted_values[low]) * (1 - frac) + float(sorted_values[high]) * frac


def median(values) -> float:
    '''中位数；空列表给 0.0（不返回 None，免得报告里出现 null 又要解释）。'''

    clean = sorted(float(v) for v in values if v is not None)
    if not clean:
        return 0.0
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2.0


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    '''把若干步压成「三段中位数 + p95」，外加每步合计的中位数。'''

    out: Dict[str, Any] = {'steps': len(rows), 'phases': {}, 'warnings': []}
    for phase in PHASES:
        values = [r.get(phase) for r in rows if r.get(phase) is not None]
        out['phases'][phase] = {
            'n': len(values),
            'median_ms': round(median(values), 1),
            'p95_ms': round(_pct(sorted(float(v) for v in values), 0.95), 1),
        }
    totals = []
    for row in rows:
        parts = [row.get(p) for p in PHASES]
        if any(v is not None for v in parts):
            totals.append(sum(float(v) for v in parts if v is not None))
    out['total_median_ms'] = round(median(totals), 1)
    if rows:
        for phase in PHASES:
            if out['phases'][phase]['n'] < len(rows):
                out['warnings'].append('有步骤缺 %s（调用方没传全）' % phase)
    return out


class StepTrace:
    '''一次直播里每步的三段计时账本（线程安全，写失败不抛）。'''

    def __init__(self, root: str, session_id: str = '',
                 clock: Optional[Any] = None, filename: str = DEFAULT_FILENAME):
        self.root = root
        self.session_id = session_id
        self.filename = filename
        self._lock = threading.Lock()
        self._clock = clock
        self.writes = 0
        self.errors: List[str] = []

    @property
    def path(self) -> str:
        return os.path.join(self.root, self.filename)

    def _now_ms(self) -> int:
        clock = self._clock
        if clock is None:
            return int(time.monotonic() * 1000)
        now = getattr(clock, 'now_ms', None)
        if callable(now):
            try:
                return int(now())
            except Exception:  # noqa: BLE001 - 记账时钟坏了不拖垮执行
                return 0
        return int(time.monotonic() * 1000)

    def record(self, *, t_encode: Optional[float] = None,
               t_upload_infer: Optional[float] = None,
               t_act: Optional[float] = None, **extra: Any) -> Dict[str, Any]:
        '''追加一步，返回落盘的那一行（写失败时 ok=False 并附原因）。'''

        row: Dict[str, Any] = {'v': 1, 't_ms': self._now_ms()}
        if self.session_id:
            row['session_id'] = self.session_id
        for phase, value in (('t_encode', t_encode),
                             ('t_upload_infer', t_upload_infer),
                             ('t_act', t_act)):
            if value is not None:
                try:
                    row[phase] = round(float(value), 1)
                except (TypeError, ValueError):
                    pass
        for key, value in extra.items():
            if key in ('v', 't_ms') or value is None:
                continue
            row[key] = value
        parts = [row.get(p) for p in PHASES]
        row['total_ms'] = round(sum(float(v) for v in parts if v is not None), 1)
        try:
            os.makedirs(self.root, exist_ok=True)
            with self._lock:
                with open(self.path, 'a', encoding='utf-8') as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + os.linesep)
                self.writes += 1
            row['ok'] = True
        except Exception as error:  # noqa: BLE001 - 记账坏不能影响按键
            message = '%s: %s' % (type(error).__name__, error)
            with self._lock:
                self.errors.append(message)
            row['ok'] = False
            row['error'] = message
        return row

    def rows(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            with open(self.path, encoding='utf-8') as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:  # noqa: BLE001 - 半截行跳过
                        continue
        except FileNotFoundError:
            return []
        except Exception as error:  # noqa: BLE001
            self.errors.append('%s: %s' % (type(error).__name__, error))
        return out

    def summary(self) -> Dict[str, Any]:
        result = summarize(self.rows())
        result['file'] = self.path
        result['writes'] = self.writes
        if self.errors:
            result.setdefault('warnings', []).extend(self.errors[-3:])
        return result


def build_trace(root: str, session_id: str = '',
                clock: Optional[Any] = None) -> StepTrace:
    return StepTrace(root, session_id=session_id, clock=clock)
