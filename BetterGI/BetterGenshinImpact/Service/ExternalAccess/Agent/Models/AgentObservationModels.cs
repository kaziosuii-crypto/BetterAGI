using System;
using System.Collections.Generic;
using System.Text.Json.Serialization;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Models;

/// <summary>
/// 观察请求。一次观察只截取一帧，可同时返回图像、UI 状态和 OCR 结果。
/// </summary>
public sealed class AgentObserveRequest
{
    /// <summary>会话标识；为空时使用默认会话。</summary>
    public string? SessionId { get; set; }

    /// <summary>是否分析当前游戏 UI 类别与可操作状态，默认开启。</summary>
    public bool IncludeUiState { get; set; } = true;

    /// <summary>是否执行 OCR，默认关闭。全屏 OCR 开销较大，建议只在需要时开启。</summary>
    public bool IncludeOcr { get; set; }

    /// <summary>OCR 的关注区域（当前截图像素坐标）；为空时识别整帧。</summary>
    public AgentRoiDto? OcrRoi { get; set; }

    /// <summary>可选的命名 OCR 区域，坐标为原始截图像素；提供时仅识别这些区域，不能与 OcrRoi 同用。</summary>
    public List<AgentNamedOcrRegionRequest>? OcrRegions { get; set; }

    /// <summary>是否在 JSON 响应中直接返回 Base64 图像，默认关闭。</summary>
    public bool IncludeImageBase64 { get; set; }

    /// <summary>返回图像的最大宽度，默认 1280，最大 1920；0 表示保持原尺寸。</summary>
    public int MaxWidth { get; set; } = AgentLimits.DefaultMaxImageWidth;

    /// <summary>图像格式：png 或 jpg，默认 png。</summary>
    public string Format { get; set; } = "png";

    /// <summary>JPEG 质量，范围 1～100，默认 85。</summary>
    public int Quality { get; set; } = 85;

    /// <summary>是否把截图写入会话目录，默认开启。</summary>
    public bool SaveFrame { get; set; } = true;

    /// <summary>可选的截图名称，用于在事件日志中标记这一帧。</summary>
    public string? Name { get; set; }
}

/// <summary>
/// 一次观察的结果。图像通过 URL 或 Base64 返回，坐标统一提供归一化值。
/// </summary>
public sealed class AgentObservationDto
{
    /// <summary>会话标识。</summary>
    public required string SessionId { get; init; }

    /// <summary>帧标识；保存截图时与文件一一对应。</summary>
    public required string FrameId { get; init; }

    /// <summary>截取时间（UTC）。</summary>
    public required DateTimeOffset CapturedAtUtc { get; init; }

    /// <summary>观察事件在会话中的序号。</summary>
    public required long EventSeq { get; init; }

    /// <summary>截取这一帧时的运行环境快照。</summary>
    public required AgentRuntimeSnapshotDto Runtime { get; init; }

    /// <summary>这一帧对应的游戏画面区域。</summary>
    public required AgentCaptureAreaDto CaptureArea { get; init; }

    /// <summary>图像元数据。</summary>
    public required AgentImageInfoDto Image { get; init; }

    /// <summary>UI 状态与 OCR 结果；未请求时为空。</summary>
    public AgentVisionDto? Vision { get; init; }

    /// <summary>命名区域的独立 OCR 结果；未请求时为空。零候选不证明区域无字。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public IReadOnlyList<AgentNamedOcrRegionResult>? OcrRegions { get; init; }

    /// <summary>可选的 Base64 图像数据，不含 data URL 前缀。</summary>
    public string? ImageBase64 { get; init; }
}

/// <summary>
/// 一帧图像的元数据。
/// </summary>
public sealed class AgentImageInfoDto
{
    /// <summary>图像 MIME 类型。</summary>
    public required string MimeType { get; init; }

    /// <summary>返回图像的宽度（可能已缩小）。</summary>
    public required int Width { get; init; }

    /// <summary>返回图像的高度（可能已缩小）。</summary>
    public required int Height { get; init; }

    /// <summary>游戏原始截图的宽度。</summary>
    public required int OriginalWidth { get; init; }

    /// <summary>游戏原始截图的高度。</summary>
    public required int OriginalHeight { get; init; }

    /// <summary>获取已保存截图的相对 URL；未保存时为空。</summary>
    public string? Url { get; init; }

    /// <summary>这一帧在会话事件中的名称。</summary>
    public string? Name { get; init; }
}

/// <summary>
/// 基于当前帧的 UI 状态与 OCR 结果。所有字段都只来自图像识别。
/// </summary>
public sealed class AgentVisionDto
{
    /// <summary>UI 类别：Unknown、Main、Talk 或 BigMap。</summary>
    public required string UiCategory { get; init; }

    /// <summary>是否位于主界面。</summary>
    public bool? IsInMainUi { get; init; }

    /// <summary>是否位于秘境中。</summary>
    public bool? IsInDomain { get; init; }

    /// <summary>是否位于任意可关闭的界面。</summary>
    public bool? IsInAnyClosableUi { get; init; }

    /// <summary>是否处于可以执行任务的界面。</summary>
    public bool? IsInGameReadyUi { get; init; }

    /// <summary>是否位于队伍选择界面。</summary>
    public bool? IsInPartyViewUi { get; init; }

    /// <summary>是否位于大地图。</summary>
    public bool? IsInBigMapUi { get; init; }

    /// <summary>是否位于对话界面。</summary>
    public bool? IsInTalkUi { get; init; }

    /// <summary>OCR 文本行；未请求 OCR 时为空。</summary>
    public IReadOnlyList<AgentOcrRegionDto>? Ocr { get; init; }

    /// <summary>视觉分析耗时（毫秒）。</summary>
    public required long ElapsedMs { get; init; }
}

/// <summary>
/// 一次观察的完整结果，包含共享 DTO 与可直接返回给 HTTP 客户端的图像字节。
/// </summary>
public sealed class AgentObservationResult
{
    /// <summary>观察 DTO。</summary>
    public required AgentObservationDto Observation { get; init; }

    /// <summary>编码后的图像字节。</summary>
    public required byte[] ImageBytes { get; init; }

    /// <summary>图像 MIME 类型。</summary>
    public required string MimeType { get; init; }
}

/// <summary>
/// 一行 OCR 结果。坐标以识别帧的像素空间给出，并附带归一化值。
/// </summary>
public sealed class AgentOcrRegionDto
{
    /// <summary>识别出的文本。</summary>
    public required string Text { get; init; }

    /// <summary>OCR 原始识别分数；不可用或非有限值时为空。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public float? RecognitionScore { get; init; }

    /// <summary>OCR 原始检测分数；不可用或非有限值时为空。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public float? DetectionScore { get; init; }

    /// <summary>文本框左上角 X 坐标。</summary>
    public required int X { get; init; }

    /// <summary>文本框左上角 Y 坐标。</summary>
    public required int Y { get; init; }

    /// <summary>文本框宽度。</summary>
    public required int Width { get; init; }

    /// <summary>文本框高度。</summary>
    public required int Height { get; init; }

    /// <summary>文本框中心归一化 X 坐标（0～1）。</summary>
    public required double NormalizedX { get; init; }

    /// <summary>文本框中心归一化 Y 坐标（0～1）。</summary>
    public required double NormalizedY { get; init; }

    /// <summary>归一化宽度（0～1）。</summary>
    public required double NormalizedWidth { get; init; }

    /// <summary>归一化高度（0～1）。</summary>
    public required double NormalizedHeight { get; init; }
}

/// <summary>一次观察中需要识别的具名像素区域。</summary>
public sealed class AgentNamedOcrRegionRequest
{
    /// <summary>区域名称，例如 dialogue_text、speaker、choice、quest 或 hud。</summary>
    public string? Name { get; set; }

    /// <summary>原始截图像素区域，必须显式提供。</summary>
    public AgentExplicitOcrRoi? Roi { get; set; }
}

/// <summary>必须显式提供全部四个坐标的原帧像素区域。</summary>
public sealed class AgentExplicitOcrRoi
{
    /// <summary>左上角 X 坐标。</summary>
    public int? X { get; set; }
    /// <summary>左上角 Y 坐标。</summary>
    public int? Y { get; set; }
    /// <summary>宽度。</summary>
    public int? Width { get; set; }
    /// <summary>高度。</summary>
    public int? Height { get; set; }
}

/// <summary>一个具名区域的独立 OCR 观测，不将失败视为无文字。</summary>
public sealed class AgentNamedOcrRegionResult
{
    /// <summary>请求中的区域名称。</summary>
    public required string Name { get; init; }

    /// <summary>所有区域共同使用的观察帧标识。</summary>
    public required string FrameId { get; init; }

    /// <summary>原始截图像素区域。</summary>
    public required AgentRoiDto Roi { get; init; }

    /// <summary>OCR 状态，例如 Completed、DetectionFailed、RecognitionFailed 或 Unavailable。</summary>
    public required string Status { get; init; }

    /// <summary>检测和识别是否都完成。</summary>
    public required bool Completed { get; init; }

    /// <summary>检测到的框数；检测未完成时为空。</summary>
    public int? RawDetectionCount { get; init; }

    /// <summary>识别完成时的文本候选及其原帧像素框；失败时为空而非空列表。</summary>
    public IReadOnlyList<AgentOcrRegionDto>? Candidates { get; init; }

    /// <summary>失败类型；无异常时为空。</summary>
    public string? ErrorType { get; init; }

    /// <summary>失败原因。</summary>
    public string? ErrorMessage { get; init; }
}

/// <summary>
/// 截图像素空间中的关注区域。
/// </summary>
public sealed class AgentRoiDto
{
    /// <summary>左上角 X 坐标。</summary>
    public int X { get; set; }

    /// <summary>左上角 Y 坐标。</summary>
    public int Y { get; set; }

    /// <summary>宽度。</summary>
    public int Width { get; set; }

    /// <summary>高度。</summary>
    public int Height { get; set; }
}
