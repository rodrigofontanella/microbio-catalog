"""findings: test names 't-test' and 'Kruskal-Wallis'

Revision ID: a8f4d5f5fd45
Revises: 9dbf58ea10c0
Create Date: 2026-11-10 15:00:00

What study 3 (PRJNA430317, PMC7568366) needs (decided 2026-11-10, Rodrigo; handoff 2026-11-10 "Findings"):
Student's t-test for the growth traits and Kruskal-Wallis for alpha diversity and taxa. Neither test prints a
statistic in the paper, so no (test, test_stat_kind) pair is added: finding_test_stat_fits_test still refuses an
F or pseudo-F on either, and both stand alone the way LSD does.

Only finding_test_check changes (same name, wider list). Downgrade restores the narrower check, which refuses
while any finding uses either test: delete those first (a downgrade must not silently drop curated findings).
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'a8f4d5f5fd45'
down_revision: Union[str, Sequence[str], None] = '9dbf58ea10c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# frozen here, not imported from app.models: a migration describes the schema at its own revision
OLD_TESTS = ("GLM", "GLMM", "PERMANOVA", "LSD")
TESTS = OLD_TESTS + ("t-test", "Kruskal-Wallis")


def _check(tests: tuple[str, ...]) -> str:
    return f"test IS NULL OR test IN ({', '.join(repr(t) for t in tests)})"


def _replace(tests: tuple[str, ...]) -> None:
    op.drop_constraint("finding_test_check", "finding", schema="core", type_="check")
    op.create_check_constraint("finding_test_check", "finding", _check(tests), schema="core")


def upgrade() -> None:
    _replace(TESTS)


def downgrade() -> None:
    _replace(OLD_TESTS)
