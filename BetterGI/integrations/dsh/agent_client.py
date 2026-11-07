#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh 的 BetterGI 智能体适配器最小 HTTP 客户端。

只使用 Python 标准库，不依赖第三方包。命令行可从令牌文件或环境变量
BETTERGI_AGENT_TOKEN 读取令牌；服务地址默认 http://127.0.0.1:30648。
"""

import argparse
import base64
import ipaddress
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:30648"
MAX_TOKEN_FILE_BYTES = 4096


def _read_token_file(path):
    """读取有界的单行 Bearer 令牌，不暴露文件名或内容。"""
    with open(path, "rb") as source:
        raw = source.read(MAX_TOKEN_FILE_BYTES + 1)
    if len(raw) > MAX_TOKEN_FILE_BYTES:
        raise ValueError("令牌文件过大")
    token = raw.decode("utf-8-sig")
    if token.endswith("\r\n"):
        token = token[:-2]
    elif token.endswith("\n"):
        token = token[:-1]
    if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise ValueError("令牌文件不是有效的单行 Bearer 令牌")
    return token


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    """令牌文件模式不向重定向目标发送授权头。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_token_file_origin(base_url):
    """令牌文件只允许经 TLS 的远程服务或本机明文服务。"""
    try:
        parts = urllib.parse.urlsplit(base_url)
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or parts.path not in ("", "/")):
            raise ValueError
        _ = parts.port
        if parts.scheme == "http" and parts.hostname.lower() != "localhost":
            if not ipaddress.ip_address(parts.hostname).is_loopback:
                raise ValueError
    except ValueError:
        raise ValueError("令牌文件模式需要本机 HTTP 或 HTTPS 服务根地址") from None


class DshAgentError(Exception):
    """携带 HTTP 状态与稳定错误码的可读异常。"""

    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def __str__(self):
        text = "错误[{0}] {1}".format(self.code, self.message)
        if self.details:
            text += "；详情：" + str(self.details)
        return text


class DshAgentClient:
    """BetterGI /api/v1/agent 的轻量客户端。"""

    def __init__(self, base_url=None, token=None, timeout=30.0, *, token_file_mode=False):
        self.base_url = (
            base_url
            or os.environ.get("BETTERGI_AGENT_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self.token = token or os.environ.get("BETTERGI_AGENT_TOKEN") or ""
        self.timeout = float(timeout)
        self._opener = None
        if token_file_mode:
            _validate_token_file_origin(self.base_url)
            self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirect())

    # ------------------------------------------------------------------
    # 底层请求
    # ------------------------------------------------------------------

    def _send(self, method, path, payload=None):
        """发送请求并返回 (body, headers, status)。

        body 为原始 bytes；HTTP 错误会转换成 DshAgentError。
        """
        if not self.token:
            raise DshAgentError(
                0,
                "NO_TOKEN",
                "未提供访问令牌。请使用 --token-file 或设置环境变量 BETTERGI_AGENT_TOKEN。",
            )

        url = self.base_url + path
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        headers["Authorization"] = "Bearer " + self.token

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            open_request = self._opener.open if self._opener is not None else urllib.request.urlopen
            with open_request(request, timeout=self.timeout) as response:
                body = response.read()
                response_headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
                return body, response_headers, response.status
        except urllib.error.HTTPError as exc:
            body = exc.read()
            raise self._build_http_error(exc.code, body) from None
        except urllib.error.URLError as exc:
            raise DshAgentError(
                0,
                "CONNECTION_FAILED",
                "无法连接 BetterGI 外部访问服务。请确认 BetterGI 已启动、"
                "HTTP API 与智能体控制已启用，并检查地址和端口是否正确。",
                str(getattr(exc, "reason", exc)),
            ) from None
        except (TimeoutError, socket.timeout):
            raise DshAgentError(
                0,
                "TIMEOUT",
                "请求超时。OCR、截图或长动作可能需要更长时间，可增大 --timeout。",
            ) from None

    def _request(self, method, path, payload=None):
        """发送 JSON 请求并解析 JSON 响应。"""
        body, _headers, status = self._send(method, path, payload)
        if status == 204 or not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DshAgentError(
                status,
                "INVALID_RESPONSE",
                "服务返回的内容不是有效 JSON。",
                str(exc),
            ) from None

    def _build_http_error(self, status, body):
        """把错误响应转换为带排查提示的异常。"""
        text = body.decode("utf-8", "replace").strip()
        code = "HTTP_{0}".format(status)
        message = text or "HTTP {0}".format(status)
        details = None
        if text:
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    code = data.get("code") or code
                    # 兼容 ASP.NET Core 框架级 ProblemDetails（title/detail）。
                    message = data.get("message") or data.get("title") or message
                    details = data.get("details") or data.get("detail")
            except json.JSONDecodeError:
                pass

        hints = {
            "AGENT_CONTROL_DISABLED": "请在设置页启用智能体控制并重启 BetterGI。",
            "AGENT_STOPPED": "请先调用 resume 恢复智能体操作。",
            "RUNTIME_UNAVAILABLE": "请先启动游戏运行环境。",
            "BETTERGI_TASK_RUNNING": "BetterGI 正在执行其他任务，请等待任务结束。",
            "COORDINATE_OUT_OF_PRIMARY_SCREEN": "当前只支持主显示器内的绝对坐标动作。",
            "DANGEROUS_SHORTCUT_BLOCKED": "该组合键会切换窗口或打开系统界面，请改用普通游戏按键。",
            "GAME_NOT_FOREGROUND": "游戏窗口未能进入前台，请手动切换或使用后台通道。",
            "TEXT_NOT_SUPPORTED": "当前输入后端不支持文本输入，请改用按键动作。",
            "TEXT_NOT_FOUND": "未在画面中找到目标文本，请先观察或调整 OCR 区域。",
            "SESSION_NOT_FOUND": "会话不存在，请先观察或创建会话。",
            "FRAME_NOT_FOUND": "截图不存在或已被会话清理。",
            "SESSION_STORE_ERROR": "会话目录读写失败，请检查程序目录权限。",
        }
        if code in hints:
            message = "{0}（{1}）".format(message, hints[code])
        elif status == 401:
            message = "{0}（令牌无效或缺失，请在设置页重新复制令牌）".format(message)
        elif status == 404:
            message = "{0}（接口未启用或路径不存在，请检查 HTTP API 与智能体开关）".format(message)
        return DshAgentError(status, code, message, details)

    @staticmethod
    def _session_path(session_id, suffix=""):
        """构造会话路径；省略会话时使用默认会话。"""
        sid = session_id or "default"
        return "/api/v1/agent/sessions/{0}{1}".format(
            urllib.parse.quote(sid, safe=""), suffix
        )

    # ------------------------------------------------------------------
    # 能力
    # ------------------------------------------------------------------

    def status(self):
        """获取适配器、运行环境和固定限制。"""
        return self._request("GET", "/api/v1/agent/status")

    def observe(
        self,
        include_image=False,
        ocr=False,
        session_id=None,
        max_width=1280,
        image_format="png",
        quality=85,
        include_ui=True,
        save_frame=True,
        name=None,
        ocr_roi=None,
    ):
        """观察一帧，可选返回 UI 状态、OCR 与 Base64 图像。"""
        payload = {
            "includeUiState": bool(include_ui),
            "includeOcr": bool(ocr),
            "includeImageBase64": bool(include_image),
            "maxWidth": int(max_width),
            "format": image_format,
            "quality": int(quality),
            "saveFrame": bool(save_frame),
        }
        if session_id:
            payload["sessionId"] = session_id
        if name:
            payload["name"] = name
        if ocr_roi:
            payload["ocrRoi"] = ocr_roi
        return self._request("POST", "/api/v1/agent/observe", payload)

    def act_raw(self, request):
        """发送完整的动作请求字典（camelCase 的 API 字段）。"""
        if not isinstance(request, dict) or not isinstance(request.get("actions"), list):
            raise DshAgentError(
                0,
                "INVALID_REQUEST",
                "动作请求必须是包含 actions 数组的 JSON 对象。",
            )
        return self._request("POST", "/api/v1/agent/actions", request)

    def act(
        self,
        actions,
        session_id=None,
        channel="foreground",
        capture_after=True,
        include_ui_state=True,
        include_ocr=False,
        include_image=False,
        max_width=1280,
        stop_on_error=True,
        image_format="png",
        quality=85,
    ):
        """执行一组动作，默认结束后观察一帧。"""
        request = {
            "channel": channel,
            "actions": actions,
            "stopOnError": bool(stop_on_error),
            "captureAfter": bool(capture_after),
            "includeUiState": bool(include_ui_state),
            "includeOcr": bool(include_ocr),
            "includeImageBase64": bool(include_image),
            "maxWidth": int(max_width),
            "format": image_format,
            "quality": int(quality),
        }
        if session_id:
            request["sessionId"] = session_id
        return self.act_raw(request)

    def screenshot(
        self,
        path,
        session_id=None,
        ocr=False,
        include_ui=True,
        max_width=1280,
        image_format="png",
        quality=85,
        save_frame=True,
        name=None,
    ):
        """下载一帧截图到本地文件，返回响应头中的帧信息。"""
        params = {
            "maxWidth": str(int(max_width)),
            "format": image_format,
            "quality": str(int(quality)),
            "ocr": "true" if ocr else "false",
            "ui": "true" if include_ui else "false",
            "save": "true" if save_frame else "false",
        }
        if session_id:
            params["sessionId"] = session_id
        if name:
            params["name"] = name

        query = urllib.parse.urlencode(params)
        body, headers, status = self._send(
            "GET", "/api/v1/agent/screenshot?" + query
        )
        if status == 204 or not body:
            raise DshAgentError(
                status, "EMPTY_SCREENSHOT", "服务没有返回截图内容。"
            )

        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as output:
            output.write(body)

        return {
            "path": path,
            "bytes": len(body),
            "contentType": headers.get("content-type"),
            "frameId": headers.get("x-bgi-frame-id"),
            "sessionId": headers.get("x-bgi-session"),
            "uiCategory": headers.get("x-bgi-ui-category"),
        }

    def memory_get(self, session_id=None):
        """读取会话内存。"""
        return self._request("GET", self._session_path(session_id, "/memory"))

    def memory_set(self, values, session_id=None):
        """合并写入会话内存；值为 None 的键会被删除。"""
        if not isinstance(values, dict):
            raise DshAgentError(
                0, "INVALID_REQUEST", "内存内容必须是 JSON 对象。"
            )
        return self._request(
            "POST", self._session_path(session_id, "/memory"), values
        )

    def session_create(self, session_id=None, name=None):
        """确保会话存在，可同时更新会话名称。"""
        payload = {}
        if session_id:
            payload["sessionId"] = session_id
        if name:
            payload["name"] = name
        return self._request("POST", "/api/v1/agent/sessions", payload)

    def session_list(self):
        """列出全部会话。"""
        return self._request("GET", "/api/v1/agent/sessions")

    def session_resume(self, session_id=None, limit=50):
        """获取恢复会话所需的信息。"""
        query = urllib.parse.urlencode({"limit": int(limit)})
        return self._request(
            "GET", self._session_path(session_id, "/resume?" + query)
        )

    def events(self, session_id=None, after_seq=0, limit=100):
        """按序号增量读取会话事件。"""
        query = urllib.parse.urlencode(
            {"afterSeq": int(after_seq), "limit": int(limit)}
        )
        return self._request(
            "GET", self._session_path(session_id, "/events?" + query)
        )

    def memory_delete(self, key, session_id=None):
        """删除一个会话内存键。"""
        if not key:
            raise DshAgentError(0, "INVALID_REQUEST", "内存键不能为空。")
        return self._request(
            "DELETE",
            self._session_path(session_id, "/memory/" + urllib.parse.quote(key, safe="")),
        )

    def checkpoints_create(self, session_id=None, name=None, note=None):
        """创建会话检查点。"""
        payload = {}
        if name:
            payload["name"] = name
        if note:
            payload["note"] = note
        return self._request(
            "POST", self._session_path(session_id, "/checkpoints"), payload
        )

    def checkpoints_list(self, session_id=None):
        """列出会话检查点。"""
        return self._request(
            "GET", self._session_path(session_id, "/checkpoints")
        )

    def runtime_start(self):
        """启动游戏运行环境与实时触发器。"""
        return self._request("POST", "/api/v1/agent/runtime/start")

    def runtime_stop(self):
        """停止游戏运行环境；成功时服务返回 204。"""
        return self._request("POST", "/api/v1/agent/runtime/stop")

    def stop(self):
        """请求停止当前智能体操作。"""
        return self._request("POST", "/api/v1/agent/stop")

    def resume(self):
        """恢复被停止的智能体操作。"""
        return self._request("POST", "/api/v1/agent/resume")


# ----------------------------------------------------------------------
# 命令行入口
# ----------------------------------------------------------------------


def _load_action_payload(text):
    """读取内联 JSON、@文件 或直接路径，返回动作请求对象。"""
    raw = text
    if text.startswith("@"):
        with open(text[1:], "r", encoding="utf-8") as source:
            raw = source.read()
    elif os.path.isfile(text):
        with open(text, "r", encoding="utf-8") as source:
            raw = source.read()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DshAgentError(
            0, "INVALID_JSON", "动作 JSON 无法解析。", str(exc)
        ) from None

    if isinstance(data, list):
        return {"actions": data}
    if isinstance(data, dict) and isinstance(data.get("actions"), list):
        return data
    raise DshAgentError(
        0,
        "INVALID_JSON",
        "动作 JSON 必须是动作数组，或包含 actions 数组的对象。",
    )


def _load_json_object(text):
    """读取内联 JSON、@文件 或直接路径，返回 JSON 对象。"""
    raw = text
    if text.startswith("@"):
        with open(text[1:], "r", encoding="utf-8") as source:
            raw = source.read()
    elif os.path.isfile(text):
        with open(text, "r", encoding="utf-8") as source:
            raw = source.read()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DshAgentError(
            0, "INVALID_JSON", "JSON 无法解析。", str(exc)
        ) from None

    if not isinstance(data, dict):
        raise DshAgentError(0, "INVALID_JSON", "内存内容必须是 JSON 对象。")
    return data


def _save_observation_image(result, path):
    """把观察结果中的 Base64 图像写到文件，并从打印内容中移除大字段。"""
    encoded = result.get("imageBase64")
    if not encoded:
        raise DshAgentError(
            0,
            "NO_IMAGE",
            "响应中没有图像数据，请确认请求已包含 includeImageBase64。",
        )
    image = base64.b64decode(encoded)
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as output:
        output.write(image)

    trimmed = dict(result)
    trimmed.pop("imageBase64", None)
    trimmed["imageFile"] = path
    trimmed["imageBytes"] = len(image)
    return trimmed


def build_parser():
    parser = argparse.ArgumentParser(
        description="BetterGI 智能体适配器命令行客户端（dsh 示例）"
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("BETTERGI_AGENT_BASE_URL", DEFAULT_BASE_URL),
        help="服务地址，默认 %(default)s",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("BETTERGI_AGENT_TOKEN"),
        help="访问令牌，默认读取环境变量 BETTERGI_AGENT_TOKEN",
    )
    parser.add_argument(
        "--token-file", default=None, help="从本地文件读取单行访问令牌，优先于环境变量",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="请求超时秒数，默认 30"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="查看适配器与运行环境状态")

    observe = subparsers.add_parser("observe", help="观察一帧")
    observe.add_argument("--session", default=None, help="会话标识")
    observe.add_argument("--ocr", action="store_true", help="执行全屏 OCR")
    observe.add_argument(
        "--image", action="store_true", help="在 JSON 中返回 Base64 图像"
    )
    observe.add_argument(
        "--out", default=None, help="把图像保存到指定路径（隐含 --image）"
    )
    observe.add_argument("--max-width", type=int, default=1280)
    observe.add_argument("--format", default="png", choices=["png", "jpg"])
    observe.add_argument("--quality", type=int, default=85)
    observe.add_argument("--no-ui", action="store_true", help="不分析 UI 状态")
    observe.add_argument("--no-save", action="store_true", help="不保存截图")
    observe.add_argument("--name", default=None, help="帧名称")

    screenshot = subparsers.add_parser("screenshot", help="下载一帧截图")
    screenshot.add_argument("path", help="保存路径")
    screenshot.add_argument("--session", default=None, help="会话标识")
    screenshot.add_argument("--ocr", action="store_true", help="同时执行 OCR")
    screenshot.add_argument("--no-ui", action="store_true", help="不分析 UI 状态")
    screenshot.add_argument("--max-width", type=int, default=1280)
    screenshot.add_argument("--format", default="png", choices=["png", "jpg"])
    screenshot.add_argument("--quality", type=int, default=85)
    screenshot.add_argument("--no-save", action="store_true", help="不写入会话目录")
    screenshot.add_argument("--name", default=None, help="帧名称")

    key = subparsers.add_parser("key", help="发送一个按键动作")
    key.add_argument("key", help="键名，例如 W、E、Space、F1")
    key.add_argument(
        "--action", default="press", choices=["press", "down", "up"]
    )
    key.add_argument("--duration-ms", type=int, default=0, help="按住毫秒数")
    key.add_argument(
        "--channel", default="foreground", choices=["foreground", "background"]
    )
    key.add_argument("--session", default=None)
    key.add_argument("--no-capture", action="store_true", help="结束后不观察")
    key.add_argument("--image", action="store_true", help="结束后返回图像")

    click = subparsers.add_parser("click", help="发送一个点击动作")
    click.add_argument("x", type=float)
    click.add_argument("y", type=float)
    click.add_argument(
        "--space",
        default="normalized",
        choices=["normalized", "capture", "desktop"],
        help="坐标空间，默认 normalized",
    )
    click.add_argument(
        "--button", default="left", choices=["left", "right", "middle"]
    )
    click.add_argument("--clicks", type=int, default=1)
    click.add_argument(
        "--channel", default="foreground", choices=["foreground", "background"]
    )
    click.add_argument("--session", default=None)
    click.add_argument("--no-capture", action="store_true", help="结束后不观察")
    click.add_argument("--image", action="store_true", help="结束后返回图像")

    act_json = subparsers.add_parser(
        "act-json", help="执行 JSON 文件或内联 JSON 中的动作序列"
    )
    act_json.add_argument(
        "payload", help="内联 JSON、@文件路径 或直接文件路径"
    )
    act_json.add_argument("--session", default=None)
    act_json.add_argument(
        "--channel", default=None, choices=["foreground", "background"]
    )
    act_json.add_argument(
        "--no-capture", action="store_true", help="结束后不观察"
    )
    act_json.add_argument("--image", action="store_true", help="结束后返回图像")
    act_json.add_argument("--max-width", type=int, default=None)

    subparsers.add_parser("stop", help="停止智能体输入并拒绝后续输入")
    subparsers.add_parser("resume", help="恢复被停止的智能体输入")
    subparsers.add_parser("runtime-start", help="启动游戏运行环境与实时触发器")
    subparsers.add_parser("runtime-stop", help="停止游戏运行环境")

    session_create = subparsers.add_parser("session-create", help="确保会话存在")
    session_create.add_argument("--session", default=None, help="会话标识")
    session_create.add_argument("--name", default=None, help="会话名称")

    subparsers.add_parser("session-list", help="列出全部会话")

    session_resume = subparsers.add_parser("session-resume", help="获取恢复会话信息")
    session_resume.add_argument("--session", default=None, help="会话标识")
    session_resume.add_argument("--limit", type=int, default=50, help="最近事件数量")

    events = subparsers.add_parser("events", help="增量读取会话事件")
    events.add_argument("--session", default=None, help="会话标识")
    events.add_argument("--after-seq", type=int, default=0, help="起始事件序号")
    events.add_argument("--limit", type=int, default=100, help="返回数量，1～500")

    mem_get = subparsers.add_parser("mem-get", help="读取会话内存")
    mem_get.add_argument("--session", default=None, help="会话标识")

    mem_set = subparsers.add_parser("mem-set", help="合并写入会话内存")
    mem_set.add_argument("payload", help="JSON 对象、@文件路径 或直接文件路径")
    mem_set.add_argument("--session", default=None, help="会话标识")

    mem_del = subparsers.add_parser("mem-del", help="删除一个会话内存键")
    mem_del.add_argument("key", help="内存键")
    mem_del.add_argument("--session", default=None, help="会话标识")

    checkpoint_create = subparsers.add_parser("checkpoint-create", help="创建检查点")
    checkpoint_create.add_argument("--session", default=None, help="会话标识")
    checkpoint_create.add_argument("--name", default=None, help="检查点名称")
    checkpoint_create.add_argument("--note", default=None, help="检查点说明")

    checkpoint_list = subparsers.add_parser("checkpoint-list", help="列出检查点")
    checkpoint_list.add_argument("--session", default=None, help="会话标识")

    return parser


def main(argv=None):
    # Windows 控制台默认编码不一定是 UTF-8，尽量切换，避免中文输出失败。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    has_token_file = args.token_file is not None
    try:
        token = _read_token_file(args.token_file) if has_token_file else args.token
        client_options = {"base_url": args.base_url, "token": token, "timeout": args.timeout}
        if has_token_file:
            client_options["token_file_mode"] = True
        client = DshAgentClient(**client_options)
    except (OSError, UnicodeError, ValueError):
        sys.stderr.write("令牌文件或服务地址无效，无法启动客户端。\n")
        return 2

    def redact(text):
        if not has_token_file:
            return text
        escaped = json.dumps(token, ensure_ascii=False)[1:-1]
        return text.replace(token, "[REDACTED]").replace(escaped, "[REDACTED]")

    def _print_json(value):
        print(redact(json.dumps(value, ensure_ascii=False, indent=2)))

    try:
        if args.command == "status":
            _print_json(client.status())
            return 0

        if args.command == "observe":
            save_to = args.out
            result = client.observe(
                include_image=args.image or bool(save_to),
                ocr=args.ocr,
                session_id=args.session,
                max_width=args.max_width,
                image_format=args.format,
                quality=args.quality,
                include_ui=not args.no_ui,
                save_frame=not args.no_save,
                name=args.name,
            )
            if save_to:
                result = _save_observation_image(result, save_to)
            _print_json(result)
            return 0

        if args.command == "screenshot":
            _print_json(
                client.screenshot(
                    args.path,
                    session_id=args.session,
                    ocr=args.ocr,
                    include_ui=not args.no_ui,
                    max_width=args.max_width,
                    image_format=args.format,
                    quality=args.quality,
                    save_frame=not args.no_save,
                    name=args.name,
                )
            )
            return 0

        if args.command == "key":
            action = {"type": "key", "key": args.key, "keyAction": args.action}
            if args.duration_ms > 0:
                action["durationMs"] = args.duration_ms
            _print_json(
                client.act(
                    [action],
                    session_id=args.session,
                    channel=args.channel,
                    capture_after=not args.no_capture,
                    include_image=args.image,
                )
            )
            return 0

        if args.command == "click":
            action = {
                "type": "click",
                "x": args.x,
                "y": args.y,
                "coordinateSpace": args.space,
                "button": args.button,
                "clicks": args.clicks,
            }
            _print_json(
                client.act(
                    [action],
                    session_id=args.session,
                    channel=args.channel,
                    capture_after=not args.no_capture,
                    include_image=args.image,
                )
            )
            return 0

        if args.command == "act-json":
            request = _load_action_payload(args.payload)
            if args.session:
                request["sessionId"] = args.session
            if args.channel:
                request["channel"] = args.channel
            if args.no_capture:
                request["captureAfter"] = False
            if args.image:
                request["includeImageBase64"] = True
            if args.max_width is not None:
                request["maxWidth"] = args.max_width
            _print_json(client.act_raw(request))
            return 0

        if args.command == "stop":
            _print_json(client.stop())
            return 0

        if args.command == "resume":
            _print_json(client.resume())
            return 0

        if args.command == "runtime-start":
            _print_json(client.runtime_start())
            return 0

        if args.command == "runtime-stop":
            result = client.runtime_stop()
            _print_json(result if result is not None else {"stopped": True})
            return 0

        if args.command == "session-create":
            _print_json(
                client.session_create(session_id=args.session, name=args.name)
            )
            return 0

        if args.command == "session-list":
            _print_json(client.session_list())
            return 0

        if args.command == "session-resume":
            _print_json(
                client.session_resume(session_id=args.session, limit=args.limit)
            )
            return 0

        if args.command == "events":
            _print_json(
                client.events(
                    session_id=args.session,
                    after_seq=args.after_seq,
                    limit=args.limit,
                )
            )
            return 0

        if args.command == "mem-get":
            _print_json(client.memory_get(session_id=args.session))
            return 0

        if args.command == "mem-set":
            _print_json(
                client.memory_set(
                    _load_json_object(args.payload), session_id=args.session
                )
            )
            return 0

        if args.command == "mem-del":
            _print_json(
                client.memory_delete(args.key, session_id=args.session)
            )
            return 0

        if args.command == "checkpoint-create":
            _print_json(
                client.checkpoints_create(
                    session_id=args.session,
                    name=args.name,
                    note=args.note,
                )
            )
            return 0

        if args.command == "checkpoint-list":
            _print_json(client.checkpoints_list(session_id=args.session))
            return 0

        parser.error("未知命令")
        return 2
    except DshAgentError as exc:
        sys.stderr.write(redact(str(exc)) + "\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("已中断。\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
