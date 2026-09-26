"""Recovery and diagnosis of missing canonical train branch owners."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select

from src.commands.task_commands import TaskCommandsMixin
from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import (
    ExecutionPrincipal,
    PrincipalKind,
    principal_context,
)
from src.database import Database
from src.database.tables import (
    integration_branch_owners,
    task_branch_origins,
    task_integration_checkpoints,
)
from src.doctor.integration_checks import run_check
from src.doctor.models import Severity
from src.integration.canonical_reservation import reserve_canonical_task_branch
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus
from src.profiles.capabilities import DENY_ALL
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("canonical-reservation.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Project"))
    await database.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
        )
    )
    await database.update_project(
        "p", hierarchical_integration_mode="train", integration_repository_id="repo"
    )
    await database.create_task(
        Task(
            id="producer",
            project_id="p",
            repo_id="repo",
            branch_name="aq/producer",
            title="Producer",
            description="",
            status=TaskStatus.BLOCKED,
        )
    )
    async with database.immediate() as conn:
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="producer",
                repository_id="repo",
                branch="aq/producer",
                updated_at=time.time(),
            )
        )
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin",
                task_id="producer",
                repository_id="repo",
                branch_name="aq/producer",
                base_sha="a" * 40,
                creation_generation=0,
                reserved=True,
                materialized=True,
                created_at=time.time(),
                materialized_at=time.time(),
            )
        )
    return database


async def _owner(db):
    async with db._engine.connect() as conn:
        return (await conn.execute(select(integration_branch_owners))).mappings().one_or_none()


async def test_restart_restores_missing_canonical_reservation_before_ready(db):
    command = SimpleNamespace(db=db)

    result = await TaskCommandsMixin._cmd_restart_task(command, {"task_id": "producer"})

    assert result["previous_status"] == "BLOCKED"
    assert (await db.get_task("producer")).status is TaskStatus.READY
    owner = await _owner(db)
    assert (owner["owner_id"], owner["owner_role"], owner["handoff_state"]) == (
        "producer",
        "worker",
        "reserved",
    )
    assert (await reserve_canonical_task_branch(db, "producer"))["outcome"] == "already_reserved"


async def test_restart_refuses_a_competing_owner_and_keeps_task_blocked(db):
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="other",
                repository_id="repo",
                ref="aq/producer",
                owner_id="other-task",
                owner_role="worker",
                fence_token=3,
                handoff_state="reserved",
                created_at=time.time(),
                updated_at=time.time(),
            )
        )

    result = await TaskCommandsMixin._cmd_restart_task(
        SimpleNamespace(db=db), {"task_id": "producer"}
    )

    assert "another or unresolved owner" in result["error"]
    assert (await db.get_task("producer")).status is TaskStatus.BLOCKED
    assert (await _owner(db))["owner_id"] == "other-task"


async def test_doctor_reports_missing_owner_then_clears_after_reservation(db):
    missing = await run_check(db, "integration.missing_canonical_owners")
    assert missing.severity is Severity.WARN
    assert missing.data["tasks"][0]["id"] == "producer"
    assert missing.data["tasks"][0]["handoff_state"] is None

    assert (await reserve_canonical_task_branch(db, "producer"))["outcome"] == "acquired"

    recovered = await run_check(db, "integration.missing_canonical_owners")
    assert recovered.severity is Severity.OK


async def test_released_row_is_reacquired_with_a_new_fence(db):
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="released",
                repository_id="repo",
                ref="aq/producer",
                owner_id="old-task",
                owner_role="worker",
                fence_token=8,
                handoff_state="released",
                created_at=time.time(),
                updated_at=time.time(),
            )
        )

    result = await reserve_canonical_task_branch(db, "producer")

    assert result["outcome"] == "acquired"
    assert (await _owner(db))["fence_token"] == 9


async def test_reservation_refuses_another_ready_task_on_the_branch(db):
    await db.create_task(
        Task(
            id="verifier",
            project_id="p",
            repo_id="repo",
            branch_name="aq/producer",
            title="Verifier",
            description="",
            status=TaskStatus.READY,
        )
    )

    result = await reserve_canonical_task_branch(db, "producer")

    assert result["outcome"] == "not_eligible"
    assert result["reason"] == "another task uses the canonical branch"
    assert await _owner(db) is None


async def test_reserve_owner_control_is_supervisor_scoped(db):
    command = IntegrationCommandsMixin()
    command.db = db
    worker = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=DENY_ALL,
        session_id="worker",
        project_id="p",
        elevated=False,
    )
    with principal_context(worker):
        denied = await command._cmd_integration_reserve_owner({"task_id": "producer"})
    assert denied["outcome"] == "unauthorized"
    assert await _owner(db) is None

    result = await command._cmd_integration_reserve_owner({"task_id": "producer"})
    assert result["outcome"] == "acquired"
    assert (await _owner(db))["owner_id"] == "producer"
