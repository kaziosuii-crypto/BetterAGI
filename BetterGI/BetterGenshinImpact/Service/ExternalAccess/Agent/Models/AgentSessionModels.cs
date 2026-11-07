using System;
using System.Collections.Generic;
using System.Text.Json.Nodes;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Models;

/// <summary>
/// 智能体会话摘要。会话用于留存截图、动作、观察和调用方的备忘，支持跨请求、跨重启续接。
/// </summary>
public sealed class AgentSessionSummaryDto
{
    /// <summary>会话标识。</summary>
    public required string Id { get; init; }

    /// <summary>会话名称。</summary>
    public required string Name { get; init; }

    /// <summary>创建时间（UTC）。</summary>
    public required DateTimeOffset CreatedAtUtc { get; init; }

    /// <summary>最后更新时间（UTC）。</summary>
    public required DateTimeOffset UpdatedAtUtc { get; init; }

    /// <summary>事件数量。</summary>
    public required long EventCount { get; init; }

    /// <summary>当前保留的截图数量。</summary>
    public required int FrameCount { get; init; }

    /// <summary>最近一帧的标识。</summary>
    public string? LastFrameId { get; init; }

    /// <summary>最近一个事件的序号。</summary>
    public required long LastSeq { get; init; }
}

/// <summary>
/// 会话内存：调用方可自由读写的键值便签，用于“记住下一步要做什么”。
/// </summary>
public sealed class AgentMemoryDto
{
    /// <summary>会话标识。</summary>
    public required string SessionId { get; init; }

    /// <summary>最后更新时间（UTC）。</summary>
    public required DateTimeOffset UpdatedAtUtc { get; init; }

    /// <summary>键值内容。</summary>
    public required JsonObject Values { get; init; }
}

/// <summary>
/// 会话检查点：某一时刻的内存、最近帧和事件位置快照。
/// </summary>
public sealed class AgentCheckpointDto
{
    /// <summary>检查点标识。</summary>
    public required string Id { get; init; }

    /// <summary>检查点名称。</summary>
    public required string Name { get; init; }

    /// <summary>调用方记录的说明。</summary>
    public string? Note { get; init; }

    /// <summary>创建时间（UTC）。</summary>
    public required DateTimeOffset CreatedAtUtc { get; init; }

    /// <summary>创建时的内存快照。</summary>
    public required JsonObject Memory { get; init; }

    /// <summary>创建时的最近帧标识。</summary>
    public string? LastFrameId { get; init; }

    /// <summary>创建时的事件序号。</summary>
    public required long LastSeq { get; init; }
}

/// <summary>
/// 会话事件。事件是按顺序追加的 JSONL 记录。
/// </summary>
public sealed class AgentEventDto
{
    /// <summary>会话内递增序号。</summary>
    public required long Seq { get; init; }

    /// <summary>事件时间（UTC）。</summary>
    public required DateTimeOffset TimestampUtc { get; init; }

    /// <summary>事件类型，例如 session.created、frame.saved、observation、action、sequence.cancelled、runtime.started、runtime.stopped、agent.stopped。</summary>
    public required string Type { get; init; }

    /// <summary>事件数据。</summary>
    public JsonNode? Data { get; init; }
}

/// <summary>
/// 恢复会话所需的信息集合。
/// </summary>
public sealed class AgentResumeDto
{
    /// <summary>会话摘要。</summary>
    public required AgentSessionSummaryDto Session { get; init; }

    /// <summary>当前内存。</summary>
    public required AgentMemoryDto Memory { get; init; }

    /// <summary>最近的事件。</summary>
    public required IReadOnlyList<AgentEventDto> RecentEvents { get; init; }

    /// <summary>已保存的检查点。</summary>
    public required IReadOnlyList<AgentCheckpointDto> Checkpoints { get; init; }
}
