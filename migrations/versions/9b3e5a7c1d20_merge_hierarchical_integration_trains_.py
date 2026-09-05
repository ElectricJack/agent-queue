"""merge hierarchical integration trains with main

Revision ID: 9b3e5a7c1d20
Revises: 4e7d1c9b2a55, d8e9f0a1b2c3
Create Date: 2026-09-05 13:22:37.594987

"""
from typing import Sequence, Union



# revision identifiers, used by Alembic.
revision: str = '9b3e5a7c1d20'
down_revision: Union[str, Sequence[str], None] = ('4e7d1c9b2a55', 'd8e9f0a1b2c3')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
