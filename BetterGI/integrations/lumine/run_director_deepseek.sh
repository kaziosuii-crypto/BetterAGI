#!/usr/bin/env bash
# 用 DeepSeek 官方 API + OpenViking 启动导演（Git Bash）。
# 密钥只从 tools/deepseek_key.txt 读取，不写进仓库。
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-/e/BetterAGI/Amaidesu/.venv/Scripts/python.exe}"
KEY_FILE="${DEEPSEEK_KEY_FILE:-/e/BetterAGI/tools/deepseek_key.txt}"

if [[ ! -f "$KEY_FILE" ]]; then
    echo "找不到 DeepSeek 密钥文件：$KEY_FILE" >&2
    exit 2
fi

export LLM_PROVIDER=openai
export OPENAI_API_KEY="$(cat "$KEY_FILE")"
export OPENAI_BASE_URL="https://api.deepseek.com/v1"
export OPENAI_MODEL="deepseek-flash"
export LLM_MAX_TOKENS="${LLM_MAX_TOKENS:-4096}"
export OPENAI_REASONING_EFFORT="${OPENAI_REASONING_EFFORT:-low}"

export OPENVIKING_ENABLED="${OPENVIKING_ENABLED:-1}"
export OPENVIKING_URL="${OPENVIKING_URL:-http://127.0.0.1:1933}"

exec "$PYTHON_BIN" "$HERE/live_director.py" "$@"
