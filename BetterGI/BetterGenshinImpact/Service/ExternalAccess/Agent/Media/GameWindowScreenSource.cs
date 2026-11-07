using BetterGenshinImpact.GameTask.Common;
using BetterGenshinImpact.GameTask.Model;
using BetterGenshinImpact.GameTask.Runtime;
using BetterGenshinImpact.Helpers;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Runtime;
using System;
using System.Net;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Media;

/// <summary>
/// 基于 BetterGI 当前运行环境的单帧截图源。
/// <para>
/// 只复用 <see cref="GameRuntimeService.Current"/> 已经启动的截图器，不自行创建第二条捕获链路；
/// 画面区域的几何信息在截图前后各读取一次，优先使用截图后的实时值。
/// </para>
/// </summary>
public sealed class GameWindowScreenSource(IGameRuntimeAccessor runtimeAccessor) : IAgentScreenSource
{
    /// <summary>无界面截图必须与采集前后的物理区域和 DPI 一致。</summary>
    internal static bool MatchesCapturedFrame(GameViewport before, GameViewport after, int width, int height) =>
        before == after && width > 0 && height > 0 && width == after.Width && height == after.Height;

    /// <inheritdoc />
    public AgentFrame Capture()
    {
        var runtime = runtimeAccessor.Current
                      ?? throw AgentException.RuntimeUnavailable("GameRuntimeService.Current 为空。");

        if (runtime.IsStopping || !runtime.Window.IsAlive)
        {
            throw AgentException.RuntimeUnavailable("游戏运行环境正在停止或游戏已经退出。");
        }

        // 截图前读取一次，截图后读取一次：窗口可能在两次读取之间被移动或缩放。
        var viewportBefore = runtime.Window.Viewport;
        OpenCvSharp.Mat image;
        try
        {
            image = TaskControl.CaptureGameImage(runtime.Capture);
        }
        catch (Exception exception) when (runtime.IsStopping || !runtime.Window.IsAlive)
        {
            throw AgentException.RuntimeUnavailable(exception.Message);
        }
        catch (Exception exception)
        {
            throw new AgentException(
                "CAPTURE_FAILED",
                "截图失败，未从游戏窗口取得画面。",
                HttpStatusCode.ServiceUnavailable,
                exception.ToString());
        }

        GameViewport viewportAfter;
        try { viewportAfter = runtime.Window.Viewport; }
        catch
        {
            image.Dispose();
            throw;
        }
        if (CommandLineOptions.Instance.IsHeadless
            && !MatchesCapturedFrame(viewportBefore, viewportAfter, image.Width, image.Height))
        {
            image.Dispose();
            throw new AgentException("CAPTURE_GEOMETRY_CHANGED", "游戏窗口或 DPI 在截图期间变化，已丢弃画面。",
                HttpStatusCode.Conflict);
        }
        if (viewportBefore != viewportAfter)
        {
            // 使用截图后的实时区域作为坐标基准；帧尺寸与区域尺寸不一致时，调用方仍按归一化坐标操作。
            viewportBefore = viewportAfter;
        }

        var captureArea = new AgentCaptureArea(
            viewportBefore.ScreenRect.X,
            viewportBefore.ScreenRect.Y,
            viewportBefore.ScreenRect.Width,
            viewportBefore.ScreenRect.Height,
            viewportBefore.DpiScale);

        ISystemInfo systemInfo;
        try
        {
            // 用截图时的实时区域重建画面信息，避免调度器尚未同步的旧 CaptureAreaRect。
            systemInfo = new SystemInfo(viewportBefore);
        }
        catch (Exception exception)
        {
            image.Dispose();
            throw new AgentException(
                "INVALID_GAME_RESOLUTION",
                "游戏画面分辨率不受支持。",
                HttpStatusCode.Conflict,
                exception.Message);
        }

        var runtimeSnapshot = new Models.AgentRuntimeSnapshotDto
        {
            Kind = runtime.Kind.ToString(),
            ProcessId = runtime.Window.ProcessId,
            WindowHandle = (long)runtime.Window.Handle,
            IsAlive = runtime.Window.IsAlive,
            IsForeground = runtime.Window.IsForeground,
            IsMinimized = runtime.Window.IsMinimized,
            RequiresForeground = runtime.Window.RequiresForeground,
            CaptureArea = captureArea.ToDto()
        };

        return new AgentFrame
        {
            Image = image,
            SystemInfo = systemInfo,
            CaptureArea = captureArea,
            Runtime = runtimeSnapshot
        };
    }
}
