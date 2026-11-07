using System.Diagnostics;
using System.Runtime.InteropServices;
using OpenCvSharp;
using Vanara.PInvoke;

namespace Fischless.GameCapture.BitBlt;

public class BitBltSession : IDisposable
{
    // 窗口句柄
    private readonly HWND _hWnd;

    private readonly object _lockObject = new();

    // 位图句柄
    private Gdi32.SafeHBITMAP _hBitmap;

    // 位图数据指针，这个指针会在位图释放时自动释放
    private IntPtr _bitsPtr;

    // 位图数据一行字节数
    private readonly int _stride;

    // 缓冲区 CompatibleDC
    private Gdi32.SafeHDC _hdcDest;

    // 来源DC
    private User32.SafeReleaseHDC _hdcSrc;

    // 旧位图，析构时一起释放掉
    private HGDIOBJ _oldBitmap;

    // 会话资源只释放一次，销毁后不再执行截图。
    private bool _disposed;

    // 窗口原宽高
    public int Width { get; }
    public int Height { get; }

    /// <summary>
    ///     不是所有的失效情况都能被检测到
    /// </summary>
    /// <returns></returns>
    public bool Invalid => _hWnd.IsNull || _hdcSrc.IsInvalid || _hdcDest.IsInvalid || _hBitmap.IsInvalid || _bitsPtr == 0;

    public BitBltSession(HWND hWnd, int w, int h)
    {
        if (hWnd.IsNull) throw new Exception("hWnd is invalid");

        _hWnd = hWnd;

        if (w <= 0 || h <= 0) throw new Exception("Invalid width or height");

        // 这俩仅做标记
        Width = w;
        Height = h;

        lock (_lockObject)
        {
            try
            {
                _hdcSrc = User32.GetDC(_hWnd);
                if (_hdcSrc.IsInvalid) throw new Exception($"Failed to get DC for {_hWnd}");

                var hdcRasterCaps = Gdi32.GetDeviceCaps(_hdcSrc, Gdi32.DeviceCap.RASTERCAPS);
                if ((hdcRasterCaps | 1) == 0) // RC_BITBLT
                    // 设备不支持 BitBlt
                    throw new Exception("BitBlt not supported");

                var hdcSrcPixel = Gdi32.GetDeviceCaps(_hdcSrc, Gdi32.DeviceCap.BITSPIXEL);
                // 颜色位数
                if (hdcSrcPixel != 32 && hdcSrcPixel != 24)
                    // 目前只考虑支持24/32位像素格式
                    throw new Exception("BitBlt only support 24 or 32 bit pixel color");

                var hdcSrcPlanes = Gdi32.GetDeviceCaps(_hdcSrc, Gdi32.DeviceCap.PLANES);
                // 颜色平面数
                if (hdcSrcPlanes > 1)
                    // 意味着色深不是标准色深
                    throw new Exception("BitBlt only support 1 plane");

                var hdcClip = Gdi32.GetDeviceCaps(_hdcSrc, Gdi32.DeviceCap.CLIPCAPS);
                if (hdcClip == 0)
                    // 设备不支持剪切出单一窗口
                    throw new Exception("Device does not support clipping");

                _hdcDest = Gdi32.CreateCompatibleDC(_hdcSrc);
                if (_hdcDest.IsInvalid)
                {
                    Debug.Fail("Failed to create CompatibleDC");
                    throw new Exception($"Failed to create CompatibleDC for {_hWnd}");
                }

                var bmi = new Gdi32.BITMAPINFO
                {
                    bmiHeader = new Gdi32.BITMAPINFOHEADER
                    {
                        biSize = (uint)Marshal.SizeOf<Gdi32.BITMAPINFOHEADER>(),
                        biWidth = Width,
                        biHeight = -Height, // Top-down image
                        biPlanes = 1,
                        biBitCount = 24,
                        biCompression = Gdi32.BitmapCompressionMode.BI_RGB, // 内存里是BGR
                        biSizeImage = 0
                    }
                };
                _hBitmap = Gdi32.CreateDIBSection(_hdcDest, bmi, Gdi32.DIBColorMode.DIB_RGB_COLORS, out _bitsPtr,
                    IntPtr.Zero);

                if (_hBitmap.IsInvalid || _bitsPtr == 0)
                {
                    if (!_hBitmap.IsInvalid) Gdi32.DeleteObject(_hBitmap);

                    throw new Exception($"Failed to create dIB section for {_hdcDest}");
                }

                var bitmap = Gdi32.GetObject<Gdi32.BITMAP>(_hBitmap);
                if (bitmap.bmPlanes != 1 || bitmap.bmBitsPixel != 24)
                    throw new Exception("Unsupported bitmap format");

                _stride = bitmap.bmWidthBytes;

                _oldBitmap = Gdi32.SelectObject(_hdcDest, _hBitmap);
                if (_oldBitmap.IsNull) throw new Exception("Failed to select object");
            }
            catch (Exception)
            {
                ReleaseResources();
                throw;
            }
            finally
            {
                Gdi32.GdiFlush();
            }
        }
    }

    public void Dispose()
    {
        lock (_lockObject)
        {
            if (_disposed) return;
            _disposed = true;
            ReleaseResources();
        }
        GC.SuppressFinalize(this);
    }

    /// <summary>
    ///     调用GDI复制到缓冲区并返回新Mat
    /// </summary>
    public Mat? GetImage()
    {
        lock (_lockObject)
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            // 截图
            var success = Gdi32.BitBlt(_hdcDest, 0, 0, Width, Height,
                _hdcSrc, 0, 0, Gdi32.RasterOperationMode.SRCCOPY);
            if (!success || !Gdi32.GdiFlush()) return null;

            // OpenCV 所有的缓冲区可由派生区域继续引用，避免会话释放后悬空。
            return BitBltMat.CopyFrom(_bitsPtr, Width, Height, _stride);
        }
    }

    /// <summary>
    ///     需加锁环境下访问，释放资源
    /// </summary>
    private void ReleaseResources()
    {
        Gdi32.GdiFlush();

        if (!_oldBitmap.IsNull)
        {
            // 先选回资源再释放_hBitmap
            Gdi32.SelectObject(_hdcDest, _oldBitmap);
            _oldBitmap = Gdi32.SafeHBITMAP.Null;
        }


        if (!_hBitmap.IsNull)
        {
            Gdi32.DeleteObject(_hBitmap);
            _hBitmap = Gdi32.SafeHBITMAP.Null;
        }


        if (!_hdcDest.IsNull)
        {
            Gdi32.DeleteDC(_hdcDest);
            _hdcDest = Gdi32.SafeHDC.Null;
        }

        if (_hdcSrc != IntPtr.Zero)
        {
            User32.ReleaseDC(_hWnd, _hdcSrc);
            _hdcSrc = User32.SafeReleaseHDC.Null;
        }

        _bitsPtr = IntPtr.Zero;
    }
}
