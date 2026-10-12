"""Legacy sessions acquire a durable refusal budget without losing their bindings."""

from __future__ import annotations

import importlib

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.database import Database
from src.database.tables import metadata
from src.models import Project, SessionRecord, Task
from tests.pg_dsn import create_scratch_database

pytestmark = pytest.mark.migration


async def test_legacy_session_refusal_upgrade_preserves_bindings_and_reruns():
    revision = importlib.import_module("migrations.versions.a00000000091_pool_claim_refusals")
    db = Database(await create_scratch_database("pool_claim_refusals"))
    await db.initialize()
    try:
        await db.create_project(Project(id="p", name="p"))
        await db.create_task(Task(
            id="attached-task", project_id="p", title="Attached", description="Legacy claim",
        ))
        await db.create_session(SessionRecord(
            id="legacy", name="legacy", instance_token="instance", profile_id="worker",
            lifecycle="pool", state="running", desired_state="running", started_at=1,
            task_id="attached-task", project_id="p", work_dir="/slot", claims=0,
            claim_phase="preparing", last_claim_epoch=7, harness="codex", provider="fake",
            epoch="previous-daemon",
        ))
        async with db.immediate() as conn:
            def verify(bind):
                with Operations.context(MigrationContext.configure(bind)):
                    revision.downgrade()
                    revision.downgrade()
                    revision.upgrade()
                    revision.upgrade()
                    row = bind.execute(sa.text(
                        "SELECT task_id, work_dir, claim_phase, last_claim_epoch, "
                        "claim_failure_count, claim_failure_reason, claim_failure_at "
                        "FROM sessions WHERE id = 'legacy'"
                    )).one()
                    assert tuple(row) == ('attached-task', '/slot', 'preparing', 7, 0, None, None)
                    assert not compare_metadata(MigrationContext.configure(bind), metadata)
                    revision.downgrade()
                    revision.upgrade()
                    assert not compare_metadata(MigrationContext.configure(bind), metadata)

            await conn.run_sync(verify)
    finally:
        await db.close()
