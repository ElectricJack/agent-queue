"""Unified deletion previews and resolves state holds instead of refusing them."""

import asyncio
import json

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import DBAPIError

from src.database import tables as t
from src.models import TaskStatus
from tests.test_task_controls import command, running_session
from tests.test_task_controls import env as env  # noqa: PLC0414
from tests.test_task_removal import child, delivered_batch, operation, state

pytestmark = pytest.mark.asyncio


async def delete_task(env, **args):
    return await command(env, "delete_task", confirmed=True, **args)


async def test_preview_is_read_only_and_lists_single_scope_children(env):
    await child(env)
    result = await command(env, "delete_task")
    assert result["disposition"] == "preview"
    assert result["task_ids"] == ["t"]
    assert result["reparented_tasks"] == ["child"]
    assert "has_children" in {row["code"] for row in result["warnings"]}
    assert (await env.db.get_task("t")).status is TaskStatus.READY
    assert await env.db.get_recent_events(event_type="task.delete_requested") == []


async def test_delete_only_container_reparents_children_and_drops_edges(env):
    await child(env)
    await env.db.add_dependency("peer", "t")
    result = await delete_task(env)
    assert result["success"], result
    assert result["task_ids"] == ["t"]
    assert await env.db.get_task("t") is None
    assert (await env.db.get_task("child")).parent_task_id is None
    assert await env.db.get_dependencies("peer") == set()
    assert "dependents" in {row["code"] for row in result["warnings"]}


async def test_deleting_last_child_settles_surviving_parent(env):
    await env.db.add_dependency("t", "peer", "parent-child")
    await env.db.update_task("peer", status=TaskStatus.IN_PROGRESS)
    result = await delete_task(env)
    assert result["success"], result
    assert await env.db.get_task("t") is None
    assert (await env.db.get_task("peer")).status is TaskStatus.COMPLETED


async def test_failed_parent_settlement_does_not_roll_back_delete(env, monkeypatch):
    await env.db.add_dependency("t", "peer", "parent-child")

    async def fail(*args, **kwargs):
        raise RuntimeError("settlement unavailable")

    monkeypatch.setattr(env.db, "settle_containers", fail)
    result = await delete_task(env)
    assert result["success"], result
    assert await env.db.get_task("t") is None
    assert await env.db.get_task("peer") is not None
    assert any("settlement unavailable" in row["message"] for row in result["warnings"])
    audit = json.loads((await env.db.get_recent_events(event_type="task.deleted"))[0]["payload"])
    assert any(row["outcome"] == "failed" for row in audit["cleanup_results"])


async def test_delete_fences_concurrent_foreign_key_writers_during_layout_cleanup(env, monkeypatch):
    original = env.db.delete_layout_rows_for_tasks
    observed = []

    async def cleanup(task_ids, *, conn):
        await original(task_ids, conn=conn)
        # Layout inserts take KEY SHARE for their FK check. A NOWAIT probe
        # verifies the lock without depending on a timing budget or sleeps.
        with pytest.raises(DBAPIError, match="could not obtain lock"):
            async with env.db.immediate() as writer:
                await writer.execute(
                    select(t.tasks.c.id)
                    .where(t.tasks.c.id.in_(task_ids))
                    .with_for_update(read=True, key_share=True, nowait=True)
                )
        observed.extend(task_ids)

    monkeypatch.setattr(env.db, "delete_layout_rows_for_tasks", cleanup)
    result = await delete_task(env)
    assert result["success"], result
    assert observed == ["t"]
    assert await env.db.get_task("t") is None


@pytest.mark.parametrize("name", ["delete_task", "remove_task"])
async def test_recursive_delete_and_alias_retain_complete_audit_without_reason(env, name):
    await child(env)
    result = await command(env, name, confirmed=True, recursive=True)
    assert result["success"], result
    assert set(result["task_ids"]) == {"t", "child"}
    assert await env.db.get_task("child") is None
    assert await env.db.get_task("peer") is not None
    events = await env.db.get_recent_events(event_type="task.delete_requested")
    audit = json.loads(events[0]["payload"])
    assert audit["actor"] == "human:local-operator"
    assert audit["reason"] == ""
    assert {row["id"] for row in audit["task_snapshots"]} == {"t", "child"}
    assert audit["task_snapshots"][0]["description"] == "Original requirements"
    assert result["audit_id"] == events[0]["id"]
    final = await env.db.get_recent_events(event_type="task.deleted")
    assert json.loads(final[0]["payload"])["task_ids"] == result["task_ids"]


@pytest.mark.parametrize("failed_stop", [False, True])
async def test_running_claim_stops_or_reports_cleanup_without_refusing(env, tmp_path, failed_stop):
    provider, _ = await running_session(env, tmp_path, lifecycle="pool")
    provider.fail_stop = failed_stop
    await child(env, TaskStatus.DEFINED)
    preview = await command(env, "delete_task", recursive=True)
    result = await delete_task(env, recursive=True, stop_running=True)
    assert result["success"], result
    assert result["task_ids"] == preview["task_ids"]
    assert await env.db.get_task("t") is None
    assert await env.db.get_task("child") is None
    assert (await env.db.get_agent("agent")).current_task_id is None
    assert (await env.db.get_workspace("workspace")).locked_by_task_id is None
    assert (await env.db.get_session("s")).task_id is None
    assert "peer-session" in provider.running
    if failed_stop:
        assert any(row["outcome"] == "failed" for row in result["cleanup_results"])
    else:
        assert "task-session" not in provider.running


async def test_delete_serializes_with_pool_session_termination(env, tmp_path, monkeypatch):
    """A reconciler holding the session must finish before deletion takes its agent."""
    provider, _ = await running_session(env, tmp_path, lifecycle="pool")
    session_locked, final_started = asyncio.Event(), asyncio.Event()
    original_lock = env.db.lock_hierarchy_project
    original_stop = provider.stop
    phases = 0
    deletion_pid = None

    async def observe_phase(conn, project_id):
        nonlocal phases, deletion_pid
        await original_lock(conn, project_id)
        phases += 1
        if phases == 2:
            deletion_pid = await conn.scalar(select(func.pg_backend_pid()))
            final_started.set()

    async def terminate():
        async with env.db.immediate() as conn:
            pid = await conn.scalar(select(func.pg_backend_pid()))
            await conn.execute(
                select(t.sessions.c.id).where(t.sessions.c.id == "s").with_for_update()
            )
            session_locked.set()
            await final_started.wait()
            # Wait for an actual lock conflict, rather than depending on a sleep
            # to place the two transactions in the failing CI interleaving.
            async with asyncio.timeout(10):
                while pid not in await conn.scalar(select(func.pg_blocking_pids(deletion_pid))):
                    await asyncio.sleep(0.01)
            released = await env.db.terminate_pool_session(
                "s", reason="operator_stop", task_status=TaskStatus.PAUSED, conn=conn
            )
            assert released.released
            await conn.execute(
                update(t.sessions).where(t.sessions.c.id == "s").values(state="stopped")
            )

    monkeypatch.setattr(env.db, "lock_hierarchy_project", observe_phase)
    async with asyncio.timeout(20), asyncio.TaskGroup() as group:
        async def stop(handle, *, grace):
            await original_stop(handle, grace=grace)
            group.create_task(terminate())
            await session_locked.wait()

        monkeypatch.setattr(provider, "stop", stop)
        result = await delete_task(env)
    assert result["success"], result
    assert await env.db.get_task("t") is None
    session = await env.db.get_session("s")
    assert session.state == "stopped"
    assert session.task_id is None
    assert (await env.db.get_agent("agent")).current_task_id is None
    assert (await env.db.get_workspace("workspace")).locked_by_task_id is None
    assert await env.db.get_task("peer") is not None


async def test_pause_cleanup_pending_becomes_warning_and_does_not_block_delete(env):
    await env.db.set_task_meta("t", "manual_pause", {"cleanup_pending": True})
    result = await delete_task(env)
    assert result["success"], result
    assert await env.db.get_task("t") is None


async def test_sealed_and_delivered_state_are_warned_and_history_survives(env):
    await delivered_batch(env)
    preview = await command(env, "delete_task")
    assert {"sealed", "delivery_target_fixed"} <= {r["code"] for r in preview["warnings"]}
    result = await delete_task(env, reason="No longer needed")
    assert result["success"], result
    assert await state(env, t.integration_batches, "lifecycle") == "aborted"
    assert await state(env, t.task_delivery_receipts, "source_task_id") == "t"
    assert await state(env, t.integration_batch_members, "task_id") == "t"
    assert await env.db.get_task("t") is None


async def test_live_integration_subject_and_owner_are_cancelled(env):
    await operation(env, subject=True)
    async with env.db.immediate() as conn:
        await conn.execute(
            insert(t.integration_branch_owners).values(
                id="owner",
                repository_id="repo",
                ref="aq/t",
                owner_id="t",
                owner_role="worker",
                fence_token=1,
                handoff_state="attached",
                created_at=1,
                updated_at=1,
            )
        )
    preview = await command(env, "delete_task")
    assert {"integration_owned", "integration_cleanup_blocked"} <= {
        r["code"] for r in preview["warnings"]
    }
    result = await delete_task(env)
    assert result["success"], result
    assert await state(env, t.integration_repair_operations, "state") == "cancelled"
    assert await state(env, t.integration_subjects, "phase") == "done"
    assert await state(env, t.integration_branch_owners, "handoff_state") == "released"
    assert await state(env, t.integration_parent_episodes, "parent_task_id") == "t"


async def test_gates_reviews_and_escalations_are_withdrawn_and_retained(env):
    gate, _ = await env.db.create_gate(
        project_id="p", gate_type="review", title="Decision", waiter_task_ids=["t"]
    )
    async with env.db.immediate() as conn:
        await conn.execute(
            insert(t.doc_reviews).values(
                id="review",
                project_id="p",
                author_task_id="t",
                kind="other",
                title="Review",
                vault_path="review.md",
                state="in_review",
                gate_id=gate,
                created_at=1,
                updated_at=1,
            )
        )
        await conn.execute(
            insert(t.escalations).values(
                id="escalation",
                project_id="p",
                task_id="t",
                source_kind="task",
                source_identity="t",
                incident_key="t",
                supervisor_owner="supervisor-p",
                summary="Decision",
                investigation="Investigated",
                decision_requested="Choose",
                severity="low",
                state="needs_human",
                created_at=1,
                updated_at=1,
            )
        )
    result = await delete_task(env)
    assert result["success"], result
    assert (await env.db.get_gate(gate))["status"] == "cancelled"
    assert await state(env, t.doc_reviews, "state") == "withdrawn"
    assert await state(env, t.escalations, "state") == "cancelled"


async def test_materialized_origin_without_branch_choice_is_preview_warning_not_refusal(env):
    await operation(env)
    async with env.db.immediate() as conn:
        await conn.execute(
            insert(t.task_branch_origins).values(
                id="origin",
                task_id="t",
                repository_id="repo",
                branch_name="aq/t",
                base_sha="a" * 40,
                creation_generation=0,
                materialized=True,
                reserved=True,
                created_at=1,
            )
        )
    preview = await command(env, "delete_task")
    assert preview["branches"] == "keep"
    assert {b["branch"] for b in preview["branch_details"]} >= {"aq/t"}
    result = await delete_task(env)
    assert result["success"], result
    assert await state(env, t.task_branch_origins, "retired_at") is not None


async def git_subtree(env, tmp_path, *, detached=False):
    from sqlalchemy import update

    from src.git.manager import GitManager
    from src.models import RepoConfig, RepoSourceType, Workspace

    git = GitManager()
    env.orch.git = git
    base, remote, slot, other = [
        tmp_path / name for name in ("checkout", "remote.git", "slot", "other-checkout")
    ]
    await git._arun(["init", "--bare", "--template=", str(remote)], cwd=str(tmp_path))
    await git._arun(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=str(remote))
    await git._arun(["init", "-b", "main", "--template=", str(base)], cwd=str(tmp_path))
    (base / "file").write_text("work\n")
    await git._arun(["add", "."], cwd=str(base))
    await git._arun(["commit", "-m", "work"], cwd=str(base))
    await git._arun(["remote", "add", "origin", str(remote)], cwd=str(base))
    names = {"aq/t", "aq/child", "aq/t-wip", "aq/child-wip"}
    for branch in names:
        await git._arun(["branch", branch], cwd=str(base))
    await git._arun(["push", "origin", "--all"], cwd=str(base))
    await git._arun(["branch", "-D", "aq/child-wip"], cwd=str(base))
    await git.aworktree_add(str(base), str(slot), ref="aq/t", detach=detached)
    await git._arun(["clone", "--template=", str(remote), str(other)], cwd=str(tmp_path))
    await git._arun(["checkout", "aq/child"], cwd=str(other))
    await env.db.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url=str(remote),
            checkout_base_path=str(base),
        )
    )
    await child(env)
    async with env.db.immediate() as conn:
        for task_id in ("t", "child"):
            await conn.execute(
                update(t.tasks)
                .where(t.tasks.c.id == task_id)
                .values(
                    repo_id="repo",
                    branch_name=f"aq/{task_id}",
                )
            )
    await env.db.create_workspace(
        Workspace(
            id="slot",
            project_id="p",
            workspace_path=str(slot),
            source_type=RepoSourceType.LINK,
            locked_by_task_id="t",
        )
    )
    await env.db.create_workspace(
        Workspace(
            id="other-checkout",
            project_id="p",
            workspace_path=str(other),
            source_type=RepoSourceType.CLONE,
        )
    )
    sha = await git.arev_parse(str(base), "main")
    return git, base, remote, slot, other, names, sha


@pytest.mark.parametrize(
    "branches,detached", [("keep", False), ("delete", False), ("delete", True)]
)
async def test_recursive_running_subtree_cleans_every_clone_remote_and_worktree(
    env, tmp_path, branches, detached
):
    provider, _ = await running_session(env, tmp_path)
    git, base, remote, slot, other, names, sha = await git_subtree(env, tmp_path, detached=detached)
    preview = await command(env, "delete_task", recursive=True, branches=branches)
    assert {b["branch"] for b in preview["branch_details"]} == names
    assert all(b["remote_sha"] == sha for b in preview["branch_details"])
    assert any(str(slot) == w["path"] for b in preview["branch_details"] for w in b["worktrees"])
    result = await delete_task(env, recursive=True, branches=branches, stop_running=True)
    assert result["success"], result
    assert "task-session" not in provider.running
    assert not [r for r in result["cleanup_results"] if r["outcome"] == "failed"], result
    assert result["task_ids"] == preview["task_ids"]
    assert await env.db.get_task("t") is None
    assert await env.db.get_task("child") is None
    audit = json.loads(
        (await env.db.get_recent_events(event_type="task.delete_requested"))[0]["payload"]
    )
    assert {b["branch"] for b in audit["preview"]["branch_details"]} == names
    assert all(b["remote_sha"] == sha for b in audit["preview"]["branch_details"])
    for branch in names:
        assert await git.arev_parse(str(remote), f"refs/heads/{branch}") == (
            sha if branches == "keep" else None
        )
    if branches == "delete":
        for path in (base, other):
            for branch in names:
                assert await git.arev_parse(str(path), f"refs/heads/{branch}") is None
                assert await git.arev_parse(str(path), f"refs/remotes/origin/{branch}") is None
        assert not slot.exists()
        assert (await env.db.get_workspace("slot")).enabled is False
        assert await git.arev_parse(str(base), "main") == sha
    else:
        assert slot.exists()
        assert await git.arev_parse(str(base), "aq/t") == sha
        assert await git.arev_parse(str(other), "aq/child") == sha


async def test_git_transport_failure_is_nonfatal_and_reported_in_audit(env, tmp_path, monkeypatch):
    git, _, remote, _, _, _, sha = await git_subtree(env, tmp_path)

    async def fail(*args, **kwargs):
        raise RuntimeError("remote unavailable")

    monkeypatch.setattr(git, "adelete_remote_ref_exact", fail)
    result = await delete_task(env, recursive=True, branches="delete")
    assert result["success"], result
    assert await env.db.get_task("t") is None
    assert await git.arev_parse(str(remote), "aq/t") == sha
    assert any(
        w["code"] == "cleanup_failed" and "remote unavailable" in w["message"]
        for w in result["warnings"]
    )
    audit = json.loads((await env.db.get_recent_events(event_type="task.deleted"))[0]["payload"])
    assert any(r["outcome"] == "failed" for r in audit["cleanup_results"])


async def test_pool_claim_file_is_removed_only_for_selected_claim(env, tmp_path):
    from src.claim_file import read_claim_file, write_claim_file

    await running_session(env, tmp_path)
    session = await env.db.get_session("s")
    task = await env.db.get_task("t")
    write_claim_file(session.work_dir, {"task_id": "t", "claim_epoch": task.claim_epoch})
    result = await delete_task(env)
    assert result["success"], result
    assert read_claim_file(session.work_dir) is None
