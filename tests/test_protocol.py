# SPDX-License-Identifier: AGPL-3.0-only
import json

import pytest

from recht_mcp import protocol
from recht_mcp.protocol import handle_message, http_exchange
from recht_mcp.tools import REASONS, TOOLS, tool_list

EXPECTED_TOOLS = ["resolveIdentifiers", "search", "getNorm", "listNormVersions",
                  "listCitingDecisions", "listCitedAuthorities", "listCasePassages",
                  "getChanges", "getCoverage"]


def rpc(method, params=None, mid=1):
    msg = {"jsonrpc": "2.0", "method": method}
    if mid is not None:
        msg["id"] = mid
    if params is not None:
        msg["params"] = params
    return msg


def test_initialize_answers_the_handshake(backend):
    r = handle_message(rpc("initialize", {"protocolVersion": "2025-11-25"}), backend)
    res = r["result"]
    assert res["protocolVersion"] == "2025-11-25"
    assert res["capabilities"] == {"tools": {"listChanged": False}}
    assert res["serverInfo"]["name"] == "nulegal-recht"
    assert res["serverInfo"]["websiteUrl"] == "https://recht.nulegal.eu/developers"
    assert "first_observed" in res["instructions"]


def test_server_info_carries_the_icons_and_the_website(backend):
    info = handle_message(rpc("initialize", {}), backend)["result"]["serverInfo"]
    assert info["title"] == protocol.SERVER_TITLE
    assert info["version"] == protocol.SERVER_VERSION
    assert info["description"] == protocol.SERVER_DESCRIPTION
    icons = info["icons"]
    assert all(i["src"].startswith("https://recht.nulegal.eu/static/") for i in icons)
    assert {i["mimeType"] for i in icons} == {"image/svg+xml", "image/png"}
    assert {s for i in icons for s in i["sizes"]} >= {"any", "512x512"}


@pytest.mark.parametrize("asked", ["2025-06-18", "2025-03-26", "2024-11-05"])
def test_an_older_client_gets_only_the_identity_its_revision_defines(backend, asked):
    info = handle_message(rpc("initialize", {"protocolVersion": asked}),
                          backend)["result"]["serverInfo"]
    assert {"icons", "description"}.isdisjoint(info)
    assert info["websiteUrl"] == "https://recht.nulegal.eu/developers"


@pytest.mark.parametrize("method,key", [("resources/list", "resources"),
                                        ("resources/templates/list", "resourceTemplates"),
                                        ("prompts/list", "prompts")])
def test_the_list_methods_we_offer_nothing_on_answer_empty_not_missing(backend, method, key):
    assert handle_message(rpc(method), backend) == {"jsonrpc": "2.0", "id": 1,
                                                    "result": {key: []}}


@pytest.mark.parametrize("params", [["x"], "x", 5, True])  # [] is falsy: read as {}
def test_params_that_are_not_an_object_are_invalid_params(backend, params):
    assert handle_message(rpc("tools/list", params), backend)["error"]["code"] == -32602


def test_every_tool_has_a_short_distinct_title():
    titles = [t["title"] for t in tool_list()]
    assert len(set(titles)) == 9 and all(len(t) <= 25 for t in titles)


def test_server_json_matches_what_the_server_says(backend):
    import pathlib
    doc = json.loads((pathlib.Path(__file__).parents[1] / "server.json").read_text())
    assert doc["version"] == protocol.SERVER_VERSION
    assert doc["title"] == protocol.SERVER_TITLE
    assert doc["description"] == protocol.SERVER_DESCRIPTION
    assert doc["icons"] == protocol.icons("https://recht.nulegal.eu")


@pytest.mark.parametrize("asked,expect", [("2025-03-26", "2025-03-26"),
                                          ("2024-11-05", "2024-11-05"),
                                          ("1999-01-01", protocol.PROTOCOL_VERSION),
                                          (None, protocol.PROTOCOL_VERSION)])
def test_protocol_version_is_negotiated_not_asserted(backend, asked, expect):
    r = handle_message(rpc("initialize", {"protocolVersion": asked}), backend)
    assert r["result"]["protocolVersion"] == expect


def test_ping_and_notifications(backend):
    assert handle_message(rpc("ping"), backend) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert handle_message(rpc("notifications/initialized", mid=None), backend) is None


@pytest.mark.parametrize("msg,code", [([], -32600), ({"jsonrpc": "1.0", "id": 1}, -32600),
                                      ({"jsonrpc": "2.0", "id": 1}, -32600)])
def test_a_malformed_envelope_is_a_json_rpc_error(backend, msg, code):
    assert handle_message(msg, backend)["error"]["code"] == code


def test_unknown_method_and_unknown_tool(backend):
    assert handle_message(rpc("nope"), backend)["error"]["code"] == -32601
    r = handle_message(rpc("tools/call", {"name": "nope"}), backend)
    assert r["error"]["code"] == -32602
    assert r["error"]["data"]["available"] == sorted(EXPECTED_TOOLS)
    r = handle_message(rpc("tools/call", {"name": 5}), backend)
    assert r["error"]["code"] == -32602


def test_tools_list_publishes_nine_read_only_tools(backend):
    tools = handle_message(rpc("tools/list"), backend)["result"]["tools"]
    assert [t["name"] for t in tools] == EXPECTED_TOOLS
    for t in tools:
        assert t["title"] and len(t["description"]) > 200
        assert t["inputSchema"]["type"] == "object"
        assert t["annotations"] == {"title": t["title"], "readOnlyHint": True,
                                    "destructiveHint": False, "idempotentHint": True,
                                    "openWorldHint": False}
        assert t["_meta"]["nulegal/costClass"] in ("cheap", "medium", "expensive")
        assert "handler" not in t


def test_every_required_argument_is_a_declared_property():
    for t in TOOLS:
        props = t["schema"].get("properties", {})
        for req in t["schema"].get("required", []):
            assert req in props, (t["name"], req)


def test_a_tool_error_is_a_result_not_a_protocol_error(call):
    is_error, payload = call("getNorm", {"law": "", "ref": ""})
    assert is_error is True
    assert payload["reason"] == "invalid_argument"
    assert payload["reason"] in REASONS


def test_an_unexpected_fault_is_reported_as_unknown_never_as_an_absence(call, backend):
    def boom(*a, **k):
        raise RuntimeError("socket closed")
    backend.stats = boom
    is_error, payload = call("getCoverage")
    assert is_error and payload["reason"] == "unavailable"
    assert "not negative" in payload["detail"]


def test_the_result_carries_text_and_structured_content_that_agree(backend):
    r = handle_message(rpc("tools/call", {"name": "getCoverage", "arguments": {}}), backend)
    res = r["result"]
    assert json.loads(res["content"][0]["text"]) == res["structuredContent"]


def test_dates_are_encoded_before_they_reach_the_envelope(backend):
    import datetime
    backend.stats = lambda: {"stand": datetime.date(2026, 9, 24),
                             "erstellt": datetime.datetime(2026, 9, 24, 10, 30)}
    r = handle_message(rpc("tools/call", {"name": "getCoverage", "arguments": {}}), backend)
    s = r["result"]["structuredContent"]
    assert s["as_of"] == "2026-09-24" and s["built_at"] == "2026-09-24T10:30:00"


# ------------------------------------------------------------ http_exchange

def _handle(backend):
    return lambda m: handle_message(m, backend)


def test_http_single_message(backend):
    status, body, headers = http_exchange(json.dumps(rpc("ping")).encode(), None,
                                          _handle(backend))
    assert status == 200 and body["result"] == {}
    assert headers == {"Cache-Control": "no-store",
                       "MCP-Protocol-Version": protocol.PROTOCOL_VERSION}


def test_http_echoes_a_supported_protocol_header(backend):
    _, _, headers = http_exchange(json.dumps(rpc("ping")).encode(), "2025-03-26",
                                  _handle(backend))
    assert headers["MCP-Protocol-Version"] == "2025-03-26"


def test_http_notification_and_all_notification_batch_are_202(backend):
    note = rpc("notifications/initialized", mid=None)
    assert http_exchange(json.dumps(note).encode(), None, _handle(backend))[:2] == (202, None)
    assert http_exchange(json.dumps([note, note]).encode(), None,
                         _handle(backend))[:2] == (202, None)


def test_http_batch_drops_notifications(backend):
    batch = [rpc("ping", mid=1), rpc("notifications/x", mid=None), rpc("ping", mid=2)]
    status, body, _ = http_exchange(json.dumps(batch).encode(), None, _handle(backend))
    assert status == 200 and [m["id"] for m in body] == [1, 2]


@pytest.mark.parametrize("raw,status,code", [(b"{not json", 400, -32700),
                                             (b"[]", 400, -32600),
                                             (b"x" * 1_000_001, 413, -32600)])
def test_http_body_faults(backend, raw, status, code):
    st, body, headers = http_exchange(raw, None, _handle(backend))
    assert st == status and body["error"]["code"] == code
    if status in (413,) or code == -32700:
        assert headers == {"Cache-Control": "no-store"}
