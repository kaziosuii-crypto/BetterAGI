using System;
using System.Collections.Generic;
using Vanara.PInvoke;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Input;

/// <summary>
/// 把智能体给出的键名转换为 Windows 虚拟键码，并限制在游戏常用、低风险的白名单内。
/// </summary>
public static class AgentKeyMap
{
    /// <summary>
    /// 常用别名。
    /// </summary>
    private static readonly Dictionary<string, string> Aliases = new(StringComparer.OrdinalIgnoreCase)
    {
        ["CTRL"] = "VK_CONTROL",
        ["CONTROL"] = "VK_CONTROL",
        ["ALT"] = "VK_MENU",
        ["SHIFT"] = "VK_SHIFT",
        ["ESC"] = "VK_ESCAPE",
        ["ESCAPE"] = "VK_ESCAPE",
        ["ENTER"] = "VK_RETURN",
        ["RETURN"] = "VK_RETURN",
        ["SPACE"] = "VK_SPACE",
        ["TAB"] = "VK_TAB",
        ["BACKSPACE"] = "VK_BACK",
        ["UP"] = "VK_UP",
        ["DOWN"] = "VK_DOWN",
        ["LEFT"] = "VK_LEFT",
        ["RIGHT"] = "VK_RIGHT",
        ["PAGEUP"] = "VK_PRIOR",
        ["PAGEDOWN"] = "VK_NEXT",
        ["DEL"] = "VK_DELETE",
        ["DELETE"] = "VK_DELETE",
        ["INS"] = "VK_INSERT",
        ["INSERT"] = "VK_INSERT",
        ["HOME"] = "VK_HOME",
        ["END"] = "VK_END",
        ["PGUP"] = "VK_PRIOR",
        ["PGDN"] = "VK_NEXT"
    };

    /// <summary>
    /// 允许的虚拟键集合。只包含游戏常用键，排除 Win、截图、F4、Apps 等可能影响系统的键。
    /// </summary>
    private static readonly HashSet<User32.VK> AllowedKeys = BuildAllowedKeys();

    /// <summary>
    /// 尝试解析键名。
    /// </summary>
    /// <param name="key">调用方给出的键名。</param>
    /// <param name="virtualKey">解析后的虚拟键。</param>
    /// <param name="error">解析失败时的说明。</param>
    /// <returns>是否解析成功且位于白名单内。</returns>
    public static bool TryParse(string? key, out User32.VK virtualKey, out string? error)
    {
        virtualKey = default;
        error = null;
        if (string.IsNullOrWhiteSpace(key))
        {
            error = "键名不能为空。";
            return false;
        }

        var normalized = key.Trim().ToUpperInvariant();
        if (Aliases.TryGetValue(normalized, out var alias))
        {
            normalized = alias;
        }
        else if (!normalized.StartsWith("VK_", StringComparison.Ordinal))
        {
            normalized = "VK_" + normalized;
        }

        if (!Enum.TryParse<User32.VK>(normalized, true, out virtualKey))
        {
            error = $"无法识别的键名：{key}。请使用 W、A、S、D、E、Q、Space、Shift、Ctrl、Alt、F1 这类名称。";
            return false;
        }

        if (!AllowedKeys.Contains(virtualKey))
        {
            error = $"键 {key} 不在智能体允许的按键白名单内。";
            return false;
        }

        return true;
    }

    /// <summary>
    /// 构建允许的虚拟键集合。
    /// </summary>
    private static HashSet<User32.VK> BuildAllowedKeys()
    {
        var keys = new HashSet<User32.VK>();
        AddRange(keys, (int)User32.VK.VK_A, (int)User32.VK.VK_Z);
        AddRange(keys, (int)User32.VK.VK_0, (int)User32.VK.VK_9);
        AddRange(keys, (int)User32.VK.VK_F1, (int)User32.VK.VK_F12);
        // F4 与 Alt 组合可能关闭前台窗口；F11 是 BetterGI 默认的全局启停热键。
        keys.Remove(User32.VK.VK_F4);
        keys.Remove(User32.VK.VK_F11);
        AddRange(keys, (int)User32.VK.VK_NUMPAD0, (int)User32.VK.VK_NUMPAD9);

        keys.Add(User32.VK.VK_SPACE);
        keys.Add(User32.VK.VK_SHIFT);
        keys.Add(User32.VK.VK_LSHIFT);
        keys.Add(User32.VK.VK_RSHIFT);
        keys.Add(User32.VK.VK_CONTROL);
        keys.Add(User32.VK.VK_LCONTROL);
        keys.Add(User32.VK.VK_RCONTROL);
        keys.Add(User32.VK.VK_MENU);
        keys.Add(User32.VK.VK_LMENU);
        keys.Add(User32.VK.VK_RMENU);
        keys.Add(User32.VK.VK_ESCAPE);
        keys.Add(User32.VK.VK_TAB);
        keys.Add(User32.VK.VK_RETURN);
        keys.Add(User32.VK.VK_BACK);
        keys.Add(User32.VK.VK_UP);
        keys.Add(User32.VK.VK_DOWN);
        keys.Add(User32.VK.VK_LEFT);
        keys.Add(User32.VK.VK_RIGHT);
        keys.Add(User32.VK.VK_HOME);
        keys.Add(User32.VK.VK_END);
        keys.Add(User32.VK.VK_PRIOR);
        keys.Add(User32.VK.VK_NEXT);
        keys.Add(User32.VK.VK_INSERT);
        keys.Add(User32.VK.VK_DELETE);
        keys.Add(User32.VK.VK_OEM_1);
        keys.Add(User32.VK.VK_OEM_2);
        keys.Add(User32.VK.VK_OEM_3);
        keys.Add(User32.VK.VK_OEM_4);
        keys.Add(User32.VK.VK_OEM_5);
        keys.Add(User32.VK.VK_OEM_6);
        keys.Add(User32.VK.VK_OEM_7);
        keys.Add(User32.VK.VK_OEM_PLUS);
        keys.Add(User32.VK.VK_OEM_MINUS);
        keys.Add(User32.VK.VK_OEM_COMMA);
        keys.Add(User32.VK.VK_OEM_PERIOD);
        return keys;
    }

    /// <summary>
    /// 把一段连续的虚拟键码加入集合。
    /// </summary>
    /// <param name="keys">目标集合。</param>
    /// <param name="first">起始虚拟键码。</param>
    /// <param name="last">结束虚拟键码。</param>
    private static void AddRange(HashSet<User32.VK> keys, int first, int last)
    {
        for (var code = first; code <= last; code++)
        {
            keys.Add((User32.VK)code);
        }
    }
}
