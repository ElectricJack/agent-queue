"""Knowledge proposals, authority, sharing and permanent redaction ledger.

Revision ID: a00000000059
Revises: a00000000055
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000059"
down_revision = "a00000000055"
branch_labels = None
depends_on = None


def upgrade():
    from src.database.tables import metadata
    from src.knowledge.protection_schema import (
        PROTECTION_TABLE_NAMES,
        install_protection_guards_v1,
    )

    bind = op.get_bind()
    metadata.create_all(bind, tables=[metadata.tables[name] for name in PROTECTION_TABLE_NAMES])
    install_protection_guards_v1(bind)


def downgrade():
    from src.database.tables import metadata
    from src.knowledge.protection_schema import PROTECTION_TABLE_NAMES
    from src.records.schema import install_record_guards_v1

    bind = op.get_bind()
    for name in (*PROTECTION_TABLE_NAMES, "knowledge_records"):
        if (
            sa.inspect(bind).has_table(name)
            and bind.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{name}")')).scalar_one()
        ):
            raise RuntimeError("knowledge records contain data; use read-only rollback")
    install_record_guards_v1(bind)
    metadata.drop_all(bind, tables=[metadata.tables[name] for name in PROTECTION_TABLE_NAMES])
    for signature in (
        "knowledge_erase_v1(uuid)",
        "knowledge_erasure_guard_v1()",
        "knowledge_protection_scope_v1()",
        "record_link_check_v2()",
    ):
        bind.exec_driver_sql(f"DROP FUNCTION IF EXISTS {signature}")
