using BetterGenshinImpact.Service.ExternalAccess.Agent.Media;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Models;
using System.Threading;
using System.Threading.Tasks;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Input;

/// <summary>
/// 执行单个键鼠动作。实现只允许调用 BetterGI 已有的模拟输入层，不允许访问游戏进程内存。
/// </summary>
public interface IAgentInputController
{
    /// <summary>
    /// 执行一个键鼠动作。
    /// </summary>
    /// <param name="action">动作。</param>
    /// <param name="area">当前游戏画面区域。</param>
    /// <param name="defaultChannel">请求级默认通道。</param>
    /// <param name="cancellationToken">取消令牌。</param>
    Task ExecuteAsync(AgentActionDto action, AgentCaptureArea area, string defaultChannel, CancellationToken cancellationToken);

    /// <summary>
    /// 释放适配器自己按下、尚未抬起的按键和鼠标键。
    /// </summary>
    void ReleasePressedInputs();
}
