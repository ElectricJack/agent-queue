"""Exact identity, human decisions and replay safety of primitives 14–19."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.tables import (
    gates,
    integration_subject_journal,
    playbook_artifacts,
    projects,
    task_delivery_receipts,
    task_metadata,
    tasks,
)
from src.integration.gates import EjectionPlan, GatePrimitives
from src.integration.records import AttemptObservation, DeliveryProof, RecordsPrimitives
from src.integration.subjects import (
    EjectArgs,
    GateArgs,
    JournalMode,
    Primitive,
    PrimitivePorts,
    ReceiptKind,
    RecordAttemptArgs,
    RecordDecisionArgs,
    RecordReceiptArgs,
    Subject,
    SubjectKind,
    SubjectPhase,
    WaitArgs,
    WriterBudget,
    WriterLease,
    WriterStatus,
)
from tests.db_fixtures import lease_dsn
from tests.test_integration_subjects import ARTIFACT, HEAD, NOW, OTHER, PIN, _subject


@pytest.fixture
async def db():
    database = Database(lease_dsn("subject-records"))
    await database.initialize()
    async with database._engine.begin() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                artifact_sha256=ARTIFACT,
                playbook_id=PIN.playbook_id,
                source_digest="sha256:" + "c" * 64,
                contract_fingerprint="sha256:" + "d" * 64,
                compiler_build="test",
                path="/artifact.json",
                created_at=NOW,
            )
        )
        await conn.execute(insert(projects).values(id="p", name="Project", created_at=NOW))
        for task_id in ("member", "repair", "parent"):
            await conn.execute(
                insert(tasks).values(
                    id=task_id,
                    project_id="p",
                    title=task_id,
                    description="Fixture",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
    yield database
    await database.close()


async def seed(db, **overrides):
    subject = _subject(**overrides)
    await db.ensure_integration_subject(subject.to_row())
    return subject


async def current(db, subject):
    return Subject.from_row(await db.get_integration_subject(subject.id))


async def rows(db, table):
    async with db._engine.connect() as conn:
        return [dict(row) for row in (await conn.execute(select(table))).mappings()]


def budget_subject(**overrides):
    return {
        "budget": WriterBudget(
            ordinal=0,
            intelligence_class="standard-high",
            started_at=NOW - 10,
            deadline_at=NOW + 100,
            attempt_limit=3,
        ),
        "writer": WriterLease(status=WriterStatus.WORKING, task_id="repair", last_push_at=NOW - 1),
        **overrides,
    }


def attempt_args(subject, **overrides):
    return RecordAttemptArgs(
        head=subject.head, ordinal=0, evidence_id="ci-1", conclusion="red", **overrides
    )


def observation(subject, **overrides):
    return AttemptObservation(
        evidence_id="ci-1",
        head=subject.head,
        producer_id="trusted-ci",
        trusted=True,
        conclusion="red",
        classification="conclusive",
        writer_task_id="repair",
        published_at=NOW - 1,
        observed_at=NOW,
        **overrides,
    )


async def test_receipt_projects_exact_source_target_once_and_survives_restart(db):
    subject = await seed(db)
    args = RecordReceiptArgs(
        kind=ReceiptKind.CODE, source_task_id="member", source_head_sha=OTHER, target=subject.head
    )

    async def proof(conn, subject, request):
        return DeliveryProof(
            **{k: v for k, v in request.model_dump().items() if k != "primitive"},
            evidence={"ancestry": True},
        )

    ports = RecordsPrimitives(db, delivery_reader=proof, clock=lambda: NOW)
    assert (await ports.record_receipt(subject, args)).outcome == "recorded"
    # A replay after process restart needs no remote proof, only the immutable receipt.
    replay = await RecordsPrimitives(db).record_receipt(subject, args)
    assert replay.outcome == "exists"
    (receipt,) = await rows(db, task_delivery_receipts)
    assert receipt["reviewed_head_sha"] == OTHER and receipt["after_sha"] == HEAD
    assert len(await rows(db, integration_subject_journal)) == 1
    # The same source into another subject is a separate receipt identity.
    other = await seed(db, id="subject-2", subject_key="another", phase=SubjectPhase.TESTING)
    assert (await ports.record_receipt(other, args)).outcome == "recorded"
    assert len(await rows(db, task_delivery_receipts)) == 2


async def test_receipt_never_fabricates_delivery_from_args_or_wrong_head(db):
    subject = await seed(db)
    args = RecordReceiptArgs(
        kind="code", source_task_id="member", source_head_sha=OTHER, target=subject.head
    )
    records = RecordsPrimitives(db)
    assert (await records.record_receipt(subject, args)).reason == "delivery_proof_unavailable"
    wrong = args.model_copy(update={"target": subject.head.model_copy(update={"sha": OTHER})})
    assert (await records.record_receipt(subject, wrong)).reason == "target_identity_mismatch"
    assert await rows(db, task_delivery_receipts) == []


@pytest.mark.parametrize("kind", ["noop", "skipped"])
async def test_non_code_receipt_retains_disposition_proof(db, kind):
    subject = await seed(db)
    args = RecordReceiptArgs(
        kind=kind, source_task_id="member", source_head_sha=OTHER, target=subject.head
    )

    async def proof(conn, subject, request):
        return DeliveryProof(
            kind=request.kind,
            source_task_id="member",
            source_head_sha=OTHER,
            target=subject.head,
            evidence={
                "content_equivalent": True,
                "repair_task_id": "repair",
                "skip_authorized": True,
            },
        )

    assert (
        await RecordsPrimitives(db, delivery_reader=proof).record_receipt(subject, args)
    ).outcome == "recorded"
    (receipt,) = await rows(db, task_delivery_receipts)
    assert (
        receipt["disposition"] == kind
        and receipt["resolution_evidence"]["repair_task_id"] == "repair"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"trusted": False},
        {"classification": "infrastructure"},
        {"conclusion": "pending"},
        {"cancelled": True},
        {"superseded": True},
        {"writer_task_id": "other"},
        {"published_at": None},
        {"published_at": NOW + 1},
        {"observed_at": NOW - 2},
    ],
)
async def test_non_attempts_never_consume_budget(db, changes):
    subject = await seed(db, **budget_subject())
    evidence = observation(subject).model_copy(update=changes)

    async def read(conn, subject, args):
        return evidence

    result = await RecordsPrimitives(db, attempt_reader=read, clock=lambda: NOW).record_attempt(
        subject, attempt_args(subject)
    )
    assert result.outcome == "not_an_attempt"
    assert (await current(db, subject)).budget.attempts == 0
    assert await rows(db, integration_subject_journal) == []


async def test_concurrent_and_repeated_checks_count_once_per_published_head(db):
    subject = await seed(db, **budget_subject())

    async def read(conn, subject, args):
        return observation(subject).model_copy(update={"evidence_id": args.evidence_id})

    records = RecordsPrimitives(db, attempt_reader=read, clock=lambda: NOW)
    args = attempt_args(subject)
    results = await asyncio.gather(
        records.record_attempt(subject, args), records.record_attempt(subject, args)
    )
    assert sorted(result.outcome for result in results) == ["counted", "stale"]
    subject = await current(db, subject)
    replay = await records.record_attempt(subject, args.model_copy(update={"evidence_id": "ci-2"}))
    assert replay.outcome == "counted" and replay.detail["replayed"]
    assert (await current(db, subject)).budget.attempts == 1
    assert len(await rows(db, integration_subject_journal)) == 1
    # A moved head or a successor budget cannot inherit old evidence.
    async with db._engine.begin() as conn:
        await db.update_integration_subject_on(
            conn,
            subject_id=subject.id,
            expected_version=subject.version,
            values={"head_sha": OTHER},
            now=NOW,
        )
    moved = await current(db, subject)
    assert (await records.record_attempt(moved, args)).outcome == "stale"


async def test_missing_attempt_reader_is_a_non_attempt(db):
    subject = await seed(db, **budget_subject())
    assert (
        await RecordsPrimitives(db).record_attempt(subject, attempt_args(subject))
    ).outcome == "not_an_attempt"


async def test_policy_decisions_are_replay_safe_and_keep_the_pinned_artifact(db):
    subject = await seed(db)
    args = RecordDecisionArgs(
        rule="ci-pending",
        facts_digest="sha256:" + "4" * 64,
        decided=Primitive.WAIT,
        mode=JournalMode.SHADOW,
    )
    records = RecordsPrimitives(db, clock=lambda: NOW)
    first = await records.record_decision(subject, args)
    second = await records.record_decision(subject, args)
    assert first.detail["journal_seq"] == second.detail["journal_seq"]
    (row,) = await rows(db, integration_subject_journal)
    assert row["policy_artifact_sha256"] == ARTIFACT and row["mode"] == "shadow"
    assert row["facts_digest"] == args.facts_digest and row["primitive"] == "wait"


async def test_wait_is_bounded_and_cannot_erase_a_human_gate(db):
    subject = await seed(db)
    gate = GatePrimitives(db, clock=lambda: NOW)
    made = await gate.gate(
        subject, GateArgs(question="Ship?", choices=("yes", "hold"), no_default=True)
    )
    subject = await current(db, subject)
    waited = await gate.wait(subject, WaitArgs(seconds=999999, reason="human decision"))
    assert waited.outcome == "waiting"
    subject = await current(db, subject)
    assert subject.schedule.next_due_at == NOW + subject.schedule.max_wait_seconds
    assert subject.schedule.gate_id == made.detail["gate_id"]


async def open_gate(db, *, no_default=True):
    subject = await seed(db)
    args = GateArgs(
        question="Ship?",
        choices=("approve", "hold"),
        **(
            {"no_default": True}
            if no_default
            else {"default_choice": "hold", "default_after_seconds": 60}
        ),
    )
    ports = GatePrimitives(db, clock=lambda: NOW)
    result = await ports.gate(subject, args)
    return await current(db, subject), args, result.detail["gate_id"]


async def test_no_default_gate_survives_legacy_resolution_and_indefinite_time(db):
    subject, args, gate_id = await open_gate(db)
    async with db._engine.begin() as conn:
        await conn.execute(
            update(gates)
            .where(gates.c.id == gate_id)
            .values(status="resolved", resolution="approve")
        )
    ports = GatePrimitives(db, clock=lambda: NOW + 10**6)
    result = await ports.gate(subject, args)
    assert result.outcome == "reused" and result.detail["no_default"]
    held = await current(db, subject)
    assert held.schedule.gate_id == gate_id and held.schedule.next_due_at is None
    changed = args.model_copy(update={"question": "A different decision?"})
    assert (await ports.gate(held, changed)).reason == "existing_human_gate"
    assert (
        await ports.answer(
            held, gate_id, choice="approve", answered_by="agent", verified_human=False
        )
    ).reason == "verified_human_required"


async def test_verified_answer_is_immutable_and_restart_consumes_it_once(db):
    subject, args, gate_id = await open_gate(db)
    ports = GatePrimitives(db, clock=lambda: NOW + 1)
    first = await ports.answer(
        subject, gate_id, choice="approve", answered_by="user:1", verified_human=True
    )
    assert first.outcome == "answered"
    assert (
        await ports.answer(
            subject, gate_id, choice="hold", answered_by="user:1", verified_human=True
        )
    ).reason == "answer_immutable"
    consumed = await GatePrimitives(db, clock=lambda: NOW + 2).gate(subject, args)
    assert consumed.outcome == "answered" and consumed.detail["choice"] == "approve"
    assert (await current(db, subject)).schedule.gate_id is None
    new_gate = await ports.gate(await current(db, subject), args)
    assert new_gate.outcome == "created" and new_gate.detail["gate_id"] != gate_id


async def test_gate_answer_cannot_be_reused_for_a_moved_head_or_generation(db):
    subject, args, gate_id = await open_gate(db)
    ports = GatePrimitives(db, clock=lambda: NOW + 1)
    assert (
        await ports.answer(
            subject, gate_id, choice="approve", answered_by="user", verified_human=True
        )
    ).outcome == "answered"
    async with db._engine.begin() as conn:
        await db.update_integration_subject_on(
            conn,
            subject_id=subject.id,
            expected_version=subject.version,
            values={"head_sha": OTHER, "generation": 3},
            now=NOW + 2,
        )
    moved = await current(db, subject)
    assert (await ports.gate(moved, args)).reason == "gate_identity_mismatch"
    assert (
        await ports.answer(
            moved, gate_id, choice="approve", answered_by="user", verified_human=True
        )
    ).reason == "gate_identity_mismatch"
    assert (await current(db, subject)).schedule.gate_id == gate_id


async def test_expiry_never_reuses_a_human_approval_and_defaults_only_when_unanswered(db):
    subject, args, gate_id = await open_gate(db, no_default=False)
    late = GatePrimitives(db, clock=lambda: NOW + 60)
    assert (
        await late.answer(
            subject, gate_id, choice="approve", answered_by="user", verified_human=True
        )
    ).reason == "approval_expired"
    result = await late.gate(subject, args)
    assert (
        result.outcome == "answered"
        and result.detail["choice"] == "hold"
        and result.detail["defaulted"]
    )


async def test_early_approval_is_not_valid_after_its_timeout(db):
    subject, args, gate_id = await open_gate(db, no_default=False)
    early = GatePrimitives(db, clock=lambda: NOW + 1)
    await early.answer(subject, gate_id, choice="approve", answered_by="user", verified_human=True)
    assert (
        await GatePrimitives(db, clock=lambda: NOW + 61).gate(subject, args)
    ).reason == "approval_expired"
    assert (await current(db, subject)).schedule.gate_id == gate_id


async def test_answered_gate_cannot_bypass_a_product_hold(db):
    subject, args, gate_id = await open_gate(db)
    ports = GatePrimitives(db, clock=lambda: NOW + 1)
    await ports.answer(subject, gate_id, choice="approve", answered_by="user", verified_human=True)
    async with db._engine.begin() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(status="PAUSED"))
    assert (await ports.gate(subject, args)).reason == "project_inactive"
    assert (await current(db, subject)).schedule.gate_id == gate_id


def ejection_plan(**overrides):
    return EjectionPlan(
        member_task_id="member",
        source_head_sha=OTHER,
        policy_artifact_sha256=ARTIFACT,
        allowed=True,
        repair_task_id="repair",
        evidence={"rule": "exhausted", "membership": "parent-child"},
        **overrides,
    )


def ejector(db, plan=None, *, failing=False):
    async def read(conn, subject, args):
        marker = (
            await conn.execute(
                select(task_metadata.c.value).where(
                    task_metadata.c.task_id == "member", task_metadata.c.key == "test-ejection"
                )
            )
        ).scalar_one_or_none()
        return None if marker else (plan or ejection_plan())

    async def apply(conn, subject, args, plan):
        # Represents the kind adapter's membership + task-disposition projection.
        # Its failure must roll back BOTH this write and the audit, not half-eject.
        await conn.execute(
            insert(task_metadata).values(
                task_id="member", key="test-ejection", value=plan.model_dump_json()
            )
        )
        if failing:
            raise RuntimeError("projection failed")

    return GatePrimitives(db, ejection_reader=read, apply_ejection_on=apply, clock=lambda: NOW)


async def test_ejection_commits_disposition_and_repair_provenance_and_rebuilds(db):
    subject = await seed(
        db,
        kind=SubjectKind.PARENT_EPISODE,
        task_id="parent",
        subject_key="parent:episode",
        phase=SubjectPhase.REPAIRING,
    )
    ports = ejector(db)
    args = EjectArgs(member_task_id="member", reason="policy budget exhausted")
    result = await ports.eject(subject, args)
    assert result.outcome == "ejected" and result.detail["repair_task_id"] == "repair"
    subject = await current(db, subject)
    assert subject.phase is SubjectPhase.BUILDING and subject.generation == 3
    assert subject.schedule.next_due_at == NOW
    assert (await ports.eject(subject, args)).outcome == "not_a_member"
    (row,) = await rows(db, integration_subject_journal)
    assert row["payload"]["previous_head"]["sha"] == HEAD
    assert row["payload"]["disposition"] == "repair_required"
    member = [task for task in await rows(db, tasks) if task["id"] == "member"][0]
    assert member["status"] == "DEFINED"  # Required work was never silently completed.


async def test_ejection_projection_failure_rolls_back_all_changes(db):
    subject = await seed(db)
    with pytest.raises(RuntimeError, match="projection failed"):
        await ejector(db, failing=True).eject(
            subject, EjectArgs(member_task_id="member", reason="conflict")
        )
    assert await rows(db, integration_subject_journal) == []
    assert await rows(db, task_metadata) == []
    assert (await current(db, subject)).version == 0


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"allowed": False}, "ejection_not_authorized"),
        ({"policy_artifact_sha256": "sha256:" + "9" * 64}, "ejection_not_authorized"),
        ({"repair_task_id": "missing"}, "repair_provenance_invalid"),
        ({"disposition": "skipped"}, "required_work_skip_not_authorized"),
    ],
)
async def test_ejection_requires_policy_and_repair_and_cannot_implicitly_skip(db, change, reason):
    subject = await seed(db)
    plan = ejection_plan().model_copy(update=change)
    result = await ejector(db, plan).eject(
        subject, EjectArgs(member_task_id="member", reason="conflict")
    )
    assert result.reason == reason and await rows(db, integration_subject_journal) == []


async def test_ejection_preserves_manual_pause_and_live_writer(db):
    subject = await seed(db)
    ports = ejector(db)
    args = EjectArgs(member_task_id="member", reason="conflict")
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(task_metadata).values(task_id="member", key="manual_pause", value="{}")
        )
    assert (await ports.eject(subject, args)).reason == "manual_pause"
    async with db._engine.begin() as conn:
        await conn.execute(task_metadata.delete())
        await db.update_integration_subject_on(
            conn,
            subject_id=subject.id,
            expected_version=subject.version,
            values={"writer_status": "working", "writer_task_id": "repair"},
            now=NOW,
        )
    assert (
        await ports.eject(await current(db, subject), args)
    ).reason == "writer_stop_proof_required"


async def test_ports_register_only_the_six_primitives(db):
    ports = PrimitivePorts()
    RecordsPrimitives(db).bind(ports)
    GatePrimitives(db).bind(ports)
    assert ports.bound == {
        Primitive.RECORD_RECEIPT,
        Primitive.RECORD_ATTEMPT,
        Primitive.RECORD_DECISION,
        Primitive.WAIT,
        Primitive.GATE,
        Primitive.EJECT,
    }


async def test_new_generation_alone_does_not_count_the_same_head_again(db):
    subject = await seed(db, **budget_subject())

    async def read(conn, subject, args):
        return observation(subject)

    records = RecordsPrimitives(db, attempt_reader=read, clock=lambda: NOW)
    await records.record_attempt(subject, attempt_args(subject))
    subject = await current(db, subject)
    async with db._engine.begin() as conn:
        await db.update_integration_subject_on(
            conn,
            subject_id=subject.id,
            expected_version=subject.version,
            values={"generation": 3},
            now=NOW,
        )
    subject = await current(db, subject)
    replay = await records.record_attempt(subject, attempt_args(subject))
    assert replay.detail["replayed"] and (await current(db, subject)).budget.attempts == 1


async def test_concurrent_gate_answers_have_one_immutable_winner(db):
    subject, args, gate_id = await open_gate(db)
    ports = GatePrimitives(db, clock=lambda: NOW + 1)
    results = await asyncio.gather(
        *[
            ports.answer(subject, gate_id, choice=choice, answered_by="user", verified_human=True)
            for choice in ("approve", "hold")
        ]
    )
    assert sorted(result.outcome for result in results) == ["answered", "unknown"]
    assert [result.reason for result in results if result.is_unknown] == ["answer_immutable"]
    answers = [
        row for row in await rows(db, integration_subject_journal) if row["outcome"] == "answered"
    ]
    assert len(answers) == 1


async def test_receipt_projection_rolls_back_if_journal_cannot_commit(db, monkeypatch):
    subject = await seed(db)
    args = RecordReceiptArgs(
        kind="code", source_task_id="member", source_head_sha=OTHER, target=subject.head
    )

    async def proof(conn, subject, request):
        return DeliveryProof(
            kind="code",
            source_task_id="member",
            source_head_sha=OTHER,
            target=subject.head,
            evidence={"ancestry": True},
        )

    async def broken(*args, **kwargs):
        raise RuntimeError("journal unavailable")

    monkeypatch.setattr(db, "append_integration_subject_journal_on", broken)
    with pytest.raises(RuntimeError, match="journal unavailable"):
        await RecordsPrimitives(db, delivery_reader=proof).record_receipt(subject, args)
    assert await rows(db, task_delivery_receipts) == []


async def test_skipped_receipt_needs_explicit_authorization_and_live_repair(db):
    subject = await seed(db)
    args = RecordReceiptArgs(
        kind="skipped", source_task_id="member", source_head_sha=OTHER, target=subject.head
    )

    async def proof(conn, subject, request):
        return DeliveryProof(
            kind="skipped",
            source_task_id="member",
            source_head_sha=OTHER,
            target=subject.head,
            evidence={"repair_task_id": "repair"},
        )

    result = await RecordsPrimitives(db, delivery_reader=proof).record_receipt(subject, args)
    assert result.reason == "skip_requires_repair_provenance"
    assert await rows(db, task_delivery_receipts) == []


async def test_explicit_policy_skip_keeps_the_member_repair_link(db):
    subject = await seed(db)
    plan = ejection_plan(disposition="skipped", allow_required_skip=True)
    result = await ejector(db, plan).eject(
        subject, EjectArgs(member_task_id="member", reason="policy skip")
    )
    assert result.outcome == "ejected" and result.detail["disposition"] == "skipped"
    links = [
        row
        for row in await rows(db, task_metadata)
        if row["key"].startswith("integration_ejection:")
    ]
    assert len(links) == 1 and '"repair_task_id": "repair"' in links[0]["value"]
