using BetterGenshinImpact.Core.Input;
using BetterGenshinImpact.Core.Input.Backends.Win32;
using BetterGenshinImpact.Core.Simulator;
using BetterGenshinImpact.GameTask.Runtime;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Input;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Media;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Runtime;
using Microsoft.Extensions.Logging;
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Security.Cryptography;
using System.Threading;
using System.Threading.Tasks;
using Vanara.PInvoke;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Live;

/// <summary>
/// 实时动作执行器：把 Lumine 的 200ms 动作块按 33ms 键块排队执行，
/// 自动做按键差分、鼠标按下/抬起、相对/绝对移动与滚轮，并在停止/失去前台时释放全部按键。
/// <para>
/// 停止语义：停止请求先作废尚未执行的键块，再等待在途注入结束并释放全部按键；
/// 停止返回后不会再有任何键被按下，也不会补播过期动作。
/// </para>
/// </summary>
public sealed class LiveActionExecutor : IDisposable
{
    /// <summary>每个键块的持续毫秒数。</summary>
    public const int ChunkIntervalMs = 33;

    /// <summary>没有新动作时释放按键的等待毫秒数。</summary>
    private const int IdleReleaseMs = 250;

    /// <summary>超过该延迟的键块视为过期，不再补播。</summary>
    private const int ExpiredChunkToleranceMs = 200;

    /// <summary>游戏画面输出宽度基准。</summary>
    private const int ModelFrameWidth = 1280;

    /// <summary>游戏画面输出高度基准。</summary>
    private const int ModelFrameHeight = 720;

    /// <summary>运行环境访问器。</summary>
    private readonly IGameRuntimeAccessor _runtimeAccessor;

    /// <summary>智能体停止开关。</summary>
    private readonly AgentControlState _controlState;

    /// <summary>日志。</summary>
    private readonly ILogger<LiveActionExecutor> _logger;

    /// <summary>待执行键块。</summary>
    private readonly ConcurrentQueue<ScheduledChunk> _queue = new();

    /// <summary>当前按下的 token。</summary>
    private readonly Dictionary<string, LiveKeyAction> _pressed = new(StringComparer.Ordinal);

    /// <summary>保护按下状态字典。</summary>
    private readonly object _pressedLock = new();

    /// <summary>执行闸门：保证同一时刻只有一批动作在注入，停止需要等待在途注入结束。</summary>
    private readonly SemaphoreSlim _executionGate = new(1, 1);

    /// <summary>
    /// 后台输入模式：允许在游戏窗口不处于前台时用 PostMessage 通道投递按键。
    /// 只对移动按键生效；对话选择、绝对点击等需要真实前台焦点的动作仍然要求前台。
    /// </summary>
    private volatile bool _backgroundInput;

    /// <summary>仅仲裁本执行器的 Live 输入，不隔离其他进程或应用输入路径。</summary>
    private const int LeaseTtlMs = 3000;
    /// <summary>执行器生命周期内不可复用的实例身份。</summary>
    private readonly string _leaseInstance = Guid.NewGuid().ToString("N");
    /// <summary>租约模式一旦启用，对本次会话保持强制认证。</summary>
    private bool _leaseRequired;
    /// <summary>当前租约绑定的会话对象。</summary>
    private object? _leaseSession;
    /// <summary>当前动作凭据和仅玩家持有的续期凭据摘要。</summary>
    private byte[]? _leaseHash, _leaseOwnerHash;
    /// <summary>预备租约不能注入；仅 owner 提交后变为 active。</summary>
    private bool _leaseActive;
    /// <summary>Owner 已批准提交，但 Bridge 尚未通过绑定连接激活。</summary>
    private bool _leaseOwnerCommitted;
    /// <summary>与预备租约绑定的请求及 Live WebSocket 连接。</summary>
    private string? _leaseBindingRequestId, _leaseBindingConnectionId;
    /// <summary>租约代次和单调时钟期限。</summary>
    private long _leaseEpoch, _leaseDeadline;
    /// <summary>正在执行的键块所绑定的租约代次。</summary>
    private long _executingLeaseEpoch;

    /// <summary>保护执行循环的启动与停止。</summary>
    private readonly object _loopLock = new();

    /// <summary>是否已请求停止：执行循环据此放弃本批剩余键块。</summary>
    private volatile bool _stopRequested;

    /// <summary>尚未取得注入闸门的显式停止请求。</summary>
    private int _pendingStopRequests;

    /// <summary>动作代次：每次停止或替换动作块都会递增，旧代次的键块不再执行。</summary>
    private long _epoch;

    /// <summary>由会话提供的截图新鲜度检查，不得获取会话锁。</summary>
    private Func<bool>? _safetyCheck;

    /// <summary>存在尚未成功释放的输入时不得继续注入。</summary>
    private volatile bool _releaseFailure;

    /// <summary>发生过无法由输入后端证明最终释放的按下请求。</summary>
    private bool _releaseProofUnavailable;

    /// <summary>是否完成过释放确认；仅在执行闸门内访问。</summary>
    private bool _releaseConfirmed;

    /// <summary>至少一个受跟踪按键的释放请求已由系统接受；仅在执行闸门内访问。</summary>
    private bool _systemReleaseAccepted;

    /// <summary>存在无法补证的其他后端、部分成功或异常；仅在执行闸门内访问。</summary>
    private bool _systemReleaseEvidenceUnavailable;

    /// <summary>系统释放快照及其所属会话；仅在执行闸门内访问。</summary>
    private LiveSystemReleaseReceiptDto? _systemReleaseReceipt;
    /// <summary>回执绑定的会话对象，避免会话标识重复时继承旧结果。</summary>
    private object? _systemReleaseSession;
    /// <summary>每次非释放输入尝试递增，令此前的释放快照失效。</summary>
    private long _inputGeneration;

    /// <summary>执行循环取消源。</summary>
    private CancellationTokenSource? _cancellation;

    /// <summary>执行循环任务。</summary>
    private Task? _loop;

    /// <summary>最近一次收到动作块的时间（单调时钟）。</summary>
    private long _lastEnqueueTicks;

    /// <summary>是否处于饥饿状态。</summary>
    private volatile bool _starved = true;

    /// <summary>已执行动作块数量。</summary>
    private long _applied;

    /// <summary>已丢弃动作块数量。</summary>
    private long _dropped;

    /// <summary>本地键块处理记录；订阅方只能执行非阻塞入队。</summary>
    internal event Action<LiveExecutionAttemptDto>? ExecutionAttempt;

    /// <summary>逐次输入回执；Accepted 仅表示系统接受，不表示游戏响应。订阅方只能非阻塞入队，不能调用执行器。</summary>
    public event Action<LiveInputEvidence>? InputEvidence;

    /// <summary>当前处理的键块归属；释放重试可能不属于任何键块。</summary>
    private AttemptAttribution? _currentAttribution;
    /// <summary>当前处理的键块序号。</summary>
    private int _currentChunkIndex;

    /// <summary>是否正在统计当前键块的输入调用。</summary>
    private bool _countInputCalls;
    /// <summary>当前键块开始的调用数。</summary>
    private int _callsStarted;
    /// <summary>当前键块正常返回的调用数。</summary>
    private int _callsReturned;
    /// <summary>当前键块是否发生后端异常。</summary>
    private bool _callFailed;

    /// <summary>是否已释放。</summary>
    private bool _disposed;

    /// <summary>创建实时动作执行器。</summary>
    public LiveActionExecutor(
        IGameRuntimeAccessor runtimeAccessor,
        AgentControlState controlState,
        ILogger<LiveActionExecutor> logger)
    {
        _runtimeAccessor = runtimeAccessor;
        _controlState = controlState;
        _logger = logger;
    }

    /// <summary>
    /// 是否有可证明的系统接受释放请求，且所有受跟踪按键已释放、队列为空、没有在途输入。
    /// 只反映当前瞬间的 SendInput 接受结果，不表示游戏处理或响应；未知时返回 false。
    /// </summary>
    public bool SystemReleaseRequestsAccepted
    {
        get
        {
            try
            {
                if (_disposed || !_executionGate.Wait(0)) return false;
            }
            catch (ObjectDisposedException)
            {
                return false;
            }

            try
            {
                return !_disposed && InputHub.Foreground is SendInputChannel
                    && _systemReleaseAccepted && !_systemReleaseEvidenceUnavailable
                    && !_releaseFailure && _queue.IsEmpty && !HasPressedKeys();
            }
            finally
            {
                _executionGate.Release();
            }
        }
    }

    /// <summary>重置会话范围的租约模式；仅在旧会话已停止时调用。</summary>
    internal void BeginLeaseSession(bool required)
    {
        _executionGate.Wait();
        try
        {
            RevokeLeaseCore();
            StopActionsCore();
            _leaseRequired = required;
            _leaseSession = null;
        }
        finally { _executionGate.Release(); }
    }

    /// <summary>将当前会话升级为必须持有租约；不会取消现有持有者或允许降级。</summary>
    internal void RequireLease()
    {
        _executionGate.Wait();
        try
        {
            if (!_leaseRequired)
            {
                StopActionsCore();
                _leaseRequired = true;
            }
        }
        finally { _executionGate.Release(); }
    }

    /// <summary>启用会话级独占 Live 租约并清理先前动作；不保证其他输入源隔离。</summary>
    internal LiveLeaseGrantDto? AcquireLease(object session, Func<bool> sessionCheck)
    {
        _executionGate.Wait();
        try
        {
            ExpireLeaseCore();
            if (_disposed || _leaseHash is not null || !sessionCheck() || !IsSafe(_runtimeAccessor.Current)) return null;
            StopActionsCore();
            if (_releaseFailure || HasPressedKeys() || _systemReleaseEvidenceUnavailable) return null;
            var secret = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
            var ownerSecret = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
            _leaseHash = SHA256.HashData(Convert.FromHexString(secret));
            _leaseOwnerHash = SHA256.HashData(Convert.FromHexString(ownerSecret));
            _leaseSession = session;
            _leaseRequired = true;
            _leaseActive = false;
            _leaseOwnerCommitted = false;
            _leaseBindingRequestId = null;
            _leaseBindingConnectionId = null;
            _leaseEpoch = checked(_leaseEpoch + 1);
            _leaseDeadline = Stopwatch.GetTimestamp() + Stopwatch.Frequency * LeaseTtlMs / 1000;
            EnsureLoop();
            return new LiveLeaseGrantDto
            {
                Credentials = new LiveLeaseCredentialsDto { Secret = secret, InstanceId = _leaseInstance, Epoch = _leaseEpoch },
                RenewalCredentials = new LiveLeaseCredentialsDto { Secret = ownerSecret, InstanceId = _leaseInstance, Epoch = _leaseEpoch },
                ExpiresInMs = LeaseTtlMs
            };
        }
        finally { _executionGate.Release(); }
    }

    /// <summary>仅当前租约持有者可在过期前续期，过期的租约不能复活。</summary>
    internal bool RenewLease(object session, LiveLeaseCredentialsDto? credentials, Func<bool> sessionCheck)
    {
        _executionGate.Wait();
        try
        {
            ExpireLeaseCore();
            if (_disposed || !sessionCheck() || !LeaseMatches(session, credentials, owner: true)) return false;
            _leaseDeadline = Stopwatch.GetTimestamp() + Stopwatch.Frequency * LeaseTtlMs / 1000;
            return true;
        }
        finally { _executionGate.Release(); }
    }

    /// <summary>验证动作凭据但不延长租约；只有玩家的续期凭据可以延长。</summary>
    internal bool ValidateActionLease(object session, LiveLeaseCredentialsDto? credentials, Func<bool> sessionCheck)
    {
        _executionGate.Wait();
        try
        {
            ExpireLeaseCore();
            return !_disposed && _leaseActive && sessionCheck() && LeaseMatches(session, credentials);
        }
        finally { _executionGate.Release(); }
    }

    /// <summary>在执行闸门内绑定预备租约和真实 Live WebSocket；此操作不允许输入。</summary>
    internal LiveLeaseBindingDto? BindPreparedLease(object session, string sessionId, LiveLeaseCredentialsDto? credentials,
        string connectionId, string requestId, Func<bool> sessionCheck)
    {
        _executionGate.Wait();
        try
        {
            ExpireLeaseCore();
            if (_disposed || _leaseActive || !sessionCheck() || !LeaseMatches(session, credentials)
                || _leaseBindingRequestId is not null) return null;
            _leaseBindingRequestId = requestId;
            _leaseBindingConnectionId = connectionId;
            return LeaseBinding(sessionId, requestId, connectionId);
        }
        finally { _executionGate.Release(); }
    }

    /// <summary>只读取与当前真实连接绑定的租约状态，不延长租约。</summary>
    internal LiveLeaseBindingDto? ProbeBoundLease(object session, string sessionId, LiveLeaseCredentialsDto? credentials,
        string connectionId, string requestId, Func<bool> sessionCheck)
    {
        _executionGate.Wait();
        try
        {
            ExpireLeaseCore();
            if (_disposed || !sessionCheck() || !LeaseMatches(session, credentials)
                || _leaseBindingRequestId != requestId || _leaseBindingConnectionId != connectionId) return null;
            return LeaseBinding(sessionId, requestId, connectionId);
        }
        finally { _executionGate.Release(); }
    }

    /// <summary>只有 owner 能提交当前仍有效且已绑定的预备租约。</summary>
    internal LiveLeaseBindingDto? CommitPreparedLease(object session, string sessionId, LiveLeaseCommitRequest request,
        Func<bool> sessionCheck)
    {
        _executionGate.Wait();
        try
        {
            ExpireLeaseCore();
            if (_disposed || request.UnsupportedFields is { Count: > 0 } || !sessionCheck()
                || request.SessionId != sessionId || request.LeaseRequestId != _leaseBindingRequestId
                || request.ConnectionId != _leaseBindingConnectionId || _leaseBindingRequestId is null
                || _leaseBindingConnectionId is null || !LeaseMatches(session, request.Credentials, owner: true)
                || _controlState.IsStopRequested || Volatile.Read(ref _pendingStopRequests) != 0
                || _releaseFailure || HasPressedKeys() || !_queue.IsEmpty || !IsSafe(_runtimeAccessor.Current)) return null;
            _leaseOwnerCommitted = true;
            return LeaseBinding(sessionId, _leaseBindingRequestId, _leaseBindingConnectionId);
        }
        finally { _executionGate.Release(); }
    }

    /// <summary>仅在 owner 同意后由已绑定连接完成激活；激活前不得注入。</summary>
    internal LiveLeaseBindingDto? ActivateBoundLease(object session, string sessionId, LiveLeaseCredentialsDto? credentials,
        string connectionId, string requestId, Func<bool> sessionCheck)
    {
        _executionGate.Wait();
        try
        {
            ExpireLeaseCore();
            if (_disposed || !_leaseOwnerCommitted || !sessionCheck() || !LeaseMatches(session, credentials)
                || _leaseBindingRequestId != requestId || _leaseBindingConnectionId != connectionId
                || _controlState.IsStopRequested || Volatile.Read(ref _pendingStopRequests) != 0
                || _releaseFailure || HasPressedKeys() || !_queue.IsEmpty || !IsSafe(_runtimeAccessor.Current)) return null;
            _leaseActive = true;
            return LeaseBinding(sessionId, requestId, connectionId);
        }
        finally { _executionGate.Release(); }
    }

    /// <summary>仅供持执行闸门的租约方法创建不含秘密的回执。</summary>
    private LiveLeaseBindingDto LeaseBinding(string sessionId, string requestId, string connectionId) => new()
    {
        SessionId = sessionId, InstanceId = _leaseInstance, Epoch = _leaseEpoch,
        RequestId = requestId, ConnectionId = connectionId,
        Status = _leaseActive ? "active" : _leaseOwnerCommitted ? "owner_committed" : "prepared",
        ExpiresInMs = (int)Math.Max(0, (_leaseDeadline - Stopwatch.GetTimestamp()) * 1000 / Stopwatch.Frequency)
    };

    /// <summary>在注入闸门内核验租约及当前帧；诊断候选不授权对话输入。</summary>
    internal LiveDialogueDecisionResult EvaluateDialogueDecision(object session, LiveLeaseCredentialsDto? credentials,
        Func<bool> sessionCheck, LiveDialogueDecisionRequest request,
        Func<Func<double, double, Func<bool>, LiveDialogueDecisionResult>,
            Func<Func<bool>, LiveDialogueDecisionResult>, LiveDialogueDecisionResult> evaluate)
    {
        _executionGate.Wait();
        try
        {
            ExpireLeaseCore();
            if (_disposed || !_leaseActive || !sessionCheck() || !LeaseMatches(session, credentials))
                return new LiveDialogueDecisionResult { Executed = false, Reason = "lease_or_session_unavailable" };
            var runtime = _runtimeAccessor.Current;
            if (_controlState.IsStopRequested || Volatile.Read(ref _pendingStopRequests) != 0
                || _releaseFailure || runtime is null || runtime.IsStopping
                || !runtime.Window.IsAlive || !runtime.Window.IsForeground || runtime.Window.IsMinimized
                || !_queue.IsEmpty || HasPressedKeys())
                return new LiveDialogueDecisionResult { Executed = false, Reason = "input_state_unavailable" };
            var admissionEpoch = Interlocked.Read(ref _epoch);
            return evaluate((x, y, stillCurrent) => ExecuteDialogueInputCore(session, credentials, sessionCheck,
                    request, x, y, false, admissionEpoch, stillCurrent),
                stillCurrent => ExecuteDialogueInputCore(session, credentials, sessionCheck,
                    request, 0, 0, true, admissionEpoch, stillCurrent));
        }
        finally { _executionGate.Release(); }
    }

    /// <summary>仅由宿主授权回调调用；输入接受不代表游戏响应。</summary>
    private LiveDialogueDecisionResult ExecuteDialogueInputCore(object session, LiveLeaseCredentialsDto? credentials,
        Func<bool> sessionCheck, LiveDialogueDecisionRequest request, double x, double y, bool advance, long admissionEpoch,
        Func<bool> stillCurrent)
    {
        LiveDialogueDecisionResult Result(bool? executed, bool? accepted, string reason) => new()
        {
            IntentId = request.IntentId, Executed = executed, SystemInputAccepted = accepted,
            GameResponseConfirmed = null, Reason = reason
        };
        bool Allowed()
        {
            ExpireLeaseCore();
            var current = _runtimeAccessor.Current;
            return !_disposed && !_stopRequested && Interlocked.Read(ref _epoch) == admissionEpoch
                && !_controlState.IsStopRequested && Volatile.Read(ref _pendingStopRequests) == 0
                && !_releaseFailure && sessionCheck()
                && _leaseActive && LeaseMatches(session, credentials) && stillCurrent() && current is not null && !current.IsStopping
                && current.Window.IsAlive && current.Window.IsForeground && !current.Window.IsMinimized;
        }
        if (!double.IsFinite(x) || !double.IsFinite(y) || x is < 0 or > 1 || y is < 0 or > 1
            || Interlocked.Read(ref _epoch) != admissionEpoch || !sessionCheck() || !stillCurrent())
            return Result(false, null, "dialogue_state_changed");
        var inputKey = advance ? User32.VK.VK_SPACE : User32.VK.VK_LBUTTON;
        var token = advance ? "Space" : "LB";
        _stopRequested = false;
        if (!Allowed() || Simulation.IsKeyDown(inputKey)
            || Simulation.IsKeyDown(User32.VK.VK_MENU) || Simulation.IsKeyDown(User32.VK.VK_CONTROL)
            || Simulation.IsKeyDown(User32.VK.VK_SHIFT)) return Result(false, null, "dialogue_input_unavailable");
        var runtime = _runtimeAccessor.Current!;
        var channel = InputHub.Foreground;
        if (channel is not SendInputChannel) return Result(false, null, "dialogue_input_backend_unverified");
        _executingLeaseEpoch = _leaseEpoch;
        _currentAttribution = new AttemptAttribution(Guid.NewGuid().ToString("N"), request.FrameSeq,
            request.IntentId, null, null);
        _currentChunkIndex = 0;
        _callFailed = false;
        try
        {
            if (!advance)
            {
                var viewport = runtime.Window.Viewport;
                var area = new AgentCaptureArea(viewport.ScreenRect.X, viewport.ScreenRect.Y,
                    viewport.ScreenRect.Width, viewport.ScreenRect.Height, viewport.DpiScale);
                var point = AgentCoordinateMapper.Resolve(x, y, "normalized", area);
                var moved = CallInput(channel, new AtomicInput(AtomicInputKind.MoveTo,
                    X: point.MouseNormalizedX, Y: point.MouseNormalizedY));
                if (moved.Status != InputReceiptStatus.Accepted || moved.Requested != 1 || moved.Accepted != 1)
                    return Result(false, null, "dialogue_cursor_unconfirmed");
                // 游戏需要时间处理模拟鼠标移动，随后重新检查租约和帧。
                Thread.Sleep(40);
            }
            if (!Allowed() || !ReferenceEquals(channel, InputHub.Foreground) || Simulation.IsKeyDown(inputKey))
                return Result(false, null, "dialogue_state_changed");
            var down = PressToken(token);
            if (down is { Status: InputReceiptStatus.Rejected or InputReceiptStatus.Unsupported })
                return Result(false, false, "dialogue_press_rejected");
            if (down is null) return Result(null, null, "dialogue_press_unknown");
            Thread.Sleep(40);
            var up = ReleaseToken(token);
            var accepted = down.Value.Status == InputReceiptStatus.Accepted && down.Value.Requested == 1
                && down.Value.Accepted == 1 && up is { Status: InputReceiptStatus.Accepted, Requested: 1, Accepted: 1 };
            return accepted ? Result(true, true, "system_input_accepted") : Result(null, null, "dialogue_release_unknown");
        }
        catch (Exception exception)
        {
            _logger.LogWarning(exception, "对话输入状态未知");
            return Result(null, null, "dialogue_input_unknown");
        }
        finally
        {
            // 未确认的按键仍绑定原通道，停止逻辑会继续尝试清理。
            if (HasPressedKeys()) ReleasePressed();
            _currentAttribution = null;
            _executingLeaseEpoch = 0;
        }
    }

    /// <summary>仅匹配凭据可主动撤销租约；安全停止独立可用。</summary>
    internal bool ReleaseLease(object session, LiveLeaseCredentialsDto? credentials, bool owner = true)
    {
        _executionGate.Wait();
        try
        {
            ExpireLeaseCore();
            if (!LeaseMatches(session, credentials, owner)) return false;
            RevokeLeaseCore();
            StopActionsCore();
            return true;
        }
        finally { _executionGate.Release(); }
    }

    /// <summary>凭据比较不泄漏秘密；调用方持有执行闸门。</summary>
    private bool LeaseMatches(object session, LiveLeaseCredentialsDto? credentials, bool owner = false)
    {
        var expectedHash = owner ? _leaseOwnerHash : _leaseHash;
        if (!_leaseRequired || expectedHash is null || _leaseHash is null || !ReferenceEquals(_leaseSession, session)
            || credentials?.InstanceId != _leaseInstance || credentials.Epoch != _leaseEpoch
            || credentials.UnsupportedFields is { Count: > 0 } || credentials.Secret is not { Length: 64 }) return false;
        try
        {
            var secret = Convert.FromHexString(credentials.Secret);
            return CryptographicOperations.FixedTimeEquals(SHA256.HashData(secret), expectedHash);
        }
        catch (FormatException) { return false; }
    }

    /// <summary>租约过期或游戏窗口失效时清队并释放；调用方持有执行闸门。</summary>
    private void ExpireLeaseCore()
    {
        var runtime = _runtimeAccessor.Current;
        if (_leaseHash is not null && (Stopwatch.GetTimestamp() >= _leaseDeadline
            || _controlState.IsStopRequested || !WindowUsable(runtime)))
        {
            RevokeLeaseCore();
            StopActionsCore();
        }
    }

    /// <summary>废止秘密，保留会话的强制租约模式。</summary>
    private void RevokeLeaseCore()
    {
        if (_leaseHash is not null) CryptographicOperations.ZeroMemory(_leaseHash);
        if (_leaseOwnerHash is not null) CryptographicOperations.ZeroMemory(_leaseOwnerHash);
        _leaseHash = null;
        _leaseOwnerHash = null;
        _leaseActive = false;
        _leaseOwnerCommitted = false;
        _leaseBindingRequestId = null;
        _leaseBindingConnectionId = null;
        _leaseDeadline = 0;
    }

    /// <summary>当前状态。</summary>
    public LiveExecutorStatus GetStatus()
    {
        lock (_pressedLock)
        {
            return new LiveExecutorStatus
            {
                PressedKeys = _pressed.Keys.OrderBy(key => key, StringComparer.Ordinal).ToArray(),
                QueueDepth = _queue.Count,
                Starved = _starved,
                AppliedActions = Interlocked.Read(ref _applied),
                DroppedActions = Interlocked.Read(ref _dropped)
            };
        }
    }

    /// <summary>
    /// 设置当前会话的截图安全检查。检查必须是无锁的轻量判断，返回 false 表示画面不可信、不得注入输入。
    /// </summary>
    internal void SetSafetyCheck(Func<bool>? safetyCheck)
    {
        lock (_pressedLock)
        {
            _safetyCheck = safetyCheck;
        }
    }

    /// <summary>在注入闸门内更新画面安全状态，不安全时立即清队并释放按键。</summary>
    internal void UpdateSafety(Action update, bool stopAlways = false)
    {
        _executionGate.Wait();
        try
        {
            update();
            if (stopAlways || !IsSafe(_runtimeAccessor.Current))
            {
                StopActionsCore();
            }
        }
        finally
        {
            _executionGate.Release();
        }
    }

    /// <summary>拒绝不支持的动作；无权调用方不能改变其他持有者的安全状态。</summary>
    internal void RejectUnsupportedAction(object session, LiveLeaseCredentialsDto? credentials,
                                          Func<bool> sessionCheck, Action activateSafety)
    {
        _executionGate.Wait();
        try
        {
            ExpireLeaseCore();
            if (!sessionCheck() || (_leaseRequired && !LeaseMatches(session, credentials))
                || (!_leaseRequired && credentials is not null)) return;
            activateSafety();
            StopActionsCore();
        }
        finally { _executionGate.Release(); }
    }

    /// <summary>不等待在途输入地读取安全状态；无法获取闸门时必须报告未知。</summary>
    internal bool TryReadSafety(object session, string sessionId, Action<bool, bool, bool, LiveSystemReleaseReceiptDto?> read)
    {
        if (!_executionGate.Wait(0)) return false;
        try
        {
            var safe = IsSafe(_runtimeAccessor.Current);
            if (!safe) InvalidateSystemReleaseReceipt();
            var releaseCallsReturned = _releaseConfirmed && !_releaseFailure && _queue.IsEmpty && !HasPressedKeys();
            var receipt = _systemReleaseReceipt;
            if (receipt is null || !ReferenceEquals(_systemReleaseSession, session) || receipt.SessionId != sessionId
                || receipt.StopEpoch != Interlocked.Read(ref _epoch) || receipt.InputGeneration != _inputGeneration
                || !releaseCallsReturned || Volatile.Read(ref _pendingStopRequests) != 0)
                receipt = null;
            read(releaseCallsReturned, releaseCallsReturned && !_releaseProofUnavailable, safe, receipt);
            return true;
        }
        finally
        {
            _executionGate.Release();
        }
    }

    /// <summary>
    /// 入队一个动作块。新动作块替换尚未执行的旧键块，避免突发到达时补播过期动作。
    /// </summary>
    /// <param name="block">动作块。</param>
    /// <returns>是否接受。</returns>
    internal bool Enqueue(LiveActionBlockDto block, out string? attemptId, Action? activateSafety = null, Func<bool>? sessionCheck = null,
        object? leaseSession = null, LiveLeaseCredentialsDto? credentials = null)
    {
        ArgumentNullException.ThrowIfNull(block);
        attemptId = null;
        _executionGate.Wait();
        try
        {
            ExpireLeaseCore();
            if ((_leaseRequired && (!_leaseActive || leaseSession is null || !LeaseMatches(leaseSession, credentials)))
                || (!_leaseRequired && credentials is not null)) return false;
            activateSafety?.Invoke();
            if (_disposed || block.Chunks.Count == 0)
            {
                return false;
            }

            var runtime = _runtimeAccessor.Current;
            if (sessionCheck is not null && !sessionCheck())
            {
                // 旧会话的请求不能进入新会话；模式切换后仍需清理不安全输入。
                if (!IsSafe(runtime)) StopActionsCore();
                return false;
            }
            if (!IsSafe(runtime))
            {
                // 游戏不可用或画面过旧：清队并拒绝本次动作。
                StopActionsCore();
                return false;
            }

            // 新代次让队列中尚未执行的旧键块立即作废。
            var epoch = Interlocked.Increment(ref _epoch);
            while (_queue.TryDequeue(out var replaced))
            {
                Interlocked.Increment(ref _dropped);
                ReportAttempt(replaced, "dropped", 0, 0);
            }

            attemptId = Guid.NewGuid().ToString("N");
            var attribution = new AttemptAttribution(attemptId, block.Seq, block.IntentId,
                block.MotorGeneration, block.ActionGroupId);
            EnsureLoop();
            _releaseConfirmed = false;
            var now = Stopwatch.GetTimestamp();
            Interlocked.Exchange(ref _lastEnqueueTicks, now);
            _starved = false;
            _stopRequested = false;

            // 入队快照，避免调用方随后修改已通过校验的动作。
            var chunks = block.Chunks.Take(6).Select(chunk => new LiveActionChunkDto
            {
                Dx = chunk.Dx,
                Dy = chunk.Dy,
                Scroll = chunk.Scroll,
                Keys = chunk.Keys.ToList()
            }).ToList();
            if (block.Absolute is not null)
            {
                _queue.Enqueue(new ScheduledChunk(epoch, now, null,
                    new LiveAbsolutePointDto { X = block.Absolute.X, Y = block.Absolute.Y }, attribution, -1,
                    _leaseRequired ? _leaseEpoch : 0));
            }

            for (var index = 0; index < chunks.Count; index++)
            {
                var due = now + (long)(index * ChunkIntervalMs * (double)Stopwatch.Frequency / 1000);
                _queue.Enqueue(new ScheduledChunk(epoch, due, chunks[index], null, attribution, index,
                    _leaseRequired ? _leaseEpoch : 0));
            }

            return true;
        }
        finally
        {
            _executionGate.Release();
        }
    }

    /// <summary>清空队列、释放按键并重置统计。</summary>
    public void Reset()
    {
        StopActions();
        Interlocked.Exchange(ref _applied, 0);
        Interlocked.Exchange(ref _dropped, 0);
        Interlocked.Exchange(ref _lastEnqueueTicks, 0);
    }

    /// <summary>
    /// 清空队列并释放按键。会等待在途注入结束，返回后不会再有键被按下。
    /// </summary>
    public void StopActions() => StopActionsWithEvidence();

    /// <summary>旧连接断开时只清理仍处于无租约模式的动作，不得撤销后来的租约。</summary>
    internal void StopLegacyActions()
    {
        _executionGate.Wait();
        try
        {
            if (!_leaseRequired) StopActionsCore();
        }
        finally { _executionGate.Release(); }
    }

    /// <summary>在停止闸门内归集该次受跟踪按键的系统释放请求结果。</summary>
    internal LiveStopSafetyObservationDto StopActionsWithEvidence(object? session = null, string? sessionId = null)
    {
        // 先登记停止请求，避免对话授权在等待闸门期间重新打开输入。
        Interlocked.Increment(ref _pendingStopRequests);
        _stopRequested = true;
        Interlocked.Increment(ref _epoch);
        try
        {
            _executionGate.Wait();
        }
        catch
        {
            Interlocked.Decrement(ref _pendingStopRequests);
            throw;
        }
        try
        {
            var boundSession = session is not null && ReferenceEquals(_leaseSession, session) && _leaseHash is not null;
            var leaseEpoch = _leaseEpoch;
            InvalidateSystemReleaseReceipt();
            // 停止请求同时撤销当前租约，任何后续动作需重新授予。
            RevokeLeaseCore();
            // 入队可能在等待期间恢复执行，持有闸门后再次作废它。
            _stopRequested = true;
            var epoch = Interlocked.Increment(ref _epoch);
            while (_queue.TryDequeue(out var dropped))
            {
                ReportAttempt(dropped, "aborted", 0, 0);
            }

            List<KeyValuePair<string, LiveKeyAction>> tracked;
            lock (_pressedLock) tracked = _pressed.ToList();
            var callsReturned = true;
            var allAccepted = true;
            foreach (var (token, action) in tracked)
            {
                var receipt = ReleaseToken(token);
                callsReturned &= receipt.HasValue;
                allAccepted &= action.Channel is SendInputChannel
                    && receipt is { Status: InputReceiptStatus.Accepted, Requested: 1, Accepted: 1 };
            }
            _releaseConfirmed = !HasPressedKeys();
            _starved = true;
            var systemAccepted = allAccepted && !_systemReleaseEvidenceUnavailable
                && !_releaseFailure && _queue.IsEmpty && !HasPressedKeys();
            if (systemAccepted && tracked.Count > 0 && boundSession && !string.IsNullOrEmpty(sessionId)
                && IsSafe(_runtimeAccessor.Current))
            {
                _systemReleaseSession = session;
                _systemReleaseReceipt = new LiveSystemReleaseReceiptDto
                {
                    SessionId = sessionId, LeaseInstanceId = _leaseInstance, LeaseEpoch = leaseEpoch,
                    StopEpoch = epoch, InputGeneration = _inputGeneration, ReleasedKeyCount = tracked.Count,
                    TimestampUtc = DateTimeOffset.UtcNow
                };
            }
            return new LiveStopSafetyObservationDto
            {
                Scope = "stop_atomic",
                Epoch = epoch,
                TrackedKeyCount = tracked.Count,
                InputReleaseCallsReturned = callsReturned,
                InputReleaseConfirmed = null,
                SystemReleaseRequestsAccepted = systemAccepted && tracked.Count > 0 ? true : null,
                SystemReleaseReceipt = _systemReleaseReceipt
            };
        }
        finally
        {
            _executionGate.Release();
            Interlocked.Decrement(ref _pendingStopRequests);
        }
    }

    /// <summary>释放全部按键并停止执行循环。</summary>
    public void Dispose()
    {
        lock (_loopLock)
        {
            if (_disposed)
            {
                return;
            }

            _disposed = true;
        }

        _cancellation?.Cancel();
        StopActions();
        try
        {
            _loop?.Wait(TimeSpan.FromSeconds(2));
        }
        catch (Exception exception)
        {
            _logger.LogDebug(exception, "停止实时动作执行器时发生异常");
        }

        _cancellation?.Dispose();
        _executionGate.Dispose();
    }

    /// <summary>确保后台执行循环已启动。</summary>
    private void EnsureLoop()
    {
        lock (_loopLock)
        {
            if (_loop is { IsCompleted: false })
            {
                return;
            }

            _cancellation?.Dispose();
            _cancellation = new CancellationTokenSource();
            _loop = Task.Run(() => RunAsync(_cancellation.Token));
        }
    }

    /// <summary>后台执行循环。</summary>
    private async Task RunAsync(CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            try
            {
                if (_executionGate.Wait(0))
                {
                    try
                    {
                        ExpireLeaseCore();
                        if (_systemReleaseReceipt is not null && !IsSafe(_runtimeAccessor.Current))
                            InvalidateSystemReleaseReceipt();
                    }
                    finally { _executionGate.Release(); }
                }
                if (_stopRequested)
                {
                    // 清理失败时禁止新输入，但仍需尝试释放残留按键。
                    var retryRelease = _releaseFailure;
                    if (retryRelease && _executionGate.Wait(0))
                    {
                        try
                        {
                            if (_stopRequested && _releaseFailure)
                            {
                                ReleasePressed();
                                _releaseConfirmed = !HasPressedKeys();
                            }
                        }
                        finally { _executionGate.Release(); }
                    }
                    await Task.Delay(retryRelease ? 100 : 5, cancellationToken).ConfigureAwait(false);
                    continue;
                }

                var runtime = _runtimeAccessor.Current;
                if (!IsSafe(runtime))
                {
                    // 等待执行闸门后重新判断，旧快照不能撤销后来变为安全的新租约。
                    _executionGate.Wait(cancellationToken);
                    try
                    {
                        if (!IsSafe(_runtimeAccessor.Current))
                        {
                            RevokeLeaseCore();
                            StopActionsCore();
                        }
                    }
                    finally { _executionGate.Release(); }
                    await Task.Delay(100, cancellationToken).ConfigureAwait(false);
                    continue;
                }

                if (_executionGate.Wait(0))
                {
                    try
                    {
                        ApplyDueChunks(runtime!);
                    }
                    finally
                    {
                        _executionGate.Release();
                    }
                }

                await Task.Delay(5, cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                _logger.LogWarning(exception, "实时动作执行循环异常，已释放按键");
                StopActions();
                try
                {
                    await Task.Delay(100, cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
                {
                    break;
                }
            }
        }

        StopActions();
    }

    /// <summary>执行当前已到期的键块。调用方必须持有执行闸门。</summary>
    private void ApplyDueChunks(GameRuntime runtime)
    {
        var epoch = Interlocked.Read(ref _epoch);
        var now = Stopwatch.GetTimestamp();
        var appliedAny = false;
        while (_queue.TryPeek(out var scheduled) && scheduled.DueAt <= now)
        {
            if (!_queue.TryDequeue(out scheduled))
            {
                break;
            }

            if (_stopRequested || scheduled.Epoch != Interlocked.Read(ref _epoch))
            {
                // 停止或新动作块取代了本批：丢弃剩余键块，不再补播。
                Interlocked.Increment(ref _dropped);
                ReportAttempt(scheduled, "aborted", 0, 0);
                continue;
            }

            // 延迟超过一个键块的动作视为过期，不再补播，避免一次性注入多个过期键。
            if (Stopwatch.GetElapsedTime(scheduled.DueAt, now) > TimeSpan.FromMilliseconds(ExpiredChunkToleranceMs))
            {
                Interlocked.Increment(ref _dropped);
                ReportAttempt(scheduled, "dropped", 0, 0);
                continue;
            }

            ExpireLeaseCore();
            if (_leaseRequired && (!_leaseActive || _leaseHash is null || scheduled.LeaseEpoch != _leaseEpoch))
            {
                ReportAttempt(scheduled, "aborted", 0, 0);
                StopActionsCore();
                break;
            }
            if (!IsSafe(runtime))
            {
                ReportAttempt(scheduled, "aborted", 0, 0);
                StopActionsCore();
                break;
            }

            if (scheduled.Chunk is { } chunk && chunk.Keys.Any(key => !LiveKeyTokenMap.TryMap(key, out _, out _)))
            {
                ReportAttempt(scheduled, "failed", 0, 0);
                StopActionsCore();
                break;
            }

            _callsStarted = 0;
            _callsReturned = 0;
            _callFailed = false;
            _countInputCalls = true;
            _currentAttribution = scheduled.Attribution;
            _currentChunkIndex = scheduled.ChunkIndex;
            _executingLeaseEpoch = scheduled.LeaseEpoch;
            try
            {
                if (scheduled.Absolute is not null)
                {
                    ApplyAbsolute(scheduled.Absolute.X, scheduled.Absolute.Y);
                }

                if (scheduled.Chunk is not null && !_stopRequested)
                {
                    ApplyChunk(runtime, scheduled.Chunk);
                }
            }
            catch (Exception exception)
            {
                _callFailed = true;
                _logger.LogWarning(exception, "实时动作键块输入调用失败");
                StopActionsCore();
            }
            finally
            {
                _countInputCalls = false;
                _currentAttribution = null;
            }

            if (_callFailed && !_stopRequested) StopActionsCore();
            var outcome = _callFailed ? "failed" : _stopRequested || scheduled.Epoch != Interlocked.Read(ref _epoch)
                ? "aborted" : "unknown";
            ReportAttempt(scheduled, outcome, _callsStarted, _callsReturned);
            if (outcome != "unknown") break;
            Interlocked.Increment(ref _applied);
            appliedAny = true;
        }

        if (appliedAny)
        {
            _starved = false;
        }
        else if (_queue.IsEmpty && HasPressedKeys()
                 && Stopwatch.GetElapsedTime(Interlocked.Read(ref _lastEnqueueTicks), now)
                     > TimeSpan.FromMilliseconds(IdleReleaseMs))
        {
            ReleasePressed();
            _releaseConfirmed = !HasPressedKeys();
            _starved = true;
        }
    }

    /// <summary>要求游戏窗口可用且有新鲜截图。</summary>
    private bool IsSafe(GameRuntime? runtime)
    {
        var frameSafe = _safetyCheck is not null && _safetyCheck();
        return frameSafe && !_releaseFailure && !_controlState.IsStopRequested && WindowUsable(runtime);
    }

    /// <summary>
    /// 开关后台输入模式。PostMessage 投递的按键不经过前台焦点，
    /// 因此系统"接受"只代表消息已排队，不代表游戏处理或响应。
    /// </summary>
    public void ConfigureBackgroundInput(bool enabled) => _backgroundInput = enabled;

    /// <summary>窗口是否仍可用于投递输入：存活、未最小化；后台模式不要求前台。</summary>
    private bool WindowUsable(GameRuntime? runtime)
    {
        if (runtime is null || runtime.IsStopping) return false;
        if (!runtime.Window.IsAlive || runtime.Window.IsMinimized) return false;
        return runtime.Window.IsForeground || _backgroundInput;
    }

    /// <summary>
    /// 选择本批按键的输入通道：窗口在前台时用 SendInput；
    /// 窗口不在前台且启用后台模式时退回 PostMessage。
    /// </summary>
    private IInputChannel ChooseChannel(GameRuntime runtime) =>
        runtime.Window.IsForeground ? InputHub.Foreground : InputHub.Background;

    /// <summary>
    /// 选择本批按键的输入通道：窗口在前台时按配置选 SendInput 或驱动级通道；
    /// 窗口不在前台且启用后台模式时退回 PostMessage。
    /// </summary>

    /// <summary>应用一个键块。</summary>
    private void ApplyChunk(GameRuntime runtime, LiveActionChunkDto chunk)
    {
        var channel = ChooseChannel(runtime);
        // PostMessage 通道不支持相对移动与滚轮：后台模式下只投递按键，
        // 把视角/滚轮类输入留给前台会话，避免发出未生效却看起来成功的动作。
        var allowsPointer = channel is SendInputChannel;

        if (allowsPointer && (chunk.Dx != 0 || chunk.Dy != 0))
        {
            CallInput(channel, new AtomicInput(AtomicInputKind.MoveBy, X: Math.Clamp(chunk.Dx, -1000, 1000),
                Y: Math.Clamp(chunk.Dy, -1000, 1000)));
        }

        if (_stopRequested || _callFailed || !IsSafe(runtime))
        {
            StopActionsCore();
            return;
        }

        if (allowsPointer && chunk.Scroll != 0)
        {
            CallInput(channel, new AtomicInput(AtomicInputKind.Scroll, Y: Math.Clamp(chunk.Scroll, -10, 10)));
        }

        if (_stopRequested || _callFailed || !IsSafe(runtime))
        {
            StopActionsCore();
            return;
        }

        ApplyKeyDiff(chunk.Keys);
    }

    /// <summary>应用一个绝对光标位置（相对 1280×720 输出帧）。</summary>
    /// <param name="x">X 像素。</param>
    /// <param name="y">Y 像素。</param>
    private void ApplyAbsolute(int x, int y)
    {
        var runtime = _runtimeAccessor.Current;
        if (!IsSafe(runtime))
        {
            StopActionsCore();
            return;
        }

        var viewport = runtime!.Window.Viewport;
        var area = new AgentCaptureArea(
            viewport.ScreenRect.X,
            viewport.ScreenRect.Y,
            viewport.ScreenRect.Width,
            viewport.ScreenRect.Height,
            viewport.DpiScale);
        var point = AgentCoordinateMapper.Resolve(
            x / (double)ModelFrameWidth,
            y / (double)ModelFrameHeight,
            "normalized",
            area);
        CallInput(InputHub.Foreground, new AtomicInput(AtomicInputKind.MoveTo, X: point.MouseNormalizedX, Y: point.MouseNormalizedY));
    }

    /// <summary>比较目标键集合并发送按下/抬起。</summary>
    private void ApplyKeyDiff(List<string> keys)
    {
        var desired = new HashSet<string>(StringComparer.Ordinal);
        foreach (var token in keys)
        {
            if (LiveKeyTokenMap.TryMap(token, out var virtualKey, out _))
            {
                var value = token.Trim();
                // 数字 one 与功能键 One 代表不同的虚拟键，不能做不区分大小写的去重。
                var code = virtualKey.HasValue ? (int)virtualKey.Value : -1;
                var preserveCase = code >= (int)User32.VK.VK_0 && code <= (int)User32.VK.VK_9
                                   || code >= (int)User32.VK.VK_F1 && code <= (int)User32.VK.VK_F12;
                desired.Add(preserveCase ? value : value.ToUpperInvariant());
            }
        }

        if (IsDangerousCombination(desired))
        {
            // 危险组合与键的先后顺序无关：整批一起判断，命中就整体拒绝并释放按键。
            _logger.LogWarning("实时动作阻止危险组合键");
            StopActionsCore();
            return;
        }

        List<string> release;
        List<string> press;
        lock (_pressedLock)
        {
            release = _pressed.Keys
                .Where(key => !desired.Contains(key))
                .ToList();
            press = desired
                .Where(key => !_pressed.ContainsKey(key))
                .ToList();
        }

        foreach (var token in release)
        {
            ReleaseToken(token);
        }

        foreach (var token in press)
        {
            if (_stopRequested || _callFailed || !IsSafe(_runtimeAccessor.Current))
            {
                StopActionsCore();
                return;
            }

            PressToken(token);
        }
    }

    /// <summary>
    /// 判断目标键集合是否构成危险系统快捷键。判断只看集合，不看顺序，
    /// 因此 "Alt+Tab" 与 "Tab+Alt" 的结论一致。
    /// </summary>
    private bool IsDangerousCombination(HashSet<string> desired)
    {
        List<string> pressed;
        lock (_pressedLock)
        {
            pressed = _pressed.Keys.ToList();
        }

        var all = new HashSet<string>(desired, StringComparer.Ordinal);
        all.UnionWith(pressed);

        // 修饰键同时考虑本批要按下的键和真实键盘状态，避免用户按住修饰键时误触系统快捷键。
        var alt = all.Contains("ALT") || Simulation.IsKeyDown(User32.VK.VK_MENU)
                  || Simulation.IsKeyDown(User32.VK.VK_LMENU) || Simulation.IsKeyDown(User32.VK.VK_RMENU);
        var ctrl = all.Contains("CTRL") || Simulation.IsKeyDown(User32.VK.VK_CONTROL)
                   || Simulation.IsKeyDown(User32.VK.VK_LCONTROL) || Simulation.IsKeyDown(User32.VK.VK_RCONTROL);
        return all.Contains("TAB") && alt
               || all.Contains("ESC") && (alt || ctrl)
               || all.Contains("SPACE") && alt
               || all.Contains("ENTER") && alt;
    }

    /// <summary>按下一个 token。</summary>
    private InputReceipt? PressToken(string token)
    {
        if (!LiveKeyTokenMap.TryMap(token, out var virtualKey, out var mouseButton))
        {
            return null;
        }

        EnsureLeaseForInput();
        // 现有状态字段仍不把操作系统接受当作游戏确认。
        _releaseProofUnavailable = true;
        var channel = ChooseChannel(_runtimeAccessor.Current!);
        var key = virtualKey ?? MouseVirtualKey(mouseButton!);
        // 在注入前登记，并固定通道；后端切换也必须向原通道释放。
        lock (_pressedLock)
        {
            _pressed[token] = new LiveKeyAction(key, channel);
        }

        try
        {
            var receipt = CallInput(channel, new AtomicInput(AtomicInputKind.KeyDown, key));
            if (receipt.Status is InputReceiptStatus.Rejected or InputReceiptStatus.Unsupported)
            {
                // 明确未注入，无需对这个 token 发送释放。
                lock (_pressedLock) _pressed.Remove(token);
            }
            else if (channel is not SendInputChannel || receipt.Status != InputReceiptStatus.Accepted
                     || receipt.Requested != 1 || receipt.Accepted != 1)
            {
                _systemReleaseEvidenceUnavailable = true;
            }
            return receipt;
        }
        catch (LiveLeaseExpiredException)
        {
            // 租约检查在开始 KeyDown 调用之前失败；不能释放用户可能按住的键。
            lock (_pressedLock) _pressed.Remove(token);
            throw;
        }
        catch (Exception exception)
        {
            _systemReleaseEvidenceUnavailable = true;
            _logger.LogDebug(exception, "按下实时动作键 {Token} 失败", token);
            ReleaseToken(token);
            return null;
        }
    }

    /// <summary>抬起一个 token。</summary>
    private InputReceipt? ReleaseToken(string token)
    {
        LiveKeyAction action;
        lock (_pressedLock)
        {
            if (!_pressed.TryGetValue(token, out action))
            {
                return null;
            }
        }

        try
        {
            var receipt = CallInput(action.Channel, new AtomicInput(AtomicInputKind.KeyUp, action.VirtualKey));
            // 只有绑定原通道的单个 INPUT 被 Windows 接受，才能作为本次释放证据。
            if (action.Channel is SendInputChannel && receipt.Status == InputReceiptStatus.Accepted
                && receipt.Requested == 1 && receipt.Accepted == 1)
                _systemReleaseAccepted = true;
            if (receipt.Status is not InputReceiptStatus.Accepted
                || (action.Channel is SendInputChannel && (receipt.Requested != 1 || receipt.Accepted != 1)))
            {
                // 未确认释放时保留原通道与按键，避免把未知结果当作已释放。
                _releaseFailure = true;
                return receipt;
            }

            lock (_pressedLock)
            {
                _pressed.Remove(token);
                if (_pressed.Count == 0) _releaseFailure = false;
            }
            return receipt;
        }
        catch (Exception exception)
        {
            // 释放失败必须保留跟踪，后续停止可重试，不能报告已确认释放。
            _releaseFailure = true;
            _logger.LogDebug(exception, "抬起实时动作键 {Token} 失败", token);
            return null;
        }
    }

    /// <summary>释放全部按键。</summary>
    private void ReleasePressed()
    {
        List<string> tokens;
        lock (_pressedLock)
        {
            tokens = _pressed.Keys.ToList();
        }

        foreach (var token in tokens)
        {
            ReleaseToken(token);
        }
    }

    /// <summary>是否存在按下的键。</summary>
    private bool HasPressedKeys()
    {
        lock (_pressedLock)
        {
            return _pressed.Count > 0;
        }
    }

    /// <summary>
    /// 清空队列并释放按键。调用方必须持有执行闸门，因此不会与在途注入并发。
    /// </summary>
    private void StopActionsCore()
    {
        InvalidateSystemReleaseReceipt();
        _stopRequested = true;
        Interlocked.Increment(ref _epoch);
        while (_queue.TryDequeue(out var dropped))
        {
            ReportAttempt(dropped, "aborted", 0, 0);
        }

        ReleasePressed();
        _releaseConfirmed = !HasPressedKeys();
        _starved = true;
    }

    /// <summary>将实时鼠标 token 映射到通用原子输入键码。</summary>
    private static User32.VK MouseVirtualKey(string button) => button switch
    {
        "RB" => User32.VK.VK_RBUTTON,
        "MB" => User32.VK.VK_MBUTTON,
        "XB1" => User32.VK.VK_XBUTTON1,
        "XB2" => User32.VK.VK_XBUTTON2,
        _ => User32.VK.VK_LBUTTON
    };

    /// <summary>通过非阻塞回调记录处理结果，接收端可能因有界队列丢失消息。</summary>
    private void ReportAttempt(ScheduledChunk chunk, string status, int started, int returned)
    {
        try
        {
            var attribution = chunk.Attribution;
            ExecutionAttempt?.Invoke(new LiveExecutionAttemptDto
            {
                AttemptId = attribution.AttemptId,
                Seq = attribution.Seq,
                IntentId = attribution.IntentId,
                MotorGeneration = attribution.MotorGeneration,
                ActionGroupId = attribution.ActionGroupId,
                ChunkIndex = chunk.ChunkIndex,
                Status = status,
                InputCallsStarted = started,
                InputCallsReturned = returned,
                TimestampUtc = DateTimeOffset.UtcNow
            });
        }
        catch (Exception exception)
        {
            _logger.LogDebug(exception, "发布动作尝试记录失败");
        }
    }

    /// <summary>撤销此前的系统释放快照；调用方持有执行闸门。</summary>
    private void InvalidateSystemReleaseReceipt()
    {
        _systemReleaseReceipt = null;
        _systemReleaseSession = null;
    }

    /// <summary>每次非释放输入前验证租约；过期后不启动新调用。</summary>
    private void EnsureLeaseForInput()
    {
        var runtime = _runtimeAccessor.Current;
        if (_leaseHash is not null && (Stopwatch.GetTimestamp() >= _leaseDeadline
            || _controlState.IsStopRequested || !WindowUsable(runtime))) RevokeLeaseCore();
        if (_leaseRequired && (!_leaseActive || _leaseHash is null || _executingLeaseEpoch != _leaseEpoch))
            throw new LiveLeaseExpiredException();
    }

    /// <summary>统计调用并发布与该次操作绑定的回执，不推断游戏响应。</summary>
    private InputReceipt CallInput(IInputChannel channel, AtomicInput input)
    {
        if (input.Kind != AtomicInputKind.KeyUp)
        {
            InvalidateSystemReleaseReceipt();
            _systemReleaseAccepted = false;
            _inputGeneration = checked(_inputGeneration + 1);
            EnsureLeaseForInput();
        }
        if (_countInputCalls) _callsStarted++;
        try
        {
            var receipt = InputHub.ExecuteAtomic(channel, input);
            if (_countInputCalls) _callsReturned++;
            if (receipt.Status is InputReceiptStatus.Rejected or InputReceiptStatus.Partial or InputReceiptStatus.Unsupported)
                _callFailed = true;
            PublishInputEvidence(input, receipt);
            return receipt;
        }
        catch
        {
            if (_countInputCalls) _callFailed = true;
            PublishInputEvidence(input, new InputReceipt(InputReceiptStatus.Unknown));
            throw;
        }
    }

    /// <summary>发送逐次证据；订阅异常不得影响真实输入。</summary>
    private void PublishInputEvidence(AtomicInput input, InputReceipt receipt)
    {
        try
        {
            var attribution = _currentAttribution;
            InputEvidence?.Invoke(new LiveInputEvidence(attribution?.AttemptId,
                attribution is null ? null : _currentChunkIndex, input, receipt,
                attribution?.Seq, attribution?.IntentId, attribution?.MotorGeneration, attribution?.ActionGroupId));
        }
        catch (Exception exception)
        {
            _logger.LogDebug(exception, "发布原子输入回执失败");
        }
    }

    /// <summary>排定的键块。</summary>
    /// <param name="Epoch">动作代次，与当前代次不一致时丢弃。</param>
    /// <param name="DueAt">到期时间（单调时钟）。</param>
    /// <param name="Chunk">键块；为空表示绝对光标定位。</param>
    /// <param name="Absolute">绝对光标位置。</param>
    private readonly record struct ScheduledChunk(long Epoch, long DueAt, LiveActionChunkDto? Chunk,
        LiveAbsolutePointDto? Absolute, AttemptAttribution Attribution, int ChunkIndex, long LeaseEpoch);

    /// <summary>表示输入尚未启动而租约已失效。</summary>
    private sealed class LiveLeaseExpiredException : Exception { }

    /// <summary>入队时固定的关联信息。</summary>
    private readonly record struct AttemptAttribution(string AttemptId, long? Seq, string? IntentId,
        long? MotorGeneration, string? ActionGroupId);

    /// <summary>按下的键动作。</summary>
    private readonly record struct LiveKeyAction(User32.VK VirtualKey, IInputChannel Channel);
}

/// <summary>单次输入证据；尝试标识为空表示停止或空闲时的释放。</summary>
/// <param name="AttemptId">所属动作尝试标识。</param>
/// <param name="ChunkIndex">所属键块序号。</param>
/// <param name="Input">实际调用的输入。</param>
/// <param name="Receipt">后端回执，不表示游戏响应。</param>
/// <param name="Seq">所属动作的调用方序号；没有归属时为空。</param>
/// <param name="IntentId">所属动作的调用方意图标识；没有归属时为空。</param>
/// <param name="MotorGeneration">所属动作的代次；没有归属时为空。</param>
/// <param name="ActionGroupId">所属动作组；没有归属时为空。</param>
public sealed record LiveInputEvidence(string? AttemptId, int? ChunkIndex, AtomicInput Input, InputReceipt Receipt,
    long? Seq = null, string? IntentId = null, long? MotorGeneration = null, string? ActionGroupId = null);
