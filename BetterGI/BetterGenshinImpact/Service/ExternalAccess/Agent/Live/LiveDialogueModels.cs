using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Text.Json.Serialization;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Media;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Live;

/// <summary>以原始画面尺寸归一化的区域，不依赖 JPEG 缩放。</summary>
public sealed class LiveDialogueRegionDto
{
    /// <summary>左上角横坐标。</summary>
    public double X { get; set; }
    /// <summary>左上角纵坐标。</summary>
    public double Y { get; set; }
    /// <summary>宽度。</summary>
    public double Width { get; set; }
    /// <summary>高度。</summary>
    public double Height { get; set; }
}

/// <summary>调用方提交的观察校准；它不是独立认证的安全策略。</summary>
public sealed class LiveDialogueCalibrationDto
{
    /// <summary>消息区域。</summary>
    public LiveDialogueRegionDto? Message { get; set; }
    /// <summary>选项区域。</summary>
    public LiveDialogueRegionDto? Options { get; set; }
    /// <summary>候选退出需要的独立帧数。</summary>
    public int ExitFrames { get; set; } = 3;
    /// <summary>请求候选诊断；没有独立可信策略批准时仍关闭，且绝不授权解锁。</summary>
    public bool EnableExitCandidate { get; set; }
}

/// <summary>一个区域的 OCR 与像素结构观察；低结构不代表没有文字。</summary>
public sealed class LiveDialogueRegionObservationDto
{
    /// <summary>text_detected、low_structure 或 uncertain；均不证明空区域。</summary>
    public required string State { get; init; }
    /// <summary>过滤后的 OCR 检测框数，零不证明没有文字。</summary>
    public int FilteredOcrRegions { get; init; }
    /// <summary>原始检测和识别是否均已完成。</summary>
    public bool OcrCompleted { get; init; }
    /// <summary>未经过识别阈值过滤的检测框数；未知为 null。</summary>
    public int? RawDetectionCount { get; init; }
    /// <summary>低置信框数；识别未完成时未知。</summary>
    public int? LowConfidenceCount { get; init; }
    /// <summary>OCR 状态，不代表无文字。</summary>
    public string OcrStatus { get; init; } = "unavailable";
    /// <summary>灰度动态范围，仅用于结构诊断。</summary>
    public double GrayRange { get; init; }
    /// <summary>边缘像素占比，仅用于结构诊断。</summary>
    public double EdgeFraction { get; init; }
    /// <summary>识别完成且所有框有效时的原始 OCR 结果；null 表示未知，空列表不证明无文字。</summary>
    public IReadOnlyList<LiveDialogueOcrCandidateDto>? Candidates { get; init; }
    /// <summary>本次识别阈值；未知或识别失败时为 null。</summary>
    public float? OcrDropScore { get; init; }
}

/// <summary>原始截图像素坐标中的一个检测点。</summary>
public sealed class LiveDialoguePointDto
{
    /// <summary>原始画面横坐标。</summary>
    public double X { get; init; }
    /// <summary>原始画面纵坐标。</summary>
    public double Y { get; init; }
}

/// <summary>未经阈值筛选的一个 OCR 候选；它不授权输入。</summary>
public sealed class LiveDialogueOcrCandidateDto
{
    /// <summary>原始检测顺序。</summary>
    public int Index { get; init; }
    /// <summary>四角，顺序为左上、右上、右下、左下，坐标属于原始截图。</summary>
    public required IReadOnlyList<LiveDialoguePointDto> Points { get; init; }
    /// <summary>检测分数。</summary>
    public float DetectionScore { get; init; }
    /// <summary>识别文本，未知时为 null。</summary>
    public string? Text { get; init; }
    /// <summary>识别分数，未知时为 null。</summary>
    public float? RecognitionScore { get; init; }
}

/// <summary>宿主将候选绑定到实际发送 JPEG 的来源；不构成动作授权。</summary>
public sealed class LiveDialogueEvidenceSourceDto
{
    /// <summary>截图所属实时会话。</summary>
    public required string SessionId { get; init; }
    /// <summary>实时会话中的帧序号。</summary>
    public long FrameSeq { get; init; }
    /// <summary>实际发送 JPEG 字节的 SHA-256，小写十六进制。</summary>
    public required string JpegSha256 { get; init; }
    /// <summary>原始截图宽度。</summary>
    public int OriginalWidth { get; init; }
    /// <summary>原始截图高度。</summary>
    public int OriginalHeight { get; init; }
    /// <summary>采集开始时的 UTC 时间。</summary>
    public DateTimeOffset CapturedAtUtc { get; init; }
}

/// <summary>同一原始画面上两个区域的观察。</summary>
public sealed class LiveDialogueObservationDto
{
    /// <summary>消息区域。</summary>
    public required LiveDialogueRegionObservationDto Message { get; init; }
    /// <summary>选项区域。</summary>
    public required LiveDialogueRegionObservationDto Options { get; init; }
    /// <summary>选项候选的同帧来源；无法核验时为 null。</summary>
    public LiveDialogueEvidenceSourceDto? EvidenceSource { get; init; }
    /// <summary>本机标定的仅推进模板在此帧正向匹配；不代表游戏状态已被实机验证。</summary>
    public bool AdvanceTemplateMatched { get; init; }
    /// <summary>disabled、collecting 或 candidate；候选不授权解锁。</summary>
    public string ExitCandidateState { get; init; } = "disabled";
    /// <summary>连续满足全部条件的帧数，不是确认无字。</summary>
    public int ExitCandidateFrames { get; init; }
    /// <summary>此诊断永远不代表已授权解除锁定。</summary>
    public bool ExitAuthorized => false;
}

/// <summary>由 Player 提交的同帧对话决策；不接受客户端坐标。</summary>
public sealed class LiveDialogueDecisionRequest
{
    /// <summary>稳定的决策意图标识。</summary>
    public string? IntentId { get; init; }
    /// <summary>已提交的思考和决策标识。</summary>
    public string? DecisionId { get; init; }
    /// <summary>Player 认知事件所属会话。</summary>
    public string? EventSessionId { get; init; }
    /// <summary>当前 Live 会话。</summary>
    public string? SessionId { get; init; }
    /// <summary>同帧标识。</summary>
    public string? FrameId { get; init; }
    /// <summary>同帧 Live 序号。</summary>
    public long FrameSeq { get; init; }
    /// <summary>实际 JPEG 的 SHA-256。</summary>
    public string? FrameSha256 { get; init; }
    /// <summary>采集时间的 Unix 毫秒数。</summary>
    public long CapturedAtMs { get; init; }
    /// <summary>Player 会话代次。</summary>
    public long PlayerGeneration { get; init; }
    /// <summary>来源视觉事件标识。</summary>
    public string? SourceEventId { get; init; }
    /// <summary>advance 或 select。</summary>
    public string? Action { get; init; }
    /// <summary>Player 已提交的选项事件标识。</summary>
    public string? OptionId { get; init; }
    /// <summary>Player 看到的排序后选项序号；不代表像素坐标。</summary>
    public int? OptionIndex { get; init; }
    /// <summary>Player 看到的原始候选文本。</summary>
    public string? OptionText { get; init; }
    /// <summary>未知字段不能用于授权。</summary>
    [JsonExtensionData]
    public Dictionary<string, JsonElement>? UnsupportedFields { get; init; }
}

/// <summary>对话决策处理结果；系统接受输入不代表游戏响应。</summary>
public sealed class LiveDialogueDecisionResult
{
    /// <summary>原始意图标识。</summary>
    public string? IntentId { get; init; }
    /// <summary>是否真正发送了确定性输入；调用异常时为 null。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.Never)]
    public bool? Executed { get; init; }
    /// <summary>系统是否接受全部输入；未知为 null，不代表游戏处理。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.Never)]
    public bool? SystemInputAccepted { get; init; }
    /// <summary>拒绝原因或输入状态。</summary>
    public required string Reason { get; init; }
    /// <summary>没有画面证据时游戏响应未知。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.Never)]
    public bool? GameResponseConfirmed { get; init; }
}

/// <summary>仅由宿主本地配置提供的 Player 决策证明与选择授权。</summary>
public interface ILivePlayerDialoguePolicy
{
    /// <summary>从固定数据库读取已提交决策并绑定当前宿主候选；失败或未知时返回 false。</summary>
    bool HasCommittedChoice(LiveDialogueDecisionRequest request, LiveDialogueEvidenceSourceDto source,
        LiveDialogueOcrCandidateDto candidate);

    /// <summary>本机启动时的独立动作授权与校准仍有效时才允许选项点击。</summary>
    bool AllowsSelect(LiveDialogueCalibrationDto? calibration);

    /// <summary>本机仅推进授权和完整选项覆盖标定均有效时可请求模板匹配。</summary>
    bool AllowsAdvance(LiveDialogueCalibrationDto? calibration);

    /// <summary>在同一原始帧上匹配本机只读的用户标定模板。</summary>
    bool MatchesAdvanceTemplate(AgentFrame frame, LiveDialogueCalibrationDto? calibration);

    /// <summary>验证已提交 Player 推进决策及同帧视觉来源。</summary>
    bool HasCommittedAdvance(LiveDialogueDecisionRequest request, LiveDialogueEvidenceSourceDto source);
}

/// <summary>宿主固定的、经用户标定的主界面正向模板能力。</summary>
public interface ILiveDialogueExitPolicy
{
    /// <summary>固定配置及校准是否仍有效；不表示当前帧已经匹配。</summary>
    bool IsAvailable(LiveDialogueCalibrationDto? calibration);

    /// <summary>需连续匹配的独立新鲜帧数量。</summary>
    int RequiredFrames { get; }

    /// <summary>在同一原始帧进行模板匹配，不使用 OCR 空结果。</summary>
    bool Matches(AgentFrame frame, LiveDialogueCalibrationDto? calibration);
}

/// <summary>只能由可信宿主注入的候选诊断策略，不允许通过外部请求自行授权。</summary>
public interface ILiveDialogueSafetyPolicy
{
    /// <summary>独立核验完整校准和帧阈值；返回 true 只允许候选诊断，不允许解锁或动作。</summary>
    bool AllowsExitCandidate(LiveDialogueCalibrationDto calibration);

    /// <summary>仅返回宿主独立配置的启动校准；缺失时为 null。</summary>
    LiveDialogueCalibrationDto? GetTrustedCalibration() => null;
}

/// <summary>校准数据的有效性与不可变快照。</summary>
internal static class LiveDialogueCalibration
{
    /// <summary>校验并复制调用方数据，避免入队后被外部修改。</summary>
    public static LiveDialogueCalibrationDto? Copy(LiveDialogueCalibrationDto? calibration)
    {
        if (calibration is null) return null;
        if (calibration.ExitFrames is < 2 or > 10 || !Valid(calibration.Message) || !Valid(calibration.Options))
            throw AgentException.InvalidRequest("对话校准需要两个有效的归一化区域及 2～10 个退出帧。");
        return new LiveDialogueCalibrationDto
        {
            Message = CopyRegion(calibration.Message!), Options = CopyRegion(calibration.Options!),
            ExitFrames = calibration.ExitFrames, EnableExitCandidate = calibration.EnableExitCandidate
        };
    }

    /// <summary>拒绝非有限数、越界及过小区域。</summary>
    private static bool Valid(LiveDialogueRegionDto? region) => region is not null
        && double.IsFinite(region.X) && double.IsFinite(region.Y)
        && double.IsFinite(region.Width) && double.IsFinite(region.Height)
        && region.X >= 0 && region.Y >= 0 && region.Width >= 0.02 && region.Height >= 0.02
        && region.X + region.Width <= 1 && region.Y + region.Height <= 1;

    /// <summary>复制一个区域。</summary>
    private static LiveDialogueRegionDto CopyRegion(LiveDialogueRegionDto value) => new()
    {
        X = value.X, Y = value.Y, Width = value.Width, Height = value.Height
    };
}
