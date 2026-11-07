from types import SimpleNamespace

import adapter


def test_focus_failure_releases_held_and_never_presses(monkeypatch):
    calls = []
    subject = object.__new__(adapter.ProfileAdapter)
    subject.profile = SimpleNamespace(game_id="test")
    subject._held_vks = [65]
    monkeypatch.setattr(subject, "main_hwnd", lambda: 10)
    monkeypatch.setattr(subject, "ensure_input", lambda: {"safe": True, "after": {"layout": "test"}})
    monkeypatch.setattr(adapter.ut_harness.u32, "GetForegroundWindow", lambda: 20)
    monkeypatch.setattr(adapter.ut_harness, "sethwnd_foreground", lambda hwnd: False)
    monkeypatch.setattr(adapter.ut_harness, "key_down_vks", lambda keys: calls.append(("down", keys)))
    monkeypatch.setattr(adapter.ut_harness, "key_up_vks", lambda keys: calls.append(("up", list(keys))))
    result = subject.play_steps([["A"], ["B"]], keep_held=True)
    assert calls == [("up", [65])]
    assert result["pressed"] == []
    assert result["step_results"][0]["skipped_reason"] == "no_window_focus"
    assert subject._held_vks == []
