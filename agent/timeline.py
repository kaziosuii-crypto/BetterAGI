# -*- coding: utf-8 -*-
'''时间轴落盘（设计文档 02_schemas.md §6）：一场直播只有一组单调时钟。

硬约束：
* t_ms 是相对本场开始的单调毫秒；任何时候都不允许倒退——倒退就是缺陷，
  这里不静默吞掉，而是记一条 jitter 事件（clamp 到上一刻）后继续，
  这样 04_acceptance.md 的 S8 时钟倒退演练才抓得到。
* 播出侧事件只有三类：speech / l2d / viewer；控制事件（intent_start / intent_end /
  game_action / handoff / freeze / incident / net_reconnect / segment_header / stuck）共用同一
  个 t_ms 与同一个文件，剪辑前用 export_viewer() 导出纯播出视图。
* 每行一个 JSON 对象，snake_case，带 schema_version；10 分钟分片的首帧写 segment_header。

对外接口（其他模块只依赖这些名字）：

    tl = Timeline(root=Path('run'), session_id='run-20261104-1830')
    t0 = tl.event('intent_start', intent_id='i-001', goal='走到存档点')
    t1 = tl.event('speech', text='好，先往左走', dur_ms=1200)
    tl.close()

Timeline 之外的模块不要自己写 timeline.jsonl，也不要用 time.time() 当 t_ms。
'''
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA_VERSION = 1

#: 播出侧事件（观众能看见/听见的）。
VIEWER_KINDS = ('speech', 'l2d', 'viewer')
#: 控制事件（不直接播出，但剪辑与验收要用）。
CONTROL_KINDS = (
    'intent_start',
    'intent_end',
    'game_action',
    'handoff',
    'freeze',
    'incident',
    'net_reconnect',
    'segment_header',
    #: 卡住检测命中（01 §7 卡关的机器判据）：手收手交棒，慢系统换思路。
    'stuck',
)
KINDS = VIEWER_KINDS + CONTROL_KINDS

#: 单个分片时长（毫秒）：10 分钟。
SEGMENT_MS = 10 * 60 * 1000


class Clock:
    '''单调毫秒时钟：进程内唯一真源，测试可以注入。'''

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def now_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)


class Timeline:
    '''一场直播的时间轴：一行一个事件，t_ms 单调。'''

    def __init__(self, root: Path, session_id: str, clock: Optional[Clock] = None) -> None:
        self.root = Path(root)
        self.session_id = str(session_id)
        self.clock = clock or Clock()
        self.path = self.root / 'timeline.jsonl'
        self._lock = threading.Lock()
        self._last_t_ms = 0
        self._seq = 0
        self._segment = 0
        self._jitter: List[Dict[str, Any]] = []
        self._closed = False

    # ---- 写入 ----------------------------------------------------------

    def event(self, kind: str, payload: Optional[Dict[str, Any]] = None, **fields: Any) -> int:
        '''落一条事件，返回它的 t_ms。kind 必须在 KINDS 里。

        payload 是给「字段名和形参撞车」留的口子（例如 incident 的载荷里就有 kind），
        fields 里的同名字段优先于 payload。
        '''
        if kind not in KINDS:
            raise ValueError('未知时间轴事件: ' + str(kind))
        merged: Dict[str, Any] = dict(payload or {})
        merged.update(fields)
        with self._lock:
            if self._closed:
                raise RuntimeError('Timeline 已关闭')
            t_ms = int(merged.pop('t_ms', 0) or 0) or self.clock.now_ms()
            if t_ms < self._last_t_ms:
                # 时钟倒退是缺陷：留痕（不吞），clamp 到上一刻保证单调。
                self._jitter.append({'at_seq': self._seq, 'wanted': t_ms, 'clamped': self._last_t_ms})
                t_ms = self._last_t_ms
            self._last_t_ms = t_ms
            self._seq += 1
            row: Dict[str, Any] = {'t_ms': t_ms, 'seq': self._seq, 'kind': kind}
            # 载荷里也可能有 kind（如 incident 的 {kind, dir}）：不许覆盖事件类型，
            # 改名 incident_kind 保留下来，断言器按事件类型的 kind 分组。
            inner_kind = merged.pop('kind', None)
            for key, value in merged.items():
                row[str(key)] = value
            if inner_kind is not None and inner_kind != kind:
                row['incident_kind'] = inner_kind
            self._write(row)
            return t_ms

    def speech(self, text: str, dur_ms: Optional[int] = None, **extra: Any) -> int:
        return self.event('speech', text=str(text or ''), dur_ms=dur_ms, **extra)

    def game_action(self, steps: Any, intent_id: str = '', **extra: Any) -> int:
        return self.event('game_action', steps=steps, intent_id=intent_id, **extra)

    def incident(self, reason: str, **extra: Any) -> int:
        return self.event('incident', reason=str(reason or ''), **extra)

    def _write(self, row: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({'v': SCHEMA_VERSION, **row}, ensure_ascii=False)
        with self.path.open('a', encoding='utf-8') as handle:
            handle.write(line + _NL)
            handle.flush()

    # ---- 分片 ----------------------------------------------------------

    def segment_index(self, t_ms: Optional[int] = None) -> int:
        return int((self._last_t_ms if t_ms is None else int(t_ms)) // SEGMENT_MS)

    def segment_header(self, frame_no: int = 0, **extra: Any) -> int:
        '''写分片头：分片首帧锚点，录制侧用同一个 t_ms 才对齐得上。'''
        self._segment = self.segment_index()
        return self.event('segment_header', segment=self._segment, frame_no=int(frame_no), **extra)

    # ---- 收尾 ----------------------------------------------------------

    @property
    def jitter(self) -> List[Dict[str, Any]]:
        '''记录到的时钟倒退（S8 演练读这个）。'''
        return list(self._jitter)

    @property
    def last_t_ms(self) -> int:
        return self._last_t_ms

    def rows(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out: List[Dict[str, Any]] = []
        with self.path.open('r', encoding='utf-8') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                out.append(json.loads(line))
        return out

    def export_viewer(self, target: Optional[Path] = None) -> Path:
        '''只导出播出侧三类事件（剪辑前导出 timeline_viewer.jsonl）。'''
        target = Path(target) if target else self.root / 'timeline_viewer.jsonl'
        rows: Iterable[Dict[str, Any]] = [r for r in self.rows() if r.get('kind') in VIEWER_KINDS]
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('w', encoding='utf-8') as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + _NL)
        return target

    def close(self) -> None:
        self._closed = True


_NL = chr(10)


def open_timeline(root: Path, session_id: str, clock: Optional[Clock] = None) -> Timeline:
    '''建一个 Timeline 并写好目录。'''
    tl = Timeline(root=root, session_id=session_id, clock=clock)
    tl.root.mkdir(parents=True, exist_ok=True)
    tl.event('segment_header', segment=0, frame_no=0, session_id=session_id)
    return tl
