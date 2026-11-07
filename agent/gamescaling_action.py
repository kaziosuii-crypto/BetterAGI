# -*- coding: utf-8 -*-
'''GameScaling 动作格式：模型输出 -> 一次 200ms 的键鼠动作块。

逐字对齐官方 gamescaling/action.py（THUSI-Lab/GameScaling，2026-11-04 经 ghproxy 取回，
落盘 _diag/official_action.py）。凡是和官方不一致的地方都在注释里写清原因，方便后来人
判断是「我们故意的」还是「抄错了」。

一段动作长这样：

    <|action_start|>X Y Z ; g1 ; g2 ; g3 ; g4 ; g5 ; g6<|action_end|>

* X Y 是整段 200ms 的鼠标位移，单位是屏幕宽/高的千分比（X>0 右，Y>0 下）；
* Z 是滚轮格数（Z>0 上）；
* g1..g6 是 6 个 33ms 子步，每组列出「这一拍按住的键」，最多 4 个。
  **同一个键出现在相邻两组里表示一直按住**，不是重复按两下；空组 = 这一拍松开所有键。

推理模型会在动作前加一段思考，动作永远只读**最后一个思考闭合标签之后**的内容：
模型会把格式原样抄在思考里（「格式是 <|action_start|>X Y Z ; ...」），整段读取就会把
抄书当决策执行。
'''

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

ACTION_START = "<|action_start|>"
ACTION_END = "<|action_end|>"
#: 思考标签。拆成三段拼，是为了让源码里不出现连续的尖括号词——那种词在日志/终端里
#: 容易被其它链路当成协议标签处理，搜索和排错都会莫名失效。
_THINK_WORD = "think"
THINK_OPEN = "<" + _THINK_WORD + ">"
THINK_CLOSE = "</" + _THINK_WORD + ">"

CHUNK_MS = 200.0
CHUNK_STEPS = 6
STEP_MS = CHUNK_MS / CHUNK_STEPS
MAX_KEYS_PER_STEP = 4
MOUSE_LIMIT = 1000.0
SCROLL_LIMIT = 5.0

#: 合法空动作（6 组全空 + 鼠标不动）的历史串：模型"决定这一拍什么都不按"时用。
#: 解析失败**不**写它（见 PARSE_FAILED_TEXT）——旧 TUI 兼容口才把坏输出回落成 no-op。
NOOP_ACTION_TEXT = ACTION_START + "0 0 0 ; ; ; ; ; ; " + ACTION_END
#: 解析失败这一轮在历史里的占位串（设计 03 C.5 P0 / 04 S1-6）。
#: 不再写 NOOP_ACTION_TEXT：no-op 在训练分布里表示"这一步就不该按键"，
#: 拿它冒充解析失败会让模型把"输出坏掉"当成"正确做法"照着学。
PARSE_FAILED_TEXT = "<parse_failed>"

#: 模型偶尔把收尾的 > 写成 }，官方正则容忍这种手滑。
_TAG_RE = re.compile(r"<\|action_start\|[>}](.*?)<\|action_end\|[>}]", re.DOTALL)
#: 有的服务端把特殊 token 剥掉了，只剩裸的 "X Y Z ; ..." 动作体。
_BARE_RE = re.compile(r"^\s*-?\d")
_SKELETON_RE = re.compile(r"[^\n;<]*(?:;[^\n;<]*){5,}")
_BARE_INT_RE = re.compile(r"(-?\d+)\s+(-?\d+)\s+(-?\d+)\s*(;[^\n<]*)", re.DOTALL)


def split_reasoning(text: str) -> Tuple[str, str, bool]:
    '''``text`` -> (思考内容, 思考之后的部分, 思考是否闭合)。

    思考没闭合 = 生成在动作出现前就结束了（token 预算烧完，或 stop 命中了思考里的文本），
    所以尾部视为空。
    '''
    text = text or ""
    if THINK_CLOSE in text:
        head, _, tail = text.rpartition(THINK_CLOSE)
        return head.split(THINK_OPEN)[-1].strip(), tail, True
    if THINK_OPEN in text:
        return text.split(THINK_OPEN)[-1].strip(), "", False
    return "", text, True


@dataclass
class Action:
    '''一次解析出来的 200ms 动作块；解析不出来的就是 parsed=False 的 no-op。'''

    mouse: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    steps: List[List[str]] = field(default_factory=lambda: [[] for _ in range(CHUNK_STEPS)])
    parsed: bool = False
    truncated: bool = False
    body: str = ""
    dropped: List[str] = field(default_factory=list)

    @property
    def history_text(self) -> str:
        '''这一轮在下一份提示词里长什么样。

        解析成功的轮次永远是**格式良好**的动作块；解析失败写成
        :data:`PARSE_FAILED_TEXT` 占位，而不是 no-op——no-op 表示"这一拍合法地
        什么都不按"，写它等于告诉模型"输出坏掉也没关系"，下一轮会照着摆烂
        （设计 03 C.5 / 04 S1-6；这是与官方 action.py 的有意差异）。
        '''
        if not self.parsed:
            return PARSE_FAILED_TEXT
        return ACTION_START + self.body + ACTION_END

    @property
    def is_noop(self) -> bool:
        return not any(self.steps) and tuple(self.mouse) == (0.0, 0.0, 0.0)

    @property
    def pressed(self) -> List[str]:
        out: List[str] = []
        for group in self.steps:
            for key in group:
                if key not in out:
                    out.append(key)
        return out

    def to_text(self) -> str:
        x, y, z = (int(round(v)) for v in self.mouse)
        groups = " ; ".join(" ".join(g) for g in self.steps)
        return ACTION_START + "%d %d %d ; %s" % (x, y, z, groups) + ACTION_END

    def as_dict(self) -> Dict[str, Any]:
        '''给 agent 工具层用的扁平结构（沿用 motor._parse_action 的历史字段名）。'''
        mouse = {"x": int(round(self.mouse[0])), "y": int(round(self.mouse[1])),
                 "z": int(round(self.mouse[2]))}
        steps = [list(step) for step in self.steps]
        return {
            "raw": self.body,
            "mouse": mouse,
            "steps": steps,
            "keys": self.pressed,
            "parsed": bool(self.parsed),
            "truncated": bool(self.truncated),
            "dropped": list(self.dropped),
            "duration_ms": int(CHUNK_MS),
            "step_ms": int(STEP_MS),
        }


def _build(nums: List[float], groups: List[str], body: str,
           *, keep_unknown: bool = False) -> Action:
    '''把数字前缀 + 6 组键名装配成 Action，顺手做官方那三条钳制。'''
    import gamescaling_vocab as vocab

    x = max(-MOUSE_LIMIT, min(MOUSE_LIMIT, nums[0]))
    y = max(-MOUSE_LIMIT, min(MOUSE_LIMIT, nums[1]))
    z = max(-SCROLL_LIMIT, min(SCROLL_LIMIT, nums[2]))
    steps: List[List[str]] = []
    dropped: List[str] = []
    for group in groups[:CHUNK_STEPS]:
        keys: List[str] = []
        for tok in group.split():
            if tok in vocab.KEY_VOCAB:
                keys.append(tok)
            elif keep_unknown:
                keys.append(tok)
            elif tok not in dropped:
                dropped.append(tok)
        steps.append(keys[:MAX_KEYS_PER_STEP])
    while len(steps) < CHUNK_STEPS:
        steps.append([])
    return Action(mouse=(x, y, z), steps=steps, parsed=True, body=body, dropped=dropped)


def _parse_body(body: str, *, keep_unknown: bool = False) -> Optional[Action]:
    body = (body or "").strip()
    if not body:
        return None
    parts = [seg.strip() for seg in body.split(";")]
    if not parts or not parts[0]:
        return None
    nums: List[float] = []
    for tok in parts[0].split()[:3]:
        try:
            nums.append(float(tok))
        except ValueError:
            return None
    if not nums:
        return None
    #: 官方允许只写两个数（"X Y" 或干脆只写 X），缺的位按 0 补——lenient 救回来的
    #: 骨架常常只有 "X Y ; k ; ..."。少这个补齐会让整轮退回 no-op（实测官方
    #: test_lenient_recovers_skeleton 的 "-5 4 ; D ; ..." 就是这样）。
    while len(nums) < 3:
        nums.append(0.0)
    return _build(nums, parts[1:], body, keep_unknown=keep_unknown)


def _parse_strict(tail: str, *, keep_unknown: bool = False) -> Action:
    match = _TAG_RE.search(tail)
    if match:
        action = _parse_body(match.group(1), keep_unknown=keep_unknown)
        return action if action is not None else Action()
    if _BARE_RE.match(tail) and ";" in tail:
        action = _parse_body(tail, keep_unknown=keep_unknown)
        return action if action is not None else Action()
    # 我们这条链路的**常态**：服务端把 "<|action_end|>" 当 stop 字符串，返回时连它一起
    # 切掉了（实测 server_gamescaling.py 的 _trim）。于是最后一个动作块永远没有收尾标签。
    # 官方客户端靠 finalize() 在 finish_reason=='stop' 时补回来；这里再兜一层，免得
    # 任何调用方忘了先 finalize 就直接落成空动作（历史 bug：整轮静默全零）。
    start = tail.rfind(ACTION_START)
    if start >= 0:
        body = tail[start + len(ACTION_START):].strip()
        if "<|" in body:
            body = body.split("<|")[0].strip()
        action = _parse_body(body, keep_unknown=keep_unknown)
        if action is not None:
            action.truncated = True
            return action
    return Action()


def _parse_lenient(text: str, *, keep_unknown: bool = True) -> Action:
    '''从格式跑偏的回复里捞动作：六组分号骨架优先，退而求其次找裸的 "X Y Z ;" 前缀。'''
    text = text.replace(THINK_OPEN, " ").replace(THINK_CLOSE, " ")
    skeleton = None
    for match in _SKELETON_RE.finditer(text):
        skeleton = match.group(0)
    if skeleton is not None:
        action = _parse_body(skeleton.strip(), keep_unknown=keep_unknown)
        if action is not None:
            return action
    match = _BARE_INT_RE.search(text)
    if match:
        body = (match.group(1) + " " + match.group(2) + " " + match.group(3)
                + match.group(4)).strip()
        action = _parse_body(body, keep_unknown=keep_unknown)
        if action is not None:
            return action
    return Action()


def parse_action(text: str, *, lenient: bool = False,
                 keep_unknown: bool = True) -> Action:
    '''模型原始回复 -> Action；永不抛异常，失败就是 no-op。

    ``lenient=False``（默认）：只认最后一个思考闭合标签之后的动作块，可以是带标签的，
    也可以是服务端剥掉标签后的裸 "X Y Z ; ..."。

    ``lenient=True``：额外从整段回复里捞「六个分号组」的骨架——对齐得好的 checkpoint
    很少需要这个，但服务端换模板/token 配置跑偏时会救回一轮。

    ``keep_unknown=True``（我们默认开）：词表外的 token 一律保留，交给
    ``gamescaling_vocab.sanitize_steps_report`` 统一清洗并记 dropped。这样日志里能看清
    「模型出了什么」（比如把小写 z 出了），而不是解析层先吃掉、下游只看到空动作。
    '''
    _, tail, closed = split_reasoning(text)
    if closed:
        action = _parse_strict(tail, keep_unknown=keep_unknown)
        if action.parsed or not lenient:
            return action
    elif not lenient:
        return Action(truncated=True)
    action = _parse_lenient(text or "", keep_unknown=keep_unknown)
    if not closed and not action.parsed:
        action.truncated = True
    return action


def finalize(text: str, finish_reason: Optional[str], prefill: str) -> str:
    '''把请求本身对回复做的两件事补回去：吃掉的前置填充、被 stop 切掉的收尾标签。

    三步（官方 policy.py:147 逐条对齐）：
    1. 回复不以思考开头标签开头 -> 补回 prefill（prefill 不会出现在返回文本里）；
    2. 思考已闭合、尾部没有动作标签，但尾巴形如 "X Y Z ; ..." -> 补 action_start；
    3. finish_reason == "stop" 且最后一个 action_start 之后没有 action_end -> 补 action_end。
    '''
    text = text or ""
    if not text.lstrip().startswith(THINK_OPEN):
        text = prefill + text
    _, tail, closed = split_reasoning(text)
    if closed and ACTION_START not in tail:
        bare = tail.strip()
        if _BARE_RE.match(bare) and ";" in bare:
            text = text[: len(text) - len(tail)] + ACTION_START + bare
    if (finish_reason == "stop" and ACTION_START in text
            and ACTION_END not in text[text.rfind(ACTION_START):]):
        text += ACTION_END
    return text


def noop_action() -> Action:
    '''显式 no-op：6 组全空、鼠标为零，但 ``parsed=True``——代表"合法地什么都不按"。

    解析失败的轮次**不要**拿它冒充：那要写 :data:`PARSE_FAILED_TEXT`，
    交给上层熔断（设计 03 C.5 / 04 S1）。
    '''
    return Action(mouse=(0.0, 0.0, 0.0),
                  steps=[[] for _ in range(CHUNK_STEPS)],
                  parsed=True, body="0 0 0 ; ; ; ; ; ; ")
