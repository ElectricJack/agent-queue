"""Integration-train regressions: the 2026-10-03/04 overnight failure modes.

One end-to-end scenario per failure mode that needed a hand fix overnight, each on
disposable PostgreSQL and real Git (a bare origin plus clones), driven through the
entry point the train or the operator actually used:

1. Generated ``tests/selection_catalogue.json`` overlaps blocked every child
   promotion into an epic branch (prime-vault-27): ``delivery_promote``.
2. An epic whose children all reached main by other routes could not settle
   (keen-grove-22): ``aq integration adopt --settle-delivered-children``.
3. Stale child reservations (crisp-cascade-19, noble-journey-92) and collector
   reservations (fresh-meadow-81) blocked that settlement.
4. Multi-child epics were never admitted: no exact ``parent`` review evidence,
   and a PR head advanced by a merge of main was never reverified (keen-quest-99).
5. An escalated parent operation whose children were all delivered
   (crisp-horizon-90).
6. Repair delegates could not claim after a stage handoff while the previous
   stage's writer lingered (keen-cascade-74): the session reconciler and the
   claim-time origin/fence check.
7. A verifier held without settling its failure deadlocked reopen-collection.

A scenario that still fails on main is ``xfail`` naming its open ticket, with a
reason that says when to remove the mark. The fixtures and builders are borrowed
from the suites that own each mechanism, so a scenario here exercises the same
setup those suites keep honest.
"""

from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, insert, select, update

from src.commands import CommandHandler
from src.commands.contracts.integration import IntegrationAdoptArgs
from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import ExecutionPrincipal, principal_context
from src.config import AppConfig, DatabaseConfig
from src.database import tables as t
from src.database.queries.result_queries import close_identity
from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY
from src.git.manager import GitManager
from src.integration.delivery_truth import DeliveryState
from src.integration.github_review_poll import GitHubReviewPoller
from src.integration.models import BranchKey, Fence, PromotionInput
from src.integration.outbox import enqueue_integration_event
from src.integration.ownership import BranchBusy, BranchOwnership, StaleFence
from src.integration.promotion import PromotionService
from src.integration.repair import RepairService
from src.integration.service import DISPATCH_RETRY_BASE_SECONDS, IntegrationService
from src.models import (
    RepoConfig,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskCompletion,
    TaskStatus,
    Workspace,
)
from src.orchestrator.core import Orchestrator
from src.sessions import SessionProviderRegistry
from src.sessions.fake import FakeProvider
from src.sessions.provider import SessionSpec
from src.sessions.reconciler import SessionReconciler
from src.test_selection.catalogue import CATALOGUE_PATH
from tests import test_development_integration as dev
from tests import test_integration_cancelled_collection as cancelled_tests
from tests import test_integration_owner_recovery as owner_tests
from tests import test_integration_parent_source as parent_source
from tests import test_integration_promotion as promotion_tests
from tests.db_fixtures import lease_dsn
from tests.test_development_integration import _merge_on_main, feature, git
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
from tests.test_integration_parent_completion import _boundary, _code_receipt, _parent_tree
from tests.test_integration_parent_source import _finish, _members, _ParentClient, _source
from tests.test_integration_promotion import _review
from tests.test_integration_repair_rollover import _batch_writer, _stage

# Fixtures shared with the suites that own them, bound by assignment so the
# scenarios' own ``db`` locals do not shadow an import.
promotion_db = promotion_tests.db  # 1: promotion database with a repo row
setup = dev.setup  # 2-3: PostgreSQL + bare origin for ``integration_adopt``
db = parent_source.db  # 4-5: requested by ``completed`` by name; 5 builds its tree on it
completed = parent_source.completed  # 4: a collected, verified two-child epic
env = owner_tests.env  # 6: PostgreSQL + origin for the repair-stage writer
train_epic = cancelled_tests.case  # 7: train epic with a red aggregate and a fix child


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


# --- 2-3. Epics that could not settle, and the reservations that blocked them -----------------
# ``aq integration adopt --settle-delivered-children``, as the operator ran it.


EPIC = "aq/epic/old-aggregate"
VERIFIER = "verify-stale-aggregate"
CANONICAL_REFUSAL = "a PAUSED managed parent with a canonical episode is required"


@pytest.fixture
def operator(setup, tmp_path):
    """``integration_adopt`` exactly as the CLI/API reaches it, minus the HTTP hop."""
    db, service, *_ = setup
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.resources.jobs.enabled = True
    handler = CommandHandler(
        SimpleNamespace(db=db, config=config, git=service.git, development_integration=service),
        config,
    )

    async def adopt(task_id, head_sha, **flags):
        args = IntegrationAdoptArgs(
            project_id="p", task_ids=[task_id], target_ref="refs/heads/main",
            head_sha=head_sha, reason="children reached main by other routes", **flags,
        )
        return await handler.execute("integration_adopt", args.model_dump(mode="json"))

    return adopt


async def stuck_epic(
    setup, parent, *, ancestry, equivalent=(), undelivered=(), episode=True, verifier=True,
    operation_state="active",
):
    """A PAUSED train parent whose children are on main, by ancestry or rebased.

    *undelivered* children are published and completed but never reach main.

    The epic branch holds an obsolete aggregate that is not an ancestor of main.
    With *episode* the parent has a canonical collection episode and operation;
    with *verifier* that operation's stale aggregate verifier is READY but
    unclaimable (its origin is reserved, never materialized) and reserves the
    epic branch. The source clone is left checked out on the epic branch.
    """
    db, _service, source, _remote, repo = setup
    base = git(source, "rev-parse", "main")
    children = [*ancestry, *equivalent, *undelivered]
    heads = {child: await feature(setup, child) for child in children}
    main = await _merge_on_main(source, *ancestry)
    for child in equivalent:
        # The same change reaches main as a different commit (same subject).
        (source / f"{child}.txt").write_text("new\n")
        git(source, "add", ".")
        git(source, "commit", "-m", child)
    if equivalent:
        git(source, "push", "origin", "main")
        main = git(source, "rev-parse", "HEAD")
    git(source, "checkout", "-B", EPIC, base)
    (source / "obsolete.txt").write_text("old aggregate\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "obsolete aggregate")
    stale = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", f"HEAD:{EPIC}")
    now = time.time()
    await db.create_task(Task(
        id=parent, project_id="p", repo_id=repo.id, title=parent, description="",
        status=TaskStatus.PAUSED, branch_name=EPIC,
    ))
    if verifier:
        await db.create_task(Task(
            id=VERIFIER, project_id="p", repo_id=repo.id, title="Verify obsolete aggregate",
            description="", status=TaskStatus.READY, branch_name=EPIC,
        ))
    async with db.immediate() as conn:
        await conn.execute(update(t.projects).where(t.projects.c.id == "p").values(
            hierarchical_integration_mode="train", hierarchical_integration_desired_mode="train",
        ))
        await conn.execute(update(t.tasks).where(t.tasks.c.id.in_(children)).values(
            parent_task_id=parent,
        ))
        if episode:
            await conn.execute(insert(t.integration_parent_episodes).values(
                id="episode", parent_task_id=parent, repository_id=repo.id, generation=1,
                pre_collection_checkpoint_sha=base, created_at=now,
            ))
            await conn.execute(insert(t.integration_repair_operations).values(
                id="collection", target_kind="parent", parent_task_id=parent,
                episode_id="episode", active_stage=0, state=operation_state,
                policy_snapshot={}, artifact_snapshot={}, required_check_version="checks-v1",
                verifier_task_id=VERIFIER if verifier else None, created_at=now, updated_at=now,
            ))
        await conn.execute(insert(t.task_integration_checkpoints).values(
            task_id=parent, repository_id=repo.id, branch=EPIC, generation=6,
            checkpoint_sha=stale, state="integration_ready", version=9,
            episode_id="episode" if episode else None,
            branch_owner_id=VERIFIER if verifier else None, updated_at=now,
        ))
        if verifier:
            await conn.execute(insert(t.task_branch_origins).values(
                id="stale-origin", task_id=VERIFIER, repository_id=repo.id, branch_name=EPIC,
                base_sha=stale, creation_generation=0, reserved=True, materialized=False,
                created_at=now,
            ))
            await conn.execute(insert(t.integration_branch_owners).values(
                id="epic-owner", repository_id=repo.id, ref=EPIC, owner_id=VERIFIER,
                owner_role="verifier", fence_token=4, handoff_state="reserved",
                created_at=now, updated_at=now,
            ))
    return children, heads, main


async def owners(db):
    async with db._engine.connect() as conn:
        rows = (await conn.execute(select(t.integration_branch_owners))).mappings().all()
    return {row["id"]: dict(row) for row in rows}


def recycled_slot(setup, tmp_path, name):
    """A pool slot recycled to another task: detached from main, unpublished local work."""
    _db, _service, source, _remote, _repo = setup
    slot = tmp_path / name
    git(source, "worktree", "add", "--detach", str(slot), "main")
    git(slot, "checkout", "-b", f"aq/next-user-of-{name}")
    (slot / "next-user.txt").write_text("unpublished work of the next slot user\n")
    git(slot, "add", ".")
    git(slot, "commit", "-m", "next slot user's local work")
    (slot / "dirty-next-user.txt").write_text("still writing\n")
    return slot


async def test_keen_grove_22_epic_settles_from_children_delivered_by_other_routes(
    setup, operator,
):
    """keen-grove-22: an epic whose children all reached main by other routes never closed.

    Incident shape (agile-harbor-62, train refactor phase 3): three children are
    ancestors of main, the fourth reached main as an equivalent commit and was
    adopted with explicit ``--accept-equivalent``; the stale aggregate verifier is
    READY but unclaimable and no control settled the parent. Fixed on main by
    6ff5310d6 (``--settle-delivered-children``).

    Tail: stark-ridge-78 part A as it actually happened. The supervisor's
    "canonical episode is required" refusals came after the parents were
    already closed; on a settled parent that refusal is the correct guard and
    changes nothing.
    """
    db, service, _source, remote, _repo = setup
    parent = "agile-harbor-62"
    ancestry = [f"{parent}.{n}" for n in (1, 2, 3)]
    rebased = f"{parent}.4"
    children, heads, main = await stuck_epic(
        setup, parent, ancestry=ancestry, equivalent=[rebased],
    )

    # Stuck: the parent needs a verified completion no verifier can produce.
    stuck = await operator(parent, main, accept_equivalent=True)
    assert stuck["outcome"] == "blocked"
    assert f"{parent}: current verified parent completion is required" in stuck["error"]
    # The rebased child is adopted on its own, by an explicit equivalence decision.
    child = await operator(rebased, main, accept_equivalent=True)
    assert child.get("success") is not False, child
    # Settlement still names the equivalent child until the operator accepts it.
    refused = await operator(parent, main, settle_delivered_children=True)
    assert refused["outcome"] == "blocked"
    assert refused["error"] == f"{rebased}: equivalent child delivery requires --accept-equivalent"

    refs = git(remote, "show-ref")
    preview = await operator(
        parent, main, settle_delivered_children=True, accept_equivalent=True, dry_run=True,
    )
    assert preview["outcome"] == "would_adopt_parent", preview
    assert preview["retire_delegates"] == [VERIFIER]
    assert {p["task_id"]: (p["kind"], p["source_sha"]) for p in preview["children"]} == {
        **{c: ("ancestry", heads[c]) for c in ancestry}, rebased: ("equivalent", heads[rebased]),
    }
    assert git(remote, "show-ref") == refs
    assert (await db.get_task(parent)).status == TaskStatus.PAUSED
    assert (await db.get_task(VERIFIER)).status == TaskStatus.READY

    result = await operator(
        parent, main, settle_delivered_children=True, accept_equivalent=True,
    )
    assert result["outcome"] == "adopted", result
    assert (await db.get_task(parent)).status == TaskStatus.COMPLETED
    assert (await db.get_task(VERIFIER)).status == TaskStatus.FAILED
    assert [(await db.get_task(c)).status for c in children] == [TaskStatus.COMPLETED] * 4
    completion = await db.get_task_completion(parent)
    assert completion.commits == [main]
    proof = (await service.delivery_observer.observe([parent])).get(parent)
    assert (proof.state, proof.source_oid) == (DeliveryState.CONTAINED, main)
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(t.integration_repair_operations.c.state)) == "cancelled"
    owner = (await owners(db))["epic-owner"]
    assert (owner["handoff_state"], owner["fence_token"]) == ("released", 5)
    # Only provenance refs were written; main and the epic branch are untouched.
    assert git(remote, "rev-parse", "main") == main
    new_refs = set(git(remote, "show-ref").splitlines()) - set(refs.splitlines())
    assert new_refs and all(" refs/heads/aq-provenance/" in ref for ref in new_refs)

    # stark-ridge-78 part A, the real shape: re-settling a settled parent refuses.
    again = await operator(
        parent, main, settle_delivered_children=True, accept_equivalent=True, dry_run=True,
    )
    assert again["outcome"] == "blocked"
    assert again["error"] == CANONICAL_REFUSAL
    assert (await db.get_task_completion(parent)).id == completion.id


async def test_crisp_cascade_19_noble_journey_92_finished_writer_reservations_settle(
    setup, operator, tmp_path,
):
    """crisp-cascade-19 + noble-journey-92: stale child reservations blocked settlement.

    Finished child writers left their branch owner rows ``reserved``; one row's
    ``confirmed_workspace_id`` names a pool slot since recycled to another task
    (unlocked, other branch, unpublished and dirty work). Fixed on main by
    44fda5d52 (crisp-cascade-19) and 0000ef321 (noble-journey-92); without
    either, the recycled-slot row is refused "must be recovered first".

    Negative guard: while a child's writer session is still live and attached to
    its owner row, settlement refuses and changes nothing.
    """
    db, _service, source, remote, repo = setup
    parent = "vivid-quest-44"
    children, heads, main = await stuck_epic(
        setup, parent, ancestry=[f"{parent}.{n}" for n in (1, 2, 3)],
    )
    recycled, live = children[0], children[1]
    slot = recycled_slot(setup, tmp_path, "slot-1")
    await db.create_workspace(Workspace(
        id="base", project_id="p", workspace_path=str(source), source_type=RepoSourceType.CLONE,
    ))
    await db.create_workspace(Workspace(
        id="slot-1", project_id="p", workspace_path=str(slot), source_type=RepoSourceType.CLONE,
        slot_index=1, base_workspace_id="base",
    ))
    now = time.time()
    async with db.immediate() as conn:
        for child in children:
            branch = "aq/" + child
            git(source, "push", "origin", f"{child}:{branch}")
            git(source, "branch", branch, heads[child])
            await conn.execute(
                update(t.tasks).where(t.tasks.c.id == child).values(branch_name=branch)
            )
            await conn.execute(insert(t.integration_branch_owners).values(
                id="owner-" + child, repository_id=repo.id, ref=branch, owner_id=child,
                owner_role="worker", fence_token=7, handoff_state="reserved",
                confirmed_workspace_id="slot-1" if child == recycled else None,
                created_at=now, updated_at=now,
            ))
        await conn.execute(update(t.integration_branch_owners).where(
            t.integration_branch_owners.c.id == "owner-" + live,
        ).values(handoff_state="attached", session_id="live-writer"))
    await db.create_session(SessionRecord(
        id="live-writer", project_id="p", task_id=live, profile_id="worker", harness="codex",
        provider="fake", name="live-writer", lifecycle="pool", work_dir="/tmp/live-writer",
        epoch="epoch", instance_token="token", started_at=now, state="running",
        desired_state="running",
    ))
    slot_head, slot_status = git(slot, "rev-parse", "HEAD"), git(slot, "status", "--porcelain")
    refs = git(remote, "show-ref")

    for dry_run in (True, False):
        held = await operator(parent, main, settle_delivered_children=True, dry_run=dry_run)
        assert held["outcome"] == "blocked"
        assert held["error"] == f"{live}: session, claim or workspace is still retained"
    assert (await db.get_task(parent)).status == TaskStatus.PAUSED
    assert all(row["handoff_state"] != "released" for row in (await owners(db)).values())
    assert git(remote, "show-ref") == refs

    # The writer finishes and stops, leaving its reservation behind, as overnight.
    async with db.immediate() as conn:
        await conn.execute(update(t.sessions).where(t.sessions.c.id == "live-writer").values(
            state="stopped", desired_state="stopped",
        ))
        await conn.execute(update(t.integration_branch_owners).where(
            t.integration_branch_owners.c.id == "owner-" + live,
        ).values(handoff_state="reserved", session_id=None))

    preview = await operator(parent, main, settle_delivered_children=True, dry_run=True)
    assert preview["outcome"] == "would_adopt_parent", preview
    assert {row["owner_id"] for row in preview["ownership"]} == {VERIFIER, *children}
    assert git(remote, "show-ref") == refs
    result = await operator(parent, main, settle_delivered_children=True)
    assert result["outcome"] == "adopted", result
    assert (await db.get_task(parent)).status == TaskStatus.COMPLETED
    assert (await db.get_task(VERIFIER)).status == TaskStatus.FAILED
    rows = await owners(db)
    assert {rid: (r["handoff_state"], r["fence_token"]) for rid, r in rows.items()} == {
        "epic-owner": ("released", 5), **{"owner-" + c: ("released", 8) for c in children},
    }
    # The recycled slot and its next user's work are untouched.
    assert rows["owner-" + recycled]["confirmed_workspace_id"] == "slot-1"
    assert (await db.get_workspace("slot-1")).locked_by_task_id is None
    assert (git(slot, "rev-parse", "HEAD"), git(slot, "status", "--porcelain")) == (
        slot_head, slot_status,
    )


async def test_fresh_meadow_81_superseded_collector_reservation_settles(setup, operator):
    """fresh-meadow-81 (ticket shape): a stale collector reservation blocked settlement.

    The epic branch's only owner row is a ``collector`` of an older, cancelled
    parent operation (its own episode), with no session or workspace and a
    ``confirmed_workspace_id`` naming an unlocked slot that holds the epic branch
    clean and published. The guard accepts only the parent, its current operation
    or a delegate, so settlement refuses "branch owner <id> must be recovered
    first". Fixed by fresh-meadow-81 (a389deba1, on main via 3b9e6eea5): settlement
    retires such a row and keeps the historical operation's state.

    Negative guard (holds before and after the fix): uncommitted work in the
    confirmed checkout of the epic branch refuses settlement.
    """
    db, _service, source, remote, repo = setup
    parent = "vivid-quest-44"
    _children, _heads, main = await stuck_epic(
        setup, parent, ancestry=[f"{parent}.{n}" for n in (1, 2, 3)],
    )
    async with db.immediate() as conn:
        await conn.execute(insert(t.integration_parent_episodes).values(
            id="old-episode", parent_task_id=parent, repository_id=repo.id, generation=0,
            pre_collection_checkpoint_sha=main, created_at=1.0,
        ))
        await conn.execute(insert(t.integration_repair_operations).values(
            id="old-collection", target_kind="parent", parent_task_id=parent,
            episode_id="old-episode", state="cancelled", policy_snapshot={},
            artifact_snapshot={}, required_check_version="checks-v1", created_at=1.0,
            updated_at=2.0,
        ))
        await conn.execute(insert(t.workspaces).values(
            id="ws-sunny-hall", project_id="p", workspace_path=str(source), created_at=1.0,
        ))
        await conn.execute(update(t.integration_branch_owners).where(
            t.integration_branch_owners.c.id == "epic-owner",
        ).values(owner_id="old-collection", owner_role="collector",
                 confirmed_workspace_id="ws-sunny-hall"))

    (source / "uncommitted.txt").write_text("unpublished parent work\n")
    dirty = await operator(parent, main, settle_delivered_children=True)
    assert dirty["outcome"] == "blocked", dirty
    (source / "uncommitted.txt").unlink()

    refs = git(remote, "show-ref")
    preview = await operator(parent, main, settle_delivered_children=True, dry_run=True)
    assert preview["outcome"] == "would_adopt_parent", preview
    assert [row["owner_id"] for row in preview["ownership"]] == ["old-collection"]
    assert git(remote, "show-ref") == refs
    result = await operator(parent, main, settle_delivered_children=True)
    assert result["outcome"] == "adopted", result
    assert (await db.get_task(parent)).status == TaskStatus.COMPLETED
    owner = (await owners(db))["epic-owner"]
    assert (owner["handoff_state"], owner["fence_token"]) == ("released", 5)
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(t.integration_repair_operations.c.state).where(
            t.integration_repair_operations.c.id == "old-collection",
        )) == "cancelled"


async def test_fresh_meadow_81_current_collector_of_undelivered_epic_still_refuses(
    setup, operator, tmp_path,
):
    """fresh-meadow-81 scope guard: the live vivid-quest-44 shape must keep refusing.

    vivid-quest-44's reserved collector row (owner c9ca6cc7) belongs to the
    parent's only and current repair operation (escalated, on the canonical
    episode) and carries a confirmed slot. Its children are not on main: none
    is an ancestor and their patches differ, so settling it as delivered would
    publish a lie. fresh-meadow-81 retires collectors of ended historical
    operations only; this shape stays a refusal (supervisor b54ae6cf,
    2026-10-04), and its real recovery is ``recover-parent-head`` followed by a
    fresh aggregate verification (stark-ridge-78). The refusal must not depend on
    the confirmed slot: with or without it, nothing is adopted or written.
    """
    db, _service, _source, remote, repo = setup
    parent = "vivid-quest-44"
    _children, _heads, main = await stuck_epic(
        setup, parent, ancestry=[], undelivered=[f"{parent}.{n}" for n in (1, 2, 3)],
        verifier=False, operation_state="escalated",
    )
    slot = recycled_slot(setup, tmp_path, "ws-sunny-hall")
    now = time.time()
    async with db.immediate() as conn:
        await conn.execute(insert(t.workspaces).values(
            id="ws-sunny-hall", project_id="p", workspace_path=str(slot), created_at=now,
        ))
        await conn.execute(insert(t.integration_branch_owners).values(
            id="c9ca6cc7", repository_id=repo.id, ref=EPIC, owner_id="collection",
            owner_role="collector", fence_token=8, handoff_state="reserved",
            created_at=now, updated_at=now,
        ))
    refs = git(remote, "show-ref")
    for confirmed in (None, "ws-sunny-hall"):
        async with db.immediate() as conn:
            await conn.execute(update(t.integration_branch_owners).where(
                t.integration_branch_owners.c.id == "c9ca6cc7",
            ).values(confirmed_workspace_id=confirmed))
        before = await owners(db)
        for dry_run in (True, False):
            result = await operator(
                parent, main, settle_delivered_children=True, dry_run=dry_run,
            )
            assert result["outcome"] == "blocked", (confirmed, dry_run, result)
        assert (await db.get_task(parent)).status == TaskStatus.PAUSED
        assert await owners(db) == before
        assert await db.get_task_completion(parent) is None
    assert git(remote, "show-ref") == refs


# --- 4. Multi-child epic admission (keen-quest-99) --------------------------------------------
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


# --- 5. Escalated parent operation, every child delivered (crisp-horizon-90) ------------------
# An operator recovers a finished post-collection repair, through ``recover-parent-head``.


async def _crisp_horizon_case(db, tmp_path, *, final_close_audits: int):
    """Three receipted children, two audited repair gaps, and a finished final stage.

    This is the crisp-horizon-90 shape, scaled down from 11 children and 7 stages:

    * Every child carries a delivery receipt into ``aq/parent``, and every
      receipt's ``after_sha`` is an ancestor of the live parent tip.
    * Stages 0 and 1 each published a repair commit between two receipts. That
      is the "receipt chain does not bind the current parent head" gap.
    * Stage 2 resolved child 3's conflict. Its promotion intent is committed
      with the resolution fields and the delegate task is COMPLETED, yet the
      stage is still ``active`` and the operation is ``escalated``.
    * The parent task is PAUSED.

    ``final_close_audits`` is how many ``integration.repair_delegate_closed``
    audits the final stage has. A clean close leaves one. Live stage 6 of
    operation 6397b45b had two (fences 37 and 40, from two delegate sessions).
    The shape fresh-meadow-81's fix a389deba1 (on main via 3b9e6eea5) reconciles
    has none, with the resolution receipt standing in for the audit.
    """
    origin, work = tmp_path / "origin.git", tmp_path / "work"

    def git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=work if work.exists() else tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "--bare", "--initial-branch=main", str(origin))
    git("clone", str(origin), str(work))
    git("config", "user.name", "Repair Test")
    git("config", "user.email", "repair@example.test")
    git("commit", "--allow-empty", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("push", "origin", "main")
    git("switch", "-c", "aq/parent")
    shas = {}
    for name in ("child_1", "gap_1", "child_2", "gap_2", "final"):
        git("commit", "--allow-empty", "-m", name)
        shas[name] = git("rev-parse", "HEAD")
    final_tree = git("rev-parse", "HEAD^{tree}")
    git("push", "origin", "aq/parent")

    hierarchy, checkpointed, children = await _parent_tree(db, children=3, base_sha=base)
    operation_id = checkpointed["operation_id"]
    await _code_receipt(db, children[0], base, shas["child_1"])
    await _code_receipt(db, children[1], shas["gap_1"], shas["child_2"])
    authoring = {
        "operation_id": operation_id,
        "stage_ordinal": 2,
        "repair_task_id": "repair-final",
        "repair_session_id": "final-session",
        "repair_session_instance_token": "final-instance",
        "repair_workspace_id": "final-workspace",
        "fence": {"repository_id": "repo", "branch": "aq/parent",
                  "owner_id": "repair-final", "token": 6},
    }
    push = {"kind": "exact_resolution_push_observed", "remote_sha": shas["final"]}
    remote = {
        "kind": "exact_resolution_tip",
        "remote_sha": shas["final"],
        "resolved_tree_sha": final_tree,
        "repair_commit_shas": [shas["final"]],
    }
    await _code_receipt(
        db,
        children[2],
        shas["gap_2"],
        shas["final"],
        squash_sha=None,
        review_evidence={"review": {"source_base": "a" * 40}},
        resolution_evidence={
            "kind": "conflict_resolution",
            "original_source_base": "a" * 40,
            "original_source_head": "b" * 40,
            "original_source_tree": "c" * 40,
            "original_expected_target": shas["gap_2"],
            "resolved_head_sha": shas["final"],
            "resolved_tree_sha": final_tree,
            "repair_commit_shas": [shas["final"]],
            "authoring": authoring,
            "push_authority": push,
            "remote_proof": remote,
        },
    )
    delegates = (
        ("repair", shas["gap_1"], 20.0),
        ("repair-middle", shas["gap_2"], 22.0),
        ("repair-final", shas["final"], 26.0),
    )
    for task_id, head, completed_at in delegates:
        await db.create_task(
            Task(
                id=task_id, project_id="p", repo_id="repo", branch_name="aq/parent",
                title=task_id, description="Authorized collection repair",
                status=TaskStatus.COMPLETED, created_by_kind="integration_repair",
                created_by_id=operation_id,
            )
        )
        await db.save_task_completion(
            TaskCompletion(
                id=f"completion-{task_id}", task_id=task_id, outcome="pass",
                branch="aq/parent",
                # The resolving delegate closed with no commits of its own:
                # its work is the committed resolution intent.
                commits=[] if task_id == "repair-final" else [head],
                completed_at=completed_at,
            )
        )
    await db.set_task_meta(
        "repair-final",
        ACCEPTED_CLOSE_KEY,
        close_identity("completion-repair-final", session_id="final-session", claim_epoch=0),
    )
    close_audits = [
        (0, "repair", "repair-session", "repair-instance", "repair-workspace", 2),
        (1, "repair-middle", "middle-session", "middle-instance", "middle-workspace", 4),
    ]
    if final_close_audits:
        close_audits.append(
            (2, "repair-final", "final-session", "final-instance", "final-workspace", 6)
        )
    if final_close_audits > 1:
        # Live stage 6 was closed a second time by a later delegate session.
        close_audits.append(
            (2, "repair-final", "final-retry-session", "final-retry-instance",
             "final-workspace", 6)
        )
    async with db.immediate() as conn:
        await conn.execute(update(t.tasks).where(t.tasks.c.id == "parent").values(status="PAUSED"))
        await conn.execute(
            update(t.task_integration_checkpoints)
            .where(t.task_integration_checkpoints.c.task_id == "parent")
            .values(checkpoint_sha=shas["child_1"], state="verifying")
        )
        await conn.execute(
            update(t.integration_branch_owners).values(
                owner_id=operation_id, owner_role="collector",
                handoff_state="reserved", fence_token=7,
            )
        )
        await conn.execute(
            update(t.integration_repair_operations)
            .where(t.integration_repair_operations.c.id == operation_id)
            .values(state="escalated", active_stage=2)
        )
        for index, (stage, task_id, session, instance, workspace, token) in enumerate(
            close_audits
        ):
            await enqueue_integration_event(
                conn,
                event_id=f"closed-{index}",
                dedup_key=f"closed-{index}",
                project_id="p",
                event_type="integration.repair_delegate_closed",
                available_at=20.0 + index,
                payload={
                    "operation_id": operation_id, "stage": stage, "task_id": task_id,
                    "session_id": session, "instance_token": instance,
                    "workspace_id": workspace, "fence_token": token,
                },
            )
        stages = (
            (0, "repair", shas["child_1"], shas["gap_1"], "expired"),
            (1, "repair-middle", shas["child_2"], shas["gap_2"], "expired"),
            (2, "repair-final", shas["gap_2"], shas["final"], "active"),
        )
        for ordinal, task_id, start, head, state in stages:
            await conn.execute(
                insert(t.integration_repair_stages).values(
                    operation_id=operation_id,
                    ordinal=ordinal,
                    policy=_boundary().repair.model_dump(mode="json"),
                    intelligence_class="medium",
                    starting_sha=start,
                    trigger_id="failed-check",
                    writer_kind="repair_delegate",
                    repair_task_id=task_id,
                    current_subject={"kind": "parent", "generation": 1, "head_sha": head},
                    started_at=2.0,
                    deadline_at=10.0,
                    deadline_event_id=f"deadline-{ordinal}",
                    attempts=1,
                    state=state,
                    dossier={"repair_commits": [head], "branch_sha": head},
                )
            )
        await conn.execute(
            insert(t.integration_promotion_intents).values(
                id="final-intent",
                domain_key="final-intent",
                operation_key=operation_id,
                project_id="p",
                receipt_id=f"receipt-{children[2]}",
                source_task_id=children[2],
                target_task_id="parent",
                source_head="b" * 40,
                source_base="a" * 40,
                repository_id="repo",
                target_branch="aq/parent",
                expected_target=shas["gap_2"],
                fence_owner_id=operation_id,
                fence_token=5,
                state="committed",
                resolution_head_sha=shas["final"],
                resolution_tree_sha=final_tree,
                resolution_commit_shas=[shas["final"]],
                resolution_operation_id=operation_id,
                resolution_stage_ordinal=2,
                resolution_task_id="repair-final",
                resolution_session_id="final-session",
                resolution_session_instance_token="final-instance",
                resolution_workspace_id="final-workspace",
                resolution_fence_owner_id="repair-final",
                resolution_fence_token=6,
                resolution_push_started_at=24.0,
                resolution_push_evidence=push,
                remote_evidence=remote,
                committed_at=25.0,
                created_at=23.0,
                updated_at=25.0,
            )
        )
    repo = RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.CLONE, url=str(origin))
    promotion = PromotionService(
        db,
        data_dir=tmp_path / "retained",
        git_manager=GitManager(),
        repository_resolver=lambda _: repo,
    )
    handler = IntegrationCommandsMixin()
    handler.db = db
    handler.orchestrator = SimpleNamespace(
        hierarchy_integration=hierarchy,
        promotion_service=promotion,
        repair_service=RepairService(db),
    )
    args = {
        "operation_id": operation_id,
        "head_sha": shas["final"],
        "dry_run": True,
        "expected_episode_id": checkpointed["episode_id"],
        "expected_generation": 1,
        "expected_stage": 2,
        "expected_fence_token": 7,
        "reason": "Reconcile the finished collection of crisp-horizon-90",
    }
    return SimpleNamespace(
        handler=handler, hierarchy=hierarchy, git=git, shas=shas, args=args,
        operation_id=operation_id, children=children,
    )


@pytest.mark.parametrize(
    "final_close_audits",
    [
        pytest.param(
            0,
            id="resolution-receipt-only",
        ),
        pytest.param(
            2,
            id="two-close-audits-as-live",
            marks=pytest.mark.xfail(
                reason=(
                    "prime-current: live stage 6 had two delegate-close audits and "
                    "recover-parent-head accepts exactly one, even with fresh-meadow-81 "
                    "(3b9e6eea5) on main; remove when prime-current lands"
                ),
                strict=False,
            ),
        ),
    ],
)
async def test_escalated_parent_with_all_children_delivered_moves_to_fresh_verification(
    db, tmp_path, final_close_audits
):
    """crisp-horizon-90 (Discord epic): an escalated finished collection must reach fresh verification.

    Incident 2026-10-03/04, ticket fresh-meadow-81 part 2, fixed by a389deba1
    ("fix(integration): reconcile proven historical parent recovery", on main via
    3b9e6eea5) for one delegate-close audit or a resolution receipt.
    Related fixes: stark-ridge-78 (3399edc2d, on main) and wise-torrent
    (6d9ca2d7d, on main).

    All 11 children were receipted into the parent, and each was an ancestor of
    the epic head. Parent repair operation 6397b45b finished its conflict
    resolutions, yet it stayed ``escalated`` with its last stage ``active`` while
    the stage's delegate was COMPLETED. The parent stayed PAUSED (stale_head:
    "delivery receipt chain does not bind the current parent head"). Every
    supported control refused: redrive-root ("PAUSED, not COMPLETED"),
    reopen-collection ("collection operation is escalated"), resume
    (invalid_state), and recover-parent-head ("repair lacks its exact fenced
    delegate-close audit").

    Required: recover-parent-head, the supported control for a finished
    post-collection repair, binds the receipt chain to the live tip. It moves
    the parent to fresh aggregate verification of that tip and leaves receipts
    and the remote unchanged.
    """
    case = await _crisp_horizon_case(db, tmp_path, final_close_audits=final_close_audits)
    final = case.shas["final"]

    # The incident shape, which holds on main today.
    receipts = await _rows(db, t.task_delivery_receipts)
    assert {row["source_task_id"] for row in receipts} == set(case.children)
    for row in receipts:
        case.git("merge-base", "--is-ancestor", row["after_sha"], final)
    assert (await db.get_task("parent")).status is TaskStatus.PAUSED
    [operation] = await _rows(db, t.integration_repair_operations)
    assert (operation["state"], operation["active_stage"]) == ("escalated", 2)
    [final_stage] = await _rows(
        db, t.integration_repair_stages, t.integration_repair_stages.c.ordinal == 2
    )
    assert final_stage["state"] == "active"
    assert (await db.get_task(final_stage["repair_task_id"])).status is TaskStatus.COMPLETED
    refs = case.git("ls-remote", "origin")

    preview = await case.handler._cmd_integration_recover_parent_head(case.args)
    assert preview["outcome"] == "would_recover", f"{preview.get('error')}: {preview}"
    applied = await case.handler._cmd_integration_recover_parent_head(
        {**case.args, "dry_run": False}
    )
    assert applied["success"] and applied["outcome"] == "recovered", applied

    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["checkpoint_sha"] == final
    assert checkpoint["state"] == "integration_ready"
    assert checkpoint["verified_sha"] is None
    assert checkpoint["current_verification_id"] is None
    verifier = await db.get_task(f"verify-{case.operation_id}")
    assert verifier is not None and verifier.status is TaskStatus.PAUSED
    # Recovery is not verification: the recovered tip needs fresh evidence.
    unverified = await case.hierarchy.verify_parent("parent", 1, final, [])
    assert unverified["outcome"] != "verified", unverified
    assert await _rows(db, t.task_delivery_receipts) == receipts
    assert case.git("ls-remote", "origin") == refs
    again = await case.handler._cmd_integration_recover_parent_head(
        {**case.args, "dry_run": False}
    )
    assert again["outcome"] == "already_recovered", again


# --- 6. Repair delegate claim after a stage handoff (keen-cascade-74) -------------------------
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
    env,
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
    the worker, and the paced dispatch retry hands the stage to the successor.
    """
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

    async def dispatcher(row):
        result = await repair.dispatch(row["operation_id"], int(row["ordinal"]))
        outcomes.append(result["outcome"])
        return result

    now = [130.0]
    # Deadline and dispatch sources only, as in the rollover suite: the
    # reservation reconciler would hand a stopped writer's branch over by
    # itself and hide whether the dispatch retry can.
    service = IntegrationService(
        db, SimpleNamespace(mark_due=AsyncMock()),
        SimpleNamespace(expire=repair.expire, pending_dispatches=repair.pending_dispatches),
        SimpleNamespace(dispatch_due=AsyncMock()),
        repair_dispatcher=dispatcher, clock=lambda: now[0],
    )

    # The stage handoff: stage 0 expires, stage 1 is allocated, and its
    # delegate's dispatch is refused while the old writer still holds the branch.
    await service.tick(now[0])
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

    # The paced dispatch retry, then the claim of the successor delegate.
    now[0] = 130.0 + DISPATCH_RETRY_BASE_SECONDS
    await service.tick(now[0])
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


# --- 7. Held aggregate verifier and reopen-collection (clear-ember-89) ------------------------
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


@pytest.mark.xfail(
    reason=(
        "eager-apex-93: a held aggregate verifier with a red trusted conclusion keeps the "
        "epic owner attached; reopen-collection refuses ('the verifier has no settled "
        "failed completion for this exact head') and the completed fix child is never "
        "collected; remove when eager-apex-93 lands"
    ),
    strict=False,
)
async def test_held_red_verifier_does_not_strand_completed_fix_child(train_epic):
    """clear-ember-89: a held verifier after a red trusted conclusion must not block collection.

    Incident 2026-10-03/04 on epic clear-ember-89, verifier verify-f357b7f3.
    Supervisor evidence (bold-flare-79 comment): "the held aggregate verifier
    keeps an attached owner on the epic branch, so completed fix children stay
    reserved and are never collected. The only exit is the verifier closing
    FAIL." Tracked as eager-apex-93. bold-flare-79 (on main) stages the first
    red but does not release the verifier. stark-ridge-78 (3399edc2d, on main)
    covers an escalated no-progress collector, not a held verifier.

    Required: once trusted exact-head CI is red, either the red conclusion
    releases or supersedes the held verifier, or reopen-collection can collect
    the completed fix child past it. Either way the verifier's fence stops
    authorizing writes.
    """
    case = train_epic
    red_head, verifier_id = await _failed_aggregate(case)
    checkpoint = await case.db.get_integration_checkpoint("epic")
    async with case.db.immediate() as conn:
        # The verifier is still held: it is in progress on a live session, it
        # has not closed, and its owner row is attached to its checkout.
        await conn.execute(
            update(t.tasks).where(t.tasks.c.id == verifier_id).values(status="IN_PROGRESS")
        )
        await conn.execute(
            delete(t.task_completion_records).where(
                t.task_completion_records.c.task_id == verifier_id
            )
        )
        await conn.execute(
            update(t.integration_branch_owners)
            .where(t.integration_branch_owners.c.ref == "aq/epic")
            .values(handoff_state="attached", session_id="verifier-session",
                    workspace_id="verifier-workspace")
        )
        # Trusted exact-head CI concluded red on the aggregate head.
        await conn.execute(
            insert(t.integration_check_evidence).values(
                id="red-aggregate-check", operation_id=case.operation_id,
                parent_task_id="epic", parent_generation=checkpoint["generation"],
                parent_head_sha=red_head, producer_id="forge", workflow_id="ci",
                run_id="red-run", attempt=1, required_check_version="test",
                checks={"unit": "failure"}, conclusion="failure",
                classification="conclusive", observed_at=50.0,
            )
        )
    old_owner = await _owner(case)
    assert (old_owner["owner_id"], old_owner["handoff_state"]) == (verifier_id, "attached")
    assert (await case.db.get_task("epic.3")).status is TaskStatus.COMPLETED
    await _reopen_and_collect_fix(case, red_head, verifier_id, old_owner)
