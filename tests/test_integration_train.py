"""The reduced train: per-target level-triggered visits over Git, checks and intent."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dataclasses import replace

from sqlalchemy import func, select

from src.database.tables import escalations, integration_batches, messages, tasks
from src.git.github_contracts import GitHubAccessError
from src.git.manager import GitError
from src.integration.batches import Batch, BatchMember, BatchObservation, candidate_ref
from src.integration.checks import (
    ChecksResult,
    ChecksState,
    CommitCheck,
    Conclusion,
    RequiredChecks,
)
from src.integration.selection_metrics import selection_count, selection_stage
from src.messages.delivery import MessageDeliveryEngine
from src.models import Message
from src.integration.train import (
    BatchSelection,
    CandidateChecks,
    IntegrationTrain,
    TrainLane,
    TrainTarget,
    candidate_head,
    conflict_brief,
    exact_gate,
    sync_default_branch_brief,
)
from tests import test_integration_gitops
from tests.test_integration_gitops import commit, git

#: The real Git/PostgreSQL repository substrate, re-exported for the fixture below.
setup = test_integration_gitops.setup

TARGET_SHA = "a" * 40
CANDIDATE = "b" * 40
SOURCE = "c" * 40
PARTIAL = "d" * 40


def batch(batch_id="batch-1", ref="refs/heads/main", intent="open", attempts=0) -> Batch:
    return Batch(id=batch_id, project_id="p", repository_id="r", target_ref=ref,
                 intent=intent, repair_attempt_count=attempts)


MEMBERS = (BatchMember(task_id="t-1", source_sha=SOURCE, source_base_sha=TARGET_SHA),)
CONFLICTING_MEMBERS = (
    BatchMember(task_id="t-1", source_sha=SOURCE, source_base_sha=TARGET_SHA, order=0),
    BatchMember(task_id="t-2", source_sha="e" * 40, source_base_sha=TARGET_SHA, order=1),
    BatchMember(task_id="t-3", source_sha="f" * 40, source_base_sha=TARGET_SHA, order=2),
)
#: The exact ``merge_sources`` conflict result for those members.
CONFLICT = {
    "outcome": "conflict", "head": PARTIAL, "reason": "alembic_head_collision",
    "member": "t-2", "files": ["migrations/versions/head.py"],
    "members": [{"member": "t-1", "source": SOURCE, "head": PARTIAL, "regenerated": False}],
}


def result(state: ChecksState):
    """A cached exact-head verdict, with the check rows a red one is judged by."""
    return ChecksResult(
        repository_id="r", sha=CANDIDATE,
        required=RequiredChecks(version="v1", names=("unit",), producer_id="p"),
        state=state, checks=(
            CommitCheck(
                repository_id="r", sha=CANDIDATE, name="unit", producer_id="p",
                required_check_version="v1",
                conclusion=Conclusion.FAILURE if state is ChecksState.RED else (
                    Conclusion.PENDING if state is ChecksState.PENDING else Conclusion.SUCCESS
                ),
                classification="conclusive", observed_at=1.0,
            ),
        ),
    )


class Service:
    def __init__(self, *states):
        self.states = list(states)
        self.calls = 0

    async def visit(self, batch, members, snapshot):
        self.calls += 1
        state = self.states.pop(0)
        candidate = TARGET_SHA if state == "conflict" else CANDIDATE
        return BatchObservation(state, candidate, TARGET_SHA, detail={"step": self.calls})

    async def repair_authorized(self, batch, members, head_sha):
        return True


class Conflicting(Service):
    """A merge conflict carrying one exact ``merge_sources`` detail."""

    def __init__(self, detail, candidate=None):
        self.detail, self.candidate = detail, candidate
        self.calls = 0

    async def visit(self, batch, members, snapshot):
        self.calls += 1
        return BatchObservation("conflict", self.candidate, TARGET_SHA, detail=self.detail)


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
        opened = self.opened.get(target.key)
        return BatchSelection(*opened) if opened else BatchSelection()

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

    async def settle_green(self, batch_id, head_sha):
        pass

    async def allocate(self, batch_id, **kwargs):
        self.calls.append((batch_id, kwargs))
        return {"outcome": "filed", "task_id": f"repair-{batch_id}", "attempts": 1}

    async def recover_reservation(self, batch_id, **kwargs):
        return {"outcome": "none"}


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


async def test_promotion_target_visits_its_own_lane_and_keeps_a_step_snapshot():
    step = {"id": "release", "target": "main", "gate": {"approval": "operator"}}
    promotion = TrainTarget("p", "r", "refs/heads/main", "promotion", step=step)
    development = TrainTarget("p", "r", "refs/heads/dev", "development")
    step["gate"]["approval"] = "requester"
    assert promotion.step_id == "release"
    assert promotion.step["gate"]["approval"] == "operator"
    t = train(Targets(development, promotion), Batches(), {
        development.key: lane(Service()), promotion.key: lane(Service()),
    })
    started = await t.tick()
    await t.drain()
    assert len(started["started"]) == 2
    assert {(row["target_ref"], row["kind"], row["state"]) for row in t.status()} == {
        ("refs/heads/dev", "development", "idle"),
        ("refs/heads/main", "promotion", "idle"),
    }


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


async def test_post_delivery_epic_snapshot_does_not_reuse_an_older_sibling_fetch(setup, tmp_path):
    from src.integration.git_truth import GitTruth

    _db, ops, _subject, _fence, repo, base, head, _green = setup
    epic = TrainTarget("p", "r", "refs/heads/epic", kind="epic")
    await ops.push(repo, epic.target_ref, base, "")
    writer = tmp_path / "writer.git"
    await ops.git._arun(["clone", "--bare", str(repo.store), str(writer)], cwd=str(tmp_path))
    truth = GitTruth(ops.git, share_fetches=True)
    fetch = ops.git.afetch_origin
    entered, release, resnapshot = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked_sibling_fetch(*args, **kwargs):
        await fetch(*args, **kwargs)
        if ops.git.afetch_origin.await_count == 2:
            entered.set()
            await release.wait()

    ops.git.afetch_origin = AsyncMock(side_effect=blocked_sibling_fetch)

    async def snapshot_for(target):
        return await truth.snapshot(
            str(repo.store), project_id="p", repository_id="r",
            repository_url=str(ops.git.remote_path), target_ref=target.target_ref,
        )

    epic_reads = 0

    async def epic_snapshot():
        nonlocal epic_reads
        epic_reads += 1
        if epic_reads == 2:
            resnapshot.set()
        return await snapshot_for(epic)

    siblings = []

    async def deliver(batch, members, snapshot):
        assert snapshot.target_oid == base
        siblings.append(asyncio.create_task(snapshot_for(ROOT)))
        await entered.wait()
        await ops.push(replace(repo, store=writer), epic.target_ref, head, base)
        return BatchObservation("delivered", head, head)

    completion_heads = []

    async def complete_epic(snapshot):
        completion_heads.append(snapshot.target_oid)
        assert await snapshot.is_fresh()
        return ({"blocking": False, "outcome": "completed", "head": snapshot.target_oid},)

    batches = Batches({epic.key: (batch(ref=epic.target_ref), MEMBERS)})
    t = train(Targets(epic), batches, {epic.key: TrainLane(
        snapshot=epic_snapshot, service=SimpleNamespace(visit=deliver),
        checks=CandidateChecks.fixed(Checks()), complete_epic=complete_epic,
    )})
    visit_task = asyncio.create_task(t.visit(epic))
    await resnapshot.wait()
    release.set()
    visit = await visit_task
    [sibling] = await asyncio.gather(*siblings)
    assert (visit.state, completion_heads) == ("delivered", [head])
    assert visit.detail["epic_completions"][0]["head"] == head
    assert sibling.for_target(epic.target_ref).target_oid == base
    assert ops.git.afetch_origin.await_count == 3
    assert not truth._fetches


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
    [(batch_id, args)] = repair.calls
    assert batch_id == "batch-1"
    assert (args["target_ref"], args["head_sha"], args["held"]) == (
        candidate_ref("batch-1"), CANDIDATE, False)
    assert await args["authorize"]()
    assert args["brief"].startswith(f"Required checks are red on candidate {CANDIDATE}")
    assert "target baseline is unavailable" in args["brief"]
    assert "  - unit" in args["brief"]


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


async def test_conflict_brief_names_the_conflicting_and_remaining_members():
    """The worker reads which members it still owes, in order, on the starting head."""
    repair = Repair()
    service = Conflicting(CONFLICT, candidate=PARTIAL)
    t = train(Targets(ROOT), Batches({ROOT.key: (batch(), CONFLICTING_MEMBERS)}),
              {ROOT.key: lane(service)}, repair)
    visit = await t.visit(ROOT)
    assert (visit.state, repair.calls[0][1]["head_sha"]) == ("repair", PARTIAL)
    brief = repair.calls[0][1]["brief"]
    assert brief == conflict_brief(CONFLICT, CONFLICTING_MEMBERS, starting_sha=PARTIAL)
    # A raw detail dict is never an instruction.
    assert "{" not in brief and "}'" not in brief
    assert "t-2" in brief and "e" * 40 in brief
    assert "migrations/versions/head.py" in brief and "alembic_head_collision" in brief
    assert f"Already merged into the starting head (do not merge them again):\n  1. t-1 (source {SOURCE})" in brief
    assert (f"Still to merge onto the starting head, in this order:\n"
            f"  1. t-2 (source {'e' * 40})\n  2. t-3 (source {'f' * 40})") in brief
    assert f"starting head {PARTIAL} in that order" in brief
    assert "Generated files are regenerated, never hand-merged" in brief
    assert "scripts/regenerate-generated.sh" in brief
    assert "Do not stop once the conflict is resolved" in brief


def test_conflict_brief_owes_every_member_when_start_is_not_the_partial_head():
    """A published target start carries no members from another partial head."""
    brief = conflict_brief(CONFLICT, CONFLICTING_MEMBERS, starting_sha=TARGET_SHA)
    assert "Nothing is merged into the starting head yet." in brief
    assert (f"Still to merge onto the starting head, in this order:\n"
            f"  1. t-1 (source {SOURCE})\n  2. t-2 (source {'e' * 40})\n"
            f"  3. t-3 (source {'f' * 40})") in brief


def test_conflict_brief_without_merge_evidence_still_instructs_every_member():
    brief = conflict_brief({"reason": "conflict"}, MEMBERS, starting_sha=TARGET_SHA)
    assert "{" not in brief and "not reported by the merge" in brief
    assert f"  1. t-1 (source {SOURCE})" in brief
    assert f"starting head {TARGET_SHA}" in brief


async def test_unpublished_conflict_never_dispatches_repair():
    class Unpublished(Service):
        async def visit(self, *args):
            return BatchObservation("conflict", target_sha=TARGET_SHA,
                                    detail={"reason": "merge_conflict", "member": "t-1"})

    repair = Repair()
    t = train(Targets(ROOT), Batches({ROOT.key: (batch(), MEMBERS)}),
              {ROOT.key: lane(Unpublished())}, repair)
    visit = await t.visit(ROOT)
    assert visit.state == "unknown"
    assert visit.detail["repair_publication"] == "unpublished"
    assert visit.detail["member"] == "t-1"
    assert repair.calls == []


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


async def complete_ordinary_repair(db, repairs, task_id, ref):
    from src.integration.models import BranchKey
    from src.models import TaskStatus

    await db.update_task(task_id, status=TaskStatus.COMPLETED)
    lease = await repairs.locks.get(BranchKey(repository_id="r", branch=ref))
    await repairs.locks.release(lease.grant())


@pytest.mark.parametrize("recovery", ["checks", "contained", "allocation", "advisory"])
async def test_repair_escalation_debug_then_one_supervisor_incident_and_green_recovery(db, recovery):
    """Spec §6.7: allocations escalate; repeated reds/restarts never spend attempts."""
    from src.integration.batches import BatchStore
    from src.integration.models import RepairPolicy
    from src.integration.repair import OrdinaryRepairService
    from src.models import TaskType

    store, repairs = BatchStore(db), OrdinaryRepairService(db)
    frozen = await store.freeze(batch("batch-escalation"), MEMBERS, trees={"t-1": PARTIAL})
    policy = RepairPolicy(primary_attempts=1, debug_attempts=1, primary_seconds=30,
                          debug_seconds=60, debug_intelligence_class="deep-high",
                          debug_profile_id="ignored-legacy-profile", on_exhausted="human")
    checks = Checks(*(ChecksState.RED,) * 5, ChecksState.GREEN)
    current = replace(lane(Service(*("testing",) * 6, "delivered"), checks),
                      repair_policy=policy)
    t = train(Targets(ROOT), Batches({ROOT.key: (frozen, MEMBERS)}), {ROOT.key: current}, repairs)
    first = await t.visit(ROOT)
    task = await db.get_task(first.repair["task_id"])
    assert task.task_type == TaskType.BUGFIX and task.profile_id is None
    assert task.class_hint is None
    assert (await repairs.input(task.id))["budget_seconds"] == 30
    # An in-flight primary is found even though the counter reached its limit.
    assert (await t.visit(ROOT)).repair["task_id"] == task.id
    await complete_ordinary_repair(db, repairs, task.id, candidate_ref(frozen.id))
    second = await t.visit(ROOT)
    task = await db.get_task(second.repair["task_id"])
    assert task.task_type == TaskType.BUGFIX and task.profile_id is None
    assert task.class_hint == "deep-high"
    assert (await repairs.input(task.id))["stage"] == "debug"
    assert (await repairs.input(task.id))["budget_seconds"] == 60
    assert "work budget: 60 seconds" in task.description
    await complete_ordinary_repair(db, repairs, task.id, candidate_ref(frozen.id))
    third = await t.visit(ROOT)
    assert (third.state, third.repair["outcome"]) == ("blocked", "human_required")
    assert third.repair["attempt_count"] == 2
    blocker = AsyncMock(return_value={"reason": "repair_completion_unconfirmed"})
    concurrent = await asyncio.gather(*(repairs.allocate(
        frozen.id, target_ref=candidate_ref(frozen.id), head_sha=CANDIDATE, policy=policy,
        completion_blocker=blocker,
    ) for _ in range(4)))
    assert all(row["escalation_id"] == third.repair["escalation_id"] for row in concurrent)
    blocker.assert_not_awaited()  # A proof blocker cannot hide the exhausted budget.
    # The allocator is stateless: a daemon restart finds the same incident/notice.
    t.repair = OrdinaryRepairService(db)
    repeated = await t.visit(ROOT)
    assert repeated.repair["escalation_id"] == third.repair["escalation_id"]
    incident = await db.get_escalation(third.repair["escalation_id"])
    assert incident["source_identity"] == frozen.id and incident["state"] == "needs_human"
    assert incident["supervisor_owner"] == "supervisor-p"
    assert incident["choices"] == ["eject", "abort-batch"]
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(tasks)) == 2
        assert await conn.scalar(select(func.count()).select_from(escalations)) == 1
        assert await conn.scalar(select(func.count()).select_from(messages).where(
            messages.c.body_kind == "integration_repair_exhausted",
        )) == 1
    if recovery == "allocation":
        green = await t.repair.allocate(
            frozen.id, target_ref=candidate_ref(frozen.id), head_sha=CANDIDATE,
            green_sha=CANDIDATE, policy=policy,
        )
        assert (green["outcome"], green["attempt_count"]) == ("green", 2)
    else:
        if recovery == "contained":
            # Cleanup can remove the candidate ref after Git proves delivery.
            current.service.visit = AsyncMock(return_value=BatchObservation(
                "delivered", target_sha=TARGET_SHA,
            ))
        elif recovery == "advisory":
            checks.states = [ChecksState.RED]
            current.checks.advisory = True
        assert (await t.visit(ROOT)).state == "delivered"
    cleared = await db.get_escalation(incident["id"])
    assert cleared["state"] == "resolved"
    assert cleared["terminal_evidence"] == {
        "batch_id": frozen.id, "candidate_sha": TARGET_SHA if recovery == "contained" else CANDIDATE,
    }
    assert (await store.get(frozen.id)).repair_attempt_count == 2


@pytest.mark.parametrize("green_after", [1, 2])
async def test_green_repair_bypasses_primary_and_debug_limits(db, green_after):
    from src.integration.batches import BatchStore
    from src.integration.models import RepairPolicy
    from src.integration.repair import OrdinaryRepairService

    store, repairs = BatchStore(db), OrdinaryRepairService(db)
    frozen = await store.freeze(batch("batch-green"), MEMBERS, trees={"t-1": PARTIAL})
    policy = RepairPolicy(primary_attempts=1, debug_attempts=1, debug_intelligence_class="deep-high")
    current = replace(lane(Service(*("testing",) * (green_after + 1), "delivered"),
                           Checks(*(ChecksState.RED,) * green_after, ChecksState.GREEN)),
                      repair_policy=policy)
    t = train(Targets(ROOT), Batches({ROOT.key: (frozen, MEMBERS)}), {ROOT.key: current}, repairs)
    for _ in range(green_after):
        visit = await t.visit(ROOT)
        await complete_ordinary_repair(db, repairs, visit.repair["task_id"], candidate_ref(frozen.id))
    assert (await t.visit(ROOT)).state == "delivered"
    assert (await store.get(frozen.id)).repair_attempt_count == green_after
    assert await db.list_escalations(project_id="p") == []


async def test_exhausted_continue_keeps_filing_ordinary_debug_repairs(db):
    from src.integration.batches import BatchStore
    from src.integration.models import RepairPolicy
    from src.integration.repair import OrdinaryRepairService

    store, repairs = BatchStore(db), OrdinaryRepairService(db)
    frozen = await store.freeze(batch("batch-continue"), MEMBERS, trees={"t-1": PARTIAL})
    policy = RepairPolicy(primary_attempts=1, debug_attempts=1, debug_intelligence_class="deep-high",
                          on_exhausted="continue")
    current = replace(lane(Service(*("testing",) * 4), Checks(*(ChecksState.RED,) * 4)),
                      repair_policy=policy)
    t = train(Targets(ROOT), Batches({ROOT.key: (frozen, MEMBERS)}), {ROOT.key: current}, repairs)
    for attempt in range(1, 5):
        visit = await t.visit(ROOT)
        assert visit.repair["outcome"] == "filed" and visit.repair["attempt_count"] == attempt
        task = await db.get_task(visit.repair["task_id"])
        assert task.class_hint == (None if attempt == 1 else "deep-high")
        await complete_ordinary_repair(db, repairs, task.id, candidate_ref(frozen.id))
    assert (await store.get(frozen.id)).repair_attempt_count == 4
    assert await db.list_escalations(project_id="p") == []


@pytest.mark.parametrize("scope", ["member", "batch"])
async def test_conflict_repair_brief_honors_policy_scope(scope):
    from src.integration.models import RepairPolicy

    repair = Repair()
    current = replace(lane(Conflicting(CONFLICT, candidate=PARTIAL)), repair_policy=RepairPolicy(
        debug_intelligence_class="deep-high", conflict_scope=scope,
    ))
    t = train(Targets(ROOT), Batches({ROOT.key: (batch(), CONFLICTING_MEMBERS)}),
              {ROOT.key: current}, repair)
    await t.visit(ROOT)
    brief = repair.calls[0][1]["brief"]
    assert f"Conflict scope: {scope}." in brief
    assert ("conflicting member's changes" if scope == "member" else "whole frozen batch") in brief
    assert "Still to merge onto the starting head, in this order:" in brief
    assert "t-2" in brief and "t-3" in brief


@pytest.mark.parametrize("scope", ["member", "batch"])
def test_default_branch_sync_conflict_brief_honors_policy_scope(scope):
    brief = sync_default_branch_brief(
        {"ref": "refs/heads/main", "sha": TARGET_SHA, "checks": ["unit"],
         "conflicting_files": ["src/example.py"]},
        CONFLICTING_MEMBERS, starting_sha=PARTIAL, conflict_scope=scope,
    )
    assert f"Conflict scope: {scope}." in brief
    assert "src/example.py" in brief
    assert "t-1" in brief and "t-2" in brief and "t-3" in brief

@pytest.mark.parametrize("state", ["testing", "unknown"])
@pytest.mark.parametrize("lost", ["released", "expired"])
async def test_train_restores_ready_repair_without_waiting_for_red_checks(db, state, lost):
    from src.database.queries.claim_queries import _frontier_where
    from src.database.queries.hierarchy_queries import ProjectIntegrationMode
    from src.database.tables import tasks
    from src.integration.batches import BatchStore
    from src.integration.lock import BranchLock
    from src.integration.models import BranchKey
    from src.integration.repair import OrdinaryRepairService
    from src.models import TaskStatus

    store, now = BatchStore(db), [100.0]
    await store.freeze(batch("batch-retry"), MEMBERS, trees={"t-1": "d" * 40})
    locks = BranchLock(db, clock=lambda: now[0])
    repair = OrdinaryRepairService(db, locks=locks, clock=lambda: now[0])
    ref = candidate_ref("batch-retry")
    target = BranchKey(repository_id="r", branch=ref)
    filed = await repair.allocate("batch-retry", target_ref=ref, head_sha=CANDIDATE,
                                  authorize=AsyncMock(return_value=True), ttl_seconds=10)
    task_id = filed["task_id"]
    original = await repair.input(task_id)
    await db.update_task(task_id, status=TaskStatus.READY)
    old = (await locks.get(target)).grant()
    if lost == "released":
        await locks.release(old)
        async with db._engine.connect() as conn:
            assert await conn.scalar(select(tasks.c.id).where(
                tasks.c.id == task_id, _frontier_where("p", ProjectIntegrationMode(True, "r")),
            )) is None
    else:
        now[0] += 10
    frozen = await store.get("batch-retry")
    t = train(Targets(ROOT), Batches({ROOT.key: (frozen, MEMBERS)}),
              {ROOT.key: lane(Service(state), Checks(ChecksState.PENDING))}, repair)

    visit = await t.visit(ROOT)

    assert visit.state == state
    lease = await locks.get(target)
    assert (lease.holder, lease.fence) == (task_id, old.token + 1)
    assert await repair.input(task_id) == original
    assert (await store.get("batch-retry")).repair_attempt_count == 1
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(tasks.c.id).where(
            tasks.c.id == task_id, _frontier_where("p", ProjectIntegrationMode(True, "r")),
        )) == task_id
    # A repeated visit keeps the same task/fence rather than allocating again.
    t = train(Targets(ROOT), Batches({ROOT.key: (frozen, MEMBERS)}),
              {ROOT.key: lane(Service(state), Checks(ChecksState.PENDING))}, repair)
    await t.visit(ROOT)
    assert await locks.get(target) == lease


async def test_filed_conflict_repair_task_carries_the_plain_english_brief(db):
    from src.integration.batches import BatchStore
    from src.integration.repair import OrdinaryRepairService

    store = BatchStore(db)
    frozen = await store.freeze(batch("batch-brief"), CONFLICTING_MEMBERS,
                                trees={member.task_id: "d" * 40 for member in CONFLICTING_MEMBERS})
    service = OrdinaryRepairService(db)
    t = train(Targets(ROOT), Batches({ROOT.key: (frozen, CONFLICTING_MEMBERS)}),
              {ROOT.key: lane(Conflicting(CONFLICT, candidate=PARTIAL))}, service)
    visit = await t.visit(ROOT)
    assert (visit.state, visit.repair["outcome"]) == ("repair", "filed")
    # The raw merge record stays in the visit, where a worker never reads it.
    assert (visit.detail["member"], visit.detail["files"]) == ("t-2", ["migrations/versions/head.py"])
    task = await db.get_task(visit.repair["task_id"])
    brief, _, rest = task.description.partition("\n\nRepair the observed head on")
    assert brief.startswith(f"The batch merge conflicted while building the starting head {PARTIAL}.")
    assert "{'outcome': 'conflict'" not in task.description and "{'member':" not in task.description
    assert "Still to merge onto the starting head, in this order:" in brief
    assert (await service.input(task.id)) == {
        "batch_id": "batch-brief", "attempt": 1, "repository_id": "r",
        "target_ref": candidate_ref("batch-brief"), "starting_sha": PARTIAL,
    }
    assert f" {candidate_ref('batch-brief')}. Publish with the managed lease" in rest


@pytest.fixture
async def conflicting_batch_env(setup):
    """Real Git and the real batch service over two members that conflict."""
    from src.integration.batches import BatchService, BatchStore
    from src.integration.git_truth import GitTruth
    from src.models import Project, RepoConfig, RepoSourceType

    db, ops, _subject, _fence, repo, base, _head, _green = setup
    await db.create_project(Project(id="p", name="Project"))
    await db.create_repo(RepoConfig(id="r", project_id="p", source_type=RepoSourceType.LINK))
    left = commit(repo.store, {"new.txt": "left\n"}, base=base)
    right = commit(repo.store, {"new.txt": "right\n"}, base=base)
    store = BatchStore(db)
    frozen = Batch("batch-conflict", "p", "r", "refs/heads/main", created_at=1700000000)
    members = (BatchMember("task-a", left, base, 0), BatchMember("task-b", right, base, 1))
    await store.freeze(frozen, members, trees={
        member.task_id: git(repo.store, "rev-parse", f"{member.source_sha}^{{tree}}")
        for member in members})
    green = set()

    async def eligible(batch, members):
        return True

    async def gate(batch, sha, tree):
        return sha in green

    async def publish(repository, ref, *, expected_old_oid, new_oid, authorize):
        assert await authorize()
        await ops.push(repository, ref, new_oid, expected_old_oid)

    truth = GitTruth(ops.git)

    async def snapshot(batch=frozen):
        return await truth.snapshot(str(repo.store), project_id="p", repository_id="r",
                                    repository_url=str(ops.git.remote_path),
                                    target_ref=batch.target_ref)

    return SimpleNamespace(
        db=db, ops=ops, repo=repo, base=base, store=store, batch=frozen, members=members,
        green=green, snapshot=snapshot,
        service=BatchService(store, ops, publish=publish, eligible=eligible, gate=gate,
                             attest=AsyncMock(return_value="published")),
    )


async def test_green_repaired_head_missing_a_member_never_settles_the_batch(conflicting_batch_env):
    """A repaired head is deliverable only once it contains every frozen member."""
    from src.integration.repair import OrdinaryRepairService

    env = conflicting_batch_env
    batches = Batches({ROOT.key: (env.batch, env.members)})
    lane = TrainLane(snapshot=lambda: env.snapshot(), service=env.service,
                     checks=CandidateChecks.fixed(Checks(*([ChecksState.GREEN] * 4))))
    t = train(Targets(ROOT), batches, {ROOT.key: lane}, OrdinaryRepairService(env.db))

    first = await t.visit(ROOT)
    assert first.state == "repair" and first.detail["member"] == "task-b"
    partial = first.detail["head"]
    assert first.detail["files"] == ["new.txt"] and first.detail["outcome"] == "conflict"

    # The worker publishes the head it reached and its exact checks go green.
    ref = candidate_ref(env.batch.id)
    git(env.repo.store, "push", "origin", f"{partial}:{ref}")
    env.green.add(partial)
    second = await t.visit(ROOT)
    assert second.state == "repair" and second.repair["outcome"] == "exists"
    assert batches.settled == [] and await env.store.members(env.batch.id) == env.members
    assert git(env.ops.git.remote_path, "rev-parse", "main") == env.base

    # A target that already contains that head still may not settle a batch
    # whose member is still missing; the member is never dropped.
    git(env.ops.git.remote_path, "update-ref", "refs/heads/main", partial)
    third = await t.visit(ROOT)
    assert third.state == "repair" and third.repair["outcome"] == "exists"
    assert batches.settled == [] and await env.store.members(env.batch.id) == env.members
    async with env.db._engine.connect() as conn:
        assert (await conn.execute(select(integration_batches.c.lifecycle).where(
            integration_batches.c.id == env.batch.id))).scalar_one() == "sealed"
    assert git(env.ops.git.remote_path, "rev-parse", "main") == partial


async def completed_repair_before_target_move(env, *, unchanged=False, retained=True,
                                            moved_file="new.txt"):
    """One ordinary repair publishes both members; main advances outside the train."""
    from src.database.queries.task_queries import DEVELOPMENT_COMPLETION_ID_KEY
    from src.integration.models import BranchKey
    from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
    from src.integration.repair import OrdinaryRepairService
    from src.models import TaskStatus

    left, right = env.members
    repaired = git(env.repo.store, "commit-tree",
                   git(env.repo.store, "rev-parse", f"{left.source_sha}^{{tree}}"),
                   "-p", left.source_sha, "-p", right.source_sha, "-m", "Resolve both members")
    starting = repaired if unchanged else left.source_sha
    ref = candidate_ref(env.batch.id)
    git(env.repo.store, "push", "origin", f"{starting}:{ref}")
    repairs = OrdinaryRepairService(env.db)
    first = await repairs.allocate(
        env.batch.id, target_ref=ref, head_sha=starting,
        authorize=lambda: env.service.repair_authorized(env.batch, env.members, starting),
    )
    assert first["outcome"] == "filed"
    git(env.repo.store, "push", "origin", f"{repaired}:{ref}")
    moved = commit(env.repo.store, {moved_file: "upstream\n"}, base=env.base)
    if unchanged:
        # Move before the close: an unchanged worker cannot call this head repaired.
        git(env.repo.store, "push", "origin", f"{moved}:refs/heads/main")
    if retained:
        generation = "previous-repair-completion"
        provenance = GitProvenance(env.ops.git, str(env.repo.store),
                                   repository_url=str(env.ops.git.remote_path))
        await provenance.write_completion(CompletedSource(
            CompletionIdentity("p", "r", first["task_id"], generation), repaired,
        ))
        await env.db.set_task_meta(first["task_id"], DEVELOPMENT_COMPLETION_ID_KEY, generation)
    await env.db.update_task(first["task_id"], status=TaskStatus.COMPLETED)
    lease = await repairs.locks.get(BranchKey(repository_id="r", branch=ref))
    await repairs.locks.release(lease.grant())
    if not unchanged:
        git(env.repo.store, "push", "origin", f"{moved}:refs/heads/main")
    return repairs, first["task_id"], starting, repaired, moved


@pytest.mark.parametrize("moved_file,merged_count", [("new.txt", 0), ("upstream.txt", 1)])
async def test_conflict_after_target_move_publishes_and_briefs_the_real_partial_head(
    conflicting_batch_env, moved_file, merged_count,
):
    from src.integration.train_sources import LeasedPublish

    env = conflicting_batch_env
    repairs, previous_task, _, repaired, moved = await completed_repair_before_target_move(
        env, moved_file=moved_file,
    )
    env.service.publish = LeasedPublish(env.db, env.ops.git)
    t = train(Targets(ROOT), Batches({ROOT.key: (env.batch, env.members)}), {
        ROOT.key: TrainLane(snapshot=env.snapshot, service=env.service,
                            checks=CandidateChecks.fixed(Checks())),
    }, repairs)
    visit = await t.visit(ROOT)
    partial = visit.detail["head"]
    assert visit.state == "repair" and visit.repair["outcome"] == "filed"
    assert visit.repair["attempt_count"] == 2 and visit.repair["task_id"] != previous_task
    assert visit.candidate_sha == partial != repaired
    assert visit.detail["replaced_candidate_sha"] == repaired
    assert len(visit.detail["members"]) == merged_count
    assert await env.ops.is_ancestor(env.repo, moved, partial)
    assert await env.ops.remote(env.repo, candidate_ref(env.batch.id)) == partial
    assert await env.ops.remote(env.repo, "refs/heads/main") == moved
    task = await env.db.get_task(visit.repair["task_id"])
    assert f"starting head {partial}." in task.description
    assert f"starting head {repaired}" not in task.description
    if merged_count:
        assert "Already merged into the starting head" in task.description
        remaining = task.description.split("Still to merge onto the starting head")[1]
        assert "task-a" not in remaining and "task-b" in remaining
    else:
        assert partial == moved
        assert "Nothing is merged into the starting head yet." in task.description
        assert "  1. task-a" in task.description and "  2. task-b" in task.description
    assert (await repairs.input(task.id))["starting_sha"] == partial


@pytest.mark.parametrize("retained,reason", [
    (True, "repair_no_progress_missing_target"), (False, "repair_completion_unconfirmed"),
])
async def test_unchanged_repair_missing_target_blocks_without_spending_a_successor(
    conflicting_batch_env, retained, reason,
):
    from src.integration.models import BranchKey
    from src.integration.repair import OrdinaryRepairService

    env = conflicting_batch_env
    repairs, previous_task, starting, repaired, moved = await completed_repair_before_target_move(
        env, unchanged=True, retained=retained,
    )
    for _ in range(3):
        # A restart and the train resetting the candidate cannot erase the close's delta.
        t = train(Targets(ROOT), Batches({ROOT.key: (env.batch, env.members)}), {
            ROOT.key: TrainLane(snapshot=env.snapshot, service=env.service,
                                checks=CandidateChecks.fixed(Checks())),
        }, OrdinaryRepairService(env.db))
        visit = await t.visit(ROOT)
        assert visit.state == "blocked" and visit.repair["outcome"] == "blocked"
        assert visit.detail["reason"] == reason
        assert visit.repair["task_id"] == previous_task
        assert visit.repair["starting_sha"] == starting == repaired
        assert visit.repair["target_sha"] == moved
        if retained:
            assert visit.repair["completed_sha"] == repaired
        assert (await env.store.get(env.batch.id)).repair_attempt_count == 1
        assert await env.ops.remote(env.repo, candidate_ref(env.batch.id)) == moved
        assert await env.ops.remote(env.repo, "refs/heads/main") == moved
        lease = await repairs.locks.get(BranchKey(
            repository_id="r", branch=candidate_ref(env.batch.id),
        ))
        assert lease.holder is None


@pytest.mark.parametrize("refusal", ["busy", "transport", "moved"])
async def test_stale_candidate_reset_never_files_over_live_writer_or_failed_transport(
    conflicting_batch_env, refusal,
):
    from src.integration.models import BranchKey
    from src.integration.train_sources import LeasedPublish

    env = conflicting_batch_env
    repairs, _, _, repaired, moved = await completed_repair_before_target_move(env)
    env.service.publish = LeasedPublish(env.db, env.ops.git)
    ref = candidate_ref(env.batch.id)
    if refusal == "busy":
        await repairs.locks.acquire(BranchKey(repository_id="r", branch=ref), "live-worker")
    elif refusal == "transport":
        env.ops.git.fail_push = True
    else:
        # Race the exact expected-old push with another candidate publication.
        async def replace_ref():
            git(env.ops.git.remote_path, "update-ref", ref, env.base)

        env.ops.git.push_hook = replace_ref
    t = train(Targets(ROOT), Batches({ROOT.key: (env.batch, env.members)}), {
        ROOT.key: TrainLane(snapshot=env.snapshot, service=env.service,
                            checks=CandidateChecks.fixed(Checks())),
    }, repairs)
    visit = await t.visit(ROOT)
    assert visit.state == ("moved" if refusal == "moved" else "unknown")
    assert visit.repair is None
    assert visit.detail["repair_start_sha"] == moved
    assert (await env.store.get(env.batch.id)).repair_attempt_count == 1
    assert await env.ops.remote(env.repo, ref) == (env.base if refusal == "moved" else repaired)
    assert await env.ops.remote(env.repo, "refs/heads/main") == moved


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


async def test_status_reports_a_first_visit_in_flight_as_visiting(monkeypatch):
    """After a restart, a lane whose first visit is running is not 'unvisited'."""
    slow = TrainTarget("p", "r", "refs/heads/main")
    release = asyncio.Event()

    async def blocked_fetch():
        await release.wait()

    batches = Batches({slow.key: (batch(), MEMBERS)})
    t = train(Targets(slow), batches, {slow.key: lane(Service("delivered"), fetch=blocked_fetch)})
    now = [100.0]
    monkeypatch.setattr("src.integration.train.time", SimpleNamespace(monotonic=lambda: now[0]))
    await t.tick()
    for _ in range(5):
        await asyncio.sleep(0)
    now[0] += 7.5
    [row] = t.status()
    assert row["state"] == "visiting" and row["running"] and row["visits"] == 0
    assert row["progress"]["stage"]
    assert row["timing"]["stages_seconds"]["fetch_snapshot"] == 7.5
    assert row["timing"]["elapsed_seconds"] == 7.5
    release.set()
    await t.drain()
    [row] = t.status()
    assert row["state"] == "delivered" and not row["running"] and "progress" not in row
    now[0] += 10
    assert t.status()[0]["timing"] == row["timing"]
    assert sum(row["timing"]["stages_seconds"].values()) == 7.5


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


def _rate_limited(retry_at=None):
    """A visit failure as the daemon sees it: GitError over the classified cause."""
    cause = GitHubAccessError(
        "rate_limited", "GitHub request was rate limited (rate_limited, HTTP 403)",
        retry_at=retry_at, http_status=403,
    )
    try:
        raise GitError("GitHub request was rate limited") from cause
    except GitError as exc:
        return exc


async def test_rate_limited_visit_defers_the_target_with_backoff(caplog):
    """fresh-quest-25: a secondary-limit burst is one line and a pause, not a failure per tick."""
    clock, visits = [100.0], []

    async def limited():
        visits.append(clock[0])
        raise _rate_limited()

    async def lane_for(target):
        return lane(Service(), fetch=limited)

    t = IntegrationTrain(targets=Targets(ROOT), batches=Batches(), lane_for=lane_for,
                         repair=Repair(), clock=lambda: clock[0])
    label = "p/r/refs/heads/main"
    caplog.set_level(logging.INFO, logger="src.integration.train")

    assert (await t.tick())["started"] == [label]
    await t.drain()
    [row] = t.status()
    assert (row["state"], row["errors"]) == ("unknown", 1)
    assert row["detail"]["reason"] == "rate_limited" and row["detail"]["retry_at"] == 160.0

    clock[0] = 159.0
    assert await t.tick() == {"started": [], "running": [], "skipped": [], "deferred": [label]}
    assert visits == [100.0]

    clock[0] = 160.0
    assert (await t.tick())["started"] == [label]
    await t.drain()
    # A second consecutive limit doubles the pause.
    assert t.status()[0]["detail"]["retry_at"] == 160.0 + 120.0

    records = [r for r in caplog.records if r.name == "src.integration.train"]
    assert len(records) == 2
    assert all(r.exc_info is None and r.levelno == logging.WARNING for r in records)
    message = records[0].getMessage()
    assert "p/r/refs/heads/main" in message
    assert "code=rate_limited" in message and "http=403" in message
    assert "\n" not in message


class Holding(Service):
    """A promotion lane that names what each visit waits for."""

    def __init__(self, *reasons):
        super().__init__()
        self.reasons = list(reasons)

    async def visit(self, batch, members, snapshot):
        self.calls += 1
        return BatchObservation("held", SOURCE, TARGET_SHA,
                                detail={"reason": self.reasons.pop(0)})


PROMOTION = TrainTarget("p", "r", "refs/heads/main", "promotion",
                        step={"id": "release", "target": "main", "gate": {"approval": "operator"}})


async def test_named_promotion_hold_defers_its_target_until_woken():
    """grand-lantern-78.4: a request waiting on a person is not re-polled every tick."""
    clock = [100.0]
    approval, vetoed = "promotion_operator_approval_missing", "promotion_pr_changes_requested"
    service = Holding(approval, approval, vetoed, vetoed)

    async def lane_for(target):
        return lane(service)

    t = IntegrationTrain(targets=Targets(PROMOTION),
                         batches=Batches({PROMOTION.key: (batch(), MEMBERS)}),
                         lane_for=lane_for, repair=Repair(), clock=lambda: clock[0])
    label = "p/r/refs/heads/main"

    assert (await t.tick())["started"] == [label]
    await t.drain()
    [row] = t.status()
    assert (row["state"], row["detail"]["reason"]) == ("held", approval)
    assert row["detail"]["retry_at"] == row["deferred_until"] == 130.0

    clock[0] = 129.0
    assert await t.tick() == {"started": [], "running": [], "skipped": [], "deferred": [label]}
    assert service.calls == 1

    clock[0] = 130.0
    assert (await t.tick())["started"] == [label]
    await t.drain()
    # The same refusal again doubles the wait.
    assert t.status()[0]["detail"]["retry_at"] == 130.0 + 60.0

    clock[0] = 190.0
    assert (await t.tick())["started"] == [label]
    await t.drain()
    # A different refusal starts the wait over.
    assert t.status()[0]["detail"]["retry_at"] == 190.0 + 30.0

    # A promote command wakes only its own target, at once.
    assert t.wake("p", "r", "refs/heads/release") == 0
    assert t.wake("p", "r", "refs/heads/main") == 1
    assert (await t.tick())["started"] == [label]
    await t.drain()
    assert service.calls == 4


async def test_only_a_named_promotion_hold_defers_its_target():
    root = train(Targets(ROOT), Batches({ROOT.key: (batch(), MEMBERS)}),
                 {ROOT.key: lane(Holding("promotion_operator_approval_missing"))})
    unnamed = train(Targets(PROMOTION), Batches({PROMOTION.key: (batch(), MEMBERS)}),
                    {PROMOTION.key: lane(Service("held", "held"))})
    for t, target, visits in ((root, ROOT, 1), (unnamed, PROMOTION, 2)):
        for _ in range(visits):
            assert (await t.tick())["started"] == ["p/r/refs/heads/main"]
            await t.drain()
        [row] = t.status()
        assert row["state"] == "held" and "deferred_until" not in row
        assert "retry_at" not in (row["detail"] or {})
    assert root.wake("p") == 0


async def test_rate_limited_visit_honours_a_later_github_retry_time():
    clock = [100.0]

    async def limited():
        raise _rate_limited(retry_at=1000.0)

    async def lane_for(target):
        return lane(Service(), fetch=limited)

    t = IntegrationTrain(targets=Targets(ROOT), batches=Batches(), lane_for=lane_for,
                         repair=Repair(), clock=lambda: clock[0])
    await t.tick()
    await t.drain()
    assert t.status()[0]["detail"]["retry_at"] == 1000.0
    clock[0] = 999.0
    assert (await t.tick())["deferred"] == ["p/r/refs/heads/main"]


async def test_a_visit_after_a_rate_limit_resets_the_backoff():
    clock, outcomes = [100.0], [_rate_limited(), None, _rate_limited()]

    async def sometimes():
        failure = outcomes.pop(0)
        if failure is not None:
            raise failure

    async def lane_for(target):
        return lane(Service(), fetch=sometimes)

    t = IntegrationTrain(targets=Targets(ROOT), batches=Batches(), lane_for=lane_for,
                         repair=Repair(), clock=lambda: clock[0])
    for now in (100.0, 160.0, 170.0):
        clock[0] = now
        assert (await t.tick())["started"] == ["p/r/refs/heads/main"]
        await t.drain()
    assert t.status()[0]["detail"]["retry_at"] == 170.0 + 60.0


async def test_a_rate_limit_pauses_every_target_of_the_repository_behind_one_probe(caplog):
    """fresh-quest-25: 33 targets of one repository re-hit GitHub's limit together each round.

    The pause is the repository's: siblings limited in the same round count once,
    and when it ends one probe visit goes first while the others wait for it.
    """
    first, second = TrainTarget("p", "r", "refs/heads/a"), TrainTarget("p", "r", "refs/heads/b")
    other = TrainTarget("p", "s", "refs/heads/main")
    clock, limited, visits = [100.0], {"r": True}, []

    def fetch_for(target):
        async def fetch():
            visits.append((clock[0], target.target_ref, target.repository_id))
            if limited.get(target.repository_id):
                raise _rate_limited()
        return fetch

    async def lane_for(target):
        return lane(Service(), fetch=fetch_for(target))

    t = IntegrationTrain(targets=Targets(first, second, other), batches=Batches(),
                         lane_for=lane_for, repair=Repair(), clock=lambda: clock[0])
    a, b, s = ("/".join(x.key) for x in (first, second, other))
    caplog.set_level(logging.INFO, logger="src.integration.train")

    assert (await t.tick())["started"] == [a, b, s]
    await t.drain()
    # Two limited siblings in one round: one pause of the first step, one warning.
    assert {row["target_ref"]: (row["detail"] or {}).get("retry_at") for row in t.status()} == {
        "refs/heads/a": 160.0, "refs/heads/b": 160.0, "refs/heads/main": None,
    }
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    clock[0] = 120.0
    tick = await t.tick()
    assert (tick["started"], tick["deferred"]) == ([s], [a, b])
    await t.drain()

    # The pause ends: one probe, still limited, so the repository waits twice as long.
    clock[0] = 160.0
    tick = await t.tick()
    assert (tick["started"], tick["deferred"]) == ([a, s], [b])
    assert (await t.tick())["deferred"] == [b]
    await t.drain()
    assert t.status()[0]["detail"]["retry_at"] == 160.0 + 120.0
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2

    # The next probe succeeds and releases every target of the repository.
    limited["r"] = False
    clock[0] = 280.0
    # The sibling that has waited longest gets this probe.
    assert (await t.tick())["started"] == [b, s]
    assert (await t.tick())["deferred"] == [a]
    await t.drain()
    clock[0] = 281.0
    assert (await t.tick())["started"] == [a, b, s]
    await t.drain()
    assert [v for v in visits if v[2] == "r"] == [
        (100.0, "refs/heads/a", "r"), (100.0, "refs/heads/b", "r"),
        (160.0, "refs/heads/a", "r"), (280.0, "refs/heads/b", "r"),
        (281.0, "refs/heads/a", "r"), (281.0, "refs/heads/b", "r"),
    ]


async def test_a_cancelled_probe_does_not_hold_the_repository_paused():
    clock, limited = [100.0], [True]
    first, second = TrainTarget("p", "r", "refs/heads/a"), TrainTarget("p", "r", "refs/heads/b")

    async def fetch():
        if limited[0]:
            raise _rate_limited()
        await asyncio.sleep(3600)

    async def lane_for(target):
        return lane(Service(), fetch=fetch)

    t = IntegrationTrain(targets=Targets(first, second), batches=Batches(), lane_for=lane_for,
                         repair=Repair(), clock=lambda: clock[0])
    await t.tick()
    await t.drain()
    limited[0] = False
    clock[0] = 160.0
    assert (await t.tick())["started"] == ["p/r/refs/heads/a"]
    await t.stop()
    assert (await t.tick())["started"] == ["p/r/refs/heads/b"]
    await t.stop()


async def test_an_unexpected_visit_failure_keeps_its_traceback(caplog):
    class Exploding(Service):
        async def visit(self, *args):
            raise RuntimeError("git exploded")

    caplog.set_level(logging.INFO, logger="src.integration.train")
    t = train(Targets(ROOT), Batches({ROOT.key: (batch(), MEMBERS)}),
              {ROOT.key: lane(Exploding())})
    await t.tick()
    await t.drain()
    [record] = [r for r in caplog.records if r.name == "src.integration.train"]
    assert record.levelno == logging.ERROR and record.exc_info is not None
    assert (await t.tick())["started"] == ["p/r/refs/heads/main"]
    await t.drain()


async def test_visit_timeout_frees_the_target_for_the_next_tick():
    async def hang():
        await asyncio.sleep(3600)

    t = train(Targets(ROOT), Batches(), {ROOT.key: lane(Service(), fetch=hang)},
              visit_timeout_seconds=0.01)
    await t.tick()
    await t.drain()
    [row] = t.status()
    assert (row["state"], row["running"]) == ("unknown", False)
    assert row["detail"] == {
        "reason": "visit_timeout", "stage": "fetch_snapshot", "timeout_seconds": 0.01,
    }
    assert (await t.tick())["started"] == ["p/r/refs/heads/main"]
    await t.stop()


@pytest.mark.parametrize("bounded_leader", [True, False])
async def test_cancelled_shared_fetch_records_waiter_failure_and_retries(setup, bounded_leader):
    from src.integration.git_truth import GitTruth

    _db, ops, _subject, _fence, repo, base, head, _green = setup
    waiters = [TrainTarget("p", "r", f"refs/heads/waiter-{i}") for i in range(3)]
    for target in waiters:
        await ops.push(repo, target.target_ref, base, "")
    truth = GitTruth(ops.git, share_fetches=True)
    fetch = ops.git.afetch_origin
    entered, release = asyncio.Event(), asyncio.Event()
    arrivals = {target.key: asyncio.Event() for target in waiters}

    async def blocked_fetch(*args, **kwargs):
        entered.set()
        await release.wait()
        return await fetch(*args, **kwargs)

    ops.git.afetch_origin = AsyncMock(side_effect=blocked_fetch)

    async def lane_for(target):
        async def snapshot_for():
            if target.key in arrivals:
                arrivals[target.key].set()
            return await truth.snapshot(
                str(repo.store), project_id="p", repository_id="r",
                repository_url=str(ops.git.remote_path), target_ref=target.target_ref,
            )

        return TrainLane(snapshot=snapshot_for, service=Service(),
                         checks=CandidateChecks.fixed(Checks()))

    t = IntegrationTrain(targets=Targets(*waiters), batches=Batches(), lane_for=lane_for,
                         repair=Repair(), clock=lambda: 100.0)
    if bounded_leader:
        await t.tick(target=ROOT)
        leader = t._lanes[ROOT.key].task
    else:
        leader = asyncio.create_task(t.visit(ROOT, seal_now=True))
    requests = []
    try:
        await entered.wait()
        requests = [asyncio.create_task(t.request_visit(target)) for target in waiters]
        await asyncio.gather(*(arrival.wait() for arrival in arrivals.values()))
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader

        visits = await asyncio.gather(*requests)
        assert ops.git.afetch_origin.await_count == 1
        assert not truth._fetches
        for target, visit in zip(waiters, visits, strict=True):
            assert visit.state == "unknown"
            assert visit.detail["reason"] == "SharedFetchCancelled"
            lane_state = t._lanes[target.key]
            assert lane_state.last is visit
            assert (lane_state.visits, lane_state.errors) == (1, 1)
            assert not lane_state.task.cancelled()

        # A later visit must fetch the current remote rather than reuse the
        # failed observation or a tracking ref from before the cancellation.
        for target in waiters:
            await ops.push(repo, target.target_ref, head, base)
        release.set()
        assert len((await t.tick())["started"]) == len(waiters)
        await t.drain()
        assert ops.git.afetch_origin.await_count == 3
        assert not truth._fetches
        for target in waiters:
            lane_state = t._lanes[target.key]
            assert (lane_state.last.state, lane_state.last.target_sha) == ("idle", head)
            assert (lane_state.visits, lane_state.errors) == (2, 1)
    finally:
        leader.cancel()
        for request in requests:
            request.cancel()
        await t.stop()
        await asyncio.gather(leader, *requests, return_exceptions=True)


async def test_visit_timeout_status_keeps_the_last_known_state():
    lanes = {ROOT.key: lane(Service())}
    t = train(Targets(ROOT), Batches(), lanes, visit_timeout_seconds=0.5)
    await t.tick()
    await t.drain()
    [first] = t.status()
    known = first["state"]

    async def hang():
        await asyncio.sleep(3600)

    lanes[ROOT.key] = lane(Service(), fetch=hang)
    for _ in range(2):  # consecutive timeouts keep the chain
        assert (await t.tick())["started"] == ["p/r/refs/heads/main"]
        await t.drain()
        [row] = t.status()
        assert row["state"] == known
        assert row["detail"]["reason"] == "visit_timeout"
        assert row["detail"]["previous_state"] == known
    await t.stop()


async def test_requested_visit_shares_running_lane_and_respects_rate_limit_pause():
    gate, entered = asyncio.Event(), asyncio.Event()
    calls = []

    async def limited():
        calls.append(True)
        entered.set()
        await gate.wait()
        raise _rate_limited()

    t = train(Targets(ROOT), Batches(), {ROOT.key: lane(Service(), fetch=limited)})
    await t.tick()
    await entered.wait()
    requested = asyncio.create_task(t.request_visit(ROOT))
    await asyncio.sleep(0)
    assert calls == [True]
    gate.set()
    visit = await requested
    assert visit.detail["reason"] == "rate_limited"
    assert await t.request_visit(ROOT) is None
    assert calls == [True]


async def test_requested_visit_uses_timeout_and_never_returns_stale_tick_result():
    t = train(Targets(ROOT), Batches(), {ROOT.key: lane(Service())})
    assert (await t.request_visit(ROOT)).state == "idle"
    async with t._tick_lock:
        assert await t.request_visit(ROOT) is None

    async def hang():
        await asyncio.Event().wait()

    t = train(Targets(ROOT), Batches(), {ROOT.key: lane(Service(), fetch=hang)},
              visit_timeout_seconds=0.01)
    assert (await t.request_visit(ROOT)).detail["reason"] == "visit_timeout"


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


async def test_repository_budget_admits_new_work_and_keeps_inbox_reconciliation_running():
    old = [TrainTarget("p", "r", f"refs/heads/old-{i}") for i in range(55)]
    fresh = TrainTarget("p", "r", "refs/heads/fresh")
    other = TrainTarget("q", "s", "refs/heads/main")
    release = asyncio.Event()
    entered = asyncio.Event()
    active, peak, calls = 0, 0, []

    class SlowBatches(Batches):
        async def open_batch(self, target, snapshot, service):
            nonlocal active, peak
            if target in old:
                active += 1
                peak = max(peak, active)
                calls.append(target.key)
                if active == 4:
                    entered.set()
                try:
                    with selection_stage("root_delivery", items=1):
                        await release.wait()
                finally:
                    active -= 1
                return BatchSelection(blockers=({"code": "missing_provenance"},))
            return BatchSelection()

    targets = Targets(*old, other)
    lanes = {target.key: lane(Service()) for target in [*old, fresh, other]}
    t = train(targets, SlowBatches(), lanes)
    try:
        scheduled = await t.tick()
        assert len(scheduled["started"]) == 5  # four here, one in another repository
        assert len(scheduled["deferred"]) == 51
        await entered.wait()
        assert peak == 4 and len(calls) == 4
        # These are running visits, not 51 additional tasks queued on a semaphore.
        assert sum(lane.task is not None for lane in t._lanes.values()) == 5
        row = next(row for row in t.status() if row["target_ref"] == old[0].target_ref)
        assert row["timing"]["selection"]["stages"]["root_delivery"]["items"] == 1

        db = SimpleNamespace(
            get_pending_recipients=AsyncMock(return_value=[("user", "dashboard", "p")]),
            get_pending_messages=AsyncMock(return_value=[Message(
                "inbox-1", "p", "system", "benchmark", "user", "dashboard", "pending")]),
            mark_delivered=AsyncMock(return_value=True),
        )
        inbox = MessageDeliveryEngine(db, None, SimpleNamespace(max_inject_per_prompt=10))
        assert (await inbox.run_delivery_pass())["delivered"] == 1
        db.mark_delivered.assert_awaited_once_with("inbox-1", via="platform")
        # Requested visits use the same budget and never bypass occupied slots.
        assert await t.request_visit(fresh) is None
        targets.items.append(fresh)
        release.set()
        await t.drain()
        for _ in range(14):
            scheduled = await t.tick()
            assert all("old-0" not in label for label in scheduled["started"])
            await t.drain()
            if "/".join(fresh.key) in scheduled["started"]:
                break
        else:
            pytest.fail("new work did not get a turn within one bounded frontier sweep")
        assert peak == 4
    finally:
        release.set()
        await t.stop()


async def test_repository_fairness_rotates_idle_targets_and_releases_cancelled_slots():
    targets = [TrainTarget("p", "r", f"refs/heads/target-{i}") for i in range(3)]
    t = train(Targets(*targets), Batches(), {x.key: lane(Service()) for x in targets},
              repository_concurrency=1)
    for target in targets * 2:
        assert (await t.tick())["started"] == ["/".join(target.key)]
        await t.drain()
    await t.tick()
    await t.stop()
    assert len((await t.tick())["started"]) == 1
    await t.drain()


async def test_unchanged_blockers_back_off_with_bounded_retry_and_wake():
    from src.integration.train import TrainVisit

    clock = [100.0]
    t = train(Targets(ROOT), Batches(), {ROOT.key: lane(Service())})
    t.clock = lambda: clock[0]
    result = TrainVisit(ROOT, "blocked", target_sha=TARGET_SHA,
                        detail={"blockers": [{"code": "missing_provenance"}]})
    t.visit = AsyncMock(return_value=result)
    for delay in (10, 20, 40, 60, 60):
        assert (await t.tick())["started"]
        await t.drain()
        assert t.status()[0]["deferred_until"] == clock[0] + delay
        assert not (await t.tick())["started"]
        clock[0] += delay
    # A changed exact target resets the retry sequence; wake also resets it.
    t.visit.return_value = replace(result, target_sha="d" * 40)
    await t.tick()
    await t.drain()
    assert t.status()[0]["deferred_until"] == clock[0] + 10
    assert t.wake("p", "r", ROOT.target_ref) == 1
    assert (await t.tick())["started"]
    await t.drain()
    assert t.status()[0]["deferred_until"] == clock[0] + 10


async def test_selection_metrics_retain_interrupted_operations_without_cross_visit_leaks():
    async def measured_fetch():
        selection_count("completion_cache_misses")
        with selection_stage("provenance", items=7):
            await asyncio.Event().wait()

    t = train(Targets(ROOT), Batches(), {ROOT.key: lane(Service(), fetch=measured_fetch)},
              visit_timeout_seconds=0.01)
    await t.tick()
    await t.drain()
    metrics = t.status()[0]["timing"]["selection"]
    assert metrics["counts"] == {"completion_cache_misses": 1}
    assert metrics["stages"]["provenance"]["items"] == 7
    assert metrics["stages"]["provenance"]["seconds"] > 0
    await t.tick()
    await t.drain()
    assert t.status()[0]["timing"]["selection"]["counts"] == metrics["counts"]


def test_repository_concurrency_must_be_positive():
    with pytest.raises(ValueError, match="repository concurrency"):
        train(Targets(), Batches(), {}, repository_concurrency=0)


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


RETIRED_TRUTH = {
    "src.integration.reconciler", "src.integration.root_runtime", "src.integration.parent_runtime",
    "src.integration.source_delivery", "src.integration.settling", "src.integration.records",
    "src.integration.subjects", "src.integration.parent_engine", "src.integration.main_promotion",
    "src.integration.promotion", "src.integration.candidates", "src.integration.root_adapters",
    "src.integration.scheduler", "src.integration.stale_schedule",
}
RETIRED_TABLES = {
    "integration_subject_journal", "integration_subjects", "task_delivery_receipts",
    "integration_legacy_deliveries", "integration_outbox",
}
# Pure helpers and data types the train may share with modules it outlives: pin and
# retained-repository construction, and the identity types gitops and checks accept.
SHARED_NAMES = {
    "src.integration.development_runtime": {
        "development_repository", "project_development_pin", "load_pinned_development_policy",
    },
}


def test_active_train_and_promotion_modules_import_retained_contracts():
    """Retirement cannot remove a live train's DTO, error or ownership dependency."""
    import ast
    from pathlib import Path

    modules = (
        "train", "train_sources", "checks", "candidate_baseline", "promotion_steps",
        "epics", "github_review_poll", "attestation", "ci_producers", "ci_adapters",
        "runtime_contracts", "promotion_contracts", "owner_guards", "source_repairs",
    )
    paths = [Path(f"src/integration/{module}.py") for module in modules]
    paths.append(Path("src/git/github.py"))
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                assert not {alias.name for alias in node.names} & RETIRED_TRUTH, path
            elif isinstance(node, ast.ImportFrom):
                assert node.module not in RETIRED_TRUTH, (path, node.module)
                if node.module == "src.integration":
                    assert not {"src.integration." + alias.name for alias in node.names} & (
                        RETIRED_TRUTH
                    ), path


def test_train_reads_no_journal_receipt_or_runtime_truth():
    import ast
    from pathlib import Path

    for path in ("src/integration/train.py", "src/integration/train_sources.py"):
        for node in ast.walk(ast.parse(Path(path).read_text())):
            if isinstance(node, ast.Import):
                assert not {alias.name for alias in node.names} & RETIRED_TRUTH, path
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            names = {alias.name for alias in node.names}
            assert node.module not in RETIRED_TRUTH, (path, node.module)
            forbidden = names & RETIRED_TABLES
            if path == "src/integration/train_sources.py" and node.module == "src.database.tables":
                forbidden -= {"integration_legacy_deliveries"}
            assert not forbidden, (path, forbidden)
            if node.module in SHARED_NAMES:
                assert names <= SHARED_NAMES[node.module], (path, names)


def test_legacy_delivery_compatibility_is_read_only_and_confined_to_project_proof():
    """Historical operator attestations are the only legacy read the train needs.

    Scope and completion-generation fences are exercised by train_sources tests;
    no journal, receipt or runtime becomes a train lifecycle authority.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path("src/integration/train_sources.py").read_text())
    proof = next(node for node in tree.body
                 if isinstance(node, ast.AsyncFunctionDef) and node.name == "project_delivered")
    allowed = {id(node) for node in ast.walk(proof)}
    uses = [node for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id == "integration_legacy_deliveries"]
    assert uses and all(id(node) in allowed for node in uses)
    for node in ast.walk(proof):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"insert", "update", "delete"}


def test_activation_runbook_exists_before_canary():
    from pathlib import Path

    runbook = Path("docs/guides/git-first-train-runbook.md")
    text = runbook.read_text()
    for section in ("## Preconditions", "## Activate", "## Reading blockers", "## Roll back"):
        assert section in text
    assert "git_first: active" in text and "git_first: shadow" in text
    assert "aq restart --no-dashboard" in text
    assert runbook.name in Path("docs/guides/README.md").read_text()
    assert runbook.name in Path("docs/concepts/integration.md").read_text()


def test_active_train_shared_imports_work_with_retirement_modules_unavailable():
    """Fresh imports cannot hide an eager legacy dependency already loaded by pytest."""
    import subprocess
    import sys
    from pathlib import Path

    script = '''
import importlib
import importlib.abc
import sys
legacy = {
    "candidates", "main_promotion", "parent_engine", "parent_runtime", "promotion",
    "reconciler", "root_adapters", "root_runtime", "scheduler", "settling",
    "stale_schedule", "subjects", "source_delivery",
}
class RefuseLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("src.integration.") and fullname.split(".")[2] in legacy:
            raise AssertionError("Retiring runtime was imported: " + fullname)
sys.meta_path.insert(0, RefuseLegacy())
for module in (
    "train", "train_sources", "gates", "parent_subjects", "hierarchy",
    "development_adapter", "development_runtime",
):
    importlib.import_module("src.integration." + module)
'''
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
