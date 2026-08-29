# migrations/versions/b2c3d4e5f6a7_hierarchy_canonicalise.py
"""hierarchy canonicalise + single-parent index (revision B)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-28

Runs the preflight on a separate autocommit connection first so the rejects
report survives an abort (spec §17).
"""

import os
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from src.database import hierarchy_migration as hm

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _open_preflight_connection(bind):
    """A connection separate from ``bind`` so its commit survives an abort.

    On SQLite's ``StaticPool`` (used by the in-process test suite),
    ``bind.engine.connect()`` hands back the very connection the outer
    migration transaction is using — a commit on it would still be undone
    by the caller's rollback.  Detect that and fall back to a brand-new
    engine over the same URL, which always gets its own connection.
    """
    candidate = bind.engine.connect()
    if candidate.connection is bind.connection:
        candidate.close()
        engine = sa.create_engine(str(bind.engine.url))
        return engine.connect(), engine
    return candidate, None


def upgrade() -> None:
    bind = op.get_bind()
    run_id = uuid.uuid4().hex[:12]

    # Preflight on its own connection: the report commits even if we abort.
    pre, extra_engine = _open_preflight_connection(bind)
    try:
        with pre.begin():
            plan = hm.canonicalise(pre)
            hm.persist_rejects(pre, run_id, plan.rejects)
    finally:
        pre.close()
        if extra_engine is not None:
            extra_engine.dispose()

    report = os.path.expanduser(f"~/.agent-queue/logs/hierarchy-preflight-{run_id}.json")
    try:
        hm.write_report(report, run_id, plan)
    except OSError:
        pass

    if plan.rejects and not hm.allow_rejects():
        raise RuntimeError(
            f"hierarchy canonicalisation found {len(plan.rejects)} reject(s); "
            f"see hierarchy_migration_rejects run_id={run_id} and {report}. "
            f"Fix the data or set {hm.ALLOW_REJECTS_ENV}=1 to proceed."
        )

    hm.apply(bind, plan)
    with op.batch_alter_table("task_dependencies", schema=None) as b:
        b.create_index(
            "uq_task_deps_single_parent",
            ["task_id"],
            unique=True,
            sqlite_where=sa.text("dep_type = 'parent-child'"),
            postgresql_where=sa.text("dep_type = 'parent-child'"),
        )


def downgrade() -> None:
    with op.batch_alter_table("task_dependencies", schema=None) as b:
        b.drop_index("uq_task_deps_single_parent")
