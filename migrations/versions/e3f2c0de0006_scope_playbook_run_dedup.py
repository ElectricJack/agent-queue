"""Scope Playbook V2 run deduplication to one playbook.

Revision ID: e3f2c0de0006
Revises: d3f2c0de0005

Different active playbooks may intentionally use the same rule id and receive
the same durable dispatch.  Include ``playbook_id`` in the partial unique
index so each matching playbook rule gets its own run while replays within one
playbook remain deduplicated.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3f2c0de0006"
down_revision: str | Sequence[str] | None = "d3f2c0de0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "uq_playbook_v2_runs_dispatch_rule"
_DISPATCH_WHERE = "dispatch_id IS NOT NULL"


def _replace_index(columns: list[str]) -> None:
    op.drop_index(
        _INDEX_NAME,
        table_name="playbook_v2_runs",
        sqlite_where=sa.text(_DISPATCH_WHERE),
        postgresql_where=sa.text(_DISPATCH_WHERE),
    )
    op.create_index(
        _INDEX_NAME,
        "playbook_v2_runs",
        columns,
        unique=True,
        sqlite_where=sa.text(_DISPATCH_WHERE),
        postgresql_where=sa.text(_DISPATCH_WHERE),
    )


def upgrade() -> None:
    _replace_index(["playbook_id", "dispatch_id", "rule_id"])


def downgrade() -> None:
    connection = op.get_bind()
    collision = connection.execute(
        sa.text(
            "SELECT dispatch_id, rule_id FROM playbook_v2_runs "
            "WHERE dispatch_id IS NOT NULL "
            "GROUP BY dispatch_id, rule_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if collision is not None:
        raise RuntimeError(
            "cannot downgrade while cross-playbook run dedup keys exist; "
            "retain or explicitly remove the colliding runs first"
        )
    _replace_index(["dispatch_id", "rule_id"])
