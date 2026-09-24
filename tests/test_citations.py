# SPDX-License-Identifier: AGPL-3.0-only
import pytest

from recht_mcp import grammar
from recht_mcp.citations import court_mismatch, normalise_citation


@pytest.mark.parametrize("raw", ["§ 823 Abs. 1 BGB", "Art. 83 DSGVO",
                                 "ECLI:DE:BGH:2019:180619UVIIIZR247.18.0",
                                 "CELEX:62014CJ0362", "2 C 9.22", ""])
def test_norms_eclis_and_bare_dockets_pass_untouched(raw):
    n = normalise_citation(raw, grammar)
    assert n["normalised"] == raw and n["asserted_court"] is None


@pytest.mark.parametrize("raw,court,date", [
    ("BVerwG, Urteil vom 24.10.2023 - 2 C 9.22", "BVerwG", "2023-10-24"),
    ("BAG, 8 AZR 26/18", "BAG", None),
    ("OLG Köln, Beschl. v. 1.2.2024 – 6 U 1/23", "OLG Köln", "2024-02-01"),
])
def test_a_whole_citation_reaches_the_resolver_whole(raw, court, date):
    n = normalise_citation(raw, grammar)
    assert n["normalised"] == raw
    assert (n["asserted_court"], n["asserted_date"]) == (court, date)


@pytest.mark.parametrize("raw,rebuilt", [
    ("siehe BGH XI ZR 338/01", "BGH, XI ZR 338/01"),
    ("OLG Bamberg 4 U 120/24 e", "OLG Bamberg, 4 U 120/24 e"),
    ("Das OLG Bamberg hat mit Urteil vom 05.05.2025 (4 U 120/24)",
     "OLG Bamberg, Urteil vom 05.05.2025 - 4 U 120/24"),
])
def test_a_declined_citation_is_rebuilt_with_its_disambiguators(raw, rebuilt):
    assert normalise_citation(raw, grammar)["normalised"] == rebuilt


def test_a_prose_abbreviation_is_never_taken_for_a_court():
    n = normalise_citation("Vgl. dazu BAG 8 AZR 26/18", grammar)
    assert n["normalised"] == "BAG, 8 AZR 26/18"


def test_without_a_court_the_docket_is_stripped_bare():
    n = normalise_citation("wie im Urteil vom 3.4.2020 zu 8 AZR 26/18", grammar)
    assert n["normalised"] == "8 AZR 26/18" and n["asserted_date"] == "2020-04-03"


class _Mover:
    """A grammar whose parse-back reads a different docket."""
    parse_prose_citation = staticmethod(
        lambda s: {"court": "BGH", "date": None, "docket": "OTHER"} if s.startswith("BGH,") else None)
    is_court_head = staticmethod(grammar.is_court_head)
    docket_key = staticmethod(grammar.docket_key)


def test_a_rebuild_that_moves_the_docket_is_dropped():
    assert normalise_citation("siehe BGH XI ZR 338/01", _Mover)["normalised"] == "XI ZR 338/01"


@pytest.mark.parametrize("asserted,actual,mismatch", [
    ("OLG Bamberg", "Oberlandesgericht Brandenburg", True),
    ("OLG Bamberg", "Oberlandesgericht Bamberg", False),
    ("OLG", "Oberlandesgericht Köln", False),
    ("BGH", "Bundesgerichtshof", False),
    ("BGH", "Bundesverwaltungsgericht", True),
    ("BGH 8. Zivilsenat", "BGH", False),
    (None, "BGH", False),
])
def test_court_mismatch(asserted, actual, mismatch):
    assert court_mismatch(asserted, actual) is mismatch


@pytest.mark.parametrize("text,key", [("Az. 4 OH 3/22", "4 OH 3/22"), ("miete 12/20", None),
                                      ("VIa  ZR 335/21", "VIa ZR 335/21")])
def test_docket_key(text, key):
    assert grammar.docket_key(text) == key


@pytest.mark.parametrize("head,ok", [("BGH", True), ("OLG Köln", True),
                                     ("Bundesarbeitsgericht", True), ("siehe", False),
                                     ("§ 823 BGB", False), ("", False)])
def test_is_court_head(head, ok):
    assert grammar.is_court_head(head) is ok
