"""Index the persistent layout-job convergence ledger by rules kind.

Revision ID: a00000000016
Revises: a00000000015

``layout_jobs`` is intentionally append-only: every engine-rules revision
uses its rows as a convergence ledger.  The ledger reader filters by ``kind``
and groups the remaining rows by project, variant, and status.  The baseline
already creates this index from ``src.database.tables.metadata``; inspecting
first keeps this revision safe for both fresh and existing databases.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

revision = "a00000000016"
down_revision = "a00000000015"
branch_labels = None
depends_on = None

_TABLE = "layout_jobs"
_INDEX = "idx_layout_jobs_kind_project_variant_status"
_COLUMNS = ["kind", "project_id", "variant", "status"]


def _index_names(bind) -> set[str]:
    return {index["name"] for index in inspect(bind).get_indexes(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if _INDEX not in _index_names(bind):
        op.create_index(_INDEX, _TABLE, _COLUMNS)


def downgrade() -> None:
    bind = op.get_bind()
    if _INDEX in _index_names(bind):
        op.drop_index(_INDEX, table_name=_TABLE)
