using BetterGenshinImpact.Service.ExternalAccess.Agent.Models;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Media;

/// <summary>
/// 游戏画面在桌面上的矩形区域，内部使用物理像素。
/// </summary>
/// <param name="X">左上角 X 坐标。</param>
/// <param name="Y">左上角 Y 坐标。</param>
/// <param name="Width">宽度。</param>
/// <param name="Height">高度。</param>
/// <param name="DpiScale">输入坐标缩放比例。</param>
public readonly record struct AgentCaptureArea(int X, int Y, int Width, int Height, float DpiScale)
{
    /// <summary>
    /// 转换为共享 DTO。
    /// </summary>
    public AgentCaptureAreaDto ToDto()
    {
        return new AgentCaptureAreaDto
        {
            X = X,
            Y = Y,
            Width = Width,
            Height = Height,
            DpiScale = DpiScale
        };
    }
}
