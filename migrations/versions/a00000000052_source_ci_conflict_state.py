"""Allow the ``conflict`` source CI state.

GitHub runs no ``pull_request`` workflow for a PR whose head conflicts with
its base, so such a source never acquires checks. ``conflict`` records that
fact so batch conflict scope can admit it. See
docs/superpowers/specs/2026-09-30-continuous-delivery-design.md.

Revision ID: a00000000052
Revises: a00000000051
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000052"
down_revision = "a00000000051"
branch_labels = None
depends_on = None

_NAME = "ck_integration_source_ci_state"
_TABLE = "integration_source_ci"
_NEW = "state IN ('green', 'red', 'cancelled', 'pending', 'conflict')"
_OLD = "state IN ('green', 'red', 'cancelled', 'pending')"


def _replace(sqltext: str) -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    constraints = sa.inspect(bind).get_check_constraints(_TABLE)
    current = next((item for item in constraints if item["name"] == _NAME), None)
    if current is not None and ("conflict" in current["sqltext"]) == ("conflict" in sqltext):
        return
    if current is not None:
        op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(_NAME, _TABLE, sqltext)


def upgrade() -> None:
    _replace(_NEW)


def downgrade() -> None:
    op.execute(sa.text(f"UPDATE {_TABLE} SET state = 'pending' WHERE state = 'conflict'"))
    _replace(_OLD)
