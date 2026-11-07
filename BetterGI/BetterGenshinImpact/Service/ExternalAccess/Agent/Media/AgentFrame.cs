using BetterGenshinImpact.GameTask.Model;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Models;
using OpenCvSharp;
using System;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Media;

/// <summary>
/// 智能体适配器捕获的一帧。持有 OpenCV Mat，使用后必须释放。
/// </summary>
public sealed class AgentFrame : IDisposable
{
    /// <summary>
    /// 原始捕获画面（BGR、CV_8UC3）。
    /// </summary>
    public required Mat Image { get; init; }

    /// <summary>
    /// 与这一帧对应的画面信息，用于把图像坐标换算到 1080P 区域树。
    /// </summary>
    public required ISystemInfo SystemInfo { get; init; }

    /// <summary>
    /// 这一帧对应的游戏画面区域。
    /// </summary>
    public required AgentCaptureArea CaptureArea { get; init; }

    /// <summary>
    /// 捕获时的运行环境快照。
    /// </summary>
    public required AgentRuntimeSnapshotDto Runtime { get; init; }

    /// <summary>
    /// 原始画面宽度。
    /// </summary>
    public int OriginalWidth => Image.Width;

    /// <summary>
    /// 原始画面高度。
    /// </summary>
    public int OriginalHeight => Image.Height;

    /// <summary>
    /// 释放画面占用的非托管内存。
    /// </summary>
    public void Dispose()
    {
        Image.Dispose();
    }
}
