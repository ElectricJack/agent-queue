"""An accepted ``task close`` finishes its tail whatever its caller does (amber-flare-10).

A close whose terminal transition committed still owes the completion record,
the draft discard and the release of the session's pool claim.  Six closes on
the live box sat for minutes behind a slow post-transition pull-request open
while their callers timed out, and the workers' retried claims unwound the
still-held binding as stale: the completed task was flagged
``claim_binding_stale`` and the session drained.

These run the real ``CommandHandler`` and ``Orchestrator`` on a disposable
PostgreSQL database and prove, against a controlled slow tail:

* a cancelled caller cannot strand the close -- exactly one record, the draft
  gone, the original claim released, and no ``--claim-next`` on its behalf;
* a claim from the same session while the close is in flight answers
  ``close_in_flight`` and leaves the binding to the close, while any identity
  that is not this session's own accepted close keeps the stale unwind;
* a slow pull-request open is bounded and finishes detached.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.commands import session_commands
from src.database.queries.result_queries import PENDING_COMPLETION_KEY, close_identity
from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY
from src.git.manager import GitManager
from src.models import TaskStatus
from src.orchestrator import execution
from src.orchestrator.stranded_work import StrandedWork
from tests import test_pool_lifecycle_integration as lifecycle
from tests.test_close_unpushed_work import _commit, _git
from tests.test_pool_lifecycle_integration import (
    only_pool_session,
    ready,
    scoped,
    settled_claim,
    single_worker_pool,
)

# The pool lifecycle module's real Orchestrator, CommandHandler and database.
config = lifecycle.config
db = lifecycle.db
orch = lifecycle.orch
handler = lifecycle.handler


class _Gate:
    """Hold one awaited call until the test lets it through."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def wrap(self, fn):
        async def held(*args, **kwargs):
            self.entered.set()
            await self.release.wait()
            return await fn(*args, **kwargs)

        return held


async def _claimed(orch, db, handler, tid="t1"):
    await single_worker_pool(db)
    await ready(db, tid)
    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    session = await only_pool_session(db)
    res = await settled_claim(scoped(handler, session.id), {"next": True, "wait": 2})
    assert res["result"] == "claimed", res
    assert res["task"]["id"] == tid, res
    return session, res["claim_epoch"]


def _close_args(epoch, *, claim_next=False):
    return {
        "outcome": "pass",
        "work_outcome": "shipped",
        "summary": "done",
        "claim_next": claim_next,
        "wait": 0,
        "claim_epoch": epoch,
    }


async def _settle_close_attempts():
    await asyncio.gather(*list(session_commands._CLOSE_ATTEMPTS))


class TestCancelledCaller:
    async def test_cancelled_close_still_records_once_and_releases_the_claim(
        self, orch, db, handler, monkeypatch
    ):
        session, epoch = await _claimed(orch, db, handler)
        claim_file = Path(session.work_dir) / ".aq" / "claim.json"
        gate = _Gate()
        monkeypatch.setattr(db, "save_task_completion", gate.wrap(db.save_task_completion))
        claims: list[dict] = []
        real_claim = handler._cmd_task_claim

        async def spy_claim(args):
            claims.append(args)
            return await real_claim(args)

        monkeypatch.setattr(handler, "_cmd_task_claim", spy_claim)

        caller = asyncio.ensure_future(handler._cmd_task_close(_close_args(epoch, claim_next=True)))
        await asyncio.wait_for(gate.entered.wait(), 10)
        # The transition committed; the record, discard and release are owed.
        assert (await db.get_task("t1")).status == TaskStatus.COMPLETED
        assert await db.accepted_close_in_flight(
            "t1", session_id=session.id, claim_epoch=epoch
        )
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert session_commands._CLOSE_ATTEMPTS, "the close must outlive its caller"

        gate.release.set()
        await asyncio.wait_for(_settle_close_attempts(), 10)

        completions = await db.get_task_completions("t1")
        assert [(c.outcome, c.summary) for c in completions] == [("pass", "done")]
        accepted = await db.get_task_meta("t1", ACCEPTED_CLOSE_KEY)
        assert accepted["completion_id"] == completions[0].id
        assert await db.get_task_meta("t1", PENDING_COMPLETION_KEY) is None
        after = await db.get_session(session.id)
        assert (after.task_id, after.claim_phase) == (None, None)
        assert not claim_file.exists()
        task = await db.get_task("t1")
        assert (task.status, task.claim_epoch) == (TaskStatus.COMPLETED, epoch)
        assert await db.get_task_meta("t1", "needs_attention") is None
        # Nobody is left to read a next claim, so none was taken.
        assert claims == []

        # A retry of the cancelled close is refused, never a second record.
        retried = await scoped(handler, session.id)._cmd_task_close(
            {**_close_args(epoch), "task_id": "t1"}
        )
        assert retried["success"] is False
        assert len(await db.get_task_completions("t1")) == 1

    async def test_uncancelled_close_keeps_claim_next_and_drain(self, orch, db, handler):
        session, epoch = await _claimed(orch, db, handler)
        closed = await handler._cmd_task_close(_close_args(epoch, claim_next=True))
        assert closed["success"], closed
        assert closed["next"]["result"] == "drain_requested"
        after = await db.get_session(session.id)
        assert (after.task_id, after.desired_state) == (None, "stopped")
        assert len(await db.get_task_completions("t1")) == 1
        assert not session_commands._CLOSE_ATTEMPTS


@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_unknown_failure_close_keeps_pool_claim_and_slot(
    orch, db, handler, monkeypatch, cancel_caller
):
    session, epoch = await _claimed(orch, db, handler)
    root = Path(session.work_dir)
    _git(["init", "--initial-branch=aq/t1"], cwd=session.work_dir)
    _git(["config", "user.name", "Test"], cwd=session.work_dir)
    _git(["config", "user.email", "t@t.com"], cwd=session.work_dir)
    sha = _commit(session.work_dir, "work.py")
    (root / "work.py").write_text("unfinished work")
    (root / "untracked.py").write_text("untracked work")
    orch.git = GitManager()
    before_task = await db.get_task("t1")
    before_session = await db.get_session(session.id)
    before_ws = await db.get_workspace_for_task("t1")
    claim_file = root / ".aq" / "claim.json"
    claim = claim_file.read_bytes()
    dirty = _git(["status", "--porcelain"], cwd=session.work_dir)
    gate = _Gate()

    async def unknown(*args, **kwargs):
        gate.entered.set()
        await gate.release.wait()
        return StrandedWork(status="unknown", error="injected inspection failure")

    monkeypatch.setattr("src.orchestrator.stranded_work.preserve_unpushed_work", unknown)
    args = {**_close_args(epoch, claim_next=True), "outcome": "fail", "session_id": session.id}
    caller = asyncio.create_task(scoped(handler, session.id)._cmd_task_close(args))
    try:
        await asyncio.wait_for(gate.entered.wait(), 10)
        if cancel_caller:
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
        gate.release.set()
        if cancel_caller:
            await asyncio.wait_for(_settle_close_attempts(), 10)
        else:
            result = await asyncio.wait_for(caller, 10)
            assert result["success"] is False, result
            assert result["unmerged"]["status"] == "unknown"
            assert "next" not in result
    finally:
        gate.release.set()
        await asyncio.gather(caller, return_exceptions=True)
        await _settle_close_attempts()

    after_task = await db.get_task("t1")
    assert (after_task.status, after_task.claim_epoch, after_task.assigned_agent_id,
            after_task.branch_name, after_task.retry_count) == (
        before_task.status, epoch, before_task.assigned_agent_id,
        before_task.branch_name, before_task.retry_count,
    )
    after_session = await db.get_session(session.id)
    assert (after_session.task_id, after_session.claim_phase, after_session.instance_token,
            after_session.state, after_session.desired_state) == (
        "t1", "active", before_session.instance_token,
        before_session.state, before_session.desired_state,
    )
    assert await db.get_workspace_for_task("t1") == before_ws
    assert claim_file.read_bytes() == claim
    assert await db.get_task_completion("t1") is None
    assert await db.get_task_meta("t1", ACCEPTED_CLOSE_KEY) is None
    assert await db.get_task_meta("t1", PENDING_COMPLETION_KEY) is None
    assert _git(["rev-parse", "HEAD"], cwd=session.work_dir) == sha
    assert _git(["status", "--porcelain"], cwd=session.work_dir) == dirty
    assert (root / "work.py").read_text() == "unfinished work"
    assert (root / "untracked.py").read_text() == "untracked work"
    orch._worktree_slots.return_value.restore_slot_after_task.assert_not_called()


class TestClaimDuringAcceptedClose:
    async def test_setup_waits_for_retained_preparation(self, orch, db, handler, monkeypatch):
        # A busy runner can exhaust the claim response window before setup
        # finishes. Exercise that boundary without depending on runner speed.
        monkeypatch.setattr("src.commands.claim_commands._PREPARATION_RESPONSE_SECONDS", 0)
        real_claim = handler._cmd_task_claim
        outcomes = []

        async def expired_response(args):
            result = await real_claim({**args, "wait": 0})
            outcomes.append(result["result"])
            return result

        monkeypatch.setattr(handler, "_cmd_task_claim", expired_response)
        session, epoch = await _claimed(orch, db, handler)
        assert outcomes == ["claim_in_progress", "claimed"]
        held = await db.get_session(session.id)
        assert (held.task_id, held.claim_phase) == ("t1", "active")
        assert (await db.get_task("t1")).claim_epoch == epoch

    async def test_same_session_claim_leaves_the_binding_to_the_close(
        self, orch, db, handler, monkeypatch
    ):
        orch.config.swarm.fresh_context_per_task = False
        session, epoch = await _claimed(orch, db, handler)
        gate = _Gate()
        monkeypatch.setattr(db, "save_task_completion", gate.wrap(db.save_task_completion))

        closing = asyncio.ensure_future(
            scoped(handler, session.id)._cmd_task_close(_close_args(epoch))
        )
        await asyncio.wait_for(gate.entered.wait(), 10)

        zombie = await scoped(handler, session.id)._cmd_task_claim({"next": True, "wait": 0})
        assert (zombie["result"], zombie["reason"], zombie["task_id"]) == (
            "no_ready_work", "close_in_flight", "t1",
        )
        held = await db.get_session(session.id)
        assert (held.task_id, held.desired_state) == ("t1", "running")
        assert await db.get_task_meta("t1", "needs_attention") is None

        gate.release.set()
        closed = await asyncio.wait_for(closing, 10)
        assert closed["success"], closed
        after = await db.get_session(session.id)
        assert (after.task_id, after.claim_phase, after.desired_state) == (None, None, "running")
        assert len(await db.get_task_completions("t1")) == 1
        assert await db.get_task_meta("t1", "needs_attention") is None

    @pytest.mark.parametrize(
        "forge",
        ["other_session", "other_epoch", "draft_mismatch", "no_draft"],
    )
    async def test_any_other_identity_keeps_the_stale_unwind(
        self, orch, db, handler, monkeypatch, forge
    ):
        orch.config.swarm.fresh_context_per_task = False
        session, epoch = await _claimed(orch, db, handler)
        gate = _Gate()
        monkeypatch.setattr(db, "save_task_completion", gate.wrap(db.save_task_completion))
        closing = asyncio.ensure_future(
            scoped(handler, session.id)._cmd_task_close(_close_args(epoch))
        )
        await asyncio.wait_for(gate.entered.wait(), 10)
        accepted = await db.get_task_meta("t1", ACCEPTED_CLOSE_KEY)
        draft = await db.get_task_meta("t1", PENDING_COMPLETION_KEY)
        if forge == "other_session":
            accepted = {**accepted, "session_id": "someone-else"}
            draft = {**draft, "identity": accepted}
        elif forge == "other_epoch":
            accepted = {**accepted, "claim_epoch": epoch + 1}
            draft = {**draft, "identity": accepted}
        elif forge == "draft_mismatch":
            draft = {**draft, "identity": {**accepted, "completion_id": "forged"}}
        if forge == "no_draft":
            await db.delete_task_meta("t1", PENDING_COMPLETION_KEY)
        else:
            await db.set_task_meta("t1", ACCEPTED_CLOSE_KEY, accepted)
            await db.set_task_meta("t1", PENDING_COMPLETION_KEY, draft)
        assert not await db.accepted_close_in_flight(
            "t1", session_id=session.id, claim_epoch=epoch
        )

        zombie = await scoped(handler, session.id)._cmd_task_claim({"next": True, "wait": 0})
        assert zombie.get("reason") != "close_in_flight", zombie
        # The binding is unwound as before: released, never re-served.
        assert zombie["result"] != "claimed"
        assert (await db.get_session(session.id)).task_id is None

        gate.release.set()
        await asyncio.wait_for(closing, 10)
        # The forged close's task keeps its status; nothing reopened it.
        assert (await db.get_task("t1")).status == TaskStatus.COMPLETED
        assert len(await db.get_task_completions("t1")) == 1


class TestAcceptedCloseInFlight:
    async def test_exact_identity_only(self, db):
        await ready(db, "t1")
        identity = close_identity("c1", session_id="s1", claim_epoch=3)
        assert not await db.accepted_close_in_flight("t1", session_id="s1", claim_epoch=3)
        await db.set_task_meta("t1", ACCEPTED_CLOSE_KEY, identity)
        assert not await db.accepted_close_in_flight("t1", session_id="s1", claim_epoch=3)
        await db.set_task_meta(
            "t1", PENDING_COMPLETION_KEY, {"identity": identity, "completion": {}}
        )
        assert await db.accepted_close_in_flight("t1", session_id="s1", claim_epoch=3)
        assert not await db.accepted_close_in_flight("t1", session_id="s2", claim_epoch=3)
        assert not await db.accepted_close_in_flight("t1", session_id="s1", claim_epoch=4)
        await db.set_task_meta(
            "t1", PENDING_COMPLETION_KEY,
            {"identity": {**identity, "completion_id": "c2"}, "completion": {}},
        )
        assert not await db.accepted_close_in_flight("t1", session_id="s1", claim_epoch=3)
        await db.set_task_meta("t1", PENDING_COMPLETION_KEY, ["not", "a", "draft"])
        assert not await db.accepted_close_in_flight("t1", session_id="s1", claim_epoch=3)


class TestBoundedClosePublication:
    async def test_slow_publication_returns_none_and_finishes_detached(self, monkeypatch):
        monkeypatch.setattr(execution, "CLOSE_PUBLICATION_TIMEOUT_SECONDS", 0.05)
        release = asyncio.Event()
        calls = []

        async def publish():
            calls.append(1)
            await release.wait()
            return {"outcome": "opened", "pr_url": "https://github.com/o/r/pull/1"}

        assert await execution._bounded_close_publication("t1", "pr", publish) is None
        (job,) = execution._DETACHED_CLOSE_PUBLICATIONS
        release.set()
        assert (await job)["outcome"] == "opened"
        assert calls == [1]
        assert not execution._DETACHED_CLOSE_PUBLICATIONS

    async def test_fast_publication_and_its_failure_reach_the_caller(self):
        async def opened():
            return {"outcome": "opened"}

        async def broken():
            raise RuntimeError("GitHub is down")

        assert await execution._bounded_close_publication("t1", "pr", opened) == {
            "outcome": "opened"
        }
        with pytest.raises(RuntimeError, match="GitHub is down"):
            await execution._bounded_close_publication("t1", "pr", broken)
        assert not execution._DETACHED_CLOSE_PUBLICATIONS

    async def test_cancelled_close_does_not_cancel_the_publication(self, monkeypatch):
        release = asyncio.Event()

        async def publish():
            await release.wait()
            return {"outcome": "opened"}

        waiter = asyncio.ensure_future(execution._bounded_close_publication("t1", "pr", publish))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        (job,) = execution._DETACHED_CLOSE_PUBLICATIONS
        release.set()
        assert (await job) == {"outcome": "opened"}

