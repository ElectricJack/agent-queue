"""Finite captures, independent device leases, recovery and native ownership."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import sys
import time
import uuid

import pytest
from sqlalchemy import update

from src.config import AppConfig
from src.database.tables import jobs
from src.jobs.artifacts import atomic_json, job_directory, read_json
from src.jobs.matter import capture_inputs, native_status, retain_capture, windows_path
from src.jobs.policy import JobError, next_admission
from src.jobs.service import JobService
from src.jobs.identity import stop_tree
from src.resources.box_lock import BoxLock
from src.resources.semaphore import SlotTimeout
from tests.test_agent_wait_queries import env as wait_env, result_count
from tests.test_jobs_queries import db as jobs_db
from tests.test_jobs_runner import ROOT, barrier, finish, launch, request
from tests.test_jobs_waits import setup as wait_setup

db = jobs_db
env = wait_env
setup = wait_setup


def configured(tmp_path, *, delay=0):
    bundle = tmp_path / "bundle"
    bundle.mkdir(exist_ok=True)
    atomic_json(
        bundle / "candidate.json",
        {
            "version": 1,
            "candidate_sha256": "a" * 64,
            "rig_sha256": "b" * 64,
            "manifest": {"rig": {"views": [{"name": "front"}]}},
        },
    )
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        """import hashlib, json, pathlib, time
def main(argv):
    assert argv[0] == 'capture'
    bundle, output = pathlib.Path(argv[1]), pathlib.Path(argv[2])
    output.mkdir()
    candidate = json.loads((bundle / 'candidate.json').read_text())
    view = {}
    for kind, suffix in [('image', '.png'), ('channels', '.png.channels.bin')]:
        data = kind.encode()
        (output / ('front' + suffix)).write_bytes(data)
        view[kind] = {'sha256': hashlib.sha256(data).hexdigest()}
    receipt = {k: candidate[k] for k in ('candidate_sha256', 'rig_sha256')}
    editor = pathlib.Path(argv[argv.index('--editor') + 1])
    receipt.update(version=1, status='complete', views={'front': view},
                   editor_sha256=hashlib.sha256(editor.read_bytes()).hexdigest())
    (output / 'capture.json').write_text(json.dumps(receipt))
    time.sleep(DELAY)
    return 0
""".replace("DELAY", str(delay))
    )
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.resources.jobs.enabled = True
    config.resources.jobs.matter_python = sys.executable
    config.resources.jobs.matter_capture_script = str(adapter)
    config.resources.jobs.matter_editor = "/bin/true"
    return config


async def submit(svc, **kwargs):
    return await svc.submit(
        project_id="p",
        task_id="t",
        session_id="s",
        claim_epoch=1,
        workspace_id="w",
        generation=0,
        preset="matter_render",
        args=["bundle"],
        idempotency_key="render",
        **kwargs,
    )


async def test_disabled_unconfigured_and_unsafe_capture_admission(db, tmp_path):
    config = configured(tmp_path)
    svc = JobService(db, config)
    config.resources.jobs.enabled = False
    with pytest.raises(JobError, match="jobs.disabled"):
        await submit(svc)
    assert await db.list_jobs() == []
    config.resources.jobs.enabled = True
    config.resources.jobs.matter_editor = ""
    with pytest.raises(JobError, match="matter_unconfigured"):
        await submit(svc)
    for args in (["../bundle"], [str(tmp_path / "bundle")], ["bundle", "--editor", "/bin/sh"]):
        with pytest.raises(JobError):
            capture_inputs(args, tmp_path)
    outside = tmp_path.parent / ("foreign-" + uuid.uuid4().hex)
    outside.mkdir()
    try:
        (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
        with pytest.raises(JobError, match="cwd_invalid"):
            capture_inputs(["escape"], tmp_path)
    finally:
        outside.rmdir()


async def test_render_submission_uses_separate_device_domain(db, tmp_path):
    svc = JobService(db, configured(tmp_path))
    job = await submit(svc)
    assert job["job_class"] == "exclusive" and job["contract"]["capacity"] == 1
    assert "locks/gpus/" in job["contract"]["lock_dir"]
    assert "AQ_TEST_RUN_ID" not in job["contract"]["env"]
    assert job["contract"]["capture_expected"]["views"] == ["front"]
    assert job["argv"][3:6] == [str(tmp_path), "/bin/true", str(tmp_path / "bundle")]
    assert not list((tmp_path / "data" / "locks" / "test-slots").glob("*"))


def test_gpu_and_pytest_frontiers_are_independent():
    def row(name, state="queued", device=None, cls="exclusive"):
        return dict(
            id=name,
            owner_id=name,
            state=state,
            job_class=cls,
            weight=1,
            submitted_at=100,
            priority_band=2,
            contract={"gpu_id": device},
        )

    gpu = row("render", device="gpu0")
    tests = row("pytest")
    assert next_admission([row("active", "running", "gpu0"), tests, gpu], 101, 2, 2) == tests
    assert next_admission([row("full", "running"), gpu], 101, 2, 2) == gpu
    assert next_admission([row("a", "running", "gpu0"), gpu], 101, 2, 2) is None
    other = row("b", device="gpu1")
    assert next_admission([row("a", "running", "gpu0"), other], 101, 2, 2) == other


async def test_two_authors_serialize_and_dead_child_frees_device(tmp_path):
    directories = [tmp_path / name for name in ("first", "second")]
    for directory in directories:
        directory.mkdir()
    command = "import pathlib,time; pathlib.Path('entered').touch();\nwhile not pathlib.Path('release').exists(): time.sleep(.02)"
    requests = [request(d, command) for d in directories]
    for directory, job in zip(directories, requests):
        job["contract"].update(gpu_id="gpu0", lock_dir=str(tmp_path / "gpu"))
        atomic_json(directory / "request.json", job)
    first = await launch(directories[0], requests[0])
    second_launch = asyncio.create_task(launch(directories[1], requests[1]))
    try:
        await barrier(directories[0] / "entered")
        await barrier(directories[1] / "intent.json")
        assert not (directories[1] / "entered").exists()
        first.kill()
        await first.wait()
        with pytest.raises(SlotTimeout):
            with BoxLock(tmp_path / "gpu", 1).acquire(timeout=0):
                pytest.fail("surviving renderer must retain its GPU")
        await stop_tree(requests[0]["runner_nonce"], grace=0.1)
        second = await second_launch
        await barrier(directories[1] / "entered")
        (directories[1] / "release").touch()
        await finish(second, directories[1])
        with BoxLock(tmp_path / "gpu", 1).acquire(timeout=0):
            pass
        with BoxLock(tmp_path / "pytest", 1).acquire(timeout=0):
            pass
    finally:
        for job in requests:
            await stop_tree(job["runner_nonce"], grace=0.1)
        await first.wait()
        await asyncio.gather(second_launch, return_exceptions=True)


async def test_timed_out_complete_capture_cannot_pass(db, tmp_path):
    svc = JobService(db, configured(tmp_path, delay=60))
    svc.settings.run_seconds = 1
    job = await submit(svc)
    directory = job_directory(Path(svc.config.data_dir), job["id"])
    atomic_json(directory / "request.json", job)
    proc = await launch(directory, job)
    try:
        receipt = await finish(proc, directory)
        assert read_json(directory / "capture/capture.json")["status"] == "complete"
        assert receipt["infra_reason"] == "run_timeout"
        result = read_json(directory / "result.json")
        assert result["state"] == "failed" and result["capture"] is None
    finally:
        await stop_tree(job["runner_nonce"], grace=0.1)
        await proc.wait()


async def test_atomic_render_wait_recovers_once_and_evidence_outlives_logs(setup, tmp_path):
    config = configured(tmp_path)
    svc = JobService(setup.db, config)
    args = dict(
        project_id="p",
        task_id="owner",
        session_id="s",
        claim_epoch=1,
        workspace_id="w",
        generation=0,
        preset="matter_render",
        args=["bundle"],
        idempotency_key="render-wait",
        wait_identity=setup.identity,
    )
    job = await svc.submit(**args)
    assert (await svc.submit(**args))["wait"]["id"] == job["wait"]["id"]
    await svc.launch(job)
    directory = job_directory(Path(config.data_dir), job["id"])
    await barrier(directory / "completion.json")
    # A fresh service adopts the detached owner's receipt with admission disabled.
    config.resources.jobs.enabled = False
    restarted = JobService(setup.db, config)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        await restarted.tick()
        adopted = await setup.db.get_job(job["id"])
        if adopted["state"] == "succeeded":
            break
        await asyncio.sleep(0.05)
    assert adopted["state"] == "succeeded", adopted
    for _ in range(2):
        await setup.db.reconcile_agent_waits(now=time.time())
    assert await result_count(setup.db, job["wait"]["id"]) == 1
    assert not await setup.db.workspace_has_job_pin("w")
    capture = adopted["result"]["capture"]
    for artifact in capture["artifacts"]:
        assert (
            hashlib.sha256((directory / artifact["path"]).read_bytes()).hexdigest()
            == artifact["sha256"]
        )
    expected = adopted["contract"]["capture_expected"]
    with pytest.raises(ValueError, match="budget"):
        retain_capture(directory, (directory / "capture/capture.json").stat().st_size, expected)
    image = directory / "capture/front.png"
    image.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        retain_capture(directory, 100000, expected)
    async with setup.db._engine.begin() as conn:
        await conn.execute(
            update(jobs).where(jobs.c.id == job["id"]).values(ended_at=time.time() - 15 * 86400)
        )
    await restarted.sweep()
    assert image.exists() and (directory / "capture/capture.json").exists()
    assert not (directory / "output.head").exists()


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["cancel", "timeout", "owner_death"])
async def test_native_windows_owner_reaps_child_tree_and_releases_mutex(tmp_path, mode):
    python = os.environ.get("AQ_WINDOWS_TEST_PYTHON")
    if not python or not Path(python).is_file():
        pytest.skip("set AQ_WINDOWS_TEST_PYTHON to a native Windows Python from WSL")
    owner_script = await windows_path(ROOT / "src/jobs/windows_owner.py")
    directory = tmp_path / mode
    directory.mkdir()
    path = directory / "native-request.json"
    nonce = uuid.uuid4().hex
    native_python = await windows_path(python)
    command = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "__import__('pathlib').Path('tree-ready').touch(); time.sleep(60)"
    )
    atomic_json(
        path,
        dict(
            nonce=nonce,
            job_id=str(uuid.uuid4()),
            gpu_id="aq-test-" + uuid.uuid4().hex,
            argv=[native_python, "-c", command],
            cwd=await windows_path(directory),
            env={},
            queue_deadline=time.time() + 20,
            run_deadline=time.time() + (2 if mode == "timeout" else 30),
        ),
    )
    proc = await asyncio.create_subprocess_exec(
        python,
        owner_script,
        "run",
        await windows_path(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    job = {
        "runner_nonce": nonce,
        "contract": {
            "native_windows": {
                "python": python,
                "owner_script": owner_script,
            }
        },
    }
    second = None
    next_directory = directory / "second"
    try:
        await barrier(directory / "native-started.json")
        await barrier(directory / "tree-ready")
        assert (await native_status(job, directory))["active_processes"] >= 2
        next_directory.mkdir()
        next_request = {
            **read_json(path),
            "nonce": uuid.uuid4().hex,
            "job_id": str(uuid.uuid4()),
            "argv": [native_python, "-c", "print('second owner')"],
            "run_deadline": time.time() + 30,
        }
        atomic_json(next_directory / "native-request.json", next_request)
        second = await asyncio.create_subprocess_exec(
            python,
            owner_script,
            "run",
            await windows_path(next_directory / "native-request.json"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await barrier(next_directory / "native-intent.json")
        assert not (next_directory / "native-started.json").exists()
        if mode == "cancel":
            atomic_json(directory / "cancel.json", {})
        elif mode == "owner_death":
            # Terminate this fixture's known native owner, not its WSL launcher.
            owner = read_json(directory / "native-intent.json")
            kill = await asyncio.create_subprocess_exec(
                python,
                "-c",
                "import ctypes,sys; k=ctypes.WinDLL('kernel32'); "
                "k.OpenProcess.restype=ctypes.c_void_p; "
                "h=k.OpenProcess(1,False,int(sys.argv[1])); "
                "k.TerminateProcess.argtypes=[ctypes.c_void_p,ctypes.c_uint]; "
                "assert k.TerminateProcess(h,1)",
                str(owner["pid"]),
            )
            assert await kill.wait() == 0
        await asyncio.wait_for(proc.wait(), 15)
        status = await native_status(job, directory)
        assert status == {"known": True, "owner_alive": False, "active_processes": 0}
        await asyncio.wait_for(second.wait(), 15)
        assert second.returncode == 0, (await second.stderr.read()).decode()
        assert read_json(next_directory / "native-completion.json")["exit_code"] == 0
        if mode != "owner_death":
            assert proc.returncode == 0, (await proc.stderr.read()).decode()
            receipt = read_json(directory / "native-completion.json")
            assert receipt["cancelled"] == (mode == "cancel")
            assert receipt["infra_reason"] == ("run_timeout" if mode == "timeout" else None)
    finally:
        atomic_json(directory / "cancel.json", {})
        if proc.returncode is None:
            await asyncio.wait_for(proc.wait(), 15)
        if second and second.returncode is None:
            atomic_json(next_directory / "cancel.json", {})
            await asyncio.wait_for(second.wait(), 15)


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["complete", "wsl_owner_death"])
async def test_native_render_job_adoption_and_wsl_owner_recovery(db, tmp_path, mode):
    python = os.environ.get("AQ_WINDOWS_TEST_PYTHON")
    if not python or not Path(python).is_file():
        pytest.skip("set AQ_WINDOWS_TEST_PYTHON to a native Windows Python from WSL")
    config = configured(tmp_path, delay=60 if mode == "wsl_owner_death" else 0)
    config.resources.jobs.matter_python = python
    svc = JobService(db, config)
    job = await submit(svc)
    await svc.launch(job)
    directory = job_directory(Path(config.data_dir), job["id"])
    try:
        await barrier(directory / "capture/capture.json")
        if mode == "wsl_owner_death":
            import signal

            intent = read_json(directory / "intent.json")
            os.kill(intent["pid"], signal.SIGKILL)
        config.resources.jobs.enabled = False
        restarted = JobService(db, config)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            await restarted.tick()
            current = await db.get_job(job["id"])
            if current["state"] in {"succeeded", "lost"}:
                break
            await asyncio.sleep(0.1)
        assert current["state"] == ("succeeded" if mode == "complete" else "lost"), current
        assert not await db.workspace_has_job_pin("w")
        assert not (await native_status(job, directory))["active_processes"]
        assert not list((Path(config.data_dir) / "locks/test-slots").glob("*"))
    finally:
        atomic_json(directory / "cancel.json", {})
        await stop_tree(job["runner_nonce"], grace=0.1)
