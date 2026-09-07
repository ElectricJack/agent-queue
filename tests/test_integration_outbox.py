"""Durable delivery tests for hierarchical-integration events."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from src.config import PlaybooksConfig
from src.database import Database
from src.database.tables import (
    integration_operation_artifact_pins,
    integration_outbox,
    integration_outbox_artifact_pins,
    integration_repair_operations,
    playbook_activations,
    playbook_artifacts,
    playbook_pending_events,
    playbook_v2_runs,
)
from src.integration.outbox import (
    IntegrationOutbox,
    enqueue_integration_event,
    freeze_destination_manifest,
    load_acceptance_state,
)
from src.integration.controls import IntegrationControlService
from src.models import Project, RepoConfig, RepoSourceType
from src.playbooks.artifact_store import ArtifactStore
from src.playbooks.definition import PlaybookDefinition
from src.playbooks import runtime as runtime_module
from src.playbooks.runtime import V2PlaybookRuntime


NOW = 1_789_000_000.0


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "integration-outbox.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="integration project"))
    yield database
    await database.close()


def _terminal_playbook(
    playbook_id: str,
    event_type: str,
    *,
    scope: str = "project",
    source_digit: str = "1",
) -> PlaybookDefinition:
    scope_value = (
        {"type": "system"}
        if scope == "system"
        else {"type": "project", "project_id": "p"}
    )
    return PlaybookDefinition.model_validate(
        {
            "schema_version": 2,
            "id": playbook_id,
            "version": 1,
            "scope": scope_value,
            "source_hash": "sha256:" + source_digit * 64,
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


async def _activate(
    db,
    compiled_root,
    playbook_id: str,
    event_type: str,
    *,
    scope: str = "project",
    source_digit: str = "1",
) -> tuple[str, str]:
    definition = _terminal_playbook(
        playbook_id, event_type, scope=scope, source_digit=source_digit
    )
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
        scope=scope,
        scope_identifier="p" if scope == "project" else "",
        artifact_sha256=ref.artifact_sha256,
        enabled=True,
        activated_by="test",
        health="ready",
        reasons="[]",
    )
    async with db._engine.connect() as conn:
        activation_id = (
            await conn.execute(
                select(playbook_activations.c.activation_id).where(
                    playbook_activations.c.playbook_id == playbook_id,
                    playbook_activations.c.scope == scope,
                )
            )
        ).scalar_one()
    return activation_id, ref.artifact_sha256


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


async def _artifact_pin_rows(db):
    async with db._engine.connect() as conn:
        return (
            await conn.execute(select(integration_outbox_artifact_pins))
        ).mappings().all()


async def _pin_operation_route(
    db,
    *,
    operation_id: str,
    playbook_id: str,
    artifact_sha256: str,
    activation_id: str,
    scope: str = "project",
    scope_identifier: str = "p",
) -> None:
    async with db.immediate() as conn:
        await conn.execute(
            integration_repair_operations.insert().values(
                id=operation_id,
                target_kind="batch",
                batch_id=operation_id,
                episode_id=operation_id,
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="test",
                route_playbook_id=playbook_id,
                route_scope=scope,
                route_scope_identifier=scope_identifier,
                route_activation_id=activation_id,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await conn.execute(
            integration_operation_artifact_pins.insert().values(
                operation_id=operation_id,
                artifact_sha256=artifact_sha256,
            )
        )


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


async def _wait_for_accepted(db, event_id: str, *, count: int = 1) -> None:
    async with asyncio.timeout(10):
        while await _accepted_activation_count(db, event_id) < count:
            await asyncio.sleep(0.02)


async def _wait_for_pending_resolution(db, event_id: str, *, count: int = 1) -> None:
    async with asyncio.timeout(10):
        while (
            sum(
                row["event_id"] == event_id and row["resolution"] == "dispatched"
                for row in await _pending_rows(db)
            )
            < count
        ):
            await asyncio.sleep(0.02)


async def test_committed_event_survives_dispatcher_restart(db, tmp_path):
    await _activate(db, tmp_path / "compiled", "integration-train", "integration.sealed")
    await _enqueue(db)

    first = _runtime(db, tmp_path / "compiled")
    await first.refresh()
    await first.shutdown()
    del first

    restarted = _runtime(db, tmp_path / "compiled")
    await restarted.refresh()
    outbox = IntegrationOutbox(db, restarted.accept_integration_event)
    assert await outbox.dispatch_due(NOW) == 1
    await _wait_for_accepted(db, "event-1")
    await _wait_for_pending_resolution(db, "event-1")
    await restarted.shutdown()

    [row] = await _outbox_rows(db)
    assert row["delivered_at"] == NOW
    assert await _artifact_pin_rows(db) == []
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
    await _wait_for_accepted(db, "event-1")
    await _wait_for_pending_resolution(db, "event-1")
    await restarted.shutdown()

    assert await _accepted_activation_count(db, "event-1") == 1


async def test_runtime_restart_replays_an_accepted_but_unstarted_pending_event(db, tmp_path):
    compiled = tmp_path / "compiled"
    activation_id, artifact_sha256 = await _activate(
        db, compiled, "integration-train", "integration.sealed"
    )
    await db.retain_integration_event(
        playbook_id="integration-train",
        activation_id=activation_id,
        artifact_sha256=artifact_sha256,
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
    await _wait_for_pending_resolution(db, "event-1")
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
    await _wait_for_accepted(db, "event-1", count=2)
    await _wait_for_pending_resolution(db, "event-1", count=2)
    await runtime.shutdown()

    rows = await _pending_rows(db)
    assert len(rows) == 2
    assert len({row["playbook_id"] for row in rows}) == 2
    assert all(row["protected"] for row in rows)
    assert await _accepted_activation_count(db, "event-1") == 2


async def test_pending_row_stays_protected_until_every_selected_rule_has_a_run(db, tmp_path):
    activation_id, artifact_sha256 = await _activate(
        db, tmp_path / "compiled", "integration-train", "integration.sealed"
    )
    runtime = _runtime(db, tmp_path / "compiled")
    await runtime.refresh()
    pending_id = await db.retain_integration_event(
        playbook_id="integration-train",
        activation_id=activation_id,
        artifact_sha256=artifact_sha256,
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


async def test_protected_pending_events_ignore_generic_expiry_overflow_and_discard(
    db, tmp_path
):
    activation_id, artifact_sha256 = await _activate(
        db, tmp_path / "compiled", "integration-train", "integration.sealed"
    )
    await db.retain_integration_event(
        playbook_id="integration-train",
        activation_id=activation_id,
        artifact_sha256=artifact_sha256,
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


async def test_protected_destination_identity_includes_activation_scope(db, tmp_path):
    compiled = tmp_path / "compiled"
    system_activation, system_sha = await _activate(
        db,
        compiled,
        "integration-train",
        "integration.sealed",
        scope="system",
    )
    project_activation, project_sha = await _activate(
        db,
        compiled,
        "integration-train",
        "integration.sealed",
        scope="project",
    )
    event = {
        "event_id": "shared",
        "project_id": "p",
        "operation_id": "op",
    }
    system = await db.retain_integration_event(
        playbook_id="integration-train",
        activation_id=system_activation,
        artifact_sha256=system_sha,
        scope="system",
        scope_identifier="",
        event_type="integration.sealed",
        event=event,
        event_id="shared",
        now=NOW,
    )
    project = await db.retain_integration_event(
        playbook_id="integration-train",
        activation_id=project_activation,
        artifact_sha256=project_sha,
        scope="project",
        scope_identifier="p",
        event_type="integration.sealed",
        event=event,
        event_id="shared",
        now=NOW,
    )

    assert system != project
    assert len(await _pending_rows(db)) == 2


async def test_scoped_destinations_dispatch_only_their_pinned_activation(db, tmp_path):
    compiled = tmp_path / "compiled"
    _system_id, system_sha = await _activate(
        db,
        compiled,
        "integration-train",
        "integration.sealed",
        scope="system",
    )
    _project_id, project_sha = await _activate(
        db,
        compiled,
        "integration-train",
        "integration.sealed",
        scope="project",
    )
    await _enqueue(db)
    runtime = _runtime(db, compiled)
    await runtime.refresh()

    assert await IntegrationOutbox(db, runtime.accept_integration_event).dispatch_due(NOW) == 1
    await _wait_for_accepted(db, "event-1", count=2)
    await _wait_for_pending_resolution(db, "event-1", count=2)
    await runtime.shutdown()

    async with db._engine.connect() as conn:
        runs = (await conn.execute(select(playbook_v2_runs))).mappings().all()
    assert {row["artifact_sha256"] for row in runs} == {system_sha, project_sha}
    assert len({row["dispatch_id"] for row in runs}) == 2


async def test_large_fanout_acceptance_resumes_in_bounded_pages(db, tmp_path):
    compiled = tmp_path / "compiled"
    for ordinal in range(65):
        await _activate(
            db,
            compiled,
            f"integration-train-{ordinal:02d}",
            "integration.sealed",
        )
    await _enqueue(db)
    runtime = _runtime(db, compiled)
    await runtime.refresh()
    runtime._schedule_integration_pending = lambda _rows: None
    original = db.retain_integration_event
    accepted: list[str] = []

    async def record_destination(**kwargs):
        accepted.append(kwargs["activation_id"])
        return await original(**kwargs)

    db.retain_integration_event = record_destination
    payload = {"project_id": "p", "operation_id": "operation-1"}

    assert not await runtime.accept_integration_event(
        "integration.sealed", payload, "event-1"
    )
    assert len(accepted) == 32
    assert not await runtime.accept_integration_event(
        "integration.sealed", payload, "event-1"
    )
    assert len(accepted) == 64
    assert await runtime.accept_integration_event(
        "integration.sealed", payload, "event-1"
    )
    assert len(accepted) == 65
    assert len(await _pending_rows(db)) == 65


async def test_managed_project_does_not_admit_legacy_merge_sweep_destination(
    db, tmp_path
):
    compiled = tmp_path / "compiled"
    await _activate(db, compiled, "pr-merge-sweep", "integration.sealed")
    async with db.immediate() as conn:
        await db.set_integration_legacy_suppression_on(
            conn,
            project_id="p",
            generation=0,
            merge_sweep_suppressed=True,
            final_review_route_suppressed=True,
            legacy_gate_creation_suppressed=True,
            policy_snapshot={},
            now=NOW,
        )
    await _enqueue(db)
    runtime = _runtime(db, compiled)
    await runtime.refresh()

    assert not await runtime.accept_integration_event(
        "integration.sealed",
        {"project_id": "p", "operation_id": "operation-1"},
        "event-1",
    )
    assert await _pending_rows(db) == []
    await runtime.shutdown()


async def test_running_runtime_reads_cutover_suppression_before_acceptance(db, tmp_path):
    compiled = tmp_path / "compiled"
    activation_id, artifact_sha = await _activate(
        db, compiled, "pr-merge-sweep", "integration.sealed"
    )
    runtime = _runtime(db, compiled)
    await runtime.refresh()
    runtime._schedule_integration_pending = lambda _rows: None

    await db.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url="https://github.com/acme/widgets.git",
            default_branch="main",
        )
    )
    async with db._engine.connect() as conn:
        artifact = (
            await conn.execute(
                select(playbook_artifacts).where(
                    playbook_artifacts.c.artifact_sha256 == artifact_sha
                )
            )
        ).mappings().one()
    route_artifact = {
        key: artifact[key]
        for key in (
            "playbook_id",
            "artifact_sha256",
            "schema_generation",
            "contract_fingerprint",
            "source_digest",
            "compiler_build",
            "compiled_at",
            "version",
        )
    }
    boundary = {
        "required_checks": {
            "version": "checks-v1",
            "names": ["Tests (default)"],
            "producer_id": "1234",
        },
        "repair": {
            "debug_intelligence_class": "deep",
            "debug_profile_id": "debugger",
        },
        "route": {
            "playbook_id": "pr-merge-sweep",
            "scope": "project",
            "scope_identifier": "p",
            "activation_id": activation_id,
            "artifact": route_artifact,
        },
        "primary_intelligence_class": "standard",
        "primary_profile_id": "worker",
    }
    await db.update_project(
        "p",
        integration_repository_id="repo",
        hierarchical_integration_policy={
            "version": 1,
            "parent": boundary,
            "root": boundary,
            "branchless_parent": "skip",
            "on_failed_child": "block",
            "on_main_moved": "rebuild",
            "cleanup": {},
        },
        integration_mode="pull_request",
    )
    enabled = await IntegrationControlService(
        db, external_preflight=lambda _project, _repository: ()
    ).enable(
        "p",
        mode="hierarchy",
        expected_generation=0,
        reason="cut over",
        operator_id="operator:local",
    )
    assert enabled["outcome"] == "enabled"
    await _enqueue(db)

    assert not await runtime.accept_integration_event(
        "integration.sealed",
        {"project_id": "p", "operation_id": "operation-1"},
        "event-1",
    )
    assert await _pending_rows(db) == []
    await runtime.shutdown()


async def test_reactivation_does_not_replace_an_accepted_artifact(db, tmp_path):
    compiled = tmp_path / "compiled"
    _old_activation, old_sha = await _activate(
        db, compiled, "integration-train", "integration.sealed", source_digit="1"
    )
    await _enqueue(db)
    accepting = _runtime(db, compiled)
    await accepting.refresh()
    accepting._schedule_integration_pending = lambda _rows: None
    assert await accepting.accept_integration_event(
        "integration.sealed",
        {"project_id": "p", "operation_id": "operation-1"},
        "event-1",
    )
    await accepting.shutdown()

    _new_activation, new_sha = await _activate(
        db, compiled, "integration-train", "integration.sealed", source_digit="2"
    )
    assert new_sha != old_sha
    restarted = _runtime(db, compiled)
    await restarted.refresh()
    await _wait_for_pending_resolution(db, "event-1")
    await restarted.shutdown()

    async with db._engine.connect() as conn:
        runs = (await conn.execute(select(playbook_v2_runs))).mappings().all()
    assert [row["artifact_sha256"] for row in runs] == [old_sha]


async def test_new_operation_event_uses_frozen_owner_and_current_sibling_artifacts(
    db, tmp_path
):
    compiled = tmp_path / "compiled"
    owner_activation, owner_old_sha = await _activate(
        db, compiled, "hierarchical-delivery", "integration.sealed", source_digit="1"
    )
    _sibling_activation, sibling_sha = await _activate(
        db, compiled, "integration-observer", "integration.sealed", source_digit="3"
    )
    await _pin_operation_route(
        db,
        operation_id="operation-1",
        playbook_id="hierarchical-delivery",
        artifact_sha256=owner_old_sha,
        activation_id=owner_activation,
    )
    _same_owner_activation, owner_new_sha = await _activate(
        db, compiled, "hierarchical-delivery", "integration.sealed", source_digit="2"
    )
    assert owner_new_sha != owner_old_sha
    await _enqueue(db)
    runtime = _runtime(db, compiled)
    await runtime.refresh()
    runtime._schedule_integration_pending = lambda _rows: None

    assert await runtime.accept_integration_event(
        "integration.sealed",
        {"project_id": "p", "operation_id": "operation-1"},
        "event-1",
    )
    await runtime.shutdown()

    pending = {row["playbook_id"]: row for row in await _pending_rows(db)}
    assert pending["hierarchical-delivery"]["artifact_sha256"] == owner_old_sha
    assert pending["integration-observer"]["artifact_sha256"] == sibling_sha
    assert owner_new_sha not in {row["artifact_sha256"] for row in pending.values()}


async def test_disabled_frozen_owner_still_accepts_new_operation_event(
    db, tmp_path
):
    compiled = tmp_path / "compiled"
    owner_activation, owner_sha = await _activate(
        db, compiled, "hierarchical-delivery", "integration.sealed", source_digit="1"
    )
    sibling_activation, sibling_sha = await _activate(
        db, compiled, "integration-observer", "integration.sealed", source_digit="3"
    )
    await _pin_operation_route(
        db,
        operation_id="operation-1",
        playbook_id="hierarchical-delivery",
        artifact_sha256=owner_sha,
        activation_id=owner_activation,
    )
    await db.set_playbook_activation(
        playbook_id="hierarchical-delivery",
        scope="project",
        scope_identifier="p",
        artifact_sha256=owner_sha,
        enabled=False,
        activated_by="test",
        health="disabled",
        reasons="[]",
    )
    await _enqueue(db)
    runtime = _runtime(db, compiled)
    await runtime.refresh()

    assert await runtime.accept_integration_event(
        "integration.sealed",
        {"project_id": "p", "operation_id": "operation-1"},
        "event-1",
    )
    state = await load_acceptance_state(db, "event-1")
    assert state.manifest == tuple(
        sorted(
            (
                {
                    "activation_id": owner_activation,
                    "playbook_id": "hierarchical-delivery",
                    "scope": "project",
                    "scope_identifier": "p",
                    "artifact_sha256": owner_sha,
                },
                {
                    "activation_id": sibling_activation,
                    "playbook_id": "integration-observer",
                    "scope": "project",
                    "scope_identifier": "p",
                    "artifact_sha256": sibling_sha,
                },
            ),
            key=lambda item: (item["activation_id"], item["artifact_sha256"]),
        )
    )
    pending = {row["activation_id"]: row for row in await _pending_rows(db)}
    assert pending[owner_activation]["artifact_sha256"] == owner_sha
    assert pending[sibling_activation]["artifact_sha256"] == sibling_sha
    await runtime.shutdown()


async def test_operation_pin_is_a_reference_for_gc_file_rechecks(db, tmp_path):
    compiled = tmp_path / "compiled"
    activation_id, old_sha = await _activate(
        db, compiled, "hierarchical-delivery", "integration.sealed", source_digit="1"
    )
    await _pin_operation_route(
        db,
        operation_id="operation-1",
        playbook_id="hierarchical-delivery",
        artifact_sha256=old_sha,
        activation_id=activation_id,
    )
    await _activate(
        db, compiled, "hierarchical-delivery", "integration.sealed", source_digit="2"
    )

    collected = await db.collect_playbook_artifacts(
        NOW + 1_000_000, min_versions=0, limit=100
    )
    assert old_sha not in {sha for sha, _path in collected}
    # The file collector re-checks references after the artifact row has been
    # deleted.  Simulate a SQLite deployment without FK enforcement so the
    # normalized operation pin must protect the hash on its own.
    async with db._engine.connect() as conn:
        await conn.execute(text("PRAGMA foreign_keys = OFF"))
        await conn.execute(
            delete(playbook_artifacts).where(
                playbook_artifacts.c.artifact_sha256 == old_sha
            )
        )
        await conn.commit()

    assert await db.filter_referenced_artifact_shas([old_sha]) == {old_sha}


async def test_restart_reconciler_drains_more_than_one_bounded_page(
    db, tmp_path, monkeypatch
):
    # The property under test is paging: the restart reconciler must keep
    # refilling its bounded replay slots until every retained row has been
    # dispatched, not stop after the first page.  A small page keeps the test
    # about paging rather than SQLite throughput — every dispatch is a real
    # playbook run behind the serialized ``immediate()`` writer lock, so 101
    # events at the production page size need well over the 10s wait on a
    # loaded runner.
    monkeypatch.setattr(runtime_module, "_INTEGRATION_REPLAY_PAGE_SIZE", 4)
    total = 9  # two full pages and a partial third
    compiled = tmp_path / "compiled"
    activation_id, artifact_sha256 = await _activate(
        db, compiled, "integration-train", "integration.sealed"
    )
    for ordinal in range(total):
        event_id = f"event-{ordinal:03d}"
        await db.retain_integration_event(
            playbook_id="integration-train",
            activation_id=activation_id,
            artifact_sha256=artifact_sha256,
            scope="project",
            scope_identifier="p",
            event_type="integration.sealed",
            event={
                "_event_type": "integration.sealed",
                "event_id": event_id,
                "project_id": "p",
                "operation_id": f"operation-{ordinal:03d}",
            },
            event_id=event_id,
            now=NOW + ordinal,
        )

    restarted = _runtime(db, compiled)
    await restarted.refresh()
    async with asyncio.timeout(10):
        while (
            sum(
                row["resolution"] == "dispatched"
                for row in await _pending_rows(db)
            )
            < total
        ):
            await asyncio.sleep(0.02)
    await restarted.shutdown()

    assert await _accepted_activation_count(db, f"event-{total - 1:03d}") == 1
    assert sum(row["resolution"] == "dispatched" for row in await _pending_rows(db)) == total


async def test_artifact_retention_keeps_pending_pin(db, tmp_path):
    compiled = tmp_path / "compiled"
    activation_id, old_sha = await _activate(
        db, compiled, "integration-train", "integration.sealed", source_digit="1"
    )
    await _enqueue(db)
    accepting = _runtime(db, compiled)
    await accepting.refresh()
    accepting._integration_reconciler_task.cancel()
    await asyncio.gather(accepting._integration_reconciler_task, return_exceptions=True)
    accepting._integration_reconciler_task = None
    accepting._integration_wakeup.clear()
    accepting._schedule_integration_pending = lambda _rows: None
    assert await accepting.accept_integration_event(
        "integration.sealed",
        {"project_id": "p", "operation_id": "operation-1"},
        "event-1",
    )

    await _activate(
        db, compiled, "integration-train", "integration.sealed", source_digit="2"
    )
    collected = await db.collect_playbook_artifacts(
        NOW + 1_000_000, min_versions=0, limit=100
    )

    assert old_sha not in {sha for sha, _path in collected}
    [pending] = await _pending_rows(db)
    assert pending["activation_id"] == activation_id
    assert pending["artifact_sha256"] == old_sha


async def test_artifact_retention_keeps_unaccepted_manifest_pin(db, tmp_path):
    compiled = tmp_path / "compiled"
    activation_id, old_sha = await _activate(
        db, compiled, "integration-train", "integration.sealed", source_digit="1"
    )
    await _enqueue(db)
    await freeze_destination_manifest(
        db,
        "event-1",
        [
            {
                "activation_id": activation_id,
                "playbook_id": "integration-train",
                "scope": "project",
                "scope_identifier": "p",
                "artifact_sha256": old_sha,
            }
        ],
    )
    [pin] = await _artifact_pin_rows(db)
    assert pin["artifact_sha256"] == old_sha
    await _activate(
        db, compiled, "integration-train", "integration.sealed", source_digit="2"
    )

    collected = await db.collect_playbook_artifacts(
        NOW + 1_000_000, min_versions=0, limit=100
    )

    assert old_sha not in {sha for sha, _path in collected}


async def test_artifact_gc_retains_a_candidate_on_concurrent_fk_conflict(db, tmp_path):
    compiled = tmp_path / "compiled"
    _activation_id, old_sha = await _activate(
        db, compiled, "integration-train", "integration.sealed", source_digit="1"
    )
    await _activate(
        db, compiled, "integration-train", "integration.sealed", source_digit="2"
    )
    await _enqueue(db)
    async with db.immediate() as conn:
        await conn.execute(
            text(
                "CREATE TRIGGER test_concurrent_artifact_pin BEFORE DELETE ON "
                f"playbook_artifacts WHEN OLD.artifact_sha256 = '{old_sha}' BEGIN "
                "INSERT INTO integration_outbox_artifact_pins(event_id, artifact_sha256) "
                "VALUES ('event-1', OLD.artifact_sha256); END"
            )
        )

    collected = await db.collect_playbook_artifacts(
        NOW + 1_000_000, min_versions=0, limit=100
    )

    assert old_sha not in {sha for sha, _path in collected}
    assert await db.get_playbook_artifact(old_sha) is not None


async def test_gc_win_does_not_freeze_an_unusable_artifact_manifest(db, tmp_path):
    compiled = tmp_path / "compiled"
    _activation_id, old_sha = await _activate(
        db, compiled, "integration-train", "integration.sealed", source_digit="1"
    )
    await _enqueue(db)
    accepting = _runtime(db, compiled)
    await accepting.refresh()
    accepting._integration_reconciler_task.cancel()
    await asyncio.gather(accepting._integration_reconciler_task, return_exceptions=True)
    accepting._integration_reconciler_task = None
    accepting._integration_wakeup.clear()
    accepting._schedule_integration_pending = lambda _rows: None

    _same_activation, new_sha = await _activate(
        db, compiled, "integration-train", "integration.sealed", source_digit="2"
    )
    collected = await db.collect_playbook_artifacts(
        NOW + 1_000_000, min_versions=0, limit=100
    )
    assert old_sha in {sha for sha, _path in collected}

    payload = {"project_id": "p", "operation_id": "operation-1"}
    assert not await accepting.accept_integration_event(
        "integration.sealed", payload, "event-1"
    )
    assert (await load_acceptance_state(db, "event-1")).manifest is None
    assert await accepting.accept_integration_event(
        "integration.sealed", payload, "event-1"
    )
    [pending] = await _pending_rows(db)
    assert pending["artifact_sha256"] == new_sha


async def test_reconciler_recovers_after_transient_pending_list_failure(db, tmp_path):
    compiled = tmp_path / "compiled"
    activation_id, artifact_sha256 = await _activate(
        db, compiled, "integration-train", "integration.sealed"
    )
    await db.retain_integration_event(
        playbook_id="integration-train",
        activation_id=activation_id,
        artifact_sha256=artifact_sha256,
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
    original = db.list_pending_integration_events
    fail_once = True

    async def transient_failure(**kwargs):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("transient pending read")
        return await original(**kwargs)

    db.list_pending_integration_events = transient_failure
    runtime = _runtime(db, compiled)
    await runtime.refresh()
    try:
        async with asyncio.timeout(2):
            while not (await _pending_rows(db))[0]["resolved_at"]:
                await asyncio.sleep(0.02)
    finally:
        await runtime.shutdown()

    assert (await _pending_rows(db))[0]["resolution"] == "dispatched"


async def test_refresh_restarts_a_completed_reconciler(db, tmp_path):
    runtime = _runtime(db, tmp_path / "compiled")
    await runtime.refresh()
    stopped = runtime._integration_reconciler_task
    stopped.cancel()
    await asyncio.gather(stopped, return_exceptions=True)
    assert stopped.cancelled()

    await runtime.refresh()
    restarted = runtime._integration_reconciler_task
    try:
        assert restarted is not stopped
        assert not restarted.done()
    finally:
        await runtime.shutdown()


async def test_acceptance_cursor_cannot_regress(db):
    await _enqueue(db)
    async with db.immediate() as conn:
        await conn.execute(
            integration_outbox.update()
            .where(integration_outbox.c.id == "event-1")
            .values(acceptance_cursor=1)
        )

    with pytest.raises(IntegrityError, match="acceptance cursor cannot decrease"):
        async with db.immediate() as conn:
            await conn.execute(
                integration_outbox.update()
                .where(integration_outbox.c.id == "event-1")
                .values(acceptance_cursor=0)
            )


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
