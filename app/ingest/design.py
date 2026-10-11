"""Load a study's experimental design from its spec (conf/designs/<BioProject>.yml).

    python -m app.ingest.design conf/designs/PRJNA667518.yml --dry-run
    python -m app.ingest.design conf/designs/PRJNA667518.yml

The spec is written and reviewed by a person, so the structure it creates is the curated layer.
Levels and assignments are read from the BioSample assertions already in the catalog (the NCBI
ingest must have run), checked against the spec (design_spec.plan_design), and loaded in one
transaction under a prov.ingest_run (source 'manual'; spec path, sha256 and git commit in params).

Re-runs: rows that already exist must match the spec exactly (design type, unit, factor kind and
nesting, level control flag, each subject's level). Anything different stops the load. A changed
design is a deliberate data change, not something a re-run does quietly.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.design_spec import DesignPlan, DesignSpec, balance, load_spec, plan_design
from app.ingest.ncbi_study import code_version
from app.models import (Assertion, Experiment, Factor, FactorLevel, IngestRun, Sample, Study, Subject,
                        SubjectFactorLevel)


class DesignConflict(Exception):
    pass


async def subject_values(db: AsyncSession, study_id: int, fields: set[str]) -> dict[str, dict[str, set[str]]]:
    rows = await db.execute(
        select(Subject.local_id, Assertion.field, Assertion.value_text)
        .join(Sample, Sample.subject_id == Subject.subject_id)
        .join(Assertion, Assertion.sample_id == Sample.sample_id)
        .where(Subject.study_id == study_id, Assertion.field.in_(fields),
               Assertion.method.like("biosample_attr:%"), Assertion.value_text.is_not(None)))
    out: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for local_id, fld, val in rows:
        out[local_id][fld].add(val)
    subjects = (await db.execute(select(Subject.local_id).where(Subject.study_id == study_id))).scalars()
    return {s: dict(out.get(s, {})) for s in subjects}


def _same(existing, wanted: dict, what: str) -> None:
    diff = {k: (getattr(existing, k), v) for k, v in wanted.items() if getattr(existing, k) != v}
    if diff:
        raise DesignConflict(f"{what} already exists with different values: {diff}")


async def load(db: AsyncSession, spec: DesignSpec, plan: DesignPlan, study_id: int, irid: int,
               counts: Counter) -> int:
    e = spec.experiment
    want = {"title": e.title, "design_type": e.design_type, "experimental_unit": e.experimental_unit}
    exp = (await db.execute(select(Experiment).where(Experiment.study_id == study_id,
                                                     Experiment.local_id == e.local_id))).scalar_one_or_none()
    if exp:
        _same(exp, want, f"experiment {e.local_id!r}")
    else:
        exp = Experiment(study_id=study_id, local_id=e.local_id, ingest_run_id=irid, **want)
        db.add(exp)
        await db.flush()
        counts["experiment.new"] += 1

    factor_id: dict[str, int] = {}
    for f in spec.factors:
        want = {"kind": f.kind, "nested_in_factor_id": factor_id.get(f.nested_in) if f.nested_in else None,
                "description": f.description}
        row = (await db.execute(select(Factor).where(Factor.experiment_id == exp.experiment_id,
                                                     Factor.name == f.name))).scalar_one_or_none()
        if row:
            _same(row, want, f"factor {f.name!r}")
        else:
            row = Factor(experiment_id=exp.experiment_id, study_id=study_id, name=f.name, ingest_run_id=irid,
                         definition={"source": f.source.model_dump(), "spec_levels": sorted(f.levels or [])},
                         **want)
            db.add(row)
            await db.flush()
            counts["factor.new"] += 1
        factor_id[f.name] = row.factor_id

    level_id: dict[tuple[str, str | None, str], int] = {}
    for f in spec.factors:          # parents first: spec order is validated to be top-down
        parent_factor = factor_id.get(f.nested_in) if f.nested_in else None
        for (parent_label, label), lspec in sorted(plan.levels[f.name].items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
            parent_level = level_id[(f.nested_in, None, parent_label)] if f.nested_in else None
            row = (await db.execute(select(FactorLevel).where(
                FactorLevel.factor_id == factor_id[f.name], FactorLevel.label == label,
                FactorLevel.parent_level_id.is_(None) if parent_level is None
                else FactorLevel.parent_level_id == parent_level))).scalar_one_or_none()
            want = {"is_control": lspec.control, "description": lspec.description}
            if row:
                _same(row, want, f"level {f.name}={label!r}")
            else:
                row = FactorLevel(factor_id=factor_id[f.name], label=label, parent_factor_id=parent_factor,
                                  parent_level_id=parent_level, ingest_run_id=irid, **want)
                db.add(row)
                await db.flush()
                counts["level.new"] += 1
            level_id[(f.name, parent_label, label)] = row.level_id

    subject_id = dict((await db.execute(select(Subject.local_id, Subject.subject_id)
                                        .where(Subject.study_id == study_id))).all())
    existing = {(s, fct): lvl for s, fct, lvl in (await db.execute(
        select(SubjectFactorLevel.subject_id, SubjectFactorLevel.factor_id, SubjectFactorLevel.level_id)
        .where(SubjectFactorLevel.study_id == study_id))).all()}
    for subject, per_factor in sorted(plan.assignments.items()):
        for fname, (parent_label, label) in per_factor.items():
            sid, fid, lid = subject_id[subject], factor_id[fname], level_id[(fname, parent_label, label)]
            if (sid, fid) in existing:
                if existing[(sid, fid)] != lid:
                    raise DesignConflict(f"subject {subject}: {fname} is already level_id {existing[(sid, fid)]}, "
                                         f"the spec says {label!r} (level_id {lid})")
                counts["assignment.existing"] += 1
                continue
            db.add(SubjectFactorLevel(subject_id=sid, factor_id=fid, level_id=lid, study_id=study_id,
                                      ingest_run_id=irid))
            counts["assignment.new"] += 1
    await db.flush()
    return exp.experiment_id


async def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.ingest.design", description=__doc__.splitlines()[0])
    ap.add_argument("spec", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    raw = args.spec.read_bytes()
    spec = load_spec(args.spec)
    from app.database import AsyncSessionLocal
    counts: Counter = Counter()
    async with AsyncSessionLocal() as db:
        study_id = (await db.execute(select(Study.study_id).where(Study.bioproject_acc == spec.bioproject))
                    ).scalar_one_or_none()
        if study_id is None:
            print(f"ERROR {spec.bioproject} is not in the catalog: run the NCBI ingest first", file=sys.stderr)
            return 1
        values = await subject_values(db, study_id, {f.source.biosample_attr for f in spec.factors})
        plan = plan_design(spec, values)
        print(f"{spec.bioproject}: {len(values)} subjects, "
              + ", ".join(f"{f}: {len(lv)} levels" for f, lv in plan.levels.items()), file=sys.stderr)
        for line in balance(spec, plan) if plan.ok else []:
            print(f"  {line}", file=sys.stderr)
        for w in plan.warnings:
            print(f"WARNING {w}", file=sys.stderr)
        for e in plan.errors[:20]:
            print(f"ERROR {e}", file=sys.stderr)
        if not plan.ok:
            return 1

        params = {"spec": str(args.spec), "spec_sha256": hashlib.sha256(raw).hexdigest(),
                  "spec_content": spec.model_dump(), "dry_run": args.dry_run, **code_version()}
        ir = IngestRun(source="manual", source_release=f"design_spec:{args.spec.name}", params=params,
                       raw_uri=str(args.spec))
        db.add(ir)
        await db.flush()
        try:
            experiment_id = await load(db, spec, plan, study_id, ir.ingest_run_id, counts)
        except DesignConflict as e:
            await db.rollback()
            print(f"ERROR {e}\nrolled back", file=sys.stderr)
            return 1
        await db.execute(update(IngestRun).where(IngestRun.ingest_run_id == ir.ingest_run_id).values(
            finished_at=datetime.now(timezone.utc), params={**params, "counts": dict(counts)}))
        irid = ir.ingest_run_id
        await (db.rollback() if args.dry_run else db.commit())
    verb = "dry run, rolled back" if args.dry_run else "committed"
    print(f"ingest_run {irid} ({verb}), experiment_id {experiment_id}: "
          + json.dumps(dict(sorted(counts.items()))), file=sys.stderr)
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
