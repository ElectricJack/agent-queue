"""Durable delivery tests for hierarchical-integration events."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.config import PlaybooksConfig
from src.database import Database
from src.database.tables import integration_outbox, playbook_pending_events, playbook_v2_runs
from src.integration.outbox import IntegrationOutbox, enqueue_integration_event
from src.models import Project
from src.playbooks.artifact_store import ArtifactStore
from src.playbooks.definition import PlaybookDefinition
from src.playbooks.runtime import V2PlaybookRuntime


NOW = 1_789_000_000.0


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "integration-outbox.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="integration project"))
    yield database
    await database.close()


def _terminal_playbook(playbook_id: str, event_type: str) -> PlaybookDefinition:
    return PlaybookDefinition.model_validate(
        {
            "schema_version": 2,
            "id": playbook_id,
            "version": 1,
            "scope": {"type": "project", "project_id": "p"},
            "source_hash": "sha256:" + "1" * 64,
            "compiled_at": "2026-09-04T00:00:00Z",
            "purpose": "routine",
            "rules": [
                {
                    "id": "accept",
                    "name": "Accept integration event",
                    "trigger": {"event_type": event_type},
                    "entry_step": "done",
                    "source": {"path": "integration.md", "start_line": 1, "end_line": 1},
                }
            ],
            "steps": {
                "done": {
                    "type": "terminal",
                    "rule": "accept",
                    "title": "Accepted",
                    "outcome": "completed",
                    "source": {"path": "integration.md", "start_line": 2, "end_line": 2},
                }
            },
        }
    )


async def _activate(db, compiled_root, playbook_id: str, event_type: str) -> None:
    definition = _terminal_playbook(playbook_id, event_type)
    store = ArtifactStore(str(compiled_root))
    ref = store.put(
        definition,
        source_digest=definition.source_hash,
        contract_fingerprint=definition.contract_fingerprint(),
        profile_fingerprint="",
        compiler_build="test",
        version=definition.version,
    )
    await db.upsert_playbook_artifact(
        ref,
        scope="project",
        scope_identifier="p",
        path=store.path_for(ref.artifact_sha256),
        size_bytes=len(store.canonical_bytes(definition)),
    )
    await db.set_playbook_activation(
        playbook_id=playbook_id,
        scope="project",
        scope_identifier="p",
        artifact_sha256=ref.artifact_sha256,
        enabled=True,
        activated_by="test",
        health="ready",
        reasons="[]",
    )


def _runtime(db, compiled_root) -> V2PlaybookRuntime:
    config = SimpleNamespace(
        compiled_root=str(compiled_root),
        playbooks=PlaybooksConfig(enabled=True),
        security=SimpleNamespace(capability_enforcement="enforce"),
    )
    return V2PlaybookRuntime(
        config=config,
        db=db,
        handler=SimpleNamespace(),
        llm=None,
        bus=None,
    )


async def _enqueue(db, *, event_id="event-1", dedup_key="sealed:operation-1") -> None:
    async with db.immediate() as conn:
        await enqueue_integration_event(
            conn,
            event_id=event_id,
            dedup_key=dedup_key,
            project_id="p",
            event_type="integration.sealed",
            payload={"operation_id": "operation-1"},
            available_at=NOW,
        )


async def _outbox_rows(db):
    async with db._engine.connect() as conn:
        return (await conn.execute(select(integration_outbox))).mappings().all()


async def _pending_rows(db):
    async with db._engine.connect() as conn:
        return (await conn.execute(select(playbook_pending_events))).mappings().all()


async def _accepted_activation_count(db, event_id: str) -> int:
    async with db._engine.connect() as conn:
        rows = (
            await conn.execute(
                select(playbook_v2_runs.c.run_id).where(
                    playbook_v2_runs.c.event_id == event_id
                )
            )
        ).all()
    return len(rows)


async def test_committed_event_survives_dispatcher_restart(db, tmp_path):
    await _activate(db, tmp_path / "compiled", "integration-train", "integration.sealed")
    await _enqueue(db)

    first = _runtime(db, tmp_path / "compiled")
    await first.refresh()
    del first

    restarted = _runtime(db, tmp_path / "compiled")
    await restarted.refresh()
    outbox = IntegrationOutbox(db, restarted.accept_integration_event)
    assert await outbox.dispatch_due(NOW) == 1
    await restarted.shutdown()

    [row] = await _outbox_rows(db)
    assert row["delivered_at"] == NOW
    assert await _accepted_activation_count(db, "event-1") == 1


async def test_consumer_crash_after_acceptance_replays_one_activation(db, tmp_path):
    await _activate(db, tmp_path / "compiled", "integration-train", "integration.sealed")
    await _enqueue(db)
    first_runtime = _runtime(db, tmp_path / "compiled")
    await first_runtime.refresh()
    first_outbox = IntegrationOutbox(db, first_runtime.accept_integration_event)

    class SimulatedProcessCrash(BaseException):
        pass

    async def crash_before_ack(*_args, **_kwargs):
        raise SimulatedProcessCrash

    first_outbox._acknowledge = crash_before_ack
    with pytest.raises(SimulatedProcessCrash):
        await first_outbox.dispatch_due(NOW)
    await first_runtime.shutdown()
    assert (await _outbox_rows(db))[0]["delivered_at"] is None

    restarted = _runtime(db, tmp_path / "compiled")
    await restarted.refresh()
    outbox = IntegrationOutbox(db, restarted.accept_integration_event)
    await outbox.dispatch_due(NOW)
    await outbox.dispatch_due(NOW)
    await restarted.shutdown()

    assert await _accepted_activation_count(db, "event-1") == 1


async def test_runtime_restart_replays_an_accepted_but_unstarted_pending_event(db, tmp_path):
    compiled = tmp_path / "compiled"
    await _activate(db, compiled, "integration-train", "integration.sealed")
    await db.retain_integration_event(
        playbook_id="integration-train",
        scope="project",
        scope_identifier="p",
        event_type="integration.sealed",
        event={
            "_event_type": "integration.sealed",
            "event_id": "event-1",
            "project_id": "p",
            "operation_id": "operation-1",
        },
        event_id="event-1",
        now=NOW,
    )

    restarted = _runtime(db, compiled)
    await restarted.refresh()
    await restarted.shutdown()

    assert await _accepted_activation_count(db, "event-1") == 1
    [row] = await _pending_rows(db)
    assert row["resolution"] == "dispatched"


async def test_zero_matching_activations_keeps_event_for_retry(db):
    await _enqueue(db)
    runtime = _runtime(db, "/tmp/no-integration-artifacts")
    await runtime.refresh()
    outbox = IntegrationOutbox(db, runtime.accept_integration_event)

    assert await outbox.dispatch_due(NOW) == 0

    [row] = await _outbox_rows(db)
    assert row["delivered_at"] is None
    assert row["attempts"] == 1
    assert row["available_at"] == NOW + 1
    assert await _pending_rows(db) == []


async def test_partial_destination_acceptance_is_completed_before_ack(db, tmp_path):
    compiled = tmp_path / "compiled"
    await _activate(db, compiled, "integration-train-a", "integration.sealed")
    await _activate(db, compiled, "integration-train-b", "integration.sealed")
    await _enqueue(db)
    runtime = _runtime(db, compiled)
    await runtime.refresh()
    original = db.retain_integration_event
    calls = 0

    async def crash_on_second_destination(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated destination write failure")
        return await original(**kwargs)

    db.retain_integration_event = crash_on_second_destination
    outbox = IntegrationOutbox(db, runtime.accept_integration_event)
    assert await outbox.dispatch_due(NOW) == 0
    assert len(await _pending_rows(db)) == 1

    db.retain_integration_event = original
    assert await outbox.dispatch_due(NOW + 1) == 1
    await runtime.shutdown()

    rows = await _pending_rows(db)
    assert len(rows) == 2
    assert len({row["playbook_id"] for row in rows}) == 2
    assert all(row["protected"] for row in rows)
    assert await _accepted_activation_count(db, "event-1") == 2


async def test_pending_row_stays_protected_until_every_selected_rule_has_a_run(db, tmp_path):
    await _activate(db, tmp_path / "compiled", "integration-train", "integration.sealed")
    runtime = _runtime(db, tmp_path / "compiled")
    await runtime.refresh()
    pending_id = await db.retain_integration_event(
        playbook_id="integration-train",
        scope="project",
        scope_identifier="p",
        event_type="integration.sealed",
        event={
            "_event_type": "integration.sealed",
            "event_id": "event-1",
            "project_id": "p",
            "operation_id": "operation-1",
        },
        event_id="event-1",
        now=NOW,
    )
    [row] = await db.get_pending_events([pending_id])

    async def partial_dispatch(*_args, **_kwargs):
        return SimpleNamespace(
            rules_selected=("accepted", "crashed-before-run"),
            run_ids=("run-1",),
        )

    runtime._engine.dispatch_event = partial_dispatch
    await runtime._dispatch_integration_pending(row)

    [retained] = await db.get_pending_events([pending_id])
    assert retained["protected"] is True
    assert "no durable playbook run" in retained["last_error"]


async def test_protected_pending_events_ignore_generic_expiry_overflow_and_discard(db):
    await db.retain_integration_event(
        playbook_id="integration-train",
        scope="project",
        scope_identifier="p",
        event_type="integration.sealed",
        event={"event_id": "protected", "project_id": "p", "operation_id": "op"},
        event_id="protected",
        now=NOW,
    )
    db.set_playbook_pending_event_quota(1)
    db.set_playbook_pending_event_overflow("drop_oldest")
    first = await db.retain_pending_event(
        playbook_id="ordinary",
        scope="system",
        scope_identifier="",
        event_type="task.completed",
        event={"event_id": "ordinary-1"},
        event_id="ordinary-1",
        dedup_key="ordinary-1",
        reason="unavailable",
        now=NOW,
        ttl_seconds=1,
    )
    second = await db.retain_pending_event(
        playbook_id="ordinary",
        scope="system",
        scope_identifier="",
        event_type="task.completed",
        event={"event_id": "ordinary-2"},
        event_id="ordinary-2",
        dedup_key="ordinary-2",
        reason="unavailable",
        now=NOW + 0.5,
        ttl_seconds=1,
    )

    await db.purge_pending_events(NOW + 10, resolved_before=0)
    assert not await db.resolve_pending_event(
        (await _pending_rows(db))[0]["pending_event_id"],
        resolution="discarded",
        resolved_by="operator",
        now=NOW + 10,
        resolution_reason="generic cleanup",
    )

    rows = await _pending_rows(db)
    protected = [row for row in rows if row["event_id"] == "protected"]
    assert len(protected) == 1
    assert protected[0]["resolved_at"] is None
    assert first != second
    [dropped] = [row for row in rows if row["pending_event_id"] == first]
    assert dropped["resolution"] == "discarded"


async def test_protected_destination_identity_includes_activation_scope(db):
    event = {
        "event_id": "shared",
        "project_id": "p",
        "operation_id": "op",
    }
    system = await db.retain_integration_event(
        playbook_id="integration-train",
        scope="system",
        scope_identifier="",
        event_type="integration.sealed",
        event=event,
        event_id="shared",
        now=NOW,
    )
    project = await db.retain_integration_event(
        playbook_id="integration-train",
        scope="project",
        scope_identifier="p",
        event_type="integration.sealed",
        event=event,
        event_id="shared",
        now=NOW,
    )

    assert system != project
    assert len(await _pending_rows(db)) == 2


async def test_retry_delay_is_exponential_and_bounded(db):
    await _enqueue(db)

    async def unavailable(*_args, **_kwargs):
        return False

    outbox = IntegrationOutbox(
        db,
        unavailable,
        retry_base_seconds=2,
        retry_max_seconds=5,
    )
    for now, expected in ((NOW, NOW + 2), (NOW + 2, NOW + 6), (NOW + 6, NOW + 11)):
        assert await outbox.dispatch_due(now) == 0
        [row] = await _outbox_rows(db)
        assert row["available_at"] == expected


async def test_dispatch_page_is_bounded(db):
    for index in range(3):
        await _enqueue(db, event_id=f"event-{index}", dedup_key=f"sealed:{index}")

    accepted: list[str] = []

    async def accept(_event_type, payload, event_id):
        accepted.append(event_id)
        return True

    outbox = IntegrationOutbox(db, accept, page_size=2)
    assert await outbox.dispatch_due(NOW) == 2
    assert len(accepted) == 2
