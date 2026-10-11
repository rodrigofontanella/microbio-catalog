"""Check a DRAFT findings file against the paper's full text, the study's design spec and the agreed rules.

    python scripts/check_findings_quotes.py conf/findings/PRJNA667518_draft.yml \\
        /home/fontanella/lab/raw/europepmc/20261011T090319Z/PMC8024495

Checks (rules in the draft's header, design doc §5.8):
  - unique claim and feature-set ids; every claim is about exactly one feature or one known feature set
  - every quote verbatim in its section (app.ingest.jats folding), and so is a statistic's own quote when
    its source is a figure legend or the Methods
  - contrast levels are diet levels of conf/designs/<bioproject>.yml, in the shape of the contrast kind
  - sites are known gut-site terms; material, measure, direction, stat scope/source from fixed lists
  - counts only with direction 'counts' and only about a feature set; no effect sizes (figures only)
Prints a summary and every open flag, so the review starts from the open questions.
Tutorial shape: a script over a draft. Once the findings table exists, this becomes its loader's check.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.ingest.jats import find_quote, norm, sections  # noqa: E402
from app.ingest.vocab import GUT_SITE_TERMS  # noqa: E402

MATERIALS = {"digesta", "mucosa", None}
MEASURES = {"relative_abundance", "alpha_diversity", "predicted_gene_count"}
DIRECTIONS = {"increase", "decrease", "no_difference", "counts"}
STAT_KINDS, STAT_OPS = {"Q", "p"}, {"<", ">", "=", "<=", ">="}
SCOPES, SOURCES = {"treatment", "contrast", "unknown"}, {"text", "figure_legend", "methods"}


def _quote_errors(cid: str, what: str, section: str | None, quote: str | None, titles: set[str],
                  jats: bytes) -> list[str]:
    if not section or not quote:
        return [f"{cid}: {what} needs a section and a quote"]
    if norm(section) not in titles:
        return [f"{cid}: no section titled {section!r}"]
    if not find_quote(jats, quote, section):
        return [f"{cid}: {what} not found verbatim in {section!r}: {quote[:80]!r}"]
    return []


def _contrast_errors(cid: str, con: dict, levels: set[str]) -> list[str]:
    kind = con.get("kind")
    shape = {"pairwise": {"kind", "treatment", "reference"}, "omnibus": {"kind", "levels"},
             "grouping": {"kind", "higher", "lower"}}
    if kind not in shape:
        return [f"{cid}: contrast kind {kind!r} is not one of {sorted(shape)}"]
    if set(con) != shape[kind]:
        return [f"{cid}: a {kind} contrast has exactly {sorted(shape[kind])}, got {sorted(con)}"]
    named = ([con["treatment"], con["reference"]] if kind == "pairwise" else
             list(con["levels"]) if kind == "omnibus" else list(con["higher"]) + list(con["lower"]))
    errors = [f"{cid}: contrast level {v!r} is not a diet level {sorted(levels)}" for v in named if v not in levels]
    if len(set(named)) != len(named):
        errors.append(f"{cid}: a level appears twice in the contrast")
    return errors


def check(spec: dict, jats: bytes, design: dict) -> list[str]:
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


def main(argv: list[str]) -> int:
    spec_path, raw_dir = Path(argv[0]), Path(argv[1]).expanduser()
    spec = yaml.safe_load(spec_path.read_text())
    jats = (raw_dir / "fulltext.xml").read_bytes()
    design = yaml.safe_load((spec_path.parent.parent / "designs" / f"{spec['bioproject']}.yml").read_text())
    errors = check(spec, jats, design)
    for e in errors:
        print(f"ERROR {e}", file=sys.stderr)
    claims = spec["claims"]
    count = lambda key: ", ".join(f"{k} {v}" for k, v in sorted(Counter(key(c) for c in claims).items()))  # noqa: E731
    print(f"{len(claims)} claims, {len(spec.get('feature_sets', []))} feature sets, "
          f"{len({c['quote'] for c in claims})} distinct quotes, {sum(bool(c.get('flags')) for c in claims)} flagged",
          file=sys.stderr)
    print("about:     " + count(lambda c: next(iter(c["about"]))), file=sys.stderr)
    print("contrast:  " + count(lambda c: c["contrast"]["kind"]), file=sys.stderr)
    print("direction: " + count(lambda c: c["direction"]), file=sys.stderr)
    print("measure:   " + count(lambda c: c["measure"]), file=sys.stderr)
    print("stat:      " + count(lambda c: f"{c['stat']['scope']}/{c['stat']['source']}"), file=sys.stderr)
    for c in claims:
        for fl in c.get("flags") or []:
            print(f"  FLAG {c['id']}: {fl.split(':')[0]}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1:]))
