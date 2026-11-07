using BetterGenshinImpact.GameTask.Runtime;
using System.Threading;
using System.Threading.Tasks;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Runtime;

/// <summary>
/// 为智能体适配器提供当前游戏运行环境。实现必须复用 BetterGI 已有的运行环境编排，
/// 不自行创建第二条截图或输入链路。
/// </summary>
public interface IGameRuntimeAccessor
{
    /// <summary>
    /// 当前运行环境；截图器未启动时为空。每次使用都应重新读取，不要缓存。
    /// </summary>
    GameRuntime? Current { get; }

    /// <summary>
    /// 当前是否已绑定运行环境。
    /// </summary>
    bool IsRunning { get; }

    /// <summary>
    /// 请求启动并绑定运行环境。该方法会同时启动实时触发器，可能激活游戏窗口。
    /// </summary>
    /// <param name="cancellationToken">取消令牌。</param>
    /// <returns>绑定后的运行环境；无法启动时为空。</returns>
    Task<GameRuntime?> StartAsync(CancellationToken cancellationToken);

    /// <summary>
    /// 停止并解绑当前运行环境。
    /// </summary>
    /// <param name="cancellationToken">取消令牌。</param>
    Task StopAsync(CancellationToken cancellationToken);
}
