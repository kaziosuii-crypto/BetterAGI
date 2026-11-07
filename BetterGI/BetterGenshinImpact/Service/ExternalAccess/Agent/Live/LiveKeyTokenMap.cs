using System;
using System.Collections.Generic;
using Vanara.PInvoke;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Live;

/// <summary>
/// Lumine 动作 token 到 BetterGI 输入动作的映射。
/// <para>
/// token 与官方 KeyRecorder 一致：A-Z、zero-nine、One-Twelve、Esc/Tab/Caps/Shift/Ctrl/Alt/Space，
/// 以及鼠标键 LB/RB/MB/XB1/XB2。F4、F11、Win、截图键等高风险键不放行。
/// </para>
/// </summary>
public static class LiveKeyTokenMap
{
    /// <summary>鼠标键 token。</summary>
    private static readonly HashSet<string> MouseTokens = new(StringComparer.OrdinalIgnoreCase)
    {
        "LB", "RB", "MB", "XB1", "XB2"
    };

    /// <summary>语义键 token。</summary>
    private static readonly Dictionary<string, User32.VK> NamedKeys = new(StringComparer.OrdinalIgnoreCase)
    {
        ["Esc"] = User32.VK.VK_ESCAPE,
        ["Tab"] = User32.VK.VK_TAB,
        ["Caps"] = User32.VK.VK_CAPITAL,
        ["Shift"] = User32.VK.VK_SHIFT,
        ["Ctrl"] = User32.VK.VK_CONTROL,
        ["Alt"] = User32.VK.VK_MENU,
        ["Space"] = User32.VK.VK_SPACE,
        ["Enter"] = User32.VK.VK_RETURN
    };

    /// <summary>数字 token。</summary>
    private static readonly string[] DigitNames =
    [
        "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"
    ];

    /// <summary>功能键 token。</summary>
    private static readonly string[] FunctionNames =
    [
        "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve"
    ];

    /// <summary>
    /// 尝试把 token 映射为键盘键或鼠标键。
    /// </summary>
    /// <param name="token">Lumine 键 token。</param>
    /// <param name="virtualKey">键盘虚拟键；鼠标键时为空。</param>
    /// <param name="mouseButton">鼠标键；键盘键时为空。</param>
    /// <returns>是否是允许的 token。</returns>
    public static bool TryMap(string? token, out User32.VK? virtualKey, out string? mouseButton)
    {
        virtualKey = null;
        mouseButton = null;
        if (string.IsNullOrWhiteSpace(token))
        {
            return false;
        }

        var value = token.Trim();
        if (MouseTokens.Contains(value))
        {
            mouseButton = value.ToUpperInvariant();
            return true;
        }

        if (value.Length == 1 && char.IsAsciiLetter(value[0]))
        {
            var letter = value.ToUpperInvariant()[0];
            virtualKey = (User32.VK)((int)User32.VK.VK_A + (letter - 'A'));
            return true;
        }

        for (var index = 0; index < DigitNames.Length; index++)
        {
            // 数字 token 为小写（one），功能键 token 为首字母大写（One）；
            // 必须区分大小写，否则 "One" 会被误映射成数字 1。
            if (value.Equals(DigitNames[index], StringComparison.Ordinal))
            {
                virtualKey = (User32.VK)((int)User32.VK.VK_0 + index);
                return true;
            }
        }

        for (var index = 0; index < FunctionNames.Length; index++)
        {
            if (!value.Equals(FunctionNames[index], StringComparison.Ordinal))
            {
                continue;
            }

            var fKey = (User32.VK)((int)User32.VK.VK_F1 + index);
            // 与智能体动作白名单保持一致：F4 可能关闭窗口，F11 是 BetterGI 默认全局热键。
            if (fKey is User32.VK.VK_F4 or User32.VK.VK_F11)
            {
                return false;
            }

            virtualKey = fKey;
            return true;
        }

        if (NamedKeys.TryGetValue(value, out var named))
        {
            virtualKey = named;
            return true;
        }

        return false;
    }
}
