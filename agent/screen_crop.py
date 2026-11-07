"""截帧内容区检测：WGC 画布上有窗口标题栏和大片底色，游戏只占中间一截。

实测（2026-11-04，_diag 里三张 966x760 的 WGC 原始帧）：
- 画布结构是 窗口标题栏(y 约 0..37) + 游戏画面 + 大片底色；底色 13~47，游戏自身深色 62~107；
- 标题栏和游戏都亮过底色，逐列最大亮度整幅都是 245 以上，所以按列取包围盒永远裁不到东西；
- 纯黑（level 0）占 67.6%，亮度中位数是 0，中位数不能当背景亮度；
- 游戏画面的左右边界和高度随场景变（标题画面只占中间一块、剧情画面铺满），
  按包围盒裁会把真实画面切掉（实测 _diag/final_*.jpg 两头都切进去过）。

所以只做一件事：裁掉上方的窗口标题栏，保留游戏画面的完整宽度与高度。

把这片底色送进策略模型的代价是实打实的：2B 会把它当成黑屏/等待，输出空按键组
（实测 _diag/t_keys_probe.py）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

#: 低于它一定不算内容/背景（纯黑）。
BLACK_LEVEL = 12
#: 底层亮度之上多少才算内容。
CONTENT_DELTA = 20
#: 一个亮度级至少占这么多像素才可能是底层（压掉 JPEG 噪点）。
BACKGROUND_MIN_PIXELS = 800
#: 相邻内容行间隔不超过这么多行就合并成同一条带。
BAND_MERGE_GAP = 3
#: 分带时阈值最多往上抬多少（越抬越容易把标题栏和游戏分开）。
BAND_MAX_LIFT = 120
BAND_LIFT_STEP = 5
#: 游戏画面至少要占画布这么高，否则分出来的可能是噪点。
BAND_MIN_HEIGHT_RATIO = 0.15
#: 最大块占画布这么高就认定已经分干净了，直接收工（实测稳定帧是 0.58）。
BAND_CLEAN_RATIO = 0.35
#: 内容带上方再留几行，避免游戏画面顶到边时被切掉头发/UI（实测会切掉主角的角）。
BAND_MARGIN = 8


def _gray(image: Any) -> Any:
    return image.convert("L")


def background_level(gray: Any, *, black_level: int = BLACK_LEVEL) -> int:
    """背景亮度 = 近黑之外最暗的那个直方图峰。

    三张原始帧近黑之外的峰是 28 / 46 / 13，游戏自身深色总比底层亮一截：游戏恒画在
    很暗的底上。换成众数会被游戏主色调骗走（某帧众数是游戏褐色 87）。
    """
    hist = gray.histogram()
    for value in range(max(0, black_level) + 1, len(hist)):
        if hist[value] >= BACKGROUND_MIN_PIXELS:
            return value
    return 0


def threshold_for(image: Any, *, black_level: int = BLACK_LEVEL,
                  delta: int = CONTENT_DELTA) -> int:
    """内容阈值：底层亮度往上抬 delta，至少 black_level。"""
    gray = _gray(image)
    return max(black_level, background_level(gray, black_level=black_level) + delta)


def _runs(gray: Any, threshold: int) -> list:
    """逐行扫描，返回所有连续"有内容"的行区间 [(top, bottom), ...]。"""
    runs = []
    start: Optional[int] = None
    for y in range(gray.height):
        line = gray.crop((0, y, gray.width, y + 1))
        has = line.point(lambda v: 255 if v > threshold else 0).getbbox() is not None
        if has and start is None:
            start = y
        elif not has and start is not None:
            runs.append((start, y))
            start = None
    if start is not None:
        runs.append((start, gray.height))
    return runs


def _merged(runs: list) -> list:
    """把相邻的带合并：暗场景里主角的深色轮廓会把游戏画面切成几段。"""
    merged = []
    for run in runs:
        if merged and run[0] - merged[-1][1] <= BAND_MERGE_GAP:
            merged[-1] = (merged[-1][0], run[1])
        else:
            merged.append(run)
    return merged


def band_top(image: Any, *, black_level: int = BLACK_LEVEL,
            delta: int = CONTENT_DELTA, max_lift: int = BAND_MAX_LIFT) -> int:
    """游戏画面开始的那一行（标题栏的下沿）；分不出来返回 0（表示别动）。

    阈值逐级抬高直到整幅断成两条以上的带，再取最高的那块——高度判据保证挑到的是
    游戏画面（几百行）而不是标题栏（三十几行）。
    """
    gray = _gray(image)
    base = threshold_for(image, black_level=black_level, delta=delta)
    # 记下第一个成功的候选，但不立刻返回：底图与游戏画面常常只差 1~2 个亮度级，
    # 第一级抬升会把画面和底切成四五段（实测 +5 时 4 段、+20 时干净的两段）。
    # 先比"最大块占画布的比例"，明显更大的才换人，避免把画面切成两块反而变小。
    best: Optional[Tuple[int, int]] = None
    best_ratio = gray.height * BAND_MIN_HEIGHT_RATIO
    for lift in range(0, max_lift + 1, BAND_LIFT_STEP):
        runs = _runs(gray, base + lift)
        if len(runs) < 2:
            continue
        merged = _merged(runs)
        top, bottom = max(merged, key=lambda run: run[1] - run[0])
        if bottom - top > best_ratio:
            best = (top, bottom)
            best_ratio = bottom - top
        if len(merged) >= 2 and bottom - top >= gray.height * BAND_CLEAN_RATIO:
            return max(0, top - BAND_MARGIN)
    return max(0, best[0] - BAND_MARGIN) if best else 0


def crop_content(image: Any, *, black_level: int = BLACK_LEVEL,
                 delta: int = CONTENT_DELTA) -> Dict[str, Any]:
    """裁掉上方的窗口标题栏，宽度和底部原样保留。

    返回 {image, cropped, top, size}；分不出标题栏时原样返回。
    """
    top = band_top(image, black_level=black_level, delta=delta)
    if top <= 0 or top >= image.height:
        return {"image": image, "cropped": False, "top": 0,
                "size": (image.width, image.height)}
    cropped = image.crop((0, top, image.width, image.height))
    return {"image": cropped, "cropped": True, "top": top,
            "size": (cropped.width, cropped.height)}


def is_blank(image: Any, *, black_level: int = BLACK_LEVEL) -> bool:
    """整幅都没有内容行带——抓帧成功但游戏没画面时用它判断。"""
    gray = _gray(image)
    return not _runs(gray, threshold_for(image, black_level=black_level))
