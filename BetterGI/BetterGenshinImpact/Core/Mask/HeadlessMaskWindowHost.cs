using System;
using System.Windows;

namespace BetterGenshinImpact.Core.Mask;

/// <summary>无界面宿主的遮罩空实现，不创建窗口或触发窗口状态事件。</summary>
public sealed class HeadlessMaskWindowHost : IMaskWindowHost
{
    /// <summary>无界面模式下遮罩始终不可见且没有位置。</summary>
    public MaskWindowState State => new(false, Rect.Empty);

    /// <summary>无可见状态变化，订阅和取消订阅均不保留回调。</summary>
    public event EventHandler<MaskWindowState>? StateChanged
    {
        add { }
        remove { }
    }

    /// <summary>不创建或绑定窗口。</summary>
    public void Attach(nint gameHandle) { }

    /// <summary>无窗口需要解绑。</summary>
    public void Detach() { }

    /// <summary>无窗口需要关闭。</summary>
    public void Close() { }

    /// <summary>无界面模式不展示或跟随游戏窗口。</summary>
    public void ReportGameWindow(GameWindowState state) { }
}
