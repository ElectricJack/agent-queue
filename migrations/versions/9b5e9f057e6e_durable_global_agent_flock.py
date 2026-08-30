"""durable global agent flock

Revision ID: 9b5e9f057e6e
Revises: 5f37c424acde
Create Date: 2026-08-30 12:43:23.890723

"""

from contextlib import contextmanager
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9b5e9f057e6e"
down_revision: Union[str, Sequence[str], None] = "5f37c424acde"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


@contextmanager
def _sqlite_rebuild():
    """Preserve references while SQLite rebuilds the referenced agents table."""
    bind = op.get_bind()
    foreign_keys = (
        bind.dialect.name == "sqlite" and bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
    )
    if foreign_keys:
        with op.get_context().autocommit_block():
            bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        yield
    finally:
        if foreign_keys:
            with op.get_context().autocommit_block():
                bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def upgrade() -> None:
    with _sqlite_rebuild():
        _upgrade()


def downgrade() -> None:
    with _sqlite_rebuild():
        _downgrade()


def _upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.add_column(sa.Column("role", sa.Text(), server_default="worker", nullable=False))
        batch_op.add_column(
            sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False)
        )
        batch_op.add_column(sa.Column("harness", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("model", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("intelligence_class", sa.Text(), nullable=True))

    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("llm_provider", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("model", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("intelligence_class", sa.Text(), nullable=True))
        batch_op.create_index("idx_sessions_agent", ["agent_id", "state"], unique=False)

    # Only recover a link whose current assignment agrees in both directions.
    # Historical sessions with unknown owners retain NULL rather than being guessed.
    op.execute(
        sa.text("""
        UPDATE sessions SET agent_id = (
            SELECT tasks.assigned_agent_id FROM tasks
            JOIN agents ON agents.id = tasks.assigned_agent_id
                AND agents.current_task_id = tasks.id
            WHERE tasks.id = sessions.task_id
        )
        WHERE agent_id IS NULL AND state IN ('starting', 'running', 'draining')
            AND EXISTS (
                SELECT 1 FROM tasks JOIN agents ON agents.id = tasks.assigned_agent_id
                    AND agents.current_task_id = tasks.id
                WHERE tasks.id = sessions.task_id
            )
    """)
    )


def _downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_index("idx_sessions_agent")
        batch_op.drop_column("intelligence_class")
        batch_op.drop_column("model")
        batch_op.drop_column("llm_provider")

    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.drop_column("intelligence_class")
        batch_op.drop_column("model")
        batch_op.drop_column("harness")
        batch_op.drop_column("enabled")
        batch_op.drop_column("role")
