# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Slim CPU image: OpenAI HTTP frontend + one or more vLLM-style mocker workers (no vLLM / no GPU).
#
# Build from repository root:
#   docker build -f examples/backends/mocker/docker/Dockerfile -t dynamo-mocker-http:local .
#
# Or: docker compose -f examples/backends/mocker/docker/compose.yaml build

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim-bookworm

ARG AI_DYNAMO_VERSION=1.0.1

ARG NATS_VERSION=v2.10.28

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        libgomp1 \
        procps \
    && arch="$(dpkg --print-architecture)" \
    && curl -fsSL -o /tmp/nats-server.deb \
        "https://github.com/nats-io/nats-server/releases/download/${NATS_VERSION}/nats-server-${NATS_VERSION}-${arch}.deb" \
    && dpkg -i /tmp/nats-server.deb \
    && rm /tmp/nats-server.deb \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

# Base ai-dynamo wheel pulls transformers + native runtime; no [vllm] extra.
RUN python -m pip install --upgrade pip \
    && pip install "ai-dynamo==${AI_DYNAMO_VERSION}"

# Optional mock LoRA admin: add only ``mock_lora_http.py`` + patch the *installed* ``main.py``.
# Do not copy the whole git ``dynamo.mocker`` tree onto an older ``ai-dynamo`` wheel — API
# drift (e.g. MockEngineArgs / create_runtime) breaks imports.
COPY components/src/dynamo/mocker/mock_lora_http.py \
    examples/backends/mocker/docker/apply_mock_lora_patch.py \
    /tmp/mock-lora/
RUN M=$(python -c "import dynamo.mocker, pathlib; print(pathlib.Path(dynamo.mocker.__file__).parent)") \
    && install -m0644 /tmp/mock-lora/mock_lora_http.py "${M}/" \
    && python3 /tmp/mock-lora/apply_mock_lora_patch.py \
    && rm -rf /tmp/mock-lora

WORKDIR /opt/dynamo-mocker-http

COPY examples/backends/mocker/docker/entrypoint.bash /opt/dynamo-mocker-http/entrypoint.bash
RUN chmod +x /opt/dynamo-mocker-http/entrypoint.bash

EXPOSE 8000
# When MOCKER_MOCK_LORA_ADMIN_PORT is set (see entrypoint), first worker uses this port, then +1 per model.
EXPOSE 8001

HEALTHCHECK --interval=15s --timeout=5s --retries=20 --start-period=120s \
    CMD curl -sf "http://127.0.0.1:${HTTP_PORT:-8000}/v1/models" | grep -q '"id"' || exit 1

ENTRYPOINT ["/opt/dynamo-mocker-http/entrypoint.bash"]
