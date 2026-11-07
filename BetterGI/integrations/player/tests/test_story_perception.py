#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剧情感知边界：来源、区域、未知状态及无服务观察适配。"""

import asyncio
import base64
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from player_dialogue import DialogueTracker  # noqa: E402
from player_knowledge import EvidenceStore  # noqa: E402
import player_vision  # noqa: E402


def row(text, x, y, *, confidence=None):
    return {"text": text, "confidence": confidence, "bbox": {"x": 10, "y": 20, "width": 20, "height": 10},
            "normalized_bbox": {"x": x, "y": y, "width": 0.1, "height": 0.05}}


def visual(frame, category="Talk", rows=None, timestamp=None):
    return {"frame_id": frame, "captured_at_ms": timestamp if timestamp is not None else int(frame),
            "ui_category": category, "ocr": rows}


def empty_visual(frame, category="Main", timestamp=None):
    """模拟对原始全帧成功执行区域 OCR，明确返回空列表。"""
    return {**visual(frame, category, [], timestamp), "ocr_requested": True, "ocr_complete": True,
            "ocr_roi": {"x": 0, "y": 0, "width": 100, "height": 100},
            "original_width": 100, "original_height": 100}


def types(result):
    return [event["type"] for event in result["events"]]


def test_only_explicit_ui_and_configured_roi_are_used():
    tracker = DialogueTracker(dialogue_roi=(0.1, 0.7, 0.8, 0.95), choice_roi=(0.8, 0.3, 1, 0.7))
    hud = row("任务追踪", 0.3, 0.15)
    assert types(tracker.observe(visual("1", "Main", [hud]))) == []
    assert types(tracker.observe(visual("2", "Unknown", [hud]))) == []
    first = tracker.observe(visual("3", rows=[hud, row("跟我来", 0.4, 0.8, confidence=0.2), row("好", 0.9, 0.5)]))
    assert types(first) == ["dialogue_started", "line_changed", "choice_presented"]
    conversation = first["conversation"]
    assert conversation["lines"][0]["text"] == "跟我来"
    assert conversation["lines"][0]["speaker"] is None
    assert conversation["lines"][0]["rows"][0]["confidence"] == 0.2
    assert conversation["choices"][0]["texts"] == ["好"]
    assert conversation["started"]["frame_id"] == "3"
    assert conversation["lines"][0]["source"]["timestamp_ms"] == 3
    first["conversation"]["lines"].clear()
    assert tracker.conversation["lines"]
    assert types(tracker.observe(visual("4", rows=[row("跟我来", 0.4, 0.8), row("好", 0.9, 0.5)]))) == []
    changed = tracker.observe(visual("5", rows=[row("出发吧", 0.4, 0.8)]))
    assert types(changed) == ["line_changed"]
    assert [line["text"] for line in changed["conversation"]["lines"]] == ["跟我来", "出发吧"]


def test_missing_roi_missing_bbox_and_missing_source_never_infer_text():
    no_roi = DialogueTracker()
    result = no_roi.observe(visual("1", rows=[row("任务", 0.5, 0.8)]))
    assert types(result) == ["dialogue_started"]
    assert not result["conversation"]["lines"]
    tracker = DialogueTracker(dialogue_roi=(0, 0, 1, 1))
    assert types(tracker.observe(visual("2", rows=[{"text": "无区域"}]))) == ["dialogue_started"]
    assert not tracker.observe({"ui_category": "Talk", "ocr": [row("无来源", 0.5, 0.5)]})["conversation"]["lines"]
    assert not tracker.context_lines()
    with pytest.raises(ValueError):
        DialogueTracker(dialogue_roi=(0, 0, 2, 1))


def test_end_requires_consecutive_distinct_non_dialogue_frames():
    tracker = DialogueTracker(non_dialogue_frames=3, dialogue_roi=(0, .7, 1, 1), choice_roi=(.7, .2, 1, .6))
    tracker.observe(visual("1"))
    assert not tracker.observe(empty_visual("2"))["ended"]
    assert not tracker.observe(empty_visual("2"))["ended"]
    assert not tracker.observe(visual("3", "Unknown"))["ended"]
    assert not tracker.observe(empty_visual("4"))["ended"]
    assert not tracker.observe({"ui_category": "Main"})["ended"]
    assert tracker.is_locked(now_ms=10**15)
    assert not tracker.observe(empty_visual("5"))["ended"]
    assert not tracker.observe(empty_visual("6", "BigMap"))["ended"]
    result = tracker.observe(empty_visual("7"))
    assert types(result) == ["dialogue_ended"]
    assert result["conversation"]["ended"]["frame_id"] == "7"
    assert not tracker.is_locked()
    assert types(tracker.observe(visual("8"))) == ["dialogue_started"]
    assert tracker.conversation["lines"] == []


def test_live_frames_are_lightweight_unknown_and_gaps_break_negative_streak():
    tracker = DialogueTracker(non_dialogue_frames=2, dialogue_roi=(0, 0, 1, 1))
    tracker.observe(visual("1", timestamp=100))
    # 即使附带 OCR，Live 也不借用独立截图的 OCR。
    live = {"type": "frame", "seq": 1, "jpeg": "anything", "ocr": [row("不归属此帧", .5, .5)]}
    assert types(tracker.observe_live_frame(live)) == []
    assert tracker.conversation["lines"] == []
    assert not tracker.observe_live_frame({"seq": 2, "uiCategory": "Main"})["ended"]
    assert not tracker.observe_live_frame({"seq": 4, "uiCategory": "Main"})["ended"]
    assert not tracker.observe_live_frame({"seq": 5, "uiCategory": "Main"})["ended"]
    # Live 没有同帧 OCR，不能提供空字幕和空选项的证明。
    assert tracker.is_locked()


def test_delayed_observe_not_discarded_by_unclassified_live_frame():
    tracker = DialogueTracker(dialogue_roi=(0, 0, 1, 1))
    tracker.observe_live_frame({"seq": 1, "timestampUtc": "2025-01-01T00:00:02Z", "jpeg": "x"})
    result = tracker.observe(visual("1", rows=[row("台词", .5, .5)], timestamp=1735689601000))
    assert types(result) == ["dialogue_started", "line_changed"]
    assert result["conversation"]["lines"][0]["source"]["timestamp_ms"] == 1735689601000


def test_contradictions_out_of_order_and_cutscene_are_conservative():
    tracker = DialogueTracker(non_dialogue_frames=2, dialogue_roi=(0, .7, 1, 1), choice_roi=(.7, .2, 1, .6))
    tracker.observe(visual("1", timestamp=100))
    assert not tracker.observe(empty_visual("2", timestamp=101))["ended"]
    assert not tracker.observe(empty_visual("stale", timestamp=99))["ended"]
    contradictory = empty_visual("3", timestamp=102)
    contradictory["is_in_talk_ui"] = True
    assert not tracker.observe(contradictory)["ended"]
    assert not tracker.observe(empty_visual("4", timestamp=103))["ended"]
    assert tracker.observe(empty_visual("5", timestamp=104))["ended"]
    event = tracker.observe({"frame_id": "6", "captured_at_ms": 105, "cutscene": True})["events"]
    assert event[0]["type"] == "cutscene" and event[0]["active"] is True
    assert not tracker.observe({"frame_id": "7", "captured_at_ms": 106})["events"]
    assert tracker.observe({"frame_id": "8", "captured_at_ms": 107, "cutscene": False})["events"][0]["active"] is False


def test_screen_observer_preserves_same_frame_bboxes_without_server(tmp_path, monkeypatch):
    data = {"sessionId": "s", "frameId": "f", "capturedAtUtc": datetime.now(timezone.utc).isoformat(),
            "imageBase64": base64.b64encode(b"same-frame").decode(),
            "image": {"width": 640, "height": 360, "originalWidth": 1920, "originalHeight": 1080},
            "vision": {"uiCategory": "Talk", "isInTalkUi": None, "ocr": [
                {"text": "你好", "x": 500, "y": 800, "width": 300, "height": 50,
                 "normalizedX": .5, "normalizedY": .8, "normalizedWidth": .2, "normalizedHeight": .05}]}}

    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return data

    requests = []

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def request(self, *args, **kwargs):
            requests.append(kwargs["json"])
            return Response()

    class Http:
        @staticmethod
        def ClientTimeout(**kwargs): return None
        @staticmethod
        def ClientSession(**kwargs): return Session()

    monkeypatch.setattr(player_vision, "aiohttp", Http)
    evidence = EvidenceStore(tmp_path)
    roi = {"x": 200, "y": 700, "width": 1200, "height": 300}
    observer = player_vision.ScreenObserver("http://localhost:80", "t", "s", evidence, ocr_roi=roi)
    roi["x"] = 0  # 构造后外部修改不能扩大识别区域。
    result = asyncio.run(observer.capture())
    assert requests[0]["includeOcr"] is True
    assert requests[0]["ocrRoi"] == {"x": 200, "y": 700, "width": 1200, "height": 300}
    assert result["ocr_requested"] is True and result["ocr_roi"] == requests[0]["ocrRoi"]
    assert result["ocr_complete"] is True
    assert result["is_in_talk_ui"] is None
    assert result["original_width"] == 1920 and result["width"] == 640
    found = result["ocr"][0]
    assert found["bbox"] == {"x": 500, "y": 800, "width": 300, "height": 50}
    assert found["normalized_bbox"] == {"x": .5, "y": .8, "width": .2, "height": .05}
    assert found["confidence"] is None
    assert evidence.get(found["evidence_id"])["frame_id"] == result["frame_id"]
    story = DialogueTracker(dialogue_roi=(0, .7, 1, 1)).observe(result)
    assert story["conversation"]["lines"][0]["source"]["frame_id"] == "f"
    assert story["conversation"]["lines"][0]["source"]["frame_sha256"] == result["frame_sha256"]
    assert abs(story["conversation"]["lines"][0]["source"]["timestamp_ms"] - int(time.time() * 1000)) < 15000

    # 即使错误/恶意响应带 OCR，未请求时也不创建证据或返回文字。
    no_ocr_store = EvidenceStore(tmp_path / "no-ocr")
    no_ocr = player_vision.ScreenObserver("http://localhost:80", "t", "s", no_ocr_store)
    no_ocr_result = asyncio.run(no_ocr.capture())
    assert requests[1]["includeOcr"] is False and "ocrRoi" not in requests[1]
    assert no_ocr_result["ocr"] is None and no_ocr_result["ocr_requested"] is False
    assert not no_ocr_result["ocr_complete"]
    assert not list((tmp_path / "no-ocr").glob("*.json"))

    # 丢弃异常行后不能把剩下的空列表当成可靠的空 OCR。
    data["vision"]["ocr"] = [None]
    incomplete = player_vision.ScreenObserver("http://localhost:80", "t", "s", evidence,
                                               ocr_roi={"x": 0, "y": 0, "width": 10, "height": 10})
    incomplete_result = asyncio.run(incomplete.capture())
    assert incomplete_result["ocr"] == [] and incomplete_result["ocr_complete"] is False


def test_named_empty_regions_are_candidates_not_global_screen_claims():
    tracker = DialogueTracker(non_dialogue_frames=2, dialogue_roi=(0.1, 0.7, 0.7, 0.9),
                              choice_roi=(0.75, 0.3, 0.95, 0.6))
    tracker.observe(visual("1", "Talk"))
    def empty(seq):
        return {**visual(str(seq), "Main", []), "ocr_requested": True, "ocr_complete": True,
                "original_width": 100, "original_height": 100,
                "ocr_regions": {
                    "dialogue_text": {"completed": True, "raw_detection_count": 0,
                                      "roi": {"x": 10, "y": 70, "width": 60, "height": 20}},
                    "choice": {"completed": True, "raw_detection_count": 0,
                               "roi": {"x": 75, "y": 30, "width": 20, "height": 30}},
                }}
    assert not tracker.observe(empty(2))["ended"]
    failed = empty(3)
    failed["ocr_regions"]["choice"]["completed"] = False
    assert not tracker.observe(failed)["ended"]
    assert not tracker.observe(empty(4))["ended"]
    assert tracker.observe(empty(5))["ended"]


def test_named_ocr_regions_share_frame_and_do_not_hide_failures(tmp_path, monkeypatch):
    regions = {"dialogue_text": {"x": 10, "y": 70, "width": 60, "height": 20},
               "choice": {"x": 75, "y": 30, "width": 20, "height": 30}}
    data = {"sessionId": "s", "frameId": "f", "capturedAtUtc": datetime.now(timezone.utc).isoformat(),
            "imageBase64": base64.b64encode(b"same-frame").decode(),
            "image": {"originalWidth": 100, "originalHeight": 100},
            "vision": {"uiCategory": "Talk"}, "ocrRegions": [
                {"name": "dialogue_text", "frameId": "f", "roi": regions["dialogue_text"],
                 "completed": True, "status": "Completed", "rawDetectionCount": 1,
                 "candidates": [{"text": "跟我来", "x": 20, "y": 75, "width": 20, "height": 10,
                                 "normalizedX": .3, "normalizedY": .8, "normalizedWidth": .2, "normalizedHeight": .1}]},
                {"name": "choice", "frameId": "f", "roi": regions["choice"],
                 "completed": False, "status": "Unavailable", "candidates": None},
            ]}
    requests = []
    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return data
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def request(self, *args, **kwargs):
            requests.append(kwargs["json"])
            return Response()
    class Http:
        @staticmethod
        def ClientTimeout(**kwargs): return None
        @staticmethod
        def ClientSession(**kwargs): return Session()
    monkeypatch.setattr(player_vision, "aiohttp", Http)
    observer = player_vision.ScreenObserver("http://localhost:80", "t", "s", EvidenceStore(tmp_path),
                                            named_regions=regions)
    result = asyncio.run(observer.capture())
    assert requests[0]["ocrRegions"] == [{"name": name, "roi": roi} for name, roi in regions.items()]
    assert result["ocr"][0]["region"] == "dialogue_text"
    assert result["ocr_complete"] is False
    assert result["ocr_regions"]["choice"]["completed"] is False
    data["ocrRegions"][0]["frameId"] = "different"
    with pytest.raises(ValueError, match="帧不匹配"):
        asyncio.run(player_vision.ScreenObserver("http://localhost:80", "t", "s", EvidenceStore(tmp_path),
                                                 named_regions=regions).capture())


@pytest.mark.parametrize("roi", [
    {}, {"x": 1, "y": 1, "width": 0, "height": 2},
    {"x": -1, "y": 1, "width": 2, "height": 2},
    {"x": True, "y": 1, "width": 2, "height": 2},
    {"x": 1.0, "y": 1, "width": 2, "height": 2},
    {"x": 2147483647, "y": 0, "width": 1, "height": 1},
    {"x": 1, "y": 1, "width": 2, "height": 2, "extra": 0},
    [1, 1, 2, 2],
])
def test_screen_observer_rejects_invalid_pixel_roi(tmp_path, roi):
    with pytest.raises(ValueError):
        player_vision.ScreenObserver("http://localhost:80", "t", "s", EvidenceStore(tmp_path), ocr_roi=roi)


def test_speaker_and_canonical_events_are_same_frame_and_legacy_events_remain():
    tracker = DialogueTracker(dialogue_roi=(.1, .7, .8, .95), speaker_roi=(.1, .55, .5, .68),
                              choice_roi=(.8, .3, 1, .7))
    first = tracker.observe(visual("1", rows=[row("你好", .4, .8), row("派蒙", .3, .6), row("好", .9, .5)]))
    assert types(first) == ["dialogue_started", "line_changed", "choice_presented"]
    assert [e["type"] for e in first["dialogue_events"]] == [
        "dialogue_started", "dialogue_line_changed", "dialogue_choice_presented"]
    line = first["conversation"]["lines"][0]
    assert line["speaker"] == "派蒙" and line["speaker_rows"][0]["text"] == "派蒙"
    assert line["source"]["frame_id"] == "1"
    second = tracker.observe(visual("2", rows=[row("再见", .4, .8)]))
    assert second["conversation"]["lines"][-1]["speaker"] is None
    assert [e["type"] for e in second["conversation"]["entries"]] == ["line", "choices", "line"]
    # 即使相同台词，只有当前帧的 speaker ROI 可以归属说话人。
    assert tracker.observe(visual("3", rows=[row("再见", .4, .8), row("安柏", .3, .6)]))["conversation"]["lines"][-1]["speaker"] == "安柏"


def test_unclassified_live_frames_do_not_break_observation_streak():
    tracker = DialogueTracker(non_dialogue_frames=2, dialogue_roi=(0, .7, 1, 1), choice_roi=(.7, .2, 1, .6))
    tracker.observe(visual("1"))
    assert not tracker.observe(empty_visual("2"))["ended"]
    assert not tracker.observe_live_frame({"type": "frame", "seq": 1})["ended"]
    assert not tracker.observe_live_frame({"type": "frame", "seq": 2})["ended"]
    assert tracker.observe(empty_visual("3"))["ended"]


def test_conversation_does_not_truncate_long_dialogue():
    tracker = DialogueTracker(dialogue_roi=(0, 0, 1, 1))
    for n in range(1, 106):
        result = tracker.observe(visual(str(n), rows=[row(f"台词 {n}", .5, .5)]))
    assert len(result["conversation"]["lines"]) == 105
    assert len(result["conversation"]["entries"]) == 105
    assert result["conversation"]["lines"][0]["text"] == "台词 1"


def test_end_requires_complete_covered_empty_ocr_and_same_session():
    tracker = DialogueTracker(non_dialogue_frames=2, dialogue_roi=(0, .7, 1, 1), choice_roi=(.7, .2, 1, .6))
    start = visual("1", rows=[row("台词", .5, .8)])
    start["session_id"] = "session-a"
    tracker.observe(start)
    for n, patch in enumerate([
        {"ocr": None}, {"ocr_complete": False}, {"ocr_requested": False},
        {"ocr_roi": {"x": 0, "y": 70, "width": 100, "height": 30}},
        {"original_width": None}, {"ocr": [{"text": "bad"}]},
        {"ocr": [row("选项", .9, .5)]}, {"session_id": "session-b"},
    ], 2):
        frame = {**empty_visual(str(n)), "session_id": "session-a", **patch}
        assert not tracker.observe(frame)["ended"]
    a = {**empty_visual("10"), "session_id": "session-a"}
    b = {**empty_visual("11"), "session_id": "session-a"}
    assert not tracker.observe(a)["ended"]
    ended = tracker.observe(b)
    assert ended["ended"] and ended["dialogue_events"][-1]["type"] == "dialogue_ended"
    assert ended["conversation"]["ended"]["session_id"] == "session-a"
    evidence = ended["dialogue_events"][-1]["evidence"]
    assert [item["source"]["frame_id"] for item in evidence] == ["10", "11"]
    assert ended["conversation"]["end_evidence"] == evidence


def test_screen_observer_combines_pixel_regions_and_rejects_mixed_config(tmp_path):
    store = EvidenceStore(tmp_path)
    regions = {"dialogue_text": {"x": 20, "y": 70, "width": 70, "height": 20},
               "speaker": {"x": 20, "y": 55, "width": 25, "height": 10},
               "choice": {"x": 80, "y": 25, "width": 20, "height": 30}}
    observer = player_vision.ScreenObserver("http://localhost:80", "t", "s", store, ocr_regions=regions)
    regions["speaker"]["x"] = 0
    assert observer._ocr_roi == {"x": 20, "y": 25, "width": 80, "height": 65}
    with pytest.raises(ValueError):
        player_vision.ScreenObserver("http://localhost:80", "t", "s", store,
                                     ocr_roi={"x": 0, "y": 0, "width": 1, "height": 1}, ocr_regions=regions)
