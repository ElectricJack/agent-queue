"""add description column to task_dependencies

Dependency edges between a spawning task and the task it spawned carry a
free-text ``description`` — the *why* behind the edge (the reason the new
task exists), not just the link type. Nullable: pre-existing edges and
edges written by paths with nothing to say stay NULL.

Revision ID: e7a2b9c41d05
Revises: 882b77dc8495
Create Date: 2026-08-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7a2b9c41d05'
down_revision: Union[str, Sequence[str], None] = '882b77dc8495'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "task_dependencies",
        sa.Column("description", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("task_dependencies", "description")
