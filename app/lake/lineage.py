"""PGMD lineage strings: pure functions, no Spark (handoff Step 6 survey, design doc §2.1.1).

A PGMD lineage is ';'-separated with a fixed number of segments per level (Species = 7), no rank prefixes, and
'__' for an empty rank. The lineage is the feature key; `microbial_name` is only a label ("Unknown" whenever the
lineage doesn't resolve to the row's own level). Resolution comes from the lineage: the number of leading
segments that carry a name.
"""
from __future__ import annotations

RANKS = ("kingdom", "phylum", "class", "order", "family", "genus", "species")
# PGMD `level` → number of lineage segments at that level (no Kingdom rows in PGMD)
LEVEL_SEGMENTS = {"Phylum": 2, "Class": 3, "Order": 4, "Family": 5, "Genus": 6, "Species": 7}
# the same markers as scripts/pgmd_profile_inspect.py, so resolved depths reproduce the survey
EMPTY = frozenset({"", "__", "k__", "p__", "c__", "o__", "f__", "g__", "s__", "unclassified", "Unknown"})


def split_lineage(lineage: str) -> list[str]:
    return [s.strip() for s in lineage.split(";")]


def resolved_depth(segments: list[str]) -> int:
    """Leading segments that carry a name; the first empty marker ends the resolution."""
    n = 0
    for s in segments:
        if s in EMPTY:
            break
        n += 1
    return n


def normalize(lineage: str) -> str:
    """The feature key: segments stripped of whitespace, joined by ';' (identical to PGMD's string when clean)."""
    return ";".join(split_lineage(lineage))


def taxon_row(lineage: str, n_segments: int = 7) -> dict[str, str | int | None]:
    """One feature_taxon row: a column per rank (None where empty), resolved rank + depth, and whether a named
    rank follows an empty one (a gap, e.g. 'Bacteria;__;Clostridia': kept as written, flagged).

    Raises ValueError if the lineage doesn't have exactly n_segments segments: a shape change in the source is
    a reason to stop, not to guess.
    """
    segs = split_lineage(lineage)
    if len(segs) != n_segments:
        raise ValueError(f"expected {n_segments} segments, got {len(segs)}: {lineage!r}")
    depth = resolved_depth(segs)
    row: dict[str, str | int | None] = {r: (seg if seg not in EMPTY else None)
                                        for r, seg in zip(RANKS[:n_segments], segs)}
    for r in RANKS[n_segments:]:
        row[r] = None
    row["resolved_depth"] = depth
    row["resolved_rank"] = RANKS[depth - 1] if depth else None
    row["has_gap"] = any(seg not in EMPTY for seg in segs[depth:])
    return row


def genus_key(lineage: str) -> str:
    """The first 6 segments, normalized: a Species lineage's Genus lineage (the genus test's join key)."""
    return ";".join(split_lineage(lineage)[:6])
