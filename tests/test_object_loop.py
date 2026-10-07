"""AQ-2 object-loop identity, budget and crash recovery contracts."""

from __future__ import annotations

import copy
import asyncio
import hashlib
import json
import time
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
from src.integration.promotion import PromotionService
from src.integration.promotion_contracts import PromotionSourceMoved
from src.models import Project, Task, TaskCompletion, TaskStatus
from src.jobs.artifacts import atomic_json
from src.jobs.matter import candidate_document
from src.object_loop.artifacts import (
    ArtifactError, canonical_bytes, metadata, retain_bytes, retain_document, retain_file,
    verify,
)
from src.object_loop.contracts import Artifact, ScoreReceipt, validate_score_receipt
from src.object_loop.render_profile import (
    RENDER_PROFILE_VERSION, RenderProfileError, capture_observations, render_profile,
    render_profile_sha256, with_digest,
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
        "incumbent_capture_sha256": H,
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


def test_the_round_cap_is_bounded_start_input_and_a_legacy_row_keeps_its_ceiling():
    """`max_rounds` is data, not a constant; the ceiling still bounds it."""
    assert ObjectLoopStartArgs.model_validate(start_args()).max_rounds == 8
    for ceiling in (0, 9):
        with pytest.raises(ValidationError):
            ObjectLoopStartArgs.model_validate({**start_args(), "max_rounds": ceiling})
    legacy = {
        **loop_state(),
        "limits": {"usd": 80, "calls": 80, "bakes": 80, "active_seconds": 800},
    }
    assert "max_rounds" not in legacy
    for _ in range(8):
        _charge(legacy, None)
        _reserve(legacy, [variant("a")])
        legacy["round_id"] += 1
    _charge(legacy, None)
    with pytest.raises(ValueError, match="round cap"):
        _reserve(legacy, [variant("a")])


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


@pytest.mark.parametrize("reference_kind", ["calibrated", "self"])
async def test_bootstrap_hold_and_repeated_reconcile_make_one_candidate(
    command_handler_factory, reference_kind,
):
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p", name="Project"))
    await db.create_task(Task(id="epic", project_id="p", title="Epic", description="Epic",
                              status=TaskStatus.READY))
    brief_gate = await approved_brief(db)
    request = {**start_args(), "reference_kind": reference_kind,
               "incumbent_capture_sha256": B, "reference_sha256": B}
    started = await handler._cmd_object_loop_start(request)
    assert started["success"], started
    assert started["state"]["reference_kind"] == reference_kind
    assert started["state"]["incumbent_capture_sha256"] == B
    again = await handler._cmd_object_loop_start(request)
    assert again["success"] and not again["created"]
    switched = await handler._cmd_object_loop_start({
        **request, "reference_kind": "self" if reference_kind == "calibrated" else "calibrated",
    })
    assert not switched["success"] and "different fixed inputs" in switched["error"]
    changed_capture = await handler._cmd_object_loop_start({
        **request, "incumbent_capture_sha256": C,
        "reference_sha256": C if reference_kind == "self" else B,
    })
    assert not changed_capture["success"] and "different fixed inputs" in changed_capture["error"]
    async with db.immediate() as conn:
        loop = (await conn.execute(select(object_loops).where(
            object_loops.c.object_id == "rock"))).mappings().one()
    assert (await db.get_task(loop.finalization_task_id)).is_blocked
    first = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    assert first["success"], first
    candidate_id = first["state"]["wave"][0]["task_id"]
    candidate = await db.get_task(candidate_id)
    description = json.loads(candidate.description)
    assert description["reference_kind"] == reference_kind
    assert description["incumbent_capture_sha256"] == B
    assert description["publication"] == "artifact_only"
    finalizer = await db.get_task(loop.finalization_task_id)
    assert f"reference_kind={reference_kind}" in finalizer.description
    if reference_kind == "self":
        assert description["result_interpretation"] == "indicative; plumbing only"
        assert "indicative, for plumbing only" in finalizer.description
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


async def test_legacy_loop_refuses_replay_without_baseline_capture_identity(command_handler_factory):
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p", name="Project"))
    await db.create_task(Task(id="epic", project_id="p", title="Epic", description="Epic",
                              status=TaskStatus.READY))
    await approved_brief(db)
    started = await handler._cmd_object_loop_start(start_args())
    assert started["success"], started
    legacy_state = copy.deepcopy(started["state"])
    del legacy_state["reference_kind"]
    del legacy_state["incumbent_capture_sha256"]
    async with db.immediate() as conn:
        await conn.execute(update(object_loops).where(object_loops.c.object_id == "rock")
                           .values(state=legacy_state))
    replay = await handler._cmd_object_loop_start(start_args())
    assert not replay["success"] and "different fixed inputs" in replay["error"]
    switched = await handler._cmd_object_loop_start({**start_args(), "reference_kind": "self"})
    assert not switched["success"] and "different fixed inputs" in switched["error"]
    reconciled = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    assert reconciled["success"], reconciled
    candidate = await db.get_task(reconciled["state"]["wave"][0]["task_id"])
    assert json.loads(candidate.description)["reference_kind"] == "calibrated"
    await db.close()


@pytest.mark.parametrize("reference_kind", ["calibrated", "self"])
def test_start_packet_binds_self_reference_to_capture_not_candidate(reference_kind):
    request = {
        **start_args(), "reference_kind": reference_kind,
        "incumbent_capture_sha256": B,
        "reference_sha256": B if reference_kind == "self" else C,
    }
    parsed = ObjectLoopStartArgs.model_validate(request)
    assert parsed.incumbent_sha256 == H
    assert parsed.incumbent_capture_sha256 == B
    assert parsed.reference_sha256 == (B if reference_kind == "self" else C)
    with pytest.raises(ValidationError, match="incumbent_capture_sha256"):
        ObjectLoopStartArgs.model_validate({k: v for k, v in request.items()
                                           if k != "incumbent_capture_sha256"})
    assert ObjectLoopStartArgs.model_validate(start_args()).reference_kind == "calibrated"


async def test_self_reference_mismatch_creates_no_loop_or_finalizer(command_handler_factory):
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p", name="Project"))
    await db.create_task(Task(id="epic", project_id="p", title="Epic", description="Epic",
                              status=TaskStatus.READY))
    await approved_brief(db)
    for reference in (H, C):
        rejected = await handler._cmd_object_loop_start({
            **start_args(), "reference_kind": "self",
            "incumbent_capture_sha256": B, "reference_sha256": reference,
        })
        assert not rejected["success"] and "must match incumbent_capture_sha256" in rejected["error"]
    async with db.immediate() as conn:
        assert not (await conn.execute(select(object_loops))).first()
        assert not (await conn.execute(select(tasks.c.id).where(
            tasks.c.created_by_kind == "object_loop",
        ))).first()
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


async def test_round_two_is_handed_round_ones_changes_and_the_frozen_inputs(
    command_handler_factory,
):
    """The pilot's round-2 worker closed blocked twice with zero captures.

    It had a hypothesis, a base hash and a rig, and nothing about what round 1
    actually changed: no branch, no commit, no retained bundle, no frozen
    comparison inputs. So every round's packet now carries them, read from the
    round's own close rather than from a worktree the slot has already released.
    """
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p", name="Project"))
    await db.create_task(Task(id="epic", project_id="p", title="Epic", description="Epic",
                              status=TaskStatus.READY))
    await approved_brief(db)
    artifact = {"uri": f"artifact://sha256/{H}", "sha256": H}
    started = await handler._cmd_object_loop_start({
        **start_args(), "incumbent_artifact": artifact,
    })
    assert started["success"], started
    first = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    round_one = first["state"]["wave"][0]["task_id"]
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == round_one).values(status="COMPLETED"))
    await db.save_task_completion(TaskCompletion(
        id="close-round-one", task_id=round_one, outcome="pass", completed_at=time.time(),
        branch="aq/rock-me3-a1.1", commits=["555d20990" + "0" * 31], summary="rounder silhouette",
    ))
    fan_in = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    score_id = fan_in["state"]["score_task_id"]
    scorer_packet = json.loads((await db.get_task(score_id)).description)
    assert scorer_packet["frozen_inputs"] == {
        "reference_kind": "calibrated", "reference_sha256": H, "rig_sha256": H,
        "scorer_sha256": H, "render_profile_sha256": H, "policy_sha256": H,
        "mandatory_views": ["front"],
    }
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == score_id).values(status="COMPLETED"))
    scored = await handler._cmd_object_score_record({
        "project_id": "p", "object_id": "rock", "expected_version": fan_in["version"],
        "score_task_id": score_id, "receipts": [receipt(task_id=round_one)],
        "action": "continue", "next_variants": [variant("next").model_dump()],
    })
    assert scored["success"], scored
    # The candidate that beat the incumbent is the incumbent now, and round 2 is
    # told so by name, with round 1's bundle, branch and commit.
    assert scored["state"]["incumbent_sha256"] == B
    assert scored["state"]["incumbent_artifact"] == {"uri": "artifact://candidate", "sha256": B}
    second = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    round_two = second["state"]["wave"][0]["task_id"]
    packet = json.loads((await db.get_task(round_two)).description)
    assert "no implicit human-review deliverable" in packet["review_guidance"]
    assert "project supervisor" in packet["review_guidance"]
    handoff = packet["round_handoff"]
    assert handoff["attempt_id"] == "attempt-1" and handoff["round_id"] == 1
    assert handoff["incumbent"] == {
        "sha256": B, "artifact": {"uri": "artifact://candidate", "sha256": B},
        "capture_sha256": H,
    }
    (entry,) = handoff["prior_rounds"]
    assert entry["round_id"] == 0 and entry["variant_id"] == "a"
    assert entry["branch"] == "aq/rock-me3-a1.1"
    assert entry["commit"] == "555d20990" + "0" * 31
    assert entry["candidate_artifact"] == {"uri": "artifact://candidate", "sha256": B}
    assert entry["patch_scope"] == ["Rock.js"] and entry["promoted"] is True
    assert entry["mean_loss"] == 0.1 and entry["validity"] == "valid"
    assert handoff["frozen_inputs"] == scorer_packet["frozen_inputs"]
    assert "--attempt-id attempt-1" in handoff["instructions"]
    assert "earlier round branches is in scope" in handoff["instructions"]
    await db.close()


async def test_a_round_without_a_close_record_hands_over_its_bundle_only(
    command_handler_factory,
):
    """A round that settled without a close record names no branch, not a guess."""
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
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(
            tasks.c.id == fan_in["state"]["score_task_id"]).values(status="COMPLETED"))
    scored = await handler._cmd_object_score_record({
        "project_id": "p", "object_id": "rock", "expected_version": fan_in["version"],
        "score_task_id": fan_in["state"]["score_task_id"],
        "receipts": [receipt(task_id=candidate_id)], "action": "continue",
        "next_variants": [variant("next").model_dump()],
    })
    assert scored["success"], scored
    second = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    (entry,) = json.loads((await db.get_task(
        second["state"]["wave"][0]["task_id"])).description)["round_handoff"]["prior_rounds"]
    assert entry["branch"] is None and entry["commit"] is None
    assert entry["candidate_artifact"] == {"uri": "artifact://candidate", "sha256": B}
    await db.close()


@pytest.mark.parametrize("reference_kind", ["calibrated", "self"])
async def test_three_rounds_of_two_variants_costs_rounds_not_calls(
    command_handler_factory, reference_kind,
):
    """What a 3-round hard cap could not say before: rounds are not a call budget.

    `calls=3` bought one variant per wave for three rounds.  `max_rounds=3`
    keeps the cap at three rounds while every wave still reserves two
    candidates against the aggregate budget.
    """
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p", name="Project"))
    await db.create_task(Task(id="epic", project_id="p", title="Epic", description="Epic",
                              status=TaskStatus.READY))
    await approved_brief(db)
    pair = [variant("a").model_dump(), variant("b").model_dump()]
    request = {
        **start_args(), "reference_kind": reference_kind, "max_rounds": 3, "variants": pair,
        "limits": {"usd": 20, "calls": 20, "bakes": 20, "active_seconds": 200},
    }
    started = await handler._cmd_object_loop_start(request)
    assert started["success"], started
    assert started["state"]["max_rounds"] == 3
    assert started["state"]["reference_kind"] == reference_kind
    assert started["state"]["incumbent_capture_sha256"] == H
    # The cap is fixed input, so a replay must not restate it as the default.
    widened = await handler._cmd_object_loop_start({**request, "max_rounds": 8})
    assert not widened["success"] and "different fixed inputs" in widened["error"]

    async def complete(ids):
        async with db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id.in_(ids)).values(status="COMPLETED"))

    def packet(fan_in, *, loss, action, **changes):
        return {
            "project_id": "p", "object_id": "rock", "expected_version": fan_in["version"],
            "score_task_id": fan_in["state"]["score_task_id"],
            "receipts": [
                receipt(task_id=member["task_id"], round_id=fan_in["state"]["round_id"],
                        variant_id=member["variant_id"],
                        base_candidate_sha256=fan_in["state"]["incumbent_sha256"],
                        per_view={"front": {"loss": loss, "quality_pass": True}})
                for member in fan_in["state"]["wave"]
            ],
            "action": action, "next_variants": pair, **changes,
        }

    for round_id, loss in enumerate((0.3, 0.2)):
        wave = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
        assert [m["variant_id"] for m in wave["state"]["wave"]] == ["a", "b"]
        assert wave["state"]["reserved"]["calls"] == 2
        await complete([m["task_id"] for m in wave["state"]["wave"]])
        fan_in = await handler._cmd_object_loop_reconcile(
            {"project_id": "p", "object_id": "rock"}
        )
        assert fan_in["state"]["round_id"] == round_id
        await complete([fan_in["state"]["score_task_id"]])
        scored = await handler._cmd_object_score_record(
            packet(fan_in, loss=loss, action="continue")
        )
        assert scored["success"] and scored["state"]["round_id"] == round_id + 1, scored
    third = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    assert [m["variant_id"] for m in third["state"]["wave"]] == ["a", "b"]
    await complete([m["task_id"] for m in third["state"]["wave"]])
    fan_in = await handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    assert fan_in["state"]["round_id"] == 2
    await complete([fan_in["state"]["score_task_id"]])
    capped = await handler._cmd_object_score_record(packet(fan_in, loss=0.1, action="continue"))
    assert not capped["success"] and capped["error"] == "round cap reached", capped
    # Six candidate calls were admitted across three rounds; the cap is not the
    # budget, and the refused continuation charged nothing.
    held = await handler._cmd_object_checkpoint_read({"project_id": "p", "object_id": "rock"})
    assert held["state"]["round_id"] == 2 and held["state"]["status"] == "active"
    assert held["state"]["spent"]["calls"] == 4
    assert held["state"]["reserved"]["calls"] == 2
    # A refused continuation is a cap, not a dead loop: the loop still stops.
    stopped = await handler._cmd_object_score_record(
        packet(fan_in, loss=0.1, action="stop", next_variants=[],
               stop_reason="three-round cap reached; retained verified incumbent")
    )
    assert stopped["success"] and stopped["outcome"] == "stop", stopped
    assert stopped["state"]["stop_reason"] == "three-round cap reached; retained verified incumbent"
    assert stopped["state"]["incumbent_sha256"] == B
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
            state="approved", gate_id=gate_id, decider="supervisor", decided_by="supervisor",
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
            state="approved", gate_id=gate_id, decider="supervisor", decided_by="supervisor",
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

def candidate_bundle(directory, *, views=("front",), generator=None):
    """A candidate manifest in the form Matter declares its identities in.

    ``candidate_sha256`` is the digest of the canonical document with the
    declaration removed, so retaining those bytes mints the candidate artifact a
    ``ScoreReceipt`` must name. *generator* varies the object under test while
    leaving the rig alone, which is what a round's candidate does: a different
    candidate_sha256 under one unchanged render preset.
    """
    rig = {
        "hold_frames": 90,
        "views": [{"name": name, "pose": [2.5, 2.0, 4.0, 0, 0.35, 0]} for name in views],
    }
    document = {
        "manifest": {"rig": rig, **({"generator": {"seed_source": generator}}
                                    if generator else {})},
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


def rendered_capture(directory, document, *, views=("front",), editor=F, image_bytes=None):
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
            if key == "image" and image_bytes is not None:
                data = image_bytes
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
    profile = with_digest(render_profile(contract, capture),
                          capture_observations(contract, capture))
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


def retention(tmp_path, views=("front",), *, image_bytes=None):
    """Drive the real retention step over a real candidate bundle and capture."""
    data_dir = tmp_path / "data"
    # The production layout: a bundle in the author's workspace and a capture
    # under the run directory the sweeper will eventually reclaim.
    directory = data_dir / "runs" / str(uuid.uuid4())
    bundle = tmp_path / str(uuid.uuid4())
    bundle.mkdir(parents=True)
    directory.mkdir(parents=True)
    document = candidate_bundle(bundle, views=views)
    expected, capture = rendered_capture(directory, document, views=views, image_bytes=image_bytes)
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

    # What the object under test did is not part of the preset. A different
    # candidate, and a heavier candidate whose render queued more VT work, both
    # render under the profile the attempt pinned — which is the whole point:
    # a candidate that is not the incumbent has to be scoreable at all.
    other_object = copy.deepcopy(capture)
    other_object["receipt"]["candidate_sha256"] = D
    assert render_profile_sha256(contract, other_object) == baseline
    unconverged = copy.deepcopy(capture)
    unconverged["receipt"]["views"]["front"]["capture"]["result"]["readiness"][
        "missing_vt"] = 4
    assert render_profile_sha256(contract, unconverged) == baseline
    assert capture_observations(contract, capture)["views"]["front"]["vt_budget"]["ready"] is True
    assert capture_observations(contract, unconverged)["views"]["front"]["vt_budget"][
        "missing_vt"] == 4
    # ...and neither the candidate identity nor a readiness counter is in the
    # hashed document, so the recorded digest covers the settings alone.
    document = render_profile(contract, capture)
    assert "candidate_sha256" not in json.dumps(document)
    assert "vt_budget" not in json.dumps(document)
    assert document["version"] == RENDER_PROFILE_VERSION == 2
    assert document["rig_sha256"] == contract["capture_expected"]["rig_sha256"]
    assert first.job["result"]["render_profile"]["observed"]["readiness"] == (
        capture["receipt"]["readiness"])

    foreign = copy.deepcopy(capture)
    foreign["receipt"]["views"]["side"] = foreign["receipt"]["views"]["front"]
    del foreign["receipt"]["views"]["front"]
    with pytest.raises(RenderProfileError, match="different view set"):
        render_profile_sha256(contract, foreign)
    with pytest.raises(RenderProfileError, match="admitted"):
        render_profile_sha256({}, capture)
    with pytest.raises(RenderProfileError, match="completed capture receipt"):
        render_profile_sha256(contract, {"receipt": {}})


def test_a_changed_candidate_renders_under_the_incumbents_profile(tmp_path):
    """The acceptance the pilot could not reach: the next candidate is comparable.

    Round 1's candidate is a different bundle from the incumbent. Same editor,
    same rig, same views — one preset, one profile identity — so the receipt it
    scores with is the profile the start packet pinned, not a profile no packet
    ever pinned. Before the fix this receipt was refused as foreign and no
    candidate could ever beat the incumbent.
    """
    incumbent = retention(tmp_path / "incumbent")
    contract = incumbent.job["contract"]
    profile = incumbent.job["result"]["render_profile"]["sha256"]
    bundle = tmp_path / "candidate"
    bundle.mkdir()
    candidate = candidate_bundle(bundle, generator="round-1")
    assert candidate["candidate_sha256"] != incumbent.document["candidate_sha256"]
    _, capture = rendered_capture(
        tmp_path / "candidate-run", candidate,
        editor=contract["capture_expected"]["editor_sha256"],
    )
    assert capture["receipt"]["candidate_sha256"] == candidate["candidate_sha256"]
    assert render_profile_sha256(contract, capture) == profile

    state = loop_state()
    state.update({
        "incumbent_sha256": incumbent.document["candidate_sha256"],
        "render_profile_sha256": profile,
        "wave": [{"variant_id": "a", "task_id": "epic.2"}],
    })
    score = ScoreReceipt.model_validate(receipt(
        base_candidate_sha256=incumbent.document["candidate_sha256"],
        candidate_sha256=candidate["candidate_sha256"],
        render_profile_sha256=profile,
        artifacts=[{"uri": f"artifact://sha256/{candidate['candidate_sha256']}",
                    "sha256": candidate["candidate_sha256"]},
                   {"uri": "artifact://frame-1", "sha256": B}],
    ))
    validate_score_receipt(score, state=state, task_id="epic.2", mandatory_views={"front"})
    # ...while a receipt carrying any other profile is still refused: the
    # comparison means something only because the preset is fixed.
    with pytest.raises(ValueError, match="render_profile_sha256"):
        validate_score_receipt(
            score.model_copy(update={"render_profile_sha256": "1" * 64}),
            state=state, task_id="epic.2", mandatory_views={"front"},
        )


@pytest.mark.parametrize("reference_kind", ["calibrated", "self"])
def test_a_retained_capture_yields_a_valid_start_packet_and_receipt(tmp_path, reference_kind):
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
    capture_identity = next(artifact["sha256"] for artifact in retained["artifacts"]
                            if artifact["kind"] == "capture_receipt")

    # Every reported URI resolves and re-hashes, before it is quoted anywhere.
    for artifact in retained["artifacts"]:
        assert verify(ready.data_dir, artifact["uri"], artifact["sha256"])["verified"]
    for capture in retained["captures"]:
        assert verify(ready.data_dir, capture["image"]["uri"],
                      capture["image"]["sha256"])["verified"]

    start = ObjectLoopStartArgs.model_validate({
        **start_args(),
        "incumbent_sha256": ready.document["candidate_sha256"],
        "incumbent_capture_sha256": capture_identity,
        "reference_kind": reference_kind,
        "reference_sha256": capture_identity if reference_kind == "self" else H,
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
    assert start.incumbent_capture_sha256 == capture_identity
    if reference_kind == "self":
        assert start.reference_sha256 == capture_identity != start.incumbent_sha256
    assert start.incumbent_artifact.uri.startswith("artifact://sha256/")

    scored = receipt(
        base_candidate_sha256=ready.document["candidate_sha256"],
        candidate_sha256=ready.document["candidate_sha256"],
        reference_sha256=start.reference_sha256,
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
        "reference_sha256": start.reference_sha256,
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


async def _refusal_for(mixin, name):
    """Run a session-scoped handler with a non-elevated worker scope."""
    handler = mixin()
    handler._current_scope = {"kind": "session", "session_id": "w", "project_id": "p"}
    handler.config = SimpleNamespace(data_dir="/nonexistent")
    handler.db = None
    return await getattr(handler, name)({"project_id": "p"})


async def test_the_operator_steps_reach_the_supervisor_session_and_not_a_worker():
    """The supervisor runs the operator steps; a worker session never does.

    Pinned here rather than left to the scope layer's defaults, because the
    start runbook depends on it: a worker that reports "out of scope" for
    ``formula cook object`` or ``object_loop_start`` must be told to hand the
    step to the project supervisor, not to work around it.
    """
    from src.api.scope import RequestScope, check_command_scope

    supervisor = RequestScope(
        kind="session", project_id="p", task_id=None, session_id="sup", elevated=True,
    )
    worker = RequestScope(
        kind="session", project_id="p", task_id="t", session_id="w", elevated=False,
    )
    for name in ("formula_cook", "object_loop_start", "object_loop_reconcile",
                 "object_score_record", "object_loop_inputs"):
        args = {"project_id": "p"}
        assert check_command_scope(name, dict(args), supervisor) is None, name
        refused = check_command_scope(name, dict(args), worker)
        assert refused == f"out of scope: {name}" or "not available" in refused, name
    # ...and the two refusals that live in the handlers rather than the gate.
    # Both name the step that supersedes them, so a worker that hits one is
    # handed to the supervisor instead of looking for a way around it.
    from src.commands.formula_commands import FormulaCommandsMixin
    from src.commands.object_loop_commands import ObjectLoopCommandsMixin

    for mixin, name, needle in (
        (FormulaCommandsMixin, "_cmd_formula_cook", "not available to agent sessions"),
        (ObjectLoopCommandsMixin, "_cmd_object_loop_inputs", "project supervisor"),
    ):
        refused = await _refusal_for(mixin, name)
        assert refused["success"] is False and needle in refused["error"], name


@pytest.fixture
async def object_evidence_env(command_handler_factory, tmp_path):
    """Real retained PNGs and two loops, with a live pool/task reader added by tests."""
    import shutil
    from io import BytesIO
    from PIL import Image

    def png(color):
        stream = BytesIO()
        Image.new("RGB", (1280, 720), color).save(stream, format="PNG")
        return stream.getvalue()

    handler = await command_handler_factory()
    db = handler.db
    baseline = retention(tmp_path, views=("front", "side"), image_bytes=png("gray"))
    handler.config.data_dir = str(baseline.data_dir)
    handler.config.security.capability_enforcement = "enforce"
    await db.create_project(Project(id="p", name="Project"))
    await db.create_project(Project(id="other", name="Other"))
    await db.create_task(Task(id="epic", project_id="p", title="Epic", description="Epic",
                              status=TaskStatus.READY))
    await approved_brief(db)
    capture_artifact = next(a for a in baseline.retained["artifacts"]
                            if a["kind"] == "capture_receipt")
    candidate = baseline.retained["candidate_artifact"]
    started = await handler._cmd_object_loop_start({
        **start_args(), "incumbent_sha256": candidate["sha256"],
        "incumbent_artifact": {k: candidate[k] for k in ("uri", "sha256")},
        "incumbent_capture_sha256": capture_artifact["sha256"],
        "rig_sha256": baseline.retained["rig_sha256"], "mandatory_views": ["front", "side"],
    })
    assert started["success"], started
    after = {name: retain_bytes(png(color), data_dir=baseline.data_dir,
                               kind="capture_image")
             for name, color in (("front", "green"), ("side", "blue"))}
    foreign = retain_bytes(b"foreign object evidence", data_dir=baseline.data_dir,
                           kind="capture_image")
    async with db.immediate() as conn:
        row = (await conn.execute(select(object_loops))).mappings().one()
        state = {**row.state, "status": "stopped", "intent": None, "wave": [],
                 "stop_reason": "bounded run finished", "best_receipt": {
                     "capture_receipts": [{"view_id": name, "image": pointer}
                                          for name, pointer in after.items()],
                 }}
        await conn.execute(update(object_loops).values(state=state))
        foreign_state = {**state, "object_id": "foreign-rock",
                         "best_receipt": {"capture_receipts": [
                             {"view_id": "front", "image": foreign}]}}
        await conn.execute(insert(object_loops).values(
            object_id="foreign-rock", project_id="p", epic_task_id="foreign-epic",
            finalization_task_id="foreign-finalizer", terminal_gate_id="foreign-gate",
            version=1, state=foreign_state, created_at=1, updated_at=1,
        ))
        await conn.execute(insert(object_loops).values(
            object_id="other-project-rock", project_id="other", epic_task_id="other-epic",
            finalization_task_id="other-finalizer", terminal_gate_id="other-gate",
            version=1, state={**foreign_state, "project_id": "other"},
            created_at=1, updated_at=1,
        ))
    # The finalizer cannot rely on any capture-run path surviving.
    shutil.rmtree(baseline.directory)
    yield SimpleNamespace(handler=handler, db=db, state=state, baseline=baseline,
                          after=after, foreign=foreign, finalizer=row.finalization_task_id)
    handler._current_scope = None
    await db.close()


async def object_reader(env, *, lifecycle="pool", purpose="finalize", harness="codex"):
    from src.api.auth import RequestScope
    from src.models import Agent, AgentProfile, AgentState, SessionRecord
    from src.profiles.parser import parse_profile

    profile_id = f"worker-{harness}"
    parsed = parse_profile((Path("src/profiles/defaults") / profile_id / "profile.md").read_text())
    assert not parsed.errors
    await env.db.upsert_profile(AgentProfile(id=profile_id, name=profile_id, harness=harness,
                                            **parsed.capabilities))
    task_id = env.finalizer
    if purpose == "candidate":
        task_id = "epic.round"
        await env.db.create_task(Task(
            id=task_id, project_id="p", parent_task_id="epic", title="Round",
            description="Round", created_by_kind="object_loop", created_by_id="rock",
        ))
        async with env.db.immediate() as conn:
            await env.db._upsert_meta(task_id, "object_experiment", {
                "object_id": "rock", "purpose": "candidate", "publish_source": False,
            }, conn=conn)
    await env.db.create_agent(Agent(id="reader", name="reader", profile_id=profile_id))
    await env.db.update_task(task_id, status=TaskStatus.IN_PROGRESS, assigned_agent_id="reader")
    await env.db.update_agent("reader", state=AgentState.BUSY, current_task_id=task_id)
    await env.db.create_session(SessionRecord(
        id="reader-session", task_id=task_id, project_id="p", agent_id="reader",
        profile_id=profile_id, harness=harness, provider="fake", name="reader",
        lifecycle=lifecycle, state="running", work_dir="/tmp", epoch="test",
        instance_token="instance-reader", started_at=time.time(), last_claim_epoch=0,
    ))
    return RequestScope(kind="session", session_id="reader-session", project_id="p",
                        task_id=task_id if lifecycle == "task" else None,
                        session_instance_token="instance-reader")


async def evidence_call(env, scope, command, args):
    """Exercise HTTP scope injection followed by real enforcing command dispatch."""
    from dataclasses import asdict
    from src.api.scope import check_request_scope

    args = dict(args)
    error = await check_request_scope(command, args, scope, db=env.db)
    if error:
        return {"success": False, "error": error}
    return await env.handler.execute(command, {**args, "_scope": asdict(scope)})


async def retained_job_artifact(env, task_id, *, project_id="p"):
    from src.models import RepoSourceType, Workspace
    from tests.test_jobs_queries import values

    workspace_id = f"workspace-{task_id}"
    await env.db.create_workspace(Workspace(
        id=workspace_id, project_id=project_id, workspace_path=f"/tmp/{workspace_id}",
        source_type=RepoSourceType.LINK, locked_by_task_id=task_id,
    ))
    job = await env.db.submit_job(values(
        project_id=project_id, task_id=task_id, owner_id=task_id,
        workspace_id=workspace_id, claim_epoch=0,
    ))
    return retain_bytes(f"capture from {job['id']}".encode(), data_dir=env.handler.config.data_dir,
                        kind="capture_image", origin={"job_id": job["id"]})


@pytest.mark.parametrize("bootstrap", [False, True])
async def test_workers_verify_own_retained_jobs_before_scoring(object_evidence_env, bootstrap):
    env = object_evidence_env
    scope = await object_reader(env, purpose="candidate")
    own = await retained_job_artifact(env, "epic.round")
    await env.db.create_task(Task(id="foreign-round", project_id="p", title="Foreign round",
                                 description="Foreign round", status=TaskStatus.IN_PROGRESS))
    foreign = await retained_job_artifact(env, "foreign-round")
    if bootstrap:
        from sqlalchemy import delete

        async with env.db.immediate() as conn:
            await conn.execute(delete(object_loops).where(object_loops.c.object_id == "rock"))
    verified = await evidence_call(env, scope, "artifact_verify", {"uri": own["uri"]})
    assert verified["success"] and verified["verified"], verified
    refused = await evidence_call(env, scope, "artifact_verify", {"uri": foreign["uri"]})
    assert not refused["success"] and "out of scope" in refused["error"], refused
    await env.db.update_task("epic.round", claim_epoch=1)
    stale = await evidence_call(env, scope, "artifact_verify", {"uri": own["uri"]})
    assert not stale["success"] and "out of scope" in stale["error"], stale


@pytest.mark.parametrize("lifecycle", ["task", "pool"])
@pytest.mark.parametrize("purpose", ["finalize", "candidate"])
@pytest.mark.parametrize("harness", ["codex", "claude"])
async def test_object_workers_read_only_their_own_checkpoint_and_artifacts(
    object_evidence_env, lifecycle, purpose, harness,
):
    env = object_evidence_env
    scope = await object_reader(env, lifecycle=lifecycle, purpose=purpose, harness=harness)
    checkpoint = await evidence_call(env, scope, "object_checkpoint_read", {"object_id": "rock"})
    assert checkpoint["success"], checkpoint
    for name, comparison in checkpoint["evidence"]["views"].items():
        assert comparison["before"]["verified"] and comparison["after"]["verified"]
        assert comparison["before"]["sha256"] != comparison["after"]["sha256"]
        assert comparison["after"]["sha256"] == env.after[name]["sha256"]
        for image in comparison.values():
            verified = await evidence_call(env, scope, "artifact_verify", {"uri": image["uri"]})
            assert verified["success"] and verified["verified"], verified
            assert Path(verified["path"]).read_bytes()
    for object_id in ("foreign-rock", "other-project-rock"):
        refused = await evidence_call(env, scope, "object_checkpoint_read", {"object_id": object_id})
        assert not refused["success"] and "out of scope" in refused["error"]
    refused = await evidence_call(env, scope, "artifact_verify", {"uri": env.foreign["uri"]})
    assert not refused["success"] and "out of scope" in refused["error"]
    refused = await evidence_call(env, scope, "artifact_verify", {
        "uri": env.after["front"]["uri"], "project_id": "other",
    })
    assert not refused["success"] and "project_id mismatch" in refused["error"]


@pytest.mark.parametrize("invalid", ["stale_claim", "replaced_session", "closed", "prose"])
async def test_object_evidence_refuses_stale_or_unrelated_held_work(object_evidence_env, invalid):
    env = object_evidence_env
    scope = await object_reader(env, purpose="candidate")
    if invalid == "stale_claim":
        await env.db.update_task("epic.round", claim_epoch=1)
    elif invalid == "replaced_session":
        await env.db.update_session("reader-session", instance_token="replacement")
    elif invalid == "closed":
        await env.db.update_task("epic.round", status=TaskStatus.COMPLETED)
    else:
        await env.db.update_task("epic.round", created_by_id="foreign-rock",
                                 description="Read rock and all its evidence")
    for command, args in (
        ("object_checkpoint_read", {"object_id": "rock"}),
        ("artifact_verify", {"uri": env.after["front"]["uri"]}),
    ):
        refused = await evidence_call(env, scope, command, args)
        assert not refused["success"] and "out of scope" in refused["error"], refused


@pytest.mark.parametrize("global_admin", [False, True])
async def test_supervisor_profile_can_read_object_evidence(object_evidence_env, global_admin):
    from src.api.auth import RequestScope
    from src.models import AgentProfile, SessionRecord
    from src.profiles.parser import parse_profile

    env = object_evidence_env
    parsed = parse_profile(Path("src/profiles/defaults/supervisor/profile.md").read_text())
    assert not parsed.errors
    await env.db.upsert_profile(AgentProfile(id="supervisor", name="Supervisor",
                                            **parsed.capabilities))
    await env.db.create_session(SessionRecord(
        id="supervisor", project_id=None if global_admin else "p", profile_id="supervisor",
        harness="codex", provider="fake", name="supervisor", lifecycle="named",
        state="running", work_dir="/tmp", epoch="test", instance_token="sup", started_at=1,
    ))
    scope = RequestScope(kind="session", session_id="supervisor", elevated=True,
                         project_id=None if global_admin else "p")
    checkpoint = await evidence_call(env, scope, "object_checkpoint_read", {
        "object_id": "rock", "project_id": "p",
    })
    assert checkpoint["success"], checkpoint
    verified = await evidence_call(env, scope, "artifact_verify", {"uri": env.after["side"]["uri"]})
    assert verified["success"] and verified["verified"], verified
    # Baseline artifacts need verification before object_loop_start creates a row.
    await env.db.create_task(Task(id="bootstrap", project_id="p", title="Bootstrap",
                                 description="Bootstrap", status=TaskStatus.IN_PROGRESS))
    retained = await retained_job_artifact(env, "bootstrap")
    verified = await evidence_call(env, scope, "artifact_verify", {"uri": retained["uri"]})
    assert verified["success"] and verified["verified"], verified
    unowned = retain_bytes(b"operator retained evidence", data_dir=env.handler.config.data_dir,
                           kind="capture_image")
    unowned_result = await evidence_call(env, scope, "artifact_verify", {"uri": unowned["uri"]})
    assert unowned_result["success"] is global_admin, unowned_result
    foreign = await evidence_call(env, scope, "object_checkpoint_read", {
        "object_id": "other-project-rock", "project_id": "other",
    })
    assert foreign["success"] is global_admin, foreign


async def test_finalizer_receives_verified_comparison_paths_and_explicit_gaps(object_evidence_env):
    from src.object_loop.artifacts import resolve
    from src.object_loop.evidence import final_evidence

    env = object_evidence_env
    reconciled = await env.handler._cmd_object_loop_reconcile({"project_id": "p", "object_id": "rock"})
    assert reconciled["success"], reconciled
    finalizer = await env.db.get_task(env.finalizer)
    packet = json.loads(finalizer.description.split("Internal evidence (for the worker):\n")[1])
    assert set(packet["views"]) == {"front", "side"}
    for comparison in packet["views"].values():
        assert comparison["before"]["verified"] and comparison["after"]["verified"]
        assert "artifacts/objects" in comparison["before"]["path"]
    resolve(env.handler.config.data_dir, env.after["side"]["uri"]).write_bytes(b"corrupt")
    packet = final_evidence(env.handler.config.data_dir, env.state)
    assert packet["views"]["front"]["after"]["verified"]
    assert not packet["views"]["side"]["after"]["verified"]
    assert "path" not in packet["views"]["side"]["after"]
    assert "does not hash" in packet["views"]["side"]["after"]["error"]
    unchanged = final_evidence(env.handler.config.data_dir, {**env.state, "best_receipt": None})
    assert unchanged["views"]["front"]["before"] == unchanged["views"]["front"]["after"]


async def test_finalize_worker_attaches_all_verified_before_after_images(object_evidence_env):
    import base64

    env = object_evidence_env
    scope = await object_reader(env)
    checkpoint = await evidence_call(env, scope, "object_checkpoint_read", {"object_id": "rock"})
    assert checkpoint["success"], checkpoint
    submitted = await evidence_call(env, scope, "review_submit", {
        "task_id": env.finalizer, "kind": "other", "title": "Rock result",
        "content": "# Rock result\nBefore and after images for each view.\n",
    })
    assert submitted["success"], submitted
    for name, comparison in checkpoint["evidence"]["views"].items():
        for label, image in comparison.items():
            attached = await evidence_call(env, scope, "review_attachment_add", {
                "review_id": submitted["review_id"], "revision": 1,
                "data_base64": base64.b64encode(Path(image["path"]).read_bytes()).decode(),
                "content_type": "image/png", "view_id": name,
                "candidate_id": label.title(), "caption": label.title(),
            })
            assert attached["success"], attached
            assert attached["attachment"]["sha256"] == image["sha256"]
    attachments = await env.db.list_review_attachments(submitted["review_id"], 1)
    assert {(a["view_id"], a["candidate_id"]) for a in attachments} == {
        (name, label) for name in ("front", "side") for label in ("Before", "After")
    }
