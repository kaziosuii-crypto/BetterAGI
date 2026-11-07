namespace BetterGenshinImpact.Service.ExternalAccess.Agent;

/// <summary>
/// 智能体适配器使用的固定限制。所有限制在能力服务和 HTTP/MCP 入口之外统一生效，
/// 用于把外部智能体的操作约束在可审计、可中断的范围内。
/// </summary>
public static class AgentLimits
{
    /// <summary>
    /// 单次请求允许的最大动作数量。
    /// </summary>
    public const int MaxActionsPerRequest = 64;

    /// <summary>
    /// 单次动作序列允许的最大总时长（毫秒）。超时后取消剩余动作并释放任务锁，
    /// 防止大量 wait 动作长时间阻塞 BetterGI 的独立任务。
    /// </summary>
    public const int MaxSequenceDurationMs = 120_000;

    /// <summary>
    /// 单个 wait 动作允许的最大等待时间（毫秒）。
    /// </summary>
    public const int MaxWaitMs = 30_000;

    /// <summary>
    /// 单次文本输入允许的最大字符数。
    /// </summary>
    public const int MaxTextLength = 200;

    /// <summary>
    /// 两次输入动作之间的最小间隔（毫秒）。游戏响应通常为 30～60 毫秒。
    /// </summary>
    public const int MinActionIntervalMs = 40;

    /// <summary>
    /// 拖拽动作允许的最大持续时间（毫秒）。
    /// </summary>
    public const int MaxDragDurationMs = 10_000;

    /// <summary>
    /// 单次滚轮动作允许的最大刻度数。
    /// </summary>
    public const int MaxScrollClicks = 10;

    /// <summary>
    /// 默认返回给智能体的截图最大宽度（像素）。原图更大时会等比缩小。
    /// </summary>
    public const int DefaultMaxImageWidth = 1280;

    /// <summary>
    /// 允许请求的最大截图宽度（像素）。
    /// </summary>
    public const int MaxImageWidth = 1920;

    /// <summary>
    /// 单个会话内存的最大序列化字节数。超出后拒绝写入，避免内存文件无限增长。
    /// </summary>
    public const int MaxMemoryBytes = 1_000_000;

    /// <summary>
    /// 单个内存键允许的最大字符数。
    /// </summary>
    public const int MaxMemoryKeyLength = 128;

    /// <summary>
    /// 单个检查点说明允许的最大字符数。
    /// </summary>
    public const int MaxCheckpointNoteLength = 512;

    /// <summary>
    /// 单个会话允许保留的最大检查点数量。
    /// </summary>
    public const int MaxCheckpointsPerSession = 50;

    /// <summary>
    /// 单张截图允许的最大字节数。
    /// </summary>
    public const int MaxFrameBytes = 20_000_000;

    /// <summary>
    /// 单个会话保留的截图数量上限，超出后删除最早的文件。
    /// </summary>
    public const int MaxFramesPerSession = 300;

    /// <summary>
    /// 会话根目录下允许保留的最大会话数量，避免无限创建会话耗尽磁盘。
    /// </summary>
    public const int MaxSessions = 100;

    /// <summary>
    /// 单个会话保留的事件条数上限，超出后压缩为最近的若干条。
    /// </summary>
    public const int MaxEventsPerSession = 20_000;

    /// <summary>
    /// 事件压缩后保留的最近事件条数。
    /// </summary>
    public const int EventCompactionKeep = 10_000;

    /// <summary>
    /// 未指定会话标识时使用的默认会话。
    /// </summary>
    public const string DefaultSessionId = "default";

    /// <summary>
    /// 会话文件相对程序目录的保存位置。
    /// </summary>
    public const string SessionRootRelativePath = @"User\AgentSessions";

    /// <summary>
    /// 智能体适配器在外部访问 API 中的版本号。
    /// </summary>
    public const string ApiVersion = "v1";
}
