"""--all's control flow (decided 2026-11-07), without Spark or a database: which studies are loaded, skipped or
failed, that one failure doesn't stop the batch, that an unexpected error does, and the summary."""
from __future__ import annotations

import pytest

from app.lake import pgmd_profile as job
from app.lake.pgmd_profile import StudyResult, run_batch, summary

PER_STUDY = {   # BioProject → (bronze rows, Species rows), deliberately unsorted
    "PRJNA000003": (900, 300),
    "PRJNA000001": (500, 0),          # other levels only: nothing to load
    "PRJNA000002": (1200, 400),
    "PRJEB000004": (100, 40),
}


def fake_load(outcomes: dict[str, StudyResult]):
    called = []

    def load(bp: str) -> StudyResult:
        called.append(bp)
        r = outcomes[bp]
        if isinstance(r, Exception):
            raise r
        return r
    return load, called


def ok(bp, irid, runs=10, rows=300, median=20000.0):
    return StudyResult(bp, "loaded", irid, {"runs": runs, "rows": rows, "median_reads": median,
                                           "depth": {"ok": runs, "low": 0, "fail": 0}, "reference": "GG 13_8"})


def test_parser_needs_exactly_one_of_bioproject_or_all():
    p = job.build_parser()
    assert p.parse_args(["--all"]).all and p.parse_args(["--bioproject", "PRJNA1"]).bioproject == "PRJNA1"
    for argv in ([], ["--all", "--bioproject", "PRJNA1"]):
        with pytest.raises(SystemExit):
            p.parse_args(argv)


def test_sorted_order_and_no_species_is_skipped_without_loading():
    load, called = fake_load({"PRJEB000004": ok("PRJEB000004", 61), "PRJNA000002": ok("PRJNA000002", 62),
                              "PRJNA000003": ok("PRJNA000003", 63)})
    results = run_batch(PER_STUDY, load)
    assert [r.bioproject for r in results] == sorted(PER_STUDY)
    assert called == ["PRJEB000004", "PRJNA000002", "PRJNA000003"]          # never PRJNA000001
    skipped = next(r for r in results if r.bioproject == "PRJNA000001")
    assert (skipped.outcome, skipped.ingest_run_id) == ("no species rows", None)


def test_a_failed_study_does_not_stop_the_batch():
    failed = StudyResult("PRJNA000002", "failed", 62, {"reason": "160 runs are not core.run rows of PRJNA000002: [...]"})
    load, called = fake_load({"PRJEB000004": ok("PRJEB000004", 61), "PRJNA000002": failed,
                              "PRJNA000003": ok("PRJNA000003", 63)})
    results = run_batch(PER_STUDY, load)
    assert called[-1] == "PRJNA000003"
    assert [r.outcome for r in results] == ["loaded", "no species rows", "failed", "loaded"]


def test_an_unexpected_error_stops_the_batch():
    load, called = fake_load({"PRJEB000004": ok("PRJEB000004", 61), "PRJNA000002": RuntimeError("Py4J died"),
                              "PRJNA000003": ok("PRJNA000003", 63)})
    with pytest.raises(RuntimeError, match="Py4J"):
        run_batch(PER_STUDY, load)
    assert called == ["PRJEB000004", "PRJNA000002"]


def test_summary_lists_each_study_and_counts_outcomes():
    results = [ok("PRJEB000004", 61), StudyResult("PRJNA000001", "no species rows", None, {"rows": 0}),
               StudyResult("PRJNA000002", "failed", 62, {"reason": "genus test failed: x" + "y" * 300}),
               StudyResult("PRJNA667518", "unchanged", 63, {"runs": 240, "rows": 21641, "median_reads": 29240.0,
                                                            "depth": {"ok": 240, "low": 0, "fail": 0},
                                                            "reference": "GG 13_8"})]
    out = summary(results).splitlines()
    assert out[0].split("\t")[:3] == ["bioproject", "outcome", "ingest_run"]
    assert out[1].split("\t") == ["PRJEB000004", "loaded", "61", "10", "300", "20000", "10/0/0", "GG 13_8", ""]
    assert out[3].split("\t")[1] == "failed" and len(out[3].split("\t")[-1]) == 160      # reason truncated
    assert out[4].split("\t")[:6] == ["PRJNA667518", "unchanged", "63", "240", "21641", "29240"]
    assert out[-1] == "outcomes: failed 1, loaded 1, no species rows 1, unchanged 1 (of 4)"
