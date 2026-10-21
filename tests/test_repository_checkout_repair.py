"""Checkout repair preserves repository identity and requires attributable authority."""

from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.api.auth import RequestScope
from src.api.scope import check_command_scope
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.config import WorktreesConfig
from src.git.manager import GitError, GitManager
from src.models import (
    AgentProfile, Project, RepoConfig, RepoSourceType, SessionRecord, Task, Workspace,
    WorkspaceKind, WorktreeSentinel,
)
from src.orchestrator.worktree_manager import WorktreeSlotManager
from src.profiles.capabilities import CapabilityPolicy

COMMAND = "repair_repository_checkout"
URL = "https://github.com/acme/project.git"


@pytest.fixture
async def repair(command_handler_factory, tmp_path):
    handler = await command_handler_factory()
    await handler.db.create_project(Project(id="p", name="Project"))
    old = str(tmp_path / "missing")
    replacement = tmp_path / "live"
    replacement.mkdir()
    git = GitManager()
    await git._arun(["init", "--initial-branch=main"], cwd=str(replacement))
    await git._arun(["remote", "add", "origin", URL], cwd=str(replacement))
    handler.orchestrator.git = git
    await handler.db.create_repo(RepoConfig(
        id="repo", project_id="p", url=URL, source_type=RepoSourceType.LINK,
        source_path=old, checkout_base_path=old,
    ))
    await handler.db.create_workspace(Workspace(
        id="ws", project_id="p", workspace_path=str(replacement),
        source_type=RepoSourceType.LINK,
    ))
    args = dict(project_id="p", repository_id="repo", expected_path=old,
                replacement_path=str(replacement), reason="Relocated checkout")
    yield handler, args
    await handler.db.close()


async def audit(handler):
    return await handler.db.get_recent_events(
        event_type="project.repository_checkout_repaired", project_id="p",
    )


async def test_preview_apply_preserves_identity_and_records_before_after(repair):
    handler, args = repair
    before = await handler.db.get_repo("repo")
    before_config = {**asdict(before), "source_type": before.source_type.value}
    workspace = await handler.db.get_workspace("ws")
    preview = await handler.execute(COMMAND, args)
    assert preview["success"] and preview["preview"] and not preview["changed"]
    assert preview["before"] == before_config
    assert await handler.db.get_repo("repo") == before
    assert await audit(handler) == []
    result = await handler.execute(COMMAND, {**args, "apply": True})
    assert result["success"] and result["changed"] and not result["preview"]
    assert result["after"] == preview["after"]
    updated = await handler.db.get_repo("repo")
    assert {**asdict(updated), "source_type": updated.source_type.value} == result["after"]
    assert await handler.db.get_workspace("ws") == workspace
    assert result["after"] == {**before_config, "source_path": args["replacement_path"],
                              "checkout_base_path": args["replacement_path"]}
    events = await audit(handler)
    assert len(events) == 1 and events[0]["id"] == result["event_id"]
    assert json.loads(events[0]["payload"]) == {
        "repository_id": "repo", "before": preview["before"], "after": preview["after"],
        "operator_id": "human:local-operator", "reason": args["reason"],
    }
    assert (await handler.execute(COMMAND, {**args, "apply": True}))["error_code"] == (
        "repository_checkout_stale"
    )
    assert len(await audit(handler)) == 1


@pytest.mark.parametrize("change,code", [
    ({"project_id": "other"}, "repository_not_found"),
    ({"repository_id": "other"}, "repository_not_found"),
    ({"expected_path": "/wrong"}, "repository_checkout_stale"),
    ({"replacement_path": "relative"}, "replacement_unverified"),
    ({"reason": " "}, "invalid_arguments"),
    ({"apply": "true"}, "invalid_arguments"),
    ({"unexpected": True}, "invalid_arguments"),
])
async def test_refusals_do_not_write(repair, change, code):
    handler, args = repair
    before = await handler.db.get_repo("repo")
    result = await handler.execute(COMMAND, {**args, **change, "apply": change.get("apply", True)})
    assert result["error_code"] == code
    assert await handler.db.get_repo("repo") == before
    assert await audit(handler) == []


@pytest.mark.parametrize("state", ["old_exists", "wrong_origin", "unknown_origin", "not_git"])
async def test_unverified_paths_do_not_write(repair, state):
    from pathlib import Path

    handler, args = repair
    if state == "old_exists":
        Path(args["expected_path"]).mkdir()
    elif state == "unknown_origin":
        await handler.db.update_repo("repo", url="")
    elif state == "not_git":
        replacement = Path(args["replacement_path"]).parent / "non-git"
        replacement.mkdir()
        args["replacement_path"] = str(replacement)
    else:
        await handler.orchestrator.git._arun(
            ["remote", "set-url", "origin", "https://github.com/acme/other.git"],
            cwd=args["replacement_path"],
        )
    before = await handler.db.get_repo("repo")
    result = await handler.execute(COMMAND, {**args, "apply": True})
    assert not result["success"]
    assert await handler.db.get_repo("repo") == before
    assert await audit(handler) == []


async def test_concurrent_config_edit_refuses_repair(repair, monkeypatch):
    handler, args = repair
    original = handler.orchestrator.git.aget_remote_url

    async def remote(*a, **kw):
        await handler.db.update_repo("repo", default_branch="new-main")
        return await original(*a, **kw)

    monkeypatch.setattr(handler.orchestrator.git, "aget_remote_url", remote)
    result = await handler.execute(COMMAND, {**args, "apply": True})
    assert result["error_code"] == "repository_checkout_stale"
    repo = await handler.db.get_repo("repo")
    assert repo.source_path == args["expected_path"] and repo.default_branch == "new-main"
    assert await audit(handler) == []


async def test_audit_failure_rolls_back(repair, monkeypatch):
    handler, args = repair
    before = await handler.db.get_repo("repo")
    monkeypatch.setattr(handler.db, "log_event", AsyncMock(side_effect=RuntimeError("audit failed")))
    result = await handler.execute(COMMAND, {**args, "apply": True})
    assert result["error"] == "audit failed"
    assert await handler.db.get_repo("repo") == before


@pytest.mark.parametrize("elevated,project_id,allowed", [
    (False, "p", False), (True, "p", False), (True, None, True),
])
def test_http_scope_requires_global_authority(elevated, project_id, allowed):
    scope = RequestScope(kind="session", session_id="s", project_id=project_id, elevated=elevated)
    assert (check_command_scope(COMMAND, {}, scope) is None) is allowed


async def test_direct_worker_dispatch_cannot_bypass_authority(repair):
    handler, args = repair
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION, session_id="worker", project_id="p",
        policy=CapabilityPolicy.from_namespaces(
            harness_tools=["Bash"], aq_commands=[COMMAND],
        ),
    )
    with principal_context(principal):
        result = await handler._cmd_repair_repository_checkout({**args, "apply": True})
    assert result["error_code"] == "global_operator_required"
    assert await audit(handler) == []


@pytest.mark.parametrize("live_instance,allowed", [("current", True), ("replaced", False)])
async def test_live_global_supervisor_identity_is_verified(repair, live_instance, allowed):
    handler, args = repair
    await handler.db.create_profile(AgentProfile(id="supervisor", name="Supervisor", harness="codex"))
    await handler.db.create_session(SessionRecord(
        id="supervisor-session", project_id=None, profile_id="supervisor",
        harness="codex", provider="fake",
        name="supervisor-global", lifecycle="named", state="running", work_dir="/tmp",
        epoch="test", instance_token=live_instance, started_at=1,
    ))
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION, session_id="supervisor-session", elevated=True,
        session_instance_token="current",
        policy=CapabilityPolicy.from_namespaces(aq_commands=[COMMAND]),
    )
    with principal_context(principal):
        result = await handler._cmd_repair_repository_checkout({**args, "apply": True})
    assert result["success"] is allowed
    if allowed:
        assert json.loads((await audit(handler))[0]["payload"])["operator_id"] == (
            "supervisor session:supervisor-session"
        )
    else:
        assert result["error_code"] == "global_operator_required"
        assert await audit(handler) == []


async def test_restored_old_path_during_validation_refuses_apply(repair, monkeypatch):
    from pathlib import Path

    handler, args = repair
    original = handler.orchestrator.git.aget_remote_url

    async def remote(*a, **kw):
        Path(args["expected_path"]).mkdir()
        return await original(*a, **kw)

    monkeypatch.setattr(handler.orchestrator.git, "aget_remote_url", remote)
    result = await handler.execute(COMMAND, {**args, "apply": True})
    assert result["error_code"] == "checkout_not_missing"
    assert (await handler.db.get_repo("repo")).source_path == args["expected_path"]
    assert await audit(handler) == []


BASE_COMMAND = "repair_workspace_checkout"


@pytest.fixture
async def base_repair(command_handler_factory, tmp_path):
    handler = await command_handler_factory()
    await handler.db.create_project(Project(id="p", name="Project", repo_url=URL))
    await handler.db.upsert_workspace_kind(WorkspaceKind(
        project_id="p", id="project-repo", is_git_repo=True, mode="worktree",
    ))
    git = handler.orchestrator.git = GitManager()
    old, replacement = tmp_path / "windows clone", tmp_path / "linux clone"
    for path in (old, replacement):
        path.mkdir()
        await git._arun(["init", "--initial-branch=main"], cwd=str(path))
        await git._arun(["remote", "add", "origin", URL], cwd=str(path))
        await git._arun(["-c", "user.name=T", "-c", "user.email=t@example.com",
                         "commit", "--allow-empty", "-m", "seed"], cwd=str(path))
    slot = replacement / ".aq" / "worktrees" / "slot-0"
    await git._arun(["worktree", "add", "-b", "slot-task", str(slot)], cwd=str(replacement))
    await handler.db.create_workspace(Workspace(
        id="base", project_id="p", workspace_path=str(old), source_type=RepoSourceType.LINK,
    ))
    await handler.db.create_workspace(Workspace(
        id="slot", project_id="p", workspace_path=str(slot), source_type=RepoSourceType.WORKTREE,
        base_workspace_id="base", slot_index=0,
    ))
    WorktreeSlotManager.write_sentinel(slot, WorktreeSentinel(
        slot="slot-0", slot_index=0, project_id="p", base_workspace_id="base", workspace_id="slot",
    ))
    args = dict(project_id="p", workspace_id="base", expected_path=str(old),
                replacement_path=str(replacement), reason="Correct base clone registration")
    yield handler, args
    await handler.db.close()


async def base_events(handler):
    return await handler.db.get_recent_events(
        event_type="project.workspace_checkout_repaired", project_id="p",
    )


@pytest.mark.parametrize("old_missing", [False, True])
async def test_independent_base_clone_repair_restores_real_slot_preflight(base_repair, old_missing):
    handler, args = base_repair
    if old_missing:
        Path(args["expected_path"]).rename(Path(args["expected_path"]).with_name("retained old clone"))
    mgr = WorktreeSlotManager(db=handler.db, git=handler.orchestrator.git, bus=None,
                              config=WorktreesConfig(), git_mutex=lambda _: None)
    await handler.db.create_task(Task(id="task", project_id="p", title="Retained", description=""))
    await handler.db.update_workspace("slot", locked_by_task_id="task", generation=7)
    slot = await handler.db.get_workspace("slot")
    task = SimpleNamespace(id="task", project_id="p", claim_epoch=1)
    with pytest.raises(GitError, match="unproved repository identity"):
        await mgr.ensure_slot_checkout(slot, task, recover_merge=False)
    dirty = Path(slot.workspace_path, "retained.txt")
    dirty.write_text("preserve this work\n")
    claim = Path(slot.workspace_path, ".aq", "claim.json")
    claim.parent.mkdir()
    claim.write_text('{"task_id":"task","claim_epoch":1}')
    sentinel = mgr.sentinel_path(slot.workspace_path).read_bytes()
    before = await handler.db.get_workspace("base")
    preview = await handler.execute(BASE_COMMAND, args)
    assert preview["success"], preview
    assert preview["preview"] and not preview["changed"]
    if old_missing:
        assert preview["identity"]["old"] is None
    else:
        assert preview["identity"]["old"]["common_directory"] != (
            preview["identity"]["replacement"]["common_directory"]
        )
    assert await handler.db.get_workspace("base") == before
    assert await base_events(handler) == []
    result = await handler.execute(BASE_COMMAND, {**args, "apply": True,
                                                 "expected_proof": preview["proof"]})
    assert result["success"] and result["changed"], result
    assert result["after"] == {**result["before"], "workspace_path": args["replacement_path"]}
    assert await handler.db.get_workspace("slot") == slot
    assert dirty.read_text() == "preserve this work\n"
    assert claim.read_text() == '{"task_id":"task","claim_epoch":1}'
    assert mgr.sentinel_path(slot.workspace_path).read_bytes() == sentinel
    assert Path(args["expected_path"], ".git").is_dir() is (not old_missing)
    await mgr.ensure_slot_checkout(slot, task, recover_merge=False)
    events = await base_events(handler)
    assert len(events) == 1 and events[0]["id"] == result["event_id"]
    payload = json.loads(events[0]["payload"])
    assert payload["before"] == preview["before"] and payload["after"] == result["after"]
    assert payload["operator_id"] == "human:local-operator" and payload["proof"] == preview["proof"]


@pytest.mark.parametrize("problem", [
    "third_clone", "wrong_origin", "missing_slot", "wrong_sentinel", "base_lock", "pin",
    "registered_target", "base_claim", "base_session", "linked_target", "wrong_project",
    "relative_origins", "split_family", "cross_project_target",
])
async def test_base_repair_refuses_unproved_identity_and_busy_base(base_repair, problem):
    handler, args = base_repair
    slot = await handler.db.get_workspace("slot")
    git = handler.orchestrator.git
    if problem == "third_clone":
        third = Path(args["replacement_path"]).parent / "third"
        await git._arun(["clone", args["replacement_path"], str(third)])
        await git._arun(["remote", "set-url", "origin", URL], cwd=str(third))
        args["replacement_path"] = str(third)
    elif problem == "wrong_origin":
        await git._arun(["remote", "set-url", "origin", "https://github.com/acme/other.git"],
                        cwd=args["replacement_path"])
    elif problem == "missing_slot":
        await handler.db.update_workspace("slot", workspace_path=str(Path(slot.workspace_path, "missing")))
    elif problem == "wrong_sentinel":
        sentinel = WorktreeSlotManager.read_sentinel(slot.workspace_path)
        sentinel.base_workspace_id = "other"
        WorktreeSlotManager.write_sentinel(slot.workspace_path, sentinel)
    elif problem == "base_lock":
        await handler.db.create_task(Task(
            id="busy", project_id="p", title="busy", description="",
        ))
        await handler.db.update_workspace("base", locked_by_task_id="busy")
    elif problem == "pin":
        await handler.db.update_workspace("base", job_pin_count=1)
    elif problem in {"registered_target", "cross_project_target"}:
        if problem == "cross_project_target":
            await handler.db.create_project(Project(id="other", name="Other"))
        await handler.db.create_workspace(Workspace(
            id="other", project_id="other" if problem == "cross_project_target" else "p",
            workspace_path=args["replacement_path"],
            source_type=RepoSourceType.LINK,
        ))
    elif problem == "base_claim":
        claim = Path(args["replacement_path"], ".aq", "claim.json")
        claim.parent.mkdir(exist_ok=True)
        claim.write_text('{}')
    elif problem == "base_session":
        await handler.db.create_profile(AgentProfile(id="worker", name="Worker"))
        await handler.db.create_session(SessionRecord(
            id="busy", project_id="p", profile_id="worker", harness="codex", provider="fake",
            state="running", work_dir=args["expected_path"], epoch="test", started_at=1,
            name="busy", lifecycle="task", instance_token="current",
        ))
    elif problem == "linked_target":
        args["replacement_path"] = slot.workspace_path
        # Avoid the registered-path refusal to exercise the clone identity check.
        await handler.db.update_workspace("slot", workspace_path=str(Path(slot.workspace_path, "missing")))
    elif problem == "relative_origins":
        await handler.db.update_project("p", repo_url="")
        for path in (args["expected_path"], args["replacement_path"]):
            await git._arun(["remote", "set-url", "origin", "./remote.git"], cwd=path)
    elif problem == "split_family":
        other = Path(args["expected_path"], ".aq", "slot-1")
        await git._arun(["worktree", "add", "-b", "other-task", str(other)],
                        cwd=args["expected_path"])
        await handler.db.create_workspace(Workspace(
            id="zz-other-slot", project_id="p", workspace_path=str(other),
            source_type=RepoSourceType.WORKTREE, base_workspace_id="base", slot_index=1,
        ))
        WorktreeSlotManager.write_sentinel(other, WorktreeSentinel(
            slot="slot-1", slot_index=1, project_id="p", base_workspace_id="base",
            workspace_id="zz-other-slot",
        ))
    else:
        args["project_id"] = "other"
    before = await handler.db.get_workspace("base")
    result = await handler.execute(BASE_COMMAND, args)
    assert not result["success"], result
    if problem in {"third_clone", "split_family"}:
        assert result["error_code"] == "identity_unverified"
        assert "object store" in result["error"]
    assert await handler.db.get_workspace("base") == before
    assert await base_events(handler) == []


@pytest.mark.parametrize("change", ["generation", "origin", "slot_sentinel", "reason", "expected_path"])
async def test_base_apply_requires_unchanged_preview_proof(base_repair, change):
    handler, args = base_repair
    preview = await handler.execute(BASE_COMMAND, args)
    assert preview["success"], preview
    if change == "generation":
        await handler.db.update_workspace("slot", generation=1)
    elif change == "origin":
        await handler.orchestrator.git._arun(
            ["remote", "set-url", "origin", "https://github.com/acme/other.git"],
            cwd=args["replacement_path"],
        )
    elif change == "slot_sentinel":
        slot = await handler.db.get_workspace("slot")
        sentinel = WorktreeSlotManager.read_sentinel(slot.workspace_path)
        sentinel.branch = "changed"
        WorktreeSlotManager.write_sentinel(slot.workspace_path, sentinel)
    elif change == "reason":
        args["reason"] = "Changed explanation"
    else:
        await handler.db.update_workspace("base", workspace_path="/changed")
    before = await handler.db.get_workspace("base")
    result = await handler.execute(BASE_COMMAND, {**args, "apply": True,
                                                 "expected_proof": preview["proof"]})
    assert not result["success"], result
    assert await handler.db.get_workspace("base") == before
    assert await base_events(handler) == []


async def test_base_apply_requires_preview_and_audit_is_atomic(base_repair, monkeypatch):
    handler, args = base_repair
    result = await handler.execute(BASE_COMMAND, {**args, "apply": True})
    assert result["error_code"] == "proof_required"
    preview = await handler.execute(BASE_COMMAND, args)
    assert preview["success"], preview
    monkeypatch.setattr(handler.db, "log_event", AsyncMock(side_effect=RuntimeError("audit failed")))
    result = await handler.execute(BASE_COMMAND, {**args, "apply": True,
                                                 "expected_proof": preview["proof"]})
    assert result["error"] == "audit failed"
    assert (await handler.db.get_workspace("base")).workspace_path == args["expected_path"]


async def test_base_repair_worker_cannot_dispatch_directly(base_repair):
    handler, args = base_repair
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION, session_id="worker", project_id="p",
        policy=CapabilityPolicy.from_namespaces(aq_commands=[BASE_COMMAND]),
    )
    with principal_context(principal):
        result = await handler._cmd_repair_workspace_checkout(args)
    assert result["error_code"] == "global_operator_required"


@pytest.mark.parametrize("elevated,project_id,allowed", [
    (False, "p", False), (True, "p", False), (True, None, True),
])
def test_base_repair_http_scope_is_global_only(elevated, project_id, allowed):
    scope = RequestScope(kind="session", session_id="s", project_id=project_id, elevated=elevated)
    assert (check_command_scope(BASE_COMMAND, {}, scope) is None) is allowed
