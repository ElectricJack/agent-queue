"""Audited first repository binding preserves worker publication authorization."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert

from src.api.auth import RequestScope
from src.api.scope import check_command_scope
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.tables import project_integration_leases, task_integration_checkpoints
from src.models import AgentProfile, Project, SessionRecord, Task, TaskStatus
from src.profiles.capabilities import CapabilityPolicy
from src.projects.github import GitHubError, GitHubErrorCode

URL = "https://github.com/ElectricJack/agent-q-sprint-eval.git"
OTHER_URL = "https://github.com/acme/another.git"
COMMAND = "bind_project_repository"


def _args(**overrides):
    return {
        "project_id": "p",
        "repo_url": URL,
        "expected_repo_url": "",
        "reason": "User-authorized repository created after init",
        **overrides,
    }


@pytest.fixture
async def binding(command_handler_factory, monkeypatch):
    handler = await command_handler_factory()
    await handler.db.create_project(Project(id="p", name="Initialized project"))
    await handler.db.create_task(
        Task(
            id="pending",
            project_id="p",
            title="Preserve pending implementation",
            description="Existing implementation awaiting a human publication gate",
            status=TaskStatus.IN_PROGRESS,
            branch_name="aq/pending",
        )
    )
    github = AsyncMock()
    monkeypatch.setattr(handler, "_github_client", lambda: github)
    yield handler, github
    await handler.db.close()


async def _audit(handler):
    return await handler.db.get_recent_events(event_type="project.repository_bound", project_id="p")


async def test_first_binding_is_audited_and_preserves_task_and_gate(binding):
    handler, github = binding
    gate_id, _ = await handler.db.create_gate(
        "p",
        "human",
        "Preserve publication approval",
        waiter_task_ids=["pending"],
    )
    gate = await handler.db.get_gate(gate_id)
    before = await handler.db.get_task("pending")
    result = await handler.execute(
        COMMAND, _args(repo_url="git@github.com:ElectricJack/agent-q-sprint-eval.git")
    )
    assert result["success"] and result["changed"]
    assert (await handler.db.get_project("p")).repo_url == URL
    assert await handler.db.get_task("pending") == before
    assert await handler.db.get_gate(gate_id) == gate
    github.validate_repository.assert_awaited_once_with(URL.removesuffix(".git"))
    audit = await _audit(handler)
    assert len(audit) == 1 and audit[0]["id"] == result["event_id"]
    assert json.loads(audit[0]["payload"]) == {
        "project_id": "p",
        "old_repo_url": "",
        "repo_url": URL,
        "reason": _args()["reason"],
        "operator_id": "human:local-operator",
    }

    stale = await handler.execute(COMMAND, _args())
    assert stale["error_code"] == "repository_binding_stale"
    repeat = await handler.execute(COMMAND, _args(expected_repo_url=URL))
    assert repeat == {"success": True, "project_id": "p", "repo_url": URL, "changed": False}
    assert len(await _audit(handler)) == 1
    assert github.validate_repository.await_count == 1


@pytest.mark.parametrize("url", [OTHER_URL, ""])
async def test_binding_cannot_be_reassigned_or_cleared(binding, url):
    handler, github = binding
    await handler.db.update_project("p", repo_url=URL)
    result = await handler.execute(COMMAND, _args(repo_url=url, expected_repo_url=URL))
    assert result["success"] is False
    assert (await handler.db.get_project("p")).repo_url == URL
    assert await _audit(handler) == []
    github.validate_repository.assert_not_awaited()


@pytest.mark.parametrize(
    "url",
    [
        "/tmp/origin.git",
        "file:///tmp/origin.git",
        "https://example.com/acme/repo",
        "http://github.com/acme/repo",
        "https://token-secret@github.com/acme/repo",
        "https://github.com/acme/repo?token=token-secret",
        "https://github.com/acme/repo#secret",
        "https://github.com/acme/repo\n",
        "-u origin",
        "",
    ],
)
async def test_invalid_urls_write_nothing_and_do_not_echo_secrets(binding, url, caplog):
    handler, github = binding
    caplog.set_level(logging.DEBUG)
    result = await handler.execute(COMMAND, _args(repo_url=url))
    assert result["error_code"] == "invalid_repository_url"
    assert "token-secret" not in json.dumps(result) + caplog.text
    assert (await handler.db.get_project("p")).repo_url == ""
    assert await _audit(handler) == []
    github.validate_repository.assert_not_awaited()
    for call in handler.orchestrator.bus.emit.await_args_list:
        assert "token-secret" not in json.dumps(call.args)


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason": " "},
        {"expected_repo_url": None},
        {"unexpected": "value"},
    ],
)
async def test_invalid_arguments_are_refused(binding, overrides):
    handler, github = binding
    result = await handler.execute(COMMAND, _args(**overrides))
    assert result["error_code"] == "invalid_arguments"
    github.validate_repository.assert_not_awaited()


async def test_missing_project_and_unsafe_edit_are_refused(binding):
    handler, github = binding
    result = await handler.execute(COMMAND, _args(project_id="missing"))
    assert result["error_code"] == "project_not_found"
    result = await handler.execute(
        "edit_project", {"project_id": "p", "repo_url": URL, "name": "changed"}
    )
    assert result["error_code"] == "repository_binding_required"
    assert (await handler.db.get_project("p")).name == "Initialized project"
    github.validate_repository.assert_not_awaited()


async def test_access_failure_writes_nothing(binding):
    handler, github = binding
    github.validate_repository.side_effect = GitHubError(
        GitHubErrorCode.REPOSITORY_INACCESSIBLE,
        "Check repository access",
    )
    result = await handler.execute(COMMAND, _args())
    assert result["error_code"] == "github_repository_inaccessible"
    assert (await handler.db.get_project("p")).repo_url == ""
    assert await _audit(handler) == []


async def test_legacy_null_url_is_an_empty_first_binding(binding):
    handler, _ = binding
    await handler.db.update_project("p", repo_url=None)
    result = await handler.execute(COMMAND, _args())
    assert result["success"]
    assert (await handler.db.get_project("p")).repo_url == URL
    assert len(await _audit(handler)) == 1


async def test_missing_expected_value_is_refused(binding):
    handler, github = binding
    args = _args()
    del args["expected_repo_url"]
    result = await handler.execute(COMMAND, args)
    assert result["error_code"] == "invalid_arguments"
    github.validate_repository.assert_not_awaited()


async def test_audit_failure_rolls_back_binding(binding, monkeypatch):
    handler, _ = binding
    monkeypatch.setattr(
        handler.db, "log_event", AsyncMock(side_effect=RuntimeError("audit unavailable"))
    )
    result = await handler.execute(COMMAND, _args())
    assert result["error"] == "audit unavailable"
    assert (await handler.db.get_project("p")).repo_url == ""
    assert await _audit(handler) == []


@pytest.mark.parametrize(
    "updates",
    [
        {"hierarchical_integration_mode": "observe"},
        {"hierarchical_integration_desired_mode": "development"},
        {"hierarchical_integration_draining": True},
    ],
)
async def test_integration_configuration_cannot_be_changed_underneath_delivery(binding, updates):
    handler, _ = binding
    # Derived fields deliberately use the test database, never an operator DB.
    async with handler.db._engine.begin() as conn:
        from sqlalchemy import update
        from src.database.tables import projects

        await conn.execute(update(projects).where(projects.c.id == "p").values(**updates))
    result = await handler.execute(COMMAND, _args())
    assert result["error_code"] == "repository_integration_active"
    assert (await handler.db.get_project("p")).repo_url == ""
    assert await _audit(handler) == []


@pytest.mark.parametrize("state", ["lease", "checkpoint"])
async def test_durable_publication_state_prevents_binding(binding, state):
    handler, _ = binding
    async with handler.db._engine.begin() as conn:
        if state == "lease":
            await conn.execute(
                insert(project_integration_leases).values(
                    project_id="p",
                    repository_id="repo",
                    batch_id="batch",
                    owner_id="owner",
                    fence_token=1,
                    heartbeat_at=1,
                    expires_at=2,
                )
            )
        else:
            await conn.execute(
                insert(task_integration_checkpoints).values(
                    task_id="pending",
                    repository_id="repo",
                    branch="aq/pending",
                    updated_at=1,
                )
            )
    result = await handler.execute(COMMAND, _args())
    assert result["error_code"] == "repository_integration_active"
    assert await _audit(handler) == []


async def test_publication_fence_refuses_binding_but_not_other_projects(binding):
    handler, _ = binding
    async with handler.db.project_repository_publication("p"):
        result = await handler.execute(COMMAND, _args())
        assert result["error_code"] == "repository_publication_busy"
        assert await _audit(handler) == []
    async with handler.db.project_repository_publication("another-project"):
        result = await handler.execute(COMMAND, _args())
        assert result["success"]


async def test_concurrent_first_bindings_commit_only_one_audit(binding):
    handler, _ = binding

    async def bind(url):
        return await handler.db.bind_project_repository(
            "p",
            repo_url=url,
            expected_repo_url="",
            reason="authorized",
            operator_id="operator",
        )

    results = await asyncio.gather(bind(URL), bind(OTHER_URL))
    winner = [r for r in results if r["success"]]
    assert len(winner) == 1
    assert (await handler.db.get_project("p")).repo_url == winner[0]["repo_url"]
    assert len(await _audit(handler)) == 1


async def test_cas_is_rechecked_after_repository_access_validation(binding):
    handler, github = binding

    async def access(*args):
        await handler.db.update_project("p", repo_url=OTHER_URL)

    github.validate_repository.side_effect = access
    result = await handler.execute(COMMAND, _args())
    assert result["error_code"] == "repository_binding_stale"
    assert (await handler.db.get_project("p")).repo_url == OTHER_URL
    assert await _audit(handler) == []


@pytest.mark.parametrize(
    "elevated,project_id,allowed",
    [
        (False, "p", False),
        (True, "p", False),
        (True, None, True),
    ],
)
def test_http_scope_is_global_admin_only(elevated, project_id, allowed):
    scope = RequestScope(
        kind="session", session_id="session", project_id=project_id, elevated=elevated
    )
    assert (check_command_scope(COMMAND, _args(), scope) is None) is allowed


@pytest.mark.parametrize(
    "kind,profile_id,project_id,state,elevated,allowed",
    [
        (PrincipalKind.SESSION, "supervisor", None, "running", True, True),
        (PrincipalKind.SESSION, "supervisor", "p", "running", True, False),
        (PrincipalKind.SESSION, "worker-codex", None, "running", True, False),
        (PrincipalKind.SESSION, "supervisor", None, "stopped", True, False),
        (PrincipalKind.SESSION, "supervisor", None, "running", False, False),
        (PrincipalKind.PLAYBOOK, "supervisor", None, "running", True, False),
        (PrincipalKind.SERVICE, "supervisor", None, "running", True, False),
    ],
)
async def test_direct_dispatch_requires_live_global_supervisor(
    binding,
    kind,
    profile_id,
    project_id,
    state,
    elevated,
    allowed,
):
    handler, github = binding
    await handler.db.create_profile(AgentProfile(id=profile_id, name=profile_id, harness="codex"))
    await handler.db.create_session(
        SessionRecord(
            id="session",
            project_id=project_id,
            profile_id=profile_id,
            harness="codex",
            provider="fake",
            name="supervisor-test",
            lifecycle="named",
            state=state,
            work_dir="/tmp",
            epoch="test",
            instance_token="test-instance",
            started_at=1,
        )
    )
    principal = ExecutionPrincipal(
        kind=kind,
        session_id="session",
        project_id=project_id,
        elevated=elevated,
        policy=CapabilityPolicy.from_namespaces(aq_commands=[COMMAND]),
    )
    with principal_context(principal):
        result = await handler.execute(COMMAND, _args())
    assert result["success"] is allowed
    if allowed:
        assert (
            json.loads((await _audit(handler))[0]["payload"])["operator_id"]
            == "supervisor session:session"
        )
    else:
        assert result["error_code"] == "global_operator_required"
        assert await _audit(handler) == []
        github.validate_repository.assert_not_awaited()


async def test_supervisor_authority_is_rechecked_after_access_validation(binding):
    handler, github = binding
    await handler.db.create_profile(
        AgentProfile(id="supervisor", name="Supervisor", harness="codex")
    )
    await handler.db.create_session(
        SessionRecord(
            id="session",
            project_id=None,
            profile_id="supervisor",
            harness="codex",
            provider="fake",
            name="supervisor-test",
            lifecycle="named",
            state="running",
            work_dir="/tmp",
            epoch="test",
            instance_token="test-instance",
            started_at=1,
        )
    )

    async def access(*args):
        await handler.db.update_session("session", desired_state="stopped")

    github.validate_repository.side_effect = access
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        session_id="session",
        elevated=True,
        policy=CapabilityPolicy.from_namespaces(aq_commands=[COMMAND]),
    )
    with principal_context(principal):
        result = await handler.execute(COMMAND, _args())
    assert result["error_code"] == "global_operator_required"
    assert (await handler.db.get_project("p")).repo_url == ""
    assert await _audit(handler) == []


async def test_global_scope_does_not_supply_a_missing_command_capability(binding):
    handler, github = binding
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        session_id="missing-session",
        elevated=True,
        policy=CapabilityPolicy.from_namespaces(aq_commands=[]),
    )
    with principal_context(principal):
        result = await handler.execute(COMMAND, _args())
    assert result["success"] is False
    assert result["error_code"] == "capability_denied"
    assert (await handler.db.get_project("p")).repo_url == ""
    github.validate_repository.assert_not_awaited()
