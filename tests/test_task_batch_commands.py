"""Batch propose/commit/update/discard + cycle detection.

Phase 6 Tasks 2 + 3 (design §8, spec ingestion).
"""
from __future__ import annotations

import time

import pytest

from src.database.queries.proposal_queries import detect_cycles, get_proposal
from src.models import AgentProfile


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


async def test_commit_preserves_explicit_task_route(handler):
    """A batch route is task intent, not metadata to discard at commit time."""
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
    await _approve(handler, proposal["proposal_id"])

    committed = await handler.execute(
        "task_batch_commit", {"proposal_id": proposal["proposal_id"]}
    )

    assert committed["success"] is True
    assert (await handler._db.get_task(committed["task_ids"][0])).profile_id == "worker"


async def test_commit_rejects_legacy_supervisor_project_default(handler):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    await handler._db.create_profile(AgentProfile(
        id="supervisor", name="Supervisor", lifecycle="named",
    ))
    await handler._db.update_project("p1", default_profile_id="supervisor")
    proposal = await handler.execute(
        "task_batch_propose",
        {
            "project_id": "p1", "source": "spec:foo",
            "tasks": [{"tempId": "a", "title": "A", "description": ""}], "edges": [],
        },
    )
    await _approve(handler, proposal["proposal_id"])
    result = await handler.execute("task_batch_commit", {"proposal_id": proposal["proposal_id"]})
    assert "project default is invalid" in result["error"]
    assert await handler._db.list_tasks(project_id="p1") == []


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
