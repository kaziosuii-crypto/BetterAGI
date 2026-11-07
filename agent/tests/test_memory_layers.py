"""五层记忆验收：层过滤、原子事实、多作用域、防剧透、加权不遗忘。

这些用例守的是"记忆真的被读到、且只投喂已获得的信息"这条线：
- 旧 JSONL 必须能读（字段缺省不抛），否则升级即失忆；
- 检索能按 layer 过滤，程序性记忆才不会被流水账淹没；
- 原子事实可增删读回，spoiler 默认被挡；
- 相似条目只合并加权、不删条目；
- memory_wire 的召回路径真的把 query/layers/防剧透闸传给后端。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import memory as M  # noqa: E402
from memory_wire import (  # noqa: E402
    episode_from_tool,
    format_recall,
    recall_for,
)


def _mem(tmp_path: Path, group: str = "undertale") -> M.LocalJsonMemory:
    return M.LocalJsonMemory(tmp_path / "mem", group)


def _write_rows(tmp_path: Path, group: str, rows: list) -> M.LocalJsonMemory:
    """直接落一份旧格式 JSONL（模拟升级前的记忆文件）。"""

    root = tmp_path / "mem"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{group}.jsonl"
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return M.LocalJsonMemory(root, group)


def test_legacy_jsonl_gets_defaults(tmp_path: Path) -> None:
    """旧行没有 layer/atoms/salience/group_ids：读出默认值，不许抛。"""

    mem = _write_rows(
        tmp_path,
        "undertale",
        [
            {
                "text": "按 [confirm] 进入命名界面",
                "kind": "lesson",
                "group_id": "undertale",
                "source": "slow",
                "confidence": "0.7",
                "ts": 1700000000.0,
                "meta": {},
            }
        ],
    )
    loaded = mem.load()
    assert len(loaded) == 1
    episode = loaded[0]
    assert episode.layer == "episodic"
    assert episode.atoms == []
    assert episode.salience == pytest.approx(1.0)
    assert episode.weight == pytest.approx(1.0)
    assert episode.spoiler is False
    assert episode.group_ids == ["undertale"]
    assert episode.confidence == pytest.approx(0.7)


def test_legacy_group_ids_string_is_migrated(tmp_path: Path) -> None:
    """group_ids 单值 str（以及完全缺失）都要归一成 list。"""

    mem = _write_rows(
        tmp_path,
        "undertale",
        [
            {"text": "旧作用域", "kind": "rule", "group_ids": "steam_730"},
            {"text": "没有作用域", "kind": "rule", "group_id": "undertale"},
        ],
    )
    by_text = {episode.text: episode for episode in mem.load()}
    assert by_text["旧作用域"].group_ids == ["steam_730"]
    assert by_text["没有作用域"].group_ids == ["undertale"]


def test_add_episode_writes_group_ids_list(tmp_path: Path) -> None:
    """写入统一 list：本游戏分区在前，额外作用域（全局/标签）去重保留。"""

    mem = _mem(tmp_path)
    mem.add_episode(
        M.Episode(text="跨区可见的教训", kind="lesson", group_ids=["global", "undertale"])
    )
    stored = mem.load()[0]
    assert stored.group_ids == ["undertale", "global"]
    assert isinstance(stored.group_ids, list)


def test_layer_filter_only_returns_requested_layer(tmp_path: Path) -> None:
    mem = _mem(tmp_path)
    mem.add_episode(M.Episode(text="正在打 Boss", kind="story", layer="working"))
    mem.add_episode(M.Episode(text="按 Z 确认菜单", kind="keymap", layer="procedural"))
    mem.add_episode(M.Episode(text="Toriel 是好人", kind="story", layer="social"))

    hits = mem.search("", layers=["procedural"], top_k=8)
    assert hits
    assert {hit.layer for hit in hits} == {"procedural"}
    assert any("按 Z 确认菜单" == hit.text for hit in hits)
    # 多选层也支持
    both = mem.search("", layers=["procedural", "social"])
    assert {hit.layer for hit in both} == {"procedural", "social"}


def test_unknown_layer_is_rejected(tmp_path: Path) -> None:
    mem = _mem(tmp_path)
    mem.add_episode(M.Episode(text="一条记忆", kind="story"))
    with pytest.raises(M.MemoryError_):
        mem.search("", layers=["dream"])


def test_atoms_add_read_merge_and_remove(tmp_path: Path) -> None:
    mem = _mem(tmp_path)
    episode = mem.add_episode(
        M.Episode(
            text="遗迹里的开关",
            kind="rule",
            layer="semantic",
            atoms=[
                {"text": "开关在房间左上角", "kind": "fact", "confidence": 0.6},
                {"text": "拉下开关会开门", "kind": "fact", "confidence": 0.7},
            ],
        )
    )
    assert len(mem.load()[0].atoms) == 2

    # 同文原子合并置信度，不重复落
    merged = mem.add_atom(episode.id, {"text": "拉下开关会开门", "confidence": 0.95})
    assert merged["confidence"] == pytest.approx(0.95)
    assert len(mem.load()[0].atoms) == 2

    added = mem.add_atom(episode.id, {"text": "房间里有两只怪", "kind": "fact"})
    assert added["source_episode"] == episode.id
    assert len(mem.load()[0].atoms) == 3

    assert mem.remove_atom(episode.id, "房间里有两只怪") is True
    assert len(mem.load()[0].atoms) == 2
    # 重新从磁盘读，确认删除真的落盘
    assert len(_mem(tmp_path).load()[0].atoms) == 2
    with pytest.raises(M.MemoryError_):
        mem.remove_atom(episode.id, "不存在的事实")


def test_spoiler_episode_hidden_by_default(tmp_path: Path) -> None:
    mem = _mem(tmp_path)
    mem.add_episode(M.Episode(text="结局是和平线", kind="story", spoiler=True))
    mem.add_episode(M.Episode(text="标题画面按确认键", kind="lesson"))

    texts = [hit.text for hit in mem.search("", top_k=8)]
    assert "结局是和平线" not in texts
    assert "标题画面按确认键" in texts

    with_spoiler = [hit.text for hit in mem.search("", top_k=8, include_spoiler=True)]
    assert "结局是和平线" in with_spoiler


def test_spoiler_atom_filtered_without_polluting_cache(tmp_path: Path) -> None:
    """spoiler 原子在默认检索里被挡；且过滤不能改坏缓存里的原对象。"""

    mem = _mem(tmp_path)
    episode = mem.add_episode(
        M.Episode(
            text="Boss 战笔记",
            kind="lesson",
            atoms=[
                {"text": "Boss 弱点是火焰", "kind": "fact", "spoiler": True},
                {"text": "Boss 会召唤小怪", "kind": "fact"},
            ],
        )
    )
    hits = mem.search("Boss", top_k=8)
    assert len(hits) == 1
    assert [atom["text"] for atom in hits[0].atoms] == ["Boss 会召唤小怪"]

    fresh = mem.load()[0]
    assert {atom["text"] for atom in fresh.atoms} == {"Boss 弱点是火焰", "Boss 会召唤小怪"}

    full = mem.search("Boss", top_k=8, include_spoiler=True)[0]
    assert {atom["text"] for atom in full.atoms} == {"Boss 弱点是火焰", "Boss 会召唤小怪"}
    # 返回的仍是同一条记忆（id 不变），不是伪造的新条目
    assert full.id == episode.id


def test_similar_merge_keeps_single_row_but_raises_salience(tmp_path: Path) -> None:
    """相似度 >0.92 只合并加权：条目数不变少、salience 变大、原话留在 meta。"""

    mem = _mem(tmp_path)
    first = mem.add_episode(
        M.Episode(text="在遗迹入口遇到 Toriel 并按下确认键继续前进", kind="story")
    )
    second = mem.add_episode(
        M.Episode(text="在遗迹入口遇到 Toriel 并按下确认键继续前进了", kind="story")
    )
    assert mem.count() == 1
    assert second.id == first.id
    stored = mem.load()[0]
    assert stored.meta["similar_merged"] is True
    assert stored.salience == pytest.approx(1.1)
    assert stored.meta["supports"] == 2
    # 被合并的原话留在 meta 里："合并"不等于"丢信息"
    assert "在遗迹入口遇到 Toriel 并按下确认键继续前进了" in stored.meta["merged_texts"]

    later = mem.bump(first.id, 0.3)
    assert later.salience == pytest.approx(1.4)
    assert mem.count() == 1
    # 加权已落盘
    assert _mem(tmp_path).load()[0].salience == pytest.approx(1.4)


def test_bump_unknown_episode_raises(tmp_path: Path) -> None:
    mem = _mem(tmp_path)
    with pytest.raises(M.MemoryError_):
        mem.bump("不存在", 0.1)


def test_recall_uses_layers_and_context_query(tmp_path: Path) -> None:
    """memory_wire 注入路径真的按 layer 检索，不是"只写不读"。"""

    mem = _mem(tmp_path)
    mem.add_episode(
        M.Episode(
            text="按 [confirm] 可以打开命名界面",
            kind="lesson",
            layer="episodic",
        )
    )
    mem.add_episode(
        M.Episode(
            text="确认键在标题画面是开始游戏",
            kind="keymap",
            layer="procedural",
        )
    )
    hits = recall_for(mem, goal="确认键", layers=["procedural"], top_k=3)
    assert hits
    assert all(hit.layer == "procedural" for hit in hits)
    # 格式化输出会标出层名，便于慢系统区分"我会的操作"与见闻
    assert "[procedural/keymap]" in format_recall(hits)


def test_recall_respects_spoiler_gate(tmp_path: Path) -> None:
    mem = _mem(tmp_path)
    mem.add_episode(M.Episode(text="真结局需要收集全部灵魂", kind="story", spoiler=True))
    assert recall_for(mem, goal="真结局", top_k=3) == []
    assert recall_for(mem, goal="真结局", top_k=3, include_spoiler=True)


def test_episode_from_tool_tags_layer_and_atom() -> None:
    """学键位写程序性记忆 + mechanic 原子事实；失败不落"事实"。"""

    episode = episode_from_tool(
        "learn_key",
        {"name": "confirm", "physical": "z"},
        "ok",
        True,
        group_id="undertale",
        confidence=0.9,
    )
    assert episode is not None
    assert episode.layer == "procedural"
    assert episode.kind == "keymap"
    assert episode.atoms and episode.atoms[0]["kind"] == "mechanic"
    assert episode.atoms[0]["text"] == "按键 confirm 对应 z"
    assert episode.atoms[0]["source_episode"] == ""

    failed = episode_from_tool(
        "learn_key", {"name": "confirm", "physical": "z"}, "no", False,
        group_id="undertale",
    )
    assert failed is not None and failed.atoms == []

    action = episode_from_tool("act", {"keys": ["confirm"]}, "ok", True, group_id="undertale")
    assert action is not None and action.layer == "episodic"


def test_load_errors_are_reported_not_swallowed(tmp_path: Path) -> None:
    """坏行不能拖垮整份记忆，但也不能静默：必须能在 load_errors 里看到。"""

    root = tmp_path / "mem"
    root.mkdir()
    (root / "undertale.jsonl").write_text(
        '{"text": "好行", "kind": "rule"}\n{坏行}\n', encoding="utf-8"
    )
    mem = M.LocalJsonMemory(root, "undertale")
    assert len(mem.load()) == 1
    errors = mem.load_errors()
    assert len(errors) == 1
    assert errors[0]["line"] == 2


class _FakeGraphiti:
    def __init__(self) -> None:
        self.calls = []
        self.results = []

    async def add_episode(self, **kwargs):
        self.calls.append(("add", kwargs))

    async def search(self, **kwargs):
        self.calls.append(("search", kwargs))
        return self.results

    async def close(self):
        pass


def test_graphiti_search_accepts_multi_scope_list(tmp_path: Path) -> None:
    """Graphiti 的 group_ids 是列表：本游戏 + 跨游戏社交层可以一起查。"""

    client = _FakeGraphiti()
    mem = M.GraphitiMemory(
        "steam:730", uri="bolt://x", user="neo4j", password="p", client=client
    )
    mem.search("宝箱", group_ids=["steam:730", "social:global"], top_k=5)
    _, kwargs = client.calls[-1]
    assert kwargs["group_ids"] == ["steam_730", "social_global"]
    assert kwargs["num_results"] == 5

    mem.search("宝箱", group_ids="steam:730")
    _, kwargs = client.calls[-1]
    assert kwargs["group_ids"] == ["steam_730"]


def test_graphiti_add_episode_carries_layer_and_atoms() -> None:
    client = _FakeGraphiti()
    mem = M.GraphitiMemory(
        "steam:730", uri="bolt://x", user="neo4j", password="p", client=client
    )
    mem.add_episode(
        M.Episode(
            text="学会按键",
            kind="keymap",
            layer="procedural",
            atoms=[{"text": "按键 confirm 对应 z", "kind": "mechanic"}],
        )
    )
    _, kwargs = client.calls[-1]
    assert kwargs["source_description"].endswith(":procedural")
    assert "原子事实" in kwargs["episode_body"]
    assert "按键 confirm 对应 z" in kwargs["episode_body"]


def test_graphiti_unsupported_weighting_raises() -> None:
    """Graphiti 后端没有 salience 列：显式报不支持，不能假装成功。"""

    mem = M.GraphitiMemory(
        "steam:730", uri="bolt://x", user="neo4j", password="p", client=_FakeGraphiti()
    )
    for call in (
        lambda: mem.bump("x"),
        lambda: mem.add_atom("x", {"text": "t"}),
        lambda: mem.remove_atom("x", "t"),
    ):
        with pytest.raises(M.MemoryError_):
            call()
