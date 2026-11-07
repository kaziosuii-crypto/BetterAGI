using BetterGenshinImpact.Service.ExternalAccess.Agent.Models;
using System;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Live;

/// <summary>只计算诊断候选；它没有解除锁或注入动作的能力。</summary>
internal static class LiveDialogueCandidateTracker
{
    /// <summary>调用方的开关不能自授权；策略异常也按未授权处理。</summary>
    public static bool Authorize(ILiveDialogueSafetyPolicy? policy, LiveDialogueCalibrationDto? calibration)
    {
        if (policy is null || calibration?.EnableExitCandidate != true) return false;
        try { return policy.AllowsExitCandidate(LiveDialogueCalibration.Copy(calibration)!); }
        catch { return false; }
    }

    /// <summary>需要同帧的明确主界面、两个完整原始 OCR 零框、低结构及有效的新鲜帧。</summary>
    public static int Advance(int previous, int required, bool authorized, bool latched,
        bool independentAndFresh, AgentVisionDto? vision, LiveDialogueObservationDto? dialogue)
    {
        if (!authorized || !latched || !independentAndFresh || required is < 2 or > 10
            || vision?.UiCategory != "Main" || vision.IsInTalkUi != false
            || !Good(dialogue?.Message) || !Good(dialogue?.Options)) return 0;
        return Math.Min(Math.Max(previous, 0) + 1, required);
    }

    /// <summary>低置信或未知 OCR 不可作为候选。</summary>
    private static bool Good(LiveDialogueRegionObservationDto? item) => item is not null
        && item.OcrCompleted && item.RawDetectionCount == 0 && item.LowConfidenceCount == 0
        && item.State == "low_structure" && double.IsFinite(item.GrayRange)
        && double.IsFinite(item.EdgeFraction) && item.GrayRange is >= 0 and <= 4
        && item.EdgeFraction is >= 0 and <= 0.0001;
}
