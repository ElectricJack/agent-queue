"""Parent subjects retain exact lineage through failures, lost events and reopen."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, insert, update

from src.database import tables as t
from src.integration.failed_verification_recovery import FailedVerificationRecovery
from src.integration.parent_subjects import (
    ParentDatabaseObservationReader,
    ParentIntegrationObserver,
    ParentSubjectAdapter,
    ParentSubjectFacts,
    parent_subject_from_rows,
)
from src.integration.subjects import (
    CIState,
    ObserveSubjectArgs,
    PolicyArtifactPin,
    Primitive,
    PrimitivePorts,
    Subject,
)
from src.models import Project, Task, TaskStatus
from tests.test_integration_cancelled_collection import (  # noqa: F401
    _failed_aggregate,
    _policy,
    _promote_next,
    _rows,
    case,
)
from tests.test_integration_parent_completion import _artifact, _code_receipt, _parent_tree

BASE, SOURCE, FIRST, HEAD = (char * 40 for char in "abcd")
PIN = PolicyArtifactPin(
    playbook_id=_artifact().playbook_id, artifact_sha256=_artifact().artifact_sha256
)


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("parent-subjects")
    await database.create_project(Project(id="p", name="parent subjects"))
    return database


async def bridge(db, task_id="parent", pin=PIN):
    async with db.immediate() as conn:
        return await ParentSubjectAdapter(db, clock=lambda: 1000).ensure_on(
            conn, task_id, policy=pin, max_wait_seconds=300
        )


def observer(db):
    return ParentIntegrationObserver(ParentDatabaseObservationReader(db), clock=lambda: 1000)


async def test_eight_children_failure_conflict_noop_and_skipped_without_events(db):
    hierarchy, _, children = await _parent_tree(db, children=8)
    await _code_receipt(db, children[0], BASE, FIRST)
    await _code_receipt(db, children[1], FIRST, HEAD)
    async with db.immediate() as conn:
        await conn.execute(
            update(t.tasks).where(t.tasks.c.id == children[2]).values(status="FAILED")
        )
        await conn.execute(
            update(t.task_integration_checkpoints)
            .where(t.task_integration_checkpoints.c.task_id == "parent")
            .values(checkpoint_sha=HEAD)
        )
        await conn.execute(
            insert(t.integration_promotion_intents).values(
                id="conflict",
                domain_key="conflict",
                receipt_id="unwritten",
                project_id="p",
                source_task_id=children[5],
                target_task_id="parent",
                repository_id="repo",
                target_branch="aq/parent",
                source_head=SOURCE,
                source_base=BASE,
                expected_target=HEAD,
                fence_owner_id="collector",
                fence_token=1,
                state="conflict",
                conflict_diagnostics={"files": ["shared.py"]},
                created_at=20,
                updated_at=20,
            )
        )
    for child, kind in ((children[3], "skipped"), (children[4], "noop")):
        await hierarchy.parent_completion.record_disposition(
            child,
            disposition=kind,
            reviewed_head_sha=SOURCE,
            reviewed_tree_sha=FIRST,
            verification_evidence={"verified": True},
            resolution_evidence={"reason": "fixture"},
        )
    subject, created = await bridge(db)
    assert created and subject.engine == "reconciler"
    before = await _rows(db, t.task_delivery_receipts)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db._engine.sync_engine, "before_cursor_execute", record)
    try:
        # No wake/event delivery is performed: the next due visit reads durable state.
        facts = await observer(db).observe(subject)
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", record)
    assert isinstance(facts, ParentSubjectFacts) and len(facts.children) == 8
    assert facts.readiness == "failed" and facts.binding()["failed_child_count"] == 1
    assert facts.conflicts[0].member_task_id == children[5]
    assert facts.conflicts[0].files == ("shared.py",)
    assert {row.kind for row in facts.receipts} == {"code", "noop", "skipped"}
    assert all(row.scope == "direct" and row.selected for row in facts.receipts)
    assert facts.children[2].blockers == ("failed_child",)
    assert facts.children[3].selected_receipt_id and facts.members[3].ejected
    assert facts.binding()["pending_child_count"] == 3
    assert await _rows(db, t.task_delivery_receipts) == before
    assert "SET TRANSACTION READ ONLY" in statements
    assert all(
        sql.lstrip().startswith(("SELECT", "SET TRANSACTION READ ONLY")) for sql in statements
    )
    assert (await db.get_integration_subject(subject.id))["next_due_at"] == 1000


async def test_bridge_replay_pins_policy_schedule_and_episode_after_generation_moves(db):
    await _parent_tree(db, children=1)
    subject, _ = await bridge(db)
    async with db.immediate() as conn:
        await conn.execute(
            update(t.task_integration_checkpoints)
            .where(t.task_integration_checkpoints.c.task_id == "parent")
            .values(generation=t.task_integration_checkpoints.c.generation + 1)
        )
    replay, created = await bridge(db)
    assert not created and replay == subject
    facts = await observer(db).observe(subject)
    assert facts.parent_operation_id and facts.collection_generation_advanced
    assert not facts.collection_reopened
    assert "parent_checkpoint_moved" in facts.unknown and facts.readiness == "unknown"


async def test_changed_child_head_and_rework_do_not_reuse_receipt(db):
    await _parent_tree(db, children=1)
    await _code_receipt(db, "parent.1", BASE, HEAD)
    subject, _ = await bridge(db)
    async with db.immediate() as conn:
        await conn.execute(
            update(t.task_integration_checkpoints)
            .where(t.task_integration_checkpoints.c.task_id == "parent.1")
            .values(checkpoint_sha=FIRST)
        )
    facts = await observer(db).observe(subject)
    assert not facts.receipts[0].selected and facts.receipts[0].source_head_sha == SOURCE
    assert facts.children[0].head_sha == FIRST and facts.children[0].pending_collection


async def test_historic_episode_receipts_are_retained_without_new_acceptance(db):
    await _parent_tree(db, children=1)
    await _code_receipt(db, "parent.1", BASE, HEAD)
    subject, _ = await bridge(db)
    data = await ParentDatabaseObservationReader(db).read(subject.id)
    rows = deepcopy(dict(data.rows))
    receipt = {
        **rows["task_delivery_receipts"][0],
        "parent_episode_id": "old",
        "parent_operation_id": "old-op",
    }
    rows["task_delivery_receipts"] = (receipt,)
    rows["parent_readiness"] = ({"outcome": "waiting", "receipts": [], "blockers": []},)
    reader = AsyncMock()
    reader.read.return_value = replace(data, rows=rows)
    facts = await ParentIntegrationObserver(reader, clock=lambda: 1000).observe(subject)
    assert facts.receipts[0].scope == "history" and not facts.receipts[0].selected
    assert facts.receipts[0].episode_id == "old" and facts.receipts[0].target_head_sha == HEAD
    reader.read.assert_awaited_once_with(subject.id)


async def test_parent_port_and_future_policy_extension_share_the_facts_contract(db):
    await _parent_tree(db, children=1)
    subject, _ = await bridge(db)
    ports = PrimitivePorts({Primitive.OBSERVE_SUBJECT: observer(db)})
    result = await ports.invoke(subject, ObserveSubjectArgs(include_remote=False))
    assert result.outcome == "observed"
    facts = ParentSubjectFacts.model_validate(result.detail["facts"])
    assert facts.parent_episode_id == subject.parent_episode_id and facts.children


@pytest.mark.parametrize("stale", [None, "head", "generation", "operation", "check_version"])
async def test_parent_verification_and_ci_bind_exact_episode_head_generation(db, stale):
    await _parent_tree(db, children=1)
    subject, _ = await bridge(db)
    [operation] = await _rows(db, t.integration_repair_operations)
    async with db.immediate() as conn:
        await conn.execute(
            insert(t.integration_parent_verifications).values(
                id="verify",
                operation_id=operation["id"],
                parent_task_id="parent",
                episode_id=subject.parent_episode_id,
                generation=subject.generation,
                head_sha=subject.head_sha,
                required_check_version=operation["required_check_version"],
                created_at=5,
            )
        )
        await conn.execute(
            update(t.task_integration_checkpoints)
            .where(t.task_integration_checkpoints.c.task_id == "parent")
            .values(current_verification_id="verify", state="verifying")
        )
        evidence = {
            "id": "green",
            "operation_id": operation["id"],
            "parent_task_id": "parent",
            "parent_generation": subject.generation,
            "parent_head_sha": subject.head_sha,
            "producer_id": "forge-observer",
            "workflow_id": "workflow",
            "run_id": "run",
            "attempt": 1,
            "required_check_version": operation["required_check_version"],
            "checks": {"unit": "success"},
            "conclusion": "success",
            "classification": "conclusive",
            "observed_at": 10,
        }
        if stale == "head":
            evidence["parent_head_sha"] = HEAD
        elif stale == "generation":
            evidence["parent_generation"] += 1
        elif stale == "operation":
            evidence["operation_id"] = "other"
        elif stale == "check_version":
            evidence["required_check_version"] = "other"
        await conn.execute(insert(t.integration_check_evidence).values(**evidence))
    facts = await observer(db).observe(subject)
    assert facts.verification.verification_id == "verify" and facts.verification.status == "pending"
    assert facts.verification.head_sha == subject.head_sha
    assert facts.ci_state == (
        CIState.GREEN
        if stale is None
        else CIState.UNTRUSTED
        if stale == "check_version"
        else CIState.NONE
    )


async def test_failed_verification_recovery_keeps_receipts_and_observes_new_fix(case):  # noqa: F811
    red_head, old_verifier = await _failed_aggregate(case)
    # Keep the recovery scenario at eight children too: a failed child and
    # four still waiting, alongside the two receipts and the completed fix.
    [origin] = await _rows(
        case.db, t.task_branch_origins, t.task_branch_origins.c.task_id == "epic.3"
    )
    [child_checkpoint] = await _rows(
        case.db,
        t.task_integration_checkpoints,
        t.task_integration_checkpoints.c.task_id == "epic.3",
    )
    for index in range(4, 9):
        task_id = f"epic.{index}"
        branch = f"aq/epic.{index}"
        await case.db.create_task(
            Task(
                id=task_id,
                project_id="p",
                parent_task_id="epic",
                repo_id="repo",
                branch_name=branch,
                title="additional child",
                description="fixture",
                status=TaskStatus.FAILED if index == 4 else TaskStatus.READY,
            )
        )
        async with case.db.immediate() as conn:
            await conn.execute(
                insert(t.task_branch_origins).values(
                    **{**origin, "id": f"origin-{index}", "task_id": task_id, "branch_name": branch}
                )
            )
            await conn.execute(
                insert(t.task_integration_checkpoints).values(
                    **{
                        **child_checkpoint,
                        "task_id": task_id,
                        "branch": branch,
                        "checkpoint_sha": case.base,
                    }
                )
            )
    _, artifact = _policy()
    pin = PolicyArtifactPin(
        playbook_id=artifact.playbook_id, artifact_sha256=artifact.artifact_sha256
    )
    subject, _ = await bridge(case.db, "epic", pin)
    old = await observer(case.db).observe(subject)
    assert len(old.children) == 8 and old.binding()["failed_child_count"] == 1
    assert old.verification.status == "failed" and old.verification.head_sha == red_head
    assert old.writer.task_id == old_verifier
    old_receipts = await _rows(case.db, t.task_delivery_receipts)
    from src.integration.parent_engine import ParentEngineOwnership

    async with ParentEngineOwnership(case.db).operation("epic", subject=subject):
        applied = await FailedVerificationRecovery(case.db, case.promotion).run(
            "epic",
            dry_run=False,
            expected_head_sha=red_head,
            reason="collect completed fix",
            operator_id="fixture",
        )
    assert applied["outcome"] == "reopened"
    checkpoint = await case.db.get_integration_checkpoint("epic")
    # The reconciler owns this versioned head refresh; the observation never writes it.
    subject = subject.model_copy(update={"generation": checkpoint["generation"]})
    async with case.db.immediate() as conn:
        await case.db.update_integration_subject_on(
            conn,
            subject_id=subject.id,
            expected_version=0,
            values={"generation": subject.generation},
            now=1001,
        )
    reopened = await observer(case.db).observe_subject(subject.id)
    assert reopened.collection_reopened and reopened.failed_aggregate_unchanged
    assert reopened.failed_aggregate_head_sha == red_head
    assert reopened.verification is None
    assert reopened.verification_history[-1].verifier_task_id == old_verifier
    assert reopened.verification_history[-1].completion_id == "failed-completion"
    assert next(
        child for child in reopened.children if child.task_id == "epic.3"
    ).pending_collection
    assert await _rows(case.db, t.task_delivery_receipts) == old_receipts
    subject = Subject.from_row(await case.db.get_integration_subject(subject.id))
    async with ParentEngineOwnership(case.db).operation("epic", subject=subject):
        assert await _promote_next(case, 30) is not None
    facts = await observer(case.db).observe_subject(subject.id)
    assert "parent_collection_head_moved" in facts.unknown
    assert facts.failed_aggregate_head_sha == red_head
    assert not facts.failed_aggregate_unchanged
    assert [
        (r.receipt_id, r.episode_id, r.source_head_sha, r.target_head_sha)
        for r in facts.receipts[:2]
    ] == [
        (r.receipt_id, r.episode_id, r.source_head_sha, r.target_head_sha)
        for r in reopened.receipts
    ]


@pytest.mark.parametrize("changed", ["repository", "episode", "generation", "branch", "operation"])
def test_bridge_rejects_mismatched_parent_identity(changed):
    parent = {"id": "parent", "project_id": "p", "repo_id": "repo", "branch_name": "aq/parent"}
    checkpoint = {
        "task_id": "parent",
        "repository_id": "repo",
        "branch": "aq/parent",
        "episode_id": "episode",
        "generation": 3,
        "checkpoint_sha": HEAD,
        "state": "awaiting_children",
    }
    episode = {
        "id": "episode",
        "parent_task_id": "parent",
        "repository_id": "repo",
        "generation": 2,
        "pre_collection_checkpoint_sha": BASE,
    }
    operation = {"parent_task_id": "parent", "episode_id": "episode", "target_kind": "parent"}
    if changed == "repository":
        episode["repository_id"] = "other"
    elif changed == "episode":
        checkpoint["episode_id"] = "other"
    elif changed == "generation":
        checkpoint["generation"] = 1
    elif changed == "branch":
        parent["branch_name"] = "aq/other"
    else:
        operation["parent_task_id"] = "other"
    with pytest.raises(ValueError, match="identity mismatch"):
        parent_subject_from_rows(
            parent, checkpoint, episode, operation, policy=PIN, now=1000, max_wait_seconds=300
        )


@pytest.mark.parametrize("dry_run", [False, True])
async def test_parent_repair_head_recovery_refuses_reconciler_owned_parent(db, tmp_path, dry_run):
    from src.database.tables import integration_repair_stages, task_integration_checkpoints
    from sqlalchemy import select
    from tests.test_integration_parent_completion import _parent_repair_case

    recovery, _, request, _, _, _ = await _parent_repair_case(db, tmp_path)
    subject, _ = await bridge(db)
    async with db.immediate() as conn:
        await conn.execute(
            update(t.integration_subjects)
            .where(t.integration_subjects.c.id == subject.id)
            .values(engine="reconciler")
        )
        before = dict(
            (
                await conn.execute(
                    select(task_integration_checkpoints).where(
                        task_integration_checkpoints.c.task_id == "parent"
                    )
                )
            )
            .mappings()
            .one()
        )
        stage_before = dict(
            (
                await conn.execute(
                    select(integration_repair_stages).where(
                        integration_repair_stages.c.operation_id == request.operation_id
                    )
                )
            )
            .mappings()
            .one()
        )
    request.dry_run = dry_run
    result = await recovery.run(request, principal="operator:test")
    assert result["outcome"] == "blocked", result
    assert "reconciler" in result["reason"]
    async with db.immediate() as conn:
        assert (
            dict(
                (
                    await conn.execute(
                        select(task_integration_checkpoints).where(
                            task_integration_checkpoints.c.task_id == "parent"
                        )
                    )
                )
                .mappings()
                .one()
            )
            == before
        )
        assert (
            dict(
                (
                    await conn.execute(
                        select(integration_repair_stages).where(
                            integration_repair_stages.c.operation_id == request.operation_id
                        )
                    )
                )
                .mappings()
                .one()
            )
            == stage_before
        )
