using BetterGenshinImpact.Core.Config;
using BetterGenshinImpact.GameTask.Common;
using BetterGenshinImpact.GameTask.Runtime;
using BetterGenshinImpact.Helpers;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Media;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Models;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Runtime;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Sessions;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Vision;
using BetterGenshinImpact.Service.Interface;
using Microsoft.Extensions.Logging;
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Live;

/// <summary>
/// BetterGI 实时会话：按 fps 采集游戏窗口并广播 JPEG，接收 Lumine 动作块并执行，
/// 维护指导、解说时间线与最新帧，供 dsh、Lumine Bridge 和 VTuber 叠加层使用。
/// </summary>
public sealed class LiveSessionService : ILiveSessionService, IDisposable
{
    /// <summary>允许的最大广播帧率。</summary>
    private const int MaxFps = 15;

    /// <summary>允许的最小广播帧率。</summary>
    private const int MinFps = 2;

    /// <summary>解说文本最大长度。</summary>
    private const int MaxCommentaryLength = 500;

    /// <summary>指导文本最大长度。</summary>
    private const int MaxGuidanceLength = 500;

    /// <summary>时间线在内存中保留的最大事件数。</summary>
    private const int MaxTimelineEvents = 5000;

    /// <summary>录制帧保留上限。</summary>
    private const int MaxRecordedFrames = 3000;

    /// <summary>允许的解说类型。</summary>
    private static readonly HashSet<string> CommentaryKinds = new(StringComparer.OrdinalIgnoreCase)
    {
        "reaction", "plan", "emotion", "lore", "chat", "result"
    };

    /// <summary>配置服务。</summary>
    private readonly IConfigService _configService;

    /// <summary>运行环境访问器。</summary>
    private readonly IGameRuntimeAccessor _runtimeAccessor;

    /// <summary>截图源。</summary>
    private readonly IAgentScreenSource _screenSource;

    /// <summary>只从同一截图提取 UI 状态。</summary>
    private readonly IAgentVisionService _visionService;

    /// <summary>只允许可信宿主提供候选诊断策略；缺省为禁用。</summary>
    private readonly ILiveDialogueSafetyPolicy? _dialogueSafetyPolicy;

    /// <summary>宿主只读证明与独立选项输入授权。</summary>
    private readonly ILivePlayerDialoguePolicy? _playerDialoguePolicy;

    /// <summary>宿主本机独立的仅主界面模板能力。</summary>
    private readonly ILiveDialogueExitPolicy? _dialogueExitPolicy;

    /// <summary>会话持有的同步触发器截图隔离能力。</summary>
    private readonly ILiveTriggerCaptureIsolation? _triggerCaptureIsolation;

    /// <summary>跨会话最多一个未完成的视觉任务；不会排队。</summary>
    private int _visionBusy;

    /// <summary>最近一次视觉分析的完成信号；停机时须等待分析释放副本。</summary>
    private TaskCompletionSource<bool>? _visionDrain;

    /// <summary>一旦有 v2 动作即在服务生命周期内保持严格模式，重启会话或 v1 不能降级。</summary>
    private volatile bool _strictUiSafety;

    /// <summary>观察到对话后锁定；没有可靠退出和授权流程时不能自动解除。</summary>
    private volatile bool _talkLatched;

    /// <summary>上一原始帧指纹，仅在执行闸门内访问，跨会话保留。</summary>
    private byte[]? _lastFrameFingerprint;

    /// <summary>有限的最近帧指纹，用于拒绝短周期重放；只在执行闸门内访问。</summary>
    private readonly Queue<string> _recentFrameFingerprints = new();

    /// <summary>最近帧指纹集合。</summary>
    private readonly HashSet<string> _recentFrameFingerprintSet = new(StringComparer.Ordinal);

    /// <summary>会话存储。</summary>
    private readonly IAgentSessionStore _sessionStore;

    /// <summary>智能体停止开关。</summary>
    private readonly AgentControlState _controlState;

    /// <summary>实时动作执行器。</summary>
    private readonly LiveActionExecutor _actionExecutor;

    /// <summary>转发全局停止信号。</summary>
    private readonly TaskStopService _taskStopService;

    /// <summary>串行化启动和停止，避免启动中取消产生僵尸会话。</summary>
    private readonly SemaphoreSlim _lifecycle = new(1, 1);

    /// <summary>日志。</summary>
    private readonly ILogger<LiveSessionService> _logger;

    /// <summary>保护会话状态。</summary>
    private readonly object _gate = new();

    /// <summary>WebSocket 客户端。</summary>
    private readonly ConcurrentDictionary<string, LiveClientConnection> _clients = new();

    /// <summary>保护全局停止回调派生的会话清理任务。</summary>
    private readonly object _pendingStopsGate = new();

    /// <summary>尚未退出的会话清理任务。</summary>
    private readonly HashSet<Task> _pendingStops = [];

    /// <summary>时间线事件。</summary>
    private readonly ConcurrentQueue<LiveTimelineEventDto> _timeline = new();

    /// <summary>时间线序号。</summary>
    private long _timelineSeq;

    /// <summary>当前会话。</summary>
    private LiveSessionState? _state;

    /// <summary>是否已释放。</summary>
    private bool _disposed;

    /// <summary>创建实时会话服务。</summary>
    public LiveSessionService(
        IConfigService configService,
        IGameRuntimeAccessor runtimeAccessor,
        IAgentScreenSource screenSource,
        IAgentSessionStore sessionStore,
        AgentControlState controlState,
        LiveActionExecutor actionExecutor,
        TaskStopService taskStopService,
        ILogger<LiveSessionService> logger,
        IAgentVisionService visionService,
        ILiveDialogueSafetyPolicy? dialogueSafetyPolicy = null,
        ILiveTriggerCaptureIsolation? triggerCaptureIsolation = null,
        ILivePlayerDialoguePolicy? playerDialoguePolicy = null,
        ILiveDialogueExitPolicy? dialogueExitPolicy = null)
    {
        _configService = configService;
        _runtimeAccessor = runtimeAccessor;
        _screenSource = screenSource;
        _visionService = visionService;
        _dialogueSafetyPolicy = dialogueSafetyPolicy;
        _playerDialoguePolicy = playerDialoguePolicy;
        _dialogueExitPolicy = dialogueExitPolicy;
        _triggerCaptureIsolation = triggerCaptureIsolation;
        _sessionStore = sessionStore;
        _controlState = controlState;
        _actionExecutor = actionExecutor;
        _taskStopService = taskStopService;
        _logger = logger;
        _actionExecutor.ExecutionAttempt += OnExecutionAttempt;
        _actionExecutor.InputEvidence += OnInputEvidence;
        _configService.Get().ExternalAccessConfig.PropertyChanged += OnConfigChanged;
    }

    /// <inheritdoc />
    public bool IsRunning
    {
        get
        {
            lock (_gate)
            {
                return _state is not null;
            }
        }
    }

    /// <inheritdoc />
    public async Task<LiveStatusDto> StartAsync(LiveStartRequest request, CancellationToken cancellationToken)
    {
        using var operation = _controlState.EnterStartOperation();
        ArgumentNullException.ThrowIfNull(request);
        ValidateStartRequest(request);
        var dialogueCalibration = LiveDialogueCalibration.Copy(request.DialogueCalibration)
            ?? _dialogueSafetyPolicy?.GetTrustedCalibration();
        await _lifecycle.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            EnsureEnabled();
            LiveSessionState? existing;
            var needsIsolation = false;
            lock (_gate)
            {
                existing = _state;
                if (existing is not null)
                {
                    if (dialogueCalibration is not null)
                        throw AgentException.InvalidRequest("会话运行期间不能更改对话区域校准；请停止后重新启动。");
                    if (request.RequireControlLease)
                    {
                        _actionExecutor.RequireLease();
                        needsIsolation = existing.TriggerPause is null;
                        if (needsIsolation) existing.TriggerPausePending = true;
                    }
                    if (request.RequireStrictSafety)
                        _actionExecutor.UpdateSafety(() => _strictUiSafety = true);
                    if (!needsIsolation) return BuildStatusLocked();
                }
            }
            if (existing is not null)
            {
                IDisposable? acquired = await AcquireTriggerPauseAsync().ConfigureAwait(false);
                try
                {
                    lock (_gate)
                    {
                        cancellationToken.ThrowIfCancellationRequested();
                        EnsureEnabled();
                        if (!ReferenceEquals(_state, existing) || existing.Cancellation.IsCancellationRequested
                            || !ReferenceEquals(_runtimeAccessor.Current, existing.Runtime)
                            || existing.Runtime.IsStopping || !existing.Runtime.Window.IsAlive)
                            throw AgentException.RuntimeUnavailable("实时会话已失效，无法隔离触发器。");
                        existing.TriggerPause = acquired;
                        existing.TriggerPausePending = false;
                        acquired = null;
                        return BuildStatusLocked();
                    }
                }
                finally
                {
                    acquired?.Dispose();
                }
            }

            var runtime = _runtimeAccessor.Current;
            if (runtime is null || runtime.IsStopping || !runtime.Window.IsAlive)
            {
                throw AgentException.RuntimeUnavailable("实时会话需要先启动游戏截图器。");
            }

            // 启动期也监听全局停止，避免耗时建会话后在停止信号之后启动。
            using var startupCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            using var startupStop = _taskStopService.Register(_ => startupCancellation.Cancel());
            using var startupRuntime = runtime.Stopping.Register(startupCancellation.Cancel);
            var session = await _sessionStore.EnsureSessionAsync(request.SessionId, null, startupCancellation.Token)
                .ConfigureAwait(false);
            if (!await TaskControl.TaskSemaphore.WaitAsync(0, startupCancellation.Token).ConfigureAwait(false))
            {
                throw AgentException.TaskRunning();
            }

            var state = new LiveSessionState
            {
                SessionId = session.Id,
                Runtime = runtime,
                Fps = request.Fps,
                Quality = request.Quality,
                MaxWidth = request.MaxWidth,
                RecordFrames = request.RecordFrames,
                RecordEvery = Math.Max(1, request.Fps / Math.Max(1, request.RecordFps)),
                DialogueCalibration = dialogueCalibration,
                AllowBackgroundInput = request.AllowBackgroundInput,
                Cancellation = new CancellationTokenSource()
            };
            try
            {
                state.StopRegistration = _taskStopService.Register(_ => RequestSessionStop(state));
                state.RuntimeRegistration = runtime.Stopping.Register(() => RequestSessionStop(state));
                using var startup = CancellationTokenSource.CreateLinkedTokenSource(
                    startupCancellation.Token, state.Cancellation.Token);
                if (request.RequireControlLease)
                {
                    state.TriggerPausePending = true;
                    state.TriggerPause = await AcquireTriggerPauseAsync().ConfigureAwait(false);
                    startup.Token.ThrowIfCancellationRequested();
                    if (!ReferenceEquals(_runtimeAccessor.Current, runtime) || runtime.IsStopping || !runtime.Window.IsAlive)
                        throw AgentException.RuntimeUnavailable("运行环境已失效，无法隔离触发器。");
                    state.TriggerPausePending = false;
                }
                await AddTimelineEventAsync(state, "live.started", null,
                    $"fps={request.Fps}, maxWidth={request.MaxWidth}", null, startup.Token).ConfigureAwait(false);
                startup.Token.ThrowIfCancellationRequested();
                EnsureEnabled();
                _actionExecutor.Reset();
                _actionExecutor.BeginLeaseSession(request.RequireControlLease);
                _actionExecutor.ConfigureBackgroundInput(state.AllowBackgroundInput);
                _actionExecutor.SetSafetyCheck(() => IsFrameSafe(state));
                _actionExecutor.UpdateSafety(() =>
                {
                    if (request.RequireStrictSafety) _strictUiSafety = true;
                    _talkLatched = true;
                }, stopAlways: true);
                lock (_gate)
                {
                    startup.Token.ThrowIfCancellationRequested();
                    _state = state;
                    state.LoopTask = Task.Run(() => RunFrameLoopAsync(state), CancellationToken.None);
                    Broadcast(new LiveServerMessage { Type = "live.started", SessionId = state.SessionId });
                }
            }
            catch
            {
                state.Cancellation.Cancel();
                _actionExecutor.StopActions();
                _actionExecutor.SetSafetyCheck(null);
                state.RuntimeRegistration.Dispose();
                state.StopRegistration?.Dispose();
                try
                {
                    Interlocked.Exchange(ref state.TriggerPause, null)?.Dispose();
                }
                finally
                {
                    state.Cancellation.Dispose();
                    TaskControl.TaskSemaphore.Release();
                }
                throw;
            }

            _logger.LogInformation("实时会话已启动：{SessionId}，FPS={Fps}", session.Id, request.Fps);
            return GetStatus();
        }
        finally
        {
            _lifecycle.Release();
        }
    }

    /// <inheritdoc />
    public Task StopAsync(CancellationToken cancellationToken) => StopCoreAsync(null, cancellationToken);

    /// <inheritdoc />
    public Task WaitForBackgroundVisionAsync(CancellationToken cancellationToken) =>
        (Volatile.Read(ref _visionDrain)?.Task ?? Task.CompletedTask).WaitAsync(cancellationToken);

    /// <inheritdoc />
    public async Task WaitForBackgroundStopsAsync(CancellationToken cancellationToken)
    {
        while (true)
        {
            Task[] pending;
            lock (_pendingStopsGate) pending = _pendingStops.ToArray();
            if (pending.Length == 0) return;
            await Task.WhenAll(pending).WaitAsync(cancellationToken).ConfigureAwait(false);
            lock (_pendingStopsGate) _pendingStops.RemoveWhere(task => task.IsCompleted);
        }
    }

    /// <summary>仅停止指定会话；空值表示停止当前会话。</summary>
    private async Task StopCoreAsync(LiveSessionState? expected, CancellationToken cancellationToken)
    {
        await _lifecycle.WaitAsync(CancellationToken.None).ConfigureAwait(false);
        try
        {
            LiveSessionState? state;
            lock (_gate)
            {
                state = _state;
                if (expected is not null && !ReferenceEquals(expected, state))
                {
                    return;
                }

                _state = null;
                _actionExecutor.StopActions();
                _actionExecutor.UpdateSafety(() => _talkLatched = true, stopAlways: true);
                _actionExecutor.SetSafetyCheck(null);
            }

            if (state is null)
            {
                return;
            }

            state.Cancellation.Cancel();
            state.RuntimeRegistration.Dispose();
            state.StopRegistration?.Dispose();
            var loop = state.LoopTask ?? Task.CompletedTask;
            try
            {
                await loop.WaitAsync(TimeSpan.FromSeconds(3), cancellationToken).ConfigureAwait(false);
                FinishState(state);
            }
            catch (Exception exception) when (exception is OperationCanceledException or TimeoutException)
            {
                // 截图可能仍在进行；直到循环结束才释放独立任务锁和取消源。
                _logger.LogWarning("实时会话采集循环尚未结束，延后释放任务锁");
                _ = loop.ContinueWith(_ => FinishState(state), CancellationToken.None,
                    TaskContinuationOptions.ExecuteSynchronously, TaskScheduler.Default);
            }
            catch (Exception exception)
            {
                _logger.LogWarning(exception, "实时会话采集循环异常结束");
                FinishState(state);
            }

            state.LatestFrame = null;
            await AddTimelineEventAsync(state, "live.stopped", null, null, null, CancellationToken.None)
                .ConfigureAwait(false);
            Broadcast(new LiveServerMessage { Type = "live.stopped", SessionId = state.SessionId });
            _logger.LogInformation("实时会话已停止：{SessionId}", state.SessionId);
        }
        finally
        {
            _lifecycle.Release();
        }
    }

    /// <inheritdoc />
    public LiveStatusDto GetStatus()
    {
        lock (_gate)
        {
            return BuildStatusLocked();
        }
    }

    /// <inheritdoc />
    public LiveFrameSnapshot? GetLatestFrame()
    {
        lock (_gate)
        {
            return _state?.LatestFrame;
        }
    }

    /// <inheritdoc />
    public async Task<LiveTimelineEventDto> SetGuidanceAsync(LiveGuidanceRequest request,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(request);
        EnsureEnabled();
        var text = request.Text?.Trim() ?? string.Empty;
        if (text.Length == 0 || text.Length > MaxGuidanceLength)
        {
            throw AgentException.InvalidRequest($"指导文本长度必须在 1～{MaxGuidanceLength} 个字符之间。");
        }

        var state = GetRunningState();
        state.Guidance = text;
        var evt = await AddTimelineEventAsync(state, "guidance", null, text, request.Source, cancellationToken)
            .ConfigureAwait(false);
        Broadcast(new LiveServerMessage
        {
            Type = "guidance",
            Seq = evt.Seq,
            TimestampUtc = evt.TimestampUtc,
            Text = text,
            Source = request.Source,
            Guidance = text
        });
        return evt;
    }

    /// <inheritdoc />
    public async Task<LiveTimelineEventDto> AddCommentaryAsync(LiveCommentaryRequest request,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(request);
        EnsureEnabled();
        var kind = string.IsNullOrWhiteSpace(request.Kind) ? "reaction" : request.Kind.Trim().ToLowerInvariant();
        if (!CommentaryKinds.Contains(kind))
        {
            throw AgentException.InvalidRequest(
                "解说类型只能是 reaction、plan、emotion、lore、chat 或 result。");
        }

        var text = request.Text?.Trim() ?? string.Empty;
        if (text.Length == 0 || text.Length > MaxCommentaryLength)
        {
            throw AgentException.InvalidRequest($"解说文本长度必须在 1～{MaxCommentaryLength} 个字符之间。");
        }

        if (request.Emotion is { Length: > 32 })
        {
            throw AgentException.InvalidRequest("情绪标签不能超过 32 个字符。");
        }

        var state = GetRunningState();
        var evt = await AddTimelineEventAsync(state, "commentary", kind, text, request.Emotion,
            cancellationToken, request.FrameSeq, request.Meta).ConfigureAwait(false);
        Broadcast(new LiveServerMessage
        {
            Type = "commentary",
            Seq = evt.Seq,
            TimestampUtc = evt.TimestampUtc,
            Kind = kind,
            Text = text,
            Emotion = request.Emotion,
            FrameSeq = request.FrameSeq,
            Meta = request.Meta
        });
        return evt;
    }

    /// <inheritdoc />
    public Task<IReadOnlyList<LiveTimelineEventDto>> GetTimelineAsync(long afterSeq, int limit,
        CancellationToken cancellationToken)
    {
        EnsureEnabled();
        cancellationToken.ThrowIfCancellationRequested();
        var take = Math.Clamp(limit, 1, 500);
        var events = _timeline
            .Where(evt => evt.Seq > afterSeq)
            .OrderBy(evt => evt.Seq)
            .Take(take)
            .ToArray();
        return Task.FromResult<IReadOnlyList<LiveTimelineEventDto>>(events);
    }

    /// <inheritdoc />
    public Task<LiveActionAckDto> EnqueueActionsAsync(LiveActionBlockDto block,
        CancellationToken cancellationToken, LiveLeaseCredentialsDto? credentials = null)
    {
        ArgumentNullException.ThrowIfNull(block);
        EnsureEnabled();
        cancellationToken.ThrowIfCancellationRequested();
        if (_controlState.IsStopRequested)
        {
            throw AgentException.Stopped();
        }

        var state = GetRunningState();
        // 不能把尚不支持的对话动作或未知字段静默当成普通键鼠动作。
        if (block.ActionKind is not null || block.Kind is not null || block.UnsupportedFields is { Count: > 0 }
            || block.Absolute?.UnsupportedFields is { Count: > 0 }
            || (block.Chunks?.Any(chunk => chunk?.UnsupportedFields is { Count: > 0 }) ?? false))
        {
            _actionExecutor.RejectUnsupportedAction(state, credentials,
                () => ReferenceEquals(Volatile.Read(ref _state), state) && !state.Cancellation.IsCancellationRequested,
                () => _strictUiSafety = true);
            throw AgentException.InvalidRequest("不支持带动作类型或未知字段的动作；对话操作尚未获授权。");
        }

        // 元数据仅用于关联，不参与动作授权或执行；原样保留合法值。
        if (block.IntentId is { } intentId &&
            (string.IsNullOrWhiteSpace(intentId) || intentId.Length > 128 || intentId.Any(char.IsControl)))
        {
            throw AgentException.InvalidRequest("intentId 必须为不超过 128 个字符的非空标识，且不能包含控制字符。");
        }

        if (block.ActionGroupId is { } actionGroupId &&
            (string.IsNullOrWhiteSpace(actionGroupId) || actionGroupId.Length > 128 || actionGroupId.Any(char.IsControl)))
        {
            throw AgentException.InvalidRequest("actionGroupId 必须为不超过 128 个字符的非空标识，且不能包含控制字符。");
        }

        if (block.MotorGeneration is < 0)
        {
            throw AgentException.InvalidRequest("motorGeneration 必须为非负整数。");
        }

        if (block.Chunks is null || block.Chunks.Count is < 1 or > 6)
        {
            throw AgentException.InvalidRequest("动作块必须包含 1～6 个键块。");
        }

        for (var index = 0; index < block.Chunks.Count; index++)
        {
            var chunk = block.Chunks[index];
            if (chunk is null || chunk.Keys is null)
            {
                throw AgentException.InvalidRequest("键块及其按键列表不能为空。");
            }

            if (chunk.Keys.Count > 4)
            {
                throw AgentException.InvalidRequest("每个 33ms 键块最多包含 4 个键。");
            }

            if (chunk.Dx is < -1000 or > 1000 || chunk.Dy is < -1000 or > 1000)
            {
                throw AgentException.InvalidRequest("鼠标相对位移必须在 -1000～1000 之间。");
            }

            if (chunk.Scroll is < -5 or > 5)
            {
                throw AgentException.InvalidRequest("滚轮刻度必须在 -5～5 之间。");
            }

            // Lumine 每个 200ms 动作块只有一个移动/滚轮前缀，只允许第 1 个键块携带，
            // 否则 6 个键块会重复应用同一段移动，导致视角转动放大 6 倍。
            if (index > 0 && (chunk.Dx != 0 || chunk.Dy != 0 || chunk.Scroll != 0))
            {
                throw AgentException.InvalidRequest(
                    "只有第 1 个 33ms 键块可以携带鼠标移动/滚轮，其余键块必须为 0。");
            }
        }

        var isV2 = block.IntentId is not null || block.MotorGeneration is not null;
        // 模式激活、检查和入队共用执行闸门；拒绝的 v2 请求也不能回退为 v1。
        var accepted = _actionExecutor.Enqueue(block, out var attemptId, isV2 ? () => _strictUiSafety = true : null,
            () => ReferenceEquals(Volatile.Read(ref _state), state) && !state.Cancellation.IsCancellationRequested,
            state, credentials);
        if (accepted)
        {
            // 回显已接受的动作块，让录制器与叠加层拿到鼠标位移和模型思考；
            // 动作执行本身仍只由执行器完成，回显不产生任何二次输入。
            Broadcast(new LiveServerMessage
            {
                Type = "action",
                AttemptId = attemptId,
                Seq = block.Seq,
                TimestampUtc = DateTimeOffset.UtcNow,
                IntentId = block.IntentId,
                MotorGeneration = block.MotorGeneration,
                ActionGroupId = block.ActionGroupId,
                Block = block,
                Text = block.Thought
            });
        }

        var status = _actionExecutor.GetStatus();
        return Task.FromResult(new LiveActionAckDto
        {
            Accepted = accepted,
            AttemptId = attemptId,
            Seq = block.Seq,
            IntentId = block.IntentId,
            MotorGeneration = block.MotorGeneration,
            ActionGroupId = block.ActionGroupId,
            QueueDepth = status.QueueDepth,
            Message = accepted ? null : "动作块已拒绝（画面、安全状态或执行器不可用）。"
        });
    }

    /// <inheritdoc />
    public LiveDialogueDecisionResult SubmitDialogueDecision(LiveDialogueDecisionRequest request,
        LiveLeaseCredentialsDto? credentials)
    {
        ArgumentNullException.ThrowIfNull(request);
        EnsureEnabled();
        LiveDialogueDecisionResult Reject(string reason) => new()
        {
            IntentId = request.IntentId, Executed = false, Reason = reason
        };
        static bool ValidId(string? value, int max) => !string.IsNullOrWhiteSpace(value)
            && value == value.Trim() && value.Length <= max && !value.Any(char.IsControl);
        if (request.UnsupportedFields is { Count: > 0 } || !ValidId(request.IntentId, 80)
            || !ValidId(request.DecisionId, 128) || !ValidId(request.EventSessionId, 512)
            || !ValidId(request.SessionId, 128) || !ValidId(request.FrameId, 128)
            || !ValidId(request.SourceEventId, 512) || request.FrameSeq <= 0
            || request.CapturedAtMs <= 0 || request.PlayerGeneration < 0
            || request.Action is not ("advance" or "select")
            || request.FrameSha256 is not { Length: 64 }
            || request.FrameSha256.Any(c => !(c is >= '0' and <= '9' or >= 'a' and <= 'f')))
            return Reject("invalid_decision");
        if (request.Action == "advance" && (request.OptionId is not null || request.OptionIndex is not null
            || request.OptionText is not null)) return Reject("invalid_decision");
        if (request.Action == "select" && (request.OptionId is not { Length: 71 }
            || !request.OptionId.StartsWith("choice-", StringComparison.Ordinal)
            || request.OptionId.AsSpan(7).ToString().Any(c => !(c is >= '0' and <= '9' or >= 'a' and <= 'f'))
            || request.OptionIndex is not (>= 0 and < 8) || !ValidId(request.OptionText, 500)))
            return Reject("invalid_decision");

        lock (_gate)
        {
            var state = _state;
            if (state is null || state.SessionId != request.SessionId || state.TriggerPausePending
                || state.TriggerPause is null) return Reject("session_unavailable");
            return _actionExecutor.EvaluateDialogueDecision(state, credentials,
                () => ReferenceEquals(Volatile.Read(ref _state), state) && !state.Cancellation.IsCancellationRequested,
                request, (executeClick, executeAdvance) =>
                {
                    if (state.DialogueDecisionIds.Count >= 1024) return Reject("decision_capacity_exhausted");
                    if (!state.DialogueDecisionIds.Add(request.DecisionId!)) return Reject("decision_replayed");
                    var frame = state.LatestFrame;
                    var evidence = frame?.DialogueObservation?.EvidenceSource;
                    var timestamp = Interlocked.Read(ref state.LastFrameTicks);
                    var wallAge = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - request.CapturedAtMs;
                    var idPrefix = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(state.SessionId)))
                        .ToLowerInvariant()[..16];
                    if (state.UiPending || !_talkLatched || timestamp == 0 || frame is null || evidence is null
                        || !FrameMatchesRuntimeGeometry(state.Runtime, frame)
                        || !frame.RuntimeState.IsAlive || !frame.RuntimeState.IsForeground || frame.RuntimeState.IsMinimized
                        || wallAge < 0 || wallAge > Math.Max(500, 3000d / state.Fps)
                        || Stopwatch.GetElapsedTime(timestamp) > TimeSpan.FromMilliseconds(Math.Max(500, 3000d / state.Fps))
                        || frame.Seq != request.FrameSeq || frame.IsInTalkUi != true || frame.UiCategory != "Talk"
                        || request.FrameId != $"live:{idPrefix}:{frame.Seq}"
                        || evidence.SessionId != state.SessionId || evidence.FrameSeq != frame.Seq
                        || evidence.CapturedAtUtc.ToUnixTimeMilliseconds() != request.CapturedAtMs
                        || evidence.JpegSha256 != request.FrameSha256
                        || Convert.ToHexString(SHA256.HashData(frame.JpegBytes)).ToLowerInvariant() != request.FrameSha256)
                        return Reject("stale_or_unbound_frame");
                    if (request.Action == "advance")
                    {
                        if (frame.DialogueObservation?.AdvanceTemplateMatched != true || _playerDialoguePolicy is null
                            || !_playerDialoguePolicy.AllowsAdvance(state.DialogueCalibration))
                            return Reject("advance_template_authorization_unavailable");
                        if (!_playerDialoguePolicy.HasCommittedAdvance(request, evidence))
                            return Reject("player_advance_binding_unverified");
                        return executeAdvance(() => ReferenceEquals(Volatile.Read(ref _state), state)
                            && !state.Cancellation.IsCancellationRequested && !state.UiPending && _talkLatched
                            && ReferenceEquals(state.LatestFrame, frame)
                            && FrameMatchesRuntimeGeometry(state.Runtime, frame)
                            && frame.DialogueObservation?.AdvanceTemplateMatched == true
                            && Interlocked.Read(ref state.LastFrameTicks) == timestamp
                            && Stopwatch.GetElapsedTime(timestamp) <= TimeSpan.FromMilliseconds(Math.Max(500, 3000d / state.Fps))
                            && DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - request.CapturedAtMs is >= 0
                            && DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - request.CapturedAtMs <= Math.Max(500, 3000d / state.Fps)
                            && _playerDialoguePolicy.AllowsAdvance(state.DialogueCalibration)
                            && Convert.ToHexString(SHA256.HashData(frame.JpegBytes)).ToLowerInvariant() == request.FrameSha256);
                    }
                    var options = frame.DialogueObservation!.Options;
                    var candidates = options.Candidates;
                    if (!options.OcrCompleted || options.OcrStatus != "Completed" || options.OcrDropScore is not float threshold
                        || !float.IsFinite(threshold) || threshold is < 0 or > 1 || candidates is null
                        || candidates.Count is < 1 or > 8 || options.RawDetectionCount != candidates.Count
                        || candidates.Any(c => c.Points.Count != 4 || c.RecognitionScore is not float score
                            || !float.IsFinite(score) || score < Math.Max(.5f, threshold)
                            || !float.IsFinite(c.DetectionScore) || c.DetectionScore < .5f || string.IsNullOrWhiteSpace(c.Text)))
                        return Reject("choice_evidence_unavailable");
                    var sorted = candidates.OrderBy(c => (c.Points.Min(p => p.Y) + c.Points.Max(p => p.Y)) / 2)
                        .ThenBy(c => (c.Points.Min(p => p.X) + c.Points.Max(p => p.X)) / 2).ToArray();
                    if (request.OptionIndex!.Value >= sorted.Length) return Reject("choice_not_found");
                    var text = string.Join(" ", sorted[request.OptionIndex!.Value].Text!.Split((char[]?)null,
                        StringSplitOptions.RemoveEmptyEntries));
                    if (!string.Equals(text, request.OptionText, StringComparison.Ordinal)) return Reject("choice_mismatch");
                    var selected = sorted[request.OptionIndex.Value];
                    if (_playerDialoguePolicy is null || !_playerDialoguePolicy.AllowsSelect(state.DialogueCalibration))
                        return Reject("dialogue_action_authorization_unavailable");
                    if (!_playerDialoguePolicy.HasCommittedChoice(request, evidence, selected))
                        return Reject("player_choice_binding_unverified");
                    var x = (selected.Points.Min(p => p.X) + selected.Points.Max(p => p.X)) / (2 * evidence.OriginalWidth);
                    var y = (selected.Points.Min(p => p.Y) + selected.Points.Max(p => p.Y)) / (2 * evidence.OriginalHeight);
                    return executeClick(x, y, () => ReferenceEquals(Volatile.Read(ref _state), state)
                        && !state.Cancellation.IsCancellationRequested && !state.UiPending && _talkLatched
                        && ReferenceEquals(state.LatestFrame, frame)
                        && FrameMatchesRuntimeGeometry(state.Runtime, frame)
                        && Interlocked.Read(ref state.LastFrameTicks) == timestamp
                        && Stopwatch.GetElapsedTime(timestamp) <= TimeSpan.FromMilliseconds(Math.Max(500, 3000d / state.Fps))
                        && DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - request.CapturedAtMs is >= 0
                        && DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - request.CapturedAtMs <= Math.Max(500, 3000d / state.Fps)
                        && _playerDialoguePolicy.AllowsSelect(state.DialogueCalibration)
                        && Convert.ToHexString(SHA256.HashData(frame.JpegBytes)).ToLowerInvariant() == request.FrameSha256);
                });
        }
    }

    /// <inheritdoc />
    public LiveLeaseGrantDto AcquireLease()
    {
        EnsureEnabled();
        lock (_gate)
        {
            var state = _state ?? throw AgentException.RuntimeUnavailable("实时会话未运行。");
            if (state.TriggerPausePending || state.TriggerPause is null)
                throw AgentException.InvalidRequest("同步截图触发器尚未隔离，Live 输入租约不可用。");
            return _actionExecutor.AcquireLease(state, () => ReferenceEquals(Volatile.Read(ref _state), state)
                && !state.Cancellation.IsCancellationRequested)
                ?? throw AgentException.InvalidRequest("Live 输入租约不可用。");
        }
    }

    /// <inheritdoc />
    public LiveLeaseBindingDto? BindPreparedLease(LiveLeaseCredentialsDto? credentials, string connectionId, string requestId)
    {
        EnsureEnabled();
        lock (_gate)
        {
            var state = _state;
            if (state is null || state.TriggerPausePending || state.TriggerPause is null) return null;
            return _actionExecutor.BindPreparedLease(state, state.SessionId, credentials, connectionId, requestId,
                () => ReferenceEquals(Volatile.Read(ref _state), state) && !state.Cancellation.IsCancellationRequested);
        }
    }

    /// <inheritdoc />
    public LiveLeaseBindingDto? ProbeBoundLease(LiveLeaseCredentialsDto? credentials, string connectionId, string requestId)
    {
        EnsureEnabled();
        lock (_gate)
        {
            var state = _state;
            if (state is null || state.TriggerPausePending || state.TriggerPause is null) return null;
            return _actionExecutor.ProbeBoundLease(state, state.SessionId, credentials, connectionId, requestId,
                () => ReferenceEquals(Volatile.Read(ref _state), state) && !state.Cancellation.IsCancellationRequested);
        }
    }

    /// <inheritdoc />
    public LiveLeaseBindingDto? ActivateBoundLease(LiveLeaseCredentialsDto? credentials, string connectionId, string requestId)
    {
        EnsureEnabled();
        lock (_gate)
        {
            var state = _state;
            if (state is null || state.TriggerPausePending || state.TriggerPause is null) return null;
            return _actionExecutor.ActivateBoundLease(state, state.SessionId, credentials, connectionId, requestId,
                () => ReferenceEquals(Volatile.Read(ref _state), state) && !state.Cancellation.IsCancellationRequested);
        }
    }

    /// <inheritdoc />
    public LiveLeaseBindingDto? CommitPreparedLease(LiveLeaseCommitRequest request)
    {
        ArgumentNullException.ThrowIfNull(request);
        EnsureEnabled();
        lock (_gate)
        {
            var state = _state;
            if (state is null || state.TriggerPausePending || state.TriggerPause is null) return null;
            return _actionExecutor.CommitPreparedLease(state, state.SessionId, request,
                () => ReferenceEquals(Volatile.Read(ref _state), state) && !state.Cancellation.IsCancellationRequested);
        }
    }

    /// <inheritdoc />
    public bool RenewLease(LiveLeaseCredentialsDto? credentials)
    {
        EnsureEnabled();
        lock (_gate)
        {
            var state = _state;
            return state is not null && _actionExecutor.RenewLease(state, credentials,
                () => ReferenceEquals(Volatile.Read(ref _state), state) && !state.Cancellation.IsCancellationRequested);
        }
    }

    /// <inheritdoc />
    public bool ValidateActionLease(LiveLeaseCredentialsDto? credentials)
    {
        EnsureEnabled();
        lock (_gate)
        {
            var state = _state;
            return state is not null && _actionExecutor.ValidateActionLease(state, credentials,
                () => ReferenceEquals(Volatile.Read(ref _state), state) && !state.Cancellation.IsCancellationRequested);
        }
    }

    /// <inheritdoc />
    public bool ReleaseActionLease(LiveLeaseCredentialsDto? credentials)
    {
        lock (_gate)
        {
            var state = _state;
            return state is not null && _actionExecutor.ReleaseLease(state, credentials, owner: false);
        }
    }

    /// <inheritdoc />
    public bool ReleaseLease(LiveLeaseCredentialsDto? credentials)
    {
        lock (_gate)
        {
            var state = _state;
            return state is not null && _actionExecutor.ReleaseLease(state, credentials);
        }
    }

    /// <inheritdoc />
    public void StopActions()
    {
        _actionExecutor.StopActions();
    }

    /// <inheritdoc />
    public void StopLegacyActions() => _actionExecutor.StopLegacyActions();

    /// <inheritdoc />
    public LiveStopReceiptDto StopActionsWithEvidence(string stopRequestId)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(stopRequestId);
        lock (_gate)
        {
            var state = _state;
            var evidence = _actionExecutor.StopActionsWithEvidence(state, state?.SessionId);
            var sessionId = state is not null && !state.Cancellation.IsCancellationRequested
                ? state.SessionId : null;
            return new LiveStopReceiptDto
            {
                StopRequestId = stopRequestId,
                SessionId = sessionId,
                Safety = sessionId is not null ? evidence : new LiveStopSafetyObservationDto
                {
                    Scope = "stop_atomic",
                    Epoch = evidence.Epoch,
                    TrackedKeyCount = evidence.TrackedKeyCount,
                    InputReleaseCallsReturned = evidence.InputReleaseCallsReturned,
                    InputReleaseConfirmed = null,
                    SystemReleaseRequestsAccepted = null
                }
            };
        }
    }

    /// <inheritdoc />
    public void AttachClient(LiveClientConnection client)
    {
        ArgumentNullException.ThrowIfNull(client);
        lock (_gate)
        {
            var state = _state;
            var frame = state?.LatestFrame;
            var guidance = state?.Guidance;
            if (!_clients.TryAdd(client.Id, client))
                throw new InvalidOperationException("客户端标识已存在。");
            try
            {
                client.TryEnqueue(new LiveServerMessage
                {
                    Type = "hello", SessionId = state?.SessionId, Fps = state?.Fps,
                    Width = frame?.Width ?? state?.MaxWidth, Height = frame?.Height,
                    ActionIntervalMs = LiveActionExecutor.ChunkIntervalMs, ChunksPerAction = 6,
                    ExecutionFeedbackProtocol = state is not null ? "intent-v2-system-input-v1" : null
                });
                if (frame is not null) client.TryEnqueue(BuildFrameMessage(frame, guidance));
                if (guidance is { Length: > 0 })
                    client.TryEnqueue(new LiveServerMessage { Type = "guidance", Text = guidance });
            }
            catch
            {
                _clients.TryRemove(client.Id, out _);
                throw;
            }
        }
    }

    /// <inheritdoc />
    public void DetachClient(LiveClientConnection client)
    {
        ArgumentNullException.ThrowIfNull(client);
        lock (_gate)
        {
            if (_clients.TryGetValue(client.Id, out var current) && ReferenceEquals(current, client))
            {
                _clients.TryRemove(client.Id, out _);
                client.Dispose();
            }
        }
    }

    /// <summary>释放服务。</summary>
    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        _configService.Get().ExternalAccessConfig.PropertyChanged -= OnConfigChanged;
        LiveSessionState? state;
        lock (_gate)
        {
            state = _state;
        }

        try
        {
            StopAsync(CancellationToken.None).GetAwaiter().GetResult();
        }
        catch (Exception exception)
        {
            _logger.LogDebug(exception, "释放实时会话时发生异常");
        }

        // StopAsync 在循环 3 秒内未结束时会延后释放；Dispose 返回前再等一次，
        // 避免调用方随后释放截图源（OpenCV Mat）时采集循环仍在读取。
        if (state?.LoopTask is { IsCompleted: false } loop)
        {
            try
            {
                loop.Wait(TimeSpan.FromSeconds(5));
            }
            catch (Exception exception)
            {
                _logger.LogDebug(exception, "等待实时会话采集循环结束时发生异常");
            }
        }

        foreach (var client in _clients.Values)
        {
            client.Dispose();
        }

        // 执行器是独立单例，依赖注入容器也会释放；其 Dispose 已做幂等处理。
        _actionExecutor.ExecutionAttempt -= OnExecutionAttempt;
        _actionExecutor.InputEvidence -= OnInputEvidence;
        _actionExecutor.Dispose();
    }

    /// <summary>接到全局停止或运行环境退出时立即停输入，并异步结束会话。</summary>
    private void RequestSessionStop(LiveSessionState state)
    {
        state.Cancellation.Cancel();
        _actionExecutor.StopActions();
        var stop = Task.Run(async () =>
        {
            try
            {
                await StopCoreAsync(state, CancellationToken.None).ConfigureAwait(false);
            }
            catch (Exception exception)
            {
                _logger.LogWarning(exception, "全局停止实时会话失败");
            }
        });
        lock (_pendingStopsGate) _pendingStops.Add(stop);
        _ = stop.ContinueWith(completed =>
        {
            lock (_pendingStopsGate) _pendingStops.Remove(completed);
        }, CancellationToken.None, TaskContinuationOptions.ExecuteSynchronously, TaskScheduler.Default);
    }

    /// <summary>等待真实暂停完成，即使调用方取消也先接收句柄供回滚。</summary>
    private async Task<IDisposable> AcquireTriggerPauseAsync()
    {
        var isolation = _triggerCaptureIsolation
            ?? throw AgentException.RuntimeUnavailable("同步截图触发器隔离不可用。");
        return await Task.Run(isolation.Pause).ConfigureAwait(false)
            ?? throw AgentException.RuntimeUnavailable("同步截图触发器隔离未返回句柄。");
    }

    /// <summary>循环确已结束后释放隔离句柄、独立任务锁及其资源。</summary>
    private static void FinishState(LiveSessionState state)
    {
        if (Interlocked.Exchange(ref state.Finished, 1) != 0) return;
        try
        {
            Interlocked.Exchange(ref state.TriggerPause, null)?.Dispose();
        }
        finally
        {
            state.Cancellation.Dispose();
            TaskControl.TaskSemaphore.Release();
        }
    }

    /// <summary>检查当前会话最近一次成功截图是否足够新鲜。</summary>
    /// <summary>无界面旧帧只能在游戏物理区域与 DPI 未变化时用于输入。</summary>
    internal static bool SamePhysicalViewport(GameViewport viewport, AgentCaptureAreaDto? capture) =>
        capture is not null && viewport.ScreenRect.X == capture.X && viewport.ScreenRect.Y == capture.Y
        && viewport.Width == capture.Width && viewport.Height == capture.Height
        && float.IsFinite(viewport.DpiScale) && viewport.DpiScale > 0 && viewport.DpiScale == capture.DpiScale;

    /// <summary>读取当前物理区域失败时拒绝无界面输入。</summary>
    private static bool FrameMatchesRuntimeGeometry(GameRuntime runtime, LiveFrameSnapshot frame)
    {
        if (!CommandLineOptions.Instance.IsHeadless) return true;
        try { return SamePhysicalViewport(runtime.Window.Viewport, frame.Capture); }
        catch { return false; }
    }

    private bool IsFrameSafe(LiveSessionState state)
    {
        if (!ReferenceEquals(Volatile.Read(ref _state), state) || state.Cancellation.IsCancellationRequested)
        {
            _talkLatched = true;
            state.ExitTemplateFrames = 0;
            state.ExitTemplateUnlocked = false;
            return false;
        }

        var runtime = _runtimeAccessor.Current;
        if (!ReferenceEquals(runtime, state.Runtime) || runtime is null || runtime.IsStopping
            || !runtime.Window.IsAlive || runtime.Window.IsMinimized
            || (!runtime.Window.IsForeground && !state.AllowBackgroundInput))
        {
            Interlocked.Exchange(ref state.LastFrameTicks, 0);
            state.DialogueCandidateFrames = 0;
            state.ExitTemplateFrames = 0;
            state.ExitTemplateUnlocked = false;
            _talkLatched = true;
            return false;
        }

        var timestamp = Interlocked.Read(ref state.LastFrameTicks);
        var frame = state.LatestFrame;
        if (timestamp == 0 || frame is null || !frame.RuntimeState.IsAlive
            || (!frame.RuntimeState.IsForeground && !state.AllowBackgroundInput)
            || frame.RuntimeState.IsMinimized || !FrameMatchesRuntimeGeometry(runtime, frame)
            || Stopwatch.GetElapsedTime(timestamp) > TimeSpan.FromMilliseconds(Math.Max(500, 3000d / state.Fps))
            || (state.ExitTemplateUnlocked && _dialogueExitPolicy?.IsAvailable(state.DialogueCalibration) != true))
        {
            _talkLatched = true;
            state.ExitTemplateFrames = 0;
            state.ExitTemplateUnlocked = false;
            return false;
        }
        // 只按已应用的最新帧授权：帧间隔 200 毫秒、识别约 250 毫秒时，
        // UiPending 几乎恒为真，若把它当作硬条件，租约将随机永远拿不到。
        // 代价是正在识别的这一帧尚未出结果前可能放出一个六十分之一秒的动作块；
        // 每个动作块都会重新检查安全，一旦识别为对话立即落锁并停手。
        return !_talkLatched
               && (!_strictUiSafety || (frame.IsDistinct
                   && frame.IsInTalkUi == false && (frame.UiCategory is "Main" or "BigMap")));
    }

    /// <summary>采集与广播循环。</summary>
    private async Task RunFrameLoopAsync(LiveSessionState state)
    {
        var interval = TimeSpan.FromMilliseconds(1000d / state.Fps);
        var stopwatch = Stopwatch.StartNew();
        var failures = 0;

        while (!state.Cancellation.IsCancellationRequested)
        {
            stopwatch.Restart();
            // 下一帧未知期间，严格模式不能继续使用上一帧；与注入互斥。
            _actionExecutor.UpdateSafety(() => state.UiPending = true);
            var captureStarted = Stopwatch.GetTimestamp();
            var capturedAtUtc = DateTimeOffset.UtcNow;
            try
            {
                using var captured = await Task.Run(() => _screenSource.Capture(), state.Cancellation.Token)
                    .ConfigureAwait(false);
                // 编码与分析均从同一个独立快照派生，避免捕获源复用缓冲区。
                using var frame = new AgentFrame
                {
                    Image = captured.Image.Clone(), SystemInfo = captured.SystemInfo,
                    CaptureArea = captured.CaptureArea, Runtime = captured.Runtime
                };
                var frameIdentity = new object();
                // 原始帧的独立副本用于后台分析，超时后不会阻塞帧循环或释放原图。
                var visionTask = TryStartVision(frame, state, frameIdentity);
                var encoded = AgentImageEncoder.Encode(frame.Image, state.MaxWidth, "jpg", state.Quality);
                // 识别预算必须覆盖单帧分析耗时，否则每帧结果都会被丢弃，
                // 严格安全永远看不到主界面并一直保持对话锁。
                var visionBudgetMs = state.DialogueCalibration is not null && _talkLatched
                    ? 350
                    : Math.Clamp((int)(1000d / state.Fps), 120, 600);
                var analysis = await GetFrameVisionAsync(visionTask, visionBudgetMs,
                    state.Cancellation.Token).ConfigureAwait(false);
                var fingerprint = GetFrameFingerprint(frame);
                if (analysis is not null && (!ReferenceEquals(analysis.FrameIdentity, frameIdentity)
                    || fingerprint is null || analysis.FrameFingerprint is null
                    || !CryptographicOperations.FixedTimeEquals(fingerprint, analysis.FrameFingerprint))) analysis = null;
                var vision = analysis?.Vision;
                var seq = Interlocked.Increment(ref state.FrameSeq);
                LiveFrameSnapshot? snapshot = null;
                state.Cancellation.Token.ThrowIfCancellationRequested();
                _actionExecutor.UpdateSafety(() =>
                {
                    state.Cancellation.Token.ThrowIfCancellationRequested();
                    var fingerprintKey = fingerprint is null ? null : Convert.ToHexString(fingerprint);
                    var distinct = fingerprintKey is not null && _lastFrameFingerprint is not null
                                   && !_recentFrameFingerprintSet.Contains(fingerprintKey);
                    _lastFrameFingerprint = fingerprint;
                    if (fingerprintKey is not null && _recentFrameFingerprintSet.Add(fingerprintKey))
                    {
                        _recentFrameFingerprints.Enqueue(fingerprintKey);
                        if (_recentFrameFingerprints.Count > 32)
                        {
                            _recentFrameFingerprintSet.Remove(_recentFrameFingerprints.Dequeue());
                        }
                    }
                    if (vision?.IsInTalkUi == true || vision?.UiCategory == "Talk")
                    {
                        _talkLatched = true;
                    }

                    // 完整原始检测结果加结构只形成候选；绝不据此解除对话锁。
                    var independentAndFresh = distinct && frame.Runtime.IsAlive && frame.Runtime.IsForeground
                        && !frame.Runtime.IsMinimized && ReferenceEquals(_runtimeAccessor.Current, state.Runtime)
                        && !state.Runtime.IsStopping && state.Runtime.Window.IsAlive
                        && state.Runtime.Window.IsForeground && !state.Runtime.Window.IsMinimized
                        && Stopwatch.GetElapsedTime(captureStarted) <= TimeSpan.FromMilliseconds(Math.Max(500, 3000d / state.Fps));
                    var exitAvailable = _dialogueExitPolicy?.IsAvailable(state.DialogueCalibration) == true;
                    var recognizedSafeUi = vision?.IsInTalkUi == false && (vision.UiCategory is "Main" or "BigMap");
                    if (!independentAndFresh || !exitAvailable || !recognizedSafeUi)
                    {
                        _talkLatched = true;
                        state.ExitTemplateUnlocked = false;
                    }
                    if (_talkLatched)
                    {
                        var exitMatch = independentAndFresh && exitAvailable && vision?.IsInTalkUi == false
                            && vision.UiCategory == "Main" && analysis?.ExitTemplateMatched == true;
                        state.ExitTemplateFrames = exitMatch ? state.ExitTemplateFrames + 1 : 0;
                        var exitRequired = _dialogueExitPolicy?.RequiredFrames ?? 0;
                        if (exitRequired is >= 2 and <= 10 && state.ExitTemplateFrames >= exitRequired
                            && _dialogueExitPolicy?.IsAvailable(state.DialogueCalibration) == true)
                        {
                            _talkLatched = false;
                            state.ExitTemplateUnlocked = true;
                            state.ExitTemplateFrames = 0;
                        }
                    }
                    else state.ExitTemplateFrames = 0;
                    if (state.LastFrameTicks != 0 && Stopwatch.GetElapsedTime(state.LastFrameTicks, captureStarted)
                        > TimeSpan.FromMilliseconds(Math.Max(500, 3000d / state.Fps)))
                    {
                        state.DialogueCandidateFrames = 0;
                        state.ExitTemplateFrames = 0;
                        state.ExitTemplateUnlocked = false;
                        _talkLatched = true;
                    }
                    var required = state.DialogueCalibration?.ExitFrames ?? 0;
                    var authorized = LiveDialogueCandidateTracker.Authorize(_dialogueSafetyPolicy, state.DialogueCalibration);
                    state.DialogueCandidateFrames = LiveDialogueCandidateTracker.Advance(state.DialogueCandidateFrames,
                        required, authorized, _talkLatched, independentAndFresh, vision, analysis?.Dialogue);
                    var dialogue = analysis?.Dialogue is { } observed
                        && (observed.Options.Candidates is null || (observed.Options.OcrCompleted
                            && observed.Options.OcrStatus == "Completed"
                            && observed.Options.OcrDropScore is float score && float.IsFinite(score)
                            && observed.Options.RawDetectionCount == observed.Options.Candidates.Count))
                        ? new LiveDialogueObservationDto
                    {
                        Message = observed.Message,
                        Options = observed.Options,
                        EvidenceSource = new LiveDialogueEvidenceSourceDto
                        {
                            SessionId = state.SessionId, FrameSeq = seq,
                            JpegSha256 = Convert.ToHexString(SHA256.HashData(encoded.Bytes)).ToLowerInvariant(),
                            OriginalWidth = frame.OriginalWidth, OriginalHeight = frame.OriginalHeight,
                            CapturedAtUtc = capturedAtUtc
                        },
                        AdvanceTemplateMatched = analysis?.AdvanceTemplateMatched == true,
                        ExitCandidateFrames = state.DialogueCandidateFrames,
                        ExitCandidateState = !authorized ? "disabled"
                            : state.DialogueCandidateFrames >= required ? "candidate" : "collecting"
                    } : null;

                    snapshot = new LiveFrameSnapshot
                    {
                        Seq = seq,
                        TimestampUtc = capturedAtUtc,
                        UiCategory = vision?.UiCategory ?? "Unknown",
                        IsInTalkUi = vision?.IsInTalkUi,
                        DialogueObservation = dialogue,
                        IsDistinct = distinct,
                        JpegBytes = encoded.Bytes,
                        Width = encoded.Width,
                        Height = encoded.Height,
                        Capture = frame.CaptureArea.ToDto(),
                        RuntimeState = new LiveRuntimeStateDto
                        {
                            IsAlive = frame.Runtime.IsAlive,
                            IsForeground = frame.Runtime.IsForeground,
                            IsMinimized = frame.Runtime.IsMinimized,
                            RequiresForeground = frame.Runtime.RequiresForeground
                        }
                    };
                    state.LatestFrame = snapshot;
                    Interlocked.Exchange(ref state.LastFrameTicks, captureStarted);
                    state.UiPending = false;
                });

                failures = 0;
                Broadcast(BuildFrameMessage(snapshot!, state.Guidance));
                Broadcast(BuildActionStateMessage());
                if (state.RecordFrames && seq % state.RecordEvery == 0)
                {
                    await TryRecordFrameAsync(state, snapshot!, state.Cancellation.Token).ConfigureAwait(false);
                }
            }
            catch (OperationCanceledException) when (state.Cancellation.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                Interlocked.Exchange(ref state.LastFrameTicks, 0);
                _actionExecutor.UpdateSafety(() =>
                {
                    state.DialogueCandidateFrames = 0;
                    state.ExitTemplateFrames = 0;
                    state.ExitTemplateUnlocked = false;
                    _talkLatched = true;
                }, stopAlways: true);
                failures++;
                _logger.LogWarning(exception, "实时会话截帧失败（连续 {Failures} 次）", failures);
                Broadcast(new LiveServerMessage
                {
                    Type = "error",
                    Code = "LIVE_CAPTURE_FAILED",
                    Message = exception.Message
                });
                try
                {
                    await Task.Delay(500, state.Cancellation.Token).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (state.Cancellation.IsCancellationRequested)
                {
                    break;
                }
            }

            var delay = interval - stopwatch.Elapsed;
            if (delay < TimeSpan.Zero)
            {
                delay = TimeSpan.Zero;
            }

            try
            {
                await Task.Delay(delay, state.Cancellation.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (state.Cancellation.IsCancellationRequested)
            {
                break;
            }
        }
    }

    /// <summary>计算原始像素指纹，无法计算时安全门视为未知；不依赖 JPEG 参数。</summary>
    private byte[]? GetFrameFingerprint(AgentFrame frame)
    {
        try
        {
            var image = frame.Image;
            var rowLength = checked((int)(image.Cols * image.ElemSize()));
            if (rowLength <= 0 || image.Rows <= 0 || image.Data == IntPtr.Zero)
            {
                return null;
            }

            using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
            hash.AppendData(BitConverter.GetBytes(image.Rows));
            hash.AppendData(BitConverter.GetBytes(image.Cols));
            hash.AppendData(BitConverter.GetBytes(image.ElemSize()));
            var row = new byte[rowLength];
            for (var y = 0; y < image.Rows; y++)
            {
                Marshal.Copy(IntPtr.Add(image.Data, checked((int)(y * image.Step()))), row, 0, row.Length);
                hash.AppendData(row);
            }
            return hash.GetHashAndReset();
        }
        catch (Exception exception)
        {
            _logger.LogDebug(exception, "实时帧指纹计算失败");
            return null;
        }
    }

    /// <summary>无排队地启动视觉分析；任务独占帧副本直到识别结束。</summary>
    private Task<LiveFrameAnalysis?>? TryStartVision(AgentFrame frame, LiveSessionState state, object frameIdentity)
    {
        if (Interlocked.CompareExchange(ref _visionBusy, 1, 0) != 0)
        {
            return null;
        }

        var completion = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        Volatile.Write(ref _visionDrain, completion);
        AgentFrame? copy = null;
        try
        {
            copy = new AgentFrame
            {
                Image = frame.Image.Clone(),
                SystemInfo = frame.SystemInfo,
                CaptureArea = frame.CaptureArea,
                Runtime = frame.Runtime
            };
            var ownedFrame = copy;
            return Task.Run<LiveFrameAnalysis?>(() =>
            {
                try
                {
                    // 先发布对话锁；区域诊断耗时或迟到都不能延迟清队。
                    using (ownedFrame)
                    {
                        var result = _visionService.Analyze(ownedFrame, true, false, null);
                        if (result?.IsInTalkUi == true || result?.UiCategory == "Talk")
                        {
                            // 即使结果超过帧等待期限，也必须保守锁定；不能把它套用到新帧。
                            _actionExecutor.UpdateSafety(() =>
                            {
                                _talkLatched = true;
                                state.DialogueCandidateFrames = 0;
                                var current = Volatile.Read(ref _state);
                                if (current is not null) current.DialogueCandidateFrames = 0;
                            });
                        }
                        if (result is null || string.IsNullOrWhiteSpace(result.UiCategory)) return null;
                        var talkUi = result.IsInTalkUi == true || result.UiCategory == "Talk";
                        LiveDialogueObservationDto? dialogue = null;
                        var advanceMatched = false;
                        var exitMatched = false;
                        if (state.DialogueCalibration is { } calibration)
                        {
                            // 模板匹配成本很低，必须在帧等待窗口内完成，否则退出证据永远累积不起来。
                            if (_talkLatched)
                            {
                                try { advanceMatched = _playerDialoguePolicy?.MatchesAdvanceTemplate(ownedFrame, calibration) == true; }
                                catch (Exception exception) { _logger.LogDebug(exception, "仅推进模板匹配失败"); }
                                try { exitMatched = _dialogueExitPolicy?.Matches(ownedFrame, calibration) == true; }
                                catch (Exception exception) { _logger.LogDebug(exception, "主界面退出模板匹配失败"); }
                            }
                            // 区域 OCR 只在疑似对话界面执行：主界面每帧做 OCR 会拖垮帧循环，
                            // 使每帧识别都超时被丢弃，严格安全永远等不到主界面证据。
                            if (talkUi)
                            {
                                try { dialogue = _visionService.AnalyzeDialogue(ownedFrame, calibration); }
                                catch (Exception exception) { _logger.LogDebug(exception, "对话区域诊断失败"); }
                            }
                        }
                        return new LiveFrameAnalysis(frameIdentity, GetFrameFingerprint(ownedFrame), result, dialogue,
                            advanceMatched, exitMatched);
                    }
                }
                catch (Exception exception)
                {
                    _logger.LogDebug(exception, "实时帧 UI 识别失败");
                    return null;
                }
                finally
                {
                    Interlocked.Exchange(ref _visionBusy, 0);
                    completion.TrySetResult(true);
                }
            });
        }
        catch (Exception exception)
        {
            try { copy?.Dispose(); }
            finally
            {
                Interlocked.Exchange(ref _visionBusy, 0);
                completion.TrySetResult(true);
            }
            _logger.LogDebug(exception, "无法启动实时帧 UI 识别");
            return null;
        }
    }

    /// <summary>限制等待时间；迟到结果被丢弃，绝不套用到后续帧。超时预算按帧间隔推导，至少 120 毫秒。</summary>
    private static async Task<LiveFrameAnalysis?> GetFrameVisionAsync(
        Task<LiveFrameAnalysis?>? task, int timeoutMs, CancellationToken cancellationToken)
    {
        if (task is null)
        {
            return null;
        }

        var completed = await Task.WhenAny(task, Task.Delay(timeoutMs, cancellationToken)).ConfigureAwait(false);
        cancellationToken.ThrowIfCancellationRequested();
        return ReferenceEquals(completed, task) ? await task.ConfigureAwait(false) : null;
    }

    /// <summary>构造帧消息。</summary>
    private static LiveServerMessage BuildFrameMessage(LiveFrameSnapshot frame, string? guidance)
    {
        var jpeg = (byte[])frame.JpegBytes.Clone();
        var dialogue = frame.DialogueObservation;
        if (dialogue?.EvidenceSource is { } source
            && !string.Equals(source.JpegSha256, Convert.ToHexString(SHA256.HashData(jpeg)).ToLowerInvariant(), StringComparison.Ordinal))
            dialogue = null;
        return new LiveServerMessage
        {
            Type = "frame",
            Seq = frame.Seq,
            TimestampUtc = frame.TimestampUtc,
            Jpeg = Convert.ToBase64String(jpeg),
            Width = frame.Width,
            Height = frame.Height,
            Capture = frame.Capture,
            Runtime = frame.RuntimeState,
            UiCategory = frame.UiCategory,
            IsInTalkUi = frame.IsInTalkUi,
            DialogueObservation = dialogue,
            Guidance = guidance
        };
    }

    /// <summary>构造动作执行状态消息。</summary>
    private LiveServerMessage BuildActionStateMessage()
    {
        var status = _actionExecutor.GetStatus();
        return new LiveServerMessage
        {
            Type = "action_state",
            Keys = status.PressedKeys,
            QueueDepth = status.QueueDepth,
            Starved = status.Starved,
            Applied = status.AppliedActions,
            Dropped = status.DroppedActions
        };
    }

    /// <summary>通过有界客户端队列广播键块本地处理记录，允许丢包。</summary>
    private void OnExecutionAttempt(LiveExecutionAttemptDto attempt)
    {
        Broadcast(new LiveServerMessage
        {
            Type = "action_attempt",
            AttemptId = attempt.AttemptId,
            Seq = attempt.Seq,
            IntentId = attempt.IntentId,
            MotorGeneration = attempt.MotorGeneration,
            ActionGroupId = attempt.ActionGroupId,
            TimestampUtc = attempt.TimestampUtc,
            ExecutionAttempt = attempt
        });
    }

    /// <summary>广播单次输入证据，不把系统接受等同于游戏响应。</summary>
    private void OnInputEvidence(LiveInputEvidence evidence)
    {
        Broadcast(new LiveServerMessage
        {
            Type = "input_evidence",
            AttemptId = evidence.AttemptId,
            Seq = evidence.Seq,
            IntentId = evidence.IntentId,
            MotorGeneration = evidence.MotorGeneration,
            ActionGroupId = evidence.ActionGroupId,
            TimestampUtc = DateTimeOffset.UtcNow,
            InputEvidence = new LiveInputEvidenceDto
            {
                AttemptId = evidence.AttemptId,
                ChunkIndex = evidence.ChunkIndex,
                Operation = evidence.Input.Kind.ToString(),
                Status = evidence.Receipt.Status.ToString().ToLowerInvariant(),
                Requested = evidence.Receipt.Requested,
                Accepted = evidence.Receipt.Accepted
            }
        });
    }

    /// <summary>广播消息到所有客户端。</summary>
    private void Broadcast(LiveServerMessage message)
    {
        foreach (var client in _clients.Values)
        {
            client.TryEnqueue(message);
        }
    }

    /// <summary>尝试把缩略帧写入会话目录。</summary>
    private async Task TryRecordFrameAsync(LiveSessionState state, LiveFrameSnapshot frame,
        CancellationToken cancellationToken)
    {
        try
        {
            var directory = GetLiveFramesDirectory(state.SessionId);
            Directory.CreateDirectory(directory);
            var path = Path.Combine(directory, $"f{frame.Seq:D8}.jpg");
            await File.WriteAllBytesAsync(path, frame.JpegBytes, cancellationToken).ConfigureAwait(false);

            var files = Directory.EnumerateFiles(directory, "f*.jpg")
                .OrderBy(file => Path.GetFileName(file), StringComparer.Ordinal)
                .ToArray();
            var removeCount = files.Length - MaxRecordedFrames;
            for (var index = 0; index < removeCount; index++)
            {
                try
                {
                    File.Delete(files[index]);
                }
                catch (Exception exception)
                {
                    _logger.LogDebug(exception, "删除旧实时帧失败：{File}", files[index]);
                }
            }
        }
        catch (Exception exception) when (exception is not OperationCanceledException)
        {
            _logger.LogWarning(exception, "写入实时缩略帧失败");
        }
    }

    /// <summary>实时帧目录。</summary>
    private string GetLiveFramesDirectory(string sessionId)
    {
        return Path.Combine(_sessionStore.RootPath, sessionId, "live", "frames");
    }

    /// <summary>追加时间线事件。</summary>
    private async Task<LiveTimelineEventDto> AddTimelineEventAsync(
        LiveSessionState state,
        string type,
        string? kind,
        string? text,
        string? emotionOrSource,
        CancellationToken cancellationToken,
        long? frameSeq = null,
        JsonObject? meta = null)
    {
        var evt = new LiveTimelineEventDto
        {
            Seq = Interlocked.Increment(ref _timelineSeq),
            TimestampUtc = DateTimeOffset.UtcNow,
            Type = type,
            Kind = kind,
            Text = text,
            Emotion = type == "guidance" ? null : emotionOrSource,
            FrameSeq = frameSeq,
            Meta = meta
        };
        _timeline.Enqueue(evt);
        while (_timeline.Count > MaxTimelineEvents && _timeline.TryDequeue(out _))
        {
        }

        await AppendSessionEventBestEffortAsync(state.SessionId, type, evt, cancellationToken)
            .ConfigureAwait(false);
        return evt;
    }

    /// <summary>把实时事件写入智能体会话存储。</summary>
    private async Task AppendSessionEventBestEffortAsync(string sessionId, string type,
        LiveTimelineEventDto evt, CancellationToken cancellationToken)
    {
        try
        {
            // 启动/停止已经是 live.*；指导与解说补上 live. 前缀，避免出现 live.live.*。
            var eventType = type.StartsWith("live.", StringComparison.OrdinalIgnoreCase)
                ? type
                : $"live.{type}";
            await _sessionStore.AppendEventAsync(sessionId, eventType, new
            {
                evt.Seq,
                evt.TimestampUtc,
                evt.Kind,
                evt.Text,
                evt.Emotion,
                evt.FrameSeq
            }, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception exception) when (exception is not OperationCanceledException)
        {
            _logger.LogWarning(exception, "写入实时事件 {Type} 失败", type);
        }
    }

    /// <summary>获取正在运行的会话，否则抛出错误。</summary>
    private LiveSessionState GetRunningState()
    {
        lock (_gate)
        {
            return _state ?? throw new AgentException(
                "LIVE_NOT_RUNNING",
                "实时会话尚未启动。",
                System.Net.HttpStatusCode.Conflict);
        }
    }

    /// <summary>构造状态 DTO；调用方必须持有 <see cref="_gate"/>。</summary>
    private LiveStatusDto BuildStatusLocked()
    {
        var state = _state;
        var executor = _actionExecutor.GetStatus();
        return new LiveStatusDto
        {
            Running = state is not null,
            SessionId = state?.SessionId,
            Fps = state?.Fps ?? 0,
            FrameSeq = state?.FrameSeq ?? 0,
            LastFrameAtUtc = state?.LatestFrame?.TimestampUtc,
            Clients = _clients.Count,
            LatestGuidance = state?.Guidance,
            PressedKeys = executor.PressedKeys,
            ActionQueueDepth = executor.QueueDepth,
            Starved = executor.Starved,
            AppliedActions = executor.AppliedActions,
            DroppedActions = executor.DroppedActions,
            TimelineSeq = Interlocked.Read(ref _timelineSeq),
            Safety = BuildSafetyStatus(state)
        };
    }

    /// <summary>读取单一安全观测；忙或未运行时返回不可授权的默认状态。</summary>
    private LiveSafetyStatusDto BuildSafetyStatus(LiveSessionState? state)
    {
        var status = new LiveSafetyStatusDto
        {
            StrictModeActive = _strictUiSafety,
            FrameRecognitionHealth = state is null ? "not_running" : "unknown"
        };
        if (state is null) return status;

        _actionExecutor.TryReadSafety(state, state.SessionId, (releaseCallsReturned, releaseConfirmed, safe, systemReleaseReceipt) =>
        {
            var authorized = LiveDialogueCandidateTracker.Authorize(_dialogueSafetyPolicy, state.DialogueCalibration);
            if (!authorized) state.DialogueCandidateFrames = 0;
            var frame = state.LatestFrame;
            var ticks = Interlocked.Read(ref state.LastFrameTicks);
            var health = state.UiPending ? "pending"
                : frame is null ? "unavailable"
                : ticks == 0 || Stopwatch.GetElapsedTime(ticks) > TimeSpan.FromMilliseconds(Math.Max(500, 3000d / state.Fps)) ? "stale"
                : !frame.IsDistinct ? "duplicate"
                : (frame.UiCategory == "Talk" && frame.IsInTalkUi == true)
                  || ((frame.UiCategory is "Main" or "BigMap") && frame.IsInTalkUi == false) ? "healthy"
                : "unknown";
            var ready = _strictUiSafety && !_talkLatched && safe;
            status = new LiveSafetyStatusDto
            {
                StrictModeActive = _strictUiSafety,
                DialogueGate = _talkLatched ? "latched" : !_strictUiSafety ? "unarmed" : ready ? "open" : "blocked",
                FrameRecognitionHealth = health,
                FrameSeq = frame?.Seq,
                FrameTimestampUtc = frame?.TimestampUtc,
                DialogueObservation = authorized && health == "healthy" && frame?.DialogueObservation?.ExitCandidateFrames == state.DialogueCandidateFrames
                    ? frame.DialogueObservation : null,
                DialogueExitRequiredFrames = _dialogueExitPolicy?.IsAvailable(state.DialogueCalibration) == true
                    ? _dialogueExitPolicy.RequiredFrames : 0,
                CanConfirmDialogueExit = _dialogueExitPolicy?.IsAvailable(state.DialogueCalibration) == true,
                CanAuthorizeDialogueActions = _playerDialoguePolicy?.AllowsSelect(state.DialogueCalibration) == true
                    && _playerDialoguePolicy.AllowsAdvance(state.DialogueCalibration),
                DialogueExitEvidenceFrames = health == "healthy" ? state.ExitTemplateFrames : 0,
                DialogueLowStructureCandidateFrames = health == "healthy" ? state.DialogueCandidateFrames : 0,
                ReadyForV2 = ready,
                BackgroundInput = state.AllowBackgroundInput,
                InputReleaseCallsReturned = releaseCallsReturned,
                InputReleaseConfirmed = releaseConfirmed,
                SystemReleaseReceipt = systemReleaseReceipt
            };
        });
        return status;
    }

    /// <summary>校验启动参数。</summary>
    private static void ValidateStartRequest(LiveStartRequest request)
    {
        if (request.Fps is < MinFps or > MaxFps)
        {
            throw AgentException.InvalidRequest($"实时帧率必须在 {MinFps}～{MaxFps} 之间。");
        }

        if (request.Quality is < 30 or > 95)
        {
            throw AgentException.InvalidRequest("JPEG 质量必须在 30～95 之间。");
        }

        if (request.MaxWidth is < 320 or > 1920)
        {
            throw AgentException.InvalidRequest("图像宽度必须在 320～1920 之间。");
        }

        if (request.RecordFps is < 1 or > 10)
        {
            throw AgentException.InvalidRequest("录制帧率必须在 1～10 之间。");
        }
    }

    /// <summary>检查智能体控制开关。</summary>
    private void EnsureEnabled()
    {
        if (!_configService.Get().ExternalAccessConfig.AgentControlEnabled)
        {
            throw AgentException.Disabled();
        }
    }

    /// <summary>关闭智能体控制时停止实时会话。</summary>
    private void OnConfigChanged(object? sender, PropertyChangedEventArgs args)
    {
        if (args.PropertyName != nameof(ExternalAccessConfig.AgentControlEnabled)
            || _configService.Get().ExternalAccessConfig.AgentControlEnabled)
        {
            return;
        }

        _actionExecutor.StopActions();
        _ = Task.Run(async () =>
        {
            try
            {
                await StopAsync(CancellationToken.None).ConfigureAwait(false);
            }
            catch (Exception exception)
            {
                _logger.LogDebug(exception, "关闭开关时停止实时会话失败");
            }
        });
    }

    /// <summary>同一张原始帧的 UI 识别与对话区域诊断。</summary>
    private sealed record LiveFrameAnalysis(object FrameIdentity, byte[]? FrameFingerprint,
        AgentVisionDto Vision, LiveDialogueObservationDto? Dialogue, bool AdvanceTemplateMatched,
        bool ExitTemplateMatched);

    /// <summary>实时会话的可变状态。</summary>
    private sealed class LiveSessionState
    {
        /// <summary>会话标识。</summary>
        public required string SessionId { get; init; }

        /// <summary>会话绑定的运行环境。</summary>
        public required GameRuntime Runtime { get; init; }

        /// <summary>广播帧率。</summary>
        public required int Fps { get; init; }

        /// <summary>JPEG 质量。</summary>
        public required int Quality { get; init; }

        /// <summary>最大宽度。</summary>
        public required int MaxWidth { get; init; }

        /// <summary>是否记录缩略帧。</summary>
        public required bool RecordFrames { get; init; }

        /// <summary>每多少帧记录一帧。</summary>
        public required int RecordEvery { get; init; }

        /// <summary>按会话复制的区域诊断校准；不是授权来源。</summary>
        public LiveDialogueCalibrationDto? DialogueCalibration { get; init; }

        /// <summary>本会话是否允许后台（PostMessage）移动按键。</summary>
        public bool AllowBackgroundInput { get; init; }

        /// <summary>取消源。</summary>
        public required CancellationTokenSource Cancellation { get; init; }

        /// <summary>会话持有的同步截图触发器暂停句柄。</summary>
        public IDisposable? TriggerPause;

        /// <summary>升级或启动仍在等待触发器隔离，不能授予输入租约。</summary>
        public bool TriggerPausePending;

        /// <summary>确保截图循环完成回收只发生一次。</summary>
        public int Finished;

        /// <summary>全局停止注册。</summary>
        public IDisposable? StopRegistration { get; set; }

        /// <summary>运行环境停止注册。</summary>
        public CancellationTokenRegistration RuntimeRegistration { get; set; }

        /// <summary>新截图正在采集或识别，不得基于上一帧执行严格模式动作。</summary>
        public volatile bool UiPending;

        /// <summary>只在执行闸门内访问的已处理对话决策，满后拒绝新决策。</summary>
        public HashSet<string> DialogueDecisionIds { get; } = new(StringComparer.Ordinal);

        /// <summary>仅在执行闸门内访问的主界面模板连续独立匹配次数。</summary>
        public int ExitTemplateFrames;

        /// <summary>本会话曾由已配置的正向模板解除对话锁。</summary>
        public bool ExitTemplateUnlocked;

        /// <summary>仅在执行闸门内访问的连续低结构诊断计数。</summary>
        public int DialogueCandidateFrames;

        /// <summary>最近一次截图开始时的单调时间戳。</summary>
        public long LastFrameTicks;

        /// <summary>采集循环。</summary>
        public Task? LoopTask { get; set; }

        /// <summary>最新帧。</summary>
        public volatile LiveFrameSnapshot? LatestFrame;

        /// <summary>最新指导。</summary>
        public volatile string? Guidance;

        /// <summary>帧序号。</summary>
        public long FrameSeq;
    }
}
