# SPDX-License-Identifier: AGPL-3.0-only
"""Reading a decision citation in the form a model actually writes it.

A model holds "BVerwG, Urteil vom 24.10.2023 - 2 C 9.22", not a bare docket.
Two things follow, and this module is both of them:

  * The court and the date in front of the docket are DISAMBIGUATORS. An
    Aktenzeichen is unique per court, not nationwide — thousands of dockets
    are held by more than one decision — so stripping a citation to its bare
    docket throws away exactly the information that picks the right decision.
  * A citation the resolver's grammar cannot read must still be rescued,
    without inventing anything, and every rewrite must be reported back to the
    caller.

The resolver's own citation grammar is reached through the backend
(`parse_prose_citation`, `is_court_head`, `docket_key`); this module only
decides what to hand it.
"""
from __future__ import annotations

import re

__all__ = ["normalise_citation", "canonical_citation", "court_mismatch", "DOCKET_IN"]

#: A German Aktenzeichen, found ANYWHERE in the string rather than parsed out of
#: a fixed citation grammar — the prose around it varies far more than the docket
#: does, so the docket is the anchor and everything before it is the prose.
#: Covers "2 C 9.22", "8 AZR 26/18", "1 BvR 209/83", "VIa ZR 335/21",
#: "XI ZR 338/01", "3 B 203/20". The lookbehind is what keeps the roman-numeral
#: branch from latching onto the C or the V inside a court abbreviation.
DOCKET_IN = re.compile(
    r"(?<![A-Za-zÄÖÜäöüß0-9])"
    r"((?:[IVXLC]{1,5}[a-z]?|\d{1,3})"        # senate: "VIa", "XI", "2", "8"
    r"(?:\s+[A-Za-zÄÖÜäöüß]{1,6}\.?){1,3}"    # register: "ZR", "AZR", "BvR", "C"
    r"\s+\d{1,5}\s*[./]\s*\d{2,4}"            # running number / year
    # Bavarian e-file suffix: "4 U 120/24 e". Without it the bare docket can
    # resolve to another court's "4 U 120/24". Not "u. a.".
    r"(?:\s[a-z](?![\w.]))?)"
    r"(?![\w/])")

DATE_IN = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{2,4})")

#: "Urteil vom", "Urt. v.", "Beschluss vom", "Beschl. v.", "Vorlagebeschluss v."
DISPOSITIVE = re.compile(
    r"(?i)\b(?:vorlage)?(?:urt(?:eil)?|beschl(?:uss)?|verf(?:ügung)?)\.?"
    r"\s*(?:v\.|vom)?")


#: The court abbreviations a citation uses, as the corpus spells the court.
COURT_TYPES = {"olg": "oberlandesgericht", "lg": "landgericht", "ag": "amtsgericht",
               "vg": "verwaltungsgericht", "ovg": "oberverwaltungsgericht",
               "vgh": "verwaltungsgerichtshof", "lag": "landesarbeitsgericht",
               "arbg": "arbeitsgericht", "sg": "sozialgericht",
               "lsg": "landessozialgericht", "fg": "finanzgericht",
               "kg": "kammergericht"}
FEDERAL_COURTS = {"bgh": "bundesgerichtshof", "bag": "bundesarbeitsgericht",
                  "bverwg": "bundesverwaltungsgericht", "bfh": "bundesfinanzhof",
                  "bsg": "bundessozialgericht", "bverfg": "bundesverfassungsgericht",
                  "bpatg": "bundespatentgericht", "eugh": "gerichtshof"}


def court_mismatch(asserted: str | None, actual: str | None) -> bool:
    """True when the court a citation named is plainly not the court we hold.

    'OLG Bamberg … 4 U 120/24' can resolve to OLG Brandenburg's decision under
    the same docket, because an Aktenzeichen is unique per court, not
    nationwide. Conservative on purpose: a federal abbreviation must match, and
    every place word ('Bamberg', 'Hamm') must appear in the held court's name;
    senate/chamber words are ignored.
    """
    if not asserted or not actual:
        return False
    have = actual.lower()
    words = [w.lower() for w in re.findall(r"[A-Za-zÄÖÜäöüß]+", asserted)]
    for w in words:
        if w in FEDERAL_COURTS:
            return w not in have and FEDERAL_COURTS[w] not in have
    places = [w for w in words if len(w) >= 3 and w not in COURT_TYPES
              and not w.endswith(("senat", "kammer", "gericht", "hof"))]
    return any(w not in have for w in places)


def normalise_citation(raw: str, grammar) -> dict:
    """Prepare one citation for the resolver, and say what was done to it.

    `grammar` is the backend: it supplies `parse_prose_citation`,
    `is_court_head` and `docket_key`.

    The order of attempts, most faithful first:

      1. the resolver's own prose grammar reads the string → hand it over
         untouched. It parses the court and date itself and uses them to
         narrow an ambiguous docket.
      2. it does not, but this module's looser docket scan does → rebuild the
         citation in the canonical form the prose grammar DOES read
         ('OLG Bamberg, Urteil vom 05.05.2025 - 4 U 120/24'), and use it only if
         it round-trips to the same docket. This rescues the forms the prose
         grammar refuses: 'OLG Bamberg 4 U 120/24 e' (no comma), 'siehe BGH XI
         ZR 338/01' (prose in front of the court).
      3. neither → the bare docket.

    Nothing is silent: `normalised` is what the resolver is actually sent, and
    the tool echoes it as `normalised_to` whenever it differs from what the
    caller wrote. The date the caller asserted is kept as `asserted_date` so
    the tool can say "the docket is real, your date is not" instead of either
    accepting or rejecting the whole string.

    Returns `{query, normalised, asserted_court, asserted_date}`. A norm citation
    ("§ 823 Abs. 1 BGB"), an ECLI or a CELEX number is returned untouched: the
    resolver reads those directly.
    """
    s = " ".join(str(raw or "").split())
    out = {"query": s, "normalised": s, "asserted_court": None, "asserted_date": None}
    if not s or "§" in s or s.lower().startswith(("art", "ecli:", "celex")):
        return out
    # 1. The resolver reads it whole. Nothing to do, and nothing to report: the
    #    court and the date reach the resolver as the disambiguators they are.
    if (prose := grammar.parse_prose_citation(s)) is not None:
        out["asserted_court"] = prose.get("court")
        out["asserted_date"] = prose.get("date")
        return out
    m = DOCKET_IN.search(s)
    if not m:
        return out
    docket = " ".join(m.group(1).split())
    head = s[:m.start()].strip(" ,;-–—")
    if not head:
        return out  # already the bare docket: nothing was stripped, say nothing
    out["normalised"] = docket
    if (d := DATE_IN.search(head)):
        dd, mo, yy = (int(x) for x in d.groups())
        if yy < 100:
            yy += 2000 if yy < 50 else 1900
        if 1 <= mo <= 12 and 1 <= dd <= 31:
            out["asserted_date"] = f"{yy:04d}-{mo:02d}-{dd:02d}"
    court = DISPOSITIVE.sub("", DATE_IN.sub("", head)).strip(" ,.;-–—()")
    if court:
        out["asserted_court"] = " ".join(court.split())
    # 2. Rebuild rather than strip, so the court and date survive the trip.
    if (rebuilt := canonical_citation(out["asserted_court"], out["asserted_date"],
                                      docket, grammar)) is not None:
        out["normalised"] = rebuilt
    return out


def canonical_citation(court: str | None, date: str | None, docket: str,
                       grammar) -> str | None:
    """`court` + `date` + `docket` as a string the prose grammar reads, or None.

    The bridge between this module's docket scan (which finds a docket ANYWHERE
    in a sentence) and the resolver's prose grammar (which is anchored, needs a
    comma before an undated docket, and rejects a court head it cannot
    recognise). Without it, every citation the anchored grammar declines loses
    its disambiguators on the way in.

    TWO GUARDS, and the function returns None rather than a guess if either fails:

      * the court head is trimmed from BOTH ends. From the left, one word at a
        time until the grammar accepts it as a court, so 'siehe BGH' and 'Vgl.
        dazu BAG' become 'BGH' and 'BAG'. A candidate whose first word carries a
        DOT is skipped on the way: a prose abbreviation like 'Vgl.' can look
        like a court abbreviation with its full stop stripped, and asserting a
        court named 'Vgl. dazu BAG' would make a sound citation read as a court
        mismatch. Citation practice writes court abbreviations without dots and
        prose abbreviations with them, so the dot is the available
        discriminator. From the right, uncapitalised trailing words go, because
        a German court's seat is capitalised and 'hat mit' is not — 'Das OLG
        Bamberg hat mit (' becomes 'OLG Bamberg'. That second trim is not
        cosmetic: narrowing by court requires every seat word to appear in the
        held court's name, so one stray verb would silently cost the narrowing
        this whole function exists to enable.
      * the rebuilt string is PARSED BACK, and kept only if the grammar reads
        the same docket out of it. A rebuild that moved the docket is a rebuild
        that would resolve a different citation, and it is dropped silently —
        the caller is no worse off than without it.
    """
    if not court or not docket:
        return None
    words = [w for w in court.split() if w.strip(".,;:-–—()")]
    while len(words) > 1 and not words[-1][:1].isupper():
        words.pop()
    head = next((" ".join(words[i:]) for i in range(len(words))
                 if "." not in words[i]
                 and grammar.is_court_head(" ".join(words[i:]))), None)
    if not head:
        return None
    if date:
        y, mo, d = date.split("-")
        cand = f"{head}, Urteil vom {int(d):02d}.{int(mo):02d}.{y} - {docket}"
    else:
        cand = f"{head}, {docket}"
    back = grammar.parse_prose_citation(cand)
    mine = grammar.docket_key(docket) or docket
    if not back or back.get("docket") not in (docket, mine):
        return None
    return cand
