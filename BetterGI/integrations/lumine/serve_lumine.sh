#!/usr/bin/env bash
# 在 AutoDL 上启动 Lumine 玩家服务：
#   1. 按需下载 koboshchan/Lumine-Agent-Pretrain-VL-7B；
#   2. 启动 vLLM OpenAI 兼容服务（默认 127.0.0.1:8000）；
#   3. 启动 autodl_server.py（默认 0.0.0.0:8010）。
#
# 常用环境变量：
#   LUMINE_MOCK=1              跳过模型与 vLLM，返回脚本动作
#   LUMINE_SERVER_TOKEN=...    WebSocket 鉴权令牌（强烈建议设置）
#   LUMINE_MODEL_DIR=...       模型本地目录
#   HF_MIRROR=https://hf-mirror.com  下载镜像（写入 HF_ENDPOINT）
#   VLLM_EXTRA_ARGS="..."      追加给 vLLM 的参数，例如 --max-model-len 8192
#   LUMINE_VENV=/path/to/venv  可选 Python 虚拟环境
#
# 额外参数会原样传给 autodl_server.py。
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python}"
MODEL_REPO="${LUMINE_MODEL_REPO:-koboshchan/Lumine-Agent-Pretrain-VL-7B}"
MODEL_DIR="${LUMINE_MODEL_DIR:-$SCRIPT_DIR/models/Lumine-Agent-Pretrain-VL-7B}"
VLLM_PORT="${VLLM_PORT:-8000}"
LUMINE_PORT="${LUMINE_PORT:-8010}"
LUMINE_HOST="${LUMINE_HOST:-0.0.0.0}"
SERVED_NAME="${LUMINE_SERVED_MODEL_NAME:-lumine-agent-vl-7b}"
MOCK="${LUMINE_MOCK:-0}"

# 支持 hf-mirror 风格的环境变量。
if [[ -n "${HF_MIRROR:-}" && -z "${HF_ENDPOINT:-}" ]]; then
    export HF_ENDPOINT="$HF_MIRROR"
fi

# 可选虚拟环境：显式 LUMINE_VENV 优先，其次脚本旁的 .venv。
if [[ -n "${LUMINE_VENV:-}" && -f "$LUMINE_VENV/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$LUMINE_VENV/bin/activate"
elif [[ -f "$SCRIPT_DIR/.venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.venv/bin/activate"
fi

SERVER_ARGS=(
    --host "$LUMINE_HOST"
    --port "$LUMINE_PORT"
    --model "$SERVED_NAME"
    --vllm-url "http://127.0.0.1:$VLLM_PORT"
)
if [[ -n "${LUMINE_SERVER_TOKEN:-}" ]]; then
    SERVER_ARGS+=(--token "$LUMINE_SERVER_TOKEN")
fi

if [[ "$MOCK" == "1" ]]; then
    echo "LUMINE_MOCK=1: skipping model download and vLLM"
    exec "$PYTHON_BIN" autodl_server.py "${SERVER_ARGS[@]}" --mock "$@"
fi

if [[ -z "${LUMINE_SERVER_TOKEN:-}" ]]; then
    echo "warning: LUMINE_SERVER_TOKEN is empty; the WebSocket endpoint will accept anyone" >&2
fi

if [[ ! -d "$MODEL_DIR" || -z "$(ls -A "$MODEL_DIR" 2>/dev/null || true)" ]]; then
    echo "Downloading $MODEL_REPO -> $MODEL_DIR"
    mkdir -p "$(dirname "$MODEL_DIR")"
    if command -v hf >/dev/null 2>&1; then
        hf download "$MODEL_REPO" --local-dir "$MODEL_DIR"
    elif command -v huggingface-cli >/dev/null 2>&1; then
        huggingface-cli download "$MODEL_REPO" --local-dir "$MODEL_DIR"
    else
        echo "error: neither 'hf' nor 'huggingface-cli' found; pip install huggingface_hub" >&2
        exit 1
    fi
fi

VLLM_LOG="${VLLM_LOG:-$SCRIPT_DIR/vllm.log}"
VLLM_ARGS=(
    --model "$MODEL_DIR"
    --served-model-name "$SERVED_NAME"
    --dtype bfloat16
    --host 127.0.0.1
    --port "$VLLM_PORT"
)
if [[ -n "${VLLM_EXTRA_ARGS:-}" ]]; then
    read -r -a EXTRA_ARGS <<< "$VLLM_EXTRA_ARGS"
    VLLM_ARGS+=("${EXTRA_ARGS[@]}")
fi

# 多模态处理器参数（JSON 原样传入，避免被空格拆分）。
if [[ -n "${VLLM_MM_KWARGS:-}" ]]; then
    VLLM_ARGS+=(--mm-processor-kwargs "$VLLM_MM_KWARGS")
fi

echo "Starting vLLM on 127.0.0.1:$VLLM_PORT (log: $VLLM_LOG)"
"$PYTHON_BIN" -m vllm.entrypoints.openai.api_server "${VLLM_ARGS[@]}" >"$VLLM_LOG" 2>&1 &
VLLM_PID=$!

cleanup() {
    if kill -0 "$VLLM_PID" 2>/dev/null; then
        echo "Stopping vLLM (pid $VLLM_PID)"
        kill "$VLLM_PID" 2>/dev/null || true
        wait "$VLLM_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# 7B 权重首次加载通常需要 1～5 分钟。
VLLM_READY=0
for _ in $(seq 1 150); do
    if curl -fsS "http://127.0.0.1:$VLLM_PORT/health" >/dev/null 2>&1; then
        VLLM_READY=1
        break
    fi
    if ! kill -0 "$VLLM_PID" 2>/dev/null; then
        echo "error: vLLM exited during startup" >&2
        tail -n 80 "$VLLM_LOG" >&2 || true
        exit 1
    fi
    sleep 2
done

if [[ "$VLLM_READY" != "1" ]]; then
    echo "error: vLLM did not become healthy within 300 seconds" >&2
    tail -n 80 "$VLLM_LOG" >&2 || true
    exit 1
fi

echo "vLLM is ready; starting Lumine player server on $LUMINE_HOST:$LUMINE_PORT"
"$PYTHON_BIN" autodl_server.py "${SERVER_ARGS[@]}" "$@"
