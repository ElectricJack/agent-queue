"""Batch propose/commit/update/discard + cycle detection.

Phase 6 Tasks 2 + 3 (design §8, spec ingestion).
"""
from __future__ import annotations

import time
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from src.database.queries.proposal_queries import detect_cycles, existing_graph_edges, get_proposal
from src.api.auth import RequestScope
from src.api.scope import check_request_scope
from src.models import (
    Agent, AgentProfile, AgentState, SessionRecord, TaskStatus, TaskType,
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


@pytest.mark.parametrize("invalid", ["cycle", "parent"])
async def test_propose_checks_operational_root_delivery_edges(handler, invalid):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    root = {"tempId": "observe", "title": "Observe", "description": "Observe delivery",
            "placement": "operational_root", "delivery_targets": ["deliver"]}
    edge = {"from": "deliver", "to": "observe", "dep_type": "blocks"}
    if invalid == "parent":
        root["parent_id"] = "deliver"
        edge = {"from": "observe", "to": "deliver", "dep_type": "parent-child"}
    result = await handler.execute("task_batch_propose", {
        "project_id": "p1", "source": "spec:foo", "tasks": [root,
            {"tempId": "deliver", "title": "Deliver", "description": "Deliver API"}],
        "edges": [edge],
    })
    assert not result["success"]
    assert invalid in result["error"]
    assert await _proposal_rows(handler) == []


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
    assert c2 == {**c1, "already_committed": True}
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
    assert replay == {**first, "already_committed": True}
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


@pytest.mark.parametrize("mutation", ["row", "same_field", "edge", "metadata", "claim", "pause", "resume"])
async def test_change_set_approval_survives_later_changes(handler, mutation):
    from sqlalchemy import update
    from src.database.tables import tasks
    tid = await _existing(handler)
    await _existing(handler, "other")
    proposal = await _changes(handler, edits=[{"task_id": tid, "title": "Proposal"}])
    gate_id = await _approve(handler, proposal["proposal_id"])
    stored = (await get_proposal(handler._db, proposal["proposal_id"]))["payload"]
    if mutation == "row":
        await handler._db.update_task(tid, description="Concurrent edit")
    elif mutation == "same_field":
        await handler._db.update_task(tid, title="Concurrent edit")
    elif mutation == "edge":
        await handler._db.add_dependency(tid, "other")
    elif mutation == "metadata":
        await handler._db.set_task_meta(tid, "manual_decision", "concurrent")
    elif mutation in ("pause", "resume"):
        await handler._db.pause_task(tid)
        if mutation == "resume":
            await handler._db.resume_task(tid)
    else:
        # Claims change row versions even without an updated_at bump.
        async with handler._db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == tid).values(claim_epoch=1))
    result = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert result["success"], result
    task = await handler._db.get_task(tid)
    assert task.title == "Proposal"
    assert task.description == ("Concurrent edit" if mutation == "row" else "Original")
    if mutation == "pause":
        assert task.status == TaskStatus.PAUSED
    if mutation == "claim":
        async with handler._db.immediate() as conn:
            from sqlalchemy import select
            assert (await conn.execute(select(tasks.c.claim_epoch).where(tasks.c.id == tid))).scalar() == 1
    if mutation == "edge":
        assert await handler._db.get_dependencies(tid) == {"other"}
    committed = await get_proposal(handler._db, proposal["proposal_id"])
    assert committed["status"] == "committed"
    assert {k: v for k, v in committed["payload"].items() if k != "receipt"} == stored
    assert (await handler._db.get_gate(gate_id))["resolution"] == "approved"


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


async def test_change_set_missing_target_commits_empty_receipt(handler):
    from src.models import TaskStatus
    tid = await _existing(handler, status=TaskStatus.FAILED)
    proposal = await _changes(handler, edits=[{"task_id": tid, "title": "Stale edit"}])
    await _approve(handler, proposal["proposal_id"])
    await handler._db.archive_task(tid)
    result = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert result["success"] and result["task_ids"] == [], result
    assert result["missing_task_ids"] == [tid]
    assert result["skipped_operations"] == [
        {"operation": "edits", "index": 0, "missing_task_ids": [tid]},
    ]
    assert await handler._db.get_task(tid) is None
    assert (await get_proposal(handler._db, proposal["proposal_id"]))["status"] == "committed"
    replay = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert replay == {**result, "already_committed": True}


@pytest.mark.parametrize("parent_style", ["parent_id", "edge"])
async def test_missing_references_skip_only_affected_operations(handler, parent_style):
    import json
    from sqlalchemy import select
    from src.database.tables import events

    gone = await _existing(handler, "gone", TaskStatus.FAILED)
    kept = await _existing(handler, "kept")
    child = {"tempId": "child", "title": "Child", "description": ""}
    edges = [{"from": kept, "to": gone, "dep_type": "related"}, {"from": kept, "to": "new"}]
    if parent_style == "parent_id":
        child["parent_id"] = gone
    else:
        edges.append({"from": "child", "to": gone, "dep_type": "parent-child"})
    # A failed parent cannot take children; stage while it is still open.
    await handler._db.update_task(gone, status=TaskStatus.DEFINED)
    await handler._db.add_dependency(kept, gone, dep_type="related")
    proposal = await _changes(handler,
        tasks=[child,
               {"tempId": "grandchild", "title": "Grandchild", "description": "", "parent_id": "child"},
               {"tempId": "new", "title": "Survivor", "description": ""}],
        edits=[{"task_id": gone, "title": "Missing"}, {"task_id": kept, "title": "Approved"}],
        edges=edges, remove_edges=[{"from": kept, "to": gone, "dep_type": "related"}],
        comments=[{"task_id": gone, "body": "Missing"}, {"task_id": "grandchild", "body": "Skipped"},
                  {"task_id": kept, "body": "Survives"}],
    )
    await _approve(handler, proposal["proposal_id"])
    await handler._db.update_task(gone, status=TaskStatus.FAILED)
    await handler._db.archive_task(gone)
    result = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert result["success"] and len(result["task_ids"]) == 1, result
    assert result["missing_task_ids"] == [gone]
    omitted = {(item["operation"], item["index"]) for item in result["skipped_operations"]}
    assert omitted == {
        ("tasks", 0), ("tasks", 1), ("edits", 0), ("edges", 0),
        ("remove_edges", 0), ("comments", 0), ("comments", 1),
    } | ({("edges", 2)} if parent_style == "edge" else set())
    assert all(item["missing_task_ids"] == [gone] for item in result["skipped_operations"])
    assert (await handler._db.get_task(kept)).title == "Approved"
    assert await handler._db.get_dependencies(kept) == set(result["task_ids"])
    assert [c["body"] for c in (await handler._db.list_task_comments(kept))["comments"]] == ["Survives"]
    assert {t.title for t in await handler._db.list_tasks(project_id="p1")} == {"Survivor", "Approved"}
    async with handler._db.immediate() as conn:
        audit = (await conn.execute(select(events.c.payload).where(
            events.c.event_type == "task.change_set_committed"
        ))).scalar_one()
    assert json.loads(audit)["receipt"]["skipped_operations"] == result["skipped_operations"]
    stored = await get_proposal(handler._db, proposal["proposal_id"])
    assert stored["payload"]["diff"] == proposal["diff"]
    assert stored["payload"]["receipt"]["missing_task_ids"] == [gone]
    replay = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert replay == {**result, "already_committed": True}


async def test_legacy_expected_snapshot_is_ignored(handler):
    import json
    from sqlalchemy import update
    from src.database.tables import task_proposals

    tid = await _existing(handler)
    proposal = await _changes(handler, edits=[{"task_id": tid, "title": "Approved"}])
    stored = (await get_proposal(handler._db, proposal["proposal_id"]))["payload"]
    stored["expected"] = {"versions": {tid: "obsolete"}, "edges": [], "metadata": []}
    async with handler._db.immediate() as conn:
        await conn.execute(update(task_proposals).where(
            task_proposals.c.id == proposal["proposal_id"]
        ).values(payload=json.dumps(stored)))
    await _approve(handler, proposal["proposal_id"])
    result = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert result["success"], result
    assert (await handler._db.get_task(tid)).title == "Approved"


@pytest.mark.parametrize("resolution", [None, "approved", "rejected"])
async def test_gated_proposal_cannot_be_discarded(handler, resolution):
    proposal_id = await _propose(handler)
    gate, _ = await handler._db.create_gate("p1", "human", "Approve?", await_id=proposal_id)
    if resolution:
        await handler._db.resolve_gate(gate, resolved_by="test", resolution=resolution)
    before = await get_proposal(handler._db, proposal_id)
    result = await handler.execute("task_batch_discard", {"proposal_id": proposal_id})
    assert not result["success"] and "approval gate" in result["error"]
    assert await get_proposal(handler._db, proposal_id) == before
    assert not [event for event in _emitted(handler) if event[0] == "proposal.status_changed"]


@pytest.mark.parametrize("operation", ["remove_edge", "resume"])
async def test_approved_operation_already_done_is_idempotent(handler, operation):
    tid = await _existing(handler)
    other = await _existing(handler, "other")
    if operation == "remove_edge":
        await handler._db.add_dependency(tid, other)
        proposal = await _changes(handler, remove_edges=[{"from": tid, "to": other}])
        await handler._db.remove_dependency(tid, other)
    else:
        await handler._db.pause_task(tid)
        proposal = await _changes(handler, edits=[{"task_id": tid, "action": "resume"}])
        await handler._db.resume_task(tid)
    await _approve(handler, proposal["proposal_id"])
    result = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert result["success"], result
    assert await handler._db.get_dependencies(tid) == set()
    assert (await handler._db.get_task(tid)).status != TaskStatus.PAUSED


@pytest.mark.parametrize("operation", ["add", "remove"])
async def test_approved_parent_edge_only_applies_its_own_intent(handler, operation):
    tid = await _existing(handler)
    parent = await _existing(handler, "approved-parent")
    later = await _existing(handler, "later-parent")
    edge = {"from": tid, "to": parent, "dep_type": "parent-child"}
    if operation == "remove":
        async with handler._db.immediate() as conn:
            await handler._db.set_parent(tid, parent, conn=conn)
    proposal = await _changes(handler, **{
        "edges" if operation == "add" else "remove_edges": [edge],
    })
    await _approve(handler, proposal["proposal_id"])
    async with handler._db.immediate() as conn:
        await handler._db.set_parent(tid, later, conn=conn)
    result = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert result["success"], result
    # Add chooses the approved parent. Removal of one typed edge leaves later
    # membership alone; it is not approval to detach every possible parent.
    expected = parent if operation == "add" else later
    assert (await handler._db.get_task(tid)).parent_task_id == expected
    assert await handler._db.get_typed_dependencies(tid) == [(expected, "parent-child")]


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


async def _ingest_assignment(handler, spec_kind="implementation", *, feedback=None, coverage=None,
                             required=True):
    from src.vault import ensure_default_intelligence_classes

    ensure_default_intelligence_classes(handler.config.data_dir)
    handler.orchestrator.intelligence_classes.reload(handler.config.data_dir)
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    path = Path(handler.config.vault_root) / "projects/p1/specs/approved.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nstatus: approved\nspec_kind: {spec_kind}\n---\n# Approved spec\n")
    await handler.db.create_profile(AgentProfile(
        id="spec-ingest", name="Spec Ingest", harness="codex",
        default_class="deep-high", needs_workspace=False,
        aq_commands=["task_batch_propose", "task_batch_commit", "list_tasks", "approved_spec_packet"],
    ))
    if feedback is None:
        decision = await handler.execute("spec_approve", {"project_id": "p1", "spec_path": str(path)})
    else:
        from tests.test_review_feedback import accept, comment, prepared

        submitted = await handler.execute("review_submit", {
            "project_id": "p1", "kind": "spec", "title": "Immutable approval",
            "content": f"---\nspec_kind: {spec_kind}\n---\n# Approved spec\n",
        })
        assert submitted["success"], submitted
        rid = submitted["review_id"]
        cid = await comment(handler, rid, feedback)
        disposition = {"feedback_key": cid, "disposition": "follow_up",
                       "required_before_release": required, "external_prerequisite": required,
                       "reason": "External implementation work; the approved document stays valid"}
        if coverage is not None:
            disposition["coverage"] = coverage
        args, preview = await prepared(handler, rid, dispositions=[disposition])
        decision = await accept(handler, args, preview)
        assert decision["success"], decision
        decision["packet_sha256"] = preview["packet_sha256"]
        path = Path(handler.config.vault_root) / (await handler.db.get_review(rid))["vault_path"]
    assert decision["success"], decision
    ensured = await handler.execute("ensure_spec_ingest", {
        "project_id": "p1", "decision_id": decision["decision_id"],
        "packet_sha256": decision["packet_sha256"],
    })
    assert ensured["success"], ensured
    ingest_id = ensured["task_id"]
    await handler.db.create_agent(Agent(id="ingester", name="Ingester", profile_id="spec-ingest"))
    await handler.db.update_task(ingest_id, status=TaskStatus.IN_PROGRESS, assigned_agent_id="ingester")
    await handler.db.update_agent("ingester", state=AgentState.BUSY, current_task_id=ingest_id)
    await handler.db.create_session(SessionRecord(
        id="s-ingest", task_id=ingest_id, project_id="p1", agent_id="ingester",
        profile_id="spec-ingest", harness="codex", provider="fake", name="ingest",
        lifecycle="task", state="running", desired_state="running", work_dir=str(path.parent),
        epoch="test", instance_token="instance", started_at=time.time(), last_claim_epoch=0,
    ))
    scope = RequestScope(
        kind="session", session_id="s-ingest", session_instance_token="instance",
        project_id="p1", task_id=ingest_id,
    )
    return path, scope, decision


def _implementation_graph(path, decision):
    graph = {
        "project_id": "p1", "source": f"spec:decision:{decision['decision_id']}",
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

    for spec in graph["tasks"]:
        spec.update(section_anchor=f"Implementation / {spec['tempId']}",
                    primary_outcome=spec["title"], acceptance=["Verify " + spec["title"]],
                    constraints=["Preserve ordinary proposal approval"], size="s",
                    owned_modules=["src/" + spec["tempId"] + ".py"])
    return graph


async def _ingest_propose(handler, path, scope, decision, graph=None):
    args = graph or _implementation_graph(path, decision)
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


async def test_approved_implementation_spec_materializes_held_without_a_commit_call(handler):
    """Real rows and their routing gates remain inert until fidelity release."""
    from sqlalchemy import select
    from src.database.queries import ingest_batch_queries as batches
    from src.database.tables import integration_outbox, tasks
    from src.asks.operations import get_operation

    path, scope, decision = await _ingest_assignment(handler)
    result = await _ingest_propose(handler, path, scope, decision)
    assert result["status"] == "held" and result["structure_revision"] == 1
    assert len(result["task_ids"]) == 6
    assert set(result["members"].values()) == set(result["task_ids"])
    proposal = await get_proposal(handler.db, result["proposal_id"])
    assert proposal["status"] == "held"
    assert proposal["payload"]["spec_ingest"]["task_ids"] == result["task_ids"]
    assert {task.id for task in await handler.db.list_tasks(project_id="p1")} == {
        scope.task_id, *result["task_ids"]
    }
    async with handler.db.immediate() as conn:
        batch = await batches.get_batch(conn, result["batch_id"])
        assert batch["structure_digest"] == await batches.live_digest(conn, result["batch_id"])
        assert batch["check_results"]["passed"]
        holds = (await conn.execute(select(tasks.c.held_batch_id).where(
            tasks.c.id.in_(result["task_ids"]),
        ))).scalars().all()
        assert holds == [result["batch_id"]] * 6
        op = await get_operation(conn, decision["decision_id"], "fidelity_review.dispatch")
        assert op["status"] == "pending" and op["parameters"]["batch_id"] == result["batch_id"]
        event = (await conn.execute(select(integration_outbox).where(
            integration_outbox.c.event_type == "batch.held",
        ))).mappings().one()
        assert event["payload"]["structure_digest"] == batch["structure_digest"]
    assert not [event for event in _emitted(handler) if event[0] == "proposal.ready"]
    replay = await _ingest_commit(handler, result["proposal_id"], scope)
    assert not replay["success"] and replay["error_code"] == "fidelity_review_required"
    assert replay["batch_id"] == result["batch_id"]
    again = await _ingest_propose(handler, path, scope, decision)
    assert again["replayed"] and again["members"] == result["members"]


async def test_design_batch_released_with_server_receipt(handler):
    path, scope, decision = await _ingest_assignment(handler, "design")
    # Even a recorded implementation graph cannot implement an explicit design.
    result = await _ingest_propose(handler, path, scope, decision)
    assert result["status"] == "released"
    assert result["release_receipt"] == {"kind": "design_authoring_shape"}
    replay = await _ingest_commit(handler, result["proposal_id"], scope)
    assert replay["already_committed"] and replay["release_receipt"] == result["release_receipt"]
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
    # Design ingestion replaces the caller's graph with a server-authored child;
    # that child must carry the supersession instructions independently of the role.
    assert "docs/templates/spec.md" in child.description
    assert "including this design" in child.description
    assert "goal as kept, changed or dropped" in child.description
    assert "Jack's explicit sign-off" in child.description
    assert "pending drops block approval and affected implementation work" in child.description
    assert len(await handler.db.list_tasks(project_id="p1")) == 3


async def test_implementation_ingestion_holds_epics_and_leaf_dependencies_without_gate(handler):
    path, scope, decision = await _ingest_assignment(handler)
    result = await _ingest_propose(handler, path, scope, decision)
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
    await handler.orchestrator._check_defined_tasks()
    await handler.orchestrator._check_defined_tasks()
    # READY is a dependency state; the independent hold fences actual admission.
    eligible = await handler.db.list_active_tasks(exclude_held_ready=True)
    assert not {task.id for task in eligible if task.status == TaskStatus.READY}
    assert await handler.db.ingest_held_task_ids(set(result["task_ids"])) == set(result["task_ids"])
    replay = await _ingest_commit(handler, result["proposal_id"], scope)
    assert not replay["success"] and replay["error_code"] == "fidelity_review_required"


async def test_ingest_transaction_hides_partial_graph_and_rolls_back_every_row(handler, monkeypatch):
    path, scope, decision = await _ingest_assignment(handler)
    original = handler.db.create_task
    original_recompute = handler.db.recompute_blocked
    created = []

    async def record_creation(task, **kwargs):
        await original(task, **kwargs)
        created.append(task.id)
        # A separate connection cannot see the inserted task or the proposal.
        assert {task.id for task in await handler.db.list_tasks(project_id="p1")} == {scope.task_id}
        assert await _proposal_rows(handler) == []

    async def fail_after_dependency(*args, **kwargs):
        # Edges are inserted on the commit connection just before this recompute.
        await original_recompute(*args, **kwargs)
        raise RuntimeError("injected failure after dependency insertion")

    monkeypatch.setattr(handler.db, "create_task", record_creation)
    monkeypatch.setattr(handler.db, "recompute_blocked", fail_after_dependency)
    result = await handler.execute(
        "task_batch_propose", {**_implementation_graph(path, decision), "_scope": asdict(scope)}
    )
    assert not result["success"] and "injected failure" in result["error"]
    assert {task.id for task in await handler.db.list_tasks(project_id="p1")} == {scope.task_id}
    assert len(created) == 6
    assert await existing_graph_edges(handler.db, "p1") == []
    # Nothing survives for an operator to commit by hand either.
    assert await _proposal_rows(handler) == []
    assert not [event for event in _emitted(handler) if event[0] == "proposal.status_changed"]
    monkeypatch.setattr(handler.db, "create_task", original)
    monkeypatch.setattr(handler.db, "recompute_blocked", original_recompute)
    retried = await _ingest_propose(handler, path, scope, decision)
    assert retried["status"] == "held"


@pytest.mark.parametrize(
    "invalid", ["flat", "container-edge", "duplicate-parent", "profile", "edit", "existing-edge"],
)
async def test_ingestion_refuses_invalid_graph_before_publishing(handler, invalid):
    path, scope, decision = await _ingest_assignment(handler)
    graph = _implementation_graph(path, decision)
    if invalid == "edit":
        # Ingest authority is ungated, so it may only create work.
        graph["edits"] = [{"task_id": scope.task_id, "title": "Renamed by ingestion"}]
    elif invalid == "existing-edge":
        graph["edges"].append({"from": scope.task_id, "to": "api", "dep_type": "blocks"})
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
    assert (await handler.db.get_task(scope.task_id)).title == (
        f"Ingest approved decision {decision['decision_id']}"
    )
    # A refused batch leaves no staged proposal behind.
    assert await _proposal_rows(handler) == []


async def test_source_text_cannot_grant_ungated_authority(handler):
    path, scope, decision = await _ingest_assignment(handler)
    ordinary = await handler.execute("task_batch_propose", _implementation_graph(path, decision))
    refused = await handler.execute("task_batch_commit", {"proposal_id": ordinary["proposal_id"]})
    assert refused["not_approved"]
    assert await check_request_scope(
        "task_batch_commit", {"proposal_id": ordinary["proposal_id"]}, scope, db=handler.db,
    ) == "out of scope: proposal does not belong to held ingestion task"
    await handler.db.update_agent("ingester", state=AgentState.IDLE, current_task_id=None)
    refused = await handler.execute(
        "task_batch_propose", {**_implementation_graph(path, decision), "_scope": asdict(scope)},
    )
    assert not refused["success"]
    assert "live role assignment" in refused["error"]


async def test_ingestion_scope_pins_project_session_and_approved_source(handler):
    path, scope, decision = await _ingest_assignment(handler)
    assert await check_request_scope("list_tasks", {}, scope, db=handler.db) is None
    assert await check_request_scope(
        "list_tasks", {"project_id": "other"}, scope, db=handler.db,
    ) == "out of scope: project_id mismatch"
    assert await check_request_scope(
        "task_batch_propose", {"session_id": "other"}, scope, db=handler.db,
    ) == "out of scope: session_id mismatch"
    graph = _implementation_graph(path, decision)
    graph["source"] = "spec:another-path"
    refused = await handler.execute("task_batch_propose", {**graph, "_scope": asdict(scope)})
    assert not refused["success"] and "source must match" in refused["error"]
    path.write_text("---\nstatus: draft\nspec_kind: implementation\n---\n# Unapproved\n")
    refused = await handler.execute(
        "task_batch_propose", {**_implementation_graph(path, decision), "_scope": asdict(scope)},
    )
    assert refused["success"] and refused["status"] == "held", refused


async def test_ingest_packet_is_pinned_scoped_and_replay_deduplicates_archived_graph(handler):
    path, scope, decision = await _ingest_assignment(handler)
    args = {"project_id": "p1", "decision_id": decision["decision_id"],
            "packet_sha256": decision["packet_sha256"]}
    path.unlink()
    read = await handler.execute("approved_spec_packet", {**args, "_scope": asdict(scope)})
    assert read["success"] and "# Approved spec" in read["body"], read
    refused = await handler.execute("approved_spec_packet", args)
    assert not refused["success"]
    for override in ({"packet_sha256": "0" * 64}, {"decision_id": "other"},
                     {"project_id": "other"}):
        refused = await handler.execute("approved_spec_packet", {
            **args, **override, "_scope": asdict(scope),
        })
        assert not refused.get("success"), refused
    result = await _ingest_propose(handler, path, scope, decision)
    for tid in reversed(result["task_ids"]):
        await handler.db.update_task(tid, status=TaskStatus.COMPLETED)
    for tid in result["task_ids"]:
        if await handler.db.get_task(tid):
            assert await handler.db.archive_task(tid)
    replay = await _ingest_propose(handler, path, scope, decision)
    assert replay["replayed"] and replay["members"] == result["members"]
    assert len(await handler.db.list_tasks(project_id="p1")) == 1
    await handler.db.update_task(scope.task_id, status=TaskStatus.COMPLETED)
    await handler.db.update_session(scope.session_id, state="stopped", desired_state="stopped")
    await handler.db.update_agent("ingester", state=AgentState.IDLE, current_task_id=None)
    assert await handler.db.archive_task(scope.task_id)
    ensured = await handler.execute("ensure_spec_ingest", args)
    assert ensured == {"success": True, "task_id": scope.task_id, "created": False}
    assert await handler.db.list_tasks(project_id="p1") == []


@pytest.mark.parametrize("coverage", [None, {"leaf_keys": ["api", "final"]}])
async def test_accepted_external_prerequisite_covers_leaves_atomically(handler, coverage):
    path, scope, decision = await _ingest_assignment(
        handler, feedback="Required external prerequisite for implementation", coverage=coverage,
    )
    args = {"project_id": "p1", "decision_id": decision["decision_id"],
            "packet_sha256": decision["packet_sha256"], "_scope": asdict(scope)}
    path.write_text("---\nstatus: draft\n---\nA completely changed mirror")
    packet = await handler.execute("approved_spec_packet", args)
    assert packet["success"] and "# Approved spec" in packet["body"], packet
    [obligation] = packet["feedback"]
    target = obligation["task_id"]
    before = {t.id for t in await handler.db.list_tasks(project_id="p1")}
    result = await _ingest_propose(handler, path, scope, decision)
    ids = dict(zip(["core", "api", "ui", "docs", "draft", "final"], result["task_ids"], strict=True))
    edges = await existing_graph_edges(handler.db, "p1")
    covered = {frm for frm, to, kind in edges if to == target and kind == "blocks"}
    expected = (coverage or {"leaf_keys": ["api", "ui", "draft", "final"]})["leaf_keys"]
    assert covered == {ids[key] for key in expected}
    assert {t.id for t in await handler.db.list_tasks(project_id="p1")} == before | set(ids.values())
    proposal = await get_proposal(handler.db, result["proposal_id"])
    assert proposal["payload"]["spec_ingest"]["feedback"] == [obligation]
    for tid in covered:
        assert (await handler.db.get_task(tid)).is_blocked
    replay = await _ingest_propose(handler, path, scope, decision)
    assert replay["task_ids"] == result["task_ids"]


async def test_nonblocking_feedback_keeps_archived_work_identity_without_recreating_it(handler):
    path, scope, decision = await _ingest_assignment(
        handler, feedback="Add an example to the operator guide", required=False,
    )
    async with handler.db._engine.connect() as conn:
        [original] = await handler.db.list_review_feedback_obligations(decision["decision_id"], conn=conn)
    target = original["task_id"]
    await handler.db.update_task(target, status=TaskStatus.COMPLETED)
    assert await handler.db.archive_task(target)
    packet = await handler.execute("approved_spec_packet", {
        "project_id": "p1", "decision_id": decision["decision_id"],
        "packet_sha256": decision["packet_sha256"], "_scope": asdict(scope),
    })
    assert packet["success"], packet
    [obligation] = packet["feedback"]
    assert obligation["task_id"] == target and obligation["acceptance_text"]
    result = await _ingest_propose(handler, path, scope, decision)
    assert {t.id for t in await handler.db.list_tasks(project_id="p1")} == {
        scope.task_id, *result["task_ids"],
    }
    for tid in result["task_ids"]:
        stamp = await handler.db.get_task_meta(tid, "spec_ingest")
        assert stamp["feedback"] == [obligation]
    assert not any(to == target for _, to, _ in await existing_graph_edges(handler.db, "p1"))


async def test_withdrawn_review_only_replays_existing_ingestion_receipt(handler):
    from sqlalchemy import update
    from src.database.tables import doc_reviews

    path, scope, decision = await _ingest_assignment(
        handler, feedback="Add an example to the operator guide", required=False,
    )
    # Model a historical source transition. Current review commands correctly
    # forbid withdrawing an approved review; its durable receipt is still inert.
    async with handler.db.immediate() as conn:
        await conn.execute(update(doc_reviews).where(
            doc_reviews.c.id == decision["review_id"],
        ).values(state="withdrawn"))
    args = {"project_id": "p1", "decision_id": decision["decision_id"],
            "packet_sha256": decision["packet_sha256"]}
    assert await handler.execute("ensure_spec_ingest", args) == {
        "success": True, "created": False, "task_id": scope.task_id,
    }
    refused = await handler.execute("task_batch_propose", {
        **_implementation_graph(path, decision), "_scope": asdict(scope),
    })
    assert not refused["success"], refused
    assert await _proposal_rows(handler) == []


@pytest.mark.parametrize("damage", ["failed", "archived", "container", "cross-project", "coverage"])
async def test_invalid_external_prerequisite_refuses_entire_graph(handler, damage):
    path, scope, decision = await _ingest_assignment(
        handler, feedback="Required external prerequisite",
        coverage={"leaf_keys": ["nonexistent"]} if damage == "coverage" else None,
    )
    async with handler.db._engine.connect() as conn:
        obligations = await handler.db.list_review_feedback_obligations(decision["decision_id"], conn=conn)
    target = obligations[0]["task_id"]
    if damage == "failed":
        await handler.db.update_task(target, status=TaskStatus.FAILED)
    elif damage == "archived":
        await handler.db.update_task(target, status=TaskStatus.COMPLETED)
        assert await handler.db.archive_task(target)
    elif damage == "container":
        from src.models import Task
        await handler.db.create_task(Task(id="prereq-child", project_id="p1", title="Child", description="",
                                          parent_task_id=target))
    elif damage == "cross-project":
        await handler.execute("create_project", {"id": "p2", "name": "p2"})
        await handler.db.update_task(target, project_id="p2")
    before = {t.id for t in await handler.db.list_tasks(project_id="p1")}
    result = await handler.execute("task_batch_propose", {
        **_implementation_graph(path, decision), "_scope": asdict(scope),
    })
    assert not result["success"], result
    assert {t.id for t in await handler.db.list_tasks(project_id="p1")} == before
    assert await _proposal_rows(handler) == []


async def test_legacy_assignment_only_replays_its_existing_committed_receipt(handler):
    from sqlalchemy import delete, update
    from src.database.tables import ingest_batch_members, ingest_batches, task_metadata, task_proposals

    path, scope, decision = await _ingest_assignment(handler, "design")
    result = await _ingest_propose(handler, path, scope, decision)
    proposal = await get_proposal(handler.db, result["proposal_id"])
    proposal["payload"]["spec_ingest"] = {"task_id": scope.task_id, "spec_path": str(path)}
    async with handler.db.immediate() as conn:
        # Historical committed proposals predate both batch tables.
        await conn.execute(delete(ingest_batch_members).where(
            ingest_batch_members.c.batch_id == result["batch_id"],
        ))
        await conn.execute(delete(ingest_batches).where(
            ingest_batches.c.batch_id == result["batch_id"],
        ))
        await conn.execute(delete(task_metadata).where(
            task_metadata.c.task_id == scope.task_id, task_metadata.c.key == "spec_ingest",
        ))
        await conn.execute(update(task_proposals).where(task_proposals.c.id == result["proposal_id"])
                           .values(payload=json.dumps(proposal["payload"])))
    await handler.db.update_task(scope.task_id, dedup_key=f"spec-ingest:{path}")
    inventory = await handler.execute("spec_ingest_legacy_inventory", {"project_id": "p1"})
    assert inventory["success"] and [r["id"] for r in inventory["assignments"]] == [scope.task_id]
    replay = await _ingest_commit(handler, result["proposal_id"], scope)
    assert replay["already_committed"] and replay["task_ids"] == result["task_ids"]
    refused = await handler.execute("task_batch_propose", {
        **_implementation_graph(path, decision), "_scope": asdict(scope),
    })
    assert not refused["success"], refused


@pytest.mark.parametrize("missing,code", [
    ("section_anchor", "section_anchor_missing"),
    ("primary_outcome", "primary_outcome_missing"),
    ("acceptance", "acceptance_missing"),
])
async def test_ingest_propose_hard_failure_creates_nothing(handler, missing, code):
    from sqlalchemy import select
    from src.database.tables import ingest_batches, ingest_batch_members, integration_outbox

    path, scope, decision = await _ingest_assignment(handler)
    graph = _implementation_graph(path, decision)
    del graph["tasks"][1][missing]
    result = await handler.execute("task_batch_propose", {**graph, "_scope": asdict(scope)})
    assert not result["success"] and result["code"] == code
    assert await _proposal_rows(handler) == []
    assert {task.id for task in await handler.db.list_tasks(project_id="p1")} == {scope.task_id}
    async with handler.db.immediate() as conn:
        for table in (ingest_batches, ingest_batch_members):
            assert not (await conn.execute(select(table))).all()
        assert not (await conn.execute(select(integration_outbox).where(
            integration_outbox.c.event_type == "batch.held",
        ))).all()


async def test_ingest_operational_root_has_hold_membership_and_delivery_condition(handler):
    from sqlalchemy import select
    from src.database.queries import ingest_batch_queries as batches
    from src.database.tables import tasks

    path, scope, decision = await _ingest_assignment(handler)
    graph = _implementation_graph(path, decision)
    graph["tasks"].append({
        "tempId": "observe", "title": "Observe deployment", "description": "Observe landed API",
        "section_anchor": "Rollout", "primary_outcome": "Verify deployed behavior",
        "acceptance": "Observe the deployed API", "placement": "operational_root",
        "delivery_targets": ["api"], "size": "s", "constraints": ["Wait for delivery"],
        "owned_modules": [],
    })
    result = await _ingest_propose(handler, path, scope, decision, graph)
    root, api = result["members"]["observe"], result["members"]["api"]
    assert (await handler.db.get_task(root)).parent_task_id is None
    assert await handler.db.get_task_meta(root, "delivery_condition") == {
        "kind": "delivery-of", "targets": [api],
    }
    assert (root, api, "delivery-of") in await existing_graph_edges(handler.db, "p1")
    async with handler.db.immediate() as conn:
        assert (await batches.batch_of_task(conn, root))["role"] == "operational_root"
        assert (await conn.execute(select(tasks.c.held_batch_id).where(
            tasks.c.id == root,
        ))).scalar_one() == result["batch_id"]
    assert await handler.db.get_task_meta(api, "owned_modules") == ["src/api.py"]


@pytest.mark.parametrize("state", ["held", "reviewing", "needs_scope"])
async def test_ingest_commit_and_discard_preserve_held_batch(handler, state):
    from src.database.queries import ingest_batch_queries as batches

    path, scope, decision = await _ingest_assignment(handler)
    result = await _ingest_propose(handler, path, scope, decision)
    async with handler.db.immediate() as conn:
        assert (await batches.set_state(conn, result["batch_id"], state))["success"]
    committed = await _ingest_commit(handler, result["proposal_id"], scope)
    assert not committed["success"] and committed["error_code"] == "fidelity_review_required"
    assert committed["state"] == state
    discarded = await handler.execute("task_batch_discard", {"proposal_id": result["proposal_id"]})
    assert not discarded["success"] and discarded["error_code"] == "batch_retire_required"
    assert (await get_proposal(handler.db, result["proposal_id"]))["status"] == "held"


async def test_ingest_concurrent_propose_creates_one_batch(handler):
    import asyncio
    from sqlalchemy import select
    from src.database.tables import ingest_batches, integration_outbox

    path, scope, decision = await _ingest_assignment(handler)
    args = {**_implementation_graph(path, decision), "_scope": asdict(scope)}
    one, two = await asyncio.gather(*(handler.execute("task_batch_propose", args) for _ in range(2)))
    assert one["success"] and two["success"], (one, two)
    assert one["batch_id"] == two["batch_id"] and one["members"] == two["members"]
    assert one["replayed"] != two["replayed"]
    async with handler.db.immediate() as conn:
        assert len((await conn.execute(select(ingest_batches))).all()) == 1
        assert len((await conn.execute(select(integration_outbox).where(
            integration_outbox.c.event_type == "batch.held",
        ))).all()) == 1


async def test_changed_approved_source_returns_previous_receipt_without_duplicating_work(handler):
    from dataclasses import replace

    path, scope, decision = await _ingest_assignment(handler)
    original = await _ingest_propose(handler, path, scope, decision)
    path.write_text("---\nstatus: approved\nspec_kind: implementation\n---\n# Revised spec\n")
    revised = await handler.execute("spec_approve", {"project_id": "p1", "spec_path": str(path)})
    assert revised["success"], revised
    ensured = await handler.execute("ensure_spec_ingest", {
        "project_id": "p1", "decision_id": revised["decision_id"],
        "packet_sha256": revised["packet_sha256"],
    })
    assert ensured["success"] and ensured["task_id"] != scope.task_id
    await handler.db.update_task(scope.task_id, status=TaskStatus.COMPLETED, assigned_agent_id=None)
    await handler.db.update_task(ensured["task_id"], status=TaskStatus.IN_PROGRESS,
                                 assigned_agent_id="ingester")
    await handler.db.update_agent("ingester", current_task_id=ensured["task_id"])
    await handler.db.update_session(scope.session_id, task_id=ensured["task_id"])
    scope = replace(scope, task_id=ensured["task_id"])
    result = await handler.execute("task_batch_propose", {
        **_implementation_graph(path, revised), "_scope": asdict(scope),
    })
    assert not result["success"] and result["error_code"] == "incremental_plan_required", result
    assert result["previous_receipt"]["members"] == original["members"]
    assert await _proposal_rows(handler) == [(original["proposal_id"], "held")]


async def test_ingest_dispatch_failure_rolls_back_batch_graph_and_hold(handler, monkeypatch):
    from src.commands import proposal_commands
    from sqlalchemy import select
    from src.database.tables import ingest_batches, ingest_batch_members

    path, scope, decision = await _ingest_assignment(handler)
    original = proposal_commands.enqueue_integration_event

    async def refuse_dispatch(*args, **kwargs):
        raise RuntimeError("outbox insertion failed")

    monkeypatch.setattr(proposal_commands, "enqueue_integration_event", refuse_dispatch)
    failed = await handler.execute("task_batch_propose", {
        **_implementation_graph(path, decision), "_scope": asdict(scope),
    })
    assert not failed["success"] and "outbox insertion failed" in failed["error"]
    assert await _proposal_rows(handler) == []
    assert {task.id for task in await handler.db.list_tasks(project_id="p1")} == {scope.task_id}
    async with handler.db.immediate() as conn:
        assert not (await conn.execute(select(ingest_batches))).all()
        assert not (await conn.execute(select(ingest_batch_members))).all()
    monkeypatch.setattr(proposal_commands, "enqueue_integration_event", original)
    assert (await _ingest_propose(handler, path, scope, decision))["status"] == "held"


async def _editable_batch(handler, *, receipt=True):
    from src.database.queries import ingest_batch_queries as batches

    path, scope, decision = await _ingest_assignment(handler)
    batch = await _ingest_propose(handler, path, scope, decision)
    if receipt:
        async with handler.db.immediate() as conn:
            recorded = await batches.record_receipt(
                conn, batch["batch_id"], structure_revision=1,
                structure_digest=batch["structure_digest"], reviewer_task_id="review",
                reviewer_session_id="review-session", passing=True,
                section_verdicts={"all": "pass"}, reverse_coverage_verdict="pass",
                walkthrough_verdict="pass", structural_findings=[],
            )
            await batches.set_state(conn, batch["batch_id"], "reviewing")
        batch["test_receipt_id"] = recorded["receipt"]["receipt_id"]
    return batch, scope, decision


async def _scoped_update(handler, batch, operations, **kwargs):
    return await handler.execute("task_batch_update", {
        "proposal_id": batch["batch_id"], "expected_revision": batch["structure_revision"],
        "operations": operations, **kwargs,
    })


async def _live_batch(handler, batch):
    from src.database.queries import ingest_batch_queries as batches

    async with handler.db.immediate() as conn:
        return await batches.get_batch(conn, batch["batch_id"])


@pytest.mark.parametrize("changes", [
    {"description": "A different implementation outcome"}, {"acceptance": ["A stronger check"]},
    {"constraints": ["Preserve an additional invariant"]}, {"section_anchor": "New section"},
    {"primary_outcome": "New outcome"}, {"owned_modules": ["src/other.py"]},
])
async def test_scoped_update_bumps_revision(handler, changes):
    from src.database.queries import ingest_batch_queries as batches

    batch, _, _ = await _editable_batch(handler)
    result = await _scoped_update(handler, batch, [{"kind": "rewrite", "task_id": "api",
                                                    "changes": changes}])
    assert result["success"] and result["structure_revision"] == 2, result
    assert result["state"] == "held"
    current = await _live_batch(handler, batch)
    assert current["check_results"]["superseded_receipts"] == {batch["test_receipt_id"]: 2}
    assert current["check_results"]["last_diff"] == result["diff"]
    async with handler.db.immediate() as conn:
        assert await batches.passing_receipt(conn, batch["batch_id"], 2) is None
        assert (await batches.passing_receipt(conn, batch["batch_id"], 1))["passing"]
        assert await batches.live_digest(conn, batch["batch_id"]) == result["structure_digest"]


async def test_nonstructural_update_keeps_receipt(handler):
    from src.database.queries import ingest_batch_queries as batches

    batch, _, _ = await _editable_batch(handler)
    api = await handler.db.get_task(batch["members"]["api"])
    result = await _scoped_update(handler, batch, [{"kind": "rewrite", "task_id": "api",
        "changes": {"title": "Clearer API title", "priority": 12, "tags": ["clarified"],
                    "description": "  " + api.description.replace(" ", " \n ") + "\n"}}])
    assert result["success"] and result["structure_revision"] == 1, result
    assert result["structure_digest"] == batch["structure_digest"]
    assert result["state"] == "reviewing"
    async with handler.db.immediate() as conn:
        assert (await batches.passing_receipt(conn, batch["batch_id"], 1))["receipt_id"] == (
            batch["test_receipt_id"]
        )
    assert not (await _live_batch(handler, batch))["check_results"].get("superseded_receipts")


@pytest.mark.parametrize("kind", ["rewrite", "reparent", "rewire", "retire", "move_to_root"])
async def test_update_refused_outside_batch(handler, kind):
    batch, scope, _ = await _editable_batch(handler)
    op = {
        "rewrite": {"task_id": scope.task_id, "changes": {"title": "Smuggled"}},
        "reparent": {"task_id": "api", "parent_id": scope.task_id},
        "rewire": {"edges": [{"from": "api", "to": scope.task_id}]},
        "retire": {"task_id": "api", "reason": "duplicate", "replacement_task_id": scope.task_id},
        "move_to_root": {"task_id": "draft", "delivery_targets": [scope.task_id]},
    }[kind]
    result = await _scoped_update(handler, batch, [{"kind": kind, **op}])
    assert not result["success"] and result["error_code"] == "membership", result
    assert (await _live_batch(handler, batch))["structure_revision"] == 1


async def test_scoped_update_stale_revision_claim_and_pins_refused(handler):
    batch, _, _ = await _editable_batch(handler)
    op = {"kind": "rewrite", "task_id": "api", "changes": {"title": "New title"}}
    stale = await _scoped_update(handler, batch, [op], expected_revision=2)
    assert stale["error_code"] == "stale_revision", stale
    pinned = await _scoped_update(handler, batch, [{**op, "changes": {"profile_id": "reviewer"}}])
    assert pinned["error_code"] == "routing.choice_forbidden", pinned
    await handler.db.create_agent(Agent(id="live-batch", name="live-batch", profile_id="spec-ingest"))
    await handler.db.update_task(batch["members"]["ui"], assigned_agent_id="live-batch")
    live = await _scoped_update(handler, batch, [op])
    assert live["error_code"] == "active_claim", live
    assert (await handler.db.get_task(batch["members"]["api"])).title == "API"


async def test_scoped_update_rolls_back_fidelity_membership_revision_and_receipts(handler):
    from src.database.queries import ingest_batch_queries as batches

    batch, _, _ = await _editable_batch(handler)
    original = await _live_batch(handler, batch)
    result = await _scoped_update(handler, batch, [
        {"kind": "rewrite", "task_id": "api", "changes": {"title": "Must roll back"}},
        {"kind": "add_leaf", "task": {"tempId": "bad", "parent_id": "core",
                                     "title": "Missing acceptance", "description": "bad",
                                     "section_anchor": "API", "primary_outcome": "bad"}},
    ])
    assert result["error_code"] == "validation_failed", result
    assert (await handler.db.get_task(batch["members"]["api"])).title == "API"
    assert await _live_batch(handler, batch) == original
    async with handler.db.immediate() as conn:
        assert await batches.member_map(conn, batch["batch_id"]) == batch["members"]


def _new_scoped_leaf(key, **fields):
    return {"tempId": key, "title": key, "description": "Implement " + key,
            "section_anchor": "Implementation / " + key, "primary_outcome": key,
            "acceptance": ["Verify " + key], "constraints": ["Keep original goals"],
            "size": "s", **fields}


async def test_scoped_add_rewire_reparent_and_operational_move(handler):
    from src.database.queries import ingest_batch_queries as batches

    batch, _, _ = await _editable_batch(handler)
    result = await _scoped_update(handler, batch, [
        {"kind": "add_leaf", "task": _new_scoped_leaf("extra", parent_id="core")},
        {"kind": "reparent", "task_id": "ui", "parent_id": "docs"},
        {"kind": "rewire", "edges": [{"from": "extra", "to": "api", "reason": "uses API"}]},
        {"kind": "move_to_root", "task_id": "draft", "delivery_targets": ["core"]},
    ])
    assert result["success"] and result["structure_revision"] == 2, result
    assert (await handler.db.get_task(batch["members"]["draft"])).parent_task_id is None
    assert (await handler.db.get_task(batch["members"]["ui"])).parent_task_id == batch["members"]["docs"]
    new_id = result["members"]["extra"]
    assert await handler.db.ingest_held_task_ids({new_id}) == {new_id}
    assert set(await handler.db.get_typed_dependencies(new_id)) == {
        (batch["members"]["api"], "blocks"), (batch["members"]["core"], "parent-child"),
    }
    async with handler.db.immediate() as conn:
        members = {m["member_key"]: m for m in await batches.members(conn, batch["batch_id"])}
    assert members["extra"]["created_revision"] == 2
    assert await handler.db.get_task_meta(new_id, "proposal_id") == batch["batch_id"]
    assert (await handler.db.get_task_meta(new_id, "spec_ingest"))["decision_id"]
    assert members["draft"]["role"] == "operational_root"
    assert await handler.db.get_task_meta(batch["members"]["draft"], "delivery_condition") == {
        "kind": "delivery-of", "targets": [batch["members"]["core"]],
    }


@pytest.mark.parametrize("kind", ["split", "combine", "retire"])
async def test_scoped_retirement_preserves_history_and_typed_dependencies(handler, kind):
    from src.database.queries import ingest_batch_queries as batches

    batch, _, _ = await _editable_batch(handler)
    if kind == "split":
        op = {"kind": kind, "task_id": "api", "reason": "separate outcomes",
              "tasks": [_new_scoped_leaf("api_one"), _new_scoped_leaf("api_two")]}
        retired, successors = ["api"], ["api_one", "api_two"]
    elif kind == "combine":
        op = {"kind": kind, "task_ids": ["api", "ui"], "reason": "one cohesive outcome",
              "task": _new_scoped_leaf("combined")}
        retired, successors = ["api", "ui"], ["combined"]
    else:
        op = {"kind": kind, "task_id": "api", "reason": "same work as UI",
              "replacement_task_id": "ui"}
        retired, successors = ["api"], ["ui"]
    result = await _scoped_update(handler, batch, [op])
    assert result["success"] and result["structure_revision"] == 2, result
    async with handler.db.immediate() as conn:
        members = {m["member_key"]: m for m in await batches.members(conn, batch["batch_id"])}
    for key in retired:
        tid = batch["members"][key]
        assert await handler.db.get_task(tid) is None
        archived = await handler.db.get_archived_task(tid)
        assert archived["description"].endswith("API files and tests" if key == "api" else "UI files and tests")
        assert members[key]["state"] == "retired" and members[key]["retired_revision"] == 2
        assert await handler.db.get_task_completions(tid) == []
        history = (await handler.db.list_task_comments(tid, project_id="p1"))["comments"]
        assert any(c["body"].startswith("Retirement evidence:") for c in history)
    dependencies = await handler.db.get_dependencies(
        batch["members"]["final"], dep_types=frozenset({"blocks", "conditional-blocks"}),
    )
    assert dependencies == {result["members"][key] for key in successors}


async def test_scoped_retirement_rejects_external_implicit_rewire_and_rolls_back(handler):
    batch, scope, _ = await _editable_batch(handler)
    await handler.db.add_dependency(scope.task_id, batch["members"]["api"], "related")
    from src.database.queries import ingest_batch_queries as batches
    async with handler.db.immediate() as conn:
        await batches.store_live_digest(conn, batch["batch_id"])
    result = await _scoped_update(handler, batch, [{"kind": "retire", "task_id": "api",
        "reason": "duplicate", "replacement_task_id": "ui"}])
    assert result["error_code"] == "membership", result
    assert await handler.db.get_task(batch["members"]["api"])
    assert (await _live_batch(handler, batch))["structure_revision"] == 1


async def test_scoped_retirement_and_rewrite_roll_back_on_late_hard_check(handler):
    batch, _, _ = await _editable_batch(handler)
    original = await _live_batch(handler, batch)
    result = await _scoped_update(handler, batch, [
        {"kind": "retire", "task_id": "api", "reason": "duplicate", "replacement_task_id": "ui"},
        {"kind": "rewrite", "task_id": "ui", "changes": {"acceptance": []}},
    ])
    assert result["error_code"] == "validation_failed", result
    assert await handler.db.get_task(batch["members"]["api"])
    assert await handler.db.get_archived_task(batch["members"]["api"]) is None
    assert await handler.db.get_dependencies(
        batch["members"]["final"], dep_types=frozenset({"blocks"}),
    ) == {batch["members"]["api"]}
    assert await _live_batch(handler, batch) == original


async def test_scoped_update_refuses_ingester_and_resumes_answered_scope_without_release(handler):
    from sqlalchemy import select, update
    from src.database.queries import ingest_batch_queries as batches
    from src.database.tables import ask_decisions, human_asks

    batch, scope, decision = await _editable_batch(handler)
    op = {"kind": "rewrite", "task_id": "api", "changes": {"title": "New title"}}
    refused = await _scoped_update(handler, batch, [op], _scope=asdict(scope))
    assert refused["error_code"] == "capability_denied", refused
    async with handler.db.immediate() as conn:
        await batches.set_state(conn, batch["batch_id"], "needs_scope")
    refused = await _scoped_update(handler, batch, [op])
    assert refused["error_code"] == "needs_scope", refused
    # The source adapter's applied, generation-bound answer is authoritative;
    # merely being a supervisor or having an unanswered ask is insufficient.
    async with handler.db.immediate() as conn:
        ask_id = (await conn.execute(select(ask_decisions.c.ask_id).where(
            ask_decisions.c.id == decision["decision_id"],
        ))).scalar_one()
        await conn.execute(update(human_asks).where(human_asks.c.id == ask_id).values(
            source_key=f"batch-scope:{batch['batch_id']}:1", decision_state="answered",
            application_state="applied",
        ))
    result = await _scoped_update(handler, batch, [op])
    assert result["success"] and result["state"] == "held", result
    assert await handler.db.ingest_held_task_ids(set(result["task_ids"])) == set(result["task_ids"])
    assert (await _live_batch(handler, batch))["release_receipt"] is None


async def test_scoped_update_with_real_role_reviewer_and_informational_edges(handler):
    from tests.test_fidelity_review import _held_batch, _ensure, _reviewer_session

    batch, _ = await _held_batch(handler)
    reviewer = (await _ensure(handler, batch))["task_id"]
    scope = await _reviewer_session(handler, reviewer, "s-edit")
    result = await _scoped_update(handler, batch, [{
        "kind": "rewrite", "task_id": "api", "changes": {"acceptance": ["New proof"]},
    }], _scope=asdict(scope))
    assert result["success"] and result["structure_revision"] == 2, result
    assert result["state"] in {"held", "reviewing"}
    assert await handler.db.ingest_held_task_ids(set(batch["task_ids"])) == set(batch["task_ids"])


@pytest.mark.parametrize("kind", ["cycle", "container", "root", "nested_pin"])
async def test_scoped_graph_refusal_rolls_back_all_writes(handler, kind):
    batch, _, _ = await _editable_batch(handler)
    op = {"kind": "rewire", "edges": [{"from": "api", "to": "final"}]}
    if kind == "container":
        op["edges"] = [{"from": "core", "to": "api"}]
    elif kind == "root":
        op = {"kind": "add_leaf", "task": _new_scoped_leaf("unplaced")}
    elif kind == "nested_pin":
        op = {"kind": "add_leaf", "task": _new_scoped_leaf(
            "pinned", parent_id="core", profile_id="reviewer",
        )}
    old_edges = await existing_graph_edges(handler.db, "p1")
    result = await _scoped_update(handler, batch, [
        {"kind": "rewrite", "task_id": "ui", "changes": {"title": "Must roll back"}}, op,
    ])
    assert not result["success"], result
    assert result["error_code"] == ("routing.choice_forbidden" if kind == "nested_pin"
                                    else "validation_failed"), result
    assert (await handler.db.get_task(batch["members"]["ui"])).title == "UI"
    assert (await _live_batch(handler, batch))["structure_revision"] == 1
    assert await existing_graph_edges(handler.db, "p1") == old_edges


async def test_scoped_operational_targets_are_replaced_and_edits_remain_held(handler):
    batch, _, _ = await _editable_batch(handler)
    first = await _scoped_update(handler, batch, [{
        "kind": "move_to_root", "task_id": "draft", "delivery_targets": ["core"],
    }])
    assert first["success"], first
    second = await _scoped_update(handler, first, [{
        "kind": "move_to_root", "task_id": "draft", "delivery_targets": ["api"],
    }])
    assert second["success"] and second["structure_revision"] == 3, second
    tid = batch["members"]["draft"]
    assert await handler.db.get_typed_dependencies(tid) == [(batch["members"]["api"], "delivery-of")]
    assert await handler.db.ingest_held_task_ids({tid}) == {tid}
    repeated = await _scoped_update(handler, second, [{
        "kind": "move_to_root", "task_id": "draft", "delivery_targets": ["api"],
    }])
    assert repeated["success"], repeated
    assert await handler.db.get_typed_dependencies(tid) == [(batch["members"]["api"], "delivery-of")]


@pytest.mark.parametrize("churn", ["epoch", "stop"])
async def test_scoped_update_rechecks_reviewer_after_writer_lock(handler, monkeypatch, churn):
    from src.commands import proposal_commands
    from tests.test_fidelity_review import _held_batch, _ensure, _reviewer_session

    batch, _ = await _held_batch(handler)
    reviewer = (await _ensure(handler, batch))["task_id"]
    scope = await _reviewer_session(handler, reviewer, "s-edit-race")
    original = proposal_commands.fidelity_review_task_for_session

    async def discover(*args, **kwargs):
        held = await original(*args, **kwargs)
        if churn == "epoch":
            await handler.db.update_task(reviewer, claim_epoch=1)
        else:
            await handler.db.update_session(scope.session_id, state="stopped", desired_state="stopped")
        return held

    monkeypatch.setattr(proposal_commands, "fidelity_review_task_for_session", discover)
    refused = await _scoped_update(handler, batch, [{
        "kind": "rewrite", "task_id": "api", "changes": {"title": "Stale edit"},
    }], _scope=asdict(scope))
    assert refused["error_code"] == "authority", refused
    assert (await handler.db.get_task(batch["members"]["api"])).title == "API"


async def test_reviewer_dispatch_refuses_to_hide_unversioned_batch_drift(handler):
    from tests.test_fidelity_review import _held_batch, _ensure

    batch, _ = await _held_batch(handler)
    await handler.db.update_task(batch["members"]["api"], description="Unversioned drift")
    refused = await _ensure(handler, batch)
    assert not refused["success"] and refused["code"] == "batch.stale_revision", refused
    assert (await _live_batch(handler, batch))["reviewer_task_id"] is None
    assert (await _live_batch(handler, batch))["structure_digest"] == batch["structure_digest"]


async def test_scoped_checks_read_updated_ownership_from_live_metadata(handler):
    from src.database.queries import ingest_batch_queries as batches

    batch, _, _ = await _editable_batch(handler)
    result = await _scoped_update(handler, batch, [{
        "kind": "rewrite", "task_id": key, "changes": {"owned_modules": ["src/shared.py"]},
    } for key in ("api", "ui", "draft", "final")])
    assert result["success"], result
    async with handler.db.immediate() as conn:
        members, _ = await batches.hydrate_structure(conn, batch["batch_id"])
    assert next(m for m in members if m["member_key"] == "api")["owned_modules"] == ["src/shared.py"]
    checks = (await _live_batch(handler, batch))["check_results"]
    assert any(f["code"] == "concentrated_ownership" for f in checks["advisory"])


async def _release_ready(handler, *, external=False, document_review=False):
    """Use the real reviewer authority and attestation command before release."""
    from tests.test_fidelity_review import _review_request, _record, _ensure, _reviewer_session
    from src.database.queries import ingest_batch_queries as batches

    if document_review:
        path, ingest_scope, decision = await _ingest_assignment(handler, feedback="Keep constraints")
        batch = await _ingest_propose(handler, path, ingest_scope, decision)
        assert batch["success"], batch
        await handler.db.create_profile(AgentProfile(
            id="fidelity-reviewer", name="Fidelity Reviewer", harness="codex",
            default_class="deep-high", needs_workspace=False,
            aq_commands=["approved_spec_packet", "task_batch_validate", "task_batch_update",
                         "task_batch_review_record"],
        ))
        reviewer = await _ensure(handler, batch)
        assert reviewer["success"], reviewer
        scope = await _reviewer_session(handler, reviewer["task_id"], "s-review")
        async with handler.db.immediate() as conn:
            active = await batches.members(conn, batch["batch_id"], state="active")
        args = {
            "batch_id": batch["batch_id"], "expected_revision": batch["structure_revision"],
            "expected_digest": batch["structure_digest"],
            "section_verdicts": {m["section_anchor"]: "faithful" for m in active},
            "reverse_coverage_verdict": "faithful", "walkthrough_verdict": "faithful",
            "structural_findings": [],
        }
    else:
        batch, scope, args = await _review_request(handler)
    if external:
        await _existing(handler, "external")
        await handler.db.add_dependency(batch["members"]["api"], "external", "blocks")
        async with handler.db.immediate() as conn:
            batch["structure_digest"] = await batches.store_live_digest(conn, batch["batch_id"])
        args["expected_digest"] = batch["structure_digest"]
    recorded = await _record(handler, scope, args)
    assert recorded["success"] and recorded["passing"], recorded
    return batch, scope, recorded["receipt"]


async def _release(handler, batch, **overrides):
    return await handler.execute("task_batch_release", {
        "batch_id": batch["batch_id"], "expected_revision": batch["structure_revision"],
        "expected_digest": batch["structure_digest"], **overrides,
    })


@pytest.mark.parametrize("stale", ["revision", "digest", "live_graph"])
async def test_release_stale_digest(handler, stale):
    batch, _, _ = await _release_ready(handler)
    overrides = {}
    if stale == "revision":
        overrides["expected_revision"] = 2
    elif stale == "digest":
        overrides["expected_digest"] = "stale"
    else:
        await handler.db.update_task(batch["members"]["api"], description="Unreviewed work")
    result = await _release(handler, batch, **overrides)
    assert not result["success"] and result["error_code"] == "stale_revision", result
    assert result["retryable"]
    assert await handler.db.ingest_held_task_ids(set(batch["task_ids"])) == set(batch["task_ids"])
    assert (await _live_batch(handler, batch))["release_receipt"] is None


async def test_release_external_leaf_became_container(handler):
    batch, _, _ = await _release_ready(handler, external=True)
    await handler.db.set_task_meta("external", "container", True)
    result = await _release(handler, batch)
    assert not result["success"] and result["error_code"] == "container_endpoint", result
    assert await handler.db.ingest_held_task_ids(set(batch["task_ids"])) == set(batch["task_ids"])


async def test_release_replays_receipt(handler):
    from sqlalchemy import select
    from src.database.tables import events, integration_outbox

    batch, _, receipt = await _release_ready(handler)
    first = await _release(handler, batch)
    assert first["success"] and not first["already_released"], first
    row = await _live_batch(handler, batch)
    assert row["state"] == "released" and row["release_revision"] == 1
    assert row["release_digest"] == batch["structure_digest"]
    assert row["release_actor"] and row["released_at"]
    assert first["release_receipt"]["review_receipt_id"] == receipt["receipt_id"]
    assert {m["task_id"] for m in first["release_receipt"]["members"]} == set(batch["task_ids"])
    assert not await handler.db.ingest_held_task_ids(set(batch["task_ids"]))
    assert (await get_proposal(handler.db, batch["batch_id"]))["status"] == "committed"
    # Freeze replay evidence even after post-release work has changed.
    await handler.db.update_task(batch["members"]["api"], description="Post-release repair")
    again = await _release(handler, batch)
    assert again["success"] and again["already_released"], again
    assert again["release_receipt"] == first["release_receipt"]
    async with handler.db.immediate() as conn:
        notices = (await conn.execute(select(integration_outbox).where(
            integration_outbox.c.event_type.in_(["batch.released", "task.change_set_committed"]),
        ))).mappings().all()
        assert {n["event_type"] for n in notices} == {"batch.released", "task.change_set_committed"}
        assert len(notices) == 2
        assert len((await conn.execute(select(events.c.id).where(
            events.c.event_type == "task.change_set_committed",
        ))).all()) == 1
    stale = await _release(handler, batch, expected_digest="other")
    assert stale["error_code"] == "stale_revision", stale


@pytest.mark.parametrize("profile,elevated", [("spec-ingest", False), ("fidelity-reviewer", False),
                                              ("worker-codex", True), ("supervisor", False)])
async def test_release_refused_for_session_principal(handler, profile, elevated):
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import CapabilityPolicy

    batch, _, _ = await _release_ready(handler)
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION, session_id="s-review", project_id="p1",
        profile_id=profile, elevated=elevated,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["task_batch_release"]),
    )
    with principal_context(principal):
        refused = await _release(handler, batch)
    assert not refused["success"] and refused["error_code"] == "not_authorized", refused
    assert await handler.db.ingest_held_task_ids(set(batch["task_ids"])) == set(batch["task_ids"])


@pytest.mark.parametrize("kind", ["service", "playbook"])
async def test_release_admits_policy_and_refuses_wrong_project(handler, kind):
    from dataclasses import replace
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import CapabilityPolicy

    batch, _, _ = await _release_ready(handler)
    principal = ExecutionPrincipal(
        kind=PrincipalKind(kind), project_id="p2", service_name="ingestion",
        parent_run_id="run", parent_step_id="step",
        policy=CapabilityPolicy.from_namespaces(aq_commands=["task_batch_release"]),
    )
    with principal_context(principal):
        refused = await _release(handler, batch)
    assert refused["error_code"] == "not_authorized", refused
    with principal_context(replace(principal, project_id="p1")):
        result = await _release(handler, batch)
    assert result["success"] and not result["already_released"], result


@pytest.mark.parametrize("stale", [None, "instance", "stopped", "project"])
async def test_release_only_live_project_supervisor(handler, stale):
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import CapabilityPolicy

    batch, _, _ = await _release_ready(handler)
    if stale == "project":
        created = await handler.execute("create_project", {"name": "p2"})
        assert created["created"] == "p2", created
    await handler.db.create_session(SessionRecord(
        id="sup", task_id=None, project_id="p1" if stale != "project" else "p2",
        profile_id="supervisor", harness="codex", provider="fake", name="n-supervisor--p1",
        lifecycle="named", state="stopped" if stale == "stopped" else "running",
        desired_state="running", work_dir="/tmp", epoch="test", instance_token="instance",
        started_at=time.time(),
    ))
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION, session_id="sup", project_id="p1", elevated=True,
        session_instance_token="stale" if stale == "instance" else "instance",
        policy=CapabilityPolicy.from_namespaces(aq_commands=["task_batch_release"]),
    )
    with principal_context(principal):
        result = await _release(handler, batch)
    assert result["success"] is (stale is None), result
    if stale:
        assert result["error_code"] == "not_authorized", result


@pytest.mark.parametrize("invalid", ["absent", "reviewer", "digest", "section", "scope_section",
                                      "reverse", "walkthrough", "verdict"])
async def test_release_revalidates_entire_receipt(handler, invalid):
    from sqlalchemy import delete, update
    from src.database.tables import ingest_batch_receipts, ingest_batches

    batch, _, receipt = await _release_ready(handler)
    async with handler.db.immediate() as conn:
        if invalid == "absent":
            await conn.execute(delete(ingest_batch_receipts))
        elif invalid == "reviewer":
            await conn.execute(update(ingest_batches).values(reviewer_task_id="replacement"))
        else:
            changes = {}
            if invalid == "digest":
                changes["structure_digest"] = "different"
            elif invalid in {"section", "scope_section", "verdict"}:
                verdicts = dict(receipt["section_verdicts"])
                section = next(iter(verdicts))
                if invalid == "section":
                    del verdicts[section]
                else:
                    verdicts[section] = "scope_question" if invalid == "scope_section" else "pass"
                changes["section_verdicts"] = verdicts
            else:
                changes[f"{invalid}_coverage_verdict" if invalid == "reverse"
                        else "walkthrough_verdict"] = "scope_question"
            await conn.execute(update(ingest_batch_receipts).values(**changes))
    result = await _release(handler, batch)
    assert not result["success"], result
    assert result["error_code"] in {"fidelity_review_required", "incomplete_review"}, result
    assert (await _live_batch(handler, batch))["release_receipt"] is None


async def test_release_refuses_open_scope_ask_from_previous_revision(handler):
    from sqlalchemy import update
    from src.asks.batch_scope import scope_source
    from src.database.tables import human_asks
    from tests.test_fidelity_review import _review_request, _record

    batch, scope, args = await _review_request(handler)
    result = await _record(handler, scope, {**args, "walkthrough_verdict": "scope_question"})
    assert result["success"] and result["ask_id"], result
    # Preserve a real open ask while simulating a corrupted passing receipt.
    from src.database.tables import ingest_batch_receipts, ingest_batches
    async with handler.db.immediate() as conn:
        await conn.execute(update(ingest_batch_receipts).values(
            passing=True, walkthrough_verdict="faithful",
        ))
        await conn.execute(update(ingest_batches).values(state="reviewing"))
        await conn.execute(update(human_asks).where(human_asks.c.id == result["ask_id"]).values(
            source_key=f"batch-scope:{batch['batch_id']}:0",
            source_identity=scope_source(await _live_batch(handler, batch), 0).reference,
        ))
    refused = await _release(handler, batch)
    assert refused["error_code"] == "needs_scope", refused
    assert (await _live_batch(handler, batch))["state"] == "reviewing"


@pytest.mark.parametrize("change", [None, "state", "revision", "hash", "pending"])
async def test_release_refuses_diverged_approved_source(handler, change):
    from sqlalchemy import update
    from src.database.tables import doc_reviews, ingest_batches

    batch, _, _ = await _release_ready(handler, document_review=True)
    row = await _live_batch(handler, batch)
    async with handler.db.immediate() as conn:
        if change == "hash":
            await conn.execute(update(ingest_batches).where(
                ingest_batches.c.batch_id == batch["batch_id"],
            ).values(source_content_sha256="0" * 64))
        elif change:
            changes = {"state": "withdrawn"} if change == "state" else (
                {"current_revision": 2} if change == "revision"
                else {"pending_decision_id": row["decision_id"]}
            )
            await conn.execute(update(doc_reviews).where(
                doc_reviews.c.id == row["source_review_id"],
            ).values(**changes))
    result = await _release(handler, batch)
    if change:
        assert not result["success"] and result["error_code"] == "source_diverged", result
        assert await handler.db.ingest_held_task_ids(set(batch["task_ids"])) == set(batch["task_ids"])
    else:
        assert result["success"], result
        async with handler.db.immediate() as conn:
            await conn.execute(update(doc_reviews).where(
                doc_reviews.c.id == row["source_review_id"],
            ).values(state="withdrawn"))
        replay = await _release(handler, batch)
        assert replay["success"] and replay["already_released"], replay
        assert replay["release_receipt"] == result["release_receipt"]


async def test_release_refuses_active_member_claim(handler):
    batch, _, _ = await _release_ready(handler)
    await handler.db.update_task(batch["members"]["api"], assigned_agent_id="ingester")
    result = await _release(handler, batch)
    assert not result["success"] and result["error_code"] == "active_claim", result
    assert result["retryable"]


async def test_release_preserves_pauses_human_routing_and_delivery_gates(handler):
    from sqlalchemy import select
    from src.database.tables import tasks
    from src.database.queries import ingest_batch_queries as batches
    from tests.test_fidelity_review import _review_request, _record

    batch, scope, args = await _review_request(handler)
    api, ui, draft = [batch["members"][key] for key in ("api", "ui", "draft")]
    async with handler.db.immediate() as conn:
        await handler.db.set_parent(draft, None, conn=conn)
    await _existing(handler, "undelivered", status=TaskStatus.COMPLETED)
    await handler.db.add_dependency(draft, "undelivered", "delivery-of")
    await handler.db.update_task(ui, status=TaskStatus.PAUSED)
    gate = await handler.execute("gate_create", {
        "project_id": "p1", "task_id": api, "gate_type": "human", "title": "Still required",
    })
    assert gate["success"], gate
    async with handler.db.immediate() as conn:
        args["expected_digest"] = batch["structure_digest"] = await batches.store_live_digest(
            conn, batch["batch_id"],
        )
        before = (await conn.execute(select(tasks.c.id, tasks.c.status, tasks.c.profile_id).where(
            tasks.c.id.in_(batch["task_ids"]),
        ).order_by(tasks.c.id))).all()
    reviewed = await _record(handler, scope, args)
    assert reviewed["success"], reviewed
    released = await _release(handler, batch)
    assert released["success"], released
    async with handler.db.immediate() as conn:
        after = (await conn.execute(select(tasks.c.id, tasks.c.status, tasks.c.profile_id).where(
            tasks.c.id.in_(batch["task_ids"]),
        ).order_by(tasks.c.id))).all()
    assert before == after
    assert (await handler.db.get_task(ui)).status == TaskStatus.PAUSED
    assert (await handler.db.get_task(api)).is_blocked
    assert (await handler.db.get_task(draft)).is_blocked
    assert not (await handler.db.get_gate(gate["gate_id"]))["resolution"]


@pytest.mark.parametrize("failure", ["readiness", "batch_event", "commit_event"])
async def test_release_rolls_back_hold_receipt_and_notifications(handler, monkeypatch, failure):
    from src.commands import proposal_commands
    from sqlalchemy import select
    from src.database.tables import integration_outbox

    batch, _, _ = await _release_ready(handler)
    before = await _live_batch(handler, batch)
    async def fail(*args, **kwargs):
        raise RuntimeError("Release transaction fault")
    if failure == "readiness":
        monkeypatch.setattr(handler.db, "recompute_blocked", fail)
    else:
        monkeypatch.setattr(proposal_commands, "enqueue_batch_event" if failure == "batch_event"
                            else "enqueue_integration_event", fail)
    result = await _release(handler, batch)
    assert not result["success"] and "Release transaction fault" in result["error"], result
    assert await _live_batch(handler, batch) == before
    assert await handler.db.ingest_held_task_ids(set(batch["task_ids"])) == set(batch["task_ids"])
    assert (await get_proposal(handler.db, batch["batch_id"]))["status"] == "held"
    async with handler.db.immediate() as conn:
        assert not (await conn.execute(select(integration_outbox.c.id).where(
            integration_outbox.c.event_type.in_(["batch.released", "task.change_set_committed"]),
        ))).all()


def test_release_contract_binds_revision_digest_and_business_outcomes():
    from pydantic import ValidationError
    from src.commands.contracts import CONTRACTS
    from src.commands.contracts.builtin import TaskBatchReleaseArgs, _outcome_of

    contract = CONTRACTS.require("task_batch_release").contract.execution
    assert {o.name for o in contract.outcomes} == {"released", "already_released", "error"}
    assert contract.retry_safe
    for bad in (True, 0, "1"):
        with pytest.raises(ValidationError):
            TaskBatchReleaseArgs(batch_id="batch", expected_revision=bad, expected_digest="hash")
    assert _outcome_of("task_batch_release", {"success": False, "error": "stale"}) == "error"
    assert _outcome_of("task_batch_release", {"success": True}) == "released"
    assert _outcome_of("task_batch_release", {"already_released": True}) == "already_released"
