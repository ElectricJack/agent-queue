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
from src.commands.project_commands import ProjectCommandsMixin
from src.commands.supervisor_authority import integration_operator
from src.database import Database
from src.database.tables import integration_branch_owners
from src.integration.owner_recovery import RecoveryOutcome
from src.models import AgentProfile, Project, RepoConfig, RepoSourceType, SessionRecord
from src.profiles.capabilities import DENY_ALL, CapabilityPolicy
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
        ("super-global-stopped", None, "supervisor", "stopped", "stopped"),
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


def _session(
    session_id: str,
    project_id: str | None,
    *,
    elevated: bool = True,
    policy: CapabilityPolicy = DENY_ALL,
) -> ExecutionPrincipal:
    return ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=policy,
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


async def test_release_stale_owners_runs_under_the_derived_operator_label(db, monkeypatch):
    run = AsyncMock(
        return_value={
            "outcome": "released",
            "project_id": "p",
            "dry_run": True,
            "count": 1,
            "outcomes": [],
            "leases": [],
        }
    )
    monkeypatch.setattr(
        "src.integration.stale_owners.stale_owner_release_for",
        lambda _orchestrator: SimpleNamespace(run=run),
    )
    handler = IntegrationCommandsMixin()
    handler.db = db
    handler.orchestrator = SimpleNamespace()

    local = await handler._cmd_integration_release_stale_owners(
        {"project_id": "p", "dry_run": True}
    )
    assert (local["success"], local["outcome"]) == (True, "released")
    assert run.await_args.args == ("p",)
    assert run.await_args.kwargs == {
        "principal": "human:local-operator",
        "dry_run": True,
        "older_than_seconds": None,
    }

    with principal_context(_session("super-p", "p")):
        await handler._cmd_integration_release_stale_owners(
            {"project_id": "p", "older_than": "2h"}
        )
    assert run.await_args.kwargs == {
        "principal": "supervisor session:super-p",
        "dry_run": False,
        "older_than_seconds": 7200.0,
    }

    run.reset_mock()
    invalid = await handler._cmd_integration_release_stale_owners(
        {"project_id": "p", "older_than": "soon"}
    )
    assert invalid["outcome"] == "invalid"
    with principal_context(_session("worker", "p")):
        refused = await handler._cmd_integration_release_stale_owners({"project_id": "p"})
    assert refused["outcome"] == "unauthorized"
    run.assert_not_awaited()


async def test_adopt_legacy_deliveries_runs_under_the_derived_operator_label(db, monkeypatch):
    run = AsyncMock(
        return_value={
            "outcome": "adopted",
            "project_id": "p",
            "dry_run": True,
            "count": 1,
            "outcomes": [],
        }
    )
    monkeypatch.setattr(
        "src.integration.legacy_deliveries.legacy_delivery_adoption_for",
        lambda _handler: SimpleNamespace(run=run),
    )
    handler = IntegrationCommandsMixin()
    handler.db = db
    handler.orchestrator = SimpleNamespace()

    local = await handler._cmd_integration_adopt_legacy_deliveries(
        {"project_id": "p", "dry_run": True}
    )
    assert (local["success"], local["outcome"]) == (True, "adopted")
    assert run.await_args.args == ("p",)
    assert run.await_args.kwargs == {
        "principal": "human:local-operator",
        "dry_run": True,
        "accept": (),
        "retire": (),
        "supersede": {},
        "reason": None,
    }

    with principal_context(_session("super-p", "p")):
        await handler._cmd_integration_adopt_legacy_deliveries(
            {
                "project_id": "p",
                "accept": ["c1"],
                "retire": ["c2"],
                "supersede": {"c3": "abcdef1234"},
                "reason": "no-code task",
            }
        )
    assert run.await_args.kwargs == {
        "principal": "supervisor session:super-p",
        "dry_run": False,
        "accept": ("c1",),
        "retire": ("c2",),
        "supersede": {"c3": "abcdef1234"},
        "reason": "no-code task",
    }

    run.reset_mock()
    for decision in (
        {"accept": ["c1"]},
        {"retire": ["c1"]},
        {"supersede": {"c1": "abcdef1234"}},
        {"supersede": {"c1": "HEAD~1"}, "reason": "not a commit id"},
    ):
        invalid = await handler._cmd_integration_adopt_legacy_deliveries(
            {"project_id": "p", **decision}
        )
        assert invalid["outcome"] == "invalid", decision
    for principal in (_session("worker", "p"), _session("super-stopped", "p")):
        with principal_context(principal):
            refused = await handler._cmd_integration_adopt_legacy_deliveries({"project_id": "p"})
        assert refused["outcome"] == "unauthorized"
    run.assert_not_awaited()


def test_operator_control_scope_defers_live_session_validation_to_the_handler():
    elevated = RequestScope(kind="session", session_id="supervisor", project_id="p", elevated=True)
    worker = RequestScope(kind="session", session_id="worker", project_id="p", elevated=False)
    for command in OPERATOR_INTEGRATION_CONTROLS:
        assert check_command_scope(command, {}, elevated) is None
        assert check_command_scope(command, {}, worker) is not None


def _handler_with_controls(db) -> tuple[IntegrationCommandsMixin, AsyncMock]:
    controls = AsyncMock()
    controls.status.return_value = {"outcome": "status"}
    controls.enable.return_value = {"outcome": "enabled", "generation": 1}
    handler = IntegrationCommandsMixin()
    handler.db = db
    handler.orchestrator = SimpleNamespace(integration_control_service=controls)
    return handler, controls


_ENABLE_ARGS = {
    "project_id": "p",
    "mode": "observe",
    "expected_generation": 0,
    "reason": "train cutover",
}


async def test_global_supervisor_reads_integration_status_for_any_project(db):
    """The global supervisor has no project scope, so status cannot match on it."""
    handler, controls = _handler_with_controls(db)
    with principal_context(_session("super-global", None)):
        for project_id in ("p", "other"):
            result = await handler._cmd_integration_status({"project_id": project_id})
            assert result["outcome"] == "status", project_id
    assert [call.args for call in controls.status.await_args_list] == [("p",), ("other",)]


async def test_global_supervisor_enables_integration_under_its_own_audit_label(db):
    handler, controls = _handler_with_controls(db)
    with principal_context(_session("super-global", None)):
        result = await handler._cmd_integration_enable(dict(_ENABLE_ARGS))
    assert result["outcome"] == "enabled"
    controls.enable.assert_awaited_once_with(
        "p",
        mode="observe",
        expected_generation=0,
        reason="train cutover",
        operator_id="supervisor session:super-global",
        waiver_id=None,
        interval_seconds=None,
    )


@pytest.mark.parametrize(
    "principal",
    [
        # A token that outlived its global supervisor.
        _session("super-global-stopped", None),
        # A projectless session that is not elevated: a manually opened terminal.
        _session("super-global", None, elevated=False),
        # Elevation alone is not enough; the row must be a named supervisor.
        _session("worker", None),
        _session("super-other", "other"),
        _session("worker", "other", elevated=False),
    ],
)
async def test_status_refuses_other_projects_and_projectless_non_supervisors(db, principal):
    handler, controls = _handler_with_controls(db)
    with principal_context(principal):
        result = await handler._cmd_integration_status({"project_id": "p"})
    assert result["outcome"] == "unauthorized"
    controls.status.assert_not_awaited()


@pytest.mark.parametrize(
    "principal",
    [
        _session("worker", "p", elevated=False),
        _session("worker", "p"),
        _session("worker", None, elevated=False),
        _session("super-global-stopped", None),
        _session("super-global", None, elevated=False),
    ],
)
async def test_workers_and_non_live_supervisors_cannot_enable_integration(db, principal):
    handler, controls = _handler_with_controls(db)
    with principal_context(principal):
        result = await handler._cmd_integration_enable(dict(_ENABLE_ARGS))
    assert result["outcome"] == "unauthorized"
    controls.enable.assert_not_awaited()


def test_global_supervisor_scope_admits_status_and_enable_and_worker_scope_does_not():
    global_supervisor = RequestScope(
        kind="session", session_id="super-global", project_id=None, elevated=True
    )
    worker = RequestScope(kind="session", session_id="worker", task_id="t", project_id="p")
    projectless_worker = RequestScope(kind="session", session_id="worker", project_id=None)
    for command in ("integration_status", "integration_enable"):
        args = {"project_id": "p"}
        assert check_command_scope(command, args, global_supervisor) is None, command
        assert args == {"project_id": "p"}, command
        assert check_command_scope(command, {"project_id": "p"}, projectless_worker), command
    assert check_command_scope("integration_enable", {"project_id": "p"}, worker)
    assert check_command_scope("integration_status", {"project_id": "other"}, worker)


_CONFIGURE = CapabilityPolicy.from_namespaces(aq_commands=["edit_project"])
_CONFIGURE_ARGS = {
    "project_id": "p",
    "integration_repository_id": "r",
    "expected_integration_generation": 3,
    "reason": "bind exact existing repository",
}


class _ConfigureHandler(ProjectCommandsMixin, IntegrationCommandsMixin):
    pass


def _configure_handler(db) -> tuple[_ConfigureHandler, AsyncMock]:
    controls = AsyncMock()
    controls.configure.return_value = {"outcome": "configured", "generation": 4}
    handler = _ConfigureHandler()
    handler.db = db
    handler.orchestrator = SimpleNamespace(integration_control_service=controls)
    return handler, controls


@pytest.mark.parametrize(
    ("principal", "expected_label"),
    [
        (None, "local:-"),
        (_session("super-p", "p", policy=_CONFIGURE), "supervisor session:super-p"),
        (_session("super-global", None, policy=_CONFIGURE), "supervisor session:super-global"),
    ],
)
async def test_live_supervisor_binds_integration_configuration(
    db, principal, expected_label
):
    """The train cutover binds repository, review mode and policy from the supervisor."""
    handler, controls = _configure_handler(db)
    if principal is None:
        result = await handler._cmd_edit_project(dict(_CONFIGURE_ARGS))
    else:
        with principal_context(principal):
            result = await handler._cmd_edit_project(dict(_CONFIGURE_ARGS))
    assert result["outcome"] == "configured"
    controls.configure.assert_awaited_once_with(
        "p",
        updates={"integration_repository_id": "r"},
        expected_generation=3,
        reason="bind exact existing repository",
        operator_id=expected_label,
    )


@pytest.mark.parametrize(
    "principal",
    [
        # ``edit_project`` without a live, named, same-project supervisor row.
        _session("worker", "p", policy=_CONFIGURE),
        _session("worker", "p", elevated=False, policy=_CONFIGURE),
        _session("super-stopped", "p", policy=_CONFIGURE),
        _session("super-other", "other", policy=_CONFIGURE),
        _session("super-global-stopped", None, policy=_CONFIGURE),
    ],
)
async def test_workers_and_stale_supervisors_cannot_bind_integration_configuration(
    db, principal
):
    handler, controls = _configure_handler(db)
    for update in (
        {"integration_repository_id": "r"},
        {"integration_mode": "pull_request"},
        {"hierarchical_integration_policy": {"parent": {}}},
    ):
        args = {"project_id": "p", "expected_integration_generation": 3, **update}
        with principal_context(principal):
            result = await handler._cmd_edit_project(args)
        assert "Integration configuration" in result["error"], update
    controls.configure.assert_not_awaited()


def test_integration_configuration_scope_admits_supervisors_and_refuses_workers():
    """Scope lets an elevated supervisor reach the handler's live-supervisor check."""
    elevated = RequestScope(kind="session", session_id="super-p", project_id="p", elevated=True)
    worker = RequestScope(kind="session", session_id="worker", task_id="t", project_id="p")
    for field in (
        "integration_repository",
        "integration_repository_id",
        "integration_mode",
        "hierarchical_integration_policy",
    ):
        args = {"project_id": "p", field: "x", "expected_integration_generation": 1}
        assert check_command_scope("edit_project", args, elevated) is None, field
        refusal = check_command_scope("edit_project", dict(args), worker)
        assert refusal == (
            "out of scope: integration configuration requires local operator or supervisor"
        ), field
