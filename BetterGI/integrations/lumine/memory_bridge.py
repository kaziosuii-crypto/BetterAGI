#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导演的长期记忆桥：把 OpenViking 接成可选的检索/写入后端。

OpenViking 是一个本地上下文数据库（AGPLv3）：设定资料放在
``viking://~/resources/genshin``，实况里积累的剧情片段放在
``viking://~/memories/genshin``。导演每轮用当前指导/场景检索相关片段，
再按固定间隔把本场摘要写回去。

没有启动 OpenViking 时整个桥自动降级为空操作，导演照常运行。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
    from openviking_sdk import AsyncHTTPClient
except ImportError:  # pragma: no cover - 仅在未安装 SDK 时触发
    AsyncHTTPClient = None  # type: ignore[assignment,misc]

LOGGER = logging.getLogger("lumine.memory")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class MemorySettings:
    """记忆桥配置。"""

    enabled: bool = False
    url: str = "http://127.0.0.1:1933"
    api_key: str = ""
    lore_uri: str = "viking://~/resources/genshin"
    memory_uri: str = "viking://~/memories/genshin"
    recall_limit: int = 4
    recall_max_chars: int = 1800
    write_interval_s: float = 60.0
    timeout_s: float = 10.0

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "MemorySettings":
        env = os.environ if env is None else env
        url = env.get("OPENVIKING_URL", cls.url)
        enabled = _env_bool("OPENVIKING_ENABLED", bool(env.get("OPENVIKING_URL")))
        return cls(
            enabled=enabled,
            url=url,
            api_key=env.get("OPENVIKING_API_KEY", ""),
            lore_uri=env.get("OPENVIKING_LORE_URI", cls.lore_uri),
            memory_uri=env.get("OPENVIKING_MEMORY_URI", cls.memory_uri),
            recall_limit=max(1, int(env.get("OPENVIKING_RECALL_LIMIT", "4"))),
            recall_max_chars=max(
                200, int(env.get("OPENVIKING_RECALL_MAX_CHARS", "1800"))
            ),
            write_interval_s=max(
                10.0, float(env.get("OPENVIKING_WRITE_INTERVAL_S", "60"))
            ),
            timeout_s=max(2.0, float(env.get("OPENVIKING_TIMEOUT_S", "10"))),
        )


class MemoryBridge:
    """OpenViking 的轻量异步封装。"""

    def __init__(self, settings: MemorySettings, logger=LOGGER) -> None:
        self.settings = settings
        self.log = logger
        self.client: Optional[Any] = None
        self.available = False
        self.last_write_at = 0.0
        self.last_error = ""
        # 每次启动/重启使用独立 run_id，避免 replace 覆盖上一场摘要。
        self.run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.last_start_attempt = 0.0

    @classmethod
    def from_env(cls, logger=LOGGER) -> "MemoryBridge":
        return cls(MemorySettings.from_env(), logger=logger)

    async def start(self) -> bool:
        """连接并初始化；失败时返回 False，不阻断导演。"""
        self.last_start_attempt = time.time()
        if not self.settings.enabled:
            self.log.info("OpenViking 记忆未启用")
            return False
        if AsyncHTTPClient is None:
            self.log.warning("未安装 openviking-sdk，记忆功能关闭")
            return False
        try:
            self.client = AsyncHTTPClient(
                url=self.settings.url,
                api_key=self.settings.api_key or None,
                timeout=self.settings.timeout_s,
            )
            await self.client.initialize()
            healthy = await self.client.health()
            self.available = bool(healthy)
            if self.available:
                self.log.info("OpenViking 记忆已连接：%s", self.settings.url)
            else:
                self.log.warning("OpenViking 健康检查失败，记忆功能关闭")
            return self.available
        except Exception as exc:  # noqa: BLE001 - 记忆是增强能力，失败即降级
            self.last_error = str(exc)
            self.log.warning("OpenViking 连接失败，记忆功能关闭：%s", exc)
            self.available = False
            self.client = None
            return False

    async def _ensure_started(self) -> None:
        """允许 OpenViking 比导演晚启动：最多每 30 秒重试一次。"""
        if self.available or not self.settings.enabled:
            return
        if time.time() - self.last_start_attempt < 30.0:
            return
        await self.start()

    async def close(self) -> None:
        if self.client is not None:
            try:
                await self.client.close()
            except Exception as exc:  # noqa: BLE001
                self.log.debug("关闭 OpenViking 客户端失败：%s", exc)
        self.client = None
        self.available = False

    async def recall(self, query: str, limit: Optional[int] = None) -> str:
        """检索设定与往期剧情，返回可直接放进提示词的短文本。"""
        await self._ensure_started()
        query = (query or "").strip()
        if not self.available or not query:
            return ""
        assert self.client is not None
        try:
            result = await asyncio.wait_for(
                self.client.search(
                    query=query,
                    target_uri=[self.settings.lore_uri, self.settings.memory_uri],
                    limit=limit or self.settings.recall_limit,
                ),
                timeout=self.settings.timeout_s + 2.0,
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self.log.debug("OpenViking 检索失败：%s", exc)
            return ""

        lines: List[str] = []
        seen: set[str] = set()
        entries: List[tuple[str, Any]] = []
        if isinstance(result, dict):
            entries.extend(("往事", item) for item in (result.get("memories") or []))
            entries.extend(("设定", item) for item in (result.get("resources") or []))
        for label, item in entries:
            if not isinstance(item, dict):
                continue
            uri = str(item.get("uri") or "")
            abstract = str(item.get("abstract") or "").strip()
            if not uri or uri in seen:
                continue
            if uri.endswith(".abstract.md") or uri.endswith(".overview.md"):
                continue
            seen.add(uri)
            content = ""
            if int(item.get("level") or 0) >= 2:
                try:
                    content = await asyncio.wait_for(
                        self.client.read(uri, limit=800),
                        timeout=self.settings.timeout_s,
                    )
                except Exception as exc:  # noqa: BLE001
                    self.log.debug("读取记忆正文失败 %s：%s", uri, exc)
            clean = _compact(content or abstract, 600 if content else 360)
            if clean:
                lines.append(f"- [{label}] {clean}")
            if len("\n".join(lines)) >= self.settings.recall_max_chars:
                break

        text = "\n".join(lines)
        if len(text) > self.settings.recall_max_chars:
            text = text[: self.settings.recall_max_chars] + "…"
        return text

    async def write_memory(self, uri: str, content: str, wait: bool = True) -> bool:
        """把一份 Markdown 记忆写入 OpenViking（替换模式）。

        ``wait=True`` 会等摘要与向量处理完成，确保下一轮就能检索到；写
        入频率是分钟级，这个等待可以接受。
        """
        await self._ensure_started()
        if not self.available:
            return False
        assert self.client is not None
        try:
            await self.client.write(uri, content, mode="replace", wait=wait)
            self.last_write_at = time.time()
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self.log.debug("OpenViking 写入失败：%s", exc)
            return False

    async def remember_session(self, session_id: str, content: str) -> bool:
        """把本场摘要写到本次运行独有的路径，便于下一场检索且不覆盖旧场。"""
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id or "default")
        uri = (
            f"{self.settings.memory_uri.rstrip('/')}/live/"
            f"{safe}-{self.run_id}.md"
        )
        return await self.write_memory(uri, content, wait=True)


def _compact(text: str, limit: int) -> str:
    """把多行摘要压成一行，避免提示词里出现大片空行。"""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text
