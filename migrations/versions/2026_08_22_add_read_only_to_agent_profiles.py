"""add read_only column to agent_profiles

Revision ID: c5e2b7a9d3f4
Revises: 0ff97e8792f8
Create Date: 2026-08-22 11:00:00.000000

OPERATIONAL NOTE
================

Adds the ``read_only`` boolean column to ``agent_profiles`` (default
False).  Consumed by workspace acquisition to refuse an exclusive lock
on the mutable ``project-repo`` kind when the resolved profile is
read-only (T3 reviewer follow-up).

Idempotent: skips ``add_column`` when the column is already present.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c5e2b7a9d3f4"
down_revision: Union[str, Sequence[str], None] = "0ff97e8792f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("agent_profiles")}
    if "read_only" in existing_columns:
        return
    with op.batch_alter_table("agent_profiles", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "read_only",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("agent_profiles")}
    if "read_only" not in existing_columns:
        return
    with op.batch_alter_table("agent_profiles", schema=None) as batch_op:
        batch_op.drop_column("read_only")
