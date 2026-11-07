using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Tasks;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Live;

/// <summary>
/// 将实时会话能力适配为 MCP 工具。
/// <para>
/// 工具只负责把 MCP 参数转换为 <see cref="ILiveSessionService"/> 调用；
/// 画面采集、动作执行、时间线与安全限制均由实时会话服务统一处理。
/// </para>
/// </summary>
/// <param name="service">与传输协议无关的实时会话服务。</param>
[McpServerToolType]
public sealed class LiveMcpTools(ILiveSessionService service)
{
    /// <summary>
    /// 工具返回 JSON 文本时使用的序列化选项：camelCase 命名并忽略空值。
    /// </summary>
    private static readonly JsonSerializerOptions _jsonOptions = new(JsonSerializerDefaults.Web)
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull
    };

    /// <summary>
    /// 启动实时会话。
    /// </summary>
    /// <param name="fps">画面广播帧率，默认 10，范围 2～15。</param>
    /// <param name="maxWidth">广播图像最大宽度，默认 1280，范围 320～1920。</param>
    /// <param name="quality">JPEG 质量，默认 70，范围 30～95。</param>
    /// <param name="recordFrames">是否把缩略帧写入会话目录。</param>
    /// <param name="sessionId">会话标识；省略时使用默认会话。</param>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <param name="requireStrictSafety">先激活严格安全模式，而不注入动作。</param>
    /// <param name="dialogueCalibration">消息与选项的归一化诊断区域，不构成授权。</param>
    /// <returns>启动后的实时会话状态。</returns>
    [McpServerTool(Name = "bgi_live_start", UseStructuredContent = true)]
    [Description("启动实时会话，按 fps 采集游戏画面并以 JPEG 广播；需要先启动游戏截图器。重复调用返回当前状态。")]
    public Task<LiveStatusDto> StartAsync(
        [Description("画面广播帧率，默认 10，范围 2～15。")]
        int fps = 10,
        [Description("广播图像最大宽度，默认 1280，范围 320～1920。")]
        int maxWidth = 1280,
        [Description("JPEG 质量，默认 70，范围 30～95。")]
        int quality = 70,
        [Description("是否把缩略帧写入会话目录，默认关闭。")]
        bool recordFrames = false,
        [Description("会话标识；省略时使用默认会话。")]
        string? sessionId = null,
        CancellationToken cancellationToken = default,
        [Description("启动或复用会话时激活严格安全模式；默认关闭。")]
        bool requireStrictSafety = false,
        [Description("消息及选项的归一化诊断区域；不用于授权或证明无文字。")]
        LiveDialogueCalibrationDto? dialogueCalibration = null)
    {
        return RunToolAsync(() => service.StartAsync(new LiveStartRequest
        {
            SessionId = sessionId,
            Fps = fps,
            MaxWidth = maxWidth,
            Quality = quality,
            RecordFrames = recordFrames,
            RequireStrictSafety = requireStrictSafety,
            DialogueCalibration = dialogueCalibration
        }, cancellationToken));
    }

    /// <summary>
    /// 停止实时会话。
    /// </summary>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>表示停止操作的任务。</returns>
    [McpServerTool(Name = "bgi_live_stop")]
    [Description("停止实时会话，释放仍处于按下状态的按键并清空动作队列。")]
    public Task StopAsync(CancellationToken cancellationToken = default)
    {
        return RunToolAsync(() => service.StopAsync(cancellationToken));
    }

    /// <summary>
    /// 获取实时会话状态。
    /// </summary>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>实时会话状态 DTO。</returns>
    [McpServerTool(Name = "bgi_live_status", UseStructuredContent = true)]
    [Description("获取实时会话状态和当前安全观测；安全状态不能保证未来动作安全。")]
    public Task<LiveStatusDto> GetStatusAsync(CancellationToken cancellationToken = default)
    {
        return RunToolAsync(() =>
        {
            cancellationToken.ThrowIfCancellationRequested();
            return Task.FromResult(service.GetStatus());
        });
    }

    /// <summary>
    /// 获取最新一帧画面。
    /// <para>
    /// JSON 元数据通过文本内容返回，JPEG 图像通过 MCP 图像内容块返回；
    /// 尚无画面帧时返回带错误说明的工具错误结果。
    /// </para>
    /// </summary>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>包含 JSON 元数据与 JPEG 图像内容块的工具结果。</returns>
    [McpServerTool(Name = "bgi_live_frame")]
    [Description("获取实时会话的最新 JPEG 画面帧，同时返回帧序号、时间、尺寸、游戏画面区域与运行环境元数据。")]
    public Task<CallToolResult> GetFrameAsync(CancellationToken cancellationToken = default)
    {
        try
        {
            cancellationToken.ThrowIfCancellationRequested();
            var frame = service.GetLatestFrame();
            if (frame is null)
            {
                return Task.FromResult(new CallToolResult
                {
                    Content =
                    [
                        new TextContentBlock
                        {
                            Text = "LIVE_FRAME_NOT_FOUND: 实时会话尚未产生画面帧。请先调用 bgi_live_start 并等待采集。"
                        }
                    ],
                    IsError = true
                });
            }

            // 文本内容先给出帧元数据，随后附上 JPEG 原始字节，避免在 JSON 中重复 Base64 数据。
            var metadata = JsonSerializer.Serialize(new
            {
                frame.Seq,
                frame.TimestampUtc,
                frame.Width,
                frame.Height,
                frame.Capture,
                frame.RuntimeState,
                frame.UiCategory,
                frame.IsInTalkUi,
                frame.DialogueObservation
            }, _jsonOptions);

            return Task.FromResult(new CallToolResult
            {
                Content =
                [
                    new TextContentBlock { Text = metadata },
                    ImageContentBlock.FromBytes(frame.JpegBytes, "image/jpeg")
                ],
                IsError = false
            });
        }
        catch (AgentException exception)
        {
            return Task.FromResult(CreateErrorResult(exception));
        }
    }

    /// <summary>
    /// 设置最新指导文本。
    /// </summary>
    /// <param name="text">指导文本，长度 1～500 个字符。</param>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>生成的时间线事件。</returns>
    [McpServerTool(Name = "bgi_live_set_guidance", UseStructuredContent = true)]
    [Description("设置实时会话的最新指导文本并广播给 WebSocket 客户端；文本长度 1～500 个字符。")]
    public Task<LiveTimelineEventDto> SetGuidanceAsync(
        [Description("指导文本，长度 1～500 个字符。")]
        string text,
        CancellationToken cancellationToken = default)
    {
        return RunToolAsync(() => service.SetGuidanceAsync(new LiveGuidanceRequest
        {
            Text = text
        }, cancellationToken));
    }

    /// <summary>
    /// 追加一条解说。
    /// </summary>
    /// <param name="kind">解说类型：reaction、plan、emotion、lore、chat 或 result。</param>
    /// <param name="text">解说文本，长度 1～500 个字符。</param>
    /// <param name="emotion">可选的情绪标签，最长 32 个字符。</param>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>生成的时间线事件。</returns>
    [McpServerTool(Name = "bgi_live_add_commentary", UseStructuredContent = true)]
    [Description("追加一条实时解说并广播；kind 只能是 reaction、plan、emotion、lore、chat 或 result。")]
    public Task<LiveTimelineEventDto> AddCommentaryAsync(
        [Description("解说类型：reaction、plan、emotion、lore、chat 或 result。")]
        string kind,
        [Description("解说文本，长度 1～500 个字符。")]
        string text,
        [Description("可选的情绪标签，例如 calm、excited、surprised，最长 32 个字符。")]
        string? emotion = null,
        CancellationToken cancellationToken = default)
    {
        return RunToolAsync(() => service.AddCommentaryAsync(new LiveCommentaryRequest
        {
            Kind = kind,
            Text = text,
            Emotion = emotion
        }, cancellationToken));
    }

    /// <summary>
    /// 按序号增量读取时间线。
    /// </summary>
    /// <param name="afterSeq">只返回序号大于该值的事件；默认 0。</param>
    /// <param name="limit">单次返回的事件数量，默认 200，范围 1～500。</param>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>按序号升序排列的时间线事件。</returns>
    [McpServerTool(Name = "bgi_live_timeline", UseStructuredContent = true)]
    [Description("按序号增量读取直播时间线事件（指导、解说、动作状态、启动与停止）；limit 范围为 1～500。")]
    public Task<IReadOnlyList<LiveTimelineEventDto>> GetTimelineAsync(
        [Description("只返回序号大于该值的事件；默认 0。")]
        long afterSeq = 0,
        [Description("单次返回的事件数量，默认 200，范围 1～500。")]
        int limit = 200,
        CancellationToken cancellationToken = default)
    {
        // 与服务端一致地限制单次读取量，避免一次拉取过多历史。
        var effectiveLimit = Math.Clamp(limit, 1, 500);
        return RunToolAsync(() => service.GetTimelineAsync(afterSeq, effectiveLimit, cancellationToken));
    }

    /// <summary>
    /// 注入一个实时动作块。
    /// </summary>
    /// <param name="chunks">1～6 个 33ms 动作键块。</param>
    /// <param name="absolute">1280×720 输出帧中的绝对光标位置；为空时使用相对位移。</param>
    /// <param name="seq">动作块序号，由调用方生成。</param>
    /// <param name="cancellationToken">MCP 请求取消令牌。</param>
    /// <returns>动作注入结果。</returns>
    [McpServerTool(Name = "bgi_live_actions", UseStructuredContent = true)]
    [Description("向实时会话注入一个 200ms 动作块（最多 6 个 33ms 键块），由执行器按节奏执行并自动释放按键。")]
    public Task<LiveActionAckDto> EnqueueActionsAsync(
        [Description("1～6 个 33ms 动作键块；每个键块最多 4 个按键。")]
        List<LiveActionChunkDto> chunks,
        [Description("1280×720 输出帧中的绝对光标位置；省略时使用相对位移。")]
        LiveAbsolutePointDto? absolute = null,
        [Description("动作块序号，由调用方生成。")]
        long? seq = null,
        CancellationToken cancellationToken = default)
    {
        return RunToolAsync(() => service.EnqueueActionsAsync(new LiveActionBlockDto
        {
            Seq = seq,
            Chunks = chunks,
            Absolute = absolute
        }, cancellationToken));
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
    /// 把业务异常转换为 MCP 工具错误结果，保留稳定错误码。
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
}
