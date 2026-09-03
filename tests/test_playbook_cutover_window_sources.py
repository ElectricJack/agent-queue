"""Package 7 commit 3 — the durable evidence sources behind the window.

Two halves.  The aggregate queries the window status reads over
``[switched_at, now]``, run against a real database (SQLite always, Postgres
when ``POSTGRES_TEST_DSN`` is set); and the three instrumentation points that
make the evidence exist at all: the V2 dispatch entry stamps the event's
arrival time so dispatch latency survives in the snapshot, a capability denial
writes a durable ``capability.denied`` event, and a snapshot-version conflict
writes a durable ``playbook.snapshot_conflict`` event.  An in-memory counter
would reset on the first daemon restart of a 72-hour window, which is why
every one of these lands in a table.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import insert

from src.database import Database
from src.database.tables import playbook_artifacts, playbook_pending_events, playbook_waits
from src.playbooks.receipts import StepReceipt
from src.playbooks.run_state import RunSnapshot, SnapshotVersionConflict
from tests.pg_dsn import ensure_worker_postgres_dsn

POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()
ARTIFACT = "sha256:" + "5d" * 32
OTHER_ARTIFACT = "sha256:" + "6e" * 32
NOW = 1_700_000_000.0
SINCE = NOW - 3600.0


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request, tmp_path):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        database = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
        await database.initialize()
        await database.reset_for_tests()
    else:
        database = Database(str(tmp_path / "test.db"))
        await database.initialize()
    for digest, playbook_id in ((ARTIFACT, "default-pipeline"), (OTHER_ARTIFACT, "coding-reflection")):
        await _seed_artifact(database, digest, playbook_id)
    yield database
    await database.close()


async def _seed_artifact(database, digest: str, playbook_id: str) -> None:
    async with database.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                artifact_sha256=digest,
                playbook_id=playbook_id,
                scope="system",
                scope_identifier="",
                schema_generation=2,
                version=1,
                source_digest="sha256:" + "2a" * 32,
                contract_fingerprint="sha256:" + "3b" * 32,
                profile_fingerprint="",
                compiler_build="test-build",
                path=f"artifacts/{digest.split(':', 1)[1]}.json",
                size_bytes=10,
                validation="{}",
                compiled_at=None,
                created_at=NOW,
            )
        )


def _snapshot(run_id: str, *, started_at: float, received_at: float | None = None,
              playbook_id: str = "default-pipeline", artifact: str = ARTIFACT) -> RunSnapshot:
    event = {"event_id": run_id, "type": "task.completed"}
    if received_at is not None:
        event["_received_at"] = received_at
    return RunSnapshot(
        run_id=run_id,
        playbook_id=playbook_id,
        artifact_sha256=artifact,
        rule_id="r",
        event=event,
        event_type="task.completed",
        event_id=run_id,
        dispatch_id=run_id,
        started_at=started_at,
        updated_at=started_at,
    )


def _receipt(snapshot: RunSnapshot, step_id: str, *, step_kind: str = "command",
             outcome: str = "success", error_code: str | None = None,
             started_at: float = NOW) -> StepReceipt:
    return StepReceipt(
        receipt_id=uuid.uuid4().hex,
        run_id=snapshot.run_id,
        artifact_sha256=snapshot.artifact_sha256,
        rule_id=snapshot.rule_id,
        step_id=step_id,
        step_kind=step_kind,
        outcome=outcome,
        error_code=error_code,
        started_at=started_at,
        snapshot_version=snapshot.version + 1,
        completed_at=started_at + 1.0,
    )


async def _run_with_receipts(db, run_id: str, *, started_at: float,
                             received_at: float | None = None,
                             receipts: list[tuple[str, str, str | None]] = (),
                             playbook_id: str = "default-pipeline",
                             artifact: str = ARTIFACT) -> RunSnapshot:
    snapshot = await db.create_run(
        _snapshot(run_id, started_at=started_at, received_at=received_at,
                  playbook_id=playbook_id, artifact=artifact)
    )
    for index, (step_kind, outcome, error_code) in enumerate(receipts):
        receipt = _receipt(
            snapshot, f"s{index}", step_kind=step_kind, outcome=outcome,
            error_code=error_code, started_at=started_at + index,
        )
        snapshot = await db.commit_boundary(snapshot, receipt)
    return snapshot


# ---------------------------------------------------------------------------
# Aggregate queries
# ---------------------------------------------------------------------------


async def test_count_v2_runs_by_playbook_is_bounded_by_the_switch(db):
    await _run_with_receipts(db, "before", started_at=SINCE - 1)
    await _run_with_receipts(db, "at", started_at=SINCE)
    await _run_with_receipts(db, "after", started_at=SINCE + 10)
    await _run_with_receipts(
        db, "other", started_at=SINCE + 20, playbook_id="coding-reflection",
        artifact=OTHER_ARTIFACT,
    )

    counts = await db.count_v2_runs_by_playbook(SINCE)

    assert counts == {"default-pipeline": 2, "coding-reflection": 1}


async def test_dispatch_latencies_come_from_the_stamped_event_arrival(db):
    await _run_with_receipts(db, "stamped", started_at=SINCE + 10.4, received_at=SINCE + 10.0)
    await _run_with_receipts(db, "stamped-2", started_at=SINCE + 21.0, received_at=SINCE + 20.0)
    await _run_with_receipts(db, "unstamped", started_at=SINCE + 30.0)
    await _run_with_receipts(db, "old", started_at=SINCE - 5.0, received_at=SINCE - 6.0)

    latencies = await db.v2_dispatch_latencies_ms(SINCE)

    assert sorted(round(value) for value in latencies) == [400, 1000]


async def test_wait_resume_latencies_join_the_claim_to_the_causing_event(db):
    snapshot = await _run_with_receipts(
        db, "waiting", started_at=SINCE + 1, receipts=[("command", "success", None)]
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(playbook_pending_events).values(
                pending_event_id="pe-1",
                playbook_id="default-pipeline",
                scope="system",
                scope_identifier="",
                event_type="gate.resolved",
                event="{}",
                event_id="evt-cause",
                dedup_key="",
                reason="wait_registration",
                attempts=0,
                received_at=SINCE + 100.0,
                expires_at=SINCE + 100_000.0,
            )
        )
        await conn.execute(
            insert(playbook_waits).values(
                wait_id="w-1",
                run_id=snapshot.run_id,
                step_id="s0",
                iteration=-1,
                kind="event",
                event_type="gate.resolved",
                correlation_key="",
                match="{}",
                deadline_at=None,
                snapshot_version=1,
                state="claimed",
                claimed_event_id="evt-cause",
                claimed_at=SINCE + 101.5,
                created_at=SINCE + 2.0,
            )
        )
        # A timer wait has no causing event and contributes no sample.
        await conn.execute(
            insert(playbook_waits).values(
                wait_id="w-timer",
                run_id=snapshot.run_id,
                step_id="s1",
                iteration=-1,
                kind="timer",
                event_type="",
                correlation_key="",
                match="{}",
                deadline_at=SINCE + 50.0,
                snapshot_version=1,
                state="claimed",
                claimed_event_id=None,
                claimed_at=SINCE + 50.0,
                created_at=SINCE + 3.0,
            )
        )

    latencies = await db.wait_resume_latencies_ms(SINCE)

    assert [round(value) for value in latencies] == [1500]


async def test_step_receipt_counts_group_by_kind_outcome_and_error_code(db):
    await _run_with_receipts(
        db, "llm-run", started_at=SINCE + 1,
        receipts=[("llm", "success", None), ("llm", "failure", "budget_exceeded"),
                  ("llm", "failure", "invalid_output"), ("agent_task", "cancelled", "cancelled")],
    )
    await _run_with_receipts(
        db, "old-run", started_at=SINCE - 100, receipts=[("llm", "failure", "budget_exceeded")]
    )

    rows = await db.count_step_receipts_since(SINCE)

    as_tuples = sorted(
        (row["step_kind"], row["receipt_kind"], row["outcome"], row["error_code"], row["count"])
        for row in rows
    )
    assert as_tuples == [
        ("agent_task", "step", "cancelled", "cancelled", 1),
        ("llm", "step", "failure", "budget_exceeded", 1),
        ("llm", "step", "failure", "invalid_output", 1),
        ("llm", "step", "success", None, 1),
    ]


async def test_agent_task_cancellations_are_listed_by_run(db):
    await _run_with_receipts(
        db, "cancelled-run", started_at=SINCE + 1,
        receipts=[("agent_task", "cancelled", "cancelled"), ("agent_task", "success", None)],
    )
    await _run_with_receipts(
        db, "old-cancel", started_at=SINCE - 100, receipts=[("agent_task", "cancelled", "cancelled")]
    )

    rows = await db.agent_task_cancellations_since(SINCE)

    assert [(row["run_id"], row["step_id"]) for row in rows] == [("cancelled-run", "s0")]


async def test_agent_task_wait_orphans_are_past_twice_their_timeout(db):
    snapshot = await _run_with_receipts(db, "agent-run", started_at=SINCE + 1)
    rows = []
    for wait_id, created, deadline, state in (
        ("orphan", NOW - 1300.0, NOW - 700.0, "active"),   # 600s timeout, 1300s old
        ("young", NOW - 700.0, NOW - 100.0, "active"),     # 600s timeout, 700s old
        ("claimed", NOW - 1300.0, NOW - 700.0, "claimed"),  # finished
        ("untimed", NOW - 99_999.0, None, "active"),        # no timeout to double
    ):
        rows.append(
            {"wait_id": wait_id, "run_id": snapshot.run_id, "step_id": wait_id,
             "iteration": -1, "kind": "agent_task", "event_type": "task.completed",
             "correlation_key": "", "match": "{}", "deadline_at": deadline,
             "snapshot_version": 1, "state": state, "claimed_event_id": None,
             "claimed_at": None, "created_at": created}
        )
    async with db.immediate() as conn:
        for row in rows:
            await conn.execute(insert(playbook_waits).values(**row))

    orphans = await db.agent_task_wait_orphans(NOW)

    assert [row["step_id"] for row in orphans] == ["orphan"]
    assert orphans[0]["run_id"] == snapshot.run_id
    assert orphans[0]["deadline_at"] == NOW - 700.0


async def test_pending_event_summary_counts_operator_visible_rows_only(db):
    base = {"scope": "system", "scope_identifier": "", "event_type": "task.completed",
            "event": "{}", "dedup_key": "", "attempts": 0, "expires_at": NOW + 100_000.0}
    async with db.immediate() as conn:
        for pending_id, reason, received, resolved in (
            ("p-old", "disabled", NOW - 5000.0, None),
            ("p-new", "stale_contract", NOW - 10.0, None),
            ("p-done", "disabled", NOW - 9000.0, NOW - 1.0),
            ("p-inbox", "wait_registration", NOW - 90_000.0, None),
        ):
            await conn.execute(
                insert(playbook_pending_events).values(
                    pending_event_id=pending_id, playbook_id="default-pipeline",
                    reason=reason, received_at=received, resolved_at=resolved, **base,
                )
            )

    summary = await db.pending_event_summary(
        reasons=["stale_contract", "invalid_artifact", "disabled", "unavailable",
                 "question_required"]
    )

    assert summary == {"count": 2, "oldest_received_at": NOW - 5000.0}


async def test_pending_event_summary_on_an_empty_table(db):
    assert await db.pending_event_summary(reasons=["disabled"]) == {
        "count": 0, "oldest_received_at": None
    }


# ---------------------------------------------------------------------------
# Instrumentation
# ---------------------------------------------------------------------------


async def test_snapshot_conflict_is_recorded_durably(db):
    snapshot = await _run_with_receipts(db, "conflict", started_at=SINCE + 1)
    stale = snapshot  # version 0, about to be overtaken
    await db.commit_boundary(snapshot, _receipt(snapshot, "first"))

    with pytest.raises(SnapshotVersionConflict):
        await db.commit_boundary(stale, _receipt(stale, "second"))

    events = await db.get_recent_events(event_type="playbook.snapshot_conflict", since=SINCE)
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["run_id"] == "conflict"
    assert payload["expected"] == 0
    assert payload["actual"] == 1
    assert payload["playbook_id"] == "default-pipeline"


async def test_v2_dispatch_entry_stamps_the_events_arrival_time():
    from src.orchestrator.core import Orchestrator

    engine = MagicMock()
    engine.dispatch_event = AsyncMock()
    orchestrator = SimpleNamespace(
        config=SimpleNamespace(playbooks=SimpleNamespace(enabled=True, v2_engine=True)),
        db=MagicMock(),
        _command_handler=MagicMock(),
        llm=MagicMock(),
        bus=MagicMock(),
    )
    event = {"type": "task.completed", "event_id": "event-1"}
    with patch("src.playbooks.services.build_v2_engine", return_value=engine):
        await Orchestrator._on_playbook_trigger(
            orchestrator, SimpleNamespace(id="playbook", to_dict=dict), event
        )
        await asyncio.sleep(0)

    engine.dispatch_event.assert_awaited_once()
    dispatched = engine.dispatch_event.await_args.args[0]
    assert isinstance(dispatched["_received_at"], float)
    assert dispatched["event_id"] == "event-1"
    # The caller's dict is not mutated; a replay sees its own arrival.
    assert "_received_at" not in event


async def test_v2_dispatch_entry_keeps_an_existing_arrival_stamp():
    from src.orchestrator.core import Orchestrator

    engine = MagicMock()
    engine.dispatch_event = AsyncMock()
    orchestrator = SimpleNamespace(
        config=SimpleNamespace(playbooks=SimpleNamespace(enabled=True, v2_engine=True)),
        db=MagicMock(), _command_handler=MagicMock(), llm=MagicMock(), bus=MagicMock(),
    )
    with patch("src.playbooks.services.build_v2_engine", return_value=engine):
        await Orchestrator._on_playbook_trigger(
            orchestrator, SimpleNamespace(id="playbook"),
            {"type": "task.completed", "event_id": "e", "_received_at": 12.5},
        )
        await asyncio.sleep(0)

    assert engine.dispatch_event.await_args.args[0]["_received_at"] == 12.5


async def _execute_denied(handler, name: str, args: dict) -> dict:
    """Run *name* as a session principal whose explicit policy grants nothing."""
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, _principal_var
    from src.profiles.capabilities import CapabilityPolicy

    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=[]),
        session_id="s-1",
        profile_id="legacy-reviewer",
    )
    token = _principal_var.set(principal)
    try:
        return await handler.execute(name, args)
    finally:
        _principal_var.reset(token)


async def test_capability_denial_is_recorded_durably(command_handler_factory):
    """§10.2: the denial still denies, and now it also leaves a row."""
    handler = await command_handler_factory()
    handler.config.security.capability_enforcement = "enforce"

    result = await _execute_denied(handler, "create_task", {"title": "x"})

    assert "capability denied" in result["error"]
    events = await handler.db.get_recent_events(event_type="capability.denied")
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["command"] == "create_task"
    assert payload["profile_id"] == "legacy-reviewer"
    assert payload["shadow"] is False
    assert "args" not in payload and "title" not in json.dumps(payload)


async def test_capability_denial_row_failure_never_unblocks_the_command(command_handler_factory):
    handler = await command_handler_factory()
    handler.config.security.capability_enforcement = "enforce"
    handler.db.log_event = AsyncMock(side_effect=RuntimeError("events table gone"))

    result = await _execute_denied(handler, "create_task", {"title": "x"})

    assert "capability denied" in result["error"]
