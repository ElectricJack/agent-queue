"""Keep record validation available during PostgreSQL backup restoration.

Revision ID: a00000000061
Revises: a00000000060
No payload validation semantics or retained data changes.
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000061"
down_revision = "a00000000060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT to_regprocedure('public.knowledge_snapshot_valid_v1(jsonb)')")):
        bind.exec_driver_sql(
            "ALTER FUNCTION public.knowledge_snapshot_valid_v1(jsonb) "
            "SET search_path = pg_catalog, public"
        )


def downgrade() -> None:
    # Retain restore-safe function lookup on rollback; it changes no v1
    # validation semantics and must remain usable with retained imports.
    pass
