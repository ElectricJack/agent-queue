"""The train's durable sources over a real origin; disposable PostgreSQL.

Targets come from project mode and completed work's routing, members are the
exact completion sources Git does not yet hold, and a whole visit freezes,
builds, gates and fast-forwards a target through the ref lease.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.tables import integration_batches, projects, task_branch_origins, tasks
from src.git.github_contracts import GitHubRepositoryBinding
from src.git.manager import GitManager
from src.integration.batches import Batch, BatchMember, BatchService, BatchStore
from src.integration.git_truth import GitTruth
from src.integration.gitops import GitOperations, RetainedRepository
from src.integration.lock import BranchLock
from src.integration.models import BranchKey
from src.integration.ownership import BranchBusy
from src.integration.repair import OrdinaryRepairService
from src.integration.train import CandidateChecks, IntegrationTrain, TrainLane, TrainTarget
from src.integration.train_sources import (
    DatabaseBatches,
    DatabaseTargets,
    LeasedPublish,
    batch_id,
    train_for,
)
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus
from tests.db_fixtures import lease_dsn
from tests.test_delivery_consumers import Origin, close, git
from tests.test_integration_gitops import LocalGit

MAIN = TrainTarget("p", "r", "refs/heads/main", "root")


@pytest.fixture
async def world(tmp_path):
    origin = Origin(tmp_path)
    db = Database(lease_dsn("train-sources"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Train"))
    await db.create_repo(
        RepoConfig(id="r", project_id="p", source_type=RepoSourceType.CLONE, url=origin.url)
    )
    async with db._engine.begin() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            integration_repository_id="r", hierarchical_integration_mode="train",
            hierarchical_integration_desired_mode="train",
        ))
    yield SimpleNamespace(db=db, origin=origin, truth=GitTruth(GitManager()))
    await db.close()


async def completed(world, tid, *, parent=None, needs=(), land=False, done=True) -> str:
    """A task with a branch origin and, when *done*, a retained completion."""
    db, origin = world.db, world.origin
    await db.create_task(Task(
        id=tid, project_id="p", repo_id="r", title=tid, description="",
        branch_name=f"aq/{tid}", status=TaskStatus.IN_PROGRESS, parent_task_id=parent,
    ))
    for need in needs:
        await db.add_dependency(tid, need)
    head = origin.work(tid)
    async with db._engine.begin() as conn:
        await conn.execute(insert(task_branch_origins).values(
            id=f"{tid}-origin", task_id=tid, repository_id="r", branch_name=f"aq/{tid}",
            parent_task_id=parent, parent_repository_id="r" if parent else None,
            parent_ref=f"aq/{parent}" if parent else None,
            base_sha=git(origin.clone, "rev-parse", f"{head}^"),
            creation_generation=0, reserved=True, materialized=True, created_at=time.time(),
        ))
    if done:
        await close(db, tid, [head], origin=origin)
    if land:
        origin.land(tid)
    return head


async def snapshot(world, target=MAIN):
    return await world.truth.snapshot(
        str(world.origin.clone), project_id="p", repository_id="r",
        repository_url=world.origin.url, target_ref=target.target_ref,
    )


def tree(world, sha):
    return git(world.origin.clone, "rev-parse", f"{sha}^{{tree}}")


async def test_targets_follow_mode_routing_and_open_batches(world):
    db, origin = world.db, world.origin
    await db.create_task(Task(id="epic", project_id="p", repo_id="r", title="epic",
                              description="", branch_name="aq/epic",
                              status=TaskStatus.IN_PROGRESS))
    git(origin.clone, "push", "-q", "origin", "main:aq/epic")
    child = await completed(world, "child", parent="epic")
    await completed(world, "top")
    await db.create_project(Project(id="q", name="Disabled"))
    await db.create_repo(RepoConfig(id="rq", project_id="q", source_type=RepoSourceType.CLONE,
                                    url=origin.url))
    async with db._engine.begin() as conn:
        await conn.execute(update(projects).where(projects.c.id == "q")
                           .values(integration_repository_id="rq"))
    gone = Batch("held", "p", "r", "refs/heads/aq/gone", created_at=1.0)
    await BatchStore(db).freeze(gone, (BatchMember("child", child, child),),
                                trees={"child": tree(world, child)})

    targets = await DatabaseTargets(db).targets(time.time())
    assert [(t.target_ref, t.kind) for t in targets] == [
        ("refs/heads/aq/epic", "epic"), ("refs/heads/aq/gone", "epic"),
        ("refs/heads/main", "root"),
    ]

    async with db._engine.begin() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p")
                           .values(hierarchical_integration_mode="development"))
    assert ("refs/heads/main", "development") in {
        (t.target_ref, t.kind) for t in await DatabaseTargets(db).targets(time.time())
    }
    async with db._engine.begin() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(status="PAUSED"))
    assert await DatabaseTargets(db).targets(time.time()) == []


async def test_pending_members_are_exact_undelivered_sources(world):
    db = world.db
    await completed(world, "landed", land=True)
    a = await completed(world, "a")
    b = await completed(world, "b", needs=("landed",))
    withheld = await completed(world, "withheld")
    await completed(world, "after", needs=("withheld",))
    await completed(world, "open", done=False)
    store = BatchStore(db)
    aborted = Batch("old", "p", "r", "refs/heads/main", created_at=1.0)
    await store.freeze(aborted, (BatchMember("withheld", withheld, withheld),),
                       trees={"withheld": tree(world, withheld)})
    await store.set_intent("old", "aborted")

    members, requests, dependencies = await DatabaseBatches(db).pending(
        MAIN, await snapshot(world)
    )
    base = world.origin.clone
    assert {m.task_id: (m.source_sha, m.source_base_sha) for m in members} == {
        "a": (a, git(base, "rev-parse", f"{a}^")),
        "b": (b, git(base, "rev-parse", f"{b}^")),
    }
    # A delivered dependency never holds its dependent back; a withheld one does.
    assert dependencies == {"a": set(), "b": set()}
    assert set(requests) == {"a", "b"}
    assert batch_id(MAIN, members) == batch_id(MAIN, tuple(reversed(members)))


async def test_eligible_tracks_project_and_task_state(world):
    db = world.db
    await completed(world, "a")
    store = BatchStore(db)
    batches = DatabaseBatches(db)
    members, _, _ = await batches.pending(MAIN, await snapshot(world))
    batch = Batch(batch_id(MAIN, members), "p", "r", MAIN.target_ref, created_at=1.0)
    await store.freeze(batch, members, trees={"a": tree(world, members[0].source_sha)})
    assert await batches.eligible(batch, members)

    async with db._engine.begin() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(status="PAUSED"))
    assert not await batches.eligible(batch, members)
    async with db._engine.begin() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(status="ACTIVE"))
        await conn.execute(update(tasks).where(tasks.c.id == "a").values(status="IN_PROGRESS"))
    assert not await batches.eligible(batch, members)


class GreenWhen:
    """Exact-head checks that turn green for named SHAs."""

    def __init__(self):
        self.green: set[str] = set()

    def _result(self, head):
        state = "green" if head.sha in self.green else "pending"
        return SimpleNamespace(state=state, green=head.sha in self.green)

    async def request(self, head):
        return None

    async def refresh(self, head):
        return self._result(head)

    async def read(self, head):
        return self._result(head)


def lane(world, transport):
    db, origin = world.db, world.origin
    retained = RetainedRepository("r", origin.clone, GitHubRepositoryBinding(123, "test/repo"),
                                  "main")

    async def repository(repository_id):
        assert repository_id == "r"
        return retained

    batches = DatabaseBatches(db)
    checks = GreenWhen()
    candidates = CandidateChecks.fixed(checks)
    service = BatchService(
        BatchStore(db), GitOperations(db, git=transport, repository=repository),
        publish=LeasedPublish(db, transport), eligible=batches.eligible, gate=candidates.gate,
    )

    async def lane_snapshot():
        return await GitTruth(transport).snapshot(
            str(origin.clone), project_id="p", repository_id="r",
            repository_url=origin.url, target_ref=MAIN.target_ref,
        )

    async def lane_for(target):
        assert target == MAIN
        return TrainLane(snapshot=lane_snapshot, service=service, checks=candidates)

    train = IntegrationTrain(targets=DatabaseTargets(db), batches=batches, lane_for=lane_for,
                             repair=OrdinaryRepairService(db))
    return train, checks, retained


async def test_visit_freezes_gates_and_fast_forwards_through_the_lease(world):
    db, origin = world.db, world.origin
    a = await completed(world, "a")
    b = await completed(world, "b", needs=("a",))
    transport = LocalGit(Path(origin.url))
    train, checks, _ = lane(world, transport)
    main = git(origin.url, "rev-parse", "refs/heads/main")

    testing = await train.visit(MAIN)
    assert testing.state == "testing", testing
    assert git(origin.url, "rev-parse", "refs/heads/main") == main

    checks.green.add(testing.candidate_sha)
    delivered = await train.visit(MAIN)
    assert delivered.state == "delivered", delivered
    tip = git(origin.url, "rev-parse", "refs/heads/main")
    assert tip == testing.candidate_sha
    for source in (a, b):
        git(origin.url, "merge-base", "--is-ancestor", source, tip)
    async with db._engine.connect() as conn:
        row = (await conn.execute(select(integration_batches.c.lifecycle,
                                         integration_batches.c.final_main_sha)
                                  .where(integration_batches.c.id == testing.batch_id))).one()
    assert tuple(row) == ("promoted", tip)
    lease = await BranchLock(db).get(BranchKey(repository_id="r", branch="refs/heads/main"))
    assert lease is None or lease.holder is None
    assert (await train.visit(MAIN)).state == "idle"


async def test_leased_publish_never_pushes_a_ref_someone_else_holds(world):
    db, origin = world.db, world.origin
    head = await completed(world, "a")
    transport = LocalGit(Path(origin.url))
    _, _, retained = lane(world, transport)
    await BranchLock(db).acquire(BranchKey(repository_id="r", branch="refs/heads/main"),
                                 "repair-worker", role="worker")

    async def authorize():
        return True

    with pytest.raises(BranchBusy):
        await LeasedPublish(db, transport)(
            retained, "refs/heads/main", authorize=authorize, new_oid=head,
            expected_old_oid=git(origin.url, "rev-parse", "refs/heads/main"),
        )
    assert transport.pushes == 0


async def test_train_runs_only_when_git_first_is_active(world):
    def orchestrator(mode):
        return SimpleNamespace(db=world.db, git=GitManager(),
                               config=SimpleNamespace(integration=SimpleNamespace(git_first=mode)))

    assert train_for(orchestrator("shadow")) is None
    assert isinstance(train_for(orchestrator("active")), IntegrationTrain)
