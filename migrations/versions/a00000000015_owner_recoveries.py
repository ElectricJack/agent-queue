"""Add ``integration_owner_recoveries`` — the audit trail of an owner recovery.

Revision ID: a00000000015
Revises: a00000000014

``src.integration.owner_recovery.OwnerRecovery`` releases an ``attached`` or
``handoff_pending`` integration branch owner once its writer is proven gone
and its branch is safe on origin (preserving unsaved work to
``aq/preserved/<owner-row-id>`` first).  Every non-dry run -- released,
preserved-and-released or refused -- writes one row here with the evidence it
acted on and the principal that ran it.  Soft references only, like
``integration_delegate_releases``.

**The create is conditional**, for the same reason ``a00000000011``'s is: the
squashed baseline builds its tables from the live
``src.database.tables.metadata``, so a database created after this table was
declared already has it.

See docs/superpowers/specs/2026-09-21-integration-owner-recovery-design.md.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000015"
down_revision = "a00000000014"
branch_labels = None
depends_on = None

_TABLE = "integration_owner_recoveries"


def upgrade() -> None:
    from src.database.tables import integration_owner_recoveries

    bind = op.get_bind()
    if _TABLE not in set(sa.inspect(bind).get_table_names()):
        integration_owner_recoveries.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE in set(sa.inspect(bind).get_table_names()):
        op.drop_table(_TABLE)
