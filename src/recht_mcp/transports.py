# SPDX-License-Identifier: AGPL-3.0-only
"""The two local transports: stdio and Streamable HTTP.

Both are thin: they move bytes, and `recht_mcp.protocol` does the rest.

stdio — one JSON-RPC message (or batch) per line on stdin, one response per
line on stdout. Logs go to stderr, never stdout.

Streamable HTTP — `POST {path}` with a JSON-RPC body, answered with
`application/json` in the same response. Stateless: no session id, no
server-initiated stream, so `GET` and `DELETE` on the endpoint answer 405. The
server binds to 127.0.0.1 by default and refuses a browser request whose
`Origin` is not a loopback address, which is what the MCP specification asks
of a local server to prevent DNS-rebinding attacks.
"""
from __future__ import annotations

import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import IO
from urllib.parse import urlsplit

from .backend import Backend
from .protocol import (INVALID_REQUEST, MAX_BODY, NO_STORE, PARSE_ERROR, _err,
                       handle_message, http_exchange)

__all__ = ["serve_stdio", "serve_http", "make_http_server", "encode_json"]

log = logging.getLogger("recht_mcp")

_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}


def encode_json(body) -> bytes:
    """Compact UTF-8 JSON, the same bytes the hosted endpoint sends."""
    return json.dumps(body, ensure_ascii=False, allow_nan=False, indent=None,
                      separators=(",", ":")).encode("utf-8")


# --------------------------------------------------------------------- stdio

def _stdio_line(line: str, backend: Backend):
    try:
        msg = json.loads(line)
    except ValueError:
        return _err(None, PARSE_ERROR, "invalid JSON")
    if isinstance(msg, list):
        if not msg:
            return _err(None, INVALID_REQUEST, "empty batch")
        out = [r for r in (handle_message(m, backend) for m in msg) if r is not None]
        return out or None
    return handle_message(msg, backend)


def serve_stdio(backend: Backend, stdin: IO[str] | None = None,
                stdout: IO[str] | None = None) -> None:
    """Serve until stdin closes."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        resp = _stdio_line(line, backend)
        if resp is None:
            continue
        stdout.write(json.dumps(resp, ensure_ascii=False, separators=(",", ":")) + "\n")
        stdout.flush()


# ---------------------------------------------------------------------- HTTP

def _origin_allowed(origin: str | None) -> bool:
    if not origin:
        return True  # not a browser request
    host = urlsplit(origin).hostname
    return host is not None and host in _LOOPBACK


def make_http_server(backend: Backend, host: str = "127.0.0.1", port: int = 8765,
                     path: str = "/mcp") -> ThreadingHTTPServer:
    """A ready-to-serve HTTP server; call `serve_forever()` on it."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "recht-mcp"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: A003 — route to logging, not stderr
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, body, headers: dict[str, str]) -> None:
            data = b"" if body is None else encode_json(body)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _guard(self) -> bool:
            if urlsplit(self.path).path != path:
                self._send(404, {"detail": "Not Found"}, dict(NO_STORE))
                return False
            if not _origin_allowed(self.headers.get("Origin")):
                self._send(403, {"detail": "Origin not allowed"}, dict(NO_STORE))
                return False
            return True

        def do_POST(self):  # noqa: N802
            if not self._guard():
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self._send(413, _err(None, INVALID_REQUEST, "request body too large"),
                           dict(NO_STORE))
                self.close_connection = True
                return
            raw = self.rfile.read(length) if length else b""
            status, body, headers = http_exchange(
                raw, self.headers.get("MCP-Protocol-Version"),
                lambda m: handle_message(m, backend))
            self._send(status, body, headers)

        def _not_allowed(self, detail: str) -> None:
            self._send(405, {"error": "method_not_allowed", "detail": detail},
                       {**NO_STORE, "Allow": "POST"})

        def do_GET(self):  # noqa: N802
            if self._guard():
                self._not_allowed(f"This MCP server is stateless: POST a JSON-RPC "
                                  f"message to {path}. There is no server-initiated "
                                  f"SSE stream.")

        def do_DELETE(self):  # noqa: N802
            if self._guard():
                self._not_allowed("Stateless server: no session id is issued and "
                                  "none can be terminated.")

    return ThreadingHTTPServer((host, port), Handler)


def serve_http(backend: Backend, host: str = "127.0.0.1", port: int = 8765,
               path: str = "/mcp") -> None:
    server = make_http_server(backend, host, port, path)
    log.warning("recht-mcp listening on http://%s:%d%s", host, server.server_port, path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
