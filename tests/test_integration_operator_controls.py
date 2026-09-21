"""Supervisor authority for operational integration controls."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert

from src.api.auth import RequestScope
from src.api.scope import OPERATOR_INTEGRATION_CONTROLS, check_command_scope
from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.commands.supervisor_authority import integration_operator
from src.database import Database
from src.database.tables import integration_branch_owners
from src.integration.owner_recovery import RecoveryOutcome
from src.models import AgentProfile, Project, RepoConfig, RepoSourceType, SessionRecord
from src.profiles.capabilities import DENY_ALL
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db():
    database = Database(lease_dsn("integration-operator-controls"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Project"))
    await database.create_project(Project(id="other", name="Other project"))
    await database.create_profile(
        AgentProfile(
            id="supervisor",
            name="Supervisor",
            harness="codex",
            lifecycle="named",
            aq_commands=[],
            harness_tools=[],
            plugin_tools=[],
            needs_workspace=False,
        )
    )
    await database.create_profile(
        AgentProfile(
            id="worker",
            name="Worker",
            harness="codex",
            lifecycle="pool",
            aq_commands=[],
            harness_tools=[],
            plugin_tools=[],
            needs_workspace=False,
        )
    )
    for session_id, project_id, profile_id, state, desired_state in (
        ("super-p", "p", "supervisor", "running", "running"),
        ("super-global", None, "supervisor", "starting", "running"),
        ("super-stopped", "p", "supervisor", "stopped", "stopped"),
        ("super-other", "other", "supervisor", "draining", "running"),
        ("worker", "p", "worker", "running", "running"),
    ):
        await database.create_session(
            SessionRecord(
                id=session_id,
                project_id=project_id,
                profile_id=profile_id,
                harness="codex",
                provider="fake",
                name=session_id,
                lifecycle="named" if profile_id == "supervisor" else "pool",
                work_dir=f"/tmp/{session_id}",
                epoch="epoch",
                instance_token=f"token-{session_id}",
                started_at=time.time(),
                state=state,
                desired_state=desired_state,
            )
        )
    yield database
    await database.close()


def _session(session_id: str, project_id: str | None, *, elevated: bool = True) -> ExecutionPrincipal:
    return ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=DENY_ALL,
        session_id=session_id,
        project_id=project_id,
        elevated=elevated,
    )


@pytest.mark.parametrize(
    ("principal", "expected_label"),
    [
        (None, "human:local-operator"),
        (_session("super-p", "p"), "supervisor session:super-p"),
        (_session("super-global", None), "supervisor session:super-global"),
    ],
)
async def test_integration_operator_accepts_local_and_live_matching_supervisors(
    db, principal, expected_label
):
    if principal is None:
        label, refusal = await integration_operator(db, "p")
    else:
        with principal_context(principal):
            label, refusal = await integration_operator(db, "p")
    assert (label, refusal) == (expected_label, None)


@pytest.mark.parametrize(
    "principal",
    [
        _session("super-stopped", "p"),
        _session("worker", "p", elevated=False),
        _session("super-other", "other"),
        _session("worker", "p"),
    ],
)
async def test_integration_operator_refuses_non_live_or_wrong_project_sessions(db, principal):
    with principal_context(principal):
        label, refusal = await integration_operator(db, "p")
    assert label is None
    assert refusal is not None


async def test_all_operator_controls_share_the_live_supervisor_matrix(db):
    principals = (
        (None, True),
        (_session("super-p", "p"), True),
        (_session("super-global", None), True),
        (_session("super-stopped", "p"), False),
        (_session("worker", "p", elevated=False), False),
        (_session("super-other", "other"), False),
        (_session("worker", "p"), False),
    )
    for command in OPERATOR_INTEGRATION_CONTROLS:
        for principal, allowed in principals:
            if principal is None:
                label, refusal = await integration_operator(db, "p")
            else:
                with principal_context(principal):
                    label, refusal = await integration_operator(db, "p")
            assert (label is not None) is allowed, command
            assert (refusal is None) is allowed, command


async def test_release_owner_passes_derived_operator_label_and_dry_run(db, monkeypatch):
    await db.create_repo(
        RepoConfig(
            id="r", project_id="p", source_type=RepoSourceType.CLONE, url="https://example.test/r"
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="owner", repository_id="r", ref="aq/task", owner_id="task",
                owner_role="worker", fence_token=1, handoff_state="attached",
                created_at=1.0, updated_at=1.0,
            )
        )
    recover_many = AsyncMock(
        return_value=[
            RecoveryOutcome("owner", "released", None, {"planned": []}, dry_run=True)
        ]
    )
    monkeypatch.setattr(
        "src.integration.owner_recovery.owner_recovery_for",
        lambda _orchestrator: SimpleNamespace(recover_many=recover_many),
    )
    handler = IntegrationCommandsMixin()
    handler.db = db
    handler.orchestrator = SimpleNamespace()

    local = await handler._cmd_integration_release_owner(
        {"owner_row_id": "owner", "dry_run": True}
    )
    assert local["outcome"] == "released"
    assert recover_many.await_args.kwargs == {
        "principal": "human:local-operator", "dry_run": True
    }

    with principal_context(_session("super-p", "p")):
        supervisor = await handler._cmd_integration_release_owner({"owner_row_id": "owner"})
    assert supervisor["outcome"] == "released"
    assert recover_many.await_args.kwargs == {
        "principal": "supervisor session:super-p", "dry_run": False
    }


def test_operator_control_scope_defers_live_session_validation_to_the_handler():
    elevated = RequestScope(kind="session", session_id="supervisor", project_id="p", elevated=True)
    worker = RequestScope(kind="session", session_id="worker", project_id="p", elevated=False)
    for command in OPERATOR_INTEGRATION_CONTROLS:
        assert check_command_scope(command, {}, elevated) is None
        assert check_command_scope(command, {}, worker) is not None
