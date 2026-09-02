"""merge task layouts with main migrations

Revision ID: d035ebcab682
Revises: d1e2f3a4b5c6, e1b7c2a94d38
Create Date: 2026-09-02 10:49:55.597513

"""
from typing import Sequence, Union



# revision identifiers, used by Alembic.
revision: str = 'd035ebcab682'
down_revision: Union[str, Sequence[str], None] = ('d1e2f3a4b5c6', 'e1b7c2a94d38')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
