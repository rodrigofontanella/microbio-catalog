"""Paper loader pure parts: raw-folder checksum, document row, Data Availability link. No database."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.ingest.paper import data_availability_evidence, document_row, verify_raw

RAW = Path(__file__).parent / "fixtures" / "europepmc" / "PMC8024495"


def test_verify_raw_passes_on_real_folder():
    assert set(verify_raw(RAW)) == {"core.json", "fulltext.xml"}


def test_verify_raw_refuses_a_changed_file(tmp_path):
    d = tmp_path / "PMC8024495"
    shutil.copytree(RAW, d)
    (d / "fulltext.xml").chmod(0o644)
    with (d / "fulltext.xml").open("ab") as fh:
        fh.write(b"\n")
    with pytest.raises(SystemExit, match="sha256 does not match"):
        verify_raw(d)


def test_document_row():
    row = document_row((RAW / "core.json").read_bytes(), "PMC8024495")
    assert (row["source"], row["ext_id"], row["pmid"], row["doi"]) == (
        "MED", "33841350", "33841350", "10.3389/fmicb.2021.619970")
    assert (row["kind"], row["license"], row["is_open_access"], row["pub_year"]) == ("paper", "cc by", True, 2021)


def test_document_row_refuses_other_pmcid():
    with pytest.raises(SystemExit, match="0 records"):
        document_row((RAW / "core.json").read_bytes(), "PMC1")


def test_data_availability_names_the_bioproject():
    ev = data_availability_evidence((RAW / "fulltext.xml").read_bytes(), "PRJNA667518")
    assert ev is not None and "PRJNA667518" in ev["quote"]
    assert "Data Availability" in ev["section"]


def test_data_availability_does_not_link_another_study():
    assert data_availability_evidence((RAW / "fulltext.xml").read_bytes(), "PRJNA664026") is None
