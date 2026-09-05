"""Ordered, restartable root integration candidate construction."""

from __future__ import annotations

import subprocess
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
)
from src.integration.models import (
    ArtifactSnapshot,
    HierarchicalIntegrationPolicy,
    IntegrationBoundaryPolicy,
    PlaybookRoute,
    RepairPolicy,
    RequiredCheckSet,
)
from src.models import AgentProfile, Project, RepoConfig, RepoSourceType


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
                    owner_id="batch",
                    owner_role="collector",
                    fence_token=1,
                    handoff_state="attached",
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

    async def ensure_audit_pr(self, **kwargs):
        from src.integration.candidates import AuditPullRequest

        if self._result is None:
            self.calls.append(kwargs)
            self._result = AuditPullRequest(
                url="https://github.com/example/repo/pull/9",
                number=9,
                head_sha=kwargs["head_sha"],
                head_branch=kwargs["branch"],
                base_branch=kwargs["base_branch"],
            )
        return self._result


class _AppClient:
    repository = None

    async def installation_token(self):
        return "dummy-token"


class _LocalPushGit:
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
    app = _AppClient()
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
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    service = CandidateService(db, data_dir=tmp_path / "data", clock=lambda: 100.0)

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


async def test_conflict_dispatches_and_exact_repair_advances_once(db, tmp_path):
    from src.integration.candidates import (
        CandidateRepairLineage,
        CandidateService,
    )
    from src.integration.repair import RepairService

    origin, work, base, members = _make_conflicting_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members, base_sha=base)
    repair = RepairService(db, route_validator=lambda _intelligence_class, _profile_id: True)
    service = CandidateService(
        db,
        data_dir=tmp_path / "data",
        repair_service=repair,
        clock=lambda: 100.0,
    )

    conflict = await service.build("batch")
    replayed_conflict = await service.build("batch")

    assert conflict.outcome == "conflict"
    assert replayed_conflict == conflict
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

    service.crash_hook = _CrashOnce("after_repair_pin")
    with pytest.raises(RuntimeError, match="crash at after_repair_pin"):
        await service.accept_repair(lineage)
    service.crash_hook = None
    accepted = await service.accept_repair(lineage)
    replay = await service.accept_repair(lineage)
    completed = await service.build("batch")

    assert accepted.outcome == "accepted"
    assert replay.outcome == "already_accepted"
    assert completed.outcome == "built"
    assert completed.head_sha == repaired
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
    assert [row["result"] for row in rows] == ["applied", "applied"]
    assert rows[1]["generated_squash_sha"] == repaired


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
    app = _AppClient()
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


async def test_rebuild_reapplies_accepted_repair_and_preserves_budget(db, tmp_path):
    from src.integration.candidates import CandidateRepairLineage, CandidateService
    from src.integration.repair import RepairService

    origin, work, base, members = _make_conflicting_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members, base_sha=base)
    service = CandidateService(
        db,
        data_dir=tmp_path / "data",
        repair_service=RepairService(db),
        clock=lambda: 100.0,
    )
    conflict = await service.build("batch")
    assert conflict.head_sha
    store = next((tmp_path / "data" / "integration-repositories").iterdir())
    _git(work, "fetch", str(store), conflict.head_sha)
    _git(work, "switch", "--detach", "FETCH_HEAD")
    (work / "shared.txt").write_text("first and second\n")
    _git(work, "add", "shared.txt")
    _git(work, "commit", "-m", "resolve member 1")
    repaired = _git(work, "rev-parse", "HEAD")
    _git(work, "push", str(store), f"HEAD:refs/aq/test-repair/{repaired}")
    assert (
        await service.accept_repair(
            CandidateRepairLineage(
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
        )
    ).outcome == "accepted"
    assert (await service.build("batch")).outcome == "built"
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_stages)
            .where(integration_repair_stages.c.operation_id == "repair-batch-batch")
            .values(
                state="awaiting_completion",
                attempts=1,
                success_subject={"candidate_sha": repaired},
                success_evidence_id="green-old",
            )
        )
    _git(work, "switch", "-C", "main", base)
    (work / "upstream.txt").write_text("new base\n")
    _git(work, "add", "upstream.txt")
    _git(work, "commit", "-m", "advance main")
    new_base = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "--force", "origin", "main")

    rebuilt = await service.rebuild("batch", 0, new_base)

    assert rebuilt.outcome == "built"
    assert rebuilt.revision == 1
    assert rebuilt.head_sha
    assert _git(store, "show", f"{rebuilt.head_sha}:shared.txt") == "first and second"
    assert _git(store, "show", f"{rebuilt.head_sha}:upstream.txt") == "new base"
    async with db._engine.connect() as conn:
        revisions = (
            (
                await conn.execute(
                    select(integration_candidate_revisions).order_by(
                        integration_candidate_revisions.c.revision
                    )
                )
            )
            .mappings()
            .all()
        )
        stage = (
            (
                await conn.execute(
                    select(integration_repair_stages).where(
                        integration_repair_stages.c.operation_id == "repair-batch-batch"
                    )
                )
            )
            .mappings()
            .one()
        )
    assert [(row["revision"], row["state"]) for row in revisions] == [
        (0, "superseded"),
        (1, "built"),
    ]
    assert revisions[1]["repair_parent_revision"] == 0
    assert (
        _git(
            store,
            "rev-parse",
            "refs/aq/integration-candidates/"
            + __import__("hashlib").sha256(b"batch").hexdigest()
            + "/0",
        )
        == repaired
    )
    assert stage["started_at"] == 100.0
    assert stage["deadline_at"] == 130.0
    assert stage["attempts"] == 1
    assert stage["state"] == "active"
    assert stage["success_subject"] is None
    assert stage["success_evidence_id"] is None
    stale = await service.rebuild("batch", 0, new_base)
    assert stale.outcome == "stale_revision"
    assert stale.revision == 1


async def test_changed_reviewed_tree_fails_closed_as_source_moved(db, tmp_path):
    from src.integration.candidates import CandidateService

    origin, _work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    member = (members[0][0], members[0][1], "f" * 40)
    await _seed_batch(db, members=(member,), base_sha=base)

    result = await CandidateService(db, data_dir=tmp_path / "data").build("batch")

    assert result.outcome == "source_moved"
    async with db._engine.connect() as conn:
        revision = (await conn.execute(select(integration_candidate_revisions))).mappings().one()
    assert revision["state"] == "constructing"
    assert revision["next_member_ordinal"] == 0


async def test_overdue_rebuild_immediately_dispatches_preserved_debug_budget(db, tmp_path):
    from src.integration.candidates import CandidateService
    from src.integration.repair import RepairService

    origin, work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members[:1], base_sha=base)
    repair = RepairService(db, route_validator=lambda _intelligence_class, _profile_id: True)
    first = await CandidateService(
        db, data_dir=tmp_path / "data", repair_service=repair, clock=lambda: 100.0
    ).build("batch")
    _git(work, "switch", "-C", "main", base)
    (work / "upstream.txt").write_text("new base\n")
    _git(work, "add", "upstream.txt")
    _git(work, "commit", "-m", "advance main")
    new_base = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "--force", "origin", "main")

    result = await CandidateService(
        db, data_dir=tmp_path / "data", repair_service=repair, clock=lambda: 200.0
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

    result = await CandidateService(db, data_dir=tmp_path / "data").build("batch")

    assert result.outcome == "conflict"
    async with db._engine.connect() as conn:
        revision = (await conn.execute(select(integration_candidate_revisions))).mappings().one()
        member = (await conn.execute(select(integration_candidate_member_results))).mappings().one()
    assert revision["state"] == "constructing"
    assert revision["next_member_ordinal"] == 0
    assert member["result"] == "conflict"
    assert member["conflict_evidence"]["detail"] == "reserved_path"
