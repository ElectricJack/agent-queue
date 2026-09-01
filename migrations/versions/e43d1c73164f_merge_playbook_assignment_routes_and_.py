"""merge playbook assignment routes and integration_mode heads

Revision ID: e43d1c73164f
Revises: a7c91e4d2b63, c4d5e6f7a8b9
Create Date: 2026-09-01 12:05:31.025311

"""
from typing import Sequence, Union



# revision identifiers, used by Alembic.
revision: str = 'e43d1c73164f'
down_revision: Union[str, Sequence[str], None] = ('a7c91e4d2b63', 'c4d5e6f7a8b9')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
