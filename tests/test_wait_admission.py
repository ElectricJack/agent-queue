"""Real PostgreSQL admission bindings, deadlines, claim predicates and restart replay."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, insert, select, update

from src.commands.principal import ExecutionPrincipal
from src.config import AppConfig
from src.config_wait_policy import parse_wait_policy
from src.database import Database
from src.database.tables import (
    admission_reservations, integration_branch_owners, projects, tasks, wait_deadlines,
    wait_escalations, wait_records,
)
from src.models import Agent, AgentProfile, Project, RepoConfig, RepoSourceType, Task, TaskStatus
from src.integration.admission import observe_admission
from src.integration.stacked_branches import StackView
from src.waits.admission import AdmissionReader
from src.waits.conditions import Scope, WaitError
from src.waits.graph import GRAPH_LOCK, recompute_from
from src.waits.registry import build_registry
from src.waits.reservations import consume_reservation
from src.wait_runtime.scheduler import SchedulerWaitService, close_row
from tests.db_fixtures import lease_dsn

NOW = 1_800_000_000.0
SERVICE = ExecutionPrincipal.service("test-scheduler")


@pytest.fixture
async def env():
    db = Database(lease_dsn("wait-admission"))
    await db.initialize()
    await db.create_project(Project(id="p", name="P"))
    await db.create_repo(RepoConfig(id="repo", project_id="p", url="https://example.test/repo",
                                    source_type=RepoSourceType.CLONE, checkout_base_path="/tmp/admission-test"))
    await db.update_project("p", integration_repository_id="repo")
    await db.create_profile(AgentProfile(id="worker", name="Worker", lifecycle="pool"))
    await db.create_agent(Agent(id="worker-agent", name="Worker", profile_id="worker"))
    await db.create_task(Task(id="holder", project_id="p", title="Holder", description="",
                              status=TaskStatus.READY, profile_id="worker", route_source="override",
                              branch_name="aq/holder", priority=100))
    async with db._engine.begin() as conn:
        await conn.execute(insert(integration_branch_owners).values(
            id="branch", repository_id="repo", ref="aq/holder", owner_id="holder",
            owner_role="task", fence_token=1, handoff_state="reserved", created_at=NOW, updated_at=NOW,
        ))
    config = AppConfig()
    config.wait_policy.v2_enabled = config.wait_policy.scheduler_waits_enabled = True
    o = SimpleNamespace(db=db, config=config, bus=SimpleNamespace(emit=AsyncMock()),
                        _worktrees_enabled=lambda: False,
                        _pool_quarantine_state=lambda *a: (None, None))
    async def admission(ids):
        return await observe_admission(db, ids, None)

    o._delivery_admission = admission
    service = SchedulerWaitService(o, clock=lambda: NOW)
    yield SimpleNamespace(db=db, o=o, service=service)
    await db.close()


async def rows(env, table):
    async with env.db._engine.connect() as conn:
        return [dict(r) for r in (await conn.execute(select(table))).mappings()]


async def sync(env, now=NOW):
    return await env.service.sync({"project_id": "p", "cycle_id": str(now), "blockers": [],
                                   "now": now}, principal=SERVICE)


async def test_scheduler_sync_reads_stack_inputs_once_per_project_per_pass(env, monkeypatch):
    for i in range(4):
        await env.db.create_task(Task(id=f"candidate-{i}", project_id="p", title="Candidate",
                                     description="", status=TaskStatus.READY))
    read = AsyncMock(return_value=())
    monkeypatch.setattr("src.integration.stacked_branches._inputs", read)
    view = StackView(env.db, "p")
    view.proofs = {"source": {"task_id": "source"}}

    async def observe(conn, task_id, **kwargs):
        # Admission verifies the same project inputs at both read boundaries.
        await view.verified_on(conn)
        await view.verified_on(conn)

    observed = AsyncMock(side_effect=observe)
    env.service.reader = SimpleNamespace(observe_task=observed)
    assert (await sync(env))["success"]
    assert observed.await_count == 5
    assert read.await_count == 1
    assert (await sync(env, NOW + 1))["success"]
    assert observed.await_count == 10
    assert read.await_count == 2
    async with env.db._engine.connect() as conn:
        await view.verified_on(conn)
        await view.verified_on(conn)
    assert read.await_count == 4


async def test_empty_stack_verification_does_not_read_project_history(env, monkeypatch):
    read = AsyncMock(side_effect=AssertionError("empty proof set scanned project history"))
    monkeypatch.setattr("src.integration.stacked_branches._inputs", read)
    async with env.db._engine.connect() as conn:
        assert await StackView(env.db, "p").verified_on(conn) == {}
    read.assert_not_awaited()


async def fire(env, deadline, now):
    return await env.service.deadline({
        "wait_id": deadline["wait_id"], "deadline_type": deadline["deadline_type"],
        "generation": deadline["generation"], "now": now,
    }, principal=SERVICE)


async def test_branch_deadline_survives_restart_and_escalates_once(env):
    await sync(env)
    first = (await rows(env, wait_deadlines))[0]
    assert first["deadline_type"] == "reservation" and first["due_at"] == NOW + 1800
    assert not await rows(env, admission_reservations)
    await sync(env, NOW + 50)
    assert (await rows(env, wait_deadlines))[0]["due_at"] == first["due_at"]
    env.service = SchedulerWaitService(env.o, clock=lambda: NOW)
    assert not (await fire(env, first, NOW + 1799))["fired"]
    assert (await fire(env, first, NOW + 1800))["fired"]
    assert not (await fire(env, first, NOW + 1800))["fired"]
    assert len(await rows(env, admission_reservations)) == 1
    second = next(d for d in await rows(env, wait_deadlines) if d["deadline_type"] == "escalation")
    assert second["due_at"] == NOW + 3600
    assert not (await fire(env, second, NOW + 3599))["fired"]
    assert (await fire(env, second, NOW + 3600))["fired"]
    assert not (await fire(env, second, NOW + 3601))["fired"]
    assert len(await rows(env, wait_escalations)) == 1
    assert (await rows(env, admission_reservations))[0]["state"] == "overdue"
    env.o.bus.emit.assert_awaited_once()
    assert env.o.bus.emit.call_args.args[1]["project_id"] == "p"


async def test_resource_reader_reobserves_and_rejects_rebinding(env):
    reader = AdmissionReader(env.o, clock=lambda: NOW)
    registry = build_registry(resource_reader=reader)
    async with env.db._engine.begin() as conn:
        observation = await reader.observe_task(conn, "holder")
        condition = next(c for c in observation.conditions() if c.parameters["blocker"] == "workspace")
        scope = Scope("p", "scheduler", "holder", principal="service", claim_epoch=0)
        evidence = await registry.verify(conn, condition, scope)
        assert evidence.source.generation == 0
        assert evidence.observation.state == "unsatisfied"
        # A config change is a new binding, never satisfaction of the old source.
        env.o.config.resources.max_concurrent_agents += 1
        with pytest.raises(WaitError, match="source could not be verified"):
            await registry.verify(conn, evidence.condition, scope)
        env.o.config.resources.max_concurrent_agents -= 1
        with pytest.raises(WaitError):
            await registry.verify(conn, evidence.condition, replace(scope, claim_epoch=1))
        with pytest.raises(WaitError):
            await registry.verify(conn, evidence.condition, replace(scope, project_id="foreign"))


async def test_scheduler_clears_suspended_subject_and_never_renews_deadline(env):
    await sync(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "holder").values(status="HIBERNATED"))
    await sync(env, NOW + 20)
    assert all(r["outcome"] != "pending" for r in await rows(env, wait_records))
    assert all(d["cancelled_at"] == NOW + 20 for d in await rows(env, wait_deadlines))


async def test_reserved_task_still_obeys_frontier_and_claim_consumes_atomically(env):
    await sync(env)
    await fire(env, (await rows(env, wait_deadlines))[0], NOW + 1800)
    async with env.db._engine.begin() as conn:
        assert await env.db.select_reserved_for_profile(
            conn, project_id="p", profile_id="other", agent_id="worker-agent",
        ) is None
        assert await env.db.select_reserved_for_profile(
            conn, project_id="p", profile_id="worker", agent_id="worker-agent", allowed_task_ids=set(),
        ) is None
        selected = await env.db.select_reserved_for_profile(
            conn, project_id="p", profile_id="worker", agent_id="worker-agent",
        )
        assert selected["task_id"] == "holder"
        task = await env.db.take_task(conn, "holder", agent_id="worker-agent", now=NOW + 1801)
        assert task is not None
        await consume_reservation(conn, selected["reservation_id"], "session", now=NOW + 1801)
    reservation = (await rows(env, admission_reservations))[0]
    assert reservation["state"] == "consumed" and reservation["consumed_by_session_id"] == "session"


async def test_disabled_service_admits_no_new_waits(env):
    env.o.config.wait_policy.scheduler_waits_enabled = False
    await sync(env)
    assert not await rows(env, wait_records)
    assert not await rows(env, wait_deadlines)


async def test_scheduler_rollback_retracts_branch_reservations_but_preserves_resume(env):
    from src.database.tables import continuations

    await sync(env)
    await fire(env, (await rows(env, wait_deadlines))[0], NOW + 1800)
    wait = (await rows(env, wait_records))[0]
    async with env.db._engine.begin() as conn:
        await conn.execute(insert(continuations).values(
            id="resume", task_id="holder", wait_id=wait["id"], suspension_generation=1,
            facts={}, branch="aq/holder", prior_status="IN_PROGRESS", held_cursor_seq=0,
            resume_state="queued", created_at=NOW,
        ))
        await conn.execute(insert(admission_reservations).values(
            id="resume-seat", continuation_id="resume", project_id="p", scope={},
            effective_priority=100, provenance="resume", deadline_at=NOW + 3000,
            state="pending", created_at=NOW,
        ))
    env.o.config.wait_policy.scheduler_waits_enabled = False
    await sync(env, NOW + 1801)
    reservations = {r["id"]: r for r in await rows(env, admission_reservations)}
    assert reservations.pop("resume-seat")["state"] == "pending"
    assert all(r["state"] == "cancelled" for r in reservations.values())
    assert all(r["cleared_at"] is not None for r in await rows(env, wait_records))


async def test_admission_reader_preserves_ingestion_hold(env):
    reader = AdmissionReader(env.o, clock=lambda: NOW)
    async with env.db._engine.begin() as conn:
        free = await reader.observe_task(conn, "holder")
        assert not free.constraints["policy_hold"]["blocked"]
        await conn.execute(update(tasks).where(tasks.c.id == "holder").values(
            held_batch_id="awaiting-fidelity-review",
        ))
        held = await reader.observe_task(conn, "holder")
        assert held.constraints["policy_hold"]["blocked"]
        assert "ingest_held" in held.constraints["policy_hold"]["reasons"]
        assert held.snapshot("policy_hold").observation.state == "unsatisfied"
        assert "policy_hold" in {condition.parameters["blocker"] for condition in held.conditions()}
    async with env.db._engine.connect() as conn:
        assert await conn.scalar(select(tasks.c.held_batch_id).where(
            tasks.c.id == "holder",
        )) == "awaiting-fidelity-review"


def test_policy_defaults_enable_cutover_and_string_flags_refused():
    policy = AppConfig().wait_policy
    assert policy.v2_enabled and policy.hibernation_enabled and policy.scheduler_waits_enabled
    assert policy.v1_bodies == "refuse"
    assert policy.branch_admission_deadline_seconds == 1800
    assert not policy.validate()
    assert parse_wait_policy({"v2_enabled": "false"}).validate()
    assert parse_wait_policy({"branch_admission_deadline_seconds": 1}).validate()
    assert parse_wait_policy({"projects": {"p": {"hibernate_margin": 0.5}}}).validate()


async def test_workspace_release_reobserves_same_identity_and_retracts_wait(env):
    from src.models import Workspace

    reader = AdmissionReader(env.o, clock=lambda: NOW)
    registry = build_registry(resource_reader=reader)
    async with env.db._engine.connect() as conn:
        observation = await reader.observe_task(conn, "holder")
        condition = next(c for c in observation.conditions() if c.parameters["blocker"] == "workspace")
        evidence = await registry.verify(conn, condition, Scope(
            "p", "scheduler", "holder", principal="service", claim_epoch=0,
        ))
    await env.db.create_workspace(Workspace(
        id="available", project_id="p", workspace_path="/tmp/admission-workspace",
        source_type=RepoSourceType.CLONE, kind_id="project-repo",
    ))
    async with env.db._engine.connect() as conn:
        observed = await registry.observe(conn, evidence.condition)
        assert observed.state == "satisfied" and observed.result_ref
        assert observed.revision != evidence.observation.revision
    await sync(env)
    assert all(r["condition"]["blocker"] != "workspace" for r in await rows(env, wait_records))


async def test_config_reload_preserves_first_and_second_branch_deadlines(env):
    await sync(env)
    first = (await rows(env, wait_deadlines))[0]
    env.o.config.resources.max_concurrent_agents += 1
    await sync(env, NOW + 100)
    live = next(d for d in await rows(env, wait_deadlines) if d["cancelled_at"] is None)
    assert live["wait_id"] != first["wait_id"] and live["due_at"] == first["due_at"]
    await fire(env, live, NOW + 1800)
    env.o.config.resources.max_concurrent_agents += 1
    await sync(env, NOW + 2000)
    second = next(d for d in await rows(env, wait_deadlines)
                  if d["cancelled_at"] is None and d["deadline_type"] == "escalation")
    assert second["due_at"] == NOW + 3600
    await fire(env, second, NOW + 3600)
    assert len(await rows(env, wait_escalations)) == 1


async def test_reservation_refuses_hold_backoff_and_stale_scope(env):
    from src.database.tables import task_labels, task_metadata

    await sync(env)
    await fire(env, (await rows(env, wait_deadlines))[0], NOW + 1800)
    async with env.db._engine.begin() as conn:
        await conn.execute(insert(task_labels).values(task_id="holder", label="hold:manual"))
        assert await env.db.select_reserved_for_profile(
            conn, project_id="p", profile_id="worker", agent_id="worker-agent",
        ) is None
        from sqlalchemy import delete
        await conn.execute(delete(task_labels).where(task_labels.c.task_id == "holder"))
        await conn.execute(insert(task_metadata).values(
            task_id="holder", key="claim_prepare_backoff_until", value="9999999999",
        ))
        assert await env.db.select_reserved_for_profile(
            conn, project_id="p", profile_id="worker", agent_id="worker-agent",
        ) is None
        await conn.execute(delete(task_metadata).where(task_metadata.c.task_id == "holder"))
        await conn.execute(update(tasks).where(tasks.c.id == "holder").values(claim_epoch=2))
        assert await env.db.select_reserved_for_profile(
            conn, project_id="p", profile_id="worker", agent_id="worker-agent",
        ) is None
    await sync(env, NOW + 1801)
    assert (await rows(env, admission_reservations))[0]["state"] == "cancelled"


async def test_resume_seat_is_counted_once_before_session_and_after_launch(env):
    from src.database.tables import continuations
    from src.models import SessionRecord
    from src.waits.occupancy import continuation_occupancy

    await sync(env)
    wait_id = (await rows(env, wait_records))[0]["id"]
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "holder").values(status="RESUME_PENDING"))
        await conn.execute(insert(continuations).values(
            id="continuation", task_id="holder", wait_id=wait_id, suspension_generation=1,
            facts={}, branch="aq/holder", prior_status="IN_PROGRESS", held_cursor_seq=0,
            resume_state="reserved",
        ))
        await conn.execute(insert(admission_reservations).values(
            id="resume", project_id="p", task_id="holder", continuation_id="continuation",
            scope={"profile_id": "worker"}, effective_priority=100, provenance="resume",
            deadline_at=NOW + 1800, state="granted", created_at=NOW,
        ))
    assert await continuation_occupancy(env.db, env.o.config) == (frozenset(), {("p", "worker"): 1})
    await env.db.create_session(SessionRecord(
        id="resumed", project_id="p", task_id="holder", profile_id="worker", harness="codex",
        provider="fake", name="resumed", lifecycle="pool", work_dir="/tmp/resume", epoch="e",
        instance_token="t", started_at=NOW, state="starting",
    ))
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).where(continuations.c.id == "continuation").values(
            new_session_id="resumed", resume_state="launching",
        ))
    assert await continuation_occupancy(env.db, env.o.config) == (frozenset({"resumed"}), {})


def test_policy_schema_serializes_nested_defaults():
    import json
    from src.config_editor import build_config_schema

    policy = json.loads(json.dumps(build_config_schema()))["properties"]["wait_policy"]
    assert policy["properties"]["kinds"]["default"]["resource"]["escalate_after_seconds"] == 1800
    assert policy["x-reload"] == "hot"


async def test_in_place_wait_is_evictable_but_keeps_capacity(env):
    from src.database.queries.wait_queries import register_wait
    from src.scheduler import PoolKey, PoolSupply, size_pools
    from src.waits.conditions import SleepDecision
    from src.waits.occupancy import evictable_sessions
    from tests.test_wait_queries import Policy, owner

    await owner(env.db, "active", "active-session", "active-agent")
    async with env.db._engine.begin() as conn:
        await register_wait(
            conn, scope=Scope("p", "task", "active", session_id="active-session",
                              instance_token="token", claim_epoch=1),
            condition={"kind": "task", "task_id": "holder"}, idempotency_key="in-place",
            registry=build_registry(), policy=Policy(), now=NOW, source_watermark=0,
            decide_sleep=lambda c, e: SleepDecision("in_place", "test"),
        )
        await conn.execute(update(tasks).where(tasks.c.id == "active").values(status="WAITING"))
    assert await evictable_sessions(env.db, env.o.config) == frozenset({"active-session"})
    key = PoolKey("worker")
    actions, _ = size_pools(
        supply={key: PoolSupply(running_busy=1, evictable=1)}, demand={key: 1},
        bounds={key: (0, 1)}, global_cap=1, surplus_since={}, now=NOW,
        scale_down_grace=60, max_starts_per_tick=1, max_drains_per_tick=1,
    )
    # A checkpoint request does not fund a new process: the wait still owns
    # its seat until the stop/save/park transaction completes.
    assert [(action.kind, action.count) for action in actions] == [('evict', 1)]


async def test_priority_donation_orders_reserved_blocker_before_ordinary_work(env):
    from src.database.queries.wait_queries import register_wait
    from src.waits.conditions import SleepDecision
    from tests.test_wait_queries import Policy

    await env.db.create_task(Task(id="donor", project_id="p", title="Donor", description="",
                                  priority=20))
    await env.db.create_task(Task(id="ordinary", project_id="p", title="Ordinary", description="",
                                  priority=10, status=TaskStatus.READY,
                                  profile_id="worker", route_source="override"))
    async with env.db._engine.begin() as conn:
        await register_wait(
            conn, scope=Scope("p", "scheduler", "donor", principal="service"),
            condition={"kind": "task", "task_id": "holder"}, idempotency_key="donation",
            registry=build_registry(), policy=Policy(), now=NOW, source_watermark=0,
            decide_sleep=lambda c, e: SleepDecision("in_place", "test"),
        )
    await sync(env)
    due = next(d for d in await rows(env, wait_deadlines) if d["deadline_type"] == "reservation")
    await fire(env, due, NOW + 1800)
    assert (await rows(env, admission_reservations))[0]["effective_priority"] == 20
    from src.waits.reservations import claim_candidate
    async with env.db._engine.begin() as conn:
        tid, reservation = await claim_candidate(SimpleNamespace(db=env.db, config=env.o.config), conn,
            project_id="p", profile_id="worker", agent_id="worker-agent")
        assert tid == "holder" and reservation


async def test_equal_priority_reservations_use_fifo_even_when_task_ids_sort_otherwise(env):
    await env.db.create_task(Task(id="aaa-newer", project_id="p", title="Newer", description="",
                                  priority=100, status=TaskStatus.READY,
                                  profile_id="worker", route_source="override"))
    await sync(env)
    await fire(env, (await rows(env, wait_deadlines))[0], NOW + 1800)
    original = (await rows(env, admission_reservations))[0]
    async with env.db._engine.begin() as conn:
        await conn.execute(insert(admission_reservations).values(
            id="newer-reservation", project_id="p", task_id="aaa-newer",
            scope=original["scope"], effective_priority=100, provenance="branch_blocker",
            state="pending", created_at=NOW + 1801, deadline_at=NOW + 3601,
        ))
        selected = await env.db.select_reserved_for_profile(
            conn, project_id="p", profile_id="worker", agent_id="worker-agent",
        )
        assert selected["task_id"] == "holder"


async def test_combined_claim_skips_locked_reservation_and_preserves_allowlist(env):
    from src.waits.reservations import claim_candidate

    await env.db.create_task(Task(
        id="ordinary", project_id="p", title="Ordinary", description="",
        status=TaskStatus.READY, profile_id="worker", route_source="override",
    ))
    await sync(env)
    await fire(env, (await rows(env, wait_deadlines))[0], NOW + 1800)
    handler = SimpleNamespace(db=env.db, config=env.o.config)
    args = dict(project_id="p", profile_id="worker", agent_id="worker-agent")
    async with env.db._engine.begin() as locked:
        await locked.execute(select(tasks.c.id).where(
            tasks.c.id == "holder",
        ).with_for_update())
        async with env.db._engine.begin() as claimant:
            assert await claim_candidate(handler, claimant, **args) == ("ordinary", None)
        async with env.db._engine.begin() as claimant:
            assert await claim_candidate(
                handler, claimant, **args, allowed_task_ids={"holder"},
            ) == (None, None)
    async with env.db._engine.begin() as claimant:
        task_id, reservation_id = await claim_candidate(handler, claimant, **args)
        assert task_id == "holder" and reservation_id


async def test_released_branch_owner_retracts_scheduler_priority_donation(env):
    await env.db.create_task(Task(id="urgent", project_id="p", title="Urgent", description="",
                                  priority=10, status=TaskStatus.READY, branch_name="aq/holder",
                                  profile_id="worker", route_source="override"))
    await sync(env)
    assert next(r["effective_priority"] for r in await rows(env, tasks) if r["id"] == "holder") == 10
    async with env.db._engine.begin() as conn:
        await conn.execute(update(integration_branch_owners).where(
            integration_branch_owners.c.id == "branch",
        ).values(handoff_state="released"))
    await sync(env, NOW + 1)
    assert next(r["effective_priority"] for r in await rows(env, tasks) if r["id"] == "holder") == 100


@pytest.mark.parametrize('status', ['READY', 'DEFINED', 'PAUSED'])
async def test_initial_audit_unknown_age_is_due_without_admitting_paused_tasks(env, status):
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == 'holder').values(status=status))
    await env.service.sync({'project_id': 'p', 'cycle_id': 'bridge', 'blockers': [],
                            'initial_audit': True, 'now': NOW}, principal=SERVICE)
    records = await rows(env, wait_records)
    assert records and all(row['decision_inputs']['age_unknown'] for row in records)
    deadlines = await rows(env, wait_deadlines)
    assert all(row['due_at'] <= NOW for row in deadlines if row['deadline_type'] == 'reservation')
    if status != 'READY':
        assert not deadlines
        holds = [row for row in records if row['condition']['blocker'] == 'policy_hold']
        assert holds
    async with env.db._engine.connect() as conn:
        assert await conn.scalar(select(tasks.c.status).where(tasks.c.id == 'holder')) == status
        if status != 'READY':
            assert await env.service.reader.observe_task(conn, 'holder') is None


async def test_bridge_project_pass_records_completed_audit_and_replays(env):
    from src.commands.principal import TRUSTED_LOCAL
    from src.database.tables import wait_bridge_progress
    from src.wait_runtime.service import WaitService

    service = WaitService(env.o, clock=lambda: NOW)
    args = {'project_id': 'p', 'mode': 'project', 'apply': True}
    first = await service.legacy_bridge.run(args, principal=TRUSTED_LOCAL)
    assert first['result']['projects']['p']['state'] == 'complete'
    deadlines = await rows(env, wait_deadlines)
    await service.legacy_bridge.run(args, principal=TRUSTED_LOCAL)
    assert await rows(env, wait_deadlines) == deadlines
    marker = await rows(env, wait_bridge_progress)
    assert len(marker) == 1 and marker[0]['state'] == 'complete'


async def test_startup_bridge_runs_once_and_restart_honours_durable_marker(env):
    from src.commands.principal import TRUSTED_LOCAL
    from src.database.tables import wait_bridge_progress
    from src.wait_runtime.service import WaitService

    service = WaitService(env.o, clock=lambda: NOW)
    async def command(name, args):
        assert name == 'wait_legacy_bridge'
        return await service.legacy_bridge.run(args, principal=TRUSTED_LOCAL)
    service.command = AsyncMock(side_effect=command)
    await service.startup_bridge()
    await service.startup_bridge()
    assert service.command.await_count == 1
    assert any(r['phase'] == 'agent-waits' and r['identity'] == '*' and r['state'] == 'complete'
               for r in await rows(env, wait_bridge_progress))
    # A new composition root relies on the durable receipt, not process memory.
    service = WaitService(SimpleNamespace(db=env.db, config=env.o.config), clock=lambda: NOW)
    service.command = AsyncMock(side_effect=command)
    await service.startup_bridge()
    service.command.assert_not_awaited()


async def test_startup_bridge_backs_off_a_held_project_instead_of_rerunning_every_tick(env):
    from src.wait_runtime.service import WaitService

    clock = NOW
    service = WaitService(env.o, clock=lambda: clock)
    service.command = AsyncMock(return_value={
        'success': True, 'result': {'projects': {'p': {'state': 'held'}}},
    })
    await service.startup_bridge()
    await service.startup_bridge()
    assert service.command.await_count == 1
    clock = NOW + service.legacy_bridge.RETRY_SECONDS + 1
    await service.startup_bridge()
    assert service.command.await_count == 2


@pytest.mark.parametrize("running", ["background", "foreground"])
async def test_wait_tick_never_queues_behind_a_running_bridge(env, monkeypatch, running):
    from src.wait_runtime.service import WaitService

    service = WaitService(env.o, clock=lambda: NOW)
    monkeypatch.setattr(service, 'processing_enabled', AsyncMock(return_value=True))
    monkeypatch.setattr(service, 'startup_bridge', AsyncMock())
    monkeypatch.setattr(service.lifecycle, 'tick', AsyncMock())
    monkeypatch.setattr(service.listener, 'tick', AsyncMock(return_value=True))
    release = asyncio.Event()
    if running == "background":
        service._bridge_task = asyncio.create_task(release.wait())
    else:
        await service._bridge_lock.acquire()
    try:
        assert await asyncio.wait_for(service.tick(), timeout=2) is True
        service.startup_bridge.assert_not_awaited()
    finally:
        release.set()
        if running == "background":
            await service._bridge_task
        else:
            service._bridge_lock.release()
async def test_scheduler_sync_uses_one_connection_under_graph_fence(env):
    from sqlalchemy.ext.asyncio import create_async_engine

    original = env.db._engine
    engine = create_async_engine(original.url, pool_size=1, max_overflow=0, pool_timeout=0.2)
    env.db._engine = engine
    try:
        assert (await sync(env))["success"]
        assert await rows(env, wait_records)
    finally:
        env.db._engine = original
        await engine.dispose()


async def test_scheduler_dirty_projects_and_recovery_scan(env, monkeypatch):
    from src.event_bus import EventBus

    bus = env.o.bus = EventBus(validate_events=False)
    service = SchedulerWaitService(env.o)
    projects = [SimpleNamespace(id="p"), SimpleNamespace(id="other")]
    monkeypatch.setattr("src.wait_runtime.scheduler.time.monotonic", lambda: 100)
    assert service.due(projects) == projects
    service._dirty.clear()
    service._last_sync = {p.id: 100 for p in projects}
    assert service.due(projects) == []
    await bus.emit("task.closed", {"project_id": "p"})
    assert service.due(projects) == projects[:1]
    service._dirty.clear()
    await bus.emit("config.updated", {})
    assert service.due(projects) == projects
    service._dirty.clear()
    monkeypatch.setattr("src.wait_runtime.scheduler.time.monotonic", lambda: 160)
    assert service.due(projects) == projects
    service._unsubscribe()


async def test_prepared_admission_does_not_admit_changed_project(env):
    reader = env.service.reader
    async with reader.prepared("p"):
        async with env.db._engine.begin() as conn:
            await conn.execute(update(projects).where(projects.c.id == "p").values(
                hierarchical_integration_generation=projects.c.hierarchical_integration_generation + 1,
            ))
            observation = await reader.observe_task(conn, "holder")
    assert observation.constraints["batch_hold"]["blocked"]
    assert "delivery_not_verified" in observation.constraints["batch_hold"]["reasons"]




@pytest.mark.parametrize("entrypoint", ["sync", "startup_bridge"])
async def test_slow_authorization_yields_graph_lock_between_owners(env, monkeypatch, entrypoint):
    from src.wait_runtime.service import WaitService

    await env.db.create_task(Task(
        id="z-last", project_id="p", title="Last", description="",
        status=TaskStatus.READY, profile_id="worker", route_source="override",
    ))
    service = WaitService(env.o, clock=lambda: NOW)
    scheduler = service.scheduler
    started, release = asyncio.Event(), asyncio.Event()
    queued, mutated = asyncio.Event(), asyncio.Event()
    observe = scheduler.reader.observe_task

    async def slow_authorization(conn, task_id, **kwargs):
        if task_id == "holder" and not started.is_set():
            started.set()
            await release.wait()
        if task_id == "z-last":
            # The concurrent mutation must commit before this next slow item
            # can finish. A project-wide transaction keeps it locked out.
            await asyncio.wait_for(mutated.wait(), timeout=10)
        return await observe(conn, task_id, **kwargs)

    monkeypatch.setattr(scheduler.reader, "observe_task", slow_authorization)

    async def mutate_wait():
        await started.wait()
        async with env.db._engine.begin() as conn:
            queued.set()
            # Queue on the real PostgreSQL fence while authorization is slow.
            # PostgreSQL hands the fence to this writer at the item boundary.
            await conn.execute(select(func.pg_advisory_xact_lock(GRAPH_LOCK)))
            row = (await conn.execute(select(wait_records).where(
                wait_records.c.owner_id == "holder", wait_records.c.origin == "scheduler",
            ).with_for_update())).mappings().first()
            assert row is not None
            changed = await close_row(conn, row, now=NOW + 1)
            await recompute_from(conn, changed, now=NOW + 1)
        mutated.set()

    async def command(name, args):
        assert name == "wait_legacy_bridge"
        return await service.legacy_bridge.run(args, principal=SERVICE)

    service.command = command
    work = asyncio.create_task(
        scheduler.sync({"project_id": "p", "cycle_id": "concurrent"}, principal=SERVICE)
        if entrypoint == "sync" else service.startup_bridge()
    )
    mutation = asyncio.create_task(mutate_wait())
    try:
        await asyncio.wait_for(queued.wait(), timeout=10)
        release.set()
        await asyncio.wait_for(asyncio.gather(work, mutation), timeout=20)
    finally:
        release.set()
        work.cancel()
        mutation.cancel()
        await asyncio.gather(work, mutation, return_exceptions=True)
    assert mutated.is_set()
    assert any(r["owner_id"] == "holder" and r["outcome"] == "cancelled"
               for r in await rows(env, wait_records))


async def test_interrupted_sync_replays_committed_owner_receipts(env, monkeypatch):
    await env.db.create_task(Task(
        id="z-last", project_id="p", title="Last", description="",
        status=TaskStatus.READY, profile_id="worker", route_source="override",
    ))
    observe = env.service.reader.observe_task

    async def interrupted(conn, task_id, **kwargs):
        if task_id == "z-last":
            raise RuntimeError("interrupted after first owner committed")
        return await observe(conn, task_id, **kwargs)

    monkeypatch.setattr(env.service.reader, "observe_task", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        await env.service.sync({"project_id": "p", "cycle_id": "first", "now": NOW,
                                "initial_audit": True}, principal=SERVICE)
    before = await rows(env, wait_records)
    assert before and all(r["owner_id"] == "holder" for r in before)
    assert all("legacy_audit" in r["decision_inputs"] for r in before)
    deadlines = await rows(env, wait_deadlines)
    env.service = SchedulerWaitService(env.o, clock=lambda: NOW + 50)
    await env.service.sync({"project_id": "p", "cycle_id": "replay", "now": NOW + 50,
                            "initial_audit": True}, principal=SERVICE)
    after = [r for r in await rows(env, wait_records) if r["owner_id"] == "holder"]
    assert {r["id"] for r in after} == {r["id"] for r in before}
    assert {r["id"]: r["decision_inputs"]["registration_receipt"] for r in after} == {
        r["id"]: r["decision_inputs"]["registration_receipt"] for r in before}
    assert await rows(env, wait_deadlines) == deadlines
async def test_tick_runs_startup_bridge_beside_the_scheduler(env, monkeypatch):
    import asyncio
    from src.wait_runtime.service import WaitService

    import src.wait_runtime.service as service_module

    service = WaitService(env.o, clock=lambda: NOW)
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def slow_bridge():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    monkeypatch.setattr(service_module, "BRIDGE_TICK_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(WaitService, "enabled", property(lambda self: True))
    service.processing_enabled = AsyncMock(return_value=True)
    service.startup_bridge = slow_bridge
    service.lifecycle.tick = AsyncMock()
    service.listener.tick = AsyncMock(return_value=False)
    await asyncio.wait_for(service.tick(), 1)
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(service.tick(), 1)
    assert calls == 1
    release.set()
    await service._bridge_task
