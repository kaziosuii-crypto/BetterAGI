"""预检补项：游戏窗口 / Omni 凭证 / 记忆连通 / 编码器（2026-11-04）。

设计文档写的是六项，代码里原只有四项；这几项的共同原则：**没查就标跳过，
不能伪装成通过**——观众不会因为「没检查」就原谅一次黑屏。
"""

from __future__ import annotations

from typing import Any, Dict, List

import preflight


class _FakeMemory:
    name = 'local-json'
    group_id = 'genshin_impact'

    def __init__(self, hits: List[Any] = None, error: Exception = None,
                 layers_ok: bool = True):
        self._hits = hits or []
        self._error = error
        self._layers_ok = layers_ok
        self.calls: List[Dict[str, Any]] = []

    def search(self, query: str, *args: Any, **kwargs: Any) -> List[Any]:
        self.calls.append({'query': query, 'kwargs': kwargs})
        if self._error is not None:
            raise self._error
        if not self._layers_ok and 'layers' in kwargs:
            raise TypeError('unexpected keyword argument layers')
        return self._hits


def test_window_without_probe_is_skipped_not_passed() -> None:
    out = preflight.check_window(None)
    assert out.skipped is True
    assert out.ok is True
    assert '跳过' in out.detail
    report = preflight.PreflightReport(results=[out])
    assert report.ok is True
    assert report.failures == []
    assert '跳过' in report.markdown()


def test_window_probe_failure_blocks_preflight() -> None:
    def boom() -> None:
        raise RuntimeError('窗口被最小化')

    out = preflight.check_window(boom)
    assert out.ok is False
    assert out.skipped is False
    assert 'RuntimeError' in out.detail
    assert '最小化' in out.detail
    assert '前台' in out.hint


def test_window_probe_accepts_frame_dict() -> None:
    out = preflight.check_window(lambda: {'data': b'jpg', 'w': 1280, 'h': 736})
    assert out.ok is True
    assert '1280x736' in out.detail


def test_omni_without_key_fails_with_hint(monkeypatch) -> None:
    import fast_system

    monkeypatch.setattr(fast_system, 'resolve_dashscope_key', lambda: '')
    out = preflight.check_omni()
    assert out.ok is False
    assert 'DASHSCOPE_API_KEY' in out.hint


def test_omni_with_key_masks_it(monkeypatch) -> None:
    import fast_system

    monkeypatch.setattr(fast_system, 'resolve_dashscope_key', lambda: 'sk-0123456789abcdef')
    out = preflight.check_omni()
    assert out.ok is True
    assert 'sk-0' in out.detail
    assert '0123456789abcdef' not in out.detail
    assert '实连验证' in out.detail


def test_memory_missing_backend_fails() -> None:
    out = preflight.check_memory(None)
    assert out.ok is False
    assert '记忆' in out.detail


def test_memory_first_stream_is_fine() -> None:
    memory = _FakeMemory(hits=[])
    out = preflight.check_memory(memory)
    assert out.ok is True
    assert '第一次' not in out.detail
    assert '还没有上一场记录' in out.detail
    assert memory.calls[0]['kwargs']['layers'] == ['episodic']


def test_memory_last_episode_readable() -> None:
    out = preflight.check_memory(_FakeMemory(hits=[object(), object()]))
    assert out.ok is True
    assert '可读（2 条）' in out.detail


def test_memory_old_search_signature_still_works() -> None:
    memory = _FakeMemory(hits=[object()], layers_ok=False)
    out = preflight.check_memory(memory)
    assert out.ok is True
    assert len(memory.calls) == 2
    assert 'layers' not in memory.calls[1]['kwargs']


def test_memory_error_names_backend() -> None:
    out = preflight.check_memory(_FakeMemory(error=ConnectionError('bolt refused')))
    assert out.ok is False
    assert 'local-json' in out.detail
    assert 'bolt refused' in out.detail
    assert 'GRAPHITI_URI' in out.hint


def test_encoder_present(monkeypatch) -> None:
    import record

    monkeypatch.setattr(record, 'ffmpeg_path', lambda: 'C:/ffmpeg/bin/ffmpeg.exe')
    out = preflight.check_encoder()
    assert out.ok is True
    assert 'ffmpeg.exe' in out.detail


def test_encoder_missing_points_at_env_var(monkeypatch) -> None:
    import record

    def boom() -> str:
        raise record.RecordError('找不到 ffmpeg')

    monkeypatch.setattr(record, 'ffmpeg_path', boom)
    out = preflight.check_encoder()
    assert out.ok is False
    assert 'BETTERAGI_FFMPEG' in out.hint
