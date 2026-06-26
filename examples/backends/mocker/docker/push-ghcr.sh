#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Tag and push the local mocker HTTP image to GitHub Packages (GHCR):
#   https://github.com/users/faust-benchou/packages?repo_name=dynamo-mocker-sglang
#
# Prerequisites:
#   - Docker image already built (e.g. compose build): default source tag dynamo-mocker-sglang:local
#   - A GitHub Personal Access Token (classic) with scope: write:packages
#     (and read:packages if the package is private). Fine-grained tokens: "Contents" read
#     is not enough; use classic PAT or a token with Packages write.
#
# Architecture:
#   By default this script builds for linux/arm64 on Apple Silicon and linux/amd64
#   on x86_64 hosts, using docker buildx with --load for the single platform.
#   To push a multi-platform manifest (both amd64 + arm64), set BUILD_PLATFORM
#   explicitly — the script will use --push and skip --load:
#     BUILD_PLATFORM=linux/amd64,linux/arm64 ./examples/backends/mocker/docker/push-ghcr.sh
#   To force a specific single platform, override BUILD_PLATFORM:
#     BUILD_PLATFORM=linux/amd64 ./examples/backends/mocker/docker/push-ghcr.sh
#     BUILD_PLATFORM=linux/arm64 ./examples/backends/mocker/docker/push-ghcr.sh
#
# Usage (from repository root):
#   export GHCR_TOKEN=ghp_xxxx          # or GITHUB_TOKEN if you use a PAT variable name
#   ./examples/backends/mocker/docker/push-ghcr.sh              # pushes :latest
#   ./examples/backends/mocker/docker/push-ghcr.sh v2026-04-17   # pushes :v2026-04-17
#
# Environment overrides:
#   GHCR_OWNER=faust-benchou
#   GHCR_IMAGE_NAME=dynamo-mocker-sglang
#   SOURCE_IMAGE=dynamo-mocker-sglang:local
#   GHCR_USERNAME=faust-benchou        # login user (usually same as owner for personal accounts)
#   BUILD_PLATFORM=linux/amd64         # default: buildx (re)build this arch into SOURCE_IMAGE first
#   AI_DYNAMO_VERSION=1.0.1          # optional: PyPI pin when using BUILD_PLATFORM

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "${ROOT}"

VERSION="${1:-latest}"
GHCR_OWNER="${GHCR_OWNER:-faust-benchou}"
GHCR_IMAGE_NAME="${GHCR_IMAGE_NAME:-dynamo-mocker-sglang}"
SOURCE_IMAGE="${SOURCE_IMAGE:-dynamo-mocker-sglang:local}"
GHCR_USERNAME="${GHCR_USERNAME:-${GHCR_OWNER}}"

# Auto-detect host arch when BUILD_PLATFORM is unset:
# Apple Silicon → linux/arm64, x86_64 → linux/amd64
if [[ -z "${BUILD_PLATFORM:-}" ]]; then
  _host_arch="$(uname -m)"
  case "${_host_arch}" in
    arm64|aarch64) BUILD_PLATFORM="linux/arm64" ;;
    *)             BUILD_PLATFORM="linux/amd64" ;;
  esac
fi

# Multi-platform → must use --push directly; single-platform → build with --load, then tag+push
_is_multi="false"
if [[ "${BUILD_PLATFORM}" == *","* ]]; then
  _is_multi="true"
fi

REGISTRY="ghcr.io"
DEST="${REGISTRY}/${GHCR_OWNER}/${GHCR_IMAGE_NAME}:${VERSION}"

TOKEN="${GHCR_TOKEN:-${GITHUB_TOKEN:-}}"
if [[ -z "${TOKEN}" ]]; then
  echo "error: set GHCR_TOKEN or GITHUB_TOKEN to a PAT with write:packages" >&2
  exit 1
fi

if ! docker buildx version >/dev/null 2>&1; then
  echo "error: docker buildx is required" >&2
  exit 1
fi

AI_DYNAMO_VERSION="${AI_DYNAMO_VERSION:-1.0.1}"
AI_DYNAMO_INSTALL_SOURCE="${AI_DYNAMO_INSTALL_SOURCE:-1}"

BUILDX_BASE=(
  --platform "${BUILD_PLATFORM}"
  -f examples/backends/mocker/docker/Dockerfile
  --build-arg "AI_DYNAMO_VERSION=${AI_DYNAMO_VERSION}"
  --build-arg "AI_DYNAMO_INSTALL_SOURCE=${AI_DYNAMO_INSTALL_SOURCE}"
)

if [[ "${_is_multi}" == "true" ]]; then
  echo "[push-ghcr] multi-platform buildx + push -> ${DEST} (platforms: ${BUILD_PLATFORM})"
  docker buildx build \
    "${BUILDX_BASE[@]}" \
    -t "${DEST}" \
    --push \
    "${ROOT}"
else
  echo "[push-ghcr] buildx --platform ${BUILD_PLATFORM} -> ${SOURCE_IMAGE}"
  docker buildx build \
    "${BUILDX_BASE[@]}" \
    -t "${SOURCE_IMAGE}" \
    --load \
    "${ROOT}"

  _arch="$(docker image inspect -f '{{.Architecture}}' "${SOURCE_IMAGE}" 2>/dev/null || echo "?")"
  _os="$(docker image inspect -f '{{.Os}}' "${SOURCE_IMAGE}" 2>/dev/null || echo "?")"
  echo "[push-ghcr] source image OS/Arch: ${_os}/${_arch}"

  echo "[push-ghcr] logging in to ${REGISTRY} as ${GHCR_USERNAME}"
  printf '%s' "${TOKEN}" | docker login "${REGISTRY}" -u "${GHCR_USERNAME}" --password-stdin

  echo "[push-ghcr] tagging ${SOURCE_IMAGE} -> ${DEST}"
  docker tag "${SOURCE_IMAGE}" "${DEST}"

  echo "[push-ghcr] pushing ${DEST}"
  docker push "${DEST}"
fi

echo "[push-ghcr] done. Pull with:"
echo "  docker pull ${DEST}"
