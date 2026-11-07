"""BetterGI 实时解说独立运行器（只读出声道，不含决策与语言模型）

Amaidesu 作为 VTuber 输出端单独运行：不启动主播 Agent、不构造 LLMManager、
不起 Dashboard，只把 BetterGI 实时会话里的解说接进语音合成、字幕与虚拟形象。
适合"导演/解说生成在别的进程"的部署——台词由外部产生，本进程只负责播出。

装配复用主程序的既有入口，不复制业务逻辑：

- ``build_tts_infrastructure`` / ``derive_speech_config``（TTS 与发言队列参数）
- ``build_subtitle_infrastructure`` + ``bind_playback_subtitle``（字幕）
- ``build_lipsync_analyzer``（口型共享件）
- ``bind_core_tools`` + ``ToolRegistry.start_providers``（VTS / Warudo / VRChat
  等虚拟形象平台，被动半订阅 ``streamer.speech`` 反射情绪）

配置同样来自 ``config/`` 七文件树（``ConfigService`` 加载与校验）。解说接收
走 ``BetterGILiveBridge``，与主程序内嵌接入是同一份实现：只读、类型白名单、
判重、有界队列、断线重连。

运行::

    uv run python -m src.bettergi_live_runner
    uv run python -m src.bettergi_live_runner --debug --filter StreamerAgent.BetterGILive
    $env:BETTERGI_LIVE_TOKEN = "<BetterGI 访问令牌>"   # PowerShell

退出：Ctrl+C。先断解说接收，再停发言队列、虚拟形象连接与事件总线。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.agents.streamer.bettergi_live import BetterGILiveBridge
from src.agents.streamer.config import BetterGILiveConfig
from src.agents.streamer.speech_dispatcher import SpeechDispatcher
from src.modules.avatar.lipsync.assembly import build_lipsync_analyzer
from src.modules.config.service import ConfigService
from src.modules.events import EventBus, ensure_registry_consistency, register_core_events
from src.modules.logging import configure_from_config, get_logger
from src.modules.subtitle import SubtitleService, build_subtitle_infrastructure
from src.modules.subtitle.playback_follower import bind_playback_subtitle
from src.modules.tools import ToolRegistry
from src.modules.tools.bootstrap import bind_core_tools
from src.modules.tts import TTSProvider, build_tts_infrastructure, derive_speech_config
from src.modules.utils.windows_dpi import setup_process_dpi_awareness

logger = get_logger("BetterGILiveRunner")

_BASE_DIR = Path(__file__).resolve().parents[1]


def _section(config: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    """沿键路径取配置子段；任一层缺失或非 dict 时返回空 dict。"""
    node: Any = config
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def bettergi_live_config(config: Dict[str, Any]) -> BetterGILiveConfig:
    """从 ``[agents.streamer.bettergi_live]`` 读接入参数。

    运行器以"进程被启动"为开关：该段的 ``enabled`` 只控制主程序内嵌接入
    （StreamerAgent），这里强制启用，避免独立运行器静默空转。
    """
    section = _section(config, "agents", "streamer", "bettergi_live")
    return BetterGILiveConfig.from_dict({**section, "enabled": True})


@dataclass
class LiveOutputStack:
    """运行器装配结果：构造完成、尚未启动。"""

    event_bus: EventBus
    subtitle_service: SubtitleService
    tts_engine: Optional[TTSProvider]
    tool_registry: ToolRegistry
    speech: SpeechDispatcher
    bridge: BetterGILiveBridge
    lipsync_analyzer: Optional[Any]
    subtitle_follows_playback: bool
    _providers_task: Optional[asyncio.Task] = field(default=None, init=False, repr=False)

    async def start(self) -> None:
        """启动出声道。

        虚拟形象连接放到后台任务：首次连接 VTube Studio 要等用户在授权窗口
        点击允许，这段时间语音与字幕不应停摆。代价是形象就绪前到达的解说
        只出声、不驱动表情。
        """
        await self.subtitle_service.start()
        logger.info("虚拟形象/演播 Provider 开始在后台连接（首次连接 VTube Studio 需在授权窗口点击允许）...")
        self._providers_task = asyncio.create_task(
            self.tool_registry.start_providers(), name="LiveOutputStack.providers"
        )
        self._providers_task.add_done_callback(_log_providers_result)
        await self.speech.start()
        await self.bridge.start()
        logger.info("BetterGI 解说运行器已就绪（Ctrl+C 退出）")

    async def stop(self) -> None:
        """按启动的逆序收尾；每一步失败只记日志，不阻断其余收尾。"""
        logger.info("正在停止 BetterGI 解说运行器...")
        await self.bridge.stop()
        await self.speech.stop()
        providers_task = self._providers_task
        self._providers_task = None
        if providers_task is not None and not providers_task.done():
            providers_task.cancel()
            await asyncio.gather(providers_task, return_exceptions=True)
        await self.tool_registry.stop_providers()
        await self.subtitle_service.stop()
        if self.tts_engine is not None:
            try:
                await self.tts_engine.cleanup()
            except Exception as exc:  # noqa: BLE001 - 停机路径不阻断其余收尾
                logger.warning(f"TTS 引擎收尾失败（已忽略）: {type(exc).__name__}: {exc}")
        await self.event_bus.cleanup()
        logger.info("BetterGI 解说运行器已停止")


def _log_providers_result(task: asyncio.Task) -> None:
    """后台 Provider 启动任务完成时记账（失败只降级虚拟形象，不影响出声）。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(f"虚拟形象/演播 Provider 启动异常: {type(exc).__name__}: {exc}")
        return
    report = task.result() or {}
    failed = [name for name, ok in report.items() if not ok]
    if failed:
        logger.warning(f"虚拟形象/演播 Provider 启动失败: {failed}（语音与字幕不受影响）")
    else:
        logger.info(
            f"虚拟形象/演播 Provider 启动完成: {list(report) or '无启用项'}（实际连接状态与自动重连见各 Provider 日志）"
        )


def build_output_stack(config: Dict[str, Any]) -> LiveOutputStack:
    """按配置构造出声道：TTS、字幕、口型、虚拟形象、发言管线与解说接收。

    降级语义（与主程序一致）：TTS 未启用/构造失败 → 只有字幕与业务事件；
    虚拟形象不可用 → 照常出声与显示字幕；口型构造失败 → 不渲染口型。
    任何一步失败都不阻断其余部分装配。
    """
    event_bus = EventBus()
    tts_section = _section(config, "tts")
    lipsync_analyzer = build_lipsync_analyzer(_section(config, "lipsync"))
    tts_engine = build_tts_infrastructure(
        tts_section,
        event_bus=event_bus,
        audio_sink=lipsync_analyzer,
        llm_service=None,
    )
    subtitle_service = build_subtitle_infrastructure(_section(config, "subtitle"))

    # 字幕源择流与主程序一致：TTS 在位时跟随播放事件，否则由发言管线直出
    subtitle_follows_playback = tts_engine is not None
    if subtitle_follows_playback:
        bind_playback_subtitle(event_bus, subtitle_service, logger)

    tool_registry = ToolRegistry(event_bus=event_bus)
    report = bind_core_tools(
        tool_registry,
        _section(config, "tools"),
        event_bus=event_bus,
        lipsync_analyzer=lipsync_analyzer,
        avatar_platform=_section(config, "platform"),
        subtitle_service=subtitle_service,
    )
    logger.info(f"虚拟形象/演播 Provider 装配报告: {report or '无启用项'}")

    speech = SpeechDispatcher(
        event_bus=event_bus,
        subtitle_service=None if subtitle_follows_playback else subtitle_service,
        tts_engine=tts_engine,
        speech_config=derive_speech_config(tts_section),
    )
    bridge = BetterGILiveBridge(bettergi_live_config(config), speech)
    return LiveOutputStack(
        event_bus=event_bus,
        subtitle_service=subtitle_service,
        tts_engine=tts_engine,
        tool_registry=tool_registry,
        speech=speech,
        bridge=bridge,
        lipsync_analyzer=lipsync_analyzer,
        subtitle_follows_playback=subtitle_follows_playback,
    )


# ---------------------------------------------------------------------------
# 命令行与日志
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="Amaidesu BetterGI 实时解说输出运行器（不启动主播 Agent、不调用 LLM）")
    parser.add_argument("--debug", action="store_true", help="启用 DEBUG 级别日志输出")
    parser.add_argument(
        "--filter",
        nargs="+",
        metavar="MODULE_NAME",
        help="仅显示指定模块的 INFO/DEBUG 级别日志（WARNING 及以上级别总是显示）",
    )
    parser.add_argument(
        "--dry",
        action="store_true",
        help="仅验证配置加载与出声道装配，不连接虚拟形象、不连接 BetterGI、不进入主循环",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=0.0,
        metavar="N",
        help="运行 N 秒后自动退出（0 = 一直运行，Ctrl+C 退出）",
    )
    return parser.parse_args()


def setup_logging(args: argparse.Namespace, logging_config: Optional[Dict[str, Any]] = None) -> None:
    """按配置文件与命令行参数配置日志（与主程序同语义）。"""
    final_config: Dict[str, Any] = {}
    if logging_config:
        final_config.update(logging_config)
    if args.debug:
        final_config["level"] = "DEBUG"
        final_config["console_level"] = "DEBUG"
    if args.filter:
        filtered_modules = set(args.filter)

        def filter_logic(record: Dict[str, Any]) -> bool:
            level_name = str(getattr(record.get("level"), "name", ""))
            if level_name in ("WARNING", "ERROR", "CRITICAL"):
                return True
            module_name = record.get("extra", {}).get("module")
            return bool(module_name and module_name in filtered_modules)

        final_config["filter"] = filter_logic
    configure_from_config(final_config)


def load_config() -> tuple[ConfigService, Dict[str, Any], bool]:
    """加载 config/ 七文件树；失败时记录原因并以非零码退出。"""
    config_service = ConfigService(base_dir=str(_BASE_DIR))
    try:
        config, was_created = config_service.initialize()
    except Exception as exc:  # noqa: BLE001 - 入口边界：配置加载失败直接退出
        logger.exception(f"配置文件初始化失败: {exc}")
        logger.error("请检查 config/ 目录下的配置文件格式，或删除 config/ 目录重新生成。")
        raise SystemExit(1) from exc
    return config_service, config, was_created


async def _run_until_stopped(stack: LiveOutputStack, *, run_seconds: float = 0.0) -> None:
    """启动出声道并等待退出信号；``run_seconds>0`` 时到点自动退出（冒烟用）。"""
    stop_event = asyncio.Event()

    def request_stop(signum: Any = None, frame: Any = None) -> None:
        stop_event.set()

    # 与主程序同款信号接法：Python 级信号处理器在 Windows 上同样能收到
    # Ctrl+C；SIGTERM 在部分环境不可注册时跳过，SIGBREAK 覆盖 taskkill 场景。
    originals: List[tuple[int, Any]] = []
    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            originals.append((sig, signal.signal(sig, request_stop)))
        except (ValueError, OSError):
            continue
    await stack.start()
    try:
        if run_seconds > 0:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=run_seconds)
        else:
            await stop_event.wait()
    finally:
        await stack.stop()
        for sig, original in originals:
            with contextlib.suppress(Exception):
                signal.signal(sig, original)


def main() -> int:
    """进程入口：加载配置 → 装配出声道 → 运行至 Ctrl+C。"""
    args = parse_args()
    setup_logging(args)
    # 字幕 Tk 窗口依赖进程级 DPI 声明，须在创建任何窗口前完成
    setup_process_dpi_awareness()

    config_service, config, was_created = load_config()
    setup_logging(args, config_service.get_section("logging", default={}))
    if was_created:
        logger.warning("配置文件已生成到 config/ 目录，请检查 TTS / 字幕 / 虚拟形象配置后重新运行。")
        return 0

    register_core_events()
    ensure_registry_consistency()
    logger.info("核心事件注册完成")

    stack = build_output_stack(config)
    logger.info(
        "出声道装配完成: "
        f"tts={'已启用' if stack.tts_engine is not None else '未启用（仅字幕/事件）'}, "
        f"字幕={'跟随播放事件' if stack.subtitle_follows_playback else '发言直出'}, "
        f"口型={'已分接' if stack.lipsync_analyzer is not None else '未启用'}, "
        f"解说地址={stack.bridge.url}"
    )
    if args.dry:
        logger.info("--dry 模式：出声道已构造，未连接虚拟形象与 BetterGI（验证 wiring 用）")
        return 0
    try:
        asyncio.run(_run_until_stopped(stack, run_seconds=args.seconds))
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，已退出")
    except asyncio.CancelledError:
        logger.info("运行被取消，已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
