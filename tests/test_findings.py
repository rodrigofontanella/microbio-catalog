"""Findings rows from the real spec: what the loader would write. No database."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from app.ingest.findings import contrast_levels, feature_set_rows, finding_rows

ROOT = Path(__file__).parents[1]
SPEC = ROOT / "conf" / "findings" / "PRJNA667518.yml"
LEVELS = {"LF": 101, "HF": 102, "HF+XY": 103, "HF+AX": 104}       # stand-in level ids


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(SPEC.read_text())


@pytest.fixture(scope="module")
def rows(spec) -> dict[str, tuple[dict, list[dict]]]:
    out = finding_rows(spec, study_id=2, experiment_id=1, doc_id=1, factor_id=10, level_ids=LEVELS,
                       feature_set_ids={"top20-genera-cae-dig": 7, "top200-otus-cae-dig": 8}, fulltext_sha256="abc")
    return {r["local_id"]: (r, lv) for r, lv in out}


def test_one_row_per_claim(spec, rows):
    assert len(rows) == len(spec["claims"]) == 16


def test_pairwise_uses_the_level_columns(rows):
    r, lv = rows["g-cae-muc-xy-hf-faecalibacterium"]
    assert (r["treatment_level_id"], r["reference_level_id"], lv) == (103, 102, [])


def test_omnibus_lists_its_members(rows):
    r, lv = rows["a-cae-muc-simpson-null"]
    assert r["treatment_level_id"] is None and r["direction"] == "no_difference"
    assert {(x["level_id"], x["role"]) for x in lv} == {(101, "member"), (102, "member"), (103, "member"),
                                                        (104, "member")}


def test_grouping_keeps_both_sides(rows):
    _, lv = rows["a-col-dig-shannon-grouping"]
    assert {(x["level_id"], x["role"]) for x in lv} == {(102, "higher"), (104, "higher"), (101, "lower"),
                                                        (103, "lower")}


def test_counts_are_on_the_set(rows):
    r, _ = rows["otu-cae-dig-hf-lf-counts"]
    assert (r["feature_set_id"], r["feature_label"], r["n_increase"], r["n_decrease"]) == (8, None, 44, 19)


def test_null_is_about_its_set(rows):
    r, _ = rows["g-cae-dig-ax-hf-top20-null"]
    assert (r["feature_set_id"], r["direction"], r["stat_op"], r["stat_value"]) == (7, "no_difference", ">",
                                                                                  Decimal("0.10"))


def test_taxonomy_only_on_taxa(spec, rows):
    assert rows["g-cae-dig-hf-lf-lactobacillus"][0]["taxonomy"] == spec["taxonomy"]
    assert rows["p-cae-dig-xy-hf-xylanase"][0]["taxonomy"] is None
    assert rows["a-cae-muc-hf-lf-chao1"][0]["taxonomy"] is None


def test_streptococcus_keeps_unknown_material_and_its_flag(rows):
    r, _ = rows["g-cae-ax-hf-streptococcus"]
    assert r["material"] is None and r["evidence"]["flags"][0].startswith("check_figure_3")


def test_legend_stat_keeps_its_source_and_quote(rows):
    r, _ = rows["otu-cae-muc-ax-hf-otu160"]
    assert (r["stat_scope"], r["stat_source"]) == ("unknown", "figure_legend")
    assert "contrast value of Q < 0.05" in r["evidence"]["stat_quote"]


def test_no_effect_sizes_and_curated_tier(rows):
    for r, _ in rows.values():
        assert (r["effect_kind"], r["effect_value"], r["tier"]) == (None, None, "curated")
        assert r["method"] == "paper_results:PMC8024495"


def test_feature_sets(spec):
    fs = {r["local_id"]: r for r in feature_set_rows(spec, 2)}
    assert fs["top200-otus-cae-dig"]["size"] == 200 and fs["top200-otus-cae-dig"]["member_kind"] == "otu"


def test_contrast_levels_roles():
    assert contrast_levels({"kind": "pairwise", "treatment": "HF+XY", "reference": "HF"}) == [
        ("HF+XY", "treatment"), ("HF", "reference")]
