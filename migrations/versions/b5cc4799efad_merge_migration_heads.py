"""merge migration heads

Revision ID: b5cc4799efad
Revises: 4599a1026fdf, f322b4dc379d
Create Date: 2026-04-09 19:54:59.187999

"""
from typing import Sequence, Union



# revision identifiers, used by Alembic.
revision: str = 'b5cc4799efad'
down_revision: Union[str, Sequence[str], None] = ('4599a1026fdf', 'f322b4dc379d')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
