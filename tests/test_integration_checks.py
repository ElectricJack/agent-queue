"""Exact-commit required checks: one refreshable cache, never a stale success."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from src.database.tables import integration_check_evidence
from src.git.manager import GitError
from src.integration.checks import (
    ChecksState,
    CommitCheck,
    Conclusion,
    ExactChecks,
    HostedChecks,
    LocalChecks,
    RequiredChecks,
    evaluate,
)
from src.integration.ci_producers import HostedCIProducer, ProducerRequest
from src.integration.subjects import HeadIdentity
from tests.test_integration_ci_producers import HEAD, OTHER, JobClient, github, local

REQUIRED = RequiredChecks(version="v1", names=("unit", "lint"), producer_id="15368")


def head(sha=HEAD, repository_id="repo"):
    return HeadIdentity(
        repository_id=repository_id, ref="refs/heads/aq/integration/batch", sha=sha, generation=1
    )


def row(name, conclusion, **overrides):
    return CommitCheck(
        **{
            "repository_id": "repo",
            "sha": HEAD,
            "name": name,
            "producer_id": "15368",
            "required_check_version": "v1",
            "conclusion": conclusion,
            "classification": "conclusive",
            "observed_at": 10,
            **overrides,
        }
    )


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
async def db(reuse_database):
    return await reuse_database("checks.db")


def test_green_needs_every_required_check_to_succeed_on_the_exact_head():
    green = evaluate(
        REQUIRED, head(), [row("unit", "success"), row("lint", "success")], now=50
    )
    assert green.green and green.due_at is None

    foreign = [
        row("lint", "success"),
        row("unit", "success", sha=OTHER),
        row("unit", "success", repository_id="fork"),
        row("unit", "success", producer_id="99"),
        row("unit", "success", required_check_version="v0"),
    ]
    result = evaluate(REQUIRED, head(), foreign, now=50)
    assert result.state is ChecksState.UNKNOWN and result.due_at == 50
    assert result.checks[0].reason == "not_observed"


@pytest.mark.parametrize(
    ("conclusion", "state"),
    [
        ("failure", ChecksState.RED),
        ("missing", ChecksState.RED),
        ("pending", ChecksState.PENDING),
        ("cancelled", ChecksState.UNKNOWN),
        ("unavailable", ChecksState.UNKNOWN),
    ],
)
def test_only_success_is_green(conclusion, state):
    result = evaluate(
        REQUIRED,
        head(),
        [row("unit", "success"), row("lint", conclusion, due_at=70)],
        now=50,
    )
    assert result.state is state and not result.green
    assert result.due_at == (None if state is ChecksState.RED else 70)


def test_failure_outranks_unknown_and_pending():
    result = evaluate(
        REQUIRED, head(), [row("unit", "pending"), row("lint", "failure")], now=50
    )
    assert result.state is ChecksState.RED


def test_required_checks_resolve_from_policy_trust_and_subject_manifest():
    _, policy_trust = github(app=False)
    _, manifest = github(manifest=True)
    assert RequiredChecks.from_trust(policy_trust) == RequiredChecks(
        version="v1", names=("unit",), producer_id="15368"
    )
    assert RequiredChecks.from_trust(manifest).producer_id == "15368"


def rerun(client, *, status, conclusion):
    # The re-run's check run (id 12) moves from in_progress to completed in place.
    run = {**client.checks[0], "id": 12, "status": status, "conclusion": conclusion}
    client.checks[:] = [check for check in client.checks if check["id"] != 12] + [run]
    client.workflows[0].update(run_attempt=2, status=status, conclusion=conclusion)
    client.jobs[0].update(
        run_attempt=2,
        status=status,
        conclusion=conclusion,
        check_run_url="https://api.github.com/repos/acme/widgets/check-runs/12",
    )


def hosted(db, client, trust, clock):
    return ExactChecks(
        db, HostedChecks(HostedCIProducer(client, trust)), clock=clock, retry_seconds=300
    )


async def stored(db):
    async with db._engine.connect() as conn:
        return (
            await conn.execute(
                select(integration_check_evidence).where(
                    integration_check_evidence.c.sha.isnot(None)
                )
            )
        ).mappings().all()


async def test_hosted_success_is_cached_per_check_with_its_attempt(db):
    client, trust = github(app=False)
    checks = hosted(db, client, trust, Clock())

    assert (await checks.read(head())).state is ChecksState.UNKNOWN
    result = await checks.refresh(head())

    assert result.green
    [cached] = await stored(db)
    assert (cached["repository_id"], cached["sha"], cached["check_name"]) == ("repo", HEAD, "unit")
    assert (cached["producer_id"], cached["run_id"], cached["attempt"]) == ("15368", "31", 1)
    assert cached["run_url"] == "https://github.com/acme/widgets/actions/runs/31/attempts/1"
    assert (cached["batch_id"], cached["parent_task_id"]) == (None, None)
    client.paged_items.reset_mock()
    assert (await checks.read(head())).green
    client.paged_items.assert_not_awaited()


async def test_current_rerun_supersedes_cached_success(db):
    client, trust = github(app=False)
    clock = Clock()
    checks = hosted(db, client, trust, clock)
    assert (await checks.refresh(head())).green

    rerun(client, status="in_progress", conclusion=None)
    clock.now += 10
    pending = await checks.refresh(head())
    assert pending.state is ChecksState.PENDING and pending.due_at == 1070

    rerun(client, status="completed", conclusion="failure")
    clock.now += 10
    red = await checks.refresh(head())
    assert red.state is ChecksState.RED
    assert (red.checks[0].attempt, red.checks[0].run_id) == (2, "31")
    assert (await checks.read(head())).state is ChecksState.RED
    assert len(await stored(db)) == 1


async def test_missing_push_run_is_bounded_across_cache_reconstruction_and_recovers(db):
    client, trust = github(app=False)
    push_runs = list(client.workflows)
    client.workflows.clear()
    clock = Clock()
    checks = hosted(db, client, trust, clock)
    first = await checks.refresh(head())
    assert first.state is ChecksState.PENDING
    assert first.checks[0].detail["missing_push_since"] == 1000
    clock.now += 299
    assert (await checks.refresh(head())).state is ChecksState.PENDING

    # New lane and daemon instances reuse the same durable exact-head deadline.
    clock.now += 1
    checks = hosted(db, client, trust, clock)
    blocked = await checks.refresh(head())
    assert blocked.state is ChecksState.UNKNOWN and not blocked.green
    check = blocked.checks[0]
    assert (check.conclusion, check.classification) == (
        Conclusion.UNAVAILABLE, "ci_not_triggered",
    )
    assert HEAD in check.reason and head().ref in check.reason
    assert blocked.due_at == 1600  # Still refreshable; this is not fabricated red.
    assert (await checks.refresh(head(sha=OTHER))).state is ChecksState.PENDING

    client.workflows[:] = push_runs
    clock.now += 1
    recovered = await checks.refresh(head())
    assert recovered.green and recovered.checks[0].classification == "conclusive"
    assert "missing_push_since" not in recovered.checks[0].detail


async def test_dispatch_and_foreign_head_push_cannot_extend_missing_push_deadline(db):
    client, trust = github(app=False)
    original = dict(client.workflows[0])
    client.workflows[:] = [
        {**original, "event": "workflow_dispatch"},
        {**original, "id": 99, "head_sha": OTHER},
    ]
    clock = Clock()
    checks = hosted(db, client, trust, clock)
    assert (await checks.refresh(head())).state is ChecksState.PENDING
    clock.now += 300
    blocked = await checks.refresh(head())
    assert blocked.state is ChecksState.UNKNOWN
    assert blocked.checks[0].classification == "ci_not_triggered"
    assert not blocked.green


async def test_transport_failure_does_not_restart_missing_push_deadline(db):
    client, trust = github(app=False)
    client.workflows.clear()
    clock = Clock()
    checks = hosted(db, client, trust, clock)
    await checks.refresh(head())
    paged = client.paged_items.side_effect
    client.paged_items.side_effect = OSError("github down")
    clock.now += 200
    unavailable = await checks.refresh(head())
    assert unavailable.checks[0].classification == "infra"
    client.paged_items.side_effect = paged
    clock.now += 100
    assert (await checks.refresh(head())).checks[0].classification == "ci_not_triggered"


async def test_real_push_run_in_progress_keeps_waiting_without_trigger_blocker(db):
    client, trust = github(app=False)
    client.checks.clear()
    client.workflows[0].update(status="in_progress", conclusion=None)
    clock = Clock()
    checks = hosted(db, client, trust, clock)
    await checks.refresh(head())
    clock.now += 10000
    pending = await checks.refresh(head())
    assert pending.state is ChecksState.PENDING
    assert pending.checks[0].classification == "pending"
    assert "missing_push_since" not in pending.checks[0].detail


async def test_workflow_inspection_failure_keeps_the_named_trigger_blocker(db):
    client, trust = github(app=False)
    client.workflows.clear()
    clock = Clock()
    diagnose = AsyncMock(side_effect=GitError("candidate blob unavailable"))
    checks = ExactChecks(db, HostedChecks(HostedCIProducer(client, trust), diagnose=diagnose),
                         clock=clock)
    await checks.refresh(head())
    diagnose.assert_not_awaited()
    clock.now += 300
    blocked = await checks.refresh(head())
    assert blocked.state is ChecksState.UNKNOWN
    assert blocked.checks[0].classification == "ci_not_triggered"
    assert "workflow inspection unavailable" in blocked.checks[0].reason
    assert "candidate blob unavailable" in blocked.checks[0].reason


@pytest.mark.parametrize(
    ("job_conclusions", "state"),
    [
        # GitHub concludes a run 'failure' when no runner was acquired for its
        # jobs: the surviving job keeps its own success and the verdict waits.
        (("success", "cancelled"), ChecksState.UNKNOWN),
        (("cancelled", "success"), ChecksState.UNKNOWN),
        # A genuinely failed job still decides the verdict.
        (("failure", "cancelled"), ChecksState.RED),
        (("cancelled", "failure"), ChecksState.RED),
    ],
)
async def test_a_cancelled_job_is_not_a_run_failure(db, job_conclusions, state):
    client, trust = github(
        app=False, names=("unit", "lint"), job_conclusions=job_conclusions,
        run_conclusion="failure",
    )
    job_states = {
        "success": Conclusion.SUCCESS,
        "cancelled": Conclusion.CANCELLED,
        "failure": Conclusion.FAILURE,
    }

    result = await hosted(db, client, trust, Clock()).refresh(head())

    assert result.state is state and not result.green
    assert [check.conclusion for check in result.checks] == [
        job_states[job] for job in job_conclusions
    ]


async def test_a_failed_workflow_keeps_successful_checks_and_blocks_publication(db):
    client, trust = github(app=False, names=("unit", "lint"), run_conclusion="failure")

    result = await hosted(db, client, trust, Clock()).refresh(head())

    # The workflow still gates publication, without inventing failed jobs.
    assert result.state is ChecksState.RED
    assert [check.conclusion for check in result.checks] == [Conclusion.SUCCESS] * 2
    assert all(check.detail["workflow_conclusion"] == "failure" for check in result.checks)


async def test_mixed_hosted_workflow_caches_each_required_jobs_own_conclusion(db):
    names = (
        "Tests (migration-and-slow)",
        *(f"Tests (default-{index}/8)" for index in range(1, 9)),
        *(f"E2E CLI ({index})" for index in range(1, 5)),
        "Lint", "CLI conformance",
    )
    failures = {"Tests (migration-and-slow)", "Tests (default-3/8)",
                "Tests (default-4/8)", "Tests (default-6/8)"}
    expected = {name: "failure" if name in failures else "success" for name in names}
    client, trust = github(app=False, names=names, job_conclusions=tuple(expected.values()),
                           run_conclusion="failure")
    # A non-required failed job must not taint the required successes either.
    client.checks.append({**client.checks[-1], "id": 999, "name": "optional",
                          "conclusion": "failure"})

    checks = hosted(db, client, trust, Clock())
    result = await checks.refresh(head())

    assert result.state is ChecksState.RED
    assert {check.name: str(check.conclusion) for check in result.checks} == expected
    assert {cached["check_name"]: cached["conclusion"] for cached in await stored(db)} == expected
    assert (await checks.read(head())).checks == result.checks
    listings = [call.kwargs["key"] for call in client.paged_items.await_args_list]
    assert listings.count("check_runs") == 1


@pytest.mark.parametrize(
    ("change", "state", "conclusion"),
    [
        ("cancelled", ChecksState.UNKNOWN, Conclusion.CANCELLED),
        ("missing", ChecksState.RED, Conclusion.MISSING),
        ("unavailable", ChecksState.UNKNOWN, Conclusion.UNAVAILABLE),
        ("repository", ChecksState.UNKNOWN, Conclusion.UNAVAILABLE),
        ("pending", ChecksState.PENDING, Conclusion.PENDING),
    ],
)
async def test_unfinished_or_untrusted_refresh_replaces_cached_success(
    db, change, state, conclusion
):
    client, trust = github(app=False)
    clock = Clock()
    checks = hosted(db, client, trust, clock)
    assert (await checks.refresh(head())).green
    clock.now += 10

    if change == "cancelled":
        rerun(client, status="completed", conclusion="cancelled")
    elif change == "missing":
        client.checks.clear()
        client.workflows[0].update(run_attempt=2)
    elif change == "unavailable":
        client.paged_items.side_effect = OSError("github down")
    elif change == "pending":
        client.checks.clear()
        client.workflows[0].update(run_attempt=2, status="in_progress", conclusion=None)
    if change == "repository":
        # The cache row for the trusted repository is untouched; the foreign
        # repository's head is never green.
        result = await checks.refresh(head(repository_id="fork"))
        assert (await checks.read(head())).green
    else:
        result = await checks.refresh(head())

    assert result.state is state and not result.green
    assert result.checks[0].conclusion is conclusion
    if state is ChecksState.UNKNOWN:
        assert result.due_at == 1310


@pytest.mark.parametrize("mode", ["shadow", "active"])
async def test_root_reader_refreshes_the_commit_cache_only_when_active(db, monkeypatch, mode):
    from src.git.github_contracts import GitHubRepositoryBinding
    from src.integration.root_runtime import root_candidate_ci_reader
    from src.integration.subjects import CIState

    client, trust = github(app=False)
    monkeypatch.setattr("src.integration.observe._required", lambda _snapshot: {})
    owner = SimpleNamespace(
        config=SimpleNamespace(integration=SimpleNamespace(git_first=mode)),
        db=db,
        github_repository_binding_resolver=AsyncMock(
            return_value=GitHubRepositoryBinding(123, "acme/widgets")
        ),
        integration_attestation_service=SimpleNamespace(
            _load_trust=AsyncMock(return_value=(trust, client))
        ),
    )
    snapshot = SimpleNamespace(subject=SimpleNamespace(project_id="p", batch_id="b", id="s"))
    read = root_candidate_ci_reader(owner)

    evidence = await read(snapshot, head())
    assert evidence.state is CIState.GREEN and evidence.head_sha == HEAD
    # Shadow keeps the legacy read; active writes only the per-commit cache.
    assert [row["check_name"] for row in await stored(db)] == (
        ["unit"] if mode == "active" else []
    )

    client.paged_items.side_effect = OSError("github down")
    evidence = await read(snapshot, head())
    assert evidence.state is not CIState.GREEN
    if mode == "active":
        assert evidence.state is CIState.INFRA
        assert [row["conclusion"] for row in await stored(db)] == ["unavailable"]


async def test_older_observation_never_overwrites_a_newer_one(db):
    class Provider:
        required = RequiredChecks(version="v1", names=("unit",), producer_id="p")

        def __init__(self):
            self.next = []

        async def request(self, _head):
            return ProducerRequest(outcome="already_running")

        async def observe(self, observed_head, *, now):
            conclusion, run_id, attempt = self.next.pop(0)
            return (
                row(
                    "unit", conclusion, producer_id="p", run_id=run_id, attempt=attempt,
                    observed_at=now,
                ),
            )

    clock, provider = Clock(), Provider()
    checks = ExactChecks(db, provider, clock=clock)
    provider.next.append(("failure", "31", 2))
    assert (await checks.refresh(head())).state is ChecksState.RED

    # An earlier observation finishing late, or an earlier attempt, never wins.
    clock.now -= 5
    provider.next.append(("success", "31", 1))
    assert (await checks.refresh(head())).state is ChecksState.RED
    clock.now += 10
    provider.next.append(("success", "31", 1))
    assert (await checks.refresh(head())).state is ChecksState.RED
    provider.next.append(("success", "32", 1))
    assert (await checks.refresh(head())).green


async def test_database_guard_keeps_run_attempt_rows_append_only(db):
    provider = SimpleNamespace(
        required=RequiredChecks(version="v1", names=("unit",), producer_id="p"),
        request=None,
        observe=AsyncMock(return_value=(row("unit", "success", producer_id="p"),)),
    )
    await ExactChecks(db, provider, clock=Clock()).refresh(head())
    legacy = (
        "INSERT INTO integration_check_evidence (id, parent_task_id, parent_generation, "
        "parent_head_sha, producer_id, workflow_id, run_id, attempt, required_check_version, "
        "checks, conclusion, classification, observed_at) VALUES ('legacy', 'parent', 1, "
        f"'{HEAD}', 'p', 'w', 'r', 1, 'v1', '{{}}', 'success', 'conclusive', 1)"
    )
    async with db._engine.begin() as conn:
        await conn.execute(text(legacy))

    refused = [
        ("UPDATE integration_check_evidence SET conclusion = 'failure' WHERE id = 'legacy'",
         "append-only"),
        ("DELETE FROM integration_check_evidence WHERE id = 'legacy'", "append-only"),
        (f"UPDATE integration_check_evidence SET sha = '{OTHER}' WHERE sha IS NOT NULL",
         "identity is immutable"),
        ("UPDATE integration_check_evidence SET observed_at = 1 WHERE sha IS NOT NULL",
         "cannot move backwards"),
    ]
    for statement, message in refused:
        with pytest.raises(DBAPIError, match=message):
            async with db._engine.begin() as conn:
                await conn.execute(text(statement))

    # A cache row can be pruned; the next refresh observes it again.
    async with db._engine.begin() as conn:
        await conn.execute(text("DELETE FROM integration_check_evidence WHERE sha IS NOT NULL"))
    assert await stored(db) == []


async def test_slow_provider_holds_no_connection_lock_or_transaction(db):
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowProvider:
        required = RequiredChecks(version="v1", names=("unit",), producer_id="p")

        async def request(self, _head):
            return ProducerRequest(outcome="already_running")

        async def observe(self, observed_head, *, now):
            entered.set()
            await release.wait()
            return (row("unit", "success", producer_id="p", observed_at=now),)

    refresh = asyncio.create_task(ExactChecks(db, SlowProvider()).refresh(head()))
    await asyncio.wait_for(entered.wait(), 10)
    try:
        async with db._engine.connect() as conn:
            busy = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity a "
                    "LEFT JOIN pg_locks l ON l.pid = a.pid AND l.locktype = 'advisory' "
                    "WHERE a.datname = current_database() AND a.pid <> pg_backend_pid() "
                    "AND (l.pid IS NOT NULL OR a.state LIKE 'idle in transaction%')"
                )
            )
        assert busy == 0
    finally:
        release.set()
    assert (await refresh).green


async def test_local_jobs_bind_the_exact_candidate_and_required_commands(db):
    client = JobClient()
    producer = local(client, commands=("npm ci", "npm run build"))
    checks = ExactChecks(db, LocalChecks(producer, project_id="p"), clock=Clock())
    assert checks.required.names == ("npm ci", "npm run build")

    assert (await checks.refresh(head())).state is ChecksState.PENDING
    assert (await checks.request(head())).outcome == "requested"
    [call] = client.calls
    assert call["input_ref"] == HEAD and call["operation_id"].startswith("checks:")
    client.complete(0)
    assert (await checks.request(head())).outcome == "requested"
    client.complete(1)

    result = await checks.refresh(head())
    assert result.green
    assert [check.run_id for check in result.checks] == ["job-0", "job-1"]

    # Another candidate SHA has its own jobs; a finished head never serves it.
    other = await checks.refresh(head(sha=OTHER))
    assert other.state is ChecksState.PENDING
    assert all(check.reason == "not_requested" for check in other.checks)


async def test_local_result_for_another_snapshot_is_not_green(db):
    client = JobClient()
    checks = ExactChecks(db, LocalChecks(local(client), project_id="p"), clock=Clock())
    await checks.request(head())
    client.complete(0, input_ref=OTHER)

    result = await checks.refresh(head())
    assert result.state is ChecksState.UNKNOWN
    assert result.checks[0].reason == "result_identity_mismatch"


async def test_local_cancelled_job_is_unknown_not_green(db):
    client = JobClient()
    checks = ExactChecks(db, LocalChecks(local(client), project_id="p"), clock=Clock())
    await checks.request(head())
    client.complete(0, exit_code=-15, cancelled=True, input_stability="unverified")

    result = await checks.refresh(head())
    assert result.state is ChecksState.UNKNOWN
    assert result.checks[0].conclusion is Conclusion.CANCELLED


async def test_pr_checks_are_separate_from_push_cache_and_share_one_listing_per_visit(db):
    from src.integration.ci import hosted_observation_scope

    client, trust = github(app=False, names=("unit",))
    # No PR run exists: green push CI cannot satisfy PR admission.
    push = ExactChecks(db, HostedChecks(HostedCIProducer(client, trust)))
    pr = ExactChecks(db, HostedChecks(HostedCIProducer(client, trust, expected_event="pull_request")))
    listed = []
    original = client.paged_items

    async def listing(path, *, key):
        listed.append(key)
        return await original(path, key=key)

    client.paged_items = listing
    with hosted_observation_scope():
        assert (await push.refresh(head())).green
        assert (await pr.refresh(head())).state is ChecksState.PENDING
    assert listed.count("check_runs") == 1
    assert listed.count("workflow_runs") == 1
    assert (await push.read(head())).green
    assert (await pr.read(head())).required.producer_id == "15368:pull_request"
    with hosted_observation_scope():
        await push.refresh(head())
    assert listed.count("check_runs") == 2
