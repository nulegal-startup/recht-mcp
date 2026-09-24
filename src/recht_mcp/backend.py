# SPDX-License-Identifier: AGPL-3.0-only
"""The data seam: every read a tool makes goes through one `Backend`.

The tool layer in this package decides WHAT to ask and HOW to present the
answer — argument validation, identifier parsing, disambiguation, the honesty
blocks, the error vocabulary. A backend only fetches. Two implementations
exist:

  * `recht_mcp.http_backend.HTTPBackend` (in this repository) reads the public
    REST API at https://recht.nulegal.eu/v1, so anyone can run the server
    locally against the hosted corpus.
  * The hosted service at https://recht.nulegal.eu/v1/mcp runs this same tool
    layer over a private backend that reads the corpus directly.

The contract below is the whole interface between the two halves. Records are
plain dicts; the keys each method returns are listed in its docstring, and
nothing outside those keys is read by the tool layer. A backend may carry more
keys (the hosted one does, for its own bookkeeping) — the tool layer never
publishes a record wholesale unless the docstring says it is a published
payload.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

__all__ = ["Backend", "BaseBackend", "NotFound", "Unavailable", "to_jsonable"]


class NotFound(Exception):
    """The backend holds no such object. `detail` is echoed to the caller."""

    def __init__(self, detail: Any = None):
        super().__init__(str(detail))
        self.detail = detail


class Unavailable(Exception):
    """A dependency was not reachable. The answer is unknown, not negative."""

    def __init__(self, detail: Any = None):
        super().__init__(str(detail))
        self.detail = detail


@runtime_checkable
class Backend(Protocol):
    """Every data access the nine tools make."""

    #: The public site every URL in an answer points at, without a trailing slash.
    base_url: str

    # ------------------------------------------------------------ housekeeping
    def archive_floor(self) -> str:
        """The first date (YYYY-MM-DD) the norm-version archive holds text for."""

    def jsonable(self, payload: Any) -> Any:
        """`payload` with every value converted to a JSON-native type."""

    def self_source(self) -> dict:
        """The `source` block naming the service: `{name, operator, terms, url}`."""

    # ------------------------------------------------------- citation grammar
    def parse_prose_citation(self, text: str) -> dict | None:
        """A whole decision citation ('OLG Köln, Urteil vom 1.2.2024 - 6 U 1/23')
        as `{court, date, docket}`, else None. `date` is YYYY-MM-DD or None."""

    def is_court_head(self, text: str) -> bool:
        """Is `text` the name or abbreviation of a German court?"""

    def docket_key(self, text: str) -> str | None:
        """`text` read as an Aktenzeichen, in the form the resolver keys on, or None."""

    # ----------------------------------------------------------------- resolve
    def resolve(self, citations: list[str], include: list[str]) -> list:
        """One result row per citation, in order — the published resolve payload."""

    # ---------------------------------------------------------- laws and norms
    def find_law(self, abbrev: str) -> tuple[dict | None, list[dict]]:
        """`(law, laws)`. `law` is None when `abbrev` names no law or several;
        `laws` lists every law the abbreviation names. A law record carries
        `id` (opaque), `slug`, `jurabk`, `title`, `status` and `fundstelle`."""

    def is_land(self, law: dict) -> bool:
        """True when `law` is Land law rather than federal law."""

    def law_url_key(self, law: dict) -> str:
        """The unquoted path segment of the law's reader page (`/gesetze/{key}`)."""

    def find_norm(self, law: dict, ref: str, as_of: str | None) -> tuple[dict | None, Any]:
        """`(norm, version)`. `norm` is None when the law has no such provision;
        `version` is None when `as_of` was given and no stored version covers
        it. A norm record carries `ref`, `ref_norm` and `heading`; `version` is
        opaque and only ever handed back to `norm_payload`."""

    def norm_payload(self, law: dict, norm: dict, version: Any) -> dict:
        """The published JSON payload of one provision."""

    def norm_url(self, law: dict, norm: dict) -> str | None:
        """The provision's reader page."""

    def norm_markdown(self, law: dict, norm: dict, ref: str, as_of: str | None) -> str | None:
        """The provision as Markdown, or None when there is no renderable text.
        Never raises: a failed render is None."""

    def norm_versions(self, law: dict, norm: dict) -> list[dict]:
        """Stored versions, newest first, at most 200. Each carries
        `first_observed`, `valid_to`, `date_precision`, `amendment_note`."""

    # ------------------------------------------------------------------ search
    def search_norms(self, query: str, *, limit: int, include_repealed: bool) -> dict:
        """The published statute-search payload (`results`, optionally
        `corrected` and `law_title_match`)."""

    def search_cases(self, query: str, *, limit: int) -> dict | list:
        """The published case-law search payload."""

    # ---------------------------------------------------------- citation graph
    def citing_decisions_for_norm(self, abbrev: str, ref: str, *, limit: int,
                                  offset: int, sort: str) -> dict:
        """The published citing-decisions payload for one provision. Raises
        `NotFound` when the provision is not held."""

    def case_key(self, case_id: str) -> str:
        """A decision key the case lookups accept, from what a caller holds:
        a reader-page URL is read back to its document number, anything else
        passes unchanged."""

    def citing_decisions_for_case(self, key: str, *, limit: int, offset: int,
                                  sort: str) -> dict | None:
        """Decisions citing one decision, or None when it is not held.
        `{case: {court, decided, aktenzeichen, cited_by}, rows, served}`, where
        `served` is the ordering actually applied ('weight' or 'recent') and each
        row carries `court`, `decided`, `aktenzeichen`, `doknr`, `ecli`,
        `leitsatz`, `rn` (the citing decision's own Randnummer, or None) and
        `url` (the citing decision's reader page)."""

    def get_case(self, key: str, *, paragraphs: bool) -> dict:
        """The published payload of one decision. Raises `NotFound`."""

    def cited_decisions(self, doknr: str | None) -> list[dict]:
        """Decisions one decision cites, most-cited first, at most 50. Each
        carries `court`, `decided`, `aktenzeichen`, `doknr`, `ecli`, `n` and
        `url`."""

    # -------------------------------------------------------- feed and coverage
    def changes(self, *, since: str | None, limit: int, days: int) -> list[dict]:
        """Provisions whose text changed in the last `days` days, newest first.
        Each carries `law`, `ref`, `heading`, `law_title`, `jurisdiction`,
        `observed`, `first_seen_at`, `amendment_note`, `law_url_key`,
        `ref_norm` and `version_id`."""

    def stats(self) -> dict:
        """The published corpus statistics. Raises `Unavailable`."""


def to_jsonable(obj: Any) -> Any:
    """Convert `obj` to JSON-native types, for the types a backend can return:
    ISO dates, stringified UUIDs, enum values, lists for tuples and sets,
    numbers for Decimals."""
    if obj is None or isinstance(obj, (str, bool, int, float)):
        return obj
    if isinstance(obj, dict):
        return {to_jsonable(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return to_jsonable(obj.value)
    if isinstance(obj, Decimal):
        return int(obj) if obj.as_tuple().exponent >= 0 else float(obj)
    if isinstance(obj, UUID):
        return str(obj)
    return str(obj)


class BaseBackend:
    """Defaults a backend may inherit. Subclasses implement the rest."""

    base_url = "https://recht.nulegal.eu"

    def archive_floor(self) -> str:
        return "2019-06-10"

    def jsonable(self, payload: Any) -> Any:
        return to_jsonable(payload)

    def self_source(self) -> dict:
        return {"name": "nu:legal Deutsches Recht", "operator": "Nulegal GmbH",
                "terms": f"{self.base_url}/nutzungsbedingungen",
                "url": self.base_url}
