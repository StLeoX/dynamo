#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Starts nats-server (in-process sidecar), dynamo.frontend, and N dynamo.mocker workers using
# file discovery (no etcd). Chat requests must include "max_tokens" (mocker requires it).
# Single published HTTP port (default 30000): OpenAI routes + GET /metrics (SGLang-style text).

set -euo pipefail

HTTP_PORT="${HTTP_PORT:-30000}"
UPSTREAM_HTTP_PORT="${UPSTREAM_HTTP_PORT:-18000}"
DYN_FILE_KV="${DYN_FILE_KV:-/var/lib/dynamo/file-kv}"
MOCK_SPEEDUP="${MOCK_SPEEDUP:-100000}"
MOCKER_ENGINE_TYPE="${MOCKER_ENGINE_TYPE:-sglang}"

# Default: three models for multi-registration (HF id used for tokenizer + served model name).
MODEL_1="${MODEL_1:-deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B}"
MODEL_2="${MODEL_2:-deepseek-ai/DeepSeek-R1-Distill-Qwen-7B}"
MODEL_3="${MODEL_3:-Qwen/Qwen3-8B}"

# Space-separated HuggingFace model ids; overrides MODEL_1 / MODEL_2 / MODEL_3 when set.
# Example: MOCKER_MODELS="org/A org/B"
MOCKER_MODELS="${MOCKER_MODELS:-}"

export DYN_DISCOVERY_BACKEND="${DYN_DISCOVERY_BACKEND:-file}"
export DYN_FILE_KV
# ai-dynamo 1.0.x mocker still expects a reachable NATS by default; ship nats-server in-image.
export NATS_SERVER="${NATS_SERVER:-nats://127.0.0.1:4222}"
# When unset, runtime defaults to NATS event plane — keep in sync with NATS_SERVER above.
export DYN_EVENT_PLANE="${DYN_EVENT_PLANE:-nats}"
export HF_HOME="${HF_HOME:-/root/.cache/huggingface}"

# Must match worker namespace (see dynamo.common.utils.namespace.get_worker_namespace).
MOCKER_NAMESPACE="${DYN_NAMESPACE:-dynamo}"

mkdir -p "${DYN_FILE_KV}" "${HF_HOME}"

# Stable short id for unique Dynamo endpoint names (EndpointId has no per-instance field).
_mocker_endpoint_suffix() {
  printf '%s' "$1" | sha256sum | awk '{print substr($1,1,16)}'
}

if [[ -n "${MOCKER_MODELS// }" ]]; then
  # shellcheck disable=SC2206
  models=( ${MOCKER_MODELS} )
else
  models=( "${MODEL_1}" "${MODEL_2}" "${MODEL_3}" )
fi

# Same list for request_proxy GET /metrics (space-separated).
export MOCKER_METRICS_MODELS="${models[*]}"

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
echo "[entrypoint] mocker engine=${MOCKER_ENGINE_TYPE}"
echo "[entrypoint] models: ${models[*]}"

nats-server -p 4222 -a 127.0.0.1 &
pids+=("$!")
sleep 0.5

# Prefer env (DYN_*) for event plane so both PyPI 1.0.x and newer releases behave consistently.
# Frontend binds to an internal port; a lightweight proxy on HTTP_PORT applies request compatibility patches.
python3 -m dynamo.frontend \
  --http-port "${UPSTREAM_HTTP_PORT}" \
  --discovery-backend "${DYN_DISCOVERY_BACKEND}" \
  --router-mode "${ROUTER_MODE:-round-robin}" &
pids+=("$!")

# Start compatibility proxy:
# - strips top-level include_usage for non-stream chat requests
# - GET /metrics -> synthetic SGLang Prometheus text
# - transparently forwards all other requests
PROXY_PORT="${HTTP_PORT}" UPSTREAM_PORT="${UPSTREAM_HTTP_PORT}" \
  python3 /opt/dynamo-mocker-vllm/request_proxy.py &
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
  # Each OS process must use a distinct dyn endpoint (namespace.component.name); otherwise
  # both claim dynamo/backend/generate and one worker's run_input exits immediately.
  mocker_endpoint_args=( )
  if [[ ${#models[@]} -gt 1 ]]; then
    _suf="$(_mocker_endpoint_suffix "${m}")"
    mocker_endpoint_args=( --endpoint "dyn://${MOCKER_NAMESPACE}.backend.generate_${_suf}" )
    echo "[entrypoint]   -> ${mocker_endpoint_args[*]}"
  elif [[ -n "${MOCKER_ENDPOINT:-}" ]]; then
    mocker_endpoint_args=( --endpoint "${MOCKER_ENDPOINT}" )
  fi
  python3 -m dynamo.mocker \
    --discovery-backend "${DYN_DISCOVERY_BACKEND}" \
    --model-path "${m}" \
    --model-name "${m}" \
    "${mocker_endpoint_args[@]}" \
    --engine-type "${MOCKER_ENGINE_TYPE}" \
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

# Block until all background jobs finish. Do not use `wait -n`: the first exiting
# child (e.g. one mocker or transient subprocess) would unblock the script, trigger
# EXIT cleanup, and tear down NATS/frontend/proxy while others are still healthy.
wait
