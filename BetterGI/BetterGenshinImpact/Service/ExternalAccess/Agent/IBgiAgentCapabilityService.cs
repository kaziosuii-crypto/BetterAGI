using BetterGenshinImpact.Service.ExternalAccess.Agent.Models;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Sessions;
using System.Collections.Generic;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent;

/// <summary>
/// 与 HTTP、MCP 等传输方式无关的智能体游戏能力。
/// <para>
/// 能力只包括截图、图像识别、模拟键鼠、会话留存和运行环境启停；
/// 不提供游戏内存读写、封包操作、Shell、进程内脚本或账号切换。
/// </para>
/// </summary>
public interface IBgiAgentCapabilityService
{
    /// <summary>
    /// 智能体控制能力是否已启用。
    /// </summary>
    bool IsEnabled { get; }

    /// <summary>
    /// 获取适配器与运行环境状态。
    /// </summary>
    AgentStatusDto GetStatus();

    /// <summary>
    /// 观察一帧游戏画面，并可选返回 UI 状态、OCR 与图像。
    /// </summary>
    Task<AgentObservationResult> ObserveAsync(AgentObserveRequest request, CancellationToken cancellationToken);

    /// <summary>
    /// 执行一组动作，默认在结束后自动观察一帧。
    /// </summary>
    Task<AgentActionResultDto> ExecuteAsync(AgentActionRequest request, CancellationToken cancellationToken);

    /// <summary>
    /// 确保会话存在。
    /// </summary>
    Task<AgentSessionSummaryDto> CreateSessionAsync(string? sessionId, string? name, CancellationToken cancellationToken);

    /// <summary>
    /// 列出会话。
    /// </summary>
    Task<IReadOnlyList<AgentSessionSummaryDto>> ListSessionsAsync(CancellationToken cancellationToken);

    /// <summary>
    /// 获取会话摘要。
    /// </summary>
    Task<AgentSessionSummaryDto> GetSessionAsync(string? sessionId, CancellationToken cancellationToken);

    /// <summary>
    /// 获取恢复会话所需的信息。
    /// </summary>
    Task<AgentResumeDto> GetResumeAsync(string? sessionId, int recentEventLimit, CancellationToken cancellationToken);

    /// <summary>
    /// 读取会话事件。
    /// </summary>
    Task<IReadOnlyList<AgentEventDto>> GetEventsAsync(string? sessionId, long afterSeq, int limit,
        CancellationToken cancellationToken);

    /// <summary>
    /// 读取会话内存。
    /// </summary>
    Task<AgentMemoryDto> GetMemoryAsync(string? sessionId, CancellationToken cancellationToken);

    /// <summary>
    /// 合并会话内存。
    /// </summary>
    Task<AgentMemoryDto> MergeMemoryAsync(string? sessionId, JsonObject values, CancellationToken cancellationToken);

    /// <summary>
    /// 删除一个内存键。
    /// </summary>
    Task<AgentMemoryDto> DeleteMemoryAsync(string? sessionId, string key, CancellationToken cancellationToken);

    /// <summary>
    /// 创建检查点。
    /// </summary>
    Task<AgentCheckpointDto> CreateCheckpointAsync(string? sessionId, string name, string? note,
        CancellationToken cancellationToken);

    /// <summary>
    /// 列出检查点。
    /// </summary>
    Task<IReadOnlyList<AgentCheckpointDto>> ListCheckpointsAsync(string? sessionId, CancellationToken cancellationToken);

    /// <summary>
    /// 获取已保存截图的文件信息。
    /// </summary>
    AgentStoredFrame? GetStoredFrame(string? sessionId, string frameId);

    /// <summary>
    /// 请求启动游戏运行环境。会同时启动 BetterGI 的实时触发器。
    /// </summary>
    Task<bool> StartRuntimeAsync(CancellationToken cancellationToken);

    /// <summary>
    /// 停止游戏运行环境并释放输入。
    /// </summary>
    Task StopRuntimeAsync(CancellationToken cancellationToken);

    /// <summary>
    /// 停止当前智能体操作，并拒绝后续输入，直到调用 <see cref="Resume"/>。
    /// </summary>
    void Stop();

    /// <summary>
    /// 恢复被停止的智能体操作。
    /// </summary>
    void Resume();
}
