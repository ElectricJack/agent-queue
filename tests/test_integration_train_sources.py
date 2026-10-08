"""The train's durable sources over a real origin; disposable PostgreSQL.

Targets come from project mode and completed work's routing, members are the
exact completion sources Git does not yet hold, and a whole visit freezes,
builds, gates and fast-forwards a target through the ref lease.

Fidelity replay (2026-10-06-git-first-train-fidelity.md, §6 test 12)
----------------------------------------------------------------
Re-read ``47ad054f0:tests/test_epic_train_end_to_end.py`` alongside the
spec's §1 process comparison. Line numbers below belong to that revision,
not this checkout. Counterpart keys resolve to exact test names in the second
table; §6 numbers identify the restored acceptance scenarios, not old DB rows.

| Old asserted state (lines) | §6 | Named counterpart key / disposition |
| --- | --- | --- |
| Delete only in the bound repository (95) | 9 | Cleanup; Replay |
| Three child sources contained in epic (163) | 1 | Open; Replay |
| PR opened once; body lists children (328-332) | 1 | Open; Replay |
| Repeated open returns already_open (333-335) | 1 | Move; Replay |
| GitHub human approval identity (349) | 2 | Gate; Replay; policy choice, §2.3 |
| Not due before 300 s; due at deadline (350-352) | 5 | Cadence; Cap |
| Sealed with exact epic, tree and ref (354, 359, 361-362) | 2, 3 | Replay |
| Frozen review_evidence_id points at approval (360) | 2 | Gate; see R1 below |
| Distinct built candidate pushed at exact OID (377-379) | 3 | Replay; Hosted |
| Red CI leaves tested_candidate_sha empty (419-422) | 3, 7 | Replay; Red |
| Promotion refused without candidate CI (439) | 3 | Replay; Hosted |
| Old candidate revision marked superseded (460-467) | — | R2: §2.3 |
| Repair rebuild uses repaired OID on same ref (469-470) | 3, 7 | Replay |
| Revision 1; same audit PR; two audit revisions (469, 471-472) | — | R2: §2.3 |
| Green checks record the exact repaired OID (518-521) | 3, 7 | Replay |
| Promoted OID is exactly main's head (528-530) | 3 | Replay; Hosted |
| Delivery receipt points at main (531-535) | 4 | Git; Forged; see R3 below |
| Cleanup materialized once (548) | 9 | Replay; Cleanup |
| Red and repaired-green CI evidence retained (572-575) | 3, 9 | Replay; R2 for revision |
| source_pr/remote_ref/local_ref/audit_pr items (576-581) | 9 | Replay; R2 for PR items |
| Every cleanup item finishes; batch complete (583-587) | 9 | Replay; Cleanup |
| Source and candidate audit PRs closed (588) | 3, 9 | Replay; R2 for audit PR |
| One comment names repaired promoted SHA (589-590) | 3, 9 | Replay; Cleanup |
| Epic, candidate and three child refs deleted (591-597) | 9 | Replay |
| Cleanup leaves exact promoted main unchanged (598) | 9 | Replay |

| Counterpart key | Exact callable (in this module unless qualified) |
| --- | --- |
| Replay | test_fidelity_replay_pr_repair_promotion_cleanup |
| Open | test_train_opens_three_child_epic_pr_from_completion_ref |
| Move | test_epic_branch_move_resettles_completion_and_existing_pr |
| Gate | test_root_pr_gate_refuses_before_freeze_then_admits_exact_green_head |
| Cadence | test_root_cadence_uses_latest_admission_and_survives_restart |
| Cap | test_root_settling_cap_bounds_continuous_admissions_across_restart |
| Hosted | test_hosted_lane_publishes_only_the_exact_candidate_github_passed |
| Red | test_hosted_red_candidate_files_one_repair_and_never_publishes |
| Git | test_train_opens_three_child_epic_pr_from_completion_ref |
| Forged | test_forged_promoted_audit_does_not_deliver_a_pending_member |
| Cleanup | test_promoted_train_cleans_refs_and_comments_only_repair_commits |

R1: The approval FK was record authority. §2.1 makes live exact-head PR checks
and reviews gate inputs and their DB evidence a refreshable cache. §2.3 does
not restore the journal/intent machinery that bound that approval to a seal.
Replay asserts the frozen source/tree/ref and Gate asserts admission instead.
Roots do not require a human unless the boundary selects reviewed admission
(§2.3); Replay selects it explicitly and checks the reviewer and exact head.

R2: §2.3 deliberately does not restore candidate revisions, episodes, parent
verifications or the stage dossier. The revision-indexed candidate audit PR
and its separate cleanup item belong to that removed machinery (§2.2 keeps
aq/batches/* and exact-OID publication). Train cleanup reuses the audit_pr
item kind for the member PR's comment; it has no separate candidate PR or
source_pr item that closes a PR. Replay proves that the same source PR and
candidate ref survive repair, GitHub reports the source PR merged, and
cleanup comments the repair commit without closing PRs itself. Red/green
evidence is keyed by exact SHA, not a candidate revision number.

R3: §2.1 replaces delivery receipts/journal authority with Git containment.
Git deletes batch/member audit rows and still sees delivery; Forged inserts
a lying promoted record and still sees pending work. No receipt is restored
to decide delivery (§2.3's removal of record machinery).

Process coverage beyond the old replay: §6 test 6's leaf root follows Gate
through GitHub's merged report. Test 7's bounded debug/human escalation is
``test_integration_train.py::
test_repair_escalation_debug_then_one_supervisor_incident_and_green_recovery``.
Test 8 is ``test_train_eject_preserves_frozen_members_pr_and_readmits_after_cadence``.
Test 9's rewritten-source exception is
``test_promoted_train_bundles_rewritten_member_without_deleting_it``; its
abort-only cleanup is
``test_abort_cleanup_removes_retained_candidate_but_keeps_local_member``.
The missing connected root red-CI -> repair -> exact promotion -> cleanup
assertions are supplied by Replay below. No old process step is excluded;
only the explicitly superseded record/audit representations above are omitted.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.tables import (
    events,
    integration_batches,
    integration_source_ci,
    integration_legacy_deliveries,
    projects,
    repos,
    task_branch_origins,
    tasks,
)
from src.git.github_contracts import (
    GitHubAccessError,
    GitHubCredentialIdentity,
    GitHubRepositoryBinding,
)
from src.git.manager import GitManager
from src.integration.attestation import IntegrationAttestationService
from src.integration.batches import (
    Batch,
    BatchMember,
    BatchObservation,
    BatchService,
    BatchStore,
    candidate_ref,
)
from src.integration.candidate_baseline import BASELINE_BLOCKER, CandidateBaselineService
from src.integration.ci import ATTESTATION_CHECK_NAME, IntegrationTrustManifest
from src.integration.cleanup import IntegrationCleanupService
from src.integration.delivery_observer import DeliveryObserver
from src.integration.git_truth import GitTruth
from src.integration.gitops import GitOperations, RetainedRepository, SubjectGitAuthority
from src.integration.lock import BranchLock
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchBusy
from src.integration.regeneration import DEFAULT_REGENERATE_COMMAND
from src.integration.repair import OrdinaryRepairService
from src.integration.reviews import ReviewRequirements, TreeReviews
from src.integration.selection_metrics import SelectionMetrics, selection_metrics_scope
from src.integration.status import IntegrationStatusService
from src.integration.train import CandidateChecks, IntegrationTrain, TrainLane, TrainTarget
from src.integration.train_controls import TrainControls
from src.integration.train_sources import (
    RETAINED_CANDIDATE_PREFIX,
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
from tests.test_integration_gitops import LocalGit, commit, inherited_source
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
                    head=None, source_base=None, pr=True, parent_ref=None) -> str:
    """A task with a branch origin and, when *done*, a retained completion.

    Cleanup-only scenarios use ``pr=False`` for sources without a tracked PR.
    """
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
            parent_ref=parent_ref if parent_ref is not None else f"aq/{parent}" if parent else None,
            base_sha=source_base or git(origin.clone, "rev-parse", f"{head}^"),
            creation_generation=0, reserved=True, materialized=True, created_at=time.time(),
        ))
    if done:
        await close(db, tid, [head], origin=origin)
        if parent is None and pr:
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


async def source_ci_binding(world, source_id, repair_id, head, *, history=()):
    from src.database.tables import integration_source_ci

    async with world.db.immediate() as conn:
        await conn.execute(insert(integration_source_ci).values(
            task_id=source_id, repository_id="r",
            source_base=git(world.origin.clone, "rev-parse", f"{head}^"), source_head=head,
            generation=0, policy_generation=0, state="red", evidence={},
            repair_task_id=repair_id, repair_attempt=1,
            repair_history=[{"task_id": tid, "attempt": i} for i, tid in enumerate(history)],
            observed_at=time.time(),
        ))


async def completed_source_ci_repair(world, source, tid="repair"):
    clone = world.origin.clone
    git(clone, "checkout", "-q", "-B", f"aq/{tid}", source)
    (clone / f"{tid}.txt").write_text("CI fixed\n")
    git(clone, "add", ".")
    git(clone, "commit", "-qm", "repair source CI")
    git(clone, "push", "-q", "origin", f"aq/{tid}")
    return await completed(world, tid, head=git(clone, "rev-parse", "HEAD"))


@pytest.mark.parametrize("status", ["READY", "IN_PROGRESS", "PAUSED", "COMPLETED"])
@pytest.mark.parametrize("reopen", ["feedback", "restart", "update"])
async def test_reopen_retires_source_ci_repairs_atomically(world, status, reopen):
    from src.commands.task_commands import TaskCommandsMixin
    from src.database.tables import integration_source_ci
    from src.integration.source_repairs import RETIREMENT_KEY

    db = world.db
    source = await completed(world, "source")
    if status == "COMPLETED":
        await completed_source_ci_repair(world, source)
    else:
        await completed(world, "repair", done=False)
        await db.transition_task("repair", TaskStatus[status], force=True)
    if status == "IN_PROGRESS":
        from src.models import Agent

        await db.create_agent(Agent(id="repair-worker", name="Repair", profile_id="worker"))
        await db.update_task("repair", assigned_agent_id="repair-worker", claim_epoch=1)
    await source_ci_binding(world, "source", "repair", source)
    before = git(world.origin.url, "rev-parse", "main")
    if reopen == "feedback":
        handler = TaskCommandsMixin()
        handler.db, handler._current_scope = db, {}
        assert (await handler._cmd_reopen_with_feedback({
            "task_id": "source", "feedback": "reject this head",
        }))["status"] == "READY"
    elif reopen == "restart":
        await db.transition_task("source", TaskStatus.READY, context="restart_task", force=True)
    else:
        await db.update_task("source", status=TaskStatus.READY)
    repair = await db.get_task("repair")
    assert repair.status is TaskStatus.FAILED
    assert repair.retry_count == repair.max_retries
    assert repair.assigned_agent_id is None
    if status == "IN_PROGRESS":
        from src.database.queries.task_queries import StaleClaim

        with pytest.raises(StaleClaim):
            await db.transition_task("repair", TaskStatus.COMPLETED, expect_claim_epoch=1)
    retirement = await db.get_task_meta("repair", RETIREMENT_KEY)
    assert retirement["disposition"] == "superseded_by_reopen"
    assert retirement["source_head"] == source
    comments = await db.list_task_comments("repair", project_id="p")
    assert any("reopen of source" in comment["body"] for comment in comments["comments"])
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(task_branch_origins.c.retired_at).where(
            task_branch_origins.c.task_id == "repair")) is not None
        assert await conn.scalar(select(integration_source_ci.c.repair_task_id)) == "repair"
    assert await DatabaseBatches(db).pending(MAIN, await snapshot(world)) is None
    assert git(world.origin.url, "rev-parse", "main") == before


async def test_source_ci_retirement_rolls_back_with_reopen(world, monkeypatch):
    source = await completed(world, "source")
    await completed_source_ci_repair(world, source)
    await source_ci_binding(world, "source", "repair", source)
    monkeypatch.setattr(world.db, "add_task_comment", AsyncMock(side_effect=ValueError("audit failed")))
    with pytest.raises(ValueError, match="audit failed"):
        await world.db.transition_task("source", TaskStatus.READY, context="reopen_with_feedback")
    assert (await world.db.get_task("source")).status is TaskStatus.COMPLETED
    assert (await world.db.get_task("repair")).status is TaskStatus.COMPLETED
    assert await world.db.get_task_meta("repair", "source_ci_retirement") is None
    async with world.db._engine.connect() as conn:
        assert await conn.scalar(select(task_branch_origins.c.retired_at).where(
            task_branch_origins.c.task_id == "repair")) is None


async def test_reopen_retires_historical_and_transitive_source_ci_repairs(world):
    db = world.db
    source = await completed(world, "source")
    first = await completed(world, "first")
    await completed(world, "repair", done=False)
    await completed(world, "descendant", done=False)
    await source_ci_binding(world, "source", "repair", source, history=("first",))
    await source_ci_binding(world, "first", "descendant", first)
    await db.transition_task("source", TaskStatus.READY, context="reopen_with_feedback")
    for tid in ("first", "repair", "descendant"):
        assert (await db.get_task(tid)).status is TaskStatus.FAILED
        assert (await db.get_task_meta(tid, "source_ci_retirement"))["reopened_task_id"] == "source"


@pytest.mark.parametrize("stale", ["binding", "checkpoint"])
async def test_stale_source_ci_repair_completion_is_blocked_at_admission_and_publication(world, stale):
    from src.database.tables import integration_source_ci, task_integration_checkpoints

    source = await completed(world, "source")
    repair = await completed_source_ci_repair(world, source)
    await source_ci_binding(world, "source", "repair", source)
    batches = fixture_batches(world.db)
    members, _, _ = await batches.pending(MAIN, await snapshot(world))
    batch = Batch("repair-batch", "p", "r", MAIN.target_ref)
    assert await batches.eligible(batch, members)
    # Simulate stale lineage left by an older daemon, without relying on the
    # new retirement hook. The bound head no longer names this completion.
    async with world.db.immediate() as conn:
        changed = git(world.origin.clone, "rev-parse", f"{source}^")
        if stale == "binding":
            await conn.execute(update(integration_source_ci).values(source_head=changed))
        else:
            await conn.execute(insert(task_integration_checkpoints).values(
                task_id="source", repository_id="r", branch="aq/source", checkpoint_sha=changed,
                updated_at=time.time(),
            ))
    blockers = []
    pending, _, _ = await batches.pending(MAIN, await snapshot(world), blockers=blockers)
    assert {member.task_id for member in pending} == {"source"}
    assert any(blocker["code"] == "source_ci_repair_superseded" and
               blocker["task_id"] == "repair" for blocker in blockers)
    assert not await batches.eligible(batch, (BatchMember("repair", repair, source),))


async def test_current_legacy_leaf_source_ci_repair_remains_eligible(world):
    from src.database.tables import task_integration_checkpoints

    source = await completed(world, "source", done=False)
    await world.db.transition_task("source", TaskStatus.COMPLETED)
    async with world.db.immediate() as conn:
        await conn.execute(insert(task_integration_checkpoints).values(
            task_id="source", repository_id="r", branch="aq/source", checkpoint_sha=source,
            updated_at=time.time(),
        ))
    repair = await completed_source_ci_repair(world, source)
    await source_ci_binding(world, "source", "repair", source)
    assert await fixture_batches(world.db).eligible(
        Batch("legacy-repair", "p", "r", MAIN.target_ref),
        (BatchMember("repair", repair, source),),
    )


@pytest.mark.parametrize("source_delivered", [False, True])
async def test_current_source_ci_repair_still_delivers(world, source_delivered):
    source = await completed(world, "source")
    repair = await completed_source_ci_repair(world, source)
    await source_ci_binding(world, "source", "repair", source)
    if source_delivered:
        world.origin.land("source")
    transport = LocalGit(Path(world.origin.url))
    train, checks, _ = lane(world, transport)
    testing = await train.visit(MAIN)
    assert testing.state == "testing", testing
    checks.green.add(testing.candidate_sha)
    assert (await train.visit(MAIN)).state == "delivered"
    tip = git(world.origin.url, "rev-parse", "main")
    git(world.origin.url, "merge-base", "--is-ancestor", repair, tip)


async def test_reopen_refuses_frozen_source_ci_repair_publication_with_named_blocker(world):
    source = await completed(world, "source", land=True)
    repair = await completed_source_ci_repair(world, source)
    await source_ci_binding(world, "source", "repair", source)
    # Match an independently batched repair: sealing the source itself would
    # already forbid its reopen through the existing subtree mutation guard.
    await BatchStore(world.db).freeze(
        Batch("repair-only", "p", "r", MAIN.target_ref),
        (BatchMember("repair", repair, source),), trees={"repair": tree(world, repair)},
    )
    train, checks, _ = lane(world, LocalGit(Path(world.origin.url)))
    testing = await train.visit(MAIN)
    assert testing.state == "testing", testing
    checks.green.add(testing.candidate_sha)
    before = git(world.origin.url, "rev-parse", "main")

    await world.db.transition_task("source", TaskStatus.READY, context="reopen_with_feedback")
    refused = await train.visit(MAIN)
    assert refused.state == "held", refused
    assert any(blocker["code"] == "source_ci_repair_superseded" and
               blocker["task_id"] == "repair" for blocker in refused.detail["blockers"])
    assert git(world.origin.url, "rev-parse", "main") == before


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


async def promotion_flow(world, targets):
    flow = []
    source = "dev"
    for i, target in enumerate(targets):
        flow.append({
            "id": f"step-{i}", "source": source, "target": target, "type": "request",
            "gate": {"approval": "operator", "checks": "inherit",
                     "attestation": f"Promotion {i}"},
            "versioning": {"kind": "none"}, "notes": {"kind": "none"}, "after": {},
        })
        source = target
    async with world.db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p")
                           .values(promotion_flow=flow))
        await conn.execute(update(repos).where(repos.c.id == "r").values(default_branch="dev"))
    return flow


@pytest.mark.parametrize("refs", [("main",), ("staging", "main")])
async def test_flow_enumerates_promotion_targets_without_a_frontier(world, refs):
    flow = await promotion_flow(world, refs)
    await completed(world, "ordinary")
    targets = await DatabaseTargets(world.db).targets(time.time())
    assert {(t.target_ref, t.kind) for t in targets} == {
        ("refs/heads/dev", "root"), *((f"refs/heads/{ref}", "promotion") for ref in refs),
    }
    promotions = [target for target in targets if target.kind == "promotion"]
    assert {target.step_id for target in promotions} == {step["id"] for step in flow}
    batches = fixture_batches(world.db)
    service = SimpleNamespace(store=BatchStore(world.db), freeze=AsyncMock())
    for target in promotions:
        assert target.step == next(step for step in flow if step["target"] ==
                                   target.target_ref.removeprefix("refs/heads/"))
        observed = SimpleNamespace(error=None, target_oid="a" * 40)
        assert await batches.pending(target, observed) is None
        assert (await batches.open_batch(target, observed, service)).batch is None
    service.freeze.assert_not_awaited()


async def test_promotion_current_ignores_other_triggers_and_other_refs(world):
    await promotion_flow(world, ("staging", "main"))
    head = await completed(world, "source")
    for bid, ref, trigger, created in (
        ("old-root", "main", "manual", 1.0),
        ("staging", "staging", "promotion", 2.0),
        ("release", "main", "promotion", 3.0),
    ):
        async with world.db.immediate() as conn:
            await conn.execute(insert(integration_batches).values(
                id=bid, project_id="p", repository_id="r", target_ref=f"refs/heads/{ref}",
                request_id=bid, trigger=trigger, source_manifest_digest="sha256:" + "1" * 64,
                base_sha=head, integration_branch=candidate_ref(bid), lifecycle="sealed",
                policy_snapshot={}, artifact_snapshot={}, cleanup_state="pending",
                created_at=created, updated_at=created,
            ))
    targets = await DatabaseTargets(world.db).targets(time.time())
    assert len(targets) == 3
    batches = fixture_batches(world.db)
    for target in targets:
        current = await batches.current(target)
        if target.kind == "promotion":
            assert current.id == ("release" if target.step_id == "step-1" else "staging")
        else:
            assert current is None


@pytest.mark.parametrize("identity", ["intent", "hotfix", "qualified-hotfix", "promotion-target"])
async def test_ordinary_eligibility_and_frontier_refuse_promotion_routing(world, identity):
    await promotion_flow(world, ("main",))
    parent_ref = {"hotfix": "main", "qualified-hotfix": "refs/heads/main"}.get(identity)
    head = await completed(world, "promotion-work", parent_ref=parent_ref)
    async with world.db.immediate() as conn:
        if identity == "intent":
            await conn.execute(update(tasks).where(tasks.c.id == "promotion-work")
                               .values(task_type="promotion"))
    target = "main" if identity == "promotion-target" else "dev"
    batches = fixture_batches(world.db)
    batch = Batch("ordinary", "p", "r", f"refs/heads/{target}")
    assert not await batches.eligible(batch, (BatchMember("promotion-work", head, head),))
    if identity != "promotion-target":
        async with world.db._engine.connect() as conn:
            assert await _pending_tasks(conn, "p", "r", limit=None) == []


@pytest.mark.parametrize("conflicting_pr", [False, True])
async def test_pending_members_are_exact_undelivered_sources(world, conflicting_pr):
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

    batches = fixture_batches(db)
    if conflicting_pr:
        train, github, _ = await hosted_train(world)
        github.pr_runs.clear()
        github.mergeability.update({url: "dirty" for url in github.pulls})
        batches = train.batches
    members, requests, dependencies = await batches.pending(
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


async def intermediate_stack(world, *, declared=False, prerequisite_done=False, final_base=False):
    """P forks Q1 while Q goes on to Q2; both origins name their actual bases."""
    origin = world.origin
    base = git(origin.clone, "rev-parse", "origin/main")
    first = await completed(world, "q", done=False)
    final = origin.work("q", "later")
    if prerequisite_done:
        await close(world.db, "q", [first, final], origin=origin)
    git(origin.clone, "checkout", "-q", "-B", "aq/p", final if final_base else first)
    source = commit(origin.clone, {"p-work.txt": "P\n"})
    git(origin.clone, "push", "-q", "origin", "aq/p")
    await completed(world, "p", head=source, needs=("q",) if declared else ())
    return base, first, final, source


async def test_undeclared_intermediate_stack_blocks_the_real_git_content_loss(world):
    base, first, final, source = await intermediate_stack(world)
    train, checks, repo = lane(world, LocalGit(Path(world.origin.url)), regenerate=None)
    ops = (await train.lane_for(MAIN)).service.gitops

    # Reproduce the old collection: only P's delta lands, but its ancestry
    # imports Q1. Q's later target sync then silently deletes Q1's file.
    poisoned = await ops.merge_sources(
        repo, base, (BatchMember("p", source, first),), created_at=1.0,
        regenerate_generated=False,
    )
    assert poisoned["outcome"] == "merged"
    target = poisoned["head"]
    clone = world.origin.clone
    git(clone, "merge-base", "--is-ancestor", first, target)
    assert "q-work.txt" not in git(clone, "ls-tree", "--name-only", target).splitlines()
    assert "p-work.txt" in git(clone, "ls-tree", "--name-only", target).splitlines()
    git(clone, "checkout", "-q", "-B", "lost-q", final)
    git(clone, "merge", "-q", "--no-ff", "-m", "sync target", target)
    assert not (clone / "q-work.txt").exists()
    assert (clone / "q-later.txt").exists()
    assert "q-work.txt" not in git(clone, "diff", "--name-only", base, "HEAD").splitlines()

    # The actual visit refuses P before freezing or touching the target.
    blocked = await train.visit(MAIN)
    assert blocked.state == "blocked", blocked
    assert blocked.batch_id is None and blocked.repair is None
    [blocker] = blocked.detail["blockers"]
    assert blocker["code"] == "undeclared_stack_base"
    assert (blocker["task_id"], blocker["prerequisite_task_id"]) == ("p", "q")
    assert (blocker["source_base_sha"], blocker["prerequisite_head_sha"]) == (first, final)
    assert git(world.origin.url, "rev-parse", "refs/heads/main") == base

    # Withhold declared dependents of P, while independent work still lands.
    await completed(world, "dependent", needs=("p",))
    independent = await completed(world, "independent")
    testing = await train.visit(MAIN)
    assert testing.state == "testing", testing
    members = await BatchStore(world.db).members(testing.batch_id)
    assert [member.task_id for member in members] == ["independent"]
    checks.green.add(testing.candidate_sha)
    assert (await train.visit(MAIN)).state == "delivered"
    git(world.origin.url, "merge-base", "--is-ancestor", independent,
        git(world.origin.url, "rev-parse", "refs/heads/main"))


@pytest.mark.parametrize("final_base", [False, True])
async def test_declared_stack_merges_prerequisite_first_and_preserves_content(world, final_base):
    base, first, final, source = await intermediate_stack(
        world, declared=True, prerequisite_done=True, final_base=final_base,
    )
    train, checks, _ = lane(world, LocalGit(Path(world.origin.url)), regenerate=None)
    testing = await train.visit(MAIN)
    assert testing.state == "testing" and not (testing.detail or {}).get("blockers"), testing
    members = await BatchStore(world.db).members(testing.batch_id)
    assert [member.task_id for member in members] == ["q", "p"]
    candidate = testing.candidate_sha
    clone = world.origin.clone
    for name in ("q-work.txt", "q-later.txt", "p-work.txt"):
        assert name in git(clone, "ls-tree", "--name-only", candidate).splitlines()
    for head in (first, final, source):
        git(clone, "merge-base", "--is-ancestor", head, candidate)
    checks.green.add(candidate)
    assert (await train.visit(MAIN)).state == "delivered"
    git(clone, "checkout", "-q", "-B", "synced-q", final)
    git(clone, "merge", "-q", "--no-ff", "-m", "safe target sync", candidate)
    assert (clone / "q-work.txt").read_text() == "work\n"
    assert "q-work.txt" in git(clone, "diff", "--name-only", base, "HEAD").splitlines()


async def test_declared_intermediate_stack_waits_for_unfinished_prerequisite(world):
    base, _, _, _ = await intermediate_stack(world, declared=True)
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)), regenerate=None)
    blocked = await train.visit(MAIN)
    assert blocked.state == "blocked" and blocked.batch_id is None, blocked
    assert [blocker["code"] for blocker in blocked.detail["blockers"]] == [
        "stack_prerequisite_pending",
    ]
    assert git(world.origin.url, "rev-parse", "refs/heads/main") == base


async def test_transitive_blocks_prerequisite_authorizes_the_stack(world):
    await intermediate_stack(world, prerequisite_done=True)
    await completed(world, "middle", needs=("q",))
    await world.db.add_dependency("p", "middle")
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)), regenerate=None)
    testing = await train.visit(MAIN)
    assert testing.state == "testing" and not (testing.detail or {}).get("blockers"), testing
    assert [m.task_id for m in await BatchStore(world.db).members(testing.batch_id)] == [
        "q", "middle", "p",
    ]
    assert "q-work.txt" in git(
        world.origin.clone, "ls-tree", "--name-only", testing.candidate_sha,
    ).splitlines()


@pytest.mark.parametrize("limit", [200, 1])
@pytest.mark.parametrize("declared", [False, True])
async def test_legacy_frozen_intermediate_stack_is_blocked_before_construction(world, limit, declared):
    base, first, _, source = await intermediate_stack(
        world, declared=declared, prerequisite_done=declared,
    )
    store = BatchStore(world.db)
    batch = Batch("unsafe-legacy", "p", "r", MAIN.target_ref)
    await store.freeze(batch, (BatchMember("p", source, first),), trees={"p": tree(world, source)})
    transport = LocalGit(Path(world.origin.url))
    train, _, _ = lane(world, transport, regenerate=None)
    train.batches.limit = limit
    await completed(world, "newest")
    blocked = await train.visit(MAIN)
    assert blocked.state == "blocked" and blocked.repair is None, blocked
    assert blocked.detail["blockers"][0]["code"] == (
        "stack_prerequisite_pending" if declared else "undeclared_stack_base"
    )
    assert blocked.detail["blockers"][0]["batch_id"] == batch.id
    assert transport.pushes == 0
    assert (await store.get(batch.id)).intent == "open"
    assert git(world.origin.url, "rev-parse", "refs/heads/main") == base


@pytest.mark.parametrize("probe", ["base_ancestor", "foreign_history"])
async def test_unavailable_stack_probe_withholds_source_and_recovers(world, monkeypatch, probe):
    base, first, _, _ = await intermediate_stack(
        world, declared=True, prerequisite_done=True,
    )
    observed = await snapshot(world)
    transport = observed.observation.git
    ancestor, run = transport.ais_ancestor, transport.arun_git_result

    async def unknown_ancestor(store, older, newer, **kwargs):
        if (older, newer) == (first, base):
            return None
        return await ancestor(store, older, newer, **kwargs)

    async def unavailable_history(args, **kwargs):
        if args[1:3] == ["for-each-ref", "--format=%(refname)"]:
            return SimpleNamespace(returncode=128, stdout="", stderr="untrusted private detail")
        return await run(args, **kwargs)

    if probe == "base_ancestor":
        monkeypatch.setattr(transport, "ais_ancestor", unknown_ancestor)
    else:
        monkeypatch.setattr(transport, "arun_git_result", unavailable_history)
    blockers = []
    batches = fixture_batches(world.db)
    members, _, _ = await batches.pending(MAIN, observed, blockers=blockers)
    assert [m.task_id for m in members] == ["q"]
    assert [b["code"] for b in blockers] == ["stack_ancestry_unknown"]
    assert "private detail" not in str(blockers)
    monkeypatch.setattr(transport, "ais_ancestor", ancestor)
    monkeypatch.setattr(transport, "arun_git_result", run)
    blockers = []
    members, _, _ = await batches.pending(MAIN, observed, blockers=blockers)
    assert {m.task_id for m in members} == {"p", "q"} and not blockers


def counted_scan(observed, monkeypatch):
    """Record every Git call this observation makes, keyed by its arguments."""
    calls = []
    transport = observed.observation.git
    run, ancestor = transport.arun_git_result, transport.ais_ancestor

    async def counted_run(args, **kwargs):
        calls.append(("git", tuple(args)))
        return await run(args, **kwargs)

    async def counted_ancestor(store, older, newer, **kwargs):
        calls.append(("ancestor", (older, newer)))
        return await ancestor(store, older, newer, **kwargs)

    monkeypatch.setattr(transport, "arun_git_result", counted_run)
    monkeypatch.setattr(transport, "ais_ancestor", counted_ancestor)
    return calls


def containment_queries(calls):
    return [call for call in calls if call[0] == "git" and "--contains" in call[1]]


async def test_stacked_base_scan_costs_no_call_per_unrelated_origin(world, monkeypatch):
    """One containment query per off-target base, not one walk per live origin.

    Origins persist until they are archived, so a repository with hundreds of
    them made every visit replay one rev-list each -- measured at ~11.6 s across
    this project's ~900 branches -- while any member waited on a stack.
    """
    _, _, _, _ = await intermediate_stack(world, declared=True, prerequisite_done=True)

    async def scan(batches):
        # A fresh truth per scan, so GitTruth's own pair cache cannot make the
        # second visit look cheaper than the first for unrelated reasons.
        observed = await GitTruth(GitManager()).snapshot(
            str(world.origin.clone), project_id="p", repository_id="r",
            repository_url=world.origin.url, target_ref=MAIN.target_ref,
        )
        calls = counted_scan(observed, monkeypatch)
        blockers = []
        members, _, _ = await batches.pending(MAIN, observed, blockers=blockers)
        assert [m.task_id for m in members] == ["p", "q"] and not blockers, blockers
        return calls

    few = await scan(fixture_batches(world.db))
    unrelated = [
        await completed(world, f"unrelated-{index}", done=False) for index in range(8)
    ]
    many = await scan(fixture_batches(world.db))
    assert len(many) == len(few)
    assert len(containment_queries(many)) == 1
    assert not [call for call in many if any(oid in str(call) for oid in unrelated)]

    # The same instance re-reads that answer while the refs and target stand,
    # which is what a visit waiting on a stacked member used to repeat.
    batches = fixture_batches(world.db)
    assert len(containment_queries(await scan(batches))) == 1
    assert not containment_queries(await scan(batches))


async def test_unreadable_unrelated_origin_does_not_withhold_a_stacked_member(
    world, monkeypatch,
):
    """Fail-closed stays scoped to the ancestry a member actually needs.

    An unrelated branch that Git cannot read withheld *every* stacked member
    with stack_ancestry_unknown, naming a task whose history had nothing to do
    with the source base under examination.
    """
    _, _, _, _ = await intermediate_stack(world, declared=True, prerequisite_done=True)
    unrelated = await completed(world, "unrelated", done=False)
    observed = await snapshot(world)
    transport = observed.observation.git
    run = transport.arun_git_result

    async def unreadable_origin(args, **kwargs):
        if any(unrelated in argument for argument in args):
            return SimpleNamespace(returncode=128, stdout="", stderr="untrusted private detail")
        return await run(args, **kwargs)

    monkeypatch.setattr(transport, "arun_git_result", unreadable_origin)
    blockers = []
    members, _, _ = await fixture_batches(world.db).pending(
        MAIN, observed, blockers=blockers,
    )
    assert [m.task_id for m in members] == ["p", "q"] and not blockers, blockers


async def sibling_of_prerequisite(world, base, head):
    """A live sibling branch that merged the prerequisite's own branch.

    Its id sorts before the prerequisite's, so an origin-at-a-time scan met it
    first and reported the sibling's unpublished work as P's blocker.
    """
    origin = world.origin
    git(origin.clone, "checkout", "-q", "-B", "aq/a-sibling", head)
    sibling = commit(origin.clone, {"sibling.txt": "sibling\n"})
    git(origin.clone, "push", "-q", "origin", "aq/a-sibling")
    await completed(world, "a-sibling", head=sibling, source_base=base, done=False)


async def test_sibling_that_merged_the_prerequisite_is_not_the_named_blocker(world):
    base, _, final, _ = await intermediate_stack(
        world, declared=True, prerequisite_done=True, final_base=True,
    )
    await sibling_of_prerequisite(world, base, final)
    train, checks, _ = lane(world, LocalGit(Path(world.origin.url)), regenerate=None)

    testing = await train.visit(MAIN)
    assert testing.state == "testing" and not (testing.detail or {}).get("blockers"), testing
    assert [m.task_id for m in await BatchStore(world.db).members(testing.batch_id)] == ["q", "p"]
    candidate = testing.candidate_sha
    clone = world.origin.clone
    for name in ("q-work.txt", "q-later.txt", "p-work.txt"):
        assert name in git(clone, "ls-tree", "--name-only", candidate).splitlines()
    checks.green.add(candidate)
    assert (await train.visit(MAIN)).state == "delivered"
    main = git(world.origin.url, "rev-parse", "refs/heads/main")
    git(clone, "merge-base", "--is-ancestor", final, main)
    # The sibling is unfinished work, not a member: naming it changed nothing.
    assert "sibling.txt" not in git(clone, "ls-tree", "--name-only", main).splitlines()


async def source_ci_repair_of(world, source_head):
    """The delegate the daemon files for one red source head, on that head."""
    origin = world.origin
    git(origin.clone, "checkout", "-q", "-B", "aq/repair", source_head)
    head = commit(origin.clone, {"repair.txt": "repair\n"})
    git(origin.clone, "push", "-q", "origin", "aq/repair")
    await completed(world, "repair", head=head, source_base=source_head)
    return head


async def bind_source_ci_repair(world, source, repair, *, source_base, source_head):
    async with world.db._engine.begin() as conn:
        await conn.execute(insert(integration_source_ci).values(
            task_id=source, repository_id="r", source_base=source_base,
            source_head=source_head, generation=0, policy_generation=0, state="red",
            evidence={"checks": [], "failing_checks": []}, repair_task_id=repair,
            observed_at=time.time(),
        ))


@pytest.mark.parametrize("bound", [False, True])
async def test_source_ci_repair_lands_with_its_source_in_one_batch(world, bound):
    """A repair's base is its bound source's head: a declared stack, not a loss.

    Its source cannot go green until the repair lands, so withholding the repair
    as an undeclared stack deadlocks the pair -- and a PR gate that requires the
    source's own checks green turns that into a stall nobody can clear.
    """
    origin = world.origin
    base = git(origin.clone, "rev-parse", "origin/main")
    source = await completed(world, "source")
    repair = await source_ci_repair_of(world, source)
    if bound:
        await bind_source_ci_repair(world, "source", "repair",
                                    source_base=base, source_head=source)
    train, checks, _ = lane(world, LocalGit(Path(world.origin.url)), regenerate=None)

    outcome = await train.visit(MAIN)
    if not bound:
        # The refusal the binding exists to lift: an unbound stack of exactly
        # this shape is still withheld, and the independent source still lands.
        assert outcome.state == "testing", outcome
        members = await BatchStore(world.db).members(outcome.batch_id)
        assert [member.task_id for member in members] == ["source"]
        [blocker] = outcome.detail["blockers"]
        assert blocker["code"] == "undeclared_stack_base"
        assert (blocker["task_id"], blocker["prerequisite_task_id"]) == ("repair", "source")
        assert git(origin.url, "rev-parse", "refs/heads/main") == base
        return
    assert outcome.state == "testing" and not (outcome.detail or {}).get("blockers"), outcome
    members = await BatchStore(world.db).members(outcome.batch_id)
    assert [member.task_id for member in members] == ["source", "repair"]
    assert [member.source_base_sha for member in members] == [base, source]
    candidate = outcome.candidate_sha
    clone = origin.clone
    for name in ("source-work.txt", "repair.txt"):
        assert name in git(clone, "ls-tree", "--name-only", candidate).splitlines()
    for head in (source, repair):
        git(clone, "merge-base", "--is-ancestor", head, candidate)
    checks.green.add(candidate)
    assert (await train.visit(MAIN)).state == "delivered"
    git(clone, "merge-base", "--is-ancestor", repair,
        git(origin.url, "rev-parse", "refs/heads/main"))


async def test_source_ci_binding_never_contradicts_a_declared_order(world):
    """A binding whose source already waits on the repair is left to the guard.

    Adding it anyway would order the pair both ways and freeze nothing at all.
    """
    origin = world.origin
    base = git(origin.clone, "rev-parse", "origin/main")
    source = await completed(world, "source")
    await source_ci_repair_of(world, source)
    await bind_source_ci_repair(world, "source", "repair",
                                source_base=base, source_head=source)
    await world.db.add_dependency("source", "repair")
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)), regenerate=None)

    outcome = await train.visit(MAIN)
    assert outcome.state == "blocked" and outcome.batch_id is None, outcome
    [blocker] = outcome.detail["blockers"]
    assert (blocker["code"], blocker["task_id"]) == ("undeclared_stack_base", "repair")
    assert git(origin.url, "rev-parse", "refs/heads/main") == base


@pytest.mark.parametrize("dep_type", ["related", "discovered-from", "waits-for",
                                     "conditional-blocks"])
async def test_other_edges_do_not_authorize_a_stack_and_delivery_clears_blocker(world, dep_type):
    _, _, final, _ = await intermediate_stack(world, prerequisite_done=True)
    await world.db.add_dependency("p", "q", dep_type)
    train, checks, _ = lane(world, LocalGit(Path(world.origin.url)), regenerate=None)
    testing = await train.visit(MAIN)
    assert testing.state == "testing", testing
    assert [m.task_id for m in await BatchStore(world.db).members(testing.batch_id)] == ["q"]
    assert [b["code"] for b in testing.detail["blockers"]] == ["undeclared_stack_base"]
    checks.green.add(testing.candidate_sha)
    assert (await train.visit(MAIN)).state == "delivered"
    git(world.origin.url, "merge-base", "--is-ancestor", final,
        git(world.origin.url, "rev-parse", "refs/heads/main"))
    # Once the base is really in the target, P can land without an edge.
    next_visit = await train.visit(MAIN)
    assert next_visit.state == "testing" and not (next_visit.detail or {}).get("blockers")
    assert [m.task_id for m in await BatchStore(world.db).members(next_visit.batch_id)] == ["p"]
async def test_open_reuses_one_candidate_scan_and_filters_before_root_proof(world, monkeypatch):
    from src.integration import train_sources

    await world.db.create_task(Task(id="epic", project_id="p", repo_id="r", title="epic",
                                   description="",
                                   branch_name="aq/epic", status=TaskStatus.IN_PROGRESS))
    git(world.origin.clone, "push", "-q", "origin", "main:aq/epic")
    for index in range(6):
        await completed(world, f"child-{index}", parent="epic")
    await completed(world, "top")
    batches = DatabaseBatches(world.db, pr_gate=AsyncMock(return_value={
        "code": "awaiting_pr_checks", "task_id": "top", "ref": "top"}))
    scan = AsyncMock(wraps=train_sources._pending_tasks)
    monkeypatch.setattr(train_sources, "_pending_tasks", scan)
    proof = AsyncMock(wraps=batches.delivered)
    monkeypatch.setattr(batches, "delivered", proof)
    service = SimpleNamespace(store=BatchStore(world.db), gitops=None, freeze=AsyncMock())
    for _ in range(2):  # A subsequent visit scans again; there is no mutable identity cache.
        metrics = SelectionMetrics()
        observed = await snapshot(world)
        with selection_metrics_scope(metrics):
            selection = await batches.open_batch(MAIN, observed, service)
        assert selection.batch is None and selection.blockers
        assert metrics.counts["candidate_scans"] == 1
        assert metrics.counts["candidate_window_reuses"] == 1
        assert metrics.counts["repository_ids"] == 7
        assert metrics.counts["routed_ids"] == 1
        assert metrics.as_dict()["stages"]["root_delivery"]["items"] == 1
        assert proof.call_args.args[2] == ["top"]
    assert scan.await_count == proof.await_count == 2
    service.freeze.assert_not_awaited()


async def test_candidate_limit_applies_after_root_delivery_and_rechecks_new_work(world):
    await completed(world, "owed")
    await completed(world, "recent-landed", land=True)
    async with world.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "recent-landed")
                           .values(updated_at=time.time() + 100))
    batches = fixture_batches(world.db, limit=1)
    assert await batches._candidate_ids(MAIN, await snapshot(world)) == ["owed"]
    # Newly completed work and a new pinned view must be visible on the next scan.
    await completed(world, "new")
    async with world.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "new")
                           .values(updated_at=time.time() + 200))
    assert await batches._candidate_ids(MAIN, await snapshot(world)) == ["new"]


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
        pause = await handler._cmd_integration_pause_batch({"batch_id": "batch"})
        resume = await handler._cmd_integration_resume_batch({"batch_id": "batch"})
        eject = await handler._cmd_integration_eject({"batch_id": "batch", "task_id": "a"})
        seal = await handler._cmd_integration_seal_now({"project_id": "p"})
        refresh = await handler._cmd_integration_refresh_epic({"task_id": "a"})
    assert abort["outcome"] == retire["outcome"] == refresh["outcome"] == "unauthorized"
    assert {r["outcome"] for r in (pause, resume, eject, seal)} == {"unauthorized"}
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


class TrainCleanupForge:
    """Only the GitHub comment boundary is substituted; Git proof stays real."""

    def __init__(self, heads=()):
        self.prs = {number: {"repository_numeric_id": 123, "repository_full_name": "test/repo",
                             "head_sha": head, "state": "closed"} for number, head in heads}
        self.comments = []
        self.markers = set()

    async def exact_pull_request(self, *, number):
        return self.prs.get(number)

    async def has_comment_marker(self, *, number, marker):
        return (number, marker) in self.markers

    async def comment_pull_request(self, *, number, marker, body):
        self.comments.append((number, body))
        self.markers.add((number, marker))

    async def close_pull_request(self, *, number):
        raise AssertionError("GitHub owns the merged state of train PRs")


def train_cleanup(world, tmp_path, *, forge=None, transport=None, clock=time.time):
    return IntegrationCleanupService(
        world.db, data_dir=tmp_path, git_manager=transport or LocalGit(Path(world.origin.url)),
        forge_provider=forge,
        binding_resolver=AsyncMock(return_value=GitHubRepositoryBinding(123, "test/repo")),
        candidate_store=AsyncMock(return_value=world.origin.clone), clock=clock,
    )


async def freeze_cleanup_batch(world, batch_id, sources, *, target=MAIN.target_ref):
    batch = Batch(batch_id, "p", "r", target)
    members = tuple(BatchMember(tid, head, git(world.origin.clone, "rev-parse", f"{head}^"), n)
                    for n, (tid, head) in enumerate(sources.items()))
    await BatchStore(world.db).freeze(batch, members,
                                    trees={m.task_id: tree(world, m.source_sha) for m in members})
    return batch


async def publish_cleanup_candidate(world, batch, head, *, cleanup=None):
    git(world.origin.clone, "push", "origin", f"{head}:{candidate_ref(batch.id)}")
    git(world.origin.clone, "update-ref", RETAINED_CANDIDATE_PREFIX + batch.id, head)
    await DatabaseBatches(world.db, cleanup=cleanup).settle(
        batch, BatchObservation("delivered", candidate_sha=head, target_sha=head),
    )


async def test_promoted_train_cleans_refs_and_comments_only_repair_commits(world, tmp_path):
    """Fidelity §6 tests 9 and 3: exact promotion cleanup and a reviewer-visible fix."""
    db, origin = world.db, world.origin
    sources = {tid: await completed(world, tid) for tid in ("a", "b")}
    for number, tid in enumerate(sources, 1):
        origin.land(tid)
        await db.update_task(tid, pr_url=f"https://github.com/test/repo/pull/{number}")
    start = git(origin.clone, "rev-parse", "HEAD")
    batch = await freeze_cleanup_batch(world, "train-clean", sources)
    git(origin.clone, "push", "origin", f"{start}:{candidate_ref(batch.id)}")
    repair = await OrdinaryRepairService(db).allocate(
        batch.id, target_ref=candidate_ref(batch.id), head_sha=start, brief="fix the candidate",
        authorize=AsyncMock(return_value=True),
    )
    fix = commit(origin.clone, {"fix.txt": "repair\n"})
    git(origin.clone, "push", "origin", f"{fix}:refs/heads/main")
    await close(db, repair["task_id"], [fix], origin=origin)
    locks = BranchLock(db)
    owner = await locks.get(BranchKey(repository_id="r", branch=candidate_ref(batch.id)))
    await locks.release(owner.grant())
    forge = TrainCleanupForge(enumerate(sources.values(), 1))
    cleanup = train_cleanup(world, tmp_path, forge=forge)
    await publish_cleanup_candidate(world, batch, fix, cleanup=cleanup)
    from src.database.tables import integration_cleanup_items

    async with db._engine.connect() as conn:
        assert len((await conn.execute(select(integration_cleanup_items))).all()) == 6
    results = await cleanup.advance(batch.id)
    async with db._engine.connect() as conn:
        errors = (await conn.execute(select(integration_cleanup_items.c.identity,
                                           integration_cleanup_items.c.last_error))).all()
    assert {r.outcome for r in results} == {"complete"}, errors
    assert (await db.get_integration_batch(batch.id))["cleanup_state"] == "complete"
    for tid in sources:
        assert not git(origin.url, "for-each-ref", "--format=%(refname)", f"refs/heads/aq/{tid}")
    assert not git(origin.url, "for-each-ref", "--format=%(refname)", candidate_ref(batch.id))
    assert not git(origin.clone, "for-each-ref", "--format=%(refname)",
                   RETAINED_CANDIDATE_PREFIX + batch.id)
    assert len(forge.comments) == 2
    for _, body in forge.comments:
        assert fix in body and "Integration repair commits" in body
        repairs = body.split("Integration repair commits", 1)[1]
        assert f"- `{fix}`" in repairs
        assert all(head not in repairs for head in sources.values())
    assert await cleanup.advance(batch.id) == []
    assert len(forge.comments) == 2


async def test_promoted_train_bundles_rewritten_member_without_deleting_it(world, tmp_path):
    head = await completed(world, "a", land=True, pr=False)
    batch = await freeze_cleanup_batch(world, "train-rewritten", {"a": head})
    promoted = git(world.origin.url, "rev-parse", "refs/heads/main")
    await publish_cleanup_candidate(world, batch, promoted)
    rewritten = world.origin.work("a", name="rewritten")
    cleanup = train_cleanup(world, tmp_path)
    results = await cleanup.advance(batch.id)
    assert {r.outcome for r in results} == {"complete", "conflict"}
    assert git(world.origin.url, "rev-parse", "refs/heads/aq/a") == rewritten
    [bundle] = (tmp_path / "branch-backups").rglob("*.bundle")
    assert rewritten in git(world.origin.clone, "bundle", "list-heads", str(bundle))
    git(world.origin.clone, "bundle", "verify", str(bundle))
    assert (await world.db.get_integration_batch(batch.id))["cleanup_state"] == "conflict"


async def test_first_advance_backfills_all_pending_promoted_train_batches(world, tmp_path):
    sources = {tid: await completed(world, tid, land=True, pr=False) for tid in "abcdefghijklmnopqrs"}
    promoted = git(world.origin.url, "rev-parse", "refs/heads/main")
    for tid, head in sources.items():
        # Before this change, freeze left both source identity fields null.
        async with world.db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == tid).values(branch_name=None))
        batch = await freeze_cleanup_batch(world, "train-backfill-" + tid, {tid: head})
        async with world.db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == tid)
                               .values(branch_name=f"aq/{tid}"))
        await publish_cleanup_candidate(world, batch, promoted)
    cleanup = train_cleanup(world, tmp_path)
    await cleanup.advance("train-backfill-a")
    from src.database.tables import integration_cleanup_items

    async with world.db._engine.connect() as conn:
        items = (await conn.execute(select(integration_cleanup_items))).mappings().all()
    assert {row["batch_id"] for row in items} == {"train-backfill-" + tid for tid in sources}
    assert len(items) == 57
    assert git(world.origin.url, "rev-parse", "refs/heads/aq/b") == sources["b"]
    await cleanup.reconcile(time.time())
    batches = [await world.db.get_integration_batch("train-backfill-" + tid) for tid in sources]
    assert all(batch["cleanup_state"] == "complete" for batch in batches)


async def test_promoted_train_cleanup_honors_retention_and_live_epic_target(world, tmp_path):
    head = await completed(world, "epic", done=False, pr=False)
    world.origin.land("epic")
    batch = await freeze_cleanup_batch(world, "train-open-epic", {"epic": head})
    promoted = git(world.origin.url, "rev-parse", "refs/heads/main")
    await publish_cleanup_candidate(world, batch, promoted)
    cleanup = train_cleanup(world, tmp_path)
    results = await cleanup.advance(batch.id)
    assert "retryable" in {r.outcome for r in results}
    assert git(world.origin.url, "rev-parse", "refs/heads/aq/epic") == head
    assert (await world.db.get_integration_batch(batch.id))["cleanup_state"] == "pending"

    # Epic delivery refs retire after settlement even when the generic
    # successful-source policy retains ordinary task branches.
    await world.db.transition_task("epic", TaskStatus.COMPLETED)
    async with world.db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_policy={"cleanup": {"successful_source_refs": "retain"}},
        ))
    retained = await freeze_cleanup_batch(world, "train-retained", {"epic": head})
    await publish_cleanup_candidate(world, retained, promoted, cleanup=cleanup)
    assert {r.outcome for r in await cleanup.advance(retained.id)} == {"complete"}
    assert not git(world.origin.url, "for-each-ref", "--format=%(refname)",
                   "refs/heads/aq/epic")


async def test_epic_refresh_cleanup_never_schedules_its_own_epic_target(world, tmp_path):
    """A refresh's only member is the epic itself, frozen at the default head.

    Its branch is the batch target and its PR is not this delivery, so cleanup
    retires only the private candidate refs and settles complete, not conflict.
    """
    from src.database.tables import integration_cleanup_items

    db, origin = world.db, world.origin
    epic = await completed(world, "epic", done=False, pr=False)
    await db.update_task("epic", pr_url="https://github.com/test/repo/pull/7")
    git(origin.clone, "checkout", "-q", "-B", "main", "origin/main")
    default = commit(origin.clone, {"default.txt": "default moved\n"})
    git(origin.clone, "push", "-q", "origin", "main")
    target = "refs/heads/aq/epic"
    batch = await freeze_cleanup_batch(world, "train-epic-refresh-" + "0" * 64,
                                       {"epic": default}, target=target)
    git(origin.clone, "checkout", "-q", "-B", "aq/epic", "origin/aq/epic")
    git(origin.clone, "merge", "-q", "--no-ff", "-m", "refresh epic", default)
    git(origin.clone, "push", "-q", "origin", "aq/epic")
    refreshed = git(origin.clone, "rev-parse", "HEAD")
    forge = TrainCleanupForge([(7, epic)])
    cleanup = train_cleanup(world, tmp_path, forge=forge)
    await publish_cleanup_candidate(world, batch, refreshed, cleanup=cleanup)
    async with db._engine.connect() as conn:
        items = (await conn.execute(select(
            integration_cleanup_items.c.kind, integration_cleanup_items.c.target_ref,
        ).where(integration_cleanup_items.c.batch_id == batch.id))).all()
    assert sorted(tuple(row) for row in items) == sorted([
        ("local_ref", RETAINED_CANDIDATE_PREFIX + batch.id),
        ("remote_ref", candidate_ref(batch.id)),
    ])
    assert {r.outcome for r in await cleanup.advance(batch.id)} == {"complete"}
    assert (await db.get_integration_batch(batch.id))["cleanup_state"] == "complete"
    assert git(origin.url, "rev-parse", target) == refreshed
    assert forge.comments == []


async def test_promoted_train_cleanup_protects_open_subject_target(world, tmp_path):
    from src.database.tables import integration_subjects, playbook_artifacts
    from src.integration.runtime_contracts import (
        PolicyArtifactPin,
        Subject,
        SubjectKind,
        SubjectPhase,
        SubjectSchedule,
    )

    head = await completed(world, "epic", land=True, pr=False)
    batch = await freeze_cleanup_batch(world, "train-subject-target", {"epic": head})
    promoted = git(world.origin.url, "rev-parse", "main")
    await publish_cleanup_candidate(world, batch, promoted)
    clock = [time.time()]
    pin = PolicyArtifactPin(playbook_id="test", artifact_sha256="sha256:" + "1" * 64)
    async with world.db.immediate() as conn:
        await conn.execute(insert(playbook_artifacts).values(
            artifact_sha256=pin.artifact_sha256, playbook_id=pin.playbook_id,
            source_digest="sha256:" + "2" * 64, contract_fingerprint="sha256:" + "3" * 64,
            compiler_build="test", path="/test/policy.json", created_at=clock[0],
        ))
    await world.db.ensure_integration_subject(Subject(
        id="open-epic", project_id="p", repository_id="r", kind=SubjectKind.SOURCE,
        subject_key="source:r:epic:0", task_id="epic", policy=pin,
        phase=SubjectPhase.REPAIRING, target_ref="refs/heads/aq/epic", head_sha=head,
        schedule=SubjectSchedule.progress(now=clock[0], max_wait_seconds=600),
        created_at=clock[0], updated_at=clock[0],
    ).to_row())
    cleanup = train_cleanup(world, tmp_path, clock=lambda: clock[0])
    assert "retryable" in {r.outcome for r in await cleanup.advance(batch.id, now=clock[0])}
    assert git(world.origin.url, "rev-parse", "refs/heads/aq/epic") == head
    async with world.db.immediate() as conn:
        await conn.execute(update(integration_subjects).where(
            integration_subjects.c.id == "open-epic",
        ).values(phase="done", next_due_at=None, closed_reason="delivered"))
    clock[0] += 30
    assert {r.outcome for r in await cleanup.advance(batch.id, now=clock[0])} == {"complete"}
    assert not git(world.origin.url, "for-each-ref", "--format=%(refname)", "refs/heads/aq/epic")


async def test_abort_cleanup_removes_retained_candidate_but_keeps_local_member(world, tmp_path):
    head = await completed(world, "a")
    batch = await freeze_cleanup_batch(world, "train-abort-retained", {"a": head})
    git(world.origin.clone, "push", "origin", f"{head}:{candidate_ref(batch.id)}")
    retained = RETAINED_CANDIDATE_PREFIX + batch.id
    git(world.origin.clone, "update-ref", retained, head)
    await BatchStore(world.db).set_intent(batch.id, "aborted")
    cleanup = train_cleanup(world, tmp_path)
    await cleanup.reconcile_aborted(time.time())
    assert (await world.db.get_integration_batch(batch.id))["cleanup_state"] == "complete"
    assert not git(world.origin.clone, "for-each-ref", "--format=%(refname)", retained)
    assert git(world.origin.clone, "rev-parse", "refs/heads/aq/a") == head
    assert git(world.origin.url, "rev-parse", "refs/heads/aq/a") == head


async def test_promoted_train_cleanup_uses_frozen_retry_policy(world, tmp_path):
    from src.database.tables import integration_cleanup_items
    from src.git.manager import GitError

    head = await completed(world, "a", land=True, pr=False)
    async with world.db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_policy={"cleanup": {"max_attempts": 3,
                "retry_base_seconds": 7, "retry_max_seconds": 10}},
        ))
    batch = await freeze_cleanup_batch(world, "train-retries", {"a": head})
    promoted = git(world.origin.url, "rev-parse", "refs/heads/main")
    await publish_cleanup_candidate(world, batch, promoted)
    async with world.db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_policy={"cleanup": {"max_attempts": 1}},
        ))
    transport = LocalGit(Path(world.origin.url))
    transport.adelete_repository_ref = AsyncMock(side_effect=GitError("transfer failed"))
    clock = [time.time()]
    cleanup = train_cleanup(world, tmp_path, transport=transport, clock=lambda: clock[0])
    first = await cleanup.advance(batch.id, now=clock[0])
    assert {r.outcome for r in first} == {"complete", "retryable"}
    async with world.db._engine.connect() as conn:
        pending = (await conn.execute(select(integration_cleanup_items).where(
            integration_cleanup_items.c.state == "retryable",
        ))).mappings().all()
    assert len(pending) == 2
    assert all(row["attempts"] == 1 and row["next_attempt_at"] == clock[0] + 7
               for row in pending)
    assert await cleanup.advance(batch.id, now=clock[0] + 6) == []
    clock[0] += 7
    assert {r.outcome for r in await cleanup.advance(batch.id, now=clock[0])} == {"retryable"}
    assert await cleanup.advance(batch.id, now=clock[0] + 9) == []
    clock[0] += 10
    last = await cleanup.advance(batch.id, now=clock[0])
    assert {r.outcome for r in last} == {"failed"}
    assert all(r.attempts == 3 for r in last)
    assert transport.adelete_repository_ref.await_count == 6
    assert git(world.origin.url, "rev-parse", "refs/heads/aq/a") == head


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_promoted_train_cleanup_defers_to_writer_and_settles_delete_response(
    world, tmp_path, ambiguous,
):
    head = await completed(world, "a", land=True, pr=False)
    batch = await freeze_cleanup_batch(world, "train-writer", {"a": head})
    promoted = git(world.origin.url, "rev-parse", "refs/heads/main")
    await publish_cleanup_candidate(world, batch, promoted)
    transport = LocalGit(Path(world.origin.url))
    transport.ambiguous = ambiguous
    clock = [time.time()]
    locks = BranchLock(world.db, clock=lambda: clock[0])
    ref = candidate_ref(batch.id)
    writer = await locks.acquire(BranchKey(repository_id="r", branch=ref),
                                 "repair-writer", role="repair", ttl_seconds=120)
    cleanup = train_cleanup(world, tmp_path, transport=transport, clock=lambda: clock[0])
    assert "retryable" in {r.outcome for r in await cleanup.advance(batch.id, now=clock[0])}
    assert git(world.origin.url, "rev-parse", ref) == promoted
    assert (await locks.get(writer.target)).holder == "repair-writer"
    await locks.release(writer)
    clock[0] += 30
    assert {r.outcome for r in await cleanup.advance(batch.id, now=clock[0])} == {"complete"}
    assert (await world.db.get_integration_batch(batch.id))["cleanup_state"] == "complete"


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


def lane(world, transport, *, regenerate=DEFAULT_REGENERATE_COMMAND, target=MAIN,
         clock=time.time, settling=False):
    """A train lane over the origin clone. *regenerate* may be a callable, read
    per visit, so a test can change the repository's configuration."""
    db, origin = world.db, world.origin
    binding = GitHubRepositoryBinding(123, "test/repo")

    def retained(command=None):
        return RetainedRepository(target.repository_id, origin.clone, binding, "main", regenerate=command)

    command = regenerate if callable(regenerate) else lambda: regenerate

    async def repository(batch):
        assert batch.repository_id == target.repository_id
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
            str(origin.clone), project_id=target.project_id, repository_id=target.repository_id,
            repository_url=origin.url, target_ref=target.target_ref,
        )

    async def lane_for(requested):
        assert requested == target
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
    # Forced sealing cannot bypass a refused PR's observation backoff.
    assert (await train.visit(MAIN, seal_now=True)).detail["blockers"][0]["code"] == "pr_checks_red"
    now[0] = 1060
    assert (await train.visit(MAIN)).detail["seal_at"] == 1360
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


@pytest.mark.parametrize("scenario", ("clean", "revert"))
async def test_train_member_with_stale_origin_and_inherited_target_needs_no_repair(world, scenario):
    db, origin = world.db, world.origin
    base = git(origin.clone, "rev-parse", "main")
    recorded, inherited, current, head = inherited_source(origin.clone, base, scenario)
    git(origin.clone, "push", "origin", f"{current}:refs/heads/main",
        f"{head}:refs/heads/aq/inherited")
    await completed(world, "inherited", head=head, source_base=recorded)
    train, checks, _ = lane(world, LocalGit(Path(origin.url)))

    testing = await train.visit(MAIN)
    assert testing.state == "testing", testing
    assert testing.repair is None
    candidate = testing.candidate_sha
    expected = "base" if scenario == "revert" else "target moved on"
    assert git(origin.url, "show", f"{candidate}:base.txt") == expected
    assert git(origin.url, "show", f"{candidate}:own.txt") == "own change"
    message = git(origin.url, "show", "-s", "--format=%B", candidate)
    assert f"Source-base: {recorded}" in message
    assert f"Effective-merge-base: {inherited}" in message
    frozen = await BatchStore(db).members(testing.batch_id)
    assert [(member.source_sha, member.source_base_sha) for member in frozen] == [(head, recorded)]
    checks.green.add(candidate)
    delivered = await train.visit(MAIN)
    assert delivered.state == "delivered", delivered
    assert git(origin.url, "rev-parse", "main") == candidate
    batch = await BatchStore(db).get(testing.batch_id)
    assert batch.repair_attempt_count == 0


async def cross_epic_world(world):
    """Two epics whose child dependency spans different branch histories."""
    epic = await completed(world, "epic", done=False)
    await completed(world, "other", done=False)
    prerequisite = await completed(world, "prerequisite", parent="other")
    await completed(world, "child", parent="epic", needs=("prerequisite",), done=False)
    await world.db.transition_task("child", TaskStatus.READY)
    return epic, prerequisite, TrainTarget("p", "r", "refs/heads/aq/epic", "epic")


@pytest.mark.parametrize("delivery", ["ancestry", "trailer", "patch"])
async def test_cross_epic_frontier_requires_exact_default_proof(world, delivery):
    db, origin = world.db, world.origin
    _, source, _ = await cross_epic_world(world)
    # Delivery to the source's own epic releases none of its cross-epic consumers.
    git(origin.clone, "checkout", "-B", "aq/other", "origin/aq/other")
    git(origin.clone, "merge", "--no-ff", "-m", "source epic", source)
    git(origin.clone, "push", "origin", "aq/other")
    exclusions = await db.claim_frontier_exclusions("child")
    assert [(item["ref"], item["epic_id"]) for item in exclusions
            if item["code"] == "prerequisite_not_on_default_branch"] == [("prerequisite", "other")]
    assert not await db.is_hierarchy_task_runnable("child")
    git(origin.clone, "checkout", "-B", "main", "origin/main")
    if delivery == "ancestry":
        git(origin.clone, "merge", "--no-ff", "-m", "default delivery", source)
    else:
        git(origin.clone, "cherry-pick", "--no-commit", source)
        message = (f"squash\n\nAQ-Source: prerequisite@{source}" if delivery == "trailer"
                   else "equivalent whole source")
        git(origin.clone, "commit", "-m", message)
    git(origin.clone, "push", "origin", "main")
    await db._delivery_observer.prerequisite_view("p", task_id="child")
    assert not [item for item in await db.claim_frontier_exclusions("child")
                if "prerequisite" in item["code"]]
    assert not await db.is_hierarchy_task_runnable("child")
    assert "frontier_epic_refresh_pending" in {
        item["code"] for item in await db.claim_frontier_exclusions("child")}
    # The epic needs the same proven source, including squash/patch delivery.
    git(origin.clone, "checkout", "-B", "aq/epic", "origin/aq/epic")
    git(origin.clone, "merge", "--no-ff", "-m", "contain default prerequisite", "origin/main")
    git(origin.clone, "push", "origin", "aq/epic")
    await db._delivery_observer.prerequisite_view("p", task_id="child")
    assert await db.is_hierarchy_task_runnable("child")


async def test_cross_epic_completed_policy_is_explicit_legacy_admission(world):
    db = world.db
    await cross_epic_world(world)
    policy = {"cross_epic_prerequisites": "completed"}
    await db.update_project("p", hierarchical_integration_policy=policy)
    assert not [item for item in await db.claim_frontier_exclusions("child")
                if "prerequisite" in item["code"] or item["code"] == "frontier_epic_refresh_pending"]
    assert await db.is_hierarchy_task_runnable("child")


@pytest.mark.parametrize("changed", [False, True])
async def test_empty_completion_root_releases_child_and_has_no_train_blocker(world, changed):
    from src.integration.delivery_truth import DeliveryState, load_delivery_requests
    from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
    from src.integration.scheduler import TrainService
    from src.integration.source_repairs import prove_source_delivered
    from src.models import TaskCompletion

    db, origin = world.db, world.origin
    base = git(origin.url, "rev-parse", "main")
    await completed(world, "epic", done=False)
    source = base
    if changed:
        source = origin.work("no-change")
        origin.land("no-change")
    else:
        git(origin.clone, "push", "origin", f"{base}:refs/heads/aq/no-change")
    await completed(world, "no-change", done=False, head=source, source_base=base)
    await db.save_task_completion(TaskCompletion(
        id="close-no-change", task_id="no-change", outcome="pass", commits=[],
        completed_at=time.time(),
    ))
    await GitProvenance(world.truth.git, str(origin.clone), repository_url=origin.url
                        ).write_completion(CompletedSource(
        CompletionIdentity("p", "r", "no-change", "close-no-change"), source,
    ))
    await db.transition_task("no-change", TaskStatus.COMPLETED)
    await completed(world, "child", parent="epic", needs=("no-change",), done=False)
    await db.transition_task("child", TaskStatus.READY)

    requests = await load_delivery_requests(db, ["no-change"], repository_id="r",
                                             target_ref=MAIN.target_ref, reduced=True)
    proof = await (await snapshot(world)).is_delivered(requests["no-change"], source_base=base)
    assert proof.state is (DeliveryState.CONTAINED if changed else DeliveryState.NO_CHANGE)
    assert proof.satisfied
    if changed:
        # Root delivery alone cannot release a cross-epic child whose parent
        # still lacks the exact source, regardless of descriptive close commits.
        assert not await db.is_hierarchy_task_runnable("child")
        assert "frontier_epic_refresh_pending" in {
            item["code"] for item in await db.claim_frontier_exclusions("child")}
        git(origin.clone, "checkout", "-B", "aq/epic", "origin/aq/epic")
        git(origin.clone, "merge", "--no-ff", "-m", "contain root prerequisite", "origin/main")
        git(origin.clone, "push", "origin", "aq/epic")
    assert await db.is_hierarchy_task_runnable("child")
    assert not [item for item in await db.claim_frontier_exclusions("child")
                if "prerequisite" in item["code"]]
    observer = db._delivery_observer
    view = await observer.observe(["no-change"])
    async with db._engine.connect() as conn:
        from src.database.tables import repos
        repository = (await conn.execute(select(repos).where(repos.c.id == "r"))).mappings().one()
        assert await TrainService(db)._git_delivered_roots_on(conn, view, "p", repository) == {"no-change"}
    repair_proof = await prove_source_delivered(db, observer, task_id="no-change", source={
        "project_id": "p", "repository_id": "r", "head": source, "base": base,
        "generation": 0,
    })
    assert repair_proof.state == "delivered"
    blockers = []
    assert await DatabaseBatches(db).pending(MAIN, await snapshot(world), blockers=blockers) is None
    assert blockers == []
    train, _, _ = lane(world, LocalGit(Path(origin.url)))
    assert (await train.visit(MAIN)).state == "idle"
    status = await IntegrationStatusService(db, git_first="active", train=train).control_status("p")
    assert not [item for item in status["blockers"] if item.get("task_id") == "no-change"]


@pytest.mark.parametrize("relation", ["sibling", "cross_epic"])
@pytest.mark.parametrize("artifact", ["branch", "pr", "commits", "checkpoint", "origin", "legacy"])
async def test_prerequisite_with_delivery_identity_still_requires_proof(world, relation, artifact):
    from src.database.tables import task_integration_checkpoints
    from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
    from src.integration.publishable_artifact import LEGACY_ARTIFACT_KEY

    db = world.db
    await completed(world, "epic", done=False)
    parent = "epic" if relation == "sibling" else None
    await db.create_task(Task(id="proof", project_id="p", title="Proof chore", description="",
                             parent_task_id=parent, status=TaskStatus.IN_PROGRESS))
    await completed(world, "child", parent="epic", needs=("proof",), done=False)
    await db.transition_task("child", TaskStatus.READY)
    base = git(world.origin.clone, "rev-parse", "main")
    await close(db, "proof", [base] if artifact == "commits" else [])
    if artifact == "branch":
        await db.update_task("proof", branch_name="aq/proof")
    elif artifact == "pr":
        await db.update_task("proof", pr_url="https://github.com/acme/widgets/pull/1")
    elif artifact == "checkpoint":
        async with db._engine.begin() as conn:
            # A checkpoint without a source yet is still a delivery request.
            await conn.execute(insert(task_integration_checkpoints).values(
                task_id="proof", repository_id="r", branch="aq/proof", updated_at=time.time(),
            ))
    elif artifact == "origin":
        async with db._engine.begin() as conn:
            await conn.execute(insert(task_branch_origins).values(
                id="proof-origin", task_id="proof", repository_id="r", branch_name="aq/proof",
                base_sha=base, creation_generation=0, reserved=True, materialized=False,
                created_at=time.time(),
            ))
    elif artifact == "legacy":
        await db.set_task_meta("proof", LEGACY_ARTIFACT_KEY, {
            "completion_id": "close-proof", "source_sha": base,
            "reason": "historical source without provenance",
        })
    assert not await db.is_hierarchy_task_runnable("child")
    assert await db.count_ready_by_profile("p") == {}
    assert any("prerequisite" in item["code"]
               for item in await db.claim_frontier_exclusions("child"))

    # Neither identity nor an empty commit list is proof; retaining the exact
    # generation lets Git prove its source on the default/parent target.
    await GitProvenance(world.truth.git, str(world.origin.clone), repository_url=world.origin.url
                        ).write_completion(CompletedSource(
        CompletionIdentity("p", "r", "proof", "close-proof"), base,
    ))
    await db._delivery_observer.prerequisite_view("p", task_id="child")
    assert await db.is_hierarchy_task_runnable("child")
    assert await db.count_ready_by_profile("p") == {None: 1}
    assert not [item for item in await db.claim_frontier_exclusions("child")
                if "prerequisite" in item["code"]]


async def test_epic_refresh_preview_apply_attestation_lease_and_origin(world):
    from src.integration.stacked_branches import EpicRefresh

    db, origin = world.db, world.origin
    epic, prerequisite, target = await cross_epic_world(world)
    origin.land("prerequisite")
    default = git(origin.url, "rev-parse", "main")
    train, checks, _ = lane(world, LocalGit(Path(origin.url)), target=target)
    controls = TrainControls(db, train=train)
    preview = await controls.refresh_epic("epic", dry_run=True, operator_id="operator")
    assert preview["outcome"] == "preview" and preview["behind"] == 2
    assert git(origin.url, "rev-parse", "aq/epic") == epic
    assert await DatabaseBatches(db).current(target) is None
    status = await IntegrationStatusService(db, git_first="active").train_status("p")
    assert next(item for item in status["epics"] if item["task_id"] == "epic")["behind"] == 2

    pending = await controls.refresh_epic("epic", dry_run=False, operator_id="operator")
    assert pending["outcome"] == "pending" and pending["state"] == "testing"
    assert git(origin.url, "rev-parse", "aq/epic") == epic
    reasons = await db.claim_frontier_exclusions("child")
    assert "frontier_epic_refresh_pending" in {item["code"] for item in reasons}
    checks.green.add(pending["candidate_sha"])
    # Checks alone cannot bypass attestation or another writer's epic lease.
    train_lane = await train.lane_for(target)
    train_lane.service.attest.return_value = "unavailable"
    refused = await controls.refresh_epic("epic", dry_run=False, operator_id="operator")
    assert refused["state"] == "held"
    assert git(origin.url, "rev-parse", "aq/epic") == epic
    train_lane.service.attest.return_value = "published"
    lock = BranchLock(db)
    key = BranchKey(repository_id="r", branch=target.target_ref)
    fence = await lock.acquire(key, "another-writer", role="integration", ttl_seconds=120)
    busy = await controls.refresh_epic("epic", dry_run=False, operator_id="operator")
    assert busy["state"] == "unknown"
    assert git(origin.url, "rev-parse", "aq/epic") == epic
    await lock.release(fence)
    refreshed = await controls.refresh_epic("epic", dry_run=False, operator_id="operator")
    assert refreshed["outcome"] == "refreshed"
    head = git(origin.url, "rev-parse", "aq/epic")
    assert refreshed["target_sha"] == head
    for source in (epic, default, prerequisite):
        git(origin.url, "merge-base", "--is-ancestor", source, head)
    task = await db.get_task("child")
    filing = await db.get_task_branch_origin_for_promotion("child", "r")
    assert await EpicRefresh(db, train).child_base(task, filing) == head
    recorded = await db.get_task_branch_origin_for_promotion("child", "r")
    assert recorded["base_sha"] == filing["base_sha"]
    assert recorded["base_refresh"]["head_sha"] == head
    assert recorded["base_refresh"]["default_sha"] == default


@pytest.mark.parametrize("repair_contains_prerequisite", [True, False])
async def test_epic_refresh_conflict_files_ordinary_repair_on_epic(
    world, repair_contains_prerequisite,
):
    from src.integration.stacked_branches import EpicRefresh, EpicRefreshPending

    db, origin = world.db, world.origin
    _, prerequisite, target = await cross_epic_world(world)
    git(origin.clone, "checkout", "-B", "aq/epic", "origin/aq/epic")
    (origin.clone / "base.txt").write_text("epic edit\n")
    git(origin.clone, "commit", "-am", "epic edit")
    git(origin.clone, "push", "origin", "aq/epic")
    origin.land("prerequisite")
    (origin.clone / "base.txt").write_text("default edit\n")
    git(origin.clone, "commit", "-am", "default edit")
    git(origin.clone, "push", "origin", "main")
    transport = LocalGit(Path(origin.url))
    train, checks, _ = lane(world, transport, target=target)
    pending = await EpicRefresh(db, train).refresh("epic", dry_run=False)
    assert pending["state"] == "repair", pending
    repairs = [task for task in await db.list_tasks("p") if task.dedup_key
               and task.dedup_key.startswith(f"repair:{pending['batch_id']}:")]
    [repair] = repairs
    assert repair.branch_name == target.target_ref.removeprefix("refs/heads/")
    owner = await BranchLock(db).get(BranchKey(repository_id="r", branch=target.target_ref))
    assert owner.holder == repair.id
    assert "frontier_epic_refresh_pending" in {
        item["code"] for item in await db.claim_frontier_exclusions("child")}

    # A repair can publish the resolved merge or only fix the conflicting
    # content. The refresh still owes checks and attestation in either case.
    git(origin.clone, "checkout", "-B", "aq/epic", "origin/aq/epic")
    conflict = await transport.arun_git_result(
        ["merge", "--no-commit", "origin/main"], cwd=origin.clone,
    )
    assert conflict.returncode == 1
    if repair_contains_prerequisite:
        (origin.clone / "base.txt").write_text("resolved epic and default edit\n")
    else:
        git(origin.clone, "merge", "--abort")
        (origin.clone / "base.txt").write_text("default edit\n")
    git(origin.clone, "add", "base.txt")
    git(origin.clone, "commit", "-m", "resolve default refresh")
    repaired = git(origin.clone, "rev-parse", "HEAD")
    locks = BranchLock(db)
    await locks.fenced_push(owner.grant(), git=transport, checkout_path=str(origin.clone),
        repository=GitHubRepositoryBinding(123, "test/repo"), tip_oid=repaired,
        expected_old_oid=pending["target_sha"])
    await db.update_task(repair.id, status=TaskStatus.COMPLETED)
    await locks.release(owner.grant())
    checking = await EpicRefresh(db, train).refresh("epic", dry_run=False)
    assert checking["state"] == "testing"
    assert git(origin.url, "rev-parse", "aq/epic") == repaired
    assert checking["candidate_sha"] not in checks.green
    (await train.lane_for(target)).service.attest.assert_not_awaited()
    # Containment and completion of the open refresh are independent gates.
    # Both repair forms still owe checks and attestation before child admission.
    view = await db._delivery_observer.prerequisite_view("p", task_id="child")
    assert view.default.satisfied("prerequisite")
    assert view.parent_containment == {"child": {"prerequisite": repair_contains_prerequisite}}
    assert not await db.is_hierarchy_task_runnable("child")
    assert "frontier_epic_refresh_pending" in {
        item["code"] for item in await db.claim_frontier_exclusions("child")}
    task = await db.get_task("child")
    filing = await db.get_task_branch_origin_for_promotion("child", "r")
    if repair_contains_prerequisite:
        git(origin.url, "merge-base", "--is-ancestor", prerequisite, repaired)
    with pytest.raises(EpicRefreshPending, match="epic refresh pending"):
        await EpicRefresh(db, train).child_base(task, filing)
    checks.green.add(checking["candidate_sha"])
    delivered = await EpicRefresh(db, train).refresh("epic", dry_run=False)
    assert delivered["outcome"] == "refreshed"
    assert await db.is_hierarchy_task_runnable("child")
    git(origin.url, "merge-base", "--is-ancestor", repaired, "aq/epic")


async def test_epic_refresh_serializes_with_open_collection(world):
    from src.integration.stacked_branches import EpicRefresh

    _, _, target = await cross_epic_world(world)
    world.origin.land("prerequisite")
    store = BatchStore(world.db)
    source = git(world.origin.url, "rev-parse", "aq/child")
    base = git(world.origin.clone, "rev-parse", source + "^")
    collection = Batch("train-existing", "p", "r", target.target_ref, created_at=time.time())
    await store.freeze(collection, (BatchMember("child", source, base),),
                       trees={"child": tree(world, source)})
    pending = await EpicRefresh(world.db).refresh("epic", dry_run=False)
    assert pending["outcome"] == "pending" and pending["batch_id"] == collection.id
    assert not await world.db.is_hierarchy_task_runnable("child")
    assert "frontier_epic_refresh_pending" in {
        item["code"] for item in await world.db.claim_frontier_exclusions("child")
    }


async def test_epic_refresh_main_advances_without_repeating_refresh(world):
    from src.integration.stacked_branches import EpicRefresh

    db, origin = world.db, world.origin
    _, prerequisite, target = await cross_epic_world(world)
    origin.land("prerequisite")
    train, checks, _ = lane(world, LocalGit(Path(origin.url)), target=target)
    refresh = EpicRefresh(db, train)
    pending = await refresh.refresh("epic", dry_run=False)
    frozen_main = git(origin.url, "rev-parse", "main")
    advanced = commit(origin.clone, {"unrelated.txt": "later main work\n"}, base=frozen_main)
    git(origin.clone, "push", "origin", f"{advanced}:main")
    checks.green.add(pending["candidate_sha"])
    applied = await refresh.refresh("epic", dry_run=False)
    assert applied["outcome"] == "refreshed", applied
    assert (await refresh.inspect("epic"))[3]["behind"] == 1
    head = git(origin.url, "rev-parse", "aq/epic")
    git(origin.url, "merge-base", "--is-ancestor", prerequisite, head)
    task = await db.get_task("child")
    filing = await db.get_task_branch_origin_for_promotion("child", "r")
    assert await refresh.child_base(task, filing) == head
    assert await DatabaseBatches(db).current(target) is None
    assert git(origin.url, "rev-parse", "main") == advanced


async def test_epic_refresh_authorization_uses_cached_visit_git(world, monkeypatch):
    from src.integration.stacked_branches import EpicRefresh

    _, _, target = await cross_epic_world(world)
    world.origin.land("prerequisite")
    train, checks, _ = lane(world, LocalGit(Path(world.origin.url)), target=target)
    pending = await EpicRefresh(world.db, train).refresh("epic", dry_run=False)
    checks.green.add(pending["candidate_sha"])
    monkeypatch.setattr("src.integration.train_sources.project_snapshot",
                        AsyncMock(side_effect=AssertionError("a visit already fetched main")))
    service = (await train.lane_for(target)).service
    publish, remote = service.publish, service.gitops.remote
    in_authorize = False
    calls = []

    async def checked_remote(*args):
        assert not in_authorize, "remote I/O inside refresh publication authorization"
        return await remote(*args)

    async def checked_publish(repo, ref, *, authorize, **kwargs):
        async def cached_authorize():
            nonlocal in_authorize
            in_authorize = True
            try:
                calls.append(ref)
                return await authorize()
            finally:
                in_authorize = False
        return await publish(repo, ref, authorize=cached_authorize, **kwargs)

    monkeypatch.setattr(service.gitops, "remote", checked_remote)
    monkeypatch.setattr(service, "publish", checked_publish)
    delivered = await train.request_visit(target)
    assert delivered.state == "delivered", delivered
    assert calls == [target.target_ref]


@pytest.mark.parametrize("failure", ["red", "ci_not_triggered", "default_sync"])
async def test_epic_refresh_failed_ci_repair_starts_at_candidate(world, failure):
    from src.integration.candidate_baseline import Baseline
    from src.integration.checks import (
        ChecksResult,
        ChecksState,
        CommitCheck,
        Conclusion,
        RequiredChecks,
    )
    from src.integration.stacked_branches import EpicRefresh
    from src.orchestrator.workspace import WorkspaceMixin

    db, origin = world.db, world.origin
    _, prerequisite, target = await cross_epic_world(world)
    origin.land("prerequisite")
    transport = LocalGit(Path(origin.url))
    train, checks, _ = lane(world, transport, target=target)
    pending = await EpicRefresh(db, train).refresh("epic", dry_run=False)
    candidate = pending["candidate_sha"]
    assert candidate != pending["target_sha"]

    def failed(head):
        return ChecksResult(repository_id="r", sha=head.sha,
            required=RequiredChecks(version="v1", names=("unit",), producer_id="test"),
            state=ChecksState.UNKNOWN if failure == "ci_not_triggered" else ChecksState.RED,
            checks=(CommitCheck(repository_id="r", sha=head.sha, name="unit", producer_id="test",
                required_check_version="v1", observed_at=time.time(), reason="push run missing",
                conclusion=Conclusion.UNAVAILABLE if failure == "ci_not_triggered"
                           else Conclusion.FAILURE,
                classification="ci_not_triggered" if failure == "ci_not_triggered"
                               else "conclusive",
                detail={"repair_source_ref": "refs/heads/main"}),))

    checks._result = failed
    if failure == "default_sync":
        train.baseline.verdict = AsyncMock(return_value=Baseline(
            pending["target_sha"], "red", (), ("unit",), (),
        ))
        train_lane = await train.lane_for(target)
        train_lane = replace(train_lane, sync_default_branch=AsyncMock(return_value={
            "ref": MAIN.target_ref, "sha": git(origin.url, "rev-parse", "main"),
            "checks": ["unit"],
        }))
        train.lane_for = AsyncMock(return_value=train_lane)
    visit = await train.request_visit(target)
    assert visit.state == "repair", visit
    original = await OrdinaryRepairService(db).input(visit.repair["task_id"])
    assert original["starting_sha"] == candidate
    assert original["target_ref"] == target.target_ref
    git(origin.url, "merge-base", "--is-ancestor", prerequisite, original["starting_sha"])
    # The epic ref still points to the pre-refresh target. Workspace preparation
    # must use the tested candidate without publishing it before repair CI.
    workspace = WorkspaceMixin()
    workspace.git = transport
    start = await workspace._hierarchy_repair_start(str(origin.clone), {
        "base_sha": original["starting_sha"], "ordinary_repair": True, "epic_refresh": True,
    }, Fence.model_validate(visit.repair["fence"]), repository_url=origin.url)
    assert start == candidate
    assert git(origin.url, "rev-parse", "aq/epic") == pending["target_sha"]


async def test_cross_epic_refresh_precedes_sibling_stack_and_child_checkout(world):
    from src.integration.ownership import BranchOwnership
    from src.integration.stacked_branches import EpicRefresh
    from src.orchestrator.workspace import WorkspaceMixin
    from tests.test_integration_stacked_branches import checkpoint

    db, origin = world.db, world.origin
    _, prerequisite, target = await cross_epic_world(world)
    sibling = await completed(world, "sibling", parent="epic")
    await checkpoint(db, "sibling", sibling)
    await checkpoint(db, "child", git(origin.url, "rev-parse", "aq/child"))
    await db.add_dependency("child", "sibling")
    await db.update_project("p", hierarchical_integration_policy={"prerequisite_branches": "stacked"})
    origin.land("prerequisite")
    transport = LocalGit(Path(origin.url))
    train, checks, _ = lane(world, transport, target=target)
    refresh = EpicRefresh(db, train)
    pending = await refresh.refresh("epic", dry_run=False)
    checks.green.add(pending["candidate_sha"])
    assert (await refresh.refresh("epic", dry_run=False))["outcome"] == "refreshed"
    await BranchOwnership(db).acquire(BranchKey(repository_id="r", branch="aq/child"),
                                       "child", "worker")
    workspace = WorkspaceMixin()
    workspace.db, workspace.git, workspace.integration_train = db, transport, train
    view = await db._delivery_observer.prerequisite_view("p", task_id="child")
    assert view.default.satisfied("prerequisite"), view.default.evidence
    assert await view.fresh(), (view.siblings.evidence, view.default.evidence)
    assert await db.is_hierarchy_task_runnable("child"), await db.claim_frontier_exclusions("child")
    overlay, _, role = await workspace._hierarchy_origin_and_fence(
        await db.get_task("child"), await db.get_project("p"),
    )
    assert role == "worker"
    epic_head = git(origin.url, "rev-parse", "aq/epic")
    for source in (epic_head, prerequisite, sibling):
        git(overlay["stack_store"], "merge-base", "--is-ancestor", source, overlay["base_sha"])
    git(origin.clone, "fetch", str(overlay["stack_store"]), overlay["base_sha"])
    await transport.aprepare_child_branch(str(origin.clone), "aq/child", overlay["base_sha"])
    for source in (epic_head, prerequisite, sibling):
        git(origin.clone, "merge-base", "--is-ancestor", source, "HEAD")


async def test_generated_catalogue_members_rebuild_once_after_combined_merge(world, monkeypatch):
    from src.integration.regeneration import regenerated_tree

    origin = world.origin
    catalogue_repository(origin)
    a = await completed(world, "a", head=catalogue_branch(origin, "a", "alpha"))
    b = await completed(world, "b", head=catalogue_branch(origin, "b", "beta"))
    c = await completed(world, "c", head=catalogue_branch(origin, "c", "gamma"))
    rebuild = AsyncMock(wraps=regenerated_tree)
    monkeypatch.setattr("src.integration.gitops.regenerated_tree", rebuild)
    train, github, _ = await hosted_train(world)
    main = git(origin.url, "rev-parse", "refs/heads/main")

    testing = await train.visit(MAIN)
    assert testing.state == "testing", testing
    assert git(origin.url, "rev-parse", "refs/heads/main") == main
    candidate = git(origin.url, "rev-parse", candidate_ref(testing.batch_id))
    rebuild.assert_awaited_once()
    modules = set(json.loads(git(origin.url, "show", f"{candidate}:{Catalogue.CATALOGUE_PATH}"))[
        "modules"])
    assert modules == {f"tests/test_{module}.py" for module in ("alpha", "base", "beta", "gamma")}
    for source in (a, b, c):
        git(origin.url, "merge-base", "--is-ancestor", source, candidate)

    github.runs[testing.candidate_sha] = "success"
    delivered = await train.visit(MAIN)
    assert delivered.state == "delivered", delivered
    assert git(origin.url, "rev-parse", "refs/heads/main") == testing.candidate_sha


@pytest.mark.parametrize("failure", ["infrastructure", "unsafe_source_write"])
async def test_combined_regeneration_failure_never_publishes_an_intermediate_union(world, failure):
    origin = world.origin
    catalogue_repository(origin)
    a = await completed(world, "a", head=catalogue_branch(origin, "a", "alpha"))
    b = await completed(world, "b", head=catalogue_branch(origin, "b", "beta"))
    script = origin.clone.parent / "failed-regenerator.py"
    script.write_text(
        "raise SystemExit(1)\n" if failure == "infrastructure" else
        "from pathlib import Path\nPath('base.txt').write_text('unsafe source change')\n"
    )
    train, checks, _ = lane(world, LocalGit(Path(origin.url)),
                            regenerate=lambda: f"{sys.executable} {script}")
    main = git(origin.url, "rev-parse", "main")
    visit = await train.visit(MAIN)
    assert visit.state == ("unknown" if failure == "infrastructure" else "repair"), visit
    assert git(origin.url, "rev-parse", "main") == main
    if failure == "infrastructure":
        assert visit.repair is None
        assert (await train.visit(MAIN)).state == "unknown"
    else:
        # A failed safe-tree check cannot leave an all-source intermediate on
        # the public candidate ref that ancestry would accept on the next visit.
        ref = candidate_ref(visit.batch_id)
        assert git(origin.url, "rev-parse", ref) == main
        checks.green.add(main)
        repeated = await train.visit(MAIN)
        assert repeated.state == "repair", repeated
        assert repeated.repair["task_id"] == visit.repair["task_id"]
        assert repeated.repair["attempt_count"] == 1
        assert git(origin.url, "rev-parse", ref) == main
    for tid, head in (("a", a), ("b", b)):
        assert git(origin.url, "rev-parse", f"aq/{tid}") == head


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
    now = [1000.0]
    heads = {}
    for tid in ("missing-a", "missing-b"):
        heads[tid] = await completed(world, tid, done=False)
        # A legacy close without retained Git evidence must never borrow its
        # published branch, even when its exact source is reported in the DB.
        await close(db, tid, [heads[tid]])
    await completed(world, "landed", land=True)
    train, _, _ = lane(world, LocalGit(Path(origin.url)), clock=lambda: now[0])
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
    assert (await train.tick())["deferred"] == ["/".join(MAIN.key)]
    now[0] = train.status()[0]["detail"]["retry_at"]
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


@pytest.mark.parametrize("unknown_after_seal", [False, True])
async def test_unknown_root_withholds_only_itself_and_dependents_while_healthy_batch_publishes(
    world, unknown_after_seal,
):
    healthy = await completed(world, "healthy")
    train, checks, _ = lane(world, LocalGit(Path(world.origin.url)))

    async def unknown_work():
        await completed(world, "missing", done=False)
        await close(world.db, "missing", [])
        await completed(world, "dependent", needs=("missing",))

    if not unknown_after_seal:
        await unknown_work()
    sealed = await train.visit(MAIN)
    assert sealed.state == "testing"
    if unknown_after_seal:
        await unknown_work()
    checks.green.add(sealed.candidate_sha)
    published = await train.visit(MAIN)
    assert published.state == "delivered"
    assert published.batch_id == sealed.batch_id
    assert [m.task_id for m in await BatchStore(world.db).members(sealed.batch_id)] == ["healthy"]
    assert git(world.origin.url, "rev-parse", "main") == published.candidate_sha
    git(world.origin.url, "merge-base", "--is-ancestor", healthy, "main")
    await train.tick()
    await train.drain()
    status = IntegrationStatusService(world.db, git_first="active", train=train)
    assert [b["code"] for b in (await status.task_blockers("missing"))["blockers"]] == [
        "missing_git_provenance",
    ]
    assert not (await status.task_blockers("healthy"))["blockers"]


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
        self.permissions = {"alice": "write", "bob": "write", "jack": "write"}
        self.pulls = {}
        #: PR url -> GitHub's ``mergeable_state``: "clean" unless set.
        self.mergeability: dict[str, str] = {}
        self.origin = None
        self.unavailable = False
        self.observed: list[str] = []
        self.records: list[dict] = []

    def _rows(self, sha, event="push"):
        runs = self.runs if event == "push" else self.pr_runs
        n = list(runs).index(sha) + 1 + (100 if event == "pull_request" else 0)
        done = {"head_sha": sha,
                "status": "in_progress" if runs[sha] == "pending" else "completed",
                "conclusion": None if runs[sha] == "pending" else runs[sha]}
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
        state = self.mergeability.get(url, "clean")
        return {"state": "closed" if merged else "open", "merged": merged,
                "merge_commit_sha": main if merged else None,
                "mergeable": None if state == "unknown" else state != "dirty",
                "mergeable_state": state,
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

    async def request_json(self, method, path, *, json_body=None, expected_statuses=None):
        if method == "GET" and path.endswith("/permission"):
            login = path.split("/")[-2]
            return {"permission": self.permissions.get(login, "read"),
                    "user": {"login": login}}
        assert method == "POST" and expected_statuses == {201}
        record = {"id": 7001 + len(self.records), "app": {"id": 101}, **json_body}
        self.records.append(record)
        return {"id": record["id"]}


async def hosted_train(world, *, retained_store=None, clock=time.time, settling=False,
                       review_requirements=None):
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
            if url and branch:
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

    async def store(repo_row, *, fetch=True):
        assert not fetch
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
        lane_for=DaemonLanes(orchestrator, batches=batches, clock=clock,
                            review_requirements=review_requirements),
        repair=OrdinaryRepairService(db, clock=clock), clock=clock,
    )
    return train, github, trusts


@pytest.mark.parametrize("kind,boundary", [("root", "root"), ("epic", "parent")])
async def test_daemon_lane_refreshes_boundary_repair_policy_on_each_visit(world, kind, boundary):
    from src.integration.models import RepairPolicy

    train, _, _ = await hosted_train(world)
    target = MAIN if kind == "root" else TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
    policy = {
        lane: {"repair": RepairPolicy(debug_intelligence_class=f"{lane}-debug").model_dump()}
        for lane in ("root", "parent")
    }
    for attempts, debug_class in ((2, "standard-high"), (1, "deep-high")):
        policy[boundary]["repair"].update(primary_attempts=attempts,
                                          debug_intelligence_class=debug_class)
        async with world.db.immediate() as conn:
            await conn.execute(update(projects).where(projects.c.id == "p").values(
                hierarchical_integration_policy=policy,
            ))
        current = await train.lane_for(target)
        assert current.repair_policy.primary_attempts == attempts
        assert current.repair_policy.debug_intelligence_class == debug_class


@pytest.fixture
async def collected_epic(world, request):
    """Two ordinary completions collected through the daemon's actual epic lane."""
    db, origin = world.db, world.origin
    now = [time.time()]
    clock = (lambda: now[0]) if getattr(request, "param", None) == "controlled_clock" else time.time
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
    train, github, trusts = await hosted_train(world, clock=clock)
    required = {"version": "v1", "names": ["unit"], "producer_id": "15368"}
    async with db._engine.begin() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_policy={
                "parent": {"required_checks": required, "admission": "authorized"},
                "root": {"required_checks": required, "admission": "reviewed"},
            },
        ))
    return SimpleNamespace(world=world, db=db, origin=origin, train=train, github=github,
                           trusts=trusts, base=base, children=children, now=now,
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
    github.permissions[reviewer.casefold()] = "write"
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

    async def development_repository(primitives, repo_row, binding, settings, *, fetch=True):
        assert not fetch
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
    ("outsider_approval", "pr_review_missing"),
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
    elif condition in {"no_approval", "changes", "old_approval", "bot_approval", "dismissed",
                       "outsider_approval"}:
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
            if condition == "outsider_approval":
                github.permissions["bob"] = "read"
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
    # Every named refusal and outage retains its result until retry is due.
    if condition != "missing_pr":
        assert (await train.visit(MAIN)).state == visit["state"]
        now[0] += 61
    github.unavailable = False
    await world.db.update_task("leaf", pr_url=url)
    github.pr_runs[head] = "success"
    github.permissions["bob"] = "write"
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


@pytest.mark.parametrize("condition", ["paired", "repair_red", "unfinished", "stale", "review",
                                      "prerequisite"])
async def test_red_source_requires_exact_admitted_repair_and_combined_candidate(world, condition):
    base = git(world.origin.clone, "rev-parse", "origin/main")
    source = await completed(world, "source")
    repair = await source_ci_repair_of(world, source)
    await bind_source_ci_repair(world, "source", "repair", source_base=base, source_head=source)
    train, github, _ = await hosted_train(world)
    github.pr_runs[source] = "failure"
    if condition == "repair_red":
        github.pr_runs[repair] = "failure"
    elif condition == "unfinished":
        await world.db.update_task("repair", status=TaskStatus.IN_PROGRESS)
    elif condition == "stale":
        async with world.db._engine.begin() as conn:
            await conn.execute(update(integration_source_ci).values(source_head="a" * 40))
    elif condition == "review":
        project = await world.db.get_project("p")
        policy = project.hierarchical_integration_policy
        policy["root"]["admission"] = "reviewed"
        await world.db.update_project("p", hierarchical_integration_policy=policy)
        github.reviews = [{"id": 1, "commit_id": repair, "state": "APPROVED",
                           "user": {"login": "alice", "type": "User"}}]
    elif condition == "prerequisite":
        await completed(world, "unready", done=False)
        await world.db.add_dependency("repair", "unready")
    observed = await train.visit(MAIN)
    if condition != "paired":
        assert observed.batch_id is None, observed
        assert git(world.origin.url, "rev-parse", "main") == base
        return
    assert observed.state == "testing", observed
    assert [member.task_id for member in await BatchStore(world.db).members(
        observed.batch_id)] == ["source", "repair"]
    for head in (source, repair):
        git(world.origin.clone, "merge-base", "--is-ancestor", head, observed.candidate_sha)
    assert git(world.origin.url, "rev-parse", "main") == base
    github.runs[observed.candidate_sha] = "success"
    assert (await train.visit(MAIN)).state == "delivered"
    assert git(world.origin.url, "rev-parse", "main") == observed.candidate_sha


async def test_root_pr_gate_admits_suppressed_checks_without_changing_source(world):
    head = await completed(world, "leaf")
    train, github, _ = await hosted_train(world)
    url = (await world.db.get_task("leaf")).pr_url
    github.pr_runs.clear()
    github.runs[head] = "success"  # Push checks do not replace PR or candidate CI.
    github.mergeability[url] = "dirty"
    main = git(world.origin.url, "rev-parse", "main")
    visit = await train.visit(MAIN)
    assert visit.state == "testing", visit
    assert [(m.task_id, m.source_sha) for m in await BatchStore(world.db).members(
        visit.batch_id)] == [("leaf", head)]
    assert git(world.origin.url, "rev-parse", "aq/leaf") == head
    assert git(world.origin.url, "rev-parse", "main") == main
    assert github.records == []
    github.runs[visit.candidate_sha] = "success"
    assert (await train.visit(MAIN)).state == "delivered"


@pytest.mark.parametrize("condition,code", [
    ("red", "pr_checks_red"),
    ("running", "awaiting_pr_checks"),
    ("cancelled", "unknown"),
    ("push_red", "awaiting_pr_checks"),
    ("push_running", "awaiting_pr_checks"),
    ("push_cancelled", "awaiting_pr_checks"),
    ("push_neutral", "awaiting_pr_checks"),
    ("push_skipped", "awaiting_pr_checks"),
    ("push_timeout", "awaiting_pr_checks"),
    ("check_outage", "unknown"),
    ("review_missing", "pr_review_missing"),
    ("changes_requested", "pr_changes_requested"),
])
async def test_conflicting_pr_does_not_hide_checks_or_review_refusals(world, condition, code):
    head = await completed(world, "leaf")
    train, github, _ = await hosted_train(world)
    url = (await world.db.get_task("leaf")).pr_url
    github.mergeability[url] = "dirty"
    github.pr_runs.clear()
    if condition in {"red", "running", "cancelled"}:
        github.pr_runs[head] = {
            "red": "failure", "running": "pending", "cancelled": "cancelled",
        }[condition]
    elif condition.startswith("push_"):
        github.runs[head] = {
            "push_red": "failure", "push_running": "pending", "push_cancelled": "cancelled",
            "push_neutral": "neutral", "push_skipped": "skipped", "push_timeout": "timed_out",
        }[condition]
    elif condition == "check_outage":
        github.paged_items = AsyncMock(side_effect=GitHubAccessError(
            "transport_unavailable", "required-check observation unavailable",
        ))
    else:
        policy = (await world.db.get_project("p")).hierarchical_integration_policy
        policy["root"]["admission"] = "reviewed"
        await world.db.update_project("p", hierarchical_integration_policy=policy)
        if condition == "changes_requested":
            github.reviews.append({"id": 1, "commit_id": head, "state": "CHANGES_REQUESTED",
                                   "user": {"login": "bob", "type": "User"}})
    visit = await train.visit(MAIN)
    assert visit.batch_id is None, visit
    assert visit.detail["blockers"][0]["code"] == code
    assert git(world.origin.url, "rev-parse", "aq/leaf") == head


async def test_one_repair_keeps_every_conflicting_source_until_combined_candidate_passes(world):
    """Three completed branches conflict with main and each other, including
    generated artifacts. Partial repair, trailers and green partial CI cannot
    deliver; one worker retains the whole frozen batch through final checks."""
    origin = world.origin
    catalogue_repository(origin)
    base = git(origin.url, "rev-parse", "main")
    sources = []
    for tid, module in (("a", "alpha"), ("b", "beta"), ("c", "gamma")):
        catalogue_branch(origin, tid, module)
        (origin.clone / "base.txt").write_text(f"{tid} edit\n")
        git(origin.clone, "commit", "-qam", f"{tid} conflicts")
        git(origin.clone, "push", "-q", "origin", f"aq/{tid}")
        head = git(origin.clone, "rev-parse", "HEAD")
        await completed(world, tid, head=head, source_base=base)
        sources.append(head)
    git(origin.clone, "checkout", "-q", "-B", "main", "origin/main")
    (origin.clone / "base.txt").write_text("main edit\n")
    git(origin.clone, "commit", "-qam", "main conflicts with all sources")
    git(origin.clone, "push", "-q", "origin", "main")
    target = git(origin.clone, "rev-parse", "HEAD")
    train, github, _ = await hosted_train(world)
    policy = (await world.db.get_project("p")).hierarchical_integration_policy
    policy["root"]["admission"] = "reviewed"
    await world.db.update_project("p", hierarchical_integration_policy=policy)
    github.pr_runs.clear()
    for index, (tid, head) in enumerate(zip(("a", "b", "c"), sources, strict=True)):
        url = (await world.db.get_task(tid)).pr_url
        github.mergeability[url] = "dirty"
        github.reviews.append({"id": index + 1, "commit_id": head, "state": "APPROVED",
                               "user": {"login": ("alice", "bob", "jack")[index],
                                        "type": "User"}})
    first = await train.visit(MAIN)
    assert first.state == "repair", first
    frozen = await BatchStore(world.db).members(first.batch_id)
    assert [(m.task_id, m.source_sha) for m in frozen] == list(zip(("a", "b", "c"), sources))
    repair = await world.db.get_task(first.repair["task_id"])
    assert all(head in repair.description for head in sources)
    assert "once" in repair.description and "ancestor" in repair.description
    ref = candidate_ref(first.batch_id)
    fence = Fence.model_validate(first.repair["fence"])
    locks = BranchLock(world.db)
    git(origin.clone, "fetch", "-q", "origin")
    git(origin.clone, "checkout", "-q", "--detach", first.candidate_sha)

    async def merge_source(index):
        result = subprocess.run(
            ["git", "merge", "--no-ff", "--no-commit", sources[index]],
            cwd=origin.clone, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 1, result.stdout + result.stderr
        conflicts = git(origin.clone, "diff", "--name-only", "--diff-filter=U").splitlines()
        assert "base.txt" in conflicts
        if Catalogue.CATALOGUE_PATH in conflicts:
            git(origin.clone, "checkout", "--ours", Catalogue.CATALOGUE_PATH)
        (origin.clone / "base.txt").write_text(
            "main edit\n" + "".join(f"{tid} edit\n" for tid in ("a", "b", "c")[:index + 1]),
        )
        git(origin.clone, "add", "base.txt", Catalogue.CATALOGUE_PATH)
        # A trailer claiming unmerged sources is deliberately not sufficient.
        message = "repair source conflict"
        if index == 0:
            message += "\n\n" + "\n".join(
                f"AQ-Source: {tid}@{head}" for tid, head in zip(("b", "c"), sources[1:])
            )
        git(origin.clone, "commit", "-qm", message)
        candidate = git(origin.clone, "rev-parse", "HEAD")
        old = git(origin.url, "rev-parse", ref)
        await locks.fenced_push(
            fence, git=train.lane_for.git, checkout_path=str(origin.clone),
            repository=GitHubRepositoryBinding(123, github.full_name),
            tip_oid=candidate, expected_old_oid=old,
        )
        return candidate

    partial = await merge_source(0)
    github.runs[partial] = "success"
    train = IntegrationTrain(
        targets=DatabaseTargets(world.db), batches=train.batches, lane_for=train.lane_for,
        repair=OrdinaryRepairService(world.db),
    )
    partial_visit = await train.visit(MAIN)
    assert partial_visit.state == "repair", partial_visit
    assert partial_visit.repair["task_id"] == repair.id
    assert partial_visit.repair["attempt_count"] == 1
    assert git(origin.url, "rev-parse", "main") == target
    assert git(origin.url, "rev-parse", ref) == partial
    for index in (1, 2):
        await merge_source(index)
    subprocess.run([sys.executable, "scripts/generate-selection-catalogue.py"],
                   cwd=origin.clone, check=True, capture_output=True)
    git(origin.clone, "add", Catalogue.CATALOGUE_PATH)
    git(origin.clone, "commit", "-qm", "regenerate after every source is merged")
    repaired = git(origin.clone, "rev-parse", "HEAD")
    await locks.fenced_push(
        fence, git=train.lane_for.git, checkout_path=str(origin.clone),
        repository=GitHubRepositoryBinding(123, github.full_name),
        tip_oid=repaired, expected_old_oid=git(origin.url, "rev-parse", ref),
    )
    github.runs[target] = "success"
    github.runs[repaired] = "failure"
    red = await train.visit(MAIN)
    assert (red.state, red.checks) == ("repair", "red"), red
    assert red.repair["task_id"] == repair.id
    assert git(origin.url, "rev-parse", "main") == target
    assert not github.records
    await close(world.db, repair.id, [repaired], origin=origin)
    await locks.release(fence)
    github.runs[repaired] = "success"
    delivered = await train.visit(MAIN)
    assert delivered.state == "delivered", delivered
    assert delivered.candidate_sha == delivered.target_sha == repaired
    assert github.records[-1]["head_sha"] == repaired
    assert git(origin.url, "rev-parse", "main") == repaired
    for tid, head in zip(("a", "b", "c"), sources, strict=True):
        git(origin.url, "merge-base", "--is-ancestor", head, repaired)
        assert git(origin.url, "rev-parse", f"aq/{tid}") == head
    git(origin.url, "merge-base", "--is-ancestor", target, repaired)
    modules = set(json.loads(git(origin.url, "show", f"{repaired}:{Catalogue.CATALOGUE_PATH}"))[
        "modules"])
    assert modules == {f"tests/test_{module}.py" for module in ("base", "alpha", "beta", "gamma")}


async def test_root_pr_gate_retries_unknown_mergeability_soon_then_backs_off(world):
    head = await completed(world, "leaf")
    now = [1000.0]
    train, github, _ = await hosted_train(world, clock=lambda: now[0])
    url = (await world.db.get_task("leaf")).pr_url
    github.pr_runs.clear()
    github.mergeability[url] = "unknown"
    first = (await train.visit(MAIN)).detail["blockers"][0]
    assert (first["code"], first["mergeable"]) == ("awaiting_pr_checks", "unknown")
    assert first["retry_at"] == now[0] + 15
    now[0] += 16
    again = (await train.visit(MAIN)).detail["blockers"][0]
    assert (again["code"], again["mergeable"]) == ("awaiting_pr_checks", "unknown")
    # GitHub still computing: the ordinary doubling backoff, never a conflict.
    assert again["retry_seconds"] == 60 and again["retry_at"] == now[0] + 60
    github.mergeability[url] = "unknown"
    github.pr_runs[head] = "success"
    now[0] += 601
    # Green exact-head PR checks admit without waiting on mergeability.
    assert (await train.visit(MAIN)).state == "testing"


async def conflicting_epic_pr(case, *, content_conflict=False):
    """A collected, reviewed epic whose PR no longer merges onto the default."""
    if content_conflict:
        git(case.origin.clone, "fetch", "-q", "origin")
        git(case.origin.clone, "checkout", "-q", "-B", "aq/epic", "origin/aq/epic")
        (case.origin.clone / "base.txt").write_text("epic edit\n")
        git(case.origin.clone, "commit", "-qam", "epic edit")
        git(case.origin.clone, "push", "-q", "origin", "aq/epic")
    head = await collect_epic(case)
    await review_epic(case)
    [opened] = (await case.train.visit(case.target)).detail["epic_completions"]
    url = opened["pr_url"]
    git(case.origin.clone, "fetch", "-q", "origin")
    git(case.origin.clone, "checkout", "-q", "-B", "main", "origin/main")
    path = "base.txt" if content_conflict else "unrelated.txt"
    (case.origin.clone / path).write_text("default moves on\n")
    git(case.origin.clone, "add", path)
    git(case.origin.clone, "commit", "-qm", "default moves on")
    git(case.origin.clone, "push", "-q", "origin", "main")
    default = git(case.origin.url, "rev-parse", "main")
    case.github.pr_runs.pop(head, None)
    case.github.mergeability[url] = "dirty"
    assert case.github.runs[head] == "success"  # Push-event runs are not PR checks.
    return head, default, url


@pytest.mark.parametrize("content_conflict", [False, True])
async def test_conflicting_epic_joins_root_batch_without_author_refresh(collected_epic, content_conflict):
    case = collected_epic
    head, default, _ = await conflicting_epic_pr(case, content_conflict=content_conflict)
    leaf = await completed(case.world, "leaf")
    case.github.reviews.append({"id": 100, "state": "APPROVED", "commit_id": leaf,
                               "user": {"login": "alice", "type": "User"}})
    visit = await case.train.visit(MAIN)
    assert visit.state == ("repair" if content_conflict else "testing"), visit
    members = await BatchStore(case.db).members(visit.batch_id)
    assert [(m.task_id, m.source_sha) for m in members] == [("epic", head), ("leaf", leaf)]
    async with case.db._engine.connect() as conn:
        assert not (await conn.execute(select(integration_batches.c.id).where(
            integration_batches.c.id.like("train-epic-refresh-%")))).first()
        assert not (await conn.execute(select(events.c.id).where(
            events.c.event_type == "integration.epic_refresh"))).first()
    assert git(case.origin.url, "rev-parse", "aq/epic") == head
    assert git(case.origin.url, "rev-parse", "main") == default
    if content_conflict:
        repair = await case.db.get_task(visit.repair["task_id"])
        assert repair.branch_name == candidate_ref(visit.batch_id).removeprefix("refs/heads/")
        assert head in repair.description and leaf in repair.description
        again = await case.train.visit(MAIN)
        assert again.repair["task_id"] == repair.id and again.repair["attempt_count"] == 1
    else:
        case.github.runs[visit.candidate_sha] = "success"
        assert (await case.train.visit(MAIN)).state == "delivered"
        for source in (head, leaf, default):
            git(case.origin.url, "merge-base", "--is-ancestor", source, "main")


async def test_explicit_conflicting_epic_refresh_still_files_one_ordinary_repair(collected_epic):
    from src.integration.stacked_branches import EpicRefresh

    case = collected_epic
    head, default, _ = await conflicting_epic_pr(case, content_conflict=True)
    started = await EpicRefresh(case.db).start("epic")
    assert started["outcome"] == "started"
    repair_visit = await case.train.visit(case.target)
    assert repair_visit.state == "repair" and repair_visit.batch_id == started["batch_id"]
    repair = await case.db.get_task(repair_visit.repair["task_id"])
    assert repair.branch_name == "aq/epic"
    assert (await case.train.visit(case.target)).repair["task_id"] == repair.id
    assert git(case.origin.url, "rev-parse", "aq/epic") == head
    assert git(case.origin.url, "rev-parse", "main") == default


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


@pytest.mark.parametrize("collected_epic", ["controlled_clock"], indirect=True)
async def test_fidelity_replay_pr_repair_promotion_cleanup(collected_epic, tmp_path):
    """§6 tests 1-3, 7, 9, 12: keep the source PR through a repaired root delivery."""
    from src.database.tables import (
        integration_batch_members,
        integration_check_evidence,
        integration_cleanup_items,
    )
    from src.integration.root_pull_requests import EpicPullRequestService

    case = collected_epic
    db, origin, train, github = case.db, case.origin, case.train, case.github
    await db.update_task("epic", status=TaskStatus.IN_PROGRESS)
    case.children.append(await completed(case.world, "child-c", parent="epic"))
    await db.transition_task("epic", TaskStatus.COMPLETED)
    collected = await train.visit(case.target)
    github.runs[collected.candidate_sha] = "success"
    assert (await train.visit(case.target)).state == "delivered"
    head = git(origin.url, "rev-parse", "aq/epic")
    await review_epic(case)
    opened = (await train.visit(case.target)).detail["epic_completions"][0]
    url = opened["pr_url"]
    assert opened["outcome"] == "opened" and opened["head_sha"] == head
    assert (await github.pull_request(url))["state"] == "open"
    assert (await train.visit(case.target)).state == "idle"
    assert (await EpicPullRequestService(db, git_manager=train.lane_for.git).open_for_epic("epic"))[
        "outcome"
    ] == "already_open"
    train.lane_for.git.acreate_pr.assert_awaited_once()

    # A DB tree approval does not stand in for the reviewed PR boundary's gate.
    [approval] = github.reviews
    assert approval["user"]["login"] == "jack" and approval["commit_id"] == head
    github.reviews.clear()
    blocked = await train.visit(MAIN)
    assert blocked.batch_id is None
    assert blocked.detail["blockers"][0]["code"] == "pr_review_missing"
    github.reviews.append(approval)
    # Retained admission refusals are refreshed only at their retry deadline.
    assert (await train.visit(MAIN)).detail["blockers"][0]["code"] == "pr_review_missing"
    case.now[0] = blocked.detail["blockers"][0]["retry_at"]
    testing = await train.visit(MAIN)
    assert (testing.state, testing.checks) == ("testing", "pending")
    batch = await db.get_integration_batch(testing.batch_id)
    assert batch["lifecycle"] == "sealed"
    async with db._engine.connect() as conn:
        [member] = (await conn.execute(select(integration_batch_members).where(
            integration_batch_members.c.batch_id == testing.batch_id,
        ))).mappings().all()
    assert (member["task_id"], member["source_sha"], member["reviewed_tree_sha"],
            member["source_ref"], member["pr_url"]) == (
        "epic", head, tree(case.world, head), case.target.target_ref, url,
    )
    ref = candidate_ref(testing.batch_id)
    assert testing.candidate_sha != head
    assert git(origin.url, "rev-parse", ref) == testing.candidate_sha
    assert f"AQ-Source: epic@{head}" in git(origin.url, "show", "-s", "--format=%B", ref)
    github.runs[testing.candidate_sha] = "failure"
    red = await train.visit(MAIN)
    assert (red.state, red.checks) == ("repair", "red")
    assert red.repair["outcome"] == "filed"
    assert (await train.visit(MAIN)).repair["task_id"] == red.repair["task_id"]
    assert (await db.get_integration_batch(testing.batch_id))["tested_candidate_sha"] is None
    assert git(origin.url, "rev-parse", "main") == case.base
    assert github.records[-1]["head_sha"] == head  # Only the epic has been attested.

    # The ordinary repair changes the candidate under its real managed lease.
    repaired = commit(origin.clone, {"fix.txt": "fix failed unit check\n"},
                      base=testing.candidate_sha)
    fence = Fence.model_validate(red.repair["fence"])
    locks = BranchLock(db)
    await locks.fenced_push(
        fence, git=train.lane_for.git, checkout_path=str(origin.clone),
        repository=GitHubRepositoryBinding(123, github.full_name),
        tip_oid=repaired, expected_old_oid=testing.candidate_sha,
    )
    await close(db, red.repair["task_id"], [repaired], origin=origin)
    await locks.release(fence)
    # The repaired SHA needs its own checks despite the source's green PR checks.
    awaiting = await train.visit(MAIN)
    assert (awaiting.state, awaiting.checks) == ("testing", "pending")
    assert awaiting.batch_id == testing.batch_id and awaiting.candidate_sha == repaired
    assert git(origin.url, "rev-parse", ref) == repaired
    assert git(origin.url, "rev-parse", "main") == case.base
    assert (await github.pull_request(url))["head"]["sha"] == head
    github.runs[repaired] = "success"
    delivered = await train.visit(MAIN)
    assert (delivered.state, delivered.checks) == ("delivered", "green")
    assert delivered.candidate_sha == delivered.target_sha == repaired
    assert git(origin.url, "rev-parse", "main") == repaired
    batch = await db.get_integration_batch(testing.batch_id)
    assert batch["tested_candidate_sha"] == batch["final_main_sha"] == repaired
    assert github.records[-1]["head_sha"] == repaired
    assert github.records[-1]["name"] == ATTESTATION_CHECK_NAME
    pull = await github.pull_request(url)
    assert pull["merged"] and pull["state"] == "closed" and pull["merge_commit_sha"] == repaired
    git(origin.url, "merge-base", "--is-ancestor", pull["merge_commit_sha"], "main")
    for source in [head, *case.children]:
        git(origin.url, "merge-base", "--is-ancestor", source, repaired)
    train.lane_for.git.acreate_pr.assert_awaited_once()
    assert (await db.get_task("epic")).pr_url == url

    # Retire the child collection and root batches; both source and private refs go.
    number = int(url.rsplit("/", 1)[-1])
    forge = TrainCleanupForge([(number, head)])
    forge.prs[number]["repository_full_name"] = github.full_name
    binding = GitHubRepositoryBinding(123, github.full_name)
    transport = LocalGit(Path(origin.url))
    transport.adelete_repository_ref = AsyncMock(wraps=transport.adelete_repository_ref)
    cleanup = IntegrationCleanupService(
        db, data_dir=tmp_path, git_manager=transport, forge_provider=forge,
        binding_resolver=AsyncMock(return_value=binding),
        candidate_store=AsyncMock(return_value=origin.clone),
    )
    cleanup_batches = (collected.batch_id, testing.batch_id)
    for cleanup_batch_id in cleanup_batches:
        assert (await cleanup.materialize(cleanup_batch_id)).outcome == "materialized"
        assert (await cleanup.materialize(cleanup_batch_id)).outcome == "already_materialized"
    for cleanup_batch_id in cleanup_batches:
        async with db._engine.connect() as conn:
            items = (await conn.execute(select(integration_cleanup_items).where(
                integration_cleanup_items.c.batch_id == cleanup_batch_id,
            ))).mappings().all()
        assert {item["kind"] for item in items} == (
            {"audit_pr", "remote_ref", "local_ref"} if cleanup_batch_id == testing.batch_id
            else {"remote_ref", "local_ref"}
        )
        if cleanup_batch_id == testing.batch_id:
            [pr_item] = [item for item in items if item["kind"] == "audit_pr"]
            assert pr_item["target_pr_url"] == url and pr_item["expected_sha"] == head
        results = await cleanup.advance(cleanup_batch_id)
        assert len(results) == len(items) and {result.outcome for result in results} == {"complete"}
        assert (await db.get_integration_batch(cleanup_batch_id))["cleanup_state"] == "complete"
        assert not git(origin.url, "for-each-ref", "--format=%(refname)",
                       candidate_ref(cleanup_batch_id))
        assert not git(origin.clone, "for-each-ref", "--format=%(refname)",
                       RETAINED_CANDIDATE_PREFIX + cleanup_batch_id)
    for tid in ("epic", "child-a", "child-b", "child-c"):
        assert not git(origin.url, "for-each-ref", "--format=%(refname)", f"refs/heads/aq/{tid}")
    assert transport.adelete_repository_ref.await_count == 6
    assert all(call.kwargs["repository"] == binding
               for call in transport.adelete_repository_ref.await_args_list)
    [(commented_pr, body)] = forge.comments
    assert commented_pr == number and repaired in body
    assert f"- `{repaired}`" in body.split("Integration repair commits", 1)[1]
    assert await cleanup.advance(testing.batch_id) == [] and len(forge.comments) == 1
    assert git(origin.url, "rev-parse", "main") == repaired
    assert (await train.visit(MAIN)).state == "idle"
    async with db._engine.connect() as conn:
        evidence = (await conn.execute(select(integration_check_evidence))).mappings().all()
    assert any(row["sha"] == testing.candidate_sha and row["conclusion"] == "failure"
               for row in evidence)
    assert any(row["sha"] == repaired and row["conclusion"] == "success" for row in evidence)


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


@pytest.mark.parametrize("suppressed", [False, True])
@pytest.mark.parametrize("mismatch", ["closed", "head", "branch", "base", "fork", "draft"])
async def test_root_pr_gate_requires_exact_open_same_repository_identity(world, mismatch, suppressed):
    await completed(world, "leaf")
    now = [1000.0]
    train, github, _ = await hosted_train(world, clock=lambda: now[0])
    url = (await world.db.get_task("leaf")).pr_url
    if suppressed:
        github.pr_runs.clear()
        github.mergeability[url] = "dirty"
    pull = await github.pull_request(url)
    if mismatch == "closed":
        pull["state"] = "closed"
    elif mismatch == "head":
        pull["head"]["sha"] = "a" * 40
    elif mismatch == "branch":
        pull["head"]["ref"] = "aq/another"
    elif mismatch == "base":
        pull["base"]["ref"] = "another"
    elif mismatch == "draft":
        pull["draft"] = True
    else:
        pull["head"]["repo"]["id"] = 456
    original = github.pull_request
    github.pull_request = AsyncMock(return_value=pull)
    blocked = await train.visit(MAIN)
    code = {"closed": "pr_closed", "draft": "pr_draft"}.get(mismatch, "awaiting_pr")
    assert blocked.batch_id is None and blocked.detail["blockers"][0]["code"] == code
    assert github.observed == []  # Don't read CI for a different proposal.
    github.pull_request = original
    now[0] += 61
    assert (await train.visit(MAIN)).state == "testing"


@pytest.mark.parametrize("condition", ["red", "review_missing", "changes", "closed", "outage"])
async def test_refused_root_github_call_budget_over_an_hour(world, condition):
    head = await completed(world, "leaf")
    now = [1000.0]
    train, github, _ = await hosted_train(world, clock=lambda: now[0])
    code = {"red": "pr_checks_red", "review_missing": "pr_review_missing",
            "changes": "pr_changes_requested", "closed": "pr_closed", "outage": "unknown"}[condition]
    if condition == "red":
        github.pr_runs[head] = "failure"
    elif condition in {"review_missing", "changes"}:
        project = await world.db.get_project("p")
        policy = project.hierarchical_integration_policy
        policy["root"]["admission"] = "reviewed"
        await world.db.update_project("p", hierarchical_integration_policy=policy)
        if condition == "changes":
            github.reviews = [{"id": 1, "state": "CHANGES_REQUESTED", "commit_id": head,
                               "user": {"login": "bob", "type": "User"}}]
    elif condition == "outage":
        github.unavailable = True
    original = github.pull_request

    async def pull(url):
        result = await original(url)
        return {**result, "state": "closed"} if condition == "closed" else result

    github.pull_request = AsyncMock(side_effect=pull)
    github.paged_items = AsyncMock(wraps=github.paged_items)
    github.paged_list = AsyncMock(wraps=github.paged_list)
    github.request_json = AsyncMock(wraps=github.request_json)
    assert (await train.visit(MAIN)).detail["blockers"][0]["code"] == code
    member = BatchMember("leaf", head, git(world.origin.clone, "rev-parse", f"{head}^"))
    # The idle train's real admission callback, visited every five seconds.
    for elapsed in range(5, 3600, 5):
        now[0] = 1000 + elapsed
        refusal = await train.batches.pr_gate(MAIN, member)
        assert refusal["code"] == code
    assert github.pull_request.await_count == 9
    calls = sum(mock.await_count for mock in (github.pull_request, github.paged_items,
                                              github.paged_list, github.request_json))
    assert calls <= 54  # At most six GitHub requests per attempt; formerly thousands.


@pytest.mark.parametrize("stage", ["pull_request", "paged_items", "paged_list", "request_json"])
async def test_root_pr_gate_rate_limit_reaches_shared_pause_with_retry_after(world, stage):
    head = await completed(world, "leaf")
    now = [1000.0]
    train, github, _ = await hosted_train(world, clock=lambda: now[0])
    project = await world.db.get_project("p")
    policy = project.hierarchical_integration_policy
    policy["root"]["admission"] = "reviewed"
    await world.db.update_project("p", hierarchical_integration_policy=policy)
    github.reviews = [{"id": 1, "state": "APPROVED", "commit_id": head,
                       "user": {"login": "bob", "type": "User"}}]
    original = getattr(github, stage)
    limited = AsyncMock(side_effect=GitHubAccessError(
        "rate_limited", "fixture secondary limit", retry_at=1900.0, http_status=403))
    setattr(github, stage, limited)
    await train.tick()
    await train.drain()
    [visit] = train.status()
    assert visit["detail"]["reason"] == "rate_limited"
    assert visit["detail"]["retry_at"] == 1900
    calls = limited.await_count
    now[0] = 1899
    await train.tick()
    await train.drain()
    assert limited.await_count == calls
    setattr(github, stage, original)
    now[0] = 1900
    await train.tick()
    await train.drain()
    assert train.status()[0]["state"] == "testing"


@pytest.mark.parametrize("permission,allowlist,state", [
    ("read", frozenset(), "pr_review_missing"),
    ("none", frozenset({"github:bob"}), "pr_review_missing"),
    ("write", frozenset(), "approved"),
    ("admin", frozenset({"github:BOB"}), "approved"),
    ("write", frozenset({"github:alice"}), "pr_review_missing"),
])
async def test_pr_reviewer_allowlist_only_narrows_repository_write_access(permission, allowlist, state):
    from src.integration.reviews import observe_pull_request_review_state

    client = SimpleNamespace(request_json=AsyncMock(return_value={
        "permission": permission, "user": {"login": "bob"},
    }))
    reviews = [{"id": 1, "state": "APPROVED", "commit_id": "a" * 40,
                "user": {"login": "bob", "type": "User"}}]
    assert await observe_pull_request_review_state(reviews, "a" * 40, client=client,
        binding=GitHubRepositoryBinding(123, "o/r"),
        requirements=ReviewRequirements(True, allowlist)) == state


@pytest.mark.parametrize("status,category", [(404, "not_found"), (403, "permission"),
                                          (502, "transient")])
async def test_root_pr_permission_lookup_failure_has_named_cached_blocker(world, status, category):
    head = await completed(world, "leaf")
    now = [1000.0]
    train, github, _ = await hosted_train(world, clock=lambda: now[0])
    project = await world.db.get_project("p")
    policy = project.hierarchical_integration_policy
    policy["root"]["admission"] = "reviewed"
    await world.db.update_project("p", hierarchical_integration_policy=policy)
    github.reviews = [{"id": 1, "state": "APPROVED", "commit_id": head,
                       "user": {"login": "bob", "type": "User"}}]
    original = github.request_json
    lookup = AsyncMock(side_effect=GitHubAccessError(category, "lookup failed", http_status=status))
    github.request_json = lookup

    visit = await train.visit(MAIN)
    assert visit.state == "blocked" and visit.batch_id is None
    [blocker] = visit.detail["blockers"]
    assert blocker["code"] == "pr_review_permission_unavailable"
    assert str(status) in blocker["reason"]
    now[0] = 1059
    assert (await train.visit(MAIN)).detail["blockers"] == [blocker]
    lookup.assert_awaited_once()
    github.request_json = original
    now[0] = 1060
    assert (await train.visit(MAIN)).state == "testing"


@pytest.mark.parametrize("permission,allowlist,admitted", [
    ("write", frozenset({"github:alice"}), False),
    ("read", frozenset({"github:bob"}), False),
    ("admin", frozenset({"github:BOB"}), True),
])
async def test_daemon_lane_passes_narrow_review_allowlist_to_pr_gate(world, permission, allowlist,
                                                                   admitted):
    head = await completed(world, "leaf")
    train, github, _ = await hosted_train(
        world, review_requirements=ReviewRequirements(True, allowlist))
    project = await world.db.get_project("p")
    policy = project.hierarchical_integration_policy
    policy["root"]["admission"] = "reviewed"
    await world.db.update_project("p", hierarchical_integration_policy=policy)
    github.permissions["bob"] = permission
    github.reviews = [{"id": 1, "state": "APPROVED", "commit_id": head,
                       "user": {"login": "bob", "type": "User"}}]

    visit = await train.visit(MAIN)
    assert visit.state == ("testing" if admitted else "blocked")
    if not admitted:
        assert visit.batch_id is None
        assert visit.detail["blockers"][0]["code"] == "pr_review_missing"


async def test_untrusted_changes_request_does_not_veto_trusted_approval():
    from src.integration.reviews import observe_pull_request_review_state

    client = SimpleNamespace(request_json=AsyncMock(side_effect=[
        {"permission": "write", "user": {"login": "bob"}},
        {"permission": "read", "user": {"login": "outsider"}},
    ]))
    reviews = [{"id": 1, "state": "APPROVED", "commit_id": "a" * 40,
                "user": {"login": "bob", "type": "User"}},
               {"id": 2, "state": "CHANGES_REQUESTED", "commit_id": "a" * 40,
                "user": {"login": "outsider", "type": "User"}}]
    assert await observe_pull_request_review_state(reviews, "a" * 40, client=client,
        binding=GitHubRepositoryBinding(123, "o/r")) == "approved"


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


async def test_train_pause_resume_controls_preview_and_fence_green_publication(world):
    from src.commands.integration_commands import IntegrationCommandsMixin

    await completed(world, "a")
    train, checks, _ = lane(world, LocalGit(Path(world.origin.url)))
    visit = await train.visit(MAIN)
    store = BatchStore(world.db)
    frozen = await store.members(visit.batch_id)
    handler = IntegrationCommandsMixin()
    handler.db = world.db
    handler.orchestrator = SimpleNamespace(integration_train=train)
    args = {"batch_id": visit.batch_id}
    preview = await handler._cmd_integration_pause_batch(args)
    assert preview["outcome"] == "preview" and preview["intent"] == "open"
    assert (await store.get(visit.batch_id)).intent == "open"
    paused = await handler._cmd_integration_pause_batch(args | {"dry_run": False})
    assert paused["intent"] == "paused"
    for dry_run in (True, False):
        repeated = await handler._cmd_integration_pause_batch(args | {"dry_run": dry_run})
        assert repeated["outcome"] == "refused" and not repeated["success"]
    checks.green.add(visit.candidate_sha)
    assert (await train.visit(MAIN)).state == "held"
    status = await IntegrationStatusService(world.db, git_first="active").control_status("p")
    assert status["batches"][0]["intent"] == "paused"
    assert status["blockers"][0]["code"] == "batch_paused"
    preview = await handler._cmd_integration_resume_batch(args)
    assert preview["outcome"] == "preview" and (await store.get(visit.batch_id)).intent == "paused"
    resumed = await handler._cmd_integration_resume_batch(args | {"dry_run": False})
    assert resumed["intent"] == "open"
    for dry_run in (True, False):
        repeated = await handler._cmd_integration_resume_batch(args | {"dry_run": dry_run})
        assert repeated["outcome"] == "refused" and not repeated["success"]
    status = await IntegrationStatusService(world.db, git_first="active").control_status("p")
    assert status["batches"][0]["intent"] == "open"
    assert await store.members(visit.batch_id) == frozen
    assert (await train.visit(MAIN)).state == "delivered"
    assert (await handler._cmd_integration_pause_batch(args | {"dry_run": False}))["outcome"] == "refused"


@pytest.mark.parametrize("paused", [False, True])
async def test_train_eject_preserves_frozen_members_pr_and_readmits_after_cadence(world, paused):
    from src.commands.integration_commands import IntegrationCommandsMixin

    now = [1000.0]
    for tid in ("a", "b", "c"):
        await completed(world, tid)
    async with world.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "c").values(pr_url="https://example/pr/3"))
    train, checks, _ = lane(world, LocalGit(Path(world.origin.url)), clock=lambda: now[0],
                            settling=True)
    visit = await train.visit(MAIN, seal_now=True)
    store = BatchStore(world.db)
    frozen = await store.members(visit.batch_id)
    assert len(frozen) == 3
    if paused:
        await store.set_intent(visit.batch_id, "paused")
    handler = IntegrationCommandsMixin()
    handler.db = world.db
    handler.orchestrator = SimpleNamespace(integration_train=train)
    args = {"batch_id": visit.batch_id, "task_id": "c", "dry_run": True}
    preview = await handler._cmd_integration_eject(args)
    assert preview["outcome"] == "preview", preview
    assert len(await store.members(visit.batch_id)) == 3
    async with world.db._engine.connect() as conn:
        assert len((await conn.execute(select(integration_batches.c.id))).all()) == 1
    ejected = await handler._cmd_integration_eject(args | {"dry_run": False, "reason": "isolate c"})
    assert ejected["outcome"] == "ejected", ejected
    replacement = ejected["replacement_batch_id"]
    assert replacement != visit.batch_id
    assert await store.members(visit.batch_id) == frozen
    assert (await store.get(visit.batch_id)).intent == "aborted"
    assert [m.task_id for m in await store.members(replacement)] == ["a", "b"]
    assert (await store.get(replacement)).intent == ("paused" if paused else "open")
    task = await world.db.get_task("c")
    assert task.pr_url == "https://example/pr/3" and task.status is TaskStatus.COMPLETED
    pending = await train.batches.pending(MAIN, await snapshot(world))
    assert "c" in {m.task_id for m in pending[0]}
    status = await IntegrationStatusService(world.db, git_first="active").control_status("p")
    old = next(b for b in status["batches"] if b["id"] == visit.batch_id)
    assert old["member_disposition"] == "pending"
    if paused:
        assert (await train.visit(MAIN)).state == "held"
        await store.set_intent(replacement, "open")
    tested = await train.visit(MAIN)
    assert tested.batch_id == replacement and tested.state == "testing", tested
    checks.green.add(tested.candidate_sha)
    assert (await train.visit(MAIN)).state == "delivered"
    waiting = await train.visit(MAIN)
    assert waiting.state == "settling" and waiting.detail["task_ids"] == ["c"]
    now[0] += 300
    readmitted = await train.visit(MAIN)
    assert readmitted.state == "testing", readmitted
    assert readmitted.batch_id not in {visit.batch_id, replacement}
    assert [m.task_id for m in await store.members(readmitted.batch_id)] == ["c"]
    assert (await world.db.get_task("c")).pr_url == "https://example/pr/3"


async def test_train_ejected_singleton_gets_new_identity_on_readmission(world):
    await completed(world, "a")
    now = [1000.0]
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)), clock=lambda: now[0], settling=True)
    visit = await train.visit(MAIN, seal_now=True)
    service = (await train.lane_for(MAIN)).service
    result = await TrainControls(world.db).eject(visit.batch_id, "a", service=service,
        dry_run=False, operator_id="operator", reason="retry alone")
    assert result["replacement_batch_id"] is None
    assert (await train.visit(MAIN)).state == "settling"
    now[0] += 300
    next_visit = await train.visit(MAIN)
    assert next_visit.state == "testing" and next_visit.batch_id != visit.batch_id


async def test_train_eject_rolls_back_abort_if_replacement_freeze_fails(world, monkeypatch):
    from src.database.tables import task_metadata

    await completed(world, "a")
    await completed(world, "b")
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)))
    visit = await train.visit(MAIN)
    service = (await train.lane_for(MAIN)).service
    monkeypatch.setattr(service.store, "_freeze_on", AsyncMock(side_effect=ValueError("freeze failed")))
    with pytest.raises(ValueError, match="freeze failed"):
        await TrainControls(world.db).eject(visit.batch_id, "b", service=service,
            dry_run=False, operator_id="operator", reason="isolate b")
    assert (await service.store.get(visit.batch_id)).intent == "open"
    async with world.db._engine.connect() as conn:
        assert len((await conn.execute(select(integration_batches.c.id))).all()) == 1
        assert not (await conn.execute(select(task_metadata).where(
            task_metadata.c.key.like("integration_train_ejection:%"),
        ))).first()
        assert await conn.scalar(select(integration_batches.c.ejection_record)) is None


async def test_train_eject_refuses_dependency_and_rolls_back_changed_source(world, monkeypatch):
    await completed(world, "a")
    await completed(world, "b", needs=("a",))
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)))
    visit = await train.visit(MAIN)
    service = (await train.lane_for(MAIN)).service
    controls = TrainControls(world.db)
    with pytest.raises(ValueError, match="undelivered prerequisite"):
        await controls.eject(visit.batch_id, "a", service=service,
            dry_run=False, operator_id="operator", reason="remove prerequisite")
    monkeypatch.setattr("src.integration.git_truth.GitTruthSnapshot.is_fresh",
                        AsyncMock(return_value=False))
    with pytest.raises(ValueError, match="batch sources, target or candidate changed"):
        await controls.eject(visit.batch_id, "b", service=service,
            dry_run=False, operator_id="operator", reason="changed")
    assert (await service.store.get(visit.batch_id)).intent == "open"
    assert len(await service.store.members(visit.batch_id)) == 2


async def test_train_seal_now_command_previews_and_bypasses_cadence_once(world):
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.database.tables import task_metadata

    await completed(world, "a")
    now = [1000.0]
    train, checks, _ = lane(world, LocalGit(Path(world.origin.url)), clock=lambda: now[0],
                            settling=True)
    handler = IntegrationCommandsMixin()
    handler.db = world.db
    handler.orchestrator = SimpleNamespace(integration_train=train)
    preview = await handler._cmd_integration_seal_now({"project_id": "p"})
    assert preview["outcome"] == "preview" and preview["members"] == ["a"]
    async with world.db._engine.connect() as conn:
        assert not (await conn.execute(select(integration_batches))).first()
        assert not (await conn.execute(select(task_metadata))).first()
    sealed = await handler._cmd_integration_seal_now({"project_id": "p", "dry_run": False})
    assert sealed["outcome"] == "sealed", sealed
    assert sealed["batch_id"] is not None
    existing = await handler._cmd_integration_seal_now({"project_id": "p", "dry_run": False})
    assert existing["outcome"] == "existing_batch" and existing["batch_id"] == sealed["batch_id"]
    visit = await train.visit(MAIN)
    assert visit.state == "testing"
    checks.green.add(visit.candidate_sha)
    assert (await train.visit(MAIN)).state == "delivered"
    await completed(world, "next-leaf")
    assert (await train.visit(MAIN)).state == "settling"


async def test_train_seal_now_command_preserves_red_pr_gate(world):
    from src.commands.integration_commands import IntegrationCommandsMixin

    train, github, _ = await hosted_train(world, settling=True)
    source = await completed(world, "a")
    github.pr_runs[source] = "failure"
    handler = IntegrationCommandsMixin()
    handler.db = world.db
    handler.orchestrator = SimpleNamespace(integration_train=train)
    result = await handler._cmd_integration_seal_now({"project_id": "p", "dry_run": False})
    assert result["outcome"] == "no_ready_work", result
    assert result["blockers"][0]["code"] == "pr_checks_red"
    assert await train.batches.current(MAIN) is None


@pytest.mark.parametrize("operator_id", ["human:local-operator", "service:integration-train"])
async def test_worker_cannot_set_ejection_marker_or_release_aborted_sources(world, operator_id):
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.commands.surface_commands import SurfaceCommandsMixin
    from src.profiles.capabilities import DENY_ALL

    await completed(world, "a")
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)))
    visit = await train.visit(MAIN)
    await BatchStore(world.db).set_intent(visit.batch_id, "aborted")
    handler = SurfaceCommandsMixin()
    handler.db = world.db
    handler._current_scope = {"kind": "session", "task_id": "a", "project_id": "p"}
    with principal_context(ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL,
                                              task_id="a", project_id="p")):
        result = await handler._cmd_task_set({"task_id": "a", "description": "forged",
            "meta": {"integration_train_ejection:" + visit.batch_id: {
                "batch_id": visit.batch_id, "project_id": "p", "task_id": "a",
                "operator_id": operator_id, "reason": "stacked source was refreshed"}}})
    assert "reserved" in result["error"]
    assert (await world.db.get_task("a")).description == ""
    assert await world.db.get_task_meta("a", "integration_train_ejection:" + visit.batch_id) is None
    assert await train.batches.pending(MAIN, await snapshot(world)) is None


@pytest.mark.parametrize("marker_task", ["a", "unrelated", "foreign"])
@pytest.mark.parametrize("operator_id", ["human:local-operator", "service:integration-train"])
async def test_task_metadata_ejection_markers_never_release_aborted_batch(world, marker_task,
                                                                      operator_id):
    from src.integration.batches import ejection_instruction

    await completed(world, "a")
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)))
    visit = await train.visit(MAIN)
    await BatchStore(world.db).set_intent(visit.batch_id, "aborted")
    if marker_task != "a":
        await world.db.create_project(Project(id="other", name="Other"))
        await world.db.create_task(Task(id=marker_task,
            project_id="other" if marker_task == "foreign" else "p",
            title="unrelated", description=""))
    await world.db.set_task_meta(marker_task, "integration_train_ejection:" + visit.batch_id,
        {"batch_id": visit.batch_id, "project_id": "p", "task_id": "a",
         "operator_id": operator_id, "reason": "stacked source was refreshed"})
    async with world.db._engine.connect() as conn:
        assert not await conn.scalar(select(ejection_instruction(visit.batch_id)))
    assert await train.batches.pending(MAIN, await snapshot(world)) is None
    status = await IntegrationStatusService(world.db, git_first="active").control_status("p")
    assert status["batches"][0]["member_disposition"] == "withheld"


async def test_superseded_batch_releases_inputs_with_daemon_owned_record(world):
    from src.integration.batches import ejection_instruction

    for tid in ("a", "b"):
        await completed(world, tid)
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)))
    visit = await train.visit(MAIN)
    store = BatchStore(world.db)
    batch = await store.get(visit.batch_id)
    reason = "stacked source of a was refreshed; a new batch replaces it"
    assert await store.supersede(batch, "a", reason=reason)
    assert (await store.get(batch.id)).intent == "aborted"
    async with world.db._engine.connect() as conn:
        assert await conn.scalar(select(ejection_instruction(batch.id)))
        record = await conn.scalar(select(integration_batches.c.ejection_record).where(
            integration_batches.c.id == batch.id))
        event = await conn.scalar(select(events.c.payload).where(
            events.c.event_type == "integration.batch_superseded"))
    assert record == {"batch_id": batch.id, "project_id": "p", "task_id": "a",
                      "replacement_batch_id": None, "operator_id": "service:integration-train",
                      "reason": reason}
    assert json.loads(event) == record
    assert await world.db.get_task_meta("a", "integration_train_ejection:" + batch.id) is None
    pending = await train.batches.pending(MAIN, await snapshot(world))
    assert {member.task_id for member in pending[0]} == {"a", "b"}


@pytest.mark.parametrize("intent", ["paused", "aborted"])
async def test_supersede_preserves_explicit_operator_hold(world, intent):
    await completed(world, "a")
    train, checks, _ = lane(world, LocalGit(Path(world.origin.url)))
    target_head = git(world.origin.url, "rev-parse", MAIN.target_ref)
    visit = await train.visit(MAIN)
    store = BatchStore(world.db)
    batch = await store.get(visit.batch_id)
    await store.set_intent(batch.id, intent)
    assert not await store.supersede(batch, "a", reason="stacked source was refreshed")
    assert (await store.get(batch.id)).intent == intent
    async with world.db._engine.connect() as conn:
        assert await conn.scalar(select(integration_batches.c.ejection_record).where(
            integration_batches.c.id == batch.id)) is None
    if intent == "paused":
        assert (await train.batches.current(MAIN)).id == batch.id
        checks.green.add(visit.candidate_sha)
        held = await train.visit(MAIN)
        assert held.batch_id == batch.id and held.state == "held"
        assert git(world.origin.url, "rev-parse", MAIN.target_ref) == target_head
    else:
        assert await train.batches.pending(MAIN, await snapshot(world)) is None


@pytest.mark.parametrize("task_id,reason", [
    ("not-a-member", "refreshed"), ("a", " "),
])
async def test_supersede_requires_frozen_member_project_and_reason(world, task_id, reason):
    await completed(world, "a")
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)))
    visit = await train.visit(MAIN)
    store = BatchStore(world.db)
    batch = await store.get(visit.batch_id)
    with pytest.raises(ValueError, match="supersede") as refusal:
        await store.supersede(batch, task_id, reason=reason)
    assert type(refusal.value) is ValueError  # Other refusals must not become recoverable blockers.
    assert (await store.get(batch.id)).intent == "open"
    async with world.db._engine.connect() as conn:
        assert await conn.scalar(select(integration_batches.c.ejection_record).where(
            integration_batches.c.id == batch.id)) is None


@pytest.mark.parametrize("task_id,reason,foreign", [
    ("not-a-member", "isolate", False), ("a", " ", False), ("a", "isolate", True),
])
async def test_eject_keeps_plain_refusals_for_non_member_reason_and_project(
    world, task_id, reason, foreign,
):
    await completed(world, "a")
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)))
    visit = await train.visit(MAIN)
    store = BatchStore(world.db)
    batch = await store.get(visit.batch_id)
    frozen = await store.members(batch.id)
    if foreign:
        await world.db.create_project(Project(id="foreign", name="Foreign"))
        async with world.db._engine.begin() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == "a").values(project_id="foreign"))
    authorize = AsyncMock(return_value=True)
    with pytest.raises(ValueError, match="eject|frozen batch membership") as refusal:
        await store.eject(batch, task_id, None, (), trees={}, authorize=authorize,
                          dry_run=False, operator_id="human:local-operator", reason=reason)
    assert type(refusal.value) is ValueError
    authorize.assert_not_awaited()
    assert await store.get(batch.id) == batch
    assert await store.members(batch.id) == frozen
    async with world.db._engine.connect() as conn:
        assert await conn.scalar(select(integration_batches.c.ejection_record).where(
            integration_batches.c.id == batch.id)) is None


@pytest.mark.parametrize("change", [
    {"task_id": "not-a-member"}, {"project_id": "foreign"}, {"batch_id": "another-batch"},
    {"operator_id": ""}, {"reason": ""},
])
async def test_ejection_record_requires_frozen_member_project_and_audit_binding(world, change):
    from src.integration.batches import ejection_instruction

    await completed(world, "a")
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)))
    visit = await train.visit(MAIN)
    await BatchStore(world.db).set_intent(visit.batch_id, "aborted")
    record = {"batch_id": visit.batch_id, "project_id": "p", "task_id": "a",
              "operator_id": "human:local-operator", "reason": "isolate a"} | change
    async with world.db._engine.begin() as conn:
        await conn.execute(update(integration_batches).where(
            integration_batches.c.id == visit.batch_id).values(ejection_record=record))
        assert not await conn.scalar(select(ejection_instruction(visit.batch_id)))
    assert await train.batches.pending(MAIN, await snapshot(world)) is None


async def test_ejection_instruction_survives_member_archive(world):
    from src.integration.batches import ejection_instruction

    for tid in ("a", "b"):
        await completed(world, tid)
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)))
    visit = await train.visit(MAIN)
    service = (await train.lane_for(MAIN)).service
    result = await TrainControls(world.db).eject(visit.batch_id, "b", service=service,
        dry_run=False, operator_id="human:local-operator", reason="isolate b")
    await world.db.archive_task("b", abandon_undelivered=True, abandon_reason="archive b")
    assert await world.db.get_task("b") is None
    async with world.db._engine.connect() as conn:
        assert await conn.scalar(select(ejection_instruction(visit.batch_id)))
        record = await conn.scalar(select(integration_batches.c.ejection_record).where(
            integration_batches.c.id == visit.batch_id))
    assert record["task_id"] == "b" and record["replacement_batch_id"] == result["replacement_batch_id"]
    pending = await train.batches.pending(MAIN, await snapshot(world))
    assert {m.task_id for m in pending[0]} == {"a"}


async def test_train_controls_refuse_supervisor_from_another_project(world, monkeypatch):
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import DENY_ALL

    await completed(world, "a")
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)))
    visit = await train.visit(MAIN)
    handler = IntegrationCommandsMixin()
    handler.db = world.db
    handler.orchestrator = SimpleNamespace(integration_train=train)
    monkeypatch.setattr(world.db, "get_session", AsyncMock(return_value=SimpleNamespace(
        id="other-supervisor", profile_id="supervisor", lifecycle="named", state="running",
        desired_state="running", project_id="other")))
    with principal_context(ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL,
            session_id="other-supervisor", elevated=True, project_id="other")):
        for command, args in (
            (handler._cmd_integration_pause_batch, {"batch_id": visit.batch_id}),
            (handler._cmd_integration_resume_batch, {"batch_id": visit.batch_id}),
            (handler._cmd_integration_eject, {"batch_id": visit.batch_id, "task_id": "a",
                                             "reason": "foreign control"}),
            (handler._cmd_integration_seal_now, {"project_id": "p"}),
        ):
            result = await command(args | {"dry_run": False})
            assert result["outcome"] == "unauthorized" and "another project" in result["error"]
        for task_id in ("a", "", None):
            foreign = await handler._cmd_integration_eject({"batch_id": visit.batch_id,
                "task_id": task_id, "reason": "foreign control"})
            missing = await handler._cmd_integration_eject({"batch_id": "missing",
                "task_id": task_id, "reason": "foreign control"})
            assert missing == foreign
    assert (await BatchStore(world.db).get(visit.batch_id)).intent == "open"


@pytest.mark.parametrize("dry_run", [True, False])
async def test_train_eject_reports_unknown_batch_and_non_member(world, dry_run):
    from src.commands.integration_commands import IntegrationCommandsMixin

    await completed(world, "a")
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)))
    visit = await train.visit(MAIN)
    handler = IntegrationCommandsMixin()
    handler.db = world.db
    handler.orchestrator = SimpleNamespace(integration_train=train)
    for requested_batch, outcome in ((visit.batch_id, "not_a_member"), ("missing", "unknown_batch")):
        result = await handler._cmd_integration_eject({"batch_id": requested_batch,
            "task_id": "not-a-member", "reason": "isolate", "dry_run": dry_run})
        assert result["outcome"] == outcome and not result["success"]
    assert (await BatchStore(world.db).get(visit.batch_id)).intent == "open"


@pytest.mark.parametrize("project_id,outcome", [("p", "unauthorized"), (None, "unknown_batch")])
async def test_train_eject_missing_batch_requires_global_authority(world, monkeypatch,
                                                                 project_id, outcome):
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import DENY_ALL

    handler = IntegrationCommandsMixin()
    handler.db = world.db
    legacy_get = AsyncMock(wraps=world.db.get_integration_batch)
    monkeypatch.setattr(world.db, "get_integration_batch", legacy_get)
    monkeypatch.setattr(world.db, "get_session", AsyncMock(return_value=SimpleNamespace(
        id="supervisor", profile_id="supervisor", lifecycle="named", state="running",
        desired_state="running", project_id=project_id)))
    with principal_context(ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL,
            session_id="supervisor", elevated=True, project_id=project_id)):
        result = await handler._cmd_integration_eject({"batch_id": "missing", "task_id": "a"})
    assert result["outcome"] == outcome and not result["success"]
    if project_id is None:
        legacy_get.assert_awaited_once_with("missing")
    else:
        legacy_get.assert_not_awaited()


@pytest.mark.parametrize("kind,elevated", [("session", False), ("session", True),
                                         ("playbook", True), ("service", False)])
async def test_train_eject_authorizes_before_batch_lookup(monkeypatch, kind, elevated):
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import DENY_ALL

    handler = IntegrationCommandsMixin()
    handler.db = SimpleNamespace(get_session=AsyncMock(return_value=None),
                                 get_integration_batch=AsyncMock())
    get = AsyncMock()
    monkeypatch.setattr(BatchStore, "get", get)
    with principal_context(ExecutionPrincipal(kind=PrincipalKind(kind), policy=DENY_ALL,
            session_id="session", elevated=elevated, project_id="p")):
        result = await handler._cmd_integration_eject({"batch_id": "batch", "task_id": "a"})
    assert result["outcome"] == "unauthorized" and not result["success"]
    get.assert_not_awaited()
    handler.db.get_integration_batch.assert_not_awaited()


@pytest.mark.parametrize("batch_id", [None, "", "   "])
async def test_train_eject_empty_batch_id_is_invalid_before_lookup(monkeypatch, batch_id):
    from src.commands.integration_commands import IntegrationCommandsMixin

    handler = IntegrationCommandsMixin()
    handler.db = SimpleNamespace(get_integration_batch=AsyncMock())
    get = AsyncMock()
    monkeypatch.setattr(BatchStore, "get", get)
    result = await handler._cmd_integration_eject({"batch_id": batch_id, "task_id": "a"})
    assert result["outcome"] == "invalid_state" and not result["success"]
    get.assert_not_awaited()
    handler.db.get_integration_batch.assert_not_awaited()


@pytest.mark.parametrize("project_id", ["p", None])
async def test_train_eject_preview_admits_live_own_project_and_global_supervisors(world, monkeypatch,
                                                                              project_id):
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import DENY_ALL

    await completed(world, "a")
    train, _, _ = lane(world, LocalGit(Path(world.origin.url)))
    visit = await train.visit(MAIN)
    handler = IntegrationCommandsMixin()
    handler.db = world.db
    handler.orchestrator = SimpleNamespace(integration_train=train)
    monkeypatch.setattr(world.db, "get_session", AsyncMock(return_value=SimpleNamespace(
        id="supervisor", profile_id="supervisor", lifecycle="named", state="running",
        desired_state="running", project_id=project_id)))
    with principal_context(ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL,
            session_id="supervisor", elevated=True, project_id=project_id)):
        result = await handler._cmd_integration_eject({"batch_id": visit.batch_id, "task_id": "a"})
    assert result["success"] and result["outcome"] == "preview", result
    assert (await BatchStore(world.db).get(visit.batch_id)).intent == "open"


async def test_train_seal_now_with_only_epic_targets_refuses(world):
    from src.commands.integration_commands import IntegrationCommandsMixin

    targets = SimpleNamespace(targets=AsyncMock(return_value=[
        TrainTarget("p", "r", "refs/heads/aq/epic", "epic")]))
    handler = IntegrationCommandsMixin()
    handler.db = world.db
    handler.orchestrator = SimpleNamespace(integration_train=SimpleNamespace(targets=targets))
    for dry_run in (True, False):
        result = await handler._cmd_integration_seal_now({"project_id": "p", "dry_run": dry_run})
        assert result["outcome"] == "refused" and "root train target" in result["error"]

async def local_ci_policy(world, *, source="local", overrides=None):
    """Valid fixture policy; never change an operator project's policy."""
    from tests.test_integration_service import _minimal_policy_values

    policy = _minimal_policy_values()
    required = {"version": "v1", "names": ["unit"], "producer_id": "15368"}
    for boundary in ("root", "parent"):
        policy[boundary] = {**policy[boundary], "required_checks": required,
                            "admission": "authorized"}
    policy["ci"] = {"source": source, "commands": {"unit": "ruff check ."}, **(overrides or {})}
    await world.db.update_project("p", hierarchical_integration_policy=policy)
    return policy


@pytest.mark.parametrize("require_hosted", [True, False])
async def test_hybrid_root_requires_hosted_candidate_checks_and_app_attestation_by_default(
    world, tmp_path, require_hosted,
):
    from src.integration.checks import HybridChecks, LocalChecks

    head = await completed(world, "leaf")
    now = [time.time()]
    train, github, _ = await hosted_train(world, clock=lambda: now[0])
    base = git(world.origin.url, "rev-parse", "main")
    await local_ci_policy(world, source="hybrid", overrides=(
        {} if require_hosted else {"hosted_attestation": {"root": False}}))
    orchestrator = train.lane_for.orchestrator
    orchestrator._command_handler = jobs_handler(world.db, tmp_path)
    unreviewed = await train.visit(MAIN)
    assert unreviewed.batch_id is None
    assert unreviewed.detail["blockers"][0]["code"] == "pr_review_missing"
    assert await job_rows(world.db) == []
    approve_pr(github, head)
    now[0] += 61  # the PR gate keeps a refusal until its retry is due
    testing = await train.visit(MAIN)
    assert testing.state == "testing", testing
    [job] = await job_rows(world.db)
    await finish(world.db, job, exit_code=0)
    lane = await train.lane_for(MAIN)
    batch = await BatchStore(world.db).get(testing.batch_id)
    exact = await lane.checks.for_candidate(batch, testing.candidate_sha)
    assert isinstance(exact, HybridChecks) if require_hosted else isinstance(exact.provider, LocalChecks)
    assert lane.service.require_attestation is require_hosted
    if require_hosted:
        # A successful local check cannot supply hosted candidate checks.
        assert (await train.visit(MAIN)).state != "delivered"
        assert git(world.origin.url, "rev-parse", "main") == base
        github.runs[testing.candidate_sha] = "success"
        attestation = orchestrator.integration_attestation_service
        orchestrator.integration_attestation_service = None
        refused = await train.visit(MAIN)
        assert refused.state != "delivered" and not github.records
        assert git(world.origin.url, "rev-parse", "main") == base
        orchestrator.integration_attestation_service = attestation
    delivered = await train.visit(MAIN)
    assert delivered.state == "delivered", delivered
    assert git(world.origin.url, "rev-parse", "main") == testing.candidate_sha
    assert len(github.records) == int(require_hosted)


async def test_local_epic_completion_reads_plan_bound_green_and_invalidates_changed_commands(
    collected_epic, tmp_path,
):
    case = collected_epic
    policy = await local_ci_policy(case.world)
    case.train.lane_for.orchestrator._command_handler = jobs_handler(case.db, tmp_path)
    testing = await case.train.visit(case.target)
    assert testing.state == "testing", testing
    for job in await job_rows(case.db):
        await finish(case.db, job, exit_code=0)
    delivered = await case.train.visit(case.target)
    assert delivered.state == "delivered", delivered
    head = delivered.target_sha
    await review_epic(case)
    settled = await case.train.visit(case.target)
    assert settled.state == "idle", settled
    completion = await case.db.get_task_completion("epic")
    assert completion and completion.commits == [head]
    # The completion path must not trust the green row after a command edit.
    before = len(await job_rows(case.db))
    policy["ci"]["commands"]["unit"] = "ruff check src"
    await case.db.update_project("p", hierarchical_integration_policy=policy)
    pending = await case.train.visit(case.target)
    assert pending.state != "delivered"
    jobs_after = await job_rows(case.db)
    assert len(jobs_after) > before
    assert any(job["input_ref"] == head and job["state"] == "queued" for job in jobs_after)
    # Policy projection uses the same command-bound producer as the reader.
    from src.integration.runtime_contracts import HeadIdentity
    from src.integration.train_sources import epic_policy_on
    async with case.db._engine.connect() as conn:
        row = (await conn.execute(select(tasks).where(tasks.c.id == "epic"))).mappings().one()
        project = (await conn.execute(select(projects).where(projects.c.id == "p"))).mappings().one()
        projected = await epic_policy_on(conn, row, project)
    lane = await case.train.lane_for(case.target)
    exact = await lane.checks.for_candidate(Batch("read", "p", "r", case.target.target_ref), head)
    assert projected.check_trust == exact.required.producer_id
    assert not (await exact.read(HeadIdentity(repository_id="r", ref=case.target.target_ref,
                                            sha=head, generation=0))).green


@pytest.mark.parametrize("train_produced", [True, False])
async def test_local_default_branch_sync_requires_plan_bound_checks_and_train_provenance(
    collected_epic, tmp_path, train_produced,
):
    from src.integration.models import integration_ci_policy
    from src.integration.train import candidate_head

    case = collected_epic
    main, testing = await preexisting_epic_with_default_fix(case,
        provenance="train_candidate" if train_produced else "none")
    policy = await local_ci_policy(case.world)
    lanes = case.train.lane_for
    lanes.orchestrator._command_handler = jobs_handler(case.db, tmp_path)
    batch = await BatchStore(case.db).get(testing.batch_id)
    ci = integration_ci_policy(policy)
    root = lanes._local_checks(MAIN, case.origin.clone, ci, policy, batch)
    from src.integration.train_sources import retain_train_candidate

    await retain_train_candidate(lanes.git, case.origin.clone, batch, main)
    head = candidate_head(batch, main)
    await root.request(head)
    [job] = await job_rows(case.db)
    await finish(case.db, job, exit_code=0)
    assert (await root.refresh(head)).green
    lane = await lanes(case.target)
    observed = await lane.snapshot()
    decision = await lane.sync_default_branch(batch, observed, testing.candidate_sha, ("unit",))
    if train_produced:
        assert decision == {"ref": MAIN.target_ref, "sha": main, "checks": ["unit"],
                            "provenance": "train_candidate"}
        policy["ci"]["commands"]["unit"] = "ruff check src"
        await case.db.update_project("p", hierarchical_integration_policy=policy)
        changed = await lanes(case.target)
        assert await changed.sync_default_branch(batch, observed, testing.candidate_sha,
                                                 ("unit",)) is None
    else:
        assert decision is None  # Local green alone cannot prove a non-train main head.


async def test_hybrid_epic_hosted_requirement_changes_completion_trust(collected_epic):
    from src.integration.checks import HybridChecks
    from src.integration.train_sources import epic_policy_on

    case = collected_epic
    await local_ci_policy(case.world)

    async def projected():
        async with case.db._engine.connect() as conn:
            row = (await conn.execute(select(tasks).where(tasks.c.id == "epic"))).mappings().one()
            project = (await conn.execute(select(projects).where(projects.c.id == "p"))).mappings().one()
            return await epic_policy_on(conn, row, project)

    before = await projected()
    await local_ci_policy(case.world, source="hybrid",
                          overrides={"hosted_attestation": {"epic": True}})
    after = await projected()
    assert before.check_trust != after.check_trust
    lane = await case.train.lane_for(case.target)
    assert lane.service.require_attestation
    batch = Batch("epic-trust", "p", "r", case.target.target_ref)
    exact = await lane.checks.for_candidate(batch, case.base)
    assert isinstance(exact, HybridChecks)
    assert after.check_trust == exact.required.producer_id
