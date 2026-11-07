using OpenCvSharp;

namespace Fischless.GameCapture.BitBlt;

/// <summary>将 GDI 像素复制到由 OpenCV 管理生命周期的 Mat。</summary>
public static class BitBltMat
{
    /// <summary>复制每行像素，允许子 Mat 独立于截图会话存活。</summary>
    public static unsafe Mat CopyFrom(IntPtr source, int width, int height, int sourceStride)
    {
        var result = new Mat(height, width, MatType.CV_8UC3);
        try
        {
            var rowBytes = width * 3;
            var targetStride = result.Step();
            for (var i = 0; i < height; i++)
            {
                Buffer.MemoryCopy((byte*)source + sourceStride * i,
                    (byte*)result.Data + targetStride * i, rowBytes, rowBytes);
            }
            return result;
        }
        catch
        {
            result.Dispose();
            throw;
        }
    }
}
