using BetterGenshinImpact.GameTask.Common;
using BetterGenshinImpact.Helpers;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Live;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Runtime;
using Microsoft.Extensions.Hosting;
using System;
using System.Net;
using System.Threading;
using System.Threading.Tasks;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent;

/// <summary>在无界面宿主退出前禁止新工作，并等待当前运行环境安全解绑。</summary>
public sealed class HeadlessShutdownCoordinator
{
    /// <summary>本进程启动身份，防止旧请求关闭新的宿主。</summary>
    private readonly string _instanceId = Guid.NewGuid().ToString("N");
    /// <summary>并发停机请求只共享一次清理操作。</summary>
    private readonly object _gate = new();
    /// <summary>不可重入的清理任务。</summary>
    private Task? _drainTask;
    /// <summary>防止重复安排进程退出。</summary>
    private int _exitScheduled;
    /// <summary>运行中的控制状态。</summary>
    private readonly AgentControlState _control;
    /// <summary>向已有任务广播停止。</summary>
    private readonly TaskStopService _taskStop;
    /// <summary>释放适配器跟踪的输入。</summary>
    private readonly IBgiAgentCapabilityService _agent;
    /// <summary>实时会话。</summary>
    private readonly ILiveSessionService _live;
    /// <summary>当前游戏运行环境。</summary>
    private readonly IGameRuntimeAccessor _runtime;
    /// <summary>宿主生命周期。</summary>
    private readonly IHostApplicationLifetime _lifetime;

    /// <summary>创建无界面停机协调器。</summary>
    public HeadlessShutdownCoordinator(AgentControlState control, TaskStopService taskStop,
        IBgiAgentCapabilityService agent, ILiveSessionService live, IGameRuntimeAccessor runtime,
        IHostApplicationLifetime lifetime)
    {
        _control = control;
        _taskStop = taskStop;
        _agent = agent;
        _live = live;
        _runtime = runtime;
        _lifetime = lifetime;
    }

    /// <summary>获取可供 CLI 绑定的本进程状态。</summary>
    public object GetStatus()
    {
        RequireHeadless();
        if (!_lifetime.ApplicationStarted.IsCancellationRequested)
            throw new AgentException("HEADLESS_STARTING", "无界面宿主尚未就绪。", HttpStatusCode.ServiceUnavailable);
        return new { headless = true, processId = Environment.ProcessId, instanceId = _instanceId,
            state = _control.IsShuttingDown ? "stopping" : "ready" };
    }

    /// <summary>校验调用方看到的身份，等待当前会话完全清理后返回。</summary>
    public async Task<object> DrainAsync(HeadlessShutdownRequest? request, CancellationToken cancellationToken)
    {
        RequireHeadless();
        if (request is null || request.ExpectedProcessId != Environment.ProcessId
            || !string.Equals(request.ExpectedInstanceId, _instanceId, StringComparison.Ordinal))
            throw new AgentException("HEADLESS_IDENTITY_MISMATCH", "宿主身份已变化，拒绝关闭。", HttpStatusCode.Conflict);

        try
        {
            await EnsureDrainedAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception exception)
        {
            throw new AgentException("HEADLESS_DRAIN_FAILED", "宿主未能安全完成停机；不会安排进程退出。",
                HttpStatusCode.Conflict, exception is TimeoutException or OperationCanceledException ? "停机等待超时。" : null);
        }
        return new { status = "drained", processId = Environment.ProcessId, instanceId = _instanceId,
            liveStopped = true, runtimeStopped = true, shutdownScheduled = true };
    }

    /// <summary>进入停机并等待同一次资源排空，不负责退出进程。</summary>
    public Task EnsureDrainedAsync(CancellationToken cancellationToken = default)
    {
        RequireHeadless();
        Task drain;
        lock (_gate)
        {
            if (_drainTask is null)
            {
                // 锁内先阻断新工作；取消回调异步完成，不能拖住请求线程。
                var drainedStarts = _control.BeginPermanentShutdown();
                _drainTask = Task.Run(() => DrainCoreAsync(drainedStarts));
            }
            drain = _drainTask;
        }
        return drain.WaitAsync(cancellationToken);
    }

    /// <summary>只在 HTTP 响应结束后通知宿主退出。</summary>
    public void ScheduleExit()
    {
        lock (_gate)
        {
            if (_drainTask is not { IsCompletedSuccessfully: true })
                throw new InvalidOperationException("停机排空尚未完成，不能安排退出。");
        }
        if (Interlocked.Exchange(ref _exitScheduled, 1) == 0) _lifetime.StopApplication();
    }

    /// <summary>先停止输入和启动请求，再等待 Live 和任务锁，最后解绑运行环境。</summary>
    private async Task DrainCoreAsync(Task drainedStarts)
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(15));
        await Task.Run(() => _taskStop.StopAll(TaskStopReason.ApplicationShutdown))
            .WaitAsync(timeout.Token).ConfigureAwait(false);
        await drainedStarts.WaitAsync(timeout.Token).ConfigureAwait(false);
        await Task.Run(_agent.Stop).WaitAsync(timeout.Token).ConfigureAwait(false);
        await _live.StopAsync(timeout.Token).WaitAsync(timeout.Token).ConfigureAwait(false);
        await _live.WaitForBackgroundStopsAsync(timeout.Token).WaitAsync(timeout.Token).ConfigureAwait(false);
        await TaskControl.TaskSemaphore.WaitAsync(timeout.Token).ConfigureAwait(false);
        try
        {
            // 延迟完成的 OCR 可能仍持有帧副本并调用执行器，不能提前释放。
            await _live.WaitForBackgroundVisionAsync(timeout.Token).WaitAsync(timeout.Token).ConfigureAwait(false);
            await _runtime.StopAsync(timeout.Token).WaitAsync(timeout.Token).ConfigureAwait(false);
            if (_live.IsRunning || _runtime.IsRunning)
                throw new InvalidOperationException("Live 或运行环境仍未停止。");
        }
        finally
        {
            TaskControl.TaskSemaphore.Release();
        }
    }

    /// <summary>GUI 宿主不暴露本停机能力。</summary>
    private static void RequireHeadless()
    {
        if (!CommandLineOptions.Instance.IsHeadless)
            throw new AgentException("HEADLESS_ONLY", "该操作仅用于无界面宿主。", HttpStatusCode.NotFound);
    }
}

/// <summary>CLI 关闭请求必须绑定先前读到的进程与实例身份。</summary>
public sealed class HeadlessShutdownRequest
{
    /// <summary>宿主进程 ID。</summary>
    public int ExpectedProcessId { get; init; }
    /// <summary>宿主本次启动的随机实例 ID。</summary>
    public string? ExpectedInstanceId { get; init; }
}
