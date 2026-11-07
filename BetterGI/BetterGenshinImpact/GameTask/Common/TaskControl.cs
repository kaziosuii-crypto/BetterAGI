using System;
using System.Threading;
using Fischless.GameCapture;
using Microsoft.Extensions.Logging;
using OpenCvSharp;

namespace BetterGenshinImpact.GameTask.Common;

/// <summary>
/// 与运行环境无关的任务公共设施：日志、独立任务锁、对指定截图器取帧。
/// 绑定到某个运行环境的操作（截图、等待、按 1080P 坐标点击）见 <see cref="GameRuntimeExtensions"/>
/// </summary>
public class TaskControl
{
    /// <summary>
    /// 任务公共日志
    /// </summary>
    /// <remarks>
    /// 延迟解析。TaskControl 位于 GameTask.Common 的最底层，独立任务锁等成员可能被宿主构建之前
    /// 或单元测试中直接访问；若在静态初始化阶段就解析日志，会连带触发 App 宿主构建，
    /// 而宿主构建链上的提权检查会在未提权进程中拉起 BetterGI.exe 并结束当前进程。
    /// </remarks>
    public static ILogger Logger => LoggerHolder.Value;

    /// <summary>
    /// 日志惰性容器，保证 TaskControl 的静态初始化不依赖 App 宿主。
    /// </summary>
    private static readonly Lazy<ILogger> LoggerHolder = new(() => App.GetLogger<TaskControl>());

    /// <summary>
    /// 独立任务锁：同一时刻只允许一个独立任务运行
    /// </summary>
    public static readonly SemaphoreSlim TaskSemaphore = new(1, 1);

    /// <summary>
    /// 失败重试的间隔（毫秒）
    /// </summary>
    private const int CaptureRetryIntervalMilliseconds = 30;

    /// <summary>
    /// 从指定截图器取一帧，失败时重试 3 次
    /// </summary>
    /// <exception cref="Exception">多次重试后仍然截图失败</exception>
    public static Mat CaptureGameImage(IGameCapture? gameCapture)
    {
        var captureFrame = gameCapture?.Capture();
        var image = captureFrame?.Frame;
        if (image != null)
        {
            return image;
        }

        captureFrame?.Dispose();
        Logger.LogWarning("截图失败!");
        // 重试3次。这里只做短暂退避，暂停与焦点检查由调用方的等待负责
        for (var i = 0; i < 3; i++)
        {
            captureFrame = gameCapture?.Capture();
            image = captureFrame?.Frame;
            if (image != null)
            {
                return image;
            }

            captureFrame?.Dispose();
            Thread.Sleep(CaptureRetryIntervalMilliseconds);
        }

        throw new Exception("尝试多次后,截图失败!");
    }

    /// <summary>
    /// 从指定截图器取一帧，不重试；失败时返回 null
    /// </summary>
    public static Mat? CaptureGameImageNoRetry(IGameCapture? gameCapture)
    {
        return gameCapture?.Capture()?.Frame;
    }
}
