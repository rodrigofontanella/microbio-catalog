"""Load a fetched paper (app.ingest.europepmc) into rag.document and link it to a study.

    python -m app.ingest.paper ~/lab/raw/europepmc/<ts>/PMC8024495 --bioproject PRJNA667518 \\
        --link paper_data_availability --link pgmd [--dry-run]

Every link is checked before it is written; an unverifiable link stops the load:
  paper_data_availability  the paper's "Data Availability" section names the BioProject (quote kept)
  pgmd                     PGMD's `Link` for the study (already loaded) names this paper's PMID/PMCID/DOI
The raw folder must still match its SHA256SUMS. Re-runs add nothing; a document whose identifiers
differ from the stored row stops the load.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.jats import sections
from app.ingest.ncbi_study import code_version
from app.models import Assertion, Document, IngestRun, Study, StudyPublication


def verify_raw(d: Path) -> dict[str, str]:
    expected = {}
    for line in (d / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(None, 1)
        expected[name.strip()] = digest
    for name, digest in expected.items():
        if hashlib.sha256((d / name).read_bytes()).hexdigest() != digest:
            raise SystemExit(f"{d / name}: sha256 does not match SHA256SUMS")
    return expected


def document_row(core_json: bytes, pmcid: str) -> dict:
    hits = [h for h in json.loads(core_json)["resultList"]["result"] if h.get("pmcid") == pmcid]
    if len(hits) != 1:
        raise SystemExit(f"core.json has {len(hits)} records for {pmcid}")
    h = hits[0]
    year = h.get("pubYear")
    return {"source": h["source"], "ext_id": h["id"], "pmid": h.get("pmid"), "pmcid": h.get("pmcid"),
            "doi": h.get("doi"), "kind": "paper", "visibility": "public", "title": h.get("title"),
            "journal": (h.get("journalInfo") or {}).get("journal", {}).get("title"),
            "pub_year": int(year) if year and year.isdigit() else None, "license": h.get("license"),
            "is_open_access": h.get("isOpenAccess") == "Y"}


def data_availability_evidence(jats: bytes, bioproject: str) -> dict | None:
    for s in sections(jats):
        if "data availability" in s.title.lower() and bioproject in s.text:
            sentence = next((x for x in re.split(r"(?<=[.:])\s+", s.text) if bioproject in x), s.text)
            return {"section": " > ".join(s.path), "quote": sentence}
    return None


async def pgmd_evidence(db: AsyncSession, study_id: int, doc: dict) -> dict | None:
    links = (await db.execute(select(Assertion.value_text).where(
        Assertion.study_id == study_id, Assertion.field == "link",
        Assertion.method.like("pgmd_curation:%")))).scalars().all()
    ids = [x for x in (doc["pmid"], doc["pmcid"], doc["doi"]) if x]
    for link in links:
        hit = next((i for i in ids if i.lower() in link.lower()), None)
        if hit:
            return {"pgmd_link": link, "matched": hit}
    return None


async def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.ingest.paper", description=__doc__.splitlines()[0])
    ap.add_argument("raw_dir", type=Path, help="…/europepmc/<timestamp>/<PMCID>")
    ap.add_argument("--bioproject", required=True)
    ap.add_argument("--link", action="append", default=[], choices=["paper_data_availability", "pgmd"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    d: Path = args.raw_dir.expanduser()
    pmcid = d.name
    manifest = verify_raw(d)
    doc = document_row((d / "core.json").read_bytes(), pmcid)
    jats = (d / "fulltext.xml").read_bytes() if (d / "fulltext.xml").exists() else None
    acc = args.bioproject.strip().upper()

    from app.database import AsyncSessionLocal
    counts: Counter = Counter()
    async with AsyncSessionLocal() as db:
        study_id = (await db.execute(select(Study.study_id).where(Study.bioproject_acc == acc))).scalar_one_or_none()
        if study_id is None:
            print(f"ERROR {acc} is not in the catalog", file=sys.stderr)
            return 1

        evidence: dict[str, dict] = {}
        for method in args.link:
            ev = (data_availability_evidence(jats, acc) if method == "paper_data_availability" and jats
                  else await pgmd_evidence(db, study_id, doc) if method == "pgmd" else None)
            if ev is None:
                print(f"ERROR link {method!r} can't be verified for {pmcid} ↔ {acc}", file=sys.stderr)
                return 1
            evidence[method] = ev
            print(f"  {method}: {json.dumps(ev, ensure_ascii=False)}", file=sys.stderr)

        params = {"raw_dir": str(d), "pmcid": pmcid, "bioproject": acc, "links": args.link,
                  "sha256": manifest, "dry_run": args.dry_run, **code_version()}
        ir = IngestRun(source="europepmc", source_release=d.parent.name, params=params, raw_uri=str(d))
        db.add(ir)
        await db.flush()

        existing = (await db.execute(select(Document).where(Document.source == doc["source"],
                                                            Document.ext_id == doc["ext_id"]))).scalar_one_or_none()
        if existing:
            diff = {k: (getattr(existing, k), doc[k]) for k in ("pmid", "pmcid", "doi") if getattr(existing, k) != doc[k]}
            if diff:
                await db.rollback()
                print(f"ERROR document {doc['source']}:{doc['ext_id']} exists with other identifiers: {diff}",
                      file=sys.stderr)
                return 1
            doc_id = existing.doc_id
        else:
            row = Document(**doc, raw_uri=str(d), raw_sha256=manifest, ingest_run_id=ir.ingest_run_id)
            db.add(row)
            await db.flush()
            doc_id = row.doc_id
            counts["document.new"] += 1

        for method, ev in evidence.items():
            res = await db.execute(pg_insert(StudyPublication).values(
                study_id=study_id, doc_id=doc_id, link_method=method, evidence=ev, ingest_run_id=ir.ingest_run_id)
                .on_conflict_do_nothing().returning(StudyPublication.doc_id))
            counts["link.new" if res.first() else "link.existing"] += 1

        await db.execute(update(IngestRun).where(IngestRun.ingest_run_id == ir.ingest_run_id).values(
            finished_at=datetime.now(timezone.utc), params={**params, "counts": dict(counts)}))
        irid = ir.ingest_run_id
        await (db.rollback() if args.dry_run else db.commit())
    verb = "dry run, rolled back" if args.dry_run else "committed"
    print(f"ingest_run {irid} ({verb}), doc_id {doc_id}: " + json.dumps(dict(sorted(counts.items()))), file=sys.stderr)
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
