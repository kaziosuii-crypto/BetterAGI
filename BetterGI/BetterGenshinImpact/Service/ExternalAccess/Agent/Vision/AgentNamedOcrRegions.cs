using BetterGenshinImpact.Core.Recognition.OCR.Paddle;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Models;
using System;
using System.Collections.Generic;
using System.Threading;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Vision;

/// <summary>校验并处理同一原始截图上的具名 OCR 区域。</summary>
internal static class AgentNamedOcrRegions
{
    /// <summary>限制每次观察的 OCR 推理次数。</summary>
    internal const int MaxRegions = 8;

    /// <summary>在截图前校验并复制区域，避免无效请求启动识别。</summary>
    internal static List<(string Name, AgentRoiDto Roi)> Prepare(AgentObserveRequest request)
    {
        var regions = request.OcrRegions!;
        if (request.OcrRoi is not null || regions.Count is 0 or > MaxRegions)
            throw AgentException.InvalidRequest($"命名 OCR 区域必须为 1～{MaxRegions} 个，且不能与 OcrRoi 同用。");

        var result = new List<(string Name, AgentRoiDto Roi)>(regions.Count);
        var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var region in regions)
        {
            if (region is null || string.IsNullOrWhiteSpace(region.Name) || region.Name.Length > 64
                || region.Name != region.Name.Trim() || !names.Add(region.Name))
                throw AgentException.InvalidRequest("OCR 区域名称必须唯一、非空且不超过 64 个字符。");
            var roi = region.Roi;
            if (roi?.X is not int x || roi.Y is not int y || roi.Width is not int width || roi.Height is not int height
                || x < 0 || y < 0 || width <= 0 || height <= 0)
                throw AgentException.InvalidRequest("每个 OCR 区域必须明确提供有效的 x、y、width 和 height。");
            var copy = new AgentRoiDto { X = x, Y = y, Width = width, Height = height };
            foreach (var existing in result)
            {
                var other = existing.Roi;
                if ((long)x < (long)other.X + other.Width && (long)other.X < (long)x + width
                    && (long)y < (long)other.Y + other.Height && (long)other.Y < (long)y + height)
                    throw AgentException.InvalidRequest("命名 OCR 区域不能重复或重叠。");
            }
            result.Add((region.Name, copy));
        }
        return result;
    }

    /// <summary>取得原帧尺寸后严格校验边界，不裁切、扩展或接受整帧 OCR。</summary>
    internal static void ValidateBounds(IReadOnlyList<(string Name, AgentRoiDto Roi)> regions, int width, int height)
    {
        foreach (var (_, roi) in regions)
        {
            if ((long)roi.X + roi.Width > width || (long)roi.Y + roi.Height > height
                || (roi.X == 0 && roi.Y == 0 && roi.Width == width && roi.Height == height))
                throw AgentException.InvalidRequest("命名 OCR 区域超出原始画面或覆盖整帧。");
        }
    }

    /// <summary>逐区域读取原始 OCR 结果；一个区域失败不影响其他区域。</summary>
    internal static List<AgentNamedOcrRegionResult> Analyze(
        IReadOnlyList<(string Name, AgentRoiDto Roi)> regions, int frameWidth, int frameHeight,
        string frameId, Func<AgentRoiDto, PaddleOcrObservation> observe, CancellationToken cancellationToken = default)
    {
        var results = new List<AgentNamedOcrRegionResult>(regions.Count);
        foreach (var (name, roi) in regions)
        {
            cancellationToken.ThrowIfCancellationRequested();
            try
            {
                var observation = observe(roi);
                var candidates = observation.IsCompleted ? BuildCandidates(observation, roi, frameWidth, frameHeight) : null;
                results.Add(new AgentNamedOcrRegionResult
                {
                    Name = name, FrameId = frameId, Roi = roi, Status = observation.Status.ToString(),
                    Completed = observation.IsCompleted, RawDetectionCount = observation.RawDetectionCount,
                    Candidates = candidates, ErrorType = observation.ErrorType, ErrorMessage = observation.ErrorMessage
                });
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception ex)
            {
                results.Add(new AgentNamedOcrRegionResult
                {
                    Name = name, FrameId = frameId, Roi = roi, Status = "Unavailable", Completed = false,
                    ErrorType = ex.GetType().Name, ErrorMessage = ex.Message
                });
            }
        }
        return results;
    }

    /// <summary>将区域内的四角检测框换算到原帧，保留所有非空候选文本。</summary>
    private static List<AgentOcrRegionDto> BuildCandidates(PaddleOcrObservation observation,
        AgentRoiDto roi, int frameWidth, int frameHeight)
    {
        var candidates = new List<AgentOcrRegionDto>();
        foreach (var item in observation.Regions)
        {
            if (string.IsNullOrWhiteSpace(item.Text)) continue;
            var points = new[] { item.TopLeft, item.TopRight, item.BottomRight, item.BottomLeft };
            double left = roi.Width, top = roi.Height, right = 0, bottom = 0;
            foreach (var point in points)
            {
                if (!float.IsFinite(point.X) || !float.IsFinite(point.Y))
                    throw new InvalidOperationException("OCR 返回了无效的检测框。");
                left = Math.Min(left, point.X); top = Math.Min(top, point.Y);
                right = Math.Max(right, point.X); bottom = Math.Max(bottom, point.Y);
            }
            var x = (int)Math.Floor(Math.Clamp(left, 0, roi.Width)) + roi.X;
            var y = (int)Math.Floor(Math.Clamp(top, 0, roi.Height)) + roi.Y;
            var rightPixel = (int)Math.Ceiling(Math.Clamp(right, 0, roi.Width)) + roi.X;
            var bottomPixel = (int)Math.Ceiling(Math.Clamp(bottom, 0, roi.Height)) + roi.Y;
            if (rightPixel <= x || bottomPixel <= y)
                throw new InvalidOperationException("OCR 返回了空检测框。");
            var width = rightPixel - x;
            var height = bottomPixel - y;
            candidates.Add(new AgentOcrRegionDto
            {
                Text = item.Text, X = x, Y = y, Width = width, Height = height,
                // 仅透传可序列化的原始分数，不用默认值代替未知值。
                RecognitionScore = item.RecognitionScore is float recognitionScore && float.IsFinite(recognitionScore)
                    ? recognitionScore : null,
                DetectionScore = float.IsFinite(item.DetectionScore) ? item.DetectionScore : null,
                NormalizedX = (x + width / 2d) / frameWidth,
                NormalizedY = (y + height / 2d) / frameHeight,
                NormalizedWidth = width / (double)frameWidth,
                NormalizedHeight = height / (double)frameHeight
            });
        }
        return candidates;
    }
}
