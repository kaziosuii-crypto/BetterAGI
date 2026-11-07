using System;
using System.Collections.Generic;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Models;

/// <summary>
/// 动作序列请求。动作按数组顺序执行，默认在首个失败处停止。
/// </summary>
public sealed class AgentActionRequest
{
    /// <summary>会话标识；为空时使用默认会话。</summary>
    public string? SessionId { get; set; }

    /// <summary>默认输入通道：foreground（SendInput，推荐）或 background（PostMessage）。</summary>
    public string Channel { get; set; } = "foreground";

    /// <summary>按顺序执行的动作。</summary>
    public List<AgentActionDto> Actions { get; set; } = [];

    /// <summary>某个动作失败时是否停止后续动作，默认 true。</summary>
    public bool StopOnError { get; set; } = true;

    /// <summary>动作执行完成后是否自动观察一帧，默认 true。</summary>
    public bool CaptureAfter { get; set; } = true;

    /// <summary>执行完成后的观察是否包含 UI 状态。</summary>
    public bool IncludeUiState { get; set; } = true;

    /// <summary>执行完成后的观察是否包含 OCR。</summary>
    public bool IncludeOcr { get; set; }

    /// <summary>执行完成后的观察是否包含 Base64 图像。</summary>
    public bool IncludeImageBase64 { get; set; }

    /// <summary>返回图像的最大宽度。</summary>
    public int MaxWidth { get; set; } = AgentLimits.DefaultMaxImageWidth;

    /// <summary>返回图像格式：png 或 jpg。</summary>
    public string Format { get; set; } = "png";

    /// <summary>JPEG 质量，范围 1～100。</summary>
    public int Quality { get; set; } = 85;
}

/// <summary>
/// 单个动作。坐标为归一化坐标（0～1，相对游戏画面）时最稳定。
/// <para>
/// 支持的 type：click、doubleClick、rightClick、moveMouse、moveMouseBy、drag、scroll、
/// key、text、wait、activateWindow、screenshot、observe、clickAtText。
/// </para>
/// </summary>
public sealed class AgentActionDto
{
    /// <summary>动作类型。</summary>
    public required string Type { get; set; }

    /// <summary>该动作使用的输入通道，覆盖请求级默认值。</summary>
    public string? Channel { get; set; }

    /// <summary>坐标空间：normalized（默认）、capture（截图像素）或 desktop（桌面物理像素）。</summary>
    public string CoordinateSpace { get; set; } = "normalized";

    /// <summary>目标 X 坐标。</summary>
    public double? X { get; set; }

    /// <summary>目标 Y 坐标。</summary>
    public double? Y { get; set; }

    /// <summary>拖拽终点 X 坐标。</summary>
    public double? ToX { get; set; }

    /// <summary>拖拽终点 Y 坐标。</summary>
    public double? ToY { get; set; }

    /// <summary>相对移动的 X 像素数（仅前台通道）。</summary>
    public int? DeltaX { get; set; }

    /// <summary>相对移动的 Y 像素数（仅前台通道）。</summary>
    public int? DeltaY { get; set; }

    /// <summary>鼠标键：left、right 或 middle。</summary>
    public string Button { get; set; } = "left";

    /// <summary>点击次数，默认 1。</summary>
    public int Clicks { get; set; } = 1;

    /// <summary>按住或拖拽的持续时间（毫秒）。</summary>
    public int DurationMs { get; set; }

    /// <summary>滚轮刻度数，正值向上，负值向下。</summary>
    public int ScrollClicks { get; set; }

    /// <summary>键名，例如 W、A、Space、Shift、E、F1。</summary>
    public string? Key { get; set; }

    /// <summary>按键方式：press（默认）、down 或 up。</summary>
    public string KeyAction { get; set; } = "press";

    /// <summary>文本输入内容，仅前台通道；受输入法与游戏限制，可能无法输入中文。</summary>
    public string? Text { get; set; }

    /// <summary>等待毫秒数。</summary>
    public int WaitMs { get; set; }

    /// <summary>截图或观察事件的名称。</summary>
    public string? Name { get; set; }

    /// <summary>clickAtText 是否要求 OCR 文本完全相等，默认包含匹配。</summary>
    public bool Exact { get; set; }
}

/// <summary>
/// 动作序列的执行结果。
/// </summary>
public sealed class AgentActionResultDto
{
    /// <summary>整个序列是否全部成功。</summary>
    public required bool Ok { get; init; }

    /// <summary>会话标识。</summary>
    public required string SessionId { get; init; }

    /// <summary>执行期间用户是否按下了停止开关。</summary>
    public required bool StopRequested { get; init; }

    /// <summary>每个动作的执行结果。</summary>
    public required IReadOnlyList<AgentStepResultDto> Steps { get; init; }

    /// <summary>序列结束后的观察；未请求时为空。</summary>
    public AgentObservationDto? Observation { get; init; }

    /// <summary>整体失败时的错误说明。</summary>
    public string? Error { get; init; }
}

/// <summary>
/// 单个动作的执行结果。
/// </summary>
public sealed class AgentStepResultDto
{
    /// <summary>动作在请求中的序号，从 0 开始。</summary>
    public required int Index { get; init; }

    /// <summary>动作类型。</summary>
    public required string Type { get; init; }

    /// <summary>是否执行成功。</summary>
    public required bool Ok { get; init; }

    /// <summary>失败原因或补充说明。</summary>
    public string? Message { get; init; }

    /// <summary>失败时的稳定错误码；成功时为空。</summary>
    public string? Code { get; init; }

    /// <summary>失败时的补充细节；成功时为空。</summary>
    public string? Details { get; init; }

    /// <summary>动作开始时间（UTC）。</summary>
    public required DateTimeOffset StartedAtUtc { get; init; }

    /// <summary>动作耗时（毫秒）。</summary>
    public required long DurationMs { get; init; }

    /// <summary>该动作产生的帧标识（截图或观察动作）。</summary>
    public string? FrameId { get; init; }

    /// <summary>该动作实际使用的坐标。</summary>
    public AgentPointDto? Point { get; init; }

    /// <summary>该动作的观察结果（observe 动作）。</summary>
    public AgentObservationDto? Observation { get; init; }
}

/// <summary>
/// 一个动作实际使用的坐标，同时提供归一化、截图和桌面三种表示。
/// </summary>
public sealed class AgentPointDto
{
    /// <summary>归一化 X 坐标（0～1）。</summary>
    public required double NormalizedX { get; init; }

    /// <summary>归一化 Y 坐标（0～1）。</summary>
    public required double NormalizedY { get; init; }

    /// <summary>相对游戏捕获区域的像素 X 坐标。</summary>
    public required double CaptureX { get; init; }

    /// <summary>相对游戏捕获区域的像素 Y 坐标。</summary>
    public required double CaptureY { get; init; }

    /// <summary>桌面物理像素 X 坐标。</summary>
    public required double DesktopX { get; init; }

    /// <summary>桌面物理像素 Y 坐标。</summary>
    public required double DesktopY { get; init; }
}
