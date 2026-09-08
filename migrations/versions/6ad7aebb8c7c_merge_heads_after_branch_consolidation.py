"""merge heads after branch consolidation

Revision ID: 6ad7aebb8c7c
Revises: 9d3895228e7b, a13a5e1e4f06, c6b64a925c90
Create Date: 2026-09-07 21:09:06.650170

"""
from typing import Sequence, Union



# revision identifiers, used by Alembic.
revision: str = '6ad7aebb8c7c'
down_revision: Union[str, Sequence[str], None] = ('9d3895228e7b', 'a13a5e1e4f06', 'c6b64a925c90')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
