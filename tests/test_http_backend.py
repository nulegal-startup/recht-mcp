# SPDX-License-Identifier: AGPL-3.0-only
"""HTTPBackend against a local stand-in for the REST API: URL building and
the error mapping (a miss is NotFound, an outage is Unavailable — never the
other way round)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from recht_mcp.backend import NotFound, Unavailable
from recht_mcp.http_backend import HTTPBackend
from recht_mcp.protocol import handle_message

BGB_META = {"slug": "bgb", "jurabk": "BGB", "title": "Bürgerliches Gesetzbuch",
            "status": "in_force", "fundstelle": "RGBl 1896, 195",
            "binding_gazette": {"jurisdiction": "BUND"},
            "source": {"url": "https://recht.nulegal.eu/gesetze/BGB"}}
NORM = {"law": "BGB", "ref": "§ 622", "heading": "Kündigungsfristen",
        "version": {"valid_from": "2019-06-10", "valid_to": None, "date_precision": "week",
                    "amendment_note": None},
        "authoritative_source": "Lesefassung.",
        "source": {"url": "https://recht.nulegal.eu/gesetze/BGB/622"},
        "version_coverage": {"archive_starts": "2019-06-10"}}


class API(BaseHTTPRequestHandler):
    seen: list = []
    status_override: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        API.seen.append((u.path, q))
        if u.path in API.status_override:
            return self._send(API.status_override[u.path], {"detail": "boom"})
        routes = {
            "/v1/law/BGB": (200, BGB_META),
            "/v1/law/bgb": (200, BGB_META),
            "/v1/law/LWG": (300, {"detail": {"error": "ambiguous", "candidates": [
                {**BGB_META, "slug": "lwg", "jurabk": "LwG"},
                {**BGB_META, "slug": "by-lwg", "jurabk": "LWG",
                 "binding_gazette": {"jurisdiction": "BY"}}]}}),
            "/v1/norm/bgb/622": (200, NORM),
            "/v1/norm/bgb/622/versions": (200, {"versions": [
                {"first_observed": "2019-06-10", "valid_to": None,
                 "date_precision": "launch", "amendment_note": None}]}),
            "/v1/stats": (200, {"stand": "2026-09-24"}),
            "/v1/case-key": (200, {"key": "KORE1"}),
        }
        if u.path == "/v1/norm/bgb/288" and q.get("asof"):
            return self._send(404, {"detail": {"error": "no_version_asof"}})
        if u.path == "/v1/norm/bgb/288":
            return self._send(200, {**NORM, "ref": "§ 288",
                                    "source": {"url": "https://recht.nulegal.eu/gesetze/BGB/288"}})
        if u.path == "/gesetze/BGB/622/download.md":
            return self._send(200, "## § 622\n", "text/markdown")
        if u.path in routes:
            return self._send(*routes[u.path])
        return self._send(404, {"detail": f"unknown: {u.path}"})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        API.seen.append((self.path, body))
        self._send(200, {"results": [{"input": c, "status": "resolved", "law": "BGB",
                                      "citation_kind": "norm"} for c in body["citations"]]})


@pytest.fixture
def api():
    API.seen, API.status_override = [], {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), API)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield HTTPBackend(f"http://127.0.0.1:{srv.server_port}")
    srv.shutdown()
    srv.server_close()


def _call(be, name, args):
    r = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": args}}, be)
    return r["result"]["isError"], r["result"]["structuredContent"]


def test_find_law_maps_meta_to_a_law_record_and_caches(api):
    law, laws = api.find_law("BGB")
    assert law["slug"] == "bgb" and law["fundstelle"] == "RGBl 1896, 195"
    assert api.law_url_key(law) == "BGB" and not api.is_land(law)
    api.find_law("BGB")
    assert [s for s in API.seen if s[0] == "/v1/law/BGB"] == [("/v1/law/BGB", {})]


def test_an_ambiguous_law_lists_candidates(api):
    law, cands = api.find_law("LWG")
    assert law is None and [c["slug"] for c in cands] == ["lwg", "by-lwg"]
    assert api.is_land(cands[1])


def test_get_norm_end_to_end(api):
    is_error, out = _call(api, "getNorm", {"law": "BGB", "ref": "622"})
    assert not is_error
    assert out["markdown"] == "## § 622\n"
    assert out["url"] == "https://recht.nulegal.eu/gesetze/BGB/622"
    assert out["version"]["first_observed"] == "2019-06-10"


def test_get_norm_json_drops_the_rest_routes_own_coverage_block(api):
    _, out = _call(api, "getNorm", {"law": "BGB", "ref": "622", "format": "json"})
    assert "version_coverage" not in out["norm"]
    assert out["version_coverage"]["archive_starts"] == "2019-06-10"


def test_an_asof_gap_is_outside_coverage(api):
    is_error, out = _call(api, "getNorm", {"law": "BGB", "ref": "288", "as_of": "2014-08-01"})
    assert out["reason"] == "outside_coverage"
    assert "§ 288 BGB" in out["detail"]


def test_versions_use_the_versions_route(api):
    _, out = _call(api, "listNormVersions", {"law": "BGB", "ref": "622"})
    assert out["versions"][0]["at_archive_floor"] is True


def test_resolve_posts_the_batch(api):
    _, out = _call(api, "resolveIdentifiers", {"citations": ["§ 622 BGB"]})
    assert out["results"][0]["fundstelle"] == "RGBl 1896, 195"
    assert ("/v1/resolve", {"citations": ["§ 622 BGB"], "include": []}) in API.seen


def test_a_case_url_is_read_back_to_its_key(api):
    assert api.case_key("https://recht.nulegal.eu/rechtsprechung/bgh/2023-07-12/x") == "KORE1"
    assert api.case_key("KORE9") == "KORE9"


def test_a_404_on_a_case_is_not_found(api):
    with pytest.raises(NotFound):
        api.get_case("NOPE", paragraphs=False)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_an_overloaded_or_failing_api_is_unavailable_never_a_miss(api, status):
    API.status_override["/v1/case"] = status
    with pytest.raises(Unavailable):
        api.get_case("KORE1", paragraphs=False)
    is_error, out = _call(api, "listCasePassages", {"case": "KORE1"})
    assert out["reason"] == "unavailable"


def test_a_stats_outage_is_reported_as_an_outage(api):
    API.status_override["/v1/stats"] = 503
    _, out = _call(api, "getCoverage", {})
    assert out["reason"] == "unavailable"


def test_an_unreachable_api_is_unavailable():
    be = HTTPBackend("http://127.0.0.1:9", timeout=2)
    _, out = _call(be, "getCoverage", {})
    assert out["reason"] == "unavailable"
