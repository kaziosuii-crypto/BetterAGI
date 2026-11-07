"""快系统 Omni 客户端的 deadline / 会话隔离测试（本简报新建）。

覆盖：
1. collect 首包 / 单步 deadline：服务端不回包时抛 OmniTimeout，不再永久挂住；
2. 超时即判死连接：尽力 response.cancel + close，_inflight 复位，broken=True；
3. 迟到事件不串台：超时后服务端补发的上一轮 response.done（带哨兵文本）
   绝不会被下一轮读到；没有可重建的传输时抛明确 OmniError_ 而不是空文本；
4. ask 总预算 max_total_s：超时抛 OmniTimeout，下一次 ask 自动 reconnect 恢复，
   重连失败或无法重连时抛明确错误。

所有用例都用 asyncio.wait_for 兜死，绝不真的等满 30s。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import omni  # noqa: E402

#: 只过魔数与长度校验的假 JPEG（encode_jpeg_frame 只查 FFD8FF 前缀）。
JPEG = b"\xff\xd8\xff" + b"\x00" * 8

#: 迟到事件的哨兵文本：只要它出现在下一轮答案里，就说明串台了。
SENTINEL_OLD = "SENTINEL_OLD"


def _run(coro, guard_s: float = 5.0):
    """在 asyncio.run 里再套一层 wait_for，用例本身不可能挂住。"""

    async def guarded():
        return await asyncio.wait_for(coro, timeout=guard_s)

    return asyncio.run(guarded())


class HangingTransport:
    """recv_json 永不返回：模拟服务端收到请求却一直不回包。"""

    def __init__(self) -> None:
        self.sent = []
        self.closed = False
        self.recv_calls = 0

    async def send_json(self, payload):
        self.sent.append(payload)

    async def recv_json(self):
        self.recv_calls += 1
        # 30s 远大于用例预算；只有被 wait_for 取消才会提前结束。
        await asyncio.sleep(30)
        return {"type": "response.done", "response": {"output": []}}

    async def close(self):
        self.closed = True

    def types(self):
        return [e.get("type", "") for e in self.sent]


class QueueTransport:
    """队列式传输：队列空时挂住；close 之后**仍然可以读到**队列里的事件。

    close 后还能读，是为了模拟真实服务端「连接关了、迟到事件还在路上/
    仍在本地缓冲」的情形：如果实现超时后没有彻底隔离旧会话，下一轮就会把
    这些事件（例如上一轮的 response.done）当成自己的答案——本文件的哨兵
    用例就是靠这个语义抓串台的。
    """

    def __init__(self, script=()):
        self._pending = list(script)
        self.sent = []
        self.closed = False
        self.read_events = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def recv_json(self):
        if self._pending:
            event = self._pending.pop(0)
            self.read_events.append(event)
            return dict(event)
        await asyncio.sleep(30)
        return {}

    async def close(self):
        self.closed = True

    def push(self, *events) -> None:
        """服务端补发的迟到事件（超时之后才到）。"""

        self._pending.extend(events)


class DripTransport:
    """不停吐 text.delta、永不 response.done：单步都在预算内，整轮却收不完。"""

    def __init__(self, interval: float = 0.02) -> None:
        self.interval = interval
        self.sent = []
        self.closed = False
        self.reads = 0

    async def send_json(self, payload):
        self.sent.append(payload)

    async def recv_json(self):
        await asyncio.sleep(self.interval)
        self.reads += 1
        return {"type": "response.text.delta", "delta": "滴"}

    async def close(self):
        self.closed = True


def _client(config=None, **kwargs):
    """统一构造客户端；默认预算压到 0.05s，避免用例本身变慢。"""

    cfg = config or omni.OmniConfig(first_token_timeout_s=0.05, step_timeout_s=0.05)
    return omni.OmniClient(cfg, **kwargs)


def test_omni_timeout_is_omni_error_subclass():
    """OmniTimeout 必须继承 OmniError_，老代码的 except OmniError_ 继续兜得住。"""

    assert issubclass(omni.OmniTimeout, omni.OmniError_)
    exc = omni.OmniTimeout("超时了", waited_s=1.5, budget_s=1.0,
                           event="首个事件", phase="首包（首 token）")
    assert isinstance(exc, omni.OmniError_)
    assert exc.waited_s == 1.5
    assert exc.budget_s == 1.0
    assert exc.event == "首个事件"
    assert exc.phase == "首包（首 token）"


def test_collect_first_token_timeout_quarantines_connection():
    """首包预算到点：抛 OmniTimeout，并判死连接（cancel + close + broken）。"""

    async def case():
        transport = HangingTransport()
        client = _client(transport=transport)
        await client.connect()

        with pytest.raises(omni.OmniTimeout) as caught:
            await client.collect()

        exc = caught.value
        # 报错信息要能定位：等了多久、预算是多少、在等什么。
        assert exc.phase.startswith("首包")
        assert "首个事件" in exc.event
        assert exc.budget_s == pytest.approx(0.05)
        assert exc.waited_s >= 0.04
        assert "%.2fs" % exc.waited_s in str(exc)

        # 连接判死：没等到 done 的轮次不能留给下一次复用。
        assert client.broken is True
        assert client._inflight is False
        assert "response.cancel" in transport.types()
        assert transport.closed is True
        # 预算到点就该返回，不要真的等满 recv 里的 30s。
        assert exc.waited_s < 2.0

    _run(case())


def test_collect_step_timeout_after_first_token():
    """首 token 到了、后续步不回：同样抛 OmniTimeout，阶段要报「后续步」。"""

    async def case():
        transport = QueueTransport([{"type": "response.text.delta", "delta": "半句"}])
        client = _client(transport=transport)
        await client.connect()

        with pytest.raises(omni.OmniTimeout) as caught:
            await client.collect()

        exc = caught.value
        assert "后续步" in exc.phase
        assert exc.event == "response.done"
        assert exc.budget_s == pytest.approx(0.05)
        assert client.broken is True
        assert client._inflight is False
        assert transport.closed is True

    _run(case())


def test_stale_response_done_never_leaks_after_timeout():
    """超时后服务端补发上一轮 done：下一轮必须换新会话，哨兵文本不能出现。"""

    async def case():
        old = QueueTransport()
        fresh = omni.ScriptedTransport([
            {"type": "response.text.done", "text": "新会话的答案"},
            {"type": "response.done", "response": {"output": []}},
        ])
        # 工厂只在超时后的 reconnect 被调用一次，必须给出干净的新连接。
        made = [fresh]
        client = _client(transport=old, transport_factory=lambda: made.pop(0))
        await client.connect()

        with pytest.raises(omni.OmniTimeout):
            await client.ask(JPEG)

        assert client.broken is True
        # 截止时间之后才到的上一轮尾巴（带哨兵），绝不能进下一轮。
        old.push(
            {"type": "response.text.done", "text": SENTINEL_OLD},
            {"type": "response.done", "response": {"output": []}},
        )
        reads_before = len(old.read_events)

        reply = await client.ask(JPEG)

        assert reply.text == "新会话的答案"
        assert SENTINEL_OLD not in reply.text
        # 旧连接一条事件都没再读（换连接后旧会话的事件投递不到客户端）。
        assert len(old.read_events) == reads_before
        assert old.closed is True
        assert client.broken is False
        # 新连接上确实重新发了图片与 response.create。
        assert "input_image_buffer.append" in fresh.types()
        assert "response.create" in fresh.types()

    _run(case())


def test_timeout_without_reconnect_raises_explicit_error_not_empty_text():
    """注入 transport 又没有工厂：第二次 ask 必须抛明确错误，不许返回空文本。"""

    async def case():
        transport = QueueTransport()
        client = _client(transport=transport)
        await client.connect()

        with pytest.raises(omni.OmniTimeout):
            await client.ask(JPEG)

        # 迟到的上一轮事件已经躺在旧连接里：如果实现「不换连接直接再问」，
        # 这些事件就会被当成本轮答案。
        transport.push(
            {"type": "response.text.done", "text": SENTINEL_OLD},
            {"type": "response.done", "response": {"output": []}},
        )

        with pytest.raises(omni.OmniError_) as caught:
            await client.ask(JPEG)

        # 明确报「重连不了」，而不是又一次超时或空字符串成功。
        assert not isinstance(caught.value, omni.OmniTimeout)
        assert "transport_factory" in str(caught.value)
        assert client.broken is True
        assert transport.read_events == []  # 哨兵一条都没被读走

    _run(case())


def test_ask_total_budget_times_out_and_next_ask_reconnects():
    """ask 总预算到点：抛 OmniTimeout；下一次 ask 自动重连拿到新答案。"""

    async def case():
        drip = DripTransport(interval=0.01)
        fresh = omni.ScriptedTransport([
            {"type": "response.text.done", "text": "恢复后的答案"},
            {"type": "response.done", "response": {"output": []}},
        ])
        # 工厂只在超时后的 reconnect 被调用一次，必须给出干净的新连接。
        made = [fresh]
        cfg = omni.OmniConfig(first_token_timeout_s=1.0, step_timeout_s=1.0)
        client = _client(config=cfg, transport=drip, transport_factory=lambda: made.pop(0))
        await client.connect()

        with pytest.raises(omni.OmniTimeout) as caught:
            await client.ask(JPEG, max_total_s=0.2)

        exc = caught.value
        assert "总预算" in exc.phase
        assert exc.budget_s == pytest.approx(0.2)
        assert 0.15 <= exc.waited_s < 2.0
        assert client.broken is True
        assert client._inflight is False
        assert drip.closed is True
        reads_after_timeout = drip.reads

        # 总预算超时也不是「静默返回空文本」：下一轮换新会话，正常出答案。
        reply = await client.ask(JPEG, max_total_s=2.0)
        assert reply.text == "恢复后的答案"
        assert client.broken is False
        # 旧连接不再被读（迟到事件不会串到这一轮）。
        assert drip.reads == reads_after_timeout

    _run(case())


def test_reconnect_failure_raises_and_keeps_broken():
    """重连失败必须抛错，且 broken 保持 True（不假装恢复成功）。"""

    async def case():
        transport = QueueTransport()

        def factory():
            raise RuntimeError("模拟建链失败")

        client = _client(transport=transport, transport_factory=factory)
        await client.connect()

        with pytest.raises(omni.OmniTimeout):
            await client.ask(JPEG)

        with pytest.raises(omni.OmniError_) as caught:
            await client.ask(JPEG)

        assert "重连失败" in str(caught.value)
        assert client.broken is True
        assert transport.read_events == []

    _run(case())


def test_collect_zero_budget_keeps_unbounded_legacy_behavior():
    """<=0 表示不限（保持旧行为）：正常脚本照旧收完，不因默认预算被误杀。"""

    async def case():
        transport = omni.ScriptedTransport([
            {"type": "response.text.done", "text": "旧行为答案"},
            {"type": "response.done", "response": {"output": []}},
        ])
        client = _client(transport=transport)
        reply = await client.collect(first_token_timeout_s=0, step_timeout_s=0)
        assert reply.text == "旧行为答案"
        assert reply.raw_events == 2
        assert client.broken is False
        assert transport.closed is False

    _run(case())
