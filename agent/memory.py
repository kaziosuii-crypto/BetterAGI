"""长期记忆：每游戏一个 group_id（FR-4）。

设计要点：

* **隔离靠 group_id**：任何读写都必须带 group_id，且必须等于 profile 的 group_id；
  跨游戏读写直接抛错，而不是"顺手"读到别的游戏。
* **两种后端**：``GraphitiMemory``（生产，时序知识图谱）与 ``LocalJsonMemory``
  （无 Graphiti/Neo4j 时的本地落盘兜底）。两者同一套接口，切换只改配置。
* **写入要带来源与置信度**：``source``/``confidence`` 是慢系统复核与提示词门控的依据；
  低置信度条目不会自动进快系统提示词。
"""

from __future__ import annotations

import difflib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


class MemoryError_(RuntimeError):
    """记忆层错误（名字带下划线是为了不与内建 MemoryError 混淆）。"""


#: 事件类型：键位、规则、地点/NPC、剧情、教训、策略。
EPISODE_KINDS = ("keymap", "rule", "place", "story", "lesson", "strategy")

#: 来源可信度排序（越靠前越可信）。人工确认过的事实不该被模型推断覆盖。
_SOURCE_RANK = ("manual", "fast", "slow", "graphiti")


def _better_source(a: str, b: str) -> str:
    """返回更可信的来源；认不出的来源排在最后，不会因为拼错就抢走署名。"""

    def rank(value: str) -> int:
        try:
            return _SOURCE_RANK.index(str(value))
        except ValueError:
            return len(_SOURCE_RANK)

    return str(a) if rank(a) <= rank(b) else str(b)


#: 五层记忆：工作/情景/语义/程序性/社交（设计 02_schemas §8）。
#: 旧 JSONL 没有 layer 字段时按 episodic 读，保证历史记忆不丢。
MEMORY_LAYERS = ("working", "episodic", "semantic", "procedural", "social")

#: 层权重：程序性记忆（机制/键位）最值钱，检索时 ×1.5（设计 §8.3），
#: 其余层给一个温和的偏置；权重只影响注入排序，不影响条目是否保留。
LAYER_WEIGHTS = {
    "working": 1.2,
    "episodic": 1.0,
    "semantic": 1.1,
    "procedural": 1.5,
    "social": 1.1,
}

#: 原子事实的固定字段；写入前统一规范化，多余字段丢掉，缺的给默认。
ATOM_FIELDS = ("text", "kind", "confidence", "source_episode", "spoiler")


def _as_float(value: Any, default: float) -> float:
    """容忍旧数据里的字符串数字；实在转不了就用默认值（不抛，迁移期必须能读）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_scope_list(value: Any) -> List[str]:
    """group_ids 兼容读：旧数据是单个 str，新数据是 list；统一成去重后的 list。"""
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value)
    out: List[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _coerce_layer(value: Any) -> str:
    """层名校验：认不出的层按情景记忆处理（旧数据/拼错都不该让读取失败）。"""
    layer = str(value or "").strip()
    return layer if layer in MEMORY_LAYERS else "episodic"


def normalize_atom(atom: Any, *, source_episode: str = "") -> Dict[str, Any]:
    """把一条原子事实规范化成 {text, kind, confidence, source_episode}。

    空事实直接拒绝：宁可报错，也不往记忆里塞一条没有信息量的节点。
    """

    if not isinstance(atom, dict):
        raise MemoryError_(f"原子事实必须是 dict，拿到 {type(atom).__name__}")
    text = " ".join(str(atom.get("text") or "").split())
    if not text:
        raise MemoryError_("原子事实缺少 text：不写空事实")
    try:
        confidence = float(atom.get("confidence", 1.0))
    except (TypeError, ValueError) as error:
        raise MemoryError_(f"原子事实 confidence 不是数字：{atom.get('confidence')!r}") from error
    return {
        "text": text,
        "kind": str(atom.get("kind") or "fact"),
        "confidence": confidence,
        "source_episode": str(atom.get("source_episode") or source_episode),
        "spoiler": bool(atom.get("spoiler", False)),
    }


def is_spoiler(episode: "Episode") -> bool:
    """条目是否属于剧透：episode.spoiler 或 meta.spoiler 任一为真。"""

    return bool(getattr(episode, "spoiler", False) or (episode.meta or {}).get("spoiler"))


def visible_atoms(episode: "Episode", include_spoiler: bool = False) -> List[Dict[str, Any]]:
    """按防剧透闸过滤原子事实；默认把 spoiler=True 的事实挡在注入之外。"""

    atoms = list(getattr(episode, "atoms", []) or [])
    if include_spoiler:
        return atoms
    return [atom for atom in atoms if not atom.get("spoiler")]


@dataclass
class Episode:
    """一条可检索的记忆：情景摘要 + 若干原子事实，归属五层之一。

    layer/atoms/salience/spoiler/group_ids/id 都是新增字段，全部带默认值：
    旧 JSONL（只有 text/kind/group_id/source/confidence/ts/meta）读出来不会抛错，
    缺的字段走默认值（layer=episodic、salience=1.0）。
    """

    text: str
    kind: str = "story"
    group_id: str = ""
    source: str = "slow"  # manual | slow | fast（快系统学习回传）
    confidence: float = 1.0
    ts: float = field(default_factory=lambda: time.time())
    meta: Dict[str, Any] = field(default_factory=dict)
    # ── 五层记忆 / 原子事实 / 显著性（新增） ─────────────────────────
    layer: str = "episodic"
    atoms: List[Dict[str, Any]] = field(default_factory=list)
    salience: float = 1.0
    spoiler: bool = False
    group_ids: List[str] = field(default_factory=list)
    id: str = ""

    def __post_init__(self) -> None:
        self.layer = _coerce_layer(self.layer)
        self.atoms = [
            normalize_atom(atom, source_episode=self.id) for atom in (self.atoms or [])
        ]
        self.salience = _as_float(self.salience, 1.0)
        self.spoiler = bool(self.spoiler)
        self.group_ids = _as_scope_list(self.group_ids)
        self.meta = dict(self.meta or {})

    @property
    def weight(self) -> float:
        """salience 的同义读法（设计文档里也叫 weight）。"""

        return float(self.salience)

    @weight.setter
    def weight(self, value: float) -> None:
        self.salience = _as_float(value, 1.0)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Episode":
        """旧 JSONL 兼容读：只取已知字段，缺的走默认值，group_ids 的 str 升级成 list。"""

        if not isinstance(raw, dict):
            raise MemoryError_(f"记忆行必须是 dict，拿到 {type(raw).__name__}")
        allowed = {item.name for item in fields(cls)}
        kwargs = {key: value for key, value in raw.items() if key in allowed}
        kwargs["text"] = str(raw.get("text") or "")
        kwargs["meta"] = dict(raw.get("meta") or {})
        kwargs["confidence"] = _as_float(raw.get("confidence", 1.0), 1.0)
        kwargs["ts"] = _as_float(raw.get("ts", time.time()), time.time())
        kwargs["salience"] = _as_float(raw.get("salience", 1.0), 1.0)
        kwargs["layer"] = _coerce_layer(raw.get("layer", "episodic"))
        kwargs["spoiler"] = bool(raw.get("spoiler", False))
        kwargs["atoms"] = raw.get("atoms") or []
        # group_ids 单值 → 列表：没有 group_ids 的旧行退回自己的 group_id。
        scopes = raw.get("group_ids")
        if scopes is None:
            scopes = raw.get("group_id", "")
        kwargs["group_ids"] = _as_scope_list(scopes)
        kwargs["id"] = str(raw.get("id") or "")
        return cls(**kwargs)


#: Graphiti 对 group_id 的字符集有硬约束：只允许字母、数字、下划线、连字符。
#: 越界字符（如 steam:730 的冒号、中文游戏名）会让写入失败或产生不可预期的分区名，
#: 因此所有入口统一过一遍 sanitize，而不是指望调用方自觉。
_GROUP_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def sanitize_group_id(raw: str) -> str:
    """把任意游戏标识映射成合规 group_id；纯符号/中文则退化为稳定哈希名。

    steam:730 → steam_730；原神 → g_ 加 sha1 前 10 位（稳定、可复现）。
    """

    text = str(raw or "").strip()
    if not text:
        raise MemoryError_("group_id 为空：记忆读写必须显式指定游戏分区")
    cleaned = "".join(ch if ch in _GROUP_ALLOWED else "_" for ch in text)
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    if not cleaned:
        import hashlib

        cleaned = "g_" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return cleaned


def _require_group(group_id: str, expected: str) -> str:
    """隔离检查：写入/检索的 group_id 必须与 profile 一致。"""
    if not group_id:
        raise MemoryError_("group_id 为空：记忆读写必须显式指定游戏分区")
    if expected and group_id != expected:
        raise MemoryError_(
            f"跨游戏记忆访问被拒绝：请求 {group_id!r}，当前 profile 是 {expected!r}"
        )
    return group_id


class LocalJsonMemory:
    """本地 JSONL 记忆：一行一条 episode，按 group_id 分文件。

    没有 Graphiti/Neo4j 时它就是唯一的长期记忆，所以不能只做"追加+关键词"，
    否则"记忆"会退化成一本越写越乱的流水账。这里补齐四个真实需求：

    1. **去重**：同 kind 同文本只留一条，重复出现提升置信度并累加 supports，
       而不是每次学习都往文件里再抄一遍；
    2. **冲突检测**：同一个键位映射到不同效果时**不静默覆盖**，两条都留着并打
       ``meta["conflict"]`` 标记，由慢系统复核（FR-7 要求），
    3. **相关记忆**：``neighbours()`` 按共享实体给出关联条目，对应图谱的边；
    4. **可信度排序**：检索分数 = 词命中 + 置信度 + 新鲜度，低置信度自然沉底。

    接口与 ``GraphitiMemory`` 保持一致，切换后端只改配置。
    """

    name = "local-json"

    def __init__(self, root: Path, group_id: str, expected_group: str = ""):
        self.root = Path(root)
        self.group_id = _require_group(group_id, expected_group)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / f"{self.group_id}.jsonl"
        self._cache: Optional[List[Episode]] = None
        self._stamp: Optional[tuple] = None
        # 坏行不拖垮检索，但也不能假装没发生：就读到的错误留痕，供调用方查。
        self._load_errors: List[Dict[str, Any]] = []

    # ── 读写 ──────────────────────────────────────────────────────────
    def _file_stamp(self) -> tuple:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return (0, 0)
        return (st.st_mtime_ns, st.st_size)

    def load(self) -> List[Episode]:
        """读出全部条目（带 mtime 缓存：慢系统每步都会检索，不能每次重读磁盘）。"""
        stamp = self._file_stamp()
        if self._cache is not None and stamp == self._stamp:
            return list(self._cache)
        out: List[Episode] = []
        errors: List[Dict[str, Any]] = []
        if self.path.exists():
            for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except Exception as error:  # noqa: BLE001 - 坏行跳过，但要留痕
                    errors.append({"line": number, "error": f"json 解析失败：{error}"})
                    continue
                try:
                    out.append(Episode.from_dict(raw))
                except Exception as error:  # noqa: BLE001 - 迁移期数据问题同样留痕
                    errors.append({"line": number, "error": f"{type(error).__name__}: {error}"})
                    continue
        self._cache = out
        self._stamp = stamp
        self._load_errors = errors
        return list(out)

    def load_errors(self) -> List[Dict[str, Any]]:
        """最近一次读盘跳过的坏行（行号 + 原因），不做静默吞错。"""

        if self._cache is None:
            self.load()
        return list(self._load_errors)

    def _rewrite(self, episodes: Sequence[Episode]) -> None:
        """整份重写（去重/合并时才用）：先写临时文件再替换，避免半截文件。"""
        tmp = self.path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for episode in episodes:
                handle.write(json.dumps(episode.as_dict(), ensure_ascii=False) + "\n")
        tmp.replace(self.path)
        self._cache = list(episodes)
        self._stamp = self._file_stamp()

    @staticmethod
    def _norm(text: Any) -> str:
        return " ".join(str(text or "").lower().split())

    def add_episode(self, episode: Episode) -> Episode:
        """写入一条记忆；重复/相似内容合并且加权，键位冲突打标记但不丢。

        粒度是两层：一条 episode 摘要 + 若干原子事实（atoms），一起落盘。
        """

        _require_group(episode.group_id or self.group_id, self.group_id)
        episode.group_id = self.group_id
        if not episode.id:
            episode.id = uuid.uuid4().hex
        # 多作用域：本游戏分区永远在内，其余由调用方显式给出（如全局 + 标签）。
        episode.group_ids = _as_scope_list([self.group_id] + list(episode.group_ids))
        episode.atoms = [
            normalize_atom(atom, source_episode=episode.id) for atom in episode.atoms
        ]
        if episode.kind not in EPISODE_KINDS:
            raise MemoryError_(f"未知记忆类型 {episode.kind!r}，可用：{EPISODE_KINDS}")

        episodes = self.load()
        norm = self._norm(episode.text)
        for existing in episodes:
            if existing.kind != episode.kind or self._norm(existing.text) != norm:
                continue
            # 同一件事又说了一遍：合并，不新增行。
            self._merge_into(existing, episode)
            self._rewrite(episodes)
            existing.meta = dict(existing.meta, deduped=True)
            return existing

        conflict = self._detect_conflict(episodes, episode)
        if conflict is None:
            similar = self._similar_episode(episodes, episode)
            if similar is not None:
                # 相似度 >0.92：视为同一件事，合并并加权，但不删条目、不新增行。
                existing, ratio = similar
                self._merge_into(existing, episode, similarity=ratio)
                self._rewrite(episodes)
                existing.meta = dict(existing.meta, deduped=True)
                return existing

        if conflict is not None:
            episode.meta = dict(episode.meta, conflict=conflict)
            for other in episodes:
                if other.kind == episode.kind and other.meta.get("key") == conflict["key"]:
                    other.meta = dict(other.meta, conflict=dict(conflict, text=other.text))
            self._rewrite(episodes)

        episodes.append(episode)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(episode.as_dict(), ensure_ascii=False) + "\n")
        self._cache = episodes
        self._stamp = self._file_stamp()
        return episode

    @staticmethod
    def _merge_atoms(
        target: Sequence[Dict[str, Any]],
        incoming: Sequence[Dict[str, Any]],
        *,
        source_episode: str,
    ) -> List[Dict[str, Any]]:
        """原子事实合并：同文只留一条，置信度取高者；不删任何已确认的事实。"""

        merged: List[Dict[str, Any]] = [normalize_atom(atom, source_episode=source_episode) for atom in target]
        index = {atom["text"]: atom for atom in merged}
        for atom in incoming:
            clean = normalize_atom(atom, source_episode=source_episode)
            existed = index.get(clean["text"])
            if existed is None:
                merged.append(clean)
                index[clean["text"]] = clean
                continue
            existed["confidence"] = max(float(existed["confidence"]), float(clean["confidence"]))
            existed["spoiler"] = bool(existed["spoiler"] or clean["spoiler"])
        return merged

    def _merge_into(
        self,
        existing: Episode,
        episode: Episode,
        *,
        similarity: Optional[float] = None,
    ) -> Episode:
        """把新条目并进已有条目：置信度取高、来源取更可信、salience += 0.1。

        条目本身永远保留（不遗忘，只加权）；相似合并把原话留在 meta.merged_texts 里，
        这样"合并"不等于"丢信息"。
        """

        meta = dict(existing.meta)
        meta["supports"] = int(meta.get("supports", 1)) + 1
        meta["last_seen"] = time.time()
        if similarity is not None:
            meta["similar_merged"] = True
            meta["last_similarity"] = round(float(similarity), 3)
            merged = list(meta.get("merged_texts") or [])
            if episode.text and episode.text not in merged and episode.text != existing.text:
                merged.append(episode.text)
            meta["merged_texts"] = merged[-20:]
        existing.meta = meta
        existing.confidence = max(float(existing.confidence), float(episode.confidence))
        existing.ts = max(float(existing.ts), float(episode.ts))
        existing.source = _better_source(existing.source, episode.source)
        existing.salience = float(existing.salience) + 0.1
        existing.group_ids = _as_scope_list(list(existing.group_ids) + list(episode.group_ids))
        existing.atoms = self._merge_atoms(
            existing.atoms, episode.atoms, source_episode=existing.id or episode.id
        )
        return existing

    def _similar_episode(
        self, episodes: Sequence[Episode], episode: Episode
    ) -> Optional[tuple]:
        """找相似度 >0.92 的同 kind 条目；太短的文本不参与（容易误判成一件事）。"""

        norm_new = self._norm(episode.text)
        if len(norm_new) < 4:
            return None
        best: Optional[Episode] = None
        best_ratio = 0.92
        for other in episodes:
            if other.kind != episode.kind:
                continue
            ratio = difflib.SequenceMatcher(None, self._norm(other.text), norm_new).ratio()
            if ratio > best_ratio:
                best, best_ratio = other, ratio
        if best is None:
            return None
        return best, best_ratio

    @staticmethod
    def _detect_conflict(episodes: Sequence[Episode], episode: Episode) -> Optional[Dict[str, Any]]:
        """同一个键位映射到不同效果 → 冲突（不覆盖、不假装一致）。"""
        key = str(episode.meta.get("key") or "").strip()
        effect = str(episode.meta.get("effect") or "").strip()
        if not key or not effect:
            return None
        for other in episodes:
            if other.kind != "keymap" or other.meta.get("key") != key:
                continue
            old_effect = str(other.meta.get("effect") or "").strip()
            if old_effect and old_effect != effect:
                return {"key": key, "effect": effect, "existing_effect": old_effect,
                        "existing_confidence": float(other.confidence)}
        return None

    # ── 检索 ──────────────────────────────────────────────────────────
    def search(
        self,
        query: str = "",
        limit: int = 8,
        kinds: Sequence[str] = (),
        *,
        layers: Optional[Sequence[str]] = None,
        top_k: Optional[int] = None,
        include_spoiler: bool = False,
        group_ids: Optional[Sequence[str] | str] = None,
    ) -> List[Episode]:
        """相关条目检索：词命中 + 置信度 + 新鲜度 + 层权重 + salience。

        - layers 只回指定层（五层：working/episodic/semantic/procedural/social）；
        - top_k 是设计里的名字，给了就覆盖 limit；
        - include_spoiler=False（默认）把剧透条目与 spoiler 原子事实挡在注入之外；
        - group_ids 兼容 str/list，但 local-json 只能读本游戏分区，跨区直接报错。
        """
        terms = [t for t in self._norm(query).split() if t]
        layer_set = {str(item).strip() for item in (layers or []) if str(item).strip()}
        unknown = layer_set - set(MEMORY_LAYERS)
        if unknown:
            raise MemoryError_(f"未知记忆层 {sorted(unknown)}，可用：{MEMORY_LAYERS}")
        if group_ids is not None:
            scopes = _as_scope_list(group_ids)
            if not scopes:
                raise MemoryError_("拒绝检索：group_ids 为空会退化为全库检索（跨游戏串档）")
            others = [scope for scope in scopes if scope != self.group_id]
            if others:
                raise MemoryError_(
                    f"local-json 后端只能检索本游戏分区 {self.group_id!r}，拒绝 {others!r}"
                )
        size = int(top_k) if top_k is not None else int(limit)
        now = time.time()
        scored: List[tuple] = []
        for episode in self.load():
            if kinds and episode.kind not in kinds:
                continue
            if layer_set and episode.layer not in layer_set:
                continue
            if not include_spoiler and is_spoiler(episode):
                continue
            text = self._norm(episode.text)
            hits = sum(1 for term in terms if term in text)
            if terms and not hits:
                continue
            hit_ratio = (hits / len(terms)) if terms else 0.0
            age_days = max(0.0, (now - float(episode.ts)) / 86400.0)
            # 置信度权重最高：低置信度的猜测不该挤掉人工确认过的事实。
            score = 2.0 * hit_ratio + 1.5 * float(episode.confidence) + 1.0 / (1.0 + age_days)
            score *= LAYER_WEIGHTS.get(episode.layer, 1.0)
            # salience 只影响注入排序，绝不决定"删不删"：不遗忘，只加权。
            score += 0.5 * max(0.0, float(episode.salience))
            if episode.meta.get("conflict"):
                score -= 0.4  # 有冲突的条目降权但不隐藏：慢系统要看得见矛盾
            scored.append((score, float(episode.ts), episode))
        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        out: List[Episode] = []
        for _, _, episode in scored[:size]:
            if include_spoiler:
                out.append(episode)
                continue
            atoms = visible_atoms(episode, include_spoiler=False)
            # 不修改缓存里的对象：要过滤 spoiler 原子时返回浅拷贝。
            out.append(episode if len(atoms) == len(episode.atoms) else replace(episode, atoms=atoms))
        return out

    # ── 加权 / 原子事实增删 ───────────────────────────────────────────
    def _find_episode(self, episodes: Sequence[Episode], episode_key: str) -> Episode:
        """按 id 或原文定位一条记忆；找不到必须报错，不能静默当成功。"""

        key = str(episode_key or "").strip()
        if not key:
            raise MemoryError_("episode_key 为空：无法定位要修改的记忆")
        for episode in episodes:
            if episode.id and episode.id == key:
                return episode
        norm = self._norm(key)
        for episode in episodes:
            if self._norm(episode.text) == norm:
                return episode
        raise MemoryError_(f"找不到记忆条目 {episode_key!r}")

    def bump(self, episode_key: str, delta: float = 0.1) -> Episode:
        """给一条记忆加权（salience += delta）；条目永不因检索被删除。"""

        episodes = self.load()
        target = self._find_episode(episodes, episode_key)
        target.salience = max(0.0, float(target.salience) + float(delta))
        self._rewrite(episodes)
        return target

    def add_atom(self, episode_key: str, atom: Dict[str, Any]) -> Dict[str, Any]:
        """给一条记忆追加原子事实；同文合并置信度，不重复落。"""

        episodes = self.load()
        target = self._find_episode(episodes, episode_key)
        clean = normalize_atom(atom, source_episode=target.id or episode_key)
        target.atoms = self._merge_atoms(
            target.atoms, [clean], source_episode=target.id or episode_key
        )
        self._rewrite(episodes)
        return next(atom for atom in target.atoms if atom["text"] == clean["text"])

    def remove_atom(self, episode_key: str, atom_text: str) -> bool:
        """删除一条原子事实；不存在就报错（删除是显式动作，不能假装做到）。"""

        episodes = self.load()
        target = self._find_episode(episodes, episode_key)
        norm = self._norm(atom_text)
        kept = [atom for atom in target.atoms if self._norm(atom.get("text")) != norm]
        if len(kept) == len(target.atoms):
            raise MemoryError_(f"记忆条目 {episode_key!r} 没有原子事实 {atom_text!r}")
        target.atoms = kept
        self._rewrite(episodes)
        return True

    def neighbours(self, episode: Episode, limit: int = 6) -> List[Episode]:
        """共享实体的关联条目（本地版的"图边"）。"""
        ents = self._entities(episode.text)
        if not ents:
            return []
        out: List[tuple] = []
        for other in self.load():
            if other is episode or self._norm(other.text) == self._norm(episode.text):
                continue
            shared = ents & self._entities(other.text)
            if shared:
                out.append((len(shared), other))
        out.sort(key=lambda row: (row[0], float(row[1].confidence)), reverse=True)
        return [row[1] for row in out[:limit]]

    @staticmethod
    def _entities(text: str) -> set:
        """粗粒度实体：键名/英文单词/中文双字片段，够本地关联用。"""
        raw = str(text or "")
        ents = {word.lower() for word in __import__("re").findall(r"[A-Za-z_]{3,}", raw)}
        cjk = __import__("re").findall(r"[\u4e00-\u9fff]{2,}", raw)
        for chunk in cjk:
            ents.update(chunk[i:i + 2] for i in range(len(chunk) - 1))
        return ents

    def conflicts(self) -> List[Dict[str, Any]]:
        """列出所有待复核的冲突（慢系统复核入口）。"""
        out: List[Dict[str, Any]] = []
        seen = set()
        for episode in self.load():
            info = episode.meta.get("conflict")
            if not info:
                continue
            key = (info.get("key"), info.get("effect"), info.get("existing_effect"))
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(info, text=episode.text, source=episode.source))
        return out

    def count(self, kind: Optional[str] = None) -> int:
        return sum(1 for e in self.load() if kind is None or e.kind == kind)

    def close(self) -> None:
        return None

class _LoopRunner:
    """一条常驻的事件循环线程。

    neo4j 异步驱动的连接绑死建立它时的那条 loop；Graphiti 客户端跨 ``asyncio.run`` 复用
    会拿到已关闭的 transport（表现为 AttributeError: 'NoneType' object has no attribute 'send'）。
    所以客户端和它的所有调用都收敛到这一条 loop 上。
    """

    def __init__(self) -> None:
        import asyncio
        import threading

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="graphiti-loop", daemon=True)
        self._thread.start()

    def submit(self, coro: Any) -> Any:
        import asyncio

        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def stop(self) -> None:
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        if not self._loop.is_closed():
            self._loop.close()


class GraphitiMemory:
    """Graphiti 后端：时序知识图谱，每游戏一个 group_id。

    依赖 ``graphiti_core``（pip install graphiti-core）与一个 Neo4j/FalkorDB 实例。
    构造时不联网；第一次 add/search 才建客户端，便于无服务时快速失败。
    """

    name = "graphiti"

    def __init__(
        self,
        group_id: str,
        *,
        uri: str,
        user: str,
        password: str,
        expected_group: str = "",
        llm_client: Any = None,
        embedder: Any = None,
        client: Any = None,
        use_cross_encoder: bool = False,
        llm_factory: Any = None,
        embedder_factory: Any = None,
    ):
        # 统一规范化：调用方给 steam:730 也安全落成 steam_730。
        safe_expected = sanitize_group_id(expected_group) if expected_group else ""
        self.group_id = _require_group(sanitize_group_id(group_id), safe_expected)
        self.use_cross_encoder = bool(use_cross_encoder)
        self.uri = uri
        self.user = user
        self.password = password
        self._llm_client = llm_client
        self._embedder = embedder
        self._client = client
        # 工厂在后台 loop 里执行：openai/httpx 的异步客户端同样绑定事件循环，
        # 在主线程造好再跨 loop 用会报 "bound to a different event loop"。
        self._llm_factory = llm_factory
        self._embedder_factory = embedder_factory

    def _runner(self) -> "_LoopRunner":
        # neo4j 的异步驱动绑定事件循环：client 必须在同一条 loop 上建、也要在同一条 loop 上用。
        # 用专门的后台 loop 线程承载，避免 asyncio.run 每次开新 loop 把连接搞死。
        if getattr(self, "_loop_runner", None) is None:
            self._loop_runner = _LoopRunner()
        return self._loop_runner

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        self._client = self._runner().submit(self._async_build())
        return self._client

    async def _async_build(self) -> Any:
        try:
            from graphiti_core import Graphiti  # type: ignore
        except Exception as error:  # noqa: BLE001
            raise MemoryError_(
                "graphiti-core 未安装：pip install graphiti-core（或把后端切回 local-json）"
            ) from error
        llm = self._llm_client if self._llm_client is not None else (
            self._llm_factory() if self._llm_factory is not None else None
        )
        embedder = self._embedder if self._embedder is not None else (
            self._embedder_factory() if self._embedder_factory is not None else None
        )
        if llm is not None:
            self._llm_client = llm
        if embedder is not None:
            self._embedder = embedder
        kwargs: Dict[str, Any] = {}
        if llm is not None:
            kwargs["llm_client"] = llm
        if embedder is not None:
            kwargs["embedder"] = embedder
        if not self.use_cross_encoder:
            # Graphiti 默认 reranker 是 OpenAIRerankerClient（强依赖 OPENAI_API_KEY，
            # 内部用 logit_bias，DeepSeek 不兼容）。关掉它，走默认 RRF 融合检索。
            noop = _make_noop_cross_encoder()
            if noop is not None:
                kwargs["cross_encoder"] = noop
        client = Graphiti(self.uri, self.user, self.password, **kwargs)
        # 新版本不再隐式建索引：不建就查不到（fulltext index edge_name_and_fact 缺失）。
        builder = getattr(client, "build_indices_and_constraints", None)
        if builder is not None:
            await builder()
        return client

    def add_episode(self, episode: Episode) -> Episode:
        _require_group(episode.group_id or self.group_id, self.group_id)
        episode.group_id = self.group_id
        # 两层粒度一起落：episode 摘要 + 原子事实（Graphiti 没有独立 Fact 节点时，
        # 原子事实以正文附注的形式带上，避免"写了 atoms 却没人能查到"）。
        episode.atoms = [
            normalize_atom(atom, source_episode=episode.id) for atom in episode.atoms
        ]
        body = episode.text
        if episode.atoms:
            body += "\n原子事实：\n" + "\n".join(
                f"- {atom['text']}" for atom in episode.atoms
            )
        client = self._ensure_client()
        # Graphiti 的 add_episode 是异步接口，交给后台 loop 执行，调用方保持同步。
        import asyncio  # noqa: F401 - 兼容旧代码里对 asyncio 的引用

        async def _run() -> None:
            # 0.11+ 的签名要求 reference_time 与 source(EpisodeType)；
            # 时间戳取 episode.ts，保证「先学后忘」的时序语义可追溯。
            from datetime import datetime, timezone  # noqa: PLC0415

            kwargs: Dict[str, Any] = {
                "name": f"{episode.kind}-{int(episode.ts)}",
                "episode_body": body,
                "source_description": f"{episode.source}:{episode.kind}:{episode.layer}",
                "reference_time": datetime.fromtimestamp(episode.ts, tz=timezone.utc),
                "group_id": self.group_id,
            }
            try:
                from graphiti_core.nodes import EpisodeType  # type: ignore

                kwargs["source"] = EpisodeType.text
            except Exception:  # noqa: BLE001 - 旧版本没有 EpisodeType，走默认值
                pass
            await client.add_episode(**kwargs)

        self._runner().submit(_run())
        return episode

    def search(
        self,
        query: str = "",
        limit: int = 8,
        kinds: Sequence[str] = (),
        *,
        layers: Optional[Sequence[str]] = None,
        top_k: Optional[int] = None,
        include_spoiler: bool = False,
        group_ids: Optional[Sequence[str] | str] = None,
    ) -> List[Episode]:
        client = self._ensure_client()
        # 空 group_ids / [""] / None 在 Graphiti 里等于全库检索，会直接跨游戏串档。
        # 多作用域（本游戏 + 全局/标签 + 社交层）用 list 表达；单值 str 也兼容。
        scopes = [self.group_id] if group_ids is None else _as_scope_list(group_ids)
        if not scopes:
            raise MemoryError_("拒绝检索：group_ids 为空会退化为全库检索（跨游戏串档）")
        scopes = [sanitize_group_id(scope) for scope in scopes]
        if not all(scopes):
            raise MemoryError_("拒绝检索：group_ids 含空分区会退化为全库检索（跨游戏串档）")
        layer_set = {str(item).strip() for item in (layers or []) if str(item).strip()}
        unknown = layer_set - set(MEMORY_LAYERS)
        if unknown:
            raise MemoryError_(f"未知记忆层 {sorted(unknown)}，可用：{MEMORY_LAYERS}")
        size = int(top_k) if top_k is not None else int(limit)

        async def _run() -> List[Episode]:
            results = await client.search(
                query=query, group_ids=scopes, num_results=size
            )
            out: List[Episode] = []
            for item in results or []:
                text = getattr(item, "fact", None) or getattr(item, "content", None) or str(item)
                # 图谱检索结果回到本地结构时保留层信息；没有层元数据就是情景记忆。
                layer = getattr(item, "layer", None) or "episodic"
                spoiler = bool(getattr(item, "spoiler", False))
                out.append(
                    Episode(
                        text=str(text),
                        kind="story",
                        group_id=self.group_id,
                        source="graphiti",
                        layer=_coerce_layer(layer),
                        spoiler=spoiler,
                    )
                )
            return out

        hits = self._runner().submit(_run())
        if kinds:
            hits = [hit for hit in hits if hit.kind in kinds]
        if layer_set:
            hits = [hit for hit in hits if hit.layer in layer_set]
        if not include_spoiler:
            hits = [hit for hit in hits if not is_spoiler(hit)]
        return hits

    def bump(self, episode_key: str, delta: float = 0.1) -> Episode:
        """Graphiti 没有本地 salience 列：显式报不支持，不假装加权成功。"""

        raise MemoryError_("Graphiti 后端不支持 bump：salience 加权只在 local-json 落盘")

    def add_atom(self, episode_key: str, atom: Dict[str, Any]) -> Dict[str, Any]:
        """Graphiti 没有独立 Fact 节点写入接口：显式报不支持。"""

        raise MemoryError_("Graphiti 后端不支持 add_atom：原子事实请随 episode 一起写入")

    def remove_atom(self, episode_key: str, atom_text: str) -> bool:
        """Graphiti 没有独立 Fact 节点删除接口：显式报不支持。"""

        raise MemoryError_("Graphiti 后端不支持 remove_atom")

    def close(self) -> None:
        client = self._client
        runner = getattr(self, "_loop_runner", None)
        try:
            if client is not None and runner is not None:
                runner.submit(client.close())
        except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
            pass
        finally:
            if runner is not None:
                runner.stop()
                self._loop_runner = None
                self._client = None


def _make_noop_cross_encoder() -> Any:
    """造一个不做重排的 CrossEncoderClient；拿不到基类就返回 None（交给默认值）。"""

    try:
        from graphiti_core.cross_encoder.client import CrossEncoderClient  # type: ignore
    except Exception:  # noqa: BLE001 - 版本差异/未安装都不应炸掉构造
        return None

    class _NoopCrossEncoder(CrossEncoderClient):  # type: ignore[misc]
        async def rank(self, query: str, passages: List[str]) -> List[Any]:
            return [(p, 0.0) for p in passages]

    return _NoopCrossEncoder()


def build_memory(
    profile: Any,
    *,
    backend: Optional[str] = None,
    root: Optional[Path] = None,
    **kwargs: Any,
) -> Any:
    """按 profile 配置造一个记忆后端；默认 local-json（Graphiti 要显式开启）。"""
    config = profile.load_config() if hasattr(profile, "load_config") else {}
    memory_cfg = dict(config.get("memory") or {})
    chosen = backend or memory_cfg.get("backend") or "local-json"
    if chosen == "graphiti":
        uri = memory_cfg.get("uri") or os.environ.get("GRAPHITI_URI") or "bolt://localhost:7687"
        user = memory_cfg.get("user") or os.environ.get("GRAPHITI_USER") or "neo4j"
        password = memory_cfg.get("password") or os.environ.get("GRAPHITI_PASSWORD") or ""
        if not password:
            raise MemoryError_("Graphiti 后端缺少密码：设置 GRAPHITI_PASSWORD 或 profile.memory.password")
        # 没显式给客户端就按本机装配（DeepSeek 对话 + 本地 fastembed），
        # 避免 Graphiti 默认去要 OPENAI_API_KEY。
        if kwargs.get("llm_client") is None or kwargs.get("embedder") is None:
            try:
                import graphiti_wiring  # type: ignore

                # 只把工厂传进去，真正实例化发生在后台 loop 里（见 GraphitiMemory._async_build）。
                kwargs.setdefault("llm_factory", lambda: graphiti_wiring.build_llm_client())
                kwargs.setdefault("embedder_factory", lambda: graphiti_wiring.build_embedder())
            except Exception as error:  # noqa: BLE001 - 装配失败要明确报出来，不静默降级
                raise MemoryError_("Graphiti 客户端装配失败：%s" % error) from error
        return GraphitiMemory(
            profile.group_id,
            uri=uri,
            user=user,
            password=password,
            expected_group=profile.group_id,
            **kwargs,
        )
    base = Path(root) if root else (Path(memory_cfg.get("root")) if memory_cfg.get("root") else profile.dir / "memory")
    return LocalJsonMemory(base, profile.group_id, expected_group=profile.group_id)