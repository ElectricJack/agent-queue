"""The train's shared waits use verified sources and survive policy replacement."""

from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from src.commands import CommandHandler
from src.commands.principal import TRUSTED_LOCAL, principal_context
from src.config import AppConfig
from src.database.tables import integration_batches, integration_outbox, wait_records, wait_source_events
from src.integration.batches import BatchStore
from src.integration.checks import CommitCheck, Conclusion, ExactChecks, RequiredChecks
from src.integration.dispatch_waits import DispatchWaits
from src.integration.outbox import IntegrationOutbox
from src.wait_runtime.service import WaitService
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
