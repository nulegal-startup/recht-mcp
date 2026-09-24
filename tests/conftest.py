# SPDX-License-Identifier: AGPL-3.0-only
"""An in-memory backend, so the tool layer can be tested without a network."""
from __future__ import annotations

import copy

import pytest

from recht_mcp import grammar
from recht_mcp.backend import BaseBackend, NotFound, Unavailable

BGB = {"id": 1, "slug": "bgb", "jurabk": "BGB", "title": "Bürgerliches Gesetzbuch",
       "status": "in_force", "fundstelle": "RGBl 1896, 195", "land": False}
SAECHS = {"id": 2, "slug": "sn-saechsdsdg", "jurabk": "SächsDSDG",
          "title": "Sächsisches Datenschutzdurchführungsgesetz", "status": "in_force",
          "fundstelle": "GVBl. 2019 S. 1", "land": True}
LWG_FED = {"id": 3, "slug": "lwg", "jurabk": "LwG", "title": "Landwirtschaftsgesetz",
           "status": "in_force", "fundstelle": None, "land": False}
LWG_BY = {"id": 4, "slug": "by-lwg", "jurabk": "LWG", "title": "Landeswahlgesetz",
          "status": "in_force", "fundstelle": None, "land": True}

NORMS = {
    (1, "622"): {"ref": "§ 622", "ref_norm": "622",
                 "heading": "Kündigungsfristen bei Arbeitsverhältnissen"},
    (1, "556g"): {"ref": "§ 556g", "ref_norm": "556g", "heading": "Rechtsfolgen"},
    (1, "288"): {"ref": "§ 288", "ref_norm": "288", "heading": "Verzugszinsen"},
    (2, "13"): {"ref": "§ 13", "ref_norm": "13", "heading": "Videoüberwachung"},
    (3, "1"): {"ref": "§ 1", "ref_norm": "1", "heading": "Zweck"},
}

VERSION = {"valid_from": "2019-06-10", "valid_to": None, "date_precision": "launch",
           "amendment_note": "zuletzt geändert durch Art. 1 G v. 1.1.2019"}

CASE = {"status": "available", "court": "BGH", "decided": "2023-07-12",
        "aktenzeichen": "VIII ZR 125/22", "doknr": "KORE304862023",
        "ecli": "ECLI:DE:BGH:2023:120723UVIIIZR125.22.0",
        "url": "https://recht.nulegal.eu/rechtsprechung/bgh/2023-07-12/viii-zr-125-22",
        "leitsatz": "Leitsatz.", "anchor_basis": "native_numbering",
        "cited_norms": [{"jurabk": "BGB", "ref": "§ 556g", "ref_norm": "556g", "n": 43}],
        "attribution": {"source": "example"}, "source_info": {"name": "test"},
        "paragraphs": [{"rn": i, "text": f"Absatz {i}"} for i in range(1, 61)]}


class FakeBackend(BaseBackend):
    """Implements the whole `Backend` protocol over the fixtures above.

    `calls` records every data access, so tests can assert what the tool layer
    asked for — not only what it answered.
    """

    def __init__(self):
        self.calls: list[tuple] = []
        self.laws = {"bgb": BGB, "sächsdsdg": SAECHS, "sn-saechsdsdg": SAECHS,
                     "lwg-fed": LWG_FED}
        self.resolve_rows: list | None = None
        self.stats_error = False
        self.markdown: str | None = "## § 622 Kündigungsfristen\n\n(1) …"
        self.cases = {"KORE304862023": copy.deepcopy(CASE)}

    # grammar: the shipped conservative one
    def parse_prose_citation(self, text):
        return grammar.parse_prose_citation(text)

    def is_court_head(self, text):
        return grammar.is_court_head(text)

    def docket_key(self, text):
        return grammar.docket_key(text)

    def resolve(self, citations, include):
        self.calls.append(("resolve", list(citations), list(include)))
        if self.resolve_rows is not None:
            return copy.deepcopy(self.resolve_rows)
        return [{"input": c, "status": "resolved", "law": "BGB", "citation_kind": "norm"}
                for c in citations]

    def find_law(self, abbrev):
        self.calls.append(("find_law", abbrev))
        if abbrev == "LWG" or abbrev == "lwg":
            if abbrev == "LWG":
                return LWG_BY, [LWG_FED, LWG_BY]
            return LWG_FED, [LWG_FED, LWG_BY]
        if abbrev == "Luft":
            return None, [LWG_FED, LWG_BY]
        law = self.laws.get(abbrev.lower())
        return (law, [law]) if law else (None, [])

    def is_land(self, law):
        return bool(law.get("land"))

    def law_url_key(self, law):
        return law["jurabk"]

    def find_norm(self, law, ref, as_of):
        self.calls.append(("find_norm", law["id"], ref, as_of))
        norm = NORMS.get((law["id"], ref))
        if norm is None:
            return None, None
        if as_of and as_of < "2019-06-10":
            return dict(norm), None
        return dict(norm), dict(VERSION)

    def norm_payload(self, law, norm, version):
        return {"law": law["jurabk"], "ref": norm["ref"], "text": "(1) …",
                "version": version, "authoritative_source": "Lesefassung."}

    def norm_url(self, law, norm):
        return f"{self.base_url}/gesetze/{law['jurabk']}/{norm['ref_norm']}"

    def norm_markdown(self, law, norm, ref, as_of):
        return self.markdown

    def norm_versions(self, law, norm):
        return [{"first_observed": "2024-01-01", "valid_to": None,
                 "date_precision": "day", "amendment_note": "neu"},
                {"first_observed": "2019-06-10", "valid_to": "2024-01-01",
                 "date_precision": "launch", "amendment_note": None}]

    def search_norms(self, query, *, limit, include_repealed):
        self.calls.append(("search_norms", query, limit, include_repealed))
        if query == "Verzugspauschale":
            return {"results": []}
        return {"results": [
            {"jurabk": "BDSG", "ref": "§ 4", "heading": "Videoüberwachung",
             "url": "u1", "jurisdiction": None},
            {"jurabk": "SächsDSDG", "ref": "§ 13", "heading": "Videoüberwachung",
             "url": "u2", "jurisdiction": "sn", "jurisdiction_label": "Sachsen"},
        ]}

    def search_cases(self, query, *, limit):
        self.calls.append(("search_cases", query, limit))
        return {"results": [{"doknr": "KORE304862023"}], "attribution": "x"}

    def citing_decisions_for_norm(self, abbrev, ref, *, limit, offset, sort):
        self.calls.append(("citing_norm", abbrev, ref, limit, offset, sort))
        if ref == "404":
            raise NotFound("unknown norm: 404 BGB")
        return {"total": 250, "cases": [{"court": "EuGH"}] * limit,
                "ranking": {"sort": sort}}

    def case_key(self, case_id):
        if case_id.startswith("https://"):
            return "KORE304862023"
        return case_id

    def citing_decisions_for_case(self, key, *, limit, offset, sort):
        self.calls.append(("citing_case", key, limit, offset, sort))
        if key not in self.cases:
            return None
        return {"case": {"court": "BGH", "decided": "2023-07-12",
                         "aktenzeichen": "VIII ZR 125/22", "cited_by": 2},
                "served": "weight",
                "rows": [{"court": "LG Berlin", "decided": "2024-01-02",
                          "aktenzeichen": "65 S 1/23", "doknr": "X1", "ecli": None,
                          "leitsatz": "", "rn": 17, "url": "https://e/x1"},
                         {"court": "AG Mitte", "decided": "2024-02-03",
                          "aktenzeichen": "1 C 1/23", "doknr": "X2", "ecli": None,
                          "leitsatz": "L", "rn": None, "url": "https://e/x2"}]}

    def get_case(self, key, *, paragraphs):
        self.calls.append(("get_case", key, paragraphs))
        if key not in self.cases:
            raise NotFound(f"unknown decision: {key}")
        return copy.deepcopy(self.cases[key])

    def cited_decisions(self, doknr):
        return [{"court": "BGH", "decided": "2020-01-01", "aktenzeichen": "VIII ZR 1/19",
                 "doknr": "D1", "ecli": None, "n": 3, "url": "https://e/d1"}]

    def changes(self, *, since, limit, days):
        self.calls.append(("changes", since, limit, days))
        return [{"law": "BGB", "ref": "§ 622", "heading": "K", "law_title": "BGB",
                 "jurisdiction": "bund", "observed": "2026-09-01",
                 "first_seen_at": "2026-09-01 12:34:56", "amendment_note": None,
                 "law_url_key": "FreizügG/EU 2004", "ref_norm": "622", "version_id": 77}]

    def stats(self):
        if self.stats_error:
            raise Unavailable("database down")
        return {"stand": "2026-09-24", "gesetze": 1, "quellen": [{"label": "x"}]}


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def call(backend):
    """Call one tool through the full JSON-RPC path; return (isError, payload)."""
    from recht_mcp.protocol import handle_message

    def _call(name, args=None, be=None):
        r = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": args or {}}},
                           be or backend)
        res = r["result"]
        return res["isError"], res["structuredContent"]
    return _call
