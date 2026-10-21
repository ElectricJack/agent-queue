"""``ensure_fidelity_review``: one role reviewer per held batch, bounded, never a release.

Spec-ingestion held batches, P2 L2.1 (design §5.2): the server owns the
reviewer's route, metadata and batch assignment; a reviewer failure leaves
the tree held and raises exactly one ask; batch-review commands reach only
the live reviewer of that batch.
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import delete, func, select, update

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
from src.database.tables import (
    escalations, human_asks, ingest_batch_receipts, ingest_batches, integration_outbox, tasks,
)
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
        aq_commands=["approved_spec_packet", "task_batch_validate", "task_batch_update",
                     "task_batch_review_record", "task_show", "list_tasks",
                     "get_downstream_tasks"],
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


async def _retire_batch_tasks(handler, batch, *, skip=None):
    """Use supported obsolete closes and explicit dependent rewrites."""
    from src.database.tables import task_dependencies
    from src.sessions.obsolete import ObsoleteClose

    row = await _batch_row(handler, batch["batch_id"])
    async with handler.db.immediate() as conn:
        members = await batches.members(conn, batch["batch_id"])
    ordered = ([row["reviewer_task_id"]] if row["reviewer_task_id"] else [])
    ordered += [m["task_id"] for m in members if m["role"] != "container"]
    ordered += [m["task_id"] for m in members if m["role"] == "container"]
    lifecycle = ObsoleteClose(handler.db, release_owner=None)
    for task_id in ordered:
        if task_id == skip:
            continue
        async with handler.db.immediate() as conn:
            incoming = (await conn.execute(select(task_dependencies).where(
                task_dependencies.c.depends_on_task_id == task_id,
                task_dependencies.c.dep_type != "parent-child",
            ))).mappings().all()
        is_container = any(m["task_id"] == task_id and m["role"] == "container" for m in members)
        await lifecycle.close(task_id, reason="Superseded graph", principal="human:test", **(
            {} if is_container else {
                "satisfied_by": "approved supersession decision:test",
                "rewire": {"remove_edges": [
                    {"from": e["task_id"], "to": task_id, "dep_type": e["dep_type"]}
                    for e in incoming
                ]},
            }
        ))


async def _retire_batch(handler, batch, **overrides):
    return await handler.execute("task_batch_retire", {
        "batch_id": batch["batch_id"], "expected_revision": batch["structure_revision"],
        "reason": "Approved graph supersession", **overrides,
    })


async def test_batch_retire_preserves_graph_source_receipts_and_holds(handler):
    from src.database.tables import events, task_proposals

    batch, scope, request = await _review_request(handler)
    assert (await _record(handler, scope, request))["success"]
    await handler.db.update_session(scope.session_id, state="stopped", desired_state="stopped")
    reviewer = (await _batch_row(handler, batch["batch_id"]))["reviewer_task_id"]
    await handler.db.update_task(reviewer, status=TaskStatus.FAILED, assigned_agent_id=None)
    await _retire_batch_tasks(handler, batch)
    before = await _batch_row(handler, batch["batch_id"])
    async with handler.db.immediate() as conn:
        members = await batches.members(conn, batch["batch_id"])
        receipts = (await conn.execute(select(ingest_batch_receipts).where(
            ingest_batch_receipts.c.batch_id == batch["batch_id"],
        ))).mappings().all()
    assert receipts
    result = await _retire_batch(handler, batch)
    assert result["success"] and not result["already_retired"], result
    replay = await _retire_batch(handler, batch)
    assert replay["success"] and replay["already_retired"], replay
    after = await _batch_row(handler, batch["batch_id"])
    assert after == {**before, "state": "retired", "updated_at": after["updated_at"]}
    assert after["release_receipt"] is None and after["released_at"] is None
    assert await _holds(handler, batch["task_ids"]) == {batch["batch_id"]}
    async with handler.db.immediate() as conn:
        assert await batches.members(conn, batch["batch_id"]) == members
        assert (await conn.execute(select(ingest_batch_receipts).where(
            ingest_batch_receipts.c.batch_id == batch["batch_id"],
        ))).mappings().all() == receipts
        assert await conn.scalar(select(task_proposals.c.status).where(
            task_proposals.c.id == batch["batch_id"],
        )) == "discarded"
        audit = (await conn.execute(select(events.c.payload).where(
            events.c.event_type == "task.batch_retired",
        ))).scalars().all()
    assert len(audit) == 1
    reconciled = await handler.execute("batch_reconcile", {})
    assert reconciled["success"], reconciled
    assert (await _batch_row(handler, batch["batch_id"]))["state"] == "retired"


async def test_batch_retire_refuses_unretired_reviewer(handler):
    batch, _ = await _held_batch(handler)
    reviewer = (await _ensure(handler, batch))["task_id"]
    await _retire_batch_tasks(handler, batch, skip=reviewer)
    before = await _batch_row(handler, batch["batch_id"])
    refused = await _retire_batch(handler, batch)
    assert not refused["success"] and refused["error_code"] == "retirement_required", refused
    assert await _batch_row(handler, batch["batch_id"]) == before


@pytest.mark.parametrize("blocker", ["status_only", "no_change", "cleanup", "claim", "owner",
                                     "dependent", "session", "hold", "revision", "project",
                                     "reason"])
async def test_batch_retire_refusals_preserve_batch_and_holds(handler, blocker):
    from src.database.queries.claim_queries import CLAIMED_BY_SESSION_KEY
    from src.database.tables import integration_branch_owners

    batch, _ = await _held_batch(handler)
    await _retire_batch_tasks(handler, batch)
    task_id = batch["task_ids"][0]
    overrides = {}
    if blocker in {"status_only", "no_change", "cleanup"}:
        marker = await handler.db.get_task_meta(task_id, "obsolete")
        if blocker == "status_only":
            await handler.db.delete_task_meta(task_id, "obsolete")
        else:
            if blocker == "no_change":
                marker["disposition"] = "no_change"
            else:
                marker["cleanup"]["state"] = "pending"
            await handler.db.set_task_meta(task_id, "obsolete", marker)
    elif blocker == "claim":
        await handler.db.set_task_meta(task_id, CLAIMED_BY_SESSION_KEY, "stale-session")
    elif blocker == "owner":
        async with handler.db.immediate() as conn:
            await conn.execute(integration_branch_owners.insert().values(
                id="retire-owner", repository_id="repo", ref="refs/heads/retired",
                owner_id=task_id, owner_role="task", fence_token=1,
                handoff_state="reserved", created_at=time.time(), updated_at=time.time(),
            ))
    elif blocker == "dependent":
        from src.models import Task

        await handler.db.create_task(Task(id="still-required", project_id="p1", title="Required",
                                          description="Required dependency"))
        await handler.db.add_dependency("still-required", task_id, "blocks")
    elif blocker == "session":
        await handler.db.create_session(SessionRecord(
            id="late-live", task_id=task_id, project_id="p1", agent_id="ingester",
            profile_id="spec-ingest", harness="codex", provider="fake", name="late-live",
            lifecycle="task", state="running", desired_state="running", work_dir="/tmp",
            epoch="test", instance_token="late-instance", started_at=time.time(),
        ))
    elif blocker == "hold":
        await handler.db.update_task(task_id, held_batch_id=None)
    elif blocker == "revision":
        overrides["expected_revision"] = batch["structure_revision"] + 1
    elif blocker == "project":
        overrides["project_id"] = "other"
    else:
        overrides["reason"] = "   "
    before = await _batch_row(handler, batch["batch_id"])
    refused = await _retire_batch(handler, batch, **overrides)
    assert not refused["success"], refused
    assert await _batch_row(handler, batch["batch_id"]) == before
    assert await _holds(handler, batch["task_ids"]) == (
        {None, batch["batch_id"]} if blocker == "hold" else {batch["batch_id"]}
    )


async def test_batch_retire_accepts_supported_archived_retirement(handler):
    from tests.test_task_batch_commands import _editable_batch, _scoped_update

    batch, _, _ = await _editable_batch(handler, receipt=False)
    updated = await _scoped_update(handler, batch, [{
        "kind": "retire", "task_id": "ui", "reason": "Duplicate UI obligation",
        "satisfied_by": "approved supersession decision:test",
    }])
    assert updated["success"], updated
    archived_id = batch["members"]["ui"]
    assert await handler.db.get_task(archived_id) is None
    batch["structure_revision"] = updated["structure_revision"]
    await _retire_batch_tasks(handler, batch, skip=archived_id)
    retired = await _retire_batch(handler, batch)
    assert retired["success"], retired


async def test_batch_retire_refuses_released_batches_and_worker_authority(handler):
    from src.profiles.capabilities import CapabilityPolicy
    from tests.test_task_batch_commands import _release, _release_ready

    batch, scope, _ = await _release_ready(handler)
    worker = ExecutionPrincipal(
        kind=PrincipalKind.SESSION, project_id="p1", session_id=scope.session_id,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["task_batch_retire"]),
    )
    with principal_context(worker):
        refused = await handler._cmd_task_batch_retire({
            "batch_id": batch["batch_id"], "expected_revision": batch["structure_revision"],
            "reason": "Attempted worker retirement",
        })
    assert not refused["success"] and refused["error_code"] == "not_authorized", refused
    assert (await _release(handler, batch))["success"]
    refused = await _retire_batch(handler, batch)
    assert not refused["success"] and refused["error_code"] == "batch_not_held", refused


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
    async with handler.db.immediate() as conn:
        # Dispatch only adds control links. It must preserve the work graph's
        # stored attestation identity, including across replacement reviewers.
        assert await batches.live_digest(conn, batch["batch_id"]) == batch["structure_digest"]

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


async def _review_request(handler):
    batch, _ = await _held_batch(handler)
    reviewer_id = (await _ensure(handler, batch))["task_id"]
    scope = await _reviewer_session(handler, reviewer_id, "s-review")
    assert await check_request_scope("task_batch_review_record", {"batch_id": batch["batch_id"]},
                                     scope, db=handler.db) is None
    async with handler.db.immediate() as conn:
        active = await batches.members(conn, batch["batch_id"], state="active")
    args = {
        "batch_id": batch["batch_id"], "expected_revision": batch["structure_revision"],
        "expected_digest": batch["structure_digest"],
        "section_verdicts": {m["section_anchor"]: "faithful" for m in active},
        "reverse_coverage_verdict": "faithful", "walkthrough_verdict": "faithful",
        "structural_findings": [],
    }
    return batch, scope, args


async def _record(handler, scope, args):
    # Exercise CommandHandler's authenticated dispatch as the actual reviewer.
    return await handler.execute("task_batch_review_record", {**args, "_scope": {
        "kind": "session", "session_id": scope.session_id,
        "session_instance_token": "instance", "project_id": "p1",
    }})


async def _lose_wakeup(handler, batch_id, event_type):
    async with handler.db.immediate() as conn:
        await conn.execute(delete(integration_outbox).where(
            integration_outbox.c.event_type == event_type,
            integration_outbox.c.payload["batch_id"].as_string() == batch_id,
        ))
        await conn.execute(update(ingest_batches).where(
            ingest_batches.c.batch_id == batch_id,
        ).values(updated_at=time.time() - 120))


async def test_reconcile_reemits_receipt(handler):
    batch, scope, args = await _review_request(handler)
    recorded = await _record(handler, scope, args)
    assert recorded["passing"], recorded
    await _lose_wakeup(handler, batch["batch_id"], "batch.receipt_recorded")
    recovered = await handler.execute("batch_reconcile", {})
    assert not recovered["errors"], recovered
    assert [row["event_type"] for row in recovered["recovered"]] == ["batch.receipt_recorded"]
    # Transport acceptance followed by a crash before release must wake policy again.
    async with handler.db.immediate() as conn:
        await conn.execute(update(integration_outbox).where(
            integration_outbox.c.id == recovered["recovered"][0]["event_id"],
        ).values(delivered_at=time.time()))
        await conn.execute(update(ingest_batches).where(
            ingest_batches.c.batch_id == batch["batch_id"],
        ).values(updated_at=time.time() - 120))
    replay = await handler.execute("batch_reconcile", {})
    assert replay["recovered"][0]["event_id"] != recovered["recovered"][0]["event_id"]
    assert await _holds(handler, batch["task_ids"]) == {batch["batch_id"]}
    assert (await _batch_row(handler, batch["batch_id"]))["reviewer_task_id"] == (
        await handler.db.get_session(scope.session_id)
    ).task_id


async def test_reconcile_redispatches_missing_reviewer(handler):
    batch, _ = await _held_batch(handler)
    await _lose_wakeup(handler, batch["batch_id"], "batch.held")
    recovered = await handler.execute("batch_reconcile", {})
    assert not recovered["errors"], recovered
    assert recovered["recovered"][0]["event_type"] == "batch.held"
    first = await _ensure(handler, batch)
    again = await _ensure(handler, batch)
    assert first["created"] and not again["created"]
    assert first["task_id"] == again["task_id"]
    assert await _holds(handler, batch["task_ids"]) == {batch["batch_id"]}


async def test_release_idempotent_after_crash(handler):
    from tests.test_task_batch_commands import _release, _release_ready

    batch, _, receipt = await _release_ready(handler)
    first = await _release(handler, batch)
    assert first["success"] and not first["already_released"], first
    await _lose_wakeup(handler, batch["batch_id"], "batch.released")
    recovered = await handler.execute("batch_reconcile", {})
    assert recovered["recovered"][0]["event_type"] == "batch.released", recovered
    again = await _release(handler, batch)
    assert again["success"] and again["already_released"], again
    assert again["release_receipt"] == first["release_receipt"]
    assert await _holds(handler, batch["task_ids"]) == {None}
    async with handler.db._engine.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(ingest_batch_receipts).where(
            ingest_batch_receipts.c.batch_id == batch["batch_id"],
            ingest_batch_receipts.c.receipt_id == receipt["receipt_id"],
        ))).scalar_one() == 1


async def test_receipt_requires_every_section(handler):
    batch, scope, args = await _review_request(handler)
    missing = next(iter(args["section_verdicts"]))
    del args["section_verdicts"][missing]
    result = await _record(handler, scope, args)
    assert result["error_code"] == "incomplete_review", result
    assert result["missing_sections"] == [missing]
    assert (await _batch_row(handler, batch["batch_id"]))["state"] == "held"
    async with handler.db.immediate() as conn:
        assert not (await conn.execute(select(ingest_batch_receipts))).first()


@pytest.mark.parametrize("field", ["reverse_coverage_verdict", "walkthrough_verdict"])
async def test_receipt_requires_reverse_coverage_verdict(handler, field):
    _, scope, args = await _review_request(handler)
    del args[field]
    result = await _record(handler, scope, args)
    assert result["error_code"] == "invalid_receipt", result


@pytest.mark.parametrize("field,value", [
    ("reverse_coverage_verdict", "pass"), ("walkthrough_verdict", ""),
    ("section_verdicts", {"section": "approved"}), ("expected_revision", True),
    ("passing", True), ("reviewer_session_id", "forged"),
])
async def test_invalid_receipt_cannot_fabricate_a_pass(handler, field, value):
    _, scope, args = await _review_request(handler)
    result = await _record(handler, scope, {**args, field: value})
    assert result["error_code"] == "invalid_receipt", result


async def test_passing_receipt_freezes_lineage_diff_and_replays(handler):
    batch, scope, args = await _review_request(handler)
    async with handler.db.immediate() as conn:
        await conn.execute(update(ingest_batches).where(
            ingest_batches.c.batch_id == batch["batch_id"],
        ).values(design_review_id="rev-design", design_revision=2,
                 design_content_sha256="design-hash", lineage_kind="resolved",
                 check_results={"last_diff": {"rewritten": ["leaf"]}}))
    args["section_verdicts"] = dict.fromkeys(args["section_verdicts"], "fixed")
    args["structural_findings"] = [{"finding": "Carry constraint", "disposition": "fixed"}]
    result = await _record(handler, scope, args)
    assert result["success"] and result["passing"] and result["state"] == "reviewing", result
    row = await _batch_row(handler, batch["batch_id"])
    evidence = result["receipt"]["structural_findings"]
    assert evidence["findings"] == args["structural_findings"]
    assert evidence["last_diff"] == {"rewritten": ["leaf"]}
    assert evidence["lineage"] == {key: row[key] for key in (
        "source_review_id", "source_revision", "source_content_sha256", "design_review_id",
        "design_revision", "design_content_sha256", "lineage_kind", "decision_id",
    )}
    assert result["receipt"]["reviewer_session_id"] == scope.session_id
    replay = await _record(handler, scope, args)
    assert replay["success"] and replay["replayed"]
    assert replay["receipt"]["receipt_id"] == result["receipt"]["receipt_id"]
    async with handler.db.immediate() as conn:
        events = (await conn.execute(select(integration_outbox).where(
            integration_outbox.c.event_type == "batch.receipt_recorded",
        ))).mappings().all()
        assert len(events) == 1
    assert await _holds(handler, batch["task_ids"]) == {batch["batch_id"]}


@pytest.mark.parametrize("field", ["section_verdicts", "reverse_coverage_verdict", "walkthrough_verdict"])
async def test_scope_question_single_ask(handler, field):
    batch, scope, args = await _review_request(handler)
    if field == "section_verdicts":
        args[field][next(iter(args[field]))] = "scope_question"
    else:
        args[field] = "scope_question"
    args["structural_findings"] = [{"question": "Keep the source constraint?", "section": "core"}]
    result = await _record(handler, scope, args)
    assert result["success"] and not result["passing"] and result["state"] == "needs_scope", result
    replay = await _record(handler, scope, args)
    assert replay["success"] and replay["replayed"] and replay["ask_id"] == result["ask_id"], replay
    async with handler.db.immediate() as conn:
        asks = (await conn.execute(select(human_asks).where(
            human_asks.c.source_kind == "batch_scope",
        ))).mappings().all()
        assert len(asks) == 1
        ask = dict(asks[0])
        assert ask["source_key"] == f"batch-scope:{batch['batch_id']}:1"
        assert ask["context"]["ask_kind"] == "batch_scope"
        assert ask["logical_owner"] == "supervisor-p1" and ask["context_state"] == "complete"
        assert ask["context"]["affected_leaves"]
        assert "Keep the source constraint?" in ask["context"]["why"]
        assert ask["context"]["blocked_work"]
        assert not (await conn.execute(select(integration_outbox).where(
            integration_outbox.c.event_type == "batch.receipt_recorded",
        ))).first()
        assert (await conn.execute(select(func.count()).select_from(integration_outbox).where(
            integration_outbox.c.event_type == "batch.needs_scope",
        ))).scalar_one() == 1
    # A new pass cannot ignore the unresolved scope question.
    faithful = {**args, "section_verdicts": dict.fromkeys(args["section_verdicts"], "faithful"),
                "reverse_coverage_verdict": "faithful", "walkthrough_verdict": "faithful"}
    assert (await _record(handler, scope, faithful))["error_code"] == "needs_scope"
    # A reply is applied by the ask boundary, returning only to held. It never
    # clears a member hold or fabricates a passing receipt.
    intent = {"ask_id": ask["id"], "option_key": "answer", "overall_note": "Keep the constraint.",
              "expected": {k: ask[k] for k in (
                  "decision_generation", "source_fingerprint", "feedback_version")}}
    preview = await handler.execute("ask_prepare_decision", intent)
    assert preview["success"], preview
    answered = await handler.execute("ask_decide", {
        **intent, "preview_id": preview["preview"]["id"], "idempotency_key": "scope-answer",
    })
    assert answered["success"], answered
    row = await _batch_row(handler, batch["batch_id"])
    assert row["state"] == "held" and row["release_receipt"] is None
    assert row["check_results"]["scope_answers"]["1"]["answer"] == "Keep the constraint."
    assert await _holds(handler, batch["task_ids"]) == {batch["batch_id"]}
    async with handler.db.immediate() as conn:
        assert await batches.passing_receipt(conn, batch["batch_id"], 1) is None
    reviewed = await _record(handler, scope, faithful)
    assert reviewed["success"] and reviewed["passing"] and reviewed["state"] == "reviewing", reviewed


@pytest.mark.parametrize("stale", ["revision", "digest", "live_graph"])
async def test_receipt_refuses_stale_graph(handler, stale):
    batch, scope, args = await _review_request(handler)
    if stale == "revision":
        args["expected_revision"] += 1
    elif stale == "digest":
        args["expected_digest"] = "old"
    else:
        await handler.db.update_task(batch["task_ids"][-1], description="Concurrent edit")
    result = await _record(handler, scope, args)
    assert result["error_code"] == "stale_revision" and result["retryable"], result


async def test_receipt_refuses_revoked_reviewer_and_wrong_project(handler):
    batch, scope, args = await _review_request(handler)
    wrong_project = await _record(handler, scope, {**args, "project_id": "other"})
    assert not wrong_project["success"], wrong_project
    async with handler.db.immediate() as conn:
        await batches.assign_reviewer(conn, batch["batch_id"], reviewer_task_id=None)
    assert (await _record(handler, scope, args))["error_code"] == "not_authorized"


async def test_review_control_links_do_not_hide_execution_dependency_changes(handler):
    batch, scope, args = await _review_request(handler)
    reviewer = (await _batch_row(handler, batch["batch_id"]))["reviewer_task_id"]
    await handler.db.add_dependency(reviewer, batch["task_ids"][0], "blocks")
    assert (await _record(handler, scope, args))["error_code"] == "stale_revision"


async def test_scope_question_cannot_replace_a_passing_receipt(handler):
    batch, scope, args = await _review_request(handler)
    result = await _record(handler, scope, args)
    assert result["success"], result
    changed = await _record(handler, scope, {**args, "walkthrough_verdict": "scope_question"})
    assert changed["error_code"] == "receipt_exists", changed
    assert (await _batch_row(handler, batch["batch_id"]))["state"] == "reviewing"
    assert result["receipt"]["structural_findings"]["lineage"]["lineage_kind"] == "none"


async def test_pending_scope_question_cannot_change_on_replay(handler):
    _, scope, args = await _review_request(handler)
    args["walkthrough_verdict"] = "scope_question"
    result = await _record(handler, scope, args)
    assert result["success"], result
    changed = await _record(handler, scope, {
        **args, "structural_findings": [{"question": "An unrelated new choice"}],
    })
    assert changed["error_code"] == "scope_question_exists", changed
    async with handler.db.immediate() as conn:
        assert (await conn.execute(select(func.count()).select_from(
            ingest_batch_receipts,
        ))).scalar_one() == 1


@pytest.mark.parametrize("state", ["held", "reviewing", "needs_scope"])
async def test_read_handlers_repeat_live_batch_boundary(handler, state):
    from dataclasses import asdict

    batch, decision = await _held_batch(handler)
    reviewer_id = (await _ensure(handler, batch))["task_id"]
    scope = await _reviewer_session(handler, reviewer_id, "s-read", epoch=2)
    async with handler.db.immediate() as conn:
        await conn.execute(update(ingest_batches).where(
            ingest_batches.c.batch_id == batch["batch_id"],
        ).values(state=state))
    commands = [("list_tasks", {}), ("task_show", {"task_id": batch["task_ids"][1]}),
        ("get_downstream_tasks", {"task_id": batch["task_ids"][1]}),
        ("approved_spec_packet", {"decision_id": decision["decision_id"],
                                  "packet_sha256": decision["packet_sha256"]})]
    for command, args in commands:
        result = await handler.execute(command, {**args, "_scope": asdict(scope)})
        assert "error" not in result, result
    # Do not trust a successful scope check saved before a reclaim.
    await handler.db.update_task(reviewer_id, claim_epoch=3)
    for command, args in commands:
        result = await handler.execute(command, {**args, "_scope": asdict(scope)})
        assert not result.get("success", True) and "out of scope" in result["error"], result


async def test_read_scope_refuses_client_hints_and_malformed_assignment(handler):
    batch, _ = await _held_batch(handler)
    reviewer_id = (await _ensure(handler, batch))["task_id"]
    scope = await _reviewer_session(handler, reviewer_id, "s-read")
    for args in ({"batch_id": "other"}, {"proposal_id": "other"},
                 {"session_id": "other"}, {"project_id": "other"}):
        assert await check_request_scope("list_tasks", args, scope, db=handler.db) is not None
    for metadata in (["batch_id", batch["batch_id"]], {"batch_id": "other"}):
        await handler.db.set_task_meta(reviewer_id, "fidelity_review", metadata)
        assert await check_request_scope("list_tasks", {}, scope, db=handler.db) is not None


async def test_reader_denies_other_batch_and_cross_project_members(handler):
    from dataclasses import asdict

    from src.models import Project, Task
    from src.database.tables import task_proposals

    batch, _ = await _held_batch(handler)
    reviewer_id = (await _ensure(handler, batch))["task_id"]
    scope = await _reviewer_session(handler, reviewer_id, "s-read")
    await handler.db.create_project(Project(id="p2", name="Foreign"))
    for tid, pid in (("other-batch-member", "p1"), ("foreign-member", "p2")):
        await handler.db.create_task(Task(id=tid, project_id=pid, title=tid,
                                         description="Private batch work"))
    async with handler.db.immediate() as conn:
        await conn.execute(task_proposals.insert().values(
            id="other-batch", project_id="p1", source="other-approved-source", payload="{}",
            status="held", created_at=time.time(), updated_at=time.time(),
        ))
        await batches.create_batch(conn, batch_id="other-batch", project_id="p1",
            source_review_id="other-source", source_revision=1, source_content_sha256="1" * 64,
            decision_id="other-decision", lineage={"lineage_kind": "none"},
            idempotency_key="other-ingestion")
        await batches.add_member(conn, "other-batch", task_id="other-batch-member",
            member_key="other", role="leaf", section_anchor="other", primary_outcome="Private", created_revision=1)
        # Even a corrupt membership row is fenced by task project ownership.
        await batches.add_member(conn, batch["batch_id"], task_id="foreign-member",
            member_key="foreign", role="leaf", section_anchor="foreign", primary_outcome="Private", created_revision=1)
    for tid in ("other-batch-member", "foreign-member"):
        await handler.db.add_dependency(tid, batch["task_ids"][1], "blocks")
        for command in ("task_show", "get_downstream_tasks"):
            args = {"task_id": tid}
            assert await check_request_scope(command, args, scope, db=handler.db) is not None
            result = await handler.execute(command, {**args, "_scope": asdict(scope)})
            assert not result["success"], result
    for command, args in (("list_tasks", {"show_dependencies": True}),
                          ("task_show", {"task_id": batch["task_ids"][1]}),
                          ("get_downstream_tasks", {"task_id": batch["task_ids"][1]})):
        result = await handler.execute(command, {**args, "_scope": asdict(scope)})
        assert "other-batch-member" not in str(result), result
        assert "foreign-member" not in str(result), result
