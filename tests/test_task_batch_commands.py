"""Batch propose/commit/update/discard + cycle detection.

Phase 6 Tasks 2 + 3 (design §8, spec ingestion).
"""
from __future__ import annotations

import pytest

from src.database.queries.proposal_queries import detect_cycles


# ---------------------------------------------------------------------------
# Cycle detection (Task 2)
# ---------------------------------------------------------------------------


def test_detect_cycles_none():
    tasks_in = [{"tempId": "t1"}, {"tempId": "t2"}]
    edges = [{"from": "t1", "to": "t2", "dep_type": "blocks"}]
    assert detect_cycles([], tasks_in, edges) == []


def test_detect_cycles_within_proposal():
    tasks_in = [{"tempId": "t1"}, {"tempId": "t2"}]
    edges = [
        {"from": "t1", "to": "t2", "dep_type": "blocks"},
        {"from": "t2", "to": "t1", "dep_type": "blocks"},
    ]
    cycles = detect_cycles([], tasks_in, edges)
    assert len(cycles) == 1
    assert set(cycles[0]) == {"t1", "t2"}


def test_detect_cycles_across_existing_and_proposal():
    # existing graph: A depends-on B (task_dependencies row (A, B, 'blocks'))
    existing = [("A", "B", "blocks")]
    tasks_in = [{"tempId": "t1"}]
    edges = [
        {"from": "B", "to": "t1", "dep_type": "blocks"},   # B depends on t1
        {"from": "t1", "to": "A", "dep_type": "blocks"},   # t1 depends on A
    ]
    # Chain: A→B→t1→A.
    cycles = detect_cycles(existing, tasks_in, edges)
    assert len(cycles) == 1
    assert set(cycles[0]) == {"A", "B", "t1"}


# ---------------------------------------------------------------------------
# Command tests (Task 3) — exercise the full CommandHandler surface.
# ---------------------------------------------------------------------------


@pytest.fixture
async def handler(command_handler_factory):
    h = await command_handler_factory()
    yield h
    if hasattr(h, "_db") and h._db is not None:
        await h._db.close()


def _emitted(h) -> list[tuple[str, dict]]:
    """Return list of (event_type, payload) emitted through the AsyncMock bus."""
    calls = h.orchestrator.bus.emit.call_args_list
    out: list[tuple[str, dict]] = []
    for c in calls:
        args, kwargs = c
        if args:
            evt = args[0]
            payload = args[1] if len(args) > 1 else kwargs.get("payload", {})
        else:
            evt = kwargs.get("event_type") or kwargs.get("name")
            payload = kwargs.get("payload", {})
        out.append((evt, payload))
    return out


async def test_propose_rejects_cycle(handler):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    r = await handler.execute(
        "task_batch_propose",
        {
            "project_id": "p1",
            "source": "spec:foo",
            "tasks": [
                {"tempId": "a", "title": "A", "description": ""},
                {"tempId": "b", "title": "B", "description": ""},
            ],
            "edges": [
                {"from": "a", "to": "b", "dep_type": "blocks"},
                {"from": "b", "to": "a", "dep_type": "blocks"},
            ],
        },
    )
    assert r["success"] is False
    assert "cycle" in r["error"].lower()


async def test_propose_rejects_unknown_existing_task_ref(handler):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    r = await handler.execute(
        "task_batch_propose",
        {
            "project_id": "p1",
            "source": "spec:foo",
            "tasks": [{"tempId": "a", "title": "A", "description": ""}],
            "edges": [
                {
                    "from": "a",
                    "to": "nonexistent-task-id",
                    "dep_type": "blocks",
                }
            ],
        },
    )
    assert r["success"] is False
    assert "unknown" in r["error"].lower()


async def test_propose_ready_emits_event(handler):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    r = await handler.execute(
        "task_batch_propose",
        {
            "project_id": "p1",
            "source": "spec:foo",
            "tasks": [{"tempId": "a", "title": "A", "description": ""}],
            "edges": [],
        },
    )
    assert r["success"] is True
    prop_id = r["proposal_id"]
    events = [e for e in _emitted(handler) if e[0] == "proposal.ready"]
    assert events and events[-1][1]["proposal_id"] == prop_id
    assert events[-1][1]["project_id"] == "p1"


async def test_commit_is_atomic_and_idempotent(handler):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    prop = await handler.execute(
        "task_batch_propose",
        {
            "project_id": "p1",
            "source": "spec:foo",
            "tasks": [
                {"tempId": "a", "title": "A", "description": ""},
                {"tempId": "b", "title": "B", "description": ""},
            ],
            "edges": [{"from": "b", "to": "a", "dep_type": "blocks"}],
        },
    )
    prop_id = prop["proposal_id"]

    c1 = await handler.execute("task_batch_commit", {"proposal_id": prop_id})
    assert c1["success"] is True
    assert len(c1["task_ids"]) == 2

    # Double-commit rejected.
    c2 = await handler.execute("task_batch_commit", {"proposal_id": prop_id})
    assert c2["success"] is False
    assert "committed" in c2["error"].lower()


async def test_commit_partial_failure_rolls_back(handler, monkeypatch):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    # Pre-create an existing task so we can add an edge that references it —
    # this proves the rollback also cleans up edges that pointed at
    # already-existing tasks (not just at the tasks the batch created).
    pre = await handler.execute(
        "create_task",
        {"project_id": "p1", "title": "PRE", "description": ""},
    )
    pre_id = pre["created"]

    prop = await handler.execute(
        "task_batch_propose",
        {
            "project_id": "p1",
            "source": "spec:foo",
            "tasks": [
                {"tempId": "a", "title": "A", "description": ""},
                {"tempId": "b", "title": "B", "description": ""},
            ],
            "edges": [
                # a -> PRE_ID (existing) is created BEFORE the failure.
                {"from": "a", "to": pre_id, "dep_type": "blocks"},
            ],
        },
    )

    from src.commands import proposal_commands as pc

    calls = {"n": 0}
    original = pc._create_one_task

    async def flaky(h, project_id, task_spec, source):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated failure")
        return await original(h, project_id, task_spec, source)

    monkeypatch.setattr(pc, "_create_one_task", flaky)

    r = await handler.execute(
        "task_batch_commit", {"proposal_id": prop["proposal_id"]}
    )
    assert r["success"] is False
    # Rollback: only the pre-existing task should remain, no batch-created ones.
    listing = await handler.execute("list_tasks", {"project_id": "p1"})
    remaining = [t for t in listing.get("tasks", []) if t.get("id") != pre_id]
    assert remaining == []
    # And no leaked edges TO the pre-existing task.
    edges = await handler._db.get_typed_dependencies(pre_id)
    # get_typed_dependencies returns edges FROM pre_id; check the reverse direction.
    # Any leaked (a -> pre_id) would show up as a dep on `a`, which is gone —
    # but the row could still exist orphaned. Assert directly against the table.
    from sqlalchemy import select as _select
    from src.database.tables import task_dependencies as _td
    async with handler._db._engine.begin() as conn:
        rows = (
            await conn.execute(
                _select(_td).where(_td.c.depends_on_task_id == pre_id)
            )
        ).all()
    assert rows == [], f"leaked edges pointing at pre-existing task: {rows}"

    # Proposal must be released back to 'ready' so a retry is possible.
    from src.database.queries.proposal_queries import get_proposal
    row = await get_proposal(handler._db, prop["proposal_id"])
    assert row["status"] == "ready"


async def test_commit_concurrent_double_commit_only_one_wins(handler):
    """Two overlapping task_batch_commit calls on the same proposal must
    materialise the task set exactly once — the loser aborts on the
    conditional-UPDATE claim BEFORE creating any tasks."""
    import asyncio

    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    prop = await handler.execute(
        "task_batch_propose",
        {
            "project_id": "p1",
            "source": "spec:foo",
            "tasks": [
                {"tempId": "a", "title": "A", "description": ""},
                {"tempId": "b", "title": "B", "description": ""},
            ],
            "edges": [],
        },
    )
    prop_id = prop["proposal_id"]

    r1, r2 = await asyncio.gather(
        handler.execute("task_batch_commit", {"proposal_id": prop_id}),
        handler.execute("task_batch_commit", {"proposal_id": prop_id}),
    )
    wins = [r for r in (r1, r2) if r.get("success")]
    losses = [r for r in (r1, r2) if not r.get("success")]
    assert len(wins) == 1 and len(losses) == 1, (r1, r2)

    listing = await handler.execute("list_tasks", {"project_id": "p1"})
    # Exactly two tasks materialised, no duplicates.
    titles = sorted(t.get("title") for t in listing.get("tasks", []))
    assert titles == ["A", "B"], titles


async def test_update_draft_only(handler):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    prop = await handler.execute(
        "task_batch_propose",
        {
            "project_id": "p1",
            "source": "spec:foo",
            "tasks": [{"tempId": "a", "title": "A", "description": ""}],
            "edges": [],
        },
    )
    up = await handler.execute(
        "task_batch_update",
        {
            "proposal_id": prop["proposal_id"],
            "payload": {
                "tasks": [{"tempId": "a", "title": "A2", "description": ""}],
                "edges": [],
            },
        },
    )
    assert up["success"] is True

    await handler.execute(
        "task_batch_commit", {"proposal_id": prop["proposal_id"]}
    )
    up2 = await handler.execute(
        "task_batch_update",
        {
            "proposal_id": prop["proposal_id"],
            "payload": {"tasks": [], "edges": []},
        },
    )
    assert up2["success"] is False


async def test_discard(handler):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    prop = await handler.execute(
        "task_batch_propose",
        {
            "project_id": "p1",
            "source": "spec:foo",
            "tasks": [{"tempId": "a", "title": "A", "description": ""}],
            "edges": [],
        },
    )
    r = await handler.execute(
        "task_batch_discard", {"proposal_id": prop["proposal_id"]}
    )
    assert r["success"] is True
