"""Add ``integration_delegate_releases`` — the audit trail of a delegate release.

Revision ID: a00000000011
Revises: a00000000010

A verifier, repair-stage writer or candidate-member resolver whose integration
operation ended is settled to a terminal non-success by
``src.integration.delegate_release.release_delegates``; this table records
which operation ended it, how, and who ran the release.

The four ``tasks.id`` foreign keys on integration history
(``integration_parent_episodes``, ``integration_parent_verifications``,
``integration_repair_operations.verifier_task_id``,
``integration_candidate_resolutions.repair_task_id``) are deliberately **kept**:
dropping them is the schema change
docs/superpowers/specs/2026-09-20-archive-tasks-with-integration-history-design.md
holds pending its review findings B1–B3.

**The create is conditional**, for the same reason ``a00000000010``'s is: the
squashed baseline builds its tables from the live
``src.database.tables.metadata``, so a database created after this table was
declared already has it.

See docs/superpowers/specs/2026-09-20-integration-delegate-release-design.md.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000011"
down_revision = "a00000000010"
branch_labels = None
depends_on = None

_TABLE = "integration_delegate_releases"


def upgrade() -> None:
    from src.database.tables import integration_delegate_releases

    bind = op.get_bind()
    if _TABLE not in set(sa.inspect(bind).get_table_names()):
        integration_delegate_releases.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE in set(sa.inspect(bind).get_table_names()):
        op.drop_table(_TABLE)
