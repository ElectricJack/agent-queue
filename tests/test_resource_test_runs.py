"""Orphaned ``aq test`` runs: attribution, detection, reaping — and the stop sweep.

On 2026-09-24 three stopped tasks' full-suite runs outlived their sessions
and held three of the box's four test slots for over an hour.  Each ran
from a detached (``setsid``) Bash-tool shell, which init adopted when the
harness exited, so nothing that stopped the session reached it.

These tests build that shape from real processes: a *harness* stand-in
carrying a session's ``AQ_INSTANCE_TOKEN`` starts, in a new session, an
``aq test`` stand-in that takes a slot through the real semaphore, records
the real :func:`holder_identity`, and runs a "pytest" child carrying
``AQ_TEST_RUN_ID`` with the slot descriptor inherited — as
``src/cli/test_runner.py`` does.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from src.resources.semaphore import SlotSemaphore
from src.resources.test_runs import (
    LIVE,
    ORPHANED,
    UNATTRIBUTED,
    held_slots,
    holder_identity,
    reap_orphans,
)
from src.sessions import proctable
from src.sessions.provider import SessionHandle

pytestmark = pytest.mark.skipif(not os.path.isdir("/proc"), reason="requires /proc")

REPO = Path(__file__).resolve().parents[1]

#: The ``aq test`` stand-in: take slot 0, record who holds it, run a child
#: that inherits the slot descriptor and the run id, report the child pid.
RUN = r"""
import json, os, subprocess, sys
from src.resources.semaphore import SlotSemaphore
from src.resources.test_runs import holder_identity

lock_dir, ready, run_id, attribute = sys.argv[1:5]
meta = (
    holder_identity(test_run_id=run_id)
    if attribute == "1"
    else {"pid": os.getpid(), "cwd": os.getcwd()}  # a record from before attribution
)
with SlotSemaphore(lock_dir, 1).acquire(timeout=10, meta=meta):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        env={**os.environ, "AQ_TEST_RUN_ID": run_id},
        close_fds=False,
    )
    with open(ready + ".tmp", "w") as f:
        json.dump({"run": os.getpid(), "pytest": child.pid}, f)
    os.replace(ready + ".tmp", ready)
    child.wait()
"""

#: The harness stand-in: start the run under setsid, as a Bash-tool call
#: does, then idle like an agent waiting on a background task.
HARNESS = r"""
import subprocess, sys, time
subprocess.Popen([sys.executable, *sys.argv[1:]], start_new_session=True)
time.sleep(300)
"""


def _env(token: str | None, session_id: str = "sess-test") -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AQ_")}
    env["PYTHONPATH"] = str(REPO)
    if token is not None:
        env["AQ_INSTANCE_TOKEN"] = token
        env["AQ_SESSION_ID"] = session_id
    return env


def _wait_for(path: Path, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


def _gone(pid: int, timeout: float = 5.0) -> bool:
    entry = proctable.read_entry_sync(pid)
    deadline = time.monotonic() + timeout
    while entry is not None and proctable.is_alive_sync(entry):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


class _Run:
    """A harness (optional) plus its detached ``aq test`` run holding slot 0."""

    def __init__(self, tmp_path: Path, *, token: str | None, attribute: bool = True):
        self.token = token
        self.lock_dir = tmp_path / "test-slots"
        self.run_id = uuid.uuid4().hex[:16]
        ready = tmp_path / f"ready-{self.run_id}.json"
        script = tmp_path / "run.py"
        script.write_text(RUN)
        argv = [str(script), str(self.lock_dir), str(ready), self.run_id, "1" if attribute else "0"]
        if token is None:
            self.harness = None
            self.run_proc = subprocess.Popen([sys.executable, *argv], env=_env(None), cwd=tmp_path)
        else:
            harness = tmp_path / "harness.py"
            harness.write_text(HARNESS)
            self.harness = subprocess.Popen(
                [sys.executable, str(harness), *argv], env=_env(token), cwd=tmp_path
            )
            self.run_proc = None
        pids = _wait_for(ready)
        self.run_pid, self.pytest_pid = pids["run"], pids["pytest"]

    def kill_harness(self) -> None:
        assert self.harness is not None
        self.harness.kill()
        self.harness.wait()

    def slot_held(self) -> bool:
        return SlotSemaphore(self.lock_dir, 1).state(0).held

    def cleanup(self) -> None:
        for proc in (self.harness, self.run_proc):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()
        for pid in (self.run_pid, self.pytest_pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.fixture
def make_run(tmp_path):
    runs: list[_Run] = []

    def _make(**kw) -> _Run:
        run = _Run(tmp_path, **kw)
        runs.append(run)
        return run

    yield _make
    for run in runs:
        run.cleanup()


def _token() -> str:
    return f"tok-{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


class TestAttribution:
    def test_the_record_names_the_session_its_harness_and_the_run(self, make_run):
        run = make_run(token=_token())
        [held] = held_slots(run.lock_dir)
        holder = held.holder
        assert holder["pid"] == run.run_pid
        assert holder["session_id"] == "sess-test"
        assert holder["test_run_id"] == run.run_id
        assert holder["session_root"]["pid"] == run.harness.pid
        assert isinstance(holder["pid_start"], int)
        # The token is a kill fence; the world-readable record carries a digest.
        assert run.token not in json.dumps(holder)

    def test_a_pool_worker_task_is_read_from_its_claim_file(self, tmp_path):
        claim = tmp_path / ".aq" / "claim.json"
        claim.parent.mkdir()
        claim.write_text(json.dumps({"task_id": "fresh-pinnacle", "claim_epoch": 1}))
        identity = holder_identity(test_run_id="r", environ={"AQ_WORK_DIR": str(tmp_path)})
        assert identity["task_id"] == "fresh-pinnacle"

    def test_an_explicit_task_id_wins_over_the_claim_file(self, tmp_path):
        claim = tmp_path / ".aq" / "claim.json"
        claim.parent.mkdir()
        claim.write_text(json.dumps({"task_id": "stale"}))
        identity = holder_identity(
            test_run_id="r", environ={"AQ_TASK_ID": "task-1", "AQ_WORK_DIR": str(tmp_path)}
        )
        assert identity["task_id"] == "task-1"

    def test_outside_a_session_there_is_no_root(self, tmp_path):
        identity = holder_identity(test_run_id="r", environ={}, cwd=str(tmp_path))
        assert "session_root" not in identity
        assert identity["session_id"] is None


# ---------------------------------------------------------------------------
# Verdicts and reaping
# ---------------------------------------------------------------------------


class TestReaping:
    def test_a_live_sessions_run_is_never_reaped(self, make_run):
        run = make_run(token=_token())
        [held] = held_slots(run.lock_dir)
        assert held.state == LIVE

        report = reap_orphans(run.lock_dir, apply=True, grace=0.5)

        assert report["orphaned"] == [] and report["results"] == []
        assert run.slot_held()
        assert proctable.read_entry_sync(run.pytest_pid) is not None

    def test_an_orphaned_run_is_detected_and_a_dry_run_changes_nothing(self, make_run):
        run = make_run(token=_token())
        run.kill_harness()

        [held] = held_slots(run.lock_dir)
        assert held.state == ORPHANED
        assert "has exited" in held.reason

        report = reap_orphans(run.lock_dir)  # dry run by default
        assert [row["slot"] for row in report["orphaned"]] == [0]
        assert report["results"] == []
        assert run.slot_held()
        assert proctable.read_entry_sync(run.run_pid) is not None

    def test_applying_the_reap_frees_the_slot(self, make_run):
        run = make_run(token=_token())
        run.kill_harness()

        report = reap_orphans(run.lock_dir, apply=True, grace=1.0)

        [result] = report["results"]
        assert result["action"] == "reaped" and result["freed"] is True
        assert {run.run_pid, run.pytest_pid} <= set(result["pids"])
        assert not run.slot_held()
        assert _gone(run.run_pid) and _gone(run.pytest_pid)

    def test_the_pytest_child_alone_is_found_by_its_run_id(self, make_run):
        """The wrapper was killed hard; its child still holds the descriptor."""
        run = make_run(token=_token())
        run.kill_harness()
        os.kill(run.run_pid, signal.SIGKILL)
        assert _gone(run.run_pid)
        assert run.slot_held()  # the inherited descriptor keeps the lock

        [result] = reap_orphans(run.lock_dir, apply=True, grace=1.0)["results"]

        assert result["freed"] is True
        assert _gone(run.pytest_pid)

    def test_an_unrelated_process_with_the_slot_file_open_is_spared(self, make_run, tmp_path):
        """A live session's waiter probes every slot file; it is not the orphan."""
        run = make_run(token=_token())
        bystander = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys, time; f = open(sys.argv[1]); print('ok', flush=True); time.sleep(60)",
                str(run.lock_dir / "slot-0.lock"),
            ],
            env=_env(_token(), session_id="sess-live"),
            stdout=subprocess.PIPE,
        )
        try:
            assert bystander.stdout.readline().strip() == b"ok"
            run.kill_harness()

            [result] = reap_orphans(run.lock_dir, apply=True, grace=1.0)["results"]

            assert result["freed"] is True
            assert bystander.pid not in result["pids"]
            assert bystander.poll() is None
        finally:
            bystander.kill()
            bystander.wait()

    def test_an_unattributed_run_is_reported_but_never_reaped(self, make_run):
        run = make_run(token=None)
        [held] = held_slots(run.lock_dir)
        assert held.state == UNATTRIBUTED

        report = reap_orphans(run.lock_dir, apply=True, grace=0.5)

        assert report["results"] == []
        assert run.slot_held()

    def test_a_record_from_before_attribution_is_judged_by_its_processes(self, make_run):
        run = make_run(token=_token(), attribute=False)
        [held] = held_slots(run.lock_dir)
        assert held.state == LIVE  # still under its harness

        run.kill_harness()
        top = proctable.read_entry_sync(run.run_pid)
        if top is None or top.ppid != 1:
            pytest.skip("orphans are adopted by a subreaper here, not init")

        [held] = held_slots(run.lock_dir)
        assert held.state == ORPHANED
        [result] = reap_orphans(run.lock_dir, apply=True, grace=1.0)["results"]
        assert result["freed"] is True


# ---------------------------------------------------------------------------
# Stopping the session frees the slot
# ---------------------------------------------------------------------------


class TestSessionStopSweep:
    async def test_subprocess_stop_frees_the_slot_its_session_held(self, make_run, tmp_path):
        from src.sessions.subprocess import SubprocessProvider

        class _Cfg:
            data_dir = str(tmp_path / "state")

        run = make_run(token=_token())
        handle = SessionHandle(name="s-gone", provider="subprocess", instance_token=run.token)

        await SubprocessProvider(config=_Cfg()).stop(handle, grace=1.0)

        assert not run.slot_held()
        assert _gone(run.run_pid) and _gone(run.pytest_pid)
        assert run.harness.wait(timeout=5) is not None

    async def test_tmux_stop_sweeps_after_the_harness_already_exited(self, make_run, monkeypatch):
        """The drained pool worker: its tmux session is gone, the run is not."""
        from src.sessions.tmux import TmuxProvider

        provider = TmuxProvider()

        async def _no_session(_h):
            return False

        monkeypatch.setattr(provider, "_fenced", _no_session)
        run = make_run(token=_token())
        run.kill_harness()

        await provider.stop(
            SessionHandle(name="s-drained", provider="tmux", instance_token=run.token), grace=1.0
        )

        assert not run.slot_held()
        assert _gone(run.run_pid) and _gone(run.pytest_pid)

    async def test_tmux_stop_leaves_another_sessions_run_alone(self, make_run, monkeypatch):
        from src.sessions.tmux import TmuxProvider

        provider = TmuxProvider()

        async def _no_session(_h):
            return False

        monkeypatch.setattr(provider, "_fenced", _no_session)
        run = make_run(token=_token())

        await provider.stop(
            SessionHandle(name="s-other", provider="tmux", instance_token=_token()), grace=0.5
        )

        assert run.slot_held()
        assert proctable.read_entry_sync(run.pytest_pid) is not None
