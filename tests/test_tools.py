# SPDX-License-Identifier: AGPL-3.0-only
import pytest

from recht_mcp.tools import provision_key


# ------------------------------------------------------------- resolveIdentifiers

def test_resolve_hands_a_prose_citation_to_the_resolver_whole(call, backend):
    raw = "BVerwG, Urteil vom 24.10.2023 - 2 C 9.22"
    is_error, out = call("resolveIdentifiers", {"citations": [raw]})
    assert not is_error
    assert backend.calls[0] == ("resolve", [raw], [])
    row = out["results"][0]
    assert "normalised_to" not in row
    assert row["asserted_court"] == "BVerwG" and row["asserted_date"] == "2023-10-24"


def test_a_citation_the_grammar_declines_is_rebuilt_and_the_rewrite_reported(call, backend):
    raw = "siehe BGH XI ZR 338/01"
    _, out = call("resolveIdentifiers", {"citations": [raw]})
    row = out["results"][0]
    assert row["normalised_from"] == raw
    assert row["normalised_to"] == "BGH, XI ZR 338/01"
    assert backend.calls[0][1] == ["BGH, XI ZR 338/01"]


def test_a_wrong_date_is_a_correction_not_a_pass(call, backend):
    backend.resolve_rows = [{"status": "resolved", "citation_kind": "docket",
                             "case": {"decided": "2023-09-14", "court": "BVerwG"}}]
    _, out = call("resolveIdentifiers",
                  {"citations": ["BVerwG, Urteil vom 24.10.2023 - 2 C 9.22"]})
    assert out["results"][0]["date_mismatch"]["actual"] == "2023-09-14"
    assert "court_mismatch" not in out["results"][0]


def test_a_docket_held_by_another_court_is_flagged(call, backend):
    backend.resolve_rows = [{"status": "resolved", "citation_kind": "docket",
                             "citation_check": {"court_matches": True},
                             "case": {"decided": "2025-05-05",
                                      "court": "Oberlandesgericht Brandenburg"}}]
    _, out = call("resolveIdentifiers",
                  {"citations": ["OLG Bamberg, Urteil vom 05.05.2025 - 4 U 120/24"]})
    flag = out["results"][0]["court_mismatch"]
    assert flag["asserted"] == "OLG Bamberg"


def test_a_resolved_norm_carries_the_gazette_citation_once_per_law(call, backend):
    _, out = call("resolveIdentifiers", {"citations": ["§ 622 BGB", "§ 623 BGB"]})
    assert [r["fundstelle"] for r in out["results"]] == ["RGBl 1896, 195"] * 2
    assert [c for c in backend.calls if c[0] == "find_law"] == [("find_law", "BGB")]


def test_a_bare_unknown_row_gets_a_reason_and_a_hint(call, backend):
    backend.resolve_rows = [{"status": "unknown", "input": "§ 5"},
                            {"status": "unknown", "note": "Landesrecht (Hessen) …"}]
    _, out = call("resolveIdentifiers", {"citations": ["§ 5", "§ 5 HDSIG"]})
    assert out["results"][0]["reason"] == "not_in_corpus"
    assert "abbreviation" in out["results"][0]["note"]
    assert out["results"][1]["reason"] == "outside_coverage"


@pytest.mark.parametrize("args", [{}, {"citations": []}, {"citations": "x"},
                                  {"citations": ["x"] * 101},
                                  {"citations": ["x"], "include": ["txt"]}])
def test_resolve_validates(call, args):
    is_error, out = call("resolveIdentifiers", args)
    assert is_error and out["reason"] == "invalid_argument"


# ------------------------------------------------------------------ search

def test_search_spans_both_corpora_and_rolls_up_jurisdictions(call, backend):
    _, out = call("search", {"q": "Videoüberwachung öffentlich zugänglicher Räume"})
    assert out["counts"] == {"norms": 2, "cases": 1}
    assert [b["jurisdiction"] for b in out["by_jurisdiction"]] == ["bund", "sn"]
    assert out["attribution"] == "x"
    assert out["source"]["url"] == "https://recht.nulegal.eu"
    assert ("search_norms", "Videoüberwachung öffentlich zugänglicher Räume", 10, False) in backend.calls
    assert ("search_cases", "Videoüberwachung öffentlich zugänglicher Räume", 10) in backend.calls


def test_an_empty_norm_search_points_at_case_law(call):
    _, out = call("search", {"q": "Verzugspauschale", "scope": "norms"})
    assert out["norms"] == [] and out["cases_available"] == "unknown"
    assert "scope='cases'" in out["next"]


def test_case_search_limit_is_capped_at_twenty(call, backend):
    call("search", {"q": "Mietpreisbremse", "scope": "cases", "limit": 50})
    assert backend.calls == [("search_cases", "Mietpreisbremse", 20)]


@pytest.mark.parametrize("args", [{"q": "x"}, {"q": "ab", "scope": "web"},
                                  {"q": "ab", "limit": 51}, {"q": "ab", "limit": "5"}])
def test_search_validates(call, args):
    assert call("search", args)[1]["reason"] == "invalid_argument"


# ------------------------------------------------------------------ getNorm

def test_get_norm_serves_markdown_with_the_date_warning(call):
    is_error, out = call("getNorm", {"law": "BGB", "ref": "622"})
    assert not is_error
    assert out["markdown"].startswith("## § 622")
    assert "norm" not in out
    assert out["version"]["first_observed"] == "2019-06-10"
    assert "NOT the legal Inkrafttreten" in out["version"]["note"]
    assert out["version_coverage"]["archive_starts"] == "2019-06-10"
    assert out["fundstelle"] == "RGBl 1896, 195"


def test_no_markdown_falls_back_to_the_json_payload(call, backend):
    backend.markdown = None
    _, out = call("getNorm", {"law": "BGB", "ref": "622"})
    assert out["markdown"] is None and out["norm"]["ref"] == "§ 622"
    assert "markdown_unavailable" in out


def test_a_pre_archive_date_is_outside_coverage_never_not_found(call):
    is_error, out = call("getNorm", {"law": "BGB", "ref": "288", "as_of": "2014-08-01"})
    assert is_error and out["reason"] == "outside_coverage"
    assert out["version_coverage"]["asked_before_archive"] is True
    assert out["amendment_history_url"] == "https://recht.nulegal.eu/gesetze/BGB/aenderungen"


def test_a_sub_unit_is_dropped_and_said(call, backend):
    _, out = call("getNorm", {"law": "BGB", "ref": "556 g Abs. 1"})
    assert out["ref_normalised_from"] == "556 g Abs. 1"
    assert ("find_norm", 1, "556g", None) in backend.calls


@pytest.mark.parametrize("asked,key", [("622", "622"), ("556g Abs. 1", "556g"),
                                       ("556 g", "556g"), ("6 S. 2", "6"),
                                       ("83 lit. f", "83"), ("3a", "3a")])
def test_provision_key(asked, key):
    assert provision_key(asked) == key


def test_a_provision_miss_names_the_other_laws_under_that_abbreviation(call):
    is_error, out = call("getNorm", {"law": "LWG", "ref": "1"})
    assert is_error and out["reason"] == "not_in_corpus"
    assert [o["law_key"] for o in out["other_laws"]] == ["lwg"]


def test_an_ambiguous_law_hands_back_keys_to_pick_with(call):
    is_error, out = call("getNorm", {"law": "Luft", "ref": "1"})
    assert out["reason"] == "ambiguous"
    assert [c["law_key"] for c in out["candidates"]] == ["lwg", "by-lwg"]


def test_an_unknown_law_is_not_in_corpus(call):
    assert call("getNorm", {"law": "XYZ", "ref": "1"})[1]["reason"] == "not_in_corpus"


@pytest.mark.parametrize("args", [{"law": "BGB"}, {"law": "BGB", "ref": "1", "format": "html"},
                                  {"law": "BGB", "ref": "1", "as_of": "1.1.2020"}])
def test_get_norm_validates(call, args):
    assert call("getNorm", args)[1]["reason"] == "invalid_argument"


# --------------------------------------------------------- listNormVersions

def test_versions_mark_the_floor_version_as_a_floor(call):
    _, out = call("listNormVersions", {"law": "BGB", "ref": "622"})
    assert [v["at_archive_floor"] for v in out["versions"]] == [False, True]
    assert [v["in_force"] for v in out["versions"]] == [True, False]
    assert out["amendment_history_url"].endswith("/gesetze/BGB/aenderungen")


def test_versions_need_a_ref_and_a_held_provision(call):
    assert call("listNormVersions", {"law": "BGB"})[1]["reason"] == "invalid_argument"
    assert call("listNormVersions", {"law": "BGB", "ref": "1"})[1]["reason"] == "not_in_corpus"


# ------------------------------------------------------ listCitingDecisions

def test_citing_decisions_for_a_norm_warn_about_page_one(call):
    _, out = call("listCitingDecisions", {"law": "BGB", "ref": "622", "limit": 5})
    assert out["pagination"]["next_offset"] == 5
    assert "national" in out["pagination"]["note"]
    assert out["coverage"]["complete_for_this_norm"] is True


def test_zero_citing_decisions_on_a_land_norm_is_labelled_not_asserted(call, backend):
    backend.citing_decisions_for_norm = lambda *a, **k: {"total": 0, "cases": []}
    _, out = call("listCitingDecisions", {"law": "SächsDSDG", "ref": "13"})
    assert out["coverage"]["complete_for_this_norm"] is False
    assert "NOT that no court has cited it" in out["coverage"]["note"]


def test_recent_is_echoed_and_a_fallback_is_said(call, backend):
    backend.citing_decisions_for_norm = lambda *a, **k: {
        "total": 1, "cases": [], "ranking": {"sort": "weight", "requested_sort": "recent",
                                             "sort_note": "too many citers"}}
    _, out = call("listCitingDecisions", {"law": "BGB", "ref": "622", "sort": "recent"})
    assert out["sort_applied"] == "weight" and "WEIGHT order" in out["sort_note"]


def test_an_unheld_provision_is_not_in_corpus_not_an_outage(call):
    is_error, out = call("listCitingDecisions", {"law": "BGB", "ref": "404"})
    assert out["reason"] == "not_in_corpus" and "unknown norm" in out["detail"]


def test_citing_decisions_for_a_decision_carry_the_citing_rn(call):
    _, out = call("listCitingDecisions", {"case": "https://recht.nulegal.eu/rechtsprechung/bgh/2023-07-12/viii-zr-125-22"})
    assert out["total"] == 2
    assert out["cases"][0]["url"] == "https://e/x1#rd_17"
    assert out["cases"][0]["leitsatz"] is None
    assert out["cases"][1]["url"] == "https://e/x2"
    assert "sort_applied" not in out


def test_citing_decisions_for_an_unheld_decision(call):
    is_error, out = call("listCitingDecisions", {"case": "NOPE"})
    assert out["reason"] == "not_in_corpus" and "known_missing" in out["detail"]


@pytest.mark.parametrize("args", [{}, {"law": "BGB", "ref": "1", "case": "X"},
                                  {"case": "X", "limit": 101}, {"case": "X", "offset": -1},
                                  {"case": "X", "sort": "new"}])
def test_citing_decisions_validate(call, args):
    assert call("listCitingDecisions", args)[1]["reason"] == "invalid_argument"


# --------------------------------------------------- listCitedAuthorities

def test_cited_authorities_never_claim_a_treatment(call):
    _, out = call("listCitedAuthorities", {"case": "KORE304862023"})
    assert out["treatment"] is None
    assert out["cited_decisions"][0]["citations_in_this_decision"] == 3
    assert out["cited_norms"][0]["ref_norm"] == "556g"


def test_a_decision_that_provably_exists_is_known_missing(call, backend):
    backend.cases["M1"] = {"status": "known_missing", "cited_by": 4,
                           "citers": [{"doknr": f"C{i}"} for i in range(12)]}
    is_error, out = call("listCitedAuthorities", {"case": "M1"})
    assert out["reason"] == "known_missing" and len(out["attested_by"]) == 10


def test_a_joined_case_hands_back_the_lead_decision(call, backend):
    backend.cases["J1"] = {"status": "joined_with", "joined_with": {"doknr": "LEAD"}}
    _, out = call("listCasePassages", {"case": "J1"})
    assert out["reason"] == "known_missing" and out["joined_with"] == {"doknr": "LEAD"}


def test_an_unknown_decision_is_not_in_corpus(call):
    _, out = call("listCitedAuthorities", {"case": "NOPE"})
    assert out["reason"] == "not_in_corpus" and "resolveIdentifiers" in out["detail"]


# ------------------------------------------------------- listCasePassages

def test_passages_default_to_a_page_of_thirty(call):
    _, out = call("listCasePassages", {"case": "KORE304862023"})
    assert len(out["paragraphs"]) == 30 and out["pagination"]["next_offset"] == 30
    assert out["amtliche_seite"] is None


@pytest.mark.parametrize("around,first", [(1, 1), (30, 25), (60, 51)])
def test_around_centres_and_clamps(call, around, first):
    _, out = call("listCasePassages", {"case": "KORE304862023", "around": around, "limit": 10})
    assert out["paragraphs"][0]["rn"] == first and len(out["paragraphs"]) == 10
    assert out["around"] == str(around)


def test_around_refuses_to_guess(call, backend):
    _, out = call("listCasePassages", {"case": "KORE304862023", "around": 99})
    assert out["reason"] == "not_in_corpus" and out["rn_last"] == "60"
    backend.cases["N"] = {"status": "available", "anchor_basis": "positional",
                          "paragraphs": [{"rn": None, "text": "a"}]}
    _, out = call("listCasePassages", {"case": "N", "around": 1})
    assert out["prints_randnummern"] is False and "NO Randnummern" in out["detail"]


@pytest.mark.parametrize("args", [{"case": ""}, {"case": "KORE304862023", "offset": -1},
                                  {"case": "KORE304862023", "limit": 401},
                                  {"case": "KORE304862023", "around": True},
                                  {"case": "KORE304862023", "around": 5, "offset": 0}])
def test_passages_validate(call, args):
    assert call("listCasePassages", args)[1]["reason"] == "invalid_argument"


# ---------------------------------------------------------- getChanges

def test_changes_quote_the_law_segment_and_say_the_date_is_an_observation(call, backend):
    _, out = call("getChanges", {"since": "2026-09-01", "limit": 5})
    c = out["changes"][0]
    assert c["url"] == "https://recht.nulegal.eu/gesetze/Freiz%C3%BCgG%2FEU%202004/622"
    assert c["change_id"].endswith("#nv-77")
    assert out["cursor"] == "2026-09-01" and out["window_days"] == 120
    assert "FIRST SEEN" in out["note"]
    assert backend.calls == [("changes", "2026-09-01", 5, 120)]


def test_changes_validate(call):
    assert call("getChanges", {"since": "yesterday"})[1]["reason"] == "invalid_argument"
    assert call("getChanges", {"limit": 201})[1]["reason"] == "invalid_argument"


# ---------------------------------------------------------- getCoverage

def test_coverage_states_its_holes(call):
    _, out = call("getCoverage")
    assert [lim["id"] for lim in out["limits"]] == [
        "version_archive_floor", "citation_graph_is_federal",
        "first_observed_is_not_inkrafttreten", "source_windows_differ"]
    assert out["totals"]["laws"] == 1


def test_a_statistics_outage_is_unavailable_not_an_empty_corpus(call, backend):
    backend.stats_error = True
    is_error, out = call("getCoverage")
    assert out["reason"] == "unavailable" and "outage" in out["detail"]
