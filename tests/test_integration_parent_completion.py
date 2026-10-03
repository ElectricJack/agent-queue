"""Receipt-driven parent collection and guarded completion."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.tables import (
    archived_tasks,
    gates,
    task_gates,
    workspaces,
    integration_check_evidence,
    integration_episode_receipt_acceptances,
    integration_branch_owners,
    integration_batch_members,
    integration_batches,
    integration_operation_artifact_pins,
    integration_outbox,
    integration_parent_operation_completions,
    integration_parent_verification_evidence,
    integration_parent_episodes,
    integration_parent_verifications,
    integration_review_evidence,
    integration_repair_operations,
    integration_repair_stages,
    playbook_artifacts,
    task_delivery_receipts,
    task_integration_checkpoints,
    tasks,
)
from src.integration.models import (
    ArtifactSnapshot,
    HierarchicalIntegrationPolicy,
    IntegrationBoundaryPolicy,
    PlaybookRoute,
    RepairPolicy,
    RequiredCheckSet,
)
from src.integration.hierarchy import HierarchyIntegration
from src.integration.parent_completion import (
    AWAITING_TRUSTED_VERIFICATION,
    ParentCompletion,
)
from src.integration.drain_owners import terminal_reservation_clause
from src.integration.status import IntegrationStatusService
from src.database.queries.hierarchy_queries import HierarchyError
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchOwnership
from src.models import (
    AgentProfile,
    Project,
    RepoConfig,
    RepoSourceType,
    Task,
    TaskCompletion,
    TaskStatus,
)
from src.profiles.capabilities import DENY_ALL
from src.database.queries.task_queries import StaleClaim


async def _parent_repair_case(db, tmp_path):
    from src.git.manager import GitManager
    from src.integration.parent_repair_heads import ParentHeadRecovery
    from src.integration.promotion import PromotionService

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
    git("commit", "--allow-empty", "-m", "collected child")
    collected = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "authorized repair")
    head = git("rev-parse", "HEAD")
    git("push", "origin", "aq/parent")
    hierarchy, checkpointed, children = await _parent_tree(db, children=1, base_sha=base)
    await _code_receipt(db, children[0], base, collected)
    await db.create_task(
        Task(
            id="repair",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="Repair",
            description="Authorized repair",
            status=TaskStatus.COMPLETED,
            created_by_kind="integration_repair",
            created_by_id=checkpointed["operation_id"],
        )
    )
    await db.save_task_completion(
        TaskCompletion(
            id="repair-completion",
            task_id="repair",
            outcome="pass",
            branch="aq/parent",
            commits=[head],
            completed_at=20.0,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "parent").values(status="PAUSED"))
        await conn.execute(
            update(task_integration_checkpoints)
            .where(
                task_integration_checkpoints.c.task_id == "parent",
            )
            .values(checkpoint_sha=collected, state="verifying")
        )
        await conn.execute(
            update(integration_branch_owners).values(
                owner_id=checkpointed["operation_id"],
                owner_role="collector",
                handoff_state="reserved",
                fence_token=3,
            )
        )
        from src.integration.outbox import enqueue_integration_event

        await enqueue_integration_event(
            conn,
            event_id="repair-closed",
            dedup_key="repair-closed",
            project_id="p",
            event_type="integration.repair_delegate_closed",
            available_at=20.0,
            payload={
                "operation_id": checkpointed["operation_id"],
                "stage": 0,
                "task_id": "repair",
                "session_id": "former-repair-session",
                "instance_token": "former-instance",
                "workspace_id": "former-workspace",
                "fence_token": 2,
            },
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id=checkpointed["operation_id"],
                ordinal=0,
                policy=_boundary().repair.model_dump(mode="json"),
                intelligence_class="medium",
                starting_sha=collected,
                trigger_id="failed-check",
                writer_kind="repair_delegate",
                repair_task_id="repair",
                current_subject={"kind": "parent", "generation": 1, "head_sha": head},
                started_at=2.0,
                deadline_at=10.0,
                deadline_event_id="old-deadline",
                attempts=1,
                state="awaiting_completion",
                dossier={"repair_commits": [head], "branch_sha": head},
            )
        )
    repo = RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.CLONE, url=str(origin))
    recovery = ParentHeadRecovery(
        PromotionService(
            db,
            data_dir=tmp_path / "retained",
            git_manager=GitManager(),
            repository_resolver=lambda _: repo,
        )
    )
    request = SimpleNamespace(
        operation_id=checkpointed["operation_id"],
        head_sha=head,
        dry_run=True,
        expected_episode_id=checkpointed["episode_id"],
        expected_generation=1,
        expected_stage=0,
        expected_fence_token=3,
        reason="Reconcile authorized repair",
    )
    return recovery, hierarchy, request, git, collected, head


@pytest.mark.parametrize("archived", [False, True])
async def test_post_collection_repair_head_recovery_preserves_receipts_and_requires_fresh_checks(
    db, tmp_path, archived
):
    recovery, hierarchy, request, _git, collected, head = await _parent_repair_case(db, tmp_path)
    async with db.immediate() as conn:
        receipts = (await conn.execute(select(task_delivery_receipts))).mappings().all()
        original_stage = dict(
            (await conn.execute(select(integration_repair_stages))).mappings().one()
        )
        if archived:
            task = dict(
                (await conn.execute(select(tasks).where(tasks.c.id == "repair"))).mappings().one()
            )
            archived_row = {key: value for key, value in task.items() if key in archived_tasks.c}
            await conn.execute(insert(archived_tasks).values(**archived_row, archived_at=30.0))
            await conn.execute(delete(tasks).where(tasks.c.id == "repair"))
        await conn.execute(
            insert(integration_check_evidence).values(
                id="old-check",
                operation_id=request.operation_id,
                parent_task_id="parent",
                parent_generation=1,
                parent_head_sha=collected,
                producer_id="forge-observer",
                workflow_id="workflow",
                run_id="old-run",
                attempt=1,
                required_check_version="parent-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=2.0,
            )
        )
    assert (await hierarchy.verify_parent("parent", 1, collected, ["old-check"]))[
        "outcome"
    ] == "verified"
    preview = await recovery.run(request, principal="operator")
    assert preview["outcome"] == "would_recover"
    assert "--episode" in preview["apply_command"]
    assert (await db.get_integration_checkpoint("parent"))["checkpoint_sha"] == collected
    request.dry_run = False
    assert (await recovery.run(request, principal="operator"))["outcome"] == "recovered"
    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["checkpoint_sha"] == head and checkpoint["current_verification_id"] is None
    assert checkpoint["episode_id"] == request.expected_episode_id and checkpoint["generation"] == 1
    assert (await hierarchy.readiness("parent"))["head_sha"] == head
    assert (await hierarchy.verify_parent("parent", 1, head, ["old-check"]))[
        "outcome"
    ] == "invalid_evidence"
    # The recovered head is the current subject and readiness still answers it
    # ready, so the refusal is not a superseded one: nothing binds trusted
    # evidence to this head yet, which is exactly what the fresh run below has
    # to supply. The old certification is never reused either way.
    before_refusal = (await db.get_task("parent")).status
    unverified = await hierarchy.complete_parent("parent", 1, head)
    assert unverified["outcome"] == AWAITING_TRUSTED_VERIFICATION
    assert unverified["reason"] == "verification_not_recorded"
    assert (await db.get_task("parent")).status is before_refusal
    async with db.immediate() as conn:
        assert (await conn.execute(select(task_delivery_receipts))).mappings().all() == receipts
        stage = dict((await conn.execute(select(integration_repair_stages))).mappings().one())
        assert {k: v for k, v in stage.items() if k != "dossier"} == {
            k: v for k, v in original_stage.items() if k != "dossier"
        }
        await conn.execute(
            insert(integration_check_evidence).values(
                id="fresh-check",
                operation_id=request.operation_id,
                parent_task_id="parent",
                parent_generation=1,
                parent_head_sha=head,
                producer_id="forge-observer",
                workflow_id="workflow",
                run_id="fresh-run",
                attempt=1,
                required_check_version="parent-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=40.0,
            )
        )
    assert (await hierarchy.verify_parent("parent", 1, head, ["fresh-check"]))[
        "outcome"
    ] == "verified"
    verified = await db.get_integration_checkpoint("parent")
    assert (await recovery.run(request, principal="operator"))["outcome"] == "already_recovered"
    assert await db.get_integration_checkpoint("parent") == verified


@pytest.mark.parametrize(
    "case",
    [
        "stale_remote",
        "unrelated",
        "unaudited",
        "wrong_episode",
        "wrong_generation",
        "wrong_fence",
        "human",
        "gate",
        "writer",
        "missing_close_proof",
    ],
)
async def test_parent_head_recovery_rejects_unproven_or_held_heads(db, tmp_path, case):
    recovery, _hierarchy, request, git, collected, head = await _parent_repair_case(db, tmp_path)
    request.dry_run = False
    if case == "stale_remote":
        git("push", "origin", f"{collected}:refs/heads/aq/parent", "--force")
    elif case == "unrelated":
        git("switch", "--orphan", "unrelated")
        git("commit", "--allow-empty", "-m", "unrelated")
        request.head_sha = git("rev-parse", "HEAD")
        git("push", "origin", "HEAD:refs/heads/aq/parent", "--force")
        async with db.immediate() as conn:
            from src.database.tables import task_completion_records

            await conn.execute(
                update(integration_repair_stages).values(
                    current_subject={
                        "kind": "parent",
                        "generation": 1,
                        "head_sha": request.head_sha,
                    },
                    dossier={"branch_sha": request.head_sha, "repair_commits": [request.head_sha]},
                )
            )
            await conn.execute(
                update(task_completion_records).values(commits=json.dumps([request.head_sha]))
            )
    elif case == "unaudited":
        async with db.immediate() as conn:
            await conn.execute(
                update(integration_repair_stages).values(
                    dossier={"branch_sha": head, "repair_commits": []}
                )
            )
    elif case == "wrong_episode":
        request.expected_episode_id = "another-episode"
    elif case == "wrong_generation":
        request.expected_generation = 2
    elif case == "wrong_fence":
        request.expected_fence_token = 2
    elif case == "human":
        async with db.immediate() as conn:
            await conn.execute(update(integration_repair_operations).values(state="human_required"))
    elif case == "gate":
        async with db.immediate() as conn:
            await conn.execute(
                insert(gates).values(
                    id="human-gate",
                    project_id="p",
                    gate_type="human",
                    title="Decision",
                    status="open",
                    created_at=5.0,
                )
            )
            await conn.execute(insert(task_gates).values(task_id="parent", gate_id="human-gate"))
    elif case == "writer":
        async with db.immediate() as conn:
            await conn.execute(update(integration_branch_owners).values(handoff_state="attached"))
    elif case == "missing_close_proof":
        async with db.immediate() as conn:
            await conn.execute(
                delete(integration_outbox).where(integration_outbox.c.id == "repair-closed")
            )
    if case.startswith("wrong_"):
        assert (await recovery.run(request, principal="operator"))["outcome"] == "changed"
    else:
        with pytest.raises(ValueError):
            await recovery.run(request, principal="operator")
    assert (await db.get_integration_checkpoint("parent"))["checkpoint_sha"] == collected


async def test_normal_parent_repair_binding_propagates_the_fenced_head(db, tmp_path):
    from src.integration.repair import RepairService
    from src.models import SessionRecord

    _recovery, hierarchy, request, _git, collected, head = await _parent_repair_case(db, tmp_path)
    await db.create_profile(AgentProfile(id="repairer", name="Repairer"))
    await db.create_session(
        SessionRecord(
            id="repair-session",
            task_id="repair",
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="repair-session",
            lifecycle="task",
            state="running",
            work_dir=str(tmp_path),
            epoch="epoch",
            instance_token="instance",
            started_at=2.0,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "repair").values(status="IN_PROGRESS"))
        await conn.execute(
            insert(workspaces).values(
                id="repair-workspace",
                project_id="p",
                workspace_path=str(tmp_path),
                source_type="link",
                locked_by_task_id="repair",
                enabled=True,
                created_at=2.0,
            )
        )
        await conn.execute(
            update(integration_branch_owners).values(
                owner_id="repair",
                owner_role="repair",
                session_id="repair-session",
                workspace_id="repair-workspace",
                handoff_state="attached",
            )
        )
        await conn.execute(
            update(integration_repair_stages).values(
                current_subject={"kind": "parent", "generation": 1, "head_sha": collected},
                dossier={},
            )
        )
        result = await RepairService(db).bind_current_parent_subject_on(
            conn,
            request.operation_id,
            head_sha=head,
            commit_proof={"base_sha": collected, "head_sha": head, "commits": [head]},
        )
    assert result["changed"] is True
    assert (await hierarchy.readiness("parent"))["head_sha"] == head
    assert (await db.get_integration_checkpoint("parent"))["checkpoint_sha"] == head
    async with db.immediate() as conn:
        replay = await RepairService(db).bind_current_parent_subject_on(
            conn,
            request.operation_id,
            head_sha=head,
            commit_proof={"base_sha": collected, "head_sha": head, "commits": [head]},
        )
        assert replay["changed"] is False
        await conn.execute(update(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "parent",
        ).values(generation=2))
        generation_only = await RepairService(db).bind_current_parent_subject_on(
            conn, request.operation_id, head_sha=head,
            commit_proof={"base_sha": head, "head_sha": head, "commits": []},
        )
        assert generation_only["changed"] is True
    readiness = await hierarchy.readiness("parent")
    assert readiness["outcome"] == "ready" and readiness["head_sha"] == head


@pytest.mark.parametrize("mismatch", ["episode_id", "generation", "operation_id"])
async def test_readiness_rejects_repair_edges_from_another_identity(db, tmp_path, mismatch):
    from src.integration.parent_repair_heads import EXTENSIONS

    recovery, hierarchy, request, _git, _collected, _head = await _parent_repair_case(db, tmp_path)
    request.dry_run = False
    assert (await recovery.run(request, principal="operator"))["outcome"] == "recovered"
    async with db.immediate() as conn:
        dossier = dict(
            (await conn.execute(select(integration_repair_stages.c.dossier))).scalar_one()
        )
        dossier[EXTENSIONS][0][mismatch] = 2 if mismatch == "generation" else "another-identity"
        await conn.execute(update(integration_repair_stages).values(dossier=dossier))
    readiness = await hierarchy.readiness("parent")
    assert readiness["outcome"] == "waiting"
    assert {item["reason"] for item in readiness["blockers"]} == {"repair_head_proof"}


async def test_repair_edge_survives_more_children_in_the_same_episode(db, tmp_path):
    recovery, hierarchy, request, _git, _collected, head = await _parent_repair_case(db, tmp_path)
    request.dry_run = False
    assert (await recovery.run(request, principal="operator"))["outcome"] == "recovered"
    filed = await hierarchy.file_children("parent", [{"title": "follow-up"}], 1)
    new_child = filed["children"][0]["task_id"]
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == new_child).values(status="COMPLETED"))
        await conn.execute(
            update(task_integration_checkpoints)
            .where(
                task_integration_checkpoints.c.task_id == new_child,
            )
            .values(checkpoint_sha="b" * 40)
        )
    await _code_receipt(db, new_child, head, "f" * 40)
    readiness = await hierarchy.readiness("parent")
    assert readiness["generation"] == 2
    assert readiness["outcome"] == "ready" and readiness["head_sha"] == "f" * 40


@pytest.fixture
async def db(tmp_path, reuse_database):
    database = await reuse_database("parent-completion.db")
    await database.create_project(Project(id="p", name="integration project"))
    yield database


async def _enable_project(
    db, *, on_failed_child: str = "block", boundary: IntegrationBoundaryPolicy | None = None
) -> dict:
    artifact = _artifact()
    policy = HierarchicalIntegrationPolicy(
        parent=boundary or _boundary(),
        root=_boundary(),
        branchless_parent="verifier",
        on_failed_child=on_failed_child,
    ).model_dump(mode="json")
    await db.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.LINK)
    )
    await db.update_project(
        "p",
        hierarchical_integration_mode="hierarchy",
        integration_repository_id="repo",
        hierarchical_integration_policy=policy,
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                **artifact.model_dump(),
                scope="project",
                scope_identifier="p",
                profile_fingerprint="",
                path="/tmp/artifact",
                size_bytes=1,
                validation="{}",
                created_at=1.0,
            )
        )
    return policy


async def _seed_parent_identity(db, *, generation: int = 0) -> None:
    await db.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.LINK)
    )
    await db.create_task(
        Task(
            id="parent",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="parent",
            description="parent",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode",
                parent_task_id="parent",
                repository_id="repo",
                generation=generation,
                pre_collection_checkpoint_sha="a" * 40,
                created_at=1.0,
            )
        )


async def _parent_tree(
    db,
    *,
    children: int = 2,
    on_failed_child: str = "block",
    boundary: IntegrationBoundaryPolicy | None = None,
    base_sha: str = "a" * 40,
):
    await _enable_project(db, on_failed_child=on_failed_child, boundary=boundary)
    await db.create_task(
        Task(
            id="parent",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="parent",
            description="parent",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    hierarchy = HierarchyIntegration(
        db,
        default_head_resolver=lambda _repo, _branch: base_sha,
        checkpoint_verifier=lambda _task, _repo, head: head,
    )
    filed = await hierarchy.file_children(
        "parent", [{"title": f"child {index}"} for index in range(children)], 0
    )
    checkpointed = await hierarchy.checkpoint_parent("parent", base_sha, 1)
    child_ids = [row["task_id"] for row in filed["children"]]
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id.in_(child_ids)).values(status="COMPLETED")
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id.in_(child_ids))
            .values(checkpoint_sha="b" * 40)
        )
    return hierarchy, checkpointed, child_ids


async def _code_receipt(
    db, child_id: str, before_sha: str, after_sha: str, **overrides
) -> None:
    async with db.immediate() as conn:
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints).where(
                    task_integration_checkpoints.c.task_id == "parent"
                )
            )
        ).mappings().one()
        operation = (
            await conn.execute(
                select(integration_repair_operations).where(
                    integration_repair_operations.c.parent_task_id == "parent",
                    integration_repair_operations.c.episode_id == checkpoint["episode_id"],
                )
            )
        ).mappings().one()
        values = {
            "id": f"receipt-{child_id}",
            "domain_key": f"delivery-{child_id}",
            "source_task_id": child_id,
            "target_task_id": "parent",
            "repository_id": "repo",
            "target_branch": "aq/parent",
            "reviewed_head_sha": "b" * 40,
            "reviewed_tree_sha": "c" * 40,
            "before_sha": before_sha,
            "squash_sha": after_sha,
            "after_sha": after_sha,
            "review_evidence": {"id": f"review-{child_id}"},
            "parent_operation_id": operation["id"],
            "parent_episode_id": checkpoint["episode_id"],
            "disposition": "code",
            "created_at": float(int(child_id.rsplit(".", 1)[1])),
        }
        values.update(overrides)
        await conn.execute(insert(task_delivery_receipts).values(**values))


def _artifact() -> ArtifactSnapshot:
    return ArtifactSnapshot(
        playbook_id="hierarchical-delivery",
        artifact_sha256="sha256:" + "a" * 64,
        schema_generation=2,
        contract_fingerprint="sha256:" + "b" * 64,
        source_digest="sha256:" + "c" * 64,
        compiler_build="test-build",
        compiled_at="2026-09-05T00:00:00Z",
        version=4,
    )


def _boundary(**overrides) -> IntegrationBoundaryPolicy:
    # The deprecated ``*_profile_id`` fields model a policy stored before
    # mandatory routing: it must still validate and every code path ignore them.
    values = {
        "required_checks": RequiredCheckSet(
            version="parent-v1", names=("unit",), producer_id="forge-observer"
        ),
        "repair": RepairPolicy(debug_intelligence_class="deep-high"),
        "route": PlaybookRoute(
            playbook_id="hierarchical-delivery",
            scope="project",
            scope_identifier="p",
            activation_id="activation-audit-only",
            artifact=_artifact(),
        ),
        "primary_intelligence_class": "medium",
        "primary_profile_id": "integrator",
        "verifier_intelligence_class": "high",
        "verifier_profile_id": "verifier",
    }
    values.update(overrides)
    return IntegrationBoundaryPolicy(**values)


def test_hierarchical_policy_freezes_full_parent_and_root_inputs():
    policy = HierarchicalIntegrationPolicy(
        version=1,
        parent=_boundary(),
        root=_boundary(),
        branchless_parent="verifier",
        on_failed_child="block",
    )

    dumped = policy.model_dump(mode="json")
    assert dumped["parent"]["route"]["artifact"] == _artifact().model_dump(mode="json")
    assert dumped["parent"]["route"]["playbook_id"] == "hierarchical-delivery"
    assert dumped["parent"]["route"]["scope"] == "project"
    assert dumped["parent"]["route"]["scope_identifier"] == "p"
    with pytest.raises(Exception):
        policy.parent.required_checks.names = ("changed",)


@pytest.mark.parametrize("field,value", [("branchless_parent", "guess"), ("on_failed_child", "ignore")])
def test_hierarchical_policy_rejects_unruled_choices(field, value):
    values = {
        "version": 1,
        "parent": _boundary(),
        "root": _boundary(),
        "branchless_parent": "verifier",
        "on_failed_child": "block",
    }
    values[field] = value
    with pytest.raises(Exception):
        HierarchicalIntegrationPolicy(**values)


async def test_project_policy_round_trips_as_nullable_json(db):
    assert (await db.get_project("p")).hierarchical_integration_policy is None
    policy = HierarchicalIntegrationPolicy(
        parent=_boundary(),
        root=_boundary(),
        branchless_parent="verifier",
        on_failed_child="block",
    ).model_dump(mode="json")
    await db.update_project("p", hierarchical_integration_policy=policy)
    assert (await db.get_project("p")).hierarchical_integration_policy == policy


async def test_parent_episode_operation_identity_survives_completion(db):
    await _seed_parent_identity(db)
    values = {
        "target_kind": "parent",
        "parent_task_id": "parent",
        "episode_id": "episode",
        "active_stage": 0,
        "state": "active",
        "policy_snapshot": {},
        "artifact_snapshot": {},
        "required_check_version": "checks-v1",
        "route_playbook_id": "hierarchical-delivery",
        "route_scope": "project",
        "route_scope_identifier": "p",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    async with db.immediate() as conn:
        await conn.execute(insert(integration_repair_operations).values(id="op-1", **values))
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == "op-1")
            .values(state="completed")
        )
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    insert(integration_repair_operations).values(id="op-2", **values)
                )


async def test_parent_identity_foreign_keys_reject_mismatched_episode_links(db):
    await _seed_parent_identity(db)
    async with db.immediate() as conn:
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    insert(integration_repair_operations).values(
                        id="wrong-operation",
                        target_kind="parent",
                        parent_task_id="parent",
                        episode_id="missing-episode",
                        active_stage=0,
                        state="active",
                        policy_snapshot={},
                        artifact_snapshot={},
                        required_check_version="test",
                        created_at=1.0,
                        updated_at=1.0,
                    )
                )
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    insert(task_integration_checkpoints).values(
                        task_id="parent",
                        repository_id="repo",
                        branch="aq/parent",
                        generation=0,
                        episode_id="missing-episode",
                        state="working",
                        version=0,
                        updated_at=1.0,
                    )
                )


async def test_check_evidence_and_verification_links_are_append_only(db):
    await _seed_parent_identity(db, generation=1)
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="parent",
                repository_id="repo",
                branch="aq/parent",
                generation=1,
                state="verifying",
                version=0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="op",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="episode",
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="checks-v1",
                route_playbook_id="hierarchical-delivery",
                route_scope="project",
                route_scope_identifier="p",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_check_evidence).values(
                id="evidence",
                operation_id="op",
                parent_task_id="parent",
                parent_generation=1,
                parent_head_sha="a" * 40,
                producer_id="forge-observer",
                workflow_id="workflow",
                run_id="run",
                attempt=1,
                required_check_version="checks-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="verification",
                operation_id="op",
                parent_task_id="parent",
                episode_id="episode",
                generation=1,
                head_sha="a" * 40,
                required_check_version="checks-v1",
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_parent_verification_evidence).values(
                verification_id="verification", evidence_id="evidence"
            )
        )
        for statement in (
            update(integration_check_evidence)
            .where(integration_check_evidence.c.id == "evidence")
            .values(conclusion="failure"),
            delete(integration_check_evidence).where(
                integration_check_evidence.c.id == "evidence"
            ),
            update(integration_parent_verification_evidence)
            .where(
                integration_parent_verification_evidence.c.verification_id
                == "verification"
            )
            .values(evidence_id="changed"),
        ):
            with pytest.raises(DBAPIError):
                async with conn.begin_nested():
                    await conn.execute(statement)


async def test_parent_operation_artifact_pin_prevents_collection(db):
    await _seed_parent_identity(db)
    artifact = _artifact()
    async with db.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                **artifact.model_dump(),
                scope="project",
                scope_identifier="p",
                profile_fingerprint="",
                path="/tmp/artifact",
                size_bytes=1,
                validation="{}",
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="op",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="episode",
                active_stage=0,
                state="completed",
                policy_snapshot={},
                artifact_snapshot=artifact.model_dump(mode="json"),
                required_check_version="checks-v1",
                route_playbook_id="hierarchical-delivery",
                route_scope="project",
                route_scope_identifier="p",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_operation_artifact_pins).values(
                operation_id="op", artifact_sha256=artifact.artifact_sha256
            )
        )

    assert await db.collect_playbook_artifacts(before=2.0, min_versions=0) == []
    async with db._engine.connect() as conn:
        assert (
            await conn.execute(
                select(playbook_artifacts.c.artifact_sha256).where(
                    playbook_artifacts.c.artifact_sha256 == artifact.artifact_sha256
                )
            )
        ).scalar_one() == artifact.artifact_sha256


async def test_first_parent_checkpoint_reserves_one_frozen_episode_operation(db):
    policy = await _enable_project(db)
    await db.create_task(
        Task(
            id="parent",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            parent_task_id=None,
            title="parent",
            description="parent",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="parent",
                repository_id="repo",
                branch="aq/parent",
                generation=2,
                checkpoint_sha="a" * 40,
                state="working",
                version=0,
                updated_at=1.0,
            )
        )
    hierarchy = HierarchyIntegration(
        db, checkpoint_verifier=lambda _task, _repo, head: head
    )

    with pytest.raises(StaleClaim):
        await hierarchy.checkpoint_and_suspend_parent(
            "parent", "b" * 40, 2, expect_claim_epoch=99,
            accepted_close={"completion_id": "c", "session_id": "s", "claim_epoch": 99},
        )
    assert (await db.get_integration_checkpoint("parent"))["episode_id"] is None
    # A fenced-out suspension accepts no close.
    assert await db.get_task_meta("parent", "accepted_close") is None
    assert await db.get_active_parent_integration_operation("parent") is None

    result = await hierarchy.checkpoint_parent("parent", "b" * 40, 2)
    checkpoint = await db.get_integration_checkpoint("parent")
    operation = await db.get_integration_operation(result["operation_id"])

    assert result["episode_id"] == checkpoint["episode_id"]
    assert operation["episode_id"] == result["episode_id"]
    assert operation["policy_snapshot"] == policy
    assert operation["artifact_snapshot"] == policy["parent"]["route"]["artifact"]
    assert operation["route_playbook_id"] == "hierarchical-delivery"
    assert operation["route_scope"] == "project"
    assert operation["route_scope_identifier"] == "p"
    assert operation["active_stage"] == 0
    async with db._engine.connect() as conn:
        pins = (
            await conn.execute(
                select(integration_operation_artifact_pins).where(
                    integration_operation_artifact_pins.c.operation_id == operation["id"]
                )
            )
        ).mappings().all()
    assert [row["artifact_sha256"] for row in pins] == [
        policy["parent"]["route"]["artifact"]["artifact_sha256"]
    ]


async def test_terminal_children_require_complete_contiguous_receipt_chain(db):
    hierarchy, _checkpointed, children = await _parent_tree(db)

    assert (await hierarchy.readiness("parent"))["outcome"] == "waiting"
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    partial = await hierarchy.readiness("parent")
    assert partial["outcome"] == "waiting"
    assert partial["head_sha"] == "d" * 40
    await _code_receipt(db, children[1], "d" * 40, "e" * 40)

    ready = await hierarchy.readiness("parent")
    assert ready["outcome"] == "ready"
    assert ready["head_sha"] == "e" * 40
    assert [row["source_task_id"] for row in ready["receipts"]] == children


async def test_status_uses_current_receipt_among_multiple_historical_deliveries(db):
    _hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    child_id = children[0]
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="historic-receipt",
                domain_key="historic-delivery",
                source_task_id=child_id,
                target_task_id="parent",
                repository_id="repo",
                target_branch="aq/parent",
                reviewed_head_sha="1" * 40,
                reviewed_tree_sha="2" * 40,
                before_sha="a" * 40,
                squash_sha="3" * 40,
                after_sha="3" * 40,
                review_evidence={"id": "historic-review"},
                parent_operation_id=None,
                parent_episode_id=None,
                disposition="code",
                created_at=0.0,
            )
        )
    await _code_receipt(db, child_id, "a" * 40, "d" * 40)

    projection = await IntegrationStatusService(db).task_blockers("parent")

    assert projection is not None
    assert projection["parent_readiness"]["operation_id"] == checkpointed["operation_id"]
    assert "missing_receipt" not in {
        blocker["code"] for blocker in projection["blockers"]
    }


@pytest.mark.parametrize(
    "invalid_values",
    [
        {"reviewed_head_sha": "9" * 40},
        {"repository_id": "wrong-repository"},
        {"target_branch": "wrong-branch"},
        {"parent_operation_id": None, "parent_episode_id": None},
    ],
)
async def test_status_rejects_receipt_not_applicable_to_current_parent_context(
    db, invalid_values
):
    _hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    child_id = children[0]
    await _code_receipt(db, child_id, "a" * 40, "d" * 40, **invalid_values)

    projection = await IntegrationStatusService(db).task_blockers("parent")

    assert projection is not None
    assert "missing_receipt" in {
        blocker["code"] for blocker in projection["blockers"]
    }


async def test_status_blocks_failed_child_without_current_disposition_receipt(db):
    _hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == children[0]).values(status="FAILED")
        )

    projection = await IntegrationStatusService(db).task_blockers("parent")

    assert projection is not None
    blocker = next(item for item in projection["blockers"] if item["code"] == "missing_receipt")
    assert blocker["cause"] == "failed_child"


@pytest.mark.parametrize(
    "ordinal,attempts,expected_limit",
    [(0, 2, 2), (1, 1, 1)],
)
async def test_status_uses_typed_limit_for_current_repair_stage(
    db, ordinal, attempts, expected_limit
):
    _hierarchy, checkpointed, _children = await _parent_tree(db, children=1)
    policy = _boundary().repair.model_dump(mode="json")
    policy["primary_attempts"] = 2
    policy["debug_attempts"] = 1
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == checkpointed["operation_id"])
            .values(active_stage=ordinal)
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id=checkpointed["operation_id"],
                ordinal=ordinal,
                policy=policy,
                intelligence_class="medium",
                profile_id="integrator",
                starting_sha="a" * 40,
                started_at=1.0,
                deadline_at=100.0,
                attempts=attempts,
                state="active",
            )
        )

    projection = await IntegrationStatusService(db, clock=lambda: 50.0).task_blockers(
        "parent"
    )

    assert projection is not None
    blocker = next(item for item in projection["blockers"] if item["code"] == "budget_exhausted")
    assert blocker["cause"] == "attempts"
    assert blocker["stage"] == ordinal
    assert blocker["limit"] == expected_limit


async def test_parent_current_stage_remains_deadline_bound_while_awaiting_completion(db):
    _hierarchy, checkpointed, _children = await _parent_tree(db, children=1)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id=checkpointed["operation_id"],
                ordinal=0,
                policy=_boundary().repair.model_dump(mode="json"),
                intelligence_class="medium",
                profile_id="integrator",
                starting_sha="a" * 40,
                started_at=1.0,
                deadline_at=100.0,
                attempts=1,
                state="awaiting_completion",
            )
        )

    projection = await IntegrationStatusService(db, clock=lambda: 101.0).task_blockers(
        "parent"
    )

    assert projection is not None
    blocker = next(item for item in projection["blockers"] if item["code"] == "budget_exhausted")
    assert blocker["cause"] == "deadline"
    assert blocker["deadline_at"] == 100.0


@pytest.mark.parametrize("policy", ["block", "ask"])
async def test_failed_child_readiness_exposes_frozen_disposition_policy(db, policy):
    hierarchy, _checkpointed, children = await _parent_tree(
        db, children=1, on_failed_child=policy
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == children[0]).values(status="FAILED")
        )

    readiness = await hierarchy.readiness("parent")

    assert readiness["outcome"] == "failed"
    assert readiness["on_failed_child"] == policy
    assert readiness["blockers"] == [
        {"task_id": children[0], "reason": "failed_child"}
    ]


async def test_arbitrary_resolution_json_cannot_satisfy_code_receipt_chain(db):
    hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    # Receipts are append-only (trg_task_delivery_receipts_update), so the
    # forged row is written as-is rather than patched after the fact.
    await _code_receipt(
        db,
        children[0],
        "a" * 40,
        "d" * 40,
        squash_sha=None,
        resolution_evidence={"kind": "conflict_resolution", "trusted": True},
    )

    readiness = await hierarchy.readiness("parent")

    assert readiness["outcome"] == "waiting"
    assert readiness["head_sha"] == "a" * 40
    assert readiness["blockers"] == [
        {"task_id": children[0], "reason": "receipt_chain"}
    ]


async def test_unbound_historic_receipts_do_not_satisfy_current_parent_episode(db):
    hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    # Receipts are append-only (trg_task_delivery_receipts_update), so the
    # unbound historic row is written as-is rather than unbound afterwards.
    await _code_receipt(
        db,
        children[0],
        "a" * 40,
        "d" * 40,
        parent_operation_id=None,
        parent_episode_id=None,
    )

    readiness = await hierarchy.readiness("parent")
    assert readiness["outcome"] == "waiting"
    assert readiness["blockers"] == [
        {"task_id": children[0], "reason": "receipt_missing"}
    ]


async def test_receipt_bound_to_unrelated_historic_episode_is_rejected(db):
    hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="unrelated-episode",
                parent_task_id="parent",
                repository_id="repo",
                generation=0,
                pre_collection_checkpoint_sha="a" * 40,
                created_at=0.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="unrelated-operation",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="unrelated-episode",
                active_stage=0,
                state="completed",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="test",
                created_at=0.0,
                updated_at=0.0,
            )
        )
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="historic-receipt",
                domain_key="historic-delivery",
                source_task_id=children[0],
                target_task_id="parent",
                repository_id="repo",
                target_branch="aq/parent",
                reviewed_head_sha="b" * 40,
                reviewed_tree_sha="c" * 40,
                before_sha="a" * 40,
                squash_sha="d" * 40,
                after_sha="d" * 40,
                review_evidence={"id": "historic-review"},
                parent_operation_id="unrelated-operation",
                parent_episode_id="unrelated-episode",
                disposition="code",
                created_at=0.0,
            )
        )

    readiness = await hierarchy.readiness("parent")
    assert readiness["outcome"] == "waiting"
    assert readiness["receipts"] == []


async def test_disposition_revision_supersedes_only_changed_child(db):
    hierarchy, _checkpointed, children = await _parent_tree(db)
    await _code_receipt(db, children[1], "a" * 40, "d" * 40)

    first = await hierarchy.record_disposition(
        children[0],
        disposition="noop",
        reviewed_head_sha="b" * 40,
        reviewed_tree_sha="c" * 40,
        verification_evidence={"producer_id": "forge-observer", "evidence_id": "noop-1"},
        resolution_evidence={"authority": "playbook", "decision_id": "decision-1"},
    )
    assert first["revision"] == 0
    assert (await hierarchy.readiness("parent"))["outcome"] == "ready"

    second = await hierarchy.record_disposition(
        children[0],
        disposition="skipped",
        reviewed_head_sha="b" * 40,
        reviewed_tree_sha="c" * 40,
        verification_evidence={"producer_id": "forge-observer", "evidence_id": "noop-2"},
        resolution_evidence={"authority": "operator", "decision_id": "decision-2"},
    )
    assert second["revision"] == 1
    projection = await hierarchy.readiness("parent")
    assert projection["outcome"] == "ready"
    selected = {row["source_task_id"]: row for row in projection["receipts"]}
    assert selected[children[0]]["disposition"] == "skipped"
    assert selected[children[1]]["id"] == f"receipt-{children[1]}"
    status = await IntegrationStatusService(db).task_blockers("parent")
    assert status is not None
    assert "missing_receipt" not in {item["code"] for item in status["blockers"]}


async def test_record_noop_command_binds_review_close_and_exact_child_head(db):
    hierarchy, _checkpointed, children = await _parent_tree(db)
    reviewer_id = children[0]
    await _code_receipt(db, children[1], "a" * 40, "d" * 40)
    await db.create_profile(
        AgentProfile(
            id="reviewer", name="Reviewer", harness="codex", lifecycle="task",
            aq_commands=[], harness_tools=[], plugin_tools=[], needs_workspace=False,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == reviewer_id)
            .values(profile_id="reviewer", route_source="role")
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == reviewer_id)
            .values(checkpoint_sha="a" * 40)
        )
        await conn.execute(
            insert(integration_review_evidence).values(
                id="approved-review",
                source_task_id=children[1],
                repository_id="repo",
                source_base="a" * 40,
                reviewed_head_sha="b" * 40,
                reviewed_tree_sha="c" * 40,
                reviewer_task_id=reviewer_id,
                review_kind="leaf",
                generation=0,
                verdict="approved",
                evidence={"decision_path": "review_task_close"},
                created_at=1.0,
            )
        )
    reviewer = await db.get_task(reviewer_id)
    await db.save_task_completion(
        TaskCompletion(
            id="noop-close-1", task_id=reviewer_id, outcome="pass", work_outcome="no-op",
            branch=reviewer.branch_name, completed_at=2.0,
        )
    )

    @asynccontextmanager
    async def repository_transaction(_path):
        yield

    class Promotion:
        git = SimpleNamespace(arepository_transaction=repository_transaction)

        async def _resolve_repository(self, _repository_id):
            return SimpleNamespace(
                repo=SimpleNamespace(project_id="p"),
                retained_git_dir="/tmp/retained-repo",
                origin_url="https://example.invalid/repo.git",
            )

        async def _ensure_retained_repository(self, _resolved):
            return None

        async def _fetch_all_heads(self, _path, _origin_url):
            return None

        async def _tree_oid(self, _path, _head):
            return "c" * 40

    class Handler(IntegrationCommandsMixin):
        orchestrator = SimpleNamespace(
            hierarchy_integration=hierarchy, promotion_service=Promotion()
        )

    handler = Handler()
    handler.db = db
    args = {"child_task_id": reviewer_id, "expected_head_sha": "a" * 40}
    worker = ExecutionPrincipal(
        kind=PrincipalKind.SESSION, project_id="p", policy=DENY_ALL,
    )
    with principal_context(worker):
        denied = await handler._cmd_integration_record_noop(args)
    assert denied["outcome"] == "unauthorized"
    assert (await hierarchy.readiness("parent"))["outcome"] == "waiting"

    stale = await handler._cmd_integration_record_noop(args | {"expected_head_sha": "b" * 40})
    assert stale["outcome"] == "stale_head"
    recorded = await handler._cmd_integration_record_noop(args)
    assert recorded["outcome"] == "recorded"
    assert recorded["reviewed_tree_sha"] == "c" * 40
    assert (await handler._cmd_integration_record_noop(args))["receipt_id"] == recorded["receipt_id"]
    projection = await hierarchy.readiness("parent")
    assert projection["outcome"] == "ready"
    receipt = next(row for row in projection["receipts"] if row["source_task_id"] == reviewer_id)
    assert receipt["verification_evidence"]["review_evidence_id"] == "approved-review"
    assert receipt["resolution_evidence"]["completion_id"] == "noop-close-1"

    await db.save_task_completion(
        TaskCompletion(
            id="noop-close-2", task_id=reviewer_id, outcome="pass", work_outcome="no-op",
            branch=reviewer.branch_name, completed_at=3.0,
        )
    )
    revised = await handler._cmd_integration_record_noop(args)
    assert revised["revision"] == 1
    assert revised["receipt_id"] != recorded["receipt_id"]
    assert (await hierarchy.readiness("parent"))["outcome"] == "ready"


async def test_verified_noop_refuses_a_child_branch_advanced_from_its_reserved_base(db):
    hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    child_id = children[0]
    child = await db.get_task(child_id)
    await db.save_task_completion(
        TaskCompletion(
            id="claimed-noop", task_id=child_id, outcome="pass", work_outcome="no-op",
            branch=child.branch_name, completed_at=2.0,
        )
    )
    with pytest.raises(HierarchyError, match="reserved base"):
        await hierarchy.record_disposition(
            child_id,
            disposition="noop",
            reviewed_head_sha="b" * 40,
            reviewed_tree_sha="c" * 40,
            verification_evidence={"completion_id": "claimed-noop"},
            resolution_evidence={"completion_id": "claimed-noop"},
            verified_completion_id="claimed-noop",
        )
    assert (await hierarchy.readiness("parent"))["outcome"] == "waiting"


async def test_parent_completion_pins_exact_verification_for_rollover(db):
    hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="older-verification",
                operation_id=checkpointed["operation_id"],
                parent_task_id="parent",
                episode_id=checkpointed["episode_id"],
                generation=0,
                head_sha="c" * 40,
                required_check_version="parent-v1",
                created_at=1.5,
            )
        )
        await conn.execute(
            insert(integration_check_evidence).values(
                id="check-unit",
                operation_id=checkpointed["operation_id"],
                parent_task_id="parent",
                parent_generation=1,
                parent_head_sha="d" * 40,
                producer_id="forge-observer",
                workflow_id="workflow",
                run_id="run",
                attempt=1,
                required_check_version="parent-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=2.0,
            )
        )
        await conn.execute(update(tasks).where(tasks.c.id == "parent").values(status="IN_PROGRESS"))

    verified = await hierarchy.verify_parent("parent", 1, "d" * 40, ["check-unit"])
    assert verified["outcome"] == "verified"
    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["current_verification_id"] == verified["verification_id"]
    assert checkpoint["checkpoint_sha"] == "d" * 40
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id=checkpointed["operation_id"],
                ordinal=0,
                policy=_boundary().repair.model_dump(mode="json"),
                intelligence_class="medium",
                profile_id="integrator",
                starting_sha="a" * 40,
                trigger_id="check-unit",
                current_subject={
                    "kind": "parent",
                    "generation": 1,
                    "head_sha": "d" * 40,
                },
                deadline_event_id=f"repair-deadline-{checkpointed['operation_id']}-0",
                success_subject={
                    "kind": "parent",
                    "generation": 1,
                    "head_sha": "d" * 40,
                },
                success_evidence_id="check-unit",
                started_at=1.0,
                deadline_at=100.0,
                attempts=1,
                state="awaiting_completion",
            )
        )
    async with db._engine.connect() as conn:
        verified_events = (
            await conn.execute(
                select(integration_outbox).where(
                    integration_outbox.c.event_type == "task.integration_verified"
                )
            )
        ).mappings().all()
    assert len(verified_events) == 1
    assert verified_events[0]["payload"]["operation_id"] == checkpointed["operation_id"]

    with pytest.raises(Exception, match="integration completion"):
        await db.transition_task("parent", TaskStatus.COMPLETED, force=True)
    assert (await hierarchy.complete_parent("parent", 1, "d" * 40))["outcome"] == "invariant_error"
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(
                integration_branch_owners.c.repository_id == "repo",
                integration_branch_owners.c.ref == "aq/parent",
            )
            .values(owner_id="parent", owner_role="verifier", fence_token=2)
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "parent")
            .values(branch_owner_id="parent")
        )
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == checkpointed["operation_id"])
            .values(state="human_required")
        )
    assert (
        await hierarchy.complete_parent("parent", 1, "d" * 40)
    )["outcome"] == "invariant_error"
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == checkpointed["operation_id"])
            .values(state="escalated")
        )
    completed = await hierarchy.complete_parent("parent", 1, "d" * 40)
    assert completed["outcome"] == "completed"
    assert (await db.get_task("parent")).status is TaskStatus.COMPLETED
    assert (await db.get_integration_operation(checkpointed["operation_id"]))["state"] == "completed"
    async with db._engine.connect() as conn:
        verifier_is_terminal = (await conn.execute(
            select(terminal_reservation_clause())
            .select_from(integration_branch_owners)
            .where(integration_branch_owners.c.ref == "aq/parent")
        )).scalar_one()
    assert verifier_is_terminal is True
    completed_status = await IntegrationStatusService(db).task_blockers("parent")
    assert completed_status is not None
    assert completed_status["parent_readiness"]["operation_id"] == checkpointed["operation_id"]
    assert "missing_receipt" not in {
        item["code"] for item in completed_status["blockers"]
    }
    assert completed_status["repair"] == []
    async with db._engine.connect() as conn:
        completion = (
            await conn.execute(select(integration_parent_operation_completions))
        ).mappings().one()
        repair_stage = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id
                    == checkpointed["operation_id"]
                )
            )
        ).mappings().one()
    assert completion["operation_id"] == checkpointed["operation_id"]
    assert completion["verification_id"] == verified["verification_id"]
    assert completion["parent_task_id"] == "parent"
    assert completion["episode_id"] == checkpointed["episode_id"]
    assert repair_stage["state"] == "passed"
    assert repair_stage["completed_at"] is not None
    completed_checkpoint = await db.get_integration_checkpoint("parent")
    assert completed_checkpoint["last_completed_operation_id"] == checkpointed["operation_id"]
    assert completed_checkpoint["last_completed_verification_id"] == verified["verification_id"]

    await db.transition_task("parent", TaskStatus.READY, assigned_agent_id=None)
    rolled = await db.get_integration_checkpoint("parent")
    assert rolled["episode_id"] is None
    assert rolled["current_verification_id"] is None
    assert rolled["last_completed_operation_id"] == checkpointed["operation_id"]
    assert rolled["last_completed_verification_id"] == verified["verification_id"]
    assert rolled["generation"] == 2

    rollover = HierarchyIntegration(
        db,
        checkpoint_verifier=lambda _task, _repo, head: head,
        ancestry_verifier=lambda _repo, ancestor, descendant: (
            ancestor == "d" * 40 and descendant == "d" * 40
        ),
    )
    next_episode = await rollover.checkpoint_parent("parent", "d" * 40, 2)
    readiness = await rollover.readiness("parent")
    assert next_episode["episode_id"] != checkpointed["episode_id"]
    assert readiness["outcome"] == "ready"
    assert [row["source_task_id"] for row in readiness["receipts"]] == children
    async with db._engine.connect() as conn:
        carried = (
            await conn.execute(select(integration_episode_receipt_acceptances))
        ).mappings().one()
    assert carried["receipt_id"] == f"receipt-{children[0]}"
    assert carried["previous_verification_id"] == verified["verification_id"]
    assert carried["operation_id"] == next_episode["operation_id"]
    status = await IntegrationStatusService(db).task_blockers("parent")
    assert status is not None
    assert "missing_receipt" not in {item["code"] for item in status["blockers"]}


async def _verified_parent_tree(db, *, children: int = 1):
    """A collected parent whose aggregate verifier is the recorded owner."""
    hierarchy, checkpointed, child_ids = await _parent_tree(db, children=children)
    await _code_receipt(db, child_ids[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "parent").values(status="IN_PROGRESS"))
        await conn.execute(
            update(integration_branch_owners)
            .where(
                integration_branch_owners.c.repository_id == "repo",
                integration_branch_owners.c.ref == "aq/parent",
            )
            .values(owner_id="parent", owner_role="verifier", fence_token=2)
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "parent")
            .values(branch_owner_id="parent")
        )
    return hierarchy, checkpointed, child_ids


async def _trusted_check(db, checkpointed, head_sha="d" * 40, generation=1, **overrides):
    values = {
        "id": "check-unit",
        "operation_id": checkpointed["operation_id"],
        "parent_task_id": "parent",
        "parent_generation": generation,
        "parent_head_sha": head_sha,
        "producer_id": "forge-observer",
        "workflow_id": "workflow",
        "run_id": "run",
        "attempt": 1,
        "required_check_version": "parent-v1",
        "checks": {"unit": "success"},
        "conclusion": "success",
        "classification": "conclusive",
        "observed_at": 2.0,
    }
    values.update(overrides)
    async with db.immediate() as conn:
        await conn.execute(insert(integration_check_evidence).values(**values))
    return values["id"]


async def test_completion_without_trusted_binding_names_the_missing_producer(db):
    """No trusted evidence is a wait on the CI producer, not a stale subject.

    The verifier's own aggregate validation already passed; only the trusted
    integration check evidence is absent, and no worker-side re-run can record
    it.  The refusal must say so, and must carry the exact owner action.
    """
    hierarchy, checkpointed, _ = await _verified_parent_tree(db)
    completion = ParentCompletion(db)

    diagnosis = await completion.diagnose_trusted_binding("parent", 1, "d" * 40)
    assert diagnosis is not None
    assert diagnosis["reason"] == "verification_not_recorded"
    assert diagnosis["required_producer_id"] == "forge-observer"
    assert diagnosis["required_check_version"] == "parent-v1"
    assert diagnosis["required_check_names"] == ["unit"]

    refused = await hierarchy.complete_parent("parent", 1, "d" * 40)
    assert refused["outcome"] == AWAITING_TRUSTED_VERIFICATION
    assert refused["reason"] == "verification_not_recorded"
    assert refused["generation"] == 1
    assert refused["head_sha"] == "d" * 40
    assert refused["verification_id"] is None
    assert refused["next_owner"] == "parent_ci_producer"
    assert "aq integration status" in refused["next_action"]
    assert "aq integration flush" in refused["next_action"]
    assert "do not re-run the local suite" in refused["next_action"]
    # A refusal completes nothing and transitions nothing.
    assert (await db.get_task("parent")).status is TaskStatus.IN_PROGRESS
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_parent_operation_completions))).all() == []
        assert (
            await conn.execute(
                select(integration_parent_verifications).where(
                    integration_parent_verifications.c.operation_id
                    == checkpointed["operation_id"]
                )
            )
        ).all() == []


async def test_unchanged_missing_binding_repeats_one_diagnosis(db):
    """Replaying the close on unchanged evidence cannot reach completion."""
    hierarchy, checkpointed, _ = await _verified_parent_tree(db)
    completion = ParentCompletion(db)

    first = await hierarchy.complete_parent("parent", 1, "d" * 40)
    second = await hierarchy.complete_parent("parent", 1, "d" * 40)
    third = await completion.diagnose_trusted_binding("parent", 1, "d" * 40)

    assert first["reason"] == second["reason"] == "verification_not_recorded"
    assert {first["reason"], second["reason"]} == {"verification_not_recorded"}
    assert third["reason"] == first["reason"]
    assert (await db.get_task("parent")).status is TaskStatus.IN_PROGRESS


async def test_trusted_evidence_after_the_wait_completes_the_parent(db):
    """The wait ends the same way: real evidence, then ordinary completion."""
    hierarchy, checkpointed, _ = await _verified_parent_tree(db)
    completion = ParentCompletion(db)
    assert (
        await hierarchy.complete_parent("parent", 1, "d" * 40)
    )["outcome"] == AWAITING_TRUSTED_VERIFICATION

    evidence_id = await _trusted_check(db, checkpointed)
    verified = await hierarchy.verify_parent("parent", 1, "d" * 40, [evidence_id])
    assert verified["outcome"] == "verified"
    assert await completion.diagnose_trusted_binding("parent", 1, "d" * 40) is None

    completed = await hierarchy.complete_parent("parent", 1, "d" * 40)
    assert completed["outcome"] == "completed"
    assert (await db.get_task("parent")).status is TaskStatus.COMPLETED
    assert (await hierarchy.complete_parent("parent", 1, "d" * 40))["outcome"] == (
        "already_completed"
    )


async def test_verification_of_another_head_is_a_missing_binding_not_a_stale_head(db):
    """A verification recorded against another subject is a wait, not staleness."""
    hierarchy, checkpointed, _ = await _verified_parent_tree(db)
    completion = ParentCompletion(db)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="other-head-verification",
                operation_id=checkpointed["operation_id"],
                parent_task_id="parent",
                episode_id=checkpointed["episode_id"],
                generation=1,
                head_sha="c" * 40,
                required_check_version="parent-v1",
                created_at=1.5,
            )
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "parent")
            .values(
                current_verification_id="other-head-verification",
                verified_generation=1,
                verified_sha="c" * 40,
            )
        )

    diagnosis = await completion.diagnose_trusted_binding("parent", 1, "d" * 40)
    assert diagnosis["reason"] == "verified_other_head"
    assert diagnosis["verification_id"] == "other-head-verification"
    refused = await hierarchy.complete_parent("parent", 1, "d" * 40)
    assert refused["outcome"] == AWAITING_TRUSTED_VERIFICATION
    assert refused["reason"] == "verified_other_head"
    assert (await db.get_task("parent")).status is TaskStatus.IN_PROGRESS


async def test_superseded_aggregate_head_still_answers_stale_verification(db):
    """The collected head moving on is the genuinely superseded subject."""
    hierarchy, checkpointed, children = await _verified_parent_tree(db, children=2)
    await _code_receipt(
        db, children[1], "d" * 40, "e" * 40, id="receipt-second", domain_key="delivery-second"
    )
    evidence_id = await _trusted_check(db, checkpointed, head_sha="e" * 40)
    assert (
        await hierarchy.verify_parent("parent", 1, "e" * 40, [evidence_id])
    )["outcome"] == "verified"

    # This close quotes the aggregate head as it stood before the second
    # child's receipt advanced it: a superseded subject, not missing evidence.
    refused = await hierarchy.complete_parent("parent", 1, "d" * 40)
    assert refused["outcome"] == "stale_verification"
    assert "reason" not in refused
    assert (await db.get_task("parent")).status is TaskStatus.IN_PROGRESS


async def test_recorded_green_evidence_names_the_playbook_as_the_owner(db):
    """Green CI evidence with no verification belongs to the verify rule."""
    hierarchy, checkpointed, _ = await _verified_parent_tree(db)
    completion = ParentCompletion(db)
    await _trusted_check(db, checkpointed)

    refused = await hierarchy.complete_parent("parent", 1, "d" * 40)
    assert refused["outcome"] == AWAITING_TRUSTED_VERIFICATION
    assert refused["reason"] == "verification_not_recorded"
    assert refused["recorded_evidence_ids"] == ["check-unit"]
    assert refused["recorded_conclusions"] == ["success"]
    assert refused["next_owner"] == "parent_integration_playbook"
    assert "integration_parent_verify" in refused["next_action"]
    assert (
        await completion.diagnose_trusted_binding("parent", 1, "d" * 40)
    )["next_owner"] == "parent_integration_playbook"


async def test_recorded_failing_evidence_names_the_repair_ladder_as_the_owner(db):
    """Recorded red evidence is a failed aggregate, not a pending CI run."""
    hierarchy, checkpointed, _ = await _verified_parent_tree(db)
    completion = ParentCompletion(db)
    await _trusted_check(db, checkpointed, conclusion="failure", checks={"unit": "failure"})

    refused = await hierarchy.complete_parent("parent", 1, "d" * 40)
    assert refused["outcome"] == AWAITING_TRUSTED_VERIFICATION
    assert refused["recorded_conclusions"] == ["failure"]
    assert refused["next_owner"] == "parent_repair_ladder"
    assert "repair ladder" in refused["next_action"]
    assert (
        await completion.diagnose_trusted_binding("parent", 1, "d" * 40)
    )["next_owner"] == "parent_repair_ladder"


async def test_child_added_after_verification_makes_completion_stale(db):
    hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_check_evidence).values(
                id="check-unit",
                operation_id=checkpointed["operation_id"],
                parent_task_id="parent",
                parent_generation=1,
                parent_head_sha="d" * 40,
                producer_id="forge-observer",
                workflow_id="workflow",
                run_id="run",
                attempt=1,
                required_check_version="parent-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=2.0,
            )
        )
    await hierarchy.verify_parent("parent", 1, "d" * 40, ["check-unit"])
    await hierarchy.file_children("parent", [{"title": "new defect"}], 1)

    result = await hierarchy.complete_parent("parent", 1, "d" * 40)
    assert result["outcome"] in {"waiting", "stale_verification"}


async def test_collector_to_parent_verifier_wake_advances_live_head(db):
    hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await db._apply_transition(
            conn,
            "parent",
            TaskStatus.PAUSED,
            context="integration_parent_suspended",
            _manual_pause_control=True,
        )
    with pytest.raises(HierarchyError, match="guarded verifier wake"):
        await db.transition_task("parent", TaskStatus.READY, force=True)
    target = BranchKey(repository_id="repo", branch="aq/parent")
    owner = await BranchOwnership(db).get_owner(target)
    worker = Fence(target=target, owner_id=owner["owner_id"], token=owner["fence_token"])
    collector = await BranchOwnership(db).transfer(
        worker, checkpointed["operation_id"], "collector"
    )
    verifier = await BranchOwnership(db).transfer(collector, "parent", "verifier")

    result = await hierarchy.wake_verifier("parent", verifier)

    assert result["outcome"] == "woken"
    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["checkpoint_sha"] == "d" * 40
    assert checkpoint["branch_owner_id"] == "parent"
    assert checkpoint["state"] == "verifying"
    assert (await db.get_task("parent")).status is TaskStatus.READY


async def test_parent_prime_summary_uses_receipt_readiness_projection(db):
    from src.prime.sections import build_integration_delivery_summary

    hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)

    summary = await build_integration_delivery_summary(db, await db.get_task("parent"))

    assert "Readiness: **ready**" in summary
    assert "Pre-collection head: `" + "a" * 40 + "`" in summary
    assert "Current aggregate head: `" + "d" * 40 + "`" in summary
    assert f"`{children[0]}`: code squash `{'d' * 40}`" in summary
    assert "Required aggregate checks: `unit`" in summary


async def test_branchless_parent_creates_unrouted_verifier_delegate_before_handoff(db):
    hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await db._apply_transition(
            conn, "parent", TaskStatus.PAUSED, _manual_pause_control=True
        )
        projection = await hierarchy.parent_completion.mark_ready_on(conn, "parent")

    operation = await db.get_integration_operation(checkpointed["operation_id"])
    delegate = await db.get_task(operation["verifier_task_id"])
    assert projection["state"] == "integration_ready"
    assert delegate.parent_task_id is None
    assert delegate.status is TaskStatus.PAUSED
    assert delegate.repo_id == "repo"
    assert delegate.branch_name == "aq/parent"
    # The frozen snapshot still names ``verifier_profile_id``; it is ignored:
    # the verifier is filed with the class hint and the router routes it.
    assert operation["policy_snapshot"]["parent"]["verifier_profile_id"] == "verifier"
    assert delegate.profile_id is None
    assert delegate.intelligence_class is None
    assert delegate.class_hint == "high"
    assert delegate.route_source == "unrouted"

    async with db._engine.connect() as conn:
        ready_event = (
            await conn.execute(
                select(integration_outbox).where(
                    integration_outbox.c.event_type == "task.integration_ready"
                )
            )
        ).mappings().one()
    payload = dict(ready_event["payload"])
    payload.pop("event_id")
    assert payload == {
        "project_id": "p",
        "operation_id": checkpointed["operation_id"],
        "task_id": "parent",
        "title": "parent",
        "episode_id": checkpointed["episode_id"],
        "generation": 1,
        "head_sha": "d" * 40,
        "verifier_task_id": delegate.id,
        "target": {"repository_id": "repo", "branch": "aq/parent"},
        "expected_token": 1,
        "next_owner_id": delegate.id,
        "next_role": "verifier",
    }

    target = BranchKey(repository_id="repo", branch="aq/parent")
    owner = await BranchOwnership(db).get_owner(target)
    worker = Fence(target=target, owner_id=owner["owner_id"], token=owner["fence_token"])
    collector = await BranchOwnership(db).transfer(
        worker, checkpointed["operation_id"], "collector"
    )
    verifier = await BranchOwnership(db).transfer(
        collector, delegate.id, "verifier"
    )
    async with db.immediate() as conn:
        replayed_projection = await hierarchy.parent_completion.mark_ready_on(
            conn, "parent"
        )
    async with db._engine.connect() as conn:
        ready_events = (
            await conn.execute(
                select(integration_outbox).where(
                    integration_outbox.c.event_type == "task.integration_ready"
                )
            )
        ).mappings().all()
    assert replayed_projection["state"] == "integration_ready"
    assert len(ready_events) == 1
    assert ready_events[0]["payload"] == ready_event["payload"]
    assert (await hierarchy.wake_verifier("parent", verifier))["outcome"] == "woken"
    assert (await db.get_task("parent")).status is TaskStatus.PAUSED
    assert (await db.get_task(delegate.id)).status is TaskStatus.READY


@pytest.mark.parametrize(
    ("overrides", "blocked"),
    [
        # A stored pre-routing policy with a profile but no class blocks:
        # the profile is ignored, so nothing names the verifier's class.
        ({"verifier_intelligence_class": None}, True),
        # The class alone is a complete route request now.
        ({"verifier_profile_id": None}, False),
    ],
)
async def test_branchless_parent_verifier_needs_only_the_class_hint(db, overrides, blocked):
    hierarchy, checkpointed, children = await _parent_tree(
        db, children=1, boundary=_boundary(**overrides)
    )
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await db._apply_transition(
            conn, "parent", TaskStatus.PAUSED, _manual_pause_control=True
        )
        projection = await hierarchy.parent_completion.mark_ready_on(conn, "parent")

    operation = await db.get_integration_operation(checkpointed["operation_id"])
    if blocked:
        assert projection["outcome"] == "configuration_blocked"
        assert projection["reason"] == "verifier_routing_missing"
        assert operation["verifier_task_id"] is None
        return
    assert projection["state"] == "integration_ready"
    delegate = await db.get_task(operation["verifier_task_id"])
    assert (delegate.profile_id, delegate.intelligence_class) == (None, None)
    assert delegate.class_hint == "high"
    assert delegate.route_source == "unrouted"


@pytest.mark.parametrize(
    "invalid", [None, "operation", "role", "attached", "branch", "binding", "target"]
)
async def test_woken_verifier_delegate_passes_the_pool_claim_origin_gate(db, invalid):
    """A verifier delegate is claimable only on its exact reserved parent fence.

    It checks the parent's branch and never gets a ``task_branch_origins`` row
    of its own, so the hierarchy origin gate must admit it by reservation, as
    it does a repair delegate.  Before that, a pool never saw it: ``aq task
    claim --next`` answered ``no_ready_work`` while the parent waited forever.
    """
    from src.database.queries.claim_queries import _frontier_where
    from src.database.queries.hierarchy_queries import ProjectIntegrationMode

    await db.create_profile(AgentProfile(id="verifier", name="Verifier", harness="claude"))
    hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await db._apply_transition(
            conn, "parent", TaskStatus.PAUSED, _manual_pause_control=True
        )
        await hierarchy.parent_completion.mark_ready_on(conn, "parent")
    operation = await db.get_integration_operation(checkpointed["operation_id"])
    delegate_id = operation["verifier_task_id"]
    target = BranchKey(repository_id="repo", branch="aq/parent")
    ownership = BranchOwnership(db)
    owner = await ownership.get_owner(target)
    collector = await ownership.transfer(
        Fence(target=target, owner_id=owner["owner_id"], token=owner["fence_token"]),
        checkpointed["operation_id"],
        "collector",
    )
    verifier = await ownership.transfer(collector, delegate_id, "verifier")
    assert (await hierarchy.wake_verifier("parent", verifier))["outcome"] == "woken"
    assert (await db.get_task(delegate_id)).status is TaskStatus.READY
    # The verifier is filed unrouted; stand in for the router writing its
    # route so the pool selection below exercises only the origin gate.
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == delegate_id)
            .values(profile_id="verifier", intelligence_class="high", route_source="router")
        )

    verifier_owner = update(integration_branch_owners).where(
        integration_branch_owners.c.owner_id == delegate_id
    )
    this_operation = update(integration_repair_operations).where(
        integration_repair_operations.c.id == operation["id"]
    )
    async with db.immediate() as conn:
        if invalid == "operation":
            await conn.execute(this_operation.values(state="completed"))
        elif invalid == "role":
            await conn.execute(verifier_owner.values(owner_role="worker"))
        elif invalid == "attached":
            await conn.execute(
                verifier_owner.values(
                    handoff_state="attached", session_id="session", workspace_id="slot"
                )
            )
        elif invalid == "branch":
            await conn.execute(verifier_owner.values(ref="aq/unrelated"))
        elif invalid == "binding":
            await conn.execute(this_operation.values(verifier_task_id="impostor"))
        elif invalid == "target":
            await conn.execute(
                update(tasks).where(tasks.c.id == "parent").values(branch_name="aq/other")
            )
        for mode in (None, ProjectIntegrationMode(True, "repo")):
            claimable = await conn.scalar(
                select(tasks.c.id).where(tasks.c.id == delegate_id, _frontier_where("p", mode))
            )
            assert (claimable == delegate_id) is (invalid is None), mode
            selected = await db.select_ready_for_profile(
                conn,
                project_id="p",
                profile_id="verifier",
                agent_id="pool-agent",
                hierarchy_mode=mode,
            )
            assert (selected == delegate_id) is (invalid is None), mode
    assert await db.is_hierarchy_task_runnable(delegate_id) is (invalid is None)


async def test_transfer_owner_replay_after_crash_still_wakes_verifier(
    command_handler_factory,
):
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p", name="integration project"))
    await db.create_profile(AgentProfile(id="verifier", name="Verifier", harness="claude"))
    hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await db._apply_transition(
            conn, "parent", TaskStatus.PAUSED, _manual_pause_control=True
        )
        await hierarchy.parent_completion.mark_ready_on(conn, "parent")
    operation = await db.get_integration_operation(checkpointed["operation_id"])
    target = BranchKey(repository_id="repo", branch="aq/parent")
    current = await BranchOwnership(db).get_owner(target)
    crashed_transfer = await BranchOwnership(db).transfer(
        Fence(target=target, owner_id=current["owner_id"], token=current["fence_token"]),
        operation["verifier_task_id"],
        "verifier",
    )

    result = await handler.execute(
        "integration_transfer_owner",
        {
            "target": target.model_dump(mode="json"),
            "expected_token": crashed_transfer.token - 1,
            "next_owner_id": operation["verifier_task_id"],
            "next_role": "verifier",
        },
    )

    assert result["outcome"] == "transferred"
    assert (await db.get_task(operation["verifier_task_id"])).status is TaskStatus.READY


async def test_sealed_batch_member_protects_descendant_mutation(db):
    await _enable_project(db)
    await db.create_task(Task(id="root", project_id="p", title="root", description=""))
    await db.create_task(
        Task(id="root.1", project_id="p", parent_task_id="root", title="child", description="")
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="batch",
                project_id="p",
                repository_id="repo",
                request_id="request-batch",
                source_manifest_digest="sha256:" + "d" * 64,
                base_sha="a" * 40,
                lifecycle="sealing",
                integration_branch="refs/heads/integration/batch",
                current_revision=0,
                policy_snapshot={},
                artifact_snapshot={},
                cleanup_state="pending",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_review_evidence).values(
                id="review-batch",
                source_task_id="root",
                repository_id="repo",
                source_base="a" * 40,
                reviewed_head_sha="b" * 40,
                reviewed_tree_sha="c" * 40,
                reviewer_task_id="root",
                review_kind="review",
                generation=0,
                verdict="approved",
                evidence={},
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_batch_members).values(
                batch_id="batch",
                ordinal=0,
                task_id="root",
                repository_id="repo",
                source_base_sha="a" * 40,
                reviewed_head_sha="b" * 40,
                reviewed_tree_sha="c" * 40,
                review_evidence_id="review-batch",
                review_evidence={},
            )
        )
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "batch")
            .values(lifecycle="sealed")
        )
        with pytest.raises(HierarchyError, match="sealed"):
            await db.guard_integration_mutation("root.1", "reopen", conn=conn)
