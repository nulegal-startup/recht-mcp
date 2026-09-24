# SPDX-License-Identifier: AGPL-3.0-only
import io
import json
import threading
import urllib.error
import urllib.request

import pytest

from recht_mcp.transports import make_http_server, serve_stdio


def test_stdio_answers_one_line_per_request_and_nothing_for_notifications(backend):
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
             "",
             "{broken",
             json.dumps([{"jsonrpc": "2.0", "id": 2, "method": "ping"},
                         {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}])]
    out = io.StringIO()
    serve_stdio(backend, io.StringIO("\n".join(lines) + "\n"), out)
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert replies[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert replies[1]["error"]["code"] == -32700
    assert [m["id"] for m in replies[2]] == [2, 3]
    assert len(replies) == 3


def test_stdio_output_is_one_json_document_per_line(backend):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "getNorm", "arguments": {"law": "BGB", "ref": "622"}}}
    out = io.StringIO()
    serve_stdio(backend, io.StringIO(json.dumps(msg) + "\n"), out)
    lines = out.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["result"]["isError"] is False


@pytest.fixture
def server(backend):
    srv = make_http_server(backend, "127.0.0.1", 0, "/mcp")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def _req(url, method="POST", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_http_post_round_trip(server):
    status, headers, raw = _req(server + "/mcp",
                                body={"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert headers["MCP-Protocol-Version"] == "2025-11-25"
    assert len(json.loads(raw)["result"]["tools"]) == 9


def test_http_notification_is_202_with_empty_body(server):
    status, _, raw = _req(server + "/mcp",
                          body={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202 and raw == b""


def test_http_get_and_delete_are_405(server):
    for method in ("GET", "DELETE"):
        status, headers, _ = _req(server + "/mcp", method=method)
        assert status == 405 and headers["Allow"] == "POST"


def test_http_other_paths_are_404(server):
    assert _req(server + "/other", body={})[0] == 404


def test_http_refuses_a_foreign_origin(server):
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert _req(server + "/mcp", body=ping, headers={"Origin": "https://evil.example"})[0] == 403
    assert _req(server + "/mcp", body=ping, headers={"Origin": "http://localhost:3000"})[0] == 200
