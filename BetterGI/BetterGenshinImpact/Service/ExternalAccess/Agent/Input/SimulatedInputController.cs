using BetterGenshinImpact.Core.Input;
using BetterGenshinImpact.Core.Simulator;
using BetterGenshinImpact.GameTask.Runtime;
using BetterGenshinImpact.Helpers;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Media;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Models;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Runtime;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Safety;
using Microsoft.Extensions.Logging;
using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Threading;
using System.Threading.Tasks;
using Vanara.PInvoke;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Input;

/// <summary>
/// 基于 <see cref="InputHub"/> 的模拟输入控制器。
/// <para>
/// 前台通道使用 SendInput，需要游戏窗口位于前台；后台通道使用 PostMessage，可与前台窗口无关，
/// 但不支持相对移动、拖拽、滚轮、中键和文本输入。
/// </para>
/// </summary>
public sealed class SimulatedInputController(
    IGameRuntimeAccessor runtimeAccessor,
    ILogger<SimulatedInputController> logger) : IAgentInputController
{
    /// <summary>
    /// 适配器已按下、尚未抬起的键盘键，以及每个键仍处于按下状态的通道。
    /// 同一虚拟键可能先后在前台和后台按下，必须分别记录并分别释放。
    /// </summary>
    private readonly Dictionary<User32.VK, HashSet<string>> _pressedKeys = [];

    /// <summary>
    /// 适配器已按下、尚未抬起的鼠标键，以及每个键仍处于按下状态的通道。
    /// </summary>
    private readonly Dictionary<string, HashSet<string>> _pressedMouseButtons = [];

    /// <summary>
    /// 保护按下状态集合。
    /// </summary>
    private readonly object _pressedLock = new();

    /// <inheritdoc />
    public async Task ExecuteAsync(
        AgentActionDto action,
        AgentCaptureArea area,
        string defaultChannel,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(action);
        var runtime = runtimeAccessor.Current
                      ?? throw AgentException.RuntimeUnavailable();
        if (runtime.IsStopping || !runtime.Window.IsAlive)
        {
            throw AgentException.RuntimeUnavailable("游戏运行环境正在停止或游戏已经退出。");
        }

        var channelName = AgentSafetyPolicy.ResolveChannel(action.Channel, defaultChannel);
        var type = action.Type.Trim().ToLowerInvariant();
        switch (type)
        {
            case "click":
                await ClickAsync(runtime, action, area, channelName, action.Clicks, cancellationToken).ConfigureAwait(false);
                break;
            case "doubleclick":
                await ClickAsync(runtime, action, area, channelName, 2, cancellationToken).ConfigureAwait(false);
                break;
            case "rightclick":
                await ClickAsync(runtime, action, area, channelName, 1, cancellationToken, "right").ConfigureAwait(false);
                break;
            case "movemouse":
                await MoveMouseAsync(runtime, action, area, channelName, cancellationToken).ConfigureAwait(false);
                break;
            case "movemouseby":
                await MoveMouseByAsync(runtime, action, channelName, cancellationToken).ConfigureAwait(false);
                break;
            case "drag":
                await DragAsync(runtime, action, area, channelName, cancellationToken).ConfigureAwait(false);
                break;
            case "scroll":
                await ScrollAsync(runtime, action, channelName, cancellationToken).ConfigureAwait(false);
                break;
            case "key":
                await KeyAsync(runtime, action, channelName, cancellationToken).ConfigureAwait(false);
                break;
            case "text":
                await TextAsync(runtime, action.Text!, channelName, cancellationToken).ConfigureAwait(false);
                break;
            case "activatewindow":
                runtime.Window.Activate();
                if (runtime.Window.RequiresForeground && !runtime.Window.IsForeground)
                    throw AgentException.RuntimeUnavailable("系统未将游戏窗口切换到前台。请从交互式终端重试。");
                break;
            default:
                throw AgentException.InvalidRequest($"输入控制器不支持动作类型：{action.Type}。");
        }
    }

    /// <inheritdoc />
    public void ReleasePressedInputs()
    {
        List<(User32.VK Key, string Channel)> keys;
        List<(string Button, string Channel)> buttons;
        lock (_pressedLock)
        {
            keys = _pressedKeys
                .SelectMany(item => item.Value.Select(channel => (item.Key, channel)))
                .ToList();
            buttons = _pressedMouseButtons
                .SelectMany(item => item.Value.Select(channel => (item.Key, channel)))
                .ToList();
            _pressedKeys.Clear();
            _pressedMouseButtons.Clear();
        }

        foreach (var (key, channel) in keys)
        {
            try
            {
                GetChannel(channel).Keyboard.KeyUp(key);
            }
            catch (Exception exception)
            {
                logger.LogDebug(exception, "释放智能体按下的按键 {Key} 失败", key);
            }
        }

        foreach (var (button, channel) in buttons)
        {
            try
            {
                ReleaseMouseButton(GetChannel(channel), button);
            }
            catch (Exception exception)
            {
                logger.LogDebug(exception, "释放智能体按下的鼠标键 {Button} 失败", button);
            }
        }
    }

    /// <summary>
    /// 执行点击。
    /// </summary>
    private async Task ClickAsync(
        GameRuntime runtime,
        AgentActionDto action,
        AgentCaptureArea area,
        string channelName,
        int clicks,
        CancellationToken cancellationToken,
        string? buttonOverride = null)
    {
        var channel = await GetReadyChannelAsync(runtime, channelName, cancellationToken).ConfigureAwait(false);
        var point = AgentCoordinateMapper.Resolve(action, GetLiveArea(runtime));
        channel.Mouse.MoveMouseTo(point.MouseNormalizedX, point.MouseNormalizedY);
        await Task.Delay(20, cancellationToken).ConfigureAwait(false);

        var button = (buttonOverride ?? action.Button ?? "left").Trim().ToLowerInvariant();
        for (var index = 0; index < Math.Clamp(clicks, 1, 3); index++)
        {
            if (index > 0)
            {
                await Task.Delay(60, cancellationToken).ConfigureAwait(false);
            }

            EnsureForegroundStillValid(runtime, channelName);
            PressMouseButton(channel, button, channelName);
            try
            {
                await Task.Delay(channelName == "background" ? 100 : 35, cancellationToken).ConfigureAwait(false);
            }
            finally
            {
                ReleaseMouseButton(channel, button);
                MarkMouseReleased(button, channelName);
            }
        }
    }

    /// <summary>
    /// 移动鼠标到目标位置。
    /// </summary>
    private async Task MoveMouseAsync(
        GameRuntime runtime,
        AgentActionDto action,
        AgentCaptureArea area,
        string channelName,
        CancellationToken cancellationToken)
    {
        var channel = await GetReadyChannelAsync(runtime, channelName, cancellationToken).ConfigureAwait(false);
        var point = AgentCoordinateMapper.Resolve(action, GetLiveArea(runtime));
        channel.Mouse.MoveMouseTo(point.MouseNormalizedX, point.MouseNormalizedY);
        await Task.Delay(20, cancellationToken).ConfigureAwait(false);
    }

    /// <summary>
    /// 相对移动鼠标（视角控制），仅前台通道。
    /// </summary>
    private async Task MoveMouseByAsync(
        GameRuntime runtime,
        AgentActionDto action,
        string channelName,
        CancellationToken cancellationToken)
    {
        if (channelName == "background")
        {
            throw AgentException.InvalidRequest("后台通道不支持相对移动。");
        }

        var channel = await GetReadyChannelAsync(runtime, channelName, cancellationToken).ConfigureAwait(false);
        EnsureForegroundStillValid(runtime, channelName);
        channel.Mouse.MoveMouseBy(action.DeltaX ?? 0, action.DeltaY ?? 0);
        await Task.Delay(20, cancellationToken).ConfigureAwait(false);
    }

    /// <summary>
    /// 拖拽，仅前台通道。
    /// </summary>
    private async Task DragAsync(
        GameRuntime runtime,
        AgentActionDto action,
        AgentCaptureArea area,
        string channelName,
        CancellationToken cancellationToken)
    {
        if (channelName == "background")
        {
            throw AgentException.InvalidRequest("后台通道不支持拖拽。");
        }

        var channel = await GetReadyChannelAsync(runtime, channelName, cancellationToken).ConfigureAwait(false);
        var start = AgentCoordinateMapper.Resolve(action, GetLiveArea(runtime));
        var end = AgentCoordinateMapper.Resolve(action.ToX!.Value, action.ToY!.Value, action.CoordinateSpace, GetLiveArea(runtime));
        var duration = Math.Clamp(action.DurationMs <= 0 ? 300 : action.DurationMs, 100, AgentLimits.MaxDragDurationMs);
        var steps = Math.Clamp(duration / 20, 4, 30);

        channel.Mouse.MoveMouseTo(start.MouseNormalizedX, start.MouseNormalizedY);
        await Task.Delay(20, cancellationToken).ConfigureAwait(false);
        EnsureForegroundStillValid(runtime, channelName);
        PressMouseButton(channel, "left", channelName);
        try
        {
            for (var step = 1; step <= steps; step++)
            {
                var ratio = step / (double)steps;
                var x = start.MouseNormalizedX + (end.MouseNormalizedX - start.MouseNormalizedX) * ratio;
                var y = start.MouseNormalizedY + (end.MouseNormalizedY - start.MouseNormalizedY) * ratio;
                channel.Mouse.MoveMouseTo(x, y);
                await Task.Delay(Math.Max(1, duration / steps), cancellationToken).ConfigureAwait(false);
            }
        }
        finally
        {
            ReleaseMouseButton(channel, "left");
            MarkMouseReleased("left", channelName);
        }
    }

    /// <summary>
    /// 滚轮，仅前台通道。
    /// </summary>
    private async Task ScrollAsync(
        GameRuntime runtime,
        AgentActionDto action,
        string channelName,
        CancellationToken cancellationToken)
    {
        if (channelName == "background")
        {
            throw AgentException.InvalidRequest("后台通道不支持滚轮。");
        }

        var channel = await GetReadyChannelAsync(runtime, channelName, cancellationToken).ConfigureAwait(false);
        // 先把光标移到游戏画面中心，避免“悬停时滚动非活动窗口”把滚轮送到其他应用。
        var area = GetLiveArea(runtime);
        var centerX = (area.X + area.Width / 2d) * 65535d / PrimaryScreen.WorkingArea.Width;
        var centerY = (area.Y + area.Height / 2d) * 65535d / PrimaryScreen.WorkingArea.Height;
        channel.Mouse.MoveMouseTo(centerX, centerY);
        await Task.Delay(20, cancellationToken).ConfigureAwait(false);
        EnsureForegroundStillValid(runtime, channelName);
        channel.Mouse.VerticalScroll(action.ScrollClicks);
        await Task.Delay(20, cancellationToken).ConfigureAwait(false);
    }

    /// <summary>
    /// 按键。
    /// </summary>
    private async Task KeyAsync(
        GameRuntime runtime,
        AgentActionDto action,
        string channelName,
        CancellationToken cancellationToken)
    {
        if (!AgentKeyMap.TryParse(action.Key, out var key, out var error))
        {
            throw AgentException.InvalidRequest(error ?? "键名不合法。");
        }

        var channel = await GetReadyChannelAsync(runtime, channelName, cancellationToken).ConfigureAwait(false);
        var keyAction = string.IsNullOrWhiteSpace(action.KeyAction)
            ? "press"
            : action.KeyAction.Trim().ToLowerInvariant();
        // 抬起动作不应被危险组合检查拦截，否则会妨碍释放已经按下的键。
        if (keyAction != "up")
        {
            EnsureNoDangerousShortcut(key);
            EnsureForegroundStillValid(runtime, channelName);
        }

        switch (keyAction)
        {
            case "down":
                channel.Keyboard.KeyDown(key);
                MarkKeyPressed(key, channelName);
                break;
            case "up":
                channel.Keyboard.KeyUp(key);
                MarkKeyReleased(key, channelName);
                break;
            default:
                if (action.DurationMs > 0)
                {
                    channel.Keyboard.KeyDown(key);
                    MarkKeyPressed(key, channelName);
                    try
                    {
                        await Task.Delay(action.DurationMs, cancellationToken).ConfigureAwait(false);
                    }
                    finally
                    {
                        channel.Keyboard.KeyUp(key);
                        MarkKeyReleased(key, channelName);
                    }
                }
                else
                {
                    channel.Keyboard.KeyPress(key);
                }

                break;
        }

        await Task.Delay(20, cancellationToken).ConfigureAwait(false);
    }

    /// <summary>
    /// 文本输入，仅前台 Win32 通道。
    /// </summary>
    private async Task TextAsync(
        GameRuntime runtime,
        string text,
        string channelName,
        CancellationToken cancellationToken)
    {
        if (channelName == "background")
        {
            throw AgentException.InvalidRequest("后台通道不支持文本输入。");
        }

        if (InputHub.Backend.Kind != InputBackendKind.Win32)
        {
            throw new AgentException(
                "TEXT_NOT_SUPPORTED",
                "当前输入后端不支持文本输入，请使用键盘动作代替。",
                HttpStatusCode.NotImplemented);
        }

        await GetReadyChannelAsync(runtime, channelName, cancellationToken).ConfigureAwait(false);
        EnsureForegroundStillValid(runtime, channelName);
        // KEYEVENTF_UNICODE 是模拟键盘输入，不经过剪贴板；游戏使用 Raw Input 时可能忽略，属于尽力而为。
        Simulation.SendInput.Keyboard.TextEntry(text);
        await Task.Delay(20, cancellationToken).ConfigureAwait(false);
    }

    /// <summary>
    /// 阻止会切换窗口或打开系统界面的常见组合键。适配器只服务于游戏，不应操作用户桌面。
    /// </summary>
    /// <param name="key">即将按下的键。</param>
    private void EnsureNoDangerousShortcut(User32.VK key)
    {
        bool altDown;
        bool controlDown;
        lock (_pressedLock)
        {
            altDown = _pressedKeys.ContainsKey(User32.VK.VK_MENU)
                      || _pressedKeys.ContainsKey(User32.VK.VK_LMENU)
                      || _pressedKeys.ContainsKey(User32.VK.VK_RMENU)
                      || Simulation.IsKeyDown(User32.VK.VK_MENU)
                      || Simulation.IsKeyDown(User32.VK.VK_LMENU)
                      || Simulation.IsKeyDown(User32.VK.VK_RMENU);
            controlDown = _pressedKeys.ContainsKey(User32.VK.VK_CONTROL)
                          || _pressedKeys.ContainsKey(User32.VK.VK_LCONTROL)
                          || _pressedKeys.ContainsKey(User32.VK.VK_RCONTROL)
                          || Simulation.IsKeyDown(User32.VK.VK_CONTROL)
                          || Simulation.IsKeyDown(User32.VK.VK_LCONTROL)
                          || Simulation.IsKeyDown(User32.VK.VK_RCONTROL);
        }

        // Alt+Tab 切换前台窗口；Alt+Space/Alt+Enter 打开窗口系统菜单或切换全屏；
        // Ctrl/Esc 或 Alt/Esc 打开系统界面；Ctrl+Shift+Esc 打开任务管理器。
        if (altDown && key is User32.VK.VK_TAB or User32.VK.VK_SPACE or User32.VK.VK_RETURN)
        {
            throw new AgentException(
                "DANGEROUS_SHORTCUT_BLOCKED",
                "已阻止会切换窗口或打开窗口系统菜单的组合键。",
                HttpStatusCode.BadRequest);
        }

        if (key == User32.VK.VK_ESCAPE && (altDown || controlDown))
        {
            throw new AgentException(
                "DANGEROUS_SHORTCUT_BLOCKED",
                "已阻止会打开系统界面的组合键。",
                HttpStatusCode.BadRequest);
        }
    }

    /// <summary>
    /// 获取可用的输入通道，并在前台通道下等待游戏窗口进入前台。
    /// </summary>
    private async Task<IInputChannel> GetReadyChannelAsync(
        GameRuntime runtime,
        string channelName,
        CancellationToken cancellationToken)
    {
        if (channelName == "background")
        {
            return InputHub.Background;
        }

        if (runtime.Window.IsMinimized)
        {
            runtime.Window.Activate();
        }

        if (runtime.Window.RequiresForeground && !runtime.Window.IsForeground)
        {
            runtime.Window.Activate();
            for (var attempt = 0; attempt < 20 && !runtime.Window.IsForeground; attempt++)
            {
                await Task.Delay(50, cancellationToken).ConfigureAwait(false);
            }

            if (runtime.Window.RequiresForeground && !runtime.Window.IsForeground)
            {
                throw new AgentException(
                    "GAME_NOT_FOREGROUND",
                    "游戏窗口未能进入前台，已拒绝发送前台输入，避免误操作其他窗口。",
                    HttpStatusCode.Conflict);
            }
        }

        return InputHub.Foreground;
    }

    /// <summary>
    /// 读取运行环境当前的实时画面区域，避免使用动作开始前可能已过期的快照。
    /// </summary>
    private static AgentCaptureArea GetLiveArea(GameRuntime runtime)
    {
        var viewport = runtime.Window.Viewport;
        return new AgentCaptureArea(
            viewport.ScreenRect.X,
            viewport.ScreenRect.Y,
            viewport.ScreenRect.Width,
            viewport.ScreenRect.Height,
            viewport.DpiScale);
    }

    /// <summary>
    /// 在真正发送输入前再次确认游戏仍在前台，缩小检查与发送之间的焦点切换窗口。
    /// </summary>
    private static void EnsureForegroundStillValid(GameRuntime runtime, string channelName)
    {
        if (channelName == "foreground"
            && runtime.Window.RequiresForeground
            && !runtime.Window.IsForeground)
        {
            throw new AgentException(
                "GAME_NOT_FOREGROUND",
                "发送输入前游戏窗口已失去前台，已拒绝操作。",
                HttpStatusCode.Conflict);
        }
    }

    /// <summary>
    /// 依据通道名获取当前输入通道。
    /// </summary>
    private static IInputChannel GetChannel(string channelName)
    {
        return channelName == "background" ? InputHub.Background : InputHub.Foreground;
    }

    /// <summary>
    /// 按下鼠标键。
    /// </summary>
    private void PressMouseButton(IInputChannel channel, string button, string channelName)
    {
        switch (button)
        {
            case "right":
                channel.Mouse.RightButtonDown();
                break;
            case "middle":
                channel.Mouse.MiddleButtonDown();
                break;
            default:
                channel.Mouse.LeftButtonDown();
                break;
        }

        lock (_pressedLock)
        {
            if (!_pressedMouseButtons.TryGetValue(button, out var channels))
            {
                channels = new HashSet<string>(StringComparer.Ordinal);
                _pressedMouseButtons[button] = channels;
            }

            channels.Add(channelName);
        }
    }

    /// <summary>
    /// 抬起鼠标键。
    /// </summary>
    private static void ReleaseMouseButton(IInputChannel channel, string button)
    {
        switch (button)
        {
            case "right":
                channel.Mouse.RightButtonUp();
                break;
            case "middle":
                channel.Mouse.MiddleButtonUp();
                break;
            default:
                channel.Mouse.LeftButtonUp();
                break;
        }
    }

    /// <summary>
    /// 记录按下的键盘键。
    /// </summary>
    private void MarkKeyPressed(User32.VK key, string channelName)
    {
        lock (_pressedLock)
        {
            if (!_pressedKeys.TryGetValue(key, out var channels))
            {
                channels = new HashSet<string>(StringComparer.Ordinal);
                _pressedKeys[key] = channels;
            }

            channels.Add(channelName);
        }
    }

    /// <summary>
    /// 记录指定通道上的键盘键已抬起。
    /// </summary>
    private void MarkKeyReleased(User32.VK key, string channelName)
    {
        lock (_pressedLock)
        {
            if (_pressedKeys.TryGetValue(key, out var channels))
            {
                channels.Remove(channelName);
                if (channels.Count == 0)
                {
                    _pressedKeys.Remove(key);
                }
            }
        }
    }

    /// <summary>
    /// 记录抬起的鼠标键。
    /// </summary>
    private void MarkMouseReleased(string button, string channelName)
    {
        lock (_pressedLock)
        {
            if (_pressedMouseButtons.TryGetValue(button, out var channels))
            {
                channels.Remove(channelName);
                if (channels.Count == 0)
                {
                    _pressedMouseButtons.Remove(button);
                }
            }
        }
    }
}
