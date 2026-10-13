"""Inspect one BioProject's rows in PGMD's Microbial.csv before designing the Delta tables (Step 6).

Stdlib only, so it runs on Skipper where the file lives:

    ssh skipper python3 - /srv/bulk/raw/pgmd/v1/Microbial.csv PRJNA667518 < scripts/pgmd_profile_inspect.py

Reports: columns; rows and runs per level; lineage shape per level (segments, rank prefixes, empty markers);
duplicates; whether microbial_name is the last resolved rank; per-run totals; and the genus check:
do Species counts, summed by their genus lineage, equal PGMD's own Genus rows, run by run?
Read-only; prints to stdout.
"""
from __future__ import annotations

import csv
import statistics
import sys
from collections import Counter, defaultdict

EMPTY = {"", "__", "k__", "p__", "c__", "o__", "f__", "g__", "s__", "unclassified", "Unknown"}


def segments(lineage: str) -> list[str]:
    return [s.strip() for s in lineage.split(";")]


def resolved(segs: list[str]) -> int:
    """Number of leading segments that carry a name (an empty marker ends the resolution)."""
    n = 0
    for s in segs:
        if s in EMPTY:
            break
        n += 1
    return n


def main(path: str, bioproject: str) -> None:
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        print("columns:", reader.fieldnames)
        for r in reader:
            if r["BioProject"].strip() == bioproject:
                rows.append(r)
    print(f"{bioproject}: {len(rows)} rows")
    if not rows:
        return

    by_level: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_level[r["level"]].append(r)
    print("\n# rows / runs / segment counts / resolved-rank depth, per level")
    for level, rs in sorted(by_level.items(), key=lambda kv: len(kv[1])):
        segs = [segments(r["specific_taxonomy"]) for r in rs]
        print(f"{level:8} rows {len(rs):6}  runs {len({r['Runs'] for r in rs}):4}  "
              f"segments {dict(sorted(Counter(len(s) for s in segs).items()))}  "
              f"resolved {dict(sorted(Counter(resolved(s) for s in segs).items()))}")

    for level in ("Genus", "Species"):
        rs = by_level.get(level, [])
        print(f"\n# {level}: 4 example rows (microbial_name | specific_taxonomy | count | abundance)")
        for r in rs[:2] + rs[-2:]:
            print(f"  {r['microbial_name']!r} | {r['specific_taxonomy']!r} | {r['count']!r} | {r['abundance']!r}")
        last = Counter(segments(r["specific_taxonomy"])[-1] for r in rs)
        print(f"  most common last segments: {last.most_common(5)}")
        prefixed = sum(1 for r in rs if any(s[:3] in ("k__", "p__", "g__", "s__") for s in segments(r["specific_taxonomy"])))
        print(f"  rows with rank prefixes (k__ p__ g__ s__): {prefixed}")
        name_is_last = sum(1 for r in rs if (lambda s: resolved(s) and s[resolved(s) - 1] == r["microbial_name"].strip())(
            segments(r["specific_taxonomy"])))
        print(f"  microbial_name == last resolved segment: {name_is_last} of {len(rs)}")
        dupes = Counter((r["Runs"], r["specific_taxonomy"]) for r in rs)
        print(f"  duplicate (run, lineage) pairs: {sum(1 for n in dupes.values() if n > 1)}")
        nonint = sum(1 for r in rs if r["count"].strip() and not r["count"].strip().isdigit())
        print(f"  non-integer counts: {nonint}")

    sp = by_level.get("Species", [])
    if sp:
        totals = defaultdict(int)
        pct = defaultdict(float)
        for r in sp:
            totals[r["Runs"]] += int(float(r["count"] or 0))
            pct[r["Runs"]] += float(r["abundance"] or 0)
        t = sorted(totals.values())
        print(f"\n# Species: per-run read totals over {len(t)} runs: min {t[0]}  median {statistics.median(t):.0f}  "
              f"max {t[-1]}")
        p = sorted(pct.values())
        print(f"  per-run abundance sums (%): min {p[0]:.3f}  max {p[-1]:.3f}")
        nfeat = sorted(Counter(r["Runs"] for r in sp).values())
        print(f"  features per run: min {nfeat[0]}  median {statistics.median(nfeat):.0f}  max {nfeat[-1]}")

    ge = by_level.get("Genus", [])
    if sp and ge:
        print("\n# genus check: Species counts summed by their first 6 segments vs Genus rows (first 6 segments)")
        sp_sum = defaultdict(int)
        for r in sp:
            sp_sum[(r["Runs"], ";".join(segments(r["specific_taxonomy"])[:6]))] += int(float(r["count"] or 0))
        ge_sum = defaultdict(int)
        for r in ge:
            ge_sum[(r["Runs"], ";".join(segments(r["specific_taxonomy"])[:6]))] += int(float(r["count"] or 0))
        keys = set(sp_sum) | set(ge_sum)
        equal = sum(1 for k in keys if sp_sum.get(k) == ge_sum.get(k))
        only_sp = sum(1 for k in keys if k not in ge_sum)
        only_ge = sum(1 for k in keys if k not in sp_sum)
        bigger = sum(1 for k in keys if k in sp_sum and k in ge_sum and sp_sum[k] > ge_sum[k])
        smaller = sum(1 for k in keys if k in sp_sum and k in ge_sum and sp_sum[k] < ge_sum[k])
        print(f"  (run, genus) keys {len(keys)}: equal {equal}, species larger {bigger}, species smaller {smaller}, "
              f"only in species {only_sp}, only in genus {only_ge}")
        runs_ok = sum(1 for run in {k[0] for k in keys}
                      if all(sp_sum.get(k) == ge_sum.get(k) for k in keys if k[0] == run))
        print(f"  runs where every genus matches: {runs_ok} of {len({k[0] for k in keys})}")
        diff = [(k, sp_sum.get(k), ge_sum.get(k)) for k in sorted(keys) if sp_sum.get(k) != ge_sum.get(k)]
        for k, a, b in diff[:5]:
            print(f"  e.g. {k[0]} {k[1]!r}: species sum {a}, genus row {b}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
