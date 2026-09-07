"""integration schedule catchup policy

Revision ID: ed46f4aec7be
Revises: e9b2f1b7c3d5
Create Date: 2026-09-05 23:05:16.782246

"""
import importlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ed46f4aec7be"
down_revision: str | Sequence[str] | None = "e9b2f1b7c3d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base_guards():
    return importlib.import_module(
        "migrations.versions.3f30b34c7e7c_hierarchical_integration_state"
    )


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("project_integration_schedules") as batch_op:
        batch_op.add_column(sa.Column("catchup_trigger", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("catchup_requested_at", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("catchup_after_sequence", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            "ck_project_integration_schedules_catchup",
            "(catchup_trigger IS NULL AND catchup_requested_at IS NULL AND "
            "catchup_after_sequence IS NULL) OR "
            "(catchup_trigger IN ('periodic', 'manual') AND "
            "catchup_requested_at IS NOT NULL AND catchup_after_sequence IS NOT NULL "
            "AND catchup_after_sequence >= 0)",
        )
    _base_guards()._recreate_sqlite_guards("project_integration_schedules")


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    project_id = conn.execute(
        sa.text(
            "SELECT project_id FROM project_integration_schedules "
            "WHERE catchup_trigger IS NOT NULL OR catchup_requested_at IS NOT NULL "
            "OR catchup_after_sequence IS NOT NULL ORDER BY project_id LIMIT 1"
        )
    ).scalar_one_or_none()
    if project_id is not None:
        raise RuntimeError(
            "cannot downgrade integration schedule catch-up policy while project "
            f"{project_id!r} has live catch-up state"
        )
    with op.batch_alter_table("project_integration_schedules") as batch_op:
        batch_op.drop_constraint(
            "ck_project_integration_schedules_catchup", type_="check"
        )
        batch_op.drop_column("catchup_after_sequence")
        batch_op.drop_column("catchup_requested_at")
        batch_op.drop_column("catchup_trigger")
    _base_guards()._recreate_sqlite_guards("project_integration_schedules")
