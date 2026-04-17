#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Starts nats-server (in-process sidecar), dynamo.frontend, and N dynamo.mocker workers using
# file discovery (no etcd). Chat requests must include "max_tokens" (mocker requires it).

set -euo pipefail

HTTP_PORT="${HTTP_PORT:-8000}"
DYN_FILE_KV="${DYN_FILE_KV:-/var/lib/dynamo/file-kv}"
MOCK_SPEEDUP="${MOCK_SPEEDUP:-100000}"

# Default: two DeepSeek distill Qwen mockers (HF id used for tokenizer + served model name).
MODEL_1="${MODEL_1:-deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B}"
MODEL_2="${MODEL_2:-deepseek-ai/DeepSeek-R1-Distill-Qwen-7B}"

# Space-separated HuggingFace model ids; overrides MODEL_1 / MODEL_2 when set.
# Example: MOCKER_MODELS="org/A org/B"
MOCKER_MODELS="${MOCKER_MODELS:-}"

export DYN_DISCOVERY_BACKEND="${DYN_DISCOVERY_BACKEND:-file}"
export DYN_FILE_KV
# ai-dynamo 1.0.x mocker still expects a reachable NATS by default; ship nats-server in-image.
export NATS_SERVER="${NATS_SERVER:-nats://127.0.0.1:4222}"
# When unset, runtime defaults to NATS event plane — keep in sync with NATS_SERVER above.
export DYN_EVENT_PLANE="${DYN_EVENT_PLANE:-nats}"
export HF_HOME="${HF_HOME:-/root/.cache/huggingface}"

mkdir -p "${DYN_FILE_KV}" "${HF_HOME}"

if [[ -n "${MOCKER_MODELS// }" ]]; then
  # shellcheck disable=SC2206
  models=( ${MOCKER_MODELS} )
else
  models=( "${MODEL_1}" "${MODEL_2}" )
fi

pids=()

cleanup() {
  local pid
  for pid in "${pids[@]:-}"; do
    if kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
}

trap cleanup EXIT INT TERM

echo "[entrypoint] discovery=${DYN_DISCOVERY_BACKEND} file_kv=${DYN_FILE_KV} nats=${NATS_SERVER} event_plane=${DYN_EVENT_PLANE}"
echo "[entrypoint] models: ${models[*]}"

nats-server -p 4222 -a 127.0.0.1 &
pids+=("$!")
sleep 0.5

# Prefer env (DYN_*) for event plane so both PyPI 1.0.x and newer releases behave consistently.
python3 -m dynamo.frontend \
  --http-port "${HTTP_PORT}" \
  --discovery-backend "${DYN_DISCOVERY_BACKEND}" \
  --router-mode "${ROUTER_MODE:-round-robin}" &
pids+=("$!")

# Let the frontend bind before workers register.
sleep 2

extra_mocker=( )
if [[ -n "${MOCKER_EXTRA_ARGS:-}" ]]; then
  # shellcheck disable=SC2206
  extra_mocker=( ${MOCKER_EXTRA_ARGS} )
fi

for m in "${models[@]}"; do
  echo "[entrypoint] starting mocker for ${m}"
  python3 -m dynamo.mocker \
    --discovery-backend "${DYN_DISCOVERY_BACKEND}" \
    --model-path "${m}" \
    --model-name "${m}" \
    --speedup-ratio "${MOCK_SPEEDUP}" \
    "${extra_mocker[@]}" &
  pids+=("$!")
  # Stagger worker registration slightly to reduce discovery races on slow disks.
  sleep 1
done

echo "[entrypoint] waiting for at least one registered model on :${HTTP_PORT} (up to 600s)..."
ready=0
for _ in $(seq 1 600); do
  if curl -sf "http://127.0.0.1:${HTTP_PORT}/v1/models" | grep -q '"id"'; then
    echo "[entrypoint] models registered"
    ready=1
    break
  fi
  sleep 1
done

if [[ "${ready}" -ne 1 ]]; then
  echo "[entrypoint] ERROR: no models registered in time (see worker logs)" >&2
  exit 1
fi

wait -n
