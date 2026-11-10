"""Read-only survey: PGMD studies with profiled runs that are not core.run rows ("orphans"), each orphan checked
against SRA's own run list for that BioProject. Nothing is written: no ingest run, no Delta commit, no rows.

    python -m app.reports.pgmd_orphans                         # every BioProject in bronze that has orphans
    python -m app.reports.pgmd_orphans PRJNA684557 PRJEB30494  # just these
    python -m app.reports.pgmd_orphans --detail                # one row per orphan instead of per study

Why: pgmd_profile stops a study whose Species rows name a run that isn't a core.run row (decided 2026-11-07: no
partial profiles). core.run came from PGMD's Sample_runs.csv (71 malformed rows rejected), so an orphan is either a
real run PGMD's sample table left out (fixable: the NCBI study ingest adds SRA's runs for the BioProject) or a PGMD
error. Each orphan gets one class:

  in_sra_project        a run of this BioProject in SRA runinfo: the NCBI ingest would add it
  suffix_in_sra_project an accession plus junk (ERR3015650_subreads, 'SRR… days') whose stripped form is a run of
                        this BioProject: PGMD mangled the name (stripped_in_core says if core.run already has it)
  suffix_not_in_project an accession plus junk whose stripped form is not in this BioProject's runinfo
  valid_not_in_project  a well-formed run accession SRA doesn't list under this BioProject (another project,
                        suppressed or withdrawn): needs a look per accession
  not_an_accession      not a run accession at all (PRJNA707602's 'sample1'): a PGMD error

Orphans come from bronze Species rows (the rows pgmd_profile loads), for the release tagged in bronze.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable

RUN_ACC = re.compile(r"^[SED]RR\d+$")
RUN_PREFIX = re.compile(r"^([SED]RR\d+)(?=\W|_)")          # an accession followed by junk
CLASSES = ("in_sra_project", "suffix_in_sra_project", "suffix_not_in_project", "valid_not_in_project",
           "not_an_accession")


@dataclass
class StudyOrphans:
    bioproject: str
    core_runs: int
    profiled_runs: int
    sra_runs: int
    sra_not_in_core: int                       # what the NCBI ingest would add for this BioProject
    classes: dict[str, list[str]] = field(default_factory=lambda: {c: [] for c in CLASSES})
    stripped_in_core: int = 0                  # suffix_in_sra_project whose stripped run is already a core.run row
    error: str | None = None

    @property
    def orphans(self) -> int:
        return sum(len(v) for v in self.classes.values())


def classify(orphan: str, sra_runs: set[str]) -> tuple[str, str | None]:
    """(class, stripped accession or None) for one orphan. Pure."""
    if RUN_ACC.match(orphan):
        return ("in_sra_project" if orphan in sra_runs else "valid_not_in_project"), None
    m = RUN_PREFIX.match(orphan)
    if m:
        stripped = m.group(1)
        return ("suffix_in_sra_project" if stripped in sra_runs else "suffix_not_in_project"), stripped
    return "not_an_accession", None


def survey_study(bioproject: str, profiled: set[str], core: set[str], sra: set[str]) -> StudyOrphans:
    """Classify one study's orphans (profiled runs not in core.run). Pure."""
    out = StudyOrphans(bioproject, len(core), len(profiled), len(sra), len(sra - core))
    for o in sorted(profiled - core):
        cls, stripped = classify(o, sra)
        out.classes[cls].append(o)
        if cls == "suffix_in_sra_project" and stripped in core:
            out.stripped_in_core += 1
    return out


def summary(studies: Iterable[StudyOrphans], detail: bool = False) -> str:
    studies = list(studies)
    if detail:
        lines = ["bioproject\torphan\tclass"]
        lines += [f"{s.bioproject}\t{o}\t{c}" for s in studies for c in CLASSES for o in s.classes[c]]
        return "\n".join(lines)
    lines = ["bioproject\tcore_runs\tprofiled_runs\torphans\t" + "\t".join(CLASSES)
             + "\tstripped_in_core\tsra_runs\tsra_not_in_core\texamples"]
    total: Counter = Counter()
    for s in studies:
        if s.error:
            lines.append(f"{s.bioproject}\t{s.core_runs}\t{s.profiled_runs}\t\t" + "\t" * len(CLASSES)
                         + f"\t\t\tERROR {s.error}")
            continue
        ex = "; ".join(f"{c}: {', '.join(s.classes[c][:3])}" for c in CLASSES if s.classes[c])
        lines.append("\t".join(str(x) for x in (s.bioproject, s.core_runs, s.profiled_runs, s.orphans,
                                                *(len(s.classes[c]) for c in CLASSES), s.stripped_in_core,
                                                s.sra_runs, s.sra_not_in_core, ex[:300])))
        total.update({c: len(s.classes[c]) for c in CLASSES})
        total["orphans"] += s.orphans
    lines.append(f"totals: {len(studies)} studies, {total['orphans']} orphans: "
                 + ", ".join(f"{c} {total[c]}" for c in CLASSES))
    return "\n".join(lines)


# ── the side-effect parts: bronze (Spark), core.run (Postgres), SRA (E-utilities) ──────────────

def profiled_runs_from_bronze() -> dict[str, set[str]]:
    """BioProject → runs that have Species rows in bronze, for the release tagged there (one small collect)."""
    from pyspark.sql import functions as F
    from app.config import settings
    from app.ingest.pgmd_parse import SOURCE_RELEASE
    from app.lake.pgmd_profile import LEVEL, Paths, one_commit
    from app.lake.spark import build_spark
    spark = build_spark("pgmd-orphans")
    try:
        paths = Paths.from_settings(settings)
        found = one_commit(spark, paths.bronze, source_release=SOURCE_RELEASE)
        if not found:
            raise SystemExit(f"bronze has no commit for {SOURCE_RELEASE}: load PGMD first")
        b = (spark.read.format("delta").option("versionAsOf", found[0]).load(paths.bronze)
             .filter((F.col("_source_release") == SOURCE_RELEASE) & (F.col("level") == LEVEL))
             .select(F.trim("BioProject").alias("bp"), F.trim("Runs").alias("run")).distinct())
        out: dict[str, set[str]] = defaultdict(set)
        for r in b.collect():                     # ~10k (BioProject, run) pairs
            out[r["bp"]].add(r["run"])
        print(f"bronze version {found[0]}: {sum(map(len, out.values()))} profiled runs in {len(out)} BioProjects",
              file=sys.stderr)
        return dict(out)
    finally:
        spark.stop()


async def core_runs() -> dict[str, set[str]]:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool
    from app.config import settings
    from app.models import Run, Study
    eng = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with AsyncSession(eng) as db:
            rows = (await db.execute(select(Study.bioproject_acc, Run.run_acc).join(Run, Run.study_id == Study.study_id))).all()
    finally:
        await eng.dispose()
    out: dict[str, set[str]] = defaultdict(set)
    for bp, run in rows:
        out[bp].add(run)
    return dict(out)


async def survey(profiled: dict[str, set[str]], only: list[str]) -> list[StudyOrphans]:
    from app.eutils import EUtils
    from app.ingest.ncbi_parse import parse_runinfo
    core = await core_runs()
    todo = sorted(bp for bp in (only or profiled) if profiled.get(bp, set()) - core.get(bp, set()))
    skipped = sorted(set(only) - set(todo))
    if skipped:
        print(f"no orphans (or not in bronze): {skipped}", file=sys.stderr)
    out = []
    async with EUtils.from_settings() as eu:
        for i, bp in enumerate(todo, 1):
            try:
                sra = {r.run for r in parse_runinfo(await eu.sra_runinfo(bp))}
                out.append(survey_study(bp, profiled[bp], core.get(bp, set()), sra))
            except Exception as e:            # one study's NCBI failure is reported, not fatal
                out.append(StudyOrphans(bp, len(core.get(bp, set())), len(profiled[bp]), 0, 0,
                                        error=f"{type(e).__name__}: {e}"[:200]))
            print(f"[{i}/{len(todo)}] {bp}: {out[-1].orphans} orphans" + (f" ERROR {out[-1].error}" if out[-1].error else ""),
                  file=sys.stderr)
        print(f"{eu.calls} E-utilities calls", file=sys.stderr)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.reports.pgmd_orphans", description=__doc__.splitlines()[0])
    ap.add_argument("bioprojects", nargs="*", help="limit to these BioProjects (default: all with orphans)")
    ap.add_argument("--detail", action="store_true", help="one row per orphan")
    args = ap.parse_args(argv)
    profiled = profiled_runs_from_bronze()
    studies = asyncio.run(survey(profiled, [b.strip().upper() for b in args.bioprojects]))
    print(summary(studies, args.detail))
    return 1 if any(s.error for s in studies) else 0


if __name__ == "__main__":
    sys.exit(main())
