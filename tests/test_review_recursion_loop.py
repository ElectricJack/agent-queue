"""Regression: the review pipeline must not spawn ``Review: Review: ...`` chains.

Task solid-beacon-50 reported a 7-deep chain of review tasks grown from a
single CI-red task: each review completed with a verdict and no code, the
completion event re-entered ``per-task-review``, and another ``Review: ``
prefix was stacked on the title.

The two existing test files each cover one half of the loop and neither can
see the recursion:

* ``tests/test_review_pipeline_rules.py`` evaluates the rules' ``when``
  clauses against *hand-written* payloads — it asserts the guard reads the
  flags, not that the close path ever sets them;
* ``tests/test_session_close_emits_completed.py`` asserts the close path's
  emitted payload — but nothing consumes it;
* ``tests/test_review_pipeline_e2e.py`` dispatches ``task.completed`` by
  hand, so its review tasks are closed without ever raising the event that
  would recurse.

These tests join the halves: they close a task through the real ``task_close``
command, take whatever ``task.completed`` the close path actually put on the
bus, and feed *that* payload into the real compiled default pipeline — then
close the review the pipeline created the same way and assert the queue does
not grow.  That is the production loop, and it is the only arrangement in
which the reported bug reproduces.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import AgentProfile, Project, TaskStatus
from src.orchestrator import Orchestrator


@pytest.fixture
def orchestrator_factory(tmp_path):
    async def _make():
        db = Database(str(tmp_path / "recursion.db"))
        await db.initialize()
        cfg = AppConfig(
            discord=DiscordConfig(bot_token="t", guild_id="1"),
            workspace_dir=str(tmp_path / "w"),
            database_path=str(tmp_path / "recursion.db"),
            data_dir=str(tmp_path / "d"),
        )
        o = Orchestrator(cfg)
        o.db = db
        o.git = MagicMock()
        o.git.arev_parse = AsyncMock(return_value="")
        o.bus = MagicMock()
        o.bus.emit = AsyncMock()
        o.command_handler = CommandHandler(o, cfg)

        # Repo mechanics are not what these tests are about; echo back any PR
        # the row already carries the way a real discovery would.  Note that
        # ``_task_produces_no_code`` is deliberately *not* stubbed — the
        # ``no_code`` guard must be computed from the real profile row.
        async def _noop_pipeline(ctx):
            return (getattr(ctx.task, "pr_url", None) or "", True)

        o._run_completion_pipeline = _noop_pipeline

        async def _noop_release(task_id, *, agent_id=None, workspace_path=None,
                                expect_claim_epoch=None):
            return None

        o.release_session_task_resources = _noop_release
        return o

    return _make


async def _seed(h: CommandHandler, *, reviewer_read_only: bool = True) -> None:
    await h.db.create_project(Project(id="p", name="P"))
    await h.db.upsert_profile(AgentProfile(id="worker", name="Worker"))
    await h.db.upsert_profile(
        AgentProfile(id="reviewer", name="Reviewer", read_only=reviewer_read_only)
    )
    await h.db.upsert_profile(
        AgentProfile(id="final-reviewer", name="Final", read_only=reviewer_read_only)
    )


async def _close_and_dispatch(orch, engine, task_id: str, summary: str) -> None:
    """Close *task_id* for real, then dispatch the events the close path raised.

    This is the join the older tests are missing: the payload handed to the
    pipeline is the one ``_close_session_task`` built, flags and all, rather
    than a literal written by the test.
    """
    seen = len(orch.bus.emit.await_args_list)
    await orch.db.transition_task(task_id, TaskStatus.IN_PROGRESS, context="test")
    result = await orch.command_handler.execute(
        "task_close", {"task_id": task_id, "outcome": "pass", "summary": summary}
    )
    assert result.get("success"), result

    for i, call in enumerate(orch.bus.emit.await_args_list[seen:]):
        if call.args and call.args[0] == "task.completed":
            await engine.dispatch(
                "task.completed", dict(call.args[1]), event_id=f"{task_id}-{i}"
            )


def _reviews(tasks) -> list:
    return [t for t in tasks if (t.profile_id or "") in ("reviewer", "final-reviewer")]


async def _run_loop(orch, engine, *, review_dedup_key_survives: bool = True):
    """Drive worker-close → review spawned → review-close and return the tasks.

    ``review_dedup_key_survives=False`` simulates a project whose own pipeline
    creates review tasks without the shipped ``review:task:`` dedup key.
    """
    h = orch.command_handler

    worker_task = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Fix CI", "profile_id": "worker"},
        )
    )["created"]
    await h.db.update_task(
        worker_task,
        branch_name="aq/fix-ci",
        pr_url="https://github.com/o/r/pull/7",
    )

    await _close_and_dispatch(orch, engine, worker_task, "fixed CI")

    reviews = _reviews(await h.db.list_tasks(project_id="p"))
    assert len(reviews) == 2, f"expected one per-task review + one final review; got {reviews}"
    per_task = next(t for t in reviews if t.profile_id == "reviewer")

    # Production shape: a reviewer runs on a worktree slot checked out on its
    # own ``aq/<id>`` branch, so its row carries a branch_name like any other
    # session task.  That branch is what made the recursion possible.
    await h.db.update_task(per_task.id, branch_name=f"aq/{per_task.id}")
    if not review_dedup_key_survives:
        await h.db.update_task(per_task.id, dedup_key=None)

    await _close_and_dispatch(orch, engine, per_task.id, "LGTM")

    return await h.db.list_tasks(project_id="p")


def _assert_no_recursion(tasks) -> None:
    nested = [t for t in tasks if t.title.startswith("Review: Review:")]
    assert not nested, f"review-of-a-review spawned: {[(t.id, t.title) for t in nested]}"
    assert len(_reviews(tasks)) == 2, (
        "the queue must still hold exactly the one per-task review and the one "
        f"final review; got {[(t.id, t.title, t.profile_id) for t in _reviews(tasks)]}"
    )


@pytest.mark.asyncio
async def test_finished_review_does_not_spawn_a_review_of_itself(
    orchestrator_factory, pipeline_engine_factory
):
    """The shipped configuration: ``read_only`` reviewer, pipeline dedup key.

    Both guards are armed here; this is the baseline the reported chain
    violated.
    """
    orch = await orchestrator_factory()
    engine = pipeline_engine_factory(handler=orch.command_handler)
    await _seed(orch.command_handler)

    _assert_no_recursion(await _run_loop(orch, engine))


@pytest.mark.asyncio
async def test_no_recursion_when_the_reviewer_profile_is_not_read_only(
    orchestrator_factory, pipeline_engine_factory
):
    """An operator who gives the reviewer write tools disarms ``no_code`` only.

    ``read_only: false`` makes ``_task_produces_no_code`` return False, so the
    ``no_code`` guard goes quiet.  The structural ``review_task`` guard reads
    the pipeline's own dedup key and must still hold.
    """
    orch = await orchestrator_factory()
    engine = pipeline_engine_factory(handler=orch.command_handler)
    await _seed(orch.command_handler, reviewer_read_only=False)

    _assert_no_recursion(await _run_loop(orch, engine))


@pytest.mark.asyncio
async def test_no_recursion_when_both_the_profile_flag_and_the_dedup_key_are_gone(
    orchestrator_factory, pipeline_engine_factory
):
    """Both existing guards disarmed at once — the case that still recursed.

    A project that routes reviews through its own pipeline keys the rows
    however it likes, so ``review_task`` reads False; a reviewer profile with
    write tools makes ``no_code`` read False too.  Nothing then distinguished
    the finishing review from a worker that shipped code, and the chain grew
    again.  The reviewer *role* is the third, independent signal: a task
    running the ``reviewer`` / ``final-reviewer`` profile is a review whatever
    its row or its flags say.
    """
    orch = await orchestrator_factory()
    engine = pipeline_engine_factory(handler=orch.command_handler)
    await _seed(orch.command_handler, reviewer_read_only=False)

    tasks = await _run_loop(orch, engine, review_dedup_key_survives=False)
    _assert_no_recursion(tasks)
