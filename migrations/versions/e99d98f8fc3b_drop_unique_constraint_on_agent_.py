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


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(op.f("agent_profiles_name_key"), "agent_profiles", type_="unique")


def downgrade() -> None:
    """Downgrade schema."""
    op.create_unique_constraint(
        op.f("agent_profiles_name_key"),
        "agent_profiles",
        ["name"],
        postgresql_nulls_not_distinct=False,
    )
