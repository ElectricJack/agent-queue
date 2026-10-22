"""Author close retains real Git ancestry and exact evidence across retries."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.config import AppConfig
from src.database.tables import integration_close_attempts, wait_records
from src.git.github_contracts import GitHubRepositoryBinding
from src.git.manager import GitError
from src.integration.close_merge import ClosePolicy, CloseStore
from src.integration.close_runtime import DaemonClose
from src.integration.gitops import RetainedRepository
from src.models import Project, Task, RepoConfig, RepoSourceType, Agent, SessionRecord, TaskStatus
from tests.git_seed_helpers import clone_seed, init_bare_seed
from tests.test_integration_gitops import LocalGit, commit, git


@pytest.fixture
async def scenario(tmp_path, reuse_database):
    db = await reuse_database("close_merge")
    await db.create_project(Project(id="p", name="P"))
    await db.create_repo(RepoConfig(id="r", project_id="p", source_type=RepoSourceType.CLONE,
                                   url="https://github.com/acme/repo.git", default_branch="main"))
    await db.update_project("p", hierarchical_integration_mode="train", integration_repository_id="r")
    await db.create_task(Task(id="parent", project_id="p", title="parent", description="parent", branch_name="parent"))
    await db.create_task(Task(id="child", project_id="p", title="child", description="child", parent_task_id="parent",
                              branch_name="child", repo_id=None))
    remote, checkout = tmp_path / "remote.git", tmp_path / "source"
    init_bare_seed(remote)
    clone_seed(remote, checkout)
    git(checkout, "config", "user.name", "Tester")
    git(checkout, "config", "user.email", "tester@example.test")
    base = commit(checkout, {"base.txt": "base\n"})
    git(checkout, "push", "origin", "HEAD:parent")
    source = commit(checkout, {"source.txt": "source\n"})
    git(checkout, "push", "origin", "HEAD:child")
    manager = LocalGit(remote)
    cfg = AppConfig(data_dir=str(tmp_path))
    runtime = DaemonClose(SimpleNamespace(db=db, git=manager, config=cfg))
    repo = RetainedRepository("r", checkout,
        GitHubRepositoryBinding(1, "acme/repo"),
        "main")
    runtime.authorize = AsyncMock()
    runtime.policy = AsyncMock(return_value=ClosePolicy(True, digest="live"))
    runtime.check = AsyncMock(return_value={"state": "green"})
    runtime.handoff = AsyncMock()

    async def observe(row):
        git(checkout, "fetch", "origin")
        return repo, git(remote, "rev-parse", "parent")

    runtime.observe = observe
    task = SimpleNamespace(id="child", project_id="p", repo_id="r", claim_epoch=1)
    row = await runtime.store.proposal(task=task, session=SimpleNamespace(id="author"),
        source=source, parent=SimpleNamespace(id="parent", branch_name="parent"),
        args={"outcome": "pass", "summary": "source change"})
    return SimpleNamespace(db=db, runtime=runtime, row=row, repo=repo, remote=remote,
                           checkout=checkout, base=base, source=source, manager=manager)


async def test_clean_merge_is_two_parent_and_lost_push_reply_recovers(scenario):
    s = scenario
    s.manager.ambiguous = True
    result = await s.runtime.run(s.row["id"])
    assert result["completed"] and result["state"] == "finished"
    head = git(s.remote, "rev-parse", "parent")
    assert git(s.checkout, "show", "-s", "--format=%P", head).split() == [s.base, s.source]
    assert result["source_oid"] == s.source
    assert Path(result["candidate_path"]) != s.checkout
    pushes = s.manager.pushes
    assert (await s.runtime.run(s.row["id"]))["completed"]
    assert s.manager.pushes == pushes


async def test_conflict_budget_keeps_author_and_parent_movement_preserves_fix(scenario):
    s = scenario
    source = commit(s.checkout, {"base.txt": "source edit\n"}, base=s.source)
    parent = commit(s.checkout, {"base.txt": "parent edit\n"}, base=s.base)
    git(s.checkout, "push", "origin", "HEAD:parent")
    row = await s.runtime.store.save(s.row, source_oid=source)
    result = await s.runtime.run(row["id"])
    assert not result["completed"] and result["state"] == "needs_author"
    async with s.db._engine.connect() as conn:
        wait = (await conn.execute(select(wait_records).where(
            wait_records.c.id == result["budget_wait_id"]))).mappings().one()
    assert wait["origin"] == "integration" and wait["session_id"] is None
    assert wait["escalate_at"] - wait["created_at"] == 1200
    assert wait["ceiling_anchor_at"] is None
    candidate = Path(result["candidate_path"])
    fixed = commit(candidate, {"base.txt": "resolved by author\n", "fix.txt": "keep me\n"})
    newer = commit(s.checkout, {"sibling.txt": "new parent\n"}, base=parent)
    git(s.checkout, "push", "origin", "HEAD:parent")
    result = await s.runtime.run(row["id"])
    assert result["completed"]
    head = git(s.remote, "rev-parse", "parent")
    assert git(s.checkout, "show", "-s", "--format=%P", head).split() == [newer, fixed]
    assert git(s.checkout, "show", f"{head}:fix.txt") == "keep me"


@pytest.mark.parametrize("policy", [ClosePolicy(False), ClosePolicy(True, author_assist=False)])
async def test_disabled_or_publisher_only_does_not_prepare_or_publish(scenario, policy):
    s = scenario
    s.runtime.policy.return_value = policy
    result = await s.runtime.run(s.row["id"])
    assert result["completed"] and result["state"] == "deferred"
    assert result["candidate_path"] is None and s.manager.pushes == 0


async def test_changed_policy_during_checks_cannot_publish(scenario):
    s = scenario
    s.runtime.policy.side_effect = [ClosePolicy(True, digest=str(n)) for n in range(9)]
    result = await s.runtime.run(s.row["id"])
    assert result["state"] == "deferred" and s.manager.pushes == 0
    assert result["reason"] == "publication_retry_limit"


async def test_duplicate_generation_and_stale_save_are_fenced(scenario):
    s = scenario
    await s.runtime.store.save(s.row, last_failure="first")
    with pytest.raises(GitError, match="changed"):
        await s.runtime.store.save(s.row, last_failure="stale")
    assert (await CloseStore(s.db).get(s.row["id"]))["last_failure"] == "first"
    async with s.db._engine.connect() as conn:
        assert len((await conn.execute(select(integration_close_attempts))).all()) == 1


async def bind_author(s):
    await s.db.create_agent(Agent(id="worker", name="worker", profile_id="worker"))
    await s.db.update_task("parent", repo_id="r")
    await s.db.update_task("child", repo_id="r", claim_epoch=1)
    await s.db.transition_task("child", TaskStatus.IN_PROGRESS, context="test",
                               assigned_agent_id="worker")
    await s.db.create_session(SessionRecord(
        id="author", project_id="p", profile_id="worker", harness="codex", provider="fake",
        name="author", lifecycle="pool", work_dir=str(s.checkout), epoch="e", instance_token="i",
        started_at=1, task_id="child", agent_id="worker", state="running", claim_phase="active",
    ))
    git(s.checkout, "checkout", "-B", "child", s.source)


async def test_live_authority_stale_claim_and_explicit_hold(scenario):
    from src.operator_decisions import OperatorDecisions

    s = scenario
    await bind_author(s)
    await DaemonClose.authorize(s.runtime, s.row)
    await s.db.update_task("child", claim_epoch=2)
    with pytest.raises(GitError, match="delegation"):
        await DaemonClose.authorize(s.runtime, s.row)
    await s.db.update_task("child", claim_epoch=1)
    await OperatorDecisions(s.db).record({
        "object_kind": "task", "object_id": "parent", "effect": "hold", "operator": "human", "decision": "review",
        "source_ref": "test:hold",
        "source": "cli", "idempotency_key": "human-hold",
    }, recorded_by="operator")
    with pytest.raises(GitError, match="human hold"):
        await DaemonClose.authorize(s.runtime, s.row)


async def test_wait_delegation_is_exact_candidate_and_claim(scenario):
    from src.waits.adapters.close_scope import author_candidate
    from src.waits.conditions import Scope

    s = scenario
    await bind_author(s)
    await s.runtime.store.save(s.row, state="checking", candidate_oid=s.source)
    scope = Scope("p", "task", "child", session_id="author", claim_epoch=1)
    async with s.db._engine.connect() as conn:
        assert await author_candidate(conn, scope, operation_id=s.row["id"], head=s.source)
        assert not await author_candidate(conn, scope, operation_id="foreign", head=s.source)
        assert not await author_candidate(conn, scope, operation_id=s.row["id"], head=s.base)
        assert not await author_candidate(conn, Scope("p", "task", "child", session_id="other",
            claim_epoch=1), operation_id=s.row["id"], head=s.source)


async def test_fallback_preserves_author_candidate_without_changing_source(scenario):
    from src.integration.batches import BatchMember
    from src.integration.close_handoff import assembly_inputs

    s = scenario
    s.runtime.check.return_value = {"state": "failed"}
    pending = await s.runtime.run(s.row["id"])
    fixed = commit(Path(pending["candidate_path"]), {"fix.txt": "author repair"})
    s.runtime.check.return_value = {"state": "unavailable"}
    done = await s.runtime.run(s.row["id"])
    assert done["completed"] and done["state"] == "deferred"
    members = (BatchMember("child", s.source, s.base),)
    batch = SimpleNamespace(project_id="p", repository_id="r", target_ref="refs/heads/parent")
    # Publisher and author use different object stores in production.
    retained = s.checkout.parent / "publisher.git"
    git(s.checkout, "clone", "--bare", str(s.remote), str(retained))
    publisher_repo = RetainedRepository("r", retained, s.repo.binding, "main")
    inputs = await assembly_inputs(s.db, s.runtime.gitops, publisher_repo, batch, members)
    assert inputs[0].source_sha == fixed
    assert git(retained, "rev-parse", fixed) == fixed
    assert members[0].source_sha == s.source
    assert git(s.remote, "rev-parse", "parent") == s.base


async def test_source_generation_change_settles_old_budget(scenario):
    s = scenario
    s.runtime.check.return_value = {"state": "failed"}
    await s.runtime.run(s.row["id"])
    await s.runtime.store.retire_previous("child", "author", 1, "f" * 40)
    row = await s.runtime.store.get(s.row["id"])
    assert row["state"] == "cancelled" and row["candidate_path"]
    with pytest.raises(GitError, match="superseded"):
        await s.runtime.run(row["id"])


async def test_budget_expiry_only_notifies_supervisor_and_keeps_claim(scenario):
    from src.database.tables import messages, escalation_deliveries
    from src.waits.registry import build_registry
    from tests.test_wait_decisions import service_for

    s = scenario
    await bind_author(s)
    s.runtime.check.return_value = {"state": "failed"}
    result = await s.runtime.run(s.row["id"])
    async with s.db._engine.connect() as conn:
        wait = (await conn.execute(select(wait_records))).mappings().one()
    service = service_for(SimpleNamespace(db=s.db, registry=build_registry()),
                          [wait["escalate_at"] + 1])
    assert await service.lease()
    fired = await service.command("wait_deadline_fire", {
        "wait_id": result["budget_wait_id"], "deadline_type": "escalation", "generation": 1})
    assert fired["success"], fired
    async with s.db._engine.connect() as conn:
        notices = (await conn.execute(select(messages))).mappings().all()
        assert [n["to_id"] for n in notices] == ["supervisor-p"]
        assert not (await conn.execute(select(escalation_deliveries))).all()
        assert (await conn.execute(select(wait_records.c.outcome))).scalar_one() == "pending"
    assert (await s.db.get_task("child")).status == TaskStatus.IN_PROGRESS
    assert (await s.db.get_session("author")).desired_state == "running"


@pytest.mark.parametrize("principal,owner_kind,owner_id,project_id", [
    ("agent", "integration", "own", "p"),
    ("service", "task", "own", "p"),
    ("service", "integration", "foreign", "p"),
    ("service", "integration", "own", "foreign"),
])
async def test_close_budget_refuses_foreign_service_or_owner(
    scenario, principal, owner_kind, owner_id, project_id,
):
    from src.waits.conditions import Scope, WaitError
    from src.waits.registry import build_registry

    s = scenario
    registry = build_registry()
    condition = registry.normalise({"kind": "close_merge", "operation_id": s.row["id"],
                                    "budget_seconds": 90})
    scope = Scope(project_id, owner_kind, s.row["id"] if owner_id == "own" else owner_id,
                  principal=principal)
    async with s.db._engine.connect() as conn:
        with pytest.raises(WaitError) as error:
            await registry.verify(conn, condition, scope)
    assert error.value.code == "wait.out_of_scope"


@pytest.mark.parametrize("state", ["finished", "deferred", "cancelled"])
async def test_close_budget_reuses_one_wait_and_settles_from_operation(scenario, state):
    from src.waits.conditions import Condition
    from src.waits.registry import build_registry
    from tests.test_wait_decisions import service_for

    s = scenario
    s.runtime.policy.return_value = ClosePolicy(True, author_budget_seconds=90, digest="live")
    s.runtime.check.return_value = {"state": "failed"}
    first = await s.runtime.run(s.row["id"])
    second = await s.runtime.run(s.row["id"])
    assert first["budget_wait_id"] == second["budget_wait_id"]
    async with s.db._engine.connect() as conn:
        wait = (await conn.execute(select(wait_records))).mappings().one()
        assert wait["origin"] == "integration" and wait["owner_id"] == s.row["id"]
        assert wait["escalate_at"] - wait["created_at"] == 90
        assert wait["session_id"] is None and wait["outcome"] == "pending"
        condition = Condition.from_record(wait["condition"])
        assert (await build_registry().observe(conn, condition)).state == "unsatisfied"
    row = await s.runtime.store.get(s.row["id"])
    await s.runtime.store.save(row, state=state)
    service = service_for(SimpleNamespace(db=s.db, registry=build_registry()),
                          [wait["created_at"] + 1])
    assert await service.lease()
    assert await service.tick()
    async with s.db._engine.connect() as conn:
        settled = (await conn.execute(select(wait_records))).mappings().one()
        assert settled["outcome"] == "satisfied"
        assert settled["result_ref"] == "close_merge:" + s.row["id"]


async def test_live_policy_edits_and_retired_route_do_not_pin_close(scenario):
    from src.integration.close_runtime import current_policy

    s = scenario
    project = await s.db.get_project("p")
    first = await current_policy(s.db, project)
    assert first.enabled and first.author_budget_seconds == 1200
    project.hierarchical_integration_policy = {
        "close_merge": {"author_assist": False, "author_budget_seconds": 90},
        "parent": {"route": {"activation_id": "retired"},
                   "required_checks": {"names": ["new-check"]}},
    }
    changed = await current_policy(s.db, project)
    assert changed.enabled and not changed.author_assist
    assert changed.digest != first.digest and changed.author_budget_seconds == 90
    assert changed.required_checks["names"] == ["new-check"]
    project.hierarchical_integration_mode = "disabled"
    assert not (await current_policy(s.db, project)).enabled
    project.hierarchical_integration_mode = "development"
    assert (await current_policy(s.db, project)).enabled


async def test_changed_candidate_runs_fresh_checks_and_concurrent_visit_is_fenced(scenario):
    s = scenario
    s.runtime.check.return_value = {"state": "failed"}
    result = await s.runtime.run(s.row["id"])
    old = result["candidate_oid"]
    new = commit(Path(result["candidate_path"]), {"fix.txt": "fixed"})
    async with s.runtime.store.visit(s.row["id"]):
        with pytest.raises(GitError, match="already being continued"):
            await s.runtime.run(s.row["id"])
    s.runtime.check.return_value = {"state": "green"}
    result = await s.runtime.run(s.row["id"])
    assert result["completed"] and result["candidate_oid"] == new != old
    checked = [call.args[0]["candidate_oid"] for call in s.runtime.check.await_args_list]
    assert old in checked and checked[-2:] == [new, new]


async def test_checks_use_current_commands_and_exact_head_idempotency(scenario):
    from src.integration.close_checks import check_candidate
    from src.integration.close_runtime import current_policy
    from src.integration.models import HierarchicalIntegrationPolicy
    from tests.test_ci_completion import project_policy

    s = scenario
    raw = project_policy()
    name = raw["parent"]["required_checks"]["names"][0]
    raw["ci"] = {"source": "local", "commands": {name: "ruff check src"}}
    policy = HierarchicalIntegrationPolicy.model_validate(raw)
    assert "close_merge" not in policy.model_dump(mode="json")
    await s.db.update_project("p", hierarchical_integration_policy=policy.model_dump(mode="json"))
    s.runtime.orch.config.wait_policy.v2_enabled = True
    handler = SimpleNamespace(_cmd_job_submit_integration=AsyncMock(return_value={
        "success": True, "job": {"id": "j", "state": "queued", "exit_code": None}}))
    s.runtime.orch._command_handler = handler
    row = {**s.row, "candidate_oid": s.source}
    current = await current_policy(s.db, await s.db.get_project("p"))
    pending = await check_candidate(s.runtime, row, s.repo, current)
    assert pending["condition"] == {"kind": "job", "job_id": "j"}
    first = handler._cmd_job_submit_integration.call_args.args[0]
    assert first["input_ref"] == s.source and first["operation_id"] == s.row["id"]
    assert first["preset"] == "lint" and first["argv"] == ["src"]
    await check_candidate(s.runtime, row, s.repo, current)
    assert handler._cmd_job_submit_integration.call_args.args[0] == first
    await check_candidate(s.runtime, {**row, "candidate_oid": s.base}, s.repo, current)
    assert handler._cmd_job_submit_integration.call_args.args[0]["idempotency_key"] != first["idempotency_key"]


async def test_continue_close_cannot_delegate_another_authors_operation(scenario, monkeypatch):
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import PrincipalKind

    s = scenario
    handler = SimpleNamespace(db=s.db, _cmd_task_close=AsyncMock(return_value={"success": True}))
    principal = SimpleNamespace(kind=PrincipalKind.SESSION, project_id="p", session_id="other")
    monkeypatch.setattr("src.commands.integration_commands.current_principal", lambda: principal)
    result = await IntegrationCommandsMixin._cmd_integration_continue_close(
        handler, {"operation_id": s.row["id"]})
    assert not result["success"] and not handler._cmd_task_close.called
    principal.session_id = "author"
    result = await IntegrationCommandsMixin._cmd_integration_continue_close(
        handler, {"operation_id": s.row["id"]})
    assert result["success"]
    assert handler._cmd_task_close.call_args.args[0]["claim_epoch"] == 1
    assert handler._cmd_task_close.call_args.args[0]["task_id"] == "child"
