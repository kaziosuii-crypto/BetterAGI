using BetterGenshinImpact.Core.Simulator;
using Fischless.WindowsInput;
using System;
using Vanara.PInvoke;

namespace BetterGenshinImpact.Core.Input.Backends.Win32;

/// <summary>
/// 前台通道：包装 Simulation.SendInput（Fischless.WindowsInput）
/// </summary>
internal sealed class SendInputChannel : InputChannelBase, IAtomicInputChannel
{
    /// <summary>原子输入与批量释放不能并发。</summary>
    private readonly object _atomicLock = new();

    /// <summary>链式按键与原子输入共用同一闸门。</summary>
    protected override object OperationLock => _atomicLock;

    /// <summary>可替换的单次系统调用，便于不注入真实输入地测试。</summary>
    private readonly Func<User32.INPUT[], uint> _send;

    /// <summary>使用 Windows SendInput。</summary>
    public SendInputChannel() : this(AtomicInputDispatcher.Send) { }

    /// <summary>使用指定的系统调用实现。</summary>
    internal SendInputChannel(Func<User32.INPUT[], uint> send) => _send = send;

    /// <summary>执行一个输入，直接返回该次 SendInput 的接受数量。</summary>
    public InputReceipt ExecuteAtomic(AtomicInput input)
    {
        lock (_atomicLock)
        {
            var builder = new InputBuilder();
            var isKey = input.Kind is AtomicInputKind.KeyDown or AtomicInputKind.KeyUp;
            var down = input.Kind == AtomicInputKind.KeyDown;
            switch (input.Kind)
            {
                case AtomicInputKind.KeyDown:
                case AtomicInputKind.KeyUp:
                    if (TryGetMouseButton(input.Key, out var button))
                    {
                        if (button is InputMouseButton.X1 or InputMouseButton.X2)
                        {
                            var id = button == InputMouseButton.X1 ? 1 : 2;
                            if (down) builder.AddMouseXButtonDown(id); else builder.AddMouseXButtonUp(id);
                        }
                        else
                        {
                            var mouse = button switch
                            {
                                InputMouseButton.Left => MouseButton.LeftButton,
                                InputMouseButton.Right => MouseButton.RightButton,
                                _ => MouseButton.MiddleButton
                            };
                            if (down) builder.AddMouseButtonDown(mouse); else builder.AddMouseButtonUp(mouse);
                        }
                    }
                    else if (down) builder.AddKeyDown(input.Key, ExtendedKeys.IsExtended(input.Key));
                    else builder.AddKeyUp(input.Key, ExtendedKeys.IsExtended(input.Key));
                    break;
                case AtomicInputKind.MoveBy: builder.AddRelativeMouseMovement((int)input.X, (int)input.Y); break;
                case AtomicInputKind.MoveTo: builder.AddAbsoluteMouseMovement((int)Math.Truncate(input.X), (int)Math.Truncate(input.Y)); break;
                case AtomicInputKind.Scroll: builder.AddMouseVerticalWheelScroll((int)input.Y * 120); break;
                default: return new InputReceipt(InputReceiptStatus.Unsupported);
            }

            var inputs = builder.ToArray();
            uint accepted;
            try
            {
                accepted = _send(inputs);
            }
            catch
            {
                // 调用可能在注入后抛错，保留按下记录供后续释放。
                if (isKey && down) SetPressed(input.Key, true);
                throw;
            }

            var receipt = InputReceipt.FromCounts(inputs.Length, accepted);
            if (isKey && receipt.Status == InputReceiptStatus.Accepted) SetPressed(input.Key, down);
            else if (isKey && down && receipt.Status is InputReceiptStatus.Unknown or InputReceiptStatus.Partial)
                SetPressed(input.Key, true);
            return receipt;
        }
    }

    private static InputSimulator Input => Simulation.SendInput;

    // 扩展标志按真实键盘判断（见 ExtendedKeys）：左 Alt 等不带，避免被识别成右 Alt；方向键等带，避免被识别成小键盘按键。
    // 不使用 Fischless 的自动判断，它把左 Alt 与泛指的 Alt / Ctrl 也算作扩展键
    protected override void OnKeyDown(User32.VK key) => Input.Keyboard.KeyDown(ExtendedKeys.IsExtended(key), key);

    protected override void OnKeyUp(User32.VK key) => Input.Keyboard.KeyUp(ExtendedKeys.IsExtended(key), key);

    protected override void OnKeyPress(User32.VK key) => Input.Keyboard.KeyPress(ExtendedKeys.IsExtended(key), key);

    protected override void OnMouseButton(InputMouseButton button, bool down)
    {
        var mouse = Input.Mouse;
        switch (button)
        {
            case InputMouseButton.Left:
                _ = down ? mouse.LeftButtonDown() : mouse.LeftButtonUp();
                break;
            case InputMouseButton.Right:
                _ = down ? mouse.RightButtonDown() : mouse.RightButtonUp();
                break;
            case InputMouseButton.Middle:
                _ = down ? mouse.MiddleButtonDown() : mouse.MiddleButtonUp();
                break;
            case InputMouseButton.X1:
                _ = down ? mouse.XButtonDown(0x0001) : mouse.XButtonUp(0x0001);
                break;
            case InputMouseButton.X2:
                _ = down ? mouse.XButtonDown(0x0002) : mouse.XButtonUp(0x0002);
                break;
        }
    }

    protected override void OnMouseClick(InputMouseButton button)
    {
        var mouse = Input.Mouse;
        switch (button)
        {
            case InputMouseButton.Left:
                mouse.LeftButtonClick();
                break;
            case InputMouseButton.Right:
                mouse.RightButtonClick();
                break;
            case InputMouseButton.Middle:
                mouse.MiddleButtonClick();
                break;
            case InputMouseButton.X1:
                mouse.XButtonClick(0x0001);
                break;
            case InputMouseButton.X2:
                mouse.XButtonClick(0x0002);
                break;
        }
    }

    protected override void OnMoveBy(int dx, int dy) => Input.Mouse.MoveMouseBy(dx, dy);

    protected override void OnMoveTo(double absX, double absY) => Input.Mouse.MoveMouseTo(absX, absY);

    protected override void OnScroll(int clicks) => Input.Mouse.VerticalScroll(clicks);

    /// <summary>
    /// 与原 Simulation.IsKeyDown 一致，查询系统的实际按键状态
    /// </summary>
    public override bool IsKeyDown(User32.VK key) => Simulation.IsKeyDown(key);

    /// <summary>
    /// 按记录抬起，再用 Simulation.ReleaseAllKey 兜底（与原 Simulation.ReleaseAllKey 行为一致）
    /// </summary>
    public override void ReleaseAll()
    {
        lock (_atomicLock)
        {
            base.ReleaseAll();
            Simulation.ReleaseAllKey();
        }
    }
}
