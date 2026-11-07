#!/bin/bash
# AutoDL 端启动飞鱼式的 Lumine 玩家服务（vLLM 8000 + WebSocket 8010）。
set -euo pipefail
export PYTHON_BIN=/root/miniconda3/bin/python
export LUMINE_MODEL_DIR=/root/autodl-tmp/models/Lumine-Agent-Pretrain-VL-7B
export LUMINE_SERVER_TOKEN="$(cat /root/autodl-tmp/lumine_token.txt)"
export LUMINE_HOST=127.0.0.1
export VLLM_EXTRA_ARGS="--max-model-len 8192 --gpu-memory-utilization 0.90"
export VLLM_MM_KWARGS='{"max_pixels":230400}'
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
# flashinfer 的 JIT 需要 ninja 与 nvcc 在 PATH 上，否则采样核编译失败会让引擎起不来。
export PATH=/root/miniconda3/bin:/usr/local/cuda/bin:/usr/local/bin:/usr/bin:/bin
export CUDA_HOME=/usr/local/cuda
export CUDA_PATH=/usr/local/cuda
exec bash /root/autodl-tmp/lumine/serve_lumine.sh
