"""Atomic train release and independent normalized cleanup."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database import Database
from src.database.tables import (
    integration_attestation_publications,
    integration_batch_members,
    integration_batches,
    integration_candidate_member_results,
    integration_candidate_publications,
    integration_candidate_ref_mutations,
    integration_candidate_revisions,
    integration_check_evidence,
    integration_cleanup_items,
    integration_outbox,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    integration_review_evidence,
    integration_root_intent_members,
    project_integration_leases,
    project_integration_schedules,
    task_delivery_receipts,
    workspaces,
)
from src.integration.cleanup import IntegrationCleanupService
from src.integration.release import IntegrationReleaseService
from src.integration.scheduler import IntegrationScheduler
from src.git.github_app import GitHubRepositoryBinding
from src.models import Project, RepoConfig, RepoSourceType
from src.profiles.capabilities import CapabilityPolicy


BASE = "a" * 40
HEAD = "b" * 40
SOURCE = "c" * 40
TREE = "d" * 40
SQUASH = "e" * 40
BRANCH = "refs/heads/aq/integration/p-" + "1" * 32 + "/r-" + "2" * 32


@pytest.fixture
async def release_db(tmp_path, request):
    db = Database(str(tmp_path / "release.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="project"))
    await db.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url="https://github.com/acme/widgets.git",
            default_branch="main",
        )
    )
    await db.update_project(
        "p", hierarchical_integration_mode="train", integration_repository_id="repo"
    )
    scheduler = IntegrationScheduler(db)
    await scheduler.configure(project_id="p", now=0.0, enabled=True, interval_seconds=300)
    due_request = await scheduler.mark_due(project_id="p", now=10.0, trigger="manual")
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="batch",
                project_id="p",
                repository_id="repo",
                request_id=due_request["request_id"],
                trigger="manual",
                source_manifest_digest="sha256:" + "3" * 64,
                base_sha=BASE,
                lifecycle="sealing",
                current_revision=0,
                integration_branch=BRANCH,
                tested_candidate_sha=HEAD,
                ci_evidence_id="ci",
                final_main_sha=None,
                policy_snapshot={
                    "cleanup": {
                        "max_attempts": 2,
                        "retry_base_seconds": 5.0,
                        "retry_max_seconds": 5.0,
                    }
                },
                artifact_snapshot={},
                cleanup_state="pending",
                created_at=1.0,
                updated_at=20.0,
            )
        )
        await conn.execute(
            insert(integration_review_evidence).values(
                id="review",
                source_task_id="root",
                repository_id="repo",
                source_base=BASE,
                reviewed_head_sha=SOURCE,
                reviewed_tree_sha=TREE,
                reviewer_task_id="reviewer",
                review_kind="leaf",
                generation=1,
                verdict="approved",
                evidence={"approved": True},
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_batch_members).values(
                batch_id="batch",
                ordinal=0,
                task_id="root",
                pr_url="https://github.com/acme/widgets/pull/1",
                repository_id="repo",
                source_base_sha=BASE,
                reviewed_head_sha=SOURCE,
                reviewed_tree_sha=TREE,
                review_evidence_id="review",
                review_evidence={"approved": True},
            )
        )
        await conn.execute(
            insert(integration_candidate_revisions).values(
                batch_id="batch",
                revision=0,
                construction_base_sha=BASE,
                next_member_ordinal=1,
                head_sha=HEAD,
                ci_evidence_id="ci",
                state="promoted",
                created_at=2.0,
                updated_at=20.0,
            )
        )
        await conn.execute(
            insert(integration_candidate_member_results).values(
                batch_id="batch",
                revision=0,
                member_ordinal=0,
                input_head_sha=SOURCE,
                input_tree_sha=TREE,
                generated_squash_sha=SQUASH,
                result="applied",
                created_at=2.0,
                updated_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="op",
                target_kind="batch",
                batch_id="batch",
                episode_id="batch",
                active_stage=0,
                state="completed",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="checks-v1",
                created_at=1.0,
                updated_at=20.0,
            )
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id="op",
                ordinal=0,
                policy={},
                starting_sha=BASE,
                attempts=0,
                state="passed",
                completed_at=20.0,
            )
        )
        await conn.execute(
            insert(integration_check_evidence).values(
                id="ci",
                operation_id="op",
                batch_id="batch",
                candidate_revision=0,
                producer_id="404",
                workflow_id="aggregate:1",
                run_id="attestation",
                attempt=0,
                required_check_version="checks-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=3.0,
            )
        )
        await conn.execute(
            insert(integration_candidate_publications).values(
                batch_id="batch",
                revision=0,
                state="pr_published",
                repository_id="repo",
                repository_numeric_id=99,
                repository_full_name="acme/widgets",
                base_ref="main",
                head_ref=BRANCH.removeprefix("refs/heads/"),
                head_sha=HEAD,
                expected_old_sha="0" * 40,
                idempotency_key="publication",
                pr_number=9,
                pr_url="https://github.com/acme/widgets/pull/9",
                created_at=2.0,
                updated_at=2.0,
            )
        )
        publication_reserved = "unresolved_attestation" in request.node.name
        await conn.execute(
            insert(integration_attestation_publications).values(
                id="attestation-publication",
                project_id="p",
                batch_id="batch",
                revision=0,
                operation_id="op",
                head_sha=HEAD,
                ci_evidence_id="ci",
                external_id="aq-attestation-v1:" + "9" * 64,
                execution_nonce="nonce",
                state="reserved" if publication_reserved else "published",
                prewrite_at=None if publication_reserved else 4.0,
                check_run_id=None if publication_reserved else 7001,
                expires_at=1000.0,
                created_at=4.0,
                updated_at=4.0,
            )
        )
        await conn.execute(
            insert(integration_promotion_intents).values(
                id="intent",
                domain_key="root:batch:0",
                operation_key="op",
                project_id="p",
                receipt_id="receipt",
                source_head=HEAD,
                source_base=BASE,
                repository_id="repo",
                target_branch="refs/heads/main",
                expected_target=BASE,
                prepared_sha=HEAD,
                recovery_ref="refs/aq/root-promotions/intent",
                fence_owner_id="op",
                fence_token=7,
                state="committed",
                review_evidence={},
                authors=[],
                provenance={},
                commit_metadata={},
                remote_evidence={"kind": "authenticated_main", "remote_sha": HEAD},
                committed_at=20.0,
                created_at=3.0,
                updated_at=20.0,
                intent_kind="root",
                root_batch_id="batch",
                root_candidate_revision=0,
                project_lease_owner_id="lease-owner",
                project_lease_fence_token=3,
                branch_fence_owner_id="op",
                branch_fence_token=7,
                ci_evidence_id="ci",
            )
        )
        await conn.execute(
            insert(integration_root_intent_members).values(
                intent_id="intent",
                member_ordinal=0,
                receipt_id="receipt",
                batch_id="batch",
                candidate_revision=0,
                source_task_id="root",
                repository_id="repo",
                reviewed_head_sha=SOURCE,
                reviewed_tree_sha=TREE,
                generated_squash_sha=SQUASH,
                result_evidence={},
                review_evidence_id="review",
                created_at=3.0,
            )
        )
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="receipt",
                domain_key="root:batch:0:0",
                source_task_id="root",
                repository_id="repo",
                target_branch="refs/heads/main",
                reviewed_head_sha=SOURCE,
                reviewed_tree_sha=TREE,
                before_sha=BASE,
                squash_sha=SQUASH,
                after_sha=HEAD,
                review_evidence={"review_evidence_id": "review"},
                verification_evidence={"ci_evidence_id": "ci"},
                batch_id="batch",
                member_ordinal=0,
                candidate_revision=0,
                disposition="code",
                created_at=3.0,
            )
        )
        await conn.execute(
            insert(integration_candidate_ref_mutations).values(
                id="root-main-mutation",
                batch_id="batch",
                revision=0,
                purpose="root_main",
                repository_id="repo",
                branch="refs/heads/main",
                target_branch="refs/heads/main",
                expected_old_sha=BASE,
                desired_sha=HEAD,
                operation_id="op",
                operation_episode_id="batch",
                operation_stage=0,
                lease_owner_id="lease-owner",
                lease_fence_token=3,
                branch_owner_id="op",
                branch_owner_role="collector",
                branch_fence_token=7,
                nonce="mutation-nonce",
                state="applied",
                expires_at=1000.0,
                remote_sha=HEAD,
                prewrite_at=19.0,
                created_at=19.0,
                updated_at=20.0,
            )
        )
        await conn.execute(
            insert(project_integration_leases).values(
                project_id="p",
                repository_id="repo",
                batch_id="batch",
                owner_id="lease-owner",
                fence_token=3,
                heartbeat_at=1.0,
                expires_at=1000.0,
            )
        )
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "batch")
            .values(lifecycle="promoted", final_main_sha=HEAD, updated_at=20.0)
        )
    yield db, scheduler
    await db.close()


async def test_release_is_atomic_replayable_and_cleanup_independent(release_db):
    db, _scheduler = release_db
    first, second = await asyncio.gather(
        IntegrationReleaseService(db).release("batch", 30.0),
        IntegrationReleaseService(db).release("batch", 30.0),
    )
    assert {first.outcome, second.outcome} == {"released", "already_released"}
    assert first.operation_id == second.operation_id == "op"
    assert first.catchup_request_id is second.catchup_request_id is None
    async with db._engine.connect() as conn:
        assert (
            await conn.execute(select(project_integration_leases.c.batch_id))
        ).scalar_one_or_none() is None
        schedule = (
            await conn.execute(select(project_integration_schedules))
        ).mappings().one()
        batch = (await conn.execute(select(integration_batches))).mappings().one()
    assert schedule["outstanding_request_id"] is None
    assert batch["cleanup_state"] == "pending"


async def test_release_atomically_promotes_first_catchup_once(release_db):
    db, scheduler = release_db
    original = await scheduler.mark_due(project_id="p", now=21.0, trigger="manual")
    periodic = await scheduler.mark_due(project_id="p", now=600.0, trigger="periodic")
    assert original["request_id"] == periodic["request_id"]

    result = await IntegrationReleaseService(db).release("batch", 601.0)
    assert result.outcome == "released"
    assert result.request_id == original["request_id"]
    assert result.catchup_request_id == "integration-sweep:p:2"
    replay = await IntegrationReleaseService(db).release("batch", 602.0)
    assert replay.outcome == "already_released"
    assert replay.catchup_request_id == result.catchup_request_id
    async with db._engine.connect() as conn:
        schedule = (
            await conn.execute(select(project_integration_schedules))
        ).mappings().one()
        events = (
            await conn.execute(
                select(integration_outbox).where(
                    integration_outbox.c.event_type == "integration.sweep_due"
                )
            )
        ).mappings().all()
    assert schedule["outstanding_request_id"] == result.catchup_request_id
    assert schedule["outstanding_trigger"] == "manual"
    assert schedule["outstanding_requested_at"] == 21.0
    assert schedule["request_sequence"] == 2
    assert schedule["catchup_trigger"] is None
    assert len(events) == 2


async def test_release_waits_for_unresolved_attestation_publication(release_db):
    db, _scheduler = release_db
    result = await IntegrationReleaseService(db).release("batch", 30.0)
    assert result.outcome == "wait"
    async with db._engine.connect() as conn:
        assert (
            await conn.execute(select(project_integration_leases.c.batch_id))
        ).scalar_one() == "batch"


async def test_cleanup_materializes_normalized_terminal_set_idempotently(release_db):
    db, _scheduler = release_db
    service = IntegrationCleanupService(db, data_dir="/daemon")
    first = await service.materialize("batch", now=30.0)
    second = await service.materialize("batch", now=31.0)
    assert first == second
    assert first.outcome == "materialized"
    assert first.item_count == 4
    async with db._engine.connect() as conn:
        rows = (
            await conn.execute(
                select(integration_cleanup_items).order_by(
                    integration_cleanup_items.c.kind,
                    integration_cleanup_items.c.identity,
                )
            )
        ).mappings().all()
    assert [row["kind"] for row in rows] == [
        "audit_pr",
        "local_ref",
        "remote_ref",
        "source_pr",
    ]
    assert all(row["project_id"] == "p" for row in rows)
    assert all(row["repository_numeric_id"] == 99 for row in rows)
    assert all(row["repository_full_name"] == "acme/widgets" for row in rows)


class CleanupApp:
    def __init__(self, refs=None):
        self.repository = GitHubRepositoryBinding(99, "acme/widgets")
        self.refs = dict(refs or {})

    async def exact_head_ref(self, branch):
        return self.refs.get(branch)

    async def installation_token(self):
        return "installation-token"


class CleanupGit:
    def __init__(self, app):
        self.app = app
        self.remote_deletes = []
        self.local_refs = {BRANCH: HEAD}

    async def adelete_ref_with_app_auth(self, store, **kwargs):
        self.remote_deletes.append((store, kwargs))
        branch = kwargs["branch"]
        assert self.app.refs[branch] == kwargs["expected_old_oid"]
        self.app.refs.pop(branch)

    async def arev_parse(self, _store, ref):
        return self.local_refs.get(ref)

    async def adelete_local_ref_exact(self, _store, *, ref, expected_old_oid):
        assert self.local_refs[ref] == expected_old_oid
        self.local_refs.pop(ref)


class CleanupForge:
    def __init__(self):
        self.prs = {
            1: {"repository_numeric_id": 99, "repository_full_name": "acme/widgets",
                "head_sha": SOURCE, "state": "open"},
            9: {"repository_numeric_id": 99, "repository_full_name": "acme/widgets",
                "head_sha": HEAD, "state": "open"},
        }
        self.markers = set()
        self.comments = []
        self.closed = []

    async def exact_pull_request(self, *, number):
        return self.prs.get(number)

    async def has_comment_marker(self, *, number, marker):
        return (number, marker) in self.markers

    async def comment_pull_request(self, *, number, marker, body):
        self.comments.append((number, marker, body))
        self.markers.add((number, marker))

    async def close_pull_request(self, *, number):
        self.closed.append(number)
        self.prs[number]["state"] = "closed"


async def test_cleanup_executes_exact_refs_and_prs_once(release_db):
    db, _scheduler = release_db
    app = CleanupApp({BRANCH.removeprefix("refs/heads/"): HEAD})
    git = CleanupGit(app)
    forge = CleanupForge()
    service = IntegrationCleanupService(
        db,
        data_dir="/daemon",
        git_manager=git,
        app_client_factory=lambda _binding: app,
        forge_provider=forge,
        clock=lambda: 30.0,
    )
    await service.materialize("batch", now=30.0)
    results = await service.advance("batch", now=30.0, limit=10)
    assert {result.outcome for result in results} == {"complete"}
    assert len(git.remote_deletes) == 1
    _, remote = git.remote_deletes[0]
    assert remote == {
        "repository": app.repository,
        "token": "installation-token",
        "branch": BRANCH.removeprefix("refs/heads/"),
        "expected_old_oid": HEAD,
    }
    assert git.local_refs == {}
    assert sorted(forge.closed) == [1, 9]
    assert len(forge.comments) == 1

    replay = await service.advance("batch", now=31.0, limit=10)
    assert replay == []
    assert len(git.remote_deletes) == 1
    assert len(forge.comments) == 1
    async with db._engine.connect() as conn:
        batch = (await conn.execute(select(integration_batches))).mappings().one()
    assert batch["cleanup_state"] == "complete"


async def test_cleanup_advance_selects_the_requested_batch_not_the_global_first_page(
    release_db,
):
    db, _scheduler = release_db
    service = IntegrationCleanupService(db, data_dir="/daemon")
    await service.materialize("batch", now=30.0)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="earlier-batch",
                project_id="p",
                repository_id="repo",
                request_id="earlier-request",
                source_manifest_digest="sha256:" + "4" * 64,
                base_sha=BASE,
                lifecycle="promoted",
                current_revision=0,
                integration_branch="refs/heads/aq/integration/earlier",
                final_main_sha=HEAD,
                policy_snapshot={},
                artifact_snapshot={},
                cleanup_state="pending",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        template = (
            await conn.execute(
                select(integration_cleanup_items).where(
                    integration_cleanup_items.c.batch_id == "batch"
                )
            )
        ).mappings().first()
        earlier = dict(template)
        earlier.update(
            batch_id="earlier-batch",
            identity="earlier",
            domain_key="cleanup:earlier-batch:audit_pr:earlier",
            next_attempt_at=29.0,
        )
        await conn.execute(insert(integration_cleanup_items).values(**earlier))

    class Complete(IntegrationCleanupService):
        async def _perform(self, row):
            return "complete", None

    results = await Complete(db, data_dir="/daemon").advance(
        "batch", now=30.0, limit=1
    )

    assert len(results) == 1
    assert results[0].batch_id == "batch"


async def test_cleanup_preserves_moved_ref_and_source_pr_with_undelivered_head(release_db):
    db, _scheduler = release_db
    moved = "f" * 40
    app = CleanupApp({BRANCH.removeprefix("refs/heads/"): moved})
    git = CleanupGit(app)
    forge = CleanupForge()
    forge.prs[1]["head_sha"] = moved
    service = IntegrationCleanupService(
        db,
        data_dir="/daemon",
        git_manager=git,
        app_client_factory=lambda _binding: app,
        forge_provider=forge,
    )
    await service.materialize("batch", now=30.0)
    results = await service.advance("batch", now=30.0, limit=10)
    outcomes = {(row.kind, row.outcome) for row in results}
    assert ("remote_ref", "conflict") in outcomes
    assert ("source_pr", "conflict") in outcomes
    assert 1 not in forge.closed
    assert git.remote_deletes == []
    async with db._engine.connect() as conn:
        batch = (await conn.execute(select(integration_batches))).mappings().one()
    assert batch["cleanup_state"] == "conflict"


async def test_integration_cleanup_command_is_subject_only_and_project_scoped(
    command_handler_factory,
):
    handler = await command_handler_factory()
    await handler.db.create_project(Project(id="p", name="project"))
    await handler.db.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url="https://github.com/acme/widgets.git",
            default_branch="main",
        )
    )
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="batch-command",
                project_id="p",
                repository_id="repo",
                request_id="request",
                trigger="manual",
                source_manifest_digest="sha256:" + "1" * 64,
                base_sha=BASE,
                lifecycle="promoted",
                current_revision=0,
                integration_branch=BRANCH,
                final_main_sha=HEAD,
                policy_snapshot={},
                artifact_snapshot={},
                cleanup_state="pending",
                created_at=1.0,
                updated_at=1.0,
            )
        )
    service = AsyncMock()
    service.materialize.return_value = type(
        "Materialized",
        (),
        {"outcome": "materialized", "item_count": 4},
    )()
    service.advance.return_value = []
    handler.orchestrator.integration_cleanup_service = service

    session = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["integration_cleanup"]),
        project_id="p",
        session_id="session",
    )
    with principal_context(session):
        denied = await handler.execute("integration_cleanup", {"batch_id": "batch-command"})
    assert denied["outcome"] == "unauthorized"

    capable = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["integration_cleanup"]),
        project_id="p",
    )
    with principal_context(capable):
        invalid = await handler.execute(
            "integration_cleanup", {"batch_id": "batch-command", "target_ref": BRANCH}
        )
        result = await handler.execute(
            "integration_cleanup", {"batch_id": "batch-command"}
        )
    assert invalid["outcome"] == "runtime_error"
    assert result == {
        "success": False,
        "outcome": "wait",
        "batch_id": "batch-command",
        "item_count": 4,
        "completed_count": 0,
        "conflict_count": 0,
    }
    service.materialize.assert_awaited_once_with("batch-command")
    service.advance.assert_awaited_once_with("batch-command")
    await handler.db.close()


async def test_cleanup_expired_takeover_fences_out_old_executor(release_db):
    db, _scheduler = release_db
    await IntegrationCleanupService(db, data_dir="/daemon").materialize("batch", now=30.0)
    started = asyncio.Event()
    finish = asyncio.Event()

    class Slow(IntegrationCleanupService):
        async def _perform(self, row):
            started.set()
            await finish.wait()
            return "conflict", "obsolete executor"

    class Success(IntegrationCleanupService):
        async def _perform(self, row):
            return "complete", None

    old = Slow(db, data_dir="/daemon")
    successor = Success(db, data_dir="/daemon")
    task = asyncio.create_task(old.execute("batch", "source_pr", "99#1", now=30.0))
    await started.wait()
    accepted = await successor.execute("batch", "source_pr", "99#1", now=400.0)
    finish.set()
    stale = await task

    assert accepted.outcome == "complete"
    assert stale.outcome == "already_complete"
    async with db._engine.connect() as conn:
        row = (
            await conn.execute(
                select(integration_cleanup_items).where(
                    integration_cleanup_items.c.batch_id == "batch",
                    integration_cleanup_items.c.kind == "source_pr",
                )
            )
        ).mappings().one()
    assert row["state"] == "complete"
    assert row["attempts"] == 2


async def test_cleanup_retry_backoff_and_exhaustion_are_frozen(release_db):
    db, _scheduler = release_db
    await IntegrationCleanupService(db, data_dir="/daemon").materialize("batch", now=30.0)

    class Retry(IntegrationCleanupService):
        async def _perform(self, row):
            return "retryable", "provider unavailable"

    service = Retry(db, data_dir="/daemon")
    first = await service.execute("batch", "audit_pr", "99#9", now=30.0)
    early = await service.execute("batch", "audit_pr", "99#9", now=34.0)
    exhausted = await service.execute("batch", "audit_pr", "99#9", now=35.0)

    assert (first.outcome, first.attempts) == ("retryable", 1)
    assert (early.outcome, early.attempts) == ("wait", 1)
    assert (exhausted.outcome, exhausted.attempts) == ("failed", 2)


async def test_cleanup_never_deletes_default_branch_even_with_matching_sha(release_db):
    db, _scheduler = release_db
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_cleanup_items).values(
                batch_id="batch", kind="remote_ref", identity="refs/heads/main",
                domain_key="cleanup:batch:remote_ref:main", project_id="p",
                repository_id="repo", repository_numeric_id=99,
                repository_full_name="acme/widgets", revision=0,
                target_ref="refs/heads/main", expected_sha=HEAD, state="pending",
                attempts=0, next_attempt_at=30.0, created_at=30.0, updated_at=30.0,
            )
        )
    app = CleanupApp({"main": HEAD})
    git = CleanupGit(app)
    result = await IntegrationCleanupService(
        db, data_dir="/daemon", git_manager=git, app_client_factory=lambda _: app
    ).execute("batch", "remote_ref", "refs/heads/main", now=30.0)
    assert result.outcome == "conflict"
    assert git.remote_deletes == []


@pytest.mark.parametrize(
    ("observed_base", "expected_outcome", "removed"),
    [
        ("/daemon/base", "complete", [("/daemon/base", "/daemon/retained")]),
        ("/foreign/base", "conflict", []),
    ],
)
async def test_cleanup_removes_only_recorded_owned_worktree(
    release_db, observed_base, expected_outcome, removed
):
    db, _scheduler = release_db
    async with db.immediate() as conn:
        await conn.execute(
            insert(workspaces).values(
                id="base-workspace",
                project_id="p",
                workspace_path="/daemon/base",
                source_type="clone",
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(workspaces).values(
                id="retained-workspace",
                project_id="p",
                workspace_path="/daemon/retained",
                source_type="worktree",
                base_workspace_id="base-workspace",
                created_at=1.0,
            )
        )
        await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.operation_id == "op",
                integration_repair_stages.c.ordinal == 0,
            )
            .values(
                retained_workspace_id="retained-workspace",
                retained_handoff={
                    "workspace_id": "retained-workspace",
                    "operation_id": "op",
                    "head_sha": HEAD,
                },
            )
        )
    await IntegrationCleanupService(db, data_dir="/daemon").materialize("batch", now=30.0)

    class WorktreeGit:
        def __init__(self):
            self.removed = []

        async def arev_parse(self, path, ref):
            assert (path, ref) == ("/daemon/retained", "HEAD")
            return HEAD

        async def aworktree_base_path(self, path):
            assert path == "/daemon/retained"
            return observed_base

        async def aremove_worktree_exact(self, base, path):
            self.removed.append((base, path))

    git = WorktreeGit()
    result = await IntegrationCleanupService(
        db, data_dir="/daemon", git_manager=git
    ).execute("batch", "worktree", "retained-workspace", now=30.0)

    async with db._engine.connect() as conn:
        stored = (
            await conn.execute(
                select(integration_cleanup_items).where(
                    integration_cleanup_items.c.batch_id == "batch",
                    integration_cleanup_items.c.kind == "worktree",
                )
            )
        ).mappings().one()
    assert result.outcome == expected_outcome, stored["last_error"]
    assert git.removed == removed
