"""VLM 坐标到虚拟桌面物理像素坐标的三步反算纯函数。

坐标参考系（与屏幕采集线一致）：
- VLM 返回的坐标基于"发送给它那张图"——即 ``region`` 裁剪区域按
  ``sent_width`` 等比缩放后的图像，原点在该图左上角；
- ``region`` 为 (left, top, width, height)，相对**所选显示器**左上角，
  全链路物理像素；
- ``monitor_origin`` 是该显示器左上角在虚拟桌面坐标系中的绝对位置。
"""


def vlm_xy_to_screen(
    vlm_xy: tuple[int, int],
    sent_width: int,
    region: tuple[int, int, int, int],
    monitor_origin: tuple[int, int],
) -> tuple[int, int] | None:
    """把 VLM 图内坐标反算为虚拟桌面物理像素坐标。

    三步反算：
    1. 按 ``sent_width / region_width`` 反除缩放，把 VLM 坐标还原为
       region 内坐标——参考系是**发送给 VLM 的实际宽度**（调用方传给
       视觉工具的 max_width，或未缩放时的 region 宽度），不是工具返回
       的 width；
    2. 加 region 左上偏移（相对显示器）；
    3. 加 monitor_origin（显示器在虚拟桌面的原点）。

    结果落在该显示器矩形 [origin, origin+size) 之外时返回 None，
    调用方应据此拒绝点击。

    Args:
        vlm_xy: VLM 返回的图内 (x, y) 坐标。
        sent_width: 发送给 VLM 的图像实际宽度（物理像素）。
        region: 采集区域 (left, top, width, height)，相对显示器左上角。
        monitor_origin: 显示器左上角在虚拟桌面中的 (x, y) 绝对位置。

    Returns:
        虚拟桌面物理像素坐标 ``(abs_x, abs_y)``；越界时 ``None``。
    """
    region_left, region_top, region_width, region_height = region
    scale = region_width / sent_width
    abs_x = round(vlm_xy[0] * scale) + region_left + monitor_origin[0]
    abs_y = round(vlm_xy[1] * scale) + region_top + monitor_origin[1]

    mon_x, mon_y = monitor_origin
    inside_x = mon_x <= abs_x < mon_x + region_width
    inside_y = mon_y <= abs_y < mon_y + region_height
    if not (inside_x and inside_y):
        return None
    return (abs_x, abs_y)
