"""Phantom Actions checks remain pending and get one exact-job recovery attempt."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.tables import integration_batches, integration_check_evidence
from src.git.github import GitHubAccessError
from src.integration.batches import candidate_ref
from src.integration.candidate_baseline import candidate_head
from src.integration.check_reconciliation import CheckReconciliation
from src.integration.checks import ChecksState, ExactChecks, HostedChecks
from src.integration.ci_producers import HostedCIProducer
from src.integration.status import IntegrationStatusService
from src.integration.train import CandidateChecks, TrainLane
from src.models import Project, RepoConfig, RepoSourceType
from src.profiles.capabilities import DENY_ALL
from tests.test_integration_candidate_baseline import (
    CANDIDATE,
    OTHER,
    ROOT,
    Clock,
    Repair,
    Service,
    frozen,
    train,
)
from tests.test_integration_ci_producers import github


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("check-reconciliation.db")
    await database.create_project(Project(id="p", name="Project"))
    await database.create_repo(RepoConfig(id="r", project_id="p", source_type=RepoSourceType.LINK))
    return database


def phantom(db, *, now=1000, workflow_completed=True, steps_completed=True):
    client, trust = github(app=False, names=("unit", "lint"))
    for record in [*client.checks, *client.workflows, *client.jobs]:
        record["head_sha"] = CANDIDATE
    client.checks[0].update(status="in_progress", conclusion=None)
    client.jobs[0].update(status="in_progress", conclusion=None)
    if workflow_completed:
        client.workflows[0]["updated_at"] = "1970-01-01T00:10:00Z"
    else:
        client.workflows[0].update(status="in_progress", conclusion=None)
    client.jobs[0]["steps"] = [{
        "name": "Tests", "status": "completed" if steps_completed else "in_progress",
        "conclusion": "success" if steps_completed else None,
        "completed_at": "1970-01-01T00:10:00Z" if steps_completed else None,
    }]
    client.rerun_job = AsyncMock(return_value={})
    trust = trust.model_copy(update={"canonical_repository_id": "r"})
    clock = Clock(now)
    checks = ExactChecks(db, HostedChecks(HostedCIProducer(client, trust)), clock=clock)
    return client, checks, clock


async def records(db):
    async with db._engine.connect() as conn:
        return list((await conn.scalars(select(
            integration_check_evidence.c.check_reconciliation,
        ).where(integration_check_evidence.c.check_reconciliation.isnot(None)))).all())


@pytest.mark.parametrize("workflow_completed,steps_completed", [(True, True), (True, False),
                                                               (False, True)])
async def test_train_reruns_only_the_phantom_job_and_never_infers_green(
    db, workflow_completed, steps_completed,
):
    client, checks, _ = phantom(db, workflow_completed=workflow_completed,
                              steps_completed=steps_completed)
    batch = await frozen(db)
    repair = Repair()
    t = train(checks, repair=repair, frozen_batch=batch)
    await t.tick()
    await t.drain()
    [visit] = t.status()
    assert visit["state"] == "testing" and visit["checks"] == "pending"
    assert visit["detail"]["check_reconciliation"][0]["outcome"] == "requested"
    client.rerun_job.assert_awaited_once_with(51)
    assert repair.calls == []
    head = candidate_head(batch, CANDIDATE)
    assert (await checks.read(head)).state is ChecksState.PENDING
    assert len(await records(db)) == 1

    # A new train/cache simulates restart: the durable reservation keeps its bound.
    restarted = ExactChecks(db, checks.provider, clock=checks.clock)
    replay = await train(restarted, frozen_batch=batch).visit(ROOT)
    assert replay.detail["check_reconciliation"][0]["outcome"] == "already_attempted"
    client.rerun_job.assert_awaited_once_with(51)
    status = await IntegrationStatusService(db, git_first="active", train=t).train_status("p")
    [projected] = status["batches"]
    assert projected["check_reconciliation"][0]["outcome"] == "requested"
    assert projected["check_reconciliation"][0]["sha"] == CANDIDATE

    # Only fresh normal evidence concludes the required check.
    client.checks[0].update(status="completed", conclusion="success")
    client.jobs[0].update(status="completed", conclusion="success")
    client.workflows[0].update(status="completed", conclusion="success")
    assert (await checks.refresh(head)).green
    assert (await records(db))[0]["outcome"] == "requested"


async def test_grace_then_one_reservation_even_with_concurrent_requests(db):
    client, checks, clock = phantom(db, now=899)
    batch = await frozen(db)
    head = candidate_head(batch, CANDIDATE)
    await checks.refresh(head)
    authorize = AsyncMock(return_value=True)
    result = await checks.reconcile_stuck(batch, head, authorize=authorize)
    assert result[0]["outcome"] == "waiting" and result[0]["due_at"] == 900
    assert await records(db) == []
    client.rerun_job.assert_not_awaited()

    clock.now = 900
    replies = await asyncio.gather(*(
        checks.reconcile_stuck(batch, head, authorize=authorize) for _ in range(3)
    ))
    assert sorted(reply[0]["outcome"] for reply in replies) == [
        "already_attempted", "already_attempted", "requested",
    ]
    client.rerun_job.assert_awaited_once_with(51)


@pytest.mark.parametrize("change", ["running", "foreign_job", "old_attempt", "foreign_repo",
                                   "foreign_head", "wrong_check", "no_timestamp"])
async def test_running_or_untrusted_jobs_never_authorize_a_rerun(db, change):
    client, checks, _ = phantom(db)
    if change == "running":
        client.workflows[0].update(status="in_progress", conclusion=None)
        client.jobs[0]["steps"][0]["status"] = "in_progress"
    elif change == "foreign_job":
        client.jobs[0]["check_run_url"] = "https://api.github.com/repos/other/repo/check-runs/11"
    elif change == "old_attempt":
        client.workflows[0]["run_attempt"] = 2
    elif change == "foreign_repo":
        client.workflows[0]["head_repository"]["id"] = 999
    elif change == "foreign_head":
        client.jobs[0]["head_sha"] = OTHER
    elif change == "wrong_check":
        client.jobs[0]["name"] = "unrequired"
    elif change == "no_timestamp":
        client.workflows[0].pop("updated_at")
        client.jobs[0]["steps"][0]["completed_at"] = None
    batch = await frozen(db)
    head = candidate_head(batch, CANDIDATE)
    result = await checks.refresh(head)
    assert not result.green
    assert await checks.reconcile_stuck(batch, head, authorize=AsyncMock(return_value=True)) == []
    client.rerun_job.assert_not_awaited()
    assert await records(db) == []


@pytest.mark.parametrize("condition", ["moved", "paused", "aborted", "generation_changed"])
async def test_stale_or_held_batch_refuses_recovery(db, condition):
    client, checks, _ = phantom(db)
    batch = await frozen(db)
    head = candidate_head(batch, CANDIDATE)
    await checks.refresh(head)
    if condition != "moved":
        values = ({"repair_attempt_count": 1} if condition == "generation_changed"
                  else {"intent": condition})
        async with db._engine.begin() as conn:
            await conn.execute(update(integration_batches).where(
                integration_batches.c.id == batch.id).values(**values))
    result = await checks.reconcile_stuck(
        batch, head, authorize=AsyncMock(return_value=condition != "moved"),
    )
    assert result[0]["outcome"] == "refused"
    client.rerun_job.assert_not_awaited()
    assert await records(db) == []


@pytest.mark.parametrize("denied", [False, True])
async def test_ambiguous_request_failure_is_durable_and_never_retried(db, denied):
    client, checks, _ = phantom(db)
    batch = await frozen(db)
    head = candidate_head(batch, CANDIDATE)
    await checks.refresh(head)
    client.rerun_job.side_effect = (GitHubAccessError("permission", "denied") if denied
                                   else OSError("connection lost after write"))
    result = await checks.reconcile_stuck(batch, head, authorize=AsyncMock(return_value=True))
    assert result[0]["outcome"] == "unavailable"
    if denied:
        assert result[0]["classification"] == "permission_blocked"
        assert "actions_permission: write" in result[0]["remediation"]
    assert (await records(db))[0]["outcome"] == "unavailable"
    await checks.refresh(head)
    result = await CheckReconciliation(db).run(
        batch, head, checks, authorize=AsyncMock(return_value=True),
    )
    assert result[0]["outcome"] == "already_attempted"
    client.rerun_job.assert_awaited_once_with(51)
    assert not (await checks.read(head)).green


async def test_supervisor_control_previews_and_shares_automatic_bound(db, monkeypatch):
    client, checks, _ = phantom(db)
    batch = await frozen(db)
    snapshot = SimpleNamespace(error=None, observation=SimpleNamespace(source_heads={
        "refs/remotes/origin/" + candidate_ref(batch.id).removeprefix("refs/heads/"): CANDIDATE,
    }))
    lane = TrainLane(snapshot=AsyncMock(return_value=snapshot), service=Service(),
                     checks=CandidateChecks.fixed(checks))
    t = SimpleNamespace(lane_for=AsyncMock(return_value=lane),
                        targets=SimpleNamespace(targets=AsyncMock(return_value=[ROOT])))
    handler = IntegrationCommandsMixin()
    handler.db = db
    handler.orchestrator = SimpleNamespace(integration_train=t)
    args = {"batch_id": batch.id, "check_name": "unit"}
    monkeypatch.setattr(db, "get_session", AsyncMock(return_value=SimpleNamespace(
        id="supervisor", profile_id="supervisor", lifecycle="named", state="running",
        desired_state="running", project_id="p")))
    principal = ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL,
                                   session_id="supervisor", elevated=True, project_id="p")
    with principal_context(principal):
        preview = await handler._cmd_integration_rerequest_check(args)
    assert preview["outcome"] == "preview", preview
    assert preview["candidate_sha"] == CANDIDATE
    client.rerun_job.assert_not_awaited()
    assert await records(db) == []
    apply = {**args, "dry_run": False, "expected_candidate_sha": CANDIDATE,
             "reason": "Reconcile stuck job"}
    with principal_context(principal):
        result = await handler._cmd_integration_rerequest_check(apply)
    assert result["outcome"] == "reconciled", result
    assert result["evidence"]["checks"][0]["outcome"] == "requested"
    replay = await handler._cmd_integration_rerequest_check(apply)
    assert replay["evidence"]["checks"][0]["outcome"] == "already_attempted"
    client.rerun_job.assert_awaited_once_with(51)
    moved = await handler._cmd_integration_rerequest_check({
        **apply, "expected_candidate_sha": OTHER,
    })
    assert not moved["success"] and moved["outcome"] == "refused"

    for scoped in (ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL,
                                     session_id="worker", project_id="p"),
                   ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL,
                                     session_id="supervisor", elevated=True, project_id="other")):
        with principal_context(scoped):
            refused = await handler._cmd_integration_rerequest_check(apply)
        assert not refused["success"] and refused["outcome"] == "unauthorized"
    client.rerun_job.assert_awaited_once_with(51)


async def test_interruption_after_reservation_does_not_grant_another_request(db):
    client, checks, _ = phantom(db)
    batch = await frozen(db)
    head = candidate_head(batch, CANDIDATE)
    await checks.refresh(head)
    started = asyncio.Event()

    async def interrupted_request(job):
        started.set()
        await asyncio.Event().wait()

    client.rerun_job.side_effect = interrupted_request
    task = asyncio.create_task(checks.reconcile_stuck(
        batch, head, authorize=AsyncMock(return_value=True),
    ))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await records(db))[0]["outcome"] == "reserved"
    replay = await checks.reconcile_stuck(batch, head, authorize=AsyncMock(return_value=True))
    assert replay[0]["request_outcome"] == "reserved"
    client.rerun_job.assert_awaited_once_with(51)


@pytest.mark.parametrize("latest", ["completed", "new_attempt"])
async def test_cached_evidence_is_rechecked_before_reserving(db, latest):
    client, checks, _ = phantom(db)
    batch = await frozen(db)
    head = candidate_head(batch, CANDIDATE)
    await checks.refresh(head)

    async def authorize():
        # Another observation wins while candidate authorization is in flight.
        if latest == "completed":
            client.checks[0].update(status="completed", conclusion="success")
            client.jobs[0].update(status="completed", conclusion="success")
        else:
            client.checks[0]["id"] += 100
            client.jobs[0]["check_run_url"] = (
                "https://api.github.com/repos/acme/widgets/check-runs/111"
            )
        await checks.refresh(head)
        return True

    assert await checks.reconcile_stuck(batch, head, authorize=authorize) == []
    assert await records(db) == []
    client.rerun_job.assert_not_awaited()
