"""Shared exact-head facts, replay, App trust and detached validation isolation."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.git.github_contracts import GitHubAccessError, GitHubCredentialIdentity
from src.integration.ci import IntegrationCITrust, IntegrationTrustManifest
from src.integration.ci_producers import (
    HostedCIProducer,
    LocalCIProducer,
    LocalValidationPlan,
    ProducerObservation,
    ProducerRequest,
    digest,
)
from src.integration.runtime_contracts import (
    CIState,
    Subject,
    SubjectSchedule,
)
from src.jobs.policy import JobError
from src.jobs.result import build_result

HEAD, OTHER = "a" * 40, "b" * 40


def subject(**overrides):
    return Subject(
        **{
            "id": "s",
            "project_id": "p",
            "repository_id": "repo",
            "kind": "root_batch",
            "subject_key": "root_batch:repo:batch",
            "engine": "reconciler",
            "phase": "testing",
            "batch_id": "batch",
            "target_ref": "refs/heads/aq/integration/batch",
            "head_sha": HEAD,
            "generation": 1,
            "policy": {"playbook_id": "development", "artifact_sha256": "sha256:" + "1" * 64},
            "schedule": SubjectSchedule.progress(now=1000, max_wait_seconds=3600),
            "created_at": 1000,
            "updated_at": 1000,
            **overrides,
        }
    )


class JobClient:
    def __init__(self):
        self.rows, self.calls = {}, []

    async def read(self, _, keys):
        return {key: deepcopy(row) for key, row in self.rows.items() if key in keys}

    async def submit(self, **args):
        key = args["idempotency_key"]
        self.calls.append(args)
        if key not in self.rows:
            self.rows[key] = {
                "id": f"job-{len(self.rows)}",
                "preset": args["preset"],
                "state": "queued",
                "owner_kind": "integration",
                "owner_id": args["operation_id"],
                "project_id": args["project_id"],
                "input_mode": "snapshot",
                "input_ref": args["input_ref"],
                "submitted_at": 1001,
                "result": None,
            }
        return self.rows[key]

    def complete(self, index=0, *, exit_code=0, **receipt):
        row = list(self.rows.values())[index]
        row["result"] = build_result(
            row,
            {
                "exit_code": exit_code,
                "input_ref": row["input_ref"],
                "input_stability": "stable",
                **receipt,
            },
        )
        row["state"] = row["result"]["state"]
        return row


def local(client, **plan):
    return LocalCIProducer(
        None,
        client,
        store="/retained/publisher",
        read_jobs=client.read,
        clock=lambda: 1002,
        plan=LocalValidationPlan(
            version="v1",
            attempt_id="attempt-0",
            **{
                "commands": ("ruff check src",),
                **plan,
            },
        ),
    )


def github(
    *,
    conclusion="success",
    status="completed",
    app=True,
    manifest=False,
    names=("unit",),
    job_conclusions=None,
    run_conclusion=None,
):
    """One hosted attempt: every required name is a job, and the run concludes
    on its own. ``job_conclusions`` and ``run_conclusion`` model a run whose
    jobs disagree with the conclusion GitHub derives from them."""
    jobs = list(job_conclusions or (conclusion,) * len(names))
    trust = dict(
        canonical_repository_id="repo",
        repository_id=123,
        full_name="acme/widgets",
        required_checks={"version": "v1", "names": tuple(names)},
    )
    trusted = (
        IntegrationTrustManifest(
            **trust,
            schema="aq.integration-trust.v1",
            ci_producer_app_id=15368,
            attestation_app_id=42,
            attestation_name="Agent Queue Integration Attestation",
        )
        if manifest
        else IntegrationCITrust(**trust, producer_id="15368")
    )
    client = SimpleNamespace(
        credential_identity=GitHubCredentialIdentity.app(42)
        if app
        else GitHubCredentialIdentity.existing_login(),
        checks=[
            {
                "id": 11 + index,
                "name": name,
                "head_sha": HEAD,
                "status": status,
                "conclusion": job,
                "app": {"id": 15368},
                "check_suite": {"id": 21},
            }
            for index, (name, job) in enumerate(zip(names, jobs))
        ],
        workflows=[
            {
                "id": 31,
                "workflow_id": 301,
                "run_attempt": 1,
                "check_suite_id": 21,
                "head_sha": HEAD,
                "status": status,
                "conclusion": conclusion if run_conclusion is None else run_conclusion,
                "event": "push",
                "repository": {"id": 123, "full_name": "acme/widgets"},
                "head_repository": {"id": 123, "full_name": "acme/widgets"},
            }
        ],
        jobs=[
            {
                "id": 51 + index,
                "name": name,
                "run_id": 31,
                "run_attempt": 1,
                "head_sha": HEAD,
                "status": status,
                "conclusion": job,
                "check_run_url": (
                    f"https://api.github.com/repos/acme/widgets/check-runs/{11 + index}"
                ),
            }
            for index, (name, job) in enumerate(zip(names, jobs))
        ],
    )

    async def paged_items(_, *, key):
        return {
            "check_runs": client.checks,
            "workflow_runs": client.workflows,
            "jobs": client.jobs,
        }[key]

    client.paged_items = AsyncMock(side_effect=paged_items)
    return client, trusted


@pytest.mark.parametrize("manifest", [False, True])
@pytest.mark.parametrize(
    ("conclusion", "status", "state", "classification"),
    [
        ("success", "completed", "green", "conclusive"),
        ("failure", "completed", "red", "conclusive"),
        ("cancelled", "completed", "infra", "cancelled"),
        ("neutral", "completed", "infra", "infra"),
        ("skipped", "completed", "infra", "infra"),
        ("timed_out", "completed", "infra", "infra"),
        (None, "in_progress", "pending", "pending"),
    ],
)
async def test_hosted_normalizes_authenticated_exact_head(
    manifest, conclusion, status, state, classification
):
    client, trust = github(conclusion=conclusion, status=status, app=manifest, manifest=manifest)
    producer = HostedCIProducer(client, trust)
    s = subject()
    result = await producer.observe(s, s.head)
    assert isinstance(result, ProducerObservation)
    assert (result.state, result.classification) == (state, classification)
    assert result.is_attempt == (classification == "conclusive")
    assert result.head_sha == HEAD and result.required_check_version == "v1"


@pytest.mark.parametrize(
    ("job_conclusions", "run_conclusion", "state", "classification"),
    [
        # A real failure is conclusive whatever its cancelled siblings say.
        (("failure", "cancelled"), "failure", "red", "conclusive"),
        (("cancelled", "failure"), "failure", "red", "conclusive"),
        (("success", "failure"), "failure", "red", "conclusive"),
        # GitHub concludes a run 'failure' when no runner was acquired for its
        # jobs, so an outage retries instead of opening a repair.
        (("success", "cancelled"), "failure", "infra", "cancelled"),
        (("cancelled", "success"), "failure", "infra", "cancelled"),
        (("cancelled", "cancelled"), "failure", "infra", "cancelled"),
    ],
)
async def test_hosted_classifies_the_jobs_before_the_run_conclusion(
    job_conclusions, run_conclusion, state, classification
):
    client, trust = github(
        app=False, names=("unit", "lint"), job_conclusions=job_conclusions,
        run_conclusion=run_conclusion,
    )
    s = subject()

    result = await HostedCIProducer(client, trust).observe(s, s.head)

    assert (result.state, result.classification) == (state, classification)
    assert result.is_attempt is (classification == "conclusive")
    assert dict(zip(("unit", "lint"), job_conclusions)) == result.checks


async def test_app_credentials_cannot_replace_subject_manifest_with_policy_only_trust():
    client, trust = github()
    requester = AsyncMock()
    producer = HostedCIProducer(client, trust, requester=requester)
    s = subject()
    assert (await producer.observe(s, s.head)).state is CIState.UNTRUSTED
    assert (await producer.request(s, s.head)).outcome == "unavailable"
    client.paged_items.assert_not_awaited()
    requester.assert_not_awaited()


@pytest.mark.parametrize("manifest", [False, True])
@pytest.mark.parametrize("aggregate", [None, "failure", "cancelled", "timed_out"])
async def test_hosted_required_job_succeeds_independently_of_unrequired_jobs(manifest, aggregate):
    client, trust = github(app=manifest, manifest=manifest)
    client.workflows[0].update(
        status="queued" if aggregate is None else "completed", conclusion=aggregate,
    )
    client.checks.append({**client.checks[0], "id": 99, "name": "waived-windows",
                          "status": "queued", "conclusion": None})
    client.jobs.append({**client.jobs[0], "id": 99, "name": "waived-windows",
                       "status": "queued", "conclusion": None})
    s = subject()
    result = await HostedCIProducer(client, trust).observe(s, s.head)
    assert result.state is CIState.GREEN
    assert result.checks == {"unit": "success"}
    assert result.details["receipt"]["workflow_runs"][0]["conclusion"] == aggregate
    assert result.details["checks"][0]["job_url"].endswith("/actions/runs/31/job/51")
    assert "job_url" not in result.details["receipt"]["checks"][0]

    # A workflow rerun cannot borrow the previous attempt's successful job.
    client.workflows[0]["run_attempt"] = 2
    result = await HostedCIProducer(client, trust).observe(s, s.head)
    assert result.state is not CIState.GREEN
    assert result.classification == "superseded"


@pytest.mark.parametrize("change", ["head", "app", "newer-pending", "older-attempt"])
async def test_hosted_never_uses_wrong_head_foreign_app_or_superseded_success(change):
    client, trust = github(app=False)
    if change == "head":
        client.checks[0]["head_sha"] = OTHER
    elif change == "app":
        client.checks[0]["app"] = {"id": 999}
    elif change == "newer-pending":
        client.checks.append({**client.checks[0], "id": 12, "status": "queued", "conclusion": None})
    else:
        client.workflows[0]["run_attempt"] = 2
        client.jobs[0].update(
            run_attempt=2, check_run_url="https://api.github.com/repos/acme/widgets/check-runs/12"
        )
    s = subject()
    observed = await HostedCIProducer(client, trust).observe(s, s.head)
    assert observed.state is not CIState.GREEN
    if change == "older-attempt":
        assert observed.classification == "superseded" and not observed.is_attempt


async def test_hosted_pending_absence_and_transport_failure_are_distinct():
    client, trust = github(app=False)
    client.checks, client.workflows = [], []
    s = subject()
    producer = HostedCIProducer(client, trust)
    assert (await producer.observe(s, s.head)).classification == "none"
    client.paged_items.side_effect = OSError("network")
    assert (await producer.observe(s, s.head)).classification == "infra"


async def test_hosted_rate_limit_preserves_retry_after_for_train_pause():
    client, trust = github(app=False)
    error = GitHubAccessError("rate_limited", "secondary limit", retry_at=1900, http_status=403)
    client.paged_items.side_effect = error
    with pytest.raises(GitHubAccessError) as caught:
        await HostedCIProducer(client, trust).observe(subject(), subject().head)
    assert caught.value is error and caught.value.retry_at == 1900




async def test_hosted_request_uses_existing_owner_without_running_git():
    client, trust = github(app=False)
    requested = ProducerRequest(
        outcome="requested", request_id="journaled-publication", requested_at=1001
    )
    requester = AsyncMock(return_value=requested)
    producer = HostedCIProducer(client, trust, requester=requester)
    s = subject()
    assert await producer.request(s, s.head) == requested
    requester.assert_awaited_once_with(s, s.head)
    assert (await HostedCIProducer(client, trust).request(s, s.head)).outcome == "unavailable"


async def test_local_replay_restart_and_sequential_commands_share_detached_snapshot(monkeypatch):
    import asyncio

    spawn = AsyncMock(side_effect=AssertionError("producer must not spawn"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    client, s = JobClient(), subject()
    commands = ("npm ci", "npm run build")
    producer = local(client, commands=commands)
    assert (await producer.observe(s, s.head)).state is CIState.NONE
    assert (await producer.request(s, s.head)).outcome == "requested"
    assert len(client.calls) == 1 and client.calls[0]["preset"] == "npm_ci"
    # A new object stands in for daemon/caller restart: durable lookup owns replay.
    producer = local(client, commands=commands)
    assert (await producer.request(s, s.head)).outcome == "already_running"
    assert (await producer.observe(s, s.head)).state is CIState.PENDING
    assert len(client.calls) == 1
    client.complete()
    assert (await producer.observe(s, s.head)).state is CIState.PENDING
    assert (await producer.request(s, s.head)).outcome == "requested"
    assert client.calls[0]["snapshot_group"] == client.calls[1]["snapshot_group"]
    assert all(call["input_ref"] == HEAD and call["operation_id"] == s.id for call in client.calls)
    client.complete(1)
    observed = await producer.observe(s, s.head)
    assert observed.state is CIState.GREEN and observed.is_attempt
    assert (await producer.observe(s, s.head)).evidence_id == observed.evidence_id
    assert (await producer.request(s, s.head)).reason == "validation_complete"
    assert len(client.calls) == 2
    spawn.assert_not_awaited()


@pytest.mark.parametrize(
    ("code", "receipt", "state", "classification"),
    [
        (0, {}, "green", "conclusive"),
        (1, {}, "red", "conclusive"),
        (-15, {"cancelled": True, "input_stability": "unverified"}, "infra", "cancelled"),
        (0, {"infra_reason": "run_timeout"}, "infra", "infra"),
        (0, {"input_stability": "modified"}, "infra", "infra"),
        (0, {"input_ref": OTHER}, "untrusted", "untrusted"),
        (5, {}, "red", "conclusive"),
    ],
)
async def test_local_classification_retains_cancellation_and_failed_producer(
    code, receipt, state, classification
):
    client, s = JobClient(), subject()
    producer = local(client)
    await producer.request(s, s.head)
    client.complete(exit_code=code, **receipt)
    observed = await producer.observe(s, s.head)
    assert (observed.state, observed.classification) == (state, classification)
    assert observed.is_attempt == (classification == "conclusive")
    assert len(client.calls) == 1


@pytest.mark.parametrize("change", ["result-hash", "passed-nonzero", "failed-zero", "live", "lost"])
async def test_local_integrity_and_observed_outcome_are_authoritative(change):
    client, s = JobClient(), subject()
    producer = local(client)
    await producer.request(s, s.head)
    row = client.complete()
    if change == "result-hash":
        row["result"]["result_hash"] = "forged"
    elif change == "live":
        row["input_mode"] = "live"
    else:
        row["result"].update(
            {
                "passed-nonzero": {"exit_code": 1},
                "failed-zero": {"outcome": "failed", "state": "failed"},
                "lost": {"outcome": "lost", "state": "lost", "input_stability": "unverified"},
            }[change]
        )
        row["state"] = row["result"]["state"]
        row["result"]["result_hash"] = digest(
            {k: v for k, v in row["result"].items() if k != "result_hash"}
        )
    observed = await producer.observe(s, s.head)
    assert observed.state is not CIState.GREEN
    if change == "failed-zero":
        assert observed.state is CIState.RED
    if change == "lost":
        assert observed.classification == "infra"


async def test_local_empty_disabled_and_unsupported_plan_never_submit():
    client, s = JobClient(), subject()
    assert (await local(client, commands=()).observe(s, s.head)).state is CIState.NONE
    assert (await local(client, commands=()).request(s, s.head)).outcome == "unavailable"
    producer = local(client, commands=("ruff check src", "echo unsafe"))
    assert (await producer.request(s, s.head)).reason == "jobs.preset_denied"
    producer = local(client)
    producer.job_client = None
    assert (await producer.request(s, s.head)).reason == "jobs.disabled"
    client.submit = AsyncMock(side_effect=JobError("jobs.disabled"))
    assert (await local(client).request(s, s.head)).outcome == "unavailable"
    assert not client.calls


async def test_local_request_identity_binds_plan_head_generation_policy_and_attempt():
    client, s = JobClient(), subject()
    producer = local(client)
    original = producer.request_identity(s, s.head)
    for changed in [subject(head_sha=OTHER), subject(generation=2)]:
        assert producer.request_identity(changed, changed.head) != original
    assert local(client, commands=("ruff check tests",)).request_identity(s, s.head) != original
    retry = LocalCIProducer(
        None,
        client,
        store="/retained",
        plan=producer.plan.model_copy(update={"attempt_id": "retry"}),
    )
    assert retry.request_identity(s, s.head) != original
















def test_typed_green_cannot_claim_failure_cancellation_or_untrusted_evidence():
    for overrides in [
        {"trusted": False},
        {"classification": "cancelled"},
        {"checks": {"unit": "failure"}},
    ]:
        with pytest.raises(ValidationError):
            ProducerObservation(
                **{
                    "head_sha": HEAD,
                    "state": "green",
                    "classification": "conclusive",
                    "trusted": True,
                    "evidence_id": "id",
                    "checks": {"unit": "success"},
                    "required_check_version": "v1",
                    **overrides,
                }
            )


@pytest.fixture
async def ci_producer_db(reuse_database):
    from sqlalchemy import insert
    from src.database.tables import playbook_artifacts

    db = await reuse_database("ci-producers")
    pin = subject().policy
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                artifact_sha256=pin.artifact_sha256,
                playbook_id=pin.playbook_id,
                source_digest="sha256:" + "c" * 64,
                contract_fingerprint="sha256:" + "d" * 64,
                compiler_build="test",
                path="/artifacts/ci-producers.json",
                created_at=1000,
            )
        )
    yield db




async def test_real_internal_submission_detaches_from_production_clone(ci_producer_db, tmp_path):
    from pathlib import Path
    from src.commands.job_commands import JobCommandsMixin
    from src.config import AppConfig
    from src.git.manager import GitManager
    from src.jobs.adapters import PublisherJobs
    from src.jobs.service import JobService
    from src.models import Project

    store = tmp_path / "publisher"
    store.mkdir()
    (store / "sample.py").write_text("value = 1\n")
    git = GitManager()
    await git._arun(["init"], cwd=str(store))
    await git._arun(["add", "sample.py"], cwd=str(store))
    await git._arun(
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "snapshot",
        ],
        cwd=str(store),
    )
    head = (await git._arun(["rev-parse", "HEAD"], cwd=str(store))).strip()
    await ci_producer_db.create_project(Project(id="p", name="Fixture"))
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.resources.jobs.enabled = True

    class Handler(JobCommandsMixin):
        def _jobs(self):
            return self.service

    handler = Handler()
    handler.db, handler.config = ci_producer_db, config
    handler.service = JobService(ci_producer_db, config)
    s = subject(head_sha=head)
    producer = LocalCIProducer(
        ci_producer_db,
        PublisherJobs(handler),
        store=store,
        plan=LocalValidationPlan(
            version="v1", attempt_id="one", commands=("ruff check sample.py",)
        ),
    )
    requested = await producer.request(s, s.head)
    assert requested.outcome == "requested"
    job = await ci_producer_db.get_job(requested.job_ids[0])
    snapshot = Path(job["contract"]["cwd"])
    assert snapshot != store and snapshot.parent == Path(config.data_dir) / "job-snapshots"
    workspace = await ci_producer_db.get_workspace(job["workspace_id"])
    assert workspace.kind_id == "job-snapshot" and not workspace.enabled
    assert job["input_mode"] == "snapshot" and job["input_ref"] == head
    assert job["owner_kind"] == "integration" and job["owner_id"] == s.id
    assert (await git._arun(["symbolic-ref", "-q", "HEAD"], cwd=str(store))).strip()
    # Even a validation tool that writes its input cannot reach the production
    # clone's tracked file or index through the runner's working directory.
    (snapshot / "sample.py").write_text("value = 2\n")
    assert (store / "sample.py").read_text() == "value = 1\n"
    assert not (await git._arun(["status", "--porcelain"], cwd=str(store))).strip()
    replay = await producer.request(s, s.head)
    assert replay.outcome == "already_running" and replay.job_ids == requested.job_ids
    assert (await producer.observe(s, s.head)).state is CIState.PENDING
