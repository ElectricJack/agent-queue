"""Dv2 Phase 2 Task 6: Rework loop — reopen cancels stale open reviews.

When a task is reopened via ``reopen_with_feedback``, any still-open review
tasks that were spawned from its prior completion (linked by a
``discovered-from`` edge pointing at the reopened task) must be cancelled so
they don't get worked on for stale feedback.  A subsequent fresh completion
of the reopened task must then be free to spawn a fresh review.

Also verifies that the ``task`` gate sweep in the orchestrator treats FAILED
as satisfying the ``await_id`` — a stale review that we just cancelled to
FAILED must not stall its downstream waiters forever.
"""
from __future__ import annotations

import pytest

from src.models import AgentProfile, Project

# ``command_handler_factory`` and ``orchestrator_factory`` fixtures live in
# tests/conftest.py — shared with test_review_pipeline_rules.py and
# test_review_pipeline_e2e.py.


@pytest.mark.asyncio
async def test_reopen_cancels_stale_open_reviews(command_handler_factory):
    h = await command_handler_factory()
    await h.db.create_project(Project(id="p", name="P"))
    await h.db.upsert_profile(AgentProfile(id="worker", name="W"))
    await h.db.upsert_profile(AgentProfile(id="reviewer", name="R"))

    t = (await h.execute(
        "create_task", {"project_id": "p", "title": "T", "profile_id": "worker"}
    ))["created"]
    await h.db.update_task(t, branch_name="feature/t")
    from src.models import TaskStatus
    await h.db.transition_task(t, TaskStatus.COMPLETED, context="test")

    r = (await h.execute(
        "create_task", {"project_id": "p", "title": "Review: T", "profile_id": "reviewer"}
    ))["created"]
    await h.execute(
        "add_dependency",
        {"task_id": r, "depends_on": t, "dep_type": "discovered-from"},
    )

    result = await h.execute("reopen_with_feedback", {"task_id": t, "feedback": "please fix X"})
    assert result.get("reopened") == t

    review_after = await h.db.get_task(r)
    assert review_after.status.value == "FAILED"

    # Audit context is preserved for dashboard filtering.
    events = await h.db.get_recent_events(task_id=r, limit=50)
    assert any(
        "reopen_cascade" in (e.get("payload") or "")
        or "reopen_cascade" in (e.get("event_type") or "")
        for e in events
    )


@pytest.mark.asyncio
async def test_reopen_preserves_completed_reviews(command_handler_factory):
    h = await command_handler_factory()
    await h.db.create_project(Project(id="p", name="P"))
    await h.db.upsert_profile(AgentProfile(id="worker", name="W"))
    await h.db.upsert_profile(AgentProfile(id="reviewer", name="R"))

    from src.models import TaskStatus
    t = (await h.execute(
        "create_task", {"project_id": "p", "title": "T", "profile_id": "worker"}
    ))["created"]
    await h.db.update_task(t, branch_name="feature/t")
    await h.db.transition_task(t, TaskStatus.COMPLETED, context="test")
    r = (await h.execute(
        "create_task", {"project_id": "p", "title": "Review: T", "profile_id": "reviewer"}
    ))["created"]
    await h.execute(
        "add_dependency",
        {"task_id": r, "depends_on": t, "dep_type": "discovered-from"},
    )
    await h.db.transition_task(r, TaskStatus.COMPLETED, context="test")  # review approved

    await h.execute("reopen_with_feedback", {"task_id": t, "feedback": "fix Y"})
    review_after = await h.db.get_task(r)
    assert review_after.status.value == "COMPLETED", (
        "completed reviews must be preserved as history"
    )


@pytest.mark.asyncio
async def test_reopen_ignores_non_discovered_from_edges(command_handler_factory):
    """Only ``discovered-from`` edges pointing at the reopened task should
    trigger cancellation.  A plain ``blocks`` edge must not.
    """
    h = await command_handler_factory()
    await h.db.create_project(Project(id="p", name="P"))
    await h.db.upsert_profile(AgentProfile(id="worker", name="W"))

    from src.models import TaskStatus
    t = (await h.execute(
        "create_task", {"project_id": "p", "title": "T", "profile_id": "worker"}
    ))["created"]
    await h.db.transition_task(t, TaskStatus.COMPLETED, context="test")

    downstream = (await h.execute(
        "create_task", {"project_id": "p", "title": "D", "profile_id": "worker"}
    ))["created"]
    await h.execute(
        "add_dependency",
        {"task_id": downstream, "depends_on": t, "dep_type": "blocks"},
    )

    await h.execute("reopen_with_feedback", {"task_id": t, "feedback": "please fix X"})
    down_after = await h.db.get_task(downstream)
    # ``blocks`` edge is NOT provenance from a review — must not be cancelled.
    assert down_after.status.value != "FAILED"


@pytest.mark.asyncio
async def test_reopen_cascade_only_cancels_reviewer_profiles(command_handler_factory):
    """Only candidates with profile_id in {reviewer, final-reviewer} are
    cascade-cancelled. A non-reviewer task with a stray ``discovered-from``
    edge to the reopened task must survive untouched.
    """
    h = await command_handler_factory()
    await h.db.create_project(Project(id="p", name="P"))
    await h.db.upsert_profile(AgentProfile(id="worker", name="W"))
    await h.db.upsert_profile(AgentProfile(id="reviewer", name="R"))

    from src.models import TaskStatus

    t = (await h.execute(
        "create_task", {"project_id": "p", "title": "T", "profile_id": "worker"}
    ))["created"]
    await h.db.update_task(t, branch_name="feature/t")
    await h.db.transition_task(t, TaskStatus.COMPLETED, context="test")

    reviewer_task = (await h.execute(
        "create_task", {"project_id": "p", "title": "Review: T", "profile_id": "reviewer"}
    ))["created"]
    await h.execute(
        "add_dependency",
        {"task_id": reviewer_task, "depends_on": t, "dep_type": "discovered-from"},
    )

    # A non-reviewer task that (hypothetically) also carries a
    # discovered-from edge to the reopened task — must not be cascaded.
    non_reviewer_task = (await h.execute(
        "create_task", {"project_id": "p", "title": "Non-review byproduct", "profile_id": "worker"}
    ))["created"]
    await h.execute(
        "add_dependency",
        {"task_id": non_reviewer_task, "depends_on": t, "dep_type": "discovered-from"},
    )

    result = await h.execute("reopen_with_feedback", {"task_id": t, "feedback": "please fix X"})
    assert result.get("reopened") == t
    assert reviewer_task in result.get("cancelled_reviews", [])
    assert non_reviewer_task not in result.get("cancelled_reviews", [])

    reviewer_after = await h.db.get_task(reviewer_task)
    assert reviewer_after.status.value == "FAILED"

    non_reviewer_after = await h.db.get_task(non_reviewer_task)
    assert non_reviewer_after.status.value != "FAILED", (
        "non-reviewer profile task must not be cascade-cancelled on reopen"
    )


@pytest.mark.asyncio
async def test_task_gate_sweep_resolves_on_failed_review(orchestrator_factory):
    orch = await orchestrator_factory()
    h = orch.command_handler
    await h.db.create_project(Project(id="p", name="P"))
    await h.db.upsert_profile(AgentProfile(id="worker", name="W"))

    from src.models import TaskStatus
    downstream = (await h.execute(
        "create_task", {"project_id": "p", "title": "D", "profile_id": "worker"}
    ))["created"]
    review = (await h.execute(
        "create_task", {"project_id": "p", "title": "R", "profile_id": "worker"}
    ))["created"]
    gate_id, _ = await h.db.create_gate(
        project_id="p",
        gate_type="task",
        title="Awaiting review",
        await_id=review,
        waiter_task_ids=[downstream],
    )
    await h.db.transition_task(review, TaskStatus.FAILED, context="reopen_cascade:stale_review")
    await orch._sweep_resolve_task_gates()
    gate = await h.db.get_gate(gate_id)
    assert gate["status"] == "resolved", "task gate must resolve on FAILED await target"


@pytest.mark.asyncio
async def test_pr_merged_sweep_unblocks_downstream(orchestrator_factory, monkeypatch):
    """After a final-reviewer merges a PR, `_sweep_resolve_pr_ci_gates` resolves
    the downstream task's `pr-merged` gate so it can be worked on."""
    orch = await orchestrator_factory()
    h = orch.command_handler
    await h.db.create_project(Project(id="p", name="P"))
    await h.db.upsert_profile(AgentProfile(id="worker", name="W"))

    downstream = (await h.execute(
        "create_task", {"project_id": "p", "title": "D", "profile_id": "worker"}
    ))["created"]
    pr = "https://github.com/o/r/pull/17"
    gate_id, _ = await h.db.create_gate(
        project_id="p",
        gate_type="pr-merged",
        title="Awaiting merge",
        await_id=pr,
        waiter_task_ids=[downstream],
    )

    # Downstream is blocked before the merge.
    dt = await h.db.get_task(downstream)
    assert dt.is_blocked

    # Monkeypatch `_poll_pr_merged` directly so the sweep finds the PR merged
    # without needing a real workspace or gh CLI.
    async def fake_poll_pr_merged(pr_url: str, *, project_id: str | None = None) -> bool:
        assert pr_url == pr
        return True

    monkeypatch.setattr(orch, "_poll_pr_merged", fake_poll_pr_merged)

    # Force the sweep interval so the throttle doesn't skip execution.
    orch._last_gate_sweep = 0.0
    orch.config.work_graph.gate_sweep_interval_seconds = 1
    await orch._sweep_gates()

    gate = await h.db.get_gate(gate_id)
    assert gate["status"] == "resolved", "pr-merged gate must resolve after sweep"
    dt2 = await h.db.get_task(downstream)
    assert not dt2.is_blocked, "downstream task must be unblocked after gate resolves"
