"""Authority and identity checks for integration ownership transfer."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select

from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.tables import (
    integration_batches,
    integration_branch_owners,
    integration_repair_operations,
    integration_repair_stages,
)
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus
from src.profiles.capabilities import CapabilityPolicy


TARGET = {"repository_id": "repo", "branch": "aq/parent"}


async def _seed(handler) -> None:
    await handler.db.create_project(Project(id="p", name="Project"))
    await handler.db.create_project(Project(id="other", name="Other"))
    await handler.db.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.CLONE)
    )
    await handler.db.create_repo(
        RepoConfig(id="other-repo", project_id="other", source_type=RepoSourceType.CLONE)
    )
    await handler.db.create_task(
        Task(
            id="parent",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="Parent",
            description="",
        )
    )
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner",
                repository_id="repo",
                ref="aq/parent",
                owner_id="parent",
                owner_role="worker",
                fence_token=4,
                handoff_state="attached",
                session_id="session-parent",
                workspace_id="slot-parent",
                created_at=time.time(),
                updated_at=time.time(),
            )
        )


async def _owner_row(handler) -> dict:
    async with handler.db._engine.connect() as conn:
        row = (await conn.execute(select(integration_branch_owners))).mappings().one()
    return dict(row)


def _args(next_owner_id: str, next_role: str = "worker", *, token: int = 4) -> dict:
    return {
        "target": TARGET,
        "expected_token": token,
        "next_owner_id": next_owner_id,
        "next_role": next_role,
    }


async def test_stale_expected_token_does_not_stop_the_current_writer(command_handler_factory):
    """Checking the token after confirmation would stop a newer valid writer."""
    handler = await command_handler_factory()
    await _seed(handler)
    await handler.db.create_task(
        Task(
            id="next",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="Next",
            description="",
        )
    )
    confirm = AsyncMock(return_value=True)
    handler.orchestrator.aconfirm_integration_owner_handoff = confirm

    result = await handler.execute(
        "integration_transfer_owner", _args("next", token=3)
    )

    assert result["outcome"] == "stale_owner"
    confirm.assert_not_awaited()
    assert (await _owner_row(handler))["fence_token"] == 4


async def test_transfer_rejects_a_cross_project_task_owner(command_handler_factory):
    """Looking up only the task ID would grant a foreign project this branch."""
    handler = await command_handler_factory()
    await _seed(handler)
    await handler.db.create_task(
        Task(
            id="foreign",
            project_id="other",
            repo_id="other-repo",
            branch_name="aq/parent",
            title="Foreign",
            description="",
        )
    )
    confirm = AsyncMock(return_value=True)
    handler.orchestrator.aconfirm_integration_owner_handoff = confirm

    result = await handler.execute("integration_transfer_owner", _args("foreign"))

    assert result["outcome"] == "human_required"
    confirm.assert_not_awaited()
    assert (await _owner_row(handler))["owner_id"] == "parent"


async def test_transfer_rejects_a_same_project_task_on_an_unrelated_branch(
    command_handler_factory,
):
    """Project membership alone must not authorize a different task branch."""
    handler = await command_handler_factory()
    await _seed(handler)
    await handler.db.create_task(
        Task(
            id="unrelated",
            project_id="p",
            repo_id="repo",
            branch_name="aq/unrelated",
            title="Unrelated",
            description="",
        )
    )
    confirm = AsyncMock(return_value=True)
    handler.orchestrator.aconfirm_integration_owner_handoff = confirm

    result = await handler.execute("integration_transfer_owner", _args("unrelated"))

    assert result["outcome"] == "human_required"
    confirm.assert_not_awaited()


async def test_transfer_accepts_a_collector_batch_bound_to_the_target(
    command_handler_factory,
):
    """Collector authority comes from the persisted batch, not a session ID."""
    handler = await command_handler_factory()
    await _seed(handler)
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="batch",
                project_id="p",
                repository_id="repo",
                source_manifest_digest="manifest",
                lifecycle="sealed",
                integration_branch="aq/parent",
                policy_snapshot={},
                artifact_snapshot={},
                cleanup_state="pending",
                created_at=time.time(),
                updated_at=time.time(),
            )
        )
    confirm = AsyncMock(return_value=True)
    handler.orchestrator.aconfirm_integration_owner_handoff = confirm

    result = await handler.execute(
        "integration_transfer_owner", _args("batch", "collector")
    )

    assert result["outcome"] == "transferred"
    assert result["fence"]["owner_id"] == "batch"
    confirm.assert_awaited_once()


async def test_transfer_rejects_a_collector_operation_bound_to_another_branch(
    command_handler_factory,
):
    """A same-project operation cannot collect onto an unrelated parent ref."""
    handler = await command_handler_factory()
    await _seed(handler)
    await handler.db.create_task(
        Task(
            id="other-parent",
            project_id="p",
            repo_id="repo",
            branch_name="aq/other-parent",
            title="Other parent",
            description="",
        )
    )
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_repair_operations).values(
                id="operation",
                target_kind="parent",
                parent_task_id="other-parent",
                episode_id="episode",
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="checks-v1",
                created_at=time.time(),
                updated_at=time.time(),
            )
        )
    confirm = AsyncMock(return_value=True)
    handler.orchestrator.aconfirm_integration_owner_handoff = confirm

    result = await handler.execute(
        "integration_transfer_owner", _args("operation", "collector")
    )

    assert result["outcome"] == "human_required"
    confirm.assert_not_awaited()


async def test_transfer_accepts_repair_task_bound_by_current_active_stage(
    command_handler_factory,
):
    """Requiring the repair task's own branch would ignore its persisted target."""
    handler = await command_handler_factory()
    await _seed(handler)
    await handler.db.create_task(
        Task(
            id="repair-task",
            project_id="p",
            repo_id="repo",
            branch_name="aq/repair-work",
            status=TaskStatus.IN_PROGRESS,
            title="Repair",
            description="",
        )
    )
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_repair_operations).values(
                id="repair-operation",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="episode",
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="checks-v1",
                created_at=time.time(),
                updated_at=time.time(),
            )
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id="repair-operation",
                ordinal=0,
                policy={},
                intelligence_class="primary",
                repair_task_id="repair-task",
                starting_sha="a" * 40,
                state="active",
            )
        )
    confirm = AsyncMock(return_value=True)
    handler.orchestrator.aconfirm_integration_owner_handoff = confirm

    result = await handler.execute(
        "integration_transfer_owner", _args("repair-task", "repair")
    )

    assert result["outcome"] == "transferred"
    assert result["fence"]["owner_id"] == "repair-task"


@pytest.mark.parametrize(
    ("parent_branch", "operation_state", "task_status"),
    [
        ("aq/unrelated", "active", TaskStatus.IN_PROGRESS),
        ("aq/parent", "completed", TaskStatus.IN_PROGRESS),
        ("aq/parent", "active", TaskStatus.COMPLETED),
    ],
)
async def test_transfer_rejects_unrelated_or_terminal_repair_relationship(
    command_handler_factory,
    parent_branch,
    operation_state,
    task_status,
):
    """Only a live current repair stage for the exact target may take ownership."""
    handler = await command_handler_factory()
    await _seed(handler)
    await handler.db.create_task(
        Task(
            id="repair-target",
            project_id="p",
            repo_id="repo",
            branch_name=parent_branch,
            title="Repair target",
            description="",
        )
    )
    await handler.db.create_task(
        Task(
            id="repair-task",
            project_id="p",
            repo_id="repo",
            branch_name="aq/repair-work",
            status=task_status,
            title="Repair",
            description="",
        )
    )
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_repair_operations).values(
                id="repair-operation",
                target_kind="parent",
                parent_task_id="repair-target",
                episode_id="episode",
                active_stage=0,
                state=operation_state,
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="checks-v1",
                created_at=time.time(),
                updated_at=time.time(),
            )
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id="repair-operation",
                ordinal=0,
                policy={},
                intelligence_class="primary",
                repair_task_id="repair-task",
                starting_sha="a" * 40,
                state="active",
            )
        )
    confirm = AsyncMock(return_value=True)
    handler.orchestrator.aconfirm_integration_owner_handoff = confirm

    result = await handler.execute(
        "integration_transfer_owner", _args("repair-task", "repair")
    )

    assert result["outcome"] == "human_required"
    confirm.assert_not_awaited()


async def test_session_principal_cannot_transfer_even_in_audit_mode(command_handler_factory):
    """Audit grace for legacy capabilities must not grant branch handoff authority."""
    handler = await command_handler_factory()
    await _seed(handler)
    await handler.db.create_task(
        Task(
            id="next",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="Next",
            description="",
        )
    )
    handler.config.security.capability_enforcement = "audit"
    confirm = AsyncMock(return_value=True)
    handler.orchestrator.aconfirm_integration_owner_handoff = confirm
    session = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["integration_transfer_owner"], derived_from_legacy=True
        ),
        project_id="p",
        session_id="session-caller",
    )

    with principal_context(session):
        result = await handler.execute("integration_transfer_owner", _args("next"))

    assert result["outcome"] == "human_required"
    confirm.assert_not_awaited()


async def test_scoped_capable_playbook_transfers_to_the_bound_task(command_handler_factory):
    """A capable project playbook may orchestrate a server-validated destination."""
    handler = await command_handler_factory()
    await _seed(handler)
    await handler.db.create_task(
        Task(
            id="next",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="Next",
            description="",
        )
    )
    confirm = AsyncMock(return_value=True)
    handler.orchestrator.aconfirm_integration_owner_handoff = confirm
    playbook = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["integration_transfer_owner"]
        ),
        project_id="p",
        parent_run_id="unrelated-run-domain",
    )

    with principal_context(playbook):
        result = await handler.execute("integration_transfer_owner", _args("next"))

    assert result == {
        "success": True,
        "outcome": "transferred",
        "fence": {"target": TARGET, "owner_id": "next", "token": 5},
    }
    confirm.assert_awaited_once()


async def test_playbook_scope_must_match_the_repository_project(command_handler_factory):
    """Capability membership cannot bypass an exact project mismatch."""
    handler = await command_handler_factory()
    await _seed(handler)
    await handler.db.create_task(
        Task(
            id="next",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="Next",
            description="",
        )
    )
    confirm = AsyncMock(return_value=True)
    handler.orchestrator.aconfirm_integration_owner_handoff = confirm
    playbook = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["integration_transfer_owner"]
        ),
        project_id="other",
    )

    with principal_context(playbook):
        result = await handler.execute("integration_transfer_owner", _args("next"))

    assert result["outcome"] == "human_required"
    confirm.assert_not_awaited()


async def test_repeated_transfer_returns_the_existing_successor_fence(command_handler_factory):
    """A retry after response loss must not stop the new owner or advance twice."""
    handler = await command_handler_factory()
    await _seed(handler)
    await handler.db.create_task(
        Task(
            id="next",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="Next",
            description="",
        )
    )
    confirm = AsyncMock(return_value=True)
    handler.orchestrator.aconfirm_integration_owner_handoff = confirm

    first = await handler.execute("integration_transfer_owner", _args("next"))
    second = await handler.execute("integration_transfer_owner", _args("next"))

    assert first == second
    confirm.assert_awaited_once()
    assert (await _owner_row(handler))["fence_token"] == 5
