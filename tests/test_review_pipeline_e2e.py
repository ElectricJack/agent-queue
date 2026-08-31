"""Dv2 Phase 2 Task 8: End-to-end integration test for the review pipeline.

Proves that Tasks 1-7 compose:

  worker completes task w/ branch + PR
    → default-pipeline dispatches on ``task.completed``
        → per-task-review rule (T4) spawns reviewer task, wires
          ``discovered-from`` and downstream ``task`` gate
        → per-branch-final-review rule (T5) spawns final-reviewer,
          wires reviewer ``blocks`` final and downstream ``pr-merged`` gate
    → reviewer closes with summary (T2)
    → sweep resolves ``task`` gate (T5 flow, exercised via T4/T6 sweep)
    → final-reviewer merges PR via ``pr_merge`` command (T1, T3)
    → final-reviewer closes with summary (T2)
    → sweep resolves ``pr-merged`` gate (T7)
    → downstream is unblocked

Second test covers the rework leg (T6):
  reopen cancels the stale review; a fresh completion respawns.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import AgentProfile, Project, TaskStatus
from src.orchestrator import Orchestrator

# ``pipeline_engine_factory`` fixture and the ``PipelineEngine`` test helper
# live in tests/conftest.py — shared with test_review_pipeline_rules.py and
# test_review_reopen_cascade.py. ``orchestrator_factory`` below is a
# specialized local override (patches completion-pipeline internals the
# generic conftest fixture doesn't need).


# ---------------------------------------------------------------------------
# Fixtures — mirror T4/T5/T6/T7 patterns
# ---------------------------------------------------------------------------


@pytest.fixture
def orchestrator_factory(tmp_path):
    async def _make():
        db = Database(str(tmp_path / "e2e.db"))
        await db.initialize()
        cfg = AppConfig(
            discord=DiscordConfig(bot_token="t", guild_id="1"),
            workspace_dir=str(tmp_path / "w"),
            database_path=str(tmp_path / "e2e.db"),
            data_dir=str(tmp_path / "d"),
        )
        o = Orchestrator(cfg)
        o.db = db
        o.git = MagicMock()
        o.git.arev_parse = AsyncMock(return_value="")
        o.bus = MagicMock()
        o.bus.emit = AsyncMock()
        o.command_handler = CommandHandler(o, cfg)
        # Bypass the workspace/git-heavy completion pipeline — the E2E test
        # exercises the graph-level chain, not repo mechanics.  The pipeline
        # inside `complete_session_task` is already wrapped in try/except but
        # returning success keeps status transitions on the happy path.
        async def _noop_pipeline(ctx):
            pr = getattr(ctx.task, "pr_url", None) or ""
            return (pr, True)
        o._run_completion_pipeline = _noop_pipeline
        # Resource-release cleanup touches workspaces we never created.
        async def _noop_release(task_id, *, agent_id=None, workspace_path=None, expect_claim_epoch=None):
            return None
        o.release_session_task_resources = _noop_release
        return o

    return _make


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_project_and_profiles(h: CommandHandler) -> None:
    await h.db.create_project(Project(id="p", name="P"))
    await h.db.upsert_profile(AgentProfile(id="worker", name="Worker"))
    await h.db.upsert_profile(AgentProfile(id="reviewer", name="Reviewer"))
    await h.db.upsert_profile(AgentProfile(id="final-reviewer", name="Final"))


async def _close_task(h: CommandHandler, task_id: str, summary: str) -> dict:
    """Move task to IN_PROGRESS then close it via the session command."""
    await h.db.transition_task(task_id, TaskStatus.IN_PROGRESS, context="e2e-test")
    return await h.execute(
        "task_close",
        {"task_id": task_id, "outcome": "pass", "summary": summary},
    )


# ---------------------------------------------------------------------------
# Test 1 — the full happy chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_review_chain_end_to_end(
    orchestrator_factory, pipeline_engine_factory, monkeypatch
):
    orch = await orchestrator_factory()
    h = orch.command_handler
    engine = pipeline_engine_factory(handler=h)

    await _seed_project_and_profiles(h)

    pr_url = "https://github.com/o/r/pull/99"
    branch = "feature/e2e"

    # Worker task with a downstream dependent.
    worker_task = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Do work", "profile_id": "worker"},
        )
    )["created"]
    downstream = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Depends", "profile_id": "worker"},
        )
    )["created"]
    await h.execute(
        "add_dependency",
        {"task_id": downstream, "depends_on": worker_task, "dep_type": "blocks"},
    )

    # Worker "produces" a branch + PR before completion.
    await h.db.update_task(worker_task, branch_name=branch, pr_url=pr_url)

    # Worker closes with a summary (Task 2).
    close_result = await _close_task(h, worker_task, summary="did the work")
    assert close_result.get("success"), close_result
    # Summary landed in task_metadata.
    meta_summary = await h.db.get_task_meta(worker_task, "summary")
    assert meta_summary == "did the work"

    # Fire the pipeline reaction (task.completed).
    await engine.dispatch(
        "task.completed",
        {"task_id": worker_task, "project_id": "p", "title": "Do work"},
        event_id="e-worker-done",
    )

    tasks = await h.db.list_tasks(project_id="p")
    reviews = [t for t in tasks if t.profile_id == "reviewer"]
    finals = [t for t in tasks if t.profile_id == "final-reviewer"]
    assert len(reviews) == 1, f"expected 1 per-task review; got {reviews}"
    assert len(finals) == 1, f"expected 1 final-review; got {finals}"

    # Review is wired discovered-from the worker task.
    review_deps = await h.db.get_typed_dependencies(reviews[0].id)
    assert any(
        dep_type == "discovered-from" and dep_id == worker_task
        for dep_id, dep_type in review_deps
    ), f"review {reviews[0].id} missing discovered-from edge; deps={review_deps}"

    # Final review is blocked by the per-task review.
    final_deps = await h.db.get_typed_dependencies(finals[0].id)
    assert any(
        dep_type == "blocks" and dep_id == reviews[0].id for dep_id, dep_type in final_deps
    ), f"final review missing blocks edge from review; deps={final_deps}"

    # Downstream now has both a ``task`` gate on the review and a ``pr-merged``
    # gate on the worker's PR URL.
    downstream_gates = await h.db.get_gates_for_task(downstream)
    gate_types = {g["gate_type"] for g in downstream_gates}
    assert "task" in gate_types, f"expected task gate on {downstream}; got {downstream_gates}"
    assert "pr-merged" in gate_types, (
        f"expected pr-merged gate on {downstream}; got {downstream_gates}"
    )
    d = await h.db.get_task(downstream)
    assert d.is_blocked, "downstream must be blocked by open gates"

    # Reviewer approves and closes.
    await _close_task(h, reviews[0].id, summary="LGTM")

    # Sweep resolves the ``task`` gate; final-reviewer's blocker clears.
    orch._last_gate_sweep = 0.0
    orch.config.work_graph.gate_sweep_interval_seconds = 1
    await orch._sweep_gates()
    final_after = await h.db.get_task(finals[0].id)
    assert not final_after.is_blocked, (
        "final-reviewer must be unblocked after per-task review completes"
    )

    # Final reviewer "merges" the PR via the pr_merge command (Task 1).
    monkeypatch.setattr(
        orch.db,
        "get_project_workspace_path",
        AsyncMock(return_value="/tmp/fake-checkout"),
    )
    orch.git.amerge_pr = AsyncMock(
        return_value={"success": True, "sha": "f" * 40, "error": None}
    )
    merge_result = await h.execute(
        "pr_merge", {"project_id": "p", "pr_url": pr_url}
    )
    assert merge_result["success"], merge_result
    assert merge_result["sha"] == "f" * 40

    # Final reviewer closes with summary.
    await _close_task(h, finals[0].id, summary=f"merged {'f' * 40}")

    # Sweep resolves the pr-merged gate on downstream (Task 7).
    async def _fake_poll(pr, *, project_id=None):
        assert pr == pr_url
        return True

    monkeypatch.setattr(orch, "_poll_pr_merged", _fake_poll)
    orch._last_gate_sweep = 0.0
    await orch._sweep_gates()

    d2 = await h.db.get_task(downstream)
    assert not d2.is_blocked, "downstream must be unblocked after pr-merged sweep"


# ---------------------------------------------------------------------------
# Test 2 — rework leg (T6): reopen cancels stale review, fresh completion
# spawns a new one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reopen_cancels_review_and_fresh_completion_respawns(
    orchestrator_factory, pipeline_engine_factory
):
    orch = await orchestrator_factory()
    h = orch.command_handler
    engine = pipeline_engine_factory(handler=h)

    await _seed_project_and_profiles(h)

    worker_task = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Do work", "profile_id": "worker"},
        )
    )["created"]
    await h.db.update_task(
        worker_task,
        branch_name="feature/rework",
        pr_url="https://github.com/o/r/pull/100",
    )

    # First completion → first review is spawned.
    await _close_task(h, worker_task, summary="round 1")
    await engine.dispatch(
        "task.completed",
        {"task_id": worker_task, "project_id": "p", "title": "Do work"},
        event_id="e-first",
    )

    tasks = await h.db.list_tasks(project_id="p")
    reviews_1 = [t for t in tasks if t.profile_id == "reviewer"]
    assert len(reviews_1) == 1, f"expected 1 review after first completion; got {reviews_1}"
    first_review_id = reviews_1[0].id

    # Reopen the worker task with feedback — stale open review should be
    # cancelled (T6).
    reopen_res = await h.execute(
        "reopen_with_feedback",
        {"task_id": worker_task, "feedback": "please fix X"},
    )
    assert reopen_res.get("reopened") == worker_task

    stale = await h.db.get_task(first_review_id)
    assert stale.status == TaskStatus.FAILED, (
        f"stale review should be FAILED; got {stale.status}"
    )

    # Worker completes again — fresh review spawns (idempotency key is scoped
    # per event_id, so a new event_id must produce a new review).
    await _close_task(h, worker_task, summary="round 2")
    await engine.dispatch(
        "task.completed",
        {"task_id": worker_task, "project_id": "p", "title": "Do work"},
        event_id="e-second",
    )

    tasks_2 = await h.db.list_tasks(project_id="p")
    live_reviews = [
        t for t in tasks_2
        if t.profile_id == "reviewer" and t.status != TaskStatus.FAILED
    ]
    assert len(live_reviews) == 1, (
        f"expected exactly one live review after rework; got {live_reviews}"
    )
    assert live_reviews[0].id != first_review_id, (
        "rework must produce a new review row, not resurrect the cancelled one"
    )
