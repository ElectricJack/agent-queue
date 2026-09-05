"""merge perf indexes with integration trains

Revision ID: f02a4a4a3010
Revises: 806ad9f97451, a7c4d9e2106b
Create Date: 2026-09-04 22:26:54.860661

"""
from typing import Sequence, Union



# revision identifiers, used by Alembic.
revision: str = 'f02a4a4a3010'
down_revision: Union[str, Sequence[str], None] = ('806ad9f97451', 'a7c4d9e2106b')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
