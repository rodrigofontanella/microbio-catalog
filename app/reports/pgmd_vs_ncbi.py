"""Conflicts report: PGMD curation vs NCBI submitter metadata, per study. Read-only.

    python -m app.reports.pgmd_vs_ncbi PRJNA667518
    python -m app.reports.pgmd_vs_ncbi PRJNA667518 --tsv data/conflicts_PRJNA667518.tsv

Verdicts:
  agree        same value (sites compared as UBERON terms, via app.ingest.vocab)
  granularity  compatible, but one side says less (PGMD 'Cecum' vs NCBI 'cecum mucosa' is agree on
               site; PGMD 'Illumina' vs NCBI 'Illumina MiSeq' is granularity)
  differ       incompatible values: a real conflict, a curation-queue item
  missing      one side has no value
  unmapped     a value the site dictionary doesn't know; the dictionary grows, not the data
Also reported: PGMD's own project-level values that contradict its sample rows, and animals that
PGMD would count twice at one site because it drops what was sampled there (pseudo-replicates).

Runs as any role with SELECT on core and prov (microbio_ro is enough).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.vocab import gut_sites, material, site_label
from app.models import Assertion, Library, Run, Sample, Study, StudySample

NCBI_SITE_FIELDS = ("tissue", "isolation_source", "body_site", "host_body_site", "host_body_habitat",
                    "env_local_scale", "host_tissue_sampled", "sample_type")
NCBI_MATERIAL_FIELDS = ("env_medium", "env_local_scale", "tissue", "isolation_source", "sample_type")
NCBI_TREATMENT_FIELDS = ("treatment", "diet", "group")
NCBI_SEX_FIELDS = ("host_sex", "sex")
PGMD_INTERNAL_FIELDS = ("isolation_location", "category", "growth_stage", "sex", "experimental_design",
                        "age", "weight", "country", "breed")


@dataclass(frozen=True)
class Finding:
    check: str
    entity: str
    pgmd: str
    ncbi: str
    verdict: str
    note: str = ""


def _norm(v: str | None) -> str:
    return " ".join((v or "").lower().split())


def compare_site(pgmd: str | None, ncbi_values: list[str]) -> tuple[str, str]:
    """(verdict, note) for one sample's site."""
    if not pgmd:
        return "missing", "PGMD has no site"
    p = gut_sites(pgmd)
    n = set().union(*(gut_sites(v) for v in ncbi_values)) if ncbi_values else set()
    if not ncbi_values:
        return "missing", "NCBI has no site field"
    if not p:
        return "unmapped", f"PGMD site {pgmd!r} not in the dictionary"
    if not n:
        return "unmapped", f"NCBI site {ncbi_values!r} not in the dictionary"
    if p == n:
        return "agree", ""
    if n < p:
        return "granularity", "PGMD lists several sites; NCBI names one"
    if p & n:
        return "granularity", "sites overlap"
    return "differ", f"PGMD {sorted(map(site_label, p))} vs NCBI {sorted(map(site_label, n))}"


def compare_platform(pgmd: str | None, platform: str | None, model: str | None) -> tuple[str, str]:
    p, pl, mo = _norm(pgmd), _norm(platform), _norm(model)
    if not p:
        return "missing", "PGMD has no platform"
    if not (pl or mo):
        return "missing", "NCBI has no platform"
    if p == mo:
        return "agree", ""
    if p == pl or (mo and mo.startswith(p)):
        return "granularity", "PGMD names the platform family"
    if p and mo and p.startswith(mo):
        return "granularity", "NCBI names less"
    return "differ", ""


def compare_text(pgmd: str | None, ncbi: str | None) -> tuple[str, str]:
    if not pgmd or not ncbi:
        return "missing", "PGMD has no value" if not pgmd else "NCBI has no value"
    return ("agree", "") if _norm(pgmd) == _norm(ncbi) else ("differ", "")


async def _assertions(db: AsyncSession, col, ids: list, method_like: str) -> dict:
    out: dict = defaultdict(lambda: defaultdict(list))
    if not ids:
        return out
    rows = await db.execute(select(col, Assertion.field, Assertion.value_text, Assertion.method)
                            .where(col.in_(ids), Assertion.method.like(method_like),
                                   Assertion.value_text.is_not(None)))
    for key, fld, val, method in rows:
        out[key][fld].append((val, method))
    return out


async def build_report(db: AsyncSession, bioproject: str) -> list[Finding]:
    study_id = (await db.execute(select(Study.study_id).where(Study.bioproject_acc == bioproject))).scalar_one()
    samples = (await db.execute(select(Sample.sample_id, Sample.biosample_acc, Sample.subject_id)
                                .join(StudySample, StudySample.sample_id == Sample.sample_id)
                                .where(StudySample.study_id == study_id))).all()
    sids = [s.sample_id for s in samples]
    ncbi = await _assertions(db, Assertion.sample_id, sids, "biosample_attr:%")
    pgmd_s = await _assertions(db, Assertion.sample_id, sids, "pgmd_curation:%")
    pgmd_study = (await _assertions(db, Assertion.study_id, [study_id], "pgmd_curation:%"))[study_id]

    def pgmd_value(sample_id: int, fld: str) -> str | None:
        """Sample row first; else the study-level value PGMD copied down (Sample_runs, then Project)."""
        if pgmd_s[sample_id].get(fld):
            return pgmd_s[sample_id][fld][0][0]
        by_file = {m: v for v, m in pgmd_study.get(fld, [])}
        return by_file.get("pgmd_curation:Sample_runs.csv") or by_file.get("pgmd_curation:Project.csv")

    def ncbi_first(sample_id: int, fields: tuple[str, ...]) -> str | None:
        return next((ncbi[sample_id][f][0][0] for f in fields if ncbi[sample_id].get(f)), None)

    out: list[Finding] = []
    site_of: dict[int, frozenset] = {}
    for s in samples:
        ncbi_sites = [v for f in NCBI_SITE_FIELDS for v, _ in ncbi[s.sample_id].get(f, [])]
        p_site = pgmd_value(s.sample_id, "isolation_location")
        verdict, note = compare_site(p_site, ncbi_sites)
        mat = next((m for f in NCBI_MATERIAL_FIELDS for v, _ in ncbi[s.sample_id].get(f, [])
                    if (m := material(v))), None)
        out.append(Finding("site", s.biosample_acc, p_site or "", "; ".join(ncbi_sites), verdict,
                           "; ".join(x for x in (note, f"NCBI material: {mat}" if mat else "") if x)))
        site_of[s.sample_id] = frozenset(gut_sites(p_site))

        v, n = compare_text(pgmd_value(s.sample_id, "group"), ncbi_first(s.sample_id, NCBI_TREATMENT_FIELDS))
        out.append(Finding("group_vs_treatment", s.biosample_acc, pgmd_value(s.sample_id, "group") or "",
                           ncbi_first(s.sample_id, NCBI_TREATMENT_FIELDS) or "", v, n))
        v, n = compare_text(pgmd_value(s.sample_id, "sex"), ncbi_first(s.sample_id, NCBI_SEX_FIELDS))
        out.append(Finding("sex", s.biosample_acc, pgmd_value(s.sample_id, "sex") or "",
                           ncbi_first(s.sample_id, NCBI_SEX_FIELDS) or "", v, n))

    # pseudo-replicates: one animal, several samples at what PGMD calls one site
    per_subject_site: Counter = Counter((s.subject_id, site_of[s.sample_id]) for s in samples
                                        if s.subject_id is not None and site_of[s.sample_id])
    doubled = {k: n for k, n in per_subject_site.items() if n > 1}
    if doubled:
        out.append(Finding("pseudo_replicates", bioproject, f"{len(doubled)} animal×site cells",
                           f"{sum(doubled.values())} samples", "differ",
                           f"PGMD's site alone gives up to {max(doubled.values())} samples per animal per site; "
                           "NCBI separates them by material"))

    runs = (await db.execute(select(Run.run_acc, Library.library_layout, Library.platform,
                                    Library.instrument_model)
                             .outerjoin(Library, Library.library_id == Run.library_id)
                             .where(Run.study_id == study_id))).all()
    pgmd_r = await _assertions(db, Assertion.run_acc, [r.run_acc for r in runs], "pgmd_curation:%")
    for r in runs:
        lay = (pgmd_r[r.run_acc].get("library_layout") or [(None, None)])[0][0]
        v, n = compare_text(lay, r.library_layout)
        out.append(Finding("layout", r.run_acc, lay or "", r.library_layout or "", v, n))
        plat = (pgmd_r[r.run_acc].get("platform") or [(None, None)])[0][0]
        v, n = compare_platform(plat, r.platform, r.instrument_model)
        out.append(Finding("platform", r.run_acc, plat or "",
                           " / ".join(x for x in (r.platform, r.instrument_model) if x), v, n))

    # PGMD against itself: project row vs its own sample rows
    for fld in PGMD_INTERNAL_FIELDS:
        proj = next((v for v, m in pgmd_study.get(fld, []) if m == "pgmd_curation:Project.csv"), None)
        rows = {v for v, m in pgmd_study.get(fld, []) if m == "pgmd_curation:Sample_runs.csv"}
        rows |= {vals[0][0] for sid in sids if (vals := pgmd_s[sid].get(fld))}
        if proj and rows and {_norm(proj)} != {_norm(x) for x in rows}:
            # 'Feed additives(Xylanase)' vs 'Xylanase': one names the other, so it's granularity
            nested = all(_norm(x) in _norm(proj) or _norm(proj) in _norm(x) for x in rows)
            out.append(Finding("pgmd_internal", f"{bioproject}:{fld}", proj, "; ".join(sorted(rows)),
                               "granularity" if nested else "differ",
                               "Project.csv vs Sample_runs.csv (the NCBI column holds PGMD's sample rows)"))
    return out


def summarize(findings: list[Finding]) -> str:
    by = defaultdict(Counter)
    example: dict = {}
    for f in findings:
        by[f.check][f.verdict] += 1
        if f.verdict != "agree":
            example.setdefault((f.check, f.verdict), f)
    lines = [f"{'check':<20} {'agree':>6} {'granul.':>8} {'differ':>7} {'missing':>8} {'unmapped':>9}"]
    for check, c in by.items():
        lines.append(f"{check:<20} {c['agree']:>6} {c['granularity']:>8} {c['differ']:>7} "
                     f"{c['missing']:>8} {c['unmapped']:>9}")
    lines.append("")
    for (check, verdict), f in example.items():
        lines.append(f"e.g. {check}/{verdict}: {f.entity}: PGMD {f.pgmd!r} | NCBI {f.ncbi!r}"
                     + (f" ({f.note})" if f.note else ""))
    return "\n".join(lines)


async def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.reports.pgmd_vs_ncbi", description=__doc__.splitlines()[0])
    ap.add_argument("bioproject", nargs="+")
    ap.add_argument("--tsv", type=Path, help="write every finding that isn't 'agree' to this file")
    args = ap.parse_args(argv)
    from app.database import AsyncSessionLocal
    findings: list[Finding] = []
    async with AsyncSessionLocal() as db:
        for acc in args.bioproject:
            fs = await build_report(db, acc.strip().upper())
            ncbi_loaded = any(f.ncbi for f in fs if f.check in ("site", "layout"))
            print(f"== {acc}" + ("" if ncbi_loaded else "   (no NCBI metadata loaded: 'missing' means not fetched yet)")
                  + f"\n{summarize(fs)}\n")
            findings += fs
    if args.tsv:
        with args.tsv.open("w", newline="") as fh:
            w = csv.writer(fh, delimiter="\t")
            w.writerow(Finding.__dataclass_fields__)
            w.writerows((f.check, f.entity, f.pgmd, f.ncbi, f.verdict, f.note)
                        for f in findings if f.verdict != "agree")
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
