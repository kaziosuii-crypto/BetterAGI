from __future__ import annotations

import threading

import httpx

import tui


class FakeInject:
    def __init__(self):
        import queue

        self.queue = queue.Queue()

    def push(self, text):
        self.queue.put(text)
        return True


def _serve(monkeypatch=None):
    import danmaku_hub

    hub = danmaku_hub.build_hub()
    runtime = tui.Runtime(goal="测试")
    stop = threading.Event()
    api = tui.Api("127.0.0.1", 0, "tok", runtime, FakeInject(), stop, danmaku=hub)
    return api, hub, stop


def test_danmaku_endpoints():
    api, hub, stop = _serve()
    try:
        base = api.url()
        headers = {"Authorization": "Bearer tok"}
        with httpx.Client(trust_env=False, timeout=5.0) as client:
            assert client.get(base + "/danmaku").status_code == 401
            hub.add("主播加油", user="观众A", t_ms=1000)
            hub.add("往左是死路", user="观众B", t_ms=2000)
            got = client.get(base + "/danmaku?since=1000&limit=10", headers=headers).json()
            assert got["ok"] is True
            assert [i["text"] for i in got["items"]] == ["往左是死路"]
            assert got["next_since"] == 2000
            stats = client.get(base + "/stats", headers=headers).json()
            assert stats["items"] == 2 and stats["last_ms"] == 2000
    finally:
        api.server.shutdown()


def test_post_danmaku_only_buffers_by_default():
    api, hub, stop = _serve()
    try:
        base = api.url()
        headers = {"Authorization": "Bearer tok"}
        with httpx.Client(trust_env=False, timeout=5.0) as client:
            posted = client.post(base + "/danmaku", headers=headers,
                                 json={"text": "这boss好难", "user": "观众C"}).json()
            assert posted["ok"] is True and posted["queued"] is False
            assert hub.stats()["items"] == 1
            pushed = client.post(base + "/danmaku", headers=headers,
                                 json={"text": "插一句", "interject": True}).json()
            assert pushed["queued"] is True
    finally:
        api.server.shutdown()


def test_no_hub_reports_not_configured():
    runtime = tui.Runtime(goal="测试")
    api = tui.Api("127.0.0.1", 0, "tok", runtime, FakeInject(), threading.Event(), danmaku=None)
    try:
        with httpx.Client(trust_env=False, timeout=5.0) as client:
            got = client.get(api.url() + "/danmaku",
                             headers={"Authorization": "Bearer tok"}).json()
            assert got["ok"] is False and got["error"] == "not_configured"
    finally:
        api.server.shutdown()