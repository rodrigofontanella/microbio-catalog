"""finding_check: a finding tested against a profile of the same study (tier 'derived')

Revision ID: 6c1f0e2a9b47
Revises: d2362d368390
Create Date: 2026-11-10 17:40:00

Decided 2026-11-10 (Rodrigo; handoff-2026-11-10-paper-vs-profile, design doc §5.8.3), after the first paper-vs-
profile report (study 3, `b8718a4`):
  - the verdicts are stored, tier **derived** (§4: deterministic computation from data; the human part, the signed
    claim map, is named in evidence by path and sha256), not 'inferred' (an LLM or heuristic reading of text);
  - in **their own table**, not in core.finding (curated; its loader stops on any change) and not as assertions
    (the numbers must be queryable: "findings reproduced at q < 0.05 by any profile");
  - **every scenario** (all, no_fail, ok_only) and every lineage (the claim's own, `role = verdict`; the homonym
    lineages, `role = also`), including `n_a`, `same_as_all` and `not_testable` rows: a missing row never means
    'not run';
  - written by a loader that recomputes (app.ingest.finding_checks), never from the report's TSV.

Rules a check row can't break (each has its own constraint, proven by data/constraint_test_m9.sql):
  - the finding and the profile belong to the same study (composite FKs; core.finding gains UNIQUE (finding_id,
    study_id) as their target, like profile_id_study_key);
  - a tested verdict has p, q and a direction; an untested one has none; p and q are probabilities;
  - only a `verdict` row carries a verdict, `also` rows are `no_verdict` when tested; `n_a` and `same_as_all` only in
    a sensitivity scenario; a row without a lineage is the map's own `not_testable`; every `not_testable` says why;
  - the six count columns come together, present <= n, and every tested row has them.
Not checkable here (the loader's job): that a verdict agrees with the finding's direction.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '6c1f0e2a9b47'
down_revision: Union[str, Sequence[str], None] = 'd2362d368390'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# frozen here, not imported from app.models: a migration describes the schema at its own revision
SCENARIOS = ("all", "no_fail", "ok_only")
ROLES = ("verdict", "also")
TESTED_VERDICTS = ("reproduced", "direction_only", "opposite_ns", "contradicted", "no_direction", "consistent_null",
                   "no_verdict")
VERDICTS = TESTED_VERDICTS + ("not_testable", "n_a", "same_as_all")
DIRECTIONS = ("increase", "decrease", "none")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_unique_constraint("finding_id_study_key", "finding", ["finding_id", "study_id"], schema="core")
    tier = postgresql.ENUM(name="evidence_tier", schema="prov", create_type=False)
    op.create_table(
        "finding_check",
        sa.Column("finding_check_id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("finding_id", sa.BigInteger(), nullable=False),
        sa.Column("study_id", sa.BigInteger(), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), nullable=False),
        sa.Column("scenario", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text()),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("n_t", sa.Integer()),
        sa.Column("n_r", sa.Integer()),
        sa.Column("present_t", sa.Integer()),
        sa.Column("present_r", sa.Integer()),
        sa.Column("median_t", postgresql.DOUBLE_PRECISION()),
        sa.Column("median_r", postgresql.DOUBLE_PRECISION()),
        sa.Column("direction", sa.Text()),
        sa.Column("p", postgresql.DOUBLE_PRECISION()),
        sa.Column("q", postgresql.DOUBLE_PRECISION()),
        sa.Column("tier", tier, nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("ingest_run_id", sa.BigInteger(), sa.ForeignKey("prov.ingest_run.ingest_run_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["finding_id", "study_id"], ["core.finding.finding_id", "core.finding.study_id"],
                                name="finding_check_finding_fk"),
        sa.ForeignKeyConstraint(["profile_id", "study_id"], ["core.profile.profile_id", "core.profile.study_id"],
                                name="finding_check_profile_fk"),
        sa.UniqueConstraint("finding_id", "profile_id", "scenario", "role", "unit", name="finding_check_key",
                            postgresql_nulls_not_distinct=True),
        sa.CheckConstraint(_in("scenario", SCENARIOS), name="finding_check_scenario_check"),
        sa.CheckConstraint(_in("role", ROLES), name="finding_check_role_check"),
        sa.CheckConstraint(_in("verdict", VERDICTS), name="finding_check_verdict_check"),
        sa.CheckConstraint(f"direction IS NULL OR {_in('direction', DIRECTIONS)}", name="finding_check_direction_check"),
        sa.CheckConstraint("(p IS NULL) = (q IS NULL)", name="finding_check_p_q_pair"),
        sa.CheckConstraint("p IS NULL OR (p >= 0 AND p <= 1 AND q >= 0 AND q <= 1)", name="finding_check_p_q_range"),
        sa.CheckConstraint("(direction IS NULL) = (q IS NULL)", name="finding_check_direction_pair"),
        sa.CheckConstraint(f"({_in('verdict', TESTED_VERDICTS)}) = (q IS NOT NULL)", name="finding_check_tested_has_q"),
        sa.CheckConstraint("role = 'also' OR verdict <> 'no_verdict'", name="finding_check_verdict_role"),
        sa.CheckConstraint("role = 'verdict' OR verdict IN ('no_verdict', 'not_testable', 'n_a', 'same_as_all')",
                           name="finding_check_also_no_verdict"),
        sa.CheckConstraint("scenario <> 'all' OR verdict NOT IN ('n_a', 'same_as_all')",
                           name="finding_check_sensitivity_only"),
        sa.CheckConstraint("unit IS NOT NULL OR (role = 'verdict' AND verdict = 'not_testable')",
                           name="finding_check_unit"),
        sa.CheckConstraint("verdict <> 'not_testable' OR reason IS NOT NULL", name="finding_check_reason"),
        sa.CheckConstraint("num_nonnulls(n_t, n_r, present_t, present_r, median_t, median_r) IN (0, 6)",
                           name="finding_check_counts_complete"),
        sa.CheckConstraint("n_t IS NULL OR (present_t BETWEEN 0 AND n_t AND present_r BETWEEN 0 AND n_r)",
                           name="finding_check_counts_order"),
        sa.CheckConstraint("q IS NULL OR n_t IS NOT NULL", name="finding_check_tested_has_counts"),
        schema="core",
    )
    op.create_index("finding_check_profile_idx", "finding_check", ["profile_id"], schema="core")


def downgrade() -> None:
    # drops every stored check: they are derived and reproducible by re-running app.ingest.finding_checks
    op.drop_index("finding_check_profile_idx", table_name="finding_check", schema="core")
    op.drop_table("finding_check", schema="core")
    op.drop_constraint("finding_id_study_key", "finding", schema="core", type_="unique")
