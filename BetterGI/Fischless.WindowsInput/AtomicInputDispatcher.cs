using System;
using System.Runtime.InteropServices;
using Vanara.PInvoke;

namespace Fischless.WindowsInput;

/// <summary>单次 SendInput 的系统接受计数，不表示目标应用已处理。</summary>
public static class AtomicInputDispatcher
{
    /// <summary>提交输入数组并返回系统实际接受的项数。</summary>
    public static uint Send(User32.INPUT[] inputs)
    {
        if (inputs == null) throw new ArgumentNullException(nameof(inputs));
        if (inputs.Length == 0) throw new ArgumentException("The input array was empty", nameof(inputs));
        return User32.SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(User32.INPUT)));
    }
}
