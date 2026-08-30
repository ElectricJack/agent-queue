"""soft delete durable agents

Revision ID: 882b77dc8495
Revises: 9b5e9f057e6e
Create Date: 2026-08-30 13:24:25.850261

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "882b77dc8495"
down_revision: Union[str, Sequence[str], None] = "9b5e9f057e6e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add a tombstone without rewriting existing identities or history."""
    with op.batch_alter_table("agents") as batch_op:
        batch_op.add_column(sa.Column("deleted_at", sa.Float(), nullable=True))


def downgrade() -> None:
    """Drop the tombstone; deleted workers remain disabled and history stays."""
    bind = op.get_bind()
    # SQLite rebuilds the referenced agents table to drop a column. Suspend
    # enforcement outside a transaction and restore the connection's setting.
    foreign_keys = (
        bind.dialect.name == "sqlite" and bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
    )
    if foreign_keys:
        with op.get_context().autocommit_block():
            bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table("agents") as batch_op:
            batch_op.drop_column("deleted_at")
    finally:
        if foreign_keys:
            with op.get_context().autocommit_block():
                bind.exec_driver_sql("PRAGMA foreign_keys=ON")
