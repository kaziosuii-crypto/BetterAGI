#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""玩家档案（profile）：加载、校验、稳定摘要与默认档案落盘。

档案描述的是「这个玩家是谁、重视什么、怎么玩、怎么说话」，不是当前剧情
状态。它应当低频修改、只读加载；运行时状态在 ``player_state`` 里。

安全约束：
- 档案中不允许出现令牌/密钥类字段；
- 缺失档案时只有默认路径会生成默认档案，自定义路径缺失直接报错，
  避免路径写错后静默换成另一套人格。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from player_io import atomic_write_text, clean_text, read_json

SCHEMA_VERSION = 1

# 默认档案模板；首次运行时复制到 PLAYER_HOME/profile/profile.json。
DEFAULT_PROFILE_TEMPLATE = Path(__file__).resolve().with_name("profiles") / "lumi.json"

# 精确匹配的敏感键名（已统一为小写、下划线形式）。
_SECRET_KEYS = {
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "secret_key",
    "key_id",
    "password",
    "secret",
    "client_secret",
    "authorization",
    "credential",
    "credentials",
}

# 档案是玩家身份，不是世界百科；这些键一旦出现，说明有人把未解锁的
# 世界真相/剧透塞进了 prompt 的稳定部分。
_FORBIDDEN_KEYS = {
    "world_only",
    "ground_truth",
    "world_database",
    "spoiler",
    "spoilers",
}


class ProfileError(ValueError):
    """玩家档案缺失、格式错误或违反硬约束。"""


_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])([A-Z])")


def _normalize_key(key: Any) -> str:
    """统一键名风格：accessToken / access-token / access_token 视为同一个键。"""
    text = _CAMEL_BOUNDARY.sub(r"_\1", str(key).strip())
    return text.lower().replace("-", "_")


def is_secret_key(key: Any) -> bool:
    """判断键名是否属于令牌/密钥类字段。"""
    name = _normalize_key(key)
    return (
        name in _SECRET_KEYS
        or name.endswith("_token")
        or name.endswith("_secret")
        or name.endswith("_password")
        or name.endswith("_api_key")
        or name.endswith("_access_key")
        or name.endswith("_private_key")
        or name.endswith("_secret_key")
        or name.endswith("_key_id")
    )


def find_secret_fields(value: Any, prefix: str = "") -> List[str]:
    """递归找出令牌/密钥类字段路径，返回可供错误信息使用的点分路径。"""
    found: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = "{0}.{1}".format(prefix, key) if prefix else str(key)
            if is_secret_key(key):
                found.append(path)
            found.extend(find_secret_fields(item, path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(find_secret_fields(item, "{0}[{1}]".format(prefix, index)))
    return found


def find_forbidden_fields(value: Any, prefix: str = "") -> List[str]:
    """递归找出世界真相/剧透字段路径。"""
    found: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            name = _normalize_key(key)
            path = "{0}.{1}".format(prefix, key) if prefix else str(key)
            if name in _FORBIDDEN_KEYS:
                found.append(path)
            found.extend(find_forbidden_fields(item, path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(find_forbidden_fields(item, "{0}[{1}]".format(prefix, index)))
    return found


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ProfileError("字段必须是字符串，实际为 {0}".format(type(value).__name__))
    return clean_text(value).strip()


def _as_text_list(value: Any, *, limit: int = 20, item_limit: int = 200) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ProfileError("字段必须是字符串数组")
    items: List[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ProfileError("数组元素必须是字符串")
        text = " ".join(clean_text(item).split())
        if text:
            items.append(text[:item_limit])
        if len(items) >= limit:
            break
    return items


def _as_mapping(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProfileError("字段必须是 JSON 对象")
    return dict(value)


def _as_ratio(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, number))


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, number))


# 内部枚举到中文提示的映射：注入提示词的只有自然语言，不出现裸数值/原始枚举。
_PACE_TEXT = {
    "exploratory": "边走边看，喜欢先摸清周围",
    "focused": "有目标时推进得比较专注",
    "completionist": "喜欢把一片区域清干净再走",
}
_COMBAT_TEXT = {
    "avoid": "能绕开就绕开",
    "cautious_but_curious": "打得谨慎，但会想试试新敌人",
    "aggressive": "遇到敌人会主动上",
}
_EXPLORATION_TEXT = {
    "check_corners": "爱翻角落和岔路",
    "follow_path": "更愿意沿主路走",
    "landmark": "喜欢先找地标再决定方向",
}
_PUZZLE_TEXT = {
    "try_first_then_look_around": "先自己试，卡住再找线索",
    "read_hints_first": "先看提示再动手",
    "brute_force": "喜欢穷举试错",
}
_QUEST_TEXT = {
    "follow_curiosity": "跟着好奇心走",
    "follow_main": "优先推进主线",
    "side_first": "先把支线清一清",
}
_SILENCE_TEXT = {
    "low": "比较愿意说话",
    "medium": "该说的时候说，不硬找话题",
    "high": "偏安静，只在值得时开口",
}


@dataclass(frozen=True)
class PlayerProfile:
    """不可变的玩家身份档案；运行时只读。"""

    schema_version: int
    player_id: str
    identity: Dict[str, Any]
    personality: Dict[str, Any]
    values: List[str]
    play_style: Dict[str, Any]
    preferences: Dict[str, Any]
    speech_style: Dict[str, Any]
    long_term_interests: List[str]
    self_history: Dict[str, Any]
    boundaries: Dict[str, Any]
    updated_at: str = ""
    source_path: Optional[Path] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def display_name(self) -> str:
        return str(self.identity.get("name") or self.player_id)

    def stable_summary(self) -> str:
        """生成与时间/进程无关的稳定人格摘要，供每轮 system prompt 使用。"""
        lines: List[str] = []
        identity = self.identity
        opening = "你是「{0}」".format(identity.get("name") or self.player_id)
        if identity.get("origin"):
            opening += "，{0}".format(identity["origin"])
        lines.append(opening + "。")
        if identity.get("self_description"):
            lines.append("你是谁：{0}".format(identity["self_description"]))

        personality = self.personality
        traits = personality.get("traits") or []
        if traits:
            lines.append("性格：{0}".format("、".join(traits)))
        if personality.get("flaws"):
            lines.append("容易犯的毛病：{0}".format("、".join(personality["flaws"])))
        if personality.get("quirks"):
            lines.append("小习惯：{0}".format("、".join(personality["quirks"])))
        if personality.get("baseline_mood"):
            lines.append("平时的状态：{0}".format(personality["baseline_mood"]))

        if self.values:
            lines.append("你在意的事：{0}".format("；".join(self.values)))

        style = self.play_style
        style_parts: List[str] = []
        if style.get("pace"):
            style_parts.append(_PACE_TEXT.get(style["pace"], str(style["pace"])))
        if style.get("risk_tolerance") is not None:
            risk = float(style["risk_tolerance"])
            if risk < 0.35:
                style_parts.append("风险上偏谨慎")
            elif risk > 0.65:
                style_parts.append("愿意冒一点险")
            else:
                style_parts.append("风险和收益之间比较均衡")
        for key, mapping in (
            ("combat", _COMBAT_TEXT),
            ("exploration", _EXPLORATION_TEXT),
            ("puzzle", _PUZZLE_TEXT),
            ("quest_focus", _QUEST_TEXT),
        ):
            if style.get(key):
                style_parts.append(mapping.get(style[key], str(style[key])))
        if style_parts:
            lines.append("游玩风格：{0}".format("；".join(style_parts)))

        preferences = self.preferences
        if preferences.get("likes"):
            lines.append("喜欢的：{0}".format("、".join(preferences["likes"])))
        if preferences.get("dislikes"):
            lines.append("不喜欢的：{0}".format("、".join(preferences["dislikes"])))
        if preferences.get("fears"):
            lines.append("会紧张的：{0}".format("、".join(preferences["fears"])))

        speech = self.speech_style
        speech_parts: List[str] = []
        if speech.get("language"):
            speech_parts.append("用{0}".format(speech["language"]))
        if speech.get("tone"):
            speech_parts.append(speech["tone"])
        if speech.get("max_sentence_chars"):
            speech_parts.append("单句尽量不超过{0}字".format(speech["max_sentence_chars"]))
        if speech.get("silence_bias"):
            speech_parts.append(_SILENCE_TEXT.get(speech["silence_bias"], ""))
        speech_parts = [part for part in speech_parts if part]
        if speech_parts:
            lines.append("说话方式：{0}".format("；".join(speech_parts)))
        if speech.get("catchphrases"):
            lines.append(
                "口头禅（自然使用，不要每句都带）：{0}".format(
                    "、".join("「{0}」".format(item) for item in speech["catchphrases"])
                )
            )
        if speech.get("avoid"):
            lines.append("说话时避免：{0}".format("；".join(speech["avoid"])))

        if self.long_term_interests:
            lines.append("长期兴趣：{0}".format("、".join(self.long_term_interests)))

        history = self.self_history
        if history.get("summary"):
            lines.append("你的经历：{0}".format(history["summary"]))
        if history.get("milestones"):
            lines.append("记得的节点：{0}".format("；".join(history["milestones"])))

        boundaries = self.boundaries
        rules: List[str] = []
        if boundaries.get("no_spoilers"):
            rules.append("不剧透自己还没在游戏里经历过的剧情")
        if boundaries.get("no_meta_ai_talk"):
            rules.append("不把自己说成 AI、模型或旁白")
        if boundaries.get("no_game_memory_access"):
            rules.append("不读取游戏内存、存档或任何未通过画面/对话获得的信息")
        if rules:
            lines.append("底线：{0}".format("；".join(rules)))

        return "\n".join(lines)


def normalize_profile(data: Any) -> Dict[str, Any]:
    """校验并补全档案字段；返回可直接落盘的规范化字典。"""
    if not isinstance(data, dict):
        raise ProfileError("玩家档案必须是 JSON 对象")

    secrets = find_secret_fields(data)
    if secrets:
        raise ProfileError(
            "玩家档案不能包含令牌/密钥字段：{0}".format("、".join(secrets))
        )

    forbidden = find_forbidden_fields(data)
    if forbidden:
        raise ProfileError(
            "玩家档案不能包含世界真相/剧透字段：{0}".format("、".join(forbidden))
        )

    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ProfileError("玩家档案缺少整数 schema_version")
    if version > SCHEMA_VERSION:
        raise ProfileError(
            "玩家档案 schema_version={0} 高于当前支持的 {1}".format(
                version, SCHEMA_VERSION
            )
        )
    if version < SCHEMA_VERSION:
        raise ProfileError(
            "玩家档案 schema_version={0} 需要迁移，当前版本不支持自动迁移".format(
                version
            )
        )

    player_id = _as_text(data.get("player_id"))
    if not player_id:
        raise ProfileError("缺少 player_id")
    if len(player_id) > 64 or any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for char in player_id
    ):
        raise ProfileError("player_id 只能包含字母、数字、下划线或连字符，且不超过 64 字符")

    identity = _as_mapping(data.get("identity"))
    if not _as_text(identity.get("name")):
        raise ProfileError("identity.name 不能为空")
    identity = {
        "name": _as_text(identity.get("name"))[:60],
        "pronouns": _as_text(identity.get("pronouns"), "她")[:20],
        "origin": _as_text(identity.get("origin"))[:200],
        "role": _as_text(identity.get("role"))[:200],
        "self_description": _as_text(identity.get("self_description"))[:400],
    }

    personality = _as_mapping(data.get("personality"))
    traits = _as_text_list(personality.get("traits"), limit=8)
    if not traits:
        raise ProfileError("personality.traits 至少需要一个性格词")
    personality = {
        "traits": traits,
        "flaws": _as_text_list(personality.get("flaws"), limit=6),
        "quirks": _as_text_list(personality.get("quirks"), limit=6),
        "baseline_mood": _as_text(personality.get("baseline_mood"))[:80],
    }

    values = _as_text_list(data.get("values"), limit=10)
    if not values:
        raise ProfileError("values 至少需要一条在意的原则")

    play_style = _as_mapping(data.get("play_style"))
    play_style = {
        "pace": _as_text(play_style.get("pace"), "exploratory")[:40],
        "risk_tolerance": _as_ratio(play_style.get("risk_tolerance"), 0.5),
        "combat": _as_text(play_style.get("combat"), "cautious_but_curious")[:40],
        "exploration": _as_text(play_style.get("exploration"), "check_corners")[:40],
        "puzzle": _as_text(play_style.get("puzzle"), "try_first_then_look_around")[
            :40
        ],
        "quest_focus": _as_text(play_style.get("quest_focus"), "follow_curiosity")[
            :40
        ],
    }

    preferences = _as_mapping(data.get("preferences"))
    preferences = {
        "likes": _as_text_list(preferences.get("likes"), limit=10),
        "dislikes": _as_text_list(preferences.get("dislikes"), limit=10),
        "fears": _as_text_list(preferences.get("fears"), limit=10),
    }

    speech = _as_mapping(data.get("speech_style"))
    tone = _as_text(speech.get("tone"))
    if not tone:
        raise ProfileError("speech_style.tone 不能为空")
    silence = _as_text(speech.get("silence_bias"), "medium").lower()
    if silence not in _SILENCE_TEXT:
        silence = "medium"
    speech = {
        "language": _as_text(speech.get("language"), "简体中文")[:40],
        "tone": tone[:200],
        "catchphrases": _as_text_list(speech.get("catchphrases"), limit=8, item_limit=40),
        "max_sentence_chars": _as_int(
            speech.get("max_sentence_chars"), 60, low=10, high=300
        ),
        "silence_bias": silence,
        "avoid": _as_text_list(speech.get("avoid"), limit=8),
    }

    long_term_interests = _as_text_list(data.get("long_term_interests"), limit=12)

    history = _as_mapping(data.get("self_history"))
    self_history = {
        "summary": _as_text(history.get("summary"))[:600],
        "milestones": _as_text_list(history.get("milestones"), limit=20, item_limit=300),
    }

    boundaries = _as_mapping(data.get("boundaries"))
    if boundaries.get("no_spoilers") is False:
        raise ProfileError("boundaries.no_spoilers 不能为 false：玩家不得提前知道未经历剧情")
    boundaries = {
        "no_spoilers": True,
        "no_meta_ai_talk": bool(boundaries.get("no_meta_ai_talk", True)),
        "no_game_memory_access": bool(boundaries.get("no_game_memory_access", True)),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _as_text(data.get("updated_at"), "")[:40],
        "player_id": player_id,
        "identity": identity,
        "personality": personality,
        "values": values,
        "play_style": play_style,
        "preferences": preferences,
        "speech_style": speech,
        "long_term_interests": long_term_interests,
        "self_history": self_history,
        "boundaries": boundaries,
    }


def profile_from_dict(
    data: Any, *, source_path: Optional[Path] = None
) -> PlayerProfile:
    """从字典构造档案；``raw`` 保留原始字段，便于未来迁移。"""
    normalized = normalize_profile(data)
    return PlayerProfile(
        schema_version=normalized["schema_version"],
        player_id=normalized["player_id"],
        identity=normalized["identity"],
        personality=normalized["personality"],
        values=list(normalized["values"]),
        play_style=normalized["play_style"],
        preferences=normalized["preferences"],
        speech_style=normalized["speech_style"],
        long_term_interests=list(normalized["long_term_interests"]),
        self_history=normalized["self_history"],
        boundaries=normalized["boundaries"],
        updated_at=normalized["updated_at"],
        source_path=source_path,
        raw=dict(data),
    )


def load_profile(path: Path | str) -> PlayerProfile:
    """读取并校验档案；任何问题都以明确的 ProfileError 抛出。"""
    source = Path(path)
    if not source.exists():
        raise ProfileError("玩家档案不存在：{0}".format(source))
    try:
        data = read_json(source)
    except (OSError, ValueError) as exc:
        raise ProfileError("玩家档案不是合法 JSON：{0}（{1}）".format(source, exc))
    try:
        return profile_from_dict(data, source_path=source)
    except ProfileError as exc:
        raise ProfileError("{0}：{1}".format(source, exc))


def seed_default_profile(
    path: Path | str, *, template: Optional[Path | str] = None
) -> bool:
    """目标档案不存在时，从模板复制一份；已存在则不动，返回是否新建。"""
    target = Path(path)
    if target.exists():
        return False
    source = Path(template) if template else DEFAULT_PROFILE_TEMPLATE
    if not source.exists():
        raise ProfileError("默认档案模板不存在：{0}".format(source))
    # 先校验模板，避免把损坏模板复制成正式档案。
    text = source.read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ProfileError("默认档案模板不是合法 JSON：{0}（{1}）".format(source, exc))
    profile_from_dict(data, source_path=source)
    atomic_write_text(target, text)
    return True
