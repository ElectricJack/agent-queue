"""Idempotency races, atomic outbox and non-expiring workspace pins."""

import asyncio
import sys
import time
import uuid
from pathlib import Path

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


async def test_node_job_path_contains_server_resolved_runtime(db, tmp_path, monkeypatch):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    monkeypatch.setattr(
        "src.jobs.policy.node_executable", lambda name: str(binaries / name)
    )
    monkeypatch.setattr(
        "src.jobs.service.node_executable", lambda name: str(binaries / name)
    )
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.resources.jobs.enabled = True
    job = await JobService(db, config).submit(
        project_id="p", task_id="t", session_id=None, claim_epoch=1,
        workspace_id="w", generation=0, preset="npm_test", args=[],
        idempotency_key="node-path",
    )
    assert job["argv"] == [str(binaries / "npm"), "test"]
    assert job["contract"]["env"]["PATH"].startswith(str(binaries) + ":")


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
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent-queue"\n')
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


@pytest.mark.parametrize("project_name", ["agent-queue", "quilt-trader"])
async def test_managed_test_job_uses_project_interpreter_and_preconditions(
    db, tmp_path, monkeypatch, project_name
):
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "{project_name}"\n')
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.resources.jobs.enabled = True
    config.resources.test_workers = 3
    config.resources.jobs.test_database_url = (
        "postgresql+asyncpg://test@localhost:5534/disposable"
        if project_name == "agent-queue" else None
    )
    if project_name == "quilt-trader":
        venv = tmp_path / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
        python = venv / "bin" / "python"
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(0o755)
    else:
        python = Path(sys.executable)
    monkeypatch.setattr(
        "src.resources.project_tests.has_xdist",
        lambda _: project_name == "agent-queue",
    )
    job = await JobService(db, config).submit(
        project_id="p", task_id="t", session_id="s", claim_epoch=1,
        workspace_id="w", generation=0, preset="test", args=["tests/test_example.py"],
        idempotency_key=f"test-{project_name}",
    )
    assert job["argv"][:3] == [str(python), "-m", "pytest"]
    assert ("-n" in job["argv"]) == (project_name == "agent-queue")
    assert ("POSTGRES_TEST_DSN" in job["contract"]["env"]) == (
        project_name == "agent-queue"
    )
    if project_name == "quilt-trader":
        assert job["contract"]["env"]["VIRTUAL_ENV"] == str(venv)
        assert job["contract"]["env"]["PATH"].startswith(str(venv / "bin") + ":")


async def test_managed_test_job_rejects_missing_pinned_interpreter(db, tmp_path):
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.resources.jobs.enabled = True
    config.resources.test_interpreters["p"] = str(tmp_path / "missing-python")
    with pytest.raises(JobError, match="jobs.test_interpreter_unavailable"):
        await JobService(db, config).submit(
            project_id="p", task_id="t", session_id="s", claim_epoch=1,
            workspace_id="w", generation=0, preset="test", args=["tests/test_example.py"],
            idempotency_key="missing-python",
        )


async def test_integration_test_job_uses_configured_interpreter_outside_snapshot(
    db, tmp_path, monkeypatch
):
    from src.git.manager import GitManager

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "quilt-trader"\n')
    git = GitManager()
    await git._arun(["init"], cwd=str(tmp_path))
    await git._arun(["add", "pyproject.toml"], cwd=str(tmp_path))
    await git._arun([
        "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "snapshot",
    ], cwd=str(tmp_path))
    head = await git._arun(["rev-parse", "HEAD"], cwd=str(tmp_path))
    async with db._engine.begin() as conn:
        await conn.execute(update(workspaces).where(workspaces.c.id == "w").values(
            kind_id="job-snapshot", enabled=False, locked_by_task_id=None,
        ))
    venv = tmp_path.parent / f"{tmp_path.name}-venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    python = venv / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    config = AppConfig(data_dir=str(tmp_path.parent / "data"))
    config.resources.jobs.enabled = True
    config.resources.test_interpreters["p"] = str(python)
    monkeypatch.setattr("src.resources.project_tests.has_xdist", lambda _: False)
    job = await JobService(db, config).submit(
        project_id="p", task_id=None, session_id=None, claim_epoch=None,
        workspace_id="w", generation=0, owner_kind="integration", owner_id="operation",
        input_mode="snapshot", input_ref=head, preset="test",
        args=["tests/test_example.py"], idempotency_key="publisher-test",
    )
    assert job["argv"][:3] == [str(python), "-m", "pytest"]
    assert "-n" not in job["argv"]
    assert job["contract"]["env"]["VIRTUAL_ENV"] == str(venv)
    assert "POSTGRES_TEST_DSN" not in job["contract"]["env"]


@pytest.mark.parametrize("input_mode", ["live", "snapshot"])
@pytest.mark.parametrize("smoke_exit", [0, 7])
async def test_managed_e2e_runs_and_reports_submitted_checkout(
    db, tmp_path, input_mode, smoke_exit
):
    from src.git.manager import GitManager
    from src.jobs.artifacts import atomic_json, read_json
    from src.jobs.identity import stop_tree
    from tests.test_jobs_runner import finish, launch

    daemon = tmp_path / "daemon"
    checkout = tmp_path / "submitted checkout"
    wrapper = daemon / "src/jobs/e2e.py"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes((Path(__file__).resolve().parents[1] / "src/jobs/e2e.py").read_bytes())
    # The old wrapper tried these non-executable daemon scripts even though
    # the submitted checkout had executable scripts and a different commit.
    (daemon / "scripts").mkdir()
    for script in ("e2e-env.sh", "e2e-smoke.sh"):
        (daemon / "scripts" / script).write_text("#!/bin/sh\nexit 99\n")
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "checkout_identity.py").write_text("NAME = 'submitted-checkout'\n")
    for script, args, code in (
        ("e2e-env.sh", "--reset", 0),
        ("e2e-smoke.sh", "", smoke_exit),
    ):
        path = checkout / "scripts" / script
        path.write_text(
            "#!/bin/sh\nset -eu\n"
            f'printf "{script}:%s\\n" "$*"\n'
            f'[ "$*" = "{args}" ]\n'
            "pwd\n"
            "python3 -c 'from checkout_identity import NAME; print(NAME)'\n"
            f"exit {code}\n"
        )
        path.chmod(0o755)
    git = GitManager()
    heads = []
    for root in (daemon, checkout):
        await git._arun(["init"], cwd=str(root))
        await git._arun(["add", "."], cwd=str(root))
        await git._arun([
            "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-m", root.name,
        ], cwd=str(root))
        heads.append((await git._arun(["rev-parse", "HEAD"], cwd=str(root))).strip())
    if input_mode == "snapshot":
        await git._arun(["checkout", "--detach"], cwd=str(checkout))
    async with db._engine.begin() as conn:
        await conn.execute(update(workspaces).where(workspaces.c.id == "w").values(
            workspace_path=str(checkout),
            **({"kind_id": "job-snapshot", "enabled": False, "locked_by_task_id": None}
               if input_mode == "snapshot" else {}),
        ))
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.resources.jobs.enabled = True
    config.resources.jobs.test_database_url = (
        "postgresql+asyncpg://test@localhost:5534/disposable"
    )
    service = JobService(db, config)
    service.root = daemon
    job = await service.submit(
        project_id="p", task_id="t" if input_mode == "live" else None,
        session_id=None, claim_epoch=1 if input_mode == "live" else None,
        workspace_id="w", generation=0, preset="e2e", args=[],
        idempotency_key="e2e-checkout", input_mode=input_mode,
        input_ref=heads[1] if input_mode == "snapshot" else None,
        owner_kind="task" if input_mode == "live" else "integration",
        owner_id="t" if input_mode == "live" else "operation",
    )
    assert job["argv"] == [str(Path(sys.executable).absolute()), str(wrapper)]
    assert job["preset_version"] == 2
    assert job["job_class"] == "exclusive"
    assert job["contract"]["cwd"] == str(checkout)
    env = job["contract"]["env"]
    assert env["E2E_DB_NAME"] == "agent_queue_e2e_job_" + job["id"].replace("-", "")
    directory = Path(config.data_dir) / "runs" / job["id"]
    assert env["AQ_E2E_HOME"] == str(directory / "e2e")
    directory.mkdir(parents=True)
    atomic_json(directory / "request.json", job)
    proc = await launch(directory, job)
    try:
        receipt = await finish(proc, directory)
        result = read_json(directory / "result.json")
        assert receipt["exit_code"] == smoke_exit
        assert result["outcome"] == ("passed" if smoke_exit == 0 else "failed")
        assert result["input_ref"] == heads[1] != heads[0]
        assert result["input_stability"] == (
            "stable" if input_mode == "snapshot" else "unverified"
        )
        assert "e2e-env.sh:--reset\n" in result["excerpt"]
        assert "e2e-smoke.sh:\n" in result["excerpt"]
        assert result["excerpt"].count(str(checkout)) == 2
        assert result["excerpt"].count("submitted-checkout") == 2
        assert str(daemon) not in result["excerpt"]
    finally:
        await stop_tree(job["runner_nonce"], grace=0.1)
        await proc.wait()
