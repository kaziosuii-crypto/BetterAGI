"""把记忆接进直播回路：检索注入 + 行动写回（纯函数，方便单测）。

设计口径：
- 检索不做语义 embedding（本地兜底是朴素关键词），所以把「上下文原文 + 中文滑窗词」
  拼成一个 query 交给后端：单次检索、不改分叠加，关键词全落空时才补一次空 query。
- 检索默认按五层过滤、默认关掉剧透内容（include_spoiler=False）。
- 写回只记录「按键 → 结果」这种可复用教训，不把每一帧画面写进记忆（那是日志，不是记忆）。
- 记忆不是直播的硬依赖：写回返回 False；检索异常交给调用方统一兜底记录，不在这里静默吞掉。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

from memory import Episode

#: 只把「动手」类工具的结果写回记忆；look 是观察，写进去会把记忆灌成流水账。
WRITEBACK_TOOLS = ("act", "learn_key", "restart", "restart_game")

#: 注入区块的字符预算（设计 02_schemas §8.3：600-900 token，取中等值）。
#: 预算只算一次：召回上限和格式化共用，不做多路叠加。
RECALL_BUDGET_CHARS = 800

_CJK = re.compile(r"[\u4e00-\u9fff]+")


def query_terms(text: str, max_terms: int = 6) -> List[str]:
    """从中文/英文混杂的目标描述里切出可用于关键词检索的词。

    中文没有空格：按标点切块后再取 2 字滑窗，保证能跟笔记里的短词对上。
    """

    raw = (text or "").strip()
    if not raw:
        return []
    terms: List[str] = []
    for chunk in re.split(r"[\s，,。;；:：/|、（）()\[\]\"\"']+", raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        if _CJK.search(chunk):
            if len(chunk) <= 4:
                terms.append(chunk)
            else:
                terms.extend(chunk[i : i + 2] for i in range(0, len(chunk) - 1, 2))
        elif len(chunk) >= 3:
            terms.append(chunk)
    seen: set[str] = set()
    out: List[str] = []
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            out.append(term)
        if len(out) >= max_terms:
            break
    return out


def recall_for(
    memory: Any,
    goal: str = "",
    hint: str = "",
    limit: int = 4,
    kinds: Sequence[str] = (),
    *,
    layers: Optional[Sequence[str]] = None,
    top_k: Optional[int] = None,
    include_spoiler: bool = False,
) -> List[Episode]:
    """按上下文检索记忆：真正调用后端 search，一次检索 + 必要时一次补齐。

    设计口径（02_schemas §8.3）：query 用"当前上下文文本"（目标 + 上一轮回执），
    所以这里把原文和中文滑窗词拼成一个 query 交给后端，而不是多路检索各查一遍再
    把分数叠起来（那会让同一条记忆被重复加权）。layers/top_k/include_spoiler
    原样透传，防剧透闸默认关闭剧透内容。
    """

    if memory is None:
        return []
    context = " ".join(
        part for part in (str(goal or "").strip(), str(hint or "").strip()) if part
    )
    probes = query_terms(context)
    # dict.fromkeys 去重且保序：上下文原文在前，补充检索词在后。
    query = " ".join(dict.fromkeys([context, *probes])).strip()
    size = int(top_k) if top_k is not None else int(limit)
    hits: List[Episode] = list(
        memory.search(
            query,
            limit=size,
            kinds=kinds,
            layers=layers,
            include_spoiler=include_spoiler,
        )
    )
    if not hits and query:
        # 关键词全落空时才用"最近/高置信"补一次；不是多路叠加。
        hits = list(
            memory.search(
                "",
                limit=size,
                kinds=kinds,
                layers=layers,
                include_spoiler=include_spoiler,
            )
        )
    seen: set[str] = set()
    out: List[Episode] = []
    for episode in hits:
        key = episode.text.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(episode)
        if len(out) >= size:
            break
    return out


def format_recall(
    episodes: Iterable[Episode], max_chars: int = RECALL_BUDGET_CHARS
) -> str:
    """把召回结果压成一段可直接进提示词的文本；空则返回空串。

    非情景层会标出层名（如 procedural），让慢系统知道这是"我会的操作"而不是道听途说。
    """

    lines: List[str] = []
    used = 0
    for episode in episodes:
        text = " ".join(str(episode.text).split())
        if not text:
            continue
        label = str(getattr(episode, "layer", "episodic") or "episodic")
        tag = f"{label}/{episode.kind}" if label != "episodic" else str(episode.kind)
        line = f"- [{tag}] {text}"
        if used + len(line) > max_chars:
            if not lines:
                # 第一条就超预算：截断保留，否则一条长笔记会把整段召回静默吞掉。
                head = f"- [{episode.kind}] "
                room = max(0, max_chars - len(head))
                lines.append(head + text[:room])
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    return "你以前在这款游戏里记下的笔记（自己写的，可能有错，用画面核对）：\n" + "\n".join(lines)


def _summarize_result(text: str, max_chars: int = 160) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:max_chars]


def episode_from_tool(
    name: str,
    arguments: Dict[str, Any],
    result_text: str,
    ok: bool,
    group_id: str = "",
    source: str = "slow",
    confidence: float = 0.8,
) -> Optional[Episode]:
    """把一次工具执行压成一条教训；不值得记的返回 None。"""

    if name not in WRITEBACK_TOOLS:
        return None
    if not group_id:
        return None
    args = arguments or {}
    if name == "act":
        keys = args.get("keys") or []
        if isinstance(keys, str):
            keys = [keys]
        detail = ", ".join(str(item) for item in keys) if keys else "(无按键)"
        what = f"按 [{detail}]"
    elif name == "learn_key":
        what = f"学会按键 {args.get('name')} -> {args.get('physical')}"
    else:
        what = f"{name}（重置游戏）"
    verdict = "成功" if ok else "失败"
    body = _summarize_result(result_text)
    kind = "keymap" if name == "learn_key" else "lesson"
    text = f"{what}：{verdict}" + (f"，画面/回执：{body}" if body else "")
    # 层归属：学到的键位映射是程序性记忆（最值钱、检索 ×1.5）；
    # 一次动手/重开是情景记忆。原子事实只在确认键位成功时落，失败不写"事实"。
    layer = "procedural" if name == "learn_key" else "episodic"
    atoms: List[Dict[str, Any]] = []
    if name == "learn_key" and ok:
        key_name = str(args.get("name") or "").strip()
        physical = str(args.get("physical") or "").strip()
        if key_name and physical:
            atoms.append(
                {
                    "text": f"按键 {key_name} 对应 {physical}",
                    "kind": "mechanic",
                    "confidence": confidence,
                    "spoiler": False,
                }
            )
    return Episode(
        text=text,
        kind=kind,
        group_id=group_id,
        source=source,
        confidence=confidence if ok else max(0.3, confidence - 0.3),
        meta={"tool": name, "args": {k: args[k] for k in list(args)[:6]}},
        layer=layer,
        atoms=atoms,
    )


def writeback(memory: Any, episode: Optional[Episode]) -> bool:
    """写回一条 episode；任何失败都吞掉（记忆不是直播的硬依赖）。"""

    if memory is None or episode is None:
        return False
    try:
        memory.add_episode(episode)
        return True
    except Exception:  # noqa: BLE001
        return False
