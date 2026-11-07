using BetterGenshinImpact.Helpers;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Media;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Models;
using System;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Input;

/// <summary>
/// 把智能体给出的坐标统一换算为归一化、截图和桌面三种表示。
/// <para>
/// 前台 SendInput 和后台 PostMessage 的绝对坐标都按主显示器归一化，因此当前实现只支持位于主显示器内的游戏窗口；
/// 副屏或负坐标会在动作执行前被拒绝，避免静默点错位置。
/// </para>
/// </summary>
public static class AgentCoordinateMapper
{
    /// <summary>
    /// 解析动作中的目标坐标。
    /// </summary>
    /// <param name="action">包含坐标的动作。</param>
    /// <param name="area">当前游戏画面区域。</param>
    /// <returns>解析后的坐标。</returns>
    public static AgentResolvedPoint Resolve(AgentActionDto action, AgentCaptureArea area)
    {
        if (action.X is null || action.Y is null)
        {
            throw AgentException.InvalidRequest($"动作 {action.Type} 缺少 X 或 Y 坐标。");
        }

        return Resolve(action.X.Value, action.Y.Value, action.CoordinateSpace, area);
    }

    /// <summary>
    /// 解析一个坐标。
    /// </summary>
    /// <param name="x">X 坐标。</param>
    /// <param name="y">Y 坐标。</param>
    /// <param name="coordinateSpace">坐标空间：normalized、capture 或 desktop。</param>
    /// <param name="area">当前游戏画面区域。</param>
    /// <returns>解析后的坐标。</returns>
    public static AgentResolvedPoint Resolve(double x, double y, string? coordinateSpace, AgentCaptureArea area)
    {
        if (!double.IsFinite(x) || !double.IsFinite(y))
        {
            throw AgentException.InvalidRequest("坐标必须是有限数值。");
        }

        var space = string.IsNullOrWhiteSpace(coordinateSpace)
            ? "normalized"
            : coordinateSpace.Trim().ToLowerInvariant();

        double normalizedX;
        double normalizedY;
        switch (space)
        {
            case "normalized":
                if (x is < 0 or > 1 || y is < 0 or > 1)
                {
                    throw AgentException.InvalidRequest("归一化坐标必须位于 0～1 之间。", $"x={x}, y={y}");
                }

                normalizedX = x;
                normalizedY = y;
                break;
            case "capture":
                if (x < 0 || x > area.Width || y < 0 || y > area.Height)
                {
                    throw AgentException.InvalidRequest(
                        "截图坐标必须位于游戏画面内。",
                        $"x={x}, y={y}, width={area.Width}, height={area.Height}");
                }

                normalizedX = x / area.Width;
                normalizedY = y / area.Height;
                break;
            case "desktop":
                if (x < area.X || x > area.X + area.Width || y < area.Y || y > area.Y + area.Height)
                {
                    throw AgentException.InvalidRequest(
                        "桌面坐标必须位于游戏画面内。",
                        $"x={x}, y={y}, area={area.X},{area.Y},{area.Width},{area.Height}");
                }

                normalizedX = (x - area.X) / area.Width;
                normalizedY = (y - area.Y) / area.Height;
                break;
            default:
                throw AgentException.InvalidRequest("坐标空间只能是 normalized、capture 或 desktop。", coordinateSpace);
        }

        var captureX = normalizedX * area.Width;
        var captureY = normalizedY * area.Height;
        var desktopX = area.X + captureX;
        var desktopY = area.Y + captureY;

        var screen = PrimaryScreen.WorkingArea;
        if (desktopX < 0 || desktopY < 0 || desktopX >= screen.Width || desktopY >= screen.Height)
        {
            throw new AgentException(
                "COORDINATE_OUT_OF_PRIMARY_SCREEN",
                "目标坐标不在主显示器内。当前模拟输入只支持主显示器上的游戏窗口。",
                System.Net.HttpStatusCode.BadRequest,
                $"desktop=({desktopX:0.#},{desktopY:0.#}), primary=({screen.Width}x{screen.Height})");
        }

        return new AgentResolvedPoint(
            normalizedX,
            normalizedY,
            captureX,
            captureY,
            desktopX,
            desktopY,
            desktopX * 65535d / screen.Width,
            desktopY * 65535d / screen.Height);
    }
}

/// <summary>
/// 解析后的坐标，包含输入层需要的归一化桌面坐标。
/// </summary>
/// <param name="NormalizedX">相对游戏画面的归一化 X。</param>
/// <param name="NormalizedY">相对游戏画面的归一化 Y。</param>
/// <param name="CaptureX">相对游戏画面的像素 X。</param>
/// <param name="CaptureY">相对游戏画面的像素 Y。</param>
/// <param name="DesktopX">桌面物理像素 X。</param>
/// <param name="DesktopY">桌面物理像素 Y。</param>
/// <param name="MouseNormalizedX">主显示器 0～65535 输入的 X。</param>
/// <param name="MouseNormalizedY">主显示器 0～65535 输入的 Y。</param>
public readonly record struct AgentResolvedPoint(
    double NormalizedX,
    double NormalizedY,
    double CaptureX,
    double CaptureY,
    double DesktopX,
    double DesktopY,
    double MouseNormalizedX,
    double MouseNormalizedY)
{
    /// <summary>
    /// 转换为共享 DTO。
    /// </summary>
    public AgentPointDto ToDto()
    {
        return new AgentPointDto
        {
            NormalizedX = NormalizedX,
            NormalizedY = NormalizedY,
            CaptureX = CaptureX,
            CaptureY = CaptureY,
            DesktopX = DesktopX,
            DesktopY = DesktopY
        };
    }
}
