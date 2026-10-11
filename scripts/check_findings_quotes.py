"""Check a DRAFT findings file against the paper's full text and the study's design spec.

    python scripts/check_findings_quotes.py conf/findings/PRJNA667518_draft.yml \\
        ~/lab/raw/europepmc/20261011T090319Z/PMC8024495

Checks: unique claim ids; every quote verbatim in its named section (app.ingest.jats folding);
contrast levels exist in conf/designs/<bioproject>.yml; sites are known UBERON gut terms.
Prints a per-kind summary and every flag, so the review starts from the open questions.
Tutorial shape: a script over a draft. Once the findings table exists, this becomes its loader's check.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.ingest.jats import find_quote, norm, sections
from app.ingest.vocab import GUT_SITE_TERMS


def check(spec: dict, jats: bytes, design: dict) -> list[str]:
    errors: list[str] = []
    titles = {s.title for s in sections(jats)}
    levels = {lvl for f in design["factors"] if f["kind"] == "treatment" for lvl in f["levels"]}
    sites = {term for term, _ in GUT_SITE_TERMS.values()}
    ids = Counter(c["id"] for c in spec["claims"])
    errors += [f"duplicate claim id {i}" for i, n in ids.items() if n > 1]
    for c in spec["claims"]:
        cid = c["id"]
        if norm(c["section"]) not in titles:
            errors.append(f"{cid}: no section titled {c['section']!r}")
        elif not find_quote(jats, c["quote"], c["section"]):
            errors.append(f"{cid}: quote not found verbatim in {c['section']!r}: {c['quote'][:80]!r}")
        con = c["contrast"]
        for role in ("treatment", "reference"):
            v = con[role]
            if v not in levels and not (role == "treatment" and v == "all") and not (role == "reference" and v is None):
                errors.append(f"{cid}: contrast {role} {v!r} is not a diet level {sorted(levels)}")
        if c["site"] not in sites:
            errors.append(f"{cid}: site {c['site']!r} is not a known gut-site term")
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
    print(f"{len(claims)} claims, {len({c['quote'] for c in claims})} distinct quotes, "
          f"{sum(bool(c.get('flags')) for c in claims)} flagged", file=sys.stderr)
    print("by measure: " + ", ".join(f"{k} {v}" for k, v in sorted(Counter(c["measure"] for c in claims).items())),
          file=sys.stderr)
    for c in claims:
        for fl in c.get("flags") or []:
            print(f"  FLAG {c['id']}: {fl.split(':')[0]}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1:]))
