"""游戏 profile：每个游戏一套独立配置、键位表、规则库、便签（短期记忆）。

需求对照（Docs/requirements/REQ-2026-11-04-dual-system-multi-game.md）：

* FR-8 多游戏 profile：目录 ``profiles/<game_id>/`` 各自独立；``profiles/registry.json``
  维护规范名与别名。**游戏名称严格对齐**：写进记忆的 ``group_id``、系统提示词的
  ``GAME_NAME``、日志、指标全部用同一个规范名；别名只用于把用户输入解析到 game_id。
* FR-5 短期记忆：LLM 自己写「提示纸条」（scratchpad.txt），有硬字符上限，超限拒绝
  并返回剩余容量——不会静默截断，也不会无限增长。
* FR-6 提示词素材：键位表与规则库是构造 GameScaling 系统提示词的唯一来源；
  没学到的键位必须显式标"未知"，不许编。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import sticky


class ProfileError(RuntimeError):
    """profile 相关错误：游戏不存在、别名冲突、便签超限等。"""


#: profile 根目录；可用 BETTERAGI_PROFILES 覆盖（测试把它指到临时目录）。
DEFAULT_PROFILE_ROOT = Path(os.environ.get("BETTERAGI_PROFILES") or r"E:\BetterAGI\profiles")

#: 便签默认字符上限 = 便利贴 1K+1K 的总配额（02_schemas.md §7.2）。
#: 便签是便利贴的裸文本视图：同样只准慢系统写、超限拒绝、绝不截断。
DEFAULT_NOTE_LIMIT = sticky.STICKY_TOTAL_LIMIT

#: 便签单条最大长度 = 单块 1K，防止一次写入就撑满一块。
MAX_NOTE_CHUNK = sticky.STICKY_BLOCK_LIMIT


def slugify(name: str) -> str:
    """把游戏名规整成 game_id：小写、空白转连字符、去掉标点。"""
    text = (name or "").strip().lower()
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"[^a-z0-9\-\u4e00-\u9fff]", "", text)
    return text.strip("-")


@dataclass
class KeyBinding:
    """一个按键 → 效果。confidence < 1 表示来自快系统学习、尚未复核。"""

    key: str
    effect: str
    confidence: float = 1.0
    source: str = "manual"  # manual | learned | inferred

    def line(self) -> str:
        return f"- {self.key} -> {self.effect_text()}"

    def physical_keys(self) -> List[str]:
        """这个绑定背后真正的物理键。

        effect 有两种写法：学习时写入的 `physical:z/enter`，以及人手写的效果描述
        （`Confirm / interact`）。前者直接解析出物理键；后者把语义键名当作唯一
        物理键——提示词渲染时再去训练词表里查，查不到就丢掉。
        """
        text = str(self.effect or "").strip()
        if text.startswith("physical:"):
            return [k.strip() for k in text[len("physical:"):].split("/") if k.strip()]
        return [str(self.key or "").strip()]

    def effect_text(self) -> str:
        """给模型看的效果描述：不把 `physical:z` 这种内部写法漏进提示词。"""
        text = str(self.effect or "").strip()
        if text.startswith("physical:"):
            keys = " / ".join(self.physical_keys()) or "?"
            return f"{keys} ({self.key})"
        return text or str(self.key)


@dataclass
class Rule:
    """一条游戏规则/机制。section 用来分组：画面、目标、胜负、菜单…"""

    section: str
    text: str
    confidence: float = 1.0
    source: str = "manual"


@dataclass
class NoteStats:
    chars: int
    limit: int
    remaining: int
    lines: int


class Profile:
    """一个游戏的全套运行数据。所有读写都限定在自己的目录里。"""

    def __init__(self, root: Path, game_id: str, meta: Dict[str, Any]):
        self.root = Path(root)
        self.game_id = game_id
        self.meta = dict(meta or {})
        self.dir = self.root / game_id
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- 身份 -------------------------------------------------------
    @property
    def display_name(self) -> str:
        """规范显示名：写提示词、写记忆、写日志都用它，别名不进这里。"""
        return str(self.meta.get("display_name") or self.game_id)

    @property
    def group_id(self) -> str:
        """Graphiti 记忆分区键。默认与 game_id 相同（严格对齐，不做二次命名）。"""
        return str(self.meta.get("group_id") or self.game_id)

    @property
    def aliases(self) -> List[str]:
        return [str(a) for a in (self.meta.get("aliases") or [])]

    @property
    def note_limit(self) -> int:
        """便签上限：profile 可声明更小的值，但不可能超过 1K+1K 的硬上限。"""
        declared = int(self.meta.get("note_limit") or DEFAULT_NOTE_LIMIT)
        return max(1, min(declared, DEFAULT_NOTE_LIMIT))

    # ---- 文件路径 ---------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.dir / "config.json"

    @property
    def keymap_path(self) -> Path:
        return self.dir / "keymap.json"

    @property
    def rules_path(self) -> Path:
        return self.dir / "rules.json"

    @property
    def note_path(self) -> Path:
        return self.dir / "scratchpad.txt"

    @property
    def log_dir(self) -> Path:
        path = self.dir / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---- JSON 读写 --------------------------------------------------
    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as error:  # noqa: BLE001 - 坏文件要报出来，不要静默当空
            raise ProfileError(f"{path} 不是合法 JSON：{error}") from error

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    # ---- 配置 -------------------------------------------------------
    def load_config(self) -> Dict[str, Any]:
        return self._read_json(self.config_path, {})

    def save_config(self, payload: Dict[str, Any]) -> None:
        self._write_json(self.config_path, payload)

    def ensure_config(self) -> Dict[str, Any]:
        """首次运行时落一份默认配置（omni / motor 端点留空，由 profile 填）。"""
        config = self.load_config()
        if not config:
            config = {
                "game_id": self.game_id,
                "display_name": self.display_name,
                "group_id": self.group_id,
                "note_limit": self.note_limit,
                "omni": {"model": "qwen3.8-omni-flash-realtime", "endpoint": "", "voice": ""},
                "motor": {"provider": "gamescaling", "endpoint": "", "system_prompt_version": 0},
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self.save_config(config)
        return config

    # ---- 键位表 -----------------------------------------------------
    def load_keymap(self) -> List[KeyBinding]:
        raw = self._read_json(self.keymap_path, {"bindings": []})
        items = raw.get("bindings") if isinstance(raw, dict) else raw
        out: List[KeyBinding] = []
        for item in items or []:
            if isinstance(item, str):
                key, _, effect = item.partition("->")
                out.append(KeyBinding(key.strip(), effect.strip()))
            else:
                out.append(
                    KeyBinding(
                        key=str(item.get("key", "")).strip(),
                        effect=str(item.get("effect", "")).strip(),
                        confidence=float(item.get("confidence", 1.0)),
                        source=str(item.get("source", "manual")),
                    )
                )
        return [b for b in out if b.key and b.effect]

    def save_keymap(self, bindings: Iterable[KeyBinding]) -> None:
        payload = {"game_id": self.game_id, "bindings": [asdict(b) for b in bindings]}
        self._write_json(self.keymap_path, payload)

    # ---- 规则库 -----------------------------------------------------
    def load_rules(self) -> List[Rule]:
        raw = self._read_json(self.rules_path, {"rules": []})
        items = raw.get("rules") if isinstance(raw, dict) else raw
        out: List[Rule] = []
        for item in items or []:
            if isinstance(item, str):
                out.append(Rule("general", item.strip()))
            else:
                out.append(
                    Rule(
                        section=str(item.get("section", "general")),
                        text=str(item.get("text", "")).strip(),
                        confidence=float(item.get("confidence", 1.0)),
                        source=str(item.get("source", "manual")),
                    )
                )
        return [r for r in out if r.text]

    def save_rules(self, rules: Iterable[Rule]) -> None:
        payload = {"game_id": self.game_id, "rules": [asdict(r) for r in rules]}
        self._write_json(self.rules_path, payload)

    # ---- 便签（短期记忆，FR-5）--------------------------------------
    def read_notes(self) -> str:
        """读便签；文件不存在返回空串（还没写过是正常状态）。"""
        try:
            return sticky.read_text(self.note_path)
        except sticky.StickyError as error:
            raise ProfileError(f"读取便签失败：{error}") from error

    def note_stats(self) -> NoteStats:
        text = self.read_notes()
        limit = self.note_limit
        return NoteStats(
            chars=len(text),
            limit=limit,
            remaining=max(0, limit - len(text)),
            lines=0 if not text else text.count("\n") + 1,
        )

    def write_note(self, text: str, mode: str = "append") -> NoteStats:
        """写便签。超限直接拒绝（抛 ProfileError），绝不静默截断。

        mode="append" 追加一段；mode="replace" 整体覆盖。
        """
        chunk = (text or "").strip()
        if not chunk:
            raise ProfileError("便签内容为空，没写")
        if len(chunk) > MAX_NOTE_CHUNK:
            raise ProfileError(
                f"单条便签最多 {MAX_NOTE_CHUNK} 字，这次 {len(chunk)} 字；请拆成几条更短的"
            )
        current = self.read_notes()
        if mode == "replace":
            candidate = chunk
        elif mode == "append":
            candidate = chunk if not current.strip() else current.rstrip("\n") + "\n" + chunk
        else:
            raise ProfileError(f"未知 mode {mode!r}，只能是 append / replace")
        limit = self.note_limit
        if len(candidate) > limit:
            raise ProfileError(
                f"便签上限 {limit} 字，写入后会到 {len(candidate)} 字（超出 {len(candidate) - limit}）。"
                f"当前 {len(current)} 字，剩余 {max(0, limit - len(current))} 字；先 clear 或 replace 精简再写"
            )
        # 落盘走便利贴同一套原子写（tmp → fsync → rename）：便签也不许写半截。
        try:
            sticky.atomic_write_text(self.note_path, candidate, backup=False)
        except sticky.StickyError as error:
            raise ProfileError(f"写便签失败：{error}") from error
        return self.note_stats()

    def clear_notes(self) -> NoteStats:
        """清空便签（同样原子写，不留半截文件）。"""
        try:
            sticky.atomic_write_text(self.note_path, "", backup=False)
        except sticky.StickyError as error:
            raise ProfileError(f"清空便签失败：{error}") from error
        return self.note_stats()


# ---------------------------------------------------------------------
# registry：规范名 + 别名 → game_id
# ---------------------------------------------------------------------

REGISTRY_NAME = "registry.json"


def _registry_path(root: Path) -> Path:
    return Path(root) / REGISTRY_NAME


def load_registry(root: Path = DEFAULT_PROFILE_ROOT) -> Dict[str, Any]:
    path = _registry_path(root)
    if not path.exists():
        return {"version": 1, "games": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:  # noqa: BLE001
        raise ProfileError(f"{path} 不是合法 JSON：{error}") from error
    data.setdefault("version", 1)
    data.setdefault("games", {})
    return data


def save_registry(registry: Dict[str, Any], root: Path = DEFAULT_PROFILE_ROOT) -> None:
    path = _registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def list_games(root: Path = DEFAULT_PROFILE_ROOT) -> List[str]:
    return sorted(load_registry(root)["games"].keys())


def register_game(
    game_id: str,
    *,
    display_name: str = "",
    aliases: Optional[Iterable[str]] = None,
    root: Path = DEFAULT_PROFILE_ROOT,
    note_limit: Optional[int] = None,
) -> Profile:
    """把游戏登记进 registry（已存在则更新别名与显示名）。"""
    gid = slugify(game_id)
    if not gid:
        raise ProfileError(f"非法 game_id：{game_id!r}")
    registry = load_registry(root)
    entry = dict(registry["games"].get(gid) or {})
    entry["display_name"] = display_name or entry.get("display_name") or game_id
    entry["group_id"] = entry.get("group_id") or gid  # 严格对齐，不做二次命名
    merged = [*(entry.get("aliases") or [])]
    for alias in aliases or []:
        text = str(alias).strip()
        if text and text not in merged:
            merged.append(text)
    entry["aliases"] = merged
    if note_limit is not None:
        entry["note_limit"] = int(note_limit)
    registry["games"][gid] = entry
    _assert_alias_unique(registry)
    save_registry(registry, root)
    return Profile(root, gid, entry)


def _assert_alias_unique(registry: Dict[str, Any]) -> None:
    """别名必须唯一，否则 --game 解析会出现歧义（游戏名严格对齐的一部分）。"""
    seen: Dict[str, str] = {}
    for gid, entry in registry["games"].items():
        names = [slugify(gid), slugify(str(entry.get("display_name", "")))]
        names += [slugify(str(a)) for a in (entry.get("aliases") or [])]
        for name in names:
            if not name:
                continue
            other = seen.get(name)
            if other and other != gid:
                raise ProfileError(f"名字 {name!r} 同时属于 {other} 与 {gid}，别名冲突")
            seen[name] = gid


def apply_learned_key(
    profile: Profile,
    name: str,
    physical: Sequence[str],
    *,
    confidence: float = 0.6,
    source: str = "learned",
) -> Dict[str, Any]:
    """把 agent 学到的「语义键名 → 物理键」写进 profile。

    两处都写，职责分开：
      * keymap.json —— 学习记录（人可读、带 confidence/source），提示词从这里生成；
      * config.json 的 adapter.keys —— 运行时真按键映射，adapter 从这里解析。
    重复学习同一条：更新 confidence 与 source，不新增重复行。
    """
    keys = [str(k).strip().lower() for k in physical if str(k).strip()]
    if not name or not str(name).strip():
        raise ProfileError("键名不能为空")
    if not keys:
        raise ProfileError(f"给 {name!r} 学习物理键时必须至少给一个键")
    name = str(name).strip().lower()

    config = profile.ensure_config()
    adapter = dict(config.get("adapter") or {})
    adapter_keys = dict(adapter.get("keys") or {})
    adapter_keys[name] = keys[0] if len(keys) == 1 else keys
    adapter["keys"] = adapter_keys
    config["adapter"] = adapter
    profile.save_config(config)

    bindings = profile.load_keymap()
    effect = "physical:" + "/".join(keys)
    existing = None
    for binding in bindings:
        if binding.key.strip().lower() == name:
            existing = binding
            break
    if existing is None:
        bindings.append(KeyBinding(name, effect, confidence, source))
        action = "added"
    elif existing.source == "manual" and confidence < existing.confidence:
        return {"action": "skipped", "reason": "manual 键位置信度更高，不覆盖",
                "key": name, "effect": existing.effect}
    else:
        existing.effect = effect
        existing.confidence = confidence
        existing.source = source
        action = "updated"
    profile.save_keymap(bindings)
    return {"action": action, "key": name, "effect": effect,
            "physical": keys, "confidence": confidence, "source": source}


def resolve_game(name: str, root: Path = DEFAULT_PROFILE_ROOT) -> Profile:
    """把用户输入（规范名/别名/中文名）解析成唯一 profile；歧义直接报错。"""
    probe = slugify(name)
    if not probe:
        raise ProfileError(f"游戏名为空或非法：{name!r}")
    registry = load_registry(root)
    for gid, entry in registry["games"].items():
        candidates = {slugify(gid), slugify(str(entry.get("display_name", "")))}
        for alias in entry.get("aliases") or []:
            candidates.add(slugify(str(alias)))
        candidates.discard("")
        if probe in candidates:
            return Profile(root, gid, entry)
    known = ", ".join(sorted(registry["games"])) or "（无）"
    raise ProfileError(
        f"没有登记过的游戏 {name!r}；已登记：{known}。先用 register_game 登记"
    )


def open_profile(name: str, root: Path = DEFAULT_PROFILE_ROOT) -> Profile:
    profile = resolve_game(name, root)
    profile.ensure_config()
    return profile
