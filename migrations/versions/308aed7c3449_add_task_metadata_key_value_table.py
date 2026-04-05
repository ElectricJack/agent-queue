"""add task_metadata key-value table

Revision ID: 308aed7c3449
Revises: 311e98c39ffa
Create Date: 2026-04-04 21:03:11.763356

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "308aed7c3449"
down_revision: Union[str, Sequence[str], None] = "311e98c39ffa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "task_metadata",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("task_id", "key"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("task_metadata")
