# -*- coding: utf-8 -*-
"""render_vtuber 的单元测试：时间线归一化、排期、ASS 转义与 ffmpeg 命令构建。

测试不调用 ffmpeg / ffprobe，也不依赖 GPU。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

# 允许直接运行：python integrations/vtuber/tests/test_render_timeline.py
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import render_vtuber as rv  # noqa: E402


def make_event(raw, index=0):
    """测试辅助：把原始字典归一化为 TimelineEvent。"""

    return rv.normalize_event(raw, index)


def commentary(ts, text="一段解说", kind="reaction", emotion="", seq=1, index=0):
    """测试辅助：构造 commentary 事件。"""

    return make_event(
        {
            "type": "commentary",
            "kind": kind,
            "text": text,
            "emotion": emotion,
            "ts": ts,
            "seq": seq,
        },
        index=index,
    )


class TimestampTests(unittest.TestCase):
    def test_parse_iso_z(self):
        parsed = rv.parse_timestamp("2026-10-26T14:00:02.345Z")
        self.assertEqual(parsed, datetime(2026, 10, 26, 14, 0, 2, 345000, tzinfo=timezone.utc))

    def test_parse_offset(self):
        parsed = rv.parse_timestamp("2026-10-26T22:00:00+08:00")
        self.assertEqual(parsed, datetime(2026, 10, 26, 14, 0, 0, tzinfo=timezone.utc))

    def test_parse_naive_assumes_utc(self):
        parsed = rv.parse_timestamp("2026-10-26T14:00:00")
        self.assertEqual(parsed, datetime(2026, 10, 26, 14, 0, 0, tzinfo=timezone.utc))

    def test_parse_epoch_milliseconds(self):
        parsed = rv.parse_timestamp(1761487202345)
        self.assertEqual(parsed, datetime(2025, 10, 26, 14, 0, 2, 345000, tzinfo=timezone.utc))

    def test_parse_numeric_string_seconds(self):
        parsed = rv.parse_timestamp("1761487202")
        self.assertEqual(parsed, datetime(2025, 10, 26, 14, 0, 2, tzinfo=timezone.utc))

    def test_parse_invalid_returns_none(self):
        self.assertIsNone(rv.parse_timestamp("不是时间"))
        self.assertIsNone(rv.parse_timestamp(None))
        self.assertIsNone(rv.parse_timestamp(""))


class TimelineParsingTests(unittest.TestCase):
    def test_payload_accepts_events_wrapper(self):
        events = rv.parse_timeline_payload(
            {
                "events": [
                    {"type": "commentary", "kind": "plan", "text": "先爬山", "ts": "2026-10-26T14:00:01Z", "seq": 1},
                    {"type": "guidance", "text": "向北走", "ts": "2026-10-26T14:00:02Z", "seq": 2},
                ]
            }
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event_type, "commentary")
        self.assertEqual(events[0].kind, "plan")
        self.assertEqual(events[1].event_type, "guidance")

    def test_payload_rejects_bad_shape(self):
        with self.assertRaises(ValueError):
            rv.parse_timeline_payload({"foo": 1})
        with self.assertRaises(ValueError):
            rv.parse_timeline_payload("nope")

    def test_camel_case_timestamp_utc(self):
        event = make_event(
            {
                "type": "commentary",
                "kind": "reaction",
                "text": "你好",
                "timestampUtc": "2026-10-26T14:00:02.345Z",
                "seq": 7,
            }
        )
        self.assertEqual(event.seq, 7)
        self.assertEqual(
            event.timestamp, datetime(2026, 10, 26, 14, 0, 2, 345000, tzinfo=timezone.utc)
        )

    def test_type_can_be_commentary_kind(self):
        event = make_event({"type": "result", "text": "复盘一下", "ts": "2026-10-26T14:00:00Z"})
        self.assertEqual(event.event_type, "commentary")
        self.assertEqual(event.kind, "result")

    def test_missing_kind_defaults_to_reaction(self):
        event = make_event({"type": "commentary", "text": "哇", "ts": "2026-10-26T14:00:00Z"})
        self.assertEqual(event.kind, "reaction")


class StartResolutionTests(unittest.TestCase):
    def test_prefers_earliest_live_started(self):
        events = [
            commentary("2026-10-26T13:59:59Z", text="开始前", index=0),
            make_event({"type": "live.started", "ts": "2026-10-26T14:00:00Z"}, index=1),
            commentary("2026-10-26T14:00:02Z", text="开始后", index=2),
        ]
        start = rv.resolve_start(events)
        self.assertEqual(start, datetime(2026, 10, 26, 14, 0, 0, tzinfo=timezone.utc))

    def test_falls_back_to_earliest_event(self):
        events = [
            commentary("2026-10-26T14:00:05Z", index=0),
            commentary("2026-10-26T14:00:01Z", index=1),
        ]
        start = rv.resolve_start(events)
        self.assertEqual(start, datetime(2026, 10, 26, 14, 0, 1, tzinfo=timezone.utc))

    def test_explicit_start_ts_wins(self):
        events = [make_event({"type": "live.started", "ts": "2026-10-26T14:00:00Z"})]
        start = rv.resolve_start(events, "2026-10-26T15:00:00Z")
        self.assertEqual(start, datetime(2026, 10, 26, 15, 0, 0, tzinfo=timezone.utc))

    def test_invalid_explicit_start_raises(self):
        with self.assertRaises(ValueError):
            rv.resolve_start([], "bad-time")

    def test_no_timestamps_returns_none(self):
        events = [make_event({"type": "commentary", "text": "无时间"})]
        self.assertIsNone(rv.resolve_start(events))


class DurationTests(unittest.TestCase):
    def test_formula(self):
        self.assertAlmostEqual(rv.display_duration_seconds("abcdefghij"), 3.7)

    def test_minimum(self):
        # 基础时长 2.5s 已高于下限 2s；空文本按基础时长计算（空文本会在排期前被过滤）。
        self.assertEqual(rv.display_duration_seconds(""), 2.5)
        self.assertEqual(rv.display_duration_seconds("短"), 2.62)

    def test_maximum(self):
        self.assertEqual(rv.display_duration_seconds("字" * 200), 8.0)


class SchedulingTests(unittest.TestCase):
    def test_clamps_before_start_to_zero(self):
        start = datetime(2026, 10, 26, 14, 0, 10, tzinfo=timezone.utc)
        events = [commentary("2026-10-26T14:00:05Z", text="早于零点")]
        cues = rv.build_cues(events, start)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].start, 0.0)

    def test_drops_events_after_video_end(self):
        start = datetime(2026, 10, 26, 14, 0, 0, tzinfo=timezone.utc)
        events = [commentary("2026-10-26T14:00:10Z", text="太晚了")]
        cues = rv.build_cues(events, start, video_duration=3.0)
        self.assertEqual(cues, [])

    def test_trims_cue_to_video_end(self):
        start = datetime(2026, 10, 26, 14, 0, 0, tzinfo=timezone.utc)
        events = [commentary("2026-10-26T14:00:02Z", text="靠近结尾的解说")]
        cues = rv.build_cues(events, start, video_duration=4.0)
        self.assertEqual(len(cues), 1)
        self.assertAlmostEqual(cues[0].start, 2.0)
        self.assertAlmostEqual(cues[0].end, 4.0)

    def test_drops_short_remainder(self):
        start = datetime(2026, 10, 26, 14, 0, 0, tzinfo=timezone.utc)
        events = [commentary("2026-10-26T14:00:03.8Z", text="几乎没时间了")]
        cues = rv.build_cues(events, start, video_duration=4.0)
        self.assertEqual(cues, [])

    def test_sequential_scheduling_avoids_overlap(self):
        start = datetime(2026, 10, 26, 14, 0, 0, tzinfo=timezone.utc)
        events = [
            commentary("2026-10-26T14:00:00Z", text="第一句", seq=1, index=0),
            commentary("2026-10-26T14:00:00.2Z", text="第二句", seq=2, index=1),
        ]
        cues = rv.build_cues(events, start)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].start, 0.0)
        self.assertGreaterEqual(cues[1].start, cues[0].end)
        self.assertEqual(cues[1].start, cues[0].end)

    def test_sorts_by_timestamp_then_seq(self):
        start = datetime(2026, 10, 26, 14, 0, 0, tzinfo=timezone.utc)
        events = [
            commentary("2026-10-26T14:00:02Z", text="后", seq=9, index=0),
            commentary("2026-10-26T14:00:01Z", text="先", seq=2, index=1),
        ]
        cues = rv.build_cues(events, start)
        self.assertEqual([cue.text for cue in cues], ["先", "后"])

    def test_guidance_excluded_by_default(self):
        start = datetime(2026, 10, 26, 14, 0, 0, tzinfo=timezone.utc)
        events = [
            make_event({"type": "guidance", "text": "向北走", "ts": "2026-10-26T14:00:00Z"}, 0),
            commentary("2026-10-26T14:00:01Z", text="解说", index=1),
        ]
        self.assertEqual(len(rv.build_cues(events, start)), 1)
        self.assertEqual(len(rv.build_cues(events, start, include_guidance=True)), 2)

    def test_empty_text_is_ignored(self):
        start = datetime(2026, 10, 26, 14, 0, 0, tzinfo=timezone.utc)
        events = [
            commentary("2026-10-26T14:00:00Z", text="   ", index=0),
            make_event({"type": "live.started", "ts": "2026-10-26T14:00:00Z"}, 1),
        ]
        self.assertEqual(rv.build_cues(events, start), [])


class AssTests(unittest.TestCase):
    def test_escape_special_characters(self):
        source = "a\\b{c}\n[d]\r\n第二行"
        expected = "a\\\\b\\{c\\}\\N[d]\\N第二行"
        self.assertEqual(rv.escape_ass_text(source), expected)

    def test_escape_tab(self):
        self.assertEqual(rv.escape_ass_text("a\tb"), "a b")

    def test_color_bgr_conversion(self):
        self.assertEqual(rv.ass_color("#112233"), "&H00332211")
        self.assertEqual(rv.ass_color("ff0000"), "&H000000FF")
        self.assertEqual(rv.ass_color("#abc", alpha=0x80), "&H80CCBBAA")

    def test_time_format(self):
        self.assertEqual(rv.format_ass_time(0), "0:00:00.00")
        self.assertEqual(rv.format_ass_time(2.5), "0:00:02.50")
        self.assertEqual(rv.format_ass_time(3661.23), "1:01:01.23")
        self.assertEqual(rv.format_ass_time(-1), "0:00:00.00")

    def test_document_contains_styles_and_dialogue(self):
        cues = [
            rv.Cue(0.0, 2.5, "reaction", "surprised", "哇{惊}喜\n第二行"),
            rv.Cue(2.5, 5.0, "plan", "calm", "沿着道路前进"),
        ]
        document = rv.build_ass(cues, font="Microsoft YaHei")

        self.assertIn("[Script Info]", document)
        self.assertIn("[V4+ Styles]", document)
        self.assertIn("[Events]", document)
        self.assertIn("C_reaction_surprised", document)
        self.assertIn("C_plan_calm", document)
        self.assertIn("【惊讶】", document)
        self.assertIn(r"\{惊\}", document)
        self.assertIn(r"\N第二行", document)
        self.assertIn(
            "Dialogue: 0,0:00:00.00,0:00:02.50,C_reaction_surprised", document
        )
        self.assertIn(
            "Dialogue: 0,0:00:02.50,0:00:05.00,C_plan_calm", document
        )

    def test_guidance_style_and_prefix(self):
        document = rv.build_ass([rv.Cue(1.0, 3.0, "guidance", "", "向北走")])
        self.assertIn("C_guidance_none", document)
        self.assertIn("【指导】", document)

    def test_style_name_sanitized(self):
        self.assertEqual(rv._style_name("re action", "sur prised"), "C_re_action_sur_prised")

    def test_filter_path_escaping(self):
        escaped = rv.escape_filter_path(r"C:\Users\测试\a,b.ass")
        self.assertEqual(escaped, r"C\:/Users/测试/a\,b.ass")
        self.assertEqual(rv.escape_filter_path("a'b.ass"), r"a\'b.ass")


class CommandTests(unittest.TestCase):
    def test_subtitles_only_with_audio_copy(self):
        command = rv.build_ffmpeg_command(
            "ffmpeg", "in.mp4", r"C:\tmp\a.ass", "out.mp4", input_has_audio=True
        )
        self.assertIn("-vf", command)
        self.assertIn("subtitles=filename=", command[command.index("-vf") + 1])
        self.assertNotIn("-filter_complex", command)
        self.assertEqual(command[command.index("-c:a") + 1], "copy")
        self.assertIn("0:a?", command)
        self.assertEqual(command[command.index("-c:v") + 1], "libx264")

    def test_copy_audio_false_uses_aac(self):
        command = rv.build_ffmpeg_command(
            "ffmpeg", "in.mp4", "a.ass", "out.mp4", input_has_audio=True, copy_audio=False
        )
        self.assertEqual(command[command.index("-c:a") + 1], "aac")

    def test_no_audio_has_no_audio_options(self):
        command = rv.build_ffmpeg_command(
            "ffmpeg", "in.mp4", "a.ass", "out.mp4", input_has_audio=False
        )
        self.assertNotIn("-c:a", command)
        self.assertNotIn("0:a?", command)

    def test_avatar_bob_overlay(self):
        command = rv.build_ffmpeg_command(
            "ffmpeg",
            "in.mp4",
            r"C:\tmp\a.ass",
            "out.mp4",
            avatar="avatar.png",
            input_has_audio=True,
        )
        self.assertIn("-filter_complex", command)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("overlay=x=W-w-28:y=H-h-32+8*sin(2*PI*t/3)", graph)
        self.assertIn("shortest=1", graph)
        self.assertIn("-loop", command)
        self.assertIn("image2", command)
        self.assertIn("subtitles=filename=", graph)
        self.assertIn("[vout]", graph)
        self.assertIn("[avatar]", graph)
        self.assertIn("[vout]", command)
        self.assertEqual(command[command.index("-c:a") + 1], "copy")

    def test_tts_mixes_delayed_audio(self):
        command = rv.build_ffmpeg_command(
            "ffmpeg",
            "in.mp4",
            "a.ass",
            "out.mp4",
            tts_files=[(0.5, "a.mp3"), (1.0, "b.mp3")],
            input_has_audio=False,
        )
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("adelay=500|500", graph)
        self.assertIn("adelay=1000|1000", graph)
        self.assertIn("amix=inputs=2", graph)
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertEqual(command[command.index("-b:a") + 1], "192k")

    def test_tts_with_input_audio_mixes_three_sources(self):
        command = rv.build_ffmpeg_command(
            "ffmpeg",
            "in.mp4",
            "a.ass",
            "out.mp4",
            tts_files=[(0.0, "a.mp3")],
            input_has_audio=True,
        )
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("[0:a]aresample=48000", graph)
        self.assertIn("[abase]", graph)
        self.assertIn("channel_layouts=stereo", graph)
        self.assertIn("amix=inputs=2", graph)

    def test_duration_is_applied(self):
        command = rv.build_ffmpeg_command(
            "ffmpeg", "in.mp4", "a.ass", "out.mp4", duration=12.3456
        )
        self.assertEqual(command[command.index("-t") + 1], "12.346")

    def test_output_is_last_argument(self):
        command = rv.build_ffmpeg_command("ffmpeg", "in.mp4", "a.ass", "out.mp4")
        self.assertEqual(command[-1], "out.mp4")


if __name__ == "__main__":
    unittest.main()
