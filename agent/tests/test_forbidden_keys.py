"""平台安全红线：全屏键 F4(0x73) 永不放行。

闸口在 ut_harness 的发送层（_key / post_vk / press_vks），所以这里直接测那个层：
绕过 adapter 也好、模型硬要也好，都过不去。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for item in (str(ROOT), str(ROOT / "tools")):
    if item not in sys.path:
        sys.path.insert(0, item)

import ut_harness as H  # noqa: E402


def test_f4_is_forbidden_by_default():
    # 0x73 = F4 = Undertale 全屏。直播时按下去就是黑屏事故。
    assert H.is_forbidden(0x73)
    assert 0x73 in H.forbidden_vks()


def test_normal_keys_pass():
    for vk in (0x0D, 0x20, 0x5A, 0x26, 0x28, 0x25, 0x27):
        assert not H.is_forbidden(vk)


def test_forbidden_vks_can_be_extended_by_env(monkeypatch):
    monkeypatch.setenv("BETTERAGI_FORBIDDEN_VKS", "0x74,117")
    assert {0x73, 0x74, 117} <= H.forbidden_vks()


def test_forbidden_vks_ignores_junk(monkeypatch):
    monkeypatch.setenv("BETTERAGI_FORBIDDEN_VKS", " ,nope, 0x74 ,")
    assert H.forbidden_vks() == {0x73, 0x74}


def test_unknown_vk_is_treated_as_forbidden():
    # 认不出的值宁可拦下：安全默认是"不发"。
    assert H.is_forbidden("nonsense")


def test_press_vks_drops_forbidden_key(monkeypatch):
    """一次按 4 个键时，禁键被剔除，其余照发——不是整批拒绝。"""
    sent = []

    class FakeU32:
        @staticmethod
        def MapVirtualKeyW(vk, flag):
            return vk

        @staticmethod
        def SendInput(count, inputs, size):
            sent.append(count)
            return count

    monkeypatch.setattr(H, "u32", FakeU32)
    out = H.press_vks([0x5A, 0x73, 0x0D], hold_ms=0)
    assert 0x73 not in out
    assert out == [0x5A, 0x0D]
    # 一次 down + 一次 up，各 2 个键
    assert sent == [2, 2]


def test_press_vks_all_forbidden_sends_nothing(monkeypatch):
    sent = []

    class FakeU32:
        @staticmethod
        def MapVirtualKeyW(vk, flag):
            return vk

        @staticmethod
        def SendInput(count, inputs, size):
            sent.append(count)
            return count

    monkeypatch.setattr(H, "u32", FakeU32)
    assert H.press_vks([0x73], hold_ms=0) == []
    assert sent == []


def test_post_vk_refuses_forbidden(monkeypatch):
    calls = []

    class FakeU32:
        @staticmethod
        def MapVirtualKeyW(vk, flag):
            return vk

        @staticmethod
        def PostMessageW(*a):
            calls.append(a)
            return True

    monkeypatch.setattr(H, "u32", FakeU32)
    assert H.post_vk(1234, 0x73) is False
    assert calls == []
    assert H.post_vk(1234, 0x0D) is True