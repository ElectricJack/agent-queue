"""Ordered, restartable root integration candidate construction."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_member_results,
    integration_candidate_revisions,
    integration_repair_operations,
    integration_repair_stages,
    integration_review_evidence,
    playbook_artifacts,
    project_integration_leases,
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
    def __init__(self):
        self.calls = []
        self._result = None

    async def lookup_audit_pr(self, *, idempotency_key):
        if self._result is not None and self._result.idempotency_key == idempotency_key:
            return self._result
        return None

    async def create_audit_pr(self, **kwargs):
        from src.integration.candidates import AuditPullRequest

        if self._result is None:
            self.calls.append(kwargs)
            self._result = AuditPullRequest(
                url="https://github.com/example/repo/pull/9",
                number=9,
                head_sha=kwargs["head_sha"],
                head_branch=kwargs["branch"],
                base_branch=kwargs["base_branch"],
                repository_numeric_id=kwargs["repository_numeric_id"],
                repository_full_name=kwargs["repository_full_name"],
                idempotency_key=kwargs["idempotency_key"],
            )
        return self._result


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
    completed = await CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=git,
        forge_provider=forge,
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
    ("repair_change", "expected", "stage"),
    (
        ("exact", "accepted", 0),
        ("exact", "accepted", 1),
        ("reserved", "stale", 0),
        ("extra", "stale", 0),
    ),
)
async def test_instance_bound_repair_reservation_push_and_accept_once(
    db, tmp_path, repair_change, expected, stage
):
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.git.github_app import GitHubRepositoryBinding
    from src.integration.candidates import (
        CandidateAuthorizationError,
        CandidateResolutionInput,
        CandidateService,
    )
    from src.integration.models import BranchKey, Fence
    from src.integration.ownership import BranchOwnership
    from src.integration.repair import RepairService
    from src.profiles.capabilities import CapabilityPolicy

    origin, work, base, members = _make_conflicting_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members, base_sha=base)
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    ownership = BranchOwnership(db, confirm_handoff=lambda _row: True)
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
        assert await service.push_repair(reservation_id, repair_fence) == reservation_id
    await db.update_session(session_id, state="stopped")
    accepted = await service.accept_repair(reservation_id)
    replay = await service.accept_repair(reservation_id)

    assert accepted.outcome == expected
    assert replay.outcome == ("already_accepted" if expected == "accepted" else "stale")


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
