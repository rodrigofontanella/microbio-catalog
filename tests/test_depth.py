"""Depth flag: the Python rule (dry runs) matches the generated column's boundaries; losses and the report text.

The generated column itself is proven by data/constraint_test_m7.sql (boundaries 999/1000/4999/5000, re-flag).
"""
from __future__ import annotations

import pytest

from app.models import DEPTH_STATUS_SQL
from app.reports.depth import format_report, status, with_losses


@pytest.mark.parametrize("n, expected", [(999, "fail"), (1000, "low"), (4999, "low"), (5000, "ok"), (67, "fail")])
def test_status_boundaries_match_the_generated_column(n, expected):
    assert status(n, 1000, 5000) == expected
    assert "n_reads < depth_fail_below THEN 'fail'" in DEPTH_STATUS_SQL      # same strict '<' as the SQL
    assert "n_reads < depth_low_below THEN 'low'" in DEPTH_STATUS_SQL


def test_no_thresholds_or_no_reads_means_no_status():
    assert status(None, 1000, 5000) is None and status(6711, None, None) is None


def test_losses_count_unprofiled_runs_as_lost():
    r = with_losses({"grp": "Antibiotic", "runs": 28, "ok": 14, "low": 7, "fail": 5, "unflagged": 0, "not_profiled": 1})
    assert (r["lost_at_fail"], r["lost_at_low"]) == (6, 13)


def test_report_text_names_thresholds_groups_and_shares():
    rows = [with_losses({"grp": g, "runs": n, "ok": ok, "low": lo, "fail": f, "unflagged": 0, "not_profiled": np})
            for g, n, ok, lo, f, np in [("Allium extract", 32, 31, 1, 0, 0), ("Antibiotic", 28, 15, 7, 5, 1),
                                        ("Control", 30, 28, 0, 1, 1)]]
    text = format_report("PRJNA664026", [{"profile_id": 8, "profile_key": "k", "fail_below": 1000, "fail_below_max": 1000,
                                          "low_below": 5000, "low_below_max": 5000, "rows": rows}], "treatment")
    assert "profile 8 (fail < 1000, low < 5000), by treatment" in text
    antibiotic = next(line for line in text.splitlines() if line.startswith("Antibiotic"))
    assert antibiotic.split()[-4:] == ["6", "21%", "13", "46%"]
    assert "MIXED" not in text and "no thresholds" not in text
    mixed = format_report("P", [{"profile_id": 1, "profile_key": "k", "fail_below": 500, "fail_below_max": 1000,
                                 "low_below": 5000, "low_below_max": 5000, "rows": []}], None)
    assert "MIXED thresholds" in mixed
    assert format_report("P", [], None) == "P: no profiles"
