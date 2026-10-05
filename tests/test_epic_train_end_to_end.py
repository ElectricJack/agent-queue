"""The epic PR and integration train advance as one connected delivery."""

from __future__ import annotations

import subprocess
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, insert, select, update

from src.database import Database
from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_candidate_revisions,
    integration_candidate_resolutions,
    integration_check_evidence,
    integration_cleanup_items,
    integration_parent_episodes,
    integration_parent_operation_completions,
    integration_parent_verifications,
    integration_repair_operations,
    integration_branch_owners,
    playbook_artifacts,
    task_branch_origins,
    task_dependencies,
    task_delivery_receipts,
    task_integration_checkpoints,
    tasks,
    task_labels,
    workspaces,
)
from src.git.github_app import GitHubRepositoryBinding
from src.git.manager import GitManager
from src.integration.candidates import AuditPullRequest, CandidateResolutionInput, CandidateService
from src.integration.ci import (
    CIReceiptPayload,
    CIService,
    CandidateCISubject,
    FailedCIObservation,
    IntegrationCITrust,
    TrustedCIObservation,
    TrustedFixtureObserver,
)
from src.integration.cleanup import IntegrationCleanupService
from src.integration.root_pull_requests import EpicPullRequestService
from src.integration.main_promotion import RootAttestationProof, RootPromotionService
from src.integration.promotion import PromotionService
from src.integration.repair import RepairService
from src.integration.review_evidence import ReviewEvidenceProducer
from src.integration.scheduler import IntegrationScheduler, TrainService
from src.integration.release import IntegrationReleaseService
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchOwnership
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.profiles.capabilities import CapabilityPolicy
from src.models import AgentProfile, Project, RepoConfig, RepoSourceType, SessionRecord, TaskStatus
from tests.db_fixtures import lease_dsn
from tests.test_integration_candidates import _AppClient, _LocalPushGit
from tests.test_integration_sealing import _artifact, _policy


def _git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


async def _one(db, table, *conditions):
    async with db._engine.connect() as conn:
        return (await conn.execute(select(table).where(*conditions))).mappings().one()


class RevisingAuditForge:
    """One open audit PR follows the candidate branch through a repair revision."""

    def __init__(self):
        self.revisions: dict[str, AuditPullRequest] = {}

    async def lookup_audit_pr(self, *, idempotency_key, branch):
        return self.revisions.get(idempotency_key)

    async def create_audit_pr(self, **kwargs):
        result = AuditPullRequest(
            url="https://github.com/example/repo/pull/9",
            number=9,
            head_sha=kwargs["head_sha"],
            head_branch=kwargs["branch"],
            base_branch=kwargs["base_branch"],
            repository_numeric_id=kwargs["repository_numeric_id"],
            repository_full_name=kwargs["repository_full_name"],
            idempotency_key=kwargs["idempotency_key"],
        )
        self.revisions[kwargs["idempotency_key"]] = result
        return result


class CleanupGit(_LocalPushGit):
    def __init__(self, origin):
        super().__init__(origin)
        self.deleted: list[str] = []

    async def adelete_repository_ref(self, _store, *, repository, branch, expected_old_oid):
        assert repository == GitHubRepositoryBinding(9, "example/repo")
        _git(self.origin, "update-ref", "-d", f"refs/heads/{branch}", expected_old_oid)
        self.deleted.append(branch)


class CleanupForge:
    def __init__(self, source, repaired):
        self.prs = {
            1: {
                "repository_numeric_id": 9,
                "repository_full_name": "example/repo",
                "head_sha": source,
                "state": "open",
            },
            9: {
                "repository_numeric_id": 9,
                "repository_full_name": "example/repo",
                "head_sha": repaired,
                "state": "open",
            },
        }
        self.markers: set[tuple[int, str]] = set()
        self.comments: list[str] = []
        self.closed: list[int] = []

    async def exact_pull_request(self, *, number):
        return self.prs.get(number)

    async def has_comment_marker(self, *, number, marker):
        return (number, marker) in self.markers

    async def comment_pull_request(self, *, number, marker, body):
        self.markers.add((number, marker))
        self.comments.append(body)

    async def close_pull_request(self, *, number):
        self.prs[number]["state"] = "closed"
        self.closed.append(number)


@pytest.mark.asyncio
@pytest.mark.parametrize("continuous", [False, True])
async def test_epic_pr_approval_red_ci_repair_exact_promotion_and_cleanup(tmp_path, continuous):
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.name", "Epic Train Test")
    _git(work, "config", "user.email", "train@example.test")
    (work / "base.txt").write_text("base\n")
    if continuous:
        versions = work / "migrations" / "versions"
        versions.mkdir(parents=True)
        (work / "alembic.ini").write_text("[alembic]\nscript_location = migrations\n")
        (versions / "base.py").write_text("revision='a00000000001'\ndown_revision=None\n")
        _git(work, "add", "migrations", "alembic.ini")
    _git(work, "add", "base.txt")
    _git(work, "commit", "-m", "base")
    base = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")
    child_heads = []
    for index in range(3):
        child_base = child_heads[0] if index == 1 else base
        _git(work, "switch", "-c", f"aq/child-{index}", child_base)
        (work / f"child-{index}.txt").write_text(f"child {index}\n")
        _git(work, "add", f"child-{index}.txt")
        _git(work, "commit", "-m", f"child {index}")
        child_heads.append(_git(work, "rev-parse", "HEAD"))
        _git(work, "push", "origin", f"HEAD:refs/heads/aq/child-{index}")
    branch = "aq/epic/retire-the-publisher"
    _git(work, "switch", "-c", branch, base)
    for index in range(3):
        _git(work, "merge", "--no-ff", "--no-edit", f"aq/child-{index}")
    if continuous:
        (work / "base.txt").write_text("epic feature\n")
        (versions / "feature.py").write_text("revision='a00000000002'\ndown_revision='a00000000001'\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-m", "epic migration and shared code")
    source = _git(work, "rev-parse", "HEAD")
    source_tree = _git(work, "rev-parse", "HEAD^{tree}")
    for child_head in child_heads:
        _git(work, "merge-base", "--is-ancestor", child_head, source)
    _git(work, "push", "origin", f"HEAD:refs/heads/{branch}")
    extra_sources = []
    if continuous:
        for index in (1, 2):
            source_branch = f"aq/other-{index}"
            _git(work, "switch", "-c", source_branch, base)
            (work / "base.txt").write_text(f"other feature {index}\n")
            (versions / f"other-{index}.py").write_text("revision='a00000000002'\ndown_revision='a00000000001'\n")
            _git(work, "add", "-A")
            _git(work, "commit", "-m", f"other {index} with colliding migration id")
            extra_sources.append((f"z-other-{index}", source_branch, _git(work, "rev-parse", "HEAD"), index + 1))
            _git(work, "push", "origin", f"HEAD:{source_branch}")
        _git(work, "switch", "-c", "aq/future", base)
        (work / "future.txt").write_text("next batch\n")
        _git(work, "add", "future.txt")
        _git(work, "commit", "-m", "already approved next batch source")
        future = _git(work, "rev-parse", "HEAD")
        _git(work, "push", "origin", "HEAD:aq/future")

    db = Database(lease_dsn("epic-train-end-to-end.db"))
    await db.initialize()
    try:
        await db.create_profile(AgentProfile(id="repairer", name="Repairer"))
        await db.create_profile(AgentProfile(id="debugger", name="Debugger"))
        await db.create_project(Project(id="p", name="Epic train"))
        await db.create_repo(
            RepoConfig(
                id="repo",
                project_id="p",
                source_type=RepoSourceType.CLONE,
                url=str(origin),
                default_branch="main",
            )
        )
        policy = _policy()
        for boundary in ("parent", "root"):
            policy[boundary]["required_checks"]["producer_id"] = "404"
        if continuous:
            policy["root"]["repair"].update(conflict_scope="batch", on_exhausted="continue")
        await db.update_project(
            "p",
            hierarchical_integration_mode="train",
            integration_repository_id="repo",
            hierarchical_integration_policy=policy,
            integration_mode="pull_request",
        )
        async with db.immediate() as conn:
            await conn.execute(
                insert(playbook_artifacts).values(
                    **_artifact().model_dump(),
                    scope="project",
                    scope_identifier="p",
                    profile_fingerprint="",
                    path="/tmp/epic-train-artifact",
                    size_bytes=1,
                    validation="{}",
                    created_at=1.0,
                )
            )
            await conn.execute(
                insert(tasks).values(
                    id="epic",
                    project_id="p",
                    repo_id="repo",
                    title="Retire the publisher",
                    description="",
                    status="COMPLETED",
                    branch_name=branch,
                    integration_mode="pull_request",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
            for index in range(3):
                await conn.execute(
                    insert(tasks).values(
                        id=f"child-{index}",
                        project_id="p",
                        repo_id="repo",
                        parent_task_id="epic",
                        title=f"Child {index}",
                        description="",
                        status="COMPLETED",
                        branch_name=f"aq/child-{index}",
                        created_at=1.0,
                        updated_at=1.0,
                    )
                )
                await conn.execute(
                    insert(task_delivery_receipts).values(
                        id=f"child-receipt-{index}",
                        domain_key=f"child:child-{index}",
                        source_task_id=f"child-{index}",
                        target_task_id="epic",
                        repository_id="repo",
                        target_branch=branch,
                        reviewed_head_sha=child_heads[index],
                        disposition="code",
                        created_at=2.0,
                    )
                )
            await conn.execute(
                insert(task_dependencies).values(
                    task_id="child-1", depends_on_task_id="child-0", dep_type="blocks"
                )
            )
            await conn.execute(
                insert(task_branch_origins).values(
                    id="origin-epic",
                    task_id="epic",
                    repository_id="repo",
                    base_sha=base,
                    creation_generation=1,
                    reserved=True,
                    created_at=1.0,
                )
            )
            await conn.execute(
                insert(integration_parent_episodes).values(
                    id="episode-epic",
                    parent_task_id="epic",
                    repository_id="repo",
                    generation=1,
                    pre_collection_checkpoint_sha=base,
                    created_at=1.0,
                )
            )
            await conn.execute(
                insert(integration_repair_operations).values(
                    id="operation-epic",
                    target_kind="parent",
                    parent_task_id="epic",
                    episode_id="episode-epic",
                    active_stage=0,
                    state="completed",
                    policy_snapshot=policy,
                    artifact_snapshot=_artifact().model_dump(mode="json"),
                    required_check_version="checks-v1",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
            await conn.execute(
                insert(integration_parent_verifications).values(
                    id="verification-epic",
                    operation_id="operation-epic",
                    parent_task_id="epic",
                    episode_id="episode-epic",
                    generation=1,
                    head_sha=source,
                    required_check_version="checks-v1",
                    created_at=1.0,
                )
            )
            await conn.execute(
                insert(integration_parent_operation_completions).values(
                    operation_id="operation-epic",
                    verification_id="verification-epic",
                    parent_task_id="epic",
                    episode_id="episode-epic",
                    completed_at=1.0,
                )
            )
            await conn.execute(
                insert(task_integration_checkpoints).values(
                    task_id="epic",
                    repository_id="repo",
                    branch=branch,
                    checkpoint_sha=source,
                    generation=1,
                    verified_sha=source,
                    verified_generation=1,
                    episode_id="episode-epic",
                    current_verification_id="verification-epic",
                    last_completed_operation_id="operation-epic",
                    last_completed_verification_id="verification-epic",
                    updated_at=1.0,
                )
            )

        pr_git = AsyncMock()
        pr_git.acreate_pr.return_value = "https://github.com/example/repo/pull/1"
        opened = await EpicPullRequestService(db, git_manager=pr_git).open_for_epic("epic")
        assert opened["outcome"] == "opened"
        assert pr_git.acreate_pr.await_count == 1
        assert all(
            f"`child-{index}`" in pr_git.acreate_pr.await_args.kwargs["body"] for index in range(3)
        )
        assert (await EpicPullRequestService(db, git_manager=pr_git).open_for_epic("epic"))[
            "outcome"
        ] == "already_open"

        scheduler = IntegrationScheduler(db)
        await scheduler.configure(project_id="p", now=0.0, enabled=True, interval_seconds=300)
        review = await ReviewEvidenceProducer(
            db,
            PromotionService(db, data_dir=tmp_path / "data", git_manager=GitManager()),
            clock=lambda: 1000.0,
        ).snapshot_from_pull_request(
            "epic",
            verdict="approved",
            reviewer_login="human",
            reviewed_sha=source,
        )
        assert review["reviewer_identity"] == "github:human"
        if continuous:
            async with db.immediate() as conn:
                for task_id, source_branch, head, number in extra_sources + [("future", "aq/future", future, 10)]:
                    await conn.execute(insert(tasks).values(
                        id=task_id, project_id="p", repo_id="repo", title=task_id, description="",
                        status="COMPLETED", task_type="feature", branch_name=source_branch,
                        pr_url=f"https://github.com/example/repo/pull/{number}", created_at=1.0, updated_at=1.0))
                    await conn.execute(insert(task_branch_origins).values(
                        id=f"origin-{task_id}", task_id=task_id, repository_id="repo", base_sha=base,
                        creation_generation=0, reserved=True, created_at=1.0))
                    await conn.execute(insert(task_integration_checkpoints).values(
                        task_id=task_id, repository_id="repo", branch=source_branch,
                        checkpoint_sha=head, generation=0, updated_at=1.0))
                await conn.execute(insert(task_labels).values(task_id="future", label="hold:next-batch"))
            producer = ReviewEvidenceProducer(db, PromotionService(db, data_dir=tmp_path / "data", git_manager=GitManager()), clock=lambda: 1000.0)
            for task_id, _branch, head, _number in extra_sources + [("future", "aq/future", future, 10)]:
                assert await producer.snapshot_from_pull_request(task_id, verdict="approved", reviewer_login="human", reviewed_sha=head)
        assert (await scheduler.mark_due("p", 1299.0, "periodic"))["outcome"] == "not_due"
        due = await scheduler.mark_due("p", 1300.0, "periodic")
        assert due["outcome"] == "due"
        sealed = await TrainService(db).seal("p", due["request_id"], 1301.0)
        assert sealed["outcome"] == "sealed"
        batch_id, operation_id = sealed["batch_id"], sealed["operation_id"]
        member = await _one(
            db, integration_batch_members, integration_batch_members.c.batch_id == batch_id,
            integration_batch_members.c.task_id == "epic",
        )
        assert member["task_id"] == "epic"
        assert member["review_evidence_id"] == review["id"]
        assert member["reviewed_tree_sha"] == source_tree
        assert member["source_ref"] == f"refs/heads/{branch}"

        app = _AppClient(origin)
        app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
        git = _LocalPushGit(origin)
        forge = RevisingAuditForge()
        async def release_writer(row):
            async with db.immediate() as conn:
                await conn.execute(update(integration_branch_owners).where(
                    integration_branch_owners.c.id == row["id"],
                    integration_branch_owners.c.fence_token == row["fence_token"]
                ).values(handoff_state="released", session_id=None, workspace_id=None,
                         confirmed_workspace_id=row["workspace_id"]))
            return True
        ownership = BranchOwnership(db, confirm_handoff=release_writer)
        repair_service = RepairService(db, clock=lambda: 1321.0)
        candidate = CandidateService(
            db,
            data_dir=tmp_path / "data",
            git_manager=git,
            app_client=app,
            forge_provider=forge,
            branch_ownership=ownership,
            repair_service=repair_service,
            clock=lambda: 1310.0,
        )
        built = await candidate.build(batch_id)
        if continuous:
            assert built.outcome == "conflict"
            repair_task_id = f"repair-{operation_id}-0"
            target = BranchKey(repository_id="repo", branch=built.branch)
            owner = await ownership.get_owner(target)
            fence = Fence(target=target, owner_id=repair_task_id, token=owner["fence_token"])
            async with db.immediate() as conn:
                await conn.execute(update(tasks).where(tasks.c.id == repair_task_id).values(status="IN_PROGRESS"))
                await conn.execute(insert(workspaces).values(id="repair-work", project_id="p", workspace_path=str(work), source_type="link", locked_by_task_id=repair_task_id, enabled=True, created_at=1310.0))
            await db.create_session(SessionRecord(id="conflict-session", task_id=repair_task_id, project_id="p", profile_id="repairer", harness="fake", provider="fake", name="conflict-session", lifecycle="task", state="running", work_dir=str(work), epoch="conflict", instance_token="conflict-token", started_at=1310.0))
            await ownership.attach(fence, "conflict-session", "repair-work", expected_role="repair")
            _git(work, "fetch", str(origin), built.branch)
            _git(work, "switch", "--detach", "FETCH_HEAD")
            partial = _git(work, "rev-parse", "HEAD")
            for _task_id, source_branch, head, _number in extra_sources:
                merged = subprocess.run(["git", "merge", "--no-ff", "--no-edit", head], cwd=work, capture_output=True, text=True)
                assert merged.returncode == 1
                (work / "base.txt").write_text("epic and both other features\n")
                _git(work, "add", "-A")
                _git(work, "commit", "-m", f"resolve complete batch member {source_branch}")
            from alembic.config import Config
            from alembic.script import ScriptDirectory
            # All three sources used the same revision id; preserve each
            # migration and re-chain them into one valid ordered head.
            for index in (1, 2):
                (versions / f"other-{index}.py").write_text(f"revision='a0000000000{index + 2}'\ndown_revision='a0000000000{index + 1}'\n")
            migration_config = Config()
            migration_config.set_main_option("script_location", str(work / "migrations"))
            assert ScriptDirectory.from_config(migration_config).get_heads() == ["a00000000004"]
            _git(work, "add", "-A")
            _git(work, "commit", "-m", "re-chain every colliding batch migration")
            resolved = _git(work, "rev-parse", "HEAD")
            principal = ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=CapabilityPolicy.from_namespaces(aq_commands=[]), session_id="conflict-session", session_instance_token="conflict-token", task_id=repair_task_id, project_id="p", profile_id="repairer")
            with principal_context(principal):
                reservation = await candidate.reserve_repair(CandidateResolutionInput(
                    batch_id=batch_id, revision=0, member_ordinal=1, operation_id=operation_id,
                    resolved_head_sha=resolved, resolved_tree_sha=_git(work, "rev-parse", "HEAD^{tree}"),
                    repair_commit_shas=tuple(_git(work, "rev-list", "--first-parent", "--reverse", f"{partial}..{resolved}").split()), fence=fence))
                publication_id = await candidate.push_repair(reservation, fence)
                assert publication_id == reservation
                publication = await _one(db, integration_candidate_resolutions, integration_candidate_resolutions.c.id == reservation)
                assert _git(origin, "rev-parse", publication["target_branch"]) == resolved
            assert (await candidate.accept_repair(reservation)).outcome == "accepted"
            await db.update_session("conflict-session", state="stopped", desired_state="stopped")
            async with db.immediate() as conn:
                await db._apply_transition(conn, repair_task_id, TaskStatus.COMPLETED, context="test_conflict_resolved", force=True)
            built = await candidate.build(batch_id)
            for head in [source] + [item[2] for item in extra_sources]:
                _git(origin, "merge-base", "--is-ancestor", head, built.head_sha)
        assert built.outcome == "built"
        assert built.head_sha and built.head_sha != source
        assert _git(origin, "rev-parse", built.branch) == built.head_sha
        subject = CandidateCISubject(
            operation_id=operation_id,
            batch_id=batch_id,
            revision=0,
            candidate_sha=built.head_sha,
        )
        trust = IntegrationCITrust(
            canonical_repository_id="repo",
            repository_id=9,
            full_name="example/repo",
            producer_id="404",
            required_checks={"version": "checks-v1", "names": ("unit",)},
        )
        failure = FailedCIObservation(
            checks=(
                {
                    "name": "unit",
                    "check_run_id": 11,
                    "check_suite_id": 21,
                    "producer_app_id": 404,
                    "head_sha": built.head_sha,
                    "conclusion": "failure",
                },
            ),
            workflow_runs=(
                {
                    "workflow_run_id": 31,
                    "run_attempt": 1,
                    "check_suite_id": 21,
                    "head_sha": built.head_sha,
                    "conclusion": "failure",
                },
            ),
            workflow_ids={21: 301},
            conclusion="failure",
        )
        red = await CIService(
            db, trust, TrustedFixtureObserver(failure), clock=lambda: 1320.0
        ).observe_candidate(subject)
        assert red["outcome"] == "red"
        assert (await _one(db, integration_batches, integration_batches.c.id == batch_id))[
            "tested_candidate_sha"
        ] is None

        async def exact_attestation(subject):
            return RootAttestationProof(
                **subject.model_dump(),
                check_run_id=7001,
                external_id="aq-attestation-v1:" + "9" * 64,
            )

        promoter = RootPromotionService(
            db,
            data_dir=tmp_path / "data",
            git_manager=git,
            app_client=app,
            attestation_resolver=exact_attestation,
            clock=lambda: 1350.0,
        )
        assert (await promoter.prepare(batch_id, 0)).outcome == "ci_missing"

        if continuous:
            dispatched = await repair_service.dispatch(operation_id, 0)
            assert dispatched["outcome"] == "dispatched" and dispatched["stage"] == 1
            assert dispatched["repair_task_id"] != repair_task_id
            ci_task_id = dispatched["repair_task_id"]
            ci_fence = Fence.model_validate(dispatched["fence"])
            async with db.immediate() as conn:
                await conn.execute(update(tasks).where(tasks.c.id == ci_task_id).values(status="IN_PROGRESS"))
                await conn.execute(update(workspaces).where(workspaces.c.id == "repair-work").values(locked_by_task_id=ci_task_id))
            await db.create_session(SessionRecord(id="ci-session", task_id=ci_task_id, project_id="p", profile_id="debugger", harness="fake", provider="fake", name="ci-session", lifecycle="task", state="running", work_dir=str(work), epoch="ci", instance_token="ci-token", started_at=1321.0))
            await ownership.attach(ci_fence, "ci-session", "repair-work", expected_role="repair")

        _git(work, "fetch", str(origin), built.branch)
        _git(work, "switch", "--detach", "FETCH_HEAD")
        (work / "fix.txt").write_text("fix failed unit check\n")
        _git(work, "add", "fix.txt")
        _git(work, "commit", "-m", "repair integration CI in place")
        repaired_sha = _git(work, "rev-parse", "HEAD")
        _git(work, "push", str(origin), f"HEAD:{built.branch}")
        proof = {"base_sha": built.head_sha, "head_sha": repaired_sha, "commits": [repaired_sha]}
        if continuous:
            closed = await repair_service.complete_delegate(ci_task_id, operation_id=operation_id,
                stage=1, session_id="ci-session", instance_token="ci-token", workspace_id="repair-work",
                fence_token=ci_fence.token, head_sha=repaired_sha, commit_proof=proof, now=1330.0)
            assert closed["outcome"] == "completed"
            await db.update_session("ci-session", state="stopped", desired_state="stopped")
            await ownership.transfer(ci_fence, operation_id, "collector")
        else:
            async with db.immediate() as conn:
                await RepairService(db).adopt_batch_repair_on(
                conn,
                operation_id,
                head_sha=repaired_sha,
                commit_proof=proof,
                now=1330.0,
            )
        assert (
            await _one(
                db,
                integration_candidate_revisions,
                integration_candidate_revisions.c.batch_id == batch_id,
                integration_candidate_revisions.c.revision == 0,
            )
        )["state"] == "superseded"
        rebuilt = await candidate.build(batch_id)
        assert rebuilt.revision == 1 and rebuilt.head_sha == repaired_sha
        assert rebuilt.branch == built.branch
        assert rebuilt.pr_url == built.pr_url
        assert len(forge.revisions) == 2

        receipt = CIReceiptPayload.model_validate(
            {
                "schema": "aq.integration-ci-receipt.v1",
                "canonical_repository_id": "repo",
                "repository_id": 9,
                "full_name": "example/repo",
                "producer_id": "404",
                "head_sha": repaired_sha,
                "required_check_set_version": "checks-v1",
                "checks": [
                    {
                        "name": "unit",
                        "check_run_id": 12,
                        "check_suite_id": 22,
                        "producer_app_id": 404,
                        "producer_id": "404",
                        "head_sha": repaired_sha,
                        "conclusion": "success",
                    }
                ],
                "workflow_runs": [
                    {
                        "workflow_run_id": 32,
                        "run_attempt": 1,
                        "check_suite_id": 22,
                        "head_sha": repaired_sha,
                        "conclusion": "success",
                    }
                ],
            }
        )
        green = await CIService(
            db,
            trust,
            TrustedFixtureObserver(TrustedCIObservation(receipt, {22: 302})),
            clock=lambda: 1340.0,
        ).observe_candidate(
            CandidateCISubject(
                operation_id=operation_id,
                batch_id=batch_id,
                revision=1,
                candidate_sha=repaired_sha,
            )
        )
        assert green["outcome"] == "green"
        assert (await _one(db, integration_batches, integration_batches.c.id == batch_id))[
            "tested_candidate_sha"
        ] == repaired_sha

        # Promotion validates the canonical GitHub repository identity. The
        # local Git transport still sends its exact OID to the fixture bare repo.
        await db.update_repo("repo", url="https://github.com/example/repo.git")

        promoted = await promoter.promote(batch_id, 1)
        assert promoted.outcome == "promoted"
        assert promoted.head_sha == repaired_sha
        assert _git(origin, "rev-parse", "refs/heads/main") == repaired_sha
        assert (
            await _one(
                db, task_delivery_receipts, task_delivery_receipts.c.source_task_id == "epic"
            )
        )["target_branch"] == "refs/heads/main"

        cleanup_git = CleanupGit(origin)
        cleanup_forge = CleanupForge(source, repaired_sha)
        for _task_id, _source_branch, head, number in extra_sources:
            cleanup_forge.prs[number] = {"repository_numeric_id": 9, "repository_full_name": "example/repo", "head_sha": head, "state": "open"}
        cleanup = IntegrationCleanupService(
            db,
            data_dir=tmp_path / "data",
            git_manager=cleanup_git,
            github_client_factory=lambda _binding: app,
            forge_provider=cleanup_forge,
            clock=lambda: 1360.0,
        )
        materialized = await cleanup.materialize(batch_id, now=1360.0)
        assert materialized.outcome == "materialized"
        async with db._engine.connect() as conn:
            items = (
                (
                    await conn.execute(
                        select(integration_cleanup_items).where(
                            integration_cleanup_items.c.batch_id == batch_id
                        )
                    )
                )
                .mappings()
                .all()
            )
            evidence = (
                (
                    await conn.execute(
                        select(integration_check_evidence).where(
                            integration_check_evidence.c.batch_id == batch_id
                        )
                    )
                )
                .mappings()
                .all()
            )
        assert any(row["conclusion"] == "failure" for row in evidence)
        assert any(
            row["conclusion"] == "success" and row["candidate_revision"] == 1 for row in evidence
        )
        assert {row["kind"] for row in items} == {
            "source_pr",
            "remote_ref",
            "audit_pr",
            "local_ref",
        }
        results = await cleanup.advance(batch_id, now=1360.0, limit=len(items))
        assert len(results) == len(items)
        assert {result.outcome for result in results} == {"complete"}
        assert (await _one(db, integration_batches, integration_batches.c.id == batch_id))[
            "cleanup_state"
        ] == "complete"
        assert sorted(cleanup_forge.closed) == ([1, 2, 3, 9] if continuous else [1, 9])
        assert len(cleanup_forge.comments) == (3 if continuous else 1)
        assert repaired_sha in cleanup_forge.comments[0]
        assert set(cleanup_git.deleted) == {
            branch,
            built.branch.removeprefix("refs/heads/"),
            "aq/child-0",
            "aq/child-1",
            "aq/child-2",
        } | {item[1] for item in extra_sources}
        assert _git(origin, "rev-parse", "refs/heads/main") == repaired_sha
        if continuous:
            release = await IntegrationReleaseService(db).release(batch_id, 1361.0)
            assert release.outcome == "released"
            async with db.immediate() as conn:
                await conn.execute(delete(task_labels).where(task_labels.c.task_id == "future"))
            # The next source already had its approval before the first seal.
            # No new review event re-arms the cleared settling window.
            due_again = await scheduler.mark_due("p", 1600.0, "periodic")
            assert due_again["outcome"] == "due"
            next_batch = await TrainService(db).seal("p", due_again["request_id"], 1601.0)
            assert next_batch["outcome"] == "sealed" and next_batch["batch_id"] != batch_id
            next_member = await _one(db, integration_batch_members, integration_batch_members.c.batch_id == next_batch["batch_id"])
            assert next_member["task_id"] == "future"
            next_candidate = CandidateService(db, data_dir=tmp_path / "data", git_manager=git,
                app_client=app, forge_provider=RevisingAuditForge(), clock=lambda: 1610.0)
            following = await next_candidate.build(next_batch["batch_id"])
            assert following.outcome == "built"
            _git(origin, "merge-base", "--is-ancestor", repaired_sha, following.head_sha)
            _git(origin, "merge-base", "--is-ancestor", future, following.head_sha)
    finally:
        await db.close()


def _green_receipt(head_sha: str) -> CIReceiptPayload:
    return CIReceiptPayload.model_validate(
        {
            "schema": "aq.integration-ci-receipt.v1",
            "canonical_repository_id": "repo",
            "repository_id": 9,
            "full_name": "example/repo",
            "producer_id": "404",
            "head_sha": head_sha,
            "required_check_set_version": "checks-v1",
            "checks": [
                {
                    "name": "unit",
                    "check_run_id": 12,
                    "check_suite_id": 22,
                    "producer_app_id": 404,
                    "producer_id": "404",
                    "head_sha": head_sha,
                    "conclusion": "success",
                }
            ],
            "workflow_runs": [
                {
                    "workflow_run_id": 32,
                    "run_attempt": 1,
                    "check_suite_id": 22,
                    "head_sha": head_sha,
                    "conclusion": "success",
                }
            ],
        }
    )




@pytest.fixture(autouse=True)
def reconciler_primitive_authority(monkeypatch):
    from tests.integration_primitive_scope import authorize_root_primitives

    authorize_root_primitives(monkeypatch)
