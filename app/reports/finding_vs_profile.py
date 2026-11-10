"""Paper findings vs a lakehouse profile of the same runs: does the profile show what the paper claims? Read-only.

    python -m app.reports.finding_vs_profile conf/match/PRJNA430317.yml
    python -m app.reports.finding_vs_profile conf/match/PRJNA430317.yml --out docs/reports

Why: the research copilot will be asked "does the data support claim X". This is that answer, built by hand for
one study, so the traps (name joins, depth, agreement read as validation) are visible before an agent does it
unsupervised (handoff-2026-11-10-paper-vs-profile-PRJNA430317).

Inputs:
  - core.finding rows of the study with measure relative_abundance and feature kind genus or phylum (curated);
  - a reviewed claim map, conf/match/<BioProject>.yml: each claim → a taxon selector (lineage prefix) or a
    not-testable reason, plus which samples a finding's site_term means. Every claim must be listed; an
    unsigned map still runs, and the report says DRAFT in its header;
  - core.profile (source from the map): silver/profile_abundance read VERSION AS OF profile.delta_version (OSS
    delta-spark time travel), silver/feature_taxon at its latest version, recorded in the header and read AS
    OF that version so the run is repeatable. Both tables are append-only; a profile_key's rows are never
    rewritten.

Decisions applied (Rodrigo, 2026-11-10):
  - unit = one pig: one run per (subject, site), diet from the design (core.subject_factor_level), checked
    against a BioSample field when the map names one (`level_check_field`);
  - a feature = a lineage prefix (6 segments for genus, 2 for phylum), never a bare name. Its count in a run =
    the Species counts under that prefix (a genus's `Genus;__` remainder included); relative abundance = that
    count / the run's total count (unrarefied; PGMD's rel_abund isn't used);
  - per site, per rank, per scenario: units present (count > 0) in >= MIN_PREVALENCE pigs, counted across both
    diets together, are tested with Kruskal-Wallis (the paper's test; two groups) and BH-corrected across all of
    them (the paper's apparent scope). Below prevalence = not testable, and listed;
  - PGMD direction = the sign of (mean rank treatment - mean rank reference), the quantity the test compares;
    medians are printed but are often both 0;
  - verdict (only on the claim's own lineage; `also` lineages are tested, no verdict):
      directional claim: reproduced (same direction, q < ALPHA) | direction_only (same, q >= ALPHA) |
                         opposite_ns | contradicted (opposite, q < ALPHA) | no_direction (equal mean ranks)
      no_difference claim: consistent_null (q >= ALPHA; weak evidence at n = 9) | contradicted (q < ALPHA)
  - depth is a flag, never a filter: scenario `all` is the result; `no_fail` (drop depth fail) and `ok_only`
    (depth ok only) are sensitivity columns, `n/a` where either diet keeps < MIN_PER_DIET pigs at the site and
    `= all` where nothing is dropped.

What it can't tell: the paper and PGMD start from the same reads. Agreement says the claim survives
reprocessing (another reference, no rarefaction, collapsed taxa), not that it is biologically true; a
disagreement may be the pipeline (Greengenes 13_8 names, depth, normalisation), not the biology.

Spark shape: two filtered reads of one file each (data skipping on profile_key stats), no join, no shuffle,
no spill: ~54 runs x a few hundred features, collected to the driver; everything else is plain Python. local[2]
and 4 shuffle partitions (settings) are already more than this needs. Postgres runs in a READ ONLY transaction,
so the report can't write even with a writable role (microbio_ro is enough).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator
from scipy.stats import false_discovery_control, kruskal, rankdata

from app.lake.lineage import RANKS, split_lineage

ALPHA = 0.05
MIN_PREVALENCE = 3          # pigs with count > 0 at the site, both diets together (decided 2026-11-10)
MIN_PER_DIET = 3            # a sensitivity scenario with fewer pigs in either diet is n/a
RANK_DEPTH = {"phylum": 2, "genus": 6}
SCENARIOS = ("all", "no_fail", "ok_only")
DIRECTIONAL = ("increase", "decrease")


# ── the claim map (conf/match/<BioProject>.yml) ───────────────────────────────────────────────────────────

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimMap(_Strict):
    taxon: dict[str, str] | None = None
    also: list[dict[str, str]] = []
    not_testable: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def one_way(self) -> "ClaimMap":
        if (self.taxon is None) == (self.not_testable is None):
            raise ValueError("a claim maps to a taxon or is not_testable, exactly one")
        if self.also and self.taxon is None:
            raise ValueError("'also' needs a taxon")
        for sel in ([self.taxon] if self.taxon is not None else []) + self.also:
            bad = [k for k in sel if k not in RANKS]
            if not sel or bad:
                raise ValueError(f"taxon selector needs ranks from {RANKS}, got {sorted(sel) or 'none'}")
        return self


class Review(_Strict):
    reviewed_by: str | None = None
    reviewed_on: date | None = None


class MatchSpec(_Strict):
    bioproject: str
    pmcid: str
    profile_source: str
    level_check_field: str | None = None
    review: Review
    sites: dict[str, dict[str, str]]
    claims: dict[str, ClaimMap]

    @property
    def signed(self) -> bool:
        return bool(self.review.reviewed_by and self.review.reviewed_on)


def load_spec(path: Path) -> MatchSpec:
    return MatchSpec.model_validate(yaml.safe_load(path.read_text()))


# ── catalog rows, as plain data ───────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Claim:
    local_id: str
    feature_kind: str               # genus | phylum
    feature_label: str
    site_term: str
    direction: str                  # increase | decrease | no_difference
    treatment: str                  # level labels of a pairwise contrast
    reference: str
    paper_stat: str = ""            # e.g. 'Q < 0.05', as stored


@dataclass(frozen=True)
class RunInfo:
    run_acc: str
    subject: str
    diet: str
    fields: dict[str, str]          # BioSample selector fields of the run's sample
    depth_status: str | None
    n_reads: int | None


def check_coverage(spec: MatchSpec, claims: list[Claim]) -> None:
    """Every testable finding is in the map and the map names nothing else; every claim's site is mapped."""
    ids = {c.local_id for c in claims}
    missing, extra = sorted(ids - spec.claims.keys()), sorted(spec.claims.keys() - ids)
    if missing or extra:
        raise ValueError(f"claim map out of step with core.finding: unlisted {missing}, unknown {extra}")
    sites = sorted({c.site_term for c in claims} - spec.sites.keys())
    if sites:
        raise ValueError(f"site_term(s) with no sample selector in the map: {sites}")
    for c in claims:
        if c.feature_kind not in RANK_DEPTH:
            raise ValueError(f"{c.local_id}: feature kind {c.feature_kind!r} not handled")
        m = spec.claims[c.local_id]
        if m.taxon and selector_rank(m.taxon) != c.feature_kind:
            raise ValueError(f"{c.local_id}: a {c.feature_kind} claim mapped to a {selector_rank(m.taxon)} selector")


# ── taxa: selectors, lineage prefixes, per-run relative abundance ─────────────────────────────────────────

def selector_rank(sel: dict[str, str]) -> str:
    return max(sel, key=RANKS.index)


def prefix(feature_id: str, depth: int) -> str:
    return ";".join(split_lineage(feature_id)[:depth])


def resolve(sel: dict[str, str], taxa: dict[str, dict[str, Any]]) -> str:
    """The ONE lineage prefix a selector names. Stops on no match or on two prefixes (ambiguous, never summed)."""
    depth = RANKS.index(selector_rank(sel)) + 1
    hits = {prefix(fid, depth) for fid, t in taxa.items() if all(t.get(r) == v for r, v in sel.items())}
    if len(hits) != 1:
        what = "matches nothing" if not hits else f"is ambiguous: {sorted(hits)}"
        raise ValueError(f"taxon selector {sel} {what} in the profile")
    return hits.pop()


def units(taxa: dict[str, dict[str, Any]], rank: str) -> dict[str, str]:
    """feature_id → its prefix at `rank`, for features named at that rank (family remainders have no genus)."""
    depth = RANK_DEPTH[rank]
    return {fid: prefix(fid, depth) for fid, t in taxa.items() if t.get(rank)}


def rel_abundance(rows: list[tuple[str, str, int]], unit_of: dict[str, str]) -> dict[str, dict[str, float]]:
    """run → unit → count / the run's total count (every Species row of the run in the total)."""
    total: Counter[str] = Counter()
    per: dict[str, Counter[str]] = defaultdict(Counter)
    for run, fid, count in rows:
        total[run] += count
        if fid in unit_of:
            per[run][unit_of[fid]] += count
    return {run: {u: c / total[run] for u, c in per[run].items()} for run in total if total[run] > 0}


# ── statistics ────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class UnitTest:
    unit: str
    n_t: int
    n_r: int
    present_t: int
    present_r: int
    median_t: float
    median_r: float
    direction: str | None = None     # increase | decrease | none; None = below prevalence
    p: float | None = None
    q: float | None = None


def site_tests(rel: dict[str, dict[str, float]], diet: dict[str, str], treatment: str, reference: str,
               min_prevalence: int = MIN_PREVALENCE) -> dict[str, UnitTest]:
    """One BH family: every unit seen in these runs. `rel` holds one run per pig, already selected."""
    t_runs = sorted(r for r in rel if diet[r] == treatment)
    r_runs = sorted(r for r in rel if diet[r] == reference)
    all_units = sorted({u for r in t_runs + r_runs for u in rel[r]})
    out: dict[str, UnitTest] = {}
    for u in all_units:
        tv = [rel[r].get(u, 0.0) for r in t_runs]
        rv = [rel[r].get(u, 0.0) for r in r_runs]
        out[u] = UnitTest(u, len(tv), len(rv), sum(v > 0 for v in tv), sum(v > 0 for v in rv),
                          statistics.median(tv) if tv else 0.0, statistics.median(rv) if rv else 0.0)
        if out[u].present_t + out[u].present_r < min_prevalence or not tv or not rv:
            continue
        ranks = rankdata(tv + rv)
        diff = ranks[:len(tv)].mean() - ranks[len(tv):].mean()
        out[u].direction = "increase" if diff > 0 else "decrease" if diff < 0 else "none"
        try:
            out[u].p = float(kruskal(tv, rv).pvalue)
        except ValueError:          # every value identical (only possible if every pig has the same share)
            out[u].p = 1.0
    tested = [x for x in out.values() if x.p is not None]
    if tested:
        for x, q in zip(tested, false_discovery_control([x.p for x in tested], method="bh")):
            x.q = float(q)
    return out


def verdict(claim_direction: str, res: UnitTest | None, alpha: float = ALPHA) -> str:
    if res is None or res.q is None:
        return "not_testable"
    sig = res.q < alpha
    if claim_direction == "no_difference":
        return "contradicted" if sig else "consistent_null"
    if claim_direction not in DIRECTIONAL:
        raise ValueError(f"direction {claim_direction!r} has no verdict rule")
    if res.direction == "none":
        return "no_direction"
    if res.direction == claim_direction:
        return "reproduced" if sig else "direction_only"
    return "contradicted" if sig else "opposite_ns"


# ── runs: site selection, one run per pig, depth scenarios ────────────────────────────────────────────────

def site_runs(runs: list[RunInfo], selector: dict[str, str], levels: tuple[str, str]) -> list[RunInfo]:
    """The runs a site means, in the contrast's two levels; stops if a pig has two runs there."""
    picked = [r for r in runs if r.diet in levels and all(r.fields.get(k) == v for k, v in selector.items())]
    dup = sorted(s for s, n in Counter(r.subject for r in picked).items() if n > 1)
    if dup:
        raise ValueError(f"site {selector}: pigs with more than one run (pseudo-replicates): {dup}")
    return picked


def scenario(picked: list[RunInfo], name: str, levels: tuple[str, str],
             min_per_diet: int = MIN_PER_DIET) -> list[RunInfo] | str:
    """The runs a depth scenario keeps, or 'n/a' (a diet below min_per_diet) or '= all' (nothing dropped)."""
    keep = {"all": lambda r: True, "no_fail": lambda r: r.depth_status != "fail",
            "ok_only": lambda r: r.depth_status == "ok"}[name]
    kept = [r for r in picked if keep(r)]
    if name != "all" and len(kept) == len(picked):
        return "= all"
    if any(sum(r.diet == lv for r in kept) < min_per_diet for lv in levels):
        return "n/a"
    return kept


# ── the analysis ──────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Row:
    claim: Claim
    role: Literal["verdict", "also"]
    mapped: str                       # the lineage prefix, or the not-testable reason
    scenario: str
    verdict: str
    reason: str = ""
    res: UnitTest | None = None


@dataclass
class Family:
    site_term: str
    rank: str
    scenario: str
    n_t: int
    n_r: int
    tested: int
    below_prevalence: list[str] = field(default_factory=list)


def analyse(spec: MatchSpec, claims: list[Claim], runs: list[RunInfo], rows: list[tuple[str, str, int]],
            taxa: dict[str, dict[str, Any]], min_prevalence: int = MIN_PREVALENCE,
            alpha: float = ALPHA) -> tuple[list[Row], list[Family]]:
    check_coverage(spec, claims)
    run_by_acc = {r.run_acc: r for r in runs}
    rel_by_rank = {rank: rel_abundance(rows, units(taxa, rank)) for rank in {c.feature_kind for c in claims}}
    cache: dict[tuple, dict[str, UnitTest] | str] = {}
    families: list[Family] = []

    def tests(c: Claim, sc: str) -> dict[str, UnitTest] | str:
        key = (c.site_term, c.feature_kind, sc, c.treatment, c.reference)
        if key not in cache:
            levels = (c.treatment, c.reference)
            kept = scenario(site_runs(runs, spec.sites[c.site_term], levels), sc, levels)
            if isinstance(kept, str):
                cache[key] = kept
            else:
                rel = {r.run_acc: rel_by_rank[c.feature_kind].get(r.run_acc, {}) for r in kept}
                res = site_tests(rel, {a: run_by_acc[a].diet for a in rel}, c.treatment, c.reference, min_prevalence)
                cache[key] = res
                families.append(Family(c.site_term, c.feature_kind, sc,
                                       sum(r.diet == c.treatment for r in kept),
                                       sum(r.diet == c.reference for r in kept),
                                       sum(x.q is not None for x in res.values()),
                                       sorted(u for u, x in res.items() if x.q is None)))
        return cache[key]

    out: list[Row] = []
    for c in sorted(claims, key=lambda c: c.local_id):
        m = spec.claims[c.local_id]
        if m.not_testable:
            out += [Row(c, "verdict", m.not_testable, sc, "not_testable", "map") for sc in SCENARIOS]
            continue
        targets = [("verdict", resolve(m.taxon, taxa))] + [("also", resolve(a, taxa)) for a in m.also]
        for role, unit in targets:
            for sc in SCENARIOS:
                res = tests(c, sc)
                if isinstance(res, str):
                    out.append(Row(c, role, unit, sc, res))
                    continue
                x = res.get(unit)
                v = verdict(c.direction, x, alpha) if role == "verdict" else "(no verdict)"
                why = "absent at this site" if x is None else "below prevalence" if x.q is None else ""
                if role == "also" and why:
                    v = "not_testable"
                out.append(Row(c, role, unit, sc, v, why, x))
    return out, families


# ── output ────────────────────────────────────────────────────────────────────────────────────────────────

def _short(unit: str) -> str:
    """The last two named segments of a prefix: 'Prevotellaceae;Prevotella'."""
    return ";".join(s for s in unit.split(";")[-2:] if s)


def _num(x: float | None, nd: int = 3) -> str:
    return "" if x is None else f"{x:.{nd}g}"


def _pct(x: float) -> str:
    return f"{100 * x:.2f}%"


TSV_COLUMNS = ("claim_id", "role", "scenario", "site_term", "feature_kind", "paper_label", "paper_direction",
               "paper_stat", "mapped", "n_t", "n_r", "present_t", "present_r", "median_rel_t", "median_rel_r",
               "pgmd_direction", "p", "q", "verdict", "reason")


def tsv_rows(rows: list[Row]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        x = r.res
        out.append({"claim_id": r.claim.local_id, "role": r.role, "scenario": r.scenario,
                    "site_term": r.claim.site_term, "feature_kind": r.claim.feature_kind,
                    "paper_label": r.claim.feature_label, "paper_direction": r.claim.direction,
                    "paper_stat": r.claim.paper_stat, "mapped": r.mapped,
                    "n_t": x.n_t if x else "", "n_r": x.n_r if x else "",
                    "present_t": x.present_t if x else "", "present_r": x.present_r if x else "",
                    "median_rel_t": f"{x.median_t:.6g}" if x else "", "median_rel_r": f"{x.median_r:.6g}" if x else "",
                    "pgmd_direction": (x.direction or "") if x else "", "p": _num(x.p if x else None, 6),
                    "q": _num(x.q if x else None, 6), "verdict": r.verdict, "reason": r.reason})
    return out


def markdown(rows: list[Row], families: list[Family], header: dict[str, Any], site_names: dict[str, str],
             depth_table: list[tuple[str, str, str, int]], notes: dict[str, str] | None = None) -> str:
    L = [f"# Paper vs profile: {header['bioproject']} ({header['pmcid']})", ""]
    if not header["signed"]:
        L += ["> **DRAFT**: the claim map is not signed (review.reviewed_by / reviewed_on empty).", ""]
    L += [f"- profile `{header['profile_key']}`",
          f"- profile_abundance `{header['delta_uri']}` VERSION AS OF **{header['delta_version']}**; "
          f"feature_taxon VERSION AS OF **{header['taxon_version']}**",
          f"- claim map `{header['map_path']}` sha256 `{header['map_sha256'][:12]}…`"
          + (f", signed by {header['reviewed_by']} on {header['reviewed_on']}" if header["signed"] else ""),
          f"- Kruskal-Wallis (two groups) on unrarefied relative abundance; BH per site, rank and scenario over every "
          f"unit present in >= {header['min_prevalence']} pigs (both diets together); verdict at q < {header['alpha']}",
          "- unit = one pig (one run per pig per site); diet from the design; PGMD direction = sign of mean-rank "
          "difference (treatment - reference)", ""]
    L += ["## Runs per site, diet and depth", "", "| site | diet | depth | runs |", "|---|---|---|---|"]
    L += [f"| {s} | {d} | {st} | {n} |" for s, d, st, n in depth_table]
    L += ["", "## Results (scenario `all`: every run; depth is a flag, not a filter)", "",
          "| claim | site | paper | PGMD taxon | present T/R | median T vs R | PGMD dir | p | q | verdict |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r.scenario != "all":
            continue
        x, c = r.res, r.claim
        paper = f"{c.feature_label} {c.direction}" + (f" ({c.paper_stat})" if c.paper_stat else "")
        if r.reason == "map":
            taxon = f"— {r.mapped}"
        else:
            taxon = _short(r.mapped) + (" *(also)*" if r.role == "also" else "")
        L.append(f"| {c.local_id} | {site_names.get(c.site_term, c.site_term)} | {paper} | {taxon} | "
                 + (f"{x.present_t}/{x.n_t} vs {x.present_r}/{x.n_r} | {_pct(x.median_t)} vs {_pct(x.median_r)} | "
                    f"{x.direction or ''} | {_num(x.p)} | {_num(x.q)} | " if x else "| | | | | ")
                 + f"**{r.verdict}**" + (f" ({r.reason})" if r.reason and r.reason != "map" else "") + " |")
    L += ["", "Present T/R = pigs with count > 0, treatment / reference. T = " + header["treatment"]
          + ", R = " + header["reference"] + ".", ""]
    if notes:
        L += ["Map notes:", ""] + [f"- `{cid}`: {n}" for cid, n in sorted(notes.items())] + [""]
    L += ["## Depth sensitivity", "", "| claim | role | all | no_fail | ok_only | q (ok_only) |", "|---|---|---|---|---|---|"]
    by: dict[tuple[str, str, str], dict[str, Row]] = defaultdict(dict)
    for r in rows:
        by[(r.claim.local_id, r.role, r.mapped)][r.scenario] = r
    for (cid, role, _), d in by.items():
        ok = d["ok_only"].res
        L.append(f"| {cid} | {role} | {d['all'].verdict} | {d['no_fail'].verdict} | {d['ok_only'].verdict} | "
                 f"{_num(ok.q) if ok else ''} |")
    L += ["", "## Test families", "", "| site | rank | scenario | pigs T/R | tested | below prevalence |",
          "|---|---|---|---|---|---|"]
    for f in sorted(families, key=lambda f: (f.site_term, f.rank, SCENARIOS.index(f.scenario))):
        L.append(f"| {site_names.get(f.site_term, f.site_term)} | {f.rank} | {f.scenario} | {f.n_t}/{f.n_r} | "
                 f"{f.tested} | {len(f.below_prevalence)} |")
    summary = Counter(r.verdict for r in rows if r.scenario == "all" and r.role == "verdict")
    L += ["", "## Summary (scenario `all`, verdict rows)", "",
          ", ".join(f"{k} {v}" for k, v in sorted(summary.items())), "",
          "## What this can't tell", "",
          "The paper and PGMD start from the same reads. Agreement says the claim survives reprocessing (Greengenes "
          "13_8 instead of the paper's reference, no rarefaction, taxa collapsed to species), not that it is "
          "biologically true. Disagreement may be the pipeline, not the biology. A `consistent_null` at nine pigs "
          "per diet is weak evidence. Unrarefied data: a shallower group detects fewer rare taxa, so presence-based "
          "differences in rare genera can follow depth (compare the depth table).", ""]
    return "\n".join(L)


# ── I/O: Postgres (read only) and Delta ───────────────────────────────────────────────────────────────────

CLAIMS_SQL = """
SELECT f.local_id, f.feature_kind, f.feature_label, f.site_term, f.direction, f.contrast_kind, f.factor_id,
       tl.label AS treatment, rl.label AS reference, f.stat_kind, f.stat_op, f.stat_value
FROM core.finding f
LEFT JOIN core.factor_level tl ON tl.level_id = f.treatment_level_id
LEFT JOIN core.factor_level rl ON rl.level_id = f.reference_level_id
WHERE f.study_id = :sid AND f.measure = 'relative_abundance' AND f.feature_kind IN ('genus', 'phylum')
ORDER BY f.local_id
"""

RUNS_SQL = """
SELECT r.run_acc, r.sample_id, sub.local_id AS subject, fl.label AS diet, pr.depth_status, pr.n_reads,
       pr.run_acc IS NOT NULL AS profiled
FROM core.run r
JOIN core.sample s ON s.sample_id = r.sample_id
LEFT JOIN core.subject sub ON sub.subject_id = s.subject_id
LEFT JOIN core.subject_factor_level sfl ON sfl.subject_id = s.subject_id AND sfl.factor_id = :fid
LEFT JOIN core.factor_level fl ON fl.level_id = sfl.level_id
LEFT JOIN core.profile_run pr ON pr.run_acc = r.run_acc AND pr.profile_id = :pid
WHERE r.study_id = :sid
"""

FIELDS_SQL = """
SELECT sample_id, field, array_agg(DISTINCT value_text) AS vals
FROM prov.assertion
WHERE sample_id = ANY(:ids) AND field = ANY(:fields) AND method LIKE 'biosample_attr:%'
GROUP BY 1, 2
"""


def _stat(r: dict[str, Any]) -> str:
    if not r["stat_kind"]:
        return ""
    return f"{r['stat_kind']} {r['stat_op']} {float(r['stat_value']):g}"


async def load_catalog(db, spec: MatchSpec) -> dict[str, Any]:
    from sqlalchemy import text
    await db.execute(text("SET TRANSACTION READ ONLY"))
    sid = (await db.execute(text("SELECT study_id FROM core.study WHERE bioproject_acc = :bp"),
                            {"bp": spec.bioproject})).scalar_one()
    profiles = (await db.execute(text(
        "SELECT profile_id, profile_key, delta_uri, delta_version FROM core.profile "
        "WHERE study_id = :sid AND source = :src"), {"sid": sid, "src": spec.profile_source})).mappings().all()
    if len(profiles) != 1:
        raise ValueError(f"{spec.bioproject}: expected one {spec.profile_source} profile, found {len(profiles)}")
    prof = dict(profiles[0])
    raw = [dict(r) for r in (await db.execute(text(CLAIMS_SQL), {"sid": sid})).mappings().all()]
    bad = [r["local_id"] for r in raw if r["contrast_kind"] != "pairwise"]
    factors = {r["factor_id"] for r in raw}
    if bad or len(factors) != 1:
        raise ValueError(f"expected pairwise claims on one factor: non-pairwise {bad}, factors {sorted(factors)}")
    claims = [Claim(r["local_id"], r["feature_kind"], r["feature_label"], r["site_term"], r["direction"],
                    r["treatment"], r["reference"], _stat(r)) for r in raw]
    runs_raw = [dict(r) for r in (await db.execute(text(RUNS_SQL), {"sid": sid, "fid": factors.pop(),
                                                                     "pid": prof["profile_id"]})).mappings().all()]
    wanted = sorted({k for sel in spec.sites.values() for k in sel} | ({spec.level_check_field} - {None}))
    vals: dict[int, dict[str, str]] = defaultdict(dict)
    for r in (await db.execute(text(FIELDS_SQL), {"ids": [r["sample_id"] for r in runs_raw],
                                                  "fields": wanted})).mappings().all():
        if len(r["vals"]) != 1:
            raise ValueError(f"sample {r['sample_id']}: field {r['field']} has several values {r['vals']}")
        vals[r["sample_id"]][r["field"]] = r["vals"][0]
    problems = [r["run_acc"] for r in runs_raw if not r["profiled"] or r["diet"] is None or r["subject"] is None]
    if problems:
        raise ValueError(f"runs not profiled, without a pig, or without a diet level: {sorted(problems)}")
    if spec.level_check_field:
        off = sorted(r["run_acc"] for r in runs_raw
                     if vals[r["sample_id"]].get(spec.level_check_field) != r["diet"])
        if off:
            raise ValueError(f"design level disagrees with {spec.level_check_field} for runs {off}")
    runs = [RunInfo(r["run_acc"], r["subject"], r["diet"], vals[r["sample_id"]], r["depth_status"], r["n_reads"])
            for r in runs_raw]
    return {"profile": prof, "claims": claims, "runs": runs}


def _check_types(df, schema, cols: list[str], table: str) -> None:
    """Delta reads take the schema from the log; this pins the columns we use to the job's explicit schema."""
    got = {f.name: f.dataType for f in df.schema.fields}
    drift = [c for c in cols if got.get(c) != schema[c].dataType]
    if drift:
        raise ValueError(f"{table}: column types drifted from app.lake.pgmd_profile schema: {drift}")


def read_profile(spark, profile: dict[str, Any], taxon_uri: str) -> dict[str, Any]:
    from delta.tables import DeltaTable
    from pyspark.sql import functions as F

    from app.lake.pgmd_profile import abundance_schema, taxon_schema

    key = profile["profile_key"]
    ab = (spark.read.format("delta").option("versionAsOf", int(profile["delta_version"]))
          .load(profile["delta_uri"]).where(F.col("profile_key") == key))
    _check_types(ab, abundance_schema(), ["profile_key", "run_acc", "feature_id", "count"], "profile_abundance")
    tv = int(DeltaTable.forPath(spark, taxon_uri).history(1).select("version").first()[0])
    tx = spark.read.format("delta").option("versionAsOf", tv).load(taxon_uri).where(F.col("profile_key") == key)
    _check_types(tx, taxon_schema(), ["profile_key", "feature_id", *RANKS], "feature_taxon")
    rows = [(r["run_acc"], r["feature_id"], int(r["count"])) for r in ab.select("run_acc", "feature_id", "count").collect()]
    taxa_rows = tx.select("feature_id", *RANKS).collect()
    taxa = {r["feature_id"]: {k: r[k] for k in RANKS} for r in taxa_rows}
    if len(taxa) != len(taxa_rows):
        raise ValueError(f"feature_taxon has duplicate feature_ids for {key}")
    return {"rows": rows, "taxa": taxa, "taxon_version": tv}


def check_profile(rows: list[tuple[str, str, int]], taxa: dict[str, Any], runs: list[RunInfo]) -> None:
    """The rows read are the rows the profile describes: same runs, totals = profile_run.n_reads, all taxa known."""
    total: Counter[str] = Counter()
    for run, _, c in rows:
        total[run] += c
    n_reads = {r.run_acc: r.n_reads for r in runs}
    if set(total) != set(n_reads):
        raise ValueError(f"runs differ: Delta-only {sorted(set(total) - set(n_reads))}, "
                         f"catalog-only {sorted(set(n_reads) - set(total))}")
    off = sorted(r for r in total if total[r] != n_reads[r])
    if off:
        raise ValueError(f"run totals differ from core.profile_run.n_reads: {off}")
    unknown = sorted({fid for _, fid, _ in rows} - taxa.keys())
    if unknown:
        raise ValueError(f"{len(unknown)} feature_ids without a feature_taxon row, e.g. {unknown[:3]}")


def depth_table(spec: MatchSpec, runs: list[RunInfo]) -> list[tuple[str, str, str, int]]:
    out: Counter[tuple[str, str, str]] = Counter()
    for r in runs:
        site = next((" / ".join(sel.values()) for sel in spec.sites.values()
                     if all(r.fields.get(k) == v for k, v in sel.items())), "(no site)")
        out[(site, r.diet, r.depth_status or "unflagged")] += 1
    return [(*k, n) for k, n in sorted(out.items())]


async def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.reports.finding_vs_profile", description=__doc__.splitlines()[0])
    ap.add_argument("map", type=Path, help="conf/match/<BioProject>.yml")
    ap.add_argument("--out", type=Path, help="directory for <BioProject>_finding_vs_profile.md and .tsv")
    ap.add_argument("--min-prevalence", type=int, default=MIN_PREVALENCE)
    ap.add_argument("--alpha", type=float, default=ALPHA)
    args = ap.parse_args(argv)
    spec = load_spec(args.map)

    from app.config import settings
    from app.database import AsyncSessionLocal
    from app.lake.pgmd_profile import Paths
    from app.lake.spark import build_spark

    async with AsyncSessionLocal() as db:
        cat = await load_catalog(db, spec)
        await db.rollback()
    spark = build_spark(f"finding_vs_profile-{spec.bioproject}")
    try:
        lake = read_profile(spark, cat["profile"], Paths.from_settings(settings).feature_taxon)
    finally:
        spark.stop()
    check_profile(lake["rows"], lake["taxa"], cat["runs"])
    rows, families = analyse(spec, cat["claims"], cat["runs"], lake["rows"], lake["taxa"],
                             args.min_prevalence, args.alpha)
    c0 = cat["claims"][0]
    header = {"bioproject": spec.bioproject, "pmcid": spec.pmcid, "signed": spec.signed,
              "reviewed_by": spec.review.reviewed_by, "reviewed_on": spec.review.reviewed_on,
              "profile_key": cat["profile"]["profile_key"], "delta_uri": cat["profile"]["delta_uri"],
              "delta_version": cat["profile"]["delta_version"], "taxon_version": lake["taxon_version"],
              "map_path": str(args.map), "map_sha256": hashlib.sha256(args.map.read_bytes()).hexdigest(),
              "min_prevalence": args.min_prevalence, "alpha": args.alpha,
              "treatment": c0.treatment, "reference": c0.reference}
    site_names = {t: " / ".join(sel.values()) for t, sel in spec.sites.items()}
    notes = {cid: m.note for cid, m in spec.claims.items() if m.note}
    md = markdown(rows, families, header, site_names, depth_table(spec, cat["runs"]), notes)
    print(md)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        stem = args.out / f"{spec.bioproject}_finding_vs_profile"
        stem.with_suffix(".md").write_text(md)
        with stem.with_suffix(".tsv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=TSV_COLUMNS, delimiter="\t")
            w.writeheader()
            w.writerows(tsv_rows(rows))
        print(f"\nwrote {stem}.md and {stem}.tsv", file=sys.stderr)
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
