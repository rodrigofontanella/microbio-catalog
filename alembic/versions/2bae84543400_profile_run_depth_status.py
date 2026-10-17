"""profile_run depth: thresholds per row + depth_status generated from n_reads

Revision ID: 2bae84543400
Revises: 10b35d7ad632
Create Date: 2026-10-17 11:42:06

Depth belongs to a run *within a profile* (PGMD's reprocessed count is not a pipeline of our own's count), so the
flag lives on core.profile_run, not as an assertion on the run (decided 2026-10-17, Rodrigo). The thresholds are
recorded on every row (`depth_fail_below`, `depth_low_below`, from DEPTH_FAIL_BELOW / DEPTH_LOW_BELOW), and
`depth_status` is GENERATED from n_reads and those two, so a wrong flag is unstorable and can't drift. New
thresholds = updating the two numbers; the status recomputes.

It is a flag, never a filter: in PRJNA664026, 12 of the 14 runs under 5,000 reads are Antibiotic, so dropping
them would bias the treatment comparison. Downstream use reports its loss per group (app.reports.depth).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '2bae84543400'
down_revision: Union[str, Sequence[str], None] = '10b35d7ad632'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEPTH_STATUS_SQL = ("CASE WHEN n_reads IS NULL OR depth_fail_below IS NULL OR depth_low_below IS NULL THEN NULL "
                    "WHEN n_reads < depth_fail_below THEN 'fail' "
                    "WHEN n_reads < depth_low_below THEN 'low' ELSE 'ok' END")


def upgrade() -> None:
    op.add_column("profile_run", sa.Column("depth_fail_below", sa.Integer), schema="core")
    op.add_column("profile_run", sa.Column("depth_low_below", sa.Integer), schema="core")
    op.add_column("profile_run", sa.Column("depth_status", sa.Text, sa.Computed(DEPTH_STATUS_SQL, persisted=True)),
                  schema="core")
    op.create_check_constraint("profile_run_depth_pair", "profile_run",
                               "(depth_fail_below IS NULL) = (depth_low_below IS NULL)", schema="core")
    op.create_check_constraint("profile_run_depth_order", "profile_run",
                               "depth_fail_below IS NULL OR (depth_fail_below > 0 AND depth_low_below >= depth_fail_below)",
                               schema="core")
    op.create_check_constraint("profile_run_depth_needs_reads", "profile_run",
                               "depth_fail_below IS NULL OR n_reads IS NOT NULL", schema="core")


def downgrade() -> None:
    op.drop_constraint("profile_run_depth_needs_reads", "profile_run", schema="core", type_="check")
    op.drop_constraint("profile_run_depth_order", "profile_run", schema="core", type_="check")
    op.drop_constraint("profile_run_depth_pair", "profile_run", schema="core", type_="check")
    op.drop_column("profile_run", "depth_status", schema="core")
    op.drop_column("profile_run", "depth_low_below", schema="core")
    op.drop_column("profile_run", "depth_fail_below", schema="core")
