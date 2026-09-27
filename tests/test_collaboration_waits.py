"""Collaboration thread semantics inside the durable message wait, on PostgreSQL."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select, update

from src.agent_waits import ProducerObservation, WaitError, resolve_wait
from src.collaboration import PARTNER_GRACE_SECONDS
from src.database import Database
from src.database.tables import messages, sessions, tasks
from src.models import Agent, AgentState, Project, SessionRecord, Task, TaskStatus
from tests.db_fixtures import lease_dsn

NOW = 1_800_000_000.0
TASKS = {"one": "p", "two": "p", "three": "p", "foreign": "q", "foreign2": "q"}


@pytest.fixture
async def env():
    db = Database(lease_dsn("collaboration-waits"))
    await db.initialize()
    await db.create_project(Project(id="p", name="P"))
    await db.create_project(Project(id="q", name="Q"))
    for task_id, project_id in TASKS.items():
        agent_id = f"agent-{task_id}"
        await db.create_agent(
            Agent(id=agent_id, name=agent_id, profile_id="worker-codex", state=AgentState.BUSY)
        )
        await db.create_task(
            Task(
                id=task_id,
                title=task_id,
                description="",
                project_id=project_id,
                status=TaskStatus.IN_PROGRESS,
                assigned_agent_id=agent_id,
            )
        )
        await db.update_agent(agent_id, current_task_id=task_id)
        await db.create_session(
            SessionRecord(
                id=f"s-{task_id}",
                project_id=project_id,
                profile_id="worker-codex",
                harness="codex",
                provider="fake",
                name=task_id,
                lifecycle="pool",
                work_dir="/tmp/collaboration-waits",
                epoch="boot",
                instance_token=f"token-{task_id}",
                started_at=NOW,
                task_id=task_id,
                state="running",
                agent_id=agent_id,
                claim_phase="active",
                last_claim_epoch=1,
            )
        )
    await db.create_session(
        SessionRecord(
            id="super",
            project_id="p",
            profile_id="supervisor",
            harness="codex",
            provider="fake",
            name="n-super",
            lifecycle="named",
            work_dir="/tmp/super",
            epoch="boot",
            instance_token="super-token",
            started_at=NOW,
            state="running",
        )
    )
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).values(claim_epoch=1))
    yield SimpleNamespace(db=db)
    await db.close()


def identity(task_id="one", epoch=1):
    return {
        "session_id": f"s-{task_id}",
        "instance_token": f"token-{task_id}",
        "project_id": TASKS[task_id],
        "claim_epoch": epoch,
        "elevated": False,
    }


SUPERVISOR = {
    "session_id": "super",
    "instance_token": "super-token",
    "project_id": "p",
    "claim_epoch": 0,
    "elevated": True,
}


async def create(env, task_ids=("one", "two"), *, project_id="p", key="thread", **values):
    return await env.db.create_collaboration_thread(
        project_id=project_id,
        created_by_kind="operator",
        created_by_id="operator",
        idempotency_key=key,
        task_ids=list(task_ids),
        goal="Review diff",
        deadline_seconds=values.pop("deadline_seconds", 7200),
        message_budget=values.pop("message_budget", 40),
        now=values.pop("now", NOW),
    )


async def accept(env, thread, *task_ids, epoch=1):
    for task_id in task_ids:
        await env.db.accept_collaboration(
            thread_id=thread["id"],
            project_id=TASKS[task_id],
            task_id=task_id,
            claim_epoch=epoch,
            now=NOW,
        )


async def joined(env, task_ids=("one", "two"), **values):
    thread = await create(env, task_ids, **values)
    await accept(env, thread, *task_ids)
    return thread


async def send(env, thread, sender="two", *, key="send", now=NOW + 1):
    return await env.db.append_collaboration_message(
        thread_id=thread["id"],
        project_id="p",
        sender_task_id=sender,
        sender_claim_epoch=1,
        sender_session_id=f"s-{sender}",
        client_key=key,
        body="finding",
        now=now,
    )


async def register(env, thread, *, after_seq=0, owner=None, key="wait", now=NOW + 2):
    return await env.db.register_agent_wait(
        identity=owner or identity(),
        kind="message",
        match={"thread_id": thread["id"], "after_seq": after_seq},
        deadline_at=now + 7200,
        idempotency_key=key,
        now=now,
    )


async def reconcile(env, row, now):
    await env.db.reconcile_agent_waits(now=now, wait_id=row["id"])
    return await env.db.get_agent_wait(row["id"])


async def result_count(env, wait_id):
    async with env.db._engine.connect() as conn:
        return await conn.scalar(
            select(func.count())
            .select_from(messages)
            .where(messages.c.id == f"wait:{wait_id}:result")
        )


async def set_task(env, task_id, **values):
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == task_id).values(**values))


def test_unavailable_observation_carries_its_typed_reason():
    typed = resolve_wait(
        ProducerObservation(
            available=False, reason="peer_failed", digest={"thread_id": "collab-x"}
        ),
        deadline=100,
        now=99,
    )
    assert typed.state == "satisfied"
    assert typed.digest == {"reason": "peer_failed", "thread_id": "collab-x"}
    default = resolve_wait(ProducerObservation(available=False), deadline=100, now=99)
    assert default.digest == {"reason": "source_unavailable"}
    expired = resolve_wait(
        ProducerObservation(available=False, reason="peer_gone"), deadline=100, now=100
    )
    assert expired.state == "expired" and expired.digest == {"reason": "deadline_expired"}


async def test_message_before_registration_satisfies_immediately(env):
    thread = await joined(env)
    sent = await send(env, thread)
    row = await register(env, thread)
    assert row["state"] == "satisfied"
    assert row["digest"] == {
        "thread_id": thread["id"],
        "seq": sent["seq"],
        "message_id": sent["message_id"],
        "sender_task_id": "two",
    }
    assert row["result_ref"] == f"collaboration:{thread['id']}:{sent['seq']}"
    assert await result_count(env, row["id"]) == 1


async def test_register_then_append_resolves_once(env):
    thread = await joined(env)
    row = await register(env, thread)
    assert row["state"] == "active"
    assert (await reconcile(env, row, NOW + 3))["state"] == "active"
    sent = await send(env, thread, now=NOW + 4)
    first = await reconcile(env, row, NOW + 5)
    second = await reconcile(env, row, NOW + 6)
    assert first["state"] == second["state"] == "satisfied"
    assert first["digest"]["seq"] == sent["seq"] == 1
    assert second["version"] == first["version"] == 2
    assert await result_count(env, row["id"]) == 1


async def test_own_messages_never_satisfy(env):
    thread = await joined(env)
    await send(env, thread, "one", key="mine")
    row = await register(env, thread)
    assert row["state"] == "active"
    assert (await reconcile(env, row, NOW + PARTNER_GRACE_SECONDS + 10))["state"] == "active"
    await send(env, thread, "two", key="theirs", now=NOW + 3)
    result = await reconcile(env, row, NOW + PARTNER_GRACE_SECONDS + 11)
    assert result["state"] == "satisfied"
    assert result["digest"]["seq"] == 2 and result["digest"]["sender_task_id"] == "two"


async def test_cursor_ignores_delivery_and_read_markers(env):
    thread = await joined(env)
    await send(env, thread)
    async with env.db._engine.begin() as conn:
        await conn.execute(
            update(messages)
            .where(messages.c.thread_id == thread["id"])
            .values(delivered_at=NOW + 1, read_at=NOW + 1, archived_at=NOW + 1)
        )
    row = await register(env, thread)
    assert row["state"] == "satisfied" and row["digest"]["seq"] == 1
    later = await register(
        env, thread, after_seq=1, owner=identity("two"), key="two-wait", now=NOW + 3
    )
    assert later["state"] == "active"


@pytest.mark.parametrize("how", ["closed", "expired", "deadline"])
async def test_closed_or_expired_thread_resolves_thread_closed(env, how):
    thread = await joined(env, deadline_seconds=60 if how == "deadline" else 7200)
    row = await register(env, thread)
    now = NOW + 10
    if how == "deadline":
        # Past its deadline but not yet expired by the lifecycle tick.
        now = thread["deadline_at"]
    else:
        await env.db.close_collaboration_thread(
            thread_id=thread["id"], project_id="p", reason=how, now=NOW + 3
        )
    result = await reconcile(env, row, now)
    assert result["state"] == "satisfied"
    assert result["digest"] == {"reason": "thread_closed", "thread_id": thread["id"]}
    assert await result_count(env, row["id"]) == 1


async def test_pending_message_beats_closure(env):
    thread = await joined(env)
    row = await register(env, thread)
    sent = await send(env, thread, now=NOW + 3)
    await env.db.close_collaboration_thread(
        thread_id=thread["id"], project_id="p", reason="closed", now=NOW + 4
    )
    result = await reconcile(env, row, NOW + 5)
    assert result["digest"]["seq"] == sent["seq"]
    after = await register(env, thread, after_seq=sent["seq"], key="after", now=NOW + 6)
    assert after["digest"] == {"reason": "thread_closed", "thread_id": thread["id"]}


async def test_budget_closing_message_is_delivered_before_closure(env):
    thread = await joined(env, message_budget=1)
    row = await register(env, thread)
    sent = await send(env, thread, now=NOW + 3)
    assert sent["thread"]["close_reason"] == "budget_exhausted"
    result = await reconcile(env, row, NOW + 4)
    assert result["digest"]["seq"] == sent["seq"] == 1
    after = await register(env, thread, after_seq=1, key="after", now=NOW + 5)
    assert after["digest"] == {"reason": "thread_closed", "thread_id": thread["id"]}


async def test_removed_owner_resolves_thread_closed(env):
    thread = await joined(env, ("one", "two", "three"))
    row = await register(env, thread)
    await env.db.remove_collaboration_member(
        thread_id=thread["id"], project_id="p", task_id="one", now=NOW + 3
    )
    assert (await env.db.get_collaboration_thread(thread["id"], project_id="p"))[
        "state"
    ] == "active"
    result = await reconcile(env, row, NOW + 4)
    assert result["digest"] == {"reason": "thread_closed", "thread_id": thread["id"]}


@pytest.mark.parametrize("status", ["FAILED", "BLOCKED"])
async def test_failed_partner_resolves_peer_failed(env, status):
    thread = await joined(env, ("one", "two", "three"))
    row = await register(env, thread)
    await set_task(env, "three", status=status)
    result = await reconcile(env, row, NOW + 3)
    assert result["digest"] == {"reason": "peer_failed", "thread_id": thread["id"]}


async def test_deleted_partner_resolves_peer_gone(env):
    thread = await joined(env, ("one", "two", "three"))
    row = await register(env, thread)
    async with env.db._engine.begin() as conn:
        await conn.execute(delete(sessions).where(sessions.c.task_id == "three"))
        await conn.execute(delete(tasks).where(tasks.c.id == "three"))
    result = await reconcile(env, row, NOW + 3)
    assert result["digest"] == {"reason": "peer_gone", "thread_id": thread["id"]}


async def test_completed_sole_partner_resolves_peer_gone(env):
    thread = await joined(env, ("one", "two", "three"))
    row = await register(env, thread)
    await set_task(env, "two", status="COMPLETED")
    # Another live partner remains, so the thread still has a peer.
    assert (await reconcile(env, row, NOW + 3))["state"] == "active"
    await set_task(env, "three", status="COMPLETED")
    result = await reconcile(env, row, NOW + 4)
    assert result["digest"] == {"reason": "peer_gone", "thread_id": thread["id"]}


async def test_removed_member_is_no_longer_a_peer(env):
    thread = await joined(env, ("one", "two", "three"))
    await env.db.remove_collaboration_member(
        thread_id=thread["id"], project_id="p", task_id="three", now=NOW + 1
    )
    row = await register(env, thread)
    async with env.db._engine.begin() as conn:
        await conn.execute(delete(sessions).where(sessions.c.task_id == "three"))
        await conn.execute(delete(tasks).where(tasks.c.id == "three"))
    assert (await reconcile(env, row, NOW + 3))["state"] == "active"


@pytest.mark.parametrize(
    "values",
    [
        {"status": "READY", "assigned_agent_id": None},
        {"status": "PAUSED"},
        {"status": "IN_PROGRESS", "assigned_agent_id": None},
    ],
)
async def test_partner_not_running_waits_out_the_grace(env, values):
    thread = await joined(env)
    row = await register(env, thread)
    await set_task(env, "two", **values)
    created = row["created_at"]
    grace = created + PARTNER_GRACE_SECONDS
    assert await env.db.blocking_wait_for("s-one", 1, grace - 1) is not None
    assert (await reconcile(env, row, grace - 1))["state"] == "active"
    assert await env.db.blocking_wait_for("s-one", 1, grace) is None
    result = await reconcile(env, row, grace)
    assert result["digest"] == {"reason": "partner_not_running", "thread_id": thread["id"]}


async def test_running_partner_holds_the_wait_past_the_grace(env):
    thread = await joined(env)
    row = await register(env, thread)
    later = await reconcile(env, row, NOW + PARTNER_GRACE_SECONDS * 10)
    assert later["state"] == "active"


async def test_registration_refuses_unjoined_threads_identically(env):
    foreign = await joined(env, ("foreign", "foreign2"), project_id="q", key="foreign")
    strangers = await joined(env, ("two", "three"), key="strangers")
    invited = await create(env, ("one", "three"), key="invited")
    removed = await joined(env, ("one", "two", "three"), key="removed")
    await env.db.remove_collaboration_member(
        thread_id=removed["id"], project_id="p", task_id="one", now=NOW + 1
    )
    rollover = await joined(env, ("one", "two"), key="rollover")
    missing = {"id": "collab-0000000000000000"}
    refusals = []
    for thread in (foreign, strangers, invited, removed, missing):
        with pytest.raises(WaitError) as exc:
            await register(env, thread, key=f"wait-{thread['id']}")
        refusals.append((exc.value.code, str(exc.value)))
    # Claim rollover: the owner holds epoch 2 but accepted only at epoch 1.
    await set_task(env, "one", claim_epoch=2)
    async with env.db._engine.begin() as conn:
        await conn.execute(
            update(sessions).where(sessions.c.id == "s-one").values(last_claim_epoch=2)
        )
    with pytest.raises(WaitError) as exc:
        await register(env, rollover, owner=identity(epoch=2), key="wait-rollover")
    refusals.append((exc.value.code, str(exc.value)))
    assert len(set(refusals)) == 1
    assert refusals[0][0] == "out_of_scope"
    await accept(env, rollover, "one", epoch=2)
    row = await register(env, rollover, owner=identity(epoch=2), key="wait-rollover")
    assert row["state"] == "active" and row["claim_epoch"] == 2


async def test_supervisor_wait_matches_any_sender_and_only_closure(env):
    thread = await joined(env)
    await send(env, thread, "one")
    row = await register(env, thread, owner=SUPERVISOR, key="sup")
    assert row["state"] == "satisfied" and row["digest"]["sender_task_id"] == "one"
    quiet = await register(env, thread, after_seq=1, owner=SUPERVISOR, key="sup-2")
    await set_task(env, "two", status="FAILED", assigned_agent_id=None)
    assert (await reconcile(env, quiet, NOW + PARTNER_GRACE_SECONDS * 2))["state"] == "active"
    await env.db.close_collaboration_thread(
        thread_id=thread["id"], project_id="p", reason="closed", now=NOW + 300
    )
    result = await reconcile(env, quiet, NOW + 301)
    assert result["digest"] == {"reason": "thread_closed", "thread_id": thread["id"]}


async def test_supervisor_registration_needs_the_threads_project(env):
    foreign = await joined(env, ("foreign", "foreign2"), project_id="q", key="foreign")
    with pytest.raises(WaitError) as exc:
        await register(env, foreign, owner=SUPERVISOR, key="sup")
    assert exc.value.code == "out_of_scope"


async def test_plain_message_wait_is_unchanged(env):
    first = await env.db.create_message(
        project_id="p",
        from_kind="system",
        from_id="producer",
        to_kind="task",
        to_id="one",
        thread_id="plain-thread",
        body="request",
    )
    row = await register(env, {"id": "plain-thread"}, after_seq=first.created_seq - 1)
    assert row["state"] == "satisfied"
    assert row["result_ref"] == f"message:{first.id}"
    assert row["digest"] == {
        "message_id": first.id,
        "created_seq": first.created_seq,
        "thread_id": "plain-thread",
    }
    with pytest.raises(WaitError) as exc:
        await register(env, {"id": "unknown-thread"}, key="unknown")
    assert str(exc.value) == "message thread is not authorized for this owner"
