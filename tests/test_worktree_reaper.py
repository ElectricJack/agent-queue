"""Worktree slot reaper + boot adoption + branch pruning (P4).

Worktree-execution implementation spec §6.2 (cascade), §6.4 (adoption),
§6.5 (pruning).  Real bare-origin + real slot manager fixtures — see
``test_worktree_prepare.py`` for the pattern.

Liveness guard (design §5) is exercised by monkey-patching the
process-table scanner rather than spawning real long-lived processes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

from sqlalchemy import update

import pytest

from src.config import DatabaseConfig, AppConfig
from src.models import (
    Agent,
    AgentProfile,
    KIND_MODE_WORKTREE,
    Project,
    RepoSourceType,
    SYSTEM_KIND_SCOPE,
    Task,
    Workspace,
    WorkspaceKind,
)
from src.orchestrator import Orchestrator
from src.orchestrator.worktree_manager import EXCLUDE_BEGIN
from src.orchestrator.workspace_binding_audit import reconcile_slot_bindings
from src.runtimes.base import Runtime
from tests.db_fixtures import lease_dsn


def _git(args: list[str], cwd) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t.com", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


class _NullRuntimeFactory:
    def create(self, agent_type, profile=None, llm_logger=None) -> Runtime:
        raise AssertionError("no runtime should be created in these tests")


@pytest.fixture
def base_repo(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
    )
    base = tmp_path / "base"
    subprocess.run(
        ["git", "clone", str(origin), str(base)], check=True, capture_output=True
    )
    (base / "README.md").write_text("init\n")
    _git(["add", "-A"], cwd=base)
    _git(["commit", "-m", "init"], cwd=base)
    _git(["push", "origin", "main"], cwd=base)
    return base


async def _orch(tmp_path, *, worktrees_enabled=True):
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        database=DatabaseConfig(url=lease_dsn("aq.db")),
        workspace_dir=str(tmp_path / "workspaces"),
    )
    config.worktrees.enabled = worktrees_enabled
    o = Orchestrator(config, runtimes=_NullRuntimeFactory())
    await o.initialize()
    return o


async def _seed(o, base_repo, *, cap=2):
    await o.db.create_project(
        Project(id="p1", name="alpha", repo_url="",
                repo_default_branch="main", max_concurrent_agents=cap)
    )
    await o.db.upsert_workspace_kind(
        WorkspaceKind(
            project_id=SYSTEM_KIND_SCOPE, id="project-repo",
            is_git_repo=True, lockable=True, writable=True,
            mode=KIND_MODE_WORKTREE, default_lock_mode="exclusive",
        )
    )
    await o.db.create_workspace(
        Workspace(
            id="ws-base", project_id="p1", workspace_path=str(base_repo),
            source_type=RepoSourceType.CLONE, kind_id="project-repo",
        )
    )
    await o.db.upsert_profile(
        AgentProfile(id="p", name="p", model="claude-haiku-4-5-20251001",
                     permission_mode="bypassPermissions")
    )


async def _make_slot(o, base_repo, task_id="tsk-1"):
    await o.db.create_task(Task(id=task_id, project_id="p1",
                                title=task_id, description=""))
    await o.db.create_agent(Agent(id=task_id + "-a", name=task_id + "-a",
                                  profile_id="p"))
    t = await o.db.get_task(task_id)
    a = await o.db.get_agent(task_id + "-a")
    slot_path = await o._prepare_workspace(t, a)
    return await o.db.get_workspace_for_task(task_id), Path(slot_path)


@pytest.mark.parametrize("case", [
    "aligned", "foreign_claim", "missing_receipt", "preparing", "stopped",
    "wrong_branch", "unknown_liveness", "malformed_claim", "changed_generation",
    "deferred_owner", "stale_receipt", "current_owner_and_pin", "foreign_owner",
    "finished_job_pin", "old_generation_pin", "unproved_pin_holder", "preparing_with_owner",
])
async def test_existing_binding_reconciliation_preserves_source_and_owner(
    tmp_path, base_repo, monkeypatch, case,
):
    from src.claim_file import write_claim_file
    from src.models import SessionRecord

    o = await _orch(tmp_path)
    try:
        await _seed(o, base_repo)
        ws, path = await _make_slot(o, base_repo)
        branch = _git(["branch", "--show-current"], path)
        await o.db.update_task("tsk-1", assigned_agent_id="tsk-1-a", claim_epoch=3,
                               branch_name=branch)
        await o.db.create_session(SessionRecord(
            id="audit-holder", task_id="tsk-1", project_id="p1", agent_id="tsk-1-a",
            profile_id="p", harness="codex", provider="fake", name="audit-holder",
            lifecycle="pool", work_dir=str(path), epoch="e", instance_token="audit-token",
            state="stopped" if case in ("stopped", "unproved_pin_holder") else "running",
            desired_state="stopped" if case in ("stopped", "unproved_pin_holder")
            else "running",
            claim_phase="preparing" if case in ("preparing", "preparing_with_owner")
            else "active",
            last_claim_epoch=3, started_at=1,
        ))
        write_claim_file(str(path), {"task_id": "tsk-1", "claim_epoch": 3,
                                    "session_id": "foreign" if case == "foreign_claim"
                                    else "audit-holder"})
        if case == "malformed_claim":
            (path / ".aq" / "claim.json").write_text("{broken")
        if case != "missing_receipt":
            await o.db.set_task_meta("tsk-1", "claim_workspace", {
                "workspace_id": ws.id, "generation": ws.generation + (case == "stale_receipt"),
                "session_id": "audit-holder", "instance_token": "audit-token", "claim_epoch": 3,
            })
        if case == "deferred_owner":
            from sqlalchemy import insert
            from src.database.tables import integration_branch_owners
            async with o.db.immediate() as conn:
                await conn.execute(insert(integration_branch_owners).values(
                    id="retained-owner", repository_id="p1", ref=branch,
                    owner_id="tsk-1", owner_role="integration", fence_token=7,
                    handoff_state="handoff_pending", session_id="audit-holder",
                    workspace_id=ws.id, created_at=1, updated_at=1,
                ))
        if case in ("current_owner_and_pin", "foreign_owner", "preparing_with_owner",
                    "unproved_pin_holder"):
            from sqlalchemy import insert
            from src.database.tables import integration_branch_owners
            async with o.db.immediate() as conn:
                await conn.execute(insert(integration_branch_owners).values(
                    id="live-owner", repository_id="p1", ref=f"refs/heads/{branch}",
                    owner_id="other-task" if case == "foreign_owner" else "tsk-1",
                    owner_role="worker", fence_token=2, handoff_state="attached",
                    session_id="audit-holder", workspace_id=ws.id, created_at=1, updated_at=1,
                ))
        if case in ("current_owner_and_pin", "finished_job_pin", "old_generation_pin",
                    "unproved_pin_holder"):
            from sqlalchemy import insert
            from src.database.tables import job_workspace_pins, jobs
            async with o.db.immediate() as conn:
                await conn.execute(insert(jobs).values(
                    id="pinned-job", project_id="p1", task_id="tsk-1", owner_kind="task",
                    owner_id="tsk-1", submitter_session_id="audit-holder", claim_epoch=3,
                    idempotency_key="k", request_hash="h", preset="test", preset_version=1,
                    argv=[], contract={}, workspace_id=ws.id,
                    workspace_generation=ws.generation, input_mode="live", job_class="shared",
                    weight=1, priority_band=2,
                    state="succeeded" if case == "finished_job_pin" else "running",
                    submitted_at=1, queue_deadline=2, run_timeout=60, runner_nonce="n",
                ))
                await conn.execute(insert(job_workspace_pins).values(
                    job_id="pinned-job", workspace_id=ws.id, created_at=1,
                    generation=ws.generation - (case == "old_generation_pin"),
                ))
        if case == "wrong_branch":
            _git(["switch", "--detach"], path)
        (path / "README.md").write_text("retained staged checkpoint\n")
        _git(["add", "README.md"], path)
        (path / "untracked-checkpoint").write_bytes(b"retained progress")
        before = (_git(["rev-parse", "HEAD"], path), _git(["status", "--porcelain"], path),
                  (path / ".aq" / "claim.json").read_bytes(),
                  (path / ".aq-worktree.json").read_bytes())
        meta = await o.db.get_all_task_meta("tsk-1")
        mgr = o._worktree_slots()
        monkeypatch.setattr(mgr, "_slot_is_live", AsyncMock(
            side_effect=PermissionError("process inventory unknown")
            if case == "unknown_liveness" else None, return_value=True,
        ))
        if case == "changed_generation":
            original = o.db.get_workspace
            calls = 0

            async def raced(workspace_id):
                nonlocal calls
                if workspace_id == ws.id:
                    calls += 1
                if workspace_id == ws.id and calls == 2:
                    from src.database.tables import workspaces
                    async with o.db.immediate() as conn:
                        await conn.execute(update(workspaces).where(
                            workspaces.c.id == workspace_id,
                        ).values(generation=workspaces.c.generation + 1))
                return await original(workspace_id)

            monkeypatch.setattr(o.db, "get_workspace", raced)
        findings = await reconcile_slot_bindings(o.db, mgr, "p1")
        if case in ("aligned", "changed_generation", "current_owner_and_pin"):
            assert findings == []
        else:
            assert len(findings) == 1
            assert findings[0]["kind"] == (
                "slot_binding_preparing" if case in ("preparing", "preparing_with_owner")
                else "slot_binding_retained"
            )
            expected_detail = {
                "foreign_claim": "foreign or unproved claim",
                "missing_receipt": "acquisition receipt missing or stale",
                "preparing": "preparation in progress",
                "stopped": "foreign or unproved claim",
                "wrong_branch": "checkout branch disagrees",
                "unknown_liveness": "process inventory unverifiable",
                "malformed_claim": "unreadable or malformed",
                "deferred_owner": "integration owner or job pin retained",
                "stale_receipt": "acquisition receipt missing or stale",
                "foreign_owner": "owner live-owner belongs to another task or session",
                "finished_job_pin": "job pinned-job is succeeded but its pin remains",
                "old_generation_pin": "job pinned-job pins another workspace generation",
                "unproved_pin_holder": "no unique current session proves the retained holder",
                "preparing_with_owner": "preparation in progress",
            }
            assert expected_detail[case] in findings[0]["detail"]
            if case in ("deferred_owner", "foreign_owner", "finished_job_pin",
                        "old_generation_pin", "unproved_pin_holder"):
                assert "use its exact recovery fence" in findings[0]["detail"]
        if case in ("foreign_claim", "current_owner_and_pin", "finished_job_pin",
                    "preparing_with_owner"):
            from types import SimpleNamespace
            from src.commands.worktree_commands import WorktreeCommandsMixin
            handler = SimpleNamespace(db=o.db, config=o.config, orchestrator=o,
                                      _active_project_id="p1", execute=AsyncMock(return_value={
                                          "success": True,
                                      }))
            doctor = await WorktreeCommandsMixin._cmd_workspace_doctor(handler, {})
            assert all(f in doctor["findings"] for f in findings)
            o._command_handler = handler
            for _ in range(2):
                o._last_worktree_reaper = 0
                await o._reap_worktree_slots()
                await o._worktree_maintenance_task
            sent = [c for c in handler.execute.await_args_list if c.args[0] == "message_send"]
            # Only retained findings ask the supervisor for recovery, once per
            # changed findings set; healthy or preparing bindings stay quiet.
            if case in ("foreign_claim", "finished_job_pin"):
                assert len(sent) == 1
                assert findings[0]["detail"] in sent[0].args[1]["body"]
            else:
                assert sent == []
        after = (_git(["rev-parse", "HEAD"], path), _git(["status", "--porcelain"], path),
                 (path / ".aq" / "claim.json").read_bytes(),
                 (path / ".aq-worktree.json").read_bytes())
        assert before == after
        fresh = await o.db.get_workspace(ws.id)
        assert fresh.locked_by_task_id == "tsk-1"
        assert fresh.locked_by_agent_id == "tsk-1-a"
        assert fresh.generation == ws.generation + (case == "changed_generation")
        assert await o.db.get_all_task_meta("tsk-1") == meta
        assert (await o.db.get_task("tsk-1")).retry_count == 0
        if case == "deferred_owner":
            from sqlalchemy import select
            async with o.db._engine.connect() as conn:
                owner = (await conn.execute(select(integration_branch_owners).where(
                    integration_branch_owners.c.id == "retained-owner",
                ))).mappings().one()
            assert owner["fence_token"] == 7
            assert owner["handoff_state"] == "handoff_pending"
        if case in ("current_owner_and_pin", "finished_job_pin", "old_generation_pin",
                    "unproved_pin_holder", "foreign_owner", "preparing_with_owner"):
            from sqlalchemy import select
            from src.database.tables import integration_branch_owners, job_workspace_pins
            async with o.db._engine.connect() as conn:
                pins = (await conn.execute(select(job_workspace_pins.c.job_id))).scalars().all()
                owners = (await conn.execute(select(
                    integration_branch_owners.c.id, integration_branch_owners.c.handoff_state,
                ))).all()
            # The audit only reports: no pin, owner or lock is released.
            assert pins == (["pinned-job"] if case in (
                "current_owner_and_pin", "finished_job_pin", "old_generation_pin",
                "unproved_pin_holder") else [])
            assert owners == ([("live-owner", "attached")] if case in (
                "current_owner_and_pin", "foreign_owner", "preparing_with_owner",
                "unproved_pin_holder") else [])
    finally:
        await o.db.close()


@pytest.mark.parametrize("case", [
    "startup", "first_claim", "draining", "drain_requested", "changed_generation",
    "second_session",
    "session_holds_task", "foreign_agent", "stopped", "claim_file", "agent_task",
    "held_sentinel", "on_branch", "unknown_liveness", "failed_preparation", "attached_owner",
])
async def test_taskless_pool_reservation_is_proved_without_a_task_binding(
    tmp_path, base_repo, monkeypatch, case,
):
    """An agent-only pool lock is healthy only when one live taskless pool
    session, a free detached source and no claim or task prove it."""
    from dataclasses import replace

    from sqlalchemy import insert, select

    from src.database.tables import integration_branch_owners, workspaces
    from src.models import SessionRecord

    o = await _orch(tmp_path)
    try:
        await _seed(o, base_repo)
        mgr = o._worktree_slots()
        if case in ("draining", "drain_requested"):
            # The slot a pool session just closed a task in: detached at a
            # published commit with its sentinel returned to the free form.
            ws, path = await _make_slot(o, base_repo)
            agent_id = "tsk-1-a"
            _git(["switch", "--detach", "origin/main"], path)
            assert await mgr.release_task_sentinel(ws, "tsk-1")
            async with o.db.immediate() as conn:
                await conn.execute(update(workspaces).where(workspaces.c.id == ws.id)
                                   .values(locked_by_task_id=None))
            await o.db.update_agent(agent_id, current_task_id=None)
        else:
            agent_id = "pool-a"
            await o.db.create_agent(Agent(id=agent_id, name=agent_id, profile_id="p"))
            project = await o.db.get_project("p1")
            growth = await o._ensure_worktree_slots(project, "project-repo")
            ws = await o.db.acquire_one_unlocked(
                project_id="p1", kind_id="project-repo", mode="exclusive",
                locked_by_task_id=None, locked_by_agent_id=agent_id,
                prefer_workspace_id=growth.created.get("project-repo"),
                kind_mode=KIND_MODE_WORKTREE, worktree_slot_cap=2,
            )
            path = Path(ws.workspace_path)
        ws = await o.db.get_workspace(ws.id)
        assert ws.locked_by_task_id is None and ws.locked_by_agent_id == agent_id
        if case in ("session_holds_task", "agent_task"):
            await o.db.create_task(Task(id="tsk-2", project_id="p1", title="t", description=""))
        draining = case in ("draining", "drain_requested")
        live = "stopped" if case == "stopped" else (
            "draining" if draining else "starting" if case == "startup" else "running")
        await o.db.create_session(SessionRecord(
            id="pool-holder", task_id="tsk-2" if case == "session_holds_task" else None,
            project_id="p1", agent_id="other-a" if case == "foreign_agent" else agent_id,
            profile_id="p", harness="claude", provider="fake", name="pool-holder",
            lifecycle="pool", work_dir=str(path), epoch="e", instance_token="pool-token",
            # Drain requested after its own close: teardown pending, still live.
            state=live, desired_state="stopped" if case in ("stopped", "drain_requested")
            else "running", claims=1 if draining else 0,
            claim_phase="claiming" if case == "first_claim" else None,
            last_claim_epoch=1 if draining else None, started_at=1,
            failed_preparation={"reason": "refused", "task_id": "tsk-failed"}
            if case == "failed_preparation" else None,
        ))
        if case == "second_session":
            await o.db.create_session(SessionRecord(
                id="pool-twin", project_id="p1", agent_id=agent_id, profile_id="p",
                harness="claude", provider="fake", name="pool-twin", lifecycle="pool",
                work_dir=str(path), epoch="e", instance_token="twin-token",
                state="running", desired_state="running", started_at=1,
            ))
        if case == "agent_task":
            await o.db.update_agent(agent_id, current_task_id="tsk-2")
        if case == "claim_file":
            from src.claim_file import write_claim_file
            write_claim_file(str(path), {"task_id": "tsk-old", "claim_epoch": 1,
                                        "session_id": "pool-holder"})
        if case == "held_sentinel":
            mgr.write_sentinel(path, replace(mgr.read_sentinel(path), task_id="tsk-old",
                                             branch="aq/tsk-old"))
        if case == "on_branch":
            _git(["switch", "-c", "stray"], path)
        if case == "attached_owner":
            async with o.db.immediate() as conn:
                await conn.execute(insert(integration_branch_owners).values(
                    id="pool-owner", repository_id="p1", ref="refs/heads/aq/tsk-old",
                    owner_id="tsk-old", owner_role="worker", fence_token=2,
                    handoff_state="attached", session_id="pool-holder",
                    workspace_id=ws.id, created_at=1, updated_at=1,
                ))
        monkeypatch.setattr(mgr, "_slot_is_live", AsyncMock(
            side_effect=PermissionError("process inventory unknown")
            if case == "unknown_liveness" else None, return_value=True,
        ))
        if case == "changed_generation":
            original = o.db.get_workspace
            calls = 0

            async def raced(workspace_id):
                nonlocal calls
                if workspace_id == ws.id:
                    calls += 1
                if workspace_id == ws.id and calls == 2:
                    async with o.db.immediate() as conn:
                        await conn.execute(update(workspaces).where(
                            workspaces.c.id == workspace_id,
                        ).values(generation=workspaces.c.generation + 1))
                return await original(workspace_id)

            monkeypatch.setattr(o.db, "get_workspace", raced)
        claim = path / ".aq" / "claim.json"
        before = (_git(["rev-parse", "HEAD"], path), _git(["status", "--porcelain"], path),
                  claim.read_bytes() if claim.exists() else None,
                  (path / ".aq-worktree.json").read_bytes())

        findings = await reconcile_slot_bindings(o.db, mgr, "p1")
        expected_detail = {
            "second_session": "no unique taskless pool session",
            "session_holds_task": "no unique taskless pool session",
            "foreign_agent": "no unique taskless pool session",
            "stopped": "no unique taskless pool session",
            "claim_file": "claim file retained without a task binding",
            "agent_task": "registered task/agent binding disagrees",
            "held_sentinel": "source sentinel disagrees",
            "on_branch": "checkout branch disagrees",
            "unknown_liveness": "process inventory unverifiable",
            "failed_preparation": "retains a failed preparation for task tsk-failed",
            "attached_owner": "a taskless pool reservation cannot hold the retained owner",
        }.get(case)
        if expected_detail is None:
            assert findings == []
        else:
            assert len(findings) == 1
            assert findings[0]["kind"] == "slot_binding_retained"
            assert findings[0]["locked_by_task_id"] is None
            assert expected_detail in findings[0]["detail"]

        if case in ("startup", "draining", "drain_requested", "claim_file"):
            from types import SimpleNamespace
            handler = SimpleNamespace(execute=AsyncMock(return_value={"success": True}))
            o._command_handler = handler
            for _ in range(2):
                o._last_worktree_reaper = 0
                await o._reap_worktree_slots()
                await o._worktree_maintenance_task
            sent = [c for c in handler.execute.await_args_list if c.args[0] == "message_send"]
            # A proved reservation sends no fenced-recovery notice; a genuine
            # one is reported once per changed findings set.
            if case == "claim_file":
                assert len(sent) == 1
                assert findings[0]["detail"] in sent[0].args[1]["body"]
            else:
                assert sent == []

        after = (_git(["rev-parse", "HEAD"], path), _git(["status", "--porcelain"], path),
                 claim.read_bytes() if claim.exists() else None,
                 (path / ".aq-worktree.json").read_bytes())
        assert before == after
        fresh = await o.db.get_workspace(ws.id)
        assert fresh.locked_by_task_id is None
        assert fresh.locked_by_agent_id == agent_id
        assert fresh.generation == ws.generation + (case == "changed_generation")
        if case == "attached_owner":
            async with o.db._engine.connect() as conn:
                owner = (await conn.execute(select(
                    integration_branch_owners.c.handoff_state,
                    integration_branch_owners.c.fence_token,
                ))).one()
            assert tuple(owner) == ("attached", 2)
    finally:
        await o.db.close()


@pytest.mark.parametrize("case", [
    "idle", "claiming", "claim_race", "active", "preparing", "failed_preparation",
    "claim_file", "agent_task", "task_lock", "dirty", "process", "sentinel",
    "session_locked", "workspace_locked", "git_locked", "owner", "pin",
])
async def test_task_branch_moves_between_pool_slots_only_without_a_live_claim(
    tmp_path, base_repo, monkeypatch, case,
):
    from sqlalchemy import select
    from sqlalchemy.exc import DBAPIError
    from types import SimpleNamespace

    from src.claim_file import write_claim_file
    from src.database.tables import (
        integration_branch_owners, job_workspace_pins, jobs, sessions, workspaces,
    )
    from src.git.manager import GitError
    from src.models import SessionRecord

    o = await _orch(tmp_path)
    try:
        await _seed(o, base_repo)
        mgr = o._worktree_slots()
        holder, path = await _make_slot(o, base_repo)
        branch = _git(["branch", "--show-current"], path)
        # Retried work is committed locally but has not yet been published.
        (path / "progress.txt").write_text("preserve predecessor work\n")
        _git(["add", "progress.txt"], path)
        _git(["commit", "-m", "predecessor progress"], path)
        head = _git(["rev-parse", "HEAD"], path)
        async with o.db.immediate() as conn:
            await conn.execute(update(workspaces).where(workspaces.c.id == holder.id).values(
                locked_by_task_id=None,
            ))
        await o.db.update_agent("tsk-1-a", current_task_id=None)
        holder = await o.db.get_workspace(holder.id)
        base = await o.db.get_workspace("ws-base")
        kind = await o.db.get_workspace_kind(SYSTEM_KIND_SCOPE, "project-repo")
        target = await mgr.create_slot(base, kind, 1)
        await o.db.create_session(SessionRecord(
            id="idle-pool", project_id="p1", agent_id="tsk-1-a", profile_id="p",
            harness="codex", provider="fake", name="idle-pool", lifecycle="pool",
            work_dir=str(path), epoch="e", instance_token="idle-token", started_at=1,
            state="running", desired_state="running",
            task_id="tsk-1" if case in ("active", "preparing") else None,
            claim_phase=case if case in ("claiming", "active", "preparing") else None,
            failed_preparation={"task_id": "tsk-1"} if case == "failed_preparation" else None,
        ))
        if case == "claim_file":
            write_claim_file(str(path), {"task_id": "tsk-1", "claim_epoch": 1,
                                        "session_id": "idle-pool"})
        elif case == "agent_task":
            await o.db.update_agent("tsk-1-a", current_task_id="tsk-1")
        elif case == "task_lock":
            await o.db.update_workspace(holder.id, locked_by_task_id="tsk-1")
        elif case == "dirty":
            (path / "uncommitted.txt").write_text("preserve this too\n")
        elif case == "git_locked":
            _git(["worktree", "lock", str(path)], base_repo)
        elif case == "owner":
            async with o.db.immediate() as conn:
                await conn.execute(integration_branch_owners.insert().values(
                    id="owner", repository_id="p1", ref=f"refs/heads/{branch}",
                    owner_id="tsk-1", owner_role="worker", fence_token=1,
                    handoff_state="attached", session_id="idle-pool", workspace_id=holder.id,
                    created_at=1, updated_at=1,
                ))
        elif case == "pin":
            async with o.db.immediate() as conn:
                await conn.execute(jobs.insert().values(
                    id="retained-job", project_id="p1", task_id="tsk-1", owner_kind="task",
                    owner_id="tsk-1", submitter_session_id="idle-pool", claim_epoch=1,
                    idempotency_key="k", request_hash="h", preset="test", preset_version=1,
                    argv=[], contract={}, workspace_id=holder.id,
                    workspace_generation=holder.generation, input_mode="live", job_class="shared",
                    weight=1, priority_band=2, state="running", submitted_at=1, queue_deadline=2,
                    run_timeout=60, runner_nonce="n",
                ))
                await conn.execute(job_workspace_pins.insert().values(
                    job_id="retained-job", workspace_id=holder.id, generation=holder.generation,
                    created_at=1,
                ))
        elif case == "sentinel":
            sentinel = mgr.read_sentinel(path)
            sentinel.workspace_id = "foreign"
            mgr.write_sentinel(path, sentinel)
        monkeypatch.setattr(mgr, "_slot_is_live", AsyncMock(return_value=case == "process"))
        before = (_git(["status", "--porcelain"], path),
                  (path / ".aq-worktree.json").read_bytes())
        original_run = mgr.git._arun_unlocked
        raced = False

        async def run(args, *positional, **kwargs):
            nonlocal raced
            if case == "claim_race" and args == ["switch", "--detach"]:
                # A sibling selecting work cannot bind a task between the
                # idle proof and Git detachment. Assert exclusion with NOWAIT.
                async with o.db._engine.begin() as other:
                    with pytest.raises(DBAPIError):
                        await other.execute(select(sessions.c.id).where(
                            sessions.c.id == "idle-pool",
                        ).with_for_update(nowait=True))
                raced = True
            return await original_run(args, *positional, **kwargs)

        monkeypatch.setattr(mgr.git, "_arun_unlocked", run)
        task = await o.db.get_task("tsk-1")
        if case in ("session_locked", "workspace_locked"):
            table = sessions if case == "session_locked" else workspaces
            identity = "idle-pool" if case == "session_locked" else holder.id
            async with o.db._engine.begin() as other:
                await other.execute(select(table).where(table.c.id == identity).with_for_update())
                with pytest.raises(DBAPIError):
                    await mgr.reset_slot_for_task(target, task, kind=kind)
        elif case not in ("idle", "claiming", "claim_race"):
            with pytest.raises(GitError):
                await mgr.reset_slot_for_task(target, task, kind=kind)
        else:
            assert await mgr.reset_slot_for_task(target, task, kind=kind) == branch
            assert _git(["branch", "--show-current"], target.workspace_path) == branch
            assert _git(["branch", "--show-current"], path) == ""
            assert _git(["rev-parse", "HEAD"], target.workspace_path) == head
            assert (Path(target.workspace_path) / "progress.txt").read_text() == (
                "preserve predecessor work\n"
            )
            assert raced == (case == "claim_race")
            # The branch may advance before the idle pool selects its next task.
            _git(["commit", "--allow-empty", "-m", "destination progress"], target.workspace_path)
            await mgr.ensure_slot_checkout(
                holder, SimpleNamespace(id="tsk-next", project_id="p1"),
            )
            sentinel = mgr.read_sentinel(path)
            assert sentinel.task_id == "tsk-1"
            assert sentinel.branch is None
        if case not in ("idle", "claiming", "claim_race"):
            assert _git(["branch", "--show-current"], path) == branch
        assert _git(["rev-parse", "HEAD"], path) == head
        assert before[0] == _git(["status", "--porcelain"], path)
        if case not in ("idle", "claiming", "claim_race"):
            assert before[1] == (path / ".aq-worktree.json").read_bytes()
        fresh = await o.db.get_workspace(holder.id)
        assert fresh.locked_by_agent_id == holder.locked_by_agent_id
        assert fresh.generation == holder.generation
    finally:
        await o.db.close()


# ── reap_slot ────────────────────────────────────────────────────────────


class TestReapSlot:
    async def test_reap_removes_retired_slot_dir_and_row_emits_event(
        self, tmp_path, base_repo, monkeypatch,
    ):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, slot_dir = await _make_slot(o, base_repo)

            # No task holding the slot — release it and mark it retired.
            await o.db.release_workspace(slot_ws.id)
            await o.db.update_project("p1", max_concurrent_agents=0)

            # No live processes.
            async def fake_scan(marker_key):
                return []

            monkeypatch.setattr(
                "src.sessions.proctable.scan_by_env_marker", fake_scan
            )

            events: list[tuple[str, dict]] = []
            orig_emit = o.bus.emit

            async def rec(t, p):
                events.append((t, p))
                return await orig_emit(t, p)

            o.bus.emit = rec

            mgr = o._worktree_slots()
            ok = await mgr.reap_slot(slot_ws, reason="retired-test")
            assert ok is True

            assert not slot_dir.exists() or not any(slot_dir.iterdir())
            assert await o.db.get_workspace(slot_ws.id) is None

            types = [t for t, _ in events]
            assert "worktree.reaped" in types
        finally:
            await o.shutdown()

    async def test_reap_blocked_by_live_process(
        self, tmp_path, base_repo, monkeypatch,
    ):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, slot_dir = await _make_slot(o, base_repo)
            await o.db.release_workspace(slot_ws.id)
            await o.db.update_project("p1", max_concurrent_agents=0)

            # Simulate a live process carrying the task's AQ_TASK_ID.
            from src.sessions.proctable import ProcEntry

            async def fake_scan(marker_key):
                return [ProcEntry(pid=99999, ppid=1, comm="agent",
                                  start_ticks=1, marker="tsk-1")]

            monkeypatch.setattr(
                "src.sessions.proctable.scan_by_env_marker", fake_scan
            )

            mgr = o._worktree_slots()
            ok = await mgr.reap_slot(slot_ws, reason="retired-test")
            assert ok is False
            assert slot_dir.is_dir()
            assert await o.db.get_workspace(slot_ws.id) is not None
        finally:
            await o.shutdown()

    async def test_reap_skips_on_proctable_scan_failure(
        self, tmp_path, base_repo, monkeypatch,
    ):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, slot_dir = await _make_slot(o, base_repo)
            await o.db.release_workspace(slot_ws.id)
            await o.db.update_project("p1", max_concurrent_agents=0)

            async def broken_scan(marker_key):
                raise OSError("/proc unavailable")

            monkeypatch.setattr(
                "src.sessions.proctable.scan_by_env_marker", broken_scan
            )

            mgr = o._worktree_slots()
            ok = await mgr.reap_slot(slot_ws, reason="retired-test")
            assert ok is False, "unknown liveness → never reap"
            assert slot_dir.is_dir()
        finally:
            await o.shutdown()

    async def test_unreadable_process_inventory_is_not_an_empty_live_set(
        self, tmp_path, base_repo, monkeypatch,
    ):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, slot_dir = await _make_slot(o, base_repo)
            await o.db.release_workspace(slot_ws.id)
            await o.db.update_project("p1", max_concurrent_agents=0)
            slot_ws = await o.db.get_workspace(slot_ws.id)
            iterdir = Path.iterdir

            def unreadable(path):
                if str(path) == "/proc":
                    raise PermissionError("inventory is unknown")
                return iterdir(path)

            monkeypatch.setattr(Path, "iterdir", unreadable)
            scanner = AsyncMock(return_value=[])
            monkeypatch.setattr("src.sessions.proctable.scan_by_env_marker", scanner)
            assert not await o._worktree_slots().reap_slot(slot_ws, reason="retired-test")
            scanner.assert_not_awaited()
            assert slot_dir.is_dir()
            assert await o.db.get_workspace(slot_ws.id) is not None
        finally:
            await o.shutdown()

    @pytest.mark.parametrize("protection", ["agent", "generation", "session", "claim", "dirty", "delete_refused", "db_delete", "git_remove", "proc_unknown"])
    async def test_retirement_preserves_source_on_changed_or_unproved_identity(
        self, tmp_path, base_repo, monkeypatch, protection,
    ):
        from src.claim_file import write_claim_file
        from src.database.tables import workspaces
        from src.models import SessionRecord

        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, slot_dir = await _make_slot(o, base_repo)
            await o.db.release_workspace(slot_ws.id)
            await o.db.update_project("p1", max_concurrent_agents=0)
            slot_ws = await o.db.get_workspace(slot_ws.id)
            mgr = o._worktree_slots()
            monkeypatch.setattr(mgr, "_slot_is_live", AsyncMock(return_value=False))
            if protection == "agent":
                await o.db.update_workspace(slot_ws.id, locked_by_agent_id="tsk-1-a")
            elif protection == "generation":
                async with o.db.immediate() as conn:
                    await conn.execute(update(workspaces).where(workspaces.c.id == slot_ws.id).values(
                        generation=workspaces.c.generation + 1,
                    ))
            elif protection == "session":
                await o.db.create_session(SessionRecord(
                    id="live", project_id="p1", profile_id="p", harness="codex", provider="fake",
                    name="live", lifecycle="pool", work_dir=str(slot_dir), epoch="e",
                    instance_token="token", state="running", started_at=1,
                ))
            elif protection == "claim":
                write_claim_file(str(slot_dir), {"task_id": "tsk-1", "claim_epoch": 9})
            elif protection == "dirty":
                (slot_dir / "README.md").write_text("staged progress")
                _git(["add", "README.md"], slot_dir)
                (slot_dir / "untracked").write_bytes(b"untracked progress")
            elif protection == "delete_refused":
                monkeypatch.setattr(o.db, "delete_workspace", AsyncMock(return_value=False))
            elif protection == "db_delete":
                monkeypatch.setattr(o.db, "delete_workspace", AsyncMock(side_effect=RuntimeError("DB rejected deletion")))
            elif protection == "git_remove":
                monkeypatch.setattr(mgr.git, "aremove_worktree_exact", AsyncMock(side_effect=RuntimeError("registration locked")))
            else:
                monkeypatch.setattr(mgr, "_slot_is_live", AsyncMock(side_effect=OSError("process ownership unknown")))
            head = _git(["rev-parse", "HEAD"], slot_dir)
            index = _git(["write-tree"], slot_dir)
            status = _git(["status", "--porcelain"], slot_dir)
            registration = _git(["worktree", "list", "--porcelain"], base_repo)
            events = []
            emit = o.bus.emit

            async def record(event, payload):
                events.append(event)
                return await emit(event, payload)

            monkeypatch.setattr(o.bus, "emit", record)
            assert not await mgr.reap_slot(slot_ws, reason="retired-test")
            assert slot_dir.is_dir()
            assert await o.db.get_workspace(slot_ws.id) is not None
            assert _git(["rev-parse", "HEAD"], slot_dir) == head
            assert _git(["write-tree"], slot_dir) == index
            assert _git(["status", "--porcelain"], slot_dir) == status
            assert _git(["worktree", "list", "--porcelain"], base_repo) == registration
            assert "worktree.reaped" not in events
            if protection == "dirty":
                assert (slot_dir / "untracked").read_bytes() == b"untracked progress"
        finally:
            await o.shutdown()


# ── prune_branches ──────────────────────────────────────────────────────


class TestPruneBranchesRetainFailedDays:
    """Terminal status and commit age do not prove unique work is disposable."""

    async def test_failed_old_recent_and_active_unique_branches_are_kept(
        self, tmp_path, base_repo,
    ):
        from src.models import TaskStatus

        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            # Shrink retention window to 1 day for the test.
            o._worktree_slots().config.retain_failed_days = 1

            # Failed + old: task exists and is FAILED; commit is dated
            # well before the retention cutoff.
            _git(["switch", "-c", "aq/failed-old"], cwd=base_repo)
            (base_repo / "fo.txt").write_text("fo\n")
            _git(["add", "-A"], cwd=base_repo)
            past = "2000-01-01T00:00:00"
            subprocess.run(
                ["git",
                 "-c", "user.name=T", "-c", "user.email=t@t.com",
                 "commit", "-m", "fo",
                 "--date", past],
                cwd=str(base_repo),
                check=True, capture_output=True,
                env={**__import__("os").environ,
                     "GIT_COMMITTER_DATE": past,
                     "GIT_AUTHOR_DATE": past},
            )
            _git(["switch", "main"], cwd=base_repo)
            await o.db.create_task(
                Task(id="failed-old", project_id="p1",
                     title="fo", description="",
                     status=TaskStatus.FAILED)
            )

            # Failed + recent: FAILED task, but committed just now — keep.
            _git(["switch", "-c", "aq/failed-recent"], cwd=base_repo)
            (base_repo / "fr.txt").write_text("fr\n")
            _git(["add", "-A"], cwd=base_repo)
            _git(["commit", "-m", "fr"], cwd=base_repo)
            _git(["switch", "main"], cwd=base_repo)
            await o.db.create_task(
                Task(id="failed-recent", project_id="p1",
                     title="fr", description="",
                     status=TaskStatus.FAILED)
            )

            # Unmerged + active: no FAILED status — keep unconditionally.
            _git(["switch", "-c", "aq/active"], cwd=base_repo)
            (base_repo / "ac.txt").write_text("ac\n")
            _git(["add", "-A"], cwd=base_repo)
            subprocess.run(
                ["git",
                 "-c", "user.name=T", "-c", "user.email=t@t.com",
                 "commit", "-m", "ac", "--date", past],
                cwd=str(base_repo),
                check=True, capture_output=True,
                env={**__import__("os").environ,
                     "GIT_COMMITTER_DATE": past,
                     "GIT_AUTHOR_DATE": past},
            )
            _git(["switch", "main"], cwd=base_repo)
            # 'active' task is IN_PROGRESS (default) — keep the branch.
            await o.db.create_task(
                Task(id="active", project_id="p1",
                     title="ac", description="")
            )

            base_ws = await o.db.get_workspace("ws-base")
            mgr = o._worktree_slots()
            pruned = await mgr.prune_branches(base_ws, default_branch="main")

            branches = _git(["branch", "--list"], cwd=base_repo)
            assert "aq/failed-old" in branches
            assert "aq/failed-recent" in branches
            assert "aq/active" in branches
            assert "aq/failed-old" not in pruned
            assert pruned == []
        finally:
            await o.shutdown()


class TestPruneBranches:
    async def test_remote_auth_failure_keeps_local_branch(self, tmp_path, base_repo):
        from unittest.mock import AsyncMock, patch

        from src.git.manager import RemoteRefResult, RemoteRefState

        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            _git(["branch", "aq/keep", "main"], cwd=base_repo)
            mgr = o._worktree_slots()
            mgr.config.prune_remote_branches = True
            unknown = RemoteRefResult(RemoteRefState.ERROR, error="App credential unavailable")
            with patch.object(mgr.git, "als_remote_ref", AsyncMock(return_value=unknown)):
                pruned = await mgr.prune_branches(
                    await o.db.get_workspace("ws-base"), default_branch="main"
                )
            assert pruned == []
            assert "aq/keep" in _git(["branch", "--list"], cwd=base_repo)
        finally:
            await o.shutdown()

    async def test_moved_remote_branch_keeps_local_branch(self, tmp_path, base_repo):
        from unittest.mock import AsyncMock, patch

        from src.git.manager import RemoteRefResult, RemoteRefState

        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            _git(["branch", "aq/keep", "main"], cwd=base_repo)
            mgr = o._worktree_slots()
            mgr.config.prune_remote_branches = True
            moved = RemoteRefResult(RemoteRefState.PRESENT, oid="b" * 40)
            with patch.object(mgr.git, "als_remote_ref", AsyncMock(return_value=moved)):
                with patch.object(mgr.git, "adelete_remote_ref_exact", AsyncMock()) as delete:
                    pruned = await mgr.prune_branches(
                        await o.db.get_workspace("ws-base"), default_branch="main"
                    )
            assert pruned == []
            delete.assert_not_awaited()
            assert "aq/keep" in _git(["branch", "--list"], cwd=base_repo)
        finally:
            await o.shutdown()

    async def test_prunes_merged_aq_branches_keeps_unmerged(
        self, tmp_path, base_repo,
    ):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)

            # Create a merged aq/ branch and an unmerged one.
            _git(["switch", "-c", "aq/merged"], cwd=base_repo)
            (base_repo / "m.txt").write_text("m\n")
            _git(["add", "-A"], cwd=base_repo)
            _git(["commit", "-m", "m"], cwd=base_repo)
            _git(["switch", "main"], cwd=base_repo)
            _git(["merge", "--no-ff", "aq/merged"], cwd=base_repo)

            _git(["switch", "-c", "aq/unmerged"], cwd=base_repo)
            (base_repo / "u.txt").write_text("u\n")
            _git(["add", "-A"], cwd=base_repo)
            _git(["commit", "-m", "u"], cwd=base_repo)
            _git(["switch", "main"], cwd=base_repo)

            base_ws = await o.db.get_workspace("ws-base")
            mgr = o._worktree_slots()
            pruned = await mgr.prune_branches(base_ws, default_branch="main")

            branches = _git(["branch", "--list"], cwd=base_repo)
            assert "aq/merged" not in branches
            assert "aq/unmerged" in branches
            assert "aq/merged" in pruned
        finally:
            await o.shutdown()


# ── adopt_existing ──────────────────────────────────────────────────────


class TestAdoptExisting:
    async def test_skips_explicit_non_git_base(self, tmp_path):
        o = await _orch(tmp_path)
        try:
            await o.db.create_project(Project(id="p1", name="alpha"))
            await o.db.upsert_workspace_kind(
                WorkspaceKind(
                    project_id="p1",
                    id="notes",
                    is_git_repo=False,
                    lockable=True,
                    writable=True,
                    mode="exclusive-clone",
                    default_lock_mode="exclusive",
                )
            )
            notes = tmp_path / "notes"
            notes.mkdir()
            await o.db.create_workspace(
                Workspace(
                    id="ws-notes",
                    project_id="p1",
                    workspace_path=str(notes),
                    source_type=RepoSourceType.LINK,
                    kind_id="notes",
                )
            )
            project = await o.db.get_project("p1")

            report = await o._worktree_slots().adopt_existing(project)

            assert report.exclude_failures == []
            assert report.repaired == []
        finally:
            await o.shutdown()

    async def test_rejects_git_subdirectory_base(self, tmp_path, base_repo):
        nested = base_repo / "nested"
        nested.mkdir()
        o = await _orch(tmp_path)
        try:
            await _seed(o, nested)
            project = await o.db.get_project("p1")

            report = await o._worktree_slots().adopt_existing(project)

            assert report.repaired == []
            assert report.adopted == []
            assert report.exclude_failures == [
                {
                    "workspace_id": "ws-base",
                    "path": str(nested),
                    "error": "managed exclude could not be installed",
                }
            ]
        finally:
            await o.shutdown()

    async def test_repairs_missing_exclude_block(self, tmp_path, base_repo):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            # Nuke the exclude file to simulate a repo that never had our block.
            exclude = base_repo / ".git" / "info" / "exclude"
            if exclude.exists():
                exclude.unlink()

            project = (await o.db.list_projects())[0]
            mgr = o._worktree_slots()
            report = await mgr.adopt_existing(project)

            # The base workspace should be in the repaired list.
            assert "ws-base" in report.repaired
            # And the block should now be in the exclude file.
            assert exclude.exists()
            content = exclude.read_text()
            assert EXCLUDE_BEGIN.split(" ")[0] in content or "agent-queue" in content
        finally:
            await o.shutdown()

    async def test_repairs_exact_exclude_for_separate_git_dir(self, tmp_path):
        origin = tmp_path / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(origin)],
            check=True,
            capture_output=True,
        )
        base = tmp_path / "base"
        metadata = tmp_path / "metadata"
        subprocess.run(
            ["git", "clone", f"--separate-git-dir={metadata}", str(origin), str(base)],
            check=True,
            capture_output=True,
        )
        (base / "README.md").write_text("init\n")
        _git(["add", "README.md"], cwd=base)
        _git(["commit", "-m", "init"], cwd=base)
        _git(["push", "origin", "main"], cwd=base)
        o = await _orch(tmp_path)
        try:
            await _seed(o, base)
            project = (await o.db.list_projects())[0]

            report = await o._worktree_slots().adopt_existing(project)

            assert "ws-base" in report.repaired
            assert EXCLUDE_BEGIN in (metadata / "info" / "exclude").read_text()
            assert not (base / ".git" / "info" / "exclude").exists()
        finally:
            await o.shutdown()

    async def test_reports_exclude_verification_failure_separately_from_unchanged(
        self, tmp_path, base_repo
    ):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            info_dir = base_repo / ".git" / "info"
            (info_dir / "exclude").unlink()
            info_dir.rmdir()
            info_dir.write_text("not a directory\n")
            project = (await o.db.list_projects())[0]

            report = await o._worktree_slots().adopt_existing(project)

            assert report.repaired == []
            assert report.exclude_failures == [
                {
                    "workspace_id": "ws-base",
                    "path": str(base_repo),
                    "error": "managed exclude could not be installed",
                }
            ]
        finally:
            await o.shutdown()

    async def test_adopts_registered_slot_with_matching_sentinel(
        self, tmp_path, base_repo,
    ):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, slot_dir = await _make_slot(o, base_repo)
            await o.db.release_workspace(slot_ws.id)

            project = (await o.db.list_projects())[0]
            mgr = o._worktree_slots()
            report = await mgr.adopt_existing(project)

            # An existing, well-formed slot should show up as adopted.
            assert slot_ws.id in report.adopted
        finally:
            await o.shutdown()

    async def test_prunes_stale_worktree_registration(
        self, tmp_path, base_repo,
    ):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, slot_dir = await _make_slot(o, base_repo)

            # Delete the directory without telling git — creates a stale
            # entry in .git/worktrees.
            import shutil
            shutil.rmtree(slot_dir)

            project = (await o.db.list_projects())[0]
            mgr = o._worktree_slots()
            report = await mgr.adopt_existing(project)

            # Stale registration is pruned by `git worktree prune`.
            lines = await o.git.aworktree_list(str(base_repo))
            paths = [line["path"] for line in lines]
            assert str(slot_dir) not in paths
            # report.pruned must name the path that was cleaned up.
            assert isinstance(report.pruned, list)
            assert any(str(slot_dir) in p for p in report.pruned), (
                f"Expected {slot_dir} in report.pruned, got: {report.pruned}"
            )
        finally:
            await o.shutdown()


# ── recovery no longer deletes in-progress slots ────────────────────────


class TestRecoveryPreservesInProgressSlots:
    async def test_in_progress_slot_survives_recover_stale_state(
        self, tmp_path, base_repo,
    ):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, slot_dir = await _make_slot(o, base_repo)
            # Mark task IN_PROGRESS to model a mid-run restart.
            from src.models import TaskStatus

            await o.db.update_task("tsk-1", status=TaskStatus.IN_PROGRESS.value)

            # Recovery should NOT delete the slot directory or its branch.
            await o._recover_stale_state()

            assert slot_dir.is_dir()
            assert "aq/tsk-1" in _git(["branch", "--list"], cwd=base_repo)
            # Row survives.
            assert await o.db.get_workspace(slot_ws.id) is not None
        finally:
            await o.shutdown()


class TestMonitorSafety:
    async def test_missing_repo_is_not_reported_as_missing_git(self, tmp_path):
        from src.git.manager import GitError
        o = await _orch(tmp_path)
        try:
            with pytest.raises(GitError, match="working directory.*does not exist"):
                await o.git._arun(["status", "--short"], cwd=str(tmp_path / "missing"))
        finally:
            await o.shutdown()

    async def test_prune_never_attempts_checked_out_branch(self, tmp_path, base_repo):
        from unittest.mock import AsyncMock, patch
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            attached = tmp_path / "attached"
            _git(["worktree", "add", "-b", "aq/attached", str(attached), "main"], cwd=base_repo)
            (attached / "uncommitted.txt").write_text("preserve this work")
            _git(["branch", "aq/free", "main"], cwd=base_repo)
            mgr = o._worktree_slots()
            with patch.object(mgr.git, "adelete_local_branch", AsyncMock(wraps=mgr.git.adelete_local_branch)) as delete:
                pruned = await mgr.prune_branches(await o.db.get_workspace("ws-base"), default_branch="main")
                assert all(call.args[1] != "aq/attached" for call in delete.await_args_list)
            assert pruned == ["aq/free"]
            assert (attached / "uncommitted.txt").read_text() == "preserve this work"
        finally:
            await o.shutdown()

    async def test_prune_skips_when_worktree_inventory_is_unknown(self, tmp_path, base_repo):
        from unittest.mock import AsyncMock, patch
        from src.git.manager import GitError
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            _git(["branch", "aq/keep", "main"], cwd=base_repo)
            mgr = o._worktree_slots()
            with patch.object(mgr.git, "aworktree_list", AsyncMock(side_effect=GitError("inventory unavailable"))):
                assert await mgr.prune_branches(await o.db.get_workspace("ws-base"), default_branch="main") == []
            assert "aq/keep" in _git(["branch", "--list"], cwd=base_repo)
        finally:
            await o.shutdown()

    async def test_prune_skips_missing_repository(self, tmp_path, base_repo):
        from unittest.mock import AsyncMock, patch
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            base = await o.db.get_workspace("ws-base")
            base.workspace_path = str(tmp_path / "missing")
            mgr = o._worktree_slots()
            with patch.object(mgr.git, "alist_merged_branches", AsyncMock(return_value=[])) as merged:
                assert await mgr.prune_branches(base, default_branch="main") == []
                merged.assert_not_awaited()
        finally:
            await o.shutdown()


# ── restore_disabled_slots — disabled slots must not cap the pool ───────


class _Handler:
    """Records supervisor alerts sent through ``message_send``."""

    def __init__(self):
        self.sent: list[dict] = []

    async def execute(self, command, args):
        assert command == "message_send"
        self.sent.append(args)
        return {"success": True}


async def _disabled_slot(o, base_repo, monkeypatch, *, fields):
    """One released slot-0, disabled with *fields*, and no live processes."""
    slot_ws, slot_dir = await _make_slot(o, base_repo)
    await o.db.release_workspace(slot_ws.id)
    await o.db.update_workspace(slot_ws.id, **fields)

    async def no_processes(marker_key):
        return []

    monkeypatch.setattr("src.sessions.proctable.scan_by_env_marker", no_processes)
    return await o.db.get_workspace(slot_ws.id), slot_dir


def _record_events(o) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    orig_emit = o.bus.emit

    async def rec(t, p):
        events.append((t, p))
        return await orig_emit(t, p)

    o.bus.emit = rec
    return events


async def _restore(o, handler=None, *, now=None):
    from src.orchestrator.slot_restore import restore_disabled_slots

    project = await o.db.get_project("p1")
    return await restore_disabled_slots(
        o.db, o._worktree_slots(), project, command_handler=handler, now=now,
    )


class TestRestoreDisabledSlots:
    async def test_missing_worktree_is_recreated_in_place_and_enabled(
        self, tmp_path, base_repo, monkeypatch,
    ):
        import shutil

        from src.workspace_disable import WORKTREE_REMOVED, disable_fields

        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, slot_dir = await _disabled_slot(
                o, base_repo, monkeypatch,
                fields=disable_fields(WORKTREE_REMOVED, "deleting task t removed it"),
            )
            shutil.rmtree(slot_dir)
            events = _record_events(o)

            [action] = await _restore(o)

            assert action.action == "recreated"
            assert (slot_dir / ".git").exists()
            row = await o.db.get_workspace(slot_ws.id)
            assert row.enabled is True
            assert row.disabled_reason is None and row.disabled_at is None
            sentinel = o._worktree_slots().read_sentinel(slot_dir)
            assert sentinel.workspace_id == slot_ws.id
            [(_, payload)] = [e for e in events if e[0] == "worktree.restored"]
            assert payload["action"] == "recreated"
            assert payload["previous_reason"].startswith("worktree_removed")
        finally:
            await o.shutdown()

    async def test_clean_slot_disabled_without_a_reason_is_reenabled(
        self, tmp_path, base_repo, monkeypatch,
    ):
        """The incident's slots 0 and 2: clean, disabled, no reason recorded."""
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, _ = await _disabled_slot(
                o, base_repo, monkeypatch, fields={"enabled": False},
            )
            assert await o.db.count_available_workspaces("p1", worktree_slot_cap=2) == 1

            [action] = await _restore(o)

            assert action.action == "reenabled"
            assert (await o.db.get_workspace(slot_ws.id)).enabled is True
            assert await o.db.count_available_workspaces("p1", worktree_slot_cap=2) == 2
        finally:
            await o.shutdown()

    async def test_dirty_slot_is_quarantined_and_the_supervisor_told_once(
        self, tmp_path, base_repo, monkeypatch,
    ):
        from src.workspace_disable import QUARANTINED_DIRTY, reason_code

        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, slot_dir = await _disabled_slot(
                o, base_repo, monkeypatch, fields={"enabled": False},
            )
            (slot_dir / "unsaved.txt").write_text("work in progress\n")
            events = _record_events(o)
            handler = _Handler()

            [action] = await _restore(o, handler)

            assert action.action == "quarantined"
            row = await o.db.get_workspace(slot_ws.id)
            assert row.enabled is False
            assert reason_code(row.disabled_reason) == QUARANTINED_DIRTY
            assert (slot_dir / "unsaved.txt").exists(), "the work is never discarded"
            [alert] = handler.sent
            assert alert["to_id"] == "supervisor-p1"
            assert "unsaved.txt" in alert["body"]
            [(_, payload)] = [e for e in events if e[0] == "worktree.quarantined"]
            assert payload["dirty_paths"] == ["unsaved.txt"]

            # Still dirty: held, and not re-announced every pass.
            [again] = await _restore(o, handler)
            assert again.action == "skipped"
            assert len(handler.sent) == 1

            # Salvaged: back in service.
            (slot_dir / "unsaved.txt").unlink()
            [restored] = await _restore(o, handler)
            assert restored.action == "reenabled"
            assert (await o.db.get_workspace(slot_ws.id)).enabled is True
        finally:
            await o.shutdown()

    async def test_operator_disabled_slot_is_left_alone(
        self, tmp_path, base_repo, monkeypatch,
    ):
        from src.workspace_disable import OPERATOR, disable_fields

        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, _ = await _disabled_slot(
                o, base_repo, monkeypatch, fields=disable_fields(OPERATOR),
            )
            [action] = await _restore(o)
            assert action.action == "skipped"
            assert (await o.db.get_workspace(slot_ws.id)).enabled is False
        finally:
            await o.shutdown()

    async def test_claim_failure_quarantine_holds_for_the_cooldown(
        self, tmp_path, base_repo, monkeypatch,
    ):
        from src.workspace_disable import CLAIM_FAILURE_COOLDOWN, CLAIM_FAILURES, disable_fields

        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, _ = await _disabled_slot(
                o, base_repo, monkeypatch,
                fields=disable_fields(CLAIM_FAILURES, "3 refusals", now=1000.0),
            )
            [early] = await _restore(o, now=1000.0 + CLAIM_FAILURE_COOLDOWN - 1)
            assert early.action == "skipped"
            assert (await o.db.get_workspace(slot_ws.id)).enabled is False

            [late] = await _restore(o, now=1000.0 + CLAIM_FAILURE_COOLDOWN + 1)
            assert late.action == "reenabled"
        finally:
            await o.shutdown()

    async def test_live_process_blocks_restore(self, tmp_path, base_repo, monkeypatch):
        from src.sessions.proctable import ProcEntry

        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, _ = await _disabled_slot(
                o, base_repo, monkeypatch, fields={"enabled": False},
            )

            async def live(marker_key):
                return [ProcEntry(pid=99999, ppid=1, comm="agent",
                                  start_ticks=1, marker="tsk-1")]

            monkeypatch.setattr("src.sessions.proctable.scan_by_env_marker", live)
            [action] = await _restore(o)
            assert action.action == "skipped"
            assert (await o.db.get_workspace(slot_ws.id)).enabled is False
        finally:
            await o.shutdown()

    async def test_reaper_pass_runs_the_reconciler(self, tmp_path, base_repo, monkeypatch):
        o = await _orch(tmp_path)
        try:
            await _seed(o, base_repo)
            slot_ws, _ = await _disabled_slot(
                o, base_repo, monkeypatch, fields={"enabled": False},
            )
            o._last_worktree_reaper = 0.0
            await o._reap_worktree_slots()
            await o._worktree_maintenance_task
            assert (await o.db.get_workspace(slot_ws.id)).enabled is True
        finally:
            await o.shutdown()


async def test_slow_maintenance_does_not_hold_cycle_or_overlap(tmp_path, monkeypatch):
    import asyncio

    o = await _orch(tmp_path)
    release = asyncio.Event()
    entered = asyncio.Event()

    async def maintenance():
        entered.set()
        await release.wait()

    monkeypatch.setattr(o, "_reap_worktree_slots_pass", maintenance)
    try:
        o._last_worktree_reaper = 0
        await asyncio.wait_for(o._reap_worktree_slots(), 5)
        await asyncio.wait_for(entered.wait(), 5)
        first = o._worktree_maintenance_task
        o._last_worktree_reaper = 0
        await asyncio.wait_for(o._reap_worktree_slots(), 5)
        assert o._worktree_maintenance_task is first
        assert not first.done()
    finally:
        release.set()
        await o.shutdown()
