# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in: every tool against the public API at recht.nulegal.eu.

    RECHT_MCP_LIVE=1 uv run pytest -q tests/test_live.py

Also compares the local server with the hosted endpoint for the calls whose
answers do not depend on the backend's own grammar, so a drift between the two
shows up here first.
"""
import json
import os
import urllib.request

import pytest

from recht_mcp.http_backend import USER_AGENT, HTTPBackend
from recht_mcp.protocol import handle_message

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("RECHT_MCP_LIVE") != "1",
                                 reason="set RECHT_MCP_LIVE=1 to call the public API")]

BASE = os.environ.get("RECHT_MCP_BASE_URL", "https://recht.nulegal.eu")
CASE = f"{BASE}/rechtsprechung/bgh/2023-07-12/viii-zr-125-22"

CALLS = [
    ("resolveIdentifiers", {"citations": ["§ 622 BGB", "BGH VIII ZR 125/22"]}),
    ("search", {"q": "Kündigungsfrist Arbeitsverhältnis", "limit": 3}),
    ("getNorm", {"law": "BGB", "ref": "622"}),
    ("listNormVersions", {"law": "BGB", "ref": "622"}),
    ("listCitingDecisions", {"law": "BGB", "ref": "626", "limit": 3}),
    ("listCitedAuthorities", {"case": CASE}),
    ("listCasePassages", {"case": CASE, "limit": 3}),
    ("getChanges", {"limit": 3}),
    ("getCoverage", {}),
]

#: Tools that read a `/v1` route added for this server; skipped with a clear
#: message until the route answers on the target API.
NEEDS = {"listNormVersions": "/v1/norm/BGB/622/versions",
         "listCitedAuthorities": "/v1/case-key?case=" + CASE,
         "listCasePassages": "/v1/case-key?case=" + CASE,
         "getChanges": "/v1/changes?limit=1"}


def _route_exists(path: str) -> bool:
    try:
        req = urllib.request.Request(BASE + path, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def _local(name, args):
    r = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": args}}, HTTPBackend(BASE))
    return r["result"]


def _hosted(name, args):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": args}}).encode()
    req = urllib.request.Request(f"{BASE}/v1/mcp", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": USER_AGENT,
                                          "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["result"]


@pytest.mark.parametrize("name,args", CALLS, ids=[c[0] for c in CALLS])
def test_every_tool_answers_live(name, args):
    if name in NEEDS and not _route_exists(NEEDS[name]):
        pytest.skip(f"{NEEDS[name]} is not served by {BASE} yet")
    res = _local(name, args)
    payload = res["structuredContent"]
    assert res["isError"] is False, payload
    if name == "getNorm":
        assert payload["markdown"]
    if name == "resolveIdentifiers":
        assert any(r.get("status") == "resolved" for r in payload["results"])


@pytest.mark.parametrize("name,args", [
    ("getNorm", {"law": "BGB", "ref": "622"}),
    ("getNorm", {"law": "BGB", "ref": "288", "as_of": "2014-08-01"}),
    ("listCitingDecisions", {"law": "BGB", "ref": "626", "limit": 3}),
    ("listCasePassages", {"case": "KORE304862023", "limit": 3}),
    ("getCoverage", {}),
])
def test_local_and_hosted_agree(name, args):
    local, hosted = _local(name, args), _hosted(name, args)
    assert local["isError"] == hosted["isError"]
    a, b = local["structuredContent"], hosted["structuredContent"]
    if name == "getCoverage":
        for k in ("as_of", "built_at", "totals", "sources", "years", "known_missing"):
            a.pop(k, None), b.pop(k, None)
    assert a == b
