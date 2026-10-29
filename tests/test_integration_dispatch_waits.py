"""The train's shared waits use verified sources and survive policy replacement."""

from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from src.commands import CommandHandler
from src.commands.principal import TRUSTED_LOCAL, principal_context
from src.config import AppConfig
from src.database.tables import integration_batches, integration_outbox, wait_records, wait_source_events
from src.integration.batches import BatchStore
from src.integration.checks import CommitCheck, Conclusion, ExactChecks, RequiredChecks
from src.integration.dispatch_waits import DispatchWaits
from src.integration.outbox import IntegrationOutbox
from src.wait_runtime.service import WaitService
from src.waits.conditions import WaitError
from tests.test_integration_gitops import LocalGit, git
from tests.test_integration_train_sources import MAIN, completed, lane, world as world


class Provider:
    compares_targets = False

    def __init__(self):
        self.required = RequiredChecks(version="v1", names=("linux",), producer_id="hosted")
        self.green = set()
        self.request = AsyncMock()

    async def observe(self, head, *, now):
        return [CommitCheck(
            repository_id=head.repository_id, sha=head.sha, name=name,
            producer_id=self.required.producer_id, required_check_version=self.required.version,
            conclusion=Conclusion.SUCCESS if head.sha in self.green else Conclusion.PENDING,
            classification="success" if head.sha in self.green else "pending", run_id="run-1",
            observed_at=now,
        ) for name in self.required.names]


async def runtime(world):
    now = [time.time()]
    train, _, retained = lane(world, LocalGit(Path(world.origin.url)),
                               clock=lambda: now[0], settling=True)
    provider = Provider()
    exact = ExactChecks(world.db, provider, clock=lambda: now[0])
    candidate_checks = (await train.lane_for(MAIN)).checks
    candidate_checks.resolve = AsyncMock(return_value=exact)
    config = AppConfig()
    config.wait_policy.v2_enabled = True
    orch = SimpleNamespace(db=world.db, config=config, git=SimpleNamespace(
        acommit_head_sha=AsyncMock(side_effect=lambda _repo, sha, **_: sha)),
        github_repository_binding_resolver=AsyncMock(return_value=retained.binding),
        bus=SimpleNamespace(emit=AsyncMock()), plugin_registry=None)
    waits = DispatchWaits(orch, lanes=train.lane_for, clock=lambda: now[0])
    service = WaitService(orch, clock=lambda: now[0])
    orch.wait_service = service
    handler = orch._command_handler = CommandHandler(orch, config)
    train.waits = waits
    return SimpleNamespace(train=train, provider=provider, exact=exact, service=service,
                           handler=handler, now=now, orch=orch)


async def rows(world, table=wait_records):
    async with world.db._engine.connect() as conn:
        return [dict(row) for row in (await conn.execute(select(table))).mappings()]


async def settle_sources(world, service):
    """Replay committed source events twice: only one outcome can commit."""
    for event in await rows(world, wait_source_events):
        for wait in await rows(world):
            if wait["source_key"] != event["source_key"]:
                continue
            async with world.db._engine.begin() as conn:
                await service.dispatcher.commit(conn, wait, event)


async def test_candidate_and_queue_waits_settle_from_exact_committed_sources(world):
    case = await runtime(world)
    await completed(world, "first")
    first = await case.train.visit(MAIN)
    assert first.state == "testing"
    ids = first.detail["dispatch"]["wait_ids"]
    assert len(ids) == 1, first.detail
    initial = (await rows(world))[0]
    assert initial["origin"] == "integration"
    assert initial["condition_kind"] == "ci_check"
    assert initial["condition"]["head_sha"] == first.candidate_sha
    batch = (await rows(world, integration_batches))[0]
    assert batch["tested_candidate_sha"] == first.candidate_sha
    assert batch["lifecycle"] != "promoted"
    assert (await case.train.visit(MAIN)).detail["dispatch"]["wait_ids"] == ids

    await completed(world, "second")
    queued = await case.train.visit(MAIN)
    assert len(queued.detail["dispatch"]["wait_ids"]) == 2
    queued_wait = next(w for w in await rows(world) if w["owner_id"] == "second")
    assert queued_wait["condition_kind"] == "batch"
    assert queued_wait["condition"]["batch_id"] == first.batch_id
    with principal_context(TRUSTED_LOCAL):
        explained = await case.handler.execute("wait_explain", {"task_id": "second"})
    assert explained["success"] and explained["total"] == 1

    case.now[0] += 61
    case.provider.green.add(first.candidate_sha)
    successor = await case.train.visit(MAIN)
    assert successor.state == "testing" and successor.batch_id != first.batch_id
    assert git(world.origin.url, "rev-parse", "main") == first.candidate_sha
    await settle_sources(world, case.service)
    await settle_sources(world, case.service)
    settled = {w["id"]: w for w in await rows(world)}
    assert settled[initial["id"]]["outcome"] == "satisfied"
    assert settled[queued_wait["id"]]["outcome"] == "satisfied"
    assert len([w for w in settled.values() if w["outcome"] == "pending"]) == 1
    publications = await rows(world, integration_outbox)
    publication = next(row for row in publications
                       if row["event_type"] == "integration.publication_verified")
    assert publication["payload"]["head_sha"] == first.candidate_sha
    assert publication["payload"]["publication_verified"] is True
    case.orch.bus.emit.reset_mock()
    accept = AsyncMock(side_effect=[False, True])
    outbox = IntegrationOutbox(world.db, accept, clock=lambda: case.now[0],
                              publication_wake=case.orch.bus.emit)
    event_types = frozenset({"integration.publication_verified"})
    assert await outbox.dispatch_due(case.now[0], event_types=event_types) == 0
    case.orch.bus.emit.assert_awaited_once_with(
        "integration.publication_verified", publication["payload"])
    case.now[0] += 2
    assert await outbox.dispatch_due(case.now[0], event_types=event_types) == 1
    assert await outbox.dispatch_due(case.now[0], event_types=event_types) == 0
    assert case.orch.bus.emit.await_count == 2
    assert accept.await_args.args[2] == publication["id"]
    await case.train.batches.settle(await BatchStore(world.db).get(first.batch_id),
        SimpleNamespace(target_sha=first.candidate_sha, candidate_sha=first.candidate_sha))
    assert len(await rows(world, integration_outbox)) == len(publications)


async def test_ci_wait_observer_recovers_after_quota_without_a_train_visit(world):
    from src.git.github_contracts import GitHubAccessError

    case = await runtime(world)
    case.provider.required = case.provider.required.model_copy(update={
        "names": ("linux", "windows"),
    })
    await completed(world, "first")
    first = await case.train.visit(MAIN)
    assert len(first.detail["dispatch"]["wait_ids"]) == 2
    observe = case.provider.observe
    retry_at = case.now[0] + 900
    case.provider.observe = AsyncMock(side_effect=GitHubAccessError(
        "rate_limited", "quota exhausted", retry_at=retry_at, http_status=403,
    ))
    # Existing subscriptions still need observation during a rollout rollback.
    case.orch.config.wait_policy.v2_enabled = False
    case.now[0] += 61
    await case.service.tick()
    assert case.provider.observe.await_count == 1  # shared repository backoff
    assert all(row["outcome"] == "pending" for row in await rows(world))
    case.now[0] = retry_at - 1
    await case.service.tick()
    assert case.provider.observe.await_count == 1
    case.provider.green.add(first.candidate_sha)
    case.provider.observe = observe
    case.now[0] = retry_at + 1
    await case.service.tick()
    assert all(row["outcome"] == "satisfied" for row in await rows(world))
    assert all(row["lifecycle"] != "promoted"
               for row in await rows(world, integration_batches))


async def test_train_visit_preserves_wait_when_ci_observation_is_unavailable(world):
    from src.waits.conditions import WaitError

    case = await runtime(world)
    await completed(world, "first")
    first = await case.train.visit(MAIN)
    wait = (await rows(world))[0]
    adapter = case.service.registry.adapter("ci_check")
    reader = adapter.reader
    adapter.reader = AsyncMock(side_effect=WaitError(
        "wait.unavailable", "CI observation temporarily unavailable", retry_after=60,
    ))
    case.provider.green.add(first.candidate_sha)
    case.now[0] += 61
    visit = await case.train.visit(MAIN)
    assert visit.state == "delivered", visit
    assert visit.detail["dispatch"]["wait_ids"] == [wait["id"]]
    error = visit.detail["dispatch"]["wait_errors"][0]
    assert error["wait_id"] == wait["id"] and error["code"] == "wait.unavailable"
    assert (await rows(world))[0]["outcome"] == "pending"

    adapter.reader = reader
    await case.service.listener.tick()
    assert (await rows(world))[0]["outcome"] == "satisfied"


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("previously_dispatched", [False, True])
async def test_skipped_ci_event_retries_unchanged_success_without_blocking_cursor(
    world, restart, previously_dispatched,
):
    from src.database.tables import wait_listener_cursors, wait_measurements
    from src.waits.conditions import Condition, WaitError
    from src.waits.listener import WaitSourceListener

    case = await runtime(world)
    await completed(world, "first")
    first = await case.train.visit(MAIN)
    case.provider.green.add(first.candidate_sha)
    case.now[0] += 61
    head = case.orch.wait_ci_watcher.head(Condition.from_record(
        (await rows(world))[0]["condition"]))
    await case.exact.refresh(head)
    success = next(event for event in await rows(world, wait_source_events)
                   if event["payload"].get("conclusion") == "success")
    later = await world.db.wait_source_commit(
        source_key="test:later", generation=0, revision="1", outcome_ref="later", payload={},
    )
    observe = case.service.registry.observe
    case.service.registry.observe = AsyncMock(side_effect=WaitError(
        "wait.unavailable", "CI observation temporarily unavailable", retry_after=300,
    ))
    await case.service.listener.tick()
    assert (await rows(world))[0]["outcome"] == "pending"
    outbox = {event["seq"]: event for event in await rows(world, wait_source_events)}
    assert outbox[success["seq"]]["dispatched_at"] is None
    retry_at = outbox[success["seq"]]["dispatch_retry_at"]
    assert retry_at == case.now[0] + 300
    assert outbox[later]["dispatched_at"] is not None
    cursor = (await rows(world, wait_listener_cursors))[0]["last_seq"]
    if previously_dispatched:
        # Reproduce the durable state left by older daemons: the failed
        # observation was stamped dispatched and the cursor already passed it.
        async with world.db._engine.begin() as conn:
            await conn.execute(update(wait_source_events).where(
                wait_source_events.c.seq == success["seq"],
            ).values(dispatched_at=case.now[0]))
    case.service.registry.observe.assert_awaited_once()
    # A normal listener tick respects provider backoff even though its cursor
    # has passed the failed event. No new source revision is needed to retry.
    await case.service.listener.tick()
    case.service.registry.observe.assert_awaited_once()
    case.service.registry.observe = observe
    if restart:
        case.now[0] += case.service.listener.lease_seconds + 1
        case.service.listener = WaitSourceListener(case.service, clock=lambda: case.now[0])
        await case.service.listener.tick()
        assert (await rows(world))[0]["outcome"] == "pending"
    case.now[0] = retry_at + 1
    await case.service.listener.tick()
    assert (await rows(world))[0]["outcome"] == "satisfied"
    outbox = {event["seq"]: event for event in await rows(world, wait_source_events)}
    assert outbox[success["seq"]]["dispatched_at"] is not None
    assert len([event for event in outbox.values()
                if event["source_key"] == success["source_key"]]) == 2  # pending, success
    assert (await rows(world, wait_listener_cursors))[0]["last_seq"] == cursor
    measurements = await rows(world, wait_measurements)
    await case.service.listener.tick()
    assert await rows(world, wait_measurements) == measurements


async def test_dispatched_ci_reconciliation_is_bounded_and_observes_current_state(world):
    from src.waits.conditions import Condition

    case = await runtime(world)
    case.provider.required = case.provider.required.model_copy(update={
        "names": ("linux", "windows"),
    })
    await completed(world, "first")
    first = await case.train.visit(MAIN)
    assert len(first.detail["dispatch"]["wait_ids"]) == 2
    for wait in await rows(world):
        await world.db.wait_source_commit(
            source_key=wait["source_key"], generation=wait["source_generation"],
            revision="pending-hint", outcome_ref="pending", payload={"conclusion": "pending"},
        )
    # Dispatch the pending hints normally. Reconciliation must not treat these
    # already dispatched hints as success, nor repeatedly replay one per pass.
    observe = AsyncMock(wraps=case.service.registry.observe)
    case.service.registry.observe = observe
    await case.service.listener.tick()
    assert observe.await_count == 2
    assert {call.args[1].parameters["check_name"] for call in observe.await_args_list} == {
        "linux", "windows",
    }
    observe.reset_mock()
    case.service.listener.retry_batch_size = 1
    # Forward dispatch already observed both pending conditions. Recovery shares
    # the progress cadence and must not immediately repeat those provider reads.
    await case.service.listener.tick()
    observe.assert_not_awaited()
    case.now[0] += case.service.listener.safety_seconds - 1
    await case.service.listener.tick()
    observe.assert_not_awaited()
    assert all(row["outcome"] == "pending" for row in await rows(world))
    # Cross the same deadline once; recovery now gets one bounded source read.
    case.now[0] += 2
    await case.service.listener.tick()
    assert observe.await_count == 1
    assert all(row["outcome"] == "pending" for row in await rows(world))
    await case.service.listener.tick()
    assert observe.await_count == 2  # the other source gets its bounded turn
    assert {call.args[1].parameters["check_name"] for call in observe.await_args_list} == {
        "linux", "windows",
    }
    await case.service.listener.tick()
    assert observe.await_count == 2  # no hammering unchanged pending sources

    case.provider.green.add(first.candidate_sha)
    case.now[0] += 61
    head = case.orch.wait_ci_watcher.head(Condition.from_record((await rows(world))[0]["condition"]))
    await case.exact.refresh(head)
    # Simulate the old daemon consuming terminal events without applying them.
    dispatch = case.service.listener._dispatch
    case.service.listener._dispatch = AsyncMock(return_value=0)
    await case.service.listener.tick()
    case.service.listener._dispatch = AsyncMock(wraps=dispatch)
    assert all(row["outcome"] == "pending" for row in await rows(world))
    await case.service.listener.tick()
    assert sum(row["outcome"] == "satisfied" for row in await rows(world)) == 1
    await case.service.listener.tick()
    assert all(row["outcome"] == "satisfied" for row in await rows(world))
    dispatched = case.service.listener._dispatch.await_args_list
    assert len(dispatched) == 2
    assert all(call.args[1]["payload"]["conclusion"] == "success" for call in dispatched)


async def test_train_ci_deadline_stays_in_supervisor_inbox_during_discord_reconcile(world):
    from src.database.tables import messages, wait_escalations
    from src.escalations import SinkTransport
    from tests.test_escalation_delivery import make_service

    case = await runtime(world)
    await completed(world, "first")
    await case.train.visit(MAIN)
    wait = (await rows(world))[0]
    assert wait["owner_kind"] == "integration" and wait["owner_id"].startswith("train:")
    case.now[0] = wait["escalate_at"]
    assert await case.service.lease()
    result = await case.service.command("wait_deadline_fire", {
        "wait_id": wait["id"], "deadline_type": "escalation", "generation": 1,
    })
    assert result["success"], result
    sink = SinkTransport()
    delivery = make_service(world.db, sink, clock=lambda: case.now[0])
    await delivery.tick()
    await delivery.tick()
    assert sink.calls == []
    incident = (await rows(world, wait_escalations))[0]
    notices = [row for row in await rows(world, messages)
               if row["thread_id"] == incident["escalation_id"]]
    assert len(notices) == 1
    assert notices[0]["to_kind"] == "session" and notices[0]["to_id"] == "supervisor-p"


async def test_stalled_ci_verification_releases_graph_for_supervisor_controls(world, monkeypatch):
    import asyncio

    from src.waits.conditions import Condition, WaitError
    from src.waits.graph import lock_graph

    case = await runtime(world)
    await completed(world, "first")
    await case.train.visit(MAIN)
    wait = (await rows(world))[0]
    condition = Condition.from_record(wait["condition"])
    watcher = case.orch.wait_ci_watcher
    # The short deadline exercises a stalled provider, not SQL preparation on
    # a busy test server. Verify the real binding before starting that clock.
    async with world.db._engine.connect() as conn:
        prepared = await watcher._prepare(conn, condition)
    assert prepared is not None and prepared[2] is case.exact
    monkeypatch.setattr(watcher, "_prepare", AsyncMock(return_value=prepared))
    started = asyncio.Event()

    async def stalled(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("src.waits.watchers.ci.OBSERVATION_TIMEOUT_SECONDS", 0.05)
    case.provider.observe = stalled

    async def verify_under_graph():
        with pytest.raises(WaitError, match="temporarily unavailable") as error:
            async with world.db._engine.begin() as conn:
                await lock_graph(conn)
                await case.service.registry.observe(conn, condition)
        assert error.value.code == "wait.unavailable"

    observation = asyncio.create_task(verify_under_graph())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        async with world.db._engine.begin() as conn:
            await lock_graph(conn, timeout=1)
        await observation
    finally:
        observation.cancel()
        await asyncio.gather(observation, return_exceptions=True)
    assert (await rows(world))[0]["outcome"] == "pending"


async def test_policy_replacement_and_restoration_never_replay_a_cancelled_receipt(world):
    case = await runtime(world)
    await completed(world, "first")
    first = await case.train.visit(MAIN)
    old_id = first.detail["dispatch"]["wait_ids"][0]
    case.provider.required = case.provider.required.model_copy(update={"names": ("windows",)})
    case.now[0] += 61
    changed = await case.train.visit(MAIN)
    new_id = changed.detail["dispatch"]["wait_ids"][0]
    assert old_id != new_id
    assert next(w for w in await rows(world) if w["id"] == old_id)["outcome"] == "cancelled"
    assert changed.candidate_sha == first.candidate_sha
    case.provider.required = case.provider.required.model_copy(update={"names": ("linux",)})
    case.now[0] += 61
    restored = await case.train.visit(MAIN)
    restored_id = restored.detail["dispatch"]["wait_ids"][0]
    assert restored_id not in {old_id, new_id}
    assert (await case.train.visit(MAIN)).detail["dispatch"]["wait_ids"] == [restored_id]
    assert [w["condition"]["check_name"] for w in await rows(world)
            if w["outcome"] == "pending"] == ["linux"]


async def test_moved_delivery_tip_replaces_exact_candidate_wait(world):
    case = await runtime(world)
    await completed(world, "first")
    first = await case.train.visit(MAIN)
    old_id = first.detail["dispatch"]["wait_ids"][0]
    await completed(world, "outside")
    world.origin.land("outside")
    # A stale candidate gets one moved observation before the replacement builds.
    for _ in range(3):
        replacement = await case.train.visit(MAIN)
        if replacement.state == "testing" and replacement.candidate_sha != first.candidate_sha:
            break
    assert replacement.candidate_sha != first.candidate_sha
    assert next(w for w in await rows(world) if w["id"] == old_id)["outcome"] == "cancelled"
    pending = [w for w in await rows(world) if w["outcome"] == "pending"]
    assert len(pending) == 1
    assert pending[0]["condition"]["head_sha"] == replacement.candidate_sha


async def test_unobservable_superseded_candidate_wait_is_cancelled_without_remote_read(world):
    case = await runtime(world)
    await completed(world, "first")
    first = await case.train.visit(MAIN)
    old_id = first.detail["dispatch"]["wait_ids"][0]
    await completed(world, "outside")
    world.origin.land("outside")
    case.service.registry.observe = AsyncMock(side_effect=WaitError(
        "wait.unavailable", "CI observation temporarily unavailable"))
    for _ in range(3):
        replacement = await case.train.visit(MAIN)
        if replacement.state == "testing" and replacement.candidate_sha != first.candidate_sha:
            break
    assert replacement.candidate_sha != first.candidate_sha
    assert next(w for w in await rows(world) if w["id"] == old_id)["outcome"] == "cancelled"


async def test_ci_observer_reuses_resolved_candidate_policy(world):
    from src.waits.conditions import Condition

    case = await runtime(world)
    await completed(world, "first")
    await case.train.visit(MAIN)
    checks = (await case.train.lane_for(MAIN)).checks
    checks.resolve.reset_mock()
    case.train.waits.lanes = AsyncMock(side_effect=AssertionError("fresh lane resolution"))
    wait = (await rows(world))[0]
    async with world.db._engine.connect() as conn:
        await case.orch.wait_ci_watcher.read(conn, Condition.from_record(wait["condition"]))
    checks.resolve.assert_not_awaited()


@pytest.mark.parametrize("decision", ["abort", "supersede"])
async def test_aborted_batch_source_retires_ci_waits_without_remote_observation(world, decision):
    from src.database.tables import wait_deadlines, wait_edges

    case = await runtime(world)
    await completed(world, "first")
    first = await case.train.visit(MAIN)
    wait = (await rows(world))[0]
    store = BatchStore(world.db)
    if decision == "abort":
        await store.set_intent(first.batch_id, "aborted", reason="obsolete candidate")
    else:
        assert await store.supersede(await store.get(first.batch_id), "first",
                                    reason="candidate superseded")
    # No new train visit and no CI observation are needed for the cleanup.
    case.service.registry.observe = AsyncMock(side_effect=AssertionError("remote CI read"))
    await case.service.listener.tick()
    retired = next(row for row in await rows(world) if row["id"] == wait["id"])
    assert retired["outcome"] == "cancelled" and retired["cleared_at"] is not None
    assert all(row["cancelled_at"] is not None for row in await rows(world, wait_deadlines)
               if row["wait_id"] == wait["id"] and row["fired_at"] is None)
    assert not any(row["active"] for row in await rows(world, wait_edges)
                   if row["wait_id"] == wait["id"])
    case.service.registry.observe.assert_not_awaited()


async def test_supervisor_inspects_and_retires_ci_blocker_without_remote_observation(world):
    from src.database.tables import events, wait_listener_cursors

    case = await runtime(world)
    await completed(world, "first")
    await case.train.visit(MAIN)
    wait = (await rows(world))[0]
    seq = await world.db.wait_source_commit(source_key=wait["source_key"], generation=0,
        revision="blocked", outcome_ref="check", payload={"repository_id": MAIN.repository_id})
    async with world.db._engine.begin() as conn:
        await conn.execute(update(wait_source_events).where(wait_source_events.c.seq == seq).values(
            dispatch_failures=3, quarantined_at=case.now[0], dispatch_retry_at=case.now[0] + 300))
    case.service.registry.observe = AsyncMock(side_effect=AssertionError("remote CI read"))
    with principal_context(TRUSTED_LOCAL):
        blockers = await case.handler.execute("wait_source_redispatch", {
            "action": "blockers", "project_id": "p"})
        assert blockers["success"], blockers
        blocker = next(row for row in blockers["result"]["blockers"] if row["seq"] == seq)
        assert blocker["dispatch_failures"] == 3
        assert blocker["subscribers"][0]["id"] == wait["id"]
        retired = await case.handler.execute("wait_source_redispatch", {
            "action": "retire", "source_seq": seq, "project_id": "p", "note": "obsolete CI"})
    assert retired["success"], retired
    assert retired["result"]["cancelled_wait_ids"] == [wait["id"]]
    assert retired["result"]["retired"]
    assert next(row for row in await rows(world) if row["id"] == wait["id"])[
        "outcome"] == "cancelled"
    assert next(row for row in await rows(world, wait_source_events) if row["seq"] == seq)[
        "dispatched_at"] is not None
    assert (await rows(world, wait_listener_cursors))[0]["last_seq"] == 0
    assert len([row for row in await rows(world, events)
                if row["event_type"] == "wait.source_retired"]) == 1
    case.service.registry.observe.assert_not_awaited()


async def test_local_checks_register_only_the_exact_pending_job_and_replace_hosted_wait(world):
    from sqlalchemy import insert

    from src.database.tables import jobs
    from src.integration.checks import LocalChecks
    from src.integration.runtime_contracts import CIState
    from src.integration.train import candidate_head

    case = await runtime(world)
    await completed(world, "first")
    first = await case.train.visit(MAIN)
    batch = await BatchStore(world.db).get(first.batch_id)
    head = candidate_head(batch, first.candidate_sha)
    producer = SimpleNamespace(plan=SimpleNamespace(commands=("test",), version="v1",
        queue_seconds=100, run_seconds=100, attempt_id="attempt"),
        job_states=AsyncMock(return_value=(("test", {"id": "local-job"},
            (CIState.PENDING, "running", None, None)),)))
    provider = LocalChecks(producer, project_id="p")
    async with world.db._engine.begin() as conn:
        await conn.execute(insert(jobs).values(
            id="local-job", project_id="p", owner_kind="integration",
            owner_id=provider._owner(head).id, input_ref=head.sha,
            idempotency_key="j", request_hash="hash", preset="test", preset_version=1,
            argv=[], contract={}, workspace_id="w", workspace_generation=1,
            input_mode="snapshot", job_class="shared", weight=1, priority_band=1,
            state="running", submitted_at=case.now[0], queue_deadline=case.now[0] + 100,
            run_timeout=100, runner_nonce="nonce"))
    exact = ExactChecks(world.db, provider)
    visit = await case.train.waits.sync(MAIN, batch, first, (), exact)
    assert len(visit["wait_ids"]) == 1 and "wait_errors" not in visit
    pending = next(w for w in await rows(world) if w["outcome"] == "pending")
    assert pending["condition_kind"] == "job" and pending["origin"] == "integration"
    assert pending["condition"]["job_id"] == "local-job"
    producer.job_states.assert_awaited_once_with(provider._owner(head), head)
    assert (await case.train.waits.sync(MAIN, batch, first, (), exact))["wait_ids"] == (
        visit["wait_ids"])


async def test_speculative_ci_run_wait_is_exact_and_cancelled_when_base_splits(world):
    from dataclasses import replace

    from src.waits.conditions import Condition, Scope, WaitError
    import pytest

    await world.db.update_project("p", hierarchical_integration_policy={
        "train": {"speculative_ci": True, "speculative_public_hosted": True},
    })
    case = await runtime(world)
    original = case.train.lane_for

    async def lane_for(target):
        current = await original(target)

        async def prepare(parent, base, snapshot):
            return await case.train.batches.prepare_speculative(
                target, current.service, parent, base, snapshot)

        return replace(current, prepare_speculative=prepare)

    case.train.lane_for = lane_for
    await completed(world, "first")
    first = await case.train.visit(MAIN)
    item = case.train._speculative[MAIN.key]
    await item.task
    case.train._discard_speculative(MAIN)
    await completed(world, "second")
    await case.train.visit(MAIN)
    item = case.train._speculative[MAIN.key]
    await item.task
    assert item.sha
    observed = await case.train.visit(MAIN)
    assert not observed.detail["dispatch"].get("wait_errors"), observed.detail
    runs = [row for row in await rows(world) if row["condition_kind"] == "ci_run"]
    assert len(runs) == 1
    run = runs[0]
    assert run["condition"]["head_sha"] == item.sha
    assert run["condition"]["run_id"] == "run-1"
    assert run["outcome"] == "pending"
    assert len(await rows(world, integration_batches)) == 1
    adapter = case.service.registry.adapter("ci_run")
    condition = Condition.from_record(run["condition"])
    async with world.db._engine.connect() as conn:
        with pytest.raises(WaitError, match="head is not known"):
            await adapter.authorise(conn, condition, Scope(
                project_id="p", owner_kind="integration", owner_id="untrusted", principal="agent"))
    await (await original(MAIN)).service.store.set_intent(first.batch_id, "aborted")
    await case.train.visit(MAIN)
    assert next(row for row in await rows(world) if row["id"] == run["id"])["outcome"] == "cancelled"
    await case.train.stop()


@pytest.mark.parametrize("subscriber", ["foreign_project", "task"])
async def test_source_retirement_preserves_subscribers_outside_integration_scope(world, subscriber):
    from sqlalchemy import insert
    from src.database.tables import projects

    case = await runtime(world)
    await completed(world, "first")
    await case.train.visit(MAIN)
    wait = (await rows(world))[0]
    seq = await world.db.wait_source_commit(source_key=wait["source_key"], generation=0,
        revision="blocked", outcome_ref="check", payload={"repository_id": MAIN.repository_id})
    async with world.db._engine.begin() as conn:
        if subscriber == "foreign_project":
            await conn.execute(insert(projects).values(id="q", name="Other", created_at=0))
            await conn.execute(insert(wait_records).values(**{
                **wait, "id": "foreign-wait", "project_id": "q", "owner_id": "foreign"}))
        else:
            await conn.execute(update(wait_records).where(wait_records.c.id == wait["id"]).values(
                owner_kind="task", owner_id="first"))
    with principal_context(TRUSTED_LOCAL):
        result = await case.handler.execute("wait_source_redispatch", {
            "action": "retire", "source_seq": seq, "project_id": "p", "note": "scoped cleanup"})
    waits = {row["id"]: row for row in await rows(world)}
    if subscriber == "foreign_project":
        assert result["success"], result
        assert not result["result"]["retired"]
        assert waits[wait["id"]]["outcome"] == "cancelled"
        assert waits["foreign-wait"]["outcome"] == "pending"
    else:
        assert not result["success"] and result["error_code"] == "wait.out_of_scope"
        assert waits[wait["id"]]["outcome"] == "pending"
    assert next(row for row in await rows(world, wait_source_events) if row["seq"] == seq)[
        "dispatched_at"] is None


async def test_aborted_batch_preserves_head_reused_by_active_batch(world):
    from sqlalchemy import insert
    from src.waits.conditions import Condition

    case = await runtime(world)
    await completed(world, "first")
    first = await case.train.visit(MAIN)
    wait = (await rows(world))[0]
    batch = (await rows(world, integration_batches))[0]
    # A newer aborted batch must not shadow an active candidate with the same SHA.
    async with world.db._engine.begin() as conn:
        await conn.execute(insert(integration_batches).values(**{
            **batch, "id": "aborted-reuse", "request_id": "aborted-reuse", "intent": "aborted",
            "lifecycle": "aborted", "created_at": batch["created_at"] + 1}))
    await world.db.wait_source_commit(source_key="batch:aborted-reuse", generation=0,
        revision="aborted", outcome_ref="batch", payload={"project_id": "p"})
    await case.service.listener.tick()
    assert next(row for row in await rows(world) if row["id"] == wait["id"])[
        "outcome"] == "pending"
    repo = {"id": MAIN.repository_id, "project_id": MAIN.project_id}
    assert await case.train.waits.checks(repo, Condition.from_record(wait["condition"])) is case.exact
    assert (await BatchStore(world.db).get(first.batch_id)).intent == "open"
