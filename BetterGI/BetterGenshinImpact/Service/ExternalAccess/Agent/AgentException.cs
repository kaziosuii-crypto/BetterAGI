using System;
using System.Net;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent;

/// <summary>
/// 智能体适配器可预期的业务异常，携带稳定的错误码和 HTTP 状态码。
/// </summary>
public sealed class AgentException : Exception
{
    /// <summary>
    /// 创建智能体适配器异常。
    /// </summary>
    /// <param name="code">稳定的错误码，供调用方分支处理。</param>
    /// <param name="message">面向调用方的错误说明。</param>
    /// <param name="statusCode">建议返回的 HTTP 状态码。</param>
    /// <param name="details">可选的补充信息。</param>
    public AgentException(string code, string message, HttpStatusCode statusCode, string? details = null)
        : base(message)
    {
        Code = code;
        StatusCode = statusCode;
        Details = details;
    }

    /// <summary>
    /// 稳定的错误码。
    /// </summary>
    public string Code { get; }

    /// <summary>
    /// 建议返回的 HTTP 状态码。
    /// </summary>
    public HttpStatusCode StatusCode { get; }

    /// <summary>
    /// 可选的补充信息。
    /// </summary>
    public string? Details { get; }

    /// <summary>
    /// 运行环境或游戏窗口不可用的统一异常。
    /// </summary>
    /// <param name="details">补充说明。</param>
    public static AgentException RuntimeUnavailable(string? details = null)
    {
        return new AgentException(
            "RUNTIME_UNAVAILABLE",
            "游戏运行环境尚未启动或已经停止。请先在 BetterGI 首页启动截图器，或调用 /api/v1/agent/runtime/start。",
            HttpStatusCode.Conflict,
            details);
    }

    /// <summary>
    /// 请求参数或动作序列不合法。
    /// </summary>
    /// <param name="message">错误说明。</param>
    /// <param name="details">补充说明。</param>
    public static AgentException InvalidRequest(string message, string? details = null)
    {
        return new AgentException("INVALID_REQUEST", message, HttpStatusCode.BadRequest, details);
    }

    /// <summary>
    /// 智能体控制能力未启用。
    /// </summary>
    public static AgentException Disabled()
    {
        return new AgentException(
            "AGENT_CONTROL_DISABLED",
            "智能体控制能力未启用。请在设置页启用“本机外部访问服务”中的智能体控制开关，并重启 BetterGI。",
            HttpStatusCode.Forbidden);
    }

    /// <summary>
    /// 用户已按下停止开关，必须先恢复后才能继续发送输入。
    /// </summary>
    public static AgentException Stopped()
    {
        return new AgentException(
            "AGENT_STOPPED",
            "智能体操作已被停止。请调用 /api/v1/agent/resume 后再继续。",
            HttpStatusCode.Conflict);
    }

    /// <summary>
    /// BetterGI 正在执行用户任务，拒绝并发输入。
    /// </summary>
    public static AgentException TaskRunning()
    {
        return new AgentException(
            "BETTERGI_TASK_RUNNING",
            "BetterGI 正在执行其他任务，智能体动作已拒绝。请等待任务结束或先停止任务。",
            HttpStatusCode.Conflict);
    }
}
