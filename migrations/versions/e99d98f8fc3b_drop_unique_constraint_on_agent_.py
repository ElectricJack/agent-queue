"""drop unique constraint on agent_profiles.name

Revision ID: e99d98f8fc3b
Revises: 60aa01bc1080
Create Date: 2026-04-26 06:22:29.850764

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e99d98f8fc3b"
down_revision: Union[str, Sequence[str], None] = "60aa01bc1080"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NAMING_CONVENTION = {"uq": "%(table_name)s_%(column_0_name)s_key"}


def upgrade() -> None:
    """Upgrade schema."""
    # batch_alter_table so SQLite (no native ALTER for constraints) goes
    # through copy-and-move; PostgreSQL still issues a plain DROP CONSTRAINT.
    # Naming convention so SQLite, where the original UniqueConstraint was
    # unnamed, reflects the same auto-name PG used (`agent_profiles_name_key`).
    with op.batch_alter_table(
        "agent_profiles", naming_convention=_NAMING_CONVENTION
    ) as batch_op:
        batch_op.drop_constraint("agent_profiles_name_key", type_="unique")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table(
        "agent_profiles", naming_convention=_NAMING_CONVENTION
    ) as batch_op:
        batch_op.create_unique_constraint(
            "agent_profiles_name_key",
            ["name"],
            postgresql_nulls_not_distinct=False,
        )
