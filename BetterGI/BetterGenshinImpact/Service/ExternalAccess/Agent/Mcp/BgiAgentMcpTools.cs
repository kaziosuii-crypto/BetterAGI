using BetterGenshinImpact.Service.ExternalAccess.Agent.Models;
using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Tasks;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Mcp;

/// <summary>
/// 将智能体游戏能力适配为 MCP 工具。
/// <para>
/// 工具只负责把 MCP 参数转换为 <see cref="IBgiAgentCapabilityService"/> 调用；
/// 截图、输入、会话留存与安全限制均由能力服务统一处理。
/// </para>
/// </summary>
/// <param name="service">与传输协议无关的智能体游戏能力服务。</param>
[McpServerToolType]
public sealed class BgiAgentMcpTools(IBgiAgentCapabilityService service)
{
    /// <summary>
    /// 工具返回 JSON 文本时使用的序列化选项：camelCase 命名并忽略空值。
    /// </summary>
    private static readonly JsonSerializerOptions _jsonOptions = new(JsonSerializerDefaults.Web)
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull
    };

    /// <summary>
    /// 获取智能体控制能力与运行环境状态。
    /// </summary>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>智能体状态 DTO。</returns>
    [McpServerTool(Name = "bgi_agent_status", UseStructuredContent = true)]
    [Description("获取智能体控制能力、运行环境、停止开关与固定限制的当前状态。调用观察或操作前应先读取该状态。")]
    public AgentStatusDto GetStatus(CancellationToken cancellationToken = default)
    {
        return service.GetStatus();
    }

    /// <summary>
    /// 观察一帧游戏画面。
    /// <para>
    /// JSON 元数据通过文本内容返回；当 <paramref name="includeImage"/> 为 true 时，
    /// 同时以 MCP 图像内容块返回 PNG 图像，避免在 JSON 文本中重复 Base64 数据。
    /// </para>
    /// </summary>
    /// <param name="sessionId">会话标识；省略时使用默认会话。</param>
    /// <param name="includeUiState">是否分析当前游戏 UI 类别与可操作状态。</param>
    /// <param name="includeOcr">是否执行 OCR；全屏 OCR 开销较大。</param>
    /// <param name="includeImage">是否附带 PNG 图像内容块。</param>
    /// <param name="maxWidth">返回图像的最大宽度；0 表示保持原尺寸。</param>
    /// <param name="ocrRegions">可选命名像素区域，最多 8 个，互不重叠且不允许覆盖整帧。</param>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>包含 JSON 元数据与可选图像内容块的工具结果。</returns>
    [McpServerTool(Name = "bgi_agent_observe")]
    [Description("观察一帧游戏画面；可用 ocrRegions 指定最多 8 个互不重叠的原帧像素区域，分别返回 OCR 结果。includeImage 为 true 时同时返回 PNG 图像。")]
    public async Task<CallToolResult> ObserveAsync(
        [Description("会话标识；省略时使用默认会话。")]
        string? sessionId = null,
        [Description("是否分析当前游戏 UI 类别与可操作状态，默认 true。")]
        bool includeUiState = true,
        [Description("是否执行 OCR，默认 false；全屏 OCR 开销较大。")]
        bool includeOcr = false,
        [Description("是否在结果中附带 PNG 图像内容，默认 true。")]
        bool includeImage = true,
        [Description("返回图像的最大宽度，默认 1280，最大 1920；0 表示保持原尺寸。")]
        int maxWidth = AgentLimits.DefaultMaxImageWidth,
        CancellationToken cancellationToken = default,
        [Description("命名 OCR 区域数组：每项包含 name 与 roi（必须显式提供 x、y、width、height，均为原始截图像素）；最多 8 个，不得重叠或覆盖整帧。提供后只识别这些区域。")]
        List<AgentNamedOcrRegionRequest>? ocrRegions = null)
    {
        try
        {
            var result = await service.ObserveAsync(new AgentObserveRequest
            {
                SessionId = sessionId,
                IncludeUiState = includeUiState,
                IncludeOcr = includeOcr,
                OcrRegions = ocrRegions,
                // 图像通过 MCP 图像内容块返回，JSON 元数据中不再重复 Base64。
                IncludeImageBase64 = false,
                MaxWidth = maxWidth,
                Format = "png"
            }, cancellationToken).ConfigureAwait(false);

            // 只序列化观察 DTO，避免把 AgentObservationResult.ImageBytes 再次编码进文本。
            var json = JsonSerializer.Serialize(result.Observation, _jsonOptions);
            return CreateContentResult(
                json,
                includeImage ? result.ImageBytes : null,
                includeImage ? result.MimeType : null);
        }
        catch (AgentException exception)
        {
            return CreateErrorResult(exception);
        }
    }

    /// <summary>
    /// 执行一组游戏动作，并可选在结束后观察一帧。
    /// <para>
    /// 动作结果以 JSON 文本返回；需要图像时，能力服务会同时生成 Base64 数据，
    /// 工具将其转换为 MCP 图像内容块，并从 JSON 文本中移除重复的 Base64 字段。
    /// </para>
    /// </summary>
    /// <param name="actions">按顺序执行的动作列表。</param>
    /// <param name="sessionId">会话标识；省略时使用默认会话。</param>
    /// <param name="channel">默认输入通道。</param>
    /// <param name="captureAfter">动作完成后是否自动观察一帧。</param>
    /// <param name="includeUiState">观察是否包含 UI 状态。</param>
    /// <param name="includeOcr">观察是否包含 OCR。</param>
    /// <param name="includeImage">是否附带 PNG 图像内容块。</param>
    /// <param name="maxWidth">返回图像的最大宽度；0 表示保持原尺寸。</param>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>包含动作执行结果与可选图像内容块的工具结果。</returns>
    [McpServerTool(Name = "bgi_agent_act")]
    [Description("按顺序执行一组游戏动作（点击、按键、等待等），默认在结束后观察一帧并返回 PNG 图像。")]
    public async Task<CallToolResult> ActAsync(
        [Description("按顺序执行的动作列表。坐标为归一化坐标时最稳定。")]
        List<AgentActionDto> actions,
        [Description("会话标识；省略时使用默认会话。")]
        string? sessionId = null,
        [Description("默认输入通道：foreground（SendInput，推荐）或 background（PostMessage）。")]
        string channel = "foreground",
        [Description("动作完成后是否自动观察一帧，默认 true。")]
        bool captureAfter = true,
        [Description("观察是否包含 UI 状态，默认 true。")]
        bool includeUiState = true,
        [Description("观察是否包含 OCR，默认 false。")]
        bool includeOcr = false,
        [Description("是否在结果中附带 PNG 图像内容，默认 true。")]
        bool includeImage = true,
        [Description("返回图像的最大宽度，默认 1280，最大 1920；0 表示保持原尺寸。")]
        int maxWidth = AgentLimits.DefaultMaxImageWidth,
        CancellationToken cancellationToken = default)
    {
        try
        {
            var result = await service.ExecuteAsync(new AgentActionRequest
            {
                SessionId = sessionId,
                Channel = channel,
                Actions = actions,
                CaptureAfter = captureAfter,
                IncludeUiState = includeUiState,
                IncludeOcr = includeOcr,
                // MCP 需要原始字节构造图像内容块，因此请求能力服务返回 Base64。
                IncludeImageBase64 = includeImage,
                MaxWidth = maxWidth,
                Format = "png"
            }, cancellationToken).ConfigureAwait(false);

            // 优先使用序列结束观察的图像；captureAfter=false 时退回最后一个 observe 步骤的图像。
            byte[]? imageBytes = null;
            var observation = result.Observation;
            var base64 = observation?.ImageBase64;
            if (includeImage && string.IsNullOrEmpty(base64))
            {
                base64 = result.Steps
                    .Select(step => step.Observation?.ImageBase64)
                    .LastOrDefault(value => !string.IsNullOrEmpty(value));
            }

            if (includeImage && !string.IsNullOrEmpty(base64))
            {
                imageBytes = Convert.FromBase64String(base64);
            }

            var json = SerializeActionResult(result);
            return CreateContentResult(json, imageBytes, observation?.Image.MimeType ?? "image/png");
        }
        catch (AgentException exception)
        {
            return CreateErrorResult(exception);
        }
    }

    /// <summary>
    /// 确保智能体会话存在。
    /// </summary>
    /// <param name="sessionId">会话标识；省略时使用默认会话。</param>
    /// <param name="name">新会话的名称。</param>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>会话摘要。</returns>
    [McpServerTool(Name = "bgi_agent_session_create", UseStructuredContent = true)]
    [Description("确保智能体会话存在并返回会话摘要；省略 sessionId 时使用默认会话。")]
    public Task<AgentSessionSummaryDto> CreateSessionAsync(
        [Description("会话标识；省略时使用默认会话。")]
        string? sessionId = null,
        [Description("新会话的名称。")]
        string? name = null,
        CancellationToken cancellationToken = default)
    {
        return RunToolAsync(() => service.CreateSessionAsync(sessionId, name, cancellationToken));
    }

    /// <summary>
    /// 列出全部智能体会话。
    /// </summary>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>会话摘要列表。</returns>
    [McpServerTool(Name = "bgi_agent_session_list", UseStructuredContent = true)]
    [Description("列出全部智能体会话及其事件数量、截图数量和最近帧标识。")]
    public Task<IReadOnlyList<AgentSessionSummaryDto>> ListSessionsAsync(
        CancellationToken cancellationToken = default)
    {
        return RunToolAsync(() => service.ListSessionsAsync(cancellationToken));
    }

    /// <summary>
    /// 获取恢复会话所需的信息。
    /// </summary>
    /// <param name="sessionId">要恢复的会话标识。</param>
    /// <param name="limit">返回的最近事件数量。</param>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>包含会话摘要、内存、最近事件和检查点的恢复信息。</returns>
    [McpServerTool(Name = "bgi_agent_session_resume", UseStructuredContent = true)]
    [Description("获取恢复会话所需的信息：会话摘要、内存、最近事件和检查点。")]
    public Task<AgentResumeDto> ResumeSessionAsync(
        [Description("要恢复的会话标识。")]
        string sessionId,
        [Description("返回的最近事件数量，默认 50。")]
        int limit = 50,
        CancellationToken cancellationToken = default)
    {
        return RunToolAsync(() => service.GetResumeAsync(sessionId, limit, cancellationToken));
    }

    /// <summary>
    /// 读取会话内存。
    /// </summary>
    /// <param name="sessionId">会话标识；省略时使用默认会话。</param>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>会话内存 DTO。</returns>
    [McpServerTool(Name = "bgi_agent_memory_get", UseStructuredContent = true)]
    [Description("读取会话内存中的键值便签；省略 sessionId 时读取默认会话。")]
    public Task<AgentMemoryDto> GetMemoryAsync(
        [Description("会话标识；省略时使用默认会话。")]
        string? sessionId = null,
        CancellationToken cancellationToken = default)
    {
        return RunToolAsync(() => service.GetMemoryAsync(sessionId, cancellationToken));
    }

    /// <summary>
    /// 合并会话内存。
    /// </summary>
    /// <param name="values">要合并的键值 JSON 对象。</param>
    /// <param name="sessionId">会话标识；省略时使用默认会话。</param>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>合并后的会话内存 DTO。</returns>
    [McpServerTool(Name = "bgi_agent_memory_set", UseStructuredContent = true)]
    [Description("合并写入会话内存中的键值便签；值为 null 的键会被删除。")]
    public Task<AgentMemoryDto> SetMemoryAsync(
        [Description("要合并写入的键值 JSON 对象；值为 null 的键会被删除。")]
        JsonObject values,
        [Description("会话标识；省略时使用默认会话。")]
        string? sessionId = null,
        CancellationToken cancellationToken = default)
    {
        return RunToolAsync(() => service.MergeMemoryAsync(sessionId, values, cancellationToken));
    }

    /// <summary>
    /// 创建会话检查点。
    /// </summary>
    /// <param name="sessionId">会话标识；省略时使用默认会话。</param>
    /// <param name="name">检查点名称；省略时使用 checkpoint。</param>
    /// <param name="note">调用方记录的检查点说明。</param>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>新创建的检查点 DTO。</returns>
    [McpServerTool(Name = "bgi_agent_checkpoint_create", UseStructuredContent = true)]
    [Description("为当前会话创建检查点，保存内存、最近帧和事件位置快照。")]
    public Task<AgentCheckpointDto> CreateCheckpointAsync(
        [Description("会话标识；省略时使用默认会话。")]
        string? sessionId = null,
        [Description("检查点名称；省略时使用 checkpoint。")]
        string? name = null,
        [Description("调用方记录的检查点说明。")]
        string? note = null,
        CancellationToken cancellationToken = default)
    {
        // 能力接口要求非空名称，这里为省略名称的调用提供稳定的默认值。
        return RunToolAsync(() => service.CreateCheckpointAsync(sessionId, name ?? "checkpoint", note, cancellationToken));
    }

    /// <summary>
    /// 请求启动游戏运行环境。
    /// </summary>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>是否成功发起启动。</returns>
    [McpServerTool(Name = "bgi_agent_runtime_start")]
    [Description("请求启动游戏运行环境，同时启动 BetterGI 的实时触发器；返回是否成功发起启动。")]
    public Task<bool> StartRuntimeAsync(CancellationToken cancellationToken = default)
    {
        return RunToolAsync(() => service.StartRuntimeAsync(cancellationToken));
    }

    /// <summary>
    /// 停止游戏运行环境并释放输入。
    /// </summary>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>表示停止操作的任务。</returns>
    [McpServerTool(Name = "bgi_agent_runtime_stop")]
    [Description("停止游戏运行环境并释放输入。")]
    public Task StopRuntimeAsync(CancellationToken cancellationToken = default)
    {
        return RunToolAsync(() => service.StopRuntimeAsync(cancellationToken));
    }

    /// <summary>
    /// 停止当前智能体操作。
    /// </summary>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>已完成的任务。</returns>
    [McpServerTool(Name = "bgi_agent_stop")]
    [Description("停止当前智能体操作并拒绝后续输入，直到调用 bgi_agent_resume 恢复。")]
    public Task StopAsync(CancellationToken cancellationToken = default)
    {
        service.Stop();
        return Task.CompletedTask;
    }

    /// <summary>
    /// 恢复被停止的智能体操作。
    /// </summary>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>已完成的任务。</returns>
    [McpServerTool(Name = "bgi_agent_resume")]
    [Description("恢复被 bgi_agent_stop 停止的智能体操作。")]
    public Task ResumeAsync(CancellationToken cancellationToken = default)
    {
        service.Resume();
        return Task.CompletedTask;
    }

    /// <summary>
    /// 执行返回 DTO 的工具逻辑，并把业务异常转换为 MCP 协议异常，保留稳定错误码。
    /// </summary>
    /// <typeparam name="T">返回类型。</typeparam>
    /// <param name="action">工具逻辑。</param>
    /// <returns>工具结果。</returns>
    private static async Task<T> RunToolAsync<T>(Func<Task<T>> action)
    {
        try
        {
            return await action().ConfigureAwait(false);
        }
        catch (AgentException exception)
        {
            throw new McpException($"{exception.Code}: {exception.Message}", exception);
        }
    }

    /// <summary>
    /// 执行不返回值的工具逻辑，并转换业务异常。
    /// </summary>
    /// <param name="action">工具逻辑。</param>
    /// <returns>异步任务。</returns>
    private static async Task RunToolAsync(Func<Task> action)
    {
        try
        {
            await action().ConfigureAwait(false);
        }
        catch (AgentException exception)
        {
            throw new McpException($"{exception.Code}: {exception.Message}", exception);
        }
    }

    /// <summary>
    /// 把能力服务抛出的业务异常转换为 MCP 工具错误结果，保留稳定错误码。
    /// </summary>
    /// <param name="exception">业务异常。</param>
    /// <returns>带错误文本的工具结果。</returns>
    private static CallToolResult CreateErrorResult(AgentException exception)
    {
        return new CallToolResult
        {
            Content =
            [
                new TextContentBlock
                {
                    Text = $"{exception.Code}: {exception.Message}"
                }
            ],
            IsError = true
        };
    }

    /// <summary>
    /// 构造包含 JSON 文本与可选图像内容块的 MCP 工具结果。
    /// </summary>
    /// <param name="json">作为文本内容返回的 JSON 元数据。</param>
    /// <param name="imageBytes">可选的图像字节。</param>
    /// <param name="mimeType">图像 MIME 类型；为空时不添加图像内容块。</param>
    /// <returns>MCP 工具调用结果。</returns>
    private static CallToolResult CreateContentResult(string json, byte[]? imageBytes, string? mimeType)
    {
        // 文本内容始终放在首位，便于调用方先读取结构化元数据。
        var content = new List<ContentBlock>
        {
            new TextContentBlock { Text = json }
        };

        if (imageBytes is { Length: > 0 } && !string.IsNullOrWhiteSpace(mimeType))
        {
            content.Add(ImageContentBlock.FromBytes(imageBytes, mimeType));
        }

        return new CallToolResult
        {
            Content = content,
            IsError = false
        };
    }

    /// <summary>
    /// 序列化动作执行结果，并移除其中重复的图像 Base64 数据。
    /// </summary>
    /// <param name="result">动作执行结果。</param>
    /// <returns>不含图像 Base64 的 JSON 文本。</returns>
    private static string SerializeActionResult(AgentActionResultDto result)
    {
        var node = JsonSerializer.SerializeToNode(result, _jsonOptions);
        RemoveImageBase64(node);
        return node?.ToJsonString(_jsonOptions) ?? "{}";
    }

    /// <summary>
    /// 递归移除 JSON 节点中的 imageBase64 字段，包括步骤内嵌的观察结果。
    /// </summary>
    /// <param name="node">待处理的 JSON 节点。</param>
    private static void RemoveImageBase64(JsonNode? node)
    {
        switch (node)
        {
            case JsonObject jsonObject:
                // 先移除当前层的图像数据，再处理其余子节点。
                jsonObject.Remove("imageBase64");
                foreach (var property in jsonObject)
                {
                    RemoveImageBase64(property.Value);
                }

                break;
            case JsonArray jsonArray:
                foreach (var item in jsonArray)
                {
                    RemoveImageBase64(item);
                }

                break;
        }
    }
}
