"""Qwen-Omni 快系统客户端（FR-6 / M3）。

官方文档核实结论（见 Docs/research/omni-realtime-protocol.md）：

* 传输是**私有 WebSocket 实时事件协议**，不是 OpenAI 兼容接口，也不是 SSE。
* 视觉输入走 input_image_buffer.append，image 字段是 **Base64 原始 JPEG**；
  **发图前必须先发过至少一次** input_audio_buffer.append，否则图像缓冲不成立。
* 要纯文本回答必须显式 modalities:["text"]，否则拿到的是 TTS 转写。
* 没有 JSON 模式 → 结构化指令只能走 Function Calling（function_call_arguments.done）。

因此本模块负责：拼 URL / 管事件帧 / 维护「先音频后图像」的顺序约束 / 给每一包等
事件设 deadline / 把一轮响应收成 OmniReply。等服务端超时或传输损坏时，客户端提供
reconnect() 换一条干净会话（何时重连仍由上层调度决定）；客户端保持可测、可替换：
真实 WebSocket 通过 WsTransport 注入，测试用 ScriptedTransport。
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple


#: 2026-11-06 用户要求把 omni 换成 MiniCPM-o-4.5-Realtime（chat 模式），这里改成默认，
#: Qwen-Omni 的实现原样保留——设 OMNI_PROVIDER=dashscope（或 base_url 指回 dashscope）
#: 就退回去，omni_minicpm.py 提供同一套调用面。
DEFAULT_MODEL = "MiniCPM-o-4.5-Realtime"
#: MiniCPM-o chat 模式入口（官方文档：wss://api.modelbest.cn/v1/realtime?mode=chat&model=...）。
MINICPM_URL = "wss://api.modelbest.cn/v1/realtime?mode=chat&model=MiniCPM-o-4.5-Realtime"
#: 官方公共入口（无 workspace 子域、无 model 查询串；模型在 session.update 里报）。
DEFAULT_URL = "wss://cn-beijing.maas.qianwenaiapi.com/api-ws/v1/realtime"
#: 官方给的图像上限：Base64 之前 ≤256KB。
MAX_IMAGE_BYTES = 256 * 1024
#: 分辨率推荐上限（单图 4~1280 token 对应区间）。
RECOMMENDED_MAX_SIDE = 1080
#: 垫在图像前面的最小静音块（16bit 单声道 16kHz，20ms）。
SILENT_PCM_20MS = bytes(640)

#: 快系统唯一的工具：「我自己看清楚了，必须按一下」。用户口径（2026-11-04）：不操作就是
#: 不操作——执行器绝不替模型按键；只有 realtime 自己判断非按不可时才发起这个调用，
#: tool_choice=auto 交给它决定用不用。
PRESS_KEY_TOOL = {
    "type": "function",
    "name": "press_key",
    "description": (
        "Press one or more keyboard keys once, right now. Only use it when you are "
        "certain the game cannot move forward without a keypress (for example a "
        "dialogue box that waits for confirm) and you want to press it yourself "
        "instead of leaving it to the executor. Do not use it when doing nothing "
        "is fine: no action is the normal case."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "1-6 key tokens to press in order, in the same vocabulary as "
                    "Common Keys (Z, X, Enter, Up, Down, Left, Right, Esc, C)."
                ),
            },
            "reason": {
                "type": "string",
                "description": "One short sentence: why this press is required now.",
            },
        },
        "required": ["keys"],
        "additionalProperties": False,
    },
}


class OmniError_(RuntimeError):
    """快系统协议层错误（配置缺失、帧非法、事件不是预期类型）。"""


class OmniTimeout(OmniError_):
    """等服务端事件超过预算（deadline 到点，服务端根本没回包）。

    继承 OmniError_：老代码里的 ``except OmniError_`` 仍然兜得住，不会因为
    新增超时类型就漏判；需要区分「服务端明确报错」和「一直不回」时再单独
    ``except OmniTimeout``。
    """

    def __init__(self, message: str, *, waited_s: float = 0.0,
                 budget_s: Optional[float] = None, event: str = "", phase: str = ""):
        super().__init__(message)
        #: 实际等了多久（秒）。
        self.waited_s = waited_s
        #: 当时的预算（秒）；None 表示不限。
        self.budget_s = budget_s
        #: 超时时正在等的事件（首个事件 / response.done / ask 总预算）。
        self.event = event
        #: 超时发生的阶段（首包 / 后续步 / ask）。
        self.phase = phase


# ---------------------------------------------------------------- 配置
@dataclass
class OmniConfig:
    """连接参数。api_key 允许为空，此时从环境变量取。"""

    api_key: str = ""
    workspace_id: str = ""
    model: str = DEFAULT_MODEL
    base_url: str = ""
    modalities: tuple = ("text",)
    instructions: str = ""
    representation_compact: str = "normal"
    #: 声明的工具（function calling）。默认只开一个「要一次按键」：快系统自己看画面，
    #: 只有在它判断非按不可时才发起（tool_choice=auto，由模型决定用不用）。
    tools: tuple = (PRESS_KEY_TOOL,)
    tool_choice: str = "auto"
    #: collect 首包（第一个事件 = 首 token）预算，秒。<=0 表示不限。
    #: 默认取设计文档 04_acceptance 的「整轮 12s」，避免服务端不回包时永久挂住。
    first_token_timeout_s: float = 12.0
    #: collect 首包之后每一包的步进预算，秒。<=0 表示不限。
    #: 默认取 02_schemas「Omni 单步 token 5000ms」与 04_acceptance「单帧 4s」中的 4s。
    step_timeout_s: float = 4.0

    def resolve_key(self) -> str:
        key = self.api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        if not key:
            # 环境变量没有就退回写盘凭据（DSH credentials / keys/dashscope.key），
            # 否则 TUI 一起跑就报"快系统未启用"，双系统直接退化成单腿。
            try:
                from fast_system import resolve_dashscope_key

                key = resolve_dashscope_key()
            except Exception:  # noqa: BLE001 - 解析器不可用就按缺 key 处理
                key = ""
        if not key:
            raise OmniError_("缺少 DASHSCOPE_API_KEY：快系统无法鉴权")
        return key

    def is_minicpm(self) -> bool:
        """当前配置是不是 MiniCPM-o（chat 模式 WS）。

        判定顺序：OMNI_PROVIDER 显式指定 > base_url 域名 > 模型名。
        默认（无环境变量）按模型名走：DEFAULT_MODEL 已经是 MiniCPM-o。
        """

        env = (os.environ.get("OMNI_PROVIDER") or "").strip().lower()
        if env in ("minicpm", "minicpm-o", "modelbest"):
            return True
        if env in ("dashscope", "qwen", "qwen-omni"):
            return False
        if self.base_url and "modelbest" in self.base_url:
            return True
        return self.model.lower().startswith("minicpm")

    def resolve_url(self) -> str:
        """按官方示例拼端点。

        官方（qianwenai.com 模型页 API 参考）给的是**公共入口**：
            wss://cn-beijing.maas.qianwenaiapi.com/api-ws/v1/realtime
        没有 workspace 子域、**没有 ?model= 查询串**——模型是在 session.update 里报的。
        早先我拼成 wss://{workspace}.cn-beijing.maas.aliyuncs.com/...?model=...，
        这是 workspace 专属入口的形态，key 不带 workspace 权限时必然 401。
        因此默认走官方公共入口；显式给了 workspace_id 才走专属入口。
        """
        if self.is_minicpm():
            # MiniCPM-o：chat 模式的 model 必须写在查询串里，没有 workspace 概念。
            base = self.base_url or MINICPM_URL
            if "model=" in base:
                return base
            sep = "&" if "?" in base else "?"
            return base + sep + "model=" + self.model
        if self.base_url:
            base = self.base_url
        else:
            ws = self.workspace_id or os.environ.get("DASHSCOPE_WORKSPACE_ID", "")
            base = (
                "wss://" + ws + ".cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"
                if ws
                else DEFAULT_URL
            )
        # 实测（2026-11-04）：公共入口不带 model 查询串会被服务端拒绝——
        # 1009 Required parameter "model" missing from request. 所以 URL 和 session.update 都要带。
        if "model=" in base:
            return base
        sep = "&" if "?" in base else "?"
        return base + sep + "model=" + self.model

    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": "Bearer " + self.resolve_key(),
            "Content-Type": "application/json",
        }

    def session_update(self) -> Dict[str, Any]:
        """首帧 session.update：把输出锁成纯文本，并带上系统指令。"""
        body: Dict[str, Any] = {
            # 官方示例：模型名在 session.update 里报，不在 URL 上。
            "model": self.model,
            "modalities": list(self.modalities),
            "instructions": self.instructions,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "turn_detection": None,
        }
        if self.representation_compact:
            body["representation_compact"] = self.representation_compact
        if self.tools:
            body["tools"] = list(self.tools)
            body["tool_choice"] = self.tool_choice
        return {"type": "session.update", "session": body}


# ---------------------------------------------------------------- 传输层
class Transport(Protocol):
    """最小传输协议：能发/收一条 JSON 事件即可，方便替换与伪造。"""

    async def send_json(self, payload: Dict[str, Any]) -> None: ...

    async def recv_json(self) -> Dict[str, Any]: ...

    async def close(self) -> None: ...


class ScriptedTransport:
    """测试用：按脚本吐出服务端事件，并记录所有发出的事件。"""

    def __init__(self, script: Iterable[Dict[str, Any]] = ()):
        self._script: List[Dict[str, Any]] = list(script)
        self.sent: List[Dict[str, Any]] = []
        self.closed = False

    async def send_json(self, payload: Dict[str, Any]) -> None:
        self.sent.append(payload)

    async def recv_json(self) -> Dict[str, Any]:
        if not self._script:
            raise OmniError_("脚本耗尽：没有更多服务端事件")
        return self._script.pop(0)

    async def close(self) -> None:
        self.closed = True

    def types(self) -> List[str]:
        return [e.get("type", "") for e in self.sent]


class WsTransport:
    """真实 WebSocket 传输。websockets 只在真正连接时才 import。"""

    def __init__(self, config: OmniConfig):
        self._config = config
        self._ws = None

    async def connect(self) -> "WsTransport":
        try:
            import websockets as _ws
        except ImportError as exc:  # pragma: no cover - 仅在缺依赖时触发
            raise OmniError_("需要 websockets 依赖：uv run --with websockets ...") from exc

        self._ws = await _ws.connect(
            self._config.resolve_url(),
            additional_headers=self._config.headers(),
            max_size=None,
        )
        return self

    async def send_json(self, payload: Dict[str, Any]) -> None:
        if self._ws is None:  # pragma: no cover - 防御性
            raise OmniError_("传输未连接")
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def recv_json(self) -> Dict[str, Any]:
        if self._ws is None:  # pragma: no cover - 防御性
            raise OmniError_("传输未连接")
        raw = await self._ws.recv()
        return json.loads(raw)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None


# ---------------------------------------------------------------- 回复
@dataclass
class OmniReply:
    """一轮快系统响应：文本 + 结构化调用 + 计时。"""

    text: str = ""
    function_calls: List[Dict[str, Any]] = field(default_factory=list)
    latency_ms: float = 0.0
    raw_events: int = 0
    truncated: bool = False

    def first_call(self, name: str = "") -> Optional[Dict[str, Any]]:
        for call in self.function_calls:
            if not name or call.get("name") == name:
                return call
        return None


# ---------------------------------------------------------------- 帧编码
#: 降质重编的质量档位（按顺序尝试，先不改变分辨率）。
REENCODE_QUALITIES = (85, 70, 55, 40)
#: 降质重编的宽度比例档位（1.0 表示原尺寸；之后每档再把质量走一遍）。
REENCODE_SCALES = (1.0, 0.85, 0.7, 0.55)


def encode_jpeg_frame_ex(data: bytes, *, max_bytes: int = MAX_IMAGE_BYTES,
                         allow_reencode: bool = True) -> Tuple[str, Dict[str, Any]]:
    """把 JPEG 字节编码成协议要的 Base64，返回 (b64, meta)。

    先做魔数校验（非 JPEG 一律抛，不猜）；不超限就原样 Base64，绝不动原图。
    超限且 allow_reencode 时用 Pillow 逐级降：先质量 85→70→55→40，再宽度
    0.85→0.7→0.55（每档尺寸再走一遍质量档），每轮编码后立刻查体积，第一个
    落进上限的就返回。meta 里带 reencoded / 最终质量 / 最终比例 / 尝试历史，
    方便上层日志复盘「这一帧被削了多少」。

    Pillow 不可用时保持旧行为：抛 OmniError_ 并写明没有 Pillow、无法降质重编
    （绝不静默丢帧或假装成功）。降到最小档仍超限同样抛 OmniError_，消息里
    带原始字节、最小可达字节和尝试过的全部档位。
    """

    if len(data) < 3 or data[0:3] != b"\xff\xd8\xff":
        raise OmniError_("快系统只收 JPEG：帧头不是 FFD8FF（PNG/raw 请先转码）")
    if len(data) <= max_bytes:
        return base64.b64encode(data).decode("ascii"), {
            "reencoded": False,
            "bytes": len(data),
            "original_bytes": len(data),
            "max_bytes": max_bytes,
            "quality": None,
            "scale": 1.0,
            "attempts": [],
        }
    if not allow_reencode:
        raise OmniError_("帧体积过大：%d B > 上限 %d B，请先降分辨率/质量" % (len(data), max_bytes))

    try:
        from PIL import Image  # noqa: WPS433 - Pillow 是可选依赖，只在这里 import
    except ImportError as exc:
        raise OmniError_(
            "帧体积过大：%d B > 上限 %d B；没有 Pillow，无法降质重编"
            "（请先自行降分辨率/质量，或安装 pillow）" % (len(data), max_bytes)
        ) from exc

    import io

    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            base_image = opened.convert("RGB")
    except Exception as exc:  # noqa: BLE001 - 解不开就没法重编，必须显式报错
        raise OmniError_(
            "帧体积过大：%d B > 上限 %d B，且 Pillow 无法解码这一帧，无法降质重编：%r"
            % (len(data), max_bytes, exc)
        ) from exc

    original_w, original_h = base_image.size
    attempts: List[Dict[str, Any]] = []
    for scale in REENCODE_SCALES:
        width = max(1, int(round(original_w * scale)))
        height = max(1, int(round(original_h * scale)))
        if (width, height) == (original_w, original_h):
            candidate = base_image
        else:
            candidate = base_image.resize((width, height), Image.LANCZOS)
        for quality in REENCODE_QUALITIES:
            buf = io.BytesIO()
            candidate.save(buf, format="JPEG", quality=quality, optimize=True)
            blob = buf.getvalue()
            attempts.append({
                "quality": quality,
                "scale": scale,
                "width": width,
                "height": height,
                "bytes": len(blob),
            })
            if len(blob) <= max_bytes:
                return base64.b64encode(blob).decode("ascii"), {
                    "reencoded": True,
                    "bytes": len(blob),
                    "original_bytes": len(data),
                    "max_bytes": max_bytes,
                    "quality": quality,
                    "scale": scale,
                    "width": width,
                    "height": height,
                    "attempts": attempts,
                }

    smallest = min(attempts, key=lambda item: item["bytes"])
    tiers = "、".join(
        "q%d/%d%%@%dx%d=%dB"
        % (a["quality"], int(round(a["scale"] * 100)), a["width"], a["height"], a["bytes"])
        for a in attempts
    )
    raise OmniError_(
        "帧体积过大：原始 %d B > 上限 %d B；已尝试 %d 档降质重编（%s），"
        "最小仍为 %d B（q%d，%dx%d），请先降分辨率"
        % (
            len(data), max_bytes, len(attempts), tiers,
            smallest["bytes"], smallest["quality"], smallest["width"], smallest["height"],
        )
    )


def encode_jpeg_frame(data: bytes, *, max_bytes: int = MAX_IMAGE_BYTES,
                      allow_reencode: bool = True) -> str:
    """兼容旧调用：只要 Base64；降质逻辑与元信息在 encode_jpeg_frame_ex。"""

    b64, _meta = encode_jpeg_frame_ex(
        data, max_bytes=max_bytes, allow_reencode=allow_reencode
    )
    return b64


def _loads_or_raw(value: Any) -> Any:
    if isinstance(value, str) and value.strip()[:1] in ("{", "["):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


# ---------------------------------------------------------------- 客户端
class OmniClient:
    """一轮视觉问答 = 垫静音 → 图 → commit → create_response → 收事件。

    严格保持官方顺序约束：第一条 input_audio_buffer.append 必须在任何图像之前。
    """

    def __init__(self, config: Optional[OmniConfig] = None,
                 transport: Optional[Transport] = None,
                 *, transport_factory: Optional[Callable[[], Any]] = None):
        self.config = config or OmniConfig()
        self._transport = transport
        #: 重建连接用：超时后连接判死（broken），下一次 ask 会先 reconnect 换一条
        #: 干净会话。注入 transport 的测试/调用方想支持自动重连就传这个工厂。
        self._transport_factory = transport_factory
        #: transport 是不是本客户端自己建的（决定没有工厂时能不能 reconnect）。
        self._owns_transport = transport is None
        self._audio_seeded = False
        #: 上一轮 response.create 之后有没有等到 response.done。
        #: 中途出错（服务端 error 事件、我们的 collect 提前抛）会把它留成 True，
        #: 下一轮 create_response 就会撞 "Conversation already has an active response"。
        self._inflight = False
        #: 通道是否已判死（超时 / 关闭）。broken 时下一次 ask 先 reconnect，
        #: 没有可重建的传输就抛明确错误——绝不允许静默返回空文本。
        self._broken = False
        self._broken_reason = ""
        self._closed = False

    @property
    def broken(self) -> bool:
        """通道是否不可直接复用（超时判死或已关闭）。

        broken 时下一次 ask 会先自动 reconnect；已关闭（close）的客户端必须
        显式 await reconnect()，不会偷偷复活。
        """

        return self._broken or self._closed

    @property
    def broken_reason(self) -> str:
        """broken 的原因，便于日志/复盘定位是哪一轮超时。"""

        if self._broken_reason:
            return self._broken_reason
        return "客户端已关闭" if self._closed else ""

    async def connect(self) -> "OmniClient":
        if self._transport is None:
            self._transport = await WsTransport(self.config).connect()
            self._owns_transport = True
        await self._send(self.config.session_update())
        self._inflight = False
        self._broken = False
        self._broken_reason = ""
        self._closed = False
        return self

    async def close(self) -> None:
        # 关掉之后不再复用：ask 会抛「已关闭」，要恢复必须显式 reconnect。
        self._closed = True
        if self._transport is not None:
            await self._transport.close()

    async def reconnect(self) -> "OmniClient":
        """关掉旧连接、重建一条干净会话（超时后的恢复路径）。

        为什么必须换连接而不是只 drain：deadline 到点时服务端完全可能稍后才补发
        上一轮的 response.done；只 drain 只能读到「此刻已经到」的事件，那条迟到
        事件会被下一轮 collect 当成本轮答案（串台 / 哨兵事件泄漏）。把旧连接关掉，
        旧会话的事件就投递不到任何地方了（04_acceptance「脏连接不复用」）。

        没有可重建的传输（注入 transport 且没给 transport_factory）时抛 OmniError_，
        不假装还能用。会话更新帧发送失败也算重连失败，抛错。
        """

        old = self._transport
        if old is not None:
            try:
                await old.close()
            except Exception as exc:  # noqa: BLE001 - 旧连接关不掉也得换新的
                self._broken_reason = "关闭旧连接失败：%r" % (exc,)
        self._transport = None
        self._inflight = False
        self._audio_seeded = False
        try:
            new = self._make_transport()
            if inspect.isawaitable(new):
                new = await new
        except OmniError_ as exc:
            self._broken = True
            self._broken_reason = str(exc)
            raise
        except Exception as exc:  # noqa: BLE001 - 建链失败必须显式抛
            self._broken = True
            self._broken_reason = "reconnect 失败：%r" % (exc,)
            raise OmniError_("快系统重连失败：%r" % (exc,)) from exc
        if new is None:
            self._broken = True
            self._broken_reason = "transport_factory 返回 None"
            raise OmniError_("快系统重连失败：transport_factory 返回 None")
        self._transport = new
        self._closed = False
        self._broken = False
        self._broken_reason = ""
        try:
            await self._send(self.config.session_update())
        except Exception as exc:  # noqa: BLE001 - 连上了但会话初始化发不出去，同样不可用
            self._broken = True
            self._broken_reason = "reconnect 后 session.update 发送失败：%r" % (exc,)
            raise OmniError_("快系统重连失败（session.update 发不出去）：%r" % (exc,)) from exc
        return self

    def _make_transport(self) -> Any:
        """按注入优先级造一条新传输：工厂 > 自己建的 WsTransport > 明确报错。"""

        if self._transport_factory is not None:
            return self._transport_factory()
        if self._owns_transport:
            return WsTransport(self.config).connect()
        raise OmniError_(
            "这家客户端用注入的 transport 且没有 transport_factory：无法 reconnect，"
            "请重建 OmniClient 或传入 transport_factory"
        )

    async def _ensure_usable(self) -> None:
        """收发前的门禁：已关闭/未连接就抛明确错误；broken 就先换一条干净连接。"""

        if self._closed:
            raise OmniError_("客户端已关闭：先 await reconnect() 再继续")
        if self._broken:
            await self.reconnect()
        if self._transport is None:
            raise OmniError_("客户端未连接：先 await connect() 或注入 transport")

    @staticmethod
    def _resolve_budget(default: Any, override: Any, name: str) -> Optional[float]:
        """取「单次调用覆盖 > 配置默认」，<=0 或 None 表示不限（保持旧行为）。"""

        value = default if override is None else override
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise OmniError_("%s 必须是数字秒：%r" % (name, value))
        value = float(value)
        if value != value:  # NaN
            raise OmniError_("%s 不能是 NaN" % name)
        return value if value > 0 else None

    @staticmethod
    def _positive_budget(value: Any, name: str) -> float:
        """整轮预算必须明确为正数；None 由调用方另行处理。"""

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise OmniError_("%s 必须是数字秒：%r" % (name, value))
        value = float(value)
        if value != value or value <= 0:
            raise OmniError_("%s 必须 > 0：%r" % (name, value))
        return value

    async def __aenter__(self) -> "OmniClient":
        return await self.connect()

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def _send(self, payload: Dict[str, Any]) -> None:
        if self._transport is None:
            raise OmniError_("客户端未连接：先 await connect() 或注入 transport")
        await self._transport.send_json(payload)

    async def seed_audio(self, pcm: bytes = SILENT_PCM_20MS) -> None:
        """发送音频垫片。官方硬约束：图像之前至少要有一条音频 append。"""
        await self._send({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode("ascii"),
        })
        self._audio_seeded = True

    async def send_frame(self, jpeg: bytes) -> None:
        """送一帧画面。未垫音频会先自动垫一块静音，避免踩协议坑。"""
        if not self._audio_seeded:
            await self.seed_audio()
        await self._send({"type": "input_image_buffer.append", "image": encode_jpeg_frame(jpeg)})

    async def commit(self) -> None:
        await self._send({"type": "input_audio_buffer.commit"})

    async def create_response(self, instructions: str = "") -> None:
        body: Dict[str, Any] = {"modalities": list(self.config.modalities)}
        if instructions:
            body["instructions"] = instructions
        self._inflight = True
        await self._send({"type": "response.create", "response": body})

    async def _drain_events(self, budget_s: float) -> Tuple[int, bool]:
        """读到 response.done / cancelled / failed 或用完预算；返回 (事件数, 是否读到终点)。

        收尾专用，所以任何异常都吞掉——这时候再抛只会盖住原本的错误。
        """

        loop = asyncio.get_event_loop()
        deadline = loop.time() + max(0.0, float(budget_s))
        seen = 0
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return seen, False
            try:
                event = await asyncio.wait_for(self._transport.recv_json(), timeout=remaining)
            except Exception:  # noqa: BLE001 - 收尾失败不该再抛
                return seen, False
            seen += 1
            etype = (event or {}).get("type", "")
            if etype in ("response.done", "response.cancelled", "response.failed"):
                return seen, True

    async def _drain_until_done(self, budget_s: float = 8.0) -> int:
        """把还在路上的事件读干净，直到 response.done / cancelled 或用完预算。

        不读完就会留下来被**下一轮** collect 当成自己的答案（错位一轮），
        而且服务端那边这一轮仍算 active，下一轮 create_response 直接报错。
        预算用完就放弃——宁可丢一轮，也不许把主流程堵死。
        """

        seen, _terminal = await self._drain_events(budget_s)
        return seen

    async def _quarantine(self, reason: str, *, close_budget_s: float = 1.0) -> None:
        """超时后把这条连接判死：尽力 cancel + close，标 broken，等下次 reconnect。

        只 drain 不关连接是不够的（见 reconnect 的说明）：迟到的 response.done
        必须彻底投递不到客户端。这里顺手清 _inflight，避免下一轮 create_response
        撞「already has an active response」。
        """

        transport = self._transport
        self._inflight = False
        self._broken = True
        self._broken_reason = reason
        if transport is None:
            return
        try:
            await asyncio.wait_for(
                transport.send_json({"type": "response.cancel"}), timeout=close_budget_s
            )
        except Exception:  # noqa: BLE001 - 通道已经挂了，取消通知发不出去不重要
            pass
        try:
            # 关闭也可能挂住（服务端半死不活），所以同样带预算；关不掉也已经标 broken。
            await asyncio.wait_for(transport.close(), timeout=close_budget_s)
        except Exception:  # noqa: BLE001
            pass

    async def cancel_inflight(self, budget_s: float = 8.0) -> None:
        """撤销一轮没等完的响应，把通道恢复到可以再问的状态。"""

        if not self._inflight:
            return
        try:
            await self._send({"type": "response.cancel"})
        except Exception:  # noqa: BLE001 - 通道都断了就没什么可撤的
            self._inflight = False
            return
        await self._drain_until_done(budget_s)
        self._inflight = False

    async def ask(self, jpeg: bytes, *, instructions: str = "", audio: Optional[bytes] = None,
                  extra_frames: Optional[Sequence[bytes]] = None,
                  max_total_s: Optional[float] = None) -> OmniReply:
        """看一帧、给一句指令、收一轮文本（+ 函数调用）。

        max_total_s: 整轮总预算（秒）。None（默认）保持原有行为——不设总预算，
        由 collect 的首包 / 单步 deadline 兜底。超时抛 OmniTimeout，并且把连接
        判死（broken）：下一次 ask 会先 reconnect 换新会话，绝不返回空文本冒充成功。
        """

        if max_total_s is None:
            return await self._ask_round(
                jpeg, instructions=instructions, audio=audio, extra_frames=extra_frames
            )
        budget = self._positive_budget(max_total_s, "max_total_s")
        loop = asyncio.get_event_loop()
        started = loop.time()
        try:
            return await asyncio.wait_for(
                self._ask_round(
                    jpeg, instructions=instructions, audio=audio, extra_frames=extra_frames
                ),
                timeout=budget,
            )
        except (asyncio.TimeoutError, TimeoutError):
            waited = loop.time() - started
            message = "快系统 ask 总预算超时：整轮已等 %.2fs（预算 %.2fs，停在发送或收包阶段）" % (
                waited, budget)
            await self._quarantine(message)
            raise OmniTimeout(message, waited_s=waited, budget_s=budget,
                              event="response.done", phase="ask 总预算") from None

    async def _ask_round(self, jpeg: bytes, *, instructions: str = "", audio: Optional[bytes] = None,
                         extra_frames: Optional[Sequence[bytes]] = None) -> OmniReply:
        """一轮问答的实际收发（ask 负责可选的总预算与超时收尾）。"""

        # broken（上一轮超时）就先换一条干净连接；换不了会抛明确错误。
        await self._ensure_usable()
        # 上一轮要是没等到 response.done（服务端 error、我们提前抛），
        # 这一轮直接问会撞 "Conversation already has an active response"。
        await self.cancel_inflight()
        # commit 会清空音频缓冲，所以「每轮」都要重新垫一条音频——只在首轮垫过，
        # 第二轮就会撞上 "Error append image before append audio."（实测 2026-11-04）。
        await self._send({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(audio or SILENT_PCM_20MS).decode("ascii"),
        })
        self._audio_seeded = True
        await self.send_frame(jpeg)
        for frame in (extra_frames or ()):
            await self.send_frame(frame)
        await self.commit()
        await self.create_response(instructions)
        return await self.collect()

    async def collect(self, max_events: int = 512, *,
                      first_token_timeout_s: Optional[float] = None,
                      step_timeout_s: Optional[float] = None) -> OmniReply:
        """收到 response.done 为止（不是 text.done）；没有阻塞式 sleep。

        每一包都带 deadline（04_acceptance S4/S10 的「不许永久挂住」）：
        首包用 first_token_timeout_s，之后每一包用 step_timeout_s；默认取
        OmniConfig 里的值，传参可覆盖，<=0 表示不限。
        超时抛 OmniTimeout（继承 OmniError_），并且**把这条连接判死**：尽力
        response.cancel + close，标 broken，_inflight 复位。下一次 ask 自动
        reconnect 换新会话——只 drain 不换连接的话，deadline 到点后服务端稍晚
        补发的上一轮 response.done 会被下一轮 collect 当成本轮答案（串台）。
        """

        await self._ensure_usable()
        transport = self._transport
        assert transport is not None
        first_budget = self._resolve_budget(
            self.config.first_token_timeout_s, first_token_timeout_s, "first_token_timeout_s")
        step_budget = self._resolve_budget(
            self.config.step_timeout_s, step_timeout_s, "step_timeout_s")
        reply = OmniReply()
        args_buffer: Dict[str, str] = {}
        loop = asyncio.get_event_loop()
        started = loop.time()
        while reply.raw_events < max_events:
            if reply.raw_events == 0:
                budget, phase, expected = first_budget, "首包（首 token）", "首个事件"
            else:
                budget, phase, expected = step_budget, "后续步（等待 response.done）", "response.done"
            wait_started = loop.time()
            try:
                if budget is None:
                    event = await transport.recv_json()
                else:
                    event = await asyncio.wait_for(transport.recv_json(), timeout=budget)
            except (asyncio.TimeoutError, TimeoutError):
                waited = loop.time() - wait_started
                message = "快系统收包超时：%s 已等 %.2fs（预算 %s，预期事件 %s）" % (
                    phase, waited, ("%.2fs" % budget) if budget is not None else "不限", expected)
                # 判死这条连接，迟到事件不会被下一轮读到。
                await self._quarantine(message)
                raise OmniTimeout(message, waited_s=waited, budget_s=budget,
                                  event=expected, phase=phase) from None
            reply.raw_events += 1
            etype = event.get("type", "")
            if etype == "response.text.delta":
                reply.text += event.get("delta", "")
            elif etype == "response.text.done":
                if not reply.text:
                    reply.text = event.get("text", "")
                # 不能在这里 break：text.done 之后还有 response.done / rate_limits 等事件，
                # 留在 socket 里会被**下一轮** collect 先读到，导致下一轮 0.001s、空文本、
                # 而且答案整体错位一轮（实测 2026-11-04：第 3 轮答的是第 2 轮的问题）。
                # 必须收到 response.done 才算这一轮真正结束。
            elif etype == "response.function_call_arguments.delta":
                key = str(event.get("call_id") or event.get("item_id") or len(args_buffer))
                args_buffer[key] = args_buffer.get(key, "") + event.get("delta", "")
            elif etype == "response.function_call_arguments.done":
                reply.function_calls.append({
                    "name": event.get("name", ""),
                    "call_id": event.get("call_id", ""),
                    "arguments": _loads_or_raw(event.get("arguments")),
                })
            elif etype == "response.done":
                self._inflight = False
                for item in (event.get("response") or {}).get("output") or []:
                    if item.get("type") != "function_call":
                        continue
                    reply.function_calls.append({
                        "name": item.get("name", ""),
                        "call_id": item.get("call_id", ""),
                        "arguments": _loads_or_raw(item.get("arguments")),
                    })
                break
            elif etype == "error":
                err = event.get("error") or {}
                # 不能立刻抛：这一轮的事件还堆在 socket 里，直接抛会让下一轮读到上一轮的尾巴
                # （答案错位一轮），服务端也仍认为有一轮 active。先把通道收干净再报错。
                await self._drain_until_done(3.0)
                self._inflight = False
                raise OmniError_(
                    "服务端错误：%s %s" % (err.get("code", "?"), err.get("message", ""))
                ) from None
        else:
            reply.truncated = True
        reply.latency_ms = (loop.time() - started) * 1000.0
        if args_buffer and not reply.function_calls:
            for key, raw in args_buffer.items():
                reply.function_calls.append({"name": "", "call_id": key, "arguments": _loads_or_raw(raw)})
        return reply


def build_client(config: Optional["OmniConfig"] = None, transport: Any = None, *,
                 transport_factory: Optional[Callable[[], Any]] = None) -> Any:
    """按配置选实现：MiniCPM-o（chat 模式）或 Qwen-Omni（session.update 模式）。

    上层（FastStream / FastObserver）只认方法名，不认协议；换模型只在这里分叉。
    """

    cfg = config or OmniConfig()
    if cfg.is_minicpm():
        from omni_minicpm import MiniCPMClient  # 延迟导入：只有真用 MiniCPM 才需要 websockets

        return MiniCPMClient(cfg, transport=transport, transport_factory=transport_factory)
    return OmniClient(cfg, transport=transport, transport_factory=transport_factory)


def run_blocking(coro):  # pragma: no cover - 同步门面，供非 async 调用方
    """把一轮调用包成同步；内部不 sleep、不轮询。"""
    return asyncio.run(coro)
