"""Keep the exact task branch on origins after the task row is deleted.

Revision ID: a00000000020
Revises: a0000000001f

Backfill from live and archived task rows, then from a unique worker ownership
record where one survived deletion. An already deleted task with no
unambiguous branch record remains NULL: guessing ``aq/<task_id>`` could delete
the wrong ref for an epic root. The discard drain parks that row for an
operator to recover the exact branch name. The column is conditional because
the squashed baseline creates fresh databases from live table metadata.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a00000000020"
down_revision = "a0000000001f"
branch_labels = None
depends_on = None

_TABLE = "task_branch_origins"


def _columns(bind) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(_TABLE)}


def _identity_guard(*, with_branch: bool) -> sa.TextClause:
    branch = "NEW.branch_name, " if with_branch else ""
    old_branch = "OLD.branch_name, " if with_branch else ""
    return sa.text(f"""
        CREATE OR REPLACE FUNCTION task_branch_origin_materialized_immutable()
        RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    IF OLD.materialized THEN
                        RAISE EXCEPTION 'materialized task branch origin is immutable';
                    END IF;
                    RETURN OLD;
                END IF;
                IF OLD.materialized AND ROW(
                    NEW.task_id, NEW.repository_id, {branch}NEW.parent_task_id,
                    NEW.parent_repository_id, NEW.parent_ref, NEW.base_sha,
                    NEW.creation_generation, NEW.reserved, NEW.materialized,
                    NEW.created_at, NEW.materialized_at
                ) IS DISTINCT FROM ROW(
                    OLD.task_id, OLD.repository_id, {old_branch}OLD.parent_task_id,
                    OLD.parent_repository_id, OLD.parent_ref, OLD.base_sha,
                    OLD.creation_generation, OLD.reserved, OLD.materialized,
                    OLD.created_at, OLD.materialized_at
                ) THEN
                    RAISE EXCEPTION 'materialized task branch origin is immutable';
                END IF;
                RETURN NEW;
            END;
        $$ LANGUAGE plpgsql
    """)


def upgrade() -> None:
    bind = op.get_bind()
    if "branch_name" not in _columns(bind):
        op.add_column(_TABLE, sa.Column("branch_name", sa.Text(), nullable=True))
    # The preceding trigger intentionally allows this one-time backfill.
    for source in ("tasks", "archived_tasks"):
        bind.execute(sa.text(f"""
            UPDATE task_branch_origins AS origin
            SET branch_name = source.branch_name
            FROM {source} AS source
            WHERE origin.task_id = source.id
              AND origin.branch_name IS NULL
              AND NULLIF(source.branch_name, '') IS NOT NULL
        """))
    bind.execute(sa.text("""
        UPDATE task_branch_origins AS origin
        SET branch_name = owner.ref
        FROM (
            SELECT owner_id, repository_id, MIN(ref) AS ref
            FROM integration_branch_owners
            WHERE owner_role = 'worker' AND ref LIKE 'aq/%'
            GROUP BY owner_id, repository_id
            HAVING COUNT(DISTINCT ref) = 1
        ) AS owner
        WHERE origin.task_id = owner.owner_id
          AND origin.repository_id = owner.repository_id
          AND origin.branch_name IS NULL
    """))
    op.execute(_identity_guard(with_branch=True))


def downgrade() -> None:
    op.execute(_identity_guard(with_branch=False))
    if "branch_name" in _columns(op.get_bind()):
        op.drop_column(_TABLE, "branch_name")
