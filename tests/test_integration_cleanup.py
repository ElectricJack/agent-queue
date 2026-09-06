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
    integration_branch_owners,
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
from src.integration.cleanup import CleanupExecutionResult, IntegrationCleanupService
from src.integration.release import IntegrationReleaseService
from src.integration.scheduler import IntegrationScheduler
from src.git.github_app import GitHubRepositoryBinding
from src.models import Project, RepoConfig, RepoSourceType
from src.profiles.capabilities import CapabilityPolicy
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn


BASE = "a" * 40
HEAD = "b" * 40
SOURCE = "c" * 40
TREE = "d" * 40
SQUASH = "e" * 40
BRANCH = "refs/heads/aq/integration/p-" + "1" * 32 + "/r-" + "2" * 32
POSTGRES_DSN = ensure_worker_postgres_dsn()


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
                        "successful_source_refs": (
                            "retain" if "retains_source_ref" in request.node.name else "delete"
                        ),
                        "failed_work_retention_seconds": 604800,
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
                source_ref=(
                    None
                    if "legacy_source_identity" in request.node.name
                    else "refs/heads/aq/root"
                ),
                source_ref_retention=(
                    None
                    if "legacy_source_identity" in request.node.name
                    else (
                        "retain" if "retains_source_ref" in request.node.name else "delete"
                    )
                ),
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


async def test_release_replay_is_immutable_after_a_later_train_acquires_lease(
    release_db,
):
    db, scheduler = release_db
    await scheduler.mark_due(project_id="p", now=21.0, trigger="manual")
    released = await IntegrationReleaseService(db).release("batch", 30.0)
    assert released.outcome == "released"
    assert released.catchup_request_id == "integration-sweep:p:2"
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="later-batch",
                project_id="p",
                repository_id="repo",
                request_id=released.catchup_request_id,
                source_manifest_digest="sha256:" + "8" * 64,
                base_sha=HEAD,
                lifecycle="sealed",
                current_revision=0,
                integration_branch="refs/heads/aq/integration/later",
                policy_snapshot={},
                artifact_snapshot={},
                cleanup_state="pending",
                created_at=31.0,
                updated_at=31.0,
            )
        )
        await conn.execute(
            insert(project_integration_leases).values(
                project_id="p",
                repository_id="repo",
                batch_id="later-batch",
                owner_id="later-owner",
                fence_token=4,
                heartbeat_at=31.0,
                expires_at=1000.0,
            )
        )

    replay = await IntegrationReleaseService(db).release("batch", 40.0)

    assert replay == released.model_copy(update={"outcome": "already_released"})


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
    assert first.item_count == 5
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
        "remote_ref",
        "source_pr",
    ]
    assert {row["target_ref"] for row in rows if row["kind"] == "remote_ref"} == {
        BRANCH,
        "refs/heads/aq/root",
    }
    assert all(row["project_id"] == "p" for row in rows)
    assert all(row["repository_numeric_id"] == 99 for row in rows)
    assert all(row["repository_full_name"] == "acme/widgets" for row in rows)


async def test_cleanup_retains_source_ref_from_frozen_policy(release_db):
    db, _scheduler = release_db
    result = await IntegrationCleanupService(db, data_dir="/daemon").materialize(
        "batch", now=30.0
    )
    async with db._engine.connect() as conn:
        refs = (
            await conn.execute(
                select(integration_cleanup_items.c.target_ref).where(
                    integration_cleanup_items.c.batch_id == "batch",
                    integration_cleanup_items.c.kind == "remote_ref",
                )
            )
        ).scalars().all()
    assert result.item_count == 4
    assert refs == [BRANCH]


async def test_cleanup_legacy_source_identity_is_visible_conflict(release_db):
    db, _scheduler = release_db
    result = await IntegrationCleanupService(db, data_dir="/daemon").materialize(
        "batch", now=30.0
    )
    async with db._engine.connect() as conn:
        batch = (
            await conn.execute(
                select(integration_batches).where(integration_batches.c.id == "batch")
            )
        ).mappings().one()
    assert result.outcome == "conflict"
    assert result.item_count == 0
    assert batch["cleanup_state"] == "conflict"


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

    async def aworktree_list(self, _store):
        return []


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
    app = CleanupApp(
        {
            BRANCH.removeprefix("refs/heads/"): HEAD,
            "aq/root": SOURCE,
        }
    )
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
    assert {remote[1]["branch"]: remote[1]["expected_old_oid"] for remote in git.remote_deletes} == {
        BRANCH.removeprefix("refs/heads/"): HEAD,
        "aq/root": SOURCE,
    }
    assert all(remote[1]["repository"] == app.repository for remote in git.remote_deletes)
    assert all(remote[1]["token"] == "installation-token" for remote in git.remote_deletes)
    assert git.local_refs == {}
    assert sorted(forge.closed) == [1, 9]
    assert len(forge.comments) == 1

    replay = await service.advance("batch", now=31.0, limit=10)
    assert replay == []
    assert len(git.remote_deletes) == 2
    assert len(forge.comments) == 1
    async with db._engine.connect() as conn:
        batch = (await conn.execute(select(integration_batches))).mappings().one()
    assert batch["cleanup_state"] == "complete"


async def test_cleanup_never_reposts_after_ambiguous_pr_comment_prewrite(release_db):
    db, _scheduler = release_db
    forge = CleanupForge()

    async def ambiguous_comment(*, number, marker, body):
        forge.comments.append((number, marker, body))
        raise OSError("provider response lost before marker became readable")

    forge.comment_pull_request = ambiguous_comment
    service = IntegrationCleanupService(
        db,
        data_dir="/daemon",
        forge_provider=forge,
        clock=lambda: 30.0,
    )
    await service.materialize("batch", now=30.0)

    first = await service.execute("batch", "source_pr", "99#1", now=30.0)
    second = await service.execute("batch", "source_pr", "99#1", now=400.0)

    assert first.outcome == "retryable"
    assert second.outcome in {"retryable", "failed"}
    assert len(forge.comments) == 1


async def test_expired_cleanup_claim_cannot_post_after_successor_prewrite(release_db):
    db, _scheduler = release_db
    forge = CleanupForge()
    first_lookup = asyncio.Event()
    release_first = asyncio.Event()
    lookups = 0

    async def paused_marker_lookup(*, number, marker):
        nonlocal lookups
        lookups += 1
        if lookups == 1:
            first_lookup.set()
            await release_first.wait()
        return False

    forge.has_comment_marker = paused_marker_lookup
    await IntegrationCleanupService(db, data_dir="/daemon").materialize(
        "batch", now=30.0
    )
    old = IntegrationCleanupService(
        db, data_dir="/daemon", forge_provider=forge, clock=lambda: 30.0
    )
    successor = IntegrationCleanupService(
        db, data_dir="/daemon", forge_provider=forge, clock=lambda: 400.0
    )
    old_task = asyncio.create_task(
        old.execute("batch", "source_pr", "99#1", now=30.0)
    )
    await asyncio.wait_for(first_lookup.wait(), timeout=1.0)
    accepted = await successor.execute(
        "batch", "source_pr", "99#1", now=400.0
    )
    release_first.set()
    stale = await old_task

    assert accepted.outcome == "complete"
    assert stale.outcome == "already_complete"
    assert len(forge.comments) == 1


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
        "outcome": "invariant_error",
        "batch_id": "batch-command",
        "item_count": 4,
        "completed_count": 0,
        "conflict_count": 0,
    }
    service.materialize.assert_awaited_once_with("batch-command")
    service.advance.assert_awaited_once_with("batch-command")
    await handler.db.close()


async def test_cleanup_command_never_reports_complete_for_a_partial_page(
    command_handler_factory,
):
    handler = await command_handler_factory()
    await handler.db.create_project(Project(id="p", name="project"))
    await handler.db.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.CLONE)
    )
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="paged-batch",
                project_id="p",
                repository_id="repo",
                request_id="paged-request",
                source_manifest_digest="sha256:" + "9" * 64,
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
        for index in range(101):
            state = "complete" if index < 100 else "pending"
            await conn.execute(
                insert(integration_cleanup_items).values(
                    batch_id="paged-batch",
                    kind="local_ref",
                    identity=f"refs/heads/cleanup-{index}",
                    domain_key=f"cleanup:paged:{index}",
                    project_id="p",
                    repository_id="repo",
                    repository_numeric_id=99,
                    repository_full_name="acme/widgets",
                    revision=0,
                    target_ref=f"refs/heads/cleanup-{index}",
                    expected_sha=HEAD,
                    state=state,
                    attempts=1 if state == "complete" else 0,
                    next_attempt_at=1.0,
                    created_at=1.0,
                    updated_at=1.0,
                    terminal_at=1.0 if state == "complete" else None,
                )
            )
    service = AsyncMock()
    service.materialize.return_value = type(
        "Materialized", (), {"outcome": "already_materialized", "item_count": 101}
    )()
    service.advance.return_value = [
        CleanupExecutionResult(
            outcome="complete",
            batch_id="paged-batch",
            kind="local_ref",
            identity=f"refs/heads/cleanup-{index}",
            attempts=1,
        )
        for index in range(100)
    ]
    handler.orchestrator.integration_cleanup_service = service
    principal = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["integration_cleanup"]),
        project_id="p",
    )

    with principal_context(principal):
        result = await handler.execute(
            "integration_cleanup", {"batch_id": "paged-batch"}
        )

    assert result["outcome"] == "advanced"
    assert result["completed_count"] == 100
    assert result["conflict_count"] == 0
    await handler.db.close()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_postgres_last_cleanup_items_serialize_aggregate_projection():
    import asyncpg

    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

    dsn = await create_scratch_database("task10c_cleanup_finalize")
    db = PostgreSQLDatabaseAdapter(dsn, 0, 2)
    await db.initialize()
    try:
        await db.create_project(Project(id="p", name="project"))
        await db.create_repo(
            RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.CLONE)
        )
        async with db.immediate() as conn:
            await conn.execute(
                insert(integration_batches).values(
                    id="batch",
                    project_id="p",
                    repository_id="repo",
                    request_id="request",
                    source_manifest_digest="sha256:" + "7" * 64,
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
            for identity, nonce in (("one", "nonce-one"), ("two", "nonce-two")):
                await conn.execute(
                    insert(integration_cleanup_items).values(
                        batch_id="batch",
                        kind="local_ref",
                        identity=identity,
                        domain_key=f"cleanup:batch:{identity}",
                        project_id="p",
                        repository_id="repo",
                        repository_numeric_id=99,
                        repository_full_name="acme/widgets",
                        revision=0,
                        target_ref=f"refs/heads/{identity}",
                        expected_sha=HEAD,
                        state="pending",
                        attempts=1,
                        next_attempt_at=1.0,
                        execution_nonce=nonce,
                        claim_expires_at=300.0,
                        created_at=1.0,
                        updated_at=1.0,
                    )
                )

        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release_first = asyncio.Event()

        class PausingProjection(IntegrationCleanupService):
            entered = 0

            async def _project_aggregate_on(self, conn, batch_id, now):
                type(self).entered += 1
                if type(self).entered == 1:
                    first_entered.set()
                    await release_first.wait()
                else:
                    second_entered.set()
                await super()._project_aggregate_on(conn, batch_id, now)

        service = PausingProjection(db, data_dir="/daemon")
        rows = {}
        async with db._engine.connect() as conn:
            for row in (
                await conn.execute(select(integration_cleanup_items))
            ).mappings().all():
                rows[row["identity"]] = dict(row)
        first = asyncio.create_task(
            service._finalize(rows["one"], "nonce-one", 10.0, "complete", None)
        )
        await asyncio.wait_for(first_entered.wait(), timeout=2.0)
        second = asyncio.create_task(
            service._finalize(rows["two"], "nonce-two", 10.0, "complete", None)
        )
        try:
            await asyncio.wait_for(second_entered.wait(), timeout=0.25)
            serialized = False
        except TimeoutError:
            serialized = True
        finally:
            release_first.set()
        await asyncio.gather(first, second)
        assert serialized is True
        async with db._engine.connect() as conn:
            cleanup_state = await conn.scalar(
                select(integration_batches.c.cleanup_state).where(
                    integration_batches.c.id == "batch"
                )
            )
        assert cleanup_state == "complete"
    finally:
        await db.close()
        prefix, _, name = dsn.rpartition("/")
        admin = await asyncpg.connect(
            prefix.replace("postgresql+asyncpg://", "postgresql://") + "/postgres"
        )
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()


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
                member_ordinal=0,
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


@pytest.mark.parametrize("protection", ["moved", "foreign_owner"])
async def test_cleanup_source_ref_requires_frozen_head_and_no_foreign_owner(
    release_db, protection
):
    db, _scheduler = release_db
    await IntegrationCleanupService(db, data_dir="/daemon").materialize(
        "batch", now=30.0
    )
    if protection == "foreign_owner":
        async with db.immediate() as conn:
            await conn.execute(
                insert(integration_branch_owners).values(
                    id="foreign-source-owner",
                    repository_id="repo",
                    ref="refs/heads/aq/root",
                    owner_id="unrelated-operation",
                    owner_role="collector",
                    fence_token=1,
                    handoff_state="reserved",
                    created_at=20.0,
                    updated_at=20.0,
                )
            )
    app = CleanupApp({"aq/root": "f" * 40 if protection == "moved" else SOURCE})
    git = CleanupGit(app)

    result = await IntegrationCleanupService(
        db, data_dir="/daemon", git_manager=git, app_client_factory=lambda _: app
    ).execute("batch", "remote_ref", "refs/heads/aq/root", now=30.0)

    assert result.outcome == "conflict"
    assert git.remote_deletes == []


async def test_cleanup_delays_failed_retained_work_by_frozen_window(release_db):
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
                id="failed-workspace",
                project_id="p",
                workspace_path="/daemon/failed",
                source_type="worktree",
                base_workspace_id="base-workspace",
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id="op",
                ordinal=1,
                policy={},
                starting_sha=HEAD,
                attempts=1,
                state="failed",
                completed_at=25.0,
                retained_workspace_id="failed-workspace",
                retained_handoff={
                    "workspace_id": "failed-workspace",
                    "operation_id": "op",
                    "head_sha": HEAD,
                },
            )
        )

    result = await IntegrationCleanupService(db, data_dir="/daemon").materialize(
        "batch", now=30.0
    )
    async with db._engine.connect() as conn:
        item = (
            await conn.execute(
                select(integration_cleanup_items).where(
                    integration_cleanup_items.c.batch_id == "batch",
                    integration_cleanup_items.c.kind == "worktree",
                )
            )
        ).mappings().one()

    assert result.outcome == "materialized"
    assert item["next_attempt_at"] == 25.0 + 604800


@pytest.mark.parametrize("protection", ["checked_out", "foreign_owner"])
async def test_cleanup_local_ref_requires_vacancy_and_recorded_owner(
    release_db, protection
):
    db, _scheduler = release_db
    await IntegrationCleanupService(db, data_dir="/daemon").materialize(
        "batch", now=30.0
    )
    if protection == "foreign_owner":
        async with db.immediate() as conn:
            await conn.execute(
                insert(integration_branch_owners).values(
                    id="foreign-owner",
                    repository_id="repo",
                    ref=BRANCH,
                    owner_id="foreign-operation",
                    owner_role="collector",
                    fence_token=99,
                    handoff_state="reserved",
                    created_at=20.0,
                    updated_at=20.0,
                )
            )

    class ProtectedGit(CleanupGit):
        async def aworktree_list(self, _store):
            return (
                [{"path": "/foreign/worktree", "branch": BRANCH.removeprefix("refs/heads/")}]
                if protection == "checked_out"
                else []
            )

    app = CleanupApp()
    git = ProtectedGit(app)
    result = await IntegrationCleanupService(
        db, data_dir="/daemon", git_manager=git
    ).execute("batch", "local_ref", BRANCH, now=30.0)

    assert result.outcome == "conflict"
    assert git.local_refs == {BRANCH: HEAD}


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


async def test_cleanup_reconciles_worktree_absent_after_remove_crash(release_db):
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
    await IntegrationCleanupService(db, data_dir="/daemon").materialize(
        "batch", now=30.0
    )

    class CrashAfterRemove:
        present = True
        removes = 0

        async def arev_parse(self, path, ref):
            assert (path, ref) == ("/daemon/retained", "HEAD")
            return HEAD if self.present else None

        async def aworktree_base_path(self, path):
            return "/daemon/base" if self.present else None

        async def aremove_worktree_exact(self, base, path):
            self.removes += 1
            self.present = False
            raise OSError("daemon crashed after git removed the worktree")

    git = CrashAfterRemove()
    service = IntegrationCleanupService(
        db, data_dir="/daemon", git_manager=git, clock=lambda: 30.0
    )
    first = await service.execute(
        "batch", "worktree", "retained-workspace", now=30.0
    )
    second = await service.execute(
        "batch", "worktree", "retained-workspace", now=400.0
    )

    assert first.outcome == "retryable"
    assert second.outcome == "complete"
    assert git.removes == 1
