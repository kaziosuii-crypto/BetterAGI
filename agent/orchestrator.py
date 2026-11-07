# -*- coding: utf-8 -*-
'''编排层（设计文档 01_architecture.md §3/§5）：把「想 → 做 → 交棒 → 再想」串成一个循环。

编排层自己**不思考、不按键、不剪视频**，它只做四件事：

1. 状态机：IDLE → PLANNING → EXECUTING → HANDOFF → PLANNING …，非法跳转一律抛错；
2. 预算与冻结：慢系统思考时画面冻结（≤2s，超 900ms 由上层贴「大肥鱼CPU烧了」），冻结起止都进时间轴；
3. 落账：意图进 session_index.json，交棒进 handoffs/<intent>.json，动作进 timeline.jsonl；
4. 熔断：手模型解析失败 = 程序错误 → 留证 → 不许继续播。

所有协作者都是注入的（executor / planner / memory / danmaku / mailbox），没有就明确退化，绝不假装。
'''
from __future__ import annotations

import inspect
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

STATE_IDLE = 'IDLE'
STATE_PLANNING = 'PLANNING'
STATE_EXECUTING = 'EXECUTING'
STATE_HANDOFF = 'HANDOFF'
STATE_ABORTED = 'ABORTED'

ALLOWED = {
    STATE_IDLE: (STATE_PLANNING, STATE_ABORTED),
    STATE_PLANNING: (STATE_EXECUTING, STATE_HANDOFF, STATE_ABORTED),
    STATE_EXECUTING: (STATE_HANDOFF, STATE_ABORTED),
    STATE_HANDOFF: (STATE_PLANNING, STATE_ABORTED),
    STATE_ABORTED: (),
}

#: 执行结果 → 交棒结果（02 §3.1 的 result 枚举）。
RESULT_MAP = {
    'goal_reached': 'reached',
    'budget': 'blocked',
    'max_decisions': 'blocked',
    'no_action': 'blocked',
    #: 卡住（画面不动 / 同一套动作反复）：不是故障、也不是做到，是「换个思路」。
    'stuck': 'blocked',
    'no_motor': 'failed',
    'capture_error': 'failed',
    'motor_error': 'failed',
    'motor_timeout': 'failed',
    'motor_parse_error': 'aborted',
}

#: 这些 ending 说明「没进展」，连续出现就记卡关，卡关超阈值才允许开 thinking。
BLOCKED_ENDINGS = ('budget', 'max_decisions', 'no_action', 'stuck')
#: 这些 ending 是程序错误：留证 + 熔断，不许继续播。
FATAL_ENDINGS = ('motor_parse_error',)

FREEZE_SLOW_MS = 30000
# 慢系统预算已放到 30s（用户 2026-11-04 拍板），900ms 就喊贴图会变成噪音。
FREEZE_WARN_MS = 30000


class OrchestratorError(RuntimeError):
    '''编排层自己的状态机/接线错误。'''


class OrchestratorAborted(OrchestratorError):
    '''熔断：程序错误，已留证，必须断流下播。'''

    def __init__(self, kind: str, bundle_dir: str, reason: str = '') -> None:
        super().__init__('熔断（%s）：%s；留证在 %s' % (kind, reason or '程序错误', bundle_dir))
        self.kind = kind
        self.bundle_dir = bundle_dir
        self.reason = reason


@dataclass
class CycleResult:
    intent_id: str
    plan_goal: str = ''
    ended: str = ''
    result: str = ''
    intent_ms: int = 0
    handoff_file: str = ''
    blocked_count: int = 0
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'intent_id': self.intent_id,
            'plan_goal': self.plan_goal,
            'ended': self.ended,
            'result': self.result,
            'intent_ms': int(self.intent_ms),
            'handoff_file': self.handoff_file,
            'blocked_count': int(self.blocked_count),
            'warnings': list(self.warnings),
        }


def _motor_budget_ms(thinking: bool = False) -> int:
    '''没显式给预算时，按实测 p95 算（env BETTERAGI_MOTOR_STEP_MS 可覆盖）。'''

    try:
        import pacing

        return pacing.step_budget_ms(thinking)
    except Exception:
        return 200


class Orchestrator:
    '''一个编排层实例 = 一场直播的一条主循环。'''

    def __init__(
        self,
        root: Any,
        session_id: str,
        game: str = '',
        save_id: str = '',
        *, 
        timeline: Any = None,
        index: Any = None,
        mailbox: Any = None,
        executor: Any = None,
        planner: Any = None,
        memory: Any = None,
        incident_root: Any = None,
        auto_trip: bool = True,
        motor_budget_ms: Optional[int] = None,
        context_factory: Any = None,
        sticky_root: Any = None,
    ) -> None:
        from pathlib import Path

        self.root = Path(root)
        self.session_id = session_id
        self.game = game
        self.save_id = save_id
        self.timeline = timeline
        self.index = index
        self.mailbox = mailbox
        self.executor = executor
        self.planner = planner
        self.memory = memory
        self.incident_root = Path(incident_root) if incident_root else self.root / 'incidents'
        self.auto_trip = bool(auto_trip)
        self.state = STATE_IDLE
        self.history: List[Dict[str, Any]] = []
        self.blocked_count = 0
        self.intent_seq = 0
        self.last_plan: Optional[Any] = None
        #: 冻结贴图（设计 01 §6 / 03 §C.9）：编排层只发「该贴图了」的信号，
        #: 真正的 JPEG 渲染在 freeze_overlay.FreezePainter，不在这里做。
        self.freeze_overlay: Optional[Any] = None
        #: 本场编排层自己的告警（贴图提醒等），上层可原样展示/落账。
        self.warnings: List[str] = []
        #: 一次决策允许花的**墙钟**毫秒。200ms 是「游戏时间」（6x33ms），实测墙钟 p95
        #: 关思考 740ms / 开思考 1490ms（_diag/gs_5090_latency.py，5090 真服务）。
        #: None = 走 BETTERAGI_MOTOR_STEP_MS 或实测默认；显式给值优先。
        self.motor_budget_ms: Optional[int] = int(motor_budget_ms) if motor_budget_ms else None
        #: 每个意图一份手嘴通道（intent_context.IntentContext）。每意图新建 = 「上一动作」
        #: 不会跨意图污染；工厂为 None 时执行器保持自己的 context（默认 None，行为不变）。
        self.context_factory: Any = context_factory
        #: 便利贴根目录（profile 目录）。慢系统每轮都拿到全文，不再指望它自己 read_sticky。
        self.sticky_root: Any = sticky_root

    def set_freeze_painter(self, painter: Any) -> Any:
        '''挂上/摘下冻结贴图器；传 None = 关掉贴图提醒。

        只要求对象有可调用的 paint(jpeg)，这样测试和上层都能用轻量假件。
        '''

        if painter is not None and not callable(getattr(painter, 'paint', None)):
            raise OrchestratorError('冻结贴图器必须提供可调用的 paint(jpeg)')
        self.freeze_overlay = painter
        return painter

    def _warn_freeze_overlay(self, elapsed_ms: int) -> None:
        '''冻结窗口超过 FREEZE_WARN_MS 且有贴图器时，记一条给上层的贴图提醒。

        编排层不直接改画面、不改时间轴协议、不开线程：观众屏幕上的替换由上层做。
        '''

        if self.freeze_overlay is None or elapsed_ms <= FREEZE_WARN_MS:
            return
        text = str(getattr(self.freeze_overlay, 'text', '') or '大肥鱼CPU烧了')
        self.warnings.append(
            '本段冻结 %dms（超过 %dms），请上层贴图：%s' % (elapsed_ms, FREEZE_WARN_MS, text)
        )

    # ---- 状态机 --------------------------------------------------------

    def _to(self, state: str, reason: str = '') -> None:
        if state == self.state:
            return
        if state not in ALLOWED.get(self.state, ()):
            raise OrchestratorError('非法状态跳转：%s → %s（%s）' % (self.state, state, reason or '无原因'))
        previous, self.state = self.state, state
        self.history.append({'from': previous, 'to': state, 'reason': reason, 'ts': time.time()})
        if self.mailbox is not None:
            try:
                self.mailbox.put('state.change', {'from': previous, 'to': state, 'reason': reason})
            except Exception:  # noqa: BLE001 - 记账失败不许打断主循环
                pass

    @contextmanager
    def _frozen(self, reason: str):
        '''画面冻结的起止都要进时间轴：进了没出 = 现场可查。'''

        started = time.monotonic()
        if self.timeline is not None:
            self.timeline.event('freeze', reason=reason, ms=0, restored=False)
        try:
            yield
        finally:
            elapsed = int((time.monotonic() - started) * 1000)
            if self.timeline is not None:
                self.timeline.event('freeze', reason=reason, ms=elapsed, restored=True)

    # ---- 一：想 --------------------------------------------------------

    def _current_sticky(self) -> Any:
        '''读这一局当前的便利贴，供慢系统每轮直接看。读不到返回 None，不抛。'''

        try:
            import sticky as sticky_mod
        except Exception:  # noqa: BLE001 - 模块不在就当没有便利贴
            return None
        try:
            return sticky_mod.read_sticky(self.game, self.save_id, root=self.sticky_root)
        except Exception as error:  # noqa: BLE001
            self.warnings.append('便利贴读取失败：%s' % error)
            return None
    def plan(
        self,
        report_summary: str,
        memory_hits: Optional[List[str]] = None,
        danmaku: Optional[List[str]] = None,
        history: Optional[List[str]] = None,
        constraints: Optional[List[str]] = None,
        frames: Optional[List[str]] = None,
    ) -> Any:
        '''让慢系统出这一轮任务。冻结窗口严格包住这次调用。

        `frames` 是原始帧的 data URL 列表：慢系统**自己看图**，不接受快系统的
        文字转述（转述会丢细节，也会多一次串台的调用）。
        '''

        if self.planner is None:
            raise OrchestratorError('没接慢系统（planner=None）：不能自己编一个任务出来')
        # 记忆注入：调用方没给就用回执上下文自己去检索（02 §8.3：上下文原文当 query）。
        # 检索必须在 freeze 之前做完——它是 IO，不该算进慢系统的 2s 思考预算。
        if memory_hits is None:
            recalled = self.recall(report_summary)
            memory_hits = [recalled['text']] if recalled.get('text') else []
        self._to(STATE_PLANNING, 'request_plan')
        call: Dict[str, Any] = {
            'memory_hits': memory_hits,
            'danmaku': danmaku,
            'history': history,
            'constraints': constraints,
            'blocked_count': self.blocked_count,
            'last_plan': getattr(self.last_plan, 'goal', None),
        }
        if self._planner_accepts('sticky'):
            call['sticky'] = self._current_sticky()
        if frames and self._planner_accepts('frames'):
            call['frames'] = list(frames)
        elif frames:
            self.warnings.append('慢系统接口不收 frames：这一轮它看不到画面，只拿到文字见闻')
        freeze_started = time.monotonic()
        with self._frozen('slow_think'):
            plan = self.planner.plan(report_summary, **call)
        #: 冻结窗口结束：超过 900ms 且有贴图器时提醒上层把这一帧换成「大肥鱼CPU烧了」。
        self._warn_freeze_overlay(int((time.monotonic() - freeze_started) * 1000))
        self.last_plan = plan
        if hasattr(plan, 'warnings') and plan.warnings and self.timeline is not None:
            self.timeline.event('freeze', reason='slow_think_warning', ms=len(plan.warnings), restored=True)
        return plan

    # ---- 二：做 --------------------------------------------------------

    def execute(self, plan: Any, intent_id: Optional[str] = None, done_when: str = '', **kwargs: Any) -> Dict[str, Any]:
        '''把 plan 交给快系统/手模型执行。返回 executor.run() 的原样结果（不修饰）。'''

        if self.executor is None:
            raise OrchestratorError('没接执行器（executor=None）：不能假装在玩')
        goal = str(getattr(plan, 'goal', '') or '').strip()
        if not goal:
            raise OrchestratorError('plan 里没有「这一轮做什么」：这轮没法执行')
        if getattr(plan, 'needs_rewrite', False):
            raise OrchestratorError('plan 的 task 超过 400 字（在写剧本）：必须退回重写，不许截断执行')
        self._to(STATE_EXECUTING, 'dispatch')
        intent_id = self._next_intent_id(intent_id)
        done = done_when or str(getattr(plan, 'success', '') or '')
        if self.index is not None:
            self.index.intent_start(intent_id, goal[:40], started_ms=self._now_ms())
        if self.timeline is not None:
            self.timeline.event('intent_start', intent_id=intent_id, goal=goal[:80])
        if self.context_factory is not None and hasattr(self.executor, 'context'):
            try:
                self.executor.context = self.context_factory()
            except Exception as error:  # noqa: BLE001
                self.warnings.append('这一轮没建手嘴通道：%s: %s' % (type(error).__name__, error))
        call = dict(kwargs)
        if call.get('deadline_ms') is None:
            call['deadline_ms'] = self.motor_budget_ms or _motor_budget_ms()
        call['intent_id'] = intent_id
        # 意图起止由编排层统一落时间轴：执行器再落一遍就会出现两条 intent_start，
        # 验收口径（每意图成对一条）当场破功。执行器独立使用时仍由它自己落。
        if self._executor_accepts('emit_intent_bounds'):
            call.setdefault('emit_intent_bounds', False)
        if self.timeline is not None:
            call.setdefault('timeline', self.timeline)
        started = time.monotonic()
        #: 执行器只认一句话意图（executor.run(intent: str, ...)）：传 goal 而不是整包 dict。
        result = self.executor.run(goal, done, **call)
        intent_ms = int((time.monotonic() - started) * 1000)
        ended = str(result.get('ended') or '')
        mapped = RESULT_MAP.get(ended, 'failed')
        result = dict(result)
        result.update({'intent_id': intent_id, 'result': mapped, 'intent_ms': intent_ms,
                       'plan_goal': goal, 'done_when': done,
                       'plan_avoid': str(getattr(plan, 'avoid', '') or '')})
        if ended in BLOCKED_ENDINGS:
            self.blocked_count += 1
        elif ended == 'goal_reached':
            self.blocked_count = 0
        result['blocked_count'] = self.blocked_count
        if self.index is not None:
            self.index.intent_end(intent_id, mapped, ended_ms=self._now_ms())
            # 意图一结束就落一次盘：进程被强杀时，索引里至少要能看到「做到哪一轮」。
            save_index = getattr(self.index, 'save', None)
            if callable(save_index):
                try:
                    save_index()
                except Exception as error:  # noqa: BLE001 - 落盘失败要留痕，不许假装成功
                    result.setdefault('errors', []).append('session_index 落盘失败：%s' % error)
        if self.timeline is not None:
            self.timeline.event('intent_end', intent_id=intent_id, ended=ended, result=mapped, intent_ms=intent_ms)
        if ended in FATAL_ENDINGS and self.auto_trip:
            bundle = self.trip(ended, reason=str((result.get('error_detail') or {}).get('message') or '手模型解析失败'))
            raise OrchestratorAborted(ended, bundle, reason='手模型解析失败=程序错误')
        return result

    def _planner_accepts(self, name: str) -> bool:
        '''慢系统吃不吃某个关键字参数（同样是并行开发期的版本差异）。'''

        plan = getattr(self.planner, 'plan', None)
        if not callable(plan):
            return False
        try:
            params = inspect.signature(plan).parameters
        except (TypeError, ValueError):
            return False
        return name in params or any(
            param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()
        )

    def _executor_accepts(self, name: str) -> bool:
        '''执行器吃不吃某个关键字参数（并行开发期各实现版本不一致，别硬传）。'''

        run = getattr(self.executor, 'run', None)
        if not callable(run):
            return False
        try:
            params = inspect.signature(run).parameters
        except (TypeError, ValueError):
            return False
        return name in params or any(
            param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()
        )

    def _next_intent_id(self, given: Optional[str]) -> str:
        if given:
            self.intent_seq += 1
            return str(given)
        self.intent_seq += 1
        return '%s-%03d' % (self.session_id, self.intent_seq)

    def _now_ms(self) -> int:
        '''编排层不另取时钟：优先用时间轴那把尺子。'''

        if self.timeline is not None and hasattr(self.timeline, 'clock'):
            try:
                return int(self.timeline.clock.now_ms())
            except Exception:  # noqa: BLE001
                pass
        return 0

    # ---- 记忆（读走 memory_wire 的检索，写走 add_episode；编排层不另写一套） ----

    def recall(self, goal: str = '', hint: str = '', top_k: int = 8) -> Dict[str, Any]:
        '''检索记忆并压成一段可注入文本；没接记忆库就返回空块（绝不编内容）。'''

        block: Dict[str, Any] = {'text': '', 'hits': 0, 'warnings': []}
        if self.memory is None:
            block['warnings'].append('没接记忆库（memory=None）：这一轮不注入记忆')
            return block
        try:
            from memory_wire import format_recall, recall_for

            episodes = recall_for(self.memory, goal=goal, hint=hint, top_k=top_k)
            block['text'] = format_recall(episodes)
            block['hits'] = len(episodes)
        except Exception as error:  # noqa: BLE001 - 记忆检索失败不许把直播拖停
            block['warnings'].append('记忆检索失败：%s: %s' % (type(error).__name__, error))
        return block

    def remember(
        self,
        *,
        conclusion: str,
        observations: str = '',
        result: str = 'failed',
        intent_id: str = '',
        atoms: Optional[List[Dict[str, Any]]] = None,
        stats: Optional[Dict[str, Any]] = None,
        layer: str = 'episodic',
    ) -> Dict[str, Any]:
        '''把这一轮回执落进长期记忆（每轮一条 episode，可带原子事实）。

        写不进去时返回 stored=False + 原因，绝不假装写成功（设计口径：记忆不是硬依赖，
        但「以为记住了其实没有」是直播事故）。
        '''

        if self.memory is None:
            return {'stored': False, 'reason': '没接记忆库（memory=None）'}
        text = ' '.join(part for part in (str(conclusion or '').strip(), str(observations or '').strip()) if part)
        if not text.strip():
            return {'stored': False, 'reason': '回执是空的：没有可记的内容'}
        try:
            from memory import Episode, sanitize_group_id

            gid = sanitize_group_id(self.game)
            episode = Episode(
                text=text[:2000],
                kind='progress',
                group_id=gid,
                group_ids=[gid],
                source='slow',
                layer=layer,
                atoms=list(atoms or []),
                meta={'intent_id': intent_id, 'result': result, 'game': self.game,
                      'save_id': self.save_id, 'session_id': self.session_id,
                      'stats': dict(stats or {})},
            )
            stored = self.memory.add_episode(episode)
        except Exception as error:  # noqa: BLE001 - 写记忆失败要能被看见
            return {'stored': False, 'reason': '%s: %s' % (type(error).__name__, error)}
        return {'stored': True, 'id': str(getattr(stored, 'id', '') or ''),
                'layer': str(getattr(stored, 'layer', layer) or layer), 'chars': len(text)}

    # ---- 三：交棒 ------------------------------------------------------

    def handoff(
        self,
        result: Dict[str, Any],
        conclusion: str,
        observations: str,
        media: Optional[Dict[str, Any]] = None,
        stats: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        '''落一份 HandoffReport：慢系统下一轮只看这一份。'''

        from handoff import HandoffReport, write_report

        self._to(STATE_HANDOFF, result.get('ended') or 'handoff')
        intent_id = str(result.get('intent_id') or '')
        report = HandoffReport(
            intent_id=intent_id,
            result=str(result.get('result') or 'failed'),
            conclusion=conclusion,
            observations=observations,
            session_id=self.session_id,
            media=dict(media or {}),
            timing={
                'started_ms': 0,
                'ended_ms': 0,
                'cycles': int((result.get('stats') or {}).get('decisions') or 0),
                'slowest_step_ms': max([int(x) for x in (result.get('decide_ms') or [0])] or [0]),
                'intent_ms': int(result.get('intent_ms') or 0),
            },
            stats=dict(stats or {}),
            last_frame=str((media or {}).get('last_frame') or result.get('last_frame') or ''),
        )
        # write_report 的 root 是「整场直播的父目录」（run/），session_id 才是这一场；
        # 编排层的 root 已经是 run/<session_id>/，所以这里显式降一层，免得写出
        # run/<sid>/<sid>/handoffs（剪辑按 02 §2 的布局会找不到）。
        run_root = self.root.parent if self.root.name == self.session_id else self.root
        payload = write_report(report, run_root, self.session_id)
        if self.timeline is not None:
            self.timeline.event('handoff', intent_id=intent_id, result=report.result,
                                conclusion=report.conclusion[:120], file=payload.get('file') or '')
        if self.mailbox is not None:
            try:
                self.mailbox.put('intent.end', {
                    'intent_id': intent_id,
                    'result': report.result,
                    'conclusion': report.conclusion,
                    'observations': report.observations,
                    'file': payload.get('file') or '',
                })
            except Exception as error:  # noqa: BLE001
                payload.setdefault('warnings', []).append('intent.end 没投进 mailbox：%s' % error)
        # 落账：每轮回执写进长期记忆（慢系统下一轮读的是它，不是这一份 JSON）。
        stored = self.remember(
            conclusion=report.conclusion,
            observations=report.observations,
            result=report.result,
            intent_id=intent_id,
            stats=dict(stats or {}),
        )
        payload['memory'] = stored
        if not stored.get('stored'):
            payload.setdefault('warnings', []).append(
                '这一轮没写进记忆：%s' % (stored.get('reason') or '未说明')
            )
        return payload

    # ---- 四：熔断 ------------------------------------------------------

    def trip(self, kind: str, reason: str = '', extra: Optional[Dict[str, Any]] = None) -> str:
        '''留证 + 进时间轴 + 状态 ABORTED。返回留证目录（字符串）。

        已经 ABORTED 再熔断 = 调用方逻辑错了（会写第二份证、覆盖「最后一条事件」），
        所以这里直接抛错，不静默返回。
        '''

        if self.state == STATE_ABORTED:
            raise OrchestratorError('已经熔断（ABORTED）：不许再熔断一次（会破坏「最后一条事件」约束）')
        from incident import IncidentBundle, prune

        bundle = IncidentBundle(self.incident_root, kind=kind, reason=reason, extra=dict(extra or {}))
        bundle.proc_snapshot({'state': self.state, 'blocked_count': self.blocked_count,
                              'intents_done': len([h for h in self.history if h.get('to') == STATE_HANDOFF])})
        bundle.env_snapshot({'game': self.game, 'save_id': self.save_id, 'session_id': self.session_id})
        if self.timeline is not None:
            try:
                bundle.timeline_slice(self.timeline.rows(), anchor_ms=self._now_ms())
            except Exception:  # noqa: BLE001
                pass
        bundle.write_json('parsed.json', {'kind': kind, 'reason': reason, 'state': self.state,
                                          'blocked_count': self.blocked_count})
        bundle.readme(summary='触发：%s；原因：%s' % (kind, reason or '（未填写）'))
        if self.index is not None:
            try:
                self.index.save()
            except Exception:  # noqa: BLE001
                pass
        #: 熔断是最后一条事件，之后不许再有 game_action（断言器 A8）。
        if self.timeline is not None:
            self.timeline.event('incident', {'kind': kind, 'dir': str(bundle.path), 'reason': reason[:200]})
        self._to(STATE_ABORTED, 'trip:' + kind)
        try:
            prune(self.incident_root)
        except Exception:  # noqa: BLE001
            pass
        return str(bundle.path)
