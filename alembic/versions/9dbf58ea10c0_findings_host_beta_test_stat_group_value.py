"""findings: host measures, beta diversity, 'differs', age window, test statistic, core.finding_group_value

Revision ID: 9dbf58ea10c0
Revises: 2bae84543400
Create Date: 2026-11-07 15:40:00

What study 2 (PRJNA664026, PMC8001633) needs that study 1 didn't (decided 2026-11-07, Rodrigo; handoff
2026-11-07 §4a):
  Q1  host measures (body weight, ADG, ADFI, FCR, mortality) live in core.finding. Rules keyed on the measure
      class: a microbiome measure needs a gut site and one feature or feature set; a host measure has neither,
      and needs an age window. age_from_d / age_to_d are general columns (a microbiome finding may carry them).
  Q2  measure beta_diversity, feature_kind distance_metric; direction 'differs', only on an omnibus contrast
      (every significant omnibus test says the groups differ, not which way).
  Q2b test, test_stat_kind, test_stat_value, df_num, df_den: the F or pseudo-F a table prints, with its d.f.
  Q5  stat_source 'table': a statistic read from a table cell (checked by jats.table_rows(), not a quote).
  Q6  core.finding_group_value: a group's mean (and spread, if printed) per level of the finding's contrast.
      Its FK goes through core.finding_level, so a value for a level outside the contrast is unstorable.

Added beyond the decisions, because adding beta diversity made the mismatch storable: the feature kind must fit
the measure (genus/otu → relative_abundance, alpha_index → alpha_diversity, distance_metric → beta_diversity,
enzyme → predicted_gene_count), and the statistic kind must fit the test (F → GLM/GLMM, pseudo_F → PERMANOVA).
Study 1's 16 stored findings satisfy both (checked against conf/findings/PRJNA667518.yml, 2026-11-07).

Downgrade refuses (by the restored checks) while any host-measure, beta, 'differs' or table-sourced finding is
stored: delete those rows first. That is deliberate: a downgrade must not silently drop curated findings.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '9dbf58ea10c0'
down_revision: Union[str, Sequence[str], None] = '2bae84543400'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# frozen here, not imported from app.models: a migration describes the schema at its own revision
MICROBIOME_MEASURES = ("relative_abundance", "alpha_diversity", "beta_diversity", "predicted_gene_count")
HOST_MEASURES = ("body_weight", "average_daily_gain", "average_daily_feed_intake", "feed_conversion_ratio",
                 "mortality")
FEATURE_KINDS = ("genus", "otu", "enzyme", "alpha_index", "distance_metric")
DIRECTIONS = ("increase", "decrease", "no_difference", "counts", "differs")
STAT_SOURCES = ("text", "figure_legend", "methods", "table")
FEATURE_FITS_MEASURE = (("relative_abundance", "genus"), ("relative_abundance", "otu"),
                        ("alpha_diversity", "alpha_index"), ("beta_diversity", "distance_metric"),
                        ("predicted_gene_count", "enzyme"))
TESTS = ("GLM", "GLMM", "PERMANOVA", "LSD")
TEST_STAT_KINDS = ("F", "pseudo_F")
STAT_FITS_TEST = (("GLM", "F"), ("GLMM", "F"), ("PERMANOVA", "pseudo_F"))
VALUE_KINDS = ("mean",)
SPREAD_KINDS = ("se", "sd", "unstated")

# as created by 284f0d1b9c5d, for the downgrade
OLD_MEASURES = ("relative_abundance", "alpha_diversity", "predicted_gene_count")
OLD_FEATURE_KINDS = ("genus", "otu", "enzyme", "alpha_index")
OLD_DIRECTIONS = ("increase", "decrease", "no_difference", "counts")
OLD_STAT_SOURCES = ("text", "figure_legend", "methods")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def _pairs_in(cols: str, pairs: tuple[tuple[str, str], ...]) -> str:
    return f"({cols}) IN ({', '.join(f'({a!r}, {b!r})' for a, b in pairs)})"


HOST = _in("measure", HOST_MEASURES)

NEW_CHECKS = {
    # Q1: where and what, keyed on the measure class
    "finding_site_by_measure": f"({HOST}) = (site_term IS NULL)",
    "finding_host_no_material": f"NOT ({HOST}) OR material IS NULL",
    "finding_feature_fits_measure": f"feature_kind IS NULL OR {_pairs_in('measure, feature_kind', FEATURE_FITS_MEASURE)}",
    "finding_age_pair": "(age_from_d IS NULL) = (age_to_d IS NULL)",
    "finding_age_order": "age_from_d IS NULL OR (age_from_d >= 0 AND age_to_d >= age_from_d)",
    "finding_host_needs_age": f"NOT ({HOST}) OR age_from_d IS NOT NULL",
    # Q2a
    "finding_differs_omnibus": "direction <> 'differs' OR contrast_kind = 'omnibus'",
    # Q2b: a test may stand alone (LSD prints no statistic); a statistic needs its test; d.f. need a statistic
    "finding_test_check": f"test IS NULL OR {_in('test', TESTS)}",
    "finding_test_stat_kind_check": f"test_stat_kind IS NULL OR {_in('test_stat_kind', TEST_STAT_KINDS)}",
    "finding_test_stat_pair": "(test_stat_kind IS NULL) = (test_stat_value IS NULL)",
    "finding_test_stat_nonneg": "test_stat_value IS NULL OR test_stat_value >= 0",
    "finding_test_stat_needs_test": "test IS NOT NULL OR (test_stat_kind IS NULL AND df_num IS NULL)",
    "finding_test_stat_fits_test": f"test_stat_kind IS NULL OR {_pairs_in('test, test_stat_kind', STAT_FITS_TEST)}",
    "finding_df_pair": "(df_num IS NULL) = (df_den IS NULL)",
    "finding_df_positive": "df_num IS NULL OR (df_num > 0 AND df_den > 0)",
    "finding_df_needs_stat": "df_num IS NULL OR test_stat_kind IS NOT NULL",
}

# replaced in place (same name, wider rule)
REPLACED = {
    "finding_measure_check": (_in("measure", MICROBIOME_MEASURES + HOST_MEASURES), _in("measure", OLD_MEASURES)),
    "finding_feature_kind_check": (f"feature_kind IS NULL OR {_in('feature_kind', FEATURE_KINDS)}",
                                   f"feature_kind IS NULL OR {_in('feature_kind', OLD_FEATURE_KINDS)}"),
    "finding_direction_check": (_in("direction", DIRECTIONS), _in("direction", OLD_DIRECTIONS)),
    "finding_stat_source_check": (f"stat_source IS NULL OR {_in('stat_source', STAT_SOURCES)}",
                                  f"stat_source IS NULL OR {_in('stat_source', OLD_STAT_SOURCES)}"),
    # a host measure is about the animals, not a feature; a microbiome measure about exactly one feature or set
    "finding_about_one": (f"CASE WHEN {HOST} THEN feature_label IS NULL AND feature_set_id IS NULL "
                          f"ELSE (feature_label IS NULL) <> (feature_set_id IS NULL) END",
                          "(feature_label IS NULL) <> (feature_set_id IS NULL)"),
}

NEW_COLUMNS = (
    ("age_from_d", sa.Integer),
    ("age_to_d", sa.Integer),
    ("test", sa.Text),
    ("test_stat_kind", sa.Text),
    ("test_stat_value", sa.Numeric),
    ("df_num", sa.Integer),
    ("df_den", sa.Integer),
)


def upgrade() -> None:
    for name, typ in NEW_COLUMNS:
        op.add_column("finding", sa.Column(name, typ), schema="core")
    op.alter_column("finding", "site_term", existing_type=sa.Text, nullable=True, schema="core")
    for name, (new, _old) in REPLACED.items():
        op.drop_constraint(name, "finding", schema="core", type_="check")
        op.create_check_constraint(name, "finding", new, schema="core")
    for name, rule in NEW_CHECKS.items():
        op.create_check_constraint(name, "finding", rule, schema="core")

    op.create_table(
        "finding_group_value",
        sa.Column("finding_id", sa.BigInteger, primary_key=True),
        sa.Column("level_id", sa.BigInteger, primary_key=True),
        sa.Column("value_kind", sa.Text, primary_key=True),          # 'mean'
        sa.Column("value", sa.Numeric, nullable=False),              # exact decimal, as printed
        sa.Column("spread", sa.Numeric),                             # the bracketed number, if printed
        sa.Column("spread_kind", sa.Text),                           # se | sd | unstated
        sa.Column("unit", sa.Text),                                  # as printed ('g/d'); NULL = none printed
        sa.Column("evidence", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("ingest_run_id", sa.BigInteger, sa.ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["finding_id", "level_id"], ["core.finding_level.finding_id",
                                                             "core.finding_level.level_id"],
                                name="finding_group_value_level_fk"),
        sa.CheckConstraint(_in("value_kind", VALUE_KINDS), name="finding_group_value_kind_check"),
        sa.CheckConstraint("(spread IS NULL) = (spread_kind IS NULL)", name="finding_group_value_spread_pair"),
        sa.CheckConstraint(f"spread_kind IS NULL OR {_in('spread_kind', SPREAD_KINDS)}",
                           name="finding_group_value_spread_kind_check"),
        sa.CheckConstraint("spread IS NULL OR spread >= 0", name="finding_group_value_spread_nonneg"),
        sa.Index("finding_group_value_level_idx", "level_id"),
        schema="core",
    )


def downgrade() -> None:
    op.drop_table("finding_group_value", schema="core")
    for name in reversed(list(NEW_CHECKS)):
        op.drop_constraint(name, "finding", schema="core", type_="check")
    for name, (_new, old) in REPLACED.items():
        op.drop_constraint(name, "finding", schema="core", type_="check")
        op.create_check_constraint(name, "finding", old, schema="core")
    op.alter_column("finding", "site_term", existing_type=sa.Text, nullable=False, schema="core")
    for name, _typ in reversed(NEW_COLUMNS):
        op.drop_column("finding", name, schema="core")
