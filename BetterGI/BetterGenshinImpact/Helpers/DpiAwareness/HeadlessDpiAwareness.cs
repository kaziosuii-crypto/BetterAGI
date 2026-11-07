using System;
using System.Runtime.CompilerServices;
using Vanara.PInvoke;

namespace BetterGenshinImpact.Helpers.DpiAwareness;

/// <summary>在无界面宿主加载 WPF 前建立进程级物理像素坐标。</summary>
internal static class HeadlessDpiAwareness
{
    /// <summary>早期初始化失败由宿主启动流程转为非零退出。</summary>
    internal static Exception? StartupError { get; private set; }

    /// <summary>识别所有会进入无界面错误处理的命令行入口。</summary>
    internal static bool IsHeadlessRequest(string[] arguments) => Array.Exists(arguments, argument =>
        string.Equals(argument, CommandLineOptions.HeadlessArgument, StringComparison.OrdinalIgnoreCase)
        || string.Equals(argument, CommandLineOptions.HeadlessConfigArgument, StringComparison.OrdinalIgnoreCase)
        || string.Equals(argument, CommandLineOptions.HeadlessTokenFileArgument, StringComparison.OrdinalIgnoreCase));

    /// <summary>程序集加载时设置，避免 WPF 或窗口 API 先固定 DPI 上下文。</summary>
    [ModuleInitializer]
    internal static void Initialize()
    {
        try
        {
            EnsurePerMonitor(IsHeadlessRequest(Environment.GetCommandLineArgs()),
                () => _ = SHCore.SetProcessDpiAwareness(SHCore.PROCESS_DPI_AWARENESS.PROCESS_PER_MONITOR_DPI_AWARE),
                () =>
                {
                    SHCore.GetProcessDpiAwareness(default, out var awareness).ThrowIfFailed();
                    return awareness == SHCore.PROCESS_DPI_AWARENESS.PROCESS_PER_MONITOR_DPI_AWARE;
                });
        }
        catch (Exception error)
        {
            StartupError = error;
        }
    }

    /// <summary>无界面模式必须验证实际 DPI 状态，不能仅依赖设置调用的返回值。</summary>
    internal static void EnsurePerMonitor(bool headless, Action setAwareness, Func<bool> isPerMonitor)
    {
        if (!headless) return;
        setAwareness();
        if (!isPerMonitor())
            throw new InvalidOperationException("无界面宿主无法启用按显示器 DPI 感知，拒绝使用虚拟化坐标。");
    }
}
