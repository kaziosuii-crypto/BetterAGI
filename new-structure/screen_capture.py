"""Windows 高性能屏幕与窗口截帧模块。

支持：
1. 全屏桌面高速截帧 (GDI BitBlt)
2. 指定窗口截帧 (精确/模糊匹配窗口标题)
3. 自动高 DPI 适配与等比缩放
4. 纯内存转换，不产生磁盘 IO
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
import io
from typing import List, Optional, Tuple
from PIL import Image

u32 = ctypes.windll.user32
g32 = ctypes.windll.gdi32

# 明确 64 位环境下的句柄签名，防止指针截断
u32.GetDC.argtypes = [wt.HWND]
u32.GetDC.restype = wt.HDC
u32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
u32.ReleaseDC.restype = ctypes.c_int

g32.CreateCompatibleDC.argtypes = [wt.HDC]
g32.CreateCompatibleDC.restype = wt.HDC
g32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
g32.CreateCompatibleBitmap.restype = wt.HBITMAP
g32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
g32.SelectObject.restype = wt.HGDIOBJ
g32.BitBlt.argtypes = [
    wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD
]
g32.BitBlt.restype = wt.BOOL
g32.DeleteObject.argtypes = [wt.HGDIOBJ]
g32.DeleteObject.restype = wt.BOOL
g32.DeleteDC.argtypes = [wt.HDC]
g32.DeleteDC.restype = wt.BOOL
g32.GetDIBits.argtypes = [
    wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT,
    ctypes.c_void_p, ctypes.c_void_p, wt.UINT
]
g32.GetDIBits.restype = ctypes.c_int


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wt.DWORD * 3)
    ]


def _init_dpi():
    try:
        u32.SetProcessDPIAware()
    except Exception:
        pass


_init_dpi()


def find_window(title_keyword: str) -> Optional[int]:
    """根据窗口标题关键词查找可见顶层窗口句柄。"""
    if not title_keyword:
        return None
    
    found_hwnd = None
    keyword = title_keyword.lower()

    def enum_proc(hwnd, lParam):
        nonlocal found_hwnd
        if u32.IsWindowVisible(hwnd):
            length = u32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                u32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                if keyword in title.lower():
                    found_hwnd = hwnd
                    return False
        return True

    cb = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(enum_proc)
    u32.EnumWindows(cb, 0)
    return found_hwnd


def list_visible_windows() -> List[Tuple[int, str]]:
    """列出当前屏幕上可见的主要有标题窗口。"""
    windows = []

    def enum_proc(hwnd, lParam):
        if u32.IsWindowVisible(hwnd):
            length = u32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                u32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.strip()
                if title:
                    windows.append((int(hwnd), title))
        return True

    cb = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(enum_proc)
    u32.EnumWindows(cb, 0)
    return windows


def capture_screen(hwnd: Optional[int] = None, fit: Tuple[int, int] = (1280, 720), quality: int = 80) -> Tuple[Image.Image, bytes]:
    """截取屏幕或指定窗口画面。
    
    返回: (PIL.Image 对象, JPEG 字节流)
    """
    if hwnd and u32.IsWindow(hwnd) and u32.IsWindowVisible(hwnd):
        rect = wt.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(rect))
        x = rect.left
        y = rect.top
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            hwnd = None
    else:
        hwnd = None

    if hwnd is None:
        # 全屏桌面截取
        x = u32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        y = u32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
        w = u32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        h = u32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        if w <= 0 or h <= 0:
            x, y = 0, 0
            w = u32.GetSystemMetrics(0)  # SM_CXSCREEN
            h = u32.GetSystemMetrics(1)  # SM_CYSCREEN

    hdc_screen = u32.GetDC(0)
    memdc = g32.CreateCompatibleDC(hdc_screen)
    hbmp = g32.CreateCompatibleBitmap(hdc_screen, w, h)
    old_bmp = g32.SelectObject(memdc, hbmp)

    # 0x00CC0020 = SRCCOPY
    g32.BitBlt(memdc, 0, 0, w, h, hdc_screen, x, y, 0x00CC0020)

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # top-down DIB
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0

    buf = (ctypes.c_char * (w * h * 4))()
    g32.GetDIBits(memdc, hbmp, 0, h, ctypes.byref(buf), ctypes.byref(bmi), 0)

    # 释放 GDI 句柄
    g32.SelectObject(memdc, old_bmp)
    g32.DeleteObject(hbmp)
    g32.DeleteDC(memdc)
    u32.ReleaseDC(0, hdc_screen)

    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")

    # 缩放到合适大小供大模型观察
    if fit:
        img.thumbnail(fit, Image.Resampling.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=int(quality), optimize=True)
    jpeg_bytes = out.getvalue()

    return img, jpeg_bytes
