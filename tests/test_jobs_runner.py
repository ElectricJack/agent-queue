"""Detached runner crash boundary tested with explicit artifact barriers."""

import asyncio
import sys
import time
import uuid
from pathlib import Path

import pytest
from src.jobs.artifacts import atomic_json, read_json
from src.jobs.identity import processes, stop_tree, verified
from src.resources.box_lock import BoxLock
from src.resources.semaphore import SlotTimeout
from src.sessions.proctable import kill_marked

ROOT = Path(__file__).resolve().parents[1]


async def barrier(path, *, timeout=15):
    async def wait():
        while not path.exists():
            await asyncio.sleep(0.02)
        return read_json(path) if path.suffix == ".json" else path.read_text()

    return await asyncio.wait_for(wait(), timeout)


def request(directory, command, *, timeout=10):
    nonce, job_id = uuid.uuid4().hex, str(uuid.uuid4())
    env = {
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "AQ_JOB_ID": job_id,
        "AQ_JOB_NONCE": nonce,
        "AQ_TASK_ID": "t",
        "AQ_DB_SCOPE": "worker",
        "AQ_DATABASE_URL": "aq-worker-no-direct-db://",
        "AGENT_QUEUE_DB": "aq-worker-no-direct-db://",
    }
    job = {
        "id": job_id,
        "runner_nonce": nonce,
        "task_id": "t",
        "preset": "fixture",
        "input_mode": "live",
        "argv": [sys.executable, "-c", command],
        "weight": 1,
        "job_class": "shared",
        "submitted_at": time.time(),
        "queue_deadline": time.time() + 10,
        "run_timeout": timeout,
        "contract": {
            "cwd": str(directory),
            "env": env,
            "capacity": 1,
            "lock_dir": str(directory / "locks"),
            "head_bytes": 128,
            "tail_bytes": 1024,
        },
    }
    atomic_json(directory / "request.json", job)
    return job


async def launch(directory, job):
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "src.jobs.runner",
        str(directory),
        cwd=ROOT,
        env=job["contract"]["env"],
        start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    async def ready():
        while (
            not (directory / "started.json").exists()
            and not (directory / "completion.json").exists()
        ):
            if proc.returncode is not None:
                raise AssertionError((await proc.stderr.read()).decode())
            await asyncio.sleep(0.02)

    await asyncio.wait_for(ready(), 15)
    return proc


async def finish(proc, directory):
    completion = await barrier(directory / "completion.json")
    await asyncio.wait_for(proc.wait(), 10)
    assert proc.returncode == 0, (await proc.stderr.read()).decode()
    return completion


async def test_session_stop_preserves_job_and_environment_has_no_credentials(tmp_path):
    command = """import os, json, pathlib, time
p = pathlib.Path('.')
p.joinpath('child.json').write_text(json.dumps(dict(os.environ)))
while not p.joinpath('release').exists(): time.sleep(.02)
print('done')
"""
    job = request(tmp_path, command)
    proc = await launch(tmp_path, job)
    try:
        env = await barrier(tmp_path / "child.json")
        assert env["AQ_JOB_ID"] == job["id"]
        assert env["AQ_DB_SCOPE"] == "worker"
        assert not set(env) & {
            "AQ_INSTANCE_TOKEN",
            "AQ_API_TOKEN",
            "OPENAI_API_KEY",
            "GITHUB_TOKEN",
        }
        # The inherited worker token cannot sweep this detached job.
        await kill_marked("fixture-session-token", grace=0.1)
        assert proc.returncode is None
        (tmp_path / "release").touch()
        receipt = await finish(proc, tmp_path)
        assert receipt["exit_code"] == 0
        assert read_json(tmp_path / "result.json")["outcome"] == "passed"
    finally:
        await stop_tree(job["runner_nonce"], grace=0.1)
        await proc.wait()


async def test_cancel_and_deadline_have_distinct_receipts(tmp_path):
    for mode in ("cancel", "deadline"):
        directory = tmp_path / mode
        directory.mkdir()
        job = request(
            directory,
            "import time; print('ready', flush=True); time.sleep(60)",
            timeout=0.2 if mode == "deadline" else 10,
        )
        proc = await launch(directory, job)
        try:
            await barrier(directory / "started.json")
            if mode == "cancel":
                atomic_json(directory / "cancel.json", {"requested_at": time.time()})
            receipt = await finish(proc, directory)
            result = read_json(directory / "result.json")
            assert result["outcome"] == ("cancelled" if mode == "cancel" else "infrastructure")
            assert receipt["infra_reason"] == (None if mode == "cancel" else "run_timeout")
            assert not await processes(job["runner_nonce"])
        finally:
            await stop_tree(job["runner_nonce"], grace=0.1)
            await proc.wait()


async def test_supervisor_killed_child_retains_lock_and_intent_prevents_replay(tmp_path):
    command = """import pathlib, time
p = pathlib.Path('launch-count')
p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')
while not pathlib.Path('release').exists(): time.sleep(.02)
"""
    job = request(tmp_path, command)
    proc = await launch(tmp_path, job)
    try:
        await barrier(tmp_path / "launch-count")
        started = await barrier(tmp_path / "started.json")
        assert await verified(started, job["runner_nonce"])
        assert not await verified(
            {**started, "start_ticks": started["start_ticks"] + 1}, job["runner_nonce"]
        )
        proc.kill()
        await proc.wait()
        from src.resources.test_runs import reap_orphans, LIVE

        observed = await asyncio.to_thread(reap_orphans, tmp_path / "locks", apply=True)
        assert not observed["orphaned"]
        assert observed["held"][0]["state"] == LIVE
        assert observed["held"][0]["job_id"] == job["id"]
        with pytest.raises(SlotTimeout):
            with BoxLock(tmp_path / "locks", 1).acquire(timeout=0):
                pytest.fail("surviving child must retain capacity")
        # Re-executing the supervisor is fenced by the durable launch intent.
        replacement = await launch(tmp_path, job)
        await asyncio.wait_for(replacement.wait(), 10)
        assert (tmp_path / "launch-count").read_text() == "1"
        assert not (tmp_path / "completion.json").exists()
    finally:
        await stop_tree(job["runner_nonce"], grace=0.1)
        await proc.wait()
    with BoxLock(tmp_path / "locks", 1).acquire(timeout=0):
        pass


async def test_leader_exit_reaps_descendants_before_completion(tmp_path):
    command = """import subprocess, sys, pathlib
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], close_fds=False)
pathlib.Path('child-pid').write_text(str(child.pid))
"""
    job = request(tmp_path, command)
    proc = await launch(tmp_path, job)
    try:
        await barrier(tmp_path / "child-pid")
        receipt = await finish(proc, tmp_path)
        assert receipt["exit_code"] == 0
        assert not await processes(job["runner_nonce"])
        with BoxLock(tmp_path / "locks", 1).acquire(timeout=0):
            pass
    finally:
        await stop_tree(job["runner_nonce"], grace=0.1)
        await proc.wait()


async def test_output_write_failure_cancels_and_cannot_pass(tmp_path, monkeypatch):
    from src.jobs.runner import supervise
    from src.jobs.artifacts import OutputStore

    def disk_full(*_):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(OutputStore, "append", disk_full)
    request(tmp_path, "print('output', flush=True)")
    await supervise(tmp_path)
    result = read_json(tmp_path / "result.json")
    assert result["outcome"] == "infrastructure"
    assert result["infra_reason"] == "output_store_failed"


@pytest.mark.parametrize("kind", ["pass", "fail", "empty"])
async def test_pytest_junit_custom_path_and_observed_exit(tmp_path, kind):
    content = {
        "pass": "def test_example():\n    assert True\n",
        "fail": "def test_example():\n    assert False\n",
        "empty": "# no tests\n",
    }[kind]
    (tmp_path / "test_example.py").write_text(content)
    job = request(tmp_path, "", timeout=30)
    job["argv"] = [sys.executable, "-m", "pytest", "test_example.py", "--junitxml=custom.xml"]
    job["contract"]["pytest"] = True
    atomic_json(tmp_path / "request.json", job)
    proc = await launch(tmp_path, job)
    try:
        receipt = await finish(proc, tmp_path)
        result = read_json(tmp_path / "result.json")
        assert receipt["exit_code"] == {"pass": 0, "fail": 1, "empty": 5}[kind]
        assert result["outcome"] == ("passed" if kind == "pass" else "failed")
        assert result["parser_source"] == "junit"
        assert (tmp_path / "custom.xml").exists()
        assert not (tmp_path / "junit.xml").exists()
        if kind == "fail":
            assert result["failing_tests"]
            assert "test_example" in result["excerpt"]
    finally:
        await stop_tree(job["runner_nonce"], grace=0.1)
        await proc.wait()
