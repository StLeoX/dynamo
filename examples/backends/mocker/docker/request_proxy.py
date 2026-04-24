#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Lightweight reverse proxy for mocker image compatibility tweaks.

- For POST /v1/chat/completions requests where JSON body contains `stream: false`,
  remove top-level `include_usage` before forwarding to dynamo.frontend.
- GET /metrics returns synthetic SGLang-style Prometheus text for models listed in
  ``MOCKER_METRICS_MODELS`` (space-separated), without opening a second listen port.
"""

from __future__ import annotations

import hashlib
import json
import os
from http import client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


LISTEN_HOST = os.environ.get("PROXY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("PROXY_PORT", "30000"))
UPSTREAM_HOST = os.environ.get("UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("UPSTREAM_PORT", "18000"))


def _path_without_query(path: str) -> str:
    return path.split("?", 1)[0]


def _metrics_model_names() -> list[str]:
    raw = os.environ.get("MOCKER_METRICS_MODELS", "").strip()
    if not raw:
        return ["mock-model"]
    return raw.split()


def _escape_prometheus_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _stable_int(seed: str, mod: int) -> int:
    h = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12], 16)
    return h % mod if mod else 0


def _metrics_values_for_model(model_name: str) -> dict[str, float | int]:
    """Synthetic values for one model (used to build valid single-family Prometheus text)."""
    seed = model_name
    token_usage = 0.12 + (_stable_int(seed + ":tu", 7800) / 10000.0)
    token_usage = min(max(token_usage, 0.0), 1.0)
    num_queue_reqs = _stable_int(seed + ":q", 6)
    ttft_count = 80 + _stable_int(seed + ":tc", 900)
    ttft_sum = round(0.02 * ttft_count + _stable_int(seed + ":ts", 100) / 1000.0, 6)
    tpot_count = 120 + _stable_int(seed + ":pc", 800)
    tpot_sum = round(0.015 * tpot_count + _stable_int(seed + ":ps", 100) / 1000.0, 6)
    ttft_b08 = int(ttft_count * 0.3)
    tpot_b005 = int(tpot_count * 0.5)
    return {
        "token_usage": token_usage,
        "num_queue_reqs": num_queue_reqs,
        "ttft_count": ttft_count,
        "ttft_sum": ttft_sum,
        "ttft_b08": ttft_b08,
        "tpot_count": tpot_count,
        "tpot_sum": tpot_sum,
        "tpot_b005": tpot_b005,
    }


def render_sglang_metrics_text() -> str:
    """One HELP/TYPE per metric name; multiple models => multiple samples (Prometheus text rules)."""
    models = _metrics_model_names()
    per_model = [(n, _escape_prometheus_label_value(n), _metrics_values_for_model(n)) for n in models]
    lines: list[str] = []

    lines.append("# HELP sglang:token_usage KV cache utilization ratio (0.0-1.0)")
    lines.append("# TYPE sglang:token_usage gauge")
    for _name, m, v in per_model:
        lines.append(f'sglang:token_usage{{model_name="{m}"}} {v["token_usage"]}')

    lines.append("# HELP sglang:num_queue_reqs Number of requests waiting in queue")
    lines.append("# TYPE sglang:num_queue_reqs gauge")
    for _name, m, v in per_model:
        lines.append(f'sglang:num_queue_reqs{{model_name="{m}"}} {v["num_queue_reqs"]}')

    lines.append("# HELP sglang:time_to_first_token_seconds Histogram of time to first token in seconds")
    lines.append("# TYPE sglang:time_to_first_token_seconds histogram")
    for _name, m, v in per_model:
        lines.append(f'sglang:time_to_first_token_seconds_bucket{{le="0.001",model_name="{m}"}} 0')
        lines.append(f'sglang:time_to_first_token_seconds_bucket{{le="0.005",model_name="{m}"}} 0')
        lines.append(
            f'sglang:time_to_first_token_seconds_bucket{{le="0.08",model_name="{m}"}} {v["ttft_b08"]}'
        )
        lines.append(
            f'sglang:time_to_first_token_seconds_bucket{{le="+Inf",model_name="{m}"}} {v["ttft_count"]}'
        )
        lines.append(f'sglang:time_to_first_token_seconds_sum{{model_name="{m}"}} {v["ttft_sum"]}')
        lines.append(f'sglang:time_to_first_token_seconds_count{{model_name="{m}"}} {v["ttft_count"]}')

    lines.append("# HELP sglang:time_per_output_token_seconds Histogram of time per output token in seconds")
    lines.append("# TYPE sglang:time_per_output_token_seconds histogram")
    for _name, m, v in per_model:
        lines.append(f'sglang:time_per_output_token_seconds_bucket{{le="0.001",model_name="{m}"}} 0')
        lines.append(
            f'sglang:time_per_output_token_seconds_bucket{{le="0.005",model_name="{m}"}} {v["tpot_b005"]}'
        )
        lines.append(
            f'sglang:time_per_output_token_seconds_bucket{{le="0.08",model_name="{m}"}} {v["tpot_count"]}'
        )
        lines.append(
            f'sglang:time_per_output_token_seconds_bucket{{le="+Inf",model_name="{m}"}} {v["tpot_count"]}'
        )
        lines.append(f'sglang:time_per_output_token_seconds_sum{{model_name="{m}"}} {v["tpot_sum"]}')
        lines.append(f'sglang:time_per_output_token_seconds_count{{model_name="{m}"}} {v["tpot_count"]}')

    return "\n".join(lines) + "\n"


def _strip_include_usage(path: str, body: bytes, content_type: str) -> tuple[bytes, bool]:
    if path.split("?", 1)[0] != "/v1/chat/completions":
        return body, False
    if "application/json" not in (content_type or "").lower():
        return body, False
    if not body:
        return body, False
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body, False
    if isinstance(payload, dict) and not bool(payload.get("stream", False)):
        had_include_usage = "include_usage" in payload
        payload.pop("include_usage", None)
        return json.dumps(payload, separators=(",", ":")).encode("utf-8"), had_include_usage
    return body, False


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _serve_metrics(self) -> None:
        body = render_sglang_metrics_text().encode("utf-8")
        self.send_response(200, "OK")
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        request_body = self.rfile.read(content_length) if content_length > 0 else b""
        request_body, stripped_include_usage = _strip_include_usage(
            self.path, request_body, self.headers.get("Content-Type", "")
        )

        headers = {k: v for k, v in self.headers.items() if k.lower() != "host"}
        headers["Content-Length"] = str(len(request_body))

        conn = client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=120)
        try:
            conn.request(
                self.command,
                self.path,
                body=request_body if request_body else None,
                headers=headers,
            )
            resp = conn.getresponse()
            data = resp.read()
        finally:
            conn.close()

        self.send_response(resp.status, resp.reason)
        for key, value in resp.getheaders():
            key_lower = key.lower()
            if key_lower in ("transfer-encoding", "connection", "content-length"):
                continue
            self.send_header(key, value)
        self.send_header(
            "X-Dynamo-Proxy-Include-Usage-Stripped",
            "true" if stripped_include_usage else "false",
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if _path_without_query(self.path) == "/metrics":
            self._serve_metrics()
            return
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802
        self._proxy()

    def do_PUT(self) -> None:  # noqa: N802
        self._proxy()

    def do_PATCH(self) -> None:  # noqa: N802
        self._proxy()

    def do_DELETE(self) -> None:  # noqa: N802
        self._proxy()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._proxy()


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    server = ReusableThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    server.serve_forever()
