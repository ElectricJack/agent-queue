"""Verified registration and source races on disposable PostgreSQL."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import create_async_engine

from src.database import Database
from src.database.queries.wait_queries import REGISTRATION_LOCK, get_wait, register_wait
from src.database.queries.wait_source_queries import WaitSourceQueriesMixin
from src.database.tables import (
    agents,
    collaboration_members,
    collaboration_messages,
    collaboration_threads,
    doc_reviews,
    integration_batches,
    jobs,
    messages,
    playbook_artifacts,
    playbook_v2_runs,
    playbook_waits,
    repos,
    sessions,
    task_comments,
    task_dependencies,
    tasks,
    wait_deadlines,
    wait_edges,
    wait_listener_cursors,
    wait_records,
    wait_source_events,
)
from src.models import Agent, AgentState, Project, SessionRecord, Task, TaskStatus
from src.waits.conditions import (
    Condition,
    NodeKey,
    Observation,
    Scope,
    SleepDecision,
    Snapshot,
    WaitError,
)
from src.waits.registry import build_registry
from src.waits.sources import payload_hash, record_source_event
from tests.db_fixtures import lease_dsn

NOW = 1_800_000_000.0
SHA = "a" * 40
# Published wait-source allocation fence; registration must use a different key.
SOURCE_SEQUENCE_LOCK = 0x415157414954


class Policy:
    in_place_ceiling_seconds = 1800

    def escalation_seconds(self, kind):
        return 300 if kind == "timer" else 7200


def decide(cond, evidence):
    return SleepDecision("in_place", "test-policy", {"expected_seconds": 30})


async def owner(db, task_id="owner", session_id="s", agent_id="a"):
    await db.create_agent(
        Agent(id=agent_id, name=agent_id, profile_id="worker-codex", state=AgentState.BUSY)
    )
    await db.create_task(
        Task(
            id=task_id,
            project_id="p",
            title="Owner",
            description="",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id=agent_id,
            claim_epoch=1,
        )
    )
    await db.update_agent(agent_id, current_task_id=task_id)
    # create_task's insert doesn't carry a caller-authored claim epoch.
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == task_id).values(claim_epoch=1))
    await db.create_session(
        SessionRecord(
            id=session_id,
            project_id="p",
            profile_id="worker-codex",
            harness="codex",
            provider="fake",
            name=f"wait-{session_id}",
            lifecycle="pool",
            work_dir="/tmp/wait-v2",
            epoch="boot",
            instance_token="token",
            started_at=NOW,
            task_id=task_id,
            state="running",
            agent_id=agent_id,
            claim_phase="active",
            last_claim_epoch=1,
        )
    )
    return Scope("p", "task", task_id, session_id, "token", 1)


@pytest.fixture
async def env():
    db = Database(lease_dsn("verified-waits"))
    await db.initialize()
    await db.create_project(Project(id="p", name="P"))
    await db.create_project(Project(id="q", name="Q"))
    scope = await owner(db)
    await db.create_task(Task(id="source", project_id="p", title="Source", description=""))
    await db.create_task(Task(id="foreign", project_id="q", title="Private title", description=""))
    yield SimpleNamespace(db=db, scope=scope, registry=build_registry(clock=lambda: NOW))
    await db.close()


async def register(env, condition=None, **kwargs):
    options = dict(
        scope=env.scope,
        registry=env.registry,
        policy=Policy(),
        decide_sleep=decide,
        now=NOW,
        idempotency_key="key",
        source_watermark=0,
    )
    if "read_source_watermark" in kwargs:
        options.pop("source_watermark")
    options.update(kwargs)
    async with env.db._engine.begin() as conn:
        return await register_wait(
            conn, condition=condition or {"kind": "task", "task_id": "source"}, **options
        )


async def records(env, table=wait_records):
    async with env.db._engine.connect() as conn:
        return [dict(r) for r in (await conn.execute(select(table))).mappings()]


async def source_state(env, status):
    async with env.db._engine.begin() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == "source").values(status=status, updated_at=NOW + 1)
        )


@pytest.mark.parametrize(
    ("condition", "code"),
    [
        ({"kind": "task", "task_id": "absent"}, "wait.source_unresolved"),
        ({"kind": "task", "task_id": "foreign"}, "wait.out_of_scope"),
        ({"kind": "task", "task_id": "source", "any": []}, "wait.condition_required"),
        ({"kind": "task", "task_id": "source", "generation": 99}, "wait.source_unresolved"),
        ({"kind": "task", "task_id": "owner"}, "wait.cycle"),
        (
            {"kind": "resource", "blocker": "slot", "identity": "p:x", "source_revision": "1"},
            "wait.out_of_scope",
        ),
    ],
)
async def test_refusals_leave_no_wait_edges_or_deadlines(env, condition, code):
    with pytest.raises(WaitError) as error:
        await register(env, condition)
    assert error.value.code == code
    assert "Private title" not in str(error.value)
    for table in (wait_records, wait_edges, wait_deadlines):
        assert await records(env, table) == []


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "BLOCKED"])
async def test_initially_settled_refuses_with_result_without_writing(env, status):
    await source_state(env, status)
    with pytest.raises(WaitError) as error:
        await register(env)
    assert error.value.code == "wait.already_satisfied"
    assert error.value.details["result_ref"].startswith("task:source@")
    assert await records(env) == []


@pytest.mark.parametrize(
    "change",
    [
        {"claim_epoch": 0},
        {"claim_epoch": None},
        {"instance_token": "stale"},
        {"session_id": "missing"},
        {"owner_id": "source"},
    ],
)
async def test_live_owner_fence_precedes_all_inserts(env, change):
    with pytest.raises(WaitError) as error:
        await register(env, scope=replace(env.scope, **change))
    assert error.value.code in {"stale_claim", "wait.out_of_scope"}
    assert await records(env) == []


@pytest.mark.parametrize(
    ("table", "values"),
    [
        (sessions, {"claim_phase": "preparing"}),
        (sessions, {"state": "stopped"}),
        (sessions, {"desired_state": "stopped"}),
        (sessions, {"last_claim_epoch": 2}),
        (agents, {"current_task_id": None}),
    ],
)
async def test_inconsistent_holder_is_refused(env, table, values):
    async with env.db._engine.begin() as conn:
        await conn.execute(update(table).values(**values))
    with pytest.raises(WaitError):
        await register(env)
    assert await records(env) == []


async def test_receipt_is_stable_after_completion_and_policy_changes(env):
    receipt = await register(env, claimed="CI is actually irrelevant to a task predicate")
    assert receipt["next_step"] == "end_turn"
    assert receipt["ceiling_at"] == NOW + 1800
    assert receipt["escalate_at"] == NOW + 7200
    await source_state(env, "COMPLETED")
    again = await register(
        env,
        now=NOW + 50,
        decide_sleep=lambda *_: 1 / 0,
        claimed="CI is actually irrelevant to a task predicate",
    )
    assert again == receipt
    assert len(await records(env)) == 1
    with pytest.raises(WaitError) as error:
        await register(env, claimed="different request")
    assert error.value.code == "wait.idempotency_conflict"


async def test_claim_epoch_is_rechecked_before_idempotency_replay(env):
    await register(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(claim_epoch=2))
    with pytest.raises(WaitError) as error:
        await register(env)
    assert error.value.code == "stale_claim"


async def test_receipt_replays_after_command_transitions_holder_to_waiting(env):
    receipt = await register(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(status="WAITING"))
    assert await register(env) == receipt
    with pytest.raises(WaitError) as error:
        await register(env, idempotency_key="new")
    assert error.value.code == "wait.active_exists"


async def test_concurrent_retries_return_one_receipt(env):
    one, two = await asyncio.gather(register(env), register(env))
    assert one == two
    assert len(await records(env)) == 1


async def test_active_wait_refuses_a_second_key(env):
    receipt = await register(env)
    with pytest.raises(WaitError) as error:
        await register(env, idempotency_key="second")
    assert error.value.code == "wait.active_exists"
    assert error.value.details["wait_id"] == receipt["wait_id"]


async def test_subscription_race_commits_resolved_audit_and_cancels_edges(env):
    async def observe(conn, cond):
        return Observation("late", "satisfied", "completion:race", {"status": "FAILED"})

    env.registry.adapter("task").observe = observe
    receipt = await register(env)
    assert receipt["code"] == "wait.already_satisfied"
    assert receipt["result_ref"] == "completion:race"
    assert receipt["next_step"] == "continue"
    audit = (await records(env))[0]
    assert audit["outcome"] == "satisfied"
    assert audit["resolved_at"] == audit["created_at"] == NOW
    assert audit["version"] == 2 and not audit["blocking"]
    assert all(d["cancelled_at"] == NOW for d in await records(env, wait_deadlines))
    assert all(not e["active"] for e in await records(env, wait_edges))
    assert await register(env) == receipt


async def test_source_deleted_during_subscription_cannot_create_an_exemption(env):
    env.registry.adapter("task").observe = AsyncMock(
        return_value=Observation("gone", "unavailable")
    )
    receipt = await register(env)
    assert receipt["code"] == "wait.source_unresolved"
    assert receipt["next_step"] == "continue"
    assert (await records(env))[0]["outcome"] == "source_unavailable"


async def test_actual_source_commit_between_subscribe_and_observe_is_not_lost(env):
    observe = env.registry.adapter("task").observe

    async def complete_before_observe(conn, cond):
        async with env.db._engine.begin() as producer:
            await producer.execute(select(func.pg_advisory_xact_lock_shared(SOURCE_SEQUENCE_LOCK)))
            await producer.execute(
                update(tasks)
                .where(tasks.c.id == "source")
                .values(status="FAILED", updated_at=NOW + 1)
            )
        return await observe(conn, cond)

    env.registry.adapter("task").observe = complete_before_observe
    receipt = await asyncio.wait_for(register(env), timeout=10)
    assert receipt["code"] == "wait.already_satisfied"
    assert receipt["result_ref"].startswith("task:source@")
    assert (await records(env))[0]["outcome"] == "satisfied"


async def test_outer_rollback_rolls_back_wait_edges_and_deadlines(env):
    with pytest.raises(RuntimeError):
        async with env.db._engine.begin() as conn:
            await register_wait(
                conn,
                scope=env.scope,
                condition={"kind": "task", "task_id": "source"},
                registry=env.registry,
                policy=Policy(),
                decide_sleep=decide,
                idempotency_key="key",
                now=NOW,
                source_watermark=0,
            )
            raise RuntimeError("job submission or command rolled back")
    for table in (wait_records, wait_edges, wait_deadlines):
        assert not await records(env, table)


async def test_source_watermark_uses_fenced_bound_including_retention_floor(env):
    async with env.db._engine.begin() as conn:
        for i, key, gen in [(1, "task:source", 0), (2, "task:source", 1), (3, "task:other", 0)]:
            await conn.execute(
                insert(wait_source_events).values(
                    id=str(i),
                    source_key=key,
                    source_generation=gen,
                    source_revision=str(i),
                    occurred_at=NOW,
                    committed_at=NOW,
                    outcome_ref="ref",
                    payload_hash="hash",
                    payload={},
                )
            )
        expected = await conn.scalar(select(func.max(wait_source_events.c.seq))) + 100
    # A source cursor floor can exceed MAX after retention; never recompute it.
    await register(env, source_watermark=expected)
    assert (await records(env))[0]["source_watermark_seq"] == expected


@pytest.mark.parametrize("rollback_earlier", [False, True])
async def test_fence_precedes_registration_locks_and_covers_inverted_commits(env, rollback_earlier):
    contended, release_reader = asyncio.Event(), asyncio.Event()

    async def emit(conn, identity):
        await conn.execute(select(func.pg_advisory_xact_lock_shared(SOURCE_SEQUENCE_LOCK)))
        return await conn.scalar(
            insert(wait_source_events)
            .values(
                id=identity,
                source_key="task:source",
                source_generation=0,
                source_revision=identity,
                occurred_at=NOW,
                committed_at=NOW,
                outcome_ref="ref",
                payload_hash="hash",
                payload={},
            )
            .returning(wait_source_events.c.seq)
        )

    async def read_source_watermark():
        # Exercise the injected producer contract using independent real
        # transactions. The writer leaf owns the helper's polling and retention.
        async with env.db._engine.connect() as reader:
            await reader.execution_options(isolation_level="READ COMMITTED")
            async with reader.begin():
                assert not await reader.scalar(
                    select(func.pg_try_advisory_xact_lock(SOURCE_SEQUENCE_LOCK))
                )
                contended.set()
                await release_reader.wait()
                assert await reader.scalar(
                    select(func.pg_try_advisory_xact_lock(SOURCE_SEQUENCE_LOCK))
                )
                return await reader.scalar(select(func.max(wait_source_events.c.seq)))

    async with env.db._engine.connect() as earlier:
        transaction = await earlier.begin()
        low = await emit(earlier, "earlier")
        async with env.db._engine.begin() as later:
            high = await emit(later, "later")
        assert high > low
        pending = asyncio.create_task(register(env, read_source_watermark=read_source_watermark))
        try:
            await asyncio.wait_for(contended.wait(), timeout=10)
            async with env.db._engine.begin() as third:
                # No owner or graph lock may be held while the fence waits.
                await third.execute(
                    select(tasks.c.id).where(tasks.c.id == "owner").with_for_update(nowait=True)
                )
                assert await third.scalar(select(func.pg_try_advisory_xact_lock(REGISTRATION_LOCK)))
                assert await third.scalar(
                    select(func.pg_try_advisory_xact_lock_shared(SOURCE_SEQUENCE_LOCK))
                )
                last = await emit(third, "third")
            if rollback_earlier:
                await transaction.rollback()
            else:
                await transaction.commit()
            release_reader.set()
            assert (await asyncio.wait_for(pending, timeout=10))["success"]
        finally:
            release_reader.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    assert (await records(env))[0]["source_watermark_seq"] == last
    committed = {r["seq"] for r in await records(env, wait_source_events)}
    assert committed == ({high, last} if rollback_earlier else {low, high, last})


async def test_prefetched_bound_supports_a_caller_already_holding_source_and_owner_locks(env):
    prefetched = 42
    async with env.db._engine.begin() as conn:
        # The caller still follows graph -> owner/source ordering; only the
        # source sequence fence is already released on its separate connection.
        await conn.execute(select(func.pg_advisory_xact_lock(REGISTRATION_LOCK)))
        await conn.execute(select(tasks.c.id).where(tasks.c.id == "owner").with_for_update())
        await conn.execute(select(func.pg_advisory_xact_lock_shared(SOURCE_SEQUENCE_LOCK)))
        receipt = await asyncio.wait_for(
            register_wait(
                conn,
                scope=env.scope,
                condition={"kind": "task", "task_id": "source"},
                registry=env.registry,
                policy=Policy(),
                decide_sleep=decide,
                idempotency_key="prefetched",
                now=NOW,
                source_watermark=prefetched,
            ),
            timeout=10,
        )
    assert receipt["success"]
    assert (await records(env))[0]["source_watermark_seq"] == prefetched


async def test_existing_read_committed_transaction_sees_source_commit_during_fence(env):
    async def read_source_watermark():
        await source_state(env, "COMPLETED")
        return 0

    async with env.db._engine.begin() as conn:
        assert await conn.scalar(select(tasks.c.status).where(tasks.c.id == "source")) == "DEFINED"
        with pytest.raises(WaitError) as error:
            await register_wait(
                conn,
                scope=env.scope,
                condition={"kind": "task", "task_id": "source"},
                registry=env.registry,
                policy=Policy(),
                decide_sleep=decide,
                idempotency_key="existing-transaction",
                now=NOW,
                read_source_watermark=read_source_watermark,
            )
        assert error.value.code == "wait.already_satisfied"
    assert not await records(env)


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
async def test_stale_outer_snapshot_is_refused_before_acquiring_source_fence(env, isolation):
    reader = AsyncMock(return_value=0)
    async with env.db._engine.connect() as conn:
        await conn.execution_options(isolation_level=isolation)
        async with conn.begin():
            await conn.execute(select(tasks.c.id))  # establish a snapshot before registration
            with pytest.raises(ValueError, match="READ COMMITTED"):
                await register_wait(
                    conn,
                    scope=env.scope,
                    condition={"kind": "task", "task_id": "source"},
                    registry=env.registry,
                    policy=Policy(),
                    decide_sleep=decide,
                    idempotency_key="stale-snapshot",
                    now=NOW,
                    read_source_watermark=reader,
                )
    reader.assert_not_awaited()
    assert not await records(env)


@pytest.mark.parametrize("bound", [None, -1, True, "2"])
async def test_missing_or_invalid_committed_watermark_is_refused(env, bound):
    with pytest.raises(ValueError, match="watermark"):
        await register(env, source_watermark=bound)
    assert not await records(env)


async def test_dependency_cycle_includes_existing_edges(env):
    async with env.db._engine.begin() as conn:
        await conn.execute(
            insert(task_dependencies).values(
                task_id="source",
                depends_on_task_id="owner",
                dep_type="blocks",
            )
        )
    with pytest.raises(WaitError) as error:
        await register(env)
    assert error.value.code == "wait.cycle"
    assert not await records(env)


async def test_opposite_concurrent_waits_cannot_both_register(env):
    other = await owner(env.db, "other", "s2", "a2")
    results = await asyncio.gather(
        register(env, {"kind": "task", "task_id": "other"}),
        register(env, {"kind": "task", "task_id": "owner"}, scope=other),
        return_exceptions=True,
    )
    assert len([r for r in results if isinstance(r, dict)]) == 1
    assert [r.code for r in results if isinstance(r, WaitError)] == ["wait.cycle"]
    assert len(await records(env)) == 1


async def test_typed_dependency_uses_conditional_failure_policy(env):
    async with env.db._engine.begin() as conn:
        await conn.execute(
            insert(task_dependencies).values(
                task_id="owner",
                depends_on_task_id="source",
                dep_type="conditional-blocks",
            )
        )
    await source_state(env, "FAILED")
    condition = {
        "kind": "dependency",
        "task_id": "owner",
        "depends_on_task_id": "source",
        "dep_type": "conditional-blocks",
    }
    # Retriable failure is still unsatisfied, unlike a task settlement wait.
    receipt = await register(env, condition)
    assert receipt["success"]
    saved = (await records(env))[0]
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "source").values(retry_count=3))
        observed = await env.registry.observe(conn, Condition.from_record(saved["condition"]))
    assert observed.state == "satisfied"


async def test_timer_deadlines_and_no_commentary_predicate(env):
    receipt = await register(env, {"kind": "timer", "due_at": NOW + 20}, claimed="wait for CI")
    assert receipt["escalate_at"] == NOW + 320
    assert sorted(d["deadline_type"] for d in await records(env, wait_deadlines)) == [
        "ceiling",
        "due",
        "escalation",
    ]


@pytest.mark.parametrize("delta", [0, 0.5, 86401])
async def test_timer_outside_allowed_interval_is_refused(env, delta):
    with pytest.raises(WaitError) as error:
        await register(env, {"kind": "timer", "due_at": NOW + delta})
    assert error.value.code == "wait.condition_required"
    assert not await records(env)


async def test_agent_can_only_shorten_escalation_deadline(env):
    with pytest.raises(WaitError):
        await register(env, escalate_after_seconds=7201)
    assert not await records(env)
    assert (await register(env, escalate_after_seconds=60))["escalate_at"] == NOW + 60


async def test_loop_policy_can_force_hibernation_before_insert(env):
    loop = AsyncMock(return_value=SleepDecision("hibernate", "wait.loop_detected"))
    receipt = await register(env, loop_check=loop, decide_sleep=lambda *_: 1 / 0)
    assert receipt["next_step"] == "checkpoint_then_end_turn"
    assert receipt["decision"]["reason"] == "wait.loop_detected"
    assert receipt["ceiling_at"] is None


async def send(
    env,
    message_id,
    *,
    sender="peer",
    recipient="s",
    to_kind="session",
    thread="thread",
    from_kind="session",
    project="p",
):
    async with env.db._engine.begin() as conn:
        return await conn.scalar(
            insert(messages)
            .values(
                id=message_id,
                project_id=project,
                from_kind=from_kind,
                from_id=sender,
                to_kind=to_kind,
                to_id=recipient,
                thread_id=thread,
                body="hello",
                created_at=NOW,
            )
            .returning(messages.c.created_seq)
        )


async def test_only_addressed_incoming_messages_after_server_cursor_satisfy(env):
    cursor = await send(env, "out", sender="s", recipient="peer")
    await send(env, "self", sender="s")
    await send(env, "other", recipient="another")
    receipt = await register(env, {"kind": "message", "thread_id": "thread", "after_seq": cursor})
    assert receipt["success"]
    saved = (await records(env))[0]
    await send(env, "incoming", recipient="owner", to_kind="task")
    async with env.db._engine.connect() as conn:
        observed = await env.registry.observe(conn, Condition.from_record(saved["condition"]))
    assert observed.state == "satisfied" and observed.result_ref == "message:incoming"


async def test_unjoined_thread_is_refused_and_payload_is_not_disclosed(env):
    await send(env, "private", recipient="another")
    with pytest.raises(WaitError) as error:
        await register(env, {"kind": "message", "thread_id": "thread", "after_seq": 0})
    assert error.value.code == "wait.out_of_scope"
    assert not await records(env)


async def test_incoming_reply_before_registration_has_no_wait_row(env):
    await send(env, "answer")
    with pytest.raises(WaitError) as error:
        await register(env, {"kind": "message", "thread_id": "thread", "after_seq": 0})
    assert error.value.details["result_ref"] == "message:answer"
    assert not await records(env)


async def test_human_thread_has_no_deadlines_and_ignores_supervisor_reply(env):
    cursor = await send(env, "question", sender="s", recipient="dashboard", to_kind="user")
    await send(env, "supervisor", sender="supervisor-p")
    await send(env, "unaddressed-human", sender="dashboard", from_kind="user", recipient="other")
    receipt = await register(
        env,
        {"kind": "human", "subject": "message_thread", "thread_id": "thread", "after_seq": cursor},
    )
    assert receipt["next_step"] == "checkpoint_then_end_turn"
    assert receipt["escalate_at"] is None and receipt["ceiling_at"] is None
    assert not await records(env, wait_deadlines)
    await send(env, "human", sender="dashboard", from_kind="user")
    saved = (await records(env))[0]
    async with env.db._engine.connect() as conn:
        observed = await env.registry.observe(conn, Condition.from_record(saved["condition"]))
    assert observed.result_ref == "message:human"


async def test_human_thread_requires_a_request_addressed_to_a_human(env):
    cursor = await send(env, "question", sender="s", recipient="supervisor-p")
    with pytest.raises(WaitError) as error:
        await register(
            env,
            {
                "kind": "human",
                "subject": "message_thread",
                "thread_id": "thread",
                "after_seq": cursor,
            },
        )
    assert error.value.code == "wait.source_unresolved"
    assert not await records(env)


async def test_task_comment_human_subject_uses_actual_comment_schema(env):
    await register(env, {"kind": "human", "subject": "task_comment", "task_id": "source"})
    async with env.db._engine.begin() as conn:
        await conn.execute(
            insert(task_comments).values(
                id="reply",
                task_id="source",
                project_id="p",
                body="done",
                author_kind="user",
                author_id="dashboard",
                created_at=NOW + 1,
            )
        )
        saved = await get_wait(conn, (await records(env))[0]["id"], project_id="p")
        observed = await env.registry.observe(conn, Condition.from_record(saved["condition"]))
    assert observed.result_ref == "task_comment:reply"


async def seed_repo(env, project="p"):
    async with env.db._engine.begin() as conn:
        await conn.execute(
            insert(repos).values(
                id="repo",
                project_id=project,
                url="https://example/repo",
                checkout_base_path="/tmp/repo",
            )
        )


async def seed_batch(env, state="testing"):
    async with env.db._engine.begin() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="batch",
                project_id="p",
                repository_id="repo",
                request_id="request",
                source_manifest_digest="digest",
                base_sha=None if state == "empty" else SHA,
                integration_branch=None if state == "empty" else "aq/batch",
                lifecycle=state,
                current_revision=2,
                policy_snapshot={},
                artifact_snapshot={},
                cleanup_state="pending",
                created_at=NOW,
                updated_at=NOW,
            )
        )


def ci_snapshot(cond, *, head=SHA, state="unsatisfied"):
    return Snapshot(
        "p",
        0,
        Observation(
            "run:1",
            state,
            "ci:result",
            {
                "repository_id": "repo",
                "head_sha": head,
                "check_name": "tests",
                "run_id": "123",
                "identity_verified": True,
                "exists_on_origin": True,
            },
        ),
        "ci:repo",
    )


@pytest.mark.parametrize("kind", ["ci_check", "ci_run"])
async def test_ci_exact_identity_and_result_before_registration(env, kind):
    await seed_repo(env)
    await seed_batch(env)
    cond = {
        "kind": kind,
        "repository_id": "repo",
        "head_sha": SHA,
        "check_name" if kind == "ci_check" else "run_id": "tests" if kind == "ci_check" else "123",
    }
    reader = AsyncMock(side_effect=lambda conn, c: ci_snapshot(c, head="b" * 40))
    registry = build_registry(ci_reader=reader)
    with pytest.raises(WaitError) as error:
        await register(env, cond, registry=registry)
    assert error.value.code == "wait.source_unresolved"
    assert not await records(env)
    reader.side_effect = lambda conn, c: ci_snapshot(c, state="satisfied")
    with pytest.raises(WaitError) as error:
        await register(env, cond, registry=registry)
    assert error.value.code == "wait.already_satisfied"
    assert error.value.details["result_ref"] == "ci:result"
    reader.side_effect = lambda conn, c: ci_snapshot(c)
    assert (await register(env, cond, registry=registry))["success"]


async def test_ci_unknown_head_and_foreign_repo_do_not_call_watcher(env):
    await seed_repo(env)
    reader = AsyncMock()
    condition = {
        "kind": "ci_check",
        "repository_id": "repo",
        "head_sha": SHA,
        "check_name": "tests",
    }
    with pytest.raises(WaitError):
        await register(env, condition, registry=build_registry(ci_reader=reader))
    reader.assert_not_awaited()
    async with env.db._engine.begin() as conn:
        await conn.execute(update(repos).values(project_id="q"))
    with pytest.raises(WaitError) as error:
        await register(env, condition, registry=build_registry(ci_reader=reader))
    assert error.value.code == "wait.out_of_scope"
    reader.assert_not_awaited()


async def test_transient_ci_verification_does_not_create_wait(env):
    await seed_repo(env)
    await seed_batch(env)
    with pytest.raises(WaitError) as error:
        await register(
            env,
            {"kind": "ci_check", "repository_id": "repo", "head_sha": SHA, "check_name": "tests"},
            registry=build_registry(ci_reader=AsyncMock(side_effect=TimeoutError)),
        )
    assert error.value.details["retry_after"] == 30
    assert not await records(env)


@pytest.mark.parametrize("state", ["promoted", "aborted", "failed"])
async def test_batch_settlement_preserves_actual_lifecycle(env, state):
    await seed_batch(env)
    receipt = await register(env, {"kind": "batch", "batch_id": "batch"})
    async with env.db._engine.begin() as conn:
        # A candidate revision changes without replacing the baseline generation.
        await conn.execute(update(integration_batches).values(lifecycle=state, current_revision=3))
        saved = await get_wait(conn, receipt["wait_id"], project_id="p")
        observed = await env.registry.observe(conn, Condition.from_record(saved["condition"]))
    assert observed.state == "satisfied" and observed.facts["lifecycle"] == state


async def test_empty_batch_is_already_settled_without_mutating_sealed_identity(env):
    await seed_batch(env, "empty")
    with pytest.raises(WaitError) as error:
        await register(env, {"kind": "batch", "batch_id": "batch"})
    assert error.value.code == "wait.already_satisfied"
    assert error.value.details["result_ref"] == "batch:batch@2"
    assert not await records(env)


def resource_snapshot(
    cond, *, revision="1", state="unsatisfied", project="p", generation=0, facts=None
):
    return Snapshot(
        project,
        generation,
        Observation(
            revision,
            state,
            "admission:current",
            {
                "blocker": cond.parameters["blocker"],
                "identity": cond.parameters["identity"],
                "owner_kind": cond.context["owner_kind"],
                "owner_id": cond.context["owner_id"],
                **(facts or {}),
            },
        ),
        f"resource:{cond.parameters['blocker']}",
        (NodeKey("slot:p:profile", "p"),),
    )


async def test_resource_service_can_register_multiple_constraints(env):
    reader = AsyncMock(side_effect=lambda conn, cond: resource_snapshot(cond))

    scope = Scope("p", "scheduler", "source", principal="service")
    for name in ("one", "two"):
        receipt = await register(
            env,
            {"kind": "resource", "blocker": "slot", "identity": name, "source_revision": "1"},
            scope=scope,
            idempotency_key=name,
            registry=build_registry(resource_reader=reader),
        )
        assert receipt["success"]
    assert len(await records(env)) == 2
    assert all(e["relation"] == "resource" for e in await records(env, wait_edges))


@pytest.mark.parametrize(
    "blocker",
    [
        "slot",
        "workspace",
        "provider",
        "backoff",
        "budget",
        "policy_hold",
        "branch_owner",
        "batch_hold",
    ],
)
async def test_resource_later_cycle_reobserves_typed_predicate_without_changing_generation(
    env, blocker
):
    observation = {"revision": "cycle-A"}
    reader = AsyncMock(side_effect=lambda conn, cond: resource_snapshot(cond, **observation))
    registry = build_registry(resource_reader=reader)
    scope = Scope("p", "scheduler", "source", principal="service")
    raw = {
        "kind": "resource",
        "blocker": blocker,
        "identity": "normalized-scope",
        "source_revision": "cycle-A",
    }
    receipt = await register(env, raw, scope=scope, registry=registry)
    assert await register(env, raw, scope=scope, registry=registry) == receipt
    saved = (await records(env))[0]
    cond = Condition.from_record(saved["condition"])
    assert saved["source_generation"] == 0
    assert saved["source_key"] == f"resource:{blocker}:normalized-scope"
    async with env.db._engine.connect() as conn:
        observation.update(revision="cycle-B")
        # A newer cycle/external revision alone is not a satisfied predicate.
        assert (await registry.observe(conn, cond)).state == "unsatisfied"
        observation.update(revision="external-change", state="satisfied")
        assert (await registry.observe(conn, cond)).state == "satisfied"
    assert len(await records(env)) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"generation": 1},
        {"project": "q"},
        {"facts": {"identity": "another-source"}},
        {"facts": {"blocker": "provider"}},
        {"facts": {"owner_kind": "task"}},
        {"facts": {"owner_id": "another-owner"}},
    ],
)
async def test_resource_mismatched_identity_owner_or_incarnation_cannot_satisfy(env, change):
    observation = {}
    reader = AsyncMock(side_effect=lambda conn, cond: resource_snapshot(cond, **observation))
    registry = build_registry(resource_reader=reader)
    scope = Scope("p", "scheduler", "source", principal="service")
    raw = {
        "kind": "resource",
        "blocker": "slot",
        "identity": "normalized-scope",
        "source_revision": "1",
    }
    await register(env, raw, scope=scope, registry=registry)
    cond = Condition.from_record((await records(env))[0]["condition"])
    observation.update(state="satisfied", **change)
    async with env.db._engine.connect() as conn:
        assert (await registry.observe(conn, cond)).state == "unavailable"
    assert (await records(env))[0]["outcome"] == "pending"
    with pytest.raises(WaitError):
        await register(env, raw, scope=scope, registry=registry, idempotency_key="wrong-source")
    assert len(await records(env)) == 1


async def test_resource_initial_revision_must_match_authoritative_admission_read(env):
    reader = AsyncMock(
        side_effect=lambda conn, cond: resource_snapshot(cond, revision="new-config")
    )
    with pytest.raises(WaitError) as error:
        await register(
            env,
            {
                "kind": "resource",
                "blocker": "workspace",
                "identity": "normalized-scope",
                "source_revision": "old-config",
            },
            scope=Scope("p", "scheduler", "source", principal="service"),
            registry=build_registry(resource_reader=reader),
        )
    assert error.value.code == "wait.source_unresolved"
    assert not await records(env)


async def test_observe_detects_source_deletion_and_generation_replacement(env):
    await register(env)
    cond = Condition.from_record((await records(env))[0]["condition"])
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "source").values(claim_epoch=1))
        observed = await env.registry.observe(conn, cond)
        assert observed.state == "unavailable" and observed.facts["reason"] == "source_changed"
        await conn.execute(delete(tasks).where(tasks.c.id == "source"))
        assert (await env.registry.observe(conn, cond)).state == "unavailable"


async def test_job_fences_ownership_attempt_and_preserves_failure_result(env):
    async with env.db._engine.begin() as conn:
        await conn.execute(
            insert(jobs).values(
                id="job",
                project_id="p",
                task_id="owner",
                owner_kind="task",
                owner_id="owner",
                idempotency_key="j",
                request_hash="hash",
                preset="test",
                preset_version=1,
                argv=[],
                contract={},
                workspace_id="w",
                workspace_generation=1,
                input_mode="snapshot",
                job_class="shared",
                weight=1,
                priority_band=1,
                state="running",
                submitted_at=NOW,
                queue_deadline=NOW + 100,
                run_timeout=100,
                runner_nonce="nonce",
            )
        )
    with pytest.raises(WaitError) as error:
        await register(env, {"kind": "job", "job_id": "job", "attempt": 2})
    assert error.value.code == "wait.source_unresolved"
    receipt = await register(env, {"kind": "job", "job_id": "job"})
    assert not await records(env, wait_edges)  # own job is not a task self-cycle
    async with env.db._engine.begin() as conn:
        await conn.execute(
            update(jobs).values(
                state="failed",
                exit_code=7,
                ended_at=NOW + 1,
                infra_reason="test_failure",
                state_version=1,
            )
        )
        saved = await get_wait(conn, receipt["wait_id"], project_id="p")
        observed = await env.registry.observe(conn, Condition.from_record(saved["condition"]))
    assert observed.state == "satisfied"
    assert observed.facts == {"state": "failed", "exit_code": 7, "reason": "test_failure"}
    other = await owner(env.db, "other", "s2", "a2")
    with pytest.raises(WaitError) as error:
        await register(env, {"kind": "job", "job_id": "job"}, scope=other)
    assert error.value.code == "wait.out_of_scope"


async def test_pr_head_replacement_and_close_unmerged_are_unavailable(env):
    await seed_repo(env)
    facts = {
        "repository_id": "repo",
        "exists_on_origin": True,
        "pr_number": 42,
        "head_sha": SHA,
        "state": "open",
        "merged": False,
    }
    reader = AsyncMock(
        side_effect=lambda conn, c: Snapshot(
            "p",
            0,
            Observation("1", "unsatisfied", "pr:repo:42", dict(facts)),
            "pr:repo",
        )
    )
    registry = build_registry(pr_reader=reader)
    receipt = await register(
        env,
        {
            "kind": "pull_request",
            "repository_id": "repo",
            "pr_number": 42,
            "expected_head_sha": SHA,
        },
        registry=registry,
    )
    async with env.db._engine.connect() as conn:
        saved = await get_wait(conn, receipt["wait_id"], project_id="p")
        cond = Condition.from_record(saved["condition"])
        facts["head_sha"] = "b" * 40
        assert (await registry.observe(conn, cond)).state == "unavailable"
        facts.update(head_sha=SHA, state="closed")
        observed = await registry.observe(conn, cond)
        assert observed.state == "unavailable" and observed.facts["reason"] == "source_changed"


async def test_review_revision_and_user_decision_are_verified(env):
    async with env.db._engine.begin() as conn:
        await conn.execute(
            insert(doc_reviews).values(
                id="review",
                project_id="p",
                author_task_id="owner",
                kind="spec",
                title="Design",
                vault_path="specs/example.md",
                current_revision=1,
                state="in_review",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    with pytest.raises(WaitError):
        await register(
            env, {"kind": "human", "subject": "review", "review_id": "review", "revision": 2}
        )
    receipt = await register(env, {"kind": "human", "subject": "review", "review_id": "review"})
    async with env.db._engine.begin() as conn:
        saved = await get_wait(conn, receipt["wait_id"], project_id="p")
        cond = Condition.from_record(saved["condition"])
        await conn.execute(
            update(doc_reviews).values(
                state="approved", decided_by="supervisor:p", decided_at=NOW + 1
            )
        )
        assert (await env.registry.observe(conn, cond)).state == "unsatisfied"
        await conn.execute(update(doc_reviews).values(decided_by="human:local-operator"))
        assert (await env.registry.observe(conn, cond)).state == "satisfied"


async def test_playbook_condition_verifies_run_identity_and_revision(env):
    async with env.db._engine.begin() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                artifact_sha256="artifact",
                playbook_id="pb",
                scope="project",
                scope_identifier="p",
                source_digest="digest",
                contract_fingerprint="fingerprint",
                compiler_build="test",
                path="artifact.json",
                created_at=NOW,
            )
        )
        await conn.execute(
            insert(playbook_v2_runs).values(
                run_id="run",
                playbook_id="pb",
                artifact_sha256="artifact",
                rule_id="rule",
                started_at=NOW,
                updated_at=NOW,
            )
        )
        await conn.execute(
            insert(playbook_waits).values(
                wait_id="pb-wait",
                run_id="run",
                step_id="step",
                kind="event",
                snapshot_version=2,
                created_at=NOW,
            )
        )
    with pytest.raises(WaitError):
        await register(env, {"kind": "playbook_condition", "run_id": "wrong", "wait_id": "pb-wait"})
    receipt = await register(
        env, {"kind": "playbook_condition", "run_id": "run", "wait_id": "pb-wait"}
    )
    async with env.db._engine.begin() as conn:
        saved = await get_wait(conn, receipt["wait_id"], project_id="p")
        cond = Condition.from_record(saved["condition"])
        await conn.execute(update(playbook_waits).values(state="claimed", claimed_event_id="event"))
        observed = await env.registry.observe(conn, cond)
        assert observed.state == "satisfied" and observed.facts["event_id"] == "event"


async def test_collaboration_cursor_and_addressed_recipient_are_both_enforced(env):
    async with env.db._engine.begin() as conn:
        await conn.execute(
            insert(collaboration_threads).values(
                id="collab-example",
                project_id="p",
                created_by_kind="session",
                created_by_id="s",
                idempotency_key="thread",
                request_hash="hash",
                created_at=NOW,
                deadline_at=NOW + 60,
                last_seq=2,
            )
        )
        await conn.execute(
            insert(collaboration_members).values(
                thread_id="collab-example",
                task_id="owner",
                state="accepted",
                invited_at=NOW,
                accepted_at=NOW,
                accepted_claim_epoch=1,
            )
        )
        for seq in (1, 2):
            await conn.execute(
                insert(collaboration_messages).values(
                    thread_id="collab-example",
                    seq=seq,
                    message_id=f"c{seq}",
                    sender_task_id="peer",
                    sender_claim_epoch=1,
                    sender_session_id="peer-s",
                    client_key=f"k{seq}",
                    body_bytes=3,
                    created_at=NOW,
                )
            )
    await send(env, "c1", thread="collab-example", recipient="owner", to_kind="task")
    await send(env, "c2", thread="collab-example", recipient="another", to_kind="task")
    receipt = await register(
        env, {"kind": "message", "thread_id": "collab-example", "after_seq": 1}
    )
    assert receipt["success"]  # seq 1 is before cursor, seq 2 addresses somebody else
    await send(env, "c2:fanout", thread="collab-example", recipient="owner", to_kind="task")
    async with env.db._engine.connect() as conn:
        saved = await get_wait(conn, receipt["wait_id"], project_id="p")
        observed = await env.registry.observe(conn, Condition.from_record(saved["condition"]))
    assert observed.result_ref == "collaboration:collab-example:2"
    assert observed.facts["message_id"] == "c2:fanout"


@pytest.fixture
async def db(reuse_database):
    db = await reuse_database("wait-sources")
    await db.create_project(Project(id="p", name="P"))
    return db


def source(revision="1", **overrides):
    return {"source_key": "task:t", "generation": 0, "revision": revision,
            "outcome_ref": "task:t", "payload": {"status": "COMPLETED"}, **overrides}


async def test_revision_replay_and_rollback_are_atomic(db):
    first = await db.wait_source_commit(**source())
    assert first > 0
    assert await db.wait_source_commit(**source()) is None
    assert await db.wait_source_commit(**source(generation=1)) > first
    with pytest.raises(RuntimeError, match="abort"):
        async with db._engine.begin() as conn:
            await record_source_event(conn, **source("rollback"))
            raise RuntimeError("abort")
    async with db._engine.connect() as conn:
        rows = (await conn.execute(select(wait_source_events))).mappings().all()
    assert len(rows) == 2
    assert all(row["payload_hash"] == payload_hash(row["payload"]) for row in rows)
    assert {row["source_revision"] for row in rows} == {"1"}


async def test_watermark_cannot_pass_an_earlier_inflight_writer(db):
    # A transaction opened under the engine's stricter default must not keep
    # the failed try-lock query's old snapshot after the writer commits.
    db._engine = db._engine.execution_options(isolation_level="REPEATABLE READ")
    async with db._engine.connect() as first_conn:
        tx = await first_conn.begin()
        first = await record_source_event(first_conn, **source("first"))
        second = await db.wait_source_commit(**source("second"))
        assert second > first  # Independent source writers can commit concurrently.
        watermark = asyncio.create_task(db.wait_source_watermark())
        # Give the watermark reader an opportunity to reach its fence.
        await asyncio.sleep(0.05)
        assert not watermark.done()
        # The waiting reader must not queue a lock that blocks later writers.
        third = await asyncio.wait_for(db.wait_source_commit(**source("third")), 2)
        await tx.commit()
        assert await watermark == third


async def test_notifications_are_commit_only_and_replay_is_silent(db):
    received = []
    notified = asyncio.Event()

    def receive(_conn, _pid, _channel, payload):
        received.append(int(payload))
        notified.set()

    async with db._engine.connect() as listener:
        raw = await listener.get_raw_connection()
        driver = raw.driver_connection
        await driver.add_listener("aq_wait_source", receive)
        try:
            with pytest.raises(RuntimeError):
                async with db._engine.begin() as conn:
                    await record_source_event(conn, **source("rollback"))
                    assert not received
                    raise RuntimeError("abort")
            seq = await db.wait_source_commit(**source("committed"))
            await asyncio.wait_for(notified.wait(), 2)
            assert received == [seq]
            notified.clear()
            assert await db.wait_source_commit(**source("committed")) is None
            # A second committed event is a delivery barrier for the silent replay.
            second = await db.wait_source_commit(**source("barrier"))
            await asyncio.wait_for(notified.wait(), 2)
            assert received == [seq, second]
        finally:
            await driver.remove_listener("aq_wait_source", receive)


async def test_watermark_retention_keeps_unread_events_and_cursor_floor(db):
    old = 100.0
    first = await db.wait_source_commit(**source("old"))
    pending = await db.wait_source_commit(**source("pending"))
    unread = await db.wait_source_commit(**source("unread"))
    async with db._engine.begin() as conn:
        await conn.execute(update(wait_source_events).values(committed_at=old))
        await conn.execute(update(wait_source_events).where(
            wait_source_events.c.seq.in_([first, unread]),
        ).values(dispatched_at=old))
        await conn.execute(insert(wait_listener_cursors).values(
            listener_key="source", last_seq=pending, updated_at=old,
        ))
    assert await db.purge_wait_source_events(now=old + 7 * 86400) == 0
    assert await db.purge_wait_source_events(now=old + 7 * 86400 + 1) == 1
    async with db._engine.connect() as conn:
        assert list((await conn.execute(select(wait_source_events.c.seq)
                                      .order_by(wait_source_events.c.seq))).scalars()) == [pending, unread]
    assert await db.wait_source_watermark(source_key="absent") == pending
    assert await db.wait_source_watermark() == unread
    async with db._engine.begin() as conn:
        await conn.execute(update(wait_source_events).values(dispatched_at=old))
        await conn.execute(update(wait_listener_cursors).values(last_seq=unread))
    assert await db.purge_wait_source_events(now=old + 7 * 86400 + 1) == 2
    # Reconstruct the reader with a fresh pool: retention must not erase the
    # durable source floor or allow a restarted listener to rewind its cursor.
    restarted = WaitSourceQueriesMixin()
    restarted._engine = create_async_engine(db._engine.url)
    try:
        assert await restarted.wait_source_watermark() == unread
        assert await restarted.wait_source_commit(**source("after-restart")) > unread
    finally:
        await restarted._engine.dispose()


async def timer(db, *, generation=1, deadline_type="due", cancelled_at=None):
    async with db._engine.begin() as conn:
        if generation == 1:
            await conn.execute(insert(wait_records).values(
                id="w", project_id="p", owner_kind="task", owner_id="owner", origin="agent",
                condition_kind="timer", condition={"due_at": 10}, source_key="timer:w",
                source_generation=0, source_watermark_seq=0, verification_state="verified",
                evidence_ref="timer:w", verified_at=1, created_at=1, request_hash="hash",
            ))
        await conn.execute(insert(wait_deadlines).values(
            id=f"d-{generation}", wait_id="w", deadline_type=deadline_type, generation=generation,
            due_at=10, cancelled_at=cancelled_at,
        ))


async def test_timer_firing_is_cas_fenced_and_rolls_back_with_its_event(db):
    await timer(db)
    assert await db.fire_wait_due_deadline("w", 1, now=9) is None
    with pytest.raises(RuntimeError):
        async with db._engine.begin() as conn:
            assert await db.fire_wait_due_deadline("w", 1, now=11, conn=conn)
            raise RuntimeError("abort")
    assert await db.fire_wait_due_deadline("w", 1, now=11)
    assert await db.fire_wait_due_deadline("w", 1, now=12) is None
    async with db._engine.connect() as conn:
        event = (await conn.execute(select(wait_source_events))).mappings().one()
    assert event["source_key"] == "timer:w" and event["occurred_at"] == 10


@pytest.mark.parametrize("case", ["cancelled", "superseded", "escalation"])
async def test_cancelled_stale_and_escalation_deadlines_emit_no_timer_event(db, case):
    await timer(db, cancelled_at=5 if case == "cancelled" else None,
                deadline_type="escalation" if case == "escalation" else "due")
    if case == "superseded":
        await timer(db, generation=2)
    assert await db.fire_wait_due_deadline("w", 1, now=11) is None
    assert await db.wait_source_watermark() == 0
