# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Slim CPU image: OpenAI HTTP frontend + one or more SGLang-style mocker workers (no GPU).
# Multi-stage: builder installs ai-dynamo into a venv; the final image copies only that venv
# plus nats + entry scripts — no monorepo source tree in the runtime layer.
#
# Build from repository root:
#   docker build -f examples/backends/mocker/docker/Dockerfile -t dynamo-mocker-sglang:local .
#
# Or: docker compose -f examples/backends/mocker/docker/compose.yaml build

ARG PYTHON_VERSION=3.12

# --- builder: compile / install Dynamo Python stack into /opt/venv ---
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ARG AI_DYNAMO_VERSION=1.0.1
# Default 1: SGLang mocker needs current tree (e.g. --engine-type). Use 0 for PyPI-only (older mocker CLI).
ARG AI_DYNAMO_INSTALL_SOURCE=1

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

# Toolchain: full Rust/clang only when building from local source; PyPI path stays light.
RUN if [ "${AI_DYNAMO_INSTALL_SOURCE}" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        clang \
        curl \
        git \
        libclang-dev \
        pkg-config \
        protobuf-compiler \
      && rm -rf /var/lib/apt/lists/*; \
    else \
      apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
      && rm -rf /var/lib/apt/lists/*; \
    fi

ENV RUSTUP_HOME=/opt/rust/rustup \
    CARGO_HOME=/opt/rust/cargo \
    PATH=/opt/rust/cargo/bin:${PATH}

RUN if [ "${AI_DYNAMO_INSTALL_SOURCE}" = "1" ]; then \
      curl https://sh.rustup.rs -sSf | sh -s -- -y --profile minimal --default-toolchain stable; \
    fi

COPY . /tmp/dynamo-src

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

RUN python -m pip install --upgrade pip \
    && if [ "${AI_DYNAMO_INSTALL_SOURCE}" = "1" ]; then \
         _lc="$(find /usr/lib/llvm-* -maxdepth 3 -name 'libclang.so*' ! -name '*.a' 2>/dev/null | head -n1)"; \
         if [ -z "${_lc}" ]; then \
           _lc="$(find /usr/lib -name 'libclang.so*' ! -name '*.a' 2>/dev/null | head -n1)"; \
         fi; \
         if [ -z "${_lc}" ]; then echo "ERROR: libclang not found (need clang + libclang-dev)"; exit 1; fi; \
         export LIBCLANG_PATH="$(dirname "${_lc}")"; \
         echo "[build] LIBCLANG_PATH=${LIBCLANG_PATH}"; \
         pip install /tmp/dynamo-src/lib/bindings/python \
         && pip install /tmp/dynamo-src; \
       else \
         pip install "ai-dynamo==${AI_DYNAMO_VERSION}"; \
       fi

# --- runtime: no source, no Rust, no compilers ---
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG NATS_VERSION=v2.10.28

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libgomp1 \
        procps \
    && arch="$(dpkg --print-architecture)" \
    && curl -fsSL -o /tmp/nats-server.deb \
        "https://github.com/nats-io/nats-server/releases/download/${NATS_VERSION}/nats-server-${NATS_VERSION}-${arch}.deb" \
    && dpkg -i /tmp/nats-server.deb \
    && rm /tmp/nats-server.deb \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /opt/dynamo-mocker-vllm

COPY examples/backends/mocker/docker/entrypoint.bash /opt/dynamo-mocker-vllm/entrypoint.bash
COPY examples/backends/mocker/docker/request_proxy.py /opt/dynamo-mocker-vllm/request_proxy.py
RUN chmod +x /opt/dynamo-mocker-vllm/entrypoint.bash

EXPOSE 30000

HEALTHCHECK --interval=15s --timeout=5s --retries=20 --start-period=120s \
    CMD curl -sf "http://127.0.0.1:${HTTP_PORT:-30000}/v1/models" | grep -q '"id"' || exit 1

ENTRYPOINT ["/opt/dynamo-mocker-vllm/entrypoint.bash"]
