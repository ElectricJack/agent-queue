"""The train's durable sources over a real origin; disposable PostgreSQL.

Targets come from project mode and completed work's routing, members are the
exact completion sources Git does not yet hold, and a whole visit freezes,
builds, gates and fast-forwards a target through the ref lease.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.tables import (
    events,
    integration_batches,
    integration_legacy_deliveries,
    projects,
    task_branch_origins,
    tasks,
)
from src.git.github_contracts import GitHubCredentialIdentity, GitHubRepositoryBinding
from src.git.manager import GitManager
from src.integration.attestation import IntegrationAttestationService
from src.integration.batches import (
    Batch,
    BatchMember,
    BatchService,
    BatchStore,
    candidate_ref,
)
from src.integration.candidate_baseline import BASELINE_BLOCKER, CandidateBaselineService
from src.integration.ci import ATTESTATION_CHECK_NAME, IntegrationTrustManifest
from src.integration.delivery_observer import DeliveryObserver
from src.integration.git_truth import GitTruth
from src.integration.gitops import GitOperations, RetainedRepository, SubjectGitAuthority
from src.integration.lock import BranchLock
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchBusy
from src.integration.regeneration import DEFAULT_REGENERATE_COMMAND
from src.integration.repair import OrdinaryRepairService
from src.integration.reviews import ReviewRequirements, TreeReviews
from src.integration.status import IntegrationStatusService
from src.integration.train import CandidateChecks, IntegrationTrain, TrainLane, TrainTarget
from src.integration.train_controls import TrainControls
from src.integration.train_sources import (
    DaemonLanes,
    DatabaseBatches,
    DatabaseTargets,
    LeasedPublish,
    _never_trusted,
    _pending_tasks,
    _push_branch_allowed,
    batch_id,
    train_for,
)
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus, Workspace
from src.test_selection import catalogue as Catalogue
from tests.db_fixtures import lease_dsn
from tests.test_delivery_consumers import Origin, close, git
from tests.test_integration_gitops import LocalGit, commit
from tests.test_jobs import finish, job_rows, jobs_handler, pin_development

MAIN = TrainTarget("p", "r", "refs/heads/main", "root")
ROOT = Path(__file__).resolve().parents[1]


async def green_pr(target, member):
    """Other train-mechanism tests supply an already-satisfied PR observer."""


class SealedBatches(DatabaseBatches):
    """Mechanism tests seal explicitly; cadence tests use DatabaseBatches itself."""

    async def open_batch(self, target, snapshot, service, *, seal_now=True):
        return await super().open_batch(target, snapshot, service, seal_now=seal_now)


def fixture_batches(db, **kwargs):
    return SealedBatches(db, pr_gate=green_pr, **kwargs)


def catalogue_repository(origin: Origin) -> None:
    """The origin repository as this project ships it: a real generated catalogue.

    The regenerator is the project's own command under its own name, so the
    train lane runs the same one a worker would.
    """
    clone = origin.clone
    for name in ("catalogue.py", "discovery.py"):
        target = clone / "src" / "test_selection" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "src" / "test_selection" / name, target)
    for package in (clone / "src", clone / "src" / "test_selection"):
        (package / "__init__.py").touch()
    scripts = clone / "scripts"
    scripts.mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "scripts" / "generate-selection-catalogue.py",
                    scripts / "generate-selection-catalogue.py")
    regenerator = scripts / "regenerate-generated.sh"
    regenerator.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        "python3 scripts/generate-selection-catalogue.py\n"
    )
    regenerator.chmod(0o755)
    (clone / ".gitattributes").write_text(
        f"{Catalogue.CATALOGUE_PATH} merge=aq-generated linguist-generated\n"
    )
    (clone / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (clone / "tests").mkdir(exist_ok=True)
    (clone / Catalogue.AREAS_PATH).write_text(
        "version: 1\nareas:\n  - id: area\n    description: Tests.\n"
        '    match: ["tests/test_*.py"]\n'
    )
    (clone / "tests" / "test_base.py").write_text("def test_base():\n    pass\n")
    subprocess.run([sys.executable, str(scripts / "generate-selection-catalogue.py")],
                   cwd=clone, capture_output=True, text=True, check=True)
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "selection inputs")
    git(clone, "push", "-q", "origin", "main")


def catalogue_branch(origin: Origin, tid: str, module: str) -> str:
    """One task branch that adds a test module and regenerates the catalogue."""
    clone = origin.clone
    git(clone, "fetch", "-q", "origin")
    git(clone, "checkout", "-q", "-B", f"aq/{tid}", "origin/main")
    (clone / "tests" / f"test_{module}.py").write_text(f"def test_{module}():\n    pass\n")
    subprocess.run(
        [sys.executable, str(clone / "scripts" / "generate-selection-catalogue.py")],
        cwd=clone, capture_output=True, text=True, check=True,
    )
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", f"{tid} adds {module}")
    git(clone, "push", "-q", "origin", f"aq/{tid}")
    return git(clone, "rev-parse", "HEAD")


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
    truth = GitTruth(GitManager())
    db.set_delivery_observer(DeliveryObserver(db, git=truth.git, data_dir=tmp_path, truth=truth))
    yield SimpleNamespace(db=db, origin=origin, truth=truth)
    await db.close()


async def completed(world, tid, *, parent=None, needs=(), land=False, done=True,
                    head=None) -> str:
    """A task with a branch origin and, when *done*, a retained completion."""
    db, origin = world.db, world.origin
    await db.create_task(Task(
        id=tid, project_id="p", repo_id="r", title=tid, description="",
        branch_name=f"aq/{tid}", status=TaskStatus.IN_PROGRESS, parent_task_id=parent,
    ))
    for need in needs:
        await db.add_dependency(tid, need)
    if head is None:
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
        if parent is None:
            url = f"https://github.com/acme/widgets/pull/{sum(tid.encode()) + 100}"
            await db.update_task(tid, pr_url=url)
            if hasattr(world, "github"):
                world.github.pulls[url] = f"aq/{tid}"
                world.github.pr_runs[head] = "success"
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

    members, requests, dependencies = await fixture_batches(db).pending(
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


@pytest.mark.parametrize("mode", ["development", "train", "hierarchy"])
async def test_root_delivered_legacy_epic_children_create_no_epic_target_or_batch(world, mode):
    db, origin = world.db, world.origin
    await db.create_task(Task(id="epic", project_id="p", repo_id="r", title="epic",
                              description="", branch_name="aq/epic", status=TaskStatus.COMPLETED))
    git(origin.clone, "push", "origin", "main:aq/epic")
    for tid in ("child-a", "child-b"):
        await completed(world, tid, parent="epic", land=True)
    # The project root is deliberately a development ref different from main.
    git(origin.clone, "push", "origin", "main:development")
    async with db._engine.begin() as conn:
        from src.database.tables import repos

        await conn.execute(update(repos).where(repos.c.id == "r")
                           .values(default_branch="development"))
        await conn.execute(update(projects).where(projects.c.id == "p")
                           .values(hierarchical_integration_mode=mode))
    [root] = await DatabaseTargets(db).targets(time.time())
    assert root.target_ref == "refs/heads/development"
    epic = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    # Even a direct stale epic visit has no inputs to freeze or repair.
    selection = await fixture_batches(db).open_batch(epic, await snapshot(world, epic),
                                                     SimpleNamespace(store=BatchStore(db)))
    assert selection.batch is None and not selection.blockers
    async with db._engine.connect() as conn:
        assert not (await conn.execute(select(integration_batches))).first()


async def legacy_delivery(world, tid, source, *, repository_id="r", target_ref="main"):
    async with world.db._engine.begin() as conn:
        await conn.execute(insert(integration_legacy_deliveries).values(
            task_id=tid, project_id="p", parent_task_id="epic", repository_id=repository_id,
            target_ref=target_ref, target_sha=source, delivered_sha=source,
            proof="development_delivery", operator_id="operator", reason="legacy delivery",
            created_at=time.time(),
        ))


async def test_scoped_legacy_delivery_attestation_suppresses_missing_provenance(world):
    db, origin = world.db, world.origin
    await db.create_task(Task(id="epic", project_id="p", title="epic", description="",
                              branch_name="aq/epic", status=TaskStatus.COMPLETED))
    git(origin.clone, "push", "origin", "main:aq/epic")
    source = await completed(world, "child", parent="epic", done=False)
    await close(db, "child", [source])  # no retained provenance
    await legacy_delivery(world, "child", source)
    assert [t.target_ref for t in await DatabaseTargets(db).targets(time.time())] == [MAIN.target_ref]
    epic = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    assert await fixture_batches(db).pending(epic, await snapshot(world, epic)) is None
    # A new close never inherits the old delivery attestation.
    await close(db, "child", [source], close_id="reopened-generation", origin=origin)
    assert "refs/heads/aq/epic" in {
        t.target_ref for t in await DatabaseTargets(db).targets(time.time())
    }


@pytest.mark.parametrize("repository_id,target_ref", [("other", "main"), ("r", "elsewhere")])
async def test_legacy_delivery_for_another_repository_or_ref_does_not_suppress_work(
    world, repository_id, target_ref,
):
    source = await completed(world, "a")
    await legacy_delivery(world, "a", source, repository_id=repository_id, target_ref=target_ref)
    members, _, _ = await fixture_batches(world.db).pending(MAIN, await snapshot(world))
    assert [m.task_id for m in members] == ["a"]


async def test_existing_epic_batch_with_root_delivered_inputs_is_held(world):
    db, origin = world.db, world.origin
    await db.create_task(Task(id="epic", project_id="p", title="epic", description="",
                              branch_name="aq/epic", status=TaskStatus.COMPLETED))
    git(origin.clone, "push", "origin", "main:aq/epic")
    source = await completed(world, "child", parent="epic")
    base = git(origin.clone, "rev-parse", f"{source}^")
    epic = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    store = BatchStore(db)
    await store.freeze(Batch("wrong", "p", "r", epic.target_ref),
                       (BatchMember("child", source, base),), trees={"child": tree(world, source)})
    origin.land("child")
    selection = await fixture_batches(db).open_batch(
        epic, await snapshot(world, epic), SimpleNamespace(store=store),
    )
    assert selection.batch is None
    assert selection.blockers[0]["code"] == "batch_inputs_delivered_to_project"


async def test_delivered_work_does_not_consume_the_pending_member_limit(world):
    db, origin = world.db, world.origin
    await db.create_task(Task(id="epic", project_id="p", title="epic", description="",
                              branch_name="aq/epic", status=TaskStatus.COMPLETED))
    git(origin.clone, "push", "origin", "main:aq/epic")
    await completed(world, "owed", parent="epic")
    await completed(world, "newer-delivered", land=True)
    targets = await DatabaseTargets(db, limit=1).targets(time.time())
    [epic] = [t for t in targets if t.kind == "epic"]
    members, _, _ = await fixture_batches(db, limit=1).pending(epic, await snapshot(world, epic))
    assert [m.task_id for m in members] == ["owed"]


async def test_visit_timeout_cause_is_visible_in_status_without_a_batch(world):
    import asyncio

    train, _, _ = lane(world, LocalGit(Path(world.origin.url)))
    original = train.lane_for

    async def blocked_fetch():
        await asyncio.Event().wait()

    async def lane_for(target):
        current = await original(target)
        return TrainLane(snapshot=blocked_fetch, service=current.service, checks=current.checks)

    train.lane_for = lane_for
    train.visit_timeout_seconds = 0.01
    await train.tick()
    await train.drain()
    status = await IntegrationStatusService(
        world.db, git_first="active", train=train,
    ).control_status("p")
    assert not status["batches"]
    [blocker] = status["blockers"]
    assert blocker["code"] == "visit_timeout"
    assert blocker["evidence"]["stage"] == "fetch_snapshot"
    assert blocker["evidence"]["timeout_seconds"] == 0.01


async def test_unavailable_root_probe_keeps_epic_work_owed(world):
    import asyncio

    db, origin = world.db, world.origin
    await db.create_task(Task(id="epic", project_id="p", title="epic", description="",
                              branch_name="aq/epic", status=TaskStatus.COMPLETED))
    git(origin.clone, "push", "origin", "main:aq/epic")
    await completed(world, "child", parent="epic", land=True)

    async def unavailable(db, target):
        await asyncio.Event().wait()

    targets = await DatabaseTargets(
        db, snapshot=unavailable, probe_timeout_seconds=0.01,
    ).targets(time.time())
    assert {t.target_ref for t in targets} == {MAIN.target_ref, "refs/heads/aq/epic"}
    # The visit has its own fetched root evidence and never batches the child.
    [epic] = [t for t in targets if t.kind == "epic"]
    assert await fixture_batches(db).pending(epic, await snapshot(world, epic)) is None


@pytest.mark.parametrize("kind", ["service", "playbook", "session"])
async def test_train_control_handlers_refuse_non_supervisor_principals(world, kind):
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import DENY_ALL

    source = await completed(world, "a", land=True)
    store = BatchStore(world.db)
    await store.freeze(Batch("batch", "p", "r", MAIN.target_ref),
                       (BatchMember("a", source, source),), trees={"a": tree(world, source)})
    handler = IntegrationCommandsMixin()
    handler.db = world.db
    with principal_context(ExecutionPrincipal(kind=PrincipalKind(kind), policy=DENY_ALL,
                                              elevated=True, project_id="p")):
        abort = await handler._cmd_integration_abort_batch({"batch_id": "batch"})
        retire = await handler._cmd_integration_retire_origin({"task_id": "a"})
    assert abort["outcome"] == retire["outcome"] == "unauthorized"
    assert (await store.get("batch")).intent == "open"


async def test_abort_batch_preview_apply(world):
    db = world.db
    source = await completed(world, "a")
    store = BatchStore(db)
    batch = Batch("abort-me", "p", "r", MAIN.target_ref)
    await store.freeze(batch, (BatchMember("a", source, source),), trees={"a": tree(world, source)})
    controls = TrainControls(db)
    preview = await controls.abort_batch(batch.id, dry_run=True, operator_id="operator", reason="")
    assert preview["outcome"] == "preview" and (await store.get(batch.id)).intent == "open"
    assert (await db.get_integration_batch(batch.id))["lifecycle"] == "sealed"
    applied = await controls.abort_batch(batch.id, dry_run=False, operator_id="operator",
                                          reason="duplicate work")
    assert applied["outcome"] == "aborted" and (await store.get(batch.id)).intent == "aborted"
    aborted = await db.get_integration_batch(batch.id)
    assert aborted["lifecycle"] == "aborted"
    assert aborted["human_abort_reason"] == "duplicate work"
    assert aborted["cleanup_state"] == "pending"
    async with db._engine.connect() as conn:
        [audit] = (await conn.execute(select(events.c.payload).where(
            events.c.event_type == "integration.batch_intent"))).scalars().all()
    assert "duplicate work" in audit
    assert await fixture_batches(db).pending(MAIN, await snapshot(world)) is None


async def test_abort_first_member_conflict_at_target_releases_member(world):
    from src.commands.task_commands import TaskCommandsMixin
    from src.database.queries.hierarchy_queries import HierarchyError

    db, origin = world.db, world.origin
    base = git(origin.clone, "rev-parse", "origin/main")
    git(origin.clone, "checkout", "-q", "-b", "aq/rework", base)
    (origin.clone / "base.txt").write_text("source change\n")
    git(origin.clone, "commit", "-qam", "source change")
    source = git(origin.clone, "rev-parse", "HEAD")
    git(origin.clone, "push", "-q", "origin", "aq/rework")
    await completed(world, "rework", head=source)
    git(origin.clone, "checkout", "-q", "main")
    (origin.clone / "base.txt").write_text("target change\n")
    git(origin.clone, "commit", "-qam", "target change")
    git(origin.clone, "push", "-q", "origin", "main")
    target = git(origin.clone, "rev-parse", "HEAD")

    store = BatchStore(db)
    batch = Batch("first-conflict", "p", "r", MAIN.target_ref)
    members = (BatchMember("rework", source, base),)
    await store.freeze(batch, members, trees={"rework": tree(world, source)})
    train, _, _ = lane(world, LocalGit(Path(origin.url)))
    train_lane = await train.lane_for(MAIN)
    result = await train_lane.service.visit(batch, members, await snapshot(world))
    assert result.state == "conflict"
    assert result.candidate_sha == result.target_sha == target
    assert git(origin.url, "rev-parse", candidate_ref(batch.id)) == target

    handler = TaskCommandsMixin()
    handler.db, handler._current_scope = db, {}
    with pytest.raises(HierarchyError, match="sealed subtree"):
        await handler._cmd_reopen_with_feedback({"task_id": "rework", "feedback": "resolve conflict"})
    before = await db.get_integration_batch(batch.id)
    async with db._engine.connect() as conn:
        audit_before = (await conn.execute(select(events.c.id).where(
            events.c.event_type == "integration.batch_intent"))).scalars().all()
    controls = TrainControls(db)
    preview = await controls.abort_batch(batch.id, dry_run=True, operator_id="operator", reason="")
    assert preview["outcome"] == "preview"
    assert preview["candidate_sha"] == preview["target_sha"] == target
    assert await db.get_integration_batch(batch.id) == before
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(events.c.id).where(
            events.c.event_type == "integration.batch_intent"))).scalars().all() == audit_before
    applied = await controls.abort_batch(batch.id, dry_run=False, operator_id="operator",
                                        reason="resolve conflict")
    assert applied["outcome"] == "aborted"
    aborted = await db.get_integration_batch(batch.id)
    assert (aborted["intent"], aborted["lifecycle"]) == ("aborted", "aborted")
    assert (await handler._cmd_reopen_with_feedback({
        "task_id": "rework", "feedback": "resolve conflict",
    }))["status"] == "READY"


@pytest.mark.parametrize("proof", ["ancestry", "squash", "lifecycle"])
@pytest.mark.parametrize("candidate_present", [False, True])
async def test_abort_refuses_promoted_batch_with_or_without_candidate(world, proof, candidate_present):
    db, origin = world.db, world.origin
    source = await completed(world, "a")
    base = git(origin.clone, "rev-parse", f"{source}^")
    store = BatchStore(db)
    batch = Batch("promoted", "p", "r", MAIN.target_ref)
    await store.freeze(batch, (BatchMember("a", source, base),), trees={"a": tree(world, source)})
    if proof == "ancestry":
        origin.land("a")
    elif proof == "squash":
        git(origin.clone, "checkout", "-q", "main")
        git(origin.clone, "merge", "--squash", "aq/a")
        git(origin.clone, "commit", "-qm", "squashed source")
        git(origin.clone, "push", "-q", "origin", "main")
        assert git(origin.clone, "rev-parse", "main") != source
    else:
        async with db._engine.begin() as conn:
            await conn.execute(update(integration_batches).where(integration_batches.c.id == batch.id)
                               .values(lifecycle="promoted"))
    if candidate_present:
        git(origin.clone, "push", "-q", "origin", f"main:{candidate_ref(batch.id)}")
    before = await db.get_integration_batch(batch.id)
    controls = TrainControls(db)
    for dry_run in (True, False):
        with pytest.raises(ValueError, match="promoted batch"):
            await controls.abort_batch(batch.id, dry_run=dry_run, operator_id="operator", reason="no")
        assert await db.get_integration_batch(batch.id) == before


@pytest.mark.parametrize("lifecycle", ["sealed", "testing"])
@pytest.mark.parametrize("old_abort", [False, True])
async def test_aborted_batch_visit_releases_reopen_but_other_batch_stays_sealed(
    world, lifecycle, old_abort,
):
    from src.commands.task_commands import TaskCommandsMixin
    from src.database.queries.hierarchy_queries import HierarchyError

    db = world.db
    source = await completed(world, "rework")
    other_source = await completed(world, "other")
    store = BatchStore(db)
    for bid, tid, head in (("abort", "rework", source), ("active", "other", other_source)):
        await store.freeze(Batch(bid, "p", "r", MAIN.target_ref),
            (BatchMember(tid, head, git(world.origin.clone, "rev-parse", f"{head}^")),),
            trees={tid: tree(world, head)})
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(integration_batches.c.id == "abort")
            .values(lifecycle=lifecycle))
    handler = TaskCommandsMixin()
    handler.db, handler._current_scope = db, {}
    with pytest.raises(HierarchyError, match="sealed subtree"):
        await handler._cmd_reopen_with_feedback({"task_id": "rework", "feedback": "fix regression"})
    if old_abort:
        # The old daemon persisted intent but left the compatibility seal active.
        async with db.immediate() as conn:
            await conn.execute(update(integration_batches).where(integration_batches.c.id == "abort")
                .values(intent="aborted", human_abort_reason="fix regression"))
    else:
        await TrainControls(db).abort_batch("abort", dry_run=False,
            operator_id="operator", reason="fix regression")
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)))
    await train.visit(MAIN)
    assert (await db.get_integration_batch("abort"))["lifecycle"] == "aborted"
    assert (await db.get_integration_batch("abort"))["human_abort_reason"] == "fix regression"
    with pytest.raises(HierarchyError, match="sealed subtree"):
        await handler._cmd_reopen_with_feedback({"task_id": "other", "feedback": "still active"})
    assert (await handler._cmd_reopen_with_feedback({
        "task_id": "rework", "feedback": "fix regression",
    }))["status"] == "READY"


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_aborted_candidate_cleanup_defers_to_repair_lease_and_preserves_sources(
    world, tmp_path, ambiguous,
):
    from src.integration.cleanup import IntegrationCleanupService

    db, origin = world.db, world.origin
    source = await completed(world, "rework")
    store = BatchStore(db)
    batch = Batch("abort", "p", "r", MAIN.target_ref)
    await store.freeze(batch, (BatchMember("rework", source,
        git(origin.clone, "rev-parse", f"{source}^")),),
        trees={"rework": tree(world, source)})
    ref = candidate_ref(batch.id)
    git(origin.clone, "push", "origin", f"{source}:{ref}")
    await TrainControls(db).abort_batch(batch.id, dry_run=False,
        operator_id="operator", reason="fix regression")
    transport = LocalGit(Path(origin.url))
    transport.ambiguous = ambiguous
    cleanup = IntegrationCleanupService(db, data_dir=tmp_path, git_manager=transport,
        binding_resolver=AsyncMock(return_value=GitHubRepositoryBinding(123, "test/repo")),
        candidate_store=AsyncMock(return_value=origin.clone))
    locks = BranchLock(db)
    writer = await locks.acquire(BranchKey(repository_id="r", branch=ref), "repair",
        ttl_seconds=120, role="repair")
    await cleanup.reconcile_aborted(time.time())
    assert transport.deletes == 0
    assert (await db.get_integration_batch(batch.id))["cleanup_state"] == "pending"
    assert (await locks.get(writer.target)).holder == "repair"
    await locks.release(writer)
    await cleanup.reconcile_aborted(time.time())
    assert transport.deletes == 1
    assert not git(Path(origin.url), "for-each-ref", "--format=%(refname)", ref)
    assert git(Path(origin.url), "rev-parse", "refs/heads/aq/rework") == source
    assert (await db.get_integration_batch(batch.id))["cleanup_state"] == "complete"
    assert await fixture_batches(db).pending(MAIN, await snapshot(world)) is None
    await cleanup.reconcile_aborted(time.time())
    assert transport.deletes == 1


async def test_abort_cleanup_recovers_old_seal_and_only_releases_its_detached_reservation(
    world, tmp_path,
):
    from src.database.tables import integration_branch_owners
    from src.integration.cleanup import IntegrationCleanupService

    db, origin = world.db, world.origin
    source = await completed(world, "rework")
    batch = Batch("abort", "p", "r", MAIN.target_ref)
    await BatchStore(db).freeze(batch, (BatchMember("rework", source, source),),
        trees={"rework": tree(world, source)})
    ref = candidate_ref(batch.id)
    git(origin.clone, "push", "origin", f"{source}:{ref}")
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(integration_batches.c.id == batch.id)
            .values(intent="aborted", lifecycle="testing", human_abort_reason="fix regression"))
        for owner_id, branch, state in ((batch.id, ref, "reserved"),
                ("other-batch", candidate_ref("other-batch"), "reserved"),
                (batch.id, "refs/heads/attached", "attached")):
            await conn.execute(insert(integration_branch_owners).values(
                id=branch, repository_id="r", ref=branch, owner_id=owner_id,
                owner_role="collector", fence_token=1, handoff_state=state,
                workspace_id="writer" if state == "attached" else None,
                expires_at=time.time() + 120, created_at=1, updated_at=1,
            ))
    transport = LocalGit(Path(origin.url))
    cleanup = IntegrationCleanupService(db, data_dir=tmp_path, git_manager=transport,
        binding_resolver=AsyncMock(return_value=GitHubRepositoryBinding(123, "test/repo")),
        candidate_store=AsyncMock(return_value=origin.clone))
    await cleanup.reconcile_aborted(time.time())
    assert transport.deletes == 1
    aborted = await db.get_integration_batch(batch.id)
    assert (aborted["lifecycle"], aborted["cleanup_state"], aborted["human_abort_reason"]) == (
        "aborted", "complete", "fix regression",
    )
    async with db._engine.connect() as conn:
        states = dict((await conn.execute(select(
            integration_branch_owners.c.ref, integration_branch_owners.c.handoff_state,
        ))).all())
    assert states["refs/heads/attached"] == "attached"
    assert states[candidate_ref("other-batch")] == "reserved"


async def test_abort_cannot_release_a_member_also_in_another_active_batch(world):
    from src.database.queries.hierarchy_queries import HierarchyError

    db = world.db
    source = await completed(world, "rework")
    store = BatchStore(db)
    for bid in ("abort", "active"):
        await store.freeze(Batch(bid, "p", "r", MAIN.target_ref),
            (BatchMember("rework", source, source),), trees={"rework": tree(world, source)})
    await store.set_intent("abort", "aborted", reason="fix regression")
    with pytest.raises(HierarchyError, match="sealed subtree"):
        await db.transition_task("rework", TaskStatus.READY, context="reopen_with_feedback")
    await store.set_intent("active", "aborted", reason="fix regression")
    await db.transition_task("rework", TaskStatus.READY, context="reopen_with_feedback")


async def test_retire_origin_preview_apply_removes_pending_and_requires_delivery(world):
    db = world.db
    await completed(world, "a", land=True)
    await completed(world, "pending")
    controls = TrainControls(db)
    with pytest.raises(ValueError, match="not proven delivered"):
        await controls.retire_origin("pending", dry_run=True, origin_id=None,
                                     operator_id="operator", reason="")
    preview = await controls.retire_origin("a", dry_run=True, origin_id=None,
                                           operator_id="operator", reason="")
    async with db._engine.connect() as conn:
        assert "a" in await _pending_tasks(conn, "p", "r", limit=200)
    with pytest.raises(ValueError, match="origin changed"):
        await controls.retire_origin("a", dry_run=False, origin_id="wrong",
                                     operator_id="operator", reason="delivered")
    assert (await controls.retire_origin("a", dry_run=False, origin_id=preview["origin_id"],
                                         operator_id="operator", reason="delivered"))["outcome"] == "retired"
    async with db._engine.connect() as conn:
        assert await _pending_tasks(conn, "p", "r", limit=200) == ["pending"]
        [audit] = (await conn.execute(select(events.c.payload).where(
            events.c.event_type == "integration.origin_retired"))).scalars().all()
    assert "delivered" in audit


@pytest.mark.parametrize("unknown_ancestry", [True, False])
@pytest.mark.parametrize("candidate_present", [False, True])
async def test_abort_requires_observed_promotion_and_unchanged_refs(
    world, monkeypatch, unknown_ancestry, candidate_present,
):
    from unittest.mock import AsyncMock

    source = await completed(world, "a")
    batch = Batch("uncertain", "p", "r", MAIN.target_ref)
    store = BatchStore(world.db)
    await store.freeze(batch, (BatchMember("a", source, source),), trees={"a": tree(world, source)})
    if candidate_present:
        git(world.origin.clone, "push", "origin", f"{source}:{candidate_ref(batch.id)}")
    if unknown_ancestry:
        monkeypatch.setattr(GitManager, "ais_ancestor", AsyncMock(return_value=None))
        cause = "promotion cannot be observed"
    else:
        monkeypatch.setattr("src.integration.git_truth.GitTruthSnapshot.is_fresh",
                            AsyncMock(return_value=False))
        cause = "target or candidate changed"
    with pytest.raises(ValueError, match=cause):
        await TrainControls(world.db).abort_batch(batch.id, dry_run=False,
                                                  operator_id="operator", reason="duplicate")
    assert (await store.get(batch.id)).intent == "open"


async def test_retire_origin_requires_aborting_its_open_batches(world):
    source = await completed(world, "a", land=True)
    pending = await completed(world, "pending")
    store = BatchStore(world.db)
    batch = Batch("duplicate", "p", "r", MAIN.target_ref)
    await store.freeze(batch, (
        BatchMember("a", source, git(world.origin.clone, "rev-parse", f"{source}^")),
        BatchMember("pending", pending, git(world.origin.clone, "rev-parse", f"{pending}^"), order=1),
    ), trees={"a": tree(world, source), "pending": tree(world, pending)})
    # A repair start already contained by the target still lacks one member.
    git(world.origin.clone, "push", "-q", "origin", f"main:{candidate_ref(batch.id)}")
    controls = TrainControls(world.db)
    with pytest.raises(ValueError, match="abort the task's open batches"):
        await controls.retire_origin("a", dry_run=False, origin_id="a-origin",
                                     operator_id="operator", reason="delivered")
    await controls.abort_batch(batch.id, dry_run=False, operator_id="operator", reason="duplicate")
    retired = await controls.retire_origin("a", dry_run=False, origin_id="a-origin",
                                           operator_id="operator", reason="delivered")
    assert retired["outcome"] == "retired"


async def test_eligible_tracks_project_and_task_state(world):
    db = world.db
    await completed(world, "a")
    store = BatchStore(db)
    batches = fixture_batches(db)
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


def lane(world, transport, *, regenerate=DEFAULT_REGENERATE_COMMAND, clock=time.time,
         settling=False):
    """A train lane over the origin clone. *regenerate* may be a callable, read
    per visit, so a test can change the repository's configuration."""
    db, origin = world.db, world.origin
    binding = GitHubRepositoryBinding(123, "test/repo")

    def retained(command=None):
        return RetainedRepository("r", origin.clone, binding, "main", regenerate=command)

    command = regenerate if callable(regenerate) else lambda: regenerate

    async def repository(batch):
        assert batch.repository_id == "r"
        return retained(command())

    batches = (DatabaseBatches(db, pr_gate=green_pr, clock=clock) if settling
               else fixture_batches(db, clock=clock))
    checks = GreenWhen()
    candidates = CandidateChecks.fixed(checks)
    service = BatchService(
        BatchStore(db), GitOperations(
            db, git=transport, repository=repository,
            authority=SubjectGitAuthority(db, trusted_green=_never_trusted),
        ),
        publish=LeasedPublish(db, transport), eligible=batches.eligible, gate=candidates.gate,
        attest=AsyncMock(return_value="published"),
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
                             repair=OrdinaryRepairService(db), clock=clock)
    return train, checks, retained(command())


async def test_root_cadence_uses_latest_admission_and_survives_restart(world):
    now = [1000.0]
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)), clock=lambda: now[0],
                       settling=True)
    await completed(world, "a")
    first = await train.visit(MAIN)
    assert first.state == "settling" and first.batch_id is None
    assert first.detail["seal_at"] == 1300
    now[0] = 1010
    await completed(world, "b")
    second = await train.visit(MAIN)
    assert second.detail["first_admission_at"] == 1000
    assert second.detail["latest_admission_at"] == 1010
    assert second.detail["seal_at"] == 1310
    # A fresh source and train instance must recover the original timing hints.
    train, checks, _ = lane(world, LocalGit(Path(world.origin.url)), clock=lambda: now[0],
                            settling=True)
    for moment in (1300, 1309.99):
        now[0] = moment
        waiting = await train.visit(MAIN)
        assert waiting.state == "settling" and waiting.detail["seal_at"] == 1310
        assert await train.batches.current(MAIN) is None
    now[0] = 1310
    sealed = await train.visit(MAIN)
    assert sealed.state == "testing", sealed
    assert {m.task_id for m in await BatchStore(world.db).members(sealed.batch_id)} == {"a", "b"}
    checks.green.add(sealed.candidate_sha)
    assert (await train.visit(MAIN)).state == "delivered"
    # A subsequent generation of work gets its own quiet period.
    now[0] = 1400
    await completed(world, "c")
    assert (await train.visit(MAIN)).detail["seal_at"] == 1700


async def test_root_settling_cap_bounds_continuous_admissions_across_restart(world):
    now = [1000.0]
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)), clock=lambda: now[0],
                       settling=True)
    for index in range(9):
        now[0] = 1000 + index * 200
        await completed(world, f"root-{index}")
        waiting = await train.visit(MAIN)
        assert waiting.state == "settling" and waiting.batch_id is None
        assert waiting.detail["seal_at"] == min(now[0] + 300, 2800)
        if index == 4:
            train, _, _ = lane(world, LocalGit(Path(world.origin.url)), clock=lambda: now[0],
                               settling=True)
    now[0] = 2799.99
    assert (await train.visit(MAIN)).state == "settling"
    now[0] = 2800
    sealed = await train.visit(MAIN)
    assert sealed.state == "testing", sealed
    async with world.db._engine.connect() as conn:
        assert len((await conn.execute(select(integration_batches.c.id))).all()) == 1
    assert len(await BatchStore(world.db).members(sealed.batch_id)) == 9


@pytest.mark.parametrize("cadence,cap,due", [(40, 1800, 1050), (300, 90, 1090)])
async def test_root_cadence_and_cap_follow_project_policy(world, cadence, cap, due):
    now = [1000.0]
    await world.db.update_project("p", hierarchical_integration_policy={
        "train": {"cadence_seconds": cadence, "settling_cap_seconds": cap},
    })
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)), clock=lambda: now[0],
                       settling=True)
    await completed(world, "a")
    assert (await train.visit(MAIN)).state == "settling"
    now[0] = 1010
    await completed(world, "b")
    assert (await train.visit(MAIN)).detail["seal_at"] == due
    now[0] = due - 0.01
    assert (await train.visit(MAIN)).batch_id is None
    now[0] = due
    assert (await train.visit(MAIN)).state == "testing"


async def test_seal_now_is_one_call_and_preserves_pr_and_candidate_gates(world):
    now = [1000.0]
    train, github, _ = await hosted_train(world, clock=lambda: now[0], settling=True)
    assert (await train.visit(MAIN, seal_now=True)).state == "idle"
    head = await completed(world, "a")
    github.pr_runs[head] = "failure"
    blocked = await train.visit(MAIN, seal_now=True)
    assert blocked.batch_id is None
    assert blocked.detail["blockers"][0]["code"] == "pr_checks_red"
    now[0] = 1010
    github.pr_runs[head] = "success"
    assert (await train.visit(MAIN)).detail["seal_at"] == 1310
    sealed = await train.visit(MAIN, seal_now=True)
    assert sealed.state == "testing", sealed
    assert sealed.checks == "pending"
    assert git(world.origin.url, "rev-parse", "main") != sealed.candidate_sha


async def test_root_cadence_resets_for_a_new_completion_identity(world):
    now = [1000.0]
    head = await completed(world, "a")
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)), clock=lambda: now[0],
                       settling=True)
    assert (await train.visit(MAIN)).detail["seal_at"] == 1300
    now[0] = 1299
    await close(world.db, "a", [head], close_id="reopened-generation", origin=world.origin)
    waiting = await train.visit(MAIN)
    assert waiting.state == "settling" and waiting.detail["seal_at"] == 1599
    now[0] = 1300
    assert (await train.visit(MAIN)).state == "settling"


async def test_mature_settling_hint_never_substitutes_for_current_pr_eligibility(world):
    now = [1000.0]
    train, github, _ = await hosted_train(world, clock=lambda: now[0], settling=True)
    head = await completed(world, "a")
    assert (await train.visit(MAIN)).state == "settling"
    now[0] = 1300
    github.pr_runs[head] = "failure"
    for forced in (False, True):
        blocked = await train.visit(MAIN, seal_now=forced)
        assert blocked.batch_id is None
        assert blocked.detail["blockers"][0]["code"] == "pr_checks_red"
    async with world.db._engine.connect() as conn:
        assert (await conn.execute(select(integration_batches.c.id))).first() is None


async def test_seal_now_direct_batch_hook_and_epic_immediacy(world):
    await world.db.create_task(Task(id="epic", project_id="p", repo_id="r", title="epic",
                                   description="", branch_name="aq/epic",
                                   status=TaskStatus.IN_PROGRESS))
    git(world.origin.clone, "push", "origin", "main:aq/epic")
    await completed(world, "child", parent="epic")
    await completed(world, "root")
    batches = DatabaseBatches(world.db, pr_gate=green_pr, clock=lambda: 1000)
    store = BatchStore(world.db)

    async def freeze(batch, members, **kwargs):
        return await store.freeze(batch, members, trees={m.task_id: tree(world, m.source_sha)
                                                        for m in members})

    service = SimpleNamespace(store=store, freeze=freeze)
    epic = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    collected = await batches.open_batch(epic, await snapshot(world, epic), service)
    assert [m.task_id for m in collected.members] == ["child"]
    assert collected.batch is not None
    assert (await batches.open_batch(MAIN, await snapshot(world), service)).batch is None
    sealed = await batches.open_batch(MAIN, await snapshot(world), service, seal_now=True)
    assert sealed.batch is not None and [m.task_id for m in sealed.members] == ["root"]


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


async def test_two_generated_catalogue_members_merge_by_regeneration(world):
    """The cutover that rolled back twice: two members each regenerated the
    selection catalogue on their own branch. The daemon's own lane rebuilds it
    from the merged sources and produces a candidate; no member is parked."""
    origin = world.origin
    catalogue_repository(origin)
    a = await completed(world, "a", head=catalogue_branch(origin, "a", "alpha"))
    b = await completed(world, "b", head=catalogue_branch(origin, "b", "beta"))
    train, github, _ = await hosted_train(world)
    main = git(origin.url, "rev-parse", "refs/heads/main")

    testing = await train.visit(MAIN)
    assert testing.state == "testing", testing
    assert git(origin.url, "rev-parse", "refs/heads/main") == main
    candidate = git(origin.url, "rev-parse", candidate_ref(testing.batch_id))
    # The candidate's catalogue is the canonical rebuild of both branches'
    # modules, and both sources are its ancestry.
    modules = set(json.loads(git(origin.url, "show", f"{candidate}:{Catalogue.CATALOGUE_PATH}"))[
        "modules"])
    assert modules == {"tests/test_alpha.py", "tests/test_base.py", "tests/test_beta.py"}
    for source in (a, b):
        git(origin.url, "merge-base", "--is-ancestor", source, candidate)

    github.runs[testing.candidate_sha] = "success"
    delivered = await train.visit(MAIN)
    assert delivered.state == "delivered", delivered
    assert git(origin.url, "rev-parse", "refs/heads/main") == testing.candidate_sha


async def test_missing_regenerator_is_a_named_blocker_and_fixes_itself(world):
    """A repository configured without a regenerator names itself in status.

    The visit neither publishes nor parks a member, and once the operator sets
    a command the same batch rebuilds and delivers.
    """
    origin = world.origin
    catalogue_repository(origin)
    await completed(world, "a", head=catalogue_branch(origin, "a", "alpha"))
    await completed(world, "b", head=catalogue_branch(origin, "b", "beta"))
    command: list[str | None] = [None]
    train, checks, _ = lane(world, LocalGit(Path(origin.url)), regenerate=lambda: command[0])
    main = git(origin.url, "rev-parse", "refs/heads/main")

    await train.tick()
    await train.drain()
    [blocked] = train.status()
    assert blocked["state"] == "no_regenerator", blocked
    assert blocked["repair"] is None
    assert git(origin.url, "rev-parse", "refs/heads/main") == main
    status = IntegrationStatusService(world.db, git_first="active", train=train)
    project = await status.control_status("p")
    assert [b["code"] for b in project["blockers"]] == ["no_regenerator"]
    assert "regenerate" in project["blockers"][0]["detail"]
    assert [b["code"] for b in (await status.task_blockers("a"))["blockers"]] == [
        "no_regenerator"
    ]

    # The operator sets the command; the same batch then rebuilds and delivers.
    command[0] = DEFAULT_REGENERATE_COMMAND
    testing = await train.visit(MAIN)
    assert testing.state == "testing", testing
    checks.green.add(testing.candidate_sha)
    assert (await train.visit(MAIN)).state == "delivered"


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
        self.credential_identity = GitHubCredentialIdentity.app(101, 202)
        self.runs: dict[str, str] = {}
        self.pr_runs: dict[str, str] = {}
        self.reviews = []
        self.pulls = {}
        self.origin = None
        self.unavailable = False
        self.observed: list[str] = []
        self.records: list[dict] = []

    def _rows(self, sha, event="push"):
        runs = self.runs if event == "push" else self.pr_runs
        n = list(runs).index(sha) + 1 + (100 if event == "pull_request" else 0)
        done = {"head_sha": sha, "status": "completed", "conclusion": runs[sha]}
        repo = {"id": 123, "full_name": self.full_name}
        return (
            {"id": 10 + n, "name": "unit", "app": {"id": 15368},
             "check_suite": {"id": 20 + n}, **done},
            {"id": 30 + n, "workflow_id": 301, "run_attempt": 1, "check_suite_id": 20 + n,
             "event": event, "repository": repo, "head_repository": repo, **done},
            {"id": 50 + n, "name": "unit", "run_id": 30 + n, "run_attempt": 1,
             "check_run_url": f"https://api.github.com/repos/{self.full_name}/check-runs/"
                              f"{10 + n}", **done},
        )

    async def pull_request(self, url):
        from src.git.github_contracts import GitHubAccessError

        if self.unavailable:
            raise GitHubAccessError("transport_unavailable", "fixture outage")
        if url not in self.pulls:
            raise ValueError("fixture PR missing")
        branch = self.pulls[url]
        head = git(self.origin.url, "rev-parse", branch)
        main = git(self.origin.url, "rev-parse", "main")
        merged = (await asyncio.to_thread(subprocess.run,
            ["git", "merge-base", "--is-ancestor", head, main],
            cwd=self.origin.url, check=False)).returncode == 0
        return {"state": "closed" if merged else "open", "merged": merged,
                "merge_commit_sha": main if merged else None,
                "head": {"ref": branch, "sha": head, "repo": {"id": 123}},
                "base": {"ref": "main", "repo": {"id": 123}}}

    async def paged_list(self, path):
        return self.reviews

    async def paged_items(self, path, *, key):
        if key == "jobs":
            run = int(re.search(r"/actions/runs/(\d+)/", path).group(1))
            for event, runs in (("push", self.runs), ("pull_request", self.pr_runs)):
                for sha in runs:
                    rows = self._rows(sha, event)
                    if rows[1]["id"] == run:
                        return [rows[2]]
            return []
        sha = re.search(r"[0-9a-f]{40}", path).group(0)
        self.observed.append(sha)
        if "check_name=Agent%20Queue%20Integration%20Attestation" in path:
            return [record for record in self.records if record["head_sha"] == sha]
        rows = [self._rows(sha, event)[0 if key == "check_runs" else 1]
                for event, runs in (("push", self.runs), ("pull_request", self.pr_runs))
                if sha in runs]
        return rows

    async def request_json(self, method, path, *, json_body, expected_statuses):
        assert method == "POST" and expected_statuses == {201}
        record = {"id": 7001 + len(self.records), "app": {"id": 101}, **json_body}
        self.records.append(record)
        return {"id": record["id"]}


async def hosted_train(world, *, retained_store=None, clock=time.time, settling=False):
    """The daemon's own lanes for a project with no development pin: hosted checks."""
    db, origin = world.db, world.origin
    async with db._engine.begin() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_policy={
                "root": {"required_checks": {"version": "v1", "names": ["unit"]},
                         "admission": "authorized"},
            },
        ))
    github, trusts = HostedGitHub(), []
    github.origin = origin
    world.github = github
    async with db._engine.connect() as conn:
        for tid, branch, url in (await conn.execute(select(
                tasks.c.id, tasks.c.branch_name, tasks.c.pr_url))).all():
            if url:
                github.pulls[url] = branch
                github.pr_runs[git(origin.url, "rev-parse", branch)] = "success"

    async def load_trust(state, *, boundary):
        required = state["policy_snapshot"][boundary]["required_checks"]
        trusts.append((boundary, state["candidate_sha"], required))
        return IntegrationTrustManifest(
            schema="aq.integration-trust.v1",
            canonical_repository_id=state["canonical_repository_id"],
            repository_id=state["repository_numeric_id"],
            full_name=state["repository_full_name"], ci_producer_app_id=15368,
            attestation_app_id=101, attestation_name=ATTESTATION_CHECK_NAME,
            required_checks={key: required[key] for key in ("version", "names")},
        ), github

    async def binding(repo_row):
        return GitHubRepositoryBinding(123, HostedGitHub.full_name)

    async def store(repo_row):
        return retained_store or origin.clone

    attestation = IntegrationAttestationService(
        db, data_dir=origin.clone.parent, git_manager=LocalGit(Path(origin.url)),
        github_client_factory=None,
    )
    attestation._load_trust = load_trust
    orchestrator = SimpleNamespace(
        db=db, git=LocalGit(Path(origin.url)), github_repository_binding_resolver=binding,
        development_integration=SimpleNamespace(store=store),
        integration_attestation_service=attestation,
    )
    orchestrator.git._github_client = lambda binding: github
    orchestrator.git.bind_github_repository = AsyncMock(return_value=GitHubRepositoryBinding(
        123, github.full_name))
    orchestrator.git.acommits_ahead_of_base = AsyncMock(return_value=1)

    async def create_pr(checkout, **kwargs):
        url = f"https://github.com/{github.full_name}/pull/{700 + len(github.pulls)}"
        github.pulls[url] = kwargs["branch"]
        github.pr_runs[git(origin.url, "rev-parse", kwargs["branch"])] = "success"
        return url

    orchestrator.git.acreate_pr = AsyncMock(side_effect=create_pr)
    batches = (DatabaseBatches if settling else SealedBatches)(db, clock=clock)
    train = IntegrationTrain(
        targets=DatabaseTargets(db), batches=batches,
        lane_for=DaemonLanes(orchestrator, batches=batches, clock=clock),
        repair=OrdinaryRepairService(db, clock=clock), clock=clock,
    )
    return train, github, trusts


@pytest.fixture
async def collected_epic(world):
    """Two ordinary completions collected through the daemon's actual epic lane."""
    db, origin = world.db, world.origin
    base = git(origin.url, "rev-parse", "main")
    await db.create_task(Task(id="epic", project_id="p", repo_id="r", title="epic",
                              description="", branch_name="aq/epic",
                              status=TaskStatus.IN_PROGRESS))
    git(origin.clone, "push", "origin", "main:aq/epic")
    async with db._engine.begin() as conn:
        await conn.execute(insert(task_branch_origins).values(
            id="epic-origin", task_id="epic", repository_id="r", branch_name="aq/epic",
            base_sha=base, creation_generation=0, reserved=True, materialized=True,
            created_at=time.time(),
        ))
    children = [await completed(world, tid, parent="epic") for tid in ("child-a", "child-b")]
    await db.transition_task("epic", TaskStatus.COMPLETED)
    train, github, trusts = await hosted_train(world)
    required = {"version": "v1", "names": ["unit"], "producer_id": "15368"}
    async with db._engine.begin() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_policy={
                "parent": {"required_checks": required, "admission": "authorized"},
                "root": {"required_checks": required, "admission": "reviewed"},
            },
        ))
    return SimpleNamespace(world=world, db=db, origin=origin, train=train, github=github,
                           trusts=trusts, base=base, children=children,
                           target=TrainTarget("p", "r", "refs/heads/aq/epic", "epic"))


async def collect_epic(case):
    testing = await case.train.visit(case.target)
    assert testing.state == "testing", testing
    case.github.runs[testing.candidate_sha] = "success"
    delivered = await case.train.visit(case.target)
    assert delivered.state == "delivered", delivered
    assert await case.db.get_task_completion("epic") is None
    return delivered.target_sha


async def review_epic(case, *, verdict="approved", decision="approve"):
    subject, head = await TreeReviews.observe(await snapshot(case.world, case.target),
                                            "epic", case.target.target_ref)
    async with case.db.immediate() as conn:
        await TreeReviews(case.db).record_on(conn, subject,
            ReviewRequirements(True, frozenset({"github:jack"})), reviewer="github:jack",
            verdict=verdict, decision_id=decision, reviewed_head_sha=head,
            source_base=case.base, provenance={})
    case.github.pr_runs[head] = "success"
    case.github.reviews.append({"id": len(case.github.reviews) + 1,
        "state": "APPROVED" if verdict == "approved" else "CHANGES_REQUESTED",
        "commit_id": head, "user": {"login": "jack", "type": "User"}})


async def test_collected_two_child_epic_gets_completion_and_lands_via_root(collected_epic):
    case = collected_epic
    # A completed container's uncollected branch tip never enters a root batch.
    before = await case.train.visit(MAIN)
    assert before.state == "blocked" and before.batch_id is None
    assert await case.db.get_task_completion("epic") is None
    head = await collect_epic(case)
    waiting = await case.train.visit(case.target)
    assert waiting.state == "blocked"
    assert "review_missing" in {b["code"] for b in waiting.detail["blockers"]}
    await review_epic(case)
    assert (await case.train.visit(case.target)).state == "idle"
    completion = await case.db.get_task_completion("epic")
    assert completion.outcome == "pass" and completion.commits == [head]
    assert completion.branch == "aq/epic"
    # A new lane/visit replays the exact generation without appending rows.
    assert (await case.train.visit(case.target)).state == "idle"
    assert len(await case.db.get_task_completions("epic")) == 1
    testing = await case.train.visit(MAIN)
    assert testing.state == "testing", testing
    assert testing.candidate_sha != head
    assert git(case.origin.url, "rev-parse", "main") == case.base
    # Green epic checks do not authorize the distinct root merge candidate.
    assert (await case.train.visit(MAIN)).state == "testing"
    case.github.runs[testing.candidate_sha] = "success"
    delivered = await case.train.visit(MAIN)
    assert delivered.state == "delivered", delivered
    tip = git(case.origin.url, "rev-parse", "main")
    for source in [head, *case.children]:
        git(case.origin.url, "merge-base", "--is-ancestor", source, tip)
    assert (await case.train.visit(MAIN)).state == "idle"


@pytest.mark.parametrize("blocker", ["open_child", "checks", "rejection", "hold"])
async def test_epic_completion_preserves_readiness_constraints(collected_epic, blocker):
    case = collected_epic
    head = await collect_epic(case)
    await review_epic(case)
    if blocker == "open_child":
        await case.db.transition_task("child-b", TaskStatus.READY)
    elif blocker == "checks":
        del case.github.runs[head]
    elif blocker == "rejection":
        await review_epic(case, verdict="rejected", decision="reject")
    else:
        await case.db.add_task_label("epic", "hold:operator")
    visit = await case.train.visit(case.target)
    assert visit.state == "blocked", visit
    assert await case.db.get_task_completion("epic") is None
    root = await case.train.visit(MAIN)
    assert root.batch_id is None
    assert git(case.origin.url, "rev-parse", "main") == case.base


async def test_epic_completion_revalidates_after_provenance_and_replays(collected_epic, monkeypatch):
    from src.integration.provenance import GitProvenance

    case = collected_epic
    head = await collect_epic(case)
    await review_epic(case)
    original = GitProvenance.write_completion

    async def changed(store, source, **kwargs):
        await original(store, source, **kwargs)
        await case.db.add_task_label("epic", "hold:operator")

    with monkeypatch.context() as patch:
        patch.setattr(GitProvenance, "write_completion", changed)
        visit = await case.train.visit(case.target)
    assert visit.state == "blocked"
    assert visit.detail["blockers"][0]["code"] == "epic_changed"
    assert await case.db.get_task_completion("epic") is None
    await case.db.remove_task_label("epic", "hold:operator")
    assert (await case.train.visit(case.target)).state == "idle"
    assert (await case.db.get_task_completion("epic")).commits == [head]


async def test_epic_branch_without_trust_manifest_is_a_named_blocker(collected_epic):
    """An epic branch cut before the repository carried a trust manifest is
    refused by name on every visit: never a failed visit, never a completion."""
    case = collected_epic
    head = await collect_epic(case)
    await review_epic(case)
    attestation = case.train.lane_for.orchestrator.integration_attestation_service
    # The real trust load reads the exact epic head's tree from a retained store.
    del attestation._load_trust
    case.github.repository = GitHubRepositoryBinding(123, HostedGitHub.full_name)
    attestation.github_client_factory = lambda binding: case.github

    async def fetch(destination_git_dir, *, repository, oid, destination_ref):
        git(case.origin.clone, "init", "-q", "--bare", destination_git_dir)
        git(destination_git_dir, "fetch", "-q", case.origin.url,
            f"+{case.target.target_ref}:{destination_ref}")
        return git(destination_git_dir, "rev-parse", destination_ref)

    attestation.git.afetch_repository_oid = fetch
    for _ in range(2):
        visit = await case.train.visit(case.target)
        assert visit.state == "blocked", visit
        refused = [b for b in visit.detail["blockers"] if b["code"] == "subject_trust_missing"]
        assert [(b["task_id"], b["ref"], b["head_sha"]) for b in refused] == [
            ("epic", "refs/heads/aq/epic", head)]
        assert ".github/agent-queue-integration.json" in refused[0]["detail"]
    assert await case.db.get_task_completion("epic") is None
    assert attestation._subject_trust["epic-readiness-epic"]["cause"] == "missing"
    root = await case.train.visit(MAIN)
    assert root.batch_id is None
    assert git(case.origin.url, "rev-parse", "main") == case.base


async def test_uncollected_epic_checkpoint_cannot_enter_root_batch(collected_epic):
    case = collected_epic
    checkpoint = case.origin.work("epic", "checkpoint")
    await close(case.db, "epic", [checkpoint], origin=case.origin)
    visit = await case.train.visit(MAIN)
    assert visit.state == "blocked" and visit.batch_id is None
    assert visit.detail["blockers"][0]["code"] == "epic_completion_pending"
    assert git(case.origin.url, "rev-parse", "main") == case.base


@pytest.mark.parametrize("notes", ["", '{"graph_sha256": "stale", "heads": []}'],
                         ids=["legacy", "stale"])
async def test_delivered_epic_with_stale_completion_does_not_block_root(collected_epic, notes):
    from src.database.tables import task_completion_records
    from src.integration.delivery_truth import load_delivery_requests

    case = collected_epic
    checkpoint = case.origin.work("epic", "checkpoint")
    await close(case.db, "epic", [checkpoint], origin=case.origin)
    async with case.db._engine.begin() as conn:
        await conn.execute(update(task_completion_records).where(
            task_completion_records.c.task_id == "epic",
        ).values(notes=notes))
    # The retained source reached main under another commit, without its ancestry.
    git(case.origin.clone, "checkout", "-q", "-B", "main", "origin/main")
    git(case.origin.clone, "merge", "--squash", "origin/aq/epic")
    git(case.origin.clone, "commit", "-qm", "squashed epic delivery")
    git(case.origin.clone, "push", "-q", "origin", "main")
    observed = await snapshot(case.world)
    request = (await load_delivery_requests(case.db, ["epic"], repository_id="r",
                                           target_ref=MAIN.target_ref, reduced=True))["epic"]
    evidence = await observed.is_delivered(request, source_base=case.base)
    assert evidence.satisfied
    assert evidence.reason in {"whole_source_patch", "full_tree", "merge_noop"}
    blockers = []
    batches = fixture_batches(case.db)
    assert await batches.pending(MAIN, observed, blockers=blockers) is None
    assert blockers == []
    assert (await case.train.visit(MAIN)).state == "idle"

    # Even a later epic head cannot invalidate its already delivered completion.
    case.origin.work("epic", "later")
    after = await completed(case.world, "after", needs=("epic",))
    members, requests, dependencies = await batches.pending(
        MAIN, await snapshot(case.world), blockers=blockers,
    )
    assert [(member.task_id, member.source_sha) for member in members] == [("after", after)]
    assert set(requests) == {"after"}
    assert dependencies == {"after": set()}
    assert blockers == []


async def test_closed_epic_retries_after_child_origins_retire(collected_epic):
    case = collected_epic
    await collect_epic(case)
    async with case.db._engine.begin() as conn:
        await conn.execute(update(task_branch_origins).where(
            task_branch_origins.c.task_id.in_(("child-a", "child-b")),
        ).values(retired_at=time.time()))
    assert case.target in await DatabaseTargets(case.db).targets(time.time())
    await review_epic(case)
    # Retiring an origin removes a child's base hint, but ancestry still proves
    # the exact retained sources in the collected branch.
    assert (await case.train.visit(case.target)).state == "idle"
    assert await case.db.get_task_completion("epic") is not None


@pytest.mark.parametrize("change", ["child", "review", "checks", "hold", "policy", "completion"])
async def test_frozen_epic_rechecks_readiness_before_root_publication(collected_epic, change):
    case = collected_epic
    head = await collect_epic(case)
    await review_epic(case)
    assert (await case.train.visit(case.target)).state == "idle"
    testing = await case.train.visit(MAIN)
    assert testing.state == "testing"
    case.github.runs[testing.candidate_sha] = "success"
    if change == "child":
        # Normal reopening is already refused by the frozen ancestor guard.
        # Inject changed graph input to exercise the publication fence itself.
        async with case.db._engine.begin() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == "child-b").values(status="READY"))
    elif change == "review":
        await review_epic(case, verdict="rejected", decision="later-rejection")
    elif change == "checks":
        # A provider rerun overwrites the cache of the epic's exact head.
        case.github.runs[head] = "failure"
        await case.train.visit(case.target)
    elif change == "hold":
        await case.db.add_task_label("epic", "hold:operator")
    elif change == "completion":
        later_head = case.origin.work("epic", "later")
        case.github.runs[later_head] = "success"
        await review_epic(case, decision="later-approval")
        assert (await case.train.visit(case.target)).state == "idle"
        assert (await case.db.get_task_completion("epic")).commits == [later_head]
    else:
        project = await case.db.get_project("p")
        policy = dict(project.hierarchical_integration_policy)
        policy["parent"] = {**policy["parent"], "required_checks": {
            **policy["parent"]["required_checks"], "version": "v2"}}
        await case.db.update_project("p", hierarchical_integration_policy=policy)
    refused = await case.train.visit(MAIN)
    assert refused.state == "held", refused
    assert git(case.origin.url, "rev-parse", "main") == case.base


async def test_epic_recompletes_same_head_when_check_policy_changes(collected_epic):
    case = collected_epic
    head = await collect_epic(case)
    await review_epic(case)
    await case.train.visit(case.target)
    first = await case.db.get_task_completion("epic")
    project = await case.db.get_project("p")
    policy = dict(project.hierarchical_integration_policy)
    policy["parent"] = {**policy["parent"], "required_checks": {
        **policy["parent"]["required_checks"], "version": "v2"}}
    await case.db.update_project("p", hierarchical_integration_policy=policy)
    assert (await case.train.visit(MAIN)).batch_id is None
    assert (await case.train.visit(case.target)).state == "idle"
    second = await case.db.get_task_completion("epic")
    assert first.id != second.id and first.commits == second.commits == [head]
    assert (await case.train.visit(MAIN)).state == "testing"


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
    # The session-close PR side effect, observed separately from completion provenance.
    await db.update_task(tid, pr_url="https://github.com/acme/widgets/pull/600")
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
    assert ("root", candidate, {"version": "v1", "names": ["unit"]}) in trusts

    github.runs[main] = "success"  # A green target says nothing about the candidate.
    assert (await train.visit(MAIN)).state == "testing"
    assert git(origin.url, "rev-parse", "refs/heads/main") == main

    github.runs[candidate] = "success"
    delivered = await train.visit(MAIN)
    assert (delivered.state, delivered.checks) == ("delivered", "green"), delivered
    tip = git(origin.url, "rev-parse", "refs/heads/main")
    assert tip == candidate
    [attestation] = github.records
    assert attestation["head_sha"] == candidate
    assert attestation["name"] == ATTESTATION_CHECK_NAME
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


@pytest.mark.parametrize(("push", "allowed"), [
    ({"branches": ["aq/parent/**", "aq/integration/**"]}, False),
    ({"branches": ["aq/batches/**"]}, True),
    ({"branches": ["aq/*"]}, False),
    ({"branches": ["aq/**", "!aq/batches/**"]}, False),
    ({"branches": ["aq/**", "!aq/batches/**", "aq/batches/*"]}, True),
    ({"branches-ignore": ["aq/batches/**"]}, False),
    ({"tags": ["*"]}, False),
    ({}, True),
    ({"branches": ["aq/batches/[a-z]+"]}, None),
    ({"branches": "aq/batches/**"}, None),
])
def test_candidate_branch_filter_diagnostic(push, allowed):
    assert _push_branch_allowed(push, "refs/heads/aq/batches/abc") is allowed


async def test_old_epic_workflow_files_repair_and_exact_push_then_delivers(collected_epic):
    """Real Git/ref lease and PostgreSQL, with Actions driven by candidate push filters."""
    case = collected_epic
    path = ".github/workflows/tests.yml"
    old = ("name: Tests\non:\n  push:\n    branches:\n"
           "      - 'aq/parent/**'\n      - 'aq/integration/**'\n"
           "  workflow_dispatch:\njobs:\n  unit:\n    name: unit\n")
    current = old.replace("  workflow_dispatch:",
                          "      - 'aq/batches/**'\n  workflow_dispatch:")
    epic = commit(case.origin.clone, {path: old}, base=case.base)
    git(case.origin.clone, "push", "origin", f"{epic}:aq/epic")
    main = commit(case.origin.clone, {path: current}, base=case.base)
    git(case.origin.clone, "push", "origin", f"{main}:main")

    # A workflow run exists only after a real push whose own tree allows the ref.
    transport = case.train.lane_for.git
    push = transport.apush_repository_oid

    async def actions_push(store, **kwargs):
        pushed = await push(store, **kwargs)
        sha, branch = kwargs["tip_oid"], kwargs["branch"]
        if branch.startswith("aq/batches/"):
            assert git(case.origin.url, "rev-parse", branch) == sha
            workflow = git(case.origin.url, "show", f"{sha}:{path}")
            if "'aq/batches/**'" in workflow:
                case.github.runs[sha] = "success"
        return pushed

    transport.apush_repository_oid = actions_push
    clock = SimpleNamespace(now=time.time())
    case.train.clock = case.train.lane_for.clock = lambda: clock.now
    await case.train.tick()
    await case.train.drain()
    testing = next(row for row in case.train.status() if row["target_ref"] == case.target.target_ref)
    assert (testing["state"], testing["checks"]) == ("testing", "pending")
    assert testing["candidate_sha"] not in case.github.runs

    clock.now += 300
    await case.train.tick()
    await case.train.drain()
    status = IntegrationStatusService(case.db, git_first="active", train=case.train)
    project = await status.control_status("p")
    blocker = next(b for b in project["blockers"] if b["code"] == "ci_not_triggered")
    assert path in blocker["detail"] and "exclude refs/heads/aq/batches/" in blocker["detail"]
    assert blocker["candidate_sha"] == testing["candidate_sha"]
    own = await status.task_blockers("child-a")
    assert "ci_not_triggered" in {b["code"] for b in own["blockers"]}
    blocked = next(row for row in case.train.status() if row["target_ref"] == case.target.target_ref)
    assert (blocked["state"], blocked["checks"]) == ("repair", "unknown")
    repair = await case.db.get_task(blocked["repair"]["task_id"])
    assert "Fetch refs/heads/main" in repair.description
    assert "ordinary commit" in repair.description and path in repair.description
    assert "workflow_dispatch" in repair.description
    assert git(case.origin.url, "rev-parse", "aq/epic") == epic
    assert case.github.records == []  # Absence never becomes an attestation.

    await case.train.tick()
    await case.train.drain()
    again = next(row for row in case.train.status() if row["target_ref"] == case.target.target_ref)
    assert again["repair"]["outcome"] == "exists"
    assert again["repair"]["task_id"] == repair.id
    assert again["repair"]["attempt_count"] == 1

    # Follow the supported repair brief: apply the default branch's workflow
    # in a normal commit, then publish under this repair task's managed lease.
    repaired = commit(case.origin.clone, {path: current}, base=testing["candidate_sha"])
    locks = BranchLock(case.db)
    fence = Fence.model_validate(blocked["repair"]["fence"])
    await locks.fenced_push(
        fence, git=transport, checkout_path=str(case.origin.clone),
        repository=GitHubRepositoryBinding(123, HostedGitHub.full_name),
        tip_oid=repaired, expected_old_oid=testing["candidate_sha"],
    )
    await locks.release(fence)
    assert repaired in case.github.runs and testing["candidate_sha"] not in case.github.runs
    clock.now += 1
    delivered = await case.train.visit(case.target)
    assert (delivered.state, delivered.checks) == ("delivered", "green")
    assert git(case.origin.url, "rev-parse", "aq/epic") == repaired
    [attestation] = case.github.records
    assert (attestation["head_sha"], attestation["name"]) == (repaired, ATTESTATION_CHECK_NAME)
    assert case.trusts[-1][2]["names"] == ["unit"]
    for source in case.children:
        git(case.origin.url, "merge-base", "--is-ancestor", source, repaired)


def approve_pr(github, head, *, reviewer="default-fix-reviewer"):
    """Supply the human review required by a reviewed root boundary."""
    github.reviews.append({"id": len(github.reviews) + 1, "state": "APPROVED",
        "commit_id": head, "user": {"login": reviewer, "type": "User"}})


async def preexisting_epic_with_default_fix(case, *, provenance="train_candidate"):
    """Deliver a default fix through the real train while the epic stays on its old base."""
    case.train.baseline = CandidateBaselineService(case.db)
    case.github.rerequest_check_suite = AsyncMock()
    head = await completed(case.world, "default-fix")
    approve_pr(case.github, head)
    root = await case.train.visit(MAIN)
    assert root.state == "testing", root
    case.github.runs[root.candidate_sha] = "success"
    delivered = await case.train.visit(MAIN)
    assert delivered.state == "delivered", delivered
    main = delivered.target_sha
    assert git(case.origin.url, "rev-parse", "aq/epic") == case.base
    if provenance != "train_candidate":
        git(case.origin.clone, "push", "origin", "--delete",
            candidate_ref(root.batch_id).removeprefix("refs/heads/"))
    if provenance != "attestation":
        case.github.records.clear()
    case.github.runs[case.base] = "failure"
    testing = await case.train.visit(case.target)
    assert testing.state == "testing", testing
    case.github.runs[testing.candidate_sha] = "failure"
    return main, testing


@pytest.mark.parametrize("provenance", ["train_candidate", "attestation"])
async def test_preexisting_epic_syncs_verified_default_and_requires_repaired_exact_ci(
    collected_epic, provenance,
):
    """Main's fix authorizes one managed merge; only the repaired candidate can publish."""
    case = collected_epic
    main, testing = await preexisting_epic_with_default_fix(case, provenance=provenance)
    visit = await case.train.visit(case.target)
    assert (visit.state, visit.checks) == ("repair", "red"), visit
    assert visit.detail["baseline"]["pre_existing_checks"] == ["unit"]
    decision = visit.detail["sync_default_branch"]
    assert decision == {"ref": MAIN.target_ref, "sha": main, "checks": ["unit"],
                        "provenance": provenance, "outcome": "filed"}
    task = await case.db.get_task(visit.repair["task_id"])
    assert f"merge the pinned commit {main}" in task.description
    assert "regenerated, never hand-merged" in task.description
    assert "unchanged trusted required checks and attestation" in task.description
    for child, source in zip(("child-a", "child-b"), case.children, strict=True):
        assert f"{child} (source {source})" in task.description
    repair_input = await case.train.repair.input(task.id)
    assert repair_input["sync_default_branch"] == {
        key: value for key, value in decision.items() if key != "outcome"
    }
    status = await IntegrationStatusService(
        case.db, git_first="active", train=case.train,
    ).control_status("p")
    own = next(row for row in status["batches"] if row["id"] == testing.batch_id)
    assert [row["task_id"] for row in own["repairs"]] == [task.id]
    members = await BatchStore(case.db).members(testing.batch_id)
    assert {member.source_sha for member in members} == set(case.children)
    again = await case.train.visit(case.target)
    assert (again.repair["outcome"], again.repair["task_id"], again.repair["attempt_count"]) == (
        "exists", task.id, 1,
    )
    assert git(case.origin.url, "rev-parse", "aq/epic") == case.base
    assert case.github.rerequest_check_suite.await_count == 0

    # Act on the immutable brief, retaining both parents and every frozen member.
    git(case.origin.clone, "checkout", "--detach", testing.candidate_sha)
    git(case.origin.clone, "merge", "--no-ff", main, "-m", "Sync verified default fix")
    repaired = git(case.origin.clone, "rev-parse", "HEAD")
    locks = BranchLock(case.db)
    fence = Fence.model_validate(visit.repair["fence"])
    await locks.fenced_push(
        fence, git=case.train.lane_for.git, checkout_path=str(case.origin.clone),
        repository=GitHubRepositoryBinding(123, HostedGitHub.full_name),
        tip_oid=repaired, expected_old_oid=testing.candidate_sha,
    )
    await locks.release(fence)
    pending = await case.train.visit(case.target)
    assert (pending.state, pending.candidate_sha) == ("testing", repaired)
    assert git(case.origin.url, "rev-parse", "aq/epic") == case.base
    assert all(record["head_sha"] != repaired for record in case.github.records)
    case.github.runs[repaired] = "success"
    delivered = await case.train.visit(case.target)
    assert (delivered.state, delivered.checks) == ("delivered", "green"), delivered
    assert git(case.origin.url, "rev-parse", "aq/epic") == repaired
    assert case.github.records[-1]["head_sha"] == repaired
    for source in (main, testing.candidate_sha, *case.children):
        git(case.origin.url, "merge-base", "--is-ancestor", source, repaired)
    assert await BatchStore(case.db).members(testing.batch_id) == members


@pytest.mark.parametrize("reason", [
    "main_red", "main_pending", "unproduced", "stale_attestation", "wrong_app",
    "missing_required_name", "unproven_base",
])
async def test_preexisting_epic_keeps_human_blocker_without_verified_default_fix(
    collected_epic, reason,
):
    case = collected_epic
    provenance = "unproduced" if reason == "unproduced" else (
        "attestation" if reason in {"stale_attestation", "wrong_app"} else "train_candidate"
    )
    main, testing = await preexisting_epic_with_default_fix(case, provenance=provenance)
    if reason == "main_red":
        case.github.runs[main] = "failure"
    elif reason == "main_pending":
        del case.github.runs[main]
    elif reason == "stale_attestation":
        moved = commit(case.origin.clone, {"new-default.txt": "unattested"}, base=main)
        git(case.origin.clone, "push", "origin", f"{moved}:main")
        case.github.runs[moved] = "success"
    elif reason == "wrong_app":
        case.github.records[-1]["app"] = {"id": 999}
    elif reason == "missing_required_name":
        async with case.db._engine.begin() as conn:
            policy = await conn.scalar(select(projects.c.hierarchical_integration_policy).where(
                projects.c.id == "p"))
            policy["root"]["required_checks"]["names"] = ["other-check"]
            await conn.execute(update(projects).where(projects.c.id == "p").values(
                hierarchical_integration_policy=policy))
    elif reason == "unproven_base":
        del case.github.runs[case.base]
    for _ in range(3):
        visit = await case.train.visit(case.target)
        assert visit.state == "preexisting" and visit.repair is None, visit
        assert "sync_default_branch" not in visit.detail
    assert visit.detail["re_request"]["blocker"] == BASELINE_BLOCKER
    assert (await BatchStore(case.db).get(testing.batch_id)).repair_attempt_count == 0
    assert git(case.origin.url, "rev-parse", "aq/epic") == case.base


async def test_preexisting_epic_does_not_sync_default_already_in_candidate(collected_epic):
    case = collected_epic
    main, testing = await preexisting_epic_with_default_fix(case)
    git(case.origin.clone, "checkout", "--detach", testing.candidate_sha)
    git(case.origin.clone, "merge", "--no-ff", main, "-m", "Default already collected")
    candidate = git(case.origin.clone, "rev-parse", "HEAD")
    git(case.origin.clone, "push", "origin", f"{candidate}:{candidate_ref(testing.batch_id)}")
    case.github.runs[candidate] = "failure"
    visit = await case.train.visit(case.target)
    assert visit.state == "preexisting" and visit.repair is None
    assert "sync_default_branch" not in visit.detail
    assert (await BatchStore(case.db).get(testing.batch_id)).repair_attempt_count == 0


async def test_spent_default_sync_returns_to_bounded_human_blocker(collected_epic):
    case = collected_epic
    _, testing = await preexisting_epic_with_default_fix(case)
    first = await case.train.visit(case.target)
    await case.db.update_task(first.repair["task_id"], status=TaskStatus.COMPLETED)
    await BranchLock(case.db).release(Fence.model_validate(first.repair["fence"]))
    # Reconstruct the visit loop with only its durable ordinary repair identity.
    previous = case.train
    case.train = IntegrationTrain(
        targets=previous.targets, batches=previous.batches, lane_for=previous.lane_for,
        repair=OrdinaryRepairService(case.db), baseline=CandidateBaselineService(case.db),
    )
    for _ in range(3):
        visit = await case.train.visit(case.target)
        assert visit.state == "preexisting" and visit.repair is None
        assert visit.detail["sync_default_branch"]["outcome"] == "sync_exhausted"
    assert visit.detail["re_request"]["blocker"] == BASELINE_BLOCKER
    assert (await BatchStore(case.db).get(testing.batch_id)).repair_attempt_count == 1


@pytest.mark.parametrize("archived", [False, True])
async def test_closed_epic_sync_after_abort_hands_code_conflicts_to_worker(collected_epic, archived):
    """Reproduce the parked phase-3 shape without reopening delivered children."""
    case = collected_epic
    collected = await collect_epic(case)
    if archived:
        async with case.db._engine.begin() as conn:
            await conn.execute(update(task_branch_origins).where(
                task_branch_origins.c.task_id.in_(("child-a", "child-b")),
            ).values(retired_at=time.time()))
        for tid in ("child-a", "child-b"):
            await case.db.archive_task(tid)
    prior = Batch("aborted-epic-work", "p", "r", case.target.target_ref)
    store = BatchStore(case.db)
    members = [BatchMember(tid, source, case.base, order)
               for order, (tid, source) in enumerate(zip(
                   ("child-a", "child-b"), case.children, strict=True))]
    await store.freeze(prior, members, trees={member.task_id: git(
        case.origin.url, "rev-parse", f"{member.source_sha}^{{tree}}") for member in members})
    await store.set_intent(prior.id, "aborted")
    files = ["src/integration/service.py", "src/orchestrator/core.py",
             "src/integration/development_runtime.py", "docs/specs/train.md"]
    old = commit(case.origin.clone, {path: "epic code\n" for path in files}, base=collected)
    git(case.origin.clone, "push", "origin", f"{old}:aq/epic")
    fix = commit(case.origin.clone, {path: "default code\n" for path in files}, base=case.base)
    git(case.origin.clone, "push", "origin", f"{fix}:refs/heads/aq/default-fix")
    await completed(case.world, "default-fix", head=fix)
    approve_pr(case.github, fix)
    root = await case.train.visit(MAIN)
    assert root.state == "testing", root
    case.github.runs[root.candidate_sha] = "success"
    main = (await case.train.visit(MAIN)).target_sha
    case.github.runs[old] = "failure"
    case.train.baseline = CandidateBaselineService(case.db)
    assert case.target in await DatabaseTargets(case.db).targets(time.time())
    visit = await case.train.visit(case.target)
    assert visit.state == "repair", visit
    batch = await store.get(visit.batch_id)
    assert batch.epic_sync and visit.batch_id != prior.id
    frozen = await store.members(batch.id)
    assert {member.source_sha for member in frozen} == set(case.children)
    decision = visit.detail["sync_default_branch"]
    assert decision["sha"] == main
    assert decision["conflicting_files"] == sorted(files)
    task = await case.db.get_task(visit.repair["task_id"])
    assert task.parent_task_id is None
    assert "Every frozen member is already merged" in task.description
    assert "scripts/regenerate-generated.sh" in task.description
    for path in files:
        assert path in task.description
    for member in frozen:
        assert f"{member.task_id} (source {member.source_sha})" in task.description
    # Inspection has neither resolved code nor moved the epic or its candidate.
    assert git(case.origin.url, "rev-parse", "aq/epic") == old
    assert visit.candidate_sha == old
    again = await case.train.visit(case.target)
    assert (again.repair["outcome"], again.repair["task_id"]) == ("exists", task.id)

    # An ordinary worker merges and resolves the named source conflicts.
    git(case.origin.clone, "checkout", "--detach", old)
    merge = await asyncio.to_thread(subprocess.run, ["git", "merge", "--no-ff", main],
        cwd=case.origin.clone, capture_output=True, text=True, check=False)
    assert merge.returncode == 1
    assert set(git(case.origin.clone, "diff", "--name-only", "--diff-filter=U").splitlines()) == set(files)
    for path in files:
        (case.origin.clone / path).write_text("epic code\ndefault code\n")
    git(case.origin.clone, "add", *files)
    git(case.origin.clone, "commit", "-m", "Worker resolves default sync")
    repaired = git(case.origin.clone, "rev-parse", "HEAD")
    locks = BranchLock(case.db)
    fence = Fence.model_validate(visit.repair["fence"])
    await locks.fenced_push(
        fence, git=case.train.lane_for.git, checkout_path=str(case.origin.clone),
        repository=GitHubRepositoryBinding(123, HostedGitHub.full_name),
        tip_oid=repaired, expected_old_oid=old,
    )
    await locks.release(fence)
    assert (await case.train.visit(case.target)).state == "testing"
    assert git(case.origin.url, "rev-parse", "aq/epic") == old
    case.github.runs[repaired] = "success"
    delivered = await case.train.visit(case.target)
    assert delivered.state == "delivered", delivered
    assert git(case.origin.url, "rev-parse", "aq/epic") == repaired
    assert case.github.records[-1]["head_sha"] == repaired
    for source in (old, main, *case.children):
        git(case.origin.url, "merge-base", "--is-ancestor", source, repaired)
    assert await store.members(batch.id) == frozen
    assert (await store.get(prior.id)).intent == "aborted"
    assert (await case.db.get_task("epic")).status is TaskStatus.COMPLETED
    assert (await case.train.visit(case.target)).batch_id is None


@pytest.mark.parametrize("reason", ["default_red", "hold", "spent", "aborted"])
async def test_closed_epic_sync_remains_bounded_and_respects_holds(collected_epic, reason):
    case = collected_epic
    collected = await collect_epic(case)
    head = await completed(case.world, "default-fix")
    approve_pr(case.github, head)
    root = await case.train.visit(MAIN)
    case.github.runs[root.candidate_sha] = "success"
    main = (await case.train.visit(MAIN)).target_sha
    case.github.runs[collected] = "failure"
    case.train.baseline = CandidateBaselineService(case.db)
    if reason == "default_red":
        case.github.runs[main] = "failure"
    elif reason == "hold":
        await case.db.add_task_label("epic", "hold:operator")
    if reason in {"default_red", "hold"}:
        for _ in range(3):
            visit = await case.train.visit(case.target)
            assert visit.state == "blocked" and visit.batch_id is None and visit.repair is None
        return
    first = await case.train.visit(case.target)
    assert first.state == "repair", first
    await case.db.update_task(first.repair["task_id"], status=TaskStatus.COMPLETED)
    await BranchLock(case.db).release(Fence.model_validate(first.repair["fence"]))
    if reason == "aborted":
        await BatchStore(case.db).set_intent(first.batch_id, "aborted")
    previous = case.train
    case.train = IntegrationTrain(
        targets=previous.targets, batches=previous.batches, lane_for=previous.lane_for,
        repair=OrdinaryRepairService(case.db), baseline=CandidateBaselineService(case.db),
    )
    for _ in range(3):
        visit = await case.train.visit(case.target)
        assert visit.batch_id == first.batch_id and visit.repair is None
        assert visit.state == ("held" if reason == "aborted" else "preexisting"), visit
    if reason == "spent":
        assert visit.detail["re_request"]["blocker"] == BASELINE_BLOCKER
    assert (await BatchStore(case.db).get(first.batch_id)).repair_attempt_count == 1
    assert git(case.origin.url, "rev-parse", "aq/epic") == collected


async def test_hosted_lane_missing_attestation_service_refuses_target_publication(world):
    origin = world.origin
    await completed(world, "a")
    train, github, _ = await hosted_train(world)
    main = git(origin.url, "rev-parse", "refs/heads/main")
    testing = await train.visit(MAIN)
    github.runs[testing.candidate_sha] = "success"
    train.lane_for.orchestrator.integration_attestation_service = None
    refused = await train.visit(MAIN)
    assert refused.state == "held"
    assert refused.detail["outcome"] == "attestation_unavailable"
    assert github.records == []
    assert git(origin.url, "rev-parse", "refs/heads/main") == main


async def development_train(world, tmp_path, monkeypatch, validation: str, *, mode="development"):
    """The daemon's own lanes for a development-pinned project: retained local jobs."""
    db, origin = world.db, world.origin
    _, config, load = await pin_development(
        db, tmp_path, validation, hierarchical_integration_mode=mode,
        hierarchical_integration_desired_mode=mode,
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
        integration_attestation_service=SimpleNamespace(publish=AsyncMock()),
    )
    batches = SealedBatches(db)
    train = IntegrationTrain(
        targets=DatabaseTargets(db), batches=batches,
        lane_for=DaemonLanes(orchestrator, batches=batches), repair=OrdinaryRepairService(db),
    )
    [target] = await DatabaseTargets(db).targets(time.time())
    assert target.kind == ("development" if mode == "development" else "root")
    train.attestation_publisher = orchestrator.integration_attestation_service.publish
    return train, target


@pytest.mark.parametrize("mode", ["development", "train"])
async def test_development_lane_publishes_without_attestation_after_exact_candidate_job_passes(
    world, tmp_path, monkeypatch, mode,
):
    origin = world.origin
    a = await completed(world, "a")
    train, target = await development_train(world, tmp_path, monkeypatch, "focused", mode=mode)
    await world.db.update_task("a", pr_url=None)
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
    # The local-validation lane never attests; it publishes on its own gate.
    train.attestation_publisher.assert_not_called()
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
    train.attestation_publisher.assert_not_called()


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


@pytest.mark.parametrize("condition,code", [
    ("missing_pr", "awaiting_pr"),
    ("pending", "awaiting_pr_checks"),
    ("push_green", "awaiting_pr_checks"),
    ("red", "pr_checks_red"),
    ("no_approval", "pr_review_missing"),
    ("old_approval", "pr_review_missing"),
    ("bot_approval", "pr_review_missing"),
    ("dismissed", "pr_review_missing"),
    ("changes", "pr_changes_requested"),
    ("outage", "unknown"),
])
async def test_root_pr_gate_refuses_before_freeze_then_admits_exact_green_head(world, condition, code):
    head = await completed(world, "leaf")
    now = [1000.0]
    train, github, _ = await hosted_train(world, clock=lambda: now[0])
    url = (await world.db.get_task("leaf")).pr_url
    if condition == "missing_pr":
        await world.db.update_task("leaf", pr_url=None)
    elif condition in {"pending", "push_green"}:
        github.pr_runs.clear()
        if condition == "push_green":
            github.runs[head] = "success"
    elif condition == "red":
        github.pr_runs[head] = "failure"
    elif condition in {"no_approval", "changes", "old_approval", "bot_approval", "dismissed"}:
        project = await world.db.get_project("p")
        policy = project.hierarchical_integration_policy
        policy["root"]["admission"] = "reviewed"
        await world.db.update_project("p", hierarchical_integration_policy=policy)
        if condition == "changes":
            github.reviews = [{"id": 1, "commit_id": head, "state": "APPROVED",
                               "user": {"login": "alice", "type": "User"}},
                              {"id": 2, "commit_id": head, "state": "CHANGES_REQUESTED",
                               "user": {"login": "bob", "type": "User"}}]
        elif condition != "no_approval":
            github.reviews = [{"id": 1, "commit_id": "a" * 40 if condition == "old_approval" else head,
                "state": "APPROVED", "user": {"login": "bob",
                "type": "Bot" if condition == "bot_approval" else "User"}}]
            if condition == "dismissed":
                github.reviews.append({"id": 2, "commit_id": head, "state": "DISMISSED",
                                       "user": {"login": "bob", "type": "User"}})
    else:
        github.unavailable = True
    await train.tick()
    await train.drain()
    [visit] = train.status()
    assert visit["batch_id"] is None
    assert visit["detail"]["blockers"][0]["code"] == code
    assert visit["state"] == ("unknown" if condition == "outage" else "blocked")
    async with world.db._engine.connect() as conn:
        assert (await conn.execute(select(integration_batches.c.id))).first() is None
    status = await IntegrationStatusService(world.db, git_first="active", train=train).control_status("p")
    assert code in {blocker["code"] for blocker in status["blockers"]}
    # An outage is cached with backoff and cannot inherit yesterday's green.
    if condition == "outage":
        github.unavailable = False
        assert (await train.visit(MAIN)).state == "unknown"
        now[0] += 61
    await world.db.update_task("leaf", pr_url=url)
    github.pr_runs[head] = "success"
    github.reviews.append({"id": 3, "commit_id": head, "state": "APPROVED",
                           "user": {"login": "bob", "type": "User"}})
    admitted = await train.visit(MAIN)
    assert admitted.state == "testing", admitted
    members = await train.batches.current(MAIN)
    frozen = await BatchStore(world.db).members(members.id)
    assert [(m.task_id, m.source_sha) for m in frozen] == [("leaf", head)]
    # Admission is a freeze-only gate: later PR failures do not undo sealing.
    github.pr_runs[head] = "failure"
    github.unavailable = True
    github.runs[admitted.candidate_sha] = "success"
    delivered = await train.visit(MAIN)
    assert delivered.state == "delivered", delivered
    github.unavailable = False
    pull = await github.pull_request(url)
    assert pull["merged"] and pull["state"] == "closed"
    git(world.origin.url, "merge-base", "--is-ancestor", pull["merge_commit_sha"], "main")


async def test_train_opens_three_child_epic_pr_from_completion_ref(collected_epic):
    from src.integration.provenance import CompletionIdentity, GitProvenance

    case = collected_epic
    # Extend the ordinary graph before collecting it.
    async with case.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic").values(status="IN_PROGRESS"))
    third = await completed(case.world, "child-c", parent="epic")
    case.children.append(third)
    await case.db.transition_task("epic", TaskStatus.COMPLETED)
    head = await collect_epic(case)
    await review_epic(case)
    visit = await case.train.visit(case.target)
    [opened] = visit.detail["epic_completions"]
    assert opened["outcome"] == "opened" and opened["head_sha"] == head
    completion = await case.db.get_task_completion("epic")
    observed = await snapshot(case.world, case.target)
    record = await GitProvenance(observed.truth.git, observed.observation.store,
        repository_url=case.origin.url).read_completion(CompletionIdentity(
            "p", "r", "epic", completion.id), refs=observed.observation.source_heads)
    assert record["source_oid"] == head
    git_manager = case.train.lane_for.git
    kwargs = git_manager.acreate_pr.await_args.kwargs
    assert kwargs["base"] == "main" and kwargs["branch"] == "aq/epic"
    for tid in ("child-a", "child-b", "child-c"):
        assert f"`{tid}`" in kwargs["body"]
    assert "AQ-Epic: epic" in kwargs["body"]
    actual = set(git(case.origin.clone, "diff", "--name-only", f"{case.base}...{head}").splitlines())
    union = set()
    for child in case.children:
        paths = git(case.origin.clone, "diff", "--name-only", f"{case.base}...{child}").splitlines()
        union.update(paths)
        for path in paths:
            assert git(case.origin.clone, "show", f"{head}:{path}") == git(
                case.origin.clone, "show", f"{child}:{path}")
    assert actual == union
    await case.train.visit(case.target)
    git_manager.acreate_pr.assert_awaited_once()
    first = await case.train.visit(MAIN)
    case.github.runs[first.candidate_sha] = "success"
    assert (await case.train.visit(MAIN)).state == "delivered"
    pull = await case.github.pull_request(opened["pr_url"])
    assert pull["merged"] and pull["merge_commit_sha"] == first.candidate_sha
    # Batch rows are an audit: deleting them cannot undo git delivery.
    from sqlalchemy import delete, text

    from src.database.tables import integration_batch_members

    async with case.db._engine.begin() as conn:
        # Destructive audit-loss simulation in this disposable fixture only.
        await conn.execute(text("ALTER TABLE integration_batch_members DISABLE TRIGGER USER"))
        await conn.execute(text("ALTER TABLE integration_batches DISABLE TRIGGER USER"))
        await conn.execute(delete(integration_batch_members))
        await conn.execute(delete(integration_batches))
        await conn.execute(text("ALTER TABLE integration_batch_members ENABLE TRIGGER USER"))
        await conn.execute(text("ALTER TABLE integration_batches ENABLE TRIGGER USER"))
    assert (await case.train.visit(MAIN)).state == "idle"
    assert await case.train.batches.pending(MAIN, await snapshot(case.world)) is None
    status = await IntegrationStatusService(case.db, git_first="active").control_status("p")
    assert status["batches"] == [] and status["blockers"] == []
    delivery = await case.db._delivery_observer.observe(["epic"])
    async with case.db._engine.connect() as conn:
        assert (await delivery.verified_on(conn, ["epic"]))["epic"].satisfied


async def test_forged_promoted_audit_does_not_deliver_a_pending_member(world):
    head = await completed(world, "leaf")
    train, _, _ = await hosted_train(world)
    batch = Batch("forged", "p", "r", MAIN.target_ref)
    base = git(world.origin.clone, "rev-parse", f"{head}^")
    await BatchStore(world.db).freeze(batch, (BatchMember("leaf", head, base),),
                                    trees={"leaf": tree(world, head)})
    async with world.db._engine.begin() as conn:
        await conn.execute(update(integration_batches).where(integration_batches.c.id == batch.id)
                           .values(lifecycle="promoted", tested_candidate_sha=head, final_main_sha=head))
    # No corresponding ref movement happened; ordinary discovery still sees it.
    pending = await train.batches.pending(MAIN, await snapshot(world))
    assert [(member.task_id, member.source_sha) for member in pending[0]] == [("leaf", head)]
    delivery = await world.db._delivery_observer.observe(["leaf"])
    async with world.db._engine.connect() as conn:
        assert not (await delivery.verified_on(conn, ["leaf"]))["leaf"].satisfied
    assert (await train.visit(MAIN)).state == "testing"


async def test_legacy_delivery_doctor_reports_unreachable_shas_without_writing(world):
    from src.doctor.integration_checks import _BY_ID
    from src.doctor.models import DoctorContext, Severity

    pending = await completed(world, "pending")
    delivered = await completed(world, "landed", land=True)
    await legacy_delivery(world, "pending", pending)
    await legacy_delivery(world, "landed", delivered)
    async with world.db._engine.connect() as conn:
        before = list((await conn.execute(select(integration_legacy_deliveries))).all())
    check = _BY_ID["integration.legacy_deliveries"]
    assert check.fix is None
    result = await check.run(DoctorContext(config=None, db=world.db))
    assert result.severity == Severity.WARN
    assert [(row["task_id"], row["sha"]) for row in result.data["unreachable"]] == [
        ("pending", pending)]
    assert result.data["unknown"] == [] and not result.fixable
    async with world.db._engine.connect() as conn:
        assert list((await conn.execute(select(integration_legacy_deliveries))).all()) == before


@pytest.mark.parametrize("mismatch", ["closed", "head", "branch", "base", "fork"])
async def test_root_pr_gate_requires_exact_open_same_repository_identity(world, mismatch):
    await completed(world, "leaf")
    train, github, _ = await hosted_train(world)
    url = (await world.db.get_task("leaf")).pr_url
    pull = await github.pull_request(url)
    if mismatch == "closed":
        pull["state"] = "closed"
    elif mismatch == "head":
        pull["head"]["sha"] = "a" * 40
    elif mismatch == "branch":
        pull["head"]["ref"] = "aq/another"
    elif mismatch == "base":
        pull["base"]["ref"] = "another"
    else:
        pull["head"]["repo"]["id"] = 456
    original = github.pull_request
    github.pull_request = AsyncMock(return_value=pull)
    blocked = await train.visit(MAIN)
    assert blocked.batch_id is None and blocked.detail["blockers"][0]["code"] == "awaiting_pr"
    assert github.observed == []  # Don't read CI for a different proposal.
    github.pull_request = original
    assert (await train.visit(MAIN)).state == "testing"


async def test_failed_epic_pr_open_is_retried_from_completion_ref_without_checkpoint(collected_epic):
    from src.git.manager import GitError
    from src.integration.root_pull_requests import RootPullRequestReconciler

    case = collected_epic
    head = await collect_epic(case)
    await review_epic(case)
    manager = case.train.lane_for.git
    create = manager.acreate_pr
    manager.acreate_pr = AsyncMock(side_effect=GitError("fixture PR open outage"))
    visit = await case.train.visit(case.target)
    assert visit.state == "idle"
    assert visit.detail["epic_completions"][0]["outcome"] == "unknown"
    assert (await case.db.get_task_completion("epic")).commits == [head]
    assert (await case.train.visit(MAIN)).detail["blockers"][0]["code"] == "awaiting_pr"
    await case.train.visit(case.target)
    manager.acreate_pr.assert_awaited_once()  # Only maintenance retries the failed open.
    manager.acreate_pr = create
    reconciler = RootPullRequestReconciler(case.db, manager)
    assert "epic" in await reconciler._page(None)
    await reconciler.tick(time.time())
    create.assert_awaited_once()
    assert (await case.db.get_task("epic")).pr_url
    assert (await case.train.visit(MAIN)).state == "testing"


async def test_epic_branch_move_resettles_completion_and_existing_pr(collected_epic):
    case = collected_epic
    old = await collect_epic(case)
    await review_epic(case)
    assert (await case.train.visit(case.target)).state == "idle"
    old_completion = await case.db.get_task_completion("epic")
    url = (await case.db.get_task("epic")).pr_url
    git(case.origin.clone, "fetch", "-q", "origin")
    git(case.origin.clone, "checkout", "-q", "-B", "aq/epic", "origin/aq/epic")
    new = commit(case.origin.clone, {"followup.txt": "followup"})
    git(case.origin.clone, "push", "-q", "origin", "aq/epic")
    case.github.runs[new] = "success"
    # Old completion cannot freeze the moved branch even though it retains all children.
    assert (await case.train.visit(MAIN)).batch_id is None
    await review_epic(case, decision="followup-approve")
    visit = await case.train.visit(case.target)
    assert visit.detail["epic_completions"][0]["outcome"] == "already_open"
    completion = await case.db.get_task_completion("epic")
    assert completion.id != old_completion.id and completion.commits == [new]
    assert old != new and (await case.github.pull_request(url))["head"]["sha"] == new
    case.train.lane_for.git.acreate_pr.assert_awaited_once()
    assert (await case.train.visit(MAIN)).state == "testing"
