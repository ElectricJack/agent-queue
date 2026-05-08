"""merge workspaces-v2 and chat-analyzer suppressed_by heads

Revision ID: e252a41eb210
Revises: 7cdb4618fd0b, df84af990b86
Create Date: 2026-05-08 15:43:52.885322

"""
from typing import Sequence, Union



# revision identifiers, used by Alembic.
revision: str = 'e252a41eb210'
down_revision: Union[str, Sequence[str], None] = ('7cdb4618fd0b', 'df84af990b86')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
