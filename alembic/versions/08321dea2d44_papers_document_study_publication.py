"""papers: rag.document, core.study_publication; assertions about documents

Revision ID: 08321dea2d44
Revises: db644d6f60c7
Create Date: 2026-10-11 09:11:16

A paper (or any document) gets one row, identified the way Europe PMC identifies it, (source, ext_id),
with pmid / pmcid / doi as nullable alternate keys (design doc §2.2, item 8). Studies and papers are
many-to-many, and every link records how it was established, so a link the paper itself states
(Data Availability) and one a curator made (PGMD) can both be kept. Chunks and embeddings come later.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '08321dea2d44'
down_revision: Union[str, Sequence[str], None] = 'db644d6f60c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DOC_KINDS = ("bioproject_desc", "abstract", "protocol", "source_docs", "paper", "own_draft", "own_methods",
             "lab_note")
VISIBILITY = ("public", "own", "private")
LINK_METHODS = ("paper_data_availability", "pgmd", "ncbi_elink", "manual")


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
        "document",
        sa.Column("doc_id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("source", sa.Text, nullable=False),          # Europe PMC source: MED, PMC, PPR, AGR …
        sa.Column("ext_id", sa.Text, nullable=False),          # its id within that source
        sa.Column("pmid", sa.Text),
        sa.Column("pmcid", sa.Text),
        sa.Column("doi", sa.Text),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("visibility", sa.Text, nullable=False, server_default="public"),
        sa.Column("title", sa.Text),
        sa.Column("journal", sa.Text),
        sa.Column("pub_year", sa.Integer),
        sa.Column("license", sa.Text),                         # as the source states it, e.g. 'cc by'
        sa.Column("is_open_access", sa.Boolean),
        sa.Column("raw_uri", sa.Text),                         # folder with the fetched files
        sa.Column("raw_sha256", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),       # file name → sha256
        ingest_run_fk(),
        created_at(),
        sa.UniqueConstraint("source", "ext_id", name="document_source_ext_id_key"),
        sa.CheckConstraint(_in("kind", DOC_KINDS), name="document_kind_check"),
        sa.CheckConstraint(_in("visibility", VISIBILITY), name="document_visibility_check"),
        sa.CheckConstraint("pmcid IS NULL OR pmcid ~ '^PMC[0-9]+$'", name="document_pmcid_format"),
        sa.Index("document_pmid_key", "pmid", unique=True, postgresql_where=sa.text("pmid IS NOT NULL")),
        sa.Index("document_pmcid_key", "pmcid", unique=True, postgresql_where=sa.text("pmcid IS NOT NULL")),
        sa.Index("document_doi_key", sa.text("lower(doi)"), unique=True,
                 postgresql_where=sa.text("doi IS NOT NULL")),
        schema="rag",
    )

    op.create_table(
        "study_publication",
        sa.Column("study_id", sa.BigInteger, sa.ForeignKey("core.study.study_id"), primary_key=True),
        sa.Column("doc_id", sa.BigInteger, sa.ForeignKey("rag.document.doc_id"), primary_key=True),
        sa.Column("link_method", sa.Text, primary_key=True),   # how we know this paper describes this study
        sa.Column("evidence", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        ingest_run_fk(),
        created_at(),
        sa.CheckConstraint(_in("link_method", LINK_METHODS), name="study_publication_link_method_check"),
        sa.Index("study_publication_doc_id_idx", "doc_id"),
        schema="core",
    )

    # facts ABOUT a document (e.g. errors found in a paper) — the exclusive arc gets an 8th target
    op.add_column("assertion", sa.Column("doc_id", sa.BigInteger,
                                         sa.ForeignKey("rag.document.doc_id", name="assertion_doc_id_fkey")),
                  schema="prov")
    op.drop_constraint("assertion_one_entity", "assertion", schema="prov", type_="check")
    op.create_check_constraint(
        "assertion_one_entity", "assertion",
        "num_nonnulls(study_id, subject_id, sample_id, library_id, run_acc, genome_acc, experiment_id, doc_id) = 1",
        schema="prov")
    op.create_index("assertion_doc_field_idx", "assertion", ["doc_id", "field"], schema="prov")


def downgrade() -> None:
    op.drop_index("assertion_doc_field_idx", table_name="assertion", schema="prov")
    op.drop_constraint("assertion_one_entity", "assertion", schema="prov", type_="check")
    op.create_check_constraint(
        "assertion_one_entity", "assertion",
        "num_nonnulls(study_id, subject_id, sample_id, library_id, run_acc, genome_acc, experiment_id) = 1",
        schema="prov")
    op.drop_column("assertion", "doc_id", schema="prov")
    op.drop_table("study_publication", schema="core")
    op.drop_table("document", schema="rag")
