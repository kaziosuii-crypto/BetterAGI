"""把便利贴接进手模型的三槽位 system（02_schemas §4.2 / 03_code_change_plan C.8）。

口径（2026-11-04 用户拍板，不可再退）：
- **手模型永远只吃便利贴**：system = 模板骨架 + {GAME_NAME} + 便利贴的 game_rules + 便利贴的 key_binding。
  两块一开始就是空的，空着就发空槽位——不拿旧的全量提示词去顶，否则「学会」这件事永远不发生。
- 旧的 prompt_builder.build_system_prompt 只在显式排错时用：环境变量
  BETTERAGI_MOTOR_LEGACY_PROMPT=1 才允许当兜底（默认关）。
- 便利贴是权威来源，读失败只留 warning，不静默改口径；绝不写盘（写只有慢系统能写）。
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional, Union

#: 提示词来源：便利贴 / 旧的全量提示词（仅排错）/ 什么都没有。
SOURCE_STICKY = "sticky"
SOURCE_LEGACY = "legacy"
SOURCE_EMPTY = "empty"

ROOT_ENV = "BETTERAGI_STICKY_ROOT"
LEGACY_ENV = "BETTERAGI_MOTOR_LEGACY_PROMPT"

#: 没指定存档时用这个 id：便利贴按存档分文件，空 id 会读到 unrelated 的路径。
DEFAULT_SAVE_ID = "default"

LegacySource = Union[str, Callable[[], str], None]


def sticky_root(root: Optional[Any] = None) -> Optional[str]:
    """便利贴根目录：显式传 > 环境变量 > None（交给 sticky 自己按项目根推）。"""

    if root:
        return str(root)
    env = str(os.environ.get(ROOT_ENV) or "").strip()
    return env or None


def legacy_allowed() -> bool:
    """旧提示词只能显式打开（默认关）：它会让便利贴永远学不到东西。"""

    return str(os.environ.get(LEGACY_ENV) or "").strip() in ("1", "true", "yes", "on")


def _legacy_text(legacy: LegacySource) -> str:
    if legacy is None:
        return ""
    if callable(legacy):
        try:
            return str(legacy() or "")
        except Exception:  # noqa: BLE001 - 旧路径拼不出来也算「没有提示词」，由调用方留痕
            return ""
    return str(legacy)


def _result(text: str, source: str, info: Dict[str, Any], warnings: list,
            key_binding: str = "", game_rules: str = "") -> Dict[str, Any]:
    return {
        "text": text,
        "source": source,
        "path": str((info or {}).get("path") or ""),
        "warnings": list(warnings),
        "chars": len(text),
        "key_binding_chars": len(key_binding),
        "game_rules_chars": len(game_rules),
        "blocks_empty": not (key_binding.strip() or game_rules.strip()),
    }


#: 目标段标题：沿用官方 Doom 样例里的写法（`**What you see on screen**` 这类自由段），
#: 不新造槽位——模板只认 GAME_NAME / GAME_RULES / KEY_BINDINGS 三处（02 §4.2）。
OBJECTIVE_HEADING = "**Current Objective**"
#: 键位块硬上限（便利贴 1K + profile 键位表补进去，超了截断并留 warning）。
KEY_BLOCK_MAX_CHARS = 1600


def _compose_rules(game_rules: Any, extra_rules: Any, objective: Any) -> str:
    """把便利贴规则、本局补充规则、本意图目标拼成 Game Rules 段的正文。

    顺序固定：规则在前、目标在后——模型读到最后看到的是「现在要做什么」。
    目标为空时一个字都不加（空标题会让模型自己编目标）。
    """

    parts = [str(game_rules or "").strip(), str(extra_rules or "").strip()]
    block = "\n\n".join(part for part in parts if part)
    goal = str(objective or "").strip()
    if goal:
        block = (block + "\n\n" + OBJECTIVE_HEADING + "\n" + goal).strip()
    return block


def _key_token(line: str) -> str:
    """取一行键位说明的键名（`- Z -> confirm` → `z`），用来去重。"""

    text = str(line or "").strip()
    if text.startswith("-"):
        text = text[1:].strip()
    for sep in ("->", "→", ":"):
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    return text.strip().lower()


#: 便利贴里出现这些字样就说明这一局没有鼠标输入（键盘游戏）：不该给它鼠标行。
MOUSE_OFF_MARKERS = ("no mouse", "without mouse", "没有鼠标", "无鼠标", "不支持鼠标")


def wants_mouse_aim(game_rules: Any) -> bool:
    """README 口径：鼠标语义行（`- Mouse X Y -> aim / rotate the camera...`）只有在
    鼠标真的控制视角时才加。键盘游戏带上它，实测会被诱导输出 LB/RB/MB（Undertale
    命名界面连发 13 步 LB，游戏零输入）。判定依据就是便利贴 game_rules 里的显式声明。
    """

    text = str(game_rules or "").lower()
    if not text.strip():
        return True
    return not any(marker in text for marker in MOUSE_OFF_MARKERS)


MOUSE_KEYS = ("lb", "rb", "mb")

#: 键盘游戏的硬规则：程序注入，不依赖便利贴（慢系统重写便利贴时删不掉它）。
NO_MOUSE_RULE = (
    "- NO MOUSE INPUT: this game is controlled by the keyboard only. Never output LB, RB or MB; "
    "press Z to confirm and the arrow keys to move."
)


def _has_mouse_binding(key_lines: Any) -> bool:
    """profile 键位表里有没有鼠标绑定（没有就当键盘游戏，不给鼠标行）。

    Undertale 的 keymap.json 里全是 keyboard 绑定；Doom 这类才可能有鼠标。
    之前只看便利贴里的 "NO MOUSE" 字样，慢系统一重写便利贴就把鼠标行带回来了。
    """

    if not key_lines:
        return True  # 没有键位表就不敢断言，保守按「可能有鼠标」
    items = key_lines if isinstance(key_lines, (list, tuple)) else str(key_lines).splitlines()
    for raw in items:
        text = str(raw or "").strip()
        if not text:
            continue
        low = text.lower()
        token = _key_token(text)
        if token in MOUSE_KEYS:
            return True
        if "mouse" in low and "no mouse" not in low:
            return True
    return False



def _merge_key_lines(key_binding: Any, key_lines: Any, warnings: list,
                     *, allow_mouse: bool = True) -> str:
    """profile 键位表 + 便利贴键位合并（键位表优先，同一键不重复）。

    2026-11-06 改顺序：便利贴是慢系统自己写的自由文本，经常写成「up -> up」「z -> z」
    这种没有动作语义的空映射，而 profile/keymap.json 里是实测绑定（up -> Up）。以前
    便利贴优先，这些含糊行会把正确行顶掉，于是手拿到「up -> up」只会按字母 W 走路，
    Undertale 里 W 什么都没发生——整轮画面不动。所以现在**键位表优先**，便利贴只补
    键位表里没有的键。

    为什么必须合并：便利贴里往往只有一两行（真人也没耐心把键位抄满），而
    profile/keymap.json 里已经有 12 条实测绑定。之前只看便利贴，手模型拿到
    的 Common Keys 段只有一行 - Enter，剩下的键它全不认识——实测退化成空动作。
    """

    lines = []
    seen = set()
    for source in (key_lines, key_binding):
        if isinstance(source, (list, tuple)):
            items = [str(item) for item in source]
        else:
            items = str(source or "").splitlines()
        for raw in items:
            line = str(raw or "").strip()
            if not line:
                continue
            if not line.startswith("-"):
                line = "- " + line
            token = _key_token(line)
            if not allow_mouse and token in MOUSE_KEYS:
                # 键盘游戏：鼠标按键列在 Common Keys 里只会让模型去按它。
                continue
            if token and token in seen:
                continue
            if token:
                seen.add(token)
            lines.append(line)
    block = "\n".join(lines).strip()
    if len(block) > KEY_BLOCK_MAX_CHARS:
        warnings.append("键位块超过 %d 字，已截断" % KEY_BLOCK_MAX_CHARS)
        block = block[:KEY_BLOCK_MAX_CHARS].rstrip()
    return block



def motor_backend() -> str:
    """当前手模型后端：game2player（2026-11-05 起的默认）或 gamescaling。"""

    try:
        import g2p  # type: ignore

        return g2p.backend()
    except Exception:  # noqa: BLE001 - 模块不在就退回老后端，不挡住出提示词
        return "gamescaling"


def build_motor_system(
    *,
    game: Any = "",
    save_id: Any = "",
    root: Optional[Any] = None,
    template: Optional[str] = None,
    legacy: LegacySource = None,
    objective: Any = "",
    key_lines: Optional[Any] = None,
    extra_rules: Any = "",
    mouse_aim: Optional[bool] = None,
) -> Dict[str, Any]:
    """返回手模型要发出去的那一条 system 字符串及其来源。

    返回 {text, source, path, warnings, chars, key_binding_chars, game_rules_chars,
    blocks_empty}：source == "sticky" 是常态（哪怕两块空着）；"legacy" 只在
    BETTERAGI_MOTOR_LEGACY_PROMPT=1 或没有游戏名时出现；"empty" 表示一个字都没有。
    """

    game_id = str(game or "").strip()
    sid = str(save_id or "").strip() or DEFAULT_SAVE_ID
    warnings: list = []
    info: Dict[str, Any] = {}

    if not game_id:
        warnings.append("没有游戏名：读不到便利贴，只能用旧提示词或空提示词")
        text = _legacy_text(legacy)
        return _result(text, SOURCE_LEGACY if text.strip() else SOURCE_EMPTY, info, warnings)

    if root is None and sticky_root(None) is None:
        warnings.append(
            "便利贴根目录没指定（也没设 %s）：会按当前目录下的 run/ 找，容易读不到同一份"
            % ROOT_ENV
        )

    try:
        import sticky as sticky_mod

        info = sticky_mod.read_sticky(game_id, sid, root=sticky_root(root))
        warnings.extend(str(item) for item in (info.get("warnings") or []))
    except Exception as error:  # noqa: BLE001 - 便利贴坏了不该让手模型停摆
        warnings.append("便利贴读取失败：%s: %s" % (type(error).__name__, error))
        info = {}

    key_binding = str((info or {}).get("key_binding") or "")
    game_rules = str((info or {}).get("game_rules") or "")
    # 慢系统这一轮的 goal 必须进 Game Rules 段：官方 user 轮固定是
    # "current game screen"（motor.decide docstring），任务信息只能走 system。
    # 之前 goal 一个字都没进提示词，模型看到的是「没有目标的游戏画面」——
    # 实测就直接退化成空动作（_diag/probe_think_matrix.py）。
    rules_block = _compose_rules(game_rules, extra_rules, objective)
    # 鼠标能不能用：便利贴声明之外，还看 profile 键位表里到底有没有鼠标绑定。
    # 键盘游戏（Undertale）profile 里一条鼠标绑定都没有 —— 这样即使慢系统把
    # 便利贴整块重写成不含 "NO MOUSE" 的版本，鼠标行也不会被塞回去（LB 连发根因）。
    if mouse_aim is None:
        aim = wants_mouse_aim(rules_block) and _has_mouse_binding(key_lines)
    else:
        aim = bool(mouse_aim)
    if not aim:
        # 程序自有的硬事实行：不写在便利贴里，慢系统重写便利贴也删不掉它。
        rules_block = NO_MOUSE_RULE + (chr(10) + rules_block if rules_block.strip() else "")
        warnings.append("这一局按键盘游戏处理：已注入 NO MOUSE 硬规则、键位表剔掉 LB/RB/MB")
    bindings_block = _merge_key_lines(key_binding, key_lines, warnings, allow_mouse=aim)
    # Game2Player 的 system 骨架与 GameScaling 不同（模型卡要求 Game Rules /
    # Role and Controls / Task Instruction / Output Format 四段），所以按后端分开拼。
    if motor_backend() == "game2player":
        try:
            import g2p  # type: ignore

            rules_only = _compose_rules(game_rules, extra_rules, "")
            text = g2p.build_system_prompt(game_id, rules_only, bindings_block,
                                           objective)
        except Exception as error:  # noqa: BLE001
            warnings.append("Game2Player 提示词拼装失败：%s: %s" % (type(error).__name__, error))
            text = _legacy_text(legacy) if legacy_allowed() else ""
            return _result(text, SOURCE_LEGACY if text.strip() else SOURCE_EMPTY,
                           info, warnings, bindings_block, rules_block)
        out = _result(text, SOURCE_STICKY, info, warnings, bindings_block, rules_block)
        out["mouse_aim"] = aim
        out["backend"] = "game2player"
        out["objective"] = str(objective or "").strip()
        out["objective_chars"] = len(out["objective"])
        return out
    try:
        import prompt_builder

        text = prompt_builder.assemble_motor_system(
            game_id, rules_block, bindings_block, template=template, mouse_aim=aim
        )
    except Exception as error:  # noqa: BLE001 - 拼装失败才允许退回旧路径
        warnings.append("三槽位拼装失败：%s: %s" % (type(error).__name__, error))
        text = _legacy_text(legacy) if legacy_allowed() else ""
        return _result(text, SOURCE_LEGACY if text.strip() else SOURCE_EMPTY,
                       info, warnings, key_binding, game_rules)

    if not (key_binding.strip() or game_rules.strip()):
        warnings.append(
            "便利贴两块都还空着：这一轮手模型只拿到模板骨架（没有规则、没有键位）"
        )
    if legacy_allowed():
        warnings.append("BETTERAGI_MOTOR_LEGACY_PROMPT 已开：本次是排错用的旧路径")
    out = _result(text, SOURCE_STICKY, info, warnings, bindings_block, rules_block)
    out["mouse_aim"] = aim
    out["objective"] = str(objective or "").strip()
    out["objective_chars"] = len(out["objective"])
    return out
