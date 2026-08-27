"""sessions.desired_state

Revision ID: 4e925610d7a6
Revises: 2ea52ac3da6c
Create Date: 2026-08-27 01:38:02.850966

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4e925610d7a6'
down_revision: Union[str, Sequence[str], None] = '2ea52ac3da6c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("desired_state", sa.Text(), server_default="running", nullable=False)
        )

    # Backfill.  The server_default marks every existing row as *wanted*,
    # including the dead ones -- so without this the first reconciler tick
    # after the runtime is enabled would try to resurrect every stopped and
    # quarantined session in the table.  Intent for history is "stopped".
    op.execute(
        "UPDATE sessions SET desired_state = 'stopped' "
        "WHERE state IN ('stopped', 'quarantined')"
    )
    op.execute("UPDATE sessions SET desired_state = 'sleeping' WHERE state = 'sleeping'")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_column("desired_state")
