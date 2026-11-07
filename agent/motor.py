# -*- coding: utf-8 -*-
"""Gamescaling 策略模型客户端：让 agent 靠专用游戏策略模型操作，而不是拍脑袋按键。

设计口径（用户要求）：**优先用 gamescaling 出手，手动 act 只是它不可用时的兜底**。
所以这个模块既提供"问它该按什么"，也提供"让它按"。

调用链路逐字对齐官方 gamescaling/policy.py（2026-11-04 取回，落盘 _diag/official_policy.py）：

* 请求体由 gamescaling_request.build_request 组装（前置填充 + continue_final_message +
  add_generation_prompt=false + skip_special_tokens=false）；
* 回复先过 gamescaling_action.finalize（补回被吃掉的 prefill、被 stop 切掉的收尾标签），
  再交给 gamescaling_action.parse_action 解析——解析只看最后一个思考闭合标签之后；
* 写回历史的是 Action.history_text（解析失败写显式 <parse_failed> 占位，不是 no-op），
  不是模型原文。

失败口径（设计 03/04：不许造假成功）：模型回复解析不出来、被截断、或清洗后一个
有效动作都不剩，decide() 抛 MotorParseError 让编排层熔断/落证；超过截止时间抛
MotorTimeout。只有显式设置 BETTERAGI_MOTOR_LEGACY_FALLBACK=1 时才走旧的
"自然语言猜键位"兼容路径（那是给旧 TUI 用的，正式链路禁止）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

import gamescaling_action as action_mod
import gamescaling_request as request_mod
import gamescaling_vocab as vocab_mod

ACTION_OPEN_TOKEN = action_mod.ACTION_START
ACTION_CLOSE_TOKEN = action_mod.ACTION_END
DEFAULT_TIMEOUT = 60.0
#: 设计预算：一次决策 = 未来 200ms，6 步 × 33ms（04 验收 S1/T1）。
#: DEFAULT_TIMEOUT 是传输层兜底上限，不是每步预算；正式链路必须由 executor
#: 显式传 decide(deadline_ms=MOTOR_STEP_BUDGET_MS)。
MOTOR_STEP_BUDGET_MS = 200.0
#: 旧 TUI 兼容口：置 1 才允许把自然语言猜成键位/把坏输出当空动作（正式链路禁止）。
LEGACY_FALLBACK_ENV = "BETTERAGI_MOTOR_LEGACY_FALLBACK"
#: 「慢但可用」的宽限（ms）。deadline_ms 是排期用的**软预算**（一次决策覆盖的
#: 游戏时间），不是「超过就把这一步判死」的硬阈值：2026-11-04 实测暴露端点
#: 1 帧 p50 904ms、6 帧 p50 1200ms，按软预算硬掐会让每一步都 MotorTimeout，
#: 整轮 0 步作废（把「慢」错判成「坏」）。真正掐断 = 软预算 + 宽限。
GRACE_ENV = "BETTERAGI_MOTOR_GRACE_MS"
#: 实测尾延迟：同一端点单帧 861/904/914ms，但真跑时也见过 2282ms（公网上传抖动）。
#: 1500ms 的宽限会把 2.2s 的合法回复判死（硬线 2240ms 就差一点），所以放到 3000ms：
#: 慢到 3.7s 以上才叫「等不起」，正常抖动一律照收（late_ms 记进回执）。
DEFAULT_GRACE_MS = 3000.0

#: 帧压缩（2026-11-05）：gamescaling 每步要传「当前帧 + 5 张历史帧」，640x480 JPEG q85
#: 单帧 17–43KB，6 帧 174KB。实测（服务器 loopback，6 帧请求）jpeg85 与 webp65 的动作
#: 输出一致、服务端延迟一致（500–510ms），但上传体 174KB → 69KB（−60%）。公网上行只有
#: ~1MB/s，这一段就是「实时」与「慢三倍」的差别，所以默认在客户端压。
#: BETTERAGI_GS_COMPRESS=0 可关掉（退回逐字节原样发送）。
COMPRESS_ENV = "BETTERAGI_GS_COMPRESS"

#: 传输层：连接必须复用。实测（2026-11-04 暴露端点）253KB 新建连接 0.433s、
#: 复用 0.170s；1.5MB 0.733s → 0.264s —— 每步新建 TLS/TCP 的代价和模型推理一个量级。
TRANSPORT_LIMITS = httpx.Limits(max_connections=4, max_keepalive_connections=4,
                                keepalive_expiry=300.0)
_shared_lock = threading.Lock()
_shared_client: Optional[httpx.Client] = None
#: 建这个单例时用的客户端工厂（测试会 monkeypatch httpx.Client；换了工厂就重建，
#: 免得一个用例的 MockTransport 泄漏到下一个用例）。
_shared_factory: Any = None


def shared_client(timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    """模块级单例 httpx.Client：连接池 + keep-alive，开播前 warmup() 预热。

    为什么必须复用：一次 200ms 的决策里，新建连接（TCP+TLS）实测要 0.17–0.43s，
    比推理本身还贵；每步新建连接等于把带宽和时延都浪费在握手上。

    httpx.Client 是线程安全的，所以 decide / health / warmup 可以共用。
    """

    global _shared_client, _shared_factory
    factory = httpx.Client
    with _shared_lock:
        if (_shared_client is not None and _shared_factory is factory
                and not getattr(_shared_client, "is_closed", False)):
            return _shared_client
        old = _shared_client
        _shared_client = None
        if old is not None:
            close = getattr(old, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - 关旧连接失败不该挡住新建
                    pass
        # trust_env=False：Windows 系统代理（verge-mihomo 127.0.0.1:7897）在注册表里
        # ProxyEnable=1，httpx 默认会读它 —— 实测每个响应都被代理塞 Connection: close，
        # keep-alive 全废、每步重新握手，且多一跳（2026-11-05 同 g2p/lithos 的修法）。
        try:
            _shared_client = factory(timeout=timeout, limits=TRANSPORT_LIMITS, trust_env=False)
        except TypeError:
            # 假件（测试）常常只认 timeout：不要因为多传参数就构造失败。
            try:
                _shared_client = factory(timeout=timeout, limits=TRANSPORT_LIMITS)
            except TypeError:
                _shared_client = factory(timeout=timeout)
        _shared_factory = factory
        return _shared_client


def _client_get(client: Any, url: str, *, headers: Optional[Dict[str, str]] = None,
                timeout: Optional[float] = None) -> Any:
    """GET，但容忍只实现 `get(url, headers=...)` 的假客户端（测试件）。"""

    try:
        return client.get(url, headers=headers, timeout=timeout)
    except TypeError:
        return client.get(url, headers=headers)


def close_shared_client() -> None:
    """显式释放连接池（下播/换端点/进程退出前）。"""

    global _shared_client, _shared_factory
    with _shared_lock:
        client = _shared_client
        _shared_client = None
        _shared_factory = None
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


def _grace_ms(default: float = DEFAULT_GRACE_MS) -> float:
    """读宽限（ms）：非法/负数一律退回默认，绝不因为环境变量写错把宽限变成 0。"""

    raw = (os.environ.get(GRACE_ENV) or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        return float(default)
    return value if value > 0 else float(default)
def _compressing() -> bool:
    """这一进程要不要在客户端压帧（默认要；BETTERAGI_GS_COMPRESS=0 关掉）。"""

    raw = (os.environ.get(COMPRESS_ENV) or "").strip().lower()
    return raw not in ("0", "false", "off", "no")


def compress_frame_url(frame_url: str) -> Tuple[str, Dict[str, Any]]:
    """把一帧 data URL 重编码成更小的 webp（frame_codec，默认 q65、不缩放）。

    返回 (url, meta)。任何异常 / 没装 Pillow / 压完反而更大 → 原样返回，meta.ok=False。
    这条路径在热循环里，绝不能因为压缩失败把决策打崩：宁可发原图。
    """
    if not frame_url or not frame_url.startswith("data:") or not _compressing():
        return frame_url, {"ok": False, "reason": "skipped"}
    try:
        import frame_codec  # 惰性导入：没装 Pillow 时只影响压缩，不影响决策

        url, meta = frame_codec.compress_data_url(frame_url)
        meta = dict(meta or {})
        meta["ok"] = bool(meta.get("ok", True))
        return url, meta
    except Exception as error:  # noqa: BLE001 - 压缩失败必须能继续跑
        return frame_url, {"ok": False, "reason": "%s: %s" % (type(error).__name__, error)}


# 2026-11-04 实测裁决：9B 在云机上是算力瓶颈（~90ms/token，单次决策 3.3~4.5s，
# 比人类反应还慢，手根本跟不上画面）。换 2B-S3 后 27~28ms/token，已把 9B 服务停掉释放显存。
#: 2026-11-06 用户把手的出口换成公网 vLLM（huabei1 租户网关）：
#: 老的本机 18041（SSH 隧道 → AutoDL 4090 的 6006）保留为可回退路径，
#: 只要设 BETTERAGI_GAMESCALING_URL 就能切回去，代码里不再写死隧道。
#: 该端点自带 /v1/models（id=gamescaling，root=/root/data/gs/weights），无需 SSE 中继。
PUBLIC_ENDPOINT = ("https://gamescal-5b0ec-045a5-tenant-81178601-aigaming-web-cn-huabei1"
                   ".ebtech-ai.com")
DEFAULT_ENDPOINT = os.environ.get("BETTERAGI_GAMESCALING_URL", PUBLIC_ENDPOINT)


class MotorError_(RuntimeError):
    pass


class MotorParseError(MotorError_):
    """模型回复不可用（解析失败/被截断/清洗后没有有效动作）。

    设计口径：**这必须是一次显式失败**。旧实现会用 label_action 把自然语言猜成
    6 组键位、或用显式 no-op 混过去，让"这一步失败了"在慢系统眼里变成"成功按了
    一下"；慢系统因此永远学不到该重试/换路。调用方（executor）按本异常类名熔断、
    落事故目录，不许吞掉。

    诊断字段：
        raw_text:    模型原文（finalize 之前，一字不改），落证要复现的就是它；
        cleaned:     finalize 之后的文本（补过 prefill/收尾标签），用于区分"服务端
                     把标签吃了"和"模型根本没吐格式"；
        http_status: 这一轮的 HTTP 状态码；
        frame_url:   这一轮看的帧（data URL），事故目录靠它冻结画面；
        reason:      unparsed / truncated / no_valid_action；
        dropped:     被训练词表清洗掉的 token（空动作归因用）。
    """

    def __init__(self, message: str, *, raw_text: str = "", cleaned: str = "",
                 http_status: Optional[int] = None, frame_url: str = "",
                 reason: str = "", dropped: Optional[Sequence[str]] = None) -> None:
        super().__init__(message)
        self.raw_text = raw_text or ""
        self.cleaned = cleaned or ""
        self.http_status = http_status
        self.frame_url = frame_url or ""
        self.reason = reason or ""
        self.dropped = list(dropped or [])


class MotorTimeout(MotorError_):
    """一次决策超过截止时间（设计预算 200ms）。

    不许把超时当成"这一步什么都不按"返回：那会把"手慢了"伪装成"模型决定不按"，
    编排层就无法用上一步动作兜底或停播。elapsed_ms 是实测耗时，用于落账。
    """

    def __init__(self, message: str, *, elapsed_ms: float = 0.0,
                 deadline_ms: float = 0.0, frame_url: str = "") -> None:
        super().__init__(message)
        self.elapsed_ms = float(elapsed_ms)
        self.deadline_ms = float(deadline_ms)
        self.frame_url = frame_url or ""


#: 模型不吐动作格式时，从自由文本里认键名（实测它爱答 "Hold A and enter to continue."）。
NATURAL_LABEL_KEYS = {
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "up arrow": "Up", "down arrow": "Down",
    "left arrow": "Left", "right arrow": "Right",
    "confirm": "Z", "accept": "Z", "press z": "Z",
    "cancel": "X", "backspace": "Back", "enter": "Enter", "return": "Enter",
    "escape": "Esc", "esc": "Esc", "menu": "C", "space": "Space",
    "press a": "Z", "button a": "Z", "use a": "Z",
}


def label_action(text: str) -> List[str]:
    """把一句自然语言收回成一步按键标签；认不出返回空列表。

    实测（_diag/t_motor_raw_content.py，2026-11-04）：命名界面上 gamescaling 会输出
    "Hold A and enter to continue." / "Use the mouse to interact with the game."
    —— 全是人话，一个动作符号都没有。此时整轮丢掉比按一下已知键更糟：
    agent 会一直看到同一帧、永远不推进。所以认出一个可信键就按下去，并标 fallback。

    返回的是**训练词表里的 token**（大写字母 / Enter / Back / Esc），不是小写别名：
    小写 z 是词表外 token，服务端静默丢弃（README §5），等于又变成空动作。
    """
    low = (text or "").lower()
    if not low.strip():
        return []
    hits: List[str] = []
    for phrase, token in NATURAL_LABEL_KEYS.items():
        if phrase in low and token not in hits:
            hits.append(token)
    return hits[:1]


def _parse_action(text: str) -> Dict[str, Any]:
    """解析 gamescaling 的动作串（严格按它自己的系统提示词规格）。

    格式（``agent/templates/gamescaling_system_prompt.txt``）：

        <|action_start|>X Y Z ; k1 k2 k3 ; k4 k5 ; k6 ; k7 ; k8 ; k9 k10<|action_end|>

    - ``X Y Z``：鼠标**相对位移**（千分比，X>0 右，Y>0 下）、``Z`` 滚轮（Z>0 上）。
    - 后面**恰好 6 组按键**，分号分隔，组内空格分隔，每组最多 4 键；空组保留分号。
    - 语义：一次决策 = 未来 200ms，6 步、每步 33ms；组 i 就是第 i 步要按住的键。

    真正的解析在 gamescaling_action（逐字对齐官方）；这里只保证对外字段名不变——
    工具层和测试都按 ``raw/mouse/steps/keys/parsed`` 读，换成内部结构会连累一大片调用方。
    """
    return action_mod.parse_action(text or "").as_dict()


class GamescalingClient:
    """OpenAI 兼容端点（默认本机 18041，可切云机）。

    请求体严格按 README 第 4 节构造：多轮历史 + assistant 前置填充 +
    skip_special_tokens=false + continue_final_message=true。历史由 set_history()
    注入，客户端自己不做帧缓存——谁抓帧谁负责记历史，这样重放/回滚都说得清。
    """

    def __init__(self, endpoint: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT,
                 api_key: Optional[str] = None):
        # 默认走 18041：云机 2B 服务在 8041，经 SSH 隧道转到本机 18041。
        # （8040/8041 落在 Windows Hyper-V 的保留端口区间里，绑定直接 WinError 10013，
        # 所以本地一律用 18xxx。本机 8040 上如果有别的东西在听，会返回 502 把请求吞掉——实测踩过。）
        self.endpoint = (endpoint or os.environ.get("BETTERAGI_GAMESCALING_URL")
                         or DEFAULT_ENDPOINT).rstrip("/")
        self.timeout = timeout
        # 服务端 /v1/models 报的 id 就是 "gamescaling"；写别的名字会被 404 挡回来。
        self.model = os.environ.get("BETTERAGI_GAMESCALING_MODEL", "gamescaling")
        self.api_key = api_key or os.environ.get("BETTERAGI_GAMESCALING_KEY",
                                                request_mod.DEFAULT_API_KEY)
        self.history: List[Dict[str, str]] = []
        self.history_len = int(os.environ.get("BETTERAGI_GAMESCALING_HISTORY", "5"))
        # reset() 被调用过几次：executor 用它核对"每个意图都真的重置了历史"
        # （旧实现 reset 全仓零调用，每个意图都在继承上一个意图的 history）。
        self.reset_count = 0
        self.last_user_text = ""
        self.last_text = ""
        self.last_reasoning = ""
        # 最近一次压缩结果：decide 压缩完把 (原 URL -> 压缩 URL) 记下来，record 直接复用，
        # 同一帧不重复编码（热循环里 6 帧各编一次已经够贵）。
        self.last_frame_meta: Dict[str, Any] = {}
        self._frame_in_url = ""
        self._frame_out_url = ""

    # ---- 历史 -------------------------------------------------------
    def reset(self) -> None:
        """新的一局 / 新意图开始时清空历史（README §3：policy.reset()）。

        幂等：重复调用不抛错，只让 reset_count 递增；executor 可以放心地
        "每个意图开头调一次"，也可以在 decide(new_intent=True) 里间接触发。
        """
        self.history = []
        self.reset_count += 1

    def set_history(self, history: Sequence[Dict[str, str]]) -> None:
        self.history = [dict(turn) for turn in (history or []) if turn]

    def record(self, frame_url: str, action_text: str, *, pressed: Optional[bool] = None,
               step: Optional[int] = None, skipped_reason: str = "") -> None:
        """把这一轮（帧 + 实际执行的动作）记进历史，只留最近 history_len 轮。

        pressed/step/skipped_reason 来自 adapter.play_steps 的 step_results：给了就在
        动作串尾巴上附一句「# step3 skipped: no_window_focus」，模型下一轮才知道
        上一步的键根本没泵进去、别再照同一个打法重复。不给则与旧格式逐字节一致。
        """
        # 历史帧也要压：6 张历史帧是上传体的大头（每张 17–43KB → webp 后 ~11KB）。
        if frame_url and frame_url == self._frame_in_url and self._frame_out_url:
            frame_url = self._frame_out_url
        else:
            frame_url, _meta = compress_frame_url(frame_url)
        turn = request_mod.history_turn(frame_url, action_text, pressed=pressed,
                                        step=step, skipped_reason=skipped_reason)
        self.history = self.history + [turn]
        if self.history_len > 0:
            self.history = self.history[-self.history_len:]

    def health(self, timeout: float = 3.0, *, probe_ready: bool = True) -> Dict[str, Any]:
        """就绪探针：**服务在**、**模型名对**、**能推理** 是三件事，分开报。

        - /v1/models 拿不到 → available=False（进程/端口/隧道问题）
        - 拿到了但 id 里没有 self.model → model_ok=False（--served-model-name 写错，
          这种情况下 vLLM 会对所有请求回 404，而 /v1/models 本身是 200——只查
          available 会误判成「健康」）
        - probe_ready 时再打 /health（vLLM 就绪）；404/超时记 None，不当失败
        """
        out: Dict[str, Any] = {"endpoint": self.endpoint, "model": self.model}
        try:
            client = shared_client(timeout=timeout)
            headers = {"Authorization": "Bearer " + self.api_key}
            resp = _client_get(client, self.endpoint + "/v1/models", headers=headers,
                               timeout=timeout)
            out["status"] = resp.status_code
            out["body"] = resp.text[:200]
            out["available"] = resp.status_code < 400
            ids: List[str] = []
            if out["available"]:
                try:
                    payload = resp.json()
                    ids = [str(item.get("id", "")) for item in (payload.get("data") or [])
                           if isinstance(item, dict)]
                except Exception:  # noqa: BLE001 - 非 JSON 就是服务不对，按空处理
                    ids = []
            out["model_ids"] = ids
            out["model_ok"] = (self.model in ids) if ids else None
            if out["available"] and probe_ready:
                try:
                    ready = _client_get(client, self.endpoint + "/health", headers=headers,
                                        timeout=timeout)
                    out["ready"] = ready.status_code < 400
                    out["ready_status"] = ready.status_code
                except Exception as error:  # noqa: BLE001
                    out["ready"] = None
                    out["ready_error"] = "%s: %s" % (type(error).__name__, error)
            else:
                # 没打 /health 就不知道能不能推理：None 表示「没查」，不是「没就绪」。
                out["ready"] = None
        except Exception as error:  # noqa: BLE001
            out.update({"available": False, "ready": False, "model_ok": None,
                        "model_ids": [],
                        "error": "%s: %s" % (type(error).__name__, error)})
        if out.get("available") and out.get("model_ok") is False:
            out["hint"] = ("服务在，但 /v1/models 里没有 " + self.model
                            + "：检查 --served-model-name 与 BETTERAGI_GAMESCALING_MODEL")
        elif not out.get("available"):
            out["hint"] = "起手模型服务（2B-S3 checkpoint 单卡），再验 /health"
        return out

    def warmup(self, *, attempts: int = 2, timeout: float = 10.0) -> Dict[str, Any]:
        """开播前预热连接池：连打 N 次 GET /v1/models。

        第一次会把 DNS + TCP + TLS 全走完，之后每步决策只付推理时间。落回执里的
        `reused` 就是「第二次比第一次快」的证据（实测复用能省 0.17–0.26s/次）。
        """

        marks: List[float] = []
        status: Optional[int] = None
        ok = False
        headers = {"Authorization": "Bearer " + self.api_key}
        client = shared_client(timeout=timeout)
        for _ in range(max(1, int(attempts))):
            started = time.monotonic()
            try:
                resp = _client_get(client, self.endpoint + "/v1/models", headers=headers,
                                   timeout=timeout)
                status = resp.status_code
                ok = resp.status_code < 400
            except Exception as error:  # noqa: BLE001 - 预热失败要如实上报
                return {"ok": False, "error": "%s: %s" % (type(error).__name__, error),
                        "attempts": len(marks),
                        "elapsed_ms": [round(value, 1) for value in marks]}
            marks.append((time.monotonic() - started) * 1000.0)
        return {"ok": ok, "status": status, "attempts": len(marks),
                "elapsed_ms": [round(value, 1) for value in marks],
                "reused": bool(len(marks) >= 2 and marks[1] < marks[0])}

    def decide(
        self,
        frame_url: str,
        instruction: str = "",
        game: str = "",
        system_prompt: Optional[str] = None,
        *,
        reasoning: bool = True,
        deadline_ms: Optional[float] = None,
        new_intent: bool = False,
        grace_ms: Optional[float] = None,
    ) -> Dict[str, Any]:
        """把当前帧交给策略模型，拿回它建议的 200ms 动作。

        instruction 只用来记日志/排错：README 的 user 轮是固定文本
        "current game screen"，任务信息全部由系统提示词里的 Game Rules 承载。
        早先我们往 user 轮里塞自造指令，等于跳出训练分布，模型随即退化。

        deadline_ms：这一步的**软预算**（一次决策覆盖的游戏时间，实测值见
        agent/pacing.py）。None 表示沿用 self.timeout（60s，传输层上限）。
        超过软预算不算失败：返回值带 late_ms/over_budget，动作照用（「慢」不是「坏」）。
        只有超过 软预算 + grace_ms（默认 1500ms，可用 BETTERAGI_MOTOR_GRACE_MS 改）
        才抛 MotorTimeout——那才是真的等不起了，绝不当空动作返回。

        new_intent：默认 False，由 executor 在意图切换时传 True；为 True 时先
        reset() 清掉上一意图的 history（reset_count 会 +1）。

        失败口径：解析失败/截断/清洗后无有效动作 → MotorParseError；超过 deadline
        → MotorTimeout。唯一例外是显式开启 BETTERAGI_MOTOR_LEGACY_FALLBACK=1 的
        旧 TUI 兼容口。
        """
        if new_intent:
            self.reset()
        if not frame_url.startswith("data:"):
            raise MotorError_("frame 必须是 data URL")
        deadline_ms = float(self.timeout * 1000.0 if deadline_ms is None else deadline_ms)
        if deadline_ms <= 0:
            raise MotorError_("deadline_ms 必须为正数")
        # hard_ms 才是「掐断」的线：软预算超了只记 late_ms，照常把动作交出去。
        grace = _grace_ms() if grace_ms is None else max(0.0, float(grace_ms))
        hard_ms = deadline_ms + grace
        started = time.monotonic()

        def elapsed_ms() -> float:
            """从发起请求到现在的耗时（ms）；超时判定与异常落账共用。"""
            return (time.monotonic() - started) * 1000.0

        self.last_user_text = (instruction or "").strip()
        # 当前帧同样压一次再发（默认 webp q65，约 −70% 字节）。压缩只改上传体，
        # 1080p/640x480 的原尺寸不变 —— 模型看到的是同一张图，只是编码更省。
        raw_frame_url = frame_url
        frame_url, frame_meta = compress_frame_url(frame_url)
        self.last_frame_meta = frame_meta
        self._frame_in_url = raw_frame_url
        self._frame_out_url = frame_url
        payload = request_mod.build_request(
            self.model, system_prompt, self.history, frame_url, reasoning=reasoning,
        )
        last_error: Optional[Exception] = None
        data: Dict[str, Any] = {}
        http_status: Optional[int] = None
        # 实测（_diag/t_motor_ab.py，2026-11-04）单发偶发 502：SSH 转发层瞬时断开，
        # 紧接着同样的请求就 200。所以只对连接层错误和 5xx 重试一次，4xx 直接报错
        # （4xx 是请求本身的问题，重试没有意义，只会白烧 GPU）。
        for attempt in range(2):
            try:
                # 剩余预算真正交给 httpx：连接/读超时都卡在 deadline 上，
                # 而不是让 60s 的 self.timeout 把 200ms 预算架空。
                remaining = max(hard_ms - elapsed_ms(), 1e-3) / 1000.0
                # 复用模块级连接池：每步新建连接要 0.17–0.43s，和推理同量级。
                client = shared_client(timeout=self.timeout)
                resp = client.post(self.endpoint + "/v1/chat/completions", json=payload,
                                   headers=request_mod.headers(self.api_key),
                                   timeout=remaining)
                http_status = resp.status_code
                if resp.status_code >= 500 and attempt == 0:
                    last_error = MotorError_(
                        "gamescaling HTTP %d: %s" % (resp.status_code, resp.text[:200]))
                    continue
                if resp.status_code >= 400:
                    raise MotorError_(
                        "gamescaling HTTP %d: %s" % (resp.status_code, resp.text[:200]))
                data = resp.json()
                last_error = None
                break
            except MotorTimeout:
                raise
            except httpx.TimeoutException as error:
                # 超时是显式失败，不是"这一步不按"。
                raise MotorTimeout(
                    "gamescaling 决策超时：已用 %.0fms，预算 %.0fms（含宽限 %.0fms 仍然超）"
                    % (elapsed_ms(), deadline_ms, grace),
                    elapsed_ms=elapsed_ms(), deadline_ms=hard_ms, frame_url=frame_url,
                ) from error
            except MotorError_:
                raise
            except Exception as error:  # noqa: BLE001 - 连接层错误重试一次
                last_error = error
                if attempt == 1:
                    break
        if last_error is not None and not data:
            raise MotorError_("gamescaling 调用失败：%s: %s"
                              % (type(last_error).__name__, last_error))
        if elapsed_ms() > hard_ms:
            # httpx.MockTransport 与部分转发层不执行 timeout，响应到了也必须复核，
            # 但复核的是硬线（软预算 + 宽限）：慢一点点照收，慢到离谱才判死。
            raise MotorTimeout(
                "gamescaling 决策超时：已用 %.0fms，硬上限 %.0fms（预算 %.0fms + 宽限 %.0fms）"
                % (elapsed_ms(), hard_ms, deadline_ms, grace),
                elapsed_ms=elapsed_ms(), deadline_ms=hard_ms, frame_url=frame_url,
            )
        text = ""
        finish_reason: Optional[str] = None
        try:
            choice = data["choices"][0]
            text = (choice.get("message") or {}).get("content") or ""
            finish_reason = choice.get("finish_reason")
        except Exception:  # noqa: BLE001
            text = json.dumps(data, ensure_ascii=False)[:400]
        # finalize 是官方解法：服务端把 stop 字符串吃掉、前置填充不进返回文本，
        # 不补回来就会解析成空动作（历史 bug：整轮静默全零）。
        # raw_text 是模型原文（finalize 之前）：事故报告里要复现的就是它，
        # cleaned 才是补过 prefill/收尾标签的文本。两者一起落进 MotorParseError。
        raw_text = text
        text = action_mod.finalize(text, finish_reason, request_mod.prefill(reasoning))
        self.last_text = text
        self.last_reasoning = action_mod.split_reasoning(text)[0]
        act = action_mod.parse_action(text)
        parsed = act.as_dict()
        # 动作块一律清洗成训练词表 token：词表外的东西会被服务端丢掉，
        # 留在这里只会让日志骗人（"明明出了 z" 其实一个都没执行）。
        steps, dropped = vocab_mod.sanitize_steps_report(parsed.get("steps") or [])
        parsed["steps"] = steps
        parsed["keys"] = [key for step in steps for key in step]
        if dropped:
            # 原样留证：模型出了什么、我们丢了什么。没有这一行，"空动作"无法归因。
            parsed["dropped"] = list(parsed.get("dropped") or []) + dropped
        # as_dict 的 mouse 是 {"x","y","z"}（沿用工具层字段名），这里只判是否全零。
        mouse = parsed.get("mouse") or {}
        mouse_vals = [float(v) for v in (mouse.values() if isinstance(mouse, dict) else mouse)]
        # 解析成功但 6 组全空 + 鼠标不动：合法（游戏里就是什么都不按），但必须显式标空。
        empty = not parsed["keys"] and not any(abs(v) > 1e-9 for v in mouse_vals)
        if os.environ.get(LEGACY_FALLBACK_ENV) == "1":
            # 旧 TUI 兼容口（默认关闭，正式链路禁止）：模型不吐格式时从自然语言猜键位，
            # 并把坏输出当 no-op 写历史——这正是本次修掉的造假路径，只留给未迁移的旧调用方。
            if not parsed.get("parsed"):
                labels = [vocab_mod.to_token(x) for x in label_action(text)]
                labels = [x for x in labels if vocab_mod.in_vocab(x)]
                if labels:
                    labels = list(dict.fromkeys(labels))
                    parsed["steps"] = [list(labels), [], [], [], [], []]
                    parsed["keys"] = list(labels)
                    parsed["fallback"] = "natural_language"
            # 兼容口猜出键位后要按猜到的结果重算 empty，否则会报一个假的"全空"。
            empty = (not parsed["keys"]
                     and not any(abs(v) > 1e-9 for v in mouse_vals))
            parsed["history_text"] = (act.history_text if act.parsed
                                     else action_mod.NOOP_ACTION_TEXT)
        else:
            # 严格口径：坏输出/截断/清洗后无有效动作都是显式失败，交给编排层熔断，
            # 绝不再猜键位、也不再把失败伪装成空动作。
            if act.truncated:
                raise MotorParseError(
                    "gamescaling 回复被截断（finish_reason=%r），拒绝把半截动作当成功"
                    % (finish_reason,),
                    raw_text=raw_text, cleaned=text, http_status=http_status,
                    frame_url=frame_url, reason="truncated", dropped=dropped,
                )
            if not parsed.get("parsed"):
                raise MotorParseError(
                    "gamescaling 回复解析不出动作块：%r" % (text[:200],),
                    raw_text=raw_text, cleaned=text, http_status=http_status,
                    frame_url=frame_url, reason="unparsed", dropped=dropped,
                )
            if empty and dropped:
                raise MotorParseError(
                    "gamescaling 动作清洗后一个有效 token 都不剩（keys=%r，dropped=%r）"
                    % (parsed.get("keys"), dropped),
                    raw_text=raw_text, cleaned=text, http_status=http_status,
                    frame_url=frame_url, reason="no_valid_action", dropped=dropped,
                )
            parsed["history_text"] = act.history_text
        parsed["empty"] = empty
        # late_ms：慢但可用时的迟到量（软预算之外的毫秒）。执行器据此记 slow_steps，
        # 慢系统据此知道「手在等模型」，但这一步不算失败。
        late = max(0.0, elapsed_ms() - deadline_ms)
        parsed.update({"endpoint": self.endpoint, "model": self.model, "text": text,
                       "reasoning": self.last_reasoning, "finish_reason": finish_reason,
                       "history_len": len(self.history), "http_status": http_status,
                       "deadline_ms": deadline_ms, "late_ms": round(late, 1),
                       "over_budget": bool(late > 0)})
        return parsed


def decision_action_text(decision: Dict[str, Any]) -> str:
    """把一次决策还原成要写进历史的动作串。

    正常路径读 decide() 写好的 history_text；解析失败那一轮写的是
    <parse_failed> 占位（设计 03 C.5），不是 no-op。只有既拿不到 history_text、
    也没有可还原 raw 的旧决策才退回显式 no-op——它们已不在正式链路上。
    """
    recorded = str((decision or {}).get("history_text") or "").strip()
    if recorded:
        return recorded
    raw = str((decision or {}).get("raw") or "").strip()
    if not (decision or {}).get("parsed") or not raw:
        return request_mod.NOOP_ACTION
    return request_mod.ACTION_OPEN + raw + request_mod.ACTION_CLOSE


def build_motor() -> Any:
    """按 BETTERAGI_MOTOR_BACKEND 选手模型后端。

    2026-11-05 用户拍板换 game2player-4b：返回 g2p.G2PClient，接口与
    GamescalingClient 一致（decide/reset/record/health），executor 不用改。
    """

    try:
        import g2p  # type: ignore

        if g2p.backend() == "game2player":
            return g2p.build_motor()
    except Exception:  # noqa: BLE001 - g2p 不在就退回 gamescaling
        pass
    return _build_gamescaling()


def _build_gamescaling() -> GamescalingClient:
    """默认构造：端点/模型/历史长度全从环境变量读，测试里能整体替掉。"""
    return GamescalingClient()
