"""脚本批处理（key_scripts）：不需要看画面就能做完的固定操作，别动用快系统。

口径（2026-11-04 用户拍板）：
- 慢系统只在「需要快速连续复杂操作」时才调 lumine（持续按键、弹幕躲避、战斗、3D 移动、
  必须看画面反馈的走位）。
- 其余一切（推进对话、翻菜单、输入名字、存档、等待过场、走直线）都用本模块的脚本批处理：
  **不起 VLM、不占 200ms 决策环**，一次调用就是几百毫秒的键盘流。
- 模型不再直接写 DSL：它对工具说一句自然语言要求，程序另开一次思考请求把要求写成
  精确脚本（DSL），再交给本模块执行。

DSL（一行写完，逗号分隔，从头执行到尾）：
  Z*8               按 Z 八次（默认每次间隔 120ms）
  Down*3,Z          先下三次再按 Z
  Right+Z           同时按 Right 和 Z（组合键）
  hold:Right:1500   按住 Right 1.5 秒（走路/蓄力）
  wait:2500         什么都不按，等 2.5 秒（过场动画）
  gap:250           把后续按键间隔改成 250ms
上限：40 步、单次 60 秒；键名必须是 KEY_VOCAB 里的 token（大小写敏感）。

预设（dialogue/confirm/back/menu/grid/walk/wait）已于 2026-11-05 全部删除：
用户实测它们「效果非常不稳定，不允许用」。脚本只能由 DSL 显式写出来。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple


class ScriptError(ValueError):
    """脚本不合法：语法错、键名不在词表、超步数/超时长。绝不静默改成别的动作。"""


#: 单次脚本的硬上限（防止模型写个死循环把直播卡住）。
MAX_STEPS = 40
MAX_TOTAL_MS = 60_000
#: 默认按键间隔：真人连点约 4~8 次/秒，250ms 是「读一句按一下」的节奏。
DEFAULT_GAP_MS = 120
#: 每一次按键**按住**多久（毫秒）。低于一帧（33ms）会被游戏漏读。
HOLD_MS = 60



#: 允许的单词键名（与 GameScaling KEY_VOCAB 同名；数字/F 键按官方写法）。
_WORDS = (
    "LB", "RB", "MB", "Up", "Down", "Left", "Right", "Shift", "Ctrl", "Alt", "Tab",
    "Caps", "Esc", "Space", "Enter", "Back", "Delete", "Insert", "Home", "End",
    "Pause", "Equal", "Minus", "Period", "Slash", "Quote",
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
    "Eleven", "Twelve",
)
ALLOWED_KEYS = set(_WORDS) | {chr(code) for code in range(ord("A"), ord("Z") + 1)}


def _key(token: str) -> str:
    """校验并规范化一个键名；词表外直接报错（错键按下去只会污染游戏状态）。"""

    name = str(token or "").strip()
    if not name:
        raise ScriptError("空键名")
    if name not in ALLOWED_KEYS:
        raise ScriptError("键名 %r 不在词表里（大小写敏感，例如 Z / Up / Space）" % name)
    return name


def _int(raw: str, what: str, *, low: int = 0, high: int = 60000) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ScriptError("%s 必须是整数，实际是 %r" % (what, raw))
    if value < low or value > high:
        raise ScriptError("%s 超出范围 %d~%d：%d" % (what, low, high, value))
    return value


Op = Tuple[str, Any]


def parse_cmd(text: str, *, gap_ms: int = DEFAULT_GAP_MS) -> List[Op]:
    """把 DSL 字符串解析成 op 列表。

    op 只有四种：("press", [键...], gap_ms) / ("hold", 键, ms) / ("wait", ms)。
    gap:250 是「改后续间隔」的伪指令，解析期就吃掉，不进 op 列表。
    """

    body = str(text or "").strip()
    if not body:
        raise ScriptError("cmd 是空的：脚本必须显式写出来（预设已删除）")
    ops: List[Op] = []
    gap = int(gap_ms)
    for chunk in [part.strip() for part in body.replace("，", ",").split(",") if part.strip()]:
        lowered = chunk.lower()
        if lowered.startswith("gap:"):
            gap = _int(chunk.split(":", 1)[1], "gap", low=1, high=2000)
            continue
        if lowered.startswith("wait:") or lowered.startswith("wait*"):
            raw = chunk.split(":", 1)[1] if ":" in chunk else chunk.split("*", 1)[1]
            ops.append(("wait", _int(raw, "wait 毫秒")))
            continue
        if lowered.startswith("hold:"):
            parts = chunk.split(":")
            if len(parts) != 3:
                raise ScriptError("hold 写法是 hold:键:毫秒，实际是 %r" % chunk)
            ops.append(("hold", _key(parts[1]), _int(parts[2], "hold 毫秒")))
            continue
        if "*" in chunk:
            name, times = chunk.split("*", 1)
            key = _key(name)
            count = _int(times, "重复次数", low=1, high=MAX_STEPS)
            ops.append(("press", [key], gap))
            for _ in range(count - 1):
                ops.append(("press", [key], gap))
            continue
        if "+" in chunk:
            keys = [_key(part) for part in chunk.split("+") if part.strip()]
            if len(keys) < 2:
                raise ScriptError("组合键至少要两个键：%r" % chunk)
            ops.append(("press", keys, gap))
            continue
        ops.append(("press", [_key(chunk)], gap))
    if len(ops) > MAX_STEPS:
        raise ScriptError("脚本 %d 步，超过上限 %d 步" % (len(ops), MAX_STEPS))
    return ops


def build_ops(cmd: str = "", *, gap_ms: int = DEFAULT_GAP_MS) -> List[Op]:
    """只接受显式 DSL。预设已删除（2026-11-05 用户要求：preset 效果不稳定，不允许用）。"""

    text = str(cmd or "").strip()
    if not text:
        raise ScriptError("cmd 是空的：脚本必须显式写出来（预设已删除）")
    return parse_cmd(text, gap_ms=gap_ms)


def plan_ms(ops: Iterable[Op]) -> int:
    """估算这串脚本要占多久（给上层做超时/预算用，不参与执行）。"""

    total = 0
    for op in ops:
        if op[0] == "press":
            total += len(op[1]) * 40 + int(op[2])
        elif op[0] == "hold":
            total += int(op[2])
        elif op[0] == "wait":
            total += int(op[1])
    return total


def describe(ops: Iterable[Op]) -> str:
    """回执里给人看的一句话（省 token：只写「按了什么、按了几次、等了多久」）。"""

    pieces = []
    for op in ops:
        kind = op[0]
        if kind == "press":
            keys = "+".join(op[1])
            pieces.append(keys)
        elif kind == "hold":
            pieces.append("hold %s %dms" % (op[1], int(op[2])))
        elif kind == "wait":
            pieces.append("wait %dms" % int(op[1]))
    squeezed: List[str] = []
    for piece in pieces:
        if squeezed and squeezed[-1] == piece:
            continue
        squeezed.append(piece)
    merged: List[str] = []
    index = 0
    while index < len(pieces):
        run = 1
        while index + run < len(pieces) and pieces[index + run] == pieces[index]:
            run += 1
        merged.append(pieces[index] if run == 1 else "%s*%d" % (pieces[index], run))
        index += run
    return " ; ".join(merged)


def actions_to_script(actions: Iterable[Dict[str, Any]], *, limit: int = 60) -> Dict[str, Any]:
    """把操作器每步的 keys 压成 DSL 风格的脚本，回给慢系统看它到底按了什么。

    用户 2026-11-05 要求：lumine 结束后慢系统要能看到操作器实际按下的键，并自动统计成
    script 形式。只统计真按下去了的步；被窗口拦掉/跳过的步单独计数，不混进脚本。
    没按键的一步记成 wait（它确实什么都没按）。返回 {script, steps, keys, skipped, truncated}。
    """

    tokens: List[str] = []
    steps = 0
    keys = 0
    skipped = 0
    for item in actions or []:
        if not isinstance(item, dict):
            continue
        wanted = [str(k).strip() for k in (item.get("keys") or []) if str(k).strip()]
        # pressed 是适配器回报的「真的按下去了的键」；拿不到就退回计划键位。
        delivered = item.get("pressed")
        if isinstance(delivered, (list, tuple)):
            group = [str(k).strip() for k in delivered if str(k).strip()]
        elif isinstance(delivered, bool):
            group = wanted if delivered else []
        else:
            group = wanted
        steps += 1
        if not group:
            # 计划了键位却一个都没按下去（窗口没焦点/键被吞）：算跳过，不算 wait。
            if wanted:
                skipped += 1
                steps -= 1
            else:
                tokens.append("wait")
            continue
        keys += len(group)
        tokens.append("+".join(group))
    truncated = len(tokens) > limit
    tokens = tokens[:limit]
    merged: List[str] = []
    index = 0
    while index < len(tokens):
        run = 1
        while index + run < len(tokens) and tokens[index + run] == tokens[index]:
            run += 1
        merged.append(tokens[index] if run == 1 else "%s*%d" % (tokens[index], run))
        index += run
    script = ",".join(merged)
    if truncated:
        script += ",..."
    return {"script": script, "steps": steps, "keys": keys, "skipped": skipped,
            "truncated": truncated}


def run(adapter: Any, ops: List[Op], *, settle_ms: int = 0) -> Dict[str, Any]:
    """真按键。只依赖 adapter.play_steps / adapter.act，不认识具体是哪个适配器。

    失败一律如实写进回执（errors），绝不吞掉假装按过了。
    """

    import time

    started = time.monotonic()
    pressed: List[str] = []
    errors: List[str] = []
    steps = 0
    for op in ops:
        kind = op[0]
        try:
            if kind == "press":
                keys, gap = list(op[1]), int(op[2])
                # 2026-11-04 修「按键不按给的走」：原来一次 press 用 play_steps 打，松手与
                # 下一次按下之间没有间隔，两次按键会落进游戏的同一帧（30fps = 33ms），
                # 游戏只认一次——看起来就像少按了。
                # 现在按人的节奏：按住 HOLD_MS（保证游戏读到一次 keydown），松手后再等 gap。
                result = adapter.play_steps([keys], step_ms=HOLD_MS)
                steps += 1
                pressed.append("+".join(keys))
                time.sleep(max(0, gap) / 1000.0)
            elif kind == "hold":
                result = adapter.act([op[1]], wait=max(0.0, int(op[2]) / 1000.0))
                steps += 1
                pressed.append("hold:%s:%d" % (op[1], int(op[2])))
            else:
                time.sleep(max(0.0, int(op[1]) / 1000.0))
                result = {"waited_ms": int(op[1])}
                steps += 1
            if isinstance(result, dict) and result.get("error"):
                errors.append("%s: %s" % (kind, result.get("error")))
        except Exception as error:  # noqa: BLE001 - 单步失败要留痕，不能中断整串
            errors.append("%s: %s: %s" % (kind, type(error).__name__, error))
    if settle_ms:
        time.sleep(max(0.0, settle_ms) / 1000.0)
    elapsed = int((time.monotonic() - started) * 1000)
    return {
        "ok": not errors,
        "did": describe(ops),
        "steps": steps,
        "pressed": pressed,
        "errors": errors,
        "elapsed_ms": elapsed,
    }

