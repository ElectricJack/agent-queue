"""PSI, test-slot occupancy and ungated attribution — nulls are admissions, never zeros."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from src.config import AppConfig, ResourcesConfig
from src.metrics import host
from src.metrics.host import (
    BACKOFF_SAMPLES,
    HostSampler,
    read_pressure,
    read_test_slots,
    read_ungated_load,
)
from src.resources.procs import ProcInfo, scan_processes
from src.resources.semaphore import SlotSemaphore
from src.resources.test_runs import ORPHANED, HeldSlot


def write_psi(root: Path, *, cpu_full: bool = True) -> None:
    root.mkdir()
    cpu = "some avg10=12.50 avg60=3.00 avg300=1.00 total=100\n"
    if cpu_full:
        cpu += "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    (root / "cpu").write_text(cpu)
    (root / "io").write_text(
        "some avg10=40.00 avg60=1.00 avg300=1.00 total=1\n"
        "full avg10=20.00 avg60=1.00 avg300=1.00 total=1\n"
    )
    (root / "memory").write_text(
        "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    )


def test_pressure_is_parsed_per_resource(tmp_path):
    write_psi(tmp_path / "pressure")
    out = read_pressure(tmp_path / "pressure")
    assert out["cpu"] == {"some_avg10": 12.5, "full_avg10": 0.0}
    assert out["io"] == {"some_avg10": 40.0, "full_avg10": 20.0}
    assert out["memory"] == {"some_avg10": 0.0, "full_avg10": 0.0}
    assert out["reason"] is None


def test_an_older_kernel_without_a_cpu_full_line_reports_null_for_it(tmp_path):
    write_psi(tmp_path / "pressure", cpu_full=False)
    out = read_pressure(tmp_path / "pressure")
    assert out["cpu"] == {"some_avg10": 12.5, "full_avg10": None}


def test_missing_psi_is_null_with_a_reason(tmp_path):
    out = read_pressure(tmp_path / "nowhere")
    assert out["cpu"] is None and out["io"] is None and out["memory"] is None
    assert out["reason"] == "psi_unavailable"


def test_an_unreadable_resource_is_null_with_a_reason(tmp_path):
    write_psi(tmp_path / "pressure")
    (tmp_path / "pressure" / "io").unlink()
    out = read_pressure(tmp_path / "pressure")
    assert out["cpu"] == {"some_avg10": 12.5, "full_avg10": 0.0}
    assert out["io"] is None
    assert out["reason"] == "psi_unreadable"


def test_a_malformed_field_is_null_rather_than_zero(tmp_path):
    root = tmp_path / "pressure"
    write_psi(root)
    (root / "cpu").write_text("some avg10=oops avg60=1.00 total=1\n")
    assert read_pressure(root)["cpu"] == {"some_avg10": None, "full_avg10": None}


def _config(**resources) -> AppConfig:
    cfg = AppConfig()
    cfg.resources = ResourcesConfig(**resources)
    return cfg


def test_test_slots_report_used_total_and_waiters(tmp_path):
    sem = SlotSemaphore(tmp_path, 2)
    acquired = sem.try_acquire({"task_id": "t-1", "pid": 1})
    assert acquired is not None
    try:
        out = read_test_slots(_config(test_slots=2), lock_dir=tmp_path)
    finally:
        os.close(acquired[1])
    assert out["used"] == 1 and out["total"] == 2 and out["waiting"] == 0
    assert out["reason"] is None


def test_a_live_waiter_is_counted(tmp_path):
    (tmp_path / "waiters").mkdir()
    (tmp_path / "waiters" / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid()}))
    out = read_test_slots(_config(test_slots=2), lock_dir=tmp_path)
    assert out["used"] == 0 and out["waiting"] == 1


def test_orphaned_holders_come_from_the_held_slot_verdicts(tmp_path, monkeypatch):
    orphan = HeldSlot(slot=0, path="slot-0.lock", holder={}, state=ORPHANED, reason="gone")
    monkeypatch.setattr(host, "held_slots", lambda lock_dir: [orphan])
    out = read_test_slots(_config(test_slots=2), lock_dir=tmp_path)
    assert out["orphaned"] == 1


def test_reading_an_absent_lock_dir_creates_nothing(tmp_path):
    lock_dir = tmp_path / "locks" / "test-slots"
    out = read_test_slots(_config(test_slots=3), lock_dir=lock_dir)
    assert out == {"used": 0, "total": 3, "waiting": 0, "orphaned": 0, "reason": None}
    assert not lock_dir.exists()


def test_the_default_lock_dir_follows_data_dir(tmp_path):
    cfg = _config(test_slots=1)
    cfg.data_dir = str(tmp_path)
    lock_dir = tmp_path / "locks" / "test-slots"
    acquired = SlotSemaphore(lock_dir, 1).try_acquire({"pid": 1})
    assert acquired is not None
    try:
        assert read_test_slots(cfg)["used"] == 1
    finally:
        os.close(acquired[1])


def test_test_slots_are_null_when_gating_is_disabled(tmp_path):
    out = read_test_slots(_config(enabled=False), lock_dir=tmp_path)
    assert out["used"] is None and out["total"] is None and out["waiting"] is None
    assert out["orphaned"] is None and out["reason"] == "resources_disabled"


def test_ungated_load_counts_processes_without_session_markers():
    procs = [
        ProcInfo(pid=1, comm="pytest", cmdline="pytest", task_id="t", session="s"),
        ProcInfo(pid=2, comm="pytest", cmdline="pytest", cwd="/x/.aq/worktrees/slot-3"),
        ProcInfo(pid=3, comm="pytest", cmdline="pytest"),
    ]
    out = read_ungated_load(procs)
    assert out == {"pytest_processes": 3, "unattributed": 2, "unattributed_slots": ["slot-3"]}


def test_a_pool_worker_is_attributed_by_its_session_id_alone():
    # A pool session is launched before it holds a task, so its environment
    # carries AQ_SESSION_ID but neither AQ_TASK_ID nor AQ_SESSION_NAME.
    procs = [
        ProcInfo(
            pid=1,
            comm="pytest",
            cmdline="pytest",
            cwd="/x/.aq/worktrees/slot-1",
            session_id="d3832352",
        ),
    ]
    assert read_ungated_load(procs)["unattributed"] == 0


def test_the_process_scan_reads_the_session_id_marker(tmp_path):
    marker = f"aq-host-reader-probe-{os.getpid()}"
    env = {k: v for k, v in os.environ.items() if not k.startswith("AQ_")}
    env["AQ_SESSION_ID"] = "sess-probe"
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; time.sleep(30)", marker],
        env=env,
        cwd=tmp_path,
    )
    try:
        deadline = time.monotonic() + 5.0
        found = []
        while not found and time.monotonic() < deadline:
            found = [p for p in scan_processes(marker) if p.pid == child.pid]
            time.sleep(0.02)
    finally:
        child.kill()
        child.wait()
    assert found and found[0].session_id == "sess-probe"
    assert found[0].task_id is None and found[0].session is None


def test_ungated_slot_names_are_bounded():
    procs = [
        ProcInfo(pid=i, comm="pytest", cmdline="pytest", cwd=f"/x/.aq/worktrees/slot-{i:02d}")
        for i in range(12)
    ]
    out = read_ungated_load(procs)
    assert out["unattributed"] == 12
    assert out["unattributed_slots"] == [f"slot-{i:02d}" for i in range(8)]


def _null_slots(config, lock_dir=None):
    return {
        "used": None,
        "total": None,
        "waiting": None,
        "orphaned": None,
        "reason": "resources_disabled",
    }


def _no_ungated(procs=None):
    return {"pytest_processes": 0, "unattributed": 0, "unattributed_slots": []}


def test_the_sampler_backs_off_and_marks_stale_after_an_overrun():
    ticks = iter([0.0, 0.050, 0.100, 0.101])  # first sample costs 50 ms, second 1 ms
    calls = {"psi": 0}

    def pressure(root=None):
        calls["psi"] += 1
        return {"cpu": None, "io": None, "memory": None, "reason": "psi_unavailable"}

    sampler = HostSampler(
        AppConfig(),
        budget_ms=20.0,
        clock=lambda: next(ticks),
        pressure=pressure,
        slots=_null_slots,
        ungated=_no_ungated,
    )
    first = sampler.sample()
    assert first["stale"] is False and first["host_ms"] == 50.0 and first["reason"] is None
    stale = [sampler.sample() for _ in range(BACKOFF_SAMPLES)]
    assert all(s["stale"] and s["reason"] == "over_budget" for s in stale)
    assert all(s["psi"] == first["psi"] and s["host_ms"] == 50.0 for s in stale)
    assert calls["psi"] == 1
    again = sampler.sample()
    assert again["stale"] is False and again["reason"] is None and calls["psi"] == 2


def test_a_sample_within_budget_does_not_back_off():
    ticks = iter([0.0, 0.001, 0.002, 0.003])
    calls = {"psi": 0}

    def pressure(root=None):
        calls["psi"] += 1
        return {"cpu": None, "io": None, "memory": None, "reason": "psi_unavailable"}

    sampler = HostSampler(
        AppConfig(),
        budget_ms=20.0,
        clock=lambda: next(ticks),
        pressure=pressure,
        slots=_null_slots,
        ungated=_no_ungated,
    )
    assert sampler.sample()["stale"] is False
    assert sampler.sample()["stale"] is False
    assert calls["psi"] == 2


def test_the_budget_defaults_to_the_metrics_config():
    ticks = iter([0.0, 0.030, 1.0, 1.001])  # 30 ms: over a 25 ms budget
    config = SimpleNamespace(metrics=SimpleNamespace(perf_host_budget_ms=25.0))
    sampler = HostSampler(
        config,
        clock=lambda: next(ticks),
        pressure=lambda: {"cpu": None, "io": None, "memory": None, "reason": "psi_unavailable"},
        slots=_null_slots,
        ungated=_no_ungated,
    )
    sampler.sample()
    assert sampler.sample()["reason"] == "over_budget"


def test_a_failing_reader_is_null_and_never_fails_the_sample():
    def broken(*args, **kwargs):
        raise PermissionError("denied")

    sampler = HostSampler(
        AppConfig(),
        budget_ms=1_000.0,
        pressure=broken,
        slots=_null_slots,
        ungated=broken,
    )
    out = sampler.sample()
    # Blocks keep their shape so the stored sample always validates.
    assert out["psi"] == {"cpu": None, "io": None, "memory": None, "reason": "psi_unreadable"}
    assert out["ungated"] == {
        "pytest_processes": None,
        "unattributed": None,
        "unattributed_slots": [],
    }
    assert out["test_slots"]["reason"] == "resources_disabled"
    assert out["stale"] is False and out["reason"] == "read_failed"


def test_a_failing_slot_read_is_null_with_a_reason():
    def broken(*args, **kwargs):
        raise OSError("lock dir vanished")

    sampler = HostSampler(
        AppConfig(),
        budget_ms=1_000.0,
        pressure=lambda: {"cpu": None, "io": None, "memory": None, "reason": "psi_unavailable"},
        slots=broken,
        ungated=_no_ungated,
    )
    out = sampler.sample()
    assert out["test_slots"] == {
        "used": None,
        "total": None,
        "waiting": None,
        "orphaned": None,
        "reason": "read_failed",
    }
    assert out["reason"] == "read_failed"


def test_the_real_readers_run_on_this_host(tmp_path):
    cfg = _config(test_slots=2)
    cfg.data_dir = str(tmp_path)
    out = HostSampler(cfg, budget_ms=10_000.0).sample()
    assert set(out) == {"psi", "test_slots", "ungated", "host_ms", "stale", "reason"}
    assert out["test_slots"]["total"] == 2
    assert isinstance(out["ungated"]["pytest_processes"], int)
    assert out["host_ms"] >= 0.0
