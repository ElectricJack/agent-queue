"""A parent collection cancelled mid-episode is reopened without stranding receipts.

Regression for calm-grove-25 and azure-vault-92 (2026-10-02): ``aq integration
cancel-preserving`` on an expired parent repair cancelled the parent's only
collection operation.  The checkpoint stayed ``awaiting_children`` in its
episode, completed children's receipts stayed bound to that episode, the
collector's fence was released, the stage delegates were archived, and a later
child's conflict intent had no repair.  ``aq integration redrive-child``
refused ("no live collection operation") and ``reserve_episode_on`` returned
the cancelled operation.  ``aq integration reopen-collection`` reactivates it
in place behind a dry run; these tests drive the real promotion, repair,
historical cancellation fixtures and current recovery paths against a real Git origin.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import (
    integration_branch_owners,
    integration_repair_operations,
    integration_repair_stages,
    playbook_artifacts,
    task_branch_origins,
    task_integration_checkpoints,
    tasks,
)
from src.git.manager import GitManager
from src.integration.cancelled_collection_recovery import (
    CancelledCollectionRecovery,
)
from src.integration.child_delivery import ChildDelivery
from src.integration.collection import CollectionService
from src.integration.delegate_release import archive_obsolete_delegates
from src.integration.hierarchy import HierarchyIntegration
from src.integration.models import (
    ArtifactSnapshot,
    HierarchicalIntegrationPolicy,
    IntegrationBoundaryPolicy,
    PlaybookRoute,
    RepairPolicy,
    RequiredCheckSet,
)
from src.integration.repository_git import RepositoryGit
from src.integration.repair import RepairService
from src.integration.verifier_subject import latest_red_parent_evidence
from src.models import (
    Project, RepoConfig, RepoSourceType, Task, TaskStatus,
)

_AMBIENT_IDENTITY_KEYS = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_DATE",
)


def _git(args: list[str], cwd: Path | None = None) -> str:
    env = {key: value for key, value in os.environ.items() if key not in _AMBIENT_IDENTITY_KEYS}
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _policy() -> tuple[dict, ArtifactSnapshot]:
    artifact = ArtifactSnapshot(
        playbook_id="hierarchical-delivery",
        artifact_sha256="sha256:" + "a" * 64,
        schema_generation=2,
        contract_fingerprint="sha256:" + "b" * 64,
        source_digest="sha256:" + "c" * 64,
        compiler_build="test",
        version=1,
    )
    boundary = IntegrationBoundaryPolicy(
        required_checks=RequiredCheckSet(version="test", names=("unit",), producer_id="forge"),
        repair=RepairPolicy(debug_intelligence_class="high", on_exhausted="continue"),
        route=PlaybookRoute(
            playbook_id="hierarchical-delivery",
            scope="project",
            scope_identifier="p",
            artifact=artifact,
        ),
        primary_intelligence_class="standard",
        verifier_intelligence_class="high",
    )
    policy = HierarchicalIntegrationPolicy(
        parent=boundary, root=boundary, branchless_parent="verifier", on_failed_child="block"
    )
    return policy.model_dump(mode="json"), artifact


def _commit_on(work: Path, branch: str, base: str, path: str, text: str) -> str:
    _git(["switch", "-c", branch, base], work)
    (work / path).write_text(text)
    _git(["add", path], work)
    _git(["commit", "-m", f"{branch} work"], work)
    _git(["push", "origin", branch], work)
    return _git(["rev-parse", "HEAD"], work)


@pytest.fixture
async def case(tmp_path, reuse_database):
    """Train epic ``epic`` with two COMPLETED children that edit the same line.

    ``epic.1`` delivers cleanly; ``epic.2`` then conflicts with it, exactly as
    calm-grove-25.3 conflicted after .1 and .2 were receipted.  ``epic.3`` is
    still to do.
    """
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(["init", "--bare", "--initial-branch=main", str(origin)])
    _git(["clone", str(origin), str(work)])
    _git(["config", "user.name", "Seed"], work)
    _git(["config", "user.email", "seed@example.test"], work)
    (work / "shared.txt").write_text("base\n")
    _git(["add", "shared.txt"], work)
    _git(["commit", "-m", "base"], work)
    base = _git(["rev-parse", "HEAD"], work)
    _git(["push", "origin", "main"], work)
    _git(["push", "origin", f"{base}:refs/heads/aq/epic"], work)
    heads = {
        "epic.1": _commit_on(work, "aq/epic.1", base, "shared.txt", "one\n"),
        "epic.2": _commit_on(work, "aq/epic.2", base, "shared.txt", "two\n"),
    }

    db = await reuse_database("cancelled-collection.db")
    await db.create_project(Project(id="p", name="train project"))
    await db.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url=str(origin),
            default_branch="main",
        )
    )
    policy, artifact = _policy()
    async with db.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                **artifact.model_dump(),
                scope="project",
                scope_identifier="p",
                profile_fingerprint="",
                path="/tmp/hierarchy-artifact",
                size_bytes=1,
                validation="{}",
                created_at=1.0,
            )
        )
    await db.update_project(
        "p",
        hierarchical_integration_mode="train",
        integration_repository_id="repo",
        hierarchical_integration_policy=policy,
    )
    hierarchy = HierarchyIntegration(
        db,
        # The collector observes the live parent tip, as production does.
        default_head_resolver=lambda _repo, branch: _git(
            ["ls-remote", str(origin), f"refs/heads/{branch.removeprefix('refs/heads/')}"]
        ).split()[0],
        checkpoint_verifier=lambda _task, _repo, head_sha: head_sha,
    )
    await db.create_task(
        Task(
            id="epic", project_id="p", repo_id="repo", title="epic", description="epic",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    await hierarchy.file_children(
        "epic", [{"title": "one"}, {"title": "two"}, {"title": "three"}], 0
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(task_branch_origins).values(materialized=True, materialized_at=2.0)
        )
        for child, head in heads.items():
            await conn.execute(
                update(tasks).where(tasks.c.id == child).values(status="COMPLETED", updated_at=3.0)
            )
            await conn.execute(
                update(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == child)
                .values(checkpoint_sha=head, updated_at=3.0)
            )
    bootstrapped = await hierarchy.bootstrap_container_collection("epic")
    assert bootstrapped["outcome"] == "checkpointed"
    promotion = RepositoryGit(db, data_dir=tmp_path / "data", git_manager=GitManager())
    async with db._engine.connect() as conn:
        operation_id = (
            await conn.execute(select(integration_repair_operations.c.id))
        ).scalar_one()
    yield SimpleNamespace(
        db=db, hierarchy=hierarchy, promotion=promotion, base=base, heads=heads,
        work=work, origin=origin, tmp_path=tmp_path, operation_id=operation_id,
    )


def _collector(case):
    return CollectionService(
        case.db,
        hierarchy_service_factory=lambda: case.hierarchy,
        child_delivery=ChildDelivery(case.db, case.promotion),
    )




async def _rows(db, table, *where) -> list[dict]:
    async with db._engine.connect() as conn:
        return [dict(row) for row in (await conn.execute(select(table).where(*where))).mappings()]








def _red_unit_row(*, observed_at: float) -> dict:
    """A conclusive red parent-evidence row for the ``unit`` required check."""
    return {
        "id": "red-unit",
        "operation_id": "op",
        "parent_task_id": "epic",
        "parent_generation": 1,
        "parent_head_sha": "f" * 40,
        "producer_id": "forge",
        "workflow_id": "suite-a",
        "run_id": "run-a",
        "attempt": 1,
        "required_check_version": "v1",
        "checks": {"unit": "failure"},
        "conclusion": "failure",
        "classification": "conclusive",
        "observed_at": observed_at,
    }


def test_latest_red_parent_evidence_survives_a_later_green_sibling_suite() -> None:
    """A green sibling observed *after* a red sibling keeps the red visible.

    Regression: required checks can span multiple suites; ``ci.py`` stamps one
    evidence row per suite with ``observed_at=self.clock()``.  When the green
    sibling's row is written a fraction later, only the group-by-suite rule sees
    the red; a naive ``max(observed_at)`` picks the green sibling and drops it.
    """
    operation = {
        "id": "op",
        "parent_task_id": "epic",
        "required_check_version": "v1",
        "policy_snapshot": {"parent": {
            "required_checks": {"version": "v1", "names": ["unit"], "producer_id": "forge"},
        }},
    }
    checkpoint = {"generation": 1, "checkpoint_sha": "f" * 40}
    red = _red_unit_row(observed_at=100.0)
    green_sibling = dict(
        _red_unit_row(observed_at=100.0005),
        id="sibling-green",
        workflow_id="suite-b",
        run_id="run-b",
        checks={"deploy": "success"},
        conclusion="success",
    )
    for order in ([red, green_sibling], [green_sibling, red]):
        found = latest_red_parent_evidence(
            order, operation=operation, checkpoint=checkpoint
        )
        assert found is not None and found["id"] == "red-unit", (
            f"red must surface for input order {order!r}"
        )


def test_latest_red_parent_evidence_still_superseded_by_later_green_same_suite() -> None:
    """A later green of the *same* suite supersedes its prior red (no regression)."""
    operation = {
        "id": "op",
        "parent_task_id": "epic",
        "required_check_version": "v1",
        "policy_snapshot": {"parent": {
            "required_checks": {"version": "v1", "names": ["unit"], "producer_id": "forge"},
        }},
    }
    checkpoint = {"generation": 1, "checkpoint_sha": "f" * 40}
    red = _red_unit_row(observed_at=100.0)
    green_rerun = dict(
        _red_unit_row(observed_at=101.0),
        id="green-rerun",
        checks={"unit": "success"},
        conclusion="success",
    )
    found = latest_red_parent_evidence(
        [red, green_rerun], operation=operation, checkpoint=checkpoint
    )
    assert found is None


def test_latest_red_parent_evidence_empty_and_no_required_red() -> None:
    operation = {
        "id": "op",
        "parent_task_id": "epic",
        "required_check_version": "v1",
        "policy_snapshot": {"parent": {
            "required_checks": {"version": "v1", "names": ["unit"], "producer_id": "forge"},
        }},
    }
    checkpoint = {"generation": 1, "checkpoint_sha": "f" * 40}
    assert latest_red_parent_evidence([], operation=operation, checkpoint=checkpoint) is None
    green_only = [dict(_red_unit_row(observed_at=100.0),
                       checks={"deploy": "success"}, conclusion="success")]
    assert latest_red_parent_evidence(
        green_only, operation=operation, checkpoint=checkpoint
    ) is None


































async def _operation(case) -> dict:
    [row] = await _rows(
        case.db, integration_repair_operations,
        integration_repair_operations.c.id == case.operation_id,
    )
    return row


async def _owner(case) -> dict:
    [row] = await _rows(
        case.db, integration_branch_owners, integration_branch_owners.c.ref == "aq/epic"
    )
    return row


def _remote_tip(case) -> str:
    return _git(["ls-remote", str(case.origin), "refs/heads/aq/epic"]).split()[0]




async def _cancel(case, reason="obsolete expired repair") -> dict:
    """Seed a historical cancellation, whose public creation control is retired."""
    from src.integration.delegate_release import release_delegates

    now = time.time()
    async with case.db.immediate() as conn:
        delegates = list((await conn.execute(select(integration_repair_stages.c.repair_task_id).where(
            integration_repair_stages.c.operation_id == case.operation_id,
            integration_repair_stages.c.repair_task_id.is_not(None),
        ))).scalars())
        await conn.execute(update(integration_branch_owners).where(
            integration_branch_owners.c.owner_id.in_([case.operation_id, *delegates]),
            integration_branch_owners.c.handoff_state == "reserved",
            integration_branch_owners.c.session_id.is_(None),
            integration_branch_owners.c.workspace_id.is_(None),
        ).values(handoff_state="released", updated_at=now))
        await conn.execute(update(integration_repair_operations).where(
            integration_repair_operations.c.id == case.operation_id,
        ).values(state="cancelled", updated_at=now))
        await conn.execute(update(integration_repair_stages).where(
            integration_repair_stages.c.operation_id == case.operation_id,
            integration_repair_stages.c.state.not_in(["passed", "cancelled"]),
        ).values(state="cancelled", completed_at=now))
    await release_delegates(
        case.db, operation_ids=[case.operation_id], now=now, released_by="historical_fixture",
    )
    return {"outcome": "cancelled", "operation_id": case.operation_id, "reason": reason}


async def _expired_repair_then_cancel(case, conflict) -> str:
    """Stage 0 repairs the conflict, then the operator cancels and the delegate archives."""
    repair = RepairService(case.db)
    started = await repair.start(case.operation_id, conflict["expected_target"], case.operation_id)
    assert started["outcome"] == "started"
    dispatched = await repair.dispatch(case.operation_id, 0)
    assert dispatched["outcome"] == "dispatched"
    delegate = dispatched["repair_task_id"]
    await _cancel(case)
    archived = await archive_obsolete_delegates(case.db, operation_ids=[case.operation_id])
    assert archived["archived_delegates"] == [delegate]
    return delegate


def _recovery(case, dispatch=None):
    return CancelledCollectionRecovery(case.db, case.promotion, dispatch=dispatch)










# ---------------------------------------------------------------------------
# The production shape: receipts, one later conflict, an archived delegate
# ---------------------------------------------------------------------------


















# ---------------------------------------------------------------------------
# Refusals: nothing ambiguous, live, held or moved is ever overridden
# ---------------------------------------------------------------------------












@pytest.mark.parametrize(
    ("state", "outcome"),
    [("active", "nothing_to_reopen"), ("human_required", "not_eligible"),
     ("completed", "not_eligible")],
)
async def test_only_a_cancelled_operation_is_reopened(case, state, outcome):
    async with case.db.immediate() as conn:
        await conn.execute(update(integration_repair_operations).values(state=state))
    result = await _recovery(case).run("epic")
    assert result["outcome"] == outcome


async def test_unknown_and_non_collecting_tasks(case):
    assert (await _recovery(case).run("missing"))["outcome"] == "not_found"
    leaf = await _recovery(case).run("epic.3")
    assert leaf["outcome"] == "not_eligible"
