"""Authoritative source transitions write unconditional, transactional wait events."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import messages, tasks, wait_source_events
from src.integration.checks import ExactChecks
from src.models import Project, RepoSourceType, Task, TaskStatus, Workspace
from src.playbooks.run_state import RunLifecycle
from src.playbooks.waits import WaitChangeSet
from src.scheduler import PoolKey, PoolSupply
from src.waits.sources import (
    execute_task_status_mutation, insert_message_with_source_event, payload_hash,
    record_pool_observations,
)
from tests.test_integration_checks import REQUIRED, head, row
from tests.test_playbook_run_repository import ARTIFACT, seed_artifact
from tests.test_playbook_wait_repository import Event, make_receipt, make_wait, pause_run_on_waits
from tests.test_review_queries import _review, _revision


@pytest.fixture
async def db(reuse_database):
    db = await reuse_database("wait-dispatch")
    await db.create_project(Project(id="p", name="P"))
    for task_id in ("source", "owner"):
        await db.create_task(Task(id=task_id, project_id="p", title=task_id, description=""))
    return db


async def events(db, key):
    async with db._engine.connect() as conn:
        return (await conn.execute(select(wait_source_events).where(
            wait_source_events.c.source_key == key,
        ).order_by(wait_source_events.c.seq))).mappings().all()


async def test_status_cas_same_status_and_transaction_rollback(db):
    await db.transition_task("source", TaskStatus.READY)
    await db.transition_task("source", TaskStatus.READY)
    with pytest.raises(RuntimeError):
        async with db._engine.begin() as conn:
            await db._apply_transition(conn, "source", TaskStatus.BLOCKED)
            raise RuntimeError("abort")
    rows = await events(db, "task:source")
    assert len(rows) == 1 and rows[0]["payload"]["status"] == "READY"
    assert (await db.get_task("source")).status is TaskStatus.READY


async def test_fused_task_and_edge_payload_hashes_match_canonical_unicode_json(db):
    task_id = 'source:é/東京/"quoted"/\n'
    await db.create_task(Task(id=task_id, project_id="p", title="Unicode", description=""))
    await db.add_dependency("owner", task_id)
    await db.transition_task(task_id, TaskStatus.READY)
    task_events = await events(db, f"task:{task_id}")
    edge_events = await events(db, f"dependency:owner:{task_id}:blocks")
    assert len(task_events) == 1 and len(edge_events) == 2
    for event in task_events + edge_events:
        assert event["payload_hash"] == payload_hash(event["payload"])


async def test_legacy_status_write_records_only_real_transitions(db):
    await db.update_task("source", status=TaskStatus.READY)
    await db.update_task("source", status=TaskStatus.READY)
    assert len(await events(db, "task:source")) == 1


async def test_bulk_status_guard_preserves_returning_and_replay(db):
    async with db._engine.begin() as conn:
        stmt = update(tasks).where(tasks.c.project_id == "p", tasks.c.status == "DEFINED").values(
            status="READY", updated_at=10,
        ).returning(tasks.c.id)
        assert set((await execute_task_status_mutation(conn, stmt)).scalars()) == {"source", "owner"}
        assert not (await execute_task_status_mutation(conn, stmt)).all()
    assert len(await events(db, "task:source")) == len(await events(db, "task:owner")) == 1


async def test_dependency_insert_remove_and_replay(db):
    await db.add_dependency("owner", "source")
    await db.add_dependency("owner", "source")
    await db.remove_dependency("owner", "source")
    await db.remove_dependency("owner", "source")
    assert [e["payload"]["action"] for e in await events(db, "dependency:owner:source:blocks")] == [
        "added", "removed",
    ]


async def test_dependency_receives_upstream_status_transition_once(db):
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "source").values(claim_epoch=7))
    await db.add_dependency("owner", "source")
    await db.transition_task("source", TaskStatus.READY)
    await db.transition_task("source", TaskStatus.READY)
    rows = await events(db, "dependency:owner:source:blocks")
    assert [e["payload"]["action"] for e in rows] == ["added", "status_changed"]
    assert rows[-1]["payload"]["depends_on_task_id"] == "source"
    assert {row["source_generation"] for row in rows} == {7}


async def test_user_comment_only_records_verified_human_evidence(db):
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "source").values(claim_epoch=7))
    await db.add_task_comment("source", "answer", author_kind="user", author_id="dashboard")
    await db.add_task_comment("source", "note", author_kind="supervisor", author_id="supervisor-p")
    rows = await events(db, "human:task_comment:source")
    assert len(rows) == 1 and "answer" not in str(rows[0]["payload"])
    assert rows[0]["source_generation"] == 7


async def test_messages_include_internal_inserts_but_never_duplicate_or_leak_bodies(db):
    msg = await db.create_message(project_id="p", from_kind="user", from_id="dashboard",
                                  to_kind="task", to_id="owner", thread_id="thread", body="secret")
    async with db._engine.begin() as conn:
        stmt = pg_insert(messages).values(
            id="internal", project_id="p", from_kind="system", from_id="test", to_kind="task",
            to_id="owner", thread_id="thread", body="secret", created_at=1,
        ).on_conflict_do_nothing().returning(messages.c.id)
        result = await insert_message_with_source_event(conn, stmt)
        assert result.scalar_one() == "internal"
        assert (await insert_message_with_source_event(conn, stmt)).scalar_one_or_none() is None
    rows = await events(db, "message:thread")
    assert len(rows) == 2
    assert rows[0]["outcome_ref"] == f"message:{msg.id}"
    assert "secret" not in str(rows)


async def test_bulk_message_fanout_records_only_new_conflict_rows(db):
    def message(mid):
        return dict(id=mid, project_id="p", from_kind="system", from_id="test",
                    to_kind="task", to_id="owner", thread_id="bulk", body="secret", created_at=1)

    async with db._engine.begin() as conn:
        await insert_message_with_source_event(conn, pg_insert(messages).values(
            [message("m1"), message("m2")],
        ).on_conflict_do_nothing())
        assert (await insert_message_with_source_event(conn, pg_insert(messages).values(
            [message("m2"), message("m3")],
        ).on_conflict_do_nothing())).rowcount == 1
    assert [row["payload"]["message_id"] for row in await events(db, "message:bulk")] == [
        "m1", "m2", "m3",
    ]


async def test_message_insert_and_notification_rollback(db):
    with pytest.raises(RuntimeError):
        async with db._engine.begin() as conn:
            await insert_message_with_source_event(conn, insert(messages).values(
                id="rolled-back", project_id="p", from_kind="user", from_id="dashboard",
                to_kind="task", to_id="owner", thread_id="thread", body="answer", created_at=1,
            ))
            raise RuntimeError("abort")
    assert not await events(db, "message:thread")


async def test_review_decision_is_in_owning_transaction_and_replay_is_silent(db):
    async with db._engine.begin() as conn:
        await db.insert_review(
            review=_review(project_id="p", author_task_id="owner"),
            revision=_revision(submitted_task_id="owner"), conn=conn,
        )
    with pytest.raises(RuntimeError):
        async with db._engine.begin() as conn:
            assert await db.transition_review("rev-bright-harbor", from_states={"in_review"},
                expected_revision=1, values={"state": "approved"}, conn=conn)
            raise RuntimeError("abort")
    assert not await events(db, "review:rev-bright-harbor")
    async with db._engine.begin() as conn:
        for expected in (True, False):
            assert await db.transition_review("rev-bright-harbor", from_states={"in_review"},
                expected_revision=1, values={"state": "approved"}, conn=conn) is expected
    assert len(await events(db, "review:rev-bright-harbor")) == 1


async def test_workspace_release_is_by_actual_workspace_and_idempotent(db):
    await db.create_workspace(Workspace(id="w", project_id="p", workspace_path="/tmp/wait-source",
                                        source_type=RepoSourceType.LINK, locked_by_task_id="owner"))
    assert await db.release_workspaces_for_task("owner") == 1
    assert await db.release_workspaces_for_task("owner") == 0
    assert len(await events(db, "resource_invalidation:workspace:w")) == 1


async def test_provider_transition_and_replayed_generation(db):
    value = {"provider": "codex", "state": "available", "since": 1, "updated_at": 1}
    transition = {"provider": "codex", "from_state": "disabled", "to_state": "available",
                  "generation": 1, "at": 1}
    await db.save_provider_availability(value, transition)
    await db.save_provider_availability(value, transition)
    assert len(await events(db, "resource_invalidation:provider:codex")) == 1


async def test_config_reload_replay_keeps_one_observation_and_no_config_secrets(db):
    from src.orchestrator.core import Orchestrator

    config = SimpleNamespace(global_token_budget_daily=100, secret="never-persist")
    orch = SimpleNamespace(db=db, _config_watcher=None, budget=SimpleNamespace())
    data = {"config": config, "changed_sections": ["scheduling"]}
    await Orchestrator._on_config_reloaded(orch, data)
    await Orchestrator._on_config_reloaded(orch, data)
    rows = await events(db, "resource_invalidation:config:global")
    assert len(rows) == 1 and "never-persist" not in str(rows)


async def test_exact_sha_check_record_ignores_stale_observation(db):
    checks = ExactChecks(db, AsyncMock(required=REQUIRED, observe=AsyncMock(return_value=[
        row("unit", "success", observed_at=10),
    ])), clock=lambda: 10)
    await checks.refresh(head())
    await checks.refresh(head())
    checks.provider.observe.return_value = [row("unit", "pending", observed_at=9)]
    await checks.refresh(head())
    source_key = f"ci_check:repo:{head().sha}:unit"
    rows = await events(db, source_key)
    assert len(rows) == 1 and rows[0]["payload"]["conclusion"] == "success"


@pytest.mark.parametrize("resolution", ["clear", "expire", "claim", "boundary", "cancel"])
async def test_playbook_resolution_uses_registered_wait_snapshot_generation(db, resolution):
    await seed_artifact(db, ARTIFACT)
    snapshot = await pause_run_on_waits(db, make_wait(deadline_at=1_000_001))
    key = "playbook_condition:run-1:wait-1"
    assert not await events(db, key)
    if resolution == "clear":
        with pytest.raises(RuntimeError):
            async with db._engine.begin() as conn:
                await db.clear_for_run("run-1", conn=conn)
                raise RuntimeError("abort")
        assert not await events(db, key)
        await db.clear_for_run("run-1")
        await db.clear_for_run("run-1")
    elif resolution == "expire":
        await db.expire_due(1_000_002)
        await db.expire_due(1_000_003)
    elif resolution == "claim":
        event = Event("pr.merged", "e1", {"pr": {"number": 41}})
        await db.claim_for_event(event, now=1_000_002)
        await db.claim_for_event(event, now=1_000_003)
    elif resolution == "boundary":
        await db.commit_boundary(
            replace(snapshot, lifecycle=RunLifecycle.RUNNING),
            make_receipt(snapshot, attempt=2), WaitChangeSet(clear_wait_ids=("wait-1",)),
        )
    else:
        await db.request_cancel("run-1", expected_version=snapshot.version,
                                reason="test", requested_by="supervisor")
    rows = await events(db, key)
    assert len(rows) == 1
    assert rows[0]["source_generation"] == snapshot.version
    assert rows[0]["payload"]["wait_id"] == "wait-1"


async def test_pool_sizing_observation_is_replayed_by_cycle(db):
    key = PoolKey(profile_id="worker")
    measurement = SimpleNamespace(supply={key: PoolSupply(running_busy=1)}, demand={key: 2},
                                  bounds={key: (0, 2)})
    await record_pool_observations(db, measurement, 100)
    await record_pool_observations(db, measurement, 100)
    assert len(await events(db, "resource_invalidation:pool:worker")) == 1
