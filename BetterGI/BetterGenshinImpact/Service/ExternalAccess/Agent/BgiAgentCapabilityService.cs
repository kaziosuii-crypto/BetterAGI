using BetterGenshinImpact.Core.Config;
using BetterGenshinImpact.GameTask;
using BetterGenshinImpact.GameTask.Common;
using BetterGenshinImpact.GameTask.Runtime;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Input;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Media;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Models;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Runtime;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Safety;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Sessions;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Vision;
using BetterGenshinImpact.Service.Interface;
using Microsoft.Extensions.Logging;
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Linq;
using System.Net;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent;

/// <summary>
/// 智能体游戏能力服务的核心实现，负责协调截图、识别、模拟输入、会话留存和运行环境启停。
/// <para>
/// 动作序列与 BetterGI 的独立任务共用 <see cref="TaskControl.TaskSemaphore"/>，并在服务内部串行化；
/// 截图与识别通过 <see cref="TaskTriggerDispatcher.PauseForInference"/> 短暂让出实时触发器。
/// 本服务不提供游戏内存读写、封包、Shell 或进程操作能力。
/// </para>
/// </summary>
public sealed class BgiAgentCapabilityService : IBgiAgentCapabilityService, IDisposable
{
    /// <summary>
    /// 状态接口返回的能力列表，供调用方发现可用的动作和服务。
    /// </summary>
    private static readonly string[] CapabilityList =
    [
        "status",
        "observe",
        "execute",
        "sessions",
        "memory",
        "checkpoints",
        "frames",
        "screenshot",
        "click",
        "doubleClick",
        "rightClick",
        "moveMouse",
        "moveMouseBy",
        "drag",
        "scroll",
        "key",
        "text",
        "wait",
        "activateWindow",
        "clickAtText",
        "runtime.start",
        "runtime.stop",
        "stop",
        "resume"
    ];

    /// <summary>
    /// 配置服务，用于每次读取智能体控制开关，确保设置修改后无需重启服务对象。
    /// </summary>
    private readonly IConfigService _configService;

    /// <summary>
    /// 游戏运行环境访问器，用于读取当前运行环境和启停截图器。
    /// </summary>
    private readonly IGameRuntimeAccessor _runtimeAccessor;

    /// <summary>
    /// 游戏画面截图源，只读取游戏窗口画面。
    /// </summary>
    private readonly IAgentScreenSource _screenSource;

    /// <summary>
    /// 图像识别服务，用于 UI 状态与 OCR。
    /// </summary>
    private readonly IAgentVisionService _visionService;

    /// <summary>
    /// 模拟输入控制器，只调用 BetterGI 已有的键鼠输入层。
    /// </summary>
    private readonly IAgentInputController _inputController;

    /// <summary>
    /// 会话存储，用于保存事件、截图、内存和检查点。
    /// </summary>
    private readonly IAgentSessionStore _sessionStore;

    /// <summary>
    /// 智能体停止开关，取消当前动作序列并拒绝后续输入，直到显式恢复。
    /// </summary>
    private readonly AgentControlState _controlState;

    /// <summary>
    /// 实时触发器调度器，用于在截图和识别期间暂停触发器自身的识图。
    /// </summary>
    private readonly TaskTriggerDispatcher _triggerDispatcher;

    /// <summary>
    /// 全局停止信号服务，用于让用户的停止热键或停止按钮取消智能体动作序列。
    /// </summary>
    private readonly TaskStopService _taskStopService;

    /// <summary>
    /// 服务日志。
    /// </summary>
    private readonly ILogger<BgiAgentCapabilityService> _logger;

    /// <summary>
    /// 串行化动作序列，避免两个智能体请求同时操作同一游戏画面。
    /// </summary>
    private readonly SemaphoreSlim _sequenceLock = new(1, 1);

    /// <summary>
    /// 是否已被释放。
    /// </summary>
    private bool _disposed;

    /// <summary>
    /// 创建智能体能力服务。
    /// </summary>
    /// <param name="configService">配置服务。</param>
    /// <param name="runtimeAccessor">运行环境访问器。</param>
    /// <param name="screenSource">游戏画面截图源。</param>
    /// <param name="visionService">图像识别服务。</param>
    /// <param name="inputController">模拟输入控制器。</param>
    /// <param name="sessionStore">会话存储。</param>
    /// <param name="controlState">智能体停止开关。</param>
    /// <param name="triggerDispatcher">实时触发器调度器。</param>
    /// <param name="taskStopService">全局停止信号服务。</param>
    /// <param name="logger">服务日志。</param>
    public BgiAgentCapabilityService(
        IConfigService configService,
        IGameRuntimeAccessor runtimeAccessor,
        IAgentScreenSource screenSource,
        IAgentVisionService visionService,
        IAgentInputController inputController,
        IAgentSessionStore sessionStore,
        AgentControlState controlState,
        TaskTriggerDispatcher triggerDispatcher,
        TaskStopService taskStopService,
        ILogger<BgiAgentCapabilityService> logger)
    {
        _configService = configService;
        _runtimeAccessor = runtimeAccessor;
        _screenSource = screenSource;
        _visionService = visionService;
        _inputController = inputController;
        _sessionStore = sessionStore;
        _controlState = controlState;
        _triggerDispatcher = triggerDispatcher;
        _taskStopService = taskStopService;
        _logger = logger;

        // 运行中关闭智能体控制开关时立即停止当前输入，不能等下一个请求才生效。
        _configService.Get().ExternalAccessConfig.PropertyChanged += OnExternalAccessConfigChanged;
    }

    /// <inheritdoc />
    public bool IsEnabled => _configService.Get().ExternalAccessConfig.AgentControlEnabled;

    /// <inheritdoc />
    public AgentStatusDto GetStatus()
    {
        var runtime = _runtimeAccessor.Current;
        return new AgentStatusDto
        {
            Enabled = IsEnabled,
            RuntimeRunning = _runtimeAccessor.IsRunning,
            StopRequested = _controlState.IsStopRequested,
            Capabilities = CapabilityList,
            Limits = new AgentLimitsDto
            {
                MaxActionsPerRequest = AgentLimits.MaxActionsPerRequest,
                MaxWaitMs = AgentLimits.MaxWaitMs,
                MaxSequenceDurationMs = AgentLimits.MaxSequenceDurationMs,
                MaxTextLength = AgentLimits.MaxTextLength,
                MinActionIntervalMs = AgentLimits.MinActionIntervalMs,
                MaxFramesPerSession = AgentLimits.MaxFramesPerSession,
                MaxImageWidth = AgentLimits.MaxImageWidth
            },
            Runtime = BuildRuntimeSnapshot(runtime),
            SessionRoot = _sessionStore.RootPath,
            MemoryAccessAllowed = false,
            ApiVersion = AgentLimits.ApiVersion
        };
    }

    /// <inheritdoc />
    public async Task<AgentObservationResult> ObserveAsync(AgentObserveRequest request,
        CancellationToken cancellationToken)
    {
        EnsureEnabled();
        // 观察与动作序列共用同一把锁，避免多个智能体请求同时截图或 OCR 同一游戏画面。
        await _sequenceLock.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            return await ObserveCoreAsync(request, cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            _sequenceLock.Release();
        }
    }

    /// <summary>
    /// 在调用方已持有 <see cref="_sequenceLock"/> 时执行观察。
    /// 动作序列内部调用该核心方法，避免非重入信号量死锁。
    /// </summary>
    private async Task<AgentObservationResult> ObserveCoreAsync(AgentObserveRequest request,
        CancellationToken cancellationToken)
    {
        EnsureEnabled();
        ArgumentNullException.ThrowIfNull(request);
        ValidateImageOptions(request.MaxWidth, request.Format, request.Quality);
        ValidateObserveName(request.Name);
        // 命名区域与旧单区域模式互斥；在截图前拒绝重复、重叠及不完整参数。
        var namedRegions = request.OcrRegions is null ? null : AgentNamedOcrRegions.Prepare(request);

        var session = await _sessionStore.EnsureSessionAsync(request.SessionId, null, cancellationToken)
            .ConfigureAwait(false);
        _logger.LogInformation("开始观察智能体会话 {SessionId}", session.Id);

        // 截图和视觉识别各自短暂暂停实时触发器；编码、保存和写事件不占用暂停租约。
        using var frame = await CaptureFrameAsync(cancellationToken).ConfigureAwait(false);
        var capturedAt = DateTimeOffset.UtcNow;
        if (namedRegions is not null)
            AgentNamedOcrRegions.ValidateBounds(namedRegions, frame.OriginalWidth, frame.OriginalHeight);

        var encoded = await Task.Run(
                () => AgentImageEncoder.Encode(frame.Image, request.MaxWidth, request.Format, request.Quality),
                cancellationToken)
            .ConfigureAwait(false);

        // 命名模式只运行各自的 ROI OCR，即使 IncludeOcr 为 true 也不能回退到整帧。
        var legacyOcr = request.IncludeOcr && namedRegions is null;
        AgentVisionDto? vision = null;
        if (request.IncludeUiState || legacyOcr)
        {
            vision = await AnalyzeFrameAsync(
                    frame, request.IncludeUiState, legacyOcr, request.OcrRoi, cancellationToken)
                .ConfigureAwait(false);
        }

        string frameId;
        string? url = null;
        if (request.SaveFrame)
        {
            var saved = await _sessionStore.SaveFrameAsync(
                    session.Id, encoded.Bytes, encoded.Extension, request.Name, cancellationToken)
                .ConfigureAwait(false);
            frameId = saved.FrameId;
            url = $"/api/v1/agent/frames/{session.Id}/{saved.FrameId}";
        }
        else
        {
            // 未保存时仍返回唯一帧标识，便于事件串联；此时不存在可下载的截图文件。
            frameId = $"unsaved-{Guid.NewGuid():N}";
        }

        IReadOnlyList<AgentNamedOcrRegionResult>? namedResults = null;
        if (namedRegions is not null)
        {
            // 使用原始 AgentFrame，而非缩放后的编码图像；全部区域共享帧标识。
            using (_triggerDispatcher.PauseForInference())
            {
                namedResults = await Task.Run(() => AgentNamedOcrRegions.Analyze(
                    namedRegions, frame.OriginalWidth, frame.OriginalHeight, frameId,
                    roi => _visionService.ObserveOcrRegion(frame, roi), cancellationToken), cancellationToken)
                    .ConfigureAwait(false);
            }
        }

        var observationEvent = await AppendObservationEventAsync(
                session.Id, frameId, frame.Runtime, frame.CaptureArea, vision, request.Name, cancellationToken)
            .ConfigureAwait(false);

        var observation = new AgentObservationDto
        {
            SessionId = session.Id,
            FrameId = frameId,
            CapturedAtUtc = capturedAt,
            EventSeq = observationEvent.Seq,
            Runtime = frame.Runtime,
            CaptureArea = frame.CaptureArea.ToDto(),
            Image = new AgentImageInfoDto
            {
                MimeType = encoded.MimeType,
                Width = encoded.Width,
                Height = encoded.Height,
                OriginalWidth = frame.OriginalWidth,
                OriginalHeight = frame.OriginalHeight,
                Url = url,
                Name = request.Name
            },
            Vision = vision,
            OcrRegions = namedResults,
            ImageBase64 = request.IncludeImageBase64 ? Convert.ToBase64String(encoded.Bytes) : null
        };

        _logger.LogInformation(
            "智能体观察完成：会话 {SessionId}，帧 {FrameId}，图像 {Width}x{Height}",
            session.Id, frameId, encoded.Width, encoded.Height);

        return new AgentObservationResult
        {
            Observation = observation,
            ImageBytes = encoded.Bytes,
            MimeType = encoded.MimeType
        };
    }

    /// <inheritdoc />
    public async Task<AgentActionResultDto> ExecuteAsync(AgentActionRequest request,
        CancellationToken cancellationToken)
    {
        EnsureEnabled();
        ArgumentNullException.ThrowIfNull(request);
        AgentSafetyPolicy.ValidateSequence(request);
        ValidateImageOptions(request.MaxWidth, request.Format, request.Quality);
        if (_controlState.IsStopRequested)
        {
            throw AgentException.Stopped();
        }

        var session = await _sessionStore.EnsureSessionAsync(request.SessionId, null, cancellationToken)
            .ConfigureAwait(false);
        var defaultChannel = AgentSafetyPolicy.ResolveChannel(request.Channel, null);
        _logger.LogInformation(
            "开始执行智能体动作序列：会话 {SessionId}，动作数 {Count}",
            session.Id, request.Actions.Count);

        // 先串行化智能体请求，再以非阻塞方式占用 BetterGI 的独立任务锁；
        // 若此时已有用户任务运行，立即拒绝，避免智能体与任务同时操作游戏。
        await _sequenceLock.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var runtime = EnsureRuntimeRunning();
            if (!await TaskControl.TaskSemaphore.WaitAsync(0, cancellationToken).ConfigureAwait(false))
            {
                throw AgentException.TaskRunning();
            }

            try
            {
                // 在产生任何输入前，用当前实时画面区域预检全部绝对坐标动作，
                // 避免序列执行到一半才发现越界而产生部分操作。
                PreValidateCoordinates(request.Actions, runtime);
                using var linkedCancellation = _controlState.CreateLinkedTokenSource(cancellationToken);
                // 让 BetterGI 的全局停止热键/停止按钮和运行环境解绑也能立即取消智能体序列。
                using var stopRegistration = _taskStopService.Register(_ => linkedCancellation.Cancel());
                using var runtimeRegistration = runtime.Stopping.Register(linkedCancellation.Cancel);
                // 限制整段序列的总时长，避免大量 wait 动作长期占用 BetterGI 的独立任务锁。
                linkedCancellation.CancelAfter(AgentLimits.MaxSequenceDurationMs);
                return await ExecuteSequenceAsync(request, session, defaultChannel, linkedCancellation.Token)
                    .ConfigureAwait(false);
            }
            finally
            {
                // 清理期间仍持有独立任务锁，避免新会话与前一序列的释放交错。
                try
                {
                    _inputController.ReleasePressedInputs();
                }
                catch
                {
                    // 无法确认清理结果时禁止后续智能体输入，直到显式恢复。
                    _controlState.RequestStop();
                    throw;
                }
                finally
                {
                    TaskControl.TaskSemaphore.Release();
                }
            }
        }
        finally
        {
            _sequenceLock.Release();
        }
    }

    /// <inheritdoc />
    public Task<AgentSessionSummaryDto> CreateSessionAsync(string? sessionId, string? name,
        CancellationToken cancellationToken)
    {
        return WithSessionStoreAsync(() => _sessionStore.EnsureSessionAsync(sessionId, name, cancellationToken));
    }

    /// <inheritdoc />
    public Task<IReadOnlyList<AgentSessionSummaryDto>> ListSessionsAsync(CancellationToken cancellationToken)
    {
        return WithSessionStoreAsync(() => _sessionStore.ListSessionsAsync(cancellationToken));
    }

    /// <inheritdoc />
    public Task<AgentSessionSummaryDto> GetSessionAsync(string? sessionId, CancellationToken cancellationToken)
    {
        return WithSessionStoreAsync(() => _sessionStore.GetSessionAsync(sessionId, cancellationToken));
    }

    /// <inheritdoc />
    public async Task<AgentResumeDto> GetResumeAsync(string? sessionId, int recentEventLimit,
        CancellationToken cancellationToken)
    {
        var safeLimit = Math.Clamp(recentEventLimit, 1, 500);
        return await WithSessionStoreAsync(
                () => _sessionStore.GetResumeAsync(sessionId, safeLimit, cancellationToken))
            .ConfigureAwait(false);
    }

    /// <inheritdoc />
    public async Task<IReadOnlyList<AgentEventDto>> GetEventsAsync(string? sessionId, long afterSeq, int limit,
        CancellationToken cancellationToken)
    {
        var safeAfterSeq = Math.Max(0, afterSeq);
        var safeLimit = Math.Clamp(limit, 1, 500);
        return await WithSessionStoreAsync(
                () => _sessionStore.GetEventsAsync(sessionId, safeAfterSeq, safeLimit, cancellationToken))
            .ConfigureAwait(false);
    }

    /// <inheritdoc />
    public Task<AgentMemoryDto> GetMemoryAsync(string? sessionId, CancellationToken cancellationToken)
    {
        return WithSessionStoreAsync(() => _sessionStore.GetMemoryAsync(sessionId, cancellationToken));
    }

    /// <inheritdoc />
    public async Task<AgentMemoryDto> MergeMemoryAsync(string? sessionId, JsonObject values,
        CancellationToken cancellationToken)
    {
        if (values is null)
        {
            throw AgentException.InvalidRequest("内存内容不能为空。");
        }

        return await WithSessionStoreAsync(
                () => _sessionStore.MergeMemoryAsync(sessionId, values, cancellationToken))
            .ConfigureAwait(false);
    }

    /// <inheritdoc />
    public async Task<AgentMemoryDto> DeleteMemoryAsync(string? sessionId, string key,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(key))
        {
            throw AgentException.InvalidRequest("内存键不能为空。");
        }

        return await WithSessionStoreAsync(
                () => _sessionStore.DeleteMemoryAsync(sessionId, key, cancellationToken))
            .ConfigureAwait(false);
    }

    /// <inheritdoc />
    public async Task<AgentCheckpointDto> CreateCheckpointAsync(string? sessionId, string name, string? note,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(name))
        {
            throw AgentException.InvalidRequest("检查点名称不能为空。");
        }

        return await WithSessionStoreAsync(
                () => _sessionStore.CreateCheckpointAsync(sessionId, name, note, cancellationToken))
            .ConfigureAwait(false);
    }

    /// <inheritdoc />
    public Task<IReadOnlyList<AgentCheckpointDto>> ListCheckpointsAsync(string? sessionId,
        CancellationToken cancellationToken)
    {
        return WithSessionStoreAsync(() => _sessionStore.ListCheckpointsAsync(sessionId, cancellationToken));
    }

    /// <inheritdoc />
    public AgentStoredFrame? GetStoredFrame(string? sessionId, string frameId)
    {
        EnsureEnabled();
        return _sessionStore.GetStoredFrame(sessionId, frameId);
    }

    /// <inheritdoc />
    public async Task<bool> StartRuntimeAsync(CancellationToken cancellationToken)
    {
        using var operation = _controlState.EnterStartOperation();
        EnsureEnabled();
        GameRuntime? runtime;
        string? failure = null;
        try
        {
            runtime = await _runtimeAccessor.StartAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception exception)
        {
            // 启动失败（分辨率不合规、截图器启动异常等）按 running=false 返回，避免调用方只看到 500。
            runtime = null;
            failure = exception.Message;
            _logger.LogWarning(exception, "智能体请求启动游戏运行环境失败");
        }

        var running = runtime is not null;
        _logger.LogInformation("智能体请求启动游戏运行环境，结果：{Running}", running);
        await AppendEventBestEffortAsync(
                AgentLimits.DefaultSessionId,
                "runtime.started",
                new
                {
                    running,
                    kind = runtime?.Kind.ToString(),
                    error = failure
                },
                CancellationToken.None)
            .ConfigureAwait(false);
        return running;
    }

    /// <inheritdoc />
    public async Task StopRuntimeAsync(CancellationToken cancellationToken)
    {
        EnsureEnabled();
        // 与动作序列一样先占用独立任务锁：已有用户任务运行时拒绝，避免外部智能体停掉用户任务。
        if (!await TaskControl.TaskSemaphore.WaitAsync(0, cancellationToken).ConfigureAwait(false))
        {
            throw AgentException.TaskRunning();
        }

        try
        {
            // 先释放适配器按下的键鼠，再解绑运行环境，避免输入停留在按下状态。
            _inputController.ReleasePressedInputs();
            await _runtimeAccessor.StopAsync(cancellationToken).ConfigureAwait(false);
            _logger.LogInformation("智能体请求停止游戏运行环境。");
            await AppendEventBestEffortAsync(
                    AgentLimits.DefaultSessionId,
                    "runtime.stopped",
                    new
                    {
                        running = _runtimeAccessor.IsRunning
                    },
                    CancellationToken.None)
                .ConfigureAwait(false);
        }
        finally
        {
            TaskControl.TaskSemaphore.Release();
        }
    }

    /// <inheritdoc />
    public void Stop()
    {
        _controlState.RequestStop();
        _inputController.ReleasePressedInputs();
        _logger.LogInformation("智能体操作已停止，等待调用恢复。");

        // 停止接口不能等待磁盘写入；事件写入失败只记录警告，不影响停止语义。
        _ = AppendEventBestEffortAsync(
            AgentLimits.DefaultSessionId,
            "agent.stopped",
            new
            {
                stopRequested = true
            },
            CancellationToken.None);
    }

    /// <inheritdoc />
    public void Resume()
    {
        _controlState.Resume();
        _logger.LogInformation("智能体操作已恢复。");
    }

    /// <summary>
    /// 释放服务持有的信号量。停止开关由依赖注入容器作为单例统一释放。
    /// </summary>
    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        _configService.Get().ExternalAccessConfig.PropertyChanged -= OnExternalAccessConfigChanged;
        _sequenceLock.Dispose();
    }

    /// <summary>
    /// 配置开关变化时，关闭智能体控制会立即取消当前序列并释放按键。
    /// </summary>
    private void OnExternalAccessConfigChanged(object? sender, PropertyChangedEventArgs args)
    {
        if (args.PropertyName != nameof(ExternalAccessConfig.AgentControlEnabled))
        {
            return;
        }

        if (_configService.Get().ExternalAccessConfig.AgentControlEnabled)
        {
            return;
        }

        _logger.LogWarning("智能体控制开关已关闭，正在停止当前操作并释放输入。");
        Stop();
    }

    /// <summary>
    /// 按顺序执行请求中的动作，并在需要时追加一次结束观察。
    /// </summary>
    /// <param name="request">动作序列请求。</param>
    /// <param name="session">已确保存在的会话。</param>
    /// <param name="defaultChannel">请求级默认输入通道。</param>
    /// <param name="cancellationToken">与停止开关联动的取消令牌。</param>
    /// <returns>动作序列结果。</returns>
    private async Task<AgentActionResultDto> ExecuteSequenceAsync(
        AgentActionRequest request,
        AgentSessionSummaryDto session,
        string defaultChannel,
        CancellationToken cancellationToken)
    {
        var steps = new List<AgentStepResultDto>(request.Actions.Count);
        AgentObservationDto? observation = null;
        string? error = null;
        var cancelled = false;

        for (var index = 0; index < request.Actions.Count; index++)
        {
            if (cancellationToken.IsCancellationRequested)
            {
                cancelled = true;
                error = BuildCancellationMessage();
                break;
            }

            var action = request.Actions[index];
            var startedAt = DateTimeOffset.UtcNow;
            var stopwatch = Stopwatch.StartNew();
            AgentStepResultDto step;
            try
            {
                step = await ExecuteSingleActionAsync(
                        index, action, request, session.Id, defaultChannel, cancellationToken)
                    .ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                // 用户停止或请求取消：记录当前动作后结束序列，由外层统一释放输入。
                stopwatch.Stop();
                cancelled = true;
                error = BuildCancellationMessage();
                step = new AgentStepResultDto
                {
                    Index = index,
                    Type = action.Type,
                    Ok = false,
                    Message = error,
                    Code = _controlState.IsStopRequested ? "AGENT_STOPPED" : "CANCELLED",
                    StartedAtUtc = startedAt,
                    DurationMs = stopwatch.ElapsedMilliseconds
                };
                steps.Add(step);
                await AppendActionEventAsync(session.Id, step).ConfigureAwait(false);
                break;
            }

            steps.Add(step);
            await AppendActionEventAsync(session.Id, step).ConfigureAwait(false);

            if (!step.Ok && request.StopOnError)
            {
                error = step.Message ?? $"第 {index} 个动作执行失败。";
                _logger.LogWarning(
                    "智能体动作序列在失败处停止：会话 {SessionId}，动作序号 {Index}",
                    session.Id, index);
                break;
            }
        }

        // 取消可能恰好发生在最后一个动作完成之后，这里补一次检查，避免把已停止的序列报告为成功。
        if (!cancelled && cancellationToken.IsCancellationRequested)
        {
            cancelled = true;
            error = BuildCancellationMessage();
        }

        if (cancelled)
        {
            await AppendSequenceCancelledEventAsync(session.Id, steps.Count, error).ConfigureAwait(false);
        }
        else if (request.CaptureAfter && error is null && !cancellationToken.IsCancellationRequested)
        {
            try
            {
                var observeResult = await ObserveCoreAsync(
                        new AgentObserveRequest
                        {
                            SessionId = session.Id,
                            IncludeUiState = request.IncludeUiState,
                            IncludeOcr = request.IncludeOcr,
                            IncludeImageBase64 = request.IncludeImageBase64,
                            MaxWidth = request.MaxWidth,
                            Format = request.Format,
                            Quality = request.Quality,
                            SaveFrame = true
                        },
                        cancellationToken)
                    .ConfigureAwait(false);
                observation = observeResult.Observation;
            }
            catch (OperationCanceledException)
            {
                cancelled = true;
                error = BuildCancellationMessage();
            }
            catch (Exception exception)
            {
                error = exception.Message;
                _logger.LogWarning(exception, "智能体动作序列结束后的观察失败：会话 {SessionId}", session.Id);
            }

            if (cancelled)
            {
                await AppendSequenceCancelledEventAsync(session.Id, steps.Count, error).ConfigureAwait(false);
            }
        }

        return new AgentActionResultDto
        {
            Ok = error is null && steps.All(step => step.Ok),
            SessionId = session.Id,
            StopRequested = _controlState.IsStopRequested,
            Steps = steps,
            Observation = observation,
            Error = error
        };
    }

    /// <summary>
    /// 执行单个动作。动作内部异常会转换为失败的步骤结果；取消则向上抛出由序列统一处理。
    /// </summary>
    /// <param name="index">动作序号。</param>
    /// <param name="action">动作。</param>
    /// <param name="request">所属请求，提供图像与观察选项。</param>
    /// <param name="sessionId">会话标识。</param>
    /// <param name="defaultChannel">请求级默认输入通道。</param>
    /// <param name="cancellationToken">取消令牌。</param>
    /// <returns>动作执行结果。</returns>
    private async Task<AgentStepResultDto> ExecuteSingleActionAsync(
        int index,
        AgentActionDto action,
        AgentActionRequest request,
        string sessionId,
        string defaultChannel,
        CancellationToken cancellationToken)
    {
        var startedAt = DateTimeOffset.UtcNow;
        var stopwatch = Stopwatch.StartNew();
        string? frameId = null;
        AgentPointDto? point = null;
        AgentObservationDto? observation = null;
        string? message = null;
        var type = action.Type.Trim().ToLowerInvariant();

        _logger.LogInformation(
            "执行智能体动作 {Index}/{Total}：{Type}",
            index + 1, request.Actions.Count, action.Type);

        try
        {
            switch (type)
            {
                case "wait":
                    // 等待动作不占用截图暂停租约，也不产生输入。
                    await Task.Delay(action.WaitMs, cancellationToken).ConfigureAwait(false);
                    break;
                case "screenshot":
                {
                    // 截图动作只保存画面，不做视觉识别。
                    using var frame = await CaptureFrameAsync(cancellationToken).ConfigureAwait(false);
                    var encoded = await Task.Run(
                            () => AgentImageEncoder.Encode(
                                frame.Image, request.MaxWidth, request.Format, request.Quality),
                            cancellationToken)
                        .ConfigureAwait(false);
                    var saved = await _sessionStore.SaveFrameAsync(
                            sessionId, encoded.Bytes, encoded.Extension, action.Name, cancellationToken)
                        .ConfigureAwait(false);
                    frameId = saved.FrameId;
                    await AppendObservationEventAsync(
                            sessionId, saved.FrameId, frame.Runtime, frame.CaptureArea, null, action.Name,
                            cancellationToken)
                        .ConfigureAwait(false);
                    message = "截图已保存。";
                    break;
                }
                case "observe":
                {
                    // observe 动作复用完整观察流程，使截图、识别和事件语义保持一致。
                    var observeResult = await ObserveCoreAsync(
                            new AgentObserveRequest
                            {
                                SessionId = sessionId,
                                IncludeUiState = request.IncludeUiState,
                                IncludeOcr = request.IncludeOcr,
                                OcrRoi = null,
                                IncludeImageBase64 = request.IncludeImageBase64,
                                MaxWidth = request.MaxWidth,
                                Format = request.Format,
                                Quality = request.Quality,
                                SaveFrame = true,
                                Name = action.Name
                            },
                            cancellationToken)
                        .ConfigureAwait(false);
                    observation = observeResult.Observation;
                    frameId = observation.FrameId;
                    message = observation.Vision is null
                        ? "已观察画面。"
                        : $"已观察画面，UI 类别：{observation.Vision.UiCategory}。";
                    break;
                }
                case "clickattext":
                {
                    var resolved = await ClickAtTextAsync(action, defaultChannel, cancellationToken)
                        .ConfigureAwait(false);
                    point = resolved.Point.ToDto();
                    message = $"已点击文本：{resolved.MatchedText}";
                    break;
                }
                default:
                {
                    // 其余动作全部经白名单校验，属于模拟输入动作。
                    var area = GetLiveCaptureArea();
                    AgentResolvedPoint? resolvedPoint = null;
                    if (RequiresPoint(type))
                    {
                        resolvedPoint = AgentCoordinateMapper.Resolve(action, area);
                    }

                    await _inputController.ExecuteAsync(action, area, defaultChannel, cancellationToken)
                        .ConfigureAwait(false);
                    point = resolvedPoint?.ToDto();

                    // BetterGI 约定键鼠操作之间保留间隔，避免 30 fps 游戏丢失输入。
                    await Task.Delay(AgentLimits.MinActionIntervalMs, cancellationToken).ConfigureAwait(false);
                    break;
                }
            }

            stopwatch.Stop();
            _logger.LogInformation(
                "智能体动作执行成功：{Index}/{Total} {Type}，耗时 {Duration} ms",
                index + 1, request.Actions.Count, action.Type, stopwatch.ElapsedMilliseconds);
            return new AgentStepResultDto
            {
                Index = index,
                Type = action.Type,
                Ok = true,
                Message = message,
                StartedAtUtc = startedAt,
                DurationMs = stopwatch.ElapsedMilliseconds,
                FrameId = frameId,
                Point = point,
                Observation = observation
            };
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (AgentException exception)
        {
            stopwatch.Stop();
            _logger.LogWarning(
                exception,
                "智能体动作执行失败：{Index}/{Total} {Type}，错误码 {Code}",
                index + 1, request.Actions.Count, action.Type, exception.Code);
            return new AgentStepResultDto
            {
                Index = index,
                Type = action.Type,
                Ok = false,
                Message = exception.Message,
                Code = exception.Code,
                Details = exception.Details,
                StartedAtUtc = startedAt,
                DurationMs = stopwatch.ElapsedMilliseconds,
                FrameId = frameId,
                Point = point,
                Observation = observation
            };
        }
        catch (Exception exception)
        {
            stopwatch.Stop();
            _logger.LogWarning(
                exception,
                "智能体动作执行失败：{Index}/{Total} {Type}",
                index + 1, request.Actions.Count, action.Type);
            return new AgentStepResultDto
            {
                Index = index,
                Type = action.Type,
                Ok = false,
                Message = exception.Message,
                Code = "INTERNAL_ERROR",
                StartedAtUtc = startedAt,
                DurationMs = stopwatch.ElapsedMilliseconds,
                FrameId = frameId,
                Point = point,
                Observation = observation
            };
        }
    }

    /// <summary>
    /// 截取一帧，按 OCR 文本定位并点击。坐标使用同一帧的捕获区域换算，避免窗口移动导致偏移。
    /// </summary>
    /// <param name="action">clickAtText 动作。</param>
    /// <param name="defaultChannel">请求级默认输入通道。</param>
    /// <param name="cancellationToken">取消令牌。</param>
    /// <returns>实际点击坐标与命中的文本。</returns>
    private async Task<(AgentResolvedPoint Point, string MatchedText)> ClickAtTextAsync(
        AgentActionDto action,
        string defaultChannel,
        CancellationToken cancellationToken)
    {
        var searchText = action.Text;
        if (string.IsNullOrWhiteSpace(searchText))
        {
            throw AgentException.InvalidRequest("clickAtText 需要 Text。");
        }

        using var frame = await CaptureFrameAsync(cancellationToken).ConfigureAwait(false);
        var vision = await AnalyzeFrameAsync(frame, false, true, null, cancellationToken).ConfigureAwait(false);
        var target = vision.Ocr?.FirstOrDefault(region =>
            region.Text is not null &&
            (action.Exact
                ? string.Equals(region.Text, searchText, StringComparison.Ordinal)
                : region.Text.Contains(searchText, StringComparison.Ordinal)));

        if (target is null)
        {
            throw new AgentException(
                "TEXT_NOT_FOUND",
                $"未在画面中找到文本“{searchText}”。",
                HttpStatusCode.NotFound,
                $"已识别 {vision.Ocr?.Count ?? 0} 行文本。");
        }

        // 识别结果已经给出相对识别帧的归一化中心，用同一帧的捕获区域换算出三种坐标表示。
        var point = AgentCoordinateMapper.Resolve(
            target.NormalizedX, target.NormalizedY, "normalized", frame.CaptureArea);
        var clickAction = new AgentActionDto
        {
            Type = "click",
            Channel = action.Channel,
            CoordinateSpace = "normalized",
            X = point.NormalizedX,
            Y = point.NormalizedY,
            Button = action.Button,
            Clicks = 1
        };

        await _inputController.ExecuteAsync(clickAction, frame.CaptureArea, defaultChannel, cancellationToken)
            .ConfigureAwait(false);
        await Task.Delay(AgentLimits.MinActionIntervalMs, cancellationToken).ConfigureAwait(false);
        return (point, target.Text);
    }

    /// <summary>
    /// 判断动作是否必须携带目标坐标并记录解析后的坐标。
    /// </summary>
    /// <param name="type">已规范化为小写的动作类型。</param>
    /// <returns>是否需要目标坐标。</returns>
    private static bool RequiresPoint(string type)
    {
        return type is "click" or "doubleclick" or "rightclick" or "movemouse" or "drag";
    }

    /// <summary>
    /// 在暂停实时触发器识别的情况下截取一帧，并在线程池执行，避免阻塞调用线程。
    /// </summary>
    /// <param name="cancellationToken">取消令牌。</param>
    /// <returns>调用方负责释放的帧。</returns>
    private async Task<AgentFrame> CaptureFrameAsync(CancellationToken cancellationToken)
    {
        using (_triggerDispatcher.PauseForInference())
        {
            return await Task.Run(() => _screenSource.Capture(), cancellationToken).ConfigureAwait(false);
        }
    }

    /// <summary>
    /// 在暂停实时触发器识别的情况下分析一帧画面。
    /// </summary>
    /// <param name="frame">要分析的帧。</param>
    /// <param name="includeUiState">是否计算 UI 布尔状态。</param>
    /// <param name="includeOcr">是否执行 OCR。</param>
    /// <param name="ocrRoi">OCR 关注区域。</param>
    /// <param name="cancellationToken">取消令牌。</param>
    /// <returns>视觉分析结果。</returns>
    private async Task<AgentVisionDto> AnalyzeFrameAsync(
        AgentFrame frame,
        bool includeUiState,
        bool includeOcr,
        AgentRoiDto? ocrRoi,
        CancellationToken cancellationToken)
    {
        using (_triggerDispatcher.PauseForInference())
        {
            return await Task.Run(
                    () => _visionService.Analyze(frame, includeUiState, includeOcr, ocrRoi),
                    cancellationToken)
                .ConfigureAwait(false);
        }
    }

    /// <summary>
    /// 读取当前运行环境，并在不可用时抛出统一异常。
    /// </summary>
    /// <returns>当前运行环境。</returns>
    private GameRuntime EnsureRuntimeRunning()
    {
        var runtime = _runtimeAccessor.Current ?? throw AgentException.RuntimeUnavailable();
        if (runtime.IsStopping || !runtime.Window.IsAlive)
        {
            throw AgentException.RuntimeUnavailable("游戏运行环境正在停止或游戏已经退出。");
        }

        return runtime;
    }

    /// <summary>
    /// 在动作序列开始前预检所有绝对坐标动作，确保不会执行一半后才发现坐标越界。
    /// </summary>
    /// <param name="actions">动作列表。</param>
    /// <param name="runtime">当前游戏运行环境。</param>
    private static void PreValidateCoordinates(IReadOnlyList<AgentActionDto> actions, GameRuntime runtime)
    {
        var viewport = runtime.Window.Viewport;
        var area = new AgentCaptureArea(
            viewport.ScreenRect.X,
            viewport.ScreenRect.Y,
            viewport.ScreenRect.Width,
            viewport.ScreenRect.Height,
            viewport.DpiScale);

        foreach (var action in actions)
        {
            var type = action.Type.Trim().ToLowerInvariant();
            if (type is not ("click" or "doubleclick" or "rightclick" or "movemouse" or "drag"))
            {
                continue;
            }

            AgentCoordinateMapper.Resolve(action, area);
            if (type == "drag")
            {
                if (action.ToX is not double toX || action.ToY is not double toY)
                {
                    throw AgentException.InvalidRequest("drag 需要 ToX 和 ToY。");
                }

                AgentCoordinateMapper.Resolve(toX, toY, action.CoordinateSpace, area);
            }
        }
    }

    /// <summary>
    /// 读取当前游戏画面区域，供坐标换算和输入控制器使用。
    /// </summary>
    /// <returns>当前游戏画面区域。</returns>
    private AgentCaptureArea GetLiveCaptureArea()
    {
        var runtime = EnsureRuntimeRunning();
        var viewport = runtime.Window.Viewport;
        return new AgentCaptureArea(
            viewport.ScreenRect.X,
            viewport.ScreenRect.Y,
            viewport.ScreenRect.Width,
            viewport.ScreenRect.Height,
            viewport.DpiScale);
    }

    /// <summary>
    /// 能力未启用时拒绝调用。
    /// </summary>
    private void EnsureEnabled()
    {
        if (!IsEnabled)
        {
            throw AgentException.Disabled();
        }
    }

    /// <summary>
    /// 校验图像编码参数，避免序列开始后才发现参数错误。
    /// </summary>
    /// <param name="maxWidth">最大宽度；0 表示保持原尺寸。</param>
    /// <param name="format">图像格式。</param>
    /// <param name="quality">JPEG 质量。</param>
    private static void ValidateImageOptions(int maxWidth, string? format, int quality)
    {
        if (maxWidth is < 0 or > AgentLimits.MaxImageWidth)
        {
            throw AgentException.InvalidRequest(
                $"MaxWidth 必须在 0～{AgentLimits.MaxImageWidth} 之间。",
                $"实际为 {maxWidth}。");
        }

        var normalizedFormat = string.IsNullOrWhiteSpace(format) ? "png" : format.Trim().ToLowerInvariant();
        if (normalizedFormat is not ("png" or "jpg" or "jpeg"))
        {
            throw AgentException.InvalidRequest("图像格式只能是 png 或 jpg。", format);
        }

        if (quality is < 1 or > 100)
        {
            throw AgentException.InvalidRequest("JPEG 质量必须在 1～100 之间。", $"实际为 {quality}。");
        }
    }

    /// <summary>
    /// 校验观察名称长度，避免调用方用超长字符串放大事件日志。
    /// </summary>
    /// <param name="name">观察名称。</param>
    private static void ValidateObserveName(string? name)
    {
        if (name is { Length: > 64 })
        {
            throw AgentException.InvalidRequest("观察名称不能超过 64 个字符。");
        }
    }

    /// <summary>
    /// 构造写入会话事件的运行时快照，属性名使用 camelCase。
    /// </summary>
    /// <param name="runtime">运行时快照。</param>
    /// <returns>事件数据对象。</returns>
    private static object BuildRuntimeEventData(AgentRuntimeSnapshotDto runtime)
    {
        return new
        {
            kind = runtime.Kind,
            processId = runtime.ProcessId,
            windowHandle = runtime.WindowHandle,
            isAlive = runtime.IsAlive,
            isForeground = runtime.IsForeground,
            isMinimized = runtime.IsMinimized,
            requiresForeground = runtime.RequiresForeground
        };
    }

    /// <summary>
    /// 构造写入会话事件的捕获区域，属性名使用 camelCase。
    /// </summary>
    /// <param name="captureArea">捕获区域。</param>
    /// <returns>事件数据对象。</returns>
    private static object BuildCaptureAreaEventData(AgentCaptureArea captureArea)
    {
        return new
        {
            x = captureArea.X,
            y = captureArea.Y,
            width = captureArea.Width,
            height = captureArea.Height,
            dpiScale = captureArea.DpiScale
        };
    }

    /// <summary>
    /// 构造视觉事件摘要，避免把完整 OCR 文本写入事件日志。
    /// </summary>
    /// <param name="vision">视觉结果；未执行识别时为空。</param>
    /// <returns>事件摘要；未执行识别时为 null。</returns>
    private static object? BuildVisionEventSummary(AgentVisionDto? vision)
    {
        return vision is null
            ? null
            : new
            {
                uiCategory = vision.UiCategory,
                isInMainUi = vision.IsInMainUi,
                isInDomain = vision.IsInDomain,
                isInAnyClosableUi = vision.IsInAnyClosableUi,
                isInGameReadyUi = vision.IsInGameReadyUi,
                isInPartyViewUi = vision.IsInPartyViewUi,
                isInBigMapUi = vision.IsInBigMapUi,
                isInTalkUi = vision.IsInTalkUi,
                ocrCount = vision.Ocr?.Count ?? 0,
                elapsedMs = vision.ElapsedMs
            };
    }

    /// <summary>
    /// 追加一条 observation 事件并返回带序号的完整事件。
    /// </summary>
    /// <param name="sessionId">会话标识。</param>
    /// <param name="frameId">帧标识。</param>
    /// <param name="runtime">截取该帧时的运行时快照。</param>
    /// <param name="captureArea">该帧的捕获区域。</param>
    /// <param name="vision">视觉结果；截图动作产生的 observation 为空。</param>
    /// <param name="name">帧名称。</param>
    /// <param name="cancellationToken">取消令牌。</param>
    /// <returns>已追加的事件。</returns>
    private async Task<AgentEventDto> AppendObservationEventAsync(
        string sessionId,
        string frameId,
        AgentRuntimeSnapshotDto runtime,
        AgentCaptureArea captureArea,
        AgentVisionDto? vision,
        string? name,
        CancellationToken cancellationToken)
    {
        return await _sessionStore.AppendEventAsync(
                sessionId,
                "observation",
                new
                {
                    frameId,
                    runtime = BuildRuntimeEventData(runtime),
                    captureArea = BuildCaptureAreaEventData(captureArea),
                    vision = BuildVisionEventSummary(vision),
                    name
                },
                cancellationToken)
            .ConfigureAwait(false);
    }

    /// <summary>
    /// 追加一条 action 事件。事件只记录帧标识与坐标，不写入完整观察数据。
    /// </summary>
    /// <param name="sessionId">会话标识。</param>
    /// <param name="step">动作结果。</param>
    /// <returns>异步任务。</returns>
    private async Task AppendActionEventAsync(string sessionId, AgentStepResultDto step)
    {
        var stepPoint = step.Point;
        object? pointData = stepPoint is null
            ? null
            : new
            {
                normalizedX = stepPoint.NormalizedX,
                normalizedY = stepPoint.NormalizedY,
                captureX = stepPoint.CaptureX,
                captureY = stepPoint.CaptureY,
                desktopX = stepPoint.DesktopX,
                desktopY = stepPoint.DesktopY
            };

        await AppendEventBestEffortAsync(
                sessionId,
                "action",
                new
                {
                    index = step.Index,
                    type = step.Type,
                    ok = step.Ok,
                    message = step.Message,
                    code = step.Code,
                    details = step.Details,
                    frameId = step.FrameId,
                    point = pointData
                },
                CancellationToken.None)
            .ConfigureAwait(false);
    }

    /// <summary>
    /// 追加动作序列取消事件。
    /// </summary>
    /// <param name="sessionId">会话标识。</param>
    /// <param name="stepCount">已执行的动作数量。</param>
    /// <param name="error">取消或停止说明。</param>
    /// <returns>异步任务。</returns>
    private Task AppendSequenceCancelledEventAsync(string sessionId, int stepCount, string? error)
    {
        return AppendEventBestEffortAsync(
            sessionId,
            "sequence.cancelled",
            new
            {
                steps = stepCount,
                stopRequested = _controlState.IsStopRequested,
                error
            },
            CancellationToken.None);
    }

    /// <summary>
    /// 尽力追加会话事件；失败只记录警告，不打断正在执行的动作或停止流程。
    /// </summary>
    /// <param name="sessionId">会话标识；为空时使用默认会话。</param>
    /// <param name="type">事件类型。</param>
    /// <param name="data">事件数据。</param>
    /// <param name="cancellationToken">取消令牌。</param>
    /// <returns>异步任务。</returns>
    private async Task AppendEventBestEffortAsync(
        string? sessionId,
        string type,
        object? data,
        CancellationToken cancellationToken)
    {
        try
        {
            await _sessionStore.AppendEventAsync(sessionId, type, data, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception exception)
        {
            _logger.LogWarning(exception, "追加智能体会话事件 {Type} 失败", type);
        }
    }

    /// <summary>
    /// 根据停止开关生成面向调用方的取消说明。
    /// </summary>
    /// <returns>取消说明。</returns>
    private string BuildCancellationMessage()
    {
        return _controlState.IsStopRequested
            ? "智能体操作已被用户停止。"
            : "动作序列已取消。";
    }

    /// <summary>
    /// 读取当前运行环境的实时快照；窗口已不可用时返回空，避免状态接口失败。
    /// </summary>
    /// <param name="runtime">当前运行环境。</param>
    /// <returns>运行时快照；未启动或读取失败时为空。</returns>
    private AgentRuntimeSnapshotDto? BuildRuntimeSnapshot(GameRuntime? runtime)
    {
        if (runtime is null)
        {
            return null;
        }

        try
        {
            var window = runtime.Window;
            var viewport = window.Viewport;
            var captureArea = new AgentCaptureArea(
                viewport.ScreenRect.X,
                viewport.ScreenRect.Y,
                viewport.ScreenRect.Width,
                viewport.ScreenRect.Height,
                viewport.DpiScale);

            return new AgentRuntimeSnapshotDto
            {
                Kind = runtime.Kind.ToString(),
                ProcessId = window.ProcessId,
                WindowHandle = (long)window.Handle,
                IsAlive = window.IsAlive,
                IsForeground = window.IsForeground,
                IsMinimized = window.IsMinimized,
                RequiresForeground = window.RequiresForeground,
                CaptureArea = captureArea.ToDto()
            };
        }
        catch (Exception exception)
        {
            _logger.LogDebug(exception, "读取游戏运行环境快照失败");
            return null;
        }
    }

    /// <summary>
    /// 包装会话存储调用：取消与已有业务异常原样抛出，参数错误转换为请求错误，其余转换为存储错误。
    /// </summary>
    /// <typeparam name="T">返回类型。</typeparam>
    /// <param name="operation">会话存储操作。</param>
    /// <returns>存储操作结果。</returns>
    private async Task<T> WithSessionStoreAsync<T>(Func<Task<T>> operation)
    {
        EnsureEnabled();
        try
        {
            return await operation().ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (AgentException)
        {
            throw;
        }
        catch (ArgumentException exception)
        {
            throw AgentException.InvalidRequest(exception.Message);
        }
        catch (Exception exception)
        {
            throw new AgentException(
                "SESSION_STORE_ERROR",
                "会话存储操作失败。请检查程序目录的写入权限。",
                HttpStatusCode.InternalServerError,
                exception.Message);
        }
    }
}
