# -*- coding: utf-8 -*-
'''把设计文档那套东西装成一个能跑的入口：预检 -> 回合循环 -> 下播。

用法（在 E:/BetterAGI/agent 下）：

    uv run python run_stream.py --profile <游戏 profile 目录> --rounds 1 --dry-run
    uv run python run_stream.py --profile ... --rounds 3

--dry-run 只做装配与预检，不碰游戏窗口、不调手模型：用来在开播前确认
「便利贴合法 / 弹幕服务活着 / 磁盘够 / 手模型在线」，也就是 02 §7 的六项预检。

装配顺序刻意固定（每一层都只依赖上一层的接口，不互相 new）：
    profile -> adapter / motor / 快系统会话 / 慢系统 / 记忆 -> 编排层 -> 总装层
'''
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_FPS = 4.0
RUN_ROOT_ENV = 'BETTERAGI_RUN_ROOT'


def _session_id() -> str:
    return 'run-' + time.strftime('%Y%m%d-%H%M%S')


def _run_root(profile: Any, root: Optional[Any] = None) -> Path:
    if root:
        return Path(root)
    from_env = os.environ.get(RUN_ROOT_ENV, '').strip()
    if from_env:
        return Path(from_env)
    base = Path(getattr(profile, 'dir', '.') or '.')
    return base / 'runs'


#: 语义键名 → 手模型训练词表里的写法（历史串与键位表都用它）。
_VOCAB_TOKENS = {
    'z': 'Z', 'x': 'X', 'c': 'C', 'a': 'A', 'w': 'W', 's': 'S', 'd': 'D',
    'enter': 'Enter', 'return': 'Enter', 'backspace': 'Back', 'space': 'Space',
    'up': 'Up', 'down': 'Down', 'left': 'Left', 'right': 'Right',
    'escape': 'Esc', 'esc': 'Esc', 'tab': 'Tab', 'shift': 'Shift',
}

#: 写进 Game Rules 的硬事实：实测模型会输出 LB/RB（手柄键）与鼠标位移，而这两个接口在
#: 键盘游戏上按不出任何效果 → 一整轮全是「空按」，画面纹丝不动。
KEY_FACTS = ('- Only the keys listed in Common Keys exist on this keyboard. Gamepad '
             'buttons (LB / RB / MB) and the mouse do not exist in this game; never '
             'emit them.')

#: 优先出现在键位表里的语义键（剩下的是 profile 学到的零碎键，排后面）。
_SEMANTIC_KEYS = ('confirm', 'cancel', 'back', 'up', 'down', 'left', 'right',
                  'menu', 'submit', 'erase', 'interact')


def _vocab_token(text: Any) -> str:
    '''把物理键名翻成训练词表写法；认不出就原样返回（别猜）。'''

    raw = str(text or '').strip()
    if not raw:
        return ''
    return _VOCAB_TOKENS.get(raw.lower(), raw)


def key_lines(profile: Any, limit: int = 16) -> list:
    '''从 profile 的 keymap.json 生成手模型的 Common Keys 行（官方 `- X -> action` 风格）。

    为什么手模型必须拿到这张表：实测（_diag/probe_think_matrix.py 2026-11-04）它对
    **字面键名**敏感——Common Keys 段只有一行时，它稳定输出 6 组空动作；把真实键位
    恢复成十几行之后才肯出 Up/Down/Z。语义键（confirm/back/up…）排前面，学到的零碎
    字母键排后面，避免把名字输入界面里学到的 l/u/m/i 当成主要操作。
    '''

    rows = []
    try:
        bindings = profile.load_keymap()
    except Exception:  # noqa: BLE001 - 没有 keymap 就是没有补充键位，不是错误
        return rows
    ordered = sorted(
        bindings or [],
        key=lambda item: (_SEMANTIC_KEYS.index(str(getattr(item, 'key', '')).lower())
                          if str(getattr(item, 'key', '')).lower() in _SEMANTIC_KEYS else 99),
    )
    seen = set()
    for binding in ordered:
        if len(rows) >= limit:
            break
        name = str(getattr(binding, 'key', '') or '').strip()
        try:
            physical = list(binding.physical_keys() or [])
        except Exception:  # noqa: BLE001 - 绑定形状怪就跳过这一条
            physical = []
        token = _vocab_token(physical[0] if physical else name)
        # 纯「学到的原始按键」（key 名等于物理键，如名字输入界面学到的 l/u/m/i）不算动作名，
        # 放进 Common Keys 只会诱导模型在游戏里乱按；语义键（confirm/up/…）才进表。
        if token.lower() == name.lower() and name.lower() not in _SEMANTIC_KEYS:
            continue
        if not token or token.lower() in seen:
            continue
        seen.add(token.lower())
        rows.append('- %s -> %s' % (token, name or token))
    return rows



def assemble(profile: Any, *, root: Optional[Any] = None, session_id: str = '',
             fps: float = DEFAULT_FPS, narrate_every_s: float = 1.0,
             done_confirm: int = 2, step_ms: int = 33,
             with_session: bool = True, record: Optional[bool] = None,
             freeze_text: str = '', motor_budget_ms: Optional[int] = None) -> Dict[str, Any]:
    '''按 profile 装配一场直播的全部零件。缺哪个就显式报哪个，不静默降级。'''

    import event_sink as sink_mod
    import intent_context as context_mod
    import mailbox as mailbox_mod
    import motor as motor_mod
    import motor_wire
    import freeze_pump as pump_mod
    import record as record_mod
    import recorder
    import steptrace as steptrace_mod
    import stuck as stuck_mod
    import timeline as timeline_mod
    from adapter import ProfileAdapter
    from danmaku_feed import build_feed
    from executor import FastExecutor
    from fast_session import build_intent_session
    from memory import build_memory
    from orchestrator import Orchestrator
    from slow_planner import build_slow_planner
    from streamer import Streamer

    session_id = str(session_id or _session_id())
    run_root = _run_root(profile, root)
    sess_root = run_root / session_id
    sess_root.mkdir(parents=True, exist_ok=True)

    game = str(getattr(profile, 'display_name', '') or getattr(profile, 'game_id', '') or '')
    game_id = str(getattr(profile, 'game_id', '') or game)
    save_id = str(os.environ.get('BETTERAGI_SAVE_ID', '') or 'default')

    timeline = timeline_mod.Timeline(sess_root, session_id)
    index = recorder.SessionIndex(sess_root, session_id, game_id, save_id,
                                 int(time.time() * 1000))
    mailbox = mailbox_mod.Mailbox()

    adapter = ProfileAdapter(profile)
    motor = motor_mod.build_motor()

    warnings = []
    session = None
    if with_session:
        try:
            session = build_intent_session(fps=fps)
        except Exception as error:  # noqa: BLE001 - 嘴没接上照样用手玩
            warnings.append('快系统会话没起来：%s: %s' % (type(error).__name__, error))
        if session is None:
            warnings.append('没有 DashScope key：这场只有手，没有嘴')
    narrator = session

    sticky_root = getattr(profile, 'dir', None)
    bindings = key_lines(profile)

    def prompt_for(goal: str = '') -> Dict[str, Any]:
        '''按「本意图目标」现拼手模型 system（慢系统的 goal 必须进 Game Rules 段）。

        为什么每次现拼：model 的 user 轮固定是 "current game screen"，任务信息只能
        走 system；executor 只构造一次，字符串形式会让整场都在执行第一个意图的目标。
        '''
        return motor_wire.build_motor_system(
            game=game_id, save_id=save_id, root=sticky_root,
            objective=goal, key_lines=bindings,
            extra_rules=KEY_FACTS,
        )

    prompt = prompt_for('')
    warnings.extend(prompt.get('warnings') or [])
    if not bindings:
        warnings.append('profile 的 keymap.json 没有可用键位：手模型只看到便利贴里那几行')

    # 卡住检测（01 §7 卡关）：没有 Pillow 就退化为「只靠快系统自述」，不阻断开播。
    stuck = stuck_mod.build_stuck()
    if stuck is None:
        warnings.append('没有 Pillow：这一场不自动判卡住，只靠快系统自述')

    # 三段计时账本：run/<sid>/steps.jsonl（编帧 / 上传+推理 / 按键）。
    trace = steptrace_mod.build_trace(str(sess_root), session_id=session_id)
    executor = FastExecutor(
        adapter, motor, narrator,
        motor_prompt=lambda goal='': prompt_for(goal),
        game=game_id,
        narrate_every_s=narrate_every_s,
        done_confirm=done_confirm,
        step_ms=step_ms,
        stuck=stuck,
        trace=trace,
    )
    planner = build_slow_planner()
    memory = build_memory(profile)

    orchestrator = Orchestrator(
        root=sess_root, session_id=session_id, game=game_id, save_id=save_id,
        timeline=timeline, index=index, mailbox=mailbox,
        executor=executor, planner=planner, memory=memory,
        incident_root=run_root / 'incidents', motor_budget_ms=motor_budget_ms,
        context_factory=context_mod.IntentContext,
        # 便利贴每轮塞进慢系统输入（2026-11-04：它原来根本不读，只在瞎试）。
        sticky_root=getattr(profile, 'dir', None),
    )
    # 冻结贴图（01 §6 / 03 §C.9）：只有显式给了文案才挂，空串 = 本场不贴图。
    freeze = None
    label = str(freeze_text or '').strip()
    if label:
        from freeze_overlay import FreezePainter

        freeze = FreezePainter(text=label)
        orchestrator.set_freeze_painter(freeze)
    # 录播：先起 ffmpeg，再把 tap 挂到采集上（一次抓帧喂两处，不重复采）。
    want_record = record if record is not None else \
        (os.environ.get('BETTERAGI_RECORD', '').strip().lower() in ('1', 'true', 'yes', 'on'))
    writer = None
    if want_record:
        writer = record_mod.SegmentWriter(sess_root, fps=fps, index=index, start_ms=0)
        try:
            writer.start()
        except Exception as error:  # noqa: BLE001 - 录播起不来不掐直播
            warnings.append('录播没起来：%s: %s' % (type(error).__name__, error))
            writer = None
    # 冻结泵（01 §6）：慢系统阻塞期间按 fps 继续喂最后一帧；超 900ms 贴「大肥鱼CPU烧了」。
    # 它有两个口子：set_frame 收采集旁路的帧，tap 往录播写冻结帧。
    pump = pump_mod.FreezePump(tap=(writer.write if writer is not None else None),
                               painter=freeze, fps=fps, text=label)
    taps = [pump.set_frame]
    if writer is not None:
        taps.insert(0, writer.write)
    adapter.frame_tap = taps[0] if len(taps) == 1 else pump_mod.FrameTee(*taps)
    feed = build_feed()
    # 事件消费端：mailbox 只写不读等于没有事件总线，这里挂上消费者（run/<sid>/events.jsonl）。
    sink = sink_mod.build_sink(mailbox, root=str(sess_root))
    streamer = Streamer(orchestrator=orchestrator, session=session, feed=feed,
                        root=sess_root, session_id=session_id, sink=sink, pump=pump,
                        constraints=['别剧透：只能依据记忆里已知的内容说话'])
    return {
        'session_id': session_id,
        'root': sess_root,
        'game': game_id,
        'save_id': save_id,
        'timeline': timeline,
        'index': index,
        'mailbox': mailbox,
        'sink': sink,
        'trace': trace,
        'adapter': adapter,
        'motor': motor,
        'session': session,
        'executor': executor,
        'planner': planner,
        'memory': memory,
        'orchestrator': orchestrator,
        'freeze': freeze,
        'feed': feed,
        'streamer': streamer,
        'recorder': writer,
        'pump': pump,
        'prompt_source': prompt.get('source'),
        'key_lines': len(bindings),
        'warnings': warnings,
    }


def _pacing_note() -> Dict[str, Any]:
    '''把「200ms 是游戏时间」这笔账摊开写进结果里，别让人再按 200ms 设墙钟预算。'''

    try:
        import pacing

        return {'no_thinking': pacing.pacing(False), 'thinking': pacing.pacing(True),
                'note': pacing.describe(False)}
    except Exception as error:  # noqa: BLE001
        return {'error': '%s: %s' % (type(error).__name__, error)}


def run(profile: Any, *, rounds: int = 1, root: Optional[Any] = None,
        signoff_summary: str = '', hook: str = '', **kwargs: Any) -> Dict[str, Any]:
    '''装起来跑 N 轮，跑完下播。返回一份可以直接 json.dumps 的结论。'''

    parts = assemble(profile, root=root, **kwargs)
    streamer = parts['streamer']
    pre = streamer.preflight()
    report: Dict[str, Any] = {
        'session_id': parts['session_id'],
        'root': str(parts['root']),
        'motor_budget_ms': parts.get('orchestrator').motor_budget_ms
        if parts.get('orchestrator') is not None else None,
        'pacing': _pacing_note(),
        'preflight': pre.to_dict() if hasattr(pre, 'to_dict') else str(pre),
        'preflight_ok': bool(getattr(pre, 'ok', False)),
        'warnings': list(parts['warnings']),
    }
    def _stop_record() -> Dict[str, Any]:
        writer = parts.get('recorder')
        if writer is None:
            return {}
        try:
            return writer.stop()
        except Exception as error:  # noqa: BLE001
            return {'ok': False, 'error': '%s: %s' % (type(error).__name__, error)}

    if not report['preflight_ok']:
        report['stopped'] = 'preflight_failed'
        report['record'] = _stop_record()
        return report
    summary = streamer.run(rounds=rounds)
    report['rounds'] = [
        {k: v for k, v in row.items() if k in ('ok', 'stage', 'intent_id', 'result', 'ended',
                                               'conclusion', 'error', 'aborted', 'bundle_dir')}
        for row in summary['rounds']
    ]
    report['aborted'] = summary['aborted']
    # 每步三段计时的中位数（编帧 / 上传+推理 / 按键），来自 run/<sid>/steps.jsonl。
    trace = parts.get('trace')
    if trace is not None:
        try:
            report['steps'] = trace.summary()
        except Exception as error:  # noqa: BLE001
            report['steps'] = {'error': '%s: %s' % (type(error).__name__, error)}
    report['record'] = _stop_record()
    report['signoff'] = streamer.signoff(summary=signoff_summary or '这一场结束', hook=hook)
    return report


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description='BetterAGI 直播回合入口（预检 / 跑 / 下播）')
    parser.add_argument('--profile', required=True, help='游戏 profile 目录名或绝对路径')
    parser.add_argument('--rounds', type=int, default=1)
    parser.add_argument('--root', default='', help='这一场 run 的父目录（默认 profile/runs）')
    parser.add_argument('--session-id', default='')
    parser.add_argument('--dry-run', action='store_true', help='只装配+预检，不执行')
    parser.add_argument('--no-session', action='store_true', help='不接快系统（只有手）')
    parser.add_argument('--hook', default='', help='下一场钩子')
    parser.add_argument('--record', action='store_true', help='开录播（ffmpeg 分片 10 分钟一片）')
    args = parser.parse_args(argv)

    profile = _resolve_profile(args.profile)
    if args.dry_run:
        parts = assemble(profile, root=args.root or None, session_id=args.session_id,
                         with_session=not args.no_session)
        report = parts['streamer'].preflight()
        print(json.dumps({'session_id': parts['session_id'], 'root': str(parts['root']),
                          'prompt_source': parts['prompt_source'],
                          'warnings': parts['warnings'],
                          'preflight': report.to_dict()}, ensure_ascii=False, indent=2))
        return 0 if getattr(report, 'ok', False) else 2

    extra: Dict[str, Any] = {}
    if args.no_session:
        extra['with_session'] = False
    if args.record:
        extra['record'] = True
    result = run(profile, rounds=max(1, args.rounds), root=args.root or None,
                 hook=args.hook, **extra)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result.get('aborted') and result.get('preflight_ok') else 1


def _resolve_profile(value: str) -> Any:
    '''按目录名或绝对路径找到 Profile（不猜、不造）。'''

    import profiles

    path = Path(value)
    if path.exists() and path.is_dir():
        # 目录优先：直接读该目录所属 registry 里的元数据（别名/显示名/上限都在那儿）。
        registry = profiles.load_registry(path.parent)
        meta = (registry.get('games') or {}).get(path.name) or {}
        return profiles.Profile(path.parent, path.name, meta)
    try:
        return profiles.open_profile(value)
    except profiles.ProfileError as error:
        raise SystemExit(str(error))


if __name__ == '__main__':
    sys.exit(main())
