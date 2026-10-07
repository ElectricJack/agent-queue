"""The train's local jobs provider: development checks of exact candidates.

A development lane validates each root candidate with resource jobs. The
candidate is retained in the store, every command runs once in a detached
snapshot pinned to that exact SHA, and only a trusted, finished job turns the
publication gate green.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update

from src.commands.job_commands import JobCommandsMixin
from src.config import AppConfig
from src.database import Database
from src.database.tables import jobs, projects
from src.git.manager import GitManager
from src.integration.batches import Batch
from src.integration.checks import (
    LOCAL_CHECKS_PRODUCER_ID,
    ChecksState,
    LocalChecks,
    RequestedChecks,
)
from src.integration.gitops import RetainedRepository
from src.integration.train import CandidateChecks, TrainTarget, candidate_head
from src.integration.train_sources import (
    RETAINED_CANDIDATE_PREFIX,
    DaemonLanes,
    DatabaseBatches,
)
from src.jobs.result import build_result
from src.jobs.service import JobService
from src.models import Project, RepoConfig, RepoSourceType
from src.playbooks.artifact_store import ArtifactStore
from tests.db_fixtures import lease_dsn
from tests.test_development_subjects import pinned_policy

TARGET = TrainTarget(project_id="p", repository_id="r", target_ref="refs/heads/main")
BATCH = Batch(id="batch-1", project_id="p", repository_id="r", target_ref="refs/heads/main")
COMMAND = "ruff check sample.py"


def settings(validation: str = "focused", commands=(COMMAND,)):
    return SimpleNamespace(
        validation=validation, commands=list(commands), slot_wait_seconds=1800,
        timeout_seconds=300,
    )


class JobsHandler(JobCommandsMixin):
    """The command owner's job surface, enough for ``job_submit_integration``."""

    def _jobs(self):
        return self.service


def jobs_handler(db, tmp_path: Path) -> JobsHandler:
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.resources.jobs.enabled = True
    handler = JobsHandler()
    handler.db, handler.config = db, config
    handler.service = JobService(db, config)
    return handler


async def run(git: GitManager, store: Path, *args: str) -> str:
    return (await git._arun(list(args), cwd=str(store))).strip()


@pytest.fixture
async def world(tmp_path):
    db = Database(lease_dsn("train-jobs"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Jobs"))
    git = GitManager()
    store = tmp_path / "store"
    store.mkdir()
    (store / "sample.py").write_text("value = 1\n")
    await run(git, store, "init", "-b", "main")
    await run(git, store, "add", "sample.py")
    ident = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    await run(git, store, *ident, "commit", "-m", "base")
    # The candidate exists only as an object, like a merge the train built: no
    # ref reaches it until the lane retains it for the job's snapshot.
    await run(git, store, "checkout", "-b", "scratch")
    (store / "sample.py").write_text("value = 2\n")
    await run(git, store, *ident, "commit", "-am", "candidate")
    candidate = await run(git, store, "rev-parse", "HEAD")
    await run(git, store, "checkout", "main")
    await run(git, store, "branch", "-D", "scratch")
    handler = jobs_handler(db, tmp_path)
    config = handler.config
    orchestrator = SimpleNamespace(db=db, git=git, _command_handler=handler)
    lanes = DaemonLanes(orchestrator, batches=DatabaseBatches(db))
    yield SimpleNamespace(
        db=db, git=git, store=store, candidate=candidate, lanes=lanes, config=config,
    )
    await db.close()


def candidate_checks(world, validation: str = "focused", commands=(COMMAND,)):
    retained = SimpleNamespace(store=world.store)
    pinned = settings(validation, commands)

    async def resolve(batch, candidate_sha):
        return await world.lanes._local(TARGET, retained, pinned, "v1", batch, candidate_sha)

    return CandidateChecks(resolve, advisory=validation == "advisory")


async def job_rows(db) -> list[dict]:
    async with db._engine.connect() as conn:
        return [dict(r) for r in (await conn.execute(select(jobs))).mappings().all()]


async def finish(db, job: dict, *, exit_code: int) -> dict:
    job = await db.transition_job(job["id"], job["state_version"], "starting")
    job = await db.transition_job(job["id"], job["state_version"], "running")
    result = build_result(job, {"exit_code": exit_code, "input_stability": "stable"})
    return await db.transition_job(
        job["id"], job["state_version"], result["state"], cleaned=True, ended_at=job["submitted_at"],
        result=result, result_ref=job["id"],
    )


async def test_candidate_runs_once_in_a_snapshot_pinned_to_its_exact_sha(world):
    checks = candidate_checks(world)
    exact = await checks.for_candidate(BATCH, world.candidate)
    head = candidate_head(BATCH, world.candidate)
    requested = await exact.request(head)
    assert requested.outcome == "requested" and len(requested.job_ids) == 1
    retained = await run(world.git, world.store, "rev-parse", RETAINED_CANDIDATE_PREFIX + BATCH.id)
    assert retained == world.candidate
    [job] = await job_rows(world.db)
    assert job["owner_kind"] == "integration" and job["project_id"] == "p"
    assert job["input_mode"] == "snapshot" and job["input_ref"] == world.candidate
    snapshot = Path(job["contract"]["cwd"])
    assert snapshot != world.store
    assert await run(world.git, snapshot, "rev-parse", "HEAD") == world.candidate
    assert (snapshot / "sample.py").read_text() == "value = 2\n"
    # The production clone never left the target, and a replay attaches.
    assert (world.store / "sample.py").read_text() == "value = 1\n"
    replay = await exact.request(head)
    assert replay.job_ids == requested.job_ids
    assert len(await job_rows(world.db)) == 1

    pending = await exact.refresh(head)
    assert pending.state == ChecksState.PENDING
    assert not await checks.gate(BATCH, world.candidate, "tree")

    await finish(world.db, job, exit_code=0)
    green = await exact.refresh(head)
    assert green.green
    assert await checks.gate(BATCH, world.candidate, "tree")


async def test_gate_reads_only_a_candidate_this_lane_resolved(world):
    checks = candidate_checks(world)
    assert not await checks.gate(BATCH, world.candidate, "tree")
    assert await job_rows(world.db) == []


@pytest.mark.parametrize("validation, publishes", [("focused", False), ("advisory", True)])
async def test_red_job_blocks_focused_validation_and_publishes_advisory(
    world, validation, publishes,
):
    checks = candidate_checks(world, validation)
    exact = await checks.for_candidate(BATCH, world.candidate)
    head = candidate_head(BATCH, world.candidate)
    await exact.request(head)
    [job] = await job_rows(world.db)
    await finish(world.db, job, exit_code=1)
    red = await exact.refresh(head)
    assert red.state == ChecksState.RED
    assert checks.passes(red) is publishes
    assert await checks.gate(BATCH, world.candidate, "tree") is publishes


@pytest.mark.parametrize("validation, commands", [("none", (COMMAND,)), ("focused", ())])
async def test_no_validation_is_green_without_a_job(world, validation, commands):
    checks = candidate_checks(world, validation, commands)
    assert await checks.for_candidate(BATCH, world.candidate) is None
    assert await checks.gate(BATCH, world.candidate, "tree")
    async with world.db._engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(jobs)) == 0
    # Nothing to validate retains nothing either.
    refs = await run(world.git, world.store, "for-each-ref", RETAINED_CANDIDATE_PREFIX)
    assert refs == ""


async def test_a_new_repair_attempt_validates_again(world):
    checks = candidate_checks(world)
    first = await checks.for_candidate(BATCH, world.candidate)
    await first.request(candidate_head(BATCH, world.candidate))
    [job] = await job_rows(world.db)
    await finish(world.db, job, exit_code=1)
    retry = Batch(
        id=BATCH.id, project_id="p", repository_id="r", target_ref=BATCH.target_ref,
        repair_attempt_count=1,
    )
    exact = await checks.for_candidate(retry, world.candidate)
    head = candidate_head(retry, world.candidate)
    await exact.request(head)
    assert len(await job_rows(world.db)) == 2
    assert (await exact.refresh(head)).state == ChecksState.PENDING
    assert not await checks.gate(retry, world.candidate, "tree")


async def pin_development(db, tmp_path: Path, validation: str, **project):
    """Pin project ``p`` to a compiled, retained development source."""
    pinned = pinned_policy(validation, commands=[COMMAND])
    compiled = ArtifactStore(str(tmp_path / "compiled"))
    ref = compiled.put(pinned.definition, source_digest=pinned.definition.source_hash,
                       contract_fingerprint="sha256:" + "b" * 64,
                       profile_fingerprint="test", compiler_build="test")
    compiled.put_source(ref.artifact_sha256, pinned.source)
    async with db._engine.begin() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_policy={"development": {"route": {"artifact": {
                "playbook_id": pinned.definition.id, "artifact_sha256": ref.artifact_sha256,
            }}}},
            **project,
        ))
    config = SimpleNamespace(compiled_root=str(tmp_path / "compiled"),
                             vault_root=str(tmp_path / "vault"))
    return pinned, config, {ref.artifact_sha256: pinned.definition}.get


async def development_lanes(world, tmp_path, monkeypatch, validation: str):
    """The daemon's lanes for a project pinned to a retained reviewed source."""
    pinned, config, load = await pin_development(world.db, tmp_path, validation)
    await world.db.create_repo(RepoConfig(
        id="r", project_id="p", source_type=RepoSourceType.CLONE, url=str(world.store),
    ))
    retained = RetainedRepository(repository_id="r", store=world.store, binding=None,
                                  default_branch="main")
    built, hosted = [], []

    async def development_repository(primitives, repo_row, binding, settings, *, fetch=True):
        assert not fetch
        built.append(settings)
        return retained

    async def binding(repo_row):
        return None

    monkeypatch.setattr("src.integration.development_runtime.development_repository",
                        development_repository)
    orchestrator = SimpleNamespace(
        db=world.db, git=world.git, _command_handler=world.lanes.orchestrator._command_handler,
        config=config, _load_playbook_artifact=load,
        github_repository_binding_resolver=binding, development_integration=SimpleNamespace(),
    )
    lanes = DaemonLanes(orchestrator, batches=DatabaseBatches(world.db))

    async def hosted_checks(policy, binding, target, batch, candidate_sha, *, retained=None):
        assert retained.repository_id == target.repository_id
        assert retained.store == world.store
        hosted.append(target.kind)
        return "hosted"

    monkeypatch.setattr(lanes, "_hosted", hosted_checks)
    return lanes, pinned, built, hosted


@pytest.mark.parametrize("validation", ["focused", "advisory"])
async def test_development_pin_validates_roots_with_its_retained_source_jobs(
    world, tmp_path, monkeypatch, validation,
):
    lanes, pinned, built, hosted = await development_lanes(world, tmp_path, monkeypatch,
                                                           validation)
    lane = await lanes(TARGET)
    # The retained reviewed source, not the vault copy, supplies the settings.
    assert built == [pinned.settings] and pinned.settings.validation == validation
    assert lane.checks.advisory is (validation == "advisory")
    checks = await lane.checks.for_candidate(BATCH, world.candidate)
    assert isinstance(checks.provider, LocalChecks) and hosted == []
    assert checks.provider.producer.plan.commands == (COMMAND,)
    assert checks.provider.producer.store == str(world.store)
    # A development source names each check after its own command.
    assert checks.provider.names == (COMMAND,)
    retained = await run(world.git, world.store, "rev-parse", RETAINED_CANDIDATE_PREFIX + BATCH.id)
    assert retained == world.candidate


async def test_development_pin_leaves_epic_targets_on_hosted_checks(
    world, tmp_path, monkeypatch,
):
    lanes, _, _, hosted = await development_lanes(world, tmp_path, monkeypatch, "focused")
    epic = TrainTarget(project_id="p", repository_id="r", target_ref="refs/heads/aq/epic",
                       kind="epic")
    lane = await lanes(epic)
    assert not lane.checks.advisory
    assert await lane.checks.for_candidate(BATCH, world.candidate) == "hosted"
    assert hosted == ["epic"]
    assert await job_rows(world.db) == []


async def ci_lanes(world, monkeypatch, ci: dict, names=("lint",)):
    """The daemon's lanes for a project whose policy ``ci`` block picks the runner."""
    from tests.test_integration_service import _minimal_policy_values

    policy = _minimal_policy_values()
    required = {"version": "v7", "names": list(names), "producer_id": "forge"}
    for boundary in ("root", "parent"):
        policy[boundary] = {**policy[boundary], "required_checks": required}
    policy["ci"] = ci
    async with world.db._engine.begin() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_policy=policy))
    await world.db.create_repo(RepoConfig(
        id="r", project_id="p", source_type=RepoSourceType.CLONE, url=str(world.store),
    ))
    hosted = []

    async def binding(repo_row):
        return None

    fetches = []

    async def store(repo_row, *, fetch=True):
        if fetch:
            fetches.append(repo_row.id)
        return str(world.store)

    orchestrator = SimpleNamespace(
        db=world.db, git=world.git, _command_handler=world.lanes.orchestrator._command_handler,
        config=SimpleNamespace(), github_repository_binding_resolver=binding,
        development_integration=SimpleNamespace(store=store, fetches=fetches),
    )
    lanes = DaemonLanes(orchestrator, batches=DatabaseBatches(world.db))

    async def hosted_checks(policy, binding, target, batch, sha, *, retained=None,
                            expected_event="push"):
        hosted.append((target.kind, expected_event))
        return "hosted"

    monkeypatch.setattr(lanes, "_hosted", hosted_checks)
    return lanes, policy, hosted


LOCAL_CI = {"source": "local", "commands": {"lint": COMMAND}}


async def test_local_ci_source_runs_the_boundarys_named_checks_on_this_box(world, monkeypatch):
    lanes, _, hosted = await ci_lanes(world, monkeypatch, LOCAL_CI)
    lane = await lanes(TARGET)
    exact = await lane.checks.for_candidate(BATCH, world.candidate)
    assert isinstance(exact.provider, LocalChecks) and hosted == []
    # The same named required checks a hosted runner reports, from this box.
    assert exact.required.names == ("lint",) and exact.required.version == "v7"
    assert exact.required.producer_id.startswith(LOCAL_CHECKS_PRODUCER_ID + ":")
    assert exact.provider.producer.plan.commands == (COMMAND,)
    assert not lane.checks.advisory
    # No hosted proof exists to attest; GitHub is just the push remote.
    assert lane.service.attest is None and not lane.service.require_attestation

    head = candidate_head(BATCH, world.candidate)
    await exact.request(head)
    [job] = await job_rows(world.db)
    assert job["input_ref"] == world.candidate
    pending = await exact.refresh(head)
    assert pending.state == ChecksState.PENDING
    assert [check.name for check in pending.checks] == ["lint"]
    await finish(world.db, job, exit_code=0)
    green = await exact.refresh(head)
    assert green.green and green.checks[0].required_check_version == "v7"
    assert await lane.checks.gate(BATCH, world.candidate, "tree")


async def test_ci_source_switch_takes_effect_on_the_next_visit(world, monkeypatch):
    lanes, policy, hosted = await ci_lanes(world, monkeypatch, LOCAL_CI)
    member = SimpleNamespace(task_id="t1", source_sha=world.candidate)

    async def store_ci(ci):
        stored = {**policy, "ci": ci} if ci is not None else {
            key: value for key, value in policy.items() if key != "ci"}
        async with world.db._engine.begin() as conn:
            await conn.execute(update(projects).where(projects.c.id == "p").values(
                hierarchical_integration_policy=stored))
        return stored

    async def visit(stored):
        lane = await lanes(TARGET)
        candidate = await lane.checks.for_candidate(BATCH, world.candidate)
        return candidate, await lanes._pr_checks(TARGET, member, stored, None)

    candidate, pr = await visit(policy)
    assert isinstance(candidate.provider, LocalChecks) and isinstance(pr, RequestedChecks)
    assert hosted == []
    # The same lanes object: each visit re-reads the stored policy.
    candidate, pr = await visit(await store_ci(None))
    assert (candidate, pr) == ("hosted", "hosted")
    assert hosted == [("root", "push"), ("root", "pull_request")]
    candidate, pr = await visit(await store_ci({**LOCAL_CI, "source": "hybrid",
                                               "hosted_attestation": {"root": False}}))
    assert isinstance(candidate.provider, LocalChecks) and pr == "hosted"
    assert hosted[-1] == ("root", "pull_request") and len(hosted) == 3

async def test_local_ci_source_names_each_check_by_its_required_name(world, monkeypatch):
    lint, unit = COMMAND, "aq test tests/test_sample.py"
    lanes, _, _ = await ci_lanes(world, monkeypatch, {
        "source": "local", "commands": {"unit": unit, "lint": lint},
    }, names=("unit", "lint"))
    lane = await lanes(TARGET)
    exact = await lane.checks.for_candidate(BATCH, world.candidate)
    # Commands run in the boundary's check order, each labelled by its name.
    assert exact.provider.producer.plan.commands == (unit, lint)
    result = await exact.refresh(candidate_head(BATCH, world.candidate))
    assert [check.name for check in result.checks] == ["unit", "lint"]
    assert {check.reason for check in result.checks} == {"not_requested"}


async def test_local_ci_source_red_check_blocks_the_candidate(world, monkeypatch):
    lanes, _, _ = await ci_lanes(world, monkeypatch, LOCAL_CI)
    lane = await lanes(TARGET)
    exact = await lane.checks.for_candidate(BATCH, world.candidate)
    head = candidate_head(BATCH, world.candidate)
    await exact.request(head)
    [job] = await job_rows(world.db)
    await finish(world.db, job, exit_code=1)
    assert (await exact.refresh(head)).state == ChecksState.RED
    assert not await lane.checks.gate(BATCH, world.candidate, "tree")


async def test_epic_override_keeps_root_candidates_hosted(world, monkeypatch):
    lanes, _, hosted = await ci_lanes(world, monkeypatch, {**LOCAL_CI, "source": "hosted",
                                                           "epic": "local"})
    root = await lanes(TARGET)
    assert await root.checks.for_candidate(BATCH, world.candidate) == "hosted"
    assert hosted == [("root", "push")]
    assert root.service.require_attestation
    epic = TrainTarget(project_id="p", repository_id="r", target_ref="refs/heads/aq/epic",
                       kind="epic")
    lane = await lanes(epic)
    exact = await lane.checks.for_candidate(BATCH, world.candidate)
    assert isinstance(exact.provider, LocalChecks) and len(hosted) == 1
    assert exact.required.names == ("lint",) and exact.required.version == "v7"


@pytest.mark.parametrize("source, local_gate", [("local", True), ("hybrid", False),
                                                ("hosted", False)])
async def test_root_pr_gate_reads_the_selected_runner(world, monkeypatch, source, local_gate):
    lanes, policy, hosted = await ci_lanes(world, monkeypatch, {**LOCAL_CI, "source": source})
    member = SimpleNamespace(task_id="t1", source_sha=world.candidate)
    exact = await lanes._pr_checks(TARGET, member, policy, None)
    if not local_gate:
        # Hybrid still reads the PR checks GitHub itself enforces.
        assert exact == "hosted" and hosted == [("root", "pull_request")]
        assert await job_rows(world.db) == []
        return
    assert isinstance(exact, RequestedChecks) and hosted == []
    assert exact.required.names == ("lint",)
    # The member head is already in the retained store: no fetch.
    assert lanes.orchestrator.development_integration.fetches == []
    head = candidate_head(Batch("pr-admission-t1", "p", "r", TARGET.target_ref),
                          world.candidate)
    # The PR gate only refreshes; a local head's jobs start on that refresh.
    pending = await exact.refresh(head)
    assert pending.state == ChecksState.PENDING
    [job] = await job_rows(world.db)
    assert job["input_ref"] == world.candidate
    retained = await run(world.git, world.store, "rev-parse",
                         RETAINED_CANDIDATE_PREFIX + "pr-admission-t1")
    assert retained == world.candidate
    await finish(world.db, job, exit_code=0)
    assert (await exact.refresh(head)).green
    assert len(await job_rows(world.db)) == 1


@pytest.mark.parametrize("condition,code", [
    ("unapproved", "pr_review_missing"), ("old_approval", "pr_review_missing"),
    ("changes", "pr_changes_requested"), ("draft", "pr_draft"),
    ("closed", "pr_closed"), ("wrong_base", "awaiting_pr"),
    ("approved", "awaiting_pr_checks"),
])
@pytest.mark.parametrize("admission", ["reviewed", "authorized"])
async def test_local_pr_gate_checks_eligibility_and_approval_before_submitting_jobs(
    world, monkeypatch, condition, code, admission,
):
    from unittest.mock import AsyncMock

    from src.git.github_contracts import GitHubRepositoryBinding
    from src.integration.github_review_poll import RootPullRequestGate
    from src.models import Task, TaskStatus

    lanes, policy, _ = await ci_lanes(world, monkeypatch, LOCAL_CI)
    policy["root"]["admission"] = admission
    await world.db.update_project("p", hierarchical_integration_policy=policy)
    url = "https://github.com/test/repo/pull/1"
    await world.db.create_task(Task(id="t1", project_id="p", title="member", description="",
        status=TaskStatus.COMPLETED, branch_name="aq/member", pr_url=url))
    pull = {"state": "open", "draft": condition == "draft",
            "head": {"ref": "aq/member", "sha": world.candidate, "repo": {"id": 123}},
            "base": {"ref": "other" if condition == "wrong_base" else "main",
                     "repo": {"id": 123}}}
    if condition == "closed":
        pull["state"] = "closed"
    reviews = [] if condition == "unapproved" else [{
        "id": 1, "state": "CHANGES_REQUESTED" if condition == "changes" else "APPROVED",
        "commit_id": "a" * 40 if condition == "old_approval" else world.candidate,
        "user": {"login": "human", "type": "User"},
    }]
    binding = GitHubRepositoryBinding(123, "test/repo")
    # The gate verifies each approver's current write permission (epic A).
    client = SimpleNamespace(pull_request=AsyncMock(return_value=pull),
                             paged_list=AsyncMock(return_value=reviews),
                             request_json=AsyncMock(return_value={
                                 "permission": "write", "user": {"login": "human"}}))
    resolver = AsyncMock(side_effect=lanes._pr_checks)
    gate = RootPullRequestGate(world.db,
        repository=AsyncMock(return_value=(binding, client)), checks=resolver)
    member = SimpleNamespace(task_id="t1", source_sha=world.candidate)
    result = await gate(TARGET, member)
    assert result["code"] == code
    rows = await job_rows(world.db)
    if condition == "approved":
        assert len(rows) == 1 and rows[0]["input_ref"] == world.candidate
        assert rows[0]["priority_band"] == 0
    else:
        assert rows == []
        resolver.assert_not_awaited()


@pytest.mark.parametrize("hosted", [True, False])
async def test_exact_head_verdict_reads_local_evidence_without_observing(hosted):
    from src.integration.checks import HostedChecks
    from src.integration.train_sources import _exact_head_verdict

    calls = []

    class Exact:
        provider = (HostedChecks.__new__(HostedChecks) if hosted
                    else LocalChecks.__new__(LocalChecks))

        async def refresh(self, head):
            calls.append("refresh")
            return "observed"

        async def read(self, head):
            calls.append("read")
            return "stored"

    # Observing a local head nobody requested would record not_requested over
    # the evidence its own candidate run left.
    verdict = await _exact_head_verdict(Exact(), "head")
    assert (verdict, calls) == (("observed", ["refresh"]) if hosted else ("stored", ["read"]))
