"""Exact identity, immutable answers and replay safety of integration human gates."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import (
    gates,
    integration_subject_journal,
    playbook_artifacts,
    projects,
    tasks,
)
from src.integration.gates import GatePrimitives
from src.integration.runtime_contracts import GateArgs, Subject
from tests.test_integration_subjects import ARTIFACT, NOW, OTHER, PIN, _subject


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("subject-records")
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


async def seed(db, **overrides):
    subject = _subject(**overrides)
    await db.ensure_integration_subject(subject.to_row())
    return subject


async def current(db, subject):
    return Subject.from_row(await db.get_integration_subject(subject.id))


async def rows(db, table):
    async with db._engine.connect() as conn:
        return [dict(row) for row in (await conn.execute(select(table))).mappings()]


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


async def test_concurrent_gate_answers_have_one_immutable_winner(db):
    subject, _, gate_id = await open_gate(db)
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


@pytest.mark.parametrize("move_head", [False, True])
async def test_ask_integration_decision_pins_current_head_and_journal(db, move_head):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from src.asks.models import SourceIdentity
    from src.commands.handler import CommandHandler
    from src.config import AppConfig
    subject, _, gate_id = await open_gate(db)
    handler = CommandHandler(SimpleNamespace(db=db, bus=SimpleNamespace(emit=AsyncMock())), AppConfig())
    source = SourceIdentity("p", "integration", gate_id, {"subject_id": subject.id, "gate_id": gate_id})
    ask = await handler._publish_source_ask(source)
    args = {"ask_id": ask["id"], "expected": {k: ask[k] for k in (
        "decision_generation", "source_fingerprint", "feedback_version")},
        "option_key": "approve", "idempotency_key": "integration-approve"}
    preview = await handler._cmd_ask_prepare_decision(args)
    assert preview["success"], preview
    if move_head:
        async with db.immediate() as conn:
            await db.update_integration_subject_on(conn, subject_id=subject.id,
                expected_version=subject.version, values={"head_sha": OTHER}, now=NOW + 1)
    args["preview_id"] = preview["preview"]["id"]
    result = await handler._cmd_ask_decide(args)
    answers = [row for row in await rows(db, integration_subject_journal) if row["outcome"] == "answered"]
    if move_head:
        assert result["error_code"] == "ask.stale_source", result
        assert answers == []
    else:
        assert result["success"], result
        replay = await handler._cmd_ask_decide(args)
        assert replay["success"] and replay["decision_id"] == result["decision_id"], replay
        assert len(answers) == 1
        assert answers[0]["payload"]["decision_id"] == result["decision_id"]
        assert answers[0]["payload"]["evidence_entry_id"]
