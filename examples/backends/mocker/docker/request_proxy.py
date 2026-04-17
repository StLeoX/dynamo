#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Lightweight reverse proxy for mocker image compatibility tweaks.

Current compatibility behavior:
- For POST /v1/chat/completions requests where JSON body contains `stream: false`,
  remove top-level `include_usage` before forwarding to dynamo.frontend.
"""

from __future__ import annotations

import json
import os
from http import client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


LISTEN_HOST = os.environ.get("PROXY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("PROXY_PORT", "8000"))
UPSTREAM_HOST = os.environ.get("UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("UPSTREAM_PORT", "18000"))


def _strip_include_usage(path: str, body: bytes, content_type: str) -> bytes:
    if path.split("?", 1)[0] != "/v1/chat/completions":
        return body
    if "application/json" not in (content_type or "").lower():
        return body
    if not body:
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if isinstance(payload, dict) and payload.get("stream") is False:
        payload.pop("include_usage", None)
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return body


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _proxy(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        request_body = self.rfile.read(content_length) if content_length > 0 else b""
        request_body = _strip_include_usage(
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
            if key_lower in ("transfer-encoding", "connection"):
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
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
