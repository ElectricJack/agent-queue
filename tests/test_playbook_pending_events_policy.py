"""Pending-event overflow, replay and discard policy — Package 6 T-16.

The plan
(``docs/superpowers/plans/2026-09-01-playbook-v2-migration-artifacts.md``
§5.5 T-16) asks for five properties, and §4.3 states why each one is a
security property rather than an ergonomic one:

* an overflow is a *drop*, and a drop that is not recorded is a silently
  lost event — so the row keeps its playbook id, its event id and the
  reason it was dropped, and the cutover report reads the same rows;
* a replay is a fresh dispatch, so the current activation's rule matching
  and guards run again — a held event is never fast-pathed past a guard;
* a held payload is untrusted input, so the server-owned principal keys are
  stripped before it re-enters the engine;
* ``replay_on_activation: automatic`` may not point at an activation whose
  health is ``question_required`` — an unreviewed playbook may not
  auto-consume a backlog;
* a discard is a waiver, so it carries an operator reason with the same
  12-character floor as a migration acknowledgement.

The storage cases are parametrised over both backends because the overflow
drop and the insert have to happen in one transaction; only PostgreSQL
proves the fence.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.playbook_v2_commands import (
    PENDING_EVENT_REASON_TOO_SHORT_ERROR,
    PlaybookV2CommandsMixin,
)
from src.commands.principal import SERVER_OWNED_ARG_KEYS
from src.config import PlaybooksConfig
from src.database import Database
from src.database.queries.playbook_run_queries import (
    PENDING_EVENT_EXPIRY_ACTOR,
    PENDING_EVENT_OVERFLOW_ACTOR,
)
from src.playbooks.run_state import PendingEventQuotaExceeded
from tests.pg_dsn import ensure_worker_postgres_dsn

POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()

NOW = 1_000_000.0
DAY = 86_400.0


# -- fixtures ---------------------------------------------------------------


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
    try:
        yield database
    finally:
        await database.close()


async def retain(db, **overrides) -> str | None:
    base: dict[str, Any] = {
        "playbook_id": "default-pipeline",
        "scope": "system",
        "scope_identifier": "",
        "event_type": "task.completed",
        "event": {"task": {"id": "t-1"}},
        "event_id": "evt-1",
        "dedup_key": "default-pipeline:t-1",
        "reason": "stale_contract",
        "now": NOW,
        "ttl_seconds": 7 * DAY,
    }
    base.update(overrides)
    return await db.retain_pending_event(**base)


async def retain_n(db, count: int, *, start: int = 0) -> list[str]:
    ids = []
    for index in range(start, start + count):
        ids.append(
            await retain(
                db,
                event={"task": {"id": f"t-{index}"}},
                event_id=f"evt-{index}",
                dedup_key=f"default-pipeline:t-{index}",
                now=NOW + index,
            )
        )
    return ids


def _command_handler(row, *, resolve=True):
    """The command mixin over doubles — the boundary, not the storage."""
    handler = PlaybookV2CommandsMixin()
    handler.config = SimpleNamespace(
        playbooks=PlaybooksConfig(
            v2_api=True,
            v2_storage_enabled=True,
            v2_activation_writes=True,
        )
    )
    handler.db = SimpleNamespace(
        get_pending_events=AsyncMock(return_value=[row]),
        resolve_pending_event=AsyncMock(return_value=resolve),
        claim_pending_event_dispatch=AsyncMock(return_value="claim-1"),
        renew_pending_event_dispatch_claim=AsyncMock(return_value=True),
        finalize_pending_event_dispatch=AsyncMock(return_value=True),
        record_pending_event_dispatch_failure=AsyncMock(return_value=True),
    )
    engine = MagicMock()
    engine.dispatch_event = AsyncMock(
        return_value=SimpleNamespace(run_ids=("run-1",), rules_selected=("r",))
    )
    handler._v2_engine = MagicMock(return_value=engine)
    return handler, engine


# -- 6: overflow -------------------------------------------------------------


async def test_overflow_drop_oldest_is_audited(db):
    """Exceeding the quota drops the oldest and records the drop."""
    db.set_playbook_pending_event_quota(3)
    db.set_playbook_pending_event_overflow("drop_oldest")
    first, *_rest = await retain_n(db, 3)

    overflowing = await retain(
        db,
        event={"task": {"id": "t-9"}},
        event_id="evt-9",
        dedup_key="default-pipeline:t-9",
        now=NOW + 9,
    )

    assert overflowing is not None
    unresolved = await db.list_pending_events(playbook_id="default-pipeline")
    assert len(unresolved) == 3
    assert first not in {row["pending_event_id"] for row in unresolved}

    everything = await db.list_pending_events(
        playbook_id="default-pipeline", include_resolved=True
    )
    [dropped] = [row for row in everything if row["pending_event_id"] == first]
    assert dropped["resolution"] == "discarded"
    assert dropped["resolved_by"] == PENDING_EVENT_OVERFLOW_ACTOR
    assert dropped["resolved_at"] is not None
    # The audit names the playbook, the event and why it went.
    assert dropped["playbook_id"] == "default-pipeline"
    assert dropped["event_id"] == "evt-0"
    assert "drop_oldest" in dropped["resolution_reason"]
    assert "3" in dropped["resolution_reason"]


async def test_overflow_reject_new_still_raises(db):
    """``reject_new`` keeps the pre-policy behaviour, and drops nothing."""
    db.set_playbook_pending_event_quota(2)
    db.set_playbook_pending_event_overflow("reject_new")
    await retain_n(db, 2)

    with pytest.raises(PendingEventQuotaExceeded) as caught:
        await retain(
            db,
            event_id="evt-9",
            dedup_key="default-pipeline:t-9",
            now=NOW + 9,
        )

    assert caught.value.limit == 2
    rows = await db.list_pending_events(playbook_id="default-pipeline", include_resolved=True)
    assert len(rows) == 2
    assert all(row["resolution"] is None for row in rows)


async def test_overflow_drop_oldest_never_drops_a_claimed_event(db):
    """A dispatch in flight owns its row; the quota may not resolve it."""
    db.set_playbook_pending_event_quota(1)
    db.set_playbook_pending_event_overflow("drop_oldest")
    [held] = await retain_n(db, 1)
    assert await db.claim_pending_event_dispatch(
        held, claimed_by="operator", now=NOW + 1, stale_before=NOW
    )

    with pytest.raises(PendingEventQuotaExceeded):
        await retain(
            db,
            event_id="evt-9",
            dedup_key="default-pipeline:t-9",
            now=NOW + 9,
        )

    [row] = await db.list_pending_events(playbook_id="default-pipeline", include_resolved=True)
    assert row["pending_event_id"] == held
    assert row["resolution"] is None


async def test_expiry_is_audited_with_a_reason(db):
    """Expiry is the other unattended drop, and carries the same audit."""
    [held] = await retain_n(db, 1)

    swept = await db.purge_pending_events(NOW + 8 * DAY, resolved_before=NOW)

    assert swept.expired == 1
    [row] = await db.list_pending_events(playbook_id="default-pipeline", include_resolved=True)
    assert row["pending_event_id"] == held
    assert row["resolution"] == "expired"
    assert row["resolved_by"] == PENDING_EVENT_EXPIRY_ACTOR
    assert "expired" in row["resolution_reason"]


async def test_duplicate_at_a_full_queue_evicts_nothing(db):
    """A duplicate is not an arrival, so it may not cost a held event.

    The regression: the quota was settled *before* the insert discovered the
    dedup conflict, so a duplicate arriving at a full queue dropped the oldest
    unrelated row and then returned ``None`` — the queue lost an event and
    gained nothing, and the audit row claimed the drop had made room.
    """
    db.set_playbook_pending_event_quota(2)
    db.set_playbook_pending_event_overflow("drop_oldest")
    oldest, newest = await retain_n(db, 2)

    duplicate = await retain(
        db,
        event={"task": {"id": "t-1"}},
        event_id="evt-dup",
        dedup_key="default-pipeline:t-1",
        now=NOW + 9,
    )

    assert duplicate is None
    rows = await db.list_pending_events(
        playbook_id="default-pipeline", include_resolved=True
    )
    assert {row["pending_event_id"] for row in rows} == {oldest, newest}
    assert all(row["resolution"] is None for row in rows)
    assert all(row["resolved_at"] is None for row in rows)


async def test_duplicate_at_a_full_queue_under_reject_new_is_a_duplicate(db):
    """``reject_new`` refuses arrivals, and a duplicate is not one."""
    db.set_playbook_pending_event_quota(2)
    db.set_playbook_pending_event_overflow("reject_new")
    held = await retain_n(db, 2)

    duplicate = await retain(
        db,
        event={"task": {"id": "t-0"}},
        event_id="evt-dup",
        dedup_key="default-pipeline:t-0",
        now=NOW + 9,
    )

    assert duplicate is None
    rows = await db.list_pending_events(playbook_id="default-pipeline")
    assert {row["pending_event_id"] for row in rows} == set(held)


async def test_duplicate_of_a_claimed_event_at_a_full_queue_evicts_nothing(db):
    """The dedup index covers claimed rows too, and still drops nothing.

    Every held row being claimed is the case that raises rather than evicting,
    so a duplicate that took the overflow path here would have raised a quota
    error for an event the queue already holds.
    """
    db.set_playbook_pending_event_quota(1)
    db.set_playbook_pending_event_overflow("drop_oldest")
    [held] = await retain_n(db, 1)
    assert await db.claim_pending_event_dispatch(
        held, claimed_by="operator", now=NOW + 1, stale_before=NOW
    )

    duplicate = await retain(
        db,
        event={"task": {"id": "t-0"}},
        event_id="evt-dup",
        dedup_key="default-pipeline:t-0",
        now=NOW + 9,
    )

    assert duplicate is None
    [row] = await db.list_pending_events(
        playbook_id="default-pipeline", include_resolved=True
    )
    assert row["pending_event_id"] == held
    assert row["resolution"] is None


async def test_overflow_never_evicts_the_arrival_it_makes_room_for(db):
    """The new row is held out of its own sweep, not spared by the ordering.

    The sweep takes the oldest unclaimed rows by ``received_at``, and an
    arrival is not guaranteed to be the newest: an event held from a queue
    that ran behind, or a replayed one carrying its original timestamp, sorts
    ahead of everything already there.  Without an explicit exclusion such an
    arrival evicts *itself*, returning an id whose row is already discarded.
    """
    db.set_playbook_pending_event_quota(2)
    db.set_playbook_pending_event_overflow("drop_oldest")
    # Both held rows are newer than the arrival, so the arrival is
    # unambiguously the oldest candidate its own sweep can see.
    older, newer = await retain_n(db, 2, start=10)

    arrival = await retain(
        db,
        event={"task": {"id": "t-late"}},
        event_id="evt-late",
        dedup_key="default-pipeline:t-late",
        now=NOW,
    )

    assert arrival is not None
    unresolved = {
        row["pending_event_id"]
        for row in await db.list_pending_events(playbook_id="default-pipeline")
    }
    assert unresolved == {arrival, newer}
    [dropped] = [
        row
        for row in await db.list_pending_events(
            playbook_id="default-pipeline", include_resolved=True
        )
        if row["resolved_at"] is not None
    ]
    assert dropped["pending_event_id"] == older


async def test_concurrent_duplicates_at_a_full_queue_lose_nothing(db):
    """Two racing duplicates settle on the index, and evict nothing.

    The insert-first ordering means the loser of the race blocks on the
    index, finds the row committed and returns ``None`` — it never reaches
    the overflow sweep.
    """
    db.set_playbook_pending_event_quota(2)
    db.set_playbook_pending_event_overflow("drop_oldest")
    oldest, newest = await retain_n(db, 2)

    results = await asyncio.gather(
        *[
            retain(
                db,
                event={"task": {"id": "t-1"}},
                event_id=f"evt-dup-{index}",
                dedup_key="default-pipeline:t-1",
                now=NOW + 20 + index,
            )
            for index in range(4)
        ]
    )

    assert results == [None, None, None, None]
    rows = await db.list_pending_events(
        playbook_id="default-pipeline", include_resolved=True
    )
    assert {row["pending_event_id"] for row in rows} == {oldest, newest}
    assert all(row["resolution"] is None for row in rows)


async def test_concurrent_arrivals_at_a_full_queue_lose_nothing(db):
    """Distinct arrivals race; every one of them is kept or audited.

    The quota is a flood ceiling, not an invariant — under real concurrency a
    loser may be refused outright — so what is asserted here is conservation:
    an arrival that reported success is on the queue or carries a drop reason,
    and a refused one left no trace at all.
    """
    db.set_playbook_pending_event_quota(2)
    db.set_playbook_pending_event_overflow("drop_oldest")
    held = set(await retain_n(db, 2))

    results = await asyncio.gather(
        *[
            retain(
                db,
                event={"task": {"id": f"t-c{index}"}},
                event_id=f"evt-c{index}",
                dedup_key=f"default-pipeline:t-c{index}",
                now=NOW + 30 + index,
            )
            for index in range(4)
        ],
        return_exceptions=True,
    )
    accepted = {result for result in results if isinstance(result, str)}
    refused = [result for result in results if isinstance(result, BaseException)]
    assert all(isinstance(error, PendingEventQuotaExceeded) for error in refused)
    assert None not in results  # distinct dedup keys are never duplicates

    everything = await db.list_pending_events(
        playbook_id="default-pipeline", include_resolved=True
    )
    # Every row that was ever written is accounted for, and only those.
    assert {row["pending_event_id"] for row in everything} == held | accepted
    for row in everything:
        if row["resolved_at"] is not None:
            assert row["resolution"] == "discarded"
            assert row["resolved_by"] == PENDING_EVENT_OVERFLOW_ACTOR
            assert "drop_oldest" in row["resolution_reason"]


# -- 7 and 8: replay ---------------------------------------------------------


async def test_replay_re_evaluates_guards():
    """A held event whose trigger no longer matches produces no run."""
    from src.commands.principal import TRUSTED_LOCAL
    from src.playbooks.definition import PlaybookDefinition
    from src.playbooks.engine import PlaybookEngine
    from src.playbooks.executors.base import EngineServices
    from tests.fixtures.contracts.engine_contracts import ENSURE_TASK, registry_with
    from tests.playbook_v2_engine_helpers import (
        InMemoryArtifactStore,
        RecordingBus,
        RecordingRunRepository,
        StubActivations,
        artifact_ref_for,
        minimal_artifact,
    )

    def guarded(outcome: str) -> PlaybookDefinition:
        payload = minimal_artifact().model_dump(mode="json")
        payload["id"] = "default-pipeline"
        payload["rules"][0]["trigger"]["filter"] = {"outcome": outcome}
        return PlaybookDefinition.model_validate(payload)

    def engine_for(definition: PlaybookDefinition):
        store = InMemoryArtifactStore()
        store.put(definition)
        registry, _adapter = registry_with(ENSURE_TASK)
        runs = RecordingRunRepository()
        return (
            PlaybookEngine(
                services=EngineServices(
                    contracts=registry,
                    clock=lambda: NOW,
                    artifact_store=store,
                    bus=RecordingBus(),
                ),
                runs=runs,
                waits=None,
                activations=StubActivations([artifact_ref_for(definition)]),
            ),
            runs,
        )

    held = {"event_type": "task.completed", "event_id": "evt-1", "outcome": "pass"}
    matching, matching_runs = engine_for(guarded("pass"))
    result = await matching.dispatch_event(held, TRUSTED_LOCAL, playbook_ids=["default-pipeline"])
    assert result.rules_selected == ("r",)
    assert matching_runs.create_calls == 1

    # The activation is rebuilt while the event is held; the guard now fails.
    rebuilt, rebuilt_runs = engine_for(guarded("fail"))
    replayed = await rebuilt.dispatch_event(
        held, TRUSTED_LOCAL, playbook_ids=["default-pipeline"]
    )
    assert replayed.rules_selected == ()
    assert replayed.run_ids == ()
    assert rebuilt_runs.create_calls == 0


async def test_replay_strips_principal_fields():
    """A held payload cannot carry principal fields into its replay (§4.3)."""
    row = {
        "pending_event_id": "event-1",
        "playbook_id": "default-pipeline",
        "event": {
            "event_type": "task.completed",
            "task_id": "t1",
            "_principal": {"kind": "trusted_local"},
            "_capabilities": {"aq_commands": ["*"]},
            "_scope": "system",
            "_policy": "allow-all",
            "_profile_id": "root",
        },
    }
    handler, engine = _command_handler(row)

    result = await handler._cmd_playbook_pending_event_action(
        {"action": "dispatch", "pending_event_ids": ["event-1"]}
    )

    assert result["success"] is True
    replayed = engine.dispatch_event.await_args.args[0]
    assert set(replayed) == {"event_type", "task_id"}
    for key in SERVER_OWNED_ARG_KEYS:
        assert key not in replayed
    # The stored row is left as received — stripping is a replay-time decision.
    assert "_principal" in row["event"]


# -- 9: automatic replay against an unreviewed activation --------------------


def test_automatic_replay_rejected_for_question_required():
    config = PlaybooksConfig(v2_pending_event_replay_on_activation="automatic")

    errors = config.validate(
        activation_healths={
            "default-pipeline": "question_required",
            "coding-reflection": "ready",
        }
    )

    [error] = [e for e in errors if e.field == "v2_pending_event_replay_on_activation"]
    assert "default-pipeline" in error.message
    assert "coding-reflection" not in error.message


def test_manual_replay_is_accepted_for_question_required():
    config = PlaybooksConfig(v2_pending_event_replay_on_activation="manual")

    assert config.validate(activation_healths={"default-pipeline": "question_required"}) == []


def test_automatic_replay_is_accepted_when_every_activation_is_reviewed():
    config = PlaybooksConfig(v2_pending_event_replay_on_activation="automatic")

    assert config.validate(activation_healths={"default-pipeline": "ready"}) == []


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("v2_pending_event_on_overflow", "delete_everything"),
        ("v2_pending_event_replay_on_activation", "eventually"),
    ],
)
def test_policy_vocabularies_are_closed(field_name, value):
    errors = PlaybooksConfig(**{field_name: value}).validate()

    assert [e.field for e in errors] == [field_name]


# -- 10: discard -------------------------------------------------------------


@pytest.mark.parametrize("reason", [None, "", "   ", "too short"])
async def test_discard_requires_reason(reason):
    row = {"pending_event_id": "event-1", "playbook_id": "default-pipeline", "event": {}}
    handler, _engine = _command_handler(row)
    args = {"action": "discard", "pending_event_ids": ["event-1"]}
    if reason is not None:
        args["reason"] = reason

    result = await handler._cmd_playbook_pending_event_action(args)

    assert result["error"] == PENDING_EVENT_REASON_TOO_SHORT_ERROR
    handler.db.resolve_pending_event.assert_not_awaited()


async def test_discard_records_the_operator_reason():
    row = {"pending_event_id": "event-1", "playbook_id": "default-pipeline", "event": {}}
    handler, _engine = _command_handler(row)

    result = await handler._cmd_playbook_pending_event_action(
        {
            "action": "discard",
            "pending_event_ids": ["event-1"],
            "reason": "superseded by the rewritten pipeline",
        }
    )

    assert result["discarded_ids"] == ["event-1"]
    kwargs = handler.db.resolve_pending_event.await_args.kwargs
    assert kwargs["resolution"] == "discarded"
    assert kwargs["resolution_reason"] == "superseded by the rewritten pipeline"


async def test_dispatch_does_not_require_a_reason():
    """Only the drop is a waiver; a replay is the ordinary operator action."""
    row = {
        "pending_event_id": "event-1",
        "playbook_id": "default-pipeline",
        "event": {"event_type": "task.completed"},
    }
    handler, engine = _command_handler(row)

    result = await handler._cmd_playbook_pending_event_action(
        {"action": "dispatch", "pending_event_ids": ["event-1"]}
    )

    assert result["success"] is True
    engine.dispatch_event.assert_awaited_once()


async def test_discard_reason_is_persisted(db):
    """End to end over real storage: the reason survives on the row."""
    [held] = await retain_n(db, 1)

    assert await db.resolve_pending_event(
        held,
        resolution="discarded",
        resolved_by="user:operator",
        resolution_reason="replaced by the reviewed artifact",
        now=NOW + 1,
    )

    [row] = await db.list_pending_events(playbook_id="default-pipeline", include_resolved=True)
    assert row["resolution"] == "discarded"
    assert row["resolved_by"] == "user:operator"
    assert row["resolution_reason"] == "replaced by the reviewed artifact"


# -- the seam that makes the config keys mean something ----------------------


def test_the_daemon_binds_the_configured_policy_onto_storage():
    """A config key nothing reads is a lie; ``build_v2_engine`` reads them."""
    from src.database.queries.playbook_run_queries import PlaybookRunQueryMixin
    from src.playbooks.services import bind_pending_event_policy

    class _Repository(PlaybookRunQueryMixin):
        pass

    repository = _Repository()
    bind_pending_event_policy(
        repository,
        PlaybooksConfig(
            v2_max_pending_events_per_playbook=7,
            v2_pending_event_on_overflow="reject_new",
        ),
    )

    assert repository.playbook_pending_event_quota() == 7
    assert repository.playbook_pending_event_overflow() == "reject_new"


def test_an_unknown_overflow_policy_does_not_break_the_daemon(caplog):
    """``AppConfig.validate()`` reports it; the engine build survives it."""
    from src.database.queries.playbook_run_queries import (
        DEFAULT_PENDING_EVENT_OVERFLOW,
        PlaybookRunQueryMixin,
    )
    from src.playbooks.services import bind_pending_event_policy

    class _Repository(PlaybookRunQueryMixin):
        pass

    repository = _Repository()
    bind_pending_event_policy(
        repository, PlaybooksConfig(v2_pending_event_on_overflow="whatever")
    )

    assert repository.playbook_pending_event_overflow() == DEFAULT_PENDING_EVENT_OVERFLOW
