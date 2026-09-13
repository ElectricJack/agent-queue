"""Repoint rows pinned to a retired intelligence class.

The shipped class ladder collapsed from twelve to seven
(``fast-{low,high}``, ``standard-high``, ``deep-{low,high}``,
``astra-{low,high}``).  A row still naming a retired class would resolve no
launch model, so forward-looking pins are moved to the surviving class in the
same tier — the same table as
:data:`src.profiles.class_retirement.RETIRED_CLASS_REPLACEMENTS`, which does
the vault half.

Historical evidence is deliberately **not** rewritten: ``sessions``,
``task_session_attempts`` and ``archived_tasks`` record what actually ran, and
model attribution reads them.

Revision ID: a0000000000f
Revises: a0000000000e
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a0000000000f"
down_revision = "a0000000000e"
branch_labels = None
depends_on = None

REPLACEMENTS: dict[str, str] = {
    "fast-off": "fast-low",
    "fast-medium": "fast-high",
    "standard-off": "standard-high",
    "standard-low": "standard-high",
    "standard-medium": "standard-high",
    "deep-off": "deep-low",
    "deep-medium": "deep-high",
}

#: (table, column) pairs holding a *live* pin.  Each is guarded by an
#: inspector check so the revision is idempotent on a database built from
#: current metadata as well as one replayed from the baseline.
TARGETS: tuple[tuple[str, str], ...] = (
    ("tasks", "intelligence_class"),
    ("agents", "intelligence_class"),
    ("task_assignment_routes", "intelligence_class"),
    ("integration_repair_stages", "intelligence_class"),
    ("agent_profiles", "default_class"),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table, column in TARGETS:
        if table not in tables:
            continue
        if column not in {col["name"] for col in inspector.get_columns(table)}:
            continue
        for retired, replacement in REPLACEMENTS.items():
            bind.execute(
                sa.text(
                    f"UPDATE {table} SET {column} = :replacement WHERE {column} = :retired"  # noqa: S608
                ),
                {"replacement": replacement, "retired": retired},
            )


def downgrade() -> None:
    """No-op: the retired classes are gone, so there is nothing to restore to."""
