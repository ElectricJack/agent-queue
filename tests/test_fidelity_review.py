"""``ensure_fidelity_review``: one role reviewer per held batch, bounded, never a release.

Spec-ingestion held batches, P2 L2.1 (design §5.2): the server owns the
reviewer's route, metadata and batch assignment; a reviewer failure leaves
the tree held and raises exactly one ask; batch-review commands reach only
the live reviewer of that batch.
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import func, select, update

from src.api.auth import RequestScope
from src.api.scope import check_request_scope
from src.asks.operations import FIDELITY_REVIEW_DISPATCH, get_operation
from src.commands.fidelity_review_commands import (
    FIDELITY_REVIEW_MAX_DISPATCHES,
    FIDELITY_REVIEW_MAX_RETRIES,
)
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.queries import ingest_batch_queries as batches
from src.database.queries.proposal_queries import existing_graph_edges
from src.database.tables import escalations, tasks
from src.models import Agent, AgentProfile, AgentState, SessionRecord, TaskStatus
from src.profiles.capabilities import DENY_ALL
from src.review_keys import fidelity_review_dedup_key
from tests.test_task_batch_commands import _ingest_assignment, _ingest_propose


@pytest.fixture
async def handler(command_handler_factory):
    h = await command_handler_factory()
    yield h
    if hasattr(h, "_db") and h._db is not None:
        await h._db.close()


async def _held_batch(handler):
    path, scope, decision = await _ingest_assignment(handler)
    result = await _ingest_propose(handler, path, scope, decision)
    assert result["status"] == "held", result
    await handler.db.create_profile(AgentProfile(
        id="fidelity-reviewer", name="Fidelity Reviewer", harness="codex",
        default_class="deep-high", needs_workspace=False,
        aq_commands=["approved_spec_packet", "task_batch_validate", "task_batch_update"],
    ))
    return result, decision


async def _ensure(handler, batch, revision=None):
    return await handler.execute("ensure_fidelity_review", {
        "project_id": "p1", "batch_id": batch["batch_id"],
        "structure_revision": batch["structure_revision"] if revision is None else revision,
    })


async def _batch_row(handler, batch_id):
    async with handler.db.immediate() as conn:
        return await batches.get_batch(conn, batch_id)


async def _dispatch_op(handler, decision):
    async with handler.db.immediate() as conn:
        return await get_operation(conn, decision["decision_id"], FIDELITY_REVIEW_DISPATCH)


async def _escalation_count(handler, batch_id):
    async with handler.db.immediate() as conn:
        return (await conn.execute(select(func.count()).select_from(escalations).where(
            escalations.c.incident_key == f"fidelity-review-exhausted:{batch_id}",
        ))).scalar_one()


async def _holds(handler, task_ids):
    async with handler.db.immediate() as conn:
        return set((await conn.execute(select(tasks.c.held_batch_id).where(
            tasks.c.id.in_(task_ids),
        ))).scalars().all())


async def test_ensure_creates_role_reviewer_once_and_records_assignment(handler):
    batch, decision = await _held_batch(handler)
    first = await _ensure(handler, batch)
    assert first.get("task_id"), first
    assert first == {"success": True, "task_id": first["task_id"], "created": True}, first
    again = await _ensure(handler, batch)
    assert again == {"success": True, "task_id": first["task_id"], "created": False}

    reviewer = await handler.db.get_task(first["task_id"])
    assert reviewer.profile_id == "fidelity-reviewer"
    assert reviewer.route_source == "role"
    assert reviewer.dedup_key == fidelity_review_dedup_key(batch["batch_id"])
    assert reviewer.parent_task_id is None
    assert await handler.db.get_task_meta(reviewer.id, "fidelity_review") == {
        "batch_id": batch["batch_id"], "structure_revision": batch["structure_revision"],
    }
    async with handler.db.immediate() as conn:
        assert (await conn.execute(select(tasks.c.max_retries).where(
            tasks.c.id == reviewer.id,
        ))).scalar_one() == FIDELITY_REVIEW_MAX_RETRIES
    row = await _batch_row(handler, batch["batch_id"])
    assert row["reviewer_task_id"] == reviewer.id
    assert row["reviewer_claim_epoch"] is None and row["reviewer_session_id"] is None
    assert row["state"] == "held"

    # Informational edges to the batch's epics only; nothing blocks on review.
    ids = dict(zip(["core", "api", "ui", "docs", "draft", "final"], batch["task_ids"],
                   strict=True))
    edges = await existing_graph_edges(handler.db, "p1")
    assert {(frm, to) for frm, to, kind in edges if frm == reviewer.id} == {
        (reviewer.id, ids["core"]), (reviewer.id, ids["docs"]),
    }
    assert {kind for frm, _, kind in edges if frm == reviewer.id} == {"related"}
    op = await _dispatch_op(handler, decision)
    assert op["status"] == "succeeded"
    assert op["receipt"]["task_id"] == reviewer.id and op["receipt"]["attempts"] == 1


async def test_ensure_refuses_sessions_stale_revisions_and_unknown_batches(handler):
    batch, _ = await _held_batch(handler)
    session = ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL,
                                 session_id="s-ingest", project_id="p1")
    with principal_context(session):
        refused = await handler._cmd_ensure_fidelity_review({
            "project_id": "p1", "batch_id": batch["batch_id"], "structure_revision": 1,
        })
    assert not refused["success"] and "ingestion policy" in refused["error"]
    smuggled = await handler.execute("ensure_fidelity_review", {
        "project_id": "p1", "batch_id": batch["batch_id"], "structure_revision": 1,
        "profile_id": "reviewer",
    })
    assert not smuggled["success"]
    stale = await _ensure(handler, batch, revision=batch["structure_revision"] + 1)
    assert not stale["success"] and stale["code"] == "batch.stale_revision", stale
    missing = await handler.execute("ensure_fidelity_review", {
        "project_id": "p1", "batch_id": "no-such-batch", "structure_revision": 1,
    })
    assert not missing["success"] and missing["code"] == "ask.not_found", missing
    assert (await _batch_row(handler, batch["batch_id"]))["reviewer_task_id"] is None


async def test_reviewer_failure_redispatch_bounded(handler):
    """Scenario 5: a failed reviewer leaves the tree held, retry is bounded, one ask."""
    batch, decision = await _held_batch(handler)
    batch_id = batch["batch_id"]

    # A reviewer that closed without a receipt, or vanished, is replaced, but
    # only up to the dispatch bound.
    reviewers = []
    for _ in range(FIDELITY_REVIEW_MAX_DISPATCHES):
        dispatched = await _ensure(handler, batch)
        assert dispatched["success"] and dispatched["created"], dispatched
        reviewers.append(dispatched["task_id"])
        await handler.db.update_task(dispatched["task_id"], status=TaskStatus.COMPLETED)
    assert len(set(reviewers)) == FIDELITY_REVIEW_MAX_DISPATCHES
    assert (await _dispatch_op(handler, decision))["receipt"]["attempts"] == (
        FIDELITY_REVIEW_MAX_DISPATCHES
    )
    bounded = await _ensure(handler, batch)
    assert not bounded["success"] and bounded["error_code"] == "fidelity_review_exhausted"
    assert bounded["ask_id"] and "dispatch_bound" in bounded["error"]

    # Every later wakeup answers with the same ask; nothing new is dispatched.
    replay = await _ensure(handler, batch)
    assert replay["escalation_id"] == bounded["escalation_id"]
    assert replay["ask_id"] == bounded["ask_id"]
    assert await _escalation_count(handler, batch_id) == 1
    assert len(await handler.db.list_tasks(project_id="p1")) == 1 + 6 + len(reviewers)

    # The tree stays held: no release on timeout or reviewer completion.
    row = await _batch_row(handler, batch_id)
    assert row["state"] == "held"
    assert await _holds(handler, batch["task_ids"]) == {batch_id}
    assert await handler.db.ingest_held_task_ids(set(batch["task_ids"])) == set(batch["task_ids"])
    op = await _dispatch_op(handler, decision)
    assert op["status"] == "failed" and op["outcome"] == "exhausted"
    assert op["receipt"]["exhausted"] == "dispatch_bound"
    assert op["receipt"]["ask_id"] == bounded["ask_id"]


async def test_reviewer_task_failure_after_retries_raises_one_ask(handler):
    batch, decision = await _held_batch(handler)
    batch_id = batch["batch_id"]
    dispatched = await _ensure(handler, batch)
    reviewer_id = dispatched["task_id"]
    # Task-level retries exhausted: the reviewer is terminally FAILED.
    await handler.db.update_task(reviewer_id, status=TaskStatus.FAILED,
                                 retry_count=FIDELITY_REVIEW_MAX_RETRIES)
    exhausted = await _ensure(handler, batch)
    assert not exhausted["success"] and exhausted["error_code"] == "fidelity_review_exhausted"
    assert "reviewer_failed" in exhausted["error"]
    again = await _ensure(handler, batch)
    assert again["escalation_id"] == exhausted["escalation_id"]
    assert await _escalation_count(handler, batch_id) == 1
    async with handler.db.immediate() as conn:
        incident = (await conn.execute(select(escalations).where(
            escalations.c.id == exhausted["escalation_id"],
        ))).mappings().one()
    assert incident["task_id"] == reviewer_id and incident["source_kind"] == "fidelity_review"
    assert (await _batch_row(handler, batch_id))["state"] == "held"
    assert await _holds(handler, batch["task_ids"]) == {batch_id}
    assert (await _dispatch_op(handler, decision))["status"] == "failed"
    # No replacement reviewer was created after the failure.
    assert len(await handler.db.list_tasks(project_id="p1")) == 1 + 6 + 1


async def _reviewer_session(handler, task_id, session_id, *, epoch=0, agent_id="fidelity"):
    if await handler.db.get_agent(agent_id) is None:
        await handler.db.create_agent(Agent(id=agent_id, name=agent_id,
                                            profile_id="fidelity-reviewer"))
    await handler.db.update_task(task_id, status=TaskStatus.IN_PROGRESS,
                                 assigned_agent_id=agent_id, claim_epoch=epoch)
    await handler.db.update_agent(agent_id, state=AgentState.BUSY, current_task_id=task_id)
    await handler.db.create_session(SessionRecord(
        id=session_id, task_id=task_id, project_id="p1", agent_id=agent_id,
        profile_id="fidelity-reviewer", harness="codex", provider="fake", name=session_id,
        lifecycle="pool", state="running", desired_state="running", work_dir="/tmp",
        epoch="test", instance_token="instance", started_at=time.time(),
        last_claim_epoch=epoch,
    ))
    return RequestScope(kind="session", session_id=session_id,
                        session_instance_token="instance", project_id="p1")


async def test_batch_review_commands_reach_only_the_live_reviewer(handler):
    batch, _ = await _held_batch(handler)
    batch_id = batch["batch_id"]
    reviewer_id = (await _ensure(handler, batch))["task_id"]
    scope = await _reviewer_session(handler, reviewer_id, "s-review")

    args = {"batch_id": batch_id}
    assert await check_request_scope("task_batch_validate", args, scope, db=handler.db) is None
    assert args["project_id"] == "p1"
    row = await _batch_row(handler, batch_id)
    assert row["reviewer_claim_epoch"] == 0 and row["reviewer_session_id"] == "s-review"
    for command in ("task_batch_update", "task_batch_review_record"):
        assert await check_request_scope(command, {"batch_id": batch_id}, scope,
                                         db=handler.db) is None
    # Another batch, a forged session id, and a non-reviewer session are refused.
    assert await check_request_scope("task_batch_validate", {"batch_id": "other"}, scope,
                                     db=handler.db) is not None
    assert await check_request_scope(
        "task_batch_validate", {"batch_id": batch_id, "session_id": "s-other"}, scope,
        db=handler.db,
    ) is not None
    ingest_scope = RequestScope(kind="session", session_id="s-ingest",
                                session_instance_token="instance", project_id="p1",
                                task_id=batch.get("ingest_task_id") or None)
    assert await check_request_scope("task_batch_validate", {"batch_id": batch_id},
                                     ingest_scope, db=handler.db) is not None

    # A reclaim under a higher epoch moves the binding forward and revokes
    # the previous holder; the epoch never moves back.
    reclaimed = await _reviewer_session(handler, reviewer_id, "s-review-2", epoch=1)
    assert await check_request_scope("task_batch_validate", {"batch_id": batch_id},
                                     reclaimed, db=handler.db) is None
    row = await _batch_row(handler, batch_id)
    assert row["reviewer_claim_epoch"] == 1 and row["reviewer_session_id"] == "s-review-2"
    assert await check_request_scope("task_batch_validate", {"batch_id": batch_id}, scope,
                                     db=handler.db) is not None

    # Reassignment (reroute) rewrites the reviewer and clears the binding.
    async with handler.db.immediate() as conn:
        await batches.assign_reviewer(conn, batch_id, reviewer_task_id=None)
    assert await check_request_scope("task_batch_validate", {"batch_id": batch_id},
                                     reclaimed, db=handler.db) is not None


async def test_reviewer_scope_ends_with_the_reviewable_state(handler):
    batch, _ = await _held_batch(handler)
    batch_id = batch["batch_id"]
    reviewer_id = (await _ensure(handler, batch))["task_id"]
    scope = await _reviewer_session(handler, reviewer_id, "s-review")
    assert await check_request_scope("task_batch_validate", {"batch_id": batch_id}, scope,
                                     db=handler.db) is None
    from src.database.tables import ingest_batches

    async with handler.db.immediate() as conn:
        await conn.execute(update(ingest_batches).where(
            ingest_batches.c.batch_id == batch_id,
        ).values(state="released"))
    assert await check_request_scope("task_batch_validate", {"batch_id": batch_id}, scope,
                                     db=handler.db) is not None
