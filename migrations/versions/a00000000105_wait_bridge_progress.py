"""Dedicated resumable legacy wait migration receipts; preserve on rollback."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from alembic import op

revision = 'a00000000105'
down_revision = 'a00000000104'
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table('wait_bridge_progress'):
        return
    op.create_table(
        'wait_bridge_progress',
        sa.Column('project_id', sa.Text(), sa.ForeignKey('projects.id'), primary_key=True),
        sa.Column('phase', sa.Text(), primary_key=True),
        sa.Column('identity', sa.Text(), primary_key=True),
        sa.Column('state', sa.Text(), nullable=False),
        sa.Column('evidence', JSONB(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.CheckConstraint("phase IN ('project','agent-waits','audit','sessionless')",
                           name='ck_wait_bridge_progress_phase'),
        sa.CheckConstraint("state IN ('pending','complete','held')",
                           name='ck_wait_bridge_progress_state'),
    )


def downgrade():
    # A rollback must not forget adopted identities or restart a completed pass.
    pass
