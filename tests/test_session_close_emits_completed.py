"""Regression: ``aq task close`` must emit ``task.completed`` on the bus.

Every agent is session-routed today (``_execute_task`` forks to
``_launch_session_for_task`` and returns — execution.py §"Session-runtime
fork").  The legacy blocking tail below that fork — which held the only
``bus.emit("task.completed", ...)`` for an ordinary task — is dead code.

That left the session close path emitting ``task.closed`` alone, while the
default pipeline's ``per-task-review`` and ``per-branch-final-review`` rules
are both ``"on": "task.completed"``.  The result was silent: workers opened
PRs, closed their tasks, and no reviewer task was ever spawned and no PR was
ever merged.

``tests/test_review_pipeline_e2e.py`` did not catch it because it dispatches
``task.completed`` into the engine by hand rather than letting the close path
raise it.  These tests close the loop: they assert the *real* close path
raises the event, and that ``branch_name``/``pr_url`` are on the row (and so
on the hydrated ``event.task``) by the time it fires.
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
        db = Database(str(tmp_path / "close.db"))
        await db.initialize()
        cfg = AppConfig(
            discord=DiscordConfig(bot_token="t", guild_id="1"),
            workspace_dir=str(tmp_path / "w"),
            database_path=str(tmp_path / "close.db"),
            data_dir=str(tmp_path / "d"),
        )
        o = Orchestrator(cfg)
        o.db = db
        o.git = MagicMock()
        o.git.arev_parse = AsyncMock(return_value="")
        o.bus = MagicMock()
        o.bus.emit = AsyncMock()
        o.command_handler = CommandHandler(o, cfg)

        # The completion pipeline is repo mechanics; these tests are about the
        # event the close path raises.  Echo back any PR the task already
        # carries, the way a real discovery would.
        async def _noop_pipeline(ctx):
            return (getattr(ctx.task, "pr_url", None) or "", True)

        o._run_completion_pipeline = _noop_pipeline

        async def _noop_release(task_id, *, agent_id=None, workspace_path=None,
                                expect_claim_epoch=None):
            return None

        o.release_session_task_resources = _noop_release
        return o

    return _make


def _emitted(bus, event_type: str) -> list[dict]:
    return [
        call.args[1]
        for call in bus.emit.await_args_list
        if call.args and call.args[0] == event_type
    ]


async def _seed(h: CommandHandler) -> None:
    await h.db.create_project(Project(id="p", name="P"))
    await h.db.upsert_profile(AgentProfile(id="worker", name="Worker"))


@pytest.mark.asyncio
async def test_session_close_emits_task_completed(orchestrator_factory):
    """A passing close raises ``task.completed`` with the base event triple."""
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)

    task_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Do work", "profile_id": "worker"},
        )
    )["created"]
    await h.db.update_task(
        task_id,
        branch_name="aq/do-work",
        pr_url="https://github.com/o/r/pull/7",
    )
    await h.db.transition_task(task_id, TaskStatus.IN_PROGRESS, context="test")

    result = await h.execute(
        "task_close", {"task_id": task_id, "outcome": "pass", "summary": "done"}
    )
    assert result.get("success"), result

    completed = _emitted(orch.bus, "task.completed")
    assert len(completed) == 1, (
        "session close must raise exactly one task.completed; "
        f"emitted={[c.args[0] for c in orch.bus.emit.await_args_list if c.args]}"
    )
    payload = completed[0]
    # The base triple every task.* consumer relies on (event_schemas.py).
    assert payload["task_id"] == task_id
    assert payload["project_id"] == "p"
    assert payload["title"] == "Do work"
    # A worker that shipped code is exactly what the review rules exist for.
    assert payload["no_code"] is False


@pytest.mark.asyncio
async def test_branch_and_pr_survive_to_the_completed_event(orchestrator_factory):
    """``event.task`` hydration reads the row, so the row must carry both fields.

    ``Orchestrator._dispatch_playbook`` hydrates ``event.task`` from a fresh
    ``db.get_task``; the pipeline's ``when`` guards then read
    ``event.task.branch_name`` / ``event.task.pr_url``.  Assert the row still
    has both once the close has committed — a close that cleared either would
    silently disarm both review rules.
    """
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)

    task_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Do work", "profile_id": "worker"},
        )
    )["created"]
    await h.db.update_task(
        task_id,
        branch_name="aq/do-work",
        pr_url="https://github.com/o/r/pull/7",
    )
    await h.db.transition_task(task_id, TaskStatus.IN_PROGRESS, context="test")

    assert (await h.execute(
        "task_close", {"task_id": task_id, "outcome": "pass", "summary": "done"}
    )).get("success")

    row = await h.db.get_task(task_id)
    assert row.status == TaskStatus.COMPLETED
    assert row.branch_name == "aq/do-work"
    assert row.pr_url == "https://github.com/o/r/pull/7"


@pytest.mark.asyncio
async def test_failed_close_does_not_emit_task_completed(orchestrator_factory):
    """Only a task that actually reaches COMPLETED may raise the event.

    A transient failure re-queues to READY; spawning a reviewer for work that
    is about to be retried would be worse than the bug this file guards.
    """
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)

    task_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Do work", "profile_id": "worker"},
        )
    )["created"]
    await h.db.update_task(task_id, branch_name="aq/do-work")
    await h.db.transition_task(task_id, TaskStatus.IN_PROGRESS, context="test")

    await h.execute(
        "task_close",
        {
            "task_id": task_id,
            "outcome": "fail",
            "failure_class": "transient",
            "summary": "broke",
        },
    )

    assert _emitted(orch.bus, "task.completed") == []
    # ``task.closed`` is the always-emitted close record and must still fire.
    assert len(_emitted(orch.bus, "task.closed")) == 1


@pytest.mark.asyncio
async def test_task_show_reports_branch_and_pr(orchestrator_factory):
    """``task_show`` must surface ``branch_name``/``pr_url`` from the row.

    ``cli/formatters.py`` renders ``task.branch_name or "—"``, so omitting the
    key from the payload made every task read as branchless on ``aq task show``
    — which is what made this outage look like a persistence bug when the rows
    were correct all along.
    """
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)

    task_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Do work", "profile_id": "worker"},
        )
    )["created"]
    await h.db.update_task(
        task_id,
        branch_name="aq/do-work",
        pr_url="https://github.com/o/r/pull/7",
    )

    info = await h.execute("task_show", {"task_id": task_id})
    assert info["branch_name"] == "aq/do-work"
    assert info["pr_url"] == "https://github.com/o/r/pull/7"

    # The keys are unconditional: absent values read as None, not as a
    # missing key, so callers can rely on the payload's shape.
    bare = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Bare", "profile_id": "worker"},
        )
    )["created"]
    bare_info = await h.execute("task_show", {"task_id": bare})
    assert bare_info["branch_name"] is None
    assert bare_info["pr_url"] is None


@pytest.mark.asyncio
async def test_close_drives_the_review_chain(orchestrator_factory, pipeline_engine_factory):
    """The real close path's own event spawns the review + final-review chain.

    ``tests/test_review_pipeline_e2e.py`` proves the chain given a
    ``task.completed`` event; it hand-writes that event, so it stayed green
    through an outage where nothing emitted one.  This test never writes the
    payload: it takes whatever the close path put on the bus and feeds exactly
    that to the pipeline, so the two halves can no longer drift apart.
    """
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)
    await h.db.upsert_profile(AgentProfile(id="reviewer", name="Reviewer"))
    await h.db.upsert_profile(AgentProfile(id="final-reviewer", name="Final"))
    engine = pipeline_engine_factory(handler=h)

    task_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Do work", "profile_id": "worker"},
        )
    )["created"]
    await h.db.update_task(
        task_id,
        branch_name="aq/do-work",
        pr_url="https://github.com/o/r/pull/7",
    )
    await h.db.transition_task(task_id, TaskStatus.IN_PROGRESS, context="test")

    assert (await h.execute(
        "task_close", {"task_id": task_id, "outcome": "pass", "summary": "done"}
    )).get("success")

    completed = _emitted(orch.bus, "task.completed")
    assert completed, "close path emitted no task.completed to dispatch"
    await engine.dispatch("task.completed", completed[0], event_id="close-emitted")

    tasks = await h.db.list_tasks(project_id="p")
    assert [t for t in tasks if t.profile_id == "reviewer"], (
        "per-task-review did not fire on the close path's own event"
    )
    assert [t for t in tasks if t.profile_id == "final-reviewer"], (
        "per-branch-final-review did not fire on the close path's own event"
    )


async def _close_pass(h: CommandHandler, task_id: str, **extra) -> None:
    await h.db.transition_task(task_id, TaskStatus.IN_PROGRESS, context="test")
    result = await h.execute(
        "task_close", {"task_id": task_id, "outcome": "pass", "summary": "done", **extra}
    )
    assert result.get("success"), result


async def _review_rows(h: CommandHandler, reviewed_id: str) -> list:
    tasks = await h.db.list_tasks(project_id="p")
    return [t for t in tasks if t.dedup_key == f"review:task:{reviewed_id}"]


@pytest.mark.asyncio
async def test_read_only_close_does_not_review_the_review(
    orchestrator_factory, pipeline_engine_factory
):
    """A finished reviewer task must not spawn a review of itself.

    Reviewer tasks run on an ordinary slot and raw ``read_only`` metadata is
    not Git proof. The structural ``review_task`` flag prevents recursion.
    """
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)
    await h.db.upsert_profile(AgentProfile(id="reviewer", name="Reviewer", read_only=True))
    await h.db.upsert_profile(AgentProfile(id="final-reviewer", name="Final"))
    engine = pipeline_engine_factory(handler=h)

    review_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Review: Do work", "profile_id": "reviewer"},
        )
    )["created"]
    # What workspace acquisition stamps on every session task, reviewer or not.
    await h.db.update_task(review_id, branch_name=f"aq/{review_id}")

    await _close_pass(h, review_id)

    completed = _emitted(orch.bus, "task.completed")
    assert len(completed) == 1, "a reviewer's close is still a completion"
    assert completed[0]["no_code"] is False
    assert completed[0]["review_task"] is True

    await engine.dispatch("task.completed", completed[0], event_id="review-closed")

    assert await _review_rows(h, review_id) == [], "spawned a review of the review"
    tasks = await h.db.list_tasks(project_id="p")
    assert [t for t in tasks if t.profile_id == "final-reviewer"] == []


@pytest.mark.asyncio
async def test_no_op_close_is_flagged_no_code(orchestrator_factory, pipeline_engine_factory):
    """A no-op event is suppressed only after the Git proof succeeds."""
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)
    await h.db.upsert_profile(AgentProfile(id="reviewer", name="Reviewer", read_only=True))
    engine = pipeline_engine_factory(handler=h)

    async def _proved_no_work_pipeline(ctx):
        ctx.no_work_proven = True
        return (getattr(ctx.task, "pr_url", None) or "", True)

    orch._run_completion_pipeline = _proved_no_work_pipeline

    task_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Investigate", "profile_id": "worker"},
        )
    )["created"]
    await h.db.update_task(task_id, branch_name=f"aq/{task_id}")

    await _close_pass(h, task_id, work_outcome="no-op")

    completed = _emitted(orch.bus, "task.completed")
    assert len(completed) == 1
    assert completed[0]["no_code"] is True

    await engine.dispatch("task.completed", completed[0], event_id="no-op-closed")
    assert await _review_rows(h, task_id) == []


@pytest.mark.asyncio
async def test_review_task_close_is_never_reviewed_even_if_profile_writes(
    orchestrator_factory, pipeline_engine_factory
):
    """The recursion guard must not depend on the reviewer profile's flags.

    A task the pipeline itself created as a review —
    recognisable by its ``review:task:`` / ``branch-review:`` dedup key — is
    flagged ``review_task`` on ``task.completed`` regardless of profile, and
    both review rules stand down on it.
    """
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)
    await h.db.upsert_profile(AgentProfile(id="reviewer", name="Reviewer", read_only=False))
    await h.db.upsert_profile(AgentProfile(id="final-reviewer", name="Final", read_only=False))
    engine = pipeline_engine_factory(handler=h)

    reviewed_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Do work", "profile_id": "worker"},
        )
    )["created"]
    # Exactly the row ``per-task-review`` writes for the reviewed task.
    review_id = (
        await h.execute(
            "ensure_task",
            {
                "project_id": "p",
                "dedup_key": f"review:task:{reviewed_id}",
                "title": "Review: Do work",
                "profile_id": "reviewer",
            },
        )
    )["task_id"]
    await h.db.update_task(review_id, branch_name=f"aq/{review_id}")

    await _close_pass(h, review_id)

    completed = _emitted(orch.bus, "task.completed")
    assert len(completed) == 1
    assert completed[0]["no_code"] is False, "read_only=false profile: the old guard is inert"
    assert completed[0]["review_task"] is True

    await engine.dispatch("task.completed", completed[0], event_id="review-closed")

    assert await _review_rows(h, review_id) == [], "spawned a review of the review"
    tasks = await h.db.list_tasks(project_id="p")
    assert [t for t in tasks if t.profile_id == "final-reviewer"] == []


@pytest.mark.asyncio
async def test_final_review_close_is_never_reviewed_even_if_profile_writes(
    orchestrator_factory, pipeline_engine_factory
):
    """Same guard for the branch's final review (``branch-review:<branch>``)."""
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)
    await h.db.upsert_profile(AgentProfile(id="reviewer", name="Reviewer", read_only=False))
    await h.db.upsert_profile(AgentProfile(id="final-reviewer", name="Final", read_only=False))
    engine = pipeline_engine_factory(handler=h)

    final_id = (
        await h.execute(
            "ensure_task",
            {
                "project_id": "p",
                "dedup_key": "branch-review:aq/do-work",
                "title": "Final review: aq/do-work",
                "profile_id": "final-reviewer",
            },
        )
    )["task_id"]
    await h.db.update_task(
        final_id, branch_name=f"aq/{final_id}", pr_url="https://github.com/o/r/pull/7"
    )

    await _close_pass(h, final_id)

    completed = _emitted(orch.bus, "task.completed")
    assert len(completed) == 1
    assert completed[0]["review_task"] is True

    await engine.dispatch("task.completed", completed[0], event_id="final-closed")

    assert await _review_rows(h, final_id) == []
    tasks = await h.db.list_tasks(project_id="p")
    assert [t for t in tasks if t.dedup_key == f"branch-review:aq/{final_id}"] == []


@pytest.mark.asyncio
async def test_worker_close_is_not_flagged_review_task(orchestrator_factory):
    """An ordinary worker task, dedup-keyed or not, is still reviewed."""
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)

    keyed = (
        await h.execute(
            "ensure_task",
            {
                "project_id": "p",
                "dedup_key": "spec-ingest:docs/specs/x.md",
                "title": "Ingest",
                "profile_id": "worker",
            },
        )
    )["task_id"]
    await h.db.update_task(keyed, branch_name=f"aq/{keyed}")
    await _close_pass(h, keyed)

    completed = _emitted(orch.bus, "task.completed")
    assert len(completed) == 1
    assert completed[0]["review_task"] is False


@pytest.mark.asyncio
async def test_empty_branch_close_is_flagged_no_code(
    orchestrator_factory, pipeline_engine_factory
):
    """A branch that ends with no commits is not worth reviewing.

    The completion pipeline settles that question before integration can
    merge the branch away and leaves the verdict on the context; the close
    path copies that proof into ``no_code``.
    It is the guard that survives what the other two cannot: this worker has
    a writing profile and no review dedup key, so ``read_only`` and
    ``review_task`` both read False — and there is still nothing to review
    (task bright-forge-78).
    """
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)
    await h.db.upsert_profile(AgentProfile(id="reviewer", name="Reviewer"))
    await h.db.upsert_profile(AgentProfile(id="final-reviewer", name="Final"))
    engine = pipeline_engine_factory(handler=h)

    async def _empty_branch_pipeline(ctx):
        ctx.no_work_proven = True
        return (getattr(ctx.task, "pr_url", None) or "", True)

    orch._run_completion_pipeline = _empty_branch_pipeline

    task_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Do work", "profile_id": "worker"},
        )
    )["created"]
    await h.db.update_task(
        task_id,
        branch_name=f"aq/{task_id}",
        pr_url="https://github.com/o/r/pull/9",
    )
    await _close_pass(h, task_id)

    completed = _emitted(orch.bus, "task.completed")
    assert len(completed) == 1
    assert completed[0]["no_code"] is True
    assert completed[0]["review_task"] is False, "not a pipeline-created review"

    await engine.dispatch("task.completed", completed[0], event_id="empty-branch")

    assert await _review_rows(h, task_id) == [], "reviewed a branch with an empty diff"
    tasks = await h.db.list_tasks(project_id="p")
    assert [t for t in tasks if t.profile_id == "final-reviewer"] == []


@pytest.mark.asyncio
async def test_branch_with_commits_close_is_still_reviewed(
    orchestrator_factory, pipeline_engine_factory
):
    """The new guard only ever narrows: real work still spawns its review."""
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)
    await h.db.upsert_profile(AgentProfile(id="reviewer", name="Reviewer"))
    await h.db.upsert_profile(AgentProfile(id="final-reviewer", name="Final"))
    engine = pipeline_engine_factory(handler=h)

    task_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Do work", "profile_id": "worker"},
        )
    )["created"]
    await h.db.update_task(
        task_id,
        branch_name=f"aq/{task_id}",
        pr_url="https://github.com/o/r/pull/10",
    )
    await _close_pass(h, task_id)

    completed = _emitted(orch.bus, "task.completed")
    assert completed[0]["no_code"] is False

    await engine.dispatch("task.completed", completed[0], event_id="with-commits")

    assert await _review_rows(h, task_id), "per-task-review no longer fires"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile_id", "work_outcome"),
    [("worker", "no-op"), ("auditor", "shipped")],
)
async def test_no_code_intent_with_delivered_work_is_still_reviewed(
    orchestrator_factory,
    pipeline_engine_factory,
    profile_id,
    work_outcome,
):
    """Intent metadata cannot suppress review after Git found delivered work."""
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)
    if profile_id == "auditor":
        await h.db.upsert_profile(
            AgentProfile(id="auditor", name="Auditor", read_only=True)
        )
    await h.db.upsert_profile(AgentProfile(id="reviewer", name="Reviewer"))
    await h.db.upsert_profile(AgentProfile(id="final-reviewer", name="Final"))
    engine = pipeline_engine_factory(handler=h)

    async def _delivered_pipeline(ctx):
        ctx.no_work_proven = False
        return (getattr(ctx.task, "pr_url", None) or "", True)

    orch._run_completion_pipeline = _delivered_pipeline
    task_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Delivered", "profile_id": profile_id},
        )
    )["created"]
    await h.db.update_task(
        task_id,
        branch_name=f"aq/{task_id}",
        pr_url="https://github.com/o/r/pull/11",
    )

    await _close_pass(h, task_id, work_outcome=work_outcome)

    completed = _emitted(orch.bus, "task.completed")
    assert completed[0]["no_code"] is False
    await engine.dispatch("task.completed", completed[0], event_id=f"intent-{profile_id}")
    assert await _review_rows(h, task_id), "proof-backed work was not reviewed"


@pytest.mark.asyncio
async def test_keyless_review_close_is_flagged_by_its_profile(
    orchestrator_factory, pipeline_engine_factory
):
    """A review created outside ``ensure_task`` is recognised by profile id."""
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)
    await h.db.upsert_profile(AgentProfile(id="reviewer", name="Reviewer", read_only=False))
    await h.db.upsert_profile(AgentProfile(id="final-reviewer", name="Final", read_only=False))
    engine = pipeline_engine_factory(handler=h)

    review_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Review: Do work", "profile_id": "reviewer"},
        )
    )["created"]
    await h.db.update_task(
        review_id, branch_name=f"aq/{review_id}", pr_url="https://github.com/o/r/pull/9"
    )

    await _close_pass(h, review_id)

    completed = _emitted(orch.bus, "task.completed")
    assert len(completed) == 1
    assert completed[0]["no_code"] is False, "read_only=false profile: old guard is inert"
    assert completed[0]["review_task"] is True

    await engine.dispatch("task.completed", completed[0], event_id="keyless-review-closed")
    assert await _review_rows(h, review_id) == [], "spawned a review of the review"


@pytest.mark.asyncio
async def test_container_settlement_flags_a_settled_review(orchestrator_factory):
    """The common completion emitter recognises settled reviews by profile."""
    orch = await orchestrator_factory()
    h = orch.command_handler
    await _seed(h)
    await h.db.upsert_profile(AgentProfile(id="reviewer", name="Reviewer", read_only=False))
    orch._emit_text_notify = AsyncMock()
    orch._check_workflow_stage_completion = AsyncMock()

    review_id = (
        await h.execute(
            "create_task",
            {"project_id": "p", "title": "Review: Do work", "profile_id": "reviewer"},
        )
    )["created"]
    worker_id = (
        await h.execute(
            "create_task", {"project_id": "p", "title": "Do work", "profile_id": "worker"}
        )
    )["created"]

    await orch._on_containers_settled([review_id, worker_id])

    completed = _emitted(orch.bus, "task.completed")
    by_task = {payload["task_id"]: payload for payload in completed}
    assert by_task[review_id]["review_task"] is True
    assert by_task[worker_id]["review_task"] is False
