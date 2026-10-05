"""The reduced train: per-target level-triggered visits over Git, checks and intent."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.integration.batches import Batch, BatchMember, BatchObservation, candidate_ref
from src.integration.checks import ChecksState
from src.integration.train import (
    CandidateChecks,
    IntegrationTrain,
    TrainLane,
    TrainTarget,
    candidate_head,
    exact_gate,
)

TARGET_SHA = "a" * 40
CANDIDATE = "b" * 40
SOURCE = "c" * 40


def batch(batch_id="batch-1", ref="refs/heads/main", intent="open", attempts=0) -> Batch:
    return Batch(id=batch_id, project_id="p", repository_id="r", target_ref=ref,
                 intent=intent, repair_attempt_count=attempts)


MEMBERS = (BatchMember(task_id="t-1", source_sha=SOURCE, source_base_sha=TARGET_SHA),)


def result(state: ChecksState):
    return SimpleNamespace(state=state, green=state is ChecksState.GREEN)


class Service:
    def __init__(self, *states):
        self.states = list(states)
        self.calls = 0

    async def visit(self, batch, members, snapshot):
        self.calls += 1
        state = self.states.pop(0)
        candidate = None if state == "conflict" else CANDIDATE
        return BatchObservation(state, candidate, TARGET_SHA, detail={"step": self.calls})


class Checks:
    def __init__(self, *states):
        self.states = list(states)
        self.requests, self.refreshes, self.reads = [], [], []

    async def request(self, head):
        self.requests.append(head)

    async def refresh(self, head):
        self.refreshes.append(head)
        return result(self.states.pop(0))

    async def read(self, head):
        self.reads.append(head)
        return result(self.states[0])


class Batches:
    def __init__(self, opened=None):
        self.opened = {} if opened is None else opened
        self.settled = []

    async def open_batch(self, target, snapshot, service):
        return self.opened.get(target.key)

    async def settle(self, batch, observation):
        self.settled.append((batch.id, observation.state))


class Targets:
    def __init__(self, *targets):
        self.items = list(targets)

    async def targets(self, now):
        return self.items


class Repair:
    def __init__(self):
        self.calls = []

    async def allocate(self, batch_id, **kwargs):
        self.calls.append((batch_id, kwargs))
        return {"outcome": "filed", "task_id": f"repair-{batch_id}", "attempts": 1}


ROOT = TrainTarget("p", "r", "refs/heads/main")


def snapshot():
    return SimpleNamespace(target_oid=TARGET_SHA)


def train(targets, batches, lanes, repair=None, **kwargs):
    async def lane_for(target):
        return lanes[target.key]

    return IntegrationTrain(targets=targets, batches=batches, lane_for=lane_for,
                            repair=repair or Repair(), clock=lambda: 100.0, **kwargs)


def lane(service, checks=None, fetch=None):
    async def snap():
        if fetch is not None:
            await fetch()
        return snapshot()

    return TrainLane(snapshot=snap, service=service,
                     checks=CandidateChecks.fixed(checks or Checks()))


def test_target_rejects_unknown_kind_and_missing_fields():
    with pytest.raises(ValueError):
        TrainTarget("p", "r", "refs/heads/main", kind="legacy")
    with pytest.raises(ValueError):
        TrainTarget("p", "", "refs/heads/main")
    with pytest.raises(ValueError):
        TrainTarget("p", "r", "main")


async def test_no_open_batch_is_idle_and_reports_the_fetched_target():
    t = train(Targets(ROOT), Batches(), {ROOT.key: lane(Service())})
    visit = await t.visit(ROOT)
    assert (visit.state, visit.batch_id, visit.target_sha) == ("idle", None, TARGET_SHA)


async def test_contained_candidate_settles_without_checks_or_repair():
    checks, repair = Checks(), Repair()
    batches = Batches({ROOT.key: (batch(), MEMBERS)})
    t = train(Targets(ROOT), batches, {ROOT.key: lane(Service("delivered"), checks)}, repair)
    visit = await t.visit(ROOT)
    assert visit.state == "delivered"
    assert batches.settled == [("batch-1", "delivered")]
    assert checks.requests == [] and repair.calls == []


async def test_green_exact_candidate_publishes_within_the_visit():
    service, checks = Service("testing", "delivered"), Checks(ChecksState.GREEN)
    batches = Batches({ROOT.key: (batch(), MEMBERS)})
    t = train(Targets(ROOT), batches, {ROOT.key: lane(service, checks)})
    visit = await t.visit(ROOT)
    assert (visit.state, visit.checks) == ("delivered", "green")
    assert service.calls == 2
    head = checks.requests[0]
    assert (head.sha, head.ref, head.generation) == (CANDIDATE, candidate_ref("batch-1"), 0)
    assert checks.refreshes == [head]
    assert batches.settled == [("batch-1", "delivered")]


async def test_pending_checks_wait_without_repair():
    repair = Repair()
    t = train(Targets(ROOT), Batches({ROOT.key: (batch(), MEMBERS)}),
              {ROOT.key: lane(Service("testing"), Checks(ChecksState.PENDING))}, repair)
    visit = await t.visit(ROOT)
    assert (visit.state, visit.checks) == ("testing", "pending")
    assert repair.calls == []


async def test_red_candidate_allocates_one_repair_for_the_exact_head():
    repair = Repair()
    t = train(Targets(ROOT), Batches({ROOT.key: (batch(attempts=2), MEMBERS)}),
              {ROOT.key: lane(Service("testing"), Checks(ChecksState.RED))}, repair)
    visit = await t.visit(ROOT)
    assert visit.state == "repair"
    assert visit.repair["task_id"] == "repair-batch-1"
    assert repair.calls == [("batch-1", {"target_ref": candidate_ref("batch-1"),
                                         "head_sha": CANDIDATE, "held": False})]


async def test_green_on_refresh_ends_repair_without_an_attempt():
    """A red cache refreshed to green publishes; no repair is filed."""
    repair = Repair()
    checks = Checks(ChecksState.GREEN)
    t = train(Targets(ROOT), Batches({ROOT.key: (batch(), MEMBERS)}),
              {ROOT.key: lane(Service("testing", "delivered"), checks)}, repair)
    assert (await t.visit(ROOT)).state == "delivered"
    assert repair.calls == []


async def test_merge_conflict_files_repair_against_the_target_head():
    repair = Repair()
    t = train(Targets(ROOT), Batches({ROOT.key: (batch(), MEMBERS)}),
              {ROOT.key: lane(Service("conflict"))}, repair)
    visit = await t.visit(ROOT)
    assert visit.state == "repair"
    assert repair.calls[0][1]["head_sha"] == TARGET_SHA
    assert repair.calls[0][1]["target_ref"] == candidate_ref("batch-1")


@pytest.fixture
async def db(reuse_database):
    from src.models import Project, RepoConfig, RepoSourceType

    database = await reuse_database("train.db")
    await database.create_project(Project(id="p", name="Project"))
    await database.create_repo(RepoConfig(id="r", project_id="p",
                                          source_type=RepoSourceType.LINK))
    yield database


async def test_red_candidate_files_one_ordinary_repair_on_the_candidate_ref(db):
    """Repeated red visits file one ordinary task; it works on the candidate ref."""
    from src.integration.batches import BatchStore
    from src.integration.repair import OrdinaryRepairService

    store = BatchStore(db)
    frozen = await store.freeze(batch("batch-db"), MEMBERS, trees={"t-1": "d" * 40})
    t = train(Targets(ROOT), Batches({ROOT.key: (frozen, MEMBERS)}),
              {ROOT.key: lane(Service("testing", "testing"),
                              Checks(ChecksState.RED, ChecksState.RED, ChecksState.RED))},
              OrdinaryRepairService(db))
    first, second = await t.visit(ROOT), await t.visit(ROOT)
    assert (first.repair["outcome"], second.repair["outcome"]) == ("filed", "exists")
    assert second.repair["task_id"] == first.repair["task_id"]
    task = await db.get_task(first.repair["task_id"])
    assert task.branch_name == candidate_ref("batch-db").removeprefix("refs/heads/")
    assert task.branch_name != "main"
    assert (await store.get("batch-db")).repair_attempt_count == 1


@pytest.mark.parametrize("state", ["held", "moved", "source_moved", "unknown"])
async def test_non_progress_observations_wait_for_the_next_visit(state):
    repair, checks = Repair(), Checks()
    t = train(Targets(ROOT), Batches({ROOT.key: (batch(), MEMBERS)}),
              {ROOT.key: lane(Service(state), checks)}, repair)
    assert (await t.visit(ROOT)).state == state
    assert repair.calls == [] and checks.requests == []


async def test_gate_reads_only_the_cached_verdict():
    checks = Checks(ChecksState.GREEN)
    gate = exact_gate(checks)
    assert await gate(batch(attempts=3), CANDIDATE, "d" * 40)
    assert checks.requests == [] and checks.refreshes == []
    assert checks.reads == [candidate_head(batch(attempts=3), CANDIDATE)]
    assert checks.reads[0].generation == 3


async def test_candidate_gate_refuses_a_candidate_this_lane_never_resolved():
    checks = Checks(ChecksState.GREEN)
    resolved = []

    async def resolve(b, sha):
        resolved.append((b.id, sha))
        return checks

    candidates = CandidateChecks(resolve)
    assert not await candidates.gate(batch(), CANDIDATE, "d" * 40)
    assert checks.reads == [] and resolved == []
    assert await candidates.for_candidate(batch(), CANDIDATE) is checks
    assert await candidates.gate(batch(), CANDIDATE, "d" * 40)
    assert not await candidates.gate(batch("batch-2"), CANDIDATE, "d" * 40)
    assert resolved == [("batch-1", CANDIDATE)]


async def test_slow_target_never_stalls_another():
    slow, fast = TrainTarget("p", "r", "refs/heads/main"), TrainTarget("q", "s", "refs/heads/main", "development")
    release = asyncio.Event()

    async def blocked_fetch():
        await release.wait()

    batches = Batches({slow.key: (batch(), MEMBERS), fast.key: (batch("batch-2"), MEMBERS)})
    t = train(Targets(slow, fast), batches, {
        slow.key: lane(Service("delivered"), fetch=blocked_fetch),
        fast.key: lane(Service("delivered", "delivered")),
    })
    first = await t.tick()
    assert sorted(first["started"]) == ["p/r/refs/heads/main", "q/s/refs/heads/main"]
    for _ in range(5):
        await asyncio.sleep(0)
    assert batches.settled == [("batch-2", "delivered")]

    second = await t.tick()
    assert second["running"] == ["p/r/refs/heads/main"] and second["started"] == ["q/s/refs/heads/main"]
    release.set()
    await t.drain()
    assert sorted(batches.settled) == [("batch-1", "delivered"), ("batch-2", "delivered"),
                                       ("batch-2", "delivered")]
    status = {row["project_id"]: row for row in t.status()}
    assert status["p"]["visits"] == 1 and status["q"]["visits"] == 2
    assert not any(row["running"] for row in status.values())


async def test_one_target_failure_is_recorded_and_others_continue():
    broken, healthy = TrainTarget("p", "r", "refs/heads/main"), TrainTarget("q", "s", "refs/heads/main")

    class Exploding(Service):
        async def visit(self, *args):
            raise RuntimeError("git exploded")

    batches = Batches({broken.key: (batch(), MEMBERS), healthy.key: (batch("b2"), MEMBERS)})
    t = train(Targets(broken, healthy), batches, {
        broken.key: lane(Exploding()), healthy.key: lane(Service("delivered")),
    })
    await t.tick()
    await t.drain()
    status = {row["project_id"]: row for row in t.status()}
    assert status["p"]["state"] == "unknown" and status["p"]["errors"] == 1
    assert status["p"]["detail"]["reason"] == "RuntimeError"
    assert status["q"]["state"] == "delivered"


async def test_visit_timeout_frees_the_target_for_the_next_tick():
    async def hang():
        await asyncio.sleep(3600)

    t = train(Targets(ROOT), Batches(), {ROOT.key: lane(Service(), fetch=hang)},
              visit_timeout_seconds=0.01)
    await t.tick()
    await t.drain()
    [row] = t.status()
    assert (row["state"], row["detail"], row["running"]) == (
        "unknown", {"reason": "visit_timeout"}, False)
    assert (await t.tick())["started"] == ["p/r/refs/heads/main"]
    await t.stop()


async def test_restart_resumes_from_git_without_any_notification():
    """A fresh train over the same refs settles a candidate a crash left behind."""
    batches = Batches({ROOT.key: (batch(), MEMBERS)})
    crashed = train(Targets(ROOT), batches, {ROOT.key: lane(Service("testing"),
                                                            Checks(ChecksState.PENDING))})
    assert (await crashed.visit(ROOT)).state == "testing"
    await crashed.stop()

    restarted = train(Targets(ROOT), batches, {ROOT.key: lane(Service("delivered"))})
    await restarted.tick()
    await restarted.drain()
    assert restarted.status()[0]["state"] == "delivered"
    assert batches.settled == [("batch-1", "delivered")]


async def test_overlapping_ticks_do_not_double_start():
    gate = asyncio.Event()

    class SlowTargets(Targets):
        async def targets(self, now):
            await gate.wait()
            return self.items

    t = train(SlowTargets(ROOT), Batches(), {ROOT.key: lane(Service())})
    first = asyncio.create_task(t.tick())
    await asyncio.sleep(0)
    assert (await t.tick())["skipped"] == ["tick_in_progress"]
    gate.set()
    assert (await first)["started"] == ["p/r/refs/heads/main"]
    await t.drain()


def test_visit_timeout_must_be_positive():
    with pytest.raises(ValueError):
        train(Targets(), Batches(), {}, visit_timeout_seconds=0)


@pytest.mark.parametrize("kind", [None, "local", "session", "playbook"])
async def test_tick_command_is_daemon_only(kind):
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import DENY_ALL

    class Train:
        async def tick(self, now):
            raise AssertionError("an untrusted caller reached the train")

    handler = IntegrationCommandsMixin()
    handler.orchestrator = SimpleNamespace(integration_train=Train())
    if kind is None:
        result = await handler._cmd_integration_train_tick({})
    else:
        principal = ExecutionPrincipal(kind=PrincipalKind(kind), policy=DENY_ALL, elevated=True)
        with principal_context(principal):
            result = await handler._cmd_integration_train_tick({})
    assert result == {"success": False, "outcome": "unauthorized",
                      "error": "only the daemon may tick the integration train"}


async def test_tick_command_reports_an_inactive_train():
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import ExecutionPrincipal, principal_context

    handler = IntegrationCommandsMixin()
    handler.orchestrator = SimpleNamespace(integration_train=None)
    with principal_context(ExecutionPrincipal.service("integration-train")):
        result = await handler._cmd_integration_train_tick({"now": 5.0})
    assert result["success"] is False and result["outcome"] == "unavailable"


async def test_service_drives_the_train_through_the_command_as_the_daemon():
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import PrincipalKind, current_principal
    from src.integration.train_sources import TrainCommandDriver

    seen, stopped = [], []

    class Train:
        async def tick(self, now):
            seen.append((now, current_principal().kind, current_principal().service_name))
            return {"started": ["p/r/refs/heads/main"], "running": [], "skipped": []}

        async def stop(self):
            stopped.append(True)

    class Handler(IntegrationCommandsMixin):
        async def execute(self, name, args):
            assert name == "integration_train_tick"
            return await self._cmd_integration_train_tick(args)

    train = Train()
    handler = Handler()
    handler.orchestrator = SimpleNamespace(integration_train=train)
    driver = TrainCommandDriver(train, lambda: handler)

    assert await driver.tick(7.0) == {
        "success": True, "started": ["p/r/refs/heads/main"], "running": [], "skipped": [],
    }
    assert seen == [(7.0, PrincipalKind.SERVICE, "integration-train")]
    assert current_principal() is None
    await driver.stop()
    assert stopped == [True]


def test_tick_command_stays_off_every_external_surface():
    from src.api.codegen import API_EXCLUDED
    from src.cli.auto_commands import EXCLUDED
    from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS, effective_tool_definitions
    from src.tools.definitions import _FALLBACK_INPUT_SCHEMAS

    name = "integration_train_tick"
    assert name in API_EXCLUDED and name in EXCLUDED and name in DEFAULT_EXCLUDED_COMMANDS
    assert "now" in _FALLBACK_INPUT_SCHEMAS[name]["properties"]
    assert name not in {tool["name"] for tool in effective_tool_definitions()}


async def test_driver_skips_until_a_command_handler_is_attached():
    from src.integration.train_sources import TrainCommandDriver

    driver = TrainCommandDriver(SimpleNamespace(), lambda: None)
    assert await driver.tick(1.0) == {"success": False, "outcome": "unavailable",
                                      "error": "no command handler is attached yet"}
