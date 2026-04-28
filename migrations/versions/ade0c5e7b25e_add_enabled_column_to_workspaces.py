"""add enabled column to workspaces

Revision ID: ade0c5e7b25e
Revises: e99d98f8fc3b
Create Date: 2026-04-28 14:21:27.364526

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ade0c5e7b25e'
down_revision: Union[str, Sequence[str], None] = 'e99d98f8fc3b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "workspaces",
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("workspaces", "enabled")
