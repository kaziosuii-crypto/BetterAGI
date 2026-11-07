using BetterGenshinImpact.Helpers;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Models;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Routing;
using System;
using System.IO;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Http;

/// <summary>
/// 对外暴露智能体游戏能力的 HTTP 端点。
/// <para>
/// 端点由外部访问宿主映射，loopback 校验与 Bearer 鉴权由宿主中间件统一处理；
/// 本类只负责参数绑定、能力服务调用和统一的错误映射。
/// </para>
/// </summary>
public static class BgiAgentHttpEndpoints
{
    /// <summary>
    /// 将 <c>/api/v1/agent</c> 下的全部端点注册到外部访问服务。
    /// </summary>
    /// <param name="endpoints">外部访问子 WebApplication 的路由构建器。</param>
    public static void MapBgiAgentEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/v1/agent").RequireCors("ExternalAccess");

        if (CommandLineOptions.Instance.IsHeadless)
        {
            // 与启动时的随机实例身份绑定，避免旧客户端关闭新进程。
            group.MapGet("/headless/status", ([FromServices] HeadlessShutdownCoordinator coordinator, HttpContext context) =>
                GuardAsync(() =>
                {
                    context.Response.Headers.CacheControl = "no-store";
                    return Task.FromResult<IResult>(Results.Ok(coordinator.GetStatus()));
                }));
            group.MapPost("/headless/shutdown", (
                    [FromBody] HeadlessShutdownRequest? request,
                    [FromServices] HeadlessShutdownCoordinator coordinator,
                    HttpContext context) =>
                GuardAsync(async () =>
                {
                    context.Response.Headers.CacheControl = "no-store";
                    var result = await coordinator.DrainAsync(request, CancellationToken.None).ConfigureAwait(false);
                    context.Response.OnCompleted(() =>
                    {
                        coordinator.ScheduleExit();
                        return Task.CompletedTask;
                    });
                    return Results.Ok(result);
                }));
        }

        // 状态查询：调用方先读取能力是否启用、游戏运行环境是否就绪。
        group.MapGet("/status", ([FromServices] IBgiAgentCapabilityService service) =>
            GuardAsync(() => Task.FromResult<IResult>(Results.Ok(service.GetStatus()))));

        // 观察一帧：默认只返回 JSON 元数据，Base64 图像由请求显式开启。
        group.MapPost("/observe", (
                [FromBody] AgentObserveRequest? request,
                [FromServices] IBgiAgentCapabilityService service,
                CancellationToken cancellationToken) =>
            GuardAsync(async () =>
            {
                var result = await service
                    .ObserveAsync(request ?? new AgentObserveRequest(), cancellationToken)
                    .ConfigureAwait(false);
                return Results.Ok(result.Observation);
            }));

        // 截图：以图像二进制返回，并通过响应头附带帧、会话与 UI 类别，便于调用方串联会话事件。
        group.MapGet("/screenshot", (
                [FromServices] IBgiAgentCapabilityService service,
                HttpContext httpContext,
                string? sessionId = null,
                int maxWidth = AgentLimits.DefaultMaxImageWidth,
                string format = "png",
                int quality = 85,
                bool ocr = false,
                bool ui = true,
                bool save = true,
                string? name = null,
                CancellationToken cancellationToken = default) =>
            GuardAsync(async () =>
            {
                var request = new AgentObserveRequest
                {
                    SessionId = sessionId,
                    MaxWidth = maxWidth,
                    Format = format,
                    Quality = quality,
                    IncludeOcr = ocr,
                    IncludeUiState = ui,
                    SaveFrame = save,
                    Name = name,
                    // 二进制端点不重复携带 Base64 图像。
                    IncludeImageBase64 = false
                };

                var result = await service.ObserveAsync(request, cancellationToken).ConfigureAwait(false);

                // 帧标识与会话标识始终可用；UI 类别只在请求了 UI 分析时提供。
                httpContext.Response.Headers["X-Bgi-Frame-Id"] = result.Observation.FrameId;
                httpContext.Response.Headers["X-Bgi-Session"] = result.Observation.SessionId;
                if (result.Observation.Vision is { } vision)
                {
                    httpContext.Response.Headers["X-Bgi-Ui-Category"] = vision.UiCategory;
                }

                return Results.File(result.ImageBytes, result.MimeType);
            }));

        // 动作序列：执行键鼠操作，默认在序列结束后自动观察一帧。
        group.MapPost("/actions", (
                [FromBody] AgentActionRequest? request,
                [FromServices] IBgiAgentCapabilityService service,
                CancellationToken cancellationToken) =>
            GuardAsync(async () =>
            {
                var result = await service
                    .ExecuteAsync(request ?? new AgentActionRequest(), cancellationToken)
                    .ConfigureAwait(false);
                return Results.Ok(result);
            }));

        // 已保存截图：支持 Range 请求，便于调用方分段读取大图。
        group.MapGet("/frames/{sessionId}/{frameId}", (
                string sessionId,
                string frameId,
                [FromServices] IBgiAgentCapabilityService service) =>
            GuardAsync(() =>
            {
                var frame = service.GetStoredFrame(sessionId, frameId);
                if (frame is null || !File.Exists(frame.Path))
                {
                    return Task.FromResult<IResult>(Results.Json(
                        new AgentErrorDto
                        {
                            Code = "FRAME_NOT_FOUND",
                            Message = "指定的截图不存在或已被清理。"
                        },
                        statusCode: StatusCodes.Status404NotFound));
                }

                return Task.FromResult<IResult>(Results.File(
                    frame.Path,
                    frame.MimeType,
                    enableRangeProcessing: true));
            }));

        // 启动游戏运行环境；成功时运行环境与 BetterGI 实时触发器同时就绪。
        group.MapPost("/runtime/start", (
                [FromServices] IBgiAgentCapabilityService service,
                CancellationToken cancellationToken) =>
            GuardAsync(async () =>
            {
                var running = await service.StartRuntimeAsync(cancellationToken).ConfigureAwait(false);
                return Results.Ok(new { running });
            }));

        // 停止游戏运行环境并释放输入。
        group.MapPost("/runtime/stop", (
                [FromServices] IBgiAgentCapabilityService service,
                CancellationToken cancellationToken) =>
            GuardAsync(async () =>
            {
                await service.StopRuntimeAsync(cancellationToken).ConfigureAwait(false);
                return Results.NoContent();
            }));

        // 用户停止开关：拒绝后续输入，直到调用 /resume。
        group.MapPost("/stop", ([FromServices] IBgiAgentCapabilityService service) =>
            GuardAsync(() =>
            {
                service.Stop();
                return Task.FromResult<IResult>(Results.Ok(new { stopRequested = true }));
            }));

        // 恢复被停止的智能体操作。
        group.MapPost("/resume", ([FromServices] IBgiAgentCapabilityService service) =>
            GuardAsync(() =>
            {
                service.Resume();
                return Task.FromResult<IResult>(Results.Ok(new { stopRequested = false }));
            }));

        // 确保会话存在；请求体可只提供 sessionId、name 中的任意字段。
        group.MapPost("/sessions", (
                [FromBody] JsonObject? body,
                [FromServices] IBgiAgentCapabilityService service,
                CancellationToken cancellationToken) =>
            GuardAsync(async () =>
            {
                var session = await service
                    .CreateSessionAsync(
                        ReadOptionalString(body, "sessionId"),
                        ReadOptionalString(body, "name"),
                        cancellationToken)
                    .ConfigureAwait(false);
                return Results.Ok(session);
            }));

        // 列出全部会话。
        group.MapGet("/sessions", (
                [FromServices] IBgiAgentCapabilityService service,
                CancellationToken cancellationToken) =>
            GuardAsync(async () => Results.Ok(
                await service.ListSessionsAsync(cancellationToken).ConfigureAwait(false))));

        // 获取单个会话摘要。
        group.MapGet("/sessions/{sessionId}", (
                string sessionId,
                [FromServices] IBgiAgentCapabilityService service,
                CancellationToken cancellationToken) =>
            GuardAsync(async () => Results.Ok(
                await service.GetSessionAsync(sessionId, cancellationToken).ConfigureAwait(false))));

        // 获取恢复会话所需的信息：摘要、内存、最近事件与检查点。
        group.MapGet("/sessions/{sessionId}/resume", (
                string sessionId,
                [FromServices] IBgiAgentCapabilityService service,
                int limit = 50,
                CancellationToken cancellationToken = default) =>
            GuardAsync(async () => Results.Ok(
                await service.GetResumeAsync(sessionId, limit, cancellationToken).ConfigureAwait(false))));

        // 按序号增量读取会话事件。
        group.MapGet("/sessions/{sessionId}/events", (
                string sessionId,
                [FromServices] IBgiAgentCapabilityService service,
                long afterSeq = 0,
                int limit = 100,
                CancellationToken cancellationToken = default) =>
            GuardAsync(async () =>
            {
                // 单次事件读取量限制在 1～500 之间，避免一次请求拉取过多历史。
                var effectiveLimit = Math.Clamp(limit, 1, 500);
                return Results.Ok(await service
                    .GetEventsAsync(sessionId, afterSeq, effectiveLimit, cancellationToken)
                    .ConfigureAwait(false));
            }));

        // 读取会话内存便签。
        group.MapGet("/sessions/{sessionId}/memory", (
                string sessionId,
                [FromServices] IBgiAgentCapabilityService service,
                CancellationToken cancellationToken) =>
            GuardAsync(async () => Results.Ok(
                await service.GetMemoryAsync(sessionId, cancellationToken).ConfigureAwait(false))));

        // 合并会话内存；值为 null 的键由存储层删除。
        group.MapPost("/sessions/{sessionId}/memory", (
                string sessionId,
                [FromBody] JsonObject? body,
                [FromServices] IBgiAgentCapabilityService service,
                CancellationToken cancellationToken) =>
            GuardAsync(async () => Results.Ok(await service
                .MergeMemoryAsync(sessionId, body ?? new JsonObject(), cancellationToken)
                .ConfigureAwait(false))));

        // 删除单个内存键。
        group.MapDelete("/sessions/{sessionId}/memory/{key}", (
                string sessionId,
                string key,
                [FromServices] IBgiAgentCapabilityService service,
                CancellationToken cancellationToken) =>
            GuardAsync(async () => Results.Ok(await service
                .DeleteMemoryAsync(sessionId, key, cancellationToken)
                .ConfigureAwait(false))));

        // 创建检查点：名称为可选字段，未提供时使用默认名称。
        group.MapPost("/sessions/{sessionId}/checkpoints", (
                string sessionId,
                [FromBody] JsonObject? body,
                [FromServices] IBgiAgentCapabilityService service,
                CancellationToken cancellationToken) =>
            GuardAsync(async () =>
            {
                var name = ReadOptionalString(body, "name");
                if (string.IsNullOrWhiteSpace(name))
                {
                    name = "checkpoint";
                }

                var checkpoint = await service
                    .CreateCheckpointAsync(sessionId, name, ReadOptionalString(body, "note"), cancellationToken)
                    .ConfigureAwait(false);
                return Results.Ok(checkpoint);
            }));

        // 列出会话的检查点。
        group.MapGet("/sessions/{sessionId}/checkpoints", (
                string sessionId,
                [FromServices] IBgiAgentCapabilityService service,
                CancellationToken cancellationToken) =>
            GuardAsync(async () => Results.Ok(
                await service.ListCheckpointsAsync(sessionId, cancellationToken).ConfigureAwait(false))));
    }

    /// <summary>
    /// 统一执行端点逻辑，并把可预期异常转换为结构化 HTTP 响应。
    /// </summary>
    /// <param name="action">实际的端点处理逻辑。</param>
    /// <returns>端点返回值或错误响应。</returns>
    private static async Task<IResult> GuardAsync(Func<Task<IResult>> action)
    {
        try
        {
            return await action().ConfigureAwait(false);
        }
        catch (AgentException exception)
        {
            // 业务异常携带稳定错误码和建议状态码，直接透传给调用方。
            return Results.Json(
                new AgentErrorDto
                {
                    Code = exception.Code,
                    Message = exception.Message,
                    Details = exception.Details
                },
                statusCode: (int)exception.StatusCode);
        }
        catch (OperationCanceledException)
        {
            // 请求被客户端取消或超时：返回 408，不写响应体。
            return Results.StatusCode(StatusCodes.Status408RequestTimeout);
        }
        catch (Exception exception)
        {
            // 未预期异常统一映射为 INTERNAL_ERROR；本机回环调用可返回异常消息便于排查。
            return Results.Json(
                new AgentErrorDto
                {
                    Code = "INTERNAL_ERROR",
                    Message = "处理智能体请求时发生内部错误。",
                    Details = exception.Message
                },
                statusCode: StatusCodes.Status500InternalServerError);
        }
    }

    /// <summary>
    /// 从可选的 JSON 请求体中读取字符串字段，字段缺失或类型不符时返回 <c>null</c>。
    /// </summary>
    /// <param name="body">请求体；允许为空。</param>
    /// <param name="propertyName">字段名。</param>
    /// <returns>字段值或 <c>null</c>。</returns>
    private static string? ReadOptionalString(JsonObject? body, string propertyName)
    {
        return body?[propertyName] is JsonValue value && value.TryGetValue<string>(out var text) ? text : null;
    }
}
