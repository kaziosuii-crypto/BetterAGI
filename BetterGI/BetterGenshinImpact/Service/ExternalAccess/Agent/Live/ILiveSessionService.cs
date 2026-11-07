using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Live;

/// <summary>排空同步截图触发器并持有暂停；不阻断其他输入或异步工作。</summary>
public interface ILiveTriggerCaptureIsolation
{
    /// <summary>取得触发器同步截图暂停句柄。</summary>
    IDisposable Pause();
}

/// <summary>将宿主调度器的暂停操作提供给实时会话。</summary>
public sealed class LiveTriggerCaptureIsolation(Func<IDisposable> pause) : ILiveTriggerCaptureIsolation
{
    /// <inheritdoc />
    public IDisposable Pause() => pause();
}

/// <summary>
/// 实时会话服务：采集游戏画面、广播帧、接收动作并执行、维护指导与解说时间线。
/// </summary>
public interface ILiveSessionService
{
    /// <summary>是否正在运行。</summary>
    bool IsRunning { get; }

    /// <summary>启动实时会话。</summary>
    /// <param name="request">启动参数。</param>
    /// <param name="cancellationToken">取消令牌。</param>
    /// <returns>启动后的状态。</returns>
    Task<LiveStatusDto> StartAsync(LiveStartRequest request, CancellationToken cancellationToken);

    /// <summary>停止实时会话并释放按键。</summary>
    /// <param name="cancellationToken">取消令牌。</param>
    Task StopAsync(CancellationToken cancellationToken);

    /// <summary>等待帧循环已停止后的后台视觉分析释放其副本。</summary>
    Task WaitForBackgroundVisionAsync(CancellationToken cancellationToken) => Task.CompletedTask;

    /// <summary>等待全局停止回调派生的会话清理任务。</summary>
    Task WaitForBackgroundStopsAsync(CancellationToken cancellationToken) => Task.CompletedTask;

    /// <summary>获取当前状态。</summary>
    LiveStatusDto GetStatus();

    /// <summary>获取最新一帧；没有帧时返回空。</summary>
    LiveFrameSnapshot? GetLatestFrame();

    /// <summary>设置最新指导并广播。</summary>
    Task<LiveTimelineEventDto> SetGuidanceAsync(LiveGuidanceRequest request, CancellationToken cancellationToken);

    /// <summary>追加一条解说并广播。</summary>
    Task<LiveTimelineEventDto> AddCommentaryAsync(LiveCommentaryRequest request,
        CancellationToken cancellationToken);

    /// <summary>读取时间线。</summary>
    Task<IReadOnlyList<LiveTimelineEventDto>> GetTimelineAsync(long afterSeq, int limit,
        CancellationToken cancellationToken);

    /// <summary>注入一个动作块。</summary>
    Task<LiveActionAckDto> EnqueueActionsAsync(LiveActionBlockDto block, CancellationToken cancellationToken,
        LiveLeaseCredentialsDto? credentials = null);

    /// <summary>核验 Player 对话决策并返回明确的未执行或执行状态。</summary>
    LiveDialogueDecisionResult SubmitDialogueDecision(LiveDialogueDecisionRequest request,
        LiveLeaseCredentialsDto? credentials) => new()
    {
        IntentId = request.IntentId, Executed = false, Reason = "dialogue_action_authorization_unavailable"
    };

    /// <summary>为当前会话申请唯一 Live 输入租约。</summary>
    LiveLeaseGrantDto AcquireLease();

    /// <summary>将预备动作凭据绑定真实 Live 连接，不允许注入。</summary>
    LiveLeaseBindingDto? BindPreparedLease(LiveLeaseCredentialsDto? credentials, string connectionId, string requestId) => null;

    /// <summary>仅查询当前连接的绑定状态。</summary>
    LiveLeaseBindingDto? ProbeBoundLease(LiveLeaseCredentialsDto? credentials, string connectionId, string requestId) => null;

    /// <summary>由已绑定连接激活经 owner 提交的租约。</summary>
    LiveLeaseBindingDto? ActivateBoundLease(LiveLeaseCredentialsDto? credentials, string connectionId, string requestId) => null;

    /// <summary>由 Player owner 凭据提交已绑定且未过期的预备租约。</summary>
    LiveLeaseBindingDto? CommitPreparedLease(LiveLeaseCommitRequest request) => null;

    /// <summary>续期当前会话的有效租约。</summary>
    bool RenewLease(LiveLeaseCredentialsDto? credentials);

    /// <summary>只验证动作凭据，不延长租约。</summary>
    bool ValidateActionLease(LiveLeaseCredentialsDto? credentials);

    /// <summary>操作桥断开时撤销匹配的动作租约。</summary>
    bool ReleaseActionLease(LiveLeaseCredentialsDto? credentials);

    /// <summary>撤销匹配的租约并释放其输入。</summary>
    bool ReleaseLease(LiveLeaseCredentialsDto? credentials);

    /// <summary>清空动作队列并释放按键。</summary>
    void StopActions();

    /// <summary>旧连接断开时只停止无租约动作，不影响之后授予的租约。</summary>
    void StopLegacyActions();

    /// <summary>将同次停止结果绑定到当前会话和请求；不代表游戏响应。</summary>
    LiveStopReceiptDto StopActionsWithEvidence(string stopRequestId);

    /// <summary>注册 WebSocket 客户端。</summary>
    void AttachClient(LiveClientConnection client);

    /// <summary>按连接实例注销 WebSocket 客户端。</summary>
    void DetachClient(LiveClientConnection client);
}
