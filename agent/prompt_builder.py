"""构造 GameScaling 系统提示词（FR-6）。

提示词必须包含三段：``{GAME_NAME}``（规范名，与 group_id 严格对齐）、
``{GAME_RULES}``（游戏规则）、``{KEY_BINDINGS}``（按键 → 效果）。

两条硬约束：

1. **没学过的东西不许编**：低于置信度阈值的条目一律不进提示词；需要的键没学到时，
   写进"未知（运行中学习）"清单，并放进 ``learning_targets`` 交给慢系统去学。
2. **可回溯**：每次构造都带 ``version``（自增）与 ``fingerprint``（内容哈希），
   FR-7 的「学习回传 → 更新记忆 → 重启快系统」要用它们证明新提示词真的生效了。

契约（GameScaling README §2/§4/§5，2026-11-04 按官方文档重写）
----------------------------------------------------------------
* 模板骨架**逐字照抄**官方 ``prompts/system_prompt_template.txt``：``**Game Rules**``、
  ``**Output Format**``、``**Explanation**`` 这些带星号的标题是模型训练时见过的纯文本段头，
  不是 Markdown 装饰——改动它们等于换了一套格式说明，模型会认不出该输出什么标签。
* ``{KEY_BINDINGS}`` 每行必须是 `- <训练键名> -> <效果>`，键名取 §5 的 74 个训练词表
  （`A`..`Z`、`Up/Down/Left/Right`、`Enter`、`Space`、`Esc`、`Back`、`LB/RB/MB`…），
  **大小写敏感**：写成小写 `up`/`confirm` 就是词表外的 token，模型侧直接丢弃。
* **没有 instruction slot**。模型只吃「系统提示词 + 画面 + 历史动作」，不吃自由文本指令；
  意图/目标只能写进 ``{GAME_RULES}``，否则它当作噪声忽略。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import gamescaling_vocab as vocab
import rule_templates as rt
from profiles import KeyBinding, Profile, Rule


TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "gamescaling_system_prompt.txt"

#: 手模型三槽位模板（便利贴 → system 拼装，02_schemas.md §4.2）。
#: 与快系统模板分开：快系统还要 {LEARNING}、键位来自 keymap，手模型只吃三槽位。
MOTOR_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "motor_system_prompt.txt"

#: 默认必须有的键位：游戏能不能动起来就靠这些。没学到就要进待学清单。
DEFAULT_REQUIRED_KEYS = (
    "W", "A", "S", "D", "Space", "Shift", "F", "E", "Q",
    "LeftClick", "RightClick", "one", "two", "three", "four", "Esc", "M",
)

#: 低于这个置信度的条目不进提示词（学习来的东西要先被慢系统复核过）。
DEFAULT_MIN_CONFIDENCE = 0.5

#: 未知条目的占位符。两条实测约束（2026-11-04）：
#: 1) 必须英文——出现中文会让整份系统提示词变成中英混排，模型对语言切换极其敏感，
#:    同一帧纯英文会出手、混排稳定全零；
#: 2) 不能带字面键名——写 `Z`/`Enter` 会被模型当成键位表的一部分，直接塞进动作里。
UNKNOWN_MARK = "unknown (learn during play)"

#: 训练词表（README §5，74 个 token，大小写敏感）。这里只列这次要用的子集，
#: 未列出的 token 会在 min_confidence 检查后被标记为"词表外"，避免把 `confirm`/`up`
#: 这类小写别名送进提示词——模型侧会当作不存在的键直接丢弃。
TRAINING_KEY_TOKENS = (
    tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    + ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
    + ("One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve")
    + ("LB", "RB", "MB")
    + ("Up", "Down", "Left", "Right")
    + ("Shift", "Ctrl", "Alt", "Tab", "Caps", "Esc", "Space", "Enter", "Back", "Delete",
       "Insert", "Home", "End", "Pause", "Equal", "Minus", "Period", "Slash", "Quote")
)

TRAINING_KEY_SET = frozenset(TRAINING_KEY_TOKENS)


def is_training_key(key: str) -> bool:
    """这个键名是不是 GameScaling 训练词表里的（大小写敏感）。"""
    return str(key or "").strip() in TRAINING_KEY_SET


@dataclass
class PromptBundle:
    """一次构造的结果，够重启快系统时做 diff 与版本回溯。"""

    text: str
    game_name: str
    fingerprint: str
    version: int
    learning_targets: List[str] = field(default_factory=list)
    used_keys: List[str] = field(default_factory=list)
    dropped_low_confidence: List[str] = field(default_factory=list)
    #: 本次拼进 {GAME_RULES} 的模板 id（规则模板库口径，可回溯是哪些模板拼出来的）。
    templates_used: List[str] = field(default_factory=list)
    #: 本次是否把 instruction 拼成了 ## Current objective。
    objective_included: bool = False
    #: instruction 原文（只用于回溯，不进指纹以外的判断）。
    instruction: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "game_name": self.game_name,
            "fingerprint": self.fingerprint,
            "version": self.version,
            "learning_targets": list(self.learning_targets),
            "used_keys": list(self.used_keys),
            "dropped_low_confidence": list(self.dropped_low_confidence),
            "templates_used": list(self.templates_used),
            "objective_included": bool(self.objective_included),
            "instruction": self.instruction,
            "chars": len(self.text),
        }


def load_template(path: Optional[Path] = None) -> str:
    target = Path(path) if path else TEMPLATE_PATH
    if not target.exists():
        raise FileNotFoundError(f"提示词模板不存在：{target}")
    return target.read_text(encoding="utf-8")


def _key_sort_key(binding: KeyBinding, prefer: Sequence[str] = ()) -> tuple:
    """按键按「本次用得上 → 移动 → 动作 → 数字 → 其他」排。

    ``prefer`` 来自规则模板声明的物理键：这次 intent 真正要用到的键排最前，
    模型读键位表时第一眼看到的就是它（列表位置本身就是注意力权重）。
    排序用的全是训练词表里的大小写原生形式；小写别名已经在 build_key_bindings 里
    被过滤掉了，不会走到这里。
    """
    order = ["W", "A", "S", "D", "Space", "Shift", "F", "E", "Q", "LB", "RB", "MB",
             "one", "two", "three", "four", "five", "M", "Esc", "Alt"]
    key = binding.key.strip()
    preferred = 0 if key in set(prefer or ()) else 1
    try:
        return (preferred, order.index(key), "")
    except ValueError:
        return (preferred, len(order), key)


def _semantic_to_physical(profile: Profile) -> Dict[str, List[str]]:
    """语义键名 → 物理键（来自 profile 的 adapter.keys）。

    required_keys 里写的是 `confirm`/`erase` 这类语义名，它们在训练词表外面。
    直接渲染成 "- confirm" 会被模型当键位抄进动作，然后被清洗丢掉，表现为"空动作"。
    所以这里先把语义名翻成物理键，再交给词表过滤。
    """
    out: Dict[str, List[str]] = {}
    try:
        config = profile.ensure_config()
    except Exception:  # noqa: BLE001 - 没有配置就没有映射
        return out
    keys = (config.get("adapter") or {}).get("keys") or {}
    for name, value in keys.items():
        physical = [value] if isinstance(value, str) else [str(v) for v in (value or [])]
        out[str(name).strip()] = [p for p in physical if p]
    return out


def build_key_bindings(
    bindings: Sequence[KeyBinding],
    *,
    required_keys: Iterable[str] = DEFAULT_REQUIRED_KEYS,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    prefer: Sequence[str] = (),
) -> tuple[str, List[str], List[str], List[str]]:
    """返回 (键位段文本, 已用键, 待学键, 被挡下的键)。

    "被挡下"有两种原因，都会进第四个返回值：置信度不够、或者键名不在训练词表里。
    ``prefer`` 里是本次规则模板声明要用到的物理键，排在最前面。
    """
    kept: List[KeyBinding] = []
    dropped: List[str] = []
    seen: Dict[str, KeyBinding] = {}
    prefer_set = set(prefer or ())
    for binding in sorted(bindings, key=lambda b: _key_sort_key(b, prefer_set)):
        if binding.confidence < min_confidence:
            dropped.append(binding.key.strip())
            continue
        # 语义键名（confirm/up）不是训练 token，但它背后绑的物理键是：
        # 提示词里要出现的是训练 token 本身（Z / Up / Enter），效果写清它干什么。
        tokens = vocab.tokens_for_profile(binding.physical_keys())
        if not tokens:
            # 物理键也不在词表里：送进去就是被服务端丢弃的噪声，宁可不写。
            dropped.append(binding.key.strip())
            continue
        effect = binding.effect_text()
        for token in tokens:
            if token in seen:
                continue
            seen[token] = binding
            kept.append(KeyBinding(token, effect, binding.confidence, binding.source))

    lines = [binding.line() for binding in kept]
    required = [str(k) for k in required_keys]
    missing = [k for k in required if k not in seen]
    if missing:
        lines.append("")
        lines.append(f"These actions are {UNKNOWN_MARK}: discover them from the screen and try them only when nothing else works.")
        lines.extend(f"- {key}" for key in missing)
    return "\n".join(lines), [b.key.strip() for b in kept], missing, dropped


def build_game_rules(
    rules: Sequence[Rule],
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    extra_sections: Optional[Dict[str, Sequence[str]]] = None,
) -> tuple[str, List[str]]:
    """返回 (规则段文本, 被置信度挡下的规则文本片段)。"""
    # 段标题必须英文：这份文本会整体喂给策略模型，任何中文都会让它退回全零动作。
    section_titles = {
        "view": "Screen",
        "goal": "Goal",
        "combat": "Combat",
        "interact": "Interaction",
        "menu": "Menus and dialogue",
        "stamina": "Stamina",
        "general": "Other",
    }
    grouped: Dict[str, List[str]] = {}
    dropped: List[str] = []
    for rule in rules:
        if rule.confidence < min_confidence:
            dropped.append(rule.text)
            continue
        grouped.setdefault(rule.section or "general", []).append(rule.text.strip())
    for section, texts in (extra_sections or {}).items():
        grouped.setdefault(section, []).extend(str(t).strip() for t in texts if str(t).strip())

    if not grouped:
        return f"{UNKNOWN_MARK}: no confirmed rules for this game yet; rely on the screen and trial-and-error.", dropped

    lines: List[str] = []
    for section in sorted(grouped, key=lambda s: list(section_titles).index(s) if s in section_titles else 99):
        title = section_titles.get(section, section)
        # 官方示例是 `## 小节名` + 顶层 `- ` 列表，和训练语料保持一致；缩进层级不要加。
        lines.append(f"## {title}")
        lines.extend(f"- {text}" for text in grouped[section])
    return "\n".join(lines), dropped


def _fingerprint(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def fingerprint_matches(motor: Dict[str, Any], fingerprint: str) -> bool:
    """已记录的指纹与本次构建的指纹一致 → 提示词没有实质变化。"""
    return str((motor or {}).get("system_prompt_fingerprint", "")) == str(fingerprint)


def profile_required_keys(profile: Profile) -> Sequence[str]:
    """profile 自己声明该学哪些键；没声明才退回通用默认表。

    为什么必须能覆盖：Undertale 这类菜单/像素游戏根本没有 WASD、Shift、1-4，
    通用默认表会往待学清单里塞一堆不存在的键——模型照着这份清单去试，就会答
    "Hold A"，然后被键位表直接判死（实测，2026-11-04）。
    """
    config = profile.ensure_config() if hasattr(profile, "ensure_config") else {}
    motor = (config or {}).get("motor") or {}
    declared = motor.get("required_keys")
    if isinstance(declared, (list, tuple)) and declared:
        return [str(k) for k in declared]
    return DEFAULT_REQUIRED_KEYS


def build_system_prompt(
    profile: Profile,
    *,
    template: Optional[str] = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    required_keys: Optional[Iterable[str]] = None,
    memory_rules: Sequence[Rule] = (),
    memory_bindings: Sequence[KeyBinding] = (),
    version: Optional[int] = None,
    instruction: str = "",
    rule_budget: int = rt.DEFAULT_RULE_BUDGET,
) -> PromptBundle:
    """把 profile（+ 记忆条目）拼成快系统要用的系统提示词。

    ``memory_rules`` / ``memory_bindings`` 是慢系统从 Graphiti 取回来、复核过的条目，
    与本地 profile 合并；同键以本地 profile 为准（本地是人手确认过的）。

    ``instruction`` 是大脑这一拍给的操作意图：拼成 ``## Current objective`` 追加在
    规则段末尾——这是它唯一合法的位置（GameScaling 没有 instruction slot，写进 user
    轮会跳出训练分布）。**指纹不含 objective**：版本号记的是「学到了什么」，不是
    「这一拍想干什么」，否则每一拍指令不同都要涨一次版本，FR-7 的收敛判断就废了。
    """
    tpl = template if template is not None else load_template()
    if required_keys is None:
        # 只有显式传 None 才按 profile 声明；传空列表 = 明确"没有待学键"。
        required_keys = profile_required_keys(profile)
    local_keys = profile.load_keymap()
    merged_keys: List[KeyBinding] = list(local_keys)
    local_names = {b.key.strip() for b in local_keys}
    for binding in memory_bindings:
        if binding.key.strip() and binding.key.strip() not in local_names:
            merged_keys.append(binding)

    rules: List[Rule] = list(profile.load_rules()) + list(memory_rules)

    # required_keys 里可能是语义名（confirm/menu），先翻成物理键再过词表：
    # 提示词里出现的必须全是训练 token，否则模型抄进动作就被服务端丢掉。
    semantic_map = _semantic_to_physical(profile)
    required_tokens: List[str] = []
    for name in required_keys:
        physical = semantic_map.get(str(name).strip(), [str(name)])
        for token in vocab.tokens_for_profile(physical):
            if token not in required_tokens:
                required_tokens.append(token)

    # ---- 规则模板库拼装（用户口径 m07071）-------------------------------
    # 大脑学到的每条规则同时是「模板」：rules.json 是学习流水（谁在什么时候学到什么），
    # 模板库是拼装素材（按 section / tags / keys 组织，可跨游戏共享）。这里把两者合成
    # 一个候选池：本游戏模板 + 共享模板 + 规则流水（历史数据即时升级成模板，不要求迁移），
    # 同正文只留一份，最后按 instruction 打分在预算内选入。
    candidates: List[rt.RuleTemplate] = []
    seen_bodies: Dict[str, rt.RuleTemplate] = {}
    try:
        library = rt.TemplateLibrary(Path(profile.root))
        library_templates = library.load(profile)
    except Exception:  # noqa: BLE001 - 模板库坏了不能阻断提示词构建
        library_templates = []
    for template in list(library_templates) + rt.templates_from_rules(rules, game_id=profile.game_id):
        body = template.body.strip()
        if body and body not in seen_bodies:
            seen_bodies[body] = template
            candidates.append(template)

    assembled = rt.assemble(
        candidates,
        instruction=instruction,
        values={
            "game_name": profile.display_name,
            "display_name": profile.display_name,
            "game_id": profile.game_id,
            "group_id": profile.group_id,
        },
        budget_chars=rule_budget,
        min_confidence=min_confidence,
    )
    rules_text = assembled["text"] or (
        f"{UNKNOWN_MARK}: no confirmed rules for this game yet; rely on the screen and trial-and-error."
    )
    # 规则是人写的自然语言，里面混着游戏说明书式的键名（ArrowLeft / Escape / a/d / 1-9:）。
    # 官方 prompt.py:113 的 translate_controls 就是干这个的：先翻成训练 token 再进提示词。
    # 不翻等于把词表外 token 喂给模型，它照着抄出来又被服务端丢掉。
    rules_text = vocab.translate_controls(rules_text)
    # 模板声明要用到的物理键排在键位表最前（列表位置就是注意力权重）。
    focus_tokens = vocab.tokens_for_profile(assembled["focus_keys"])
    by_id = {t.id: t for t in candidates}
    dropped_rules = [
        by_id[item].body
        for item in assembled["dropped"]
        if item in by_id and float(by_id[item].confidence) < min_confidence
    ]

    key_text, used_keys, missing, dropped_keys = build_key_bindings(
        merged_keys, required_keys=required_tokens, min_confidence=min_confidence,
        prefer=focus_tokens,
    )

    learning_lines: List[str] = []
    if missing:
        learning_lines.append("**Learning targets (unknown, learn during play)**")
        learning_lines.extend(
            f"- `{key}`: only try it while no menu/dialogue is open; if the screen does not change, it is not used by this game"
            for key in missing
        )
    learning = "\n".join(learning_lines)

    text = (
        tpl.replace("{GAME_NAME}", profile.display_name)
        .replace("{GAME_RULES}", rules_text)
        .replace("{KEY_BINDINGS}", key_text)
        .replace("{LEARNING}", learning)
    )
    text = text.replace("\n\n\n", "\n\n").strip() + "\n"

    config = profile.ensure_config()
    motor = config.get("motor") or {}
    # 指纹只覆盖「学到的东西」（规则骨架 + 键位），不覆盖本次 objective：
    # 版本号的语义是提示词第几次真的变化，不是第几次换了指令。
    skeleton = text
    objective_marker = "\n## " + rt.OBJECTIVE_TITLE
    cut = skeleton.find(objective_marker)
    if cut >= 0:
        skeleton = skeleton[:cut].rstrip() + "\n"
    fingerprint = _fingerprint(skeleton)
    disk_version = int(motor.get("system_prompt_version", 0))
    unchanged = fingerprint_matches(motor, fingerprint)
    if version is not None:
        resolved_version = int(version)
    elif unchanged:
        # 内容没变就不涨版本号：版本号的语义是「提示词第几次真的变化」。
        resolved_version = disk_version
    else:
        resolved_version = disk_version + 1

    if version is None and not unchanged:
        # 「指纹变了」本身必须立刻落盘，否则下一次 build 还会以为变了、再涨一次版本，
        # 磁盘永远落后内存一档（真实 bug：learn 之后 restart_fast 永远 ok=False）。
        # 指纹和版本必须在同一次写入里成对更新，build 才能做到幂等。
        config["motor"] = {
            **motor,
            "system_prompt_fingerprint": fingerprint,
            "system_prompt_version": resolved_version,
            "system_prompt_chars": len(text),
        }
        try:
            profile.save_config(config)
        except Exception:  # noqa: BLE001 - 落盘失败不阻断构建
            pass
    bundle = PromptBundle(
        text=text,
        game_name=profile.display_name,
        fingerprint=fingerprint,
        version=resolved_version,
        learning_targets=missing,
        used_keys=used_keys,
        dropped_low_confidence=dropped_keys + dropped_rules,
        templates_used=list(assembled["used"]),
        objective_included=bool(assembled["objective"]),
        instruction=str(instruction or ""),
    )
    return bundle


def record_prompt_version(profile: Profile, bundle: PromptBundle) -> Dict[str, Any]:
    """把本次生效的提示词版本写进 profile 配置（FR-7 的可观测性）。"""
    config = profile.ensure_config()
    motor = dict(config.get("motor") or {})
    motor["system_prompt_version"] = bundle.version
    motor["system_prompt_fingerprint"] = bundle.fingerprint
    motor["system_prompt_chars"] = len(bundle.text)
    motor["system_prompt_updated_at"] = __import__("time").strftime("%Y-%m-%dT%H:%M:%S")
    config["motor"] = motor
    profile.save_config(config)
    return config


def dump_bundle(bundle: PromptBundle, path: Path) -> Path:
    """落盘一份快照，用于重启前后 diff。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bundle.text, encoding="utf-8")
    meta = path.with_suffix(".json")
    meta.write_text(
        json.dumps(bundle.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------
# 手模型（motor）system：便利贴三槽位拼装（02_schemas.md §4.2）
# ---------------------------------------------------------------------
#: 官方 GameScaling 鼠标语义行的固定原文。便利贴里没写鼠标时这一行也必须在：
#: 手模型训练时见过它，缺了它鼠标动作会退化。
MOUSE_BINDING_LINE = "- Mouse X Y -> aim / rotate the camera (X>0 right, Y>0 down)"

#: 三槽位，顺序与 02_schemas.md §4.2 一致。
MOTOR_SLOTS = ("GAME_NAME", "GAME_RULES", "KEY_BINDINGS")

#: 模板里可能出现、但本函数不负责填的其余槽位（编排层另有实现）：显式填空串。
_OTHER_SLOTS = ("LEARNING", "TASK", "DONE_HINT", "SPEAK_POLICY")

#: 本函数自己填、不属于便利贴的槽位（鼠标语义行，README 规定只在鼠标控制视角时加）。
_FILLED_EXTRA = ("MOUSE_LINE",)

_PLACEHOLDER_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
_MOUSE_LINE_RE = re.compile(r"(?im)^[ \t]*-[ \t]*Mouse[ \t]+X[ \t]+Y\b.*$")


def load_motor_template(path: Optional[Path] = None) -> str:
    """读手模型三槽位模板；不存在直接抛，绝不退回空模板假装拼好了。"""
    target = Path(path) if path else MOTOR_TEMPLATE_PATH
    if not target.exists():
        raise FileNotFoundError(f"手模型提示词模板不存在：{target}")
    return target.read_text(encoding="utf-8")


def _slot_text(value: Any) -> str:
    """槽位值规整：None → 空串，其它转字符串并去掉首尾空白。"""
    if value is None:
        return ""
    return str(value).strip()


def assemble_motor_system(
    game: Any,
    rules: Any,
    key_bindings: Any,
    *,
    template: Optional[str] = None,
    mouse_aim: bool = True,
) -> str:
    """用模板把便利贴两块 + 游戏名拼成手模型的完整 system 字符串。

    产物就是 gamescaling_request.build_messages 的 system 参数（单条字符串）：
    模板里 GAME_NAME / GAME_RULES / KEY_BINDINGS 三槽位被真实内容填满，
    没给内容的槽位填空串——绝不留未替换的花括号（模型会把 {KEY_BINDINGS}
    当字面文本抄进动作，表现为空动作/乱按）。

    鼠标行（README 原文字面量 MOUSE_BINDING_LINE）只在 mouse_aim=True 时出现：
    官方的口径是「仅当鼠标控制视角时才把这一行加进 KEY_BINDINGS」。键盘游戏
    （Undertale 这类）带上这行会被诱导输出 LB/RB/MB——玩家按不出这些东西。
    mouse_aim=True 时便利贴里重复的鼠标行删掉去重，避免出现两条。
    """
    tpl = template if template is not None else load_motor_template()
    unknown = sorted(
        {
            name
            for name in _PLACEHOLDER_RE.findall(tpl)
            if name not in MOTOR_SLOTS
            and name not in _OTHER_SLOTS
            and name not in _FILLED_EXTRA
        }
    )
    if unknown:
        raise ValueError(
            f"手模型模板里有未定义的槽位 {unknown}；"
            f"只有 {list(MOTOR_SLOTS)} 由便利贴填充（02 §4.2）"
        )

    key_text = _slot_text(key_bindings)
    if mouse_aim and key_text:
        # 模板已固定鼠标行：便利贴里重复的那行删掉，其余内容原样保留。
        key_text = _MOUSE_LINE_RE.sub("", key_text).strip()
    mouse_text = MOUSE_BINDING_LINE if mouse_aim else ""

    text = (
        tpl.replace("{GAME_NAME}", _slot_text(game))
        .replace("{GAME_RULES}", _slot_text(rules))
        .replace("{KEY_BINDINGS}", key_text)
        .replace("{MOUSE_LINE}", mouse_text)
    )
    for name in _OTHER_SLOTS:
        # 这些槽位本函数不填（编排层另有实现）：显式填空串，不留花括号。
        text = text.replace("{" + name + "}", "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    return text
