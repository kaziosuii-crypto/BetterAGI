namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Media;

/// <summary>
/// 按需截取游戏画面。实现只允许读取游戏窗口画面，不允许读取桌面其他内容。
/// </summary>
public interface IAgentScreenSource
{
    /// <summary>
    /// 截取一帧。调用方负责释放返回的帧。
    /// </summary>
    /// <exception cref="AgentException">运行环境不可用。</exception>
    AgentFrame Capture();
}
