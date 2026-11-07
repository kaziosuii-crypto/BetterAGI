"""Windows Graphics Capture (WGC) 抓帧：抓「窗口自己渲染的内容」，不看屏幕、不怕被遮挡。

为什么走这条路（2026-11-04 实测结论，别改回去）
-----------------------------------------------
同一时刻对同一个 UNDERTALE 窗口实测三条路：

1. PrintWindow(hwnd, dc, PW_CLIENTONLY|PW_RENDERFULLCONTENT)：641x480 画布里真实内容
   只占右下 457x327，左 184px、上下各 89px 全黑；把窗口挪到 (0,0) 黑边一样 → 与窗口
   位置无关，是 DWM 表层尺寸和窗口尺寸不一致。
2. GetDC(hwnd) + BitBlt 客户区：纯白（游戏是 GPU 渲染，GDI DC 上没有像素）。
3. 屏幕 BitBlt 裁窗口矩形：窗口被 DSH 盖住时拿到的就是**别人的画面**（实测：聊天界面）。
4. WGC（本模块）：直连窗口的 DWM 合成表层，**966x760 真实内容、无黑边、被遮挡也能抓**。

所以默认抓帧走 WGC；PrintWindow 只当 WGC 不可用（老系统/跨会话）时的降级。

实现要点：ctypes 调 COM/WinRT。**所有 vtable 槽位都是从元数据和 SDK 头文件数出来的，
不是猜的**（盲扫槽位会把 0xC0000409 直接打进进程）：

  WinRT IInspectable 派生接口 = QI/AddRef/Release/GetIids/GetRuntimeClassName/GetTrustLevel
  占 0..5，业务方法从 6 开始；属性 getter 就是普通方法，retval 是**最后一个参数**。
  IGraphicsCaptureItem          79C3F95B-31F7-4EC2-A464-632EF5D30760  Size=get(7)
  IGraphicsCaptureItemInterop   3628E81B-3CAC-4C60-B7F4-23CE0E0C3356  CreateForWindow(3)
  FramePoolStatics2             589B103F-6BBC-5DF5-A991-02E28B3B66D5  CreateFreeThreaded(6)
  IDirect3D11CaptureFramePool   24EB6D22-1975-422E-82E7-780DBD8DDF24  TryGetNextFrame(7) CreateCaptureSession(10)
  IDirect3D11CaptureFrame       FA50C623-38DA-4B32-ACF3-FA9734AD800E  Surface=get(6) ContentSize=get(8)
  IGraphicsCaptureSession       814E42A9-F70F-4AD7-939B-FDDCC6EB880D  StartCapture(6)

  ID3D11DeviceContext 方法从槽位 7 起（d3d11.h 顺序数出）：Map=14, CopySubresourceRegion=46,
  CopyResource=47；ID3D11Device::CreateTexture2D=6；ID3D11DeviceChild::GetDevice=3。
  IDirect3DDxgiInterfaceAccess  A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1  GetInterface(3)

D3D11CreateDevice 参数也有硬约束：本机只有 (driver=1 ENUMERATED, flags=0, level=11_0) 这一组
能成功，别的组合返回 0x887a002d / 0x80070057。

线程模型：FramePool 用 CreateFreeThreaded，抓帧线程不需要消息泵；如果 StartCapture 之后
拿不到帧，会在超时后明确抛错，不静默返回黑帧。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time
import uuid
from typing import Any, Dict, NamedTuple, Optional, Tuple

# ---------------------------------------------------------------------------
# 常量 / IID
# ---------------------------------------------------------------------------
IID_GRAPHICS_CAPTURE_ITEM = "79C3F95B-31F7-4EC2-A464-632EF5D30760"
IID_GRAPHICS_CAPTURE_ITEM_INTEROP = "3628E81B-3CAC-4C60-B7F4-23CE0E0C3356"
IID_FRAME_POOL_STATICS2 = "589B103F-6BBC-5DF5-A991-02E28B3B66D5"
IID_DXGI_INTERFACE_ACCESS = "A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1"
IID_DXGIDEVICE = "54EC77FA-1377-44E6-8C32-88FD5F44C84C"
IID_D3D11_TEXTURE2D = "DC8E63F3-D12B-4952-B47B-5E45026A862D"

DXGI_FORMAT_B8G8R8A8_UNORM = 87
D3D11_USAGE_STAGING = 3
D3D11_CPU_ACCESS_READ = 0x20000

# vtable 槽位（见模块文档；改之前先回 d3d11.h / Windows.Graphics.Capture.idl 数一遍）
SLOT_ITEM_SIZE = 7
SLOT_INTEROP_CREATE_FOR_WINDOW = 3
SLOT_POOL_STATICS_CREATE_FREE_THREADED = 6
SLOT_POOL_TRY_GET_NEXT_FRAME = 7
SLOT_POOL_CREATE_CAPTURE_SESSION = 10
SLOT_SESSION_START_CAPTURE = 6
SLOT_FRAME_SURFACE = 6
SLOT_FRAME_CONTENT_SIZE = 8
# ID3D11Device 方法从槽位 3 起（IUnknown 占 0..2）。CreateTexture2D 是 d3d11.h 里
# 设备方法的第 2 个（CreateBuffer 第 0 个），所以真实槽位 = 3 + 2 = 5。
# 实测槽位 6 会稳定返回 E_INVALIDARG(0x80070057)。
SLOT_DEVICE_CHILD_GET_DEVICE = 3
SLOT_DEVICE_CREATE_TEXTURE2D = 5
SLOT_CONTEXT_MAP = 14
SLOT_CONTEXT_COPY_RESOURCE = 47
# ID3D11Texture2D::GetDesc = IUnknown(3) + DeviceChild(4) + Resource(3) = 10
SLOT_TEXTURE2D_GET_DESC = 10

HRESULT = ctypes.c_long


class WgcError(RuntimeError):
    pass


class WgcFrame(NamedTuple):
    """一帧 BGRA 像素（内存里，不落盘）。"""

    width: int
    height: int
    row_pitch: int
    data: bytes
    captured_at: float

    def bgra(self) -> bytes:
        if self.row_pitch == self.width * 4:
            return self.data
        stride = self.width * 4
        buf = bytearray(stride * self.height)
        for y in range(self.height):
            src = y * self.row_pitch
            buf[y * stride:(y + 1) * stride] = self.data[src:src + stride]
        return bytes(buf)


# ---------------------------------------------------------------------------
# Win32 / COM 结构体
# ---------------------------------------------------------------------------
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wt.DWORD),
        ("Data2", wt.WORD),
        ("Data3", wt.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class SizeInt32(ctypes.Structure):
    _fields_ = [("Width", ctypes.c_int32), ("Height", ctypes.c_int32)]


class D3D11Texture2DDesc(ctypes.Structure):
    _fields_ = [
        ("Width", wt.UINT),
        ("Height", wt.UINT),
        ("MipLevels", wt.UINT),
        ("ArraySize", wt.UINT),
        ("Format", wt.UINT),
        ("SampleCount", wt.UINT),
        ("SampleQuality", wt.UINT),
        ("Usage", wt.UINT),
        ("BindFlags", wt.UINT),
        ("CPUAccessFlags", wt.UINT),
        ("MiscFlags", wt.UINT),
    ]


class D3D11MappedSubresource(ctypes.Structure):
    _fields_ = [("pData", ctypes.c_void_p), ("RowPitch", wt.UINT), ("DepthPitch", wt.UINT)]


def _guid(text: str) -> GUID:
    out = GUID()
    ctypes.memmove(ctypes.byref(out), uuid.UUID(text).bytes_le, 16)
    return out


def _check(hr: int, what: str) -> int:
    if hr < 0:
        raise WgcError("%s 失败: HRESULT 0x%08X" % (what, hr & 0xFFFFFFFF))
    return hr


def _call(ptr: Any, slot: int, restype: Any, argtypes: list, *args: Any) -> Any:
    """按 vtable 槽位调方法。

    坑（2026-11-04 实测）：this 必须作为 **c_void_p 对象**传给 WINFUNCTYPE 包装后的函数。
    把指针拆成 Python int 再传，ctypes 在 64 位下会按 c_int 转换，指针被截断成 32 位，
    表现为「明明同一个对象，CreateTexture2D 却返回 E_INVALIDARG(0x80070057)」。
    """
    handle = ptr if isinstance(ptr, ctypes.c_void_p) else ctypes.c_void_p(int(ptr))
    table = ctypes.cast(handle, ctypes.POINTER(ctypes.c_void_p))[0]
    fn_table = ctypes.cast(ctypes.c_void_p(table), ctypes.POINTER(ctypes.c_void_p))
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(fn_table[slot])
    return fn(handle, *args)


def _release(ptr: Optional[ctypes.c_void_p]) -> None:
    if ptr is not None and ptr.value:
        try:
            _call(ptr, 2, ctypes.c_ulong, [], )
        except Exception:  # noqa: BLE001 - 释放失败不该掩盖主流程异常
            pass


class _WinRt:
    """combase / d3d11 / user32 的懒加载单例，argtypes 一次设好（不设会崩进程）。"""

    _lock = threading.Lock()
    _ready = False
    combase: Any = None
    d3d11: Any = None
    user32: Any = None

    @classmethod
    def ensure(cls) -> "_WinRt":
        with cls._lock:
            if cls._ready:
                return cls
            try:
                cls.combase = ctypes.WinDLL("combase")
                cls.d3d11 = ctypes.WinDLL("d3d11")
                cls.user32 = ctypes.WinDLL("user32")
            except OSError as exc:  # pragma: no cover - 非 Windows
                raise WgcError("加载 combase/d3d11 失败: %s" % exc) from exc

            combase = cls.combase
            for name, argtypes in (
                ("RoInitialize", [ctypes.c_int]),
                ("WindowsCreateString", [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]),
                ("RoGetActivationFactory", [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]),
                ("WindowsDeleteString", [ctypes.c_void_p]),
            ):
                fn = getattr(combase, name)
                fn.argtypes = argtypes
                fn.restype = HRESULT

            d3d11 = cls.d3d11
            d3d11.D3D11CreateDevice.restype = HRESULT
            d3d11.D3D11CreateDevice.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint),
                ctypes.POINTER(ctypes.c_void_p),
            ]
            d3d11.CreateDirect3D11DeviceFromDXGIDevice.restype = HRESULT
            d3d11.CreateDirect3D11DeviceFromDXGIDevice.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
            ]

            # 房间已初始化过（S_FALSE）不是错误
            hr = combase.RoInitialize(1)
            if hr < 0:
                raise WgcError("RoInitialize 失败: 0x%08X" % (hr & 0xFFFFFFFF))
            cls._ready = True
            return cls

    @classmethod
    def activation_factory(cls, runtime_class: str, iid: str) -> ctypes.c_void_p:
        cls.ensure()
        combase = cls.combase
        hstr = ctypes.c_void_p()
        _check(combase.WindowsCreateString(runtime_class, len(runtime_class), ctypes.byref(hstr)), "WindowsCreateString")
        try:
            out = ctypes.c_void_p()
            ident = _guid(iid)
            _check(
                combase.RoGetActivationFactory(hstr, ctypes.byref(ident), ctypes.byref(out)),
                "RoGetActivationFactory(%s)" % runtime_class,
            )
            return out
        finally:
            combase.WindowsDeleteString(hstr)


def available() -> bool:
    """WGC 能不能用（系统有 combase + d3d11 就当可用，真正的错误在抓帧时暴露）。"""
    try:
        _WinRt.ensure()
        return True
    except Exception:  # noqa: BLE001
        return False


def item_size(hwnd: int) -> Tuple[int, int]:
    """窗口真实内容尺寸（WGC 报的，不是 GetClientRect）。"""
    item = create_item(hwnd)
    try:
        size = SizeInt32()
        _check(_call(item, SLOT_ITEM_SIZE, HRESULT, [ctypes.POINTER(SizeInt32)], ctypes.byref(size)), "IGraphicsCaptureItem::Size")
        return int(size.Width), int(size.Height)
    finally:
        _release(item)


def create_item(hwnd: int) -> ctypes.c_void_p:
    interop = _WinRt.activation_factory(
        "Windows.Graphics.Capture.GraphicsCaptureItem", IID_GRAPHICS_CAPTURE_ITEM_INTEROP
    )
    item = ctypes.c_void_p()
    ident = _guid(IID_GRAPHICS_CAPTURE_ITEM)
    try:
        _check(
            _call(interop, SLOT_INTEROP_CREATE_FOR_WINDOW, HRESULT,
                  [wt.HWND, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                  wt.HWND(hwnd), ctypes.byref(ident), ctypes.byref(item)),
            "IGraphicsCaptureItemInterop::CreateForWindow",
        )
    finally:
        _release(interop)
    if not item.value:
        raise WgcError("CreateForWindow(%d) 返回空 item" % hwnd)
    return item


class _Capture:
    """一次抓帧会话要持有的所有 COM 对象。用完 close()。"""

    def __init__(self, hwnd: int):
        _WinRt.ensure()
        self.hwnd = hwnd
        self.device = ctypes.c_void_p()
        self.context = ctypes.c_void_p()
        self.rt_device = ctypes.c_void_p()
        self.item = ctypes.c_void_p()
        self.pool = ctypes.c_void_p()
        self.session = ctypes.c_void_p()
        self.size = (0, 0)
        self._open()

    def _open(self) -> None:
        d3d11 = _WinRt.d3d11
        self.item = create_item(self.hwnd)
        size = SizeInt32()
        _check(_call(self.item, SLOT_ITEM_SIZE, HRESULT, [ctypes.POINTER(SizeInt32)], ctypes.byref(size)), "Size")
        if size.Width <= 0 or size.Height <= 0:
            raise WgcError("窗口 %d 内容尺寸非法: %sx%s" % (self.hwnd, size.Width, size.Height))
        self.size = (int(size.Width), int(size.Height))

        feature = ctypes.c_uint(0)
        _check(
            d3d11.D3D11CreateDevice(
                None, 1, None, 0, None, 0, 7,
                ctypes.byref(self.device), ctypes.byref(feature), ctypes.byref(self.context),
            ),
            "D3D11CreateDevice",
        )

        dxgi = ctypes.c_void_p()
        ident = _guid(IID_DXGIDEVICE)
        _check(
            _call(self.device, 0, HRESULT, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                  ctypes.byref(ident), ctypes.byref(dxgi)),
            "ID3D11Device::QueryInterface(IDXGIDevice)",
        )
        try:
            _check(
                d3d11.CreateDirect3D11DeviceFromDXGIDevice(dxgi, ctypes.byref(self.rt_device)),
                "CreateDirect3D11DeviceFromDXGIDevice",
            )
        finally:
            _release(dxgi)

        statics = _WinRt.activation_factory(
            "Windows.Graphics.Capture.Direct3D11CaptureFramePool", IID_FRAME_POOL_STATICS2
        )
        try:
            _check(
                _call(statics, SLOT_POOL_STATICS_CREATE_FREE_THREADED, HRESULT,
                      [ctypes.c_void_p, wt.UINT, ctypes.c_int32, SizeInt32, ctypes.POINTER(ctypes.c_void_p)],
                      self.rt_device, DXGI_FORMAT_B8G8R8A8_UNORM, 2, size, ctypes.byref(self.pool)),
                "Direct3D11CaptureFramePool::CreateFreeThreaded",
            )
        finally:
            _release(statics)
        _check(
            _call(self.pool, SLOT_POOL_CREATE_CAPTURE_SESSION, HRESULT,
                  [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], self.item, ctypes.byref(self.session)),
            "CreateCaptureSession",
        )
        _check(_call(self.session, SLOT_SESSION_START_CAPTURE, HRESULT, []), "StartCapture")

    def grab(self, timeout: float = 3.0) -> WgcFrame:
        deadline = time.time() + max(0.1, timeout)
        frame = ctypes.c_void_p()
        while True:
            hr = _call(self.pool, SLOT_POOL_TRY_GET_NEXT_FRAME, HRESULT, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(frame))
            if hr == 0 and frame.value:
                break
            if time.time() > deadline:
                raise WgcError("WGC %.1fs 内没拿到帧（窗口可能已关闭或最小化）" % timeout)
            time.sleep(0.015)
        try:
            return self._read_frame(frame)
        finally:
            _release(frame)

    def _read_frame(self, frame: ctypes.c_void_p) -> WgcFrame:
        surface = ctypes.c_void_p()
        _check(_call(frame, SLOT_FRAME_SURFACE, HRESULT, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(surface)), "Frame::Surface")
        content = SizeInt32()
        _call(frame, SLOT_FRAME_CONTENT_SIZE, HRESULT, [ctypes.POINTER(SizeInt32)], ctypes.byref(content))
        width = int(content.Width) or self.size[0]
        height = int(content.Height) or self.size[1]

        access = ctypes.c_void_p()
        ident = _guid(IID_DXGI_INTERFACE_ACCESS)
        try:
            _check(
                _call(surface, 0, HRESULT, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                      ctypes.byref(ident), ctypes.byref(access)),
                "Surface::QueryInterface(IDirect3DDxgiInterfaceAccess)",
            )
        finally:
            _release(surface)

        texture = ctypes.c_void_p()
        tex_iid = _guid(IID_D3D11_TEXTURE2D)
        try:
            _check(
                _call(access, 3, HRESULT, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                      ctypes.byref(tex_iid), ctypes.byref(texture)),
                "IDirect3DDxgiInterfaceAccess::GetInterface(ID3D11Texture2D)",
            )
        finally:
            _release(access)

        staging = ctypes.c_void_p()
        try:
            # staging 纹理不带任何 MiscFlags（实测这个组合可创建、可拷贝；
            # 不要为了"对齐源纹理的共享标志"去读 GetDesc——那一步没验证过槽位，会把 tex 读坏）。
            desc = D3D11Texture2DDesc(width, height, 1, 1, DXGI_FORMAT_B8G8R8A8_UNORM, 1, 0,
                                      D3D11_USAGE_STAGING, 0, D3D11_CPU_ACCESS_READ, 0)
            _check(
                _call(self.device, SLOT_DEVICE_CREATE_TEXTURE2D, HRESULT,
                      [ctypes.POINTER(D3D11Texture2DDesc), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                      ctypes.byref(desc), None, ctypes.byref(staging)),
                "ID3D11Device::CreateTexture2D(staging)",
            )
            _check(
                _call(self.context, SLOT_CONTEXT_COPY_RESOURCE, HRESULT,
                      [ctypes.c_void_p, ctypes.c_void_p], staging, texture),
                "ID3D11DeviceContext::CopyResource",
            )
            mapped = D3D11MappedSubresource()
            _check(
                _call(self.context, SLOT_CONTEXT_MAP, HRESULT,
                      [ctypes.c_void_p, wt.UINT, wt.UINT, wt.UINT, ctypes.POINTER(D3D11MappedSubresource)],
                      staging, 0, 1, 0, ctypes.byref(mapped)),
                "ID3D11DeviceContext::Map",
            )
            try:
                data = ctypes.string_at(mapped.pData, int(mapped.RowPitch) * height)
                return WgcFrame(width, height, int(mapped.RowPitch), data, time.time())
            finally:
                _call(self.context, 15, HRESULT, [ctypes.c_void_p, wt.UINT], staging, 0)  # Unmap=15
        finally:
            _release(staging)
            _release(texture)

    def close(self) -> None:
        for name in ("session", "pool", "rt_device", "context", "device", "item"):
            ptr = getattr(self, name, None)
            _release(ptr)
            setattr(self, name, ctypes.c_void_p())


class WgcCapture:
    """可复用的抓帧器：进程内保持 device/pool/session，每帧只取新帧。

    调用方需要在窗口换过（句柄变了）或连续失败时 rebuild()。
    """

    def __init__(self, hwnd: int):
        self.hwnd = int(hwnd)
        self._capture: Optional[_Capture] = None
        self._lock = threading.Lock()
        self.frames = 0

    @property
    def size(self) -> Tuple[int, int]:
        if self._capture is None:
            return (0, 0)
        return self._capture.size

    def rebuild(self) -> None:
        with self._lock:
            if self._capture is not None:
                self._capture.close()
                self._capture = None
            self._capture = _Capture(self.hwnd)

    def grab(self, timeout: float = 3.0) -> WgcFrame:
        with self._lock:
            if self._capture is None:
                self._capture = _Capture(self.hwnd)
            try:
                frame = self._capture.grab(timeout)
            except WgcError:
                # 会话失效（窗口重建/设备丢失）就重建一次再试
                self._capture.close()
                self._capture = _Capture(self.hwnd)
                frame = self._capture.grab(timeout)
            self.frames += 1
            return frame

    def close(self) -> None:
        with self._lock:
            if self._capture is not None:
                self._capture.close()
                self._capture = None


def grab_frame(hwnd: int, timeout: float = 3.0) -> WgcFrame:
    """一次性抓一帧（不复用会话，慢但简单）。"""
    cap = _Capture(hwnd)
    try:
        return cap.grab(timeout)
    finally:
        cap.close()


def find_window_by_title(title: str) -> int:
    """按标题精确找顶层窗口（找不到返回 0）。"""
    user32 = _WinRt.ensure().user32
    found = []

    callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _cb(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value == title:
            found.append(int(hwnd))
            return False
        return True

    user32.EnumWindows(callback_type(_cb), 0)
    return found[0] if found else 0
