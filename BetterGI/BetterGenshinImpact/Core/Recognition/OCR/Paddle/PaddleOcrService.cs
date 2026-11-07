using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using BetterGenshinImpact.Core.Config;
using BetterGenshinImpact.Core.Recognition.OCR.Engine;
using BetterGenshinImpact.Core.Recognition.ONNX;
using BetterGenshinImpact.GameTask.Common.BgiVision;
using OpenCvSharp;
using OpenCvSharp.Extensions;
using YamlDotNet.Core;
using YamlDotNet.Core.Events;
using Size = OpenCvSharp.Size;

namespace BetterGenshinImpact.Core.Recognition.OCR.Paddle;

public class PaddleOcrService : IOcrService, IDisposable
{
    /// <summary>
    ///     Usage:
    ///     https://github.com/sdcb/PaddleSharp/blob/master/docs/ocr.md
    ///     模型列表:
    ///     https://github.com/PaddlePaddle/PaddleOCR/blob/release/2.5/doc/doc_ch/models_list.md
    /// </summary>
    private readonly Det _localDetModel;

    private readonly Rec _localRecModel;
    private readonly object _inferenceLock = new();
    private bool _disposed;

    private float _dropScore = 0.5f;

    /// <summary>Gets or sets the official recognition score threshold used to discard low-confidence results.</summary>
    public float DropScore
    {
        get { lock (_inferenceLock) return _dropScore; }
        set { lock (_inferenceLock) _dropScore = value; }
    }

    public record PaddleOcrModelType(
        BgiOnnxModel DetectionModel,
        OcrVersionConfig DetectionVersion,
        BgiOnnxModel RecognitionModel,
        OcrVersionConfig RecognitionVersion,
        Func<IReadOnlyList<string>> RecLabel,
        String PreHeatImagePath
    )
    {
        public static string TestImagePath = Global.Absolute(@"Assets\Model\PaddleOCR\test_pp_ocr.png");

        public static string TestNumberImagePath =
            Global.Absolute(@"Assets\Model\PaddleOCR\test_pp_ocr_number.png");

        private static readonly Func<BgiOnnxModel, IReadOnlyList<string>> DefaultRecLabelFunc =
            recModel =>
            {
                const string modelConfigFileName = "inference.yml";
                var configFilePath = Path.Combine(
                    Path.GetDirectoryName(recModel.ModalPath) ??
                    throw new InvalidOperationException("Cannot get model directory"),
                    modelConfigFileName);

                if (!File.Exists(configFilePath))
                    throw new FileNotFoundException(
                        $"PaddleOCR config file {modelConfigFileName} not found: {configFilePath}");

                using var reader = new StreamReader(configFilePath);
                var parser = new Parser(reader);

                // Traverse YAML to find PostProcess:character_dict
                while (parser.MoveNext())
                {
                    if (parser.Current is not YamlDotNet.Core.Events.Scalar { Value: "PostProcess" }) continue;
                    parser.MoveNext(); // Should be MappingStart
                    while (parser.MoveNext())
                    {
                        if (parser.Current is not YamlDotNet.Core.Events.Scalar { Value: "character_dict" }) continue;
                        parser.MoveNext(); // Should be SequenceStart
                        var result = new List<string>();
                        while (parser.MoveNext())
                        {
                            switch (parser.Current)
                            {
                                case SequenceEnd:
                                    return result;
                                case YamlDotNet.Core.Events.Scalar charScalar:
                                    result.Add(charScalar.Value);
                                    break;
                            }
                        }
                    }
                }

                throw new InvalidOperationException("未在 YAML 的 PostProcess 部分找到 character_dict。");
            };


        private static PaddleOcrModelType Create(
            BgiOnnxModel detectionModel,
            OcrVersionConfig detectionVersion,
            BgiOnnxModel recognitionModel,
            OcrVersionConfig recognitionVersion,
            String? preHeatImagePath = null,
            Func<IReadOnlyList<string>>? recLabel = null
        )
        {
            return new PaddleOcrModelType(
                detectionModel,
                detectionVersion,
                recognitionModel,
                recognitionVersion,
                recLabel ?? (() => DefaultRecLabelFunc(recognitionModel)),
                preHeatImagePath ?? TestImagePath);
        }

        public (Det, Rec) Build(BgiOnnxSessionFactory onnxFactory)
        {
            var detection = new Det(DetectionModel, DetectionVersion, onnxFactory);
            try { return (detection, new Rec(RecognitionModel, RecLabel(), RecognitionVersion, onnxFactory)); }
            catch { detection.Dispose(); throw; }
        }

        public static readonly PaddleOcrModelType V4 = Create(
            BgiOnnxModel.PaddleOcrDetV4,
            OcrVersionConfig.PpOcrV4,
            BgiOnnxModel.PaddleOcrRecV4,
            OcrVersionConfig.PpOcrV4);

        public static readonly PaddleOcrModelType V4En = Create(
            BgiOnnxModel.PaddleOcrDetV4,
            OcrVersionConfig.PpOcrV4,
            BgiOnnxModel.PaddleOcrRecV4En,
            OcrVersionConfig.PpOcrV4,
            TestNumberImagePath);

        public static readonly PaddleOcrModelType V5 = Create(
            BgiOnnxModel.PaddleOcrDetV5,
            OcrVersionConfig.PpOcrV5,
            BgiOnnxModel.PaddleOcrRecV5,
            OcrVersionConfig.PpOcrV5);

        public static readonly PaddleOcrModelType V6 = Create(
            BgiOnnxModel.PaddleOcrDetV6,
            OcrVersionConfig.PpOcrV6,
            BgiOnnxModel.PaddleOcrRecV6,
            OcrVersionConfig.PpOcrV6);

        public static readonly PaddleOcrModelType V5Latin = Create(
            BgiOnnxModel.PaddleOcrDetV5,
            OcrVersionConfig.PpOcrV5,
            BgiOnnxModel.PaddleOcrRecV5Latin,
            OcrVersionConfig.PpOcrV5);

        public static readonly PaddleOcrModelType V5Eslav = Create(
            BgiOnnxModel.PaddleOcrDetV5,
            OcrVersionConfig.PpOcrV5,
            BgiOnnxModel.PaddleOcrRecV5Eslav,
            OcrVersionConfig.PpOcrV5);

        public static readonly PaddleOcrModelType V5Korean = Create(
            BgiOnnxModel.PaddleOcrDetV5,
            OcrVersionConfig.PpOcrV5,
            BgiOnnxModel.PaddleOcrRecV5Korean,
            OcrVersionConfig.PpOcrV5);

        /// <summary>
        /// 这边多语言部分写的比较丑陋，但是能跑。可以根据PP-OCR的语言列表来优化。
        /// </summary>
        /// <param name="cultureInfo"></param>
        /// <returns></returns>
        public static PaddleOcrModelType? FromCultureInfo(CultureInfo cultureInfo)
        {
            HashSet<string> eslavLangs = new(StringComparer.OrdinalIgnoreCase)
                { "ru", "be", "uk" };
            HashSet<string> latinLangs = new(StringComparer.OrdinalIgnoreCase)
            {
                "af", "az", "bs", "cs", "cy", "da", "de", "es", "et", "fr", "ga", "hr", "hu", "id", "is", "it", "ku",
                "la",
                "lt", "lv", "mi", "ms", "mt", "nl", "no", "oc", "pi", "pl", "pt", "ro", "rs_latin", "sk", "sl", "sq",
                "sv",
                "sw", "tl", "tr", "uz", "vi", "french", "german"
            };
            HashSet<string> ocrV5Langs = new(StringComparer.OrdinalIgnoreCase)
                { "zh", "chi", "zho", "en", "japan", "jp" };
            // HashSet<string> SPECIAL_LANGS = new(StringComparer.OrdinalIgnoreCase)
            //     { "ch", "chinese_cht", "en", "japan", "korean" };

            List<string> names =
            [
                cultureInfo.EnglishName.ToLowerInvariant(), cultureInfo.Name.ToLowerInvariant(),
                cultureInfo.ThreeLetterISOLanguageName.ToLowerInvariant(),
                cultureInfo.TwoLetterISOLanguageName.ToLowerInvariant()
            ];
            foreach (var name in names)
            {
                if (name.Equals("korean") || name.Equals("ko"))
                {
                    return V5Korean;
                }

                if (eslavLangs.Contains(name))
                {
                    return V5Eslav;
                }

                if (latinLangs.Contains(name))
                {
                    return V5Latin;
                }

                if (ocrV5Langs.Contains(name))
                {
                    return V5;
                }
            }

            return null;
        }

        /// <summary>
        /// 中英文优先使用V4模型，其他语言使用V5模型
        /// </summary>
        /// <param name="cultureInfo"></param>
        /// <returns></returns>
        public static PaddleOcrModelType? FromCultureInfoV4(CultureInfo cultureInfo)
        {
            var v5 = FromCultureInfo(cultureInfo);
            // 如果用的是v5, 那么优先用V4的细分模型
            if (v5 == V5)
            {
                List<string> names =
                [
                    cultureInfo.EnglishName.ToLowerInvariant(), cultureInfo.Name.ToLowerInvariant(),
                    cultureInfo.ThreeLetterISOLanguageName.ToLowerInvariant(),
                    cultureInfo.TwoLetterISOLanguageName.ToLowerInvariant()
                ];
                foreach (var name in names)
                {
                    if (name.Equals("en"))
                    {
                        return V4En;
                    }
                    else if (name.Equals("zh-hant") || name.Equals("zh-tw") || name.Equals("zh-hk"))
                    {
                        return V5;
                    }
                }

                return V4;
            }
            else
            {
                return v5;
            }
        }
    }

    public PaddleOcrService(BgiOnnxFactory bgiOnnxFactory, PaddleOcrModelType modelType)
        : this(bgiOnnxFactory.ForCurrentRuntime(), modelType)
    {
    }

    internal PaddleOcrService(BgiOnnxSessionFactory bgiOnnxFactory, PaddleOcrModelType modelType)
    {
        var (modelsDet, modelsRec) = modelType.Build(bgiOnnxFactory);
        _localDetModel = modelsDet;
        _localRecModel = modelsRec;

        try
        {
            // 预热模型
            using var preHeatImageMat = Bv.ImRead(modelType.PreHeatImagePath) ??
                                        throw new FileNotFoundException($"预热图片未找到: {modelType.PreHeatImagePath}");
            // Debug输出结果
            var preHeatResult = RunAll(preHeatImageMat, 1);
            Debug.WriteLine(
                $"PaddleOcrService 预热完成，使用模型: {modelType.DetectionModel.Name} 和 {modelType.RecognitionModel.Name}，结果: {preHeatResult.Text}");
        }
        catch
        {
            _localDetModel.Dispose();
            _localRecModel.Dispose();
            throw;
        }
    }

    /// <summary>
    ///     推荐传入三通道BGR mat，虽然四通道和单通道也做了兼容，但是三通道最快
    /// </summary>
    public string Ocr(Mat mat)
    {
        return OcrResult(mat).Text;
    }

    /// <summary>
    ///     推荐传入三通道BGR mat，虽然四通道和单通道也做了兼容，但是三通道最快
    /// </summary>
    public OcrResult OcrResult(Mat mat)
    {
        lock (_inferenceLock)
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            if (mat.Channels() == 4)
            {
                using var mat3 = mat.CvtColor(ColorConversionCodes.BGRA2BGR);
                return _OcrResult(mat3);
        }

        return _OcrResult(mat);
        }
    }

    /// <summary>
    /// 获取一次完整推理的只读观测，不按 DropScore 丢弃检测框。检测器自身仍有阈值，零框不证明无文字。
    /// 所有坐标相对于传入图像；此方法与普通 OCR、释放及阈值修改使用同一把锁。
    /// </summary>
    public PaddleOcrObservation Observe(Mat mat)
    {
        lock (_inferenceLock)
        {
            if (_disposed)
                return PaddleOcrObservation.Unavailable("OCR 服务已释放。", _dropScore);
            try
            {
                if (mat.Channels() == 4)
                {
                    using var mat3 = mat.CvtColor(ColorConversionCodes.BGRA2BGR);
                    return ObserveCore(mat3, _dropScore, _localDetModel.RunBoxes,
                        images => _localRecModel.Run(images));
                }

                return ObserveCore(mat, _dropScore, _localDetModel.RunBoxes,
                    images => _localRecModel.Run(images));
            }
            catch (Exception ex)
            {
                return PaddleOcrObservation.Failure(PaddleOcrObservationStatus.DetectionFailed, _dropScore, ex);
            }
        }
    }

    /// <summary>分离检测与识别阶段，失败时保留已完成的检测结果。</summary>
    internal static PaddleOcrObservation ObserveCore(Mat src, float dropScore,
        Func<Mat, PaddleOcrDetectionBox[]> detect, Func<Mat[], OcrRecognizerResult[]> recognize)
    {
        PaddleOcrDetectionBox[] boxes;
        try { boxes = SortBoxes(detect(src)); }
        catch (Exception ex)
        {
            return PaddleOcrObservation.Failure(PaddleOcrObservationStatus.DetectionFailed, dropScore, ex);
        }

        var regions = boxes.Select(box => new PaddleOcrObservedRegion(
            box.Points[0], box.Points[1], box.Points[2], box.Points[3], box.Score, null, null)).ToArray();
        List<Mat> mats = new(boxes.Length);
        try
        {
            foreach (var box in boxes) mats.Add(GetRotateCropImage(src, box.Points));
            var results = recognize(mats.ToArray());
            if (results.Length != boxes.Length)
                throw new InvalidOperationException("OCR 识别结果数与检测框数不一致。");
            for (var i = 0; i < regions.Length; i++)
                regions[i] = regions[i] with { Text = results[i].Text, RecognitionScore = results[i].Score };
            return new PaddleOcrObservation(PaddleOcrObservationStatus.Completed, dropScore, regions, null, null);
        }
        catch (Exception ex)
        {
            return new PaddleOcrObservation(PaddleOcrObservationStatus.RecognitionFailed, dropScore, regions,
                ex.GetType().Name, ex.Message);
        }
        finally
        {
            foreach (var image in mats) image.Dispose();
        }
    }

    /// <summary>
    ///     推荐传入三通道BGR mat，虽然四通道和单通道也做了兼容，但是三通道最快
    /// </summary>
    public string OcrWithoutDetector(Mat mat)
    {
        lock (_inferenceLock)
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            var startTime = Stopwatch.GetTimestamp();
            var str = _localRecModel.Run(mat).Text;
            var time = Stopwatch.GetElapsedTime(startTime);
            // Debug.WriteLine($"PaddleOcrWithoutDetector 耗时 {time.TotalMilliseconds}ms 结果: {str}");
            return str;
        }
    }

    private OcrResult _OcrResult(Mat mat)
    {
        var startTime = Stopwatch.GetTimestamp();
        var result = RunAll(mat);
        var time = Stopwatch.GetElapsedTime(startTime);
        // Debug.WriteLine($"PaddleOcr 耗时 {time.TotalMilliseconds}ms 结果: {result.Text}");
        return result;
    }

    /// <summary>
    ///     推荐传入三通道BGR mat，虽然四通道和单通道也做了兼容，但是三通道最快
    /// </summary>
    private OcrResult RunAll(Mat src, int recognizeBatchSize = 0)
    {
        var boxes = SortBoxes(_localDetModel.RunBoxes(src));
        List<Mat> mats = new(boxes.Length);
        try
        {
            foreach (var box in boxes)
            {
                mats.Add(GetRotateCropImage(src, box.Points));
            }

            return new OcrResult(_localRecModel.Run(mats.ToArray(), recognizeBatchSize)
                .Select((result, i) => new OcrResultRegion(boxes[i].Rect, result.Text, result.Score))
                .Where(region => region.Score >= DropScore)
                .ToArray());
        }
        finally
        {
            foreach (var mat in mats) mat.Dispose();
        }
    }

    /// <summary>
    ///     按 PaddleOCR 官方 get_rotate_crop_image 的方式对检测框执行透视裁剪。
    /// </summary>
    /// <param name="src">原始图像。</param>
    /// <param name="points">按左上、右上、右下、左下排列的四个顶点。</param>
    /// <returns>透视变换后的文字图像。</returns>
    private static Mat GetRotateCropImage(Mat src, Point2f[] points)
    {
        if (points.Length != 4)
        {
            throw new ArgumentException("OCR detection box must contain exactly four points.", nameof(points));
        }

        var cropWidth = (int)Math.Max(GetDistance(points[0], points[1]), GetDistance(points[2], points[3]));
        var cropHeight = (int)Math.Max(GetDistance(points[0], points[3]), GetDistance(points[1], points[2]));
        if (cropWidth <= 0 || cropHeight <= 0)
        {
            throw new InvalidOperationException($"Invalid OCR crop size: {cropWidth}x{cropHeight}");
        }

        Point2f[] destinationPoints =
        [
            new(0, 0),
            new(cropWidth, 0),
            new(cropWidth, cropHeight),
            new(0, cropHeight)
        ];
        using var transform = Cv2.GetPerspectiveTransform(points, destinationPoints);
        var cropped = new Mat();
        try
        {
            Cv2.WarpPerspective(src, cropped, transform, new Size(cropWidth, cropHeight),
                InterpolationFlags.Cubic, BorderTypes.Replicate);

            if (cropped.Height * 1.0 / cropped.Width >= 1.5)
            {
                Cv2.Rotate(cropped, cropped, RotateFlags.Rotate90Counterclockwise);
            }

            return cropped;
        }
        catch
        {
            cropped.Dispose();
            throw;
        }
    }

    private static double GetDistance(Point2f first, Point2f second)
    {
        var deltaX = first.X - second.X;
        var deltaY = first.Y - second.Y;
        return Math.Sqrt(deltaX * deltaX + deltaY * deltaY);
    }

    private static PaddleOcrDetectionBox[] SortBoxes(PaddleOcrDetectionBox[] source)
    {
        var boxes = source
            .OrderBy(box => box.Points[0].Y)
            .ThenBy(box => box.Points[0].X)
            .ToArray();

        for (var i = 0; i < boxes.Length - 1; i++)
        {
            for (var j = i; j >= 0; j--)
            {
                if (Math.Abs(boxes[j + 1].Points[0].Y - boxes[j].Points[0].Y) < 10
                    && boxes[j + 1].Points[0].X < boxes[j].Points[0].X)
                {
                    (boxes[j], boxes[j + 1]) = (boxes[j + 1], boxes[j]);
                }
                else
                {
                    break;
                }
            }
        }

        return boxes;
    }

    public void Dispose()
    {
        lock (_inferenceLock)
        {
            if (_disposed) return;
            _disposed = true;
            try { _localDetModel.Dispose(); }
            finally { _localRecModel.Dispose(); }
        }
    }

    /// <summary>
    /// 返回(DetConfigName, RecConfigName)
    /// </summary>
    public (string, string) GetConfigName
    {
        get
        {
            return (this._localDetModel.GetConfigName, this._localRecModel.GetConfigName);
        }
    }
}

/// <summary>一次 OCR 观测的完成状态，不代表区域是否真的有文字。</summary>
public enum PaddleOcrObservationStatus
{
    /// <summary>检测和识别均已完成。</summary>
    Completed,
    /// <summary>检测未完成，检测数未知。</summary>
    DetectionFailed,
    /// <summary>检测完成但裁剪或识别未完成，已检测框仍可读取。</summary>
    RecognitionFailed,
    /// <summary>OCR 服务不可用，未执行推理。</summary>
    Unavailable
}

/// <summary>检测器输出的四角和分数，以及识别完成时的文本和识别分数。</summary>
public readonly record struct PaddleOcrObservedRegion(
    Point2f TopLeft, Point2f TopRight, Point2f BottomRight, Point2f BottomLeft,
    float DetectionScore, string? Text, float? RecognitionScore);

/// <summary>独立于服务生命周期的只读快照；空检测只表示检测器未返回框。</summary>
public sealed class PaddleOcrObservation
{
    internal PaddleOcrObservation(PaddleOcrObservationStatus status, float? dropScore,
        PaddleOcrObservedRegion[] regions, string? errorType, string? errorMessage)
    {
        Status = status;
        DropScore = dropScore;
        Regions = Array.AsReadOnly((PaddleOcrObservedRegion[])regions.Clone());
        ErrorType = errorType;
        ErrorMessage = errorMessage;
    }

    /// <summary>推理状态。</summary>
    public PaddleOcrObservationStatus Status { get; }
    /// <summary>本次推理是否完成检测和识别。</summary>
    public bool IsCompleted => Status == PaddleOcrObservationStatus.Completed;
    /// <summary>本次使用的识别过滤阈值；服务不可取得时未知。</summary>
    public float? DropScore { get; }
    /// <summary>检测完成时返回的全部框，未受识别阈值过滤。</summary>
    public IReadOnlyList<PaddleOcrObservedRegion> Regions { get; }
    /// <summary>检测器返回的框数；检测未完成时为 null，而不是零。</summary>
    public int? RawDetectionCount => Status is PaddleOcrObservationStatus.Completed or PaddleOcrObservationStatus.RecognitionFailed
        ? Regions.Count : null;
    /// <summary>未通过原 OCR 过滤条件的框数；识别未完成时为 null。</summary>
    public int? LowConfidenceCount => IsCompleted
        ? Regions.Count(region => !(region.RecognitionScore >= DropScore)) : null;
    /// <summary>失败异常类型，不可用但无异常时为 null。</summary>
    public string? ErrorType { get; }
    /// <summary>失败原因，不会将异常误报为零检测。</summary>
    public string? ErrorMessage { get; }

    /// <summary>构造服务不可用的观测。</summary>
    public static PaddleOcrObservation Unavailable(string message, float? dropScore = null) =>
        new(PaddleOcrObservationStatus.Unavailable, dropScore, [], null, message);

    internal static PaddleOcrObservation Failure(PaddleOcrObservationStatus status, float? dropScore, Exception ex) =>
        new(status, dropScore, [], ex.GetType().Name, ex.Message);
}
