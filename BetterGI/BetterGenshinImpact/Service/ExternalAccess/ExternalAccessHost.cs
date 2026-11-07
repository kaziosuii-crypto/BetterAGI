using BetterGenshinImpact.Core.Config;
using BetterGenshinImpact.Helpers;
using BetterGenshinImpact.Service.ExternalAccess.Agent;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Http;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Live;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Mcp;
using BetterGenshinImpact.Service.ExternalAccess.Logging;
using BetterGenshinImpact.Service.ExternalAccess.Mcp;
using BetterGenshinImpact.Service.Instance;
using BetterGenshinImpact.Service.Interface;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using ModelContextProtocol.AspNetCore;
using ModelContextProtocol.Server;
using Serilog;
using System;
using System.Net;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace BetterGenshinImpact.Service.ExternalAccess;

/// <summary>
/// 在 BetterGI 主实例内托管仅监听 loopback 的 HTTP、WebSocket 与 MCP 服务。
/// </summary>
public sealed class ExternalAccessHost : IHostedService
{
    /// <summary>
    /// 全局配置读写服务。
    /// </summary>
    private readonly IConfigService _configService;

    /// <summary>
    /// BetterGI 多实例上下文服务。
    /// </summary>
    private readonly InstanceService _instanceService;

    /// <summary>
    /// 设置页面可观察的运行状态。
    /// </summary>
    private readonly ExternalAccessState _state;

    /// <summary>
    /// 访问令牌生成与校验服务。
    /// </summary>
    private readonly ExternalAccessTokenService _tokenService;

    /// <summary>
    /// 与传输协议无关的 BetterGI 能力服务。
    /// </summary>
    private readonly IBgiExternalCapabilityService _capabilityService;

    /// <summary>
    /// 实时日志分发器。
    /// </summary>
    private readonly ExternalLogHub _logHub;

    /// <summary>
    /// WebSocket 日志连接处理器。
    /// </summary>
    private readonly ExternalLogWebSocketHandler _logWebSocketHandler;

    /// <summary>
    /// 与传输协议无关的智能体游戏能力服务。
    /// </summary>
    private readonly IBgiAgentCapabilityService _agentCapabilityService;

    /// <summary>
    /// 实时会话服务，提供画面帧、时间线与动作注入。
    /// </summary>
    private readonly ILiveSessionService _liveSessionService;

    /// <summary>
    /// 实时会话 WebSocket 连接处理器。
    /// </summary>
    private readonly LiveWebSocketHandler _liveWebSocketHandler;

    /// <summary>无界面宿主停机协调，GUI 模式可为空。</summary>
    private readonly HeadlessShutdownCoordinator? _headlessShutdown;

    /// <summary>
    /// 外部访问服务自身的日志记录器。
    /// </summary>
    private readonly ILogger<ExternalAccessHost> _logger;

    /// <summary>
    /// 实际承载外部协议端点的子 WebApplication。
    /// </summary>
    private WebApplication? _webApplication;

    /// <summary>
    /// 创建 BetterGI 外部访问宿主。
    /// </summary>
    public ExternalAccessHost(
        IConfigService configService,
        InstanceService instanceService,
        ExternalAccessState state,
        ExternalAccessTokenService tokenService,
        IBgiExternalCapabilityService capabilityService,
        ExternalLogHub logHub,
        ExternalLogWebSocketHandler logWebSocketHandler,
        IBgiAgentCapabilityService agentCapabilityService,
        ILiveSessionService liveSessionService,
        LiveWebSocketHandler liveWebSocketHandler,
        ILogger<ExternalAccessHost> logger,
        HeadlessShutdownCoordinator? headlessShutdown = null)
    {
        _configService = configService;
        _instanceService = instanceService;
        _state = state;
        _tokenService = tokenService;
        _capabilityService = capabilityService;
        _logHub = logHub;
        _logWebSocketHandler = logWebSocketHandler;
        _agentCapabilityService = agentCapabilityService;
        _liveSessionService = liveSessionService;
        _liveWebSocketHandler = liveWebSocketHandler;
        _headlessShutdown = headlessShutdown;
        _logger = logger;
    }

    /// <summary>
    /// 按启动时配置创建并启动 loopback 子 WebApplication。
    /// </summary>
    /// <param name="cancellationToken">应用启动取消令牌。</param>
    public async Task StartAsync(CancellationToken cancellationToken)
    {
        if (!_instanceService.Context.IsRoot)
        {
            _state.Update(ExternalAccessServiceStatus.Disabled, "仅 Primary 实例可以启动外部访问服务", "未启动");
            return;
        }

        var config = _configService.Get().ExternalAccessConfig;
        var httpApiEnabled = config.HttpApiEnabled;
        var webSocketEnabled = config.WebSocketEnabled;
        var mcpEnabled = config.McpEnabled;
        var agentEnabled = config.AgentControlEnabled;
        var port = config.Port;

        if (!httpApiEnabled && !webSocketEnabled && !mcpEnabled && !agentEnabled)
        {
            _state.Update(ExternalAccessServiceStatus.Disabled, "外部访问服务未启用", "未启动");
            return;
        }

        if (port is < 1024 or > 65535)
        {
            _state.Update(ExternalAccessServiceStatus.Failed, "端口必须在 1024 到 65535 之间", "未启动");
            _logger.LogError("外部访问服务端口 {Port} 不合法", port);
            return;
        }

        var address = $"http://127.0.0.1:{port}";
        _state.Update(ExternalAccessServiceStatus.Starting, "正在启动本机外部访问服务", address);

        try
        {
            // 令牌生成和持久化也属于服务启动流程，失败时只更新状态而不终止 WPF 主程序。
            // 智能体实时会话 WebSocket 需要 Bearer 令牌，因此启用智能体时也必须确保令牌存在。
            if (httpApiEnabled || mcpEnabled || agentEnabled)
            {
                _tokenService.EnsureToken();
            }

            _webApplication = BuildApplication(port, httpApiEnabled, webSocketEnabled, mcpEnabled, agentEnabled);
            await _webApplication.StartAsync(cancellationToken).ConfigureAwait(false);
            _state.Update(ExternalAccessServiceStatus.Running, "配置开关和端口将在下次重启时重新加载", address);
            _logger.LogInformation(
                "外部访问服务已启动：{Address}，HTTP={HttpEnabled}，WebSocket={WebSocketEnabled}，MCP={McpEnabled}，Agent={AgentEnabled}",
                address,
                httpApiEnabled,
                webSocketEnabled,
                mcpEnabled,
                agentEnabled);
        }
        catch (Exception exception) when (exception is not OperationCanceledException || !cancellationToken.IsCancellationRequested)
        {
            _state.Update(ExternalAccessServiceStatus.Failed, exception.Message, address);
            _logger.LogError(exception, "外部访问服务启动失败：{Address}", address);
            try
            {
                await DisposeWebApplicationAsync().ConfigureAwait(false);
            }
            catch (Exception disposeException)
            {
                // 失败宿主的清理异常也不能中断 WPF 主程序启动。
                _logger.LogWarning(disposeException, "释放启动失败的外部访问服务时发生异常");
            }
        }
    }

    /// <summary>
    /// 停止并释放外部访问子 WebApplication。
    /// </summary>
    /// <param name="cancellationToken">应用停止取消令牌。</param>
    public async Task StopAsync(CancellationToken cancellationToken)
    {
        if (_webApplication is not null)
        {
            if (CommandLineOptions.Instance.IsHeadless)
            {
                // 停止未完成时保留子宿主，不能把取消当作资源已释放。
                await _webApplication.StopAsync(cancellationToken).ConfigureAwait(false);
                await DisposeWebApplicationAsync().ConfigureAwait(false);
            }
            else
            {
                try
                {
                    await _webApplication.StopAsync(cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
                {
                    // 主程序正在强制结束，继续释放子宿主。
                }
                finally
                {
                    await DisposeWebApplicationAsync().ConfigureAwait(false);
                }
            }
        }

        if (_state.Status != ExternalAccessServiceStatus.Disabled)
        {
            _state.Update(ExternalAccessServiceStatus.Stopped, "外部访问服务已停止", "未启动");
        }
    }

    /// <summary>
    /// 创建并配置承载外部协议的子 WebApplication。
    /// </summary>
    /// <param name="port">loopback 监听端口。</param>
    /// <param name="httpApiEnabled">是否映射 HTTP API。</param>
    /// <param name="webSocketEnabled">是否映射 WebSocket 日志。</param>
    /// <param name="mcpEnabled">是否映射 MCP。</param>
    /// <param name="agentEnabled">是否启用智能体控制能力。</param>
    /// <returns>尚未启动的 WebApplication。</returns>
    private WebApplication BuildApplication(
        int port,
        bool httpApiEnabled,
        bool webSocketEnabled,
        bool mcpEnabled,
        bool agentEnabled)
    {
        var builder = WebApplication.CreateBuilder(new WebApplicationOptions
        {
            Args = [],
            ApplicationName = typeof(ExternalAccessHost).Assembly.GetName().Name,
            ContentRootPath = AppContext.BaseDirectory,
            EnvironmentName = Environments.Production
        });

        builder.Logging.ClearProviders();
        builder.Logging.AddSerilog(Log.Logger, dispose: false);
        builder.WebHost.ConfigureKestrel(options =>
        {
            options.ListenLocalhost(port);
            // 外部访问只接收小型 JSON 请求，限制请求体可避免超大内存/名称耗尽磁盘与内存。
            options.Limits.MaxRequestBodySize = 4 * 1024 * 1024;
        });

        builder.Services.ConfigureHttpJsonOptions(options =>
        {
            options.SerializerOptions.PropertyNamingPolicy = JsonNamingPolicy.CamelCase;
        });
        builder.Services.AddCors(options => options.AddPolicy(
            "ExternalAccess",
            policy => policy.AllowAnyOrigin().AllowAnyHeader().AllowAnyMethod()));
        builder.Services.AddSingleton(_capabilityService);
        builder.Services.AddSingleton(_tokenService);
        builder.Services.AddSingleton(_logHub);
        builder.Services.AddSingleton(_logWebSocketHandler);

        // 智能体能力服务仅在开关启用时注入子宿主，关闭时其依赖链路完全不参与外部访问。
        if (agentEnabled)
        {
            builder.Services.AddSingleton(_agentCapabilityService);
            // 实时会话服务与 WebSocket 处理器由父容器持有，子宿主只借用现有实例；
            // 关闭智能体开关时不注册，实时会话端点与 MCP 工具保持不可用。
            builder.Services.AddSingleton(_liveSessionService);
            builder.Services.AddSingleton(_liveWebSocketHandler);
            if (_headlessShutdown is not null) builder.Services.AddSingleton(_headlessShutdown);
        }

        if (mcpEnabled)
        {
            var mcpServerBuilder = builder.Services.AddMcpServer()
                .WithHttpTransport(options => options.SessionMode = HttpServerSessionMode.Stateless)
                .WithTools<BgiMcpTools>();

            // 智能体工具只在开关启用时注册，避免关闭后仍可截屏或模拟键鼠。
            if (agentEnabled)
            {
                mcpServerBuilder.WithTools<BgiAgentMcpTools>();
                mcpServerBuilder.WithTools<LiveMcpTools>();
            }
        }

        var app = builder.Build();
        ConfigurePipeline(app, httpApiEnabled, webSocketEnabled, mcpEnabled, agentEnabled);
        return app;
    }

    /// <summary>
    /// 配置 Host 校验、CORS、Bearer 鉴权及已启用的协议端点。
    /// </summary>
    /// <param name="app">外部访问子 WebApplication。</param>
    /// <param name="httpApiEnabled">是否映射 HTTP API。</param>
    /// <param name="webSocketEnabled">是否映射 WebSocket 日志。</param>
    /// <param name="mcpEnabled">是否映射 MCP。</param>
    /// <param name="agentEnabled">是否映射智能体控制端点。</param>
    private void ConfigurePipeline(
        WebApplication app,
        bool httpApiEnabled,
        bool webSocketEnabled,
        bool mcpEnabled,
        bool agentEnabled)
    {
        app.Use(async (context, next) =>
        {
            if (!IsLoopbackHost(context.Request.Host.Host))
            {
                context.Response.StatusCode = StatusCodes.Status400BadRequest;
                await context.Response.WriteAsync("Host 必须是 loopback 地址。", context.RequestAborted);
                return;
            }

            await next(context);
        });

        app.UseRouting();
        // CORS 策略通过端点元数据按需应用，未启用的协议路径仍由框架返回 404。
        app.UseCors();
        app.Use(async (context, next) =>
        {
            if (HttpMethods.IsOptions(context.Request.Method)
                || !RequiresBearerToken(context.Request.Path, httpApiEnabled, mcpEnabled, agentEnabled))
            {
                await next(context);
                return;
            }

            var authorized = _tokenService.IsAuthorized(context.Request.Headers.Authorization);

            // 浏览器 WebSocket API 无法设置 Authorization 头，因此仅智能体实时会话允许查询参数令牌。
            // 风险：令牌会出现在 URL、访问日志与浏览器历史中；服务只监听 loopback，且令牌可在设置页轮换。
            if (!authorized
                && agentEnabled
                && context.Request.Path.StartsWithSegments("/ws/v1/agent", StringComparison.OrdinalIgnoreCase)
                && context.Request.Query.TryGetValue("access_token", out var queryToken))
            {
                authorized = _tokenService.IsAuthorized($"Bearer {queryToken}");
            }

            if (!authorized)
            {
                context.Response.StatusCode = StatusCodes.Status401Unauthorized;
                context.Response.Headers["WWW-Authenticate"] = "Bearer";
                return;
            }

            await next(context);
        });

        if (httpApiEnabled)
        {
            app.MapGet("/api/v1/health", async (CancellationToken cancellationToken) =>
                    Results.Ok(await _capabilityService.GetHealthAsync(cancellationToken).ConfigureAwait(false)))
                .RequireCors("ExternalAccess");
            app.MapGet("/api/v1/version", async (CancellationToken cancellationToken) =>
                    Results.Ok(await _capabilityService.GetVersionAsync(cancellationToken).ConfigureAwait(false)))
                .RequireCors("ExternalAccess");
        }

        if (httpApiEnabled && agentEnabled)
        {
            // 智能体端点仅在 HTTP API 与智能体开关同时启用时映射；关闭时保持 404。
            app.MapBgiAgentEndpoints();
            // 实时会话控制端点与智能体 HTTP API 同开关。
            app.MapLiveEndpoints();
        }

        // 智能体实时会话 WebSocket 只依赖智能体开关；与日志 WebSocket 共用一次 UseWebSockets 注册。
        var webSocketsConfigured = false;
        if (agentEnabled)
        {
            app.UseWebSockets(new WebSocketOptions { KeepAliveInterval = TimeSpan.FromSeconds(30) });
            webSocketsConfigured = true;
            app.Map("/ws/v1/agent/live", _liveWebSocketHandler.HandleAsync);
        }

        if (webSocketEnabled)
        {
            // AllowedOrigins 为空表示按产品约定允许所有 Origin；该端点也明确不使用 Bearer 鉴权。
            if (!webSocketsConfigured)
            {
                app.UseWebSockets(new WebSocketOptions { KeepAliveInterval = TimeSpan.FromSeconds(30) });
            }

            app.Map("/ws/v1/logs", _logWebSocketHandler.HandleAsync);
        }

        if (mcpEnabled)
        {
            app.MapMcp("/mcp").RequireCors("ExternalAccess");
        }
    }

    /// <summary>
    /// 判断请求 Host 是否为 localhost 或 loopback IP，阻止 DNS rebinding Host。
    /// </summary>
    /// <param name="host">HTTP Host 中不含端口的主机部分。</param>
    /// <returns>是否为本机回环地址。</returns>
    private static bool IsLoopbackHost(string host)
    {
        return host.Equals("localhost", StringComparison.OrdinalIgnoreCase)
               || IPAddress.TryParse(host, out var address) && IPAddress.IsLoopback(address);
    }

    /// <summary>
    /// 判断当前路径是否属于已启用且需要 Bearer 令牌的 HTTP API、MCP 或智能体实时 WebSocket。
    /// </summary>
    /// <param name="path">请求路径。</param>
    /// <param name="httpApiEnabled">HTTP API 是否已映射。</param>
    /// <param name="mcpEnabled">MCP 是否已映射。</param>
    /// <param name="agentEnabled">智能体能力是否已启用。</param>
    /// <returns>是否需要鉴权。</returns>
    private static bool RequiresBearerToken(PathString path, bool httpApiEnabled, bool mcpEnabled, bool agentEnabled)
    {
        return httpApiEnabled && path.StartsWithSegments("/api", StringComparison.OrdinalIgnoreCase)
               || mcpEnabled && path.StartsWithSegments("/mcp", StringComparison.OrdinalIgnoreCase)
               // 日志 WebSocket 保持无鉴权；只有智能体实时会话需要 Bearer 令牌。
               || agentEnabled && path.StartsWithSegments("/ws/v1/agent", StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>
    /// 释放当前子 WebApplication 并清空引用。
    /// </summary>
    private async ValueTask DisposeWebApplicationAsync()
    {
        var app = _webApplication;
        _webApplication = null;
        if (app is not null)
        {
            await app.DisposeAsync().ConfigureAwait(false);
        }
    }
}
