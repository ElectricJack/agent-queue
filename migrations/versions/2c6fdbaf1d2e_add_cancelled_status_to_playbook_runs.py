"""add cancelled status to playbook_runs

Revision ID: 2c6fdbaf1d2e
Revises: 0c466030a9da
Create Date: 2026-08-22 17:10:14.775993

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '2c6fdbaf1d2e'
down_revision: Union[str, Sequence[str], None] = '0c466030a9da'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('playbook_runs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_playbook_runs_status', type_='check')
        batch_op.create_check_constraint(
            'ck_playbook_runs_status',
            "status IN ('running', 'paused', 'completed', 'failed', 'timed_out', 'cancelled')",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('playbook_runs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_playbook_runs_status', type_='check')
        batch_op.create_check_constraint(
            'ck_playbook_runs_status',
            "status IN ('running', 'paused', 'completed', 'failed', 'timed_out')",
        )
