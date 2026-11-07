"""游戏文字截图渲染器：把 UT 的汉字提示栅格化成训练用图片。

为什么需要：gamescaling 靠 agent 历史截图学键位。但 Undertale 原版字体里
没有汉字，中文汉化在游戏内会退化成 AAA 方块——历史里全是无信息画面，
策略模型就学不到「按哪个键能推进」。这里把同样的提示词用可读字体画出来，
作为历史帧交给训练，让「画面 -> 动作」的对应关系是真的。

约束：本模块只在组离线训练数据时使用（写历史集），不参与实时推理。
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

FONT_DIR = Path(__file__).resolve().parent / "templates" / "fonts"
#: 开源可商用字体（SIL OFL）——系统里没有中文字体也能渲染。
DEFAULT_FONT = FONT_DIR / "ZCOOLKuaiLe-Regular.ttf"
#: UT 纯文本框的尺寸（游戏原生 640x480）。
FRAME_SIZE = (640, 480)


class TextFrameError(RuntimeError):
    """没有可用字体，或者字体渲染失败。"""


def font_path() -> Path:
    """优先用随仓库带的字体，其次退回系统里的中文字体。"""

    if DEFAULT_FONT.exists():
        return DEFAULT_FONT
    for candidate in (
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ):
        if os.path.exists(candidate):
            return Path(candidate)
    raise TextFrameError("找不到可渲染中文的字体：%s 不存在，系统字体也没有" % DEFAULT_FONT)


def render_text(
    lines: Sequence[str],
    path: Optional[Path] = None,
    *,
    size: Tuple[int, int] = FRAME_SIZE,
    font_size: int = 28,
    bg: str = "#000000",
    fg: str = "#ffffff",
) -> bytes:
    """把几行提示画成 JPEG 字节；path 给了就顺便落盘（诊断用）。"""

    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", size, bg)
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(font_path()), font_size)
    line_height = int(font_size * 1.5)
    total = line_height * max(1, len(lines))
    y = max(8, (size[1] - total) // 2)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        x = max(8, (size[0] - (bbox[2] - bbox[0])) // 2)
        draw.text((x, y), line, font=font, fill=fg)
        y += line_height
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88)
    data = buffer.getvalue()
    if path is not None:
        Path(path).write_bytes(data)
    return data


def history_pairs(dialogues: Iterable[Tuple[Sequence[str], Sequence[str]]], target_dir: Path) -> List[dict]:
    """把「画面文字 + 该按的键」成对渲染成训练历史，返回每对的路径与元数据。"""

    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    made: List[dict] = []
    for index, (lines, keys) in enumerate(dialogues):
        path = target_dir / ("hist_%03d.jpg" % index)
        render_text(list(lines), path)
        made.append({"path": str(path), "lines": list(lines), "keys": list(keys)})
    return made
