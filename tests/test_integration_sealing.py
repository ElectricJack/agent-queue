"""Atomic full-frontier integration-train sealing."""

from __future__ import annotations

import asyncio
import re
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    gates,
    integration_batch_members,
    integration_batches,
    integration_operation_artifact_pins,
    integration_outbox,
    integration_parent_episodes,
    integration_parent_operation_completions,
    integration_parent_verifications,
    integration_repair_operations,
    integration_repair_stages,
    integration_review_evidence,
    messages,
    playbook_artifacts,
    project_integration_leases,
    project_integration_schedules,
    projects,
    repos,
    task_branch_origins,
    task_completion_records,
    task_delivery_receipts,
    task_gates,
    task_integration_checkpoints,
    task_labels,
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
from src.models import Project, RepoConfig, RepoSourceType, TaskStatus
from src.profiles.capabilities import CapabilityPolicy
from tests.pg_dsn import ensure_worker_postgres_dsn
from tests.pg_trigger_helpers import injected_trigger
from src.integration.migration_heads import MigrationHead, MigrationInspector, declaration, select_members

BASE_SHA = "a" * 40
POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()


@pytest.mark.parametrize(
    "source, parents",
    [
        ("revision = '44'\ndown_revision = '43'", ("43",)),
        ("revision: str = '44'\ndown_revision: tuple = ('42', '43')", ("42", "43")),
        ("revision = '44'\ndown_revision = None", ()),
    ],
)
def test_migration_declaration_reads_literals_without_executing(source, parents):
    head = declaration("migrations/versions/new.py", source + "\nraise RuntimeError('never run')")
    assert head.revision == "44"
    assert head.down_revisions == parents


def test_dynamic_migration_revision_is_not_executed():
    with pytest.raises(ValueError):
        declaration("migration.py", "revision = dangerous()\ndown_revision = '43'")


async def test_migration_inspector_reads_only_added_files_at_the_reviewed_head(tmp_path):
    from src.git.manager import GitManager
    from tests.test_integration_candidates import _git, _make_origin

    origin, work, base, _members = _make_origin(tmp_path)
    _git(work, "switch", "-C", "migration", base)
    directory = work / "migrations" / "versions"
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text("")
    (directory / "44.py").write_text(
        "revision: str = '44'\ndown_revision = '43'\nraise RuntimeError('never execute')\n"
    )
    _git(work, "add", ".")
    _git(work, "commit", "-m", "migration")
    reviewed = _git(work, "rev-parse", "HEAD")
    # Moving the local branch after review must not change inspected metadata.
    (directory / "44.py").write_text("revision = '999'\ndown_revision = '998'\n")
    _git(work, "commit", "-am", "after review")
    git = GitManager()
    git.bind_github_repository = AsyncMock(return_value=SimpleNamespace())
    git.afetch_repository_oid = AsyncMock()
    promotion = SimpleNamespace(
        git=git,
        _ensure_retained_repository=AsyncMock(),
        _resolve_repository=AsyncMock(
            return_value=SimpleNamespace(retained_git_dir=work, origin_url=str(origin))
        ),
    )
    heads = await MigrationInspector(promotion)(
        dict(repository_id="repo", source_base=base, source_head=reviewed)
    )
    assert heads == (MigrationHead("migrations/versions/44.py", "44", ("43",)),)
    assert [call.kwargs["oid"] for call in git.afetch_repository_oid.await_args_list] == [
        base,
        reviewed,
    ]


def test_internal_migration_chain_does_not_hide_a_sibling_collision():
    members = [
        dict(task_id=task, repository_id="repo", source_base="base", source_head=task)
        for task in ("first", "second", "third")
    ]
    inspected = {
        (member["task_id"], "repo", "base", member["source_head"]): heads
        for member, heads in zip(
            members,
            (
                (MigrationHead("44.py", "44", ("43",)), MigrationHead("45.py", "45", ("44",))),
                (MigrationHead("46.py", "46", ("43",)),),
                (MigrationHead("47.py", "47", ("45",)),),
            ),
            strict=True,
        )
    }
    accepted, deferred = select_members(members, inspected)
    assert [member["task_id"] for member in accepted] == ["first", "third"]
    assert [member["task_id"] for member in deferred] == ["second"]


def test_separate_alembic_environments_can_use_the_same_revision_ids():
    members = [
        dict(task_id=task, repository_id="repo", source_base="base", source_head=task)
        for task in ("first", "second")
    ]
    inspected = {
        (member["task_id"], "repo", "base", member["source_head"]): (
            MigrationHead(f"{member['task_id']}/migrations/versions/44.py", "44", ("43",)),
        )
        for member in members
    }
    accepted, deferred = select_members(members, inspected)
    assert accepted == members and deferred == []


@pytest.mark.parametrize(
    "second_revision, second_parent", [("44", "43"), ("45", "43"), ("44", "42")]
)
async def test_seal_defers_alembic_collisions_and_notifies_once(db, second_revision, second_parent):
    from src.integration.scheduler import TrainService

    await _enable_train(db)
    await _seed_leaf(db, "first", "1" * 40)
    await _seed_leaf(db, "second", "2" * 40)
    await _seed_leaf(db, "unrelated", "3" * 40)
    request = await _request(db)

    async def inspect(member):
        if member["task_id"] == "unrelated":
            return ()
        revision, parent = (
            ("44", "43") if member["task_id"] == "first" else (second_revision, second_parent)
        )
        return (MigrationHead(f"migrations/versions/{revision}.py", revision, (parent,)),)

    service = TrainService(db, page_size=1, migration_inspector=inspect)
    batch = await service.seal("p", request["request_id"], 20.0)
    assert batch["outcome"] == "sealed"
    assert await service.seal("p", request["request_id"], 21.0) == batch
    async with db._engine.connect() as conn:
        members = (
            (
                await conn.execute(
                    select(integration_batch_members.c.task_id).order_by(
                        integration_batch_members.c.ordinal
                    )
                )
            )
            .scalars()
            .all()
        )
        notices = (await conn.execute(select(messages))).mappings().all()
        approval = (
            await conn.execute(
                select(integration_review_evidence.c.verdict).where(
                    integration_review_evidence.c.source_task_id == "second"
                )
            )
        ).scalar_one()
        eligible = await db.eligible_root_page_on(
            conn, project_id="p", repository_id="repo", after=None, limit=100
        )
    assert members == ["first", "unrelated"]
    assert len(notices) == 1
    assert notices[0]["to_id"] == "supervisor-p"
    assert "second" in notices[0]["body"] and "Rechain" in notices[0]["body"]
    assert approval == "approved"
    assert "second" in {member["task_id"] for member in eligible}


async def test_collision_deferral_also_defers_its_declared_dependent(db):
    from src.integration.scheduler import TrainService
    from src.task_graph.integration_dependencies import declare

    await _enable_train(db)
    for index, task in enumerate(("first", "second", "dependent", "independent"), start=1):
        await _seed_leaf(db, task, str(index) * 40)
    async with db.immediate() as conn:
        await declare(conn, dependent_task_id="dependent", dependency_task_id="second", now=2.0)
    request = await _request(db)

    async def inspect(member):
        if member["task_id"] in {"dependent", "independent"}:
            return (MigrationHead("99.py", "99", ("98",)),)
        return (MigrationHead("44.py", "44", ("43",)),)

    await TrainService(db, migration_inspector=inspect).seal("p", request["request_id"], 20.0)
    async with db._engine.connect() as conn:
        assert (
            await conn.execute(
                select(integration_batch_members.c.task_id).order_by(
                    integration_batch_members.c.ordinal
                )
            )
        ).scalars().all() == ["first", "independent"]


async def test_seal_rechecks_reviewed_head_after_migration_inspection(db):
    from src.integration.scheduler import TrainService

    await _enable_train(db)
    await _seed_leaf(db, "first", "1" * 40)
    request = await _request(db)

    async def inspect(member):
        await _seed_leaf(db, "late", "2" * 40)
        return ()

    result = await TrainService(db, migration_inspector=inspect)._seal_once(
        "p", request["request_id"], 20.0
    )
    assert result["outcome"] == "stale"
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_batches.c.id))).first() is None


async def test_public_seal_reinspects_a_frontier_that_changed_during_git_reads(db):
    from src.integration.scheduler import TrainService

    await _enable_train(db)
    await _seed_leaf(db, "first", "1" * 40)
    request = await _request(db)
    inspected = []

    async def inspect(member):
        if not inspected:
            await _seed_leaf(db, "late", "2" * 40)
        inspected.append(member["task_id"])
        return ()

    result = await TrainService(db, migration_inspector=inspect).seal(
        "p", request["request_id"], 20.0
    )
    assert result["outcome"] == "sealed"
    assert inspected == ["first", "first", "late"]


async def test_continuously_changing_frontier_gets_a_bounded_durable_retry(db):
    from src.integration.scheduler import TrainService

    await _enable_train(db)
    await _seed_leaf(db, "first", "1" * 40)
    request = await _request(db)
    observations = []

    async def inspect(member):
        observations.append(member["task_id"])
        ordinal = len(observations)
        await _seed_leaf(db, f"late-{ordinal}", f"{ordinal + 10:040x}")
        return ()

    result = await TrainService(db, migration_inspector=inspect).seal(
        "p", request["request_id"], 20.0
    )
    assert result["outcome"] == "busy"
    assert len(observations) == 7  # Three snapshots of 1, 2 and 4 sources.
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_batches.c.id))).first() is None
        retries = (
            (
                await conn.execute(
                    select(integration_outbox).where(
                        integration_outbox.c.id.startswith("integration-seal-retry:")
                    )
                )
            )
            .mappings()
            .all()
        )
        schedule = (await conn.execute(select(project_integration_schedules))).mappings().one()
    assert retries == []
    assert schedule["outstanding_request_id"] == request["request_id"]


def _artifact() -> ArtifactSnapshot:
    return ArtifactSnapshot(
        playbook_id="hierarchical-delivery",
        artifact_sha256="sha256:" + "1" * 64,
        schema_generation=2,
        contract_fingerprint="sha256:" + "2" * 64,
        source_digest="sha256:" + "3" * 64,
        compiler_build="task8b-test",
        compiled_at="2026-09-05T00:00:00Z",
        version=1,
    )


def _policy() -> dict:
    boundary = IntegrationBoundaryPolicy(
        required_checks=RequiredCheckSet(version="checks-v1", names=("unit",), producer_id="forge"),
        repair=RepairPolicy(
            primary_seconds=30,
            primary_attempts=2,
            debug_seconds=60,
            debug_attempts=1,
            debug_intelligence_class="debug-high",
            debug_profile_id="debugger",
        ),
        route=PlaybookRoute(
            playbook_id="hierarchical-delivery",
            scope="project",
            scope_identifier="p",
            activation_id="activation-audit",
            artifact=_artifact(),
        ),
        primary_intelligence_class="primary-medium",
        primary_profile_id="repairer",
        verifier_intelligence_class="verifier-high",
        verifier_profile_id="verifier",
    )
    return HierarchicalIntegrationPolicy(
        parent=boundary,
        root=boundary,
        branchless_parent="verifier",
        on_failed_child="block",
    ).model_dump(mode="json")


@pytest.fixture
async def db(tmp_path, reuse_database):
    database = await reuse_database("integration-sealing.db")
    await database.create_project(Project(id="p", name="integration project"))
    await database.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.LINK,
            default_branch="main",
        )
    )
    yield database


@pytest.fixture
async def concurrent_db(request, tmp_path, reuse_database):
    database = await reuse_database("integration-sealing-concurrent.db")
    await database.create_project(Project(id="p", name="integration project"))
    await database.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.LINK,
            default_branch="main",
        )
    )
    yield database
    await database.close()


async def _enable_train(db) -> dict:
    artifact = _artifact()
    policy = _policy()
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
                **artifact.model_dump(),
                scope="project",
                scope_identifier="p",
                profile_fingerprint="",
                path="/tmp/task8b-artifact",
                size_bytes=1,
                validation="{}",
                created_at=1.0,
            )
        )
    return policy


async def _request(db, *, now: float = 10.0) -> dict:
    from src.integration.scheduler import IntegrationScheduler

    return await IntegrationScheduler(db).mark_due("p", now, "manual")


async def _seed_leaf(db, task_id: str, head: str, *, source_base=BASE_SHA, with_review=True, **task_overrides) -> dict:
    review = _review_row(task_id, head, evidence_id=f"review-{task_id}", source_base=source_base)
    async with db.immediate() as conn:
        await conn.execute(insert(tasks).values(**_task_row(task_id, **task_overrides)))
        await conn.execute(insert(task_branch_origins).values(
            **{**_origin_row(task_id), "base_sha": source_base}
        ))
        await conn.execute(
            insert(task_integration_checkpoints).values(**_checkpoint_row(task_id, head))
        )
        if with_review:
            await conn.execute(insert(integration_review_evidence).values(**review))
    return review


@pytest.fixture
async def adopted_root(db, tmp_path, request):
    """Retained Git proof with no root receipt or manufactured CI evidence."""
    from src.git.manager import GitManager
    from src.integration.development import DevelopmentPrimitives
    from tests.test_integration_candidates import _git, _make_origin

    origin, work, base, sources = _make_origin(tmp_path)
    adoption = getattr(request, "param", "main")
    target = "other" if adoption == "other" else "main"
    head = sources[0][1]
    await _enable_train(db)
    await _seed_leaf(db, "adopted", head, branch_name="root-0", source_base=base)
    async with db.immediate() as conn:
        await conn.execute(update(repos).where(repos.c.id == "repo").values(url=str(origin)))
        await conn.execute(update(task_integration_checkpoints).values(branch="root-0"))
        await conn.execute(insert(task_completion_records).values(
            id="adopted-close", task_id="adopted", outcome="pass",
            commits='["' + head + '"]', completed_at=2.0,
        ))
    _git(work, "switch", "main")
    if adoption == "equivalent":
        (work / "equivalent.txt").write_text("operator replacement\n")
        _git(work, "add", ".")
        _git(work, "commit", "-m", "operator supplied equivalent work")
    else:
        _git(work, "merge", "--no-ff", "-m", "deliver root", "root-0")
    _git(work, "push", "origin", f"HEAD:refs/heads/{target}")
    service = DevelopmentPrimitives(db, data_dir=tmp_path / "data", git=GitManager())
    # Retain proof from an already-installed generation; no retired control runs.
    from src.integration.provenance import CompletionIdentity, CompletedSource, GitProvenance
    provenance = GitProvenance(service.git, str(work), repository_url=str(origin))
    original = CompletedSource(CompletionIdentity("p", "repo", "adopted", "adopted-close"), head)
    await provenance.write_completion(original)
    if adoption == "equivalent":
        replacement = _git(work, "rev-parse", "HEAD")
        await provenance.write_replacement(
            source_oid=replacement, base_oid=base, replaces=[original],
            authority="operator", reason="historical operator acceptance",
        )
    db.set_delivery_observer(service.delivery_observer)
    return service, origin, work, base, sources


@pytest.mark.parametrize("adopted_root", ["main", "equivalent"], indirect=True)
async def test_adopted_default_branch_source_is_not_reseated_on_two_sweeps(db, adopted_root):
    from src.integration.scheduler import TrainService

    inspector = AsyncMock(return_value=())
    for now in (10.0, 30.0):
        request = await _request(db, now=now)
        result = await TrainService(db, migration_inspector=inspector).seal(
            "p", request["request_id"], now + 1
        )
        assert result["outcome"] == "empty"
    inspector.assert_not_awaited()
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_batches))).all() == []
        assert (await conn.execute(select(task_delivery_receipts))).all() == []


@pytest.mark.parametrize("adopted_root", ["other"], indirect=True)
async def test_adoption_on_another_target_still_needs_default_branch_delivery(db, adopted_root):
    from src.integration.scheduler import TrainService

    request = await _request(db)
    result = await TrainService(db).seal("p", request["request_id"], 20.0)
    assert result["outcome"] == "sealed"


async def test_failed_git_observation_does_not_exclude_a_root(db, adopted_root, monkeypatch):
    from dataclasses import replace

    from src.integration.scheduler import TrainService

    service, *_rest = adopted_root
    snapshot = service.delivery_observer._snapshot

    async def failed_snapshot(target, max_age=0.0):
        return replace(await snapshot(target, max_age), target_oid=None, error="fetch_failed")

    monkeypatch.setattr(service.delivery_observer, "_snapshot", failed_snapshot)
    request = await _request(db)
    result = await TrainService(db).seal("p", request["request_id"], 20.0)
    assert result["outcome"] == "sealed"


@pytest.mark.parametrize("changed", ["source", "generation", "target", "repository"])
@pytest.mark.parametrize("during_observation", [False, True])
async def test_stale_adoption_cannot_hide_current_root(
    db, adopted_root, monkeypatch, changed, during_observation
):
    from src.integration.scheduler import TrainService
    from tests.test_integration_candidates import _git

    service, _origin, work, base, sources = adopted_root

    async def change_identity():
        async with db.immediate() as conn:
            if changed == "source":
                await conn.execute(update(task_integration_checkpoints).values(
                    checkpoint_sha=sources[1][1], generation=2,
                ))
                await conn.execute(insert(integration_review_evidence).values(**_review_row(
                    "adopted", sources[1][1], evidence_id="changed-review",
                    source_base=base, generation=2, created_at=3.0,
                )))
            elif changed == "generation":
                # Same source SHA, a new close without retained provenance:
                # the old adoption cannot answer this generation.
                await conn.execute(insert(task_completion_records).values(
                    id="new-close", task_id="adopted", outcome="pass",
                    commits='["' + sources[0][1] + '"]', completed_at=3.0,
                ))
                await conn.execute(update(task_integration_checkpoints).values(generation=2))
                await conn.execute(insert(integration_review_evidence).values(**_review_row(
                    "adopted", sources[0][1], evidence_id="changed-review",
                    source_base=base, generation=2, created_at=3.0,
                )))
            elif changed == "target":
                _git(work, "push", "origin", f"{base}:refs/heads/other")
                await conn.execute(update(repos).values(default_branch="other"))
            else:
                # The completion ref is fenced to repo, never to this new id.
                await conn.execute(insert(repos).values(
                    id="new-repo", project_id="p", source_type="clone",
                    url=str(_origin), default_branch="main", checkout_base_path="",
                ))
                await conn.execute(update(projects).values(integration_repository_id="new-repo"))
                await conn.execute(update(tasks).values(repo_id="new-repo"))
                await conn.execute(update(task_integration_checkpoints).values(repository_id="new-repo"))
                await conn.execute(update(task_branch_origins).values(repository_id="new-repo"))
                await conn.execute(insert(integration_review_evidence).values(**_review_row(
                    "adopted", sources[0][1], evidence_id="changed-review",
                    source_base=base, repository_id="new-repo", created_at=3.0,
                )))

    if during_observation:
        observe = service.delivery_observer.observe

        async def observe_then_change(ids):
            view = await observe(ids)
            await change_identity()
            return view

        monkeypatch.setattr(service.delivery_observer, "observe", observe_then_change)
    else:
        await change_identity()
    request = await _request(db)
    result = await TrainService(db).seal("p", request["request_id"], 20.0)
    assert result["outcome"] == "sealed"
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_batch_members.c.task_id))).scalars().all() == ["adopted"]


@pytest.mark.parametrize("blocked", [None, "hold", "gate", "review", "reopened"])
async def test_adopted_dependency_satisfies_ordering_without_bypassing_admission(
    db, adopted_root, blocked
):
    from src.task_graph.integration_dependencies import declare
    from src.integration.scheduler import TrainService

    _service, _origin, _work, base, sources = adopted_root
    await _seed_leaf(db, "dependent", sources[1][1], branch_name="root-1", source_base=base)
    async with db.immediate() as conn:
        await declare(conn, dependent_task_id="dependent", dependency_task_id="adopted", now=1.0)
        # The prerequisite's hold does not revoke its delivery.
        await conn.execute(insert(task_labels).values(task_id="adopted", label="hold:operator"))
        if blocked == "hold":
            await conn.execute(insert(task_labels).values(task_id="dependent", label="hold:operator"))
        elif blocked == "gate":
            await conn.execute(insert(gates).values(
                id="dependent-gate", project_id="p", gate_type="human", title="review",
                question="Approve delivery?", status="open", created_at=1.0,
            ))
            await conn.execute(insert(task_gates).values(task_id="dependent", gate_id="dependent-gate"))
        elif blocked == "review":
            await conn.execute(insert(integration_review_evidence).values(**_review_row(
                "dependent", sources[1][1], evidence_id="rejected-review",
                source_base=base, verdict="rejected", created_at=3.0,
            )))
        elif blocked == "reopened":
            await conn.execute(update(tasks).where(tasks.c.id == "adopted").values(
                status="IN_PROGRESS", updated_at=3.0,
            ))
    request = await _request(db)
    # Production also has migration inspection, which must see the dependent
    # even though its prerequisite has no train receipt.
    result = await TrainService(db, migration_inspector=AsyncMock(return_value=())).seal(
        "p", request["request_id"], 20.0
    )
    assert result["outcome"] == ("empty" if blocked else "sealed")
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_batch_members.c.task_id))).scalars().all() == (
            [] if blocked else ["dependent"]
        )


async def _seed_exact_parent(db) -> None:
    parent_head = "e" * 40
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks),
            [
                _task_row("parent"),
                _task_row(
                    "child",
                    parent_task_id="parent",
                    status=TaskStatus.COMPLETED.value,
                    pr_url=None,
                ),
            ],
        )
        await conn.execute(insert(task_branch_origins).values(**_origin_row("parent")))
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode-parent",
                parent_task_id="parent",
                repository_id="repo",
                generation=4,
                pre_collection_checkpoint_sha=BASE_SHA,
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="operation-parent",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="episode-parent",
                active_stage=0,
                state="completed",
                policy_snapshot={"version": 1, "kind": "parent-proof"},
                artifact_snapshot={"artifact_sha256": "sha256:" + "1" * 64},
                required_check_version="checks-v1",
                created_at=1.0,
                updated_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="verification-parent",
                operation_id="operation-parent",
                parent_task_id="parent",
                episode_id="episode-parent",
                generation=4,
                head_sha=parent_head,
                required_check_version="checks-v1",
                created_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_parent_operation_completions).values(
                operation_id="operation-parent",
                verification_id="verification-parent",
                parent_task_id="parent",
                episode_id="episode-parent",
                completed_at=2.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                **_checkpoint_row(
                    "parent",
                    parent_head,
                    generation=4,
                    verified_sha=parent_head,
                    verified_generation=4,
                    episode_id="episode-parent",
                    current_verification_id="verification-parent",
                    last_completed_operation_id="operation-parent",
                    last_completed_verification_id="verification-parent",
                )
            )
        )
        await conn.execute(
            insert(integration_review_evidence).values(
                **_review_row(
                    "parent",
                    parent_head,
                    evidence_id="review-parent",
                    review_kind="parent",
                    generation=4,
                    evidence={
                        "decision": "approved",
                        "verification_id": "verification-parent",
                    },
                )
            )
        )


def _task_row(task_id: str, **overrides):
    values = {
        "id": task_id,
        "project_id": "p",
        "parent_task_id": None,
        "repo_id": "repo",
        "title": task_id,
        "description": "root delivery",
        "status": TaskStatus.COMPLETED.value,
        "integration_mode": "pull_request",
        "pr_url": f"https://example.test/pull/{task_id}",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    values.update(overrides)
    return values


def _origin_row(task_id: str):
    return {
        "id": f"origin-{task_id}",
        "task_id": task_id,
        "repository_id": "repo",
        "base_sha": BASE_SHA,
        "creation_generation": 0,
        "reserved": True,
        "materialized": False,
        "created_at": 1.0,
        "materialized_at": None,
    }


def _checkpoint_row(task_id: str, head: str, **overrides):
    values = {
        "task_id": task_id,
        "repository_id": "repo",
        "branch": f"aq/{task_id}",
        "generation": 1,
        "checkpoint_sha": head,
        "state": "working",
        "version": 1,
        "updated_at": 1.0,
    }
    values.update(overrides)
    return values


def _review_row(task_id: str, head: str, *, evidence_id: str, **overrides):
    values = {
        "id": evidence_id,
        "source_task_id": task_id,
        "repository_id": "repo",
        "source_base": BASE_SHA,
        "reviewed_head_sha": head,
        "reviewed_tree_sha": f"{int(head, 16) + 10_000:040x}",
        "reviewer_task_id": f"reviewer-{task_id}",
        "reviewer_session_attempt_id": None,
        "reviewer_identity": None,
        "review_kind": "leaf",
        "generation": 1,
        "verdict": "approved",
        "evidence": {"decision": "approved", "task_id": task_id, "head": head},
        "created_at": 1.0,
    }
    values.update(overrides)
    return values


async def test_keyset_pages_full_frontier_and_bulk_review_selects_latest_exact(db):
    leaf_ids = [f"root-{index:03d}" for index in range(205)]
    heads = {task_id: f"{index + 1:040x}" for index, task_id in enumerate(leaf_ids)}
    async with db.immediate() as conn:
        await conn.execute(insert(tasks), [_task_row(task_id) for task_id in leaf_ids])
        await conn.execute(
            insert(task_branch_origins), [_origin_row(task_id) for task_id in leaf_ids]
        )
        await conn.execute(
            insert(task_integration_checkpoints),
            [_checkpoint_row(task_id, heads[task_id]) for task_id in leaf_ids],
        )
        reviews = [
            _review_row(task_id, heads[task_id], evidence_id=f"review-{task_id}")
            for task_id in leaf_ids
        ]
        reviews.extend(
            [
                _review_row(
                    "root-050",
                    heads["root-050"],
                    evidence_id="review-root-050-earlier",
                    created_at=2.0,
                ),
                _review_row(
                    "root-050",
                    heads["root-050"],
                    evidence_id="review-root-050-latest-rejection",
                    verdict="rejected",
                    evidence={"decision": "rejected", "reason": "latest wins"},
                    created_at=3.0,
                ),
            ]
        )
        await conn.execute(insert(integration_review_evidence), reviews)

        scanned = []
        reviews_by_key = {}
        after = None
        while True:
            page = await db.eligible_root_page_on(
                conn,
                project_id="p",
                repository_id="repo",
                after=after,
                limit=17,
            )
            if not page:
                break
            scanned.extend(page)
            reviews_by_key.update(await db.latest_exact_reviews_on(conn, page))
            after = (page[-1]["task_id"], page[-1]["source_head"])

    assert [(row["task_id"], row["source_head"]) for row in scanned] == [
        (task_id, heads[task_id]) for task_id in leaf_ids
    ]
    assert len({row["task_id"] for row in scanned}) == 205
    assert {row["source_kind"] for row in scanned} == {"leaf"}
    rejected_key = ("root-050", "repo", BASE_SHA, heads["root-050"], 1)
    assert reviews_by_key[rejected_key]["id"] == "review-root-050-latest-rejection"
    assert reviews_by_key[rejected_key]["verdict"] == "rejected"
    assert reviews_by_key[rejected_key]["evidence"] == {
        "decision": "rejected",
        "reason": "latest wins",
    }


async def test_root_projection_requires_leaf_or_exact_current_parent_identity(db):
    parent_head = "e" * 40
    leaf_head = "f" * 40
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks),
            [
                _task_row("leaf"),
                _task_row("parent"),
                _task_row(
                    "parent-child",
                    parent_task_id="parent",
                    status=TaskStatus.COMPLETED.value,
                    pr_url=None,
                ),
            ],
        )
        await conn.execute(
            insert(task_branch_origins), [_origin_row("leaf"), _origin_row("parent")]
        )
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode-parent",
                parent_task_id="parent",
                repository_id="repo",
                generation=4,
                pre_collection_checkpoint_sha=BASE_SHA,
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="operation-parent",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="episode-parent",
                active_stage=0,
                state="completed",
                policy_snapshot={"version": 1, "kind": "parent-proof"},
                artifact_snapshot={"artifact_sha256": "sha256:" + "1" * 64},
                required_check_version="checks-v1",
                created_at=1.0,
                updated_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="verification-parent",
                operation_id="operation-parent",
                parent_task_id="parent",
                episode_id="episode-parent",
                generation=4,
                head_sha=parent_head,
                required_check_version="checks-v1",
                created_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_parent_operation_completions).values(
                operation_id="operation-parent",
                verification_id="verification-parent",
                parent_task_id="parent",
                episode_id="episode-parent",
                completed_at=2.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(**_checkpoint_row("leaf", leaf_head))
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                **_checkpoint_row(
                    "parent",
                    parent_head,
                    generation=4,
                    verified_sha=parent_head,
                    verified_generation=4,
                    episode_id="episode-parent",
                    current_verification_id="verification-parent",
                    last_completed_operation_id="operation-parent",
                    last_completed_verification_id="verification-parent",
                )
            )
        )
        await conn.execute(
            insert(integration_review_evidence),
            [
                _review_row("leaf", leaf_head, evidence_id="review-leaf"),
                _review_row(
                    "parent",
                    parent_head,
                    evidence_id="review-parent",
                    review_kind="parent",
                    generation=4,
                    evidence={
                        "decision": "approved",
                        "verification_id": "verification-parent",
                    },
                ),
            ],
        )

        page = await db.eligible_root_page_on(
            conn,
            project_id="p",
            repository_id="repo",
            after=None,
            limit=10,
        )
        reviews = await db.latest_exact_reviews_on(conn, page)

    assert [(row["task_id"], row["source_kind"]) for row in page] == [
        ("leaf", "leaf"),
        ("parent", "parent"),
    ]
    parent = page[1]
    assert parent["source_base"] == BASE_SHA
    assert parent["source_head"] == parent_head
    assert parent["generation"] == 4
    assert parent["current_verification_id"] == "verification-parent"
    parent_key = ("parent", "repo", BASE_SHA, parent_head, 4)
    assert reviews[parent_key]["id"] == "review-parent"
    assert reviews[parent_key]["evidence"]["verification_id"] == "verification-parent"


@pytest.mark.parametrize("receipt_branch", ["main", "refs/heads/main", "refs/heads/other"])
async def test_root_projection_excludes_each_common_near_miss(db, receipt_branch):
    task_ids = (
        "good",
        "nested",
        "not-completed",
        "wrong-repository",
        "missing-checkpoint",
        "retired-origin",
        "held",
        "gated",
        "already-batched",
        "already-delivered",
        "blank-pr",
    )
    for index, task_id in enumerate(task_ids):
        await _seed_leaf(db, task_id, f"{index + 1:040x}")
    await db.create_repo(
        RepoConfig(
            id="other-repo",
            project_id="p",
            source_type=RepoSourceType.LINK,
            default_branch="main",
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == "nested").values(parent_task_id="not-completed")
        )
        await conn.execute(
            update(tasks).where(tasks.c.id == "not-completed").values(status=TaskStatus.READY.value)
        )
        await conn.execute(
            update(tasks).where(tasks.c.id == "wrong-repository").values(repo_id="other-repo")
        )
        await conn.execute(
            delete(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == "missing-checkpoint"
            )
        )
        await conn.execute(
            update(task_branch_origins)
            .where(task_branch_origins.c.task_id == "retired-origin")
            .values(retired_at=2.0)
        )
        await conn.execute(insert(task_labels).values(task_id="held", label="hold:operator"))
        await conn.execute(
            insert(gates).values(
                id="gate-open",
                project_id="p",
                gate_type="human",
                title="blocking gate",
                question="continue?",
                status="open",
                created_at=1.0,
            )
        )
        await conn.execute(insert(task_gates).values(task_id="gated", gate_id="gate-open"))
        await conn.execute(
            insert(integration_batches).values(
                id="prior-batch",
                project_id="p",
                repository_id="repo",
                request_id="prior-request",
                trigger="manual",
                source_manifest_digest="sha256:" + "4" * 64,
                base_sha=BASE_SHA,
                lifecycle="sealing",
                integration_branch="refs/heads/aq/integration/p/prior",
                policy_snapshot={"version": 1},
                artifact_snapshot={"artifact_sha256": "sha256:" + "1" * 64},
                cleanup_state="pending",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        batched_review = (
            (
                await conn.execute(
                    select(integration_review_evidence).where(
                        integration_review_evidence.c.source_task_id == "already-batched"
                    )
                )
            )
            .mappings()
            .one()
        )
        await conn.execute(
            insert(integration_batch_members).values(
                batch_id="prior-batch",
                ordinal=0,
                task_id="already-batched",
                pr_url="https://example.test/pull/already-batched",
                repository_id="repo",
                source_base_sha=BASE_SHA,
                reviewed_head_sha=batched_review["reviewed_head_sha"],
                reviewed_tree_sha=batched_review["reviewed_tree_sha"],
                review_evidence_id=batched_review["id"],
                review_evidence=dict(batched_review),
            )
        )
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "prior-batch")
            .values(lifecycle="sealed")
        )
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="root-receipt",
                domain_key="root-receipt-domain",
                source_task_id="already-delivered",
                target_task_id=None,
                repository_id="repo",
                target_branch=receipt_branch,
                disposition="code",
                created_at=2.0,
            )
        )
        await conn.execute(update(tasks).where(tasks.c.id == "blank-pr").values(pr_url="   "))

        page = await db.eligible_root_page_on(
            conn,
            project_id="p",
            repository_id="repo",
            after=None,
            limit=100,
        )
        delivered = await db.delivered_root_task_ids_on(
            conn, project_id="p", repository_id="repo"
        )

    expected = ["already-delivered", "good"] if receipt_branch.endswith("/other") else ["good"]
    assert [row["task_id"] for row in page] == expected
    assert delivered == (set() if receipt_branch.endswith("/other") else {"already-delivered"})


async def test_seal_orders_declared_dependencies_and_defers_missing_ones(db):
    from src.task_graph.integration_dependencies import declare
    from src.integration.scheduler import TrainService

    await _enable_train(db)
    for index, task_id in enumerate(("a", "b", "c", "d"), start=1):
        await _seed_leaf(db, task_id, f"{index:040x}")
    async with db.immediate() as conn:
        await declare(conn, dependent_task_id="a", dependency_task_id="b", now=1.0)
        await declare(conn, dependent_task_id="d", dependency_task_id="missing", now=1.0)

    request = await _request(db)
    result = await TrainService(db, page_size=2).seal("p", request["request_id"], 20.0)
    async with db.immediate() as conn:
        members = (
            await conn.execute(
                select(integration_batch_members.c.task_id)
                .where(integration_batch_members.c.batch_id == result["batch_id"])
                .order_by(integration_batch_members.c.ordinal)
            )
        ).scalars().all()

    assert result["outcome"] == "sealed"
    assert members == ["b", "a", "c"]


async def test_seal_accepts_delivered_dependency_after_source_task_is_archived(db):
    from src.task_graph.integration_dependencies import declare
    from src.integration.scheduler import TrainService

    await _enable_train(db)
    await _seed_leaf(db, "dependent", "b" * 40)
    async with db.immediate() as conn:
        await declare(
            conn,
            dependent_task_id="dependent",
            dependency_task_id="archived-dependency",
            now=1.0,
        )
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="archived-dependency-receipt",
                domain_key="archived-dependency-receipt",
                source_task_id="archived-dependency",
                target_task_id=None,
                repository_id="repo",
                target_branch="refs/heads/main",
                disposition="code",
                created_at=1.0,
            )
        )

    request = await _request(db)
    result = await TrainService(db).seal("p", request["request_id"], 20.0)
    async with db.immediate() as conn:
        members = (
            await conn.execute(
                select(integration_batch_members.c.task_id).where(
                    integration_batch_members.c.batch_id == result["batch_id"]
                )
            )
        ).scalars().all()

    assert result["outcome"] == "sealed"
    assert members == ["dependent"]


async def test_zero_root_seal_is_terminal_resource_free_and_request_replay(db):
    from src.integration.scheduler import TrainService

    await _enable_train(db)
    request = await _request(db)
    service = TrainService(db, page_size=3)

    first = await service.seal("p", request["request_id"], 20.0)
    replay = await service.seal("p", request["request_id"], 30.0)

    assert first == replay
    assert first == {
        "outcome": "empty",
        "project_id": "p",
        "request_id": request["request_id"],
        "batch_id": f"integration-empty:{request['request_id']}",
        "operation_id": None,
    }
    async with db._engine.connect() as conn:
        schedule = (
            (
                await conn.execute(
                    select(project_integration_schedules).where(
                        project_integration_schedules.c.project_id == "p"
                    )
                )
            )
            .mappings()
            .one()
        )
        # An empty frontier is not a train: no batch row, lease or operation.
        assert (await conn.execute(select(integration_batches))).all() == []
        assert (await conn.execute(select(project_integration_leases))).all() == []
        assert (await conn.execute(select(integration_repair_operations))).all() == []
        events = (await conn.execute(select(integration_outbox))).mappings().all()

    assert schedule["outstanding_request_id"] is None
    assert schedule["last_completed_sweep_at"] == 20.0
    assert events == []  # Durable Subject visits do not enqueue legacy sweeps.


async def test_empty_seal_replay_is_not_busy_while_a_later_train_holds_the_lease(db):
    from src.integration.scheduler import IntegrationScheduler, TrainService

    await _enable_train(db)
    first_request = await _request(db)
    empty = await TrainService(db).seal("p", first_request["request_id"], 20.0)
    await _seed_leaf(db, "root", "b" * 40)
    second_request = await IntegrationScheduler(db).mark_due("p", 30.0, "manual")
    sealed = await TrainService(db).seal("p", second_request["request_id"], 40.0)
    inspector = AsyncMock(side_effect=AssertionError("an ended request reads no Git"))

    replays = [
        await TrainService(db).seal("p", first_request["request_id"], 50.0),
        await TrainService(db, migration_inspector=inspector).seal(
            "p", first_request["request_id"], 60.0
        ),
    ]

    assert empty["outcome"] == "empty"
    assert sealed["outcome"] == "sealed"
    assert replays == [empty, empty]
    async with db._engine.connect() as conn:
        batches = (await conn.execute(select(integration_batches))).mappings().all()
        lease = (await conn.execute(select(project_integration_leases))).mappings().one()
    assert [(batch["id"], batch["request_id"], batch["lifecycle"]) for batch in batches] == [
        (sealed["batch_id"], second_request["request_id"], "sealed")
    ]
    assert lease["batch_id"] == sealed["batch_id"]


async def test_concurrent_seals_of_one_empty_request_insert_no_batch(concurrent_db):
    from src.integration.scheduler import TrainService

    await _enable_train(concurrent_db)
    request = await _request(concurrent_db)

    results = await asyncio.gather(
        TrainService(concurrent_db).seal("p", request["request_id"], 20.0),
        TrainService(concurrent_db).seal("p", request["request_id"], 20.0),
    )

    assert results[0] == results[1]
    assert results[0]["outcome"] == "empty"
    async with concurrent_db._engine.connect() as conn:
        assert (await conn.execute(select(integration_batches))).all() == []
        schedule = (
            (await conn.execute(select(project_integration_schedules))).mappings().one()
        )
    assert schedule["outstanding_request_id"] is None


@pytest.mark.parametrize(
    "request_id", ["integration-sweep:p:2", "integration-sweep:other:1", "request-1"]
)
async def test_seal_still_refuses_a_request_that_was_never_minted(db, request_id):
    from src.integration.scheduler import TrainService

    await _enable_train(db)
    request = await _request(db)
    assert request["request_id"] == "integration-sweep:p:1"

    with pytest.raises(ValueError, match="not outstanding"):
        await TrainService(db).seal("p", request_id, 20.0)


async def test_release_confirms_a_rowless_empty_seal_and_refuses_forged_ones(db):
    from src.integration.release import IntegrationReleaseService
    from src.integration.scheduler import IntegrationScheduler, TrainService

    await _enable_train(db)
    first = await _request(db)
    empty = await TrainService(db).seal("p", first["request_id"], 20.0)
    await _seed_leaf(db, "root", "b" * 40)
    second = await IntegrationScheduler(db).mark_due("p", 30.0, "manual")

    async def schedule_row():
        async with db._engine.connect() as conn:
            return dict(
                (await conn.execute(select(project_integration_schedules))).mappings().one()
            )

    before = await schedule_row()
    service = IntegrationReleaseService(db)
    confirmed = await service.release(empty["batch_id"], 35.0)
    # The second request is still outstanding, so it was never an empty seal.
    outstanding = await service.release(f"integration-empty:{second['request_id']}", 35.0)
    assert await schedule_row() == before

    sealed = await TrainService(db).seal("p", second["request_id"], 40.0)
    forged = [
        # Its request has a batch row: the seal was not empty.
        f"integration-empty:{second['request_id']}",
        "integration-empty:integration-sweep:p:9",
        "integration-empty:integration-sweep:other:1",
        "integration-empty:request-1",
    ]
    refused = [await service.release(batch_id, 45.0) for batch_id in forged]

    assert sealed["outcome"] == "sealed"
    assert (confirmed.outcome, confirmed.project_id, confirmed.request_id) == (
        "empty",
        "p",
        first["request_id"],
    )
    assert confirmed.batch_id == empty["batch_id"]
    assert (confirmed.operation_id, confirmed.catchup_request_id) == (None, None)
    assert outstanding.outcome == "stale"
    assert [result.outcome for result in refused] == ["stale"] * len(forged)


async def test_seal_disarms_previous_batch_window_before_a_new_approval(db):
    from src.integration.scheduler import TrainService
    from src.integration.settling import note_approval

    await _enable_train(db)
    await _seed_leaf(db, "root", "b" * 40)
    request = await _request(db)
    async with db.immediate() as conn:
        await note_approval(conn, project_id="p", now=10.0)

    assert (await TrainService(db).seal("p", request["request_id"], 320.0))["outcome"] == "sealed"
    async with db.immediate() as conn:
        new_window = await note_approval(conn, project_id="p", now=4000.0)
    assert new_window["outcome"] == "armed"
    assert new_window["first_approval_at"] == 4000.0
    assert new_window["fires_at"] == 4300.0


async def test_one_root_seal_freezes_review_and_real_unstarted_operation(db):
    from src.integration.scheduler import TrainService

    policy = await _enable_train(db)
    review = await _seed_leaf(db, "root", "b" * 40)
    request = await _request(db)
    service = TrainService(db, page_size=1)

    first = await service.seal("p", request["request_id"], 20.0)
    replay = await service.seal("p", request["request_id"], 30.0)

    assert first == replay
    assert first["outcome"] == "sealed"
    assert first["batch_id"] != first["operation_id"]
    async with db._engine.connect() as conn:
        batch = (
            (
                await conn.execute(
                    select(integration_batches).where(integration_batches.c.id == first["batch_id"])
                )
            )
            .mappings()
            .one()
        )
        member = (
            (
                await conn.execute(
                    select(integration_batch_members).where(
                        integration_batch_members.c.batch_id == first["batch_id"]
                    )
                )
            )
            .mappings()
            .one()
        )
        operation = (
            (
                await conn.execute(
                    select(integration_repair_operations).where(
                        integration_repair_operations.c.id == first["operation_id"]
                    )
                )
            )
            .mappings()
            .one()
        )
        stages = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == first["operation_id"]
                )
            )
        ).all()
        pins = (
            (
                await conn.execute(
                    select(integration_operation_artifact_pins).where(
                        integration_operation_artifact_pins.c.operation_id == first["operation_id"]
                    )
                )
            )
            .mappings()
            .all()
        )
        sealed_events = (
            (
                await conn.execute(
                    select(integration_outbox).where(
                        integration_outbox.c.event_type == "integration.sealed"
                    )
                )
            )
            .mappings()
            .all()
        )

    assert batch["lifecycle"] == "sealed"
    assert batch["integration_branch"].startswith("refs/heads/aq/integration/")
    assert batch["policy_snapshot"] == policy
    assert member["task_id"] == "root"
    assert member["source_ref"] == "refs/heads/aq/root"
    assert member["source_ref_retention"] == "delete"
    assert member["review_evidence_id"] == review["id"]
    assert member["review_evidence"] == review
    assert operation["target_kind"] == "batch"
    assert operation["batch_id"] == first["batch_id"]
    assert operation["artifact_snapshot"] == policy["root"]["route"]["artifact"]
    assert stages == []
    assert [pin["artifact_sha256"] for pin in pins] == [
        policy["root"]["route"]["artifact"]["artifact_sha256"]
    ]
    assert len(sealed_events) == 1
    assert sealed_events[0]["payload"] == {
        "project_id": "p",
        "batch_id": first["batch_id"],
        "operation_id": first["operation_id"],
        "event_id": f"integration-sealed:{first['batch_id']}",
    }


async def test_seal_freezes_source_ref_and_retention_from_authoritative_checkpoint(db):
    from src.integration.scheduler import TrainService

    await _enable_train(db)
    policy = _policy()
    policy["cleanup"] = {
        "successful_source_refs": "retain",
        "failed_work_retention_seconds": 1234,
    }
    await db.update_project("p", hierarchical_integration_policy=policy)
    await _seed_leaf(db, "root", "b" * 40)
    request = await _request(db)

    sealed = await TrainService(db).seal("p", request["request_id"], 20.0)
    # Corrupt the stored task row directly: the public update path refuses a
    # branch that disagrees with its integration checkpoint.
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == "root").values(branch_name="mutated-after-seal")
        )

    async with db._engine.connect() as conn:
        batch = (
            (
                await conn.execute(
                    select(integration_batches).where(
                        integration_batches.c.id == sealed["batch_id"]
                    )
                )
            )
            .mappings()
            .one()
        )
        member = (
            (
                await conn.execute(
                    select(integration_batch_members).where(
                        integration_batch_members.c.batch_id == sealed["batch_id"]
                    )
                )
            )
            .mappings()
            .one()
        )

    assert member["source_ref"] == "refs/heads/aq/root"
    assert member["source_ref_retention"] == "retain"
    assert batch["policy_snapshot"]["cleanup"]["failed_work_retention_seconds"] == 1234


async def test_nonempty_seal_retains_first_request_for_manual_and_periodic_coalescing(db):
    from src.integration.scheduler import IntegrationScheduler, TrainService
    from src.integration.settling import note_approval

    await _enable_train(db)
    await _seed_leaf(db, "root", "b" * 40)
    scheduler = IntegrationScheduler(db)
    await scheduler.configure(project_id="p", now=1.0, enabled=True, interval_seconds=5)
    first = await scheduler.mark_due("p", 10.0, "manual")
    sealed = await TrainService(db).seal("p", first["request_id"], 20.0)

    manual = await scheduler.mark_due("p", 30.0, "manual")
    async with db.immediate() as conn:
        await note_approval(conn, project_id="p", now=40.0)
    periodic = await scheduler.mark_due("p", 340.0, "periodic")

    assert sealed["outcome"] == "sealed"
    for replay in (manual, periodic):
        assert replay["outcome"] == "coalesced"
        assert replay["request_id"] == first["request_id"]
        assert replay["trigger"] == "manual"
        assert replay["requested_at"] == 10.0
        assert replay["request_sequence"] == 1
    async with db._engine.connect() as conn:
        schedule = (
            (
                await conn.execute(
                    select(project_integration_schedules).where(
                        project_integration_schedules.c.project_id == "p"
                    )
                )
            )
            .mappings()
            .one()
        )
        sweep_events = (
            (
                await conn.execute(
                    select(integration_outbox).where(
                        integration_outbox.c.event_type == "integration.sweep_due"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert schedule["outstanding_request_id"] == first["request_id"]
    assert schedule["outstanding_trigger"] == "manual"
    assert schedule["outstanding_requested_at"] == 10.0
    assert schedule["request_sequence"] == 1
    assert sweep_events == []


def test_integration_branch_is_ref_safe_for_adversarial_project_and_request_ids():
    from src.git.manager import _validate_ref
    from src.integration.scheduler import TrainService

    inputs = (
        ("../project:^~ with space.lock", "request@{bad}..\\value"),
        (".leading", "trailing."),
    )
    refs = [TrainService._integration_branch(project, request) for project, request in inputs]

    assert refs[0] != refs[1]
    for ref in refs:
        assert re.fullmatch(r"refs/heads/aq/integration/p-[0-9a-f]{32}/r-[0-9a-f]{32}", ref)
        assert _validate_ref(ref) == ref
        checked = subprocess.run(
            ["git", "check-ref-format", ref],
            check=False,
            capture_output=True,
            text=True,
        )
        assert checked.returncode == 0, checked.stderr


async def test_live_batch_is_busy_without_frontier_read_and_expired_batch_resumes(db, monkeypatch):
    from src.integration.scheduler import TrainService

    await _enable_train(db)
    await _seed_leaf(db, "root", "b" * 40)
    first_request = await _request(db)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="existing-batch",
                project_id="p",
                repository_id="repo",
                request_id=first_request["request_id"],
                trigger="manual",
                source_manifest_digest="sha256:" + "4" * 64,
                base_sha=BASE_SHA,
                lifecycle="sealing",
                current_revision=0,
                integration_branch="refs/heads/aq/integration/p/existing",
                policy_snapshot=_policy(),
                artifact_snapshot=_artifact().model_dump(mode="json"),
                cleanup_state="pending",
                created_at=10.0,
                updated_at=10.0,
            )
        )
        await conn.execute(
            insert(project_integration_leases).values(
                project_id="p",
                repository_id="repo",
                batch_id="existing-batch",
                owner_id="sealer-existing-batch",
                fence_token=1,
                heartbeat_at=10.0,
                expires_at=40.0,
            )
        )

    async def forbidden_frontier(*_args, **_kwargs):
        raise AssertionError("busy sealing inspected the source frontier")

    original_frontier = db.eligible_root_page_on
    monkeypatch.setattr(db, "eligible_root_page_on", forbidden_frontier)
    busy = await TrainService(db).seal("p", first_request["request_id"], 20.0)
    assert busy == {
        "outcome": "busy",
        "project_id": "p",
        "request_id": first_request["request_id"],
        "batch_id": "existing-batch",
        "operation_id": None,
    }

    monkeypatch.setattr(db, "eligible_root_page_on", original_frontier)
    resumed = await TrainService(db, page_size=1).seal("p", first_request["request_id"], 50.0)
    assert resumed["outcome"] == "sealed"
    assert resumed["batch_id"] == "existing-batch"
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_batches.c.id))).scalars().all() == [
            "existing-batch"
        ]


async def test_seal_exhausts_small_pages_past_200_and_advances_over_rejections(db):
    from src.integration.scheduler import TrainService

    await _enable_train(db)
    leaf_ids = [f"root-{index:03d}" for index in range(205)]
    heads = {task_id: f"{index + 1:040x}" for index, task_id in enumerate(leaf_ids)}
    task_rows = [_task_row(task_id) for task_id in leaf_ids]
    task_rows[50]["integration_mode"] = "direct"
    task_rows[150]["integration_mode"] = "corrupt-value"
    review_rows = [
        _review_row(task_id, heads[task_id], evidence_id=f"review-{task_id}")
        for task_id in leaf_ids
    ]
    review_rows.append(
        _review_row(
            "root-100",
            heads["root-100"],
            evidence_id="review-root-100-rejected-latest",
            verdict="rejected",
            evidence={"decision": "rejected", "reason": "newest exact review"},
            created_at=2.0,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(insert(tasks), task_rows)
        await conn.execute(
            insert(task_branch_origins), [_origin_row(task_id) for task_id in leaf_ids]
        )
        await conn.execute(
            insert(task_integration_checkpoints),
            [_checkpoint_row(task_id, heads[task_id]) for task_id in leaf_ids],
        )
        await conn.execute(insert(integration_review_evidence), review_rows)
    request = await _request(db)

    result = await TrainService(db, page_size=7).seal("p", request["request_id"], 20.0)

    async with db._engine.connect() as conn:
        members = (
            (
                await conn.execute(
                    select(integration_batch_members)
                    .where(integration_batch_members.c.batch_id == result["batch_id"])
                    .order_by(integration_batch_members.c.ordinal)
                )
            )
            .mappings()
            .all()
        )
    expected = [task_id for task_id in leaf_ids if task_id not in {"root-050", "root-100"}]
    assert result["outcome"] == "sealed"
    assert [member["task_id"] for member in members] == expected
    assert [member["ordinal"] for member in members] == list(range(203))
    assert "root-150" in expected


async def test_failure_after_first_member_insert_rolls_back_every_sealing_write(db):
    from src.integration.scheduler import TrainService

    await _enable_train(db)
    await _seed_leaf(db, "root-a", "b" * 40)
    await _seed_leaf(db, "root-b", "c" * 40)
    request = await _request(db)
    async with injected_trigger(
        db,
        name="task8b_fail_second_member",
        table="integration_batch_members",
        event="INSERT",
        condition="NEW.ordinal = 1",
        body="RAISE EXCEPTION 'injected second member failure';",
    ):
        with pytest.raises((IntegrityError, DBAPIError), match="injected second member failure"):
            await TrainService(db, page_size=1).seal("p", request["request_id"], 20.0)

        async with db._engine.connect() as conn:
            assert (await conn.execute(select(integration_batches))).all() == []
            assert (await conn.execute(select(integration_batch_members))).all() == []
            assert (await conn.execute(select(project_integration_leases))).all() == []
            assert (await conn.execute(select(integration_repair_operations))).all() == []
            assert (
                await conn.execute(
                    select(integration_outbox.c.event_type).order_by(integration_outbox.c.id)
                )
            ).scalars().all() == []
            schedule = (
                (
                    await conn.execute(
                        select(project_integration_schedules).where(
                            project_integration_schedules.c.project_id == "p"
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert schedule["outstanding_request_id"] == request["request_id"]

    replay = await TrainService(db, page_size=1).seal("p", request["request_id"], 30.0)
    assert replay["outcome"] == "sealed"
    async with db._engine.connect() as conn:
        assert len((await conn.execute(select(integration_batches))).all()) == 1
        assert len((await conn.execute(select(integration_batch_members))).all()) == 2
        assert len((await conn.execute(select(integration_repair_operations))).all()) == 1
        assert (
            await conn.execute(
                select(integration_outbox.c.event_type).where(
                    integration_outbox.c.event_type == "integration.sealed"
                )
            )
        ).scalars().all() == ["integration.sealed"]


async def test_sealed_member_review_snapshot_is_immutable(db):
    from src.integration.scheduler import TrainService

    await _enable_train(db)
    await _seed_leaf(db, "root", "b" * 40)
    request = await _request(db)
    sealed = await TrainService(db).seal("p", request["request_id"], 20.0)

    with pytest.raises((IntegrityError, DBAPIError), match="membership is immutable"):
        async with db.immediate() as conn:
            await conn.execute(
                update(integration_batch_members)
                .where(integration_batch_members.c.batch_id == sealed["batch_id"])
                .values(review_evidence={"changed": True})
            )


async def test_integration_seal_command_is_typed_and_project_scoped(
    command_handler_factory,
):
    handler = await command_handler_factory()
    await handler.db.create_project(Project(id="p", name="integration project"))
    service = AsyncMock()
    service.seal.return_value = {
        "outcome": "sealed",
        "project_id": "p",
        "request_id": "integration-sweep:p:1",
        "batch_id": "batch-1",
        "operation_id": "repair-batch-batch-1",
    }
    handler.orchestrator.integration_train_service = service
    args = {
        "project_id": "p",
        "request_id": "integration-sweep:p:1",
        "now": 20.0,
    }

    session = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["integration_seal"]),
        project_id="p",
        session_id="session",
    )
    with principal_context(session):
        denied_session = await handler.execute("integration_seal", args)
    assert denied_session["outcome"] == "unauthorized"

    wrong_project = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["integration_seal"]),
        project_id="other",
    )
    with principal_context(wrong_project):
        denied_project = await handler.execute("integration_seal", args)
    assert denied_project["outcome"] == "unauthorized"

    capable = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["integration_seal"]),
        project_id="p",
    )
    with principal_context(capable):
        allowed = await handler.execute("integration_seal", args)
    assert allowed == {"success": True, **service.seal.return_value}
    service.seal.assert_awaited_once_with("p", "integration-sweep:p:1", 20.0)
    await handler.db.close()


async def test_seal_lock_excludes_descendant_reopen_until_frozen(concurrent_db, monkeypatch):
    from src.integration.scheduler import TrainService

    await _enable_train(concurrent_db)
    await _seed_exact_parent(concurrent_db)
    request = await _request(concurrent_db)
    first_page_scanned = asyncio.Event()
    finish_snapshot = asyncio.Event()
    original_page = concurrent_db.eligible_root_page_on
    calls = 0

    async def pause_after_first_page(*args, **kwargs):
        nonlocal calls
        page = await original_page(*args, **kwargs)
        calls += 1
        if calls == 1:
            first_page_scanned.set()
            await finish_snapshot.wait()
        return page

    monkeypatch.setattr(concurrent_db, "eligible_root_page_on", pause_after_first_page)
    seal_task = asyncio.create_task(
        TrainService(concurrent_db, page_size=1).seal("p", request["request_id"], 20.0)
    )
    await asyncio.wait_for(first_page_scanned.wait(), timeout=3)
    reopen_task = asyncio.create_task(concurrent_db.transition_task("child", TaskStatus.READY))
    await asyncio.sleep(0.05)
    assert not reopen_task.done()
    finish_snapshot.set()

    sealed = await asyncio.wait_for(seal_task, timeout=5)
    assert sealed["outcome"] == "sealed"
    with pytest.raises(HierarchyError) as rejected:
        await asyncio.wait_for(reopen_task, timeout=5)
    assert rejected.value.code == "sealed"
    assert (await concurrent_db.get_task("child")).status is TaskStatus.COMPLETED


async def test_descendant_reopen_before_seal_changes_fresh_snapshot(concurrent_db):
    from src.integration.scheduler import TrainService

    await _enable_train(concurrent_db)
    await _seed_exact_parent(concurrent_db)
    await concurrent_db.transition_task("child", TaskStatus.READY)
    request = await _request(concurrent_db)

    result = await TrainService(concurrent_db, page_size=1).seal("p", request["request_id"], 20.0)

    assert result["outcome"] == "empty"
    assert (await concurrent_db.get_task("child")).status is TaskStatus.READY


async def test_public_review_append_requires_existing_source_task_project(db):
    missing = _review_row("missing", "b" * 40, evidence_id="review-missing")

    with pytest.raises(ValueError, match="source task project does not exist"):
        await db.append_integration_review_evidence(missing)


async def test_postgres_sealer_first_freezes_approval_before_later_rejection(
    concurrent_db, monkeypatch
):
    if concurrent_db._engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL advisory-lock ordering only")
    from src.integration.scheduler import TrainService

    await _enable_train(concurrent_db)
    approval = await _seed_leaf(concurrent_db, "root", "b" * 40)
    request = await _request(concurrent_db)
    review_read = asyncio.Event()
    finish_seal = asyncio.Event()
    original_reviews = concurrent_db.latest_exact_reviews_on

    async def pause_after_review_read(*args, **kwargs):
        reviews = await original_reviews(*args, **kwargs)
        review_read.set()
        await finish_seal.wait()
        return reviews

    monkeypatch.setattr(concurrent_db, "latest_exact_reviews_on", pause_after_review_read)
    seal_task = asyncio.create_task(
        TrainService(concurrent_db).seal("p", request["request_id"], 20.0)
    )
    await asyncio.wait_for(review_read.wait(), timeout=3)
    rejection = _review_row(
        "root",
        "b" * 40,
        evidence_id="review-root-later-rejection",
        verdict="rejected",
        evidence={"decision": "rejected"},
        created_at=2.0,
    )
    append_task = asyncio.create_task(concurrent_db.append_integration_review_evidence(rejection))
    await asyncio.sleep(0.05)
    assert not append_task.done()
    finish_seal.set()

    sealed = await asyncio.wait_for(seal_task, timeout=5)
    await asyncio.wait_for(append_task, timeout=5)
    async with concurrent_db._engine.connect() as conn:
        member = (
            (
                await conn.execute(
                    select(integration_batch_members).where(
                        integration_batch_members.c.batch_id == sealed["batch_id"]
                    )
                )
            )
            .mappings()
            .one()
        )
    assert sealed["outcome"] == "sealed"
    assert member["review_evidence_id"] == approval["id"]


async def test_postgres_rejection_writer_first_makes_seal_exclude_root(concurrent_db, monkeypatch):
    if concurrent_db._engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL advisory-lock ordering only")
    from src.integration.scheduler import TrainService

    await _enable_train(concurrent_db)
    await _seed_leaf(concurrent_db, "root", "b" * 40)
    request = await _request(concurrent_db)
    writer_has_lock = asyncio.Event()
    finish_writer = asyncio.Event()
    original_lock = concurrent_db.lock_hierarchy_project

    async def pause_review_writer(conn, project_id):
        await original_lock(conn, project_id)
        if asyncio.current_task().get_name() == "task8b-review-writer":
            writer_has_lock.set()
            await finish_writer.wait()

    monkeypatch.setattr(concurrent_db, "lock_hierarchy_project", pause_review_writer)
    rejection = _review_row(
        "root",
        "b" * 40,
        evidence_id="review-root-first-rejection",
        verdict="rejected",
        evidence={"decision": "rejected"},
        created_at=2.0,
    )
    append_task = asyncio.create_task(
        concurrent_db.append_integration_review_evidence(rejection),
        name="task8b-review-writer",
    )
    await asyncio.wait_for(writer_has_lock.wait(), timeout=3)
    seal_task = asyncio.create_task(
        TrainService(concurrent_db).seal("p", request["request_id"], 20.0)
    )
    await asyncio.sleep(0.05)
    assert not seal_task.done()
    finish_writer.set()

    await asyncio.wait_for(append_task, timeout=5)
    sealed = await asyncio.wait_for(seal_task, timeout=5)
    assert sealed["outcome"] == "empty"


async def test_immediate_transition_preserves_ordinary_status_and_post_commit_callback(db):
    ready_events = []

    async def on_ready(entries):
        ready_events.extend(entries)

    db.set_ready_listener(on_ready)
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                **_task_row(
                    "ordinary",
                    status=TaskStatus.DEFINED.value,
                    integration_mode=None,
                    pr_url=None,
                )
            )
        )

    await db.transition_task("ordinary", TaskStatus.READY, context="ordinary-test")

    assert (await db.get_task("ordinary")).status is TaskStatus.READY
    assert ready_events == [("ordinary", "promoted")]


@pytest.mark.parametrize("expired", [False, True])
async def test_scheduler_maintains_batch_lease_before_next_sweep(db, expired):
    from src.integration.scheduler import IntegrationScheduler, TrainService

    await _enable_train(db)
    await _seed_leaf(db, "root", "b" * 40)
    scheduler = IntegrationScheduler(db, clock=lambda: now)
    await scheduler.configure(project_id="p", now=1.0, enabled=True, interval_seconds=3600)
    request = await _request(db)
    sealed = await TrainService(db).seal("p", request["request_id"], 20.0)
    now = 400.0 if expired else 200.0
    due = await db.due_integration_schedule_page(now=now, after=None, limit=10)
    assert [row["project_id"] for row in due] == ["p"]
    await scheduler.mark_due("p", now, "periodic")
    async with db._engine.connect() as conn:
        first_lease = dict(
            (await conn.execute(select(project_integration_leases))).mappings().one()
        )
    await scheduler.mark_due("p", now + 1, "periodic")
    async with db._engine.connect() as conn:
        lease = (await conn.execute(select(project_integration_leases))).mappings().one()
        events = (
            (
                await conn.execute(
                    select(integration_outbox).where(
                        integration_outbox.c.event_type == "integration.sealed"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert lease["batch_id"] == sealed["batch_id"]
    assert dict(lease) == first_lease
    assert await db.due_integration_schedule_page(now=now + 1, after=None, limit=10) == []
    assert lease["expires_at"] > now + 150
    assert lease["fence_token"] == (2 if expired else 1)
    assert len(events) == (2 if expired else 1)
    if expired:
        recovered = next(e for e in events if "lease" in e["id"])
        assert recovered["payload"]["batch_id"] == sealed["batch_id"]
        assert recovered["payload"]["operation_id"]


@pytest.mark.parametrize("expired", [False, True])
async def test_scheduler_does_not_renew_or_recover_another_lease_owner(db, expired):
    from src.integration.scheduler import IntegrationScheduler, TrainService

    await _enable_train(db)
    await _seed_leaf(db, "root", "b" * 40)
    scheduler = IntegrationScheduler(db, clock=lambda: now)
    await scheduler.configure(project_id="p", now=1.0, enabled=True, interval_seconds=3600)
    request = await _request(db)
    await TrainService(db).seal("p", request["request_id"], 20.0)
    now = 400.0 if expired else 200.0
    async with db.immediate() as conn:
        await conn.execute(update(project_integration_leases).values(owner_id="another-owner"))
        before = dict((await conn.execute(select(project_integration_leases))).mappings().one())
    assert await db.due_integration_schedule_page(now=now, after=None, limit=10) == []
    await scheduler.mark_due("p", now, "periodic")
    async with db._engine.connect() as conn:
        after = dict((await conn.execute(select(project_integration_leases))).mappings().one())
        events = (
            (
                await conn.execute(
                    select(integration_outbox).where(
                        integration_outbox.c.event_type == "integration.sealed"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert after == before


@pytest.mark.parametrize("draining", [False, True])
@pytest.mark.parametrize("expired", [False, True])
async def test_disabled_schedule_maintains_existing_batch_without_new_sweep(db, draining, expired):
    from src.integration.scheduler import IntegrationScheduler, TrainService

    await _enable_train(db)
    await _seed_leaf(db, "root", "b" * 40)
    scheduler = IntegrationScheduler(db, clock=lambda: now)
    await scheduler.configure(project_id="p", now=1, enabled=True, interval_seconds=3600)
    request = await _request(db)
    sealed = await TrainService(db).seal("p", request["request_id"], 20)
    await scheduler.configure(project_id="p", now=21, enabled=False)
    if draining:
        async with db.immediate() as conn:
            from src.database.tables import projects
            await conn.execute(update(projects).where(projects.c.id == "p").values(
                hierarchical_integration_draining=True,
                hierarchical_integration_desired_mode="disabled",
            ))
    now = 400 if expired else 200
    assert [row["project_id"] for row in await db.due_integration_schedule_page(
        now=now, after=None, limit=10
    )] == ["p"]
    result = await scheduler.mark_due("p", now, "periodic")
    assert result["outcome"] == "disabled"
    async with db._engine.connect() as conn:
        lease = (await conn.execute(select(project_integration_leases))).mappings().one()
        schedule = (await conn.execute(select(project_integration_schedules))).mappings().one()
        events = (await conn.execute(select(integration_outbox).where(
            integration_outbox.c.event_type == "integration.sweep_due"
        ))).mappings().all()
    assert lease["batch_id"] == sealed["batch_id"]
    assert lease["expires_at"] == now + 300
    assert schedule["outstanding_request_id"] == request["request_id"]
    assert schedule["request_sequence"] == request["request_sequence"]
    assert events == []


@pytest.fixture(autouse=True)
def reconciler_primitive_authority(monkeypatch):
    from tests.integration_primitive_scope import authorize_root_primitives
    authorize_root_primitives(monkeypatch)


async def test_reconciler_seals_live_green_without_review_or_source_ci_rows(db, monkeypatch):
    from src.integration.engine import root_admission
    from src.integration.scheduler import TrainService
    from src.integration.subjects import AdmissionPredicate

    policy = await _enable_train(db)
    policy['root']['repair']['source_ci'] = True
    await db.update_project('p', hierarchical_integration_policy=policy)
    await _seed_leaf(db, 'green', '1' * 40, with_review=False)
    await _seed_leaf(db, 'closed', '2' * 40, with_review=False)
    await _seed_leaf(db, 'pending', '3' * 40, with_review=False)
    request = await _request(db)

    async def observe(member, policy):
        if member['task_id'] == 'closed':
            return {'reason': 'pr_closed'}
        if member['task_id'] == 'pending':
            return {'reason': 'source_ci_pending'}
        return {'reason': None, 'tree': '4' * 40, 'checks': {'head_sha': member['source_head']},
                'target_sha': BASE_SHA, 'observed_at': 15.0}

    monkeypatch.setattr("src.integration.engine.current_admission",
                        lambda: AdmissionPredicate(require_source_ci=True))
    with root_admission(AdmissionPredicate(require_source_ci=True)):
        service = TrainService(db, admission_reader=observe)
        result = await service.seal('p', request['request_id'], 20)
        assert await service.seal('p', request['request_id'], 21) == result
    assert result['outcome'] == 'sealed'
    assert {row['task_id']: row['reason'] for row in result['exclusions']} == {
        'closed': 'pr_closed', 'pending': 'source_ci_pending',
    }
    async with db._engine.connect() as conn:
        member = (await conn.execute(select(integration_batch_members))).mappings().one()
        review = (await conn.execute(select(integration_review_evidence))).mappings().one()
    assert member['task_id'] == 'green'
    assert member['review_evidence'] == dict(review)
    assert review['evidence']['decision_path'] == 'git_source_ci'
    assert review['reviewer_identity'] == 'service:root-reconciler'


@pytest.mark.parametrize('mutation, reason', [
    ('head', 'source_identity_changed'), ('policy', 'policy_changed'),
    ('rejection', 'review_rejected'), ('unavailable', 'source_observation_unavailable'),
    ('delivered', 'already_delivered'), ('hold', 'source_no_longer_eligible'),
])
async def test_reconciler_seal_explains_exclusions_and_rechecks_fresh_identity(
    db, mutation, reason, monkeypatch,
):
    from src.integration.engine import root_admission
    from src.integration.scheduler import TrainService
    from src.integration.subjects import AdmissionPredicate

    await _enable_train(db)
    await _seed_leaf(db, 'first', '1' * 40)
    request = await _request(db)

    async def observe(member, policy):
        async with db.immediate() as conn:
            if mutation == 'head':
                await conn.execute(update(task_integration_checkpoints).values(checkpoint_sha='2' * 40))
            elif mutation == 'policy':
                await conn.execute(update(projects).values(hierarchical_integration_generation=999))
            elif mutation == 'hold':
                await conn.execute(insert(task_labels).values(task_id='first', label='hold:human'))
            elif mutation == 'rejection':
                await conn.execute(insert(integration_review_evidence).values(**_review_row(
                    'first', '1' * 40, evidence_id='rejected', verdict='rejected', created_at=99,
                )))
        return {'reason': 'already_delivered' if mutation == 'delivered' else None,
                'tree': '4' * 40, 'checks': {}, 'observed_at': 15.0}

    monkeypatch.setattr("src.integration.engine.current_admission", lambda: AdmissionPredicate())
    with root_admission(AdmissionPredicate()):
        result = await TrainService(db, admission_reader=None if mutation == 'unavailable' else observe
                                    ).seal('p', request['request_id'], 20)
    assert result['outcome'] == 'empty'
    assert result['exclusions'] == [{'task_id': 'first',
                                     'head_sha': ('2' if mutation == 'head' else '1') * 40,
                                     'reason': reason}]


@pytest.mark.parametrize('condition, expected', [
    ('green', None), ('closed', 'pr_closed'), ('moved', 'pr_identity_changed'),
    # PR CI is never consulted before admission: red, pending, foreign and
    # check-run outages all admit; batch candidate CI is the only gate.
    ('foreign', None), ('red', None), ('rerun', None),
    ('delivered', 'already_delivered'),
    ('unknown', 'ancestry_unknown'), ('outage', None),
])
async def test_live_root_admission_ignores_pr_ci_and_reads_git(condition, expected):
    from contextlib import asynccontextmanager
    from src.git.manager import RemoteRefState
    from src.integration.source_ci import RootAdmissionReader

    @asynccontextmanager
    async def transaction(store):
        yield

    member = {'repository_id': 'repo', 'pr_url': 'https://github.com/example/repo/pull/1',
              'source_head': '1' * 40, 'source_branch': 'aq/source', 'default_branch': 'main'}
    pull = {'state': 'closed' if condition == 'closed' else 'open',
            'head': {'sha': ('2' if condition == 'moved' else '1') * 40,
                     'ref': 'aq/source', 'repo': {'id': 9}},
            'base': {'ref': 'main', 'repo': {'id': 9}}}
    check = {'id': 1, 'name': 'unit', 'head_sha': '1' * 40, 'status': 'completed',
             'conclusion': 'failure' if condition == 'red' else 'success',
             'app': {'id': 'other' if condition == 'foreign' else 'forge'}}
    checks = [check]
    if condition == 'rerun':
        checks.append({**check, 'id': 2, 'status': 'in_progress', 'conclusion': None})
    client = SimpleNamespace(pull_request=AsyncMock(return_value=pull),
                             commit_check_runs=AsyncMock(return_value=checks))
    if condition == 'outage':
        client.commit_check_runs.side_effect = RuntimeError('network unavailable')
    git = SimpleNamespace(
        bind_github_repository=AsyncMock(return_value=SimpleNamespace(repository_id=9)),
        _github_client=lambda binding: client, arepository_transaction=transaction,
        als_remote_ref=AsyncMock(side_effect=[
            SimpleNamespace(state=RemoteRefState.PRESENT, oid='1' * 40),
            SimpleNamespace(state=RemoteRefState.PRESENT, oid=BASE_SHA),
        ]),
        ais_ancestor=AsyncMock(return_value=None if condition == 'unknown' else condition == 'delivered'),
    )
    promotion = SimpleNamespace(
        git=git, _resolve_repository=AsyncMock(return_value=SimpleNamespace(
            origin_url='https://github.com/example/repo', retained_git_dir='/retained',
        )), _ensure_retained_repository=AsyncMock(), _fetch_all_heads=AsyncMock(),
        _tree_oid=AsyncMock(return_value='3' * 40),
    )
    result = await RootAdmissionReader(promotion)(member, HierarchicalIntegrationPolicy.model_validate(_policy()))
    assert result['reason'] == expected
    if expected is None:
        assert result['tree'] == '3' * 40
        client.commit_check_runs.assert_not_awaited()
    elif expected.startswith('pr_'):
        git.ais_ancestor.assert_not_awaited()


async def test_root_frontier_prunes_incomplete_delivered_and_closed_sources(db):
    from src.integration.observe import ObservationRows
    from src.integration.root_runtime import _RootObservationReader
    from src.integration.subjects import PolicyArtifactPin, Subject, SubjectKind, SubjectSchedule

    await _enable_train(db)
    for index, name in enumerate(('open', 'closed', 'incomplete', 'delivered'), start=1):
        await _seed_leaf(db, name, str(index) * 40)
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == 'incomplete').values(status='READY'))
        await conn.execute(insert(task_delivery_receipts).values(
            id='delivered', domain_key='delivered', source_task_id='delivered',
            repository_id='repo', target_branch='main', disposition='code', created_at=1,
        ))
    subject = Subject(
        id='subject', project_id='p', repository_id='repo', kind=SubjectKind.ROOT_BATCH,
        subject_key='root_batch:repo:request', target_ref='refs/heads/main', phase='admitting',
        schedule=SubjectSchedule.progress(now=1, max_wait_seconds=60),
        policy=PolicyArtifactPin(playbook_id='test', artifact_sha256='sha256:' + '1' * 64),
        created_at=1, updated_at=1,
    )
    snapshot = ObservationRows(subject=subject, project={}, repository={}, rows={
        'tasks': tuple(_task_row(name) for name in ('open', 'closed', 'incomplete', 'delivered')),
    })
    reader = _RootObservationReader(db, open_prs=AsyncMock(return_value={_task_row('open')['pr_url']}))
    reader.reader = SimpleNamespace(read=AsyncMock(return_value=snapshot))
    result = await reader.read('subject')
    assert [row['id'] for row in result.all('tasks')] == ['open']


async def test_root_admission_batches_closed_pr_pruning_without_reading_each_head():
    from src.integration.source_ci import RootAdmissionReader

    client = SimpleNamespace(paged_list=AsyncMock(return_value=[]))
    git = SimpleNamespace(bind_github_repository=AsyncMock(return_value=SimpleNamespace(repository_id=9)),
                          _github_client=lambda binding: client)
    promotion = SimpleNamespace(git=git, _resolve_repository=AsyncMock(return_value=SimpleNamespace(
        origin_url='https://github.com/example/repo',
    )))
    members = [{'repository_id': 'repo', 'pr_url': f'https://github.com/example/repo/pull/{i}'}
               for i in range(352)]
    result = await RootAdmissionReader(promotion).observe_many(members, None)
    assert result == [{'reason': 'pr_closed'}] * 352
    client.paged_list.assert_awaited_once()
    git.bind_github_repository.assert_awaited_once()
