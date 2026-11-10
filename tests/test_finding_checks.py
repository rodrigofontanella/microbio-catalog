"""Stored paper-vs-profile verdicts (app.ingest.finding_checks): report rows → core.finding_check rows, the verdict
re-derivation, and the stored-vs-new diff. Made-up study from test_finding_vs_profile; no Spark, no database.
`_db_rules` mirrors migration 6c1f0e2a9b47's CHECKs, so a row the loader builds that the table would refuse fails
here first (the table itself is proven by data/constraint_test_m9.sql)."""
from __future__ import annotations

import copy

from app.ingest.finding_checks import check_rows, diff_stored, verdict_errors
from app.models import CHECK_DIRECTIONS, CHECK_ROLES, CHECK_SCENARIOS, CHECK_TESTED_VERDICTS, CHECK_VERDICTS
from app.reports.finding_vs_profile import Claim, analyse
from tests.test_finding_vs_profile import TAXA, _study

HEADER = {"map_path": "conf/match/x.yml", "map_sha256": "0" * 64, "reviewed_by": "R", "reviewed_on": "2026-11-10",
          "profile_key": "pgmd|r|PRJNA0000003|taxon_collapsed|species", "delta_version": 49, "taxon_version": 157,
          "alpha": 0.05, "min_prevalence": 3}


def _db_rules(r: dict) -> list[str]:
    nn = lambda *cs: sum(r[c] is not None for c in cs)  # noqa: E731
    rules = {
        "scenario_check": r["scenario"] in CHECK_SCENARIOS,
        "role_check": r["role"] in CHECK_ROLES,
        "verdict_check": r["verdict"] in CHECK_VERDICTS,
        "direction_check": r["direction"] is None or r["direction"] in CHECK_DIRECTIONS,
        "p_q_pair": (r["p"] is None) == (r["q"] is None),
        "p_q_range": r["p"] is None or (0 <= r["p"] <= 1 and 0 <= r["q"] <= 1),
        "direction_pair": (r["direction"] is None) == (r["q"] is None),
        "tested_has_q": (r["verdict"] in CHECK_TESTED_VERDICTS) == (r["q"] is not None),
        "verdict_role": r["role"] == "also" or r["verdict"] != "no_verdict",
        "also_no_verdict": r["role"] == "verdict" or r["verdict"] in ("no_verdict", "not_testable", "n_a", "same_as_all"),
        "sensitivity_only": r["scenario"] != "all" or r["verdict"] not in ("n_a", "same_as_all"),
        "unit": r["unit"] is not None or (r["role"] == "verdict" and r["verdict"] == "not_testable"),
        "reason": r["verdict"] != "not_testable" or r["reason"] is not None,
        "counts_complete": nn("n_t", "n_r", "present_t", "present_r", "median_t", "median_r") in (0, 6),
        "counts_order": r["n_t"] is None or (0 <= r["present_t"] <= r["n_t"] and 0 <= r["present_r"] <= r["n_r"]),
        "tested_has_counts": r["q"] is None or r["n_t"] is not None,
    }
    return [k for k, ok in rules.items() if not ok]


def _rows():
    spec, claims, runs, rows = _study()
    claims = [Claim(*[getattr(c, f) for f in ("local_id", "feature_kind", "feature_label", "site_term", "direction",
                                               "treatment", "reference", "paper_stat")], finding_id=100 + i)
              for i, c in enumerate(claims)]
    out, families = analyse(spec, claims, runs, rows, TAXA)
    return claims, out, check_rows(out, families, HEADER, study_id=3, profile_id=7)


def test_every_report_row_becomes_a_row_the_table_accepts():
    _, out, db = _rows()
    assert len(db) == len(out) == 12
    assert {k: v for r in db for k, v in [(id(r), _db_rules(r))] if v} == {}
    keys = [(r["finding_id"], r["profile_id"], r["scenario"], r["role"], r["unit"]) for r in db]
    assert len(keys) == len(set(keys))                                      # finding_check_key
    assert {r["tier"] for r in db} == {"derived"} and {r["method"] for r in db} == {"finding_vs_profile"}


def test_report_labels_become_table_values():
    _, _, db = _rows()
    got = {(r["evidence"]["claim"], r["role"], r["scenario"]): r for r in db}
    assert got[("g-cae-prevotella", "verdict", "no_fail")]["verdict"] == "same_as_all"
    assert got[("g-cae-prevotella", "also", "all")]["verdict"] == "no_verdict"
    assert got[("b-feces-firmicutes-null", "verdict", "ok_only")]["verdict"] == "n_a"
    m = got[("g-cae-paraprevotella", "verdict", "all")]                   # the map's own not_testable
    assert m["unit"] is None and m["reason"] == "no such genus in the profile" and m["n_t"] is None


def test_evidence_names_the_map_the_profile_version_and_the_test_family():
    _, _, db = _rows()
    r = next(r for r in db if r["evidence"]["claim"] == "g-cae-prevotella" and r["scenario"] == "all")
    ev = r["evidence"]
    assert (ev["map_sha256"], ev["delta_version"], ev["feature_taxon_version"]) == ("0" * 64, 49, 157)
    assert ev["family"] == {"pigs_t": 9, "pigs_r": 9, "tested": 3, "below_prevalence": 0}
    na = next(r for r in db if r["verdict"] == "n_a")
    assert na["evidence"]["family"] is None                                 # no test family was formed


def test_verdicts_are_re_derived_before_writing():
    claims, _, db = _rows()
    assert verdict_errors(db, claims, 0.05) == []
    bad = copy.deepcopy(db)
    r = next(r for r in bad if r["verdict"] == "reproduced")
    r["direction"] = "increase"                                             # PGMD direction flipped, verdict kept
    assert any("rule gives contradicted" in e for e in verdict_errors(bad, claims, 0.05))


def test_stored_rows_that_differ_are_named():
    _, _, db = _rows()
    one = [r for r in db if r["finding_id"] == db[0]["finding_id"]]
    assert diff_stored(copy.deepcopy(one), one) == []
    changed = copy.deepcopy(one)
    changed[0]["q"] = 0.5
    changed[0]["evidence"]["map_sha256"] = "1" * 64
    (msg,) = diff_stored(changed, one)
    assert "q 0.5 ->" in msg and "evidence" in msg
    assert diff_stored(one[1:], one) == [f"new only: {(one[0]['scenario'], one[0]['role'], one[0]['unit'])}"]
