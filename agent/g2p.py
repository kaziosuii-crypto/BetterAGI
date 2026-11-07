
"""Game2Player-4B 手模型适配层（2026-11-05 用户拍板：把 GameScaling 9B 换成 Game2Player 4B）。

口径来源：HuggingFace 模型卡 game2player/Game2Player-4B（2026-10-29 发布，base Qwen3.5-4B）。
与 GameScaling 的差别（照抄模型卡，不自己发明）：

- 输入：system 里写 Game Rules / Role and Controls / Task Instruction / Output Format；
  user 里是 "current screenshot"，最多再带 6 张历史帧和最多 50 条历史动作（每条一句 note）。
  官方建议总图数 <= 7，每图约 1024 vision token。
- 输出：<tool_call> 里一个 computer_use 动作：
      {"name":"computer_use","arguments":{"action":"press_key","key":"ArrowRight","reasoning":"..."}}
  也可能是 Qwen 原生 XML 形式（<function=computer_use><parameter=action>...）。
  鼠标坐标是 0-1000 归一化；时长单位是秒。
- 思考：chat_template_kwargs {"enable_thinking": bool}，思考链单独返回，历史里只留 note。

本模块只做三件事：拼请求、解析动作、把「一个动作」翻成 executor 认识的 6 步键位组。
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence

try:  # 帧压缩是可选的：没有 Pillow 就原样上传，不让链路断在这
    import frame_codec as _frame_codec
except Exception:  # noqa: BLE001
    _frame_codec = None

try:  # 帧间差分（MP4 式 P 帧）：没有 Pillow/numpy 就退回整帧
    import frame_delta as _frame_delta
except Exception:  # noqa: BLE001
    _frame_delta = None

#: 端点：默认走本机隧道 18042（云机 8080 -> 8000，走 i-2.gpushare.com:47960 也可以直连）。
DEFAULT_ENDPOINT = "http://127.0.0.1:18042"
URL_ENV = "BETTERAGI_G2P_URL"
MODEL = "game2player-4b"
MODEL_ENV = "BETTERAGI_G2P_MODEL"
BACKEND_ENV = "BETTERAGI_MOTOR_BACKEND"
#: 默认后端 = gamescaling（用户 2026-11-05 m07298：把 agent 改回调用 gamescaling，
#: 之前的设计照旧）。g2p 只是备选，必须显式设 BETTERAGI_MOTOR_BACKEND=game2player 才启用 ——
#: 否则任何没带环境变量的入口（脚本、复测、子进程）都会静默跑到 4B 上。
BACKEND = "gamescaling"

#: 连接复用（用户 2026-11-05 要求）：以前每个决策都新建 TCP，613–702ms 的单步白扔一次握手。
#: 实例上缓存一个带 keep-alive 的 Client，timeout 按请求传（决策用的短、健康检查用的长）。
KEEPALIVE_CONNECTIONS = 8
KEEPALIVE_EXPIRY_S = 30.0

ACTION_NAME = "computer_use"
DEFAULT_DURATION_S = 0.15
MAX_HISTORY_ACTIONS = 50
MAX_HISTORY_FRAMES = 6
# 每步带几张历史帧：官方要 6，但上行只有 0.1–1.1MB/s，6 张 ≈ 1MB/步。
HISTORY_FRAMES_ENV = "BETTERAGI_G2P_HISTORY_FRAMES"
DEFAULT_HISTORY_FRAMES = 1

#: 帧间差分（2026-11-05 用户要求「参考 mp4 编码，只发变化部分」）。
#: 打开后：当前帧只发变化块，历史帧改成引用中继里已存的帧（零字节）。
#: 需要 GPU 机上的中继 svc/frame_relay.py；终端地址放 RELAY_ENV，中继自己再转给 vLLM。
DELTA_ENV = "BETTERAGI_FRAME_DELTA"
RELAY_ENV = "BETTERAGI_FRAME_RELAY"
DELTA_SID_PREFIX = "g2p-"


def relay_endpoint() -> str:
    """中继地址；没配就返回空串（表示不做差分，照旧发整帧）。"""
    return str(os.environ.get(RELAY_ENV) or "").strip().rstrip("/")


def delta_enabled() -> bool:
    flag = str(os.environ.get(DELTA_ENV) or "").strip().lower()
    if flag in ("0", "false", "off", "no"):
        return False
    return bool(relay_endpoint()) and _frame_delta is not None
MAX_IMAGES = 7

USER_HEAD = "# Current step\n"
USER_TAIL = "\nReturn the next action."

KEY_ALIASES = {
    "arrowup": "Up", "arrowdown": "Down", "arrowleft": "Left", "arrowright": "Right",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "escape": "Esc", "esc": "Esc", "backspace": "Back", "back": "Back",
    "enter": "Enter", "return": "Enter", "space": "Space", "spacebar": "Space",
    "tab": "Tab", "shift": "Shift", "ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt",
    "capslock": "Caps", "delete": "Delete", "insert": "Insert",
    "home": "Home", "end": "End", "pageup": "Prior", "pagedown": "Next",
    "mouseleft": "LB", "mouseright": "RB", "mousemiddle": "MB",
}

F_KEYS = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six",
          7: "Seven", 8: "Eight", 9: "Nine", 10: "Ten", 11: "Eleven", 12: "Twelve"}

#: 鼠标类动作：键盘游戏用不上，但要如实认出来（别当成解析失败去熔断）。
MOUSE_ACTIONS = ("click", "left_click", "right_click", "double_click", "move_mouse",
                 "mouse_move", "move", "drag", "scroll", "wheel")
WAIT_ACTIONS = ("wait", "noop", "no_op", "none", "idle")
PRESS_ACTIONS = ("press_key", "key_press", "key", "press", "tap", "hold_key", "key_down")

SYSTEM_TEMPLATE = """You are an expert game agent. Choose the next action from the screenshot.

# Game Rules
{game_rules}

# Role and Controls
{controls}

# Task Instruction
{goal}

# Output Format
Return exactly one action inside <tool_call> tags.
Use press_key with a key from the controls above, or wait with a duration in seconds.
If the key must be held down (walking, aiming, charging), add "duration" in seconds
(e.g. 1.5). If you only want a single tap, omit duration.
Include reasoning with one short sentence explaining the action.
Think in the thinking channel, then emit only the tool call as the final answer.
Example:
<tool_call>{{"name":"computer_use","arguments":{{"action":"press_key","key":"ArrowRight","reasoning":"Move toward the exit."}}}}</tool_call>
"""


def backend() -> str:
    """当前配的手模型后端：game2player（默认）或 gamescaling。"""
    return str(os.environ.get(BACKEND_ENV) or BACKEND).strip().lower()


def endpoint() -> str:
    return str(os.environ.get(URL_ENV) or DEFAULT_ENDPOINT).rstrip("/")


def build_system_prompt(game: str = "", game_rules: str = "", controls: str = "",
                        goal: str = "") -> str:
    """按模型卡的骨架拼 system。空块写 (none) —— 空标题会让模型自己编规则。"""
    return SYSTEM_TEMPLATE.format(
        game_rules=str(game_rules or "").strip() or "(none)",
        controls=str(controls or "").strip() or "(none)",
        goal=str(goal or "").strip() or "(none)",
    )


def normalize_key(name: Any) -> str:
    token = str(name or "").strip()
    if not token:
        return ""
    low = token.lower().replace(" ", "").replace("-", "")
    if low in KEY_ALIASES:
        return KEY_ALIASES[low]
    match = re.fullmatch(r"f([1-9]|1[0-2])", low)
    if match:
        return F_KEYS[int(match.group(1))]
    if len(token) == 1:
        return token.upper()
    return token


_CALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.S)
_FUNC_RE = re.compile(r"<function=([A-Za-z0-9_\-]+)>")
_PARAM_RE = re.compile(r"<parameter=([A-Za-z0-9_\-]+)>\s*(.*?)\s*</parameter>", re.S)


def parse_tool_call(text: str) -> Dict[str, Any]:
    """从模型输出里取出那个 tool_call（JSON 或 Qwen XML 两种形状都收）。

    只认**最后一个** <tool_call>：思考链里可能引用了示例，最后一个才是真决定。
    """
    raw = str(text or "")
    blocks = _CALL_RE.findall(raw)
    if not blocks:
        raise ValueError("没有 <tool_call> 块")
    inner = blocks[-1].strip()
    if inner.startswith("{"):
        data = json.loads(inner)
        args = data.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"action": args}
        return {"name": str(data.get("name") or ACTION_NAME), "arguments": dict(args)}
    name_match = _FUNC_RE.search(inner)
    if not name_match:
        raise ValueError("tool_call 里没有 function= 也没有 JSON")
    args: Dict[str, Any] = {}
    for key, value in _PARAM_RE.findall(inner):
        text_value = value.strip()
        if text_value.lower() in ("true", "false"):
            args[str(key)] = text_value.lower() == "true"
            continue
        try:
            args[str(key)] = json.loads(text_value)
        except ValueError:
            args[str(key)] = text_value
    return {"name": name_match.group(1), "arguments": args}


def _duration_ms(args: Dict[str, Any], default_s: float) -> int:
    for key in ("duration", "duration_s", "seconds", "hold", "hold_s"):
        if key in args and args[key] not in (None, ""):
            try:
                return max(0, int(round(float(args[key]) * 1000)))
            except (TypeError, ValueError):
                continue
    return max(0, int(round(default_s * 1000)))


def action_to_steps(args: Dict[str, Any], *, step_ms: int = 33, max_steps: int = 6,
                     default_duration_s: float = DEFAULT_DURATION_S) -> Dict[str, Any]:
    """一个 Game2Player 动作 -> executor 认识的一组键位 + 按住时长（2026-11-05 勘误）。

    这里以前是错的：GameScaling 的语义才是「一次决策 = 6 x 33ms」，Game2Player 的
    一次输出**本来就是动作本身**，按住多久由它自己的 duration 字段给（模型卡：
    "action durations are in seconds"）。所以：

    - press_key -> 只有一组 [[key]]，配 hold_ms = duration x 1000；
    - 不要复制成 6 组同键 —— 那是我们自己发明的节拍，等于把「按住 2 秒往右走」
      变成「敲 6 下右键」。
    """
    action = str(args.get("action") or args.get("type") or "").strip().lower()
    reason = str(args.get("reasoning") or args.get("reason") or "").strip()
    mouse: Dict[str, Any] = {}
    for key in ("x", "y"):
        if key in args:
            try:
                mouse[key] = int(round(float(args[key])))
            except (TypeError, ValueError):
                pass
    if "button" in args:
        mouse["button"] = str(args["button"])

    if action in PRESS_ACTIONS:
        raw_key = args.get("key") or args.get("keys") or args.get("button") or ""
        if isinstance(raw_key, (list, tuple)):
            raw_key = raw_key[0] if raw_key else ""
        token = normalize_key(raw_key)
        if not token:
            raise ValueError("press_key 没有给 key")
        hold_ms = _duration_ms(args, default_duration_s)
        # 一组就够了：按住时长交给 hold_ms，executor 用 step_ms=hold_ms 按一次。
        return {"action": action, "keys": [token], "steps": [[token]], "mouse": mouse,
                "reason": reason, "hold_ms": hold_ms, "parsed": True}

    if action in WAIT_ACTIONS or not action:
        hold_ms = _duration_ms(args, DEFAULT_DURATION_S)
        return {"action": action or "wait", "keys": [], "steps": [[]], "mouse": mouse,
                "reason": reason, "hold_ms": hold_ms, "parsed": True}

    if action in MOUSE_ACTIONS:
        hold_ms = _duration_ms(args, DEFAULT_DURATION_S)
        return {"action": action, "keys": [], "steps": [], "mouse": mouse,
                "reason": reason, "hold_ms": hold_ms, "parsed": True,
                "unsupported": "键盘游戏不支持鼠标动作"}

    raise ValueError("不认识的动作 %s" % action)


def history_note(decision: Dict[str, Any]) -> str:
    """历史里存的那一句 note（模型卡：previous actions with their short notes）。"""
    action = str(decision.get("action") or "")
    keys = decision.get("keys") or []
    reason = str(decision.get("reason") or "").strip()
    head = "%s %s" % (action, ",".join(keys)) if keys else action
    hold_ms = int(decision.get("hold_ms") or 0)
    if hold_ms:
        head += " (%.1fs)" % (hold_ms / 1000.0)
    return (head + (" - " + reason if reason else "")).strip()


def build_messages(system: str, frame_url: str, history: Optional[Sequence[Dict[str, Any]]] = None,
                   notes: Optional[Sequence[str]] = None,
                   max_frames: Optional[int] = None,
                   frame_ref: bool = False) -> List[Dict[str, Any]]:
    """拼一条 user：历史动作 note + 最多 max_frames 张历史帧 + 当前帧。总图数 <= 7。

    max_frames 默认取 HISTORY_FRAMES_ENV（1）：每张 640x480 JPEG 约 30KB，
    官方要求 6 张历史帧，但那条 5090 的上行只有 0.1–1.1MB/s，6 张就是 1MB+，
    每步 5–10s——先只带 1 张，动作 note 仍然是完整的 20 条，模型靠 note 自纠。
    """
    history = list(history or [])[-MAX_HISTORY_FRAMES:]
    notes = [str(n).strip() for n in (notes or []) if str(n).strip()][-MAX_HISTORY_ACTIONS:]
    parts_text = USER_HEAD
    if notes:
        parts_text += "Previous actions:\n" + "\n".join("- " + n for n in notes) + "\n\n"
    parts_text += "Current game screen:\n"
    content: List[Dict[str, Any]] = [{"type": "text", "text": parts_text}]
    frames = [str(item.get("frame_url") or "") for item in history
              if str(item.get("frame_url") or "").startswith("data:image/")]
    limit = MAX_HISTORY_FRAMES if max_frames is None else max(0, int(max_frames))
    if limit:
        frames = frames[-limit:]
    if frame_ref:
        # 差分模式：历史帧交给中继自己去取（frame://-1 是上一帧，-2 是上上帧……），一个字节都不传
        for i in range(len(frames), 0, -1):
            content.append({"type": "image_url", "image_url": {"url": "frame://-%d" % i}})
        content.append({"type": "image_url", "image_url": {"url": "delta://cur"}})
    else:
        for url in frames:
            content.append({"type": "image_url", "image_url": {"url": url}})
        content.append({"type": "image_url", "image_url": {"url": str(frame_url or "")}})
    content.append({"type": "text", "text": USER_TAIL})
    if len(content) > MAX_IMAGES + 2:
        keep = content[:1] + content[-(MAX_IMAGES + 1):]
        content = keep
    return [
        {"role": "system", "content": str(system or build_system_prompt())},
        {"role": "user", "content": content},
    ]


def _motor_errors() -> Any:
    try:
        import motor as motor_mod  # type: ignore
        return motor_mod
    except Exception:  # noqa: BLE001
        return None


class G2PClient:
    """game2player-4b 的 OpenAI 兼容客户端，接口与 GamescalingClient 对齐。"""

    def __init__(self, endpoint_url: Optional[str] = None, model: Optional[str] = None,
                 timeout: float = 60.0, thinking: bool = False,
                 history_actions: int = 20) -> None:
        _relay = relay_endpoint()
        #: 直连 vLLM 的地址（不走中继）。中继挂了就退回它，保证游戏还能继续玩。
        self.direct_endpoint = str(endpoint_url or os.environ.get(URL_ENV) or endpoint()).rstrip("/")
        self.endpoint = str(endpoint_url or _relay or self.direct_endpoint).rstrip("/")
        self.model = str(model or os.environ.get(MODEL_ENV) or MODEL)
        self.timeout = float(timeout)
        self.thinking = bool(thinking)
        self.max_tokens = int(os.environ.get("BETTERAGI_G2P_MAX_TOKENS", "2048" if self.thinking else "512"))
        self.history_actions = int(history_actions)
        self.history_frames = int(
            os.environ.get(HISTORY_FRAMES_ENV, str(DEFAULT_HISTORY_FRAMES)))
        self.history: List[Dict[str, Any]] = []
        self.notes: List[str] = []
        self.reset_count = 0
        self.last_text = ""
        self.last_reasoning = ""
        self.last_latency_ms = 0.0
        self.last_action: Dict[str, Any] = {}
        #: 最近一帧的压缩结果（ok/bytes/quality/ratio），排查上传体积时看它
        self.last_frame_meta: Dict[str, Any] = {}
        self.frame_compress_count = 0
        #: 复用同一个 httpx.Client：决策之间不再重新握手（见 KEEPALIVE_* 注释）。
        self._client: Any = None
        #: 帧间差分：sid 固定这一个客户端实例，中继按 sid 存历史帧
        self.sid = DELTA_SID_PREFIX + uuid.uuid4().hex[:12]
        self.delta = delta_enabled()
        self.delta_chain = _frame_delta.DeltaChain() if (self.delta and _frame_delta is not None) else None
        self.last_delta_meta: Dict[str, Any] = {}
        self.delta_fallbacks = 0
        #: 与本实例发送链对齐的序号（中继按它校验参考帧是否是同一链的上一帧）
        self.delta_seq = 0
        self._delta_seq_sent = 0

    def _http(self, timeout: float) -> Any:
        """取（必要时新建）共享的 httpx.Client；timeout 由调用方按请求传。"""
        import httpx
        if self._client is None:
            # trust_env=False 很关键：Windows 系统代理（verge-mihomo 127.0.0.1:7897）会被
            # httpx 默认读进来，所有请求都绕一层本地代理，而那个代理会给每个响应塞
            # `Connection: close`，keep-alive 全废、每步重新握手（实测 ~190ms/步 vs 144ms）。
            self._client = httpx.Client(
                trust_env=False,
                limits=httpx.Limits(
                    max_keepalive_connections=KEEPALIVE_CONNECTIONS,
                    max_connections=KEEPALIVE_CONNECTIONS * 2,
                    keepalive_expiry=KEEPALIVE_EXPIRY_S,
                ),
            )
        return self._client

    def _fallback_direct(self, reason: str) -> bool:
        """中继不可用（连不上 / 5xx）时退回直连 vLLM：宁可多发整帧，也不能停摆。"""
        if self.endpoint == self.direct_endpoint and not self.delta:
            return False
        if self.endpoint != self.direct_endpoint:
            self.endpoint = self.direct_endpoint
        self.delta = False
        self.delta_fallbacks += 1
        self.last_delta_meta = {"ok": False, "fallback": reason}
        if self.delta_chain is not None:
            self.delta_chain.reset()
        self.delta_seq = 0
        return True

    def _base_payload(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "model": self.model,
            "messages": messages,
            "temperature": 1.0,
            "max_tokens": self.max_tokens,
            "chat_template_kwargs": {"enable_thinking": bool(self.thinking)},
        }

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 收尾失败不该影响已经拿到的结果
                pass

    # ---- 帧压缩 -----------------------------------------------------
    def small_frame(self, frame_url: str) -> str:
        """把一帧 data URL 重编码得更小（默认 webp q65，失败就原样上传）。

        实测 640x480 的 Undertale 帧：jpeg q85 4:4:4 约 24KB -> webp q65 约 9KB，
        一帧省 ~15KB（base64 后 20KB），每步带 2 帧就是 40KB，按 ~1.2MB/s 上行算省 30ms 左右。
        """
        url = str(frame_url or "")
        if _frame_codec is None or not url.startswith("data:image/"):
            return url
        try:
            out, meta = _frame_codec.compress_data_url(url)
        except Exception as error:  # noqa: BLE001 - 压坏了不该影响这一轮决策
            self.last_frame_meta = {"ok": False, "error": str(error)[:160]}
            return url
        self.last_frame_meta = meta
        if meta.get("ok") and not meta.get("kept_original"):
            self.frame_compress_count += 1
        return out

    # ---- 历史 -------------------------------------------------------
    def reset(self) -> None:
        self.history = []
        self.notes = []
        self.reset_count += 1
        # 一局结束 policy.reset()：差分链也从头开始，下一帧必是 keyframe
        self.delta_seq = 0
        if self.delta_chain is not None:
            self.delta_chain.reset()

    def record(self, frame_url: str, action_text: str, *, pressed: Optional[bool] = None,
               step: Optional[int] = None, skipped_reason: str = "") -> None:
        note = str(action_text or "").strip()
        if skipped_reason:
            note = (note + " (skipped: %s)" % skipped_reason).strip()
        elif pressed is False:
            note = (note + " (not delivered)").strip()
        if note:
            self.notes.append(note)
        if str(frame_url or "").startswith("data:image/"):
            self.history.append({"frame_url": self.small_frame(str(frame_url))})
        if self.history_actions > 0:
            self.history = self.history[-self.history_actions:]
            self.notes = self.notes[-MAX_HISTORY_ACTIONS:]

    # ---- 推理 -------------------------------------------------------
    def decide(self, frame_url: str, instruction: str = "", game: str = "",
               system_prompt: Optional[str] = None, *, deadline_ms: Optional[float] = None,
               new_intent: bool = False) -> Dict[str, Any]:
        if new_intent:
            self.reset()
        system = system_prompt or build_system_prompt(game or "")
        raw_url = str(frame_url or "")
        delta_refs: Optional[Dict[str, str]] = None
        if self.delta and self.delta_chain is not None and raw_url.startswith("data:image/"):
            try:
                raw_bytes = base64.b64decode(raw_url.split(",", 1)[1])
                self._last_frame_bytes = raw_bytes
                patch, meta = self.delta_chain.feed(raw_bytes)
                self.last_delta_meta = meta
                # 中继靠 seq 判断「它手里的参考帧是不是我这条链的上一帧」：
                # keyframe（整帧）发 0 让中继清空重来，之后的帧必须是上一帧的 seq+1。
                self._delta_seq_sent = 0 if meta.get("mode") == "full" else self.delta_seq
                delta_refs = {"cur": _frame_delta.to_b64(patch)}
            except Exception as error:  # noqa: BLE001 - 差分编不出来就退回整帧，不能让链路断
                self.last_delta_meta = {"ok": False, "error": str(error)[:160]}
                self.delta_chain.reset()
                delta_refs = None
        if delta_refs is None:
            frame_url = self.small_frame(raw_url)
            messages = build_messages(system, frame_url, history=self.history, notes=self.notes,
                                      max_frames=self.history_frames)
        else:
            messages = build_messages(system, "", history=self.history, notes=self.notes,
                                      max_frames=self.history_frames, frame_ref=True)
        payload = self._base_payload(messages)
        if delta_refs is not None:
            payload["frame_delta"] = {"sid": self.sid, "seq": self._delta_seq_sent,
                                      "refs": delta_refs}
        started = time.monotonic()
        motor_mod = _motor_errors()
        timeout = None
        if deadline_ms:
            timeout = max(1.0, float(deadline_ms) / 1000.0 + 3.0)
        timeout = timeout or self.timeout
        def _rebuild_full() -> None:
            """退回直连时把请求改成两张整帧：中继不在了，frame:// 与 delta:// 没人认。"""
            nonlocal payload, messages, delta_refs
            messages = build_messages(system, self.small_frame(raw_url), history=self.history,
                                      notes=self.notes, max_frames=self.history_frames)
            payload = self._base_payload(messages)
            delta_refs = None

        resp = None
        for attempt in (0, 1):
            try:
                resp = self._http(timeout).post(
                    self.endpoint + "/v1/chat/completions", json=payload,
                    headers={"Content-Type": "application/json"}, timeout=timeout)
            except Exception as error:  # noqa: BLE001 - 网络问题按超时归类，让执行器去重试
                if attempt == 0 and self._fallback_direct("connect_error"):
                    _rebuild_full()
                    continue
                elapsed = (time.monotonic() - started) * 1000.0
                if motor_mod is not None and hasattr(motor_mod, "MotorTimeout"):
                    raise motor_mod.MotorTimeout(
                        "game2player 请求失败：%s: %s" % (type(error).__name__, error),
                        elapsed_ms=elapsed, deadline_ms=float(deadline_ms or 0), frame_url=frame_url)
                raise
            if (resp.status_code >= 500 and attempt == 0
                    and self._fallback_direct("relay_%d" % resp.status_code)):
                _rebuild_full()
                continue
            break
        if resp.status_code == 409 and delta_refs is not None:
            # 中继没有参考帧（重启 / 换 sid / 历史被淘汰）：整帧重发一次
            self.delta_fallbacks += 1
            self.delta_chain.reset()
            self.last_delta_meta = {"ok": False, "fallback": "relay_need_full"}
            try:
                patch, meta = self.delta_chain.feed(self._last_frame_bytes)
                self.last_delta_meta = dict(meta, fallback="relay_need_full")
                self._delta_seq_sent = 0 if meta.get("mode") == "full" else self.delta_seq
                payload["frame_delta"] = {"sid": self.sid, "seq": self._delta_seq_sent,
                                          "refs": {"cur": _frame_delta.to_b64(patch)}}
                resp = self._http(timeout).post(
                    self.endpoint + "/v1/chat/completions", json=payload,
                    headers={"Content-Type": "application/json"}, timeout=timeout)
            except Exception as error:  # noqa: BLE001 - 兜底也失败就按网络错误往上抛
                elapsed = (time.monotonic() - started) * 1000.0
                if motor_mod is not None and hasattr(motor_mod, "MotorTimeout"):
                    raise motor_mod.MotorTimeout(
                        "game2player 中继兜底失败：%s: %s" % (type(error).__name__, error),
                        elapsed_ms=elapsed, deadline_ms=float(deadline_ms or 0), frame_url=raw_url)
                raise
        elapsed = (time.monotonic() - started) * 1000.0
        self.last_latency_ms = elapsed
        if resp.status_code < 400 and delta_refs is not None:
            self.delta_seq = int(getattr(self, "_delta_seq_sent", 0)) + 1
        if resp.status_code >= 400:
            if motor_mod is not None and hasattr(motor_mod, "MotorParseError"):
                raise motor_mod.MotorParseError(
                    "game2player 返回 %d" % resp.status_code, raw_text=resp.text[:500],
                    cleaned="", http_status=resp.status_code, frame_url=frame_url,
                    reason="http_error")
            raise RuntimeError("game2player 返回 %d" % resp.status_code)
        try:
            body = resp.json()
            message = (body.get("choices") or [{}])[0].get("message") or {}
        except Exception as error:  # noqa: BLE001
            raise RuntimeError("game2player 响应不是合法 JSON：%s" % error)
        text = str(message.get("content") or "")
        reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "")
        self.last_text = text
        self.last_reasoning = reasoning
        try:
            call = parse_tool_call(text)
            decoded = action_to_steps(call.get("arguments") or {})
        except Exception as error:  # noqa: BLE001 - 解析失败必须显式失败，不许伪造空动作
            if motor_mod is not None and hasattr(motor_mod, "MotorParseError"):
                raise motor_mod.MotorParseError(
                    "game2player 输出不可解析：%s" % error, raw_text=text, cleaned=text,
                    http_status=resp.status_code, frame_url=frame_url, reason="unparsed")
            raise
        decoded["raw"] = text
        decoded["reasoning"] = reasoning
        decoded["name"] = call.get("name") or ACTION_NAME
        decoded["history_text"] = history_note(decoded)
        decoded["latency_ms"] = elapsed
        if deadline_ms and elapsed > float(deadline_ms):
            decoded["slow"] = True
        self.last_action = decoded
        return decoded

    # ---- 运维 -------------------------------------------------------
    def health(self, timeout: float = 5.0, *, probe_ready: bool = True) -> Dict[str, Any]:
        out: Dict[str, Any] = {"endpoint": self.endpoint, "model": self.model}
        try:
            resp = self._http(timeout).get(self.endpoint + "/v1/models", timeout=timeout)
            out["status"] = resp.status_code
            out["available"] = resp.status_code < 400
            if out["available"]:
                ids = [str(item.get("id", "")) for item in (resp.json().get("data") or [])
                       if isinstance(item, dict)]
                out["model_ids"] = ids
                out["model_ok"] = self.model in ids if ids else None
        except Exception as error:  # noqa: BLE001
            out["available"] = False
            out["error"] = "%s: %s" % (type(error).__name__, error)
        return out


def build_motor(**kwargs: Any) -> G2PClient:
    return G2PClient(**kwargs)
