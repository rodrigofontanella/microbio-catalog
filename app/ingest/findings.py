"""Load reviewed findings from a paper (conf/findings/<BioProject>.yml) into core.finding (design doc §5.8).

    python -m app.ingest.findings conf/findings/PRJNA667518.yml --dry-run    # checks; review not needed
    python -m app.ingest.findings conf/findings/PRJNA667518.yml              # needs review.reviewed_by/on

The same guards as app.ingest.curation, plus the findings rules (check()):
  - every quote verbatim in its section of the full text (raw file from rag.document, sha256-checked),
    and so is a statistic's own quote when it comes from a figure legend or the Methods;
  - contrast levels are levels of the experiment's treatment factor, in the shape of the contrast kind;
  - a finding is about exactly one feature or one declared feature set; counts only about a set;
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
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy import select, update

from app.ingest.jats import find_quote, norm, sections
from app.ingest.ncbi_study import code_version
from app.ingest.vocab import GUT_SITE_TERMS
from app.models import (Document, Experiment, Factor, FactorLevel, FeatureSet, Finding, FindingLevel, IngestRun,
                        Study)

MATERIALS = {"digesta", "mucosa", None}
MEASURES = {"relative_abundance", "alpha_diversity", "predicted_gene_count"}
DIRECTIONS = {"increase", "decrease", "no_difference", "counts"}
STAT_KINDS, STAT_OPS = {"Q", "p"}, {"<", ">", "=", "<=", ">="}
SCOPES, SOURCES = {"treatment", "contrast", "unknown"}, {"text", "figure_legend", "methods"}
TAXON_KINDS = {"genus", "otu"}
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


def check(spec: dict, jats: bytes, design: dict) -> list[str]:
    """Every rule of the findings spec (design doc §5.8). Empty list = loadable."""
    errors: list[str] = []
    titles = {s.title for s in sections(jats)}
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
        errors += _quote_errors(cid, "quote", c.get("section"), c.get("quote"), titles, jats)
        errors += _contrast_errors(cid, c["contrast"], levels)

        about = c.get("about") or {}
        if len(about) != 1 or next(iter(about)) not in ("feature", "feature_set"):
            errors.append(f"{cid}: 'about' names exactly one feature or one feature_set")
        elif "feature_set" in about and about["feature_set"] not in sets:
            errors.append(f"{cid}: unknown feature set {about['feature_set']!r}")
        is_set = "feature_set" in about

        if c["site"] not in sites:
            errors.append(f"{cid}: site {c['site']!r} is not a known gut-site term")
        if c.get("material") not in MATERIALS:
            errors.append(f"{cid}: material {c.get('material')!r} is not one of digesta / mucosa / null")
        if c["measure"] not in MEASURES:
            errors.append(f"{cid}: measure {c['measure']!r} is not one of {sorted(MEASURES)}")
        if c["direction"] not in DIRECTIONS:
            errors.append(f"{cid}: direction {c['direction']!r} is not one of {sorted(DIRECTIONS)}")
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
        feat = c["about"].get("feature")
        st = c.get("stat") or {}
        counts = c.get("counts") or {}
        row = {
            "study_id": study_id, "experiment_id": experiment_id, "doc_id": doc_id, "local_id": c["id"],
            "contrast_kind": con["kind"], "factor_id": factor_id,
            "treatment_level_id": level_ids[con["treatment"]] if pairwise else None,
            "reference_level_id": level_ids[con["reference"]] if pairwise else None,
            "site_term": c["site"], "material": c.get("material"), "measure": c["measure"],
            "feature_kind": feat["kind"] if feat else None,
            "feature_label": feat["label"] if feat else None,
            "feature_rank": feat.get("rank") if feat else None,
            "otu_dataset": feat.get("otu_dataset") if feat else None,
            "taxonomy": spec.get("taxonomy") if feat and feat["kind"] in TAXON_KINDS else None,
            "feature_set_id": None if feat else feature_set_ids[c["about"]["feature_set"]],
            "direction": c["direction"],
            "n_increase": counts.get("increase"), "n_decrease": counts.get("decrease"),
            "stat_kind": st.get("kind"), "stat_op": st.get("op"),
            "stat_value": Decimal(str(st["value"])) if st.get("value") is not None else None,   # exact decimal, not a binary float
            "stat_scope": st.get("scope"), "stat_source": st.get("source"),
            "effect_kind": None, "effect_value": None, "effect_se": None,
            "figure": c.get("figure"), "tier": "curated", "method": f"paper_results:{spec['pmcid']}",
            "evidence": {"claim_id": c["id"], "pmcid": spec["pmcid"], "doc_id": doc_id,
                         "section": c["section"], "quote": c["quote"],
                         "stat_section": st.get("section"), "stat_quote": st.get("quote"),
                         "note": c.get("note"), "flags": c.get("flags") or [],
                         "reviewed_by": spec["review"]["reviewed_by"],
                         "reviewed_on": str(spec["review"]["reviewed_on"]) if spec["review"]["reviewed_on"] else None,
                         "fulltext_sha256": fulltext_sha256},
        }
        levels = [] if pairwise else [{"level_id": level_ids[lbl], "factor_id": factor_id, "role": role}
                                      for lbl, role in contrast_levels(con)]
        out.append((row, levels))
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
        errors += [f"diet level {m!r} is in the spec but not in the loaded design" for m in missing]
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
                counts["finding.existing"] += 1
                continue
            f = Finding(**row, ingest_run_id=ir.ingest_run_id)
            db.add(f)
            await db.flush()
            for lv in levels:
                db.add(FindingLevel(finding_id=f.finding_id, ingest_run_id=ir.ingest_run_id, **lv))
            counts["finding.new"] += 1
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
