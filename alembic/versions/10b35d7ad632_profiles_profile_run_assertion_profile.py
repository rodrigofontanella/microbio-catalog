"""profiles: core.profile, core.profile_run, assertion arc + profile_id (9 targets)

Revision ID: 10b35d7ad632
Revises: 284f0d1b9c5d
Create Date: 2026-10-13 18:14:20

A profile is a pointer into Delta (design doc §5.9, §6): which table, which version, which source file, and
the grain it was loaded at. Decided 2026-10-13 (Rodrigo):
- one profile per (source, source_release, study, feature_type, taxon_rank). Region and primers differ
  between studies, so a profile never spans studies;
- profile_key is that tuple as text. Delta stores the key, because the Delta commit comes BEFORE the
  Postgres row exists, and a re-run must find "its" rows without a profile_id;
- profiles attach to RUNS (PGMD measures runs). A run in a profile must belong to the profile's study:
  composite FKs through (profile_id, study_id) and (run_acc, study_id) make anything else unstorable;
- what is inferred about a profile (reference GG 13_8, marker, region, pipeline) is an assertion with a tier,
  not a column, so prov.assertion gains profile_id as its 9th target.

Downgrade drops profile assertions with the column; the 8-target check is then violated by any assertion
that pointed only at a profile, so the downgrade refuses while such rows exist (it never deletes them).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '10b35d7ad632'
down_revision: Union[str, Sequence[str], None] = '284f0d1b9c5d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SOURCES = ("microbeatlas", "micoda", "pgmd", "nutreco_boxmeer", "own")
FEATURE_TYPES = ("asv", "mapseq_otu", "otu_closed", "otu_open", "otu_denovo", "read_classification",
                 "taxon_collapsed")
TAXON_RANKS = ("phylum", "class", "order", "family", "genus", "species")
ABUNDANCE_KINDS = ("counts", "relative", "presence")

ARC_8 = "study_id, subject_id, sample_id, library_id, run_acc, genome_acc, experiment_id, doc_id"
ARC_9 = f"{ARC_8}, profile_id"


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def ingest_run_fk():
    return sa.Column("ingest_run_id", sa.BigInteger,
                     sa.ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)


def created_at():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                     server_default=sa.text("now()"))


def upgrade() -> None:
    # target of profile_run's composite FK: a run together with the study it belongs to
    op.create_unique_constraint("run_acc_study_key", "run", ["run_acc", "study_id"], schema="core")

    op.create_table(
        "profile",
        sa.Column("profile_id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("profile_key", sa.Text, nullable=False),       # 'pgmd|<release>|PRJNA667518|taxon_collapsed|species'
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("source_release", sa.Text, nullable=False),
        sa.Column("study_id", sa.BigInteger, sa.ForeignKey("core.study.study_id"), nullable=False),
        sa.Column("feature_type", sa.Text, nullable=False),
        sa.Column("taxon_rank", sa.Text),                         # taxon_collapsed only
        sa.Column("abundance_kind", sa.Text, nullable=False),
        sa.Column("delta_uri", sa.Text, nullable=False),          # path-based table until Unity Catalog
        sa.Column("delta_version", sa.BigInteger, nullable=False),
        sa.Column("source_uri", sa.Text, nullable=False),         # the file the rows came from
        sa.Column("source_sha256", sa.Text, nullable=False),
        ingest_run_fk(),
        created_at(),
        sa.UniqueConstraint("profile_key", name="profile_key_key"),
        sa.UniqueConstraint("source", "source_release", "study_id", "feature_type", "taxon_rank",
                            name="profile_grain_key", postgresql_nulls_not_distinct=True),
        sa.UniqueConstraint("profile_id", "study_id", name="profile_id_study_key"),
        sa.CheckConstraint(_in("source", SOURCES), name="profile_source_check"),
        sa.CheckConstraint(_in("feature_type", FEATURE_TYPES), name="profile_feature_type_check"),
        sa.CheckConstraint(f"taxon_rank IS NULL OR {_in('taxon_rank', TAXON_RANKS)}",
                           name="profile_taxon_rank_check"),
        sa.CheckConstraint("(feature_type = 'taxon_collapsed') = (taxon_rank IS NOT NULL)",
                           name="profile_rank_pair"),
        sa.CheckConstraint(_in("abundance_kind", ABUNDANCE_KINDS), name="profile_abundance_kind_check"),
        sa.CheckConstraint("delta_uri LIKE 's3a://%'", name="profile_delta_uri_s3a"),
        sa.CheckConstraint("source_uri LIKE 's3a://%'", name="profile_source_uri_s3a"),
        sa.CheckConstraint("delta_version >= 0", name="profile_delta_version_check"),
        sa.CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'", name="profile_source_sha256_format"),
        sa.Index("profile_study_idx", "study_id"),
        schema="core",
    )

    op.create_table(
        "profile_run",
        sa.Column("profile_id", sa.BigInteger, primary_key=True),
        sa.Column("run_acc", sa.Text, primary_key=True),
        sa.Column("study_id", sa.BigInteger, nullable=False),
        sa.Column("n_reads", sa.BigInteger),                     # sum of counts in the profile; NULL if not counts
        sa.Column("n_features", sa.Integer, nullable=False),     # features with a count > 0
        ingest_run_fk(),
        created_at(),
        sa.ForeignKeyConstraint(["profile_id", "study_id"], ["core.profile.profile_id", "core.profile.study_id"],
                                name="profile_run_profile_fk"),
        sa.ForeignKeyConstraint(["run_acc", "study_id"], ["core.run.run_acc", "core.run.study_id"],
                                name="profile_run_run_fk"),
        sa.CheckConstraint("n_reads IS NULL OR n_reads > 0", name="profile_run_n_reads_check"),
        sa.CheckConstraint("n_features > 0", name="profile_run_n_features_check"),
        sa.Index("profile_run_run_idx", "run_acc"),
        schema="core",
    )

    # ── facts ABOUT a profile (reference DB, marker, region, pipeline): the arc gets a 9th target ──
    op.add_column("assertion", sa.Column("profile_id", sa.BigInteger,
                                         sa.ForeignKey("core.profile.profile_id", name="assertion_profile_id_fkey")),
                  schema="prov")
    op.drop_constraint("assertion_one_entity", "assertion", schema="prov", type_="check")
    op.create_check_constraint("assertion_one_entity", "assertion", f"num_nonnulls({ARC_9}) = 1", schema="prov")
    op.create_index("assertion_profile_field_idx", "assertion", ["profile_id", "field"], schema="prov")


def downgrade() -> None:
    op.drop_index("assertion_profile_field_idx", table_name="assertion", schema="prov")
    op.drop_constraint("assertion_one_entity", "assertion", schema="prov", type_="check")
    op.drop_column("assertion", "profile_id", schema="prov")
    op.create_check_constraint("assertion_one_entity", "assertion", f"num_nonnulls({ARC_8}) = 1", schema="prov")
    op.drop_table("profile_run", schema="core")
    op.drop_table("profile", schema="core")
    op.drop_constraint("run_acc_study_key", "run", schema="core", type_="unique")
