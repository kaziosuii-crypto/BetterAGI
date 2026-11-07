using BetterGenshinImpact.Core.Config;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Media;
using OpenCvSharp;
using System;
using System.ComponentModel;
using System.IO;
using System.Threading;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Live;

/// <summary>用户本机标定的主界面全区域正向模板，不依赖 OCR 空结果。</summary>
public sealed class HostDialogueExitPolicy : ILiveDialogueExitPolicy, IDisposable
{
    private readonly Func<ExternalAccessConfig?> _getConfig;
    private readonly ExternalAccessConfig? _root;
    private readonly HostDialogueExitConfig? _settings;
    private readonly HostDialogueCandidateConfig? _candidate;
    private readonly HostDialogueRegionConfig? _message, _options, _regionConfig, _possibleConfig;
    private readonly string? _templatePath;
    private readonly bool _enabled, _mainTemplateCertified, _coverageConfirmed;
    private readonly int _requiredFrames;
    private readonly LiveDialogueCalibrationDto? _calibration;
    private LiveDialogueRegionDto? _region;
    private FileStream? _stream;
    private Mat? _template;
    private Mat? _mask;
    private int _maskCount;
    private readonly object _templateGate = new();
    private int _invalidated;

    /// <summary>固定宿主本地模板和校准，任何后续更改都撤销本次能力。</summary>
    public HostDialogueExitPolicy(Func<ExternalAccessConfig?> getConfig)
    {
        _getConfig = getConfig ?? throw new ArgumentNullException(nameof(getConfig));
        try
        {
            _root = _getConfig();
            _settings = _root?.DialogueExitAuthorization;
            _candidate = _root?.DialogueCandidate;
            _message = _candidate?.Message;
            _options = _candidate?.Options;
            _regionConfig = _settings?.Region;
            _possibleConfig = _settings?.PossibleDialogueRegion;
            _templatePath = _settings?.TemplatePath;
            _enabled = _settings?.Enabled == true;
            _mainTemplateCertified = _settings?.MainTemplateCertified == true;
            _coverageConfirmed = _settings?.CoverageConfirmed == true;
            _requiredFrames = _settings?.RequiredFrames ?? 0;
            _calibration = HostDialogueSafetyPolicy.ReadCalibration(_candidate);
            if (_root is not null) _root.PropertyChanged += OnRootChanged;
            if (_settings is not null) _settings.PropertyChanged += Invalidate;
            if (_candidate is not null) _candidate.PropertyChanged += Invalidate;
            if (_message is not null) _message.PropertyChanged += Invalidate;
            if (_options is not null && !ReferenceEquals(_options, _message)) _options.PropertyChanged += Invalidate;
            if (_regionConfig is not null) _regionConfig.PropertyChanged += Invalidate;
            if (_possibleConfig is not null && !ReferenceEquals(_possibleConfig, _regionConfig))
                _possibleConfig.PropertyChanged += Invalidate;
            InitializeTemplate();
        }
        catch { Interlocked.Exchange(ref _invalidated, 1); }
    }

    /// <summary>退出所需帧数；无有效配置时为零。</summary>
    public int RequiredFrames => _requiredFrames is >= 2 and <= 10 ? _requiredFrames : 0;

    /// <summary>仅用户确认的本机静态主界面模板可启用；区域须与模板像素尺寸一致。</summary>
    private void InitializeTemplate()
    {
        if (!_enabled || !_mainTemplateCertified || !_coverageConfirmed || RequiredFrames == 0 || _calibration is null
            || !HostDialogueSafetyPolicy.ReadRegion(_regionConfig, out var region)
            || !HostDialogueSafetyPolicy.ReadRegion(_possibleConfig, out var possible)) return;
        FileStream? stream = null;
        Mat? template = null;
        try
        {
            // 退出模板是独立的静态主界面 HUD 区域，不要求覆盖对话区域：
            // 对话文字与选项所在位置在无对话时画面仍会变化，无法做像素级全等匹配。
            // 仍要求“潜在对话区域”覆盖消息与选项，避免标定漏掉可能的对话位置。
            if (!Covers(possible, _calibration.Message) || !Covers(possible, _calibration.Options)) return;
            if (string.IsNullOrWhiteSpace(_templatePath) || !Path.IsPathFullyQualified(_templatePath)) return;
            var extension = Path.GetExtension(_templatePath);
            if (!extension.Equals(".png", StringComparison.OrdinalIgnoreCase)
                && !extension.Equals(".jpg", StringComparison.OrdinalIgnoreCase)
                && !extension.Equals(".jpeg", StringComparison.OrdinalIgnoreCase)) return;
            stream = new FileStream(_templatePath, FileMode.Open, FileAccess.Read, FileShare.Read);
            if (stream.Length is <= 0 or > 8388608) return;
            var bytes = new byte[(int)stream.Length];
            stream.ReadExactly(bytes);
            template = Cv2.ImDecode(bytes, ImreadModes.Color);
            if (template.Empty() || template.Width < 64 || template.Height < 64
                || template.Width > 8192 || template.Height > 8192) return;
            using var gray = new Mat();
            Cv2.CvtColor(template, gray, ColorConversionCodes.BGR2GRAY);
            Cv2.MeanStdDev(gray, out _, out var deviation);
            if (!double.IsFinite(deviation.Val0) || deviation.Val0 < 8) return;
            var mask = BuildMask(template);
            var maskCount = mask is null ? 0 : Cv2.CountNonZero(mask);
            if (mask is null || maskCount < 200) { mask?.Dispose(); return; }
            _region = region;
            _template = template;
            _mask = mask;
            _maskCount = maskCount;
            _stream = stream;
            template = null;
            stream = null;
        }
        catch { }
        finally { template?.Dispose(); stream?.Dispose(); }
    }

    /// <summary>归一化外框必须完整覆盖内框。</summary>
    private static bool Covers(LiveDialogueRegionDto outer, LiveDialogueRegionDto? inner) => inner is not null
        && outer.X <= inner.X && outer.Y <= inner.Y
        && outer.X + outer.Width >= inner.X + inner.Width && outer.Y + outer.Height >= inner.Y + inner.Height;

    /// <summary>只保留模板里强烈属于 UI 的像素（亮白与高饱和红），背景明暗变化不参与比对。</summary>
    private static Mat? BuildMask(Mat template)
    {
        try
        {
            using var hsv = new Mat();
            Cv2.CvtColor(template, hsv, ColorConversionCodes.BGR2HSV);
            using var white = new Mat();
            Cv2.InRange(hsv, new Scalar(0, 0, 215), new Scalar(180, 45, 255), white);
            using var redA = new Mat();
            Cv2.InRange(hsv, new Scalar(0, 120, 110), new Scalar(12, 255, 255), redA);
            using var redB = new Mat();
            Cv2.InRange(hsv, new Scalar(168, 120, 110), new Scalar(180, 255, 255), redB);
            var mask = new Mat();
            Cv2.BitwiseOr(white, redA, mask);
            Cv2.BitwiseOr(mask, redB, mask);
            return mask;
        }
        catch { return null; }
    }

    /// <summary>仅表示固定配置可用，不表示任何帧已经匹配或游戏已响应。</summary>
    public bool IsAvailable(LiveDialogueCalibrationDto? calibration)
    {
        try
        {
            if (Volatile.Read(ref _invalidated) != 0 || !_enabled || !_mainTemplateCertified || !_coverageConfirmed || RequiredFrames == 0
                || _calibration is null || calibration is null || _template is null || _region is null
                || _settings is null) return false;
            var root = _getConfig();
            if (!ReferenceEquals(root, _root) || !ReferenceEquals(root?.DialogueExitAuthorization, _settings)
                || !ReferenceEquals(root?.DialogueCandidate, _candidate)
                || !ReferenceEquals(_candidate?.Message, _message) || !ReferenceEquals(_candidate?.Options, _options)
                || !ReferenceEquals(_settings.Region, _regionConfig)
                || !ReferenceEquals(_settings.PossibleDialogueRegion, _possibleConfig)
                || _settings.Enabled != _enabled || _settings.MainTemplateCertified != _mainTemplateCertified
                || _settings.CoverageConfirmed != _coverageConfirmed
                || _settings.RequiredFrames != _requiredFrames || _settings.TemplatePath != _templatePath) return false;
            var current = HostDialogueSafetyPolicy.ReadCalibration(_candidate);
            var requested = LiveDialogueCalibration.Copy(calibration);
            return current is not null && requested is not null && HostDialogueSafetyPolicy.Equal(_calibration, current)
                && HostDialogueSafetyPolicy.Equal(_calibration, requested) && Volatile.Read(ref _invalidated) == 0;
        }
        catch { return false; }
    }

    /// <summary>在当前原始帧上严格全区域匹配；真实识别准确率须独立实机标定。</summary>
    public bool Matches(AgentFrame frame, LiveDialogueCalibrationDto? calibration)
    {
        try
        {
            if (!IsAvailable(calibration) || _region is null) return false;
            var image = frame.Image;
            var left = (int)Math.Floor(_region.X * image.Width);
            var top = (int)Math.Floor(_region.Y * image.Height);
            var right = (int)Math.Ceiling((_region.X + _region.Width) * image.Width);
            var bottom = (int)Math.Ceiling((_region.Y + _region.Height) * image.Height);
            if (left < 0 || top < 0 || right > image.Width || bottom > image.Height || right <= left || bottom <= top)
                return false;
            lock (_templateGate)
            {
                if (!IsAvailable(calibration) || _template is null || _stream is null
                    || _template.Width != right - left || _template.Height != bottom - top)
                {
                    // 临时诊断（BGI_EXIT_DIAG=1 时启用）：尺寸不一致是"永远匹配不上"的最常见原因。
                    ReportDiag($"exit-template unavailable or size mismatch: " +
                        $"template={_template?.Width}x{_template?.Height} " +
                        $"region={right - left}x{bottom - top} image={image.Width}x{image.Height}");
                    return false;
                }
                using var crop = new Mat(image, new Rect(left, top, right - left, bottom - top));
                // 只和模板里强烈属于 UI 的像素比对：HUD 叠在世界画面上，
                // 背景随镜头变化，整块像素比对在真实游戏里不可能稳定。
                using var difference = new Mat();
                Cv2.Absdiff(crop, _template, difference);
                var planes = Cv2.Split(difference);
                try
                {
                    using var maxDifference = new Mat();
                    Cv2.Max(planes[0], planes[1], maxDifference);
                    Cv2.Max(maxDifference, planes[2], maxDifference);
                    using var hits = new Mat();
                    Cv2.Compare(maxDifference, new Scalar(24), hits, CmpType.LE);
                    using var masked = new Mat();
                    Cv2.BitwiseAnd(hits, _mask!, masked);
                    var matched = Cv2.CountNonZero(masked);
                    var agreement = _maskCount == 0 ? 0d : (double)matched / _maskCount;
                    ReportDiag($"agreement={agreement:F4} mask={_maskCount} " +
                        $"template={_template.Width}x{_template.Height} image={image.Width}x{image.Height}");
                    return agreement >= 0.90 && IsAvailable(calibration);
                }
                finally
                {
                    foreach (var plane in planes) plane.Dispose();
                }
            }
        }
        catch { return false; }
    }

    /// <summary>临时诊断输出（仅在设置 BGI_EXIT_DIAG=1 时写临时文件），定位完成后移除。</summary>
    private static void ReportDiag(string message)
    {
        try
        {
            if (Environment.GetEnvironmentVariable("BGI_EXIT_DIAG") != "1") return;
            File.AppendAllText(Path.Combine(Path.GetTempPath(), "bgi-exit-diag.log"),
                $"{DateTime.Now:HH:mm:ss.fff} {message}{Environment.NewLine}");
        }
        catch { }
    }

    /// <summary>只有相关根配置替换时撤销。</summary>
    private void OnRootChanged(object? sender, PropertyChangedEventArgs args)
    {
        if (string.IsNullOrEmpty(args.PropertyName) || args.PropertyName is nameof(ExternalAccessConfig.DialogueExitAuthorization)
            or nameof(ExternalAccessConfig.DialogueCandidate)) Interlocked.Exchange(ref _invalidated, 1);
    }
    /// <summary>任一校准细项改变后必须重新启动。</summary>
    private void Invalidate(object? sender, PropertyChangedEventArgs args) => Interlocked.Exchange(ref _invalidated, 1);
    /// <summary>撤销能力并释放模板和配置订阅。</summary>
    public void Dispose()
    {
        Interlocked.Exchange(ref _invalidated, 1);
        if (_root is not null) _root.PropertyChanged -= OnRootChanged;
        if (_settings is not null) _settings.PropertyChanged -= Invalidate;
        if (_candidate is not null) _candidate.PropertyChanged -= Invalidate;
        if (_message is not null) _message.PropertyChanged -= Invalidate;
        if (_options is not null && !ReferenceEquals(_options, _message)) _options.PropertyChanged -= Invalidate;
        if (_regionConfig is not null) _regionConfig.PropertyChanged -= Invalidate;
        if (_possibleConfig is not null && !ReferenceEquals(_possibleConfig, _regionConfig)) _possibleConfig.PropertyChanged -= Invalidate;
        lock (_templateGate)
        {
            _template?.Dispose();
            _template = null;
            _mask?.Dispose();
            _mask = null;
            _maskCount = 0;
            _stream?.Dispose();
            _stream = null;
        }
    }
}
