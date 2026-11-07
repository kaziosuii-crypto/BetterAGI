from __future__ import annotations

import danmaku_hub


def test_add_and_since_cursor():
    hub = danmaku_hub.build_hub()
    first = hub.add("主播好", user="A", t_ms=1000)
    second = hub.add("往右走", user="B", t_ms=2000)
    assert first["t_ms"] == 1000 and second["seq"] == 2
    assert [i["text"] for i in hub.since(0)] == ["主播好", "往右走"]
    assert [i["text"] for i in hub.since(1000)] == ["往右走"]
    assert hub.since(2000) == []


def test_empty_text_is_ignored():
    hub = danmaku_hub.build_hub()
    assert hub.add("") is None
    assert hub.add("   ") is None
    assert hub.stats()["items"] == 0


def test_limit_is_clamped():
    hub = danmaku_hub.build_hub()
    for i in range(5):
        hub.add("第%d条" % i, t_ms=1000 + i)
    assert len(hub.since(0, limit=2)) == 2
    assert len(hub.since(0, limit=9999)) == 5


def test_capacity_drops_oldest():
    hub = danmaku_hub.DanmakuHub(capacity=3)
    for i in range(5):
        hub.add("第%d条" % i, t_ms=1000 + i)
    texts = [i["text"] for i in hub.since(0)]
    assert texts == ["第2条", "第3条", "第4条"]
    assert hub.stats()["dropped"] == 2


def test_text_is_truncated_and_clip_returns_texts():
    hub = danmaku_hub.build_hub()
    hub.add("x" * 900)
    items = hub.since(0)
    assert len(items[0]["text"]) == danmaku_hub.MAX_TEXT_CHARS
    assert hub.clip(items) == [items[0]["text"]]


def test_clear_resets():
    hub = danmaku_hub.build_hub()
    hub.add("hi", t_ms=1)
    hub.clear()
    assert hub.stats()["items"] == 0 and hub.since(0) == []