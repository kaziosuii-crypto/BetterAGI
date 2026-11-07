#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP stdio 到 BetterGI Streamable HTTP 的便捷桥。

stdin 读取按行分隔的 JSON-RPC，POST 到 http://127.0.0.1:30648/mcp，
再把 JSON 或 SSE 的 data 负载按行写回 stdout。

这只是便利桥：支持 MCP 的客户端也可以直接连接
http://127.0.0.1:30648/mcp 并携带 Authorization: Bearer <token>。

环境变量：
  BETTERGI_AGENT_TOKEN       必填，BetterGI 设置页复制的访问令牌
  BETTERGI_AGENT_BASE_URL    可选，默认 http://127.0.0.1:30648
"""

import json
import os
import socket
import sys
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:30648"


def log(message):
    """日志只写 stderr，避免污染 stdout 上的 JSON-RPC。"""
    sys.stderr.write("[bettergi-mcp] " + str(message) + "\n")
    sys.stderr.flush()


def normalize_message(text):
    """把一条 JSON-RPC 消息压成单行，保证 newline-delimited 输出。"""
    text = text.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        # 不是 JSON 时保留内容，但压成一行。
        return " ".join(line.strip() for line in text.splitlines() if line.strip())
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def write_message(text):
    """以 UTF-8 写出一条 JSON-RPC 消息。"""
    if not text:
        return
    sys.stdout.buffer.write(text.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def parse_sse(body):
    """从 SSE 响应中提取 each event 的 data 负载。"""
    text = body.decode("utf-8", "replace")
    messages = []
    data_lines = []

    def flush_event():
        if not data_lines:
            return
        data = "\n".join(data_lines).strip()
        data_lines.clear()
        if data:
            messages.append(data)

    for line in text.splitlines():
        if line == "":
            flush_event()
            continue
        if line.startswith(":"):
            # SSE 注释或心跳。
            continue
        field, separator, value = line.partition(":")
        if not separator:
            field, value = line, ""
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)

    flush_event()
    return messages


class McpHttpBridge:
    """维护连接状态并把 stdio 请求转发到 /mcp。"""

    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session_id = None
        self.protocol_version = None

    def _headers(self):
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _request(self, payload):
        request = urllib.request.Request(
            self.base_url + "/mcp",
            data=payload.encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            # 无状态服务通常不回传会话头，但兼容有状态实现。
            session_id = response.headers.get("Mcp-Session-Id")
            if session_id:
                self.session_id = session_id

            body = response.read()
            content_type = (response.headers.get("Content-Type") or "").lower()
            if response.status in (202, 204) or not body:
                return []

            if "text/event-stream" in content_type:
                return parse_sse(body)

            if "application/json" in content_type:
                text = body.decode("utf-8", "replace")
                normalized = normalize_message(text)
                return [normalized] if normalized else []

            # 未预期类型：尽量按 JSON 处理，否则记录日志。
            normalized = normalize_message(body.decode("utf-8", "replace"))
            if normalized:
                return [normalized]
            log("忽略非 JSON 响应，Content-Type=" + content_type)
            return []

    def forward(self, line):
        """转发一条 stdin 消息，返回需要写回的消息列表。"""
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            log("无法解析 stdin JSON：" + str(exc))
            return [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    },
                    separators=(",", ":"),
                )
            ]

        if isinstance(message, dict) and message.get("method") == "initialize":
            params = message.get("params") or {}
            version = params.get("protocolVersion")
            if isinstance(version, str):
                # initialize 成功后再随后续请求发送协议版本。
                self.protocol_version = None
                pending_version = version
            else:
                pending_version = None
        else:
            pending_version = None

        # 通知没有 id，不应给客户端写回任何响应。
        is_notification = isinstance(message, dict) and "id" not in message

        try:
            messages = self._request(line)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            text = body.decode("utf-8", "replace").strip()
            log("HTTP {0}: {1}".format(exc.code, text or exc.reason))
            if is_notification:
                return []
            # 只有真正的 JSON-RPC 响应才原样转发；ProblemDetails 等任意 HTTP
            # 错误体可能不是合法的 JSON-RPC，必须改写为 JSON-RPC error。
            try:
                parsed = json.loads(text) if text else None
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("jsonrpc") == "2.0":
                response_id = parsed.get("id")
                request_id = message.get("id") if isinstance(message, dict) else None
                # 接受匹配的 id 或省略 id 的 JSON-RPC 错误；不转发其他请求的响应。
                if response_id is None or response_id == request_id:
                    forwarded = normalize_message(text)
                    if forwarded:
                        return [forwarded]
            return self._build_error(message, exc.code, text or str(exc.reason))
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            log("无法连接 BetterGI：" + reason)
            if is_notification:
                return []
            return self._build_error(message, 0, reason)
        except (TimeoutError, socket.timeout):
            log("请求超时")
            if is_notification:
                return []
            return self._build_error(message, 0, "Request timed out")

        if pending_version:
            self.protocol_version = pending_version
        if is_notification:
            return []
        return [item for item in messages if item]

    @staticmethod
    def _build_error(message, status, detail):
        """通知不响应；请求返回 JSON-RPC 错误对象。"""
        if not isinstance(message, dict) or "id" not in message:
            return []
        status_text = "HTTP {0}".format(status) if status else "connection error"
        return [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {
                        "code": -32000,
                        "message": "BetterGI MCP request failed ({0})".format(
                            status_text
                        ),
                        "data": detail,
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        ]


def main():
    token = os.environ.get("BETTERGI_AGENT_TOKEN", "").strip()
    if not token:
        log("缺少环境变量 BETTERGI_AGENT_TOKEN，无法连接 BetterGI。")
        return 2

    base_url = (
        os.environ.get("BETTERGI_AGENT_BASE_URL") or DEFAULT_BASE_URL
    ).strip()
    bridge = McpHttpBridge(base_url, token)

    try:
        for raw_line in sys.stdin.buffer:
            line = raw_line.decode("utf-8", "replace").strip().lstrip("﻿")
            if not line:
                continue
            for output in bridge.forward(line):
                write_message(output)
    except BrokenPipeError:
        # 客户端已退出，静默结束。
        return 0
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
