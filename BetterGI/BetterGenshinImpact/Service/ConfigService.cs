using BetterGenshinImpact.Core.Config;
using BetterGenshinImpact.Helpers;
using BetterGenshinImpact.Service.Interface;
using BetterGenshinImpact.View.Windows;
using OpenCvSharp;
using System;
using System.IO;
using System.Runtime.ExceptionServices;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Threading;
using Application = System.Windows.Application;

namespace BetterGenshinImpact.Service;

/// <summary>
/// 配置读写服务。
/// <para>
/// 保存链路（详见 Docs/design/config-persistence.md）：
/// 1. 任意线程修改配置 → <see cref="RequestSave"/> 只登记改动，500ms 窗口内的多次改动合并为一次写盘；
/// 2. 快照（序列化）在 UI 线程生成，避免与 UI 线程上的集合修改竞争；
/// 3. 写盘在后台线程完成，先写临时文件再原子替换，避免写一半导致配置损坏。
/// </para>
/// </summary>
public class ConfigService : IConfigService, IDisposable
{
    private const string ConfigRelativePath = @"User/config.json";
    private const string ConfigFileName = "config.json";
    private const string BackupFolderName = "backup";

    /// <summary>
    /// 防抖窗口：窗口内的多次改动合并为一次写盘
    /// </summary>
    private static readonly TimeSpan SaveDelay = TimeSpan.FromMilliseconds(500);

    /// <summary>
    /// 等待 UI 线程生成快照的最长时间，超时后退回当前线程生成
    /// </summary>
    private static readonly TimeSpan UiSnapshotTimeout = TimeSpan.FromSeconds(3);

    /// <summary>
    /// 残留临时文件的清理阈值（避免误删其他实例正在写的临时文件）
    /// </summary>
    private static readonly TimeSpan StaleTempFileAge = TimeSpan.FromMinutes(1);

    private const int MaxSnapshotFailures = 3;
    private const int ReplaceRetryCount = 5;
    private const int ReplaceRetryDelayMs = 50;

    private static readonly UTF8Encoding Utf8NoBom = new(false);

    public static readonly JsonSerializerOptions JsonOptions = new()
    {
        NumberHandling = JsonNumberHandling.AllowNamedFloatingPointLiterals,
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        Converters =
        {
            new OpenCvPointJsonConverter(),
            new OpenCvRectJsonConverter(),
        },
        WriteIndented = true,
        AllowTrailingCommas = true,
        ReadCommentHandling = JsonCommentHandling.Skip,
    };

    private readonly object _initLocker = new();

    /// <summary>
    /// 保证快照按顺序生成，使版本号顺序与内容新旧一致
    /// </summary>
    private readonly object _snapshotLocker = new();

    /// <summary>
    /// 只串行化本进程内的文件写入（写与写之间），读取不受影响
    /// </summary>
    private readonly object _fileLocker = new();

    private readonly object _scheduleLocker = new();
    private readonly Timer _saveTimer;
    private bool _saveScheduled;

    /// <summary>
    /// 每次保存请求 +1
    /// </summary>
    private long _changeSeq;

    /// <summary>
    /// 已落盘快照对应的 <see cref="_changeSeq"/>
    /// </summary>
    private long _persistedSeq;

    private int _snapshotFailures;

    /// <summary>Headless 进程使用独立的配置文件，不能退回界面实例的配置。</summary>
    private readonly bool _headless;
    /// <summary>本服务唯一使用的配置文件路径。</summary>
    private readonly string _configFilePath;
    /// <summary>整个 headless 宿主生命周期内持有的配置排他锁。</summary>
    private FileStream? _headlessLock;
    /// <summary>持有配置文件的只读句柄，阻止其他进程写入或替换。</summary>
    private FileStream? _headlessConfigStream;
    /// <summary>防止重复释放和重复结束事件订阅。</summary>
    private int _disposed;

    /// <summary>
    /// 全局唯一的配置实例，任何线程都可以读写。
    /// 写入只会登记改动并异步落盘，序列化统一在 UI 线程进行。
    /// </summary>
    public static AllConfig? Config { get; private set; }

    /// <summary>根据启动模式选择配置路径并在无界面模式下持有跨进程锁。</summary>
    public ConfigService()
    {
        var options = CommandLineOptions.Instance;
        _headless = options.IsHeadless;
        if (_headless)
        {
            var path = options.HeadlessConfigPath;
            if (string.IsNullOrWhiteSpace(path) || !Path.IsPathFullyQualified(path))
                throw new ArgumentException("无界面模式需要现有配置文件的绝对路径。");
            _configFilePath = Path.GetFullPath(path);
            var guiConfigPath = Path.GetFullPath(Global.Absolute(ConfigRelativePath));
            if (string.Equals(_configFilePath, guiConfigPath, StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("无界面模式不能使用图形界面的配置文件。");
            if (!File.Exists(_configFilePath))
                throw new FileNotFoundException("无界面配置文件不存在。", _configFilePath);
            RejectHeadlessReparsePoints(_configFilePath);
            var lockPath = _configFilePath + ".lock";
            if (File.Exists(lockPath) && (File.GetAttributes(lockPath) & FileAttributes.ReparsePoint) != 0)
                throw new ArgumentException("无界面配置锁不能是符号链接。");
            _headlessLock = new FileStream(lockPath, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
            try
            {
                _headlessConfigStream = new FileStream(_configFilePath, FileMode.Open, FileAccess.Read, FileShare.Read);
            }
            catch
            {
                _headlessLock.Dispose();
                _headlessLock = null;
                throw;
            }
        }
        else
        {
            _configFilePath = Global.Absolute(ConfigRelativePath);
        }

        _saveTimer = new Timer(OnSaveTimerElapsed, null, Timeout.Infinite, Timeout.Infinite);
    }

    /// <summary>不跟随配置文件或父目录符号链接，避免指向其他实例配置。</summary>
    private static void RejectHeadlessReparsePoints(string filePath)
    {
        if ((File.GetAttributes(filePath) & FileAttributes.ReparsePoint) != 0)
            throw new ArgumentException("无界面配置文件不能是符号链接。");
        for (var directory = Directory.GetParent(filePath); directory is not null; directory = directory.Parent)
        {
            if ((directory.Attributes & FileAttributes.ReparsePoint) != 0)
                throw new ArgumentException("无界面配置目录不能是符号链接。");
        }
    }

    private void OnSaveTimerElapsed(object? state)
    {
        _ = SaveScheduledAsync();
    }

    private bool HasPendingChanges => Interlocked.Read(ref _changeSeq) != Interlocked.Read(ref _persistedSeq);

    public AllConfig Get()
    {
        lock (_initLocker)
        {
            ThrowIfHeadlessUnavailable();
            if (Config == null)
            {
                if (!_headless) CleanupStaleTempFiles();
                try
                {
                    Config = Read();
                }
                catch
                {
                    if (_headless) Dispose();
                    throw;
                }
                Config.OnAnyChangedAction = RequestSave;
                Config.InitEvent();
                // 覆盖 Environment.Exit 等不经过 Application.Exit 的退出路径
                AppDomain.CurrentDomain.ProcessExit += OnProcessExit;
            }

            return Config;
        }
    }

    /// <summary>无界面服务关闭后不得继续读取配置。</summary>
    private void ThrowIfHeadlessUnavailable()
    {
        if (_headless && (Volatile.Read(ref _disposed) != 0 || _headlessLock is null || _headlessConfigStream is null))
            throw new ObjectDisposedException(nameof(ConfigService));
    }

    /// <summary>
    /// 立即同步写盘
    /// </summary>
    public void Save()
    {
        if (_headless)
        {
            ThrowIfHeadlessUnavailable();
            return;
        }
        if (Config == null)
        {
            return;
        }

        // 让本次快照成为最新版本：即使有未通知的改动（如直接修改 List），也不会被更早生成的后台快照覆盖
        Interlocked.Increment(ref _changeSeq);
        SaveNow(showDialogOnError: true);
    }

    /// <summary>
    /// 有未保存的改动时立即同步写盘
    /// </summary>
    public void Flush()
    {
        if (_headless)
        {
            ThrowIfHeadlessUnavailable();
            return;
        }
        if (Config == null || !HasPendingChanges)
        {
            return;
        }

        SaveNow(showDialogOnError: true);
    }

    /// <summary>
    /// 登记一次改动并在防抖窗口结束后写盘，任意线程可调用
    /// </summary>
    public void RequestSave()
    {
        if (_headless) return;
        Interlocked.Increment(ref _changeSeq);
        ScheduleSave();
    }

    /// <summary>
    /// 从磁盘读取配置。
    /// 不与写入互斥：写入是"临时文件 + 原子替换"，读取方只会看到完整的旧文件或新文件，不会被写入阻塞
    /// </summary>
    public AllConfig Read()
    {
        ThrowIfHeadlessUnavailable();
        var filePath = _configFilePath;
        try
        {
            if (!File.Exists(filePath))
            {
                if (_headless) throw new FileNotFoundException("无界面配置文件不存在。", filePath);
                return MigrateLegacyConfig(new AllConfig());
            }

            var json = _headless ? ReadHeadlessConfig() : File.ReadAllText(filePath);
            var config = JsonSerializer.Deserialize<AllConfig>(json, JsonOptions);
            if (config == null)
            {
                if (_headless) throw new InvalidDataException("无界面配置为空。");
                return MigrateLegacyConfig(new AllConfig());
            }

            if (_headless) ApplyHeadlessToken(config);
            return MigrateLegacyConfig(config);
        }
        catch (Exception e)
        {
            if (_headless) throw new InvalidDataException("无界面配置读取失败，未使用默认配置。", e);
            Console.WriteLine(e.Message);
            Console.WriteLine(e.StackTrace);
            BackupConfigFile(filePath);
            ShowConfigExceptionDialog("读取", e);
            return MigrateLegacyConfig(new AllConfig());
        }
    }

    /// <summary>从受锁保护的文件句柄读取一次无界面配置快照。</summary>
    private string ReadHeadlessConfig()
    {
        lock (_fileLocker)
        {
            ThrowIfHeadlessUnavailable();
            var stream = _headlessConfigStream!;
            if (stream.Length > 16 * 1024 * 1024)
                throw new InvalidDataException("无界面配置文件过大。");
            stream.Position = 0;
            using var reader = new StreamReader(stream, new UTF8Encoding(false, true), true, 4096, leaveOpen: true);
            return reader.ReadToEnd();
        }
    }

    /// <summary>只将显式令牌文件加载到无界面配置的内存快照，不持久化或输出。</summary>
    private void ApplyHeadlessToken(AllConfig config)
    {
        if (config.ExternalAccessConfig is null)
            throw new InvalidDataException("无界面配置缺少外部访问设置。");
        var tokenFile = CommandLineOptions.Instance.HeadlessTokenFile;
        if (tokenFile is not null)
        {
            if (string.IsNullOrWhiteSpace(tokenFile) || !Path.IsPathFullyQualified(tokenFile))
                throw new ArgumentException("无界面令牌文件需要绝对路径。");
            var path = Path.GetFullPath(tokenFile);
            if (string.Equals(path, _configFilePath, StringComparison.OrdinalIgnoreCase)
                || string.Equals(path, Path.GetFullPath(Global.Absolute(ConfigRelativePath)), StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("令牌文件不能是配置文件。");
            using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
            if (stream.Length > 4096) throw new InvalidDataException("无界面令牌文件过大。");
            using var reader = new StreamReader(stream, new UTF8Encoding(false, true), true);
            config.ExternalAccessConfig.AccessToken = reader.ReadToEnd().Trim();
        }

        var token = config.ExternalAccessConfig.AccessToken;
        if (string.IsNullOrWhiteSpace(token))
            throw new InvalidDataException("无界面模式需要显式配置访问令牌。");
        foreach (var character in token)
        {
            if (char.IsWhiteSpace(character) || char.IsControl(character))
                throw new InvalidDataException("无界面访问令牌格式无效。");
        }
    }

    /// <summary>统一迁移读取或新建的配置；所有 Read 返回分支均经过这里。</summary>
    /// <param name="config">反序列化或新建的配置。</param>
    /// <returns>已完成旧字段迁移的配置。</returns>
    private static AllConfig MigrateLegacyConfig(AllConfig config)
    {
        config.AutoPickConfig.MigrateLegacyConfig();
        config.AutoDomainConfig.MigrateLegacyResinUsage();
        config.AutoStygianOnslaughtConfig.MigrateLegacyResinUsage();
        config.AutoBossConfig.MigrateLegacyResinUsage();
        config.AutoLeyLineOutcropConfig.MigrateLegacyResinUsage();
        return config;
    }

    public void Write(AllConfig config)
    {
        if (_headless)
        {
            ThrowIfHeadlessUnavailable();
            throw new InvalidOperationException("无界面宿主使用只读配置快照，不能写入配置。");
        }
        if (ReferenceEquals(config, Config))
        {
            Save();
            return;
        }

        // 非当前实例：保持旧接口语义，直接序列化并写入
        try
        {
            var json = JsonSerializer.Serialize(config, JsonOptions);
            lock (_fileLocker)
            {
                WriteJsonAtomically(json);
            }
        }
        catch (Exception e)
        {
            Console.WriteLine(e.Message);
            Console.WriteLine(e.StackTrace);
            ShowConfigExceptionDialog("写入", e);
        }
    }

    private void OnProcessExit(object? sender, EventArgs e)
    {
        try
        {
            if (Config != null && HasPendingChanges)
            {
                // 进程即将结束，弹窗没有意义
                SaveNow(showDialogOnError: false);
            }
        }
        catch
        {
            // 退出阶段不抛异常
        }
        finally
        {
            Dispose();
        }
    }

    /// <summary>在无界面宿主停止后释放配置排他锁；图形模式仍沿用原有生命周期。</summary>
    public void Dispose()
    {
        if (!_headless || Interlocked.Exchange(ref _disposed, 1) != 0) return;
        _saveTimer.Dispose();
        AppDomain.CurrentDomain.ProcessExit -= OnProcessExit;
        lock (_fileLocker)
        {
            _headlessConfigStream?.Dispose();
            _headlessConfigStream = null;
            _headlessLock?.Dispose();
            _headlessLock = null;
        }
    }

    private void ScheduleSave()
    {
        lock (_scheduleLocker)
        {
            if (_saveScheduled)
            {
                return;
            }

            _saveScheduled = true;
            _saveTimer.Change(SaveDelay, Timeout.InfiniteTimeSpan);
        }
    }

    /// <summary>
    /// 防抖定时器回调（线程池线程）
    /// </summary>
    private async Task SaveScheduledAsync()
    {
        lock (_scheduleLocker)
        {
            _saveScheduled = false;
        }

        try
        {
            if (Config == null || !HasPendingChanges)
            {
                return;
            }

            ConfigSnapshot snapshot;
            try
            {
                snapshot = await CaptureSnapshotOnUiThreadAsync().ConfigureAwait(false);
                Interlocked.Exchange(ref _snapshotFailures, 0);
            }
            catch (Exception e)
            {
                // 多半是其他线程正在修改集合，稍后重试
                var failures = Interlocked.Increment(ref _snapshotFailures);
                Serilog.Log.Warning(e, "配置序列化失败（第 {Count} 次）", failures);
                if (failures < MaxSnapshotFailures)
                {
                    ScheduleSave();
                }
                else
                {
                    // 停止自动重试，等待下一次改动或退出时再尝试
                    Interlocked.Exchange(ref _snapshotFailures, 0);
                    ShowConfigExceptionDialog("序列化", e);
                }

                return;
            }

            WriteSnapshot(snapshot, showDialogOnError: true);
        }
        catch (Exception e)
        {
            // 定时器回调中的异常不能抛出
            Serilog.Log.Error(e, "配置保存失败");
        }
    }

    private void SaveNow(bool showDialogOnError)
    {
        ConfigSnapshot snapshot;
        try
        {
            snapshot = CaptureSnapshotOnUiThread();
        }
        catch (Exception)
        {
            try
            {
                // 可能与其他线程的集合修改冲突，在当前线程重试一次
                snapshot = CaptureSnapshot();
            }
            catch (Exception e)
            {
                Console.WriteLine(e.Message);
                Console.WriteLine(e.StackTrace);
                if (showDialogOnError)
                {
                    ShowConfigExceptionDialog("序列化", e);
                }

                return;
            }
        }

        WriteSnapshot(snapshot, showDialogOnError);
    }

    private ConfigSnapshot CaptureSnapshot()
    {
        lock (_snapshotLocker)
        {
            // 版本号必须在序列化之前读取：序列化期间发生的改动会使版本号更大，从而继续被视为未保存
            var seq = Interlocked.Read(ref _changeSeq);
            var json = JsonSerializer.Serialize(Config!, JsonOptions);
            return new ConfigSnapshot(seq, json);
        }
    }

    /// <summary>
    /// 在 UI 线程上执行时使用：异常以返回值带回调用方，避免进入 Dispatcher 的未处理异常流程
    /// </summary>
    private SnapshotResult TryCaptureSnapshot()
    {
        try
        {
            return new SnapshotResult(CaptureSnapshot(), null);
        }
        catch (Exception e)
        {
            return new SnapshotResult(null, e);
        }
    }

    private static ConfigSnapshot Unwrap(SnapshotResult result)
    {
        if (result.Error != null)
        {
            ExceptionDispatchInfo.Capture(result.Error).Throw();
        }

        return result.Snapshot!;
    }

    private static bool CanCaptureOnCurrentThread(out Dispatcher? dispatcher)
    {
        dispatcher = Application.Current?.Dispatcher;
        return dispatcher == null || dispatcher.CheckAccess() || dispatcher.HasShutdownStarted;
    }

    private ConfigSnapshot CaptureSnapshotOnUiThread()
    {
        if (CanCaptureOnCurrentThread(out var dispatcher))
        {
            return CaptureSnapshot();
        }

        SnapshotResult? result = null;
        try
        {
            result = dispatcher!.Invoke(TryCaptureSnapshot, DispatcherPriority.Send, CancellationToken.None, UiSnapshotTimeout);
        }
        catch (Exception e) when (e is OperationCanceledException or TimeoutException)
        {
            // Dispatcher 关闭或 UI 线程繁忙
        }

        // 超时未执行时 Invoke 返回 null，退回当前线程生成
        return result == null ? CaptureSnapshot() : Unwrap(result);
    }

    private async Task<ConfigSnapshot> CaptureSnapshotOnUiThreadAsync()
    {
        if (CanCaptureOnCurrentThread(out var dispatcher))
        {
            return CaptureSnapshot();
        }

        var operation = dispatcher!.InvokeAsync(TryCaptureSnapshot, DispatcherPriority.Background);
        try
        {
            return Unwrap(await operation.Task.WaitAsync(UiSnapshotTimeout).ConfigureAwait(false));
        }
        catch (TimeoutException)
        {
            // UI 线程长时间繁忙：未开始执行则取消并在当前线程生成，已开始则等它完成
            if (operation.Abort())
            {
                return CaptureSnapshot();
            }

            return Unwrap(await operation.Task.ConfigureAwait(false));
        }
        catch (OperationCanceledException)
        {
            // Dispatcher 已关闭
            return CaptureSnapshot();
        }
    }

    private void WriteSnapshot(ConfigSnapshot snapshot, bool showDialogOnError)
    {
        lock (_fileLocker)
        {
            if (snapshot.Seq <= Interlocked.Read(ref _persistedSeq))
            {
                // 更新的快照已经落盘
                return;
            }

            try
            {
                WriteJsonAtomically(snapshot.Json);
                Interlocked.Exchange(ref _persistedSeq, snapshot.Seq);
            }
            catch (Exception e)
            {
                // 未更新 _persistedSeq，下次改动或退出时会再次尝试
                Console.WriteLine(e.Message);
                Console.WriteLine(e.StackTrace);
                Serilog.Log.Error(e, "配置写入失败");
                if (showDialogOnError)
                {
                    ShowConfigExceptionDialog("写入", e);
                }
            }
        }
    }

    /// <summary>
    /// 先写临时文件再原子替换，读取方只会看到完整的旧文件或新文件
    /// </summary>
    private static void WriteJsonAtomically(string json)
    {
        var file = Global.Absolute(ConfigRelativePath);
        var directory = Path.GetDirectoryName(file)!;
        Directory.CreateDirectory(directory);

        // 带上进程号和 GUID，避免主实例与桌面分身等多个实例互相踩到同一个临时文件
        var tempFile = Path.Combine(directory, $"{ConfigFileName}.{Environment.ProcessId}.{Guid.NewGuid():N}.tmp");
        try
        {
            using (var stream = new FileStream(tempFile, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                var bytes = Utf8NoBom.GetBytes(json);
                stream.Write(bytes, 0, bytes.Length);
                stream.Flush(flushToDisk: true);
            }

            ReplaceWithRetry(tempFile, file);
        }
        finally
        {
            TryDeleteFile(tempFile);
        }
    }

    private static void ReplaceWithRetry(string source, string destination)
    {
        for (var attempt = 1; ; attempt++)
        {
            try
            {
                // 同卷 rename，替换是原子的
                File.Move(source, destination, overwrite: true);
                return;
            }
            catch (Exception e) when (attempt < ReplaceRetryCount && e is IOException or UnauthorizedAccessException)
            {
                // 目标文件可能正被其他进程读取（未开放删除共享），稍后重试
                Thread.Sleep(ReplaceRetryDelayMs);
            }
        }
    }

    private static void CleanupStaleTempFiles()
    {
        try
        {
            var directory = Path.GetDirectoryName(Global.Absolute(ConfigRelativePath))!;
            if (!Directory.Exists(directory))
            {
                return;
            }

            var threshold = DateTime.UtcNow - StaleTempFileAge;
            foreach (var tempFile in Directory.EnumerateFiles(directory, $"{ConfigFileName}.*.tmp"))
            {
                if (File.GetLastWriteTimeUtc(tempFile) < threshold)
                {
                    TryDeleteFile(tempFile);
                }
            }
        }
        catch (Exception e)
        {
            Console.WriteLine(e.Message);
        }
    }

    private static void TryDeleteFile(string path)
    {
        try
        {
            if (File.Exists(path))
            {
                File.Delete(path);
            }
        }
        catch
        {
            // 清理失败不影响主流程
        }
    }

    private static void BackupConfigFile(string filePath)
    {
        try
        {
            if (!File.Exists(filePath))
            {
                return;
            }

            var directoryPath = Path.GetDirectoryName(filePath);
            if (string.IsNullOrWhiteSpace(directoryPath))
            {
                return;
            }

            var backupDirectory = Path.Combine(directoryPath, BackupFolderName);
            Directory.CreateDirectory(backupDirectory);

            var backupFileName = $"config_{DateTime.Now:yyyyMMdd_HHmmss_fff}.json.bak";
            var backupFilePath = Path.Combine(backupDirectory, backupFileName);
            File.Copy(filePath, backupFilePath, false);
        }
        catch (Exception ex)
        {
            Console.WriteLine(ex.Message);
            Console.WriteLine(ex.StackTrace);
        }
    }

    private static void ShowConfigExceptionDialog(string operation, Exception exception)
    {
        var current = Application.Current;
        if (current?.Dispatcher == null)
        {
            return;
        }

        var coreException = exception.GetBaseException();
        var coreStack = string.IsNullOrWhiteSpace(coreException.StackTrace) ? "无可用堆栈信息" : coreException.StackTrace;
        var message = $"配置文件{operation}失败\n错误：{coreException.Message}\n堆栈：\n{coreStack}";
        _ = ThemedMessageBox.ErrorAsync(message, "配置文件异常");
    }

    /// <param name="Seq">生成快照时的改动版本号</param>
    /// <param name="Json">序列化后的配置内容</param>
    private sealed record ConfigSnapshot(long Seq, string Json);

    private sealed record SnapshotResult(ConfigSnapshot? Snapshot, Exception? Error);
}

public class OpenCvRectJsonConverter : JsonConverter<Rect>
{
    public override unsafe Rect Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options)
    {
        RectHelper helper = JsonSerializer.Deserialize<RectHelper>(ref reader, options);
        return *(Rect*)&helper;
    }

    public override unsafe void Write(Utf8JsonWriter writer, Rect value, JsonSerializerOptions options)
    {
        RectHelper helper = *(RectHelper*)&value;
        JsonSerializer.Serialize(writer, helper, options);
    }

    // DO NOT MODIFY: Keep the layout same as OpenCvSharp.Rect
    private struct RectHelper
    {
        public int X { get; set; }
        public int Y { get; set; }
        public int Width { get; set; }
        public int Height { get; set; }
    }
}

public class OpenCvPointJsonConverter : JsonConverter<Point>
{
    public override unsafe Point Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options)
    {
        PointHelper helper = JsonSerializer.Deserialize<PointHelper>(ref reader, options);
        return *(Point*)&helper;
    }

    public override unsafe void Write(Utf8JsonWriter writer, Point value, JsonSerializerOptions options)
    {
        PointHelper helper = *(PointHelper*)&value;
        JsonSerializer.Serialize(writer, helper, options);
    }

    // DO NOT MODIFY: Keep the layout same as OpenCvSharp.Point
    private struct PointHelper
    {
        public int X { get; set; }
        public int Y { get; set; }
    }
}
