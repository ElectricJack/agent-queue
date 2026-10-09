"""Batch propose/commit/update/discard + cycle detection.

Phase 6 Tasks 2 + 3 (design §8, spec ingestion).
"""
from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path

import pytest

from src.database.queries.proposal_queries import detect_cycles, existing_graph_edges, get_proposal
from src.api.auth import RequestScope
from src.api.scope import check_request_scope
from src.models import (
    Agent, AgentProfile, AgentState, SessionRecord, Task, TaskStatus, TaskType,
)


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


def test_detect_cycles_deep_chain_no_recursion_error():
    """A 5000-node A→B→...→Z→A chain must not recurse past Python's limit."""
    import sys

    n = 5000
    ids = [f"n{i}" for i in range(n)]
    tasks_in = [{"tempId": i} for i in ids]
    edges = [{"from": ids[i], "to": ids[i + 1], "dep_type": "blocks"} for i in range(n - 1)]
    edges.append({"from": ids[-1], "to": ids[0], "dep_type": "blocks"})  # close cycle
    # Guard against a resurrection of the recursive form.
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(500)
    try:
        cycles = detect_cycles([], tasks_in, edges)
    finally:
        sys.setrecursionlimit(old_limit)
    assert len(cycles) == 1
    assert len(cycles[0]) == n


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


async def _approve(
    handler, proposal_id: str, *, project_id: str = "p1", resolution: str = "approved"
) -> str:
    """Raise and resolve the proposal's human gate as the pipeline and dashboard do."""
    gate = await handler.execute(
        "gate_create",
        {
            "project_id": project_id,
            "gate_type": "human",
            "title": "Approve task batch?",
            "await_id": proposal_id,
        },
    )
    assert gate["success"] is True, gate
    await handler._db.resolve_gate(
        gate["gate_id"], resolved_by="human:test", resolution=resolution
    )
    return gate["gate_id"]


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
                {
                    "tempId": "a",
                    "title": "A",
                    "description": "",
                    "deliverables": [
                        {"id": "module", "kind": "file", "target": "src/new_module.py"}
                    ],
                },
                {"tempId": "b", "title": "B", "description": ""},
            ],
            "edges": [{"from": "b", "to": "a", "dep_type": "blocks"}],
        },
    )
    prop_id = prop["proposal_id"]
    gate_id = await _approve(handler, prop_id)

    c1 = await handler.execute("task_batch_commit", {"proposal_id": prop_id})
    assert c1["success"] is True
    assert len(c1["task_ids"]) == 2
    assert (await handler._db.get_task(c1["task_ids"][0])).deliverables == [
        {"id": "module", "kind": "file", "target": "src/new_module.py"}
    ]

    # A replayed approval is idempotent: the original receipt, no second graph.
    c2 = await handler.execute(
        "task_batch_commit", {"proposal_id": prop_id, "gate_id": gate_id, "project_id": "p1"}
    )
    assert c2 == {"success": True, "already_committed": True, "task_ids": c1["task_ids"]}
    assert len(await handler._db.list_tasks(project_id="p1")) == 2


async def test_propose_refuses_a_task_route(handler):
    """A batch carries hints, never a route (mandatory task routing §5.1)."""
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    await handler._db.create_profile(
        AgentProfile(id="worker", name="Worker", harness="claude")
    )
    proposal = await handler.execute(
        "task_batch_propose",
        {
            "project_id": "p1",
            "source": "spec:route",
            "tasks": [
                {
                    "tempId": "a",
                    "title": "A",
                    "description": "",
                    "profile_id": "worker",
                }
            ],
            "edges": [],
        },
    )

    assert proposal["success"] is False
    assert proposal["code"] == "routing.choice_forbidden"
    assert proposal["refused"] == ["tasks[0].profile_id"]
    assert "proposal_id" not in proposal
    assert await handler._db.list_tasks(project_id="p1") == []


async def test_commit_keeps_a_task_class_as_its_hint(handler):
    """A batch class is the filer's hint: the committed task is unrouted."""
    from src.vault import ensure_default_intelligence_classes

    ensure_default_intelligence_classes(handler.config.data_dir)
    handler.orchestrator.intelligence_classes.reload(handler.config.data_dir)
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    proposal = await handler.execute(
        "task_batch_propose",
        {
            "project_id": "p1",
            "source": "spec:route",
            "tasks": [
                {
                    "tempId": "a",
                    "title": "A",
                    "description": "",
                    "intelligence_class": "standard-high",
                }
            ],
            "edges": [],
        },
    )
    await _approve(handler, proposal["proposal_id"])

    committed = await handler.execute(
        "task_batch_commit", {"proposal_id": proposal["proposal_id"]}
    )

    assert committed["success"] is True, committed
    task = await handler._db.get_task(committed["task_ids"][0])
    assert (task.profile_id, task.intelligence_class, task.class_hint, task.route_source) == (
        None, None, "standard-high", "unrouted",
    )


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
    await _approve(handler, prop["proposal_id"])

    original = handler._db.create_task
    calls = {"n": 0}

    async def flaky(task, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated failure")
        return await original(task, **kwargs)

    monkeypatch.setattr(handler._db, "create_task", flaky)

    r = await handler.execute(
        "task_batch_commit", {"proposal_id": prop["proposal_id"]}
    )
    assert r["success"] is False
    # Rollback: only the pre-existing task should remain, no batch-created ones.
    listing = await handler.execute("list_tasks", {"project_id": "p1"})
    remaining = [t for t in listing.get("tasks", []) if t.get("id") != pre_id]
    assert remaining == []
    # And no leaked edges TO the pre-existing task.
    await handler._db.get_typed_dependencies(pre_id)
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

    # Public-API lock-in: get_typed_dependencies must show no leaked
    # edges after rollback. Pre-existing task had no outgoing deps
    # before the batch, and the batch's only outgoing edge (a -> pre_id)
    # must be fully cleaned up — asserting via the public API catches
    # future regressions where cleanup skips one of the two directions.
    assert await handler._db.get_typed_dependencies(pre_id) == []
    # And for every remaining task (post-rollback), no edge should
    # dangle. Only pre_id remains, but assert generically so a future
    # test author extending this fixture stays honest.
    for t in listing.get("tasks", []):
        deps = await handler._db.get_typed_dependencies(t["id"])
        assert deps == [], f"task {t['id']!r} has leaked deps after rollback: {deps}"

    # Proposal must be released back to 'ready' so a retry is possible.
    from src.database.queries.proposal_queries import get_proposal
    row = await get_proposal(handler._db, prop["proposal_id"])
    assert row["status"] == "ready"


async def test_commit_concurrent_double_commit_only_one_wins(handler):
    """Two overlapping task_batch_commit calls on the same proposal must
    materialise the task set exactly once — the loser aborts on the
    conditional-UPDATE claim BEFORE creating any tasks, or, if it starts
    after the winner finished, replays the winner's receipt."""
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
    await _approve(handler, prop_id)

    r1, r2 = await asyncio.gather(
        handler.execute("task_batch_commit", {"proposal_id": prop_id}),
        handler.execute("task_batch_commit", {"proposal_id": prop_id}),
    )
    created = [r for r in (r1, r2) if r.get("success") and not r.get("already_committed")]
    assert len(created) == 1, (r1, r2)
    other = r2 if created[0] is r1 else r1
    assert other.get("already_committed") or "already claimed" in other.get("error", ""), other

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

    await _approve(handler, prop["proposal_id"])
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


# ---------------------------------------------------------------------------
# Approval at the materialisation boundary (policy simplification, audit F1)
# ---------------------------------------------------------------------------


async def _propose(handler, project_id: str = "p1") -> str:
    await handler.execute("create_project", {"id": project_id, "name": project_id})
    prop = await handler.execute(
        "task_batch_propose",
        {
            "project_id": project_id,
            "source": "spec:foo",
            "tasks": [{"tempId": "a", "title": "A", "description": ""}],
            "edges": [],
        },
    )
    assert prop["success"] is True, prop
    return prop["proposal_id"]


async def _open_gate(handler, proposal_id: str, **extra) -> str:
    gate = await handler.execute(
        "gate_create",
        {
            "project_id": "p1",
            "gate_type": "human",
            "title": "Approve task batch?",
            "await_id": proposal_id,
            **extra,
        },
    )
    assert gate["success"] is True, gate
    return gate["gate_id"]


async def test_commit_without_an_approval_gate_is_not_approved(handler):
    """The direct command path cannot bypass the approval the pipeline asks for."""
    prop_id = await _propose(handler)

    r = await handler.execute("task_batch_commit", {"proposal_id": prop_id})

    assert r["success"] is False and r["not_approved"] is True, r
    assert "no human approval gate" in r["error"]
    assert await handler._db.list_tasks(project_id="p1") == []
    assert (await get_proposal(handler._db, prop_id))["status"] == "ready"


@pytest.mark.parametrize(
    "case",
    [
        "rejected",
        "rejected-newest-gate",
        "free-text",
        "open",
        "expired",
        "not-a-human-gate",
        "another-proposal",
        "another-project",
        "project-argument-mismatch",
    ],
)
async def test_commit_refuses_any_decision_but_this_proposals_approval(handler, case):
    """Rejected, expired, unrelated and wrong-project decisions create nothing."""
    prop_id = await _propose(handler)
    db = handler._db
    args: dict = {"proposal_id": prop_id}
    if case == "rejected":
        args["gate_id"] = await _approve(handler, prop_id, resolution="rejected")
    elif case == "rejected-newest-gate":
        await _approve(handler, prop_id, resolution="reject")
    elif case == "free-text":
        args["gate_id"] = await _approve(handler, prop_id, resolution="Yes, approve it")
    elif case == "open":
        args["gate_id"] = await _open_gate(handler, prop_id)
    elif case == "expired":
        gate_id = await _open_gate(handler, prop_id, timeout_at=time.time() - 1)
        assert gate_id in await db.expire_open_gates(time.time())
        args["gate_id"] = gate_id
    elif case == "not-a-human-gate":
        gate_id = await _open_gate(handler, prop_id, gate_type="task")
        await db.resolve_gate(gate_id, resolved_by="sweep:task", resolution="approved")
        args["gate_id"] = gate_id
    elif case == "another-proposal":
        other = await handler.execute(
            "task_batch_propose",
            {
                "project_id": "p1",
                "source": "spec:bar",
                "tasks": [{"tempId": "x", "title": "X", "description": ""}],
                "edges": [],
            },
        )
        args["gate_id"] = await _approve(handler, other["proposal_id"])
    elif case == "another-project":
        await handler.execute("create_project", {"id": "p2", "name": "p2"})
        args["gate_id"] = await _approve(handler, prop_id, project_id="p2")
    elif case == "project-argument-mismatch":
        args["gate_id"] = await _approve(handler, prop_id)
        args["project_id"] = "p2"

    r = await handler.execute("task_batch_commit", args)

    assert r["success"] is False and r.get("not_approved") is True, r
    assert await db.list_tasks(project_id="p1") == []
    assert (await get_proposal(db, prop_id))["status"] == "ready"


async def test_commit_with_the_exact_approving_gate_materialises_once(handler):
    prop_id = await _propose(handler)
    gate_id = await _approve(handler, prop_id, resolution="approve")
    args = {"proposal_id": prop_id, "gate_id": gate_id, "project_id": "p1"}

    first = await handler.execute("task_batch_commit", args)
    replay = await handler.execute("task_batch_commit", args)

    assert first["success"] is True and not first.get("already_committed"), first
    assert replay == {"success": True, "already_committed": True, "task_ids": first["task_ids"]}
    assert {t.id for t in await handler._db.list_tasks(project_id="p1")} == set(first["task_ids"])


async def test_update_is_refused_once_an_approval_gate_exists(handler):
    """The gate asks about one payload; that is the only one it can approve."""
    prop_id = await _propose(handler)
    await _open_gate(handler, prop_id)

    up = await handler.execute(
        "task_batch_update",
        {
            "proposal_id": prop_id,
            "payload": {
                "tasks": [{"tempId": "a", "title": "Changed", "description": ""}],
                "edges": [],
            },
        },
    )

    assert up["success"] is False and "frozen" in up["error"], up
    assert (await get_proposal(handler._db, prop_id))["payload"]["tasks"][0]["title"] == "A"


def test_commit_outcomes_are_typed():
    from src.commands.contracts.builtin import _outcome_of

    assert _outcome_of("task_batch_commit", {"success": True, "task_ids": ["t"]}) == "committed"
    assert _outcome_of(
        "task_batch_commit", {"success": True, "already_committed": True, "task_ids": ["t"]}
    ) == "already_committed"
    assert _outcome_of(
        "task_batch_commit", {"success": False, "not_approved": True, "error": "no gate"}
    ) == "not_approved"
    assert _outcome_of("task_batch_commit", {"success": False, "error": "cycle"}) == "rejected"


async def _existing(handler, tid="existing", status=None):
    from src.models import Task, TaskStatus
    if not await handler._db.get_project("p1"):
        await handler.execute("create_project", {"id": "p1", "name": "p1"})
    await handler._db.create_task(Task(
        id=tid, project_id="p1", title=tid, description="Original",
        status=status or TaskStatus.DEFINED,
    ))
    return tid


async def _changes(handler, **operations):
    result = await handler.execute("task_batch_propose", {
        "project_id": "p1", "source": "change-set:test", **operations,
    })
    assert result["success"], result
    return result


async def test_change_set_preview_and_proposal_are_invisible(handler):
    from sqlalchemy import select
    from src.database.tables import task_comments, task_proposals
    tid = await _existing(handler)
    ops = {
        "tasks": [{"tempId": "new", "title": "New", "description": ""}],
        "edits": [{"task_id": tid, "title": "Changed", "priority": 7}],
        "edges": [{"from": tid, "to": "new"}],
        "comments": [{"task_id": tid, "body": "staged finding"}],
    }
    preview = await _changes(handler, **ops, dry_run=True)
    assert preview["dry_run"] and len(preview["diff"]["tasks"]) == 2
    assert (await handler._db.get_task(tid)).title == tid
    assert len(await handler._db.list_tasks(project_id="p1")) == 1
    assert await handler._db.get_typed_dependencies(tid) == []
    async with handler._db.immediate() as conn:
        assert not (await conn.execute(select(task_proposals))).all()
        assert not (await conn.execute(select(task_comments))).all()
    proposed = await _changes(handler, **ops)
    await handler.orchestrator._check_defined_tasks()
    assert len(await handler._db.list_tasks(project_id="p1")) == 1
    # The existing row may progress independently; the staged row never does.
    assert (await get_proposal(handler._db, proposed["proposal_id"]))["status"] == "ready"


async def test_change_set_commits_edits_edges_comments_and_audit_once(handler):
    import json
    from sqlalchemy import select
    from src.database.tables import events
    tid = await _existing(handler)
    proposal = await _changes(handler,
        tasks=[{"tempId": "new", "title": "Prerequisite", "description": ""}],
        edits=[{"task_id": tid, "title": "Changed", "description": "Revised", "priority": 3}],
        edges=[{"from": tid, "to": "new"}],
        comments=[{"task_id": tid, "body": "finding", "kind": "progress"},
                  {"task_id": "new", "body": "new task finding"}],
    )
    await _approve(handler, proposal["proposal_id"])
    committed = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert committed["success"], committed
    task = await handler._db.get_task(tid)
    assert (task.title, task.description, task.priority, task.is_blocked) == ("Changed", "Revised", 3, True)
    assert await handler._db.get_dependencies(tid) == set(committed["task_ids"])
    comments = (await handler._db.list_task_comments(tid))["comments"]
    assert comments[0]["body"] == "finding" and comments[0]["author_kind"] == "user"
    replay = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert replay["already_committed"]
    async with handler._db.immediate() as conn:
        audits = (await conn.execute(select(events.c.payload).where(
            events.c.event_type == "task.change_set_committed"
        ))).scalars().all()
    assert len(audits) == 1
    assert json.loads(audits[0])["receipt"]["edited_task_ids"] == [tid]


@pytest.mark.parametrize("mutation", ["row", "edge", "metadata", "claim"])
async def test_change_set_detects_optimistic_conflicts(handler, mutation):
    from sqlalchemy import update
    from src.database.tables import tasks
    tid = await _existing(handler)
    await _existing(handler, "other")
    proposal = await _changes(handler, edits=[{"task_id": tid, "title": "Proposal"}])
    await _approve(handler, proposal["proposal_id"])
    if mutation == "row":
        await handler._db.update_task(tid, description="Concurrent edit")
    elif mutation == "edge":
        await handler._db.add_dependency(tid, "other")
    elif mutation == "metadata":
        await handler._db.set_task_meta(tid, "manual_decision", "concurrent")
    else:
        # A raw task-row write without an updated_at bump must also conflict.
        async with handler._db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == tid).values(claim_epoch=1))
    result = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert result["code"] == "change_set.conflict", result
    assert (await handler._db.get_task(tid)).title == tid
    assert (await get_proposal(handler._db, proposal["proposal_id"]))["status"] == "ready"


async def test_change_set_late_failure_rolls_back_every_operation(handler, monkeypatch):
    from sqlalchemy import select
    from src.database.tables import events, task_comments
    tid = await _existing(handler)
    await _existing(handler, "old")
    await handler._db.add_dependency(tid, "old")
    proposal = await _changes(handler,
        tasks=[{"tempId": "new", "title": "New", "description": ""}],
        edits=[{"task_id": tid, "title": "Changed", "action": "pause"}],
        remove_edges=[{"from": tid, "to": "old"}],
        edges=[{"from": tid, "to": "new"}],
        comments=[{"task_id": tid, "body": "not durable"}],
    )
    await _approve(handler, proposal["proposal_id"])
    original = handler._db.add_task_comment
    async def fail_after_comment(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("injected after all staged writes")
    monkeypatch.setattr(handler._db, "add_task_comment", fail_after_comment)
    result = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert not result["success"] and "injected" in result["error"]
    assert len(await handler._db.list_tasks(project_id="p1")) == 2
    task = await handler._db.get_task(tid)
    assert task.title == tid and task.status.value == "DEFINED"
    assert await handler._db.get_dependencies(tid) == {"old"}
    assert await handler._db.get_task_meta(tid, "manual_pause") is None
    async with handler._db.immediate() as conn:
        assert not (await conn.execute(select(task_comments))).all()
        assert not (await conn.execute(select(events).where(
            events.c.event_type == "task.change_set_committed"
        ))).all()
    assert (await get_proposal(handler._db, proposal["proposal_id"]))["status"] == "ready"


async def test_change_set_edge_reversal_is_validated_as_a_whole(handler):
    a = await _existing(handler, "a")
    b = await _existing(handler, "b")
    await handler._db.add_dependency(a, b)
    proposal = await _changes(handler,
        remove_edges=[{"from": a, "to": b}], edges=[{"from": b, "to": a}],
    )
    await _approve(handler, proposal["proposal_id"])
    result = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert result["success"], result
    assert await handler._db.get_dependencies(a) == set()
    assert await handler._db.get_dependencies(b) == {a}


async def test_change_set_pause_resume_block_and_archive(handler):
    tid = await _existing(handler)
    for action in ("pause", "resume", "block", "archive"):
        proposal = await _changes(handler, edits=[{"task_id": tid, "action": action}])
        await _approve(handler, proposal["proposal_id"])
        result = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
        assert result["success"], result
        if action == "pause":
            assert (await handler._db.get_task(tid)).status.value == "PAUSED"
        elif action == "resume":
            assert (await handler._db.get_task(tid)).status.value == "DEFINED"
        elif action == "block":
            await handler.orchestrator._check_defined_tasks()
            assert (await handler._db.get_task(tid)).status.value == "BLOCKED"
    assert await handler._db.get_task(tid) is None
    assert await handler._db.get_archived_task(tid)


async def test_change_set_reparent_and_parent_edge_removal(handler):
    tid = await _existing(handler)
    await _existing(handler, "parent")
    proposal = await _changes(handler, edits=[{"task_id": tid, "parent_id": "parent"}])
    await _approve(handler, proposal["proposal_id"])
    result = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert result["success"], result
    assert (await handler._db.get_task(tid)).parent_task_id == "parent"
    proposal = await _changes(handler, remove_edges=[{"from": tid, "to": "parent", "dep_type": "parent-child"}])
    await _approve(handler, proposal["proposal_id"])
    result = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert result["success"], result
    assert (await handler._db.get_task(tid)).parent_task_id is None


@pytest.mark.parametrize("control", ["dependency", "block"])
async def test_change_set_scheduler_tick_cannot_promote_stale_snapshot(handler, monkeypatch, control):
    import asyncio
    tid = await _existing(handler)
    proposal = await _changes(handler,
        tasks=[{"tempId": "prerequisite", "title": "Prerequisite", "description": ""}],
        edges=[{"from": tid, "to": "prerequisite"}] if control == "dependency" else [],
        edits=[{"task_id": tid, "action": "block"}] if control == "block" else [],
        comments=[{"task_id": tid, "body": "Barrier"}],
    )
    await _approve(handler, proposal["proposal_id"])
    staged, release, promotion = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_comment = handler._db.add_task_comment
    original_transition = handler._db.transition_task
    async def barrier(*args, **kwargs):
        staged.set()
        await release.wait()
        return await original_comment(*args, **kwargs)
    async def observed_transition(*args, **kwargs):
        promotion.set()
        return await original_transition(*args, **kwargs)
    monkeypatch.setattr(handler._db, "add_task_comment", barrier)
    monkeypatch.setattr(handler._db, "transition_task", observed_transition)
    commit = asyncio.create_task(handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]}))
    await asyncio.wait_for(staged.wait(), 5)
    tick = asyncio.create_task(handler.orchestrator._check_defined_tasks())
    try:
        await asyncio.wait_for(promotion.wait(), 5)
        # Reader sees exactly the old committed graph while scheduler writes wait.
        task = await handler._db.get_task(tid)
        assert not task.is_blocked and task.status.value == "DEFINED"
        assert len(await handler._db.list_tasks(project_id="p1")) == 1
        assert await handler._db.get_dependencies(tid) == set()
        assert not tick.done()
    finally:
        release.set()
    assert (await asyncio.wait_for(commit, 5))["success"]
    await asyncio.wait_for(tick, 5)
    task = await handler._db.get_task(tid)
    assert task.is_blocked == (control == "dependency")
    assert task.status.value == ("DEFINED" if control == "dependency" else "BLOCKED")
    await handler.orchestrator._check_defined_tasks()
    assert (await handler._db.get_task(tid)).status == task.status


async def test_change_set_concurrent_claim_rechecks_blocked_state(handler, monkeypatch):
    import asyncio
    from src.models import Agent, AgentState, TaskStatus
    tid = await _existing(handler, status=TaskStatus.READY)
    await handler._db.create_agent(Agent(
        id="claim-agent", name="claim-agent", state=AgentState.IDLE, profile_id="test",
    ))
    proposal = await _changes(handler,
        tasks=[{"tempId": "prerequisite", "title": "Prerequisite", "description": ""}],
        edges=[{"from": tid, "to": "prerequisite"}],
        comments=[{"task_id": tid, "body": "Barrier"}],
    )
    await _approve(handler, proposal["proposal_id"])
    staged, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = handler._db.add_task_comment
    async def barrier(*args, **kwargs):
        staged.set()
        await release.wait()
        return await original(*args, **kwargs)
    async def claim():
        async with handler._db.immediate() as conn:
            attempted.set()
            return await handler._db.take_task(conn, tid, agent_id="claim-agent", now=time.time())
    monkeypatch.setattr(handler._db, "add_task_comment", barrier)
    commit = asyncio.create_task(handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]}))
    await asyncio.wait_for(staged.wait(), 5)
    claimant = asyncio.create_task(claim())
    try:
        await asyncio.wait_for(attempted.wait(), 5)
        assert (await handler._db.get_task(tid)).status == TaskStatus.READY
        assert not claimant.done()
    finally:
        release.set()
    assert (await asyncio.wait_for(commit, 5))["success"]
    assert await asyncio.wait_for(claimant, 5) is None
    task = await handler._db.get_task(tid)
    assert task.is_blocked and task.assigned_agent_id is None


@pytest.mark.parametrize("operation", ["cycle", "container", "closed_parent", "resume", "live"])
async def test_change_set_checks_guards_before_staging(handler, operation):
    from src.models import TaskStatus
    tid = await _existing(handler)
    other = await _existing(handler, "other")
    ops = {}
    if operation == "cycle":
        ops["edges"] = [{"from": tid, "to": other}, {"from": other, "to": tid}]
    elif operation == "container":
        async with handler._db.immediate() as conn:
            await handler._db.mark_container(other, conn=conn)
        ops["edges"] = [{"from": tid, "to": other}]
    elif operation == "closed_parent":
        await handler._db.update_task(other, status=TaskStatus.COMPLETED)
        ops["edits"] = [{"task_id": tid, "parent_id": other}]
    elif operation == "resume":
        ops["edits"] = [{"task_id": tid, "action": "resume"}]
    else:
        from src.models import Agent, AgentState
        await handler._db.create_agent(Agent(
            id="holder", name="holder", profile_id="test", state=AgentState.IDLE,
        ))
        await handler._db.update_task(tid, assigned_agent_id="holder")
        ops["edits"] = [{"task_id": tid, "action": "pause"}]
    result = await handler.execute("task_batch_propose", {
        "project_id": "p1", "source": "guards", **ops,
    })
    assert not result["success"], result
    assert len(await handler._db.list_tasks(project_id="p1")) == 2
    assert await handler._db.get_dependencies(tid) == set()


async def test_change_set_recomputes_final_graph_once(handler, monkeypatch):
    a = await _existing(handler, "a")
    b = await _existing(handler, "b")
    proposal = await _changes(handler,
        edits=[{"task_id": a, "action": "pause"}, {"task_id": b, "action": "block"}],
        edges=[{"from": b, "to": a}],
    )
    await _approve(handler, proposal["proposal_id"])
    from unittest.mock import AsyncMock
    recompute = AsyncMock(wraps=handler._db.recompute_blocked)
    monkeypatch.setattr(handler._db, "recompute_blocked", recompute)
    result = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert result["success"], result
    assert recompute.await_count == 1
    assert (await handler._db.get_task(a)).status.value == "PAUSED"
    assert (await handler._db.get_task(b)).is_blocked


async def test_change_set_resume_notifies_frontier_only_after_commit(handler):
    from src.models import TaskStatus
    tid = await _existing(handler, status=TaskStatus.READY)
    await handler._db.pause_task(tid)
    notices = []
    async def notified(entries):
        assert (await handler._db.get_task(tid)).status == TaskStatus.READY
        notices.extend(entries)
    handler._db.set_ready_listener(notified)
    proposal = await _changes(handler, edits=[{"task_id": tid, "action": "resume"}])
    assert notices == []
    await _approve(handler, proposal["proposal_id"])
    result = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert result["success"], result
    assert notices == [(tid, "unblocked")]


async def test_change_set_missing_target_is_an_optimistic_conflict(handler):
    from src.models import TaskStatus
    tid = await _existing(handler, status=TaskStatus.FAILED)
    proposal = await _changes(handler, edits=[{"task_id": tid, "title": "Stale edit"}])
    await _approve(handler, proposal["proposal_id"])
    await handler._db.archive_task(tid)
    result = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert result["code"] == "change_set.conflict", result
    assert await handler._db.get_task(tid) is None
    assert (await get_proposal(handler._db, proposal["proposal_id"]))["status"] == "ready"


async def test_change_set_does_not_settle_an_unrelated_container(handler):
    from src.models import TaskStatus
    tid = await _existing(handler)
    parent = await _existing(handler, "unrelated")
    child = await _existing(handler, "unrelated-child")
    async with handler._db.immediate() as conn:
        await handler._db.set_parent(child, parent, conn=conn)
    await handler._db.update_task(child, status=TaskStatus.COMPLETED)
    await handler._db.update_task(parent, status=TaskStatus.IN_PROGRESS)
    proposal = await _changes(handler, comments=[{"task_id": tid, "body": "Local change"}])
    assert (await handler._db.get_task(parent)).status == TaskStatus.IN_PROGRESS
    await _approve(handler, proposal["proposal_id"])
    result = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert result["success"], result
    assert (await handler._db.get_task(parent)).status == TaskStatus.IN_PROGRESS


async def test_change_set_postcommit_notifications_match_event_contracts(handler, monkeypatch):
    from types import MethodType
    from src.event_schemas import validate_event
    from src.models import TaskStatus
    from src.orchestrator.events import EventsMixin
    tid = await _existing(handler)
    await _existing(handler, "parent")
    archived = await _existing(handler, "archived", status=TaskStatus.FAILED)
    proposal = await _changes(handler,
        tasks=[{"tempId": "new", "title": "New", "description": ""}],
        edits=[
            {"task_id": tid, "title": "Reviewed", "parent_id": "parent"},
            {"task_id": archived, "action": "archive"},
        ],
        comments=[{"task_id": tid, "body": "Reviewed finding"}],
    )
    await _approve(handler, proposal["proposal_id"])
    notices = []
    async def validate_notice(event_type, payload):
        assert (await get_proposal(handler._db, proposal["proposal_id"]))["status"] == "committed"
        assert validate_event(event_type, payload) == [], (event_type, payload)
        notices.append(event_type)
    handler.orchestrator.bus.emit.side_effect = validate_notice
    monkeypatch.setattr(handler.orchestrator, "_emit_task_event", MethodType(
        EventsMixin._emit_task_event, handler.orchestrator,
    ))
    result = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert result["success"], result
    assert {"task.created", "task.updated", "task.reparented", "task.archived", "notify.task_comment"} <= set(notices)


async def _ingest_assignment(handler, spec_kind="implementation"):
    from src.vault import ensure_default_intelligence_classes

    ensure_default_intelligence_classes(handler.config.data_dir)
    handler.orchestrator.intelligence_classes.reload(handler.config.data_dir)
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    path = Path(handler.config.vault_root) / "projects/p1/specs/approved.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nstatus: approved\nspec_kind: {spec_kind}\n---\n# Approved spec\n")
    await handler.db.create_profile(AgentProfile(
        id="spec-ingest", name="Spec Ingest", harness="codex",
        aq_commands=["task_batch_propose", "task_batch_commit", "list_tasks"],
    ))
    await handler.db.create_agent(Agent(id="ingester", name="Ingester", profile_id="spec-ingest"))
    await handler.db.create_task(Task(
        id="ingest", project_id="p1", title="Ingest approved spec", description=str(path),
        status=TaskStatus.IN_PROGRESS, profile_id="spec-ingest", route_source="role",
        assigned_agent_id="ingester", dedup_key=f"spec-ingest:{path}",
    ))
    await handler.db.update_agent("ingester", state=AgentState.BUSY, current_task_id="ingest")
    await handler.db.create_session(SessionRecord(
        id="s-ingest", task_id="ingest", project_id="p1", agent_id="ingester",
        profile_id="spec-ingest", harness="codex", provider="fake", name="ingest",
        lifecycle="task", state="running", desired_state="running", work_dir=str(path.parent),
        epoch="test", instance_token="instance", started_at=time.time(), last_claim_epoch=0,
    ))
    scope = RequestScope(
        kind="session", session_id="s-ingest", session_instance_token="instance",
        project_id="p1", task_id="ingest",
    )
    return path, scope


def _implementation_graph(path):
    return {
        "project_id": "p1", "source": f"spec:{path}",
        "tasks": [
            {"tempId": "core", "title": "Core epic", "description": str(path)},
            {"tempId": "api", "title": "API", "description": f"{path}: API files and tests",
             "task_type": "feature", "intelligence_class": "standard-high"},
            {"tempId": "ui", "title": "UI", "description": f"{path}: UI files and tests",
             "task_type": "feature", "intelligence_class": "standard-high"},
            {"tempId": "docs", "title": "Docs epic", "description": str(path)},
            {"tempId": "draft", "title": "Draft docs", "description": f"{path}: early draft",
             "task_type": "docs", "intelligence_class": "standard-high"},
            {"tempId": "final", "title": "Finalize docs",
             "description": f"{path}: waits for API output to document final behavior",
             "task_type": "docs", "intelligence_class": "standard-high"},
        ],
        "edges": [
            {"from": child, "to": parent, "dep_type": "parent-child"}
            for child, parent in [("api", "core"), ("ui", "core"),
                                  ("draft", "docs"), ("final", "docs")]
        ] + [{"from": "final", "to": "api", "dep_type": "blocks"}],
    }


async def _ingest_propose(handler, path, scope, graph=None):
    args = graph or _implementation_graph(path)
    assert await check_request_scope("task_batch_propose", args, scope, db=handler.db) is None
    result = await handler.execute("task_batch_propose", {**args, "_scope": asdict(scope)})
    assert result["success"], result
    return result


async def _ingest_commit(handler, proposal_id, scope):
    args = {"proposal_id": proposal_id}
    assert await check_request_scope("task_batch_commit", args, scope, db=handler.db) is None
    return await handler.execute("task_batch_commit", {**args, "_scope": asdict(scope)})


async def _proposal_rows(handler, project_id="p1"):
    """(id, status) of every proposal in a project, read on its own connection."""
    from sqlalchemy import select

    from src.database.tables import task_proposals

    async with handler.db._engine.begin() as conn:
        rows = await conn.execute(
            select(task_proposals.c.id, task_proposals.c.status).where(
                task_proposals.c.project_id == project_id
            )
        )
        return [tuple(row) for row in rows]


async def test_approved_implementation_spec_publishes_live_tasks_without_a_commit_call(handler):
    """The graph an approved spec describes goes live in the call that stages it.

    Nothing waits on a second command: no ready proposal, no human gate, no
    operator running ``task_batch_commit`` by hand.
    """
    path, scope = await _ingest_assignment(handler)
    result = await _ingest_propose(handler, path, scope)

    assert result["committed"] is True
    assert len(result["task_ids"]) == 6
    proposal = await get_proposal(handler.db, result["proposal_id"])
    assert proposal["status"] == "committed"
    assert proposal["payload"]["spec_ingest"]["task_ids"] == result["task_ids"]
    # Every proposed task is a real row the graph already serves.
    assert {task.id for task in await handler.db.list_tasks(project_id="p1")} == {
        "ingest", *result["task_ids"]
    }
    assert await existing_graph_edges(handler.db, "p1")
    emitted = [event for event in _emitted(handler) if event[0].startswith("proposal")]
    assert not [event for event in emitted if event[0] == "proposal.ready"]
    assert ("proposal.status_changed", {
        "project_id": "p1", "proposal_id": result["proposal_id"], "status": "committed",
    }) in emitted
    # A later commit is the idempotent replay of that same receipt.
    replay = await _ingest_commit(handler, result["proposal_id"], scope)
    assert replay["already_committed"] and replay["task_ids"] == result["task_ids"]


async def test_design_ingestion_only_files_implementation_spec_for_review(handler):
    path, scope = await _ingest_assignment(handler, "design")
    # Even a recorded implementation graph cannot implement an explicit design.
    result = await _ingest_propose(handler, path, scope)
    assert not [event for event in _emitted(handler) if event[0] == "proposal.ready"]
    epic, child = [await handler.db.get_task(tid) for tid in result["task_ids"]]
    assert child.parent_task_id == epic.id
    assert child.task_type == TaskType.DESIGN
    assert child.class_hint == "deep-high"
    assert child.profile_id is None
    assert child.deliverables == [{"id": "implementation_review", "kind": "review", "target": "spec"}]
    assert "spec_kind: implementation" in child.description
    assert "aq review submit" in child.description
    assert str(path) in child.description
    assert len(await handler.db.list_tasks(project_id="p1")) == 3


async def test_implementation_ingestion_commits_epics_and_leaf_dependencies_without_gate(handler):
    path, scope = await _ingest_assignment(handler)
    result = await _ingest_propose(handler, path, scope)
    ids = dict(zip(["core", "api", "ui", "docs", "draft", "final"], result["task_ids"], strict=True))
    for leaf, parent in [("api", "core"), ("ui", "core"), ("draft", "docs"), ("final", "docs")]:
        task = await handler.db.get_task(ids[leaf])
        assert task.parent_task_id == ids[parent]
        assert task.class_hint == "standard-high"
        assert task.profile_id is None
    edges = await existing_graph_edges(handler.db, "p1")
    nonstructural = [(frm, to) for frm, to, kind in edges if kind != "parent-child"]
    assert nonstructural == [(ids["final"], ids["api"])]
    assert not ({ids["core"], ids["docs"]} & {endpoint for edge in nonstructural for endpoint in edge})
    assert detect_cycles(edges, [], []) == []
    # Promotion releases containers before it promotes their independent children.
    await handler.orchestrator._check_defined_tasks()
    await handler.orchestrator._check_defined_tasks()
    assert {task.id for task in await handler.db.list_tasks(project_id="p1")
            if task.status == TaskStatus.READY} == {ids["api"], ids["ui"], ids["draft"]}
    assert not (await handler.db.get_task(ids["api"])).is_blocked
    assert not (await handler.db.get_task(ids["ui"])).is_blocked
    assert not (await handler.db.get_task(ids["draft"])).is_blocked
    assert (await handler.db.get_task(ids["final"])).is_blocked
    replay = await _ingest_commit(handler, result["proposal_id"], scope)
    assert replay["already_committed"]
    assert replay["task_ids"] == result["task_ids"]


async def test_ingest_transaction_hides_partial_graph_and_rolls_back_every_row(handler, monkeypatch):
    path, scope = await _ingest_assignment(handler)
    original = handler.db.create_task
    original_recompute = handler.db.recompute_blocked
    created = []

    async def record_creation(task, **kwargs):
        await original(task, **kwargs)
        created.append(task.id)
        # A separate connection cannot see the inserted task or the proposal.
        assert {task.id for task in await handler.db.list_tasks(project_id="p1")} == {"ingest"}
        assert await _proposal_rows(handler) == []

    async def fail_after_dependency(*args, **kwargs):
        # Edges are inserted on the commit connection just before this recompute.
        await original_recompute(*args, **kwargs)
        raise RuntimeError("injected failure after dependency insertion")

    monkeypatch.setattr(handler.db, "create_task", record_creation)
    monkeypatch.setattr(handler.db, "recompute_blocked", fail_after_dependency)
    result = await handler.execute(
        "task_batch_propose", {**_implementation_graph(path), "_scope": asdict(scope)}
    )
    assert not result["success"] and "injected failure" in result["error"]
    assert {task.id for task in await handler.db.list_tasks(project_id="p1")} == {"ingest"}
    assert len(created) == 6
    assert await existing_graph_edges(handler.db, "p1") == []
    # Nothing survives for an operator to commit by hand either.
    assert await _proposal_rows(handler) == []
    assert not [event for event in _emitted(handler) if event[0] == "proposal.status_changed"]
    monkeypatch.setattr(handler.db, "create_task", original)
    monkeypatch.setattr(handler.db, "recompute_blocked", original_recompute)
    retried = await _ingest_propose(handler, path, scope)
    assert retried["committed"] is True


@pytest.mark.parametrize(
    "invalid", ["flat", "container-edge", "duplicate-parent", "profile", "edit"],
)
async def test_ingestion_refuses_invalid_graph_before_publishing(handler, invalid):
    path, scope = await _ingest_assignment(handler)
    graph = _implementation_graph(path)
    if invalid == "edit":
        # Ingest authority is ungated, so it may only create work.
        graph["edits"] = [{"task_id": "ingest", "title": "Renamed by ingestion"}]
    elif invalid == "flat":
        graph["edges"] = []
    elif invalid == "container-edge":
        graph["edges"].append({"from": "docs", "to": "api", "dep_type": "blocks"})
    elif invalid == "duplicate-parent":
        graph["edges"].append({"from": "api", "to": "docs", "dep_type": "parent-child"})
    else:
        graph["tasks"][1]["profile_id"] = "standard-high-codex"
    result = await handler.execute("task_batch_propose", {**graph, "_scope": asdict(scope)})
    assert not result["success"], result
    assert len(await handler.db.list_tasks(project_id="p1")) == 1
    assert (await handler.db.get_task("ingest")).title == "Ingest approved spec"
    # A refused batch leaves no staged proposal behind.
    assert await _proposal_rows(handler) == []


async def test_source_text_cannot_grant_ungated_authority(handler):
    path, scope = await _ingest_assignment(handler)
    ordinary = await handler.execute("task_batch_propose", _implementation_graph(path))
    refused = await handler.execute("task_batch_commit", {"proposal_id": ordinary["proposal_id"]})
    assert refused["not_approved"]
    assert await check_request_scope(
        "task_batch_commit", {"proposal_id": ordinary["proposal_id"]}, scope, db=handler.db,
    ) == "out of scope: proposal does not belong to held ingestion task"
    await handler.db.update_agent("ingester", state=AgentState.IDLE, current_task_id=None)
    refused = await handler.execute(
        "task_batch_propose", {**_implementation_graph(path), "_scope": asdict(scope)},
    )
    assert not refused["success"]
    assert "live role assignment" in refused["error"]


async def test_ingestion_scope_pins_project_session_and_approved_source(handler):
    path, scope = await _ingest_assignment(handler)
    assert await check_request_scope("list_tasks", {}, scope, db=handler.db) is None
    assert await check_request_scope(
        "list_tasks", {"project_id": "other"}, scope, db=handler.db,
    ) == "out of scope: project_id mismatch"
    assert await check_request_scope(
        "task_batch_propose", {"session_id": "other"}, scope, db=handler.db,
    ) == "out of scope: session_id mismatch"
    graph = _implementation_graph(path)
    graph["source"] = "spec:another-path"
    refused = await handler.execute("task_batch_propose", {**graph, "_scope": asdict(scope)})
    assert not refused["success"] and "source must match" in refused["error"]
    path.write_text("---\nstatus: draft\nspec_kind: implementation\n---\n# Unapproved\n")
    refused = await handler.execute(
        "task_batch_propose", {**_implementation_graph(path), "_scope": asdict(scope)},
    )
    assert not refused["success"] and "approved document" in refused["error"]
