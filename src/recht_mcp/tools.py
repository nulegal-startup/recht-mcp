# SPDX-License-Identifier: AGPL-3.0-only
"""The nine tools: their contracts, their validation and their answers.

Every tool here is a thin layer over a `Backend` (see `recht_mcp.backend`): it
validates the arguments, asks the backend for data, and shapes the answer. The
shaping is where the value is, because the answers are built to carry two
things a plain REST response often does not: what the answer is, and what the
answer is NOT.

  * `version_coverage` on every norm answer. The norm-version archive has a
    floor; a missing version before it means the archive does not reach that
    date — never that the provision did not exist. An agent that reads a bare
    "not found" for `as_of=2014-08-01` concludes the provision did not exist in
    2014, which can be exactly wrong. So the floor travels with every answer.
  * `coverage` on `listCitingDecisions`. The decision-to-norm citation graph is
    built over federal case law, so a Land provision can answer zero citing
    decisions because it is not indexed. That zero is labelled, never asserted.
  * Three different misses, never collapsed: `not_in_corpus` (not held, and no
    evidence it exists), `outside_coverage` (it may exist, outside what is
    mirrored) and `known_missing` (it provably exists — decisions that ARE held
    cite it — but its text is not held).
  * A norm version date is `first_observed`, never "in force from", and travels
    with its own `date_precision`.
  * A passage's `rn` is the Randnummer the court printed; it is never inferred
    from position.
"""
from __future__ import annotations

import re
from urllib.parse import quote as _quote

from .backend import Backend, NotFound, Unavailable
from .citations import court_mismatch, normalise_citation

__all__ = ["TOOLS", "REASONS", "INSTRUCTIONS", "ToolError", "tool_list", "call_tool",
           "version_coverage", "provision_key", "PASSAGE_LIMIT", "CHANGES_WINDOW_DAYS",
           "CASE_KEY_HINT"]

#: Shown by clients that render one. Kept to what the corpus is, with the two
#: limits an agent has to know before its first call.
INSTRUCTIONS = (
    "German federal statutes and case law, plus the Landesrecht of Bayern, "
    "Brandenburg, Nordrhein-Westfalen and Sachsen. Read-only, no key, free, "
    "CC BY 4.0.\n\n"
    "Two limits worth knowing before you rely on an answer:\n"
    "1. Norm version dates are FIRST-OBSERVED dates, not legal Inkrafttreten, "
    "and the version archive starts 2019-06-10. Never compute a limitation "
    "period or a transitional boundary from `first_observed` without reading "
    "`date_precision` next to it.\n"
    "2. The decision-to-norm citation graph is built over federal case law. A "
    "Land norm can answer with zero citing decisions because the graph does not "
    "index it, not because no court has cited it; `coverage` says which.\n\n"
    "Call `getCoverage` once at the start of a session if the answer depends on "
    "what the corpus does and does not hold. Call `resolveIdentifiers` before "
    "you state any citation you did not read here — it answers `not_in_corpus` "
    "rather than guessing a near match."
)


# --------------------------------------------------------------------------
# Honest failures
# --------------------------------------------------------------------------

class ToolError(Exception):
    """A tool that cannot answer, with a machine-readable reason.

    NOT a JSON-RPC error. MCP draws the line exactly where it belongs: a
    protocol fault (unknown method, malformed params) is a JSON-RPC error and a
    tool that ran and could not answer is a RESULT carrying `isError: true`, so
    the model sees the reason and can act on it instead of the client swallowing
    a transport failure. `reason` comes from one closed vocabulary, `REASONS` —
    `not_in_corpus`, `outside_coverage` and `known_missing` are three different
    answers and are never collapsed into one.
    """

    def __init__(self, reason: str, detail: str, **extra):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.extra = extra

    def payload(self) -> dict:
        return {"error": True, "reason": self.reason, "detail": self.detail,
                **self.extra}


#: Every `reason` any tool can emit. Published in full so a caller can branch
#: on the set without discovering it one failure at a time.
REASONS = {
    "invalid_argument": "the call was malformed — a required argument is missing, "
                        "empty, or outside its range",
    "unparseable": "the identifier could not be read as any supported citation kind",
    "ambiguous": "the identifier names more than one object; candidates are listed",
    "not_in_corpus": "we do not hold this object and have no evidence that it exists",
    "known_missing": "this decision provably exists — decisions we DO hold cite it — "
                     "but we do not hold its text",
    "outside_coverage": "the object may well exist; it falls outside the period, "
                        "jurisdiction or source we mirror",
    "unavailable": "a dependency was not reachable for this call; the answer is "
                   "unknown, not negative",
}


# --------------------------------------------------------------------------
# Shared honesty blocks
# --------------------------------------------------------------------------

def version_coverage(backend: Backend, asked_asof: str | None = None) -> dict:
    """The block every norm answer carries, asked for or not.

    A version lookup before the archive floor fails correctly about the archive
    and wrongly about the law, unless the answer says which. So the floor
    travels with every answer rather than only with the failure.
    """
    floor = backend.archive_floor()
    out = {
        "archive_starts": floor,
        "note": (f"Stored norm versions begin {floor}. A missing version before "
                 "that date means our archive does not reach it — never that the "
                 "provision did not exist. Version dates are first-observed "
                 "dates, not legal Inkrafttreten."),
    }
    if asked_asof and asked_asof < floor:
        out["asked_before_archive"] = True
    return out


def _citation_graph_coverage(backend: Backend, law: dict, total: int) -> dict:
    """`coverage` for a norm's citing-decision list.

    A zero over a Land provision is shaped exactly like a genuine zero unless
    the answer says the graph does not index it — and an agent then states that
    there is no case law on the provision. The scope of the graph is a fact we
    hold; withholding it is what turns a gap into an assertion.
    """
    if not backend.is_land(law):
        return {"citation_graph": "bund", "complete_for_this_norm": True}
    return {"citation_graph": "bund",
            "complete_for_this_norm": False,
            "note": (("The decision-to-norm citation graph is built over federal "
                      "case law. This zero means the Land provision is not "
                      "indexed by that graph, NOT that no court has cited it. "
                      "Use `search` with the provision's wording to look for "
                      "Land decisions.")
                     if not total else
                     ("The decision-to-norm citation graph is built over federal "
                      "case law. These are the decisions it indexed; decisions "
                      "of the Land's own courts citing this provision are "
                      "largely outside it. Use `search` to look for those."))}


# --------------------------------------------------------------------------
# Laws
# --------------------------------------------------------------------------

def _law_choice(law: dict) -> dict:
    """One law as a pickable candidate: `law_key` is accepted as `law` by every
    tool and names exactly this law."""
    return {"law_key": law.get("slug"), "jurabk": law["jurabk"],
            "title": law["title"], "status": law.get("status")}


def _same_abbrev(backend: Backend, abbrev: str, law: dict) -> list[dict]:
    """The OTHER laws `abbrev` also names. The law lookup picks one ('LWG' → the
    federal LwG, not Bayern's LWG) and a provision miss on the pick must not
    read as a miss everywhere."""
    _, rows = backend.find_law(abbrev)
    return [_law_choice(r) for r in rows if r.get("id") != law.get("id")][:10]


def _law_or_fail(backend: Backend, abbrev: str):
    law, candidates = backend.find_law(abbrev)
    if law is None:
        if candidates:
            # A list of bare abbreviations gives a model nothing to pick WITH
            # when two laws share one — hence the `law_key` on every candidate.
            raise ToolError(
                "ambiguous", f"'{abbrev}' names more than one law in the corpus. "
                "Call again with `law` set to a candidate's `law_key`.",
                candidates=[_law_choice(c) for c in candidates[:10]])
        raise ToolError("not_in_corpus", f"No law is held under '{abbrev}'. "
                        "Try `search` with the law's name, or `getCoverage` for "
                        "what the corpus holds.")
    return law


def _attach_fundstelle(backend: Backend, rows: list[dict]) -> None:
    """Put the gazette citation on every resolved norm result.

    The resolve payload names the provision; the gazette citation
    (`fundstelle`) belongs to the LAW. A caller grounding a citation should get
    the citation a court accepts, not only our reading-copy URL — and for Land
    law the gazette citation is the only published source reference there is.

    One lookup per DISTINCT law, not per citation — a batch of 100 citations is
    typically a handful of statutes — and any failure leaves the field absent
    rather than inventing one.
    """
    cache: dict[str, str | None] = {}
    for row in rows:
        abk = row.get("law")
        if not abk or row.get("citation_kind") not in (None, "norm"):
            continue
        if abk not in cache:
            try:
                law, _c = backend.find_law(abk)
                cache[abk] = (law or {}).get("fundstelle")
            except Exception:  # noqa: BLE001 — never fail a resolve for a nicety
                cache[abk] = None
        if cache[abk]:
            row["fundstelle"] = cache[abk]


# --------------------------------------------------------------------------
# The tools
# --------------------------------------------------------------------------

def t_resolveIdentifiers(args: dict, backend: Backend) -> dict:
    items = args.get("citations")
    if not isinstance(items, list) or not items:
        raise ToolError("invalid_argument", "`citations` must be a non-empty list "
                                            "of citation strings.")
    if len(items) > 100:
        raise ToolError("invalid_argument", "At most 100 citations per call.")
    include = args.get("include") or []
    if [x for x in include if x not in ("text", "leitsatz")]:
        raise ToolError("invalid_argument",
                        "`include` accepts only 'text' and 'leitsatz'. An unknown "
                        "value is refused rather than ignored: a caller who "
                        "misspells it must not read a 300-character preview as a "
                        "whole Leitsatz.")
    norm = [normalise_citation(x, backend) for x in items]
    results = backend.resolve([n["normalised"][:200] for n in norm], list(include))
    out = []
    for n, r in zip(norm, results if isinstance(results, list) else []):
        row = dict(r) if isinstance(r, dict) else {"result": r}
        if n["normalised"] != n["query"]:
            # Never silent: the caller must be able to see that what we sent the
            # resolver is not byte-for-byte what it wrote.
            row["normalised_from"] = n["query"]
            row["normalised_to"] = n["normalised"]
        # Echoed whether or not the string was rewritten: the court and the date
        # travel INTO the resolver as disambiguators, so what we read out of the
        # citation is part of how the answer was reached, and a caller cannot
        # audit a disambiguation it cannot see.
        if n["asserted_court"]:
            row["asserted_court"] = n["asserted_court"]
        # The docket is real and the DATE the model held is wrong: saying so is
        # the whole point of a grounding call. Answering "resolved" and letting
        # the caller re-publish its own bad date is not.
        case = row.get("case") or {}
        check = row.get("citation_check") or {}
        actual = str(case.get("decided") or "")
        if n["asserted_date"]:
            row["asserted_date"] = n["asserted_date"]
            if actual and actual != n["asserted_date"]:
                row["date_mismatch"] = {
                    "asserted": n["asserted_date"], "actual": actual,
                    "note": ("The docket resolves, but to a decision with a "
                             "different date. Cite the actual date.")}
        # EITHER comparator may flag, and neither may veto the other. The
        # resolver's `citation_check.court_matches` drops the SEAT on purpose (a
        # bare 'OLG' must not contradict 'OLG Köln'), so it answers True for
        # Bamberg against Brandenburg — the very pair this flag exists for.
        # `court_mismatch` is the seat-aware one. Taking the OR of the two can
        # only ever raise the flag more often, which is the safe direction for a
        # warning a model reads before it publishes a citation.
        if check.get("court_matches") is False or court_mismatch(
                n["asserted_court"], case.get("court")):
            row["court_mismatch"] = {
                "asserted": n["asserted_court"], "actual": case.get("court"),
                "note": ("The docket resolves, but at a DIFFERENT court. An "
                         "Aktenzeichen is unique per court only — this is most "
                         "likely not the decision you meant. Do not cite it as "
                         "that court's; `search` with court and topic instead.")}
        if row.get("status") == "unknown" and not row.get("reason"):
            # A bare '§ 5' used to come back {status: unknown} with no reason and
            # no hint — the one miss shape `REASONS` did not explain. A Land we
            # do not mirror is a coverage hole, not an absence.
            land = str(row.get("note") or "").startswith("Landesrecht (")
            row["reason"] = "outside_coverage" if land else "not_in_corpus"
            row.setdefault("note", (
                f"{row['law']} is held, but no provision under this number."
                if row.get("law") else
                "No law named in the citation, or none we hold under that name. "
                "Write the abbreviation after the provision ('§ 5 BDSG'); "
                "`search` finds a law by its name."))
        out.append(row)
    _attach_fundstelle(backend, out)
    return {"results": out,
            "reasons": REASONS,
            "note": ("A miss is data, never a guess: this tool answers "
                     "`not_in_corpus` or `known_missing` rather than returning "
                     "the nearest-looking decision.")}


def t_search(args: dict, backend: Backend) -> dict:
    query = (args.get("q") or "").strip()
    if len(query) < 2:
        raise ToolError("invalid_argument", "`q` must be at least 2 characters.")
    query = query[:2000]
    scope = args.get("scope") or "all"
    if scope not in ("all", "norms", "cases"):
        raise ToolError("invalid_argument", "`scope` must be 'all', 'norms' or 'cases'.")
    limit = args.get("limit") or 10
    if not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ToolError("invalid_argument", "`limit` must be an integer 1..50.")
    out: dict = {"query": query, "scope": scope}

    if scope in ("all", "norms"):
        norms = backend.search_norms(query, limit=min(limit, 100),
                                     include_repealed=bool(args.get("include_repealed")))
        rows = norms.get("results") or []
        out["norms"] = rows
        if norms.get("corrected"):
            out["corrected"] = norms["corrected"]
        if norms.get("law_title_match"):
            out["law_title_match"] = norms["law_title_match"]
        # The index already answers a cross-Land question in one call (Bund plus
        # several Länder for "Videoüberwachung öffentlich zugänglicher Räume")
        # and never SAYS it did. The roll-up is presentation over a capability
        # that is already there.
        buckets: dict[str, dict] = {}
        for r in rows:
            code = r.get("jurisdiction") or "bund"
            label = r.get("jurisdiction_label") or "Bund"
            b = buckets.setdefault(code, {"jurisdiction": code,
                                          "jurisdiction_label": label,
                                          "n": 0, "norms": []})
            b["n"] += 1
            if len(b["norms"]) < 5:
                b["norms"].append({"jurabk": r.get("display_abk") or r.get("jurabk"),
                                   "ref": r.get("ref"), "heading": r.get("heading"),
                                   "url": r.get("url")})
        if len(buckets) > 1:
            out["by_jurisdiction"] = sorted(buckets.values(), key=lambda b: -b["n"])

    if scope in ("all", "cases"):
        cases = backend.search_cases(query, limit=min(limit, 20))
        rows = cases.get("results", cases) if isinstance(cases, dict) else cases
        out["cases"] = rows if isinstance(rows, list) else []
        if isinstance(cases, dict):
            for k in ("frequently_cited", "docket_match", "fundstelle_match",
                      "attribution"):
                if cases.get(k):
                    out[k] = cases[k]

    # The norm index legitimately misses a term of art the statute does not use
    # ("Verzugspauschale" — § 288 BGB says "Pauschale in Höhe von 40 Euro"), and
    # a bare empty array reads as "the corpus holds nothing on this" while many
    # decisions use the word. One field turns a dead end into a next call.
    if scope == "norms" and not out.get("norms"):
        out["cases_available"] = "unknown"
        out["next"] = ("No statute matched. Terms of art often appear only in "
                       "case law — retry this tool with scope='cases' or "
                       "scope='all' before concluding the corpus is silent.")
    elif scope == "all":
        out["counts"] = {"norms": len(out.get("norms") or []),
                         "cases": len(out.get("cases") or [])}
        if not out.get("norms") and out.get("cases"):
            out["note"] = ("No statute matched this wording but case law does. "
                           "That is common for terms of art the statute itself "
                           "does not use.")
    out["source"] = backend.self_source()
    return out


#: 'Abs. 1', 'S. 2', 'Nr. 3', 'lit. f', 'UAbs. 1' and whatever follows them.
_SUBUNIT = re.compile(r"\s+(?:(?:abs|absatz|satz|nr|nummer|lit|buchst|hs|halbsatz|"
                      r"uabs|unterabs|unterabsatz)\b\.?|s\.).*$", re.I)


def provision_key(ref: str) -> str:
    """The provision a model means by `ref`, as the norm lookup keys it.

    Models write '556g Abs. 1' and '556 g', and both would otherwise answer
    `not_in_corpus` — a confident negative for a provision that is held.
    getNorm serves whole provisions, so the sub-unit is dropped (the answer
    says `ref_normalised_from`, never silently) and a split letter suffix is
    joined.
    """
    key = _SUBUNIT.sub("", ref.strip())
    return re.sub(r"(\d)\s+([a-zA-Z]{1,3})$", r"\1\2", key).strip() or ref


def _amendment_history_url(backend: Backend, law: dict) -> str:
    return (f"{backend.base_url}/gesetze/"
            f"{_quote(backend.law_url_key(law), safe='')}/aenderungen")


def t_getNorm(args: dict, backend: Backend) -> dict:
    abbrev = (args.get("law") or "").strip()
    ref = str(args.get("ref") or "").strip()
    if not abbrev or not ref:
        raise ToolError("invalid_argument", "`law` and `ref` are both required, "
                                            "e.g. law='BGB', ref='622'.")
    fmt = args.get("format") or "markdown"
    if fmt not in ("markdown", "json"):
        raise ToolError("invalid_argument", "`format` must be 'markdown' or 'json'.")
    asof = args.get("as_of")
    if asof and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(asof)):
        raise ToolError("invalid_argument", "`as_of` must be YYYY-MM-DD.")
    law = _law_or_fail(backend, abbrev)
    asked, ref = ref, provision_key(ref)
    norm, ver = backend.find_norm(law, ref, asof)
    if norm is None:
        others = _same_abbrev(backend, abbrev, law)
        raise ToolError("not_in_corpus",
                        f"'{asked}' is not a provision of {law['jurabk']} in this corpus."
                        + (f" '{abbrev}' also names other laws — try one of "
                           f"`other_laws` by its `law_key`." if others else ""),
                        law={"jurabk": law["jurabk"], "title": law["title"]},
                        **({"other_laws": others} if others else {}))
    if ver is None and asof:
        # NOT a bare "not found": without the archive floor next to it, an agent
        # infers the provision did not exist on that date.
        raise ToolError(
            "outside_coverage",
            f"No stored version of {norm['ref']} {law['jurabk']} as of {asof}. "
            f"The version archive starts {backend.archive_floor()}; this says nothing "
            f"about whether the provision was in force on that date.",
            version_coverage=version_coverage(backend, str(asof)),
            amendment_history_url=_amendment_history_url(backend, law))

    payload = backend.norm_payload(law, norm, ver)
    out = {"law": law["jurabk"], "ref": norm["ref"],
           "heading": norm.get("heading"),
           # The backend builds the link: some laws carry a space or a slash in
           # their URL key ('BNatSchG 2009', 'FreizügG/EU 2004').
           "url": backend.norm_url(law, norm),
           **({"ref_normalised_from": asked} if ref != asked else {}),
           "version_coverage": version_coverage(backend, str(asof) if asof else None)}
    if fmt == "markdown":
        # The leanest machine form of a provision — about a tenth the size of
        # the reader page. A render that fails is reported, never guessed.
        out["markdown"] = backend.norm_markdown(law, norm, ref, asof)
        if not out.get("markdown"):
            out["markdown_unavailable"] = ("No renderable text for this provision "
                                           "(e.g. '(weggefallen)'). The JSON "
                                           "payload below is what we hold.")
            out["norm"] = payload
    else:
        out["norm"] = payload
    # The most dangerous date in the payload, and its name is the only defence
    # that travels with the data.
    vv = (payload.get("version") or {}) if isinstance(payload, dict) else {}
    out["version"] = {
        "first_observed": vv.get("valid_from"),
        "valid_to": vv.get("valid_to"),
        "date_precision": vv.get("date_precision"),
        "amendment_note": vv.get("amendment_note"),
        "note": ("`first_observed` is the day we first saw this text, NOT the "
                 "legal Inkrafttreten. Branch on `date_precision` before "
                 "computing any deadline from it."),
    }
    out["authoritative_source"] = payload.get("authoritative_source") if isinstance(payload, dict) else None
    out["fundstelle"] = law.get("fundstelle")
    return out


def t_listNormVersions(args: dict, backend: Backend) -> dict:
    """Every observed version of one provision, with the archive floor stated
    once — so a caller can find out which dates `getNorm(as_of=…)` can answer
    before it asks."""
    law = _law_or_fail(backend, (args.get("law") or "").strip())
    ref = str(args.get("ref") or "").strip()
    if not ref:
        raise ToolError("invalid_argument", "`ref` is required, e.g. '622'.")
    norm, _ver = backend.find_norm(law, ref, None)
    if norm is None:
        raise ToolError("not_in_corpus",
                        f"'{ref}' is not a provision of {law['jurabk']} in this corpus.")
    rows = backend.norm_versions(law, norm)
    floor = backend.archive_floor()
    versions = [{
        "first_observed": r["first_observed"],
        "valid_to": r["valid_to"],
        "date_precision": r["date_precision"],
        "at_archive_floor": r["first_observed"] == floor,
        "amendment_note": r["amendment_note"],
        "in_force": r["valid_to"] is None,
    } for r in rows]
    return {
        "law": law["jurabk"], "ref": norm["ref"], "heading": norm.get("heading"),
        "url": backend.norm_url(law, norm),
        "versions": versions,
        "version_coverage": version_coverage(backend),
        "amendment_history_url": _amendment_history_url(backend, law),
        "note": ("A version whose `at_archive_floor` is true is the text as it "
                 "stood when mirroring began — its date is a floor, not an "
                 "amendment. Earlier amendments are named in the law's own "
                 "Änderungsverlauf at `amendment_history_url`."),
    }


def t_listCitingDecisions(args: dict, backend: Backend) -> dict:
    limit = args.get("limit") or 20
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ToolError("invalid_argument", "`limit` must be an integer 1..100.")
    offset = args.get("offset") or 0
    if not isinstance(offset, int) or not 0 <= offset <= 100000:
        raise ToolError("invalid_argument", "`offset` must be an integer 0..100000.")
    sort = args.get("sort") or "weight"
    if sort not in ("weight", "recent"):
        raise ToolError("invalid_argument", "`sort` must be 'weight' or 'recent'.")
    law_abk = (args.get("law") or "").strip()
    ref = str(args.get("ref") or "").strip()
    case_id = (args.get("case") or "").strip()
    if bool(law_abk and ref) == bool(case_id):
        raise ToolError("invalid_argument",
                        "Give either `law` + `ref` (decisions citing a statute "
                        "provision) or `case` (decisions citing a decision) — "
                        "exactly one of the two.")
    if law_abk:
        law = _law_or_fail(backend, law_abk)
        try:
            got = backend.citing_decisions_for_norm(law_abk, ref, limit=limit,
                                                    offset=offset, sort=sort)
        except NotFound as exc:
            # The law resolved (above), so a miss here is about the PROVISION.
            # Reporting it as an outage would be the wrong direction: this is a
            # real "we do not hold that provision".
            raise ToolError("not_in_corpus",
                            f"'{ref}' is not a provision of {law['jurabk']} in "
                            f"this corpus. ({exc.detail})") from None
        out = dict(got)
        out["coverage"] = _citation_graph_coverage(backend, law, got.get("total") or 0)
        if (got.get("total") or 0) > offset + limit:
            # For a provision with many EU decisions the first page can be almost
            # all CJEU, and German courts only appear past it. An agent that
            # trusts page one draws the wrong conclusion about national case law.
            out["pagination"] = {
                "returned": len(got.get("cases") or []), "total": got["total"],
                "offset": offset, "max_limit": 100,
                "next_offset": offset + limit,
                "note": (
                    "Ordered newest decision first. Page on with `offset`."
                    if (out.get("ranking") or {}).get("sort") == "recent" else
                    "Ranked by citation weight, then court tier, then "
                    "recency — so the first page skews toward the most-cited "
                    "courts. Page on with `offset` to see the national "
                    "courts below them.")}
        # The fall-back is a fact about THIS answer and a model must not have to
        # find it inside a nested block it was never told to read. `sort_applied`
        # is flat and always present once a sort was asked for, so a caller can
        # branch on one key instead of learning `ranking`'s shape.
        rank = out.get("ranking") or {}
        if sort != "weight":
            out["sort_applied"] = rank.get("sort") or "weight"
        if rank.get("requested_sort"):
            out["sort_note"] = (
                f"`sort: \"recent\"` could not be served for this provision "
                f"({rank.get('sort_note')}) — these rows are in WEIGHT order. Do "
                f"not describe them as the most recent decisions.")
        return out

    got = backend.citing_decisions_for_case(backend.case_key(case_id), limit=limit,
                                            offset=offset, sort=sort)
    if got is None:
        raise ToolError("not_in_corpus",
                        f"No decision is held under '{case_id}'. " + CASE_KEY_HINT
                        + " It also distinguishes `not_in_corpus` from "
                          "`known_missing`.")
    row, served = got["case"], got["served"]
    cases = []
    for r in got["rows"]:
        cases.append({"court": r["court"], "decided": str(r["decided"]),
                      "aktenzeichen": r["aktenzeichen"], "doknr": r["doknr"],
                      "ecli": r["ecli"], "leitsatz": r["leitsatz"] or None,
                      # The pinpoint AS THE CITING COURT WROTE IT, so the link
                      # lands on the sentence rather than the top of a long
                      # document. Null where the citing document carries no
                      # Randnummern.
                      "citing_rn": r["rn"],
                      "url": f"{r['url']}#rd_{r['rn']}" if r["rn"] else r["url"]})
    out = {"case": {"court": row["court"], "decided": str(row["decided"]),
                    "aktenzeichen": row["aktenzeichen"]},
           "total": row.get("cited_by") or len(cases),
           "cases": cases,
           "coverage": {"citation_graph": "bund", "complete_for_this_norm": True},
           "note": ("`citing_rn` is the Randnummer of the CITING decision that "
                    "carries the citation, as that court numbered it — not a "
                    "position in our text.")}
    if sort != "weight":
        out["sort_applied"] = served
        if served != sort:
            out["sort_note"] = (
                "`sort: \"recent\"` could not be served for this decision — it has "
                "too many citers to order by date inside the query budget. These "
                "rows are in WEIGHT order; do not describe them as the most recent.")
    return out


#: A reader-page URL of a decision: /rechtsprechung/{court}/{date}/{slug}.
CASE_URL = re.compile(r"^(?:https?://[^/]+)?/rechtsprechung/([^/?#]+)/"
                      r"(\d{4}-\d{2}-\d{2})/([^/?#]+)/?(?:[?#].*)?$")

CASE_KEY_HINT = ("`case` takes a juris doknr, an ECLI, or this site's decision "
                 "URL (as `search` returns it). For an Aktenzeichen, call "
                 "`resolveIdentifiers` first — it returns the doknr.")


def _refuse_textless(payload: dict, because: str) -> None:
    """Raise the right honest failure for a decision that carries no text.

    Two states have to be told apart here:

      `known_missing`  we can PROVE the decision exists — decisions we hold cite
                       it by Aktenzeichen — and we do not hold its text. A plain
                       "not found" would have an agent tell a lawyer the
                       decision does not exist, and for this set that is wrong.
      `joined_with`    the same, except the decision was decided as a joined case
                       and its text IS the lead decision's. That is not a miss at
                       all, so the caller is handed the key to call again with —
                       collapsing it into `known_missing` would send an agent
                       away from a text it can have.
    """
    status = payload.get("status")
    if status == "joined_with":
        joined = payload.get("joined_with") or {}
        raise ToolError(
            "known_missing",
            "Decided as a joined case: this decision has no separate text, "
            f"{because}. The text is the lead decision's — call again with the "
            "`joined_with` key below.",
            joined_with=joined,
            cited_by=payload.get("cited_by"))
    if status == "known_missing" or payload.get("text_available") is False:
        raise ToolError(
            "known_missing",
            "This decision provably exists — decisions we hold cite it — but we "
            f"do not hold its text, {because}.",
            cited_by=payload.get("cited_by"),
            court_hint=payload.get("court_hint"),
            decided_hint=payload.get("decided_hint"),
            # Inferred from how the citing decisions name it, never checked
            # against the decision itself — the field names say so.
            attested_by=(payload.get("citers") or [])[:10],
            sources=payload.get("sources") or [])


def _case_or_fail(backend: Backend, case_id: str, paragraphs: bool) -> dict:
    try:
        return backend.get_case(backend.case_key(case_id), paragraphs=paragraphs)
    except NotFound as exc:
        raise ToolError("not_in_corpus", f"No decision is held under '{case_id}'. "
                                         f"({exc.detail}) " + CASE_KEY_HINT) from None


def t_listCitedAuthorities(args: dict, backend: Backend) -> dict:
    case_id = (args.get("case") or "").strip()
    if not case_id:
        raise ToolError("invalid_argument", "`case` is required. " + CASE_KEY_HINT)
    payload = _case_or_fail(backend, case_id, False)
    _refuse_textless(payload, "so it has no outgoing citations here")
    cases = []
    for r in backend.cited_decisions(payload.get("doknr")):
        cases.append({
            "court": r["court"], "decided": str(r["decided"]),
            "aktenzeichen": r["aktenzeichen"], "doknr": r["doknr"],
            "ecli": r["ecli"], "citations_in_this_decision": r["n"],
            "url": r["url"]})
    return {
        "case": {"court": payload.get("court"), "decided": payload.get("decided"),
                 "aktenzeichen": payload.get("aktenzeichen"),
                 "doknr": payload.get("doknr"), "ecli": payload.get("ecli"),
                 "url": payload.get("url")},
        "cited_norms": payload.get("cited_norms") or [],
        "cited_decisions": cases,
        "treatment": None,
        "note": ("`treatment` (gefolgt / abgegrenzt / aufgegeben) is null on "
                 "every edge and will stay null until it is classified and "
                 "measured — a wrong 'aufgegeben' is worse than none. "
                 "`citations_in_this_decision` is how often this decision cites "
                 "that one, which is weight, not treatment."),
        "source": payload.get("source_info"),
    }


#: Passages per page when the caller names no `limit`. A decision's passages
#: are read for one passage far more often than for all of them, and a whole
#: long judgment in one answer costs a model tens of kilobytes of context — so
#: the default is a page and `around` is the way to land on the right one. The
#: CEILING is 400: a caller that knows it wants the whole text can say so.
PASSAGE_LIMIT = 30


def t_listCasePassages(args: dict, backend: Backend) -> dict:
    case_id = (args.get("case") or "").strip()
    if not case_id:
        raise ToolError("invalid_argument", "`case` is required. " + CASE_KEY_HINT)
    payload = _case_or_fail(backend, case_id, True)
    _refuse_textless(payload, "so it has no passages here")
    paras = payload.get("paragraphs") or []
    offset = args.get("offset") or 0
    limit = args.get("limit") or PASSAGE_LIMIT
    around = args.get("around")
    if not isinstance(offset, int) or offset < 0:
        raise ToolError("invalid_argument", "`offset` must be a non-negative integer.")
    if not isinstance(limit, int) or not 1 <= limit <= 400:
        raise ToolError("invalid_argument", "`limit` must be an integer 1..400.")
    # `isinstance(True, int)` is True in Python, so a JSON boolean would otherwise
    # be stringified to 'True', match no Randnummer, and come back as a confident
    # "this decision prints no Randnummer True".
    if around is not None and (isinstance(around, bool)
                               or not isinstance(around, (int, str))):
        raise ToolError("invalid_argument",
                        "`around` must be a Randnummer — the number the court "
                        "printed, as an integer or a string.")
    around_at = None
    if around is not None:
        if args.get("offset") is not None:
            raise ToolError("invalid_argument",
                            "`around` and `offset` address the same list two "
                            "different ways. Give one of them, not both.")
        want = str(around).strip()
        around_at = next((i for i, p in enumerate(paras)
                          if str(p.get("rn") or "").strip() == want), None)
        if around_at is None:
            # TWO DIFFERENT MISSES, and a model that reads them as one wastes a
            # turn. Some decisions print no Randnummern AT ALL, and "prints no
            # Randnummer 19" sends a model looking for the numbers it does print.
            # There are none, and only `offset` can work on that decision — so
            # say THAT, and say it before the retry.
            printed = [str(p["rn"]) for p in paras if p.get("rn") is not None]
            raise ToolError(
                "not_in_corpus",
                (f"This decision prints NO Randnummern at all, so `around` cannot "
                 f"address it — `rn` is null on every passage and "
                 f"`anchor_basis` is {payload.get('anchor_basis')!r}, which "
                 f"DESIGN.md §4.1a forbids inferring a number from. Page with "
                 f"`offset` instead; it has {len(paras)} passages."
                 if not printed else
                 f"This decision prints no Randnummer {want}. It prints "
                 f"{printed[0]}-{printed[-1]} over {len(paras)} passages; call "
                 f"again with one of those, or page with `offset`."),
                total=len(paras),
                prints_randnummern=bool(printed),
                rn_first=printed[0] if printed else None,
                rn_last=printed[-1] if printed else None,
                anchor_basis=payload.get("anchor_basis"))
        # Centred, then clamped to the ends — so `around` on the first or the last
        # Randnummer still returns a full window rather than half of one.
        offset = max(0, min(around_at - limit // 2, max(0, len(paras) - limit)))
    window = paras[offset:offset + limit]
    basis = payload.get("anchor_basis")
    return {
        "case": {"court": payload.get("court"), "decided": payload.get("decided"),
                 "aktenzeichen": payload.get("aktenzeichen"),
                 "doknr": payload.get("doknr"),
                 "ecli": payload.get("ecli"), "url": payload.get("url")},
        "leitsatz": payload.get("leitsatz"),
        "anchor_basis": basis,
        "total": len(paras),
        "offset": offset,
        "limit": limit,
        # Echoed so the window is auditable: `around` never moves the caller to a
        # different Randnummer silently, and `offset` says where the window it
        # produced actually starts.
        **({"around": str(around), "around_at_offset": around_at}
           if around_at is not None else {}),
        "paragraphs": window,
        **({"pagination": {
            "returned": len(window), "total": len(paras), "offset": offset,
            "limit": limit, "max_limit": 400,
            "next_offset": offset + limit if offset + limit < len(paras) else None,
            "note": ("This decision has more passages than one page. Page with "
                     "`offset`, widen with `limit` (max 400), or jump straight to "
                     "a Randnummer with `around`.")}}
           if len(paras) > len(window) else {}),
        "amtliche_seite": None,
        "note": ("DESIGN.md §4.1a: `rn` is the number the COURT printed, read out "
                 "of the decision's own markup — never inferred from position, "
                 "and null where the document prints none. `anchor_basis` is "
                 "derived per decision: only 'native_numbering' means the "
                 "anchor and the printed number provably coincide. "
                 "`amtliche_seite` is null everywhere — our texts carry no page "
                 "breaks."),
        "attribution": payload.get("attribution"),
        "source": payload.get("source_info"),
    }


#: How far back `getChanges` looks.
CHANGES_WINDOW_DAYS = 120


def t_getChanges(args: dict, backend: Backend) -> dict:
    """Recently changed provisions — the JSON twin of the site's Atom change
    feed, with the same window and the same caveat sentence."""
    limit = args.get("limit") or 50
    if not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ToolError("invalid_argument", "`limit` must be an integer 1..200.")
    since = args.get("since")
    if since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(since)):
        raise ToolError("invalid_argument", "`since` must be YYYY-MM-DD.")
    days = CHANGES_WINDOW_DAYS
    base = backend.base_url
    changes = []
    for r in backend.changes(since=since, limit=limit, days=days):
        seg = _quote(r["law_url_key"], safe="")
        changes.append({
            "law": r["law"], "ref": r["ref"], "heading": r["heading"],
            "law_title": r["law_title"],
            "jurisdiction": r["jurisdiction"],
            "observed": r["observed"],
            "first_seen_at": r["first_seen_at"],
            "amendment_note": r["amendment_note"],
            "url": f"{base}/gesetze/{seg}/{r['ref_norm']}",
            "change_id": f"{base}/gesetze/{seg}/{r['ref_norm']}#nv-{r['version_id']}",
        })
    return {
        "window_days": days,
        "since": str(since) if since else None,
        "changes": changes,
        "cursor": changes[-1]["observed"] if changes else None,
        "note": ("`observed` is the day the new text was FIRST SEEN here — not "
                 "necessarily the day it came into force. Poll with `since` set "
                 "to the newest `observed` you have already processed."),
        "atom_feed": f"{base}/feeds/gesetzesaenderungen.xml",
        "html": f"{base}/aenderungen",
    }


def t_getCoverage(args: dict, backend: Backend) -> dict:
    """What the corpus holds — and, in the same payload, what it does not.

    `outside_coverage` and `not_in_corpus` are different answers, which only
    helps a caller who can find out where the edges are. The corpus statistics
    are reshaped here, with the limits an agent cannot learn from totals alone.
    """
    try:
        s = backend.stats()
    except Unavailable as exc:
        raise ToolError("unavailable", f"Corpus statistics are not available "
                                       f"right now ({exc.detail}). This is an "
                                       f"outage, not an empty corpus.") from None
    base = backend.base_url
    return {
        "as_of": s.get("stand"),
        "built_at": s.get("erstellt"),
        "totals": {"laws": s.get("gesetze"), "norms": s.get("normen"),
                   "norm_versions": s.get("fassungen"),
                   "decisions": s.get("entscheidungen"),
                   "courts": s.get("gerichte"),
                   "decision_to_norm_edges": s.get("verweise_entscheidung_norm"),
                   "decision_to_decision_edges": s.get("verweise_entscheidung_entscheidung")},
        "sources": s.get("quellen") or [],
        "years": s.get("jahre") or [],
        "known_missing": s.get("vermisste_aktenzeichen"),
        "limits": [
            {"id": "version_archive_floor",
             "detail": (f"Stored norm versions begin {backend.archive_floor()}. Earlier "
                        "amendments are named in each law's Änderungsverlauf but "
                        "their TEXT is not held. A missing `as_of` version before "
                        "that date is an archive limit, never a statement about "
                        "the law.")},
            {"id": "citation_graph_is_federal",
             "detail": ("The decision-to-norm citation graph is built over "
                        "federal case law. A Land provision can report zero "
                        "citing decisions because it is not indexed, not because "
                        "no court cited it.")},
            {"id": "first_observed_is_not_inkrafttreten",
             "detail": ("Every norm version date is the day the text was first "
                        "observed here. It travels with `date_precision` "
                        "(day / week / launch); `launch` means the text predates "
                        "mirroring and the date is a floor.")},
            {"id": "source_windows_differ",
             "detail": ("Each entry in `sources` carries its own newest/last-seen "
                        "date. A decision outside a source's window is "
                        "`outside_coverage`, which is a different answer from "
                        "`not_in_corpus`.")},
        ],
        "landesrecht": ("Full text is held for Bayern, Brandenburg, "
                        "Nordrhein-Westfalen and Sachsen; see `sources` for the "
                        "per-channel counts."),
        "license": f"{base}/lizenz",
        "terms": f"{base}/nutzungsbedingungen",
        "source": backend.self_source(),
    }


# --------------------------------------------------------------------------
# The tool manifest
# --------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "resolveIdentifiers",
        "title": "Resolve legal citations",
        "handler": t_resolveIdentifiers,
        "cost_class": "cheap",
        "description": (
            "Ground a batch of German legal citations against the corpus. Call this "
            "BEFORE stating any citation you did not read here.\n\n"
            "Takes the citation in the form you already hold it — including the "
            "court name, the dispositive word and the date a model normally writes "
            "around a docket. KEEP THEM IN: the court and the date are used to "
            "disambiguate. An Aktenzeichen is unique per court, not nationwide, and "
            "21,021 dockets in this corpus are held by more than one decision, so "
            "'OLG Bamberg, 4 U 120/24' resolves to Bamberg's decision where the bare "
            "'4 U 120/24' is ambiguous or lands on another court's. Where the string "
            "has to be rewritten to be read, the rewrite is reported back under "
            "`normalised_from` / `normalised_to`, never silently, and "
            "`disambiguated_by` says when it was YOUR court or date that picked the "
            "decision out. Where the court you named writes a suffix your citation "
            "dropped ('4 U 120/24 e'), the answer carries `docket_completed` with "
            "the full Aktenzeichen — cite that one.\n\n"
            "Accepted kinds: norm citations ('§ 823 Abs. 1 BGB', '§§ 305-310 BGB', "
            "'Art. 83 DSGVO'), Aktenzeichen ('2 C 9.22', '8 AZR 26/18'), ECLI "
            "('ECLI:DE:BGH:2019:180619UVIIIZR247.18.0') and Fundstellen "
            "('BVerfGE 65, 1'). Full prose citations work: "
            "'BVerwG, Urteil vom 24.10.2023 - 2 C 9.22'.\n\n"
            "It never returns a near match. A miss comes back as `not_in_corpus` "
            "(we hold nothing and know of nothing), `attested` / `known_missing` "
            "(the decision provably EXISTS — decisions we do hold cite it by "
            "Aktenzeichen, and they are listed as the evidence — but we do not "
            "have its text), `ambiguous` (with candidates) or `unparseable`. "
            "`attested` is not a failure: you may state that the decision exists, "
            "cite it, and say the text was not available to you. What you must "
            "not do is treat it as `not_in_corpus`.\n\n"
            "A resolved norm carries `fundstelle`: the gazette citation of the "
            "authentic text, which is the citation a court accepts. Our own URL "
            "is a reading copy, and for Land law the gazette citation is the only "
            "source reference there is. Prefer it in anything you publish.\n\n"
            "`text` on a resolved norm is a 300-character stub unless you pass "
            "`include: [\"text\"]`, and `text_truncated` says which it is. Never "
            "verify a quotation against the stub: it is the head of the provision, "
            "not the Absatz you cited.\n\n"
            "When you supply a date or a court that does not match the decision "
            "the docket resolves to, the result carries `date_mismatch` / "
            "`court_mismatch` with the actual value. That is the hallucinated-"
            "citation case this tool exists for: cite what is actually there, not "
            "what you held — and a `court_mismatch` usually means this is not the "
            "decision you meant at all."),
        "schema": {
            "type": "object",
            "properties": {
                "citations": {
                    "type": "array", "minItems": 1, "maxItems": 100,
                    "items": {"type": "string", "maxLength": 400},
                    "description": "The citations, verbatim as you hold them.",
                    "examples": [["§ 622 Abs. 2 BGB", "BVerwG, Urteil vom 24.10.2023 - 2 C 9.22",
                                  "BVerfGE 65, 1", "Art. 83 DSGVO"]],
                },
                "include": {
                    "type": "array", "items": {"type": "string", "enum": ["text", "leitsatz"]},
                    "description": (
                        "Opt-in extra payload. 'text' returns a norm's FULL text "
                        "instead of the 300-character stub — the stub is the same "
                        "300 characters whichever Absatz you cited, so never verify "
                        "a quotation against it. 'leitsatz' returns a decision's "
                        "whole Leitsatz instead of its preview. An unknown value is "
                        "refused, not ignored."),
                },
            },
            "required": ["citations"],
        },
    },
    {
        "name": "search",
        "title": "Search German law",
        "handler": t_search,
        "cost_class": "expensive",
        "description": (
            "One query over BOTH corpora: federal and Land statutes (lexical, with "
            "concept pinning) and court decisions (semantic — natural-language "
            "questions work well here and are the better shape for case law).\n\n"
            "Search both unless you have a reason not to. A term of art often does "
            "not appear in the statute that governs it: 'Verzugspauschale' matches "
            "no provision (§ 288 BGB says 'Pauschale in Höhe von 40 Euro') while "
            "184 decisions use the word. scope='norms' alone will read as 'nothing "
            "here' in exactly those cases.\n\n"
            "CROSS-LAND COMPARISON: a single query returns the parallel provisions "
            "of the Bund and of every covered Land side by side, each row "
            "jurisdiction-labelled, plus a `by_jurisdiction` roll-up. Ask "
            "'Videoüberwachung öffentlich zugänglicher Räume' and you get BDSG § 4 "
            "next to the Land data-protection and police provisions. Full text is "
            "held for Bayern, Brandenburg, Nordrhein-Westfalen and Sachsen.\n\n"
            "Decision hits come back already anchored at the best-matching "
            "Randnummer (…#rd_51), so you can quote a paragraph rather than a "
            "document, AND carry `doknr` — the key `listCasePassages`, "
            "`listCitedAuthorities` and `listCitingDecisions` take, so you can go "
            "straight from a search hit to that decision's passages or its "
            "authorities without resolving anything first. Query in German; write "
            "raw umlauts, they are handled."),
        "schema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "minLength": 2, "maxLength": 2000,
                      "description": "German query. Keywords, a citation, or a full question.",
                      "examples": ["Kündigungsfrist Arbeitsverhältnis 10 Jahre",
                                   "Videoüberwachung öffentlich zugänglicher Räume",
                                   "Welche Rechte hat ein Arbeitnehmer bei verspäteter Lohnzahlung"]},
                "scope": {"type": "string", "enum": ["all", "norms", "cases"], "default": "all",
                          "description": "'all' (default) searches both. Narrow only "
                                         "when you know which corpus answers."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "include_repealed": {
                    "type": "boolean", "default": False,
                    "description": "Include repealed (aufgehobene) provisions. Off by "
                                   "default; turn it on when researching an older state "
                                   "of the law."},
            },
            "required": ["q"],
        },
    },
    {
        "name": "getNorm",
        "title": "Get statute text",
        "handler": t_getNorm,
        "cost_class": "cheap",
        "description": (
            "The text of one provision, by default as clean Markdown — about a "
            "tenth the size of the reader page for the same provision, with no "
            "navigation, no scripts and no boilerplate.\n\n"
            "`law` is the abbreviation as a citation writes it ('BGB', 'DSGVO', "
            "'BDSG 2018', 'RVG'); `ref` is the bare number, with any letter suffix "
            "and no § or Art. ('622', '823', '3a', '83'). Aliases resolve, and CASE "
            "IS READ: 'LwG' is the federal Landwirtschaftsgesetz while 'LWG' is a "
            "Land statute (Bayern's Landeswahlgesetz, NRW's Landeswassergesetz), so "
            "write the abbreviation the way your citation writes it. A spelling "
            "that matches no law exactly still resolves "
            "case-insensitively, and a miss lists the other laws the abbreviation "
            "names under `other_laws`, each with a `law_key` you can call again "
            "with.\n\n"
            "POINT IN TIME: `as_of=YYYY-MM-DD` returns the version stored for that "
            "date. Read `version_coverage` on every answer — the version archive "
            "begins 2019-06-10, and a date before that answers `outside_coverage` "
            "with the law's amendment register attached. That is a limit of our "
            "archive and says nothing about whether the provision existed.\n\n"
            "Every answer carries `first_observed`, `valid_to`, `date_precision` "
            "and `amendment_note`. `first_observed` is the day we first saw the "
            "text, NOT the legal Inkrafttreten — do not compute a deadline from it "
            "without reading `date_precision` (day / week / launch; 'launch' means "
            "the date is a floor).\n\n"
            "TRUST: `fundstelle` is the gazette citation of the authentic text — "
            "the citation a court accepts. `authoritative_source` names what our "
            "copy is (a consolidated, non-official reading version) and where the "
            "binding text lives. Quote the provision from `markdown`; the reader "
            "page at `url` carries per-Absatz anchors (#abs-N) if you want to "
            "deep-link a single Absatz."),
        "schema": {
            "type": "object",
            "properties": {
                "law": {"type": "string",
                        "description": ("Law abbreviation, or a `law_key` "
                                        "(`slug` in search results) when an "
                                        "abbreviation is ambiguous."),
                        "examples": ["BGB", "DSGVO", "RVG", "BDSG 2018", "SächsDSDG"]},
                "ref": {"type": "string",
                        "description": ("Provision number without § or Art. A "
                                        "sub-unit ('Abs. 1', 'lit. f') is dropped: "
                                        "the whole provision is returned."),
                        "examples": ["622", "823", "3a", "83", "28"]},
                "as_of": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$",
                          "description": "Return the version stored for this date."},
                "format": {"type": "string", "enum": ["markdown", "json"], "default": "markdown",
                           "description": "'markdown' (default, compact, quotable) or "
                                          "'json' for the structured payload."},
            },
            "required": ["law", "ref"],
        },
    },
    {
        "name": "listNormVersions",
        "title": "List statute versions",
        "handler": t_listNormVersions,
        "cost_class": "cheap",
        "description": (
            "Every stored version of one provision, newest first, so you can find "
            "out which dates `getNorm(as_of=…)` can actually answer before you ask.\n\n"
            "Each entry carries `first_observed` (the day the text was first seen "
            "here — NOT the Inkrafttreten), `valid_to`, `date_precision` and the "
            "law-level `amendment_note`. `at_archive_floor: true` marks the version "
            "that was current when mirroring began: its date is a floor, not an "
            "amendment, and earlier amendments exist that are named in the law's "
            "Änderungsverlauf (linked as `amendment_history_url`) but whose text is "
            "not held.\n\n"
            "There is no diff tool: fetch two versions with `getNorm(as_of=…)` and "
            "diff them yourself — a diff we computed would hide which side of it "
            "came from a floor date."),
        "schema": {
            "type": "object",
            "properties": {
                "law": {"type": "string", "examples": ["BGB", "DSGVO"]},
                "ref": {"type": "string", "examples": ["288", "622"]},
            },
            "required": ["law", "ref"],
        },
    },
    {
        "name": "listCitingDecisions",
        "title": "List citing decisions",
        "handler": t_listCitingDecisions,
        "cost_class": "medium",
        "description": (
            "Incoming citation edges. Give EITHER `law` + `ref` (which decisions "
            "apply this statute provision) OR `case` (which decisions cite this "
            "decision) — exactly one of the two.\n\n"
            "Results are ranked by citation weight, then court tier, then recency. "
            "Read the ranking honestly: for a provision with many EU decisions the "
            "first ten can be almost all CJEU, and the German courts appear only "
            "further down. If `total` exceeds what you read, page on with "
            "`offset` (`pagination.next_offset`) before concluding anything about "
            "national case law.\n\n"
            "NEWEST FIRST: pass `sort: \"recent\"` when the question is about "
            "current case law ('fünf aktuelle Entscheidungen zu …'). Paging works "
            "the same way. This ordering is bounded — for a handful of procedural "
            "giants (§ 154 VwGO, § 708 ZPO) it cannot be computed inside the query "
            "budget, and then the answer comes back in WEIGHT order and says so in "
            "`sort_applied` and `sort_note`. Check `sort_applied` before you "
            "describe a list as the most recent decisions.\n\n"
            "For a decision, each citer carries `citing_rn`: the Randnummer of the "
            "CITING decision's own text that holds the citation, as that court "
            "numbered it, and the URL is anchored to it.\n\n"
            "COVERAGE: the graph is built over federal case law. A Land provision "
            "can answer `total: 0` because it is not indexed, not because no court "
            "has cited it — `coverage.complete_for_this_norm` tells you which, and "
            "for a Land provision you should fall back to `search` on the "
            "provision's wording."),
        "schema": {
            "type": "object",
            "properties": {
                "law": {"type": "string", "examples": ["DSGVO", "RVG", "BGB"]},
                "ref": {"type": "string", "examples": ["83", "3a", "288"]},
                "case": {"type": "string",
                         "description": ("A juris doknr, an ECLI, or this site's "
                                         "decision URL as `search` returns it."),
                         "examples": ["ECLI:DE:BAG:2018:250918.U.8AZR26.18.0"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0,
                           "description": "Rows to skip, for reading past the first page."},
                "sort": {"type": "string", "enum": ["weight", "recent"],
                         "default": "weight",
                         "description": ("'weight' (default) is citation weight, then "
                                         "court tier, then recency. 'recent' is newest "
                                         "decision first; where it cannot be computed "
                                         "the answer falls back to 'weight' and says "
                                         "so in `sort_applied`.")},
            },
        },
    },
    {
        "name": "listCitedAuthorities",
        "title": "List cited authorities",
        "handler": t_listCitedAuthorities,
        "cost_class": "medium",
        "description": (
            "Outgoing citation edges of one decision: the statute provisions it "
            "cites (with how often it cites each — that is the Normenkette, "
            "weighted) and the decisions it relies on.\n\n"
            "`treatment` is null on every edge and stays null. Classifying an edge "
            "as gefolgt / abgegrenzt / aufgegeben is unbuilt work, and a wrong "
            "'aufgegeben' in a brief is worse than no label at all. Read the citing "
            "passage yourself with `listCasePassages`.\n\n"
            "A decision we can prove exists but do not hold answers "
            "`known_missing`, with the decisions that attest it — not a 404."),
        "schema": {
            "type": "object",
            "properties": {
                "case": {"type": "string",
                         "description": ("A juris doknr, an ECLI, or this site's "
                                         "decision URL as `search` returns it.")},
            },
            "required": ["case"],
        },
    },
    {
        "name": "listCasePassages",
        "title": "List case passages",
        "handler": t_listCasePassages,
        "cost_class": "cheap",
        "description": (
            "The full text of one decision, split into its paragraphs, each with a "
            "permalink you can cite.\n\n"
            "`rn` is the Randnummer the COURT printed, read out of the decision's "
            "own markup. It is never inferred from position: where a document "
            "prints no numbers, `rn` is null and stays null. `anchor_basis` is "
            "derived per decision — only 'native_numbering' means our anchor and "
            "the printed number provably coincide, so pin-cite a Randnummer only "
            "when you see that value.\n\n"
            "`amtliche_seite` is null everywhere: our texts carry no page breaks, "
            "so a BVerfGE-style page pin cannot be produced honestly.\n\n"
            "SIZE. Long decisions run to several hundred paragraphs, so the "
            "default page is 30. Three ways to move: `offset` pages, `limit` "
            "widens (max 400 — enough for a whole decision when you really want "
            "it), and `around` jumps. `pagination` appears whenever there is more "
            "than the page you were handed.\n\n"
            "`around: 51` returns a window of `limit` passages CENTRED on "
            "Randnummer 51 — the right call when `listCitingDecisions` gave you a "
            "`citing_rn`, when a search hit came back anchored at …#rd_51, or when "
            "you want the passage around a pin cite and not the whole judgment. It "
            "takes the number the court printed, not a position, and a decision "
            "that prints no such number answers `not_in_corpus` rather than "
            "silently handing you a different passage. `around` and `offset` "
            "address the same list two different ways; give one."),
        "schema": {
            "type": "object",
            "properties": {
                "case": {"type": "string",
                         "description": ("A juris doknr, an ECLI, or this site's "
                                         "decision URL as `search` returns it.")},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 400, "default": 30},
                "around": {"type": ["integer", "string"],
                           "description": ("A Randnummer as the court printed it. "
                                           "Returns a window of `limit` passages "
                                           "centred on it. Not combinable with "
                                           "`offset`."),
                           "examples": [51, "12"]},
            },
            "required": ["case"],
        },
    },
    {
        "name": "getChanges",
        "title": "Recent changes",
        "handler": t_getChanges,
        "cost_class": "cheap",
        "description": (
            "Which provisions got a new text recently, newest first — the freshness "
            "feed, as JSON. Poll it with `since` set to the newest `observed` you "
            "have already processed.\n\n"
            "`observed` is the day the new text was FIRST SEEN here, which is not "
            "necessarily the day it came into force. Say so if you report a date. "
            "The window is the last 120 days; the law's own Änderungsverlauf goes "
            "further back."),
        "schema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$",
                          "description": "Only changes observed on or after this date."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
        },
    },
    {
        "name": "getCoverage",
        "title": "Corpus coverage",
        "handler": t_getCoverage,
        "cost_class": "cheap",
        "description": (
            "Corpus scope with its holes stated. Call this once when your answer "
            "depends on whether an absence is real.\n\n"
            "Returns totals (laws, provisions, versions, decisions, courts, "
            "citation edges), the per-source windows, the count of Aktenzeichen we "
            "can prove exist and do not hold, and `limits`: the version-archive "
            "floor, the federal scope of the citation graph, what a version date "
            "actually means, and why source windows differ.\n\n"
            "Use it to tell `outside_coverage` from `not_in_corpus`. They are "
            "different answers and this API never collapses them."),
        "schema": {"type": "object", "properties": {}},
    },
]

_BY_NAME = {t["name"]: t for t in TOOLS}


def tool_list() -> list[dict]:
    """The `tools/list` payload — the wire shape, without our handler."""
    return [{
        "name": t["name"],
        "title": t["title"],
        "description": t["description"],
        "inputSchema": t["schema"],
        "annotations": {"title": t["title"], "readOnlyHint": True,
                        "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": False},
        "_meta": {"nulegal/costClass": t["cost_class"]},
    } for t in TOOLS]


def call_tool(name: str, args: dict, backend: Backend) -> dict:
    """Run one tool. Raises ToolError, or KeyError for an unknown tool; every
    other exception is the caller's to report as `unavailable`."""
    tool = _BY_NAME.get(name)
    if tool is None:
        raise KeyError(name)
    return tool["handler"](args if isinstance(args, dict) else {}, backend)
