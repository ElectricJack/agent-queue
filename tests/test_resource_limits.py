"""Resource gating layer 1 — env derivation, ``nice``, cgroup wrapping.

See ``docs/guides/resource-gating.md``.  The invariant every test here
protects is the one the 2026-09-01 incident broke: a session must never be
able to ask the *machine* how many cores it has and act on the answer.
"""

from __future__ import annotations

import pytest

from src.config import AppConfig, ResourceCgroupConfig, ResourcesConfig
from src.resources.limits import (
    THREAD_CAP_KEYS,
    XDIST_WORKERS_KEY,
    CgroupDelegation,
    cgroup_delegation,
    resolve_budget,
    session_env_caps,
    wrap_session_argv,
)


def _config(**kw) -> AppConfig:
    cfg = AppConfig()
    cfg.resources = ResourcesConfig(**kw)
    return cfg


class TestCpuShareDerivation:
    def test_share_is_cores_over_agents(self):
        res = ResourcesConfig(cores=24, max_concurrent_agents=8)
        assert res.cpu_share() == 3

    def test_explicit_share_wins(self):
        res = ResourcesConfig(cores=24, max_concurrent_agents=8, per_session_cpu_share=6)
        assert res.cpu_share() == 6

    def test_share_never_falls_below_one(self):
        # 4 cores across 8 agents floors at 1: `-n 0` is not something xdist
        # accepts and OMP_NUM_THREADS=0 is undefined.
        res = ResourcesConfig(cores=4, max_concurrent_agents=8)
        assert res.cpu_share() == 1

    def test_cores_defaults_to_the_machine(self):
        import os

        assert ResourcesConfig().core_count() == (os.cpu_count() or 1)

    def test_test_worker_cap_follows_the_share_by_default(self):
        res = ResourcesConfig(cores=24, max_concurrent_agents=6)
        assert res.test_worker_cap() == 4

    def test_test_worker_cap_can_be_pinned(self):
        res = ResourcesConfig(cores=24, max_concurrent_agents=6, test_workers=2)
        assert res.test_worker_cap() == 2


class TestSessionEnvCaps:
    def test_xdist_auto_is_capped(self):
        caps = session_env_caps(_config(cores=24, max_concurrent_agents=8))
        assert caps[XDIST_WORKERS_KEY] == "3"

    def test_every_thread_knob_is_capped(self):
        caps = session_env_caps(_config(cores=24, max_concurrent_agents=8))
        for key in THREAD_CAP_KEYS:
            assert caps[key] == "3", key

    def test_the_session_is_told_the_test_budget(self):
        caps = session_env_caps(_config(cores=24, max_concurrent_agents=8, test_slots=2))
        assert caps["AQ_TEST_SLOTS"] == "2"
        assert caps["AQ_TEST_WORKERS"] == "3"
        assert caps["AQ_CPU_CORES"] == "24"

    def test_disabled_gating_sets_nothing(self):
        assert session_env_caps(_config(enabled=False)) == {}

    def test_a_config_without_the_section_sets_nothing(self):
        class _Legacy:
            pass

        assert session_env_caps(_Legacy()) == {}

    def test_an_operator_pinned_harness_value_is_not_overridden(self):
        # The vault stopgap lives in the harness `env` block.  Moving the
        # derivation into code must not silently overrule an operator who
        # still wants their number.
        caps = session_env_caps(
            _config(cores=24, max_concurrent_agents=8),
            skip={XDIST_WORKERS_KEY: "4"},
        )
        assert XDIST_WORKERS_KEY not in caps
        assert caps["OMP_NUM_THREADS"] == "3"


class TestArgvWrapping:
    def test_nice_is_prepended(self):
        argv = wrap_session_argv(["claude", "--model", "x"], _config(session_nice=10))
        assert argv[:3] == ["nice", "-n", "10"]
        assert argv[3:] == ["claude", "--model", "x"]

    def test_zero_nice_disables_it(self):
        argv = wrap_session_argv(["claude"], _config(session_nice=0))
        assert argv == ["claude"]

    def test_disabled_gating_leaves_argv_alone(self):
        argv = wrap_session_argv(["claude"], _config(enabled=False))
        assert argv == ["claude"]

    def test_an_empty_argv_is_returned_untouched(self):
        assert wrap_session_argv([], _config()) == []

    def test_the_sh_wrapper_form_is_still_wrapped(self):
        # Oversized prompts turn argv into `sh -c <script> sh <file> …`;
        # nice has to end up outside that, not inside it.
        argv = wrap_session_argv(["sh", "-c", "script", "sh", "f", "claude"], _config())
        assert argv[0] == "nice"
        assert argv[3] == "sh"

    def test_cgroup_scope_wraps_outside_nice(self, monkeypatch):
        monkeypatch.setattr(
            "src.resources.limits.cgroup_delegation",
            lambda: CgroupDelegation(True, "test"),
        )
        cfg = _config(
            session_nice=10,
            cgroups=ResourceCgroupConfig(enabled=True, cpu_quota_percent=600, memory_max="6G"),
        )
        argv = wrap_session_argv(["claude"], cfg, scope_name="s-task-1")
        assert argv[0] == "systemd-run"
        assert "CPUQuota=600%" in argv
        assert "MemoryMax=6G" in argv
        assert argv.index("--unit") < argv.index("--")
        assert argv[argv.index("--") + 1 :] == ["nice", "-n", "10", "claude"]

    def test_cgroups_degrade_when_delegation_is_missing(self, monkeypatch):
        # A box without delegation must still launch agents — the whole
        # point of layer 3 being optional.
        monkeypatch.setattr(
            "src.resources.limits.cgroup_delegation",
            lambda: CgroupDelegation(False, "no delegation"),
        )
        cfg = _config(session_nice=0, cgroups=ResourceCgroupConfig(enabled=True))
        assert wrap_session_argv(["claude"], cfg) == ["claude"]

    def test_cgroups_off_never_probes(self, monkeypatch):
        def _boom():  # pragma: no cover - must not be called
            raise AssertionError("delegation probed while cgroups are disabled")

        monkeypatch.setattr("src.resources.limits.cgroup_delegation", _boom)
        assert wrap_session_argv(["claude"], _config(session_nice=0)) == ["claude"]


class TestBudget:
    def test_budget_reports_the_whole_picture(self):
        budget = resolve_budget(_config(cores=24, max_concurrent_agents=8, test_slots=2))
        assert (budget.cores, budget.cpu_share, budget.test_slots) == (24, 3, 2)

    def test_no_budget_when_disabled(self):
        assert resolve_budget(_config(enabled=False)) is None


class TestValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"cores": 0},
            {"max_concurrent_agents": 0},
            {"test_slots": 0},
            {"session_nice": 25},
            {"load_warn_ratio": 0},
            {"cgroups": ResourceCgroupConfig(cpu_quota_percent=0)},
        ],
    )
    def test_bad_values_are_rejected(self, kwargs):
        assert ResourcesConfig(**kwargs).validate()

    def test_defaults_are_valid(self):
        assert ResourcesConfig().validate() == []


class TestDelegationProbe:
    def test_missing_systemd_run_is_reported_not_raised(self, monkeypatch):
        cgroup_delegation.cache_clear()
        monkeypatch.setattr("src.resources.limits.shutil.which", lambda _: None)
        result = cgroup_delegation()
        assert result.available is False
        assert "systemd-run" in result.reason
        cgroup_delegation.cache_clear()

    def test_a_successful_probe_is_available(self, monkeypatch):
        import subprocess

        cgroup_delegation.cache_clear()
        monkeypatch.setattr("src.resources.limits.shutil.which", lambda _: "/usr/bin/systemd-run")
        monkeypatch.setattr("src.resources.limits.os.path.isdir", lambda _: True)
        monkeypatch.setattr(
            "src.resources.limits.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""),
        )
        assert cgroup_delegation().available is True
        cgroup_delegation.cache_clear()


class TestSessionLaunchIntegration:
    """The caps have to reach an actual launch, not just the helper."""

    def _spec(self, resources: ResourcesConfig):
        from src.sessions.harness_parser import Harness, ResumeSpec
        from src.sessions.spec import SessionSpecBuilder

        class _McpCfg:
            host = "127.0.0.1"
            port = 8081

        class _Cfg:
            mcp_server = _McpCfg()
            security = None
            data_dir = "/tmp/aq"

        class _Task:
            id = "task-1"
            project_id = "proj-1"

        class _Profile:
            id = "claude-opus"
            model = ""
            effort = ""
            harness = "claude"
            permission_mode = ""

        cfg = _Cfg()
        cfg.resources = resources
        harness = Harness(
            id="claude",
            name="Claude Code",
            command="claude",
            prompt_mode="arg",
            resume=ResumeSpec(style="flag", flag="--resume"),
            process_names=("claude",),
            max_argv_prompt_bytes=1024,
        )
        return SessionSpecBuilder(cfg).build_task_spec(
            task=_Task(),
            profile=_Profile(),
            harness=harness,
            work_dir="/tmp/wd",
            session_id="sess-1",
            instance_token="tok",
            epoch="1",
            api_url="http://127.0.0.1:8081",
            api_token="t",
        )

    def test_a_launched_session_carries_the_caps(self):
        spec = self._spec(ResourcesConfig(cores=24, max_concurrent_agents=8))
        assert spec.env[XDIST_WORKERS_KEY] == "3"
        assert spec.env["OMP_NUM_THREADS"] == "3"
        assert spec.env["AQ_TEST_WORKERS"] == "3"

    def test_a_launched_session_runs_niced(self):
        spec = self._spec(ResourcesConfig(session_nice=10))
        assert spec.command[:3] == ("nice", "-n", "10")
        assert spec.command[3] == "claude"

    def test_gating_off_leaves_the_launch_alone(self):
        import os

        spec = self._spec(ResourcesConfig(enabled=False))
        assert spec.command[0] == "claude"
        # The daemon's own environment is still inherited, so the assertion
        # is "nothing was *derived*", not "the key is absent".
        assert spec.env.get(XDIST_WORKERS_KEY) == os.environ.get(XDIST_WORKERS_KEY)
        assert "AQ_TEST_WORKERS" not in spec.env
