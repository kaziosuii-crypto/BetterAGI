using BetterGenshinImpact.GameTask.Runtime;
using System;
using System.Net;
using System.Threading;
using System.Threading.Tasks;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent;

/// <summary>
/// 智能体操作的停止开关。停止会取消当前动作序列，并在显式恢复前拒绝新的输入。
/// </summary>
public sealed class AgentControlState : IDisposable, IGameRuntimeStartBarrier
{
    /// <summary>
    /// 保护停止标记和取消源。
    /// </summary>
    private readonly object _gate = new();

    /// <summary>
    /// 当前会话的取消源；每次恢复都会替换。
    /// </summary>
    private CancellationTokenSource _sessionCancellation = new();

    /// <summary>
    /// 是否已请求停止。
    /// </summary>
    private bool _stopRequested;

    /// <summary>宿主停机一旦开始便不可恢复。</summary>
    private bool _shutdownRequested;

    /// <summary>停机前已进入的 runtime 和 Live 启动操作数。</summary>
    private int _startingOperations;

    /// <summary>最后一个启动操作退出后完成。</summary>
    private TaskCompletionSource<bool>? _startsDrained;

    /// <summary>
    /// 用户是否已按下停止开关。
    /// </summary>
    public bool IsStopRequested
    {
        get
        {
            lock (_gate)
            {
                return _stopRequested;
            }
        }
    }

    /// <summary>停机后拒绝任何新启动，并等待已进入的启动完成。</summary>
    public Task BeginPermanentShutdown()
    {
        CancellationTokenSource? cancel = null;
        Task drained;
        lock (_gate)
        {
            if (!_shutdownRequested)
            {
                _shutdownRequested = true;
                _stopRequested = true;
                cancel = _sessionCancellation;
            }
            drained = _startsDrained?.Task ?? Task.CompletedTask;
        }
        var cancellation = cancel?.CancelAsync() ?? Task.CompletedTask;
        return Task.WhenAll(drained, cancellation);
    }

    /// <summary>宿主已经进入不可恢复的停机阶段。</summary>
    public bool IsShuttingDown
    {
        get { lock (_gate) return _shutdownRequested; }
    }

    /// <summary>登记启动操作，避免停机与已在途的启动交错。</summary>
    internal IDisposable EnterStartOperation()
    {
        lock (_gate)
        {
            if (_shutdownRequested) throw ShuttingDown();
            if (_startingOperations++ == 0)
                _startsDrained = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            return new StartOperation(this);
        }
    }

    /// <inheritdoc />
    IDisposable IGameRuntimeStartBarrier.EnterStartOperation() => EnterStartOperation();

    /// <summary>释放一次启动登记，并通知等待停机的调用方。</summary>
    private void ExitStartOperation()
    {
        lock (_gate)
        {
            if (--_startingOperations == 0) _startsDrained!.TrySetResult(true);
        }
    }

    /// <summary>统一拒绝停机后的新工作。</summary>
    private static AgentException ShuttingDown() => new(
        "HEADLESS_SHUTTING_DOWN", "无界面宿主正在退出，无法恢复或启动任务。", HttpStatusCode.Conflict);

    /// <summary>确保每次启动登记只释放一次。</summary>
    private sealed class StartOperation(AgentControlState owner) : IDisposable
    {
        private AgentControlState? _owner = owner;
        public void Dispose() => Interlocked.Exchange(ref _owner, null)?.ExitStartOperation();
    }

    /// <summary>
    /// 请求停止：取消当前操作并拒绝后续输入。
    /// </summary>
    public void RequestStop()
    {
        lock (_gate)
        {
            _stopRequested = true;
            _sessionCancellation.Cancel();
        }
    }

    /// <summary>
    /// 恢复操作：清除停止标记并创建新的取消源。
    /// </summary>
    public void Resume()
    {
        lock (_gate)
        {
            if (_shutdownRequested) throw ShuttingDown();
            _stopRequested = false;
            // 旧的取消源可能仍被正在收尾的动作序列链接，交给 GC 回收，避免在取消回调完成前释放。
            _sessionCancellation = new CancellationTokenSource();
        }
    }

    /// <summary>
    /// 为一次动作序列创建取消令牌。已停止时抛出异常。
    /// </summary>
    /// <param name="requestCancellationToken">调用方请求的取消令牌。</param>
    /// <returns>与停止开关联动的取消源，调用方负责释放。</returns>
    public CancellationTokenSource CreateLinkedTokenSource(CancellationToken requestCancellationToken)
    {
        lock (_gate)
        {
            if (_stopRequested)
            {
                throw AgentException.Stopped();
            }

            return CancellationTokenSource.CreateLinkedTokenSource(
                requestCancellationToken,
                _sessionCancellation.Token);
        }
    }

    /// <summary>
    /// 释放取消源。
    /// </summary>
    public void Dispose()
    {
        lock (_gate)
        {
            _sessionCancellation.Dispose();
        }
    }
}
