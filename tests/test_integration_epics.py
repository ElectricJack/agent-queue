"""Reduced parent readiness over real Git and ordinary PostgreSQL task inputs."""

# ruff: noqa: F811 - pytest fixtures intentionally share parameter names

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import (
    archived_tasks, integration_outbox, integration_parent_episodes, integration_parent_verifications,
    integration_review_evidence, task_completion_records,
    tasks,
)
from src.integration.epics import (
    EpicGraphReader, EpicPolicy, EpicReadinessEvaluator, HeadChecks,
)
from src.integration.reviews import ReviewRequirements, TreeReviews
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus
from tests.test_integration_git_truth import repository  # noqa: F401 - shared real Git fixture


async def completion(db, task_id, *, close_id=None):
    async with db.immediate() as conn:
        await conn.execute(insert(task_completion_records).values(
            id=close_id or "close-" + task_id, task_id=task_id, outcome="pass", completed_at=1.0,
        ))


@pytest.fixture
async def epic_case(repository, reuse_database):
    repo = repository
    head = await repo.commit("child")
    await repo.retain(head, task="child", completion="close-child")
    await repo.run("push", "origin", "HEAD:refs/heads/aq/epic")
    db = await reuse_database("epics.db")
    await db.create_project(Project(id="p", name="P"))
    await db.create_repo(RepoConfig(id="r", project_id="p", source_type=RepoSourceType.CLONE,
                                    url=str(repo.remote)))
    await db.update_project("p", integration_repository_id="r",
                            hierarchical_integration_mode="train")
    for task_id, parent_id, status, branch in (
        ("epic", None, TaskStatus.IN_PROGRESS, "aq/epic"),
        ("child", "epic", TaskStatus.COMPLETED, "source"),
    ):
        await db.create_task(Task(id=task_id, project_id="p", repo_id="r", title=task_id,
                                  description="", parent_task_id=parent_id, status=status,
                                  branch_name=branch))
    await completion(db, "child")
    policy = EpicPolicy(("Tests",), "trusted-ci")
    policy_reader = AsyncMock(return_value=policy)
    reader = EpicGraphReader(policy_on=policy_reader)
    reviews = TreeReviews(db)

    async def green(repository_id, oid, policy):
        return HeadChecks(repository_id, oid, policy.check_names, policy.check_trust, "green")

    checks = AsyncMock(side_effect=green)
    evaluator = EpicReadinessEvaluator(db, reader, reviews, checks=checks)
    return SimpleNamespace(repo=repo, db=db, reader=reader, evaluator=evaluator,
                           reviews=reviews, policy=policy, policy_reader=policy_reader,
                           checks=checks, head=head)


async def evaluate(case):
    async with case.db._engine.connect() as conn:
        graph = await case.reader.read_on(conn, "epic")
    snapshot = await case.repo.snapshot(target="refs/heads/aq/epic")
    return await case.evaluator.evaluate(graph, snapshot), snapshot


async def test_zero_verifier_rows_and_duplicate_audits_are_not_readiness_inputs(epic_case):
    case = epic_case
    # These incidents used to block on an empty verifier join or duplicate
    # delegate audits. There are no legacy control reads in the reduced path.
    async with case.db._engine.connect() as conn:
        for table in (integration_parent_episodes, integration_parent_verifications):
            assert (await conn.execute(select(table))).first() is None
    async with case.db.immediate() as conn:
        for index in range(2):
            await conn.execute(insert(integration_outbox).values(
                id=f"duplicate-audit-{index}", dedup_key=f"legacy-{index}",
                project_id="p", event_type="integration.repair_delegate_closed",
                payload={"parent_task_id": "epic", "task_id": "child", "stage": 0},
                available_at=0.0, created_at=1.0,
            ))
    readiness, snapshot = await evaluate(case)
    assert readiness.ready and readiness.head_sha == case.head
    assert readiness.sources[0].reason == "ancestor"
    complete = AsyncMock(return_value={"success": True})
    async with case.db.immediate() as conn:
        result = await case.evaluator.complete_on(conn, readiness, snapshot, complete=complete)
    assert result["success"]
    complete.assert_awaited_once_with(conn, "epic", case.head)
    async with case.db._engine.connect() as conn:
        for table in (integration_parent_episodes, integration_parent_verifications,
                      integration_review_evidence):
            assert (await conn.execute(select(table))).first() is None


@pytest.mark.parametrize("mutation", ["new_child", "reopened", "claim", "policy", "retarget",
                                      "source", "hold", "check_rerun", "head"])
async def test_completion_rechecks_graph_sources_policy_checks_and_ref(epic_case, mutation):
    case = epic_case
    readiness, snapshot = await evaluate(case)
    assert readiness.ready
    if mutation == "new_child":
        await case.db.create_task(Task(id="new", project_id="p", repo_id="r", title="new",
                                      description="", parent_task_id="epic", status=TaskStatus.READY))
    elif mutation == "reopened":
        async with case.db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == "child").values(status="READY"))
    elif mutation == "claim":
        async with case.db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == "child").values(claim_epoch=2))
    elif mutation == "policy":
        case.policy_reader.return_value = replace(case.policy, check_names=("Tests", "Lint"))
    elif mutation == "retarget":
        await case.db.update_repo("r", default_branch="release")
    elif mutation == "source":
        async with case.db.immediate() as conn:
            await conn.execute(insert(task_completion_records).values(
                id="new-close", task_id="child", outcome="pass", completed_at=10.0,
            ))
    elif mutation == "hold":
        await case.db.add_task_label("epic", "hold:operator")
    elif mutation == "check_rerun":
        case.checks.side_effect = lambda repository_id, oid, policy: HeadChecks(
            repository_id, oid, policy.check_names, policy.check_trust, "pending")
    elif mutation == "head":
        await case.repo.commit("later")
        await case.repo.run("push", "origin", "HEAD:refs/heads/aq/epic")
    complete = AsyncMock()
    async with case.db.immediate() as conn:
        result = await case.evaluator.complete_on(conn, readiness, snapshot, complete=complete)
    assert result["outcome"] == "changed"
    complete.assert_not_awaited()


@pytest.mark.parametrize("status", ["READY", "IN_PROGRESS", "FAILED", "BLOCKED", "CANCELLED"])
async def test_unsettled_required_child_is_never_implicitly_waived(epic_case, status):
    case = epic_case
    async with case.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "child").values(status=status))
    readiness, _ = await evaluate(case)
    assert readiness.state == "waiting"
    assert ("child", "work_" + status.lower()) in readiness.blockers


@pytest.mark.parametrize("hold", ["label", "manual_pause", "gate", "project"])
async def test_green_head_preserves_explicit_holds(epic_case, hold):
    case = epic_case
    if hold == "label":
        await case.db.add_task_label("epic", "hold:operator")
    elif hold == "manual_pause":
        await case.db.set_task_meta("epic", "manual_pause", {"reason": "operator"})
    elif hold == "gate":
        from src.database.tables import gates, task_gates
        async with case.db.immediate() as conn:
            await conn.execute(insert(gates).values(
                id="human", project_id="p", gate_type="human", title="hold", created_at=1.0,
            ))
            await conn.execute(insert(task_gates).values(task_id="epic", gate_id="human"))
    else:
        await case.db.update_project("p", status="PAUSED")
    readiness, _ = await evaluate(case)
    assert readiness.state == "held" and not readiness.ready


@pytest.mark.parametrize("state", ["red", "pending", "unknown", "wrong_head", "wrong_trust"])
async def test_only_current_exact_trusted_checks_satisfy_readiness(epic_case, state):
    case = epic_case
    async def checks(repository_id, oid, policy):
        return HeadChecks(repository_id, "f" * 40 if state == "wrong_head" else oid,
                          policy.check_names, "other" if state == "wrong_trust" else policy.check_trust,
                          "green" if state.startswith("wrong") else state)
    case.checks.side_effect = checks
    readiness, _ = await evaluate(case)
    assert readiness.state == ("waiting" if state in {"red", "pending"} else "unknown")


async def test_missing_source_stays_unknown_even_when_child_work_is_closed(epic_case):
    case = epic_case
    await completion(case.db, "child", close_id="unretained-close")
    # Latest id wins deterministically when close timestamps tie.
    readiness, _ = await evaluate(case)
    assert readiness.state == "unknown"
    assert ("child", "missing_git_provenance") in readiness.blockers


async def test_authorized_required_tree_review_and_later_rejection_bind_green(epic_case):
    case = epic_case
    requirements = ReviewRequirements(True, frozenset({"github:jack"}))
    case.policy_reader.return_value = replace(case.policy, reviews=requirements)
    readiness, snapshot = await evaluate(case)
    assert readiness.state == "waiting" and readiness.review.state == "missing"
    subject, head = await case.reviews.observe(snapshot, "epic", "refs/heads/aq/epic")
    async with case.db.immediate() as conn:
        await case.reviews.record_on(conn, subject, requirements, reviewer="github:jack",
                                    verdict="approved", decision_id="1", reviewed_head_sha=head,
                                    source_base=case.repo.base, provenance={"review_url": "https://test/1"})
    readiness, snapshot = await evaluate(case)
    assert readiness.ready
    async with case.db.immediate() as conn:
        await case.reviews.record_on(conn, subject, requirements, reviewer="github:jack",
                                    verdict="rejected", decision_id="2", reviewed_head_sha=head,
                                    source_base=case.repo.base, provenance={})
    complete = AsyncMock()
    async with case.db.immediate() as conn:
        result = await case.evaluator.complete_on(conn, readiness, snapshot, complete=complete)
    assert result["outcome"] == "changed"
    complete.assert_not_awaited()
    rejected, _ = await evaluate(case)
    assert rejected.state == "rejected"


async def test_nested_epic_rechecks_descendants_even_after_its_ordinary_close(epic_case):
    case = epic_case
    await case.repo.run("push", "origin", "HEAD:refs/heads/aq/nested")
    await case.repo.retain(case.head, task="nested", completion="close-nested")
    await case.db.create_task(Task(id="nested", project_id="p", repo_id="r", title="nested",
                                  description="", parent_task_id="epic", status=TaskStatus.COMPLETED,
                                  branch_name="aq/nested"))
    await completion(case.db, "nested")
    async with case.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "child").values(parent_task_id="nested"))
    readiness, snapshot = await evaluate(case)
    assert readiness.ready and readiness.nested[0].ready
    await case.db.create_task(Task(id="new", project_id="p", repo_id="r", title="new",
                                  description="", parent_task_id="nested", status=TaskStatus.READY))
    complete = AsyncMock()
    async with case.db.immediate() as conn:
        result = await case.evaluator.complete_on(conn, readiness, snapshot, complete=complete)
    assert result["outcome"] == "changed"
    current, _ = await evaluate(case)
    assert current.state == "waiting" and current.nested[0].state == "waiting"


async def test_collection_freezes_current_missing_source_in_shared_batch_port(epic_case):
    case = epic_case
    await case.repo.run("push", "--force", "origin", f"{case.repo.base}:refs/heads/aq/epic")
    readiness, snapshot = await evaluate(case)
    assert readiness.state == "waiting" and len(readiness.missing_sources) == 1
    submit = AsyncMock(return_value={"success": True, "batch_id": "shared"})
    result = await case.evaluator.collect(readiness, snapshot, submit_batch=submit)
    assert result["batch_id"] == "shared"
    submit.assert_awaited_once_with(project_id="p", repository_id="r", target_ref="refs/heads/aq/epic",
                                    expected_target=case.repo.base,
                                    members=({"task_id": "child", "source_sha": case.head,
                                              "source_base": None},))


async def test_collection_rechecks_sources_before_freezing_batch(epic_case):
    case = epic_case
    await case.repo.run("push", "--force", "origin", f"{case.repo.base}:refs/heads/aq/epic")
    readiness, snapshot = await evaluate(case)
    await case.db.add_task_label("child", "hold:operator")
    submit = AsyncMock()
    result = await case.evaluator.collect(readiness, snapshot, submit_batch=submit)
    assert result["outcome"] == "changed"
    submit.assert_not_awaited()


async def test_green_epic_delivers_via_a_separate_batch_for_the_moved_main(epic_case):
    case = epic_case
    await case.repo.retain(case.head, task="epic", completion="close-epic")
    await completion(case.db, "epic")
    async with case.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "epic").values(status="COMPLETED"))
    await case.repo.run("checkout", "main")
    main = await case.repo.commit("main-moved")
    await case.repo.publish()
    readiness, snapshot = await evaluate(case)
    assert readiness.ready and readiness.head_sha == case.head
    submit = AsyncMock(return_value={"success": True, "outcome": "candidate_needs_checks"})
    result = await case.evaluator.deliver(readiness, snapshot, submit_batch=submit)
    assert result["outcome"] == "candidate_needs_checks"
    submit.assert_awaited_once_with(project_id="p", repository_id="r", target_ref="refs/heads/main",
                                    expected_target=main, members=({"task_id": "epic",
                                                                   "source_sha": case.head,
                                                                   "source_base": None},))
    assert await case.repo.run("ls-remote", "origin", "refs/heads/main") == main + "\trefs/heads/main"


async def test_archived_required_child_uses_ordinary_completion_and_git(epic_case):
    case = epic_case
    # Archive without legacy receipt controls; the reduced reader includes
    # ordinary archived identities and their immutable completion sources.
    from sqlalchemy import delete
    async with case.db.immediate() as conn:
        row = (await conn.execute(select(tasks).where(tasks.c.id == "child"))).mappings().one()
        values = {key: value for key, value in row.items() if key in archived_tasks.c}
        await conn.execute(insert(archived_tasks).values(**values, archived_at=10.0))
        await conn.execute(delete(tasks).where(tasks.c.id == "child"))
    readiness, _ = await evaluate(case)
    assert readiness.ready and readiness.sources[0].request.archived


@pytest.mark.parametrize("waived_child", [False, True])
async def test_empty_or_fully_waived_nested_container_keeps_its_own_tree_review(epic_case,
                                                                          waived_child):
    case = epic_case
    await case.repo.run("push", "origin", "HEAD:refs/heads/source")
    await case.db.set_task_meta("child", "container", True)
    if waived_child:
        await case.db.create_task(Task(id="waived", project_id="p", repo_id="r", title="waived",
                                      description="", parent_task_id="child", status=TaskStatus.READY))
        case.reader.required_on = AsyncMock(side_effect=lambda conn, row: row["id"] != "waived")
    requirements = ReviewRequirements(True, frozenset({"github:jack"}))
    case.policy_reader.side_effect = lambda conn, row, project: replace(
        case.policy, reviews=requirements) if row["id"] == "child" else case.policy
    readiness, snapshot = await evaluate(case)
    assert readiness.state == "waiting" and readiness.nested[0].review.state == "missing"
    from src.integration.reviews import ReviewSubject
    async with case.db.immediate() as conn:
        await case.reviews.record_on(
            conn, ReviewSubject("p", "r", "child", readiness.nested[0].tree_sha), requirements,
            reviewer="github:jack", verdict="approved", decision_id="nested-review",
            reviewed_head_sha=case.head, source_base=case.repo.base, provenance={},
        )
    readiness = await case.evaluator.evaluate(readiness.graph, snapshot)
    assert readiness.ready and readiness.nested[0].ready


async def test_source_base_provenance_proves_external_squash_and_is_rechecked(epic_case):
    case = epic_case
    await case.repo.run("checkout", "-b", "external-epic", case.repo.base)
    await case.repo.commit("unrelated")
    await case.repo.run("merge", "--squash", "source")
    await case.repo.run("commit", "-m", "external squash without trailer")
    await case.repo.run("push", "--force", "origin", "HEAD:refs/heads/aq/epic")
    case.reader.source_base_on = AsyncMock(side_effect=lambda conn, row:
                                          case.repo.base if row["id"] == "child" else None)
    readiness, snapshot = await evaluate(case)
    assert readiness.ready and readiness.sources[0].reason == "whole_source_patch"
    case.reader.source_base_on.return_value = None
    case.reader.source_base_on.side_effect = None
    complete = AsyncMock()
    async with case.db.immediate() as conn:
        assert not (await case.evaluator.complete_on(conn, readiness, snapshot,
                                                   complete=complete))["success"]
    complete.assert_not_awaited()


async def test_collection_rechecks_new_review_rejection_before_submitting(epic_case):
    case = epic_case
    await case.repo.run("push", "--force", "origin", case.repo.base + ":refs/heads/aq/epic")
    requirements = ReviewRequirements(True, frozenset({"github:jack"}))
    case.policy_reader.return_value = replace(case.policy, reviews=requirements)
    readiness, snapshot = await evaluate(case)
    assert readiness.state == "waiting" and readiness.missing_sources
    from src.integration.reviews import ReviewSubject
    async with case.db.immediate() as conn:
        await case.reviews.record_on(
            conn, ReviewSubject("p", "r", "epic", readiness.tree_sha), requirements,
            reviewer="github:jack", verdict="rejected", decision_id="late-rejection",
            reviewed_head_sha=case.repo.base, source_base=case.repo.base, provenance={},
        )
    submit = AsyncMock()
    assert (await case.evaluator.collect(readiness, snapshot,
                                         submit_batch=submit))["outcome"] == "rejected"
    submit.assert_not_awaited()


async def test_readiness_never_overrides_ordinary_completion_owner_rejection(epic_case):
    case = epic_case
    readiness, snapshot = await evaluate(case)
    complete = AsyncMock(return_value={"success": False, "code": "hierarchy.open_children"})
    async with case.db.immediate() as conn:
        result = await case.evaluator.complete_on(conn, readiness, snapshot, complete=complete)
    assert result == {"success": False, "code": "hierarchy.open_children"}
    complete.assert_awaited_once()
