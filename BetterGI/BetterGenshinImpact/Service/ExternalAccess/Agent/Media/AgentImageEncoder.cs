using OpenCvSharp;
using System;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Media;

/// <summary>
/// 把 OpenCV 画面编码为 PNG 或 JPEG，并按需等比缩小。
/// </summary>
public static class AgentImageEncoder
{
    /// <summary>
    /// 编码一帧图像。
    /// </summary>
    /// <param name="image">原始画面。</param>
    /// <param name="maxWidth">最大宽度；0 表示保持原尺寸，超过 1920 时按 1920 处理。</param>
    /// <param name="format">png 或 jpg。</param>
    /// <param name="quality">JPEG 质量，范围 1～100。</param>
    /// <returns>编码后的字节、MIME 类型与实际宽高。</returns>
    public static AgentEncodedImage Encode(Mat image, int maxWidth, string format, int quality)
    {
        ArgumentNullException.ThrowIfNull(image);

        var normalizedFormat = string.IsNullOrWhiteSpace(format) ? "png" : format.Trim().ToLowerInvariant();
        if (normalizedFormat is not ("png" or "jpg" or "jpeg"))
        {
            throw AgentException.InvalidRequest("图像格式只能是 png 或 jpg。", format);
        }

        var effectiveMaxWidth = maxWidth <= 0 ? 0 : Math.Min(maxWidth, AgentLimits.MaxImageWidth);
        Mat? resized = null;
        try
        {
            var target = image;
            if (effectiveMaxWidth > 0 && image.Width > effectiveMaxWidth)
            {
                var targetHeight = (int)Math.Round(image.Height * (effectiveMaxWidth / (double)image.Width));
                resized = new Mat();
                Cv2.Resize(image, resized, new OpenCvSharp.Size(effectiveMaxWidth, Math.Max(1, targetHeight)), 0, 0,
                    InterpolationFlags.Area);
                target = resized;
            }

            var extension = normalizedFormat == "png" ? ".png" : ".jpg";
            byte[] bytes;
            if (normalizedFormat == "png")
            {
                Cv2.ImEncode(extension, target, out var pngBytes);
                bytes = pngBytes;
            }
            else
            {
                var safeQuality = Math.Clamp(quality, 1, 100);
                Cv2.ImEncode(extension, target, out var jpegBytes,
                    new ImageEncodingParam(ImwriteFlags.JpegQuality, safeQuality));
                bytes = jpegBytes;
            }

            if (bytes == null || bytes.Length == 0)
            {
                throw new AgentException("IMAGE_ENCODE_FAILED", "图像编码失败。", System.Net.HttpStatusCode.InternalServerError);
            }

            return new AgentEncodedImage
            {
                Bytes = bytes,
                MimeType = normalizedFormat == "png" ? "image/png" : "image/jpeg",
                Width = target.Width,
                Height = target.Height,
                Extension = normalizedFormat == "png" ? "png" : "jpg"
            };
        }
        finally
        {
            resized?.Dispose();
        }
    }
}

/// <summary>
/// 编码后的图像及其元数据。
/// </summary>
public sealed class AgentEncodedImage
{
    /// <summary>编码后的字节。</summary>
    public required byte[] Bytes { get; init; }

    /// <summary>MIME 类型。</summary>
    public required string MimeType { get; init; }

    /// <summary>编码后宽度。</summary>
    public required int Width { get; init; }

    /// <summary>编码后高度。</summary>
    public required int Height { get; init; }

    /// <summary>文件扩展名，不含点。</summary>
    public required string Extension { get; init; }
}
