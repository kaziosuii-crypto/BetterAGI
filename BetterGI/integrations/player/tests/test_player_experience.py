#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""玩家经历的来源、可见性、SQLite 原子性与跨会话测试。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from player_event_log import EventLogConflict, PlayerEventLog
from player_experience import (
    BELIEF, CANONICAL_WORLD_TRUTH, EXPERIENCED_EVENT, EXPERIENCED_FACT,
    HYPOTHESIS, PROJECTION, RAW_OBSERVATION, SUBJECTIVE_INTERPRETATION,
    ExperienceError, PlayerExperience,
)


class ExperienceTests(unittest.TestCase):
    """使用临时真实 SQLite 文件，不依赖网络或游戏剧情。"""

    def setUp(self):
        """每个测试使用独立数据库。"""
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "events.sqlite"
        self.log = PlayerEventLog(self.path)
        self.memory = PlayerExperience(self.log)

    def tearDown(self):
        """关闭 WAL 连接并清理文件。"""
        self.log.close()
        self.directory.cleanup()

    def observe(self, eid, text, *, session="s1", channel="visual", visibility="player", **extra):
        """记录一条测试输入；文本是合成样例，不表示真实剧情。"""
        return self.memory.record_observation(
            session_id=session, event_id=eid, timestamp="2026-01-01T00:00:00Z",
            source_id="source-" + eid, channel=channel, text=text,
            visibility=visibility, **extra)

    def claim(self, eid, kind, text, evidence, *, session="s1", uncertainty=None):
        """使用显式引用记录测试经历。"""
        return self.memory.record_claim(
            session_id=session, event_id=eid, timestamp="2026-01-01T00:00:01Z",
            kind=kind, text=text, evidence=evidence, uncertainty=uncertainty)

    def test_provenance_and_uncertainty(self):
        """确定类只复制观察原文，推断和信念必须标注不确定性。"""
        self.observe("o1", "画面显示一扇门")
        ref = [{"session_id": "s1", "event_id": "o1"}]
        for index, kind in enumerate((EXPERIENCED_EVENT, EXPERIENCED_FACT)):
            self.claim("fact" + str(index), kind, "画面显示一扇门", ref)
        for index, kind in enumerate((SUBJECTIVE_INTERPRETATION, BELIEF, HYPOTHESIS)):
            with self.assertRaises(ExperienceError):
                self.claim("bad" + str(index), kind, "门可能能打开", ref)
            self.claim("guess" + str(index), kind, "门可能能打开", ref, uncertainty="尚未验证")
        result = self.memory.recall(["s1"])
        self.assertEqual(result[0]["kind"], RAW_OBSERVATION)
        self.assertEqual(result[-1]["evidence"], [{"session_id": "s1", "event_id": "o1", "source_id": "source-o1"}])
        self.assertEqual(result[-1]["uncertainty"], "尚未验证")
        result[0]["text"] = "外部修改"
        self.assertEqual(self.memory.recall(["s1"])[0]["text"], "画面显示一扇门")
        with self.assertRaises(ExperienceError):
            self.claim("invented", EXPERIENCED_FACT, "门已打开", ref)
        with self.assertRaises(ExperienceError):
            self.claim("no-source", EXPERIENCED_EVENT, "画面显示一扇门", [])
        with self.assertRaises(ExperienceError):
            self.claim("wrong-source", BELIEF, "猜测", [{"session_id": "s2", "event_id": "o1"}], uncertainty="未知")
        with self.assertRaises(ExperienceError):
            self.claim("derived-source", EXPERIENCED_FACT, "画面显示一扇门", [{"session_id": "s1", "event_id": "fact0"}])

    def test_derived_experience_has_provenance_and_is_unverified(self):
        self.observe("o1", "安柏说跟我来", channel="dialogue")
        self.observe("o2", "任务目标发生变化", channel="visual")
        refs = [{"session_id": "s1", "event_id": "o1"},
                {"session_id": "s1", "event_id": "o2"}]
        self.memory.record_claim(session_id="s1", event_id="derived", timestamp="t",
                                 kind=EXPERIENCED_EVENT, text="安柏引导了任务",
                                 evidence=refs, derivation="结合对话和目标变化", confidence=0.6)
        entry = self.memory.recall(["s1"])[-1]
        self.assertEqual(entry["verification"], "derived_unverified")
        self.assertEqual(len(entry["evidence"]), 2)
        self.assertEqual(entry["confidence"], 0.6)
        self.observe("attempt-only", "请求前进", channel="action_attempt")
        with self.assertRaises(ExperienceError):
            self.memory.record_claim(session_id="s1", event_id="false", timestamp="t",
                                     kind=EXPERIENCED_FACT, text="已到达目的地",
                                     evidence=[{"session_id": "s1", "event_id": "attempt-only"}],
                                     derivation="尝试前进", confidence=0.9)

    def test_attempt_is_not_success_and_feedback_is_evidence(self):
        """动作请求即使声称成功也不能升级为确定经历；真实反馈可以。"""
        self.observe("attempt", "打开成功", channel="action_attempt")
        ref = [{"session_id": "s1", "event_id": "attempt"}]
        with self.assertRaises(ExperienceError):
            self.claim("false-success", EXPERIENCED_EVENT, "打开成功", ref)
        self.claim("uncertain", HYPOTHESIS, "可能打开了", ref, uncertainty="只有动作请求")
        self.observe("feedback", "打开成功", channel="feedback")
        self.claim("confirmed-feedback", EXPERIENCED_EVENT, "打开成功",
                   [{"session_id": "s1", "event_id": "feedback"}])
        self.assertNotIn("false-success", [x["event_id"] for x in self.memory.recall(["s1"])])

    def test_visibility_prevents_spoilers_and_evidence_laundering(self):
        """隐藏观察、世界真相不可召回，也不能借引用变成可见经历。"""
        self.observe("hidden", "隐含信息", visibility="hidden")
        self.memory.record_world_truth(session_id="s1", event_id="truth", timestamp="t",
                                       source_id="private-source", text="未见到的世界真相")
        self.observe("visible", "可见文字")
        for eid in ("hidden", "truth"):
            with self.assertRaises(ExperienceError):
                self.claim("launder-" + eid, SUBJECTIVE_INTERPRETATION, "猜测",
                           [{"session_id": "s1", "event_id": eid}], uncertainty="未知")
        with self.assertRaises(ExperienceError):
            self.claim("truth-claim", CANONICAL_WORLD_TRUTH, "未见到的世界真相",
                       [{"session_id": "s1", "event_id": "visible"}])
        output = json.dumps(self.memory.recall(["s1"]), ensure_ascii=False)
        self.assertNotIn("隐含信息", output)
        self.assertNotIn("未见到的世界真相", output)
        self.assertNotIn("private-source", output)
        self.assertEqual(len(self.memory.recall(["s1"])), 1)

    def test_conversation_and_reopen_cross_session(self):
        """对话保持观察顺序及未知说话人；重新打开数据库后保留跨会话引用。"""
        self.observe("line1", "你好", channel="dialogue", speaker="甲", conversation_id="c")
        self.observe("line2", "再见", channel="dialogue", conversation_id="c")
        self.observe("hidden-line", "秘密", channel="dialogue", conversation_id="c", visibility="hidden")
        self.observe("not-grouped", "无对话标记", channel="dialogue")
        self.observe("line3", "另一会话", session="s2", channel="dialogue", conversation_id="c")
        self.claim("cross", HYPOTHESIS, "可能记得问候", [{"session_id": "s1", "event_id": "line1"}],
                   session="s2", uncertainty="仅基于画面文本")
        self.log.close()
        self.log = PlayerEventLog(self.path)
        self.memory = PlayerExperience(self.log)
        conversations = self.memory.conversations(["s1", "s2"])
        self.assertEqual([c["session_id"] for c in conversations], ["s1", "s2"])
        self.assertEqual([t["text"] for t in conversations[0]["turns"]], ["你好", "再见"])
        self.assertEqual(conversations[0]["turns"][0]["speaker"], "甲")
        self.assertNotIn("speaker", conversations[0]["turns"][1])
        cross = self.memory.recall(["s2"])[-1]
        self.assertEqual(cross["evidence"][0], {"session_id": "s1", "event_id": "line1", "source_id": "source-line1"})
        with self.assertRaises(ExperienceError):
            self.memory.recall(["s1", "s1"])

    def test_duplicate_and_invalid_input_leave_event_and_projection_unchanged(self):
        """重复事件及拒绝的输入不产生部分事务或污染投影。"""
        seq = self.observe("same", "第一条")
        before = self.log.get_projection("s1", PROJECTION)
        self.assertEqual(before["revision"], seq)
        with self.assertRaises(EventLogConflict):
            self.observe("same", "覆盖内容")
        with self.assertRaises(ExperienceError):
            self.claim("bad", EXPERIENCED_FACT, "没有证据", [])
        self.assertEqual(self.log.get_projection("s1", PROJECTION), before)
        events = self.log.list_events("s1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"], before["state"]["entries"][0])
        self.log.close()
        self.log = PlayerEventLog(self.path)
        self.memory = PlayerExperience(self.log)
        self.assertEqual(self.memory.recall(["s1"])[0]["text"], "第一条")

    def test_projection_failure_rolls_back_inserted_event(self):
        """模拟索引写入失败，验证事件插入也回滚。"""
        self.observe("base", "基础")
        before = self.log.get_projection("s1", PROJECTION)
        self.log._db.execute("""CREATE TRIGGER fail_experience_projection
            BEFORE INSERT ON experience_entries BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        with self.assertRaises(ValueError):
            self.observe("rejected", "失败写入")
        self.assertEqual(self.log.get_projection("s1", PROJECTION), before)
        self.assertEqual([x["event_id"] for x in self.log.list_events("s1")], ["base"])
        self.log.close()
        self.log = PlayerEventLog(self.path)
        self.memory = PlayerExperience(self.log)
        self.assertEqual([x["event_id"] for x in self.memory.recall(["s1"])], ["base"])

    def test_stale_projection_conflict_preserves_winner(self):
        """两个连接之间旧 revision 必须冲突，且失败事件不能残留。"""
        self.observe("base", "基础")
        other_log = PlayerEventLog(self.path)
        other = PlayerExperience(other_log)
        original_append = self.log.append

        def interleave(*args, **kwargs):
            """在第一个读取快照之后提交第二个连接的更新。"""
            other.record_observation(session_id="s1", event_id="winner", timestamp="t",
                                     source_id="source-winner", channel="visual", text="获胜",
                                     visibility="player")
            return original_append(*args, **kwargs)

        try:
            with patch.object(self.log, "append", side_effect=interleave):
                with self.assertRaises(EventLogConflict):
                    self.memory.end_conversation(session_id="s1", event_id="loser", timestamp="t",
                                                 source_id="end", conversation_id="c")
            self.assertEqual([x["event_id"] for x in self.memory.recall(["s1"])], ["base", "winner"])
            self.assertEqual([x["event_id"] for x in self.log.list_events("s1")], ["base", "winner"])
        finally:
            other_log.close()

    def test_unknown_projection_version_fails_closed(self):
        """不兼容的投影不能被覆盖。"""
        event = {"event_id": "other", "session_id": "s1", "timestamp": "t", "type": "other",
                 "source": "test", "payload": {}}
        self.log.append([event], projections={PROJECTION: {"version": 2, "entries": []}})
        with self.assertRaises(ExperienceError):
            self.observe("new", "文字")
        with self.assertRaises(ExperienceError):
            self.memory.recall(["s1"])
        self.assertEqual(len(self.log.list_events("s1")), 1)


if __name__ == "__main__":
    unittest.main()
