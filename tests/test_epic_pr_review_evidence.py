"""A GitHub pull-request verdict becomes exact train review evidence."""

from __future__ import annotations

import json
import logging
import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, insert, select, update

from src.database import Database
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    integration_parent_episodes,
    integration_parent_operation_completions,
    integration_parent_verifications,
    integration_repair_operations,
    integration_review_evidence,
    integration_source_ci,
    project_integration_schedules,
    projects,
    repos,
    task_branch_origins,
    task_completion_records,
    task_integration_checkpoints,
    task_labels,
    tasks,
)
from src.doctor.stall_checks import _unmaterialized_pr_findings
from src.git.github_contracts import GitHubAccessError
from src.git.manager import GitError, GitManager
from src.integration.delivery_observer import DeliveryObserver
from src.integration.github_review_poll import GitHubReviewPoller
from src.integration.models import HierarchicalIntegrationPolicy
from src.integration.promotion import PromotionService
from src.integration.review_evidence import ReviewEvidenceProducer
from src.integration.root_materialization import RootMaterialization
from src.integration.scheduler import TrainService
from src.integration.settling import settled
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus, TaskType
from tests.db_fixtures import lease_dsn
from tests.test_integration_sealing import _policy


async def _continuous_policy(case):
    policy = _policy()
    policy["root"]["admission"] = "authorized"
    policy["root"]["required_checks"]["producer_id"] = "7"
    policy["root"]["repair"].update(source_ci=True, on_exhausted="continue", conflict_scope="batch")
    await case["db"].update_project("p", hierarchical_integration_policy=policy)
    async with case["db"].immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "e1").values(task_type="feature"))
    return policy


async def test_authorized_admission_is_exact_audited_and_preserves_reviewer_hold(case):
    await _continuous_policy(case)
    evidence = await case["producer"].snapshot_authorized("e1", reviewed_sha=case["first"], policy_generation=0)
    assert evidence["evidence"]["decision_path"] == "authorized_task"
    assert evidence["reviewed_tree_sha"] == case["tree"]
    assert (await case["producer"].snapshot_authorized("e1", reviewed_sha=case["first"], policy_generation=0))["id"] == evidence["id"]
    assert await case["producer"].snapshot_authorized("e1", reviewed_sha=case["first"], policy_generation=1) is None
    async with case["db"].immediate() as conn:
        await conn.execute(insert(task_labels).values(task_id="e1", label="hold:product"))
    assert await case["producer"].snapshot_authorized("e1", reviewed_sha=case["first"], policy_generation=0) is None


async def test_authorization_does_not_override_rejected_human_review(case):
    await _continuous_policy(case)
    await case["producer"].snapshot_from_pull_request(
        "e1", verdict="rejected", reviewer_login="reviewer", reviewed_sha=case["first"])
    assert await case["producer"].snapshot_authorized("e1", reviewed_sha=case["first"], policy_generation=0) is None


async def test_explicit_authorization_admits_chore_without_retagging_parent(case):
    policy = await _continuous_policy(case)
    async with case["db"].immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "e1").values(task_type="chore"))
    assert await case["producer"].snapshot_authorized(
        "e1", reviewed_sha=case["first"], policy_generation=0
    ) is None
    policy["root"]["authorized_task_ids"] = ["e1"]
    await case["db"].update_project("p", hierarchical_integration_policy=policy)
    evidence = await case["producer"].snapshot_authorized(
        "e1", reviewed_sha=case["first"], policy_generation=0
    )
    assert evidence["review_kind"] == "parent"
    assert evidence["evidence"]["verification_id"] == "verification-e1"
    assert (await case["db"].get_task("e1")).task_type.value == "chore"
    async with case["db"].immediate() as conn:
        await conn.execute(insert(task_labels).values(task_id="e1", label="hold:product"))
    assert await case["producer"].snapshot_authorized(
        "e1", reviewed_sha=case["first"], policy_generation=0
    ) is None


@pytest.mark.parametrize("conclusion,state", [("failure", "red"), ("cancelled", "cancelled")])
async def test_source_ci_repair_is_deduplicated_and_replaced_only_after_failure(case, tmp_path, conclusion, state):
    import asyncio
    from unittest.mock import AsyncMock

    from src.commands.handler import CommandHandler
    from src.config import AppConfig, DatabaseConfig, DiscordConfig
    from src.integration.models import HierarchicalIntegrationPolicy
    from src.integration.source_ci import SourceCIObservation, classify_source_checks
    from src.models import Task, TaskStatus, TaskType
    from src.orchestrator import Orchestrator
    from src.vault import ensure_default_intelligence_classes

    policy = HierarchicalIntegrationPolicy.model_validate(await _continuous_policy(case))
    async with case["db"]._engine.connect() as conn:
        source = await case["producer"]._pull_request_source_on(conn, "e1")
    entries = [{"id": 1, "name": policy.root.required_checks.names[0],
                "head_sha": source["head"], "app": {"id": int(policy.root.required_checks.producer_id)},
                "status": "completed", "conclusion": conclusion,
                "html_url": "https://github.com/o/r/actions/runs/1",
                "output": {"summary": "tests/test_source.py::test_delivery failed"}}]
    verdict, checks = classify_source_checks(entries, head=source["head"], required=policy.root.required_checks)
    assert verdict == state
    data = str(tmp_path / "handler-data")
    ensure_default_intelligence_classes(data)
    config = AppConfig(discord=DiscordConfig(bot_token="t", guild_id="1"),
                       database=DatabaseConfig(url=lease_dsn("source-ci.db")), data_dir=data)
    orch = Orchestrator(config)
    orch.db = case["db"]
    handler = CommandHandler(orch, config)
    # Isolate only normal filing; the source command, locks, durable history,
    # authority checks and replay run against PostgreSQL.
    async def ensure(args):
        existing = await case["db"].find_task_by_dedup_key("p", args["dedup_key"])
        if existing:
            return {"success": True, "task_id": existing.id, "created": False}
        task_id = f"source-repair-{len(filing.call_args_list)}"
        await case["db"].create_task(Task(id=task_id, project_id="p", repo_id="repo",
            title=args["title"], description=args["description"], dedup_key=args["dedup_key"],
            task_type=TaskType.BUGFIX, status=TaskStatus.READY))
        return {"success": True, "task_id": task_id, "created": True}
    filing = AsyncMock(side_effect=ensure)
    handler._cmd_ensure_task = filing
    observation = SourceCIObservation("e1", source, 0, state, checks)
    first, replay = await asyncio.gather(*(handler._cmd_observe_integration_source_ci(observation) for _ in range(2)))
    assert first["repair_task_id"] == replay["repair_task_id"]
    assert filing.await_count == 1
    args = filing.call_args.args[0]
    assert args["repo_id"] == "repo" and args["task_type"] == "bugfix" and args["root"] is True
    assert source["head"] in args["description"] and "tests/test_source.py::test_delivery" in args["description"]
    await case["db"].transition_task(first["repair_task_id"], TaskStatus.FAILED, force=True)
    successor = await handler._cmd_observe_integration_source_ci(observation)
    assert successor["repair_task_id"] != first["repair_task_id"]
    async with case["db"]._engine.connect() as conn:
        record = (await conn.execute(select(integration_source_ci))).mappings().one()
    assert record["repair_attempt"] == 2 and record["repair_history"][0]["task_id"] == first["repair_task_id"]
    # A new verified generation is a new source identity even at the same
    # Git head; its assignment must not reuse a predecessor's dedup key.
    async with case["db"].immediate() as conn:
        # Verification evidence is append-only. Create the next completed
        # episode instead of rewriting the first generation's receipts.
        for table, values in (
            (integration_parent_episodes, {
                "id": "episode-e1-next", "generation": 2,
            }),
            (integration_repair_operations, {
                "id": "operation-e1-next", "episode_id": "episode-e1-next",
            }),
            (integration_parent_verifications, {
                "id": "verification-e1-next", "operation_id": "operation-e1-next",
                "episode_id": "episode-e1-next", "generation": 2,
            }),
            (integration_parent_operation_completions, {
                "operation_id": "operation-e1-next", "verification_id": "verification-e1-next",
                "episode_id": "episode-e1-next",
            }),
        ):
            previous = (await conn.execute(select(table))).mappings().one()
            await conn.execute(insert(table).values({**previous, **values}))
        await conn.execute(update(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "e1"
        ).values(
            generation=2, verified_generation=2, episode_id="episode-e1-next",
            current_verification_id="verification-e1-next",
            last_completed_operation_id="operation-e1-next",
            last_completed_verification_id="verification-e1-next",
        ))
    async with case["db"]._engine.connect() as conn:
        next_source = await case["producer"]._pull_request_source_on(conn, "e1")
    assert next_source["generation"] == 2 and next_source["head"] == source["head"]
    changed = await handler._cmd_observe_integration_source_ci(
        SourceCIObservation("e1", next_source, 0, state, checks))
    assert changed["repair_task_id"] not in {first["repair_task_id"], successor["repair_task_id"]}


def _land(case, *, squash: bool):
    """Deliver the fixture's source head to the default branch, for real.

    ``squash`` is the adoption shape: the work reaches main under a different
    commit, so the source head itself is never an ancestor of the target.  A
    plain merge keeps it one, which is the ancestry shape.
    """
    work = case["work"]
    _git("checkout", "-B", "main", "origin/main", cwd=work)
    _git("merge", *("--squash" if squash else "--no-ff", "-m", "deliver e1"),
         "origin/aq/epic/retire-the-publisher", cwd=work)
    if squash:
        _git("commit", "-m", "deliver e1", cwd=work)
    _git("push", "origin", "main", cwd=work)
    return _git("rev-parse", "HEAD", cwd=work)


def _is_ancestor(work, sha, ref) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, ref],
        cwd=work, check=False, capture_output=True,
    ).returncode == 0


async def _retain_completion(case, *, close_id, commits):
    """Record the completion and retain its exact source in git, as a close does."""
    from src.git.manager import GitManager
    from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance

    # This fixture is a verified parent, not an ordinary leaf close. A newer
    # leaf completion must invalidate its earlier parent proof (fair-rapids-53).
    # Retain the exact verified parent identity and explicitly adopt a squash.
    store = GitProvenance(
        GitManager(), str(case["work"]), repository_url=case["remote"]
    )
    source = CompletedSource(
        CompletionIdentity("p", "repo", "e1", "parent:verification-e1"),
        case["first"],
    )
    await store.write_completion(source)
    if not await store.ancestor(case["first"], commits[-1]):
        await store.write_replacement(
            source_oid=commits[-1],
            base_oid=await store.run("merge-base", case["first"], commits[-1]),
            replaces=[source], authority="operator", reason="already deployed",
        )


async def _adopt_equivalent(case, *, close_id, replaced, by):
    """``aq integration adopt --accept-equivalent``: bind *replaced* to *by* in git.

    The operator decision is a replacement record naming the exact immutable
    completion binding, so delivery truth answers about the generation through
    the operator's commit rather than through the source head it never received.
    """
    from src.git.manager import GitManager
    from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance

    store = GitProvenance(GitManager(), str(case["work"]), repository_url=case["remote"])
    original = CompletedSource(CompletionIdentity("p", "repo", "e1", "parent:verification-e1"), replaced)
    await store.write_replacement(
        source_oid=by, base_oid=await store.run("merge-base", replaced, by),
        replaces=[original], authority="operator", reason="already deployed",
    )


async def _source_handler(case, tmp_path):
    """A CommandHandler over the fixture, with filing isolated and counted."""
    from unittest.mock import AsyncMock

    from src.commands.handler import CommandHandler
    from src.config import AppConfig, DatabaseConfig, DiscordConfig
    from src.orchestrator import Orchestrator
    from src.vault import ensure_default_intelligence_classes

    data = str(tmp_path / "handler-data")
    ensure_default_intelligence_classes(data)
    config = AppConfig(discord=DiscordConfig(bot_token="t", guild_id="1"),
                       database=DatabaseConfig(url=lease_dsn("source-delivery.db")),
                       data_dir=data)
    orch = Orchestrator(config)
    orch.db = case["db"]
    handler = CommandHandler(orch, config)
    filed = []

    async def filing(args):
        existing = await case["db"].find_task_by_dedup_key("p", args["dedup_key"])
        if existing:
            return {"success": True, "task_id": existing.id, "created": False}
        task_id = f"source-repair-{len(filed)}"
        filed.append(task_id)
        await case["db"].create_task(Task(
            id=task_id, project_id="p", repo_id="repo", title=args["title"],
            description=args["description"], dedup_key=args["dedup_key"],
            task_type=TaskType.BUGFIX, status=TaskStatus.READY))
        return {"success": True, "task_id": task_id, "created": True}

    counting = AsyncMock(side_effect=filing)
    handler._cmd_ensure_task = counting
    return handler, counting


async def _red_observation(case):
    from src.integration.source_ci import SourceCIObservation, classify_source_checks

    policy = HierarchicalIntegrationPolicy.model_validate(await _continuous_policy(case))
    async with case["db"]._engine.connect() as conn:
        source = await case["producer"]._pull_request_source_on(conn, "e1")
    entries = [{"id": 1, "name": policy.root.required_checks.names[0],
                "head_sha": source["head"], "app": {"id": int(policy.root.required_checks.producer_id)},
                "status": "completed", "conclusion": "failure",
                "html_url": "https://github.com/o/r/actions/runs/1",
                "output": {"summary": "tests/test_source.py::test_delivery failed"}}]
    state, checks = classify_source_checks(
        entries, head=source["head"], required=policy.root.required_checks)
    return source, SourceCIObservation("e1", source, 0, state, checks)


async def _delivered_handler(case, tmp_path, *, squash):
    """A handler whose registered observer can prove e1 delivered, for real.

    The returned observer counts fetches, so a test can assert that the review
    poller's repeated ticks ask git once per source generation and attempt.
    """
    from src.integration.delivery_observer import DeliveryObserver

    await _retain_completion(
        case, close_id="close-e1", commits=[_land(case, squash=squash)])

    class _Counting(DeliveryObserver):
        fetches = 0

        async def observe(self, task_ids, *, max_age=0.0):
            _Counting.fetches += 1
            return await super().observe(task_ids, max_age=max_age)

    observer = _Counting(case["db"], git=GitManager(), data_dir=tmp_path / "observer")
    case["db"].set_delivery_observer(observer)
    handler, filing = await _source_handler(case, tmp_path)
    return handler, filing, observer


async def _observed_proof(case):
    async with case["db"]._engine.connect() as conn:
        record = (await conn.execute(select(integration_source_ci))).mappings().one()
    return record["evidence"].get("source_delivery")


async def _withheld(case, tmp_path):
    """The queued source-CI repairs a claim would withhold, proved right now."""
    from src.integration.source_delivery import delivered_queued_repairs

    return await delivered_queued_repairs(
        case["db"], case["db"]._delivery_observer, project_id="p")


def _publish(case, ref: str, sha: str) -> None:
    """Force *ref* to *sha* on the real origin, retarget or rewind included."""
    _git("push", "--force", "origin", f"{sha}:refs/heads/{ref}", cwd=case["work"])


async def _set_default_branch(case, ref: str) -> None:
    async with case["db"].immediate() as conn:
        await conn.execute(update(repos).where(repos.c.id == "repo").values(default_branch=ref))


async def test_delivered_source_generation_is_not_repaired(case, tmp_path):
    # Ancestry: the source head itself is an ancestor of the default target.
    handler, filing, observer = await _delivered_handler(case, tmp_path, squash=False)
    source, observation = await _red_observation(case)
    result = await handler._cmd_observe_integration_source_ci(observation)
    assert result["outcome"] == "source_delivered", result
    assert result["delivery"]["state"] == "delivered"
    assert result["delivery"]["target_ref"] == "refs/heads/main"
    assert result["delivery"]["source_oid"] and result["delivery"]["target_oid"]
    assert "repair_task_id" not in result
    assert filing.await_count == 0
    async with case["db"]._engine.connect() as conn:
        record = (await conn.execute(select(integration_source_ci))).mappings().one()
    assert record["repair_task_id"] is None and record["repair_attempt"] == 0
    # What was observed is recorded for audit, without displacing the check
    # evidence it sits beside.
    assert record["evidence"]["source_delivery"]["state"] == "delivered"
    assert record["evidence"]["failing_checks"] == observation.evidence["failing_checks"]
    # The next tick asks git again rather than trusting the record: delivery
    # truth is request-scoped and a remembered answer goes stale when the
    # target moves.
    replay = await handler._cmd_observe_integration_source_ci(observation)
    assert replay["outcome"] == "source_delivered" and filing.await_count == 0
    assert replay["delivery"] == result["delivery"]
    assert observer.fetches == 2
    assert source["head"] == case["first"]


async def test_source_adopted_under_other_commits_is_not_repaired(case, tmp_path):
    # Adoption: the work reached main as a squash, so the observed source head
    # is not an ancestor of anything. The proof names the generation's retained
    # source, with explicit replacement evidence binding it to the squash.
    handler, filing, _observer = await _delivered_handler(case, tmp_path, squash=True)
    _source, observation = await _red_observation(case)
    assert not _is_ancestor(case["work"], observation.source["head"], "main"), (
        "adoption needs a source head main never received"
    )
    result = await handler._cmd_observe_integration_source_ci(observation)
    assert result["outcome"] == "source_delivered", result
    assert result["delivery"]["source_oid"] == observation.source["head"]
    assert filing.await_count == 0


async def test_source_covered_by_an_explicit_adoption_is_not_repaired(case, tmp_path):
    # The incident shape: an operator recorded that the generation's work was
    # already deployed under another commit, so the source head is on nothing.
    await _retain_completion(case, close_id="close-e1", commits=[case["first"]])
    await _adopt_equivalent(
        case, close_id="close-e1", replaced=case["first"],
        by=_land(case, squash=True),
    )
    case["db"].set_delivery_observer(DeliveryObserver(
        case["db"], git=GitManager(), data_dir=tmp_path / "observer"))
    handler, filing = await _source_handler(case, tmp_path)
    _source, observation = await _red_observation(case)
    assert not _is_ancestor(case["work"], observation.source["head"], "main")
    result = await handler._cmd_observe_integration_source_ci(observation)
    assert result["outcome"] == "source_delivered", result
    assert result["delivery"]["source_oid"] == case["first"]
    assert filing.await_count == 0


async def test_a_source_git_cannot_place_is_still_repaired(case, tmp_path):
    handler, filing = await _source_handler(case, tmp_path)
    case["db"].set_delivery_observer(DeliveryObserver(
        case["db"], git=GitManager(), data_dir=tmp_path / "observer"))
    _source, observation = await _red_observation(case)
    # A real branch, no completion generation retained: nothing stands in for
    # this generation's final artifact, so delivery stays unknown.
    result = await handler._cmd_observe_integration_source_ci(observation)
    assert result["outcome"] == "repair_created", result
    assert filing.await_count == 1
    assert (await _observed_proof(case))["state"] == "undelivered"


async def test_a_genuinely_undelivered_source_is_still_repaired(case, tmp_path):
    await _retain_completion(case, close_id="close-e1", commits=[case["first"]])
    case["db"].set_delivery_observer(DeliveryObserver(
        case["db"], git=GitManager(), data_dir=tmp_path / "observer"))
    handler, filing = await _source_handler(case, tmp_path)
    _source, observation = await _red_observation(case)
    result = await handler._cmd_observe_integration_source_ci(observation)
    assert result["outcome"] == "repair_created", result
    assert filing.await_count == 1
    assert (await _observed_proof(case))["state"] == "undelivered"
    # An undelivered answer withholds nothing: the repair stays queued.
    assert result["repair_task_id"] not in await _withheld(case, tmp_path)


async def test_unknown_delivery_evidence_still_files_a_repair(case, tmp_path):
    class _Broken:
        async def observe(self, task_ids, *, max_age=0.0):
            raise OSError("no route to host")

    handler, filing = await _source_handler(case, tmp_path)
    case["db"].set_delivery_observer(_Broken())
    _source, observation = await _red_observation(case)
    result = await handler._cmd_observe_integration_source_ci(observation)
    assert result["outcome"] == "repair_created", result
    assert filing.await_count == 1
    assert (await _observed_proof(case))["state"] == "unknown"


async def test_a_source_that_moves_before_its_proof_is_refused(case, tmp_path):
    # The delivery answer is git I/O taken outside the hierarchy lock, so the
    # exact source identity is re-read under it. A generation that moved has no
    # answer, and withholding a repair it may still need would strand work.
    class _Racing(DeliveryObserver):
        async def observe(self, task_ids, *, max_age=0.0):
            view = await super().observe(task_ids, max_age=max_age)
            async with case["db"].immediate() as conn:
                await conn.execute(update(task_integration_checkpoints).where(
                    task_integration_checkpoints.c.task_id == "e1"
                ).values(generation=2, verified_generation=2))
            return view

    await _retain_completion(case, close_id="close-e1", commits=[_land(case, squash=False)])
    case["db"].set_delivery_observer(_Racing(
        case["db"], git=GitManager(), data_dir=tmp_path / "observer"))
    handler, filing = await _source_handler(case, tmp_path)
    _source, observation = await _red_observation(case)
    result = await handler._cmd_observe_integration_source_ci(observation)
    assert result == {
        "success": False, "outcome": "stale",
        "error": "source changed before its delivery proof",
    }, result
    assert filing.await_count == 0
    assert await _observed_proof(case) is None


async def test_an_open_delegate_is_never_ended_by_a_delivery_proof(case, tmp_path):
    handler, filing = await _source_handler(case, tmp_path)
    case["db"].set_delivery_observer(DeliveryObserver(
        case["db"], git=GitManager(), data_dir=tmp_path / "observer"))
    _source, observation = await _red_observation(case)
    first = await handler._cmd_observe_integration_source_ci(observation)
    assert first["outcome"] == "repair_created" and filing.await_count == 1
    repair_id = first["repair_task_id"]
    assert repair_id not in await _withheld(case, tmp_path)
    # The source lands, so the delegate's work is now already delivered. The
    # handler reports both facts and leaves the delegate exactly as it is.
    await _retain_completion(case, close_id="close-e1", commits=[_land(case, squash=False)])
    landed = await handler._cmd_observe_integration_source_ci(observation)
    assert landed["outcome"] == "already_repairing", landed
    assert landed["repair_task_id"] == repair_id
    assert landed["delivery"]["state"] == "delivered"
    assert filing.await_count == 1
    task = await case["db"].get_task(repair_id)
    assert task is not None and task.status.value == "READY"
    # A claim withholds it, from a proof taken now rather than from the record.
    assert repair_id in await _withheld(case, tmp_path)


async def test_a_retargeted_default_target_is_answered_afresh(case, tmp_path):
    # Delivery truth is request-scoped: the same source, the same repair
    # attempt, and a target that moved. A recorded proof must never answer for
    # a branch the project no longer delivers to.
    handler, filing, _observer = await _delivered_handler(case, tmp_path, squash=False)
    _source, observation = await _red_observation(case)
    assert (await handler._cmd_observe_integration_source_ci(observation))["outcome"] \
        == "source_delivered"
    _publish(case, "release", case["base"])
    await _set_default_branch(case, "release")
    retargeted = await handler._cmd_observe_integration_source_ci(observation)
    assert retargeted["outcome"] == "repair_created", retargeted
    assert retargeted["delivery"]["state"] == "undelivered"
    assert retargeted["delivery"]["target_ref"] == "refs/heads/release"
    assert filing.await_count == 1


async def test_a_target_that_loses_containment_repairs_again(case, tmp_path):
    handler, filing, _observer = await _delivered_handler(case, tmp_path, squash=False)
    _source, observation = await _red_observation(case)
    assert (await handler._cmd_observe_integration_source_ci(observation))["outcome"] \
        == "source_delivered"
    # The default branch is rewound past the generation's retained source.
    _publish(case, "main", case["base"])
    rewound = await handler._cmd_observe_integration_source_ci(observation)
    assert rewound["outcome"] == "repair_created", rewound
    assert rewound["delivery"]["state"] == "undelivered"
    assert filing.await_count == 1


async def test_delivery_arriving_after_a_negative_answer_is_answered_freshly(case, tmp_path):
    handler, filing = await _source_handler(case, tmp_path)
    case["db"].set_delivery_observer(DeliveryObserver(
        case["db"], git=GitManager(), data_dir=tmp_path / "observer"))
    _source, observation = await _red_observation(case)
    first = await handler._cmd_observe_integration_source_ci(observation)
    assert first["outcome"] == "repair_created" and filing.await_count == 1
    assert (await _observed_proof(case))["state"] == "undelivered"
    repair_id = first["repair_task_id"]
    assert repair_id not in await _withheld(case, tmp_path)
    # The work reaches the default branch after the negative answer. The next
    # tick asks again instead of re-reading the record, and the claim gate
    # withholds the queued delegate from a proof taken now.
    await _retain_completion(case, close_id="close-e1", commits=[_land(case, squash=False)])
    arrived = await handler._cmd_observe_integration_source_ci(observation)
    assert arrived["outcome"] == "already_repairing", arrived
    assert arrived["delivery"]["state"] == "delivered"
    assert (await _observed_proof(case))["state"] == "delivered"
    assert repair_id in await _withheld(case, tmp_path)
    # ...and withholding is not durable either: rewind the target and the same
    # queued delegate is claimable again.
    _publish(case, "main", case["base"])
    assert repair_id not in await _withheld(case, tmp_path)
    assert (await case["db"].get_task(repair_id)).status.value == "READY"


def test_cancelled_old_check_cannot_supersede_newer_run():
    from src.integration.models import RequiredCheckSet
    from src.integration.source_ci import classify_source_checks
    required = RequiredCheckSet(version="v1", names=("Tests",), producer_id="7")
    def run(number, status, conclusion, **extra):
        return {"id": number, "name": "Tests", "head_sha": "a" * 40,
                "app": {"id": 7}, "status": status, "conclusion": conclusion, **extra}
    for status, conclusion, expected in [("in_progress", None, "pending"), ("completed", "success", "green")]:
        assert classify_source_checks([run(1, "completed", "cancelled"), run(2, status, conclusion)],
                                      head="a" * 40, required=required)[0] == expected
    assert classify_source_checks([run(3, "completed", "success", app={"id": 9})],
                                  head="a" * 40, required=required)[0] == "pending"
    assert classify_source_checks([run(4, "in_progress", "success")],
                                  head="a" * 40, required=required)[0] == "pending"


def test_conflicting_pull_request_without_checks_is_conflict_not_pending():
    from src.integration.models import RequiredCheckSet
    from src.integration.source_ci import classify_source_checks, pull_request_conflicts
    required = RequiredCheckSet(version="v1", names=("Tests",), producer_id="7")
    head = "a" * 40
    dirty = {"mergeable": False, "mergeable_state": "dirty"}
    assert pull_request_conflicts(dirty)
    assert not pull_request_conflicts({"mergeable": None, "mergeable_state": "unknown"})
    assert not pull_request_conflicts({"mergeable": True, "mergeable_state": "clean"})
    assert classify_source_checks([], head=head, required=required)[0] == "pending"
    assert classify_source_checks(
        [], head=head, required=required, conflicting=True)[0] == "conflict"
    # Any run of a required check on the exact head supersedes the conflict.
    def run(status, conclusion):
        return {"id": 1, "name": "Tests", "head_sha": head, "app": {"id": 7},
                "status": status, "conclusion": conclusion}
    for status, conclusion, expected in [
        ("in_progress", None, "pending"), ("completed", "failure", "red"),
        ("completed", "cancelled", "cancelled"), ("completed", "success", "green"),
    ]:
        assert classify_source_checks(
            [run(status, conclusion)], head=head, required=required, conflicting=True,
        )[0] == expected


@pytest.mark.parametrize("conflict_scope", ["batch", "member"])
async def test_conflicting_source_is_admitted_under_either_conflict_scope(
    case, conflict_scope
):
    policy = await _continuous_policy(case)
    if conflict_scope == "member":
        policy["root"]["repair"]["conflict_scope"] = "member"
        await case["db"].update_project("p", hierarchical_integration_policy=policy)
    db = case["db"]
    async with db.immediate() as conn:
        generation = (await conn.execute(select(task_integration_checkpoints.c.generation).where(
            task_integration_checkpoints.c.task_id == "e1"))).scalar_one()
        await conn.execute(insert(integration_source_ci).values(
            task_id="e1", repository_id="repo", source_base=case["base"],
            source_head=case["first"], generation=generation, policy_generation=0,
            state="conflict", evidence={}, observed_at=1000.0))
    assert await case["producer"].snapshot_authorized(
        "e1", reviewed_sha=case["first"], policy_generation=0)
    async with db.immediate() as conn:
        members = await TrainService(db)._eligible_members(
            conn, project_id="p", repository_id="repo", project_mode="pull_request")
    # Member scope must admit too: no source repair is filed for a conflict,
    # so only candidate construction's member conflict repair can recover it.
    assert {item["task_id"] for item in members} == {"e1"}


async def test_conflict_observation_files_no_source_repair(case, tmp_path):
    from unittest.mock import AsyncMock

    from src.commands.handler import CommandHandler
    from src.config import AppConfig, DatabaseConfig, DiscordConfig
    from src.integration.source_ci import SourceCIObservation
    from src.orchestrator import Orchestrator
    from src.vault import ensure_default_intelligence_classes

    await _continuous_policy(case)
    data = str(tmp_path / "handler-data")
    ensure_default_intelligence_classes(data)
    config = AppConfig(discord=DiscordConfig(bot_token="t", guild_id="1"),
                       database=DatabaseConfig(url=lease_dsn("source-ci.db")), data_dir=data)
    orch = Orchestrator(config)
    orch.db = case["db"]
    handler = CommandHandler(orch, config)
    handler._cmd_ensure_task = AsyncMock()
    async with case["db"]._engine.connect() as conn:
        source = await case["producer"]._pull_request_source_on(conn, "e1")
    assert await case["producer"].snapshot_authorized(
        "e1", reviewed_sha=source["head"], policy_generation=0)
    result = await handler._cmd_observe_integration_source_ci(
        SourceCIObservation("e1", source, 0, "conflict", {"checks": [], "failing_checks": []}))
    assert result == {"success": True, "outcome": "observed", "state": "conflict"}
    async with case["db"].immediate() as conn:
        record = (await conn.execute(select(integration_source_ci))).mappings().one()
    assert record["state"] == "conflict" and record["repair_task_id"] is None
    handler._cmd_ensure_task.assert_not_called()


@pytest.mark.parametrize("source_blocker", [
    "hold", "repair_hold", "gate", "rejected", "generation", "policy",
])
async def test_green_repair_readmits_exact_failed_source_with_cleanup_coverage(case, source_blocker):
    await _continuous_policy(case)
    db = case["db"]
    branch = "aq/source-repair"
    _git("push", "origin", f"{case['second']}:refs/heads/{branch}", cwd=case["work"])
    async with db.immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="repair-source", project_id="p", repo_id="repo", title="CI repair", description="",
            status="COMPLETED", task_type="bugfix", branch_name=branch,
            pr_url="https://github.com/o/r/pull/8", created_at=1.0, updated_at=1.0))
        await conn.execute(insert(task_branch_origins).values(
            id="origin-repair-source", task_id="repair-source", repository_id="repo",
            base_sha=case["base"], creation_generation=0, reserved=True, created_at=1.0))
        await conn.execute(insert(task_integration_checkpoints).values(
            task_id="repair-source", repository_id="repo", branch=branch,
            checkpoint_sha=case["second"], generation=0, updated_at=1.0))
        await conn.execute(insert(integration_source_ci).values(
            task_id="e1", repository_id="repo", source_base=case["base"], source_head=case["first"],
            generation=1, policy_generation=0, state="red", evidence={"failed_check": "unit"},
            repair_task_id="repair-source", repair_attempt=1, observed_at=1000.0))
        await conn.execute(insert(integration_source_ci).values(
            task_id="repair-source", repository_id="repo", source_base=case["base"], source_head=case["second"],
            generation=0, policy_generation=0, state="pending", evidence={}, observed_at=1000.0))
    for task_id, head in (("e1", case["first"]), ("repair-source", case["second"])):
        assert await case["producer"].snapshot_authorized(task_id, reviewed_sha=head, policy_generation=0)
    async def members():
        async with db.immediate() as conn:
            return await TrainService(db)._eligible_members(
                conn, project_id="p", repository_id="repo", project_mode="pull_request")
    assert await members() == []
    async with db.immediate() as conn:
        await conn.execute(update(integration_source_ci).where(
            integration_source_ci.c.task_id == "repair-source").values(state="green"))
    assert {item["task_id"] for item in await members()} == {"e1", "repair-source"}
    # A second repair retains the complete ancestry and cleanup coverage.
    _git("checkout", "--detach", case["second"], cwd=case["work"])
    (case["work"] / "final-fix.txt").write_text("second CI repair\n")
    _git("add", "final-fix.txt", cwd=case["work"])
    _git("commit", "-m", "second CI repair", cwd=case["work"])
    third = _git("rev-parse", "HEAD", cwd=case["work"])
    final_branch = "aq/final-source-repair"
    _git("push", "origin", f"HEAD:refs/heads/{final_branch}", cwd=case["work"])
    async with db.immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="final-repair", project_id="p", repo_id="repo", title="Second CI repair",
            description="", status="COMPLETED", task_type="bugfix", branch_name=final_branch,
            pr_url="https://github.com/o/r/pull/9", created_at=1.0, updated_at=1.0))
        await conn.execute(insert(task_branch_origins).values(
            id="origin-final-repair", task_id="final-repair", repository_id="repo",
            base_sha=case["base"], creation_generation=0, reserved=True, created_at=1.0))
        await conn.execute(insert(task_integration_checkpoints).values(
            task_id="final-repair", repository_id="repo", branch=final_branch,
            checkpoint_sha=third, generation=0, updated_at=1.0))
        await conn.execute(update(integration_source_ci).where(
            integration_source_ci.c.task_id == "repair-source"
        ).values(state="red", repair_task_id="final-repair", repair_attempt=1))
        await conn.execute(insert(integration_source_ci).values(
            task_id="final-repair", repository_id="repo", source_base=case["base"],
            source_head=third, generation=0, policy_generation=0, state="green",
            evidence={}, observed_at=1001.0))
    assert await case["producer"].snapshot_authorized(
        "final-repair", reviewed_sha=third, policy_generation=0)
    assert {item["task_id"] for item in await members()} == {"e1", "repair-source", "final-repair"}
    # Explicit gates and stale source identities bind every repair, including
    # a second green repair whose own authorization evidence already exists.
    if source_blocker == "rejected":
        await case["producer"].snapshot_from_pull_request(
            "e1", verdict="rejected", reviewer_login="reviewer", reviewed_sha=case["first"])
    elif source_blocker == "gate":
        await db.create_gate("p", "human", "Product decision", waiter_task_ids=["e1"])
    else:
        async with db.immediate() as conn:
            if source_blocker in {"hold", "repair_hold"}:
                held = "repair-source" if source_blocker == "repair_hold" else "e1"
                await conn.execute(insert(task_labels).values(task_id=held, label="hold:product"))
            elif source_blocker == "generation":
                await conn.execute(update(task_integration_checkpoints).where(
                    task_integration_checkpoints.c.task_id == "e1").values(generation=2))
            else:
                await conn.execute(update(integration_source_ci).where(
                    integration_source_ci.c.task_id == "e1").values(policy_generation=1))
    assert await members() == []
    # Revocation invalidates authorization and CI tied to the older policy generation.
    async with db.immediate() as conn:
        assert await db.cas_project_integration_control_on(
            conn, project_id="p", expected_generation=0, effective_mode="train",
            desired_mode="train", draining=False)
    assert await members() == []


async def test_source_repair_admission_refuses_missing_source_ancestry(case):
    await _continuous_policy(case)
    async with case["db"].immediate() as conn:
        await conn.execute(insert(integration_source_ci).values(
            task_id="source", repository_id="repo", source_base=case["base"],
            source_head=case["second"], generation=0, policy_generation=0, state="red",
            evidence={}, repair_task_id="e1", observed_at=1000.0))
    with pytest.raises(HierarchyError, match="source ancestry"):
        await case["producer"].snapshot_authorized("e1", reviewed_sha=case["first"], policy_generation=0)


def _git(*args: str, cwd=None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
async def case(tmp_path):
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git("init", "--bare", "--initial-branch=main", str(remote))
    _git("clone", str(remote), str(work))
    _git("config", "user.name", "GitHub Review Test", cwd=work)
    _git("config", "user.email", "review@example.test", cwd=work)
    (work / "source.txt").write_text("base\n")
    _git("add", "source.txt", cwd=work)
    _git("commit", "-m", "base", cwd=work)
    base = _git("rev-parse", "HEAD", cwd=work)
    _git("push", "origin", "main", cwd=work)
    _git("switch", "-c", "aq/epic/retire-the-publisher", cwd=work)
    (work / "source.txt").write_text("first\n")
    _git("commit", "-am", "first epic head", cwd=work)
    first = _git("rev-parse", "HEAD", cwd=work)
    tree = _git("rev-parse", "HEAD^{tree}", cwd=work)
    _git("push", "origin", "HEAD", cwd=work)
    (work / "source.txt").write_text("second\n")
    _git("commit", "-am", "second epic head", cwd=work)
    second = _git("rev-parse", "HEAD", cwd=work)

    db = Database(lease_dsn("epic-pr-review-evidence.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="PR review project"))
    await db.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.CLONE, url=str(remote))
    )
    await db.update_project(
        "p",
        hierarchical_integration_mode="train",
        integration_repository_id="repo",
        integration_mode="pull_request",
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(project_integration_schedules).values(
                project_id="p", interval_seconds=300, next_due_at=1300.0, updated_at=1000.0
            )
        )
        await conn.execute(
            insert(tasks).values(
                id="e1",
                project_id="p",
                repo_id="repo",
                title="Epic",
                description="",
                status="COMPLETED",
                branch_name="aq/epic/retire-the-publisher",
                pr_url="https://github.com/o/r/pull/7",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(tasks).values(
                id="c1",
                project_id="p",
                repo_id="repo",
                title="Child",
                description="",
                status="COMPLETED",
                parent_task_id="e1",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin-e1",
                task_id="e1",
                repository_id="repo",
                base_sha=base,
                creation_generation=1,
                reserved=True,
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode-e1",
                parent_task_id="e1",
                repository_id="repo",
                generation=1,
                pre_collection_checkpoint_sha=base,
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="operation-e1",
                target_kind="parent",
                parent_task_id="e1",
                episode_id="episode-e1",
                active_stage=0,
                state="completed",
                policy_snapshot={"version": 1},
                artifact_snapshot={"version": 1},
                required_check_version="checks-v1",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="verification-e1",
                operation_id="operation-e1",
                parent_task_id="e1",
                episode_id="episode-e1",
                generation=1,
                head_sha=first,
                required_check_version="checks-v1",
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_parent_operation_completions).values(
                operation_id="operation-e1",
                verification_id="verification-e1",
                parent_task_id="e1",
                episode_id="episode-e1",
                completed_at=1.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="e1",
                repository_id="repo",
                branch="aq/epic/retire-the-publisher",
                checkpoint_sha=first,
                generation=1,
                verified_sha=first,
                verified_generation=1,
                episode_id="episode-e1",
                current_verification_id="verification-e1",
                last_completed_operation_id="operation-e1",
                last_completed_verification_id="verification-e1",
                updated_at=1.0,
            )
        )
    promotion = PromotionService(db, data_dir=tmp_path / "data", git_manager=GitManager())
    yield {
        "db": db,
        "producer": ReviewEvidenceProducer(db, promotion, clock=lambda: 1000.0),
        "work": work,
        "remote": str(remote),
        "first": first,
        "second": second,
        "tree": tree,
        "base": base,
    }
    await db.close()


async def _rows(db):
    async with db._engine.connect() as conn:
        rows = (await conn.execute(select(integration_review_evidence))).mappings().all()
    return [dict(row) for row in rows]


class _ReviewClient:
    def __init__(self, head: str, reviews: list[dict], *, moved: bool = False):
        self.head = head
        self.reviews = reviews
        self.moved = moved

    async def pull_request(self, _url):
        return {
            "state": "open",
            "head": {
                "sha": "0" * 40 if self.moved else self.head,
                "ref": "aq/epic/retire-the-publisher",
                "repo": {"id": 7},
            },
            "base": {"ref": "main", "repo": {"id": 7}},
        }

    async def paged_list(self, _path):
        return self.reviews


class _ReviewGit:
    def __init__(self, client):
        self.client = client

    async def bind_github_repository(self, _url):
        return SimpleNamespace(repository_id=7, full_name="o/r")

    def _github_client(self, _binding):
        return self.client


async def test_live_review_poller_arms_window_only_for_exact_human_approval(case):
    reviews = [
        {
            "id": 1,
            "state": "APPROVED",
            "user": {"type": "Bot", "login": "fixture[bot]"},
            "commit_id": case["first"],
        },
        {
            "id": 2,
            "state": "APPROVED",
            "user": {"type": "User", "login": "reviewer"},
            "commit_id": case["first"],
            "body": "Approved on GitHub",
        },
    ]
    poller = GitHubReviewPoller(
        case["db"], case["producer"], _ReviewGit(_ReviewClient(case["first"], reviews))
    )
    await poller.tick(1000.0)
    rows = await _rows(case["db"])
    assert len(rows) == 1
    assert rows[0]["reviewer_identity"] == "github:reviewer"
    assert rows[0]["evidence"]["github_review_id"] == 2
    async with case["db"]._engine.connect() as conn:
        schedule = (
            await conn.execute(select(project_integration_schedules))
        ).mappings().one()
    assert schedule["settling_first_approval_at"] == 1000.0
    await poller.tick(1031.0)
    assert len(await _rows(case["db"])) == 1


@pytest.mark.parametrize("moved", [True, False])
async def test_live_review_poller_refuses_moved_pr_or_stale_review(case, moved):
    client = _ReviewClient(
        case["first"],
        [{
            "id": 3,
            "state": "APPROVED",
            "user": {"type": "User", "login": "reviewer"},
            "commit_id": case["first"] if moved else case["second"],
        }],
        moved=moved,
    )
    await GitHubReviewPoller(case["db"], case["producer"], _ReviewGit(client)).tick(1000.0)
    assert await _rows(case["db"]) == []


async def test_legacy_leaf_root_materializes_exact_pr_source(case, tmp_path, monkeypatch):
    async with case["db"].immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="legacy", project_id="p", repo_id="repo", title="Legacy root",
            description="", status="COMPLETED",
            branch_name="aq/epic/retire-the-publisher",
            pr_url="https://github.com/o/r/pull/7", created_at=1.0, updated_at=1.0,
        ))
    git = GitManager()
    findings = await _unmaterialized_pr_findings(
        SimpleNamespace(db=case["db"]), {"p"}
    )
    assert [item["task_id"] for item in findings] == ["legacy"]

    async def binding(_url):
        return SimpleNamespace(repository_id=7, full_name="o/r")

    monkeypatch.setattr(git, "bind_github_repository", binding)
    monkeypatch.setattr(git, "_github_client", lambda _binding: _ReviewClient(case["first"], []))
    promotion = PromotionService(case["db"], data_dir=tmp_path / "legacy-data", git_manager=git)
    repair = RootMaterialization(case["db"], promotion)
    dry = await repair.run("legacy")
    assert dry["outcome"] == "would_materialize"
    assert (dry["base_sha"], dry["head_sha"]) == (case["base"], case["first"])
    assert (await repair.run("legacy", dry_run=False, expected_head_sha=case["second"],
                             reason="legacy PR", operator_id="operator"))["outcome"] == "changed"
    applied = await repair.run("legacy", dry_run=False, expected_head_sha=case["first"],
                               reason="legacy PR", operator_id="operator")
    assert applied["outcome"] == "materialized"
    assert (await repair.run("legacy"))["outcome"] == "not_eligible"
    async with case["db"]._engine.connect() as conn:
        checkpoint = (await conn.execute(select(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "legacy"
        ))).mappings().one()
        origin = (await conn.execute(select(task_branch_origins).where(
            task_branch_origins.c.task_id == "legacy"
        ))).mappings().one()
        source = await case["producer"]._pull_request_source_on(conn, "legacy")
        eligible = await case["db"].eligible_root_page_on(
            conn, project_id="p", repository_id="repo", after=None, limit=20,
        )
    assert checkpoint["checkpoint_sha"] == case["first"]
    assert origin["base_sha"] == case["base"]
    assert origin["materialized"] is True
    assert source["head"] == case["first"]
    assert any(row["task_id"] == "legacy" for row in eligible)
    assert await _unmaterialized_pr_findings(SimpleNamespace(db=case["db"]), {"p"}) == []
    evidence = await case["producer"].snapshot_from_pull_request(
        "legacy", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    async with case["db"].immediate() as conn:
        members = await TrainService(case["db"])._eligible_members(
            conn, project_id="p", repository_id="repo", project_mode="pull_request"
        )
    assert [member["task_id"] for member in members] == ["legacy"]
    assert members[0]["review"] == evidence


async def test_materialization_refuses_pr_head_that_differs_from_git(case, tmp_path, monkeypatch):
    async with case["db"].immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="legacy", project_id="p", repo_id="repo", title="Legacy root",
            description="", status="COMPLETED",
            branch_name="aq/epic/retire-the-publisher",
            pr_url="https://github.com/o/r/pull/7", created_at=1.0, updated_at=1.0,
        ))
    git = GitManager()

    async def binding(_url):
        return SimpleNamespace(repository_id=7, full_name="o/r")

    monkeypatch.setattr(git, "bind_github_repository", binding)
    monkeypatch.setattr(git, "_github_client", lambda _binding: _ReviewClient(case["first"], [], moved=True))
    repair = RootMaterialization(
        case["db"], PromotionService(case["db"], data_dir=tmp_path, git_manager=git)
    )
    result = await repair.run("legacy")
    assert result["outcome"] == "changed"
    async with case["db"]._engine.connect() as conn:
        assert (await conn.execute(select(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "legacy"
        ))).first() is None


async def test_legacy_parent_root_cannot_forge_leaf_checkpoint(case, tmp_path):
    async with case["db"].immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="legacy-parent", project_id="p", repo_id="repo", title="Legacy parent",
            description="", status="COMPLETED",
            branch_name="aq/epic/retire-the-publisher",
            pr_url="https://github.com/o/r/pull/7", created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(insert(tasks).values(
            id="legacy-child", project_id="p", repo_id="repo", title="Legacy child",
            description="", status="COMPLETED", parent_task_id="legacy-parent",
            created_at=1.0, updated_at=1.0,
        ))
    repair = RootMaterialization(
        case["db"], PromotionService(case["db"], data_dir=tmp_path, git_manager=GitManager())
    )
    result = await repair.run("legacy-parent")
    assert result["outcome"] == "not_eligible"
    assert "verification evidence" in result["reason"]


async def test_poller_warns_when_completed_pr_has_no_source(case, caplog):
    async with case["db"].immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="legacy", project_id="p", repo_id="repo", title="Legacy root",
            description="", status="COMPLETED",
            branch_name="aq/epic/retire-the-publisher",
            pr_url="https://github.com/o/r/pull/7", created_at=1.0, updated_at=1.0,
        ))
    poller = GitHubReviewPoller(
        case["db"], case["producer"], _ReviewGit(_ReviewClient(case["first"], []))
    )
    await poller.tick(1000.0)
    assert "Completed train root legacy has PR" in caplog.text


class _PollClient:
    """A GitHub fake whose PR changes between ticks and that records each call."""

    def __init__(self, head: str, reviews: list[dict], *, state: str = "open"):
        self.head = head
        self.reviews = reviews
        self.state = state
        self.updated_at = "2026-10-02T00:00:00Z"
        self.calls: list[str] = []
        self.pull_error: Exception | None = None
        self.reviews_error: Exception | None = None
        self.open_error: Exception | None = None

    async def pull_request(self, _url):
        self.calls.append("pull")
        if self.pull_error is not None:
            raise self.pull_error
        return {
            "state": self.state,
            "updated_at": self.updated_at,
            "head": {"sha": self.head, "ref": "aq/epic/retire-the-publisher", "repo": {"id": 7}},
            "base": {"ref": "main", "repo": {"id": 7}},
        }

    async def paged_list(self, path):
        if "/reviews" in path:
            self.calls.append("reviews")
            if self.reviews_error is not None:
                raise self.reviews_error
            return list(self.reviews)
        assert path == "/repositories/7/pulls?state=open&per_page=100"
        self.calls.append("open")
        if self.open_error is not None:
            raise self.open_error
        return [{"number": 7, "state": "open"}] if self.state == "open" else []

    async def commit_check_runs(self, _sha):
        self.calls.append("checks")
        return []


class _CountingGit(_ReviewGit):
    def __init__(self, client):
        super().__init__(client)
        self.binds = 0
        self.error: Exception | None = None

    async def bind_github_repository(self, url):
        self.binds += 1
        if self.error is not None:
            raise self.error
        return await super().bind_github_repository(url)


class _CountingProducer:
    """Delegate to the real producer, recording each Git-proven snapshot request."""

    def __init__(self, producer):
        self.producer = producer
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        return getattr(self.producer, name)

    async def snapshot_from_pull_request(self, task_id, **kwargs):
        self.calls.append(("review", kwargs["github_review_id"]))
        return await self.producer.snapshot_from_pull_request(task_id, **kwargs)

    async def snapshot_authorized(self, task_id, **kwargs):
        self.calls.append(("authorized", kwargs["policy_generation"]))
        return await self.producer.snapshot_authorized(task_id, **kwargs)


def _review(review_id: int, state: str, head: str) -> dict:
    return {
        "id": review_id, "state": state, "commit_id": head, "body": f"{state} on GitHub",
        "user": {"type": "User", "login": "reviewer"},
    }


async def _review_ids(db) -> list[int]:
    return sorted(row["evidence"]["github_review_id"] for row in await _rows(db))


async def test_poller_rereads_reviews_only_when_the_pull_request_changes(case):
    client = _PollClient(case["first"], [_review(2, "APPROVED", case["first"])])
    producer = _CountingProducer(case["producer"])
    poller = GitHubReviewPoller(case["db"], producer, _ReviewGit(client))
    await poller.tick(1000.0)
    await poller.tick(1031.0)
    await poller.tick(1062.0)
    # The unchanged PR is fetched on each visit; its reviews and the stored
    # approval are not read or proven against Git again.
    assert client.calls == ["pull", "reviews", "pull", "pull"]
    assert producer.calls == [("review", 2)]

    # A rejection moves updated_at: only the new verdict is proven and stored.
    client.reviews.append(_review(3, "CHANGES_REQUESTED", case["first"]))
    client.updated_at = "2026-10-02T00:05:00Z"
    await poller.tick(1093.0)
    assert client.calls[-2:] == ["pull", "reviews"]
    assert producer.calls == [("review", 2), ("review", 3)]
    latest = max(await _rows(case["db"]), key=lambda row: row["created_at"])
    assert (latest["verdict"], latest["evidence"]["github_review_id"]) == ("rejected", 3)

    # A dismissed approval is a new verdict for the same review id.
    client.reviews[0] = {**client.reviews[0], "state": "DISMISSED"}
    client.updated_at = "2026-10-02T00:06:00Z"
    await poller.tick(1124.0)
    assert producer.calls[-1] == ("review", 2)
    assert [row["verdict"] for row in await _rows(case["db"])].count("rejected") == 2


async def test_poller_refresh_reads_unchanged_reviews_without_reproving_stored_ones(case):
    client = _PollClient(case["first"], [_review(2, "APPROVED", case["first"])])
    producer = _CountingProducer(case["producer"])
    poller = GitHubReviewPoller(
        case["db"], producer, _ReviewGit(client), review_refresh_seconds=300.0
    )
    await poller.tick(1000.0)
    # A verdict GitHub did not surface through updated_at lands at the refresh.
    client.reviews.append(_review(4, "APPROVED", case["first"]))
    await poller.tick(1031.0)
    assert client.calls.count("reviews") == 1
    await poller.tick(1300.0)
    assert client.calls.count("reviews") == 2
    assert producer.calls == [("review", 2), ("review", 4)]
    assert await _review_ids(case["db"]) == [2, 4]


@pytest.mark.parametrize("open_list", ["readable", "unavailable"])
async def test_poller_skips_a_closed_pull_request_until_it_reopens(case, open_list):
    client = _PollClient(case["first"], [_review(2, "APPROVED", case["first"])], state="closed")
    if open_list == "unavailable":
        client.open_error = GitHubAccessError("transient", "GitHub request failed (transient)")
    poller = GitHubReviewPoller(case["db"], case["producer"], _ReviewGit(client))
    await poller.tick(1000.0)
    await poller.tick(1031.0)
    # Without a readable open list the closed PR is fetched as before.
    assert client.calls == (
        ["pull", "open"] if open_list == "readable" else ["pull", "open", "pull"]
    )
    assert await _rows(case["db"]) == []

    # Reopening is observed on the next visit, and its reviews are read.
    client.state = "open"
    client.updated_at = "2026-10-02T00:05:00Z"
    await poller.tick(1062.0)
    assert client.calls[-3:] == ["open", "pull", "reviews"]
    assert await _review_ids(case["db"]) == [2]

    # Closing again is observed on the next visit.
    client.state = "closed"
    await poller.tick(1093.0)
    assert client.calls[-1] == "pull"
    client.calls.clear()
    await poller.tick(1124.0)
    assert client.calls == (["open"] if open_list == "readable" else ["open", "pull"])


async def test_cursor_reset_failure_or_restart_cannot_lose_a_review(case):
    client = _PollClient(case["first"], [])
    poller = GitHubReviewPoller(case["db"], case["producer"], _ReviewGit(client))
    await poller.tick(1000.0)
    # A cursor past every root wraps within the tick and still sees the change.
    client.reviews.append(_review(2, "APPROVED", case["first"]))
    client.updated_at = "2026-10-02T00:05:00Z"
    poller.after_id = "zzz"
    await poller.tick(1031.0)
    assert await _review_ids(case["db"]) == [2]

    # A failed read is not an observation: the next visit reads again although
    # the PR has not changed since.
    client.reviews.append(_review(3, "CHANGES_REQUESTED", case["first"]))
    client.updated_at = "2026-10-02T00:10:00Z"
    client.reviews_error = GitHubAccessError("transient", "GitHub request failed (transient)")
    await poller.tick(1062.0)
    client.reviews_error = None
    await poller.tick(1093.0)
    assert await _review_ids(case["db"]) == [2, 3]

    # A restart starts from an empty cache: a verdict GitHub never surfaced
    # through updated_at is recorded on the first visit.
    client.reviews.append(_review(4, "APPROVED", case["first"]))
    await poller.tick(1124.0)
    assert await _review_ids(case["db"]) == [2, 3]
    await GitHubReviewPoller(case["db"], case["producer"], _ReviewGit(client)).tick(1124.0)
    assert await _review_ids(case["db"]) == [2, 3, 4]


async def test_authorization_is_proven_once_per_identity_and_generation(case):
    await _continuous_policy(case)
    observed = []

    async def source_ci(observation):
        observed.append(observation.state)
        return {"success": True}

    client = _PollClient(case["first"], [])
    producer = _CountingProducer(case["producer"])
    poller = GitHubReviewPoller(
        case["db"], producer, _ReviewGit(client), source_ci_handler=source_ci
    )
    # A hold refuses authorization, which is attempted again until it clears.
    async with case["db"].immediate() as conn:
        await conn.execute(insert(task_labels).values(task_id="e1", label="hold:product"))
    await poller.tick(1000.0)
    await poller.tick(1031.0)
    assert producer.calls == [("authorized", 0)] * 2
    async with case["db"].immediate() as conn:
        await conn.execute(delete(task_labels).where(task_labels.c.task_id == "e1"))
    await poller.tick(1062.0)
    await poller.tick(1093.0)
    assert producer.calls == [("authorized", 0)] * 3
    assert [row["evidence"]["decision_path"] for row in await _rows(case["db"])] == [
        "authorized_task"
    ]
    # Exact-head source CI is still observed on every visit.
    assert observed == ["pending"] * 4
    assert client.calls.count("checks") == 4

    # A new policy generation is a new authorization.
    async with case["db"].immediate() as conn:
        await conn.execute(
            update(projects).where(projects.c.id == "p")
            .values(hierarchical_integration_generation=1)
        )
    await poller.tick(1124.0)
    assert producer.calls[-1] == ("authorized", 1)
    assert sorted(
        row["evidence"]["policy_generation"] for row in await _rows(case["db"])
    ) == [0, 1]


async def test_poller_logs_each_root_condition_once_per_change(case, caplog):
    caplog.set_level(logging.INFO, logger="src.integration.github_review_poll")
    async with case["db"].immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="legacy", project_id="p", repo_id="repo", title="Legacy root",
            description="", status="COMPLETED",
            branch_name="aq/epic/retire-the-publisher",
            pr_url="https://github.com/o/r/pull/8", created_at=1.0, updated_at=1.0,
        ))
    client = _PollClient(case["first"], [])
    client.pull_error = GitHubAccessError("transient", "GitHub request failed (transient)")
    poller = GitHubReviewPoller(case["db"], case["producer"], _ReviewGit(client))
    for now in (1000.0, 1031.0, 1062.0):
        await poller.tick(now)
    assert caplog.text.count("Completed train root legacy has PR") == 1
    assert caplog.text.count("GitHub PR review poll failed for epic e1") == 1

    # Recovery, then a moved head, then a recurrence: each is a change.
    client.pull_error = None
    await poller.tick(1093.0)
    client.head = case["second"]
    await poller.tick(1124.0)
    await poller.tick(1155.0)
    assert caplog.text.count("does not show its exact source head (head moved)") == 1
    client.pull_error = GitHubAccessError("transient", "GitHub request failed (transient)")
    await poller.tick(1186.0)
    assert caplog.text.count("GitHub PR review poll failed for epic e1") == 2
    assert caplog.text.count("Completed train root legacy has PR") == 1


async def test_poller_binds_each_repository_once_per_tick(case, caplog):
    async with case["db"].immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="e2", project_id="p", repo_id="repo", title="Leaf root", description="",
            status="COMPLETED", branch_name="aq/e2",
            pr_url="https://github.com/o/r/pull/8", created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(insert(task_branch_origins).values(
            id="origin-e2", task_id="e2", repository_id="repo", base_sha=case["base"],
            creation_generation=1, reserved=True, created_at=1.0,
        ))
        await conn.execute(insert(task_integration_checkpoints).values(
            task_id="e2", repository_id="repo", branch="aq/e2",
            checkpoint_sha=case["second"], generation=0, updated_at=1.0,
        ))
    client = _PollClient(case["first"], [])
    git = _CountingGit(client)
    poller = GitHubReviewPoller(case["db"], case["producer"], git)
    await poller.tick(1000.0)
    assert (git.binds, client.calls.count("pull")) == (1, 2)
    # A failed bind fails every root of that repository with one attempt.
    git.error = GitError("GitHub access service is not configured")
    await poller.tick(1031.0)
    assert git.binds == 2
    assert caplog.text.count("GitHub PR review poll failed for epic") == 2


async def test_new_github_review_id_can_supersede_an_earlier_rejection(case):
    for review_id, verdict in ((4, "rejected"), (5, "approved")):
        await case["producer"].snapshot_from_pull_request(
            "e1",
            verdict=verdict,
            reviewer_login="reviewer",
            reviewed_sha=case["first"],
            github_review_id=review_id,
        )
    rows = await _rows(case["db"])
    assert len(rows) == 2
    assert max(rows, key=lambda row: row["created_at"])["verdict"] == "approved"


async def test_approval_writes_exact_trusted_evidence_and_is_eligible(case):
    evidence = await case["producer"].snapshot_from_pull_request(
        "e1",
        verdict="approved",
        reviewer_login="jkern",
        reviewed_sha=case["first"],
        summary="Looks good",
    )
    assert evidence == (await _rows(case["db"]))[0]
    assert {
        key: evidence[key]
        for key in (
            "source_task_id",
            "repository_id",
            "source_base",
            "reviewed_head_sha",
            "reviewed_tree_sha",
            "reviewer_task_id",
            "reviewer_session_attempt_id",
            "reviewer_identity",
            "review_kind",
            "generation",
            "verdict",
        )
    } == {
        "source_task_id": "e1",
        "repository_id": "repo",
        "source_base": case["base"],
        "reviewed_head_sha": case["first"],
        "reviewed_tree_sha": case["tree"],
        "reviewer_task_id": None,
        "reviewer_session_attempt_id": None,
        "reviewer_identity": "github:jkern",
        "review_kind": "parent",
        "generation": 1,
        "verdict": "approved",
    }
    assert evidence["evidence"] == {
        "decision_path": "github_pull_request",
        "summary": "Looks good",
        "feedback": "",
        "reviewed_sha": case["first"],
        "pr_url": "https://github.com/o/r/pull/7",
        "verification_id": "verification-e1",
    }
    async with case["db"].immediate() as conn:
        assert await settled(conn, project_id="p", now=1299.0) is False
        assert await settled(conn, project_id="p", now=1300.0) is True
        members = await TrainService(case["db"])._eligible_members(
            conn, project_id="p", repository_id="repo", project_mode="pull_request"
        )
    assert [member["task_id"] for member in members] == ["e1"]
    assert members[0]["review"] == evidence


async def _childless_root(case, *, checkpoint_sha=None):
    """A childless train root on its own published branch (noble-harbor-74's shape)."""
    head = checkpoint_sha or case["first"]
    branch = "aq/epic/fix-one-thing"
    _git("push", "origin", f"{case['first']}:refs/heads/{branch}", cwd=case["work"])
    async with case["db"].immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                id="r1", project_id="p", repo_id="repo", title="Fix one thing",
                description="", status="COMPLETED", branch_name=branch,
                pr_url="https://github.com/o/r/pull/9", created_at=1.0, updated_at=1.0,
            )
        )
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin-r1", task_id="r1", repository_id="repo", branch_name=branch,
                parent_ref="main", base_sha=case["base"], creation_generation=0,
                reserved=True, materialized=True, created_at=1.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="r1", repository_id="repo", branch=branch, checkpoint_sha=head,
                generation=0, state="working", version=1, updated_at=1.0,
            )
        )


async def test_approval_of_a_childless_root_is_leaf_evidence_the_train_seats(case):
    """A leaf root's PR approval must reach the train, not stop at ingestion."""
    await _childless_root(case)

    evidence = await case["producer"].snapshot_from_pull_request(
        "r1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )

    assert evidence["review_kind"] == "leaf"
    assert evidence["reviewed_head_sha"] == case["first"]
    assert evidence["source_base"] == case["base"]
    assert evidence["generation"] == 0
    assert evidence["evidence"]["verification_id"] is None
    async with case["db"].immediate() as conn:
        members = await TrainService(case["db"])._eligible_members(
            conn, project_id="p", repository_id="repo", project_mode="pull_request"
        )
    assert [member["task_id"] for member in members] == ["r1"]
    assert members[0]["source_kind"] == "leaf"
    assert members[0]["review"] == evidence


async def test_childless_root_still_at_its_base_takes_no_verdict(case):
    await _childless_root(case, checkpoint_sha=case["base"])

    assert await case["producer"].snapshot_from_pull_request(
        "r1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["base"]
    ) is None
    assert await _rows(case["db"]) == []


async def test_approval_of_moved_head_is_not_reused(case):
    first = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    _git("push", "origin", "HEAD", cwd=case["work"])
    async with case["db"].immediate() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode-e1-next",
                parent_task_id="e1",
                repository_id="repo",
                generation=2,
                pre_collection_checkpoint_sha=case["first"],
                created_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="operation-e1-next",
                target_kind="parent",
                parent_task_id="e1",
                episode_id="episode-e1-next",
                active_stage=0,
                state="completed",
                policy_snapshot={"version": 1},
                artifact_snapshot={"version": 1},
                required_check_version="checks-v1",
                created_at=2.0,
                updated_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="verification-e1-next",
                operation_id="operation-e1-next",
                parent_task_id="e1",
                episode_id="episode-e1-next",
                generation=2,
                head_sha=case["second"],
                required_check_version="checks-v1",
                created_at=2.0,
            )
        )
        await conn.execute(
            insert(integration_parent_operation_completions).values(
                operation_id="operation-e1-next",
                verification_id="verification-e1-next",
                parent_task_id="e1",
                episode_id="episode-e1-next",
                completed_at=2.0,
            )
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "e1")
            .values(
                generation=2,
                checkpoint_sha=case["second"],
                verified_sha=case["second"],
                verified_generation=2,
                episode_id="episode-e1-next",
                current_verification_id="verification-e1-next",
                last_completed_operation_id="operation-e1-next",
                last_completed_verification_id="verification-e1-next",
            )
        )
    async with case["db"].immediate() as conn:
        candidates = await case["db"].eligible_root_page_on(
            conn, project_id="p", repository_id="repo", after=None, limit=10
        )
        assert await case["db"].latest_exact_reviews_on(conn, candidates) == {}
    second = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["second"]
    )
    assert len(await _rows(case["db"])) == 2
    assert first["reviewed_head_sha"] == case["first"]
    assert second["reviewed_head_sha"] == case["second"]


async def test_rejection_records_feedback_without_arming_window(case):
    evidence = await case["producer"].snapshot_from_pull_request(
        "e1",
        verdict="rejected",
        reviewer_login="jkern",
        reviewed_sha=case["first"],
        feedback="Needs a test for the cap.",
    )
    assert evidence["verdict"] == "rejected"
    assert evidence["evidence"]["feedback"] == "Needs a test for the cap."
    async with case["db"].immediate() as conn:
        assert await settled(conn, project_id="p", now=1e12) is True
        schedule = (await conn.execute(select(project_integration_schedules))).mappings().one()
        assert schedule["settling_first_approval_at"] is None
        assert schedule["settling_fires_at"] is None


async def test_epic_without_pull_request_is_refused(case):
    async with case["db"].immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "e1").values(pr_url=None))
    result = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    assert result is None
    assert await _rows(case["db"]) == []


async def test_unverified_head_is_refused(case):
    with pytest.raises(HierarchyError, match="reviewed head"):
        await case["producer"].snapshot_from_pull_request(
            "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["second"]
        )
    assert await _rows(case["db"]) == []


async def test_approval_is_refused_when_remote_head_moved(case):
    _git("push", "origin", "HEAD", cwd=case["work"])
    with pytest.raises(HierarchyError, match="reviewed remote ref"):
        await case["producer"].snapshot_from_pull_request(
            "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
        )
    assert await _rows(case["db"]) == []


async def test_later_rejection_wins_when_clock_has_not_advanced(case):
    approved = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    rejected = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="rejected", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    assert rejected["created_at"] > approved["created_at"]
    async with case["db"].immediate() as conn:
        candidates = await case["db"].eligible_root_page_on(
            conn, project_id="p", repository_id="repo", after=None, limit=10
        )
        latest = await case["db"].latest_exact_reviews_on(conn, candidates)
        members = await TrainService(case["db"])._eligible_members(
            conn, project_id="p", repository_id="repo", project_mode="pull_request"
        )
    assert list(latest.values()) == [rejected]
    assert members == []


async def test_duplicate_approval_is_idempotent(case):
    first = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    second = await case["producer"].snapshot_from_pull_request(
        "e1", verdict="approved", reviewer_login="jkern", reviewed_sha=case["first"]
    )
    assert first == second
    assert len(await _rows(case["db"])) == 1
