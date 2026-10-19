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

import asyncio

from src.commands import CommandHandler
from src.commands.principal import ExecutionPrincipal, principal_context
from src.config import AppConfig
from src.database.tables import wait_deadlines, wait_listener_cursors, wait_records
from src.waits.conditions import Condition, Scope
from src.waits.listener import LeaseLost, ListenerUnavailable
from src.waits.registry import build_registry
from src.waits.resource_sources import resource_invalidation
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
    config.wait_policy = SimpleNamespace(v2_enabled=enabled)
    orch = SimpleNamespace(db=env.db, config=config, bus=SimpleNamespace(emit=AsyncMock()),
                           plugin_registry=None, wait_registry=registry or env.registry,
                           wait_dispatcher=dispatcher)
    orch._command_handler = CommandHandler(orch, config)
    return WaitService(orch, clock=clock)


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


async def test_wait_runtime_is_dormant_and_service_bound(runtime_env):
    env = runtime_env
    service = runtime(env, enabled=False)
    service.listener.connect = AsyncMock(side_effect=AssertionError("disabled listener connected"))
    await service.run()
    assert not await service.tick()
    assert not await records(env, wait_listener_cursors)
    assert (await service.command("wait_source_commit", {
        "source_key": "task:source", "generation": 0, "revision": "disabled",
        "outcome_ref": "test", "payload": {},
    }))["error_code"] == "wait.disabled"
    with principal_context(ExecutionPrincipal.service("waits-listener")):
        with pytest.raises(ValueError, match="disabled"):
            await service.orch._command_handler.wait_listener_lease(service.listener)
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
