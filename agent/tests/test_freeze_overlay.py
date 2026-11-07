# -*- coding: utf-8 -*-
'''冻结贴图（01 §6 / 03 §C.9）：渲染、字体缺失、tap、失败兜底、stats、接线。

全部离线：只跑 Pillow 与假件，不碰网络 / GPU / 游戏窗口。
'''
from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from PIL import Image

import adapter as adapter_mod
import danmaku_feed
import fast_session
import freeze_overlay
import memory as memory_mod
import motor as motor_mod
import orchestrator as orchestrator_mod
import run_stream
import slow_planner

from test_integration_loop import FakeAdapter, FakeMotor, FakeNarrator, FakeSlowClient, PLAN_TEXT


def make_jpeg(width: int = 321, height: int = 203) -> bytes:
    image = Image.new('RGB', (width, height), (38, 64, 96))
    for x in range(0, width, 17):
        for y in range(0, height, 13):
            image.putpixel((x, y), (200, 180, 120))
    out = io.BytesIO()
    image.save(out, format='JPEG', quality=90)
    return out.getvalue()


def decode(jpeg: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(jpeg))
    image.load()
    return image


# ---- render_overlay ----------------------------------------------------

def test_render_overlay_returns_valid_jpeg_with_same_size():
    source = make_jpeg(321, 203)

    painted = freeze_overlay.render_overlay(source, freeze_overlay.DEFAULT_WARN_TEXT,
                                            font_size=30)

    assert isinstance(painted, bytes) and painted != source
    assert painted[:2] == b'\xff\xd8' and painted[-2:] == b'\xff\xd9'
    image = decode(painted)
    assert image.format == 'JPEG'
    assert image.size == (321, 203)
    rgb = image.convert('RGB')
    # 中央黑底条里必须有白色文字像素（说明中文真的画出来了，不是空白条）。
    band = [rgb.getpixel((x, y)) for y in range(80, 123) for x in range(0, 321, 3)]
    assert any(min(pixel) > 230 for pixel in band)
    assert any(max(pixel) < 60 for pixel in band)


def test_render_overlay_raises_clear_error_when_font_missing(monkeypatch):
    monkeypatch.setattr(freeze_overlay, 'FONT_CANDIDATES', ('Z:/nope/not-a-font.ttf',))

    with pytest.raises(freeze_overlay.FreezeOverlayError) as caught:
        freeze_overlay.render_overlay(make_jpeg(120, 90))
    assert '字体' in str(caught.value)


def test_render_overlay_rejects_non_jpeg_input():
    with pytest.raises(freeze_overlay.FreezeOverlayError):
        freeze_overlay.render_overlay(b'not a jpeg at all')
    with pytest.raises(freeze_overlay.FreezeOverlayError):
        freeze_overlay.render_overlay(b'')


# ---- FreezePainter -----------------------------------------------------

def test_painter_forwards_rendered_bytes_to_tap():
    seen = []
    painter = freeze_overlay.FreezePainter(text='大肥鱼CPU烧了', tap=seen.append)
    source = make_jpeg(320, 180)

    painted = painter.paint(source)

    assert painted != source
    assert seen == [painted]
    assert painter.stats() == {'painted': 1, 'errors': 0, 'last_error': ''}


def test_painter_swallows_draw_error_and_returns_input_bytes():
    painter = freeze_overlay.FreezePainter(text='大肥鱼CPU烧了')
    broken = b'\xff\xd8 this is not a real jpeg \xff\xd9'

    painted = painter.paint(broken)

    assert painted == broken
    stats = painter.stats()
    assert stats['painted'] == 0 and stats['errors'] == 1
    assert 'FreezeOverlayError' in stats['last_error']


def test_painter_stats_counts_successes_and_failures():
    painter = freeze_overlay.FreezePainter(text='大肥鱼CPU烧了')
    source = make_jpeg()

    assert painter.paint(source) != source
    assert painter.paint(b'') == b''
    assert painter.paint(source) != source

    assert painter.stats() == {'painted': 2, 'errors': 1,
                              'last_error': 'FreezeOverlayError: 冻结贴图收到空帧'}


# ---- orchestrator 接线 -------------------------------------------------

class FakeClock:
    '''每次取 monotonic 前进 step 秒：plan() 的冻结窗口可被精确放大到阈值以上。'''

    def __init__(self, step: float = 0.0, base: float = 100.0) -> None:
        self.step = step
        self.now = base

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        value = self.now
        self.now += self.step
        return value


class ImmediatePlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, report_summary, **kwargs):
        self.calls += 1
        return slow_planner.SlowPlan(goal='往东走到村口', success='对话框说完',
                                     avoid='不要出村')


def build_orchestrator(tmp_path, planner=None):
    return orchestrator_mod.Orchestrator(
        root=tmp_path, session_id='fz', game='minecraft', save_id='s1',
        planner=planner or ImmediatePlanner(),
    )


def test_orchestrator_warns_to_paint_when_freeze_exceeds_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator_mod, 'time', FakeClock(step=0.5))
    painter = freeze_overlay.FreezePainter(text='大肥鱼CPU烧了')
    orch = build_orchestrator(tmp_path)
    assert orch.set_freeze_painter(painter) is painter
    assert orch.freeze_overlay is painter

    plan = orch.plan('村口井边', memory_hits=[])

    assert plan.goal.startswith('往东走')
    assert len(orch.warnings) == 1
    warning = orch.warnings[0]
    assert '本段冻结' in warning and '请上层贴图' in warning
    assert '大肥鱼CPU烧了' in warning
    assert '1500ms' in warning  # plan 内共 4 次取时钟，每次前进 0.5s


def test_orchestrator_stays_quiet_when_fast_or_without_painter(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator_mod, 'time', FakeClock(step=0.0))
    orch = build_orchestrator(tmp_path)
    orch.set_freeze_painter(freeze_overlay.FreezePainter())
    orch.plan('村口井边', memory_hits=[])
    assert orch.warnings == []

    # 冻结够久但没挂贴图器：编排层只提醒上层，不自己画。
    monkeypatch.setattr(orchestrator_mod, 'time', FakeClock(step=5.0))
    orch2 = build_orchestrator(tmp_path)
    orch2.plan('村口井边', memory_hits=[])
    assert orch2.warnings == []

    with pytest.raises(orchestrator_mod.OrchestratorError):
        orch.set_freeze_painter(object())


# ---- run_stream.assemble 接线 -----------------------------------------

class FakeProfile:
    def __init__(self, root: Path) -> None:
        self.dir = root
        self.game_id = 'minecraft'
        self.display_name = '我的世界'
        self.group_id = 'minecraft'

    def load_config(self):
        return {}


class FakeSession(FakeNarrator):
    def start(self):
        pass

    def close_intent(self):
        pass

    def stats(self):
        return {'session_opens': 0, 'reconnects': 0, 'degraded': False}


class MemoryStub:
    def sanitize_group_id(self, value):
        return str(value or '')

    def search(self, *args, **kwargs):
        return []

    def add_episode(self, episode):
        return 'e1'


class FeedStub:
    def fetch(self, since_ms, limit=50):
        return type('S', (), {'items': []})()

    def health(self):
        return {'ok': True, 'items': 0}


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    profile = FakeProfile(tmp_path / 'profiles' / 'minecraft')
    profile.dir.mkdir(parents=True)
    monkeypatch.setattr(adapter_mod, 'ProfileAdapter', lambda profile: FakeAdapter())
    monkeypatch.setattr(motor_mod, 'build_motor', lambda: FakeMotor())
    monkeypatch.setattr(fast_session, 'build_intent_session', lambda **kwargs: FakeSession())
    monkeypatch.setattr(memory_mod, 'build_memory', lambda profile, **kwargs: MemoryStub())
    monkeypatch.setattr(
        slow_planner, 'build_slow_planner',
        lambda client=None, **kwargs: slow_planner.SlowPlanner(
            FakeSlowClient(PLAN_TEXT), budget_ms=2000))
    monkeypatch.setattr(danmaku_feed, 'build_feed', lambda **kwargs: FeedStub())
    return profile


def test_assemble_creates_painter_and_attaches_it(tmp_path, wired):
    parts = run_stream.assemble(wired, root=tmp_path / 'runs', session_id='fz1',
                                freeze_text='大肥鱼CPU烧了')

    freeze = parts['freeze']
    assert isinstance(freeze, freeze_overlay.FreezePainter)
    assert freeze.text == '大肥鱼CPU烧了'
    assert parts['orchestrator'].freeze_overlay is freeze


def test_assemble_without_freeze_text_leaves_overlay_off(tmp_path, wired):
    parts = run_stream.assemble(wired, root=tmp_path / 'runs', session_id='fz2')

    assert parts['freeze'] is None
    assert parts['orchestrator'].freeze_overlay is None
