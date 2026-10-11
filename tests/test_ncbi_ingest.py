"""Parser, validation, raw-file and E-utilities tests for the NCBI study ingest. No database.

Fixtures are real NCBI responses for PRJNA667518, fetched 2026-10-10/11: the BioProject record, and
BioSamples + run info trimmed to pens 101 and 102 (8 BioSamples).
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from app.eutils import EUtils
from app.ingest.ncbi_parse import (is_null, parse_bioproject, parse_biosamples, parse_runinfo,
                                   subject_values, validate)
from app.ingest.ncbi_study import Fetched, read_raw, write_raw
from app.ncbi_client import NCBIUnavailableError

FIX = Path(__file__).parent / "fixtures" / "ncbi" / "PRJNA667518"
BASE = "https://eutils.test/entrez/eutils"
BIOPROJECT_XML = (FIX / "bioproject.xml").read_bytes()


@pytest.fixture(scope="module")
def samples():
    return parse_biosamples((FIX / "biosample.xml").read_bytes())


@pytest.fixture(scope="module")
def runs():
    return parse_runinfo((FIX / "runinfo.csv").read_bytes())


@pytest.fixture(scope="module")
def project():
    return parse_bioproject(BIOPROJECT_XML, "667518")


# ── parsing ─────────────────────────────────────────────────────────────────

def test_bioproject(project):
    assert project.accession == "PRJNA667518"
    assert project.title == "16S rRNA gene sequencing on swine hindgut microbiota after xylanase supplementation"
    assert "individually-housed" in project.description     # evidence that one pen = one pig


def test_bioproject_whitespace_collapsed():
    xml = (b'<RecordSet><DocumentSummary><Project><ProjectID><ArchiveID accession="PRJNA1"/></ProjectID>'
           b"<ProjectDescr><Title>a   b\n c</Title></ProjectDescr></Project></DocumentSummary></RecordSet>")
    assert parse_bioproject(xml, "1").title == "a b c"


def test_biosamples_ids_and_free_attributes(samples):
    assert len(samples) == 8
    s = samples[0]
    assert s.accession.startswith("SAMN") and s.sra_accession.startswith("SRS")
    pen = next(a for a in s.attributes if a.attribute_name == "Pen")
    assert pen.harmonized_name is None and pen.field == "pen"          # free attribute: normalized name
    taxid = next(a for a in s.attributes if a.attribute_name == "host_taxid")
    assert taxid.field == "host_taxid" and taxid.value == "9825"        # harmonized attribute
    assert s.get("pen") == s.get("Pen") == pen.value                    # lookup ignores case


def test_four_samples_per_pen(samples):
    by_pen = {}
    for s in samples:
        by_pen.setdefault(s.get("Pen"), set()).add(s.get("env_local_scale"))
    assert by_pen == {p: {"cecum digesta", "cecum mucosa", "colon digesta", "colon mucosa"}
                      for p in ("101", "102")}


def test_runinfo(runs, samples):
    assert len(runs) == 8 and {r.library_layout for r in runs} == {"PAIRED"}
    assert {r.biosample for r in runs} == {s.accession for s in samples}
    r = runs[0]
    assert r.run.startswith("SRR") and r.experiment.startswith("SRX")
    assert isinstance(r.spots, int) and isinstance(r.release_date, date)


@pytest.mark.parametrize("value,null", [("missing", True), ("Not Applicable", True), ("", True),
                                        (None, True), ("HF", False), ("0", False)])
def test_insdc_nulls(value, null):
    assert is_null(value) is null


# ── validation ──────────────────────────────────────────────────────────────

def test_fixture_validates(project, samples, runs):
    v = validate(project, samples, runs, "Pen", ("Pen", "host_taxid"))
    assert v.ok, v.errors
    assert subject_values(samples, "Pen").keys() == {s.accession for s in samples}


def test_run_pointing_outside_project_is_an_error(project, samples, runs):
    bad = [dataclasses.replace(runs[0], biosample="SAMN00000000")] + runs[1:]
    assert any("not linked" in e for e in validate(project, samples, bad, "Pen", ()).errors)


def test_experiment_spanning_two_samples_is_an_error(project, samples, runs):
    bad = [dataclasses.replace(runs[1], experiment=runs[0].experiment)] + runs[2:] + [runs[0]]
    assert any("spans BioSamples" in e for e in validate(project, samples, bad, "Pen", ()).errors)


def test_subject_with_two_values_of_a_constant_attribute_is_an_error(project, samples, runs):
    s0 = samples[0]
    attrs = tuple(dataclasses.replace(a, value="999") if a.attribute_name == "host_taxid" else a
                  for a in s0.attributes)
    bad = [dataclasses.replace(s0, attributes=attrs)] + samples[1:]
    assert any("host_taxid" in e for e in validate(project, bad, runs, "Pen", ("host_taxid",)).errors)


def test_missing_subject_attribute_is_a_warning_not_an_error(project, samples, runs):
    v = validate(project, samples, runs, "host_subject_id", ())
    assert v.ok and any("lack 'host_subject_id'" in w for w in v.warnings)


# ── raw files ───────────────────────────────────────────────────────────────

def test_raw_roundtrip_and_tamper_check(tmp_path):
    f = Fetched("667518", BIOPROJECT_XML, (FIX / "biosample.xml").read_bytes(), (FIX / "runinfo.csv").read_bytes())
    d, manifest = write_raw(tmp_path, "PRJNA667518", f, datetime(2026, 10, 11, 8, 0, tzinfo=timezone.utc))
    assert d == tmp_path / "ncbi" / "20261011T080000Z" / "PRJNA667518"
    back, m2 = read_raw(d)
    assert back == f and m2 == manifest
    (d / "runinfo.csv").write_bytes(b"tampered\n")
    with pytest.raises(SystemExit, match="checksum"):
        read_raw(d)


# ── E-utilities transport ───────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_retries_429_then_succeeds_and_sends_identity():
    route = respx.get(f"{BASE}/esearch.fcgi").mock(side_effect=[
        httpx.Response(429), httpx.Response(200, json={"esearchresult": {"idlist": ["667518"]}})])
    async with EUtils(api_key="k", email="me@example.org", tool="ncbi-api", base_url=BASE, backoff_s=0) as eu:
        assert await eu.bioproject_uid("PRJNA667518") == "667518"
    assert route.call_count == 2
    q = route.calls.last.request.url.params
    assert q["api_key"] == "k" and q["tool"] == "ncbi-api" and q["term"] == "PRJNA667518[PRJA]"


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_in_http_200_body_is_retried():
    route = respx.get(f"{BASE}/esearch.fcgi").mock(side_effect=[
        httpx.Response(200, json={"error": "API rate limit exceeded", "count": "0"}),
        httpx.Response(200, json={"esearchresult": {"idlist": ["1"]}})])
    async with EUtils(base_url=BASE, backoff_s=0) as eu:
        assert await eu.bioproject_uid("PRJNA1") == "1"
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_gives_up_with_unavailable_error():
    respx.get(f"{BASE}/esearch.fcgi").mock(return_value=httpx.Response(503))
    async with EUtils(base_url=BASE, backoff_s=0, max_attempts=3) as eu:
        with pytest.raises(NCBIUnavailableError, match="after 3 attempts"):
            await eu.bioproject_uid("PRJNA1")


@pytest.mark.asyncio
@respx.mock
async def test_biosample_batches_are_posted_and_counted():
    xml = (FIX / "biosample.xml").read_bytes()
    route = respx.post(f"{BASE}/efetch.fcgi").mock(return_value=httpx.Response(200, content=xml))
    async with EUtils(base_url=BASE, backoff_s=0) as eu:
        out = await eu.biosamples_xml([str(i) for i in range(8)])
    assert route.call_count == 1 and len(parse_biosamples(out)) == 8
    async with EUtils(base_url=BASE, backoff_s=0) as eu:
        with pytest.raises(NCBIUnavailableError, match="returned 8 of 9"):
            await eu.biosamples_xml([str(i) for i in range(9)])


@pytest.mark.asyncio
@respx.mock
async def test_runinfo_pages_keep_one_header(monkeypatch):
    monkeypatch.setattr("app.eutils.SRA_PAGE", 5)
    lines = (FIX / "runinfo.csv").read_bytes().splitlines()
    header, body = lines[0], lines[1:]
    respx.get(f"{BASE}/esearch.fcgi").mock(return_value=httpx.Response(200, json={
        "esearchresult": {"count": "8", "webenv": "W", "querykey": "1"}}))
    pages = respx.get(f"{BASE}/efetch.fcgi").mock(side_effect=[
        httpx.Response(200, content=b"\n".join([header, *body[:5]]) + b"\n"),
        httpx.Response(200, content=b"\n".join([header, *body[5:]]) + b"\n")])
    async with EUtils(base_url=BASE, backoff_s=0) as eu:
        out = await eu.sra_runinfo("PRJNA667518")
    assert pages.call_count == 2
    assert pages.calls[1].request.url.params["retstart"] == "5"
    assert out.count(header) == 1 and len(parse_runinfo(out)) == 8
