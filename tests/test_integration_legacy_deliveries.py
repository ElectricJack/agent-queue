"""Legacy delivered children: observe readiness and ``adopt-legacy-deliveries``.

Real PostgreSQL and real Git: a bare ``origin.git`` and a clone the tests push
from.  A parent that finished before the train has no parent collection and
never will, so its terminal children can never get train receipts.  Status
accepts a child whose current completion git finds on the default branch (no
delivery row is consulted); ``LegacyDeliveryAdoption`` proves the rest against
the default-branch tip and lists what it cannot prove.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, select, update

from src.database.tables import (
    events,
    integration_legacy_deliveries,
    integration_parent_episodes,
    integration_repair_operations,
    projects,
    task_completion_records,
    task_integration_checkpoints,
    tasks,
)
from src.git.manager import GitManager
from src.integration.controls import IntegrationControlService
from src.integration.delivery_observer import DeliveryObserver
from src.integration.development import DevelopmentIntegration
from src.integration.legacy_deliveries import (
    ABANDONED,
    ADOPTED,
    BRANCH_TIP,
    CHILD_NOT_COMPLETED,
    CONTENT_EQUIVALENT,
    DEVELOPMENT_DELIVERY,
    NO_PARENT_COLLECTION,
    NOT_ON_DEFAULT_BRANCH,
    OPERATOR_ACCEPTED,
    PARENT_NOT_TERMINAL,
    STATE_CHANGED,
    SUPERSEDED,
    UNPROVEN,
    LegacyDeliveryAdoption,
)
from src.integration.legacy_repositories import LegacyRepositoryBinding
from src.integration.parent_completion import ParentCompletion
from src.integration.status import IntegrationStatusService
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus

PRINCIPAL = "supervisor session:super-p"


def git(path, *args) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE
    ).strip()


class Env:
    def __init__(self, *, db, clone, tmp_path):
        self.db, self.clone, self.tmp_path = db, clone, tmp_path

    def branch(self, name: str, *, landed: bool) -> str:
        """Push *name* with one commit; *landed* also fast-forwards main onto it."""
        git(self.clone, "checkout", "-q", "-b", name, "main")
        (self.clone / f"{name.replace('/', '-')}.txt").write_text("work\n")
        git(self.clone, "add", ".")
        git(self.clone, "commit", "-q", "-m", name)
        git(self.clone, "push", "-q", "origin", name)
        tip = git(self.clone, "rev-parse", "HEAD")
        git(self.clone, "checkout", "-q", "main")
        if landed:
            git(self.clone, "merge", "-q", "--ff-only", name)
            git(self.clone, "push", "-q", "origin", "main")
        return tip

    def commit(self, name: str, *, on: str = "main", content: str = "work\n") -> str:
        """Commit one file named after *name* on *on* and push it; return the commit."""
        git(self.clone, "checkout", "-q", on)
        (self.clone / f"{name}.txt").write_text(content)
        git(self.clone, "add", ".")
        git(self.clone, "commit", "-q", "-m", name)
        git(self.clone, "push", "-q", "origin", on)
        return git(self.clone, "rev-parse", "HEAD")

    def cherry_pick(self, *commits: str) -> str:
        """Re-deliver *commits* onto main under new commit ids; return main's tip."""
        git(self.clone, "checkout", "-q", "main")
        for commit in commits:
            # A distinct committer date guarantees a new commit id.
            subprocess.check_call(
                ["git", "-C", str(self.clone), "cherry-pick", "-x", commit],
                stdout=subprocess.DEVNULL,
                env={**os.environ, "GIT_COMMITTER_DATE": "2030-01-01T00:00:00"},
            )
        git(self.clone, "push", "-q", "origin", "main")
        return git(self.clone, "rev-parse", "HEAD")

    def amended(self, name: str) -> tuple[str, str]:
        """Push *name*, amend and force-push it, and land only the amendment.

        The close recorded the first commit; the branch tip that reached main
        is the amended one.  Returns ``(recorded, landed)``.
        """
        recorded = self.branch(name, landed=False)
        git(self.clone, "checkout", "-q", name)
        git(self.clone, "commit", "-q", "--amend", "-m", f"{name} (amended)")
        git(self.clone, "push", "-q", "-f", "origin", name)
        landed = git(self.clone, "rev-parse", "HEAD")
        git(self.clone, "checkout", "-q", "main")
        git(self.clone, "merge", "-q", "--ff-only", name)
        git(self.clone, "push", "-q", "origin", "main")
        return recorded, landed

    def collect(self, collection: str, source: str) -> str:
        """Merge *source* into a parent *collection* branch and land that on main."""
        git(self.clone, "checkout", "-q", "-b", collection, "main")
        git(self.clone, "merge", "-q", "--no-ff", "-m", f"collect {source}", source)
        git(self.clone, "push", "-q", "origin", collection)
        tip = git(self.clone, "rev-parse", "HEAD")
        git(self.clone, "checkout", "-q", "main")
        git(self.clone, "merge", "-q", "--ff-only", collection)
        git(self.clone, "push", "-q", "origin", "main")
        return tip

    def delete_branch(self, name: str) -> None:
        git(self.clone, "push", "-q", "origin", "--delete", name)
        git(self.clone, "branch", "-q", "-D", name)

    async def completion(self, task_id: str, commit: str, *, retain: bool = False) -> None:
        """Close *task_id* on *commit*; *retain* keeps it in git as a worker close does.

        Without it the close is an unlabelled legacy generation: git cannot say
        its work is complete, so status flags it until adoption proves it.
        """
        async with self.db.immediate() as conn:
            await conn.execute(
                insert(task_completion_records).values(
                    id=f"completion-{task_id}",
                    task_id=task_id,
                    outcome="pass",
                    commits=json.dumps([commit]),
                    completed_at=time.time(),
                )
            )
        if retain:
            from src.integration.provenance import (
                CompletedSource,
                CompletionIdentity,
                GitProvenance,
            )

            url = git(self.clone, "remote", "get-url", "origin")
            await GitProvenance(GitManager(), str(self.clone), repository_url=url
                                ).write_completion(
                CompletedSource(
                    CompletionIdentity("p", "r", task_id, f"completion-{task_id}"), commit
                )
            )

    async def collection(self, parent: str, *, state: str) -> None:
        """Seed a parent collection (episode, checkpoint, operation) in *state*."""
        now = time.time()
        async with self.db.immediate() as conn:
            await conn.execute(
                insert(integration_parent_episodes).values(
                    id=f"episode-{parent}",
                    parent_task_id=parent,
                    repository_id="r",
                    generation=0,
                    pre_collection_checkpoint_sha="a" * 40,
                    created_at=now,
                )
            )
            await conn.execute(
                insert(task_integration_checkpoints).values(
                    task_id=parent,
                    repository_id="r",
                    branch=f"aq/{parent}",
                    generation=1,
                    checkpoint_sha="a" * 40,
                    state="awaiting_children",
                    episode_id=f"episode-{parent}",
                    updated_at=now,
                )
            )
            await conn.execute(
                insert(integration_repair_operations).values(
                    id=f"operation-{parent}",
                    target_kind="parent",
                    parent_task_id=parent,
                    episode_id=f"episode-{parent}",
                    active_stage=0,
                    state=state,
                    policy_snapshot={},
                    artifact_snapshot={},
                    required_check_version="checks-v1",
                    created_at=now,
                    updated_at=now,
                )
            )

    async def task(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        parent: str | None = None,
        branch: str | None = None,
    ) -> None:
        await self.db.create_task(
            Task(
                id=task_id,
                project_id="p",
                title=task_id,
                description="",
                parent_task_id=parent,
                repo_id="r",
                branch_name=branch,
            )
        )
        await self.db.update_task(task_id, status=status)

    async def delivery(self, delivery_id: str, *, target_ref: str, prepared_sha: str, manifest):
        """A retired development journal row, as revision a00000000038 keeps it."""
        now = time.time()
        async with self.db.immediate() as conn:
            await conn.execute(
                insert(events).values(
                    event_type="development.legacy_provenance",
                    project_id="p",
                    payload=json.dumps({
                        "id": "legacy-provenance:" + delivery_id,
                        "legacy_id": delivery_id,
                        "project_id": "p",
                        "repository_id": "r",
                        "target_ref": target_ref,
                        "expected_sha": None,
                        "prepared_sha": prepared_sha,
                        "kind": "local",
                        "manifest": manifest,
                        "created_at": now,
                    }),
                    timestamp=now,
                )
            )

    async def observe(self) -> None:
        async with self.db.immediate() as conn:
            await conn.execute(
                update(projects)
                .where(projects.c.id == "p")
                .values(
                    hierarchical_integration_mode="observe",
                    hierarchical_integration_desired_mode="observe",
                )
            )

    def adoption(self) -> LegacyDeliveryAdoption:
        development = DevelopmentIntegration(
            self.db, data_dir=self.tmp_path / "data", git=GitManager()
        )
        return LegacyDeliveryAdoption(self.db, development=development)

    async def missing_receipts(self) -> dict[str, str | None]:
        """``aq integration status``'s ``missing_receipt`` blockers, ref -> cause."""
        status = await IntegrationControlService(self.db).status("p")
        assert status["effective_mode"] == "observe"
        return {
            blocker["ref"]: blocker.get("cause")
            for blocker in status["blockers"]
            if blocker["code"] == "missing_receipt"
        }

    async def recorded(self) -> dict[str, dict]:
        async with self.db._engine.connect() as conn:
            rows = (
                await conn.execute(select(integration_legacy_deliveries))
            ).mappings().all()
        return {row["task_id"]: dict(row) for row in rows}


@pytest.fixture
async def env(tmp_path, reuse_database):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    clone = tmp_path / "clone"
    git(tmp_path, "clone", str(origin), str(clone))
    git(clone, "config", "user.name", "Tester")
    git(clone, "config", "user.email", "tester@example.test")
    (clone / "base.txt").write_text("base\n")
    git(clone, "add", ".")
    git(clone, "commit", "-q", "-m", "base")
    git(clone, "push", "-q", "origin", "main")
    db = await reuse_database("legacy-deliveries.db")
    await db.create_project(Project(id="p", name="P"))
    await db.create_repo(
        RepoConfig(id="r", project_id="p", source_type=RepoSourceType.CLONE, url=str(origin))
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(projects).where(projects.c.id == "p").values(integration_repository_id="r")
        )
    # The daemon registers its git delivery observer on the database; status
    # and the legacy readers answer delivery from git through it.
    db.set_delivery_observer(
        DeliveryObserver(db, git=GitManager(), data_dir=tmp_path / "observer")
    )
    yield Env(db=db, clone=clone, tmp_path=tmp_path)


async def delivered_legacy_graph(env: Env) -> dict[str, str]:
    """A parent the development publisher finished before the train existed."""
    await env.task("done", TaskStatus.DEFINED)
    # Delivered straight to main by a close retained in git: git proves it.
    dev_sha = env.branch("aq/done.dev", landed=True)
    await env.task("done.dev", TaskStatus.COMPLETED, parent="done", branch="aq/done.dev")
    await env.completion("done.dev", dev_sha, retain=True)
    # Delivered into a development parent collection that later reached main,
    # its own branch cleaned up since, closed before provenance: status flags
    # it, and adoption tests the source a retired journal row locates.
    collected = env.branch("aq/done.collected", landed=False)
    collection = env.collect("aq/development/parent/done", "aq/done.collected")
    env.delete_branch("aq/done.collected")
    await env.task(
        "done.collected", TaskStatus.COMPLETED, parent="done", branch="aq/done.collected"
    )
    # Closed on a commit that was then amended: only the amended tip landed,
    # so git cannot prove the recorded completion and only the tip proves it.
    recorded, tip_sha = env.amended("aq/done.tip")
    await env.task("done.tip", TaskStatus.COMPLETED, parent="done", branch="aq/done.tip")
    await env.completion("done.tip", recorded)
    await env.db.update_task("done", status=TaskStatus.COMPLETED)
    await env.delivery(
        "delivery-parent",
        target_ref="refs/heads/aq/development/parent/done",
        prepared_sha=collection,
        manifest=[
            {"task_id": "done.collected", "source_sha": collected, "parent_task_id": "done"}
        ],
    )
    return {"dev": dev_sha, "tip": tip_sha, "recorded": recorded, "collected": collected}


async def unprovable_children(env: Env) -> None:
    """Children no proof reaches: lost work, a failed child, an open parent."""
    await env.task("gone", TaskStatus.DEFINED)
    env.branch("aq/gone.lost", landed=False)
    await env.task("gone.lost", TaskStatus.COMPLETED, parent="gone", branch="aq/gone.lost")
    await env.task("gone.failed", TaskStatus.FAILED, parent="gone")
    await env.db.update_task("gone", status=TaskStatus.COMPLETED)
    await env.task("open", TaskStatus.IN_PROGRESS)
    env.branch("aq/open.1", landed=True)
    await env.task("open.1", TaskStatus.COMPLETED, parent="open", branch="aq/open.1")


async def test_observe_status_is_clean_after_adopting_delivered_legacy_children(env):
    """The task's acceptance: no ``missing_receipt`` once the control has run."""
    shas = await delivered_legacy_graph(env)
    await env.observe()

    # Git already proves the retained close; no row does.  The unlabelled
    # legacy closes are flagged until adoption proves them in git.
    assert await env.missing_receipts() == {
        "done.tip": NO_PARENT_COLLECTION, "done.collected": NO_PARENT_COLLECTION,
    }

    result = await env.adoption().run("p", principal=PRINCIPAL)

    assert (result["outcome"], result["count"], result["dry_run"]) == ("adopted", 2, False)
    assert result["head_sha"] == git(env.clone, "rev-parse", "origin/main")
    by_task = {item["task_id"]: item for item in result["outcomes"]}
    assert by_task["done.tip"] == {
        "task_id": "done.tip",
        "parent_task_id": "done",
        "outcome": ADOPTED,
        "proof": BRANCH_TIP,
        "delivered_sha": shas["tip"],
        "development_delivery_id": None,
    }
    # The retired journal row only located the collected source; git proved it.
    assert by_task["done.collected"] == {
        "task_id": "done.collected",
        "parent_task_id": "done",
        "outcome": ADOPTED,
        "proof": CONTENT_EQUIVALENT,
        "delivered_sha": shas["collected"],
        "development_delivery_id": "delivery-parent",
    }
    recorded = await env.recorded()
    assert set(recorded) == {"done.tip", "done.collected"}
    assert recorded["done.collected"]["development_delivery_id"] == "delivery-parent"
    assert recorded["done.tip"]["operator_id"] == PRINCIPAL
    assert recorded["done.tip"]["target_sha"] == result["head_sha"]
    assert recorded["done.tip"]["target_ref"] == "refs/heads/main"

    assert await env.missing_receipts() == {}

    # Idempotent: nothing is left to adopt and nothing is written again.
    again = await env.adoption().run("p", principal=PRINCIPAL)
    assert (again["outcome"], again["count"], again["outcomes"]) == ("nothing_to_adopt", 0, [])
    assert await env.recorded() == recorded


async def test_a_misleading_delivery_row_proves_nothing(env):
    """A ``delivered`` row whose source is not on main is not a delivery."""
    await env.task("claimed", TaskStatus.DEFINED)
    lost = env.branch("aq/claimed.1", landed=False)
    await env.task("claimed.1", TaskStatus.COMPLETED, parent="claimed", branch="aq/claimed.1")
    await env.completion("claimed.1", lost)
    await env.db.update_task("claimed", status=TaskStatus.COMPLETED)
    await env.delivery(
        "delivery-lie",
        target_ref="refs/heads/main",
        prepared_sha=git(env.clone, "rev-parse", "main"),
        manifest=[{"task_id": "claimed.1", "source_sha": lost, "parent_task_id": "claimed"}],
    )
    await env.observe()

    assert await env.missing_receipts() == {"claimed.1": NO_PARENT_COLLECTION}
    result = await env.adoption().run("p", principal=PRINCIPAL, dry_run=True)
    assert [(i["task_id"], i["outcome"], i["cause"]) for i in result["outcomes"]] == [
        ("claimed.1", UNPROVEN, NOT_ON_DEFAULT_BRANCH)
    ]


async def test_git_proof_of_the_current_completion_is_a_development_delivery(env):
    """Adoption's ``development_delivery`` proof is git's answer, re-proved on the tip."""
    shas = await delivered_legacy_graph(env)
    await env.observe()
    adoption = env.adoption()
    view = await adoption.delivery.observe({"done.dev"})
    child = {
        "task_id": "done.dev", "parent_task_id": "done", "status": "COMPLETED",
        "parent_status": "COMPLETED", "branch_name": "aq/done.dev",
    }
    store = await adoption.development.store(await env.db.get_repo("r"))
    head = git(env.clone, "rev-parse", "origin/main")
    target = type("Target", (), {"sha": head, "tree": None})()

    item = await adoption._prove(store, target, "r", child, [], None, view.get("done.dev"))

    assert (item["outcome"], item["proof"], item["delivered_sha"]) == (
        ADOPTED, DEVELOPMENT_DELIVERY, shas["dev"],
    )


async def test_dry_run_reports_every_flagged_child_and_writes_nothing(env):
    await delivered_legacy_graph(env)
    await unprovable_children(env)
    await env.observe()

    result = await env.adoption().run("p", principal=PRINCIPAL, dry_run=True)

    assert (result["outcome"], result["count"], result["dry_run"]) == ("adopted", 2, True)
    assert {
        item["task_id"]: (item["outcome"], item.get("proof") or item.get("cause"))
        for item in result["outcomes"]
    } == {
        "done.tip": (ADOPTED, BRANCH_TIP),
        "done.collected": (ADOPTED, CONTENT_EQUIVALENT),
        "gone.failed": (UNPROVEN, CHILD_NOT_COMPLETED),
        "gone.lost": (UNPROVEN, NOT_ON_DEFAULT_BRANCH),
        "open.1": (UNPROVEN, PARENT_NOT_TERMINAL),
    }
    lost = next(item for item in result["outcomes"] if item["task_id"] == "gone.lost")
    assert "branch aq/gone.lost at" in lost["detail"]
    assert await env.recorded() == {}
    assert set(await env.missing_receipts()) == {
        "done.tip", "done.collected", "gone.failed", "gone.lost", "open.1",
    }


async def test_unprovable_children_stay_blocked_until_explicitly_accepted(env):
    await delivered_legacy_graph(env)
    await unprovable_children(env)
    await env.observe()

    await env.adoption().run("p", principal=PRINCIPAL)
    assert await env.missing_receipts() == {
        "gone.failed": NO_PARENT_COLLECTION,
        "gone.lost": NO_PARENT_COLLECTION,
        "open.1": NO_PARENT_COLLECTION,
    }

    accepted = await env.adoption().run(
        "p",
        principal=PRINCIPAL,
        accept=["gone.lost", "gone.failed"],
        reason="work superseded; nothing to deliver",
    )

    assert (accepted["outcome"], accepted["count"]) == ("adopted", 2)
    by_task = {item["task_id"]: item for item in accepted["outcomes"]}
    assert by_task["gone.lost"]["proof"] == OPERATOR_ACCEPTED
    assert by_task["gone.lost"]["unproven_cause"] == NOT_ON_DEFAULT_BRANCH
    assert by_task["gone.failed"]["unproven_cause"] == CHILD_NOT_COMPLETED
    assert by_task["open.1"]["cause"] == PARENT_NOT_TERMINAL
    recorded = await env.recorded()
    assert recorded["gone.lost"]["delivered_sha"] is None
    assert recorded["gone.lost"]["reason"] == "work superseded; nothing to deliver"
    # An open parent's completion still needs its train receipts.
    assert await env.missing_receipts() == {"open.1": NO_PARENT_COLLECTION}


@pytest.mark.parametrize(
    ("accept", "reason", "error"),
    [
        (["open.1"], "operator decision", "parent is still open"),
        (["done.dev"], "operator decision", "does not report"),
        (["no-such-task"], "operator decision", "does not report"),
        (["gone.lost"], None, "requires an audit reason"),
    ],
)
async def test_accept_refuses_what_is_not_a_legacy_blocker(env, accept, reason, error):
    await delivered_legacy_graph(env)
    await unprovable_children(env)
    await env.observe()

    result = await env.adoption().run("p", principal=PRINCIPAL, accept=accept, reason=reason)

    assert result["outcome"] == "invalid"
    assert error in result["error"]
    assert await env.recorded() == {}


async def test_a_child_reopened_before_the_write_is_not_adopted(env):
    await delivered_legacy_graph(env)
    await env.observe()
    adoption = env.adoption()
    prove = adoption._prove

    async def reopen_then_prove(*args):
        item = await prove(*args)
        await env.db.update_task("done.tip", status=TaskStatus.READY)
        return item

    adoption._prove = reopen_then_prove
    result = await adoption.run("p", principal=PRINCIPAL)

    by_task = {item["task_id"]: item for item in result["outcomes"]}
    assert by_task["done.tip"]["outcome"] == UNPROVEN
    assert by_task["done.tip"]["cause"] == STATE_CHANGED
    # Only the child that stayed terminal is recorded.
    assert set(await env.recorded()) == {"done.collected"}


async def test_an_undesignated_or_unknown_project_is_refused(env):
    async with env.db.immediate() as conn:
        await conn.execute(
            update(projects).where(projects.c.id == "p").values(integration_repository_id=None)
        )
    result = await env.adoption().run("p", principal=PRINCIPAL)
    assert result["outcome"] == "invalid"
    assert (await env.adoption().run("missing", principal=PRINCIPAL))["outcome"] == "not_found"


async def test_status_still_flags_children_of_open_parents_and_uncollected_children(env):
    """Only a terminal parent's delivered children are settled without a collection."""
    await unprovable_children(env)
    await env.observe()
    async with env.db._engine.connect() as conn:
        count = await conn.scalar(select(func.count()).select_from(integration_legacy_deliveries))
    assert count == 0
    assert await env.missing_receipts() == {
        "gone.failed": NO_PARENT_COLLECTION,
        "gone.lost": NO_PARENT_COLLECTION,
        "open.1": NO_PARENT_COLLECTION,
    }


async def redelivered_children(env: Env) -> dict[str, str]:
    """A finished parent whose children's work reached main under other commits."""
    await env.task("again", TaskStatus.DEFINED)
    # Cherry-picked whole onto main: same content, different commit.
    git(env.clone, "checkout", "-q", "-b", "aq/again.whole", "main")
    whole = env.commit("again-whole", on="aq/again.whole")
    await env.task("again.whole", TaskStatus.COMPLETED, parent="again", branch="aq/again.whole")
    # Two commits, only the first re-delivered: the second is still undelivered.
    git(env.clone, "checkout", "-q", "-b", "aq/again.part", "main")
    first = env.commit("again-part-1", on="aq/again.part")
    part = env.commit("again-part-2", on="aq/again.part")
    await env.task("again.part", TaskStatus.COMPLETED, parent="again", branch="aq/again.part")
    env.commit("unrelated")
    env.cherry_pick(whole)
    redelivered_first = env.cherry_pick(first)
    env.commit("later")
    await env.db.update_task("again", status=TaskStatus.COMPLETED)
    return {"whole": whole, "part": part, "redelivered_first": redelivered_first}


async def test_work_re_delivered_under_other_commits_is_proven_by_content(env):
    shas = await redelivered_children(env)
    await env.observe()

    result = await env.adoption().run("p", principal=PRINCIPAL)

    by_task = {item["task_id"]: item for item in result["outcomes"]}
    assert by_task["again.whole"] == {
        "task_id": "again.whole",
        "parent_task_id": "again",
        "outcome": ADOPTED,
        "proof": CONTENT_EQUIVALENT,
        "delivered_sha": shas["whole"],
        "development_delivery_id": None,
    }
    part = by_task["again.part"]
    assert (part["outcome"], part["cause"]) == (UNPROVEN, NOT_ON_DEFAULT_BRANCH)
    # A human sees exactly what merging the child's work would still change.
    assert part["undelivered"] == {
        "sha": shas["part"],
        "conflict": False,
        "summary": "1 file changed, 1 insertion(+)",
        "file_count": 1,
        "files": ["again-part-2.txt"],
    }
    assert set(await env.recorded()) == {"again.whole"}
    assert await env.missing_receipts() == {"again.part": NO_PARENT_COLLECTION}


async def test_supersede_records_the_re_delivering_commit_on_the_default_branch(env):
    shas = await redelivered_children(env)
    await env.observe()

    result = await env.adoption().run(
        "p",
        principal=PRINCIPAL,
        supersede={"again.part": shas["redelivered_first"][:12]},
        reason="second commit reverted on purpose; re-delivered by a cherry-pick",
    )

    by_task = {item["task_id"]: item for item in result["outcomes"]}
    assert (result["outcome"], result["count"]) == ("adopted", 2)
    part = by_task["again.part"]
    assert (part["proof"], part["delivered_sha"], part["unproven_cause"]) == (
        SUPERSEDED,
        shas["redelivered_first"],
        NOT_ON_DEFAULT_BRANCH,
    )
    recorded = await env.recorded()
    assert recorded["again.part"]["proof"] == SUPERSEDED
    assert recorded["again.part"]["delivered_sha"] == shas["redelivered_first"]
    assert recorded["again.part"]["reason"].startswith("second commit reverted")
    assert await env.missing_receipts() == {}


async def test_a_superseded_child_and_its_parent_are_then_bound_to_the_repository(env):
    """``bind-legacy-repositories`` accepts what adoption recorded as delivery proof."""
    shas = await redelivered_children(env)
    # The development publisher accepted these tasks without a repository.
    async with env.db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.project_id == "p").values(repo_id=None)
        )
    await env.observe()
    await env.adoption().run(
        "p",
        principal=PRINCIPAL,
        supersede={"again.part": shas["redelivered_first"]},
        reason="re-delivered by a cherry-pick",
    )

    async def unbound() -> set[str]:
        status = await IntegrationControlService(env.db).status("p")
        return {
            blocker["ref"]
            for blocker in status["blockers"]
            if blocker["code"] == "repository_not_designated"
        }

    assert await unbound() == {"again", "again.part", "again.whole"}

    result = await LegacyRepositoryBinding(env.db).run(
        "p", principal=PRINCIPAL, dry_run=False, reason="bind attested legacy hierarchy"
    )

    assert result["bound"] == [
        {"task_id": "again", "proof": "delivered_children"},
        {"task_id": "again.part", "proof": "legacy_delivery", "legacy_proof": SUPERSEDED},
        {"task_id": "again.whole", "proof": "legacy_delivery", "legacy_proof": CONTENT_EQUIVALENT},
    ]
    assert result["unproven"] == []
    assert await unbound() == set()


@pytest.mark.parametrize("by", ["part", "deadbeefdead", "not-a-sha"])
async def test_supersede_refuses_a_commit_that_is_not_on_the_default_branch(env, by):
    shas = await redelivered_children(env)
    await env.observe()

    result = await env.adoption().run(
        "p",
        principal=PRINCIPAL,
        supersede={"again.part": shas.get(by, by)},
        reason="operator decision",
    )

    assert result["outcome"] == "invalid"
    assert "is not a commit on refs/heads/main" in result["error"]
    assert await env.recorded() == {}


async def test_retire_records_abandoned_work_and_deletes_nothing(env):
    await env.task("lost", TaskStatus.DEFINED)
    env.branch("aq/lost.1", landed=False)
    env.delete_branch("aq/lost.1")
    await env.task("lost.1", TaskStatus.COMPLETED, parent="lost", branch="aq/lost.1")
    await env.task("lost.2", TaskStatus.FAILED, parent="lost")
    await env.db.update_task("lost", status=TaskStatus.COMPLETED)
    await env.observe()

    listed = await env.adoption().run("p", principal=PRINCIPAL, dry_run=True)
    lost = next(item for item in listed["outcomes"] if item["task_id"] == "lost.1")
    assert (lost["cause"], lost["undelivered"]) == (NOT_ON_DEFAULT_BRANCH, None)
    assert "branch aq/lost.1 absent" in lost["detail"]

    result = await env.adoption().run(
        "p", principal=PRINCIPAL, retire=["lost.1", "lost.2"], reason="abandoned; superseded plan"
    )

    assert (result["outcome"], result["count"]) == ("adopted", 2)
    recorded = await env.recorded()
    assert {task_id: row["proof"] for task_id, row in recorded.items()} == {
        "lost.1": ABANDONED,
        "lost.2": ABANDONED,
    }
    assert recorded["lost.1"]["delivered_sha"] is None
    # Nothing is deleted or reopened: the tasks keep their terminal states.
    assert (await env.db.get_task("lost.1")).status == TaskStatus.COMPLETED
    assert (await env.db.get_task("lost.2")).status == TaskStatus.FAILED
    assert await env.missing_receipts() == {}


async def test_a_deleted_branch_is_proven_by_its_completion_commit(env):
    """Git proves a retained completion on main: status is clean, nothing to adopt.

    An unlabelled legacy close of the same shape is flagged instead, and
    adoption proves its recorded commit in git rather than trusting it.
    """
    await env.task("pruned", TaskStatus.DEFINED)
    tip = env.branch("aq/pruned.1", landed=True)
    env.delete_branch("aq/pruned.1")
    legacy = env.branch("aq/pruned.2", landed=True)
    env.delete_branch("aq/pruned.2")
    env.commit("after-prune")
    await env.task("pruned.1", TaskStatus.COMPLETED, parent="pruned", branch="aq/pruned.1")
    await env.completion("pruned.1", tip, retain=True)
    await env.task("pruned.2", TaskStatus.COMPLETED, parent="pruned", branch="aq/pruned.2")
    await env.completion("pruned.2", legacy)
    await env.db.update_task("pruned", status=TaskStatus.COMPLETED)
    await env.observe()

    assert await env.missing_receipts() == {"pruned.2": NO_PARENT_COLLECTION}
    result = await env.adoption().run("p", principal=PRINCIPAL)
    assert [(i["task_id"], i["proof"], i["delivered_sha"]) for i in result["outcomes"]] == [
        ("pruned.2", CONTENT_EQUIVALENT, legacy)
    ]
    assert await env.missing_receipts() == {}


async def test_a_task_takes_one_decision(env):
    await unprovable_children(env)
    await env.observe()

    result = await env.adoption().run(
        "p",
        principal=PRINCIPAL,
        accept=["gone.lost"],
        retire=["gone.lost"],
        reason="operator decision",
    )

    assert result["outcome"] == "invalid"
    assert "gone.lost (--accept and --retire)" in result["error"]
    assert await env.recorded() == {}


async def test_a_terminal_parent_whose_collection_was_cancelled_is_settled_without_it(
    env, monkeypatch
):
    """Switching to development cancels a parent's collection; it never resumes.

    Its delivered children are settled like those of a never-collected parent
    instead of reporting ``receipt_missing`` against the dead collection.
    """
    await env.task("drained", TaskStatus.DEFINED)
    drained = env.branch("aq/drained.1", landed=True)
    await env.task("drained.1", TaskStatus.COMPLETED, parent="drained", branch="aq/drained.1")
    await env.completion("drained.1", drained, retain=True)
    recorded, _tip = env.amended("aq/drained.2")
    await env.task("drained.2", TaskStatus.COMPLETED, parent="drained", branch="aq/drained.2")
    await env.completion("drained.2", recorded)
    await env.db.update_task("drained", status=TaskStatus.COMPLETED)
    await env.collection("drained", state="cancelled")
    await env.observe()

    async def dead_collection(*_args, **_kwargs):
        raise AssertionError("a terminal parent's cancelled collection was evaluated")

    monkeypatch.setattr(ParentCompletion, "readiness_on", dead_collection)

    projection = await IntegrationStatusService(env.db).task_blockers("drained")

    assert projection["parent_readiness"] is None
    assert await env.missing_receipts() == {"drained.2": NO_PARENT_COLLECTION}

    result = await env.adoption().run("p", principal=PRINCIPAL)
    assert [(i["task_id"], i["proof"]) for i in result["outcomes"]] == [
        ("drained.2", BRANCH_TIP)
    ]
    assert await env.missing_receipts() == {}


async def test_an_open_parent_keeps_its_cancelled_collection(env, monkeypatch):
    """A live parent will be collected again: it still needs bound train receipts."""
    calls = []

    async def readiness_on(self, conn, *, parent, project, checkpoint, operation):
        calls.append((parent["id"], operation["state"]))
        return {"blockers": [{"task_id": "live.1", "reason": "receipt_missing"}]}

    monkeypatch.setattr(ParentCompletion, "readiness_on", readiness_on)
    await env.task("live", TaskStatus.IN_PROGRESS)
    env.branch("aq/live.1", landed=True)
    await env.task("live.1", TaskStatus.COMPLETED, parent="live", branch="aq/live.1")
    await env.collection("live", state="cancelled")
    await env.observe()

    projection = await IntegrationStatusService(env.db).task_blockers("live")

    assert calls == [("live", "cancelled")]
    assert [
        (b["ref"], b["cause"]) for b in projection["blockers"] if b["code"] == "missing_receipt"
    ] == [("live.1", "receipt_missing")]


def test_handler_resolves_the_service_from_the_orchestrator():
    from src.integration.legacy_deliveries import legacy_delivery_adoption_for

    assert legacy_delivery_adoption_for(SimpleNamespace(db=None)) is None
    assert (
        legacy_delivery_adoption_for(
            SimpleNamespace(db=object(), orchestrator=SimpleNamespace(git=None))
        )
        is None
    )


@pytest.mark.migration
@pytest.mark.integration
async def test_upgrade_creates_the_table_on_a_database_built_before_it():
    """Only a database built before the table exercises ``a00000000022``'s create."""
    from sqlalchemy import inspect, text

    from src.database.engine import create_postgres_engine, run_schema_setup
    from src.database.schema_key import alembic_head_revisions
    from src.database.tables import metadata
    from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn

    if not ensure_worker_postgres_dsn():
        pytest.skip("POSTGRES_TEST_DSN not set")

    dsn = await create_scratch_database("legacy_deliveries_upgrade")
    engine = create_postgres_engine(dsn)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            await conn.execute(text("DROP TABLE integration_legacy_deliveries"))
            await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            await conn.execute(text("INSERT INTO alembic_version VALUES ('a00000000021')"))

        await run_schema_setup(engine)

        def _checks(sync_conn):
            return {
                c["name"]
                for c in inspect(sync_conn).get_check_constraints("integration_legacy_deliveries")
            }

        async with engine.begin() as conn:
            assert await conn.run_sync(_checks) == {
                "ck_integration_legacy_deliveries_proof",
                "ck_integration_legacy_deliveries_delivered_sha",
            }
            version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        assert version in alembic_head_revisions()
        # Idempotent: a second pass over the upgraded database is a no-op.
        await run_schema_setup(engine)
    finally:
        await engine.dispose()


@pytest.mark.migration
@pytest.mark.integration
async def test_upgrade_widens_the_proofs_of_a_table_built_before_them():
    """``a00000000023`` admits re-delivered, superseded and abandoned proofs."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from src.database.engine import create_postgres_engine, run_schema_setup
    from src.database.schema_key import alembic_head_revisions
    from src.database.tables import metadata
    from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn

    if not ensure_worker_postgres_dsn():
        pytest.skip("POSTGRES_TEST_DSN not set")

    dsn = await create_scratch_database("legacy_delivery_proofs_upgrade")
    engine = create_postgres_engine(dsn)
    row = (
        "INSERT INTO integration_legacy_deliveries (task_id, project_id, parent_task_id, "
        "repository_id, target_ref, target_sha, delivered_sha, proof, operator_id, reason, "
        "created_at) VALUES (:task_id, 'p', 'parent', 'r', 'refs/heads/main', :target, "
        ":delivered, :proof, 'operator', 'reason', 1.0)"
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            await conn.execute(text(
                "ALTER TABLE integration_legacy_deliveries "
                "DROP CONSTRAINT ck_integration_legacy_deliveries_proof, "
                "DROP CONSTRAINT ck_integration_legacy_deliveries_delivered_sha, "
                "ADD CONSTRAINT ck_integration_legacy_deliveries_proof CHECK "
                "(proof IN ('development_delivery', 'branch_tip', 'operator_accepted')), "
                "ADD CONSTRAINT ck_integration_legacy_deliveries_delivered_sha CHECK "
                "(proof = 'operator_accepted' OR delivered_sha IS NOT NULL)"
            ))
            await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            await conn.execute(text("INSERT INTO alembic_version VALUES ('a00000000022')"))
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(row),
                    {"task_id": "c0", "target": "a" * 40, "delivered": None, "proof": "abandoned"},
                )

        await run_schema_setup(engine)
        # Idempotent: a second pass over the upgraded database is a no-op.
        await run_schema_setup(engine)

        async with engine.begin() as conn:
            for task_id, proof, delivered in (
                ("c1", "content_equivalent", "b" * 40),
                ("c2", "superseded", "c" * 40),
                ("c3", "abandoned", None),
            ):
                await conn.execute(
                    text(row),
                    {"task_id": task_id, "target": "a" * 40, "delivered": delivered,
                     "proof": proof},
                )
            version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        assert version in alembic_head_revisions()
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(row),
                    {"task_id": "c4", "target": "a" * 40, "delivered": None,
                     "proof": "superseded"},
                )
    finally:
        await engine.dispose()
