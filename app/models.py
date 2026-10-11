# app/models.py
from sqlalchemy import Text, ForeignKey, ForeignKeyConstraint, Identity, CheckConstraint, text, UniqueConstraint, Index, Boolean, false
from sqlalchemy.dialects.postgresql import JSONB, ENUM
from sqlalchemy import String, Integer, BigInteger, Numeric, Date, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func
from typing import Optional
from datetime import date, datetime


class Base(DeclarativeBase):
    pass


class IngestRun(Base):
    __tablename__ = "ingest_run"
    __table_args__ = (
        CheckConstraint("source IN ('ncbi','microbeatlas','micoda','pgmd','bacdive','europepmc','docs','manual','own')",
                        name="ingest_run_source_check"),
        {"schema": "prov"},
    )

    ingest_run_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_release: Mapped[Optional[str]] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    raw_uri: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Assembly(Base):
    """Genome metadata, stored in ref.genome. Class name kept so routers and scripts don't change."""
    __tablename__ = "genome"
    __table_args__ = (
        CheckConstraint("source IN ('ncbi','pigc','upgg','gtdb')", name="genome_source_check"),
        CheckConstraint("domain IN ('Bacteria','Archaea','Fungi','Other')", name="genome_domain_check"),
        {"schema": "ref"},
    )

    accession: Mapped[str] = mapped_column(String, primary_key=True)
    organism_name: Mapped[str] = mapped_column(String, nullable=False)
    tax_id: Mapped[Optional[int]] = mapped_column(Integer)
    species_tax_id: Mapped[Optional[int]] = mapped_column(Integer)
    assembly_level: Mapped[Optional[str]] = mapped_column(String)
    assembly_name: Mapped[Optional[str]] = mapped_column(String)
    submitter: Mapped[Optional[str]] = mapped_column(String)
    bioproject_accession: Mapped[Optional[str]] = mapped_column(String)
    biosample_accession: Mapped[Optional[str]] = mapped_column(String)
    contig_n50: Mapped[Optional[int]] = mapped_column(Integer)
    total_sequence_length: Mapped[Optional[int]] = mapped_column(BigInteger)
    number_of_contigs: Mapped[Optional[int]] = mapped_column(Integer)
    gc_percent: Mapped[Optional[float]] = mapped_column(Numeric(5, 2))
    submission_date: Mapped[Optional[date]] = mapped_column(Date)
    checkm_completeness: Mapped[Optional[float]] = mapped_column(Numeric(5, 2))
    checkm_contamination: Mapped[Optional[float]] = mapped_column(Numeric(5, 2))
    ani_best_match_organism: Mapped[Optional[str]] = mapped_column(String)
    ani_best_match_percent: Mapped[Optional[float]] = mapped_column(Numeric(5, 2))
    cached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="ncbi")
    domain: Mapped[Optional[str]] = mapped_column(Text)
    gtdb_taxonomy: Mapped[Optional[str]] = mapped_column(Text)
    gtdb_release: Mapped[Optional[str]] = mapped_column(Text)
    file_path: Mapped[Optional[str]] = mapped_column(Text)
    sha256: Mapped[Optional[str]] = mapped_column(Text)
    ingest_run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("prov.ingest_run.ingest_run_id", name="genome_ingest_run_fk"))


# ── catalog core (migration: core study/subject/sample/library/run, prov.assertion) ─

EVIDENCE_TIER = ENUM("inferred", "asserted", "derived", "curated", name="evidence_tier", schema="prov")


class Study(Base):
    __tablename__ = "study"
    __table_args__ = (
        UniqueConstraint("bioproject_acc", name="study_bioproject_acc_key"),
        UniqueConstraint("local_id", name="study_local_id_key"),
        CheckConstraint("bioproject_acc IS NOT NULL OR local_id IS NOT NULL", name="study_has_key"),
        {"schema": "core"},
    )

    study_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    bioproject_acc: Mapped[Optional[str]] = mapped_column(Text)
    local_id: Mapped[Optional[str]] = mapped_column(Text)
    title: Mapped[Optional[str]] = mapped_column(Text)
    description: Mapped[Optional[str]] = mapped_column(Text)
    ingest_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Subject(Base):
    """The animal: the biological unit. Pen and litter are the grouping that defines the experimental unit."""
    __tablename__ = "subject"
    __table_args__ = (
        UniqueConstraint("study_id", "local_id", name="subject_study_local_id_key"),
        UniqueConstraint("subject_id", "study_id", name="subject_id_study_key"),
        {"schema": "core"},
    )

    subject_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    study_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.study.study_id"), nullable=False)
    local_id: Mapped[str] = mapped_column(Text, nullable=False)
    host_taxid: Mapped[Optional[int]] = mapped_column(Integer)
    pen: Mapped[Optional[str]] = mapped_column(Text)
    litter: Mapped[Optional[str]] = mapped_column(Text)
    ingest_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Sample(Base):
    """One BioSample = one specimen (animal × site × time)."""
    __tablename__ = "sample"
    __table_args__ = (
        UniqueConstraint("biosample_acc", name="sample_biosample_acc_key"),
        UniqueConstraint("sra_sample_acc", name="sample_sra_sample_acc_key"),
        UniqueConstraint("local_id", name="sample_local_id_key"),
        CheckConstraint("num_nonnulls(biosample_acc, sra_sample_acc, local_id) >= 1", name="sample_has_key"),
        CheckConstraint("sample_type IN ('biological', 'negative_control', 'extraction_blank', 'mock_community')",
                        name="sample_type_check"),
        Index("sample_subject_id_idx", "subject_id"),
        {"schema": "core"},
    )

    sample_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    biosample_acc: Mapped[Optional[str]] = mapped_column(Text)
    sra_sample_acc: Mapped[Optional[str]] = mapped_column(Text)
    local_id: Mapped[Optional[str]] = mapped_column(Text)
    subject_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("core.subject.subject_id"))
    sample_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="biological")
    ingest_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class StudySample(Base):
    __tablename__ = "study_sample"
    __table_args__ = (
        Index("study_sample_sample_id_idx", "sample_id"),
        {"schema": "core"},
    )

    study_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.study.study_id"), primary_key=True)
    sample_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.sample.sample_id"), primary_key=True)
    ingest_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)


class Library(Base):
    """One SRA experiment (SRX) = one library prep. Library descriptors live here, as in SRA."""
    __tablename__ = "library"
    __table_args__ = (
        UniqueConstraint("experiment_acc", name="library_experiment_acc_key"),
        UniqueConstraint("local_id", name="library_local_id_key"),
        UniqueConstraint("library_id", "sample_id", name="library_id_sample_key"),
        CheckConstraint("experiment_acc IS NOT NULL OR local_id IS NOT NULL", name="library_has_key"),
        CheckConstraint("library_layout IN ('SINGLE','PAIRED')", name="library_layout_check"),
        Index("library_sample_id_idx", "sample_id"),
        {"schema": "core"},
    )

    library_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    experiment_acc: Mapped[Optional[str]] = mapped_column(Text)
    local_id: Mapped[Optional[str]] = mapped_column(Text)
    sample_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.sample.sample_id"), nullable=False)
    study_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.study.study_id"), nullable=False)
    library_name: Mapped[Optional[str]] = mapped_column(Text)
    library_strategy: Mapped[Optional[str]] = mapped_column(Text)
    library_source: Mapped[Optional[str]] = mapped_column(Text)
    library_selection: Mapped[Optional[str]] = mapped_column(Text)
    library_layout: Mapped[Optional[str]] = mapped_column(Text)
    platform: Mapped[Optional[str]] = mapped_column(Text)
    instrument_model: Mapped[Optional[str]] = mapped_column(Text)
    ingest_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Run(Base):
    """One SRA run (SRR). library_id is filled once SRA run info is loaded."""
    __tablename__ = "run"
    __table_args__ = (
        ForeignKeyConstraint(["library_id", "sample_id"], ["core.library.library_id", "core.library.sample_id"],
                             name="run_library_fk"),
        Index("run_sample_id_idx", "sample_id"),
        Index("run_study_id_idx", "study_id"),
        Index("run_library_id_idx", "library_id"),
        {"schema": "core"},
    )

    run_acc: Mapped[str] = mapped_column(Text, primary_key=True)
    sample_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.sample.sample_id"), nullable=False)
    study_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.study.study_id"), nullable=False)
    library_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    spots: Mapped[Optional[int]] = mapped_column(BigInteger)
    bases: Mapped[Optional[int]] = mapped_column(BigInteger)
    avg_read_len: Mapped[Optional[int]] = mapped_column(Integer)
    release_date: Mapped[Optional[date]] = mapped_column(Date)
    ingest_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Assertion(Base):
    __tablename__ = "assertion"
    __table_args__ = (
        CheckConstraint("num_nonnulls(study_id, subject_id, sample_id, library_id, run_acc, genome_acc, "
                        "experiment_id, doc_id) = 1", name="assertion_one_entity"),
        CheckConstraint("num_nonnulls(value_text, value_term, value_num) >= 1", name="assertion_has_value"),
        Index("assertion_study_field_idx", "study_id", "field"),
        Index("assertion_subject_field_idx", "subject_id", "field"),
        Index("assertion_sample_field_idx", "sample_id", "field"),
        Index("assertion_library_field_idx", "library_id", "field"),
        Index("assertion_run_field_idx", "run_acc", "field"),
        Index("assertion_genome_field_idx", "genome_acc", "field"),
        Index("assertion_experiment_field_idx", "experiment_id", "field"),
        Index("assertion_doc_field_idx", "doc_id", "field"),
        {"schema": "prov"},
    )

    assertion_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    study_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("core.study.study_id"))
    subject_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("core.subject.subject_id"))
    sample_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("core.sample.sample_id"))
    library_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("core.library.library_id"))
    run_acc: Mapped[Optional[str]] = mapped_column(Text, ForeignKey("core.run.run_acc"))
    genome_acc: Mapped[Optional[str]] = mapped_column(String, ForeignKey("ref.genome.accession"))
    experiment_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("core.experiment.experiment_id", name="assertion_experiment_id_fkey"))
    doc_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("rag.document.doc_id", name="assertion_doc_id_fkey"))
    field: Mapped[str] = mapped_column(Text, nullable=False)
    value_text: Mapped[Optional[str]] = mapped_column(Text)
    value_term: Mapped[Optional[str]] = mapped_column(Text)
    value_num: Mapped[Optional[float]] = mapped_column(Numeric)
    unit: Mapped[Optional[str]] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(EVIDENCE_TIER, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    ingest_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

# ── experiment layer (migration db644d6f60c7, design doc §5.6) ────────────────

DESIGN_TYPES = ("completely_randomized", "randomized_block", "factorial", "crossover",
                "repeated_measures", "observational")
EXPERIMENTAL_UNITS = ("animal", "pen", "litter", "room", "farm")
FACTOR_KINDS = ("treatment", "blocking", "time", "nuisance")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class Experiment(Base):
    """One designed comparison within a study; the experimental unit is what was randomised."""
    __tablename__ = "experiment"
    __table_args__ = (
        UniqueConstraint("study_id", "local_id", name="experiment_study_local_id_key"),
        UniqueConstraint("experiment_id", "study_id", name="experiment_id_study_key"),
        CheckConstraint(_in("design_type", DESIGN_TYPES), name="experiment_design_type_check"),
        CheckConstraint(_in("experimental_unit", EXPERIMENTAL_UNITS), name="experiment_unit_check"),
        {"schema": "core"},
    )

    experiment_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    study_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.study.study_id"), nullable=False)
    local_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(Text)
    design_type: Mapped[str] = mapped_column(Text, nullable=False)
    experimental_unit: Mapped[str] = mapped_column(Text, nullable=False)
    ingest_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Factor(Base):
    """diet, rep, block … `nested_in_factor_id`: block is nested in rep."""
    __tablename__ = "factor"
    __table_args__ = (
        ForeignKeyConstraint(["experiment_id", "study_id"], ["core.experiment.experiment_id",
                                                             "core.experiment.study_id"],
                             name="factor_experiment_fk"),
        UniqueConstraint("experiment_id", "name", name="factor_experiment_name_key"),
        UniqueConstraint("factor_id", "study_id", name="factor_id_study_key"),
        UniqueConstraint("factor_id", "experiment_id", name="factor_id_experiment_key"),
        UniqueConstraint("factor_id", "nested_in_factor_id", name="factor_id_nested_key"),
        ForeignKeyConstraint(["nested_in_factor_id", "experiment_id"], ["core.factor.factor_id",
                                                                      "core.factor.experiment_id"],
                             name="factor_nested_in_fk"),
        CheckConstraint("nested_in_factor_id IS DISTINCT FROM factor_id", name="factor_not_nested_in_itself"),
        CheckConstraint(_in("kind", FACTOR_KINDS), name="factor_kind_check"),
        {"schema": "core"},
    )

    factor_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    experiment_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    study_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    nested_in_factor_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    description: Mapped[Optional[str]] = mapped_column(Text)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    ingest_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class FactorLevel(Base):
    """A level of a factor. A nested level points at its parent level: block 2 of rep 1 ≠ block 2 of rep 3."""
    __tablename__ = "factor_level"
    __table_args__ = (
        UniqueConstraint("level_id", "factor_id", name="factor_level_id_factor_key"),
        ForeignKeyConstraint(["factor_id", "parent_factor_id"], ["core.factor.factor_id",
                                                                 "core.factor.nested_in_factor_id"],
                             name="factor_level_nesting_fk"),
        ForeignKeyConstraint(["parent_level_id", "parent_factor_id"], ["core.factor_level.level_id",
                                                                       "core.factor_level.factor_id"],
                             name="factor_level_parent_fk"),
        CheckConstraint("(parent_level_id IS NULL) = (parent_factor_id IS NULL)", name="factor_level_parent_pair"),
        Index("factor_level_top_label_key", "factor_id", "label", unique=True,
              postgresql_where=text("parent_level_id IS NULL")),
        Index("factor_level_nested_label_key", "factor_id", "parent_level_id", "label", unique=True,
              postgresql_where=text("parent_level_id IS NOT NULL")),
        {"schema": "core"},
    )

    level_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    factor_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    is_control: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    description: Mapped[Optional[str]] = mapped_column(Text)
    parent_factor_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    parent_level_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    ingest_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SubjectFactorLevel(Base):
    """Which level each animal received; the primary key allows one level per factor per animal."""
    __tablename__ = "subject_factor_level"
    __table_args__ = (
        ForeignKeyConstraint(["level_id", "factor_id"], ["core.factor_level.level_id",
                                                         "core.factor_level.factor_id"], name="sfl_level_fk"),
        ForeignKeyConstraint(["subject_id", "study_id"], ["core.subject.subject_id", "core.subject.study_id"],
                             name="sfl_subject_fk"),
        ForeignKeyConstraint(["factor_id", "study_id"], ["core.factor.factor_id", "core.factor.study_id"],
                             name="sfl_factor_fk"),
        Index("sfl_level_id_idx", "level_id"),
        {"schema": "core"},
    )

    subject_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    factor_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    level_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    study_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ingest_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ── papers (migration 08321dea2d44, design doc §5.7) ─────────────────────────

DOC_KINDS = ("bioproject_desc", "abstract", "protocol", "source_docs", "paper", "own_draft", "own_methods",
             "lab_note")
LINK_METHODS = ("paper_data_availability", "pgmd", "ncbi_elink", "manual")


class Document(Base):
    """A paper or other document, keyed as Europe PMC keys it: (source, ext_id); pmid/pmcid/doi are alternates."""
    __tablename__ = "document"
    __table_args__ = (
        UniqueConstraint("source", "ext_id", name="document_source_ext_id_key"),
        CheckConstraint(_in("kind", DOC_KINDS), name="document_kind_check"),
        CheckConstraint("visibility IN ('public', 'own', 'private')", name="document_visibility_check"),
        CheckConstraint("pmcid IS NULL OR pmcid ~ '^PMC[0-9]+$'", name="document_pmcid_format"),
        Index("document_pmid_key", "pmid", unique=True, postgresql_where=text("pmid IS NOT NULL")),
        Index("document_pmcid_key", "pmcid", unique=True, postgresql_where=text("pmcid IS NOT NULL")),
        Index("document_doi_key", text("lower(doi)"), unique=True, postgresql_where=text("doi IS NOT NULL")),
        {"schema": "rag"},
    )

    doc_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    ext_id: Mapped[str] = mapped_column(Text, nullable=False)
    pmid: Mapped[Optional[str]] = mapped_column(Text)
    pmcid: Mapped[Optional[str]] = mapped_column(Text)
    doi: Mapped[Optional[str]] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(Text, nullable=False, server_default="public")
    title: Mapped[Optional[str]] = mapped_column(Text)
    journal: Mapped[Optional[str]] = mapped_column(Text)
    pub_year: Mapped[Optional[int]] = mapped_column(Integer)
    license: Mapped[Optional[str]] = mapped_column(Text)
    is_open_access: Mapped[Optional[bool]] = mapped_column(Boolean)
    raw_uri: Mapped[Optional[str]] = mapped_column(Text)
    raw_sha256: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    ingest_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class StudyPublication(Base):
    """Study ↔ paper, one row per way the link was established."""
    __tablename__ = "study_publication"
    __table_args__ = (
        CheckConstraint(_in("link_method", LINK_METHODS), name="study_publication_link_method_check"),
        Index("study_publication_doc_id_idx", "doc_id"),
        {"schema": "core"},
    )

    study_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.study.study_id"), primary_key=True)
    doc_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("rag.document.doc_id"), primary_key=True)
    link_method: Mapped[str] = mapped_column(Text, primary_key=True)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    ingest_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
