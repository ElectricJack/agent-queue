"""Reduced parent readiness over real Git and ordinary PostgreSQL task inputs."""

# ruff: noqa: F811 - pytest fixtures intentionally share parameter names

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import (
    archived_tasks, integration_outbox, integration_parent_episodes, integration_parent_verifications,
    integration_review_evidence, playbook_activations, playbook_artifacts, task_completion_records,
    task_dependencies, tasks,
)
from src.integration.epics import (
    EpicGraphReader, EpicPolicy, EpicReadinessEvaluator, HeadChecks, playbook_epic_checks_on,
)
from src.integration.reviews import ReviewRequirements, TreeReviews
from src.playbooks.definition import load_definition_json
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus
from tests.test_integration_git_truth import repository  # noqa: F401 - shared real Git fixture


async def completion(db, task_id, *, close_id=None, completed_at=1.0):
    async with db.immediate() as conn:
        await conn.execute(insert(task_completion_records).values(
            id=close_id or "close-" + task_id, task_id=task_id, outcome="pass", completed_at=completed_at,
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
    policy = EpicPolicy(("Tests",), "trusted-ci", checks_required=True)
    policy_reader = AsyncMock(return_value=policy)
    reader = EpicGraphReader(db, policy_on=policy_reader)
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


@pytest.mark.parametrize("metadata", [
    {"integration_rework_at": 2.0},
    {"development_completion_id": "close-child-next"},
])
async def test_aggregate_cannot_reuse_a_childs_previous_close(epic_case, metadata):
    case = epic_case
    for key, value in metadata.items():
        await case.db.set_task_meta("child", key, value)
    readiness, _ = await evaluate(case)
    child = readiness.graph.node("child").request
    assert child.completion_id != "close-child"
    assert child.completed_at is None
    assert not readiness.ready
    assert ("child", "missing_git_provenance") in readiness.blockers
    # A fresh exact generation can satisfy the aggregate without altering the
    # already contained source or weakening the reopened generation fence.
    async with case.db.immediate() as conn:
        await conn.execute(insert(task_completion_records).values(
            id="close-child-next", task_id="child", outcome="pass", completed_at=3.0,
        ))
    await case.repo.retain(case.head, task="child", completion="close-child-next")
    fresh, _ = await evaluate(case)
    assert fresh.ready
    assert fresh.graph.node("child").request.completion_id == "close-child-next"


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


@pytest.mark.parametrize("disposition", ["obsolete", "retire", None])
async def test_archive_decision_preserves_history_and_only_abandons_explicit_subtree(
    epic_case, disposition
):
    case = epic_case
    for task_id, parent in (("retired", "epic"), ("retired-child", "retired")):
        await case.db.create_task(Task(
            id=task_id, project_id="p", repo_id="r", title=task_id, description="",
            parent_task_id=parent, status=TaskStatus.PAUSED,
        ))
        await case.db.add_task_label(task_id, "hold:historical")
    before, observed = await evaluate(case)
    assert not before.ready
    assert await case.db.archive_task(
        "retired", archive_reason="explicit no-drop retirement",
        abandon_undelivered=disposition is not None,
        abandon_reason="explicit no-drop retirement", disposition=disposition,
    )
    readiness, _ = await evaluate(case)
    graph = readiness.graph
    for task_id in ("retired", "retired-child"):
        history = await case.db.get_archived_task(task_id)
        assert history["status"] == "PAUSED"
        assert graph.node(task_id).request.archived
        assert graph.node(task_id).required is (disposition is None)
        assert await case.db.get_task_completion(task_id) is None
    assert readiness.ready is (disposition is not None)
    expected = ("child",) if disposition else ("child", "retired")
    assert tuple(node.task_id for node in graph.children("epic")) == expected
    # A decision that changes the graph invalidates an earlier evaluation.
    async with case.db.immediate() as conn:
        assert not await case.evaluator.current_on(conn, before, observed)
    if disposition:
        # The surviving parent's hold remains authoritative even after waiver.
        await case.db.add_task_label("epic", "hold:operator")
        held, _ = await evaluate(case)
        assert held.state == "held" and ("epic", "hold:operator") in held.blockers


@pytest.mark.parametrize("payload", ['{"disposition":"obsolete"}', 'invalid',
                                      '{"disposition":"deliver"}'])
async def test_abandonment_event_cannot_waive_active_work(epic_case, payload):
    case = epic_case
    await case.db.log_event("task.delivery_abandoned", project_id="p", task_id="child",
                            payload=payload)
    async with case.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "child").values(status="PAUSED"))
    readiness, _ = await evaluate(case)
    assert readiness.graph.node("child").required
    assert ("child", "work_paused") in readiness.blockers


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


async def test_patch_equivalent_squash_does_not_complete_an_epic(epic_case):
    case = epic_case
    await case.repo.run("checkout", "-b", "external-epic", case.repo.base)
    await case.repo.commit("unrelated")
    await case.repo.run("merge", "--squash", "source")
    await case.repo.run("commit", "-m", "external squash without trailer")
    await case.repo.run("push", "--force", "origin", "HEAD:refs/heads/aq/epic")
    case.reader.source_base_on = AsyncMock(side_effect=lambda conn, row:
                                          case.repo.base if row["id"] == "child" else None)
    readiness, snapshot = await evaluate(case)
    assert readiness.state == "waiting"
    assert readiness.sources[0].reason == "source_not_contained"
    complete = AsyncMock()
    async with case.db.immediate() as conn:
        assert not (await case.evaluator.complete_on(conn, readiness, snapshot,
                                                   complete=complete))["success"]
    complete.assert_not_awaited()


@pytest.mark.parametrize("mutation", ["hold", "reopened", "generation", "dependency"])
async def test_transitive_authorization_closure_is_frozen(epic_case, mutation):
    case = epic_case
    for task_id in ("upstream", "transitive"):
        await case.db.create_task(Task(id=task_id, project_id="p", repo_id="r", title=task_id,
            description="", status=TaskStatus.COMPLETED, branch_name="source"))
        await completion(case.db, task_id)
        await case.repo.retain(case.head, task=task_id, completion="close-" + task_id)
    async with case.db.immediate() as conn:
        await conn.execute(insert(task_dependencies), [
            {"task_id": "child", "depends_on_task_id": "upstream", "dep_type": "blocks"},
            {"task_id": "upstream", "depends_on_task_id": "transitive", "dep_type": "waits-for"},
        ])
    readiness, snapshot = await evaluate(case)
    assert readiness.ready
    assert readiness.graph.authorization("epic") == ("transitive", "upstream")
    if mutation == "hold":
        await case.db.add_task_label("transitive", "hold:operator")
    elif mutation == "reopened":
        async with case.db.immediate() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == "transitive").values(status="READY"))
    elif mutation == "generation":
        await completion(case.db, "transitive", close_id="new-close", completed_at=10.0)
    else:
        async with case.db.immediate() as conn:
            await conn.execute(task_dependencies.delete().where(task_dependencies.c.task_id == "upstream"))
    complete = AsyncMock()
    async with case.db.immediate() as conn:
        result = await case.evaluator.complete_on(conn, readiness, snapshot, complete=complete)
    assert result["outcome"] == "changed"
    complete.assert_not_awaited()


async def test_completed_child_requires_passing_outcome_or_authorized_code_free(epic_case):
    case = epic_case
    async with case.db.immediate() as conn:
        await conn.execute(update(task_completion_records).values(outcome="fail"))
    readiness, _ = await evaluate(case)
    assert not readiness.ready
    assert ("child", "completion_not_passing") in readiness.blockers
    async with case.db.immediate() as conn:
        await conn.execute(update(task_completion_records).values(outcome="pass"))
    # A provenance record explicitly stating no artifact is authorized code-free
    # evidence; a missing branch by itself never qualifies.
    await completion(case.db, "child", close_id="code-free", completed_at=10.0)
    await case.repo.retain(case.head, task="child", completion="code-free", artifact=False)
    readiness, _ = await evaluate(case)
    assert readiness.ready and readiness.sources[0].reason == "git_no_artifact"


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


async def test_external_prerequisite_membership_is_an_authorization_input(epic_case):
    case = epic_case
    await case.db.create_task(Task(id="upstream", project_id="p", repo_id="r", title="upstream",
        description="", status=TaskStatus.COMPLETED, branch_name="aq/upstream"))
    await completion(case.db, "upstream")
    await case.repo.retain(case.head, task="upstream", completion="close-upstream")
    await case.repo.run("push", "origin", "HEAD:refs/heads/aq/upstream")
    async with case.db.immediate() as conn:
        await conn.execute(insert(task_dependencies).values(task_id="child",
            depends_on_task_id="upstream", dep_type="blocks"))
    readiness, snapshot = await evaluate(case)
    assert readiness.ready
    await case.db.create_task(Task(id="upstream-new", project_id="p", repo_id="r", title="new",
        description="", status=TaskStatus.READY, parent_task_id="upstream"))
    complete = AsyncMock()
    async with case.db.immediate() as conn:
        assert (await case.evaluator.complete_on(conn, readiness, snapshot,
                                                complete=complete))["outcome"] == "changed"
    current, _ = await evaluate(case)
    assert current.state == "waiting"
    assert ("upstream", "prerequisite_waiting") in current.blockers


async def test_external_epic_head_is_rechecked_before_completion(epic_case):
    case = epic_case
    for task_id, parent in (("upstream", None), ("upstream-child", "upstream")):
        await case.db.create_task(Task(id=task_id, project_id="p", repo_id="r", title=task_id,
            description="", status=TaskStatus.COMPLETED, parent_task_id=parent,
            branch_name="aq/upstream" if parent is None else "source"))
        await completion(case.db, task_id)
        await case.repo.retain(case.head, task=task_id, completion="close-" + task_id)
    await case.repo.run("push", "origin", "HEAD:refs/heads/aq/upstream")
    async with case.db.immediate() as conn:
        await conn.execute(insert(task_dependencies).values(task_id="child",
            depends_on_task_id="upstream", dep_type="blocks"))
    readiness, snapshot = await evaluate(case)
    assert readiness.ready and readiness.nested[0].task_id == "upstream"
    await case.repo.commit("upstream-later")
    await case.repo.run("push", "origin", "HEAD:refs/heads/aq/upstream")
    complete = AsyncMock()
    async with case.db.immediate() as conn:
        assert (await case.evaluator.complete_on(conn, readiness, snapshot,
                                                complete=complete))["outcome"] == "changed"
    complete.assert_not_awaited()


PARENT_PLAYBOOK = Path(__file__).parent / "fixtures/playbooks/historical-v2/parent-integration"


async def require_epic_checks(db, tmp_path, *, names=("Tests",), required=None, project_id="p",
                              playbook_id="project-parent-integration"):
    """Activate a project playbook whose parent-check data requires *names*.

    *required* is a whole ``RequiredCheckSet`` mapping, such as a project's
    stored parent checks, made required by playbook data.
    """
    artifact = json.loads((PARENT_PLAYBOOK / "artifact.json").read_text())
    artifact["integration_policy"]["tables"]["parent_episode"]["required_checks"] = required or {
        "version": "v1", "names": list(names), "producer_id": "trusted-ci"}
    data = json.dumps(artifact, sort_keys=True).encode()
    sha = f"sha256:{hashlib.sha256(data).hexdigest()}"
    path = tmp_path / f"{sha[7:]}.json"
    path.write_bytes(data)
    async with db.immediate() as conn:
        await conn.execute(insert(playbook_artifacts).values(
            artifact_sha256=sha, playbook_id=playbook_id, scope="project",
            scope_identifier=project_id, source_digest="sha256:" + "0" * 64,
            contract_fingerprint="test", compiler_build="test", path=str(path), created_at=1.0))
        await conn.execute(insert(playbook_activations).values(
            activation_id="activation-" + playbook_id, playbook_id=playbook_id, scope="project",
            scope_identifier=project_id, active_artifact_sha256=sha, enabled=True,
            health="ready", updated_at=1.0))
    return path


def playbook_policy(case):
    async def policy_on(conn, row, project):
        explicit = await playbook_epic_checks_on(conn, project["id"])
        if explicit is None:
            return case.policy  # the stored set, advisory
        return replace(case.policy, check_names=explicit.names, check_version=explicit.version,
                       check_trust=explicit.producer_id, checks_required=True)
    return policy_on


@pytest.mark.parametrize("state", ["red", "pending", "unknown", "unavailable"])
async def test_advisory_epic_checks_are_shown_but_never_hold_readiness(epic_case, state):
    case = epic_case
    case.policy_reader.return_value = replace(case.policy, checks_required=False)

    async def checks(repository_id, oid, policy):
        if state == "unavailable":
            raise RuntimeError("provider unavailable")
        return HeadChecks(repository_id, oid, policy.check_names, policy.check_trust, state)
    case.checks.side_effect = checks
    readiness, _ = await evaluate(case)
    assert readiness.ready, readiness.blockers
    assert readiness.checks.names == ("Tests",)
    assert readiness.checks.state == ("unknown" if state == "unavailable" else state)


async def test_required_epic_check_without_a_producer_is_named(epic_case):
    case = epic_case
    case.policy_reader.return_value = replace(case.policy, check_trust="")
    readiness, _ = await evaluate(case)
    assert readiness.state == "waiting"
    assert ("epic", "checks_producer_missing") in readiness.blockers


async def test_shipped_parent_playbooks_require_no_epic_checks(epic_case):
    from src.integration.runtime_contracts import SubjectKind

    case = epic_case
    for name in ("parent-integration", "agent-queue-parent-integration"):
        artifact = load_definition_json(
            (PARENT_PLAYBOOK.parent / name / "artifact.json").read_text())
        assert artifact.integration_policy.tables[SubjectKind.PARENT_EPISODE].required_checks is None
    async with case.db._engine.connect() as conn:
        assert await playbook_epic_checks_on(conn, "p") is None


async def test_playbook_required_epic_check_holds_and_its_removal_applies_live(epic_case,
                                                                              tmp_path):
    case = epic_case
    case.policy_reader.side_effect = playbook_policy(case)
    case.policy = replace(case.policy, checks_required=False)
    async def red(repository_id, oid, policy):
        return HeadChecks(repository_id, oid, policy.check_names, policy.check_trust, "red")
    case.checks.side_effect = red
    advisory, _ = await evaluate(case)
    assert advisory.ready and advisory.checks.state == "red"

    await require_epic_checks(case.db, tmp_path, names=("Tests", "Lint"))
    required, snapshot = await evaluate(case)
    assert required.state == "waiting"
    assert ("epic", "checks_red") in required.blockers
    assert required.checks.names == ("Tests", "Lint")

    # The operator removes the requirement: the pending epic is ready on its
    # next visit, with no drain and no daemon restart.
    async with case.db.immediate() as conn:
        await conn.execute(update(playbook_activations).where(
            playbook_activations.c.scope_identifier == "p").values(enabled=False))
    released, _ = await evaluate(case)
    assert released.ready, released.blockers
    complete = AsyncMock()
    async with case.db.immediate() as conn:
        # A verdict read under the old requirement is never completed on.
        assert (await case.evaluator.complete_on(conn, required, snapshot,
                                                complete=complete))["outcome"] == "changed"
    complete.assert_not_awaited()


async def test_unreadable_or_unready_playbook_adds_no_epic_gate(epic_case, tmp_path):
    case = epic_case
    path = await require_epic_checks(case.db, tmp_path)
    activated = path.read_bytes()
    path.write_text("{}")  # no longer the activated bytes
    async with case.db._engine.connect() as conn:
        assert await playbook_epic_checks_on(conn, "p") is None
    path.write_bytes(activated)
    async with case.db._engine.connect() as conn:
        assert (await playbook_epic_checks_on(conn, "p")).names == ("Tests",)
    async with case.db.immediate() as conn:
        await conn.execute(update(playbook_activations).values(health="stale_contract"))
    async with case.db._engine.connect() as conn:
        assert await playbook_epic_checks_on(conn, "p") is None


async def test_advisory_policy_keeps_the_completion_digest(epic_case):
    from src.integration.train_sources import epic_graph_digest

    case = epic_case
    case.policy_reader.return_value = replace(case.policy, checks_required=False)
    async with case.db._engine.connect() as conn:
        advisory = await case.reader.read_on(conn, "epic")
    case.policy_reader.return_value = case.policy
    async with case.db._engine.connect() as conn:
        required = await case.reader.read_on(conn, "epic")
    digest = epic_graph_digest(advisory)
    assert digest != epic_graph_digest(required)
    # A completion fenced before epic checks became advisory stays current.
    data = asdict(advisory)
    data["dependencies"] = [tuple(edge) for edge in advisory.dependencies]
    for node in data["nodes"]:
        node["policy"].pop("checks_required")
        if node["request"]["task_id"] == "epic":
            node["request"].update(completion_id=None, completed_at=None, reported_source=None,
                                   completion_outcome=None, completion_commits=None,
                                   has_recorded_source=False)
    assert digest == hashlib.sha256(
        json.dumps(data, sort_keys=True, default=sorted).encode()).hexdigest()
