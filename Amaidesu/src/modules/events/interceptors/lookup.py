"""
拦截器共用的 payload 字段查找

直播间行为流 payload（``RoomMessagePayload``）的发送者信息在嵌套的
``user.id`` 下，而其他 payload 形状可能把用户标识放在顶层。查找顺序：
先嵌套结构，再顶层候选键，都取不到回退到固定占位符——让限流/相似
过滤按真实用户分桶，而不是全部落进同一个占位桶。
"""

from typing import Any, Dict

# 顶层 user_id 候选键（兼容非房间消息的 payload 形状）
_USER_ID_KEYS = ("user_id", "open_id", "uid", "sender_id")
# 顶层 text 候选键
_TEXT_KEYS = ("text", "content", "msg", "message")

# 取不到用户标识时的占位桶名（所有无主消息共享同一限流配额）
UNKNOWN_USER = "unknown_user"


def extract_user_id(payload: Dict[str, Any]) -> str:
    """
    从 payload 中提取用户标识

    Args:
        payload: 事件数据（``model_dump()`` 后的 dict）

    Returns:
        用户标识字符串；取不到时返回 ``UNKNOWN_USER``
    """
    user = payload.get("user")
    if isinstance(user, dict):
        value = user.get("id")
        if value is not None and value != "":
            return str(value)
    for key in _USER_ID_KEYS:
        value = payload.get(key)
        if value is not None and value != "":
            return str(value)
    return UNKNOWN_USER


def extract_text(payload: Dict[str, Any]) -> str:
    """
    从 payload 中提取文本内容（限流日志预览与相似过滤共用）

    Args:
        payload: 事件数据（``model_dump()`` 后的 dict）

    Returns:
        文本字符串；取不到时返回空串
    """
    for key in _TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str):
            return value
    # 嵌套 user.name 兜底（如纯进房事件无 content，用昵称做预览）
    user = payload.get("user")
    if isinstance(user, dict):
        name = user.get("name", "")
        if name:
            return f"[{name}]"
    return ""


__all__ = ["UNKNOWN_USER", "extract_text", "extract_user_id"]
