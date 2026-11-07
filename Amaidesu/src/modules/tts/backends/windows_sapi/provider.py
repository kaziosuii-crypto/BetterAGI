"""WindowsSapiProvider - Windows SAPI（System.Speech）离线语音合成引擎

- 引擎：调用 Windows 自带 PowerShell + System.Speech，把文本合成为临时 WAV，
  再由 soundfile 读取为 PCM 交给统一音频通道播放；全程离线，不依赖 LLM 或网络
- 调用方：``src.agents.streamer.utterance_queue``（编排层确定性调用）
- 自治生命周期：``handle_speech()`` 入口自动 ensure_setup，调用方无需预调
- 错误语义：合成失败主动发 ``tts.utterance.failed`` 并 raise，由队列兜底
- 接受可选 ``utterance_id`` 参数：传入时按 ``tts.utterance.*`` 生命周期发布
  事件；缺省/None 时静默跳过（手动 / 直调场景无 utterance 上下文）

文本经由临时文件而不是命令行参数传给 PowerShell：台词来自 LLM，含引号、
换行与 PowerShell 元字符时命令行转义容易出错，文件通道把转义问题整体消除。
合成进程使用隐藏窗口且不继承交互输入，单句串行由公共骨架的 ``tts_lock``
保证；超时或被取消时终止子进程，临时目录由 ``TemporaryDirectory`` 统一回收。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np

from src.modules.config.core_schemas import WindowsSapiParams
from src.modules.events.event_bus import EventBus
from src.modules.logging import get_logger
from ...common import build_stats_dict, run_full_engine_speech

DEPENDENCIES_OK = True
try:
    import soundfile as sf
except ImportError:
    DEPENDENCIES_OK = False


# 固定的合成脚本：只做语音选择、语速/音量设置与写 WAV，文本与路径全部经
# 参数或环境变量传入，脚本内容不含任何用户数据。
# 脚本保持纯 ASCII——Windows PowerShell 5.1 会把无 BOM 的 UTF-8 脚本按本地
# 代码页解析，中文注释会造成语法错误。
_POWERSHELL_SCRIPT = """param(
    [Parameter(Mandatory = $true)][string]$TextPath,
    [Parameter(Mandatory = $true)][string]$OutputPath
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    if ($env:AMAIDESU_SAPI_VOICE) {
        $synth.SelectVoice($env:AMAIDESU_SAPI_VOICE)
    }
    $synth.Rate = [int]$env:AMAIDESU_SAPI_RATE
    $synth.Volume = [int]$env:AMAIDESU_SAPI_VOLUME
    $text = [System.IO.File]::ReadAllText($TextPath, [System.Text.UTF8Encoding]::new($false))
    $synth.SetOutputToWaveFile($OutputPath)
    $synth.Speak($text)
} catch {
    [Console]::Error.WriteLine("SAPI synthesis failed: " + $_.Exception.Message)
    exit 1
} finally {
    $synth.Dispose()
}
"""

# 从 GUI 进程拉起 PowerShell 时抑制控制台窗口闪烁（非 Windows 上该常量不存在）
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class WindowsSapiProvider:
    """Windows SAPI 离线语音合成引擎。

    引擎通过 ``handle_speech(text, utterance_id)`` 暴露唯一入口；调用方
    （编排队列）无需关心 setup / cleanup 生命周期——``handle_speech`` 开头
    自动 ensure_setup。
    """

    PROVIDER_NAME = "windows_sapi"

    # 参数 Schema 单一事实源在 config 侧（core_schemas），此处仅别名引用
    ConfigSchema = WindowsSapiParams

    def __init__(
        self,
        config: Dict[str, Any],
        event_bus: Optional[EventBus] = None,
        audio_sink: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.event_bus = event_bus
        # 音频分接（AudioSink 协议形状；口型分析等消费者经装配链注入）
        self.audio_sink = audio_sink
        self.logger = get_logger(self.__class__.__name__)

        self.typed_config = self.ConfigSchema.from_dict(config)
        self.voice = self.typed_config.voice
        self.rate = self.typed_config.rate
        self.volume = self.typed_config.volume
        self.synth_timeout = self.typed_config.synth_timeout_ms / 1000

        # 音频设备管理器与 PowerShell 路径在 setup 中初始化
        self.audio_manager: Any = None
        self._powershell_path: Optional[str] = None
        self._is_connected = False
        self._has_started = False
        # 串行化锁：同一时间只合成一句话
        self.tts_lock = asyncio.Lock()
        self.render_count = 0
        self.error_count = 0

    @property
    def name(self) -> str:
        return self.PROVIDER_NAME

    # ===== 生命周期 =====

    async def setup(self) -> None:
        """初始化引擎（幂等）。

        平台与运行环境检查放在这里而不是构造期：装配层对构造异常 fail-soft，
        而 setup 由首次发声触发，错误能带上 ``tts.utterance.failed`` 事件与
        队列日志，便于用户定位"没声音"的原因。
        """
        if self._has_started:
            return
        if sys.platform != "win32":
            raise RuntimeError("Windows SAPI TTS 仅在 Windows 上可用")
        if not DEPENDENCIES_OK:
            raise RuntimeError("缺少必要的依赖: soundfile")
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if powershell is None:
            raise RuntimeError("未找到 powershell.exe，无法调用 Windows SAPI")
        self._powershell_path = powershell

        self._setup_audio_device()
        self._has_started = True
        self._is_connected = True
        self.logger.info(f"WindowsSapiProvider 启动完成（语音={self.voice or '系统默认'}）")

    async def cleanup(self) -> None:
        if not self._has_started:
            return
        self.logger.info("WindowsSapiProvider 停止中...")
        if self.audio_manager:
            self.audio_manager.stop_audio()
        self._is_connected = False
        self._has_started = False
        self.logger.info("WindowsSapiProvider 停止完成")

    # ===== 业务方法 =====

    async def handle_speech(self, text: str, utterance_id: Optional[str] = None) -> None:
        """合成并播放语音（发声四步前奏走公共入口，引擎只剩合成差异）。

        Args:
            text: 要合成的文本。
            utterance_id: 一次发声实例的唯一 ID；非 None 时按
                ``tts.utterance.*`` 生命周期发布事件。
        """
        await run_full_engine_speech(self, text=text, utterance_id=utterance_id, synthesize=self._synthesize_audio)

    async def _synthesize_audio(self, text: str) -> Tuple[np.ndarray, int]:
        """调用 PowerShell 合成到临时 WAV，再读成 PCM（失败直接抛出）。

        异常不在此处理：由公共骨架统一计错误数、发 ``tts.utterance.failed``
        并上抛给编排队列兜底——引擎侧吞错会让失败以假音频形态静默过关。
        """
        if not DEPENDENCIES_OK:
            raise RuntimeError("缺少必要的依赖: soundfile")
        powershell = self._powershell_path or shutil.which("powershell.exe") or shutil.which("powershell")
        if powershell is None:
            raise RuntimeError("未找到 powershell.exe，无法调用 Windows SAPI")

        with tempfile.TemporaryDirectory(prefix="amaidesu_sapi_") as work_dir:
            work_path = Path(work_dir)
            script_path = work_path / "sapi_synth.ps1"
            text_path = work_path / "speech.txt"
            wav_path = work_path / "speech.wav"
            script_path.write_text(_POWERSHELL_SCRIPT, encoding="utf-8")
            text_path.write_text(text, encoding="utf-8")

            await self._run_powershell(powershell, script_path, text_path, wav_path)

            if not wav_path.is_file() or wav_path.stat().st_size == 0:
                raise RuntimeError("Windows SAPI 未生成有效的 WAV 文件")
            audio_array, samplerate = await asyncio.to_thread(sf.read, str(wav_path), dtype="float32")

        if getattr(audio_array, "ndim", 1) > 1:  # 多声道语音取首声道，播放侧按单声道处理
            audio_array = audio_array[:, 0]
        return audio_array, samplerate

    def _build_command(self, powershell: str, script_path: Path, text_path: Path, wav_path: Path) -> list[str]:
        """构造 PowerShell 调用参数（文本与路径均走独立文件/参数，不做 shell 拼接）。"""
        return [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(script_path),
            "-TextPath",
            str(text_path),
            "-OutputPath",
            str(wav_path),
        ]

    def _build_env(self) -> Dict[str, str]:
        """合成进程环境：语音/语速/音量走环境变量，避免命令行对中文音色名的编码歧义。"""
        env = os.environ.copy()
        env["AMAIDESU_SAPI_VOICE"] = self.voice
        env["AMAIDESU_SAPI_RATE"] = str(self.rate)
        env["AMAIDESU_SAPI_VOLUME"] = str(self.volume)
        return env

    async def _run_powershell(self, powershell: str, script_path: Path, text_path: Path, wav_path: Path) -> None:
        """执行一次合成子进程；超时与取消都会终止子进程后上抛。

        子进程输出只用于错误诊断：失败时把 stderr 尾部带进异常与日志，
        正常路径不解析 stdout。
        """
        self.logger.debug(
            f"启动 Windows SAPI 合成: voice={self.voice or '系统默认'!r}, "
            f"rate={self.rate}, volume={self.volume}, timeout={self.synth_timeout:.1f}s"
        )
        process = await asyncio.create_subprocess_exec(
            *self._build_command(powershell, script_path, text_path, wav_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._build_env(),
            creationflags=_CREATE_NO_WINDOW,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.synth_timeout)
        except asyncio.TimeoutError:
            await self._terminate(process)
            self.logger.error(
                f"Windows SAPI 合成超时（{self.typed_config.synth_timeout_ms} ms），已终止 PowerShell 进程"
            )
            raise RuntimeError(f"Windows SAPI 合成超时（{self.typed_config.synth_timeout_ms} ms）") from None
        except asyncio.CancelledError:
            await self._terminate(process)
            self.logger.warning("Windows SAPI 合成被取消，已终止 PowerShell 进程")
            raise

        if process.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            detail = stderr_text[-500:] or "无错误输出"
            self.logger.error(f"Windows SAPI 合成失败（退出码 {process.returncode}）: {detail}")
            raise RuntimeError(f"Windows SAPI 合成失败（退出码 {process.returncode}）: {detail}")

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        """终止仍在运行的子进程并等待回收，避免遗留 PowerShell 进程。"""
        if process.returncode is not None:
            return
        process.kill()
        await process.wait()

    def _setup_audio_device(self) -> None:
        """初始化音频设备管理器（统一走系统默认输出设备）"""
        # 函数内 import：src.modules.audio 顶层即拉起 numpy（重型数值依赖），仅在需要音频设备管理时加载
        from src.modules.audio import AudioDeviceManager

        manager = AudioDeviceManager(sample_rate=48000, channels=1, dtype=np.float32, sink=self.audio_sink)
        self.audio_manager = manager

    def get_stats(self) -> Dict[str, Any]:
        return build_stats_dict(
            name=self.__class__.__name__,
            is_connected=self._is_connected,
            render_count=self.render_count,
            error_count=self.error_count,
        )


def create_windows_sapi_provider(
    config: Dict[str, Any],
    event_bus: Optional[EventBus] = None,
    audio_sink: Optional[Any] = None,
) -> WindowsSapiProvider:
    """工厂：构造 ``WindowsSapiProvider`` 实例。

    装配阶段由编排队列持有 Provider 实例并直接调用 ``handle_speech``。
    """
    return WindowsSapiProvider(
        config=config,
        event_bus=event_bus,
        audio_sink=audio_sink,
    )


__all__ = ["WindowsSapiProvider", "create_windows_sapi_provider"]
