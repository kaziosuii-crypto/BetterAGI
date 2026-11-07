using System;
using System.Threading;
using Vanara.PInvoke;

namespace BetterGenshinImpact.Core.Simulator;

/// <summary>
///     虚拟键代码
///     https://learn.microsoft.com/zh-cn/windows/win32/inputdev/virtual-key-codes
///     User32.VK.VK_SPACE 键盘空格键
/// </summary>
public class PostMessageSimulator
{
    public static readonly uint WM_LBUTTONDOWN = 0x201; //按下鼠标左键

    public static readonly uint WM_LBUTTONUP = 0x202; //释放鼠标左键

    public static readonly uint WM_RBUTTONDOWN = 0x204;
    public static readonly uint WM_RBUTTONUP = 0x205;

    private readonly IntPtr _hWnd;

    /// <summary>消息投递与隔离状态的同步锁。</summary>
    private static readonly object InputGate = new();
    /// <summary>当前持有后台消息隔离的租约数。</summary>
    private static int _inputPauseCount;
    /// <summary>消息一旦投递，无法证明游戏窗口已经消费。</summary>
    private static bool _unconfirmedMessage;

    /// <summary>阻止新的后台消息；此前投递过消息时无法证明队列已排空。</summary>
    internal static IDisposable PauseInput()
    {
        lock (InputGate)
        {
            if (_unconfirmedMessage)
                throw new InvalidOperationException("后台窗口消息的消费状态无法确认，拒绝隔离输入。");
            var lease = new InputPauseLease();
            _inputPauseCount++;
            return lease;
        }
    }

    /// <summary>一个可嵌套且可重复释放的后台消息隔离租约。</summary>
    private sealed class InputPauseLease : IDisposable
    {
        private int _disposed;
        public void Dispose()
        {
            if (Interlocked.Exchange(ref _disposed, 1) == 0)
                lock (InputGate) _inputPauseCount--;
        }
    }

    /// <summary>同一锁内排除隔离并记录无法确认的窗口消息。</summary>
    private void Post(uint message, nint wParam, nint lParam = 0)
    {
        lock (InputGate)
        {
            if (_inputPauseCount > 0)
                throw new InvalidOperationException("后台窗口消息在输入隔离期间不可发送。");
            _unconfirmedMessage = true;
            User32.PostMessage(_hWnd, message, wParam, lParam);
        }
    }

    /// <summary>发送枚举类型的窗口消息。</summary>
    private void Post(User32.WindowMessage message, nint wParam, nint lParam = 0)
        => Post((uint)message, wParam, lParam);

    public PostMessageSimulator(IntPtr hWnd)
    {
        _hWnd = hWnd;
    }

    /// <summary>
    ///     按键消息的 lParam：重复次数 1，扫描码按 VK 计算；扩展键带第 24 位，判断规则与前台 SendInput 通道相同（见 ExtendedKeys）；
    ///     抬起时带“之前为按下”和“状态转换”位
    /// </summary>
    public static nint MakeKeyLParam(User32.VK vk, bool keyUp)
    {
        var scan = User32.MapVirtualKey((uint)vk, 0) & 0xFFu;
        var lParam = 1u | (scan << 16);
        if (ExtendedKeys.IsExtended(vk))
        {
            lParam |= 1u << 24;
        }

        if (keyUp)
        {
            lParam |= (1u << 30) | (1u << 31);
        }

        // 与原实现 unchecked((nint)0xc01e0001) 一致：按无符号 32 位零扩展
        return unchecked((nint)lParam);
    }

    public static int MakeLParam(int x, int y) => (y << 16) | (x & 0xFFFF);

    /// <summary>
    ///     通知窗口已激活，游戏在后台时需要先发送它，按键和点击才会生效
    /// </summary>
    public PostMessageSimulator Activate()
    {
        Post(User32.WindowMessage.WM_ACTIVATE, 1, 0);
        return this;
    }

    /// <summary>
    ///     指定位置并按下左键
    /// </summary>
    /// <param name="x"></param>
    /// <param name="y"></param>
    public PostMessageSimulator LeftButtonClick(int x, int y)
    {
        IntPtr p = MakeLParam(x, y);
        Post(WM_LBUTTONDOWN, IntPtr.Zero, p);
        Thread.Sleep(100);
        Post(WM_LBUTTONUP, IntPtr.Zero, p);
        return this;
    }

    /// <summary>
    ///     指定位置并按下左键
    /// </summary>
    /// <param name="x"></param>
    /// <param name="y"></param>
    public PostMessageSimulator LeftButtonClickBackground(int x, int y)
    {
        Activate();
        var p = MakeLParam(x, y);
        Post(WM_LBUTTONDOWN, 1, p);
        Thread.Sleep(100);
        Post(WM_LBUTTONUP, 0, p);
        return this;
    }

    public PostMessageSimulator LeftButtonClick()
    {
        return LeftButtonClick(16, 16);
    }

    public PostMessageSimulator LeftButtonClickBackground()
    {
        Activate();
        return LeftButtonClick(16, 16);
    }

    /// <summary>
    ///     默认位置左键按下
    /// </summary>
    public PostMessageSimulator LeftButtonDown()
    {
        Post(WM_LBUTTONDOWN, IntPtr.Zero);
        return this;
    }

    /// <summary>
    ///     默认位置左键释放
    /// </summary>
    public PostMessageSimulator LeftButtonUp()
    {
        Post(WM_LBUTTONUP, IntPtr.Zero);
        return this;
    }

    /// <summary>
    ///     指定位置左键按下
    /// </summary>
    public PostMessageSimulator LeftButtonDown(int x, int y)
    {
        Post(WM_LBUTTONDOWN, IntPtr.Zero, MakeLParam(x, y));
        return this;
    }

    /// <summary>
    ///     指定位置左键释放
    /// </summary>
    public PostMessageSimulator LeftButtonUp(int x, int y)
    {
        Post(WM_LBUTTONUP, IntPtr.Zero, MakeLParam(x, y));
        return this;
    }

    /// <summary>
    ///     默认位置右键按下
    /// </summary>
    public PostMessageSimulator RightButtonDown()
    {
        Post(WM_RBUTTONDOWN, IntPtr.Zero);
        return this;
    }

    /// <summary>
    ///     默认位置右键释放
    /// </summary>
    public PostMessageSimulator RightButtonUp()
    {
        Post(WM_RBUTTONUP, IntPtr.Zero);
        return this;
    }

    /// <summary>
    ///     指定位置右键按下
    /// </summary>
    public PostMessageSimulator RightButtonDown(int x, int y)
    {
        Post(WM_RBUTTONDOWN, IntPtr.Zero, MakeLParam(x, y));
        return this;
    }

    /// <summary>
    ///     指定位置右键释放
    /// </summary>
    public PostMessageSimulator RightButtonUp(int x, int y)
    {
        Post(WM_RBUTTONUP, IntPtr.Zero, MakeLParam(x, y));
        return this;
    }

    public PostMessageSimulator RightButtonClick()
    {
        return RightButtonClick(16, 16);
    }

    /// <summary>
    ///     指定位置右键点击
    /// </summary>
    public PostMessageSimulator RightButtonClick(int x, int y)
    {
        IntPtr p = MakeLParam(x, y);
        Post(WM_RBUTTONDOWN, IntPtr.Zero, p);
        Thread.Sleep(100);
        Post(WM_RBUTTONUP, IntPtr.Zero, p);
        return this;
    }

    public PostMessageSimulator KeyPress(User32.VK vk)
    {
        Post(User32.WindowMessage.WM_KEYDOWN, (nint)vk, MakeKeyLParam(vk, false));
        Post(User32.WindowMessage.WM_CHAR, (nint)vk, MakeKeyLParam(vk, false));
        Post(User32.WindowMessage.WM_KEYUP, (nint)vk, MakeKeyLParam(vk, true));
        return this;
    }

    public PostMessageSimulator KeyPress(User32.VK vk, int ms)
    {
        Post(User32.WindowMessage.WM_KEYDOWN, (nint)vk, MakeKeyLParam(vk, false));
        Thread.Sleep(ms);
        Post(User32.WindowMessage.WM_CHAR, (nint)vk, MakeKeyLParam(vk, false));
        Post(User32.WindowMessage.WM_KEYUP, (nint)vk, MakeKeyLParam(vk, true));
        return this;
    }

    public PostMessageSimulator LongKeyPress(User32.VK vk)
    {
        return KeyPress(vk, 1000);
    }

    public PostMessageSimulator KeyDown(User32.VK vk)
    {
        Post(User32.WindowMessage.WM_KEYDOWN, (nint)vk, MakeKeyLParam(vk, false));
        return this;
    }

    public PostMessageSimulator KeyUp(User32.VK vk)
    {
        Post(User32.WindowMessage.WM_KEYUP, (nint)vk, MakeKeyLParam(vk, true));
        return this;
    }

    public PostMessageSimulator KeyPressBackground(User32.VK vk)
    {
        Activate();
        return KeyPress(vk);
    }

    public PostMessageSimulator KeyDownBackground(User32.VK vk)
    {
        Activate();
        return KeyDown(vk);
    }

    public PostMessageSimulator KeyUpBackground(User32.VK vk)
    {
        Activate();
        return KeyUp(vk);
    }

    public PostMessageSimulator Sleep(int ms)
    {
        Thread.Sleep(ms);
        return this;
    }
}
