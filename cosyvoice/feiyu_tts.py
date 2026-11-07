#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞鱼语音（FeiyuVoice-CosyVoice3）批量离线 TTS。

模型包来自 ModelScope ``Esmerubphire/FeiyuVoice-CosyVoice3``：
基础模型 Fun-CosyVoice3-0.5B-2512 + 飞鱼 cute F6 权重 + 参考音频。
本脚本一次加载模型、批量合成多句台词，避免逐句重启进程。

请求 JSON::

    {
      "style": "cute",
      "speed": 1.0,
      "seed": 42,
      "output_dir": "E:/.../audio",
      "texts": [{"index": 1, "text": "你好呀"}, ...]
    }

输出 ``result.json``（与请求同目录或 stdout）::

    {"ok": true, "sample_rate": 24000, "results": [
       {"index": 1, "path": "...", "seconds": 2.4, "peak": 0.7, "elapsed": 1.3}
    ]}

单独试听::

    python feiyu_tts.py --text "你好呀，今天想聊点什么？" --out demo.wav
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

COSY_ROOT = Path(r"E:\BetterAGI\cosyvoice")
DEFAULT_REPO = COSY_ROOT / "repo"
DEFAULT_MODEL_ROOT = COSY_ROOT / "model"

BASE_MODEL_DIR = "models/Fun-CosyVoice3-0.5B-2512"
PROMPT_PREFIX = "You are a helpful assistant.<|endofprompt|>"

STYLES: Dict[str, Dict[str, str]] = {
    "cute": {
        "checkpoint": "models/feiyu_cute_F6_0040.pt",
        "reference": "references/cute_N01.wav",
        "reference_text": "我已经准备好了，我们开始吧。",
    },
    "gentle": {
        "checkpoint": "",
        "reference": "references/gentle_N10.wav",
        "reference_text": (
            "我刚刚把桌子收拾了一下，也给自己倒了一杯温水。"
            "现在可以坐下来，安安静静地听你说话了。"
        ),
    },
}


def _find_chinese_punct_split(text: str, limit: int = 80) -> List[str]:
    """把超长中文台词按标点切成不超过 ``limit`` 字的片段。

    短句原样返回；只有长段落才切，避免单次 LLM 解码过长。
    """
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    pieces: List[str] = []
    current = ""
    for char in text:
        current += char
        if char in "。！？；!?;" or (char in "，,、" and len(current) >= limit // 2):
            if current.strip():
                pieces.append(current.strip())
            current = ""
    if current.strip():
        pieces.append(current.strip())
    merged: List[str] = []
    for piece in pieces:
        if merged and len(merged[-1]) + len(piece) <= limit:
            merged[-1] += piece
        else:
            merged.append(piece)
    return merged


class FeiyuTTS:
    """加载一次模型，反复合成。"""

    def __init__(
        self,
        repo: Path,
        model_root: Path,
        style: str,
        *,
        fp16: bool = False,
    ) -> None:
        if style not in STYLES:
            raise ValueError(f"未知风格：{style}，可选 {sorted(STYLES)}")
        self.style = style
        self.sample_rate = 24000
        self.seed = 42

        repo = Path(repo)
        model_root = Path(model_root)
        base_dir = model_root / BASE_MODEL_DIR
        if not (base_dir / "cosyvoice3.yaml").is_file():
            raise FileNotFoundError(f"基础模型不完整：{base_dir}")

        # CosyVoice 依赖仓库自身与 Matcha-TTS 子模块。
        sys.path.insert(0, str(repo))
        sys.path.append(str(repo / "third_party" / "Matcha-TTS"))
        os.chdir(repo)
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

        # wetext 首次使用会从 ModelScope 拉取 gated 模型，失败后 CosyVoice
        # 会回退到无前端模式；这里直接让它 ImportError，避免每次启动卡住。
        sys.modules["wetext"] = None  # type: ignore[assignment]

        import torch

        try:
            os.add_dll_directory(str(Path(torch.__file__).parent / "lib"))
        except (AttributeError, OSError):
            pass
        torch.set_num_threads(4)

        # onnxruntime 的 CUDA provider 依赖 torch 自带的 cuDNN/CUDA DLL；
        # 个别驱动组合下会话创建会失败。失败时退回 CPU，语音 tokenizer
        # 只是预处理，慢一点不影响成片。
        import onnxruntime as ort

        original_session = ort.InferenceSession

        def safe_session(*args: Any, **kwargs: Any) -> Any:
            if os.environ.get("FEIYU_ORT_CPU") == "1":
                kwargs["providers"] = ["CPUExecutionProvider"]
                return original_session(*args, **kwargs)
            try:
                return original_session(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"onnxruntime CUDA 初始化失败，改用 CPU：{exc}",
                    file=sys.stderr,
                    flush=True,
                )
                kwargs["providers"] = ["CPUExecutionProvider"]
                return original_session(*args, **kwargs)

        ort.InferenceSession = safe_session  # type: ignore[assignment]

        from cosyvoice.cli.cosyvoice import AutoModel
        from cosyvoice.utils.common import set_all_random_seed

        self._torch = torch
        self._set_seed = set_all_random_seed

        # torchaudio 2.9 的 load 默认走 torchcodec；这里改用 soundfile，
        # 少一个二进制依赖，也避免 TorchCodec 与 torch 版本强绑定。
        self._patch_load_wav()

        started = time.perf_counter()
        self.voice = AutoModel(
            model_dir=str(base_dir),
            fp16=fp16,
            load_trt=False,
            load_vllm=False,
        )
        self.sample_rate = int(self.voice.sample_rate)

        checkpoint = STYLES[style]["checkpoint"]
        if checkpoint:
            checkpoint_path = model_root / checkpoint
            if not checkpoint_path.is_file():
                raise FileNotFoundError(f"缺少 {style} 权重：{checkpoint_path}")
            state = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
            self.voice.model.llm.load_state_dict(state, strict=True)
            del state
        self.voice.model.llm.eval()

        self.reference = model_root / STYLES[style]["reference"]
        self.reference_text = PROMPT_PREFIX + STYLES[style]["reference_text"]
        if not self.reference.is_file():
            raise FileNotFoundError(f"缺少参考音频：{self.reference}")
        self.load_seconds = time.perf_counter() - started

    def _patch_load_wav(self) -> None:
        """用 soundfile + torchaudio 重采样替换 CosyVoice 的 load_wav。"""
        import soundfile as sf
        import torchaudio

        def load_wav(wav: Any, target_sr: int, min_sr: int = 16000) -> Any:
            data, sample_rate = sf.read(str(wav), dtype="float32", always_2d=True)
            if sample_rate < min_sr:
                raise ValueError(f"采样率 {sample_rate} 低于 {min_sr}")
            speech = self._torch.from_numpy(data.T)
            if sample_rate != target_sr:
                speech = torchaudio.functional.resample(speech, sample_rate, target_sr)
            return speech

        import cosyvoice.cli.frontend as frontend_module
        import cosyvoice.utils.file_utils as file_utils

        file_utils.load_wav = load_wav
        frontend_module.load_wav = load_wav

    def synthesize(self, text: str, speed: float = 1.0) -> tuple[Any, float]:
        """合成一句话，返回 (float32 波形, 耗时秒)。"""
        import numpy as np

        pieces = _find_chinese_punct_split(text)
        if not pieces:
            raise ValueError("台词为空")
        self._set_seed(self.seed)
        chunks: List[Any] = []
        started = time.perf_counter()
        for piece in pieces:
            with self._torch.inference_mode():
                for output in self.voice.inference_zero_shot(
                    piece,
                    self.reference_text,
                    str(self.reference),
                    stream=False,
                    speed=float(speed),
                    text_frontend=False,
                ):
                    chunks.append(
                        output["tts_speech"].detach().cpu().numpy().reshape(-1)
                    )
        if not chunks:
            raise RuntimeError("模型没有返回音频")
        audio = np.concatenate(chunks).astype("float32")
        if (
            audio.size == 0
            or not np.isfinite(audio).all()
            or float(np.abs(audio).max()) < 1e-5
        ):
            raise RuntimeError("模型返回了静音或非法音频")
        return audio, time.perf_counter() - started


def _write_wav(path: Path, audio: Any, sample_rate: int) -> float:
    """峰值归一化后写 PCM16，返回峰值。"""
    import numpy as np
    import soundfile as sf

    peak = float(np.abs(audio).max())
    gain = min(1.0, 0.98 / peak) if peak > 1e-6 else 1.0
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, audio * gain, sample_rate, subtype="PCM_16")
    return peak


def run_request(tts: FeiyuTTS, request: Dict[str, Any]) -> Dict[str, Any]:
    output_dir = Path(request["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    speed = float(request.get("speed") or 1.0)
    tts.seed = int(request.get("seed") or 42)
    results: List[Dict[str, Any]] = []
    for item in request.get("texts") or []:
        index = int(item["index"])
        text = str(item.get("text") or "").strip()
        name = f"tts-{index:03d}.wav"
        target = output_dir / name
        record: Dict[str, Any] = {"index": index, "text": text, "path": str(target)}
        if not text:
            record["error"] = "空台词"
            results.append(record)
            continue
        # 每句可用 speech prosody 覆盖全局语速（慢/快），缺省沿用请求值。
        item_speed = float(item.get("speed") or speed)
        item_speed = max(0.5, min(1.5, item_speed))
        try:
            audio, elapsed = tts.synthesize(text, speed=item_speed)
            peak = _write_wav(target, audio, tts.sample_rate)
            record.update(
                {
                    "seconds": round(audio.size / tts.sample_rate, 3),
                    "peak": round(peak, 5),
                    "elapsed": round(elapsed, 3),
                }
            )
            print(
                f"[{index}] {record['seconds']}s  {elapsed:.2f}s  {text[:28]}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - 单句失败不拖垮整批
            record["error"] = f"{type(exc).__name__}: {exc}"
            print(f"[{index}] 失败：{record['error']}", file=sys.stderr, flush=True)
        results.append(record)
    ok = any("error" not in item for item in results)
    vram = 0.0
    if tts._torch.cuda.is_available():
        vram = round(tts._torch.cuda.max_memory_allocated() / 1024**3, 2)
    return {
        "ok": ok,
        "style": tts.style,
        "sample_rate": tts.sample_rate,
        "load_seconds": round(tts.load_seconds, 2),
        "peak_vram_gb": vram,
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="飞鱼 CosyVoice3 批量 TTS")
    parser.add_argument("--request", type=Path, default=None, help="请求 JSON")
    parser.add_argument("--result", type=Path, default=None, help="结果 JSON 输出路径")
    parser.add_argument("--text", default="", help="单句试听文本")
    parser.add_argument("--out", type=Path, default=None, help="单句试听输出 wav")
    parser.add_argument("--style", choices=sorted(STYLES), default="cute")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--fp16", action="store_true", help="显存不足时可尝试")
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--model-root", type=Path, default=DEFAULT_MODEL_ROOT)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    # 被父进程捕获输出时，Windows 默认 cp936 会让中文日志乱码。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    args = build_parser().parse_args(argv)
    request = None
    if args.request:
        request = json.loads(Path(args.request).read_text(encoding="utf-8-sig"))
    style = args.style
    if isinstance(request, dict) and request.get("style") in STYLES:
        style = str(request["style"])
    tts = FeiyuTTS(args.repo, args.model_root, style, fp16=args.fp16)
    print(
        f"模型已加载：{style}，{tts.load_seconds:.1f}s，{tts.sample_rate}Hz",
        flush=True,
    )
    if args.text:
        out = args.out or (COSY_ROOT / "demo.wav")
        audio, elapsed = tts.synthesize(args.text, speed=args.speed)
        peak = _write_wav(out, audio, tts.sample_rate)
        print(
            f"已生成 {out}（{audio.size / tts.sample_rate:.2f}s，"
            f"峰值 {peak:.3f}，推理 {elapsed:.2f}s）",
            flush=True,
        )
        return 0
    if request is None:
        print("需要 --request 或 --text", file=sys.stderr)
        return 2
    result = run_request(tts, request)
    print(f"显存峰值：{result.get('peak_vram_gb', 0):.2f} GB", flush=True)
    if args.result:
        args.result.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
