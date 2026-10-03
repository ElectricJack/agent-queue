"""Externally delivered, completed repair trains can release their sweep safely."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, insert, select, update

from src.commands.contracts.integration import IntegrationSettleDeliveredBatchArgs
from src.database import tables as t
from src.git.manager import GitManager
from src.integration.batch_settlement import AUDIT_KEY, DeliveredBatchSettlement
from src.integration.promotion import PromotionService
from src.integration.scheduler import IntegrationScheduler, TrainService
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus


BRANCH = "refs/heads/aq/integration/batch"


def git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
async def case(tmp_path, reuse_database):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git(checkout, "init", "--initial-branch=main")
    git(checkout, "config", "user.name", "Settlement Test")
    git(checkout, "config", "user.email", "settlement@example.invalid")
    (checkout / "base").write_text("base")
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "base")
    base = git(checkout, "rev-parse", "HEAD")
    heads, trees, generated = [], [], []
    current = base
    for ordinal in range(2):
        git(checkout, "checkout", "-b", f"source-{ordinal}", base)
        (checkout / f"member-{ordinal}").write_text(f"source {ordinal}")
        git(checkout, "add", ".")
        git(checkout, "commit", "-m", f"member {ordinal}")
        head = git(checkout, "rev-parse", "HEAD")
        tree = git(checkout, "rev-parse", "HEAD^{tree}")
        merged_tree = git(checkout, "merge-tree", "--write-tree", current, head).splitlines()[0]
        current = git(
            checkout,
            "commit-tree",
            merged_tree,
            "-p",
            current,
            "-p",
            head,
            "-m",
            f"integrate member {ordinal}",
        )
        heads.append(head)
        trees.append(tree)
        generated.append(current)
    candidate = current
    git(checkout, "checkout", "main")
    git(checkout, "reset", "--hard", candidate)
    git(checkout, "commit", "--allow-empty", "-m", "external follow-up")
    target = git(checkout, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(checkout, "push", str(remote), "main", f"{candidate}:{BRANCH}")

    db = await reuse_database("batch-settlement")
    await db.create_project(Project(id="p", name="project"))
    await db.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url=str(remote),
            default_branch="main",
        )
    )
    await db.update_project(
        "p",
        integration_repository_id="repo",
        hierarchical_integration_mode="train",
    )
    for task_id, status, repair in (
        ("source-0", TaskStatus.COMPLETED, False),
        ("source-1", TaskStatus.COMPLETED, False),
        ("old-repair", TaskStatus.FAILED, True),
        ("repair", TaskStatus.COMPLETED, True),
    ):
        await db.create_task(
            Task(
                id=task_id,
                project_id="p",
                title=task_id,
                description="",
                status=status,
                repo_id="repo",
                branch_name=BRANCH.removeprefix("refs/heads/") if repair else task_id,
                created_by_kind="integration_repair" if repair else None,
                created_by_id="operation" if repair else None,
            )
        )
    scheduler = IntegrationScheduler(db)
    await scheduler.configure(project_id="p", now=1, enabled=True, interval_seconds=300)
    request = await scheduler.mark_due(project_id="p", now=10, trigger="manual")
    members = []
    async with db.immediate() as conn:
        await conn.execute(
            update(t.projects)
            .where(t.projects.c.id == "p")
            .values(hierarchical_integration_desired_mode="train")
        )
        await conn.execute(
            insert(t.integration_batches).values(
                id="batch",
                project_id="p",
                repository_id="repo",
                request_id=request["request_id"],
                trigger="manual",
                source_manifest_digest="frozen",
                base_sha=base,
                lifecycle="sealing",
                current_revision=2,
                integration_branch=BRANCH,
                policy_snapshot={"required_checks": ["unit"]},
                artifact_snapshot={},
                cleanup_state="pending",
                created_at=10,
                updated_at=10,
            )
        )
        for ordinal in range(2):
            review = dict(
                id=f"review-{ordinal}",
                source_task_id=f"source-{ordinal}",
                repository_id="repo",
                source_base=base,
                reviewed_head_sha=heads[ordinal],
                reviewed_tree_sha=trees[ordinal],
                review_kind="trusted",
                generation=0,
                verdict="approved",
                evidence={},
                created_at=10,
            )
            await conn.execute(insert(t.integration_review_evidence).values(**review))
            member = dict(
                batch_id="batch",
                ordinal=ordinal,
                task_id=f"source-{ordinal}",
                repository_id="repo",
                source_base_sha=base,
                reviewed_head_sha=heads[ordinal],
                reviewed_tree_sha=trees[ordinal],
                review_evidence_id=review["id"],
                review_evidence=review,
            )
            await conn.execute(insert(t.integration_batch_members).values(**member))
        members = [
            dict(row)
            for row in (
                await conn.execute(
                    select(t.integration_batch_members).order_by(
                        t.integration_batch_members.c.ordinal
                    )
                )
            ).mappings()
        ]
        digest = TrainService._manifest_digest(
            [
                dict(
                    task_id=m["task_id"],
                    repository_id=m["repository_id"],
                    source_base=m["source_base_sha"],
                    source_head=m["reviewed_head_sha"],
                    review={
                        "id": m["review_evidence_id"],
                        "reviewed_tree_sha": m["reviewed_tree_sha"],
                    },
                    source_ref=m["source_ref"],
                    source_ref_retention=m["source_ref_retention"],
                )
                for m in members
            ]
        )
        await conn.execute(update(t.integration_batches).values(source_manifest_digest=digest))
        await conn.execute(update(t.integration_batches).values(lifecycle="testing"))
        await conn.execute(
            insert(t.integration_candidate_revisions).values(
                batch_id="batch",
                revision=2,
                construction_base_sha=base,
                source_manifest=members,
                next_member_ordinal=2,
                head_sha=candidate,
                state="built",
                created_at=10,
                updated_at=10,
            )
        )
        for ordinal in range(2):
            await conn.execute(
                insert(t.integration_candidate_member_results).values(
                    batch_id="batch",
                    revision=2,
                    member_ordinal=ordinal,
                    input_head_sha=heads[ordinal],
                    input_tree_sha=trees[ordinal],
                    generated_squash_sha=generated[ordinal],
                    result="applied",
                    created_at=10,
                    updated_at=10,
                )
            )
        await conn.execute(
            insert(t.integration_repair_operations).values(
                id="operation",
                batch_id="batch",
                target_kind="batch",
                episode_id="batch",
                active_stage=1,
                state="escalated",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="checks-v1",
                created_at=10,
                updated_at=10,
            )
        )
        for ordinal, task_id, state in ((0, "old-repair", "failed"), (1, "repair", "active")):
            await conn.execute(
                insert(t.integration_repair_stages).values(
                    operation_id="operation",
                    ordinal=ordinal,
                    policy={},
                    repair_task_id=task_id,
                    writer_kind="repair_delegate",
                    starting_sha=candidate,
                    state=state,
                    attempts=1,
                    started_at=10,
                    deadline_at=500,
                    dossier={"original": "keep"},
                )
            )
        await conn.execute(
            insert(t.task_completion_records).values(
                id="completion",
                task_id="repair",
                outcome="pass",
                work_outcome="no-op",
                commits="[]",
                completed_at=20,
            )
        )
        await conn.execute(
            insert(t.project_integration_leases).values(
                project_id="p",
                repository_id="repo",
                batch_id="batch",
                owner_id="sealer",
                fence_token=4,
                heartbeat_at=10,
                expires_at=500,
            )
        )
        await conn.execute(
            insert(t.integration_branch_owners).values(
                id="owner",
                repository_id="repo",
                ref=BRANCH,
                owner_id="repair",
                owner_role="repair",
                handoff_state="reserved",
                fence_token=3,
                created_at=10,
                updated_at=10,
            )
        )
        await conn.execute(
            insert(t.task_delivery_receipts).values(
                id="receipt",
                domain_key="historical-delivery",
                repository_id="repo",
                target_branch="refs/heads/main",
                disposition="code",
                created_at=9,
            )
        )
    promotion = PromotionService(
        db, data_dir=tmp_path / "data", git_manager=GitManager(), clock=lambda: 30
    )
    return SimpleNamespace(
        db=db,
        service=DeliveredBatchSettlement(promotion),
        scheduler=scheduler,
        checkout=checkout,
        remote=remote,
        base=base,
        candidate=candidate,
        target=target,
        heads=heads,
        members=members,
    )


async def row(case, table):
    async with case.db._engine.connect() as conn:
        return dict((await conn.execute(select(table))).mappings().one())


async def preview(case):
    return await case.service.run(
        IntegrationSettleDeliveredBatchArgs(batch_id="batch"), principal="operator:test"
    )


def apply_request(result, **overrides):
    return IntegrationSettleDeliveredBatchArgs(
        **(
            dict(
                batch_id="batch",
                dry_run=False,
                expected_candidate_sha=result["candidate_sha"],
                expected_target_sha=result["target_sha"],
                expected_snapshot_digest=result["snapshot_digest"],
                reason="Candidate independently delivered",
            )
            | overrides
        )
    )


async def test_completed_noop_external_delivery_releases_sweep_and_is_idempotent(case):
    receipt = await row(case, t.task_delivery_receipts)
    before = git(case.remote, "show-ref")
    result = await preview(case)
    assert result["outcome"] == "would_settle", result
    assert result["member_count"] == 2
    assert result["target_sha"] == case.target
    assert result["validation"] == "external delivery; not CI attested"
    assert (await row(case, t.integration_batches))["lifecycle"] == "testing"
    request = apply_request(result)
    applied = await case.service.run(request, principal="operator:test")
    assert applied["outcome"] == "settled", applied
    batch = await row(case, t.integration_batches)
    assert batch["lifecycle"] == "aborted"
    assert batch["final_main_sha"] == case.target
    assert batch["tested_candidate_sha"] is None and batch["ci_evidence_id"] is None
    assert (await row(case, t.integration_candidate_revisions))["state"] == "built"
    assert (await row(case, t.integration_repair_operations))["state"] == "cancelled"
    async with case.db._engine.connect() as conn:
        stages = (
            (
                await conn.execute(
                    select(t.integration_repair_stages).order_by(
                        t.integration_repair_stages.c.ordinal
                    )
                )
            )
            .mappings()
            .all()
        )
        assert [s["state"] for s in stages] == ["failed", "cancelled"]
        assert stages[1]["attempts"] == 1 and stages[1]["deadline_at"] == 500
        assert stages[1]["dossier"]["original"] == "keep"
        assert stages[1]["dossier"][AUDIT_KEY]["repair_completion_ids"] == ["completion"]
        assert (await conn.execute(select(t.project_integration_leases))).first() is None
    assert (await row(case, t.project_integration_schedules))["outstanding_request_id"] is None
    assert (await row(case, t.integration_branch_owners))["handoff_state"] == "released"
    assert await row(case, t.task_delivery_receipts) == receipt
    assert git(case.remote, "show-ref") == before
    replay = await case.service.run(request, principal="operator:test")
    assert replay["outcome"] == "already_settled"
    assert replay["operator_id"] == "operator:test"
    different = await case.service.run(
        apply_request(result, expected_target_sha=case.base), principal="operator:test"
    )
    assert different["outcome"] == "changed"
    next_request = await case.scheduler.mark_due(project_id="p", now=40, trigger="manual")
    assert next_request["outcome"] == "due"


async def test_remote_head_moved_since_preview_requires_a_new_preview(case):
    result = await preview(case)
    assert result["outcome"] == "would_settle", result
    git(case.checkout, "commit", "--allow-empty", "-m", "new main")
    git(case.checkout, "push", str(case.remote), "main")
    changed = await case.service.run(apply_request(result), principal="operator:test")
    assert changed["outcome"] == "changed"
    assert (await row(case, t.integration_batches))["lifecycle"] == "testing"


async def test_remote_moving_during_apply_does_not_partially_settle(case, monkeypatch):
    result = await preview(case)
    assert result["outcome"] == "would_settle", result
    observe = case.service._target
    observations = 0

    async def moved(store, target_ref, origin_url):
        nonlocal observations
        observations += 1
        return case.base if observations == 2 else await observe(store, target_ref, origin_url)

    monkeypatch.setattr(case.service, "_target", moved)
    changed = await case.service.run(apply_request(result), principal="operator:test")
    assert changed["outcome"] == "changed"
    assert (await row(case, t.integration_repair_operations))["state"] == "escalated"
    assert (await row(case, t.project_integration_schedules))["outstanding_request_id"]


async def test_candidate_not_delivered_refuses(case):
    git(case.checkout, "push", "--force", str(case.remote), f"{case.base}:refs/heads/main")
    result = await preview(case)
    assert result["outcome"] == "blocked" and "ancestry" in result["reason"]


async def test_every_frozen_member_must_be_reachable(case):
    async with case.db.immediate() as conn:
        # Source two is complete but absent from the first generated member.
        await conn.execute(
            update(t.integration_candidate_revisions).values(
                head_sha=git(case.checkout, "rev-parse", f"{case.candidate}^1")
            )
        )
    result = await preview(case)
    assert result["outcome"] == "blocked" and "ancestry" in result["reason"]


@pytest.mark.parametrize(
    "kind",
    ["hold", "gate", "writer", "incomplete", "human_required", "completion", "reservation", "tree"],
)
async def test_unsafe_or_ambiguous_state_refuses_without_release(case, kind, monkeypatch):
    async with case.db.immediate() as conn:
        if kind == "hold":
            await conn.execute(
                insert(t.task_metadata).values(
                    task_id="source-1", key="manual_pause", value="operator hold"
                )
            )
        elif kind == "gate":
            await conn.execute(
                insert(t.gates).values(
                    id="gate",
                    project_id="p",
                    gate_type="human",
                    status="open",
                    await_id="repair",
                    title="review delivery",
                    created_at=10,
                )
            )
        elif kind == "writer":
            await conn.execute(
                update(t.integration_branch_owners).values(
                    session_id="attached", handoff_state="attached"
                )
            )
        elif kind == "incomplete":
            await conn.execute(
                delete(t.integration_candidate_member_results).where(
                    t.integration_candidate_member_results.c.member_ordinal == 1
                )
            )
        elif kind == "human_required":
            await conn.execute(
                update(t.integration_repair_operations).values(state="human_required")
            )
        elif kind == "completion":
            await conn.execute(update(t.task_completion_records).values(outcome="fail"))
        elif kind == "reservation":
            await conn.execute(
                insert(t.integration_candidate_ref_mutations).values(
                    id="mutation",
                    batch_id="batch",
                    revision=2,
                    operation_id="operation",
                    purpose="candidate_final",
                    repository_id="repo",
                    branch=BRANCH,
                    target_branch=BRANCH,
                    expected_old_sha=case.base,
                    desired_sha=case.candidate,
                    operation_episode_id="batch",
                    operation_stage=1,
                    lease_owner_id="sealer",
                    lease_fence_token=4,
                    branch_owner_id="repair",
                    branch_owner_role="repair",
                    branch_fence_token=3,
                    nonce="nonce",
                    expires_at=500,
                    state="reserved",
                    created_at=10,
                    updated_at=10,
                )
            )
        elif kind == "tree":
            from unittest.mock import AsyncMock

            monkeypatch.setattr(
                case.service.promotion, "_tree_oid", AsyncMock(return_value=case.base)
            )
    result = await preview(case)
    assert result["outcome"] == "blocked", result
    assert (await row(case, t.integration_batches))["lifecycle"] == "testing"
    assert (await row(case, t.project_integration_schedules))["outstanding_request_id"]


async def test_changed_stage_snapshot_refuses_apply(case):
    result = await preview(case)
    assert result["outcome"] == "would_settle", result
    async with case.db.immediate() as conn:
        await conn.execute(update(t.integration_repair_stages).values(attempts=2))
    changed = await case.service.run(apply_request(result), principal="operator:test")
    assert changed["outcome"] == "changed"


async def test_settlement_preserves_coalesced_followup_work(case):
    coalesced = await case.scheduler.mark_due(project_id="p", now=25, trigger="manual")
    assert coalesced["outcome"] == "coalesced"
    result = await preview(case)
    assert result["outcome"] == "would_settle", result
    applied = await case.service.run(apply_request(result), principal="operator:test")
    assert applied["outcome"] == "settled", applied
    schedule = await row(case, t.project_integration_schedules)
    assert schedule["outstanding_request_id"] == "integration-sweep:p:2"
    assert schedule["request_sequence"] == 2
    replay = await case.service.run(apply_request(result), principal="operator:test")
    assert replay["outcome"] == "already_settled"
    assert await row(case, t.project_integration_schedules) == schedule


async def test_expired_current_stage_settlement_preserves_expiry_and_replays(case):
    async with case.db.immediate() as conn:
        await conn.execute(
            update(t.integration_repair_stages)
            .where(t.integration_repair_stages.c.ordinal == 1)
            .values(state="expired", completed_at=25)
        )
    result = await preview(case)
    assert result["outcome"] == "would_settle", result
    request = apply_request(result)
    assert (await case.service.run(request, principal="operator:test"))["outcome"] == "settled"
    replay = await case.service.run(request, principal="operator:test")
    assert replay["outcome"] == "already_settled"
    assert replay["repair_stage_state"] == "expired"


def test_apply_requires_all_preview_fences_and_a_reason():
    with pytest.raises(ValueError, match="apply requires"):
        IntegrationSettleDeliveredBatchArgs(batch_id="batch", dry_run=False)


async def test_archived_historical_delegate_settles_with_preserved_provenance(case):
    assert await case.db.archive_task("old-repair", obsolete_integration_delegate=True)
    result = await preview(case)
    assert result["outcome"] == "would_settle", result
    applied = await case.service.run(apply_request(result), principal="operator:test")
    assert applied["outcome"] == "settled", applied
    assert (await row(case, t.archived_tasks))["id"] == "old-repair"
    assert (await row(case, t.integration_batches))["tested_candidate_sha"] is None


@pytest.mark.parametrize("field,value", [("created_by_id", "other-operation"),
                                        ("status", "IN_PROGRESS"),
                                        ("branch_name", "other-branch")])
async def test_archived_delegate_still_requires_terminal_matching_provenance(case, field, value):
    assert await case.db.archive_task("old-repair", obsolete_integration_delegate=True)
    async with case.db.immediate() as conn:
        await conn.execute(update(t.archived_tasks).values(**{field: value}))
    result = await preview(case)
    assert result["outcome"] == "blocked", result
    assert (await row(case, t.integration_batches))["lifecycle"] == "testing"


async def test_archived_delegate_change_invalidates_preview(case):
    assert await case.db.archive_task("old-repair", obsolete_integration_delegate=True)
    result = await preview(case)
    assert result["outcome"] == "would_settle", result
    async with case.db.immediate() as conn:
        await conn.execute(update(t.archived_tasks).values(updated_at=999))
    applied = await case.service.run(apply_request(result), principal="operator:test")
    assert applied["outcome"] == "changed", applied
