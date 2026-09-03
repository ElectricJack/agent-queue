"""Findings and authored comments use real persistence and ownership fences."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.api.auth import RequestScope
from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.database import Database
from src.models import Agent, AgentProfile, Project, SessionRecord, Task, TaskStatus
from src.orchestrator import Orchestrator
from tests.pg_dsn import ensure_worker_postgres_dsn

pytestmark = pytest.mark.asyncio
POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()


@pytest.fixture(params=["sqlite", "postgres"])
async def env(tmp_path, request):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        db = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
        await db.initialize()
        await db.reset_for_tests()
    else:
        db = Database(str(tmp_path / "comments.db"))
        await db.initialize()
    for pid in ("p", "other"):
        await db.create_project(Project(id=pid, name=pid))
    await db.create_profile(AgentProfile(id="worker", name="Worker", needs_workspace=False))
    await db.create_agent(Agent(id="agent", name="Worker", profile_id="worker"))
    for tid, pid in (("t", "p"), ("peer", "p"), ("foreign", "other")):
        await db.create_task(
            Task(
                id=tid,
                project_id=pid,
                title=tid,
                description="Original requirements",
                status=TaskStatus.IN_PROGRESS,
                assigned_agent_id="agent" if tid == "t" else None,
            )
        )
    await db.update_task("t", claim_epoch=7)
    await db.create_session(
        SessionRecord(
            id="session",
            task_id="t",
            project_id="p",
            agent_id="agent",
            profile_id="worker",
            harness="codex",
            provider="fake",
            name="worker-session",
            lifecycle="pool",
            state="running",
            work_dir=str(tmp_path),
            epoch="test",
            instance_token="instance",
            started_at=time.time(),
        )
    )
    config = AppConfig(data_dir=str(tmp_path / "data"), workspace_dir=str(tmp_path / "ws"))
    orch = Orchestrator(config)
    orch.db = db
    orch.bus.emit = AsyncMock()
    handler = CommandHandler(orch, config)
    scope = RequestScope(kind="session", session_id="session", project_id="p")
    yield SimpleNamespace(db=db, handler=handler, scope=scope, orch=orch)
    await db.close()


async def run(env, command, args=None, *, scope=None):
    args = dict(args or {})
    if scope is not None:
        args["_scope"] = asdict(scope)
    return await env.handler.execute(command, args)


async def test_description_cas_preserves_fields_and_rejects_all_stale_side_effects(env):
    await env.db.add_task_context("t", type="note", label="note", content="Legacy note")
    result = await run(
        env,
        "task_set",
        {
            "task_id": "t",
            "description": "Original requirements\nFindings: verified",
            "expected_description": "Original requirements",
            "branch": "findings",
            "claim_epoch": 7,
        },
        scope=env.scope,
    )
    assert "error" not in result, result
    assert "description" in result["fields_changed"]
    before = await env.db.get_task("t")
    rejected = await run(
        env,
        "task_set",
        {
            "task_id": "t",
            "description": "Stale replacement",
            "expected_description": "Original requirements",
            "branch": "bad",
            "note": "bad",
            "labels_add": ["bad"],
            "meta": {"bad": "bad"},
            "claim_epoch": 7,
        },
        scope=env.scope,
    )
    assert rejected.get("error_code") == "description_conflict", rejected
    after = await env.db.get_task("t")
    assert after.description == before.description
    assert after.branch_name == "findings"
    assert after.updated_at == before.updated_at
    assert [c["content"] for c in await env.db.get_task_contexts("t")] == ["Legacy note"]
    assert await env.db.get_task_labels("t") == []
    assert await env.db.get_task_meta("t", "bad") is None


async def test_comments_are_authored_append_only_paginated_and_deleted_with_task(env):
    first = await run(env, "task_comment", {"task_id": "t", "body": "Human finding"})
    assert "error" not in first, first
    second = await run(
        env,
        "task_comment",
        {
            "task_id": "t",
            "body": "Worker finding",
            "claim_epoch": 7,
        },
        scope=env.scope,
    )
    assert "error" not in second, second
    assert first["comment"]["author_kind"] == "user"
    assert first["comment"]["author_id"] == "local"
    assert second["comment"]["author_kind"] == "agent"
    assert second["comment"]["author_id"] == "agent"
    assert second["comment"]["created_at"] >= first["comment"]["created_at"]
    page = await run(env, "task_comments", {"task_id": "t", "limit": 1, "offset": 1})
    assert page == {"comments": [first["comment"]], "total": 2, "limit": 1, "offset": 1}
    events = [
        call.args for call in env.orch.bus.emit.await_args_list if call.args[0] == "task.updated"
    ]
    assert len(events) == 2
    assert all(set(payload) <= {"task_id", "project_id", "seq"} for _, payload in events)
    await env.db.delete_task("t")
    assert (await env.db.list_task_comments("t"))["total"] == 0


@pytest.mark.parametrize("description,expected", [(42, None), (None, None), ("valid", 42)])
async def test_description_validation_precedes_all_mutations(env, description, expected):
    args = {"task_id": "t", "description": description, "branch": "bad"}
    if expected is not None:
        args["expected_description"] = expected
    result = await run(env, "task_set", args)
    assert "error" in result
    assert (await env.db.get_task("t")).branch_name is None


@pytest.mark.parametrize("body", ["", "  \n", "x" * 16001, 123, None])
async def test_comment_body_validation(env, body):
    result = await run(env, "task_comment", {"task_id": "t", "body": body})
    assert "error" in result
    assert (await run(env, "task_comments", {"task_id": "t"}))["total"] == 0


async def test_concurrent_description_cas_has_exactly_one_winner(env):
    results = await asyncio.gather(
        *[
            run(
                env,
                "task_set",
                {
                    "task_id": "t",
                    "description": value,
                    "expected_description": "Original requirements",
                },
            )
            for value in ("Findings A", "Findings B")
        ]
    )
    assert sum("error" not in result for result in results) == 1, results
    assert sum(result.get("error_code") == "description_conflict" for result in results) == 1
    assert (await env.db.get_task("t")).description in {"Findings A", "Findings B"}


async def test_claim_and_project_fences(env):
    for command, extra in (("task_comment", {"body": "No"}), ("task_set", {"description": "No"})):
        for task_id, epoch in (("t", None), ("t", 6), ("peer", 7), ("foreign", 7)):
            args = {"task_id": task_id, **extra}
            if epoch is not None:
                args["claim_epoch"] = epoch
            assert "error" in await run(env, command, args, scope=env.scope)
    supervisor = RequestScope(
        kind="session", session_id="supervisor", project_id="p", elevated=True
    )
    for command, extra in (
        ("task_comments", {}),
        ("task_comment", {"body": "No"}),
        ("task_set", {"description": "No"}),
    ):
        assert "error" in await run(env, command, {"task_id": "foreign", **extra}, scope=supervisor)
    assert "error" in await run(env, "task_comments", {"task_id": "foreign"}, scope=env.scope)
    assert (await env.db.get_task("t")).description == "Original requirements"


async def test_valid_comment_boundaries_concurrent_append_and_no_gate_effects(env):
    gate_id, _ = await env.db.create_gate(
        project_id="p",
        gate_type="human",
        title="Approval",
        waiter_task_ids=["t"],
    )
    values = ["x" * 16000] + [f"finding {i}" for i in range(8)]
    results = await asyncio.gather(
        *[run(env, "task_comment", {"task_id": "t", "body": value}) for value in values]
    )
    assert all("comment" in result for result in results), results
    page = await env.db.list_task_comments("t")
    assert page["total"] == len(values)
    assert {c["body"] for c in page["comments"]} == set(values)
    assert len({c["id"] for c in page["comments"]}) == len(values)
    assert (await env.db.get_gate(gate_id))["status"] == "open"
    assert (await env.db.get_task("t")).status == TaskStatus.IN_PROGRESS


@pytest.mark.parametrize(
    "args", [{"limit": 0}, {"limit": 201}, {"offset": -1}, {"limit": True}, {"offset": "0"}]
)
async def test_comment_pagination_validation(env, args):
    assert "error" in await run(env, "task_comments", {"task_id": "t", **args})


async def test_forged_author_rejected_and_supervisor_author_derived(env):
    forged = await run(
        env,
        "task_comment",
        {
            "task_id": "t",
            "body": "Forged",
            "author_kind": "user",
            "author_id": "local",
            "created_at": 0,
            "claim_epoch": 7,
        },
        scope=env.scope,
    )
    assert "error" in forged
    supervisor = RequestScope(
        kind="session", session_id="supervisor", project_id="p", elevated=True
    )
    result = await run(
        env, "task_comment", {"task_id": "peer", "body": "Supervisor finding"}, scope=supervisor
    )
    assert result["comment"]["author_kind"] == "supervisor"
    assert result["comment"]["author_id"] == "supervisor"
    assert (
        await run(env, "task_set", {"task_id": "peer", "description": "Updated"}, scope=supervisor)
    )["description"] == "Updated"


@pytest.mark.parametrize("command", ["task_comment", "task_set"])
async def test_reclaimed_between_authorization_and_write_is_fenced(env, monkeypatch, command):
    original = env.handler._task_findings_write_fence

    async def reclaimed(task, args):
        result = await original(task, args)
        await env.db.update_task("t", claim_epoch=8)
        return result

    monkeypatch.setattr(env.handler, "_task_findings_write_fence", reclaimed)
    extra = (
        {"body": "Too late"}
        if command == "task_comment"
        else {"description": "Too late", "note": "Too late"}
    )
    result = await run(env, command, {"task_id": "t", "claim_epoch": 7, **extra}, scope=env.scope)
    assert result.get("error_code") == "stale_claim", result
    assert (await env.db.get_task("t")).description == "Original requirements"
    assert (await env.db.list_task_comments("t"))["total"] == 0
    assert await env.db.get_task_contexts("t") == []


async def test_task_pinned_reads_and_stopped_sessions_are_fenced(env):
    pinned = RequestScope(kind="session", session_id="session", project_id="p", task_id="t")
    assert "error" in await run(env, "task_comments", {"task_id": "peer"}, scope=pinned)
    await env.db.update_session("session", state="stopped")
    assert "error" in await run(
        env, "task_comment", {"task_id": "t", "body": "Late", "claim_epoch": 7}, scope=env.scope
    )


async def test_empty_description_and_expected_description_without_write(env):
    invalid = await run(
        env,
        "task_set",
        {"task_id": "t", "expected_description": "Original requirements", "branch": "bad"},
    )
    assert "error" in invalid
    assert (await env.db.get_task("t")).branch_name is None
    result = await run(env, "task_set", {"task_id": "t", "description": ""})
    assert result["description"] == ""
    result = await run(
        env, "task_set", {"task_id": "t", "description": "Restored", "expected_description": ""}
    )
    assert result["description"] == "Restored"


async def test_findings_text_never_enters_invalidation_or_command_bus(env):
    import json

    description = "private description finding"
    expected = "Original requirements"
    comment = "private comment finding"
    assert "error" not in await run(
        env,
        "task_set",
        {
            "task_id": "t",
            "description": description,
            "expected_description": expected,
        },
    )
    assert "error" not in await run(env, "task_comment", {"task_id": "t", "body": comment})
    serialized = json.dumps([call.args for call in env.orch.bus.emit.await_args_list], default=str)
    assert description not in serialized
    assert expected not in serialized
    assert comment not in serialized


async def test_comments_survive_archive_recreation_and_archive_snapshot_cleanup(env):
    result = await run(env, "task_comment", {"task_id": "peer", "body": "Durable history"})
    original = result["comment"]
    await env.db.update_task("peer", status=TaskStatus.COMPLETED)
    assert await env.db.archive_task("peer")
    assert await env.db.get_task("peer") is None
    page = await run(env, "task_comments", {"task_id": "peer"}, scope=env.scope)
    assert page["comments"] == [original]
    foreign_scope = RequestScope(
        kind="session", session_id="foreign", project_id="other", elevated=True
    )
    assert "error" in await run(env, "task_comments", {"task_id": "peer"}, scope=foreign_scope)
    assert "error" in await run(
        env, "task_comment", {"task_id": "peer", "body": "No archived mutation"}
    )
    await env.db.create_task(
        Task(id="peer", project_id="p", title="Restored", description="Restored")
    )
    assert await env.db.delete_archived_task("peer")
    assert (await run(env, "task_comments", {"task_id": "peer"}))["comments"] == [original]
    await env.db.delete_task("peer")
    assert (await env.db.list_task_comments("peer"))["total"] == 0


async def test_archived_comment_identity_cannot_be_reused_in_another_project(env):
    await run(env, "task_comment", {"task_id": "peer", "body": "Project p history"})
    await env.db.update_task("peer", status=TaskStatus.COMPLETED)
    await env.db.archive_task("peer")
    with pytest.raises(ValueError, match="archived.*project"):
        await env.db.create_task(
            Task(id="peer", project_id="other", title="Wrong project", description="")
        )
    assert await env.db.get_task("peer") is None
    assert (await env.db.list_task_comments("peer"))["total"] == 1


async def test_permanent_archive_and_project_deletion_clean_history(env):
    await run(env, "task_comment", {"task_id": "peer", "body": "Archive delete"})
    await env.db.update_task("peer", status=TaskStatus.COMPLETED)
    await env.db.archive_task("peer")
    assert await env.db.delete_archived_task("peer")
    assert (await env.db.list_task_comments("peer"))["total"] == 0
    await env.db.create_project(Project(id="disposable", name="Disposable"))
    for tid in ("active-disposable", "archived-disposable"):
        await env.db.create_task(Task(id=tid, project_id="disposable", title=tid, description=""))
        await run(env, "task_comment", {"task_id": tid, "body": tid})
    await env.db.update_task("archived-disposable", status=TaskStatus.COMPLETED)
    await env.db.archive_task("archived-disposable")
    await env.db.delete_project("disposable")
    for tid in ("active-disposable", "archived-disposable"):
        assert (await env.db.list_task_comments(tid))["total"] == 0


async def test_generated_task_ids_reserve_archived_roots_and_children(env, monkeypatch):
    from src import task_names

    for tid in ("swift-falcon", "peer.1"):
        await env.db.create_task(Task(id=tid, project_id="p", title=tid, description=""))
        await env.db.update_task(tid, status=TaskStatus.COMPLETED)
        await env.db.archive_task(tid)
    choices = iter(["swift", "falcon", "bright", "horizon"])
    monkeypatch.setattr(task_names.random, "choice", lambda values: next(choices))
    async with env.db._engine.begin() as conn:
        assert await task_names.fresh_root_id(conn) == "bright-horizon"
        assert await task_names.child_task_id(conn, "peer") == ("peer.2", False)


async def seed_cross_project_collision(env):
    from sqlalchemy import insert
    from src.database.tables import tasks

    await env.db.create_project(Project(id="comment-archive", name="Comment archive"))
    await env.db.update_task("peer", project_id="comment-archive")
    original = await run(env, "task_comment", {"task_id": "peer", "body": "Archived project secret"})
    await env.db.update_task("peer", status=TaskStatus.COMPLETED)
    await env.db.archive_task("peer")
    # Legacy allocation allowed a different project to reuse archived IDs.
    async with env.db._engine.begin() as conn:
        await conn.execute(insert(tasks).values(
            id="peer", project_id="other", title="Collision", description="Active requirements",
            status="READY", created_at=time.time(), updated_at=time.time(),
        ))
    return original["comment"]


async def test_existing_collision_supports_comments_and_description_without_leaking(env):
    original = await seed_cross_project_collision(env)
    page = await run(env, "task_comments", {"task_id": "peer"})
    assert page == {"comments": [], "total": 0, "limit": 50, "offset": 0}
    added = await run(env, "task_comment", {"task_id": "peer", "body": "Active project feedback"})
    assert "error" not in added, added
    updated = await run(env, "task_set", {
        "task_id": "peer", "description": "Corrected requirements",
        "expected_description": "Active requirements",
    })
    assert "error" not in updated, updated
    page = await run(env, "task_comments", {"task_id": "peer"})
    assert page["comments"] == [added["comment"]]
    assert page["total"] == 1
    assert "error" in await run(env, "task_comments", {"task_id": "peer"}, scope=env.scope)
    assert (await env.db.get_archived_task("peer"))["description"] == "Original requirements"
    from sqlalchemy import select
    from src.database.tables import task_comments
    async with env.db._engine.connect() as conn:
        assert (await conn.execute(select(task_comments.c.body).where(
            task_comments.c.id == original["id"]))).scalar_one() == "Archived project secret"


@pytest.mark.parametrize("operation", ["active", "archive", "active_project", "archive_project"])
async def test_collision_cleanup_preserves_other_projects_comments(env, operation):
    original = await seed_cross_project_collision(env)
    result = await run(env, "task_comment", {"task_id": "peer", "body": "Active feedback"})
    assert "error" not in result, result
    active = result["comment"]
    if operation == "active":
        await env.db.delete_task("peer")
    elif operation == "archive":
        await env.db.delete_archived_task("peer")
    elif operation == "active_project":
        await env.db.delete_project("other")
    else:
        await env.db.delete_project("comment-archive")
    from sqlalchemy import select
    from src.database.tables import task_comments
    async with env.db._engine.connect() as conn:
        remaining = (await conn.execute(select(task_comments.c.id).where(
            task_comments.c.task_id == "peer"))).scalars().all()
    assert remaining == [original["id"] if operation in {"active", "active_project"} else active["id"]]


@pytest.mark.parametrize("operation", ["delete", "archive"])
async def test_append_race_with_task_removal_is_lossless_or_clean(env, monkeypatch, operation):
    from contextlib import asynccontextmanager
    from sqlalchemy import event

    if operation == "archive":
        await env.db.update_task("peer", status=TaskStatus.COMPLETED)
    entered = asyncio.Event()
    release = asyncio.Event()
    removal_started = asyncio.Event()
    original = env.db._write_task_findings
    immediate = env.db.immediate

    @asynccontextmanager
    async def observe_immediate():
        # SQLite now blocks at BEGIN IMMEDIATE (or its in-process lock),
        # before archive emits its first task UPDATE.
        if operation == "archive" and entered.is_set():
            removal_started.set()
        async with immediate() as conn:
            yield conn

    if env.db._engine.dialect.name == "sqlite":
        monkeypatch.setattr(env.db, "immediate", observe_immediate)

    async def hold_append(*args, **kwargs):
        await original(*args, **kwargs)
        entered.set()
        await release.wait()

    monkeypatch.setattr(env.db, "_write_task_findings", hold_append)

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if not entered.is_set():
            return
        if operation == "archive":
            match = statement.startswith("SELECT tasks.id, tasks.status") and "FOR UPDATE" in statement
        elif env.db._engine.dialect.name == "postgresql":
            # Pending-pause deletion safety locks the task before FK cleanup.
            match = statement.startswith("SELECT tasks.id") and "FOR UPDATE" in statement
        else:
            # On SQLite the delete path's first write is the layout dirty-mark,
            # which takes the file's write lock ahead of the task-lock UPDATE
            # in ``_assert_pause_cleanup_complete`` — so the UPDATE is never
            # reached while the append holds BEGIN IMMEDIATE.  Either statement
            # means the removal has started and is blocked behind the append.
            match = statement.startswith(
                ("UPDATE tasks SET id=tasks.id", "INSERT INTO layout_dirty")
            )
        if match:
            removal_started.set()

    event.listen(env.db._engine.sync_engine, "before_cursor_execute", before_execute)
    try:
        append = asyncio.create_task(
            run(env, "task_comment", {"task_id": "peer", "body": "Concurrent history"})
        )
        await asyncio.wait_for(entered.wait(), 10)
        remove = asyncio.create_task(
            env.db.archive_task("peer") if operation == "archive" else env.db.delete_task("peer")
        )
        await asyncio.wait_for(removal_started.wait(), 10)
        assert not remove.done()
        release.set()
        result, _ = await asyncio.gather(append, remove)
        assert "comment" in result, result
        page = await env.db.list_task_comments("peer")
        assert page["comments"] == ([result["comment"]] if operation == "archive" else [])
    finally:
        release.set()
        event.remove(env.db._engine.sync_engine, "before_cursor_execute", before_execute)


async def test_comment_read_fences_deleted_id_recreated_in_another_project(env, monkeypatch):
    original = env.db.list_task_comments

    async def replace_identity(task_id, **kwargs):
        await env.db.delete_task("peer")
        await env.db.create_task(
            Task(id="peer", project_id="other", title="New identity", description="")
        )
        await env.db.add_task_comment(
            "peer", "Other project secret", author_kind="user", author_id="local"
        )
        return await original(task_id, **kwargs)

    monkeypatch.setattr(env.db, "list_task_comments", replace_identity)
    result = await run(env, "task_comments", {"task_id": "peer"}, scope=env.scope)
    assert result["comments"] == []
    assert result["total"] == 0


async def test_project_delete_serializes_task_creation_and_comment_cleanup(env):
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError

    await env.db.create_project(Project(id="delete-race", name="Delete race"))
    pending = []

    async def create_late():
        try:
            await env.db.create_task(
                Task(id="late-created", project_id="delete-race", title="Late", description="")
            )
            await env.db.add_task_comment(
                "late-created", "Late comment", author_kind="user", author_id="local"
            )
        except IntegrityError:
            pass  # Project deletion won; the new task cannot outlive it.

    def after_execute(conn, cursor, statement, parameters, context, executemany):
        if not pending and statement.startswith("SELECT tasks.id"):
            pending.append(asyncio.create_task(create_late()))

    event.listen(env.db._engine.sync_engine, "after_cursor_execute", after_execute)
    try:
        await env.db.delete_project("delete-race")
        assert pending
        await asyncio.gather(*pending)
        assert await env.db.get_task("late-created") is None
        assert (await env.db.list_task_comments("late-created"))["comments"] == []
    finally:
        event.remove(env.db._engine.sync_engine, "after_cursor_execute", after_execute)


async def test_collision_archive_refuses_to_discard_active_identity(env):
    from src.database.queries.hierarchy_queries import HierarchyError

    await seed_cross_project_collision(env)
    added = await run(env, "task_comment", {"task_id": "peer", "body": "Keep active history"})
    assert "error" not in added, added
    await env.db.update_task("peer", status=TaskStatus.COMPLETED)
    before = await env.db.get_archived_task("peer")
    with pytest.raises(HierarchyError, match="archive_identity_conflict"):
        await env.db.archive_task("peer")
    assert (await env.db.get_task("peer")).project_id == "other"
    assert await env.db.get_archived_task("peer") == before
    assert (await run(env, "task_comments", {"task_id": "peer"}))["comments"] == [added["comment"]]


async def test_unknown_legacy_comments_stay_hidden_after_collision_removed(env):
    from sqlalchemy import insert
    from src.database.tables import task_comments

    await seed_cross_project_collision(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(insert(task_comments).values(
            id="unknown-history", task_id="peer", project_id=None, body="Uncertain owner",
            author_kind="user", author_id="local", created_at=1,
        ))
    assert (await env.db.list_task_comments("peer"))["comments"] == []
    await env.db.delete_archived_task("peer")
    assert (await env.db.list_task_comments("peer", project_id="other"))["comments"] == []
    from sqlalchemy import select
    async with env.db._engine.connect() as conn:
        assert (await conn.execute(select(task_comments.c.body).where(
            task_comments.c.id == "unknown-history"))).scalar_one() == "Uncertain owner"


async def test_comments_follow_authorized_project_move(env):
    await env.db.create_project(Project(id="comment-source", name="Source"))
    await env.db.update_task("peer", project_id="comment-source")
    first = await run(env, "task_comment", {"task_id": "peer", "body": "Keep on move"})
    moved = await run(env, "edit_task", {"task_id": "peer", "project_id": "other"})
    assert "error" not in moved, moved
    assert (await run(env, "task_comments", {"task_id": "peer"}))["comments"] == [first["comment"]]
    assert (await env.db.list_task_comments("peer", project_id="comment-source"))["total"] == 0
    await env.db.delete_project("comment-source")
    assert (await run(env, "task_comments", {"task_id": "peer"}))["comments"] == [first["comment"]]


async def test_collision_move_keeps_archive_and_unknown_comments_unchanged(env):
    from sqlalchemy import insert, select
    from src.database.tables import task_comments

    old = await seed_cross_project_collision(env)
    active = await run(env, "task_comment", {"task_id": "peer", "body": "Move active only"})
    async with env.db._engine.begin() as conn:
        await conn.execute(insert(task_comments).values(
            id="unknown", task_id="peer", body="Unknown", author_kind="user", author_id="local", created_at=1,
        ))
    moved = await run(env, "edit_task", {"task_id": "peer", "project_id": "p"})
    assert "error" not in moved, moved
    assert (await run(env, "task_comments", {"task_id": "peer"}))["comments"] == [active["comment"]]
    async with env.db._engine.connect() as conn:
        ownership = dict((await conn.execute(select(task_comments.c.id, task_comments.c.project_id))).all())
    assert ownership == {old["id"]: "comment-archive", active["comment"]["id"]: "p", "unknown": None}


@pytest.mark.parametrize("archived_owner", ["source", "destination"])
async def test_project_move_refuses_merging_archived_comment_identity(env, archived_owner):
    if archived_owner == "source":
        old = await run(env, "task_comment", {"task_id": "peer", "body": "Shared snapshot"})
        await env.db.update_task("peer", status=TaskStatus.COMPLETED)
        await env.db.archive_task("peer")
        await env.db.create_task(Task(id="peer", project_id="p", title="Restored", description=""))
        destination = "other"
    else:
        await seed_cross_project_collision(env)
        old = await run(env, "task_comment", {"task_id": "peer", "body": "Active feedback"})
        destination = "comment-archive"
    before = await env.db.get_task("peer")
    moved = await run(env, "edit_task", {"task_id": "peer", "project_id": destination})
    assert "error" in moved, moved
    assert (await env.db.get_task("peer")).project_id == before.project_id
    assert (await run(env, "task_comments", {"task_id": "peer"}))["comments"] == [old["comment"]]
