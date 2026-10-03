"""Epic implementation progress kept apart from delivery (noble-nexus-42).

The projection is read-only: it classifies facts existing reads establish and
never changes a task's stored lifecycle. Spec:
docs/superpowers/specs/2026-10-02-epic-delivery-status-design.md.
"""

# The imported fixtures intentionally share names with injected test parameters.
# ruff: noqa: F811

from __future__ import annotations

import json
import time
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import insert, select, update

from src.api.models.task import EpicDeliveryStatus, GetTaskResponse
from src.commands.handler import CommandHandler
from src.database.tables import (
    events,
    gates,
    integration_branch_owners,
    integration_parent_episodes,
    integration_parent_operation_completions,
    integration_parent_verifications,
    integration_repair_operations,
    projects,
    repos,
    task_delivery_receipts,
    task_gates,
    task_integration_checkpoints,
    tasks,
)
from src.git.manager import GitManager
from src.integration.delivery_observer import DeliveryObserver, DeliveryView, delivery_targets
from src.integration.delivery_truth import (
    DeliveryEvidence,
    DeliveryState,
    load_delivery_requests,
)
from src.integration.epic_delivery import (
    CanonicalDelivery,
    EpicDeliveryProjection,
    EpicFacts,
    WorkerFacts,
    classify_epic_delivery,
    lease_ttl_from,
)
from src.models import (
    AgentProfile,
    Project,
    RepoConfig,
    RepoSourceType,
    Task,
    TaskCompletion,
    TaskStatus,
)
from tests.db_fixtures import seed_task_session_attempt
from tests.test_delivery_consumers import git

# A real ParentCompletion close on a real origin, at generation 6.
from tests.test_integration_parent_completion import (
    _code_receipt,
    _parent_tree,
    git_completed_parent,  # noqa: F401
)

NOW = 1_800_000_000.0
TTL = 480.0
OPERATION = {
    "id": "op-1", "state": "escalated", "active_stage": 13, "verifier_task_id": "verify-op-1",
    "updated_at": NOW - 600,
}
VERIFYING = {
    "task_id": "epic", "repository_id": "repo", "branch": "aq/epic", "state": "verifying",
    "episode_id": "episode", "generation": 2, "checkpoint_sha": "a" * 40, "verified_sha": None,
    "verified_generation": None, "last_completed_operation_id": None, "updated_at": NOW - 900,
}


def _children(count: int = 5, status: str = "COMPLETED") -> tuple[dict, ...]:
    return tuple(
        {"id": f"epic.{index}", "status": status, "updated_at": NOW - 7200 + index, "title": f"Child {index}"}
        for index in range(1, count + 1)
    )


def _facts(**overrides) -> EpicFacts:
    """A managed epic, 5/5 implemented, frozen for verification, readiness ready."""
    values = {
        "task_id": "epic",
        "status": "PAUSED",
        "updated_at": NOW - 900,
        "mode": "train",
        "children": _children(),
        "checkpoint": VERIFYING,
        "operation": OPERATION,
        "readiness": {"outcome": "ready", "blockers": []},
    }
    values.update(overrides)
    return EpicFacts(**values)


def _classify(facts: EpicFacts) -> dict:
    result = classify_epic_delivery(facts, now=NOW, lease_ttl=TTL)
    EpicDeliveryStatus.model_validate(result)  # every answer fits the wire model
    return result


def _verifier(**overrides) -> WorkerFacts:
    values = {"id": "verify-op-1", "status": "READY", "updated_at": NOW - 300, "profile_id": "verifier"}
    values.update(overrides)
    return WorkerFacts(**values)


STAGE_13 = {"ordinal": 13, "state": "expired", "repair_task_id": "repair-op-13", "completed_at": NOW - 3600}
REPAIR_OWNER = {
    "owner_id": "repair-op-13", "owner_role": "repair", "handoff_state": "reserved",
    "updated_at": NOW - 3500,
}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_active_integration_needs_a_live_recent_session():
    session = {"id": "sess-1", "profile_id": "standard-high-claude", "last_activity": NOW - 30}
    writer = WorkerFacts(id="repair-op-0", status="IN_PROGRESS", session=session)
    stage = {"ordinal": 0, "state": "active", "repair_task_id": "repair-op-0"}

    active = _classify(_facts(writer=writer, stage=stage, operation={**OPERATION, "state": "active"}))

    assert (active["state"], active["label"], active["display_status"]) == (
        "integrating", "Integrating", "Integrating",
    )
    assert active["responsible"] == {
        "kind": "session", "id": "sess-1", "label": "standard-high-claude sess-1",
    }
    assert active["since"] == NOW - 30
    assert {"kind": "task", "id": "repair-op-0", "label": "Integration task repair-op-0"} in active["links"]

    # A claimed writer with no live session is not evidence of work.
    idle = _classify(_facts(writer=replace(writer, session=None), stage=stage))
    assert idle["state"] != "integrating"


def test_a_silent_session_is_stale_evidence_not_active_work():
    session = {"id": "sess-2", "profile_id": "p", "last_activity": NOW - 1500}
    result = _classify(_facts(verifier=_verifier(status="IN_PROGRESS", session=session)))

    assert result["state"] == "unknown"
    assert result["label"] == "Verification status stale"
    assert result["evidence"] == "stale"
    assert "no activity for 25m (lease 8m)" in result["reason"]


def test_a_live_verifier_is_verifying():
    session = {"id": "sess-3", "profile_id": "verifier", "started_at": NOW - 120, "last_activity": None}
    result = _classify(_facts(verifier=_verifier(status="IN_PROGRESS", session=session)))
    assert (result["state"], result["label"]) == ("verifying", "Verifying aggregate")


def test_queued_verifier_is_queued_never_verifying():
    result = _classify(_facts(verifier=_verifier()))

    assert result["state"] == "queued"
    assert result["label"] == "Verification queued - waiting for a worker"
    assert result["display_status"] == "Queued"
    assert result["hold"] == "integration"
    assert result["responsible"]["label"] == "Worker pool"
    assert {"kind": "task", "id": "verify-op-1", "label": "Verification task verify-op-1"} in result["links"]


def test_an_unrouted_verifier_waits_for_routing():
    result = _classify(_facts(verifier=_verifier(profile_id=None)))
    assert result["label"] == "Verification queued - waiting for routing"
    assert result["responsible"]["label"] == "Router"


def test_stranded_reservation_blocks_verification_on_a_branch_handoff():
    """azure-vault-92: delivery is ready, the verifier READY, repair owns the branch."""
    result = _classify(
        _facts(
            verifier=_verifier(exclusions=("origin_not_materialized",)),
            stage=STAGE_13,
            owner=REPAIR_OWNER,
            children=_children(9),
        )
    )

    assert result["state"] == "blocked"
    assert result["label"] == "Verification blocked - branch handoff required"
    assert result["display_status"] == "Delivery blocked"
    assert result["responsible"] == {"kind": "task", "id": "repair-op-13", "label": "Repair stage 13"}
    assert result["reason"].startswith("Repair stage 13 still holds the branch reservation (reserved)")
    assert (result["implementation_completed"], result["implementation_total"]) == (9, 9)
    assert result["since"] == NOW - 300
    link_ids = {link["id"] for link in result["links"]}
    assert {"verify-op-1", "repair-op-13", "op-1"} <= link_ids


def test_any_other_frontier_exclusion_names_the_predicate():
    result = _classify(_facts(verifier=_verifier(exclusions=("hold_label",))))
    assert result["label"] == "Verification blocked - not claimable"
    assert "no hold:* label may be present" in result["reason"]


def test_missing_receipt_after_the_aggregate_froze_is_an_uncollected_final_fix():
    """calm-grove-25: 5/5 complete, receipt_missing for the fix child .5."""
    readiness = {"outcome": "waiting", "blockers": [{"task_id": "epic.5", "reason": "receipt_missing"}]}
    result = _classify(_facts(readiness=readiness, verifier=_verifier(status="BLOCKED")))

    assert result["state"] == "blocked"
    assert result["label"] == "Integration blocked - final fix not collected"
    assert result["remedy"] == "aq integration reopen-collection epic"
    assert result["responsible"]["kind"] == "operator"
    assert result["since"] == NOW - 7200 + 5
    assert {"kind": "task", "id": "epic.5", "label": "Child 5"} in result["links"]

    collecting = _classify(
        _facts(readiness=readiness, checkpoint={**VERIFYING, "state": "awaiting_children"})
    )
    assert (collecting["state"], collecting["label"]) == ("queued", "Collecting child work")
    assert collecting["responsible"]["label"] == "Integration collector"


def test_other_readiness_blockers_are_blocked_with_their_cause():
    readiness = {"outcome": "waiting", "blockers": [{"task_id": "epic.2", "reason": "receipt_chain"}]}
    result = _classify(_facts(readiness=readiness))
    assert result["label"] == "Integration blocked - receipt chain broken"
    assert result["reason"].startswith("epic.2:")


def test_a_failed_verifier_blocks_with_its_terminal_reason():
    result = _classify(
        _facts(verifier=_verifier(status="BLOCKED", blocked_terminal="session_close_hard_failure"))
    )
    assert result["label"] == "Verification blocked - verifier failed"
    assert "session close hard failure" in result["reason"]


def test_an_expired_stage_with_nothing_live_is_blocked():
    result = _classify(_facts(stage=STAGE_13, writer=WorkerFacts(id="repair-op-13", status="COMPLETED")))
    assert result["label"] == "Integration blocked - repair stage 13 expired"
    assert result["since"] == NOW - 600


def test_paused_is_reserved_for_an_operator_hold():
    paused = _classify(_facts(manual_hold=True))
    assert (paused["state"], paused["label"], paused["display_status"], paused["hold"]) == (
        "paused", "Paused by operator", "Paused", "operator",
    )
    held = _classify(_facts(status="IN_PROGRESS", hold_labels=("hold:release",)))
    assert (held["label"], held["display_status"]) == ("Held by operator", "Held")


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"verifier": _verifier()},
        {"verifier": _verifier(exclusions=("origin_not_materialized",)), "owner": REPAIR_OWNER},
        {"children": _children(5, "IN_PROGRESS")},
        {"checkpoint": None},
        {"readiness_error": "boom"},
    ],
)
def test_an_integration_hold_never_reads_as_paused(overrides):
    result = _classify(_facts(**overrides))
    assert result["display_status"] != "Paused"
    assert result["hold"] == ("integration" if overrides.get("checkpoint", VERIFYING) else None)


def test_human_approval_holds():
    gate = {"id": "gate-1", "title": "Approve the release plan", "created_at": NOW - 60}
    approval = _classify(_facts(approval_gates=(gate,)))
    assert (approval["state"], approval["label"], approval["reason"]) == (
        "awaiting_approval", "Awaiting approval", "Approve the release plan",
    )
    assert {"kind": "gate", "id": "gate-1", "label": "Gate gate-1"} in approval["links"]

    human = _classify(_facts(operation={**OPERATION, "state": "human_required"}))
    assert human["label"] == "Integration awaiting operator decision"
    assert human["remedy"] == "aq integration resume op-1"

    batch = _classify(
        _facts(status="COMPLETED", operation=None, batch={"id": "b1", "lifecycle": "human_blocked"})
    )
    assert batch["label"] == "Train awaiting operator decision"


def test_only_a_current_receipt_marks_delivery_complete():
    verified = {**VERIFYING, "verified_sha": "a" * 40, "verified_generation": 2,
                "last_completed_operation_id": "op-1", "state": "awaiting_children"}
    done = _facts(status="COMPLETED", operation=None, checkpoint=verified)

    # 5/5 implemented and complete is still not delivered.
    queued = _classify(done)
    assert (queued["state"], queued["label"]) == ("queued", "Waiting for the next train")

    receipt = {"target_branch": "refs/heads/main", "batch_id": "batch-1", "created_at": NOW - 10,
               "reviewed_head_sha": "a" * 40, "target_task_id": None}
    delivered = _classify(replace(done, receipt=receipt))
    assert (delivered["state"], delivered["label"], delivered["display_status"]) == (
        "delivered", "Delivered", "Delivered",
    )
    assert delivered["reason"] == "Delivered to main by train batch batch-1"
    assert delivered["since"] == NOW - 10

    nested = _classify(replace(done, parent_task_id="root", receipt={**receipt, "target_task_id": "root"}))
    assert nested["label"] == "Delivered to parent epic"

    stale = _classify(replace(done, stale_receipt={**receipt, "reviewed_head_sha": "b" * 40}))
    assert (stale["state"], stale["evidence"], stale["label"]) == (
        "unknown", "stale", "Delivery evidence stale",
    )


def test_a_root_in_a_train_batch():
    done = _facts(status="COMPLETED", operation=None)
    running = _classify(replace(done, batch={"id": "b1", "lifecycle": "testing", "updated_at": NOW}))
    assert (running["state"], running["label"]) == ("integrating", "Integrating to main")
    sealed = _classify(replace(done, batch={"id": "b1", "lifecycle": "sealed"}))
    assert (sealed["state"], sealed["label"]) == ("queued", "Queued in train batch")


def test_unavailable_evidence_is_reported_as_unknown():
    missing = _classify(_facts(checkpoint=None))
    assert (missing["state"], missing["evidence"]) == ("unknown", "unavailable")

    failed_read = _classify(_facts(readiness_error="Collection readiness could not be read: x"))
    assert (failed_read["state"], failed_read["reason"]) == (
        "unknown", "Collection readiness could not be read: x",
    )

    archived = _classify(_facts(verifier=WorkerFacts(id="verify-op-1")))
    assert archived["evidence"] == "unavailable"
    assert "no longer in the queue" in archived["reason"]

    orphan = _classify(_facts(verifier=_verifier(status="IN_PROGRESS")))
    assert (orphan["label"], orphan["evidence"]) == ("Verification status stale", "stale")


def test_work_finished_outside_the_train_is_not_tracked():
    no_checkpoint = _classify(_facts(status="COMPLETED", checkpoint=None, operation=None))
    assert (no_checkpoint["state"], no_checkpoint["label"], no_checkpoint["evidence"]) == (
        "not_tracked", "Completed outside the train", "untracked",
    )
    cancelled = _classify(_facts(status="COMPLETED", operation=None, collection_cancelled=True))
    assert cancelled["state"] == "not_tracked"
    unverified = _classify(_facts(status="COMPLETED", operation=None, checkpoint={**VERIFYING, "episode_id": None}))
    assert unverified["state"] == "not_tracked"
    orphaned = _classify(
        _facts(status="COMPLETED", operation=None, parent_task_id="root", parent_status="COMPLETED")
    )
    assert orphaned["state"] == "not_tracked"
    waiting = _classify(
        _facts(status="COMPLETED", operation=None, parent_task_id="root", parent_status="PAUSED")
    )
    assert waiting["label"] == "Waiting for parent collection"


def test_implementation_and_untracked_modes():
    open_child = {"id": "epic.9", "status": "IN_PROGRESS", "updated_at": NOW - 5, "title": "x"}
    implementing = _classify(_facts(children=(*_children(3), open_child)))
    assert (implementing["state"], implementing["display_status"]) == ("implementing", "In progress")
    assert (implementing["implementation_completed"], implementing["implementation_total"]) == (3, 4)
    assert implementing["since"] == NOW - 5

    development = _classify(_facts(mode="development", status="COMPLETED", checkpoint=None))
    assert (development["state"], development["display_status"]) == ("not_tracked", "Completed")

    backoff = _classify(_facts(mode="disabled", checkpoint=None, resume_after=NOW + 60))
    assert (backoff["hold"], backoff["display_status"]) == ("backoff", "Waiting")


def test_lease_ttl_comes_from_numeric_config_only():
    assert lease_ttl_from(None) == 480.0
    config = MagicMock()
    assert lease_ttl_from(config) == 480.0  # a mock is not a number
    config.sessions.lease_ttl_seconds = 120
    assert lease_ttl_from(config) == 120.0


# ---------------------------------------------------------------------------
# Canonical request-scoped git delivery truth
# ---------------------------------------------------------------------------

#: A canonically adopted epic of a train-mode parent: generation 6, verified
#: source 2593096b, adopted onto main at c0ad23f3.  The live bug reported this
#: exact shape reading as "Waiting for the next train".
ADOPTED = CanonicalDelivery(
    state="contained", reason="verified_parent_completion",
    source="2593096b" + "a" * 32, target_ref="refs/heads/main",
    target_oid="c0ad23f3" + "b" * 32, completion_id="parent:verification-6",
    parent_generation=6, since=NOW - 30,
)
VERIFIED = {
    **VERIFYING, "state": "integration_ready", "verified_sha": "a" * 40,
    "verified_generation": 2, "last_completed_operation_id": "op-1",
}


def _complete(**overrides) -> EpicFacts:
    """Complete, train-verified, with no receipt and no batch: the live case."""
    values = {
        "task_id": "epic", "status": "COMPLETED", "updated_at": NOW - 60,
        "project_id": "p", "mode": "train", "children": _children(),
        "checkpoint": VERIFIED,
    }
    values.update(overrides)
    return EpicFacts(**values)


def test_a_canonically_adopted_epic_is_delivered_where_no_receipt_exists():
    # Without git's answer this epic reads as delivered-to-be: the bug.
    assert _classify(_complete())["label"] == "Waiting for the next train"

    delivered = _classify(_complete(canonical=ADOPTED))
    assert (delivered["state"], delivered["label"], delivered["display_status"]) == (
        "delivered", "Delivered", "Delivered",
    )
    assert delivered["evidence"] == "current"
    assert delivered["reason"] == "Verified generation 6 source 2593096baaaa is in main at c0ad23f3bbbb"
    assert delivered["since"] == NOW - 30
    # Nothing is invented to make it read that way: no receipt, batch or
    # operation link appears, and no row was written.
    assert delivered["links"] == []


def test_operator_equivalence_is_named_rather_than_absorbed():
    equivalent = _classify(replace(_complete(), canonical=replace(
        ADOPTED, acceptance="operator_equivalent",
    )))
    assert equivalent["state"] == "delivered"
    assert equivalent["reason"].endswith("(operator accepted this source as equivalent)")

    leaf = CanonicalDelivery(
        state="contained", reason="git_completion", source="a" * 40,
        target_ref="refs/heads/main", target_oid="c" * 40,
        completion_id="close-epic-2", since=NOW - 5,
    )
    closed = _classify(_complete(canonical=leaf))
    assert closed["reason"] == (
        f"Completion close-epic-2 source {'a' * 12} is in main at {'c' * 12}"
    )


def test_a_settled_generation_is_delivered_and_says_why():
    settled = CanonicalDelivery(
        state="settled", reason="settled: already delivered to the replaced target",
        source="a" * 40, target_ref="refs/heads/main", target_oid="c" * 40,
        completion_id="close-epic-2", settled_reason="already delivered to release/2.0",
    )
    result = _classify(_complete(canonical=settled))
    assert (result["state"], result["label"]) == (
        "delivered", "Delivered - not owed to this target",
    )
    assert result["reason"] == (
        "Recorded as not owed to main: already delivered to release/2.0"
    )


def test_provable_pending_work_replaces_the_claim_that_nothing_is_owed():
    pending = replace(ADOPTED, state="pending", reason="verified_parent_completion")
    for mode in ("train", "development", "disabled"):
        result = _classify(_complete(mode=mode, canonical=pending))
        assert (result["state"], result["label"], result["evidence"]) == (
            "queued", "Delivery pending", "current",
        )
        assert result["responsible"] == {"kind": "system", "id": None, "label": "Integration publisher"}
        assert "verified generation 6 source 2593096baaaa is not in main" in result["reason"]


def test_git_that_cannot_account_for_the_epic_says_so():
    unlabelled = replace(ADOPTED, state="unknown", reason="missing_git_provenance")
    unknown = _classify(_complete(canonical=unlabelled))
    assert (unknown["state"], unknown["label"], unknown["evidence"]) == (
        "unknown", "Delivery evidence unavailable", "unavailable",
    )
    assert "no exact source is retained in git" in unknown["reason"]
    assert unknown["remedy"] == "aq integration migrate-provenance p --task-id epic --apply"

    misplaced = _classify(_complete(canonical=replace(
        ADOPTED, state="unknown", reason="scope_mismatch",
    )))
    assert "another repository" in misplaced["reason"]
    assert misplaced["remedy"] is None

    unreachable = _classify(_complete(canonical=replace(
        ADOPTED, state="unknown", reason="observer_error: GitError",
    )))
    assert "git could not be observed (observer_error: GitError)" in unreachable["reason"]


def test_an_identity_that_changed_after_git_was_asked_is_stale():
    moved = _classify(_complete(canonical=replace(
        ADOPTED, state="unknown", reason="changed_during_observation", changed=True,
    )))
    assert (moved["state"], moved["label"], moved["evidence"]) == (
        "unknown", "Delivery evidence stale", "stale",
    )
    # A stale identity never claims delivery, whatever git had said.
    assert _classify(_complete(canonical=replace(ADOPTED, changed=True)))["state"] == "queued"


def test_an_organizational_epic_keeps_its_ordinary_reading():
    container = replace(ADOPTED, state="no_artifact", reason="branchless_organization",
                        source=None, completion_id=None, parent_generation=None)
    assert _classify(_complete(canonical=container))["label"] == "Waiting for the next train"
    assert _classify(_complete(mode="development", canonical=container))["state"] == "not_tracked"


def test_gates_and_running_integration_keep_their_ordinary_statuses():
    pending = replace(ADOPTED, state="pending")
    unknown = replace(ADOPTED, state="unknown", reason="missing_git_provenance")
    session = {"id": "sess-1", "profile_id": "standard-high-claude", "last_activity": NOW - 30}
    writer = WorkerFacts(id="repair-op-0", status="IN_PROGRESS", session=session)
    stage = {"ordinal": 0, "state": "active", "repair_task_id": "repair-op-0"}

    for canonical in (pending, unknown):
        integrating = _classify(_complete(
            status="PAUSED", operation=OPERATION, writer=writer, stage=stage, canonical=canonical,
        ))
        assert integrating["state"] == "integrating"
        assert _classify(_complete(canonical=canonical, batch={"id": "b1", "lifecycle": "testing"}))[
            "state"
        ] == "integrating"
        assert _classify(_complete(
            mode="development", status="PAUSED", manual_hold=True, canonical=canonical,
        ))["hold"] == "operator"
        gates = ({"id": "gate-1", "title": "Approve", "question": "", "created_at": 1.0},)
        assert _classify(_complete(canonical=canonical, approval_gates=gates))["state"] == (
            "awaiting_approval"
        )
        open_child = {"id": "epic.9", "status": "READY", "updated_at": NOW, "title": "x"}
        assert _classify(_complete(
            canonical=canonical, children=(*_children(3), open_child),
        ))["state"] == "implementing"


# ---------------------------------------------------------------------------
# Fact gathering against a real parent collection
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path, reuse_database):
    database = await reuse_database("epic-delivery.db")
    await database.create_project(Project(id="p", name="integration project"))
    yield database


async def _project(db, task_id: str = "parent") -> dict:
    return (await EpicDeliveryProjection(db, lease_ttl=TTL).for_tasks([task_id]))[task_id]


async def _collected(db, children: int = 2, collect: int | None = None) -> list[str]:
    _hierarchy, _checkpointed, child_ids = await _parent_tree(db, children=children)
    head = "a" * 40
    for index, child_id in enumerate(child_ids[: children if collect is None else collect]):
        after = f"{index + 1:x}" * 40
        await _code_receipt(db, child_id, head, after)
        head = after
    return child_ids


async def _verifying(db, *, owner_role: str, owner_id: str) -> None:
    """Freeze the checkpoint for verification with a READY, routed verifier."""
    await db.create_profile(AgentProfile(id="verifier", name="verifier"))
    await db.create_task(
        Task(
            id="verify-op", project_id="p", title="Verify aggregate for parent", description="",
            status=TaskStatus.READY, repo_id="repo", branch_name="aq/parent",
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == "verify-op")
            .values(profile_id="verifier", route_source="router")
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "parent")
            .values(state="verifying")
        )
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.parent_task_id == "parent")
            .values(verifier_task_id="verify-op")
        )
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.ref == "aq/parent")
            .values(owner_role=owner_role, owner_id=owner_id)
        )


async def test_collection_reads_from_the_real_readiness_projection(db):
    await _parent_tree(db)
    collecting = await _project(db)
    assert (collecting["state"], collecting["label"]) == ("queued", "Collecting child work")
    assert (collecting["implementation_completed"], collecting["implementation_total"]) == (2, 2)
    assert "parent.1, parent.2" in collecting["reason"]


async def test_all_receipts_collected_waits_for_verification(db):
    await _collected(db)
    result = await _project(db)
    assert result["label"] == "Collection complete - verification pending"


async def test_branch_handoff_and_queued_verifier_come_from_the_claim_frontier(db):
    await _collected(db)
    await _verifying(db, owner_role="repair", owner_id="repair-op-13")

    blocked = await _project(db)
    assert blocked["label"] == "Verification blocked - branch handoff required"
    assert blocked["responsible"]["id"] == "repair-op-13"

    async with db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.ref == "aq/parent")
            .values(owner_role="verifier", owner_id="verify-op")
        )
    queued = await _project(db)
    assert (queued["state"], queued["label"]) == ("queued", "Verification queued - waiting for a worker")


async def test_only_a_recently_active_session_reads_as_verifying(db):
    await _collected(db)
    await _verifying(db, owner_role="verifier", owner_id="verify-op")
    await seed_task_session_attempt(
        db, task_id="verify-op", project_id="p", create_task=False, heartbeat_age=10,
        session_id="sess-live", profile_id="verifier",
    )
    live = await _project(db)
    assert (live["state"], live["responsible"]["id"]) == ("verifying", "sess-live")

    projection = EpicDeliveryProjection(db, lease_ttl=TTL, clock=lambda: time.time() + 3600)
    stale = (await projection.for_tasks(["parent"]))["parent"]
    assert (stale["state"], stale["evidence"]) == ("unknown", "stale")


async def test_a_fix_completed_after_the_freeze_is_not_collected(db):
    child_ids = await _collected(db, children=3, collect=2)
    async with db.immediate() as conn:
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "parent")
            .values(state="verifying")
        )
    result = await _project(db)
    assert result["label"] == "Integration blocked - final fix not collected"
    assert result["remedy"] == "aq integration reopen-collection parent"
    assert child_ids[2] in {link["id"] for link in result["links"]}


async def test_a_manual_pause_is_the_only_paused(db):
    await _parent_tree(db)
    await db.pause_task("parent")
    result = await _project(db)
    assert (result["state"], result["display_status"], result["hold"]) == (
        "paused", "Paused", "operator",
    )


async def test_an_open_human_gate_is_awaiting_approval(db):
    await _parent_tree(db)
    async with db.immediate() as conn:
        await conn.execute(
            insert(gates).values(
                id="gate-1", project_id="p", gate_type="human", title="Approve release",
                question="", status="open", created_at=1.0,
            )
        )
        await conn.execute(insert(task_gates).values(task_id="parent", gate_id="gate-1"))
    result = await _project(db)
    assert (result["state"], result["reason"]) == ("awaiting_approval", "Approve release")


async def test_a_root_receipt_for_the_current_head_delivers(db):
    await _parent_tree(db)
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == "parent").values(status="COMPLETED")
        )
        checkpoint_sha = await conn.scalar(
            select(task_integration_checkpoints.c.checkpoint_sha)
            .where(task_integration_checkpoints.c.task_id == "parent")
        )
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="root-receipt", domain_key="root-parent", source_task_id="parent",
                target_task_id=None, repository_id="repo", target_branch="refs/heads/main",
                reviewed_head_sha="f" * 40, disposition="code", created_at=5.0,
            )
        )
    stale = await _project(db)
    assert (stale["state"], stale["evidence"]) == ("unknown", "stale")

    # Receipts are append-only: the delivery of the current head is a new row.
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="root-receipt-2", domain_key="root-parent-2", source_task_id="parent",
                target_task_id=None, repository_id="repo", target_branch="refs/heads/main",
                reviewed_head_sha=checkpoint_sha, disposition="code", created_at=6.0,
            )
        )
    delivered = await _project(db)
    assert (delivered["state"], delivered["label"]) == ("delivered", "Delivered")


async def test_a_missing_verifier_task_is_unavailable_evidence(db):
    await _collected(db)
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.parent_task_id == "parent")
            .values(verifier_task_id="verify-gone")
        )
    result = await _project(db)
    assert (result["state"], result["evidence"]) == ("unknown", "unavailable")


async def test_a_failed_read_never_raises(db, monkeypatch):
    await _parent_tree(db)

    async def boom(self, conn, ids):
        raise RuntimeError("database went away")

    monkeypatch.setattr(EpicDeliveryProjection, "facts_on", boom)
    assert await EpicDeliveryProjection(db).for_tasks(["parent"]) == {}


async def test_leaves_and_untracked_projects_get_no_epic_answer(db):
    await db.create_task(Task(id="leaf", project_id="p", title="leaf", description=""))
    assert await EpicDeliveryProjection(db).for_tasks(["leaf", "missing"]) == {}


async def test_get_task_reports_delivery_for_an_epic_only(db, tmp_path):
    await _parent_tree(db)
    orch = MagicMock()
    orch.db = db
    orch._emit_notify = AsyncMock()
    config = MagicMock()
    config.vault_root = str(tmp_path / "vault")
    handler = CommandHandler(orch, config)
    handler._active_project_id = None

    epic = GetTaskResponse.model_validate(await handler._cmd_get_task({"task_id": "parent"}))
    assert epic.status == "IN_PROGRESS"  # the stored lifecycle is untouched
    assert epic.delivery_status is not None
    assert epic.delivery_status.label == "Collecting child work"
    assert (epic.delivery_status.implementation_completed, epic.delivery_status.implementation_total) == (2, 2)

    leaf = GetTaskResponse.model_validate(await handler._cmd_get_task({"task_id": "parent.1"}))
    assert leaf.delivery_status is None


# ---------------------------------------------------------------------------
# The observer wiring: asked outside the read, identity rechecked after it
# ---------------------------------------------------------------------------


class GitStub:
    """The observer contract without git: the answer this test decided.

    ``during`` runs while the projection holds no read, which is where a real
    reopen, re-close or retarget lands: the identity the evidence names is
    then no longer the current one, exactly as a fetch racing a close would
    leave it.
    """

    READ_MAX_AGE = 30.0

    def __init__(self, db, evidence, *, during=None):
        self.db = db
        self.evidence = evidence
        self.during = during
        self.asked: list[list[str]] = []
        self.max_ages: list[float] = []

    async def observe(self, task_ids, *, max_age=0.0):
        ids = set(task_ids)
        self.asked.append(sorted(ids))
        self.max_ages.append(max_age)
        async with self.db._engine.connect() as conn:
            targets = await delivery_targets(conn, ids)
        if self.during is not None:
            await self.during()
        return DeliveryView(
            self.db,
            {task_id: self.evidence[task_id] for task_id in ids if task_id in self.evidence},
            targets,
            (),
        )


async def _plain_epic(db) -> None:
    """An epic with completed children, its own branch and no train behind it."""
    await db.create_repo(RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.LINK))
    async with db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            integration_repository_id="repo",
        ))
    await db.create_task(Task(
        id="parent", project_id="p", repo_id="repo", branch_name="aq/parent", title="parent",
        description="", status=TaskStatus.IN_PROGRESS,
    ))
    for index in (1, 2):
        await db.create_task(Task(
            id=f"parent.{index}", project_id="p", title=f"child {index}", description="",
            status=TaskStatus.IN_PROGRESS,
        ))
        await db.add_dependency(f"parent.{index}", "parent", "parent-child")
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id.in_(["parent.1", "parent.2"])).values(status="COMPLETED")
        )


async def _close_epic(db, generation: str, *, status: str = "COMPLETED") -> None:
    """Close the parent epic the way a worker or a publisher closes it."""
    await db.save_task_completion(TaskCompletion(
        id=generation, task_id="parent", outcome="pass", commits=[f"{generation[-1]}" * 40],
        completed_at=NOW,
    ))
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == "parent").values(status=status)
        )


async def _git_answer(db, *, state=DeliveryState.CONTAINED, reason="git_completion"):
    """The evidence the real identity reader would have produced, and its target."""
    async with db._engine.connect() as conn:
        targets = await delivery_targets(conn, ["parent"])
    target = targets["parent"]
    requests = await load_delivery_requests(
        db, ["parent"], repository_id=target.repository_id, target_ref=target.target_ref
    )
    return {
        "parent": DeliveryEvidence(
            requests["parent"], state, f"{'c' * 40}", requests["parent"].reported_source, reason,
        )
    }


async def test_the_projection_asks_git_for_completed_epics_and_rechecks_the_identity(db):
    await _plain_epic(db)
    await _close_epic(db, "close-parent-1")
    stub = GitStub(db, await _git_answer(db))

    result = (await EpicDeliveryProjection(db, delivery=stub).for_tasks(["parent"]))["parent"]

    assert (result["state"], result["label"]) == ("delivered", "Delivered")
    assert stub.asked == [["parent"]]  # only epics that could owe delivery
    assert stub.max_ages == [GitStub.READ_MAX_AGE]  # a read-only surface may reuse
    assert result["reason"] == (
        f"Completion close-parent-1 source {'1' * 12} is in main at {'c' * 12}"
    )


async def test_a_generation_that_changed_while_git_was_asked_is_never_delivered(db):
    await _plain_epic(db)
    await _close_epic(db, "close-parent-1")
    stub = GitStub(
        db, await _git_answer(db), during=lambda: _close_epic(db, "close-parent-2"),
    )

    result = (await EpicDeliveryProjection(db, delivery=stub).for_tasks(["parent"]))["parent"]

    assert (result["state"], result["evidence"]) == ("unknown", "stale")
    assert "changed after git was asked" in result["reason"]


async def test_a_failed_observation_keeps_the_train_reading(db):
    """An unreachable observer costs the canonical answer, nothing else."""
    await _collected(db)
    await _close_epic(db, "close-parent-1")
    await db.create_task(Task(
        id="root", project_id="p", title="root", description="", status=TaskStatus.IN_PROGRESS,
    ))
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == "parent").values(parent_task_id="root")
        )

    class Broken:
        async def observe(self, task_ids, *, max_age=0.0):
            raise RuntimeError("the observer could not be reached")

    result = (await EpicDeliveryProjection(db, delivery=Broken()).for_tasks(["parent"]))["parent"]
    assert (result["label"], result["evidence"]) == ("Waiting for parent collection", "current")


async def test_a_retargeted_project_is_not_delivered_through_the_old_answer(db):
    await _plain_epic(db)
    await _close_epic(db, "close-parent-1")

    async def retarget():
        await db.create_repo(RepoConfig(
            id="repo-2", project_id="p", source_type=RepoSourceType.LINK, default_branch="release",
        ))
        async with db.immediate() as conn:
            await conn.execute(
                update(projects).where(projects.c.id == "p").values(integration_repository_id="repo-2")
            )

    stub = GitStub(db, await _git_answer(db), during=retarget)
    result = (await EpicDeliveryProjection(db, delivery=stub).for_tasks(["parent"]))["parent"]
    assert (result["state"], result["evidence"]) == ("unknown", "stale")


async def test_a_recorded_operator_adoption_names_its_equivalence(db):
    await _plain_epic(db)
    await _close_epic(db, "close-parent-1")
    evidence = await _git_answer(db)
    source = evidence["parent"].source_oid
    async with db.immediate() as conn:
        await conn.execute(insert(events).values(
            event_type="development.operation", project_id="p", timestamp=NOW,
            payload=json.dumps({
                "id": "adoption-1", "state": "finished", "project_id": "p",
                "created_at": NOW, "expected_sha": "c" * 40, "prepared_sha": "c" * 40,
                "evidence": {"kind": "operator_accepted", "conclusion": "not_ci_attested"},
                "manifest": [{"task_id": "parent", "source_sha": source,
                              "acceptance": "operator_equivalent"}],
                "reason": "content landed rebased",
            }),
        ))
    result = (await EpicDeliveryProjection(db, delivery=GitStub(db, evidence)).for_tasks(["parent"]))[
        "parent"
    ]
    assert result["state"] == "delivered"
    assert result["reason"].endswith("(operator accepted this source as equivalent)")


# ---------------------------------------------------------------------------
# The live case: a verified parent epic, real Git, the daemon's observer
# ---------------------------------------------------------------------------


def _fresh_observer(db, tmp_path):
    """A request-scoped observer, so one read's fetch never answers the next."""
    return DeliveryObserver(db, git=GitManager(), data_dir=tmp_path / "data")


async def _epic_delivery(db, tmp_path, task_id: str = "parent") -> dict:
    projection = EpicDeliveryProjection(db, lease_ttl=TTL, delivery=_fresh_observer(db, tmp_path))
    return (await projection.for_tasks([task_id]))[task_id]


async def test_a_verified_parent_delivered_to_main_stops_waiting_for_a_train(
    git_completed_parent, tmp_path,
):
    """The reported bug: git proves the delivery, the card said otherwise."""
    service, origin, base, _partial, head, _checkpointed, _verified = git_completed_parent

    # Complete and train-verified with nothing on main: what git says, not
    # the old "Waiting for the next train" (which claimed the train owed it).
    waiting = await _epic_delivery(service.db, tmp_path)
    assert (waiting["state"], waiting["label"], waiting["evidence"]) == (
        "queued", "Delivery pending", "current",
    )

    origin.land("parent")
    main = git(origin.clone, "rev-parse", "main")
    delivered = await _epic_delivery(service.db, tmp_path)
    assert (delivered["state"], delivered["label"], delivered["display_status"]) == (
        "delivered", "Delivered", "Delivered",
    )
    # The exact identity git proved, not a train receipt: the verified
    # generation and the source that is on main.
    assert delivered["reason"] == (
        f"Verified generation 6 source {head[:12]} is in main at {main[:12]}"
    )
    assert delivered["links"] == []

    # A rewind of the target takes the work back out of main, so the same
    # epic is owed again instead of still reading as delivered.
    git(origin.clone, "push", "--force", "origin", f"{base}:main")
    rewound = await _epic_delivery(service.db, tmp_path)
    assert (rewound["state"], rewound["label"]) == ("queued", "Delivery pending")
    assert rewound["responsible"] == {
        "kind": "system", "id": None, "label": "Integration publisher",
    }


async def test_a_reopened_epic_never_reads_as_delivered(git_completed_parent, tmp_path):
    service, origin, _base, _partial, _head, *_rest = git_completed_parent
    origin.land("parent")
    assert (await _epic_delivery(service.db, tmp_path))["state"] == "delivered"

    async with service.db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == "parent").values(status="IN_PROGRESS")
        )
    reopened = await _epic_delivery(service.db, tmp_path)
    # A reopened epic is a new incarnation: its older verified generation
    # proves nothing for it, and the view says so instead of repeating the
    # delivery it already had.
    assert (reopened["state"], reopened["evidence"]) == ("unknown", "unavailable")


async def test_a_new_verified_generation_is_its_own_delivery_question(
    git_completed_parent, tmp_path,
):
    """An adoption of generation 6 says nothing about generation 7."""
    service, origin, _base, _partial, _head, _checkpointed, verified = git_completed_parent
    origin.land("parent")
    assert "generation 6" in (await _epic_delivery(service.db, tmp_path))["reason"]

    # Collection advanced after the delivery: a new episode verifies a new
    # head that no one has landed.
    later = origin.work("parent", "again")
    async with service.db.immediate() as conn:
        for table, values in (
            (integration_parent_episodes, {"id": "episode-next", "generation": 7}),
            (integration_repair_operations, {
                "id": "operation-next", "episode_id": "episode-next", "active_stage": 0,
            }),
            (integration_parent_verifications, {
                "id": "verification-next", "operation_id": "operation-next",
                "episode_id": "episode-next", "generation": 7, "head_sha": later,
            }),
            (integration_parent_operation_completions, {
                "operation_id": "operation-next", "verification_id": "verification-next",
                "episode_id": "episode-next",
            }),
        ):
            previous = (await conn.execute(select(table).limit(1))).mappings().one()
            await conn.execute(insert(table).values({**dict(previous), **values}))
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "parent")
            .values(
                generation=7, verified_generation=7, checkpoint_sha=later, verified_sha=later,
                episode_id="episode-next", current_verification_id="verification-next",
                last_completed_operation_id="operation-next",
                last_completed_verification_id="verification-next",
            )
        )
    assert verified["verification_id"] != "verification-next"

    advanced = await _epic_delivery(service.db, tmp_path)
    assert advanced["state"] == "queued"
    assert f"verified generation 7 source {later[:12]} is not in main" in advanced["reason"]


async def test_a_retargeted_repository_invalidates_the_observed_answer(
    git_completed_parent, tmp_path,
):
    service, origin, base, _partial, _head, *_rest = git_completed_parent
    origin.land("parent")
    assert (await _epic_delivery(service.db, tmp_path))["state"] == "delivered"

    # The project is retargeted at a branch that never received the work.
    git(origin.clone, "push", "origin", f"{base}:refs/heads/release")
    async with service.db.immediate() as conn:
        await conn.execute(update(repos).where(repos.c.id == "repo").values(
            default_branch="release",
        ))

    retargeted = await _epic_delivery(service.db, tmp_path)
    assert (retargeted["state"], retargeted["label"]) == ("queued", "Delivery pending")
    assert "is not in release as observed at" in retargeted["reason"]


async def test_a_real_operator_equivalence_adoption_is_delivered_and_says_so(
    git_completed_parent, tmp_path,
):
    """The recorded adoption the display must agree with, on a real adoption."""
    service, origin, _base, _partial, head, *_rest = git_completed_parent
    # The operator landed the aggregate by hand: main carries the content but
    # not the verified source, so git alone cannot prove the delivery.
    git(origin.clone, "checkout", "-B", "main", "origin/main")
    (origin.clone / "parent.txt").write_text("new\n")
    git(origin.clone, "add", "-A")
    git(origin.clone, "commit", "-qm", "operator supplied the aggregate")
    git(origin.clone, "push", "-q", "origin", "main")
    main = git(origin.clone, "rev-parse", "main")

    projection = EpicDeliveryProjection(service.db, lease_ttl=TTL)  # the daemon's observer
    unproven = (await projection.for_tasks(["parent"]))["parent"]
    assert (unproven["state"], unproven["label"]) == ("queued", "Delivery pending")
    assert f"source {head[:12]} is not in main as observed at {main[:12]} yet" in unproven["reason"]

    adopted = await service.adopt(
        project_id="p", task_ids=["parent"], target_ref="refs/heads/main", head_sha=main,
        reason="operator landed the aggregate by hand", operator_id="local",
        accept_equivalent=True,
    )
    assert adopted["manifest"] == [
        {"task_id": "parent", "source_sha": head, "acceptance": "operator_equivalent"},
    ]

    # A fresh observer, because the one above answered before the adoption
    # and a read-only surface may reuse that snapshot for its read window.
    delivered = await _epic_delivery(service.db, tmp_path)
    assert (delivered["state"], delivered["label"]) == ("delivered", "Delivered")
    assert delivered["reason"] == (
        f"Verified generation 6 source {head[:12]} is in main at {main[:12]} "
        "(operator accepted this source as equivalent)"
    )
    assert delivered["implementation_total"] == 1
