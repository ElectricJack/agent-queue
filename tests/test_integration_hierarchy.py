"""Flag-enabled isolated child origins and hierarchy mutation guards."""

from __future__ import annotations

import asyncio
import subprocess
import time

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    integration_outbox,
    integration_promotion_intents,
    integration_review_evidence,
    playbook_artifacts,
    task_branch_origins,
    task_delivery_receipts,
    task_integration_checkpoints,
    task_session_attempts,
    tasks,
)
from src.git.manager import GitManager
from src.integration.hierarchy import (
    HierarchyIntegration,
    materialize_exact_branch,
    verify_workspace_checkpoint,
)
from src.integration.models import (
    ArtifactSnapshot,
    BranchKey,
    HierarchicalIntegrationPolicy,
    IntegrationBoundaryPolicy,
    PlaybookRoute,
    RepairPolicy,
    RequiredCheckSet,
)
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus, Workspace
from tests.db_fixtures import lease_dsn

BASE = "a" * 40
NEXT = "b" * 40


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
    ).stdout.strip()


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("hierarchy.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="hierarchy"))
    await database.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.LINK,
            source_path=str(tmp_path),
        )
    )
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
        required_checks=RequiredCheckSet(
            version="test", names=("unit",), producer_id="forge-observer"
        ),
        repair=RepairPolicy(debug_intelligence_class="high"),
        route=PlaybookRoute(
            playbook_id="hierarchical-delivery",
            scope="project",
            scope_identifier="p",
            artifact=artifact,
        ),
    )
    async with database.immediate() as conn:
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
    await database.update_project(
        "p",
        hierarchical_integration_mode="hierarchy",
        integration_repository_id="repo",
        hierarchical_integration_policy=HierarchicalIntegrationPolicy(
            parent=boundary,
            root=boundary,
            branchless_parent="verifier",
            on_failed_child="block",
        ).model_dump(mode="json"),
    )
    yield database
    await database.close()


@pytest.fixture
def hierarchy(db):
    return HierarchyIntegration(
        db,
        default_head_resolver=lambda _repo, _branch: BASE,
        checkpoint_verifier=lambda _task, _repo, head_sha: head_sha,
    )


async def _create(db, task_id: str, *, parent_id: str | None = None) -> None:
    await db.create_task(
        Task(
            id=task_id,
            project_id="p",
            repo_id="repo",
            parent_task_id=parent_id,
            title=task_id,
            description=task_id,
            status=TaskStatus.IN_PROGRESS,
        )
    )


async def _origins(db) -> list[dict]:
    async with db._engine.connect() as conn:
        rows = (
            await conn.execute(
                select(task_branch_origins, tasks.c.branch_name.label("branch"))
                .join(tasks, tasks.c.id == task_branch_origins.c.task_id)
                .order_by(task_branch_origins.c.created_at)
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def test_project_mode_and_designated_repository_round_trip_and_validate(tmp_path):
    database = Database(lease_dsn("project.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="one"))
    await database.create_project(Project(id="other", name="two"))
    await database.create_repo(
        RepoConfig(id="foreign", project_id="other", source_type=RepoSourceType.LINK)
    )

    with pytest.raises(ValueError, match="same project"):
        await database.update_project(
            "p",
            hierarchical_integration_mode="train",
            integration_repository_id="foreign",
        )

    project = await database.get_project("p")
    assert project.hierarchical_integration_mode == "disabled"
    assert project.integration_repository_id is None
    await database.close()


async def test_branchless_three_level_tree_reserves_distinct_top_down_origins(db, hierarchy):
    await _create(db, "root")

    first = await hierarchy.file_children("root", [{"title": "child"}], 0)
    second = await hierarchy.file_children(
        first["children"][0]["task_id"], [{"title": "grandchild"}], 0
    )

    assert first["generation"] == 1
    assert second["generation"] == 1
    origins = await _origins(db)
    by_task = {row["task_id"]: row for row in origins}
    assert set(by_task) == {"root", "root.1", "root.1.1"}
    assert {row["branch"] for row in origins} == {"aq/root", "aq/root.1", "aq/root.1.1"}
    assert by_task["root"]["parent_ref"] == "main"
    assert by_task["root.1"]["parent_ref"] == "aq/root"
    assert by_task["root.1.1"]["parent_ref"] == "aq/root.1"
    assert all(
        row["parent_ref"] != "main" for row in origins if row["parent_task_id"] is not None
    )

    async with db._engine.connect() as conn:
        events = (
            await conn.execute(
                select(integration_outbox.c.event_type, integration_outbox.c.payload).order_by(
                    integration_outbox.c.created_at, integration_outbox.c.id
                )
            )
        ).all()
    assert [event_type for event_type, _ in events] == [
        "integration.branch_materialization_pending",
        "integration.branch_materialization_pending",
        "integration.branch_materialization_pending",
    ]


async def test_batch_filing_advances_generation_once_and_concurrent_stale_writer_loses(
    db, hierarchy
):
    await _create(db, "parent")

    result = await hierarchy.file_children(
        "parent", [{"title": "A"}, {"title": "B"}], 0
    )
    assert result["generation"] == 1
    assert {origin["creation_generation"] for origin in result["origins"]} == {1}

    outcomes = await asyncio.gather(
        hierarchy.file_children("parent", [{"title": "C"}], 1),
        hierarchy.file_children("parent", [{"title": "D"}], 1),
        return_exceptions=True,
    )
    assert sum(not isinstance(value, Exception) for value in outcomes) == 1
    error = next(value for value in outcomes if isinstance(value, Exception))
    assert isinstance(error, HierarchyError)
    assert error.code == "stale_parent"


async def test_later_child_does_not_rewrite_earlier_child_base(db, hierarchy):
    await _create(db, "parent")

    first = await hierarchy.file_children("parent", [{"title": "A"}], 0)
    await hierarchy.checkpoint_parent("parent", NEXT, 1)
    second = await hierarchy.file_children("parent", [{"title": "B"}], 1)

    assert first["generation"] == 1
    assert second["generation"] == 2
    assert first["origins"][0]["base_sha"] == BASE
    assert second["origins"][0]["base_sha"] == NEXT
    origins = {row["task_id"]: row for row in await _origins(db)}
    assert origins[first["children"][0]["task_id"]]["base_sha"] == BASE


async def test_pending_origin_is_not_claim_frontier_eligible(db, hierarchy):
    await _create(db, "parent")
    result = await hierarchy.file_children("parent", [{"title": "child"}], 0)
    child_id = result["children"][0]["task_id"]
    await db.transition_task(child_id, TaskStatus.READY)

    assert await db.count_ready_by_profile("p") == {}

    async with db.immediate() as conn:
        await conn.execute(
            update(task_branch_origins)
            .where(task_branch_origins.c.task_id == child_id)
            .values(materialized=True, materialized_at=1.0)
        )
    assert await db.count_ready_by_profile("p") == {None: 1}


async def test_checkpoint_rejects_stale_generation(db, hierarchy):
    await _create(db, "parent")
    await hierarchy.file_children("parent", [{"title": "child"}], 0)

    with pytest.raises(HierarchyError) as exc:
        await hierarchy.checkpoint_parent("parent", NEXT, 0)
    assert exc.value.code == "stale"


async def test_checkpoint_without_verifier_fails_closed(db):
    await _create(db, "parent")
    hierarchy = HierarchyIntegration(
        db, default_head_resolver=lambda _repo, _branch: BASE
    )
    await hierarchy.file_children("parent", [{"title": "child"}], 0)

    with pytest.raises(HierarchyError) as exc:
        await hierarchy.checkpoint_parent("parent", NEXT, 1)

    assert exc.value.code == "dirty"
    assert "verifier" in exc.value.detail


async def test_ordinary_create_routes_all_child_writes_through_atomic_origin_writer(
    db, hierarchy, internal_plugins_handler
):
    await _create(db, "parent")
    handler = await internal_plugins_handler(db=db)
    handler.orchestrator.hierarchy_integration = hierarchy

    result = await handler.execute(
        "create_task",
        {
            "project_id": "p",
            "parent_id": "parent",
            "title": "child",
            "description": "child",
            "requires_kinds": ["project-repo"],
            "labels": ["integration-child"],
        },
    )

    assert result["created"] == "parent.1"
    child = await db.get_task("parent.1")
    assert child.repo_id == "repo"
    assert child.branch_name == "aq/parent.1"
    assert [row.kind_id for row in await db.fetch_task_workspace_requirements("parent.1")] == [
        "project-repo"
    ]
    assert await db.get_task_labels("parent.1") == ["integration-child"]


async def test_proposal_commit_uses_one_atomic_hierarchy_transaction(
    db, hierarchy, internal_plugins_handler
):
    handler = await internal_plugins_handler(db=db)
    handler.orchestrator.hierarchy_integration = hierarchy
    proposal = await handler.execute(
        "task_batch_propose",
        {
            "project_id": "p",
            "source": "spec:test",
            "tasks": [
                {"tempId": "parent", "title": "parent", "description": ""},
                {"tempId": "a", "title": "A", "description": ""},
                {"tempId": "b", "title": "B", "description": ""},
            ],
            "edges": [
                {"from": "a", "to": "parent", "dep_type": "parent-child"},
                {"from": "b", "to": "parent", "dep_type": "parent-child"},
            ],
        },
    )

    committed = await handler.execute(
        "task_batch_commit", {"proposal_id": proposal["proposal_id"]}
    )

    assert committed["success"] is True
    created = [await db.get_task(task_id) for task_id in committed["task_ids"]]
    parent = next(task for task in created if task.title == "parent")
    children = [task for task in created if task.title in {"A", "B"}]
    assert {task.parent_task_id for task in children} == {parent.id}
    checkpoint = await db.get_integration_checkpoint(parent.id)
    assert checkpoint["generation"] == 1
    assert len(await _origins(db)) == 3


async def test_reparent_unmaterialized_child_invalidates_both_parents(db, hierarchy):
    await _create(db, "old")
    await _create(db, "new")
    filed = await hierarchy.file_children("old", [{"title": "child"}], 0)
    child_id = filed["children"][0]["task_id"]

    result = await hierarchy.mutate_hierarchy(
        child_id,
        "reparent",
        {"parent_id": "new", "expected_old_generation": 1, "expected_new_generation": 0},
    )

    assert result["outcome"] == "updated"
    assert result["old_parent_generation"] == 2
    assert result["new_parent_generation"] == 1
    async with db._engine.connect() as conn:
        checkpoints = {
            row["task_id"]: dict(row)
            for row in (
                await conn.execute(select(task_integration_checkpoints))
            ).mappings().all()
        }
        child = (
            await conn.execute(select(tasks.c.parent_task_id).where(tasks.c.id == child_id))
        ).one()
    assert child.parent_task_id == "new"
    assert checkpoints["old"]["verified_sha"] is None
    assert checkpoints["new"]["verified_sha"] is None


async def test_canonical_set_parent_cannot_bypass_origin_guard(db, hierarchy):
    await _create(db, "old")
    await _create(db, "new")
    filed = await hierarchy.file_children("old", [{"title": "child"}], 0)
    child_id = filed["children"][0]["task_id"]

    with pytest.raises(HierarchyError) as exc:
        async with db.immediate() as conn:
            await db.set_parent(child_id, "new", conn=conn)
    assert exc.value.code == "delivery_target_fixed"


async def test_delete_unmaterialized_child_retires_origin_and_invalidates_parent(db, hierarchy):
    await _create(db, "parent")
    filed = await hierarchy.file_children("parent", [{"title": "child"}], 0)
    child_id = filed["children"][0]["task_id"]

    await db.delete_task(child_id)

    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["generation"] == 2
    async with db._engine.connect() as conn:
        origin = (
            await conn.execute(
                select(task_branch_origins).where(task_branch_origins.c.task_id == child_id)
            )
        ).mappings().one()
    assert origin["retired_at"] is not None


async def _materialized_child(db, hierarchy, *, title: str = "child") -> str:
    """A terminal child whose branch reached the remote."""
    filed = await hierarchy.file_children("parent", [{"title": title}], 0)
    child_id = filed["children"][0]["task_id"]
    async with db.immediate() as conn:
        await conn.execute(
            update(task_branch_origins)
            .where(task_branch_origins.c.task_id == child_id)
            .values(materialized=True, materialized_at=1.0)
        )
        await conn.execute(
            update(tasks).where(tasks.c.id == child_id).values(status=TaskStatus.FAILED.value)
        )
    return child_id


async def _origin_row(db, task_id: str) -> dict:
    async with db._engine.connect() as conn:
        return dict(
            (
                await conn.execute(
                    select(task_branch_origins).where(
                        task_branch_origins.c.task_id == task_id
                    )
                )
            ).mappings().one()
        )


async def test_deleting_a_materialized_child_requires_a_branch_choice(db, hierarchy):
    """A caller that says nothing is told what to say — not silently obeyed."""
    await _create(db, "parent")
    child_id = await _materialized_child(db, hierarchy)

    with pytest.raises(HierarchyError) as deleted:
        await db.delete_task(child_id)
    assert deleted.value.code == "branch_discard_required"
    assert deleted.value.context["branches"] == [
        {"task_id": child_id, "branch": f"aq/{child_id}", "base_sha": BASE}
    ]
    # Nothing moved: the refusal is a question, not a partial delete.
    assert await db.get_task(child_id) is not None
    assert (await _origin_row(db, child_id))["retired_at"] is None


async def test_deleting_with_keep_retires_the_origin_and_spares_the_branch(db, hierarchy):
    await _create(db, "parent")
    child_id = await _materialized_child(db, hierarchy)

    await db.delete_task(child_id, branch_policy="keep")

    assert await db.get_task(child_id) is None
    origin = await _origin_row(db, child_id)
    assert origin["retired_at"] is not None
    assert origin["discard_state"] is None


async def test_deleting_with_discard_queues_the_branch_for_removal(db, hierarchy):
    await _create(db, "parent")
    child_id = await _materialized_child(db, hierarchy)

    await db.delete_task(child_id, branch_policy="discard")

    assert await db.get_task(child_id) is None
    origin = await _origin_row(db, child_id)
    assert origin["retired_at"] is not None
    assert origin["discard_state"] == "pending"
    assert origin["discard_requested_at"] is not None
    assert origin["discard_attempts"] == 0


async def test_discard_survives_the_task_it_describes(db, hierarchy):
    """The drain must still find its work after the subtree is gone."""
    await _create(db, "parent")
    child_id = await _materialized_child(db, hierarchy)
    await db.delete_task(child_id, branch_policy="discard")

    async with db._engine.connect() as conn:
        pending = (
            await conn.execute(
                select(task_branch_origins.c.task_id).where(
                    task_branch_origins.c.discard_state == "pending"
                )
            )
        ).scalars().all()
    assert pending == [child_id]


async def test_archiving_a_materialized_child_never_touches_the_branch(db, hierarchy):
    """Archive is 'move out of my view', so it needs no choice and asks for none."""
    await _create(db, "parent")
    child_id = await _materialized_child(db, hierarchy)

    assert await db.archive_task(child_id) is True

    origin = await _origin_row(db, child_id)
    assert origin["retired_at"] is not None
    assert origin["discard_state"] is None


async def test_deleting_a_materialized_child_advances_the_parent_generation(db, hierarchy):
    await _create(db, "parent")
    child_id = await _materialized_child(db, hierarchy)
    before = (await db.get_integration_checkpoint("parent"))["generation"]

    await db.delete_task(child_id, branch_policy="keep")

    assert (await db.get_integration_checkpoint("parent"))["generation"] == before + 1


async def test_delivered_identity_refuses_under_every_branch_policy(db, hierarchy):
    """The relaxation is scoped to materialization; delivery is still fixed."""
    await _create(db, "parent")
    child_id = await _materialized_child(db, hierarchy)
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="receipt-1",
                domain_key="receipt:discard-policy",
                source_task_id=child_id,
                target_task_id="parent",
                repository_id="repo",
                target_branch="aq/parent",
                disposition="code",
                created_at=1.0,
            )
        )

    for policy in (None, "keep", "discard"):
        with pytest.raises(HierarchyError) as refused:
            await db.delete_task(child_id, branch_policy=policy)
        assert refused.value.code == "delivery_target_fixed"


async def test_unknown_branch_policy_is_rejected(db, hierarchy):
    await _create(db, "parent")
    child_id = await _materialized_child(db, hierarchy)
    with pytest.raises(ValueError, match="unknown branch_policy"):
        await db.delete_task(child_id, branch_policy="nuke")


async def test_delivered_task_cannot_be_reopened(db, hierarchy):
    await _create(db, "parent")
    filed = await hierarchy.file_children("parent", [{"title": "child"}], 0)
    child_id = filed["children"][0]["task_id"]
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == child_id).values(status=TaskStatus.COMPLETED.value)
        )
        await conn.execute(
            task_delivery_receipts.insert().values(
                id="receipt",
                domain_key="receipt:child",
                source_task_id=child_id,
                target_task_id="parent",
                repository_id="repo",
                target_branch="aq/parent",
                disposition="code",
                created_at=1.0,
            )
        )

    with pytest.raises(HierarchyError) as reopened:
        await db.transition_task(child_id, TaskStatus.READY)
    assert reopened.value.code == "delivery_target_fixed"


async def test_undelivered_reopen_invalidates_parent_generation(db, hierarchy):
    await _create(db, "parent")
    filed = await hierarchy.file_children("parent", [{"title": "child"}], 0)
    child_id = filed["children"][0]["task_id"]
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == child_id)
            .values(status=TaskStatus.FAILED.value)
        )

    await db.transition_task(child_id, TaskStatus.READY)

    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["generation"] == 2
    assert checkpoint["verified_sha"] is None


async def test_materialization_creates_only_absent_or_exact_remote_ref(tmp_path):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    _git(["init", "--bare", str(remote)], tmp_path)
    _git(["init", str(work)], tmp_path)
    _git(["config", "user.email", "test@example.com"], work)
    _git(["config", "user.name", "Test"], work)
    (work / "value.txt").write_text("base\n")
    _git(["add", "value.txt"], work)
    _git(["commit", "-m", "base"], work)
    base = _git(["rev-parse", "HEAD"], work)
    _git(["remote", "add", "origin", str(remote)], work)
    git = GitManager()
    # Clone before a subsequent remote commit, reproducing a stale daemon store.
    _git(["push", "origin", f"{base}:refs/heads/main"], work)
    retained = tmp_path / "retained.git"
    _git(["clone", "--bare", str(remote), str(retained)], tmp_path)
    (work / "value.txt").write_text("new pinned base\n")
    _git(["commit", "-am", "new base"], work)
    base = _git(["rev-parse", "HEAD"], work)
    _git(["push", "origin", f"{base}:refs/heads/main"], work)
    assert await materialize_exact_branch(git, str(retained), "aq/fetched", base) == base
    assert _git(["rev-parse", "refs/heads/aq/fetched"], remote) == base

    assert await materialize_exact_branch(git, str(work), "aq/child", base) == base
    assert _git(["rev-parse", "refs/heads/aq/child"], remote) == base
    assert await materialize_exact_branch(git, str(work), "aq/child", base) == base

    (work / "value.txt").write_text("other\n")
    _git(["commit", "-am", "other"], work)
    other = _git(["rev-parse", "HEAD"], work)
    _git(["push", "--force", "origin", f"{other}:refs/heads/aq/child"], work)
    with pytest.raises(HierarchyError) as conflict:
        await materialize_exact_branch(git, str(work), "aq/child", base)
    assert conflict.value.code == "delivery_target_fixed"
    assert _git(["rev-parse", "refs/heads/aq/child"], remote) == other


async def test_materialized_origin_is_published_to_ready_only_after_exact_confirmation(
    db, hierarchy
):
    await _create(db, "parent")
    filed = await hierarchy.file_children("parent", [{"title": "child"}], 0)
    origin = filed["origins"][0]
    child_id = filed["children"][0]["task_id"]
    assert (await db.get_task(child_id)).status is TaskStatus.DEFINED
    service = HierarchyIntegration(
        db,
        default_head_resolver=lambda _repo, _branch: BASE,
        branch_materializer=lambda _repo, _branch, base_sha: base_sha,
    )

    await service.materialize_origin(origin["id"])

    assert (await db.get_task(child_id)).status is TaskStatus.READY
    persisted = await db.get_task_branch_origin_for_promotion(child_id, "repo")
    assert persisted["materialized"] is True


async def test_checkpoint_verifies_actual_clean_and_pushed_workspace_head(db, tmp_path):
    remote = tmp_path / "checkpoint.git"
    work = tmp_path / "checkpoint-work"
    _git(["init", "--bare", str(remote)], tmp_path)
    _git(["init", str(work)], tmp_path)
    _git(["config", "user.email", "test@example.com"], work)
    _git(["config", "user.name", "Test"], work)
    _git(["checkout", "-b", "aq/parent"], work)
    (work / "value.txt").write_text("base\n")
    _git(["add", "value.txt"], work)
    _git(["commit", "-m", "base"], work)
    _git(["remote", "add", "origin", str(remote)], work)
    _git(["push", "-u", "origin", "aq/parent"], work)
    await _create(db, "parent")
    await db.update_task("parent", branch_name="aq/parent")
    await db.create_workspace(
        Workspace(
            id="checkpoint-slot",
            project_id="p",
            workspace_path=str(work),
            source_type=RepoSourceType.LINK,
            locked_by_task_id="parent",
        )
    )
    task = await db.get_task("parent")
    repo = await db.get_repo("repo")
    git = GitManager()
    base = _git(["rev-parse", "HEAD"], work)

    (work / "dirty.txt").write_text("dirty\n")
    with pytest.raises(HierarchyError, match="uncommitted"):
        await verify_workspace_checkpoint(db, git, task.__dict__, repo, base)
    _git(["add", "dirty.txt"], work)
    _git(["commit", "-m", "next"], work)
    next_head = _git(["rev-parse", "HEAD"], work)
    with pytest.raises(HierarchyError, match="exactly pushed"):
        await verify_workspace_checkpoint(db, git, task.__dict__, repo, next_head)

    _git(["push", "origin", "aq/parent"], work)
    assert await verify_workspace_checkpoint(
        db, git, task.__dict__, repo, next_head
    ) == next_head


@pytest.mark.parametrize("mode", ["hierarchy", "train"])
@pytest.mark.parametrize("operation", ["create_task", "task_batch_commit"])
async def test_new_root_bootstraps_default_branch_before_its_branch_exists(
    db, internal_plugins_handler, mode, operation
):
    from src.git.manager import GitError

    await db.update_project("p", hierarchical_integration_mode=mode)
    requested = []

    def resolve(repo, branch):
        requested.append(branch)
        if branch != repo.default_branch:
            raise GitError(f"repository branch {branch!r} does not exist")
        return BASE

    handler = await internal_plugins_handler(db=db)
    handler.orchestrator.hierarchy_integration = HierarchyIntegration(
        db, default_head_resolver=resolve
    )
    if operation == "create_task":
        result = await handler.execute(operation, {
            "project_id": "p", "title": "New root", "description": "No branch exists yet",
        })
        assert "created" in result, result
        task_id = result["created"]
    else:
        proposal = await handler.execute("task_batch_propose", {
            "project_id": "p", "source": "regression:new-root",
            "tasks": [{"tempId": "root", "title": "New root", "description": "No branch"}],
            "edges": [],
        })
        result = await handler.execute(operation, {"proposal_id": proposal["proposal_id"]})
        assert result["success"], result
        task_id = result["task_ids"][0]
    repo = await db.get_repo("repo")
    assert requested == [repo.default_branch]
    task = await db.get_task(task_id)
    assert task.branch_name == f"aq/{task_id}"
    checkpoint = await db.get_integration_checkpoint(task_id)
    assert checkpoint["checkpoint_sha"] == BASE
    origins = await _origins(db)
    assert len(origins) == 1
    assert origins[0]["base_sha"] == BASE
    assert origins[0]["parent_ref"] == repo.default_branch
    assert not origins[0]["materialized"]


async def test_existing_root_adoption_keeps_its_bound_branch(db):
    await _create(db, "existing")
    await db.update_task("existing", branch_name="existing-work")
    requested = []

    def resolve(_repo, branch):
        requested.append(branch)
        assert branch == "existing-work"
        return NEXT

    service = HierarchyIntegration(db, default_head_resolver=resolve)
    result = await service.file_children("existing", [{"title": "Child"}], 0)
    assert requested == ["existing-work"]
    assert result["origins"][0]["base_sha"] == NEXT


@pytest.mark.parametrize("existing_parent", [False, True])
async def test_graph_creation_uses_atomic_filing_and_rewrites_all_graph_ids(
    db, internal_plugins_handler, existing_parent
):
    from src.database.tables import task_context, task_criteria
    from src.task_graph import parse_graph
    from src.task_graph.creator import FormulaProvenance, create_graph

    def resolve(repo, branch):
        assert branch == repo.default_branch
        return BASE

    handler = await internal_plugins_handler(db=db)
    handler.orchestrator.hierarchy_integration = HierarchyIntegration(
        db, default_head_resolver=resolve
    )
    parent_id = None
    if existing_parent:
        await _create(db, "existing")
        parent_id = "existing"
    document = {
        "version": 1,
        "parent": {"title": "Graph epic", "labels": ["epic"]},
        "nodes": [
            {"key": "first", "title": "First", "description": "Do the work",
             "task_type": "bugfix", "priority": 42,
             "deliverables": [{"id": "source", "kind": "file", "target": "src/cli/tasks.py"}],
             "acceptance": ["Evidence is retained"], "labels": ["regression"],
             "context": [{"type": "file", "path": "src/cli/tasks.py"}]},
            {"key": "second", "title": "Second", "needs": ["first"]},
        ],
    }
    graph = parse_graph(document)
    provenance = FormulaProvenance(
        name="audit", scope="system", path="formulas/audit.md", vars={},
        chain_sha="test", snapshot=document,
    )
    dry = await create_graph(handler, graph, project_id="p", parent_id=parent_id, dry_run=True)
    assert not await _origins(db)
    report = await create_graph(
        handler, graph, project_id="p", parent_id=parent_id, provenance=provenance
    )
    parent_id = report["parent_id"]
    first, second = report["task_ids"]
    assert first == f"{parent_id}.1"
    assert second == f"{parent_id}.2"
    if existing_parent:
        assert dry["parent_id"] == parent_id == "existing"
    first_task = await db.get_task(first)
    assert first_task.parent_task_id == parent_id
    assert first_task.priority == 42
    assert first_task.task_type.value == "bugfix"
    assert first_task.deliverables[0]["target"] == "src/cli/tasks.py"
    assert "Evidence is retained" in first_task.description
    assert "regression" in await db.get_task_labels(first)
    assert "formula:audit" in await db.get_task_labels(parent_id)
    if not existing_parent:
        assert "epic" in await db.get_task_labels(parent_id)
    assert (await db.get_task(second)).is_blocked
    assert report["nodes"][1]["needs"][0]["task_id"] == first
    assert {row["task_id"] for row in await _origins(db)} == {parent_id, first, second}
    assert (await db.get_integration_checkpoint(parent_id))["generation"] == 1
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(task_criteria.c.task_id)) == first
        contexts = (await conn.execute(select(task_context))).mappings().all()
    assert {(row["task_id"], row["type"]) for row in contexts} == {
        (first, "file"), (parent_id, "formula_snapshot"),
    }


async def test_hierarchical_graph_failure_rolls_back_roots_children_and_origins(
    db, hierarchy, internal_plugins_handler, monkeypatch
):
    from src.task_graph import parse_graph
    from src.task_graph.creator import create_graph

    handler = await internal_plugins_handler(db=db)
    handler.orchestrator.hierarchy_integration = hierarchy
    original = hierarchy.file_prepared_children_on

    async def fail_after_children(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("failed after sibling origins")

    monkeypatch.setattr(hierarchy, "file_prepared_children_on", fail_after_children)
    graph = parse_graph({"parent": {"title": "Epic"}, "nodes": [
        {"key": "a", "title": "A"}, {"key": "b", "title": "B", "needs": ["a"]},
    ]})
    with pytest.raises(RuntimeError, match="failed after sibling origins"):
        await create_graph(handler, graph, project_id="p")
    assert not await _origins(db)
    async with db._engine.connect() as conn:
        assert not (await conn.execute(select(tasks.c.id))).all()
        assert not (await conn.execute(select(task_integration_checkpoints))).all()


async def test_concurrent_graph_commands_allocate_distinct_sibling_ids(
    db, hierarchy, internal_plugins_handler
):
    await _create(db, "parent")
    handler = await internal_plugins_handler(db=db)
    handler.orchestrator.hierarchy_integration = hierarchy
    args = {"project_id": "p", "parent_id": "parent", "graph": {
        "nodes": [{"key": "a", "title": "A"}, {"key": "b", "title": "B"}],
    }}
    results = await asyncio.gather(
        handler._cmd_create_task_graph(args), handler._cmd_create_task_graph(args)
    )
    assert all(result.get("created") for result in results), results
    assert {task_id for result in results for task_id in result["task_ids"]} == {
        "parent.1", "parent.2", "parent.3", "parent.4",
    }
    assert (await db.get_integration_checkpoint("parent"))["generation"] == 2
    assert len(await _origins(db)) == 5


async def test_new_root_missing_base_rolls_back_task_and_origin(db):
    from src.git.manager import GitError

    def missing_base(_repo, _branch):
        raise GitError("default branch is missing")

    service = HierarchyIntegration(db, default_head_resolver=missing_base)
    with pytest.raises(GitError, match="default branch is missing"):
        async with db.immediate() as conn:
            await service.file_root_on(conn, Task(
                id="", project_id="p", title="Root", description="No base",
            ))
    assert not await _origins(db)
    async with db._engine.connect() as conn:
        assert not (await conn.execute(select(tasks.c.id))).all()



@pytest.mark.parametrize("condition", ["untouched", "released", "changed_head", "manual_pause"])
async def test_never_run_container_starts_collection_only_at_untouched_origin(db, hierarchy, condition):
    await _create(db, "epic")
    await hierarchy.file_children("epic", [{"title": "child"}], 0)
    async with db.immediate() as conn:
        await conn.execute(update(task_branch_origins).where(
            task_branch_origins.c.task_id == "epic"
        ).values(materialized=True, materialized_at=2.0))
        if condition == "released":
            await conn.execute(update(tasks).where(tasks.c.id == "epic").values(claim_epoch=1))
        elif condition == "manual_pause":
            await conn.execute(update(tasks).where(tasks.c.id == "epic").values(status="PAUSED"))
    if condition == "changed_head":
        hierarchy.default_head_resolver = lambda _repo, _branch: "b" * 40
    result = await hierarchy.bootstrap_container_collection("epic")
    checkpoint = await db.get_integration_checkpoint("epic")
    if condition in {"changed_head", "manual_pause"}:
        assert result["outcome"] == "waiting"
        assert checkpoint["episode_id"] is None
        return
    assert result["outcome"] == "checkpointed"
    assert checkpoint["episode_id"]
    assert (await db.get_task("epic")).status is TaskStatus.PAUSED
    owner = await hierarchy.ownership.get_owner(BranchKey(repository_id="repo", branch="aq/epic"))
    assert owner["owner_role"] == "collector"
    assert owner["owner_id"] == result["operation_id"]
    assert (await hierarchy.bootstrap_container_collection("epic"))["outcome"] == "waiting"
    assert (await db.get_integration_checkpoint("epic"))["episode_id"] == checkpoint["episode_id"]


@pytest.mark.parametrize("review_state", [
    "approved", "missing", "rejected", "stale", "conflict", "delivered", "committed",
])
async def test_collector_queues_only_current_approved_child_once(db, hierarchy, review_state):
    from src.integration.collection import CollectionService

    await _create(db, "epic")
    await hierarchy.file_children("epic", [{"title": "child"}], 0)
    async with db.immediate() as conn:
        await conn.execute(update(task_branch_origins).values(materialized=True, materialized_at=2.0))
        await conn.execute(update(tasks).where(tasks.c.id == "epic.1").values(status="COMPLETED"))
        await conn.execute(update(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "epic.1"
        ).values(checkpoint_sha=NEXT))
        if review_state != "missing":
            await conn.execute(insert(integration_review_evidence).values(
                id="approval", source_task_id="epic.1", repository_id="repo",
                source_base=BASE, reviewed_head_sha=BASE if review_state == "stale" else NEXT,
                reviewed_tree_sha=NEXT, reviewer_task_id="review", review_kind="leaf",
                generation=0, verdict="approved", evidence={}, created_at=1.0,
            ))
        if review_state == "rejected":
            await conn.execute(insert(integration_review_evidence).values(
                id="rejection", source_task_id="epic.1", repository_id="repo",
                source_base=BASE, reviewed_head_sha=NEXT, reviewed_tree_sha=NEXT,
                reviewer_task_id="review", review_kind="leaf", generation=0,
                verdict="rejected", evidence={}, created_at=2.0,
            ))
    await hierarchy.bootstrap_container_collection("epic")
    async with db.immediate() as conn:
        if review_state in {"conflict", "committed"}:
            await conn.execute(insert(integration_promotion_intents).values(
                id="unresolved", domain_key="unresolved", receipt_id="unresolved",
                source_task_id="epic.1", source_head=NEXT, source_base=BASE, repository_id="repo",
                target_branch="aq/epic", expected_target=BASE,
                fence_owner_id="collector", fence_token=1, state=review_state,
                committed_at=2.0 if review_state == "committed" else None,
                remote_evidence={"head": NEXT} if review_state == "committed" else None,
                created_at=1.0, updated_at=1.0,
            ))
        if review_state == "delivered":
            await conn.execute(insert(task_delivery_receipts).values(
                id="delivered", domain_key="delivered", source_task_id="epic.1",
                target_task_id="epic", repository_id="repo", target_branch="aq/epic",
                reviewed_head_sha=NEXT, disposition="code", created_at=1.0,
            ))
    collector = CollectionService(db, hierarchy_service_factory=lambda: hierarchy)
    await collector.tick(3.0)
    await collector.tick(4.0)
    async with db._engine.connect() as conn:
        events = (await conn.execute(select(integration_outbox).where(
            integration_outbox.c.event_type == "delivery.ready"
        ))).mappings().all()
    assert len(events) == (1 if review_state == "approved" else 0)
    if events:
        payload = events[0]["payload"]
        assert payload["source_task_id"] == "epic.1"
        assert payload["source_head"] == NEXT
        assert payload["source_base"] == payload["expected_target"] == BASE
        assert payload["fence"]["target"]["branch"] == "aq/epic"
        assert payload["operation_id"] == payload["fence"]["owner_id"]


@pytest.mark.parametrize('blocker', ['none', 'escalated', 'live_task', 'attached', 'pending', 'wrong_stage'])
async def test_collector_recovers_only_completed_detached_delivered_repair(db, hierarchy, blocker):
    from src.database.tables import (
        integration_branch_owners, integration_repair_operations, integration_repair_stages,
    )
    from src.integration.collection import CollectionService
    from src.integration.models import Fence

    await _create(db, 'epic')
    await hierarchy.file_children('epic', [{'title': 'child'}], 0)
    async with db.immediate() as conn:
        await conn.execute(update(task_branch_origins).values(materialized=True, materialized_at=2.0))
    result = await hierarchy.bootstrap_container_collection('epic')
    operation_id = result['operation_id']
    if blocker == 'escalated':
        async with db.immediate() as conn:
            await conn.execute(update(integration_repair_operations).where(
                integration_repair_operations.c.id == operation_id
            ).values(state='escalated'))
    await db.create_task(Task(
        id='repair', project_id='p', title='Repair', description='', repo_id='repo', branch_name='aq/epic',
        status=TaskStatus.IN_PROGRESS if blocker == 'live_task' else TaskStatus.COMPLETED,
        created_by_kind='integration_repair', created_by_id=operation_id,
    ))
    fence = await hierarchy.ownership.transfer(Fence.model_validate(result['fence']),
                                               'repair', 'repair')
    async with db.immediate() as conn:
        await conn.execute(insert(integration_repair_stages).values(
            operation_id=operation_id, ordinal=0, policy={}, starting_sha=BASE,
            repair_task_id='other' if blocker == 'wrong_stage' else 'repair',
            writer_kind='repair_delegate', state='active',
        ))
        if blocker == 'attached':
            await conn.execute(update(integration_branch_owners).where(
                integration_branch_owners.c.owner_id == 'repair'
            ).values(handoff_state='attached'))
        if blocker == 'pending':
            await conn.execute(insert(integration_promotion_intents).values(
                id='pending', domain_key='pending', receipt_id='pending',
                source_head=NEXT, source_base=BASE, repository_id='repo',
                target_branch='aq/epic', expected_target=BASE, fence_owner_id='repair',
                fence_token=fence.token, state='conflict', created_at=1.0, updated_at=1.0,
            ))
    collector = CollectionService(db, hierarchy_service_factory=lambda: hierarchy)
    await collector.tick(3.0)
    await collector.tick(4.0)
    owner = await hierarchy.ownership.get_owner(fence.target)
    assert owner['owner_id'] == (operation_id if blocker in {'none', 'escalated'} else 'repair')
    assert owner['fence_token'] == fence.token + (1 if blocker in {'none', 'escalated'} else 0)


@pytest.mark.parametrize("current", [False, True])
@pytest.mark.parametrize("attempt_project", ["p", None, "previous-project"])
async def test_container_collection_ignores_old_attempt_but_refuses_current_one(
    db, hierarchy, current, attempt_project
):
    await _create(db, "epic")
    await hierarchy.file_children("epic", [{"title": "child"}], 0)
    task = await db.get_task("epic")
    async with db.immediate() as conn:
        await conn.execute(update(task_branch_origins).where(
            task_branch_origins.c.task_id == "epic"
        ).values(materialized=True, materialized_at=2.0))
        await conn.execute(insert(task_session_attempts).values(
            id="old" if not current else "current",
            session_id="old-session",
            task_id="epic",
            project_id=attempt_project,
            profile_id="worker",
            name="old session",
            lifecycle="task",
            harness="test",
            provider="test",
            state="stopped",
            work_dir="/tmp/old",
            started_at=task.created_at if current else task.created_at - 1,
            session_started_at=task.created_at if current else task.created_at - 1,
            ended_at=task.created_at if current else task.created_at - 1,
        ))
    result = await hierarchy.bootstrap_container_collection("epic")
    assert result["outcome"] == ("waiting" if current else "checkpointed")
    if current:
        assert result["reason"] == "current_incarnation_attempt"


async def test_sibling_prerequisite_needs_current_delivery_receipt_before_claim(db, hierarchy):
    await _create(db, "parent")
    filed = await hierarchy.file_children("parent", [{"title": "first"}, {"title": "second"}], 0)
    first, second = [row["task_id"] for row in filed["children"]]
    await db.add_dependency(second, first, "blocks")
    async with db.immediate() as conn:
        await conn.execute(update(task_branch_origins).where(
            task_branch_origins.c.task_id.in_((first, second))
        ).values(materialized=True, materialized_at=1.0))
        await conn.execute(update(tasks).where(tasks.c.id == first).values(
            status="COMPLETED", updated_at=time.time()
        ))
        await conn.execute(update(tasks).where(tasks.c.id == second).values(
            status="READY", is_blocked=False
        ))
    assert not await db.is_hierarchy_task_runnable(second)
    assert await db.count_ready_by_profile("p") == {}

    source = await db.get_integration_checkpoint(first)
    async with db.immediate() as conn:
        await conn.execute(insert(task_delivery_receipts).values(
            id="receipt",
            domain_key="receipt:parent.1",
            source_task_id=first,
            target_task_id="parent",
            repository_id="repo",
            target_branch="aq/parent",
            reviewed_head_sha=source["checkpoint_sha"],
            before_sha=BASE,
            squash_sha=NEXT,
            after_sha=NEXT,
            disposition="code",
            created_at=time.time() + 1,
        ))
    assert await db.is_hierarchy_task_runnable(second)
    assert await db.count_ready_by_profile("p") == {None: 1}

    # A reopened prerequisite invalidates the former receipt even when its
    # task id and checkpoint happen to be unchanged.
    await db.update_task(first, status=TaskStatus.READY)
    await db.update_task(first, status=TaskStatus.COMPLETED)
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == first).values(
            updated_at=time.time() + 2
        ))
    assert not await db.is_hierarchy_task_runnable(second)


@pytest.mark.parametrize("operator_hold", [False, True])
async def test_container_bootstrap_recovers_after_reservation_before_transfer(
    db, hierarchy, monkeypatch, operator_hold
):
    await _create(db, "epic")
    await hierarchy.file_children("epic", [{"title": "child"}], 0)
    async with db.immediate() as conn:
        await conn.execute(update(task_branch_origins).where(
            task_branch_origins.c.task_id == "epic"
        ).values(materialized=True, materialized_at=2.0))
    transfer = hierarchy.ownership.transfer

    async def interrupted(*args, **kwargs):
        raise RuntimeError("crash before transfer")

    monkeypatch.setattr(hierarchy.ownership, "transfer", interrupted)
    with pytest.raises(RuntimeError, match="crash before transfer"):
        await hierarchy.bootstrap_container_collection("epic")
    reserved = await db.get_integration_checkpoint("epic")
    assert reserved["episode_id"]
    monkeypatch.setattr(hierarchy.ownership, "transfer", transfer)
    if operator_hold:
        await db.pause_task("epic")
    result = await hierarchy.bootstrap_container_collection("epic")
    if operator_hold:
        assert result["outcome"] == "waiting"
        assert result["reason"] == "manual_pause"
        owner = await hierarchy.ownership.get_owner(
            BranchKey(repository_id="repo", branch="aq/epic")
        )
        assert owner["owner_role"] == "worker"
        assert owner["owner_id"] == "epic"
        assert (await db.get_integration_checkpoint("epic"))["episode_id"] == reserved["episode_id"]
        return
    assert result["outcome"] == "checkpointed"
    recovered = await db.get_integration_checkpoint("epic")
    assert recovered["episode_id"] == reserved["episode_id"]
