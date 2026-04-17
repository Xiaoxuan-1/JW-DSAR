#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${JWFLARE_LOG_DIR:-$ROOT_DIR/logs}"
LOG_FILE="${JWFLARE_LOG_FILE:-$LOG_DIR/jwflare.log}"
PORT="${JWFLARE_PORT:-2222}"
SIZE_FACTOR="${SIZE_FACTOR:-4}"
MAX_PIXELS="${MAX_PIXELS:-52112}"
CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
CONDA_BIN="${JWFLARE_CONDA_BIN:-conda}"
CONDA_ENV="${JWFLARE_CONDA_ENV:-qwen}"
SWIFT_BIN="${JWFLARE_SWIFT_BIN:-swift}"
CKPT_DIR="${JWFLARE_CKPT_DIR:-}"

if [[ -z "$CKPT_DIR" ]]; then
  echo "Set JWFLARE_CKPT_DIR before starting JW-Flare."
  exit 1
fi

mkdir -p "$LOG_DIR"

RUN_CMD="\"$SWIFT_BIN\" deploy --ckpt_dir \"$CKPT_DIR\" --port \"$PORT\""
if command -v "$CONDA_BIN" >/dev/null 2>&1; then
  RUN_CMD="\"$CONDA_BIN\" run -n \"$CONDA_ENV\" $RUN_CMD"
fi

nohup bash -c \
  "SIZE_FACTOR=$SIZE_FACTOR MAX_PIXELS=$MAX_PIXELS CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES $RUN_CMD" \
  >"$LOG_FILE" 2>&1 & disown

echo "JW-Flare started on port $PORT"
echo "Log file: $LOG_FILE"
