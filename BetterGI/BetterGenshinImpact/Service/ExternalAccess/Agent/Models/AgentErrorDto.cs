namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Models;

/// <summary>
/// 智能体适配器返回的结构化错误。
/// </summary>
public sealed class AgentErrorDto
{
    /// <summary>稳定的错误码。</summary>
    public required string Code { get; init; }

    /// <summary>面向调用方的错误说明。</summary>
    public required string Message { get; init; }

    /// <summary>补充信息，例如线程、异常或参数细节。</summary>
    public string? Details { get; init; }
}
