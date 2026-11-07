# -*- coding: utf-8 -*-
'''场次索引（设计文档 02_schemas.md §6.4）：意图索引 + 分片索引，单独一个索引文件。

为什么单独一个文件而不是塞进 timeline.jsonl：

- 时间轴是**事件流**，只加不改、播出侧只有 speech/l2d/viewer 三类（外加控制事件）；
- 意图索引是**结构化列表**，剪辑和复盘要按 intent_id 直接查，不适合扫全量事件；
- 所以：事件流里不写意图，意图只写这里。

时钟铁律（§6.3）：唯一时钟源、单调、10 分钟分片、每片首帧锚点、**不二次取时钟**。
这个模块自己不取时钟——所有毫秒都由调用方传进来，避免出现「索引和事件来自两次取时」的历史 bug。
'''
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = '1.0'
DEFAULT_SEGMENT_MS = 10 * 60 * 1000


@dataclass
class _Intent:
    intent_id: str
    goal_short: str = ''
    started_ms: Optional[int] = None
    ended_ms: Optional[int] = None
    result: str = ''


@dataclass
class SessionIndex:
    '''一场直播的索引：意图列表 + 分片列表。落盘 run/<session_id>/session_index.json。'''

    root: Path
    session_id: str
    game: str = ''
    save_id: str = ''
    t0_epoch_ms: int = 0
    segment_ms: int = DEFAULT_SEGMENT_MS
    intents: List[_Intent] = field(default_factory=list)
    segments: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        if not self.t0_epoch_ms:
            import time

            self.t0_epoch_ms = int(time.time() * 1000)

    # ---- 意图 ----------------------------------------------------------

    def intent_start(self, intent_id: str, goal_short: str = '', started_ms: Optional[int] = None) -> _Intent:
        intent = self._find(intent_id) or _Intent(intent_id=intent_id)
        if intent not in self.intents:
            self.intents.append(intent)
        intent.goal_short = goal_short or intent.goal_short
        intent.started_ms = int(started_ms or 0)
        return intent

    def intent_end(self, intent_id: str, result: str = '', ended_ms: Optional[int] = None) -> Optional[_Intent]:
        intent = self._find(intent_id)
        if intent is None:
            self.warnings.append('intent_end 找不到已登记的意图：%s' % intent_id)
            return None
        end = int(ended_ms or 0)
        if intent.started_ms is not None and end < intent.started_ms:
            self.warnings.append('意图 %s 结束早于开始（%d < %d），已钳到开始时刻' % (intent_id, end, intent.started_ms))
            end = intent.started_ms
        intent.ended_ms = end
        intent.result = result or intent.result
        return intent

    def _find(self, intent_id: str) -> Optional[_Intent]:
        for intent in self.intents:
            if intent.intent_id == intent_id:
                return intent
        return None

    # ---- 分片 ----------------------------------------------------------

    def segment_header(self, file: str, start_ms: int, frame_no: int = 0) -> Dict[str, Any]:
        '''每片第 1 帧的锚点。缺了它这片就不能用于剪辑（orphan）。'''

        record = self._segment(file)
        record['start_ms'] = int(start_ms)
        record['frame_no'] = int(frame_no)
        record['anchored'] = True
        return record

    def segment_end(self, file: str, end_ms: int, frames: int = 0) -> Dict[str, Any]:
        record = self._segment(file)
        start = record.get('start_ms')
        record['end_ms'] = int(end_ms)
        if frames:
            record['frames'] = int(frames)
        if start is not None and record['end_ms'] < start:
            self.warnings.append('分片 %s 结束早于开始，已钳到开始时刻' % file)
            record['end_ms'] = start
        return record

    def _segment(self, file: str) -> Dict[str, Any]:
        for record in self.segments:
            if record.get('file') == file:
                return record
        record: Dict[str, Any] = {
            'file': file,
            'start_ms': None,
            'end_ms': None,
            'frames': 0,
            'anchored': False,
        }
        self.segments.append(record)
        self.segments.sort(key=lambda r: (r.get('start_ms') is None, r.get('start_ms') or 0, r['file']))
        return record

    def segment_name(self, index: int) -> str:
        '''分片命名不许依赖 ffmpeg 自增（§6.3）。'''

        return 'seg_%03d.mp4' % int(index)

    def orphans(self) -> List[str]:
        '''没有首帧锚点的分片：不可用于剪辑。'''

        return [r['file'] for r in self.segments if not r.get('anchored')]

    # ---- 落盘 ----------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            'schema_version': SCHEMA_VERSION,
            'session_id': self.session_id,
            'game': self.game,
            'save_id': self.save_id,
            't0_epoch_ms': int(self.t0_epoch_ms),
            'segment_ms': int(self.segment_ms),
            'intents': [
                {
                    'intent_id': i.intent_id,
                    'goal_short': i.goal_short,
                    'started_ms': i.started_ms,
                    'ended_ms': i.ended_ms,
                    'result': i.result,
                }
                for i in self.intents
            ],
            'segments': [
                {
                    'file': r['file'],
                    'start_ms': r.get('start_ms'),
                    'end_ms': r.get('end_ms'),
                    'frames': r.get('frames') or 0,
                    'frame_no': r.get('frame_no'),
                    'orphan': not r.get('anchored'),
                }
                for r in self.segments
            ],
        }

    @property
    def path(self) -> Path:
        return self.root / 'session_index.json'

    def save(self) -> Path:
        '''原子写：tmp → fsync → replace，半截文件不会成为「最后一份索引」。'''

        self.root.mkdir(parents=True, exist_ok=True)
        target = self.path
        tmp = target.with_suffix('.json.tmp')
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        with open(tmp, 'w', encoding='utf-8') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        return target

    @classmethod
    def load(cls, root: Path) -> 'SessionIndex':
        root = Path(root)
        payload = json.loads((root / 'session_index.json').read_text(encoding='utf-8'))
        index = cls(
            root=root,
            session_id=payload.get('session_id') or root.name,
            game=payload.get('game') or '',
            save_id=payload.get('save_id') or '',
            t0_epoch_ms=int(payload.get('t0_epoch_ms') or 0),
            segment_ms=int(payload.get('segment_ms') or DEFAULT_SEGMENT_MS),
        )
        for item in payload.get('intents') or []:
            index.intents.append(_Intent(
                intent_id=item.get('intent_id') or '',
                goal_short=item.get('goal_short') or '',
                started_ms=item.get('started_ms'),
                ended_ms=item.get('ended_ms'),
                result=item.get('result') or '',
            ))
        for item in payload.get('segments') or []:
            index.segments.append({
                'file': item.get('file') or '',
                'start_ms': item.get('start_ms'),
                'end_ms': item.get('end_ms'),
                'frames': item.get('frames') or 0,
                'frame_no': item.get('frame_no'),
                'anchored': not item.get('orphan'),
            })
        return index
