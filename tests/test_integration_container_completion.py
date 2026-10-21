"""A held writer that gains children closes at its exact final published head.

Real Git and disposable PostgreSQL. Under ``integration.git_first: active`` no
parent episode collects such a container: the train's epic lane does, from a
completion and checkpoint that must agree exactly. Every refusal is a
fail-closed no-write; review, CI and delivery stay separate train facts.
"""
from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import delete, insert, select, update

from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    integration_branch_owners,
    task_completion_records,
    task_integration_checkpoints,
    tasks,
)
from src.git.manager import GitManager
from src.integration.delivery_truth import load_delivery_requests
from src.integration.hierarchy import HierarchyIntegration, verify_workspace_checkpoint
from src.integration.models import BranchKey
from src.integration.ownership import BranchOwnership
from src.integration.provenance import record_worker_completion
from src.integration.train import TrainTarget
from src.models import RepoConfig, RepoSourceType, Task, TaskCompletion, TaskStatus, Workspace
from tests.test_delivery_consumers import git
from tests.test_integration_train_sources import (  # noqa: F401 - pytest fixture
    MAIN,
    completed,
    hosted_train,
    review_epic,
    snapshot,
    world,
)

EPIC = TrainTarget("p", "r", "refs/heads/aq/epic", "epic")
REQUIRED = {"version": "v1", "names": ["unit"], "producer_id": "15368"}


class Held:
    """The container's own writer: a separate checkout locked by the task."""

    def __init__(self, train_world, tmp_path):
        self.world, self.db, self.origin = train_world, train_world.db, train_world.origin
        self.checkout = tmp_path / "worker"
        git(tmp_path, "clone", "-q", self.origin.url, str(self.checkout))
        for key, value in (("user.name", "Worker"), ("user.email", "worker@example.test"),
                           ("maintenance.auto", "false"), ("gc.auto", "0")):
            git(self.checkout, "config", "--local", key, value)
        self.git = GitManager()

    def publish(self, name: str, *, push: bool = True) -> str:
        git(self.checkout, "fetch", "-q", "origin")
        git(self.checkout, "checkout", "-q", "-B", "aq/epic", "origin/aq/epic")
        (self.checkout / f"epic-{name}.txt").write_text(name + "\n")
        git(self.checkout, "add", ".")
        git(self.checkout, "commit", "-qm", f"epic {name}")
        if push:
            git(self.checkout, "push", "-q", "origin", "aq/epic")
        return git(self.checkout, "rev-parse", "HEAD")

    def hierarchy(self) -> HierarchyIntegration:
        async def verify_checkpoint(task_row, repo_row, head):
            return await verify_workspace_checkpoint(self.db, self.git, task_row, repo_row, head)

        async def verify_ancestry(_repo_row, ancestor, descendant):
            return await self.git.ais_ancestor(
                str(self.checkout), ancestor, descendant, strict=True,
            )

        return HierarchyIntegration(
            self.db, checkpoint_verifier=verify_checkpoint, ancestry_verifier=verify_ancestry,
        )

    async def checkpoint(self, head: str, *, generation: int = 0, epoch: int = 1) -> dict:
        return await self.hierarchy().checkpoint_container_completion(
            "epic", head, generation, expect_claim_epoch=epoch,
        )

    async def close(self, head: str, close_id: str = "epic-close") -> None:
        """The session close's git-completion leg after a granted checkpoint."""
        task = await self.db.get_task("epic")
        source = await record_worker_completion(
            self.db, self.git, task, await self.db.get_project("p"),
            str(self.checkout), close_id, commit=head,
        )
        assert source == head
        await self.db.save_task_completion(TaskCompletion(
            id=close_id, task_id="epic", outcome="pass", branch="aq/epic",
            commits=[head], completed_at=time.time(),
        ))
        await self.db.transition_task("epic", TaskStatus.COMPLETED)


async def checkpoint_row(db) -> dict:
    async with db._engine.connect() as conn:
        return dict((await conn.execute(select(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "epic",
        ))).mappings().one())


@pytest.fixture
async def held(world, tmp_path):  # noqa: F811 - imported pytest fixture
    """A claimed epic leaf with its own branch, origin, checkpoint and fence."""
    from src.database.tables import task_branch_origins

    db, origin = world.db, world.origin
    base = git(origin.url, "rev-parse", "main")
    git(origin.clone, "push", "-q", "origin", "main:aq/epic")
    await db.create_task(Task(
        id="epic", project_id="p", repo_id="r", title="epic", description="",
        branch_name="aq/epic", status=TaskStatus.IN_PROGRESS, claim_epoch=1,
    ))
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic").values(claim_epoch=1))
        await conn.execute(insert(task_branch_origins).values(
            id="epic-origin", task_id="epic", repository_id="r", branch_name="aq/epic",
            base_sha=base, creation_generation=0, reserved=True, materialized=True,
            created_at=time.time(),
        ))
        await conn.execute(insert(task_integration_checkpoints).values(
            task_id="epic", repository_id="r", branch="aq/epic", checkpoint_sha=base,
            state="working", updated_at=time.time(),
        ))
    await BranchOwnership(db).acquire(BranchKey(repository_id="r", branch="aq/epic"), "epic", "worker")
    case = Held(world, tmp_path)
    await db.create_workspace(Workspace(
        id="epic-worker", project_id="p", workspace_path=str(case.checkout),
        source_type=RepoSourceType.LINK, locked_by_task_id="epic",
    ))
    case.base = base
    # The leaf discovers required work: two children routed to its branch.
    case.children = [await completed(world, tid, parent="epic") for tid in ("child-a", "child-b")]
    case.train, case.github, _ = await hosted_train(world)
    async with db._engine.begin() as conn:
        from src.database.tables import projects

        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_policy={
                "parent": {"required_checks": REQUIRED, "admission": "authorized"},
                "root": {"required_checks": REQUIRED, "admission": "reviewed"},
            },
        ))
    return case


async def integrate_children(case) -> str:
    """The train collects the completed children into the held epic branch."""
    testing = await case.train.visit(EPIC)
    assert testing.state == "testing", testing
    case.github.runs[testing.candidate_sha] = "success"
    delivered = await case.train.visit(EPIC)
    assert delivered.state == "delivered", delivered
    for child in case.children:
        git(case.origin.url, "merge-base", "--is-ancestor", child, delivered.target_sha)
    return delivered.target_sha


async def test_held_container_closes_at_exact_head_and_is_admitted(held):
    db = held.db
    await integrate_children(held)
    head = held.publish("final")
    granted = await held.checkpoint(head)
    assert granted == {"outcome": "checkpointed", "task_id": "epic",
                       "generation": 0, "head_sha": head}
    await held.close(head)
    checkpoint = await checkpoint_row(db)
    completion = await db.get_task_completion("epic")
    assert checkpoint["checkpoint_sha"] == head and checkpoint["episode_id"] is None
    assert completion.commits == [head]
    assert git(held.origin.url, "rev-parse", "refs/heads/aq/epic") == head
    # The stacked-admission gate (stacked_branches.refresh): the current pass
    # completion's reported source is exactly the native checkpoint.
    request = (await load_delivery_requests(
        db, ["epic"], repository_id="r", target_ref=MAIN.target_ref, reduced=True,
    ))["epic"]
    assert request.completion_id == "epic-close"
    assert request.reported_source == checkpoint["checkpoint_sha"] == head
    # Review and CI are the train's own gates on the exact head, never implied.
    waiting = await held.train.visit(EPIC)
    assert waiting.state == "blocked", waiting
    codes = {blocker["code"] for blocker in waiting.detail["blockers"]}
    assert "review_missing" in codes or "checks_unknown" in codes, codes
    case = held
    case.target = EPIC
    held.github.runs[head] = "success"
    await review_epic(case)
    assert (await held.train.visit(EPIC)).state == "idle"
    settled = await db.get_task_completion("epic")
    assert settled.commits == [head]
    assert (await checkpoint_row(db))["checkpoint_sha"] == head
    testing = await held.train.visit(MAIN)
    assert testing.state == "testing", testing
    assert git(held.origin.url, "rev-parse", "main") == held.base
    held.github.runs[testing.candidate_sha] = "success"
    assert (await held.train.visit(MAIN)).state == "delivered"
    git(held.origin.url, "merge-base", "--is-ancestor", head, "refs/heads/main")


async def test_unreviewed_or_unchecked_container_is_not_delivered(held):
    await integrate_children(held)
    head = held.publish("final")
    await held.checkpoint(head)
    await held.close(head)
    held.target = EPIC
    # Checks without review, then review without checks: never a parent completion.
    held.github.runs[head] = "success"
    assert (await held.train.visit(EPIC)).state == "blocked"
    del held.github.runs[head]
    await review_epic(held)
    assert (await held.train.visit(EPIC)).state == "blocked"
    assert (await held.db.get_task_completion("epic")).id == "epic-close"
    assert (await held.train.visit(MAIN)).batch_id is None
    assert git(held.origin.url, "rev-parse", "main") == held.base


async def refused(held, head, code, *, repository=None, **kwargs):
    if repository is not None:
        await held.db.create_repo(RepoConfig(
            id=repository, project_id="p", source_type=RepoSourceType.CLONE, url=held.origin.url,
        ))
        async with held.db._engine.begin() as conn:
            await conn.execute(update(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == "epic",
            ).values(repository_id=repository))
    before = await checkpoint_row(held.db)
    with pytest.raises(HierarchyError) as raised:
        await held.checkpoint(head, **kwargs)
    assert raised.value.code == code, raised.value
    after = await checkpoint_row(held.db)
    assert after == before
    assert await held.db.get_task_completion("epic") is None
    return raised.value


async def test_stale_epoch_generation_and_claim_refuse_without_writes(held):
    await integrate_children(held)
    head = held.publish("final")
    await refused(held, head, "stale", epoch=2)
    await refused(held, head, "stale", generation=1)
    await held.db.transition_task("epic", TaskStatus.PAUSED)
    await refused(held, head, "stale")


async def test_unpublished_or_changed_remote_refuses_without_writes(held):
    await integrate_children(held)
    local = held.publish("local only", push=False)
    await refused(held, local, "dirty")
    head = held.publish("final")
    git(held.origin.clone, "fetch", "-q", "origin")
    git(held.origin.clone, "checkout", "-q", "-B", "aq/epic", "origin/aq/epic")
    (held.origin.clone / "racer.txt").write_text("concurrent\n")
    git(held.origin.clone, "add", ".")
    git(held.origin.clone, "commit", "-qm", "concurrent remote change")
    git(held.origin.clone, "push", "-q", "origin", "aq/epic")
    await refused(held, head, "dirty")


async def test_wrong_repository_or_branch_refuses_without_writes(held):
    await integrate_children(held)
    head = held.publish("final")
    async with held.db._engine.begin() as conn:
        await conn.execute(update(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "epic",
        ).values(branch="aq/elsewhere"))
    await refused(held, head, "delivery_target_fixed")
    async with held.db._engine.begin() as conn:
        await conn.execute(update(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "epic",
        ).values(branch="aq/epic"))
    await refused(held, head, "delivery_target_fixed", repository="other")


async def owners(held, **values) -> None:
    async with held.db._engine.begin() as conn:
        await conn.execute(update(integration_branch_owners).where(
            integration_branch_owners.c.repository_id == "r",
            integration_branch_owners.c.ref == "aq/epic",
        ).values(**values))


async def test_live_foreign_pending_or_missing_owner_refuses_without_writes(held):
    await integrate_children(held)
    head = held.publish("final")
    # Another live writer, the container's own pending handoff, then no record.
    await owners(held, owner_id="someone-else", owner_role="worker", handoff_state="attached")
    await refused(held, head, "stale")
    await owners(held, owner_id="epic", owner_role="worker", handoff_state="handoff_pending")
    await refused(held, head, "stale")
    async with held.db._engine.begin() as conn:
        await conn.execute(delete(integration_branch_owners).where(
            integration_branch_owners.c.ref == "aq/epic",
        ))
    await refused(held, head, "stale")


async def test_train_released_branch_owner_is_the_only_writer(held):
    """After the train collects children, its released fence leaves the claim writing."""
    await integrate_children(held)
    async with held.db._engine.connect() as conn:
        row = (await conn.execute(select(integration_branch_owners).where(
            integration_branch_owners.c.ref == "aq/epic",
        ))).mappings().one()
    assert (row["owner_id"], row["handoff_state"]) == ("service:integration-train", "released")
    head = held.publish("final")
    assert (await held.checkpoint(head))["head_sha"] == head


async def test_unresolved_or_unproven_child_refuses_without_writes(held):
    await integrate_children(held)
    head = held.publish("final")
    # A newly discovered required child is still open.
    await completed(held.world, "child-c", parent="epic", done=False)
    error = await refused(held, head, "invariant_error")
    assert "child-c" in str(error)
    await held.db.transition_task("child-c", TaskStatus.COMPLETED)
    # Completed with no passing completion: uncertain descendant provenance.
    error = await refused(held, head, "invariant_error")
    assert "child-c" in str(error)
    async with held.db._engine.begin() as conn:
        await conn.execute(insert(task_completion_records).values(
            id="child-c-close", task_id="child-c", outcome="pass",
            commits=json.dumps([held.origin.work("child-c", "unmerged")]), completed_at=time.time(),
        ))
    # A recorded child source the published container head does not contain.
    await refused(held, head, "stale_head")


async def test_fence_moved_during_remote_verification_refuses_without_writes(held):
    await integrate_children(held)
    head = held.publish("final")
    hierarchy = held.hierarchy()
    verify = hierarchy.checkpoint_verifier

    async def train_delivers_meanwhile(task_row, repo_row, oid):
        verified = await verify(task_row, repo_row, oid)
        await owners(held, owner_id="service:integration-train", owner_role="integration",
                     handoff_state="released", fence_token=3)
        return verified

    hierarchy.checkpoint_verifier = train_delivers_meanwhile
    before = await checkpoint_row(held.db)
    with pytest.raises(HierarchyError, match="owner changed"):
        await hierarchy.checkpoint_container_completion(
            "epic", head, 0, expect_claim_epoch=1,
        )
    assert await checkpoint_row(held.db) == before
