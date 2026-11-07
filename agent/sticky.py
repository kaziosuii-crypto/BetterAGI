# -*- coding: utf-8 -*-
"""便利贴 sticky：按存档存「学会怎么玩」的程序性记忆（02_schemas.md §7）。

一块便利贴只有两块内容，都是写给手模型（GameScaling 2B-S3）的：

* key_binding —— 键名 → 效果，≤1024 字；
* game_rules  —— 这游戏怎么玩，≤1024 字。

文件按存档分开：<root>/sticky/<game>/<save_id>.md（02 §0.1 / §7.1），
慢系统整体重写、不淘汰、不做增量合并——手模型是无状态模型，每次调用都要吃全量。

三条硬约束（违反任何一条都会把「手」按坏，或者把事故藏起来）：

1. 只有慢系统能写：write_sticky(..., source="fast") 直接 PermissionError。
   快系统（realtime）连读都不该读（01 §3 职责分离 / 00 D12）。
2. 超配额抛 StickyOverflow，绝不截断：消息里带实际字数与上限，让慢系统重写。
   一次写进 3K 文本会把历史帧预算挤掉，动作质量断崖下跌（README 风险 3）。
3. 词表外的键名不静默删：写进 warnings 并保留原文（02 §7.2 键名校验）。
   静默删等于骗慢系统「写进去了」，下一次它还会这么写。

写盘是事务（02 §7.3）：tmp → fsync → 回读校验 → 旧文件留 .bak → os.replace。
任何一步失败都抛异常，旧文件保持原样，不会出现半截内容。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import gamescaling_vocab as vocab

#: 文件格式版本，写在第一行的注释头里。
STICKY_VERSION = "v1"

#: 单块上限（按 Unicode 码点计，02 §7.2）。
STICKY_BLOCK_LIMIT = 1024

#: 总上限 = 1K + 1K = 2048。
STICKY_TOTAL_LIMIT = STICKY_BLOCK_LIMIT * 2

#: 固定的两块，顺序与 02 §7.1 模板一致（顺序变了解析就对不上）。
BLOCKS = ("key_binding", "game_rules")

#: 唯一的合法写者。
SLOW_SOURCE = "slow"

#: 便利贴根目录环境变量；测试用 root 参数直接指定。
ROOT_ENV = "BETTERAGI_STICKY_ROOT"


class StickyError(RuntimeError):
    """便利贴相关错误：路径非法、内容非法、回读校验失败等。"""


class StickyOverflow(StickyError):
    """超出配额。chars / limit / block 三件套都要带，慢系统要据此重写。"""

    def __init__(self, message: str, *, chars: int, limit: int, block: str = "") -> None:
        super().__init__(message)
        self.chars = int(chars)
        self.limit = int(limit)
        self.block = str(block or "")


# ---------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------
_BAD_PATH_RE = re.compile(r'[\\/:*?"<>|]')


def _safe_component(value: Any, *, field: str) -> str:
    """存档名/游戏名要能直接当路径分量用：拒绝空值、路径分隔符与控制字符。"""
    text = str(value or "").strip()
    if not text:
        raise StickyError(f"{field} 不能为空")
    if text in (".", "..") or _BAD_PATH_RE.search(text):
        raise StickyError(f"{field} 含非法字符（不能有路径分隔符）：{value!r}")
    if any(ord(ch) < 32 for ch in text):
        raise StickyError(f"{field} 含控制字符：{value!r}")
    return text


def sticky_root(root: Optional[Any] = None) -> Path:
    """便利贴根目录：显式 root > BETTERAGI_STICKY_ROOT > 当前目录下的 run/。"""
    if root is not None:
        return Path(root)
    configured = os.environ.get(ROOT_ENV)
    if configured:
        return Path(configured)
    return Path.cwd() / "run"


def sticky_path(game: Any, save_id: Any, root: Optional[Any] = None) -> Path:
    """便利贴文件路径：<root>/sticky/<game>/<save_id>.md。"""
    game_name = _safe_component(game, field="game")
    save_name = _safe_component(save_id, field="save_id")
    return sticky_root(root) / "sticky" / game_name / (save_name + ".md")


# ---------------------------------------------------------------------
# 读写事务（profiles 的便签也走这两个，避免两套实现）
# ---------------------------------------------------------------------
def read_text(path: Any) -> str:
    """读 UTF-8 文本；文件不存在返回空串。

    其它错误（权限、编码）一律抛 StickyError：静默当空文件会把「读失败」伪装成
    「还没写过」，慢系统就会拿空便利贴去重写，把已经学会的东西抹掉。
    """
    target = Path(path)
    if not target.exists():
        return ""
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise StickyError(f"{target} 不是 UTF-8 文本：{error}") from error
    except OSError as error:
        raise StickyError(f"读取 {target} 失败：{error}") from error


def atomic_write_text(path: Any, text: str, *, backup: bool = True) -> None:
    """原子写文本：tmp → fsync → 回读校验 → 旧文件留 .bak → os.replace。

    backup=True 时旧文件先复制成 <name>.bak（只留 1 版，02 §7.3）。
    任何一步失败都抛异常并删掉 tmp，目标文件保持原样——这是「不许写半截」的落点。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    payload = str(text)
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        # 回读校验：落盘字节必须与内存文本一致，否则不覆盖旧文件。
        back = tmp.read_text(encoding="utf-8")
        if back != payload:
            raise StickyError(f"回读校验失败：{tmp} 落盘内容与内存不一致")
        if backup and target.exists():
            old = target.read_bytes()
            bak = target.with_name(target.name + ".bak")
            with open(bak, "wb") as handle:
                handle.write(old)
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------
# 解析 / 渲染
# ---------------------------------------------------------------------
_HEADER_RE = re.compile(r"^<!--\s*sticky\s+(?P<version>\S+)(?P<rest>.*?)-->")
_HEADING_RE = re.compile(r"^##\s+(?P<name>.+?)\s*$")


def _parse_header(line: str) -> Dict[str, str]:
    """解析版本头：<!-- sticky v1 | game: x | save: y | updated_at: ... | by: slow -->"""
    matched = _HEADER_RE.match(line.strip())
    if not matched:
        return {}
    fields: Dict[str, str] = {"version": matched.group("version").strip()}
    for chunk in matched.group("rest").split("|"):
        key, sep, value = chunk.partition(":")
        if not sep:
            continue
        fields[key.strip()] = value.strip()
    return fields


def _split_sections(body: str) -> List[Tuple[str, str]]:
    """把正文切成有序的 (块名, 块文本)；未知块也原样保留。"""
    sections: List[Tuple[str, str]] = []
    name: Optional[str] = None
    buffer: List[str] = []

    def flush() -> None:
        if name is not None:
            sections.append((name, "\n".join(buffer).strip()))

    for line in body.splitlines():
        matched = _HEADING_RE.match(line)
        if matched:
            flush()
            name = matched.group("name").strip()
            buffer = []
            continue
        if name is not None:
            buffer.append(line)
    flush()
    return sections


def _load(path: Path) -> Tuple[str, Dict[str, str], List[Tuple[str, str]]]:
    """读文件并拆成 (原文, 头部, 有序块列表)。文件不存在返回空三元组。"""
    raw = read_text(path)
    if not raw.strip():
        return raw, {}, []
    lines = raw.splitlines()
    header: Dict[str, str] = {}
    body_start = 0
    for index, line in enumerate(lines):
        if not line.strip():
            body_start = index + 1
            continue
        if line.strip().startswith("<!--"):
            header = _parse_header(line)
            body_start = index + 1
        else:
            body_start = index
        break
    body = "\n".join(lines[body_start:])
    return raw, header, _split_sections(body)


def _render(
    game: str,
    save_id: str,
    key_binding: str,
    game_rules: str,
    *,
    extra_sections: Sequence[Tuple[str, str]] = (),
) -> str:
    """渲染成 02 §7.1 的形态；未知块（别的工具写的）原样保留在末尾，不静默丢。"""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    lines: List[str] = [
        f"<!-- sticky {STICKY_VERSION} | game: {game} | save: {save_id}"
        f" | updated_at: {stamp} | by: {SLOW_SOURCE} -->",
        "",
        "## key_binding",
    ]
    if key_binding:
        lines.extend(key_binding.splitlines())
    lines.extend(["", "## game_rules"])
    if game_rules:
        lines.extend(game_rules.splitlines())
    for name, text in extra_sections:
        if name in BLOCKS:
            continue
        lines.extend(["", f"## {name}"])
        if text:
            lines.extend(text.splitlines())
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------
# 键名校验（不删，只告警）
# ---------------------------------------------------------------------
#: 键位行里会出现、但不是键名的英文词（鼠标/操作描述）。命中就跳过，不当违规。
_NOT_KEY_WORDS = frozenset({
    "mouse", "wheel", "scroll", "camera", "aim", "look", "move", "moves",
    "movement", "drag", "press", "hold", "tap", "click", "doubleclick",
    "key", "keys", "button", "buttons", "and", "or", "the", "a", "an",
    "in", "on", "of", "to", "for", "with", "by", "pad", "trackpad", "axis",
})

_TOKEN_SPLIT_RE = re.compile(r"[\s,，、;/+]+")
_NUMBER_RANGE_RE = re.compile(r"\d+(-\d+)?")


def _strip_token(token: str) -> str:
    return token.strip().strip("\"'()（）[]【】").rstrip("键")


def key_binding_warnings(text: str) -> List[str]:
    """找出 key_binding 里「不像训练 token」的键名，返回告警文本。

    只做提醒，不删原文：慢系统要学会自己按 02 §7.2 写词表 token，
    静默删掉它只会让下一次继续写错。
    """
    warnings: List[str] = []
    seen: List[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lhs = stripped.split("->", 1)[0]
        lhs = lhs.lstrip("-*•· \t")
        for raw_token in _TOKEN_SPLIT_RE.split(lhs):
            token = _strip_token(raw_token)
            if not token:
                continue
            if any("\u4e00" <= ch <= "\u9fff" for ch in token):
                continue  # 中文是效果描述，不是键名
            if token.lower() in _NOT_KEY_WORDS:
                continue
            if _NUMBER_RANGE_RE.fullmatch(token):
                continue
            if token.isalpha() and token.isupper() and 2 <= len(token) <= 8:
                # WASD 这种连写：拆成单字母后全在词表里就算过。
                if all(vocab.in_vocab(ch) for ch in token):
                    continue
            if vocab.in_vocab(token):
                continue
            if token in seen:
                continue
            seen.append(token)
            warnings.append(
                f"词表外键名 {token!r}（行：{stripped}）；原文已保留，"
                f"但游戏服务端会把它当不存在的键丢掉（02 §7.2）"
            )
    return warnings


# ---------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------
def _empty(path: Path) -> Dict[str, Any]:
    return {
        "key_binding": "",
        "game_rules": "",
        "raw": "",
        "path": str(path),
        "updated_at": "",
        "warnings": [],
    }


def read_sticky(game: Any, save_id: Any, root: Optional[Any] = None) -> Dict[str, Any]:
    """读一个存档的便利贴。

    文件不存在返回空块（不抛异常）——「还没学过」是正常状态。
    文件存在但格式不对时返回已解析到的部分 + warnings，原文在 raw 里可查。
    """
    path = sticky_path(game, save_id, root=root)
    if not path.exists():
        return _empty(path)
    raw, header, sections = _load(path)
    warnings: List[str] = []
    if raw.strip() and not header:
        warnings.append(f"缺少 <!-- sticky {STICKY_VERSION} ... --> 版本头：{path}")
    elif header and header.get("version") != STICKY_VERSION:
        warnings.append(
            f"便利贴版本是 {header.get('version')!r}，本程序只认 {STICKY_VERSION!r}"
        )

    blocks: Dict[str, str] = {}
    for name, text in sections:
        if name not in BLOCKS:
            continue
        if name in blocks:
            warnings.append(f"重复的 ## {name} 块：只取第一块，后一块原文留在 raw 里")
            continue
        blocks[name] = text
    for name in BLOCKS:
        if name not in blocks:
            warnings.append(f"缺少 ## {name} 块：{path}")

    info = {
        "key_binding": blocks.get("key_binding", ""),
        "game_rules": blocks.get("game_rules", ""),
        "raw": raw,
        "path": str(path),
        "updated_at": str(header.get("updated_at") or header.get("updated_ms") or ""),
        "warnings": warnings,
    }
    info["warnings"].extend(key_binding_warnings(info["key_binding"]))
    return info


def sticky_chars(info: Dict[str, Any]) -> int:
    """两块正文字数合计（预检与指标用；上限见 STICKY_TOTAL_LIMIT）。"""
    body = info or {}
    return len(str(body.get("key_binding") or "")) + len(str(body.get("game_rules") or ""))


_HEADING_IN_BLOCK_RE = re.compile(r"(?m)^\s*##\s+")
_SCRIPT_RE = re.compile(r"<\s*script", re.IGNORECASE)


def _check_block_text(name: str, text: str) -> None:
    """块内不许出现会把文件解析切碎、或者会伪装成版本头的内容。"""
    if _HEADING_IN_BLOCK_RE.search(text):
        raise StickyError(
            f"{name} 块里不能出现 ## 标题行：会切断解析，请改用普通列表项"
        )
    if "<!--" in text:
        raise StickyError(
            f"{name} 块里不能出现 HTML 注释头 <!--：会和便利贴版本头混淆"
        )
    if _SCRIPT_RE.search(text):
        raise StickyError(f"{name} 块里不能出现 script 标签（04 S7 非法内容）")


def _check_quota(name: str, text: str) -> int:
    chars = len(text)
    if chars > STICKY_BLOCK_LIMIT:
        raise StickyOverflow(
            f"便利贴 {name} 块 {chars} 字，超过单块上限 {STICKY_BLOCK_LIMIT} 字",
            chars=chars,
            limit=STICKY_BLOCK_LIMIT,
            block=name,
        )
    return chars


def write_sticky(
    game: Any,
    save_id: Any,
    key_binding: Optional[str] = None,
    game_rules: Optional[str] = None,
    root: Optional[Any] = None,
    replace: bool = False,
    source: str = SLOW_SOURCE,
) -> Dict[str, Any]:
    """写便利贴（只有慢系统能写）。

    * replace=True：整体重写，必须同时给出两块（02 §7.3 不接受 diff）；
    * replace=False：按块替换，未传（None）的块保持原样。

    超配额抛 StickyOverflow，内容非法抛 StickyError；
    两种情况下旧文件都不动。成功后返回与 read_sticky 同结构的 dict。
    """
    if str(source) != SLOW_SOURCE:
        raise PermissionError(
            f"便利贴只有慢系统能写，{source!r} 被拒绝（02 §7.2 写者约束）"
        )
    path = sticky_path(game, save_id, root=root)
    game_name = _safe_component(game, field="game")
    save_name = _safe_component(save_id, field="save_id")

    current = read_sticky(game, save_id, root=root)
    if replace:
        if key_binding is None or game_rules is None:
            raise StickyError(
                "replace=True 整体重写时必须同时给出 key_binding 与 game_rules（02 §7.3）"
            )
        text_key = str(key_binding).strip()
        text_rules = str(game_rules).strip()
    else:
        if key_binding is None and game_rules is None:
            raise StickyError("没有要写的块：key_binding 与 game_rules 至少给一个")
        text_key = (
            current["key_binding"] if key_binding is None else str(key_binding).strip()
        )
        text_rules = (
            current["game_rules"] if game_rules is None else str(game_rules).strip()
        )

    _check_block_text("key_binding", text_key)
    _check_block_text("game_rules", text_rules)
    chars_key = _check_quota("key_binding", text_key)
    chars_rules = _check_quota("game_rules", text_rules)
    total = chars_key + chars_rules
    if total > STICKY_TOTAL_LIMIT:
        raise StickyOverflow(
            f"便利贴合计 {total} 字，超过总上限 {STICKY_TOTAL_LIMIT} 字（1K+1K）",
            chars=total,
            limit=STICKY_TOTAL_LIMIT,
        )

    _, _, sections = _load(path)
    rendered = _render(
        game_name, save_name, text_key, text_rules, extra_sections=sections
    )
    atomic_write_text(path, rendered)
    # 返回值读盘：必须反映真实落盘内容，而不是内存里的推测。
    return read_sticky(game, save_id, root=root)
