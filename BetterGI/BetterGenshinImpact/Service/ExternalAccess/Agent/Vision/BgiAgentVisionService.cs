using BetterGenshinImpact.Core.Recognition;
using BetterGenshinImpact.Core.Recognition.OCR;
using BetterGenshinImpact.Core.Recognition.OCR.Paddle;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Live;
using BetterGenshinImpact.Core.Recognition.OpenCv;
using BetterGenshinImpact.GameTask;
using BetterGenshinImpact.GameTask.Common.BgiVision;
using BetterGenshinImpact.GameTask.Model.Area;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Media;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Models;
using OpenCvSharp;
using System;
using System.Collections.Generic;
using System.Diagnostics;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Vision;

/// <summary>
/// 基于 BetterGI 现有 Bv 识别体系的视觉分析服务。
/// <para>
/// 所有结果都来自截图上的模板匹配与 PaddleOCR；一次观察只构建一棵区域树，
/// 并在返回前释放全部非托管图像资源。
/// </para>
/// </summary>
public sealed class BgiAgentVisionService : IAgentVisionService
{
    /// <inheritdoc />
    public AgentVisionDto Analyze(AgentFrame frame, bool includeUiState, bool includeOcr, AgentRoiDto? ocrRoi)
    {
        ArgumentNullException.ThrowIfNull(frame);
        var stopwatch = Stopwatch.StartNew();

        // CaptureContent 接管传入的 Mat，并在区域树释放时一并释放。
        using var region = new CaptureContent(frame.Image.Clone(), 0, 0, frame.SystemInfo).CaptureRectArea;
        var category = Bv.WhichGameUi(region);
        // 地图缩放标志的遮罩模板可能命中派蒙菜单左侧图标，必须有独立的地图关闭按钮佐证。
        bool? mapCloseDetected = category == GameUiCategory.BigMap ? Bv.IsInAnyClosableUi(region) : null;
        category = ResolveAgentCategory(category, mapCloseDetected == true);

        bool? isInMainUi = null;
        bool? isInDomain = null;
        bool? isInAnyClosableUi = null;
        bool? isInGameReadyUi = null;
        bool? isInPartyViewUi = null;
        bool? isInBigMapUi = null;
        bool? isInTalkUi = null;
        if (includeUiState)
        {
            isInMainUi = Bv.IsInMainUi(region);
            isInDomain = Bv.IsInDomain(region);
            isInAnyClosableUi = mapCloseDetected ?? Bv.IsInAnyClosableUi(region);
            isInGameReadyUi = Bv.IsInGameReadyUi(region);
            isInPartyViewUi = Bv.IsInPartyViewUi(region);
            isInBigMapUi = category == GameUiCategory.BigMap;
            isInTalkUi = Bv.IsInTalkUi(region);
        }

        List<AgentOcrRegionDto>? ocr = null;
        if (includeOcr)
        {
            ocr = RunOcr(region, frame, ocrRoi);
        }

        stopwatch.Stop();
        return new AgentVisionDto
        {
            UiCategory = category.ToString(),
            IsInMainUi = isInMainUi,
            IsInDomain = isInDomain,
            IsInAnyClosableUi = isInAnyClosableUi,
            IsInGameReadyUi = isInGameReadyUi,
            IsInPartyViewUi = isInPartyViewUi,
            IsInBigMapUi = isInBigMapUi,
            IsInTalkUi = isInTalkUi,
            Ocr = ocr,
            ElapsedMs = stopwatch.ElapsedMilliseconds
        };
    }

    /// <summary>地图候选缺少关闭按钮佐证时，不允许将它视作可执行的地图界面。</summary>
    internal static GameUiCategory ResolveAgentCategory(GameUiCategory category, bool mapCloseDetected) =>
        category == GameUiCategory.BigMap && !mapCloseDetected ? GameUiCategory.Unknown : category;

    /// <inheritdoc />
    public PaddleOcrObservation ObserveOcrRegion(AgentFrame frame, AgentRoiDto roi)
    {
        ArgumentNullException.ThrowIfNull(frame);
        return ObserveOcrRegion(frame.Image, roi, ObservePaddle);
    }

    /// <summary>使用原始观测 API；异常或不支持均明确报告不可用。</summary>
    private static PaddleOcrObservation ObservePaddle(Mat image)
    {
        try
        {
            return OcrFactory.Paddle is PaddleOcrService paddle
                ? paddle.Observe(image)
                : PaddleOcrObservation.Unavailable("当前 OCR 服务不支持原始观测。");
        }
        catch (Exception ex)
        {
            return PaddleOcrObservation.Failure(PaddleOcrObservationStatus.Unavailable, null, ex);
        }
    }

    /// <summary>在原帧像素坐标裁剪区域；无效区域不执行 OCR。</summary>
    internal static PaddleOcrObservation ObserveOcrRegion(Mat frame, AgentRoiDto roi,
        Func<Mat, PaddleOcrObservation> observe)
    {
        ArgumentNullException.ThrowIfNull(roi);
        if (roi.X < 0 || roi.Y < 0 || roi.Width <= 0 || roi.Height <= 0
            || (long)roi.X + roi.Width > frame.Width || (long)roi.Y + roi.Height > frame.Height)
            throw AgentException.InvalidRequest("OCR 关注区域为空或超出画面。");
        using var image = new Mat(frame, new Rect(roi.X, roi.Y, roi.Width, roi.Height));
        try { return observe(image); }
        catch (Exception ex)
        {
            return PaddleOcrObservation.Failure(PaddleOcrObservationStatus.Unavailable, null, ex);
        }
    }

    /// <summary>分别检查同一原始帧上的消息和选项区域，不将无 OCR 结果解释为无字。</summary>
    public LiveDialogueObservationDto AnalyzeDialogue(AgentFrame frame, LiveDialogueCalibrationDto calibration)
    {
        ArgumentNullException.ThrowIfNull(frame);
        var copy = LiveDialogueCalibration.Copy(calibration) ?? throw new ArgumentNullException(nameof(calibration));
        return new LiveDialogueObservationDto
        {
            Message = AnalyzeObservedDialogueRegion(frame.Image, copy.Message!, ObservePaddle),
            Options = AnalyzeObservedDialogueRegion(frame.Image, copy.Options!, ObservePaddle)
        };
    }

    /// <summary>检测器零框只表示未检出；低置信、失败或缺失完整性均不能形成候选。</summary>
    internal static LiveDialogueRegionObservationDto AnalyzeObservedDialogueRegion(
        Mat frame, LiveDialogueRegionDto region, Func<Mat, PaddleOcrObservation> observe)
    {
        PaddleOcrObservation? observation = null;
        var structure = AnalyzeDialogueRegion(frame, region, image =>
        {
            observation = observe(image);
            return observation?.RawDetectionCount ?? 0;
        });
        var completed = observation?.IsCompleted == true && observation.DropScore is float score && float.IsFinite(score);
        return new LiveDialogueRegionObservationDto
        {
            State = completed ? structure.State : "uncertain",
            OcrCompleted = completed,
            OcrStatus = observation?.Status.ToString() ?? "unavailable",
            RawDetectionCount = observation?.RawDetectionCount,
            LowConfidenceCount = completed ? observation!.LowConfidenceCount : null,
            FilteredOcrRegions = completed ? observation!.Regions.Count - (observation.LowConfidenceCount ?? 0) : 0,
            OcrDropScore = completed ? observation!.DropScore : null,
            Candidates = completed ? CopyDialogueCandidates(frame, region, observation!) : null,
            GrayRange = structure.GrayRange,
            EdgeFraction = structure.EdgeFraction
        };
    }

    /// <summary>保留所有原始框及置信度；无效几何或分数使整组候选未知。</summary>
    private static IReadOnlyList<LiveDialogueOcrCandidateDto>? CopyDialogueCandidates(
        Mat frame, LiveDialogueRegionDto region, PaddleOcrObservation observation)
    {
        var left = (int)Math.Floor(region.X * frame.Width);
        var top = (int)Math.Floor(region.Y * frame.Height);
        var right = (int)Math.Ceiling((region.X + region.Width) * frame.Width);
        var bottom = (int)Math.Ceiling((region.Y + region.Height) * frame.Height);
        var result = new List<LiveDialogueOcrCandidateDto>(observation.Regions.Count);
        foreach (var item in observation.Regions)
        {
            if (!float.IsFinite(item.DetectionScore)
                || (item.RecognitionScore is float score && !float.IsFinite(score))) return null;
            var corners = new[] { item.TopLeft, item.TopRight, item.BottomRight, item.BottomLeft };
            var points = new List<LiveDialoguePointDto>(corners.Length);
            foreach (var corner in corners)
            {
                if (!float.IsFinite(corner.X) || !float.IsFinite(corner.Y)
                    || corner.X < 0 || corner.Y < 0 || corner.X > right - left || corner.Y > bottom - top)
                    return null;
                points.Add(new LiveDialoguePointDto { X = corner.X + left, Y = corner.Y + top });
            }
            // 连续边的叉积必须同号且非零，排除重复点、共线、凹形和自交框。
            double? orientation = null;
            for (var i = 0; i < points.Count; i++)
            {
                var a = points[i];
                var b = points[(i + 1) % points.Count];
                var c = points[(i + 2) % points.Count];
                var cross = (b.X - a.X) * (c.Y - b.Y) - (b.Y - a.Y) * (c.X - b.X);
                if (Math.Abs(cross) <= 0.000001 || (orientation is double sign && cross * sign <= 0))
                    return null;
                orientation ??= cross;
            }
            result.Add(new LiveDialogueOcrCandidateDto
            {
                Index = result.Count, Points = points.AsReadOnly(), DetectionScore = item.DetectionScore,
                Text = item.Text, RecognitionScore = item.RecognitionScore
            });
        }
        return result.AsReadOnly();
    }

    /// <summary>边缘与灰度范围只提供保守诊断，不能证实空文字。</summary>
    internal static LiveDialogueRegionObservationDto AnalyzeDialogueRegion(
        Mat frame, LiveDialogueRegionDto region, Func<Mat, int> countOcrRegions)
    {
        var left = (int)Math.Floor(region.X * frame.Width);
        var top = (int)Math.Floor(region.Y * frame.Height);
        var right = (int)Math.Ceiling((region.X + region.Width) * frame.Width);
        var bottom = (int)Math.Ceiling((region.Y + region.Height) * frame.Height);
        if (left < 0 || top < 0 || right > frame.Width || bottom > frame.Height
            || right - left < 16 || bottom - top < 16)
            throw AgentException.InvalidRequest("对话校准区域超出画面或小于 16 像素。");
        using var roi = new Mat(frame, new Rect(left, top, right - left, bottom - top));
        using var gray = new Mat();
        if (roi.Channels() == 3) Cv2.CvtColor(roi, gray, ColorConversionCodes.BGR2GRAY);
        else if (roi.Channels() == 4) Cv2.CvtColor(roi, gray, ColorConversionCodes.BGRA2GRAY);
        else if (roi.Channels() == 1) roi.CopyTo(gray);
        else throw new InvalidOperationException("不支持的画面通道数。");
        Cv2.MinMaxLoc(gray, out double min, out double max);
        using var edges = new Mat();
        Cv2.Canny(gray, edges, 20, 40);
        var fraction = Cv2.CountNonZero(edges) / (double)(roi.Width * roi.Height);
        var count = countOcrRegions(roi);
        if (count < 0) throw new InvalidOperationException("OCR 返回无效检测数。");
        return new LiveDialogueRegionObservationDto
        {
            State = count > 0 ? "text_detected" : max - min <= 4 && fraction <= 0.0001 ? "low_structure" : "uncertain",
            FilteredOcrRegions = count, GrayRange = max - min, EdgeFraction = fraction
        };
    }

    /// <summary>
    /// 在指定区域执行 OCR，并把结果换算为归一化坐标。
    /// </summary>
    private static List<AgentOcrRegionDto> RunOcr(ImageRegion region, AgentFrame frame, AgentRoiDto? roi)
    {
        Rect roiRect = default;
        if (roi is not null)
        {
            var scaleX = region.Width / (double)frame.Image.Width;
            var scaleY = region.Height / (double)frame.Image.Height;
            var x = (int)Math.Round(roi.X * scaleX);
            var y = (int)Math.Round(roi.Y * scaleY);
            var width = (int)Math.Round(roi.Width * scaleX);
            var height = (int)Math.Round(roi.Height * scaleY);
            roiRect = new Rect(x, y, width, height).ClampTo(region.SrcMat);
            if (roiRect.Width <= 0 || roiRect.Height <= 0)
            {
                throw AgentException.InvalidRequest("OCR 关注区域为空或超出画面。",
                    $"roi={roi.X},{roi.Y},{roi.Width},{roi.Height}");
            }
        }

        var recognitionObject = new RecognitionObject
        {
            RecognitionType = RecognitionTypes.Ocr,
            RegionOfInterest = roiRect
        };

        var results = region.FindMulti(recognitionObject);
        var list = new List<AgentOcrRegionDto>(results.Count);
        try
        {
            foreach (var item in results)
            {
                var centerX = item.X + item.Width / 2d;
                var centerY = item.Y + item.Height / 2d;
                list.Add(new AgentOcrRegionDto
                {
                    Text = item.Text,
                    X = item.X,
                    Y = item.Y,
                    Width = item.Width,
                    Height = item.Height,
                    NormalizedX = Math.Clamp(centerX / region.Width, 0, 1),
                    NormalizedY = Math.Clamp(centerY / region.Height, 0, 1),
                    NormalizedWidth = Math.Clamp(item.Width / (double)region.Width, 0, 1),
                    NormalizedHeight = Math.Clamp(item.Height / (double)region.Height, 0, 1)
                });
            }
        }
        finally
        {
            foreach (var item in results)
            {
                item.Dispose();
            }
        }

        return list;
    }
}
