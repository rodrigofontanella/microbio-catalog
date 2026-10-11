"""Europe PMC fetch: metadata parsing, JATS outline, raw files, retries. No network, no database."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from app.ingest.europepmc import EuropePMC, outline, parse_core, write_raw, xml_license
from app.ncbi_client import NCBIUnavailableError

BASE = "https://epmc.test/rest"
CORE = json.dumps({"resultList": {"result": [{
    "pmcid": "PMC8024495", "pmid": "33841350", "doi": "10.3389/fmicb.2021.619970", "pubYear": "2021",
    "title": "Xylanase …", "license": "cc by", "isOpenAccess": "Y", "inEPMC": "Y",
    "journalInfo": {"journal": {"title": "Frontiers in microbiology"}}}]}}).encode()
JATS = b"""<article xmlns:xlink="http://www.w3.org/1999/xlink"><front><article-meta><permissions>
<license xlink:href="https://creativecommons.org/licenses/by/4.0/"><license-p>CC BY</license-p></license>
</permissions></article-meta></front><body>
<sec><title>Introduction</title><p>a</p><p>b</p></sec>
<sec><title>Materials and Methods</title>
  <sec><title>Animals and <italic>Housing</italic></title><p>individually housed</p></sec>
  <sec><title>DNA Extraction</title><p>x</p></sec></sec>
<sec><title>Results</title><p>r</p>
  <table-wrap id="T1"><label>Table 1</label><caption><p>Relative abundance of genera by diet</p></caption></table-wrap>
  <fig id="F2"><label>Figure 2</label><caption><title>Alpha diversity</title></caption></fig></sec>
</body></article>"""


def test_parse_core():
    m = parse_core(CORE, "PMC8024495")
    assert (m.pmid, m.doi, m.license, m.is_open_access, m.in_epmc) == (
        "33841350", "10.3389/fmicb.2021.619970", "cc by", True, True)


def test_parse_core_refuses_ambiguity():
    with pytest.raises(ValueError, match="0 records"):
        parse_core(CORE, "PMC1")


def test_outline_and_license():
    lines = outline(JATS)
    assert lines[:4] == ["§ Introduction  [2 ¶]", "§ Materials and Methods  [0 ¶]",
                         "  § Animals and Housing  [1 ¶]", "  § DNA Extraction  [1 ¶]"]
    assert "table Table 1: Relative abundance of genera by diet" in lines
    assert "figure Figure 2: Alpha diversity" in lines
    assert xml_license(JATS) == "https://creativecommons.org/licenses/by/4.0/"


def test_write_raw(tmp_path):
    d, m = write_raw(tmp_path, "PMC1", {"core.json": b"{}", "fulltext.xml": b"<a/>"},
                     datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc))
    assert d == tmp_path / "europepmc" / "20261011T090000Z" / "PMC1"
    assert (d / "SHA256SUMS").read_text().count("\n") == 2 and set(m) == {"core.json", "fulltext.xml"}


@pytest.mark.asyncio
@respx.mock
async def test_retry_then_ok_and_404_is_none():
    respx.get(f"{BASE}/search").mock(side_effect=[httpx.Response(503), httpx.Response(200, content=CORE)])
    respx.get(f"{BASE}/PMC1/supplementaryFiles").mock(return_value=httpx.Response(404))
    async with EuropePMC(base_url=BASE, backoff_s=0, min_interval=0) as e:
        assert await e.core("PMC8024495") == CORE
        assert await e.supplementary("PMC1") is None


@pytest.mark.asyncio
@respx.mock
async def test_gives_up():
    respx.get(f"{BASE}/search").mock(return_value=httpx.Response(429))
    async with EuropePMC(base_url=BASE, backoff_s=0, min_interval=0, max_attempts=2) as e:
        with pytest.raises(NCBIUnavailableError, match="after 2 attempts"):
            await e.core("PMC1")
