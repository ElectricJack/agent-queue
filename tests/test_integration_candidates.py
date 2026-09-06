"""Ordered, restartable root integration candidate construction."""

from __future__ import annotations

import asyncio
import subprocess
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import delete, func, insert, select, update

from src.database import Database
from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_member_results,
    integration_candidate_publications,
    integration_candidate_ref_mutations,
    integration_candidate_revisions,
    integration_repair_operations,
    integration_repair_stages,
    integration_review_evidence,
    playbook_artifacts,
    project_integration_leases,
    sessions,
    tasks,
    workspaces,
)
from src.integration.models import (
    ArtifactSnapshot,
    HierarchicalIntegrationPolicy,
    IntegrationBoundaryPolicy,
    PlaybookRoute,
    RepairPolicy,
    RequiredCheckSet,
)
from src.models import AgentProfile, Project, RepoConfig, RepoSourceType, SessionRecord


BASE = "a" * 40


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _artifact() -> ArtifactSnapshot:
    return ArtifactSnapshot(
        playbook_id="root-train",
        artifact_sha256="sha256:" + "1" * 64,
        schema_generation=2,
        contract_fingerprint="sha256:" + "2" * 64,
        source_digest="sha256:" + "3" * 64,
        compiler_build="task9b1-test",
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
            playbook_id="root-train",
            scope="project",
            scope_identifier="p",
            activation_id="activation",
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
async def db(tmp_path):
    database = Database(str(tmp_path / "candidates.db"))
    await database.initialize()
    await database.create_profile(AgentProfile(id="repairer", name="Repairer"))
    await database.create_profile(AgentProfile(id="debugger", name="Debugger"))
    await database.create_project(Project(id="p", name="project"))
    await database.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url=str(tmp_path / "origin.git"),
            default_branch="main",
        )
    )
    await database.update_project(
        "p",
        hierarchical_integration_mode="train",
        integration_repository_id="repo",
        hierarchical_integration_policy=_policy(),
    )
    async with database.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                **_artifact().model_dump(),
                scope="project",
                scope_identifier="p",
                profile_fingerprint="",
                path="/tmp/task9b1-artifact",
                size_bytes=1,
                validation="{}",
                created_at=1.0,
            )
        )
    yield database
    await database.close()


async def _seed_batch(db, *, lifecycle="sealed", members=(), base_sha=BASE):
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="batch",
                project_id="p",
                repository_id="repo",
                request_id="request",
                trigger="manual",
                source_manifest_digest="sha256:" + "4" * 64,
                base_sha=base_sha if lifecycle != "empty" else None,
                lifecycle="sealing" if lifecycle != "empty" else "empty",
                current_revision=0,
                integration_branch=(
                    "refs/heads/aq/integration/p-" + "5" * 32 + "/r-" + "6" * 32
                    if lifecycle != "empty"
                    else None
                ),
                policy_snapshot=_policy(),
                artifact_snapshot=_artifact().model_dump(mode="json"),
                cleanup_state="pending" if lifecycle != "empty" else "complete",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        for ordinal, member in enumerate(members):
            await conn.execute(
                insert(integration_review_evidence).values(
                    id=f"review-{ordinal}",
                    source_task_id=f"root-{ordinal}",
                    repository_id="repo",
                    source_base=member[0],
                    reviewed_head_sha=member[1],
                    reviewed_tree_sha=member[2],
                    reviewer_task_id=f"reviewer-{ordinal}",
                    reviewer_session_attempt_id=None,
                    review_kind="leaf",
                    generation=1,
                    verdict="approved",
                    evidence={"decision": "approved"},
                    created_at=1.0,
                )
            )
            await conn.execute(
                insert(integration_batch_members).values(
                    batch_id="batch",
                    ordinal=ordinal,
                    task_id=f"root-{ordinal}",
                    pr_url=f"https://example.test/pull/{ordinal + 1}",
                    repository_id="repo",
                    source_base_sha=member[0],
                    reviewed_head_sha=member[1],
                    reviewed_tree_sha=member[2],
                    review_evidence_id=f"review-{ordinal}",
                    review_evidence={
                        "id": f"review-{ordinal}",
                        "authors": [f"Author {ordinal} <a{ordinal}@example.test>"],
                    },
                )
            )
        if lifecycle != "empty":
            await conn.execute(
                update(integration_batches)
                .where(integration_batches.c.id == "batch")
                .values(lifecycle=lifecycle)
            )
            await conn.execute(
                insert(project_integration_leases).values(
                    project_id="p",
                    repository_id="repo",
                    batch_id="batch",
                    owner_id="sealer",
                    fence_token=1,
                    heartbeat_at=1.0,
                    expires_at=1000.0,
                )
            )
        if lifecycle != "empty":
            await conn.execute(
                insert(integration_repair_operations).values(
                    id="repair-batch-batch",
                    target_kind="batch",
                    batch_id="batch",
                    episode_id="batch",
                    active_stage=0,
                    state="active",
                    policy_snapshot=_policy(),
                    artifact_snapshot=_artifact().model_dump(mode="json"),
                    required_check_version="checks-v1",
                    route_playbook_id="root-train",
                    route_scope="project",
                    route_scope_identifier="p",
                    route_activation_id="activation",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
            await conn.execute(
                insert(integration_branch_owners).values(
                    id="candidate-owner",
                    repository_id="repo",
                    ref="refs/heads/aq/integration/p-" + "5" * 32 + "/r-" + "6" * 32,
                    owner_id="repair-batch-batch",
                    owner_role="collector",
                    fence_token=1,
                    handoff_state="reserved",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )


async def test_empty_batch_build_replays_typed_terminal_outcome(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateBuildResult, CandidateService

    await _seed_batch(db, lifecycle="empty")
    app = _AppClient()
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    git = _LocalPushGit(tmp_path / "unused.git")
    forge = _AuditForge()
    service = CandidateService(
        db,
        data_dir=tmp_path,
        git_manager=git,
        app_client=app,
        forge_provider=forge,
    )

    first = await service.build("batch")
    replay = await service.build("batch")

    assert (
        first
        == replay
        == CandidateBuildResult(
            outcome="empty",
            batch_id="batch",
            revision=0,
            operation_id=None,
        )
    )
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_candidate_revisions))).all() == []
        assert (await conn.execute(select(integration_candidate_member_results))).all() == []
        assert (await conn.execute(select(integration_repair_stages))).all() == []
    assert git.pushes == []
    assert forge.calls == []


def _make_origin(tmp_path: Path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.name", "Candidate Test")
    _git(work, "config", "user.email", "candidate@example.test")
    (work / "base.txt").write_text("base\n")
    _git(work, "add", "base.txt")
    _git(work, "commit", "-m", "base")
    base = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")
    members = []
    for ordinal in range(2):
        _git(work, "switch", "-C", f"root-{ordinal}", base)
        (work / f"member-{ordinal}.txt").write_text(f"member {ordinal}\n")
        _git(work, "add", f"member-{ordinal}.txt")
        message = f"member {ordinal}"
        if ordinal == 1:
            message += "\n\nCo-authored-by: Pair Author <pair@example.test>"
        _git(work, "commit", "-m", message)
        head = _git(work, "rev-parse", "HEAD")
        tree = _git(work, "rev-parse", f"{head}^{{tree}}")
        _git(work, "push", "origin", f"HEAD:refs/heads/root-{ordinal}")
        members.append((base, head, tree))
    return origin, work, base, members


def _make_conflicting_origin(tmp_path: Path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.name", "Candidate Test")
    _git(work, "config", "user.email", "candidate@example.test")
    (work / "shared.txt").write_text("base\n")
    _git(work, "add", "shared.txt")
    _git(work, "commit", "-m", "base")
    base = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")
    members = []
    for ordinal, text in enumerate(("first\n", "second\n")):
        _git(work, "switch", "-C", f"root-{ordinal}", base)
        (work / "shared.txt").write_text(text)
        _git(work, "add", "shared.txt")
        _git(work, "commit", "-m", f"member {ordinal}")
        head = _git(work, "rev-parse", "HEAD")
        tree = _git(work, "rev-parse", f"{head}^{{tree}}")
        _git(work, "push", "origin", f"HEAD:refs/heads/root-{ordinal}")
        members.append((base, head, tree))
    return origin, work, base, members


class _AuditForge:
    def __init__(self, backing=None):
        self.backing = backing if backing is not None else {"result": None, "calls": []}

    @property
    def calls(self):
        return self.backing["calls"]

    async def lookup_audit_pr(self, *, idempotency_key):
        result = self.backing["result"]
        if result is not None and result.idempotency_key == idempotency_key:
            return result
        return None

    async def create_audit_pr(self, **kwargs):
        from src.integration.candidates import AuditPullRequest

        if self.backing["result"] is None:
            self.calls.append(kwargs)
            self.backing["result"] = AuditPullRequest(
                url="https://github.com/example/repo/pull/9",
                number=9,
                head_sha=kwargs["head_sha"],
                head_branch=kwargs["branch"],
                base_branch=kwargs["base_branch"],
                repository_numeric_id=kwargs["repository_numeric_id"],
                repository_full_name=kwargs["repository_full_name"],
                idempotency_key=kwargs["idempotency_key"],
            )
        return self.backing["result"]


class _AppClient:
    repository = None

    def __init__(self, origin=None):
        self.origin = origin

    async def installation_token(self):
        return "dummy-token"

    async def exact_head_ref(self, branch):
        if self.origin is None:
            return None
        result = subprocess.run(
            ["git", "--git-dir", str(self.origin), "rev-parse", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None


class _LocalPushGit:
    trusted_local = True

    def __init__(self, origin):
        from src.git.manager import GitManager

        self.delegate = GitManager()
        self.origin = origin
        self.pushes = []

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def apush_oid_with_app_auth(self, checkout_path, **kwargs):
        self.pushes.append({"checkout_path": checkout_path, **kwargs})
        result = await self.delegate.arun_git_result(
            [
                "push",
                str(self.origin),
                f"--force-with-lease=refs/heads/{kwargs['branch']}:{kwargs['expected_old_oid']}",
                f"{kwargs['tip_oid']}:refs/heads/{kwargs['branch']}",
            ],
            cwd=checkout_path,
        )
        assert result.returncode == 0, result.stderr
        return kwargs["tip_oid"]

    async def afetch_exact_oid_with_app_auth(self, destination_git_dir, **kwargs):
        result = await self.delegate.arun_git_result(
            [
                "fetch",
                "--no-tags",
                "--force",
                str(self.origin),
                f"{kwargs['oid']}:{kwargs['destination_ref']}",
            ],
            cwd=destination_git_dir,
        )
        assert result.returncode == 0, result.stderr
        return kwargs["oid"]


class _CrashOnce:
    def __init__(self, point):
        self.point = point
        self.crashed = False

    def __call__(self, point):
        if point == self.point and not self.crashed:
            self.crashed = True
            raise RuntimeError(f"crash at {point}")


async def test_many_members_build_in_ordinal_order_without_moving_sources(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members, base_sha=base)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    git = _LocalPushGit(origin)
    forge = _AuditForge()

    result = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=git,
        forge_provider=forge,
        app_client=app,
        clock=lambda: 100.0,
    ).build("batch")
    replay = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=git,
        forge_provider=forge,
        app_client=app,
        clock=lambda: 100.0,
    ).build("batch")

    assert result.outcome == "built"
    assert result.revision == 0
    assert result.head_sha and result.head_sha != base
    assert _git(origin, "ls-tree", "--name-only", result.head_sha).splitlines() == [
        "base.txt",
        "member-0.txt",
        "member-1.txt",
    ]
    assert _git(origin, "rev-parse", "refs/heads/root-0") == members[0][1]
    assert _git(origin, "rev-parse", "refs/heads/root-1") == members[1][1]
    store = next((tmp_path / "data" / "integration-repositories").iterdir())
    source_namespace = (
        "refs/aq/integration-sources/" + __import__("hashlib").sha256(b"batch").hexdigest()
    )
    assert _git(store, "rev-parse", f"{source_namespace}/0/base") == base
    assert _git(store, "rev-parse", f"{source_namespace}/0/head") == members[0][1]
    assert _git(store, "rev-parse", f"{source_namespace}/1/head") == members[1][1]
    candidate_message = _git(origin, "show", "-s", "--format=%B", result.head_sha)
    assert f"Reviewed-head: {members[1][1]}" in candidate_message
    assert "Review-evidence: review-1" in candidate_message
    assert "Co-authored-by: Pair Author <pair@example.test>" in candidate_message
    assert _git(origin, "show", "-s", "--format=%ae", result.head_sha) in {
        "candidate@example.test",
        "pair@example.test",
    }
    async with db._engine.connect() as conn:
        revision = (await conn.execute(select(integration_candidate_revisions))).mappings().one()
        applied = (
            (
                await conn.execute(
                    select(integration_candidate_member_results).order_by(
                        integration_candidate_member_results.c.member_ordinal
                    )
                )
            )
            .mappings()
            .all()
        )
    assert revision["state"] == "built"
    assert revision["next_member_ordinal"] == 2
    assert [row["result"] for row in applied] == ["applied", "applied"]
    assert [row["input_head_sha"] for row in applied] == [members[0][1], members[1][1]]
    assert replay.outcome == "already_built"
    assert replay.head_sha == result.head_sha
    assert replay.pr_url == "https://github.com/example/repo/pull/9"
    assert len(git.pushes) == 1
    assert (
        git.pushes[0]
        | {
            "tip_oid": result.head_sha,
            "branch": "aq/integration/p-" + "5" * 32 + "/r-" + "6" * 32,
            "expected_old_oid": "0" * 40,
        }
        == git.pushes[0]
    )
    assert len(forge.calls) == 1


async def test_one_member_build_and_local_replay_are_deterministic(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    service = CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: 100.0,
    )

    built = await service.build("batch")
    replay = await service.build("batch")

    assert built.outcome == "built"
    assert replay.outcome == "already_built"
    assert replay.head_sha == built.head_sha
    assert built.head_sha
    store = next((tmp_path / "data" / "integration-repositories").iterdir())
    assert _git(store, "ls-tree", "--name-only", built.head_sha).splitlines() == [
        "base.txt",
        "member-0.txt",
    ]


async def test_conflict_dispatches_and_rejects_caller_supplied_lineage(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import (
        CandidateRepairLineage,
        CandidateService,
    )
    from src.integration.repair import RepairService

    origin, work, base, members = _make_conflicting_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members, base_sha=base)
    repair = RepairService(db, route_validator=lambda _intelligence_class, _profile_id: True)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    service = CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        repair_service=repair,
        clock=lambda: 100.0,
    )

    conflict = await service.build("batch")
    replayed_conflict = await service.build("batch")

    assert conflict.outcome == "conflict"
    assert replayed_conflict.outcome == "wait"
    assert conflict.member_ordinal == 1
    assert conflict.head_sha
    async with db._engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(integration_candidate_member_results).order_by(
                        integration_candidate_member_results.c.member_ordinal
                    )
                )
            )
            .mappings()
            .all()
        )
        batch = (
            (
                await conn.execute(
                    select(integration_batches).where(integration_batches.c.id == "batch")
                )
            )
            .mappings()
            .one()
        )
    assert [row["result"] for row in rows] == ["applied", "conflict"]
    evidence = rows[1]["conflict_evidence"]
    assert (
        evidence
        | {
            "operation_id": "repair-batch-batch",
            "operation_stage": 0,
        }
        == evidence
    )
    assert evidence["batch_id"] == "batch"
    assert evidence["revision"] == 0
    assert evidence["partial_head_sha"] == conflict.head_sha
    assert evidence["source_base_sha"] == members[1][0]
    assert evidence["source_head_sha"] == members[1][1]
    assert batch["lifecycle"] == "repairing"

    store = next((tmp_path / "data" / "integration-repositories").iterdir())
    _git(work, "fetch", str(store), conflict.head_sha)
    _git(work, "switch", "--detach", "FETCH_HEAD")
    (work / "shared.txt").write_text("first and second\n")
    _git(work, "add", "shared.txt")
    _git(work, "commit", "-m", "resolve member 1")
    repaired = _git(work, "rev-parse", "HEAD")
    _git(work, "push", str(store), f"HEAD:refs/aq/test-repair/{repaired}")
    lineage = CandidateRepairLineage(
        batch_id="batch",
        revision=0,
        member_ordinal=1,
        operation_id="repair-batch-batch",
        operation_stage=0,
        partial_head_sha=conflict.head_sha,
        source_base_sha=members[1][0],
        source_head_sha=members[1][1],
        resolved_head_sha=repaired,
        repair_commit_shas=(repaired,),
    )

    from src.integration.candidates import CandidateAuthorizationError

    with pytest.raises(CandidateAuthorizationError):
        await service.accept_repair(lineage)


@pytest.mark.parametrize(
    "crash_point",
    (
        "after_member_mutation",
        "after_member_progress",
        "after_candidate_push",
        "after_audit_pr_create",
        "after_audit_pr_write",
    ),
)
async def test_build_restarts_at_every_persisted_external_boundary(db, tmp_path, crash_point):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members, base_sha=base)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    git = _LocalPushGit(origin)
    forge = _AuditForge()
    crashed = CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=git,
        forge_provider=forge,
        app_client=app,
        crash_hook=_CrashOnce(crash_point),
        clock=lambda: 100.0,
    )

    with pytest.raises(RuntimeError, match=f"crash at {crash_point}"):
        await crashed.build("batch")
    replay_forge = (
        _AuditForge(forge.backing) if crash_point == "after_audit_pr_create" else forge
    )
    completed = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=git,
        forge_provider=replay_forge,
        app_client=app,
        clock=lambda: 100.0,
    ).build("batch")

    assert completed.outcome in {"built", "already_built"}
    assert completed.pr_url == "https://github.com/example/repo/pull/9"
    assert len(git.pushes) == 1
    assert len(forge.calls) == 1
    async with db._engine.connect() as conn:
        rows = (await conn.execute(select(integration_candidate_member_results))).mappings().all()
    assert len(rows) == 2
    assert {row["result"] for row in rows} == {"applied"}


async def test_conflict_partial_is_published_before_repair_handoff(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService
    from src.integration.repair import RepairService

    origin, work, base, members = _make_conflicting_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members, base_sha=base)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    service = CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        repair_service=RepairService(db),
        clock=lambda: 100.0,
    )
    conflict = await service.build("batch")
    assert conflict.head_sha
    assert _git(origin, "rev-parse", conflict.branch) == conflict.head_sha


async def test_same_owner_concurrent_builds_never_duplicate_external_mutation(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    git = _LocalPushGit(origin)
    forge = _AuditForge()
    services = [
        CandidateService(
            db,
            data_dir=tmp_path / "data",
            git_manager=git,
            forge_provider=forge,
            app_client=app,
            clock=lambda: 100.0,
        )
        for _ in range(2)
    ]

    results = await asyncio.gather(*(service.build("batch") for service in services))

    assert {result.outcome for result in results} <= {"built", "already_built", "wait"}
    assert any(result.outcome in {"built", "already_built"} for result in results)
    assert len(git.pushes) == 1
    assert len(forge.calls) == 1


async def test_stage_change_before_conflict_cas_cannot_mark_new_stage_repairing(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_conflicting_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members, base_sha=base)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    service = CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: 100.0,
    )
    original = service._conflict

    async def change_stage(*args, **kwargs):
        async with db.immediate() as conn:
            await conn.execute(
                update(integration_repair_operations)
                .where(integration_repair_operations.c.id == "repair-batch-batch")
                .values(active_stage=1)
            )
        return await original(*args, **kwargs)

    service._conflict = change_stage
    result = await service.build("batch")

    assert result.outcome == "wait"
    async with db._engine.connect() as conn:
        batch = (
            await conn.execute(select(integration_batches).where(integration_batches.c.id == "batch"))
        ).mappings().one()
        conflict_rows = (
            await conn.execute(
                select(integration_candidate_member_results).where(
                    integration_candidate_member_results.c.result == "conflict"
                )
            )
        ).all()
    assert batch["lifecycle"] == "building"
    assert conflict_rows == []


async def test_partial_push_crash_reconciles_from_fresh_service_before_dispatch(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_conflicting_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members, base_sha=base)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    git = _LocalPushGit(origin)
    crashed = CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=git,
        forge_provider=_AuditForge(),
        app_client=app,
        crash_hook=_CrashOnce("after_partial_push"),
        clock=lambda: 100.0,
    )
    with pytest.raises(RuntimeError, match="crash at after_partial_push"):
        await crashed.build("batch")

    result = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=git,
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: 100.0,
    ).build("batch")

    assert result.outcome == "conflict"
    assert _git(origin, "rev-parse", result.branch) == result.head_sha
    assert len(git.pushes) == 1


async def test_candidate_network_awaits_run_after_database_commit(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    depth = 0
    original_immediate = db.immediate

    @asynccontextmanager
    async def tracked_immediate():
        nonlocal depth
        async with original_immediate() as conn:
            depth += 1
            try:
                yield conn
            finally:
                depth -= 1

    db.immediate = tracked_immediate

    class GuardedApp(_AppClient):
        async def installation_token(self):
            assert depth == 0
            return await super().installation_token()

        async def exact_head_ref(self, branch):
            assert depth == 0
            return await super().exact_head_ref(branch)

    class GuardedGit(_LocalPushGit):
        async def apush_oid_with_app_auth(self, *args, **kwargs):
            assert depth == 0
            return await super().apush_oid_with_app_auth(*args, **kwargs)

    app = GuardedApp(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    result = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=GuardedGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: 100.0,
    ).build("batch")

    assert result.outcome == "built"


async def test_live_external_claim_blocks_rebuild_and_branch_transfer(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService
    from src.integration.models import BranchKey, Fence
    from src.integration.ownership import BranchBusy, BranchOwnership

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    service = CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: 100.0,
    )
    built = await service.build("batch")
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_candidate_ref_mutations).values(
                id="live-claim",
                batch_id="batch",
                revision=0,
                member_ordinal=None,
                resolution_id=None,
                purpose="candidate_final",
                repository_id="repo",
                branch=built.branch,
                target_branch=built.branch,
                expected_old_sha=built.head_sha,
                desired_sha="f" * 40,
                operation_id="repair-batch-batch",
                operation_episode_id="batch",
                operation_stage=0,
                lease_owner_id="sealer",
                lease_fence_token=1,
                branch_owner_id="repair-batch-batch",
                branch_owner_role="collector",
                branch_fence_token=1,
                nonce="claim-nonce",
                state="reserved",
                expires_at=160.0,
                created_at=100.0,
                updated_at=100.0,
            )
        )
    waiting = await service.rebuild("batch", 0, base)
    assert waiting.outcome == "wait"
    owner = BranchOwnership(db, clock=lambda: 100.0)
    with pytest.raises(BranchBusy, match="live external mutation claim"):
        await owner.transfer(
            Fence(
                target=BranchKey(repository_id="repo", branch=built.branch),
                owner_id="repair-batch-batch",
                token=1,
            ),
            "repair-task",
            "repair",
        )
    expiry = await service.repair.expire("repair-batch-batch", 0, now=131.0)
    assert expiry["outcome"] == "not_due"
    assert expiry["action"] == "wait"


@pytest.mark.parametrize("invalidator", ("stage", "ordinary", "confirmed", "rebuild"))
async def test_expired_ambiguous_claim_blocks_every_invalidator(db, tmp_path, invalidator):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService
    from src.integration.models import BranchKey, Fence
    from src.integration.ownership import BranchBusy, BranchOwnership

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    service = CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: 100.0,
    )
    built = await service.build("batch")
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_candidate_ref_mutations).values(
                id=f"expired-ambiguous-{invalidator}",
                batch_id="batch",
                revision=0,
                member_ordinal=None,
                resolution_id=None,
                purpose="candidate_final",
                repository_id="repo",
                branch=built.branch,
                target_branch=built.branch,
                expected_old_sha="e" * 40,
                desired_sha="f" * 40,
                operation_id="repair-batch-batch",
                operation_episode_id="batch",
                operation_stage=0,
                lease_owner_id="sealer",
                lease_fence_token=1,
                branch_owner_id="repair-batch-batch",
                branch_owner_role="collector",
                branch_fence_token=1,
                nonce="expired-ambiguous",
                state="reserved",
                expires_at=99.0,
                created_at=1.0,
                updated_at=1.0,
            )
        )
    ownership = BranchOwnership(db, clock=lambda: 100.0)
    fence = Fence(
        target=BranchKey(repository_id="repo", branch=built.branch),
        owner_id="repair-batch-batch",
        token=1,
    )
    if invalidator == "stage":
        result = await service.repair.expire("repair-batch-batch", 0, now=131.0)
        assert result["outcome"] == "not_due"
        assert result["action"] == "wait"
    elif invalidator == "ordinary":
        with pytest.raises(BranchBusy, match="external mutation claim"):
            await ownership.transfer(fence, "repair-task", "repair")
    elif invalidator == "confirmed":
        async with db.immediate() as conn:
            await conn.execute(
                update(integration_branch_owners)
                .where(integration_branch_owners.c.repository_id == "repo")
                .values(handoff_state="handoff_pending")
            )
        confirmation = await ownership.get_owner(fence.target)
        async with db.immediate() as conn:
            with pytest.raises(BranchBusy, match="external mutation claim"):
                await ownership.transfer_confirmed_on(
                    conn, fence, "repair-task", "repair", confirmation
                )
    else:
        result = await service.rebuild("batch", 0, base)
        assert result.outcome == "wait"
    async with db._engine.connect() as conn:
        claim = (
            await conn.execute(
                select(integration_candidate_ref_mutations.c.state).where(
                    integration_candidate_ref_mutations.c.id
                    == f"expired-ambiguous-{invalidator}"
                )
            )
        ).scalar_one()
    assert claim == "reserved"


@pytest.mark.parametrize("invalidator", ("stage", "ordinary", "confirmed", "rebuild"))
async def test_invalidator_commit_fences_later_mutation_reservation(db, tmp_path, invalidator):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService, CandidateStaleAuthority
    from src.integration.models import BranchKey, Fence
    from src.integration.ownership import BranchOwnership

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    service = CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: 100.0,
    )
    built = await service.build("batch")
    stale_state = await service._locked_state("batch")
    ownership = BranchOwnership(db, clock=lambda: 100.0)
    fence = Fence(
        target=BranchKey(repository_id="repo", branch=built.branch),
        owner_id="repair-batch-batch",
        token=1,
    )

    if invalidator == "stage":
        expired = await service.repair.expire("repair-batch-batch", 0, now=131.0)
        assert expired["outcome"] == "expired"
    elif invalidator == "ordinary":
        await ownership.transfer(fence, "repair-task", "repair")
    elif invalidator == "confirmed":
        async with db.immediate() as conn:
            await conn.execute(
                update(integration_branch_owners)
                .where(integration_branch_owners.c.repository_id == "repo")
                .values(handoff_state="handoff_pending")
            )
        confirmation = await ownership.get_owner(fence.target)
        async with db.immediate() as conn:
            await ownership.transfer_confirmed_on(
                conn, fence, "repair-task", "repair", confirmation
            )
    else:
        service.forge_provider = _AuditForge()
        rebuilt = await service.rebuild("batch", 0, base)
        assert rebuilt.outcome in {"built", "already_built"}

    with pytest.raises(CandidateStaleAuthority):
        await service._mutate_ref(
            stale_state,
            revision=0,
            purpose="candidate_partial",
            target_branch=built.branch,
            expected_old_sha=built.head_sha,
            desired_sha="f" * 40,
            store=tmp_path / "data" / "integration-objects" / "repo.git",
            member_ordinal=0,
        )
    async with db._engine.connect() as conn:
        late_claims = (
            await conn.execute(
                select(func.count()).select_from(integration_candidate_ref_mutations).where(
                    integration_candidate_ref_mutations.c.desired_sha == "f" * 40
                )
            )
        ).scalar_one()
    assert late_claims == 0


async def test_rebuild_rechecks_claim_inside_supersession_transaction(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    service = CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: 100.0,
    )
    built = await service.build("batch")
    original_pin = service._pin
    inserted = False

    async def pin_then_race(store, ref, head):
        nonlocal inserted
        await original_pin(store, ref, head)
        if not inserted:
            inserted = True
            async with db.immediate() as conn:
                await conn.execute(
                    insert(integration_candidate_ref_mutations).values(
                        id="rebuild-race-claim",
                        batch_id="batch",
                        revision=0,
                        member_ordinal=None,
                        resolution_id=None,
                        purpose="candidate_final",
                        repository_id="repo",
                        branch=built.branch,
                        target_branch=built.branch,
                        expected_old_sha=built.head_sha,
                        desired_sha="f" * 40,
                        operation_id="repair-batch-batch",
                        operation_episode_id="batch",
                        operation_stage=0,
                        lease_owner_id="sealer",
                        lease_fence_token=1,
                        branch_owner_id="repair-batch-batch",
                        branch_owner_role="collector",
                        branch_fence_token=1,
                        nonce="race",
                        state="reserved",
                        expires_at=300.0,
                        created_at=100.0,
                        updated_at=100.0,
                    )
                )

    service._pin = pin_then_race
    result = await service.rebuild("batch", 0, base)

    assert result.outcome == "wait"
    async with db._engine.connect() as conn:
        batch = (
            await conn.execute(select(integration_batches).where(integration_batches.c.id == "batch"))
        ).mappings().one()
        revisions = (
            await conn.execute(
                select(integration_candidate_revisions).order_by(
                    integration_candidate_revisions.c.revision
                )
            )
        ).mappings().all()
    assert batch["current_revision"] == 0
    assert [row["state"] for row in revisions] == ["built"]


async def test_mutation_claim_covers_transport_bound_plus_margin(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: 100.0,
    ).build("batch")
    async with db._engine.connect() as conn:
        rows = (
            await conn.execute(select(integration_candidate_ref_mutations))
        ).mappings().all()
    assert rows
    assert all(float(row["expires_at"]) - float(row["created_at"]) >= 125.0 for row in rows)


async def test_expired_writer_observes_without_starting_push(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    now = {"value": 100.0}

    class ExpiringApp(_AppClient):
        async def exact_head_ref(self, branch):
            value = await super().exact_head_ref(branch)
            if branch.startswith("aq/integration/"):
                now["value"] = 1001.0
            return value

    app = ExpiringApp(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    git = _LocalPushGit(origin)
    result = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=git,
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: now["value"],
    ).build("batch")

    assert result.outcome == "wait"
    assert git.pushes == []
    fresh_git = _LocalPushGit(origin)
    fresh = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=fresh_git,
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: now["value"],
    ).build("batch")
    assert fresh.outcome == "wait"
    assert fresh_git.pushes == []
    async with db._engine.connect() as conn:
        remaining = (
            await conn.execute(
                select(
                    integration_candidate_ref_mutations.c.id,
                    integration_candidate_ref_mutations.c.state,
                ).where(
                    integration_candidate_ref_mutations.c.state == "reserved"
                )
            )
        ).one_or_none()
    assert remaining is not None
    assert remaining.state == "reserved"


@pytest.mark.parametrize("purpose", ("candidate_partial", "candidate_final"))
async def test_public_build_takes_over_expired_prepush_claim_once(
    db, tmp_path, purpose
):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService
    from src.integration.repair import RepairService

    make_origin = _make_conflicting_origin if purpose == "candidate_partial" else _make_origin
    origin, _work, base, members = make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members if purpose == "candidate_partial" else members[:1], base_sha=base)
    now = {"value": 100.0}
    branch = "aq/integration/p-" + "5" * 32 + "/r-" + "6" * 32

    class PrepushCrashApp(_AppClient):
        crashed = False

        async def exact_head_ref(self, requested_branch):
            if requested_branch == branch and not self.crashed:
                self.crashed = True
                raise RuntimeError("crash before candidate mutation push")
            return await super().exact_head_ref(requested_branch)

    crash_app = PrepushCrashApp(origin)
    crash_app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    crashed_git = _LocalPushGit(origin)
    crashed = CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=crashed_git,
        forge_provider=_AuditForge(),
        app_client=crash_app,
        repair_service=RepairService(db),
        clock=lambda: now["value"],
    )
    with pytest.raises(RuntimeError, match="crash before candidate mutation push"):
        await crashed.build("batch")
    assert crashed_git.pushes == []
    async with db._engine.connect() as conn:
        claim = (
            await conn.execute(
                select(integration_candidate_ref_mutations).where(
                    integration_candidate_ref_mutations.c.state == "reserved"
                )
            )
        ).mappings().one()
    assert claim["purpose"] == purpose
    assert float(claim["expires_at"]) == 235.0

    now["value"] = 236.0

    class ObservationBarrierApp(_AppClient):
        readers = 0
        both_reading = asyncio.Event()

        async def exact_head_ref(self, requested_branch):
            value = await super().exact_head_ref(requested_branch)
            if requested_branch == branch and self.readers < 2:
                self.readers += 1
                if self.readers == 2:
                    self.both_reading.set()
                await self.both_reading.wait()
            return value

    app = ObservationBarrierApp(origin)
    app.repository = crash_app.repository
    git = _LocalPushGit(origin)
    forge = _AuditForge()
    services = [
        CandidateService(
            db,
            data_dir=tmp_path / "data",
            git_manager=git,
            forge_provider=forge,
            app_client=app,
            repair_service=RepairService(db),
            clock=lambda: now["value"],
        )
        for _ in range(2)
    ]
    results = await asyncio.wait_for(
        asyncio.gather(*(service.build("batch") for service in services)),
        timeout=10.0,
    )

    assert len(git.pushes) == 1
    if purpose == "candidate_partial":
        assert {result.outcome for result in results} <= {"conflict", "wait"}
        assert "conflict" in {result.outcome for result in results}
        converged = await services[0].build("batch")
        assert converged.outcome == "conflict"
        assert forge.calls == []
    else:
        assert {result.outcome for result in results} <= {"already_built", "wait"}
        assert "already_built" in {result.outcome for result in results}
        converged = await services[0].build("batch")
        assert converged.outcome == "already_built"
        assert len(forge.calls) == 1
    async with db._engine.connect() as conn:
        canonical = (
            await conn.execute(
                select(integration_candidate_ref_mutations).where(
                    integration_candidate_ref_mutations.c.id == claim["id"]
                )
            )
        ).mappings().one()
    assert canonical["state"] == "applied"
    assert canonical["nonce"] != claim["nonce"]


@pytest.mark.parametrize(
    "blocker", ("live", "unexpected", "insufficient_lease", "failed_read", "out_of_order")
)
async def test_public_build_observer_fails_closed_for_nonrecoverable_claims(
    db, tmp_path, blocker
):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    now = {"value": 100.0}
    branch = "aq/integration/p-" + "5" * 32 + "/r-" + "6" * 32

    class PrepushCrashApp(_AppClient):
        crashed = False

        async def exact_head_ref(self, requested_branch):
            if requested_branch == branch and not self.crashed:
                self.crashed = True
                raise RuntimeError("crash before final push")
            return await super().exact_head_ref(requested_branch)

    crash_app = PrepushCrashApp(origin)
    crash_app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    with pytest.raises(RuntimeError, match="crash before final push"):
        await CandidateService(
            db,
            data_dir=tmp_path / "data",
            git_manager=_LocalPushGit(origin),
            forge_provider=_AuditForge(),
            app_client=crash_app,
            clock=lambda: now["value"],
        ).build("batch")
    async with db._engine.connect() as conn:
        original = (
            await conn.execute(select(integration_candidate_ref_mutations))
        ).mappings().one()

    if blocker != "live":
        now["value"] = 236.0
    if blocker == "unexpected":
        _git(work, "push", "origin", f"{members[0][1]}:refs/heads/{branch}")
    elif blocker == "insufficient_lease":
        async with db.immediate() as conn:
            await conn.execute(
                update(project_integration_leases)
                .where(project_integration_leases.c.project_id == "p")
                .values(expires_at=300.0)
            )
    elif blocker == "out_of_order":
        async with db.immediate() as conn:
            await conn.execute(
                update(integration_candidate_revisions)
                .where(
                    integration_candidate_revisions.c.batch_id == "batch",
                    integration_candidate_revisions.c.revision == 0,
                )
                .values(state="constructing")
            )

    class RetryApp(_AppClient):
        async def exact_head_ref(self, requested_branch):
            if blocker == "failed_read" and requested_branch == branch:
                raise RuntimeError("authenticated ref observation failed")
            return await super().exact_head_ref(requested_branch)

    app = RetryApp(origin)
    app.repository = crash_app.repository
    git = _LocalPushGit(origin)
    result = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=git,
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: now["value"],
    ).build("batch")

    assert result.outcome == "wait"
    assert git.pushes == []
    async with db._engine.connect() as conn:
        canonical = (
            await conn.execute(
                select(integration_candidate_ref_mutations).where(
                    integration_candidate_ref_mutations.c.id == original["id"]
                )
            )
        ).mappings().one()
    assert canonical["state"] == "reserved"
    assert canonical["nonce"] == original["nonce"]


async def test_public_build_reconciles_desired_remote_without_repush(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    branch = "aq/integration/p-" + "5" * 32 + "/r-" + "6" * 32

    class PrepushCrashApp(_AppClient):
        crashed = False

        async def exact_head_ref(self, requested_branch):
            if requested_branch == branch and not self.crashed:
                self.crashed = True
                raise RuntimeError("lost response after remote accepted final push")
            return await super().exact_head_ref(requested_branch)

    crash_app = PrepushCrashApp(origin)
    crash_app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    with pytest.raises(RuntimeError, match="lost response after remote accepted final push"):
        await CandidateService(
            db,
            data_dir=tmp_path / "data",
            git_manager=_LocalPushGit(origin),
            forge_provider=_AuditForge(),
            app_client=crash_app,
            clock=lambda: 100.0,
        ).build("batch")
    async with db._engine.connect() as conn:
        claim = (
            await conn.execute(select(integration_candidate_ref_mutations))
        ).mappings().one()
    store = next((tmp_path / "data" / "integration-repositories").iterdir())
    _git(store, "push", str(origin), f"{claim['desired_sha']}:refs/heads/{branch}")

    app = _AppClient(origin)
    app.repository = crash_app.repository
    git = _LocalPushGit(origin)
    result = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=git,
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: 100.0,
    ).build("batch")

    assert result.outcome == "already_built"
    assert git.pushes == []
    async with db._engine.connect() as conn:
        canonical = (
            await conn.execute(
                select(integration_candidate_ref_mutations).where(
                    integration_candidate_ref_mutations.c.id == claim["id"]
                )
            )
        ).mappings().one()
    assert canonical["state"] == "applied"
    assert canonical["remote_sha"] == claim["desired_sha"]


async def test_remote_success_after_lease_expiry_is_observation_reconciled(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    now = {"value": 100.0}

    class ExpiringPushGit(_LocalPushGit):
        async def apush_oid_with_app_auth(self, checkout_path, **kwargs):
            result = await super().apush_oid_with_app_auth(checkout_path, **kwargs)
            now["value"] = 1001.0
            return result

    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    git = ExpiringPushGit(origin)
    result = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=git,
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: now["value"],
    ).build("batch")
    assert result.outcome == "wait"
    assert len(git.pushes) == 1
    async with db._engine.connect() as conn:
        states = (
            await conn.execute(select(integration_candidate_ref_mutations.c.state))
        ).scalars().all()
    assert states == ["applied"]
    fresh_git = _LocalPushGit(origin)
    fresh = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=fresh_git,
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: now["value"],
    ).build("batch")
    assert fresh.outcome == "wait"
    assert fresh_git.pushes == []


@pytest.mark.parametrize("expire_authority", (False, True))
async def test_lost_force_with_lease_response_reconciles_without_second_push(
    db, tmp_path, expire_authority
):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    now = {"value": 100.0}

    class LostResponseGit(_LocalPushGit):
        async def apush_oid_with_app_auth(self, checkout_path, **kwargs):
            await super().apush_oid_with_app_auth(checkout_path, **kwargs)
            if expire_authority:
                now["value"] = 1001.0
            raise RuntimeError("transport lost the successful push response")

    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    git = LostResponseGit(origin)
    forge_backing = {"result": None, "calls": []}
    result = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=git,
        forge_provider=_AuditForge(forge_backing),
        app_client=app,
        clock=lambda: now["value"],
    ).build("batch")

    assert result.outcome == ("wait" if expire_authority else "built")
    assert len(git.pushes) == 1
    async with db._engine.connect() as conn:
        states = (
            await conn.execute(select(integration_candidate_ref_mutations.c.state))
        ).scalars().all()
    assert states and set(states) == {"applied"}

    replay_git = _LocalPushGit(origin)
    replay = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=replay_git,
        forge_provider=_AuditForge(forge_backing),
        app_client=app,
        clock=lambda: now["value"],
    ).build("batch")
    assert replay.outcome == ("wait" if expire_authority else "already_built")
    assert replay_git.pushes == []


@pytest.mark.parametrize(
    "claim_case", ("expired_expected", "live_expected", "expired_unexpected")
)
async def test_mutation_claim_executor_takeover_is_exclusive(db, tmp_path, claim_case):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    initial_app = _AppClient(origin)
    initial_app.repository = GitHubRepositoryBinding(
        repository_id=9, full_name="example/repo"
    )
    initial = CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=initial_app,
        clock=lambda: 100.0,
    )
    built = await initial.build("batch")
    state = await initial._locked_state("batch")
    store = await initial._ensure_store(await initial._repository("repo"))
    expected_old = built.head_sha if claim_case != "expired_unexpected" else "e" * 40
    expires_at = 300.0 if claim_case == "live_expected" else 99.0
    mutation_id = initial._mutation_id(
        purpose="candidate_partial",
        batch_id="batch",
        revision=0,
        ordinal=0,
        resolution_id=None,
    )
    identity = initial._mutation_identity(
        state,
        revision=0,
        purpose="candidate_partial",
        target_branch=built.branch,
        expected_old_sha=expected_old,
        desired_sha=base,
        member_ordinal=0,
        resolution_id=None,
        expected_role="collector",
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_candidate_ref_mutations).values(
                id=mutation_id,
                **identity,
                nonce="existing-executor",
                state="reserved",
                expires_at=expires_at,
                created_at=1.0,
                updated_at=1.0,
            )
        )

    class ReadBarrierApp(_AppClient):
        def __init__(self, origin, branch):
            super().__init__(origin)
            self.branch = branch.removeprefix("refs/heads/")
            self.readers = 0
            self.both_reading = asyncio.Event()

        async def exact_head_ref(self, branch):
            value = await super().exact_head_ref(branch)
            if branch == self.branch:
                self.readers += 1
                if self.readers == 2:
                    self.both_reading.set()
                await self.both_reading.wait()
            return value

    app = ReadBarrierApp(origin, built.branch)
    app.repository = initial_app.repository
    git = _LocalPushGit(origin)
    services = [
        CandidateService(
            db,
            data_dir=tmp_path / "data",
            git_manager=git,
            forge_provider=_AuditForge(),
            app_client=app,
            clock=lambda: 100.0,
        )
        for _ in range(2)
    ]
    states = [await candidate._locked_state("batch") for candidate in services]
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                candidate._mutate_ref(
                    candidate_state,
                    revision=0,
                    purpose="candidate_partial",
                    target_branch=built.branch,
                    expected_old_sha=expected_old,
                    desired_sha=base,
                    store=store,
                    member_ordinal=0,
                )
                for candidate, candidate_state in zip(services, states, strict=True)
            )
        ),
        timeout=10.0,
    )

    async with db._engine.connect() as conn:
        claim = (
            await conn.execute(
                select(integration_candidate_ref_mutations).where(
                    integration_candidate_ref_mutations.c.id == mutation_id
                )
            )
        ).mappings().one()
    if claim_case == "expired_expected":
        assert results.count(True) == 1
        assert len(git.pushes) == 1
        assert claim["state"] == "applied"
        assert claim["nonce"] != "existing-executor"
        assert _git(origin, "rev-parse", built.branch) == base
    else:
        assert results == [False, False]
        assert git.pushes == []
        assert claim["state"] == "reserved"
        assert claim["nonce"] == "existing-executor"
        assert _git(origin, "rev-parse", built.branch) == built.head_sha


async def test_mutation_remains_authorized_beyond_old_sixty_second_window(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    now = {"value": 100.0}

    class SlowBoundedPushGit(_LocalPushGit):
        async def apush_oid_with_app_auth(self, checkout_path, **kwargs):
            result = await super().apush_oid_with_app_auth(checkout_path, **kwargs)
            now["value"] = 165.0
            return result

    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    git = SlowBoundedPushGit(origin)
    result = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=git,
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: now["value"],
    ).build("batch")
    assert result.outcome == "built"
    assert len(git.pushes) == 1
    async with db._engine.connect() as conn:
        state = (
            await conn.execute(select(integration_candidate_ref_mutations.c.state))
        ).scalar_one()
    assert state == "applied"


async def test_changed_reviewed_tree_fails_closed_as_source_moved(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    member = (members[0][0], members[0][1], "f" * 40)
    await _seed_batch(db, members=(member,), base_sha=base)

    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    result = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: 100.0,
    ).build("batch")

    assert result.outcome == "source_moved"
    async with db._engine.connect() as conn:
        revision = (await conn.execute(select(integration_candidate_revisions))).mappings().one()
    assert revision["state"] == "constructing"
    assert revision["next_member_ordinal"] == 0


async def test_overdue_rebuild_immediately_dispatches_preserved_debug_budget(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService
    from src.integration.repair import RepairService

    origin, work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    repair = RepairService(db, route_validator=lambda _intelligence_class, _profile_id: True)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    git = _LocalPushGit(origin)
    forge = _AuditForge()
    first = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        repair_service=repair,
        clock=lambda: 100.0,
        git_manager=git,
        forge_provider=forge,
        app_client=app,
    ).build("batch")
    _git(work, "switch", "-C", "main", base)
    (work / "upstream.txt").write_text("new base\n")
    _git(work, "add", "upstream.txt")
    _git(work, "commit", "-m", "advance main")
    new_base = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "--force", "origin", "main")

    result = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        repair_service=repair,
        clock=lambda: 200.0,
        git_manager=git,
        forge_provider=forge,
        app_client=app,
    ).rebuild("batch", first.revision, new_base)

    assert result.outcome == "wait"
    async with db._engine.connect() as conn:
        operation = (
            (
                await conn.execute(
                    select(integration_repair_operations).where(
                        integration_repair_operations.c.id == "repair-batch-batch"
                    )
                )
            )
            .mappings()
            .one()
        )
        stages = (
            (
                await conn.execute(
                    select(integration_repair_stages).order_by(integration_repair_stages.c.ordinal)
                )
            )
            .mappings()
            .all()
        )
    assert operation["active_stage"] == 1
    assert [stage["state"] for stage in stages] == ["expired", "active"]
    assert stages[1]["repair_task_id"] == "repair-repair-batch-batch-1"


async def test_reserved_member_path_never_marks_partial_candidate_built(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, work, base, _members = _make_origin(tmp_path)
    _git(work, "switch", "-C", "root-reserved", base)
    (work / ".codex").mkdir()
    (work / ".codex" / "settings.json").write_text("{}\n")
    _git(work, "add", ".codex/settings.json")
    _git(work, "commit", "-m", "reserved path")
    head = _git(work, "rev-parse", "HEAD")
    tree = _git(work, "rev-parse", f"{head}^{{tree}}")
    _git(work, "push", "origin", "HEAD:refs/heads/root-reserved")
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=((base, head, tree),), base_sha=base)

    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    result = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: 100.0,
    ).build("batch")

    assert result.outcome == "conflict"
    async with db._engine.connect() as conn:
        revision = (await conn.execute(select(integration_candidate_revisions))).mappings().one()
        member = (await conn.execute(select(integration_candidate_member_results))).mappings().one()
    assert revision["state"] == "constructing"
    assert revision["next_member_ordinal"] == 0
    assert member["result"] == "conflict"
    assert member["conflict_evidence"]["detail"] == "reserved_path"


async def test_expired_project_lease_cannot_advance_candidate(db, tmp_path):
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)

    result = await CandidateService(db, data_dir=tmp_path / "data", clock=lambda: 1001.0).build(
        "batch"
    )

    assert result.outcome == "wait"
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_candidate_revisions))).all() == []


async def test_repair_owned_branch_cannot_advance_candidate(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(integration_branch_owners.c.id == "candidate-owner")
            .values(owner_id="repair-task", owner_role="repair-primary", handoff_state="reserved")
        )
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    result = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: 100.0,
    ).build("batch")

    assert result.outcome == "wait"
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_candidate_revisions))).all() == []


async def test_direct_caller_repair_lineage_is_not_authoritative(db, tmp_path):
    from src.integration.candidates import (
        CandidateAuthorizationError,
        CandidateRepairLineage,
        CandidateService,
    )

    service = CandidateService(db, data_dir=tmp_path / "data")
    caller_claim = CandidateRepairLineage(
        batch_id="batch",
        revision=0,
        member_ordinal=0,
        operation_id="operation",
        operation_stage=0,
        partial_head_sha="1" * 40,
        source_base_sha="2" * 40,
        source_head_sha="3" * 40,
        resolved_head_sha="4" * 40,
        repair_commit_shas=("4" * 40,),
    )

    with pytest.raises(CandidateAuthorizationError):
        await service.accept_repair(caller_claim)


@pytest.mark.parametrize(
    ("repair_change", "expected", "stage", "handoff_crash"),
    (
        ("exact", "accepted", 0, None),
        ("exact", "accepted", 0, "after_handoff_reservation"),
        ("exact", "accepted", 0, "after_handoff_transfer"),
        ("exact", "accepted", 0, "after_handoff_push"),
        ("exact", "accepted", 0, "before_repair_acceptance"),
        ("exact", "accepted", 0, "overlap"),
        ("exact", "accepted", 1, None),
        ("reserved", "stale", 0, None),
        ("extra", "stale", 0, None),
    ),
)
async def test_instance_bound_repair_reservation_push_and_accept_once(
    db, tmp_path, repair_change, expected, stage, handoff_crash
):
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import (
        CandidateAuthorizationError,
        CandidateResolutionInput,
        CandidateService,
    )
    from src.integration.models import BranchKey, Fence
    from src.integration.ownership import BranchBusy, BranchOwnership
    from src.integration.repair import RepairService
    from src.profiles.capabilities import CapabilityPolicy

    origin, work, base, members = _make_conflicting_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members, base_sha=base)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")

    async def release_like_orchestrator(row: dict) -> bool:
        async with db.immediate() as conn:
            changed = await conn.execute(
                update(integration_branch_owners)
                .where(
                    integration_branch_owners.c.id == row["id"],
                    integration_branch_owners.c.fence_token == row["fence_token"],
                    integration_branch_owners.c.handoff_state == "handoff_pending",
                )
                .values(
                    handoff_state="released",
                    session_id=None,
                    workspace_id=None,
                    confirmed_workspace_id=row["workspace_id"],
                )
            )
        return changed.rowcount == 1

    ownership = BranchOwnership(db, confirm_handoff=release_like_orchestrator)
    repair = RepairService(db, route_validator=lambda *_: True)
    service = CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        repair_service=repair,
        branch_ownership=ownership,
        clock=lambda: 100.0,
    )
    conflict = await service.build("batch")
    if stage == 1:
        expired = await repair.expire("repair-batch-batch", 0, now=131.0)
        assert expired["action"] == "dispatch_debug"
        async with db.immediate() as conn:
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == "repair-batch-batch",
                    integration_repair_stages.c.ordinal == 0,
                )
                .values(repair_task_id=None, writer_kind=None)
            )
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.operation_id == "repair-batch-batch",
                    integration_repair_stages.c.ordinal == 1,
                )
                .values(
                    repair_task_id="repair-repair-batch-batch-0",
                    writer_kind="repair_delegate",
                    state="active",
                )
            )
    repair_task = "repair-repair-batch-batch-0"
    workspace_id = "candidate-repair-workspace"
    session_id = "candidate-repair-session"
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == repair_task).values(status="IN_PROGRESS")
        )
        await conn.execute(
            insert(workspaces).values(
                id=workspace_id,
                project_id="p",
                workspace_path=str(work),
                source_type="link",
                locked_by_task_id=repair_task,
                enabled=True,
                created_at=100.0,
            )
        )
    await db.create_session(
        SessionRecord(
            id=session_id,
            task_id=repair_task,
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="candidate-repair",
            lifecycle="task",
            state="running",
            work_dir=str(work),
            epoch="test",
            instance_token="instance-1",
            started_at=100.0,
        )
    )
    owner = await ownership.get_owner(BranchKey(repository_id="repo", branch=conflict.branch))
    repair_fence = Fence(
        target={"repository_id": "repo", "branch": conflict.branch},
        owner_id=repair_task,
        token=owner["fence_token"],
    )
    await ownership.attach(repair_fence, session_id, workspace_id, expected_role="repair")
    _git(work, "fetch", str(origin), conflict.branch)
    _git(work, "switch", "--detach", "FETCH_HEAD")
    (work / "shared.txt").write_text("first and second\n")
    if repair_change == "reserved":
        (work / ".codex").mkdir()
        (work / ".codex" / "settings.json").write_text("{}\n")
    elif repair_change == "extra":
        (work / "extra.txt").write_text("not in the sealed member delta\n")
    _git(work, "add", "shared.txt")
    if repair_change != "exact":
        _git(work, "add", "-A")
    _git(work, "commit", "-m", "resolve exact candidate conflict")
    resolved = _git(work, "rev-parse", "HEAD")
    tree = _git(work, "rev-parse", "HEAD^{tree}")
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=[]),
        session_id=session_id,
        session_instance_token="instance-1",
        task_id=repair_task,
        project_id="p",
        profile_id="repairer",
    )
    request = CandidateResolutionInput(
        batch_id="batch",
        revision=0,
        member_ordinal=1,
        operation_id="repair-batch-batch",
        resolved_head_sha=resolved,
        resolved_tree_sha=tree,
        repair_commit_shas=(resolved,),
        fence=repair_fence,
    )
    stale_principal = replace(principal, session_instance_token="replaced")
    with principal_context(stale_principal):
        with pytest.raises(CandidateAuthorizationError):
            await service.reserve_repair(request)
    with principal_context(principal):
        reservation_id = await service.reserve_repair(request)
        if stage == 0 and repair_change == "exact":
            replacement_workspace = "candidate-repair-workspace-replacement"
            replacement_path = tmp_path / "replacement-workspace"
            replacement_path.mkdir()
            async with db.immediate() as conn:
                await conn.execute(
                    insert(workspaces).values(
                        id=replacement_workspace,
                        project_id="p",
                        workspace_path=str(replacement_path),
                        source_type="link",
                        locked_by_task_id=repair_task,
                        enabled=True,
                        created_at=100.0,
                    )
                )
                await conn.execute(
                    update(integration_branch_owners)
                    .where(integration_branch_owners.c.repository_id == "repo")
                    .values(workspace_id=replacement_workspace)
                )
                await conn.execute(
                    update(sessions)
                    .where(sessions.c.id == session_id)
                    .values(work_dir=str(replacement_path))
                )
            with pytest.raises(CandidateAuthorizationError):
                await service.push_repair(reservation_id, repair_fence)
            async with db.immediate() as conn:
                await conn.execute(
                    update(integration_branch_owners)
                    .where(integration_branch_owners.c.repository_id == "repo")
                    .values(workspace_id=workspace_id)
                )
                await conn.execute(
                    update(sessions)
                    .where(sessions.c.id == session_id)
                    .values(work_dir=str(work))
                )
            rebound_path = tmp_path / "same-id-rebound-workspace"
            rebound_path.mkdir()
            async with db.immediate() as conn:
                await conn.execute(
                    update(workspaces)
                    .where(workspaces.c.id == workspace_id)
                    .values(workspace_path=str(rebound_path))
                )
                await conn.execute(
                    update(sessions)
                    .where(sessions.c.id == session_id)
                    .values(work_dir=str(rebound_path))
                )
            with pytest.raises(CandidateAuthorizationError):
                await service.push_repair(reservation_id, repair_fence)
            async with db.immediate() as conn:
                await conn.execute(
                    update(workspaces)
                    .where(workspaces.c.id == workspace_id)
                    .values(workspace_path=str(work))
                )
                await conn.execute(
                    update(sessions)
                    .where(sessions.c.id == session_id)
                    .values(work_dir=str(work))
                )
        wrong_target = Fence(
            target={"repository_id": "repo", "branch": conflict.branch + "-other"},
            owner_id=repair_task,
            token=repair_fence.token,
        )
        with pytest.raises(CandidateAuthorizationError):
            await service.push_repair(reservation_id, wrong_target)
        if stage == 1 and repair_change == "exact":
            service.crash_hook = _CrashOnce("after_repair_push")
            with pytest.raises(RuntimeError, match="crash at after_repair_push"):
                await service.push_repair(reservation_id, repair_fence)
            service = CandidateService(
                db,
                data_dir=tmp_path / "data",
                git_manager=_LocalPushGit(origin),
                forge_provider=_AuditForge(),
                app_client=app,
                repair_service=repair,
                branch_ownership=ownership,
                clock=lambda: 100.0,
            )
        assert await service.push_repair(reservation_id, repair_fence) == reservation_id
    await db.update_session(session_id, state="stopped")
    if handoff_crash == "overlap":
        service.crash_hook = _CrashOnce("after_handoff_reservation")
        with pytest.raises(RuntimeError, match="crash at after_handoff_reservation"):
            await service.accept_repair(reservation_id)
        async with db.immediate() as conn:
            await conn.execute(
                delete(integration_candidate_ref_mutations).where(
                    integration_candidate_ref_mutations.c.batch_id == "batch",
                    integration_candidate_ref_mutations.c.revision == 0,
                    integration_candidate_ref_mutations.c.purpose == "repair_handoff",
                )
            )

        class HandoffReadBarrierApp(_AppClient):
            def __init__(self, origin, branch):
                super().__init__(origin)
                self.branch = branch.removeprefix("refs/heads/")
                self.readers = 0
                self.both_reading = asyncio.Event()
                self.active = False

            async def exact_head_ref(self, branch):
                value = await super().exact_head_ref(branch)
                if self.active and branch == self.branch:
                    self.readers += 1
                    if self.readers == 2:
                        self.both_reading.set()
                    await self.both_reading.wait()
                return value

        overlap_app = HandoffReadBarrierApp(origin, conflict.branch)
        overlap_app.repository = app.repository
        overlap_git = _LocalPushGit(origin)
        both_reserved = asyncio.Event()
        reservation_count = 0

        async def overlap_after_reservation(point):
            nonlocal reservation_count
            if point != "after_handoff_reservation":
                return
            reservation_count += 1
            if reservation_count == 2:
                overlap_app.active = True
                both_reserved.set()
            await both_reserved.wait()

        services = [
            CandidateService(
                db,
                data_dir=tmp_path / "data",
                git_manager=overlap_git,
                forge_provider=_AuditForge(),
                app_client=overlap_app,
                repair_service=repair,
                branch_ownership=ownership,
                clock=lambda: 100.0,
                crash_hook=overlap_after_reservation,
            )
            for _ in range(2)
        ]
        results = await asyncio.wait_for(
            asyncio.gather(
                *(candidate.accept_repair(reservation_id) for candidate in services),
                return_exceptions=True,
            ),
            timeout=10.0,
        )
        assert not [result for result in results if isinstance(result, BaseException)]
        assert len(overlap_git.pushes) == 1
        assert sum(result.outcome == "accepted" for result in results) == 1
        assert all(result.outcome in {"accepted", "wait", "already_accepted"} for result in results)
        service = services[0]
        accepted = next(result for result in results if result.outcome == "accepted")
        replay = await service.accept_repair(reservation_id)
    elif handoff_crash:
        service.crash_hook = _CrashOnce(handoff_crash)
        with pytest.raises(RuntimeError, match=f"crash at {handoff_crash}"):
            await service.accept_repair(reservation_id)
        if handoff_crash == "after_handoff_reservation":
            blocked_expiry = await repair.expire("repair-batch-batch", stage, now=131.0)
            assert blocked_expiry["outcome"] == "not_due"
            collector_owner = await ownership.get_owner(repair_fence.target)
            collector_fence = Fence(
                target=repair_fence.target,
                owner_id=collector_owner["owner_id"],
                token=collector_owner["fence_token"],
            )
            with pytest.raises(BranchBusy, match="external mutation claim"):
                await ownership.transfer(collector_fence, "other-repair", "repair")
            blocked_rebuild = await service.rebuild("batch", 0, base)
            assert blocked_rebuild.outcome == "wait"
            async with db._engine.connect() as conn:
                handoff_claim = (
                    await conn.execute(
                        select(integration_candidate_ref_mutations.c.state).where(
                            integration_candidate_ref_mutations.c.batch_id == "batch",
                            integration_candidate_ref_mutations.c.revision == 0,
                            integration_candidate_ref_mutations.c.purpose == "repair_handoff",
                        )
                    )
                ).scalar_one()
            assert handoff_claim == "reserved"
        if handoff_crash in {"after_handoff_reservation", "after_handoff_transfer"}:
            async with db.immediate() as conn:
                await conn.execute(
                    update(integration_candidate_ref_mutations)
                    .where(
                        integration_candidate_ref_mutations.c.batch_id == "batch",
                        integration_candidate_ref_mutations.c.revision == 0,
                        integration_candidate_ref_mutations.c.purpose == "repair_handoff",
                        integration_candidate_ref_mutations.c.state == "reserved",
                    )
                    .values(expires_at=99.0)
                )
        service = CandidateService(
            db,
            data_dir=tmp_path / "data",
            git_manager=_LocalPushGit(origin),
            forge_provider=_AuditForge(),
            app_client=app,
            repair_service=repair,
            branch_ownership=ownership,
            clock=lambda: 100.0,
        )
        accepted = await service.accept_repair(reservation_id)
        replay = await service.accept_repair(reservation_id)
    else:
        accepted = await service.accept_repair(reservation_id)
        replay = await service.accept_repair(reservation_id)
    assert accepted.outcome == expected
    assert replay.outcome == ("already_accepted" if expected == "accepted" else "stale")
    if repair_change == "exact" and stage == 0:
        resumed = await service.build("batch")
        assert resumed.outcome in {"built", "already_built"}
        assert resumed.pr_url == "https://github.com/example/repo/pull/9"
        async with db._engine.connect() as conn:
            publication = (
                await conn.execute(
                    select(integration_candidate_publications).where(
                        integration_candidate_publications.c.batch_id == "batch",
                        integration_candidate_publications.c.revision == 0,
                    )
                )
            ).mappings().one()
        assert publication["expected_old_sha"] == resolved
        source_before = _git(origin, "rev-parse", "refs/heads/root-1")
        _git(work, "switch", "-C", "main", base)
        (work / "upstream.txt").write_text("new base\n")
        _git(work, "add", "upstream.txt")
        _git(work, "commit", "-m", "advance main after accepted repair")
        new_base = _git(work, "rev-parse", "HEAD")
        _git(work, "push", "--force", "origin", "main")
        service.forge_provider = _AuditForge()
        rebuilt = await service.rebuild("batch", 0, new_base)
        assert rebuilt.outcome in {"built", "already_built"}
        assert _git(origin, "show", f"{rebuilt.head_sha}:shared.txt") == "first and second"
        assert _git(origin, "rev-parse", "refs/heads/root-1") == source_before


async def test_nonempty_build_requires_authenticated_repository_dependencies(db, tmp_path):
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)

    result = await CandidateService(db, data_dir=tmp_path / "data", clock=lambda: 100.0).build(
        "batch"
    )

    assert result.outcome == "configuration_blocked"
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_candidate_revisions))).all() == []


async def test_persisted_pr_never_hides_diverged_candidate_ref(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    git = _LocalPushGit(origin)
    forge = _AuditForge()
    service = CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=git,
        forge_provider=forge,
        app_client=app,
        clock=lambda: 100.0,
    )
    built = await service.build("batch")
    assert built.branch
    _git(origin, "update-ref", built.branch, base)

    replay = await service.build("batch")

    assert replay.outcome == "wait"


async def test_published_pr_identity_is_immutable_and_replay_is_canonical(db, tmp_path):
    from sqlalchemy.exc import IntegrityError

    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    forge = _AuditForge()
    service = CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=forge,
        app_client=app,
        clock=lambda: 100.0,
    )
    built = await service.build("batch")
    replay = await service.build("batch")
    assert replay.pr_url == built.pr_url
    with pytest.raises(IntegrityError):
        async with db.immediate() as conn:
            await conn.execute(
                update(integration_candidate_publications)
                .where(integration_candidate_publications.c.batch_id == "batch")
                .values(pr_number=10, pr_url="https://github.com/example/repo/pull/10")
            )
    assert replay.head_sha == built.head_sha


def _make_divergent_source_bases(tmp_path: Path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.name", "Candidate Test")
    _git(work, "config", "user.email", "candidate@example.test")
    (work / "common.txt").write_text("common\n")
    _git(work, "add", "common.txt")
    _git(work, "commit", "-m", "common ancestor")
    common = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")
    _git(work, "switch", "-C", "root-0", common)
    (work / "member-0.txt").write_text("member zero\n")
    _git(work, "add", "member-0.txt")
    _git(work, "commit", "-m", "member zero")
    first = _git(work, "rev-parse", "HEAD")
    first_tree = _git(work, "rev-parse", f"{first}^{{tree}}")
    _git(work, "push", "origin", "HEAD:refs/heads/root-0")
    _git(work, "switch", "-C", "root-1-base", common)
    (work / "unreviewed-history.txt").write_text("must not be integrated\n")
    _git(work, "add", "unreviewed-history.txt")
    _git(work, "commit", "-m", "earlier unrelated history")
    second_base = _git(work, "rev-parse", "HEAD")
    (work / "member-1.txt").write_text("reviewed delta\n")
    _git(work, "add", "member-1.txt")
    _git(work, "commit", "-m", "member one")
    second = _git(work, "rev-parse", "HEAD")
    second_tree = _git(work, "rev-parse", f"{second}^{{tree}}")
    _git(work, "push", "origin", "HEAD:refs/heads/root-1")
    return (
        origin,
        common,
        (
            (common, first, first_tree),
            (second_base, second, second_tree),
        ),
    )


async def test_construction_applies_only_each_sealed_source_delta(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, base, members = _make_divergent_source_bases(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members, base_sha=base)

    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    result = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: 100.0,
    ).build("batch")

    assert result.outcome == "built"
    assert result.head_sha
    store = next((tmp_path / "data" / "integration-repositories").iterdir())
    assert _git(store, "ls-tree", "--name-only", result.head_sha).splitlines() == [
        "common.txt",
        "member-0.txt",
        "member-1.txt",
    ]


async def test_exact_sealed_delta_preserves_binary_delete_and_rename(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin = tmp_path / "delta-origin.git"
    work = tmp_path / "delta-work"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.name", "Delta Test")
    _git(work, "config", "user.email", "delta@example.test")
    (work / "renamed.txt").write_text("rename me\n")
    (work / "deleted.txt").write_text("delete me\n")
    (work / "binary.bin").write_bytes(b"\x00\x01old")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "delta base")
    base = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")
    _git(work, "mv", "renamed.txt", "new-name.txt")
    (work / "deleted.txt").unlink()
    (work / "binary.bin").write_bytes(b"\x00\x02new")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "exact reviewed delta")
    head = _git(work, "rev-parse", "HEAD")
    tree = _git(work, "rev-parse", "HEAD^{tree}")
    _git(work, "push", "origin", "HEAD:refs/heads/root-delta")
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=((base, head, tree),), base_sha=base)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    result = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: 100.0,
    ).build("batch")

    assert result.outcome == "built"
    store = next((tmp_path / "data" / "integration-repositories").iterdir())
    assert _git(store, "ls-tree", "--name-only", result.head_sha).splitlines() == [
        "binary.bin",
        "new-name.txt",
    ]
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", f"{result.head_sha}:deleted.txt"], cwd=store
        ).returncode
        != 0
    )


async def test_rebuild_rejects_non_authoritative_base_without_superseding(db, tmp_path):
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import CandidateService

    origin, work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    service = CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        clock=lambda: 100.0,
    )
    assert (await service.build("batch")).outcome == "built"
    foreign = "f" * 40
    moved = await service.rebuild("batch", 0, foreign)

    assert moved.outcome == "base_moved"
    async with db._engine.connect() as conn:
        batch = (await conn.execute(select(integration_batches))).mappings().one()
        revision = (await conn.execute(select(integration_candidate_revisions))).mappings().one()
    assert batch["current_revision"] == 0
    assert revision["state"] == "built"
