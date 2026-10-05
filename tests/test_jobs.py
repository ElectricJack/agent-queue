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
from src.integration.checks import ChecksState, LocalChecks
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

    async def development_repository(primitives, repo_row, binding, settings):
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

    async def hosted_checks(policy, binding, target, batch, candidate_sha):
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
