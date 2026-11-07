using BetterGenshinImpact.Service.ExternalAccess.Agent.Input;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Models;
using System;
using System.Collections.Generic;
using System.Linq;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Safety;

/// <summary>
/// 智能体动作的白名单与参数校验。所有动作在执行前统一经过该策略，
/// 非法动作不会产生任何部分输入。
/// </summary>
public static class AgentSafetyPolicy
{
    /// <summary>
    /// 允许的动作类型。
    /// </summary>
    private static readonly HashSet<string> AllowedActionTypes = new(StringComparer.OrdinalIgnoreCase)
    {
        "click",
        "doubleClick",
        "rightClick",
        "moveMouse",
        "moveMouseBy",
        "drag",
        "scroll",
        "key",
        "text",
        "wait",
        "activateWindow",
        "screenshot",
        "observe",
        "clickAtText"
    };

    /// <summary>
    /// 校验整个动作序列。任何一项不合法都会在开始执行前抛出异常。
    /// </summary>
    /// <param name="request">动作序列请求。</param>
    public static void ValidateSequence(AgentActionRequest request)
    {
        ArgumentNullException.ThrowIfNull(request);
        ValidateChannel(request.Channel);
        if (request.Actions is null || request.Actions.Count == 0)
        {
            throw AgentException.InvalidRequest("动作序列不能为空。");
        }

        if (request.Actions.Count > AgentLimits.MaxActionsPerRequest)
        {
            throw AgentException.InvalidRequest(
                $"单次请求最多允许 {AgentLimits.MaxActionsPerRequest} 个动作。",
                $"实际为 {request.Actions.Count} 个。");
        }

        for (var index = 0; index < request.Actions.Count; index++)
        {
            var action = request.Actions[index];
            if (action is null)
            {
                throw AgentException.InvalidRequest($"第 {index} 个动作不能为空。");
            }

            try
            {
                ValidateAction(action, request.Channel);
            }
            catch (AgentException exception)
            {
                throw AgentException.InvalidRequest(
                    $"第 {index} 个动作不合法：{exception.Message}",
                    exception.Details);
            }
        }
    }

    /// <summary>
    /// 校验单个动作。
    /// </summary>
    /// <param name="action">动作。</param>
    /// <param name="defaultChannel">请求级默认通道。</param>
    public static void ValidateAction(AgentActionDto action, string? defaultChannel)
    {
        ArgumentNullException.ThrowIfNull(action);
        var type = action.Type?.Trim() ?? string.Empty;
        if (!AllowedActionTypes.Contains(type))
        {
            throw AgentException.InvalidRequest(
                $"不支持的动作类型：{action.Type}。",
                "支持：" + string.Join(", ", AllowedActionTypes.OrderBy(item => item)));
        }

        var channel = ResolveChannel(action.Channel, defaultChannel);
        switch (type.ToLowerInvariant())
        {
            case "click":
            case "doubleclick":
            case "rightclick":
            case "movemouse":
                RequirePoint(action);
                ValidateCoordinateSpace(action);
                ValidateButton(action.Button, channel);
                break;
            case "movemouseby":
                if (action.DeltaX is null && action.DeltaY is null)
                {
                    throw AgentException.InvalidRequest("moveMouseBy 需要 DeltaX 或 DeltaY。");
                }

                if (channel == "background")
                {
                    throw AgentException.InvalidRequest("后台通道不支持相对移动（视角控制），请使用 foreground。");
                }

                break;
            case "drag":
                RequirePoint(action);
                ValidateCoordinateSpace(action);
                if (action.ToX is null || action.ToY is null)
                {
                    throw AgentException.InvalidRequest("drag 需要 ToX 和 ToY。");
                }

                if (action.DurationMs is < 0 or > AgentLimits.MaxDragDurationMs)
                {
                    throw AgentException.InvalidRequest(
                        $"drag 持续时间必须在 0～{AgentLimits.MaxDragDurationMs} 毫秒之间。");
                }

                if (channel == "background")
                {
                    throw AgentException.InvalidRequest("后台通道不支持拖拽，请使用 foreground。");
                }

                break;
            case "scroll":
                if (action.ScrollClicks == 0 || Math.Abs((long)action.ScrollClicks) > AgentLimits.MaxScrollClicks)
                {
                    throw AgentException.InvalidRequest(
                        $"scroll 的 ScrollClicks 绝对值必须在 1～{AgentLimits.MaxScrollClicks} 之间。");
                }

                if (channel == "background")
                {
                    throw AgentException.InvalidRequest("后台通道不支持滚轮，请使用 foreground。");
                }

                break;
            case "key":
                if (!AgentKeyMap.TryParse(action.Key, out _, out var keyError))
                {
                    throw AgentException.InvalidRequest(keyError ?? "键名不合法。");
                }

                var keyAction = string.IsNullOrWhiteSpace(action.KeyAction) ? "press" : action.KeyAction.Trim().ToLowerInvariant();
                if (keyAction is not ("press" or "down" or "up"))
                {
                    throw AgentException.InvalidRequest("KeyAction 只能是 press、down 或 up。");
                }

                if (action.DurationMs is < 0 or > AgentLimits.MaxWaitMs)
                {
                    throw AgentException.InvalidRequest($"按键持续时间必须在 0～{AgentLimits.MaxWaitMs} 毫秒之间。");
                }

                break;
            case "text":
                if (string.IsNullOrEmpty(action.Text))
                {
                    throw AgentException.InvalidRequest("text 动作的 Text 不能为空。");
                }

                if (action.Text.Length > AgentLimits.MaxTextLength)
                {
                    throw AgentException.InvalidRequest($"文本长度不能超过 {AgentLimits.MaxTextLength} 个字符。");
                }

                if (action.Text.Any(char.IsControl))
                {
                    throw AgentException.InvalidRequest("文本不能包含控制字符。");
                }

                if (channel == "background")
                {
                    throw AgentException.InvalidRequest("后台通道不支持文本输入，请使用 foreground。");
                }

                break;
            case "wait":
                if (action.WaitMs is < 0 or > AgentLimits.MaxWaitMs)
                {
                    throw AgentException.InvalidRequest($"wait 的 WaitMs 必须在 0～{AgentLimits.MaxWaitMs} 毫秒之间。");
                }

                break;
            case "activatewindow":
                break;
            case "screenshot":
            case "observe":
                if (action.Name is { Length: > 64 })
                {
                    throw AgentException.InvalidRequest("Name 不能超过 64 个字符。");
                }

                break;
            case "clickattext":
                if (string.IsNullOrWhiteSpace(action.Text))
                {
                    throw AgentException.InvalidRequest("clickAtText 需要 Text。");
                }

                if (action.Text.Length > 100)
                {
                    throw AgentException.InvalidRequest("clickAtText 的 Text 不能超过 100 个字符。");
                }

                ValidateButton(action.Button, channel);
                break;
        }

        if ((action.Clicks is < 1 or > 3) && type.Equals("click", StringComparison.OrdinalIgnoreCase))
        {
            throw AgentException.InvalidRequest("click 的 Clicks 必须在 1～3 之间。");
        }
    }

    /// <summary>
    /// 解析并校验输入通道。
    /// </summary>
    /// <param name="channel">调用方给出的通道。</param>
    /// <param name="defaultChannel">请求级默认通道。</param>
    /// <returns>规范化后的通道名。</returns>
    public static string ResolveChannel(string? channel, string? defaultChannel)
    {
        var value = string.IsNullOrWhiteSpace(channel) ? defaultChannel : channel;
        value = string.IsNullOrWhiteSpace(value) ? "foreground" : value.Trim().ToLowerInvariant();
        ValidateChannel(value);
        return value;
    }

    /// <summary>
    /// 判断动作是否会产生键鼠输入。
    /// </summary>
    /// <param name="type">动作类型。</param>
    /// <returns>是否属于输入动作。</returns>
    public static bool IsInputAction(string? type)
    {
        return type?.Trim().ToLowerInvariant() is
            "click" or "doubleclick" or "rightclick" or "movemouse" or "movemouseby" or
            "drag" or "scroll" or "key" or "text" or "activatewindow" or "clickattext";
    }

    /// <summary>
    /// 校验通道名。
    /// </summary>
    private static void ValidateChannel(string? channel)
    {
        if (string.IsNullOrWhiteSpace(channel))
        {
            return;
        }

        var value = channel.Trim().ToLowerInvariant();
        if (value is not ("foreground" or "background"))
        {
            throw AgentException.InvalidRequest("Channel 只能是 foreground 或 background。", channel);
        }
    }

    /// <summary>
    /// 校验坐标空间名称，并对归一化坐标做范围检查。
    /// capture/desktop 的像素范围依赖当前游戏画面，由能力服务在执行前用实时区域预检。
    /// </summary>
    private static void ValidateCoordinateSpace(AgentActionDto action)
    {
        var space = string.IsNullOrWhiteSpace(action.CoordinateSpace)
            ? "normalized"
            : action.CoordinateSpace.Trim().ToLowerInvariant();
        if (space is not ("normalized" or "capture" or "desktop"))
        {
            throw AgentException.InvalidRequest("CoordinateSpace 只能是 normalized、capture 或 desktop。");
        }

        if (space != "normalized")
        {
            return;
        }

        if (action.X is < 0 or > 1 || action.Y is < 0 or > 1)
        {
            throw AgentException.InvalidRequest("归一化坐标必须位于 0～1 之间。");
        }

        if (string.Equals(action.Type, "drag", StringComparison.OrdinalIgnoreCase)
            && (action.ToX is < 0 or > 1 || action.ToY is < 0 or > 1))
        {
            throw AgentException.InvalidRequest("drag 的归一化终点坐标必须位于 0～1 之间。");
        }
    }

    /// <summary>
    /// 校验鼠标键。
    /// </summary>
    private static void ValidateButton(string? button, string channel)
    {
        var value = string.IsNullOrWhiteSpace(button) ? "left" : button.Trim().ToLowerInvariant();
        if (value is not ("left" or "right" or "middle"))
        {
            throw AgentException.InvalidRequest("Button 只能是 left、right 或 middle。");
        }

        if (channel == "background" && value == "middle")
        {
            throw AgentException.InvalidRequest("后台通道不支持鼠标中键，请使用 foreground。");
        }
    }

    /// <summary>
    /// 校验动作是否包含目标坐标。
    /// </summary>
    private static void RequirePoint(AgentActionDto action)
    {
        if (action.X is null || action.Y is null)
        {
            throw AgentException.InvalidRequest($"{action.Type} 需要 X 和 Y 坐标。");
        }
    }
}
