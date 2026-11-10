"""Load reviewed findings from a paper (conf/findings/<BioProject>.yml) into core.finding (design doc §5.8).

    python -m app.ingest.findings conf/findings/PRJNA667518.yml --dry-run    # checks; review not needed
    python -m app.ingest.findings conf/findings/PRJNA667518.yml              # needs review.reviewed_by/on

The same guards as app.ingest.curation, plus the findings rules (check()):
  - every quote verbatim in its section of the full text (raw file from rag.document, sha256-checked),
    and so is a statistic's own quote when it comes from a figure legend or the Methods;
  - every number taken from a table equals its cell (table label + row + column header; jats.tables()):
    p, test statistic, d.f., group means and spreads. The spec states each value; the cell proves it;
  - contrast levels are levels of the experiment's treatment factor, in the shape of the contrast kind;
  - a microbiome finding has a gut site and is about exactly one feature or one declared feature set
    (counts only about a set); a host finding (body weight, ADG, ...) has neither, and has an age window;
  - 'differs' only on an omnibus contrast; group values only for levels of the finding's contrast;
  - no effect sizes from text.
A real load needs a signed review, and writes tier 'curated', method 'paper_results:<pmcid>'.
Re-runs add nothing; a stored finding or feature set that differs from the file stops the load
(a changed finding is a deliberate data change, like a changed design).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy import select, update

from app.ingest.jats import (Table, find_quote, find_rows, norm, parse_df, parse_mean_spread, parse_number,
                             parse_p, sections, tables)
from app.ingest.ncbi_study import code_version
from app.ingest.vocab import GUT_SITE_TERMS
from app.models import (Document, Experiment, Factor, FactorLevel, FeatureSet, Finding, FindingGroupValue,
                        FindingLevel, IngestRun, Study)

MATERIALS = {"digesta", "mucosa", None}
MICROBIOME_MEASURES = {"relative_abundance", "alpha_diversity", "beta_diversity", "predicted_gene_count"}
HOST_MEASURES = {"body_weight", "average_daily_gain", "average_daily_feed_intake", "feed_conversion_ratio",
                 "mortality"}
MEASURES = MICROBIOME_MEASURES | HOST_MEASURES
DIRECTIONS = {"increase", "decrease", "no_difference", "counts", "differs"}
STAT_KINDS, STAT_OPS = {"Q", "p"}, {"<", ">", "=", "<=", ">="}
SCOPES, SOURCES = {"treatment", "contrast", "unknown"}, {"text", "figure_legend", "methods", "table"}
TAXON_KINDS = {"genus", "otu", "phylum"}
FEATURE_FITS_MEASURE = {("relative_abundance", "genus"), ("relative_abundance", "otu"),
                        ("alpha_diversity", "alpha_index"), ("beta_diversity", "distance_metric"),
                        ("predicted_gene_count", "enzyme"), ("relative_abundance", "phylum")}
TESTS = {"GLM", "GLMM", "PERMANOVA", "LSD", "t-test", "Kruskal-Wallis"}   # neither of the last two prints a statistic
TEST_STAT_KINDS = {"F", "pseudo_F"}
STAT_FITS_TEST = {("GLM", "F"), ("GLMM", "F"), ("PERMANOVA", "pseudo_F")}
SPREAD_KINDS = {"se", "sd", "unstated"}
TEST_KEYS = {"name", "stat_kind", "value", "df", "stat_cell", "df_cell"}
N_RULE = re.compile(r"^df_den \+ (\d+)$")
DF_HEADER = re.compile(r"\bd\.?\s*f\.?(?![a-z])|degrees? of freedom", re.I)
CONTRAST_SHAPE = {"pairwise": {"kind", "treatment", "reference"}, "omnibus": {"kind", "levels"},
                  "grouping": {"kind", "higher", "lower"}}


# ---- checks (pure) -----------------------------------------------------------------------------

def _quote_errors(cid: str, what: str, section: str | None, quote: str | None, titles: set[str],
                  jats: bytes) -> list[str]:
    if not section or not quote:
        return [f"{cid}: {what} needs a section and a quote"]
    if norm(section) not in titles:
        return [f"{cid}: no section titled {section!r}"]
    if not find_quote(jats, quote, section):
        return [f"{cid}: {what} not found verbatim in {section!r}: {quote[:80]!r}"]
    return []


def contrast_levels(con: dict) -> list[tuple[str, str]]:
    """(level label, role) for a contrast; pairwise roles are 'treatment' / 'reference'."""
    kind = con["kind"]
    if kind == "pairwise":
        return [(con["treatment"], "treatment"), (con["reference"], "reference")]
    if kind == "omnibus":
        return [(lvl, "member") for lvl in con["levels"]]
    return [(lvl, "higher") for lvl in con["higher"]] + [(lvl, "lower") for lvl in con["lower"]]


def _contrast_errors(cid: str, con: dict, levels: set[str]) -> list[str]:
    kind = con.get("kind")
    if kind not in CONTRAST_SHAPE:
        return [f"{cid}: contrast kind {kind!r} is not one of {sorted(CONTRAST_SHAPE)}"]
    if set(con) != CONTRAST_SHAPE[kind]:
        return [f"{cid}: a {kind} contrast has exactly {sorted(CONTRAST_SHAPE[kind])}, got {sorted(con)}"]
    named = [lvl for lvl, _ in contrast_levels(con)]
    errors = [f"{cid}: contrast level {v!r} is not a diet level {sorted(levels)}" for v in named if v not in levels]
    if len(set(named)) != len(named):
        errors.append(f"{cid}: a level appears twice in the contrast")
    if kind == "omnibus" and len(named) < 2:
        errors.append(f"{cid}: an omnibus test names at least two levels")
    if kind == "grouping" and (not con["higher"] or not con["lower"]):
        errors.append(f"{cid}: a grouping needs both a higher and a lower group")
    return errors


def _table_row(cid: str, ref: dict, tabs: dict[str, Table]) -> tuple[dict | None, list[str]]:
    """The one body row a claim's `table: {label, row}` names, or errors."""
    label, match = ref.get("label"), ref.get("row")
    if label not in tabs:
        return None, [f"{cid}: no table labelled {label!r} (have {sorted(tabs)})"]
    if not isinstance(match, dict) or not match:
        return None, [f"{cid}: table.row is a mapping of column header → cell text"]
    t = tabs[label]
    unknown = [h for h in match if norm(h) not in t.header]
    if unknown:
        return None, [f"{cid}: {label} has no column {unknown[0]!r} (columns {list(t.header)})"]
    hits = find_rows(t, match)
    if len(hits) != 1:
        return None, [f"{cid}: {label} row {match} matches {len(hits)} rows, not 1"]
    return t.rows[hits[0]], []


def _cell(cid: str, row: dict | None, header: str | None, what: str) -> tuple[str | None, list[str]]:
    if row is None:
        return None, [f"{cid}: {what} names a cell, but the claim has no table row"]
    if not header or norm(header) not in row:
        return None, [f"{cid}: {what}: no column {header!r} in the table"]
    return row[norm(header)], []


def _num_errors(cid: str, what: str, cell: str | None, parse, want) -> list[str]:
    if cell is None:
        return []
    try:
        got = parse(cell)
    except ValueError as e:
        return [f"{cid}: {what}: {e}"]
    return [] if got == want else [f"{cid}: {what} is {want} in the spec but the cell prints {cell!r}"]


def review_errors(review: dict) -> list[str]:
    """A signature, once filled in, is a person and a calendar date. YAML reads an unquoted 2026-11-07 as a date,
    but '07 nov 2026' as text and '2026-11-07 10:00' as a timestamp; both would be stored as-is in every finding's
    evidence (it happened: ingest_run 51, 2026-11-07). The curation loader refuses them via pydantic; so does this."""
    errors = []
    on, by = review.get("reviewed_on"), review.get("reviewed_by")
    if on is not None and type(on) is not date:
        errors.append(f"review.reviewed_on {on!r} is not a date: write it as YYYY-MM-DD, unquoted")
    if by is not None and not (isinstance(by, str) and by.strip()):
        errors.append(f"review.reviewed_by {by!r} is not a name")
    return errors


def check(spec: dict, jats: bytes, design: dict) -> list[str]:
    """Every rule of the findings spec (design doc §5.8). Empty list = loadable."""
    errors: list[str] = []
    errors += review_errors(spec.get("review") or {})
    titles = {s.title for s in sections(jats)}
    tabs = tables(jats)
    levels = {lvl for f in design["factors"] if f["kind"] == "treatment" for lvl in f["levels"]}
    sites = {term for term, _ in GUT_SITE_TERMS.values()}
    sets = {fs["id"]: fs for fs in spec.get("feature_sets", [])}
    for name, ids in (("claim", [c["id"] for c in spec["claims"]]),
                      ("feature set", [fs["id"] for fs in spec.get("feature_sets", [])])):
        errors += [f"duplicate {name} id {i}" for i, n in Counter(ids).items() if n > 1]
    for fs in sets.values():
        if fs["site"] not in sites or fs.get("material") not in MATERIALS:
            errors.append(f"feature set {fs['id']}: unknown site or material")

    for c in spec["claims"]:
        cid = c["id"]
        # evidence: a verbatim quote, a table row, or both
        row = None
        if c.get("section") or c.get("quote") or not c.get("table"):
            errors += _quote_errors(cid, "quote", c.get("section"), c.get("quote"), titles, jats)
        if c.get("table") is not None:
            row, errs = _table_row(cid, c["table"], tabs)
            errors += errs
        errors += _contrast_errors(cid, c["contrast"], levels)
        kind = c["contrast"].get("kind")

        measure = c.get("measure")
        host = measure in HOST_MEASURES
        about = c.get("about") or {}
        is_set = "feature_set" in about
        if measure not in MEASURES:
            errors.append(f"{cid}: measure {measure!r} is not one of {sorted(MEASURES)}")
        if host:
            if c.get("site") is not None or c.get("material") is not None:
                errors.append(f"{cid}: a host measure ({measure}) has no gut site or material")
            if about:
                errors.append(f"{cid}: a host measure is about the animals: no 'about'")
            if c.get("age") is None:
                errors.append(f"{cid}: a host measure needs its age window (age: {{from, to}}, days)")
        else:
            if len(about) != 1 or next(iter(about)) not in ("feature", "feature_set"):
                errors.append(f"{cid}: 'about' names exactly one feature or one feature_set")
            elif is_set and about["feature_set"] not in sets:
                errors.append(f"{cid}: unknown feature set {about['feature_set']!r}")
            elif "feature" in about and measure in MICROBIOME_MEASURES and \
                    (measure, about["feature"].get("kind")) not in FEATURE_FITS_MEASURE:
                errors.append(f"{cid}: a {about['feature'].get('kind')!r} feature doesn't fit measure {measure}")
            if c.get("site") not in sites:
                errors.append(f"{cid}: site {c.get('site')!r} is not a known gut-site term")
            if c.get("material") not in MATERIALS:
                errors.append(f"{cid}: material {c.get('material')!r} is not one of digesta / mucosa / null")

        age = c.get("age")
        if age is not None:
            a, b = (age.get("from"), age.get("to")) if isinstance(age, dict) else (None, None)
            if not isinstance(age, dict) or set(age) != {"from", "to"} or \
                    not all(isinstance(x, int) and not isinstance(x, bool) for x in (a, b)) or not 0 <= a <= b:
                errors.append(f"{cid}: age is {{from, to}} in whole days, 0 <= from <= to")

        if c["direction"] not in DIRECTIONS:
            errors.append(f"{cid}: direction {c['direction']!r} is not one of {sorted(DIRECTIONS)}")
        if c["direction"] == "differs" and kind != "omnibus":
            errors.append(f"{cid}: 'differs' is only for an omnibus test (a pair or grouping has a direction)")
        if (c["direction"] == "counts") != ("counts" in c):
            errors.append(f"{cid}: 'counts' goes with direction 'counts', and only then")
        if "counts" in c and not is_set:
            errors.append(f"{cid}: counts are a claim about a feature set, not a feature")

        st = c.get("stat")
        if st is not None:
            if st.get("kind") not in STAT_KINDS or st.get("op") not in STAT_OPS:
                errors.append(f"{cid}: stat kind/op must be one of {sorted(STAT_KINDS)} / {sorted(STAT_OPS)}")
            if st.get("scope") not in SCOPES or st.get("source") not in SOURCES:
                errors.append(f"{cid}: stat scope/source must be one of {sorted(SCOPES)} / {sorted(SOURCES)}")
            if st.get("source") in ("figure_legend", "methods"):
                errors += _quote_errors(cid, f"stat quote ({st['source']})", st.get("section"), st.get("quote"),
                                        titles, jats)
            if st.get("source") == "table":
                cell, errs = _cell(cid, row, st.get("cell"), "stat.cell")
                errors += errs + _num_errors(cid, "stat", cell, parse_p,
                                             (st.get("op"), Decimal(str(st.get("value")))))
            elif "cell" in st:
                errors.append(f"{cid}: stat.cell goes with source 'table'")

        t = c.get("test")
        if t is not None:
            if not set(t) <= TEST_KEYS or t.get("name") not in TESTS:
                errors.append(f"{cid}: test is {{name {sorted(TESTS)}, stat_kind?, value?, df?, stat_cell?, df_cell?}}")
            if ("stat_kind" in t) != ("value" in t):
                errors.append(f"{cid}: test stat_kind and value go together")
            if "stat_kind" in t and "value" in t:
                if t["stat_kind"] not in TEST_STAT_KINDS or (t.get("name"), t["stat_kind"]) not in STAT_FITS_TEST:
                    errors.append(f"{cid}: a {t['stat_kind']!r} statistic doesn't come from a {t.get('name')} test")
                if "stat_cell" in t:
                    cell, errs = _cell(cid, row, t["stat_cell"], "test.stat_cell")
                    errors += errs + _num_errors(cid, "test statistic", cell, parse_number, Decimal(str(t["value"])))
                elif str(t["value"]) not in norm(c.get("quote") or ""):
                    errors.append(f"{cid}: test value {t['value']} is in neither a table cell nor the quote")
            if "df" in t:
                df = t["df"]
                if "stat_kind" not in t:
                    errors.append(f"{cid}: d.f. belong to a test statistic")
                if not (isinstance(df, list) and len(df) == 2 and all(isinstance(x, int) and x > 0 for x in df)):
                    errors.append(f"{cid}: test.df is [numerator, denominator], positive integers")
                elif "df_cell" in t:
                    if not DF_HEADER.search(t["df_cell"]):
                        # with the comma lost, an F like '1.56' reads as d.f. (1, 56): name the d.f. column itself
                        errors.append(f"{cid}: test.df_cell {t['df_cell']!r} is not a d.f. column")
                    cell, errs = _cell(cid, row, t["df_cell"], "test.df_cell")
                    errors += errs + _num_errors(cid, "d.f.", cell, parse_df, tuple(df))

        n = c.get("n_analysed")
        if n is not None:
            m = N_RULE.match(str(n.get("rule", "")))
            df = (t or {}).get("df")
            if not m or not isinstance(df, list):
                errors.append(f"{cid}: n_analysed.rule is 'df_den + <k>' and needs test.df")
            elif df[1] + int(m.group(1)) != n.get("value"):
                errors.append(f"{cid}: n_analysed {n.get('value')} != df_den {df[1]} + {m.group(1)}")

        gv = c.get("group_values")
        if gv is not None:
            named = {lvl for lvl, _ in contrast_levels(c["contrast"])} if kind in CONTRAST_SHAPE else set()
            if kind == "pairwise":
                errors.append(f"{cid}: group values attach to an omnibus or grouping contrast (finding_level)")
            if gv.get("spread_kind") not in SPREAD_KINDS:
                errors.append(f"{cid}: group_values.spread_kind is one of {sorted(SPREAD_KINDS)}")
            for lvl, v in (gv.get("levels") or {}).items():
                if lvl not in named:
                    errors.append(f"{cid}: group value for {lvl!r}, which is not a level of this contrast")
                    continue
                cell, errs = _cell(cid, row, v.get("cell"), f"group value {lvl!r}")
                spread = Decimal(str(v["spread"])) if v.get("spread") is not None else None
                errors += errs + _num_errors(cid, f"group value {lvl!r}", cell, parse_mean_spread,
                                             (Decimal(str(v.get("mean"))), spread))

        if c.get("effect") is not None:
            errors.append(f"{cid}: effect given, but the text has no effect sizes (figures only)")
    return errors


# ---- rows (pure) -------------------------------------------------------------------------------

def feature_set_rows(spec: dict, study_id: int) -> list[dict]:
    return [{"study_id": study_id, "local_id": fs["id"], "member_kind": fs["member_kind"],
             "definition": fs["definition"], "size": fs.get("size"), "site_term": fs["site"],
             "material": fs.get("material"), "otu_dataset": fs.get("otu_dataset")}
            for fs in spec.get("feature_sets", [])]


def finding_rows(spec: dict, *, study_id: int, experiment_id: int, doc_id: int, factor_id: int,
                 level_ids: dict[str, int], feature_set_ids: dict[str, int],
                 fulltext_sha256: str | None) -> list[tuple[dict, list[dict]]]:
    """(core.finding row, its core.finding_level rows) per claim. No database."""
    out = []
    for c in spec["claims"]:
        con = c["contrast"]
        pairwise = con["kind"] == "pairwise"
        about = c.get("about") or {}
        feat = about.get("feature")
        t = c.get("test") or {}
        age = c.get("age") or {}
        st = c.get("stat") or {}
        counts = c.get("counts") or {}
        row = {
            "study_id": study_id, "experiment_id": experiment_id, "doc_id": doc_id, "local_id": c["id"],
            "contrast_kind": con["kind"], "factor_id": factor_id,
            "treatment_level_id": level_ids[con["treatment"]] if pairwise else None,
            "reference_level_id": level_ids[con["reference"]] if pairwise else None,
            "site_term": c.get("site"), "material": c.get("material"), "measure": c["measure"],
            "age_from_d": age.get("from"), "age_to_d": age.get("to"),
            "feature_kind": feat["kind"] if feat else None,
            "feature_label": feat["label"] if feat else None,
            "feature_rank": feat.get("rank") if feat else None,
            "otu_dataset": feat.get("otu_dataset") if feat else None,
            "taxonomy": spec.get("taxonomy") if feat and feat["kind"] in TAXON_KINDS else None,
            "feature_set_id": feature_set_ids[about["feature_set"]] if "feature_set" in about else None,
            "direction": c["direction"],
            "n_increase": counts.get("increase"), "n_decrease": counts.get("decrease"),
            "stat_kind": st.get("kind"), "stat_op": st.get("op"),
            "stat_value": Decimal(str(st["value"])) if st.get("value") is not None else None,   # exact decimal, not a binary float
            "stat_scope": st.get("scope"), "stat_source": st.get("source"),
            "test": t.get("name"), "test_stat_kind": t.get("stat_kind"),
            "test_stat_value": Decimal(str(t["value"])) if t.get("value") is not None else None,
            "df_num": t["df"][0] if t.get("df") else None, "df_den": t["df"][1] if t.get("df") else None,
            "effect_kind": None, "effect_value": None, "effect_se": None,
            "figure": c.get("figure"), "tier": "curated", "method": f"paper_results:{spec['pmcid']}",
            "evidence": {"claim_id": c["id"], "pmcid": spec["pmcid"], "doc_id": doc_id,
                         "section": c.get("section"), "quote": c.get("quote"),
                         "stat_section": st.get("section"), "stat_quote": st.get("quote"),
                         "note": c.get("note"), "flags": c.get("flags") or [],
                         "reviewed_by": spec["review"]["reviewed_by"],
                         "reviewed_on": str(spec["review"]["reviewed_on"]) if spec["review"]["reviewed_on"] else None,
                         "fulltext_sha256": fulltext_sha256},
        }
        # keys only when present, so findings stored before these existed still compare equal
        for key in ("table", "n_analysed"):
            if c.get(key) is not None:
                row["evidence"][key] = c[key]
        levels = [] if pairwise else [{"level_id": level_ids[lbl], "factor_id": factor_id, "role": role}
                                      for lbl, role in contrast_levels(con)]
        out.append((row, levels))
    return out


def group_value_rows(spec: dict, level_ids: dict[str, int]) -> dict[str, list[dict]]:
    """core.finding_group_value rows per claim id (claims without group values are absent). No database."""
    out: dict[str, list[dict]] = {}
    for c in spec["claims"]:
        gv = c.get("group_values")
        if not gv:
            continue
        rows = []
        for lvl, v in gv["levels"].items():
            spread = Decimal(str(v["spread"])) if v.get("spread") is not None else None
            rows.append({"level_id": level_ids[lvl], "value_kind": "mean", "value": Decimal(str(v["mean"])),
                         "spread": spread, "spread_kind": gv["spread_kind"] if spread is not None else None,
                         "unit": gv.get("unit"),
                         "evidence": {"claim_id": c["id"], "table": c["table"]["label"], "row": c["table"]["row"],
                                      "cell": v["cell"], "flags": gv.get("flags") or []}})
        out[c["id"]] = rows
    return out


def diff(stored, row: dict) -> dict:
    """Columns whose stored value differs from the file's (ids and run bookkeeping ignored)."""
    return {k: (getattr(stored, k), v) for k, v in row.items() if getattr(stored, k) != v}


# ---- load --------------------------------------------------------------------------------------

async def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.ingest.findings", description=__doc__.splitlines()[0])
    ap.add_argument("spec", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    raw_spec = args.spec.read_bytes()
    spec = yaml.safe_load(raw_spec)
    review = spec.get("review") or {}
    if not (review.get("reviewed_by") and review.get("reviewed_on")) and not args.dry_run:
        print("ERROR review.reviewed_by / reviewed_on are empty: a curated load needs a signed review "
              "(--dry-run checks everything without it)", file=sys.stderr)
        return 1
    design_path = args.spec.resolve().parent.parent / "designs" / f"{spec['bioproject']}.yml"
    design = yaml.safe_load(design_path.read_text())

    from app.database import AsyncSessionLocal
    counts: Counter = Counter()
    async with AsyncSessionLocal() as db:
        study_id = (await db.execute(select(Study.study_id).where(Study.bioproject_acc == spec["bioproject"]))
                    ).scalar_one_or_none()
        doc = (await db.execute(select(Document).where(Document.pmcid == spec["pmcid"]))).scalar_one_or_none()
        if study_id is None or doc is None:
            print(f"ERROR load the study ({spec['bioproject']}) and the paper ({spec['pmcid']}) first", file=sys.stderr)
            return 1
        experiment_id = (await db.execute(select(Experiment.experiment_id).where(
            Experiment.study_id == study_id, Experiment.local_id == spec["experiment"]))).scalar_one_or_none()
        factors = (await db.execute(select(Factor.factor_id).where(
            Factor.experiment_id == experiment_id, Factor.kind == "treatment"))).scalars().all()
        if experiment_id is None or len(factors) != 1:
            print(f"ERROR experiment {spec['experiment']!r} needs exactly one treatment factor "
                  f"(found {len(factors)}); load the design first", file=sys.stderr)
            return 1
        factor_id = factors[0]
        level_ids = dict((await db.execute(select(FactorLevel.label, FactorLevel.level_id).where(
            FactorLevel.factor_id == factor_id, FactorLevel.parent_level_id.is_(None)))).all())

        xml_path = Path(doc.raw_uri) / "fulltext.xml"
        jats = xml_path.read_bytes()
        sha = hashlib.sha256(jats).hexdigest()
        if sha != doc.raw_sha256.get("fulltext.xml"):
            print(f"ERROR {xml_path} no longer matches the sha256 recorded on rag.document", file=sys.stderr)
            return 1
        errors = check(spec, jats, design)
        missing = sorted({lbl for c in spec["claims"] for lbl, _ in contrast_levels(c["contrast"])} - set(level_ids))
        errors += [f"treatment level {m!r} is in the spec but not in the loaded design" for m in missing]
        for e in errors:
            print(f"ERROR {e}", file=sys.stderr)
        if errors:
            return 1
        print(f"{len(spec['claims'])} claims: every quote found verbatim, every rule passed", file=sys.stderr)

        params = {"spec": str(args.spec), "spec_sha256": hashlib.sha256(raw_spec).hexdigest(),
                  "reviewed_by": review.get("reviewed_by"), "reviewed_on": str(review.get("reviewed_on")),
                  "pmcid": spec["pmcid"], "doc_id": doc.doc_id, "dry_run": args.dry_run, **code_version()}
        ir = IngestRun(source="manual", source_release=f"findings:{args.spec.name}", params=params,
                       raw_uri=str(args.spec))
        db.add(ir)
        await db.flush()

        async def stop(msg: str) -> int:
            await db.rollback()
            print(f"ERROR {msg}", file=sys.stderr)
            return 1

        feature_set_ids: dict[str, int] = {}
        for row in feature_set_rows(spec, study_id):
            stored = (await db.execute(select(FeatureSet).where(
                FeatureSet.study_id == study_id, FeatureSet.local_id == row["local_id"]))).scalar_one_or_none()
            if stored:
                if d := diff(stored, row):
                    return await stop(f"feature set {row['local_id']} is stored differently: {d}")
                counts["feature_set.existing"] += 1
            else:
                stored = FeatureSet(**row, ingest_run_id=ir.ingest_run_id)
                db.add(stored)
                await db.flush()
                counts["feature_set.new"] += 1
            feature_set_ids[row["local_id"]] = stored.feature_set_id

        group_values = group_value_rows(spec, level_ids)
        for row, levels in finding_rows(spec, study_id=study_id, experiment_id=experiment_id, doc_id=doc.doc_id,
                                        factor_id=factor_id, level_ids=level_ids, feature_set_ids=feature_set_ids,
                                        fulltext_sha256=sha):
            stored = (await db.execute(select(Finding).where(
                Finding.study_id == study_id, Finding.local_id == row["local_id"]))).scalar_one_or_none()
            if stored:
                if d := diff(stored, row):
                    return await stop(f"finding {row['local_id']} is stored differently: {d}")
                have = set((await db.execute(select(FindingLevel.level_id, FindingLevel.role).where(
                    FindingLevel.finding_id == stored.finding_id))).all())
                if have != {(lv["level_id"], lv["role"]) for lv in levels}:
                    return await stop(f"finding {row['local_id']} has other levels stored")
                stored_gv = (await db.execute(select(FindingGroupValue).where(
                    FindingGroupValue.finding_id == stored.finding_id))).scalars().all()
                key = lambda g: (g["level_id"], g["value_kind"], g["value"], g["spread"], g["spread_kind"],  # noqa: E731
                                 g["unit"], json.dumps(g["evidence"], sort_keys=True))
                have_gv = {key({k: getattr(g, k) for k in ("level_id", "value_kind", "value", "spread",
                                                           "spread_kind", "unit", "evidence")}) for g in stored_gv}
                if have_gv != {key(g) for g in group_values.get(row["local_id"], [])}:
                    return await stop(f"finding {row['local_id']} has other group values stored")
                counts["finding.existing"] += 1
                continue
            f = Finding(**row, ingest_run_id=ir.ingest_run_id)
            db.add(f)
            await db.flush()
            for lv in levels:
                db.add(FindingLevel(finding_id=f.finding_id, ingest_run_id=ir.ingest_run_id, **lv))
            await db.flush()          # finding_level rows first: group values reference them
            for gv in group_values.get(row["local_id"], []):
                db.add(FindingGroupValue(finding_id=f.finding_id, ingest_run_id=ir.ingest_run_id, **gv))
                counts["finding_group_value.new"] += 1
            counts["finding.new"] += 1
            counts[f"measure.{row['measure']}"] += 1
            counts["finding_level.new"] += len(levels)
            counts[f"contrast.{row['contrast_kind']}"] += 1
        await db.flush()

        await db.execute(update(IngestRun).where(IngestRun.ingest_run_id == ir.ingest_run_id).values(
            finished_at=datetime.now(timezone.utc),
            params={**params, "counts": dict(counts)}))
        irid = ir.ingest_run_id
        await (db.rollback() if args.dry_run else db.commit())
    verb = "dry run, rolled back" if args.dry_run else "committed"
    print(f"ingest_run {irid} ({verb}): " + json.dumps(dict(sorted(counts.items()))), file=sys.stderr)
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
