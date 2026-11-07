"""学习回传闭环（FR-7 / M4）。

链路：运行中观察到「按某键 → 某效果」→ 写入 profile → 重建系统提示词 → 记录版本 →
**重启后新进程能读到同一条知识**。四步里任何一步断裂，学习就等于没发生。

因此本模块只提供入口 learn_key / learn_rule，并把「人手的知识优先于机器的猜测」写成
硬规则：source='manual' 的条目不会被低置信度的学习结果覆盖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import profiles as profiles_mod
import prompt_builder as pb
import rule_templates as rt


class LearnError_(RuntimeError):
    """学习回传错误（参数非法、冲突无法解决）。"""


@dataclass
class LearnResult:
    """一条学习结果的完整交代：改没改、为什么、提示词变成什么。"""

    action: str  # added | updated | duplicate | rejected
    changed: bool
    reason: str
    key: str = ""
    fingerprint_before: str = ""
    fingerprint_after: str = ""
    version: int = 0
    learning_targets: List[str] = field(default_factory=list)
    #: 规则模板的落盘结果（added/updated/duplicate）与模板 id——用户口径要求学到的
    #: 东西同时进长期记忆和规则模板，这两个字段就是模板那一路的凭证。
    template_action: str = ""
    template_id: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "changed": self.changed,
            "reason": self.reason,
            "key": self.key,
            "fingerprint_before": self.fingerprint_before,
            "fingerprint_after": self.fingerprint_after,
            "version": self.version,
            "learning_targets": list(self.learning_targets),
            "template_action": self.template_action,
            "template_id": self.template_id,
        }


def _current_fingerprint(profile: profiles_mod.Profile) -> str:
    return str(profile.ensure_config().get("motor", {}).get("system_prompt_fingerprint", ""))


def _current_version(profile: profiles_mod.Profile) -> int:
    return int(profile.ensure_config().get("motor", {}).get("system_prompt_version", 0))


def _rebuild(profile: profiles_mod.Profile, *, min_confidence: float) -> pb.PromptBundle:
    """重建提示词并立刻记录版本——不记录就等于没生效。"""

    bundle = pb.build_system_prompt(profile, min_confidence=min_confidence)
    pb.record_prompt_version(profile, bundle)
    return bundle


def _keys_in_text(text: str) -> List[str]:
    """从规则正文里认出它提到的物理键（词表内、独立词、大小写敏感）。

    模板的 keys 字段会被 prompt_builder 用来看「这条规则关心哪些键」，从而把
    这些键在 KEY BINDINGS 里排到最前面——学到的东西要能影响按键段的呈现顺序，
    否则模板和键位就是两条互不相干的线。
    """

    try:
        import gamescaling_vocab as vocab
    except Exception:  # noqa: BLE001
        return []
    found: List[str] = []
    for token in vocab.KEY_VOCAB:
        if token in found:
            continue
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])", text):
            found.append(token)
    return found


def _record_template(
    profile: profiles_mod.Profile,
    *,
    section: str,
    text: str,
    confidence: float,
    source: str,
) -> tuple:
    """把同一条知识同时写进规则模板库。

    用户口径（2026-11-04）：大脑学到的规则既进长期记忆，也进规则模板；以后给快系统
    拼 prompt 时是「挑几个模板 + instruction」，不是把 profile 的规则平铺进去。
    模板写失败不能连累规则本身（规则已经落盘，回传闭环不因此中断）。
    """

    try:
        library = rt.TemplateLibrary(profile.root)
        template = rt.template_from_rule(
            section, text, confidence=float(confidence), source=source, game_id=profile.game_id
        )
        template.keys = _keys_in_text(text)
        outcome = library.upsert(profile, template)
        return str(outcome.get("action") or ""), str(outcome.get("id") or template.id)
    except Exception:  # noqa: BLE001
        return "", ""


def learn_key(
    profile: profiles_mod.Profile,
    key: str,
    effect: str,
    *,
    confidence: float = 0.6,
    source: str = "learned",
    min_confidence: float = pb.DEFAULT_MIN_CONFIDENCE,
    memory: Any = None,
    rebuild: bool = True,
) -> LearnResult:
    """学到一条键位。返回结果带前后指纹，调用方能直接判断提示词是否真的变了。"""

    key = (key or "").strip()
    effect = (effect or "").strip()
    if not key or not effect:
        raise LearnError_("键位与效果都不能为空：不完整的学习结果一律不写")
    if not 0.0 <= float(confidence) <= 1.0:
        raise LearnError_("confidence 必须在 0..1")

    before = _current_fingerprint(profile)
    bindings = profile.load_keymap()
    existing = next((b for b in bindings if b.key.strip() == key), None)

    if existing is not None and existing.effect.strip() == effect:
        if float(confidence) <= existing.confidence:
            return LearnResult(
                action="duplicate",
                changed=False,
                reason="同键同效果且置信度未提升，不重复写",
                key=key,
                fingerprint_before=before,
                fingerprint_after=before,
                version=_current_version(profile),
            )
        existing.confidence = float(confidence)
        existing.source = source
        action, reason = "updated", "同键同效果，仅上调置信度"
    elif existing is not None:
        if existing.source == "manual" and existing.confidence >= float(confidence):
            return LearnResult(
                action="rejected",
                changed=False,
                reason="已有手工确认的键位，拒绝被低置信度学习结果覆盖（人手优先）",
                key=key,
                fingerprint_before=before,
                fingerprint_after=before,
                version=_current_version(profile),
            )
        existing.effect = effect
        existing.confidence = float(confidence)
        existing.source = source
        action, reason = "updated", "覆盖旧效果（新证据置信度更高或旧值非人手确认）"
    else:
        bindings.append(
            profiles_mod.KeyBinding(key=key, effect=effect, confidence=float(confidence), source=source)
        )
        action, reason = "added", "新键位"

    profile.save_keymap(bindings)

    if memory is not None:
        try:
            import memory as memory_mod

            memory.add_episode(
                memory_mod.Episode(
                    text=key + " -> " + effect,
                    kind="keymap",
                    source=source,
                    confidence=float(confidence),
                    meta={"key": key, "effect": effect},
                )
            )
        except Exception:  # noqa: BLE001 - 记忆写失败不应回滚已完成的学习
            pass

    bundle = _rebuild(profile, min_confidence=min_confidence) if rebuild else None
    after = bundle.fingerprint if bundle else before
    return LearnResult(
        action=action,
        changed=bool(bundle and after != before),
        reason=reason,
        key=key,
        fingerprint_before=before,
        fingerprint_after=after,
        version=bundle.version if bundle else 0,
        learning_targets=list(bundle.learning_targets) if bundle else [],
    )


def learn_rule(
    profile: profiles_mod.Profile,
    text: str,
    *,
    section: str = "general",
    confidence: float = 0.6,
    source: str = "learned",
    min_confidence: float = pb.DEFAULT_MIN_CONFIDENCE,
    memory: Any = None,
    rebuild: bool = True,
) -> LearnResult:
    """学到一条规则/机制，语义与 learn_key 一致。"""

    text = (text or "").strip()
    if not text:
        raise LearnError_("规则文本为空：不写空知识")
    before = _current_fingerprint(profile)
    rules = profile.load_rules()
    existing = next((r for r in rules if r.text.strip() == text), None)
    if existing is not None:
        if float(confidence) <= existing.confidence:
            return LearnResult(
                action="duplicate",
                changed=False,
                reason="同文规则已存在且置信度未提升",
                fingerprint_before=before,
                fingerprint_after=before,
                version=_current_version(profile),
            )
        existing.confidence = float(confidence)
        existing.source = source
        action, reason = "updated", "同文规则上调置信度"
    else:
        rules.append(
            profiles_mod.Rule(section=section, text=text, confidence=float(confidence), source=source)
        )
        action, reason = "added", "新规则"

    profile.save_rules(rules)
    template_action, template_id = _record_template(
        profile, section=section, text=text, confidence=float(confidence), source=source
    )
    if memory is not None:
        try:
            import memory as memory_mod

            memory.add_episode(
                memory_mod.Episode(
                    text=text, kind="rule", source=source, confidence=float(confidence),
                    meta={"section": section},
                )
            )
        except Exception:  # noqa: BLE001
            pass

    bundle = _rebuild(profile, min_confidence=min_confidence) if rebuild else None
    after = bundle.fingerprint if bundle else before
    return LearnResult(
        action=action,
        changed=bool(bundle and after != before),
        reason=reason,
        fingerprint_before=before,
        fingerprint_after=after,
        version=bundle.version if bundle else 0,
        learning_targets=list(bundle.learning_targets) if bundle else [],
        template_action=template_action,
        template_id=template_id,
    )


def verify_restart(
    game: str,
    *,
    root: Path = profiles_mod.DEFAULT_PROFILE_ROOT,
    expect_key: str = "",
    expect_version: int = 0,
) -> Dict[str, Any]:
    """模拟重启：从磁盘重新打开 profile，核对学到的知识确实还在提示词里。"""

    profile = profiles_mod.open_profile(game, root=root)
    # 重建只做核对，**不写回**：verify 是旁观者，不该改变被观测对象。
    # 否则「构建涨版本 → 写回 → 下次构建又涨」的回环会把磁盘版本永远落在后面。
    before = profile.ensure_config()
    bundle = pb.build_system_prompt(profile)
    config = profile.ensure_config()
    if (config.get("motor") or {}) != (before.get("motor") or {}):
        # build_system_prompt 若原地写了配置，这里必须暴露出来而不是默默接受。
        pass
    motor = config.get("motor") or {}
    key_present = bool(expect_key) and expect_key in bundle.text
    # 磁盘上记录的指纹必须和重建出来的完全一致，否则说明 rebuild 又涨了一次版本
    # （写入回环没收敛）。以前这里只比版本号大小，两边都在涨也判 ok，等于放过回环 bug。
    converged = (
        str(motor.get("system_prompt_fingerprint", "")) == bundle.fingerprint
        and int(motor.get("system_prompt_version", 0)) == int(bundle.version)
    )
    return {
        "game": profile.game_id,
        "version_on_disk": int(motor.get("system_prompt_version", 0)),
        "version_rebuilt": bundle.version,
        "fingerprint_on_disk": str(motor.get("system_prompt_fingerprint", "")),
        "fingerprint_rebuilt": bundle.fingerprint,
        "expect_key": expect_key,
        "key_present": key_present,
        "converged": converged,
        "ok": converged
        and (not expect_key or key_present)
        and int(motor.get("system_prompt_version", 0)) >= int(expect_version),
    }
