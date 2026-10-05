"""The train's durable sources over a real origin; disposable PostgreSQL.

Targets come from project mode and completed work's routing, members are the
exact completion sources Git does not yet hold, and a whole visit freezes,
builds, gates and fast-forwards a target through the ref lease.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.tables import integration_batches, projects, task_branch_origins, tasks
from src.git.github_contracts import GitHubCredentialIdentity, GitHubRepositoryBinding
from src.git.manager import GitManager
from src.integration.batches import (
    Batch,
    BatchMember,
    BatchService,
    BatchStore,
    candidate_ref,
)
from src.integration.ci import IntegrationCITrust
from src.integration.git_truth import GitTruth
from src.integration.gitops import GitOperations, RetainedRepository, SubjectGitAuthority
from src.integration.lock import BranchLock
from src.integration.models import BranchKey
from src.integration.ownership import BranchBusy
from src.integration.repair import OrdinaryRepairService
from src.integration.status import IntegrationStatusService
from src.integration.train import CandidateChecks, IntegrationTrain, TrainLane, TrainTarget
from src.integration.train_sources import (
    DaemonLanes,
    DatabaseBatches,
    DatabaseTargets,
    LeasedPublish,
    _never_trusted,
    batch_id,
    train_for,
)
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus, Workspace
from tests.db_fixtures import lease_dsn
from tests.test_delivery_consumers import Origin, close, git
from tests.test_integration_gitops import LocalGit
from tests.test_jobs import finish, job_rows, jobs_handler, pin_development

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

    async def repository(batch):
        assert batch.repository_id == "r"
        return retained

    batches = DatabaseBatches(db)
    checks = GreenWhen()
    candidates = CandidateChecks.fixed(checks)
    service = BatchService(
        BatchStore(db), GitOperations(
            db, git=transport, repository=repository,
            authority=SubjectGitAuthority(db, trusted_green=_never_trusted),
        ),
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


async def test_active_status_projects_the_train_not_subjects(world):
    db, origin = world.db, world.origin
    await completed(world, "a")
    await completed(world, "b", needs=("a",))
    train, _, _ = lane(world, LocalGit(Path(origin.url)))
    await train.tick()
    await train.drain()
    status = IntegrationStatusService(db, git_first="active", train=train)

    project = await status.control_status("p")
    assert project["projection_kind"] == "train"
    assert "subjects" not in project and "journal" not in project
    [target] = project["targets"]
    assert (target["state"], target["checks"]) == ("testing", "pending")
    [batch] = project["batches"]
    assert batch["id"] == target["batch_id"]
    assert [m["task_id"] for m in batch["members"]] == ["a", "b"]
    assert [b["code"] for b in project["blockers"]] == ["checks_pending"]
    assert project["blockers"][0]["candidate_sha"] == target["candidate_sha"]
    assert await status.status("p") == project

    task = await status.task_blockers("b")
    assert task["projection_kind"] == "train"
    assert [b["code"] for b in task["blockers"]] == ["checks_pending"]

    await BatchStore(db).set_intent(batch["id"], "paused")
    paused = await status.control_status("p")
    assert [b["code"] for b in paused["blockers"]] == ["batch_paused"]

    # A restarted daemon has not visited yet; status still names the batch.
    fresh = await IntegrationStatusService(db, git_first="active").control_status("p")
    assert fresh["targets"] == []
    assert [b["code"] for b in fresh["blockers"]] == ["batch_paused"]
    await BatchStore(db).set_intent(batch["id"], "open")
    fresh = await IntegrationStatusService(db, git_first="active").control_status("p")
    assert [b["code"] for b in fresh["blockers"]] == ["awaiting_visit"]


async def test_missing_completion_provenance_reports_blocked_target_and_task(world):
    db, origin = world.db, world.origin
    heads = {}
    for tid in ("missing-a", "missing-b"):
        heads[tid] = await completed(world, tid, done=False)
        # A legacy close without retained Git evidence must never borrow its
        # published branch, even when its exact source is reported in the DB.
        await close(db, tid, [heads[tid]])
    await completed(world, "landed", land=True)
    train, _, _ = lane(world, LocalGit(Path(origin.url)))
    await train.tick()
    await train.drain()
    status = IntegrationStatusService(db, git_first="active", train=train)
    project = await status.control_status("p")
    assert project["batches"] == []
    assert project["targets"][0]["state"] == "blocked"
    assert {(b["code"], b["task_id"], b["target_ref"]) for b in project["blockers"]} == {
        ("missing_git_provenance", tid, MAIN.target_ref) for tid in heads
    }
    own = await status.task_blockers("missing-a")
    assert [(b["code"], b["ref"]) for b in own["blockers"]] == [
        ("missing_git_provenance", "missing-a"),
    ]
    assert (await status.task_blockers("landed"))["blockers"] == []

    # A later visit reads repaired evidence and replaces the blocked projection.
    from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance

    provenance = GitProvenance(GitManager(), str(origin.clone), repository_url=origin.url)
    for tid, head in heads.items():
        completion = await db.get_task_completion(tid)
        await provenance.write_completion(CompletedSource(
            CompletionIdentity("p", "r", tid, completion.id), head,
        ))
    await train.tick()
    await train.drain()
    project = await status.control_status("p")
    assert [b["code"] for b in project["blockers"]] == ["checks_pending"]
    assert {m["task_id"] for m in project["batches"][0]["members"]} == set(heads)


async def test_shadow_status_keeps_the_subject_projection(world):
    await completed(world, "a")
    project = await IntegrationStatusService(world.db).control_status("p")
    assert project.get("projection_kind") != "train"
    assert await IntegrationStatusService(world.db, git_first="active").control_status(
        "missing") is None


async def test_failed_delivery_probe_projects_safe_step_detail_in_active_status(world, monkeypatch):
    from unittest.mock import AsyncMock

    from src.git.manager import GitError

    await completed(world, "unknown")
    monkeypatch.setattr("src.integration.git_truth._whole_patch", AsyncMock(side_effect=GitError(
        "git command stdin exceeds the bounded input limit",
    )))
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)))
    await train.tick()
    await train.drain()
    status = IntegrationStatusService(world.db, git_first="active", train=train)
    project = await status.control_status("p")
    assert project["batches"] == []
    assert project["targets"][0]["state"] == "blocked"
    [blocker] = project["blockers"]
    assert blocker["code"] == "missing_or_ambiguous_source"
    assert blocker["detail"].endswith(
        "whole_source_patch: GitError: git command stdin exceeds the bounded input limit"
    )
    assert (await status.task_blockers("unknown"))["blockers"] == [blocker]


async def test_unknown_source_is_visible_while_an_independent_batch_waits_for_checks(world):
    await completed(world, "missing", done=False)
    await close(world.db, "missing", [])
    await completed(world, "healthy")
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)))
    status = IntegrationStatusService(world.db, git_first="active", train=train)
    for _ in range(2):
        # Keep reporting unadmitted sources when revisiting an existing batch.
        await train.tick()
        await train.drain()
        project = await status.control_status("p")
        assert {b["code"] for b in project["blockers"]} == {
            "checks_pending", "missing_git_provenance",
        }
        assert [m["task_id"] for m in project["batches"][0]["members"]] == ["healthy"]
        assert [b["code"] for b in (await status.task_blockers("missing"))["blockers"]] == [
            "missing_git_provenance",
        ]


class HostedGitHub:
    """Authenticated GitHub that has finished the required ``unit`` check on some SHAs.

    Rows are built for whichever SHA a request names, so the candidate the
    train builds inside a visit is observed exactly as GitHub would report it.
    """

    full_name = "acme/widgets"

    def __init__(self):
        self.credential_identity = GitHubCredentialIdentity.existing_login()
        self.runs: dict[str, str] = {}
        self.observed: list[str] = []

    def _rows(self, sha):
        n = list(self.runs).index(sha) + 1
        done = {"head_sha": sha, "status": "completed", "conclusion": self.runs[sha]}
        repo = {"id": 123, "full_name": self.full_name}
        return (
            {"id": 10 + n, "name": "unit", "app": {"id": 15368},
             "check_suite": {"id": 20 + n}, **done},
            {"id": 30 + n, "workflow_id": 301, "run_attempt": 1, "check_suite_id": 20 + n,
             "event": "push", "repository": repo, "head_repository": repo, **done},
            {"id": 50 + n, "name": "unit", "run_id": 30 + n, "run_attempt": 1,
             "check_run_url": f"https://api.github.com/repos/{self.full_name}/check-runs/"
                              f"{10 + n}", **done},
        )

    async def paged_items(self, path, *, key):
        if key == "jobs":
            run = int(re.search(r"/actions/runs/(\d+)/", path).group(1))
            return [self._rows(list(self.runs)[run - 31])[2]]
        sha = re.search(r"[0-9a-f]{40}", path).group(0)
        self.observed.append(sha)
        if sha not in self.runs:
            return []
        check, run, _ = self._rows(sha)
        return [check] if key == "check_runs" else [run]


async def hosted_train(world, *, retained_store=None):
    """The daemon's own lanes for a project with no development pin: hosted checks."""
    db, origin = world.db, world.origin
    async with db._engine.begin() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_policy={
                "root": {"required_checks": {"version": "v1", "names": ["unit"]}},
            },
        ))
    github, trusts = HostedGitHub(), []

    async def load_trust(state, *, boundary):
        required = state["policy_snapshot"][boundary]["required_checks"]
        trusts.append((boundary, state["candidate_sha"], required))
        return IntegrationCITrust(
            canonical_repository_id=state["canonical_repository_id"],
            repository_id=state["repository_numeric_id"],
            full_name=state["repository_full_name"], producer_id="15368",
            required_checks=required,
        ), github

    async def binding(repo_row):
        return GitHubRepositoryBinding(123, HostedGitHub.full_name)

    async def store(repo_row):
        return retained_store or origin.clone

    orchestrator = SimpleNamespace(
        db=db, git=LocalGit(Path(origin.url)), github_repository_binding_resolver=binding,
        development_integration=SimpleNamespace(store=store),
        integration_attestation_service=SimpleNamespace(_load_trust=load_trust),
    )
    batches = DatabaseBatches(db)
    train = IntegrationTrain(
        targets=DatabaseTargets(db), batches=batches,
        lane_for=DaemonLanes(orchestrator, batches=batches), repair=OrdinaryRepairService(db),
    )
    return train, github, trusts


async def test_ordinary_train_completion_with_large_history_in_fresh_retained_store(world, tmp_path):
    from src.integration.delivery_truth import DeliveryState, load_delivery_requests
    from src.integration.provenance import record_worker_completion

    db, origin = world.db, world.origin
    git(origin.clone, "checkout", "main")
    (origin.clone / "base.txt").write_text("historical train fixture\n" * 50_000)
    git(origin.clone, "commit", "-am", "large historical target change")
    git(origin.clone, "push", "origin", "main")
    base = git(origin.clone, "rev-parse", "HEAD")
    historical = git(origin.clone, "diff", f"{base}^", base, "--")
    assert len(historical.encode()) > GitManager._MAX_STDIN_BYTES
    # The daemon's retained store predates the worker's branch and provenance.
    retained = tmp_path / "retained"
    git(tmp_path, "clone", origin.url, str(retained))
    tid, branch = "ordinary-train", "aq/epic/ordinary-train"
    await db.create_task(Task(id=tid, project_id="p", repo_id="r", title=tid, description="",
                              branch_name=branch, status=TaskStatus.IN_PROGRESS, claim_epoch=1))
    git(origin.clone, "checkout", "-b", branch)
    with (origin.clone / "base.txt").open("a") as handle:
        handle.write("new source\n")
    git(origin.clone, "commit", "-am", "ordinary train source")
    git(origin.clone, "push", "-u", "origin", branch)
    head = git(origin.clone, "rev-parse", "HEAD")
    await db.create_workspace(Workspace(
        id="worker", project_id="p", workspace_path=str(origin.clone),
        source_type=RepoSourceType.LINK, locked_by_task_id=tid,
    ))
    async with db._engine.begin() as conn:
        await conn.execute(insert(task_branch_origins).values(
            id="ordinary-origin", task_id=tid, repository_id="r", branch_name=branch,
            base_sha=base, creation_generation=0, reserved=True, materialized=True,
            created_at=time.time(),
        ))
    # The fair-impact-55 path: the real completion writer verifies and retains
    # the exact pushed source before its immutable passing close is recorded.
    assert await record_worker_completion(
        db, GitManager(), await db.get_task(tid), await db.get_project("p"),
        str(origin.clone), "ordinary-close", commit=head,
    ) == head
    await close(db, tid, [head], close_id="ordinary-close")
    request = (await load_delivery_requests(
        db, [tid], repository_id="r", target_ref=MAIN.target_ref, reduced=True,
    ))[tid]
    truth = GitTruth(GitManager())

    async def observed():
        return await truth.snapshot(str(retained), project_id="p", repository_id="r",
                                    repository_url=origin.url, target_ref=MAIN.target_ref)

    proof = await (await observed()).is_delivered(request, source_base=base)
    assert proof.state == DeliveryState.PENDING and proof.error_detail is None
    train, github, _ = await hosted_train(world, retained_store=retained)
    await train.tick()
    await train.drain()
    [testing] = train.status()
    assert (testing["state"], testing["checks"]) == ("testing", "pending"), testing
    status = await IntegrationStatusService(db, git_first="active", train=train).control_status("p")
    assert [member["source_sha"] for member in status["batches"][0]["members"]] == [head]
    assert [blocker["code"] for blocker in status["blockers"]] == ["checks_pending"]
    github.runs[testing["candidate_sha"]] = "success"
    assert (await train.visit(MAIN)).state == "delivered"
    proof = await (await observed()).is_delivered(request, source_base=base)
    assert proof.state == DeliveryState.CONTAINED and proof.reason == "ancestor"


async def test_hosted_lane_publishes_only_the_exact_candidate_github_passed(world):
    origin = world.origin
    a = await completed(world, "a")
    train, github, trusts = await hosted_train(world)
    main = git(origin.url, "rev-parse", "refs/heads/main")

    testing = await train.visit(MAIN)
    assert (testing.state, testing.checks) == ("testing", "pending"), testing
    candidate = testing.candidate_sha
    # Pushing the candidate ref is what starts hosted CI; main has not moved.
    assert git(origin.url, "rev-parse", candidate_ref(testing.batch_id)) == candidate
    assert git(origin.url, "rev-parse", "refs/heads/main") == main
    assert candidate in github.observed
    assert trusts[0] == ("root", candidate, {"version": "v1", "names": ["unit"]})

    github.runs[main] = "success"  # A green target says nothing about the candidate.
    assert (await train.visit(MAIN)).state == "testing"
    assert git(origin.url, "rev-parse", "refs/heads/main") == main

    github.runs[candidate] = "success"
    delivered = await train.visit(MAIN)
    assert (delivered.state, delivered.checks) == ("delivered", "green"), delivered
    tip = git(origin.url, "rev-parse", "refs/heads/main")
    assert tip == candidate
    git(origin.url, "merge-base", "--is-ancestor", a, tip)


async def test_hosted_red_candidate_files_one_repair_and_never_publishes(world):
    origin = world.origin
    await completed(world, "a")
    train, github, _ = await hosted_train(world)
    main = git(origin.url, "rev-parse", "refs/heads/main")

    testing = await train.visit(MAIN)
    github.runs[testing.candidate_sha] = "failure"
    red = await train.visit(MAIN)
    again = await train.visit(MAIN)
    assert (red.state, red.checks) == ("repair", "red"), red
    assert (red.repair["outcome"], again.repair["outcome"]) == ("filed", "exists")
    assert again.repair["task_id"] == red.repair["task_id"]
    assert git(origin.url, "rev-parse", "refs/heads/main") == main


async def development_train(world, tmp_path, monkeypatch, validation: str):
    """The daemon's own lanes for a development-pinned project: retained local jobs."""
    db, origin = world.db, world.origin
    _, config, load = await pin_development(
        db, tmp_path, validation, hierarchical_integration_mode="development",
        hierarchical_integration_desired_mode="development",
    )
    retained = RetainedRepository(repository_id="r", store=origin.clone, binding=None,
                                  default_branch="main")

    async def development_repository(primitives, repo_row, binding, settings):
        return retained

    async def binding(repo_row):
        return None

    monkeypatch.setattr("src.integration.development_runtime.development_repository",
                        development_repository)
    orchestrator = SimpleNamespace(
        db=db, git=LocalGit(Path(origin.url)), _command_handler=jobs_handler(db, tmp_path),
        config=config, _load_playbook_artifact=load,
        github_repository_binding_resolver=binding, development_integration=SimpleNamespace(),
    )
    batches = DatabaseBatches(db)
    train = IntegrationTrain(
        targets=DatabaseTargets(db), batches=batches,
        lane_for=DaemonLanes(orchestrator, batches=batches), repair=OrdinaryRepairService(db),
    )
    [target] = await DatabaseTargets(db).targets(time.time())
    assert target.kind == "development"
    return train, target


async def test_development_lane_publishes_only_after_its_exact_candidate_job_passes(
    world, tmp_path, monkeypatch,
):
    origin = world.origin
    a = await completed(world, "a")
    train, target = await development_train(world, tmp_path, monkeypatch, "focused")
    main = git(origin.url, "rev-parse", "refs/heads/main")

    testing = await train.visit(target)
    assert (testing.state, testing.checks) == ("testing", "pending"), testing
    [job] = await job_rows(world.db)
    assert (job["input_ref"], job["owner_kind"]) == (testing.candidate_sha, "integration")
    assert (await train.visit(target)).state == "testing"
    assert len(await job_rows(world.db)) == 1  # One job per candidate, not per visit.
    assert git(origin.url, "rev-parse", "refs/heads/main") == main

    await finish(world.db, job, exit_code=0)
    delivered = await train.visit(target)
    assert (delivered.state, delivered.checks) == ("delivered", "green"), delivered
    tip = git(origin.url, "rev-parse", "refs/heads/main")
    assert tip == testing.candidate_sha
    git(origin.url, "merge-base", "--is-ancestor", a, tip)


@pytest.mark.parametrize(("validation", "published"), [("focused", False), ("advisory", True)])
async def test_development_red_job_repairs_focused_and_publishes_advisory(
    world, tmp_path, monkeypatch, validation, published,
):
    origin = world.origin
    await completed(world, "a")
    train, target = await development_train(world, tmp_path, monkeypatch, validation)
    main = git(origin.url, "rev-parse", "refs/heads/main")

    testing = await train.visit(target)
    [job] = await job_rows(world.db)
    await finish(world.db, job, exit_code=1)
    after = await train.visit(target)
    tip = git(origin.url, "rev-parse", "refs/heads/main")
    if published:
        assert after.state == "delivered" and tip == testing.candidate_sha, after
    else:
        assert (after.state, after.checks, after.repair["outcome"]) == ("repair", "red", "filed")
        assert tip == main


async def test_train_status_still_publishes_the_configuration_generation(world):
    """``edit_project`` reads this as its compare-and-set token.

    The train projection replaces the subject rows, not ordinary project
    configuration: without the generation an operator cannot reconfigure a
    project's integration mode while the protocol is active.
    """
    await completed(world, "a")
    active = await IntegrationStatusService(world.db, git_first="active").control_status("p")
    shadow = await IntegrationStatusService(world.db).control_status("p")
    assert active["projection_kind"] == "train"
    assert active["generation"] == shadow["generation"]
    assert active["effective_mode"] == shadow["effective_mode"]
    assert active["desired_mode"] == shadow["desired_mode"]
