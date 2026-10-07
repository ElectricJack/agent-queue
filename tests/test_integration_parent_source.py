"""Two-child aggregate admission survives an advanced canonical PR head."""

import asyncio
from types import SimpleNamespace
from importlib import import_module

import pytest
from sqlalchemy import insert, select, update

from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import ExecutionPrincipal, principal_context
from src.database import tables as t
from src.database.queries.hierarchy_queries import HierarchyError
from src.doctor.stall_checks import _find_unadmitted_parents
from src.doctor.stall_checks import _unadmitted_parent_findings
from src.git.manager import GitManager
from src.integration.cancelled_collection_recovery import CancelledCollectionRecovery, _ProofFailed
from src.integration.github_review_poll import GitHubReviewPoller
from src.integration.models import BranchKey, Fence, PromotionInput
from src.integration.ownership import BranchOwnership
from src.integration.records import ParentEpisodeRecords
from src.integration.parent_source import ParentHeadObservation, ParentSourceReverification
from src.integration.promotion import PromotionService
from src.integration.review_evidence import ReviewEvidenceProducer
from src.integration.scheduler import TrainService
from src.models import Project, Task, TaskStatus
from tests.test_delivery_consumers import Origin, git
from tests.test_epic_pr_review_evidence import _PollClient, _ReviewGit
from tests.test_integration_parent_completion import (
    _parent_tree,
    _trusted_check,
)


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("parent-source.db")
    await database.create_project(Project(id="p", name="parent source"))
    return database


class _Commands(IntegrationCommandsMixin):
    def __init__(self, database, promotion):
        self.db, self.promotion = database, promotion
        self.completion = ParentEpisodeRecords(database)

    def _hierarchy_integration_service(self):
        return self.completion

    def _integration_repository_git(self):
        return self.promotion

    def _integration_promotion_service(self):
        raise AssertionError("train parent completion must use the retained repository Git port")

    async def _integration_delivery_authorized(self, *args):
        return True


async def _finish(case, *, hook=True, parent_id="parent"):
    database, completion = case.db, case.commands.completion
    checkpoint = await database.get_integration_checkpoint(parent_id)
    branch = checkpoint["branch"]
    async with database.immediate() as conn:
        operation = dict(
            (
                await conn.execute(
                    select(t.integration_repair_operations).where(
                        t.integration_repair_operations.c.episode_id == checkpoint["episode_id"],
                    )
                )
            )
            .mappings()
            .one()
        )
        owner = (
            (
                await conn.execute(
                    select(t.integration_branch_owners).where(
                        t.integration_branch_owners.c.ref == branch,
                    )
                )
            )
            .mappings()
            .one()
        )
        fence = await BranchOwnership(database).transfer_detached_on(
            conn,
            Fence(
                target=BranchKey(repository_id="repo", branch=branch),
                owner_id=owner["owner_id"],
                token=owner["fence_token"],
            ),
            operation["verifier_task_id"],
            "verifier",
        )
    assert (await completion.wake_verifier(parent_id, fence))["outcome"] == "woken"
    checkpoint = await database.get_integration_checkpoint(parent_id)
    generation = checkpoint["generation"]
    check_id = f"check-{generation}" if parent_id == "parent" else f"check-{parent_id}-{generation}"
    await _trusted_check(
        database,
        {"operation_id": operation["id"]},
        head_sha=checkpoint["checkpoint_sha"],
        generation=generation,
        parent_task_id=parent_id,
        id=check_id,
        run_id=f"run-{check_id}",
    )
    verified = await completion.verify_parent(
        parent_id,
        generation,
        checkpoint["checkpoint_sha"],
        [check_id],
    )
    assert verified["outcome"] == "verified"
    args = {"task_id": parent_id, "generation": generation, "head_sha": checkpoint["checkpoint_sha"]}
    if hook:
        assert (await case.commands._cmd_integration_complete_parent(args))["success"]
    else:
        assert (await completion.complete_parent(parent_id, generation, args["head_sha"]))[
            "outcome"
        ] == "completed"
    # Session close normally settles the verifier after completing the parent.
    async with database.immediate() as conn:
        await conn.execute(
            update(t.tasks)
            .where(t.tasks.c.id == operation["verifier_task_id"])
            .values(status="COMPLETED")
        )
    return verified


@pytest.fixture
async def completed(db, tmp_path, request):
    origin = Origin(tmp_path)
    base = git(origin.clone, "rev-parse", "origin/main")
    hierarchy, checkpointed, children = await _parent_tree(db, children=2, base_sha=base)
    git(origin.clone, "push", "origin", base + ":refs/heads/aq/parent")
    async with db.immediate() as conn:
        policy = dict(
            (await conn.execute(select(t.projects.c.hierarchical_integration_policy))).scalar_one()
        )
        policy["root"]["admission"] = "authorized"
        policy["root"]["repair"]["source_ci"] = True
        await conn.execute(
            update(t.projects).values(
                hierarchical_integration_mode="train",
                hierarchical_integration_policy=policy,
                integration_mode="pull_request",
            )
        )
        await conn.execute(
            insert(t.project_integration_schedules).values(
                project_id="p",
                interval_seconds=300,
                next_due_at=1300.0,
                updated_at=1000.0,
            )
        )
        await conn.execute(update(t.repos).values(url=origin.url))
        await conn.execute(
            update(t.tasks)
            .where(t.tasks.c.id == "parent")
            .values(
                status="PAUSED",
                task_type="feature",
                pr_url="https://github.com/o/r/pull/7",
            )
        )
        await conn.execute(
            update(t.integration_branch_owners)
            .where(
                t.integration_branch_owners.c.ref == "aq/parent",
            )
            .values(
                owner_id=checkpointed["operation_id"],
                owner_role="collector",
            )
        )
    promotion = PromotionService(db, data_dir=tmp_path / "retained", git_manager=GitManager())
    case = SimpleNamespace(
        db=db,
        origin=origin,
        base=base,
        commands=_Commands(db, promotion),
        promotion=promotion,
        producer=ReviewEvidenceProducer(db, promotion),
        children=children,
        grandchildren=[],
    )
    if getattr(request, "param", False):
        nested_id = children[0]
        # The flat-tree helper seeds terminal leaves. Turn one into an
        # unfinished container before any of its work is promoted to the root.
        async with db.immediate() as conn:
            await conn.execute(
                update(t.tasks).where(t.tasks.c.id == nested_id).values(status="IN_PROGRESS")
            )
            await conn.execute(
                update(t.task_integration_checkpoints)
                .where(t.task_integration_checkpoints.c.task_id == nested_id)
                .values(checkpoint_sha=base)
            )
        nested_checkpoint = await db.get_integration_checkpoint(nested_id)
        filed = await hierarchy.file_children(
            nested_id, [{"title": "grandchild"}], nested_checkpoint["generation"]
        )
        nested_operation = await hierarchy.checkpoint_parent(nested_id, base, filed["generation"])
        case.grandchildren = [row["task_id"] for row in filed["children"]]
        async with db.immediate() as conn:
            await conn.execute(
                update(t.tasks).where(t.tasks.c.id == nested_id).values(status="PAUSED")
            )
            await conn.execute(
                update(t.tasks).where(t.tasks.c.id.in_(case.grandchildren)).values(status="COMPLETED")
            )
        await _collect_children(case, nested_id, nested_operation, case.grandchildren)
        await _finish(case, parent_id=nested_id, hook=False)
    case.head = await _collect_children(case, "parent", checkpointed, children)
    await _finish(case, hook=False)
    return case


async def _collect_children(case, parent_id, checkpointed, children):
    db, origin, base, promotion = case.db, case.origin, case.base, case.promotion
    checkpoint = await db.get_integration_checkpoint(parent_id)
    branch = checkpoint["branch"]
    git(origin.clone, "push", "origin", base + ":refs/heads/" + branch)
    git(origin.clone, "fetch", "origin")
    owner = await BranchOwnership(db).get_owner(BranchKey(repository_id="repo", branch=branch))
    async with db.immediate() as conn:
        fence = await BranchOwnership(db).transfer_detached_on(
            conn,
            Fence(
                target=BranchKey(repository_id="repo", branch=branch),
                owner_id=owner["owner_id"],
                token=owner["fence_token"],
            ),
            checkpointed["operation_id"],
            "collector",
        )
    head = base
    for child in children:
        child_checkpoint = await db.get_integration_checkpoint(child)
        verification_id = child_checkpoint["current_verification_id"]
        child_head = child_checkpoint["checkpoint_sha"] if verification_id else origin.work(child)
        async with db.immediate() as conn:
            await conn.execute(
                update(t.task_integration_checkpoints)
                .where(
                    t.task_integration_checkpoints.c.task_id == child,
                )
                .values(checkpoint_sha=child_head)
            )
            await conn.execute(
                update(t.task_branch_origins)
                .where(
                    t.task_branch_origins.c.task_id == child,
                )
                .values(materialized=True, materialized_at=1.0)
            )
        await db.append_integration_review_evidence(
            {
                "id": f"review-{child}",
                "source_task_id": child,
                "repository_id": "repo",
                "source_base": base,
                "reviewed_head_sha": child_head,
                "reviewed_tree_sha": git(origin.clone, "rev-parse", child_head + "^{tree}"),
                "reviewer_task_id": None,
                "review_kind": "parent" if verification_id else "leaf",
                "generation": child_checkpoint["generation"],
                "verdict": "approved",
                "evidence": {"checks": ["unit"], "verification_id": verification_id},
                "created_at": 1.0,
            }
        )
        prepared = await promotion.prepare(
            PromotionInput(
                operation_key=checkpointed["operation_id"],
                source_task_id=child,
                source_head=child_head,
                source_base=base,
                expected_target=head,
                fence=fence,
            )
        )
        pushed = await promotion.push(prepared.intent_id, fence)
        assert pushed.receipt_id
        head = prepared.prepared_sha
    async with db.immediate() as conn:
        ready = await ParentEpisodeRecords(db).mark_ready_on(conn, parent_id, require_verifier=True)
        assert ready["outcome"] == "ready", ready
    return head


async def _source(case):
    async with case.db._engine.connect() as conn:
        return await case.producer._pull_request_source_on(conn, "parent")


async def _members(case, *, green=False):
    source = await _source(case)
    async with case.db.immediate() as conn:
        if green:
            await conn.execute(
                insert(t.integration_source_ci).values(
                    task_id="parent",
                    repository_id="repo",
                    source_base=source["base"],
                    source_head=source["head"],
                    generation=source["generation"],
                    policy_generation=0,
                    state="green",
                    evidence={"checks": {"unit": "success"}},
                    observed_at=1.0,
                )
            )
        return await TrainService(case.db)._eligible_members(
            conn,
            project_id="p",
            repository_id="repo",
            project_mode="pull_request",
        )


class _ParentClient(_PollClient):
    async def pull_request(self, url):
        pull = await super().pull_request(url)
        pull["head"]["ref"] = "aq/parent"
        return pull


@pytest.mark.parametrize("completed", [False, True], ids=["flat", "nested"], indirect=True)
async def test_two_child_parent_completes_reviews_and_readmits_after_two_head_advances(completed):
    case = completed
    initial = await _source(case)
    assert await _members(case, green=True) == []
    # The completion command's crash retry also produces exact parent evidence.
    assert (
        await case.commands._cmd_integration_complete_parent(
            {
                "task_id": "parent",
                "generation": initial["generation"],
                "head_sha": initial["head"],
            }
        )
    )["success"]
    assert [member["task_id"] for member in await _members(case)] == ["parent"]
    old_reviews = []
    async with case.db._engine.connect() as conn:
        old_reviews = [
            dict(row)
            for row in (await conn.execute(select(t.integration_review_evidence))).mappings()
        ]
        receipts = [
            dict(row) for row in (await conn.execute(select(t.task_delivery_receipts))).mappings()
        ]
    root_receipts = [row for row in receipts if row["target_task_id"] == "parent"]
    nested_checkpoints = {
        task_id: await case.db.get_integration_checkpoint(task_id)
        for task_id in case.children + case.grandchildren
    }
    if case.grandchildren:
        assert any(
            row["source_task_id"] in case.grandchildren
            and row["target_task_id"] == case.children[0]
            for row in receipts
        )
    client = _ParentClient(case.head, [])

    async def reverify(observation):
        with principal_context(ExecutionPrincipal.service("test-parent-source")):
            return await case.commands.reverify_integration_parent_source(observation)

    poller = GitHubReviewPoller(
        case.db, case.producer, _ReviewGit(client), parent_head_handler=reverify
    )
    for index in (1, 2):
        previous = await _source(case)
        client.head = case.origin.work("parent", f"advance-{index}")
        await poller.tick(1000.0 + index * 31)
        checkpoint = await case.db.get_integration_checkpoint("parent")
        assert (await case.db.get_task("parent")).status.value == "PAUSED"
        assert checkpoint["current_verification_id"] is None
        assert checkpoint["verified_sha"] is None
        assert checkpoint["last_completed_verification_id"] == previous["verification_id"]
        assert checkpoint["generation"] == previous["generation"] + 1
        for task_id, unchanged in nested_checkpoints.items():
            assert await case.db.get_integration_checkpoint(task_id) == unchanged
            assert (await case.db.get_task(task_id)).status is TaskStatus.COMPLETED
        assert await _source(case) is None
        assert await _members(case) == []
        stale_check = await case.commands.completion.verify_parent(
            "parent",
            checkpoint["generation"],
            client.head,
            [f"check-{previous['generation']}"],
        )
        assert stale_check["outcome"] == "invalid_evidence"
        async with case.db._engine.connect() as conn:
            operation = (
                (
                    await conn.execute(
                        select(t.integration_repair_operations).where(
                            t.integration_repair_operations.c.episode_id
                            == checkpoint["episode_id"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            verifier = await conn.scalar(
                select(t.tasks.c.id).where(t.tasks.c.id == operation["verifier_task_id"])
            )
            assert verifier
            accepted = (
                (
                    await conn.execute(
                        select(t.integration_episode_receipt_acceptances).where(
                            t.integration_episode_receipt_acceptances.c.episode_id
                            == checkpoint["episode_id"],
                        )
                    )
                )
                .mappings()
                .all()
            )
            assert {row["receipt_id"] for row in accepted} == {row["id"] for row in root_receipts}
        verified = await _finish(case)
        members = await _members(case, green=True)
        assert [member["task_id"] for member in members] == ["parent"]
        review = members[0]["review"]
        assert review["review_kind"] == "parent"
        assert review["reviewed_head_sha"] == client.head
        assert review["evidence"]["verification_id"] == verified["verification_id"]
        assert review["evidence"]["verification_id"] != previous["verification_id"]
    async with case.db._engine.connect() as conn:
        assert [
            dict(row) for row in (await conn.execute(select(t.task_delivery_receipts))).mappings()
        ] == receipts
        all_reviews = [
            dict(row)
            for row in (await conn.execute(select(t.integration_review_evidence))).mappings()
        ]
        assert all(review in all_reviews for review in old_reviews)
        assert (
            len((await conn.execute(select(t.integration_parent_operation_completions))).all())
            == 3 + bool(case.grandchildren)
        )


@pytest.mark.parametrize("completed", [True], ids=["nested"], indirect=True)
async def test_delivered_nested_parent_cannot_rollover_outside_its_subtree(completed):
    case = completed
    nested_id = case.children[0]
    before = await case.db.get_integration_checkpoint(nested_id)
    with pytest.raises(HierarchyError) as refused:
        await case.db.transition_task(nested_id, TaskStatus.PAUSED, force=True)
    assert refused.value.code == "delivery_target_fixed"
    assert await case.db.get_integration_checkpoint(nested_id) == before
    assert (await case.db.get_task(nested_id)).status is TaskStatus.COMPLETED


@pytest.mark.parametrize("target", [None, "outside"])
@pytest.mark.parametrize("completed", [True], ids=["nested"], indirect=True)
async def test_nested_parent_reverification_refuses_receipts_leaving_subtree(completed, target):
    case = completed
    source = await _source(case)
    head = case.origin.work("parent", "advance")
    if target:
        await case.db.create_task(
            Task(id=target, project_id="p", title="Other parent", description="")
        )
    async with case.db.immediate() as conn:
        await conn.execute(
            insert(t.task_delivery_receipts).values(
                id="external-receipt",
                domain_key="external-receipt",
                source_task_id=case.grandchildren[0],
                target_task_id=target,
                repository_id="repo",
                target_branch="aq/outside" if target else "main",
                disposition="code",
                created_at=1.0,
            )
        )
    before = await case.db.get_integration_checkpoint("parent")
    with pytest.raises(HierarchyError) as refused:
        await ParentSourceReverification(case.db, case.promotion).run(
            "parent", ParentHeadObservation("parent", source, head, 0)
        )
    assert refused.value.code == "delivery_target_fixed"
    assert await case.db.get_integration_checkpoint("parent") == before
    assert (await case.db.get_task("parent")).status is TaskStatus.COMPLETED


@pytest.mark.parametrize(
    "blocker", ["hold", "rejected", "owner", "rewritten", "stale", "delivered", "gate"]
)
async def test_moved_parent_refuses_binding_blockers_without_changing_checkpoint(
    completed, blocker
):
    case = completed
    source = await _source(case)
    head = case.origin.work("parent", "advance")
    async with case.db.immediate() as conn:
        if blocker == "hold":
            await conn.execute(
                insert(t.task_labels).values(task_id="parent", label="hold:operator")
            )
        elif blocker == "owner":
            await conn.execute(
                update(t.integration_branch_owners)
                .where(
                    t.integration_branch_owners.c.ref == "aq/parent",
                )
                .values(handoff_state="attached", session_id="live")
            )
        elif blocker == "delivered":
            await conn.execute(
                insert(t.task_delivery_receipts).values(
                    id="delivered",
                    domain_key="delivered",
                    source_task_id="parent",
                    target_task_id=None,
                    repository_id="repo",
                    target_branch="main",
                    reviewed_head_sha=source["head"],
                    reviewed_tree_sha="c" * 40,
                    before_sha=case.base,
                    squash_sha=source["head"],
                    after_sha=source["head"],
                    disposition="code",
                    created_at=1.0,
                )
            )
        elif blocker == "gate":
            await conn.execute(
                insert(t.gates).values(
                    id="gate",
                    project_id="p",
                    gate_type="human",
                    title="Operator gate",
                    status="open",
                    created_at=1.0,
                )
            )
            await conn.execute(insert(t.task_gates).values(task_id="parent", gate_id="gate"))
    if blocker == "rejected":
        git(
            case.origin.clone, "push", "--force", "origin", source["head"] + ":refs/heads/aq/parent"
        )
        await case.producer.snapshot_from_pull_request(
            "parent",
            verdict="rejected",
            reviewer_login="human",
            reviewed_sha=source["head"],
        )
        git(case.origin.clone, "push", "origin", head + ":refs/heads/aq/parent")
    if blocker == "rewritten":
        head = case.origin.work("other", "unrelated")
        git(case.origin.clone, "push", "--force", "origin", head + ":refs/heads/aq/parent")
    if blocker == "stale":
        async with case.db.immediate() as conn:
            await conn.execute(update(t.projects).values(hierarchical_integration_generation=1))
    before = await case.db.get_integration_checkpoint("parent")
    with pytest.raises((HierarchyError, _ProofFailed)):
        await ParentSourceReverification(case.db, case.promotion).run(
            "parent",
            ParentHeadObservation("parent", source, head, 0),
        )
    assert await case.db.get_integration_checkpoint("parent") == before
    assert (await case.db.get_task("parent")).status.value == "COMPLETED"


async def test_doctor_and_stall_name_parent_admission_blockers(completed, monkeypatch):
    case = completed
    ctx = SimpleNamespace(db=case.db, handler=None)

    async def observation(ctx, row):
        return {"pr_open": True, "pr_head": case.head, "pr_canonical": True}

    monkeypatch.setattr(
        import_module("src.doctor.stall_checks"), "_parent_pr_observation", observation
    )
    async with case.db.immediate() as conn:
        await conn.execute(update(t.tasks).where(t.tasks.c.id == "parent").values(updated_at=1.0))
    assert (await _find_unadmitted_parents(ctx))[0]["reason"] == "exact_parent_review_missing"
    assert (await _unadmitted_parent_findings(ctx, {"p"}))[0]["kind"] == "unadmitted_parent"
    async with case.db.immediate() as conn:
        await conn.execute(
            update(t.tasks).where(t.tasks.c.id == "parent").values(task_type="chore")
        )
    assert (await _find_unadmitted_parents(ctx))[0]["reason"] == "parent_admission_not_authorized"
    case.head = case.origin.work("parent", "advance")
    assert (await _find_unadmitted_parents(ctx))[0]["reason"] == "parent_head_moved"


async def test_completed_parent_stays_admissible_when_all_children_archive(completed):
    case = completed
    source = await _source(case)
    assert await case.producer.snapshot_authorized(
        "parent",
        reviewed_sha=source["head"],
        policy_generation=0,
    )
    assert [member["task_id"] for member in await _members(case, green=True)] == ["parent"]
    # Seed historical archives. Current archive guards preserve a delivered
    # child's active identity until the aggregate is delivered.
    async with case.db.immediate() as conn:
        for child in case.children:
            task = await case.db._get_task_conn(child, conn=conn)
            await case.db._archive_one(task, conn=conn)
    assert (await _source(case))["review_kind"] == "parent"
    members = await _members(case)
    assert [member["task_id"] for member in members] == ["parent"]
    assert members[0]["review"]["evidence"]["verification_id"] == source["verification_id"]


@pytest.mark.parametrize("changed", ["hold", "fence"])
async def test_parent_source_rechecks_controls_after_remote_proof(completed, monkeypatch, changed):
    case = completed
    source = await _source(case)
    head = case.origin.work("parent", "advance")
    before = await case.db.get_integration_checkpoint("parent")
    prove = CancelledCollectionRecovery._prove

    async def change_after_proof(service, facts):
        result = await prove(service, facts)
        async with case.db.immediate() as conn:
            if changed == "hold":
                await conn.execute(
                    insert(t.task_labels).values(task_id="parent", label="hold:operator")
                )
            else:
                await conn.execute(
                    update(t.integration_branch_owners)
                    .where(
                        t.integration_branch_owners.c.ref == "aq/parent",
                    )
                    .values(fence_token=t.integration_branch_owners.c.fence_token + 1)
                )
        return result

    monkeypatch.setattr(CancelledCollectionRecovery, "_prove", change_after_proof)
    with pytest.raises(HierarchyError):
        await ParentSourceReverification(case.db, case.promotion).run(
            "parent",
            ParentHeadObservation("parent", source, head, 0),
        )
    assert await case.db.get_integration_checkpoint("parent") == before
    assert (await case.db.get_task("parent")).status.value == "COMPLETED"


async def test_parent_source_command_requires_server_principal():
    commands = _Commands(None, None)
    result = await commands.reverify_integration_parent_source(
        ParentHeadObservation("parent", {"review_kind": "parent"}, "b" * 40, 0),
    )
    assert result["success"] is False
    assert result["outcome"] == "unauthorized"


async def test_doctor_keeps_local_parent_findings_when_github_cannot_answer(completed, monkeypatch):
    module = import_module("src.doctor.stall_checks")

    async def unavailable(ctx, row):
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "_parent_pr_observation", unavailable)
    monkeypatch.setattr(module, "_PARENT_PR_PROBE_SECONDS", 0.01)
    async with completed.db.immediate() as conn:
        await conn.execute(update(t.tasks).where(t.tasks.c.id == "parent").values(updated_at=1.0))
    findings = await module._find_unadmitted_parents(SimpleNamespace(db=completed.db, handler=None))
    assert findings[0]["reason"] == "exact_parent_review_missing"
    assert findings[0]["pr_open"] is None
