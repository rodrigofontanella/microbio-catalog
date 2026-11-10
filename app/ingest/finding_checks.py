"""Store paper-vs-profile verdicts as tier 'derived' rows in core.finding_check (design doc §5.8.3).

    python -m app.ingest.finding_checks conf/match/PRJNA430317.yml --dry-run    # computes, writes, rolls back
    python -m app.ingest.finding_checks conf/match/PRJNA430317.yml              # needs a signed map, a clean tree

Decided 2026-11-10 (Rodrigo; migration 6c1f0e2a9b47):
  - tier `derived`: a deterministic computation (Kruskal-Wallis + BH, app.reports.finding_vs_profile) on a signed
    claim map; the map is named in every row's evidence by path and sha256, the code by the ingest run's git commit;
  - the loader **recomputes** with the report's own `compute()`, never reads the report's TSV;
  - **every** row the report has: three scenarios x every lineage, including `n_a`, `same_as_all`, `not_testable`.

Guards, as for the other loaders:
  - a real load needs `review.reviewed_by` / `reviewed_on` on the map and a clean git tree (the ingest run's commit
    must be the code and map that produced the rows); --dry-run needs neither;
  - every verdict is re-derived from (finding direction, PGMD direction, q) before writing: the one rule the
    database can't check;
  - re-runs add nothing; a finding x profile whose stored rows differ in any way (a number, a verdict, the map's
    sha256, a missing or extra row) stops the load and names it. A changed map or new code that changes a result
    is a deliberate data change: delete that finding x profile's rows, then load.
Nothing in core.finding is touched.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.reports import finding_vs_profile as fvp

METHOD = "finding_vs_profile"
VERDICT_DB = {"= all": "same_as_all", "n/a": "n_a", "(no verdict)": "no_verdict"}
COMPARE = ("unit", "verdict", "reason", "n_t", "n_r", "present_t", "present_r", "median_t", "median_r", "direction",
           "p", "q", "tier", "method", "evidence")


def check_rows(rows: list[fvp.Row], families: list[fvp.Family], header: dict[str, Any], study_id: int,
               profile_id: int) -> list[dict[str, Any]]:
    """The report's rows as core.finding_check rows (no ingest_run_id). The map's own not_testable has no lineage."""
    fam = {(f.site_term, f.rank, f.scenario): f for f in families}
    common = {"map": header["map_path"], "map_sha256": header["map_sha256"],
              "reviewed_by": header["reviewed_by"],
              "reviewed_on": str(header["reviewed_on"]) if header["reviewed_on"] else None,
              "profile_key": header["profile_key"], "delta_version": int(header["delta_version"]),
              "feature_taxon_version": int(header["taxon_version"]), "test": "Kruskal-Wallis", "correction": "BH",
              "alpha": header["alpha"], "min_prevalence": header["min_prevalence"], "min_per_diet": fvp.MIN_PER_DIET,
              "direction_rule": "sign of mean-rank difference, treatment - reference"}
    out = []
    for r in rows:
        if r.claim.finding_id is None:
            raise ValueError(f"{r.claim.local_id}: no finding_id (claims must come from core.finding)")
        x, from_map = r.res, r.reason == "map"
        f = fam.get((r.claim.site_term, r.claim.feature_kind, r.scenario))
        out.append({
            "finding_id": r.claim.finding_id, "study_id": study_id, "profile_id": profile_id,
            "scenario": r.scenario, "role": r.role,
            "unit": None if from_map else r.mapped,
            "verdict": VERDICT_DB.get(r.verdict, r.verdict),
            "reason": r.mapped if from_map else (r.reason or None),
            "n_t": x.n_t if x else None, "n_r": x.n_r if x else None,
            "present_t": x.present_t if x else None, "present_r": x.present_r if x else None,
            "median_t": x.median_t if x else None, "median_r": x.median_r if x else None,
            "direction": x.direction if x else None, "p": x.p if x else None, "q": x.q if x else None,
            "tier": "derived", "method": METHOD,
            "evidence": {**common, "claim": r.claim.local_id,
                         "family": None if f is None else {"pigs_t": f.n_t, "pigs_r": f.n_r, "tested": f.tested,
                                                           "below_prevalence": len(f.below_prevalence)}},
        })
    return out


def verdict_errors(db_rows: list[dict[str, Any]], claims: list[fvp.Claim], alpha: float) -> list[str]:
    """Every tested verdict re-derived from the finding's direction, PGMD's direction and q."""
    direction = {c.finding_id: c.direction for c in claims}
    errors = []
    for r in db_rows:
        if r["role"] != "verdict" or r["q"] is None:
            continue
        want = fvp.verdict(direction[r["finding_id"]],
                           fvp.UnitTest(r["unit"], 0, 0, 0, 0, 0.0, 0.0, r["direction"], r["p"], r["q"]), alpha)
        if want != r["verdict"]:
            errors.append(f"{r['evidence']['claim']} {r['scenario']}: verdict {r['verdict']} but the rule gives {want}")
    return errors


def _key(r: dict[str, Any]) -> tuple:
    return (r["scenario"], r["role"], r["unit"])


def diff_stored(stored: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[str]:
    """Why one finding x profile's stored rows aren't the new ones; [] when identical."""
    have, want = {_key(r): r for r in stored}, {_key(r): r for r in new}
    problems = [f"stored only: {k}" for k in sorted(have.keys() - want.keys(), key=str)]
    problems += [f"new only: {k}" for k in sorted(want.keys() - have.keys(), key=str)]
    for k in sorted(have.keys() & want.keys(), key=str):
        cols = {c: (have[k][c], want[k][c]) for c in COMPARE if have[k][c] != want[k][c]}
        if cols:
            problems.append(f"{k}: " + ", ".join(f"{c} {a!r} -> {b!r}" for c, (a, b) in cols.items()))
    return problems


async def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.ingest.finding_checks", description=__doc__.splitlines()[0])
    ap.add_argument("map", type=Path, help="conf/match/<BioProject>.yml (signed)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    from sqlalchemy import select, update

    from app.database import AsyncSessionLocal
    from app.ingest.ncbi_study import code_version
    from app.models import FindingCheck, IngestRun

    spec = fvp.MatchSpec.model_validate(yaml.safe_load(args.map.read_bytes()))
    cv = code_version()
    if not args.dry_run and not spec.signed:
        print("ERROR the claim map is not signed (review.reviewed_by / reviewed_on): --dry-run checks without it",
              file=sys.stderr)
        return 1
    if not args.dry_run and cv["git_dirty"]:
        print("ERROR the git tree is dirty: commit first, so the ingest run's commit is the code and map that "
              "produced the rows (--dry-run doesn't need it)", file=sys.stderr)
        return 1

    out = await fvp.compute(args.map)
    header, cat = out["header"], out["catalog"]
    rows = check_rows(out["rows"], out["families"], header, cat["study_id"], cat["profile"]["profile_id"])
    errors = verdict_errors(rows, cat["claims"], header["alpha"])
    for e in errors:
        print(f"ERROR {e}", file=sys.stderr)
    if errors:
        return 1

    counts: Counter = Counter()
    by_finding: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_finding[r["finding_id"]].append(r)
    async with AsyncSessionLocal() as db:
        params = {"map": str(args.map), "map_sha256": header["map_sha256"], "reviewed_by": header["reviewed_by"],
                  "reviewed_on": str(header["reviewed_on"]), "profile_key": header["profile_key"],
                  "delta_version": int(header["delta_version"]), "feature_taxon_version": header["taxon_version"],
                  "dry_run": args.dry_run, **cv}
        ir = IngestRun(source="own", source_release=f"finding_checks:{args.map.name}", params=params,
                       raw_uri=str(args.map))
        db.add(ir)
        await db.flush()
        for fid, new in sorted(by_finding.items()):
            stored = (await db.execute(select(FindingCheck).where(
                FindingCheck.finding_id == fid, FindingCheck.profile_id == cat["profile"]["profile_id"]))
                      ).scalars().all()
            if stored:
                problems = diff_stored([{c: getattr(s, c) for c in ("scenario", "role") + COMPARE} for s in stored],
                                       new)
                if problems:
                    await db.rollback()
                    print(f"ERROR {new[0]['evidence']['claim']} is stored differently against this profile:\n  "
                          + "\n  ".join(problems), file=sys.stderr)
                    return 1
                counts["finding_check.existing"] += len(new)
                continue
            for r in new:
                db.add(FindingCheck(**r, ingest_run_id=ir.ingest_run_id))
            counts["finding_check.new"] += len(new)
            counts["finding.checked"] += 1
        await db.flush()
        for r in rows:
            if r["scenario"] == "all" and r["role"] == "verdict":
                counts[f"verdict.{r['verdict']}"] += 1
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
