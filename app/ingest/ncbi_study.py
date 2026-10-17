"""Ingest one BioProject from NCBI into the catalog: study, subjects, samples, libraries, runs,
and every BioSample attribute as an `asserted` prov.assertion.

    python -m app.ingest.ncbi_study PRJNA667518 --subject-attr Pen --pen-attr Pen --dry-run
    python -m app.ingest.ncbi_study PRJNA667518 --subject-attr Pen --pen-attr Pen
    python -m app.ingest.ncbi_study PRJNA667518 --subject-attr Pen --pen-attr Pen --from-raw <dir>
    python -m app.ingest.ncbi_study PRJNA664026 --subject-pattern 'isolate=^(?P<code>Ci|I|Co|D)(?P<animal>[0-9]+)$' \
        --subject-code-map 'tissue:Ci=Caecum,I=Ileum,Co=Colon,D=Duodenum' --subject-constant sex --subject-constant treatment

Subjects: --subject-attr when an attribute's value is the animal; --subject-pattern when the animal is part of
a value (named group `animal`, optional `code` checked against --subject-code-map). A parsed key is tier
`inferred`: every value must parse, every code must agree with the sample's own attribute, an animal can't have
the same code twice, and each --subject-constant must have one value per animal. Any failure stops the load.

Order: fetch → write raw files + SHA256SUMS → parse → validate (errors stop here) → one
transaction: prov.ingest_run, upserts, assertions, finished_at. A failure rolls the whole load back;
the raw files stay and can be reloaded with --from-raw without calling NCBI again.

Re-runs are idempotent: entities are keyed by accession (existing rows are kept, NULL links are
filled), and an assertion is only added if the same (entity, field, method, value) isn't there yet.
"Filled" covers what another loader (PGMD) left NULL on existing rows: study title/description, sample SRS,
subject link, run library and SRA run facts (spots, bases, length, release date). A non-NULL value is never
overwritten.
A value that changed at NCBI therefore appears as a second assertion, i.e. a conflict, not an overwrite.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.eutils import EUtils
from app.ingest.ncbi_parse import (BioProject, BioSample, SraRun, SubjectRule, is_null, parse_bioproject,
                                   parse_biosamples, parse_runinfo, subject_values, validate)
from app.models import Assertion, IngestRun, Library, Run, Sample, Study, StudySample, Subject

RAW_FILES = ("bioproject.xml", "biosample.xml", "runinfo.csv")
SUBJECT_KEY_ATTRS = {"host_subject_id"}   # attributes that *are* an animal id → tier 'asserted'


@dataclass
class Fetched:
    bioproject_uid: str
    bioproject_xml: bytes
    biosample_xml: bytes
    runinfo_csv: bytes


async def fetch(eutils: EUtils, accession: str) -> Fetched:
    uid = await eutils.bioproject_uid(accession)
    bp_xml = await eutils.bioproject_xml(uid)
    bs_uids = await eutils.biosample_uids(uid)
    bs_xml = await eutils.biosamples_xml(bs_uids) if bs_uids else b"<BioSampleSet></BioSampleSet>\n"
    runinfo = await eutils.sra_runinfo(accession)
    return Fetched(uid, bp_xml, bs_xml, runinfo)


def write_raw(root: Path, accession: str, f: Fetched, fetched_at: datetime) -> tuple[Path, dict[str, str]]:
    """<root>/ncbi/<UTC timestamp>/<BioProject>/ + SHA256SUMS (checkable with `sha256sum -c`)."""
    d = root / "ncbi" / fetched_at.strftime("%Y%m%dT%H%M%SZ") / accession
    d.mkdir(parents=True, exist_ok=False)
    blobs = dict(zip(RAW_FILES, (f.bioproject_xml, f.biosample_xml, f.runinfo_csv)))
    manifest = {}
    for name, data in blobs.items():
        (d / name).write_bytes(data)
        manifest[name] = hashlib.sha256(data).hexdigest()
    (d / "bioproject_uid.txt").write_text(f.bioproject_uid + "\n")
    (d / "SHA256SUMS").write_text("".join(f"{h}  {n}\n" for n, h in manifest.items()))
    return d, manifest


def read_raw(d: Path) -> tuple[Fetched, dict[str, str]]:
    """Reload a raw folder, refusing it if any file no longer matches SHA256SUMS."""
    expected = {}
    for line in (d / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(None, 1)
        expected[name.strip()] = digest
    blobs = {}
    for name in RAW_FILES:
        data = (d / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected.get(name):
            raise SystemExit(f"{d / name}: checksum does not match SHA256SUMS")
        blobs[name] = data
    uid = (d / "bioproject_uid.txt").read_text().strip()
    return Fetched(uid, blobs["bioproject.xml"], blobs["biosample.xml"], blobs["runinfo.csv"]), expected


def code_version() -> dict[str, str | bool]:
    repo = Path(__file__).resolve().parents[2]
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                              check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                                    capture_output=True, text=True, check=True).stdout.strip())
        return {"git_commit": head, "git_dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return {"git_commit": "unknown", "git_dirty": True}


# ── load ────────────────────────────────────────────────────────────────────

async def _upsert_returning_new(db: AsyncSession, model, rows: list[dict], conflict: list[str]) -> int:
    if not rows:
        return 0
    stmt = pg_insert(model).values(rows).on_conflict_do_nothing(index_elements=conflict)
    res = await db.execute(stmt.returning(*[getattr(model, c) for c in conflict]))
    return len(res.all())


async def load(db: AsyncSession, project: BioProject, samples: list[BioSample], runs: list[SraRun], *,
               subject: SubjectRule | None, pen_attr: str | None, litter_attr: str | None,
               raw_dir: Path, manifest: dict[str, str], params: dict) -> tuple[int, Counter]:
    counts: Counter = Counter()

    run_row = IngestRun(source="ncbi", source_release=params["fetched_at"], params=params, raw_uri=str(raw_dir))
    db.add(run_row)
    await db.flush()
    irid = run_row.ingest_run_id

    # study
    counts["study.new"] = await _upsert_returning_new(db, Study, [{
        "bioproject_acc": project.accession, "title": project.title, "description": project.description,
        "ingest_run_id": irid}], ["bioproject_acc"])
    study_id = (await db.execute(select(Study.study_id).where(Study.bioproject_acc == project.accession))).scalar_one()
    for col, value in (("title", project.title), ("description", project.description)):
        if value is not None:    # a study created by the PGMD loader has neither
            res = await db.execute(update(Study).where(Study.study_id == study_id, getattr(Study, col).is_(None))
                                   .values({col: value}))
            counts[f"study.{col}_filled"] += res.rowcount or 0

    # subjects: one per distinct animal key (an attribute's value, or the animal parsed from it)
    subj_of = subject_values(samples, subject)
    key_prefix = subject.attr.lower() if subject else ""
    subject_rows: dict[str, dict] = {}
    for s in samples:
        key = subj_of.get(s.accession)
        if key is None:
            continue
        taxid = s.get("host_taxid")
        subject_rows.setdefault(key, {
            "study_id": study_id, "local_id": f"{key_prefix}-{key}",
            "host_taxid": int(taxid) if taxid and taxid.isdigit() else None,
            "pen": None if pen_attr is None or is_null(s.get(pen_attr)) else s.get(pen_attr),
            "litter": None if litter_attr is None or is_null(s.get(litter_attr)) else s.get(litter_attr),
            "ingest_run_id": irid})
    counts["subject.new"] = await _upsert_returning_new(db, Subject, list(subject_rows.values()),
                                                        ["study_id", "local_id"])
    subject_id = {lid: sid for lid, sid in (await db.execute(
        select(Subject.local_id, Subject.subject_id).where(Subject.study_id == study_id))).all()}

    # samples (+ fill subject/SRS links that an earlier load left NULL)
    sample_rows = [{"biosample_acc": s.accession, "sra_sample_acc": s.sra_accession,
                    "subject_id": subject_id.get(f"{key_prefix}-{subj_of[s.accession]}")
                    if s.accession in subj_of else None,
                    "ingest_run_id": irid} for s in samples]
    counts["sample.new"] = await _upsert_returning_new(db, Sample, sample_rows, ["biosample_acc"])
    for r in sample_rows:
        for col in ("subject_id", "sra_sample_acc"):
            if r[col] is None:
                continue
            res = await db.execute(update(Sample).where(Sample.biosample_acc == r["biosample_acc"],
                                                        getattr(Sample, col).is_(None))
                                   .values({col: r[col]}))
            counts[f"sample.{col.removesuffix('_id').removesuffix('_acc')}_filled"] += res.rowcount or 0
    sample_id = {acc: sid for acc, sid in (await db.execute(
        select(Sample.biosample_acc, Sample.sample_id).where(
            Sample.biosample_acc.in_([s.accession for s in samples])))).all()}

    counts["study_sample.new"] = await _upsert_returning_new(db, StudySample, [
        {"study_id": study_id, "sample_id": sid, "ingest_run_id": irid} for sid in sample_id.values()],
        ["study_id", "sample_id"])

    # libraries (SRX) and runs (SRR)
    libs: dict[str, dict] = {}
    for r in runs:
        libs.setdefault(r.experiment, {
            "experiment_acc": r.experiment, "sample_id": sample_id[r.biosample], "study_id": study_id,
            "library_name": r.library_name, "library_strategy": r.library_strategy,
            "library_source": r.library_source, "library_selection": r.library_selection,
            "library_layout": r.library_layout, "platform": r.platform,
            "instrument_model": r.instrument_model, "ingest_run_id": irid})
    counts["library.new"] = await _upsert_returning_new(db, Library, list(libs.values()), ["experiment_acc"])
    library_id = {acc: lid for acc, lid in (await db.execute(
        select(Library.experiment_acc, Library.library_id).where(Library.experiment_acc.in_(list(libs))))).all()}

    run_rows = [{"run_acc": r.run, "sample_id": sample_id[r.biosample], "study_id": study_id,
                 "library_id": library_id[r.experiment], "spots": r.spots, "bases": r.bases,
                 "avg_read_len": r.avg_read_len, "release_date": r.release_date, "ingest_run_id": irid}
                for r in runs]
    counts["run.new"] = await _upsert_returning_new(db, Run, run_rows, ["run_acc"])
    for r in run_rows:   # a run created earlier without SRA info (e.g. from PGMD) gets its library + facts now
        res = await db.execute(update(Run).where(Run.run_acc == r["run_acc"], Run.library_id.is_(None))
                               .values(library_id=r["library_id"]))
        counts["run.library_filled"] += res.rowcount or 0
        res = await db.execute(update(Run).where(
            Run.run_acc == r["run_acc"], Run.spots.is_(None), Run.bases.is_(None),
            Run.avg_read_len.is_(None), Run.release_date.is_(None)).values(
            spots=r["spots"], bases=r["bases"], avg_read_len=r["avg_read_len"], release_date=r["release_date"]))
        counts["run.sra_facts_filled"] += res.rowcount or 0

    # assertions: every BioSample attribute, raw, tier 'asserted'
    existing = {tuple(x) for x in (await db.execute(
        select(Assertion.sample_id, Assertion.field, Assertion.method, Assertion.value_text)
        .where(Assertion.sample_id.in_(list(sample_id.values())),
               Assertion.method.like("biosample_attr:%")))).all()}
    new_assertions = []
    for s in samples:
        sid = sample_id[s.accession]
        for a in s.attributes:
            if is_null(a.value):
                counts["assertion.skipped_null"] += 1
                continue
            method = f"biosample_attr:{a.attribute_name}"
            if (sid, a.field, method, a.value) in existing:
                counts["assertion.existing"] += 1
                continue
            existing.add((sid, a.field, method, a.value))
            new_assertions.append({
                "sample_id": sid, "field": a.field, "value_text": a.value, "tier": "asserted",
                "method": method, "ingest_run_id": irid,
                "evidence": {"biosample_acc": s.accession, "attribute_name": a.attribute_name,
                             "harmonized_name": a.harmonized_name, "display_name": a.display_name,
                             "raw_file": "biosample.xml", "raw_sha256": manifest["biosample.xml"]}})

    # how each subject was identified: 'asserted' only if the source gives an animal id as a whole value
    if subject:
        tier = "asserted" if subject.attr.lower() in SUBJECT_KEY_ATTRS and not subject.is_parsed else "inferred"
        if subject.is_parsed:
            basis = (f"one subject per animal parsed from BioSample {subject.attr!r} with "
                     f"{subject.pattern.pattern!r}; assumes the parsed number is the same animal across samples. "
                     "Verify in the paper's Methods")
            value_text = f"biosample_attr:{subject.attr} ~ {subject.pattern.pattern}"
            method = "ingest_rule:subject_from_pattern"
        else:
            basis = (f"one subject per distinct BioSample {subject.attr!r} value"
                     + ("" if tier == "asserted" else "; assumes one sampled animal per value. Verify in the paper's Methods"))
            value_text = f"biosample_attr:{subject.attr}"
            method = "ingest_rule:subject_from_attr"
        have = {sid for (sid,) in (await db.execute(select(Assertion.subject_id).where(
            Assertion.subject_id.in_(list(subject_id.values())), Assertion.field == "subject_basis"))).all()}
        this_load = {row["local_id"] for row in subject_rows.values()}
        for lid, subj in subject_id.items():
            if subj in have or lid not in this_load:
                continue
            new_assertions.append({
                "subject_id": subj, "field": "subject_basis", "value_text": value_text,
                "tier": tier, "method": method, "ingest_run_id": irid,
                "evidence": {"rule": basis, "bioproject": project.accession, "subject_rule": subject.describe(),
                             "checked_constant": params.get("subject_constants", [])}})

    # a multi-row INSERT needs the same keys in every row: the exclusive arc's other targets are NULL
    targets = ("study_id", "subject_id", "sample_id", "library_id", "run_acc", "genome_acc")
    new_assertions = [{**{t: None for t in targets}, **row} for row in new_assertions]
    for i in range(0, len(new_assertions), 1000):
        await db.execute(pg_insert(Assertion).values(new_assertions[i:i + 1000]))
    counts["assertion.new"] = len(new_assertions)

    await db.execute(update(IngestRun).where(IngestRun.ingest_run_id == irid).values(
        finished_at=datetime.now(timezone.utc), params={**params, "counts": dict(counts)}))
    return irid, counts


# ── CLI ─────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m app.ingest.ncbi_study", description=__doc__.splitlines()[0])
    ap.add_argument("bioproject", help="BioProject accession, e.g. PRJNA667518")
    ap.add_argument("--subject-attr",
                    help="BioSample attribute whose value is the animal (default: host_subject_id)")
    ap.add_argument("--subject-pattern",
                    help="ATTR=REGEX with a named group (?P<animal>…) and optionally (?P<code>…): the animal is "
                         "part of the value, e.g. \"isolate=^(?P<code>Ci|I|Co|D)(?P<animal>[0-9]+)$\"")
    ap.add_argument("--subject-code-map",
                    help="ATTR:CODE=VALUE,… : each code must equal the sample's ATTR, e.g. 'tissue:Ci=Caecum,I=Ileum'")
    ap.add_argument("--subject-constant", action="append", default=[],
                    help="BioSample attribute that must have one value per animal (repeatable), e.g. sex")
    ap.add_argument("--pen-attr", help="BioSample attribute holding the pen")
    ap.add_argument("--litter-attr", help="BioSample attribute holding the litter / dam")
    ap.add_argument("--from-raw", type=Path, help="reload a raw folder instead of calling NCBI")
    ap.add_argument("--dry-run", action="store_true", help="do everything, then roll back")
    return ap


async def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    acc = args.bioproject.strip().upper()
    fetched_at = datetime.now(timezone.utc)

    if args.from_raw:
        fetched, manifest = read_raw(args.from_raw)
        raw_dir = args.from_raw
    else:
        if not settings.raw_dir:
            print("RAW_DIR is not set (env or .env): raw NCBI responses must be kept", file=sys.stderr)
            return 2
        async with EUtils.from_settings() as eu:
            fetched = await fetch(eu, acc)
            print(f"fetched {acc}: {eu.calls} E-utilities calls", file=sys.stderr)
        raw_dir, manifest = write_raw(Path(settings.raw_dir).expanduser(), acc, fetched, fetched_at)
        print(f"raw: {raw_dir}", file=sys.stderr)

    project = parse_bioproject(fetched.bioproject_xml, fetched.bioproject_uid)
    if project.accession != acc:
        print(f"BioProject XML is {project.accession}, expected {acc}", file=sys.stderr)
        return 2
    samples = parse_biosamples(fetched.biosample_xml)
    runs = parse_runinfo(fetched.runinfo_csv)
    try:
        subject = SubjectRule.from_cli(args.subject_attr, args.subject_pattern, args.subject_code_map)
    except (ValueError, re.error) as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 2
    constant = tuple(a for a in (args.pen_attr, args.litter_attr, "host_taxid", *args.subject_constant) if a)
    check = validate(project, samples, runs, subject, constant)
    print(f"parsed: {len(samples)} BioSamples, {len(runs)} runs, "
          f"{len({r.experiment for r in runs})} experiments; title: {project.title!r}", file=sys.stderr)
    for w in check.warnings:
        print(f"WARNING {w}", file=sys.stderr)
    for e in check.errors:
        print(f"ERROR {e}", file=sys.stderr)
    if not check.ok:
        return 1

    subj = subject_values(samples, subject)
    print(f"subjects: {len(set(subj.values()))} animals from {len(subj)} of {len(samples)} BioSamples "
          f"({'parsed from ' + subject.attr if subject.is_parsed else subject.attr})", file=sys.stderr)
    params = {"bioproject": acc, "bioproject_uid": project.uid, "subject_attr": subject.attr,
              "subject_rule": subject.describe(), "subject_constants": list(args.subject_constant),
              "pen_attr": args.pen_attr, "litter_attr": args.litter_attr,
              "fetched_at": (fetched_at.isoformat() if not args.from_raw else f"from_raw:{raw_dir.name}"),
              "from_raw": bool(args.from_raw), "manifest": manifest, "dry_run": args.dry_run,
              "warnings": check.warnings, **code_version()}

    from app.database import AsyncSessionLocal   # imported here: the engine is built from settings
    async with AsyncSessionLocal() as db:
        irid, counts = await load(db, project, samples, runs, subject=subject,
                                  pen_attr=args.pen_attr, litter_attr=args.litter_attr,
                                  raw_dir=raw_dir, manifest=manifest, params=params)
        if args.dry_run:
            await db.rollback()
        else:
            await db.commit()
    verb = "dry run, rolled back" if args.dry_run else "committed"
    print(f"ingest_run {irid} ({verb}): " + json.dumps(dict(sorted(counts.items()))), file=sys.stderr)
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
