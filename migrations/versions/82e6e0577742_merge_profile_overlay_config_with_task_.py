"""merge profile overlay config with task layouts

Revision ID: 82e6e0577742
Revises: f2a4c6e8b0d2, d035ebcab682
Create Date: 2026-09-02 11:03:49.911194

"""
from typing import Sequence, Union



# revision identifiers, used by Alembic.
revision: str = '82e6e0577742'
down_revision: Union[str, Sequence[str], None] = ('f2a4c6e8b0d2', 'd035ebcab682')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
