using CommunityToolkit.Mvvm.ComponentModel;
using System;
using System.ComponentModel.DataAnnotations;

namespace BetterGenshinImpact.Core.Config;

/// <summary>
/// BetterGI 本机外部访问服务配置。
/// </summary>
[Serializable]
public partial class ExternalAccessConfig : ObservableValidator
{
    /// <summary>
    /// 是否启用 HTTP API。
    /// </summary>
    [ObservableProperty]
    private bool _httpApiEnabled;

    /// <summary>
    /// 是否启用 WebSocket 实时日志。
    /// </summary>
    [ObservableProperty]
    private bool _webSocketEnabled;

    /// <summary>
    /// 是否启用 MCP Streamable HTTP 服务。
    /// </summary>
    [ObservableProperty]
    private bool _mcpEnabled;

    /// <summary>
    /// 是否启用智能体控制能力。
    /// <para>
    /// 启用后向本机 AI 智能体开放游戏窗口截图和模拟键鼠输入，不提供游戏内存读写；
    /// 默认关闭，需重启 BetterGI 后生效。
    /// </para>
    /// </summary>
    [ObservableProperty]
    private bool _agentControlEnabled;

    /// <summary>
    /// 本机对话候选诊断校准；默认未配置，不授权解锁或执行对话动作。
    /// 修改后需要重启 BetterGI 才能重新启用诊断。
    /// </summary>
    [ObservableProperty]
    private HostDialogueCandidateConfig? _dialogueCandidate;

    /// <summary>宿主启动时固定的玩家事件数据库和身份；不授予输入权限。</summary>
    [ObservableProperty]
    private HostPlayerEvidenceConfig? _playerEvidence;

    /// <summary>宿主本机显式允许已证明的选项点击，默认不允许。</summary>
    [ObservableProperty]
    private HostDialogueActionConfig? _dialogueActionAuthorization;

    /// <summary>宿主本地校准的返回主界面正向模板，默认关闭。</summary>
    [ObservableProperty]
    private HostDialogueExitConfig? _dialogueExitAuthorization;

    /// <summary>
    /// 本机外部访问服务共享端口。
    /// </summary>
    [ObservableProperty]
    [NotifyDataErrorInfo]
    [Range(1024, 65535, ErrorMessage = "端口必须在 1024 到 65535 之间。")]
    private int _port = 30648;

    /// <summary>
    /// HTTP API 与 MCP 共用的 Bearer 访问令牌。
    /// </summary>
    [ObservableProperty]
    private string _accessToken = string.Empty;
}

/// <summary>宿主本地只读 Player 事件证明配置，不可由远程请求指定。</summary>
[Serializable]
public partial class HostPlayerEvidenceConfig : ObservableObject
{
    /// <summary>事件 SQLite 文件的绝对路径。</summary>
    [ObservableProperty]
    private string _databasePath = string.Empty;

    /// <summary>固定的 Player 投影身份，格式为 player- 后跟 64 位小写十六进制。</summary>
    [ObservableProperty]
    private string _playerScope = string.Empty;
}

/// <summary>宿主本地对话输入授权；不会授权推进或解除对话锁。</summary>
[Serializable]
public partial class HostDialogueActionConfig : ObservableObject
{
    /// <summary>允许宿主点击与已提交 Player 决策严格匹配的同帧选项。</summary>
    [ObservableProperty]
    private bool _allowSelect;

    /// <summary>显式启用本地标定的仅推进画面模板，默认拒绝。</summary>
    [ObservableProperty]
    private bool _allowAdvance;

    /// <summary>用户标定的仅推进 ROI 截图绝对路径。</summary>
    [ObservableProperty]
    private string _advanceTemplatePath = string.Empty;

    /// <summary>用户确认模板来自仅可推进且不可选择选项的实机画面。</summary>
    [ObservableProperty]
    private bool _advanceTemplateCertified;

    /// <summary>模板对应原始画面归一化区域。</summary>
    [ObservableProperty]
    private HostDialogueRegionConfig? _advanceRegion;

    /// <summary>用户标定的所有可能对话选项所在归一化区域。</summary>
    [ObservableProperty]
    private HostDialogueRegionConfig? _possibleOptionsRegion;

    /// <summary>用户确认所给选项区域覆盖该界面的所有可能选项。</summary>
    [ObservableProperty]
    private bool _optionCoverageConfirmed;
}

/// <summary>经用户实机标定的仅主界面模板；默认不提供退出证明。</summary>
[Serializable]
public partial class HostDialogueExitConfig : ObservableObject
{
    /// <summary>显式启用本机退出模板。</summary>
    [ObservableProperty]
    private bool _enabled;

    /// <summary>用户标定的主界面 ROI 截图绝对路径。</summary>
    [ObservableProperty]
    private string _templatePath = string.Empty;

    /// <summary>用户确认模板来自不存在对话的实机主界面。</summary>
    [ObservableProperty]
    private bool _mainTemplateCertified;

    /// <summary>模板对应的原始画面归一化区域。</summary>
    [ObservableProperty]
    private HostDialogueRegionConfig? _region;

    /// <summary>用户标定的所有可能对话及选项区域。</summary>
    [ObservableProperty]
    private HostDialogueRegionConfig? _possibleDialogueRegion;

    /// <summary>用户确认该区域覆盖所有可能的对话和选项。</summary>
    [ObservableProperty]
    private bool _coverageConfirmed;

    /// <summary>需要的独立新鲜帧数，必须显式设置 2 至 10。</summary>
    [ObservableProperty]
    private int? _requiredFrames;
}

/// <summary>仅供本机配置的对话候选诊断，不提供任何输入授权。</summary>
[Serializable]
public partial class HostDialogueCandidateConfig : ObservableObject
{
    /// <summary>是否显式启用候选诊断，默认关闭。</summary>
    [ObservableProperty]
    private bool _enabled;

    /// <summary>消息区域的归一化坐标。</summary>
    [ObservableProperty]
    private HostDialogueRegionConfig? _message;

    /// <summary>选项区域的归一化坐标。</summary>
    [ObservableProperty]
    private HostDialogueRegionConfig? _options;

    /// <summary>候选所需连续独立帧数，须显式提供 2 至 10。</summary>
    [ObservableProperty]
    private int? _exitFrames;
}

/// <summary>本机保存的归一化区域，所有字段均须显式提供。</summary>
[Serializable]
public partial class HostDialogueRegionConfig : ObservableObject
{
    /// <summary>左上角横坐标。</summary>
    [ObservableProperty]
    private double? _x;

    /// <summary>左上角纵坐标。</summary>
    [ObservableProperty]
    private double? _y;

    /// <summary>区域宽度。</summary>
    [ObservableProperty]
    private double? _width;

    /// <summary>区域高度。</summary>
    [ObservableProperty]
    private double? _height;
}
