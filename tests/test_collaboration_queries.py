"""Collaboration ordering and claim fencing on disposable PostgreSQL."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, insert, select, update

from src.collaboration import CollaborationError
from src.database import Database
from src.database.tables import (
    archived_tasks,
    collaboration_messages,
    messages,
    sessions,
    tasks,
)
from src.models import Agent, AgentState, Project, SessionRecord, Task, TaskStatus
from tests.db_fixtures import lease_dsn

NOW = 1_800_000_000.0


@pytest.fixture
async def env():
    db = Database(lease_dsn("collaboration"))
    await db.initialize()
    await db.create_project(Project(id="p", name="P"))
    await db.create_project(Project(id="q", name="Q"))
    for task_id in ("one", "two", "three", "four", "foreign", "done"):
        agent_id = f"agent-{task_id}"
        await db.create_agent(
            Agent(id=agent_id, name=agent_id, profile_id="worker-codex", state=AgentState.BUSY)
        )
        await db.create_task(
            Task(
                id=task_id,
                title=task_id,
                description="",
                project_id="q" if task_id == "foreign" else "p",
                status=TaskStatus.COMPLETED if task_id == "done" else TaskStatus.IN_PROGRESS,
                assigned_agent_id=agent_id,
            )
        )
        await db.update_agent(agent_id, current_task_id=task_id)
        await db.create_session(
            SessionRecord(
                id=f"s-{task_id}",
                project_id="q" if task_id == "foreign" else "p",
                profile_id="worker-codex",
                harness="codex",
                provider="fake",
                name=task_id,
                lifecycle="pool",
                work_dir="/tmp/collaboration",
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
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).values(claim_epoch=1))
    yield SimpleNamespace(db=db)
    await db.close()


async def create(env, *, task_ids=("one", "two"), key="thread", **values):
    return await env.db.create_collaboration_thread(
        project_id="p",
        created_by_kind="operator",
        created_by_id="operator",
        idempotency_key=key,
        task_ids=list(task_ids),
        goal=values.pop("goal", "Review diff"),
        deadline_seconds=values.pop("deadline_seconds", 7200),
        message_budget=values.pop("message_budget", 40),
        now=values.pop("now", NOW),
        **values,
    )


async def accept(env, thread, task_id="one", epoch=1, now=NOW):
    return await env.db.accept_collaboration(
        thread_id=thread["id"], project_id="p", task_id=task_id, claim_epoch=epoch, now=now
    )


async def send(env, thread, *, sender="one", key="send", now=NOW + 1, **values):
    return await env.db.append_collaboration_message(
        thread_id=thread["id"],
        project_id="p",
        sender_task_id=sender,
        sender_claim_epoch=values.pop("epoch", 1),
        sender_session_id=f"s-{sender}",
        client_key=key,
        body=values.pop("body", "A useful finding"),
        now=now,
        **values,
    )


async def count(env, thread, body_kind):
    async with env.db._engine.connect() as conn:
        return await conn.scalar(
            select(func.count())
            .select_from(messages)
            .where(messages.c.thread_id == thread["id"], messages.c.body_kind == body_kind)
        )


@pytest.mark.parametrize(
    "members",
    [
        ("one",),
        ("one", "one"),
        ("one", "two", "three", "four", "done"),
        ("one", "foreign"),
        ("one", "missing"),
        ("one", "done"),
    ],
)
async def test_creation_refuses_invalid_members(env, members):
    with pytest.raises(CollaborationError, match="member") as error:
        await create(env, task_ids=members)
    assert error.value.code == "collaboration.invalid"


@pytest.mark.parametrize(
    "values",
    [
        {"deadline_seconds": 59},
        {"deadline_seconds": 7201},
        {"deadline_seconds": float("nan")},
        {"message_budget": 0},
        {"message_budget": 41},
        {"goal": "x" * 1001},
    ],
)
async def test_creation_bounds(env, values):
    with pytest.raises(CollaborationError) as error:
        await create(env, **values)
    assert error.value.code == "collaboration.invalid"


async def test_creation_replay_conflict_and_invites(env):
    one, two = await asyncio.gather(create(env), create(env, task_ids=("two", "one")))
    assert one["id"] == two["id"]
    assert await count(env, one, "collaboration_invite") == 2
    with pytest.raises(CollaborationError) as error:
        await create(env, goal="Different")
    assert error.value.code == "collaboration.idempotency_conflict"
    # A replay remains valid after a member is terminal.
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "two").values(status="COMPLETED"))
    assert (await create(env))["id"] == one["id"]


async def test_concurrent_sequence_and_fanout(env, monkeypatch):
    monkeypatch.setattr("src.collaboration.MAX_SENDS_PER_MINUTE", 100)
    thread = await create(env, task_ids=("one", "two", "three", "four"))
    await accept(env, thread)
    await accept(env, thread, "two")
    results = await asyncio.gather(
        *[send(env, thread, sender="one" if i % 2 else "two", key=f"send-{i}") for i in range(12)]
    )
    assert sorted(result["seq"] for result in results) == list(range(1, 13))
    assert all(len(result["message_ids"]) == 3 for result in results)
    assert all(result["created_seq"] > 0 for result in results)
    assert await count(env, thread, "collaboration") == 36
    async with env.db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(collaboration_messages)) == 12
    current = await env.db.get_collaboration_thread(thread["id"], project_id="p")
    assert current["last_seq"] == current["message_count"] == 12


async def test_replay_does_not_spend_budget_and_returns_fanout_ids(env):
    thread = await create(env, task_ids=("one", "two", "three"), message_budget=1)
    await accept(env, thread)
    first = await send(env, thread)
    replay = await send(env, thread, body="Changed retry body")
    assert replay["replayed"] is True
    for key in ("seq", "message_id", "message_ids", "created_seq"):
        assert replay[key] == first[key]
    assert replay["thread"]["message_count"] == 1


async def test_fortieth_message_closes_once(env, monkeypatch):
    monkeypatch.setattr("src.collaboration.MAX_SENDS_PER_MINUTE", 100)
    thread = await create(env)
    await accept(env, thread)
    for i in range(40):
        result = await send(env, thread, key=str(i))
    assert result["thread"]["state"] == "closed"
    assert result["thread"]["close_reason"] == "budget_exhausted"
    assert result["thread"]["final_result"]["last_seq"] == 40
    with pytest.raises(CollaborationError) as error:
        await send(env, thread, key="41")
    assert error.value.code == "collaboration.closed"
    again = await env.db.close_collaboration_thread(
        thread_id=thread["id"], project_id="p", reason="closed", note="Again", now=NOW + 2
    )
    assert again["final_result"] == result["thread"]["final_result"]
    assert await count(env, thread, "collaboration_closed") == 2


async def test_acceptance_rate_bytes_and_target_refusals(env):
    thread = await create(env)
    with pytest.raises(CollaborationError) as error:
        await send(env, thread)
    assert error.value.code == "collaboration.not_accepted"
    await accept(env, thread)
    for body in ("x" * 4097, "é" * 2049):
        with pytest.raises(CollaborationError) as error:
            await send(env, thread, body=body)
        assert error.value.code == "collaboration.message_too_large"
    for target in ("one", "foreign", "missing"):
        with pytest.raises(CollaborationError):
            await send(env, thread, to_task_id=target)
    for i in range(5):
        await send(env, thread, key=str(i), body="é" * 2048, now=NOW + i)
    with pytest.raises(CollaborationError) as error:
        await send(env, thread, key="six", now=NOW + 5)
    assert error.value.code == "collaboration.rate_limited"
    assert error.value.retry_after == 55
    await send(env, thread, key="later", now=NOW + 60)


async def test_claim_rollover_and_idempotent_accept(env):
    thread = await create(env)
    first = await accept(env, thread)
    again = await accept(env, thread, now=NOW + 10)
    assert first["members"] == again["members"]
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "one").values(claim_epoch=2))
        await conn.execute(
            update(sessions).where(sessions.c.id == "s-one").values(last_claim_epoch=2)
        )
    with pytest.raises(CollaborationError) as error:
        await send(env, thread)
    assert error.value.code == "collaboration.stale_claim"
    await accept(env, thread, epoch=2)
    with pytest.raises(CollaborationError):
        await send(env, thread, epoch=1)
    await send(env, thread, epoch=2)


@pytest.mark.parametrize("operation", ["send", "accept"])
async def test_expiry_is_committed_before_refusal(env, operation):
    thread = await create(env, deadline_seconds=60)
    await accept(env, thread)
    with pytest.raises(CollaborationError) as error:
        if operation == "send":
            await send(env, thread, now=NOW + 60)
        else:
            await accept(env, thread, now=NOW + 60)
    assert error.value.code == "collaboration.closed"
    current = await env.db.get_collaboration_thread(thread["id"], project_id="p")
    assert current["state"] == "expired"
    assert await count(env, thread, "collaboration_closed") == 2


async def test_targeted_send_removed_member_and_close(env):
    thread = await create(env, task_ids=("one", "two", "three"))
    await accept(env, thread)
    result = await send(env, thread, to_task_id="two")
    assert len(result["message_ids"]) == 1
    await env.db.remove_collaboration_member(
        thread_id=thread["id"], project_id="p", task_id="three", now=NOW + 2
    )
    result = await send(env, thread, key="next")
    assert len(result["message_ids"]) == 1
    with pytest.raises(CollaborationError):
        await accept(env, thread, "three")
    await env.db.remove_collaboration_member(
        thread_id=thread["id"], project_id="p", task_id="two", now=NOW + 3
    )
    current = await env.db.get_collaboration_thread(thread["id"], project_id="p")
    assert current["close_reason"] == "members_below_two"
    assert await count(env, thread, "collaboration_closed") == 1


async def test_reads_are_scoped_ordered_and_bounded(env, monkeypatch):
    monkeypatch.setattr("src.collaboration.MAX_SENDS_PER_MINUTE", 100)
    thread = await create(env)
    await accept(env, thread)
    for i in range(25):
        await send(env, thread, key=str(i), body="é" * 2048)
    db = env.db
    tail = await db.read_collaboration_messages(thread["id"], project_id="p", limit=100)
    assert [m["seq"] for m in tail["messages"]] == list(range(18, 26))
    assert tail["next_cursor"] == 25
    page = await db.read_collaboration_messages(thread["id"], project_id="p", after_seq=0)
    assert [m["seq"] for m in page["messages"]] == list(range(1, 9))
    assert page["next_cursor"] == 8 and page["has_more"]
    small = await db.read_collaboration_messages(
        thread["id"], project_id="p", after_seq=8, limit=2, max_bytes=8192
    )
    assert [m["seq"] for m in small["messages"]] == [9, 10]
    empty = await db.read_collaboration_messages(thread["id"], project_id="p", after_seq=25)
    assert empty == {"messages": [], "next_cursor": 25, "has_more": False}
    assert await db.get_collaboration_thread(thread["id"], project_id="q") is None
    assert await db.read_collaboration_messages(thread["id"], project_id="q") is None
    assert await db.list_collaboration_threads(project_id="q") == []
    assert len(await db.list_collaboration_threads(project_id="p", task_id="one")) == 1
    assert await db.list_collaboration_threads(project_id="p", task_id="foreign") == []
    for op in (
        db.accept_collaboration(
            thread_id=thread["id"], project_id="q", task_id="one", claim_epoch=1, now=NOW
        ),
        db.close_collaboration_thread(
            thread_id=thread["id"], project_id="q", reason="closed", now=NOW
        ),
    ):
        with pytest.raises(CollaborationError) as error:
            await op
        assert error.value.code == "collaboration.not_found"


async def test_read_limit_of_twenty_and_task_liveness(env, monkeypatch):
    monkeypatch.setattr("src.collaboration.MAX_SENDS_PER_MINUTE", 100)
    thread = await create(env)
    await accept(env, thread)
    for i in range(25):
        await send(env, thread, key=str(i))
    page = await env.db.read_collaboration_messages(thread["id"], project_id="p", after_seq=0)
    assert len(page["messages"]) == 20 and page["next_cursor"] == 20
    async with env.db._engine.begin() as conn:
        # The member is a soft reference; read metadata survives archive and deletion.
        await conn.execute(
            insert(archived_tasks).values(
                id="two",
                project_id="p",
                title="two",
                description="",
                status="COMPLETED",
                archived_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await conn.execute(delete(sessions).where(sessions.c.task_id == "two"))
        await conn.execute(delete(tasks).where(tasks.c.id == "two"))
    current = await env.db.get_collaboration_thread(thread["id"], project_id="p")
    members = {member["task_id"]: member for member in current["members"]}
    assert members["one"]["running"] is True
    assert members["two"]["task_status"] == "ARCHIVED"
    assert members["two"]["running"] is False


async def test_small_read_never_advances_past_unseen_message(env):
    thread = await create(env)
    await accept(env, thread)
    await send(env, thread, body="x" * 100)
    page = await env.db.read_collaboration_messages(
        thread["id"], project_id="p", after_seq=0, max_bytes=99
    )
    assert page == {"messages": [], "next_cursor": 0, "has_more": True}
    page = await env.db.read_collaboration_messages(
        thread["id"], project_id="p", after_seq=page["next_cursor"]
    )
    assert [row["seq"] for row in page["messages"]] == [1]


async def test_failed_delivery_rolls_back_sequence_and_budget(env):
    from sqlalchemy.exc import IntegrityError

    thread = await create(env)
    await accept(env, thread)
    with pytest.raises(IntegrityError):
        await send(env, thread, reply_to_id="missing-message")
    current = await env.db.get_collaboration_thread(thread["id"], project_id="p")
    assert current["last_seq"] == current["message_count"] == 0
    result = await send(env, thread)
    assert result["seq"] == 1 and not result["replayed"]


async def test_concurrent_replay_and_close_remain_single_writes(env):
    thread = await create(env)
    await accept(env, thread)
    replies = await asyncio.gather(*[send(env, thread) for _ in range(5)])
    assert {reply["seq"] for reply in replies} == {1}
    assert sum(not reply["replayed"] for reply in replies) == 1
    await asyncio.gather(
        *[
            env.db.close_collaboration_thread(
                thread_id=thread["id"],
                project_id="p",
                reason="closed",
                now=NOW + 2,
            )
            for _ in range(5)
        ]
    )
    assert await count(env, thread, "collaboration_closed") == 2
    assert await count(env, thread, "collaboration") == 1


async def test_replay_is_refused_after_claim_rollover(env):
    thread = await create(env)
    await accept(env, thread)
    await send(env, thread)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "one").values(claim_epoch=2))
    with pytest.raises(CollaborationError) as error:
        await send(env, thread)
    assert error.value.code == "collaboration.stale_claim"


async def test_foreign_sender_and_session_cannot_append(env):
    thread = await create(env)
    await accept(env, thread)
    with pytest.raises(CollaborationError) as error:
        await send(env, thread, sender="foreign")
    assert error.value.code == "collaboration.not_member"
    with pytest.raises(CollaborationError) as error:
        await env.db.append_collaboration_message(
            thread_id=thread["id"],
            project_id="q",
            sender_task_id="foreign",
            sender_claim_epoch=1,
            sender_session_id="s-foreign",
            client_key=None,
            body="Guessed a thread",
            now=NOW,
        )
    assert error.value.code == "collaboration.not_found"
    async with env.db._engine.begin() as conn:
        await conn.execute(update(sessions).where(sessions.c.id == "s-one").values(task_id="two"))
    with pytest.raises(CollaborationError) as error:
        await send(env, thread)
    assert error.value.code == "collaboration.stale_claim"


async def test_expired_unaccepted_send_commits_closure(env):
    thread = await create(env, deadline_seconds=60)
    with pytest.raises(CollaborationError) as error:
        await send(env, thread, now=NOW + 60)
    assert error.value.code == "collaboration.closed"
    assert (await env.db.get_collaboration_thread(thread["id"], project_id="p"))[
        "state"
    ] == "expired"
    assert await count(env, thread, "collaboration_closed") == 2
