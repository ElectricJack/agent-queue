"""add allow_base_checkout column to agent_profiles

Revision ID: d7f4c1a9b2e8
Revises: 33bdb059ceff
Create Date: 2026-09-01 12:00:00.000000

OPERATIONAL NOTE
================

Adds the ``allow_base_checkout`` boolean column to ``agent_profiles``
(default False).  It is the opt-in for the base-checkout launch guard:
a session whose ``work_dir`` is a *base* workspace — the clone that hosts
a kind's slot worktrees, routinely a human's own working tree — is
refused unless its profile sets this.

No shipped profile sets it, so the default False is the correct value for
every existing row.

Idempotent: skips ``add_column`` when the column is already present.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d7f4c1a9b2e8"
down_revision: Union[str, Sequence[str], None] = "33bdb059ceff"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("agent_profiles")}
    if "allow_base_checkout" in existing_columns:
        return
    with op.batch_alter_table("agent_profiles", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "allow_base_checkout",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("agent_profiles")}
    if "allow_base_checkout" not in existing_columns:
        return
    with op.batch_alter_table("agent_profiles", schema=None) as batch_op:
        batch_op.drop_column("allow_base_checkout")
