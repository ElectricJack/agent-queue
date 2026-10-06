"""Shared exact-head facts, replay, App trust and detached validation isolation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.git.github_contracts import GitHubAccessError, GitHubCredentialIdentity
from src.integration.ci import IntegrationCITrust, IntegrationTrustManifest
from src.integration.ci_adapters import CIAdapters, bind_ci_adapters
from src.integration.ci_producers import (
    HostedCIProducer,
    LocalCIProducer,
    LocalValidationPlan,
    ProducerObservation,
    ProducerRequest,
    digest,
)
from src.integration.main_promotion import RootAttestationProof, RootAttestationSubject
from src.integration.subjects import (
    CIAttestArgs,
    CIObserveArgs,
    CIRequestArgs,
    CIState,
    Primitive,
    PrimitivePorts,
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


class SubjectDB:
    def __init__(self, current):
        self.row = current.to_row()
        self.entries = {}
        self._engine = self

    @asynccontextmanager
    async def begin(self):
        yield self

    async def get_integration_subject(self, _):
        return deepcopy(self.row)

    async def lock_integration_subject_on(self, *_):
        return deepcopy(self.row)

    async def append_integration_subject_journal_on(self, _, values):
        key = values["idempotency_key"]
        created = key not in self.entries
        if created:
            self.entries[key] = {**deepcopy(values), "seq": len(self.entries) + 1}
        return self.entries[key], created


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


async def test_adapters_register_all_ci_ports_and_journal_replays_do_not_count_attempts():
    client, s = JobClient(), subject()
    db, producer = SubjectDB(s), local(client)
    adapters = CIAdapters(db, lambda _: producer, clock=lambda: 1002)
    ports = bind_ci_adapters(PrimitivePorts(), adapters)
    assert ports.bound == {Primitive.CI_REQUEST, Primitive.CI_OBSERVE, Primitive.CI_ATTEST}
    await ports.invoke(s, CIRequestArgs(head=s.head))
    client.complete()
    one = await ports.invoke(s, CIObserveArgs(head=s.head, required_check_version="v1"))
    two = await ports.invoke(s, CIObserveArgs(head=s.head, required_check_version="v1"))
    assert one == two and one.outcome == "green" and one.detail["is_attempt"]
    assert len(db.entries) == 2
    assert all(entry["entry_kind"] == "action" for entry in db.entries.values())
    assert (await ports.invoke(s, CIAttestArgs(head=s.head))).outcome == "refused"


async def test_observation_age_advances_without_appending_another_attempt():
    client, s = JobClient(), subject()
    producer, db = local(client), SubjectDB(s)
    await producer.request(s, s.head)
    times = iter((1010, 1050))
    producer.clock = lambda: next(times)
    adapters = CIAdapters(db, lambda _: producer, clock=lambda: 1002)
    first = await adapters.observe(s, CIObserveArgs(head=s.head))
    second = await adapters.observe(s, CIObserveArgs(head=s.head))
    assert first.detail["evidence"]["age_seconds"] == 9
    assert second.detail["evidence"]["age_seconds"] == 49
    assert first.detail["evidence"]["evidence_id"] == second.detail["evidence"]["evidence_id"]
    assert first.detail["journal_seq"] == second.detail["journal_seq"]
    assert len(db.entries) == 1 and not second.detail["is_attempt"]


async def test_request_replay_preserves_original_requested_at():
    s = subject()
    client, trust = github(app=False)
    producer = HostedCIProducer(
        client,
        trust,
        requester=AsyncMock(
            return_value=ProducerRequest(
                outcome="requested",
                request_id="durable-dispatch",
            )
        ),
    )
    times = iter((1000, 1001, 1100, 1101))
    db = SubjectDB(s)
    adapters = CIAdapters(db, lambda _: producer, clock=lambda: next(times))
    first = await adapters.request(s, CIRequestArgs(head=s.head))
    second = await adapters.request(s, CIRequestArgs(head=s.head))
    assert first.detail["requested_at"] == second.detail["requested_at"] == 1000
    assert len(db.entries) == 1


@pytest.mark.parametrize("change", ["version", "head", "generation", "legacy", "gate", "done"])
async def test_adapter_refuses_stale_feature_off_and_human_held_subject_without_work(change):
    s, client = subject(), JobClient()
    db, producer = SubjectDB(s), local(client)
    db.row.update(
        {
            "version": {"version": 1},
            "head": {"head_sha": OTHER},
            "generation": {"generation": 2},
            "legacy": {"engine": "legacy"},
            "gate": {"gate_id": "human", "next_due_at": None},
            "done": {"phase": "done", "next_due_at": None, "closed_reason": "delivered"},
        }[change]
    )
    resolver = AsyncMock(return_value=producer)
    adapters = CIAdapters(db, resolver)
    for args, call in [
        (CIRequestArgs(head=s.head), adapters.request),
        (CIObserveArgs(head=s.head), adapters.observe),
        (CIAttestArgs(head=s.head), adapters.attest),
    ]:
        result = await call(s, args)
        assert result.detail["classification"] == "superseded"
        assert not result.detail["is_attempt"]
    resolver.assert_not_awaited()
    assert not client.calls and not db.entries


async def test_late_green_is_superseded_before_evidence_append():
    s, client = subject(), JobClient()
    db, producer = SubjectDB(s), local(client)
    await producer.request(s, s.head)
    client.complete()
    observe = producer.observe

    async def moved(*args):
        result = await observe(*args)
        db.row.update(head_sha=OTHER, generation=2, version=1)
        return result

    producer.observe = moved
    result = await CIAdapters(db, lambda _: producer).observe(s, CIObserveArgs(head=s.head))
    assert result.outcome == "infra" and result.detail["classification"] == "superseded"
    assert not db.entries


async def test_required_check_version_mismatch_cannot_observe():
    s, client = subject(), JobClient()
    producer = local(client)
    producer.observe = AsyncMock()
    result = await CIAdapters(SubjectDB(s), lambda _: producer).observe(
        s, CIObserveArgs(head=s.head, required_check_version="wrong")
    )
    assert result.outcome == "untrusted"
    producer.observe.assert_not_awaited()


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        ("published", "attested"),
        ("already_published", "already"),
        ("configuration_blocked", "refused"),
    ],
)
async def test_attestation_delegates_to_existing_journaled_service(published, expected):
    s = subject()
    client, trust = github(manifest=True)
    target = RootAttestationSubject(
        repository_numeric_id=123,
        repository_full_name="acme/widgets",
        operation_id="op",
        batch_id="batch",
        revision=1,
        candidate_sha=HEAD,
        required_check_version="v1",
    )
    proof = RootAttestationProof(
        **target.model_dump(), check_run_id=77, external_id="aq-attestation-v1:" + "2" * 64
    )
    service = SimpleNamespace(
        publish=AsyncMock(return_value=SimpleNamespace(outcome=published, proof=proof))
    )
    adapters = CIAdapters(
        SubjectDB(s),
        lambda _: HostedCIProducer(client, trust),
        attestation_service=service,
        attestation_subject_for=lambda _: target,
    )
    result = await adapters.attest(s, CIAttestArgs(head=s.head))
    assert result.outcome == expected
    service.publish.assert_awaited_once_with(target)


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
async def ci_producer_db():
    from sqlalchemy import insert
    from src.database import Database
    from src.database.tables import playbook_artifacts
    from tests.db_fixtures import lease_dsn

    db = Database(lease_dsn("ci-producers"))
    await db.initialize()
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
    await db.close()


async def test_real_journal_replay_is_append_only_and_never_counts_budget(ci_producer_db):
    s, client = subject(batch_id=None), JobClient()
    await ci_producer_db.ensure_integration_subject(s.to_row())
    producer = local(client)
    await producer.request(s, s.head)
    client.complete()
    adapters = CIAdapters(ci_producer_db, lambda _: producer, clock=lambda: 1002)
    first = await adapters.observe(s, CIObserveArgs(head=s.head))
    second = await adapters.observe(s, CIObserveArgs(head=s.head))
    entries = await ci_producer_db.list_integration_subject_journal(s.id)
    assert first == second and len(entries) == 1
    assert entries[0]["head_sha"] == HEAD and entries[0]["generation"] == s.generation
    row = await ci_producer_db.get_integration_subject(s.id)
    assert row["version"] == s.version and row["budget_attempts"] == 0


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
