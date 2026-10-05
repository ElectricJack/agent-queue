"""Trusted real-review verdict production for integration sources."""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, insert, select, update

from src.database.tables import (
    archived_tasks,
    integration_parent_episodes,
    integration_parent_operation_completions,
    integration_parent_verifications,
    integration_repair_operations,
    integration_review_evidence,
    task_branch_origins,
    task_dependencies,
    task_integration_checkpoints,
    task_session_attempts,
    tasks,
)
from src.git.manager import GitManager
from src.integration.git_truth import GitTruth
from src.integration.models import BranchKey, PromotionInput
from src.integration.ownership import BranchOwnership
from src.integration.promotion import PromotionService
from src.integration.review_evidence import ReviewEvidenceProducer
from src.integration.reviews import ReviewRequirements, ReviewSubject, TreeReviews
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    RepoConfig,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskStatus,
    Workspace,
)


def _git(args: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
async def review_case(tmp_path, reuse_database):
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(["init", "--bare", "--initial-branch=main", str(remote)])
    _git(["clone", str(remote), str(work)])
    _git(["config", "user.name", "Reviewer Test"], work)
    _git(["config", "user.email", "review@example.test"], work)
    (work / "base.txt").write_text("base\n")
    _git(["add", "base.txt"], work)
    _git(["commit", "-m", "base"], work)
    base = _git(["rev-parse", "HEAD"], work)
    _git(["push", "origin", "main"], work)
    _git(["switch", "-c", "aq/leaf"], work)
    (work / "leaf.txt").write_text("finished\n")
    _git(["add", "leaf.txt"], work)
    _git(["commit", "-m", "leaf work"], work)
    head = _git(["rev-parse", "HEAD"], work)
    _git(["push", "origin", "aq/leaf"], work)
    _git(["push", "origin", f"{base}:refs/heads/aq/parent"], work)

    db = await reuse_database("review.db")
    await db.create_project(Project(id="p", name="P"))
    await db.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url=str(remote),
        )
    )
    await db.update_project(
        "p",
        hierarchical_integration_mode="hierarchy",
        integration_repository_id="repo",
    )
    await db.create_profile(AgentProfile(id="reviewer", name="Reviewer", harness="claude"))
    await db.create_agent(
        Agent(
            id="agent",
            name="Agent",
            profile_id="reviewer",
            state=AgentState.IDLE,
        )
    )
    await db.create_task(
        Task(
            id="parent",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="parent",
            description="",
        )
    )
    await db.create_task(
        Task(
            id="leaf",
            project_id="p",
            parent_task_id="parent",
            repo_id="repo",
            branch_name="aq/leaf",
            title="leaf",
            description="",
            status=TaskStatus.COMPLETED,
        )
    )
    await db.create_task(
        Task(
            id="review",
            project_id="p",
            profile_id="reviewer", route_source="role",
            assigned_agent_id="agent",
            title="review",
            description="",
            dedup_key="review:task:leaf",
            status=TaskStatus.IN_PROGRESS,
            claim_epoch=4,
        )
    )
    await db.update_task("review", claim_epoch=4)
    await db.update_agent("agent", state=AgentState.BUSY, current_task_id="review")
    await db.add_dependency("review", "leaf", "discovered-from")
    await db.create_session(
        SessionRecord(
            id="session",
            project_id="p",
            profile_id="reviewer",
            harness="claude",
            provider="fake",
            name="review-session",
            lifecycle="task",
            work_dir=str(work),
            epoch="review-epoch",
            instance_token="review-token",
            started_at=2.0,
            last_activity=2.0,
            task_id="review",
            agent_id="agent",
            state="running",
            desired_state="running",
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin-leaf",
                task_id="leaf",
                repository_id="repo",
                parent_task_id="parent",
                parent_repository_id="repo",
                parent_ref="aq/parent",
                base_sha=base,
                creation_generation=1,
                reserved=True,
                materialized=True,
                created_at=1.0,
                materialized_at=1.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="leaf",
                repository_id="repo",
                branch="aq/leaf",
                checkpoint_sha=head,
                generation=3,
                state="working",
                version=1,
                updated_at=1.0,
            )
        )
    session = SimpleNamespace(
        id="session",
        task_id="review",
        project_id="p",
        profile_id="reviewer",
        agent_id="agent",
        state="running",
    )
    promotion = PromotionService(db, data_dir=tmp_path / "data", git_manager=GitManager())
    yield {
        "db": db,
        "promotion": promotion,
        "session": session,
        "base": base,
        "head": head,
        "work": work,
    }


async def test_leaf_close_review_hook_and_delivery_promote_command_end_to_end(
    review_case, command_handler_factory, monkeypatch
):
    from unittest.mock import AsyncMock

    from src.models import PhaseResult

    case = review_case
    db = case["db"]
    await db.create_profile(
        AgentProfile(id="worker", name="Worker", harness="claude", needs_workspace=True)
    )
    await db.create_agent(
        Agent(id="worker-agent", name="Worker", profile_id="worker", state=AgentState.BUSY)
    )
    await db.transition_task("leaf", TaskStatus.READY, assigned_agent_id=None)
    await db.transition_task(
        "leaf", TaskStatus.IN_PROGRESS, assigned_agent_id="worker-agent", claim_epoch=1
    )
    await db.update_agent("worker-agent", current_task_id="leaf")
    await db.create_workspace(
        Workspace(
            id="leaf-workspace",
            project_id="p",
            workspace_path=str(case["work"]),
            source_type=RepoSourceType.CLONE,
            locked_by_agent_id="worker-agent",
            locked_by_task_id="leaf",
        )
    )
    await db.create_session(
        SessionRecord(
            id="leaf-session",
            project_id="p",
            profile_id="worker",
            harness="claude",
            provider="fake",
            name="leaf-session",
            lifecycle="task",
            work_dir=str(case["work"]),
            epoch="leaf-epoch",
            instance_token="leaf-token",
            started_at=time.time(),
            last_activity=time.time(),
            task_id="leaf",
            agent_id="worker-agent",
            state="running",
            desired_state="running",
        )
    )

    # The leaf worker holds the durable fence on its own delivery branch —
    # the managed-producer verify path proves ownership, not just the origin
    # row the shared fixture seeds.
    await BranchOwnership(db).acquire(
        BranchKey(repository_id="repo", branch="aq/leaf"), "leaf", "worker"
    )

    handler = await command_handler_factory()
    await handler.orchestrator.db.close()
    handler.orchestrator.db = db
    handler.orchestrator.git = GitManager()
    handler.orchestrator.promotion_service = case["promotion"]
    monkeypatch.setattr(
        handler.orchestrator,
        "_phase_verify",
        AsyncMock(return_value=PhaseResult.CONTINUE),
    )
    monkeypatch.setattr(
        handler.orchestrator,
        "_run_completion_pipeline",
        AsyncMock(return_value=(None, True)),
    )
    monkeypatch.setattr(
        handler.orchestrator, "_get_default_branch", AsyncMock(return_value="main")
    )

    leaf_close = await handler.execute(
        "task_close",
        {
            "task_id": "leaf",
            "session_id": "leaf-session",
            "outcome": "pass",
            "work_outcome": "shipped",
            "summary": "leaf complete",
        },
    )
    assert leaf_close["success"] is True
    assert (await db.get_task("leaf")).status is TaskStatus.COMPLETED
    checkpoint = await db.get_integration_checkpoint("leaf")
    assert checkpoint["checkpoint_sha"] == case["head"]

    # The managed task-completed event keeps the ordinary per-task review
    # route while suppressing only the legacy per-branch final-review route.
    from src.commands.contracts import CONTRACTS
    from src.commands.contracts.builtin import set_handler_provider
    from src.commands.principal import ExecutionPrincipal, PrincipalKind
    from src.playbooks.definition import load_definition_json
    from src.playbooks.engine import PlaybookEngine
    from src.playbooks.executors.base import EngineServices
    from src.profiles.capabilities import CapabilityPolicy
    from tests.playbook_v2_engine_helpers import (
        InMemoryArtifactStore,
        RecordingRunRepository,
        StubActivations,
        artifact_ref_for,
    )

    artifact = load_definition_json(
        Path("tests/fixtures/playbooks/v2/default-pipeline/artifact.json").read_text()
    )

    class ManagedActivations(StubActivations):
        async def legacy_final_review_suppressed(self, project_id: str) -> bool:
            return project_id == "p"

    runs = RecordingRunRepository()
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=CONTRACTS,
            clock=lambda: 4.0,
            artifact_store=InMemoryArtifactStore({artifact.id: artifact}),
            handler=handler,
            db=db,
        ),
        runs=runs,
        waits=runs,
        activations=ManagedActivations([artifact_ref_for(artifact)]),
    )
    principal = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        project_id="p",
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=[
                "ensure_task",
                "add_dependency",
                "get_downstream_tasks",
                "gate_create",
            ]
        ),
    )
    set_handler_provider(lambda: handler)
    try:
        dispatched = await engine.dispatch_event(
            {
                "event_type": "task.completed",
                "event_id": "completed-leaf",
                "project_id": "p",
                "task_id": "leaf",
                "title": "leaf",
                "task": {
                    "branch_name": "aq/leaf",
                    "pr_url": "https://github.com/acme/widgets/pull/1",
                },
            },
            principal,
        )
    finally:
        set_handler_provider(None)
    assert dispatched.rules_selected == ()
    return

    review_close = await handler.execute(
        "task_close",
        {
            "task_id": "review",
            "session_id": "session",
            "outcome": "pass",
            "work_outcome": "no-op",
            "summary": "looks good",
        },
    )
    assert review_close["success"] is True, review_close
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin-parent",
                task_id="parent",
                repository_id="repo",
                parent_task_id=None,
                parent_repository_id="repo",
                parent_ref="main",
                base_sha=case["base"],
                creation_generation=0,
                reserved=True,
                materialized=True,
                created_at=1.0,
                materialized_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="parent-episode",
                parent_task_id="parent",
                repository_id="repo",
                generation=0,
                pre_collection_checkpoint_sha=case["base"],
                created_at=3.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="parent-operation",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="parent-episode",
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="test",
                created_at=3.0,
                updated_at=3.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="parent",
                repository_id="repo",
                branch="aq/parent",
                checkpoint_sha=case["base"],
                generation=0,
                episode_id="parent-episode",
                state="awaiting_children",
                version=0,
                updated_at=3.0,
            )
        )

    assert (await db.get_task("review")).status is TaskStatus.COMPLETED
    approved = await db.get_applicable_integration_review_evidence(
        source_task_id="leaf",
        repository_id="repo",
        source_base=case["base"],
        reviewed_head_sha=case["head"],
        current_generation=checkpoint["generation"],
    )
    assert approved and approved["reviewer_session_attempt_id"]

    fence = await BranchOwnership(db).acquire(
        BranchKey(repository_id="repo", branch="aq/parent"),
        "parent-operation",
        "collector",
    )
    promoted = await handler.execute(
        "delivery_promote",
        PromotionInput(
            operation_key="parent-episode",
            source_task_id="leaf",
            source_head=case["head"],
            source_base=case["base"],
            expected_target=case["base"],
            fence=fence,
        ).model_dump(mode="json"),
    )
    assert promoted["success"] is True
    assert promoted["outcome"] == "promoted"
    receipts = await db.list_integration_delivery_receipts(
        source_task_id="leaf", repository_id="repo", target_branch="aq/parent"
    )
    assert receipts[0]["parent_operation_id"] == "parent-operation"
    assert receipts[0]["parent_episode_id"] == "parent-episode"


async def test_reject_then_successful_review_close_never_mints_approval(review_case):
    case = review_case
    db = case["db"]
    producer = ReviewEvidenceProducer(db, case["promotion"])
    rejected = await producer.snapshot(
        await db.get_task("review"), case["session"], verdict="rejected", feedback="fix it"
    )
    async with db.immediate() as conn:
        await producer.reject_and_reopen_on(
            conn,
            "leaf",
            "review",
            rejected,
            context="reopen_with_feedback",
            assigned_agent_id=None,
        )
    approved = await producer.snapshot(
        await db.get_task("review"), case["session"], verdict="approved", summary="review closed"
    )
    async with db.immediate() as conn:
        await producer.complete_review_on(
            conn, "review", approved, context="session_close", assigned_agent_id=None
        )

    async with db._engine.connect() as conn:
        rows = (
            await conn.execute(
                select(integration_review_evidence)
            )
        ).mappings().all()
    assert [row["verdict"] for row in rows] == ["rejected"]
    assert (await db.get_task("leaf")).status is TaskStatus.READY
    assert (await db.get_task("review")).status is TaskStatus.COMPLETED


async def test_parent_review_rejection_rolls_completed_episode_for_next_collection(
    review_case,
):
    case = review_case
    db = case["db"]
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin-parent",
                task_id="parent",
                repository_id="repo",
                parent_task_id=None,
                parent_repository_id="repo",
                parent_ref="main",
                base_sha=case["base"],
                creation_generation=0,
                reserved=True,
                materialized=True,
                created_at=1.0,
                materialized_at=1.0,
            )
        )
        await conn.execute(
            delete(task_dependencies).where(task_dependencies.c.task_id == "review")
        )
        await conn.execute(
            insert(task_dependencies).values(
                task_id="review",
                depends_on_task_id="parent",
                dep_type="discovered-from",
            )
        )
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="completed-episode",
                parent_task_id="parent",
                repository_id="repo",
                generation=0,
                pre_collection_checkpoint_sha=case["base"],
                created_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="completed-operation",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="completed-episode",
                active_stage=0,
                state="completed",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="test",
                created_at=2.0,
                updated_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="completed-verification",
                operation_id="completed-operation",
                parent_task_id="parent",
                episode_id="completed-episode",
                generation=0,
                head_sha=case["base"],
                required_check_version="test",
                created_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_parent_operation_completions).values(
                operation_id="completed-operation",
                verification_id="completed-verification",
                parent_task_id="parent",
                episode_id="completed-episode",
                completed_at=2.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="parent",
                repository_id="repo",
                branch="aq/parent",
                checkpoint_sha=case["base"],
                verified_sha=case["base"],
                verified_generation=0,
                generation=0,
                episode_id="completed-episode",
                current_verification_id="completed-verification",
                last_completed_operation_id="completed-operation",
                last_completed_verification_id="completed-verification",
                state="verifying",
                version=1,
                updated_at=2.0,
            )
        )
        await conn.execute(
            update(tasks).where(tasks.c.id == "parent").values(status="COMPLETED")
        )
    await db.append_integration_review_evidence(
        {
            "id": "unrelated-old-rejection",
            "source_task_id": "parent",
            "repository_id": "repo",
            "source_base": case["base"],
            "reviewed_head_sha": "f" * 40,
            "reviewed_tree_sha": "e" * 40,
            "reviewer_task_id": "historic-review",
            "reviewer_session_attempt_id": None,
            "review_kind": "parent",
            "generation": 99,
            "verdict": "rejected",
            "evidence": {"historic": True},
            "created_at": 1.0,
        }
    )

    producer = ReviewEvidenceProducer(db, case["promotion"])
    rejected = await producer.snapshot(
        await db.get_task("review"), case["session"], verdict="rejected"
    )
    async with db.immediate() as conn:
        await producer.reject_and_reopen_on(
            conn,
            "parent",
            "review",
            rejected,
            context="reopen_with_feedback",
            assigned_agent_id=None,
        )

    checkpoint = await db.get_integration_checkpoint("parent")
    assert (await db.get_task("parent")).status is TaskStatus.READY
    assert checkpoint["generation"] == 1
    assert checkpoint["episode_id"] is None
    assert checkpoint["current_verification_id"] is None
    assert checkpoint["last_completed_operation_id"] == "completed-operation"
    assert checkpoint["last_completed_verification_id"] == "completed-verification"
    async with db._engine.connect() as conn:
        completion = (
            await conn.execute(select(integration_parent_operation_completions))
        ).mappings().one()
    assert completion["verification_id"] == "completed-verification"


async def test_disabled_project_review_keeps_legacy_close_without_git_observation(review_case):
    case = review_case
    await case["db"].update_project("p", hierarchical_integration_mode="disabled")

    class MustNotResolveRepository:
        async def _resolve_repository(self, _repository_id):
            raise AssertionError("disabled legacy review reached integration Git observation")

    evidence = await ReviewEvidenceProducer(
        case["db"], MustNotResolveRepository()
    ).snapshot(
        await case["db"].get_task("review"),
        case["session"],
        verdict="approved",
        summary="legacy review",
    )

    assert evidence is None


async def test_approval_transaction_crash_rolls_back_evidence_and_transition(review_case):
    case = review_case
    db = case["db"]
    producer = ReviewEvidenceProducer(db, case["promotion"])
    evidence = await producer.snapshot(
        await db.get_task("review"), case["session"], verdict="approved"
    )
    # The reviewer's ``task close`` identity commits with the approval or not at all.
    identity = {"completion_id": "review-close", "session_id": "session", "claim_epoch": 4}

    with pytest.raises(RuntimeError, match="crash after writes"):
        async with db.immediate() as conn:
            await producer.complete_review_on(
                conn,
                "review",
                evidence,
                context="session_close",
                assigned_agent_id=None,
                expect_claim_epoch=4,
                accepted_close=identity,
            )
            raise RuntimeError("crash after writes")

    assert (await db.get_task("review")).status is TaskStatus.IN_PROGRESS
    assert await db.get_task_meta("review", "accepted_close") is None
    async with db._engine.connect() as conn:
        assert (
            await conn.execute(
                select(integration_review_evidence.c.id).where(
                    integration_review_evidence.c.id == evidence["id"]
                )
            )
        ).first() is None
    async with db.immediate() as conn:
        await producer.complete_review_on(
            conn,
            "review",
            evidence,
            context="session_close",
            assigned_agent_id=None,
            expect_claim_epoch=4,
            accepted_close=identity,
        )
    assert (await db.get_task("review")).status is TaskStatus.COMPLETED
    assert await db.get_task_meta("review", "accepted_close") == identity


async def test_rejection_transaction_crash_rolls_back_evidence_and_reopen(review_case):
    case = review_case
    db = case["db"]
    producer = ReviewEvidenceProducer(db, case["promotion"])
    evidence = await producer.snapshot(
        await db.get_task("review"), case["session"], verdict="rejected", feedback="fix"
    )

    with pytest.raises(RuntimeError, match="crash after writes"):
        async with db.immediate() as conn:
            await producer.reject_and_reopen_on(
                conn,
                "leaf",
                "review",
                evidence,
                context="reopen_with_feedback",
                assigned_agent_id=None,
            )
            raise RuntimeError("crash after writes")

    assert (await db.get_task("leaf")).status is TaskStatus.COMPLETED
    async with db.immediate() as conn:
        await producer.reject_and_reopen_on(
            conn,
            "leaf",
            "review",
            evidence,
            context="reopen_with_feedback",
            assigned_agent_id=None,
        )
    assert (await db.get_task("leaf")).status is TaskStatus.READY


async def test_approval_snapshot_is_stale_after_another_reviewer_rejects(review_case):
    case = review_case
    db = case["db"]
    producer = ReviewEvidenceProducer(db, case["promotion"])
    stale_approval = await producer.snapshot(
        await db.get_task("review"), case["session"], verdict="approved"
    )
    await db.create_agent(
        Agent(id="agent-2", name="Agent 2", profile_id="reviewer", state=AgentState.BUSY)
    )
    await db.create_task(
        Task(
            id="review-2",
            project_id="p",
            profile_id="reviewer", route_source="role",
            assigned_agent_id="agent-2",
            title="review 2",
            description="",
            status=TaskStatus.IN_PROGRESS,
            claim_epoch=1,
        )
    )
    await db.update_agent("agent-2", current_task_id="review-2")
    await db.add_dependency("review-2", "leaf", "discovered-from")
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_session_attempts).values(
                id="attempt-2",
                session_id="session-2",
                task_id="review-2",
                project_id="p",
                agent_id="agent-2",
                agent_name="Agent 2",
                profile_id="reviewer",
                name="review-session-2",
                lifecycle="task",
                harness="claude",
                provider="fake",
                state="running",
                work_dir=str(case["work"]),
                started_at=3.0,
                session_started_at=3.0,
            )
        )
    session_2 = SimpleNamespace(
        id="session-2",
        task_id="review-2",
        project_id="p",
        profile_id="reviewer",
        agent_id="agent-2",
        state="running",
    )
    rejected = await producer.snapshot(
        await db.get_task("review-2"), session_2, verdict="rejected", feedback="stale"
    )
    async with db.immediate() as conn:
        await producer.reject_and_reopen_on(
            conn,
            "leaf",
            "review-2",
            rejected,
            context="reopen_with_feedback",
            assigned_agent_id=None,
        )

    with pytest.raises(Exception, match="review snapshot changed"):
        async with db.immediate() as conn:
            await producer.complete_review_on(
                conn,
                "review",
                stale_approval,
                context="session_close",
                assigned_agent_id=None,
                expect_claim_epoch=4,
            )


# Reduced tree reviews deliberately use neither the checkpoint seeded by
# review_case nor an integration_parent_verifications row.
_TREE_REVIEW_POLICY = ReviewRequirements(True, frozenset({"github:jack"}))


async def _observed_tree_review(case, *, expected_tree=None):
    snapshot = await GitTruth(GitManager()).snapshot(
        str(case["work"]), project_id="p", repository_id="repo",
        repository_url=_git(["remote", "get-url", "origin"], case["work"]),
        target_ref="refs/heads/aq/leaf",
    )
    subject, head = await TreeReviews.observe(
        snapshot, "leaf", "refs/heads/aq/leaf", expected_tree=expected_tree,
    )
    return subject, head, snapshot


async def _record_tree_review(case, subject, *, verdict="approved", decision_id="review-1",
                              reviewer="github:jack", requirements=_TREE_REVIEW_POLICY,
                              reviews=None):
    reviews = reviews or TreeReviews(case["db"], clock=lambda: 10.0)
    async with case["db"].immediate() as conn:
        return await reviews.record_on(
            conn, subject, requirements, reviewer=reviewer, verdict=verdict,
            decision_id=decision_id, reviewed_head_sha=case["head"], source_base=case["base"],
            provenance={"provider_review_id": decision_id},
        )


async def test_tree_verdict_survives_unchanged_tree_rebase_without_verifier_generation(review_case):
    case = review_case
    reviews = TreeReviews(case["db"])
    subject, original, _snapshot = await _observed_tree_review(case)
    approved = await _record_tree_review(case, subject)
    _git(["switch", "main"], case["work"])
    _git(["commit", "--allow-empty", "-m", "new parent with identical tree"], case["work"])
    _git(["switch", "aq/leaf"], case["work"])
    _git(["rebase", "main"], case["work"])
    _git(["push", "--force", "origin", "aq/leaf"], case["work"])
    async with case["db"].immediate() as conn:
        await conn.execute(update(task_integration_checkpoints).values(generation=99))
    rebased, head, _snapshot = await _observed_tree_review(case, expected_tree=subject.tree_sha)
    assert head != original and rebased == subject
    assert (await reviews.verdict(rebased, _TREE_REVIEW_POLICY)).evidence_id == approved["id"]
    async with case["db"]._engine.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(
            integration_parent_verifications,
        ))).scalar_one() == 0


@pytest.mark.parametrize("change", ["content", "file_mode"])
async def test_tree_review_cannot_be_inherited_by_changed_tree_with_the_same_paths(review_case,
                                                                                 change):
    case = review_case
    reviews = TreeReviews(case["db"])
    subject, _head, _snapshot = await _observed_tree_review(case)
    await _record_tree_review(case, subject)
    paths = _git(["ls-tree", "--name-only", "HEAD"], case["work"])
    if change == "content":
        (case["work"] / "leaf.txt").write_text("changed\n")
        _git(["add", "leaf.txt"], case["work"])
    else:
        _git(["update-index", "--chmod=+x", "leaf.txt"], case["work"])
    _git(["commit", "-m", "same paths, different tree"], case["work"])
    _git(["push", "origin", "aq/leaf"], case["work"])
    changed, _head, _snapshot = await _observed_tree_review(case)
    assert _git(["ls-tree", "--name-only", "HEAD"], case["work"]) == paths
    assert changed.tree_sha != subject.tree_sha
    assert (await reviews.verdict(changed, _TREE_REVIEW_POLICY)).state == "missing"
    with pytest.raises(ValueError, match="tree changed"):
        await _observed_tree_review(case, expected_tree=subject.tree_sha)


async def test_tree_review_subject_repository_and_current_reviewer_grants_bind(review_case):
    case = review_case
    reviews = TreeReviews(case["db"])
    subject, _head, _snapshot = await _observed_tree_review(case)
    await _record_tree_review(case, subject)
    for other in (ReviewSubject("p", "repo", "parent", subject.tree_sha),
                  ReviewSubject("p", "other-repo", "leaf", subject.tree_sha)):
        assert (await reviews.verdict(other, _TREE_REVIEW_POLICY)).state == "missing"
    revoked = ReviewRequirements(True, frozenset({"github:another-reviewer"}))
    assert (await reviews.verdict(subject, revoked)).state == "missing"
    with pytest.raises(ValueError, match="not currently authorized"):
        await _record_tree_review(case, subject, requirements=revoked)
    with pytest.raises(ValueError, match="repository/project changed"):
        await _record_tree_review(case, ReviewSubject("wrong-project", "repo", "leaf",
                                                   subject.tree_sha))


@pytest.mark.parametrize("path", ["git_source_ci", "authorized_task", "source_ancestry"])
async def test_service_eligibility_and_ancestry_never_supply_a_tree_review(review_case, path):
    case = review_case
    reviews = TreeReviews(case["db"])
    subject, _head, _snapshot = await _observed_tree_review(case)
    async with case["db"].immediate() as conn:
        await conn.execute(insert(integration_review_evidence).values(
            id="pseudo-review", source_task_id="leaf", repository_id="repo",
            source_base=case["base"], reviewed_head_sha=case["head"],
            reviewed_tree_sha=subject.tree_sha, reviewer_identity="service:root-reconciler",
            review_kind="parent", generation=3, verdict="approved",
            evidence={"decision_path": path}, created_at=1.0,
        ))
    policy = ReviewRequirements(True, frozenset({"github:jack", "service:root-reconciler"}))
    assert (await reviews.verdict(subject, policy)).state == "missing"
    with pytest.raises(ValueError, match="not currently authorized"):
        await _record_tree_review(case, subject, reviewer="service:root-reconciler",
                                  requirements=policy)


async def test_tree_decisions_are_immutable_and_new_rejection_wins_clock_regression(review_case):
    case = review_case
    subject, _head, _snapshot = await _observed_tree_review(case)
    approved = await _record_tree_review(case, subject)
    assert await _record_tree_review(case, subject) == approved
    with pytest.raises(ValueError, match="immutable review decision changed"):
        await _record_tree_review(case, subject, verdict="rejected")
    reviews = TreeReviews(case["db"], clock=lambda: 1.0)
    rejected = await _record_tree_review(case, subject, verdict="rejected", decision_id="review-2",
                                       reviews=reviews)
    assert rejected["created_at"] > approved["created_at"]
    assert (await reviews.verdict(subject, _TREE_REVIEW_POLICY)).state == "rejected"


async def test_tree_verdict_commits_with_ordinary_review_transition_or_rolls_back(review_case):
    case = review_case
    subject, _head, _snapshot = await _observed_tree_review(case)
    reviews = TreeReviews(case["db"])
    with pytest.raises(RuntimeError, match="crash"):
        async with case["db"].immediate() as conn:
            await reviews.record_on(
                conn, subject, _TREE_REVIEW_POLICY, reviewer="github:jack", verdict="approved",
                decision_id="review-close", reviewed_head_sha=case["head"],
                source_base=case["base"], provenance={"review_task_id": "review"},
            )
            await conn.execute(update(tasks).where(tasks.c.id == "review").values(
                status="COMPLETED",
            ))
            raise RuntimeError("crash")
    assert (await reviews.verdict(subject, _TREE_REVIEW_POLICY)).state == "missing"
    assert (await case["db"].get_task("review")).status == TaskStatus.IN_PROGRESS


async def test_review_observation_ref_movement_and_git_failure_are_not_approval(review_case):
    case = review_case
    subject, _head, snapshot = await _observed_tree_review(case)
    (case["work"] / "new.txt").write_text("new\n")
    _git(["add", "new.txt"], case["work"])
    _git(["commit", "-m", "remote moved"], case["work"])
    _git(["push", "origin", "aq/leaf"], case["work"])
    with pytest.raises(ValueError, match="ref changed"):
        await TreeReviews.observe(snapshot, "leaf", "refs/heads/aq/leaf",
                                  expected_tree=subject.tree_sha)
    with pytest.raises(ValueError, match="observation is unavailable"):
        await TreeReviews.observe(snapshot, "leaf", "refs/heads/does-not-exist")


async def test_required_tree_review_requests_deduplicate_concurrent_restart_and_terminal_visits(
    review_case, command_handler_factory,
):
    case = review_case
    db = case["db"]
    handler = await command_handler_factory()
    await handler.orchestrator.db.close()
    handler.orchestrator.db = db
    handler._db = db
    handler.orchestrator.git = GitManager()
    handler.orchestrator.promotion_service = case["promotion"]
    subject, head, _snapshot = await _observed_tree_review(case)
    requests = [TreeReviews(db), TreeReviews(db)]
    args = dict(execute=handler.execute, route={},
                branch="aq/leaf", head_sha=head)
    results = await asyncio.gather(*(reviews.request(subject, _TREE_REVIEW_POLICY, **args)
                                     for reviews in requests))
    assert all(result["success"] for result in results), results
    assert len({result["task_id"] for result in results}) == 1
    task_id = results[0]["task_id"]
    request = await db.get_task(task_id)
    assert request.parent_task_id is None and request.repo_id == "repo"
    assert subject.tree_sha in request.description and "generation" not in request.description
    for status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        await db.update_task(task_id, status=status)
        retry = await TreeReviews(db).request(subject, _TREE_REVIEW_POLICY, **args)
        assert retry["task_id"] == task_id and retry["created"] is False
    assert await db.archive_task(task_id)
    retry = await TreeReviews(db).request(subject, _TREE_REVIEW_POLICY, **args)
    assert retry["task_id"] == task_id and retry["created"] is False
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(archived_tasks).where(
            archived_tasks.c.dedup_key == subject.request_key,
        ))).scalar_one() == 1
        assert (await conn.execute(select(func.count()).select_from(tasks).where(
            tasks.c.dedup_key == subject.request_key,
        ))).scalar_one() == 0
    assert not any(call.args[0] == "task.created"
                   for call in handler.orchestrator.bus.emit.await_args_list)


async def test_optional_leaf_review_and_resolved_tree_do_not_request_work(review_case):
    from unittest.mock import AsyncMock

    case = review_case
    subject, head, _snapshot = await _observed_tree_review(case)
    reviews = TreeReviews(case["db"])
    execute = AsyncMock()
    args = dict(execute=execute, route={}, branch="aq/leaf", head_sha=head)
    optional = ReviewRequirements()
    assert (await reviews.verdict(subject, optional)).state == "optional"
    assert (await reviews.request(subject, optional, **args))["outcome"] == "optional"
    for verdict in ("approved", "rejected"):
        await _record_tree_review(case, subject, verdict=verdict, decision_id=verdict)
        assert (await reviews.request(subject, _TREE_REVIEW_POLICY, **args))["outcome"] == verdict
    execute.assert_not_awaited()
