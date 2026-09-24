# SPDX-License-Identifier: AGPL-3.0-only
"""JSON-RPC 2.0 and the Model Context Protocol, independent of any transport.

`handle_message` turns one JSON-RPC message into one response (or None for a
notification). `http_exchange` wraps it for a Streamable-HTTP POST body:
size limit, parse errors, batches, and the status code each outcome gets. Both
the stdio server and the HTTP server in `recht_mcp.transports` — and the hosted
endpoint — go through these two functions.

TRANSPORT SHAPE. Streamable HTTP, stateless, JSON responses. One `POST`
carries one JSON-RPC message (or a batch) and gets the response in the same
HTTP response; there is no session id, no server-initiated stream and
therefore no SSE. That is a legal Streamable-HTTP server — the specification
makes the GET/SSE leg optional and prescribes 405 when it is not offered — and
it fits a server whose every operation is a read-only lookup that completes in
one response.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from .backend import Backend
from .tools import INSTRUCTIONS, ToolError, _BY_NAME, tool_list
from .tools import call_tool as _call_tool

__all__ = ["PROTOCOL_VERSION", "SUPPORTED_PROTOCOLS", "SERVER_NAME", "SERVER_TITLE",
           "SERVER_VERSION", "SERVER_DESCRIPTION", "MAX_BODY", "handle_message", "http_exchange",
           "negotiated_version", "server_info", "icons", "tool_result"]

log = logging.getLogger("recht_mcp")

#: What we speak. The newest is what an `initialize` without a recognised version
#: is answered with; a client naming one of the others is answered in ITS
#: version, which is what the specification's negotiation asks for.
PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

#: The revision that added `icons` and `description` (and `websiteUrl`) to
#: `Implementation`. `serverInfo` carries the first two only when this or a
#: later revision was negotiated: an older client's schema has no such members,
#: and the handshake answers in the version the client asked for. `websiteUrl`
#: went out to every revision before this one existed, and an unknown member is
#: legal in every revision's schema, so it stays on for all of them.
_IDENTITY_SINCE = "2025-11-25"

SERVER_NAME = "nulegal-recht"
SERVER_TITLE = "nu:legal Deutsches Recht"
#: Bumped when the TOOL CONTRACT or the registry entry changes, not when the
#: corpus does. Kept equal to `version` in server.json.
SERVER_VERSION = "1.0.1"
SERVER_DESCRIPTION = ("German federal and Land statutes plus court decisions for "
                      "agents. Keyless, read-only, CC BY 4.0.")

#: Largest request body accepted over HTTP, in bytes.
MAX_BODY = 1_000_000

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: An MCP response is a JSON-RPC envelope keyed to one request id; it must never
#: be served to the next caller out of a shared cache.
NO_STORE = {"Cache-Control": "no-store"}


def _err(mid, code: int, message: str, data=None) -> dict:
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": mid, "error": e}


def _ok(mid, result) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def icons(base_url: str) -> list[dict]:
    """The nu:legal owl, served from the site's own /static (same origin as the
    hosted endpoint, which is what the specification asks a client to check
    before it loads an icon). The same three URLs are in server.json."""
    return [
        {"src": f"{base_url}/static/nulegal-icon.svg", "mimeType": "image/svg+xml",
         "sizes": ["any"]},
        {"src": f"{base_url}/static/nulegal-icon-512.png", "mimeType": "image/png",
         "sizes": ["512x512"]},
        {"src": f"{base_url}/static/nulegal-icon-256.png", "mimeType": "image/png",
         "sizes": ["256x256"]},
    ]


def server_info(version: str, backend: Backend) -> dict:
    """`serverInfo` for the negotiated protocol revision (see `_IDENTITY_SINCE`)."""
    info = {"name": SERVER_NAME, "title": SERVER_TITLE, "version": SERVER_VERSION,
            "websiteUrl": f"{backend.base_url}/developers"}
    if version >= _IDENTITY_SINCE:  # ISO dates: string order is date order
        info.update(description=SERVER_DESCRIPTION, icons=icons(backend.base_url))
    return info


def tool_result(payload: dict, backend: Backend, is_error: bool = False) -> dict:
    """One tool answer, in both shapes MCP defines.

    `content` is what every client can render and what a model reads; the same
    object rides as `structuredContent` for clients that parse it. No
    `outputSchema` is declared: these payloads carry honesty fields that appear
    only when they apply (`date_mismatch`, `coverage`, `pagination`), and a
    schema tight enough to be worth validating would have to forbid exactly
    those.

    `backend.jsonable` runs FIRST, at the one seam every tool answer passes
    through, so no tool has to remember to stringify a date a backend returned
    as a date object.
    """
    payload = backend.jsonable(payload)
    return {"content": [{"type": "text",
                         "text": json.dumps(payload, ensure_ascii=False,
                                            indent=2, default=str)}],
            "structuredContent": payload,
            "isError": is_error}


def handle_message(msg: Any, backend: Backend, *,
                   logger: logging.Logger | None = None,
                   call_tool: Callable[[str, dict, Backend], dict] | None = None
                   ) -> dict | None:
    """One JSON-RPC message in, one response out — or None for a notification.

    `logger` receives the traceback of a tool that failed unexpectedly;
    `call_tool` replaces `recht_mcp.tools.call_tool`, for a host that wraps
    every tool call (metrics, tracing)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(None, INVALID_REQUEST, "expected a JSON-RPC 2.0 message object")
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}
    if not isinstance(method, str):
        return _err(mid, INVALID_REQUEST, "missing method")
    if mid is None:  # a notification: acknowledged, never answered
        return None
    if not isinstance(params, dict):  # MCP params are always an object
        return _err(mid, INVALID_PARAMS, "params must be an object")

    if method == "initialize":
        want = params.get("protocolVersion")
        version = want if want in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
        return _ok(mid, {
            "protocolVersion": version,
            # Tools only. `resources` and `prompts` mean "the server offers
            # some" in the specification, and this one offers none, so they are
            # not declared — the three list methods below answer empty for the
            # clients and directory scanners that call them anyway.
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": server_info(version, backend),
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": tool_list()})
    # Empty, not -32601: several clients and every directory scanner list all
    # three regardless of the declared capabilities, and "none" is the true
    # answer where "no such method" reads as a broken server.
    if method == "resources/list":
        return _ok(mid, {"resources": []})
    if method == "resources/templates/list":
        return _ok(mid, {"resourceTemplates": []})
    if method == "prompts/list":
        return _ok(mid, {"prompts": []})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            return _err(mid, INVALID_PARAMS, "params.name must be a tool name")
        try:
            payload = (call_tool or _call_tool)(name, params.get("arguments") or {},
                                                backend)
        except KeyError:
            return _err(mid, INVALID_PARAMS, f"unknown tool: {name}",
                        {"available": sorted(_BY_NAME)})
        except ToolError as te:
            # A tool that ran and could not answer is a RESULT, not a protocol
            # error — the model has to see the reason to act on it.
            return _ok(mid, tool_result(te.payload(), backend, is_error=True))
        except Exception:  # noqa: BLE001
            (logger or log).exception("mcp tool %s failed", name)
            return _ok(mid, tool_result(
                {"error": True, "reason": "unavailable",
                 "detail": "This lookup failed on our side. The answer is "
                           "unknown, not negative — do not report an absence."},
                backend, is_error=True))
        return _ok(mid, tool_result(payload, backend))
    return _err(mid, METHOD_NOT_FOUND, f"unknown method: {method}")


def negotiated_version(header: str | None) -> str:
    """The `MCP-Protocol-Version` a response echoes for the request header."""
    return header if header in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION


def http_exchange(raw: bytes, protocol_header: str | None,
                  handle: Callable[[Any], dict | None]
                  ) -> tuple[int, Any, dict[str, str]]:
    """One Streamable-HTTP POST: `(status, json_body_or_None, headers)`.

    `handle` answers one message (normally `handle_message` bound to a
    backend). A None body means an empty response — 202, the acknowledgement
    for notifications. Body-level faults (too large, not JSON) are answered
    before any version is negotiated, so they carry only the cache header.
    """
    if len(raw) > MAX_BODY:
        return 413, _err(None, INVALID_REQUEST, "request body too large"), dict(NO_STORE)
    try:
        msg = json.loads(raw or b"")
    except Exception:  # noqa: BLE001
        return 400, _err(None, PARSE_ERROR, "invalid JSON"), dict(NO_STORE)

    headers = dict(NO_STORE)
    # Echo the negotiated version, per the Streamable HTTP transport.
    headers["MCP-Protocol-Version"] = negotiated_version(protocol_header)

    if isinstance(msg, list):
        if not msg:
            return 400, _err(None, INVALID_REQUEST, "empty batch"), headers
        out = []
        for one in msg:
            r = handle(one)
            if r is not None:
                out.append(r)
        if not out:
            return 202, None, headers
        return 200, out, headers

    resp = handle(msg)
    if resp is None:  # notification
        return 202, None, headers
    return 200, resp, headers
