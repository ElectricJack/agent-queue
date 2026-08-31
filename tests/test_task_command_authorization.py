"""End-to-end scope + destructive-operation tests through ``CommandHandler.execute``.

Commands 23–24 of the test-coverage plan: the representative *mutating*
boundary evidence that the definition-level scope matrix in
``tests/test_command_scope_matrix.py`` deliberately does not attempt.  Both
tests dispatch through the real ``execute()`` with a trusted ``_scope``
envelope and a real SQLite database.
"""

from __future__ import annotations

import time

from src.models import (
    Agent,
    Project,
    SessionRecord,
    Task,
    TaskStatus,
)


def _session(
    session_id: str,
    *,
    project_id: str,
    task_id: str | None,
    lifecycle: str = "pool",
    state: str = "running",
) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        project_id=project_id,
        profile_id="generic",
        harness="claude",
        provider="anthropic",
        name=f"n-{session_id}",
        lifecycle=lifecycle,
        work_dir="/tmp/ws",
        epoch="e1",
        instance_token=f"tok-{session_id}",
        started_at=time.time(),
        task_id=task_id,
        state=state,
    )


def _scope(session_id: str, project_id: str, task_id: str | None) -> dict:
    return {
        "kind": "session",
        "session_id": session_id,
        "project_id": project_id,
        "task_id": task_id,
        "elevated": False,
    }


# ---------------------------------------------------------------------------
# 23: pool-session reads and worker filings are fenced by project
# ---------------------------------------------------------------------------


async def test_execute_blocks_pool_session_reading_foreign_task_and_worker_filing_foreign_project(
    command_handler_factory,
):
    handler = await command_handler_factory()
    db = handler.db

    await db.create_project(Project(id="p1", name="One", repo_url=""))
    await db.create_project(Project(id="p2", name="Two", repo_url=""))
    await db.create_task(Task(id="t1", project_id="p1", title="own task", description=""))
    await db.create_task(Task(id="t2", project_id="p2", title="foreign task", description=""))
    # A pool worker's token pins no task_id — its claim changes on every pull.
    await db.create_session(_session("s1", project_id="p1", task_id="t1"))

    scope = _scope("s1", "p1", None)

    # Own project: readable.
    own = await handler.execute("task_show", {"task_id": "t1", "_scope": scope})
    assert "error" not in own, own
    assert own["id"] == "t1"

    # Foreign project: refused, with the out-of-scope claim result.
    foreign = await handler.execute("task_show", {"task_id": "t2", "_scope": scope})
    assert foreign["success"] is False
    assert foreign["result"] == "out_of_scope"
    assert "outside this session's scope" in foreign["error"]

    # A worker filing new work cannot aim it at another project — the filing
    # is pinned to (or rejected for) the session's own project.
    rejected = await handler.execute(
        "create_task",
        {"title": "sneaky", "description": "", "project_id": "p2", "_scope": scope},
    )
    assert rejected["success"] is False
    assert rejected["error"] == "worker-filed tasks are pinned to the session's project"
    assert [t.id for t in await db.list_tasks(project_id="p2")] == ["t2"]

    # Filing with the project omitted is pinned to p1 and starts DEFINED.
    filed = await handler.execute(
        "create_task",
        {"title": "discovered work", "description": "found it", "_scope": scope},
    )
    assert filed.get("error") is None, filed
    filed_id = filed.get("task_id") or filed.get("created") or filed.get("id")
    assert filed_id
    created = await db.get_task(filed_id)
    assert created.project_id == "p1"
    assert created.status == TaskStatus.DEFINED

    # An idle pool session (holding nothing) cannot file at all.
    await db.create_session(_session("s-idle", project_id="p1", task_id=None))
    idle = await handler.execute(
        "create_task",
        {"title": "from idle", "description": "", "_scope": _scope("s-idle", "p1", None)},
    )
    assert idle["success"] is False
    assert idle["code"] == "idle_session_cannot_file"


# ---------------------------------------------------------------------------
# 24: cascade delete refuses a live descendant session, atomically
# ---------------------------------------------------------------------------


async def test_delete_task_cascade_refuses_live_descendant_session_without_partial_delete(
    command_handler_factory,
):
    handler = await command_handler_factory()
    db = handler.db

    await db.create_project(Project(id="p1", name="One", repo_url=""))
    await db.create_agent(Agent(id="a1", name="a1", profile_id="generic"))
    await db.create_task(Task(id="parent", project_id="p1", title="parent", description=""))
    await db.create_task(Task(id="child", project_id="p1", title="child", description=""))
    async with db.immediate() as conn:
        await db.set_parent("child", "parent", conn=conn)
    # A live session holding the *child* — the grandchild case the guard exists for.
    await db.create_session(_session("s-live", project_id="p1", task_id="child", lifecycle="task"))

    result = await handler.execute("delete_task", {"task_id": "parent", "cascade": True})

    assert result["success"] is False
    assert result["code"] == "hierarchy.live_descendants"
    assert result["sessions"] == [{"session_id": "s-live", "task_id": "child"}]

    # Nothing was deleted — the refusal is all-or-nothing.
    assert await db.get_task("parent") is not None
    assert await db.get_task("child") is not None
    assert (await db.get_task("child")).parent_task_id == "parent"

    # Once the session is no longer live, the same cascade succeeds.
    await db.update_session("s-live", state="stopped")
    ok = await handler.execute("delete_task", {"task_id": "parent", "cascade": True})
    assert ok["deleted"] == "parent"
    assert await db.get_task("parent") is None
    assert await db.get_task("child") is None


async def test_delete_task_without_cascade_refuses_a_task_that_still_has_children(
    command_handler_factory,
):
    handler = await command_handler_factory()
    db = handler.db

    await db.create_project(Project(id="p1", name="One", repo_url=""))
    await db.create_task(Task(id="parent", project_id="p1", title="parent", description=""))
    await db.create_task(Task(id="child", project_id="p1", title="child", description=""))
    async with db.immediate() as conn:
        await db.set_parent("child", "parent", conn=conn)

    result = await handler.execute("delete_task", {"task_id": "parent"})

    assert result.get("code", "").startswith("hierarchy.")
    assert await db.get_task("parent") is not None
    assert await db.get_task("child") is not None

    assert await handler.execute("delete_task", {"task_id": "ghost"}) == {
        "error": "Task 'ghost' not found"
    }
