# -*- coding: utf-8 -*-
'''画面冻结贴图：慢系统超预算时，观众屏幕上那块「大肥鱼CPU烧了」。

设计 01 §6 / 03 §C.9 的 P1 视觉部分。编排层只负责发事件与提醒（orchestrator），
这里负责把提醒变成真正的一张 JPEG：解码 -> 半透明黑底条 + 居中白字 -> 编码回 JPEG。

约束：
- 只用 Pillow，不依赖任何 GUI（渲染要在直播线程之外也能跑）。
- 画不出来就显式抛 FreezeOverlayError（字体缺失绝不静默画空图）；
  但 FreezePainter.paint() 是直播热路径，任何异常都只记账并原样返回入参字节。
'''
from __future__ import annotations

import io
import os
from typing import Any, Callable, Dict, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

#: 默认文案：用户指定的「大肥鱼CPU烧了」，不要在别处再抄一遍字面量。
DEFAULT_WARN_TEXT = '大肥鱼CPU烧了'

#: 中文字体候选，按优先级排列（Windows 自带；msyh.ttc = 微软雅黑）。
FONT_CANDIDATES: Tuple[str, ...] = (
    'C:/Windows/Fonts/msyh.ttc',
    'C:/Windows/Fonts/msyhbd.ttc',
    'C:/Windows/Fonts/simhei.ttf',
)

#: 输出 JPEG 质量：与录播/推流的帧质量一致，不额外放大体积。
JPEG_QUALITY = 85

_MIN_FONT_SIZE = 10
_MAX_FONT_SIZE = 120


class FreezeOverlayError(RuntimeError):
    '''贴图渲染失败：字体缺失、入参不是 JPEG、尺寸/参数非法。'''


def find_cjk_font() -> str:
    '''返回第一个真实存在的中文字体路径；一个都没有就明确失败。'''

    for path in FONT_CANDIDATES:
        if os.path.isfile(path):
            return path
    raise FreezeOverlayError(
        '找不到可用的中文字体（试过 %s）：无法渲染冻结贴图，'
        '请安装微软雅黑/黑体或把字体放到上述路径之一'
        % ', '.join(FONT_CANDIDATES)
    )


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    path = find_cjk_font()
    try:
        return ImageFont.truetype(path, size)
    except Exception as error:  # noqa: BLE001 - 字体坏了也要给出中文明确的错
        raise FreezeOverlayError('中文字体加载失败（%s）：%s' % (path, error)) from error


def _coerce_size(font_size: Optional[int], height: int) -> int:
    if font_size is None:
        size = int(height * 0.12)
    else:
        try:
            size = int(font_size)
        except (TypeError, ValueError) as error:
            raise FreezeOverlayError('font_size 必须是正整数：%r' % (font_size,)) from error
        if size <= 0:
            raise FreezeOverlayError('font_size 必须是正整数：%r' % (font_size,))
    return max(_MIN_FONT_SIZE, min(_MAX_FONT_SIZE, size))


def _fit_font(label: str, width: int, size: int) -> Tuple[ImageFont.FreeTypeFont, Any, int]:
    '''按可用宽度收字号，返回 (字体, 文字包围盒, 最终字号)。'''

    margin = max(8, width // 40)
    max_width = max(1, width - 2 * margin)
    draw = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    while True:
        font = _load_font(size)
        bbox = draw.textbbox((0, 0), label, font=font)
        if bbox[2] - bbox[0] <= max_width or size <= _MIN_FONT_SIZE:
            return font, bbox, size
        size = max(_MIN_FONT_SIZE, int(size * 0.88))


def render_overlay(jpeg: bytes, text: str = DEFAULT_WARN_TEXT, *,
                   opacity: int = 180, font_size: Optional[int] = None) -> bytes:
    '''在一帧 JPEG 中央画半透明黑底条 + 居中中文，返回新 JPEG（尺寸不变）。

    失败一律抛 FreezeOverlayError：字体缺失、入参不是可解码的 JPEG、
    opacity/font_size 非法。不会静默返回原图或空白图。
    '''

    if not isinstance(jpeg, (bytes, bytearray)) or not jpeg:
        raise FreezeOverlayError('冻结贴图需要非空 JPEG 字节，收到 %r' % (type(jpeg).__name__,))
    try:
        source = Image.open(io.BytesIO(bytes(jpeg)))
        source.load()
    except Exception as error:  # noqa: BLE001 - 解不开就是坏帧
        raise FreezeOverlayError('冻结贴图的入参解不成 JPEG：%s' % error) from error
    if (source.format or '').upper() != 'JPEG':
        raise FreezeOverlayError('冻结贴图只处理 JPEG，收到 %s' % (source.format,))

    try:
        alpha = int(opacity)
    except (TypeError, ValueError) as error:
        raise FreezeOverlayError('opacity 必须是 0-255 的整数：%r' % (opacity,)) from error
    alpha = max(0, min(255, alpha))

    image = source.convert('RGB')
    width, height = image.size
    if width <= 0 or height <= 0:
        raise FreezeOverlayError('冻结贴图收到空画面：%dx%d' % (width, height))

    label = str(text if text is not None else DEFAULT_WARN_TEXT).strip() or DEFAULT_WARN_TEXT
    size = _coerce_size(font_size, height)
    font, bbox, size = _fit_font(label, width, size)
    text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]

    # 黑底条高度跟着字号走，但不许超出画面。
    band_height = min(height, max(size + 16, int(size * 1.9)))
    top = (height - band_height) // 2
    # 整幅透明层 + 条带矩形：alpha_composite 要求两层同尺寸。
    layer = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rectangle([0, top, width, top + band_height], fill=(0, 0, 0, alpha))

    x = (width - text_width) // 2 - bbox[0]
    y = top + (band_height - text_height) // 2 - bbox[1]
    # 白色文字 + 深色描边：亮背景（白天场景）上也能读清。
    draw.text((x, y), label, font=font, fill=(255, 255, 255, 255),
              stroke_width=max(1, size // 16), stroke_fill=(0, 0, 0, 220))

    composed = Image.alpha_composite(image.convert('RGBA'), layer)
    out = io.BytesIO()
    composed.convert('RGB').save(out, format='JPEG', quality=JPEG_QUALITY)
    return out.getvalue()


class FreezePainter:
    '''直播侧的贴图器：paint() 永不抛出，失败就原样放行这一帧。

    用法（run_stream.assemble 里已接好）::

        painter = FreezePainter(text='大肥鱼CPU烧了', tap=writer.write)
        shown = painter.paint(jpeg)      # 渲染后字节；失败时 == jpeg
        painter.stats()                  # {'painted': 1, 'errors': 0, 'last_error': ''}
    '''

    def __init__(self, text: str = DEFAULT_WARN_TEXT,
                 tap: Optional[Callable[[bytes], Any]] = None) -> None:
        self.text = str(text if text is not None else DEFAULT_WARN_TEXT).strip() or DEFAULT_WARN_TEXT
        self.tap = tap
        self._painted = 0
        self._errors = 0
        self._last_error = ''

    def paint(self, jpeg: bytes) -> bytes:
        '''渲染一帧；draw 失败只记 errors 并原样返回入参字节，绝不带崩直播。'''

        raw = bytes(jpeg) if isinstance(jpeg, (bytes, bytearray)) else b''
        try:
            if not raw:
                raise FreezeOverlayError('冻结贴图收到空帧')
            painted = render_overlay(raw, self.text)
        except Exception as error:  # noqa: BLE001 - 热路径：任何异常都不许外泄
            self._errors += 1
            self._last_error = '%s: %s' % (type(error).__name__, error)
            return raw

        self._painted += 1
        if callable(self.tap):
            try:
                self.tap(painted)
            except Exception as error:  # noqa: BLE001 - 下游记账失败不改画面
                self._errors += 1
                self._last_error = 'tap %s: %s' % (type(error).__name__, error)
        return painted

    def stats(self) -> Dict[str, Any]:
        return {
            'painted': self._painted,
            'errors': self._errors,
            'last_error': self._last_error,
        }
