# ruff: noqa: F401, F811
"""Main advancing must not erase an accepted batch CI repair."""

import subprocess

import pytest
from sqlalchemy import insert, select, update
from tests.test_integration_candidates import (
    db,
    _make_origin,
    _git,
    _seed_batch,
    _AppClient,
    _CrashOnce,
    _LocalPushGit,
    _AuditForge,
)
from src.git.github_app import GitHubRepositoryBinding
from src.integration.candidates import CandidateService
from src.integration.hierarchy import resolve_repair_commit_proof
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchOwnership
from src.integration.repair import RepairService
from src.integration.main_promotion import RootPromotionService
from src.database.tables import (
    integration_batches,
    integration_branch_owners,
    integration_candidate_member_results,
    integration_candidate_revisions,
    integration_repair_stages,
    tasks,
    workspaces,
)
from src.models import SessionRecord, Task, TaskStatus


@pytest.mark.parametrize("conflict", [False, True])
async def test_repeated_main_advances_keep_repair_commit_and_member_ancestry(
    db, tmp_path, conflict
):
    origin, work, base, members = _make_origin(tmp_path)
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
        clock=lambda: 110,
    )
    built = await service.build("batch")
    _git(work, "fetch", "origin", built.branch)
    _git(work, "switch", "--detach", "FETCH_HEAD")
    (work / "ci-fix.txt").write_text("keep the repair\n")
    _git(work, "add", "ci-fix.txt")
    _git(work, "commit", "-m", "repair CI")
    repaired_head = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", f"HEAD:{built.branch}")
    async with db.immediate() as conn:
        await RepairService(db).adopt_batch_repair_on(
            conn,
            "repair-batch-batch",
            head_sha=repaired_head,
            commit_proof={
                "base_sha": built.head_sha,
                "head_sha": repaired_head,
                "commits": [repaired_head],
            },
            now=111,
        )
    service.forge_provider = _AuditForge()
    adopted = await service.build("batch")
    assert adopted.head_sha == repaired_head
    current = adopted
    for index in range(2):
        _git(work, "switch", "-C", "main", base)
        (work / ("ci-fix.txt" if conflict else f"upstream-{index}.txt")).write_text("new main\n")
        _git(work, "add", ".")
        _git(work, "commit", "-m", "advance main")
        base = _git(work, "rev-parse", "HEAD")
        _git(work, "push", "origin", "main")
        service.forge_provider = _AuditForge()
        previous = current
        current = await service.rebuild("batch", current.revision, base)
        if conflict:
            assert current.outcome == "conflict"
            assert _git(origin, "rev-parse", previous.branch) == repaired_head
            async with db._engine.connect() as conn:
                batch = (await conn.execute(select(integration_batches))).mappings().one()
            assert batch["current_revision"] == previous.revision
            return
        assert current.outcome in {"built", "already_built"}
        assert _git(origin, "show", f"{current.head_sha}:ci-fix.txt") == "keep the repair"
        _git(origin, "merge-base", "--is-ancestor", repaired_head, current.head_sha)
        _git(origin, "merge-base", "--is-ancestor", base, current.head_sha)
        for member in members:
            _git(origin, "merge-base", "--is-ancestor", member[1], current.head_sha)
    async with db._engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(integration_candidate_member_results).where(
                        integration_candidate_member_results.c.revision == current.revision
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == len(members)


async def test_conflicting_main_rebuild_uses_current_stage_and_requires_fresh_ci(
    db, tmp_path
):
    origin, work, base, members = _make_origin(tmp_path)
    await db.update_repo("repo", url=str(origin))
    await _seed_batch(db, members=members, base_sha=base)
    now = {"value": 100.0}
    repair = RepairService(
        db,
        route_validator=lambda _intelligence_class, _profile_id: True,
        clock=lambda: now["value"],
    )
    app = _AppClient(origin)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    service = CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        repair_service=repair,
        clock=lambda: now["value"],
    )
    built = await service.build("batch")
    _git(work, "fetch", "origin", built.branch)
    _git(work, "switch", "--detach", "FETCH_HEAD")
    (work / "ci-fix.txt").write_text("candidate repair\n")
    _git(work, "add", "ci-fix.txt")
    _git(work, "commit", "-m", "repair candidate CI")
    repaired_head = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", f"HEAD:{built.branch}")
    async with db.immediate() as conn:
        await repair.adopt_batch_repair_on(
            conn,
            "repair-batch-batch",
            head_sha=repaired_head,
            commit_proof={
                "base_sha": built.head_sha,
                "head_sha": repaired_head,
                "commits": [repaired_head],
            },
            now=101.0,
        )
    service.forge_provider = _AuditForge()
    adopted = await service.build("batch")
    assert adopted.revision == 1

    old_delegate_id = "repair-repair-batch-batch-0"
    await db.create_task(
        Task(
            id=old_delegate_id,
            project_id="p",
            title="Completed candidate repair",
            description="prior repair",
            status=TaskStatus.COMPLETED,
            repo_id="repo",
            branch_name=adopted.branch,
            profile_id="repairer",
            intelligence_class="primary-medium",
            created_by_kind="integration_repair",
            created_by_id="repair-batch-batch",
        )
    )
    async with db.immediate() as conn:
        stage = (
            await conn.execute(select(integration_repair_stages).with_for_update())
        ).mappings().one()
        dossier = dict(stage["dossier"])
        dossier["budget"] = {**dossier["budget"], "attempts": 1}
        await conn.execute(
            update(integration_repair_stages).values(
                repair_task_id=old_delegate_id,
                writer_kind="repair_delegate",
                attempts=1,
                state="awaiting_completion",
                success_subject=stage["current_subject"],
                success_evidence_id="accepted-ci",
                dossier=dossier,
            )
        )

    _git(work, "switch", "-C", "main", base)
    (work / "ci-fix.txt").write_text("new main\n")
    _git(work, "add", "ci-fix.txt")
    _git(work, "commit", "-m", "advance main with overlap")
    new_main = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")
    now["value"] = 110.0

    # An out-of-band candidate-branch movement is never handed to a repair
    # worker. Nothing is persisted until the frozen candidate tip is current.
    _git(origin, "update-ref", adopted.branch, new_main)
    moved = await service.rebuild("batch", adopted.revision, new_main)
    assert moved.outcome == "wait"
    async with db._engine.connect() as conn:
        before_record = (
            await conn.execute(select(integration_repair_stages.c.dossier))
        ).scalar_one()
    assert "candidate_rebuild_conflict" not in before_record
    _git(origin, "update-ref", adopted.branch, repaired_head)

    # The conflict write is restartable independently of dispatch. A lost
    # response after the transaction leaves the exact subject durable and the
    # collector fence untouched; the next instance performs the handoff once.
    service.crash_hook = _CrashOnce("after_rebuild_conflict_record")
    with pytest.raises(RuntimeError, match="crash at after_rebuild_conflict_record"):
        await service.rebuild("batch", adopted.revision, new_main)
    async with db._engine.connect() as conn:
        recorded_stage = (
            await conn.execute(select(integration_repair_stages))
        ).mappings().one()
        recorded_owner = (
            await conn.execute(select(integration_branch_owners))
        ).mappings().one()
    assert recorded_stage["repair_task_id"] == old_delegate_id
    assert recorded_stage["dossier"]["candidate_rebuild_conflict"]["new_base_sha"] == new_main
    assert recorded_owner["owner_id"] == "repair-batch-batch"
    assert recorded_owner["owner_role"] == "collector"
    service = CandidateService(
        db,
        data_dir=tmp_path / "data",
        git_manager=_LocalPushGit(origin),
        forge_provider=_AuditForge(),
        app_client=app,
        repair_service=repair,
        clock=lambda: now["value"],
    )
    conflict = await service.rebuild("batch", adopted.revision, new_main)
    assert conflict.outcome == "conflict"
    replay = await service.rebuild("batch", adopted.revision, new_main)
    assert replay.outcome == "wait"
    async with db._engine.connect() as conn:
        stage = (await conn.execute(select(integration_repair_stages))).mappings().one()
        delegates = (
            (
                await conn.execute(
                    select(tasks)
                    .where(tasks.c.created_by_id == "repair-batch-batch")
                    .order_by(tasks.c.id)
                )
            )
            .mappings()
            .all()
        )
        owner = (
            await conn.execute(select(integration_branch_owners))
        ).mappings().one()
    active_delegate = next(row for row in delegates if row["status"] == "READY")
    assert active_delegate["id"] == old_delegate_id
    assert len(delegates) == 1
    assert stage["attempts"] == 1
    assert stage["deadline_at"] == 130.0
    assert stage["dossier"]["budget"]["attempts"] == 1
    frozen = stage["dossier"]["candidate_rebuild_conflict"]
    assert frozen["candidate_sha"] == repaired_head
    assert frozen["new_base_sha"] == new_main
    assert frozen["resolution"]["parents"] == [repaired_head, new_main]
    assert new_main in active_delegate["description"]
    assert owner["owner_id"] == active_delegate["id"]
    assert owner["owner_role"] == "repair"
    assert owner["handoff_state"] == "reserved"

    branch = adopted.branch.removeprefix("refs/heads/")
    _git(work, "fetch", "origin", f"{branch}:refs/remotes/origin/{branch}")
    _git(work, "switch", "-C", branch, f"refs/remotes/origin/{branch}")
    merged = subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", new_main],
        cwd=work,
        capture_output=True,
        text=True,
    )
    assert merged.returncode == 1
    (work / "ci-fix.txt").write_text("candidate repair and new main\n")
    _git(work, "add", "ci-fix.txt")
    _git(work, "commit", "-m", "resolve moved-main candidate conflict")
    resolved_head = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", branch)

    workspace_id = "root-rebuild-repair-workspace"
    session_id = "root-rebuild-repair-session"
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == active_delegate["id"])
            .values(status=TaskStatus.IN_PROGRESS.value)
        )
        await conn.execute(
            insert(workspaces).values(
                id=workspace_id,
                project_id="p",
                workspace_path=str(work),
                source_type="link",
                locked_by_task_id=active_delegate["id"],
                enabled=True,
                created_at=110.0,
            )
        )
    await db.create_session(
        SessionRecord(
            id=session_id,
            task_id=active_delegate["id"],
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="root-rebuild-repair",
            lifecycle="task",
            state="running",
            work_dir=str(work),
            epoch="test",
            instance_token="root-rebuild-instance",
            started_at=110.0,
        )
    )
    repair_fence = Fence(
        target=BranchKey(repository_id="repo", branch=adopted.branch),
        owner_id=active_delegate["id"],
        token=int(owner["fence_token"]),
    )
    await BranchOwnership(db).attach(
        repair_fence,
        session_id,
        workspace_id,
        expected_role="repair",
    )
    live_replay = await service.rebuild("batch", adopted.revision, new_main)
    assert live_replay.outcome == "wait"
    async with db._engine.connect() as conn:
        live_owner = (
            await conn.execute(select(integration_branch_owners))
        ).mappings().one()
    assert live_owner["owner_id"] == active_delegate["id"]
    assert live_owner["handoff_state"] == "attached"
    assert live_owner["session_id"] == session_id
    proof = await resolve_repair_commit_proof(
        service.git,
        str(work),
        base_sha=repaired_head,
        head_sha=resolved_head,
    )
    assert set(proof["head_parents"]) == {repaired_head, new_main}
    now["value"] = 115.0
    closed = await repair.complete_delegate(
        active_delegate["id"],
        operation_id="repair-batch-batch",
        stage=0,
        session_id=session_id,
        instance_token="root-rebuild-instance",
        workspace_id=workspace_id,
        fence_token=repair_fence.token,
        head_sha=resolved_head,
        commit_proof=proof,
        now=115.0,
    )
    assert closed["outcome"] == "completed"
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
        batch = (await conn.execute(select(integration_batches))).mappings().one()
        stage = (await conn.execute(select(integration_repair_stages))).mappings().one()
    repaired_revision = revisions[-1]
    assert repaired_revision["revision"] == 2
    assert repaired_revision["construction_base_sha"] == new_main
    assert repaired_revision["head_sha"] == resolved_head
    assert repaired_revision["ci_evidence_id"] is None
    assert batch["current_revision"] == 2
    assert batch["tested_candidate_sha"] is None
    assert batch["ci_evidence_id"] is None
    assert stage["attempts"] == 1
    assert stage["deadline_at"] == 130.0
    assert "candidate_rebuild_conflict" not in stage["dossier"]
    assert stage["dossier"]["candidate_rebuild_conflicts"][-1][
        "resolved_head_sha"
    ] == resolved_head
    _git(origin, "merge-base", "--is-ancestor", repaired_head, resolved_head)
    _git(origin, "merge-base", "--is-ancestor", new_main, resolved_head)
    for _source_base, reviewed_head, _tree in members:
        _git(origin, "merge-base", "--is-ancestor", reviewed_head, resolved_head)

    # A successful repair close creates a new untested candidate. Even with
    # the exact merge published, root promotion cannot proceed on old CI.
    promotion = await RootPromotionService(
        db, data_dir=tmp_path / "promotion-data"
    ).prepare("batch", 2)
    assert promotion.outcome == "ci_missing"
