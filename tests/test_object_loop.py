"""AQ-2 object-loop identity, budget and crash recovery contracts."""

from __future__ import annotations

import copy
import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, select, update

from src.commands.job_commands import JobCommandsMixin
from src.commands.object_loop_commands import _charge, _reserve
from src.commands.contracts.job import JobRetainArgs
from src.commands.contracts.object_loop import ObjectLoopStartArgs, Reservation, Variant
from src.database.tables import doc_review_revisions, doc_reviews, object_loops, tasks
from src.integration.development import _publishable_object_task
from src.integration.child_delivery import _child_refusal
from src.integration.promotion import PromotionService, PromotionSourceMoved
from src.models import Project, Task, TaskStatus
from src.jobs.artifacts import atomic_json
from src.jobs.matter import candidate_document
from src.object_loop.artifacts import (
    ArtifactError, canonical_bytes, metadata, retain_bytes, retain_document, retain_file,
    verify,
)
from src.object_loop.contracts import Artifact, ScoreReceipt, validate_score_receipt
from src.object_loop.render_profile import (
    RenderProfileError, render_profile, render_profile_sha256, with_digest,
)

H = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64
E = "e" * 64
F = "f" * 64


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


async def failed_scorer_round(command_handler_factory, *, scorer_status="FAILED",
                              retry_count=3, candidate_status="BLOCKED"):
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p", name="Project"))
    await db.create_task(Task(id="epic", project_id="p", title="Epic", description="Epic",
                              status=TaskStatus.READY))
    await approved_brief(db)
    args = start_args()
    args["score_reservation"] = {"usd": 0.5, "calls": 1, "bakes": 0, "active_seconds": 2}
    args["incumbent_artifact"] = {"uri": "artifact://incumbent", "sha256": H}
    assert (await handler._cmd_object_loop_start(args))["success"]
    wave = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    candidate_id = wave["state"]["wave"][0]["task_id"]
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == candidate_id).values(
            status=candidate_status, retry_count=3, max_retries=3,
        ))
    fan_in = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    assert fan_in["success"] and fan_in["state"]["score_task_id"], fan_in
    score_id = fan_in["state"]["score_task_id"]
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == score_id).values(
            status=scorer_status, retry_count=retry_count, max_retries=3,
        ))
        loop = (await conn.execute(select(object_loops).where(
            object_loops.c.object_id == "rock",
        ))).mappings().one()
    request = {
        "project_id": "p", "object_id": "rock", "expected_version": fan_in["version"],
        "score_task_id": score_id, "receipts": [], "action": "stop",
        "stop_reason": "Scorer provider outage; retain incumbent and report defect",
        "spent": {"usd": 0, "calls": 0, "bakes": 0, "active_seconds": 0},
    }
    return handler, loop, request


@pytest.mark.parametrize("scorer_status", ["FAILED", "BLOCKED"])
@pytest.mark.parametrize("candidate_status", ["FAILED", "COMPLETED"])
async def test_exhausted_scorer_defect_stop_retains_incumbent_and_charges_reservation(
    command_handler_factory, scorer_status, candidate_status,
):
    handler, loop, request = await failed_scorer_round(
        command_handler_factory, scorer_status=scorer_status, candidate_status=candidate_status,
    )
    db = handler.db
    candidate_id = loop.state["wave"][0]["task_id"]
    assert (await db.get_task(loop.finalization_task_id)).is_blocked
    # Stale approval must not prevent a defect report or become an approval itself.
    async with db.immediate() as conn:
        await conn.execute(update(doc_reviews).where(doc_reviews.c.id == "brief-review")
                           .values(state="rejected", current_revision=2))
    stopped = await handler._cmd_object_score_record(request)
    assert stopped["success"] and stopped["outcome"] == "stop", stopped
    assert stopped["version"] == loop.version + 1
    state = stopped["state"]
    assert state["status"] == "stopped" and state["stop_reason"] == request["stop_reason"]
    for key in ("incumbent_sha256", "incumbent_artifact", "incumbent_loss", "round_id",
                "repair_count", "plateau_count", "checkpoint", "final_reserve", "wave"):
        assert state[key] == loop.state[key]
    assert state["spent"] == loop.state["reserved"]
    assert state["reserved"] == {"usd": 0, "calls": 0, "bakes": 0, "active_seconds": 0}
    assert state["intent"] is None
    assert state["defect_stop"]["status"] == scorer_status
    assert (await db.get_task(loop.finalization_task_id)).is_blocked
    assert (await handler._cmd_object_checkpoint_read({
        "project_id": "p", "object_id": "rock",
    }))["approved"] is False

    # Simulate daemon loss after committing the stop but before releasing the gate.
    original_resolve = db.resolve_gate
    db.resolve_gate = AsyncMock(side_effect=RuntimeError("restart before release"))
    with pytest.raises(RuntimeError, match="restart"):
        await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    assert (await db.get_gate(loop.terminal_gate_id))["status"] == "open"
    db.resolve_gate = original_resolve
    for _ in range(2):
        replay = await handler._cmd_object_score_record(request)
        assert replay["success"] and replay["outcome"] == "reused"
        assert replay["version"] == stopped["version"]
        released = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
        assert released["outcome"] == "stopped"
        assert released["version"] == stopped["version"]
        assert released["state"]["spent"] == state["spent"]
    assert (await db.get_gate(loop.terminal_gate_id))["status"] == "resolved"
    assert not (await db.get_task(loop.finalization_task_id)).is_blocked
    assert (await db.get_task(candidate_id)).status.value == candidate_status
    assert (await db.get_task(request["score_task_id"])).status.value == scorer_status
    async with db.immediate() as conn:
        children = (await conn.execute(select(tasks.c.id).where(
            tasks.c.created_by_kind == "object_loop", tasks.c.created_by_id == "rock",
        ))).scalars().all()
        review = (await conn.execute(select(doc_reviews).where(
            doc_reviews.c.id == "brief-review",
        ))).mappings().one()
    assert len(children) == 3
    assert review.state == "rejected" and review.current_revision == 2
    await db.close()


@pytest.mark.parametrize("scorer_status,retry_count", [
    ("FAILED", 2), ("IN_PROGRESS", 3), ("PAUSED", 3),
])
async def test_unsettled_scorer_cannot_stop(command_handler_factory, scorer_status, retry_count):
    handler, loop, request = await failed_scorer_round(
        command_handler_factory, scorer_status=scorer_status, retry_count=retry_count,
    )
    result = await handler._cmd_object_score_record(request)
    assert not result["success"] and "has not completed" in result["error"]
    held = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    assert held["outcome"] == "waiting_for_score"
    assert held["version"] == loop.version and held["state"] == loop.state
    assert (await handler.db.get_task(loop.finalization_task_id)).is_blocked
    await handler.db.close()


async def test_failed_scorer_stop_rejects_scores_continuations_and_stale_versions(
    command_handler_factory,
):
    handler, loop, request = await failed_scorer_round(
        command_handler_factory, candidate_status="COMPLETED",
    )
    candidate_id = loop.state["wave"][0]["task_id"]
    invalid = [
        {"action": "continue", "next_variants": [variant("extra").model_dump()]},
        {"action": "checkpoint", "review_id": "brief-review"},
        {"receipts": [receipt(task_id=candidate_id)]},
        {"next_variants": [variant("extra").model_dump()]},
        {"review_id": "brief-review", "review_revision": 1, "review_sha256": C},
        {"stop_reason": None}, {"stop_reason": "   "},
        {"score_task_id": candidate_id}, {"expected_version": loop.version - 1},
    ]
    for change in invalid:
        result = await handler._cmd_object_score_record(dict(request, **change))
        assert not result["success"], (change, result)
    held = await handler._cmd_object_checkpoint_read({"project_id": "p", "object_id": "rock"})
    assert held["version"] == loop.version and held["state"] == loop.state
    assert (await handler.db.get_task(loop.finalization_task_id)).is_blocked
    await handler.db.close()


@pytest.mark.parametrize("reopened", ["candidate", "scorer"])
async def test_defect_stop_waits_for_reopened_work_and_releases_only_terminal_gate(
    command_handler_factory, reopened,
):
    handler, loop, request = await failed_scorer_round(command_handler_factory)
    db = handler.db
    candidate_id = loop.state["wave"][0]["task_id"]
    task_id = candidate_id if reopened == "candidate" else request["score_task_id"]
    # A retrying candidate prevents recording the stop in the first place.
    if reopened == "candidate":
        async with db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == task_id)
                               .values(status="FAILED", retry_count=2))
        refused = await handler._cmd_object_score_record(request)
        assert not refused["success"] and "unsettled candidates" in refused["error"]
        async with db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == task_id)
                               .values(status="BLOCKED", retry_count=3))
    stopped = await handler._cmd_object_score_record(request)
    assert stopped["success"], stopped
    approval_gate, _ = await db.create_gate(
        "p", "review", "Separate final approval", await_id="final-approval",
        waiter_task_ids=[loop.finalization_task_id],
    )
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == task_id)
                           .values(status="FAILED", retry_count=2))
    held = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    assert held["outcome"] == "waiting_for_settlement"
    assert held["version"] == stopped["version"]
    assert (await db.get_gate(loop.terminal_gate_id))["status"] == "open"
    assert (await db.get_task(loop.finalization_task_id)).is_blocked
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == task_id)
                           .values(status="FAILED", retry_count=3))
    releases = []
    original_resolve = db.resolve_gate

    async def track_release(gate_id, **kwargs):
        prior = await db.get_gate(gate_id)
        flipped = await original_resolve(gate_id, **kwargs)
        if prior["status"] != "resolved":
            releases.append(gate_id)
        return flipped

    db.resolve_gate = AsyncMock(side_effect=track_release)
    for _ in range(2):
        reconciled = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
        assert reconciled["outcome"] == "stopped"
        assert reconciled["version"] == stopped["version"]
    assert (await db.get_gate(loop.terminal_gate_id))["status"] == "resolved"
    assert releases == [loop.terminal_gate_id]
    assert (await db.get_gate(approval_gate))["status"] == "open"
    assert (await db.get_task(loop.finalization_task_id)).is_blocked
    assert (await db.get_task(task_id)).status.value == "FAILED"
    await db.close()


# ---------------------------------------------------------------------------
# Durable artifact identities and the render profile (brisk-beacon-64)
# ---------------------------------------------------------------------------

def candidate_bundle(directory, *, views=("front",)):
    """A candidate manifest in the form Matter declares its identities in.

    ``candidate_sha256`` is the digest of the canonical document with the
    declaration removed, so retaining those bytes mints the candidate artifact a
    ``ScoreReceipt`` must name.
    """
    rig = {
        "hold_frames": 90,
        "views": [{"name": name, "pose": [2.5, 2.0, 4.0, 0, 0.35, 0]} for name in views],
    }
    document = {
        "manifest": {"rig": rig},
        "rig_sha256": hashlib.sha256(canonical_bytes(rig)).hexdigest(),
        "source_sha256": {"Rock.js": D},
        "source_closure_sha256": E,
        "version": 1,
    }
    document["manifest_sha256"] = hashlib.sha256(
        canonical_bytes(document["manifest"])
    ).hexdigest()
    document["candidate_sha256"] = hashlib.sha256(canonical_bytes(document)).hexdigest()
    atomic_json(directory / "candidate.json", document)
    return document


def rendered_capture(directory, document, *, views=("front",), editor=F):
    """A capture directory and the ``retain_capture`` receipt it produces."""
    capture = directory / "capture"
    capture.mkdir(parents=True)
    expected = {
        "candidate_sha256": document["candidate_sha256"],
        "rig_sha256": document["rig_sha256"],
        "editor_sha256": editor,
    }
    members, receipt_views = [], {}
    for name in views:
        view = {}
        for key, suffix in (("image", ".png"), ("channels", ".png.channels.bin")):
            data = f"{name}:{key}".encode()
            (capture / f"{name}{suffix}").write_bytes(data)
            view[key] = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "width": 1280 if key == "image" else None,
                "height": 720 if key == "image" else None,
            }
        receipt_views[name] = view
        view["capture"] = {"result": {
            "presented": {"id": f"{name}-frame"},
            "image": {"format": "png", "width": 1280, "height": 720},
            "readiness": {
                "available": True, "ready": True, "blockers": [], "missing_blas": 0,
                "missing_draws": 0, "missing_vt": 0, "unmatched_tokens": 0,
                "visible_refinement_pending": 0, "visible_sectors_pending": 0,
                "vt_dirty_pages": 0, "vt_queue_depth": 0, "vt_rejected_variants": 0,
                # Per-run jitter: the camera settles at a different frame count
                # every time, which is why the profile hashes the admitted rig
                # budget instead of this observed value.
                "stable_camera_frames": 95,
            },
        }}
        view["image"] = {"sha256": view["image"]["sha256"], "width": 1280, "height": 720,
                         "path": str(capture / f"{name}.png")}
    receipt = dict(expected, version=1, status="complete", views=receipt_views,
                   readiness="captured_visible_detail_ready",
                   run_id="f2a65d1c-503c-47e5-b17c-8ec80a210254",
                   timing_seconds={"render": 1.25})
    (capture / "capture.json").write_text(json.dumps(receipt))
    for path in sorted(capture.rglob("*")):
        if path.is_file():
            members.append({
                "path": str(path.relative_to(directory)),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    return expected, {"receipt": receipt, "artifacts": members,
                      "bytes": sum(m["bytes"] for m in members)}


def render_job(directory, bundle, expected, capture):
    """The job row shape the retention step reads: a contract and a result.

    The candidate bundle stays in the author's workspace, which is exactly why a
    released workspace is the failure mode retention has to report.
    """
    contract = {
        "adapter_sha256": E,
        "gpu_id": "gpu0",
        "capture_expected": {
            **expected, "views": sorted(capture["receipt"]["views"]),
            "hold_frames": 90,
        },
    }
    profile = with_digest(render_profile(contract, capture))
    return {
        "id": str(directory.name),
        "preset": "matter_render",
        "argv": ["/usr/bin/python3", "matter_capture.py", "adapter.py", str(directory.parent),
                 "/opt/editor/editor.exe", str(bundle)],
        "contract": contract,
        "result": {"capture": capture, "render_profile": profile},
    }


class _Retention(JobCommandsMixin):
    """The retention step reads a job row and a configured data directory."""

    def __init__(self, data_dir):
        self.config = SimpleNamespace(data_dir=str(data_dir))


def retention(tmp_path, views=("front",)):
    """Drive the real retention step over a real candidate bundle and capture."""
    data_dir = tmp_path / "data"
    # The production layout: a bundle in the author's workspace and a capture
    # under the run directory the sweeper will eventually reclaim.
    directory = data_dir / "runs" / str(uuid.uuid4())
    bundle = tmp_path / str(uuid.uuid4())
    bundle.mkdir(parents=True)
    directory.mkdir(parents=True)
    document = candidate_bundle(bundle, views=views)
    expected, capture = rendered_capture(directory, document, views=views)
    job = render_job(directory, bundle, expected, capture)
    handler = _Retention(data_dir)
    retained = JobCommandsMixin._retain_capture(
        handler, job, capture, JobRetainArgs(job_id=job["id"]))
    return SimpleNamespace(job=job, document=document, capture=capture, retained=retained,
                           directory=directory, data_dir=data_dir)


def test_durable_artifact_identities_mint_resolve_and_verify(tmp_path):
    data_dir = tmp_path / "data"
    source = tmp_path / "capture.png"
    source.write_bytes(b"png bytes")
    minted = retain_file(source, data_dir=data_dir, kind="capture_image",
                         origin={"member": "capture/front.png"})
    assert minted["uri"] == f"artifact://sha256/{minted['sha256']}"
    assert Artifact(uri=minted["uri"], sha256=minted["sha256"]).uri == minted["uri"]
    # Retention is idempotent: the digest is the identity, so a second pass
    # yields the same URI and never rewrites the object.
    again = retain_file(source, data_dir=data_dir, kind="capture_image")
    assert again["uri"] == minted["uri"] and again["path"] == minted["path"]
    verified = verify(data_dir, minted["uri"], minted["sha256"])
    assert verified["verified"] and verified["bytes"] == minted["bytes"]
    assert verified["kind"] == "capture_image"
    assert Path(verified["path"]).read_bytes() == b"png bytes"

    # Bytes that no longer hash to the identity they are served under are never
    # certified, on a fresh retention or on a check.
    Path(minted["path"]).write_bytes(b"tampered")
    with pytest.raises(ArtifactError, match="no longer hashes"):
        retain_file(source, data_dir=data_dir, kind="capture_image")
    with pytest.raises(ArtifactError, match="does not hash"):
        verify(data_dir, minted["uri"])

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside)
    with pytest.raises(ArtifactError, match="regular file"):
        retain_file(tmp_path / "link", data_dir=data_dir, kind="capture_image")
    with pytest.raises(ArtifactError, match="regular file"):
        retain_file(outside, data_dir=data_dir, kind="capture_image")


def test_artifact_uris_resolve_only_to_this_store(tmp_path):
    data_dir = tmp_path / "data"
    minted = retain_bytes(b"evidence", data_dir=data_dir, kind="capture_receipt")
    assert verify(data_dir, minted["uri"])["sha256"] == minted["sha256"]
    for uri in (
        f"s3://bucket/{'0' * 64}",
        f"https://example.invalid/{'0' * 64}",
        f"artifact://sha256/{'A' * 64}",
        f"artifact://sha256/{'0' * 63}",
        f"artifact://sha256/{'0' * 64}/extra",
        "artifact://sha256/../../etc/passwd",
        "artifact://sha256",
        f"artifact://other/{'0' * 64}",
    ):
        with pytest.raises(ArtifactError):
            verify(data_dir, uri)
    with pytest.raises(ArtifactError, match="does not match the URI"):
        verify(data_dir, minted["uri"], "0" * 64)
    with pytest.raises(ArtifactError, match="not retained"):
        verify(data_dir, f"artifact://sha256/{'0' * 64}")
    assert metadata(data_dir, minted["uri"])["bytes"] == len(b"evidence")


def test_a_retained_document_is_named_by_the_identity_it_declares(tmp_path):
    data_dir = tmp_path / "data"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    document = candidate_bundle(bundle)
    parsed, declared = candidate_document(bundle / "candidate.json")
    assert parsed == {k: v for k, v in document.items() if k != "candidate_sha256"}
    minted = retain_document(parsed, data_dir=data_dir, kind="candidate",
                             expect_sha256=declared)
    # The minted artifact's digest *is* the candidate identity the receipt quotes.
    assert minted["sha256"] == document["candidate_sha256"] == declared
    with pytest.raises(ArtifactError, match="does not hash to the identity"):
        retain_document(parsed, data_dir=data_dir, kind="candidate", expect_sha256=D)
    with pytest.raises(ValueError, match="not a readable file"):
        candidate_document(bundle / "absent.json")


def test_render_profile_is_stable_across_runs_and_sensitive_to_its_preset(tmp_path):
    """One preset, one digest: run jitter must not mint a new profile identity."""
    first = retention(tmp_path / "a")
    contract, capture = first.job["contract"], first.job["result"]["capture"]
    baseline = first.job["result"]["render_profile"]["sha256"]
    assert contract["capture_expected"]["hold_frames"] == 90
    assert baseline == render_profile_sha256(contract, capture)

    rerun = copy.deepcopy(capture)
    for view in rerun["receipt"]["views"].values():
        readiness = view["capture"]["result"]["readiness"]
        readiness["stable_camera_frames"] = 7
        view["capture"]["result"]["presented"]["id"] = "another-frame"
        view["image"]["path"] = "C:\\elsewhere\\capture\\front.png"
    rerun["receipt"]["run_id"] = "00000000-0000-0000-0000-000000000000"
    rerun["receipt"]["timing_seconds"] = {"render": 99.5}
    assert render_profile_sha256(contract, rerun) == baseline

    for changed in (
        {"editor_sha256": "1" * 64},
        {"rig_sha256": "1" * 64},
        {"hold_frames": 91},
    ):
        moved = copy.deepcopy(contract)
        moved["capture_expected"].update(changed)
        assert render_profile_sha256(moved, capture) != baseline
    for changed in ({"adapter_sha256": "1" * 64}, {"gpu_id": "gpu1"}):
        moved = copy.deepcopy(contract)
        moved.update(changed)
        assert render_profile_sha256(moved, capture) != baseline
    resized = copy.deepcopy(capture)
    resized["receipt"]["views"]["front"]["image"].update(width=640)
    assert render_profile_sha256(contract, resized) != baseline

    # A render that never converged is not the same render.
    unconverged = copy.deepcopy(capture)
    unconverged["receipt"]["views"]["front"]["capture"]["result"]["readiness"][
        "missing_vt"] = 4
    assert render_profile_sha256(contract, unconverged) != baseline

    foreign = copy.deepcopy(capture)
    foreign["receipt"]["views"]["side"] = foreign["receipt"]["views"]["front"]
    del foreign["receipt"]["views"]["front"]
    with pytest.raises(RenderProfileError, match="different view set"):
        render_profile_sha256(contract, foreign)
    with pytest.raises(RenderProfileError, match="admitted"):
        render_profile_sha256({}, capture)
    with pytest.raises(RenderProfileError, match="completed capture receipt"):
        render_profile_sha256(contract, None)


def test_a_retained_capture_yields_a_valid_start_packet_and_receipt(tmp_path):
    """The acceptance path: a render job becomes start args and a ScoreReceipt."""
    ready = retention(tmp_path, views=("front", "side"))
    retained = ready.retained
    profile = retained["render_profile_sha256"]
    assert retained["candidate_artifact"]["sha256"] == ready.document["candidate_sha256"]
    assert {capture["view_id"] for capture in retained["captures"]} == {"front", "side"}
    # ``artifacts`` is exactly what becomes ScoreReceipt.artifacts.
    assert sorted({artifact["kind"] for artifact in retained["artifacts"]}) == [
        "candidate", "capture_image", "capture_receipt",
    ]
    assert len(retained["artifacts"]) == 1 + len(retained["captures"]) + 1

    # Every reported URI resolves and re-hashes, before it is quoted anywhere.
    for artifact in retained["artifacts"]:
        assert verify(ready.data_dir, artifact["uri"], artifact["sha256"])["verified"]
    for capture in retained["captures"]:
        assert verify(ready.data_dir, capture["image"]["uri"],
                      capture["image"]["sha256"])["verified"]

    start = ObjectLoopStartArgs.model_validate({
        **start_args(),
        "incumbent_artifact": {
            "uri": retained["candidate_artifact"]["uri"],
            "sha256": retained["candidate_artifact"]["sha256"],
        },
        "render_profile_sha256": profile,
        "rig_sha256": ready.job["contract"]["capture_expected"]["rig_sha256"],
        "mandatory_views": ["front", "side"],
        "limits": {"usd": 30, "calls": 3, "bakes": 3, "active_seconds": 300},
    })
    assert start.render_profile_sha256 == profile
    assert start.incumbent_artifact.uri.startswith("artifact://sha256/")

    scored = receipt(
        base_candidate_sha256=ready.document["candidate_sha256"],
        candidate_sha256=ready.document["candidate_sha256"],
        rig_sha256=ready.job["contract"]["capture_expected"]["rig_sha256"],
        render_profile_sha256=profile,
        capture_receipts=[
            {**capture, "ready": True, "decoded": True} for capture in retained["captures"]
        ],
        artifacts=[{"uri": artifact["uri"], "sha256": artifact["sha256"]}
                   for artifact in retained["artifacts"]],
        per_view={"front": {"loss": 0.2, "quality_pass": True},
                  "side": {"loss": 0.1, "quality_pass": True}},
        worst_view="front",
    )
    state = loop_state()
    state.update({
        "incumbent_sha256": ready.document["candidate_sha256"],
        "render_profile_sha256": profile,
        "rig_sha256": ready.job["contract"]["capture_expected"]["rig_sha256"],
        "mandatory_views": ["front", "side"],
        "wave": [{"variant_id": "a", "task_id": "epic.2"}],
    })
    validate_score_receipt(ScoreReceipt.model_validate(scored), state=state,
                           task_id="epic.2", mandatory_views={"front", "side"})
    # The same receipt with a local path instead of a durable URI is refused.
    with pytest.raises(ValidationError, match="durable artifact"):
        ScoreReceipt.model_validate(receipt(artifacts=[{"uri": "/tmp/front.png",
                                                         "sha256": B}]))


def test_retention_refuses_capture_bytes_that_moved(tmp_path):
    ready = retention(tmp_path)
    (ready.directory / "capture/front.png").write_bytes(b"edited after the job")
    with pytest.raises(ArtifactError, match="changed since the job retained it"):
        _Retention(ready.data_dir)._retain_capture(
            ready.job, ready.capture, JobRetainArgs(job_id=ready.job["id"]),
        )
    with pytest.raises(ValueError, match="unknown capture views"):
        _Retention(ready.data_dir)._retain_capture(
            ready.job, ready.capture, JobRetainArgs(job_id=ready.job["id"], views=["nope"]),
        )


async def test_retain_is_scoped_and_refuses_work_it_cannot_evidence(command_handler_factory):
    handler = await command_handler_factory()
    db = handler.db
    try:
        assert (await handler._cmd_job_retain({"job_id": str(uuid.uuid4())}))["error"] == "not_found"
        assert (await handler._cmd_artifact_verify({"uri": "artifact://sha256/" + "0" * 64}))[
            "success"] is False
        handler._current_scope = {"kind": "session", "session_id": "s"}
        refused = await handler._cmd_object_loop_inputs({"project_id": "p"})
        assert "project supervisor" in refused["error"] and "aq object_loop inputs" in refused["error"]
    finally:
        await db.close()


async def test_artifact_verify_rehashes_through_the_command(command_handler_factory, tmp_path):
    handler = await command_handler_factory()
    handler.config.data_dir = str(tmp_path / "store")
    minted = retain_bytes(b"receipt evidence", data_dir=handler.config.data_dir, kind="candidate")
    try:
        verified = await handler._cmd_artifact_verify({"uri": minted["uri"]})
        assert verified["success"] and verified["verified"] and verified["bytes"] == 16
        mismatch = await handler._cmd_artifact_verify({"uri": minted["uri"], "sha256": D})
        assert mismatch["success"] is False and "does not match the URI" in mismatch["error"]
        absent = await handler._cmd_artifact_verify({"uri": f"artifact://sha256/{'0' * 64}"})
        assert absent["success"] is False and "not retained" in absent["error"]
    finally:
        await handler.db.close()
