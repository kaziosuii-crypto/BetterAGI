# AutoDL Lumine 部署记录（2026-11-02，RTX PRO 6000 Blackwell）

旧记录 `autodl_deploy_notes.md` 是 4090 机器（nmb1:26687，已销毁）的历史参数，编码已损坏，仅作参考。
本文件记录**当前有效**的部署方式。

## 实例

| 项目 | 值 |
|---|---|
| 主机 | `connect.bjb1.seetacloud.com`，端口 `28279`（密码由用户提供，只走环境变量 `AUTODL_SSH_PASSWORD`） |
| 主机指纹 | `ssh-ed25519 SHA256:6zkijTPdPT6WfS5rwH9cKmrjcUOEAXfeneFegE8bUbw`（首次信任 TOFU，未与用户单独核对） |
| 容器 | `autodl-container-1vtba44xj0-67cd5fe6` |
| GPU | `NVIDIA RTX PRO 6000 Blackwell Server Edition`，97887 MiB |
| 系统 | 1007GB 内存 / 208 核；`/root/autodl-tmp` 50G |

## 权重

- 来源：HuggingFace `koboshchan/Lumine-Agent-Pretrain-VL-7B`（AutoDL 直连 HF 不通，需 `source /etc/network_turbo`；ModelScope 无对应镜像）。
- 落地目录：`/root/autodl-tmp/models/Lumine-Agent-Pretrain-VL-7B`（14 个文件）。
- 大文件：`model-00001-of-00001.safetensors`，16577095590 字节，
  SHA256 `685e3343c57cca1a757d2a9e4e3cfa237c230524141d166cf60ee7815e7d3829`。
- 下载方式：`E:\BetterAGI\_diag\remote\dl_parallel.sh`（16 路 `curl -sL -f -r` 分块 + 逐块重试 + `cat` 合并 + SHA256 校验），日志 `/root/autodl-tmp/dl_parallel.log`。单连接只有 1.4MB/s 左右，分块聚合约 1.2GB/min。

## 运行时

| 组件 | 版本 |
|---|---|
| vLLM | 0.32.0（`vllm server` / `python -m vllm.entrypoints.openai.api_server`） |
| torch | 2.13.0+cu130 |
| transformers | 5.19.0 |
| tokenizers | 0.23.3 |
| flashinfer-python | 0.7.0.post1（未装 flashinfer-cubin，靠 JIT） |
| ninja | 1.13.2（在 `/root/miniconda3/bin`，**不在默认 PATH**） |
| CUDA toolkit | `/usr/local/cuda` → cuda-13.0 |

**关键修复**：`start_lumine.sh` 必须导出

```bash
export PATH=/root/miniconda3/bin:/usr/local/cuda/bin:/usr/local/bin:/usr/bin:/bin
export CUDA_HOME=/usr/local/cuda
export CUDA_PATH=/usr/local/cuda
```

否则 flashinfer 的 `Building JIT module sampling` 失败（日志只报 `FlashInfer kernels are disabled: flashinfer-cubin is not installed and nvcc ... or ninja (PATH) is missing`），
随后 `EngineCore failed to start`，栈落在 `vllm/v1/worker/gpu/warmup.py:381 _warmup_kernels`。

## 启动与检查

```bash
# 启动（screen 内跑包装脚本，包装脚本自己 exec >log 2>&1）
screen -dmS lumine bash /root/autodl-tmp/run_lumine.sh

curl -s 127.0.0.1:8010/health   # {"ok": true, ...}
curl -s 127.0.0.1:8010/status   # clients/inflight/queue/latestSeq/lastLatencyMs
tail -f /root/autodl-tmp/lumine_console.log
```

启动完成约需 3–4 分钟（torch.compile 14s + CUDA graph 捕获 33s + 采样 JIT）。

## 本机接入

```bash
python _diag/tunnel.py --host connect.bjb1.seetacloud.com --port 28279 \
  --fingerprint SHA256:6zkijTPdPT6WfS5rwH9cKmrjcUOEAXfeneFegE8bUbw \
  --local 18010 --remote 8010
```

`integrations/lumine/bridge_config.json` 的 `lumineWs` 设为 `ws://127.0.0.1:18010/ws/player`，`lumineToken` 用 `E:\BetterAGI\tools\lumine_token.txt`。

冒烟脚本 `E:\BetterAGI\_diag\lumine_smoke.py`（真实握手 + 能力挑战 + 意图 + 连续帧）：

```bash
python _diag/lumine_smoke.py --url ws://127.0.0.1:18010/ws/player \
  --token <token> --image E:\BetterAGI\_diag\game-now.png --frames 8
```

**注意**：v2 会话（`protocol=lumine-live-v2`）没有有效 `intent` 时服务端直接跳过推理（`autodl_server.py:461`），
不会报错也不出动作。冒烟必须先发 `intent`（含 `intentId` + `motorGeneration`）。

## 实测（2026-11-02 20:0x）

| 项目 | 结果 |
|---|---|
| vLLM 启动 | 约 3.5 分钟到 ready |
| 单帧推理 | 212–248ms，8 帧均值 238ms（≈4.2Hz） |
| 显存占用 | 86789 MiB / 97887 MiB（`--gpu-memory-utilization 0.90`） |
| 输出 | `<|action_start|>0 0 ;  ;  ;  ;  ;  ; <|action_end|>`（截图是背包界面，模型给 idle 属正常） |
| 吞吐提升 | 旧 4090 部署是 0.39–0.46s/帧，本次 0.24s/帧 |

`thought` 字段为 null：当前 `promptMode=pretrain`，模型只输出动作 token，不含自然语言思考。

## 远程操作约定（踩过的坑）

- 远端命令一律写成 `.sh` 上传后 `bash xxx.sh`：PowerShell 会把内层双引号吃掉，`grep -E "a|b"` 会变成管道。
- `screen -dmS name bash script` 的 stdout 不进文件，包装脚本里要 `exec >log 2>&1`。
- `pkill -f 脚本名` 会匹配到执行它的 `bash -c` 自身，用字符类规避（`serve_lumin[e]`）。
- Windows 侧 CRLF 会让 bash 报 `set: pipefail: invalid option name`；`_diag/remote.py --put` 已自动去 BOM + CRLF→LF。
