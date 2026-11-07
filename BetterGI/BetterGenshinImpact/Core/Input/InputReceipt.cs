using Vanara.PInvoke;

namespace BetterGenshinImpact.Core.Input;

/// <summary>单次原子输入的类型；键码也可表示 Windows 鼠标键。</summary>
public enum AtomicInputKind { KeyDown, KeyUp, MoveBy, MoveTo, Scroll }

/// <summary>单次原子输入及其参数。</summary>
public readonly record struct AtomicInput(AtomicInputKind Kind, User32.VK Key = default, double X = 0, double Y = 0);

/// <summary>输入后端能够证明的最强结果，不表示游戏已响应。</summary>
public enum InputReceiptStatus { Unknown, Accepted, Rejected, Partial, Unsupported }

/// <summary>与单次调用绑定的回执；计数仅表示 Windows 接受的 INPUT 数量。</summary>
public readonly record struct InputReceipt(InputReceiptStatus Status, int? Requested = null, int? Accepted = null)
{
    /// <summary>按实际请求与系统接受数计算结果；非法计数不可作为证据。</summary>
    public static InputReceipt FromCounts(int requested, uint accepted)
    {
        if (requested <= 0 || accepted > requested) return new InputReceipt(InputReceiptStatus.Unknown);
        var status = accepted == requested ? InputReceiptStatus.Accepted
            : accepted == 0 ? InputReceiptStatus.Rejected : InputReceiptStatus.Partial;
        return new InputReceipt(status, requested, (int)accepted);
    }
}

/// <summary>可直接返回原子输入回执的通道。</summary>
public interface IAtomicInputChannel
{
    /// <summary>执行一次输入并返回该次调用的证据。</summary>
    InputReceipt ExecuteAtomic(AtomicInput input);
}
