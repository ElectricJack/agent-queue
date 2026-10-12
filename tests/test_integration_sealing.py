"""Retained migration inspection and exact root source projections."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, insert, select, update

from src.database.tables import (
    gates,
    integration_batch_members,
    integration_batches,
    integration_parent_episodes,
    integration_parent_operation_completions,
    integration_parent_verifications,
    integration_repair_operations,
    integration_review_evidence,
    task_branch_origins,
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
from tests.pg_dsn import ensure_worker_postgres_dsn
from src.integration.migration_heads import MigrationHead, declaration, select_members

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


async def test_public_review_append_requires_existing_source_task_project(db):
    missing = _review_row("missing", "b" * 40, evidence_id="review-missing")

    with pytest.raises(ValueError, match="source task project does not exist"):
        await db.append_integration_review_evidence(missing)


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


@pytest.fixture(autouse=True)
def reconciler_primitive_authority(monkeypatch):
    from tests.integration_primitive_scope import authorize_root_primitives
    authorize_root_primitives(monkeypatch)


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
