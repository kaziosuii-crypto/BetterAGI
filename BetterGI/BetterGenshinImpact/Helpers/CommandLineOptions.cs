using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using BetterGenshinImpact.Service.Instance;

namespace BetterGenshinImpact.Helpers;

/// <summary>
/// 命令行参数统一解析，启动时解析一次，各处查询解析结果。
/// </summary>
public class CommandLineOptions
{
    public const string InstanceArgument = "--instance";
    public const string InstanceNameArgument = "--instance-name";
    public const string RestartFromProcessIdArgument = "--restart-from-pid";
    /// <summary>不创建 BetterGI 图形界面的宿主模式。</summary>
    public const string HeadlessArgument = "--headless";
    /// <summary>无界面宿主的独立配置路径。</summary>
    public const string HeadlessConfigArgument = "--headless-config";
    /// <summary>无界面宿主的凭据文件路径，不接受命令行明文令牌。</summary>
    public const string HeadlessTokenFileArgument = "--headless-token-file";

    private static CommandLineOptions? _instance;

    public static CommandLineOptions Instance => _instance ??= Parse(Environment.GetCommandLineArgs());

    public CommandLineAction Action { get; }
    /// <summary>只运行命令行控制的无界面宿主。</summary>
    public bool IsHeadless { get; }
    /// <summary>无界面宿主专用的绝对配置文件路径。</summary>
    public string? HeadlessConfigPath { get; }
    /// <summary>无界面宿主使用的凭据文件绝对路径。</summary>
    public string? HeadlessTokenFile { get; }
    /// <summary>命令行指定运行的已保存计划 ID，不接受路径或任意 JSON。</summary>
    public string? PuloniaPlanId { get; }
    /// <summary>是否应交给 Pulonia 内核，避免旧启动流程触碰游戏。</summary>
    public bool IsPuloniaAction => Action is CommandLineAction.PuloniaRun;

    /// <summary>
    /// 当前 BetterGI 的实例类型。
    /// </summary>
    public BetterGiInstanceType InstanceType { get; }

    /// <summary>
    /// 是否通过命令行明确指定了只能作为客户端运行的实例类型。
    /// </summary>
    public bool HasExplicitInstanceType { get; }

    /// <summary>
    /// 实例名（--instance-name），只对网页版实例有意义，未校验。
    /// </summary>
    public string? InstanceName { get; }

    /// <summary>
    /// 应用重启时被替换的旧进程 ID。
    /// </summary>
    public int? RestartFromProcessId { get; }

    /// <summary>
    /// startOneDragon 时可选的配置名称（第 3 个参数）
    /// </summary>
    public string? OneDragonConfigName { get; }

    /// <summary>
    /// --startGroups 时传入的组名列表（第 3 个参数起）
    /// </summary>
    public string[] GroupNames { get; } = [];

    /// <summary>
    /// 是否有命令行任务参数（startOneDragon / --startGroups / start）
    /// </summary>
    public bool HasTaskArgs => Action != CommandLineAction.None;

    /// <summary>
    /// 是否是需要任务自行处理游戏启动的命令
    /// （一条龙、配置组由各自流程中的 TaskRunner 按需启动游戏）
    /// </summary>
    public bool ShouldDeferGameStart => Action is CommandLineAction.StartOneDragon
        or CommandLineAction.StartGroups or CommandLineAction.PuloniaRun;

    private CommandLineOptions(
        CommandLineAction action,
        string? oneDragonConfigName = null,
        string[]? groupNames = null,
        BetterGiInstanceType instanceType = BetterGiInstanceType.Primary,
        bool hasExplicitInstanceType = false,
        int? restartFromProcessId = null,
        string? instanceName = null, string? puloniaPlanId = null,
        bool isHeadless = false, string? headlessConfigPath = null, string? headlessTokenFile = null)
    {
        Action = action;
        OneDragonConfigName = oneDragonConfigName;
        GroupNames = groupNames ?? [];
        InstanceType = instanceType;
        HasExplicitInstanceType = hasExplicitInstanceType;
        RestartFromProcessId = restartFromProcessId;
        InstanceName = instanceName;
        PuloniaPlanId = puloniaPlanId;
        IsHeadless = isHeadless;
        HeadlessConfigPath = headlessConfigPath;
        HeadlessTokenFile = headlessTokenFile;
    }

    internal static CommandLineOptions Parse(string[] args)
    {
        var launchArgs = args.Skip(1).Select(x => x.Trim()).ToArray();
        var instanceType = BetterGiInstanceType.Primary;
        var hasExplicitInstanceType = false;
        int? restartFromProcessId = null;
        string? instanceName = null;
        bool headless = false;
        bool instanceArgumentSeen = false;
        string? headlessConfigPath = null;
        string? headlessTokenFile = null;
        var commandArgs = new List<string>();

        for (var index = 0; index < launchArgs.Length; index++)
        {
            var argument = launchArgs[index];
            if (argument.Equals(HeadlessArgument, StringComparison.OrdinalIgnoreCase))
            {
                if (headless) throw new ArgumentException("--headless 不能重复。");
                headless = true;
                continue;
            }
            if (argument.Equals(HeadlessConfigArgument, StringComparison.OrdinalIgnoreCase))
            {
                if (headlessConfigPath is not null || !TryReadNext(launchArgs, ref index, out var path)
                    || !Path.IsPathFullyQualified(path))
                    throw new ArgumentException("--headless-config 需要唯一的绝对文件路径。");
                headlessConfigPath = path;
                continue;
            }
            if (argument.Equals(HeadlessTokenFileArgument, StringComparison.OrdinalIgnoreCase))
            {
                if (headlessTokenFile is not null || !TryReadNext(launchArgs, ref index, out var path)
                    || !Path.IsPathFullyQualified(path))
                    throw new ArgumentException("--headless-token-file 需要唯一的绝对文件路径。");
                headlessTokenFile = path;
                continue;
            }
            if (argument.Equals(InstanceArgument, StringComparison.OrdinalIgnoreCase))
            {
                instanceArgumentSeen = true;
                if (TryReadNext(launchArgs, ref index, out var instanceTypeValue))
                {
                    var parsedType = instanceTypeValue.ToLowerInvariant() switch
                    {
                        "childsession" => BetterGiInstanceType.ChildSession,
                        "webview" => BetterGiInstanceType.WebView,
                        _ => (BetterGiInstanceType?)null
                    };
                    if (parsedType is not null)
                    {
                        instanceType = parsedType.Value;
                        hasExplicitInstanceType = true;
                    }
                }
                continue;
            }

            if (argument.Equals(InstanceNameArgument, StringComparison.OrdinalIgnoreCase))
            {
                instanceArgumentSeen = true;
                if (TryReadNext(launchArgs, ref index, out var instanceNameValue))
                {
                    instanceName = instanceNameValue;
                }
                continue;
            }

            if (argument.Equals(RestartFromProcessIdArgument, StringComparison.OrdinalIgnoreCase))
            {
                instanceArgumentSeen = true;
                if (TryReadNext(launchArgs, ref index, out var processIdValue)
                    && int.TryParse(processIdValue, out var parsedProcessId)
                    && parsedProcessId > 0)
                {
                    restartFromProcessId = parsedProcessId;
                }
                continue;
            }

            commandArgs.Add(argument);
        }

        if (headless)
        {
            if (headlessConfigPath is null || instanceArgumentSeen || commandArgs.Count != 0)
                throw new ArgumentException("--headless 需要独立配置路径，且不能与图形实例或任务参数混用。");
        }
        else if (headlessConfigPath is not null || headlessTokenFile is not null)
        {
            throw new ArgumentException("headless 配置和凭据文件参数只能与 --headless 一同使用。");
        }

        if (commandArgs.Count == 0)
        {
            return Create(CommandLineAction.None);
        }

        var arg1 = commandArgs[0];
        var extra = commandArgs.Skip(1).ToArray();
        if (arg1.Equals("--pulonia-run", StringComparison.OrdinalIgnoreCase))
        {
            if (extra.Length != 1)
                throw new ArgumentException("--pulonia-run 需要且只接受一个计划 ID。");
            Pulonia.Services.PuloniaTaskValidator.ValidateId(extra[0], "--pulonia-run");
            return Create(CommandLineAction.PuloniaRun, puloniaPlanId: extra[0]);
        }

        if (arg1.Contains("startOneDragon", StringComparison.OrdinalIgnoreCase))
        {
            return Create(
                CommandLineAction.StartOneDragon,
                oneDragonConfigName: extra.Length > 0 ? extra[0] : null);
        }

        if (arg1.Equals("--startGroups", StringComparison.OrdinalIgnoreCase))
        {
            return Create(
                CommandLineAction.StartGroups,
                groupNames: extra);
        }

        if (arg1.Contains("start", StringComparison.OrdinalIgnoreCase))
        {
            return Create(CommandLineAction.Start);
        }

        return Create(CommandLineAction.None);

        CommandLineOptions Create(
            CommandLineAction action,
            string? oneDragonConfigName = null,
            string[]? groupNames = null, string? puloniaPlanId = null)
        {
            return new CommandLineOptions(
                action,
                oneDragonConfigName,
                groupNames,
                instanceType,
                hasExplicitInstanceType,
                restartFromProcessId,
                instanceName, puloniaPlanId, headless, headlessConfigPath, headlessTokenFile);
        }
    }

    private static bool TryReadNext(string[] args, ref int index, out string value)
    {
        if (index + 1 >= args.Length)
        {
            value = string.Empty;
            return false;
        }

        value = args[++index];
        return true;
    }
}

public enum CommandLineAction
{
    /// <summary>双击启动，无命令行参数</summary>
    None,

    /// <summary>纯 "start" — 仅启动截图器</summary>
    Start,

    /// <summary>startOneDragon — 启动一条龙</summary>
    StartOneDragon,

    /// <summary>--startGroups — 启动调度组</summary>
    StartGroups,
    /// <summary>通过计划 ID 手动提交新运行。</summary>
    PuloniaRun,

}
