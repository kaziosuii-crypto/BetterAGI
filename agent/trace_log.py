"""逐轮追踪日志：除图片外，把提示词、模型输出和每一步的时间都落成 JSONL。

2026-11-06 用户原话：「你就不能做好log，在里面写上所有除了图片的提示词，输出结果，
时间什么的吗？？」——所以这里的口径是：

* **文字一个都不省**：system 提示词、每条上下文消息、工具参数、工具回执、模型原文
  全部原样写进去；
* **图片只留元信息**：base64 不进日志（一张图几十 KB，滚两轮就把日志撑成垃圾场），
  统一换成 `[图片已省略：jpeg，约 33201 字节]`，还能看出"这轮到底喂了几张图、多大"；
* **时间一律落盘**：每行带本地时间与相对本次运行的毫秒数，慢系统一次请求的
  TTFT / 总耗时、手模型每次决策的耗时都在字段里。

为什么要单独一个模块：TUI 的 run-*.jsonl 只记 start/assistant/tool 四类事件，
既没有提示词也没有手模型输出；排查「手为什么按 W」「慢系统为什么这么理解画面」
时全靠猜。这里只负责写文件，不参与任何游戏逻辑，写坏了也绝不能影响直播。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

#: data URL 里的图片载荷。阈值取 64，避免把正常的短文本误判成图片。
_IMAGE_RE = re.compile(r"data:image/([A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]{64,})")


def _base64_bytes(payload: str) -> int:
    """base64 长度 → 原始字节数（估算即可，日志只为看清规模）。"""
    return max(0, len(re.sub(r"\s+", "", payload)) * 3 // 4)


def image_note(fmt: str, payload: str) -> str:
    return f"[图片已省略：{fmt or 'image'}，约 {_base64_bytes(payload)} 字节]"


def redact_images(text: str) -> str:
    """把字符串里的 data:image/...;base64,... 换成一行元信息。"""
    if "base64," not in text:
        return text
    return _IMAGE_RE.sub(lambda match: image_note(match.group(1), match.group(2)), text)


def redact(value: Any, *, _depth: int = 0) -> Any:
    """递归脱敏：图片换元信息，其它类型尽量原样保留（中文不转义）。"""
    if _depth > 24:
        return "[嵌套过深，已截断]"
    if isinstance(value, str):
        return redact_images(value)
    if isinstance(value, dict):
        return {str(key): redact(item, _depth=_depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_images(str(value))


def describe_messages(messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把 API 的 messages 压成"文字全留、图片留元信息"的可读结构。"""
    described: List[Dict[str, Any]] = []
    for message in messages or []:
        if not isinstance(message, dict):
            described.append({"role": "?", "text": redact(str(message))})
            continue
        item: Dict[str, Any] = {"role": message.get("role") or "?"}
        content = message.get("content")
        if isinstance(content, str):
            item["text"] = redact_images(content)
        elif isinstance(content, list):
            texts: List[str] = []
            images: List[str] = []
            for part in content:
                if not isinstance(part, dict):
                    texts.append(redact_images(str(part)))
                    continue
                kind = part.get("type")
                if kind == "text":
                    texts.append(str(part.get("text") or ""))
                elif kind in ("image_url", "input_image", "image"):
                    url = part.get("image_url") or part.get("image") or ""
                    if isinstance(url, dict):
                        url = url.get("url") or ""
                    url = str(url)
                    match = _IMAGE_RE.search(url)
                    if match:
                        images.append(image_note(match.group(1), match.group(2)))
                    else:
                        images.append(f"[图片：{url[:160] or '无 url'}]")
                else:
                    texts.append(redact_images(json.dumps(part, ensure_ascii=False, default=str)))
            item["text"] = redact_images("\n".join(texts))
            if images:
                item["images"] = images
        elif content is not None:
            item["text"] = redact_images(str(content))
        if message.get("tool_calls"):
            item["tool_calls"] = redact(message.get("tool_calls"))
        if message.get("tool_call_id"):
            item["tool_call_id"] = str(message.get("tool_call_id"))
        if message.get("reasoning_content"):
            item["reasoning"] = redact_images(str(message.get("reasoning_content")))
        described.append(item)
    return described


class TraceLog:
    """一个 append-only 的 JSONL 追踪器；线程安全，每行 flush。"""

    def __init__(self, path: Path | str, *, run_id: str = "") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.t0 = time.time()
        #: buffering=1 = 行缓冲：窗口被强关也最多丢最后一行。
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    def event(self, kind: str, **fields: Any) -> Dict[str, Any]:
        now = time.time()
        record: Dict[str, Any] = {
            "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "t_ms": int((now - self.t0) * 1000),
            "kind": kind,
        }
        if self.run_id:
            record["run"] = self.run_id
        record.update(redact(fields))
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
            except Exception:  # noqa: BLE001 - 日志写坏了绝不能影响直播
                pass
        return record

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> "TraceLog":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def build(path: Path | str, *, run_id: str = "") -> Optional[TraceLog]:
    """建追踪器；建不出来返回 None（调用方按"没有日志"继续跑）。"""
    try:
        return TraceLog(path, run_id=run_id)
    except Exception:  # noqa: BLE001
        return None
