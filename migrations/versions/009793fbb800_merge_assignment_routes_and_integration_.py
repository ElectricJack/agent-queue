"""merge assignment_routes and integration_mode heads

Revision ID: 009793fbb800
Revises: a7c91e4d2b63, c4d5e6f7a8b9
Create Date: 2026-09-01 12:18:22.915036

"""
from typing import Sequence, Union



# revision identifiers, used by Alembic.
revision: str = '009793fbb800'
down_revision: Union[str, Sequence[str], None] = ('a7c91e4d2b63', 'c4d5e6f7a8b9')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
