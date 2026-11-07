"""通用适配层与 profile 驱动工具面的测试（不碰真窗口/真游戏）。

覆盖用户要求的两件事：
1. 画面捕获/按键是**一个**通用函数、按 profile 自动适配，不在代码里写死某个游戏；
2. 键位绑定由 agent 学习后持久化到 profile，不是硬编码。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
ROOT = AGENT_DIR.parent
for item in (str(AGENT_DIR), str(ROOT)):
    if item not in sys.path:
        sys.path.insert(0, item)

import agent as A  # noqa: E402
import profiles as P  # noqa: E402


@pytest.fixture()
def profile(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    P.save_registry(
        {"version": 1, "games": {"demo": {"display_name": "Demo", "group_id": "demo", "aliases": ["演示"]}}},
        root,
    )
    prof = P.open_profile("demo", root)
    prof.save_config(
        {
            "adapter": {
                "capture": {"method": "printwindow"},
                "match": {"title": "DEMO", "exe": "demo.exe"},
                "kill_dialogs": [],
                "keys": {"up": "up", "confirm": ["z", "enter"]},
            }
        },
        )
    return prof


def test_adapter_reads_keymap_from_profile(profile):
    import adapter as adapter_mod

    ad = adapter_mod.ProfileAdapter(profile)
    assert ad.resolve("up") == ["up"]
    assert ad.resolve("confirm") == ["z", "enter"]
    # 物理键可直接用，不需要在 keymap 里额外登记
    assert ad.resolve("esc") == ["esc"]


def test_adapter_rejects_unknown_key_with_actionable_message(profile):
    import adapter as adapter_mod

    ad = adapter_mod.ProfileAdapter(profile)
    with pytest.raises(adapter_mod.AdapterError) as err:
        ad.resolve("dance")
    assert "keymap" in str(err.value)


def test_learned_key_persists_to_both_files(profile):
    result = P.apply_learned_key(profile, "menu", ["c"], confidence=0.8)
    assert result["action"] == "added"
    cfg = json.loads((profile.dir / "config.json").read_text(encoding="utf-8"))
    assert cfg["adapter"]["keys"]["menu"] == "c"
    bindings = {b.key: b for b in profile.load_keymap()}
    assert bindings["menu"].effect == "physical:c"
    assert bindings["menu"].source == "learned"
    # 再学一次是更新，不是重复追加
    again = P.apply_learned_key(profile, "menu", ["escape"], confidence=0.9)
    assert again["action"] == "updated"
    assert again["effect"] == "physical:escape"
    assert len([b for b in profile.load_keymap() if b.key == "menu"]) == 1


def test_learned_key_does_not_downgrade_manual_high_confidence(profile):
    profile.save_keymap([P.KeyBinding("confirm", "physical:z", confidence=1.0, source="manual")])
    out = P.apply_learned_key(profile, "confirm", ["enter"], confidence=0.4)
    assert out["action"] == "skipped"
    bindings = {b.key: b for b in profile.load_keymap()}
    assert bindings["confirm"].effect == "physical:z"


def test_learn_key_requires_physical(profile):
    with pytest.raises(P.ProfileError):
        P.apply_learned_key(profile, "confirm", [])


def test_tool_surface_follows_profile_not_hardcoded(profile):
    tools = A.BetterGITools(A.Session(root=profile.dir / "runs"), profile=profile)
    names = [spec["function"]["name"] for spec in tools.tool_specs()]
    # 双系统分工（2026-11-04 用户口径纠偏后）：
    # look 归慢系统——它自己就是多模态，直接给真图，不经快系统转述；
    # 动作**只有** lumine 一个入口——操作器（快系统 + 键鼠）替慢系统按键。
    # act / press / type / finish_intent / motor 都不许出现在慢系统工具面上：
    # 一旦慢系统能直接按键盘，快慢分层就退化成"慢模型自己当手"（REQ-2026-11-04 FR-3/FR-9）。
    # screen 工具已删除（把快系统当读屏器用是架构错误，回归就锁死在这里）。
    # learn_rule / restart_fast 是 FR-7 学习回传闭环的两个入口：
    # 慢系统把经验写成规则（learn_rule），再从零重建快系统验证知识还活着（restart_fast）。
    # 2026-11-04：慢系统工具面追加慢系统工具（02_schemas.md §9）；
    # 2026-11-05 用户要求删掉交棒工具（request_handoff / report_blocked），
    # 再加 run_script，原有 6 个的顺序不变，总数 16——分层靠禁名断言锁，不靠"正好 6 个"锁。
    assert names[:6] == [
        "look", "lumine", "learn_key", "learn_rule", "restart_fast", "end_test",
    ]
    assert len(names) == 16, f"慢系统工具面应为 6 老 + 10 新（含 run_script，不含交棒），实际 {len(names)} 个"
    for forbidden in ("screen", "act", "press", "type", "finish_intent", "motor"):
        assert forbidden not in names, f"{forbidden} 是操作器内部能力，不该回到慢系统工具面"
    lumine = [s for s in tools.tool_specs() if s["function"]["name"] == "lumine"][0]
    desc = lumine["function"]["description"]
    assert "意图" in desc and "操作器" in desc
    props = lumine["function"]["parameters"]["properties"]
    assert "text" in props and "done_when" in props


def _tools_with(profile, monkeypatch, dev: bool, hook):
    if dev:
        monkeypatch.setenv("BETTERAGI_DEV", "1")
    else:
        monkeypatch.delenv("BETTERAGI_DEV", raising=False)
    tools = A.BetterGITools(A.Session(root=profile.dir / "runs"), profile=profile)
    tools.stop_hook = hook
    return tools


def test_end_test_is_listed_on_every_surface(profile):
    # 模型必须能"看见"这个工具；能不能用是运行时判定的，不是藏起来。
    names = [spec["function"]["name"] for spec in A.BetterGITools(A.Session(root=profile.dir / "runs"), profile=profile).tool_specs()]
    assert "end_test" in names
    assert "end_test" in [s["function"]["name"] for s in A.TOOL_SPECS]


def test_end_test_refused_without_dev_flag(profile, monkeypatch):
    calls = []
    tools = _tools_with(profile, monkeypatch, dev=False, hook=lambda reason: calls.append(reason))
    result = tools.dispatch("end_test", {"reason": "试玩结束"})
    assert result.ok is False
    assert "开发环境" in result.text
    assert calls == [], "没有 dev 标志时绝不能真的停掉运行"


def test_end_test_refused_without_hook_even_in_dev(profile, monkeypatch):
    # 生产直播不会有任何东西挂钩子；dev 打开但没挂钩子也照样拒绝。
    tools = _tools_with(profile, monkeypatch, dev=True, hook=None)
    result = tools.dispatch("end_test", {"reason": "试玩结束"})
    assert result.ok is False
    assert "开发环境" in result.text


def test_end_test_stops_run_in_dev(profile, monkeypatch):
    calls = []
    tools = _tools_with(profile, monkeypatch, dev=True, hook=lambda reason: calls.append(reason))
    result = tools.dispatch("end_test", {"reason": "试玩结束"})
    assert result.ok is True
    assert calls == ["试玩结束"]
    assert json.loads(result.text)["ok"] is True


def test_end_test_accepts_event_object(profile, monkeypatch):
    import threading

    event = threading.Event()
    tools = _tools_with(profile, monkeypatch, dev=True, hook=event)
    assert tools.dispatch("end_test", {"reason": "结束"}).ok is True
    assert event.is_set()


def test_genshin_channel_keeps_scan_look_lumine():
    names = [spec["function"]["name"] for spec in A.BetterGITools(A.Session(root=Path("E:/BetterAGI/_diag/t"))).tool_specs()]
    assert names == ["scan", "look", "lumine", "end_test"]


def test_learn_key_tool_writes_through_dispatch(profile):
    tools = A.BetterGITools(A.Session(root=profile.dir / "runs"), profile=profile)
    result = tools.dispatch("learn_key", {"name": "cancel", "physical": ["x"], "confidence": 0.7})
    assert result.ok
    payload = json.loads(result.text)
    assert payload["key"] == "cancel" and payload["confidence"] == 0.7
    assert "cancel" in tools.known_keys()


def test_act_error_surfaces_instead_of_silently_pressing(profile):
    tools = A.BetterGITools(A.Session(root=profile.dir / "runs"), profile=profile)
    with pytest.raises(Exception):
        tools.generic_act(["not-a-key"], wait=0)

def test_operator_traces_the_fast_lane_through_the_executor(profile, monkeypatch):
    """操作器跑在快车道上：每一次决策都要能被宿主"看见"，否则界面上什么都画不出来。"""
    from types import SimpleNamespace

    tools = A.BetterGITools(A.Session(root=profile.dir / "runs"), profile=profile)
    seen = {"decisions": 0}

    class FakeMotor:
        def health(self, timeout=3):
            return {"available": True}

        def decide(self, frame_url, instruction="", game="", system_prompt=None):
            seen["decisions"] += 1
            # 手：第一次交一组 6 步操作（含真按键），之后交空动作让执行器按"没动作"收手。
            steps = [[], ["confirm"], [], [], [], []] if seen["decisions"] == 1 else [[], [], [], [], [], []]
            return {"steps": steps, "mouse": {"x": 1, "y": 0, "z": 0},
                    "raw": "1 0 0 ; ; confirm ; ; ; ;", "decide_ms": 90}

    class FakeAdapter:
        def grab(self):
            return {"data": b"frame-bytes", "source": "fake"}

        def type_text(self, text, delay_ms=45.0):
            return {"typed": text}

        def play_steps(self, steps, step_ms=33):
            pressed = [k for step in steps for k in step]
            return {"pressed": pressed, "skipped_keys": [], "steps": steps}

    monkeypatch.setattr(
        A, "_adapter_mod", lambda: SimpleNamespace(ProfileAdapter=lambda prof: FakeAdapter())
    )
    monkeypatch.setattr(tools, "_fast_stream", lambda: (None, "测试里没有嘴"))

    import motor as motor_mod

    monkeypatch.setattr(motor_mod, "build_motor", lambda *a, **k: FakeMotor())
    events = []
    tools.trace = lambda event, data: events.append((event, data))
    tools._operator("确认掉这一屏", max_steps=1)

    names = [name for name, _ in events]
    assert names[0] == "operator_start"
    assert names[-1] == "operator_end"
    actions = [data for name, data in events if name == "operator_action"]
    assert actions, "每一次决策都该有一条 operator_action"
    assert actions[0]["keys"] == ["confirm"] and actions[0]["pressed"] == ["confirm"]
    assert seen["decisions"] == len(actions)


def test_trace_hook_failures_never_break_the_run(profile):
    """追踪钩子是外部挂进来的，坏掉、没挂、挂错了都不许影响游戏。"""
    tools = A.BetterGITools(A.Session(root=profile.dir / "runs"), profile=profile)
    tools.trace = None
    tools._trace("operator_start", intent="x")
    tools.trace = "not callable"
    tools._trace("operator_start", intent="x")

    def boom(event, data):
        raise RuntimeError("trace exploded")

    tools.trace = boom
    tools._trace("operator_step", i=0)  # 不抛就是对的
