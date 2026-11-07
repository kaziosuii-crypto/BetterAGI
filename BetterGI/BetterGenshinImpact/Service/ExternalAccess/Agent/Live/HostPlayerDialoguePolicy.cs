using BetterGenshinImpact.Core.Config;
using Microsoft.Data.Sqlite;
using OpenCvSharp;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Media;
using System;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Live;

/// <summary>宿主固定的只读 Player 决策证明和独立选项点击授权。</summary>
public sealed class HostPlayerDialoguePolicy : ILivePlayerDialoguePolicy, IDisposable
{
    /// <summary>读取启动时所在的本机配置根。</summary>
    private readonly Func<ExternalAccessConfig?> _getConfig;
    /// <summary>启动时的配置对象与固定值。</summary>
    private readonly ExternalAccessConfig? _root;
    private readonly HostPlayerEvidenceConfig? _evidence;
    private readonly HostDialogueActionConfig? _action;
    private readonly HostDialogueCandidateConfig? _candidate;
    private readonly HostDialogueRegionConfig? _message, _options;
    private readonly string? _databasePath, _scope, _advanceTemplatePath;
    private readonly LiveDialogueCalibrationDto? _calibration;
    private readonly bool _selectAllowed, _advanceAllowed, _advanceCertified, _coverageConfirmed;
    private readonly HostDialogueRegionConfig? _advanceRegionConfig, _possibleOptionsConfig;
    private LiveDialogueRegionDto? _advanceRegion;
    private FileStream? _templateStream;
    private Mat? _advanceTemplate;
    private readonly object _templateGate = new();
    /// <summary>配置改变后本次宿主生命周期不得恢复授权。</summary>
    private int _invalidated;

    /// <summary>固定数据库、玩家身份及独立的本机授权；缺失配置时一律拒绝。</summary>
    public HostPlayerDialoguePolicy(Func<ExternalAccessConfig?> getConfig)
    {
        _getConfig = getConfig ?? throw new ArgumentNullException(nameof(getConfig));
        try
        {
            _root = _getConfig();
            _evidence = _root?.PlayerEvidence;
            _action = _root?.DialogueActionAuthorization;
            _candidate = _root?.DialogueCandidate;
            _message = _candidate?.Message;
            _options = _candidate?.Options;
            var path = _evidence?.DatabasePath;
            var scope = _evidence?.PlayerScope;
            if (!string.IsNullOrWhiteSpace(path) && Path.IsPathFullyQualified(path)
                && !string.IsNullOrWhiteSpace(scope) && scope.Length == 71
                && scope.StartsWith("player-", StringComparison.Ordinal)
                && scope.AsSpan(7).ToString().All(c => c is >= '0' and <= '9' or >= 'a' and <= 'f'))
            {
                _databasePath = Path.GetFullPath(path);
                _scope = scope;
            }
            _selectAllowed = _action?.AllowSelect == true;
            _advanceAllowed = _action?.AllowAdvance == true;
            _advanceCertified = _action?.AdvanceTemplateCertified == true;
            _advanceTemplatePath = _action?.AdvanceTemplatePath;
            _coverageConfirmed = _action?.OptionCoverageConfirmed == true;
            _advanceRegionConfig = _action?.AdvanceRegion;
            _possibleOptionsConfig = _action?.PossibleOptionsRegion;
            _calibration = HostDialogueSafetyPolicy.ReadCalibration(_candidate);
            if (_root is not null) _root.PropertyChanged += OnRootChanged;
            if (_evidence is not null) _evidence.PropertyChanged += Invalidate;
            if (_action is not null) _action.PropertyChanged += Invalidate;
            if (_candidate is not null) _candidate.PropertyChanged += Invalidate;
            if (_message is not null) _message.PropertyChanged += Invalidate;
            if (_options is not null && !ReferenceEquals(_options, _message)) _options.PropertyChanged += Invalidate;
            if (_advanceRegionConfig is not null) _advanceRegionConfig.PropertyChanged += Invalidate;
            if (_possibleOptionsConfig is not null && !ReferenceEquals(_possibleOptionsConfig, _advanceRegionConfig))
                _possibleOptionsConfig.PropertyChanged += Invalidate;
            InitializeAdvanceTemplate();
        }
        catch
        {
            Interlocked.Exchange(ref _invalidated, 1);
        }
    }

    /// <summary>本机显式授权和启动时独立校准均未变化时允许选择。</summary>
    public bool AllowsSelect(LiveDialogueCalibrationDto? calibration)
    {
        try
        {
            if (!Current() || !_selectAllowed || _calibration is null || calibration is null) return false;
            var current = HostDialogueSafetyPolicy.ReadCalibration(_candidate);
            var requested = LiveDialogueCalibration.Copy(calibration);
            return current is not null && requested is not null
                && HostDialogueSafetyPolicy.Equal(_calibration, current)
                && HostDialogueSafetyPolicy.Equal(_calibration, requested) && Current();
        }
        catch { return false; }
    }

    /// <summary>只接受用户标定的完整区域模板，不以缺失选项作为推进证明。</summary>
    private void InitializeAdvanceTemplate()
    {
        if (!_advanceAllowed || !_advanceCertified || !_coverageConfirmed || _action is null || _calibration is null
            || !HostDialogueSafetyPolicy.ReadRegion(_advanceRegionConfig, out var region)
            || !HostDialogueSafetyPolicy.ReadRegion(_possibleOptionsConfig, out var possible)) return;
        FileStream? stream = null;
        Mat? template = null;
        try
        {
            var validated = LiveDialogueCalibration.Copy(new LiveDialogueCalibrationDto
            {
                Message = region, Options = possible, EnableExitCandidate = true, ExitFrames = 3
            });
            if (validated?.Message is not { } outer || validated.Options is not { } options
                || !Covers(outer, _calibration.Message) || !Covers(outer, _calibration.Options)
                || !Covers(outer, options) || !Covers(options, _calibration.Options)) return;
            var path = _action.AdvanceTemplatePath;
            if (string.IsNullOrWhiteSpace(path) || !Path.IsPathFullyQualified(path)) return;
            var extension = Path.GetExtension(path);
            if (!extension.Equals(".png", StringComparison.OrdinalIgnoreCase)
                && !extension.Equals(".jpg", StringComparison.OrdinalIgnoreCase)
                && !extension.Equals(".jpeg", StringComparison.OrdinalIgnoreCase)) return;
            stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
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
            _advanceRegion = outer;
            _advanceTemplate = template;
            _templateStream = stream;
            template = null;
            stream = null;
        }
        catch { }
        finally
        {
            template?.Dispose();
            stream?.Dispose();
        }
    }

    /// <summary>归一化外框必须覆盖完整内框。</summary>
    private static bool Covers(LiveDialogueRegionDto outer, LiveDialogueRegionDto? inner) => inner is not null
        && outer.X <= inner.X && outer.Y <= inner.Y
        && outer.X + outer.Width >= inner.X + inner.Width && outer.Y + outer.Height >= inner.Y + inner.Height;

    /// <summary>本机仅推进授权、用户覆盖声明及启动时校准均有效。</summary>
    public bool AllowsAdvance(LiveDialogueCalibrationDto? calibration)
    {
        try
        {
            if (!Current() || !_advanceAllowed || !_advanceCertified || !_coverageConfirmed || _advanceTemplate is null
                || _advanceRegion is null || _calibration is null || calibration is null) return false;
            var current = HostDialogueSafetyPolicy.ReadCalibration(_candidate);
            var requested = LiveDialogueCalibration.Copy(calibration);
            return current is not null && requested is not null
                && HostDialogueSafetyPolicy.Equal(_calibration, current)
                && HostDialogueSafetyPolicy.Equal(_calibration, requested) && Current();
        }
        catch { return false; }
    }

    /// <summary>全区域严格比较用户标定模板；匹配不代替真实界面校准。</summary>
    public bool MatchesAdvanceTemplate(AgentFrame frame, LiveDialogueCalibrationDto? calibration)
    {
        try
        {
            if (!AllowsAdvance(calibration) || _advanceRegion is null) return false;
            var image = frame.Image;
            var region = _advanceRegion;
            var left = (int)Math.Floor(region.X * image.Width);
            var top = (int)Math.Floor(region.Y * image.Height);
            var right = (int)Math.Ceiling((region.X + region.Width) * image.Width);
            var bottom = (int)Math.Ceiling((region.Y + region.Height) * image.Height);
            if (left < 0 || top < 0 || right > image.Width || bottom > image.Height || right <= left || bottom <= top)
                return false;
            lock (_templateGate)
            {
                if (!AllowsAdvance(calibration) || _advanceTemplate is null || _templateStream is null
                    || _advanceTemplate.Width != right - left || _advanceTemplate.Height != bottom - top) return false;
                using var crop = new Mat(image, new Rect(left, top, right - left, bottom - top));
                using var result = new Mat();
                Cv2.MatchTemplate(crop, _advanceTemplate, result, TemplateMatchModes.CCoeffNormed);
                var score = result.At<float>(0, 0);
                var maxDifference = Cv2.Norm(crop, _advanceTemplate, NormTypes.INF);
                return float.IsFinite(score) && score >= 0.9999f && double.IsFinite(maxDifference)
                    && maxDifference <= 3 && AllowsAdvance(calibration);
            }
        }
        catch { return false; }
    }

    /// <summary>只读核验 Player 已提交的选项决策。</summary>
    public bool HasCommittedChoice(LiveDialogueDecisionRequest request, LiveDialogueEvidenceSourceDto source,
        LiveDialogueOcrCandidateDto candidate) => HasCommitted(request, source, candidate);

    /// <summary>只读核验 Player 已提交的推进决策；不凭 OCR 空结果授权。</summary>
    public bool HasCommittedAdvance(LiveDialogueDecisionRequest request, LiveDialogueEvidenceSourceDto source) =>
        HasCommitted(request, source, null);

    /// <summary>在单一只读快照中核验 Player 会话、决策收据和来源。</summary>
    private bool HasCommitted(LiveDialogueDecisionRequest request, LiveDialogueEvidenceSourceDto source,
        LiveDialogueOcrCandidateDto? candidate)
    {
        try
        {
            if (!Current() || _databasePath is null || _scope is null || request.Action is not ("select" or "advance")
                || request.EventSessionId is null || request.DecisionId is null || request.SourceEventId is null
                || request.FrameId is null || request.FrameSha256 is null
                || (request.Action == "select" && (request.OptionId is null || request.OptionText is null
                    || request.OptionIndex is null || candidate is null || candidate.Points.Count != 4))
                || (request.Action == "advance" && (request.OptionId is not null || request.OptionText is not null
                    || request.OptionIndex is not null || candidate is not null))
                || source.OriginalWidth <= 0 || source.OriginalHeight <= 0
                || !request.EventSessionId.EndsWith(":" + request.PlayerGeneration.ToString(CultureInfo.InvariantCulture),
                    StringComparison.Ordinal)) return false;

            var builder = new SqliteConnectionStringBuilder
            {
                DataSource = _databasePath, Mode = SqliteOpenMode.ReadOnly, Pooling = false, DefaultTimeout = 1
            };
            using var db = new SqliteConnection(builder.ToString());
            db.Open();
            using var transaction = db.BeginTransaction(deferred: true);
            var binding = ReadProjection(db, transaction, "projections", request.EventSessionId,
                "cognitive.session_owner.v1");
            if (binding is null) return false;
            var b = binding.Value.State;
            var bindingId = String(b, "event_id");
            if (b.ValueKind != JsonValueKind.Object || b.EnumerateObject().Count() != 4
                || Number(b, "version") != 1 || String(b, "session_id") != request.EventSessionId
                || String(b, "player_scope") != _scope || string.IsNullOrWhiteSpace(bindingId)) return false;
            var bindingEvent = ReadEvent(db, transaction, request.EventSessionId, bindingId);
            if (bindingEvent is null || bindingEvent.Value.Seq != binding.Value.Revision
                || bindingEvent.Value.Type != "cognitive.session_binding"
                || bindingEvent.Value.Source != "player_cognitive_transaction"
                || Number(bindingEvent.Value.Payload, "version") != 1
                || String(bindingEvent.Value.Payload, "event_id") != bindingId
                || String(bindingEvent.Value.Payload, "session_id") != request.EventSessionId
                || String(bindingEvent.Value.Payload, "player_scope") != _scope) return false;

            var turn = ReadEvent(db, transaction, request.EventSessionId, request.DecisionId);
            var visual = ReadEvent(db, transaction, request.EventSessionId, request.SourceEventId);
            var choice = request.Action == "select"
                ? ReadEvent(db, transaction, request.EventSessionId, request.OptionId!) : null;
            if (turn is null || visual is null || (request.Action == "select" && choice is null)
                || turn.Value.Seq <= binding.Value.Revision
                || (choice is not null && choice.Value.Seq >= visual.Value.Seq) || visual.Value.Seq >= turn.Value.Seq
                || turn.Value.Type != "cognitive.turn" || turn.Value.Source != "player_cognitive_transaction"
                || turn.Value.CausedBy != request.SourceEventId
                || String(turn.Value.Payload, "player_scope") != _scope
                || String(turn.Value.Payload, "visibility") != "player_knowledge"
                || String(turn.Value.Payload, "verification") != "subjective_unverified"
                || string.IsNullOrWhiteSpace(String(turn.Value.Payload, "thought"))
                || String(turn.Value.Payload, "source_event_id") != request.SourceEventId
                || Number(turn.Value.Payload, "source_seq") != visual.Value.Seq) return false;
            var decision = Property(turn.Value.Payload, "dialogue_decision");
            if (String(decision, "action") != request.Action || String(decision, "status") != "proposed_not_executed"
                || String(decision, "frame_id") != request.FrameId || String(decision, "option_id") != request.OptionId
                || (request.Action == "advance" && Property(decision, "option_id").ValueKind is not
                    (JsonValueKind.Undefined or JsonValueKind.Null)))
                return false;

            var current = ReadProjection(db, transaction, "scoped_projections", _scope, "cognitive.v1");
            var receiptName = "cognitive.receipt.turn." + Convert.ToHexString(
                SHA256.HashData(Encoding.UTF8.GetBytes(request.DecisionId))).ToLowerInvariant();
            var receipt = ReadProjection(db, transaction, "scoped_projections", _scope, receiptName);
            if (current is null || receipt is null || Number(current.Value.State, "version") != 2
                || current.Value.Revision < turn.Value.Seq || receipt.Value.Revision != turn.Value.Seq
                || String(receipt.Value.State, "event_id") != request.DecisionId
                || String(receipt.Value.State, "session_id") != request.EventSessionId
                || String(receipt.Value.State, "player_scope") != _scope
                || String(receipt.Value.State, "source_event_id") != request.SourceEventId) return false;

            if (!ValidObservation(visual.Value, request, request.SourceEventId, "visual", "observe:" + request.FrameId + ":sha256:" + request.FrameSha256)
                || !Indexed(db, transaction, request.EventSessionId, visual.Value.Seq)) return false;
            if (request.Action == "advance")
            {
                transaction.Commit();
                return Current();
            }
            if (candidate is null || choice is null || request.OptionId is null
                || !ValidObservation(choice.Value, request, request.OptionId, "choice", "frame:" + request.FrameId + ":sha256:" + request.FrameSha256)
                || !Indexed(db, transaction, request.EventSessionId, choice.Value.Seq)
                || Number(choice.Value.Payload, "choice_index") != request.OptionIndex
                || String(choice.Value.Payload, "text") != request.OptionText
                || string.Join(" ", (candidate.Text ?? string.Empty).Split((char[]?)null,
                    StringSplitOptions.RemoveEmptyEntries)) != request.OptionText) return false;
            var bounds = Property(choice.Value.Payload, "choice_bounds");
            var left = candidate.Points.Min(p => p.X);
            var right = candidate.Points.Max(p => p.X);
            var top = candidate.Points.Min(p => p.Y);
            var bottom = candidate.Points.Max(p => p.Y);
            if (!Close(bounds, "x", (left + right) / (2 * source.OriginalWidth))
                || !Close(bounds, "y", (top + bottom) / (2 * source.OriginalHeight))
                || !Close(bounds, "width", (right - left) / source.OriginalWidth)
                || !Close(bounds, "height", (bottom - top) / source.OriginalHeight)) return false;
            transaction.Commit();
            return Current();
        }
        catch { return false; }
    }

    /// <summary>只有固定的宿主配置仍有效时才可读取证明。</summary>
    private bool Current()
    {
        if (Volatile.Read(ref _invalidated) != 0 || _databasePath is null || _scope is null
            || !File.Exists(_databasePath)) return false;
        var root = _getConfig();
        return ReferenceEquals(root, _root) && ReferenceEquals(root?.PlayerEvidence, _evidence)
            && ReferenceEquals(root?.DialogueActionAuthorization, _action)
            && ReferenceEquals(root?.DialogueCandidate, _candidate)
            && ReferenceEquals(_candidate?.Message, _message) && ReferenceEquals(_candidate?.Options, _options)
            && _evidence?.DatabasePath is string path && Path.GetFullPath(path) == _databasePath
            && _evidence.PlayerScope == _scope && _action is not null && _action.AllowSelect == _selectAllowed
            && _action.AllowAdvance == _advanceAllowed && _action.AdvanceTemplateCertified == _advanceCertified
            && _action.OptionCoverageConfirmed == _coverageConfirmed
            && _action.AdvanceTemplatePath == _advanceTemplatePath
            && ReferenceEquals(_action.AdvanceRegion, _advanceRegionConfig)
            && ReferenceEquals(_action.PossibleOptionsRegion, _possibleOptionsConfig);
    }

    /// <summary>读取固定表的单条投影；参数始终绑定，不拼接请求值。</summary>
    private static (JsonElement State, long Revision)? ReadProjection(SqliteConnection db, SqliteTransaction tx,
        string table, string value, string name)
    {
        using var command = db.CreateCommand();
        command.Transaction = tx;
        command.CommandText = table == "projections"
            ? "SELECT state,revision FROM projections WHERE session_id=$key AND name=$name AND length(state)<=8388608"
            : "SELECT state,revision FROM scoped_projections WHERE scope=$key AND name=$name AND length(state)<=8388608";
        command.Parameters.AddWithValue("$key", value);
        command.Parameters.AddWithValue("$name", name);
        using var reader = command.ExecuteReader();
        if (!reader.Read()) return null;
        using var doc = JsonDocument.Parse(reader.GetString(0));
        return (doc.RootElement.Clone(), reader.GetInt64(1));
    }

    /// <summary>读取由 Player 写入的事件原文。</summary>
    private static (long Seq, string Type, string Source, string Timestamp, string? CausedBy, JsonElement Payload)?
        ReadEvent(SqliteConnection db, SqliteTransaction tx, string session, string eventId)
    {
        using var command = db.CreateCommand();
        command.Transaction = tx;
        command.CommandText = "SELECT seq,type,source,timestamp,caused_by,payload FROM events WHERE session_id=$session AND event_id=$id AND length(payload)<=1048576";
        command.Parameters.AddWithValue("$session", session);
        command.Parameters.AddWithValue("$id", eventId);
        using var reader = command.ExecuteReader();
        if (!reader.Read()) return null;
        using var doc = JsonDocument.Parse(reader.GetString(5));
        return (reader.GetInt64(0), reader.GetString(1), reader.GetString(2), reader.GetString(3),
            reader.IsDBNull(4) ? null : reader.GetString(4), doc.RootElement.Clone());
    }

    /// <summary>仅已登记的 Player 可见观察可作为来源。</summary>
    private static bool ValidObservation((long Seq, string Type, string Source, string Timestamp, string? CausedBy,
        JsonElement Payload) item, LiveDialogueDecisionRequest request, string eventId, string channel, string source)
    {
        var p = item.Payload;
        return item.Type == "experience.raw_observation" && item.Source == "player_experience"
            && DateTimeOffset.TryParse(item.Timestamp, CultureInfo.InvariantCulture, DateTimeStyles.None, out var at)
            && at.ToUnixTimeMilliseconds() == request.CapturedAtMs
            && String(p, "kind") == "raw_observation" && String(p, "visibility") == "player"
            && String(p, "session_id") == request.EventSessionId && String(p, "event_id") == eventId
            && String(p, "channel") == channel
            && String(p, "source_id") == source && !string.IsNullOrWhiteSpace(String(p, "text"));
    }

    /// <summary>确认观察已经登记到该会话的经历索引。</summary>
    private static bool Indexed(SqliteConnection db, SqliteTransaction tx, string session, long seq)
    {
        using var command = db.CreateCommand();
        command.Transaction = tx;
        command.CommandText = "SELECT COUNT(*) FROM experience_entries WHERE session_id=$session AND seq=$seq";
        command.Parameters.AddWithValue("$session", session);
        command.Parameters.AddWithValue("$seq", seq);
        return Convert.ToInt64(command.ExecuteScalar(), CultureInfo.InvariantCulture) == 1;
    }

    /// <summary>提取 JSON 对象属性；缺失即未知。</summary>
    private static JsonElement Property(JsonElement item, string key) =>
        item.ValueKind == JsonValueKind.Object && item.TryGetProperty(key, out var value) ? value : default;
    /// <summary>提取 JSON 字符串；错误类型为未知。</summary>
    private static string? String(JsonElement item, string key)
    {
        var value = Property(item, key);
        return value.ValueKind == JsonValueKind.String ? value.GetString() : null;
    }
    /// <summary>提取 JSON 整数；错误类型为未知。</summary>
    private static long? Number(JsonElement item, string key)
    {
        var value = Property(item, key);
        return value.ValueKind == JsonValueKind.Number && value.TryGetInt64(out var number) ? number : null;
    }
    /// <summary>比较 Player 持久化的归一化框与宿主候选推导的归一化框。</summary>
    private static bool Close(JsonElement item, string key, double expected)
    {
        var value = Property(item, key);
        return value.ValueKind == JsonValueKind.Number && value.TryGetDouble(out var actual)
            && double.IsFinite(actual) && double.IsFinite(expected) && Math.Abs(actual - expected) <= 1e-9;
    }

    /// <summary>本机授权、数据库或校准替换后撤销。</summary>
    private void OnRootChanged(object? sender, PropertyChangedEventArgs args)
    {
        if (string.IsNullOrEmpty(args.PropertyName) || args.PropertyName is nameof(ExternalAccessConfig.PlayerEvidence)
            or nameof(ExternalAccessConfig.DialogueActionAuthorization) or nameof(ExternalAccessConfig.DialogueCandidate))
            Interlocked.Exchange(ref _invalidated, 1);
    }
    /// <summary>任一本机细项变化后撤销。</summary>
    private void Invalidate(object? sender, PropertyChangedEventArgs args) => Interlocked.Exchange(ref _invalidated, 1);
    /// <summary>释放配置订阅并撤销授权。</summary>
    public void Dispose()
    {
        Interlocked.Exchange(ref _invalidated, 1);
        if (_root is not null) _root.PropertyChanged -= OnRootChanged;
        if (_evidence is not null) _evidence.PropertyChanged -= Invalidate;
        if (_action is not null) _action.PropertyChanged -= Invalidate;
        if (_candidate is not null) _candidate.PropertyChanged -= Invalidate;
        if (_message is not null) _message.PropertyChanged -= Invalidate;
        if (_options is not null && !ReferenceEquals(_options, _message)) _options.PropertyChanged -= Invalidate;
        if (_advanceRegionConfig is not null) _advanceRegionConfig.PropertyChanged -= Invalidate;
        if (_possibleOptionsConfig is not null && !ReferenceEquals(_possibleOptionsConfig, _advanceRegionConfig))
            _possibleOptionsConfig.PropertyChanged -= Invalidate;
        lock (_templateGate)
        {
            _advanceTemplate?.Dispose();
            _advanceTemplate = null;
            _templateStream?.Dispose();
            _templateStream = null;
        }
    }
}
