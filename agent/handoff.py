# -*- coding: utf-8 -*-
"""HandoffReport：交棒载荷（设计文档 02_schemas.md §3）。

为什么要单独一个对象，而不是让快系统"随手说一段总结"：
上一版交棒只靠 lumine 返回的自由文本，字段随措辞漂移、格式不固定，慢系统要的
result / conclusion / observations / last_frame / stats 全都得靠猜。这里把交棒做成
**可校验、可落盘、可回放**的一条数据：

    report = HandoffReport(intent_id="01J...", result="reached",
                           conclusion="拿到钥匙了，NPC 说晚上八点在塔楼见",
                           observations="## 结论\n……")
    report.validate()                        # 长度/枚举不合规直接抛 ValueError
    report.append_to_session(session)         # 进会话事件流（kind="handoff"）
    report.write_file(run_root, session_id)   # run/<session_id>/handoffs/<intent_id>.json

三条硬约束（02_schemas.md §3.3）：conclusion 8–60 字、observations 120–2000 字、
不许出现 markdown 表格/代码块/超链接（慢系统读的是散文）。违反就抛错，
**不截断伪造一个"看起来成功"的报告**。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List

SCHEMA_VERSION = "1.0"

#: 交棒结果：达成了 / 失败 / 卡住。慢系统按这三个值分派（02_schemas.md §3.2）。
RESULTS = ("reached", "failed", "blocked")

CONCLUSION_MIN = 8
CONCLUSION_MAX = 60
OBSERVATIONS_MIN = 120
OBSERVATIONS_MAX = 2000

#: 02_schemas.md §3.3：必含段缺失不阻断，但计入 handoff_quality 指标。
REQUIRED_SECTIONS = ("## 结论", "## 看到了什么", "## 我现在在哪")

#: 代码块围栏（三个反引号）。写成 chr 拼接，免得源码里的围栏把文档/工具搞乱。
FENCE = chr(96) * 3


class HandoffError(ValueError):
    """交棒报告不合法（长度/枚举/必填）。继承 ValueError，调用方按值错误抓即可。"""


_LINK = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:\-|]+\|[\s:\-|]*$")
_SAFE_ID = re.compile(r"[^0-9A-Za-z_\-]+")


def _strip_markdown_noise(text: str) -> str:
    """去掉代码块围栏/表格/超链接（内容保留），让慢系统读到的是散文。

    只做"变成散文"的变换，不删事实：表格行变成用「；」连起来的句子，
    链接 [文字](url) 只留文字。
    """

    out: List[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if FENCE and stripped.startswith(FENCE):
            # 围栏行自己去掉，围栏里的内容照旧保留。
            continue
        if "|" in stripped and _TABLE_SEP.match(stripped):
            continue
        if stripped.startswith("|") and stripped.count("|") >= 2:
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            joined = "；".join(cell for cell in cells if cell)
            if joined:
                out.append(joined)
            continue
        out.append(_LINK.sub(r"\1", line))
    return "\n".join(out).strip()


def _one_line(text: str) -> str:
    """结论是一句话：换行折叠成空格，首尾空白去掉。"""

    return " ".join(str(text or "").split())


def _safe_id(text: str) -> str:
    """intent_id 要当文件名：只留安全字符，杜绝 ../ 之类的路径穿越。"""

    cleaned = _SAFE_ID.sub("_", str(text or "").strip()).strip("_")
    return cleaned or "intent"


@dataclass
class HandoffReport:
    """一次交棒的完整载荷（字段与 02_schemas.md §3.2 一一对应）。"""

    intent_id: str
    result: str
    conclusion: str
    observations: str
    session_id: str = ""
    #: {frames: [...], clip: {segment, from_ms, to_ms}}，最多 4 帧。
    media: Dict[str, Any] = field(default_factory=dict)
    #: {started_ms, ended_ms, cycles, slowest_step_ms, intent_ms}
    timing: Dict[str, Any] = field(default_factory=dict)
    #: 侧车：{decisions, actions, failures}
    stats: Dict[str, Any] = field(default_factory=dict)
    last_frame: str = ""
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    schema_version: str = SCHEMA_VERSION
    #: 非阻断的质检信息（必含段缺失、markdown 被清理等）。
    warnings: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.intent_id = str(self.intent_id or "").strip()
        self.result = str(self.result or "").strip()
        self.conclusion = _one_line(self.conclusion)
        cleaned = _strip_markdown_noise(self.observations)
        if cleaned != str(self.observations or "").strip():
            self.warnings.append("observations 里的 markdown 表格/代码块/链接已按 §3.3 转成散文")
        self.observations = cleaned
        self.media = dict(self.media or {})
        self.timing = dict(self.timing or {})
        self.stats = dict(self.stats or {})
        self.last_frame = str(self.last_frame or "").strip()

    # ---- 校验 -------------------------------------------------------
    def validate(self) -> "HandoffReport":
        """校验长度与枚举；不合规抛 HandoffError（错误信息里写清实际长度）。"""

        if not self.intent_id:
            raise HandoffError("intent_id 不能为空：交棒必须能对回到某一个意图")
        if self.result not in RESULTS:
            raise HandoffError(
                "result 只能是 %s，实际是 %r" % ("/".join(RESULTS), self.result)
            )
        if not isinstance(self.conclusion, str) or not self.conclusion:
            raise HandoffError("conclusion 不能为空：慢系统第一眼看的就是这一句")
        length = len(self.conclusion)
        if length < CONCLUSION_MIN or length > CONCLUSION_MAX:
            raise HandoffError(
                "conclusion 长度不合法：实际 %d 字，要求 %d–%d 字"
                % (length, CONCLUSION_MIN, CONCLUSION_MAX)
            )
        obs_len = len(self.observations or "")
        if obs_len < OBSERVATIONS_MIN or obs_len > OBSERVATIONS_MAX:
            raise HandoffError(
                "observations 长度不合法：实际 %d 字，要求 %d–%d 字（写清看到/听到/还剩什么）"
                % (obs_len, OBSERVATIONS_MIN, OBSERVATIONS_MAX)
            )
        missing = [name for name in REQUIRED_SECTIONS if name not in self.observations]
        for name in missing:
            note = "observations 缺段 %s（不阻断，但计入 handoff_quality）" % name
            if note not in self.warnings:
                self.warnings.append(note)
        frames = (self.media or {}).get("frames")
        if frames is not None and len(frames) > 4:
            raise HandoffError("media.frames 最多 4 张，实际 %d 张" % len(frames))
        return self

    # ---- 序列化 -----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        """落盘/发送用的纯字典（字段顺序固定，方便人眼看 diff）。"""

        return asdict(self)

    def as_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    # ---- 交付 -------------------------------------------------------
    def append_to_session(self, session: Any) -> bool:
        """把这次交棒追加进会话事件流（kind="handoff"）；没有 session 就不写。

        返回是否真的写进去了——调用方要把这个值报出来，不能假装写了。
        """

        append = getattr(session, "append", None)
        if not callable(append):
            return False
        try:
            append("handoff", **self.to_dict())
        except Exception:  # noqa: BLE001 - 落盘失败由调用方按返回值上报
            return False
        return True

    def write_file(self, root: Any, session_id: str = "") -> Path:
        """原子写 run/<session_id>/handoffs/<intent_id>.json，返回落盘路径。"""

        self.validate()
        sid = str(session_id or self.session_id or "").strip()
        folder = Path(root) / _safe_id(sid) / "handoffs" if sid else Path(root) / "handoffs"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("%s.json" % _safe_id(self.intent_id))
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
        return path


def write_report(
    report: HandoffReport,
    root: Any,
    session_id: str = "",
    session: Any = None,
) -> Dict[str, Any]:
    """一次性做完"校验 → 落盘 → 进事件流"，返回可回给模型的字典。

    失败一律抛异常（HandoffError / OSError），绝不返回一个"看起来成功"的空结果。
    """

    report.validate()
    path = report.write_file(root, session_id)
    appended = report.append_to_session(session)
    payload = report.to_dict()
    payload.update({"ok": True, "file": str(path), "session_event": appended})
    if not appended:
        payload["warning"] = "会话事件没写进去（session 不可用）"
    return payload
