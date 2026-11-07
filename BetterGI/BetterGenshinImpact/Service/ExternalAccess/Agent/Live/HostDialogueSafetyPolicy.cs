using BetterGenshinImpact.Core.Config;
using System;
using System.ComponentModel;
using System.Threading;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Live;

/// <summary>仅根据启动时本机配置授权候选诊断，不授权对话动作或解除锁定。</summary>
public sealed class HostDialogueSafetyPolicy : ILiveDialogueSafetyPolicy, IDisposable
{
    /// <summary>读取当前本机配置以检测替换。</summary>
    private readonly Func<ExternalAccessConfig?> _getConfig;
    /// <summary>启动时的外部访问配置。</summary>
    private readonly ExternalAccessConfig? _root;
    /// <summary>启动时的候选配置。</summary>
    private readonly HostDialogueCandidateConfig? _settings;
    /// <summary>启动时的消息区域配置。</summary>
    private readonly HostDialogueRegionConfig? _message;
    /// <summary>启动时的选项区域配置。</summary>
    private readonly HostDialogueRegionConfig? _options;
    /// <summary>启动时有效且启用的独立校准快照。</summary>
    private readonly LiveDialogueCalibrationDto? _approved;
    /// <summary>配置变化或策略释放后不可恢复。</summary>
    private int _invalidated;

    /// <summary>从宿主本地配置捕获快照；配置变化后须重启宿主。</summary>
    public HostDialogueSafetyPolicy(Func<ExternalAccessConfig?> getConfig)
    {
        _getConfig = getConfig ?? throw new ArgumentNullException(nameof(getConfig));
        try
        {
            _root = _getConfig();
            _settings = _root?.DialogueCandidate;
            _message = _settings?.Message;
            _options = _settings?.Options;
            if (_root is not null) _root.PropertyChanged += OnRootChanged;
            if (_settings is not null) _settings.PropertyChanged += OnSettingsChanged;
            if (_message is not null) _message.PropertyChanged += OnSettingsChanged;
            if (_options is not null && !ReferenceEquals(_options, _message)) _options.PropertyChanged += OnSettingsChanged;
            _approved = ReadCalibration(_settings);
        }
        catch
        {
            Interlocked.Exchange(ref _invalidated, 1);
        }
    }

    /// <inheritdoc />
    public bool AllowsExitCandidate(LiveDialogueCalibrationDto calibration)
    {
        try
        {
            if (Volatile.Read(ref _invalidated) != 0 || _approved is null || calibration?.EnableExitCandidate != true)
                return false;
            var root = _getConfig();
            var settings = root?.DialogueCandidate;
            if (!ReferenceEquals(root, _root) || !ReferenceEquals(settings, _settings)
                || !ReferenceEquals(settings?.Message, _message) || !ReferenceEquals(settings?.Options, _options))
                return false;
            var current = ReadCalibration(settings);
            var request = LiveDialogueCalibration.Copy(calibration);
            return current is not null && request is not null && Equal(_approved, current)
                && Equal(_approved, request) && Volatile.Read(ref _invalidated) == 0;
        }
        catch
        {
            return false;
        }
    }

    /// <inheritdoc />
    public LiveDialogueCalibrationDto? GetTrustedCalibration()
    {
        try
        {
            var calibration = LiveDialogueCalibration.Copy(_approved);
            return calibration is not null && AllowsExitCandidate(calibration) ? calibration : null;
        }
        catch { return null; }
    }

    /// <summary>拒绝缺失字段和无效区域，复用运行时的归一化校验。</summary>
    internal static LiveDialogueCalibrationDto? ReadCalibration(HostDialogueCandidateConfig? settings)
    {
        if (settings?.Enabled != true || settings.ExitFrames is not int frames
            || !ReadRegion(settings.Message, out var message) || !ReadRegion(settings.Options, out var options))
            return null;
        return LiveDialogueCalibration.Copy(new LiveDialogueCalibrationDto
        {
            EnableExitCandidate = true, ExitFrames = frames, Message = message, Options = options
        });
    }

    /// <summary>区分显式零坐标和未设置字段。</summary>
    internal static bool ReadRegion(HostDialogueRegionConfig? value, out LiveDialogueRegionDto? region)
    {
        region = null;
        if (value?.X is not double x || value.Y is not double y
            || value.Width is not double width || value.Height is not double height) return false;
        region = new LiveDialogueRegionDto { X = x, Y = y, Width = width, Height = height };
        return true;
    }

    /// <summary>要求两个完整区域和帧数精确一致。</summary>
    internal static bool Equal(LiveDialogueCalibrationDto left, LiveDialogueCalibrationDto right) =>
        left.EnableExitCandidate && right.EnableExitCandidate && left.ExitFrames == right.ExitFrames
        && EqualRegion(left.Message, right.Message) && EqualRegion(left.Options, right.Options);

    /// <summary>比较区域的所有坐标，不使用容差。</summary>
    private static bool EqualRegion(LiveDialogueRegionDto? left, LiveDialogueRegionDto? right) =>
        left is not null && right is not null && left.X == right.X && left.Y == right.Y
        && left.Width == right.Width && left.Height == right.Height;

    /// <summary>根配置只在替换候选配置时撤销。</summary>
    private void OnRootChanged(object? sender, PropertyChangedEventArgs args)
    {
        if (string.IsNullOrEmpty(args.PropertyName) || args.PropertyName == nameof(ExternalAccessConfig.DialogueCandidate))
            Interlocked.Exchange(ref _invalidated, 1);
    }

    /// <summary>任一候选配置变化均撤销当前策略。</summary>
    private void OnSettingsChanged(object? sender, PropertyChangedEventArgs args) =>
        Interlocked.Exchange(ref _invalidated, 1);

    /// <summary>撤销策略并解除配置事件订阅。</summary>
    public void Dispose()
    {
        Interlocked.Exchange(ref _invalidated, 1);
        if (_root is not null) _root.PropertyChanged -= OnRootChanged;
        if (_settings is not null) _settings.PropertyChanged -= OnSettingsChanged;
        if (_message is not null) _message.PropertyChanged -= OnSettingsChanged;
        if (_options is not null && !ReferenceEquals(_options, _message)) _options.PropertyChanged -= OnSettingsChanged;
    }
}
