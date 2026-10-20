"""Design §15 acceptance matrix across persistence, delivery and lifecycle boundaries.

All protocol scenarios use disposable PostgreSQL. Clocks advance explicitly; a
restart discards the service object, never the database. Provider calls stand in
for turns in the deterministic arm; the tmux arm counts actual model requests.
"""

import asyncio
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.config import AppConfig
from src.database.queries.wait_queries import register_wait
from src.database.tables import (
    admission_reservations,
    continuations,
    escalations,
    messages,
    sessions,
    tasks,
    wait_deadlines,
    wait_edges,
    wait_escalations,
    wait_listener_cursors,
    wait_measurements,
    wait_records,
)
from src.messages.delivery import MessageDeliveryEngine
from src.models import Task, TaskStatus
from src.sessions.reconciler import SessionReconciler
from src.wait_runtime.scheduler import SchedulerWaitService
from src.waits.conditions import Scope, SleepDecision, WaitError
from src.waits.mail import acknowledge_in_place
from src.waits.reservations import claim_candidate
from tests.test_wait_admission import env as _admission_env, fire, sync
from tests.test_wait_commands import execute, human_runtime
from tests.test_wait_decisions import complete_and_ack, decide, escalated, service_for
from tests.test_wait_hibernation import (
    bound_resume, capture, env as _hibernation_env, fallback, intent, park,
)
from tests.test_wait_queries import NOW, Policy, env as _env, owner, records, register
from tests.test_tmux_harness_wake import (
    SPECS, _delivered, _provider, _start_idle, api as _api, cli_home as _cli_home,
    harness_id as _harness_id, socket_name as _socket_name,
)

env = _env
admission_env = _admission_env
hibernation_env = _hibernation_env
api = _api
cli_home = _cli_home
harness_id = _harness_id
socket_name = _socket_name


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class Turns:
    """Count every terminal submit by session, including an accidental patrol."""

    def __init__(self):
        self.counts = Counter()
        self.activity = AsyncMock(return_value="idle")
        self.ensure_started = AsyncMock()

    async def nudge(self, *, target_id, **kwargs):
        self.counts[target_id] += 1
        return True


async def routine_mail(db, scope, kind="report_request"):
    return await db.create_message(
        project_id="p", from_kind="system", from_id="patrol", to_kind="session",
        to_id=scope.session_id, body="Patrol or digest reminder", body_kind=kind,
    )


async def one_outcome(env, outcome="satisfied", *, wakes=1):
    rows = await records(env)
    assert len(rows) == 1
    assert rows[0]["outcome"] == outcome
    assert len(await records(env, wait_measurements)) == 1
    notices = [m for m in await records(env, messages)
               if m["body_kind"] in {"wait_outcome", "wait_decision"}]
    assert len(notices) == wakes
    return {k: rows[0][k] for k in ("outcome", "resolved_at", "result_ref")}


@pytest.mark.parametrize("condition,code", [
    ({"kind": "task", "task_id": "missing"}, "wait.source_unresolved"),
    ({"kind": "task", "task_id": "foreign"}, "wait.out_of_scope"),
    ({"kind": "task", "task_id": "source", "any": []}, "wait.condition_required"),
    ({"kind": "task", "task_id": "owner"}, "wait.cycle"),
], ids=["missing", "foreign", "mixed-predicates", "cycle"])
async def test_matrix_invalid_condition_has_no_side_effect_even_on_retry(env, condition, code):
    for _ in range(2):
        with pytest.raises(WaitError) as error:
            await register(env, condition)
        assert error.value.code == code
        for table in (wait_records, wait_edges, wait_deadlines, continuations, messages):
            assert await records(env, table) == []


async def test_matrix_source_resolves_before_registration_preserves_result_pointer(env):
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    pointers = []
    for _ in range(2):
        with pytest.raises(WaitError) as error:
            await register(env)
        assert error.value.code == "wait.already_satisfied"
        pointers.append(error.value.details["result_ref"])
    assert pointers[0] == pointers[1] and pointers[0].startswith("task:source@")
    assert not await records(env)
    assert not await records(env, messages)


@pytest.mark.parametrize("crash", ["before-dispatch", "inside-dispatch", "after-commit"])
async def test_matrix_daemon_crash_and_double_listeners_keep_one_immutable_outcome(env, crash):
    now = [NOW + 1]
    receipt = await register(env)
    first = service_for(env, now)
    assert await first.lease()
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    if crash == "inside-dispatch":
        # Fail after the outcome/message writes but before their transaction commits.
        original = first.dispatcher.commit

        async def abort(*args, **kwargs):
            result = await original(*args, **kwargs)
            assert result["committed"]
            raise RuntimeError("injected process death before commit")

        first.dispatcher.commit = abort
        with pytest.raises(RuntimeError, match="injected process death"):
            await first.tick()
        assert (await records(env))[0]["outcome"] == "pending"
        assert not await records(env, wait_measurements)
        assert not await records(env, messages)
        assert (await records(env, wait_listener_cursors))[0]["last_seq"] == 0
    elif crash == "after-commit":
        assert await first.tick()
        committed = await one_outcome(env)
    # No clean lease release: both replacements start after the dead lease expires.
    now[0] += 61
    contenders = [service_for(env, now), service_for(env, now)]
    leases = await asyncio.gather(*(service.lease() for service in contenders))
    assert sorted(leases) == [False, True]
    winner = contenders[leases.index(True)]
    assert await winner.tick()
    outcome = await one_outcome(env)
    if crash == "after-commit":
        assert outcome == committed
    assert await register(env) == receipt
    await env.db.wait_source_commit(source_key="task:source", generation=0,
                                   revision="duplicate-observation", payload={},
                                   outcome_ref="must-not-replace-the-result")
    assert await winner.tick()
    assert await one_outcome(env) == outcome


async def test_matrix_nine_sessions_zero_wait_induced_turns_then_one_outcome_each(env, monkeypatch):
    holders = [env.scope] + [await owner(env.db, f"owner-{n}", f"s-{n}", f"a-{n}")
                             for n in range(1, 9)]
    receipts = [await register(env, scope=scope) for scope in holders]
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id.in_(
            [scope.owner_id for scope in holders])).values(status="WAITING"))
    clock = Clock()
    monkeypatch.setattr("src.messages.delivery.time.time", clock)
    turns = Turns()
    config = AppConfig()
    config.agents_config.stuck_timeout_seconds = 60
    rec = SessionReconciler(env.db, config, providers=SimpleNamespace(),
                            orchestrator=SimpleNamespace())
    engine = MessageDeliveryEngine(env.db, turns, config)
    held = {scope.session_id: [] for scope in holders}
    for elapsed in (1800, 7200, 86400):
        clock.now = NOW + elapsed
        live = [await env.db.get_session(scope.session_id) for scope in holders]
        activity = [row.last_activity for row in live]
        for scope in holders:
            for kind in ("report_request", "conversation_input"):
                held[scope.session_id].append(await routine_mail(env.db, scope, kind))
        await rec._step_stall_ladder(live, clock.now)
        await rec._step_backstop(live, clock.now)
        await rec._step_orphans(live, clock.now)
        await engine.run_delivery_pass()
        assert all(turns.counts[scope.session_id] == 0 for scope in holders)
        current = [await env.db.get_session(scope.session_id) for scope in holders]
        assert [row.last_activity for row in current] == activity
        assert all(row.state == "running" for row in current)
    turns.activity.assert_not_awaited()
    now = [clock.now]
    service = service_for(env, now)
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await service.tick()
    await engine.run_delivery_pass()
    clock.now += 1000
    await engine.run_delivery_pass()
    assert {scope.session_id: turns.counts[scope.session_id] for scope in holders} == {
        scope.session_id: 1 for scope in holders}
    for scope, receipt in zip(holders, receipts, strict=True):
        for message in held[scope.session_id]:
            assert (await env.db.get_message(message.id)).delivered_at is None
        async with env.db._engine.begin() as conn:
            ack = await acknowledge_in_place(conn, wait_id=receipt["wait_id"],
                                             scope=scope, now=clock.now)
        assert {m["id"] for m in ack["held_messages"]} == {
            m.id for m in held[scope.session_id]}
    assert len(await records(env, wait_measurements)) == 9


async def test_matrix_escalation_extend_and_concurrent_wakes_preserve_age(env):
    receipt, service, now, args = await escalated(env)
    before = (await records(env))[0]
    assert not [m for m in await records(env, messages) if m["to_id"] == "s"]
    assert (await decide(service, receipt, "extend", extend_seconds=120,
                         expected_generation=1))["success"]
    now[0] += 120
    assert await service.lease()
    assert (await service.command("wait_deadline_fire", {**args, "generation": 2}))["success"]
    results = await asyncio.gather(*(decide(service, receipt, "wake", expected_generation=2)
                                     for _ in range(2)))
    assert sum(r["success"] for r in results) == 1
    after = (await records(env))[0]
    assert after["created_at"] == before["created_at"]
    assert after["ceiling_anchor_at"] == before["ceiling_anchor_at"]
    outcome = await one_outcome(env, "cancelled")
    restarted = service_for(env, now)
    assert not (await decide(restarted, receipt, "wake", expected_generation=2))["success"]
    assert await one_outcome(env, "cancelled") == outcome


async def test_matrix_repeated_condition_hibernates_even_across_service_restart(env):
    now = [NOW + 1]
    for number in range(2):
        service = service_for(env, now)
        receipt = await register(env, idempotency_key=f"loop-{number}", now=now[0])
        await complete_and_ack(env, service, receipt, now)
        await service.lease(release=True)
    service_for(env, now)
    receipt = await register(env, idempotency_key="loop-3", now=now[0])
    assert receipt["decision"]["mode"] == "hibernate"
    assert receipt["decision"]["reason"] == "wait.loop_detected"
    assert await register(env, idempotency_key="loop-3", now=now[0]) == receipt
    assert len(await records(env, wait_escalations)) == 1


async def test_matrix_human_days_of_digest_and_supervisor_reply_require_explicit_lift(env):
    from tests.test_digest_queries import WINDOW

    service, handler, principal, receipt = await human_runtime(env)
    for day in (1, 2, 5):
        now = NOW + day * 86400
        facts = await env.db.collect_digest_supervisor_facts(WINDOW, now=now, project_ids=("p",))
        assert [row["task_id"] for row in facts["waiting_on_you"]] == ["owner"]
        assert (await records(env))[0]["outcome"] == "pending"
    await env.db.add_task_comment("owner", "Supervisor relays an answer",
                                   author_kind="supervisor", author_id="supervisor-p")
    assert await service.tick()
    assert (await records(env))[0]["outcome"] == "pending"
    service.human.clock = lambda: NOW + 5 * 86400
    lifted = await execute(handler, "wait_lift", {
        "wait_id": receipt["wait_id"], "response": "The human approved the scope.",
        "expected_generation": receipt["generation"],
    }, principal)
    assert lifted["success"] and lifted["outcome"] == "lifted_by_supervisor"
    assert (await records(env, continuations))[0]["resume_state"] == "queued"
    assert not [m for m in await records(env, messages) if m["body_kind"] == "wait_outcome"]
    assert not [d for d in await records(env, wait_deadlines)
                if d["deadline_type"] in {"due", "ceiling", "escalation"}]


async def test_matrix_repair_priority_20_reservation_and_deadline_survive_restart(admission_env):
    env = admission_env
    await env.db.create_task(Task(id="donor", project_id="p", title="Downstream", description="",
                                  priority=20))
    async with env.db._engine.begin() as conn:
        await register_wait(
            conn, scope=Scope("p", "scheduler", "donor", principal="service"),
            condition={"kind": "task", "task_id": "holder"}, idempotency_key="donor",
            registry=env.service.registry, policy=Policy(), now=NOW, source_watermark=0,
            decide_sleep=lambda *_: SleepDecision("in_place", "test"))
    await sync(env)
    task = await env.db.get_task("holder")
    assert task.priority == 100
    async with env.db._engine.connect() as conn:
        assert await conn.scalar(select(tasks.c.effective_priority).where(
            tasks.c.id == "holder")) == 20
    deadline = next(d for d in await records(env, wait_deadlines)
                    if d["deadline_type"] == "reservation")
    assert deadline["due_at"] == NOW + 1800
    env.service = SchedulerWaitService(env.o, clock=lambda: NOW + 100)
    await sync(env, NOW + 100)
    assert (await fire(env, deadline, NOW + 1800))["fired"]
    assert not (await fire(env, deadline, NOW + 1801))["fired"]
    reservations = await records(env, admission_reservations)
    assert len(reservations) == 1 and reservations[0]["effective_priority"] == 20
    escalation = next(d for d in await records(env, wait_deadlines)
                      if d["deadline_type"] == "escalation" and d["wait_id"] == deadline["wait_id"])
    result = await fire(env, escalation, NOW + 3600)
    assert result.get("fired"), result
    assert not (await fire(env, escalation, NOW + 3601))["fired"]
    assert len(await records(env, wait_escalations)) == 1
    async with env.db._engine.begin() as conn:
        task_id, reservation = await claim_candidate(env.o, conn, project_id="p",
                                                     profile_id="worker", agent_id="worker-agent")
    assert task_id == "holder" and reservation


async def suspension_driver(env, monkeypatch, *, failure=None, on_stop=None):
    """Fake only process/git I/O; retain the actual stop, save and park protocol."""
    from src.wait_runtime.hibernate import HibernationTransaction
    from src.waits.registry import build_registry

    clock = Clock(NOW + 1)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(
            branch_name="aq/owner", status="WAITING"))
    workspace = SimpleNamespace(id="ws", workspace_path="/tmp/wait-v2", is_slot=False)
    monkeypatch.setattr(env.db, "get_workspace_for_task", AsyncMock(return_value=workspace))
    monkeypatch.setattr(env.db, "get_project", AsyncMock(return_value=SimpleNamespace(
        integration_repository_id="repo")))
    monkeypatch.setattr(env.db, "get_repo", AsyncMock(return_value=SimpleNamespace(
        url="remote", default_branch="main")))
    order = []

    async def stop(handle):
        order.append("stop")
        if on_stop:
            await on_stop()
        if failure == "stop":
            raise OSError("injected unproven stop")

    async def save(*args, **kwargs):
        order.append("push")
        assert "stop" in order
        return SimpleNamespace(status="failed" if failure == "push" else "pushed",
                               commit="a" * 40, error="injected push failure")

    async def release(workspace):
        order.append("release")
        assert (await intent(env))["stopped_at"] is not None
        assert (await env.db.get_session("s")).state == "stopped"

    provider = SimpleNamespace(stop=stop, confirm_instance_stopped=AsyncMock(return_value=True),
                               process_alive=AsyncMock(return_value=False))
    monkeypatch.setattr("src.orchestrator.stranded_work.save_wip_to_task_branch", save)
    monkeypatch.setattr("src.orchestrator.workspace_attachments."
                        "detach_workspace_for_integration_handoff",
                        AsyncMock(return_value=True))
    monkeypatch.setattr("src.wait_runtime.hibernate.remove_claim_file_if_matches", lambda *a: True)
    config = AppConfig()
    config.wait_policy.hibernation_enabled = True
    config.wait_policy.transition_budget_seconds = 120
    orch = SimpleNamespace(
        db=env.db, config=config, _task_control_lock=lambda _: asyncio.Lock(),
        session_providers=SimpleNamespace(create=lambda *a: provider),
        session_reconciler=SimpleNamespace(_process_names=lambda _: ("codex",)),
        git=SimpleNamespace(resolve_commit_identity=lambda _: None,
                            arev_parse=AsyncMock(return_value="a" * 40)),
        bus=None, _git_mutex=None, token_store=SimpleNamespace(revoke_session=AsyncMock()),
        _release_workspace_and_cleanup=AsyncMock(side_effect=release))
    command_service = service_for(SimpleNamespace(db=env.db, registry=build_registry(clock=clock)),
                                  [clock.now])
    handler = SimpleNamespace(wait_escalation_create=AsyncMock(
        wraps=command_service.orch._command_handler.wait_escalation_create))
    driver = HibernationTransaction(SimpleNamespace(orch=orch), clock=clock)
    return driver, handler, clock, order


async def test_matrix_source_resolves_during_stop_never_overlaps_writable_sessions(
    hibernation_env, monkeypatch,
):
    from src.waits.registry import build_registry

    env = hibernation_env
    env.registry = build_registry(clock=lambda: NOW)
    await capture(env)
    now = [NOW + 1]
    service = service_for(env, now)

    async def resolve_during_stop():
        await env.db.transition_task("source", TaskStatus.COMPLETED)
        assert await service.tick()
        await one_outcome(env, wakes=0)
        assert (await intent(env))["resume_state"] == "sleeping"
        assert (await intent(env))["new_session_id"] is None
        assert (await env.db.get_session("s")).task_id == "owner"

    driver, handler, _, order = await suspension_driver(
        env, monkeypatch, on_stop=resolve_during_stop)
    assert (await driver.advance(env.wait_id, handler=handler))["parked"]
    assert order == ["stop", "push", "release"]
    assert await service.tick()
    outcome = await one_outcome(env, wakes=0)
    assert (await intent(env))["resume_state"] == "queued"
    assert (await env.db.get_task("owner")).claim_epoch == 2
    live = [r for r in await records(env, sessions) if r["state"] == "running"]
    assert live == []
    assert await service.tick()
    assert await one_outcome(env, wakes=0) == outcome
    assert len(await records(env, continuations)) == 1


@pytest.mark.parametrize("failure", ["push", "stop", "summary"])
async def test_matrix_failed_hibernation_retains_claim_and_replay_cannot_discard_work(
    hibernation_env, monkeypatch, failure,
):
    env = hibernation_env
    if failure != "summary":
        await capture(env)
    driver, handler, clock, order = await suspension_driver(env, monkeypatch, failure=failure)
    first = await driver.advance(env.wait_id, handler=handler)
    if failure == "summary":
        assert first["next_step"] == "checkpoint_then_end_turn"
        assert order == []
        clock.now = NOW + 120
        degraded = await fallback(env)
        assert degraded["success"]
        assert (await intent(env))["summary_degraded"]
        # A missing authored summary never fabricates checkpoint prose.
        assert "Goal: resume" not in ((await intent(env))["summary"] or "")
    elif failure == "push":
        assert first["push_pending"]
        clock.now = NOW + 122
        assert (await driver.advance(env.wait_id, handler=handler))["held"]
    else:
        assert first["held"]
    row = await intent(env)
    assert row["stopped_at"] is None and row["saved_head"] is None
    assert (await env.db.get_task("owner")).claim_epoch == 1
    driver.orch._release_workspace_and_cleanup.assert_not_awaited()
    driver.orch.token_store.revoke_session.assert_not_awaited()
    if failure != "summary":
        assert (await driver.advance(env.wait_id, handler=handler))["held"]
        handler.wait_escalation_create.assert_awaited_once()


async def test_matrix_ceiling_and_eviction_suspend_once_with_inherited_anchor(env):
    from src.wait_runtime.eviction import candidate
    from src.waits.decision import ceiling_anchor

    now = [NOW + 100]
    service = service_for(env, now)
    service.orch.config.wait_policy.hibernation_enabled = True
    first = await register(env)
    second_scope = await owner(env.db, "second", "s2", "a2")
    second = await register(env, scope=second_scope, now=NOW + 50)
    async with env.db._engine.begin() as conn:
        for task_id in ("owner", "second"):
            await conn.execute(update(tasks).where(tasks.c.id == task_id).values(
                branch_name=f"aq/{task_id}", status="WAITING"))
        assert (await candidate(conn, "p", "worker-codex"))["id"] == second["wait_id"]
    assert (await service.command("wait_evict", {
        "wait_id": second["wait_id"], "reason": "pressure"}))["suspended"]
    assert not (await service.command("wait_evict", {
        "wait_id": second["wait_id"], "reason": "pressure"}))["suspended"]
    # Ceiling on the remaining waiter uses the same suspension boundary.
    now[0] = NOW + 1800
    async with env.db._engine.connect() as conn:
        assert await ceiling_anchor(conn, env.scope, now=now[0], policy=Policy()) == NOW
    assert await service.lease()
    result = await service.command("wait_deadline_fire", {
        "wait_id": first["wait_id"], "deadline_type": "ceiling", "generation": 1})
    assert result["success"], result
    rows = {row["owner_id"]: row for row in await records(env)}
    assert rows["owner"]["decision_reason"] == "ceiling"
    assert rows["second"]["decision_reason"] == "evicted"
    assert all(row["outcome"] == "pending" for row in rows.values())
    assert rows["owner"]["ceiling_anchor_at"] == NOW
    assert len(await records(env, continuations)) == 2
    assert [m["body_kind"] for m in await records(env, messages)] == [
        "wait_suspend", "wait_suspend"]
    assert not await records(env, wait_escalations)


async def test_matrix_eviction_reclaims_and_grants_reservation_in_same_call(
    hibernation_env, monkeypatch,
):
    from src.database.tables import integration_branch_owners
    from src.models import AgentProfile, RepoConfig, RepoSourceType, Workspace
    from src.wait_runtime.lifecycle import WaitLifecycle
    from src.waits.reservations import consume_reservation

    env = hibernation_env
    await env.db.create_profile(AgentProfile(id="worker-codex", name="Worker",
                                            lifecycle="pool", max_active=1))
    await env.db.create_repo(RepoConfig(id="repo", project_id="p", url="https://example.test/repo",
        source_type=RepoSourceType.CLONE, checkout_base_path="/tmp/eviction-base"))
    await env.db.update_project("p", integration_repository_id="repo")
    await env.db.create_workspace(Workspace(id="free", project_id="p",
        workspace_path="/tmp/eviction-free", source_type=RepoSourceType.CLONE,
        kind_id="project-repo"))
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "source").values(
            status="READY", profile_id="worker-codex", route_source="override",
            branch_name="aq/source"))
        await conn.execute(update(sessions).where(sessions.c.id == "s").values(
            lifecycle="pool", profile_id="worker-codex"))
        await conn.execute(insert(integration_branch_owners).values(
            id="source-branch", repository_id="repo", ref="aq/source", owner_id="source",
            owner_role="task", fence_token=1, handoff_state="reserved",
            created_at=NOW - 1800, updated_at=NOW - 1800))
        await conn.execute(update(wait_records).values(decision_reason="evicted"))
    config = AppConfig()
    config.swarm.global_max_active = 1
    config.wait_policy.v2_enabled = config.wait_policy.scheduler_waits_enabled = True
    arbiter = SimpleNamespace(db=env.db, config=config, bus=SimpleNamespace(emit=AsyncMock()),
                             _worktrees_enabled=lambda: False,
                             _pool_quarantine_state=lambda *a: (None, None))
    scheduler = SchedulerWaitService(arbiter, clock=lambda: NOW)
    admission = SimpleNamespace(db=env.db, o=arbiter, service=scheduler)
    await sync(admission, NOW - 1800)
    deadline = next(d for d in await records(env, wait_deadlines)
                    if d["deadline_type"] == "reservation")
    assert (await fire(admission, deadline, NOW))["fired"]
    async with env.db._engine.connect() as conn:
        before = await scheduler.reader.observe_task(conn, "source")
    assert before.constraints["slot"]["blocked"]
    await capture(env)
    get_project, get_repo = env.db.get_project, env.db.get_repo
    driver, handler, _, order = await suspension_driver(env, monkeypatch)

    async def arbitrate():
        # This callback is the same arbitration entry point the lifecycle uses.
        # Reobserve real occupancy and consume the reservation in the claim transaction.
        monkeypatch.setattr(env.db, "get_project", get_project)
        monkeypatch.setattr(env.db, "get_repo", get_repo)
        async with env.db._engine.begin() as conn:
            observed = await scheduler.reader.observe_task(conn, "source")
            assert observed.constraints["slot"]["fleet"] == 0
            assert observed.constraints["slot"]["awaiting_branch_admission"]
            assert not any(c["blocked"] for key, c in observed.constraints.items()
                           if key != "slot"), observed.constraints
            task_id, reservation_id = await claim_candidate(arbiter, conn,
                project_id="p", profile_id="worker-codex", agent_id="a")
            assert task_id == "source" and reservation_id
            assert await env.db.take_task(conn, task_id, agent_id="a", now=NOW + 1)
            await consume_reservation(conn, reservation_id, "replacement", now=NOW + 1)
        order.append("grant")

    driver.orch._reconcile_pools = arbitrate
    lifecycle = WaitLifecycle(driver.service, clock=driver.clock)
    lifecycle.hibernate = driver
    assert (await lifecycle.sleep_decide({"wait_id": env.wait_id},
        principal=None, scope=None, handler=handler))["parked"]
    assert order == ["stop", "push", "release", "grant"]
    assert (await records(env, admission_reservations))[0]["state"] == "consumed"


@pytest.mark.parametrize("change", ["pause", "delete", "gate"])
async def test_matrix_manual_intervention_during_wake_retains_audit_without_launch(
    hibernation_env, change,
):
    from src.waits.registry import build_registry
    from src.wait_runtime.resume import ResumeTransaction

    env = hibernation_env
    env.registry = build_registry(clock=lambda: NOW)
    await capture(env)
    await park(env)
    service = service_for(env, [NOW + 30])
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await service.tick()
    outcome = await one_outcome(env, wakes=0)
    if change == "delete":
        await env.db.delete_task("owner")
    elif change == "gate":
        await env.db.create_gate("p", "human", "Approval revoked", waiter_task_ids=["owner"])
        service.orch._worktrees_enabled = lambda: False
        service.orch._pool_quarantine_state = lambda *a: (None, None)
        async with env.db._engine.connect() as conn:
            observed = await service.scheduler.reader.observe_task(conn, "owner", continuation_id="c")
        assert "dependency_blocked" in observed.constraints["policy_hold"]["reasons"]
    else:
        async with env.db._engine.begin() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == "owner").values(
                status="PAUSED"))
    resume = ResumeTransaction(service)
    if change == "delete":
        with pytest.raises(WaitError):
            await resume.reserve("c", 1)
    else:
        assert await resume.reserve("c", 1) is None
        assert (await env.db.get_task("owner")).status.value == (
            "PAUSED" if change == "pause" else "RESUME_PENDING")
    assert await one_outcome(env, wakes=0) == outcome
    if change == "delete":
        assert not await records(env, continuations)
        assert not await records(env, admission_reservations)
    else:
        assert (await intent(env))["new_session_id"] is None


async def test_matrix_two_launch_failures_then_provider_event_releases_without_relay(
    hibernation_env,
):
    from src.database.tables import provider_availability
    from src.models import AgentProfile
    from src.wait_runtime.hibernate import HibernationTransaction
    from src.waits.registry import build_registry

    env = hibernation_env
    driver, action, _ = await bound_resume(env)
    await env.db.update_session("fresh", state="stopped", desired_state="stopped")
    driver.service.lifecycle = SimpleNamespace(hibernate=HibernationTransaction(driver.service))
    driver.orch.token_store = SimpleNamespace(revoke_session=AsyncMock())
    env.registry = build_registry(clock=lambda: NOW)
    service = service_for(env, [NOW + 30])
    driver.service.decisions = service.decisions
    handler = service.orch._command_handler
    task = await env.db.get_task("owner")
    assert (await driver.failed(action, task, "first failure", handler=handler))["retry_queued"]
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(
            resume_state="reserved", launch_attempts=2, new_session_id=None,
            facts={"resume_attempt_started_at": NOW + 30}))
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(assigned_agent_id="a"))
    assert (await driver.failed(action, task, "second failure", handler=handler))["held"]
    assert len(await records(env, escalations)) == 1
    await env.db.create_profile(AgentProfile(id="worker-codex", name="Worker"))
    driver.orch.provider_availability = SimpleNamespace(provider_for_profile=lambda *a, **k: "fake")
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(
            profile_id="worker-codex", route_source="override"))
        await conn.execute(insert(provider_availability).values(
            provider="fake", state="available", since=NOW + 60, updated_at=NOW + 60, generation=2))
    for generation in (1, 2, 2):
        async with env.db._engine.begin() as conn:
            await driver.recovery_event(conn, {
                "source_key": "resource_invalidation:provider:fake",
                "source_revision": str(generation), "occurred_at": NOW + 60,
                "payload": {"identity": "fake", "facts": {"generation": generation}}})
        row = await intent(env)
        assert row["resume_state"] == ("held" if generation == 1 else "queued")
    assert row["launch_attempts"] == 0 and row["hold_reason"] is None
    assert (await records(env, escalations))[0]["state"] == "cancelled"
    assert (await env.db.get_task("owner")).status is TaskStatus.RESUME_PENDING


async def test_matrix_live_legacy_wait_and_waiting_input_restart_preserve_deadlines(env):
    from src.wait_runtime.legacy_human import adopt_waiting_input
    from tests.test_wait_legacy_bridge import legacy

    service = service_for(env, [NOW])
    bridge = service.legacy_bridge
    await legacy(bridge, created=NOW - 7000, expires=NOW + 17)
    first = await bridge.adopt_agent_wait("old", handler=service.orch._command_handler)
    service = service_for(env, [NOW + 10])
    replay = await service.legacy_bridge.adopt_agent_wait(
        "old", handler=service.orch._command_handler)
    assert first["wait_id"] == replay["wait_id"] and replay["replayed"]
    row = (await records(env))[0]
    assert row["created_at"] == NOW - 7000
    assert row["escalate_at"] == NOW + 17
    assert row["sleep_decision"] == "in_place"
    await owner(env.db, "human", "human-s", "human-a")
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "human").values(
            status="WAITING_INPUT", branch_name="aq/human", updated_at=NOW - 500))
    await env.db.create_message(project_id="p", from_kind="session", from_id="human-s",
                                to_kind="user", to_id="dashboard", body="Human decision?")
    service.orch.config.wait_policy.hibernation_enabled = True
    adopted = await adopt_waiting_input(service.legacy_bridge, "human")
    again = await adopt_waiting_input(service.legacy_bridge, "human")
    assert adopted["wait_id"] == again["wait_id"]
    human = next(row for row in await records(env) if row["owner_id"] == "human")
    assert human["condition_kind"] == "human" and human["sleep_decision"] == "hibernate"
    assert human["escalate_at"] is None
    continuation = (await records(env, continuations))[0]
    assert continuation["summary_degraded"] and continuation["stopped_at"] is None
    # Adoption cannot claim a live writer has already stopped or release its seat.
    assert (await env.db.get_session("human-s")).state == "running"
    assert not [d for d in await records(env, wait_deadlines)
                if d["wait_id"] == human["id"] and d["deadline_type"] == "escalation"]


async def test_matrix_hibernate_resume_fresh_folder_prime_ack_and_single_writer(
    hibernation_env, tmp_path, monkeypatch,
):
    from dataclasses import replace
    from pathlib import Path

    from src.database.tables import agents
    from src.git.manager import GitManager
    from src.models import RepoSourceType, SessionRecord, Workspace
    from src.prime import PrimeRenderer
    from src.scheduler import AssignAction
    from src.wait_runtime.resume import ResumeTransaction
    from src.waits.fencing import mutation_fence
    from src.waits.registry import build_registry
    from src.waits.resume_ack import acknowledge, mark_delivered

    env = hibernation_env
    env.registry = build_registry(clock=lambda: NOW)
    await capture(env, "Retain the verified work; next run the fault matrix.")
    driver, handler, _, order = await suspension_driver(env, monkeypatch)
    assert (await driver.advance(env.wait_id, handler=handler))["parked"]
    assert order == ["stop", "push", "release"]
    await env.db.add_task_comment("owner", "Posted while the worker was asleep.",
                                   author_kind="user", author_id="dashboard")
    service = service_for(env, [NOW + 30])
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await service.tick()
    git = GitManager()
    base_path, old = tmp_path / "base", tmp_path / "old"
    base_path.mkdir()
    await git._arun(["init", "-b", "main"], cwd=str(base_path))
    # Identity is local to the disposable fixture, never the worker checkout.
    await git._arun(["config", "user.email", "test@example.test"], cwd=str(base_path))
    await git._arun(["config", "user.name", "Test"], cwd=str(base_path))
    (base_path / ".gitignore").write_text("transcript.cache\n")
    await git._arun(["add", ".gitignore"], cwd=str(base_path))
    await git._arun(["commit", "-m", "saved work"], cwd=str(base_path))
    head = await git._arun(["rev-parse", "HEAD"], cwd=str(base_path))
    await git._arun(["worktree", "add", "-b", "aq/owner", str(old)], cwd=str(base_path))
    (old / "transcript.cache").write_text("Never copy the old transcript")
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(
            saved_head=head, resume_state="reserved", launch_attempts=1))
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(assigned_agent_id="a"))
        await conn.execute(update(agents).where(agents.c.id == "a").values(
            state="BUSY", current_task_id="owner"))
        await conn.execute(insert(admission_reservations).values(
            id="reservation", project_id="p", continuation_id="c", scope={},
            effective_priority=100, provenance="resume", deadline_at=NOW + 300,
            state="granted", created_at=NOW + 30))
    workspace = Workspace(id="slot", project_id="p", workspace_path=str(old),
                          source_type=RepoSourceType.CLONE, slot_index=0,
                          base_workspace_id="base", kind_id="project-repo")
    base = Workspace(id="base", project_id="p", workspace_path=str(base_path),
                     source_type=RepoSourceType.CLONE)
    monkeypatch.setattr(env.db, "get_workspace_for_task", AsyncMock(return_value=workspace))
    monkeypatch.setattr(env.db, "get_workspace", AsyncMock(return_value=base))
    monkeypatch.setattr(env.db, "resolve_workspace_kind", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(env.db, "update_workspace", AsyncMock())
    slots = SimpleNamespace(reprovision_slot=AsyncMock())
    orch = SimpleNamespace(db=env.db, git=git, _prepare_workspace_locked=AsyncMock(return_value=str(old)),
                           _git_mutex=lambda _: asyncio.Lock(), _worktree_slots=lambda: slots)
    resume = ResumeTransaction(SimpleNamespace(orch=orch), clock=lambda: NOW + 30)
    action = AssignAction("a", "owner", "p", "c", 1)
    fresh = await resume.fresh_workspace(action, await env.db.get_task("owner"), None,
                                         await intent(env))
    assert fresh != str(old) and Path(fresh).is_dir()
    assert not (Path(fresh) / "transcript.cache").exists()
    assert await git._arun(["rev-parse", "HEAD"], cwd=fresh) == head
    record = SessionRecord(
        id="new", task_id="owner", agent_id="a", project_id="p", profile_id="worker-codex",
        harness="codex", provider="fake", name="new", lifecycle="task", state="running",
        desired_state="running", work_dir=fresh, epoch="new-boot", instance_token="new-token",
        last_claim_epoch=2, started_at=NOW + 30)
    await resume.bind_launch(action, record)
    await env.db.create_session(record)
    assert len([r for r in await records(env, sessions) if r["state"] == "running"]) == 1
    renderer = PrimeRenderer(env.db, AppConfig())
    # Restore the real project read for the prime renderer.
    monkeypatch.undo()
    body = (await renderer.render_for_task("owner", session_id="new", work_dir=fresh)).to_markdown()
    assert "Retain the verified work" in body and "Posted while the worker was asleep" in body
    assert "## Wait outcome" in body and "## Task\n" in body
    principal = SimpleNamespace(session_id="new", session_instance_token="new-token")
    async with env.db._engine.begin() as conn:
        assert (await mutation_fence(conn, "task_close", principal))["error_code"] == "wait.ack_required"
        await mark_delivered(conn, continuation_id="c", session_id="new",
                             instance_token="new-token", claim_epoch=2, now=NOW + 40)
        await acknowledge(conn, {"wait_id": env.wait_id, "continuation_id": "c", "generation": 1},
                          scope=replace(env.scope, session_id="new", instance_token="new-token",
                                        claim_epoch=2), now=NOW + 45)
        assert await mutation_fence(conn, "task_close", principal) is None
    assert (await env.db.get_session("s")).state == "stopped"
    assert (await env.db.get_task("owner")).status is TaskStatus.IN_PROGRESS
    sample = (await records(env, wait_measurements))[0]
    assert sample["hibernated"]
    assert sample["waited_seconds"] == 30
    assert sample["rebuild_seconds"] == 10  # delivered at +40, fresh process started at +30


async def test_matrix_accounting_wait_duration_and_resume_cost_are_transition_derived(env):
    now = [NOW + 1800]
    service = service_for(env, now)
    await register(env)
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await service.tick()
    sample = (await records(env, wait_measurements))[0]
    row = (await records(env))[0]
    assert sample["waited_seconds"] == row["resolved_at"] - row["created_at"] == 1800
    assert sample["recorded_at"] == now[0]
    assert not sample["hibernated"]
    # Missing costs are unknown, never a fabricated zero or a saved-cost claim.
    assert sample["setup_seconds"] is sample["teardown_seconds"] is sample["rebuild_seconds"] is None
    now[0] += 86400
    await service.tick()
    assert await records(env, wait_measurements) == [sample]


@pytest.mark.parametrize("capacity_delay", [0, 600])
async def test_matrix_accounting_from_park_through_sampler_api_and_display_contract(
    hibernation_env, monkeypatch, capacity_delay,
):
    import json
    from dataclasses import replace
    from pathlib import Path

    from src.api.models.metrics import MetricsSample
    from src.config_wait_policy import parse_wait_policy
    from src.database.queries.wait_metrics import slot_accounting, wait_metrics_snapshot
    from src.metrics.sampler import MetricsSampler
    from src.metrics.waits import with_wait_policy
    from src.models import AgentProfile, Project, RepoConfig, RepoSourceType, SessionRecord, Workspace
    from src.wait_runtime.measurements import capture_costs
    from src.wait_runtime.resume import ResumeTransaction
    from src.waits.registry import build_registry
    from src.waits.resume_ack import acknowledge, mark_delivered

    env = hibernation_env
    env.registry = build_registry(clock=lambda: NOW)
    await capture(env)
    before = await env.db.metrics_waits_snapshot(NOW + 100)
    assert before["released_slot_seconds_24h"] == {"condition": 0, "capacity": 0}
    driver, handler, clock, _ = await suspension_driver(env, monkeypatch)
    clock.now = NOW + 120
    assert (await driver.advance(env.wait_id, handler=handler))["parked"]
    monkeypatch.undo()
    assert (await intent(env))["stopped_at"] == clock.now
    await env.db.create_profile(AgentProfile(id="worker-codex", name="Worker", lifecycle="pool",
                                            max_active=1))
    await env.db.create_repo(RepoConfig(id="repo", project_id="p", url="https://example.test/repo",
        source_type=RepoSourceType.CLONE, checkout_base_path="/tmp/accounting-base"))
    await env.db.update_project("p", integration_repository_id="repo")
    await env.db.create_workspace(Workspace(id="available", project_id="p",
        workspace_path="/tmp/accounting-fresh", source_type=RepoSourceType.CLONE,
        kind_id="project-repo"))
    service = service_for(env, [NOW + 7200])
    service.orch.config.wait_policy.hibernation_enabled = True
    service.orch._worktrees_enabled = lambda: False
    service.orch._pool_quarantine_state = lambda *a: (None, None)
    service.orch._task_control_lock = lambda _: asyncio.Lock()
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(
            profile_id="worker-codex", route_source="override"))
    await env.db.transition_task("source", TaskStatus.COMPLETED)
    assert await service.tick()
    clock.now = NOW + 7200
    resume = ResumeTransaction(service, clock=clock)
    if capacity_delay:
        # An actual running process owns the seat until its stop is observed.
        # The production reserve path must retain this request boundary even
        # when no seat can be granted yet; do not seed a synthetic reservation.
        service.orch.config.swarm.global_max_active = 1
        await env.db.create_session(SessionRecord(id="idle", project_id="p",
            profile_id="worker-codex", harness="codex", provider="fake", name="idle",
            lifecycle="pool", work_dir="/tmp/idle", epoch="boot", instance_token="idle-token",
            state="running", desired_state="running", started_at=NOW, last_activity=NOW))
        assert await resume.reserve("c", 1) is None
        assert (await env.db.get_session("idle")).desired_state == "stopped"
        clock.now += capacity_delay
        await env.db.update_session("idle", state="stopped")

    async def prepare(*args):
        clock.now += 5
        return "/tmp/accounting-fresh"

    async def launch(action, task, unused, path):
        record = SessionRecord(id="new", project_id="p", task_id="owner", agent_id=action.agent_id,
            profile_id="worker-codex", harness="codex", provider="fake", name="new",
            lifecycle="task", work_dir=path, epoch="new-boot", instance_token="new-token",
            state="running", desired_state="running", last_claim_epoch=task.claim_epoch,
            started_at=clock.now)
        await resume.bind_launch(action, record)
        await env.db.create_session(record)

    monkeypatch.setattr(resume, "fresh_workspace", prepare)
    service.orch._launch_session_for_task_locked = launch
    assert (await resume.dispatch({"continuation_id": "c", "generation": 1},
        principal=None, scope=None, handler=service.orch._command_handler))["dispatched"]
    clock.now = NOW + 7215 + capacity_delay
    async with env.db._engine.begin() as conn:
        await mark_delivered(conn, continuation_id="c", session_id="new",
                             instance_token="new-token", claim_epoch=2, now=clock.now)
        await acknowledge(conn, {"wait_id": env.wait_id, "continuation_id": "c", "generation": 1},
            scope=replace(env.scope, session_id="new", instance_token="new-token", claim_epoch=2),
            now=clock.now)
    config = AppConfig()
    config.metrics.perf_enabled = False
    config.wait_policy = parse_wait_policy({"metrics": {
        "pre_rollout_turn_rate": 2, "pre_rollout_tokens_per_turn": 100,
        "cohort": "synthetic-fault-matrix", "window": "disposable-fixture:0..7200",
    }})
    assert not config.wait_policy.validate()
    raw = await env.db.metrics_waits_snapshot(clock.now)
    assert raw["tokens_saved_counterfactual"] is None
    assert with_wait_policy(raw, config.wait_policy)["tokens_saved_counterfactual"]["tokens"] is None
    # Synthetic transcript readings enter through the production measurement
    # path; an incomplete reading above cannot become claimed token savings.
    monkeypatch.setattr("src.wait_runtime.measurements.session_turn_tokens",
                        AsyncMock(side_effect=[7, 11]))
    service.lifecycle.clock = clock
    await capture_costs(service.lifecycle, env.wait_id)
    await env.db.create_project(Project(id="q", name="Foreign"))
    await env.db.create_task(Task(id="foreign", project_id="q", title="Foreign", description=""))
    async with env.db._engine.begin() as conn:
        await register_wait(conn, scope=Scope("q", "scheduler", "foreign", principal="service"),
            condition={"kind": "timer", "due_at": NOW + 9000}, idempotency_key="foreign",
            registry=env.registry, policy=Policy(), now=NOW + 6000, source_watermark=0,
            decide_sleep=lambda *_: SleepDecision("in_place", "test"))
        isolated = await wait_metrics_snapshot(conn, now=clock.now, project_id="p")
        foreign = await wait_metrics_snapshot(conn, now=clock.now, project_id="q")
        assert isolated["pending_in_place"] == 0 and foreign["pending_in_place"] == 1
        assert foreign["released_slot_seconds_24h"] == {"condition": 0, "capacity": 0}
        clipped = await slot_accounting(conn, now=clock.now, window_seconds=300, project_id="p")
    sample = (await records(env, wait_measurements))[0]
    assert (sample["waited_seconds"], sample["teardown_seconds"], sample["setup_seconds"],
            sample["rebuild_seconds"]) == (7200, 120, 5, 10)
    sampler = MetricsSampler(env.db, config, clock=clock)
    payload = MetricsSample.model_validate(await sampler.collect()).model_dump(mode="json")
    fixture = Path(__file__).resolve().parents[1] / (
        "dashboard/src/pages/metrics/__tests__/waitFaultAccounting.json")
    expected = json.loads(fixture.read_text())
    expected["released_slot_seconds_1h"] = {"condition": 3585 - capacity_delay,
                                             "capacity": capacity_delay}
    expected["released_slot_seconds_24h"] = {"condition": 7080, "capacity": capacity_delay}
    expected["wait_seconds_1h"] = 3585 - capacity_delay
    expected["tokens_saved_counterfactual"]["tokens"] = 2 * (3585 - capacity_delay) / 60 * 100 - 18
    assert {key: payload["waits"][key] for key in expected} == expected
    assert clipped == {
        "in_place_slot_seconds_1h": 0,
        "released_slot_seconds_1h": {"condition": 0 if capacity_delay else 285,
                                      "capacity": 285 if capacity_delay else 0},
    }
    reservations = await records(env, admission_reservations)
    assert min(row["created_at"] for row in reservations) == NOW + 7200
    assert min(row["granted_at"] for row in reservations if row["granted_at"] is not None) == (
        NOW + 7200 + capacity_delay)


@pytest.mark.tmux
async def test_matrix_real_harness_wait_suppresses_mail_until_one_outcome_turn(
    env, harness_id, api, cli_home, socket_name, tmp_path,
):
    """Opt-in: actual harnesses talk only to the hermetic local fake model API."""
    provider = _provider(socket_name, tmp_path)
    handle = await _start_idle(
        provider, SPECS[harness_id](tmp_path, api, "wait-fault-token", cli_home), harness_id)
    try:
        await register(env)
        await routine_mail(env.db, env.scope)
        delivered = []

        async def nudge(**kwargs):
            try:
                result = await provider.nudge(handle, kwargs["text"])
            except Exception as exc:
                exc.add_note(await provider.peek(handle, 30))
                raise
            delivered.append(kwargs)
            return result

        lens = SimpleNamespace(activity=AsyncMock(return_value="idle"), nudge=nudge,
                               ensure_started=AsyncMock())
        engine = MessageDeliveryEngine(env.db, lens, AppConfig())
        initial = len(api.bodies)
        for _ in range(3):
            await engine.run_delivery_pass()
        assert not delivered and len(api.bodies) == initial
        service = service_for(env, [NOW + 5])
        await env.db.transition_task("source", TaskStatus.COMPLETED)
        assert await service.tick()
        await engine.run_delivery_pass()
        assert len(delivered) == 1
        await _delivered(api, delivered[0]["text"])
        after = len(api.bodies)
        for _ in range(3):
            await engine.run_delivery_pass()
        assert len(delivered) == 1 and len(api.bodies) == after
    finally:
        await provider.stop(handle)


async def test_continuation_incident_rejects_forged_bindings(hibernation_env):
    from src.waits.registry import build_registry

    env = hibernation_env
    env.registry = build_registry(clock=lambda: NOW)
    service = service_for(env, [NOW])
    handler = service.orch._command_handler
    args = {
        "project_id": "p", "task_id": "owner", "task_title": "Owner",
        "source_kind": "wait", "source_identity": "continuation:c:stop_uncertain",
        "incident_key": "continuation:c:stop_uncertain", "severity": "high",
        "summary": "Stop uncertain", "investigation": "Provider did not confirm stop",
        "decision_requested": "Repair the retained writer",
    }
    for overrides in (
        {"task_id": "source"},
        {"source_identity": "continuation:missing:stop_uncertain"},
        {"source_identity": "continuation:c:unsupported"},
        {"incident_key": "different-incident"},
    ):
        async with env.db._engine.begin() as conn:
            result = await handler.wait_escalation_create(conn, {**args, **overrides})
        assert result["error_code"] == "invalid_binding", result
    assert not await records(env, escalations)
    # The public command cannot acquire internal wait-driver authority.
    result = await handler.execute("escalation_create", args)
    assert result["error_code"] == "invalid_binding", result


@pytest.mark.parametrize("reason", [
    "stop_uncertain", "push_failed", "detach_uncertain", "resume_launch_failed",
    "resume_workspace_failed", "resume_stop_uncertain", "resume_detach_uncertain", "paused",
])
async def test_continuation_incident_binds_runtime_hold_and_replay(hibernation_env, reason):
    from src.waits.registry import build_registry

    env = hibernation_env
    env.registry = build_registry(clock=lambda: NOW)
    handler = service_for(env, [NOW]).orch._command_handler
    identity = f"continuation:c:{reason}"
    args = {
        "project_id": "p", "task_id": "owner", "source_kind": "wait",
        "source_identity": identity, "incident_key": identity, "severity": "high",
        "summary": reason, "investigation": "Retain the writer until recovery",
        "decision_requested": "Repair the infrastructure hold",
    }
    async with env.db._engine.begin() as conn:
        created = await handler.wait_escalation_create(conn, args)
        replay = await handler.wait_escalation_create(conn, args)
    assert created["success"] and created["created"], created
    assert replay["success"] and not replay["created"], replay
    assert len(await records(env, escalations)) == 1
