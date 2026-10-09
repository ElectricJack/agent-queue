"""Explicit decisions delete real Git refs only after a durable SHA audit."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, insert, select, update

from src.database import Database
from src.database.tables import (
    branch_deletion_audit, branch_retirements, integration_batches,
    integration_branch_owners, projects, tasks,
)
from src.git.github_contracts import GitHubRepositoryBinding
from src.git.manager import GitError, GitManager
from src.integration.branch_retirement import (
    BranchRetirementService,
    request_branch_retirement_on,
    request_task_retirement_on,
)
from src.integration.cleanup import IntegrationCleanupService
from src.integration.batches import Batch, BatchMember, BatchStore, candidate_ref
from src.integration.train_sources import RETAINED_CANDIDATE_PREFIX
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskCompletion, TaskStatus
from src.sessions.obsolete import ObsoleteClose
from tests.db_fixtures import lease_dsn

BRANCH = "aq/retired"
BINDING = GitHubRepositoryBinding(1234, "example/project")


class LocalTransport(GitManager):
    """Only authentication is replaced; backup, refs and leases use real Git."""

    def __init__(self, remote):
        super().__init__()
        self.remote = str(remote)
        self.before_delete = None
        self.fail_delete = False

    async def afetch_repository_oid(self, checkout, *, repository, oid, destination_ref):
        await self._arun(["fetch", "--no-tags", self.remote, oid], cwd=checkout)
        await self._arun(["update-ref", destination_ref, oid], cwd=checkout)
        return oid

    async def adelete_repository_ref(
        self, checkout, *, repository, branch, expected_old_oid, authority_deadline=None,
    ):
        if self.before_delete:
            await self.before_delete(branch, expected_old_oid)
        if self.fail_delete:
            raise GitError("transport failed")
        await self._arun([
            "push", f"--force-with-lease=refs/heads/{branch}:{expected_old_oid}",
            self.remote, ":refs/heads/" + branch,
        ], cwd=checkout)


@pytest.fixture
async def setup(tmp_path):
    remote, checkout = tmp_path / "remote.git", tmp_path / "checkout"
    git = LocalTransport(remote)
    await git._arun(["init", "--bare", "--template=", str(remote)], cwd=str(tmp_path))
    await git._arun(["init", "-b", "main", "--template=", str(checkout)], cwd=str(tmp_path))
    (checkout / "base.txt").write_text("base\n")
    await git._arun(["add", "."], cwd=str(checkout))
    await git._arun(["commit", "-m", "base"], cwd=str(checkout))
    await git._arun(["remote", "add", "origin", str(remote)], cwd=str(checkout))
    await git._arun(["push", "origin", "main"], cwd=str(checkout))
    await git._arun(["checkout", "-b", BRANCH], cwd=str(checkout))
    (checkout / "work.txt").write_text("unique abandoned work\n")
    await git._arun(["add", "."], cwd=str(checkout))
    await git._arun(["commit", "-m", "work to abandon"], cwd=str(checkout))
    await git._arun(["push", "origin", BRANCH], cwd=str(checkout))
    head = await git.arev_parse(str(checkout), BRANCH)
    await git._arun(["checkout", "main"], cwd=str(checkout))
    db = Database(lease_dsn("branch-retirement"))
    await db.initialize()
    await db.create_project(Project(id="p", name="project"))
    await db.create_repo(RepoConfig(
        id="repo", project_id="p", source_type=RepoSourceType.CLONE, url=str(remote),
        checkout_base_path=str(checkout),
    ))
    await db.create_task(Task(
        id="retired", project_id="p", repo_id="repo", title="retire", description="",
        branch_name=BRANCH, status=TaskStatus.COMPLETED,
    ))
    client = AsyncMock()
    client.repository = BINDING

    async def exact_ref(branch):
        return await git.arev_parse(str(remote), "refs/heads/" + branch)

    client.exact_head_ref.side_effect = exact_ref
    service = BranchRetirementService(
        db, data_dir=tmp_path / "data", git_manager=git,
        github_client_factory=lambda binding: client,
        github_repository_binding_resolver=lambda repository: BINDING,
    )
    yield db, git, service, checkout, remote, head
    await db.close()


async def request(db, *, request_id="decision", reason="abandoned"):
    async with db.immediate() as conn:
        await request_task_retirement_on(conn, "retired", request_id=request_id, reason=reason)


async def rows(db):
    async with db._engine.connect() as conn:
        return [dict(row) for row in (await conn.execute(
            select(branch_retirements).order_by(branch_retirements.c.branch)
        )).mappings()]


@pytest.mark.parametrize("decision", ["abandoned", "failed", "archived"])
async def test_daily_backstop_recovers_missed_abandonment_and_audits_unique_work(setup, decision):
    db, git, service, checkout, remote, head = setup
    if decision == "abandoned":
        await db.set_task_meta("retired", "work_outcome", "abandoned")
    elif decision == "failed":
        await db.update_task("retired", status=TaskStatus.FAILED)
    else:
        await db.archive_task("retired")
        async with db.immediate() as conn:
            await conn.execute(delete(branch_retirements))
    assert await rows(db) == []
    outcomes = await service.reconcile_project("p")
    assert outcomes and all(state == "complete" for state, _ in outcomes)
    audit = next(row for row in await rows(db) if row["branch"] == BRANCH)
    assert audit["evidence"]["remote"]["sha"] == head
    assert Path(audit["evidence"]["remote"]["bundle"]).is_file()
    assert await git.arev_parse(str(remote), BRANCH) is None
    assert await git.arev_parse(str(checkout), BRANCH) is None
    assert await service.reconcile_project("p") == []


async def test_daily_backstop_keeps_completed_unique_work_without_abandonment(setup):
    db, git, service, _checkout, remote, head = setup
    assert await service.reconcile_project("p") == []
    assert await rows(db) == []
    assert await git.arev_parse(str(remote), BRANCH) == head


@pytest.mark.parametrize("status", [TaskStatus.READY, TaskStatus.PAUSED, TaskStatus.WAITING_INPUT])
async def test_daily_backstop_does_not_retire_live_task_even_with_old_abandon_marker(setup, status):
    db, git, service, _checkout, remote, head = setup
    await db.set_task_meta("retired", "work_outcome", "abandoned")
    await db.update_task("retired", status=status)
    assert await service.reconcile_project("p") == []
    assert await rows(db) == []
    assert await git.arev_parse(str(remote), BRANCH) == head


async def test_abandon_deletes_local_and_remote_after_committing_audit(setup):
    db, git, service, checkout, remote, head = setup
    await ObsoleteClose(db, release_owner=None, git_manager=git).close(
        "retired", reason="superseded work", principal="human:test",
    )

    async def audit_is_already_committed(branch, sha):
        audit = (await rows(db))[0]
        assert audit["branch"] == branch
        assert audit["evidence"]["remote"]["sha"] == sha == head
        assert audit["evidence"]["local:" + str(checkout)]["sha"] == head
        assert Path(audit["evidence"]["local:" + str(checkout)]["bundle"]).is_file()

    git.before_delete = audit_is_already_committed
    outcomes = await service.drain_due()
    assert all(state == "complete" for state, _ in outcomes), outcomes
    assert await git.arev_parse(str(remote), "refs/heads/" + BRANCH) is None
    assert await git.arev_parse(str(checkout), "refs/heads/" + BRANCH) is None
    assert await git.arev_parse(str(checkout), "refs/remotes/origin/" + BRANCH) is None
    audit = (await rows(db))[0]
    assert audit["state"] == "complete"
    assert audit["evidence"]["remote"]["sha"] == head
    assert audit["evidence"]["remote"]["state"] == "deleted"
    assert audit["evidence"]["tracking:" + str(checkout)]["sha"] == head
    assert Path(audit["evidence"]["remote"]["bundle"]).is_file()
    bundle = audit["evidence"]["local:" + str(checkout)]["bundle"]
    restored = checkout.parent / "restore.git"
    await git._arun(["init", "--bare", str(restored)], cwd=str(checkout.parent))
    await git._arun(["bundle", "unbundle", bundle], cwd=str(restored))
    assert await git.arev_parse(str(restored), head) == head
    assert await service.drain_due() == []


@pytest.mark.parametrize("decision", ["archive", "failed-close", "abandoned-close", "cancel"])
async def test_lifecycle_decisions_queue_retirement_atomically(setup, decision):
    db, git, service, checkout, remote, _head = setup
    if decision == "archive":
        await db.archive_task("retired")
        assert await db.get_task("retired") is None
    elif decision == "cancel":
        await db.transition_task("retired", TaskStatus.IN_PROGRESS, force=True)
        await db.transition_task("retired", TaskStatus.BLOCKED, context="stop_task")
    else:
        await db.save_task_completion(TaskCompletion(
            id="close-record", task_id="retired", outcome="fail" if decision == "failed-close"
            else "pass", work_outcome="abandoned" if decision == "abandoned-close" else None,
            branch=BRANCH,
        ))
    queued = await rows(db)
    assert {row["branch"] for row in queued} == {BRANCH, BRANCH + "-wip"}
    assert all(state == "complete" for state, _ in await service.drain_due())
    assert await git.arev_parse(str(remote), "refs/heads/" + BRANCH) is None
    assert await git.arev_parse(str(checkout), "refs/heads/" + BRANCH) is None


async def test_failed_close_that_retries_keeps_its_work(setup):
    db, git, service, checkout, remote, head = setup
    await db.transition_task("retired", TaskStatus.READY, force=True)
    await db.save_task_completion(TaskCompletion(id="failed", task_id="retired", outcome="fail"))
    assert all(state == "withdrawn" for state, _ in await service.drain_due())
    assert await git.arev_parse(str(remote), BRANCH) == head
    assert await git.arev_parse(str(checkout), BRANCH) == head


async def test_comment_timestamp_does_not_cancel_an_abandon_decision(setup):
    db, git, service, _checkout, remote, _head = setup
    await request(db)
    async with db.immediate() as conn:
        await conn.execute(update(tasks).values(updated_at=service.clock() + 10))
    assert all(state == "complete" for state, _ in await service.drain_due())
    assert await git.arev_parse(str(remote), BRANCH) is None


async def test_new_completion_invalidates_an_older_abandon_decision(setup):
    db, git, service, _checkout, remote, head = setup
    await request(db)
    await db.save_task_completion(TaskCompletion(
        id="new-work", task_id="retired", outcome="pass", completed_at=service.clock(),
    ))
    assert all(state == "withdrawn" for state, _ in await service.drain_due())
    assert await git.arev_parse(str(remote), BRANCH) == head


async def test_same_status_reclaim_invalidates_the_old_decision(setup):
    db, git, service, _checkout, remote, head = setup
    await request(db)
    async with db.immediate() as conn:
        await conn.execute(update(tasks).values(claim_epoch=1))
    assert all(state == "withdrawn" for state, _ in await service.drain_due())
    assert await git.arev_parse(str(remote), BRANCH) == head


async def test_container_abandonment_retires_descendant_branches(setup):
    db, git, service, checkout, remote, _head = setup
    await db.create_task(Task(
        id="container", project_id="p", title="container", description="",
        status=TaskStatus.IN_PROGRESS,
    ))
    await db.transition_task("retired", TaskStatus.READY, force=True)
    async with db.immediate() as conn:
        await db.set_parent("retired", "container", conn=conn)
        result = await db.abandon_subtree("container", conn=conn)
    assert result.abandoned == ["retired"]
    assert all(state == "complete" for state, _ in await service.drain_due())
    assert await git.arev_parse(str(remote), BRANCH) is None
    assert await git.arev_parse(str(checkout), BRANCH) is None


async def test_unreleased_owner_is_never_deleted_even_after_expiry(setup):
    db, git, service, _checkout, remote, head = setup
    await request(db)
    async with db.immediate() as conn:
        await conn.execute(insert(integration_branch_owners).values(
            id="owner", repository_id="repo", ref="refs/heads/" + BRANCH, owner_id="writer",
            owner_role="worker", fence_token=1, handoff_state="attached", expires_at=1.0,
            created_at=1.0, updated_at=1.0,
        ))
    outcomes = await service.drain_due()
    assert all(state == "pending" for state, _ in outcomes)
    assert await git.arev_parse(str(remote), BRANCH) == head
    assert (await rows(db))[0]["evidence"] == {}


async def test_attached_worktree_keeps_both_refs_until_it_detaches(setup):
    db, git, service, checkout, remote, head = setup
    await request(db)
    await git._arun(["checkout", BRANCH], cwd=str(checkout))
    assert (await service.drain_due())[0][0] == "pending"
    assert await git.arev_parse(str(remote), BRANCH) == head
    await git._arun(["checkout", "main"], cwd=str(checkout))
    async with db.immediate() as conn:
        await conn.execute(update(branch_retirements).values(next_attempt_at=0))
    assert all(state == "complete" for state, _ in await service.drain_due())


async def test_transport_failure_then_moved_tip_parks_a_conflict(setup):
    db, git, service, checkout, remote, head = setup
    await request(db)
    git.fail_delete = True
    assert (await service.drain_due())[0][0] == "pending"
    await git._arun(["update-ref", "refs/heads/" + BRANCH, "refs/heads/main"], cwd=str(remote))
    async with db.immediate() as conn:
        await conn.execute(update(branch_retirements).values(next_attempt_at=0))
    assert (await service.drain_due())[0][0] == "conflict"
    assert (await rows(db))[0]["evidence"]["remote"]["sha"] == head
    assert await git.arev_parse(str(checkout), BRANCH) == head


async def test_request_rollback_and_replay_do_not_create_spurious_intents(setup):
    db, _git, _service, _checkout, _remote, _head = setup
    with pytest.raises(RuntimeError):
        async with db.immediate() as conn:
            await request_task_retirement_on(conn, "retired", request_id="rolled-back", reason="x")
            raise RuntimeError("transaction rolled back")
    assert await rows(db) == []
    await request(db)
    await request(db)
    assert len(await rows(db)) == 2


async def test_protected_flow_target_inside_aq_namespace_survives(setup):
    db, git, service, _checkout, remote, head = setup
    async with db.immediate() as conn:
        await conn.execute(update(projects).values(promotion_flow=[{
            "id": "release", "source": "main", "target": BRANCH,
        }]))
    await request(db)
    assert (await service.drain_due())[0][0] == "conflict"
    assert await git.arev_parse(str(remote), BRANCH) == head


async def test_aborted_candidate_cleanup_uses_the_same_audit(setup):
    db, git, retirements, checkout, remote, head = setup
    candidate = candidate_ref("batch")
    retained = RETAINED_CANDIDATE_PREFIX + "batch"
    await git._arun(["push", "origin", f"{head}:{candidate}"], cwd=str(checkout))
    publisher = checkout.parent / "publisher"
    await git._arun(["clone", "--no-checkout", str(remote), str(publisher)], cwd=str(checkout))
    await git._arun(["update-ref", retained, head], cwd=str(publisher))
    retirements.candidate_store = lambda repository: publisher
    batch = await aborted_batch(db, head)
    await db.transition_task("retired", TaskStatus.READY, force=True)
    cleanup = IntegrationCleanupService(
        db, data_dir=checkout.parent / "data", retirement_service=retirements,
    )
    assert await cleanup._cleanup_aborted_candidate(batch)
    audited = await rows(db)
    assert {row["branch"] for row in audited} == {
        candidate.removeprefix("refs/heads/"), retained.removeprefix("refs/heads/"),
    }
    audit = next(row for row in audited if row["branch"] == candidate.removeprefix("refs/heads/"))
    assert audit["request_id"] == "abort:batch"
    assert audit["evidence"]["remote"]["sha"] == head
    assert await git.arev_parse(str(remote), candidate) is None
    assert await git.arev_parse(str(publisher), retained) is None
    local = next(row for row in audited if row["branch"] == retained.removeprefix("refs/heads/"))
    assert local["evidence"]["local:" + str(publisher)]["sha"] == head
    assert await git.arev_parse(str(remote), BRANCH) == head
    assert await git.arev_parse(str(checkout), BRANCH) == head


async def aborted_batch(db, head, *, aborted=True):
    async with db.immediate() as conn:
        await conn.execute(insert(integration_batches).values(
            id="batch", project_id="p", repository_id="repo", request_id="abort-request",
            target_ref="refs/heads/main", integration_branch=candidate_ref("batch"),
            intent="aborted" if aborted else "open",
            lifecycle="aborted" if aborted else "failed", source_manifest_digest="digest",
            base_sha=head, policy_snapshot={}, artifact_snapshot={}, cleanup_state="pending",
            created_at=1, updated_at=1,
        ))
    return await db.get_integration_batch("batch")


async def test_abort_decision_queues_private_refs_but_preview_does_not(setup):
    db, _git, service, _checkout, _remote, head = setup
    await aborted_batch(db, head, aborted=False)
    store = BatchStore(db)
    await store.set_intent("batch", "aborted", dry_run=True)
    assert await rows(db) == []
    assert (await store.get("batch")).intent == "open"
    await store.set_intent("batch", "aborted")
    assert {row["branch"] for row in await rows(db)} == {
        candidate_ref("batch").removeprefix("refs/heads/"),
        (RETAINED_CANDIDATE_PREFIX + "batch").removeprefix("refs/heads/"),
    }
    assert all(state == "complete" for state, _ in await service.drain_due())


async def test_supersede_queues_and_deletes_private_candidates_preserving_sources(setup):
    db, git, service, checkout, remote, head = setup
    store = BatchStore(db)
    batch = await store.freeze(
        Batch(id="batch", project_id="p", repository_id="repo", target_ref="refs/heads/main"),
        [BatchMember(task_id="retired", source_sha=head, source_base_sha=head)],
        trees={"retired": await git.arev_parse(str(checkout), head + "^{tree}")},
    )
    candidate = candidate_ref("batch")
    await git._arun(["push", "origin", f"{head}:{candidate}"], cwd=str(checkout))
    assert await store.supersede(batch, "retired", reason="refreshed source")
    audited = await rows(db)
    assert len(audited) == 2
    assert all(audit["reason"] == "superseded batch candidate" for audit in audited)
    assert all(state == "complete" for state, _ in await service.drain_due())
    assert await git.arev_parse(str(remote), candidate) is None
    assert await git.arev_parse(str(remote), BRANCH) == head


@pytest.mark.parametrize("outcome", ["pending", "held", "failed", "moved-head"])
async def test_retained_wip_audit_is_imported_with_its_original_sha(setup, outcome):
    db, git, service, checkout, remote, head = setup
    async with db.immediate() as conn:
        await conn.execute(insert(branch_deletion_audit).values(
            id="previous", project_id="p", repository_id="repo", task_id="retired",
            branch=BRANCH, head_sha=head, reason="obsolete_close",
            outcome="pending" if outcome == "moved-head" else outcome, recorded_at=service.clock(),
        ))
    if outcome == "moved-head":
        await git._arun(["update-ref", "refs/heads/" + BRANCH, "refs/heads/main"], cwd=str(remote))
    state = "conflict" if outcome == "moved-head" else "complete"
    assert await service.drain_due() == [(state, "remote branch moved since the previous audit"
                                         if state == "conflict" else None)]
    audit = (await rows(db))[0]
    assert audit["evidence"]["previous_audit"]["sha"] == head
    async with db._engine.connect() as conn:
        previous = (await conn.execute(select(branch_deletion_audit))).mappings().one()
    assert previous["head_sha"] == head
    assert previous["outcome"] == ("moved" if outcome == "moved-head" else "deleted")
    assert await service.drain_due() == []
    assert (await git.arev_parse(str(checkout), BRANCH) is None) == (state == "complete")


async def test_aborted_batch_does_not_authorize_deleting_a_source(setup):
    db, git, service, _checkout, remote, head = setup
    await aborted_batch(db, head)
    async with db.immediate() as conn:
        identity = await request_branch_retirement_on(
            conn, request_id="abort:batch", project_id="p", repository_id="repo",
            branch=BRANCH, reason="invalid candidate",
        )
    assert (await service.advance(identity))[0] == "conflict"
    assert await git.arev_parse(str(remote), BRANCH) == head


async def test_premature_abort_request_cannot_delete_a_candidate(setup):
    db, git, service, checkout, remote, head = setup
    await aborted_batch(db, head, aborted=False)
    candidate = candidate_ref("batch")
    await git._arun(["push", "origin", f"{head}:{candidate}"], cwd=str(checkout))
    async with db.immediate() as conn:
        identity = await request_branch_retirement_on(
            conn, request_id="abort:batch", project_id="p", repository_id="repo",
            branch=candidate, reason="aborted candidate",
        )
    assert (await service.advance(identity))[0] == "withdrawn"
    assert await git.arev_parse(str(remote), candidate) == head


async def test_local_only_unmerged_tip_is_bundled_and_removed(setup):
    db, git, service, checkout, remote, head = setup
    await git._arun(["update-ref", "-d", "refs/heads/" + BRANCH], cwd=str(remote))
    await request(db)
    assert all(state == "complete" for state, _ in await service.drain_due())
    audit = (await rows(db))[0]
    assert audit["evidence"]["remote"]["sha"] is None
    assert audit["evidence"]["local:" + str(checkout)]["sha"] == head
    assert await git.arev_parse(str(checkout), BRANCH) is None


async def test_remote_branch_appearing_after_absent_audit_survives(setup, monkeypatch):
    db, git, service, checkout, remote, head = setup
    await git._arun(["update-ref", "-d", "refs/heads/" + BRANCH], cwd=str(remote))
    await request(db)
    audit = service._audit

    async def appear_after_committed_audit(row, evidence):
        await audit(row, evidence)
        if row["branch"] == BRANCH:
            await git._arun(["update-ref", "refs/heads/" + BRANCH, head], cwd=str(remote))

    monkeypatch.setattr(service, "_audit", appear_after_committed_audit)
    assert (await service.drain_due())[0][0] == "conflict"
    assert (await rows(db))[0]["evidence"]["remote"]["sha"] is None
    assert await git.arev_parse(str(remote), BRANCH) == head
    assert await git.arev_parse(str(checkout), BRANCH) == head


@pytest.mark.parametrize("ref", ["refs/heads/" + BRANCH, "refs/remotes/other/" + BRANCH])
async def test_tracking_cleanup_cannot_delete_local_heads_or_other_remotes(setup, ref):
    _db, git, _service, checkout, _remote, head = setup
    with pytest.raises(GitError):
        await git.adelete_remote_tracking_ref_exact(str(checkout), ref=ref, expected_old_oid=head)
    assert await git.arev_parse(str(checkout), "refs/heads/" + BRANCH) == head
