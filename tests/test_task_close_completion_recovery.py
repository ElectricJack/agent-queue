"""A restart between task close's terminal transition and its completion
record must not lose the agent's account (fair-ridge-26) -- and must never
turn a close that was not accepted into one (smart-cascade).

Recovery needs positive proof: the ``accepted_close`` identity that the
close's own terminal transition wrote in its transaction must name the draft's
completion id, session and claim epoch.  A task's status alone proves nothing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import update

from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.database import Database
from src.database.queries.result_queries import PENDING_COMPLETION_KEY
from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY
from src.database.tables import tasks
from src.models import Project, Task, TaskStatus
from tests.db_fixtures import lease_dsn
from tests.test_task_close_summary_enforcement import _StubOrchestrator


class _Restart(BaseException):
    """The daemon dying mid-close.

    A ``BaseException`` so no ``except Exception`` or cleanup in the command
    runs: what the close had written is exactly what the next start finds.
    """


class _Accepting(_StubOrchestrator):
    """The real close contract: the terminal transition carries the close's
    identity (``accepted_close``) under its claim fence."""

    async def complete_session_task(self, task, **kwargs):
        status = TaskStatus.COMPLETED if kwargs.get("outcome") == "pass" else TaskStatus.READY
        await self.db.transition_task(
            task.id,
            status,
            context="session_close",
            assigned_agent_id=None,
            expect_claim_epoch=kwargs.get("expect_claim_epoch"),
            accepted_close=kwargs.get("accepted_close"),
        )
        return {"status": status.value, "pr_url": None, "pipeline_ok": True}


class _CrashAfterTransition(_Accepting):
    async def complete_session_task(self, task, **kwargs):
        await super().complete_session_task(task, **kwargs)
        raise _Restart()


class _CrashBeforeTransition(_Accepting):
    async def complete_session_task(self, task, **kwargs):
        raise _Restart()


class _FailBeforeTransition(_Accepting):
    async def complete_session_task(self, task, **kwargs):
        raise RuntimeError("pipeline boom")


class _FailAfterTransition(_Accepting):
    async def complete_session_task(self, task, **kwargs):
        await super().complete_session_task(task, **kwargs)
        raise RuntimeError("boom after the commit")


class _RefuseVerification(_Accepting):
    async def complete_session_task(self, task, **kwargs):
        return {
            "status": TaskStatus.IN_PROGRESS.value,
            "pr_url": None,
            "pipeline_ok": False,
            "verification_retry": True,
            "issues": ["branch is not pushed"],
            "feedback": "branch is not pushed",
        }


class _UnmarkedTransition(_Accepting):
    """A transition that committed without this close's identity."""

    async def complete_session_task(self, task, **kwargs):
        await super().complete_session_task(task, **{**kwargs, "accepted_close": None})
        raise _Restart()


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("close_recovery.db"))
    await database.initialize()
    yield database
    await database.close()


def _handler(db, orch_cls):
    cfg = AppConfig()
    cfg.messages.enabled = False
    return CommandHandler(orch_cls(db), cfg)


async def _in_progress(db) -> str:
    await db.create_project(Project(id="p", name="P"))
    await db.create_task(Task(id="t1", project_id="p", title="t", description="d"))
    await db.transition_task("t1", TaskStatus.IN_PROGRESS, context="test")
    return "t1"


CLOSE_ARGS = {
    "task_id": "t1",
    "outcome": "pass",
    "summary": "did the thing",
    "tests": ["aq test tests/test_x.py"],
    "commands": ["ruff check src/x.py"],
    "commit": "a" * 40,
}


async def _crash_before_transition(db) -> dict:
    """Leave the draft of a close whose transition never committed."""
    tid = await _in_progress(db)
    with pytest.raises(_Restart):
        await _handler(db, _CrashBeforeTransition).execute("task_close", dict(CLOSE_ARGS))
    assert (await db.get_task(tid)).status == TaskStatus.IN_PROGRESS
    draft = await db.get_task_meta(tid, PENDING_COMPLETION_KEY)
    assert draft is not None
    return draft


async def _assert_dropped(db, tid="t1"):
    assert await db.recover_pending_completions() == []
    assert await db.get_task_completions(tid) == []
    assert await db.get_task_meta(tid, PENDING_COMPLETION_KEY) is None


async def test_successful_close_leaves_no_draft_and_names_its_record(db):
    tid = await _in_progress(db)
    result = await _handler(db, _Accepting).execute("task_close", dict(CLOSE_ARGS))
    assert result["success"] is True
    assert await db.get_task_meta(tid, PENDING_COMPLETION_KEY) is None
    records = await db.get_task_completions(tid)
    assert len(records) == 1
    accepted = await db.get_task_meta(tid, ACCEPTED_CLOSE_KEY)
    assert accepted == {"completion_id": records[0].id, "session_id": None, "claim_epoch": 0}
    assert await db.recover_pending_completions() == []


async def test_draft_is_bound_to_the_close_identity(db):
    draft = await _crash_before_transition(db)
    identity = draft["identity"]
    assert identity["completion_id"] == draft["completion"]["id"]
    assert identity["session_id"] is None
    assert identity["claim_epoch"] == (await db.get_task("t1")).claim_epoch
    assert draft["completion"]["tests"] == ["aq test tests/test_x.py"]


async def test_restart_after_accepted_transition_recovers_exactly_once(db):
    tid = await _in_progress(db)
    with pytest.raises(_Restart):
        await _handler(db, _CrashAfterTransition).execute("task_close", dict(CLOSE_ARGS))
    assert (await db.get_task(tid)).status == TaskStatus.COMPLETED
    assert await db.get_task_completion(tid) is None
    draft = await db.get_task_meta(tid, PENDING_COMPLETION_KEY)
    assert await db.get_task_meta(tid, ACCEPTED_CLOSE_KEY) == draft["identity"]

    assert await db.recover_pending_completions() == [tid]

    record = await db.get_task_completion(tid)
    assert record is not None
    assert record.id == draft["identity"]["completion_id"]
    assert record.outcome == "pass"
    assert record.summary == "did the thing"
    assert record.tests == ["aq test tests/test_x.py"]
    assert record.commands == ["ruff check src/x.py"]
    assert record.commits == ["a" * 40]
    assert record.completed_at == draft["completion"]["completed_at"]
    assert await db.get_task_meta(tid, PENDING_COMPLETION_KEY) is None
    # A second start is a no-op: no duplicate record.
    assert await db.recover_pending_completions() == []
    assert len(await db.get_task_completions(tid)) == 1


async def test_recovery_replay_is_idempotent_if_the_draft_survives_its_save(db):
    """A restart between the recovered save and the draft delete saves nothing twice."""
    tid = await _in_progress(db)
    with pytest.raises(_Restart):
        await _handler(db, _CrashAfterTransition).execute("task_close", dict(CLOSE_ARGS))
    draft = await db.get_task_meta(tid, PENDING_COMPLETION_KEY)
    from src.models import TaskCompletion

    await db.save_task_completion(TaskCompletion(**draft["completion"]))
    assert await db.recover_pending_completions() == [tid]
    assert [c.id for c in await db.get_task_completions(tid)] == [draft["completion"]["id"]]


async def test_accepted_retry_leg_is_recovered_on_its_ready_status(db):
    """READY is not proof either way: an accepted transient-failure close is."""
    tid = await _in_progress(db)
    args = {**CLOSE_ARGS, "outcome": "fail", "failure_class": "transient"}
    with pytest.raises(_Restart):
        await _handler(db, _CrashAfterTransition).execute("task_close", args)
    assert (await db.get_task(tid)).status == TaskStatus.READY
    assert await db.recover_pending_completions() == [tid]
    record = await db.get_task_completion(tid)
    assert (record.outcome, record.failure_class) == ("fail", "transient")


async def test_restart_before_transition_drops_draft(db):
    await _crash_before_transition(db)
    await _assert_dropped(db)
    assert (await db.get_task("t1")).status == TaskStatus.IN_PROGRESS


async def _move(db, tid: str, status: str, context: str) -> None:
    """Move *tid* by some path that is not this close.

    ``CANCELLED`` is no ``TaskStatus`` member, but integration readers treat
    it as terminal (``TERMINAL_TASK_STATES``); a row can carry it, so a draft
    must not be recovered on it either.
    """
    if status == "CANCELLED":
        async with db.immediate() as conn:
            await conn.execute(
                update(tasks).where(tasks.c.id == tid).values(
                    status=status, assigned_agent_id=None
                )
            )
        return
    await db.transition_task(
        tid, TaskStatus(status), context=context, force=True, assigned_agent_id=None
    )


@pytest.mark.parametrize(
    "status,context",
    [
        # The stale-state reset requeues an interrupted IN_PROGRESS task.
        ("READY", "restart_requeue"),
        ("DEFINED", "test_redefine"),
        ("FAILED", "unrelated_failure"),
        ("CANCELLED", "operator_cancel"),
        # Abandonment closes a task COMPLETED (work_outcome abandoned).
        ("COMPLETED", "abandoned"),
        ("BLOCKED", "unrelated_block"),
    ],
)
async def test_status_without_accepted_identity_never_recovers(db, status, context):
    """An unaccepted draft on any later status is dropped, never a fabricated pass."""
    await _crash_before_transition(db)
    await _move(db, "t1", status, context)
    await _assert_dropped(db)


async def test_transition_without_the_close_identity_never_recovers(db):
    """A committed transition that does not carry this close's identity proves nothing."""
    tid = await _in_progress(db)
    with pytest.raises(_Restart):
        await _handler(db, _UnmarkedTransition).execute("task_close", dict(CLOSE_ARGS))
    assert (await db.get_task(tid)).status == TaskStatus.COMPLETED
    assert await db.get_task_meta(tid, ACCEPTED_CLOSE_KEY) is None
    await _assert_dropped(db)


async def test_stale_claim_epoch_never_recovers(db):
    """The task was claimed again after the accepted close: its draft is stale."""
    tid = await _in_progress(db)
    with pytest.raises(_Restart):
        await _handler(db, _CrashAfterTransition).execute("task_close", dict(CLOSE_ARGS))
    await db.transition_task(tid, TaskStatus.READY, context="reopen", force=True)
    await db.bump_claim_epoch(tid)
    await _assert_dropped(db)


async def test_an_earlier_accepted_close_does_not_vouch_for_a_later_draft(db):
    tid = await _in_progress(db)
    first = await _handler(db, _Accepting).execute("task_close", dict(CLOSE_ARGS))
    assert first["success"] is True
    earlier = await db.get_task_meta(tid, ACCEPTED_CLOSE_KEY)
    # Reopened and claimed again under the same epoch; the second close dies
    # before its transition, then something unrelated cancels the task.
    await db.transition_task(tid, TaskStatus.IN_PROGRESS, context="reopen", force=True)
    with pytest.raises(_Restart):
        await _handler(db, _CrashBeforeTransition).execute(
            "task_close", {**CLOSE_ARGS, "summary": "second"}
        )
    await _move(db, tid, "CANCELLED", "operator_cancel")
    assert await db.get_task_meta(tid, ACCEPTED_CLOSE_KEY) == earlier

    assert await db.recover_pending_completions() == []
    assert [c.summary for c in await db.get_task_completions(tid)] == ["did the thing"]
    assert await db.get_task_meta(tid, PENDING_COMPLETION_KEY) is None


@pytest.mark.parametrize("field", ["session_id", "claim_epoch", "completion_id"])
async def test_identity_must_match_in_every_field(db, field):
    tid = await _in_progress(db)
    with pytest.raises(_Restart):
        await _handler(db, _CrashAfterTransition).execute("task_close", dict(CLOSE_ARGS))
    accepted = await db.get_task_meta(tid, ACCEPTED_CLOSE_KEY)
    forged = {**accepted, field: 7 if field == "claim_epoch" else "someone-else"}
    await db.set_task_meta(tid, ACCEPTED_CLOSE_KEY, forged)
    await _assert_dropped(db)


async def test_unbound_draft_on_a_terminal_task_is_not_synthesized(db):
    """A draft without identity (16d8a5419's shape) proves nothing, even on COMPLETED."""
    tid = await _in_progress(db)
    closed = await _handler(db, _Accepting).execute("task_close", dict(CLOSE_ARGS))
    assert closed["success"] is True
    record = (await db.get_task_completions(tid))[0]
    legacy = {
        "id": "legacy-draft", "task_id": tid, "outcome": "pass", "summary": "invented",
        "completed_at": record.completed_at + 1,
    }
    await db.set_task_meta(tid, PENDING_COMPLETION_KEY, legacy)
    await db.set_task_meta(
        tid, ACCEPTED_CLOSE_KEY,
        {"completion_id": "legacy-draft", "session_id": None, "claim_epoch": 0},
    )
    assert await db.recover_pending_completions() == []
    assert [c.id for c in await db.get_task_completions(tid)] == [record.id]
    assert await db.get_task_meta(tid, PENDING_COMPLETION_KEY) is None


async def test_malformed_draft_is_dropped(db):
    tid = await _in_progress(db)
    await db.set_task_meta(tid, PENDING_COMPLETION_KEY, ["not", "a", "draft"])
    await _assert_dropped(db)


async def test_refused_close_clears_its_draft(db):
    tid = await _in_progress(db)
    result = await _handler(db, _RefuseVerification).execute("task_close", dict(CLOSE_ARGS))
    assert result["result"] == "verification_failed"
    assert await db.get_task_meta(tid, PENDING_COMPLETION_KEY) is None
    # Even if the task later leaves its claim for an unrelated reason.
    await _move(db, tid, "CANCELLED", "operator_cancel")
    await _assert_dropped(db)


async def test_stale_claim_refusal_clears_its_draft(db):
    tid = await _in_progress(db)
    result = await _handler(db, _Accepting).execute(
        "task_close", {**CLOSE_ARGS, "claim_epoch": 5}
    )
    assert result["success"] is False
    assert await db.get_task_meta(tid, PENDING_COMPLETION_KEY) is None
    assert (await db.get_task(tid)).status == TaskStatus.IN_PROGRESS


async def test_unexpected_failure_before_acceptance_clears_its_draft(db):
    tid = await _in_progress(db)
    result = await _handler(db, _FailBeforeTransition).execute("task_close", dict(CLOSE_ARGS))
    assert result.get("success") is not True
    assert await db.get_task_meta(tid, PENDING_COMPLETION_KEY) is None
    await _move(db, tid, "FAILED", "unrelated_failure")
    await _assert_dropped(db)


async def test_unexpected_failure_after_acceptance_keeps_its_draft_for_recovery(db):
    tid = await _in_progress(db)
    result = await _handler(db, _FailAfterTransition).execute("task_close", dict(CLOSE_ARGS))
    assert result.get("success") is not True
    assert (await db.get_task(tid)).status == TaskStatus.COMPLETED
    draft = await db.get_task_meta(tid, PENDING_COMPLETION_KEY)
    assert draft is not None
    assert await db.recover_pending_completions() == [tid]
    assert [c.id for c in await db.get_task_completions(tid)] == [draft["identity"]["completion_id"]]


async def test_discard_never_touches_another_close_draft(db):
    tid = "t1"
    draft = await _crash_before_transition(db)
    assert await db.discard_pending_completion(tid, "some-other-close") is False
    assert await db.get_task_meta(tid, PENDING_COMPLETION_KEY) == draft
    assert await db.discard_pending_completion(tid, draft["identity"]["completion_id"]) is True
    assert await db.get_task_meta(tid, PENDING_COMPLETION_KEY) is None


async def test_accepted_marker_is_written_with_the_transition_or_not_at_all(db):
    """The identity commits in the status write's transaction: a fenced-out
    transition leaves neither."""
    from src.database.queries.task_queries import StaleClaim

    tid = await _in_progress(db)
    identity = {"completion_id": "c1", "session_id": "s1", "claim_epoch": 0}
    with pytest.raises(StaleClaim):
        await db.transition_task(
            tid, TaskStatus.COMPLETED, context="session_close",
            expect_claim_epoch=3, accepted_close=identity,
        )
    assert await db.get_task_meta(tid, ACCEPTED_CLOSE_KEY) is None
    assert (await db.get_task(tid)).status == TaskStatus.IN_PROGRESS

    await db.transition_task(
        tid, TaskStatus.COMPLETED, context="session_close", accepted_close=identity
    )
    assert await db.get_task_meta(tid, ACCEPTED_CLOSE_KEY) == identity
