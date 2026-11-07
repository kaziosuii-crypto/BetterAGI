# -*- coding: utf-8 -*-
"""离线验收器（agent/verify.py）的单元测试。

全部用 tmp_path 造运行目录，不联网、不启动模型：
* 一个所有文件都合规的基线目录，断言整体 ok=True；
* 每种破坏只改一处，确认对应断言能抓住，且 detail/evidence 点得出问题。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import verify


GOOD_CONCLUSION = "拿到了钥匙并打开了塔楼的门"
GOOD_OBSERVATIONS = (
    "## 结论\n这一轮已经完成目标，钥匙到手，塔楼的门开了。\n\n"
    "## 看到了什么\n画面右下角出现了钥匙图标，对话框里 NPC 说晚上八点在塔楼见；"
    "背景音乐从紧张变成舒缓，道具栏第二格多了一把铜钥匙，地上没有掉落物，"
    "血条还是满的，弹药数量没有变化，准星附近没有敌人。\n\n"
    "## 我现在在哪\n还在教堂门口，人物可以自由移动，面前是通往塔楼的石阶，"
    "存档点就在右手边，回头能看到来时的那条小路。"
)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _timeline(root: Path) -> List[Dict[str, Any]]:
    path = root / "timeline.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _write_timeline(root: Path, events: List[Dict[str, Any]]) -> None:
    (root / "timeline.jsonl").write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )


def _append_timeline(root: Path, *events: Dict[str, Any]) -> None:
    rows = _timeline(root)
    rows.extend(events)
    _write_timeline(root, rows)


def _handoff(intent_id: str, conclusion: str = GOOD_CONCLUSION,
             observations: str = GOOD_OBSERVATIONS) -> Dict[str, Any]:
    return {
        "intent_id": intent_id,
        "result": "reached",
        "conclusion": conclusion,
        "observations": observations,
        "session_id": "sess-1",
        "media": {},
        "timing": {},
        "stats": {},
        "last_frame": "",
        "ts_ms": 1700000000000,
        "schema_version": "1.0",
        "warnings": [],
    }


def _make_run(tmp_path: Path) -> Path:
    """一个 A1-A9 都能通过（或明确 skipped）的最小运行目录。"""

    root = tmp_path / "run"
    root.mkdir()

    intents = [
        {"intent_id": "sess-1-001", "goal_short": "拿钥匙",
         "started_ms": 0, "ended_ms": 200, "result": "reached"},
        {"intent_id": "sess-1-002", "goal_short": "去塔楼",
         "started_ms": 220, "ended_ms": 400, "result": "reached"},
    ]
    _write_json(root / "session_index.json", {
        "schema_version": "1.0",
        "session_id": "sess-1",
        "game": "doom",
        "save_id": "save-1",
        "t0_epoch_ms": 1700000000000,
        "segment_ms": 600000,
        "intents": intents,
        "segments": [{"file": "seg_000.mp4", "start_ms": 0, "end_ms": 500,
                      "frames": 2, "frame_no": 0, "orphan": False}],
    })

    events = [
        {"v": 1, "t_ms": 0, "seq": 1, "kind": "segment_header",
         "segment": "seg_000.mp4", "frame_no": 0},
        {"v": 1, "t_ms": 0, "seq": 2, "kind": "intent_start",
         "intent_id": "sess-1-001", "goal": "拿钥匙"},
        {"v": 1, "t_ms": 100, "seq": 3, "kind": "game_action",
         "intent_id": "sess-1-001", "steps": [["W"], ["W"]], "decide_ms": 42},
        {"v": 1, "t_ms": 200, "seq": 4, "kind": "intent_end",
         "intent_id": "sess-1-001", "result": "reached", "intent_ms": 200},
        {"v": 1, "t_ms": 210, "seq": 5, "kind": "handoff",
         "intent_id": "sess-1-001", "result": "reached",
         "conclusion": GOOD_CONCLUSION},
        {"v": 1, "t_ms": 220, "seq": 6, "kind": "intent_start",
         "intent_id": "sess-1-002", "goal": "去塔楼"},
        {"v": 1, "t_ms": 300, "seq": 7, "kind": "game_action",
         "intent_id": "sess-1-002", "steps": [["A"]], "decide_ms": 30},
        {"v": 1, "t_ms": 400, "seq": 8, "kind": "intent_end",
         "intent_id": "sess-1-002", "result": "reached", "intent_ms": 180},
        {"v": 1, "t_ms": 410, "seq": 9, "kind": "handoff",
         "intent_id": "sess-1-002", "result": "reached",
         "conclusion": GOOD_CONCLUSION},
    ]
    _write_timeline(root, events)

    _write_json(root / "handoffs" / "sess-1-001.json", _handoff("sess-1-001"))
    _write_json(root / "handoffs" / "sess-1-002.json", _handoff("sess-1-002"))

    segments = root / "segments"
    segments.mkdir()
    (segments / "seg_000.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    (segments / "segments.csv").write_text(
        "seg_000.mp4,0.000000,0.500000\n", encoding="utf-8")
    return root


def _check(result: Dict[str, Any], letter: str) -> Dict[str, Any]:
    for item in result["checks"]:
        if item["name"].startswith(letter + " "):
            return item
    raise AssertionError("没有找到断言 %s：%r" % (letter, result["checks"]))


# ---------------------------------------------------------------- 基线
def test_clean_run_passes(tmp_path: Path) -> None:
    result = verify.verify_run(_make_run(tmp_path))

    assert result["ok"] is True, verify.format_report(result)
    assert result["counts"]["failed"] == 0
    assert [item["name"][:2] for item in result["checks"]] == [
        "A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9"]
    assert result["counts"]["passed"] == 4
    assert result["counts"]["skipped"] == 5
    assert _check(result, "A4")["ok"] is True
    text = verify.format_report(result)
    assert "通过" in text and "A4" in text


# ---------------------------------------------------------------- A3
def test_timeline_regression_and_duplicate_seq_caught(tmp_path: Path) -> None:
    root = _make_run(tmp_path)
    _append_timeline(
        root,
        {"v": 1, "t_ms": 5, "seq": 10, "kind": "freeze",
         "reason": "slow_think", "ms": 0, "restored": False},
        {"v": 1, "t_ms": 6, "seq": 10, "kind": "freeze",
         "reason": "slow_think", "ms": 0, "restored": False},
    )

    result = verify.verify_run(root)
    check = _check(result, "A3")

    assert result["ok"] is False
    assert check["ok"] is False
    assert "单调性违规" in check["detail"]
    assert any("倒退" in item for item in check["evidence"])
    assert any("seq=10" in item and "没有严格大于" in item
               for item in check["evidence"])
    assert any("t_ms=5" in item for item in check["evidence"])


# ---------------------------------------------------------------- A4
def test_missing_handoff_caught(tmp_path: Path) -> None:
    root = _make_run(tmp_path)
    (root / "handoffs" / "sess-1-002.json").unlink()

    result = verify.verify_run(root)
    check = _check(result, "A4")

    assert check["ok"] is False
    assert "sess-1-002" in check["detail"] or any(
        "sess-1-002" in item for item in check["evidence"])
    assert any("缺交棒报告" in item for item in check["evidence"])


def test_handoff_lengths_caught(tmp_path: Path) -> None:
    root = _make_run(tmp_path)
    _write_json(root / "handoffs" / "sess-1-001.json",
                _handoff("sess-1-001", conclusion="太短", observations="太短"))

    result = verify.verify_run(root)
    check = _check(result, "A4")

    assert check["ok"] is False
    assert any("conclusion 实测 2 字" in item for item in check["evidence"])
    assert any("observations 实测 2 字" in item for item in check["evidence"])


# ---------------------------------------------------------------- A2
def test_game_action_outside_intent_caught(tmp_path: Path) -> None:
    root = _make_run(tmp_path)
    _append_timeline(root, {
        "v": 1, "t_ms": 500, "seq": 10, "kind": "game_action",
        "intent_id": "sess-1-002", "steps": [["B"]], "decide_ms": 10,
    })

    result = verify.verify_run(root)
    check = _check(result, "A2")

    assert check["ok"] is False
    assert "不在意图" in check["detail"] or any(
        "不在意图" in item for item in check["evidence"])


# ---------------------------------------------------------------- A7 / A8
def test_action_after_incident_caught(tmp_path: Path) -> None:
    root = _make_run(tmp_path)
    incident_dir = root / "incidents" / "20261104-210000_sess-1-002"
    incident_dir.mkdir(parents=True)
    (incident_dir / "README.md").write_text("事故留证", encoding="utf-8")
    _append_timeline(
        root,
        {"v": 1, "t_ms": 420, "seq": 10, "kind": "incident",
         "dir": "incidents/20261104-210000_sess-1-002",
         "incident_kind": "motor_parse_error", "reason": "解析失败"},
        {"v": 1, "t_ms": 430, "seq": 11, "kind": "game_action",
         "intent_id": "sess-1-002", "steps": [["B"]], "decide_ms": 10},
    )

    result = verify.verify_run(root)
    assert _check(result, "A7")["ok"] is True, _check(result, "A7")
    check = _check(result, "A8")
    assert check["ok"] is False
    assert "incident 之后" in check["detail"]


def test_incident_missing_readme_caught(tmp_path: Path) -> None:
    root = _make_run(tmp_path)
    incident_dir = root / "incidents" / "20261104-210100_sess-1-002"
    incident_dir.mkdir(parents=True)
    _append_timeline(root, {
        "v": 1, "t_ms": 420, "seq": 10, "kind": "incident",
        "dir": "incidents/20261104-210100_sess-1-002",
        "incident_kind": "motor_parse_error", "reason": "解析失败",
    })

    result = verify.verify_run(root)
    check = _check(result, "A7")

    assert check["ok"] is False
    assert "README.md" in check["detail"] or any(
        "README.md" in item for item in check["evidence"])


# ---------------------------------------------------------------- A5
def test_segments_csv_missing_file_caught(tmp_path: Path) -> None:
    root = _make_run(tmp_path)
    (root / "segments" / "segments.csv").write_text(
        "seg_999.mp4,0.000000,0.500000\n", encoding="utf-8")

    result = verify.verify_run(root)
    check = _check(result, "A5")

    assert check["ok"] is False
    assert any("seg_999.mp4" in item for item in check["evidence"])
    assert any("不存在" in item for item in check["evidence"])


def test_segment_start_far_from_timeline_caught(tmp_path: Path) -> None:
    root = _make_run(tmp_path)
    (root / "segments" / "segments.csv").write_text(
        "seg_000.mp4,5.000000,5.500000\n", encoding="utf-8")

    result = verify.verify_run(root)
    check = _check(result, "A5")

    assert check["ok"] is False
    assert any("±1000ms" in item for item in check["evidence"])


# ---------------------------------------------------------------- A6 / A9
def test_sticky_block_overflow_caught(tmp_path: Path) -> None:
    root = _make_run(tmp_path)
    sticky = root / "sticky" / "doom" / "save-1.md"
    sticky.parent.mkdir(parents=True)
    sticky.write_text(
        "<!-- sticky v1 | game: doom | save: save-1 | updated_at: x | by: slow -->\n\n"
        "## key_binding\n" + ("按 W 前进。" * 200) + "\n\n"
        "## game_rules\n" + ("别碰红桶。" * 10) + "\n",
        encoding="utf-8",
    )

    result = verify.verify_run(root)
    check = _check(result, "A6")

    assert check["ok"] is False
    assert any("超过单块上限" in item for item in check["evidence"])


def test_sticky_body_leaking_into_fast_prompt_caught(tmp_path: Path) -> None:
    root = _make_run(tmp_path)
    sticky = root / "sticky" / "doom" / "save-1.md"
    sticky.parent.mkdir(parents=True)
    leak_line = "按 X 键可以翻滚躲避攻击"
    sticky.write_text(
        "<!-- sticky v1 | game: doom | save: save-1 | updated_at: x | by: slow -->\n\n"
        "## key_binding\n" + leak_line + "\n\n"
        "## game_rules\n红桶不要碰。\n",
        encoding="utf-8",
    )
    _write_json(root / "packets" / "intent_001.json", {
        "intent_id": "sess-1-001",
        "system": "你是快系统，负责按手柄。\n" + leak_line,
        "messages": [{"role": "user", "content": "开始"}],
    })

    result = verify.verify_run(root)
    check = _check(result, "A9")

    assert check["ok"] is False
    assert any(leak_line in item for item in check["evidence"])
    assert any("intent_001.json" in item for item in check["evidence"])


# ---------------------------------------------------------------- A1
def test_concurrent_sessions_caught(tmp_path: Path) -> None:
    root = _make_run(tmp_path)
    _append_timeline(
        root,
        {"v": 1, "t_ms": 500, "seq": 10, "kind": "session_start",
         "session_id": "omni-a"},
        {"v": 1, "t_ms": 501, "seq": 11, "kind": "session_start",
         "session_id": "omni-b"},
    )

    result = verify.verify_run(root)
    check = _check(result, "A1")

    assert check["ok"] is False
    assert "会话并发" in check["detail"]
    assert any("同时活跃" in item for item in check["evidence"])
