"""快系统（Qwen-Omni 实时）门面：把「看一帧 → 一句判断」包成同步调用 + 意图解析。

分工口径：
- 快系统只负责**看**：这一帧是什么状态、下一步该按什么键。延迟 <1s，可以每帧都调。
- 慢系统（DeepSeek）负责**决定**：目标、策略、要不要听弹幕。它拿快系统的结论当输入。
- 所以快系统不直接下指令，只产出 Intent 候选，交给 IntentBus 仲裁。

实测（2026-11-04，真实 Undertale 命名界面）：
connect 0.21s；三轮 0.61s / 0.41s / 0.34s；能读出「为坠落的人类命名」并判断高亮在 A。
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import omni
from intent import Intent, INTENT_TYPES

#: 快系统的人设：越短越省 token，也越不容易跑偏去写小说。
FAST_INSTRUCTIONS = (
    "你是一个游戏画面观察员。只根据画面回答，不许编造。"
    "用一句话说清当前画面的状态（在哪个界面、看到了什么文字、光标在哪）。"
)

INTENT_INSTRUCTIONS = (
    "你是游戏操作观察员。看着画面判断此刻最该做的一个操作。"
    "只输出一行 JSON，不要解释："
    '{"type":"navigate|interact|combat|explore|wait|NO_OP","target":"要按的键或对象","why":"不超过20字"}'
)


#: 单帧上限。官方硬限制是 Base64 之前 256KB。
MAX_FRAME_BYTES = omni.MAX_IMAGE_BYTES

#: 一次 observe 的默认上限（毫秒）。设计文档 02_schemas.md §5.3 给了 Omni 首 token 30s /
#: 单步 5s；observe 是「一整轮问答」，实测 0.3-0.7s，给 12s 已经非常宽松——超过就是故障。
#: 可用环境变量 BETTERAGI_OBSERVE_DEADLINE_MS 覆盖。
DEFAULT_OBSERVE_DEADLINE_MS = 12000


def observe_deadline_ms() -> int:
    '''当前 observe 上限（毫秒）：环境变量优先。'''

    try:
        value = int(os.environ.get('BETTERAGI_OBSERVE_DEADLINE_MS') or 0)
    except Exception:  # noqa: BLE001
        value = 0
    return value if value > 0 else DEFAULT_OBSERVE_DEADLINE_MS


class FastSystemError_(RuntimeError):
    """快系统不可用（缺 key、连不上、返回不可解析）。"""


def data_url_to_bytes(data_url: str) -> bytes:
    """把 data:image/png;base64,xxx 拆成原始字节。"""

    if not data_url:
        raise FastSystemError_("空图像")
    if data_url.startswith("data:"):
        try:
            _, payload = data_url.split(",", 1)
        except ValueError as exc:
            raise FastSystemError_("图像 data URL 格式不对") from exc
    else:
        payload = data_url
    try:
        return base64.b64decode(payload)
    except Exception as exc:  # noqa: BLE001
        raise FastSystemError_("图像 base64 解不开") from exc


def parse_intent(text: str) -> Optional[Intent]:
    """从快系统输出里抠出一个 Intent；抠不到返回 None（不是错，别抛）。"""

    raw = (text or "").strip()
    if not raw:
        return None
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    itype = str(payload.get("type") or "").strip()
    # 兼容模型把类型写成大写或中文
    aliases = {"无操作": "NO_OP", "等待": "wait", "移动": "navigate", "战斗": "combat"}
    itype = aliases.get(itype, itype)
    if itype not in INTENT_TYPES:
        return None
    return Intent(
        type=itype,
        target=str(payload.get("target") or "")[:64],
        why=str(payload.get("why") or "")[:120],
        confidence=0.6,
        meta={"source": "fast"},
    )


class FastObserver:
    """同步门面：内部常驻一个 asyncio loop 线程，复用同一条 WS 连接。

    不做阻塞 sleep、不做轮询：每次 observe 就是一次 request/response。
    """

    def __init__(self, config: Optional[omni.OmniConfig] = None, model: str = ""):
        self.config = config or omni.OmniConfig(
            api_key=resolve_omni_key(),
            model=model or omni.DEFAULT_MODEL,
        )
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._client: Optional[omni.OmniClient] = None
        self.last_latency_ms: float = 0.0
        #: 上一次连接为什么被判死（超时/解析失败），落 session 用。
        self.last_drop_reason: str = ''
        #: 累计判死次数：连续增长说明链路真的坏了，不是偶发。
        self.drop_count: int = 0

    # ---------------------------------------------------------------- 生命周期
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            return self._loop
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(target=run, name="fast-system-loop", daemon=True)
        thread.start()
        ready.wait(5.0)
        self._loop, self._thread = loop, thread
        return loop

    def _run(self, coro, timeout_s: float = 60.0, what: str = 'call'):
        '''在常驻 loop 线程里跑一个协程，超时必须「取消 + 断开」，不许只丢一个异常。

        旧实现的缺陷：future.result(timeout=60) 超时之后，协程还在跑、WS 上还挂着
        一轮没读完的 response，于是下一次 ask 会读到上一轮迟到的 response.done，
        表现为串台（设计文档 04_acceptance.md 的 S4 演练）。这里的规矩：

        1. future.cancel() 让协程退出；
        2. 立刻把这条连接判死（self._client = None），下一次 observe 会重连；
        3. 抛 FastSystemError_，调用方能区分「慢」与「死」，不许当成功。
        '''

        loop = self._ensure_loop()
        started = time.monotonic()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError as error:
            future.cancel()
            elapsed_ms = int((time.monotonic() - started) * 1000)
            dropped = self._drop_client('timeout %dms (%s)' % (elapsed_ms, what))
            raise FastSystemError_(
                '快系统超时：%s 等了 %dms 没回（上限 %.1fs），连接已断开，下次会重连%s'
                % (what, elapsed_ms, float(timeout_s), '' if dropped else '（断开失败）')
            ) from error

    def _drop_client(self, reason: str) -> bool:
        '''把当前连接判死：在 loop 上排一个关闭任务，然后清空引用。'''

        client, loop = self._client, self._loop
        self._client = None
        self.last_drop_reason = str(reason or '')
        if client is not None:
            self.drop_count += 1
        if client is None or loop is None:
            return False

        async def _close() -> None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - 关不掉也要往下走
                pass

        coro = _close()
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:  # noqa: BLE001 - loop 已经关了/没在跑：协程要显式关掉，别留 RuntimeWarning
            coro.close()
            return False
        # 关闭失败不该在后台炸出 unhandled exception（连接已经判死了）。
        future.add_done_callback(lambda done: done.cancelled() or done.exception())
        return True

    def connect(self) -> None:
        async def _do() -> None:
            # 2026-11-06：按配置用 MiniCPM-o 或 Qwen-Omni，上层调用面不变。
            client = omni.build_client(self.config)
            await client.connect()
            self._client = client

        self._run(_do())

    def close(self) -> None:
        """收摊：先让 loop 把在飞的协程（含 _drop_client 排的那次 close）跑完，再停 loop。

        直接 loop.stop() 会把 pending 的 Task 连同它的协程一起丢掉，Python 解释器
        退出时就会报 'Task was destroyed but it is pending' + 'was never awaited'。
        所以这里先排一个 shutdown 协程、等它真的跑完（有上限，绝不无限等），
        再停 loop、再 join 线程。
        """

        client, loop = self._client, self._loop
        self._client = None
        if loop is None:
            self._loop = None
            self._thread = None
            return
        done = threading.Event()

        async def _shutdown() -> None:
            try:
                if client is not None:
                    try:
                        await client.close()
                    except Exception:  # noqa: BLE001
                        pass
                # 把已经排进来但还没跑的收尾任务（_drop_client 里那次 close）也跑完。
                pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            finally:
                done.set()

        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), loop)
        except Exception:  # noqa: BLE001 - loop 已停/已关：没什么可收的了
            done.set()
        done.wait(15)
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:  # noqa: BLE001
            pass
        thread, self._thread = self._thread, None
        self._loop = None
        if thread is not None:
            thread.join(timeout=5)

    # ---------------------------------------------------------------- 观察
    def available(self) -> bool:
        try:
            self.config.resolve_key()
            return True
        except Exception:  # noqa: BLE001
            return False

    def observe(
        self,
        image: Any,
        instructions: str = FAST_INSTRUCTIONS,
        deadline_ms: Optional[int] = None,
    ) -> str:
        """看一帧，返回一句描述。image 可以是 bytes、原始 base64 或 data URL。

        deadline_ms 缺省走 observe_deadline_ms()；超时抛 FastSystemError_ 并断开连接，
        调用方（adapter/executor）必须把它当失败处理，不许当「画面没变化」继续跑。
        """

        # 允许一次看多帧：主画面 + 把看不清的文字重画一份的清晰图（同一时刻的两张）。
        extra: list[bytes] = []
        if isinstance(image, (list, tuple)):
            head = image[0]
            rest = list(image[1:])
            if isinstance(head, (list, tuple)):  # [(bytes, mime), ...]
                head, rest = head[0], [item[0] if isinstance(item, tuple) else item for item in rest]
            for item in rest:
                if isinstance(item, tuple):
                    item = item[0]
                if isinstance(item, str):
                    item = data_url_to_bytes(item)
                if item:
                    extra.append(item)
            image = head
        if isinstance(image, str):
            image = data_url_to_bytes(image)
        if not image:
            raise FastSystemError_("空帧")
        if len(image) > MAX_FRAME_BYTES:
            raise FastSystemError_(f"帧太大（{len(image)}B > {MAX_FRAME_BYTES}B）")

        if self._client is None:
            self.connect()
        assert self._client is not None

        async def _do() -> omni.OmniReply:
            return await self._client.ask(image, instructions=instructions, extra_frames=extra or None)

        budget_ms = int(deadline_ms or observe_deadline_ms())
        reply = self._run(_do(), timeout_s=budget_ms / 1000.0, what='observe')
        self.last_latency_ms = reply.latency_ms
        return reply.text

    def suggest_intent(self, image: bytes | str) -> Optional[Intent]:
        """看一帧 → 解析成 Intent 候选；解析不出就是 None。"""

        text = self.observe(image, instructions=INTENT_INSTRUCTIONS)
        intent = parse_intent(text)
        if intent is not None:
            intent.meta["raw"] = text[:200]
            intent.meta["latency_ms"] = round(self.last_latency_ms, 1)
        return intent


def resolve_dashscope_key() -> str:
    """取快系统的凭据：环境变量优先，其次 DSH 的 credentials 文件，最后 local_key 文件。

    为什么必须落盘可读：普通 cmd/PowerShell 窗口里没有 key、DSH 也不会把
    key 注入子进程，之前 TUI 一起跑就报"快系统未启用"，整条双系统退化成单腿。
    读取顺序固定、任何一步失败都继续往下，不抛异常。
    """
    key = (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    if key:
        return key
    # 1) DSH credentials（yaml 的 refs 段）+ 2) 项目自己的 key 文件（不入库，仅本机）
    candidates = [
        os.path.expanduser(os.environ.get("DSH_CREDENTIALS", "~/.dsh/.credentials.yaml")),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "keys", "dashscope.key"),
    ]
    for path in candidates:
        try:
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            if path.endswith((".yaml", ".yml")):
                import yaml  # type: ignore

                data = yaml.safe_load(text) or {}
                value = str((data.get("refs") or {}).get("DASHSCOPE_API_KEY") or "").strip()
            else:
                value = text.strip()
            if value:
                return value
        except Exception:  # noqa: BLE001 - 读不到就继续找下一个来源
            continue
    return ""


def resolve_omni_key() -> str:
    """快系统的 key：MiniCPM 优先，其次旧的 DashScope 链。

    2026-11-06 用户把 omni 换成 MiniCPM-o-4.5-Realtime（key 以 sk-live- 开头），
    DashScope 那把留给 qwen 的慢系统和回退路径。两把 key 属于不同区域，绝不能串用：
    先按 MiniCPM 的顺序找，找不到再退回 DashScope。
    """

    env = (os.environ.get("MINICPM_API_KEY") or "").strip()
    if env:
        return env
    try:
        import omni_minicpm

        key = omni_minicpm._read_key_file()
        if key:
            return key
    except Exception:  # noqa: BLE001 - 读不到就继续往下找
        pass
    return resolve_dashscope_key()


def build_fast_system(model: str = "") -> Optional[FastObserver]:
    """按环境变量决定要不要起快系统；没 key 就返回 None（慢系统单腿跑）。"""

    if os.environ.get("BETTERAGI_FAST", "1") in ("0", "false", "off"):
        return None
    key = resolve_omni_key()
    if not key:
        return None
    try:
        return FastObserver(omni.OmniConfig(api_key=key, model=model or omni.DEFAULT_MODEL))
    except Exception:  # noqa: BLE001
        return None
