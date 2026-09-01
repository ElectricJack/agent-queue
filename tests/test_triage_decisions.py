"""Authenticated routing decisions are immutable and commit atomically."""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from src.api.auth import LOCAL_SCOPE, RequestScope
from src.database import Database
from src.models import AgentState, Project, Task
from src.triage.models import RoutingChoice, TriagePrincipal
from tests.triage_support import TriageCase
from tests.pg_dsn import ensure_worker_postgres_dsn


POSTGRES_DSN = ensure_worker_postgres_dsn()


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "triage.db"))
    await database.initialize()
    try:
        yield database
    finally:
        await database.close()


def choice(case: TriageCase, **changes) -> RoutingChoice:
    values = {
        "task_id": case.task_id,
        "execution_type_key": case.type_key,
        "expected_revision": case.revision,
        "reason": "Bounded work",
    }
    values.update(changes)
    return RoutingChoice(**values)


async def test_completion_is_idempotent_and_preserves_other_gates(db):
    case = await TriageCase.create(db)
    human, _ = await db.create_gate(
        "p", "human", "Approval", waiter_task_ids=[case.task_id]
    )
    first = await case.service.complete(case.principal, choice(case))
    again = await case.service.complete(case.principal, choice(case))
    assert first["decision_id"] == again["decision_id"]
    assert (await db.get_gate(case.gate_id))["status"] == "resolved"
    assert (await db.get_gate(human))["status"] == "open"
    assert (await db.get_task(case.task_id)).is_blocked


async def test_conflicting_duplicate_route_is_rejected_without_reopening_gate(db):
    case = await TriageCase.create(db)
    first = await case.service.complete(case.principal, choice(case))
    conflict = await case.service.complete(
        case.principal, choice(case, execution_type_key="f" * 64)
    )
    assert first["success"] is True
    assert conflict["success"] is False and conflict["code"] == "decision_conflict"
    assert (await db.get_task(case.task_id)).routing_decision_id == first["decision_id"]


async def test_same_type_with_different_reason_conflicts(db):
    case = await TriageCase.create(db)
    first = await case.service.complete(case.principal, choice(case))
    conflict = await case.service.complete(
        case.principal, choice(case, reason="A materially different justification")
    )
    assert first["success"] is True
    assert conflict["success"] is False and conflict["code"] == "decision_conflict"


async def test_wrong_project_and_dead_or_stale_session_are_rejected(db):
    case = await TriageCase.create(db)
    await db.create_project(Project("other", "Other"))
    wrong = await case.service.complete(
        TriagePrincipal("other", case.principal.run_id, case.principal.session_id,
                        case.principal.instance_token),
        choice(case),
    )
    assert wrong["code"] == "unauthorized"
    stale = await case.service.complete(
        TriagePrincipal("p", case.principal.run_id, case.principal.session_id, "stale"),
        choice(case),
    )
    assert stale["code"] == "unauthorized"
    await db.update_session(case.principal.session_id, state="stopped")
    dead = await case.service.complete(case.principal, choice(case))
    assert dead["code"] == "unauthorized"
    assert (await db.get_gate(case.gate_id))["status"] == "open"


async def test_absent_type_is_rejected_but_busy_only_type_is_valid(db):
    case = await TriageCase.create(db)
    await db.update_agent("worker-1", enabled=False)
    absent = await case.service.complete(case.principal, choice(case))
    assert absent["code"] == "execution_type_unavailable"
    await db.create_task(Task("occupied", "p", "Occupied", ""))
    await db.update_agent(
        "worker-1", enabled=True, state=AgentState.BUSY, current_task_id="occupied"
    )
    routed = await case.service.complete(case.principal, choice(case))
    assert routed["success"] is True


async def test_structured_request_mismatch_never_records_decision(db):
    case = await TriageCase.create(db)
    await db.update_task(case.task_id, routing_request={"model": "required-model"})
    result = await case.service.complete(case.principal, choice(case))
    assert result["success"] is False and result["code"] == "constraint_mismatch"
    assert (await db.get_task(case.task_id)).routing_decision_id is None
    assert (await db.get_gate(case.gate_id))["status"] == "open"


@pytest.mark.parametrize(
    "changes",
    [
        {"task_id": ""},
        {"execution_type_key": ""},
        {"expected_revision": "1"},
        {"expected_revision": True},
        {"reason": "   "},
    ],
)
async def test_choice_requires_strict_nonempty_inputs_before_transaction(db, changes):
    case = await TriageCase.create(db)
    result = await case.service.complete(case.principal, choice(case, **changes))
    assert result["success"] is False and result["code"] == "invalid_request"
    assert (await db.get_gate(case.gate_id))["status"] == "open"


async def test_revision_and_active_execution_are_fenced(db):
    case = await TriageCase.create(db)
    stale = await case.service.complete(case.principal, choice(case, expected_revision=2))
    assert stale["code"] == "stale_revision"
    await db.update_task(case.task_id, assigned_agent_id="worker-1")
    active = await case.service.complete(case.principal, choice(case))
    assert active["code"] == "active_execution"


async def test_direct_local_operator_scope_cannot_authenticate_as_triage(db):
    case = await TriageCase.create(db)
    local = await case.service.authenticate(LOCAL_SCOPE)
    assert local["success"] is False and local["code"] == "unauthorized"
    missing_evidence = await case.service.authenticate(
        RequestScope(kind="session", session_id=case.principal.session_id, project_id="p")
    )
    assert missing_evidence["success"] is False
    fabricated = await case.service.authenticate({
        "kind": "session", "session_id": case.principal.session_id, "project_id": "p",
        "instance_token": case.principal.instance_token,
    })
    assert fabricated["success"] is False
    verified = await case.service.authenticate(RequestScope(
        kind="session",
        session_id=case.principal.session_id,
        project_id="p",
        instance_token=case.principal.instance_token,
    ))
    assert verified == case.principal
    stale = await case.service.authenticate(RequestScope(
        kind="session",
        session_id=case.principal.session_id,
        project_id="p",
        instance_token="stale-instance",
    ))
    assert stale["success"] is False


async def test_defer_is_durable_and_keeps_routing_gate_open(db):
    case = await TriageCase.create(db)
    first = await case.service.defer(
        case.principal, case.task_id, case.revision, "No compatible specialist"
    )
    again = await case.service.defer(
        case.principal, case.task_id, case.revision, "No compatible specialist"
    )
    assert first["success"] is True and again["deferral_id"] == first["deferral_id"]
    saved = await db.get_routing_deferral(first["deferral_id"])
    assert saved["routing_revision"] == 1
    assert saved["catalog_generation"] and saved["policy_generation"]
    assert saved["reason"] == "No compatible specialist"
    assert (await db.get_gate(case.gate_id))["status"] == "open"


async def test_busy_to_idle_changes_deferral_catalog_generation(db):
    case = await TriageCase.create(db)
    await db.create_task(Task("occupied", "p", "Occupied", ""))
    await db.update_agent(
        "worker-1", state=AgentState.BUSY, current_task_id="occupied"
    )
    busy = await case.service.defer(
        case.principal, case.task_id, case.revision, "No idle capacity"
    )
    await db.update_agent("worker-1", state=AgentState.IDLE, current_task_id=None)
    idle = await case.service.defer(
        case.principal, case.task_id, case.revision, "No idle capacity"
    )
    assert idle["catalog_generation"] != busy["catalog_generation"]
    assert idle["deferral_id"] != busy["deferral_id"]


async def test_concurrent_definition_edit_cannot_create_unvalidated_snapshot(db):
    case = await TriageCase.create(db)
    routed, _ = await asyncio.gather(
        case.service.complete(case.principal, choice(case)),
        db.update_profile("worker", model="changed-model"),
    )
    if routed["success"]:
        saved = await db.get_routing_decision(routed["decision_id"])
        assert saved["execution_snapshot"]["model"] == "fixture-model"
    else:
        assert routed["code"] == "execution_type_unavailable"
        assert (await db.get_task(case.task_id)).routing_decision_id is None


async def test_post_commit_event_failure_does_not_recast_committed_decision_as_failure(db):
    case = await TriageCase.create(db)

    async def broken_event_sink(_name, _payload):
        raise RuntimeError("event bus unavailable")

    case.service.event_callback = broken_event_sink
    result = await case.service.complete(case.principal, choice(case))
    assert result["success"] is True
    assert (await db.get_task(case.task_id)).routing_decision_id == result["decision_id"]


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not configured")
async def test_postgresql_decision_and_deferral_round_trip():
    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

    database = PostgreSQLDatabaseAdapter(POSTGRES_DSN)
    await database.initialize()
    await database.reset_for_tests()
    try:
        case = await TriageCase.create(database)
        deferred = await case.service.defer(
            case.principal, case.task_id, case.revision, "Waiting for catalog policy"
        )
        routed = await case.service.complete(case.principal, choice(case))
        assert (await database.get_routing_deferral(deferred["deferral_id"]))["reason"] == (
            "Waiting for catalog policy"
        )
        saved = await database.get_routing_decision(routed["decision_id"])
        assert saved["execution_snapshot"]["model"] == "fixture-model"
        again = await case.service.complete(case.principal, choice(case))
        assert again["decision_id"] == routed["decision_id"]

        await database.create_task(Task("concurrent", "p", "Concurrent", ""))
        await database.create_gate(
            "p", "routing", "Route concurrent", waiter_task_ids=["concurrent"]
        )
        concurrent_choice = RoutingChoice(
            "concurrent", case.type_key, 1, "Validated against one locked snapshot"
        )
        concurrent, _ = await asyncio.gather(
            case.service.complete(case.principal, concurrent_choice),
            database.update_profile("worker", model="changed-model"),
        )
        if concurrent["success"]:
            decision = await database.get_routing_decision(concurrent["decision_id"])
            assert decision["execution_snapshot"]["model"] == "fixture-model"
        else:
            assert concurrent["code"] == "execution_type_unavailable"
    finally:
        await database.close()


def test_deferral_migration_downgrade_and_upgrade_round_trip(tmp_path):
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    engine = sa.create_engine(url)
    cfg = Config("alembic.ini")
    try:
        with engine.connect() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            conn.commit()
            assert sa.inspect(conn).has_table("task_routing_deferrals")
            command.downgrade(cfg, "72671cc2b86c")
            conn.commit()
            assert not sa.inspect(conn).has_table("task_routing_deferrals")
            command.upgrade(cfg, "head")
            conn.commit()
            assert sa.inspect(conn).has_table("task_routing_deferrals")
    finally:
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not configured")
async def test_postgresql_deferral_migration_downgrade_and_upgrade_round_trip():
    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

    database = PostgreSQLDatabaseAdapter(POSTGRES_DSN)
    await database.initialize()

    def migrate(sync_conn, target, *, downgrade=False):
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = sync_conn
        (command.downgrade if downgrade else command.upgrade)(cfg, target)

    try:
        async with database._engine.connect() as conn:
            await conn.run_sync(lambda sync: migrate(sync, "72671cc2b86c", downgrade=True))
            await conn.commit()
            assert not await conn.run_sync(
                lambda sync: sa.inspect(sync).has_table("task_routing_deferrals")
            )
            await conn.run_sync(lambda sync: migrate(sync, "head"))
            await conn.commit()
            assert await conn.run_sync(
                lambda sync: sa.inspect(sync).has_table("task_routing_deferrals")
            )
    finally:
        await database.close()
