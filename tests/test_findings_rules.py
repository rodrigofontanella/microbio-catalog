"""check() and the rows for study 2's new shapes (host measures, tables, test statistics, group values), on the real
PMC8001633 full text and conf/designs/PRJNA664026.yml. Claims here are test material, not the reviewed spec.
No database."""
from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from app.ingest.findings import check, finding_rows, group_value_rows

ROOT = Path(__file__).parents[1]
PAPER = ROOT / "tests" / "fixtures" / "europepmc" / "PMC8001633" / "fulltext.xml"
DESIGN = ROOT / "conf" / "designs" / "PRJNA664026.yml"
ALL = ["Control", "Allium extract", "Antibiotic"]
LEVELS = {"Control": 201, "Allium extract": 202, "Antibiotic": 203}       # stand-in level ids

ADG = {   # Table 4: ADG days 28-70, Treatment
    "id": "host-adg", "measure": "average_daily_gain", "age": {"from": 28, "to": 70},
    "table": {"label": "Table 4", "row": {"Dependent Variable": "ADG 28-70 days, g/d",
                                          "Independent Variables": "Treatment"}},
    "contrast": {"kind": "omnibus", "levels": ALL}, "direction": "differs",
    "test": {"name": "GLMM", "stat_kind": "F", "value": 14.59, "df": [2, 18], "stat_cell": "F", "df_cell": "D.f."},
    "stat": {"kind": "p", "op": "<", "value": 0.001, "scope": "treatment", "source": "table", "cell": "p"},
    "n_analysed": {"value": 24, "rule": "df_den + 6", "why": "intercept, treatment 2, sex, room, initial BW"},
    "group_values": {"unit": "g/d", "spread_kind": "unstated", "levels": {
        "Control": {"cell": "Control", "mean": 325.25, "spread": 11.01},
        "Allium extract": {"cell": "Allium Extract", "mean": 367.71, "spread": 16.84},
        "Antibiotic": {"cell": "Antibiotic", "mean": 391.53, "spread": 15.37}}},
}
BETA = {  # Table 3: caecum, Unweighted UniFrac (blank-cell fill-down not needed: first row of the pair)
    "id": "beta-cae-unweighted", "site": "UBERON:0001153", "measure": "beta_diversity", "age": {"from": 70, "to": 70},
    "about": {"feature": {"kind": "distance_metric", "label": "Unweighted UniFrac"}},
    "table": {"label": "Table 3", "row": {"col1": "Caecum", "β-Diversity Distance Matrix": "Unweighted UniFrac"}},
    "contrast": {"kind": "omnibus", "levels": ALL}, "direction": "differs",
    "test": {"name": "PERMANOVA", "stat_kind": "pseudo_F", "value": 1.56, "df": [2, 15], "stat_cell": "Pseudo-F",
             "df_cell": "D.f."},
    "stat": {"kind": "p", "op": "=", "value": 0.007, "scope": "treatment", "source": "table", "cell": "p"},
    "n_analysed": {"value": 18, "rule": "df_den + 3", "why": "one-way, 3 levels"},
    "flags": ["rarefaction_exclusion"],
}
LSD = {   # §3.1 text: caecum richness, Allium < Control
    "id": "alpha-cae-richness-allium-control", "site": "UBERON:0001153", "measure": "alpha_diversity",
    "about": {"feature": {"kind": "alpha_index", "label": "Species richness"}},
    "section": "3.1. Effects of Teatment on Piglets' Gut Bacterial Alpha Diversity",
    "quote": "in caecum, Allium extract group showed lower values of Species richness and Faith's diversity index "
             "than Control group (LSD Posthoc test, p = 0.007",
    "contrast": {"kind": "pairwise", "treatment": "Allium extract", "reference": "Control"}, "direction": "decrease",
    "test": {"name": "LSD"},
    "stat": {"kind": "p", "op": "=", "value": 0.007, "scope": "contrast", "source": "text"},
}
SPEC = {"bioproject": "PRJNA664026", "pmcid": "PMC8001633", "experiment": "main",
        "review": {"reviewed_by": None, "reviewed_on": None}, "claims": [ADG, BETA, LSD]}


@pytest.fixture(scope="module")
def jats() -> bytes:
    return PAPER.read_bytes()


@pytest.fixture(scope="module")
def design() -> dict:
    return yaml.safe_load(DESIGN.read_text())


def broken(claim_id: str, change) -> dict:
    spec = copy.deepcopy(SPEC)
    change(next(c for c in spec["claims"] if c["id"] == claim_id))
    return spec


def test_valid_spec_passes(jats, design):
    assert check(SPEC, jats, design) == []


@pytest.mark.parametrize("claim_id, change, expect", [
    ("host-adg", lambda c: c.update(site="UBERON:0001153"), "has no gut site"),
    ("host-adg", lambda c: c.update(about={"feature": {"kind": "genus", "label": "Lactobacillus"}}), "no 'about'"),
    ("host-adg", lambda c: c.pop("age"), "age window"),
    ("host-adg", lambda c: c.update(age={"from": 70, "to": 28}), "0 <= from <= to"),
    ("beta-cae-unweighted", lambda c: c.pop("site"), "not a known gut-site term"),
    ("beta-cae-unweighted", lambda c: c.update(measure="alpha_diversity"), "doesn't fit measure"),
    ("beta-cae-unweighted", lambda c: c.update(contrast={"kind": "pairwise", "treatment": "Antibiotic",
                                                         "reference": "Control"}), "only for an omnibus"),
    # every number from a table must equal its cell
    ("beta-cae-unweighted", lambda c: c["stat"].update(value=0.07), "the cell prints '0.007'"),
    ("beta-cae-unweighted", lambda c: c["stat"].update(op="<="), "the cell prints '0.007'"),
    ("beta-cae-unweighted", lambda c: c["test"].update(value=1.65), "the cell prints '1.56'"),
    ("beta-cae-unweighted", lambda c: c["test"].update(df=[2, 16]), "the cell prints '2.15'"),
    ("host-adg", lambda c: c["group_values"]["levels"]["Control"].update(mean=325.52), "prints '325.25 (11.01)'"),
    # the row must be exactly one row; Caecum alone heads two
    ("beta-cae-unweighted", lambda c: c["table"]["row"].pop("β-Diversity Distance Matrix"), "matches 2 rows"),
    ("beta-cae-unweighted", lambda c: c["table"].update(label="Table 5"), "no table labelled"),
    # the caption's "General Linear Models" with a pseudo-F
    ("beta-cae-unweighted", lambda c: c["test"].update(name="GLM"), "doesn't come from a GLM test"),
    ("host-adg", lambda c: c["test"].pop("value"), "stat_kind and value go together"),
    ("host-adg", lambda c: c["test"].update(df=[0, 18]), "positive integers"),
    ("beta-cae-unweighted", lambda c: c["test"].update(df=[1, 56], df_cell="Pseudo-F"), "is not a d.f. column"),
    ("host-adg", lambda c: c["n_analysed"].update(value=25), "!= df_den 18 + 6"),
    ("host-adg", lambda c: c["contrast"].update(levels=["Control", "Antibiotic"]),
     "'Allium extract', which is not a level of this contrast"),
    ("host-adg", lambda c: c["group_values"].update(spread_kind="sem"), "spread_kind is one of"),
    ("alpha-cae-richness-allium-control", lambda c: c["stat"].update(source="table", cell="p"), "no table row"),
    ("alpha-cae-richness-allium-control", lambda c: c["test"].update(stat_kind="F", value=4.03),
     "neither a table cell nor the quote"),
    # study 3's tests print no statistic: no F or pseudo-F fits either (a8f4d5f5fd45)
    ("beta-cae-unweighted", lambda c: c["test"].update(name="Kruskal-Wallis"), "doesn't come from a Kruskal-Wallis"),
    ("host-adg", lambda c: c["test"].update(name="t-test"), "doesn't come from a t-test"),
    ("alpha-cae-richness-allium-control", lambda c: c["test"].update(name="Wilcoxon"), "test is {name"),
])
def test_rule_breaks(jats, design, claim_id, change, expect):
    errors = check(broken(claim_id, change), jats, design)
    assert any(expect in e for e in errors), errors


def test_initial_bw_statistic_cannot_be_stored(jats, design):
    spec = broken("host-adg", lambda c: (c["table"]["row"].update({"Dependent Variable": "Initial BW (28 days), kg"}),
                                         c["test"].update(value=0.01, df=[2, 19]), c.pop("group_values"),
                                         c.pop("n_analysed"), c["stat"].update(op="=", value=0.998)))
    assert any("operator" in e for e in check(spec, jats, design))


def test_host_row(design):
    (row, levels), _, _ = finding_rows(SPEC, study_id=3, experiment_id=6, doc_id=9, factor_id=12, level_ids=LEVELS,
                                       feature_set_ids={}, fulltext_sha256="abc")
    assert (row["site_term"], row["feature_label"], row["feature_set_id"]) == (None, None, None)
    assert (row["age_from_d"], row["age_to_d"], row["direction"]) == (28, 70, "differs")
    assert (row["test"], row["test_stat_kind"], row["test_stat_value"], row["df_num"], row["df_den"]) == (
        "GLMM", "F", Decimal("14.59"), 2, 18)
    assert (row["stat_op"], row["stat_value"], row["stat_source"]) == ("<", Decimal("0.001"), "table")
    assert row["evidence"]["table"]["label"] == "Table 4" and row["evidence"]["n_analysed"]["value"] == 24
    assert row["evidence"]["quote"] is None
    assert {lv["level_id"] for lv in levels} == {201, 202, 203}


def test_lsd_row_has_a_test_without_statistic():
    *_, (row, levels) = finding_rows(SPEC, study_id=3, experiment_id=6, doc_id=9, factor_id=12, level_ids=LEVELS,
                                     feature_set_ids={}, fulltext_sha256="abc")
    assert (row["test"], row["test_stat_kind"], row["df_num"], row["age_from_d"]) == ("LSD", None, None, None)
    assert "table" not in row["evidence"] and "n_analysed" not in row["evidence"] and levels == []


def test_group_values():
    gv = group_value_rows(SPEC, LEVELS)
    assert set(gv) == {"host-adg"}
    by_level = {g["level_id"]: g for g in gv["host-adg"]}
    assert (by_level[201]["value"], by_level[201]["spread"], by_level[201]["spread_kind"], by_level[201]["unit"]) == (
        Decimal("325.25"), Decimal("11.01"), "unstated", "g/d")
    assert by_level[202]["evidence"]["cell"] == "Allium Extract"


@pytest.mark.parametrize("signed_on, ok", [
    (None, True),                          # unsigned: fine for a dry run (the load itself refuses it)
    ("2026-11-07", False),                 # quoted: YAML keeps it text
    ("07 nov 2026", False),                # what ingest_run 51 stored
])
def test_review_date_as_text_is_refused(jats, design, signed_on, ok):
    spec = copy.deepcopy(SPEC)
    spec["review"] = {"reviewed_by": "Rodrigo Fontanella" if signed_on else None, "reviewed_on": signed_on}
    errors = check(spec, jats, design)
    assert (not any("reviewed_on" in e for e in errors)) is ok, errors


def test_review_date_as_yaml_reads_it():
    from datetime import date, datetime
    from app.ingest.findings import review_errors
    assert yaml.safe_load("d: 2026-11-07")["d"] == date(2026, 11, 7)
    assert review_errors({"reviewed_by": "Rodrigo Fontanella", "reviewed_on": date(2026, 11, 7)}) == []
    assert isinstance(yaml.safe_load("d: 07 nov 2026")["d"], str)
    assert review_errors({"reviewed_by": "R", "reviewed_on": "07 nov 2026"})
    # a timestamp is a datetime, which Python counts as a date: refused anyway
    ts = yaml.safe_load("d: 2026-11-07 10:00:00")["d"]
    assert isinstance(ts, datetime) and review_errors({"reviewed_by": "R", "reviewed_on": ts})
    assert review_errors({"reviewed_by": "  ", "reviewed_on": date(2026, 11, 7)}) == ["review.reviewed_by '  ' is not a name"]


@pytest.mark.parametrize("name", ["t-test", "Kruskal-Wallis"])
def test_study3_tests_stand_alone(jats, design, name):
    """Like LSD: a test name with no statistic passes check() and lands in the row's test column."""
    spec = broken("alpha-cae-richness-allium-control", lambda c: c["test"].update(name=name))
    assert check(spec, jats, design) == []
    *_, (row, _levels) = finding_rows(spec, study_id=3, experiment_id=6, doc_id=9, factor_id=12, level_ids=LEVELS,
                                      feature_set_ids={}, fulltext_sha256="abc")
    assert (row["test"], row["test_stat_kind"], row["test_stat_value"]) == (name, None, None)
