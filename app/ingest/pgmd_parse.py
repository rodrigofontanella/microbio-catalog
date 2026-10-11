"""PGMD v1 (figshare 10.6084/m9.figshare.25911745.v1, CC BY 4.0): read Project.csv and
Sample_runs.csv and plan the assertions to load. Pure functions: no database.

Rules (design doc §2.1.1, extended after surveying all 201 projects on 2026-10-11):
- Null sentinels "", "NA", "无", "null" (after stripping) produce no assertion; the raw file keeps them.
- `Project.csv` fields → assertions on the study.
- `Sample_runs.csv` descriptive fields: constant over all of a project's rows → asserted once on the
  study (that's what PGMD did: project values copied down); varying → on each sample (BioSample).
- `Library_Layout`, `Platform`, `Release_Date` describe the sequencing → always on the run.
- List fields keep the raw string as value_text, with the parsed list in evidence["values"].
  Separators: ; , and their full-width forms ； ，.
- Age: when every part is "N day(s)", "N week(s)", "N - M days" or "N(±x) days", the bounds go into
  derived assertions age_min_days / age_max_days. A list of ages ("7 days; 21 days") is a set of
  possible ages, not a range measured on the sample; the bounds still hold, and evidence says "list".
"""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

SOURCE_RELEASE = "figshare:10.6084/m9.figshare.25911745.v1"
NULLS = {"", "na", "无", "null"}
LIST_SEP = re.compile(r"\s*[;,；，]\s*")

PROJECT_FIELDS = {  # Project.csv column → assertion field
    "Phenotypes": "phenotypes", "Phenotypes_info": "phenotypes_info",
    "Experimental_design": "experimental_design", "Country": "country", "Category": "category",
    "Breed": "breed", "GrowthStage": "growth_stage", "IsolationLocation": "isolation_location",
    "Age": "age", "Weight": "weight", "Sex": "sex", "Title": "title", "Description": "description",
    "Link": "link", "SequencingTool": "sequencing_tool", "ReleaseDate": "release_date",
    "Assay_type": "assay_type",
}
SAMPLE_FIELDS = {   # Sample_runs.csv: describe the specimen / animal
    "Experimental_Design": "experimental_design", "Group": "group", "Sub_Group": "sub_group",
    "Country": "country", "Category": "category", "Breed": "breed", "Growth_Stage": "growth_stage",
    "Isolation_Location": "isolation_location", "Age": "age", "Weight": "weight", "Sex": "sex",
}
RUN_FIELDS = {"Library_Layout": "library_layout", "Platform": "platform", "Release_Date": "release_date"}
LIST_FIELDS = {"sex", "category", "growth_stage", "isolation_location", "breed"}
REQUIRED_PROJECT = ("Bio_Project", *PROJECT_FIELDS)
REQUIRED_SAMPLE = ("Runs", "Bio_Project", "Bio_Sample", *SAMPLE_FIELDS, *RUN_FIELDS)

_AGE_PART = re.compile(
    r"^(?P<a>\d+(?:\.\d+)?)\s*(?:\(\s*±\s*\d+(?:\.\d+)?\s*\))?\s*(?:-\s*(?P<b>\d+(?:\.\d+)?)\s*)?"
    r"(?P<unit>days?|d|weeks?|wk|w)$", re.IGNORECASE)


@dataclass(frozen=True)
class Planned:
    """One assertion to load. target ∈ {'study','sample','run'}; key = BioProject / BioSample / run."""
    target: str
    key: str
    field: str
    value_text: str | None
    tier: str = "asserted"
    value_num: float | None = None
    unit: str | None = None
    method_file: str = ""
    evidence: dict = field(default_factory=dict, hash=False, compare=False)


@dataclass
class ProjectPlan:
    bioproject: str
    runs: dict[str, str]                 # run accession → BioSample accession
    assertions: list[Planned]
    nulls_skipped: int


def clean(value: str | None) -> str | None:
    """Strip; collapse internal whitespace; map PGMD null sentinels to None."""
    if value is None:
        return None
    v = " ".join(value.split())
    return None if v.lower() in NULLS else v


def split_list(value: str) -> list[str]:
    return [p for p in LIST_SEP.split(value) if p]


def parse_age_days(value: str) -> tuple[float, float, str] | None:
    """(min_days, max_days, kind) with kind 'single' | 'range' | 'list'; None when any part is free text."""
    parts = [p for p in re.split(r"\s*[;；]\s*", value) if p]
    bounds: list[float] = []
    kind = "single"
    for p in parts:
        m = _AGE_PART.match(p.strip())
        if not m:
            return None
        k = 7.0 if m["unit"].lower().startswith("w") else 1.0
        a = float(m["a"]) * k
        b = float(m["b"]) * k if m["b"] else a
        if m["b"]:
            kind = "range"
        bounds += [a, b]
    if not bounds:
        return None
    if len(parts) > 1:
        kind = "list"
    return min(bounds), max(bounds), kind


def read_csv(path: Path, required: tuple[str, ...]) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name}: missing columns {missing}")
        return list(reader)


def _assertion(target: str, key: str, fld: str, raw: str, value: str, method_file: str,
               extra: dict | None = None) -> list[Planned]:
    ev = {"file": method_file, "raw": raw, **(extra or {})}
    if fld in LIST_FIELDS:
        values = split_list(value)
        if len(values) > 1:
            ev["values"] = values
    out = [Planned(target, key, fld, value, method_file=method_file, evidence=ev)]
    if fld == "age":
        parsed = parse_age_days(value)
        if parsed:
            lo, hi, kind = parsed
            for name, num in (("age_min_days", lo), ("age_max_days", hi)):
                out.append(Planned(target, key, name, None, tier="derived", value_num=num, unit="d",
                                   method_file=method_file,
                                   evidence={"file": method_file, "from_field": "age", "raw": raw,
                                             "kind": kind, "rule": "pgmd_parse.parse_age_days"}))
    return out


def plan_project(bioproject: str, project_row: dict[str, str] | None,
                 sample_rows: list[dict[str, str]]) -> ProjectPlan:
    planned: list[Planned] = []
    nulls = 0

    if project_row:
        for col, fld in PROJECT_FIELDS.items():
            raw = project_row.get(col, "")
            v = clean(raw)
            if v is None:
                nulls += 1
                continue
            planned += _assertion("study", bioproject, fld, raw, v, "Project.csv")

    runs: dict[str, str] = {}
    for r in sample_rows:
        runs[clean(r["Runs"])] = clean(r["Bio_Sample"])

    # descriptive fields: constant over the project's rows → study; varying → per sample
    for col, fld in SAMPLE_FIELDS.items():
        cleaned = [(clean(r["Bio_Sample"]), r[col], clean(r[col])) for r in sample_rows]
        distinct = {v for _, _, v in cleaned}
        nulls += sum(1 for _, _, v in cleaned if v is None)
        if len(distinct) == 1:
            v = next(iter(distinct))
            if v is not None:
                raw = next(raw for _, raw, cv in cleaned if cv == v)
                planned += _assertion("study", bioproject, fld, raw, v, "Sample_runs.csv",
                                      {"constant_over_rows": len(cleaned)})
            continue
        seen: set[tuple[str, str]] = set()
        for bs, raw, v in cleaned:
            if v is None or (bs, v) in seen:
                continue
            seen.add((bs, v))
            planned += _assertion("sample", bs, fld, raw, v, "Sample_runs.csv")

    # sequencing descriptors: on the run, always
    for r in sample_rows:
        run = clean(r["Runs"])
        for col, fld in RUN_FIELDS.items():
            v = clean(r[col])
            if v is None:
                nulls += 1
                continue
            planned += _assertion("run", run, fld, r[col], v, "Sample_runs.csv")

    return ProjectPlan(bioproject, runs, planned, nulls)


def group_rows(project_rows: list[dict[str, str]], sample_rows: list[dict[str, str]]
               ) -> tuple[dict[str, dict[str, str]], dict[str, list[dict[str, str]]]]:
    projects: dict[str, dict[str, str]] = {}
    for r in project_rows:
        acc = clean(r["Bio_Project"])
        if acc in projects:
            raise ValueError(f"Project.csv: {acc} appears twice")
        projects[acc] = r
    samples: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in sample_rows:
        samples[clean(r["Bio_Project"])].append(r)
    return projects, samples


RUN_RE = re.compile(r"^[SED]RR\d+$")
BIOSAMPLE_RE = re.compile(r"^SAM[NED][A-Z]?\d+$")


def split_valid_rows(sample_rows: list[dict[str, str]]
                     ) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    """(valid rows, rejected rows with a 'reason', errors that stop the load).

    A row whose run or BioSample isn't a well-formed accession is rejected, not repaired:
    `ERR3015650_subreads` → `ERR3015650` looks safe, but 3 of the stripped ids collide with
    existing runs, so the repair would be a guess. Rejected rows are reported and recorded.
    """
    valid, rejected = [], []
    for r in sample_rows:
        run, bs = clean(r["Runs"]) or "", clean(r["Bio_Sample"]) or ""
        reason = ("bad run accession" if not RUN_RE.match(run)
                  else "bad BioSample accession" if not BIOSAMPLE_RE.match(bs) else None)
        if reason:
            rejected.append({"bioproject": clean(r["Bio_Project"]), "runs": r["Runs"],
                             "bio_sample": r["Bio_Sample"], "reason": reason})
        else:
            valid.append(r)
    runs = [clean(r["Runs"]) for r in valid]
    dupes = sorted({x for x in runs if runs.count(x) > 1}) if len(runs) != len(set(runs)) else []
    errors = [f"Sample_runs.csv: duplicate run accessions {dupes[:5]}"] if dupes else []
    return valid, rejected, errors
