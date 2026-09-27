"""Add capacity-spill audit reasons and the project preferred provider.

Revision ID: a00000000035
Revises: a00000000034

The squashed baseline uses live metadata, so both changes are conditional.
Downgrade refuses existing capacity_spill rows rather than deleting audit history.
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000035"
down_revision = "a00000000034"
branch_labels = None
depends_on = None

_CHECK = "ck_task_reroutes_reason_code"
_OLD_REASONS = "reason_code IN ('provider_unavailable','operator_forced','operator_undo')"
_NEW_REASONS = (
    "reason_code IN ('provider_unavailable','capacity_spill','operator_forced','operator_undo')"
)


def _reason_check(bind) -> str | None:
    for check in sa.inspect(bind).get_check_constraints("task_reroutes"):
        if check["name"] == _CHECK:
            return check["sqltext"]
    return None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("projects")}
    if "preferred_provider" not in columns:
        op.add_column("projects", sa.Column("preferred_provider", sa.Text(), nullable=True))
    current = _reason_check(bind)
    if current is None or "capacity_spill" not in current:
        if current is not None:
            op.drop_constraint(_CHECK, "task_reroutes", type_="check")
        op.create_check_constraint(_CHECK, "task_reroutes", _NEW_REASONS)


def downgrade() -> None:
    bind = op.get_bind()
    current = _reason_check(bind)
    if current is None or "capacity_spill" in current:
        if current is not None:
            op.drop_constraint(_CHECK, "task_reroutes", type_="check")
        op.create_check_constraint(_CHECK, "task_reroutes", _OLD_REASONS)
    columns = {column["name"] for column in sa.inspect(bind).get_columns("projects")}
    if "preferred_provider" in columns:
        op.drop_column("projects", "preferred_provider")
