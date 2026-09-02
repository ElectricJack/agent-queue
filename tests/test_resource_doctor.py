"""``resources.*`` doctor checks and the ``aq test`` wrapper's argv rules.

The doctor checks are the operator-facing half of resource gating: they
have to name *which* session is eating the box, because "load is 61" on its
own has never helped anyone.
"""

from __future__ import annotations

import sys

import pytest

import src.doctor  # noqa: F401  (pulls the submodule into sys.modules)
from src.config import AppConfig, ResourceCgroupConfig, ResourcesConfig
from src.doctor import DoctorContext, Severity, default_registry
from src.doctor.resource_checks import run_check
from src.resources.limits import CgroupDelegation
from src.resources.procs import ProcInfo, summarize_by_session

# ``src/doctor/__init__.py`` imports the ``resource_checks`` *factory* out of
# the ``resource_checks`` submodule, which rebinds the package attribute to
# the function -- so ``import src.doctor.resource_checks`` hands back the
# function, not the module.  Same workaround as tests/test_pool_doctor.py.
resource_checks = sys.modules["src.doctor.resource_checks"]


def _ctx(**kw) -> DoctorContext:
    cfg = AppConfig()
    cfg.resources = ResourcesConfig(**kw)
    return DoctorContext(config=cfg)


def _proc(pid, cwd=None, task_id=None, session=None) -> ProcInfo:
    return ProcInfo(
        pid=pid,
        comm="pytest",
        cmdline="pytest -n auto",
        cwd=cwd,
        task_id=task_id,
        session=session,
    )


class TestRegistration:
    def test_the_checks_are_in_the_default_registry(self):
        ids = default_registry().ids()
        assert {"resources.load", "resources.test_pressure", "resources.cgroups"} <= set(ids)


class TestLoadCheck:
    @pytest.mark.asyncio
    async def test_ok_below_the_threshold(self, monkeypatch):
        monkeypatch.setattr(resource_checks, "load_average", lambda: (2.0, 3.0, 4.0))
        result = await run_check("resources.load", _ctx(cores=24))
        assert result.severity is Severity.OK
        assert result.data["cores"] == 24

    @pytest.mark.asyncio
    async def test_warns_on_the_five_minute_average(self, monkeypatch):
        # A 1-minute spike is a build starting; five minutes above one
        # runnable task per core is a saturated box.
        monkeypatch.setattr(resource_checks, "load_average", lambda: (61.0, 55.0, 30.0))
        monkeypatch.setattr(
            resource_checks,
            "pytest_processes",
            lambda: [_proc(1, task_id="prime-ember"), _proc(2, task_id="prime-ember")],
        )
        result = await run_check("resources.load", _ctx(cores=24))
        assert result.severity is Severity.WARN
        assert "prime-ember" in result.detail
        assert result.data["sessions"][0]["count"] == 2

    @pytest.mark.asyncio
    async def test_a_spike_that_has_not_lasted_is_not_a_warning(self, monkeypatch):
        monkeypatch.setattr(resource_checks, "load_average", lambda: (61.0, 8.0, 4.0))
        assert (await run_check("resources.load", _ctx(cores=24))).severity is Severity.OK

    @pytest.mark.asyncio
    async def test_the_ratio_is_configurable(self, monkeypatch):
        monkeypatch.setattr(resource_checks, "load_average", lambda: (30.0, 30.0, 30.0))
        monkeypatch.setattr(resource_checks, "pytest_processes", lambda: [])
        ok = await run_check("resources.load", _ctx(cores=24, load_warn_ratio=2.0))
        warn = await run_check("resources.load", _ctx(cores=24, load_warn_ratio=1.0))
        assert (ok.severity, warn.severity) == (Severity.OK, Severity.WARN)

    @pytest.mark.asyncio
    async def test_disabled_gating_is_info(self):
        assert (await run_check("resources.load", _ctx(enabled=False))).severity is Severity.INFO


class TestTestPressureCheck:
    @pytest.mark.asyncio
    async def test_ok_under_the_limit(self, monkeypatch):
        monkeypatch.setattr(
            resource_checks, "pytest_processes", lambda: [_proc(1)]
        )
        result = await run_check("resources.test_pressure", _ctx(max_pytest_processes=24))
        assert result.severity is Severity.OK

    @pytest.mark.asyncio
    async def test_names_the_sessions_responsible(self, monkeypatch):
        procs = [
            _proc(i, cwd="/repo/.aq/worktrees/slot-3", task_id="prime-ember") for i in range(60)
        ] + [_proc(100 + i, cwd="/repo/.aq/worktrees/slot-7", task_id="other") for i in range(40)]
        monkeypatch.setattr(resource_checks, "pytest_processes", lambda: procs)
        result = await run_check("resources.test_pressure", _ctx(max_pytest_processes=24))
        assert result.severity is Severity.WARN
        assert "slot-3" in result.detail and "prime-ember" in result.detail
        assert result.data["count"] == 100
        assert result.data["sessions"][0]["count"] == 60

    @pytest.mark.asyncio
    async def test_a_zero_limit_disables_the_check(self, monkeypatch):
        monkeypatch.setattr(
            resource_checks, "pytest_processes", lambda: [_proc(1)] * 99
        )
        result = await run_check("resources.test_pressure", _ctx(max_pytest_processes=0))
        assert result.severity is Severity.OK


class TestCgroupCheck:
    @pytest.mark.asyncio
    async def test_off_is_info_not_a_problem(self, monkeypatch):
        monkeypatch.setattr(
            resource_checks, "cgroup_delegation",
            lambda: CgroupDelegation(False, "no systemd"),
        )
        result = await run_check("resources.cgroups", _ctx())
        assert result.severity is Severity.INFO

    @pytest.mark.asyncio
    async def test_requested_but_undelegated_is_a_warning(self, monkeypatch):
        # The dangerous state: the operator believes hard limits are on.
        monkeypatch.setattr(
            resource_checks, "cgroup_delegation",
            lambda: CgroupDelegation(False, "Delegate=yes is not set"),
        )
        result = await run_check(
            "resources.cgroups", _ctx(cgroups=ResourceCgroupConfig(enabled=True))
        )
        assert result.severity is Severity.WARN
        assert "setup-cgroup-delegation.sh" in result.detail

    @pytest.mark.asyncio
    async def test_delegated_and_enabled_is_ok(self, monkeypatch):
        monkeypatch.setattr(
            resource_checks, "cgroup_delegation",
            lambda: CgroupDelegation(True, "ok"),
        )
        result = await run_check(
            "resources.cgroups",
            _ctx(cgroups=ResourceCgroupConfig(enabled=True, cpu_quota_percent=600)),
        )
        assert result.severity is Severity.OK
        assert result.data["cpu_quota_percent"] == 600


class TestProcessAttribution:
    def test_the_slot_is_read_off_the_cwd(self):
        proc = _proc(1, cwd="/home/j/dev/aq/.aq/worktrees/slot-3/src")
        assert proc.slot == "slot-3"

    def test_the_task_id_wins_when_both_are_present(self):
        proc = _proc(1, cwd="/x/.aq/worktrees/slot-3", task_id="prime-ember")
        assert proc.label == "slot-3 / prime-ember"

    def test_an_unattributable_process_still_gets_a_label(self):
        assert _proc(4242).label == "pid 4242"

    def test_summary_is_busiest_first(self):
        rows = summarize_by_session(
            [_proc(1, task_id="a"), _proc(2, task_id="b"), _proc(3, task_id="b")]
        )
        assert [r["count"] for r in rows] == [2, 1]
        assert rows[0]["task_id"] == "b"


class TestPytestProcessCounting:
    """xdist workers must be counted, or the pressure check is blind.

    A ``pytest -n 24`` run is one controller plus 24 ``execnet`` workers
    whose command line is ``python -u -c import sys;exec(...)`` — no
    "pytest" in it.  Matching on the name alone reports 1 where the truth is
    25, i.e. it misses exactly the fan-out it exists to catch.
    """

    def test_descendants_of_a_pytest_controller_are_counted(self, monkeypatch):
        import src.resources.procs as procs_mod

        table = {
            10: (1, "python -m pytest tests/ -n 3"),
            11: (10, "python -u -c import sys;exec(eval(sys.stdin.readline()))"),
            12: (10, "python -u -c import sys;exec(eval(sys.stdin.readline()))"),
            13: (12, "cc1plus -O2"),  # a grandchild the worker spawned
            20: (1, "vim notes.md"),
        }
        monkeypatch.setattr(procs_mod, "_cmdlines", lambda: table)
        monkeypatch.setattr(
            procs_mod,
            "_enrich",
            lambda pid, ppid, cmdline: ProcInfo(
                pid=pid, comm="x", cmdline=cmdline, ppid=ppid
            ),
        )
        assert sorted(p.pid for p in procs_mod.pytest_processes()) == [10, 11, 12, 13]

    def test_an_unrelated_process_is_not_swept_in(self, monkeypatch):
        import src.resources.procs as procs_mod

        table = {10: (1, "python -m pytest tests/"), 20: (1, "node server.js")}
        monkeypatch.setattr(procs_mod, "_cmdlines", lambda: table)
        monkeypatch.setattr(
            procs_mod,
            "_enrich",
            lambda pid, ppid, cmdline: ProcInfo(
                pid=pid, comm="x", cmdline=cmdline, ppid=ppid
            ),
        )
        assert [p.pid for p in procs_mod.pytest_processes()] == [10]

    def test_a_real_run_is_visible_to_the_scanner(self):
        # Anchors the /proc plumbing itself: this test process is running
        # under pytest, so the scan must find at least itself.
        import os

        from src.resources.procs import pytest_processes

        pids = {p.pid for p in pytest_processes()}
        assert pids, "no pytest process found while running under pytest"
        assert os.getpid() in pids or os.getppid() in pids
