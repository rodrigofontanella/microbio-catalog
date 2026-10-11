"""findings: core.feature_set, core.finding, core.finding_level

Revision ID: 284f0d1b9c5d
Revises: 08321dea2d44
Create Date: 2026-10-11 14:00:00

What a paper claims about a contrast (design doc §5.8). A finding is about exactly one feature (a taxon
label, an OTU, an alpha index, an enzyme) or one named feature set ("200 most abundant OTUs"). Pairwise
contrasts are two level columns, checked by composite FKs against the experiment's own factor;
omnibus tests and groupings list their levels in core.finding_level, against the same factor.
Null results (no_difference) and counts over a set are findings too. A statistic records its source
(the claim's sentence, a figure legend, the Methods) and its scope (treatment, contrast, unknown).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '284f0d1b9c5d'
down_revision: Union[str, Sequence[str], None] = '08321dea2d44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONTRAST_KINDS = ("pairwise", "omnibus", "grouping")
FEATURE_KINDS = ("genus", "otu", "enzyme", "alpha_index")
MATERIALS = ("digesta", "mucosa", "swab")
MEASURES = ("relative_abundance", "alpha_diversity", "predicted_gene_count")
DIRECTIONS = ("increase", "decrease", "no_difference", "counts")
STAT_KINDS = ("Q", "p")
STAT_OPS = ("<", ">", "=", "<=", ">=")
STAT_SCOPES = ("treatment", "contrast", "unknown")
STAT_SOURCES = ("text", "figure_legend", "methods")
LEVEL_ROLES = ("member", "higher", "lower")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def ingest_run_fk():
    return sa.Column("ingest_run_id", sa.BigInteger,
                     sa.ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)


def created_at():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                     server_default=sa.text("now()"))


def upgrade() -> None:
    op.create_table(
        "feature_set",
        sa.Column("feature_set_id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("study_id", sa.BigInteger, sa.ForeignKey("core.study.study_id"), nullable=False),
        sa.Column("local_id", sa.Text, nullable=False),            # 'top200-otus-cae-dig'
        sa.Column("member_kind", sa.Text, nullable=False),
        sa.Column("definition", sa.Text, nullable=False),          # as printed: '200 most abundant OTUs'
        sa.Column("size", sa.Integer),
        sa.Column("site_term", sa.Text),
        sa.Column("material", sa.Text),
        sa.Column("otu_dataset", sa.Text),
        ingest_run_fk(),
        created_at(),
        sa.UniqueConstraint("study_id", "local_id", name="feature_set_study_local_id_key"),
        sa.UniqueConstraint("feature_set_id", "study_id", name="feature_set_id_study_key"),
        sa.CheckConstraint(_in("member_kind", FEATURE_KINDS), name="feature_set_member_kind_check"),
        sa.CheckConstraint(f"material IS NULL OR {_in('material', MATERIALS)}", name="feature_set_material_check"),
        sa.CheckConstraint("size IS NULL OR size > 0", name="feature_set_size_check"),
        schema="core",
    )

    op.create_table(
        "finding",
        sa.Column("finding_id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("study_id", sa.BigInteger, nullable=False),
        sa.Column("experiment_id", sa.BigInteger, nullable=False),
        sa.Column("doc_id", sa.BigInteger, sa.ForeignKey("rag.document.doc_id"), nullable=False),
        sa.Column("local_id", sa.Text, nullable=False),            # claim id in the findings spec
        # what was compared
        sa.Column("contrast_kind", sa.Text, nullable=False),
        sa.Column("factor_id", sa.BigInteger, nullable=False),     # the treatment factor (diet)
        sa.Column("treatment_level_id", sa.BigInteger),            # pairwise only
        sa.Column("reference_level_id", sa.BigInteger),            # pairwise only
        # where, and what was measured
        sa.Column("site_term", sa.Text, nullable=False),
        sa.Column("material", sa.Text),                            # NULL = not stated
        sa.Column("measure", sa.Text, nullable=False),
        # about exactly one feature or one feature set
        sa.Column("feature_kind", sa.Text),
        sa.Column("feature_label", sa.Text),                       # as printed
        sa.Column("feature_rank", sa.Text),
        sa.Column("otu_dataset", sa.Text),
        sa.Column("taxonomy", sa.Text),
        sa.Column("feature_set_id", sa.BigInteger),
        # the result
        sa.Column("direction", sa.Text, nullable=False),
        sa.Column("n_increase", sa.Integer),
        sa.Column("n_decrease", sa.Integer),
        sa.Column("stat_kind", sa.Text),
        sa.Column("stat_op", sa.Text),
        sa.Column("stat_value", sa.Numeric),
        sa.Column("stat_scope", sa.Text),
        sa.Column("stat_source", sa.Text),
        sa.Column("effect_kind", sa.Text),                         # NULL from text; figures only
        sa.Column("effect_value", sa.Numeric),
        sa.Column("effect_se", sa.Numeric),
        sa.Column("figure", sa.Text),
        sa.Column("tier", postgresql.ENUM(name="evidence_tier", schema="prov", create_type=False), nullable=False),
        sa.Column("method", sa.Text, nullable=False),              # paper_results:<pmcid>
        sa.Column("evidence", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        ingest_run_fk(),
        created_at(),
        sa.UniqueConstraint("study_id", "local_id", name="finding_study_local_id_key"),
        sa.UniqueConstraint("finding_id", "factor_id", name="finding_id_factor_key"),
        sa.ForeignKeyConstraint(["experiment_id", "study_id"], ["core.experiment.experiment_id",
                                                                "core.experiment.study_id"],
                                name="finding_experiment_fk"),
        sa.ForeignKeyConstraint(["factor_id", "experiment_id"], ["core.factor.factor_id",
                                                                 "core.factor.experiment_id"],
                                name="finding_factor_fk"),
        sa.ForeignKeyConstraint(["treatment_level_id", "factor_id"], ["core.factor_level.level_id",
                                                                      "core.factor_level.factor_id"],
                                name="finding_treatment_level_fk"),
        sa.ForeignKeyConstraint(["reference_level_id", "factor_id"], ["core.factor_level.level_id",
                                                                      "core.factor_level.factor_id"],
                                name="finding_reference_level_fk"),
        sa.ForeignKeyConstraint(["feature_set_id", "study_id"], ["core.feature_set.feature_set_id",
                                                                 "core.feature_set.study_id"],
                                name="finding_feature_set_fk"),
        sa.CheckConstraint(_in("contrast_kind", CONTRAST_KINDS), name="finding_contrast_kind_check"),
        sa.CheckConstraint("(contrast_kind = 'pairwise') = (treatment_level_id IS NOT NULL "
                           "AND reference_level_id IS NOT NULL)", name="finding_pairwise_levels"),
        sa.CheckConstraint("contrast_kind = 'pairwise' OR (treatment_level_id IS NULL "
                           "AND reference_level_id IS NULL)", name="finding_levels_only_pairwise"),
        sa.CheckConstraint("treatment_level_id <> reference_level_id",
                           name="finding_treatment_not_reference"),
        sa.CheckConstraint(f"material IS NULL OR {_in('material', MATERIALS)}", name="finding_material_check"),
        sa.CheckConstraint(_in("measure", MEASURES), name="finding_measure_check"),
        sa.CheckConstraint("(feature_label IS NULL) <> (feature_set_id IS NULL)", name="finding_about_one"),
        sa.CheckConstraint("(feature_kind IS NULL) = (feature_label IS NULL)", name="finding_feature_kind_pair"),
        sa.CheckConstraint(f"feature_kind IS NULL OR {_in('feature_kind', FEATURE_KINDS)}",
                           name="finding_feature_kind_check"),
        sa.CheckConstraint(_in("direction", DIRECTIONS), name="finding_direction_check"),
        sa.CheckConstraint("(direction = 'counts') = (n_increase IS NOT NULL OR n_decrease IS NOT NULL)",
                           name="finding_counts_pair"),
        sa.CheckConstraint("direction <> 'counts' OR feature_set_id IS NOT NULL", name="finding_counts_on_set"),
        sa.CheckConstraint("num_nonnulls(stat_kind, stat_op, stat_value, stat_scope, stat_source) IN (0, 5)",
                           name="finding_stat_complete"),
        sa.CheckConstraint(f"stat_kind IS NULL OR {_in('stat_kind', STAT_KINDS)}", name="finding_stat_kind_check"),
        sa.CheckConstraint(f"stat_op IS NULL OR {_in('stat_op', STAT_OPS)}", name="finding_stat_op_check"),
        sa.CheckConstraint(f"stat_scope IS NULL OR {_in('stat_scope', STAT_SCOPES)}",
                           name="finding_stat_scope_check"),
        sa.CheckConstraint(f"stat_source IS NULL OR {_in('stat_source', STAT_SOURCES)}",
                           name="finding_stat_source_check"),
        sa.CheckConstraint("(effect_value IS NULL) = (effect_kind IS NULL)", name="finding_effect_pair"),
        sa.Index("finding_contrast_idx", "treatment_level_id", "reference_level_id"),
        sa.Index("finding_feature_label_idx", "feature_label"),
        sa.Index("finding_feature_set_idx", "feature_set_id"),
        sa.Index("finding_doc_idx", "doc_id"),
        schema="core",
    )

    op.create_table(
        "finding_level",
        sa.Column("finding_id", sa.BigInteger, primary_key=True),
        sa.Column("level_id", sa.BigInteger, primary_key=True),
        sa.Column("factor_id", sa.BigInteger, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        ingest_run_fk(),
        sa.ForeignKeyConstraint(["finding_id", "factor_id"], ["core.finding.finding_id", "core.finding.factor_id"],
                                name="finding_level_finding_fk"),
        sa.ForeignKeyConstraint(["level_id", "factor_id"], ["core.factor_level.level_id",
                                                            "core.factor_level.factor_id"],
                                name="finding_level_level_fk"),
        sa.CheckConstraint(_in("role", LEVEL_ROLES), name="finding_level_role_check"),
        sa.Index("finding_level_level_idx", "level_id"),
        schema="core",
    )


def downgrade() -> None:
    op.drop_table("finding_level", schema="core")
    op.drop_table("finding", schema="core")
    op.drop_table("feature_set", schema="core")
