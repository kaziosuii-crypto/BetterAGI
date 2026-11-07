using BetterGenshinImpact.Core.Recognition.OCR.Paddle;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Live;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Media;
using BetterGenshinImpact.Service.ExternalAccess.Agent.Models;

namespace BetterGenshinImpact.Service.ExternalAccess.Agent.Vision;

/// <summary>
/// 从一帧画面中提取 UI 状态与 OCR 结果。实现只能使用图像识别，不允许读取游戏内存或封包。
/// </summary>
public interface IAgentVisionService
{
    /// <summary>
    /// 分析一帧画面。
    /// </summary>
    /// <param name="frame">要分析的帧。</param>
    /// <param name="includeUiState">是否计算主界面、秘境等布尔状态。</param>
    /// <param name="includeOcr">是否执行 OCR。</param>
    /// <param name="ocrRoi">OCR 关注区域，使用原始截图像素坐标；为空时识别整帧。</param>
    /// <returns>UI 状态与 OCR 结果。</returns>
    AgentVisionDto Analyze(AgentFrame frame, bool includeUiState, bool includeOcr, AgentRoiDto? ocrRoi);

    /// <summary>同一帧上的两区域诊断；未实现时返回未知，不授权退出。</summary>
    LiveDialogueObservationDto? AnalyzeDialogue(AgentFrame frame, LiveDialogueCalibrationDto calibration) => null;

    /// <summary>
    /// 观测截图像素区域内未经识别分数过滤的检测框。坐标相对于区域左上角；
    /// 返回零检测不表示区域无字。未实现的服务返回不可用，而非空结果。
    /// </summary>
    PaddleOcrObservation ObserveOcrRegion(AgentFrame frame, AgentRoiDto roi) =>
        PaddleOcrObservation.Unavailable("该视觉服务不提供原始 OCR 观测。");
}
