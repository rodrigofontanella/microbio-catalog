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


# ── findings (migration 284f0d1b9c5d, design doc §5.8) ───────────────────────

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


class FeatureSet(Base):
    """A named set of features a finding can be about: '200 most abundant OTUs' in caecal digesta."""
    __tablename__ = "feature_set"
    __table_args__ = (
        UniqueConstraint("study_id", "local_id", name="feature_set_study_local_id_key"),
        UniqueConstraint("feature_set_id", "study_id", name="feature_set_id_study_key"),
        CheckConstraint(_in("member_kind", FEATURE_KINDS), name="feature_set_member_kind_check"),
        CheckConstraint(f"material IS NULL OR {_in('material', MATERIALS)}", name="feature_set_material_check"),
        CheckConstraint("size IS NULL OR size > 0", name="feature_set_size_check"),
        {"schema": "core"},
    )

    feature_set_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    study_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.study.study_id"), nullable=False)
    local_id: Mapped[str] = mapped_column(Text, nullable=False)
    member_kind: Mapped[str] = mapped_column(Text, nullable=False)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[Optional[int]] = mapped_column(Integer)
    site_term: Mapped[Optional[str]] = mapped_column(Text)
    material: Mapped[Optional[str]] = mapped_column(Text)
    otu_dataset: Mapped[Optional[str]] = mapped_column(Text)
    ingest_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Finding(Base):
    """What a paper claims about one feature or one feature set under one contrast, with its evidence."""
    __tablename__ = "finding"
    __table_args__ = (
        UniqueConstraint("study_id", "local_id", name="finding_study_local_id_key"),
        UniqueConstraint("finding_id", "factor_id", name="finding_id_factor_key"),
        ForeignKeyConstraint(["experiment_id", "study_id"], ["core.experiment.experiment_id",
                                                             "core.experiment.study_id"],
                             name="finding_experiment_fk"),
        ForeignKeyConstraint(["factor_id", "experiment_id"], ["core.factor.factor_id", "core.factor.experiment_id"],
                             name="finding_factor_fk"),
        ForeignKeyConstraint(["treatment_level_id", "factor_id"], ["core.factor_level.level_id",
                                                                   "core.factor_level.factor_id"],
                             name="finding_treatment_level_fk"),
        ForeignKeyConstraint(["reference_level_id", "factor_id"], ["core.factor_level.level_id",
                                                                   "core.factor_level.factor_id"],
                             name="finding_reference_level_fk"),
        ForeignKeyConstraint(["feature_set_id", "study_id"], ["core.feature_set.feature_set_id",
                                                              "core.feature_set.study_id"],
                             name="finding_feature_set_fk"),
        CheckConstraint(_in("contrast_kind", CONTRAST_KINDS), name="finding_contrast_kind_check"),
        CheckConstraint("(contrast_kind = 'pairwise') = (treatment_level_id IS NOT NULL "
                        "AND reference_level_id IS NOT NULL)", name="finding_pairwise_levels"),
        CheckConstraint("contrast_kind = 'pairwise' OR (treatment_level_id IS NULL "
                        "AND reference_level_id IS NULL)", name="finding_levels_only_pairwise"),
        CheckConstraint("treatment_level_id <> reference_level_id",
                        name="finding_treatment_not_reference"),
        CheckConstraint(f"material IS NULL OR {_in('material', MATERIALS)}", name="finding_material_check"),
        CheckConstraint(_in("measure", MEASURES), name="finding_measure_check"),
        CheckConstraint("(feature_label IS NULL) <> (feature_set_id IS NULL)", name="finding_about_one"),
        CheckConstraint("(feature_kind IS NULL) = (feature_label IS NULL)", name="finding_feature_kind_pair"),
        CheckConstraint(f"feature_kind IS NULL OR {_in('feature_kind', FEATURE_KINDS)}",
                        name="finding_feature_kind_check"),
        CheckConstraint(_in("direction", DIRECTIONS), name="finding_direction_check"),
        CheckConstraint("(direction = 'counts') = (n_increase IS NOT NULL OR n_decrease IS NOT NULL)",
                        name="finding_counts_pair"),
        CheckConstraint("direction <> 'counts' OR feature_set_id IS NOT NULL", name="finding_counts_on_set"),
        CheckConstraint("num_nonnulls(stat_kind, stat_op, stat_value, stat_scope, stat_source) IN (0, 5)",
                        name="finding_stat_complete"),
        CheckConstraint(f"stat_kind IS NULL OR {_in('stat_kind', STAT_KINDS)}", name="finding_stat_kind_check"),
        CheckConstraint(f"stat_op IS NULL OR {_in('stat_op', STAT_OPS)}", name="finding_stat_op_check"),
        CheckConstraint(f"stat_scope IS NULL OR {_in('stat_scope', STAT_SCOPES)}", name="finding_stat_scope_check"),
        CheckConstraint(f"stat_source IS NULL OR {_in('stat_source', STAT_SOURCES)}",
                        name="finding_stat_source_check"),
        CheckConstraint("(effect_value IS NULL) = (effect_kind IS NULL)", name="finding_effect_pair"),
        Index("finding_contrast_idx", "treatment_level_id", "reference_level_id"),
        Index("finding_feature_label_idx", "feature_label"),
        Index("finding_feature_set_idx", "feature_set_id"),
        Index("finding_doc_idx", "doc_id"),
        {"schema": "core"},
    )

    finding_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    study_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    experiment_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    doc_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("rag.document.doc_id"), nullable=False)
    local_id: Mapped[str] = mapped_column(Text, nullable=False)
    contrast_kind: Mapped[str] = mapped_column(Text, nullable=False)
    factor_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    treatment_level_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    reference_level_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    site_term: Mapped[str] = mapped_column(Text, nullable=False)
    material: Mapped[Optional[str]] = mapped_column(Text)
    measure: Mapped[str] = mapped_column(Text, nullable=False)
    feature_kind: Mapped[Optional[str]] = mapped_column(Text)
    feature_label: Mapped[Optional[str]] = mapped_column(Text)
    feature_rank: Mapped[Optional[str]] = mapped_column(Text)
    otu_dataset: Mapped[Optional[str]] = mapped_column(Text)
    taxonomy: Mapped[Optional[str]] = mapped_column(Text)
    feature_set_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    n_increase: Mapped[Optional[int]] = mapped_column(Integer)
    n_decrease: Mapped[Optional[int]] = mapped_column(Integer)
    stat_kind: Mapped[Optional[str]] = mapped_column(Text)
    stat_op: Mapped[Optional[str]] = mapped_column(Text)
    stat_value: Mapped[Optional[float]] = mapped_column(Numeric)
    stat_scope: Mapped[Optional[str]] = mapped_column(Text)
    stat_source: Mapped[Optional[str]] = mapped_column(Text)
    effect_kind: Mapped[Optional[str]] = mapped_column(Text)
    effect_value: Mapped[Optional[float]] = mapped_column(Numeric)
    effect_se: Mapped[Optional[float]] = mapped_column(Numeric)
    figure: Mapped[Optional[str]] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(EVIDENCE_TIER, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    ingest_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class FindingLevel(Base):
    """The levels of an omnibus test (member) or a grouping (higher / lower); pairwise uses Finding's columns."""
    __tablename__ = "finding_level"
    __table_args__ = (
        ForeignKeyConstraint(["finding_id", "factor_id"], ["core.finding.finding_id", "core.finding.factor_id"],
                             name="finding_level_finding_fk"),
        ForeignKeyConstraint(["level_id", "factor_id"], ["core.factor_level.level_id", "core.factor_level.factor_id"],
                             name="finding_level_level_fk"),
        CheckConstraint(_in("role", LEVEL_ROLES), name="finding_level_role_check"),
        Index("finding_level_level_idx", "level_id"),
        {"schema": "core"},
    )

    finding_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    level_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    factor_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    ingest_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False)
