"""PGMD parsing/planning, the site dictionary and the conflict comparisons. No database.

Fixtures: PGMD v1 rows for PRJNA667518 (the project row, and the 8 Sample_runs rows of pens 101/102),
plus small inline rows for edge cases seen in the full file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.ingest import pgmd_parse as p
from app.ingest.vocab import gut_sites, material
from app.reports.pgmd_vs_ncbi import compare_platform, compare_site, compare_text

FIX = Path(__file__).parent / "fixtures" / "pgmd"


@pytest.fixture(scope="module")
def plan():
    proj = p.read_csv(FIX / "Project.csv", p.REQUIRED_PROJECT)
    rows = p.read_csv(FIX / "Sample_runs.csv", p.REQUIRED_SAMPLE)
    projects, samples = p.group_rows(proj, rows)
    return p.plan_project("PRJNA667518", projects["PRJNA667518"], samples["PRJNA667518"])


def row(**kw) -> dict[str, str]:
    base = {c: "NA" for c in p.REQUIRED_SAMPLE}
    base.update(Runs="SRR1", Bio_Project="PRJNA1", Bio_Sample="SAMN1")
    base.update(kw)
    return base


# ── cleaning, lists, ages ───────────────────────────────────────────────────

@pytest.mark.parametrize("raw,out", [("NA", None), ("  ", None), ("无", None), ("null", None),
                                     ("  Cecum ", "Cecum"), ("a  b", "a b")])
def test_clean(raw, out):
    assert p.clean(raw) == out


def test_split_list_handles_fullwidth():
    assert p.split_list("male;female") == ["male", "female"]
    assert p.split_list("Piglets；Sows，Gilts") == ["Piglets", "Sows", "Gilts"]


@pytest.mark.parametrize("raw,expected", [
    ("28 days", (28, 28, "single")), ("0 day", (0, 0, "single")), ("7 - 10 days", (7, 10, "range")),
    ("188(±14) days", (188, 188, "single")), ("3 weeks", (21, 21, "single")),
    ("7 days; 21 days", (7, 21, "list")), ("2 days before the farrowing", None),
    ("days 115 of pregnancy", None), ("pregnant sows", None)])
def test_parse_age(raw, expected):
    assert p.parse_age_days(raw) == expected


# ── planning on the real PRJNA667518 rows ───────────────────────────────────

def test_varying_fields_go_on_the_sample(plan):
    sample_fields = {a.field for a in plan.assertions if a.target == "sample"}
    assert sample_fields == {"group", "isolation_location"}       # vary within pens 101/102


def test_constant_fields_go_on_the_study_once(plan):
    sr = [a for a in plan.assertions if a.target == "study" and a.method_file == "Sample_runs.csv"]
    assert {a.field for a in sr} == {"experimental_design", "country", "category", "growth_stage",
                                     "weight", "sex"}
    assert all(a.evidence["constant_over_rows"] == 8 for a in sr)


def test_run_descriptors_stay_on_the_run(plan):
    runs = [a for a in plan.assertions if a.target == "run"]
    assert len(runs) == 8 * 3 and {a.field for a in runs} == {"library_layout", "platform", "release_date"}


def test_project_row_curation_errors_are_kept_as_asserted(plan):
    proj = {a.field: a.value_text for a in plan.assertions if a.method_file == "Project.csv"}
    assert proj["isolation_location"] == "Feces"     # its samples say Cecum/Colon: a conflict, kept
    assert proj["category"] == "Sow"                 # gilts of ~25 kg: a benchmark item, kept
    assert "age" not in proj                          # empty in PGMD → no assertion


def test_nulls_are_skipped_not_loaded(plan):
    assert plan.nulls_skipped > 0
    assert not any(a.value_text in ("NA", "") for a in plan.assertions)


# ── edge cases from the full file ───────────────────────────────────────────

def test_list_values_are_kept_raw_with_parsed_list():
    pl = p.plan_project("PRJNA1", None, [row(Sex="male;female")])
    sex = next(a for a in pl.assertions if a.field == "sex")
    assert sex.value_text == "male;female" and sex.evidence["values"] == ["male", "female"]


def test_age_list_gives_derived_bounds():
    pl = p.plan_project("PRJNA1", None, [row(Runs="SRR1", Bio_Sample="SAMN1", Age="7 days; 21 days"),
                                         row(Runs="SRR2", Bio_Sample="SAMN2", Age="28 days")])
    got = {(a.key, a.field): (a.value_num, a.tier) for a in pl.assertions if a.field.startswith("age_")}
    assert got[("SAMN1", "age_min_days")] == (7, "derived") and got[("SAMN1", "age_max_days")] == (21, "derived")
    assert got[("SAMN2", "age_min_days")] == (28, "derived")


def test_malformed_accessions_are_rejected_not_repaired():
    rows = [row(Runs="ERR3015650_subreads", Bio_Sample="SAMEA5192554"),
            row(Runs="SRR2", Bio_Sample="SAMN123 days"), row(Runs="SRR3", Bio_Sample="SAMEA9")]
    valid, rejected, errors = p.split_valid_rows(rows)
    assert [r["Runs"] for r in valid] == ["SRR3"] and not errors
    assert [r["reason"] for r in rejected] == ["bad run accession", "bad BioSample accession"]


def test_duplicate_runs_stop_the_load():
    _, _, errors = p.split_valid_rows([row(Runs="SRR1"), row(Runs="SRR1")])
    assert errors and "duplicate" in errors[0]


# ── site dictionary and comparisons ─────────────────────────────────────────

def test_gut_sites_and_material():
    assert gut_sites("cecum mucosa") == {"UBERON:0001153"} == gut_sites("Caecal content")
    assert gut_sites("Rectum;Stomach;Intestine;Colon") == {"UBERON:0001052", "UBERON:0000945", "UBERON:0001155"}
    assert material("colon digesta") == "digesta" and material("Cecum") is None


@pytest.mark.parametrize("pgmd,ncbi,verdict", [
    ("Cecum", ["cecum mucosa", "cecum"], "agree"),
    ("Colon", ["Rectal swab"], "differ"),                      # PRJNA687128's NCBI template default
    ("Rectum;Stomach;Intestine;Colon", ["colon"], "granularity"),
    ("Intestine", ["colon"], "unmapped"),
    ("Cecum", [], "missing")])
def test_compare_site(pgmd, ncbi, verdict):
    assert compare_site(pgmd, ncbi)[0] == verdict


def test_compare_platform_and_text():
    assert compare_platform("Illumina MiSeq", "ILLUMINA", "Illumina MiSeq")[0] == "agree"
    assert compare_platform("Illumina", "ILLUMINA", "Illumina MiSeq")[0] == "granularity"
    assert compare_platform("Ion Torrent", "ILLUMINA", "Illumina MiSeq")[0] == "differ"
    assert compare_text("HF+XY", "hf+xy")[0] == "agree" and compare_text(None, "x")[0] == "missing"
