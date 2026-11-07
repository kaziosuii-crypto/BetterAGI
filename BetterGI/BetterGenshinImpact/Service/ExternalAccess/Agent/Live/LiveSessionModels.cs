using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Models;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Live;

/// <summary>
/// 启动实时会话的请求。
/// </summary>
public sealed class LiveStartRequest
{
    /// <summary>会话标识；为空时使用默认会话。</summary>
    public string? SessionId { get; set; }

    /// <summary>画面广播帧率，默认 10，范围 2～15。</summary>
    public int Fps { get; set; } = 10;

    /// <summary>JPEG 质量，范围 30～95。</summary>
    public int Quality { get; set; } = 70;

    /// <summary>广播图像最大宽度，默认 1280，最大 1920。</summary>
    public int MaxWidth { get; set; } = 1280;

    /// <summary>启动或复用会话时先激活严格安全模式；默认 false 保持 v1 行为。</summary>
    public bool RequireStrictSafety { get; set; }

    /// <summary>启动或复用会话时先要求 Live 动作租约；当前会话内不可降级。</summary>
    public bool RequireControlLease { get; set; }

    /// <summary>可选的对话消息与选项区域；未经独立认证的校准仅供观察，不提供动作授权。</summary>
    public LiveDialogueCalibrationDto? DialogueCalibration { get; set; }

    /// <summary>是否把缩略帧写入会话目录，默认关闭。</summary>
    public bool RecordFrames { get; set; }

    /// <summary>写入缩略帧的帧率，默认 1。</summary>
    public int RecordFps { get; set; } = 1;

    /// <summary>
    /// 允许在游戏窗口不处于前台时用 PostMessage 投递移动按键；默认关闭。
    /// 只对移动按键生效，对话选择与绝对点击仍然要求前台。
    /// </summary>
    public bool AllowBackgroundInput { get; set; }
}

/// <summary>
/// 实时会话状态。
/// </summary>
public sealed class LiveStatusDto
{
    /// <summary>是否运行中。</summary>
    public required bool Running { get; init; }

    /// <summary>会话标识。</summary>
    public string? SessionId { get; init; }

    /// <summary>广播帧率。</summary>
    public required int Fps { get; init; }

    /// <summary>最新帧序号。</summary>
    public required long FrameSeq { get; init; }

    /// <summary>最新帧时间（UTC）。</summary>
    public DateTimeOffset? LastFrameAtUtc { get; init; }

    /// <summary>当前 WebSocket 客户端数。</summary>
    public required int Clients { get; init; }

    /// <summary>最新指导文本。</summary>
    public string? LatestGuidance { get; init; }

    /// <summary>当前按下的键。</summary>
    public required IReadOnlyList<string> PressedKeys { get; init; }

    /// <summary>待执行动作块数量。</summary>
    public required int ActionQueueDepth { get; init; }

    /// <summary>动作队列是否处于饥饿状态。</summary>
    public required bool Starved { get; init; }

    /// <summary>已执行动作块数量。</summary>
    public required long AppliedActions { get; init; }

    /// <summary>因队列过长被丢弃的动作块数量。</summary>
    public required long DroppedActions { get; init; }

    /// <summary>时间线最新序号。</summary>
    public required long TimelineSeq { get; init; }

    /// <summary>当前的安全能力和观测；不保证后续帧或输入安全。</summary>
    public LiveSafetyStatusDto Safety { get; init; } = new();
}

/// <summary>实时安全能力与当前观测；所有未知值均不可用于授权。</summary>
public sealed class LiveSafetyStatusDto
{
    /// <summary>严格模式能力协议版本，1 表示支持 fail-closed v2 门。</summary>
    public int CapabilityVersion { get; init; } = 1;

    /// <summary>严格模式是否已激活；未激活时旧协议仍可使用。</summary>
    public bool StrictModeActive { get; init; }

    /// <summary>对话门：unknown、unarmed、blocked、open 或 latched；仅为当前观测。</summary>
    public string DialogueGate { get; init; } = "unknown";

    /// <summary>识别健康：not_running、unknown、pending、unavailable、stale、duplicate、healthy。</summary>
    public string FrameRecognitionHealth { get; init; } = "not_running";

    /// <summary>此观测对应的帧序号，不同于动作授权。</summary>
    public long? FrameSeq { get; init; }

    /// <summary>该帧采集开始时间（UTC）。</summary>
    public DateTimeOffset? FrameTimestampUtc { get; init; }

    /// <summary>严格模式当前检查是否通过；不能保证后续状态。</summary>
    public bool ReadyForV2 { get; init; }

    /// <summary>本会话是否启用后台输入（PostMessage）；系统接受不代表游戏响应。</summary>
    public bool BackgroundInput { get; init; }

    /// <summary>跟踪表为空、释放调用已返回且无在途输入；不证明实际投递。</summary>
    public bool InputReleaseCallsReturned { get; init; }

    /// <summary>是否能确认跟踪的按下请求已实际释放；无后端回执时为 false。</summary>
    public bool InputReleaseConfirmed { get; init; }

    /// <summary>最近一次当前仍有效的系统释放快照；不代表游戏响应或其他输入源隔离。</summary>
    public LiveSystemReleaseReceiptDto? SystemReleaseReceipt { get; init; }

    /// <summary>本机配置的正向主界面模板能力是否可用，不保证实机识别正确。</summary>
    public bool CanConfirmDialogueExit { get; init; }

    /// <summary>同会话连续独立的正向主界面模板匹配帧数。</summary>
    public int DialogueExitEvidenceFrames { get; init; }

    /// <summary>连续低结构候选帧数；不是可靠无字证据，不能解除锁。</summary>
    public int DialogueLowStructureCandidateFrames { get; init; }

    /// <summary>解除对话锁所需的连续独立帧数。</summary>
    public int DialogueExitRequiredFrames { get; init; }

    /// <summary>最近同帧区域观测；不构成授权。</summary>
    public LiveDialogueObservationDto? DialogueObservation { get; init; }

    /// <summary>本机独立对话动作能力是否配置；不构成具体动作授权。</summary>
    public bool CanAuthorizeDialogueActions { get; init; }
}

/// <summary>
/// 设置指导文本的请求。
/// </summary>
public sealed class LiveGuidanceRequest
{
    /// <summary>指导文本。</summary>
    public string Text { get; set; } = string.Empty;

    /// <summary>来源，例如 dsh、director。</summary>
    public string Source { get; set; } = "dsh";
}

/// <summary>
/// 追加解说的请求。
/// </summary>
public sealed class LiveCommentaryRequest
{
    /// <summary>解说类型：reaction、plan、emotion、lore、chat、result。</summary>
    public string Kind { get; set; } = "reaction";

    /// <summary>解说文本。</summary>
    public string Text { get; set; } = string.Empty;

    /// <summary>情绪标签，例如 calm、excited、surprised。</summary>
    public string? Emotion { get; set; }

    /// <summary>关联的帧序号。</summary>
    public long? FrameSeq { get; set; }

    /// <summary>附加元数据。</summary>
    public JsonObject? Meta { get; set; }
}

/// <summary>
/// 时间线事件，用于 VTuber 叠加层和离线渲染。
/// </summary>
public sealed class LiveTimelineEventDto
{
    /// <summary>会话内递增序号。</summary>
    public required long Seq { get; init; }

    /// <summary>事件时间（UTC）。</summary>
    public required DateTimeOffset TimestampUtc { get; init; }

    /// <summary>事件类型：guidance、commentary、action_state、live.started、live.stopped。</summary>
    public required string Type { get; init; }

    /// <summary>解说类型。</summary>
    public string? Kind { get; init; }

    /// <summary>文本内容。</summary>
    public string? Text { get; init; }

    /// <summary>情绪标签。</summary>
    public string? Emotion { get; init; }

    /// <summary>关联帧序号。</summary>
    public long? FrameSeq { get; init; }

    /// <summary>附加元数据。</summary>
    public JsonObject? Meta { get; init; }
}

/// <summary>
/// 一个 33ms 动作键块。
/// </summary>
public sealed class LiveActionChunkDto
{
    /// <summary>相对鼠标 X 位移。</summary>
    public int Dx { get; set; }

    /// <summary>相对鼠标 Y 位移。</summary>
    public int Dy { get; set; }

    /// <summary>滚轮刻度。</summary>
    public int Scroll { get; set; }

    /// <summary>该键块按下的键，例如 W、Shift、LB。</summary>
    public List<string> Keys { get; set; } = [];

    /// <summary>不能静默忽略的未知键块字段。</summary>
    [JsonExtensionData]
    public Dictionary<string, JsonElement>? UnsupportedFields { get; set; }
}

/// <summary>
/// 1280×720 输出帧中的绝对光标位置。
/// </summary>
public sealed class LiveAbsolutePointDto
{
    /// <summary>X 像素。</summary>
    public int X { get; set; }

    /// <summary>Y 像素。</summary>
    public int Y { get; set; }

    /// <summary>不能静默忽略的未知坐标字段。</summary>
    [JsonExtensionData]
    public Dictionary<string, JsonElement>? UnsupportedFields { get; set; }
}

/// <summary>
/// 一个 200ms 动作块，包含最多 6 个 33ms 键块。
/// </summary>
public sealed class LiveActionBlockDto
{
    /// <summary>动作块序号，由调用方生成。</summary>
    public long? Seq { get; set; }

    /// <summary>6 个键块。</summary>
    public List<LiveActionChunkDto> Chunks { get; set; } = [];

    /// <summary>绝对光标位置；为空时使用相对位移。</summary>
    public LiveAbsolutePointDto? Absolute { get; set; }

    /// <summary>调用方提供的意图标识，不表示动作已执行。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? IntentId { get; set; }

    /// <summary>调用方提供的非负动作代次。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    [JsonNumberHandling(JsonNumberHandling.Strict)]
    public long? MotorGeneration { get; set; }

    /// <summary>调用方提供的动作组标识。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ActionGroupId { get; set; }

    /// <summary>显式动作类型；目前没有实现任何带类型的对话动作。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ActionKind { get; set; }

    /// <summary>动作类型别名；当前不支持。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? Kind { get; set; }

    /// <summary>不能静默忽略的未知动作字段。</summary>
    [JsonExtensionData]
    public Dictionary<string, JsonElement>? UnsupportedFields { get; set; }

    /// <summary>模型思考文本（可选）。</summary>
    public string? Thought { get; set; }
}

/// <summary>
/// 动作入队结果；接受或回显不代表动作已执行。
/// </summary>
public sealed class LiveActionAckDto
{
    /// <summary>是否接受。</summary>
    public required bool Accepted { get; init; }

    /// <summary>动作块序号。</summary>
    public long? Seq { get; init; }

    /// <summary>调用方提供的意图标识。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? IntentId { get; init; }

    /// <summary>调用方提供的动作代次。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public long? MotorGeneration { get; init; }

    /// <summary>调用方提供的动作组标识。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ActionGroupId { get; init; }

    /// <summary>当前队列深度。</summary>
    public required int QueueDepth { get; init; }

    /// <summary>已接受动作的执行尝试标识。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? AttemptId { get; init; }

    /// <summary>说明。</summary>
    public string? Message { get; init; }
}

/// <summary>键块处理记录；不表示操作系统或游戏已执行。</summary>
public sealed class LiveExecutionAttemptDto
{
    /// <summary>服务端唯一的动作尝试标识。</summary>
    public required string AttemptId { get; init; }
    /// <summary>调用方动作序号。</summary>
    public long? Seq { get; init; }
    /// <summary>调用方意图标识。</summary>
    public string? IntentId { get; init; }
    /// <summary>调用方动作代次。</summary>
    public long? MotorGeneration { get; init; }
    /// <summary>调用方动作组。</summary>
    public string? ActionGroupId { get; init; }
    /// <summary>键块从零开始；-1 表示绝对定位。</summary>
    public int ChunkIndex { get; init; }
    /// <summary>unknown、failed、aborted 或 dropped；unknown 不表示投递成功。</summary>
    public required string Status { get; init; }
    /// <summary>本次键块开始调用输入后端的次数。</summary>
    public int InputCallsStarted { get; init; }
    /// <summary>其中正常返回的次数；不证明底层投递或游戏响应。</summary>
    public int InputCallsReturned { get; init; }
    /// <summary>处理时间（UTC）。</summary>
    public DateTimeOffset TimestampUtc { get; init; }
}

/// <summary>单次后端输入的调用证据；不表示游戏已响应。</summary>
public sealed class LiveInputEvidenceDto
{
    /// <summary>所属尝试；释放重试可能为空。</summary>
    public string? AttemptId { get; init; }
    /// <summary>键块序号。</summary>
    public int? ChunkIndex { get; init; }
    /// <summary>输入操作。</summary>
    public required string Operation { get; init; }
    /// <summary>系统层回执状态，unknown 表示无法确认。</summary>
    public required string Status { get; init; }
    /// <summary>请求数量；不适用时为空。</summary>
    public int? Requested { get; init; }
    /// <summary>系统接受数量；不代表游戏响应。</summary>
    public int? Accepted { get; init; }
}

/// <summary>
/// WebSocket 客户端发给服务端的消息。使用扁平结构以简化协议。
/// </summary>
public sealed class LiveClientMessage
{
    /// <summary>消息类型：guidance、commentary、actions、stop_actions、ping。</summary>
    public string Type { get; set; } = string.Empty;

    /// <summary>指导文本或解说文本。</summary>
    public string? Text { get; set; }

    /// <summary>解说类型。</summary>
    public string? Kind { get; set; }

    /// <summary>情绪。</summary>
    public string? Emotion { get; set; }

    /// <summary>关联帧序号。</summary>
    public long? FrameSeq { get; set; }

    /// <summary>附加元数据。</summary>
    public JsonObject? Meta { get; set; }

    /// <summary>动作块。</summary>
    public LiveActionBlockDto? Block { get; set; }

    /// <summary>动作块序号。</summary>
    public long? Seq { get; set; }

    /// <summary>动作键块（兼容直接发送）。</summary>
    public List<LiveActionChunkDto>? Chunks { get; set; }

    /// <summary>绝对光标（兼容直接发送）。</summary>
    public LiveAbsolutePointDto? Absolute { get; set; }

    /// <summary>调用方提供的意图标识，支持扁平动作消息。</summary>
    public string? IntentId { get; set; }

    /// <summary>调用方提供的非负动作代次，支持扁平动作消息。</summary>
    [JsonNumberHandling(JsonNumberHandling.Strict)]
    public long? MotorGeneration { get; set; }

    /// <summary>调用方提供的动作组标识，支持扁平动作消息。</summary>
    public string? ActionGroupId { get; set; }

    /// <summary>显式动作类型；当前不支持对话动作。</summary>
    public string? ActionKind { get; set; }

    /// <summary>租约绑定请求标识；仅用于绑定回执关联。</summary>
    public string? LeaseRequestId { get; set; }

    /// <summary>每次租约探测的独立标识。</summary>
    public string? LeaseProbeId { get; set; }

    /// <summary>输入租约，仅用于认证，不回显。</summary>
    public LiveLeaseCredentialsDto? Lease { get; set; }

    /// <summary>不能静默忽略的未知消息字段。</summary>
    [JsonExtensionData]
    public Dictionary<string, JsonElement>? UnsupportedFields { get; set; }

    /// <summary>模型思考文本。</summary>
    public string? Thought { get; set; }
}

/// <summary>仅用于本机 Live 输入控制的租约凭据，不进入动作回显或录制。</summary>
public sealed class LiveLeaseCredentialsDto
{
    /// <summary>本次租约的随机秘密。</summary>
    public string? Secret { get; set; }
    /// <summary>执行器实例随机标识。</summary>
    public string? InstanceId { get; set; }
    /// <summary>租约代次。</summary>
    public long Epoch { get; set; }
    /// <summary>不能忽略的未知凭据字段。</summary>
    [JsonExtensionData]
    public Dictionary<string, JsonElement>? UnsupportedFields { get; set; }
}

/// <summary>唯一持有者的租约授权响应。</summary>
public sealed class LiveLeaseGrantDto
{
    /// <summary>仅向获取者返回的凭据。</summary>
    public required LiveLeaseCredentialsDto Credentials { get; init; }
    /// <summary>仅由玩家保管的续期及主动释放凭据，不得交给操作桥。</summary>
    public required LiveLeaseCredentialsDto RenewalCredentials { get; init; }

    /// <summary>服务端 TTL；续期后需以新返回值为准。</summary>
    public int ExpiresInMs { get; init; }

    /// <summary>新租约仅为 prepared；不能作为输入授权。</summary>
    public string Status { get; init; } = "prepared";
}

/// <summary>只有 Player 持有的 owner 凭据可提交已绑定租约。</summary>
public sealed class LiveLeaseCommitRequest
{
    /// <summary>仅 Player 持有的 owner 凭据。</summary>
    public LiveLeaseCredentialsDto? Credentials { get; init; }
    /// <summary>已由 Live WebSocket 绑定的请求标识。</summary>
    public string? LeaseRequestId { get; init; }
    /// <summary>绑定的 Live 会话标识。</summary>
    public string? SessionId { get; init; }
    /// <summary>绑定的 Live WebSocket 连接标识。</summary>
    public string? ConnectionId { get; init; }
    /// <summary>未知字段不得参与授权。</summary>
    [JsonExtensionData]
    public Dictionary<string, JsonElement>? UnsupportedFields { get; init; }
}

/// <summary>不含秘密的租约绑定或提交回执。</summary>
public sealed class LiveLeaseBindingDto
{
    /// <summary>当前 Live 会话。</summary>
    public required string SessionId { get; init; }
    /// <summary>Live 输入执行器身份。</summary>
    public required string InstanceId { get; init; }
    /// <summary>租约代次。</summary>
    public long Epoch { get; init; }
    /// <summary>绑定请求标识。</summary>
    public required string RequestId { get; init; }
    /// <summary>Live WebSocket 的绑定连接身份。</summary>
    public required string ConnectionId { get; init; }
    /// <summary>prepared、owner_committed 或 active；只有 active 可注入。</summary>
    public required string Status { get; init; }
    /// <summary>生成回执时的剩余单调时钟毫秒数。</summary>
    public int ExpiresInMs { get; init; }
}

/// <summary>
/// 服务端发给 WebSocket 客户端的消息。使用扁平结构并按需填充字段。
/// </summary>
public sealed class LiveServerMessage
{
    /// <summary>消息类型。</summary>
    public required string Type { get; init; }

    /// <summary>会话标识。</summary>
    public string? SessionId { get; init; }

    /// <summary>帧或事件序号；动作拒绝时为该请求中可确定的原始动作序号。</summary>
    public long? Seq { get; init; }

    /// <summary>UTC 时间戳。</summary>
    public DateTimeOffset? TimestampUtc { get; init; }

    /// <summary>广播帧率。</summary>
    public int? Fps { get; init; }

    /// <summary>图像宽度。</summary>
    public int? Width { get; init; }

    /// <summary>图像高度。</summary>
    public int? Height { get; init; }

    /// <summary>Base64 JPEG。</summary>
    public string? Jpeg { get; init; }

    /// <summary>游戏画面区域。</summary>
    public AgentCaptureAreaDto? Capture { get; init; }

    /// <summary>运行环境状态。</summary>
    public LiveRuntimeStateDto? Runtime { get; init; }

    /// <summary>帧的 UI 类别；未完成或失败时为 Unknown。</summary>
    public string? UiCategory { get; init; }

    /// <summary>同一帧的对话界面识别结果；null 表示未知。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.Never)]
    public bool? IsInTalkUi { get; init; }

    /// <summary>同一帧上校准区域的诊断，不构成授权。</summary>
    public LiveDialogueObservationDto? DialogueObservation { get; init; }

    /// <summary>最新指导。</summary>
    public string? Guidance { get; init; }

    /// <summary>指导来源。</summary>
    public string? Source { get; init; }

    /// <summary>解说类型/时间线事件类型。</summary>
    public string? Kind { get; init; }

    /// <summary>文本。</summary>
    public string? Text { get; init; }

    /// <summary>情绪。</summary>
    public string? Emotion { get; init; }

    /// <summary>关联帧序号。</summary>
    public long? FrameSeq { get; init; }

    /// <summary>当前按下的键。</summary>
    public IReadOnlyList<string>? Keys { get; init; }

    /// <summary>动作队列深度。</summary>
    public int? QueueDepth { get; init; }

    /// <summary>是否饥饿。</summary>
    public bool? Starved { get; init; }

    /// <summary>已执行动作数。</summary>
    public long? Applied { get; init; }

    /// <summary>丢弃动作数。</summary>
    public long? Dropped { get; init; }

    /// <summary>每帧动作块数量。</summary>
    public int? ChunksPerAction { get; init; }

    /// <summary>动作块间隔毫秒。</summary>
    public int? ActionIntervalMs { get; init; }

    /// <summary>调用方提供的意图标识；回显不代表执行。动作拒绝时仅回传无歧义且合法的归属。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? IntentId { get; init; }

    /// <summary>调用方提供的动作代次。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public long? MotorGeneration { get; init; }

    /// <summary>调用方提供的动作组标识。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ActionGroupId { get; init; }

    /// <summary>执行尝试记录，仅表示本地处理而非游戏结果。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public LiveExecutionAttemptDto? ExecutionAttempt { get; init; }

    /// <summary>当前宿主支持的系统输入回执归属通路，不代表游戏响应。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ExecutionFeedbackProtocol { get; init; }

    /// <summary>单次输入后端证据；系统接受不等于游戏响应。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public LiveInputEvidenceDto? InputEvidence { get; init; }

    /// <summary>已接受动作的执行尝试标识。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? AttemptId { get; init; }

    /// <summary>已接受入队的动作块；回显不代表实际执行。</summary>
    public LiveActionBlockDto? Block { get; init; }

    /// <summary>解说附加元数据；录制器用 meta.internal 区分内部思考。</summary>
    public JsonObject? Meta { get; init; }

    /// <summary>租约绑定请求标识；仅回传已验证的原始值。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? LeaseRequestId { get; init; }

    /// <summary>绑定成功的执行器实例标识，不包含租约秘密。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? LeaseInstanceId { get; init; }

    /// <summary>绑定成功的租约代次。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public long? LeaseEpoch { get; init; }

    /// <summary>绑定该租约的 Live WebSocket 连接标识。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? LeaseConnectionId { get; init; }

    /// <summary>prepared 或 owner_committed 均不得输入，active 才可输入。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? LeaseStatus { get; init; }

    /// <summary>探测回执关联的独立标识；初次绑定时为空。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.Never)]
    public string? LeaseProbeId { get; init; }

    /// <summary>宿主回执生成时的租约剩余时间。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? LeaseExpiresInMs { get; init; }

    /// <summary>有界且唯一的停止请求标识；错误或回执只回传已验证原值。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? StopRequestId { get; init; }

    /// <summary>该次同步停止调用已正常返回；不表示系统或游戏响应。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? StopCallReturned { get; init; }

    /// <summary>同次停止回执或显式标记为事后读取的安全快照。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public LiveStopSafetyObservationDto? Safety { get; init; }

    /// <summary>错误码。</summary>
    public string? Code { get; init; }

    /// <summary>错误说明。</summary>
    public string? Message { get; init; }
}

/// <summary>服务锁内绑定会话与请求的停止回执。</summary>
public sealed class LiveStopReceiptDto
{
    /// <summary>停止请求标识。</summary>
    public required string StopRequestId { get; init; }

    /// <summary>停止时的实时会话；无会话时为空。</summary>
    public string? SessionId { get; init; }

    /// <summary>执行闸门内生成的停止证据。</summary>
    public required LiveStopSafetyObservationDto Safety { get; init; }
}

/// <summary>停止时的安全证据；未知值不代表成功或失败。</summary>
public sealed class LiveStopSafetyObservationDto
{
    /// <summary>证据范围；stop_atomic 表示在本次停止的执行闸门内取得。</summary>
    public string Scope { get; init; } = "post_stop_unattributed";

    /// <summary>原子停止代次；非原子快照不提供。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public long? Epoch { get; init; }

    /// <summary>该次停止开始时受执行器跟踪的按键数量。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? TrackedKeyCount { get; init; }

    /// <summary>释放调用是否返回；无证据时为 null。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.Never)]
    public bool? InputReleaseCallsReturned { get; init; }

    /// <summary>游戏是否确认释放不可由输入调用证明；原子停止始终为 null。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.Never)]
    public bool? InputReleaseConfirmed { get; init; }

    /// <summary>本次受跟踪输入的系统释放请求已接受；未知为 null，不表示游戏响应。</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.Never)]
    public bool? SystemReleaseRequestsAccepted { get; init; }

    /// <summary>在停止闸门内生成的系统释放快照；后续读取须重新核验。</summary>
    public LiveSystemReleaseReceiptDto? SystemReleaseReceipt { get; init; }
}

/// <summary>执行器跟踪的释放请求被操作系统接受；不代表游戏或其他输入源状态。</summary>
public sealed class LiveSystemReleaseReceiptDto
{
    /// <summary>生成时的 Live 会话标识。</summary>
    public required string SessionId { get; init; }
    /// <summary>生成时的执行器租约实例标识。</summary>
    public required string LeaseInstanceId { get; init; }
    /// <summary>生成前持有的租约代次，停止会撤销该租约。</summary>
    public long LeaseEpoch { get; init; }
    /// <summary>停止动作代次。</summary>
    public long StopEpoch { get; init; }
    /// <summary>本执行器非释放输入调用代次。</summary>
    public long InputGeneration { get; init; }
    /// <summary>执行器跟踪并获得系统接受释放的按键数。</summary>
    public int ReleasedKeyCount { get; init; }
    /// <summary>生成回执的 UTC 时间。</summary>
    public DateTimeOffset TimestampUtc { get; init; }
}

/// <summary>
/// 游戏运行环境状态 DTO。
/// </summary>
public sealed class LiveRuntimeStateDto
{
    /// <summary>游戏是否存活。</summary>
    public bool IsAlive { get; init; }

    /// <summary>是否前台。</summary>
    public bool IsForeground { get; init; }

    /// <summary>是否最小化。</summary>
    public bool IsMinimized { get; init; }

    /// <summary>是否要求前台。</summary>
    public bool RequiresForeground { get; init; }
}

/// <summary>
/// 最新一帧及其元数据。
/// </summary>
public sealed class LiveFrameSnapshot
{
    /// <summary>帧序号。</summary>
    public required long Seq { get; init; }

    /// <summary>采集时间（UTC）。</summary>
    public required DateTimeOffset TimestampUtc { get; init; }

    /// <summary>JPEG 字节。</summary>
    public required byte[] JpegBytes { get; init; }

    /// <summary>宽度。</summary>
    public required int Width { get; init; }

    /// <summary>高度。</summary>
    public required int Height { get; init; }

    /// <summary>游戏画面区域。</summary>
    public required AgentCaptureAreaDto Capture { get; init; }

    /// <summary>运行环境状态。</summary>
    public required LiveRuntimeStateDto RuntimeState { get; init; }

    /// <summary>本帧的 UI 类别；识别不可用时为 Unknown。</summary>
    public string UiCategory { get; init; } = "Unknown";

    /// <summary>本帧的对话界面识别结果；null 表示未知。</summary>
    public bool? IsInTalkUi { get; init; }

    /// <summary>同一帧上校准区域的诊断，不构成授权。</summary>
    public LiveDialogueObservationDto? DialogueObservation { get; init; }

    /// <summary>原始画面与上一帧不同；缺少指纹时为 false。</summary>
    internal bool IsDistinct { get; init; }
}

/// <summary>
/// 动作执行器状态。
/// </summary>
public sealed class LiveExecutorStatus
{
    /// <summary>当前按下的键。</summary>
    public required IReadOnlyList<string> PressedKeys { get; init; }

    /// <summary>队列深度。</summary>
    public required int QueueDepth { get; init; }

    /// <summary>是否饥饿。</summary>
    public required bool Starved { get; init; }

    /// <summary>已执行动作块。</summary>
    public required long AppliedActions { get; init; }

    /// <summary>已丢弃动作块。</summary>
    public required long DroppedActions { get; init; }
}

/// <summary>
/// WebSocket 客户端连接，服务端通过有界队列推送消息。
/// </summary>
public sealed class LiveClientConnection : IDisposable
{
    /// <summary>客户端标识。</summary>
    public required string Id { get; init; }

    /// <summary>此连接最后接受的动作序号；空值表示尚未使用序号。</summary>
    public long? LastActionSeq { get; set; }

    /// <summary>此连接已使用的停止请求标识；由唯一的接收循环访问并限制数量。</summary>
    internal HashSet<string> StopRequestIds { get; } = new(StringComparer.Ordinal);

    /// <summary>本连接已经处理的租约绑定请求标识。</summary>
    internal HashSet<string> LeaseRequestIds { get; } = new(StringComparer.Ordinal);

    /// <summary>消息有界队列。</summary>
    public required System.Threading.Channels.Channel<LiveServerMessage> Messages { get; init; }

    /// <summary>尝试入队；队列满时丢弃最旧消息。</summary>
    /// <param name="message">要推送的消息。</param>
    /// <returns>是否写入成功。</returns>
    public bool TryEnqueue(LiveServerMessage message)
    {
        return Messages.Writer.TryWrite(message);
    }

    /// <summary>完成消息队列。</summary>
    public void Dispose()
    {
        Messages.Writer.TryComplete();
    }
}
