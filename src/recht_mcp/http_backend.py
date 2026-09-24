# SPDX-License-Identifier: AGPL-3.0-only
"""A backend that reads the public REST API at https://recht.nulegal.eu/v1.

Standard library only (`urllib`), so the whole server installs with no
third-party dependency. Every method maps to one documented `/v1` endpoint —
see docs/DESIGN.md for the table — and the tool layer on top of it is the one
the hosted endpoint runs.

Error mapping, in one place:

  * 404 and other 4xx answers on a lookup become `NotFound` (or an empty
    result) — the object is not held;
  * 429 and 5xx answers, and network failures, become `Unavailable` — the
    answer is unknown, and the tool layer reports it as such, never as an
    absence.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import __version__, grammar
from .backend import BaseBackend, NotFound, Unavailable
from .tools import CASE_URL

__all__ = ["HTTPBackend", "DEFAULT_BASE_URL"]

DEFAULT_BASE_URL = "https://recht.nulegal.eu"
USER_AGENT = f"recht-mcp/{__version__} (+https://github.com/nulegal-startup/recht-mcp)"


def _q(value: Any) -> str:
    """One path segment, percent-encoded including '/'."""
    return urllib.parse.quote(str(value), safe="")


def _path_tail(url: str | None, prefix: str) -> str | None:
    """The unquoted path after `prefix` in one of the service's own URLs."""
    if not url:
        return None
    path = urllib.parse.urlsplit(url).path
    if not path.startswith(prefix):
        return None
    return urllib.parse.unquote(path[len(prefix):])


class HTTPBackend(BaseBackend):
    """`Backend` over HTTPS. Thread-safe; one instance serves every call."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, *, timeout: float = 30.0,
                 law_cache_seconds: float = 600.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._law_cache: dict[str, tuple[float, tuple[dict | None, list[dict]]]] = {}
        self._law_cache_seconds = law_cache_seconds
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ HTTP
    def _request(self, method: str, path: str, params: dict | None = None,
                 body: Any = None, *, raw: bool = False) -> tuple[int, Any]:
        """`(status, parsed_body)`. 2xx-4xx come back; 429, 5xx and network
        failures raise `Unavailable`."""
        query = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
        data = None
        headers = {"User-Agent": USER_AGENT,
                   "Accept": "text/markdown, text/plain" if raw else "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status, payload = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise Unavailable(f"{self.base_url} not reachable: {exc}") from None
        if status == 429 or status >= 500:
            raise Unavailable(f"{self.base_url} answered HTTP {status}")
        text = payload.decode("utf-8", "replace")
        if raw and status < 300:
            return status, text
        try:
            return status, json.loads(text) if text else None
        except ValueError:
            return status, text

    def _get(self, path: str, **params) -> tuple[int, Any]:
        return self._request("GET", path, params)

    @staticmethod
    def _detail(body: Any) -> Any:
        return body.get("detail", body) if isinstance(body, dict) else body

    # ------------------------------------------------------- citation grammar
    def parse_prose_citation(self, text: str) -> dict | None:
        return grammar.parse_prose_citation(text)

    def is_court_head(self, text: str) -> bool:
        return grammar.is_court_head(text)

    def docket_key(self, text: str) -> str | None:
        return grammar.docket_key(text)

    # ----------------------------------------------------------------- resolve
    def resolve(self, citations: list[str], include: list[str]) -> list:
        status, body = self._request("POST", "/v1/resolve",
                                     body={"citations": citations, "include": include})
        if status != 200:
            raise Unavailable(f"resolve answered HTTP {status}: {self._detail(body)}")
        results = body.get("results", body) if isinstance(body, dict) else body
        return results if isinstance(results, list) else []

    # ---------------------------------------------------------- laws and norms
    @staticmethod
    def _law_record(meta: dict) -> dict:
        gazette = meta.get("binding_gazette") or {}
        source = meta.get("source") or {}
        return {"id": meta.get("slug"), "slug": meta.get("slug"),
                "jurabk": meta.get("jurabk"), "title": meta.get("title"),
                "status": meta.get("status"), "fundstelle": meta.get("fundstelle"),
                "jurisdiction": gazette.get("jurisdiction"),
                "url_key": _path_tail(source.get("url"), "/gesetze/")}

    def find_law(self, abbrev: str) -> tuple[dict | None, list[dict]]:
        now = time.monotonic()
        with self._lock:
            hit = self._law_cache.get(abbrev)
        if hit and now - hit[0] < self._law_cache_seconds:
            return hit[1]
        status, body = self._get(f"/v1/law/{_q(abbrev)}")
        if status == 200 and isinstance(body, dict):
            law = self._law_record(body)
            found: tuple[dict | None, list[dict]] = (law, [law])
        elif status == 300:
            detail = self._detail(body)
            cands = detail.get("candidates") if isinstance(detail, dict) else None
            found = (None, [self._law_record(c) for c in cands or []])
        else:
            found = (None, [])
        with self._lock:
            self._law_cache[abbrev] = (now, found)
        return found

    def is_land(self, law: dict) -> bool:
        return law.get("jurisdiction") not in (None, "BUND")

    def law_url_key(self, law: dict) -> str:
        return law.get("url_key") or law.get("jurabk") or law["slug"]

    @staticmethod
    def _norm_record(payload: dict) -> dict:
        page = (payload.get("source") or {}).get("url")
        tail = _path_tail(page, "/gesetze/")
        return {"ref": payload.get("ref"), "heading": payload.get("heading"),
                "ref_norm": tail.rsplit("/", 1)[-1] if tail and "/" in tail else None,
                "url": page}

    def find_norm(self, law: dict, ref: str, as_of: str | None) -> tuple[dict | None, Any]:
        path = f"/v1/norm/{_q(law['slug'])}/{_q(ref)}"
        status, body = self._get(path, asof=as_of)
        if status == 200 and isinstance(body, dict):
            return self._norm_record(body), body
        detail = self._detail(body)
        if as_of and isinstance(detail, dict):
            # The provision exists; no stored version covers `as_of`.
            status, body = self._get(path)
            if status == 200 and isinstance(body, dict):
                return self._norm_record(body), None
        return None, None

    def norm_payload(self, law: dict, norm: dict, version: Any) -> dict:
        # The REST route adds `version_coverage` to its payload; the tool layer
        # states version coverage once, at the top of its own answer.
        return {k: v for k, v in (version or {}).items() if k != "version_coverage"}

    def norm_url(self, law: dict, norm: dict) -> str | None:
        return norm.get("url")

    def norm_markdown(self, law: dict, norm: dict, ref: str, as_of: str | None) -> str | None:
        if not norm.get("ref_norm"):
            return None
        path = (f"/gesetze/{_q(self.law_url_key(law))}/{_q(norm['ref_norm'])}"
                f"/download.md")
        try:
            status, text = self._request("GET", path, {"asof": as_of}, raw=True)
        except Unavailable:
            return None
        return text if status == 200 and isinstance(text, str) and text else None

    def norm_versions(self, law: dict, norm: dict) -> list[dict]:
        status, body = self._get(
            f"/v1/norm/{_q(law['slug'])}/{_q(norm.get('ref_norm') or norm['ref'])}/versions")
        if status != 200 or not isinstance(body, dict):
            raise Unavailable(f"norm versions answered HTTP {status}")
        return body.get("versions") or []

    # ------------------------------------------------------------------ search
    def search_norms(self, query: str, *, limit: int, include_repealed: bool) -> dict:
        status, body = self._get("/v1/search", q=query, limit=limit, m="auto",
                                 repealed="true" if include_repealed else "false")
        if status != 200 or not isinstance(body, dict):
            raise Unavailable(f"search answered HTTP {status}: {self._detail(body)}")
        return body

    def search_cases(self, query: str, *, limit: int) -> dict | list:
        status, body = self._get("/v1/case-search", q=query, limit=limit)
        if status != 200:
            raise Unavailable(f"case search answered HTTP {status}: {self._detail(body)}")
        return body

    # ---------------------------------------------------------- citation graph
    def citing_decisions_for_norm(self, abbrev: str, ref: str, *, limit: int,
                                  offset: int, sort: str) -> dict:
        status, body = self._get(f"/v1/norm/{_q(abbrev)}/{_q(ref)}/citing-cases",
                                 limit=limit, offset=offset, sort=sort)
        if status != 200 or not isinstance(body, dict):
            raise NotFound(self._detail(body))
        return body

    def case_key(self, case_id: str) -> str:
        if not CASE_URL.match(case_id.strip()):
            return case_id
        status, body = self._get("/v1/case-key", case=case_id)
        if status == 200 and isinstance(body, dict) and body.get("key"):
            return body["key"]
        return case_id

    def citing_decisions_for_case(self, key: str, *, limit: int, offset: int,
                                  sort: str) -> dict | None:
        status, body = self._get("/v1/case-citations/citing", case=key, limit=limit,
                                 offset=offset, sort=sort)
        if status == 404:
            return None
        if status != 200 or not isinstance(body, dict):
            raise Unavailable(f"citing decisions answered HTTP {status}")
        return body

    def get_case(self, key: str, *, paragraphs: bool) -> dict:
        status, body = self._get("/v1/case", doknr=key,
                                 paragraphs="true" if paragraphs else "false")
        if status != 200 or not isinstance(body, dict):
            raise NotFound(self._detail(body))
        return body

    def cited_decisions(self, doknr: str | None) -> list[dict]:
        if not doknr:
            return []
        status, body = self._get("/v1/case-citations/cited", case=doknr)
        if status == 404:
            return []
        if status != 200 or not isinstance(body, dict):
            raise Unavailable(f"cited decisions answered HTTP {status}")
        return body.get("decisions") or []

    # -------------------------------------------------------- feed and coverage
    def changes(self, *, since: str | None, limit: int, days: int) -> list[dict]:
        status, body = self._get("/v1/changes", since=since, limit=limit, days=days)
        if status != 200 or not isinstance(body, dict):
            raise Unavailable(f"changes answered HTTP {status}")
        return body.get("changes") or []

    def stats(self) -> dict:
        status, body = self._get("/v1/stats")
        if status != 200 or not isinstance(body, dict):
            raise Unavailable(self._detail(body))
        return body
