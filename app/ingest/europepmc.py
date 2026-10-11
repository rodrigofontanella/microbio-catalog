"""Fetch an open-access paper from Europe PMC and keep it as raw files with checksums.

    python -m app.ingest.europepmc PMC8024495

Writes $RAW_DIR/europepmc/<UTC timestamp>/<PMCID>/:
  core.json          search result, resultType=core (identifiers, licence, OA flags, abstract)
  fulltext.xml       JATS full text (only fetched when Europe PMC says it is open access)
  supplementary.zip  only with --supplementary: Europe PMC zips ALL supplementary material,
                     figures included, so it can be very large
  SHA256SUMS         checkable with `sha256sum -c`
and prints the identifiers, the licence and an outline (sections, tables, figures) of the paper.
No database writes: loading the paper is a separate step.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import settings
from app.ncbi_client import NCBINotFoundError, NCBIUnavailableError

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
RETRY = {429, 500, 502, 503, 504}


class EuropePMC:
    def __init__(self, base_url: str = BASE, client: httpx.AsyncClient | None = None,
                 max_attempts: int = 5, backoff_s: float = 1.0, min_interval: float = 0.2) -> None:
        self.base_url = base_url.rstrip("/")
        # fail fast on a dead connection; allow a slow but live transfer
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=15.0), follow_redirects=True)
        self._owns = client is None
        self.max_attempts, self.backoff_s, self.min_interval = max_attempts, backoff_s, min_interval
        self.calls = 0

    async def __aenter__(self) -> "EuropePMC":
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns:
            await self._client.aclose()

    async def _get(self, path: str, params: dict[str, str] | None = None, *, allow_404: bool = False) -> bytes | None:
        last = ""
        for attempt in range(self.max_attempts):
            await asyncio.sleep(self.min_interval)
            self.calls += 1
            if attempt:
                print(f"  retry {attempt}/{self.max_attempts - 1} for {path}: {last}", file=sys.stderr, flush=True)
            try:
                r = await self._client.get(f"{self.base_url}/{path}", params=params)
            except httpx.TransportError as e:
                last = f"{type(e).__name__}: {e}"
            else:
                if r.status_code == 404 and allow_404:
                    return None
                if r.status_code == 404:
                    raise NCBINotFoundError(f"Europe PMC 404 for {path}")
                if r.status_code not in RETRY:
                    r.raise_for_status()
                    return r.content
                last = f"HTTP {r.status_code}"
            await asyncio.sleep(self.backoff_s * 2 ** attempt)
        raise NCBIUnavailableError(f"Europe PMC {path} failed after {self.max_attempts} attempts: {last}")

    async def core(self, pmcid: str) -> bytes:
        return await self._get("search", {"query": f"PMCID:{pmcid}", "resultType": "core", "format": "json"})

    async def full_text(self, pmcid: str) -> bytes | None:
        return await self._get(f"{pmcid}/fullTextXML", allow_404=True)

    async def supplementary(self, pmcid: str) -> bytes | None:
        return await self._get(f"{pmcid}/supplementaryFiles", allow_404=True)


@dataclass(frozen=True)
class PaperMeta:
    pmcid: str
    pmid: str | None
    doi: str | None
    title: str | None
    journal: str | None
    year: str | None
    license: str | None
    is_open_access: bool
    in_epmc: bool


def parse_core(core_json: bytes, pmcid: str) -> PaperMeta:
    hits = [h for h in json.loads(core_json).get("resultList", {}).get("result", []) if h.get("pmcid") == pmcid]
    if len(hits) != 1:
        raise ValueError(f"Europe PMC returned {len(hits)} records for {pmcid}")
    h = hits[0]
    return PaperMeta(pmcid=pmcid, pmid=h.get("pmid"), doi=h.get("doi"), title=h.get("title"),
                     journal=(h.get("journalInfo") or {}).get("journal", {}).get("title"),
                     year=h.get("pubYear"), license=h.get("license"),
                     is_open_access=h.get("isOpenAccess") == "Y", in_epmc=h.get("inEPMC") == "Y")


def _text(el: ET.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def outline(jats: bytes) -> list[str]:
    """Section tree with paragraph counts, then tables and figures with their captions (first 90 chars)."""
    root = ET.fromstring(jats)
    lines: list[str] = []

    def walk(sec: ET.Element, depth: int) -> None:
        title = _text(sec.find("title")) or "(untitled)"
        paras = len(sec.findall("p"))
        lines.append(f"{'  ' * depth}§ {title}  [{paras} ¶]")
        for child in sec.findall("sec"):
            walk(child, depth + 1)

    body = root.find(".//body")
    for sec in (body.findall("sec") if body is not None else []):
        walk(sec, 0)
    for kind, tag in (("table", "table-wrap"), ("figure", "fig")):
        for el in root.iter(tag):
            label = _text(el.find("label")) or el.get("id", "?")
            cap = _text(el.find("caption"))
            lines.append(f"{kind} {label}: {cap[:90]}{'…' if len(cap) > 90 else ''}")
    return lines


def xml_license(jats: bytes) -> str | None:
    lic = ET.fromstring(jats).find(".//permissions/license")
    if lic is None:
        return None
    href = next((v for k, v in lic.attrib.items() if k.endswith("href")), None)
    return href or _text(lic)[:120] or None


def write_raw(root: Path, pmcid: str, files: dict[str, bytes], at: datetime) -> tuple[Path, dict[str, str]]:
    d = root / "europepmc" / at.strftime("%Y%m%dT%H%M%SZ") / pmcid
    d.mkdir(parents=True, exist_ok=False)
    manifest = {}
    for name, data in files.items():
        (d / name).write_bytes(data)
        manifest[name] = hashlib.sha256(data).hexdigest()
    (d / "SHA256SUMS").write_text("".join(f"{h}  {n}\n" for n, h in manifest.items()))
    return d, manifest


async def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.ingest.europepmc", description=__doc__.splitlines()[0])
    ap.add_argument("pmcid", help="e.g. PMC8024495")
    ap.add_argument("--supplementary", action="store_true",
                    help="also download the supplementary zip (can be large and slow)")
    args = ap.parse_args(argv)
    pmcid = args.pmcid.strip().upper()
    if not re.fullmatch(r"PMC\d+", pmcid):
        print(f"ERROR not a PMCID: {args.pmcid!r}", file=sys.stderr)
        return 2
    if not settings.raw_dir:
        print("RAW_DIR is not set (env or .env)", file=sys.stderr)
        return 2

    def step(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    async with EuropePMC() as epmc:
        step(f"{pmcid}: metadata …")
        core = await epmc.core(pmcid)
        meta = parse_core(core, pmcid)
        files = {"core.json": core}
        step(f"  {len(core):,} bytes; open access: {meta.is_open_access}, in EPMC: {meta.in_epmc}")
        if meta.is_open_access and meta.in_epmc:
            step(f"{pmcid}: full text …")
            xml = await epmc.full_text(pmcid)
            if xml:
                files["fulltext.xml"] = xml
                step(f"  {len(xml):,} bytes")
            if args.supplementary:
                step(f"{pmcid}: supplementary zip (may be large) …")
                supp = await epmc.supplementary(pmcid)
                if supp:
                    files["supplementary.zip"] = supp
                    step(f"  {len(supp):,} bytes")
                else:
                    step("  none")
        calls = epmc.calls

    d, manifest = write_raw(Path(settings.raw_dir).expanduser(), pmcid, files, datetime.now(timezone.utc))
    print(f"{pmcid}: {calls} Europe PMC calls; raw: {d}", file=sys.stderr)
    print(f"  pmid {meta.pmid} · doi {meta.doi} · {meta.journal} {meta.year}", file=sys.stderr)
    print(f"  licence (search): {meta.license} · open access: {meta.is_open_access} · in EPMC: {meta.in_epmc}",
          file=sys.stderr)
    for name, sha in manifest.items():
        print(f"  {name}: {len(files[name]):,} bytes, sha256 {sha[:16]}…", file=sys.stderr)
    if "fulltext.xml" not in files:
        print("WARNING no full text: only metadata and abstract are available", file=sys.stderr)
        return 0
    print(f"  licence (XML): {xml_license(files['fulltext.xml'])}", file=sys.stderr)
    print("\n".join(outline(files["fulltext.xml"])))
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
