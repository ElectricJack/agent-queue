# ruff: noqa: F401, F811
"""Main advancing must not erase an accepted batch CI repair."""

import pytest
from sqlalchemy import select
from tests.test_integration_candidates import (
    db,
    _make_origin,
    _git,
    _seed_batch,
    _AppClient,
    _LocalPushGit,
    _AuditForge,
)
from src.git.github_app import GitHubRepositoryBinding
from src.integration.candidates import CandidateService
from src.integration.repair import RepairService
from src.database.tables import integration_candidate_member_results, integration_batches


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
            assert current.outcome == "human_required"
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
