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
#   By default this script forces linux/amd64 (x86_64) by rebuilding SOURCE_IMAGE
#   with docker buildx before push.
#   To use a different architecture, override BUILD_PLATFORM when running:
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
BUILD_PLATFORM="${BUILD_PLATFORM:-linux/amd64}"

REGISTRY="ghcr.io"
DEST="${REGISTRY}/${GHCR_OWNER}/${GHCR_IMAGE_NAME}:${VERSION}"

TOKEN="${GHCR_TOKEN:-${GITHUB_TOKEN:-}}"
if [[ -z "${TOKEN}" ]]; then
  echo "error: set GHCR_TOKEN or GITHUB_TOKEN to a PAT with write:packages" >&2
  exit 1
fi

if [[ -n "${BUILD_PLATFORM:-}" ]]; then
  echo "[push-ghcr] buildx --platform ${BUILD_PLATFORM} -> ${SOURCE_IMAGE}"
  if ! docker buildx version >/dev/null 2>&1; then
    echo "error: docker buildx is required when BUILD_PLATFORM is set" >&2
    exit 1
  fi
  AI_DYNAMO_VERSION="${AI_DYNAMO_VERSION:-1.0.1}"
  AI_DYNAMO_INSTALL_SOURCE="${AI_DYNAMO_INSTALL_SOURCE:-1}"
  docker buildx build \
    --platform "${BUILD_PLATFORM}" \
    -f examples/backends/mocker/docker/Dockerfile \
    --build-arg "AI_DYNAMO_VERSION=${AI_DYNAMO_VERSION}" \
    --build-arg "AI_DYNAMO_INSTALL_SOURCE=${AI_DYNAMO_INSTALL_SOURCE}" \
    -t "${SOURCE_IMAGE}" \
    --load \
    "${ROOT}"
fi

if ! docker image inspect "${SOURCE_IMAGE}" >/dev/null 2>&1; then
  echo "error: source image not found: ${SOURCE_IMAGE}" >&2
  echo "  build first, e.g.:" >&2
  echo "  docker compose -f examples/backends/mocker/docker/compose.yaml build" >&2
  echo "  or: BUILD_PLATFORM=linux/amd64 $0 ..." >&2
  exit 1
fi

_arch="$(docker image inspect -f '{{.Architecture}}' "${SOURCE_IMAGE}" 2>/dev/null || echo "?")"
_os="$(docker image inspect -f '{{.Os}}' "${SOURCE_IMAGE}" 2>/dev/null || echo "?")"
echo "[push-ghcr] source image OS/Arch: ${_os}/${_arch}"

echo "[push-ghcr] logging in to ${REGISTRY} as ${GHCR_USERNAME}"
printf '%s' "${TOKEN}" | docker login "${REGISTRY}" -u "${GHCR_USERNAME}" --password-stdin

echo "[push-ghcr] tagging ${SOURCE_IMAGE} -> ${DEST}"
docker tag "${SOURCE_IMAGE}" "${DEST}"

echo "[push-ghcr] pushing ${DEST}"
docker push "${DEST}"

echo "[push-ghcr] done. Pull with:"
echo "  docker pull ${DEST}"
