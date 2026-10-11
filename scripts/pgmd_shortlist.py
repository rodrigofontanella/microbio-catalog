"""Step 1, pass 1: per-project facts from PGMD v1, to shortlist the study we curate perfectly.

Exploration, not a job. It reads the three CSVs once and prints one TSV row per BioProject to
stdout (a summary goes to stderr). Ranking happens afterwards, on the TSV. The production shape is
the Step 3 PGMD loader, which writes to Postgres under a prov.ingest_run.

Stdlib only, so it runs on Skipper's system python3 next to the data:

    ssh skipper python3 - /srv/bulk/raw/pgmd/v1 < scripts/pgmd_shortlist.py > data/pgmd_projects.tsv

Memory: a few MB (one set of ~10k run ids plus per-project counters). One streaming pass over
Microbial.csv (~293 MB), CPU-bound on csv parsing.
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

NULLS = {"", "NA", "无"}  # design doc §2.1.1; raw values are kept, only counting treats them as null
PROFILE_LEVEL = "Species"  # Microbial.csv stacks every rank: count reads at one level only

PROJECT_COLS = ("Bio_Project", "Phenotypes", "Phenotypes_info", "Experimental_design", "Category",
                "GrowthStage", "IsolationLocation", "Age", "Title", "Link", "SequencingTool",
                "Assay_type", "Country")
SAMPLE_COLS = ("Runs", "Bio_Project", "Bio_Sample", "Experimental_Design", "Group", "Sub_Group",
               "Growth_Stage", "Isolation_Location", "Age", "Library_Layout", "Platform")
MICROBIAL_COLS = ("level", "count", "Runs", "BioProject")


def norm(value: str | None) -> str | None:
    v = (value or "").strip()
    return None if v in NULLS else v


def split_list(value: str | None) -> list[str]:
    """`;` and `,` are both list separators in PGMD."""
    v = norm(value)
    if v is None:
        return []
    return [p.strip() for p in v.replace(",", ";").split(";") if p.strip()]


def fmt_counter(c: Counter[str], limit: int = 12) -> str:
    items = c.most_common(limit)
    more = len(c) - len(items)
    out = "|".join(f"{k}:{n}" for k, n in items)
    return out + (f"|…+{more}" if more > 0 else "")


def clean(text: str | None, width: int = 160) -> str:
    """One line, no tabs, bounded width: TSV-safe."""
    t = " ".join((text or "").split())
    return t if len(t) <= width else t[: width - 1] + "…"


def open_csv(path: Path, required: tuple[str, ...]) -> csv.DictReader:
    fh = path.open(newline="", encoding="utf-8-sig")
    reader = csv.DictReader(fh)
    missing = [c for c in required if c not in (reader.fieldnames or [])]
    if missing:
        sys.exit(f"{path.name}: missing columns {missing}; header is {reader.fieldnames}")
    return reader


@dataclass
class ProjectFacts:
    runs: set[str] = field(default_factory=set)
    biosamples: Counter[str] = field(default_factory=Counter)  # BioSample -> n runs
    sites_raw: Counter[str] = field(default_factory=Counter)  # Isolation_Location as written
    sites: Counter[str] = field(default_factory=Counter)  # after splitting lists
    groups: Counter[str] = field(default_factory=Counter)
    subgroups: Counter[str] = field(default_factory=Counter)
    designs: Counter[str] = field(default_factory=Counter)
    stages: Counter[str] = field(default_factory=Counter)
    ages: Counter[str] = field(default_factory=Counter)
    platforms: Counter[str] = field(default_factory=Counter)
    layouts: Counter[str] = field(default_factory=Counter)
    group_site: Counter[str] = field(default_factory=Counter)  # "group × site" cells


def read_samples(path: Path) -> dict[str, ProjectFacts]:
    facts: dict[str, ProjectFacts] = defaultdict(ProjectFacts)
    for row in open_csv(path, SAMPLE_COLS):
        pf = facts[row["Bio_Project"].strip()]
        run = row["Runs"].strip()
        pf.runs.add(run)
        if bs := norm(row["Bio_Sample"]):
            pf.biosamples[bs] += 1
        site = norm(row["Isolation_Location"]) or "NULL"
        pf.sites_raw[site] += 1
        for s in split_list(row["Isolation_Location"]) or ["NULL"]:
            pf.sites[s] += 1
        group = norm(row["Group"]) or "NULL"
        pf.groups[group] += 1
        pf.subgroups[norm(row["Sub_Group"]) or "NULL"] += 1
        pf.designs[norm(row["Experimental_Design"]) or "NULL"] += 1
        pf.stages[norm(row["Growth_Stage"]) or "NULL"] += 1
        pf.ages[norm(row["Age"]) or "NULL"] += 1
        pf.platforms[norm(row["Platform"]) or "NULL"] += 1
        pf.layouts[norm(row["Library_Layout"]) or "NULL"] += 1
        pf.group_site[f"{group} × {site}"] += 1
    return facts


def read_profiles(path: Path) -> tuple[dict[str, int], dict[str, set[str]]]:
    """Species-level read total per run, and profiled runs per project."""
    depth: dict[str, int] = defaultdict(int)
    by_project: dict[str, set[str]] = defaultdict(set)
    for row in open_csv(path, MICROBIAL_COLS):
        if row["level"] != PROFILE_LEVEL:
            continue
        run = row["Runs"].strip()
        try:
            depth[run] += int(float(row["count"] or 0))
        except ValueError:
            pass
        by_project[row["BioProject"].strip()].add(run)
    return depth, by_project


COLUMNS = ("bioproject", "n_runs", "n_biosamples", "max_runs_per_biosample", "n_profiled",
           "pct_profiled", "n_orphan_profiled", "median_depth", "n_sites", "multisite_label",
           "sites", "sites_raw", "n_groups", "groups", "subgroups", "group_x_site", "sample_designs",
           "growth_stages", "ages", "platforms", "layouts",
           "p_experimental_design", "p_phenotypes", "p_phenotypes_info", "p_category",
           "p_growthstage", "p_isolationlocation", "p_age", "p_assay_type", "p_sequencingtool",
           "p_country", "p_link", "p_title")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pgmd_dir", type=Path, help="directory holding PGMD v1 CSVs")
    args = ap.parse_args(argv)
    d: Path = args.pgmd_dir

    projects = {row["Bio_Project"].strip(): row for row in open_csv(d / "Project.csv", PROJECT_COLS)}
    samples = read_samples(d / "Sample_runs.csv")
    depth, profiled_by_project = read_profiles(d / "Microbial.csv")

    w = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    w.writerow(COLUMNS)
    all_ids = sorted(set(projects) | set(samples) | set(profiled_by_project))
    for pid in all_ids:
        pf = samples.get(pid, ProjectFacts())
        prof = profiled_by_project.get(pid, set())
        in_meta = prof & pf.runs
        depths = [depth[r] for r in prof]
        p = projects.get(pid, {})
        n_sites = len([s for s in pf.sites if s != "NULL"])
        w.writerow([
            pid, len(pf.runs), len(pf.biosamples), max(pf.biosamples.values(), default=0),
            len(in_meta), f"{len(in_meta) / len(pf.runs):.2f}" if pf.runs else "",
            len(prof - pf.runs), int(statistics.median(depths)) if depths else "",
            n_sites, int(any((";" in s or "," in s) for s in pf.sites_raw)),
            fmt_counter(pf.sites), fmt_counter(pf.sites_raw), len(pf.groups),
            fmt_counter(pf.groups), fmt_counter(pf.subgroups), fmt_counter(pf.group_site, 20),
            fmt_counter(pf.designs, 6), fmt_counter(pf.stages, 6), fmt_counter(pf.ages, 6),
            fmt_counter(pf.platforms, 4), fmt_counter(pf.layouts, 3),
            *(clean(p.get(c)) for c in ("Experimental_design", "Phenotypes", "Phenotypes_info",
                                         "Category", "GrowthStage", "IsolationLocation", "Age",
                                         "Assay_type", "SequencingTool", "Country", "Link")),
            clean(p.get("Title"), 200),
        ])

    n_runs = sum(len(pf.runs) for pf in samples.values())
    print(f"projects: Project.csv {len(projects)} · with sample rows {len(samples)} · "
          f"with {PROFILE_LEVEL} profiles {len(profiled_by_project)} · union {len(all_ids)}",
          file=sys.stderr)
    print(f"runs: Sample_runs {n_runs} · profiled {len(depth)}", file=sys.stderr)


if __name__ == "__main__":
    main()
