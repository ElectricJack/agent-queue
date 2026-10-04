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
from src.doctor.integration_checks import _find_unadmitted_parents, run_check
from src.doctor.models import Severity
from src.doctor.stall_checks import _unadmitted_parent_findings
from src.git.manager import GitManager
from src.integration.cancelled_collection_recovery import CancelledCollectionRecovery
from src.integration.github_review_poll import GitHubReviewPoller
from src.integration.models import BranchKey, Fence, PromotionInput
from src.integration.ownership import BranchOwnership
from src.integration.parent_completion import ParentCompletion
from src.integration.parent_source import (
    ESCALATION_SOURCE_KIND,
    ParentHeadObservation,
    ParentSourceReverification,
)
from src.integration.promotion import PromotionService
from src.integration.review_evidence import ReviewEvidenceProducer
from src.integration.scheduler import TrainService
from src.models import Project
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
        self.completion = ParentCompletion(database)
        self.escalation_events = []

    def _hierarchy_integration_service(self):
        return self.completion

    def _integration_promotion_service(self):
        return self.promotion

    async def _integration_delivery_authorized(self, *args):
        return True

    async def _emit_escalation(self, event_type, payload):
        self.escalation_events.append((event_type, payload))


async def _finish(case, *, hook=True):
    database, completion = case.db, case.commands.completion
    checkpoint = await database.get_integration_checkpoint("parent")
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
                        t.integration_branch_owners.c.ref == "aq/parent",
                    )
                )
            )
            .mappings()
            .one()
        )
        fence = await BranchOwnership(database).transfer_detached_on(
            conn,
            Fence(
                target=BranchKey(repository_id="repo", branch="aq/parent"),
                owner_id=owner["owner_id"],
                token=owner["fence_token"],
            ),
            operation["verifier_task_id"],
            "verifier",
        )
    assert (await completion.wake_verifier("parent", fence))["outcome"] == "woken"
    checkpoint = await database.get_integration_checkpoint("parent")
    generation = checkpoint["generation"]
    await _trusted_check(
        database,
        {"operation_id": operation["id"]},
        head_sha=checkpoint["checkpoint_sha"],
        generation=generation,
        id=f"check-{generation}",
        run_id=f"run-{generation}",
    )
    verified = await completion.verify_parent(
        "parent",
        generation,
        checkpoint["checkpoint_sha"],
        [f"check-{generation}"],
    )
    assert verified["outcome"] == "verified"
    args = {"task_id": "parent", "generation": generation, "head_sha": checkpoint["checkpoint_sha"]}
    if hook:
        assert (await case.commands._cmd_integration_complete_parent(args))["success"]
    else:
        assert (await completion.complete_parent("parent", generation, args["head_sha"]))[
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
async def completed(db, tmp_path):
    origin = Origin(tmp_path)
    base = git(origin.clone, "rev-parse", "origin/main")
    _, checkpointed, children = await _parent_tree(db, children=2, base_sha=base)
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
    owner = await BranchOwnership(db).get_owner(BranchKey(repository_id="repo", branch="aq/parent"))
    fence = Fence(
        target=BranchKey(repository_id="repo", branch="aq/parent"),
        owner_id=owner["owner_id"],
        token=owner["fence_token"],
    )
    head = base
    for child in children:
        child_head = origin.work(child)
        child_checkpoint = await db.get_integration_checkpoint(child)
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
                "review_kind": "leaf",
                "generation": child_checkpoint["generation"],
                "verdict": "approved",
                "evidence": {"checks": ["unit"]},
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
        # Both real promotions reached the published aggregate; readiness
        # files the initial verifier and durable handoff.
        ready = await ParentCompletion(db).mark_ready_on(conn, "parent", require_verifier=True)
        assert ready["outcome"] == "ready"
    case = SimpleNamespace(
        db=db,
        origin=origin,
        base=base,
        head=head,
        commands=_Commands(db, promotion),
        promotion=promotion,
        producer=ReviewEvidenceProducer(db, promotion),
        children=children,
    )
    await _finish(case, hook=False)
    return case


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
            assert {row["receipt_id"] for row in accepted} == {row["id"] for row in receipts}
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
            len((await conn.execute(select(t.integration_parent_operation_completions))).all()) == 3
        )


@pytest.mark.parametrize(
    ("blocker", "code"),
    [
        ("hold", "waiting"),
        ("rejected", "rejected"),
        ("owner", "waiting"),
        ("rewritten", "unproven"),
        ("stale", "stale"),
        ("delivered", "delivered"),
        ("gate", "waiting"),
        ("policy", "unauthorized"),
    ],
)
async def test_moved_parent_refuses_binding_blockers_without_changing_checkpoint(
    completed, monkeypatch, blocker, code
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
        elif blocker == "policy":
            policy = dict(
                (await conn.execute(select(t.projects.c.hierarchical_integration_policy)))
                .scalar_one()
            )
            policy["root"]["repair"]["source_ci"] = False
            await conn.execute(update(t.projects).values(hierarchical_integration_policy=policy))
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
    if blocker != "rewritten":

        async def unreachable(service, facts):
            raise AssertionError("a refusal Postgres decides costs no remote proof")

        monkeypatch.setattr(CancelledCollectionRecovery, "_prove", unreachable)
    with pytest.raises(HierarchyError) as refused:
        await ParentSourceReverification(case.db, case.promotion).run(
            "parent",
            ParentHeadObservation("parent", source, head, 0),
        )
    assert refused.value.code == code
    assert await case.db.get_integration_checkpoint("parent") == before
    assert (await case.db.get_task("parent")).status.value == "COMPLETED"


def _poller(case, client, calls=None):
    async def reverify(observation):
        if calls is not None:
            calls.append(observation)
        with principal_context(ExecutionPrincipal.service("test-parent-source")):
            return await case.commands.reverify_integration_parent_source(observation)

    return GitHubReviewPoller(
        case.db, case.producer, _ReviewGit(client), parent_head_handler=reverify
    )


async def _incidents(case):
    rows = await case.db.list_escalations(task_id="parent", source_kind=ESCALATION_SOURCE_KIND)
    return {row["incident_key"]: row for row in rows}


def _review(review_id, login, state, commit):
    return {
        "id": review_id,
        "state": state,
        "user": {"type": "User", "login": login},
        "commit_id": commit,
    }


async def test_rejected_parent_head_escalates_until_its_reviewer_approves_the_fix(completed):
    case = completed
    source = await _source(case)
    await case.producer.snapshot_from_pull_request(
        "parent", verdict="rejected", reviewer_login="human", reviewed_sha=source["head"]
    )
    fixed = case.origin.work("parent", "fix")
    client = _ParentClient(fixed, [_review(1, "human", "CHANGES_REQUESTED", source["head"])])
    poller = _poller(case, client)
    before = await case.db.get_integration_checkpoint("parent")

    await poller.tick(1031.0)
    assert (await case.db.get_task("parent")).status.value == "COMPLETED"
    assert "reviews" not in client.calls
    [(key, incident)] = (await _incidents(case)).items()
    assert key == f"parent-head:parent:{fixed}:rejected"
    assert incident["state"] == "needs_human"
    assert incident["supervisor_owner"] == "supervisor-p"
    assert f"approve PR head {fixed}" in incident["decision_requested"]
    assert "github:human rejected" in incident["investigation"]
    assert [event for event, _ in case.commands.escalation_events] == ["escalation.created.v1"]

    # Another reviewer's approval does not outvote a standing request for changes.
    client.reviews.append(_review(2, "other", "APPROVED", fixed))
    client.updated_at = "2026-10-02T00:01:00Z"
    await poller.tick(1062.0)
    assert client.calls.count("reviews") == 1
    assert await case.db.get_integration_checkpoint("parent") == before
    assert list(await _incidents(case)) == [key]
    assert len(case.commands.escalation_events) == 1

    # The rejecting reviewer approves exactly the fixed head: reverification starts.
    client.reviews.append(_review(3, "human", "APPROVED", fixed))
    client.updated_at = "2026-10-02T00:02:00Z"
    await poller.tick(1093.0)
    assert (await case.db.get_task("parent")).status.value == "PAUSED"
    checkpoint = await case.db.get_integration_checkpoint("parent")
    assert checkpoint["checkpoint_sha"] == fixed
    assert checkpoint["generation"] == source["generation"] + 1
    resolved = (await _incidents(case))[key]
    assert resolved["state"] == "resolved"
    assert fixed in resolved["terminal_outcome"]
    assert case.commands.escalation_events[-1][0] == "escalation.updated.v1"
    async with case.db._engine.connect() as conn:
        payload = await conn.scalar(
            select(t.events.c.payload).where(
                t.events.c.event_type == "integration.parent_source_advanced"
            )
        )
    assert '"superseding_approval": {"review_id": 3, "reviewer_login": "human"}' in payload


async def test_unproven_parent_head_escalates_once_and_is_not_probed_until_it_moves(
    completed, monkeypatch
):
    case = completed
    rewritten = case.origin.work("other", "unrelated")
    git(case.origin.clone, "push", "--force", "origin", rewritten + ":refs/heads/aq/parent")
    proofs = []
    prove = CancelledCollectionRecovery._prove

    async def counting(service, facts):
        proofs.append(facts["expected_tip"])
        return await prove(service, facts)

    monkeypatch.setattr(CancelledCollectionRecovery, "_prove", counting)
    client = _ParentClient(rewritten, [])
    calls = []
    poller = _poller(case, client, calls)
    before = await case.db.get_integration_checkpoint("parent")

    await poller.tick(1031.0)
    await poller.tick(1062.0)
    assert proofs == [rewritten]
    assert len(calls) == 1
    [incident] = (await _incidents(case)).values()
    assert incident["incident_key"] == f"parent-head:parent:{rewritten}:unproven"
    assert "on top of " + (await _source(case))["head"] in incident["decision_requested"]

    again = case.origin.work("other", "again")
    git(case.origin.clone, "push", "--force", "origin", again + ":refs/heads/aq/parent")
    client.head = again
    await poller.tick(1093.0)
    assert proofs == [rewritten, again]
    incidents = await _incidents(case)
    assert incidents[f"parent-head:parent:{rewritten}:unproven"]["state"] == "resolved"
    assert incidents[f"parent-head:parent:{again}:unproven"]["state"] == "needs_human"
    assert await case.db.get_integration_checkpoint("parent") == before
    assert (await case.db.get_task("parent")).status.value == "COMPLETED"


async def test_parent_head_waiting_answer_keeps_the_incident_open(completed, monkeypatch, caplog):
    case = completed
    source = await _source(case)
    await case.producer.snapshot_from_pull_request(
        "parent", verdict="rejected", reviewer_login="human", reviewed_sha=source["head"]
    )
    observation = ParentHeadObservation(
        "parent", source, case.origin.work("parent", "fix"), 0
    )

    async def reverify():
        with principal_context(ExecutionPrincipal.service("test-parent-source")):
            return await case.commands.reverify_integration_parent_source(observation)

    assert (await reverify())["outcome"] == "rejected"
    [key] = await _incidents(case)

    # A busy parent engine answers ``waiting`` without raising; that decides nothing.
    async def busy(self, task_id, observation):
        return {"outcome": "waiting", "reason": "parent engine is busy"}

    monkeypatch.setattr(ParentSourceReverification, "run", busy)
    result = await reverify()
    assert result == {"success": False, "outcome": "waiting", "error": "parent engine is busy"}
    assert (await _incidents(case))[key]["state"] == "needs_human"

    # Bookkeeping that fails is logged; the answer still reaches the poller.
    async def broken(*args, **kwargs):
        raise RuntimeError("escalation store unavailable")

    monkeypatch.setattr(
        import_module("src.integration.parent_source"), "settle_refusal_escalation", broken
    )
    with caplog.at_level("WARNING"):
        assert (await reverify())["outcome"] == "waiting"
    assert "escalation bookkeeping failed" in caplog.text


async def test_doctor_and_stall_name_parent_admission_blockers(completed, monkeypatch):
    case = completed
    ctx = SimpleNamespace(db=case.db, handler=None)

    async def observation(ctx, row):
        return {"pr_open": True, "pr_head": case.head, "pr_canonical": True}

    monkeypatch.setattr(
        import_module("src.doctor.integration_checks"), "_parent_pr_observation", observation
    )
    async with case.db.immediate() as conn:
        await conn.execute(update(t.tasks).where(t.tasks.c.id == "parent").values(updated_at=1.0))
    assert (await _find_unadmitted_parents(ctx))[0]["reason"] == "exact_parent_review_missing"
    result = await run_check(case.db, "integration.unadmitted_parents")
    assert result.severity is Severity.WARN
    assert result.fixable is False
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
    module = import_module("src.doctor.integration_checks")

    async def unavailable(ctx, row):
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "_parent_pr_observation", unavailable)
    monkeypatch.setattr(module, "_PARENT_PR_PROBE_SECONDS", 0.01)
    async with completed.db.immediate() as conn:
        await conn.execute(update(t.tasks).where(t.tasks.c.id == "parent").values(updated_at=1.0))
    findings = await module._find_unadmitted_parents(SimpleNamespace(db=completed.db, handler=None))
    assert findings[0]["reason"] == "exact_parent_review_missing"
    assert findings[0]["pr_open"] is None
