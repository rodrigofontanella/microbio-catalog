"""Depth of a study's profiles, per group: what a depth filter would remove from each group. Read-only.

    python -m app.reports.depth PRJNA664026 --group-by treatment
    python -m app.reports.depth PRJNA667518

The flag (core.profile_run.depth_status, migration 2bae84543400) is fail / low / ok from n_reads and the
thresholds recorded on the row. It is a flag, never a filter: in PRJNA664026 the shallow runs are almost all
Antibiotic, so filtering would bias the comparison. This report is the "loss per group" any filter must show.

Every run of the study is counted, including runs the profile doesn't cover (`not_profiled`), because a run PGMD
didn't profile is lost to an analysis just like a `fail`. `--group-by` names a sample assertion field
(`treatment`, `tissue`, …); a sample with several distinct values for it is labelled with all of them ('A | B').
Works as microbio_ro.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from sqlalchemy import text

STATUSES = ("ok", "low", "fail")


def status(n_reads: int | None, fail_below: int | None, low_below: int | None) -> str | None:
    """The same rule as the generated column (models.DEPTH_STATUS_SQL), for dry runs that write nothing."""
    if n_reads is None or fail_below is None or low_below is None:
        return None
    return "fail" if n_reads < fail_below else "low" if n_reads < low_below else "ok"


PROFILES_SQL = text("""
SELECT p.profile_id, p.profile_key, min(pr.depth_fail_below) AS fail_below, max(pr.depth_fail_below) AS fail_below_max,
       min(pr.depth_low_below) AS low_below, max(pr.depth_low_below) AS low_below_max
FROM core.profile p JOIN core.study st USING (study_id)
LEFT JOIN core.profile_run pr USING (profile_id)
WHERE st.bioproject_acc = :bp
GROUP BY 1, 2 ORDER BY 1
""")

ROWS_SQL = text("""
WITH runs AS (
  SELECT r.run_acc, r.sample_id FROM core.run r JOIN core.study st USING (study_id) WHERE st.bioproject_acc = :bp
), grp AS (
  SELECT a.sample_id, string_agg(DISTINCT a.value_text, ' | ' ORDER BY a.value_text) AS g
  FROM prov.assertion a
  WHERE CAST(:field AS text) IS NOT NULL AND a.field = :field AND a.sample_id IN (SELECT sample_id FROM runs)
  GROUP BY 1
)
SELECT CASE WHEN CAST(:field AS text) IS NULL THEN 'all' ELSE coalesce(grp.g, '(no value)') END AS grp,
       count(*) AS runs,
       count(*) FILTER (WHERE pr.depth_status = 'ok') AS ok,
       count(*) FILTER (WHERE pr.depth_status = 'low') AS low,
       count(*) FILTER (WHERE pr.depth_status = 'fail') AS fail,
       count(*) FILTER (WHERE pr.run_acc IS NOT NULL AND pr.depth_status IS NULL) AS unflagged,
       count(*) FILTER (WHERE pr.run_acc IS NULL) AS not_profiled
FROM runs
LEFT JOIN core.profile_run pr ON pr.run_acc = runs.run_acc AND pr.profile_id = :pid
LEFT JOIN grp ON grp.sample_id = runs.sample_id
GROUP BY 1 ORDER BY 1
""")


def with_losses(row: dict[str, Any]) -> dict[str, Any]:
    """Runs a filter at each threshold would remove: everything below it, plus runs the profile doesn't have."""
    out = dict(row)
    out["lost_at_fail"] = row["fail"] + row["not_profiled"]
    out["lost_at_low"] = row["fail"] + row["low"] + row["not_profiled"]
    return out


async def report(db, bioproject: str, group_by: str | None) -> list[dict[str, Any]]:
    """One entry per profile of the study: its key, thresholds and per-group rows."""
    out = []
    for p in (await db.execute(PROFILES_SQL, {"bp": bioproject})).mappings().all():
        rows = (await db.execute(ROWS_SQL, {"bp": bioproject, "pid": p["profile_id"], "field": group_by})).mappings().all()
        out.append({**dict(p), "rows": [with_losses(dict(r)) for r in rows]})
    return out


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "-"


def format_report(bioproject: str, profiles: list[dict[str, Any]], group_by: str | None) -> str:
    if not profiles:
        return f"{bioproject}: no profiles"
    lines = []
    for p in profiles:
        if p["fail_below"] is None:
            th = "no thresholds recorded"
        elif (p["fail_below"], p["low_below"]) != (p["fail_below_max"], p["low_below_max"]):
            th = "MIXED thresholds"
        else:
            th = f"fail < {p['fail_below']}, low < {p['low_below']}"
        lines.append(f"== {bioproject} profile {p['profile_id']} ({th}), by {group_by or 'all runs'}")
        w = max([len("group")] + [len(r["grp"]) for r in p["rows"]])
        lines.append(f"{'group':<{w}}  runs    ok   low  fail  unflagged  not_profiled  lost@fail  lost@low")
        for r in p["rows"]:
            lines.append(f"{r['grp']:<{w}}  {r['runs']:>4}  {r['ok']:>4}  {r['low']:>4}  {r['fail']:>4}  {r['unflagged']:>9}  "
                         f"{r['not_profiled']:>12}  {r['lost_at_fail']:>4} {_pct(r['lost_at_fail'], r['runs']):>4}  "
                         f"{r['lost_at_low']:>3} {_pct(r['lost_at_low'], r['runs']):>4}")
        if group_by and all(r["grp"] == "(no value)" for r in p["rows"]):
            lines.append(f"WARNING no sample of {bioproject} has an assertion with field {group_by!r}")
    return "\n".join(lines)


async def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.reports.depth", description=__doc__.splitlines()[0])
    ap.add_argument("bioproject")
    ap.add_argument("--group-by", help="sample assertion field to group by, e.g. treatment")
    args = ap.parse_args(argv)
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        profiles = await report(db, args.bioproject.strip().upper(), args.group_by)
    print(format_report(args.bioproject.strip().upper(), profiles, args.group_by))
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
