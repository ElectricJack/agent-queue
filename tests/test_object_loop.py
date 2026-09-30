"""AQ-2 object-loop identity, budget and crash recovery contracts."""

from __future__ import annotations

import copy
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, select, update

from src.commands.object_loop_commands import _charge, _reserve
from src.commands.contracts.object_loop import Reservation, Variant
from src.database.tables import doc_review_revisions, doc_reviews, object_loops, tasks
from src.integration.development import _publishable_object_task
from src.integration.child_delivery import _child_refusal
from src.integration.promotion import PromotionService, PromotionSourceMoved
from src.models import Project, Task, TaskStatus
from src.object_loop.contracts import ScoreReceipt, validate_score_receipt

H = "a" * 64
B = "b" * 64
C = "c" * 64


def variant(name="a", usd=1):
    return Variant(variant_id=name, title=name, hypothesis="try a change", reservation=Reservation(
        usd=usd, calls=1, bakes=1, active_seconds=10,
    ))


def loop_state():
    return {
        "object_id": "rock", "project_id": "p", "attempt_id": "attempt-1", "round_id": 0,
        "incumbent_sha256": H, "reference_sha256": H, "rig_sha256": H,
        "scorer_sha256": H, "render_profile_sha256": H,
        "limits": {"usd": 2, "calls": 2, "bakes": 2, "active_seconds": 20},
        "spent": {"usd": 0, "calls": 0, "bakes": 0, "active_seconds": 0},
        "reserved": {"usd": 0, "calls": 0, "bakes": 0, "active_seconds": 0},
        "final_reserve": {"usd": 0, "calls": 0, "bakes": 0, "active_seconds": 0},
        "score_reservation": {"usd": 0, "calls": 0, "bakes": 0, "active_seconds": 0},
        "wave": [{"variant_id": "a", "task_id": "epic.2"}],
    }


def receipt(**changes):
    data = {
        "schema_version": 1, "object_id": "rock", "attempt_id": "attempt-1",
        "round_id": 0, "variant_id": "a", "task_id": "epic.2",
        "base_candidate_sha256": H, "candidate_sha256": B,
        "source_closure_sha256": B, "params_sha256": B,
        "reference_sha256": H, "rig_sha256": H,
        "scorer_sha256": H, "render_profile_sha256": H,
        "capture_receipts": [{"view_id": "front", "frame_id": "frame-1",
                              "image": {"uri": "artifact://frame-1", "sha256": B},
                              "width": 256, "height": 256, "ready": True, "decoded": True}],
        "artifacts": [{"uri": "artifact://candidate", "sha256": B}],
        "validity": "valid", "per_view": {"front": {"loss": 0.1, "quality_pass": True}},
        "worst_view": "front", "hard_gates": {"geometry": True}, "quality_pass": True,
        "timings": {"capture": 1.0}, "resources": {"peak_mb": 10.0},
        "usage_refs": [], "cost_coverage": {"known": True},
        "hypothesis": "test", "patch_scope": ["Rock.js"],
        "predicted_effect": "rounder", "observed_delta": "measured",
    }
    data.update(changes)
    return data


def test_score_receipts_reject_nonfinite_missing_and_foreign_evidence():
    valid = ScoreReceipt.model_validate(receipt())
    validate_score_receipt(valid, state=loop_state(), task_id="epic.2", mandatory_views={"front"})
    with pytest.raises(ValidationError):
        ScoreReceipt.model_validate(receipt(per_view={"front": {"loss": float("nan"),
                                                                  "quality_pass": True}}))
    with pytest.raises(ValidationError):
        ScoreReceipt.model_validate(receipt(cost_coverage={"nested": [float("nan")]}))
    with pytest.raises(ValueError, match="mandatory view coverage"):
        validate_score_receipt(valid, state=loop_state(), task_id="epic.2",
                               mandatory_views={"front", "back"})
    with pytest.raises(ValueError, match="base_candidate_sha256"):
        validate_score_receipt(ScoreReceipt.model_validate(receipt(base_candidate_sha256=C)),
                               state=loop_state(), task_id="epic.2", mandatory_views={"front"})
    with pytest.raises(ValueError, match="foreign"):
        validate_score_receipt(ScoreReceipt.model_validate(receipt(variant_id="other")),
                               state=loop_state(), task_id="epic.2", mandatory_views={"front"})
    invalid = ScoreReceipt.model_validate(receipt(
        validity="capture_failed", quality_pass=False, worst_view=None,
        per_view={"front": None}, capture_receipts=[{
            "view_id": "front", "ready": False, "decoded": False,
        }],
    ))
    assert invalid.per_view["front"] is None


def test_siblings_reserve_one_aggregate_budget_and_unknown_spend_is_conservative():
    state = loop_state()
    _reserve(state, [variant("a"), variant("b")])
    assert state["reserved"]["usd"] == 2
    _charge(state, None)
    assert state["spent"]["usd"] == 2
    state["round_id"] += 1
    with pytest.raises(ValueError, match="overspent"):
        _reserve(state, [variant("c")])
    state = loop_state()
    state["final_reserve"]["usd"] = 1.5
    with pytest.raises(ValueError, match="overspent"):
        _reserve(state, [variant("a"), variant("b")])
    state = loop_state()
    state["score_reservation"]["usd"] = 0.5
    with pytest.raises(ValueError, match="overspent"):
        _reserve(state, [variant("a"), variant("b")])


def start_args():
    return {
        "project_id": "p", "epic_task_id": "epic", "object_id": "rock",
        "attempt_id": "attempt-1", "incumbent_sha256": H,
        "reference_sha256": H, "rig_sha256": H, "scorer_sha256": H,
        "render_profile_sha256": H, "policy_sha256": H,
        "brief_review_id": "brief-review", "brief_review_revision": 1,
        "brief_review_sha256": C,
        "mandatory_views": ["front"], "noise_band": 0.01,
        "limits": {"usd": 3, "calls": 3, "bakes": 3, "active_seconds": 30},
        "final_reserve": {"usd": 1, "calls": 1, "bakes": 1, "active_seconds": 10},
        "score_reservation": {"usd": 0, "calls": 0, "bakes": 0, "active_seconds": 0},
        "variants": [variant().model_dump()],
    }


async def approved_brief(db):
    gate_id, _ = await db.create_gate("p", "review", "Object brief", await_id="brief-review")
    await db.resolve_gate(gate_id, resolved_by="Jack", resolution="approved")
    async with db.immediate() as conn:
        await conn.execute(insert(doc_reviews).values(
            id="brief-review", project_id="p", author_task_id=None, kind="other",
            title="Object brief", vault_path="projects/p/object-brief.md", current_revision=1,
            state="approved", gate_id=gate_id, decider="user", decided_by="Jack",
            decided_at=1.0, created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(insert(doc_review_revisions).values(
            review_id="brief-review", revision=1, content="brief", content_sha256=C,
            submitted_by="agent", submitted_at=1.0,
        ))
    return gate_id


async def test_bootstrap_hold_and_repeated_reconcile_make_one_candidate(
    command_handler_factory,
):
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p", name="Project"))
    await db.create_task(Task(id="epic", project_id="p", title="Epic", description="Epic",
                              status=TaskStatus.READY))
    brief_gate = await approved_brief(db)
    started = await handler._cmd_object_loop_start(start_args())
    assert started["success"], started
    again = await handler._cmd_object_loop_start(start_args())
    assert again["success"] and not again["created"]
    async with db.immediate() as conn:
        loop = (await conn.execute(select(object_loops).where(
            object_loops.c.object_id == "rock"))).mappings().one()
    assert (await db.get_task(loop.finalization_task_id)).is_blocked
    first = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    assert first["success"], first
    candidate_id = first["state"]["wave"][0]["task_id"]
    assert brief_gate in {g["id"] for g in await db.get_gates_for_task(candidate_id)}
    # Recreate the interruption after task creation but before intent completion.
    crash_state = copy.deepcopy(first["state"])
    crash_state["wave"][0]["task_id"] = None
    crash_state["intent"] = started["state"]["intent"]
    async with db.immediate() as conn:
        await conn.execute(update(object_loops).where(object_loops.c.object_id == "rock")
                           .values(state=crash_state))
    recovered = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    assert recovered["success"], recovered
    assert recovered["state"]["wave"][0]["task_id"] == candidate_id
    async with db.immediate() as conn:
        children = (await conn.execute(select(tasks.c.id).where(
            tasks.c.created_by_kind == "object_loop", tasks.c.created_by_id == "rock",
        ))).scalars().all()
        publishable = (await conn.execute(select(tasks.c.id).where(
            tasks.c.id == candidate_id, _publishable_object_task(tasks),
        ))).scalar_one_or_none()
        finalizer_publishable = (await conn.execute(select(tasks.c.id).where(
            tasks.c.id == loop.finalization_task_id, _publishable_object_task(tasks),
        ))).scalar_one_or_none()
    assert len(children) == 2  # finalizer plus one candidate
    assert publishable is None
    assert finalizer_publishable is None
    await db.close()


async def test_simultaneous_start_keeps_one_finalization_hold(command_handler_factory):
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p", name="Project"))
    await db.create_task(Task(id="epic", project_id="p", title="Epic", description="Epic",
                              status=TaskStatus.READY))
    await approved_brief(db)
    results = await asyncio.gather(
        handler._cmd_object_loop_start(start_args()),
        handler._cmd_object_loop_start(start_args()),
    )
    assert all(result["success"] for result in results), results
    async with db.immediate() as conn:
        rows = (await conn.execute(select(object_loops).where(
            object_loops.c.object_id == "rock"))).mappings().all()
        finalizers = (await conn.execute(select(tasks.c.id).where(
            tasks.c.created_by_kind == "object_loop", tasks.c.created_by_id == "rock",
        ))).scalars().all()
    assert len(rows) == len(finalizers) == 1
    await db.close()


async def test_exhausted_failure_reaches_scoring_fan_in(command_handler_factory):
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p", name="Project"))
    await db.create_task(Task(id="epic", project_id="p", title="Epic", description="Epic",
                              status=TaskStatus.READY))
    await approved_brief(db)
    assert (await handler._cmd_object_loop_start(start_args()))["success"]
    wave = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    candidate_id = wave["state"]["wave"][0]["task_id"]
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == candidate_id)
                           .values(status="FAILED", retry_count=3, max_retries=3))
    scored = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    assert scored["success"] and scored["state"]["score_task_id"]
    again = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    assert again["state"]["score_task_id"] == scored["state"]["score_task_id"]
    await db.close()


async def test_score_promotes_only_valid_completed_evidence_and_reserves_next_wave(
    command_handler_factory,
):
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p", name="Project"))
    await db.create_task(Task(id="epic", project_id="p", title="Epic", description="Epic",
                              status=TaskStatus.READY))
    await approved_brief(db)
    assert (await handler._cmd_object_loop_start(start_args()))["success"]
    wave = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    candidate_id = wave["state"]["wave"][0]["task_id"]
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == candidate_id).values(status="COMPLETED"))
    fan_in = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    score_id = fan_in["state"]["score_task_id"]
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == score_id).values(status="COMPLETED"))
    request = {
        "project_id": "p", "object_id": "rock", "expected_version": fan_in["version"],
        "score_task_id": score_id, "receipts": [receipt(task_id=candidate_id)],
        "action": "continue", "next_variants": [variant("next").model_dump()],
    }
    foreign = dict(request, receipts=[receipt(task_id=candidate_id),
                                      receipt(variant_id="foreign", task_id="other")])
    refused = await handler._cmd_object_score_record(foreign)
    assert not refused["success"] and "foreign variant" in refused["error"]
    request["receipts"] = [receipt(task_id=candidate_id, cost_coverage={"known": False})]
    request["spent"] = {"usd": 0.1, "calls": 0, "bakes": 0, "active_seconds": 1}
    recorded = await handler._cmd_object_score_record(request)
    assert recorded["success"], recorded
    assert recorded["state"]["incumbent_sha256"] == B
    assert recorded["state"]["round_id"] == 1
    assert recorded["state"]["spent"]["usd"] == 1
    assert recorded["state"]["reserved"]["usd"] == 1
    assert (await handler._cmd_object_loop_start(start_args()))["created"] is False
    repeated = await handler._cmd_object_score_record(request)
    assert repeated["outcome"] == "reused"
    next_wave = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    assert next_wave["state"]["wave"][0]["task_id"]
    await db.close()


async def test_checkpoint_reads_only_same_approved_revision(command_handler_factory):
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p", name="Project"))
    await db.create_task(Task(id="epic", project_id="p", title="Epic", description="Epic",
                              status=TaskStatus.READY))
    await approved_brief(db)
    assert (await handler._cmd_object_loop_start(start_args()))["success"]
    gate_id, _ = await db.create_gate("p", "review", "Review rock", await_id="review-1")
    async with db.immediate() as conn:
        await conn.execute(insert(doc_reviews).values(
            id="review-1", project_id="p", author_task_id=None, kind="other",
            title="Rock", vault_path="projects/p/review-rock.md", current_revision=1,
            state="approved", gate_id=gate_id, decider="user", decided_by="Jack",
            decided_at=1.0, created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(insert(doc_review_revisions).values(
            review_id="review-1", revision=1, content="packet", content_sha256=B,
            submitted_by="agent", submitted_at=1.0,
        ))
        row = (await conn.execute(select(object_loops).where(
            object_loops.c.object_id == "rock"))).mappings().one()
        state = dict(row.state)
        state["intent"] = None
        state["wave"] = []
        state["reserved"] = {"usd": 0, "calls": 0, "bakes": 0, "active_seconds": 0}
        state["checkpoint"] = {"review_id": "review-1", "revision": 1,
                               "sha256": B, "candidate_sha256": H, "gate_id": gate_id}
        await conn.execute(update(object_loops).where(object_loops.c.object_id == "rock")
                           .values(state=state))
    approved = await handler._cmd_object_checkpoint_read({"project_id": "p", "object_id": "rock"})
    assert approved["approved"]
    async with db.immediate() as conn:
        await conn.execute(update(doc_reviews).where(doc_reviews.c.id == "review-1")
                           .values(current_revision=2))
    stale = await handler._cmd_object_checkpoint_read({"project_id": "p", "object_id": "rock"})
    assert not stale["approved"]
    refused = await handler._cmd_object_loop_reconcile({
        "project_id": "p", "object_id": "rock", "expected_version": 1,
        "next_variants": [variant("after-review").model_dump()],
    })
    assert not refused["success"]
    async with db.immediate() as conn:
        await conn.execute(update(doc_reviews).where(doc_reviews.c.id == "review-1")
                           .values(current_revision=1))
    continued = await handler._cmd_object_loop_reconcile({
        "project_id": "p", "object_id": "rock", "expected_version": 1,
        "next_variants": [variant("after-review").model_dump()],
    })
    assert continued["success"], continued
    assert continued["state"]["checkpoint"] is None
    assert continued["state"]["last_approved_checkpoint"]["revision"] == 1
    assert continued["state"]["wave"][0]["task_id"]
    await db.close()


async def test_artifact_only_child_refuses_hierarchy_promotion(tmp_path):
    assert _child_refusal({"object_experiment": True}) == (
        "not_eligible", "object evaluation is an artifact-only experiment"
    )
    db = MagicMock()
    db.get_task = AsyncMock(return_value=SimpleNamespace(
        id="candidate", parent_task_id="epic", repo_id="repo", branch_name="branch",
    ))
    db.get_task_meta = AsyncMock(return_value={"object_id": "rock"})
    service = PromotionService(db, data_dir=tmp_path, git_manager=MagicMock())
    request = SimpleNamespace(
        source_task_id="candidate", source_head="a" * 40, source_base="a" * 40,
        expected_target="a" * 40,
    )
    with pytest.raises(PromotionSourceMoved, match="artifact-only"):
        await service._validated_route(request)


@pytest.mark.parametrize("verdict", ["rejected", "withdrawn", "changes_requested"])
async def test_unapproved_promoted_checkpoint_can_record_defect_stop(
    command_handler_factory, verdict,
):
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p", name="Project"))
    await db.create_task(Task(id="epic", project_id="p", title="Epic", description="Epic",
                              status=TaskStatus.READY))
    await approved_brief(db)
    await handler._cmd_object_loop_start(start_args())
    args = {"project_id": "p", "object_id": "rock"}
    wave = await handler._cmd_object_loop_reconcile(args)
    candidate_id = wave["state"]["wave"][0]["task_id"]
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == candidate_id).values(status="COMPLETED"))
    scored = await handler._cmd_object_loop_reconcile(args)
    score_id = scored["state"]["score_task_id"]
    gate_id, _ = await db.create_gate("p", "review", "Review rock", await_id="review-1")
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == score_id).values(status="COMPLETED"))
        await conn.execute(insert(doc_reviews).values(
            id="review-1", project_id="p", kind="other", title="Rock",
            vault_path="projects/p/review-rock.md", current_revision=1,
            state="approved", gate_id=gate_id, decider="user", decided_by="Jack",
            decided_at=1, created_at=1, updated_at=1,
        ))
        await conn.execute(insert(doc_review_revisions).values(
            review_id="review-1", revision=1, content="packet", content_sha256=C,
            submitted_by="agent", submitted_at=1,
        ))
    checkpoint = await handler._cmd_object_score_record({
        **args, "expected_version": scored["version"], "score_task_id": score_id,
        "receipts": [receipt(task_id=candidate_id)], "action": "checkpoint",
        "review_id": "review-1", "review_revision": 1, "review_sha256": C,
    })
    assert checkpoint["success"], checkpoint
    assert checkpoint["state"]["incumbent_sha256"] == B
    async with db.immediate() as conn:
        await conn.execute(update(doc_reviews).where(doc_reviews.c.id == "review-1")
                           .values(state=verdict))
        loop = (await conn.execute(select(object_loops))).mappings().one()
    held = await handler._cmd_object_loop_reconcile(args)
    assert held["outcome"] == "checkpoint_held"
    assert (await db.get_task(loop.finalization_task_id)).is_blocked
    stop = {**args, "expected_version": checkpoint["version"], "stop_reason": "terminal defect"}
    stale = await handler._cmd_object_loop_reconcile(dict(stop, expected_version=0))
    assert not stale["success"]
    # Simulate a crash between committing the stop and releasing its hold.
    resolve = db.resolve_gate
    db.resolve_gate = AsyncMock(side_effect=RuntimeError("interrupted gate release"))
    with pytest.raises(RuntimeError, match="interrupted gate release"):
        await handler._cmd_object_loop_reconcile(stop)
    db.resolve_gate = resolve
    assert (await db.get_task(loop.finalization_task_id)).is_blocked
    recovered = await handler._cmd_object_loop_reconcile(args)
    assert recovered["outcome"] == "stopped"
    assert recovered["version"] == checkpoint["version"] + 1
    repeated = await handler._cmd_object_loop_reconcile(stop)
    assert repeated == recovered
    assert repeated["state"]["checkpoint"] == checkpoint["state"]["checkpoint"]
    assert repeated["state"]["incumbent_sha256"] == B
    assert repeated["state"]["incumbent_artifact"] == checkpoint["state"]["incumbent_artifact"]
    assert repeated["state"]["stop_reason"] == "terminal defect"
    assert "last_approved_checkpoint" not in repeated["state"]
    assert not (await db.get_task(loop.finalization_task_id)).is_blocked
    conflict = await handler._cmd_object_loop_reconcile(dict(stop, stop_reason="different"))
    assert not conflict["success"]
    continued = await handler._cmd_object_loop_reconcile({
        **args, "expected_version": repeated["version"],
        "next_variants": [variant("forbidden").model_dump()],
    })
    assert not continued["success"]
    async with db.immediate() as conn:
        assert (await conn.execute(select(doc_reviews.c.state).where(
            doc_reviews.c.id == "review-1"))).scalar_one() == verdict
        children = (await conn.execute(select(tasks.c.id).where(
            tasks.c.created_by_id == "rock"))).scalars().all()
        assert len(children) == 3
        assert not (await conn.execute(select(tasks.c.id).where(
            tasks.c.id.in_(children), _publishable_object_task(tasks)))).scalars().all()
    await db.close()


@pytest.mark.parametrize("stage", ["intent", "candidate", "scorer"])
async def test_terminal_stop_waits_for_existing_work_without_creating_more(
    command_handler_factory, stage,
):
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p", name="Project"))
    await db.create_task(Task(id="epic", project_id="p", title="Epic", description="Epic",
                              status=TaskStatus.READY))
    await approved_brief(db)
    current = await handler._cmd_object_loop_start(start_args())
    args = {"project_id": "p", "object_id": "rock"}
    work_id = None
    if stage != "intent":
        current = await handler._cmd_object_loop_reconcile(args)
        work_id = current["state"]["wave"][0]["task_id"]
    if stage == "scorer":
        async with db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == work_id).values(status="COMPLETED"))
        current = await handler._cmd_object_loop_reconcile(args)
        work_id = current["state"]["score_task_id"]
    async with db.immediate() as conn:
        # Revoked brief must not prevent a terminal deadline report either.
        await conn.execute(update(doc_reviews).where(doc_reviews.c.id == "brief-review")
                           .values(state="withdrawn"))
        loop = (await conn.execute(select(object_loops))).mappings().one()
        before = (await conn.execute(select(tasks.c.id))).scalars().all()
    stop = {**args, "expected_version": current["version"], "stop_reason": "deadline reached"}
    stopped = await handler._cmd_object_loop_reconcile(stop)
    assert stopped["success"], stopped
    assert stopped["state"]["intent"] is None
    assert stopped["state"]["incumbent_sha256"] == H
    if work_id:
        assert stopped["outcome"] == "waiting_for_settlement"
        assert (await db.get_task(loop.finalization_task_id)).is_blocked
        assert await handler._cmd_object_loop_reconcile(stop) == stopped
        async with db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == work_id)
                               .values(status="FAILED", retry_count=3, max_retries=3))
    settled = await handler._cmd_object_loop_reconcile(args)
    assert settled["outcome"] == "stopped"
    assert settled["version"] == stopped["version"]
    assert not (await db.get_task(loop.finalization_task_id)).is_blocked
    async with db.immediate() as conn:
        assert set((await conn.execute(select(tasks.c.id))).scalars().all()) == set(before)
    await db.close()
