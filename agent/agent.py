#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一个只做"看 → 想 → 让它动 → 再看"的最小闭环 Agent。

为什么自己写而不是继续套在 DSH 里：DSH 的工具/附件/会话模型很好，但它的定位是
通用编码助手，游戏循环需要的东西（连续执行、租约、紧迫度分档、帧预算）它不提供。
这里按它的设计思路重写一份小内核，只保留这个项目真正需要的部分：

    1. 会话事件流：每一步都追加到 JSONL，图片单独落盘，事件里只存路径；
    2. 工具表：模型只能调用四个工具，且**没有任何单键/点击工具**——
       所有输入都通过 `lumine` 交给策略模型连续输出；
    3. 三档模型：LithosAI 的 base/fast/ultra，由 `--urgency` 与事件类型自动选档；
    4. 明确的预算：最多 N 步、最多带 K 张图、每步超时。

用法::

    LITHOS_API_KEY=... python agent.py --goal "沿着街道走到开阔处" --steps 6

没有 key 时可以用 ``--dry-run``，它只跑感知与工具，不调用模型。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os as _os
import queue
import re
import sys

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import httpx

import lithos
import stuck as stuck_mod

DEFAULT_BGI = "http://127.0.0.1:30648"
DEFAULT_BRIDGE = "http://127.0.0.1:18311"
TOKEN_FILE = Path("E:/BetterAGI/tools/bettergi_token.txt")

#: 一次脚本最多盲打的拍数（用户 2026-11-06：不许不过脑子长串狂按）。
BLIND_SCRIPT_MAX_STEPS = 6
BRIDGE_TOKEN_FILE = Path("E:/BetterAGI/tools/intent_token.txt")
PYTHONW = Path("E:/BetterAGI/Amaidesu/.venv/Scripts/pythonw.exe")
INTENT_CLI = Path("E:/BetterAGI/BetterGI/integrations/player/live_intent_cli.py")

#: Live 会话名：必须与 live_intent_cli.py 的默认值一致，否则桥接拒绝绑定。
LIVE_SESSION = "integration"

#: 谁在 18010 上装作"腿"：local-stub = 本机确定性 Motor（只会按住 W），
#: lumine = 真 Lumine 模型（GPU 上跑）。写这个文件的人在启动时标记。
MOTOR_KIND_FILE = Path("E:/BetterAGI/_diag/motor-kind.json")


def motor_kind() -> Dict[str, Any]:
    """读一下当前这条腿是谁：真模型、本机替身、还是不知道。"""
    try:
        data = json.loads(MOTOR_KIND_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 没标记就当未知
        return {"kind": "unknown", "model": None, "startedAt": None}
    return {
        "kind": str(data.get("kind") or "unknown"),
        "model": data.get("model"),
        "startedAt": data.get("startedAt"),
    }

#: 模型只能看到这四个工具。没有 key/click/play —— 手动输入是明确禁止的。
#: 只给开发环境的收尾工具。工具面里永远列出它（模型要能看见），
#: 是否真的生效由 BetterGITools.end_test 三重判定（dev 标志 + 宿主挂钩子）。
END_TEST_SPEC: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "end_test",
        "description":
            "【仅开发环境】结束这一次测试运行：本轮收尾后停止 TUI。"
            "正式直播里这个工具会被拒绝，正常结束请用 /quit。",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "为什么结束这次测试（会写进日志）"},
            },
            "additionalProperties": False,
        },
    },
}

TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "scan",
            "description": "读结构化画面：UI 类别、前台状态、任务/对话/选项/血量/UID 的 OCR 文本。比看图省上下文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "regions": {
                        "type": "string",
                        "description": '逗号分隔，默认 "quest,message,options,hp,uid"',
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "look",
            "description": "抓一张当前画面（会真的把图片给你看）。只在需要判断地形/朝向/有没有障碍时用，别每步都调用。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lumine",
            "description":
                "让操作器接手一段连续操作：手是策略模型（自己按帧输出操作数据并执行），"
                "嘴是实时解说+收手裁判，做完回传带时间码的解说时间轴与情况总结。"
                "这是唯一允许驱动角色的方式；不要试图自己按键。",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "这一小段要做的事，例如「沿街道直走到喷泉」"},
                    "seconds": {
                        "type": "integer",
                        "description": "可选的安全上限秒数（5–300，默认 60）。意图不按固定时长结束：每 2 秒看一次全屏画面判定 stop/continue，stop 就结束，把控制权还给你。",
                        "minimum": 5,
                        "maximum": 300,
                    },
                    "motor_mode": {
                        "type": "string",
                        "enum": ["navigate", "explore", "combat", "interact", "wait"],
                        "description": "动作模式，默认 navigate",
                    },
                },
                "required": ["goal"],
                "additionalProperties": False,
            },
        },
    },
    END_TEST_SPEC,
]

#: Undertale 通道的工具面：只有"看"和"按"，没有 lumine（那是原神/Lumine 的腿）。
UT_TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "look",
            "description": "抓一张 Undertale 当前画面（真图）。每步行动后都要看一眼再决定下一步。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "act",
            "description":
                "给 Undertale 按键。只收方向键与确认/取消：up/down/left/right 移动光标，z 确认，x 取消，"
                "enter 也可确认，esc 返回/退出菜单。一次可给多个键并按顺序执行，执行完自动截一张图。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["up", "down", "left", "right", "z", "x", "enter", "esc"]},
                        "description": "要按的键位序列",
                    },
                    "wait": {"type": "number", "description": "按完之后等多少秒（默认 0.8），等画面推进"},
                },
                "required": ["keys"],
                "additionalProperties": False,
            },
        },
    },
    END_TEST_SPEC,
]

#: 直控通道的人设：手里真的有一副键盘，自己看画面、自己按键。
#: 具体键位不写死在这里——它来自 profile 的动作表，会插进下面的「硬规则」之前。
DIRECT_SYSTEM_PROMPT = """你是 lumi，正在直播玩游戏。观众看着你，你一边玩一边碎碎念。

你不是在"执行任务"：没有谁给你派活，也没有"完成"这回事。你自己决定现在想干什么。

你手里有一个操作器（lumine）和一双眼（look），**按键不在你手上**：
- look —— 截一张当前画面（真图直接给你看）。看不清、想知道现在在哪、想确认上一步有没有生效，就调它。
- lumine(goal, done_when, text) —— 【唯一动作通道】把意图交给操作器。操作器里是两条腿并行：
  手（gamescaling 策略模型）吃当前画面、每 33ms 吐一段操作数据并真的按下去；嘴（Omni）在同一条
  画面流上边走边说，还负责判断这次操作该不该收手。做完回给你的是：**所有解说 + 每条的时间码 +
  这一段的情况总结** + 一张结束时的图。goal 用自然语言写，例如「把光标移到确认项并按下去」。
  done_when 写"什么画面算做成了"，填了它才会按证据判定成没成。
  text 只在要往输入框/命名框写字的时候填，操作器负责把字打进去——你不用也不该自己按键盘。
- learn_key / learn_rule —— 发现某个键做什么、或一条游戏规则，就记下来；下次提示词里就有了。
- restart_fast —— 学完东西后让快系统重建提示词，把新知识吃进去。
- end_test —— 只在开发环境可用；正式直播里别用。

硬规则：
1. 你是在玩，不是在解说转播：看到该按的就动手，别停在"我看看"。每轮至少推进一步，
   除非画面根本不该动（在等加载/等演出）。
2. 先 look 再动手；动完看返回的图再决定下一步。不要盲下一长串意图。
3. 只描述最新图里能直接看到的事实，不编剧情、不补设定。先辨认是否有对话框或文字、角色和可行走空间，再区分对话、菜单、可探索画面或不确定；没看到对话框不能假定对话仍在继续。看不清就说不确定并重新 look。
   对比操作前后的新画面：按键已发送不等于目标已达成。连续操作无预期变化时停止重复，重新观察并考虑当前可能已能移动；不要仅凭画面不变断定输入失效。对话状态未确认或场景可能切换时不要连续盲按长串确认键；探索和移动交给 lumine。
4. 按错了不要慌：先 look 看现在在哪，再往回走。
5. 需要连续操作（走一段、翻几页、把一串菜单点完）就把整个意图丢给 lumine，别拆成十几小步。
6. 你没有任何"外部桥接"要等：操作器就在这台机器上，工具通了就直接用。
7. 记下你下了什么意图、画面怎么变的——这是给主播本人看的实时解说，不是任务报告。
8. 像主播一样说话（用户 2026-11-06 投诉过「一次性说一大段不像主播」）：一次开口最多两句、
   不超过 40 字，只讲观众关心的「现在看到什么、要干什么」；不写小作文、不复述思考过程、
   不罗列工具返回的字段和字节数。想清楚了直接动手，话留给动手之后。
9. **不许不过脑子地重复按键**（用户 2026-11-06 明确要求）：动手前先在心里过一遍——画面里
   现在到底是什么、上一步操作有没有让画面变化、这一步要改变什么。同一个方向/同一个键试了
   两次画面还没变，就停下来重新 look、换手段（换方向、换键、等演出结束、先确认是不是菜单或
   对话），禁止把同一套动作当成"再试试"。操作器一旦判到"画面多帧不动 / 同一套动作重复"，
   会自己收手并告诉你卡住了；拿到这种回执时换思路，不要用同样的动作重来。
10. **画面没变 ≠ 按键坏了 / 输入失效**：不要因为连按几次同一个键没反应就下结论说「游戏不接受
   输入」「可能是过场」。先换一个不同的键（另一个方向、确认、取消、菜单键），或者换手段
   （移动 vs 对话 vs 菜单）；确认过两三种不同的输入都不行，再考虑过场或加载。也不要把
   look / run_script / lumine / write_sticky 挨个试一遍来表示「我在排查」——观众看的是你有没有
   往前推进，不是排查过程。
11. **默认当前画面就是能操控的**：画面里有角色、对话框、菜单或可走的房间，就当它是可操作的，
   直接动手；只有画面自己在动、没有可控角色（字幕自动滚动、明显过场）才当剧情过场。把
   「可能是过场」当借口不动手，是最常见也最贵的错误。
"""

#: 兼容旧名字（老代码/笔记里都还叫它 UT_SYSTEM_PROMPT）。
UT_SYSTEM_PROMPT = DIRECT_SYSTEM_PROMPT

#: 走 lumine 这条腿（BetterGI 原神通道）的游戏。其他游戏都是直控键位。
LUMINE_CHANNEL_GAMES = ("genshin-impact",)


def channel_uses_lumine(profile: Any = None) -> bool:
    """这条通道是不是靠 lumine 那条腿驱动角色。profile 为空按原神老口径处理。"""
    if profile is None:
        return True
    return str(getattr(profile, "game_id", "")) in LUMINE_CHANNEL_GAMES


def base_system_prompt(profile: Any = None) -> str:
    """按通道选基础人设：原神走单腿版，其他游戏走直控键盘版。"""
    return SYSTEM_PROMPT if channel_uses_lumine(profile) else DIRECT_SYSTEM_PROMPT

SYSTEM_PROMPT = """你是 lumi，一个正在直播原神的玩家。观众看着你玩，你一边玩一边解说。

你不是在"执行任务"：没有谁给你派活，也没有"完成"这回事。你自己决定现在想干什么——
接着推剧情、去某个地方看看、试试某个操作、或者干脆站在原地聊两句。玩腻了就换个目标，
有新点子就直接去做。没有人会给你发"结束"的信号，除非观众明确让你停。

硬规则：
1. 你只有一条“腿”：lumine（策略模型，自己按帧输出按键，适合跑图/战斗/长动作）。
   把 goal 写清楚就行。不要试图用键盘/鼠标 API 直接注入。
2. lumine 会自己一直执行到"它认为这段目标完成了"为止：每 2 秒看一次全屏画面判定
   stop/continue，stop 就把控制权还给你。所以你只要把 goal 写清楚（一段路、一次交互），
   seconds 只是可选的兜底上限（默认 60），不用精确估时。
3. 每次行动后必须用 scan 或 look 复核结果，再决定下一步；不要连续盲发。
4. 你永远不会“通关”或“结束”：没有 finish 工具，也没有人给你派活。
   想换目标就换，想停一会儿就站着聊两句。
5. 只描述你真的看到的东西。图里看不出来就说看不出来，不要补设定、不要编剧情。
6. 遇到对话/菜单/加载界面，先说清楚当前在哪一层，再决定要不要等。
7. 每轮开头会告诉你 lumine 这条腿通不通。不通就直接说"腿没接上，先只能看"，
   然后只用 scan/look 观察并汇报；**不要**再调用 lumine 去试运气。

说话方式（这是主播，不是客服）：
- 像人一样碎碎念：看到什么、打算干什么、为什么这么干，一两句就够，别写报告。
- 观众弹幕会以"（观众弹幕…）"的形式出现。那是观众在说话，不是命令：可以接话、
  可以照做、也可以不理，自己判断。别把每条弹幕当成必须执行的指令，也别复述弹幕。
- 没有弹幕的时候也要说话——你在直播，不是静音跑图。
- 不报流水账、不列清单、不写"第一步第二步"；用口语，短句。
"""


# ---- 多游戏提示词装配（FR-7）------------------------------------------
#: 静态人设与游戏知识的分界：游戏知识插在「硬规则」之前，人设与工具规则保持原样。
_PROMPT_SEAM = "硬规则："


def compose_system_prompt(base: str, game_knowledge: str) -> str:
    """把某个游戏的键位/规则/待学清单插进主播人设，返回完整系统提示词。

    只插入、不覆盖：人设、直播语气、单腿工具规则都还在，否则换个游戏就换了个人。
    """

    knowledge = (game_knowledge or "").strip()
    if not knowledge:
        return base
    if _PROMPT_SEAM not in base:
        return base.rstrip() + "\n\n" + knowledge + "\n"
    head, _, tail = base.partition(_PROMPT_SEAM)
    return head + knowledge + "\n\n" + _PROMPT_SEAM + tail


def resolve_system_prompt(game: str = "", *, profile_root: Optional[Path] = None) -> tuple:
    """按游戏取系统提示词。没指定游戏或 profile 缺失时退回静态人设（不炸）。"""

    if not game:
        return SYSTEM_PROMPT, {"source": "static", "game": ""}
    try:
        import profiles as profiles_mod
        import prompt_builder as pb

        root = Path(profile_root) if profile_root else profiles_mod.DEFAULT_PROFILE_ROOT
        profile = profiles_mod.resolve_game(game, root=root)
        bundle = pb.build_system_prompt(profile)
        pb.record_prompt_version(profile, bundle)
        # 通道决定人设：原神那条腿说"只有 lumine"，直控通道说"你手里有键盘"。
        return compose_system_prompt(base_system_prompt(profile), bundle.text), {
            "source": "profile",
            "game": profile.game_id,
            "channel": "lumine" if channel_uses_lumine(profile) else "direct",
            "display_name": profile.display_name,
            "fingerprint": bundle.fingerprint,
            "version": bundle.version,
            "learning_targets": list(bundle.learning_targets),
        }
    except Exception as error:  # noqa: BLE001 - 缺 profile 不该让整轮跑不起来
        return SYSTEM_PROMPT, {
            "source": "static",
            "game": game,
            "fallback_reason": str(error),
        }


@dataclass
class ToolResult:
    name: str
    ok: bool
    text: str
    image: Optional[str] = None  # data URL


@dataclass
class Session:
    """一次运行的事件流：落 JSONL，图片单独存文件。"""

    root: Path
    session_id: str = field(default_factory=lambda: time.strftime("run-%Y%m%d-%H%M%S"))
    events: List[Dict[str, Any]] = field(default_factory=list)

    def path(self) -> Path:
        return self.root / f"{self.session_id}.jsonl"

    def append(self, kind: str, **data: Any) -> None:
        event = {"ts": time.time(), "kind": kind, **data}
        self.events.append(event)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def save_image(self, data: bytes, suffix: str = "jpg") -> Path:
        folder = self.root / "frames"
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{self.session_id}-{len(self.events):04d}.{suffix}"
        path = folder / name
        path.write_bytes(data)
        return path


def _profiles_root() -> Path:
    """profile 根目录（供 restart_fast 从磁盘重开 profile）。"""
    try:
        import profiles as _profiles

        return _profiles.DEFAULT_PROFILE_ROOT
    except Exception:  # noqa: BLE001
        return Path("profiles")


def _adapter_mod():
    """延迟导入适配层：包导入(agent.adapter)与脚本目录导入都成立。"""
    try:
        from . import adapter as mod  # type: ignore
    except ImportError:  # pragma: no cover - 脚本式导入
        import adapter as mod  # type: ignore
    return mod

# ---- 慢系统工具面（02_schemas.md §9）------------------------------------
#: 11 个慢系统/编排工具的 JSON schema。description 直接写清「什么时候该调」，
#: parameters 一律标准 JSON Schema（type=object + properties + required +
#: additionalProperties=false），不接受 type="json" 这种简写。
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]" )


def _first_cjk(text: Any) -> str:
    """返回文本里第一个中文字符（没有就返回空串）。

    用来挡住「便利贴写中文」：操作模型只吃英文，中文会让它用中文思考，
    中文 reasoning 里的换行直接产出非法 JSON（实测 3 次里 2 次解析失败）。
    """
    match = _CJK_RE.search(str(text or ""))
    return match.group(0) if match else ""


def _cjk_ratio(text: Any) -> float:
    """中文占非空白字符的比例（判断「这句是不是中文写的」）。"""

    body = "".join(str(text or "").split())
    if not body:
        return 0.0
    return len(_CJK_RE.findall(body)) / float(len(body))


#: 引号对：里面是「屏幕上的原文」，留着中文是可以的（手模型需要知道画面上写的是什么）。
QUOTE_PAIRS = (("「", "」"), ("『", "』"), ("“", "”"), ('"', '"'))


def _strip_quoted(text: Any) -> str:
    """去掉引号内的片段（只留引号外的正文），用来判断「这句话是不是中文写的」。"""

    body = str(text or "")
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        hit = None
        for open_ch, close_ch in QUOTE_PAIRS:
            if ch == open_ch:
                hit = close_ch
                break
        if not hit:
            out.append(ch)
            i += 1
            continue
        end = body.find(hit, i + 1)
        if end < 0:
            out.append(ch)
            i += 1
            continue
        out.append(' ')
        i = end + 1
    return "".join(out)


def _needs_english(text: Any) -> bool:
    """这句话是否必须改写/翻成英文（引号外的中文占比 > 15% 或汉字 ≥ 12 个）。

    引号里的屏幕原文（例如「完成」按钮）不算——那种句子手模型照样能读。
    """

    body = "".join(_strip_quoted(text).split())
    if not body:
        return False
    return _cjk_ratio(body) > 0.15 or len(_CJK_RE.findall(body)) >= 12


SLOW_TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_script",
            "description":
                "【固定流程用脚本，优先于 lumine】推进对话/过场、确认已知位置的菜单项、取消/返回、"
                "在字母网格里按固定次数挪光标——这些不看画面也能确定，脚本几秒就过完，比手模型一步步试快得多。"
                "参数只写一句自然语言要求（goal）：说「要做到什么」，**不要指导怎么按键**"
                "（不写按哪个键、按几次、先左后右）——按键步骤由程序另发一次思考请求生成。"
                "看不见画面：**必须边看边改**的（走路找路、靠近 NPC、战斗闪避、3D 转视角）才用 lumine。"
                "【硬性限制，用户 2026-11-06】一次脚本最多 6 拍，超出会被截断（Z*15 / Down*7 这类"
                "长串盲按属于「不过脑子」）：打完这几拍必须 look 一次确认画面，再决定下一步。",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "自然语言要求，写目的不写按键，例如「翻过这段开场旁白」"
                                       "「在字母网格里把名字打完并提交」",
                    },
                },
                "required": ["goal"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dispatch_task",
            "description":
                "【派活给快系统】把一段任务说明（markdown）投给快系统执行器：只投递、不等待，"
                "适合「把这串操作跑完」这类明确动作。通道没接线时会明确报错，不会假装派出去了。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_markdown": {"type": "string", "description": "任务说明，写清起点、步骤与完成条件"},
                    "done_hint": {"type": "string", "description": "可选：完成时屏幕上应该出现的证据"},
                    "avoid": {"type": "string", "description": "可选：明确不要做的事（防手滑）"},
                },
                "required": ["task_markdown"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_sticky",
            "description":
                "【读便利贴】进新存档/新场景时先读：拿回 key_binding（已学键位）与 game_rules"
                "（游戏规则），避免重新摸索。save_id 是存档标识，没有就传 default。",
            "parameters": {
                "type": "object",
                "properties": {
                    "save_id": {"type": "string", "description": "存档标识（几号档/存档名）；没有就传 default"},
                },
                "required": ["save_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_sticky",
            "description":
                "【写便利贴】学到新键位或新规则、且值得跨轮记住时才写。两块都要写全："
                "key_binding（键位）与 game_rules（规则）；单块上限 1024 字（合计 2048），超了会被明确拒绝。"
                "【硬性要求】便利贴必须全英文：任何中文（含中文标点、全角符号、中文游戏名与按钮名）都会被直接拒绝，"
                "根本写不进去。操作模型只吃英文，出现中文会让它退回全零动作，所以游戏名、按钮名也一律用英文写。",
            "parameters": {
                "type": "object",
                "properties": {
                    "save_id": {"type": "string", "description": "存档标识"},
                    "key_binding": {"type": "string", "description": "键位块：语义键 → 物理键，一行一条"},
                    "game_rules": {"type": "string", "description": "规则块：一句话一条机制"},
                    "replace": {
                        "type": "boolean",
                        "description": "true=两块整体重写；false=替换给了的块（两块都给时等价于整体重写）",
                    },
                },
                "required": ["save_id", "key_binding", "game_rules"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description":
                "【查长期记忆】要回忆以前见过的地点/NPC/规则/剧情/观众时调它，返回最相关的条目"
                "（带来源与置信度）。凭印象编答案会直接变成玩家看到的幻觉。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要查什么，用自然语言"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 32, "description": "返回条数，默认 8"},
                    "save_id": {"type": "string", "description": "可选：限定哪个存档"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_memory",
            "description":
                "【记一条长期记忆】发生值得跨场记住的事（事实/机制/地点/梗/观众）时写。kind 决定分类，"
                "confidence 是把握程度 0–1。别拿它记临时现状（那属于交棒报告）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["fact", "mechanic", "place", "bit", "viewer"],
                        "description": "fact=事实；mechanic=机制；place=地点/NPC；bit=节目效果梗；viewer=观众",
                    },
                    "text": {"type": "string", "description": "记忆原文，一句话一条"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1, "description": "把握程度 0–1，默认 1"},
                    "save_id": {"type": "string", "description": "可选：属于哪个存档"},
                },
                "required": ["kind", "text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_danmaku",
            "description":
                "【读弹幕】想知道观众刚刚在说什么、该接什么话时调它。只在规划阶段读，别在动作中间刷屏。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "最多几条，默认 30"},
                    "since_ms": {"type": "integer", "description": "只要这个时间点（epoch 毫秒）之后的弹幕"},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_last_episode",
            "description":
                "【上一轮见闻】开新一轮前调它：拿回上一次交棒的结论/见闻/最后一帧，保持连贯。"
                "没有记录会明确说没有，不会编一段复盘。",
            "parameters": {
                "type": "object",
                "properties": {
                    "game": {"type": "string", "description": "可选：游戏名；默认当前 profile"},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_hook",
            "description":
                "【埋钩子】想到后面能兑现的伏笔/回马枪（「等会儿念一条观众 ID」）时写下来，"
                "due_session 说明打算在哪一轮兑现。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "钩子内容：后面要兑现的那件事"},
                    "due_session": {"type": "string", "description": "可选：计划在第几轮/哪一场兑现"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_plan",
            "description":
                "【记企划/计划】形成跨场计划（系列主题、长期目标）或把它标成完成/放弃时调；status 默认 open。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "计划内容"},
                    "status": {
                        "type": "string",
                        "enum": ["open", "done", "dropped"],
                        "description": "open=进行中（默认）；done=已完成；dropped=放弃",
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    },
]

#: ULID 的 Crockford base32 字母表（去掉了 I/L/O/U，避免和 1/0 看混）。
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_intent_id() -> str:
    """造一个 26 位 ULID 形状的意图 id（10 位毫秒时间 + 16 位随机），不引第三方库。"""
    ms = int(time.time() * 1000)
    head = ""
    for _ in range(10):
        head = _ULID_ALPHABET[ms % 32] + head
        ms //= 32
    tail = "".join(_ULID_ALPHABET[byte % 32] for byte in _os.urandom(16))
    return head + tail


def _sibling_module(name: str):
    """惰性导入兄弟模块：包导入（agent.x）与脚本目录导入（x）都成立。"""
    try:
        return __import__("." + name, fromlist=[name])
    except ImportError:  # pragma: no cover - 脚本式导入
        return __import__(name)


#: write_memory 的五类记忆 → (记忆库条目 kind, 五层记忆里的层)。
WRITE_MEMORY_KINDS: Dict[str, Any] = {
    "fact": ("story", "semantic"),
    "mechanic": ("rule", "procedural"),
    "place": ("place", "semantic"),
    "bit": ("story", "episodic"),
    "viewer": ("story", "social"),
}

#: set_plan 只认这三个状态（02_schemas.md §4 的 Plan.status）。
PLAN_STATUSES = ("open", "done", "dropped")


class BetterGITools:
    """把 BetterGI 的 HTTP 接口包成四个模型可见的工具。"""

    def __init__(
        self,
        session: Session,
        *,
        bgi: str = DEFAULT_BGI,
        token_file: Path = TOKEN_FILE,
        pythonw: Path = PYTHONW,
        intent_cli: Path = INTENT_CLI,
        max_width: int = 960,
        allow_stub_motor: bool = False,
        profile: Any = None,
    ):
        self.session = session
        #: 当前游戏 profile；None = 原神/BetterGI 历史通道。
        self.profile = profile
        #: 快系统执行器的系统提示词（gamescaling 侧的规则/键位），restart_fast 会重算。
        self._motor_prompt: Optional[str] = None
        #: gamescaling 客户端常驻：README §4 要求每轮都带上最近 5 轮「帧 + 动作」历史，
        #: 每次新建客户端等于把历史清零，模型就只能看到一个孤立静态帧瞎猜（实测全零）。
        self._motor_client: Any = None
        self.bgi = bgi.rstrip("/")
        self.bridge = DEFAULT_BRIDGE.rstrip("/")
        self.bridge_token = BRIDGE_TOKEN_FILE.read_text(encoding="utf-8").strip()
        # Live 会话必须和 live_intent_cli.py 的 --session 一致，否则桥接会拒绝绑定。
        self.session_id = LIVE_SESSION
        self.token = token_file.read_text(encoding="utf-8").strip()
        self.pythonw = pythonw
        self.intent_cli = intent_cli
        self.max_width = max_width
        # 默认不接受"本机替身"：那只是通路验证，不能让 agent 以为自己真的在用 Lumine 策略。
        self.allow_stub_motor = allow_stub_motor
        # 本机回环不要走系统代理：环境里有 HTTP_PROXY 时 httpx 会把 127.0.0.1 也代理出去，
        # 拿回一个没有响应头的 502（实测）。
        self.client = httpx.Client(timeout=40.0, trust_env=False)
        #: 只给开发环境的收尾钩子：宿主（TUI）把它的"停止"挂在这里，
        #: 模型调用 end_test 就等于点了停止。生产环境没人挂 = 工具直接拒绝，绝不误停直播。
        self.stop_hook: Optional[Any] = None
        #: 更细的判据：测试替身会把 dev 从环境变量里踢掉，从而连带关掉这个工具。
        self.dev_enabled = (
            _os.environ.get("BETTERAGI_DEV", "").strip().lower() in ("1", "true", "yes", "on")
        )
        #: 车道追踪钩子：宿主（TUI）挂一个 callable(event: str, data: dict)，就能看见
        #: 操作器内部快系统到底选了哪几个键、画面有没有变。不挂 = 零成本，什么都不发生。
        self.trace: Optional[Any] = None
        #: 逐轮追踪日志（agent/trace_log.py，由宿主 TUI 建好挂进来）。和上面的
        #: trace 钩子分开：trace 只喂界面，trace_log 落盘留证据（提示词、原文、时间）。
        self.trace_log: Optional[Any] = None
        #: 卡住次数（04_acceptance 用来看卡点频率）。
        self.stuck_count = 0
        #: 快系统投递通道：宿主可挂 callable(packet)，或接一个 mailbox 对象；
        #: 两个都没接时 dispatch_task 必须明确失败，绝不假装"任务已派出"。
        self.dispatch_hook: Optional[Any] = None
        self.mailbox: Optional[Any] = None
        self.current_intent_id = ""
        self._intent_started_ms: Optional[int] = None
        # 便利贴按存档分文件；不指定存档时用 "default"，否则读到的路径是空的。
        self.current_save_id = "default"
        #: 记忆后端惰性缓存（同一进程内不反复建后端）。
        self._memory_backend: Any = None

    # ---- 基础 -----------------------------------------------------
    def _trace(self, event: str, **data: Any) -> None:
        """把操作器内部的动作播报给宿主。追踪坏了绝不能影响游戏，所以全吞。"""
        handler = self.trace
        if not callable(handler):
            return
        try:
            handler(event, data)
        except Exception:  # noqa: BLE001 - 一线动作，绝不为日志崩掉
            return

    def _post(self, path: str, body: Optional[dict] = None) -> dict:
        # 截图偶尔会因窗口切换/动画瞬间返回 502，重试一次比让模型看到"工具坏了"更接近真实。
        last: Optional[httpx.Response] = None
        for attempt in range(2):
            response = self.client.post(
                self.bgi + path,
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                json=body if body is not None else {},
            )
            last = response
            if response.status_code < 500:
                break
            time.sleep(0.3 * (attempt + 1))
        assert last is not None
        if last.status_code >= 400:
            raise RuntimeError(f"BetterGI {path} 返回 {last.status_code}: {last.text[:200]}")
        return last.json() if last.text else {}

    def _get(self, path: str) -> dict:
        response = self.client.get(self.bgi + path, headers={"Authorization": f"Bearer {self.token}"})
        if response.status_code >= 400:
            raise RuntimeError(f"BetterGI {path} 返回 {response.status_code}: {response.text[:200]}")
        return response.json()

    # ---- 工具实现 -------------------------------------------------
    def observe(self) -> dict:
        return self._post(
            "/api/v1/agent/observe",
            {"maxWidth": self.max_width, "format": "jpg", "quality": 70, "includeImageBase64": True},
        )

    def scan(self, regions: str) -> ToolResult:
        names = [item.strip() for item in regions.split(",") if item.strip()]
        presets = {
            "quest": (0.02, 0.045, 0.26, 0.085),
            "options": (0.18, 0.585, 0.42, 0.155),
            "message": (0.2, 0.745, 0.42, 0.135),
            "hp": (0.4, 0.92, 0.2, 0.05),
            "uid": (0.85, 0.945, 0.145, 0.045),
        }
        probe = self._post(
            "/api/v1/agent/observe",
            {"maxWidth": self.max_width, "format": "jpg", "quality": 60, "includeImageBase64": False},
        )
        width = probe["image"]["originalWidth"]
        height = probe["image"]["originalHeight"]
        rois = []
        for name in names:
            if name not in presets:
                raise ValueError(f"未知区域 {name}")
            px, py, pw, ph = presets[name]
            x = max(0, min(width - 1, round(px * width)))
            y = max(0, min(height - 1, round(py * height)))
            w = max(1, min(width - x, round(pw * width)))
            h = max(1, min(height - y, round(ph * height)))
            rois.append({"name": name, "roi": {"x": x, "y": y, "width": w, "height": h}})
        payload = self._post(
            "/api/v1/agent/observe",
            {
                "maxWidth": self.max_width,
                "format": "jpg",
                "quality": 70,
                "includeImageBase64": False,
                "includeUiState": True,
                "ocrRegions": rois,
            },
        )
        vision = payload.get("vision") or {}
        regions_out = {}
        for item in payload.get("ocrRegions") or []:
            regions_out[item["name"]] = {
                "texts": [candidate.get("text") for candidate in item.get("candidates") or []],
                "status": item.get("status"),
            }
        summary = {
            "frameId": payload.get("frameId"),
            "uiCategory": vision.get("uiCategory"),
            "isForeground": (payload.get("runtime") or {}).get("isForeground"),
            "regions": regions_out,
            "note": "空 texts 表示这次没识别到字，不等于界面里没字",
        }
        return ToolResult("scan", True, json.dumps(summary, ensure_ascii=False), None)

    def look(self) -> ToolResult:
        """BetterGI 通道的看：图像**内存直出**，不写盘（高频直播每轮看图）。"""
        payload = self.observe()
        raw = base64.b64decode(payload["imageBase64"])
        vision = payload.get("vision") or {}
        data_url = "data:image/jpeg;base64," + payload["imageBase64"]
        text = json.dumps(
            {
                "frameId": payload.get("frameId"),
                "uiCategory": vision.get("uiCategory"),
                "isForeground": (payload.get("runtime") or {}).get("isForeground"),
                "bytes": len(raw),
            },
            ensure_ascii=False,
        )
        return ToolResult("look", True, text, data_url)

    def _status_foreground(self) -> bool:
        """查一次宿主状态里的前台标记；查询失败按不在前台处理，交给 ensure_foreground。"""
        try:
            payload = self._get('/api/v1/agent/status')
        except Exception:  # noqa: BLE001 - 状态查不到时退化到激活流程
            return False
        runtime = (payload or {}).get('runtime') or {}
        return bool(runtime.get('isForeground'))

    def ensure_foreground(self) -> Dict[str, Any]:
        """把游戏窗口切到前台（不启动 Live）。

        严格安全要求前台，而 Live 运行期间宿主会拒掉所有 agent 动作，
        所以"切前台"只能在 Live 没跑的时候做：先尝试 stop，再 activate。
        """
        report: Dict[str, Any] = {}
        try:
            self._post("/api/v1/agent/live/stop", {})
        except Exception as error:  # noqa: BLE001 - 没在跑就是好事
            report["stop"] = f"{type(error).__name__}: {error}"
        try:
            self._post("/api/v1/agent/runtime/start", {})
        except Exception as error:  # noqa: BLE001
            report["runtime"] = f"{type(error).__name__}: {error}"
        try:
            self._post(
                "/api/v1/agent/actions",
                {
                    "channel": "foreground",
                    "actions": [{"type": "activateWindow"}],
                    "stopOnError": True,
                    "captureAfter": False,
                },
            )
            report["activated"] = True
        except Exception as error:  # noqa: BLE001
            report["activated"] = False
            report["error"] = f"{type(error).__name__}: {error}"
        return report

    def ensure_live(self) -> Dict[str, Any]:
        """把 Live 会话准备到"门开着、租约能给"的状态。

        顺序不能颠倒：激活窗口必须在 Live 停止时做（Live 运行期间宿主会把
        所有 agent 动作拒掉），而 Live 一旦启动又会重新采集，所以每次都从
        stop → runtime/start → activateWindow → live/start 重来一遍。
        """
        report: Dict[str, Any] = {}
        try:
            self._post("/api/v1/agent/live/stop", {})
        except Exception as error:  # noqa: BLE001 - 没在跑就是好事
            report["stop"] = f"{type(error).__name__}: {error}"
        try:
            report["runtime"] = self._post("/api/v1/agent/runtime/start", {})
        except Exception as error:  # noqa: BLE001
            report["runtime"] = f"{type(error).__name__}: {error}"
        try:
            report["activate"] = self._post(
                "/api/v1/agent/actions",
                {
                    "channel": "foreground",
                    "actions": [{"type": "activateWindow"}],
                    "stopOnError": True,
                    "captureAfter": False,
                },
            )
        except Exception as error:  # noqa: BLE001
            report["activate"] = f"{type(error).__name__}: {error}"
        started = time.monotonic()
        try:
            report["live"] = self._post(
                "/api/v1/agent/live/start",
                {
                    "sessionId": self.session_id,
                    "fps": 5,
                    "quality": 65,
                    "maxWidth": 960,
                    "requireStrictSafety": True,
                    "requireControlLease": True,
                    "dialogueCalibration": {
                        "message": {"x": 0.20, "y": 0.74, "width": 0.42, "height": 0.14},
                        "options": {"x": 0.18, "y": 0.58, "width": 0.42, "height": 0.16},
                        "exitFrames": 3,
                        "enableExitCandidate": True,
                    },
                },
            )
        except Exception as error:  # noqa: BLE001 - 已经在跑时会 400，继续看状态
            report["live"] = f"{type(error).__name__}: {error}"
        ready = False
        gate = "unknown"
        while time.monotonic() - started < 12.0:
            try:
                status = self._get("/api/v1/agent/live/status")
            except Exception:  # noqa: BLE001
                time.sleep(0.6)
                continue
            safety = status.get("safety") or {}
            gate = str(safety.get("dialogueGate") or "unknown")
            ready = safety.get("readyForV2") is True
            if ready:
                break
            time.sleep(0.6)
        report["gate"] = gate
        report["ready"] = ready
        report["waitedMs"] = int((time.monotonic() - started) * 1000)
        return report

    def lumine_availability(self) -> Dict[str, Any]:
        """先问一句"腿在不在"，别让模型对着没开的 Lumine 空转。

        Lumine 这条腿要三个东西同时在线：桥接（18311）在跑、桥接连着 BetterGI、
        桥接连着 Motor。少一个，`lumine` 工具都只会白等一轮。
        """
        report: Dict[str, Any] = {"available": False, "reason": "", "detail": {}}
        kind = motor_kind()
        report["motor"] = kind
        try:
            response = self.client.get(
                self.bridge + "/feedback",
                headers={"Authorization": f"Bearer {self.bridge_token}"},
                timeout=5.0,
            )
            if response.status_code >= 400:
                report["reason"] = f"桥接返回 {response.status_code}"
                return report
            feedback = response.json()
        except Exception as error:  # noqa: BLE001 - 桥接没起就是不可用
            report["reason"] = f"桥接连不上（{type(error).__name__}）"
            return report
        bgi_active = feedback.get("bettergiConnectionActive") is True
        motor_active = feedback.get("motorConnectionActive") is True
        report["detail"] = {
            "bettergiConnectionActive": bgi_active,
            "liveActive": bgi_active,
            "motorConnectionActive": motor_active,
            "verifiedLiveSessionId": feedback.get("verifiedLiveSessionId"),
            "motorOrigin": (feedback.get("motorCapability") or {}).get("origin"),
        }
        if not motor_active:
            report["reason"] = "桥接没有连上 Motor（Lumine/本机 Motor 没在跑）"
            return report
        if not bgi_active:
            # Live 没开时桥接连不上 BetterGI 是正常的：真正调用 lumine 时才会去 start。
            # 这里只记录状态，不当作"腿断了"，否则会陷入"没 Live 就不敢开 Live"的死锁。
            report["detail"]["liveActive"] = False
        if kind["kind"] != "lumine" and not self.allow_stub_motor:
            report["reason"] = (
                "接的是本机替身 Motor，不是真 Lumine 模型"
                if kind["kind"] == "local-stub"
                else "无法确认接的是真 Lumine 模型（没有 motor-kind.json 标记）"
            )
            return report
        report["available"] = True
        return report

    #: 判定模型的节拍：上一拍返回后，再等这么久才发下一拍（用户定的 2 秒）。
    JUDGE_INTERVAL_SEC = 2.0
    #: 判定失败连续这么多次就暂时不再问，让 Lumine 继续跑（安全上限仍然生效）。
    JUDGE_MAX_FAILURES = 3

    def _judge_client(self):
        """判定模型客户端：独立于主播 LLM，避免把图片塞进 TUI 的上下文。"""
        if getattr(self, "_judge", None) is None:
            import lithos

            self._judge = lithos.LithosClient()
        return self._judge

    def full_screen(self) -> str:
        """取判定用的全屏图（data URL）。

        优先用桥接 /frame 里已在流动的 Live 帧：Live 运行期间再去打宿主 observe
        会打断输入租约（实测 lease_lost，heldSec 8.0 就掉）。只有拿不到时才回退到
        宿主 observe（通常意味着 Live 没在跑）。
        """
        try:
            response = self.client.get(
                self.bridge + "/frame",
                headers={"Authorization": f"Bearer {self.bridge_token}"},
                timeout=5.0,
            )
            if response.status_code == 200:
                payload = response.json()
                if payload.get("ok") is True and payload.get("jpegBase64"):
                    return "data:image/jpeg;base64," + str(payload["jpegBase64"])
        except Exception:  # noqa: BLE001 - 桥接没有 /frame 时回退
            pass
        payload = self._post(
            "/api/v1/agent/observe",
            {"maxWidth": 1920, "format": "jpg", "quality": 70, "includeImageBase64": True},
        )
        encoded = payload.get("imageBase64") or ""
        if not encoded:
            raise RuntimeError("宿主没有返回全屏截图")
        return "data:image/jpeg;base64," + encoded

    #: 判定上下文的画面数：最多 3 张最新连续截图（用户明确要求）。
    JUDGE_MAX_IMAGES = 3
    #: 采样帧的间隔：与游戏 5fps 对齐，保证送给判定的是"连续"画面。
    JUDGE_FRAME_INTERVAL_SEC = 0.2

    def _sample_frames(self, buffer, seconds: float) -> None:
        """在等待下一拍判定的时间里按 5fps 采样，保持最多 3 张最新连续帧。"""
        import collections
        import time as _time

        if not isinstance(buffer, collections.deque):
            raise TypeError("buffer 必须是 collections.deque")
        deadline = _time.monotonic() + max(0.0, seconds)
        while _time.monotonic() < deadline:
            try:
                buffer.append(self.full_screen())
            except Exception:  # noqa: BLE001 - 采样失败不该打断意图
                pass
            _time.sleep(self.JUDGE_FRAME_INTERVAL_SEC)

    def judge_intent(self, goal: str, images: Sequence[str]) -> str:
        """判定上下文 = 任务文本 + 最多 3 张最新连续截图；模型关思考，只回 stop/continue。"""
        frames = [image for image in images if image][-self.JUDGE_MAX_IMAGES:]
        parts: List[Dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"意图目标：{goal}\n"
                    f"下面是最近 {len(frames)} 帧连续画面（按时间从早到晚）。"
                    "这个意图还需要继续吗？只回 stop 或 continue。"
                ),
            }
        ]
        for image in frames:
            parts.append({"type": "image_url", "image_url": {"url": image}})
        reply = self._judge_client().complete(
            [
                {
                    "role": "system",
                    "content": "你在判断一个游戏智能体的动作意图是否已经完成。只回答一个英文单词：stop 或 continue。",
                },
                {"role": "user", "content": parts},
            ],
            tier="base",
            thinking=False,
            temperature=0.0,
            max_tokens=8,
            max_retries=0,
        )
        word = (reply.text or "").strip().lower()
        return "stop" if word.startswith("stop") else "continue"

    def _cancel_intent(self) -> None:
        """让桥接立刻撤销当前意图（判定 stop 时用），不靠杀进程碰运气。"""
        try:
            self.client.post(
                self.bridge + "/intent",
                json={"cancel": True},
                headers={"Authorization": f"Bearer {self.bridge_token}"},
                timeout=3.0,
            )
        except Exception:  # noqa: BLE001 - 取消失败交给租约 TTL 兜底
            pass

    def _live_counters(self) -> dict:
        try:
            live = self._get("/api/v1/agent/live/status")
            return {
                "appliedActions": live.get("appliedActions"),
                "pressedKeys": live.get("pressedKeys"),
            }
        except Exception:  # noqa: BLE001
            return {}

    def _current_intent_id(self) -> str:
        try:
            response = self.client.get(
                self.bridge + "/feedback",
                headers={"Authorization": f"Bearer {self.bridge_token}"},
                timeout=3.0,
            )
            if response.status_code == 200:
                return str((response.json().get("current") or {}).get("intent_id") or "")
        except Exception:  # noqa: BLE001
            pass
        return ""

    RUNNER_URL = "http://127.0.0.1:18312"
    RUNNER_SCRIPT = "E:/BetterAGI/BetterGI/integrations/player/live_intent_runner.py"
    RUNNER_HOLD_MS = 3000

    def _runner_status(self):
        try:
            response = self.client.get(
                self.RUNNER_URL + "/status",
                headers={"Authorization": f"Bearer {self.bridge_token}"},
                timeout=2.0,
            )
            if response.status_code == 200:
                return response.json()
        except Exception:  # noqa: BLE001
            return None
        return None

    def ensure_runner(self, timeout: float = 12.0) -> bool:
        """确保常驻 intent runner 在线（18312）；它让意图切换不杀进程、不重拿租约。"""
        import subprocess
        import time as _time

        snap = self._runner_status()
        if snap and snap.get("ready") is True:
            return True
        if snap is None:
            try:
                subprocess.Popen(
                    [str(self.pythonw), self.RUNNER_SCRIPT],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:  # noqa: BLE001
                return False
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            snap = self._runner_status()
            if snap and snap.get("ready") is True:
                return True
            _time.sleep(0.4)
        return False

    def _runner_start_intent(self, goal: str, motor_mode: str, cap: int) -> Dict[str, Any]:
        try:
            response = self.client.post(
                self.RUNNER_URL + "/intent",
                json={"goal": goal, "motor_mode": motor_mode, "seconds": cap},
                headers={"Authorization": f"Bearer {self.bridge_token}"},
                timeout=12.0,
            )
            return response.json()
        except Exception as error:  # noqa: BLE001
            return {"accepted": False, "error": f"{type(error).__name__}: {error}"}

    def _runner_stop_intent(self, hold_ms: int = 3000) -> None:
        try:
            self.client.post(
                self.RUNNER_URL + "/stop",
                json={"hold_ms": int(hold_ms)},
                headers={"Authorization": f"Bearer {self.bridge_token}"},
                timeout=5.0,
            )
        except Exception:  # noqa: BLE001
            pass

    def lumine(self, goal: str, seconds: Optional[int] = None, motor_mode: str = "navigate",
               done_when: str = "", text: str = "", max_steps: int = 10,
               wait: float = 0.7) -> ToolResult:
        """**唯一的动作通道（操作器）**：慢系统把意图交给它，由它去动键鼠。

        为什么只有一个入口（用户口径 m05832）：慢系统管抉择与记忆，操作器管执行。
        一旦允许慢系统直接叫 act/press/type，快慢分层就退化成"慢模型自己当手"，
        FR-3/FR-9 的意图总线形同虚设。所以按通道分发，工具面对外只有一个 lumine：

        - 原神通道（LUMINE_CHANNEL_GAMES）：走 Lumine 桥接 + 判定模型（_lumine_bridge）；
        - 直控通道（Undertale 等）：走内建操作器 _operator（快系统选键 + 物理按键）。
        """
        # 用户 2026-11-05：交给操作模型（lumine）的一切输入必须是英文。
        # 中文目标会让 4B 用中文思考，中文 reasoning 里的换行直接产出非法 JSON。
        if _needs_english(goal):
            return ToolResult(
                "lumine", False,
                "lumine 的 goal 必须全英文（操作模型只吃英文，中文会让它输出坏 JSON）："
                "发现中文目标「%s」。把 goal 改写成一句到三句英文再重试，意思不变（屏幕上的中文可用「」原样引用）。" % str(goal)[:40],
            )
        if channel_uses_lumine(self.profile):
            return self._lumine_bridge(goal, seconds, motor_mode)
        return self._operator(
            goal,
            max_steps=max_steps,
            wait=wait,
            done_when=done_when,
            text=text,
        )

    def _lumine_bridge(self, goal: str, seconds: Optional[int] = None,
                       motor_mode: str = "navigate") -> ToolResult:
        """把意图交给 Lumine，一直跑到判定模型说 stop（或安全上限）。

        与旧版的区别：不再"固定 seconds 秒"，而是起一个不定长意图。每约 2 秒
        （上一拍返回后再计时）把全屏画面 + 目标文本交给一个关掉思考的判定模型，
        只问一个词：stop（完成/卡住）还是 continue。stop 就撤销意图、把控制权
        还给 LLM；continue 就让 Lumine 继续接手。seconds 只是安全上限。
        """
        import subprocess
        import time as _time

        available = self.lumine_availability()
        if available["available"] is not True:
            return ToolResult(
                "lumine",
                False,
                json.dumps(
                    {
                        "ok": False,
                        "stage": "lumine_unavailable",
                        "reason": available["reason"],
                        "detail": available["detail"],
                        "hint": "Lumine 这条腿现在没接通。不要再调用 lumine；先用 scan/look 观察，并把情况告诉玩家。",
                    },
                    ensure_ascii=False,
                ),
            )

        prepared = self.ensure_live()
        if prepared.get("ready") is not True:
            return ToolResult(
                "lumine",
                False,
                json.dumps(
                    {
                        "ok": False,
                        "stage": "live_not_ready",
                        "error": "Live 会话没有进入可授权状态（通常是没有前台窗口或识别不到主界面）",
                        "prepare": prepared,
                    },
                    ensure_ascii=False,
                ),
            )

        cap = max(5, min(300, int(seconds or 60)))
        # 优先常驻 runner：切换意图不杀进程、不重拿租约，避免可见空档。
        use_runner = self.ensure_runner()
        runner_state: Dict[str, Any] = {}
        if use_runner:
            runner_state = self._runner_start_intent(goal, motor_mode, cap)
            use_runner = bool(runner_state.get("accepted"))
        args = [
            str(self.intent_cli),
            "--goal",
            goal,
            "--seconds",
            str(cap),
            "--motor-mode",
            motor_mode,
            "--sample",
            "2",
        ]
        proc = None
        if not use_runner:
            proc = subprocess.Popen(
                [str(self.pythonw), *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        started = _time.monotonic()
        before = self._live_counters()
        judged = {"stop": 0, "continue": 0, "errors": 0, "last": ""}
        ended_by = "cap"
        # 取消意图后再查就查不到了：先记下 intentId，终止原因里要带它。
        intent_before_cancel = ""
        # 判定上下文：任务文本 + 最多 3 张最新连续截图（用户明确要求）。
        import collections

        frames: "collections.deque[str]" = collections.deque(maxlen=self.JUDGE_MAX_IMAGES)
        self._sample_frames(frames, self.JUDGE_MAX_IMAGES * self.JUDGE_FRAME_INTERVAL_SEC)
        try:
            while True:
                if proc is not None and proc.poll() is not None:
                    ended_by = "intent_exit"
                    break
                if _time.monotonic() - started >= cap:
                    ended_by = "cap"
                    intent_before_cancel = self._current_intent_id()
                    if use_runner:
                        self._runner_stop_intent(self.RUNNER_HOLD_MS)
                    else:
                        self._cancel_intent()
                    break
                try:
                    verdict = self.judge_intent(goal, list(frames))
                    judged[verdict] = judged.get(verdict, 0) + 1
                    judged["last"] = verdict
                    judged["errors"] = 0
                except Exception as error:  # noqa: BLE001 - 判定失败不能把意图停掉
                    verdict = "continue"
                    judged["errors"] += 1
                    judged["last"] = f"{type(error).__name__}: {error}"[:120]
                    if judged["errors"] >= self.JUDGE_MAX_FAILURES:
                        # 判定端不可用时不要在死循环里空转：让 Lumine 自己跑到上限。
                        break
                if verdict == "stop":
                    ended_by = "judge_stop"
                    intent_before_cancel = self._current_intent_id()
                    if use_runner:
                        self._runner_stop_intent(self.RUNNER_HOLD_MS)
                    else:
                        self._cancel_intent()
                    break
                self._sample_frames(frames, self.JUDGE_INTERVAL_SEC)
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except Exception:  # noqa: BLE001
                    proc.kill()

        stdout = proc.stdout.read() if proc is not None and proc.stdout else ""
        stderr = proc.stderr.read() if proc is not None and proc.stderr else ""
        payload = {}
        for line in reversed((stdout or "").strip().splitlines()):
            if line.strip().startswith("{"):
                try:
                    payload = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        if payload and payload.get("ok") is not True:
            return ToolResult("lumine", False, json.dumps(payload, ensure_ascii=False))

        after = self._live_counters()
        summary = {
            "ok": True,
            "intentId": payload.get("intentId") or runner_state.get("intentId") or intent_before_cancel or self._current_intent_id(),
            "via": "runner" if use_runner else "cli",
            "endedBy": ended_by,
            "heldSec": round(_time.monotonic() - started, 1),
            "appliedFrom": before.get("appliedActions"),
            "appliedTo": after.get("appliedActions"),
            "pressedKeys": after.get("pressedKeys"),
            "judge": judged,
            "note": "appliedActions 是输入 chunk 计数（6 chunk = 一个 200ms 动作块）；endedBy=judge_stop 表示判定模型认为意图已经完成，接下来你自己决定干什么。",
        }
        if not stdout and stderr:
            summary["stderr"] = stderr[-300:]
        return ToolResult("lumine", True, json.dumps(summary, ensure_ascii=False))

    def generic_prepare(self) -> Dict[str, Any]:
        """通用通道的前台准备：清对话框 + 把游戏窗口切到前台（全部走 profile adapter）。"""
        adapter_mod = _adapter_mod()
        return adapter_mod.ProfileAdapter(self.profile).prepare()

    def window_state(self) -> Dict[str, Any]:
        """轻量窗口探针：只做窗口发现，不抢前台、不截帧、不等 tasklist。

        TUI 用它做「游戏还没开就先等着」的门：窗口不在时不该请求模型、更不该烧 token。
        """
        if self.profile is None:
            return {"ok": False, "reason": "没有 profile，无法定位游戏窗口"}
        adapter_mod = _adapter_mod()
        state = adapter_mod.ProfileAdapter(self.profile).windows(running=False)
        main = state.get("main") or {}
        return {"ok": bool(main), "game": getattr(self.profile, "game_id", ""),
                "title": main.get("title", ""), "hwnd": main.get("hwnd"),
                "pid": main.get("pid")}

    def generic_look(self, native: bool = False) -> ToolResult:
        """非 BetterGI 游戏（由 profile 的 adapter 描述）的看：通用截帧，**内存直出**不落盘。

        native=True 走原尺寸（不缩放）：策略模型要读画面上的小字，缩放会把 Z 认成 2。
        """
        adapter_mod = _adapter_mod()
        ad = adapter_mod.ProfileAdapter(self.profile)
        report = ad.prepare()
        state = ad.windows()
        frame = ad.grab(quality=90, fit=None) if native else ad.grab()
        raw = frame.pop("data")
        data_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
        hint = (
            "先根据最新画面辨认可见对话文字、菜单、角色与可行走空间；没有对话框不要假定还在对话。"
            "仅在状态明确且短流程可预期时使用 run_script；场景可能变化或状态不明时不要长串盲按。"
            "【画面没变不等于按键坏了】同一个键两次没反应就换一个不同的键（另一个方向/确认/取消/菜单），"
            "再不行换手段；不要为了验证输入把同一个键连按，也不要把工具挨个试一遍。"
            "【默认当前画面能操控】画面上只要有角色、对话框、菜单或可走的房间，就当它是可操作的；"
            "只有画面自己在动、没有可控角色时才当剧情过场。不要用「可能是过场」当借口不动手。"
            "探索、走路和需要边看边改的操作用 lumine。"
        )
        text = json.dumps(
            {"game": self.profile.game_id, "width": frame.get("width"),
             "height": frame.get("height"), "bytes": len(raw),
             "prepare": report, "dialogs": len(state["dialogs"]),
             "hint": hint},
            ensure_ascii=False,
        )
        return ToolResult("look", True, text, data_url)

    def generic_act(self, keys: List[str], wait: float = 0.8) -> ToolResult:
        """非 BetterGI 游戏的按：按键名先经 profile 的 keymap 解析成物理键，再真按键并回一帧（内存）。"""
        adapter_mod = _adapter_mod()
        ad = adapter_mod.ProfileAdapter(self.profile)
        out = ad.act(keys, wait=wait)
        data_url = "data:image/jpeg;base64," + out.pop("jpeg_b64")
        return ToolResult("act", True, json.dumps(out, ensure_ascii=False), data_url)

    def run_script(self, goal: str = "", script: str = "", **_ignored: Any) -> ToolResult:
        """脚本批处理：把程序生成好的精确脚本一次打完（key_scripts）。

        用户 2026-11-05 的最新口径：
        - 模型对工具只说一句**自然语言要求**（goal），不再自己写 DSL，也没有任何预设；
        - 真正发给本方法的 script 由 tui 那一层额外发一次「开思考」的请求生成，
          并连同那次请求一起记进上下文；
        - 预设（dialogue/confirm/…）已全部删除。
        """
        try:
            import key_scripts as ks
        except Exception as error:  # noqa: BLE001
            return ToolResult("run_script", False,
                              "key_scripts 不可用：%s: %s" % (type(error).__name__, error))
        text = str(script or "").strip()
        if not text:
            return ToolResult(
                "run_script", False,
                "还没拿到脚本：程序会根据你的 goal 另发一次思考请求把它写成精确按键序列。"
                "如果这条反复出现，说明脚本生成失败，改用 lumine。")
        try:
            ops = ks.build_ops(cmd=text)
        except Exception as error:  # noqa: BLE001 - 语法错要原样报回去，不能猜一个动作
            return ToolResult("run_script", False, "%s: %s" % (type(error).__name__, error))
        # 用户 2026-11-06 投诉「不过脑子就狂按」：脚本一次最多 6 拍，长串（Z*15 / Down*7）
        # 一律截断到前 6 拍并让模型先看画面。以前一次打 25~40 拍，等于闭着眼睛按。
        blind_cut = 0
        if len(ops) > BLIND_SCRIPT_MAX_STEPS:
            blind_cut = len(ops) - BLIND_SCRIPT_MAX_STEPS
            ops = ops[:BLIND_SCRIPT_MAX_STEPS]
        try:
            adapter_mod = _adapter_mod()
            ad = adapter_mod.ProfileAdapter(self.profile)
        except Exception as error:  # noqa: BLE001
            return ToolResult("run_script", False,
                              "适配器起不来：%s: %s" % (type(error).__name__, error))
        try:
            result = ks.run(ad, ops, settle_ms=int(getattr(self, "script_settle_ms", 120)))
        except Exception as error:  # noqa: BLE001
            return ToolResult("run_script", False,
                              "脚本执行失败：%s: %s" % (type(error).__name__, error))
        payload = dict(result)
        payload["script"] = text
        payload["goal"] = str(goal or "")
        payload["game"] = getattr(self.profile, "game_id", "")
        if blind_cut:
            payload["truncated_steps"] = blind_cut
            payload["note"] = ("只打了前 %d 拍，后面 %d 拍被截掉（用户要求不许长串盲按："
                               "先看画面再决定下一步）" % (BLIND_SCRIPT_MAX_STEPS, blind_cut))
        return ToolResult(
            "run_script",
            bool(payload.get("ok")),
            json.dumps(payload, ensure_ascii=False, default=str),
        )


    # ---- 快系统通道（Qwen-Omni 当手，慢系统只下意图）---------------------
    def _trace_event(self, event: str, data: Dict[str, Any]) -> None:
        """把执行器的事件（event, dict）接到车道追踪上：执行器只管报，谁听它不管。"""
        payload = dict(data or {})
        # 2026-11-06：执行器事件同时进追踪日志（界面只画一部分，日志要全留）。
        if self.trace_log is not None:
            try:
                self.trace_log.event("fast_" + event, **payload)
            except Exception:  # noqa: BLE001 - 日志坏了不能影响按键
                pass
        self._trace(event, **payload)

    def _fast_stream(self):
        """起一个 Omni 流（嘴 + 裁判）。缺 key 就返回 (None, 原因)，绝不假装有嘴。"""
        try:
            import fast_stream as _fast
        except Exception as error:  # noqa: BLE001
            return None, f"fast_stream 模块不可用：{type(error).__name__}: {error}"
        if _os.environ.get("BETTERAGI_FAST", "1") in ("0", "false", "False"):
            return None, "BETTERAGI_FAST=0：快系统被显式禁用"
        # pump=False：灌帧节奏由执行器按决策节拍控制，后台泵会和 ask 抢缓冲。
        stream = _fast.build_fast_stream(fps=2.0, pump=False)
        if stream is None:
            return None, "快系统不可用（缺少 DASHSCOPE_API_KEY，或 BETTERAGI_FAST=0）"
        return stream, ""

    def _fast_probe(self) -> Dict[str, Any]:
        """两条腿各探一次：手（gamescaling）在不在，嘴（Omni）能不能说话。"""
        out: Dict[str, Any] = {"motor": False, "narrator": False}
        try:
            from motor import build_motor

            motor = build_motor()
            if motor is not None:
                out["motor"] = bool(motor.health(timeout=3).get("available"))
        except Exception as error:  # noqa: BLE001 - 探活失败就是不可用
            out["motor_error"] = "%s: %s" % (type(error).__name__, error)
        stream, why = self._fast_stream()
        if stream is None:
            out["narrator_error"] = why
            return out
        try:
            stream.start()
            said = str(stream.ask("只回复两个字：在的", timeout=20) or "").strip()
            out["narrator"] = bool(said)
        except Exception as error:  # noqa: BLE001
            out["narrator_error"] = "%s: %s" % (type(error).__name__, error)
        finally:
            try:
                stream.stop()
            except Exception:  # noqa: BLE001 - 收尾失败不影响探活结论
                pass
        return out

    def _operator(self, intent: str, max_steps: int = 0, wait: float = 0.0,
                  done_when: str = "", text: str = "", budget_s: float = 20.0) -> ToolResult:
        """操作器本体（内部实现，**不是**慢系统的工具）：把一个意图做到位，中途不汇报。

        双系统口径（REQ-2026-11-04 术语表 + FR-3/FR-9，用户 2026-11-04 三次纠偏）：
        慢系统只产出意图；快系统是两条腿并行——**手**是 gamescaling（吃当前帧，输出
        33ms 的操作数据），**嘴+裁判**是 Omni（同一条流上给带时间码的解说，并界定这次
        操作该不该收手）。所以这里既没有"选键"也没有 linker：慢系统看不到任何
        confirm/up 之类的东西，回收的只有解说时间轴 + 情况总结。

        text：意图里带字面文字时（命名框/输入框）由操作器注入，慢系统不碰键盘。
        done_when：完成条件，交给 Omni 当裁判；命中才算 reached。
        """
        if self.profile is None:
            return ToolResult("lumine", False, "没有 profile，无法操作")
        try:
            from executor import FastExecutor
        except Exception as error:  # noqa: BLE001
            return ToolResult("lumine", False, f"executor 模块不可用：{type(error).__name__}: {error}")
        try:
            from motor import build_motor
        except Exception as error:  # noqa: BLE001
            return ToolResult("lumine", False, f"motor 模块不可用：{type(error).__name__}: {error}")
        adapter_mod = _adapter_mod()
        ad = adapter_mod.ProfileAdapter(self.profile)
        started = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        typed: Dict[str, Any] = {}
        errors: List[str] = []
        if str(text or "").strip():
            # 字面文字属于"意图的负载"，由操作器注入；慢系统手里永远没有 type 原语。
            try:
                typed = ad.type_text(str(text))
            except Exception as error:  # noqa: BLE001 - 注入失败如实记录，别吞
                errors.append("输入文字失败：%s: %s" % (type(error).__name__, error))
        # 手：gamescaling。不可用就直接失败——**绝不退回慢系统自己按键**，那等于把快系统
        # 的职责塞回慢系统兜里，正是这次要修的架构走样。
        motor = build_motor()
        motor_error = ""
        if motor is None:
            motor_error = "没有 gamescaling 客户端（手缺席）"
        else:
            try:
                health = motor.health(timeout=3)
            except Exception as error:  # noqa: BLE001
                health = {"available": False, "error": "%s: %s" % (type(error).__name__, error)}
            if not health.get("available"):
                motor_error = "gamescaling 不可用：%s" % json.dumps(
                    {k: health.get(k) for k in ("endpoint", "status", "error")}, ensure_ascii=False
                )
        if motor_error:
            self._trace("operator_end", steps=0, ended="no_motor", total_ms=0,
                        reached=False, errors=[motor_error])
            return ToolResult("lumine", False, json.dumps({
                "at": stamp, "intent": intent, "ended": "no_motor",
                "errors": [motor_error] + errors[:2],
                "hint": "手（gamescaling）没接上：先把 18040 的策略服务/隧道拉起来再操作。",
            }, ensure_ascii=False))
        # 嘴 + 裁判：Omni。缺 key 时仍可操作，但没有解说也没有收手判定。
        narrator, narrator_why = self._fast_stream()
        if narrator is not None:
            try:
                narrator.start()
            except Exception as error:  # noqa: BLE001 - 嘴起不来不影响手
                errors.append("解说通道启动失败：%s: %s" % (type(error).__name__, error))
                narrator = None
        motor_prompt = ""
        prompt_error = ""
        prompt_source = ""
        prompt_path = ""
        # 手的提示词优先用便利贴（慢系统 per-save 写的那两块）；没学过才退旧的全量提示词。
        # 不接这一步，便利贴就只是「有存储、零调用」——学会了也不生效（03 C.8）。
        try:
            import motor_wire

            # 2026-11-06：便利贴只写了「up -> up」这种含糊映射时，手模型会按字母 W
            # 想走路（Undertale 只认方向键，W 什么都没发生），于是整轮画面不动。
            # 这里把 profile keymap 生成的 Common Keys 一起喂给手（run_stream 一直是
            # 这么做的，TUI 这条通道漏了），再补两条硬事实。
            try:
                from run_stream import key_lines as _key_lines
                bindings = _key_lines(self.profile)
            except Exception:  # noqa: BLE001 - 拿不到就只靠便利贴
                bindings = []
            resolved = motor_wire.build_motor_system(
                game=getattr(self.profile, "game_id", "") or "",
                save_id=str(getattr(self, "current_save_id", "") or ""),
                # 便利贴根 = profile 目录（<profile>/sticky/<game>/<save>.md）；
                # 不传根目录会读到当前工作目录下的 run/sticky，永远读不到同一份。
                root=getattr(self.profile, "dir", None),
                legacy=(lambda: __import__("prompt_builder").build_system_prompt(self.profile).text),
                # 这一轮的目标必须进手的 system（**Current Objective** 段）：
                # 不给目标的实测后果是手只会按方向键试探，永远不知道要干什么。
                objective=intent,
                key_lines=bindings,
                extra_rules=(
                    "- Movement uses the arrow keys only: Up / Down / Left / Right. "
                    "The letters W / A / S / D do nothing here; pressing W does not walk.\n"
                    "- Confirm and advance dialogue with Z; cancel with X; open the menu with C; "
                    "go back with Backspace."
                ),
            )
            motor_prompt = str(resolved.get("text") or "")
            prompt_source = str(resolved.get("source") or "")
            prompt_path = str(resolved.get("path") or "")
            warning_text = "; ".join(str(x) for x in (resolved.get("warnings") or []))
            if warning_text:
                prompt_error = warning_text
            if not motor_prompt.strip():
                prompt_error = prompt_error or "手模型提示词是空的：便利贴没写，旧提示词也没拼出来"
        except Exception as error:  # noqa: BLE001 - 拼不出提示词也照常跑，只是会退化
            prompt_error = "%s: %s" % (type(error).__name__, error)
        # 2026-11-06：手模型这一轮读到的完整提示词必须落盘。排查「手为什么按 W /
        # 为什么不动」时，光看动作看不出来——要么它没拿到方向键，要么规则被便利贴
        # 的含糊映射顶掉了，只有原文能证明。
        if self.trace_log is not None:
            try:
                self.trace_log.event(
                    "hand_prompt",
                    intent=intent,
                    done_when=done_when,
                    prompt_source=prompt_source,
                    prompt_path=prompt_path,
                    prompt_chars=len(motor_prompt),
                    prompt_error=prompt_error,
                    system=motor_prompt,
                )
            except Exception:  # noqa: BLE001 - 日志坏了不能拦操作
                pass
        # 用户 2026-11-05 投诉「明明还需要操作一会就直接说任务失败」：
        # 20 秒 / 3 次决策对真实游戏太短，一次决策现在实测 ~1s，光走路都不够。
        budget = float(budget_s or 60.0)
        # max_steps 现在的意思是"最多几次决策"（每次决策 = 一段 200ms 的操作数据），
        # 不是"按几次键"：一次决策里可能一次键都不按（纯移动鼠标）。
        # max_steps 只是慢系统的「估计」，不是硬上限：少于 20 次决策会频繁半路收手。
        decisions = max(20, int(max_steps or 10))
        # 用户 2026-11-06 投诉「不过脑子就狂按按键」：手模型会在画面毫无变化时
        # 连按同一个键十几次（实测 18 次 W 画面一帧不动）。执行器本来就有卡住检测，
        # 但这条 TUI 通道以前没把检测器传进来（self.stuck is None → 检查永远返回空），
        # 等于完全没开。这里按「6 帧几乎不动」或「同一套动作重复 3 次且画面不动」判定：
        # 一旦成立就收手交回慢系统重新判断，而不是继续按。
        executor = FastExecutor(
            ad, motor=motor, narrator=narrator,
            motor_prompt=motor_prompt, game=getattr(self.profile, "display_name", "") or "",
            on_event=self._trace_event, step_ms=33,
            stuck=stuck_mod.build_stuck(window=6, min_change=1.5, min_repeats=3),
        )
        self._trace("operator_start", intent=intent, budget_s=budget,
                    max_decisions=decisions,
                    done_when=done_when, text=text, typed=bool(typed),
                    narrator=bool(narrator), motor=True)
        try:
            result = executor.run(intent, done_when=done_when, budget_s=budget,
                                  max_decisions=decisions)
        finally:
            if narrator is not None:
                try:
                    narrator.stop()
                except Exception:  # noqa: BLE001 - 收尾失败不该影响已经拿到的手感
                    pass
        total_ms = int((time.time() - started) * 1000)
        ended = str(result.get("ended") or "unknown")
        reached = bool(result.get("reached"))
        self._trace("operator_end", steps=int(result.get("action_count") or 0), ended=ended,
                    total_ms=total_ms, reached=reached,
                    errors=(result.get("errors") or [])[:3])
        frame = None
        try:
            grabbed = ad.grab()
            frame = "data:image/jpeg;base64," + base64.b64encode(grabbed.pop("data")).decode()
        except Exception:  # noqa: BLE001 - 收尾截帧失败不影响结论
            frame = None
        # 用户 2026-11-05：慢系统要看到操作器到底按了哪些键，程序自动统计成脚本形式。
        all_actions = list(result.get("actions") or [])
        try:
            import key_scripts as _key_scripts

            pressed_script = _key_scripts.actions_to_script(all_actions)
        except Exception as script_error:  # noqa: BLE001 - 统计失败不该影响结论
            pressed_script = {"script": "", "steps": 0, "keys": 0, "skipped": 0,
                              "truncated": False, "error": "%s: %s" % (
                                  type(script_error).__name__, script_error)}
        payload = {
            "at": stamp,
            "intent": intent,
            "ended": ended,
            "total_ms": total_ms,
            "action_count": int(result.get("action_count") or 0),
            # 回传口径（用户原话）：所有解说 + 每条的时间码 + 操作时间内的情况总结。
            "commentary": result.get("commentary") or [],
            "summary": result.get("summary") or "",
            # 这一段实际按下的键（自动压成 DSL 脚本形式），慢系统据此判断做到哪一步。
            "script": pressed_script.get("script") or "",
            "script_stats": {k: pressed_script.get(k) for k in
                             ("steps", "keys", "skipped", "truncated")},
            "actions": [
                {k: a.get(k) for k in ("t_ms", "keys", "decide_ms", "pressed", "skipped")}
                for a in all_actions
            ][-12:],
            "errors": (errors + list(result.get("errors") or []))[:3],
            "done_when": done_when or None,
            "achievable": bool(done_when) and bool(narrator),
            "reached": reached,
            "typed": typed or None,
            "narrator": bool(narrator),
            "narrator_error": narrator_why or None,
            "prompt_error": prompt_error or None,
            # 提示词从哪来的：sticky=便利贴已生效 / legacy=旧全量提示词 / empty=一个字都没有。
            "prompt_source": prompt_source or None,
            "prompt_chars": len(motor_prompt or ""),
            "sticky_path": prompt_path or None,
        }
        # 给了完成条件就只认裁判的判定；没给就只要求"确实动过手"。
        if done_when:
            ok = reached
        else:
            ok = int(result.get("action_count") or 0) > 0 and ended not in ("motor_error", "capture_error")
        return ToolResult("lumine", ok, json.dumps(payload, ensure_ascii=False), frame)



    @staticmethod
    def _tune_step_timing(steps: List[List[str]], window_ms: int = 200, step_ms: int = 33) -> int:
        """把"6 步"换算成真正等分的步长，让一次决策刚好覆盖它声称的 200ms。

        gamescaling 的契约是「一次决策 = 未来 200ms、6 步」——6x33 只有 198ms，
        近似但慢一档。这里按 200/6 取整（33ms），并在每步之后按实际耗时补齐，
        保证整段动作落在 200ms 窗口内。真实按键本身要几十毫秒，不补齐就会越拖越慢。
        """
        n = max(1, len([s for s in steps]) or 1)
        return max(8, min(step_ms if step_ms > 0 else window_ms // n, window_ms))

    def motor(self, instruction: str = "", keys_hint: List[str] | None = None) -> ToolResult:
        """用 gamescaling 策略模型操作：截帧 → 它给动作 → 真的按下去。

        这是**首选操作通道**。手动 act 只在 motor 不可用时用来兜底：model 自己拍的
        按键没有策略模型依据，容易死循环。
        """
        if self.profile is None:
            return ToolResult("motor", False, "没有 profile，无法用 gamescaling")
        try:
            import motor as motor_mod
            from motor import build_motor
        except Exception as error:  # noqa: BLE001
            return ToolResult("motor", False, f"motor 模块不可用：{type(error).__name__}: {error}")
        # 原尺寸交给策略模型（README §4：JPEG quality 90、不缩放）。
        frame = self.generic_look(native=True)
        if not frame.ok or not frame.image:
            return ToolResult("motor", False, f"截帧失败，无法调用 gamescaling：{frame.text}")
        client = self._motor_client
        if client is None:
            client = build_motor()
            self._motor_client = client
        # 传给策略模型的是 profile 渲染出的 gamescaling 系统提示词（游戏规则 + 已学键位
        # + 待学目标）。没有它，模型就是对着屏幕瞎猜，输出全零。
        system_prompt = None
        try:
            from prompt_builder import build_system_prompt
            bundle = build_system_prompt(self.profile)
            system_prompt = bundle.text
        except Exception as error:  # noqa: BLE001 - 拼不出提示词也照常调，只是会退化
            system_prompt = None
            prompt_error = f"{type(error).__name__}: {error}"
        else:
            prompt_error = None
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        t0 = time.time()
        try:
            decision = client.decide(
                frame.image, instruction=instruction, game=self.profile.display_name,
                system_prompt=system_prompt,
            )
        except Exception as error:  # noqa: BLE001 - 服务不通就如实说，让模型自己决定要不要手动兜底
            return ToolResult(
                "motor", False,
                f"[{stamp}] gamescaling 不可用（{type(error).__name__}: {error}）。"
                "可以继续用 look 观察；如果确实必须动，再用 act 手动兜底。",
            )
        decide_ms = int((time.time() - t0) * 1000)
        # 无条件记历史（空动作也记 no-op）：下一轮请求要带最近 5 轮帧+动作，
        # 少了任何一轮，模型看到的动作序列就和自己真正做过的事对不上。
        try:
            client.record(frame.image, motor_mod.decision_action_text(decision))
        except Exception:  # noqa: BLE001 - 记历史失败不该阻断执行
            pass
        steps = decision.get("steps") or []
        flat = [k for step in steps for k in step]
        mapped = [k for k in (keys_hint or [])] or flat
        if not mapped:
            return ToolResult(
                "motor", True,
                json.dumps({"at": stamp, "decide_ms": decide_ms, "raw": decision.get("raw"),
                            "mouse": decision.get("mouse"), "pressed": [],
                            "note": "策略模型这一步没给可执行按键（空动作也是合法决策）；画面和原文都在，自己判断要不要继续 motor"},
                           ensure_ascii=False),
                frame.image,
            )
        # 按 6 步语义真按键：组内同时按下，步间 33ms——拍平会毁掉连招。
        adapter_mod = _adapter_mod()
        ad = adapter_mod.ProfileAdapter(self.profile)
        step_ms = self._tune_step_timing(steps, window_ms=int(decision.get("duration_ms") or 200),
                                        step_ms=int(decision.get("step_ms") or 33))
        try:
            played = ad.play_steps(steps, step_ms=step_ms)
        except Exception as error:  # noqa: BLE001
            return ToolResult("motor", False, f"[{stamp}] 动作已算出但按键失败：{type(error).__name__}: {error}")
        out = {
            "at": stamp,
            "decide_ms": decide_ms,
            "raw": decision.get("raw"),
            "mouse": decision.get("mouse"),
            "steps": steps,
            "step_ms": step_ms,
            "window_ms": decision.get("duration_ms"),
            "pressed": played.get("pressed"),
            "skipped_keys": played.get("skipped_keys"),
            "prompt_chars": len(system_prompt or ""),
            "prompt_error": prompt_error,
        }
        return ToolResult("motor", True, json.dumps(out, ensure_ascii=False))

    # ---- 通用游戏通道（profile 驱动）------------------------------------
    def known_keys(self) -> Dict[str, List[str]]:
        """当前 profile 已学到的语义键名 → 物理键。空 dict 表示还没有任何绑定。"""
        if self.profile is None:
            return {}
        adapter_mod = _adapter_mod()
        return adapter_mod.ProfileAdapter(self.profile).keymap

    def end_test(self, reason: str = "") -> ToolResult:
        """结束这一次测试运行——**只在开发环境可用**。

        判定是三重的，缺一不可：
        1. 进程是 dev（BETTERAGI_DEV=1 或宿主显式打开 dev_enabled）；
        2. 宿主确实挂了 stop_hook（生产直播没有任何东西会挂它）；
        3. 钩子调用没有抛错。
        任何一条不成立就如实回绝，绝不允许模型把观众的直播关掉。
        """
        if not getattr(self, "dev_enabled", False) or self.stop_hook is None:
            return ToolResult(
                "end_test", False,
                json.dumps({
                    "ok": False,
                    "detail": "end_test 只在开发环境可用：当前进程没有 dev 标志，或宿主没有挂停止钩子。"
                              "正式运行请忽略这个工具，用 /quit 或关闭窗口来停。",
                }, ensure_ascii=False),
            )
        reason = (reason or "").strip()[:200]
        try:
            handler = self.stop_hook
            if callable(handler):
                handler(reason)
            else:  # 直接给了 Event，set() 就是标准停止语义
                handler.set()
        except Exception as error:  # noqa: BLE001 - 停止失败要如实告诉模型
            return ToolResult("end_test", False, "%s: %s" % (type(error).__name__, error))
        return ToolResult(
            "end_test", True,
            json.dumps({"ok": True, "detail": "已经请求宿主结束本次运行，当前轮收尾后退出。",
                        "reason": reason}, ensure_ascii=False),
        )

    def uses_lumine(self) -> bool:
        """这条通道是不是靠 lumine 那条腿（只有 BetterGI 原神通道是）。"""
        return channel_uses_lumine(self.profile)

    def tool_specs(self) -> List[Dict[str, Any]]:
        """按 profile 选工具面：BetterGI 游戏给 scan/look/lumine，其余给 look/act/learn_key。

        act 的键名不写死在这里——它来自 profile 的 keymap，agent 学到什么就有什么。
        """
        if self.profile is None or self.profile.game_id == "genshin-impact":
            return TOOL_SPECS
        keys = self.known_keys()
        known = "、".join(sorted(keys)) or "（还没有学到任何键位）"
        return [
            {
                "type": "function",
                "function": {
                    "name": "look",
                    "description": f"截一张 {self.profile.display_name} 的当前画面（真图）。每步行动后都要看一眼再决定下一步。",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "lumine",
                    "description":
                        "【只在必须边看边改时用】固定流程（推进对话/翻菜单/字母网格/走直线/等过场）先用 run_script。"
                        "【唯一的动作通道 · 操作器】把「一个意图」交出去。操作器里手嘴并行：手是策略模型"
                        "（吃当前画面、每 33ms 吐一段操作数据并真的按下去），嘴是同一条画面流上的实时解说"
                        "并判断该不该收手。中途不返回，做完给你：**带时间码的解说时间轴 + 这一段的情况总结**"
                        "+ 一张结束时的真图。总结只是它的自述，要判断真假就自己再 look 一次。"
                        "你只写意图，不写按键——「把这个名字确认掉」「把光标移到第一个选项并确认」就是合格意图。"
                        f"当前学到的键位（仅供你判断可行性，不用你来按）：{known}。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string", "description": "要达成的意图，用自然语言写，例如「确认角色名字并进入游戏」"},
                            "done_when": {
                                "type": "string",
                                "description": "（强烈建议填）完成条件的画面证据，例如「屏幕出现游戏剧情开头或可操控角色」。"
                                "填了它，只有操作器里的解说+裁判在画面上确认条件成立才算完成（连续两次看到才算，防幻觉），"
                                "否则 ok=false，并把解说时间轴交给你判断打到了哪一步。",
                            },
                            "text": {
                                "type": "string",
                                "description": "（可选）意图要求往输入框/命名框里写一段字时，把那段字放在这里；操作器负责注入，你不用碰键盘。",
                            },
                            "max_steps": {"type": "integer", "description": "最多连续做几次决策，默认按时间预算走（20 秒）"},
                            "wait": {"type": "number", "description": "（可选）留给旧接口的节奏参数，现在按决策节拍自动推进"},
                        },
                        "required": ["goal"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "learn_key",
                    "description":
                        "把一个语义键名绑定到物理键并写进 profile（持久化，重启后仍生效）。"
                        "例：你观察到按 z 会推进对话，就可以 learn_key(name='confirm', physical=['z'])。"
                        "已经学到但想改绑，也用它覆盖。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "语义键名，如 confirm / cancel / up / menu"},
                            "physical": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "物理键名，取值：up/down/left/right/z/x/enter/esc/c/shift/space 等",
                            },
                            "confidence": {"type": "number", "description": "这次观察的可信度 0–1，默认 0.6"},
                        },
                        "required": ["name", "physical"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "learn_rule",
                    "description":
                        "记录一条游戏机制/规则并立刻重建系统提示词（持久化，重启后仍生效）。"
                        "例：你发现「对话气泡下方的小箭头表示还要再按一次确认」，就写进来。"
                        "键位用 learn_key，规则用 learn_rule——两者都会让提示词版本号 +1。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "规则原文，一句一义，不要写推测"},
                            "section": {"type": "string", "description": "归类，如 dialog / menu / battle，默认 general"},
                            "confidence": {"type": "number", "description": "这次观察的可信度 0–1，默认 0.6"},
                        },
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "restart_fast",
                    "description":
                        "重启快系统并验证刚学到的知识真的活着：清掉进程内提示词缓存 → 从磁盘重开 profile → "
                        "比对版本号与指纹 → 确认学到的键名出现在新提示词里。学完 learn_key/learn_rule 后调用一次。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "expect_key": {"type": "string", "description": "希望在新提示词里看到的键名，例如 confirm"},
                            "expect_version": {"type": "integer", "description": "期望的最低提示词版本号"},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "end_test",
                    "description":
                        "【仅开发环境】结束这一次测试运行：本轮收尾后停止 TUI。"
                        "正式直播里这个工具会被拒绝，正常结束请用 /quit。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {"type": "string", "description": "为什么结束这次测试（会写进日志）"},
                        },
                        "additionalProperties": False,
                    },
                },
            },
        ] + SLOW_TOOL_SPECS

    def learn_rule(self, text: str, section: str = "general", confidence: float = 0.6) -> ToolResult:
        """把一条游戏机制/规则写进 profile 并重建系统提示词（FR-7 回传链路）。

        与 learn_key 的区别：键位回答「按什么」，规则回答「为什么」。
        两者都走 learning._rebuild → prompt_builder.record_prompt_version，
        所以学完之后 config 里的 system_prompt_version / fingerprint 必然变化——
        这就是「学没学到」的可观测口径，不靠感觉。
        """
        if self.profile is None:
            return ToolResult("learn_rule", False, "当前没有 profile，无法记录规则")
        try:
            import learning
        except Exception as error:  # noqa: BLE001
            return ToolResult("learn_rule", False, f"learning 模块不可用：{type(error).__name__}: {error}")
        try:
            out = learning.learn_rule(
                self.profile, text, section=section or "general", confidence=float(confidence)
            )
        except Exception as error:  # noqa: BLE001 - 学习失败要如实回给模型
            return ToolResult("learn_rule", False, f"{type(error).__name__}: {error}")
        return ToolResult("learn_rule", True, json.dumps(out.as_dict(), ensure_ascii=False))

    def restart_fast(self, expect_key: str = "", expect_version: int = 0) -> ToolResult:
        """重启快系统并证明知识活着：丢掉缓存 → 重新读磁盘 → 比对版本与指纹。

        快系统现在是两条腿（手=gamescaling、嘴+裁判=Omni），"重启"要换掉的是：
        1) 本对象里缓存下来的执行器提示词（gamescaling 的 system_prompt）；
        2) prompt_builder 的渲染缓存；
        3) 探活两条腿：手能连上、嘴能说话。
        然后 verify_restart 从**磁盘**重新打开 profile 重建一遍，两边版本/指纹必须一致，
        且 keymap 里学到的键名确实出现在新提示词里。对不上一律 ok=False，不修饰。
        """
        if self.profile is None:
            return ToolResult("restart_fast", False, "当前没有 profile，无法重启快系统")
        if _os.environ.get("BETTERAGI_FAST", "1") in ("0", "false", "False"):
            return ToolResult("restart_fast", False, "BETTERAGI_FAST=0：快系统被显式禁用，重启也不会起来")
        dropped: List[str] = []
        try:
            import prompt_builder as _pb

            for attr in ("_CACHE", "_BUNDLE_CACHE", "_PROMPT_CACHE"):
                if hasattr(_pb, attr):
                    setattr(_pb, attr, {})
                    dropped.append("prompt_builder." + attr)
        except Exception:  # noqa: BLE001 - 没有缓存就是没有，不算错
            pass
        try:
            import learning as _learning

            check = _learning.verify_restart(
                self.profile.game_id,
                root=getattr(self.profile, "root", None) or _profiles_root(),
                expect_key=expect_key,
                expect_version=int(expect_version or 0),
            )
        except Exception as error:  # noqa: BLE001
            return ToolResult("restart_fast", False, f"重启校验失败：{type(error).__name__}: {error}")
        legs = self._fast_probe()
        check["dropped_caches"] = dropped
        check["fast_alive"] = bool(legs.get("motor")) and bool(legs.get("narrator"))
        check["legs"] = legs
        # 只有「磁盘重建一致 + 两条腿都重新起来」同时成立才算重启成功。
        ok = bool(check.get("ok")) and bool(check["fast_alive"])
        return ToolResult("restart_fast", ok, json.dumps(check, ensure_ascii=False))

    def learn_key(self, name: str, physical: List[str], confidence: float = 0.6) -> ToolResult:
        """把语义键名 → 物理键写进 profile（学习回填）。"""
        if self.profile is None:
            return ToolResult("learn_key", False, "当前没有 profile，无法记录键位")
        adapter_mod = _adapter_mod()
        ad = adapter_mod.ProfileAdapter(self.profile)
        try:
            out = ad.learn_key(name, physical, confidence)
        except Exception as error:  # noqa: BLE001 - 学习失败要如实回给模型
            return ToolResult("learn_key", False, f"{type(error).__name__}: {error}")
        return ToolResult("learn_key", True, json.dumps(out, ensure_ascii=False))

    # ---- 交棒与慢系统工具（02_schemas.md §3 / §9）----------------------
    def _session_events(self) -> List[Dict[str, Any]]:
        """会话事件流快照；没有 session（测试替身）就当空列表。"""
        session = getattr(self, "session", None)
        return list(getattr(session, "events", []) or [])

    def _session_stats(self) -> Dict[str, int]:
        """从事件流数真实的决策/动作/失败次数——交棒报告里的数字不许编。"""
        events = self._session_events()
        decisions = sum(1 for event in events if event.get("kind") == "assistant")
        actions = sum(
            1
            for event in events
            if event.get("kind") == "tool" and event.get("ok") and event.get("tool") in ("lumine", "act")
        )
        failures = sum(1 for event in events if event.get("kind") == "tool" and not event.get("ok"))
        return {"decisions": decisions, "actions": actions, "failures": failures}

    def _session_timing(self) -> Dict[str, int]:
        """时间侧车：这段意图花了多久、最慢一步多慢（都来自真实事件）。"""
        events = self._session_events()
        latencies = [int(event.get("latencyMs") or 0) for event in events if event.get("kind") == "assistant"]
        now_ms = int(time.time() * 1000)
        timing: Dict[str, int] = {
            "ended_ms": now_ms,
            "cycles": len(latencies),
            "slowest_step_ms": max(latencies) if latencies else 0,
        }
        started = getattr(self, "_intent_started_ms", None)
        if started:
            timing["started_ms"] = int(started)
            timing["intent_ms"] = max(0, now_ms - int(started))
        return timing

    def _publish_event(self, kind: str, payload: Dict[str, Any]) -> str:
        """把事件投到宿主接的 mailbox；没接或投失败返回空串，绝不假装发出去了。"""
        box = getattr(self, "mailbox", None)
        put = getattr(box, "put", None)
        if not callable(put):
            return ""
        session_id = str(getattr(getattr(self, "session", None), "session_id", "") or "")
        try:
            envelope = put(kind, dict(payload), {"from": "slow", "session_id": session_id})
        except Exception as error:  # noqa: BLE001 - 投递失败要留痕，交棒本身已经落盘
            self._trace("mailbox_error", kind=kind, error="%s: %s" % (type(error).__name__, error))
            return ""
        return str(getattr(envelope, "id", "") or "")




    def dispatch_task(self, task_markdown: str, done_hint: str = "", avoid: str = "") -> ToolResult:
        """把一段任务投给快系统：只投递、不等待（02 §9.3 的 100ms 语义）。

        宿主挂 dispatch_hook 就交给它；否则投 mailbox 的 intent.dispatch；
        两条通道都没接就明确报错——绝不让模型以为任务已经派出去了。
        """
        task = str(task_markdown or "").strip()
        if not task:
            return ToolResult("dispatch_task", False, "task_markdown 不能为空：要给快系统一段能执行的任务说明")
        intent_id = new_intent_id()
        started_ms = int(time.time() * 1000)
        packet = {
            "intent_id": intent_id,
            "session_id": str(getattr(getattr(self, "session", None), "session_id", "") or ""),
            "goal": task,
            "done_hint": str(done_hint or ""),
            "avoid": str(avoid or ""),
            "started_ms": started_ms,
            "sticky": self._sticky_snapshot(),
        }
        hook = getattr(self, "dispatch_hook", None)
        if callable(hook):
            try:
                detail = hook(dict(packet))
            except Exception as error:  # noqa: BLE001 - 投递失败要如实上报
                return ToolResult("dispatch_task", False, "投递意图失败：%s: %s" % (type(error).__name__, error))
            self.current_intent_id = intent_id
            self._intent_started_ms = started_ms
            out = {"ok": True, "channel": "dispatch_hook", "intent_id": intent_id, "detail": detail}
            return ToolResult("dispatch_task", True, json.dumps(out, ensure_ascii=False, default=str))
        box = getattr(self, "mailbox", None)
        put = getattr(box, "put", None)
        if callable(put):
            try:
                envelope = put("intent.dispatch", dict(packet), {"from": "slow", "session_id": packet["session_id"]})
            except Exception as error:  # noqa: BLE001
                return ToolResult("dispatch_task", False, "投递意图失败：%s: %s" % (type(error).__name__, error))
            self.current_intent_id = intent_id
            self._intent_started_ms = started_ms
            out = {
                "ok": True,
                "channel": "mailbox",
                "intent_id": intent_id,
                "mailbox_id": str(getattr(envelope, "id", "") or ""),
            }
            return ToolResult("dispatch_task", True, json.dumps(out, ensure_ascii=False))
        return ToolResult(
            "dispatch_task", False,
            "快系统投递通道未接线：宿主既没挂 dispatch_hook，也没接 mailbox，任务没有真的发出去。",
        )

    def _sticky_root(self, root: Any = None) -> Any:
        """便利贴根目录：显式 root > profile 目录（<profile>/sticky/<game>/<save>.md）。

        不传根目录时 sticky 会退回「当前目录/run」，读到的不是同一份文件——
        手模型与慢系统的 read/write_sticky 必须落在同一处，否则学会了也不生效。
        """

        if root:
            return root
        return getattr(getattr(self, "profile", None), "dir", None)

    def _sticky_snapshot(self) -> Dict[str, Any]:
        """给意图包附上便利贴快照；读不到就空 dict（不编内容，也不阻断派活）。"""
        save_id = str(getattr(self, "current_save_id", "") or "")
        game_id = str(getattr(getattr(self, "profile", None), "game_id", "") or "")
        if not save_id or not game_id:
            return {}
        try:
            sticky_mod = _sibling_module("sticky")
            info = sticky_mod.read_sticky(game_id, save_id, root=self._sticky_root())
        except Exception:  # noqa: BLE001 - 便利贴只是附加信息
            return {}
        return {
            "key_binding": str(info.get("key_binding") or ""),
            "game_rules": str(info.get("game_rules") or ""),
        }

    def read_sticky(self, save_id: str = "", game: str = "", root: Any = None) -> ToolResult:
        """读便利贴（key_binding + game_rules）。sticky 模块不可用就明确失败。"""
        sid = str(save_id or getattr(self, "current_save_id", "") or "").strip()
        if not sid:
            return ToolResult("read_sticky", False, "save_id 必填：便利贴按存档分开存，没有 save_id 不知道该读哪一份")
        # 与 write_sticky 同一个口径：慢系统偶尔自己编 save_id（实测读/写都用过
        # "undertale"），知识会被劈成两份、手上那份永远是空的。这里静默纠正。
        current_save = str(getattr(self, "current_save_id", "") or "").strip()
        if current_save and sid != current_save:
            sid = current_save
        game_id = str(game or getattr(getattr(self, "profile", None), "game_id", "") or "").strip()
        if not game_id:
            return ToolResult("read_sticky", False, "没有游戏名（profile 为空且调用没给 game），不知道该读哪个游戏的便利贴")
        try:
            sticky_mod = _sibling_module("sticky")
        except ImportError as error:
            return ToolResult("read_sticky", False, "sticky 模块不可用：%s: %s" % (type(error).__name__, error))
        try:
            info = sticky_mod.read_sticky(game_id, sid, root=self._sticky_root(root))
        except Exception as error:  # noqa: BLE001 - 读失败要如实回给模型
            return ToolResult("read_sticky", False, "读便利贴失败：%s: %s" % (type(error).__name__, error))
        out = {"ok": True, "game": game_id, "save_id": sid, "sticky": info}
        return ToolResult("read_sticky", True, json.dumps(out, ensure_ascii=False, default=str))

    def write_sticky(
        self,
        save_id: str = "",
        key_binding: Optional[str] = None,
        game_rules: Optional[str] = None,
        replace: bool = False,
        game: str = "",
        root: Any = None,
    ) -> ToolResult:
        """写便利贴（慢系统专属写者）。超配额/非法内容由 sticky 拒绝，这里原样上报。"""
        sid = str(save_id or getattr(self, "current_save_id", "") or "").strip()
        if not sid:
            return ToolResult("write_sticky", False, "save_id 必填：便利贴按存档分开存")
        # 存档号必须跟运行时一致：慢系统偶尔会自己编一个（实测写了 save_id=undertale），
        # 于是知识被劈成两份，手上看到的还是 default 那份。这里静默纠正成当前存档。
        current_save = str(getattr(self, "current_save_id", "") or "").strip()
        if current_save and sid != current_save:
            sid = current_save
        if key_binding is None and game_rules is None:
            return ToolResult("write_sticky", False, "至少要给一块内容：key_binding 或 game_rules")
        game_id = str(game or getattr(getattr(self, "profile", None), "game_id", "") or "").strip()
        if not game_id:
            return ToolResult("write_sticky", False, "没有游戏名（profile 为空且调用没给 game），不知道该写哪个游戏的便利贴")
        # 用户 2026-11-05：操作模型只吃英文。便利贴是它的 system，中文会让它用中文思考，
        # 中文 reasoning 里的换行直接产出非法 JSON（实测解析失败的元凶）。
        cjk = _first_cjk((key_binding or "") + "\n" + (game_rules or ""))
        if cjk:
            return ToolResult(
                "write_sticky", False,
                "便利贴必须全英文：发现中文字符「%s」。把 key_binding 与 game_rules 改写成英文再写"
                "（例：- Z -> confirm / enter；- Arrow keys -> move cursor）。" % cjk)
        try:
            sticky_mod = _sibling_module("sticky")
        except ImportError as error:
            return ToolResult("write_sticky", False, "sticky 模块不可用：%s: %s" % (type(error).__name__, error))
        # 用户 2026-11-05：便利贴写入**异步**——慢系统每轮都写会一直拖时间。
        # 这里只做参数校验和入队，真正的整块重写交给后台单线程按顺序落盘；
        # 写失败不回灌到本轮（下一轮读便利贴时会看到旧内容，并可在 warnings 里说）。
        rules_root = self._sticky_root(root)
        game_rules = self._keep_fact_lines(sticky_mod, game_id, sid, game_rules, rules_root)
        self._queue_sticky_write(sticky_mod, game_id, sid, key_binding, game_rules,
                                 bool(replace), rules_root)
        out = {"ok": True, "queued": True, "game": game_id, "save_id": sid,
               "note": "已提交后台写入，立刻继续规划，不要等结果、也不要重复提交"}
        return ToolResult("write_sticky", True, json.dumps(out, ensure_ascii=False, default=str))

    def _keep_fact_lines(self, sticky_mod: Any, game_id: str, save_id: str,
                         game_rules: Optional[str], root: Any) -> Optional[str]:
        """把带 [FACT] 的行保住：那些是程序/人工核过的事实，慢系统整块重写不该抹掉。

        实测慢系统连续两轮把「命名界面怎么按」和「NO MOUSE」这类硬事实洗掉，之后就只会
        瞎试。这里在写盘前把旧文件里的 [FACT] 行原样拼回新内容的开头。
        """
        if game_rules is None:
            return None
        try:
            info = sticky_mod.read_sticky(game_id, save_id, root=root) or {}
        except Exception:  # noqa: BLE001 - 读不到旧的就按新的写
            return game_rules
        old_text = str(info.get("game_rules") or "")
        facts = [line for line in old_text.splitlines() if "[FACT]" in line]
        if not facts:
            return game_rules
        body = [line for line in str(game_rules).splitlines() if "[FACT]" not in line]
        missing = [line for line in facts if line not in body]
        return "\n".join(missing + body) if missing else game_rules

    def _queue_sticky_write(self, sticky_mod: Any, game_id: str, save_id: str,
                            key_binding: Optional[str], game_rules: Optional[str],
                            replace: bool, root: Any) -> None:
        """把一次便利贴重写排进后台队列（单 worker 保证先后顺序）。"""
        queue_obj = getattr(self, "_sticky_queue", None)
        if queue_obj is None:
            queue_obj = queue.Queue()
            self._sticky_queue = queue_obj
            self._sticky_last_error = ""
            self._sticky_written = 0

            def worker() -> None:
                while True:
                    job = queue_obj.get()
                    try:
                        if job is None:
                            return
                        job["sticky_mod"].write_sticky(
                            job["game"], job["save_id"],
                            key_binding=job["key_binding"],
                            game_rules=job["game_rules"],
                            root=job["root"], replace=job["replace"],
                        )
                        self._sticky_written += 1
                    except Exception as error:  # noqa: BLE001 - 后台失败只留痕，不打断直播
                        self._sticky_last_error = "%s: %s" % (type(error).__name__, error)
                    finally:
                        queue_obj.task_done()

            threading.Thread(target=worker, name="sticky-writer", daemon=True).start()
        queue_obj.put({
            "sticky_mod": sticky_mod, "game": game_id, "save_id": save_id,
            "key_binding": key_binding, "game_rules": game_rules,
            "root": root, "replace": replace,
        })

    def sticky_write_stats(self) -> Dict[str, Any]:
        """后台便利贴写入的自检口：排了几次、成功几次、最后一次错什么。"""
        queue_obj = getattr(self, "_sticky_queue", None)
        pending = 0
        if queue_obj is not None:
            try:
                pending = int(queue_obj.unfinished_tasks)
            except Exception:  # noqa: BLE001
                pending = 0
        return {"pending": pending,
                "written": int(getattr(self, "_sticky_written", 0)),
                "last_error": getattr(self, "_sticky_last_error", "")}

    def _memory(self) -> Any:
        """惰性建记忆后端（每实例缓存）；失败抛异常，由调用方转成明确错误。"""
        cached = getattr(self, "_memory_backend", None)
        if cached is not None:
            return cached
        profile = getattr(self, "profile", None)
        if profile is None:
            raise RuntimeError("当前没有 profile，无法定位记忆库（记忆按游戏分区存）")
        memory_mod = _sibling_module("memory")
        backend = memory_mod.build_memory(profile)
        self._memory_backend = backend
        return backend

    def search_memory(self, query: str, top_k: int = 8, save_id: str = "") -> ToolResult:
        """查长期记忆：返回最相关条目（带来源/置信度），查不到就回空列表而不是编答案。"""
        text = str(query or "").strip()
        if not text:
            return ToolResult("search_memory", False, "query 不能为空：要查什么得说清")
        try:
            backend = self._memory()
        except ImportError as error:
            return ToolResult("search_memory", False, "memory 模块不可用：%s: %s" % (type(error).__name__, error))
        except Exception as error:  # noqa: BLE001
            return ToolResult("search_memory", False, "记忆后端不可用：%s: %s" % (type(error).__name__, error))
        try:
            size = max(1, min(32, int(top_k or 8)))
        except (TypeError, ValueError):
            return ToolResult("search_memory", False, "top_k 必须是整数，实际是 %r" % (top_k,))
        try:
            episodes = backend.search(text, top_k=size, include_spoiler=False)
        except Exception as error:  # noqa: BLE001
            return ToolResult("search_memory", False, "检索记忆失败：%s: %s" % (type(error).__name__, error))
        items = [episode.as_dict() for episode in episodes]
        out: Dict[str, Any] = {"ok": True, "query": text, "count": len(items), "items": items}
        if save_id:
            out["note"] = "save_id=%s 已收到；当前后端按游戏分区存储，没有按存档隔离" % save_id
        return ToolResult("search_memory", True, json.dumps(out, ensure_ascii=False, default=str))

    def write_memory(
        self,
        kind: str,
        text: str,
        confidence: float = 1.0,
        save_id: str = "",
    ) -> ToolResult:
        """记一条长期记忆。kind 决定落到哪一层，返回里写明真实存放形态。"""
        memory_kind = str(kind or "").strip()
        if memory_kind not in WRITE_MEMORY_KINDS:
            return ToolResult(
                "write_memory", False,
                "kind 只能是 %s，实际是 %r" % ("/".join(WRITE_MEMORY_KINDS), kind),
            )
        body = str(text or "").strip()
        if not body:
            return ToolResult("write_memory", False, "text 不能为空：要记的事得写出来")
        try:
            score = float(confidence)
        except (TypeError, ValueError):
            return ToolResult("write_memory", False, "confidence 必须是 0–1 的数，实际是 %r" % (confidence,))
        if not 0.0 <= score <= 1.0:
            return ToolResult("write_memory", False, "confidence 必须在 0–1 之间，实际是 %s" % score)
        episode_kind, layer = WRITE_MEMORY_KINDS[memory_kind]
        try:
            backend = self._memory()
            memory_mod = _sibling_module("memory")
        except ImportError as error:
            return ToolResult("write_memory", False, "memory 模块不可用：%s: %s" % (type(error).__name__, error))
        except Exception as error:  # noqa: BLE001
            return ToolResult("write_memory", False, "记忆后端不可用：%s: %s" % (type(error).__name__, error))
        meta: Dict[str, Any] = {"memory_kind": memory_kind}
        if save_id:
            meta["save_id"] = str(save_id)
        episode = memory_mod.Episode(
            text=body,
            kind=episode_kind,
            layer=layer,
            source="slow",
            confidence=score,
            meta=meta,
        )
        try:
            saved = backend.add_episode(episode)
        except Exception as error:  # noqa: BLE001
            return ToolResult("write_memory", False, "写入记忆失败：%s: %s" % (type(error).__name__, error))
        out = {
            "ok": True,
            "id": str(getattr(saved, "id", "") or ""),
            "kind": memory_kind,
            "stored_kind": episode_kind,
            "layer": layer,
            "confidence": score,
            "text": body,
        }
        return ToolResult("write_memory", True, json.dumps(out, ensure_ascii=False, default=str))

    def read_danmaku(self, limit: int = 30, since_ms: Any = None) -> ToolResult:
        """读本地弹幕服务。服务不通就明确返回错误，不编造观众说的话。"""
        try:
            feed_mod = _sibling_module("danmaku_feed")
        except ImportError as error:
            return ToolResult("read_danmaku", False, "danmaku_feed 模块不可用：%s: %s" % (type(error).__name__, error))
        try:
            size = max(1, min(200, int(limit or 30)))
        except (TypeError, ValueError):
            return ToolResult("read_danmaku", False, "limit 必须是整数，实际是 %r" % (limit,))
        since: Optional[int] = None
        if since_ms not in (None, ""):
            try:
                since = int(since_ms)
            except (TypeError, ValueError):
                return ToolResult("read_danmaku", False, "since_ms 必须是 epoch 毫秒，实际是 %r" % (since_ms,))
        # 同进程主持播时弹幕就在内存环形缓冲里（tui 传进来的 hub），不必再绕一圈 HTTP；
        # 跨进程（弹幕服务是别的代理）才走 danmaku_feed。
        hub = getattr(self, "danmaku_hub", None)
        if hub is not None:
            items = hub.since(since or 0, size)
            out = {"ok": True, "items": items, "since_ms": since,
                   "elapsed_ms": 0, "source": "hub",
                   "stats": hub.stats() if hasattr(hub, "stats") else {}}
            return ToolResult("read_danmaku", True, json.dumps(out, ensure_ascii=False, default=str))
        try:
            feed = feed_mod.build_feed()
            status = feed.fetch(since_ms=since, limit=size)
        except Exception as error:  # noqa: BLE001 - 接口不通要说清
            return ToolResult("read_danmaku", False, "读弹幕失败：%s: %s" % (type(error).__name__, error))
        if not getattr(status, "ok", False):
            return ToolResult(
                "read_danmaku", False,
                "弹幕服务不可用：%s（耗时 %sms）"
                % (getattr(status, "error", "") or "没有错误信息", getattr(status, "elapsed_ms", 0)),
            )
        out = {
            "ok": True,
            "items": list(getattr(status, "items", []) or []),
            "since_ms": getattr(status, "since_ms", since),
            "elapsed_ms": getattr(status, "elapsed_ms", 0),
        }
        return ToolResult("read_danmaku", True, json.dumps(out, ensure_ascii=False, default=str))

    def get_last_episode(self, game: str = "") -> ToolResult:
        """上一轮见闻：优先取会话里最后一次交棒，没有交棒再退回记忆库最新一条。"""
        game_id = str(game or getattr(getattr(self, "profile", None), "game_id", "") or "")
        for event in reversed(self._session_events()):
            if event.get("kind") == "handoff":
                payload = {key: value for key, value in event.items() if key not in ("ts", "kind")}
                out = {"ok": True, "source": "session.handoff", "game": game_id, "episode": payload}
                return ToolResult("get_last_episode", True, json.dumps(out, ensure_ascii=False, default=str))
        try:
            backend = self._memory()
        except ImportError as error:
            return ToolResult("get_last_episode", False, "memory 模块不可用：%s: %s" % (type(error).__name__, error))
        except Exception as error:  # noqa: BLE001
            return ToolResult(
                "get_last_episode", False,
                "还没有上一轮见闻：会话里没有 handoff，记忆后端也不可用（%s: %s）" % (type(error).__name__, error),
            )
        try:
            episodes = backend.load() if hasattr(backend, "load") else []
        except Exception as error:  # noqa: BLE001
            return ToolResult("get_last_episode", False, "读记忆库失败：%s: %s" % (type(error).__name__, error))
        if not episodes:
            return ToolResult(
                "get_last_episode", False,
                "还没有上一轮见闻：会话里没有 handoff，记忆库也是空的（不编一段复盘给你）",
            )
        latest = max(episodes, key=lambda item: float(getattr(item, "ts", 0) or 0))
        out = {"ok": True, "source": "memory", "game": game_id, "episode": latest.as_dict()}
        return ToolResult("get_last_episode", True, json.dumps(out, ensure_ascii=False, default=str))

    def _store_plan_like(self, tool_name: str, text: str, meta: Dict[str, Any], layer: str) -> ToolResult:
        """把跨场钩子/企划写成一条真实的 strategy 记忆条目。

        独立的 Plan 存储（02 §4）还没接线，所以这里不假装"计划已进入调度"：
        真的写进记忆库就返回 id，写不进去就明确失败。
        """
        body = str(text or "").strip()
        if not body:
            return ToolResult(tool_name, False, "text 不能为空：钩子/企划得写清内容")
        try:
            backend = self._memory()
            memory_mod = _sibling_module("memory")
        except ImportError as error:
            return ToolResult(tool_name, False, "memory 模块不可用：%s: %s" % (type(error).__name__, error))
        except Exception as error:  # noqa: BLE001
            return ToolResult(tool_name, False, "记忆后端不可用：%s: %s" % (type(error).__name__, error))
        payload_meta = dict(meta)
        payload_meta["tool"] = tool_name
        episode = memory_mod.Episode(
            text=body,
            kind="strategy",
            layer=layer,
            source="slow",
            confidence=1.0,
            meta=payload_meta,
        )
        try:
            saved = backend.add_episode(episode)
        except Exception as error:  # noqa: BLE001
            return ToolResult(tool_name, False, "写入失败：%s: %s" % (type(error).__name__, error))
        out = {
            "ok": True,
            "id": str(getattr(saved, "id", "") or ""),
            "store": "memory.episode",
            "text": body,
            "meta": payload_meta,
            "note": "独立的 Plan/钩子存储尚未接线：这条已作为 strategy 记忆条目真实落盘，"
                    "search_memory 能找回，但不会自动进快系统调度。",
        }
        return ToolResult(tool_name, True, json.dumps(out, ensure_ascii=False, default=str))

    def set_hook(self, text: str, due_session: str = "") -> ToolResult:
        """埋一个跨场钩子（伏笔/回马枪）。"""
        meta = {"plan_kind": "hook", "status": "open", "due_session": str(due_session or "")}
        return self._store_plan_like("set_hook", text, meta, "episodic")

    def set_plan(self, text: str, status: str = "open") -> ToolResult:
        """记一条跨场企划，或把它标成完成/放弃。"""
        state = str(status or "open").strip()
        if state not in PLAN_STATUSES:
            return ToolResult("set_plan", False, "status 只能是 %s，实际是 %r" % ("/".join(PLAN_STATUSES), status))
        meta = {"plan_kind": "plan", "status": state}
        return self._store_plan_like("set_plan", text, meta, "procedural")

    def dispatch(self, name: str, arguments: Dict[str, Any]) -> ToolResult:
        if name == "scan":
            return self.scan(arguments.get("regions") or "quest,message,options,hp,uid")
        if name == "look":
            # 通用通道（profile 描述的窗口）：慢系统自己就是多模态模型，**直接把图给它**。
            # 快系统是"手"不是"眼"——它的判断只服务于它自己执行意图的过程，
            # 唯一应该回传慢系统的是"这段时间发生了什么"的总结，不是逐帧读屏。
            if self.profile is not None and self.profile.game_id != "genshin-impact":
                return self.generic_look()
            return self.look()
        if name == "learn_rule":
            return self.learn_rule(
                str(arguments.get("text") or ""),
                str(arguments.get("section") or "general"),
                float(arguments.get("confidence") or 0.6),
            )
        if name == "end_test":
            return self.end_test(str(arguments.get("reason") or ""))
        if name == "restart_fast":
            return self.restart_fast(
                str(arguments.get("expect_key") or ""),
                int(arguments.get("expect_version") or 0),
            )
        if name == "learn_key":
            return self.learn_key(
                str(arguments.get("name") or ""),
                [str(k) for k in (arguments.get("physical") or [])],
                float(arguments.get("confidence") or 0.6),
            )
        if name == "lumine":
            # 唯一的动作入口：慢系统只交意图（+ 可选文字负载），按键全由操作器负责。
            return self.lumine(
                str(arguments["goal"]),
                int(arguments.get("seconds") or 60),
                str(arguments.get("motor_mode") or "navigate"),
                done_when=str(arguments.get("done_when") or ""),
                text=str(arguments.get("text") or ""),
                max_steps=int(arguments.get("max_steps") or 10),
                wait=float(arguments.get("wait") or 0.7),
            )
        if name == "run_script":
            # 固定流程走脚本，不叫 lumine（省 token、省 200ms 决策环）。
            # 模型只给自然语言 goal；精确脚本由 tui 层另发一次思考请求生成后塞进 script。
            return self.run_script(
                goal=str(arguments.get("goal") or ""),
                script=str(arguments.get("script") or ""),
            )
        # ---- 交棒与慢系统工具：名字严格匹配，拼错一律走到最后的 ValueError ----
        if name == "dispatch_task":
            return self.dispatch_task(
                str(arguments.get("task_markdown") or ""),
                str(arguments.get("done_hint") or ""),
                str(arguments.get("avoid") or ""),
            )
        if name == "read_sticky":
            return self.read_sticky(
                str(arguments.get("save_id") or ""),
                str(arguments.get("game") or ""),
            )
        if name == "write_sticky":
            return self.write_sticky(
                str(arguments.get("save_id") or ""),
                None if arguments.get("key_binding") is None else str(arguments.get("key_binding")),
                None if arguments.get("game_rules") is None else str(arguments.get("game_rules")),
                replace=bool(arguments.get("replace") or False),
            )
        if name == "search_memory":
            return self.search_memory(
                str(arguments.get("query") or ""),
                int(arguments.get("top_k") or 8),
                str(arguments.get("save_id") or ""),
            )
        if name == "write_memory":
            return self.write_memory(
                str(arguments.get("kind") or ""),
                str(arguments.get("text") or ""),
                float(arguments.get("confidence") or 1.0),
                str(arguments.get("save_id") or ""),
            )
        if name == "read_danmaku":
            return self.read_danmaku(
                int(arguments.get("limit") or 30),
                arguments.get("since_ms"),
            )
        if name == "get_last_episode":
            return self.get_last_episode(str(arguments.get("game") or ""))
        if name == "set_hook":
            return self.set_hook(
                str(arguments.get("text") or ""),
                str(arguments.get("due_session") or ""),
            )
        if name == "set_plan":
            return self.set_plan(
                str(arguments.get("text") or ""),
                str(arguments.get("status") or "open"),
            )
        raise ValueError(f"未知工具 {name}")


def run(
    goal: str,
    *,
    steps: int = 6,
    urgency: float = 0.5,
    dry_run: bool = False,
    log_root: Path = Path("E:/BetterAGI/_diag/agent-runs"),
    game: str = "",
) -> Dict[str, Any]:
    """跑一轮闭环，返回最后的结论与事件数。

    ``game`` 给 profile 名（如 undertale）就切到通用通道：工具面由 profile 决定，
    感知走快系统、操作走 finish_intent。给空则是 BetterGI / 原神历史通道。
    """
    session = Session(root=log_root)
    profile = None
    if game:
        from profiles import open_profile

        profile = open_profile(game)
    tools = BetterGITools(session, profile=profile)
    client = lithos.LithosClient()
    prompt_text, prompt_meta = resolve_system_prompt(game)
    session.append(
        "start", goal=goal, steps=steps, urgency=urgency,
        tier=lithos.choose_tier(urgency), prompt=prompt_meta,
    )

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": prompt_text},
        {"role": "user", "content": f"目标：{goal}\n先看清楚现在在哪（通用游戏用 screen），然后决定第一步。"},
    ]

    final: Dict[str, Any] = {"ok": False, "summary": None, "steps": 0}
    # 先保证游戏在前台：严格安全下，非前台的画面会被识别成 Unknown，
    # 模型看到"不知道在哪"就会乱走。
    if not dry_run:
        prepare = getattr(tools, "ensure_foreground", None)
        if callable(prepare):
            session.append("foreground", **prepare())
    for step in range(1, steps + 1):
        if dry_run:
            result = tools.dispatch("scan", {})
            session.append("tool", step=step, tool=result.name, ok=result.ok, text=result.text)
            final.update({"ok": True, "summary": "dry-run 只跑了感知", "steps": step})
            break

        # 工具面由 profile 决定；测试替身没有 tool_specs 时退回默认面。
        specs = tools.tool_specs() if hasattr(tools, "tool_specs") else TOOL_SPECS
        reply = client.complete(messages, urgency=urgency, tools=specs)
        session.append(
            "assistant",
            step=step,
            tier=reply.tier,
            model=reply.model,
            latencyMs=reply.latency_ms,
            text=lithos.redact(reply.text, 2000),
            toolCalls=[call.get("function", {}).get("name") for call in reply.tool_calls],
        )
        messages.append(reply.as_message())
        if not reply.tool_calls:
            final.update({"ok": True, "summary": reply.text, "steps": step})
            break

        # 一条 assistant 消息里的所有 tool_calls 必须紧跟同数量的 tool 消息，
        # 否则下一次请求会被服务端拒绝（insufficient tool messages）。
        # 所以先逐个应答完，再把图片作为单独一条 user 消息追加在后面。
        finished_summary: Optional[str] = None
        pending_images: List[str] = []
        for call in reply.tool_calls:
            name = call["function"]["name"]
            try:
                arguments = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            if finished_summary is not None:
                messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": "本轮已结束，未执行。"}
                )
                continue
            try:
                result = tools.dispatch(name, arguments)
            except Exception as error:  # noqa: BLE001 - 工具失败要如实回给模型
                result = ToolResult(name, False, f"{type(error).__name__}: {error}")
            session.append("tool", step=step, tool=result.name, ok=result.ok, text=result.text[:2000])
            if result.image:
                pending_images.append(result.image)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result.text if result.ok else f"失败：{result.text}",
                }
            )
        if finished_summary is not None:
            final.update({"ok": True, "summary": finished_summary, "steps": step})
            break
        if pending_images:
            content: List[Any] = [{"type": "text", "text": "上面工具返回的画面："}]
            for data_url in pending_images:
                content.append({"type": "image_url", "image_url": {"url": data_url}})
            messages.append({"role": "user", "content": content})

    if not final["ok"] and final["steps"] == 0 and not dry_run:
        # 跑到步数上限仍没给 finish：如实记录"用完了步数"，别伪装成成功。
        final.update({"steps": steps, "summary": "达到步数上限，模型没有给出 finish 结论"})
    session.append("end", ok=final["ok"], summary=final.get("summary"), steps=final["steps"])
    final["log"] = str(session.path())
    return final


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="BetterAGI 最小闭环 agent（LithosAI 三档）")
    parser.add_argument("--goal", help="这一轮要做的事")
    parser.add_argument("--goal-file", help="从 UTF-8 文件读目标（避免 PowerShell 传参把中文变成 GBK 乱码）")
    parser.add_argument("--steps", type=int, default=6, help="最多几步，默认 6")
    parser.add_argument("--urgency", type=float, default=0.5, help="紧迫度 0–1，决定用 base/fast/ultra")
    parser.add_argument("--dry-run", action="store_true", help="只跑感知，不调用模型")
    parser.add_argument("--log-root", default="E:/BetterAGI/_diag/agent-runs")
    parser.add_argument("--game", default="", help="游戏 profile 名，例如 undertale；留空走 BetterGI/原神通道")
    args = parser.parse_args(argv)
    goal = (args.goal or "").strip()
    if args.goal_file:
        goal = Path(args.goal_file).read_text(encoding="utf-8").strip()
    if not goal:
        print("必须给 --goal 或 --goal-file", file=sys.stderr)
        return 2

    if not args.dry_run and not lithos.LithosConfig.from_env().available():
        print("缺少 LITHOS_API_KEY：先设置环境变量，或加 --dry-run 只跑感知", file=sys.stderr)
        return 2

    result = run(
        goal,
        steps=max(1, args.steps),
        urgency=args.urgency,
        dry_run=args.dry_run,
        log_root=Path(args.log_root),
        game=args.game,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
