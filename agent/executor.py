#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快系统执行器：一条视频流上，手和嘴并行干活。

用户口径（2026-11-04，三次纠偏后的最终分工）：

- **手 = gamescaling**：吃当前帧，输出的是操作数据（XYZ 鼠标 + 6 组按键，每步 33ms，
  一次决策覆盖未来 200ms），执行器把它真的按下去；
- **嘴 + 裁判 = Omni**：同一条视频流上边走边说，产出带时间码的解说，并判断这次操作
  是否该收手；
- **操作结束后回给慢系统的不是逐帧读屏**，而是：所有解说 + 每条的时间码 + 这一段的
  情况总结。

所以慢系统永远看不到 confirm/up 这种"选键"——那是手的内部节拍，不是决策。执行器也
不假设任何具体游戏：怎么按由 gamescaling 决定，什么时候算完由 Omni 判定，帧从
adapter 来。
"""

from __future__ import annotations

import base64
import inspect
import os
import re
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

#: 慢系统一次最多看几张真帧（与 slow_planner.MAX_SLOW_FRAMES 同口径）。
MAX_SLOW_FRAMES = 3

from intent_context import IntentContext, describe_action

#: 解说与收手判定合成一次调用：每轮既说一句画面，也判断是否该停（省一半请求）。
#: 用户口径（2026-11-04）：「加一个参数，要求结束回合的时候必须给出结束的理由」——
#: 光给 {mark} 不算收手，必须紧跟一句理由，指出画面上哪处证据成立。
#: 没有理由的 {mark} 会被 _split_done 判为无效并计入 done_rejected（实测裁判会
#: 在「他们用一道魔法咒文」这种剧情推进中幻觉早停，理由能把它逼回证据上）。
NARRATE_INSTRUCTIONS = (
    "你在实时看着游戏画面，不是在看剧情简介。玩家想做的事：{intent}\n"
    "硬规则（用户 2026-11-05 明确：不许编故事）：\n"
    "- 你只看得见这一帧。玩家说的意图、完成条件、你自己猜的剧情，都不是画面上发生的事实。\n"
    "- 不许编造角色名、对白、剧情、地名，也不许说 NPC 说了什么做了什么——画面上没有的一个字都不能说。\n"
    "- 不许把意图当成已经发生的事。意图写「走到村口」，画面上没到村口就不能说到了。\n"
    "- 看不出来就写「看不太清」，画面没动就写「画面基本没变」。\n"
    "只做两件事，不要给操作建议、不要讲攻略：\n"
    "1) 用一句不超过 25 字的中文说画面上真实变化了什么（界面变了什么、角色往哪挪了多少）；"
    "几乎没变化就写「画面基本没变」。\n"
    "2) 只有下面这条完成条件在画面里明确成立时才收手，写法必须是：句末先写 {mark}，"
    "紧接着写「理由：」和一句话，把画面上直接看得见的东西写出来——屏幕上有字就原样引用，"
    "用「」括起来。没有可见证据就不要写 {mark}；只写 {mark} 不带理由，会被当成条件没达成。\n"
    "完成条件：{done_when}\n"
    "只输出那一句话，不要解释判断过程。"
)

SUMMARY_INSTRUCTIONS = (
    "刚才这一段操作的内容如下（按时间顺序的解说）：\n{lines}\n\n"
    "意图：{intent}\n最终状态：{ended}\n"
    "用不超过 60 字总结这一段时间里整体发生了什么（从什么画面开始、中间发生了什么、"
    "现在停在哪）。只说解说里出现过的事实，不要给下一步建议，不要补写解说里没有的剧情或角色。"
)

DONE_MARK = "[DONE]"

#: 理由至少要有的字数：一个词（"是"/"done"）不算证据。
MIN_DONE_REASON_CHARS = 4

#: 屏幕上的字被原样引用时的括法（「」『』“”""）。静态画面上收手必须带这么一段，
#: 否则模型就是在「凭感觉宣布做完了」——实测它会把意图当画面事实（用户 2026-11-05 投诉）。
QUOTE_RE = re.compile(r"[「『“\"][^」』”\"]{1,24}[」』”\"]")
#: 两个帧指纹相差几位以内算「画面没动」（8x8 平均哈希，抖动容忍 3 位）。
STATIC_HAMMING = 3

KEY_LATCH_ENV = "BETTERAGI_KEY_LATCH"


def _key_latch() -> bool:
    """跨决策保持按住（默认开）。

    为什么：一次决策大约 600~700ms（4B 单步实测 613/653/670/698ms），而动作本身
    只有 6x33ms=198ms 游戏时间；决策期间把键松开，角色就是「走一步停两拍」。
    用户 2026-11-05 明确要求「一次决定的直到下一次之前按键一直按着」。
    设 BETTERAGI_KEY_LATCH=0 可退回旧行为（每步松开）。
    """
    raw = str(os.environ.get(KEY_LATCH_ENV) or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


#: 手模型连续解析失败几次才放弃（第一次坏 JSON 只跳过这一步重来）。
PARSE_RETRY_LIMIT = 2
#: 一个动作最多按住多久（Game2Player 的 duration 可能给到 2 秒；上限防模型写成 60 秒）。
MAX_HOLD_MS = 3000
#: 画面变化量小于这个值算「画面没动」。取「分块最大平均差」而不是整幅平均：
#: 整幅平均会把「光标挪了一格」这种小面积真变化摊平成 0.5，导致把真变化说成没动。
#: 实测（640x480，裁掉顶部主播立绘）：静止对 < 3.3，光标从 A 挪到 s 是 42.3。
STATIC_DELTA = 8.0
#: 同一套动作连着重复几次就判定「不过脑子地重复按键」（用户 2026-11-06 明确要求）。
#: 只跟「画面相对本次意图起点几乎没动」一起用：走对路时画面在变，不会误伤行走。
REPEAT_STEPS_LIMIT = 6
#: 重复守卫用的变化阈值：比 STATIC_DELTA 更严（真的没动才算，避免把对话小变化当静止）。
REPEAT_STATIC_DELTA = 4.0
#: 帧签名栅格：裁掉顶部立绘区再缩到 64x33 灰度，按 8x4 分块取最大块平均差。
SIG_W, SIG_H = 64, 33
SIG_CELL_W, SIG_CELL_H = 8, 4
SIG_CROP_TOP = 150
#: 解说里表示「没变化」的说法。逐帧比对说没动、解说却说动了，就是编故事。
NO_CHANGE_HINTS = ("没变", "未变", "没动", "没有变化", "毫无变化", "一样", "相同", "看不清")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _frame_fingerprint(raw: Any) -> str:
    """帧指纹（8x8 平均哈希），用来判断「画面到底动没动」。

    没有 Pillow 时返回空串：调用方看到空串就跳过静态帧判定——宁可放过一次
    幻觉，也不能因为缺依赖把正常收手全拒了。
    """
    if not raw:
        return ""
    try:
        import io
        from PIL import Image
        image = Image.open(io.BytesIO(raw)).convert("L").resize((9, 8))
        pixels = list(image.getdata())
    except Exception:  # noqa: BLE001 - 依赖缺失/坏帧都不该影响决策
        return ""
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if pixels[base + col] > pixels[base + col + 1] else 0)
    return "%016x" % bits


def _frame_signature(raw: Any) -> bytes:
    """帧签名（64x33 灰度缩略图，裁掉顶部主播立绘）：量「画面到底变了多少」。

    比 8x8 平均哈希细，且先裁掉顶部立绘区——立绘在动不代表游戏画面在动。
    拿不到 Pillow 时返回 b""，调用方据此跳过静态判定。
    """
    if not raw:
        return b""
    try:
        import io
        from PIL import Image
        image = Image.open(io.BytesIO(raw)).convert("L")
        width, height = image.size
        top = min(SIG_CROP_TOP, max(0, height // 3))
        if height - top >= 32:
            image = image.crop((0, top, width, height))
        return image.resize((SIG_W, SIG_H)).tobytes()
    except Exception:  # noqa: BLE001 - 依赖缺失/坏帧都不该影响决策
        return b""


def _signature_delta(first: bytes, second: bytes) -> float:
    """两张帧签名的「分块最大平均差」（0-255）；拿不到返回 -1（表示「不知道」）。

    取最大块而不是整体平均：光标挪一格只有几百个像素变化，整体平均会被摊到 0.5，
    反而判成「没动」；最大块能保住这块真变化，同时静止画面的抖动仍接近 0。
    """
    if not first or not second or len(first) != len(second) or len(first) != SIG_W * SIG_H:
        return -1.0
    worst = 0.0
    for cell_y in range(0, SIG_H, SIG_CELL_H):
        for cell_x in range(0, SIG_W, SIG_CELL_W):
            total = 0
            count = 0
            for row in range(cell_y, min(cell_y + SIG_CELL_H, SIG_H)):
                base = row * SIG_W
                for col in range(cell_x, min(cell_x + SIG_CELL_W, SIG_W)):
                    total += abs(first[base + col] - second[base + col])
                    count += 1
            if count:
                worst = max(worst, total / float(count))
    return worst


def _claims_no_change(text: str) -> bool:
    """这句话是不是在说「画面没变」。"""
    body = str(text or "")
    return any(hint in body for hint in NO_CHANGE_HINTS)


def _fingerprint_gap(first: str, second: str) -> int:
    """两个指纹差几位；任一方为空或格式不对返回 -1（表示「不知道」）。"""
    if not first or not second or len(first) != len(second):
        return -1
    try:
        return bin(int(first, 16) ^ int(second, 16)).count("1")
    except ValueError:
        return -1


def _echo_goal(reason: str, goal: str) -> bool:
    """理由是不是只是把目标复述了一遍（把意图当画面事实）。

    判据很土但有效：理由里的汉字有 80% 以上能在目标里找到，就算复述。
    目标为空时一律不算复述。
    """
    left = _CJK_RE.findall(str(reason or ""))
    right = set(_CJK_RE.findall(str(goal or "")))
    if len(left) < 3 or not right:
        return False
    hit = sum(1 for ch in left if ch in right)
    return hit / float(len(left)) >= 0.8


def _split_done(text: str, *, require_reason: bool = True, goal: str = "",
                static: Optional[bool] = None) -> Dict[str, Any]:
    """把一轮解说拆成 (解说正文, 是否收手, 收手理由, 拒绝原因)。

    规则：出现 DONE_MARK 且（不要求理由 或 理由 ≥ MIN_DONE_REASON_CHARS 字）才算收手。
    理由会被剥掉前缀（"理由："/"原因:"）后返回，方便直接回报给慢系统。
    """
    raw = str(text or "").strip()
    index = raw.find(DONE_MARK)
    if index < 0:
        return {"text": raw, "done": False, "reason": "", "rejected": ""}
    before = raw[:index].strip()
    after = raw[index + len(DONE_MARK):].strip()
    reason = after
    for prefix in ("理由：", "理由:", "原因：", "原因:", "Reason:", "reason:", "because "):
        if reason.startswith(prefix):
            reason = reason[len(prefix):].strip()
            break
    if not reason:
        return {"text": before or raw, "done": False, "reason": "", "rejected": "no_reason"}
    if require_reason and len(reason) < MIN_DONE_REASON_CHARS:
        return {"text": before or raw, "done": False, "reason": reason,
                "rejected": "reason_too_short"}
    # 理由只是把目标念了一遍 = 把意图当画面事实（实测：「高亮字母从A移动到了H」而画面根本没动）。
    if _echo_goal(reason, goal):
        return {"text": before or raw, "done": False, "reason": reason,
                "rejected": "echo_goal"}
    # 画面从头到尾没动过，理由里又没有引用屏幕上的字：这只能是猜的。
    if static and not QUOTE_RE.search(reason):
        return {"text": before or raw, "done": False, "reason": reason,
                "rejected": "static_no_evidence"}
    return {"text": before or "（只看画面）", "done": True, "reason": reason, "rejected": ""}


def _classify_motor_error(error: BaseException) -> str:
    """把手侧异常分成三类：motor_parse_error / motor_timeout / motor_error。

    「模型输出坏」和「网络等超时」必须分开：前者要留模型原文（raw_text）复盘，
    后者要改超时/预算，混成一句 motor_error 就什么也查不出来。
    motor 的异常类可能还没加载（并行开发期、旧版本），所以先按真实类型认，
    认不到再按 type(exc).__name__ / MRO 类名认。
    """
    try:
        from motor import MotorParseError, MotorTimeout  # type: ignore
    except Exception:  # noqa: BLE001 - motor 不可用时退回按类名判断
        MotorParseError = None  # type: ignore
        MotorTimeout = None  # type: ignore
    if MotorParseError is not None and isinstance(error, MotorParseError):
        return "motor_parse_error"
    if MotorTimeout is not None and isinstance(error, MotorTimeout):
        return "motor_timeout"
    names = [type(error).__name__] + [base.__name__ for base in type(error).__mro__]
    if "MotorParseError" in names:
        return "motor_parse_error"
    for name in names:
        if name == "MotorTimeout" or name.endswith("Timeout"):
            return "motor_timeout"
    return "motor_error"


def _step_timing(step_results: Any, step_ms: int) -> Dict[str, Any]:
    '''把 play_steps 的 step_results 压成一本时序账（设计 03 §C.13 的消费方）。

    late 的口径是「晚了整整一拍」：lag_ms > step_ms 才算，因为 33ms 内的抖动
    本来就在调度精度里，拿它当缺陷会天天报警。没有 step_results（旧 adapter /
    纯离线替身）返回空 dict——不许伪造 0 延迟。
    '''
    rows = [row for row in (step_results or []) if isinstance(row, dict)]
    if not rows:
        return {}
    lags = [int(row.get('lag_ms') or 0) for row in rows]
    late = [index for index, lag in enumerate(lags) if lag > int(step_ms)]
    return {
        'steps': len(rows),
        'late_steps': len(late),
        'max_lag_ms': max(lags),
        'mean_lag_ms': int(round(sum(lags) / float(len(lags)))),
        'step_ms': int(step_ms),
    }


def _run_timing(actions: Any, step_ms: int) -> Dict[str, Any]:
    '''整段运行的按键时序总账：所有步的 lag 合起来算一次。'''
    rows = []
    for entry in actions or []:
        timing = (entry or {}).get('timing') or {}
        if timing:
            rows.append({'lag_ms': timing.get('max_lag_ms') or 0,
                         'pressed': True})
    if not rows:
        return {}
    lags = [int(row['lag_ms']) for row in rows]
    return {
        'decisions': len(lags),
        'max_lag_ms': max(lags),
        'mean_lag_ms': int(round(sum(lags) / float(len(lags)))),
        'step_ms': int(step_ms),
    }


def _aggregate_step_status(step_results: Any) -> Dict[str, Any]:
    '''一次决策 6 步 -> 一条 history 注记：全部落地才写 pressed ok。

    手模型每个意图只看到 5 条历史，一条历史对应一次决策（6 步），所以这里只能
    给整段一个状态：任一步没落地就不许说 ok，原因取第一个非空（no_window_focus
    比 skipped_keys 更值得模型看见）。没有 step_results 就返回空结论，不编状态。
    '''
    rows = [row for row in (step_results or []) if isinstance(row, dict)]
    if not rows:
        return {}
    pressed = all(bool(row.get('pressed')) for row in rows)
    reason = ''
    for row in rows:
        text = str(row.get('skipped_reason') or '').strip()
        if text:
            reason = text
            break
    return {'pressed': pressed, 'step': len(rows), 'reason': reason}

class FastExecutor:
    """把"手"和"嘴"按同一条时间轴跑起来。

    motor：gamescaling 客户端（decide(frame_url, instruction, game, system_prompt)）。
    narrator：Omni 连续视频流（push(jpeg_bytes) / ask(instructions)），可为 None。
    """

    def __init__(
        self,
        adapter: Any,
        motor: Any = None,
        narrator: Any = None,
        *,
        motor_prompt: Any = "",
        game: str = "",
        motion_instruction: str = "",
        on_event: Optional[Any] = None,
        narrate_every_s: float = 1.0,
        done_confirm: int = 2,
        step_ms: int = 33,
        clock: Any = None,
        require_done_reason: bool = True,
        context: Optional[IntentContext] = None,
        stuck: Optional[Any] = None,
        trace: Optional[Any] = None,
        retry_empty: int = 1,
    ) -> None:
        self.adapter = adapter
        self.motor = motor
        self.narrator = narrator
        #: 手模型 system 提示词：可以是字符串，也可以是 callable(intent) -> str。
        #: callable 是必须的：慢系统每一轮的 goal 要进 Game Rules 段，而 executor
        #: 只构造一次——string 形式等于整场都用第一个意图的目标（实测退化成空动作）。
        self.motor_prompt = motor_prompt or ""
        #: 本段真正发出去的那条 system（run() 里按意图解析后赋值）。
        self.motor_prompt_text = "" if callable(motor_prompt) else str(motor_prompt or "")
        self.game = game
        #: 「运动阶段」的固定指令（不按键、只走位）。实测策略模型要靠**字面键名**才出键，
        #: 语义化的意图（"往前走"）会让它输出全零；所以运动指令里也带真实键名，
        #: 但明确是本局可以自由使用的那一类（方向键），不会误触 confirm 推进剧情。
        self.motion_instruction = motion_instruction or ""
        self.on_event = on_event
        #: 卡住检测器（agent/stuck.py，可为 None）。判到就收手交棒，不靠模型自述。
        self.stuck = stuck
        self.stuck_last: Dict[str, Any] = {}
        #: 最近一帧真实画面（JPEG 原始字节）。
        self.last_frame: Optional[bytes] = None
        #: 最近几帧真图（新的在后）。慢系统规划时**直接看这些图**——不经过快系统转述。
        self.last_frames: Deque[bytes] = deque(maxlen=MAX_SLOW_FRAMES)
        self.narrate_every_s = max(0.2, float(narrate_every_s))
        self.done_confirm = max(1, int(done_confirm))
        self.step_ms = int(step_ms)
        #: 收手必须给理由（用户口径 2026-11-04）。关掉只在单测里为了构造旧行为。
        self.require_done_reason = bool(require_done_reason)
        #: 手嘴共信信箱（设计 03 §C.6 / D.2）。None = 与改造前完全一致：嘴不知道
        #: 手刚按了什么，老调用方（agent.py / run_stream.py）不需要改一行。
        self.context = context
        #: 每步三段计时账本（agent/steptrace.py，可为 None）。用户口径 2026-11-04：
        #: 别再拿「单次上传秒数」当带宽 —— 编帧 / 上传+推理 / 按键要分开记。
        self.trace = trace
        #: 空动作重采样次数（2026-11-04 用户口径「不操作就是不操作」）：模型这一步没出键
        #: 时，允许把同一个问题再问一遍（换一次采样），**只采纳模型自己出的键**；
        #: 再问也没有就一步都不按，绝不替它编动作。
        self.retry_empty = max(0, int(retry_empty))
        #: 跨决策保持按住（用户 2026-11-05 口径）：这一步最后一组的键不松开，
        #: 一直按到下一次决策回来（BETTERAGI_KEY_LATCH=0 可关掉）。
        self.key_latch = _key_latch()
        #: 测试可以塞一个假时钟；生产就是 time.time。
        self.clock = clock or time.time
        #: 每步 motor.decide 的执行期预算（毫秒），run() 可按意图覆盖。
        self.deadline_ms = 300
        #: 当前意图 id（时间轴归属用），run() 里赋值。
        self.intent_id = ""
        self.commentary: List[Dict[str, Any]] = []
        self.actions: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ 基础
    def _emit(self, event: str, **data: Any) -> None:
        handler = self.on_event
        if not callable(handler):
            return
        try:
            handler(event, data)
        except Exception:  # noqa: BLE001 - 播报坏了绝不能影响操作
            return

    def _frame_url(self) -> Dict[str, Any]:
        """取当前帧，返回 (data_url, error)。内存里转 base64，不落盘。

        顺带把两段本地时间拆开记（steptrace 用）：t_capture=截帧，t_encode=base64。
        合起来才是「本地编帧成本」，不能和上传/推理混成一个数。
        """
        started = time.monotonic()
        try:
            frame = self.adapter.grab()
            data = frame.pop("data")
        except Exception as error:  # noqa: BLE001 - 截帧失败如实记录
            return {"url": "", "error": "%s: %s" % (type(error).__name__, error), "meta": {}}
        captured = time.monotonic()
        if not data:
            return {"url": "", "error": "截帧返回空数据", "meta": {}}
        # 嘴的收手门：记下「这一帧长什么样」与「意图开始时那一帧」，
        # 用来识别「画面根本没动却说做完了」（_split_done 的 static 判定）。
        signature = _frame_signature(data)
        if signature:
            if not getattr(self, "_intent_sig", b""):
                self._intent_sig = signature
            self._last_sig = signature
        meta = {k: v for k, v in frame.items() if k not in ("data", "jpeg_b64")}
        url = "data:image/jpeg;base64," + base64.b64encode(data).decode()
        encoded = time.monotonic()
        return {"url": url, "error": "", "meta": meta, "raw": data,
                "t_capture_ms": (captured - started) * 1000.0,
                "t_encode_ms": (encoded - captured) * 1000.0,
                "payload_bytes": len(data)}

    # ------------------------------------------------------ 手嘴共信（IntentContext）
    def _record_action(self, step_index: int, keys: List[str], decide_ms: int,
                       t_ms: int, note: str = "") -> None:
        """把这一步记进 IntentContext；记账坏了绝不能影响按键（同 _emit 的口径）。"""
        context = self.context
        if context is None:
            return
        try:
            context.record_action(step_index=step_index, keys=keys, decide_ms=decide_ms,
                                  t_ms=t_ms, note=note)
        except Exception:  # noqa: BLE001 - 嘴的记账不能拖垮手
            return

    def _action_brief(self) -> str:
        """最近一步动作的一句话（拼在嘴的 instructions 末尾）；没 context / 没动作返回 ""。"""
        context = self.context
        if context is None:
            return ""
        try:
            return describe_action(context.last_action()).strip()
        except Exception:  # noqa: BLE001 - 拿不到动作就当没有，绝不打断解说
            return ""

    def _context_snapshot(self) -> Dict[str, Any]:
        """run() 附带返回的上下文快照；快照坏了退回空 dict，不抛。"""
        context = self.context
        if context is None:
            return {}
        try:
            return dict(context.snapshot())
        except Exception:  # noqa: BLE001
            return {}

    def _narrate_loop(self, intent: str, done_when: str, stop: threading.Event,
                      done: threading.Event, streak: Dict[str, int],
                      errors: List[str]) -> None:
        """嘴：按节拍说话；只有连续 done_confirm 次看到 DONE 才算收手（防幻觉早停）。"""
        if self.narrator is None:
            return
        while not stop.is_set():
            tick = self.clock()
            instructions = NARRATE_INSTRUCTIONS.format(
                intent=intent or "（未写明）",
                mark=DONE_MARK,
                done_when=done_when or "（没有给完成条件，只有时间预算）",
            )
            # 手在第 k 步做的事必须进嘴的眼睛：否则嘴看到的是「当前帧」而不是
            # 「第 k 步那帧」，收手判断天然滞后一个动作（设计 03 §D.2 修法 3）。
            # 只有注入 context 且手确实走过一步时才追加，未注入时逐字不变。
            brief = self._action_brief()
            if brief:
                instructions = instructions + "\n" + brief
            try:
                text = str(self.narrator.ask(instructions) or "").strip()
            except Exception as error:  # noqa: BLE001 - 解说断了只记一次，别刷屏
                message = "%s: %s" % (type(error).__name__, error)
                if not errors or errors[-1] != message:
                    errors.append(message)
                stop.wait(self.narrate_every_s)
                continue
            if not text:
                stop.wait(self.narrate_every_s)
                continue
            # 收手门：目标复述 + 静态画面无证据，两种都被判为幻觉早停。
            # 判据是像素：意图开始时那一帧 vs 现在这一帧的平均绝对差。
            delta = _signature_delta(getattr(self, "_intent_sig", b""),
                                     getattr(self, "_last_sig", b""))
            static = 0 <= delta <= STATIC_DELTA
            parsed = _split_done(text, require_reason=self.require_done_reason,
                                 goal=intent, static=static)
            # 像素说没动、解说却说动了 = 编故事（实测：命名界面上「名字从 AAAAAA 减为 AAAAA」，
            # 而按键其实是 Right 在挪光标）。以像素为准，改成实话留给下一轮与观众。
            if static and not parsed["done"] and not _claims_no_change(parsed["text"]):
                claim = str(parsed["text"])
                parsed = {"text": "画面基本没变（解说说变化了，但逐帧比对显示画面没动）",
                          "done": False, "reason": "", "rejected": "static_claim"}
                note = "解说编了变化（static_claim，Δ%.2f）：%s" % (max(delta, 0.0), claim[:60])
                if not errors or errors[-1] != note:
                    errors.append(note)
            hit = bool(parsed["done"])
            body = str(parsed["text"])
            if parsed["rejected"]:
                # 只喊收手不给理由 = 幻觉早停。留痕但不算达成，让 hand 继续按。
                note = "收手被拒（%s）：%s" % (parsed["rejected"], text[:60])
                if not errors or errors[-1] != note:
                    errors.append(note)
            self.commentary.append({
                "t_ms": int((self.clock() - self.started) * 1000),
                "text": body,
                "done_mark": hit,
                "done_reason": parsed["reason"],
                "done_rejected": parsed["rejected"],
            })
            if self.context is not None:
                # 嘴写账：留正文（DONE 标记与理由在 self.commentary 的结构化字段里）。
                try:
                    self.context.record_commentary(body, self.commentary[-1]["t_ms"])
                except Exception:  # noqa: BLE001 - 记解说失败不影响解说本身
                    pass
            self._emit("operator_note", t_ms=self.commentary[-1]["t_ms"], text=body, done=hit,
                       reason=parsed["reason"], rejected=parsed["rejected"])
            if hit:
                streak["done"] += 1
                streak["reason"] = str(parsed["reason"])
                if streak["done"] >= self.done_confirm:
                    done.set()
                    return
            else:
                streak["done"] = 0
            # 节拍对齐：这一轮说话花掉的时间算进来，别越说越密。
            wait = self.narrate_every_s - (self.clock() - tick)
            if wait > 0:
                stop.wait(wait)

    def _motor_accepts(self, name: str) -> bool:
        """motor.decide 吃不吃某个关键字参数（deadline_ms / new_intent）。

        旧客户端没有这些参数，硬传会 TypeError 把整段操作打崩；签名探不出来
        （C 扩展 / 装饰器把签名藏了）时按「不支持」处理——宁可少一个执行期预算，
        也不能在热路径上抛 TypeError。
        """
        decide = getattr(self.motor, "decide", None)
        if not callable(decide):
            return False
        try:
            params = inspect.signature(decide).parameters
        except (TypeError, ValueError):
            return False
        if name in params:
            return True
        return any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values())

    def _motor_accepts_deadline(self) -> bool:
        return self._motor_accepts("deadline_ms")

    # ------------------------------------------------------- 提示词与空动作兜底
    def _resolve_prompt(self, intent: str, errors: List[str]) -> str:
        """按本意图解析手模型 system 提示词（callable 时现拼，字符串时原样）。"""

        source = self.motor_prompt
        if not callable(source):
            return str(source or "").strip()
        try:
            text = source(intent or "")
        except Exception as error:  # noqa: BLE001 - 拼提示词失败要说出来，不能静默空提示词
            errors.append("手模型提示词拼装失败：%s: %s" % (type(error).__name__, error))
            return ""
        if isinstance(text, dict):
            # motor_wire.build_motor_system 返回的是 dict：顺手把 warnings 带进来。
            for item in text.get("warnings") or []:
                note = "手模型提示词：%s" % item
                if note not in errors:
                    errors.append(note)
            text = text.get("text") or ""
        return str(text or "").strip()

    def _play_steps(self, steps: List[List[str]], step_ms: Optional[int] = None,
                    keep_held: Optional[bool] = None) -> Dict[str, Any]:
        """按键。keep_held 只传给真正支持的 adapter（假件/旧实现不受影响）。

        keep_held 的条件是「这一步最后一组还有键」：最后一组是空组（模型显式等的
        那一步）时不能按住不放，否则角色会一直往那个方向走。

        step_ms：这一组按住的时长。Game2Player 的动作自带 duration（「按住 2 秒
        往右走」），不传就把 2000ms 摊成 33ms 的碎拍，动作语义就丢了。
        """
        play = self.adapter.play_steps
        ms = int(step_ms or self.step_ms)
        if keep_held is None:
            keep = bool(self.key_latch and steps and steps[-1])
        else:
            keep = bool(keep_held and steps and steps[-1])
        if keep:
            try:
                params = inspect.signature(play).parameters
            except (TypeError, ValueError):
                params = {}
            if "keep_held" in params:
                return play(steps, step_ms=ms, keep_held=True)
        return play(steps, step_ms=ms)

    def _release_held(self) -> None:
        """松开跨决策按住的键。收手/异常退出必调，否则角色一直往一个方向走。"""
        release = getattr(self.adapter, "release_held", None)
        if callable(release):
            try:
                release()
            except Exception:  # noqa: BLE001 - 松开失败不该盖掉真正的结束原因
                pass

    def _call_decide(self, frame_url: str, instruction: str,
                     pass_deadline: bool, new_intent: bool = False) -> Dict[str, Any]:
        """问手要这一步的动作；新 motor 带执行期预算，旧 motor 原样调用。

        new_intent：一个意图的第一次决策。Game2Player/gamescaling 都要在换意图时
        清历史（官方 README：一局/一段结束 policy.reset()），否则上一段的历史动作
        会被当成这一步的上下文，模型会接着上一段的思路乱按。
        """
        kwargs: Dict[str, Any] = {
            "instruction": instruction,
            "game": self.game,
            "system_prompt": self.motor_prompt_text or None,
        }
        if pass_deadline:
            kwargs["deadline_ms"] = self.deadline_ms
        if new_intent and self._motor_accepts("new_intent"):
            kwargs["new_intent"] = True
        return self.motor.decide(frame_url, **kwargs)

    def _record_history(self, frame_url: str, decision: Dict[str, Any],
                        status: Dict[str, Any], fallback_reason: str = '') -> Dict[str, Any]:
        """把这一轮（帧 + 真正执行的动作 + 泵没泵动）写回手的历史。

        为什么必须有这一步：gamescaling 的输入是「当前帧 + 前 5 帧 + 当时动作」，
        history 是它唯一能自纠的通道。改造后执行器一直没调 motor.record()，
        等于每步都在「无历史」条件下决策（history_len 永远是 0）：模型看不出
        上一步被窗口吞了，也看不出自己在原地重复按同一个键。

        返回 {'ok': bool, ...} 只用于留痕；写历史失败绝不能中断按键。
        """
        record = getattr(self.motor, 'record', None)
        if not callable(record):
            return {'ok': False, 'reason': '手没有 record()'}
        text = str((decision or {}).get('history_text') or '').strip()
        if not text:
            return {'ok': False, 'reason': 'decision 里没有 history_text'}
        pressed = status.get('pressed')
        step = status.get('step')
        reason = str(status.get('reason') or fallback_reason or '')
        try:
            try:
                record(frame_url, text, pressed=pressed, step=step, skipped_reason=reason)
            except TypeError:
                # 旧 motor 的 record 只收两个位置参数：状态注记没了也要把历史写进去。
                record(frame_url, text)
        except Exception as error:  # noqa: BLE001 - 写历史坏了不能把这一段打崩
            return {'ok': False, 'reason': '%s: %s' % (type(error).__name__, error)}
        return {'ok': True, 'pressed': pressed, 'step': step, 'reason': reason}
    # ------------------------------------------------------------------ 主循环
    def run(self, intent: str, done_when: str = "", budget_s: float = 20.0,
            max_decisions: int = 60, deadline_ms: int = 200, intent_id: str = "",
            timeline: Any = None, emit_intent_bounds: bool = True) -> Dict[str, Any]:
        """跑一段「意图 → 操作 → 收手」。返回解说（带时间码）、动作、结束原因、总结。

        deadline_ms：每步 motor.decide 的执行期预算（毫秒）。超预算只计入 slow_steps，
        不判失败——模型慢不等于模型错，但慢到超预算必须能被统计出来。
        intent_id：时间轴上把这一步动作归到哪个意图。
        timeline：timeline.Timeline 实例；不传就完全不发事件（它不是硬依赖）。
        """
        self.deadline_ms = max(1, int(deadline_ms))
        self.intent_id = str(intent_id or "")
        self.commentary = []
        self.actions = []
        self.started = self.clock()
        #: 意图级帧签名：每个意图重新取基准，否则上一轮的画面会污染这一轮的静态判定。
        self._intent_sig = b""
        self._last_sig = b""
        errors: List[str] = []
        #: 每个意图重新解析一次手模型提示词：goal 变了，system 必须跟着变。
        self.motor_prompt_text = self._resolve_prompt(intent, errors)
        #: 每步 motor 耗时（毫秒），与 actions[i]["decide_ms"] 同源。
        decide_ms_list: List[int] = []
        #: 超过 deadline_ms 的步数（只统计，不判失败）。
        slow_steps = 0
        #: 按键时序晚了一整拍（lag_ms > step_ms）的累计步数：这是「按键真的按在
        #: 计划时间点上吗」的唯一证据，也是手模型下一轮自纠的依据。
        late_steps = 0
        dropped_steps = 0
        #: 每次决策写回手历史的结果（{ok, pressed, step, reason}），与 actions 对齐。
        history_log: List[Dict[str, Any]] = []
        #: 手失败时的异常诊断（None 表示手没失败）；errors 只留前三行进结果，细节在这里。
        error_detail: Optional[Dict[str, Any]] = None
        #: stop = 执行器要求嘴收口（run 结束时必置位，单向命令，主循环不读它）；
        #: done = 嘴连续确认收手（只有嘴会置位，是主循环唯一的正常退出信号）。
        stop = threading.Event()
        done = threading.Event()
        streak = {"done": 0, "empty": 0, "reason": ""}
        # 手模型解析失败的重试额度：连续坏输出到上限才认输（一次坏 JSON 不判死）。
        parse_failures = 0
        parse_failures_total = 0
        ended = "max_decisions"
        reached = False
        #: 一旦失败就锁死：绝不允许被并发置位的 done 翻成 goal_reached。
        failed = False
        #: 意图本身已经做到（广播器命中过至少一次）→ 后续决策转成自由走位。
        reached_intent = False
        mind = None
        #: 旧 motor 没有 deadline_ms，一次性探明，循环里不再反复 inspect。
        pass_deadline = self.motor is not None and self._motor_accepts_deadline()

        def _tl(kind: str, **payload: Any) -> None:
            """落一条时间轴事件；没传 timeline 就什么都不做，写盘失败留痕不吞。"""
            if timeline is None:
                return
            try:
                timeline.event(kind, **payload)
            except Exception as error:  # noqa: BLE001 - 打点失败不能阻断操作，但必须出现在 errors
                note = "时间轴写入失败：%s: %s" % (type(error).__name__, error)
                if note not in errors:
                    errors.append(note)

        if emit_intent_bounds:
            _tl("intent_start", intent_id=self.intent_id, goal=intent or "")
        try:
            if self.motor is None:
                ended = "no_motor"
                errors.append("没有 gamescaling（手缺席）：执行器不替你按键。")
            else:
                # 每个意图都从干净历史开始：policy 里残留上一个意图的 5 帧，
                # 会让手照着上一幕的画面按。reset 失败也要继续，但必须留痕。
                reset = getattr(self.motor, "reset", None)
                if callable(reset):
                    try:
                        reset()
                    except Exception as error:  # noqa: BLE001 - 清历史失败不该毁掉这一段
                        errors.append("motor.reset 失败：%s: %s" % (type(error).__name__, error))
                # 没有嘴就不要起嘴线程：起了只会在 self.narrator.ask 上每轮刷 AttributeError，
                # 而且永远等不到 DONE，把整段拖成 max_decisions（2026-11-04 实测）。
                if self.narrator is not None:
                    mind = threading.Thread(
                        target=self._narrate_loop,
                        args=(intent, done_when, stop, done, streak, errors),
                        name="fast-narrator", daemon=True,
                    )
                    mind.start()
                failures = 0
                # 手模型单次超时当作「这一步没跟上」，跳过继续；连续两次才认输。
                # 否则一次网络抖动就把整轮判死（实测端点 p50 已 900ms+，抖动很常见）。
                timeouts = 0
                while not done.is_set():
                    elapsed = self.clock() - self.started
                    if elapsed >= float(budget_s):
                        ended = "budget"
                        break
                    if len(self.actions) >= int(max_decisions):
                        ended = "max_decisions"
                        break
                    shot = self._frame_url()
                    if shot["error"]:
                        failures += 1
                        errors.append("截帧失败：" + str(shot["error"]))
                        if failures >= 3:
                            ended = "capture_error"
                            break
                        time.sleep(0.2)
                        continue
                    self.last_frame = shot.get("raw") or self.last_frame
                    if shot.get("raw"):
                        self.last_frames.append(shot["raw"])
                    if self.narrator is not None and hasattr(self.narrator, "push"):
                        try:
                            self.narrator.push(shot["raw"])
                        except Exception as error:  # noqa: BLE001 - 灌帧失败不影响手上动作
                            errors.append("灌帧失败：%s" % error)
                    t0 = self.clock()
                    # 阶段切换：主动作还欠着就用意图本身；已经达标就切到自由走位，
                    # 免得一次按对之后继续乱按把画面按坏（同类动作重复最容易出事）。
                    instruction = self.motion_instruction if reached_intent else intent
                    try:
                        decision = self._call_decide(
                            shot["url"], instruction, pass_deadline,
                            new_intent=not decide_ms_list)
                    except Exception as error:  # noqa: BLE001 - 手断了如实收手，不伪装成功
                        decide_ms = int((self.clock() - t0) * 1000)
                        decide_ms_list.append(decide_ms)
                        ended = _classify_motor_error(error)
                        failed = True
                        errors.append("%s: %s" % (type(error).__name__, error))
                        error_detail = {
                            "kind": ended,
                            "type": type(error).__name__,
                            "message": str(error),
                            # 模型输出坏的时候原文是唯一证据：落证，别只剩一句「解析失败」。
                            "raw_text": getattr(error, "raw_text", None),
                            "step": len(self.actions),
                            "decide_ms": decide_ms,
                            "deadline_ms": self.deadline_ms,
                            "intent_id": self.intent_id,
                        }
                        if ended == "motor_timeout" and timeouts < 1:
                            # 第一次超时：记一笔，跳过这一步继续（等价于重试一次），
                            # 不算失败、不结束意图——把「慢」和「坏」分开。
                            timeouts += 1
                            failed = False
                            # 真机实测：超时多半不是模型慢，而是「5 张历史帧 = 1.5MB」
                            # 在公网上传太慢（3.7Mbps 下光上传就 ~3.2s）。重试时清掉
                            # 手历史，只带当前帧（约 250KB），把重试留给真正能答上来的形态。
                            reset = getattr(self.motor, "reset", None)
                            if callable(reset):
                                try:
                                    reset()
                                except Exception as reset_error:  # noqa: BLE001
                                    errors.append(
                                        "超时后清历史失败：%s" % reset_error)
                            errors.append("手模型超时，已清空历史帧重试（只带当前帧）")
                            continue
                        if ended == "motor_parse_error" and parse_failures < PARSE_RETRY_LIMIT:
                            # 4B 手模型偶发一行坏 JSON（实测一整段 7 步后炸一次），
                            # 一次坏输出就把整段意图判死太重：重试一次，只跳过这一步。
                            parse_failures += 1
                            failed = False
                            errors.append("手模型这次输出解析不了，已跳过这一步重试：%s"
                                          % str(error)[:80])
                            parse_failures_total += 1
                            continue
                        # 解析失败但异常里带了已解析出的 steps（部分解析）：也要让嘴
                        # 看见「手这一步想按什么、为什么没按成」，否则它会以为手在发呆。
                        partial = getattr(error, "steps", None)
                        if isinstance(partial, (list, tuple)) and partial:
                            try:
                                partial_steps = [list(step) for step in partial][:6]
                            except (TypeError, ValueError):
                                partial_steps = []
                            if partial_steps:
                                self._record_action(
                                    step_index=len(self.actions) + 1,
                                    keys=[key for step in partial_steps for key in step],
                                    decide_ms=decide_ms,
                                    t_ms=int((self.clock() - self.started) * 1000),
                                    note="解析失败，动作没有执行",
                                )
                        break
                    decide_ms = int((self.clock() - t0) * 1000)
                    decide_ms_list.append(decide_ms)
                    timeouts = 0
                    parse_failures = 0
                    if decide_ms > self.deadline_ms:
                        slow_steps += 1
                    steps = [list(step) for step in (decision.get("steps") or [])][:6]
                    keys = [key for step in steps for key in step]
                    # 空动作重采样：只多问一遍，不替模型按键（用户口径 2026-11-04）。
                    resamples = 0
                    while self.retry_empty and not keys and resamples < self.retry_empty:
                        resamples += 1
                        try:
                            again = self._call_decide(shot["url"], instruction, pass_deadline)
                        except Exception:  # noqa: BLE001 - 重采样失败就按第一次的判定走
                            break
                        more = [list(step) for step in (again.get("steps") or [])][:6]
                        if any(more):
                            decision, steps = again, more
                            keys = [key for step in steps for key in step]
                            break
                    if resamples:
                        decide_ms = int((self.clock() - t0) * 1000)
                        if decide_ms_list:
                            decide_ms_list[-1] = decide_ms
                    # 用户 2026-11-06 投诉「不过脑子就狂按按键」：同一套动作连着重复
                    # REPEAT_STEPS_LIMIT 次、画面相对本次意图的起点还是没怎么变，
                    # 说明手在原地死按（实测连按 C 16 次、Z 15 次）。这里直接收手，
                    # 把「卡住了」交回慢系统重新判断；走对路时画面在变，不会误判。
                    repeat_key = tuple(tuple(step) for step in steps)
                    if repeat_key == getattr(self, "_repeat_steps_key", None):
                        self._repeat_steps = int(getattr(self, "_repeat_steps", 0)) + 1
                    else:
                        self._repeat_steps_key = repeat_key
                        self._repeat_steps = 1
                    if any(steps) and self._repeat_steps >= REPEAT_STEPS_LIMIT:
                        moved = _signature_delta(getattr(self, "_intent_sig", b""),
                                                 getattr(self, "_last_sig", b""))
                        if 0 <= moved <= REPEAT_STATIC_DELTA:
                            ended = "stuck"
                            note = ("同一套动作（%s）连着按了 %d 次，画面相对本次意图起点几乎没动（Δ%.2f）"
                                    % (" ".join("+".join(step) for step in steps if step),
                                       self._repeat_steps, moved))
                            errors.append("卡住：" + note)
                            self._emit("operator_stuck", why=note, repeat=self._repeat_steps,
                                       source="repeat_guard")
                            break
                    played: Dict[str, Any] = {}
                    play_failed = False
                    act_started = time.monotonic()
                    if any(steps):
                        try:
                            # 单一动作 + duration：按 duration 按住，别摊成碎拍
                            # （Game2Player 的 hold_ms；GameScaling 没有这个字段）。
                            hold_ms = int(decision.get("hold_ms") or 0)
                            single = len([s for s in steps if s]) == 1 and len(steps) == 1
                            play_ms = None
                            if single and hold_ms:
                                play_ms = max(int(self.step_ms), min(hold_ms, MAX_HOLD_MS))
                            # 带 duration 的动作是一次「有始有终的按住」，按完就松；
                            # 没给 duration 的才靠 latch 跨决策继续按。
                            keep = None if play_ms is None else False
                            played = self._play_steps(steps, step_ms=play_ms, keep_held=keep)
                        except Exception as error:  # noqa: BLE001 - 按键失败不该把整段丢掉
                            play_failed = True
                            errors.append("按键失败：%s: %s" % (type(error).__name__, error))
                    t_act_ms = (time.monotonic() - act_started) * 1000.0
                    # 时序账：adapter 真的按计划节拍走了吗（旧 adapter 没有 step_results
                    # -> 空 dict，绝不伪造 0 延迟）。晚了整整一拍才算 late。
                    timing = _step_timing(played.get("step_results"), self.step_ms)
                    if timing:
                        late_steps += int(timing.get("late_steps") or 0)
                    # 这一步的键到底泵没泵动：窗口抢不到 / 词表外 / 按键异常。
                    # 它同时进动作账（慢系统能看见）和手模型历史（模型下次能自纠）。
                    status = _aggregate_step_status(played.get("step_results"))
                    if play_failed:
                        status = {"pressed": False, "step": len(steps), "reason": "play_failed"}
                    if status:
                        dropped_steps += 0 if status.get("pressed") else 1
                    entry = {
                        "t_ms": int((self.clock() - self.started) * 1000),
                        "keys": keys,
                        "mouse": decision.get("mouse") or {},
                        "steps": steps,
                        "decide_ms": decide_ms,
                        "pressed": played.get("pressed") or [],
                        "skipped": played.get("skipped_keys") or [],
                        "raw": decision.get("raw") or "",
                        "timing": timing,
                        "step_status": status,
                        "resamples": resamples,
                    }
                    self.actions.append(entry)
                    # 手写账：这一步按了什么、决策花多久、什么时候发生。step_index 以 1 起，
                    # 与 actions[i] 对齐（第 1 步 = actions[0]）；失败/吞键要让嘴知道。
                    note = ""
                    if play_failed:
                        note = "按键失败"
                    elif entry["skipped"]:
                        note = "被吞按键：" + ",".join(str(key) for key in entry["skipped"])
                    self._record_action(step_index=len(self.actions), keys=keys,
                                        decide_ms=decide_ms, t_ms=entry["t_ms"], note=note)
                    # 手写自己的历史：帧 + 实际动作 + 这一步到底泵没泵动。没有这一步，
                    # gamescaling 的 history 永远是空的，5 帧上下文等于白配。
                    history_decision = decision
                    wrote = self._record_history(shot["url"], history_decision, status)
                    history_log.append(wrote)
                    # 三段计时：编帧（本地）/ 上传+推理（decide 墙钟）/ 按键。
                    # 缺一段就记 None，宁可说「没测到」也不拿别的段冒充。
                    self._trace_step(
                        shot=shot, decide_ms=decide_ms, t_act_ms=t_act_ms,
                        status=status, timing=timing, steps=steps,
                        decision=decision,
                    )
                    if not wrote.get("ok"):
                        note_hist = "历史没写进手模型：%s" % wrote.get("reason")
                        if note_hist not in errors:
                            errors.append(note_hist)
                    self._emit("operator_action", **entry)
                    # 快系统（Omni）看画面时可以直接要一次按键（tool press_key）。用户口径
                    # 2026-11-04：「能不用就不用，除非必须」——只有它自己判断非按不可才发起；
                    # 这里如实执行，来源记 omni，不进 actions（手模型没按，步数不能算到它头上）。
                    take = getattr(self.narrator, "take_press", None)
                    if callable(take):
                        wanted = list(take() or [])
                        if wanted:
                            try:
                                self.adapter.play_steps([[key] for key in wanted],
                                                        step_ms=self.step_ms)
                                self._emit("omni_press", keys=wanted)
                                _tl("game_action", steps=[[key] for key in wanted],
                                    intent_id=self.intent_id, source="omni")
                            except Exception as error:  # noqa: BLE001 - 快系统按键失败不结束意图
                                errors.append("快系统要的按键没按成：%s: %s"
                                              % (type(error).__name__, error))
                    # 用户口径（2026-11-04）：「不操作就是不操作」——模型没出键就一步都不按，
                    # 时间轴如实记 steps=[]，绝不替它补按键。
                    _tl("game_action", steps=steps, intent_id=self.intent_id,
                        decide_ms=decide_ms)
                    # 卡住检测（01 §7「卡关」的机器判据）：画面十几帧不动、或同一套
                    # 动作反复但画面没推进，就收手交棒——不能指望模型每次都自述「我卡住了」。
                    stuck_info = self._check_stuck(shot.get("raw"), steps)
                    if stuck_info.get("error"):
                        errors.append("卡住检测异常：%s" % stuck_info["error"])
                    if stuck_info.get("stuck"):
                        ended = "stuck"
                        errors.append("卡住：%s" % stuck_info.get("why"))
                        _tl("stuck", why=stuck_info.get("why"),
                            static_streak=stuck_info.get("static_streak"),
                            repeat=stuck_info.get("repeat"), intent_id=self.intent_id)
                        self._emit("operator_stuck", **stuck_info)
                        break
                    if not any(steps):
                        # 模型这一步是显式「等」：跨决策按住的键要松开，否则角色会一直往
                        # 上一个方向走（模型说停，键还按着，是最糟的组合）。
                        self._release_held()
                        streak["empty"] += 1
                        # 连续空动作才收手：4B 手模型偶发空步，3 次太容易误判「做完了」。
                        if streak["empty"] >= 6:
                            ended = "no_action"
                            break
                    else:
                        streak["empty"] = 0
                if done.is_set() and not failed:
                    ended = "goal_reached"
                    reached = True
        finally:
            # 收手/熔断/异常都必须先松手：按住的 key-down 会让角色继续走。
            self._release_held()
            # 嘴线程只有一个出口：这里置 stop 再 join；任何异常路径都不许漏线程。
            stop.set()
            if mind is not None:
                mind.join(timeout=2.0)
                if mind.is_alive():
                    errors.append("解说线程 2.0s 内没有退出（已放弃等待，按线程泄漏记）")
        total_ms = int((self.clock() - self.started) * 1000)
        if emit_intent_bounds:
            _tl("intent_end", intent_id=self.intent_id, ended=ended, reached=bool(reached),
                total_ms=total_ms)
        summary = self._summarize(intent, ended)
        if done_when and reached:
            pass
        result: Dict[str, Any] = {
            "ended": ended,
            "reached": bool(reached),
            #: 裁判收手时给的理由（收手必须有理由，见 NARRATE_INSTRUCTIONS）。
            "done_reason": str(streak.get("reason") or ""),
            "total_ms": total_ms,
            "commentary": list(self.commentary),
            "action_count": len(self.actions),
            "actions": list(self.actions),
            "summary": summary,
            "errors": errors[:3],
            #: 超 deadline 的步数（04 验收要统计 decide_ms > 300ms 的占比）。
            "slow_steps": slow_steps,
            #: 每步 motor 耗时（毫秒），与 actions[i]["decide_ms"] 对齐。
            "decide_ms": decide_ms_list,
            #: 晚了一整拍的按键步数（step_results.lag_ms > step_ms 的累计）。
            "late_steps": late_steps,
            "dropped_steps": dropped_steps,
            #: 按键时序总账（没有 step_results 时是空 dict）。
            "timing": _run_timing(self.actions, self.step_ms),
            #: 每次决策写回手历史的结果，与 actions 对齐。
            "history": history_log,
            #: 手失败时的异常诊断（None 表示手没失败）。
            "error_detail": error_detail,
        }
        # 注入 context 时才多这个键；未注入时返回结构与改造前完全一致（老调用方不受影响）。
        if self.context is not None:
            result["intent_context"] = self._context_snapshot()
        # 挂了卡住检测才多这两个键：最后那一帧的判定 + 累计统计（看命中有多频繁）。
        if self.stuck is not None:
            result["stuck"] = dict(self.stuck_last)
            result["stuck_stats"] = dict(getattr(self.stuck, "stats", {}) or {})
        # 挂了计时账本才多这个键：三段中位数（编帧 / 上传+推理 / 按键）。
        if self.trace is not None:
            try:
                result["steps_summary"] = self.trace.summary()
            except Exception as error:  # noqa: BLE001 - 汇总坏了不影响交付结果
                result["steps_summary"] = {"steps": 0, "warnings": [
                    "%s: %s" % (type(error).__name__, error)]}
        return result

    # -------------------------------------------------------------- 三段计时
    def _history_len(self) -> int:
        """手模型当前带了几张历史帧（A/B 要证明「5 张 vs 2 张」真的不同）。"""

        history = getattr(self.motor, "history", None)
        try:
            return len(history or [])
        except TypeError:
            return 0

    def _trace_step(self, *, shot: Dict[str, Any], decide_ms: int, t_act_ms: float,
                    status: Dict[str, Any], timing: Dict[str, Any],
                    steps: Any, decision: Dict[str, Any]) -> Dict[str, Any]:
        """落一行的三段计时；账本坏了只记录，绝不影响按键。"""

        trace = self.trace
        if trace is None:
            return {}
        try:
            return trace.record(
                t_encode=shot.get("t_encode_ms"),
                t_upload_infer=decide_ms,
                t_act=t_act_ms,
                intent_id=self.intent_id or "",
                step=len(self.actions),
                t_capture_ms=round(float(shot.get("t_capture_ms") or 0.0), 1),
                payload_bytes=int(shot.get("payload_bytes") or 0),
                history_len=self._history_len(),
                key_groups=len([group for group in (steps or []) if group]),
                pressed=bool((status or {}).get("pressed", True)),
                late_steps=int((timing or {}).get("late_steps") or 0),
                raw_chars=len(decision.get("raw") or ""),
            )
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "error": "%s: %s" % (type(error).__name__, error)}

    # -------------------------------------------------------------- 卡住检测
    def _check_stuck(self, jpeg: Any, steps: Any) -> Dict[str, Any]:
        """喂一帧给卡住检测器；检测器本身出问题只记错误，绝不影响手上动作。"""

        detector = self.stuck
        if detector is None:
            return {}
        # 空组不占位：动作串只写真的按了键的那几拍（与手模型历史里的写法一致）。
        action = " ; ".join(
            " ".join(str(key) for key in step) for step in (steps or []) if step
        )
        try:
            info = detector.observe(jpeg, action)
        except Exception as error:  # noqa: BLE001 - 检测坏了不能打断游玩
            self.stuck_last = {"ok": False, "stuck": False,
                               "error": "%s: %s" % (type(error).__name__, error)}
            return dict(self.stuck_last)
        self.stuck_last = dict(info or {})
        return dict(self.stuck_last)

    # ------------------------------------------------------------------ 总结
    def _summarize(self, intent: str, ended: str) -> str:
        if not self.commentary and not self.actions:
            return ""
        lines = "\n".join(
            "[%05.1fs] %s" % (item["t_ms"] / 1000.0, item["text"]) for item in self.commentary
        ) or "（解说腿没说话，只有动作记录）"
        if self.narrator is not None:
            try:
                text = str(self.narrator.ask(SUMMARY_INSTRUCTIONS.format(
                    lines=lines, intent=intent or "（未写明）", ended=ended,
                )) or "").strip()
                if text:
                    return text[:400]
            except Exception:  # noqa: BLE001 - 总结失败就退回机械拼接
                pass
        heard = [item["text"] for item in self.commentary if item.get("text")]
        return ("；".join(heard[-4:]) if heard else "")[:400]
