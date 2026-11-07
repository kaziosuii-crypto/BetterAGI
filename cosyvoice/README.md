# 飞鱼语音（FeiyuVoice-CosyVoice3）本地 TTS

把 `session.jsonl` 里的解说台词直接合成成飞鱼音色，替代 SAPI + SVC 的两段式
配音。模型来自 ModelScope：

<https://modelscope.cn/models/Esmerubphire/FeiyuVoice-CosyVoice3>

- 基础模型：`FunAudioLLM/Fun-CosyVoice3-0.5B-2512`（CosyVoice3，0.5B）
- 音色：`cute`（F6 权重，可爱）与 `gentle`（原 LLM + N10 参考，温柔）
- 原始音源与训练授权见模型包 `COPYRIGHT.md`（B 站 UP 主 星落落_oi，
  已获授权，仅供合规使用）

## 目录

```text
E:\BetterAGI\cosyvoice\
    .venv\                 # Python 3.12 独立环境
    repo\                  # CosyVoice 源码，固定 commit 074ca6dc
    model\                 # 模型权重（约 7GB）
        models\Fun-CosyVoice3-0.5B-2512\
        models\feiyu_cute_F6_0040.pt
        references\cute_N01.wav
        references\gentle_N10.wav
    feiyu_tts.py           # 批量推理脚本
    requirements-windows.txt
```

## 环境

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | 3.12.14 | 与模型包的 3.11 不同，但推理已验证 |
| torch / torchaudio | 2.9.1+cu128 | 黑韦尔 sm_120 可用；模型包推荐 2.11 |
| CosyVoice | commit `074ca6dc` | 含 `third_party/Matcha-TTS` 子模块 |
| transformers | 4.51.3 | CosyVoice3 的 LLM 结构依赖 |
| onnxruntime-gpu | 1.22.0 | speech tokenizer 走 CUDA，失败自动回退 CPU |
| openai-whisper | 20231117 | 仅用于提取 mel 特征，不下载 ASR 权重 |
| wetext | 0.0.4 | **未启用**：其模型仓库需要授权；中文直接送 tokenizer |

没有安装 WSL。模型包自带的 `Speak.ps1` 需要 WSL2 + Ubuntu 24.04，本目录
改为在 Windows 原生 Python 里直接调用 CosyVoice 的 `AutoModel`，省去
WSL 依赖。

## 用法

单句试听：

```bash
E:\BetterAGI\cosyvoice\.venv\Scripts\python.exe E:\BetterAGI\cosyvoice\feiyu_tts.py ^
    --text "你好呀，今天想聊点什么？" --style cute --out demo.wav
```

批量（供成片流水线调用，只加载一次模型）：

```bash
E:\BetterAGI\cosyvoice\.venv\Scripts\python.exe E:\BetterAGI\cosyvoice\feiyu_tts.py ^
    --request request.json --result result.json
```

`request.json`：

```json
{
  "style": "cute",
  "speed": 1.0,
  "seed": 42,
  "output_dir": "E:/BetterAGI/demo/audio",
  "texts": [{"index": 1, "text": "大家好，今天来跑须弥。"}]
}
```

每句输出 `output_dir/tts-001.wav`（24kHz 单声道 PCM16），`result.json`
带时长、峰值、单句耗时与显存峰值。

## 实测（RTX 5070 Laptop 8GB）

| 阶段 | 耗时 |
|---|---|
| 首次加载（含 ONNX 会话） | 约 14 秒 |
| 第一批第一句（冷启动） | 约 20–22 秒 |
| 同进程后续每句（3 秒音频） | 约 3 秒 |
| PyTorch 显存峰值 | 3.3 GB（不含 ONNX 运行时） |

批量接口是必须的：逐句重启会反复加载 7GB 权重，一句要一分多钟。

## 与成片对接

`render_session.py` 已经接入：

```bash
python render_session.py sessions/<会话id>                    # 默认 cute
python render_session.py sessions/<会话id> --feiyu-style gentle
```

默认 `--tts auto`：检测到本环境（解释器、脚本、模型齐全）就用飞鱼 cute，
否则回退 SAPI。也可以显式 `--tts feiyu`。渲染器默认使用
`E:\BetterAGI\cosyvoice\.venv\Scripts\python.exe` 与 `feiyu_tts.py`，
可用 `--feiyu-python` / `--feiyu-script` / `--feiyu-model-root` 覆盖。
飞鱼失败时默认整体报错；加 `--allow-tts-fallback` 会回退到 SAPI。

## 已知限制

- 文本前端未启用：纯中文与常见标点没问题；阿拉伯数字会按字符送进
  tokenizer，读法可能不如 wetext 自然。需要时可先在台词里写成中文数字。
- 单句过长会按标点切分（每段 ≤80 字），段落之间语调会重置。
- 模型包装载时带 `torch 2.11` 的校验；本环境用 2.9.1 直接调用 API，
  不经过它的 `infer.py` 校验，功能已验证。
- 角色音色仅限授权范围内使用；不要用于冒充真人或违规内容。
