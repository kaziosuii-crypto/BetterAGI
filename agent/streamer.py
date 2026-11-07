# -*- coding: utf-8 -*-
'''总装：把「想 / 做 / 交棒 / 落账」串成一场直播的回合循环（设计文档 01 §4、03 C.12）。

这个模块只做接线，不实现任何模型能力：

    预检 -> [拉弹幕 -> 慢系统规划 -> 快系统开新会话 -> 手执行 -> 交棒回执 -> 落账] x N -> 下播

三条硬规定（验收会查）：
* 一个意图一个快系统会话：execute 前 start()，交棒后 close_intent()；
* 慢系统超时不算故障：停在 PLANNING 重试，绝不熔断（04 §三 S3）；
* 回执的结论 8-60 字、见闻 120-2000 字是 handoff 的硬门槛，凑不出来就如实写
  "没做到"，不许编一条漂亮结论。
'''
from __future__ import annotations

import base64
import inspect
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

NL = chr(10)
#: 冻结超过这个时长就必须贴图（02 §4 降级口径）；这里只用来在没贴图时报警。
FREEZE_WARN_MS = 30000
#: 一轮规划最多带几张真帧给慢系统（与 executor.MAX_SLOW_FRAMES 同口径）。
MAX_SLOW_FRAMES = 3


class StreamerError(Exception):
    '''总装层的用法错误（不是运行故障）。'''


#: ended -> 人话（结论兜底用；没映射的就照抄 ended）。
ENDED_TEXT = {
    'goal_reached': '做到了',
    'budget': '时间用完还没做到',
    'max_decisions': '决策次数用完还没做到',
    'no_action': '手没给出动作',
    'no_motor': '手模型没接上',
    'motor_error': '手模型报错',
    'motor_timeout': '手模型超时',
    'motor_parse_error': '手模型解析失败',
    'capture_error': '截帧失败',
    'stuck': '卡住了，得换个思路',
}

#: 见闻不够 120 字时的补白：每一句都是可核对的事实，不含任何画面推测。
PAD_LINES = (
    '（本轮没留下更多解说：快系统只说了这么多，其余信息都在执行记录里。）',
    '（这一轮的意图、结果与耗时已写进 session_index 与 handoffs，需要细节请看那两份文件。）',
    '（见闻只记录已经被说出口的内容，不补充推测：没说的就是没看见。）',
    '（观众与弹幕这一轮没有需要单独记录的反应。）',
)

CONCLUSION_MIN = 8
CONCLUSION_MAX = 60
OBSERVATIONS_MIN = 120
OBSERVATIONS_MAX = 2000


def build_conclusion(result: Dict[str, Any], plan: Any = None, hint: str = '') -> str:
    '''一句 8-60 字的结论：先用执行器自己的总结，短了就用 ended 说人话。'''

    # 快系统自己说要交棒时带了理由：它对自己刚做完什么最清楚，优先用它那句话。
    hint = str(hint or '').strip()
    if CONCLUSION_MIN <= len(hint) <= CONCLUSION_MAX:
        return hint
    summary = str((result or {}).get('summary') or '').strip()
    if CONCLUSION_MIN <= len(summary) <= CONCLUSION_MAX:
        return summary
    ended = str((result or {}).get('ended') or 'unknown')
    goal = str(getattr(plan, 'goal', '') or '').strip()[:20]
    phrase = ENDED_TEXT.get(ended, ended)
    if goal:
        text = '这一轮%s（目标：%s）' % (phrase, goal)
    else:
        text = '这一轮%s' % phrase
    if len(text) < CONCLUSION_MIN:
        text = '这一轮结束，%s，没有拿到可用的结论' % phrase
    return text[:CONCLUSION_MAX]


def build_observations(result: Dict[str, Any], extra: Sequence[str] = (),
                       handoff: str = '') -> str:
    '''把解说流水账压成 120-2000 字的见闻；不够就如实说明还知道什么。'''

    result = result or {}
    lines: List[str] = []
    handoff = str(handoff or '').strip()
    if handoff:
        lines.append('- 快系统交棒时说的理由：' + handoff)
    for note in result.get('commentary') or []:
        if not isinstance(note, dict):
            continue
        text = str(note.get('text') or '').strip()
        if not text:
            continue
        t_ms = note.get('t_ms')
        lines.append('- [%sms] %s' % (t_ms, text) if t_ms is not None else '- ' + text)
    for note in extra:
        note = str(note or '').strip()
        if note:
            lines.append('- ' + note)
    body = NL.join(lines)
    facts = []
    if result.get('action_count') is not None:
        facts.append('共 %s 个决策步' % result.get('action_count'))
    if result.get('total_ms') is not None:
        facts.append('耗时 %sms' % result.get('total_ms'))
    if result.get('done_reason'):
        facts.append('收手理由：%s' % result.get('done_reason'))
    # 时序账：慢系统必须知道「手到底泵动没有」——不然它会以为键按进去了，
    # 下一轮还照同一个打法再来一遍。这两条只在真有数据时写，绝不编数字。
    timing = result.get('timing') or {}
    if result.get('late_steps'):
        facts.append('有 %s 步按键晚了一拍以上（最晚 %sms，单拍 %sms）'
                     % (result.get('late_steps'), timing.get('max_lag_ms'), timing.get('step_ms')))
    # 卡住：慢系统必须知道「这一轮是被判卡住才交棒的」，否则它会把同一套打法
    # 再发一遍（原来只有快系统自述这一条路，模型不自知时就是死循环）。
    stuck = result.get('stuck') or {}
    if stuck.get('stuck'):
        facts.append('这一轮被判卡住：%s' % (stuck.get('why') or '画面或动作没有推进'))
    if result.get('dropped_steps'):
        reasons = sorted({str((entry.get('step_status') or {}).get('reason') or '')
                          for entry in (result.get('actions') or [])
                          if not (entry.get('step_status') or {}).get('pressed')} - {''})
        suffix = '（%s）' % '、'.join(reasons) if reasons else ''
        facts.append('有 %s 步的按键没真的生效%s' % (result.get('dropped_steps'), suffix))
    if facts:
        body = (body + NL + NL if body else '') + '（%s）' % '，'.join(facts)
    # 见闻有 120 字下限：不够就如实补充「我确定知道什么」，不许编画面内容。
    for padding in PAD_LINES:
        if len(body) >= OBSERVATIONS_MIN:
            break
        body = (body + NL + padding) if body else padding
    return body[:OBSERVATIONS_MAX]


class Streamer:
    '''一场直播的总装：只持有编排层与外围（弹幕 / 快系统会话 / 历史）。'''

    def __init__(
        self,
        *,
        orchestrator: Any,
        session: Any = None,
        feed: Any = None,
        constraints: Optional[Sequence[str]] = None,
        history_rounds: int = 30,
        root: Any = None,
        session_id: str = '',
        sink: Any = None,
        pump: Any = None,
    ) -> None:
        if orchestrator is None:
            raise StreamerError('没有编排层（orchestrator=None）：总装层不自己造一个')
        self.orchestrator = orchestrator
        self.session = session
        self.feed = feed
        self.constraints = [str(x) for x in (constraints or []) if str(x).strip()]
        self.history_rounds = max(1, int(history_rounds))
        self.history: List[str] = []
        self.rounds: List[Dict[str, Any]] = []
        self.warnings: List[str] = []
        self.root = Path(root) if root else None
        self.session_id = str(session_id or getattr(orchestrator, 'session_id', '') or '')
        self.last_report = ''
        self._danmaku_cursor = 0
        #: 事件总线消费端（event_sink.EventSink）：每轮抽干 mailbox，落 events.jsonl。
        #: 没有它就等于「事件只写不读」——事故复盘时答不出那一刻发生了什么。
        self.sink = sink
        #: 冻结泵（freeze_pump.FreezePump）：慢系统阻塞那 2s/10s 里继续喂最后一帧，
        #: 超过 900ms 贴上「大肥鱼CPU烧了」。没有它，录播会以为那段时间没流过。
        self.pump = pump

    # ------------------------------------------------------------ 一：预检
    def preflight(self, checks: Optional[Sequence[Any]] = None, timeout_ms: int = 3000) -> Any:
        '''六项预检（02 §7）；没给 checks 用默认探针。'''

        from preflight import run_preflight

        if checks is None:
            checks = self.default_checks()
        return run_preflight(checks, timeout_ms=timeout_ms)

    def default_checks(self) -> List[Any]:
        '''默认预检清单：能查的查，查不了的显式失败（不假装通过）。'''

        from preflight import (check_danmaku, check_disk, check_encoder, check_memory,
                               check_motor, check_omni, check_sticky, check_window)

        game = str(getattr(self.orchestrator, 'game', '') or '')
        save_id = str(getattr(self.orchestrator, 'save_id', '') or '')
        root = self.root or Path(str(getattr(self.orchestrator, 'root', '.') or '.'))
        checks: List[Any] = []

        def _no_feed():
            raise RuntimeError('没接弹幕服务（feed=None）')

        executor = getattr(self.orchestrator, 'executor', None)
        adapter = getattr(executor, 'adapter', None)
        grab = getattr(adapter, 'grab', None)
        checks.append(('游戏窗口', lambda: check_window(grab)))
        checks.append(('快系统', lambda: check_omni()))
        checks.append(('弹幕服务', (lambda: check_danmaku(self.feed)) if self.feed is not None else _no_feed))
        checks.append(('便利贴', lambda: check_sticky(game, save_id, root=self.root)))
        checks.append(('记忆', lambda: check_memory(getattr(self.orchestrator, 'memory', None))))
        checks.append(('磁盘', lambda: check_disk(str(root))))
        checks.append(('编码器', lambda: check_encoder()))
        motor = getattr(executor, 'motor', None)
        if motor is not None:
            checks.append(('手模型', lambda: check_motor(motor)))
        return checks

    # ------------------------------------------------------------ 二：弹幕
    def pull_danmaku(self, limit: int = 50) -> List[str]:
        '''只拉上次之后的新弹幕；服务挂了返回空表（不阻塞回合）。'''

        if self.feed is None:
            return []
        try:
            status = self.feed.fetch(self._danmaku_cursor, limit=limit)
        except Exception:  # noqa: BLE001 - 弹幕断了照常播，不能因此不玩
            return []
        items = getattr(status, 'items', None)
        if not items:
            return []
        texts: List[str] = []
        for item in items:
            text = ''
            if isinstance(item, dict):
                text = str(item.get('text') or item.get('content') or '').strip()
                t_ms = item.get('t_ms') or item.get('ts_ms')
                if isinstance(t_ms, int) and t_ms > self._danmaku_cursor:
                    self._danmaku_cursor = t_ms
            else:
                text = str(item or '').strip()
            if text:
                texts.append(text)
        return texts

    # ------------------------------------------------------------ 三：一个回合
    def round(self, report_summary: str = '', danmaku: Optional[Sequence[str]] = None,
              intent_id: Optional[str] = None, **exec_kwargs: Any) -> Dict[str, Any]:
        '''一个回合 + 抽干事件总线（每轮必有回执，事件不许烂在队列里）。'''

        outcome = self._do_round(report_summary=report_summary, danmaku=danmaku,
                                 intent_id=intent_id, **exec_kwargs)
        events = self.drain_events()
        outcome['events'] = len(events)
        if events:
            outcome['event_types'] = sorted({str(row.get('type') or '') for row in events})
        return outcome

    def drain_events(self) -> List[Dict[str, Any]]:
        '''把 mailbox 里登记的各类事件抽出来（落 events.jsonl），返回本轮新事件。'''

        if self.sink is None:
            return []
        try:
            rows = self.sink.drain()
        except Exception as error:  # noqa: BLE001 - 消费端故障不能停播
            self.warn('事件消费失败：%s: %s' % (type(error).__name__, error))
            return []
        if rows:
            for row in rows:
                if str(row.get('type')) == 'incident':
                    payload = row.get('payload') or {}
                    self.warn('事件流里有事故：%s（%s）' % (payload.get('kind') or '未知',
                                                          payload.get('reason') or ''))
        return rows

    def _plan_call(self, summary: str, danmaku_list: Sequence[str], frames: List[str]) -> Any:
        '''调编排层的 plan()。frames 只有对方接口认才传（并行开发期的版本差异）。'''

        kwargs: Dict[str, Any] = {
            'danmaku': list(danmaku_list) or None,
            'history': self.history[-self.history_rounds:] or None,
            'constraints': self.constraints or None,
        }
        if frames:
            accepts = False
            try:
                params = inspect.signature(self.orchestrator.plan).parameters
                accepts = 'frames' in params or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            except (TypeError, ValueError):
                accepts = False
            if accepts:
                kwargs['frames'] = list(frames)
            else:
                self.warn('编排层不接受 frames：这一轮慢系统看不到画面，只拿到文字见闻')
        return self.orchestrator.plan(summary, **kwargs)

    def latest_frames(self, limit: int = MAX_SLOW_FRAMES) -> List[str]:
        '''拿最近几帧真画面转成 data URL，给慢系统自己看（不转述）。

        顺序：执行器缓存的最近几帧（新的在后）-> 一帧都没有就现抓一张。
        抓不到就返回空列表，由上层如实记 warning —— 不许拿旧帧冒充「现在的画面」
        超过 30 秒（那会让慢系统的判断落在上一幕）。
        '''

        executor = getattr(self.orchestrator, 'executor', None)
        raw: List[bytes] = []
        cached = getattr(executor, 'last_frames', None)
        try:
            if cached is not None:
                raw = [bytes(x) for x in list(cached)[-int(limit):] if x]
        except Exception:  # noqa: BLE001 - 缓存坏了不挡回合，退回现抓
            raw = []
        if not raw:
            one = getattr(executor, 'last_frame', None)
            if one:
                raw = [bytes(one)]
        if not raw:
            adapter = getattr(executor, 'adapter', None)
            grab = getattr(adapter, 'grab', None)
            if callable(grab):
                try:
                    shot = grab() or {}
                    if shot.get('raw'):
                        raw = [bytes(shot['raw'])]
                except Exception as error:  # noqa: BLE001
                    self.warn('给慢系统抓帧失败：%s: %s' % (type(error).__name__, error))
        out: List[str] = []
        for data in raw[-int(limit):]:
            if not data:
                continue
            out.append('data:image/jpeg;base64,' + base64.b64encode(data).decode('ascii'))
        return out

    def _prime_pump(self) -> None:
        '''冻结泵还没有底片时，抓一张真画面垫上（不抓新内容、不改判定）。

        为什么必须有：冻结窗口里喂的是「最后一帧真实画面」。第一轮开播时还没有任何
        帧，泵的 available() 为假，第一次慢系统思考期完全没有帧可喂 —— 真机实测
        就是第一轮 freeze 结算为空（0 帧），录播那一段直接缺时间。
        '''

        pump = self.pump
        if pump is None:
            return
        again = getattr(pump, 'available', None)
        try:
            if callable(again) and again():
                return
        except Exception:  # noqa: BLE001 - 探测失败就按「没有帧」处理
            pass
        executor = getattr(self.orchestrator, 'executor', None)
        adapter = getattr(executor, 'adapter', None)
        grab = getattr(adapter, 'grab', None)
        if not callable(grab):
            self.warn('冻结泵没有底片，也没有采集口：第一次思考期画面会停住')
            return
        set_frame = getattr(pump, 'set_frame', None)
        if not callable(set_frame):
            return
        try:
            shot = grab() or {}
            data = shot.get('data') if isinstance(shot, dict) else getattr(shot, 'data', None)
            if data:
                set_frame(data)
            else:
                self.warn('冻结泵补底片失败：采集口没给画面')
        except Exception as error:  # noqa: BLE001 - 补底片失败不能挡回合
            self.warn('冻结泵补底片失败：%s: %s' % (type(error).__name__, error))

    def _freeze_summary(self, hold: Any, before: Dict[str, Any]) -> Dict[str, Any]:
        '''结算一次冻结窗口：时长 + 本轮增量帧数/贴图数（不是累计）。'''

        out: Dict[str, Any] = {}
        try:
            out.update(dict(hold.summary()))
        except Exception:  # noqa: BLE001 - 结算失败不能盖住慢系统的原始错误
            out['summary_error'] = True
        for key in ('frames', 'painted', 'errors'):
            now = int((getattr(self.pump, 'stats', {}) or {}).get(key) or 0)
            out[key] = now - int(before.get(key) or 0)
        return out

    def _do_round(self, report_summary: str = '', danmaku: Optional[Sequence[str]] = None,
                  intent_id: Optional[str] = None, **exec_kwargs: Any) -> Dict[str, Any]:
        '''拉弹幕 -> 规划 -> 开新会话 -> 执行 -> 交棒。任何一步失败都返回结论，不抛。'''

        danmaku_list = list(danmaku) if danmaku is not None else self.pull_danmaku()
        summary = report_summary or self.last_report
        # 慢系统要看真图：拿执行器最近几帧（没有就现抓一张），转 data URL 直接给它看。
        frames = self.latest_frames()
        freeze_stats: Dict[str, Any] = {}
        try:
            # 规划期画面冻结（01 §6）：慢系统阻塞期间由冻结泵按固定节奏继续喂帧，
            # 超 900ms 的帧会被贴图，录播与预览都不会出现「时间没流过」的空档。
            # 第一轮还没有任何帧时先补一张底片——否则第一次思考期根本无帧可喂（实测
            # 首轮 freeze 统计为 {}，录播会缺一整段）。
            self._prime_pump()
            if self.pump is not None and getattr(self.pump, 'available', lambda: False)():
                before_stats = dict(getattr(self.pump, 'stats', {}) or {})
                hold = None
                with self.pump.holding() as hold:
                    try:
                        plan = self._plan_call(summary, danmaku_list, frames)
                    finally:
                        # 慢系统抛错也要把冻结窗口的账结清（否则回执里看不出冻结过没）
                        freeze_stats = self._freeze_summary(hold, before_stats)
                elapsed = int(freeze_stats.get('elapsed_ms') or 0)
                if elapsed > FREEZE_WARN_MS and not freeze_stats.get('painted'):
                    self.warn('这一轮规划冻结 %dms 但一帧都没贴图（画面会停在原帧）' % elapsed)
            else:
                plan = self._plan_call(summary, danmaku_list, frames)
        except Exception as error:  # noqa: BLE001 - 慢系统超时/格式不对：留在本轮，不熔断
            return {
                'ok': False,
                'stage': 'plan',
                'error': '%s: %s' % (type(error).__name__, error),
                'danmaku': danmaku_list,
                'freeze': freeze_stats,
                'state': getattr(self.orchestrator, 'state', ''),
            }
        goal = str(getattr(plan, 'goal', '') or '')
        if self.session is not None:
            try:
                self.session.start()
            except Exception as error:  # noqa: BLE001 - 嘴哑了照样用手玩
                self.warn('快系统会话没开起来：%s: %s' % (type(error).__name__, error))
        started = time.monotonic()
        try:
            result = self.orchestrator.execute(plan, intent_id=intent_id, **exec_kwargs)
        except Exception as error:  # noqa: BLE001 - 熔断已由编排层留证，这里只收口
            session_stats = self._close_session()
            return {
                'ok': False,
                'stage': 'execute',
                'error': '%s: %s' % (type(error).__name__, error),
                'aborted': bool(getattr(error, 'bundle_dir', None)),
                'bundle_dir': getattr(error, 'bundle_dir', None),
                'goal': goal,
                'session': session_stats,
                'elapsed_ms': int((time.monotonic() - started) * 1000),
            }
        session_stats = self._close_session()
        outcome: Dict[str, Any] = {
            'ok': True,
            'stage': 'handoff',
            'intent_id': result.get('intent_id') or '',
            'result': result.get('result') or '',
            'ended': result.get('ended') or '',
            'goal': goal,
            'danmaku': danmaku_list,
            'freeze': freeze_stats,
            'session': session_stats,
            'executor': {
                'action_count': result.get('action_count'),
                'total_ms': result.get('total_ms'),
                'slow_steps': result.get('slow_steps'),
                'errors': list(result.get('errors') or []),
            },
        }
        handoff_reason = str(session_stats.get('handoff_reason') or '')
        conclusion = build_conclusion(result, plan, hint=handoff_reason)
        observations = build_observations(result, handoff=handoff_reason)
        session_stats_tools = {
            'events': session_stats.get('events') or 0,
            'handoff_requested': bool(session_stats.get('handoff_requested')),
            'reconnects': session_stats.get('reconnects') or 0,
            'degraded': bool(session_stats.get('degraded')),
        }
        try:
            payload = self.orchestrator.handoff(
                result,
                conclusion,
                observations,
                stats={'session': session_stats, 'tools': session_stats_tools,
                       'slow_steps': result.get('slow_steps') or 0},
            )
        except Exception as error:  # noqa: BLE001 - 回执写不出来要留痕，不能当成功
            outcome.update({'ok': False, 'stage': 'handoff_error',
                            'error': '%s: %s' % (type(error).__name__, error)})
            return outcome
        outcome['handoff'] = payload
        outcome['conclusion'] = conclusion
        outcome['observations_chars'] = len(observations)
        self.last_report = self.format_report(conclusion, observations)
        self.history.append('%s ｜ %s ｜ %s' % (goal[:30], outcome['result'], conclusion))
        self.history = self.history[-self.history_rounds:]
        self.rounds.append(outcome)
        return outcome

    def format_report(self, conclusion: str, observations: str) -> str:
        '''交给下一轮慢系统的回执摘要（一份 JSON：结构稳，慢系统好读）。'''

        payload = {'结论': conclusion, '见闻': observations}
        return json.dumps(payload, ensure_ascii=False)[:4000]

    def _close_session(self) -> Dict[str, Any]:
        if self.session is None:
            return {}
        stats: Dict[str, Any] = {}
        try:
            stats = dict(self.session.stats()) if hasattr(self.session, 'stats') else {}
        except Exception:  # noqa: BLE001
            stats = {}
        try:
            closer = getattr(self.session, 'close_intent', None) or getattr(self.session, 'stop', None)
            if callable(closer):
                closer()
        except Exception as error:  # noqa: BLE001
            stats['close_error'] = '%s: %s' % (type(error).__name__, error)
        return stats

    def warn(self, message: str) -> None:
        text = str(message or '').strip()
        if text:
            self.warnings.append(text)

    # ------------------------------------------------------------ 四：多回合
    def run(self, rounds: int = 1, max_rounds: Optional[int] = None, **kwargs: Any) -> Dict[str, Any]:
        '''跑 N 个回合；熔断立刻停（编排层已经留证），慢系统超时只记一次失败继续。'''

        total = int(max_rounds or rounds or 1)
        summary: Dict[str, Any] = {'rounds': [], 'aborted': False, 'ok': True}
        report = str(kwargs.pop('report_summary', '') or '')
        for index in range(total):
            outcome = self.round(report_summary=report, **kwargs)
            summary['rounds'].append(outcome)
            report = ''
            if outcome.get('aborted'):
                summary['aborted'] = True
                summary['ok'] = False
                summary['stopped_at'] = index + 1
                summary['bundle_dir'] = outcome.get('bundle_dir')
                break
        summary['round_count'] = len(summary['rounds'])
        summary['ok'] = bool(summary['ok']) and all(bool(r.get('ok')) for r in summary['rounds'])
        # 事件总线：整场累计，收尾时把残留的（下播那次的 intent.end 等）也抽干。
        summary['events'] = sum(int(r.get('events') or 0) for r in summary['rounds'])
        tail = self.drain_events()
        summary['events'] += len(tail)
        if self.sink is not None:
            try:
                summary['sink'] = self.sink.snapshot()
            except Exception as error:  # noqa: BLE001
                summary['sink'] = {'errors': ['%s: %s' % (type(error).__name__, error)]}
        return summary

    # ------------------------------------------------------------ 五：下播
    def signoff(self, summary: str = '', hook: str = '', lines: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        '''下播四件事：本场记忆、下播话术、录播索引、下一场钩子。'''

        out: Dict[str, Any] = {'summary': summary, 'lines': list(lines or []), 'hook': hook,
                               'warnings': []}
        memory = getattr(self.orchestrator, 'memory', None)
        if memory is not None and summary:
            try:
                out['memory'] = self.orchestrator.remember(
                    conclusion=summary,
                    result='reached',
                    stats={'rounds': len(self.rounds)},
                )
            except Exception as error:  # noqa: BLE001
                out['warnings'].append('本场记忆没写进去：%s: %s' % (type(error).__name__, error))
        index = getattr(self.orchestrator, 'index', None)
        if index is not None:
            try:
                save = getattr(index, 'save', None)
                if callable(save):
                    save()
                orphans = getattr(index, 'orphans', None)
                out['index'] = {
                    'saved': True,
                    'orphans': list(orphans()) if callable(orphans) else [],
                }
            except Exception as error:  # noqa: BLE001
                out['warnings'].append('录播索引没写进去：%s: %s' % (type(error).__name__, error))
        if hook:
            out['hook_file'] = str(self._write_hook(hook))
        if not out['lines']:
            out['lines'] = ['（没有准备下播话术）']
        tail = self.drain_events()
        if tail:
            out['events'] = len(tail)
        return out

    def _write_hook(self, hook: str) -> Path:
        root = self.root or Path(str(getattr(self.orchestrator, 'root', '.') or '.'))
        root.mkdir(parents=True, exist_ok=True)
        target = root / 'next_hook.json'
        payload = {'session_id': self.session_id, 'hook': hook,
                   'created_ms': int(time.time() * 1000)}
        tmp = target.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(str(tmp), str(target))
        return target


def build_streamer(orchestrator: Any, *, session: Any = None, feed: Any = None, **kwargs: Any) -> Streamer:
    '''从编排层出发造一个总装件（会话/弹幕没给就是单腿跑）。'''

    return Streamer(orchestrator=orchestrator, session=session, feed=feed, **kwargs)
