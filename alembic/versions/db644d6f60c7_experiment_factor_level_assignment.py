"""experiment layer: experiment, factor, factor_level, subject_factor_level; sample_type; assertions on experiments

Revision ID: db644d6f60c7
Revises: 5fa98eb29c21
Create Date: 2026-10-11 08:01:49

Shaped by PRJNA667518 (design doc §5.6): a randomised block design where the pen is the experimental
unit, with a treatment factor (diet), a cohort (rep) and a body-weight block NESTED in rep.

Composite foreign keys keep wrong combinations unstorable:
- a factor's experiment, a subject and its factor assignments all belong to the same study;
- an assigned level belongs to the assigned factor;
- a nested level's parent belongs to the factor it is nested in;
- one level per factor per subject (primary key).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'db644d6f60c7'
down_revision: Union[str, Sequence[str], None] = '5fa98eb29c21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DESIGN_TYPES = ("completely_randomized", "randomized_block", "factorial", "crossover",
                "repeated_measures", "observational")
EXPERIMENTAL_UNITS = ("animal", "pen", "litter", "room", "farm")
FACTOR_KINDS = ("treatment", "blocking", "time", "nuisance")
SAMPLE_TYPES = ("biological", "negative_control", "extraction_blank", "mock_community")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def ingest_run_fk():
    return sa.Column("ingest_run_id", sa.BigInteger,
                     sa.ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)


def created_at():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                     server_default=sa.text("now()"))


def upgrade() -> None:
    # target of subject_factor_level's composite FK: (subject, study) pairs
    op.create_unique_constraint("subject_id_study_key", "subject", ["subject_id", "study_id"], schema="core")

    # blanks and mock communities are samples too (low-biomass tissues need them)
    op.add_column("sample", sa.Column("sample_type", sa.Text, nullable=False, server_default="biological"),
                  schema="core")
    op.create_check_constraint("sample_type_check", "sample", _in("sample_type", SAMPLE_TYPES), schema="core")

    # ── core.experiment: one designed comparison within a study (ISA "study") ─
    op.create_table(
        "experiment",
        sa.Column("experiment_id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("study_id", sa.BigInteger, sa.ForeignKey("core.study.study_id"), nullable=False),
        sa.Column("local_id", sa.Text, nullable=False),          # 'main', or a name when a study has several
        sa.Column("title", sa.Text),
        sa.Column("design_type", sa.Text, nullable=False),
        sa.Column("experimental_unit", sa.Text, nullable=False),  # what was randomised: the pen, not the sample
        ingest_run_fk(),
        created_at(),
        sa.UniqueConstraint("study_id", "local_id", name="experiment_study_local_id_key"),
        sa.UniqueConstraint("experiment_id", "study_id", name="experiment_id_study_key"),
        sa.CheckConstraint(_in("design_type", DESIGN_TYPES), name="experiment_design_type_check"),
        sa.CheckConstraint(_in("experimental_unit", EXPERIMENTAL_UNITS), name="experiment_unit_check"),
        schema="core",
    )

    # ── core.factor: diet, rep, block … A factor can be nested in another ────
    op.create_table(
        "factor",
        sa.Column("factor_id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("experiment_id", sa.BigInteger, nullable=False),
        sa.Column("study_id", sa.BigInteger, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("nested_in_factor_id", sa.BigInteger),          # block → rep
        sa.Column("description", sa.Text),
        sa.Column("definition", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),          # where the levels come from (spec + source attribute)
        ingest_run_fk(),
        created_at(),
        sa.ForeignKeyConstraint(["experiment_id", "study_id"], ["core.experiment.experiment_id",
                                                                "core.experiment.study_id"],
                                name="factor_experiment_fk"),
        sa.UniqueConstraint("experiment_id", "name", name="factor_experiment_name_key"),
        sa.UniqueConstraint("factor_id", "study_id", name="factor_id_study_key"),
        sa.UniqueConstraint("factor_id", "experiment_id", name="factor_id_experiment_key"),
        sa.UniqueConstraint("factor_id", "nested_in_factor_id", name="factor_id_nested_key"),
        # the parent factor must be in the same experiment
        sa.ForeignKeyConstraint(["nested_in_factor_id", "experiment_id"], ["core.factor.factor_id",
                                                                         "core.factor.experiment_id"],
                                name="factor_nested_in_fk"),
        sa.CheckConstraint("nested_in_factor_id IS DISTINCT FROM factor_id", name="factor_not_nested_in_itself"),
        sa.CheckConstraint(_in("kind", FACTOR_KINDS), name="factor_kind_check"),
        schema="core",
    )

    # ── core.factor_level: LF / HF / …; a nested level points at its parent level ─
    op.create_table(
        "factor_level",
        sa.Column("level_id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("factor_id", sa.BigInteger, nullable=False),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("is_control", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("description", sa.Text),
        sa.Column("parent_factor_id", sa.BigInteger),             # = the factor's nested_in_factor_id
        sa.Column("parent_level_id", sa.BigInteger),              # 'block 2' → 'rep 1'
        ingest_run_fk(),
        created_at(),
        sa.UniqueConstraint("level_id", "factor_id", name="factor_level_id_factor_key"),
        # parent_factor_id must be the factor this one is nested in …
        sa.ForeignKeyConstraint(["factor_id", "parent_factor_id"], ["core.factor.factor_id",
                                                                    "core.factor.nested_in_factor_id"],
                                name="factor_level_nesting_fk"),
        # … and the parent level must be a level of that factor
        sa.ForeignKeyConstraint(["parent_level_id", "parent_factor_id"], ["core.factor_level.level_id",
                                                                          "core.factor_level.factor_id"],
                                name="factor_level_parent_fk"),
        sa.CheckConstraint("(parent_level_id IS NULL) = (parent_factor_id IS NULL)",
                           name="factor_level_parent_pair"),
        sa.Index("factor_level_top_label_key", "factor_id", "label", unique=True,
                 postgresql_where=sa.text("parent_level_id IS NULL")),
        sa.Index("factor_level_nested_label_key", "factor_id", "parent_level_id", "label", unique=True,
                 postgresql_where=sa.text("parent_level_id IS NOT NULL")),
        schema="core",
    )

    # ── core.subject_factor_level: which level each animal received ──────────
    op.create_table(
        "subject_factor_level",
        sa.Column("subject_id", sa.BigInteger, primary_key=True),
        sa.Column("factor_id", sa.BigInteger, primary_key=True),  # PK: one level per factor per animal
        sa.Column("level_id", sa.BigInteger, nullable=False),
        sa.Column("study_id", sa.BigInteger, nullable=False),
        ingest_run_fk(),
        created_at(),
        sa.ForeignKeyConstraint(["level_id", "factor_id"], ["core.factor_level.level_id",
                                                            "core.factor_level.factor_id"],
                                name="sfl_level_fk"),
        sa.ForeignKeyConstraint(["subject_id", "study_id"], ["core.subject.subject_id", "core.subject.study_id"],
                                name="sfl_subject_fk"),
        sa.ForeignKeyConstraint(["factor_id", "study_id"], ["core.factor.factor_id", "core.factor.study_id"],
                                name="sfl_factor_fk"),
        sa.Index("sfl_level_id_idx", "level_id"),
        schema="core",
    )

    # ── prov.assertion can now describe an experiment (ARRIVE items, Step 5) ─
    op.add_column("assertion", sa.Column("experiment_id", sa.BigInteger,
                                         sa.ForeignKey("core.experiment.experiment_id",
                                                       name="assertion_experiment_id_fkey")),
                  schema="prov")
    op.drop_constraint("assertion_one_entity", "assertion", schema="prov", type_="check")
    op.create_check_constraint(
        "assertion_one_entity", "assertion",
        "num_nonnulls(study_id, subject_id, sample_id, library_id, run_acc, genome_acc, experiment_id) = 1",
        schema="prov")
    op.create_index("assertion_experiment_field_idx", "assertion", ["experiment_id", "field"], schema="prov")


def downgrade() -> None:
    op.drop_index("assertion_experiment_field_idx", table_name="assertion", schema="prov")
    op.drop_constraint("assertion_one_entity", "assertion", schema="prov", type_="check")
    op.create_check_constraint(
        "assertion_one_entity", "assertion",
        "num_nonnulls(study_id, subject_id, sample_id, library_id, run_acc, genome_acc) = 1",
        schema="prov")
    op.drop_column("assertion", "experiment_id", schema="prov")
    op.drop_table("subject_factor_level", schema="core")
    op.drop_table("factor_level", schema="core")
    op.drop_table("factor", schema="core")
    op.drop_table("experiment", schema="core")
    op.drop_constraint("sample_type_check", "sample", schema="core", type_="check")
    op.drop_column("sample", "sample_type", schema="core")
    op.drop_constraint("subject_id_study_key", "subject", schema="core", type_="unique")
