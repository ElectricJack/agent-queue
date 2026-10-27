"""Authoritative source transitions write unconditional, transactional wait events."""

from dataclasses import replace
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from asyncpg.exceptions import DeadlockDetectedError, SerializationError, UniqueViolationError
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError

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

import asyncio

from src.commands import CommandHandler
from src.commands.principal import ExecutionPrincipal, principal_context
from src.config import AppConfig
from src.database.tables import wait_deadlines, wait_listener_cursors, wait_records
from src.integration.admission import observe_admission
from src.waits.conditions import Condition, Scope
from src.waits.listener import LeaseLost, ListenerUnavailable
from src.waits.registry import build_registry
from src.waits.resource_sources import resource_invalidation
from src.waits.timers import WaitTimerDriver
from src.wait_runtime.service import WaitService
from src.waits.sources import record_source_event
from tests.test_wait_queries import (
    NOW, env as _runtime_env, records, register, resource_snapshot,
)


runtime_env = _runtime_env

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




class ObservingDispatcher:
    """Test outcome owner: source events alone cannot satisfy the predicate."""

    def __init__(self, registry):
        self.registry = registry
        self.calls = []
        self.fail = False

    async def commit(self, conn, row, event, *, evidence=None):
        observation = await self.registry.observe(conn, Condition.from_record(row["condition"]))
        self.calls.append((row["id"], event["seq"], evidence))
        if observation.state == "satisfied":
            await conn.execute(update(wait_records).where(
                wait_records.c.id == row["id"], wait_records.c.outcome == "pending",
            ).values(outcome="satisfied", result_ref=observation.result_ref))
        if self.fail:
            raise RuntimeError("dispatch abort")
        return {"success": True}


def runtime(env, *, clock=lambda: NOW, dispatcher=None, enabled=True, registry=None):
    config = AppConfig()
    config.wait_policy.v2_enabled = enabled
    orch = SimpleNamespace(db=env.db, config=config, bus=SimpleNamespace(emit=AsyncMock()),
                           _delivery_admission=lambda ids: observe_admission(env.db, ids, None),
                           plugin_registry=None, wait_registry=registry or env.registry,
                           wait_dispatcher=dispatcher)
    orch._command_handler = CommandHandler(orch, config)
    return WaitService(orch, clock=clock)


async def test_source_batch_can_exceed_renewal_cadence_without_restarting(runtime_env):
    env = runtime_env
    await register(env)
    dispatcher = ObservingDispatcher(env.registry)
    original = dispatcher.commit

    async def slow(conn, row, event, **kwargs):
        await asyncio.sleep(0.03)
        return await original(conn, row, event, **kwargs)

    dispatcher.commit = slow
    service = runtime(env, dispatcher=dispatcher)
    service.listener.renewal_seconds = 0.01
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await service.tick()
    assert (await records(env))[0]["outcome"] == "satisfied"
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] > 0


async def test_slow_batches_commit_only_processed_prefix_and_renew_before_continuing(runtime_env):
    now = [NOW]
    service = runtime(runtime_env, clock=lambda: now[0])
    for n in range(8):
        await runtime_env.db.wait_source_commit(source_key='test:slow', generation=0,
            revision=str(n), outcome_ref=f'test:{n}', payload={})
    expected = [row['seq'] for row in await records(runtime_env, wait_source_events)]
    seen = []
    async def slow(conn, event):
        seen.append(event['seq'])
        now[0] += 6
    service.listener._dispatch = slow
    assert await service.listener.tick()
    assert seen == sorted(expected)
    assert now[0] - NOW > service.listener.lease_seconds
    cursor = (await records(runtime_env, wait_listener_cursors))[0]
    assert cursor['last_seq'] == max(expected)
    assert all(row['dispatched_at'] is not None
               for row in await records(runtime_env, wait_source_events))


async def test_observation_retries_are_bounded_and_keep_monotonic_cursor(runtime_env, caplog):
    now = [NOW]
    service = runtime(runtime_env, clock=lambda: now[0])
    service.listener.retry_batch_size = 1
    for n in range(3):
        await runtime_env.db.wait_source_commit(
            source_key="test:retry", generation=0, revision=str(n),
            outcome_ref=f"retry:{n}", payload={}, occurred_at=NOW,
        )
    dispatch = service.listener._dispatch = AsyncMock(return_value=30)
    await service.listener.tick()
    cursor = (await records(runtime_env, wait_listener_cursors))[0]["last_seq"]
    assert dispatch.await_count == 3
    assert "wait.source_observation_deferred" in caplog.text
    assert "source_key=test:retry" in caplog.text
    assert all(event["dispatched_at"] is None
               for event in await records(runtime_env, wait_source_events))
    now[0] += 31
    dispatch.reset_mock()
    dispatch.return_value = 0
    await service.listener.tick()
    assert dispatch.await_count == 1
    assert (await records(runtime_env, wait_listener_cursors))[0]["last_seq"] == cursor
    assert sum(event["dispatched_at"] is not None
               for event in await records(runtime_env, wait_source_events)) == 1
    # A bounded retry pass leaves the remaining durable events for later ticks.
    await service.listener.tick()
    await service.listener.tick()
    assert all(event["dispatched_at"] is not None
               for event in await records(runtime_env, wait_source_events))


async def test_aged_retry_respects_backoff_and_shares_turns_across_drains(runtime_env):
    env = runtime_env
    now = [NOW]
    service = runtime(env, clock=lambda: now[0])
    listener = service.listener
    listener.drain_budget_seconds = 0
    retained = await env.db.wait_source_commit(source_key="test:retry-fairness", generation=0,
        revision="1", outcome_ref="retry", payload={})
    seen = []

    async def unavailable(conn, event):
        seen.append(event["seq"])
        return 120 if event["seq"] == retained else 0

    listener._dispatch = unavailable
    await listener.tick()
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_source_events).where(
            wait_source_events.c.seq == retained,
        ).values(committed_at=NOW))
    later = [await env.db.wait_source_commit(source_key=f"test:forward:{n}", generation=0,
        revision="1", outcome_ref="forward", payload={}) for n in range(3)]
    # It is old enough for a fair turn, but its observation backoff still applies.
    now[0] += 61
    await listener.tick()
    assert seen == [retained, later[0]]
    # Due retries run without waiting for the forward queue to empty.
    now[0] += 61
    await listener.tick()
    assert seen == [retained, later[0], retained]
    # Even with backoff expired again, give forward work the next turn.
    now[0] += 121
    await listener.tick()
    assert seen == [retained, later[0], retained, later[1]]
    await listener.tick()
    assert seen == [retained, later[0], retained, later[1], retained]
    sources = {r["seq"]: r for r in await records(env, wait_source_events)}
    assert sources[retained]["dispatched_at"] is None
    assert sources[later[2]]["dispatched_at"] is None
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == later[1]


async def test_event_failure_preserves_committed_prefix_and_replays_only_failed_event(runtime_env):
    env = runtime_env
    for n in range(3):
        await env.db.wait_source_commit(source_key='test:prefix', generation=0,
            revision=str(n), outcome_ref=f'test:{n}', payload={})
    expected = sorted(row['seq'] for row in await records(env, wait_source_events))
    service = runtime(env)
    seen = []

    async def dispatch(conn, event):
        seen.append(event['seq'])
        if event['seq'] == expected[1]:
            raise RuntimeError('second event failed')

    service.listener._dispatch = dispatch
    with pytest.raises(RuntimeError, match='second event failed'):
        await service.listener.tick()
    cursor = (await records(env, wait_listener_cursors))[0]
    assert cursor['last_seq'] == expected[0]
    rows = sorted(await records(env, wait_source_events), key=lambda row: row['seq'])
    assert rows[0]['dispatched_at'] is not None
    assert all(row['dispatched_at'] is None for row in rows[1:])
    await service.lease(release=True)
    restarted = runtime(env)

    async def replay(conn, event):
        seen.append(event['seq'])

    restarted.listener._dispatch = replay
    assert await restarted.listener.tick()
    assert seen == [expected[0], expected[1], expected[1], expected[2]]
    assert (await records(env, wait_listener_cursors))[0]['last_seq'] == expected[-1]


async def test_irrelevant_batch_rolls_back_on_expired_lease_and_replays(runtime_env, monkeypatch):
    env = runtime_env
    for n in range(3):
        await env.db.wait_source_commit(source_key="ci_check:repo:sha:unit", generation=0,
            revision=str(n), outcome_ref=f"check:{n}", payload={})
    now = [NOW]
    service = runtime(env, clock=lambda: now[0])
    prefix = service.listener._unobserved_prefix

    async def expire(conn, rows):
        from src.waits.graph import graph_locked

        result = await prefix(conn, rows)
        assert len(result) == 3
        if graph_locked(conn):
            now[0] += service.listener.lease_seconds + 1
        return result

    monkeypatch.setattr(service.listener, "_unobserved_prefix", expire)
    with pytest.raises(LeaseLost):
        await service.listener.tick()
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == 0
    assert all(event["dispatched_at"] is None for event in await records(env, wait_source_events))
    monkeypatch.setattr(service.listener, "_unobserved_prefix", prefix)
    assert await service.listener.tick()
    assert all(event["dispatched_at"] is not None for event in await records(env, wait_source_events))


@pytest.mark.parametrize('kind', ['ci_check', 'message', 'dependency', 'task', 'job', 'batch'])
async def test_irrelevant_batch_stops_before_subscribed_event(runtime_env, kind):
    env = runtime_env
    receipt = await register(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).where(
            wait_records.c.id == receipt["wait_id"],
        ).values(source_key=f"{kind}:subscribed", source_generation=0))
    for key in (f"{kind}:other", f"{kind}:subscribed", f"{kind}:other"):
        await env.db.wait_source_commit(source_key=key, generation=0,
            revision=str(len(await records(env, wait_source_events))), outcome_ref=key, payload={})
    source = sorted(await records(env, wait_source_events), key=lambda event: event["seq"])
    service = runtime(env)
    # No dispatcher: the subscribed middle event must fail, retaining its cursor,
    # while the irrelevant prefix is already committed and the suffix untouched.
    with pytest.raises(ListenerUnavailable):
        await service.listener.tick()
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == source[0]["seq"]
    dispatched = sorted(await records(env, wait_source_events), key=lambda event: event["seq"])
    assert dispatched[0]["dispatched_at"] is not None
    assert all(event["dispatched_at"] is None for event in dispatched[1:])
    dispatcher = SimpleNamespace(commit=AsyncMock(return_value={"success": True}))
    service.orch.wait_dispatcher = dispatcher
    assert await service.listener.tick()
    assert dispatcher.commit.await_count == 1
    assert dispatcher.commit.await_args.args[2]["seq"] == source[1]["seq"]
    assert all(event["dispatched_at"] is not None for event in await records(env, wait_source_events))


async def test_mixed_backlog_batches_preserve_terminal_and_provider_callbacks(runtime_env):
    env = runtime_env
    service = runtime(env)
    ordinary = [
        ('message:mail', {}), ('dependency:a:b:blocks', {}),
        ('task:changing', {'status': 'IN_PROGRESS'}), ('ci_run:repo:head', {}),
        ('batch:batch', {}), ('job:job', {}), ('playbook_condition:run:wait', {}),
        ('resource_invalidation:workspace:ws', {}), ('resource_invalidation:config:cfg', {}),
    ]
    terminal = ('task:gone', {'status': 'COMPLETED'})
    provider = ('resource_invalidation:provider:fake', {'identity': 'fake'})
    sequence = ordinary * 25 + [terminal] + ordinary + [provider] + ordinary
    async with env.db._engine.begin() as conn:
        for n, (key, payload) in enumerate(sequence):
            await record_source_event(conn, source_key=key, generation=0,
                revision=str(n), outcome_ref=f'event:{n}', payload=payload, occurred_at=NOW)
    dispatch = service.listener._dispatch
    service.listener._dispatch = AsyncMock(wraps=dispatch)
    drain_one = service.listener._drain_one
    service.listener._drain_one = AsyncMock(wraps=drain_one)
    assert await service.listener.tick()
    assert [call.args[1]['source_key'] for call in service.listener._dispatch.await_args_list] == [
        terminal[0], provider[0],
    ]
    # Count transactions: prefix planning now runs both before and after the fence.
    # More than one bounded batch, but no per-event transactions for irrelevant traffic.
    assert 1 < service.listener._drain_one.await_count < 10
    source = await records(env, wait_source_events)
    assert len(source) == len(sequence) and all(event['dispatched_at'] is not None for event in source)
    assert (await records(env, wait_listener_cursors))[0]['last_seq'] == max(
        event['seq'] for event in source)


async def test_unused_ci_retries_retire_in_batches_during_backoff_without_observation(
    runtime_env, monkeypatch,
):
    from src.waits import loops

    env = runtime_env
    service = runtime(env)
    listener = service.listener
    assert await service.lease()
    for n in range(5):
        await env.db.wait_source_commit(source_key=f"ci_check:repo:sha:unit-{n}", generation=0,
            revision="success", outcome_ref=f"check:{n}", payload={})
    # An unrelated event behind the cursor still requires its lifecycle dispatch.
    other = await env.db.wait_source_commit(source_key="ciXcheck:lifecycle", generation=0,
        revision="1", outcome_ref="other", payload={})
    watermark = await env.db.wait_source_watermark()
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_listener_cursors).values(last_seq=watermark))
    listener.batch_size = 2
    listener._retry_due = {r["seq"]: NOW + 1000 for r in await records(env, wait_source_events)
                           if r["seq"] != other}
    prepare = AsyncMock(side_effect=AssertionError("unused CI must not prepare owner progress"))
    monkeypatch.setattr(loops, "prepare_progress", prepare)
    dispatched = []

    async def dispatch(conn, event):
        assert event["seq"] == other
        dispatched.append(event["seq"])

    listener._dispatch = dispatch
    # The only preparation/dispatch belongs to the unrelated lifecycle source.
    prepare.side_effect = None
    assert await service.drain(watermark) == 3
    assert dispatched == [other]
    assert prepare.await_count == 1
    prepare.side_effect = AssertionError("unused CI must not prepare owner progress")
    assert listener.changed.is_set()
    assert await service.drain(watermark) == 2
    assert await service.drain(watermark) == 1
    assert all(r["dispatched_at"] is not None for r in await records(env, wait_source_events))
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == watermark
    assert listener._retry_due == {}


@pytest.mark.parametrize("producer", ["pool", "config", "workspace"])
@pytest.mark.parametrize("retained", [False, True])
async def test_unused_resource_backlog_retires_but_provider_lifecycle_still_dispatches(
    runtime_env, monkeypatch, producer, retained,
):
    env = runtime_env
    service = runtime(env)
    listener = service.listener
    assert await service.lease()
    unused = []
    for n in range(5):
        unused.append(await env.db.wait_source_commit(**resource_invalidation(
            producer=producer, identity=f"source-{n}", revision="1", outcome_ref="hint",
            payload={})))
    provider = await env.db.wait_source_commit(**resource_invalidation(
        producer="provider", identity="fake", revision="1", outcome_ref="recovery", payload={}))
    watermark = await env.db.wait_source_watermark()
    if retained:
        async with env.db._engine.begin() as conn:
            await conn.execute(update(wait_listener_cursors).values(last_seq=watermark))
        listener._retry_due = {seq: NOW + 1000 for seq in unused}
    listener.batch_size = 2
    recovery = AsyncMock()
    monkeypatch.setattr(service.lifecycle.resume, "recovery_event", recovery)
    observed = []
    original = listener._dispatch

    async def dispatch(conn, event):
        assert event["seq"] == provider
        observed.append(event["seq"])
        return await original(conn, event)

    monkeypatch.setattr(listener, "_dispatch", dispatch)
    assert sum([await service.drain(watermark) for _ in range(3)]) == 6
    assert observed == [provider]
    recovery.assert_awaited_once()
    assert recovery.await_args.args[1]["seq"] == provider
    assert all(r["dispatched_at"] is not None for r in await records(env, wait_source_events))
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == watermark
    assert listener._retry_due == {}


@pytest.mark.parametrize("source", [None, "ci_check:repo:sha:unit", "resource_invalidation:workspace:w"])
async def test_unsubscribed_listener_does_not_prepare_fleet_admission(runtime_env, monkeypatch, source):
    service = runtime(runtime_env)
    if source:
        await runtime_env.db.wait_source_commit(source_key=source, generation=0,
            revision="1", outcome_ref="hint", payload={})

    def forbidden(**kwargs):
        pytest.fail("unsubscribed listener must not prepare fleet admission")

    monkeypatch.setattr(service.scheduler.reader, "prepared", forbidden)
    assert await service.listener.tick()
    assert all(r["dispatched_at"] is not None
               for r in await records(runtime_env, wait_source_events))


async def test_listener_prepares_admission_outside_graph_after_registration_race(
    runtime_env, monkeypatch,
):
    from src.waits import listener as listener_module

    env = runtime_env
    observation = {"state": "unsatisfied", "revision": "1"}
    reader = AsyncMock(side_effect=lambda conn, cond: resource_snapshot(cond, **observation))
    registry = build_registry(resource_reader=reader)
    seq = await env.db.wait_source_commit(**resource_invalidation(
        producer="workspace", identity="w", revision="1", outcome_ref="hint", payload={}))
    service = runtime(env, registry=registry, dispatcher=ObservingDispatcher(registry))
    original_lock = listener_module.lock_graph
    registered = False
    preparations = []

    async def register_before_lock(conn, **kwargs):
        nonlocal registered
        if not registered:
            registered = True
            await register(env, {"kind": "resource", "blocker": "workspace", "identity": "opaque",
                                 "source_revision": "1"}, registry=registry,
                           scope=Scope("p", "scheduler", "source", principal="service"))
            observation.update(state="satisfied", revision="2")
        await original_lock(conn, **kwargs)

    @asynccontextmanager
    async def prepared(**kwargs):
        # Taking this fence in a different transaction proves preparation
        # cannot be entered while the dispatch transaction still holds it.
        async with env.db._engine.begin() as conn:
            await original_lock(conn, queued=True, timeout=0.25)
        preparations.append(kwargs)
        yield

    monkeypatch.setattr(listener_module, "lock_graph", register_before_lock)
    monkeypatch.setattr(service.scheduler.reader, "prepared", prepared)
    assert await service.listener.tick()
    assert len(preparations) == 1
    assert (await records(env))[0]["outcome"] == "satisfied"
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == seq


@pytest.mark.parametrize("producer", ["pool", "config", "workspace"])
async def test_resource_retry_retirement_preserves_pending_resource_subscriber(runtime_env, producer):
    env = runtime_env
    reader = AsyncMock(side_effect=lambda conn, cond: resource_snapshot(
        cond, state="unsatisfied", revision="1"))
    registry = build_registry(resource_reader=reader)
    await register(env, {"kind": "resource", "blocker": "slot", "identity": "opaque",
                         "source_revision": "1"}, registry=registry,
                   scope=Scope("p", "scheduler", "source", principal="service"))
    seq = await env.db.wait_source_commit(**resource_invalidation(
        producer=producer, identity="producer", revision="1", outcome_ref="hint", payload={}))
    service = runtime(env, registry=registry)
    assert await service.lease()
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_listener_cursors).values(last_seq=seq))
    assert await service.listener._retire_unused_retries(seq) == 0
    assert (await records(env, wait_source_events))[0]["dispatched_at"] is None


@pytest.mark.parametrize("subscriber", [
    "pending", "satisfied", "cancelled", "unverified", "watermarked", "other_generation",
])
async def test_ci_retry_retirement_preserves_only_unobserved_pending_subscribers(
    runtime_env, subscriber,
):
    env = runtime_env
    receipt = await register(env)
    seq = await env.db.wait_source_commit(source_key="ci_check:repo:sha:unit", generation=0,
        revision="success", outcome_ref="check", payload={})
    service = runtime(env)
    assert await service.lease()
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_listener_cursors).values(last_seq=seq))
        await conn.execute(update(wait_records).where(
            wait_records.c.id == receipt["wait_id"],
        ).values(source_key="ci_check:repo:sha:unit",
                 source_generation=1 if subscriber == "other_generation" else 0,
                 outcome=subscriber if subscriber in {"satisfied", "cancelled"} else "pending",
                 verification_state="stale" if subscriber == "unverified" else "verified",
                 source_watermark_seq=seq if subscriber == "watermarked" else 0))
    retired = await service.listener._retire_unused_retries(seq)
    assert retired == (0 if subscriber == "pending" else 1)
    assert ((await records(env, wait_source_events))[0]["dispatched_at"] is None) == (
        subscriber == "pending")
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == seq


async def test_ci_retry_retirement_rechecks_subscriptions_after_graph_fence(runtime_env, monkeypatch):
    from src.waits import listener as listener_module

    env = runtime_env
    receipt = await register(env)
    seq = await env.db.wait_source_commit(source_key="ci_check:repo:sha:unit", generation=0,
        revision="success", outcome_ref="check", payload={})
    service = runtime(env)
    assert await service.lease()
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_listener_cursors).values(last_seq=seq))
    original_lock = listener_module.lock_graph

    async def subscribe_before_lock(conn, **kwargs):
        async with env.db._engine.begin() as registration:
            await original_lock(registration)
            await registration.execute(update(wait_records).where(
                wait_records.c.id == receipt["wait_id"],
            ).values(source_key="ci_check:repo:sha:unit", source_generation=0))
        await original_lock(conn, **kwargs)

    monkeypatch.setattr(listener_module, "lock_graph", subscribe_before_lock)
    assert await service.listener._retire_unused_retries(seq) == 0
    assert (await records(env, wait_source_events))[0]["dispatched_at"] is None


async def test_ci_retry_retirement_rolls_back_on_lease_loss(runtime_env, monkeypatch):
    env = runtime_env
    seq = await env.db.wait_source_commit(source_key="ci_check:repo:sha:unit", generation=0,
        revision="success", outcome_ref="check", payload={})
    service = runtime(env)
    assert await service.lease()
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_listener_cursors).values(last_seq=seq))
    original_lease = service.listener.locked_lease
    calls = 0

    async def lose_after_stamp(conn):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise LeaseLost("lost before retirement commit")
        return await original_lease(conn)

    monkeypatch.setattr(service.listener, "locked_lease", lose_after_stamp)
    with pytest.raises(LeaseLost):
        await service.listener._retire_unused_retries(seq)
    assert (await records(env, wait_source_events))[0]["dispatched_at"] is None


async def test_deadline_command_can_exceed_renewal_cadence(runtime_env):
    service = runtime(runtime_env)
    service.listener.renewal_seconds = 0.01
    async def slow(*args):
        await asyncio.sleep(0.03)
        return {'success': True}
    service.command = slow
    service.timers.heap = [(NOW, 'deadline', {
        'wait_id': 'w', 'deadline_type': 'due', 'generation': 1,
    })]
    assert await service.timers.tick() == 1
    assert not service.timers.heap


async def test_listener_two_daemons_share_one_renewable_lease(runtime_env):
    env = runtime_env
    now = [NOW]
    first = runtime(env, clock=lambda: now[0])
    second = runtime(env, clock=lambda: now[0])
    results = await asyncio.gather(first.lease(), second.lease())
    assert sorted(results) == [False, True]
    winner, loser = (first, second) if results[0] else (second, first)
    now[0] += 20
    assert await winner.lease()
    now[0] += 15  # Past the original lease; renewal still excludes the loser.
    assert not await loser.lease()
    now[0] += 31
    assert await loser.lease()
    async with env.db._engine.begin() as conn:
        with pytest.raises(LeaseLost):
            await winner.listener.locked_lease(conn)
    # An old owner's disconnect cannot release the replacement's lease.
    await winner.lease(release=True)
    assert not await winner.lease()


async def test_listener_replays_down_time_and_rolls_back_cursor_with_dispatch(runtime_env):
    env = runtime_env
    receipt = await register(env)
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    dispatcher = ObservingDispatcher(env.registry)
    dispatcher.fail = True
    first = runtime(env, dispatcher=dispatcher)
    with pytest.raises(RuntimeError, match="dispatch abort"):
        await first.tick()
    assert (await records(env))[0]["outcome"] == "pending"
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == 0
    await first.lease(release=True)
    dispatcher.fail = False
    restarted = runtime(env, dispatcher=dispatcher)
    assert await restarted.tick()
    assert (await records(env))[0]["id"] == receipt["wait_id"]
    assert (await records(env))[0]["outcome"] == "satisfied"
    cursor = (await records(env, wait_listener_cursors))[0]["last_seq"]
    assert cursor == max(row["seq"] for row in await records(env, wait_source_events))
    calls = len(dispatcher.calls)
    assert await restarted.tick()
    assert len(dispatcher.calls) == calls


async def test_listener_missing_dispatcher_retains_matching_event(runtime_env):
    env = runtime_env
    await register(env)
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    service = runtime(env)
    with pytest.raises(ListenerUnavailable):
        await service.tick()
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == 0
    assert (await records(env))[0]["outcome"] == "pending"


async def test_listener_fenced_drain_handles_commit_inversion_and_rollback_gap(runtime_env):
    env = runtime_env
    service = runtime(env)
    assert await service.lease()
    async with env.db._engine.connect() as early:
        transaction = await early.begin()
        low = await record_source_event(early, source_key="task:old", generation=0,
                                       revision="low", outcome_ref="old", payload={})
        high = await env.db.wait_source_commit(source_key="task:new", generation=0,
                                              revision="high", outcome_ref="new", payload={})
        assert low < high
        started = asyncio.Event()
        original = env.db.wait_source_watermark

        async def fenced():
            started.set()
            return await original()

        env.db.wait_source_watermark = fenced
        drain = asyncio.create_task(service.tick())
        await started.wait()
        # Two independent real transactions; the fence cannot publish high yet.
        await asyncio.sleep(0.05)
        assert not drain.done()
        await transaction.commit()
        assert await drain
    with pytest.raises(RuntimeError):
        async with env.db._engine.begin() as conn:
            await record_source_event(conn, source_key="task:gap", generation=0,
                                      revision="rolled", outcome_ref="gap", payload={})
            raise RuntimeError("rollback")
    next_seq = await env.db.wait_source_commit(source_key="task:after", generation=0,
                                              revision="after", outcome_ref="after", payload={})
    assert await service.tick()
    rows = await records(env, wait_source_events)
    assert {row["seq"] for row in rows} == {low, high, next_seq}
    assert all(row["dispatched_at"] is not None for row in rows)
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == next_seq
    await env.db.purge_wait_source_events(now=NOW + 8 * 86400)
    assert not await records(env, wait_source_events)
    assert await service.tick()  # Cursor floor survives complete retention.
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == next_seq


async def test_listener_resource_bridge_pages_and_rechecks_owner(runtime_env):
    env = runtime_env
    observation = {"state": "unsatisfied", "revision": "1"}
    reader = AsyncMock(side_effect=lambda conn, cond: resource_snapshot(cond, **observation))
    registry = build_registry(resource_reader=reader)
    for name in ("one", "two", "three"):
        await register(env, {"kind": "resource", "blocker": "slot", "identity": name,
                             "source_revision": "1"}, registry=registry,
                       scope=Scope("p", "scheduler", "source", principal="service"),
                       idempotency_key=name)
    observation.update(state="satisfied", revision="opaque-new-cycle")
    seq = await env.db.wait_source_commit(**resource_invalidation(
        producer="pool", identity="profile", revision="cycle-B", outcome_ref="producer-hint",
        payload={},
    ))
    dispatcher = ObservingDispatcher(registry)
    service = runtime(env, registry=registry, dispatcher=dispatcher)
    service.listener.batch_size = 1
    assert await service.tick()
    assert len(dispatcher.calls) == 3
    assert all(row["outcome"] == "satisfied" and row["result_ref"] == "admission:current"
               for row in await records(env))
    assert all(event_seq == seq and evidence is not None
               for _, event_seq, evidence in dispatcher.calls)


@pytest.mark.parametrize("deadline_type", ["due", "escalation", "ceiling", "transition",
                                          "reservation"])
async def test_all_deadlines_fire_once_and_rebuild_after_restart(runtime_env, deadline_type):
    env = runtime_env
    receipt = await register(env, {"kind": "timer", "due_at": NOW + 10})
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_deadlines).where(
            wait_deadlines.c.wait_id == receipt["wait_id"],
        ).values(cancelled_at=NOW))
        await conn.execute(insert(wait_deadlines).values(
            id="runtime-deadline", wait_id=receipt["wait_id"], deadline_type=deadline_type,
            generation=2, due_at=NOW + 10,
        ))
    first = runtime(env)
    assert await first.lease()
    await first.lease(release=True)
    now = NOW + 11
    service = runtime(env, clock=lambda: now)
    effects = []

    async def effect(conn, deadline, wait, *, now, handler):
        effects.append((deadline["deadline_type"], deadline["generation"]))
        # A same-transaction write models the downstream owner's durable effect.
        await conn.execute(update(wait_records).where(wait_records.c.id == wait["id"]).values(
            decision_inputs={**wait["decision_inputs"], "deadline_effect": deadline_type},
        ))
        return {"success": True}

    service.orch.wait_deadline_handlers = {deadline_type: effect}
    assert await service.lease()
    await service.timers.rearm()
    assert await service.timers.tick() == 1
    assert await service.timers.tick() == 0
    stale = await service.command("wait_deadline_fire", {
        "wait_id": receipt["wait_id"], "deadline_type": deadline_type, "generation": 2,
    })
    assert stale["error_code"] == "wait.deadline_stale"
    assert len(effects) == (0 if deadline_type == "due" else 1)
    source_rows = await records(env, wait_source_events)
    assert len(source_rows) == (1 if deadline_type == "due" else 0)
    if source_rows:
        assert source_rows[0]["source_key"] == (await records(env))[0]["source_key"]
    assert (await records(env))[0]["outcome"] == "pending"  # Driver never wakes directly.
    assert (await records(env))[0]["decision_inputs"]["registration_receipt"] == receipt


async def test_deadline_missing_or_failed_effect_and_supersession_do_not_fire(runtime_env):
    env = runtime_env
    receipt = await register(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_deadlines).values(cancelled_at=NOW))
        await conn.execute(insert(wait_deadlines).values(
            id="runtime-deadline", wait_id=receipt["wait_id"], deadline_type="transition",
            generation=2, due_at=NOW,
        ))
    service = runtime(env)
    # This regression exercises an unavailable downstream owner. Production
    # composition now installs the hibernation transition owner by default.
    service.orch.wait_deadline_handlers.pop('transition')
    assert await service.lease()
    args = {"wait_id": receipt["wait_id"], "deadline_type": "transition", "generation": 2}
    assert (await service.command("wait_deadline_fire", args))["error_code"] == "wait.unavailable"

    async def abort(conn, deadline, wait, **kwargs):
        await conn.execute(update(wait_records).values(decision_reason="should-rollback"))
        raise RuntimeError("effect failed")

    service.orch.wait_deadline_handlers = {"transition": abort}
    result = await service.command("wait_deadline_fire", args)
    assert not result.get("success") and result["error"] == "effect failed"
    assert (await records(env))[0]["decision_reason"] != "should-rollback"
    assert next(row for row in await records(env, wait_deadlines)
                if row["id"] == "runtime-deadline")["fired_at"] is None
    async with env.db._engine.begin() as conn:
        await conn.execute(insert(wait_deadlines).values(
            id="newer-deadline", wait_id=receipt["wait_id"], deadline_type="transition",
            generation=3, due_at=NOW + 100,
        ))
    assert (await service.command("wait_deadline_fire", args))["error_code"] == "wait.deadline_stale"


@pytest.mark.parametrize("lost_owner,code", [("session", "wait.out_of_scope"),
                                            ("claim", "stale_claim")])
async def test_poisoned_deadline_is_quarantined_and_later_deadlines_fire_after_restart(
    runtime_env, lost_owner, code,
):
    from src.database.tables import events
    from tests.test_wait_queries import owner

    env = runtime_env
    orphan = await register(env, escalate_after_seconds=10)
    other = await owner(env.db, "later-owner", "later-session", "later-agent")
    later = await register(env, {"kind": "timer", "due_at": NOW + 11},
                           scope=other, idempotency_key="later")
    async with env.db._engine.begin() as conn:
        # A lost claim leaves the task nonterminal, so terminal cleanup alone
        # cannot fix it. The real escalation callback will reject its fence.
        if lost_owner == "session":
            from src.database.tables import sessions
            await conn.execute(update(sessions).where(sessions.c.id == "s").values(state="stopped"))
        else:
            await conn.execute(update(tasks).where(tasks.c.id == "owner").values(claim_epoch=2))
    service = runtime(env, clock=lambda: NOW + 12)
    assert await service.lease()
    await service.timers.rearm()
    poisoned = next(row for _, _, row in service.timers.heap
                    if row["wait_id"] == orphan["wait_id"] and row["deadline_type"] == "escalation")
    assert await service.timers.tick() == 1
    rows = await records(env, wait_deadlines)
    assert next(r for r in rows if r["id"] == poisoned["id"])["cancelled_at"] == NOW + 12
    assert next(r for r in rows if r["wait_id"] == later["wait_id"]
                and r["deadline_type"] == "due")["fired_at"] == NOW + 12
    audit = [r for r in await records(env, events)
             if r["event_type"] == "wait.deadline_quarantined"]
    assert len(audit) == 1 and code in audit[0]["payload"]
    notices = [r for r in await records(env, messages)
               if r["body_kind"] == "wait_deadline_quarantined"]
    assert len(notices) == 1 and notices[0]["to_id"] == "supervisor-p"
    assert next(r for r in await records(env) if r["id"] == orphan["wait_id"])["outcome"] == "pending"
    await service.lease(release=True)
    restarted = runtime(env, clock=lambda: NOW + 12)
    assert await restarted.lease()
    await restarted.timers.rearm()
    assert all(row["id"] != poisoned["id"] for _, _, row in restarted.timers.heap)
    # Replaying the internal cleanup cannot duplicate either durable record.
    await restarted.quarantine_deadline(poisoned, {"success": False,
                                                 "error_code": "wait.out_of_scope"})
    assert await restarted.timers.tick() == 0
    assert len([r for r in await records(env, events)
                if r["event_type"] == "wait.deadline_quarantined"]) == 1
    assert len([r for r in await records(env, messages)
                if r["body_kind"] == "wait_deadline_quarantined"]) == 1


@pytest.mark.parametrize("refusal", [
    {"success": False, "error_code": "wait.unavailable"},
    {"success": False, "error": "database temporarily unavailable"},
    {"success": False, "error_code": "command_runtime_error", "error": "connection lost"},
    {"success": False, "error_code": "command_runtime_error",
     "error": "deadline effect failed: not a refusal dictionary"},
    {"success": False, "error_code": "command_runtime_error",
     "error": "deadline effect failed: {'success': False, 'error_code': 'wait.unavailable'}"},
])
async def test_transient_deadline_refusal_is_not_quarantined(runtime_env, refusal):
    env = runtime_env
    await register(env, {"kind": "timer", "due_at": NOW + 10})
    service = runtime(env, clock=lambda: NOW + 11)
    assert await service.lease()
    await service.timers.rearm()
    service.command = AsyncMock(return_value=refusal)
    with pytest.raises(ListenerUnavailable):
        await service.timers.tick()
    assert service.timers.heap
    assert all(r["cancelled_at"] is None and r["fired_at"] is None
               for r in await records(env, wait_deadlines))
    assert not await records(env, messages)


@pytest.mark.parametrize("deadline_type,code,wrapped", [
    ("due", "wait.graph_busy", False),
    ("reservation", "wait.source_unresolved", False),
    ("reservation", "wait.source_unresolved", True),
    ("transition", "wait.effect_refused", False),
])
async def test_refused_deadline_retries_with_backoff_without_blocking_later_deadlines(
    runtime_env, deadline_type, code, wrapped, caplog,
):
    from tests.test_wait_queries import owner

    env = runtime_env
    first = await register(env, {"kind": "timer", "due_at": NOW + 10})
    later_scope = await owner(env.db, "later-owner", "later-session", "later-agent")
    later = await register(env, {"kind": "timer", "due_at": NOW + 11},
                           scope=later_scope, idempotency_key="later")
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_deadlines).where(
            wait_deadlines.c.wait_id == first["wait_id"], wait_deadlines.c.deadline_type == "due",
        ).values(deadline_type=deadline_type))
    now = [NOW + 12]
    service = runtime(env, clock=lambda: now[0])
    assert await service.lease()
    original = service.command
    busy = True

    async def effect(conn, deadline, wait, **kwargs):
        return {"success": True}

    if deadline_type != "due":
        service.orch.wait_deadline_handlers[deadline_type] = effect

    async def command(name, args):
        if busy and args["wait_id"] == first["wait_id"]:
            refusal = {"success": False, "error_code": code, "error": "retry"}
            if wrapped:
                return {"success": False, "error_code": "command_runtime_error",
                        "error": f"deadline effect failed: {refusal}"}
            return refusal
        return await original(name, args)

    service.command = AsyncMock(side_effect=command)
    for attempt, delay in enumerate((1, 2, 4, 8, 16, 30, 30)):
        assert await service.lease()
        await service.timers.rearm()
        assert await service.timers.tick() == (1 if attempt == 0 else 0)
        rows = await records(env, wait_deadlines)
        first_due = next(r for r in rows if r["wait_id"] == first["wait_id"]
                         and r["deadline_type"] == deadline_type)
        assert first_due["fired_at"] is None and first_due["cancelled_at"] is None
        assert next(r for r in rows if r["wait_id"] == later["wait_id"]
                    and r["deadline_type"] == "due")["fired_at"] == NOW + 12
        calls = service.command.await_count
        now[0] += delay - 0.01
        await service.timers.rearm()  # Notifications cannot bypass backoff.
        assert await service.timers.tick() == 0
        assert service.command.await_count == calls
        now[0] += 0.01
    busy = False
    assert await service.lease()
    await service.timers.rearm()
    assert await service.timers.tick() == 1
    assert await service.timers.tick() == 0
    assert not await records(env, messages)
    assert f"wait_id={first['wait_id']} deadline_type={deadline_type} generation=1" in caplog.text


@pytest.mark.parametrize("code,deadline_type", [
    ("wait.out_of_scope", "reservation"),
    ("wait.invalid", "reservation"),
    ("wait.condition_required", "reservation"),
    ("wait.not_escalated", "reservation"),
    ("wait.source_unresolved", "transition"),
    ("continuation.stale_generation", "reservation"),
    ("stale_claim", "reservation"),
])
async def test_structured_permanent_deadline_effect_is_quarantined_immediately(
    runtime_env, code, deadline_type,
):
    env = runtime_env
    receipt = await register(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_deadlines).values(cancelled_at=NOW))
        await conn.execute(insert(wait_deadlines).values(
            id="refused-effect", wait_id=receipt["wait_id"], deadline_type=deadline_type,
            generation=2, due_at=NOW,
        ))
    service = runtime(env)

    async def effect(conn, deadline, wait, **kwargs):
        await conn.execute(update(wait_records).values(decision_reason="must-rollback"))
        return {"success": False, "error_code": code, "error": "permanent refusal"}

    service.orch.wait_deadline_handlers[deadline_type] = effect
    assert await service.lease()
    await service.timers.rearm()
    assert await service.timers.tick() == 0
    deadline = next(r for r in await records(env, wait_deadlines) if r["id"] == "refused-effect")
    assert deadline["fired_at"] is None and deadline["cancelled_at"] == NOW
    assert (await records(env))[0]["decision_reason"] != "must-rollback"
    assert not service.timers._retry_deadlines
    notice = next(r for r in await records(env, messages)
                  if r["body_kind"] == "wait_deadline_quarantined")
    assert code in notice["body"]


@pytest.mark.parametrize("failure", ["timeout", "cancelled"])
async def test_interrupted_deadline_retries_with_backoff_and_continues_heap(failure, caplog):
    now = [NOW + 2]
    blocked = True

    async def command(name, args):
        assert name == "wait_deadline_fire"
        if blocked and args["wait_id"] == "slow-wait":
            if failure == "cancelled":
                raise asyncio.CancelledError
            await asyncio.sleep(60)
        return {"success": True}

    service = SimpleNamespace(listener=SimpleNamespace(lease_seconds=0.001),
                              command=AsyncMock(side_effect=command),
                              quarantine_deadline=AsyncMock())
    driver = WaitTimerDriver(service, clock=lambda: now[0])
    slow = {"id": "slow", "wait_id": "slow-wait", "deadline_type": "due", "generation": 3}
    later = {**slow, "id": "later", "wait_id": "later-wait"}
    driver.heap = [(NOW, "slow", slow), (NOW + 1, "later", later)]

    for attempt, delay in enumerate((1, 2, 4, 8, 16, 30, 30)):
        assert await driver.tick() == (1 if attempt == 0 else 0)
        assert driver._retry_deadlines["slow"] == (now[0] + delay, delay)
        calls = service.command.await_count
        now[0] += delay - 0.01
        assert await driver.tick() == 0
        assert service.command.await_count == calls
        now[0] += 0.01

    diagnostic = next(record.getMessage() for record in caplog.records
                      if record.getMessage().startswith(f"wait.deadline_{failure}"))
    assert "wait_id=slow-wait deadline_type=due generation=3 elapsed_seconds=" in diagnostic
    service.quarantine_deadline.assert_not_awaited()
    blocked = False
    assert await driver.tick() == 1
    assert not driver.heap and not driver._retry_deadlines


async def test_timer_run_survives_deadline_timeout_until_shutdown():
    later_fired = asyncio.Event()

    async def command(name, args):
        if args["wait_id"] == "slow-wait":
            await asyncio.sleep(60)
        later_fired.set()
        return {"success": True}

    listener = SimpleNamespace(lease_seconds=0.001, deadlines_changed=asyncio.Event())
    driver = WaitTimerDriver(SimpleNamespace(listener=listener, command=command),
                             clock=lambda: NOW + 2)
    driver.rearm = AsyncMock()
    driver.heap = [(NOW + n, str(n), {"wait_id": wait_id, "deadline_type": "due", "generation": 1})
                   for n, wait_id in enumerate(("slow-wait", "later-wait"))]
    running = asyncio.create_task(driver.run())
    try:
        await asyncio.wait_for(later_fired.wait(), 5)
        assert not running.done()
        assert "0" in driver._retry_deadlines
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running


@pytest.mark.parametrize("method", ["tick", "run"])
async def test_deadline_timer_shutdown_propagates_cancellation(method, caplog):
    started = asyncio.Event()

    async def command(name, args):
        started.set()
        await asyncio.sleep(60)

    listener = SimpleNamespace(lease_seconds=60, deadlines_changed=asyncio.Event())
    driver = WaitTimerDriver(SimpleNamespace(listener=listener, command=command), clock=lambda: NOW)
    driver.rearm = AsyncMock()
    driver.heap = [(NOW, "slow", {"wait_id": "slow-wait", "deadline_type": "due", "generation": 1})]
    running = asyncio.create_task(getattr(driver, method)())
    try:
        await asyncio.wait_for(started.wait(), 5)
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert not driver._retry_deadlines
    assert driver.heap
    assert "wait.deadline_timeout" not in caplog.text
    assert "wait.deadline_cancelled" not in caplog.text
    assert not driver._heap_lock.locked()


async def test_deadline_graph_lock_contention_retains_retry_after_transaction_rollback(runtime_env):
    from sqlalchemy import func
    from src.waits.graph import GRAPH_LOCK

    env = runtime_env
    receipt = await register(env, {"kind": "timer", "due_at": NOW + 10})
    now = [NOW + 11]
    service = runtime(env, clock=lambda: now[0])
    assert await service.lease()
    await service.timers.rearm()
    async with env.db._engine.begin() as holder:
        await holder.execute(select(func.pg_advisory_xact_lock(GRAPH_LOCK)))
        assert await service.timers.tick() == 0
    assert not await records(env, wait_source_events)
    assert all(r["fired_at"] is None and r["cancelled_at"] is None
               for r in await records(env, wait_deadlines))
    assert await service.timers.tick() == 0
    now[0] += 1
    assert await service.timers.tick() == 1
    assert len([r for r in await records(env, wait_source_events)
                if r["source_key"].startswith("timer:")]) == 1
    assert (await records(env))[0]["id"] == receipt["wait_id"]


@pytest.mark.parametrize("deadline_type,code", [
    ("transition", "wait.graph_busy"),
    ("reservation", "wait.source_unresolved"),
])
async def test_retryable_deadline_effect_rolls_back_and_preserves_code(
    runtime_env, deadline_type, code,
):
    env = runtime_env
    receipt = await register(env, {"kind": "timer", "due_at": NOW + 10})
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_deadlines).values(cancelled_at=NOW))
        await conn.execute(insert(wait_deadlines).values(
            id="transition", wait_id=receipt["wait_id"], deadline_type=deadline_type,
            generation=2, due_at=NOW + 10,
        ))
    now = [NOW + 11]
    service = runtime(env, clock=lambda: now[0])
    busy = True

    async def effect(conn, deadline, wait, **kwargs):
        await conn.execute(update(wait_records).values(decision_reason="effect"))
        return {"success": not busy, "error_code": code}

    service.orch.wait_deadline_handlers[deadline_type] = effect
    assert await service.lease()
    result = await service.command("wait_deadline_fire", {
        "wait_id": receipt["wait_id"], "deadline_type": deadline_type, "generation": 2,
    })
    assert result["error_code"] == code
    assert result["effect_refusal"] == {"success": False, "error_code": code}
    await service.timers.rearm()
    assert await service.timers.tick() == 0
    assert (await records(env))[0]["decision_reason"] != "effect"
    busy = False
    now[0] += 1
    assert await service.timers.tick() == 1
    assert (await records(env))[0]["decision_reason"] == "effect"


async def test_graph_busy_quarantine_retries_without_retiring_deadline(runtime_env):
    from src.waits.conditions import WaitError

    env = runtime_env
    await register(env, {"kind": "timer", "due_at": NOW + 10})
    now = [NOW + 11]
    service = runtime(env, clock=lambda: now[0])
    assert await service.lease()
    await service.timers.rearm()
    service.command = AsyncMock(return_value={"success": False, "error_code": "stale_claim"})
    quarantine = service.quarantine_deadline
    service.quarantine_deadline = AsyncMock(side_effect=WaitError("wait.graph_busy", "retry"))
    assert await service.timers.tick() == 0
    assert service.timers.heap
    assert all(r["cancelled_at"] is None for r in await records(env, wait_deadlines))
    service.quarantine_deadline = quarantine
    now[0] += 1
    assert await service.timers.tick() == 0
    assert all(row["deadline_type"] != "due" for _, _, row in service.timers.heap)
    due = next(r for r in await records(env, wait_deadlines) if r["deadline_type"] == "due")
    assert due["cancelled_at"] == now[0] and due["fired_at"] is None


@pytest.mark.parametrize("raised", [False, True])
async def test_graph_busy_dispatch_rolls_back_batch_and_retries(runtime_env, raised):
    from src.waits.conditions import WaitError

    env = runtime_env
    await register(env)
    dispatcher = ObservingDispatcher(env.registry)
    original = dispatcher.commit

    async def busy(conn, row, event, **kwargs):
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(title="rollback"))
        if raised:
            raise WaitError("wait.graph_busy", "retry")
        return {"success": False, "error_code": "wait.graph_busy", "error": "retry"}

    dispatcher.commit = busy
    service = runtime(env, dispatcher=dispatcher)
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await service.listener.tick()
    assert (await records(env))[0]["outcome"] == "pending"
    assert (await env.db.get_task("owner")).title != "rollback"
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == 0
    assert all(r["dispatched_at"] is None for r in await records(env, wait_source_events))
    dispatcher.commit = original
    assert await service.listener.tick()
    assert (await records(env))[0]["outcome"] == "satisfied"


async def test_old_terminal_owner_is_retired_before_deadline_effect(runtime_env):
    env = runtime_env
    await register(env, escalate_after_seconds=10)
    # Model an orphan left after the listener already consumed its task event.
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(
            status="COMPLETED", assigned_agent_id=None,
        ))
    service = runtime(env, clock=lambda: NOW + 11)
    assert await service.lease()
    await service.timers.rearm()
    assert await service.timers.tick() == 0
    wait = (await records(env))[0]
    assert wait["outcome"] == "cancelled" and wait["cleared_at"] == NOW + 11
    assert all(r["cancelled_at"] == NOW + 11 for r in await records(env, wait_deadlines))
    assert not await records(env, messages)


async def test_deadline_quarantine_requires_current_listener_lease(runtime_env):
    env = runtime_env
    await register(env, escalate_after_seconds=10)
    now = [NOW + 11]
    stale = runtime(env, clock=lambda: now[0])
    assert await stale.lease()
    await stale.timers.rearm()
    deadline = stale.timers.heap[0][2]
    now[0] += stale.listener.lease_seconds + 1
    current = runtime(env, clock=lambda: now[0])
    assert await current.lease()
    with pytest.raises(LeaseLost):
        await stale.quarantine_deadline(deadline, {"success": False,
                                                  "error_code": "wait.out_of_scope"})
    assert all(r["cancelled_at"] is None for r in await records(env, wait_deadlines))
    assert not await records(env, messages)


async def test_wait_runtime_is_dormant_and_service_bound(runtime_env):
    env = runtime_env
    service = runtime(env, enabled=False)
    service.listener.connect = AsyncMock(side_effect=AssertionError("disabled listener connected"))
    background = asyncio.create_task(service.run())
    await asyncio.sleep(0)
    assert not background.done()
    background.cancel()
    await asyncio.gather(background, return_exceptions=True)
    assert not await service.tick()
    assert not await records(env, wait_listener_cursors)
    assert (await service.command("wait_source_commit", {
        "source_key": "task:source", "generation": 0, "revision": "disabled",
        "outcome_ref": "test", "payload": {},
    }))["success"]
    with principal_context(ExecutionPrincipal.service("waits-listener")):
        assert await service.orch._command_handler.wait_listener_lease(service.listener)
    with pytest.raises(ValueError, match="service principal"):
        await service.orch._command_handler.wait_listener_drain(service.listener, 0)


async def test_live_listener_subscribes_reconnects_and_replays_while_disconnected(runtime_env):
    env = runtime_env
    await register(env)
    service = runtime(env, dispatcher=ObservingDispatcher(env.registry))
    connections = []
    reconnecting, allow_connect, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_connect, original_drain = service.listener._connect, service.drain

    async def connect():
        if connections:
            reconnecting.set()
            await allow_connect.wait()
        connection = await original_connect()
        connections.append(connection)
        return connection

    async def drain(watermark):
        # The real dedicated connection subscribed before reading this bound.
        channels = await connections[-1].fetch("SELECT pg_listening_channels() AS channel")
        assert {row["channel"] for row in channels} == {"aq_wait_source", "aq_wait_deadline"}
        result = await original_drain(watermark)
        drained.set()
        return result

    service.listener.connect, service.drain = connect, drain
    background = asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(drained.wait(), 10)
        drained.clear()
        await connections[0].close()
        await asyncio.wait_for(reconnecting.wait(), 10)
        drained.clear()
        # A commit with no subscribed connection must still be replayed.
        await env.db.transition_task("source", TaskStatus.COMPLETED)
        allow_connect.set()
        await asyncio.wait_for(drained.wait(), 10)
        assert (await records(env))[0]["outcome"] == "satisfied"
        assert len(connections) == 2
    finally:
        background.cancel()
        await asyncio.gather(background, return_exceptions=True)
    assert all(connection.is_closed() for connection in connections)
    assert (await records(env, wait_listener_cursors))[0]["lease_owner"] is None


async def test_listener_hint_requires_current_predicate_and_expired_batch_rolls_back(runtime_env):
    env = runtime_env
    await register(env)
    # A positive producer payload cannot substitute for an authoritative read.
    await env.db.wait_source_commit(source_key="task:source", generation=0, revision="hint",
                                   outcome_ref="untrusted", payload={"status": "COMPLETED"})
    now = [NOW]
    dispatcher = ObservingDispatcher(env.registry)
    service = runtime(env, clock=lambda: now[0], dispatcher=dispatcher)
    assert await service.tick()
    assert (await records(env))[0]["outcome"] == "pending"
    cursor = (await records(env, wait_listener_cursors))[0]["last_seq"]
    original = dispatcher.commit

    async def slow(conn, row, event, **kwargs):
        result = await original(conn, row, event, **kwargs)
        now[0] += 31
        return result

    dispatcher.commit = slow
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    with pytest.raises(LeaseLost):
        await service.tick()
    assert (await records(env))[0]["outcome"] == "pending"
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == cursor
    dispatcher.commit = original
    replacement = runtime(env, clock=lambda: now[0], dispatcher=dispatcher)
    assert await replacement.tick()
    assert (await records(env))[0]["outcome"] == "satisfied"


@pytest.mark.parametrize("replaced", [False, True])
async def test_resource_invalidation_checks_the_current_claim(runtime_env, replaced):
    env = runtime_env
    observation = {"state": "unsatisfied", "revision": "1"}
    reader = AsyncMock(side_effect=lambda conn, cond: resource_snapshot(cond, **observation))
    registry = build_registry(resource_reader=reader)
    await register(env, {"kind": "resource", "blocker": "slot", "identity": "opaque",
                         "source_revision": "1"}, registry=registry,
                   scope=Scope("p", "scheduler", "owner", principal="service"))
    async with env.db._engine.begin() as conn:
        # Model a service-authored resource subscription tied to an active
        # holder. Registry registration currently owns only scheduler scopes;
        # the listener bridge also supports canonical claim-bound subscriptions.
        row = (await conn.execute(select(wait_records))).mappings().one()
        condition = {**row["condition"], "_verified": {
            **row["condition"]["_verified"], "owner_kind": "task", "owner_id": "owner",
            "session_id": "s", "claim_epoch": 1,
        }}
        await conn.execute(update(wait_records).values(
            owner_kind="task", owner_id="owner", session_id="s", claim_epoch=1,
            condition=condition,
        ))
        if replaced:
            await conn.execute(update(tasks).where(tasks.c.id == "owner").values(claim_epoch=2))
    observation.update(state="satisfied", revision="opaque-fresh")
    await env.db.wait_source_commit(**resource_invalidation(
        producer="pool", identity="profile", revision="cycle-B", outcome_ref="hint", payload={},
    ))
    dispatcher = ObservingDispatcher(registry)
    service = runtime(env, registry=registry, dispatcher=dispatcher)
    assert await service.tick()
    assert len(dispatcher.calls) == (0 if replaced else 1)
    assert (await records(env))[0]["outcome"] == ("pending" if replaced else "satisfied")


async def test_deadline_insert_notifies_only_after_registration_commit(runtime_env):
    env = runtime_env
    service = runtime(env)
    connection = await service.listener._connect()
    received, notified = [], asyncio.Event()

    def receive(connection, pid, channel, payload):
        received.append(payload)
        if len(received) == 3:
            notified.set()

    await connection.add_listener("aq_wait_deadline", receive)
    original = env.registry.observe
    try:
        env.registry.observe = AsyncMock(side_effect=RuntimeError("post-subscribe abort"))
        with pytest.raises(RuntimeError, match="post-subscribe abort"):
            await register(env, {"kind": "timer", "due_at": NOW + 10})
        assert not received
        assert not await records(env, wait_deadlines)
        env.registry.observe = original
        await register(env, {"kind": "timer", "due_at": NOW + 10})
        await asyncio.wait_for(notified.wait(), 10)
        assert set(received) == {row["id"] for row in await records(env, wait_deadlines)}
    finally:
        env.registry.observe = original
        await connection.close()


async def test_busy_graph_yields_without_losing_lease_or_skipping_wake(runtime_env):
    from src.waits.graph import lock_graph

    env = runtime_env
    await register(env)
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    dispatcher = ObservingDispatcher(env.registry)
    service = runtime(env, dispatcher=dispatcher)
    assert await service.lease()
    watermark = await env.db.wait_source_watermark()
    service.listener.renewal_seconds = 0.05
    async with env.db._engine.begin() as blocker:
        await lock_graph(blocker)
        # A scheduler-held graph fence is normal contention, not a listener crash.
        assert await service.drain(watermark) == 0
        assert await service.lease()  # Drain did not retain the lease row lock.
        assert (await records(env, wait_listener_cursors))[0]["last_seq"] == 0
        assert (await records(env))[0]["outcome"] == "pending"
    service.listener.renewal_seconds = 10
    assert await service.tick()
    assert (await records(env))[0]["outcome"] == "satisfied"
    count = len(dispatcher.calls)
    assert count == 1
    assert await service.tick()
    assert len(dispatcher.calls) == count


async def test_deadline_waits_for_graph_without_blocking_lease_renewal(runtime_env):
    from sqlalchemy import event

    from src.waits.graph import lock_graph

    env = runtime_env
    await register(env, {"kind": "timer", "due_at": NOW + 10})
    service = runtime(env, clock=lambda: NOW + 11)
    assert await service.lease()
    await service.timers.rearm()
    pending = None
    waiting = asyncio.Event()

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if "advisory_xact_lock" in statement:
            waiting.set()

    try:
        async with env.db._engine.begin() as blocker:
            await lock_graph(blocker)
            event.listen(env.db._engine.sync_engine, "before_cursor_execute", before_execute)
            pending = asyncio.create_task(service.timers.tick())
            await asyncio.wait_for(waiting.wait(), 10)
            assert not pending.done()
            # A timer must not hold the lease row while waiting for the graph.
            assert await asyncio.wait_for(service.lease(), 1)
        assert await asyncio.wait_for(pending, 5) == 1
    finally:
        event.remove(env.db._engine.sync_engine, "before_cursor_execute", before_execute)
        if pending and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_listener_prepares_only_matching_waits_beyond_unrelated_prefix(runtime_env, monkeypatch):
    from src.waits import loops
    from src.waits.graph import graph_locked

    env = runtime_env
    receipt = await register(env)
    base = (await records(env))[0]
    async with env.db._engine.begin() as conn:
        await conn.execute(insert(wait_records), [
            {**base, "id": f"000-unrelated-{n:03}", "owner_id": f"unrelated-{n}",
             "source_key": "task:unrelated"}
            for n in range(205)
        ])
    prepared = []
    original = loops.prepare_progress

    async def prepare(conn, owners):
        assert not graph_locked(conn)
        prepared.extend(owner["id"] for owner in owners)
        await original(conn, owners)

    monkeypatch.setattr(loops, "prepare_progress", prepare)
    dispatcher = ObservingDispatcher(env.registry)
    service = runtime(env, dispatcher=dispatcher)
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await service.tick()
    assert prepared == [receipt["wait_id"]]
    assert dispatcher.calls[0][0] == receipt["wait_id"]
    assert next(row for row in await records(env) if row["id"] == receipt["wait_id"])[
        "outcome"
    ] == "satisfied"


async def test_listener_retries_preparation_if_cursor_advances_during_git(runtime_env, monkeypatch):
    from src.waits import loops

    env = runtime_env
    service = runtime(env)
    assert await service.lease()
    for n in range(2):
        await env.db.wait_source_commit(source_key="test:cursor-race", generation=0,
            revision=str(n), outcome_ref=f"test:{n}", payload={})
    source = sorted(await records(env, wait_source_events), key=lambda row: row["seq"])
    calls = 0

    async def prepare(conn, owners):
        nonlocal calls
        calls += 1
        if calls == 1:
            # A competing drain committed the first event while this drain
            # prepared Git. Its cursor must be re-read under the lease lock.
            async with env.db._engine.begin() as other:
                await other.execute(update(wait_source_events).where(
                    wait_source_events.c.seq == source[0]["seq"],
                ).values(dispatched_at=NOW))
                await other.execute(update(wait_listener_cursors).where(
                    wait_listener_cursors.c.listener_key == "source",
                ).values(last_seq=source[0]["seq"]))

    monkeypatch.setattr(loops, "prepare_progress", prepare)
    seen = []

    async def dispatch(conn, event):
        seen.append(event["seq"])

    service.listener._dispatch = dispatch
    assert await service.drain(await env.db.wait_source_watermark()) == 1
    assert calls == 2
    assert seen == [source[1]["seq"]]


async def test_slow_source_rolls_back_and_later_event_commits_before_retry(runtime_env, monkeypatch):
    """Cancellation inside a real query must release graph/cursor and retain the event."""
    from unittest.mock import Mock

    from sqlalchemy import event as sql_event, func

    env = runtime_env
    receipt = await register(env)
    slow_seq = await env.db.wait_source_commit(
        source_key="test:slow-source", generation=0, revision="1", outcome_ref="slow", payload={})
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    now = [NOW]
    dispatcher = ObservingDispatcher(env.registry)
    service = runtime(env, clock=lambda: now[0], dispatcher=dispatcher)
    original = service.listener._dispatch
    event_budget = None
    expired_budgets = []

    def controlled_timeout(seconds):
        nonlocal event_budget
        budget = asyncio.timeout(None)
        if seconds == service.listener.event_budget_seconds:
            event_budget = budget
        return budget

    # Control only the listener's clocks. Database and asyncio cancellation stay real;
    # ordinary queries cannot consume the event or drain budget under host load.
    listener_asyncio = Mock(wraps=asyncio)
    listener_asyncio.timeout.side_effect = controlled_timeout
    listener_asyncio.get_running_loop.return_value = SimpleNamespace(time=lambda: 0)
    monkeypatch.setattr("src.waits.listener.asyncio", listener_asyncio)

    def expire_on_query(conn, cursor, statement, parameters, context, executemany):
        if "pg_sleep" in statement:
            assert event_budget is not None
            expired_budgets.append(event_budget)
            event_budget.reschedule(asyncio.get_running_loop().time())

    async def stall(conn, event):
        if event["seq"] == slow_seq:
            await conn.execute(update(tasks).where(tasks.c.id == "owner").values(title="abort"))
            await conn.execute(select(func.pg_sleep(60)))
        return await original(conn, event)

    service.listener._dispatch = stall
    sql_event.listen(env.db._engine.sync_engine, "before_cursor_execute", expire_on_query)
    try:
        assert await service.listener.tick()
    finally:
        sql_event.remove(env.db._engine.sync_engine, "before_cursor_execute", expire_on_query)
    assert len(expired_budgets) == 1 and expired_budgets[0].expired()
    assert service.listener._retry_due[slow_seq] == NOW + service.listener.retry_seconds
    assert (await env.db.get_task("owner")).title != "abort"
    assert next(r for r in await records(env) if r["id"] == receipt["wait_id"])[
        "outcome"] == "satisfied"
    source = {r["seq"]: r for r in await records(env, wait_source_events)}
    assert source[slow_seq]["dispatched_at"] is None
    cursor = (await records(env, wait_listener_cursors))[0]["last_seq"]
    assert cursor > slow_seq
    service.listener._dispatch = original
    now[0] += service.listener.retry_seconds + 1
    assert await service.listener.tick()
    source = {r["seq"]: r for r in await records(env, wait_source_events)}
    assert source[slow_seq]["dispatched_at"] is not None
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == cursor


@pytest.mark.parametrize("kind", ["ci_check", "pool", "workspace", "config"])
async def test_contiguous_observations_coalesce_and_stop_before_lifecycle_event(runtime_env, kind):
    env = runtime_env
    key = "ci_check:repo:sha:unit"
    if kind == "ci_check":
        receipt = await register(env)
        async with env.db._engine.begin() as conn:
            # Exercise reobservation rather than irrelevant-source retirement.
            await conn.execute(update(wait_records).where(wait_records.c.id == receipt["wait_id"])
                               .values(source_key=key))
    else:
        reader = AsyncMock(side_effect=lambda conn, cond: resource_snapshot(
            cond, state="unsatisfied", revision="1"))
        await register(env, {"kind": "resource", "blocker": "slot", "identity": "opaque",
                             "source_revision": "1"}, registry=build_registry(resource_reader=reader),
                       scope=Scope("p", "scheduler", "source", principal="service"))
    for n in range(12):
        event = (dict(source_key=key, generation=0, revision=str(n),
                      outcome_ref=key, payload={}) if kind == "ci_check" else
                 resource_invalidation(producer=kind, identity=f"identity-{n}",
                                       revision=str(n), outcome_ref=kind, payload={}))
        await env.db.wait_source_commit(**event)
    barrier = await env.db.wait_source_commit(
        source_key="test:lifecycle", generation=0, revision="1", outcome_ref="barrier", payload={})
    service = runtime(env)
    seen = []

    async def observe(conn, event):
        seen.append(event["seq"])

    service.listener._dispatch = observe
    await service.listener.tick()
    assert seen == [barrier - 1, barrier]
    assert all(r["dispatched_at"] is not None for r in await records(env, wait_source_events))
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == barrier


async def test_resource_coalescing_preserves_project_and_provider_boundaries(runtime_env):
    env = runtime_env
    reader = AsyncMock(side_effect=lambda conn, cond: resource_snapshot(
        cond, state="unsatisfied", revision="1"))
    await register(env, {"kind": "resource", "blocker": "slot", "identity": "opaque",
                         "source_revision": "1"}, registry=build_registry(resource_reader=reader),
                   scope=Scope("p", "scheduler", "source", principal="service"))
    for producer, project in [("workspace", "p"), ("workspace", "q"),
                              ("provider", None), ("provider", None)]:
        await env.db.wait_source_commit(**resource_invalidation(
            producer=producer, identity="same", project_id=project,
            revision=str(len(await records(env, wait_source_events))),
            outcome_ref="resource", payload={}))
    service = runtime(env)
    dispatch = service.listener._dispatch = AsyncMock(return_value=0)
    await service.listener.tick()
    assert dispatch.await_count == 4


@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize("burst", [1, 40])
@pytest.mark.parametrize("rollback", [False, True])
async def test_pool_burst_reobserves_once_with_constant_graph_acquisitions(
    runtime_env, monkeypatch, retained, burst, rollback,
):
    from src.waits import listener as listener_module

    env = runtime_env
    reader = AsyncMock(side_effect=lambda conn, cond: resource_snapshot(
        cond, state="unsatisfied", revision="1"))
    await register(env, {"kind": "resource", "blocker": "slot", "identity": "opaque",
                         "source_revision": "1"}, registry=build_registry(resource_reader=reader),
                   scope=Scope("p", "scheduler", "source", principal="service"))
    hints = []
    for n in range(burst):
        hints.append(await env.db.wait_source_commit(**resource_invalidation(
            producer="pool", identity="worker", project_id="p", revision=str(n),
            outcome_ref="pool", payload={})))
        if n == 0:
            barrier = await env.db.wait_source_commit(source_key="test:lifecycle", generation=0,
                revision="1", outcome_ref="barrier", payload={})
    service = runtime(env)
    listener = service.listener
    assert await service.lease()
    watermark = await env.db.wait_source_watermark()
    if retained:
        async with env.db._engine.begin() as conn:
            await conn.execute(update(wait_listener_cursors).values(last_seq=watermark))
    dispatch = AsyncMock(return_value=0)
    monkeypatch.setattr(listener, "_dispatch", dispatch)
    locked = AsyncMock(wraps=listener_module.lock_graph)
    monkeypatch.setattr(listener_module, "lock_graph", locked)

    if rollback:
        async def fail(conn, event):
            assert event["seq"] == hints[-1]
            await conn.execute(update(tasks).where(tasks.c.id == "owner").values(title="abort"))
            raise RuntimeError("pool observation failed")

        monkeypatch.setattr(listener, "_dispatch", fail)
        with pytest.raises(RuntimeError, match="pool observation failed"):
            await listener._drain_one(watermark, True, {}, {"attempted": set()},
                                      admission_ready=True)
        assert (await env.db.get_task("owner")).title != "abort"
        assert all(r["dispatched_at"] is None for r in await records(env, wait_source_events))
        assert (await records(env, wait_listener_cursors))[0]["last_seq"] == (
            watermark if retained else 0)
        monkeypatch.setattr(listener, "_dispatch", dispatch)
        locked.reset_mock()

    # Lookahead may stamp later hints, but must not consume the lifecycle event.
    result = await listener._drain_one(watermark, True, {}, {"attempted": set()},
                                       admission_ready=True)
    assert set(result["processed"]) == set(hints)
    position = (await records(env, wait_listener_cursors))[0]["last_seq"]
    assert position == (watermark if retained else hints[0])
    sources = {r["seq"]: r for r in await records(env, wait_source_events)}
    assert sources[barrier]["dispatched_at"] is None
    assert all(sources[seq]["dispatched_at"] is not None for seq in hints)

    await listener.drain(watermark, admission_ready=True)
    assert [call.args[1]["seq"] for call in dispatch.await_args_list] == [hints[-1], barrier]
    assert locked.await_count <= 3
    assert all(r["dispatched_at"] is not None for r in await records(env, wait_source_events))
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == watermark


async def test_retained_pool_coalescing_preserves_project_and_provider_events(runtime_env):
    env = runtime_env
    reader = AsyncMock(side_effect=lambda conn, cond: resource_snapshot(
        cond, state="unsatisfied", revision="1"))
    await register(env, {"kind": "resource", "blocker": "slot", "identity": "opaque",
                         "source_revision": "1"}, registry=build_registry(resource_reader=reader),
                   scope=Scope("p", "scheduler", "source", principal="service"))
    expected = {}
    for n in range(4):
        for producer, project, identity in [("pool", "p", "worker"), ("pool", "q", "worker"),
                                             ("pool", "p", "other"), ("provider", None, "worker")]:
            seq = await env.db.wait_source_commit(**resource_invalidation(
                producer=producer, identity=identity, project_id=project, revision=f"{project}-{n}",
                outcome_ref="hint", payload={}))
            if producer == "pool":
                expected[project, identity] = seq
            else:
                expected[seq] = seq
    service = runtime(env)
    assert await service.lease()
    watermark = await env.db.wait_source_watermark()
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_listener_cursors).values(last_seq=watermark))
    dispatch = service.listener._dispatch = AsyncMock(return_value=0)
    await service.listener.drain(watermark, admission_ready=True)
    assert {call.args[1]["seq"] for call in dispatch.await_args_list} == set(expected.values())
    assert dispatch.await_count == len(expected)
    assert all(r["dispatched_at"] is not None for r in await records(env, wait_source_events))


async def test_startup_renews_lease_while_first_drain_is_blocked(runtime_env):
    service = runtime(runtime_env)
    entered, renewed = asyncio.Event(), asyncio.Event()
    service.listener.renewal_seconds = 0.01
    original_lease = service.lease

    async def lease(*, release=False):
        if entered.is_set() and not release:
            renewed.set()
        return await original_lease(release=release)

    async def drain(watermark):
        entered.set()
        await asyncio.Event().wait()

    service.lease, service.drain = lease, drain
    running = asyncio.create_task(service.listener.run())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(renewed.wait(), 5)
        assert not running.done()
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_listener_is_live_while_maintenance_and_observation_are_blocked(runtime_env):
    env = runtime_env
    await register(env)
    service = runtime(env, dispatcher=ObservingDispatcher(env.registry))
    entered, drained = asyncio.Event(), asyncio.Event()

    async def stall():
        entered.set()
        await asyncio.Event().wait()

    original = service.drain

    async def drain(watermark):
        result = await original(watermark)
        drained.set()
        return result

    service.lifecycle.tick, service.observe_sources_once = stall, stall
    service.startup_bridge = stall
    service.drain = drain
    running = asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(drained.wait(), 5)
        drained.clear()
        await env.db.transition_task("source", TaskStatus.COMPLETED)
        await asyncio.wait_for(drained.wait(), 5)
        assert (await records(env))[0]["outcome"] == "satisfied"
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("reason", ["count", "age"])
async def test_lag_alert_includes_deferred_rows_and_deduplicates_after_restart(runtime_env, reason):
    from src.waits.graph import lock_graph

    env = runtime_env
    await register(env)
    service = runtime(env)
    service.orch.config.wait_policy.listener_lag_events = 1 if reason == "count" else 100
    for n in range(2):
        await env.db.wait_source_commit(source_key="test:lag", generation=0, revision=str(n),
                                       outcome_ref="test", payload={})
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_source_events).values(committed_at=NOW - 121
                                                            if reason == "age" else NOW))
    assert await service.lease()
    seq = max(r["seq"] for r in await records(env, wait_source_events))
    async with env.db._engine.begin() as conn:
        # A deferred event behind the cursor is still outstanding source work.
        await conn.execute(update(wait_listener_cursors).values(last_seq=seq))
    async with env.db._engine.begin() as blocker:
        await lock_graph(blocker)
        await service.orch._command_handler.wait_service_report_lag(service.listener)
    restarted = runtime(env)
    restarted.orch.config.wait_policy.listener_lag_events = service.orch.config.wait_policy.listener_lag_events
    await restarted.orch._command_handler.wait_service_report_lag(restarted.listener)
    notices = [r for r in await records(env, messages) if r["body_kind"] == "wait_listener_lag"]
    assert len(notices) == 1
    assert notices[0]["to_id"] == "supervisor-p"
    assert len(await records(env, wait_source_events)) == 2
    assert '"pending_events": 2' in notices[0]["body"]


@pytest.mark.parametrize("minutes", [15, 3])
async def test_sustained_lag_alerts_coalesce_advancing_oldest_per_project_and_bucket(
    runtime_env, minutes,
):
    env = runtime_env
    await register(env)
    await register(env, {"kind": "timer", "due_at": NOW + 9000},
                   scope=Scope("q", "scheduler", "foreign", principal="service"),
                   idempotency_key="foreign")
    # Start at a bucket boundary so the checks span exactly one configured interval.
    interval = minutes * 60
    start = NOW // interval * interval
    now = [start]

    def restart():
        service = runtime(env, clock=lambda: now[0])
        service.orch.config.wait_policy.listener_lag_events = 1
        service.orch.config.wait_policy.listener_lag_alert_minutes = minutes
        return service

    for n in range(18):
        await env.db.wait_source_commit(source_key="test:lag", generation=0, revision=str(n),
                                       outcome_ref="test", payload={})
    service = restart()
    source_rows = sorted(await records(env, wait_source_events), key=lambda r: r["seq"])
    for step in range(16):
        now[0] = start + step * interval / 16
        # Oldest sequence changes every check while at least two events remain.
        if step:
            async with env.db._engine.begin() as conn:
                await conn.execute(update(wait_source_events).where(
                    wait_source_events.c.seq == source_rows[step - 1]["seq"],
                ).values(dispatched_at=now[0]))
        if step == 8:
            service = restart()
        await service.orch._command_handler.wait_service_report_lag(service.listener)
        notices = [r for r in await records(env, messages) if r["body_kind"] == "wait_listener_lag"]
        assert sorted(r["to_id"] for r in notices) == ["supervisor-p", "supervisor-q"]
        assert len(await records(env, wait_source_events)) == len(source_rows)

    now[0] = start + interval
    await service.orch._command_handler.wait_service_report_lag(service.listener)
    notices = [r for r in await records(env, messages) if r["body_kind"] == "wait_listener_lag"]
    for project in ("p", "q"):
        assert sorted(r["created_at"] for r in notices if r["project_id"] == project) == [
            start, start + interval,
        ]
    assert len(await records(env, wait_source_events)) == len(source_rows)


@pytest.mark.parametrize("source_key", ["task:source", "ci_check:repo:sha:unit"])
async def test_manual_redispatch_already_dispatched_skips_busy_listener(
    runtime_env, monkeypatch, source_key,
):
    from src.commands.principal import TRUSTED_LOCAL
    from src.database.tables import events as audit_events

    env = runtime_env
    seq = await env.db.wait_source_commit(source_key=source_key, generation=0,
        revision="1", outcome_ref="test", payload={"project_id": "p"})
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_source_events).where(
            wait_source_events.c.seq == seq,
        ).values(dispatched_at=NOW))
    service = runtime(env)
    lease = AsyncMock(side_effect=AssertionError("already dispatched needs no lease"))
    dispatch = AsyncMock(side_effect=AssertionError("already dispatched needs no observation"))
    monkeypatch.setattr(service, "lease", lease)
    monkeypatch.setattr(service.listener, "_dispatch", dispatch)
    prepare = AsyncMock(side_effect=AssertionError("already dispatched needs no preparation"))
    monkeypatch.setattr("src.waits.loops.prepare_progress", prepare)
    service.listener.event_budget_seconds = 0
    async with service.listener._tick_lock:
        with principal_context(TRUSTED_LOCAL):
            result = await service.orch._command_handler.execute("wait_source_redispatch", {
                "project_id": "p", "source_seq": seq, "note": "already handled"})
    assert result == {"success": True, "result": {
        "source_seq": seq, "project_id": "p", "outcome": "already_dispatched",
        "dispatched_at": NOW, "redispatched": False, "retired_events": 0,
    }}
    lease.assert_not_awaited()
    dispatch.assert_not_awaited()
    prepare.assert_not_awaited()
    assert not await records(env, wait_listener_cursors)
    assert not [r for r in await records(env, audit_events)
                if r["event_type"] == "wait.source_redispatched"]
    assert not service.listener.changed.is_set()


@pytest.mark.parametrize("project_id,code", [
    ("q", "wait.out_of_scope"), ("missing", "wait.out_of_scope"),
    ("p", "wait.source_unresolved"),
])
async def test_manual_redispatch_fast_path_checks_source_scope(runtime_env, project_id, code):
    from src.commands.principal import TRUSTED_LOCAL

    env = runtime_env
    seq = await env.db.wait_source_commit(source_key="test:scoped", generation=0,
        revision="1", outcome_ref="test", payload={"project_id": "p"})
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_source_events).where(
            wait_source_events.c.seq == seq,
        ).values(dispatched_at=NOW))
    service = runtime(env)
    with principal_context(TRUSTED_LOCAL):
        result = await service.orch._command_handler.execute("wait_source_redispatch", {
            "project_id": project_id, "source_seq": seq + 1 if project_id == "p" else seq,
            "note": "scope check"})
    assert not result["success"] and result["error_code"] == code
    assert not await records(env, wait_listener_cursors)


async def test_manual_redispatch_rechecks_after_waiting_for_listener(runtime_env, monkeypatch):
    from src.commands.principal import TRUSTED_LOCAL

    env = runtime_env
    seq = await env.db.wait_source_commit(source_key="test:race", generation=0,
        revision="1", outcome_ref="test", payload={})
    service = runtime(env)
    original = service.listener._redispatch_event
    read = asyncio.Event()

    async def observed(conn, args):
        event = await original(conn, args)
        read.set()
        return event

    monkeypatch.setattr(service.listener, "_redispatch_event", observed)
    lease = AsyncMock(side_effect=AssertionError("completed source needs no lease"))
    monkeypatch.setattr(service, "lease", lease)
    async with service.listener._tick_lock:
        with principal_context(TRUSTED_LOCAL):
            request = asyncio.create_task(service.orch._command_handler.execute(
                "wait_source_redispatch", {
                    "project_id": "p", "source_seq": seq, "note": "concurrent dispatch"}))
        try:
            await read.wait()
            async with env.db._engine.begin() as conn:
                await conn.execute(update(wait_source_events).where(
                    wait_source_events.c.seq == seq,
                ).values(dispatched_at=NOW))
        except BaseException:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            raise
    result = await request
    assert result["success"], result
    assert result["result"]["outcome"] == "already_dispatched"
    lease.assert_not_awaited()


async def test_manual_source_redispatch_reobserves_without_moving_cursor(runtime_env):
    from src.commands.principal import TRUSTED_LOCAL
    from src.database.tables import events as audit_events

    env = runtime_env
    await register(env)
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    seq = next(r["seq"] for r in await records(env, wait_source_events)
               if r["source_key"] == "task:source")
    dispatcher = ObservingDispatcher(env.registry)
    service = runtime(env, dispatcher=dispatcher)
    args = {"project_id": "p", "source_seq": seq, "note": "recover undispatched completion"}
    with principal_context(TRUSTED_LOCAL):
        result = await service.orch._command_handler.execute("wait_source_redispatch", args)
    assert result["success"], result
    assert (await records(env))[0]["outcome"] == "satisfied"
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == 0
    assert len([r for r in await records(env, audit_events)
                if r["event_type"] == "wait.source_redispatched"]) == 1
    assert len(dispatcher.calls) == 1
    await service.listener.tick()
    assert len(dispatcher.calls) == 1


@pytest.mark.parametrize("producer", ["ci_check", "pool", "config", "workspace"])
async def test_manual_redispatch_drains_unused_history_but_preserves_foreign_subscriber(
    runtime_env, producer,
):
    from src.commands.principal import TRUSTED_LOCAL
    from src.database.tables import events as audit_events

    env = runtime_env
    receipt = await register(env)
    key = ("ci_check:repo:sha:unit" if producer == "ci_check"
           else f"resource_invalidation:{producer}:producer")
    for n in range(3):
        await env.db.wait_source_commit(source_key=key, generation=0,
            revision=str(n), outcome_ref=f"hint:{n}", payload={})
    seq = await env.db.wait_source_watermark()
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).where(
            wait_records.c.id == receipt["wait_id"],
        ).values(project_id="q", source_key=key, source_generation=0,
                 condition_kind="task" if producer == "ci_check" else "resource"))
    service = runtime(env)
    args = {"project_id": "p", "source_seq": seq, "note": "drain settled CI history"}
    with principal_context(TRUSTED_LOCAL):
        result = await service.orch._command_handler.execute("wait_source_redispatch", args)
    assert result["success"], result
    assert result["result"]["retired_events"] == 0
    assert all(r["dispatched_at"] is None for r in await records(env, wait_source_events))
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).where(
            wait_records.c.id == receipt["wait_id"],
        ).values(outcome="satisfied"))
    with principal_context(TRUSTED_LOCAL):
        result = await service.orch._command_handler.execute("wait_source_redispatch", args)
    assert result["success"], result
    assert result["result"]["retired_events"] == 3
    assert all(r["dispatched_at"] is not None for r in await records(env, wait_source_events))
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == 0
    import json
    audits = [json.loads(r["payload"]) for r in await records(env, audit_events)
              if r["event_type"] == "wait.source_redispatched"]
    assert sorted(a["retired_events"] for a in audits) == [0, 3]


async def test_manual_global_invalidation_filters_other_project_subscriptions(runtime_env):
    from src.commands.principal import TRUSTED_LOCAL

    env = runtime_env
    observation = {"state": "unsatisfied", "revision": "1"}
    reader = AsyncMock(side_effect=lambda conn, cond: resource_snapshot(cond, **observation))
    registry = build_registry(resource_reader=reader)
    receipt = await register(env, {"kind": "resource", "blocker": "slot", "identity": "opaque",
                                   "source_revision": "1"}, registry=registry,
                             scope=Scope("p", "scheduler", "owner", principal="service"))
    base = (await records(env))[0]
    async with env.db._engine.begin() as conn:
        await conn.execute(insert(wait_records).values(
            **{**base, "id": "foreign-resource", "project_id": "q", "owner_id": "foreign",
               "condition": {**base["condition"], "_verified": {
                   **base["condition"]["_verified"], "project_id": "q", "owner_id": "foreign"}}}))
    seq = await env.db.wait_source_commit(**resource_invalidation(
        producer="pool", identity="profile", revision="cycle", outcome_ref="pool", payload={}))
    observation.update(state="satisfied", revision="2")
    dispatcher = ObservingDispatcher(registry)
    service = runtime(env, registry=registry, dispatcher=dispatcher)
    with principal_context(TRUSTED_LOCAL):
        result = await service.orch._command_handler.execute("wait_source_redispatch", {
            "source_seq": seq, "project_id": "p", "note": "recover resource"})
    assert result["success"], result
    assert [call[0] for call in dispatcher.calls] == [receipt["wait_id"]]
    rows = {r["id"]: r for r in await records(env)}
    assert rows[receipt["wait_id"]]["outcome"] == "satisfied"
    assert rows["foreign-resource"]["outcome"] == "pending"
    assert next(r for r in await records(env, wait_source_events) if r["seq"] == seq)[
        "dispatched_at"] is None


async def test_drain_yields_between_attempts_and_requests_fresh_watermark(runtime_env, monkeypatch):
    service = runtime(runtime_env)
    listener = service.listener
    listener.drain_budget_seconds = 0.01
    assert await service.lease()
    for n in range(3):
        await runtime_env.db.wait_source_commit(source_key="test:bounded-drain", generation=0,
            revision=str(n), outcome_ref=f"test:{n}", payload={})
    calls = []

    async def empty_retirement(watermark):
        # Empty housekeeping must not starve the first source attempt even
        # when database contention consumes the pass budget before it.
        await asyncio.sleep(0.02)
        return 0

    async def dispatch(conn, event):
        calls.append(event["seq"])
        await asyncio.sleep(0.02)
        return 0

    monkeypatch.setattr(listener, "_retire_unused_retries", empty_retirement)
    monkeypatch.setattr(listener, "_dispatch", dispatch)
    watermark = await runtime_env.db.wait_source_watermark()
    assert await service.drain(watermark) == 1
    assert listener.changed.is_set()
    assert len(calls) == 1
    assert (await records(runtime_env, wait_listener_cursors))[0]["last_seq"] == calls[0]
    assert sum(row["dispatched_at"] is None
               for row in await records(runtime_env, wait_source_events)) == 2


@pytest.mark.parametrize("failure", ["budget", "observation_timeout"])
async def test_manual_redispatch_budget_rolls_back_without_cursor_or_audit(
    runtime_env, monkeypatch, failure,
):
    from unittest.mock import Mock
    from src.commands.principal import TRUSTED_LOCAL
    from src.database.tables import events as audit_events

    env = runtime_env
    service = runtime(env)
    # Expire the budget at dispatch, after real database setup, so this
    # regression does not measure how busy the shared host happens to be.
    budget = asyncio.timeout(None)
    listener_asyncio = Mock(wraps=asyncio)
    listener_asyncio.timeout.return_value = budget
    monkeypatch.setattr("src.waits.listener.asyncio", listener_asyncio)
    seq = await env.db.wait_source_commit(source_key="test:manual-timeout", generation=0,
        revision="1", outcome_ref="test", payload={})

    async def stalled(conn, event, **kwargs):
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(title="abort"))
        if failure == "observation_timeout":
            raise TimeoutError("remote observation timed out")
        budget.reschedule(asyncio.get_running_loop().time())
        await asyncio.Event().wait()

    monkeypatch.setattr(service.listener, "_dispatch", stalled)
    with principal_context(TRUSTED_LOCAL):
        result = await service.orch._command_handler.execute("wait_source_redispatch", {
            "source_seq": seq, "project_id": "p", "note": "recover slow source"})
    assert not result["success"] and result["error_code"] == "wait.unavailable"
    assert ("exceeded its work budget" in result["error"]) == (failure == "budget")
    assert result["source_seq"] == seq
    assert result["phase"] == "dispatch"
    assert result["dispatch_attempts"] == 1
    assert result["budget_seconds"] == service.listener.event_budget_seconds
    assert result["elapsed_seconds"] >= 0
    assert (await env.db.get_task("owner")).title != "abort"
    assert next(r for r in await records(env, wait_source_events) if r["seq"] == seq)[
        "dispatched_at"] is None
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == 0
    assert not [r for r in await records(env, audit_events)
                if r["event_type"] == "wait.source_redispatched"]


async def ci_listener(env, monkeypatch, *, now=None):
    from src.waits.graph import graph_locked
    from src.waits.watchers.ci import CheckWatcher
    from tests.test_wait_queries import SHA, ci_snapshot, seed_batch, seed_repo

    await seed_repo(env)
    await seed_batch(env)
    state = {"value": "unsatisfied"}
    reader = AsyncMock(side_effect=lambda conn, cond: ci_snapshot(cond, state=state["value"]))
    registry = build_registry(ci_reader=reader)
    receipt = await register(env, {"kind": "ci_check", "repository_id": "repo", "head_sha": SHA,
                                   "check_name": "tests"}, registry=registry)
    service = runtime(env, registry=registry, clock=lambda: now[0] if now else NOW)
    service.orch.wait_dispatcher = service._dispatcher
    watcher = CheckWatcher(env.db, None, None, checks=None, repository=None)
    service.orch.wait_ci_lock_source = watcher.lock_source
    reader.reset_mock()

    async def observe(conn, cond):
        assert not graph_locked(conn), "CI observation must precede GRAPH_LOCK"
        # Another transaction can acquire the fence while the remote read runs.
        from src.waits.graph import lock_graph
        async with env.db._engine.begin() as other:
            await lock_graph(other, queued=True, timeout=0.1)
        return ci_snapshot(cond, state=state["value"])

    reader.side_effect = observe
    return service, receipt, reader, state, f"ci_check:repo:{SHA}:tests"


@pytest.mark.parametrize("conflict", [DeadlockDetectedError, SerializationError])
async def test_ci_source_conflict_rolls_back_and_reobserves_without_restart(
    runtime_env, monkeypatch, caplog, conflict,
):
    env = runtime_env
    now = [NOW]
    service, receipt, reader, state, key = await ci_listener(env, monkeypatch, now=now)
    state["value"] = "satisfied"
    seq = await env.db.wait_source_commit(source_key=key, generation=0, revision="conflict",
        outcome_ref="hint", payload={"conclusion": "success"})
    later = await env.db.wait_source_commit(source_key="test:later", generation=0,
        revision="1", outcome_ref="later", payload={})
    lock_source = service.orch.wait_ci_lock_source
    attempts = 0

    async def conflict_once(conn, cond):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await conn.execute(update(tasks).where(tasks.c.id == "owner").values(title="abort"))
            raise DBAPIError("source fence", {}, conflict("retry transaction"), False)
        await lock_source(conn, cond)

    service.orch.wait_ci_lock_source = conflict_once
    drained = asyncio.Event()
    original_drain = service.drain

    async def drain(watermark):
        result = await original_drain(watermark)
        drained.set()
        return result

    service.drain = drain
    connect = AsyncMock(side_effect=service.listener._connect)
    service.listener.connect = connect
    background = asyncio.create_task(service.listener.run())
    try:
        await asyncio.wait_for(drained.wait(), 15)
        assert (await env.db.get_task("owner")).title != "abort"
        wait = next(r for r in await records(env) if r["id"] == receipt["wait_id"])
        assert wait["outcome"] == "pending" and wait["source_watermark_seq"] < seq
        sources = {r["seq"]: r for r in await records(env, wait_source_events)}
        assert sources[seq]["dispatched_at"] is None
        assert sources[later]["dispatched_at"] is not None
        assert service.listener._retry_due[seq] == NOW + service.listener.retry_seconds
        cursor = (await records(env, wait_listener_cursors))[0]["last_seq"]
        assert cursor >= later
        assert reader.await_count == attempts == 1
        drained.clear()
        now[0] += service.listener.retry_seconds + 1
        service.listener.changed.set()
        await asyncio.wait_for(drained.wait(), 15)
        assert reader.await_count == attempts == 2
        assert next(r for r in await records(env) if r["id"] == receipt["wait_id"])[
            "outcome"] == "satisfied"
        assert next(r for r in await records(env, wait_source_events) if r["seq"] == seq)[
            "dispatched_at"] is not None
        assert (await records(env, wait_listener_cursors))[0]["last_seq"] == cursor
        assert connect.await_count == 1
        assert conflict.sqlstate in caplog.text
        assert "wait.source_dispatch_deferred" in caplog.text
        assert "wait.listener_restarted" not in caplog.text
    finally:
        background.cancel()
        await asyncio.gather(background, return_exceptions=True)


async def test_nontransient_source_database_error_propagates(runtime_env, monkeypatch):
    service, _, _, state, key = await ci_listener(runtime_env, monkeypatch)
    state["value"] = "satisfied"
    await runtime_env.db.wait_source_commit(source_key=key, generation=0, revision="bad",
        outcome_ref="hint", payload={})
    service.orch.wait_ci_lock_source = AsyncMock(side_effect=DBAPIError(
        "source fence", {}, UniqueViolationError("not retryable"), False))
    with pytest.raises(DBAPIError):
        await service.listener.tick()
    assert (await records(runtime_env, wait_listener_cursors))[0]["last_seq"] == 0
    assert not service.listener._retry_due


@pytest.mark.parametrize("interleaved", [False, True])
async def test_ci_progress_coalesces_once_unlocked_and_terminal_resolves(
    runtime_env, monkeypatch, interleaved,
):
    env = runtime_env
    service, receipt, reader, state, key = await ci_listener(env, monkeypatch)
    dispatched = []
    original = service.listener._dispatch

    async def dispatch(conn, event, **kwargs):
        dispatched.append(event["source_key"])
        return await original(conn, event, **kwargs)

    monkeypatch.setattr(service.listener, "_dispatch", dispatch)
    for n in range(12):
        await env.db.wait_source_commit(source_key=key, generation=0, revision=str(n),
            outcome_ref="hint", payload={"conclusion": "pending"})
        if interleaved:
            await env.db.wait_source_commit(source_key=f"test:other:{n}", generation=0,
                revision="1", outcome_ref="other", payload={})
    assert await service.listener.tick()
    assert reader.await_count == 1
    assert dispatched.count(key) == 1
    assert len(dispatched) == (13 if interleaved else 1)
    assert (await records(env))[0]["outcome"] == "pending"
    assert all(r["dispatched_at"] is not None for r in await records(env, wait_source_events))
    state["value"] = "satisfied"
    await env.db.wait_source_commit(source_key=key, generation=0, revision="terminal",
        outcome_ref="hint", payload={"conclusion": "success"})
    assert await service.listener.tick()
    wait = next(r for r in await records(env) if r["id"] == receipt["wait_id"])
    assert wait["outcome"] == "satisfied" and wait["result_ref"] == "ci:result"
    assert reader.await_count == 2


async def test_ci_progress_stream_keeps_only_latest_retry_and_terminal_bypasses_backoff(
    runtime_env, monkeypatch,
):
    env = runtime_env
    now = [NOW]
    service, receipt, reader, state, key = await ci_listener(env, monkeypatch, now=now)
    for n in range(6):
        await env.db.wait_source_commit(source_key=key, generation=0, revision=str(n),
            outcome_ref="hint", payload={"conclusion": "pending"})
        other = await env.db.wait_source_commit(source_key=f"test:other:{n}", generation=0,
            revision="1", outcome_ref="other", payload={})
        assert await service.listener.tick()
        sources = await records(env, wait_source_events)
        assert next(r for r in sources if r["seq"] == other)["dispatched_at"] is not None
        pending = [r for r in sources if r["dispatched_at"] is None]
        assert len(pending) == (0 if n == 0 else 1)
    assert reader.await_count == 1
    # A terminal hint bypasses progress throttling and supersedes its retained hint.
    state["value"] = "satisfied"
    await env.db.wait_source_commit(source_key=key, generation=0, revision="terminal",
        outcome_ref="hint", payload={"conclusion": "failure"})
    await service.listener.tick()
    assert reader.await_count == 2
    assert next(r for r in await records(env) if r["id"] == receipt["wait_id"])[
        "outcome"] == "satisfied"
    assert all(r["dispatched_at"] is not None for r in await records(env, wait_source_events)
               if r["source_key"] == key)


@pytest.mark.parametrize("restart", [False, True])
async def test_aged_ci_retry_self_heals_while_forward_backlog_remains(
    runtime_env, monkeypatch, restart,
):
    env = runtime_env
    now = [NOW]
    service, receipt, reader, state, key = await ci_listener(env, monkeypatch, now=now)
    await env.db.wait_source_commit(source_key=key, generation=0, revision="initial",
        outcome_ref="hint", payload={"conclusion": "pending"})
    await service.listener.tick()
    now[0] += 1
    retained = await env.db.wait_source_commit(source_key=key, generation=0, revision="progress",
        outcome_ref="hint", payload={"conclusion": "pending"})
    await service.listener.tick()
    assert reader.await_count == 1
    assert next(r for r in await records(env, wait_source_events)
                if r["seq"] == retained)["dispatched_at"] is None
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_source_events).where(
            wait_source_events.c.seq == retained,
        ).values(committed_at=NOW))
    if restart:
        await service.lease(release=True)
        lock_source = service.orch.wait_ci_lock_source
        service = runtime(env, registry=service.registry, clock=lambda: now[0])
        service.orch.wait_dispatcher = service._dispatcher
        service.orch.wait_ci_lock_source = lock_source
    # Every pass has forward work and room for exactly one source attempt.
    # Repeated forward-first passes used to starve this retained CI hint forever.
    service.listener.drain_budget_seconds = 0
    now[0] += 121
    state["value"] = "satisfied"
    later = await env.db.wait_source_commit(source_key="test:later", generation=0,
        revision="1", outcome_ref="later", payload={})
    cursor = (await records(env, wait_listener_cursors))[0]["last_seq"]
    await service.listener.tick()
    assert reader.await_count == 2
    assert next(r for r in await records(env) if r["id"] == receipt["wait_id"])[
        "outcome"] == "satisfied"
    sources = {r["seq"]: r for r in await records(env, wait_source_events)}
    assert sources[retained]["dispatched_at"] is not None
    assert sources[later]["dispatched_at"] is None
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == cursor
    await service.listener.tick()
    assert next(r for r in await records(env, wait_source_events)
                if r["seq"] == later)["dispatched_at"] is not None


@pytest.mark.parametrize("change", ["repository", "check_cache", "condition", "owner", "subscription"])
async def test_ci_preparation_race_never_applies_stale_observation(runtime_env, monkeypatch, change):
    from src.database.tables import repos
    from src.waits import listener as listener_module

    env = runtime_env
    now = [NOW]
    service, receipt, reader, state, key = await ci_listener(env, monkeypatch, now=now)
    state["value"] = "satisfied"
    await env.db.wait_source_commit(source_key=key, generation=0, revision="terminal",
        outcome_ref="hint", payload={"conclusion": "success"})
    if change == "subscription":
        async with env.db._engine.begin() as conn:
            await conn.execute(update(wait_records).values(outcome="cancelled"))
    original_lock = listener_module.lock_graph
    changed = False

    async def mutate_before_apply(conn, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            async with env.db._engine.begin() as other:
                await original_lock(other)
                if change == "repository":
                    await other.execute(update(repos).values(url="https://example/new-binding"))
                elif change == "check_cache":
                    checks = ExactChecks(env.db, AsyncMock(required=REQUIRED,
                        observe=AsyncMock(return_value=[row("unit", "success", observed_at=10)])),
                        clock=lambda: 10)
                    await checks.refresh(head())
                elif change == "condition":
                    saved = (await records(env))[0]
                    cond = {**saved["condition"], "required_check_set": "changed"}
                    await other.execute(update(wait_records).values(condition=cond))
                elif change == "subscription":
                    await other.execute(update(wait_records).values(outcome="pending"))
                else:
                    await other.execute(update(tasks).where(tasks.c.id == "owner").values(
                        claim_epoch=2))
        await original_lock(conn, **kwargs)

    monkeypatch.setattr(listener_module, "lock_graph", mutate_before_apply)
    await service.listener.tick()
    assert reader.await_count == (0 if change == "subscription" else 1)
    assert next(r for r in await records(env) if r["id"] == receipt["wait_id"])[
        "outcome"] == "pending"
    if change in {"repository", "check_cache", "subscription"}:
        now[0] += 2
        await service.listener.tick()
        assert reader.await_count == (1 if change == "subscription" else 2)
        assert (await records(env))[0]["outcome"] == "satisfied"


async def test_ci_failed_observation_keeps_latest_retry_and_drains_unrelated_source(
    runtime_env, monkeypatch,
):
    from src.waits.conditions import WaitError

    env = runtime_env
    service, receipt, reader, state, key = await ci_listener(env, monkeypatch)
    observe = reader.side_effect

    async def unavailable(conn, cond):
        await observe(conn, cond)
        raise WaitError("wait.unavailable", "provider temporarily unavailable", retry_after=30)

    reader.side_effect = unavailable
    for n in range(8):
        latest = await env.db.wait_source_commit(source_key=key, generation=0, revision=str(n),
            outcome_ref="hint", payload={"conclusion": "pending"})
        other = await env.db.wait_source_commit(source_key=f"test:unrelated:{n}", generation=0,
            revision="1", outcome_ref="other", payload={})
    await service.listener.tick()
    assert reader.await_count == 1
    sources = await records(env, wait_source_events)
    assert [r["seq"] for r in sources if r["dispatched_at"] is None] == [latest]
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == other
    # A fresh terminal event remains eligible during the old observation backoff.
    reader.side_effect = observe
    state["value"] = "satisfied"
    await env.db.wait_source_commit(source_key=key, generation=0, revision="terminal",
        outcome_ref="hint", payload={"conclusion": "cancelled"})
    await service.listener.tick()
    assert reader.await_count == 2
    assert next(r for r in await records(env) if r["id"] == receipt["wait_id"])[
        "outcome"] == "satisfied"


@pytest.fixture
async def large_listener_prefix(runtime_env):
    env = runtime_env
    await register(env)
    base = (await records(env))[0]
    async with env.db._engine.begin() as conn:
        await conn.execute(insert(wait_records), [
            {**base, "id": f"pending-{n}", "owner_id": f"observer-{n}",
             "idempotency_key": None, "source_key": f"ci_check:unrelated:{n}"}
            for n in range(2000)
        ])
    await env.db.wait_source_commit(source_key="ci_check:unused:0", generation=0,
        revision="1", outcome_ref="hint", payload={})
    source = (await records(env, wait_source_events))[0]
    async with env.db._engine.begin() as conn:
        await conn.execute(insert(wait_source_events), [
            {key: value for key, value in {**source, "id": f"source-{n}", "source_key": f"ci_check:unused:{n}"}.items()
             if key != "seq"}
            for n in range(1, 1200)
        ])
    service = runtime(env)
    assert await service.lease()
    return service.listener, await env.db.wait_source_watermark()


async def test_large_prefix_plans_unlocked_and_rechecks_bounded_subscriptions(
    large_listener_prefix, monkeypatch, caplog,
):
    from sqlalchemy import event
    from src.waits.graph import graph_locked
    from src.waits import loops

    listener, watermark = large_listener_prefix
    prefix = listener._unobserved_prefix
    phases = []
    locked_statements = []

    async def observe(conn, *args):
        phases.append(graph_locked(conn))
        if graph_locked(conn):
            def statement(conn, cursor, sql, parameters, context, executemany):
                locked_statements.append(sql)
            event.listen(conn.sync_connection, "before_cursor_execute", statement)
        return await prefix(conn, *args)

    monkeypatch.setattr(listener, "_unobserved_prefix", observe)
    monkeypatch.setattr(loops, "prepare_progress", AsyncMock(
        side_effect=AssertionError("irrelevant prefix must not prepare owner progress")))
    listener._dispatch = AsyncMock(side_effect=AssertionError("irrelevant prefix must not dispatch"))
    result = await listener._drain_one(watermark, True, {}, {"attempted": set()})
    assert len(result["processed"]) == listener.batch_size
    assert (await records(SimpleNamespace(db=listener.db), wait_listener_cursors))[0][
        "last_seq"] == result["processed"][-1] < watermark
    assert phases == [False, True]
    assert not any("EXISTS" in sql or "ORDER BY wait_source_events.seq" in sql
                   for sql in locked_statements)
    assert sum("SELECT wait_records.source_key" in sql for sql in locked_statements) == 1
    assert "wait.graph_lock_slow" not in caplog.text


@pytest.mark.perf
async def test_large_prefix_graph_hold_below_one_second(
    large_listener_prefix, perf_strict, monkeypatch,
):
    from src.waits import listener as listener_module

    listener, watermark = large_listener_prefix
    original_lock = listener_module.lock_graph
    acquired = []
    loop = asyncio.get_running_loop()

    async def timed_lock(conn, **kwargs):
        await original_lock(conn, **kwargs)
        acquired.append(loop.time())

    monkeypatch.setattr(listener_module, "lock_graph", timed_lock)
    result = await listener._drain_one(watermark, True, {}, {"attempted": set()})
    held = loop.time() - acquired[0]
    print(f"large prefix: events=1200 pending_waits=2001 retired={len(result['processed'])} "
          f"held_seconds={held:.6f}")
    assert len(acquired) == 1
    assert len(result["processed"]) == listener.batch_size
    assert held < 1


@pytest.mark.parametrize("subscribed_index", [0, 1])
@pytest.mark.parametrize("kind", ["ci_check", "message", "dependency", "task", "job", "batch"])
async def test_forward_prefix_rechecks_new_subscriber_before_advancing(
    runtime_env, monkeypatch, subscribed_index, kind,
):
    from src.waits import listener as listener_module

    env = runtime_env
    receipt = await register(env)
    seqs = [await env.db.wait_source_commit(source_key=f"{kind}:unused:{n}", generation=0,
                revision="1", outcome_ref="hint", payload={}) for n in range(3)]
    listener = runtime(env).listener
    assert await listener.service.lease()
    original_lock = listener_module.lock_graph

    async def subscribe_before_lock(conn, **kwargs):
        async with env.db._engine.begin() as other:
            await original_lock(other)
            await other.execute(update(wait_records).where(
                wait_records.c.id == receipt["wait_id"],
            ).values(source_key=f"{kind}:unused:{subscribed_index}", source_generation=0))
        await original_lock(conn, **kwargs)

    monkeypatch.setattr(listener_module, "lock_graph", subscribe_before_lock)
    listener._dispatch = AsyncMock(side_effect=AssertionError("must prepare new subscriber unlocked"))
    result = await listener._drain_one(seqs[-1], True, {}, {"attempted": set()})
    if subscribed_index == 0:
        assert result == {"needs_preparation": True}
    else:
        assert result["processed"] == [seqs[0]]
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == (
        0 if subscribed_index == 0 else seqs[0])
    assert [row["seq"] for row in await records(env, wait_source_events)
            if row["dispatched_at"] is not None] == seqs[:subscribed_index]


async def test_forward_prefix_preserves_cursor_race(runtime_env, monkeypatch):
    from src.waits import listener as listener_module

    env = runtime_env
    seq = await env.db.wait_source_commit(source_key="ci_check:unused:0", generation=0,
        revision="1", outcome_ref="hint", payload={})
    listener = runtime(env).listener
    assert await listener.service.lease()
    original_lock = listener_module.lock_graph

    async def advance_before_lock(conn, **kwargs):
        async with env.db._engine.begin() as other:
            await original_lock(other)
            await other.execute(update(wait_listener_cursors).values(last_seq=seq))
        await original_lock(conn, **kwargs)

    monkeypatch.setattr(listener_module, "lock_graph", advance_before_lock)
    assert await listener._drain_one(seq, True, {}, {"attempted": set()}) == {"raced": True}
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == seq
    assert (await records(env, wait_source_events))[0]["dispatched_at"] is None


@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
async def test_drain_hold_timing_includes_dispatch_and_transaction_end_on_failure(
    runtime_env, monkeypatch, caplog, failure,
):
    from sqlalchemy import event
    from src.waits import listener as listener_module

    env = runtime_env
    seq = await env.db.wait_source_commit(source_key="test:timing", generation=0,
        revision="1", outcome_ref="hint", payload={})
    listener = runtime(env).listener
    assert await listener.service.lease()
    elapsed = [0.0]
    monkeypatch.setattr(listener_module, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    original_lock = listener_module.lock_graph

    def transaction_end(conn):
        elapsed[0] += 0.3

    async def delayed_lock(conn, **kwargs):
        await original_lock(conn, **kwargs)
        elapsed[0] += 10  # Acquisition wait must not count as a hold.
        event.listen(conn.sync_connection, "commit", transaction_end)
        event.listen(conn.sync_connection, "rollback", transaction_end)

    async def dispatch(conn, source, **kwargs):
        elapsed[0] += 1.2
        if failure:
            raise failure()
        return 0

    monkeypatch.setattr(listener_module, "lock_graph", delayed_lock)
    monkeypatch.setattr(listener, "_dispatch", dispatch)
    if failure:
        with pytest.raises(failure):
            await listener._drain_one(seq, True, {}, {"attempted": set()})
    else:
        await listener._drain_one(seq, True, {}, {"attempted": set()})
    diagnostic = next(record.getMessage() for record in caplog.records
                      if "wait.source_lock_slow" in record.getMessage())
    assert "held_seconds=1.500" in diagnostic
    assert "dispatch_seconds=1.200" in diagnostic
    assert "transaction_end_seconds=0.300" in diagnostic
    assert all(f"{step}_seconds=0.000" in diagnostic for step in (
        "lease", "prefix_retirement", "admission_recheck", "outcome_writes"))
    assert ((await records(env, wait_source_events))[0]["dispatched_at"] is None) == bool(failure)
    assert (await records(env, wait_listener_cursors))[0]["last_seq"] == (0 if failure else seq)
