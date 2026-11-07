using BetterGenshinImpact.Core.Input.Backends.Win32;
using System;
using System.Threading;
using Vanara.PInvoke;

namespace BetterGenshinImpact.Core.Input;

/// <summary>
/// 模拟输入的统一入口。
/// 业务代码只通过这里取通道，不直接使用 Simulation / PostMessageSimulator 等原始实现。
/// <para>
/// 通道引用只能在单个任务内持有；跨任务存活的对象（静态字段、DI 单例、Trigger 等）必须每次从这里取，
/// 否则后端切换后引用会过期。
/// </para>
/// </summary>
public static class InputHub
{
    private static readonly object AttachLock = new();

    /// <summary>
    /// 未 Attach 时：未绑定窗口的 Win32 后端，前台可用，后台 warn
    /// </summary>
    private static IInputBackend _backend = new Win32InputBackend(IntPtr.Zero);

    public static IInputBackend Backend => Volatile.Read(ref _backend);

    public static IInputChannel Foreground => Backend.Foreground;

    /// <summary>
    /// 后台通道。WebSdk / Gamepad 后端下与 <see cref="Foreground"/> 相同
    /// </summary>
    public static IInputChannel Background => Backend.Background;

    /// <summary>
    /// 替换当前后端并释放旧后端。按键释放由停止流程显式处理，避免绑定新后端时释放全局按键。
    /// 只在没有任务运行时调用。
    /// (a,b)、c、d 之间的切换都通过它完成
    /// </summary>
    public static void Attach(IInputBackend backend)
    {
        ArgumentNullException.ThrowIfNull(backend);
        lock (AttachLock)
        {
            var old = Backend;
            if (ReferenceEquals(old, backend))
            {
                return;
            }

            Volatile.Write(ref _backend, backend);
            old.Dispose();
        }
    }

    /// <summary>
    /// 释放当前后端所有通道中处于按下状态的按键
    /// </summary>
    public static void ReleaseAll() => Backend.ReleaseAll();

    /// <summary>在指定通道执行一次输入；没有回执能力的通道只报告未知。</summary>
    public static InputReceipt ExecuteAtomic(IInputChannel channel, AtomicInput input)
    {
        ArgumentNullException.ThrowIfNull(channel);
        if (channel is IAtomicInputChannel atomic)
        {
            return atomic.ExecuteAtomic(input);
        }

        ExecuteLegacy(channel, input);
        return new InputReceipt(InputReceiptStatus.Unknown);
    }

    /// <summary>调用原链式 API，供没有原子回执的后端沿用既有行为。</summary>
    internal static void ExecuteLegacy(IInputChannel channel, AtomicInput input)
    {
        switch (input.Kind)
        {
            case AtomicInputKind.KeyDown:
            case AtomicInputKind.KeyUp:
                var down = input.Kind == AtomicInputKind.KeyDown;
                switch (input.Key)
                {
                    case User32.VK.VK_LBUTTON: if (down) channel.Mouse.LeftButtonDown(); else channel.Mouse.LeftButtonUp(); break;
                    case User32.VK.VK_RBUTTON: if (down) channel.Mouse.RightButtonDown(); else channel.Mouse.RightButtonUp(); break;
                    case User32.VK.VK_MBUTTON: if (down) channel.Mouse.MiddleButtonDown(); else channel.Mouse.MiddleButtonUp(); break;
                    case User32.VK.VK_XBUTTON1: if (down) channel.Mouse.XButtonDown(1); else channel.Mouse.XButtonUp(1); break;
                    case User32.VK.VK_XBUTTON2: if (down) channel.Mouse.XButtonDown(2); else channel.Mouse.XButtonUp(2); break;
                    default: if (down) channel.Keyboard.KeyDown(input.Key); else channel.Keyboard.KeyUp(input.Key); break;
                }
                break;
            case AtomicInputKind.MoveBy: channel.Mouse.MoveMouseBy((int)input.X, (int)input.Y); break;
            case AtomicInputKind.MoveTo: channel.Mouse.MoveMouseTo(input.X, input.Y); break;
            case AtomicInputKind.Scroll: channel.Mouse.VerticalScroll((int)input.Y); break;
            default: throw new ArgumentOutOfRangeException(nameof(input));
        }
    }
}
