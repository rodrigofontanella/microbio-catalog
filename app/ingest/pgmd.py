"""Load PGMD v1 curation into the catalog as `asserted` assertions (method 'pgmd_curation:<file>').

    python -m app.ingest.pgmd ~/lab/raw/pgmd/v1 --bioproject PRJNA667518 --dry-run
    python -m app.ingest.pgmd ~/lab/raw/pgmd/v1 --bioproject PRJNA667518
    python -m app.ingest.pgmd ~/lab/raw/pgmd/v1                      # all projects

The folder is a working copy; /srv/bulk/raw/pgmd/v1 on Skipper is canonical. The load refuses to
start unless Project.csv and Sample_runs.csv match pgmd-v1.SHA256SUMS.

Entities: a study, sample or run PGMD names but the catalog lacks is created from the accession
alone (runs without a library; the NCBI ingest fills that in later). An existing run that PGMD
attaches to a different BioSample stops the load. Rows with malformed accessions are skipped and
recorded in ingest_run.params["rejected_rows"]. Re-runs add only new (entity, field, method, value)
assertions, as the NCBI ingest does.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.ncbi_study import code_version
from app.ingest.pgmd_parse import (REQUIRED_PROJECT, REQUIRED_SAMPLE, SOURCE_RELEASE, Planned, ProjectPlan,
                                   group_rows, plan_project, read_csv, split_valid_rows)
from app.models import Assertion, IngestRun, Run, Sample, Study, StudySample

FILES = ("Project.csv", "Sample_runs.csv")
SUMS = "pgmd-v1.SHA256SUMS"
TARGETS = ("study_id", "subject_id", "sample_id", "library_id", "run_acc", "genome_acc")


def verify_checksums(d: Path) -> dict[str, str]:
    expected = {}
    for line in (d / SUMS).read_text().splitlines():
        digest, name = line.split(None, 1)
        expected[name.strip()] = digest
    out = {}
    for name in FILES:
        got = hashlib.sha256((d / name).read_bytes()).hexdigest()
        if got != expected.get(name):
            raise SystemExit(f"{d / name}: sha256 {got} does not match {SUMS}")
        out[name] = got
    return out


async def _ids(db: AsyncSession, col_key, col_id, keys: list[str]) -> dict[str, int]:
    if not keys:
        return {}
    return dict((await db.execute(select(col_key, col_id).where(col_key.in_(keys)))).all())


async def load_project(db: AsyncSession, plan: ProjectPlan, irid: int, counts: Counter) -> list[str]:
    """Upsert the project's entities and add its assertions. Returns errors (empty = loaded)."""
    acc = plan.bioproject
    res = await db.execute(pg_insert(Study).values(bioproject_acc=acc, ingest_run_id=irid)
                           .on_conflict_do_nothing(index_elements=["bioproject_acc"]).returning(Study.study_id))
    counts["study.new"] += len(res.all())
    study_id = (await db.execute(select(Study.study_id).where(Study.bioproject_acc == acc))).scalar_one()

    biosamples = sorted(set(plan.runs.values()))
    if biosamples:
        res = await db.execute(pg_insert(Sample).values(
            [{"biosample_acc": b, "ingest_run_id": irid} for b in biosamples])
            .on_conflict_do_nothing(index_elements=["biosample_acc"]).returning(Sample.sample_id))
        counts["sample.new"] += len(res.all())
    sample_id = await _ids(db, Sample.biosample_acc, Sample.sample_id, biosamples)
    if sample_id:
        res = await db.execute(pg_insert(StudySample).values(
            [{"study_id": study_id, "sample_id": s, "ingest_run_id": irid} for s in sample_id.values()])
            .on_conflict_do_nothing(index_elements=["study_id", "sample_id"]).returning(StudySample.sample_id))
        counts["study_sample.new"] += len(res.all())

    existing_runs = await _ids(db, Run.run_acc, Run.sample_id, list(plan.runs))
    errors = [f"{acc}: run {r} is on sample_id {existing_runs[r]} in the catalog, but PGMD puts it on "
              f"{bs} (sample_id {sample_id[bs]})"
              for r, bs in plan.runs.items() if r in existing_runs and existing_runs[r] != sample_id[bs]]
    if errors:
        return errors
    new_runs = [{"run_acc": r, "sample_id": sample_id[bs], "study_id": study_id, "library_id": None,
                 "ingest_run_id": irid} for r, bs in plan.runs.items() if r not in existing_runs]
    for i in range(0, len(new_runs), 1000):
        await db.execute(pg_insert(Run).values(new_runs[i:i + 1000]).on_conflict_do_nothing())
    counts["run.new"] += len(new_runs)

    def target(a: Planned) -> dict:
        t = {k: None for k in TARGETS}
        if a.target == "study":
            t["study_id"] = study_id
        elif a.target == "sample":
            t["sample_id"] = sample_id[a.key]
        else:
            t["run_acc"] = a.key
        return t

    have = set()
    for col, keys in (("study_id", [study_id]), ("sample_id", list(sample_id.values())),
                      ("run_acc", list(plan.runs))):
        if not keys:
            continue
        c = getattr(Assertion, col)
        rows = await db.execute(select(c, Assertion.field, Assertion.method, Assertion.value_text,
                                       Assertion.value_num)
                                .where(c.in_(keys), Assertion.method.like("pgmd_curation:%")))
        have |= {(col, *r[:4], float(r[4]) if r[4] is not None else None) for r in rows}

    rows, added = [], set()
    for a in plan.assertions:
        t = target(a)
        col = next(k for k in ("study_id", "sample_id", "run_acc") if t[k] is not None)
        method = f"pgmd_curation:{a.method_file}"
        key = (col, t[col], a.field, method, a.value_text, a.value_num)
        if key in have:
            counts["assertion.existing"] += 1
            continue
        if key in added:   # e.g. a BioSample with two runs whose ages parse to the same bound
            counts["assertion.duplicate_in_file"] += 1
            continue
        added.add(key)
        rows.append({**t, "field": a.field, "value_text": a.value_text, "value_num": a.value_num,
                     "unit": a.unit, "tier": a.tier, "method": method, "evidence": a.evidence,
                     "ingest_run_id": irid, "value_term": None})
    for i in range(0, len(rows), 1000):
        await db.execute(pg_insert(Assertion).values(rows[i:i + 1000]))
    counts["assertion.new"] += len(rows)
    counts["projects.loaded"] += 1
    return []


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m app.ingest.pgmd", description=__doc__.splitlines()[0])
    ap.add_argument("pgmd_dir", type=Path, help="folder with Project.csv, Sample_runs.csv, pgmd-v1.SHA256SUMS")
    ap.add_argument("--bioproject", action="append", default=[], help="load only this project (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="do everything, then roll back")
    return ap


async def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    d: Path = args.pgmd_dir.expanduser()
    sums = verify_checksums(d)
    projects, samples_all = group_rows(read_csv(d / "Project.csv", REQUIRED_PROJECT), [])
    valid, rejected, errors = split_valid_rows(read_csv(d / "Sample_runs.csv", REQUIRED_SAMPLE))
    if errors:
        print("\n".join(f"ERROR {e}" for e in errors), file=sys.stderr)
        return 1
    _, samples = group_rows([], valid)

    wanted = [b.strip().upper() for b in args.bioproject] or sorted(set(projects) | set(samples))
    unknown = [b for b in wanted if b not in projects and b not in samples]
    if unknown:
        print(f"ERROR not in PGMD: {unknown}", file=sys.stderr)
        return 1
    rejected = [r for r in rejected if r["bioproject"] in set(wanted)]
    plans = [plan_project(b, projects.get(b), samples.get(b, [])) for b in wanted]
    print(f"PGMD: {len(wanted)} projects, {sum(len(p.runs) for p in plans)} runs, "
          f"{sum(len(p.assertions) for p in plans)} planned assertions, "
          f"{sum(p.nulls_skipped for p in plans)} null cells skipped, {len(rejected)} rows rejected",
          file=sys.stderr)
    for r in rejected[:10]:
        print(f"WARNING rejected {r['bioproject']} {r['runs']!r} / {r['bio_sample']!r}: {r['reason']}", file=sys.stderr)

    params = {"pgmd_dir": str(d), "canonical": "skipper:/srv/bulk/raw/pgmd/v1", "sha256": sums,
              "bioprojects": wanted if args.bioproject else "all", "rejected_rows": rejected,
              "dry_run": args.dry_run, **code_version()}

    from app.database import AsyncSessionLocal
    counts: Counter = Counter()
    async with AsyncSessionLocal() as db:
        ir = IngestRun(source="pgmd", source_release=SOURCE_RELEASE, params=params, raw_uri=str(d))
        db.add(ir)
        await db.flush()
        all_errors = []
        for plan in plans:
            all_errors += await load_project(db, plan, ir.ingest_run_id, counts)
        if all_errors:
            await db.rollback()
            print("\n".join(f"ERROR {e}" for e in all_errors[:20]), file=sys.stderr)
            print(f"rolled back: {len(all_errors)} linkage errors", file=sys.stderr)
            return 1
        await db.execute(update(IngestRun).where(IngestRun.ingest_run_id == ir.ingest_run_id).values(
            finished_at=datetime.now(timezone.utc), params={**params, "counts": dict(counts)}))
        irid = ir.ingest_run_id
        await (db.rollback() if args.dry_run else db.commit())
    verb = "dry run, rolled back" if args.dry_run else "committed"
    print(f"ingest_run {irid} ({verb}): " + json.dumps(dict(sorted(counts.items()))), file=sys.stderr)
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
