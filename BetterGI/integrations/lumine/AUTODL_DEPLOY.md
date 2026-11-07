# AutoDL Lumine 部署清单

本文件是 AutoDL 端部署 `koboshchan/Lumine-Agent-Pretrain-VL-7B` 的操作清单，
配合 [PROTOCOL.md](PROTOCOL.md) 与 [README.md](README.md) 使用。

## 1. AutoDL 实例准备

| 项目 | 要求 |
|---|---|
| GPU | 显存 ≥ 16GB（7B bf16 权重约 15GB，AWQ4 约 5GB） |
| 磁盘 | 模型约 16GB，另留 20GB 给 vLLM 缓存与量化产物 |
| 系统 | Linux + Python 3.10/3.11/3.12 |
| 端口 | `8000`（vLLM，仅本机）、`8010`（玩家服务，建议只经 SSH 隧道） |

AutoDL 镜像通常已预装 CUDA 与 PyTorch，仍需安装：

```bash
pip install vllm huggingface_hub
```

## 2. 下载模型

```bash
# 官方源
hf download koboshchan/Lumine-Agent-Pretrain-VL-7B --local-dir ./models/Lumine-Agent-Pretrain-VL-7B

# 国内镜像（更稳）
export HF_ENDPOINT=https://hf-mirror.com
hf download koboshchan/Lumine-Agent-Pretrain-VL-7B --local-dir ./models/Lumine-Agent-Pretrain-VL-7B
```

## 3. 启动

```bash
cd integrations/lumine
export LUMINE_SERVER_TOKEN='换成强随机串'
export LUMINE_HOST=127.0.0.1        # 只绑本机，配合 SSH 隧道
bash serve_lumine.sh
```

脚本会依次启动 vLLM（8000）与玩家服务（8010），并等待 vLLM 健康检查通过。

显存不足时按顺序尝试：

```bash
export VLLM_EXTRA_ARGS="--max-model-len 8192 --gpu-memory-utilization 0.92"
# 仍不足则量化后用 AWQ 权重
```

## 4. 无 GPU 先验通路

```bash
LUMINE_MOCK=1 LUMINE_SERVER_TOKEN='dev-token' bash serve_lumine.sh
```

Mock 返回确定性的脚本动作（前进、左右转视角、跳跃、攻击、绝对坐标点击、静止），
用于先跑通 BetterGI → Bridge → 玩家服务 → BetterGI 的整条链路。

## 5. 本机接回

```bash
ssh -N -L 8010:127.0.0.1:8010 -p <SSH端口> root@<AutoDL主机>
```

然后把 `integrations/lumine/bridge_config.json` 的 `lumineWs` 指向
`ws://127.0.0.1:8010/ws/player`，`lumineToken` 填与 AutoDL 相同的 `LUMINE_SERVER_TOKEN`。

## 6. 验收

| 检查 | 期望 |
|---|---|
| `curl http://127.0.0.1:8010/health` | 200 |
| `curl http://127.0.0.1:8010/status` | 含 `model`、`sessions` |
| Bridge 每 5 秒统计 | `framesOut` 增长、`actionsOut` 增长、`lastLatencyMs` 有值 |
| BetterGI `/api/v1/agent/live/status` | `appliedActions` 增长、`pressedKeys` 随动作变化 |

## 7. 注意

- `8010` 的 `/health` 与 `/status` 不校验令牌，`8010` 绝不能直接暴露公网。
- 玩家服务令牌为空时**不鉴权**，生产必须设置 `LUMINE_SERVER_TOKEN`。
- 该检查点是 Pretrain 版本，指令跟随能力有限；如表现不佳可尝试 `--prompt-mode instruct`
  或换用其他 Lumine 检查点。
- 模型下载需要外网；若 AutoDL 访问 Hugging Face 缓慢，使用 `HF_ENDPOINT=https://hf-mirror.com`。

## 8. 2026-10-27 实机部署记录（RTX 4090 / 24GB）

本机验证通过的组合与补丁：

| 组件 | 版本 |
|---|---|
| vLLM | 0.7.3 |
| transformers | **4.57.3**（4.49 会把 `text_config` 读成 dict，vLLM 断言失败） |
| torch / torchaudio | 2.5.1+cu124（镜像自带） |
| tokenizers | 0.21.0 |
| numpy | 1.26.4（vLLM 0.7.3 要求 <2） |

两个必须注意的兼容点：

1. **权重前缀**：本模型由 transformers 4.57 保存，权重名是
   `model.language_model.*` / `model.visual.*`；vLLM 0.7.3 的
   `qwen2_vl.py` 只映射旧版 `model.*`。需要给
   `hf_to_vllm_mapper` 增加：

   ```python
   "model.language_model.": "language_model.model.",
   "model.visual.": "visual.",
   ```

2. **图像 token 数**：默认 `max_pixels=12845056` 会让 1280×720 图产生
   约 1100 个视觉 token。启动时加
   `--mm-processor-kwargs '{"max_pixels":230400}'`，单帧延迟从约
   2.7 秒降到约 0.4 秒。

解析器已兼容 vLLM 丢 `<|action_start|>`、省略滚轮值、空键块和重复
`<|action_end|>` 的情况（见 `lumine_protocol.py`）。

实测：vLLM 加载约 23 秒，首帧约 2.2 秒，之后约 0.39–0.46 秒/帧
（约 2.2–2.5Hz，未达到 5Hz）；显存 21.3GB / 24.5GB。启动脚本在本机
`/root/autodl-tmp/lumine/start_lumine.sh`，模型 SHA256 为
`685e3343c57cca1a757d2a9e4e3cfa237c230524141d166cf60ee7815e7d3829`。
