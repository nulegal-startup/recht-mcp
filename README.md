# recht-mcp

[![smithery badge](https://smithery.ai/badge/nulegal/recht)](https://smithery.ai/servers/nulegal/recht)

An MCP server for German law: federal statutes and regulations, the Landesrecht
of Bayern, Brandenburg, Nordrhein-Westfalen and Sachsen, and court decisions —
for AI agents that need to ground a citation, read a provision, or follow the
citation graph, and need to know when an answer is *not* complete.

This repository is the tool layer of the hosted server at
**`https://recht.nulegal.eu/v1/mcp`**. The hosted endpoint runs this code,
vendored at a pinned commit, over a private backend that reads the corpus
directly; `recht-mcp` runs the same code locally over the public REST API. The
corpus and the database stay on the service side — see
[docs/DESIGN.md](docs/DESIGN.md) for exactly where the line runs.

## The nine tools

| Tool | What it does |
|---|---|
| `resolveIdentifiers` | Grounds up to 100 citations at once — `§ 823 Abs. 1 BGB`, `BVerwG, Urteil vom 24.10.2023 - 2 C 9.22`, ECLIs, `BVerfGE 65, 1`. Keeps the court and the date as disambiguators, flags a wrong date or a different court, and never returns a near match. |
| `search` | One query over statutes (lexical) and case law (semantic), with a per-jurisdiction roll-up for cross-Land comparison. |
| `getNorm` | The text of one provision as Markdown (or JSON), optionally as of a date, with its gazette citation (`fundstelle`). |
| `listNormVersions` | Every stored version of a provision, with the archive floor marked. |
| `listCitingDecisions` | Decisions citing a provision or a decision, by citation weight or newest first, with the citing court's own Randnummer. |
| `listCitedAuthorities` | What one decision cites: provisions (the weighted Normenkette) and decisions. |
| `listCasePassages` | A decision as numbered passages; page through it or jump to a Randnummer. |
| `getChanges` | Provisions whose text changed recently — the freshness feed as JSON. |
| `getCoverage` | What the corpus holds, and the limits every answer should be read against. |

Every tool is read-only. Each answer says what it is *and* what it is not:
version dates are first-observed dates, not Inkrafttreten; the norm-version
archive has a floor that every norm answer states; a zero from the citation
graph on a Land provision is labelled as "not indexed", not "never cited"; and
three kinds of miss — `not_in_corpus`, `outside_coverage`, `known_missing` —
are never collapsed into one. The full contract of each tool is in its
description (`tools/list`).

## Use the hosted server (recommended)

No key, no signup. Point any MCP client that speaks Streamable HTTP at:

    https://recht.nulegal.eu/v1/mcp

Claude Code:

    claude mcp add --transport http nulegal-recht https://recht.nulegal.eu/v1/mcp

A client configured with JSON:

```json
{
  "mcpServers": {
    "nulegal-recht": { "type": "http", "url": "https://recht.nulegal.eu/v1/mcp" }
  }
}
```

Developer documentation for the service and its REST API:
<https://recht.nulegal.eu/developers>.

## Run it locally

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). The package has no
third-party runtime dependencies.

    git clone https://github.com/nulegal-startup/recht-mcp
    cd recht-mcp
    uv run recht-mcp            # stdio
    uv run recht-mcp --http     # Streamable HTTP on http://127.0.0.1:8765/mcp

Or without a checkout:

    uvx --from git+https://github.com/nulegal-startup/recht-mcp recht-mcp

A client that launches stdio servers:

```json
{
  "mcpServers": {
    "nulegal-recht": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/nulegal-startup/recht-mcp", "recht-mcp"]
    }
  }
}
```

Options: `--http`, `--host` (default `127.0.0.1`), `--port` (default `8765`),
`--path` (default `/mcp`), `--base-url` (or `RECHT_MCP_BASE_URL`; default
`https://recht.nulegal.eu`), `--timeout`, `--log-level`. The local HTTP server
binds to loopback and refuses browser requests from non-loopback origins.

The local server reads the same data the hosted one does, through the public
REST API, and gives the same answers with two documented exceptions (a
simplified citation grammar, and no `other_laws` hint on a provision miss) —
see [docs/DESIGN.md](docs/DESIGN.md#local-vs-hosted).

## Development

    uv sync
    uv run pytest -q                                   # unit tests, offline
    RECHT_MCP_LIVE=1 uv run pytest -q tests/test_live.py   # against recht.nulegal.eu

The live tests call every tool through the local server and compare several
answers with the hosted endpoint's.

## Data

The legal corpus served by the hosted endpoint — its structure and selection,
the links between provisions and decisions, the version history — is licensed
separately from this code, under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Attribution is
required: „Quelle: nu:legal – recht.nulegal.eu“. The terms, including what is
reserved, are at <https://recht.nulegal.eu/lizenz> and
<https://recht.nulegal.eu/nutzungsbedingungen>.

The texts are non-official reading copies. The binding text of a statute is
the one in its official gazette; each norm answer names it (`fundstelle`,
`authoritative_source`).

## Contributing and security

Issues are welcome; outside pull requests are not accepted yet — see
[CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities privately as
described in [SECURITY.md](SECURITY.md).

## License

The code in this repository is licensed under the GNU Affero General Public
License, version 3 only (`AGPL-3.0-only`) — see [LICENSE](LICENSE).

The legal corpus served by the hosted endpoint is licensed separately, under
CC BY 4.0 with attribution required (see [Data](#data)).

The nu:legal name and logo are not licensed.
