"""Upgrade legacy withdrawals without granting approval or inventing a surface."""

from __future__ import annotations

import importlib

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.database import Database
from src.database.tables import doc_reviews, metadata
from src.models import Project, Task, TaskStatus
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration


@pytest.mark.parametrize("old_status", ["open", "expired"])
async def test_upgrade_cancels_legacy_gate_preserves_audit_and_reruns(old_status):
    revision = importlib.import_module(
        "migrations.versions.a00000000090_review_withdrawal_recovery"
    )
    db = Database(lease_dsn("review_withdrawal_migration"))
    await db.initialize()
    try:
        await db.create_project(Project(id="p", name="p"))
        await db.create_task(Task(
            id="waiter", project_id="p", title="Waiter", description="Dependent work",
            status=TaskStatus.READY,
        ))
        gate_id, _ = await db.create_gate(
            "p", "review", "Legacy review", await_id="rev-legacy",
            waiter_task_ids=["waiter"],
        )
        async with db.immediate() as conn:
            await conn.execute(sa.insert(doc_reviews).values(
                id="rev-legacy", project_id="p", kind="spec", title="Legacy review",
                vault_path="legacy.md", state="withdrawn", gate_id=gate_id,
                decided_by="human:local-operator", decided_at=100.0,
                decision_note="superseded", created_at=90.0, updated_at=100.0,
            ))
            await conn.exec_driver_sql(
                "ALTER TABLE gates DROP CONSTRAINT ck_gates_status"
            )
            await conn.exec_driver_sql(
                "ALTER TABLE gates ADD CONSTRAINT ck_gates_status "
                "CHECK (status IN ('open','resolved','expired'))"
            )
            for name in revision.AUDIT_COLUMNS:
                await conn.exec_driver_sql(f"ALTER TABLE doc_reviews DROP COLUMN {name}")
            await conn.execute(sa.text("UPDATE gates SET status = :status"),
                               {"status": old_status})

            def verify(bind):
                with Operations.context(MigrationContext.configure(bind)):
                    revision.upgrade()
                    revision.upgrade()
                    row = bind.execute(sa.select(doc_reviews)).mappings().one()
                    assert row["withdrawn_by"] == "human:local-operator"
                    assert row["withdrawn_at"] == 100.0
                    assert row["withdrawal_reason"] == "superseded"
                    assert row["withdrawn_via"] is None
                    assert bind.execute(sa.text("SELECT status FROM gates")).scalar_one() == (
                        "cancelled"
                    )
                    assert bind.execute(sa.text(
                        "SELECT value FROM task_metadata WHERE task_id = 'waiter' "
                        "AND key = 'needs_attention'"
                    )).scalar_one() == '"review_withdrawn"'
                    assert bind.execute(sa.text(
                        "SELECT is_blocked FROM tasks WHERE id = 'waiter'"
                    )).scalar_one() == 1
                    columns = {col["name"]: col for col in sa.inspect(bind).get_columns(
                        "doc_reviews"
                    )}
                    assert all(columns[name]["nullable"] for name in revision.AUDIT_COLUMNS)
                    assert not compare_metadata(MigrationContext.configure(bind), metadata)
                    revision.downgrade()
                    assert bind.execute(sa.text("SELECT status FROM gates")).scalar_one() == (
                        "open"
                    )
                    assert not set(revision.AUDIT_COLUMNS) & {
                        col["name"] for col in sa.inspect(bind).get_columns("doc_reviews")
                    }
                    revision.upgrade()
                    assert not compare_metadata(MigrationContext.configure(bind), metadata)

            await conn.run_sync(verify)
        assert (await db.get_task("waiter")).is_blocked
    finally:
        await db.close()
