"""Inert durable record identities, revisions and informational links.

Revision ID: a00000000055
Revises: a00000000054
No task backfill, legacy import, activation or provider initialization.
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000055"
down_revision = "a00000000054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from src.database.tables import metadata
    from src.records.schema import (
        RECORD_TABLE_NAMES,
        install_record_guards_v1,
        install_record_validators_v1,
    )

    bind = op.get_bind()
    install_record_validators_v1(bind)
    metadata.create_all(bind, tables=[metadata.tables[name] for name in RECORD_TABLE_NAMES])
    # Baseline create_all may already have made every table; triggers still need
    # repair/reinstallation. The installation UUID is inserted only if absent.
    install_record_guards_v1(bind)


def downgrade() -> None:
    from src.database.tables import metadata
    from src.records.schema import GUARDS_V1, RECORD_TABLE_NAMES

    bind = op.get_bind()
    existing = [name for name in RECORD_TABLE_NAMES if sa.inspect(bind).has_table(name)]
    # Identity seed alone is empty. Every other row is durable evidence or state,
    # even an unused scope/backfill cursor. Refuse before executing any DDL.
    for name in existing:
        if (
            name != "record_installation"
            and bind.execute(
                sa.text(f'SELECT EXISTS (SELECT 1 FROM "{name}" LIMIT 1)')
            ).scalar_one()
        ):
            raise RuntimeError(
                "knowledge records contain data; disable writes and use read-only rollback"
            )
    metadata.drop_all(bind, tables=[metadata.tables[name] for name in existing])
    for statement in reversed(GUARDS_V1):
        name = statement.split("FUNCTION ", 1)[1].split("(", 1)[0]
        bind.exec_driver_sql(f"DROP FUNCTION IF EXISTS {name}()")
    for name, argument in (
        ("knowledge_snapshot_valid_v1", "jsonb"),
        ("record_utc_valid_v1", "text"),
        ("record_metadata_valid_v1", "jsonb"),
    ):
        bind.exec_driver_sql(f"DROP FUNCTION IF EXISTS {name}({argument})")
