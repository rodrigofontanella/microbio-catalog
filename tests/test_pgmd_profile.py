"""PGMD profile job: pure lineage functions, then the Spark transforms on a small synthetic fixture.

The fixture imitates Microbial.csv's shape (long format, every level stacked, 7-segment Species lineages, '__' for
empty ranks, PGMD's labels). Lineages and counts are invented for the test, not biology. Spark runs in local[1]
with no Delta and no network: Delta writes and s3a are proven on Leeloo only (Claude's sandbox has no Maven).
"""
from __future__ import annotations

import csv
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.lake import lineage as lin
from app.lake import pgmd_profile as job

# ── pure functions ─────────────────────────────────────────────────────────────────────────────

FAMILY_ONLY = "Bacteria;Firmicutes;Clostridia;Clostridiales;Christensenellaceae;__;__"


def test_split_and_normalize_strip_whitespace():
    assert lin.split_lineage("Bacteria; Firmicutes ;__") == ["Bacteria", "Firmicutes", "__"]
    assert lin.normalize("Bacteria; Firmicutes ;__") == "Bacteria;Firmicutes;__"


def test_resolution_comes_from_the_lineage_not_the_level():
    assert lin.resolved_depth(lin.split_lineage(FAMILY_ONLY)) == 5
    assert lin.resolved_depth(lin.split_lineage("Bacteria;__;__;__;__;__;__")) == 1
    assert lin.resolved_depth(lin.split_lineage("__;__")) == 0


def test_taxon_row_keeps_named_ranks_and_flags_gaps():
    t = lin.taxon_row(FAMILY_ONLY)
    assert (t["family"], t["genus"], t["species"]) == ("Christensenellaceae", None, None)
    assert (t["resolved_rank"], t["resolved_depth"], t["has_gap"]) == ("family", 5, False)
    g = lin.taxon_row("Bacteria;__;Clostridia;__;__;__;__")
    assert (g["class"], g["resolved_rank"], g["has_gap"]) == ("Clostridia", "kingdom", True)
    assert lin.taxon_row("__;__;__;__;__;__;__")["resolved_rank"] is None


def test_taxon_row_refuses_a_shape_change():
    with pytest.raises(ValueError, match="expected 7 segments, got 6"):
        lin.taxon_row("Bacteria;Firmicutes;Clostridia;Clostridiales;Christensenellaceae;__")


def test_genus_key_is_the_first_six_segments():
    assert lin.genus_key(FAMILY_ONLY) == "Bacteria;Firmicutes;Clostridia;Clostridiales;Christensenellaceae;__"


def test_profile_key_and_paths_come_from_settings():
    assert job.profile_key("figshare:10.6084/m9.figshare.25911745.v1", "PRJNA667518") == \
        "pgmd|figshare:10.6084/m9.figshare.25911745.v1|PRJNA667518|taxon_collapsed|species"
    p = job.Paths.from_settings(Settings(_env_file=None, lake_uri="s3a://lake/", cold_uri="s3a://cold"))
    assert p.csv == "s3a://cold/raw/pgmd/v1/Microbial.csv"
    assert p.sums == "s3a://cold/raw/pgmd/v1/pgmd-v1.SHA256SUMS"
    assert (p.bronze, p.profile_abundance) == ("s3a://lake/bronze/pgmd_microbial_raw", "s3a://lake/silver/profile_abundance")


# ── Spark transforms on the fixture ────────────────────────────────────────────────────────────

pyspark = pytest.importorskip("pyspark")
needs_java = pytest.mark.skipif(shutil.which("java") is None, reason="no Java on PATH")

SPECIES = {   # lineage → (PGMD label, {run: count})
    "Bacteria;Firmicutes;Clostridia;Clostridiales;Ruminococcaceae;Faecalibacterium;prausnitzii":
        ("prausnitzii", {"SRR0000001": 100, "SRR0000002": 40}),
    "Bacteria;Firmicutes;Clostridia;Clostridiales;Ruminococcaceae;Oscillospira;__":
        ("Unknown", {"SRR0000001": 50}),
    "Bacteria;Firmicutes;Clostridia;Clostridiales;Ruminococcaceae;Oscillospira;guilliermondii":
        ("guilliermondii", {"SRR0000001": 10, "SRR0000002": 5}),
    "Bacteria;Bacteroidetes;Bacteroidia;Bacteroidales;Prevotellaceae;Prevotella;copri":
        ("copri", {"SRR0000001": 200, "SRR0000002": 300}),
    FAMILY_ONLY: ("Unknown", {"SRR0000001": 30, "SRR0000002": 7}),
    "Bacteria;Firmicutes;Clostridia;Clostridiales;Lachnospiraceae;Ruminococcus;__":
        ("Unknown", {"SRR0000002": 20}),
    "Bacteria;Firmicutes;Bacilli;Lactobacillales;Lactobacillaceae;Lactobacillus;p-1630-c5":
        ("p-1630-c5", {"SRR0000001": 9, "SRR0000002": 1}),
    "Bacteria;__;__;__;__;__;__": ("Unknown", {"SRR0000002": 2}),
}
OTHER_PROJECT = "PRJNA0000002"
N_SPECIES_ROWS = sum(len(runs) for _, runs in SPECIES.values())


def fixture_rows(genus_override: dict | None = None) -> list[dict[str, str]]:
    """Species rows, Genus rows summed from them (PGMD's own roll-up), Phylum rows, and one other project."""
    rows, genus, phylum = [], {}, {}
    for lineage, (label, runs) in SPECIES.items():
        for run, c in runs.items():
            rows.append({"microbial_name": label, "specific_taxonomy": lineage, "level": "Species", "count": str(c),
                         "Runs": run, "BioProject": "PRJNA0000001"})
            gk = (run, lin.genus_key(lineage))
            genus[gk] = genus.get(gk, 0) + c
            pk = (run, ";".join(lin.split_lineage(lineage)[:2]))
            phylum[pk] = phylum.get(pk, 0) + c
    genus.update(genus_override or {})
    for (run, g), c in sorted(genus.items()):
        rows.append({"microbial_name": g.split(";")[-1], "specific_taxonomy": g, "level": "Genus", "count": str(c),
                     "Runs": run, "BioProject": "PRJNA0000001"})
    for (run, p), c in sorted(phylum.items()):
        rows.append({"microbial_name": p.split(";")[-1], "specific_taxonomy": p, "level": "Phylum", "count": str(c),
                     "Runs": run, "BioProject": "PRJNA0000001"})
    rows.append({"microbial_name": "copri", "specific_taxonomy": "Bacteria;Bacteroidetes;Bacteroidia;Bacteroidales;"
                 "Prevotellaceae;Prevotella;copri", "level": "Species", "count": "7", "Runs": "SRR0000009",
                 "BioProject": OTHER_PROJECT})
    for i, r in enumerate(rows, 1):
        r["id"], r["abundance"] = str(i), "0.0"
    return rows


def write_csv(path: Path, rows: list[dict[str, str]], header=job.CSV_COLUMNS) -> Path:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow([r[c] for c in job.CSV_COLUMNS])
    return path


@pytest.fixture(scope="module")
def spark():
    from pyspark.sql import SparkSession
    s = (SparkSession.builder.master("local[1]").appName("test-pgmd-profile")
         .config("spark.sql.shuffle.partitions", "2").config("spark.ui.enabled", "false")
         .config("spark.sql.session.timeZone", "UTC").getOrCreate())
    yield s
    s.stop()


LOADED = datetime(2026, 10, 13, 18, 0, tzinfo=timezone.utc)
KEY = "pgmd|test|PRJNA0000001|taxon_collapsed|species"


def study_and_species(spark, tmp_path, rows):
    from pyspark.sql import functions as F
    raw = job.read_csv(spark, str(write_csv(tmp_path / "Microbial.csv", rows)))
    bronze = job.stamp_bronze(raw, "test", "s3a://cold/raw/pgmd/v1/Microbial.csv", "ab" * 32, 7, LOADED)
    study = bronze.filter(F.trim("BioProject") == "PRJNA0000001")
    return bronze, study, study.filter(F.col("level") == "Species")


@needs_java
def test_bronze_keeps_every_row_and_level_as_strings(spark, tmp_path):
    bronze, study, _ = study_and_species(spark, tmp_path, fixture_rows())
    assert [f.name for f in bronze.schema] == [f.name for f in job.bronze_schema()]
    assert bronze.count() == len(fixture_rows())
    assert {f.dataType.simpleString() for f in bronze.schema if f.name in job.CSV_COLUMNS} == {"string"}
    assert job.level_counts(study) == {"Species": N_SPECIES_ROWS, "Genus": 12, "Phylum": 5}
    row = bronze.filter("id = '1'").first()
    assert (row["_source"], row["_ingest_run_id"], row["_source_sha256"]) == ("pgmd", 7, "ab" * 32)


@needs_java
def test_a_changed_header_stops_before_reading(spark, tmp_path):
    good = write_csv(tmp_path / "good.csv", fixture_rows())
    assert job.check_header(spark, str(good)) == list(job.CSV_COLUMNS)
    bom = tmp_path / "bom.csv"
    bom.write_bytes(b"\xef\xbb\xbf" + good.read_bytes())       # a BOM is tolerated, as the survey's utf-8-sig did
    assert job.check_header(spark, str(bom)) == list(job.CSV_COLUMNS)
    assert job.read_csv(spark, str(bom)).filter("id = '1'").first()["Runs"] == "SRR0000001"
    renamed = write_csv(tmp_path / "renamed.csv", fixture_rows(),
                        header=("id", "microbial_name", "taxonomy", "level", "count", "abundance", "Runs", "BioProject"))
    with pytest.raises(job.Stop, match="header"):
        job.check_header(spark, str(renamed))
    with pytest.raises(Exception, match="(?i)does not conform"):   # Spark itself, once the column is read
        job.read_csv(spark, str(renamed)).collect()


@needs_java
def test_species_checks_pass_on_clean_rows_and_name_each_problem(spark, tmp_path):
    _, _, species = study_and_species(spark, tmp_path, fixture_rows())
    p = job.species_problems(species)
    assert p["rows"] == N_SPECIES_ROWS
    assert {k: v for k, v in p.items() if k != "rows"} == {
        "null_keys": 0, "non_integer_counts": 0, "wrong_segment_count": 0, "zero_counts": 0,
        "duplicate_run_features": 0, "features_with_two_labels": 0, "lineages_not_normalized": 0}

    bad = fixture_rows()
    sp = [r for r in bad if r["level"] == "Species"]
    sp[0]["count"] = "12.5"
    sp[1]["specific_taxonomy"] = "Bacteria;Firmicutes"
    bad.append(dict(sp[2]))                                   # the same (run, lineage) twice
    sp[3]["microbial_name"] = "another label"                 # copri labelled two ways
    _, _, species = study_and_species(spark, tmp_path, bad)
    p = job.species_problems(species)
    assert (p["non_integer_counts"], p["wrong_segment_count"], p["duplicate_run_features"],
            p["features_with_two_labels"]) == (1, 1, 1, 1)


@needs_java
def test_abundance_recomputes_proportions_from_counts(spark, tmp_path):
    from pyspark.sql import functions as F
    _, _, species = study_and_species(spark, tmp_path, fixture_rows())
    ab = job.build_abundance(species, KEY, "ab" * 32, 3, 7, LOADED)
    assert [f.name for f in ab.schema] == [f.name for f in job.abundance_schema()]
    assert ab.count() == N_SPECIES_ROWS
    sums = {r["run_acc"]: r["s"] for r in ab.groupBy("run_acc").agg(F.sum("rel_abund").alias("s")).collect()}
    assert sums.keys() == {"SRR0000001", "SRR0000002"} and all(abs(v - 1) < 1e-12 for v in sums.values())
    copri = ab.filter("run_acc = 'SRR0000001' AND feature_id LIKE '%;copri'").first()
    assert (copri["count"], copri["rel_abund"]) == (200, 200 / 399)
    stats = {r["run_acc"]: (r["n_reads"], r["n_features"]) for r in job.per_run_stats(ab)}
    assert stats == {"SRR0000001": (399, 6), "SRR0000002": (375, 7)}


@needs_java
def test_genus_test_passes_when_species_roll_up_and_names_the_failure(spark, tmp_path):
    _, study, species = study_and_species(spark, tmp_path, fixture_rows())
    gt = job.genus_test(job.build_abundance(species, KEY, "ab" * 32, 3, 7, LOADED), study)
    assert (gt["passed"], gt["keys"], gt["equal"], gt["runs"], gt["runs_all_equal"]) == (True, 12, 12, 2, 2)

    osc = ("SRR0000001", "Bacteria;Firmicutes;Clostridia;Clostridiales;Ruminococcaceae;Oscillospira")
    _, study, species = study_and_species(spark, tmp_path, fixture_rows({osc: 61}))
    gt = job.genus_test(job.build_abundance(species, KEY, "ab" * 32, 3, 7, LOADED), study)
    assert not gt["passed"]
    assert gt["verdicts"] == {"equal": 11, "species_smaller": 1} and gt["runs_all_equal"] == 1
    assert gt["examples"][0]["genus_row"] == 61 and gt["examples"][0]["species_sum"] == 60


@needs_java
def test_feature_taxon_rows_fit_their_schema(spark, tmp_path):
    _, _, species = study_and_species(spark, tmp_path, fixture_rows())
    rows = job.taxon_rows(species, KEY, 7, LOADED)
    df = spark.createDataFrame(rows, job.taxon_schema())
    assert df.count() == len(SPECIES)
    fam = df.filter(f"feature_id = '{FAMILY_ONLY}'").first()
    assert (fam["family"], fam["genus"], fam["resolved_rank"], fam["external_label"]) == \
        ("Christensenellaceae", None, "family", "Unknown")


@needs_java
def test_reference_evidence_counts_the_greengenes_markers(spark, tmp_path):
    _, _, species = study_and_species(spark, tmp_path, fixture_rows())
    assert job.reference_evidence(species) == {
        "distinct_lineages": len(SPECIES), "silva_style_names": 0, "bracketed_names": 0,
        "gg_ruminococcaceae_oscillospira": 2, "gg_placeholder_p_1630_c5": 1}


@needs_java
def test_sha256_streams_through_the_jvm_and_sums_are_parsed(spark, tmp_path):
    path = write_csv(tmp_path / "Microbial.csv", fixture_rows())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert job.sha256_of(spark, path.as_uri()) == digest
    sums = tmp_path / "pgmd-v1.SHA256SUMS"
    sums.write_text(f"{'0' * 64}  Project.csv\n{digest}  Microbial.csv\n")
    assert job.expected_sha256(spark, sums.as_uri(), "Microbial.csv") == digest
    with pytest.raises(job.Stop, match="Lefse.csv is not listed"):
        job.expected_sha256(spark, sums.as_uri(), "Lefse.csv")
