"""Check a findings spec against the paper's full text, the study's design spec and the agreed rules.

    python scripts/check_findings_quotes.py conf/findings/PRJNA667518.yml \\
        /home/fontanella/lab/raw/europepmc/20261011T090319Z/PMC8024495

No database: reads the raw folder directly. The rules live in app.ingest.findings.check(), which the loader
(python -m app.ingest.findings) runs too, so the two can't drift apart. Prints a summary and every open flag.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.ingest.findings import check  # noqa: E402,F401  (re-exported for tests)


def main(argv: list[str]) -> int:
    spec_path, raw_dir = Path(argv[0]), Path(argv[1]).expanduser()
    spec = yaml.safe_load(spec_path.read_text())
    jats = (raw_dir / "fulltext.xml").read_bytes()
    design = yaml.safe_load((spec_path.resolve().parent.parent / "designs" / f"{spec['bioproject']}.yml").read_text())
    errors = check(spec, jats, design)
    for e in errors:
        print(f"ERROR {e}", file=sys.stderr)
    claims = spec["claims"]

    def count(key) -> str:
        return ", ".join(f"{k} {v}" for k, v in sorted(Counter(key(c) for c in claims).items()))

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
