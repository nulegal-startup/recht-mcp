# Design: one tool layer, two backends

## Goal

"The code you read is the code we run." The hosted MCP endpoint at
`https://recht.nulegal.eu/v1/mcp` and the `recht-mcp` command in this
repository execute the same tool layer. What differs is only where the data
comes from. The database and the corpus stay private; everything that decides
what a tool *says* is here.

```
                         ┌──────────────────────────────────────────┐
  MCP client ── JSON-RPC │ recht_mcp.protocol   (JSON-RPC / MCP)     │
                         │ recht_mcp.tools      (9 tools: validation,│  this repository
                         │ recht_mcp.citations   shaping, honesty)   │
                         └───────────────┬──────────────────────────┘
                                         │ Backend protocol (recht_mcp.backend)
                   ┌─────────────────────┴─────────────────────┐
         HTTPBackend (this repository)              the hosted backend (private)
         reads https://recht.nulegal.eu/v1          reads the corpus in process
```

## What is in this repository

| Module | Contents |
|---|---|
| `tools.py` | The nine tool definitions (name, title, description, input schema, cost class), argument validation, answer shaping, the honesty blocks (`version_coverage`, citation-graph `coverage`, pagination and sort notes), the closed `reason` vocabulary (`REASONS`) and `ToolError`. |
| `citations.py` | Reading a decision citation the way a model writes it: the docket scan, the court/date extraction, the canonical rebuild with its two guards, the seat-aware `court_mismatch`. |
| `protocol.py` | JSON-RPC 2.0 and MCP: `initialize` and version negotiation, `tools/list`, `tools/call`, notifications, the tool-result envelope, and `http_exchange` (one Streamable-HTTP POST: size limit, parse errors, batches, 202). |
| `backend.py` | The `Backend` protocol — every data access a tool makes — plus `NotFound` / `Unavailable` and a JSON encoder. |
| `http_backend.py` | `HTTPBackend`: the protocol over the public REST API, standard library only. |
| `grammar.py` | A self-contained citation grammar for `HTTPBackend` (see *Local vs hosted*). |
| `transports.py`, `cli.py` | stdio and Streamable-HTTP servers; the `recht-mcp` command. |

Tool descriptions and the server `instructions` are part of the contract and
live here verbatim: a client sees exactly these strings from the hosted
endpoint.

## The seam

A tool never fetches. It calls one of these `Backend` methods:

| Method | Used by | Hosted backend | `HTTPBackend` route |
|---|---|---|---|
| `resolve` | resolveIdentifiers | the resolve handler, in process | `POST /v1/resolve` |
| `parse_prose_citation`, `is_court_head`, `docket_key` | resolveIdentifiers | the resolver's own grammar | local (`grammar.py`) |
| `find_law` | getNorm, listNormVersions, listCitingDecisions, resolveIdentifiers (`fundstelle`) | law lookup by alias/key | `GET /v1/law/{law}` |
| `is_land`, `law_url_key` | getNorm, listNormVersions, listCitingDecisions | law metadata | from the `/v1/law` answer |
| `find_norm`, `norm_payload`, `norm_url` | getNorm, listNormVersions | norm lookup, as-of resolution, payload builder | `GET /v1/norm/{law}/{ref}[?asof=]` |
| `norm_markdown` | getNorm | the Markdown download, in process | `GET /gesetze/{law}/{ref}/download.md` |
| `norm_versions` | listNormVersions | version list | `GET /v1/norm/{law}/{ref}/versions` ¹ |
| `search_norms`, `search_cases` | search | the two search handlers | `GET /v1/search`, `GET /v1/case-search` |
| `citing_decisions_for_norm` | listCitingDecisions | citing-cases handler | `GET /v1/norm/{law}/{ref}/citing-cases` |
| `case_key` | the three case tools | reader-page URL → document number | `GET /v1/case-key` ¹ |
| `citing_decisions_for_case` | listCitingDecisions | citing decisions of a decision | `GET /v1/case-citations/citing` ¹ |
| `get_case` | listCitedAuthorities, listCasePassages | decision handler | `GET /v1/case?doknr=` |
| `cited_decisions` | listCitedAuthorities | decisions a decision cites | `GET /v1/case-citations/cited` ¹ |
| `changes` | getChanges | the change feed's rows | `GET /v1/changes` ¹ |
| `stats` | getCoverage | corpus statistics | `GET /v1/stats` |
| `archive_floor`, `self_source`, `jsonable` | several | service constants, encoder | defaults in `BaseBackend` |

¹ Added for this server. The data behind four tools was published only as HTML
or as a feed, or not at all; each of these five read-only routes is a thin
wrapper over the same hosted-backend method the hosted tool calls, so the two
paths cannot disagree about what the data is.

Records crossing the seam are plain dicts whose keys are listed in
`backend.py`. The tool layer reads only those keys.

### Errors across the seam

| Backend raises | Tool answers |
|---|---|
| `NotFound` on a lookup | `not_in_corpus` (the object is not held) |
| `Unavailable` from `stats` | `unavailable`, "an outage, not an empty corpus" |
| anything else | `unavailable`, "unknown, not negative — do not report an absence" |

`HTTPBackend` maps HTTP 429, 5xx and network failures to `Unavailable`, never
to `NotFound`: an overloaded server must never read as "this does not exist".

## How the hosted service runs this code

The service copies `src/recht_mcp/` at a pinned commit of this repository into
its own code base and records the commit and the git blob id of every file.
Its test suite recomputes those ids, so the hosted copy cannot drift from a
commit you can check out here.

The hosted module that remains private holds only the data access behind the
backend methods and the HTTP route that calls `protocol.http_exchange`.

### Import map of the hosted module

Before the split, the hosted MCP module reached into the rest of the service
directly. Each of those dependencies now lands in one of three places:

| Dependency of the hosted module | What it provided | Now |
|---|---|---|
| service configuration — public site URL | the base of every link | public: `Backend.base_url` |
| service configuration — query filters | parameters of the backend's queries | private: inside the backend |
| database access | queries | private: behind `norm_versions`, `citing_decisions_for_case`, `cited_decisions`, `changes`, `case_key` |
| the `/v1` handlers — resolve, search, case search, law and norm lookup, norm payload, page URL, citing cases, decision payload, statistics, source record | the REST API, called in process | backend methods (`resolve`, `search_*`, `find_law`, `find_norm`, `norm_payload`, `norm_url`, `citing_decisions_for_norm`, `get_case`, `stats`, `self_source`) |
| the reader-page renderers — Markdown download, law URL key | the same pages a browser gets | backend methods `norm_markdown`, `law_url_key` |
| the citation resolver — prose-citation parser, court-head test, docket key | the resolver's grammar | backend methods `parse_prose_citation`, `is_court_head`, `docket_key` |
| court → URL segment | decision URLs | private: the backend returns finished URLs |
| federal or Land | the citation-graph coverage note | backend method `is_land` |
| Land display abbreviations | the change feed's `law` field | private: inside `changes` |
| archive constants | `version_coverage` | backend method `archive_floor` |
| the web framework's JSON encoder | dates in handler rows | backend method `jsonable` (the public default mirrors it) |
| the web framework's routing | the HTTP route | private: the route calls `protocol.http_exchange` |

Everything else in the old module — the tool definitions and descriptions,
validation, citation normalisation, answer shaping, the reason vocabulary and
the JSON-RPC handling — moved here unchanged. The hosted server's responses
were compared before and after it moved onto this package, and were
identical.

## Local vs hosted

Run against the same corpus, `recht-mcp` gives the hosted server's answers
with two known differences:

1. **Citation grammar.** The hosted backend decides whether a citation such as
   `OLG Köln, Urteil vom 1.2.2024 - 6 U 1/23` is a whole citation with the
   resolver's own grammar, which uses the service's registry of court names.
   `HTTPBackend` uses `grammar.py`, which recognises a court from its
   abbreviation or from a court word in its name. The resolve endpoint parses
   the string again with the full grammar either way, so the practical effect
   is limited to how a citation is rewritten before it is sent (always
   reported in `normalised_to`). In our comparison both produced identical
   answers.
2. **`other_laws`.** When a provision is missing from the law an abbreviation
   picked, the hosted answer lists the other laws with the same abbreviation.
   `GET /v1/law/{law}` names only the picked law, so the local answer omits
   the hint (the `not_in_corpus` answer itself is the same).

Answers are also subject to the REST API's own availability: a request the
API declines or cannot serve is reported as `unavailable`.

## Transport

Streamable HTTP, stateless, JSON responses. One POST carries one JSON-RPC
message (or a batch) and receives the response in the same HTTP response. There
is no session id and no server-initiated stream, so `GET` and `DELETE` answer
405 — the transport specification makes the SSE leg optional and prescribes
exactly that. Every operation is a read-only lookup that completes in one
response, so a session would add nothing.

## Two conventions the answers cite

* `rn` is the Randnummer the court printed, read from the decision's own
  markup — never inferred from a passage's position — and `anchor_basis` says
  whether our anchor and the printed number provably coincide. Some answers
  cite this rule by its section number in the service's own specification,
  "DESIGN.md §4.1a".
* A norm version date is the day the text was first observed
  (`first_observed`), never a statement about when it came into force, and it
  travels with its `date_precision`.

## Risks, and what guards them

| Risk | Guard |
|---|---|
| The hosted copy drifts from this repository. | Pinned commit plus per-file blob ids, recomputed by the service's test suite. |
| A future change publishes private detail (data layout, operational detail, internal references). | The backend seam keeps queries out of this package by construction; `tests/test_repo_hygiene.py` fails on SQL, issue-tracker keys and e-mail addresses; a review of the whole tree before publication. |
| The local server and the hosted one diverge silently. | The opt-in live tests compare local and hosted answers for the same calls. |
| A REST route the backend reads changes shape. | The service tests the routes added for this server against `HTTPBackend` itself. |
| Local HTTP server reachable from a web page (DNS rebinding). | Binds to loopback by default; refuses a non-loopback `Origin`. |
| A client relies on the answer shape. | `SERVER_VERSION` is bumped when the tool contract changes, not when the corpus does. |
