using System.Collections.Generic;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Models;

/// <summary>
/// 智能体适配器的整体状态。调用方应先读取该状态，再决定是否观察或操作游戏。
/// </summary>
public sealed class AgentStatusDto
{
    /// <summary>
    /// 智能体控制能力是否已启用。
    /// </summary>
    public required bool Enabled { get; init; }

    /// <summary>
    /// 当前是否已绑定游戏运行环境。
    /// </summary>
    public required bool RuntimeRunning { get; init; }

    /// <summary>
    /// 用户是否已按下停止开关。
    /// </summary>
    public required bool StopRequested { get; init; }

    /// <summary>
    /// 能力列表，便于调用方发现可用动作。
    /// </summary>
    public required IReadOnlyList<string> Capabilities { get; init; }

    /// <summary>
    /// 固定限制。
    /// </summary>
    public required AgentLimitsDto Limits { get; init; }

    /// <summary>
    /// 当前运行环境快照；未启动时为空。
    /// </summary>
    public required AgentRuntimeSnapshotDto? Runtime { get; init; }

    /// <summary>
    /// 会话文件相对程序目录的位置。
    /// </summary>
    public required string SessionRoot { get; init; }

    /// <summary>
    /// 适配器是否提供任何游戏内存读写能力。该值恒为 false，用于向调用方明确安全边界。
    /// </summary>
    public bool MemoryAccessAllowed { get; init; }

    /// <summary>
    /// 适配器 API 版本。
    /// </summary>
    public required string ApiVersion { get; init; }
}

/// <summary>
/// 智能体适配器的固定限制。
/// </summary>
public sealed class AgentLimitsDto
{
    /// <summary>单次请求允许的最大动作数。</summary>
    public required int MaxActionsPerRequest { get; init; }

    /// <summary>单个 wait 动作允许的最大毫秒数。</summary>
    public required int MaxWaitMs { get; init; }

    /// <summary>单次动作序列允许的最大总毫秒数。</summary>
    public required int MaxSequenceDurationMs { get; init; }

    /// <summary>单次文本输入允许的最大字符数。</summary>
    public required int MaxTextLength { get; init; }

    /// <summary>两次输入动作之间的最小间隔毫秒数。</summary>
    public required int MinActionIntervalMs { get; init; }

    /// <summary>单个会话保留的截图数量上限。</summary>
    public required int MaxFramesPerSession { get; init; }

    /// <summary>允许请求的最大截图宽度。</summary>
    public required int MaxImageWidth { get; init; }
}

/// <summary>
/// 游戏运行环境的只读快照。所有坐标均为桌面物理像素。
/// </summary>
public sealed class AgentRuntimeSnapshotDto
{
    /// <summary>运行环境种类，例如 Win32Window 或 WebPage。</summary>
    public required string Kind { get; init; }

    /// <summary>游戏窗口所属进程标识。</summary>
    public required int ProcessId { get; init; }

    /// <summary>游戏顶层窗口句柄。</summary>
    public required long WindowHandle { get; init; }

    /// <summary>游戏是否仍在运行。</summary>
    public required bool IsAlive { get; init; }

    /// <summary>游戏窗口当前是否位于前台。</summary>
    public required bool IsForeground { get; init; }

    /// <summary>游戏窗口是否已最小化。</summary>
    public required bool IsMinimized { get; init; }

    /// <summary>输入是否依赖游戏窗口位于前台。</summary>
    public required bool RequiresForeground { get; init; }

    /// <summary>当前游戏画面在桌面上的区域。</summary>
    public required AgentCaptureAreaDto CaptureArea { get; init; }
}

/// <summary>
/// 游戏画面在桌面上的矩形区域。
/// </summary>
public sealed class AgentCaptureAreaDto
{
    /// <summary>左上角 X 坐标（物理像素）。</summary>
    public required int X { get; init; }

    /// <summary>左上角 Y 坐标（物理像素）。</summary>
    public required int Y { get; init; }

    /// <summary>宽度（物理像素）。</summary>
    public required int Width { get; init; }

    /// <summary>高度（物理像素）。</summary>
    public required int Height { get; init; }

    /// <summary>输入坐标缩放比例。</summary>
    public required float DpiScale { get; init; }
}
