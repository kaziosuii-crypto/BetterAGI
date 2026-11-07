using BetterGenshinImpact.Service.ExternalAccess.Agent.Models;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Routing;
using System;
using System.Globalization;
using System.Threading;
using System.Threading.Tasks;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Live;

/// <summary>
/// 对外暴露实时会话的 HTTP 端点。
/// <para>
/// 端点由外部访问宿主映射，loopback 校验与 Bearer 鉴权由宿主中间件统一处理；
/// 本类只负责参数绑定、实时会话服务调用和统一的错误映射。
/// </para>
/// </summary>
public static class LiveHttpEndpoints
{
    /// <summary>
    /// 将 <c>/api/v1/agent/live</c> 下的全部端点注册到外部访问服务。
    /// </summary>
    /// <param name="endpoints">外部访问子 WebApplication 的路由构建器。</param>
    public static void MapLiveEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/v1/agent/live").RequireCors("ExternalAccess");

        // 启动实时会话：需要游戏截图器已经运行；重复调用返回当前状态。
        group.MapPost("/start", (
                [FromBody] LiveStartRequest? request,
                [FromServices] ILiveSessionService service,
                CancellationToken cancellationToken) =>
            GuardAsync(async () => Results.Ok(await service
                .StartAsync(request ?? new LiveStartRequest(), cancellationToken)
                .ConfigureAwait(false))));

        // 停止实时会话：释放仍按下的按键并清空动作队列。
        group.MapPost("/stop", (
                [FromServices] ILiveSessionService service,
                CancellationToken cancellationToken) =>
            GuardAsync(async () =>
            {
                await service.StopAsync(cancellationToken).ConfigureAwait(false);
                return Results.NoContent();
            }));

        // 状态查询：包含能力版本和当前安全观测，不构成未来动作授权。
        group.MapGet("/status", ([FromServices] ILiveSessionService service) =>
            GuardAsync(() => Task.FromResult<IResult>(Results.Ok(service.GetStatus()))));

        // 最新画面帧：以 JPEG 二进制返回，并通过响应头附带帧序号、时间与游戏画面区域。
        group.MapGet("/frame", (
                [FromServices] ILiveSessionService service,
                HttpContext httpContext) =>
            GuardAsync(() =>
            {
                var frame = service.GetLatestFrame();
                if (frame is null)
                {
                    return Task.FromResult<IResult>(Results.Json(
                        new AgentErrorDto
                        {
                            Code = "LIVE_FRAME_NOT_FOUND",
                            Message = "实时会话尚未产生画面帧。请先启动会话并等待采集。"
                        },
                        statusCode: StatusCodes.Status404NotFound));
                }

                // 这些响应头让调用方无需解码 JPEG 即可关联帧和时间线事件。
                httpContext.Response.Headers["X-Bgi-Live-Seq"] =
                    frame.Seq.ToString(CultureInfo.InvariantCulture);
                httpContext.Response.Headers["X-Bgi-Live-Timestamp"] =
                    frame.TimestampUtc.ToString("O", CultureInfo.InvariantCulture);
                // 与响应 JPEG 是同一帧；unknown 不能解释为 false。
                httpContext.Response.Headers["X-Bgi-Ui-Category"] = frame.UiCategory;
                httpContext.Response.Headers["X-Bgi-Is-In-Talk-Ui"] =
                    frame.IsInTalkUi is bool inTalk ? (inTalk ? "true" : "false") : "unknown";
                httpContext.Response.Headers["X-Bgi-Capture-X"] =
                    frame.Capture.X.ToString(CultureInfo.InvariantCulture);
                httpContext.Response.Headers["X-Bgi-Capture-Y"] =
                    frame.Capture.Y.ToString(CultureInfo.InvariantCulture);
                httpContext.Response.Headers["X-Bgi-Capture-Width"] =
                    frame.Capture.Width.ToString(CultureInfo.InvariantCulture);
                httpContext.Response.Headers["X-Bgi-Capture-Height"] =
                    frame.Capture.Height.ToString(CultureInfo.InvariantCulture);

                return Task.FromResult<IResult>(Results.File(frame.JpegBytes, "image/jpeg"));
            }));

        // 设置最新指导文本并广播到 WebSocket 客户端。
        group.MapPost("/guidance", (
                [FromBody] LiveGuidanceRequest? request,
                [FromServices] ILiveSessionService service,
                CancellationToken cancellationToken) =>
            GuardAsync(async () => Results.Ok(await service
                .SetGuidanceAsync(request ?? new LiveGuidanceRequest(), cancellationToken)
                .ConfigureAwait(false))));

        // 追加一条解说并广播到 WebSocket 客户端。
        group.MapPost("/commentary", (
                [FromBody] LiveCommentaryRequest? request,
                [FromServices] ILiveSessionService service,
                CancellationToken cancellationToken) =>
            GuardAsync(async () => Results.Ok(await service
                .AddCommentaryAsync(request ?? new LiveCommentaryRequest(), cancellationToken)
                .ConfigureAwait(false))));

        // 按序号增量读取时间线；单次读取量限制在 1～500 之间。
        group.MapGet("/timeline", (
                [FromServices] ILiveSessionService service,
                long afterSeq = 0,
                int limit = 200,
                CancellationToken cancellationToken = default) =>
            GuardAsync(async () =>
            {
                var effectiveLimit = Math.Clamp(limit, 1, 500);
                return Results.Ok(await service
                    .GetTimelineAsync(afterSeq, effectiveLimit, cancellationToken)
                    .ConfigureAwait(false));
            }));

        // 租约只控制 Live 输入，紧急停止仍可由任意已认证调用方执行。
        group.MapPost("/lease/acquire", ([FromServices] ILiveSessionService service, HttpContext context) =>
            GuardAsync(() =>
            {
                context.Response.Headers.CacheControl = "no-store";
                return Task.FromResult<IResult>(Results.Ok(service.AcquireLease()));
            }));
        group.MapPost("/lease/commit", ([FromBody] LiveLeaseCommitRequest? request,
                [FromServices] ILiveSessionService service, HttpContext context) =>
            GuardAsync(() =>
            {
                context.Response.Headers.CacheControl = "no-store";
                var result = service.CommitPreparedLease(request ?? new LiveLeaseCommitRequest());
                return Task.FromResult<IResult>(result is null
                    ? Results.StatusCode(StatusCodes.Status403Forbidden) : Results.Ok(result));
            }));
        group.MapPost("/lease/renew", ([FromBody] LiveLeaseCredentialsDto? credentials,
                [FromServices] ILiveSessionService service) =>
            GuardAsync(() => Task.FromResult<IResult>(service.RenewLease(credentials)
                ? Results.Ok(new { renewed = true }) : Results.StatusCode(StatusCodes.Status403Forbidden))));
        group.MapPost("/lease/release", ([FromBody] LiveLeaseCredentialsDto? credentials,
                [FromServices] ILiveSessionService service) =>
            GuardAsync(() => Task.FromResult<IResult>(service.ReleaseLease(credentials)
                ? Results.Ok(new { released = true }) : Results.StatusCode(StatusCodes.Status403Forbidden))));

        // 对话决策仅在宿主同帧与租约成立时处理；不接受调用方的坐标。
        group.MapPost("/dialogue/decision", ([FromBody] LiveDialogueDecisionRequest? request,
                [FromServices] ILiveSessionService service, HttpContext context) =>
            GuardAsync(() => Task.FromResult<IResult>(Results.Ok(service.SubmitDialogueDecision(
                request ?? new LiveDialogueDecisionRequest(), ReadLeaseHeaders(context))))));

        // 注入一个动作块；执行器按 33ms 节奏消费并释放按键。
        group.MapPost("/actions", (
                [FromBody] LiveActionBlockDto? request,
                [FromServices] ILiveSessionService service,
                HttpContext context,
                CancellationToken cancellationToken) =>
            GuardAsync(async () => Results.Ok(await service
                .EnqueueActionsAsync(request ?? new LiveActionBlockDto(), cancellationToken, ReadLeaseHeaders(context))
                .ConfigureAwait(false))));
    }

    /// <summary>从请求头读取租约；不完整或重复的头部不能降级到无租约模式。</summary>
    private static LiveLeaseCredentialsDto? ReadLeaseHeaders(HttpContext context)
    {
        var headers = context.Request.Headers;
        var secret = headers["X-Bgi-Lease-Secret"];
        var instance = headers["X-Bgi-Lease-Instance"];
        var epoch = headers["X-Bgi-Lease-Epoch"];
        if (secret.Count == 0 && instance.Count == 0 && epoch.Count == 0) return null;
        if (secret.Count != 1 || instance.Count != 1 || epoch.Count != 1
            || !long.TryParse(epoch[0], NumberStyles.None, CultureInfo.InvariantCulture, out var parsed))
            return new LiveLeaseCredentialsDto();
        return new LiveLeaseCredentialsDto { Secret = secret[0], InstanceId = instance[0], Epoch = parsed };
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
                    Message = "处理实时会话请求时发生内部错误。",
                    Details = exception.Message
                },
                statusCode: StatusCodes.Status500InternalServerError);
        }
    }
}
