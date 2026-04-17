# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""In-process mock LoRA admin HTTP server for ``dynamo.mocker``.

Registers and unregisters LoRA *names* as Model Deployment Cards on the same
``generate`` endpoint as the mocker worker, so ``/v1/chat/completions`` on the
frontend can route ``"model": "<lora_name>"`` like a real backend.

Uses only the stdlib (no Flask) so PyPI wheels stay unchanged. Listens on
``MOCKER_MOCK_LORA_ADMIN_HOST`` (default ``0.0.0.0`` so Docker port publish works)
and ``MOCKER_MOCK_LORA_ADMIN_PORT + worker_id`` when ``MOCKER_MOCK_LORA_ADMIN_PORT``
is set (see mocker docker entrypoint).

Supported routes (OpenAI-style paths often used in tests):

- ``POST /v1/load_lora_adapter`` — JSON ``{"lora_name": str, "lora_path"?: str}``
  or ``{"lora_name": str, "source": {"uri": str}}`` (``lora_path`` / URI are
  ignored for the mock; only the name is published).
- ``POST /v1/unload_lora_adapter`` — JSON ``{"lora_name": str}``
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Coroutine


def _parse_endpoint(endpoint: str) -> tuple[str, str, str]:
    endpoint_str = endpoint.replace("dyn://", "", 1)
    parts = endpoint_str.split(".")
    if len(parts) != 3:
        raise ValueError(
            f"Invalid endpoint format: '{endpoint}'. "
            "Expected 'dyn://namespace.component.endpoint' or 'namespace.component.endpoint'."
        )
    return parts[0], parts[1], parts[2]
from dynamo.llm import (
    ModelInput,
    ModelRuntimeConfig,
    ModelType,
    fetch_model,
    lora_name_to_id,
    register_model,
    unregister_model,
)
from dynamo.runtime import DistributedRuntime

logger = logging.getLogger(__name__)


def _endpoint_dot_path(endpoint: str) -> str:
    return endpoint.replace("dyn://", "", 1)


def _parse_json_body(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


class MockLoraHttpContext:
    """Per-worker state shared with the HTTP handler."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        runtime: DistributedRuntime,
        endpoint_dyn: str,
        base_model_path: str,
        kv_cache_block_size: int,
        runtime_config: ModelRuntimeConfig,
        is_prefill: bool,
    ) -> None:
        self.loop = loop
        self.runtime = runtime
        self.endpoint_dyn = endpoint_dyn
        ns, comp, ep = _parse_endpoint(endpoint_dyn)
        self.generate_endpoint = runtime.endpoint(f"{ns}.{comp}.{ep}")
        self.base_model_path = base_model_path
        self.kv_cache_block_size = kv_cache_block_size
        self.runtime_config = runtime_config
        self.model_type: ModelType = (
            ModelType.Prefill if is_prefill else ModelType.Chat | ModelType.Completions
        )

    def run_coro(self, coro: Coroutine[Any, Any, Any], timeout: float) -> Any:
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    async def load_lora(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        lora_name = body.get("lora_name")
        if not lora_name or not isinstance(lora_name, str):
            return (
                400,
                {"status": "error", "message": "lora_name is required (non-empty string)"},
            )

        source = body.get("source")
        if isinstance(source, dict) and source.get("uri"):
            pass
        elif body.get("lora_path"):
            pass
        else:
            # Accept name-only loads for mock ergonomics; real backends want URI.
            logger.debug(
                "mock LoRA load without source.uri or lora_path (ok for mocker): %s",
                lora_name,
            )

        user_data = {
            "lora_adapter": True,
            "lora_id": lora_name_to_id(lora_name),
        }
        try:
            # register_model's Rust path resolves base_model_path on disk; for an HF id it
            # otherwise fetches *full* weights (ignore_weights=false), which fights the
            # running mocker's HF cache locks. Use tokenizer-only cache like the mocker worker.
            resolved = await fetch_model(self.base_model_path, ignore_weights=True)
            base_path = str(resolved)
            await register_model(
                ModelInput.Tokens,
                self.model_type,
                self.generate_endpoint,
                base_path,
                kv_cache_block_size=self.kv_cache_block_size,
                runtime_config=self.runtime_config,
                user_data=user_data,
                lora_name=lora_name,
                base_model_path=base_path,
            )
        except Exception as e:
            logger.exception("register_model (LoRA) failed for %s", lora_name)
            return (
                500,
                {"status": "error", "message": str(e), "lora_name": lora_name},
            )
        return (
            200,
            {
                "status": "success",
                "lora_name": lora_name,
                "message": "mock LoRA registered (MDC only; no weights)",
            },
        )

    async def unload_lora(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        lora_name = body.get("lora_name")
        if not lora_name or not isinstance(lora_name, str):
            return (
                400,
                {"status": "error", "message": "lora_name is required (non-empty string)"},
            )
        try:
            await unregister_model(self.generate_endpoint, lora_name=lora_name)
        except Exception as e:
            logger.warning("unregister_model failed for %s: %s", lora_name, e)
            return (
                404,
                {"status": "error", "message": str(e), "lora_name": lora_name},
            )
        return 200, {"status": "success", "lora_name": lora_name}


def _make_handler(
    ctx: MockLoraHttpContext,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            logger.debug(format, *args)

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path != "/v1/load_lora_adapter" and path != "/v1/unload_lora_adapter":
                self._send_json(404, {"status": "error", "message": f"unknown path {path}"})
                return
            try:
                body = _parse_json_body(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except (json.JSONDecodeError, ValueError) as e:
                self._send_json(400, {"status": "error", "message": f"invalid JSON: {e}"})
                return

            try:
                if path == "/v1/load_lora_adapter":
                    status, payload = ctx.run_coro(ctx.load_lora(body), timeout=120.0)
                else:
                    status, payload = ctx.run_coro(ctx.unload_lora(body), timeout=60.0)
            except Exception as e:
                logger.exception("mock LoRA admin request failed")
                self._send_json(500, {"status": "error", "message": str(e)})
                return
            self._send_json(status, payload)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] in ("/health", "/healthz"):
                self._send_json(200, {"status": "ok"})
                return
            self._send_json(404, {"status": "error", "message": "not found"})

    return Handler


def start_mock_lora_admin_http(
    *,
    loop: asyncio.AbstractEventLoop,
    runtime: DistributedRuntime,
    endpoint_dyn: str,
    base_model_path: str,
    kv_cache_block_size: int,
    runtime_config: ModelRuntimeConfig,
    is_prefill: bool,
    host: str,
    port: int,
) -> ThreadingHTTPServer:
    ctx = MockLoraHttpContext(
        loop=loop,
        runtime=runtime,
        endpoint_dyn=endpoint_dyn,
        base_model_path=base_model_path,
        kv_cache_block_size=kv_cache_block_size,
        runtime_config=runtime_config,
        is_prefill=is_prefill,
    )
    handler_cls = _make_handler(ctx)
    httpd = ThreadingHTTPServer((host, port), handler_cls)

    def serve() -> None:
        logger.info("mock LoRA admin listening on http://%s:%s", host, port)
        httpd.serve_forever()

    t = threading.Thread(target=serve, name=f"mock-lora-admin-{port}", daemon=True)
    t.start()
    return httpd


def mock_lora_admin_port_for_worker(worker_id: int) -> int | None:
    raw = os.environ.get("MOCKER_MOCK_LORA_ADMIN_PORT")
    if raw is None or raw.strip() == "":
        return None
    base = int(raw)
    return base + worker_id


def mock_lora_admin_host() -> str:
    # Default 0.0.0.0 so Docker-published ports (e.g. -p 8001:8001) reach this process;
    # binding 127.0.0.1 alone often rejects traffic from the bridge network.
    return os.environ.get("MOCKER_MOCK_LORA_ADMIN_HOST", "0.0.0.0")
