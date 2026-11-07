"""口型分析器装配入口

口型分析器是虚拟形象共享件（一个实例扇出多具形象），调参在 avatar.toml
顶层 ``[lipsync]`` 段（拍平合并视图里的键为 ``lipsync``）。装配入口集中
处理"关闭 / 配置非法 / 构造失败"的降级：任何失败都按"不用口型"处理并
返回 ``None``，口型是装饰性路径，不阻断启动，也不影响语音与字幕。
"""

from __future__ import annotations

from typing import Any, Optional

from src.modules.logging import get_logger

from .analyzer import LipSyncAnalyzer


def build_lipsync_analyzer(lipsync_config: Any) -> Optional[LipSyncAnalyzer]:
    """按 ``[lipsync]`` 段构造口型分析器；关闭或构造失败返回 ``None``。

    Args:
        lipsync_config: avatar.toml 顶层 ``[lipsync]`` 段字典；缺失 / 非字典
            视为未配置（按默认值构造，与配置物化的默认一致）。

    Returns:
        分析器实例；``enabled=False``、配置非法或构造异常时为 ``None``。
    """
    logger = get_logger("LipSyncAssembly")
    section = lipsync_config if isinstance(lipsync_config, dict) else {}
    if not bool(section.get("enabled", True)):
        logger.info("口型分析已关闭（[lipsync].enabled=false），口型不分接")
        return None
    try:
        analyzer = LipSyncAnalyzer(config=dict(section))
    except Exception as exc:  # noqa: BLE001 - 装饰性能力装配边界
        logger.warning(f"口型分析器构造失败（口型不分接）: {type(exc).__name__}: {exc}")
        return None
    logger.info("口型分析器已构造（[avatar.lipsync]）")
    return analyzer


__all__ = ["build_lipsync_analyzer"]
