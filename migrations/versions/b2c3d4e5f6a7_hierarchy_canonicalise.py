# migrations/versions/b2c3d4e5f6a7_hierarchy_canonicalise.py
"""hierarchy canonicalise + single-parent index (revision B)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-28

Runs the preflight on a separate autocommit connection first so the rejects
report survives an abort (spec §17).

REQUIRES ``transaction_per_migration=True`` in ``migrations/env.py``.  The
preflight below opens a second connection via ``bind.engine.connect()``.  If
Alembic wrapped the whole revision chain in one transaction, that second
connection could not see revision a1b2c3d4e5f6's still-uncommitted DDL on a
fresh Postgres database, and would block on its ACCESS EXCLUSIVE locks on an
existing one.  With one transaction per migration, revision A is committed
before this revision starts.
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


def upgrade() -> None:
    bind = op.get_bind()
    run_id = uuid.uuid4().hex[:12]

    # Preflight, committed before we decide whether to abort.  On Postgres
    # ``bind.engine.connect()`` opens a genuinely separate connection and
    # transaction, independent of the outer migration transaction, so the
    # commit below is durable even if ``upgrade`` later raises.  On SQLite's
    # ``StaticPool`` (used by the in-process test suite and any embedded
    # deployment) there is only ever one underlying DBAPI connection, so
    # this "separate" connection is in fact the same one the outer
    # migration transaction runs on — the commit here lands the rejects
    # rows (and revision A's DDL) immediately, before the RuntimeError
    # below aborts the *rest* of this migration.  That is the intended
    # durability on both backends: the report and the rejects table always
    # survive an abort.
    with bind.engine.connect() as pre:
        with pre.begin():
            plan = hm.canonicalise(pre)
            hm.persist_rejects(pre, run_id, plan.rejects)

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
