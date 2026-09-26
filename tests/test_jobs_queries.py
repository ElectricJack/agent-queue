"""Idempotency races, atomic outbox and non-expiring workspace pins."""

import asyncio
import time
import uuid

import pytest
from sqlalchemy import select, update
from src.config import AppConfig
from src.database import Database
from src.database.tables import workspaces, jobs, job_outbox, job_workspace_pins, tasks
from src.models import Project, Task, TaskStatus, Workspace, RepoSourceType, Agent
from src.jobs.policy import JobError
from src.jobs.result import build_result
from src.jobs.workspace import mutation_guard
from src.jobs.service import JobService
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("jobs"))
    await database.initialize()
    await database.create_project(Project(id="p", name="project"))
    await database.create_agent(Agent(id="a", name="fixture", profile_id="worker-codex"))
    await database.create_task(
        Task(
            id="t",
            project_id="p",
            title="test",
            description="",
            status=TaskStatus.IN_PROGRESS,
            claim_epoch=1,
        )
    )
    async with database._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "t").values(claim_epoch=1))
    await database.create_workspace(
        Workspace(
            id="w",
            project_id="p",
            workspace_path=str(tmp_path),
            source_type=RepoSourceType.LINK,
            locked_by_agent_id="a",
            locked_by_task_id="t",
        )
    )
    yield database
    await database.close()


def values(**override):
    now = time.time()
    return {
        "id": str(uuid.uuid4()),
        "project_id": "p",
        "task_id": "t",
        "owner_kind": "task",
        "owner_id": "t",
        "claim_epoch": 1,
        "idempotency_key": "key",
        "request_hash": "hash",
        "preset": "lint",
        "preset_version": 1,
        "argv": ["/bin/true"],
        "contract": {},
        "workspace_id": "w",
        "workspace_generation": 0,
        "input_mode": "live",
        "job_class": "shared",
        "weight": 1,
        "priority_band": 2,
        "submitted_at": now,
        "queue_deadline": now + 1800,
        "run_timeout": 7200,
        "runner_nonce": uuid.uuid4().hex,
        **override,
    }


async def test_racing_idempotency_key_returns_one_row_and_one_pin(db):
    first, second = await asyncio.gather(db.submit_job(values()), db.submit_job(values()))
    assert first["id"] == second["id"]
    async with db._engine.connect() as conn:
        assert len((await conn.execute(select(jobs))).all()) == 1
        assert len((await conn.execute(select(job_workspace_pins))).all()) == 1
        assert await conn.scalar(select(workspaces.c.job_pin_count)) == 1
    with pytest.raises(JobError, match="idempotency_conflict"):
        await db.submit_job(values(request_hash="different"))
    # Replays work even after quotas fill.
    assert (await db.submit_job(values(), max_queued=0))["id"] == first["id"]


async def test_pin_blocks_release_acquisition_delete_and_mutation(db):
    job = await db.submit_job(values())
    await db.release_workspace("w")
    await db.release_workspaces_for_agent("a")
    await db.release_workspaces_for_task("t")
    await db.delete_workspace("w")
    assert (await db.get_workspace("w")).locked_by_task_id == "t"
    assert await db.acquire_workspace("p", "a", "t") is None
    with pytest.raises(JobError, match="workspace_busy"):
        async with mutation_guard(db, await db.get_workspace("w")):
            pytest.fail("must not mutate pinned tree")
    job = await db.transition_job(job["id"], 0, "starting")
    job = await db.transition_job(
        job["id"], 1, "lost", result=build_result(job, None), cleanup_blocked=True
    )
    assert await db.workspace_has_job_pin("w")
    await db.job_cleanup_verified(job["id"])
    await db.job_cleanup_verified(job["id"])  # duplicate cleanup never decrements twice
    await db.release_workspace("w")
    assert (await db.get_workspace("w")).locked_by_task_id is None
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(workspaces.c.job_pin_count)) == 0


async def test_version_cas_terminal_outbox_and_immutability(db):
    job = await db.submit_job(values())
    results = await asyncio.gather(
        db.transition_job(job["id"], 0, "starting"), db.transition_job(job["id"], 0, "starting")
    )
    assert sum(r is not None for r in results) == 1
    job = next(r for r in results if r)
    result = build_result(job, {"exit_code": 1})
    await db.transition_job(job["id"], 1, "failed", result=result, cleaned=True)
    assert await db.transition_job(job["id"], 2, "lost", result={}) is None
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(job_outbox))).mappings().one()["payload"] == result
    assert not await db.workspace_has_job_pin("w")


async def test_stale_generation_claim_and_output_reservation(db):
    with pytest.raises(JobError, match="stale_claim"):
        await db.submit_job(values(claim_epoch=0))
    with pytest.raises(JobError, match="workspace_busy"):
        await db.submit_job(values(workspace_generation=1))
    with pytest.raises(JobError, match="output_capacity"):
        await db.submit_job(values(), log_budget=1)
    assert not await db.workspace_has_job_pin("w")


async def test_mutation_serializes_submit_and_guard_is_reentrant(db):
    ws = await db.get_workspace("w")
    async with mutation_guard(db, ws):
        async with mutation_guard(db, ws):
            pass
        pending = asyncio.create_task(db.submit_job(values()))
        await asyncio.sleep(0.02)
        assert not pending.done()
    assert (await pending)["state"] == "queued"


async def test_feature_off_and_accepted_contract_immutable_on_reload(db, tmp_path):
    config = AppConfig(data_dir=str(tmp_path / "data"))
    service = JobService(db, config)
    args = dict(
        project_id="p",
        task_id="t",
        session_id=None,
        claim_epoch=1,
        workspace_id="w",
        generation=0,
        preset="lint",
        args=["src"],
        idempotency_key="one",
    )
    with pytest.raises(JobError, match="disabled"):
        await service.submit(**args)
    config.resources.jobs.enabled = True
    job = await service.submit(**args)
    assert "AQ_INSTANCE_TOKEN" not in job["contract"]["env"]
    assert job["contract"]["env"]["AQ_DB_SCOPE"] == "worker"
    config.resources.jobs.run_seconds = 10
    replay = await service.submit(**args)
    assert replay["run_timeout"] == 7200
    for bad in (["../../outside"], ["/tmp/foreign"], ["--aq-offline"]):
        with pytest.raises(JobError):
            await service.submit(**{**args, "args": bad, "idempotency_key": str(bad)})


async def test_output_budget_keeps_accepted_reservations_on_config_change(db):
    first = await db.submit_job(values(), reservation=64, log_budget=100)
    with pytest.raises(JobError, match="output_capacity"):
        await db.submit_job(values(idempotency_key="other"), reservation=40, log_budget=100)
    second = await db.submit_job(values(idempotency_key="other"), reservation=36, log_budget=100)
    assert first["output_reservation_bytes"] == 64
    assert second["output_reservation_bytes"] == 36
    assert await db.job_output_reservations() == 100


@pytest.mark.migration
async def test_jobs_migration_handles_existing_workspace_schema_and_is_idempotent():
    import importlib
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from tests.pg_dsn import create_scratch_database

    database = Database(await create_scratch_database("jobs_upgrade"))
    try:
        await database.initialize()
        async with database._engine.begin() as conn:
            for table in (job_outbox, job_workspace_pins, jobs):
                await conn.execute(text(f'DROP TABLE "{table.name}"'))
            for name in ("generation", "job_pin_count"):
                await conn.execute(text(f'ALTER TABLE workspaces DROP COLUMN "{name}"'))
            migration = importlib.import_module("migrations.versions.a00000000031_managed_jobs")

            def upgrade(sync_conn):
                with Operations.context(MigrationContext.configure(sync_conn)):
                    migration.upgrade()
                    migration.upgrade()

            await conn.run_sync(upgrade)
            assert await conn.scalar(select(workspaces.c.job_pin_count).limit(1)) is None
            assert list((await conn.execute(select(jobs))).all()) == []
    finally:
        await database.close()


async def test_configured_test_database_cannot_alias_daemon_database(db, tmp_path):
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.resources.jobs.enabled = True
    config.database.url = "postgresql+asyncpg://operator@localhost:5534/operator"
    config.resources.jobs.test_database_url = (
        "postgres://other@localhost:5534/operator?sslmode=prefer"
    )
    svc = JobService(db, config)
    args = dict(
        project_id="p",
        task_id="t",
        session_id="s",
        claim_epoch=1,
        workspace_id="w",
        generation=0,
        preset="test",
        args=["tests/test_jobs_output.py"],
        idempotency_key="database-contract",
    )
    with pytest.raises(JobError, match="test_database_unconfigured"):
        await svc.submit(**args)
    config.resources.jobs.test_database_url = "postgresql+asyncpg://test@localhost:5534/disposable"
    accepted = await svc.submit(**args)
    assert accepted["contract"]["env"]["POSTGRES_TEST_DSN"].endswith("/disposable")
    assert accepted["contract"]["env"]["AQ_DB_SCOPE"] == "worker"
    assert accepted["contract"]["env"]["AQ_DATABASE_URL"].startswith("aq-worker-")
    await svc.cancel(accepted)
