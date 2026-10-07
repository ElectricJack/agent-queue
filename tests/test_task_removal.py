"""Confirmed operator removal preserves audit, fences writers, and retires orphans."""

import pytest
from sqlalchemy import insert, select, text, update

from src.database import tables as t
from src.integration.task_removal import settle_orphaned_parent_operations
from src.models import RepoConfig, RepoSourceType, Task, TaskStatus
from tests.test_task_controls import command, env as env, running_session

pytestmark = pytest.mark.asyncio


async def child(env, status=TaskStatus.READY):
    await env.db.create_task(Task(
        id="child", project_id="p", title="Child", description="Child work", status=status,
        parent_task_id="t", profile_id="worker", route_source="legacy",
    ))


async def operation(env, *, subject=False):
    await env.db.create_repo(RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.LINK))
    async with env.db.immediate() as conn:
        await conn.execute(insert(t.integration_parent_episodes).values(
            id="episode", parent_task_id="t", repository_id="repo", generation=0,
            pre_collection_checkpoint_sha="a" * 40, created_at=1,
        ))
        await conn.execute(insert(t.integration_repair_operations).values(
            id="operation", target_kind="parent", parent_task_id="t", episode_id="episode",
            state="active", policy_snapshot={}, artifact_snapshot={}, required_check_version="v1",
            created_at=1, updated_at=1,
        ))
        if subject:
            await conn.execute(insert(t.playbook_artifacts).values(
                artifact_sha256="sha256:" + "1" * 64, playbook_id="test",
                source_digest="sha256:" + "2" * 64, contract_fingerprint="sha256:" + "3" * 64,
                compiler_build="test", path="/artifacts/test", created_at=1,
            ))
            await conn.execute(insert(t.integration_subjects).values(
                id="subject", project_id="p", repository_id="repo", task_id="t",
                parent_episode_id="episode", kind="parent_episode", subject_key="parent:t:0",
                engine="reconciler", phase="building", policy_artifact_sha256="sha256:" + "1" * 64,
                policy_playbook_id="test", next_due_at=2, due_set_at=1, max_wait_seconds=3600,
                created_at=1, updated_at=1,
            ))


async def state(env, table, field):
    async with env.db._engine.connect() as conn:
        return await conn.scalar(select(table.c[field]))


async def remove(env):
    return await command(env, "remove_task", confirmed=True, reason="Superseded by new epic")


async def test_preview_and_missing_reason_make_no_changes(env):
    await child(env)
    preview = await command(env, "remove_task")
    assert preview["disposition"] == "preview"
    assert set(preview["task_ids"]) == {"t", "child"}
    assert (await env.db.get_task("t")).status == TaskStatus.READY
    rejected = await command(env, "remove_task", confirmed=True)
    assert rejected["code"] == "removal.reason_required"
    assert (await env.db.get_task("child")).status == TaskStatus.READY


async def test_remove_plain_subtree_keeps_peer_and_local_branch_artifacts(env, tmp_path):
    await child(env, TaskStatus.DEFINED)
    artifact = tmp_path / "branch-artifact"
    artifact.write_text("keep")
    result = await remove(env)
    assert result["success"], result
    assert result["disposition"] == "deleted"
    assert result["branches"] == "keep"
    assert await env.db.get_task("t") is None
    assert await env.db.get_task("child") is None
    assert (await env.db.get_task("peer")).status == TaskStatus.READY
    assert artifact.read_text() == "keep"


async def test_remove_stops_exact_running_session_before_releasing_resources(env, tmp_path):
    provider, _ = await running_session(env, tmp_path)
    await child(env)
    result = await remove(env)
    assert result["success"], result
    assert "task-session" not in provider.running
    assert "peer-session" in provider.running
    assert (await env.db.get_session("s")).state == "stopped"
    assert (await env.db.get_workspace("workspace")).locked_by_task_id is None
    assert (await env.db.get_agent("agent")).current_task_id is None
    assert await env.db.get_task("t") is None


async def test_failed_stop_holds_entire_subtree_and_retry_finishes(env, tmp_path):
    provider, _ = await running_session(env, tmp_path)
    await child(env)
    provider.fail_stop = True
    result = await remove(env)
    assert result["code"] == "removal.cleanup_pending", result
    assert (await env.db.get_task("child")).status == TaskStatus.PAUSED
    assert await env.db.assign_task_to_agent("child", "agent") is False
    assert (await env.db.get_workspace("workspace")).locked_by_task_id == "t"
    provider.fail_stop = False
    assert (await remove(env))["success"]
    assert await env.db.get_task("child") is None


async def test_remove_cancels_legacy_operation_and_archives_parent_episode(env):
    await operation(env)
    result = await remove(env)
    assert result["success"], result
    assert result["disposition"] == "archived"
    assert result["cancelled_operations"] == ["operation"]
    assert await state(env, t.integration_repair_operations, "state") == "cancelled"
    assert await state(env, t.integration_parent_episodes, "parent_task_id") == "t"
    archived = await env.db.get_archived_task("t")
    assert archived["branch_name"] == "preserved"
    assert "Superseded by new epic" in str(await state(env, t.task_comments, "body"))


async def test_orphan_maintenance_respects_live_subject_until_engine_retired(env):
    await operation(env, subject=True)
    assert await settle_orphaned_parent_operations(env.db, 10) == []
    assert await state(env, t.integration_repair_operations, "state") == "active"
    assert await settle_orphaned_parent_operations(env.db, 11, legacy_engine_gone=True) == ["operation"]
    assert await state(env, t.integration_subjects, "phase") == "done"
    assert await settle_orphaned_parent_operations(env.db, 12, legacy_engine_gone=True) == []


async def test_orphan_maintenance_preserves_attached_writer(env, tmp_path):
    await operation(env)
    provider, _ = await running_session(env, tmp_path)
    assert await settle_orphaned_parent_operations(env.db, 10, legacy_engine_gone=True) == []
    assert await state(env, t.integration_repair_operations, "state") == "active"
    assert "task-session" in provider.running


async def test_orphan_maintenance_retires_queued_delegate_before_claim(env):
    await operation(env)
    async with env.db.immediate() as conn:
        await conn.execute(update(t.integration_repair_operations).where(
            t.integration_repair_operations.c.id == "operation",
        ).values(verifier_task_id="peer"))
    assert await settle_orphaned_parent_operations(env.db, 10) == ["operation"]
    assert (await env.db.get_task("peer")).status == TaskStatus.FAILED
    assert await env.db.assign_task_to_agent("peer", "agent") is False
    assert await state(env, t.integration_delegate_releases, "task_id") == "peer"


async def test_orphan_maintenance_skips_engine_lock_without_waiting(env):
    import asyncio
    from src.integration.owner_guards import parent_lock_key

    await operation(env)
    async with env.db.immediate() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"),
                           {"key": parent_lock_key("t")})
        assert await asyncio.wait_for(settle_orphaned_parent_operations(env.db, 10), 5) == []
    assert await state(env, t.integration_repair_operations, "state") == "active"


async def test_confirmed_remove_stays_fenced_while_cleanup_runs(env, monkeypatch):
    await child(env)
    finish = env.orch._finish_manual_pause

    async def check_hold(task_id, snapshot):
        for tid in ("t", "child"):
            assert (await env.db.get_task(tid)).status == TaskStatus.PAUSED
            assert await env.db.assign_task_to_agent(tid, "agent") is False
        await finish(task_id, snapshot)

    monkeypatch.setattr(env.orch, "_finish_manual_pause", check_hold)
    assert (await remove(env))["success"]


async def test_live_subject_refusal_keeps_audit_and_task(env):
    await operation(env, subject=True)
    result = await remove(env)
    assert result["code"] == "hierarchy.integration_owned", result
    assert await env.db.get_task("t") is not None
    assert await state(env, t.integration_repair_operations, "state") == "active"


@pytest.mark.parametrize("status", [TaskStatus.DEFINED, TaskStatus.READY, TaskStatus.PAUSED])
async def test_archive_open_task_requires_and_retains_reason(env, status):
    await env.db.transition_task("t", status, force=True)
    refused = await command(env, "archive_task")
    assert "error" in refused
    result = await command(env, "archive_task", reason="Dead epic")
    assert "error" not in result, result
    assert await env.db.get_task("t") is None
    assert "Dead epic" in str(await state(env, t.task_comments, "body"))


async def delivered_batch(env, *, lifecycle="sealed", intent="open"):
    await env.db.create_repo(RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.LINK))
    await env.db.transition_task("t", TaskStatus.COMPLETED, force=True)
    async with env.db.immediate() as conn:
        await conn.execute(insert(t.integration_batches).values(
            id="batch", project_id="p", repository_id="repo", request_id="request",
            source_manifest_digest="sha256:" + "1" * 64, lifecycle="sealing", intent="open",
            base_sha="a" * 40, integration_branch="integration/batch", policy_snapshot={},
            artifact_snapshot={}, cleanup_state="pending", created_at=1, updated_at=1,
        ))
        await conn.execute(insert(t.integration_batch_members).values(
            batch_id="batch", ordinal=0, task_id="t", repository_id="repo",
            source_sha="b" * 40,
            source_base_sha="a" * 40, reviewed_head_sha="b" * 40, reviewed_tree_sha="c" * 40,
            review_evidence={},
        ))
        await conn.execute(update(t.integration_batches).where(t.integration_batches.c.id == "batch")
                           .values(lifecycle=lifecycle, intent=intent))
        await conn.execute(insert(t.task_delivery_receipts).values(
            id="receipt", domain_key="batch:batch", repository_id="repo", target_branch="main",
            source_task_id="t", disposition="code", created_at=1,
        ))


async def test_remove_aborts_sealed_batch_and_preserves_delivered_task(env):
    await delivered_batch(env)
    result = await remove(env)
    assert result["success"], result
    assert result["aborted_batches"] == ["batch"]
    assert result["disposition"] == "archived"
    assert await state(env, t.integration_batches, "intent") == "aborted"
    assert await state(env, t.task_delivery_receipts, "source_task_id") == "t"
    assert (await env.db.get_archived_task("t"))["status"] == "COMPLETED"


async def test_archive_delivered_task_in_aborted_batch(env):
    await delivered_batch(env, lifecycle="aborted", intent="aborted")
    result = await command(env, "archive_task")
    assert "error" not in result, result
    assert await env.db.get_archived_task("t") is not None


async def test_remove_reconciles_aborted_intent_with_stale_sealed_lifecycle(env):
    await delivered_batch(env, intent="aborted")
    result = await remove(env)
    assert result["success"], result
    assert await state(env, t.integration_batches, "lifecycle") == "aborted"


async def test_remove_rolls_back_batch_abort_when_a_valid_subject_still_owns_task(env):
    await operation(env, subject=True)
    # Reuse the repository owned by the parent operation fixture.
    await env.db.transition_task("t", TaskStatus.COMPLETED, force=True)
    async with env.db.immediate() as conn:
        await conn.execute(insert(t.integration_batches).values(
            id="batch", project_id="p", repository_id="repo", request_id="request",
            source_manifest_digest="digest", lifecycle="sealing", base_sha="a" * 40,
            integration_branch="integration/batch", policy_snapshot={}, artifact_snapshot={},
            cleanup_state="pending", created_at=1, updated_at=1,
        ))
        await conn.execute(insert(t.integration_batch_members).values(
            batch_id="batch", ordinal=0, task_id="t", repository_id="repo", source_sha="b" * 40,
            source_base_sha="a" * 40, reviewed_head_sha="b" * 40, reviewed_tree_sha="c" * 40,
            review_evidence={},
        ))
        await conn.execute(update(t.integration_batches).where(
            t.integration_batches.c.id == "batch",
        ).values(lifecycle="sealed"))
    result = await remove(env)
    assert result["code"] == "hierarchy.integration_owned", result
    assert await state(env, t.integration_batches, "lifecycle") == "sealed"
    assert await state(env, t.integration_batches, "intent") == "open"
    assert await env.db.get_archived_task("t") is None


async def test_remove_worker_scope_denied_without_pausing(env):
    from dataclasses import asdict
    from src.api.auth import RequestScope

    result = await command(env, "remove_task", confirmed=True, reason="reason",
                           _scope=asdict(RequestScope(kind="session", session_id="s", project_id="p")))
    assert "error" in result
    assert (await env.db.get_task("t")).status == TaskStatus.READY


async def test_live_project_supervisor_can_remove_but_cannot_cross_project(env):
    from dataclasses import asdict
    from src.api.auth import RequestScope
    from src.models import SessionRecord

    await env.db.create_session(SessionRecord(
        id="supervisor", project_id="p", profile_id="supervisor", harness="fake", provider="fake",
        name="supervisor", lifecycle="named", state="running", work_dir="/tmp",
        epoch="test", instance_token="instance", started_at=1,
    ))
    scope = asdict(RequestScope(kind="session", session_id="supervisor", project_id="p", elevated=True))
    await env.db.create_task(Task(id="foreign", project_id="other", title="Foreign", description=""))
    refused = await command(env, "remove_task", "foreign", confirmed=True, reason="Dead", _scope=scope)
    assert "error" in refused
    assert await env.db.get_task("foreign") is not None
    allowed = await command(env, "remove_task", confirmed=True, reason="Dead", _scope=scope)
    assert allowed["success"], allowed


async def test_remove_standalone_delegate_retires_its_orphan_parent_operation(env):
    await operation(env)
    async with env.db.immediate() as conn:
        await conn.execute(update(t.integration_repair_operations).where(
            t.integration_repair_operations.c.id == "operation",
        ).values(verifier_task_id="peer"))
    result = await command(env, "remove_task", "peer", confirmed=True, reason="Obsolete verifier")
    assert result["success"], result
    assert result["disposition"] == "archived"
    assert result["cancelled_operations"] == ["operation"]
    assert await env.db.get_archived_task("peer") is not None
    assert await env.db.get_task("t") is not None
