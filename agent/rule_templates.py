"""规则模板库（用户 2026-11-04 口径）：大脑学到的知识先落成模板，再按意图拼装。

为什么不是「一堆平铺规则」：显式要求——「规则和按键绑定是大脑 LLM 做的规划……他学到
任何规则之后，就会把这些内容记录进长期记忆，同时也记录在规则模板里，这样在後面，大脑
LLM 就可以把几个规则模板拼起来再加上 instruction 来拼成 game rules 和 key binding，
快速调用模型。」所以分三层：

1. **模板（RuleTemplate）**：一条可复用的知识片段，带 section / tags / 需要的物理键；
   正文允许 ``{game_name}`` 这类占位符。共享模板放 ``profiles/_templates/``，单个游戏
   学到的放 ``profiles/<game>/templates.json``。
2. **选择（select_templates）**：按 section 权重 + instruction 关键词命中 + 置信度打分，
   在字符预算内贪心取用——提示词长度是有代价的，不能把所有学到的东西全塞给策略模型。
3. **拼装（assemble）**：选中的模板按固定 section 顺序渲染成 ``## Title`` + ``- 文本``，
   再把 instruction 作为 ``## Current objective`` 追加；同时给出 focus_keys（模板声明的
   物理键），交给键位段排序，把本次真正用得上的键排前面。

契约（别改坏）：拼出来的文本要能被 prompt_builder 直接塞进官方模板的 ``{GAME_RULES}``，
所以段标题一律英文、列表一律顶层 ``- ``、不缩进、不写 Markdown 装饰。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


#: section 的固定顺序 = 渲染顺序。顺序本身就是信息：先看画面，再谈目标。
SECTION_ORDER: Tuple[str, ...] = ("view", "goal", "combat", "interact", "menu", "stamina", "general")

#: 段标题必须英文：整段会喂给策略模型，中文段标题会让它退回全零动作（实测）。
SECTION_TITLES: Dict[str, str] = {
    "view": "Screen",
    "goal": "Goal",
    "combat": "Combat",
    "interact": "Interaction",
    "menu": "Menus and dialogue",
    "stamina": "Stamina",
    "general": "Other",
}

#: section 基础权重：目标与画面最重要，体力这种细节最后。
SECTION_WEIGHTS: Dict[str, float] = {
    "view": 2.2,
    "goal": 3.0,
    "combat": 1.6,
    "interact": 2.0,
    "menu": 1.6,
    "stamina": 0.6,
    "general": 1.0,
}

#: 默认字符预算：整个 {GAME_RULES} 段（含 objective）不超过这个长度。
DEFAULT_RULE_BUDGET = 1400

#: instruction 拼进规则段时用的段标题。
OBJECTIVE_TITLE = "Current objective"


def slugify(text: str) -> str:
    """模板 id 用：小写、非字母数字转连字符。"""
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", str(text or "").strip().lower())
    return value.strip("-")


@dataclass
class RuleTemplate:
    """一条可复用的知识模板。"""

    id: str
    title: str
    body: str
    section: str = "general"
    tags: Tuple[str, ...] = ()
    keys: Tuple[str, ...] = ()
    confidence: float = 1.0
    source: str = "manual"  # manual | learned | memory
    game_id: str = ""  # 空 = 共享模板（跨游戏可用）
    updated_at: str = ""

    def render(self, values: Optional[Dict[str, Any]] = None) -> str:
        """把 ``{game_name}`` 之类占位符换成实际值；缺值保留原样，不编。"""
        text = str(self.body or "")
        for name, value in (values or {}).items():
            text = text.replace("{" + str(name) + "}", str(value))
        return text.strip()

    def missing_placeholders(self) -> List[str]:
        return sorted(set(re.findall(r"\{([a-z_][a-z0-9_]*)\}", str(self.body or ""))))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "section": self.section,
            "tags": list(self.tags),
            "keys": list(self.keys),
            "confidence": float(self.confidence),
            "source": self.source,
            "game_id": self.game_id,
            "updated_at": self.updated_at or time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RuleTemplate":
        section = str(payload.get("section") or "general").strip().lower()
        if section not in SECTION_TITLES:
            section = "general"
        body = str(payload.get("body") or payload.get("text") or "").strip()
        title = str(payload.get("title") or "").strip() or (body[:40] if body else "untitled")
        tid = str(payload.get("id") or slugify(title) or "tpl").strip()
        return cls(
            id=tid,
            title=title,
            body=body,
            section=section,
            tags=tuple(str(t).strip() for t in (payload.get("tags") or ()) if str(t).strip()),
            keys=tuple(str(k).strip() for k in (payload.get("keys") or ()) if str(k).strip()),
            confidence=float(payload.get("confidence", 1.0)),
            source=str(payload.get("source") or "manual"),
            game_id=str(payload.get("game_id") or ""),
            updated_at=str(payload.get("updated_at") or ""),
        )


def template_from_rule(section: str, text: str, *, confidence: float = 0.6,
                       source: str = "learned", game_id: str = "") -> RuleTemplate:
    """把一条扁平规则升级成模板（老数据不用迁移，读的时候即时转换）。"""
    body = str(text or "").strip()
    tid = (slugify(section) + "-" + slugify(body))[:80] or "rule"
    return RuleTemplate(
        id=tid,
        title=body[:60],
        body=body,
        section=section if section in SECTION_TITLES else "general",
        tags=(section,),
        confidence=float(confidence),
        source=source,
        game_id=game_id,
    )


def templates_from_rules(rules: Iterable[Any], *, game_id: str = "") -> List[RuleTemplate]:
    """把 profiles.Rule 列表转成模板列表；同一段正文只留一份。"""
    out: List[RuleTemplate] = []
    seen: set = set()
    for rule in rules or []:
        text = str(getattr(rule, "text", "") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(template_from_rule(
            str(getattr(rule, "section", "general") or "general"),
            text,
            confidence=float(getattr(rule, "confidence", 0.6) or 0.6),
            source=str(getattr(rule, "source", "learned") or "learned"),
            game_id=game_id,
        ))
    return out


def _instruction_terms(instruction: str) -> List[str]:
    """把 instruction 切成可匹配的词：英文 3 字母以上、中文按单字。"""
    text = str(instruction or "").lower()
    return re.findall(r"[a-z0-9]{3,}|[\u4e00-\u9fff]", text)


def score_template(template: RuleTemplate, instruction: str = "",
                   focus: Sequence[str] = ()) -> float:
    """打分：section 权重 × 置信度 + instruction 命中 + 指定 section 加分。"""
    base = SECTION_WEIGHTS.get(template.section, 1.0)
    score = base * max(0.0, min(1.0, float(template.confidence)))
    haystack = (template.title + " " + template.body + " " + " ".join(template.tags)).lower()
    hits = sum(1 for term in _instruction_terms(instruction) if term in haystack)
    score += min(2.0, 0.4 * float(hits))
    if template.section in (focus or ()):
        score += 1.0
    return round(score, 4)


def select_templates(
    templates: Sequence[RuleTemplate],
    *,
    instruction: str = "",
    budget_chars: int = DEFAULT_RULE_BUDGET,
    min_confidence: float = 0.5,
    focus: Sequence[str] = (),
    max_items: int = 40,
) -> Tuple[List[RuleTemplate], List[RuleTemplate]]:
    """按分数贪心取用，返回 (选中, 被丢)。预算按渲染后文本长度算。"""
    candidates = [t for t in templates if str(t.body or "").strip()]
    low = [t for t in candidates if float(t.confidence) < float(min_confidence)]
    ranked = sorted(
        [t for t in candidates if float(t.confidence) >= float(min_confidence)],
        key=lambda t: (score_template(t, instruction, focus), t.id),
        reverse=True,
    )
    used: List[RuleTemplate] = []
    dropped: List[RuleTemplate] = list(low)
    spent = 0
    for template in ranked:
        if len(used) >= max(1, int(max_items)):
            dropped.append(template)
            continue
        cost = len(template.body) + len(SECTION_TITLES.get(template.section, template.section)) + 6
        if spent + cost > max(120, int(budget_chars)):
            dropped.append(template)
            continue
        used.append(template)
        spent += cost
    return used, dropped


def render_rules(
    templates: Sequence[RuleTemplate],
    *,
    values: Optional[Dict[str, Any]] = None,
    instruction: str = "",
) -> Tuple[str, List[str], bool]:
    """渲染成 ``## Title`` + ``- 文本``，再追加 objective 段。"""
    grouped: Dict[str, List[str]] = {}
    missing: List[str] = []
    for template in templates:
        text = template.render(values)
        if not text:
            continue
        missing.extend(template.missing_placeholders())
        grouped.setdefault(template.section, []).append(text)
    lines: List[str] = []
    for section in SECTION_ORDER:
        texts = grouped.get(section)
        if not texts:
            continue
        lines.append("## " + SECTION_TITLES.get(section, section))
        lines.extend("- " + text for text in texts)
    objective = str(instruction or "").strip()
    if objective:
        if lines:
            lines.append("")
        lines.append("## " + OBJECTIVE_TITLE)
        lines.append("- " + objective)
    return "\n".join(lines), sorted(set(missing)), bool(objective)


def assemble(
    templates: Sequence[RuleTemplate],
    *,
    instruction: str = "",
    values: Optional[Dict[str, Any]] = None,
    budget_chars: int = DEFAULT_RULE_BUDGET,
    min_confidence: float = 0.5,
    focus: Sequence[str] = (),
) -> Dict[str, Any]:
    """选择 + 渲染，一次给出规则段文本和它用到的信息（可回溯）。"""
    used, dropped = select_templates(
        templates,
        instruction=instruction,
        budget_chars=budget_chars,
        min_confidence=min_confidence,
        focus=focus,
    )
    text, missing, has_objective = render_rules(used, values=values, instruction=instruction)
    focus_keys: List[str] = []
    for template in used:
        for key in template.keys:
            if key not in focus_keys:
                focus_keys.append(key)
    return {
        "text": text,
        "used": [t.id for t in used],
        "dropped": [t.id for t in dropped],
        "chars": len(text),
        "objective": has_objective,
        "missing_placeholders": missing,
        "focus_keys": focus_keys,
    }


class TemplateLibrary:
    """模板库的落盘门面：共享模板 + 每个游戏自己的模板。"""

    def __init__(self, root: Path):
        self.root = Path(root)

    # ---- 路径 -----------------------------------------------------
    @property
    def shared_path(self) -> Path:
        return self.root / "_templates" / "templates.json"

    def game_path(self, profile: Any) -> Path:
        base = getattr(profile, "dir", None)
        if base is None:
            raise ValueError("需要带 dir 的 profile")
        return Path(base) / "templates.json"

    # ---- 读写 -----------------------------------------------------
    def _read(self, path: Path) -> List[RuleTemplate]:
        import json

        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 坏文件当成没有，别把启动搞死
            return []
        items = raw.get("templates") if isinstance(raw, dict) else raw
        return [RuleTemplate.from_dict(item) for item in (items or []) if isinstance(item, dict)]

    def _write(self, path: Path, templates: Sequence[RuleTemplate]) -> None:
        import json

        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "templates": [t.to_dict() for t in templates]}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def shared(self) -> List[RuleTemplate]:
        return self._read(self.shared_path)

    def for_game(self, profile: Any) -> List[RuleTemplate]:
        return self._read(self.game_path(profile))

    def load(self, profile: Optional[Any] = None) -> List[RuleTemplate]:
        """共享 + 本游戏；同 id 以本游戏为准。"""
        merged: Dict[str, RuleTemplate] = {t.id: t for t in self.shared()}
        if profile is not None:
            for template in self.for_game(profile):
                merged[template.id] = template
        return list(merged.values())

    def save_game(self, profile: Any, templates: Sequence[RuleTemplate]) -> None:
        gid = str(getattr(profile, "game_id", "") or "")
        stamped = []
        for template in templates:
            item = RuleTemplate(**{**template.__dict__})
            item.game_id = item.game_id or gid
            item.updated_at = item.updated_at or time.strftime("%Y-%m-%dT%H:%M:%S")
            stamped.append(item)
        self._write(self.game_path(profile), stamped)

    def upsert(self, profile: Any, template: RuleTemplate) -> Dict[str, Any]:
        """新增或更新一个游戏模板；同 id 覆盖，同正文视为重复。"""
        items = self.for_game(profile)
        gid = str(getattr(profile, "game_id", "") or "")
        template.game_id = template.game_id or gid
        template.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        for index, existing in enumerate(items):
            if existing.id == template.id or existing.body.strip() == template.body.strip():
                if existing.body.strip() == template.body.strip() and float(template.confidence) <= float(existing.confidence):
                    return {"action": "duplicate", "id": existing.id, "updated_at": existing.updated_at}
                items[index] = template
                self._write(self.game_path(profile), items)
                return {"action": "updated", "id": template.id, "updated_at": template.updated_at}
        items.append(template)
        self._write(self.game_path(profile), items)
        return {"action": "added", "id": template.id, "updated_at": template.updated_at}

    def seed_shared(self, templates: Sequence[RuleTemplate]) -> int:
        """写入共享模板（已存在的同 id 不覆盖），返回新增数量。"""
        existing = {t.id: t for t in self.shared()}
        added = 0
        for template in templates:
            if template.id in existing:
                continue
            existing[template.id] = template
            added += 1
        if added:
            self._write(self.shared_path, list(existing.values()))
        return added
