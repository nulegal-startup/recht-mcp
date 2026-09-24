# SPDX-License-Identifier: AGPL-3.0-only
"""A self-contained citation grammar for backends that have none of their own.

The tool layer asks its backend three grammar questions (see
`recht_mcp.backend.Backend`): does a whole string read as a decision citation,
is a string a court, and what is the docket key of an Aktenzeichen. The hosted
service answers them with the resolver's own grammar, which leans on its
registry of court names. The HTTP backend cannot reach that registry, so it
answers them here, from the shape of a citation alone.

This grammar is deliberately CONSERVATIVE. Its only job is to decide what
string the tool layer sends to the resolve endpoint, and the resolve endpoint
parses the string again with the full grammar. Declining here costs little —
the tool layer then rebuilds or strips the citation, and says so in
`normalised_to` — while accepting something the server would refuse costs a
round trip. So a head counts as a court only when it opens with a court
abbreviation or a word that names a court.
"""
from __future__ import annotations

import datetime
import re

from .citations import COURT_TYPES, DOCKET_IN, FEDERAL_COURTS

__all__ = ["parse_prose_citation", "is_court_head", "docket_key"]

#: Court abbreviations as citations write them, lower-cased, dots removed.
COURT_ABBREVIATIONS = frozenset(COURT_TYPES) | frozenset(FEDERAL_COURTS) | {
    "eug", "egmr", "bayobl g", "bayoblg", "bayvgh", "bayverfgh", "verfgh",
    "vgh", "lsg", "larbg", "sozg", "stgh", "bgh-gs", "gmsogb", "rg", "bverfge",
}

#: A spelled-out court name contains one of these.
_COURT_WORD = re.compile(r"(?i)(gericht|gerichtshof|kammergericht|hof)\b")

#: The label a docket is pasted behind: 'Az. 4 OH 3/22', 'Aktenzeichen: 1 BvL 5/21'.
_AZ_LABEL = re.compile(r"^(?:az\.?|aktenzeichen)\s*[:.]?\s+", re.I)

#: 'Urteil vom', 'Beschl. v.', 'Versäumnisurteil vom' — one word and its 'vom'.
_DOKTYP_VOM = (r"(?:[A-Za-zÄÖÜäöüß]{3,28}\.?\s*"
               r"(?:vom|v\.|vom\.)\s*)")
#: 24.10.2023 / 5.3.2019. Two-digit years are not read: '24.10.23' is as likely
#: to be a docket fragment as a date.
_CITE_DATE = r"(?P<date>\d{1,2}\.\s*\d{1,2}\.\s*\d{4})"
#: A comma, a hyphen, a Unicode dash, or plain space.
_CITE_SEP = r"(?:\s*[,‐-―\-]\s*|\s+)"

_PROSE_DATED = re.compile(
    rf"^(?P<court>[^,]{{2,60}}?){_CITE_SEP}{_DOKTYP_VOM}?{_CITE_DATE}"
    rf"{_CITE_SEP}(?P<docket>\S.{{0,80}})$", re.I)
_PROSE_UNDATED = re.compile(
    rf"^(?P<court>[^,]{{2,60}}?)\s*,\s*{_DOKTYP_VOM}?(?P<docket>\S.{{0,80}})$", re.I)


def is_court_head(text: str) -> bool:
    head = (text or "").strip(" .,")
    if not head:
        return False
    first = head.split()[0].replace(".", "").lower()
    if first in COURT_ABBREVIATIONS:
        return True
    return bool(_COURT_WORD.search(head.split()[0]))


def docket_key(text: str) -> str | None:
    s = " ".join(_AZ_LABEL.sub("", (text or "").strip(), count=1).split())
    m = DOCKET_IN.fullmatch(s)
    return " ".join(m.group(1).split()) if m else None


def _cite_date(raw: str | None) -> str | None:
    if not raw:
        return None
    d, m, y = (p.strip() for p in raw.split("."))
    try:
        return datetime.date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def parse_prose_citation(text: str) -> dict | None:
    s = " ".join((text or "").split())
    if not s or len(s) > 200:
        return None
    for pattern in (_PROSE_DATED, _PROSE_UNDATED):
        m = pattern.match(s)
        if not m:
            continue
        court = m.group("court").strip(" .,")
        if not is_court_head(court):
            continue
        az = docket_key(m.group("docket").strip())
        if not az:
            continue
        return {"court": court, "date": _cite_date(m.groupdict().get("date")),
                "docket": az}
    return None
