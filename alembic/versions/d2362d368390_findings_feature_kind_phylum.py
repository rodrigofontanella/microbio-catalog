"""findings: feature kind 'phylum' (relative abundance only)

Revision ID: d2362d368390
Revises: a8f4d5f5fd45
Create Date: 2026-11-10 15:30:00

What study 3 (PRJNA430317, PMC7568366) needs (decided 2026-11-10, Rodrigo): the paper's phylum claims are stored
(endpoint Firmicutes, Bacteroidetes, Tenericutes in caecum and colon; the four baseline phyla in day-0 feces),
so a finding may be about one phylum. A phylum fits relative_abundance only, like genus and otu.

Not added (decided the same day): a Firmicutes/Bacteroidetes ratio kind. The ratio is derived from two phyla
the findings already carry; one claim doesn't justify a new concept.

core.feature_set's member kinds are unchanged: no study has declared a set of phyla.

Two checks change (same names, wider rules). Downgrade restores the narrower ones, which refuse while any
finding is about a phylum: delete those first (a downgrade must not silently drop curated findings).
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'd2362d368390'
down_revision: Union[str, Sequence[str], None] = 'a8f4d5f5fd45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# frozen here, not imported from app.models: a migration describes the schema at its own revision
OLD_FEATURE_KINDS = ("genus", "otu", "enzyme", "alpha_index", "distance_metric")
FEATURE_KINDS = OLD_FEATURE_KINDS + ("phylum",)
OLD_FEATURE_FITS_MEASURE = (("relative_abundance", "genus"), ("relative_abundance", "otu"),
                            ("alpha_diversity", "alpha_index"), ("beta_diversity", "distance_metric"),
                            ("predicted_gene_count", "enzyme"))
FEATURE_FITS_MEASURE = OLD_FEATURE_FITS_MEASURE + (("relative_abundance", "phylum"),)


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def _pairs_in(cols: str, pairs: tuple[tuple[str, str], ...]) -> str:
    return f"({cols}) IN ({', '.join(f'({a!r}, {b!r})' for a, b in pairs)})"


def _rules(kinds: tuple[str, ...], fits: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return {
        "finding_feature_kind_check": f"feature_kind IS NULL OR {_in('feature_kind', kinds)}",
        "finding_feature_fits_measure": f"feature_kind IS NULL OR {_pairs_in('measure, feature_kind', fits)}",
    }


def _replace(rules: dict[str, str]) -> None:
    for name, rule in rules.items():
        op.drop_constraint(name, "finding", schema="core", type_="check")
        op.create_check_constraint(name, "finding", rule, schema="core")


def upgrade() -> None:
    _replace(_rules(FEATURE_KINDS, FEATURE_FITS_MEASURE))


def downgrade() -> None:
    _replace(_rules(OLD_FEATURE_KINDS, OLD_FEATURE_FITS_MEASURE))
