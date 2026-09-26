"""Receipt adoption, retention, authorization and deterministic admission."""

import time
from types import SimpleNamespace

from sqlalchemy import update
from src.config import AppConfig
from src.database.tables import jobs
from src.jobs.artifacts import atomic_json, job_directory, read_json
from src.jobs.policy import next_admission
from src.jobs.service import JobService
from src.commands.job_commands import JobCommandsMixin
from tests.test_jobs_queries import values, db as jobs_db

db = jobs_db


def service(db, tmp_path):
    config = AppConfig(data_dir=str(tmp_path / "data"))
    return JobService(db, config)


async def test_completion_adopted_with_flag_off_and_result_rebuilt(db, tmp_path):
    svc = service(db, tmp_path)
    job = await db.submit_job(values())
    job = await db.transition_job(job["id"], 0, "starting", launch_at=time.time())
    directory = job_directory(tmp_path / "data", job["id"])
    completion = {
        "job_id": job["id"],
        "nonce": job["runner_nonce"],
        "exit_code": 0,
        "run_seconds": 2,
    }
    atomic_json(directory / "completion.json", completion)
    # Starting can finish before the daemon sees running; receipt proves exit.
    await svc.reconcile(job)
    adopted = await db.get_job(job["id"])
    assert adopted["state"] == "succeeded"
    assert not await db.workspace_has_job_pin("w")
    assert read_json(directory / "result.json") == adopted["result"]
    # Replayed completion does not replace result or append another outbox key.
    atomic_json(directory / "completion.json", {**completion, "exit_code": 1})
    await svc.reconcile(adopted)
    assert (await db.get_job(job["id"]))["result"] == adopted["result"]


async def test_ambiguous_spawn_lost_retains_pin_until_receipt(db, tmp_path):
    svc = service(db, tmp_path)
    job = await db.submit_job(values())
    job = await db.transition_job(job["id"], 0, "starting", launch_at=time.time() - 31)
    await svc.reconcile(job)
    lost = await db.get_job(job["id"])
    assert lost["state"] == "lost"
    assert lost["cleanup_blocked"]
    assert await db.workspace_has_job_pin("w")
    await svc.reconcile(lost)
    assert await db.workspace_has_job_pin("w")  # elapsed time is never cleanup proof
    directory = job_directory(tmp_path / "data", job["id"])
    atomic_json(
        directory / "completion.json",
        {"job_id": job["id"], "nonce": job["runner_nonce"], "exit_code": 0},
    )
    await svc.reconcile(lost)
    assert not await db.workspace_has_job_pin("w")
    assert (await db.get_job(job["id"]))["state"] == "lost"  # terminal results are immutable


async def test_unreadable_process_identity_quarantines_job(db, tmp_path, monkeypatch):
    svc = service(db, tmp_path)
    job = await db.submit_job(values())
    job = await db.transition_job(job["id"], 0, "starting", launch_at=time.time() - 31)

    async def unavailable(_):
        raise RuntimeError("jobs.cleanup_blocked")

    monkeypatch.setattr("src.jobs.service.processes", unavailable)
    await svc.reconcile(job)
    assert (await db.get_job(job["id"]))["cleanup_blocked"]
    assert await db.workspace_has_job_pin("w")


async def test_logs_expire_independently_results_then_purge(db, tmp_path):
    svc = service(db, tmp_path)
    job = await db.submit_job(values(contract={"head_bytes": 8, "tail_bytes": 16}))
    job = await db.transition_job(job["id"], 0, "starting")
    from src.jobs.result import build_result

    result = build_result(job, {"exit_code": 1})
    await db.transition_job(
        job["id"],
        1,
        "failed",
        result=result,
        cleaned=True,
        ended_at=time.time() - 15 * 86400,
        output_retention="retained",
    )
    directory = job_directory(tmp_path / "data", job["id"])
    (directory / "output.head").write_bytes(b"failure")
    atomic_json(directory / "result.json", result)
    await svc.sweep()
    assert not (directory / "output.head").exists()
    assert read_json(directory / "result.json") == result
    assert (await db.get_job(job["id"]))["output_retention"] == "expired"
    async with db._engine.begin() as conn:
        await conn.execute(
            update(jobs).where(jobs.c.id == job["id"]).values(ended_at=time.time() - 91 * 86400)
        )
    await svc.sweep()
    assert await db.get_job(job["id"]) is None
    assert not directory.exists()


async def test_foreign_and_unclaimed_scope_cannot_read_or_cancel(db, tmp_path):
    class Handler(JobCommandsMixin):
        pass

    h = Handler()
    h.db = db
    h.config = AppConfig(data_dir=str(tmp_path / "data"))
    h.orchestrator = SimpleNamespace()
    job = await db.submit_job(values())
    for scope in (
        {"kind": "session", "project_id": "other", "task_id": "t"},
        {"kind": "session", "project_id": "p", "task_id": "foreign"},
        {"kind": "session", "project_id": "p", "task_id": None},
    ):
        h._current_scope = scope
        for method in (h._cmd_job_get, h._cmd_job_result, h._cmd_job_logs, h._cmd_job_cancel):
            assert (await method({"job_id": job["id"]}))["error"] == "not_found"
    h._current_scope = {"kind": "session", "project_id": "p", "task_id": "t"}
    assert (await h._cmd_job_get({"job_id": job["id"]}))["success"]


def test_admission_aged_order_and_exclusive_frontier():
    def row(id, state="queued", cls="shared", weight=1, band=2, submitted=100):
        return {
            "id": id,
            "state": state,
            "job_class": cls,
            "weight": weight,
            "priority_band": band,
            "submitted_at": submitted,
            "owner_id": id,
        }

    active, exclusive, later = (
        row("active", "running"),
        row("exclusive", cls="exclusive"),
        row("later", submitted=101),
    )
    assert next_admission([active, exclusive, later], 200, 4, 2) is None
    assert next_admission([exclusive, later], 200, 4, 2) == exclusive
    assert next_admission([row("a", "running", "exclusive"), later], 200, 4, 2) is None
    old = row("old", band=2, submitted=0)
    integration = row("integration", band=0, submitted=1000)
    assert next_admission([integration, old], 1300, 4, 2) == old


async def test_wrong_launch_identity_and_corrupt_receipt_never_release_pin(db, tmp_path):
    svc = service(db, tmp_path)
    job = await db.submit_job(values())
    job = await db.transition_job(job["id"], 0, "starting", launch_at=time.time() - 31)
    directory = job_directory(tmp_path / "data", job["id"])
    atomic_json(directory / "intent.json", {"job_id": job["id"], "nonce": "wrong"})
    await svc.reconcile(job)
    lost = await db.get_job(job["id"])
    assert lost["state"] == "lost" and lost["cleanup_blocked"]
    (directory / "completion.json").write_text("{broken")
    await svc.reconcile(lost)
    assert await db.workspace_has_job_pin("w")


async def test_service_launch_and_adoption_preserve_canonical_result(db, tmp_path, monkeypatch):
    import asyncio
    import sys
    from src.jobs.policy import Preset

    svc = service(db, tmp_path)
    svc.config.resources.jobs.enabled = True
    monkeypatch.setattr(
        "src.jobs.service.presets",
        lambda _: {"lint": Preset("lint", (sys.executable, "-c", "print('complete')"))},
    )
    children = []
    launch_args = []
    original_spawn = asyncio.create_subprocess_exec

    async def capture_spawn(*args, **kwargs):
        kwargs["stderr"] = asyncio.subprocess.PIPE
        child = await original_spawn(*args, **kwargs)
        children.append(child)
        launch_args.append((args, kwargs["cwd"]))
        return child

    monkeypatch.setattr("src.jobs.service.asyncio.create_subprocess_exec", capture_spawn)
    queued = await svc.submit(
        project_id="p",
        task_id="t",
        session_id="s",
        claim_epoch=1,
        workspace_id="w",
        generation=0,
        preset="lint",
        args=[],
        idempotency_key="launch",
    )
    await svc.tick()
    # This is the same adoption path after a daemon restart, including flag-off recovery.
    svc = service(db, tmp_path)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        await svc.tick()
        job = await db.get_job(queued["id"])
        if job["state"] == "succeeded":
            break
        await asyncio.sleep(0.05)
    if job["state"] != "succeeded" and children and children[0].returncode is not None:
        raise AssertionError(
            (launch_args, children[0].returncode, (await children[0].stderr.read()).decode())
        )
    assert job["state"] == "succeeded", job
    assert not await db.workspace_has_job_pin("w")
    directory = job_directory(tmp_path / "data", job["id"])
    assert read_json(directory / "result.json") == job["result"]
    assert "complete" in job["result"]["excerpt"]


async def test_owner_close_retries_workspace_release_after_verified_cleanup(db, tmp_path):
    from src.database.tables import tasks

    svc = service(db, tmp_path)
    job = await db.submit_job(values())
    job = await db.transition_job(job["id"], 0, "starting", launch_at=time.time() - 31)
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "t").values(status="COMPLETED"))
    await db.release_workspaces_for_task("t")
    await svc.tick()
    assert (await db.get_workspace("w")).locked_by_task_id == "t"
    directory = job_directory(tmp_path / "data", job["id"])
    atomic_json(
        directory / "completion.json",
        {
            "job_id": job["id"],
            "nonce": job["runner_nonce"],
            "exit_code": -15,
            "cancelled": True,
        },
    )
    await svc.tick()
    assert not await db.workspace_has_job_pin("w")
    assert (await db.get_workspace("w")).locked_by_task_id is None


async def test_unattributed_execution_lock_blocks_receipt_cleanup(db, tmp_path, monkeypatch):
    svc = service(db, tmp_path)
    job = await db.submit_job(values(contract={"lock_dir": str(tmp_path / "locks")}))
    job = await db.transition_job(job["id"], 0, "starting", launch_at=time.time() - 31)
    directory = job_directory(tmp_path / "data", job["id"])
    atomic_json(
        directory / "completion.json",
        {
            "job_id": job["id"],
            "nonce": job["runner_nonce"],
            "exit_code": 0,
        },
    )
    monkeypatch.setattr(
        "src.resources.test_runs.held_slots",
        lambda _: [
            SimpleNamespace(holder={"job_nonce": job["runner_nonce"]}),
        ],
    )
    await svc.reconcile(job)
    assert (await db.get_job(job["id"]))["cleanup_blocked"]
    assert await db.workspace_has_job_pin("w")
    from src.doctor.models import DoctorContext, Severity
    from src.doctor.resource_checks import _check_jobs_cleanup

    diagnosis = await _check_jobs_cleanup(DoctorContext(config=svc.config, db=db))
    assert diagnosis.severity == Severity.WARN
    assert diagnosis.data["blocked"][0]["job_id"] == job["id"]


async def test_missing_observed_output_cannot_be_adopted_as_pass(db, tmp_path):
    svc = service(db, tmp_path)
    job = await db.submit_job(values(contract={"head_bytes": 8, "tail_bytes": 16}))
    job = await db.transition_job(job["id"], 0, "starting", launch_at=time.time() - 31)
    directory = job_directory(tmp_path / "data", job["id"])
    atomic_json(
        directory / "completion.json",
        {
            "job_id": job["id"],
            "nonce": job["runner_nonce"],
            "exit_code": 0,
            "output_bytes_seen": 100,
        },
    )
    await svc.reconcile(job)
    result = (await db.get_job(job["id"]))["result"]
    assert result["outcome"] == "infrastructure"
    assert not await db.workspace_has_job_pin("w")
