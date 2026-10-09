"""Integration-train regressions: the 2026-10-03/04 overnight failure modes.

One end-to-end scenario per failure mode that needed a hand fix overnight, each on
disposable PostgreSQL and real Git (a bare origin plus clones), driven through the
entry point the train or the operator actually used:

1. Generated ``tests/selection_catalogue.json`` overlaps blocked every child
   promotion into an epic branch (prime-vault-27): ``delivery_promote``.
2. Multi-child epics were never admitted: no exact ``parent`` review evidence,
   and a PR head advanced by a merge of main was never reverified (keen-quest-99).
3. Repair delegates could not claim after a stage handoff while the previous
   stage's writer lingered (keen-cascade-74): the session reconciler and the
   claim-time origin/fence check.
4. A verifier that closed FAIL left a released epic owner, and reopen-collection
   had to collect the completed fix child past it (clear-ember-89).

These drive the Subject runtimes (``integration.git_first: shadow``, the default).
The Git-first train (``active``) replaces them with per-target visits that file
ordinary repair tasks; its suites are ``tests/test_integration_train*.py``.

Scenarios the original suite also carried, dropped because current code covers
them elsewhere or retired their entry point:

* keen-grove-22, crisp-cascade-19, noble-journey-92, fresh-meadow-81: they drove
  ``aq integration adopt --settle-delivered-children``, retired with the legacy
  recovery controls (a99125ebc).
* crisp-horizon-90 (escalated parent, every child delivered, through
  ``recover-parent-head``): ``test_integration_parent_completion.py`` replays the
  captured epic and the receipt-covered final stage
  (``test_finished_collection_replays_captured_discord_epic``,
  ``test_finished_collection_reconciles_both_gaps_and_files_fresh_verifier``,
  ``test_finished_collection_selects_one_exact_close_among_retries``).
* A held red verifier stranding the fix child (eager-apex-93, landed as
  b3e988930): ``test_integration_cancelled_collection.py``
  ``test_held_red_verifier_recovery_preserves_evidence_and_detach_proof`` and its
  siblings.

The fixtures and builders are borrowed from the suites that own each mechanism,
so a scenario here exercises the same setup those suites keep honest.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, select, update

from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import ExecutionPrincipal, principal_context
from src.config import AppConfig, DatabaseConfig
from src.database import tables as t
from src.git.manager import GitManager
from src.integration.github_review_poll import GitHubReviewPoller
from src.integration.models import BranchKey, Fence, PromotionInput
from src.integration.ownership import BranchBusy, BranchOwnership, StaleFence
from src.integration.promotion import PromotionService
from src.integration.repair import RepairService
from src.models import TaskStatus
from src.orchestrator.core import Orchestrator
from src.sessions import SessionProviderRegistry
from src.sessions.fake import FakeProvider
from src.sessions.provider import SessionSpec
from src.sessions.reconciler import SessionReconciler
from src.test_selection.catalogue import CATALOGUE_PATH
from tests import test_integration_cancelled_collection as cancelled_tests
from tests import test_integration_owner_recovery as owner_tests
from tests import test_integration_parent_source as parent_source
from tests import test_integration_promotion as promotion_tests
from tests.db_fixtures import lease_dsn
from tests.integration_primitive_scope import authorize_root_primitives
from tests.test_development_integration import git
from tests.test_epic_pr_review_evidence import _ReviewGit
from tests.test_generated_artifacts import _catalogue_branches
from tests.test_integration_cancelled_collection import (
    _failed_aggregate,
    _operation,
    _owner,
    _promote_next,
    _remote_tip,
    _rows,
)
from tests.test_integration_parent_source import _finish, _members, _ParentClient, _source
from tests.test_integration_promotion import _review
from tests.test_integration_repair_rollover import _batch_writer, _stage

# Fixtures shared with the suites that own them, bound by assignment so the
# scenarios' own ``db`` locals do not shadow an import.
promotion_db = promotion_tests.db  # 1: promotion database with a repo row
db = parent_source.db  # 2: requested by ``completed`` by name
completed = parent_source.completed  # 2: a collected, verified two-child epic
env = owner_tests.env  # 3: PostgreSQL + origin for the repair-stage writer
train_epic = cancelled_tests.case  # 4: train epic with a red aggregate and a fix child


# --- 1. Generated catalogue overlap (prime-vault-27) ------------------------------------------
# A child promotion into an epic branch, through ``delivery_promote``.


def _commit(repo, branch: str, path: str, text: str) -> str:
    git(repo, "switch", "-q", branch)
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    git(repo, "add", path)
    git(repo, "commit", "-qm", f"{branch}: {path}")
    return git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("source_conflict", [False, True], ids=["generated-only", "source-conflict"])
async def test_prime_vault_27_delivery_promote_regenerates_catalogue_overlap(
    promotion_db, tmp_path, command_handler_factory, source_conflict
):
    """prime-vault-27: generated catalogue conflicts blocked every parent PR.

    A child and its epic branch each add a test module (so each regenerated
    ``tests/selection_catalogue.json``) and each change a disjoint real source
    file.  The collector's ``delivery_promote`` must rebuild the catalogue from
    the merged sources and promote without a conflict.  A genuine source
    conflict alongside the generated overlap must still be reported as one.
    """
    database = promotion_db
    work, base, _, _ = _catalogue_branches(tmp_path)
    source = _commit(work, "other", "src/child_feature.py", "CHILD = 1\n")
    target = _commit(work, "main", "src/parent_feature.py", "PARENT = 1\n")
    if source_conflict:
        source = _commit(work, "other", "shared.txt", "child\n")
        target = _commit(work, "main", "shared.txt", "parent\n")
    origin = tmp_path / "catalogue-origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(work), str(origin))
    git(origin, "update-ref", "refs/heads/aq/parent", target)
    git(origin, "update-ref", "refs/heads/aq/child", source)
    await database.update_repo("repo", url=str(origin))
    async with database.immediate() as conn:
        await conn.execute(insert(t.task_branch_origins).values(
            id="origin", task_id="child", repository_id="repo", parent_task_id="parent",
            parent_repository_id="repo", parent_ref="aq/parent", base_sha=base,
            creation_generation=0, reserved=True, materialized=True,
            created_at=1.0, materialized_at=1.0,
        ))
    review = _review(evidence_id="catalogue-review", generation=0)
    review.update(
        source_base=base, reviewed_head_sha=source,
        reviewed_tree_sha=git(origin, "rev-parse", f"{source}^{{tree}}"),
        reviewer_session_attempt_id=None,
    )
    await database.append_integration_review_evidence(review)
    fence = await BranchOwnership(database).acquire(
        BranchKey(repository_id="repo", branch="aq/parent"), "collector-op", "collector"
    )
    handler = await command_handler_factory()
    await handler.orchestrator.db.close()
    handler.orchestrator.db = database
    handler.orchestrator.promotion_service = PromotionService(
        database, data_dir=tmp_path / "data", git_manager=GitManager()
    )
    args = PromotionInput(
        operation_key="collector-op", source_task_id="child", source_base=base,
        source_head=source, expected_target=target, fence=fence,
    ).model_dump(mode="json")

    result = await handler.execute("delivery_promote", args)

    intent = await database.get_integration_promotion_intent(result["intent_id"])
    async with database._engine.connect() as conn:
        receipts = await conn.scalar(select(func.count()).select_from(t.task_delivery_receipts))
    if source_conflict:
        assert result["success"] is False and result["outcome"] == "conflict", result
        assert intent["state"] == "conflict" and intent["prepared_sha"] is None
        assert "shared.txt" in intent["conflict_diagnostics"]["paths"]
        assert git(origin, "rev-parse", "aq/parent") == target
        assert receipts == 0
        return
    assert result["success"] is True and result["outcome"] == "promoted", result
    assert intent["state"] == "committed"
    assert receipts == 1
    promoted = git(origin, "rev-parse", "aq/parent")
    assert promoted == result["prepared_sha"]
    assert git(origin, "show", "-s", "--format=%P", promoted) == f"{target} {source}"
    catalogue = json.loads(git(origin, "show", f"{promoted}:{CATALOGUE_PATH}"))
    assert set(catalogue["modules"]) == {
        "tests/test_a.py", "tests/test_b.py", "tests/test_c.py",
    }
    assert git(origin, "show", f"{promoted}:src/child_feature.py") == "CHILD = 1"
    assert git(origin, "show", f"{promoted}:src/parent_feature.py") == "PARENT = 1"


# --- 2. Multi-child epic admission (keen-quest-99) --------------------------------------------
# Exact ``parent`` review evidence, then reverification after a merge of main.


def _merge_main_into_parent(origin) -> str:
    """Advance ``main``, then merge it into the open epic PR head, as the operator did."""
    clone = origin.clone
    git(clone, "fetch", "-q", "origin")
    git(clone, "checkout", "-q", "-B", "main", "origin/main")
    (clone / "main-advance.txt").write_text("main moved on\n")
    git(clone, "add", "main-advance.txt")
    git(clone, "commit", "-q", "-m", "main advances")
    git(clone, "push", "-q", "origin", "main")
    git(clone, "checkout", "-q", "-B", "aq/parent", "origin/aq/parent")
    git(clone, "merge", "-q", "--no-ff", "-m", "Merge origin/main into aq/parent", "main")
    git(clone, "push", "-q", "origin", "aq/parent")
    return git(clone, "rev-parse", "HEAD")


@pytest.mark.parametrize("kind", ["feature", "container"])
async def test_keen_quest_99_multi_child_epic_admitted_and_reverified_after_main_merge(
    completed, kind
):
    """keen-quest-99: the train never admitted multi-child epic PRs.

    A two-child epic is collected through real promotions, verified by trusted
    CI and completed.  Completion through ``integration_complete_parent`` must
    produce exact ``parent`` review evidence bound to the verified head, so the
    train admits it.  ``container`` is an epic container (``task_type`` null)
    admitted by the root allowlist.  When the PR head then advances by a merge
    of ``origin/main``, the old evidence must stop admitting it and the review
    poller's parent-head handler must start a fresh verification whose
    completion produces new exact evidence bound to the new head.
    """
    case = completed
    if kind == "container":
        async with case.db.immediate() as conn:
            policy = dict(await conn.scalar(select(t.projects.c.hierarchical_integration_policy)))
            policy["root"] = {**policy["root"], "authorized_task_ids": ["parent"]}
            await conn.execute(update(t.projects).values(hierarchical_integration_policy=policy))
            await conn.execute(
                update(t.tasks).where(t.tasks.c.id == "parent").values(task_type=None)
            )
    initial = await _source(case)
    assert initial["review_kind"] == "parent"
    # The incident: verified and completed, yet no parent evidence, so never admitted.
    assert await _members(case, green=True) == []
    completed_again = await case.commands._cmd_integration_complete_parent(
        {"task_id": "parent", "generation": initial["generation"], "head_sha": initial["head"]}
    )
    assert completed_again["success"], completed_again
    members = await _members(case)
    assert [member["task_id"] for member in members] == ["parent"]
    first_review = members[0]["review"]
    assert first_review["review_kind"] == "parent"
    assert first_review["reviewed_head_sha"] == initial["head"]
    assert first_review["evidence"]["verification_id"] == initial["verification_id"]

    advanced = _merge_main_into_parent(case.origin)
    client = _ParentClient(advanced, [])

    async def reverify(observation):
        # Production wiring: Orchestrator._reverify_integration_parent_source.
        with principal_context(ExecutionPrincipal.service("integration-parent-source")):
            return await case.commands.reverify_integration_parent_source(observation)

    poller = GitHubReviewPoller(
        case.db, case.producer, _ReviewGit(client), parent_head_handler=reverify
    )
    await poller.tick(1031.0)

    checkpoint = await case.db.get_integration_checkpoint("parent")
    assert (await case.db.get_task("parent")).status.value == "PAUSED"
    assert checkpoint["generation"] == initial["generation"] + 1
    assert checkpoint["current_verification_id"] is None
    assert checkpoint["last_completed_verification_id"] == initial["verification_id"]
    # The old exact evidence survives but no longer admits anything.
    async with case.db._engine.connect() as conn:
        kept = await conn.scalar(
            select(func.count()).select_from(t.integration_review_evidence).where(
                t.integration_review_evidence.c.id == first_review["id"]
            )
        )
    assert kept == 1
    assert await _source(case) is None
    assert await _members(case) == []

    verified = await _finish(case)
    members = await _members(case, green=True)
    assert [member["task_id"] for member in members] == ["parent"]
    review = members[0]["review"]
    assert review["review_kind"] == "parent"
    assert review["reviewed_head_sha"] == advanced
    assert review["evidence"]["verification_id"] == verified["verification_id"]
    assert review["evidence"]["verification_id"] != initial["verification_id"]
    assert review["id"] != first_review["id"]


# --- 3. Repair delegate claim after a stage handoff (keen-cascade-74) -------------------------
# Stage deadline and dispatch, the session reconciler, then the claim check.


class _Bus:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type, payload=None):
        self.events.append((event_type, dict(payload or {})))


def _reconciler(db, provider, orchestrator):
    """A session reconciler on the fake provider, wired to the real pool teardown."""

    class _Registry(SessionProviderRegistry):
        def create(self, name, config=None):
            return provider

    config = AppConfig(database=DatabaseConfig(url=lease_dsn("train-regressions")))
    config.sessions.enabled = True
    config.sessions.provider = "fake"
    config.swarm.enabled = True
    config.agents_config.stuck_timeout_seconds = 0
    registry = _Registry({"fake": FakeProvider})
    orchestrator.config = config
    orchestrator.session_providers = registry
    reconciler = SessionReconciler(
        db, config, registry, bus=orchestrator.bus, orchestrator=orchestrator, epoch="e2"
    )
    return reconciler, config.sessions.idle_stop_grace_seconds


async def _claim_refusal(orch, task_id) -> str | None:
    """Run the claim-time origin/fence resolution; return its refusal, if any."""
    task = await orch.db.get_task(task_id)
    project = await orch.db.get_project("p")
    try:
        await orch._hierarchy_origin_and_fence(task, project)
    except (BranchBusy, ValueError) as exc:
        return str(exc)
    return None


async def test_keen_cascade_74_successor_repair_delegate_claimable_after_lingering_writer_stops(
    env, monkeypatch
):
    """keen-cascade-74: a successor repair delegate stayed unclaimable after a handoff.

    The stage-0 delegate closed terminal (BLOCKED) while its branch handoff stayed
    unproven, so it kept its claim: the pool session still names the task
    (``draining``, ``desired_state='stopped'``, ``claim_phase='active'``) and the
    branch owner stays attached to it.  The stage deadline hands the operation to
    stage 1, whose delegate's dispatch answers ``busy`` and sits PAUSED, so the
    claim check refuses it.  The reconciler's idle-stop gate gave up on *any*
    ``claim_phase``, so nothing ever stopped the lingering worker; overnight a
    supervisor killed it and moved the owner by hand.  Fixed on main by 4f9888fdf
    (integrated as 2f64a0c9a): once the idle grace expires the reconciler stops
    the worker, and the subject runtime's next dispatch visit hands the stage to
    the successor.
    """
    # Stage expiry and dispatch are root primitives, scoped to the runtime.
    authorize_root_primitives(monkeypatch)
    case = await _batch_writer(env)
    db, repair = case.db, case.repair

    # Stage 0's delegate closed terminal while its handoff stayed unproven
    # (``retain_claim``): the task is BLOCKED, the pool session still names it
    # (draining, desired_state='stopped', claim_phase='active'), and the branch
    # owner is attached to that session.  The process stays up.
    await db.update_task(case.primary, status=TaskStatus.BLOCKED)
    provider = FakeProvider()
    await provider.start(SessionSpec(
        session_name="n-old-session", work_dir=str(case.slot), command=("claude",),
        instance_token="tok-old-session",
    ))
    lingering = await db.get_session("old-session")
    assert (lingering.task_id, lingering.claim_phase) == (case.primary, "active")
    # A conclusive attempt lets the stage deadline hand off instead of deferring.
    async with db.immediate() as conn:
        await conn.execute(update(t.integration_repair_stages).where(
            t.integration_repair_stages.c.operation_id == case.operation,
            t.integration_repair_stages.c.ordinal == 0,
        ).values(attempts=1))

    outcomes: list[str] = []

    async def dispatch():
        # The subject runtime's writer-file step (``integration_repair_dispatch``).
        result = await repair.dispatch(case.operation, 1)
        outcomes.append(result["outcome"])
        return result

    # The stage handoff: stage 0 expires, stage 1 is allocated, and its
    # delegate's dispatch is refused while the old writer still holds the branch.
    assert (await repair.expire(case.operation, 0, now=130.0))["stage"] == 1
    await dispatch()
    stage = await _stage(case, 1)
    successor = stage["repair_task_id"]
    assert stage["state"] == "active" and outcomes == ["busy"]
    assert (await db.get_task(successor)).status is TaskStatus.PAUSED
    owner = await BranchOwnership(db).get_owner(case.target)
    assert (owner["owner_id"], owner["handoff_state"], owner["session_id"]) == (
        case.primary, "attached", "old-session",
    )
    orch = Orchestrator.__new__(Orchestrator)
    orch.db, orch.git, orch.bus = db, case.recovery.git, _Bus()
    assert not await db.is_hierarchy_task_runnable(successor)
    assert await _claim_refusal(orch, successor) == (
        "hierarchy prerequisite delivery is not current"
    )

    # The real reconciler tick, across the idle grace.  Nothing else will ever
    # stop this worker: its task is terminal, so no close can come.
    reconciler, grace = _reconciler(db, provider, orch)
    t0 = 200.0
    for at in (t0, t0 + grace + 1):
        await reconciler.tick(now=at)
    stopped = await db.get_session("old-session")

    # The runtime's next visit retries the dispatch, then the successor claims.
    await dispatch()
    successor_task = await db.get_task(successor)
    owner = await BranchOwnership(db).get_owner(case.target)
    refusal = await _claim_refusal(orch, successor)
    assert refusal is None and successor_task.status is TaskStatus.READY, (
        f"successor repair delegate {successor} is unclaimable after the stage handoff: "
        f"claim refusal={refusal!r}, status={successor_task.status.value}, "
        f"dispatch outcomes={outcomes}, lingering writer session "
        f"state={stopped.state}/desired={stopped.desired_state}/"
        f"claim_phase={stopped.claim_phase}, branch owner={owner['owner_id']}/"
        f"{owner['handoff_state']}/session={owner['session_id']}"
    )
    assert (stopped.state, stopped.end_reason) == ("stopped", "stop_intent_idle")
    assert outcomes == ["busy", "dispatched"]
    assert (owner["owner_id"], owner["handoff_state"]) == (successor, "reserved")
    task = await db.get_task(successor)
    origin, fence, role = await orch._hierarchy_origin_and_fence(
        task, await db.get_project("p")
    )
    assert role == "repair" and fence.owner_id == successor
    assert fence.token == owner["fence_token"]
    assert origin["operation_id"] == case.operation


# --- 4. Failed aggregate verifier and reopen-collection (clear-ember-89) -----------------------
# The completed fix child is collected, through ``reopen-collection``.


def _reopen_handler(case) -> IntegrationCommandsMixin:
    handler = IntegrationCommandsMixin()
    handler.db = case.db
    handler.orchestrator = SimpleNamespace(
        hierarchy_integration=case.hierarchy,
        promotion_service=case.promotion,
        repair_service=RepairService(case.db),
    )
    return handler


async def _reopen_and_collect_fix(case, red_head: str, verifier_id: str, old_owner: dict):
    """Reopen through the operator control, then collect the completed fix child."""
    handler = _reopen_handler(case)
    preview = await handler._cmd_integration_reopen_collection({"task_id": "epic"})
    assert preview["outcome"] == "would_reopen", f"{preview.get('reason')}: {preview}"
    assert preview["head_sha"] == red_head
    result = await handler._cmd_integration_reopen_collection(
        {
            "task_id": "epic",
            "dry_run": False,
            "expected_head_sha": red_head,
            "reason": "Collect the completed aggregate fix child",
        }
    )
    assert result["outcome"] == "reopened", result
    assert (await case.db.get_integration_checkpoint("epic"))["state"] == "awaiting_children"
    assert (await _operation(case))["verifier_task_id"] is None
    # The verifier's fence no longer authorizes a write to the epic branch.
    with pytest.raises(StaleFence):
        async with case.db.immediate() as conn:
            await BranchOwnership(case.db).transfer_detached_on(
                conn,
                Fence(target=BranchKey(repository_id="repo", branch="aq/epic"),
                      owner_id=verifier_id, token=old_owner["fence_token"]),
                "old-verifier-writer", "verifier",
            )
    assert await _promote_next(case, 30.0) is not None
    fix = [
        row for row in await _rows(case.db, t.task_delivery_receipts)
        if row["source_task_id"] == "epic.3"
    ]
    assert len(fix) == 1 and fix[0]["parent_operation_id"] == case.operation_id
    assert _remote_tip(case) == fix[0]["after_sha"] != red_head


async def test_failed_verifier_close_releases_epic_owner_and_reopen_collects_fix(train_epic):
    """clear-ember-89 hand fix: after the held verifier closes FAIL, reopen-collection collects the fix.

    Incident 2026-10-03/04 on epic clear-ember-89, aggregate verifier
    verify-f357b7f3, branch aq/epic/phase-6-epic-extraction-proposals. Context:
    bold-flare-79 (comment) and stark-ridge-78 (3399edc2d).

    The supervisor's only exit was for the verifier to close FAIL. Its session
    teardown then released the attached owner row: handoff ``released``, no
    session or workspace, and the checkout kept as ``confirmed_workspace_id``.
    After that, reopen-collection reopened the epic and the completed fix child
    (clear-ember-89.4/.5) was collected. This guards that path, which passes on
    main.
    """
    case = train_epic
    red_head, verifier_id = await _failed_aggregate(case)
    async with case.db.immediate() as conn:
        await conn.execute(
            update(t.integration_branch_owners)
            .where(t.integration_branch_owners.c.ref == "aq/epic")
            .values(handoff_state="released", session_id=None, workspace_id=None,
                    confirmed_workspace_id="verifier-workspace")
        )
    old_owner = await _owner(case)
    assert (old_owner["owner_id"], old_owner["owner_role"]) == (verifier_id, "verifier")
    await _reopen_and_collect_fix(case, red_head, verifier_id, old_owner)
