"""The orphan-run survey's classification (app.reports.pgmd_orphans), without NCBI, Spark or a database.
Accessions are made up in the shapes the 2026-11-07 dry run showed."""
from __future__ import annotations

import pytest

from app.reports.pgmd_orphans import CLASSES, classify, summary, survey_study

SRA = {"SRR13518762", "SRR13518763", "ERR3015650", "SRR6109350"}


@pytest.mark.parametrize("orphan, cls, stripped", [
    ("SRR13518762", "in_sra_project", None),                 # real run, missing from PGMD's Sample_runs
    ("SRR99999999", "valid_not_in_project", None),           # well-formed, not listed under this BioProject
    ("ERR3015650_subreads", "suffix_in_sra_project", "ERR3015650"),
    ("SRR6109350 days", "suffix_in_sra_project", "SRR6109350"),
    ("SRR11111111_x", "suffix_not_in_project", "SRR11111111"),
    ("sample1", "not_an_accession", None),                   # PRJNA707602's shape
    ("SRS1234567", "not_an_accession", None),                # a sample accession is not a run
    ("SRR", "not_an_accession", None),
])
def test_classify(orphan, cls, stripped):
    assert classify(orphan, SRA) == (cls, stripped)


def test_survey_study_counts_and_what_ncbi_would_add():
    profiled = {"SRR13518762", "SRR13518763", "SRR0000001", "ERR3015650_subreads", "sample1"}
    core = {"SRR0000001", "ERR3015650"}                      # the stripped run is already a core.run row
    s = survey_study("PRJNA0000002", profiled, core, SRA)
    assert s.orphans == 4 and s.core_runs == 2 and s.profiled_runs == 5
    assert s.classes["in_sra_project"] == ["SRR13518762", "SRR13518763"]
    assert s.classes["suffix_in_sra_project"] == ["ERR3015650_subreads"] and s.stripped_in_core == 1
    assert s.classes["not_an_accession"] == ["sample1"]
    assert s.sra_not_in_core == 3                            # SRA runs core.run lacks: what the NCBI ingest adds


def test_summary_rows_and_totals():
    a = survey_study("PRJNA0000002", {"SRR13518762", "sample1"}, set(), SRA)
    b = survey_study("PRJNA0000003", {"SRR99999999"}, {"SRR6109350"}, SRA)
    out = summary([a, b]).splitlines()
    header = out[0].split("\t")
    assert header[:4] == ["bioproject", "core_runs", "profiled_runs", "orphans"] and header[4:9] == list(CLASSES)
    assert out[1].split("\t")[:9] == ["PRJNA0000002", "0", "2", "2", "1", "0", "0", "0", "1"]
    assert out[-1] == ("totals: 2 studies, 3 orphans: in_sra_project 1, suffix_in_sra_project 0, "
                       "suffix_not_in_project 0, valid_not_in_project 1, not_an_accession 1")
    detail = summary([a], detail=True).splitlines()
    assert detail == ["bioproject\torphan\tclass", "PRJNA0000002\tSRR13518762\tin_sra_project",
                      "PRJNA0000002\tsample1\tnot_an_accession"]
