"""Async client for NCBI E-utilities (BioProject, BioSample, SRA).

Separate from `NCBIClient`, which talks to the Datasets v2 API. E-utilities limits:
3 requests/s without an API key, 10/s with one. Above that NCBI answers 429, or sometimes
HTTP 200 with a JSON `error` body, so both are retried.
"""
from __future__ import annotations

import asyncio
import json
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping

import httpx

from app.config import settings
from app.ncbi_client import NCBINotFoundError, NCBIUnavailableError

RETRY_STATUS = {429, 500, 502, 503, 504}
BIOSAMPLE_BATCH = 200   # ids per efetch POST
SRA_PAGE = 500          # SRA experiment UIDs per runinfo page


class EUtils:
    def __init__(self, *, api_key: str = "", email: str = "", tool: str = "ncbi-api",
                 base_url: str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
                 client: httpx.AsyncClient | None = None, max_attempts: int = 6,
                 backoff_s: float = 1.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.email = email
        self.tool = tool
        self.min_interval = 0.11 if api_key else 0.34
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        self._client = client or httpx.AsyncClient(timeout=60.0)
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.calls = 0

    @classmethod
    def from_settings(cls) -> "EUtils":
        return cls(api_key=settings.ncbi_api_key, email=settings.ncbi_email,
                   tool=settings.ncbi_tool, base_url=settings.eutils_base_url)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "EUtils":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ── transport ───────────────────────────────────────────────────────────

    def _common(self) -> dict[str, str]:
        p = {"tool": self.tool}
        if self.email:
            p["email"] = self.email
        if self.api_key:
            p["api_key"] = self.api_key
        return p

    async def _pace(self) -> None:
        async with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def _request(self, endpoint: str, params: Mapping[str, str], *, post: bool = False) -> bytes:
        url = f"{self.base_url}/{endpoint}.fcgi"
        payload = {**self._common(), **params}
        last_error = ""
        for attempt in range(self.max_attempts):
            await self._pace()
            self.calls += 1
            try:
                r = (await self._client.post(url, data=payload) if post
                     else await self._client.get(url, params=payload))
            except httpx.TransportError as e:
                last_error = f"{type(e).__name__}: {e}"
            else:
                if r.status_code in RETRY_STATUS:
                    last_error = f"HTTP {r.status_code}"
                    retry_after = r.headers.get("Retry-After", "")
                    if retry_after.isdigit():
                        await asyncio.sleep(float(retry_after))
                        continue
                elif r.status_code == 404:
                    raise NCBINotFoundError(f"{endpoint}: 404 for {dict(params)}")
                else:
                    r.raise_for_status()
                    if b"API rate limit exceeded" in r.content[:500]:
                        last_error = "rate limit (HTTP 200 body)"
                    else:
                        return r.content
            await asyncio.sleep(self.backoff_s * 2 ** attempt)
        raise NCBIUnavailableError(f"{endpoint} failed after {self.max_attempts} attempts: {last_error}")

    async def _json(self, endpoint: str, **params: str) -> dict:
        return json.loads(await self._request(endpoint, {**params, "retmode": "json"}))

    # ── BioProject / BioSample / SRA ────────────────────────────────────────

    async def bioproject_uid(self, accession: str) -> str:
        ids = (await self._json("esearch", db="bioproject", term=f"{accession}[PRJA]"))["esearchresult"]["idlist"]
        if not ids:
            raise NCBINotFoundError(f"BioProject {accession} not found")
        if len(ids) > 1:
            raise NCBIUnavailableError(f"BioProject {accession} matched {len(ids)} records: {ids}")
        return ids[0]

    async def bioproject_xml(self, uid: str) -> bytes:
        return await self._request("efetch", {"db": "bioproject", "id": uid, "retmode": "xml"})

    async def biosample_uids(self, bioproject_uid: str) -> list[str]:
        res = await self._json("elink", dbfrom="bioproject", db="biosample", id=bioproject_uid)
        for linkset in res.get("linksets", []):
            for db in linkset.get("linksetdbs", []):
                if db.get("linkname") == "bioproject_biosample":
                    return sorted({str(x) for x in db.get("links", [])}, key=int)
        return []

    async def biosamples_xml(self, uids: list[str]) -> bytes:
        """All BioSamples in one <BioSampleSet>, fetched in POST batches."""
        parts: list[bytes] = []
        for i in range(0, len(uids), BIOSAMPLE_BATCH):
            chunk = uids[i:i + BIOSAMPLE_BATCH]
            xml = await self._request("efetch", {"db": "biosample", "id": ",".join(chunk), "retmode": "xml"},
                                      post=True)
            got = ET.fromstring(xml).findall("BioSample")
            if len(got) != len(chunk):
                raise NCBIUnavailableError(f"efetch biosample returned {len(got)} of {len(chunk)} records")
            parts.extend(ET.tostring(bs, encoding="utf-8") for bs in got)
        return b'<?xml version="1.0" encoding="UTF-8"?>\n<BioSampleSet>' + b"".join(parts) + b"</BioSampleSet>\n"

    async def sra_runinfo(self, bioproject_acc: str) -> bytes:
        """SRA run info CSV for a BioProject, paged by SRA experiment UID; one header line."""
        res = (await self._json("esearch", db="sra", term=f"{bioproject_acc}[BioProject]",
                                usehistory="y"))["esearchresult"]
        count = int(res.get("count", 0))
        if count == 0:
            return b""
        header: bytes | None = None
        body: list[bytes] = []
        for start in range(0, count, SRA_PAGE):
            page = await self._request("efetch", {"db": "sra", "rettype": "runinfo", "retmode": "text",
                                                  "WebEnv": res["webenv"], "query_key": res["querykey"],
                                                  "retstart": str(start), "retmax": str(SRA_PAGE)})
            lines = [ln for ln in page.splitlines() if ln.strip()]
            if not lines:
                continue
            if header is None:
                header = lines[0]
            body.extend(ln for ln in lines[1:] if ln != header)
        return b"\n".join([header or b"", *body]) + b"\n"
