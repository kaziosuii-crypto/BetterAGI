# Lumine 部署记录：gpushare 4090（i-2.gpushare.com:41846）

> 本文件只记录可复现的部署步骤与实测结论，不含任何密码/令牌原文。

## 1. 主机与环境

| 项 | 值 |
| --- | --- |
| 连接 | `ssh -p 41846 root@i-2.gpushare.com`（密码只经环境变量 `AUTODL_SSH_PASSWORD` 传给 `_diag/remote.py`） |
| 主机指纹 | `ssh-ed25519 SHA256:ctccQQbbMeavq7D0b3QYWVnVANA3HMkOCsKM9QGN3eA`（首次信任，TOFU） |
| 容器 | `I2c1fa2879e013017b5` |
| GPU | RTX 4090，49140 MiB，驱动 550.163.01 / CUDA 12.4 |
| 磁盘 | `/` overlay 30G、`/hy-tmp` 50G（持久数据放这里） |
| Python | 3.11.11，pip 24.2（阿里云镜像） |
| 预装 | torch 2.5.1+cu124、`/usr/local/cuda/bin/nvcc`、screen |
| 网络 | `huggingface.co` 不可达；`hf-mirror.com` 可用；pypi 可用 |

## 2. 目录布局

```
/hy-tmp/lumine-deploy/
├── model/            # 权重（model-00001-of-00001.safetensors 16577095590 B + 配置/tokenizer）
├── service/          # autodl_server.py, lumine_protocol.py, serve_lumine.sh
├── venv/             # --without-pip --system-site-packages，复用系统 torch
├── token             # 8010 的 Bearer 令牌（chmod 600）
├── start.sh          # image 协议
├── start_text.sh     # text 协议（由 start.sh sed 生成）
└── server.log        # 8010 服务日志；vLLM 日志在 service/vllm.log
```

## 3. 权重下载

`_diag/remote/gpushare_download.sh`：hf-mirror + 16 路分块 `curl -r`，合并后校验

```
sha256sum = 685e3343c57cca1a757d2a9e4e3cfa237c230524141d166cf60ee7815e7d3829
```

小文件：`config.json`、`preprocessor_config.json`、`tokenizer*.json`、`merges.txt`、`vocab.json`、
`special_tokens_map.json`、`added_tokens.json`、`chat_template.jinja`（`generation_config.json` 上游缺失，可选）。

## 4. 依赖

关键版本：`vllm 0.7.3`、`transformers 4.57.3`、`tokenizers 0.22.2`、`numpy 1.26.4`、
`opencv-python-headless 4.11.0.86`、`cupy-cuda12x 13.3.0`、`aiohttp 3.14.5`、`huggingface-hub 0.36.2`、
`accelerate`（transformers 直推对照用）。

踩坑：

- `python -m venv` 触发 `ensurepip` 失败 → `python -m venv --without-pip --system-site-packages`，
  再用 `python -m pip --python <venv>/bin/python install ...`。
- `huggingface_hub==0.29.0` 与 `transformers 4.57.3` 冲突（后者要 `>=0.34`）→ 不固定 hub 版本。
- `opencv-python-headless` 与 numpy<2 会反复回溯 → 固定 `<4.12`；`cupy-cuda12x` 固定 `==13.3.0`。

## 5. vLLM 适配补丁（必须）

`vllm/model_executor/models/qwen2_vl.py` 的 `hf_to_vllm_mapper.orig_to_new_prefix` 需要两条更具体的映射：

```python
"model.language_model.": "language_model.model.",
"model.visual.": "visual.",
```

否则加载报 `KeyError: 'language_model.embed_tokens.weight'`（权重由 transformers 4.57 保存，
名字带 `model.language_model.*` / `model.visual.*`）。补丁脚本 `_diag/remote/gpushare_mapping.py`，
原文件另存 `qwen2_vl.py.original`。

## 6. 启动

```bash
screen -dmS lumine bash -c 'exec > /hy-tmp/lumine-deploy/server.log 2>&1; exec bash /hy-tmp/lumine-deploy/start.sh'
```

`start.sh` 关键环境变量：

```
PYTHON_BIN=/hy-tmp/lumine-deploy/venv/bin/python
PATH=/hy-tmp/lumine-deploy/venv/bin:/usr/local/cuda/bin:/usr/local/bin:/usr/bin:/bin
CUDA_HOME=/usr/local/cuda
LUMINE_MODEL_DIR=/hy-tmp/lumine-deploy/model
LUMINE_SERVER_TOKEN=$(cat /hy-tmp/lumine-deploy/token)
LUMINE_HOST=127.0.0.1
VLLM_EXTRA_ARGS='--max-model-len 8192 --gpu-memory-utilization 0.75'
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
exec bash ./serve_lumine.sh --protocol <image|text>
```

约 2 分钟就绪：`curl 127.0.0.1:8010/health` → `{"ok": true, "model": "lumine-agent-vl-7b", "mock": false}`；
显存占用 36–37 GB。

## 7. 本机侧

- 隧道：`pythonw _diag/tunnel.py --host i-2.gpushare.com --port 41846 --fingerprint SHA256:ctcc… --local 18010 --remote 8010`
- 桥接：`pythonw BetterGI/integrations/lumine/lumine_bridge.py --config bridge_config.json --motor-protocol v2 --intent-port 18311 --intent-token <tools/intent_token.txt>`
- `GET http://127.0.0.1:18311/feedback` 应显示 `motorConnectionActive: true`、`motorCapability.declarations.protocol = lumine-live-v2`。

## 8. 实测结论：image-only 协议恒 idle（重要）

按官方 Runner 配方（1280×720 → 底部补 16px 黑边 → 1280×736 PNG、单条 image-only user 消息、
`temperature=0, max_tokens=192, skip_special_tokens=false`）实测：

| 输入 | 输出 |
| --- | --- |
| 6 张不同真实游戏帧（街道/广场/菜单/战斗中） | 全部 `0 0 0 ; ; ; ; ; ;`（idle） |
| 补黑边 / 不补黑边、PNG / JPEG、温度 0 / 0.8 | 同上，全部 idle |
| 黑图、随机噪声 | 同上，全部 idle |
| 纯文本 "Press W to walk forward." | `0 0 0 ; W ; W ; W ; W ; W ; W` |

用 transformers 原生推理（同权重、同 chat template、`grid [[1, 52, 92]]`、`prompt_tokens=1580`）结果相同
→ 排除 vLLM 权重映射/采样问题，是权重对这类帧本身给 no-op。

`chat_template.jinja` 会自动注入官方 system prompt（200ms、6 步、`<|action_start|>X Y Z ; k1..k6<|action_end|>`），
`preprocessor_config.json`：`patch_size=14, merge_size=2, min_pixels=3136, max_pixels=12845056`。

**当前策略**：这台机器上用 `--protocol text`（intent + guidance 的 motor 模式）。实测 3 帧中会出现
`W ; W ; W ; W ; W ; W`；配合服务端的空闲重采样（`_MAX_IDLE_RESAMPLES = 4`）与
`MOTION_CARRY_MS = 1500`（idle 时沿用上一次移动动作），可以维持连续移动。

待验证假设：官方训练帧可能是**原生 1280×720 渲染**，而我们是 1920×1080 渲染后缩放，UI 尺度分布不同；
可在游戏里切一次 1280×720 分辨率再复测 image 协议。

## 9. 关机

```bash
screen -S lumine -X quit; pkill -f 'autodl_serve[r]'; pkill -f 'vllm.entrypoints.openai.api_serve[r]'
```

确认 `nvidia-smi` 显存回落后再关机，避免按小时计费空转。
