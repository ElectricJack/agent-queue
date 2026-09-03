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


# -- 9b: the automatic policy at activation time -----------------------------
#
# The config key is only half the property: refusing `automatic` in
# validation is worth nothing while no runtime path reads the setting.  These
# cover the other half — what `playbook_activate` actually does with a backlog
# under each policy, and the production caller that hands live activation
# health to `PlaybooksConfig.validate()`.


def _activation_record(ref, *, health, enabled=True):
    from src.playbooks.activation import ActivationHealth, ActivationHealthRecord

    return ActivationHealthRecord(
        "activation-1",
        ref.playbook_id,
        "system",
        "",
        enabled,
        ref.artifact_sha256,
        ActivationHealth(health),
        (),
        activated_by="operator",
    )


def _activation_handler(*, policy, health="ready", enabled=True, held=(), quota=1000):
    """``playbook_activate`` over doubles, with a backlog behind the artifact."""
    from tests.test_api_playbook_v2_commands import _backend_fixture
    from tests.test_playbook_activation_commands import _Handler

    definition, ref, _activation = _backend_fixture()
    refreshed = _activation_record(ref, health=health, enabled=enabled)
    handler = _Handler(definition, ref, [[], [refreshed]])
    handler.config = SimpleNamespace(
        playbooks=PlaybooksConfig(
            v2_api=True,
            v2_storage_enabled=True,
            v2_activation_writes=True,
            v2_pending_event_replay_on_activation=policy,
            v2_max_pending_events_per_playbook=quota,
        )
    )
    handler.db.list_pending_events = AsyncMock(return_value=[dict(row) for row in held])
    handler.db.claim_pending_event_dispatch = AsyncMock(return_value="claim-1")
    handler.db.renew_pending_event_dispatch_claim = AsyncMock(return_value=True)
    handler.db.finalize_pending_event_dispatch = AsyncMock(return_value=True)
    handler.db.record_pending_event_dispatch_failure = AsyncMock(return_value=True)
    engine = MagicMock()
    engine.dispatch_event = AsyncMock(
        return_value=SimpleNamespace(run_ids=("run-1",), rules_selected=("r",))
    )
    handler._v2_engine = MagicMock(return_value=engine)
    return handler, engine, definition, ref


async def _activate(handler, definition, ref, **extra):
    args = {
        "playbook_id": definition.id,
        "artifact_sha256": ref.artifact_sha256,
        "acknowledge_diff": ref.artifact_sha256,
    }
    args.update(extra)
    return await handler._cmd_playbook_activate(args)


def _held(event_id, **overrides):
    row = {
        "pending_event_id": event_id,
        "playbook_id": "default-pipeline",
        "event": {"event_type": "task.completed", "event_id": event_id},
    }
    row.update(overrides)
    return row


async def test_manual_policy_leaves_the_backlog_for_the_operator():
    """The default policy does not touch held events, and says so."""
    handler, engine, definition, ref = _activation_handler(
        policy="manual", held=[_held("event-1")]
    )

    result = await _activate(handler, definition, ref)

    assert result["blocked"] is False
    replay = result["pending_event_replay"]
    assert replay == {
        "policy": "manual",
        "replayed": False,
        "refused_reason": None,
        "considered": 0,
        "dispatched_run_ids": [],
        "skipped": [],
        "errors": [],
    }
    handler.db.list_pending_events.assert_not_awaited()
    engine.dispatch_event.assert_not_awaited()


async def test_automatic_policy_consumes_the_backlog_on_activation():
    """``automatic`` is what the config docstring says it is: activation drains."""
    handler, engine, definition, ref = _activation_handler(
        policy="automatic", held=[_held("event-1"), _held("event-2")]
    )

    result = await _activate(handler, definition, ref)

    replay = result["pending_event_replay"]
    assert replay["policy"] == "automatic"
    assert replay["replayed"] is True
    assert replay["refused_reason"] is None
    assert replay["considered"] == 2
    assert replay["dispatched_run_ids"] == ["run-1", "run-1"]
    assert replay["errors"] == []
    # Each row went through the durable claim and was finalised, not merely
    # dispatched — a replay that does not consume its row replays forever.
    assert handler.db.claim_pending_event_dispatch.await_count == 2
    assert handler.db.finalize_pending_event_dispatch.await_count == 2
    assert engine.dispatch_event.await_count == 2


async def test_automatic_replay_is_a_fresh_dispatch_of_the_stripped_payload():
    """The activation replay re-enters the same guarded path as the manual one."""
    handler, engine, definition, ref = _activation_handler(
        policy="automatic",
        held=[
            _held(
                "event-1",
                event={
                    "event_type": "task.completed",
                    "task_id": "t1",
                    "_principal": {"kind": "trusted_local"},
                    "_capabilities": {"aq_commands": ["*"]},
                },
            )
        ],
    )

    await _activate(handler, definition, ref)

    replayed = engine.dispatch_event.await_args.args[0]
    assert set(replayed) == {"event_type", "task_id"}
    for key in SERVER_OWNED_ARG_KEYS:
        assert key not in replayed
    # Matching is re-run against the playbook that was just activated.
    assert engine.dispatch_event.await_args.kwargs["playbook_ids"] == ["default-pipeline"]


async def test_automatic_replay_is_refused_for_a_question_required_activation():
    """Fail-closed: an unreviewed playbook may not auto-consume a backlog."""
    from src.commands.playbook_v2_commands import PENDING_EVENT_REPLAY_UNREVIEWED_REFUSAL

    handler, engine, definition, ref = _activation_handler(
        policy="automatic", health="question_required", held=[_held("event-1")]
    )

    result = await _activate(handler, definition, ref)

    assert result["blocked"] is False  # the artifact is live; only the replay is refused
    replay = result["pending_event_replay"]
    assert replay["replayed"] is False
    assert replay["refused_reason"] == PENDING_EVENT_REPLAY_UNREVIEWED_REFUSAL
    assert "question_required" in replay["refused_reason"]
    handler.db.list_pending_events.assert_not_awaited()
    engine.dispatch_event.assert_not_awaited()
    handler.db.claim_pending_event_dispatch.assert_not_awaited()


async def test_automatic_replay_is_refused_for_a_disabled_activation():
    from src.commands.playbook_v2_commands import PENDING_EVENT_REPLAY_DISABLED_REFUSAL

    handler, engine, definition, ref = _activation_handler(
        policy="automatic", enabled=False, held=[_held("event-1")]
    )

    result = await _activate(handler, definition, ref, enabled=False)

    replay = result["pending_event_replay"]
    assert replay["replayed"] is False
    assert replay["refused_reason"] == PENDING_EVENT_REPLAY_DISABLED_REFUSAL
    engine.dispatch_event.assert_not_awaited()


@pytest.mark.parametrize("health", ["stale_contract", "invalid", "unavailable"])
async def test_automatic_replay_requires_a_ready_activation(health):
    """Only ``ready`` may drain: a stale or broken artifact is not a review."""
    handler, engine, definition, ref = _activation_handler(
        policy="automatic", health=health, held=[_held("event-1")]
    )

    result = await _activate(handler, definition, ref)

    replay = result["pending_event_replay"]
    assert replay["replayed"] is False
    assert health in replay["refused_reason"]
    engine.dispatch_event.assert_not_awaited()


async def test_automatic_replay_is_bounded_by_the_pending_event_quota():
    """An activation may not become an unbounded dispatch storm."""
    handler, _engine, definition, ref = _activation_handler(
        policy="automatic", held=[_held("event-1")], quota=7
    )

    await _activate(handler, definition, ref)

    kwargs = handler.db.list_pending_events.await_args.kwargs
    assert kwargs["limit"] == 7
    assert kwargs["playbook_id"] == "default-pipeline"
    assert kwargs["include_resolved"] is False


async def test_automatic_replay_reports_a_failed_dispatch_without_losing_the_row():
    """A replay failure is reported; the row is restored, not consumed."""
    handler, engine, definition, ref = _activation_handler(
        policy="automatic", held=[_held("event-1")]
    )
    engine.dispatch_event.side_effect = RuntimeError("executor exploded")

    result = await _activate(handler, definition, ref)

    replay = result["pending_event_replay"]
    assert replay["replayed"] is True
    assert replay["dispatched_run_ids"] == []
    assert replay["errors"] == ["event-1: executor exploded"]
    handler.db.record_pending_event_dispatch_failure.assert_awaited_once()
    handler.db.finalize_pending_event_dispatch.assert_not_awaited()
    # The activation itself still succeeded — the artifact is live either way.
    assert result["success"] is True
    assert result["blocked"] is False


async def test_automatic_replay_skips_a_row_another_operator_holds():
    handler, engine, definition, ref = _activation_handler(
        policy="automatic", held=[_held("event-1")]
    )
    handler.db.claim_pending_event_dispatch = AsyncMock(return_value=None)

    result = await _activate(handler, definition, ref)

    replay = result["pending_event_replay"]
    assert replay["skipped"] == ["event-1"]
    assert replay["dispatched_run_ids"] == []
    engine.dispatch_event.assert_not_awaited()


async def test_blocked_activation_never_replays():
    """No activation, no replay — and the response says which refusal it was."""
    from src.commands.playbook_v2_commands import PENDING_EVENT_REPLAY_BLOCKED_REFUSAL

    handler, engine, definition, ref = _activation_handler(
        policy="automatic", held=[_held("event-1")]
    )

    # No ``acknowledge_diff`` — the executable-change blocker fires.
    result = await handler._cmd_playbook_activate(
        {"playbook_id": definition.id, "artifact_sha256": ref.artifact_sha256}
    )

    assert result["blocked"] is True
    replay = result["pending_event_replay"]
    assert replay["replayed"] is False
    assert replay["refused_reason"] == PENDING_EVENT_REPLAY_BLOCKED_REFUSAL
    engine.dispatch_event.assert_not_awaited()


# -- 9c: the production caller for activation-aware config validation ---------


def _doctor_ctx(policy, healths):
    """A doctor context whose activation health is fixed by the test."""
    from src.doctor.models import DoctorContext
    from tests.playbook_v2_helpers import StubContracts, StubProfiles

    async def _v2_lookups():
        return StubContracts(), StubProfiles(), None

    records = [
        SimpleNamespace(playbook_id=playbook_id, health=SimpleNamespace(value=health))
        for playbook_id, health in healths.items()
    ]

    async def _load(db, **kwargs):
        return records

    return (
        DoctorContext(
            config=SimpleNamespace(
                playbooks=PlaybooksConfig(
                    v2_storage_enabled=True,
                    v2_pending_event_replay_on_activation=policy,
                )
            ),
            db=SimpleNamespace(list_playbook_activations=AsyncMock(return_value=[])),
            handler=SimpleNamespace(_v2_lookups=_v2_lookups),
        ),
        _load,
    )


async def test_doctor_reports_automatic_replay_against_an_unreviewed_activation(monkeypatch):
    """The missing production caller: config validation against live rows."""
    import src.playbooks.activation as activation_module
    from src.doctor.models import Severity
    from src.doctor.playbook_v2_checks import (
        REPLAY_POLICY_CHECK_ID,
        _check_pending_event_replay_policy,
    )

    ctx, load = _doctor_ctx(
        "automatic", {"default-pipeline": "question_required", "coding-reflection": "ready"}
    )
    monkeypatch.setattr(activation_module, "load_activation_health", load)

    result = await _check_pending_event_replay_policy(ctx)

    assert result.id == REPLAY_POLICY_CHECK_ID
    assert result.severity is Severity.ERROR
    assert "default-pipeline" in result.detail
    assert "coding-reflection" not in result.detail
    assert result.data["question_required"] == ["default-pipeline"]
    assert result.fixable is False


async def test_doctor_is_ok_when_every_activation_is_reviewed(monkeypatch):
    import src.playbooks.activation as activation_module
    from src.doctor.models import Severity
    from src.doctor.playbook_v2_checks import _check_pending_event_replay_policy

    ctx, load = _doctor_ctx("automatic", {"default-pipeline": "ready"})
    monkeypatch.setattr(activation_module, "load_activation_health", load)

    result = await _check_pending_event_replay_policy(ctx)

    assert result.severity is Severity.OK
    assert result.data["checked"] == 1


async def test_doctor_does_not_read_activations_under_the_manual_policy(monkeypatch):
    import src.playbooks.activation as activation_module
    from src.doctor.models import Severity
    from src.doctor.playbook_v2_checks import _check_pending_event_replay_policy

    ctx, _load = _doctor_ctx("manual", {"default-pipeline": "question_required"})

    def _explode(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("manual policy must not need activation health")

    monkeypatch.setattr(activation_module, "load_activation_health", _explode)

    result = await _check_pending_event_replay_policy(ctx)

    assert result.severity is Severity.OK
    assert result.data["policy"] == "manual"


def test_replay_policy_check_is_registered_in_the_default_doctor_registry():
    from src.doctor import default_registry
    from src.doctor.playbook_v2_checks import REPLAY_POLICY_CHECK_ID

    registry = default_registry()
    check = next(c for c in registry.checks() if c.id == REPLAY_POLICY_CHECK_ID)
    assert check.fix is None
