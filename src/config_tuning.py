"""Curated, resource-aware default tuning for a fresh install.

A new install gets AQ's *code* defaults, which are the values a single
developer box happened to need while the subsystem was written — eight
concurrent agents, two test slots, a one-second metrics sampler, an hourly
"still failed" report.  On a four-core laptop that is a machine that never
stops thrashing; on a 64-core server it is a fleet that never uses the box.

This module is the one place that turns *the machine you are installing on*
into a coherent set of tuning values.  Three properties matter:

**Portable.**  Nothing here reads the operator's projects, memory, vault or
paths.  Every value is either a constant or a function of core count and
RAM, so the result of :func:`recommended_tuning` passes
:func:`src.portable_config.curated_config` unchanged and can travel in an
``.aqbundle``.

**Explained.**  Every emitted key has a matching :class:`TuningNote` giving
the reason and the override — ``tests/test_config_tuning.py`` fails if one
is missing, so the guide cannot drift away from the values.

**Overridable.**  The recommendation is written into ``config.yaml`` as
ordinary sections.  Nothing consults this module at runtime; edit the file
(or ``aq system config set``) and the edit stands.  ``aq system config tune``
keeps sections you already customised unless you pass ``--overwrite``.

Guide: ``docs/guides/default-tuning.md``.
"""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass
from typing import Any

#: Cores budgeted per concurrent agent session.  A session is one harness
#: CLI plus whatever it shells out to; the expensive moments are its test
#: runs, which ``resources.cpu_share`` divides this same way.
CORES_PER_AGENT = 3
#: RAM budgeted per concurrent agent session, in GiB.  Agent CLIs are Node
#: processes in the 1–1.5 GiB range and a pytest-xdist fan-out adds more.
MEMORY_GB_PER_AGENT = 3.0
#: Ceiling on the derived fleet size.  Past a dozen concurrent sessions the
#: binding constraint stops being the box and starts being provider rate
#: limits, so a bigger machine should raise this deliberately.
MAX_DERIVED_AGENTS = 12
#: Ceiling on concurrent ``aq test`` runs box-wide.
MAX_DERIVED_TEST_SLOTS = 4

_DAY = 86400


@dataclass(frozen=True)
class MachineResources:
    """The two numbers every derived value below is a function of."""

    cores: int
    memory_gb: float

    def __post_init__(self) -> None:
        if self.cores < 1:
            raise ValueError("cores must be >= 1")
        if self.memory_gb <= 0:
            raise ValueError("memory_gb must be > 0")

    @classmethod
    def detect(cls) -> MachineResources:
        """This box, with conservative fallbacks when it cannot be read."""
        cores = os.cpu_count() or 1
        return cls(cores=max(1, cores), memory_gb=_detect_memory_gb(cores))

    @property
    def concurrent_agents(self) -> int:
        """How many agent sessions this box should run at once.

        The smaller of what the cores and the RAM support, floored at one so
        a small machine still runs AQ, and capped at
        :data:`MAX_DERIVED_AGENTS`.
        """
        by_cores = self.cores // CORES_PER_AGENT
        by_memory = int(self.memory_gb // MEMORY_GB_PER_AGENT)
        return max(1, min(by_cores, by_memory, MAX_DERIVED_AGENTS))

    @property
    def cpu_share(self) -> int:
        """Cores one session may assume it has — what ``-n auto`` resolves to."""
        return max(1, self.cores // self.concurrent_agents)

    @property
    def test_slots(self) -> int:
        """Concurrent ``aq test`` runs allowed box-wide.

        Each slot spends up to :attr:`cpu_share` xdist workers, so slots are
        deliberately scarcer than agents: the fleet's steady state is agents
        thinking, not agents all testing at the same instant.
        """
        return max(1, min(MAX_DERIVED_TEST_SLOTS, self.cores // 12))

    @property
    def size_class(self) -> str:
        """``small`` | ``standard`` | ``large`` — the cadence tier."""
        agents = self.concurrent_agents
        if agents <= 1:
            return "small"
        if agents <= 5:
            return "standard"
        return "large"

    def as_dict(self) -> dict[str, Any]:
        return {
            "cores": self.cores,
            "memory_gb": round(self.memory_gb, 1),
            "size_class": self.size_class,
            "concurrent_agents": self.concurrent_agents,
            "cpu_share": self.cpu_share,
            "test_slots": self.test_slots,
        }


def _detect_memory_gb(cores: int) -> float:
    """Physical RAM in GiB, or a 2 GiB-per-core guess when unreadable."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return pages * page_size / (1024**3)
    except (OSError, ValueError, AttributeError):
        pass
    if platform.system() == "Darwin":  # older macOS lacks SC_PHYS_PAGES
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            return int(out.stdout.strip()) / (1024**3)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return float(cores * 2)


@dataclass(frozen=True)
class TuningNote:
    """Why one emitted key holds the value it does, and when to change it."""

    key: str
    why: str
    override: str


def _by_size(machine: MachineResources, small: Any, standard: Any, large: Any) -> Any:
    return {"small": small, "standard": standard, "large": large}[machine.size_class]


def recommended_tuning(machine: MachineResources | None = None) -> dict[str, Any]:
    """The curated config sections for ``machine`` (this box by default).

    The return value is a plain mapping of top-level config section name to
    section body, ready to hand to :func:`src.config_editor.write_section`.
    Keys the code default already gets right are still emitted, because the
    point of the file is that an operator can see and edit the knobs; keys
    whose *correct* value is "derive it at runtime" (``resources.cores``,
    ``resources.test_workers``, ``swarm.global_max_active``) are deliberately
    omitted so they keep deriving.
    """
    machine = machine or MachineResources.detect()
    agents = machine.concurrent_agents

    return {
        "scheduling": {
            "rolling_window_hours": 24,
            "min_task_guarantee": True,
            "affinity_wait_seconds": _by_size(machine, 300, 120, 120),
        },
        "pause_retry": {
            "rate_limit_backoff_seconds": 60,
            "rate_limit_max_retries": 3,
            "rate_limit_max_backoff_seconds": 300,
            "token_exhaustion_retry_seconds": 900,
        },
        "agents_config": {
            "heartbeat_interval_seconds": 30,
            "stuck_timeout_seconds": _by_size(machine, 3600, 1800, 1800),
            "graceful_shutdown_timeout_seconds": 30,
        },
        "monitoring": {
            "stuck_task_threshold_seconds": 3600,
            "failed_blocked_report_interval_seconds": 21600,
        },
        "archive": {
            "enabled": True,
            "after_hours": 24.0,
            "statuses": ["COMPLETED", "FAILED", "BLOCKED"],
        },
        "auto_task": {"max_verification_retries": 2},
        "work_graph": {
            "gate_sweep_interval_seconds": _by_size(machine, 60, 30, 30),
            "conditional_autoclose": True,
            "container_sweep_interval_seconds": _by_size(machine, 120, 60, 60),
        },
        "state_machine": {"enforce": False},
        "surface": {"context_cost_ceiling_tokens": 8000},
        "swarm": {
            "enabled": True,
            "fresh_context_per_task": True,
            "claim_wait_max": 60,
            "max_starts_per_tick": _by_size(machine, 1, 2, 3),
            "max_drains_per_tick": 5,
            "scale_down_grace": _by_size(machine, 300, 120, 120),
            "prepare_timeout": 120,
            "max_filings_per_task": 20,
        },
        "resources": {
            "enabled": True,
            "max_concurrent_agents": agents,
            "session_nice": 10,
            "test_slots": machine.test_slots,
            "test_wait_timeout": 1800,
            "test_poll_interval": 2.0,
            "test_deselect_markers": (
                "not perf and not migration and not slow and not tmux and not integration"
            ),
            "load_warn_ratio": 1.0,
            "max_pytest_processes": max(4, machine.cores),
            "cgroups": {
                "enabled": False,
                "cpu_quota_percent": machine.cpu_share * 100,
                "memory_max": f"{max(2, int(machine.memory_gb // agents))}G",
            },
        },
        "metrics": {
            "enabled": True,
            "interval_seconds": _by_size(machine, 5.0, 2.0, 1.0),
            "slow_interval_seconds": _by_size(machine, 15.0, 10.0, 5.0),
            "flush_interval_seconds": _by_size(machine, 15.0, 10.0, 5.0),
            "rollup_interval_seconds": 60.0,
            "token_window_seconds": 300.0,
            "subagent_window_seconds": 3600.0,
            "retain_seconds_1s": _by_size(machine, 900, 1800, 3600),
            "retain_seconds_1m": _by_size(machine, 7 * _DAY, 14 * _DAY, 30 * _DAY),
            "retain_seconds_1h": _by_size(machine, 90 * _DAY, 180 * _DAY, 365 * _DAY),
        },
        "llm_logging": {
            "enabled": True,
            "retention_days": _by_size(machine, 7, 14, 30),
        },
        "integration": {
            "default_mode": "pull_request",
            "merge_ci_policy": "warn",
            "merge_required_checks": [],
            "merge_require_up_to_date": True,
        },
        "pricing": {"models": _pricing_rows()},
        "max_concurrent_playbook_runs": max(1, min(4, agents)),
    }


def _pricing_rows() -> list[dict[str, Any]]:
    """List prices for the Claude families, most specific glob first.

    Only used to put dollars on the token ledger; a missing or stale row
    costs nothing but an unpriced column.  Rates are Anthropic's published
    first-party list prices (checked 2026-06); Bedrock and Vertex bill
    separately and other providers are left for the operator to add.
    """
    return [
        {"model": "claude-fable-*", "input_per_mtok": 10.0, "output_per_mtok": 50.0},
        {"model": "claude-mythos-*", "input_per_mtok": 10.0, "output_per_mtok": 50.0},
        {"model": "claude-opus-*", "input_per_mtok": 5.0, "output_per_mtok": 25.0},
        {"model": "claude-sonnet-4-6*", "input_per_mtok": 3.0, "output_per_mtok": 15.0},
        {"model": "claude-sonnet-*", "input_per_mtok": 2.0, "output_per_mtok": 10.0},
        {"model": "claude-haiku-*", "input_per_mtok": 1.0, "output_per_mtok": 5.0},
    ]


def tuning_notes(machine: MachineResources | None = None) -> tuple[TuningNote, ...]:
    """One note per emitted key: the reason, and the override.

    ``test_every_emitted_key_has_a_tuning_note`` keeps this exhaustive, so
    this is the authoritative rationale — the guide quotes it rather than
    restating it.
    """
    machine = machine or MachineResources.detect()
    agents = machine.concurrent_agents
    tuned = recommended_tuning(machine)
    res = tuned["resources"]

    notes = [
        TuningNote(
            "scheduling.rolling_window_hours",
            "A day of history is what proportional credit needs to even out a "
            "fleet that works in bursts; a shorter window lets one busy hour "
            "starve a project for the rest of the day.",
            "Raise to 48–72 on a fleet whose projects go quiet for days.",
        ),
        TuningNote(
            "scheduling.min_task_guarantee",
            "Every active project gets at least one slot regardless of credit, "
            "so a project that has spent its share still makes progress.",
            "Set false only when one project must be able to take the whole box.",
        ),
        TuningNote(
            "scheduling.affinity_wait_seconds",
            f"{tuned['scheduling']['affinity_wait_seconds']}s: with "
            f"{agents} concurrent agent(s) a busy affinity agent is worth "
            "waiting for rather than reassigning; the wait is longer on a "
            "small box because there is no second agent to wait for.",
            "Lower it when throughput matters more than keeping a task on the "
            "agent that already has its context.",
        ),
        TuningNote(
            "pause_retry.rate_limit_backoff_seconds",
            "A provider 429 clears in seconds to minutes; a minute is long "
            "enough not to re-trip it and short enough not to idle the fleet.",
            "Raise on a shared API key where several fleets compete.",
        ),
        TuningNote(
            "pause_retry.rate_limit_max_retries",
            "Three in-process retries before the task is paused: enough to "
            "ride out a burst, few enough that a real outage reaches PAUSED "
            "where an operator can see it.",
            "0 pauses on the first 429 instead of retrying.",
        ),
        TuningNote(
            "pause_retry.rate_limit_max_backoff_seconds",
            "Caps the exponential backoff at five minutes so a retry loop "
            "cannot silently grow into an hour-long stall.",
            "Raise together with rate_limit_max_retries.",
        ),
        TuningNote(
            "pause_retry.token_exhaustion_retry_seconds",
            "15 min, not the code default of 5: a spent subscription quota "
            "window is measured in hours, so retrying every five minutes only "
            "produces failures to look at.",
            "Lower it on a pay-as-you-go key, where exhaustion is transient.",
        ),
        TuningNote(
            "agents_config.heartbeat_interval_seconds",
            "Half a minute keeps the stuck detector honest without turning "
            "every idle session into database traffic.",
            "Leave alone unless stuck detection is being tuned with it.",
        ),
        TuningNote(
            "agents_config.stuck_timeout_seconds",
            f"{tuned['agents_config']['stuck_timeout_seconds']}s without a "
            "heartbeat before a session is treated as stuck — longer on a "
            "small box, where a single long tool call competes with everything "
            "else for CPU.",
            "0 disables the timeout; raise it if healthy long tool calls are "
            "being reaped.",
        ),
        TuningNote(
            "agents_config.graceful_shutdown_timeout_seconds",
            "Half a minute for a harness to finish its turn before it is "
            "killed, so a restart does not routinely lose a message.",
            "Raise if shutdown regularly kills work mid-turn.",
        ),
        TuningNote(
            "monitoring.stuck_task_threshold_seconds",
            "An hour in one status is the point where a human should look; "
            "shorter reports on tasks that are merely slow.",
            "Lower on a fleet of short tasks.",
        ),
        TuningNote(
            "monitoring.failed_blocked_report_interval_seconds",
            "6 h, not the code default of 1 h. The repeat report is the main "
            "source of noise on an idle queue: one old failed task otherwise "
            "produces a notification every hour forever.",
            "Lower to 3600 when actively triaging failures.",
        ),
        TuningNote(
            "archive.enabled",
            "Terminal tasks are archived automatically; without it the active "
            "list and every list query grow forever.",
            "Set false only if an external process owns retention.",
        ),
        TuningNote(
            "archive.after_hours",
            "A day is long enough that a completed task is still in the list "
            "the next morning, short enough that the list stays readable.",
            "Raise when you review work less often than daily.",
        ),
        TuningNote(
            "archive.statuses",
            "COMPLETED, FAILED and BLOCKED are terminal; nothing else is safe "
            "to archive on a timer.",
            "Drop FAILED to keep failures in the active list until triaged.",
        ),
        TuningNote(
            "auto_task.max_verification_retries",
            "Two reopens for a git verification failure: one for a genuine "
            "flake, and a stop before a broken task reopens forever.",
            "0 turns a verification failure into a single terminal failure.",
        ),
        TuningNote(
            "work_graph.gate_sweep_interval_seconds",
            f"{tuned['work_graph']['gate_sweep_interval_seconds']}s between "
            "gate sweeps — the delay between a gate becoming satisfiable and "
            "work starting, paid every cycle whether or not anything is "
            "waiting, so it is halved in frequency on a small box.",
            "0 disables the sweep entirely.",
        ),
        TuningNote(
            "work_graph.conditional_autoclose",
            "Contingency tasks whose conditional-blocks dependency completed "
            "can never run again; without disposal they rot in the queue.",
            "Inert on a graph with no conditional-blocks edges.",
        ),
        TuningNote(
            "work_graph.container_sweep_interval_seconds",
            f"{tuned['work_graph']['container_sweep_interval_seconds']}s "
            "backstop for container settlement — a backstop, not the primary "
            "path, so it can be slow.",
            "0 disables it.",
        ),
        TuningNote(
            "state_machine.enforce",
            "Warn-only on a new install: enforcement turns a status "
            "transition an integration or playbook did not anticipate into a "
            "hard failure.",
            "Turn on once the fleet's own transitions are clean.",
        ),
        TuningNote(
            "surface.context_cost_ceiling_tokens",
            "The threshold `aq doctor` warns at for prompt context cost; 8k "
            "is roughly where an agent's context stops being mostly task.",
            "Raise on a deployment with deliberately large system prompts.",
        ),
        TuningNote(
            "swarm.enabled",
            "Pull-based pools are how workers get work: a pool worker claims "
            "the next ready task itself instead of waiting to be pushed one. "
            "This is the shipped work model; the code default is off only "
            "because it post-dates the push path.",
            "Set false to keep every profile on lifecycle: task (push).",
        ),
        TuningNote(
            "swarm.fresh_context_per_task",
            "Retiring the conversation after each task is what stops one "
            "task's context from leaking into the next on a shared worker.",
            "Set false to keep one conversation across tasks (cheaper, "
            "noticeably more cross-talk).",
        ),
        TuningNote(
            "swarm.claim_wait_max",
            "A minute is the longest a `task claim --wait` may block: long "
            "enough to avoid a claim storm on an empty queue, short enough "
            "that a drain request is noticed promptly.",
            "Lower if drains need to be picked up faster.",
        ),
        TuningNote(
            "swarm.max_starts_per_tick",
            f"{tuned['swarm']['max_starts_per_tick']} per 5s cycle, scaled to "
            f"the fleet size ({agents}): ramping a pool gradually keeps a "
            "queue that suddenly fills from starting every worker at once.",
            "Raise for a faster ramp on a machine with headroom.",
        ),
        TuningNote(
            "swarm.max_drains_per_tick",
            "Draining is cheap and idle workers cost money, so shedding is "
            "allowed to be faster than starting.",
            "Lower if drains disrupt long-running work.",
        ),
        TuningNote(
            "swarm.scale_down_grace",
            f"{tuned['swarm']['scale_down_grace']}s of measured surplus before "
            "a worker is drained, longer on a small box where starting a "
            "replacement costs proportionally more.",
            "Raise on a bursty queue to stop start/drain churn.",
        ),
        TuningNote(
            "swarm.prepare_timeout",
            "A claim stuck in 'preparing' longer than two minutes is a dead "
            "session, and its task must go back to the frontier.",
            "Raise if workspace acquisition is genuinely slow here.",
        ),
        TuningNote(
            "swarm.max_filings_per_task",
            "A worker may file 20 discovered tasks against the one it holds — "
            "generous for real findings, bounded against a loop.",
            "Lower if worker-filed tasks are noisy.",
        ),
        TuningNote(
            "resources.enabled",
            "Resource gating is what keeps N agents from becoming N×cores "
            "test processes. Off, nothing below applies.",
            "Leave on; the individual layers are separately tunable.",
        ),
        TuningNote(
            "resources.max_concurrent_agents",
            f"{agents} = min(cores/{CORES_PER_AGENT}, "
            f"RAM/{MEMORY_GB_PER_AGENT:g}GiB) on {machine.cores} cores and "
            f"{machine.memory_gb:.0f} GiB, floored at 1 and capped at "
            f"{MAX_DERIVED_AGENTS}. It is also the denominator of every "
            f"session's CPU share, so each session assumes {machine.cpu_share} "
            "core(s).",
            "Raising it shrinks every session's test parallelism; raise "
            "per_session_cpu_share or test_workers with it if that is not "
            "what you meant.",
        ),
        TuningNote(
            "resources.session_nice",
            "Agents run at nice 10 so the daemon, dashboard and tmux stay "
            "responsive when the box is saturated.",
            "0 disables the renice.",
        ),
        TuningNote(
            "resources.test_slots",
            f"{machine.test_slots} concurrent `aq test` run(s) box-wide, each "
            f"spending up to {machine.cpu_share} xdist worker(s). Slots are "
            "scarcer than agents on purpose: the fleet's steady state is "
            "agents thinking, not all of them testing at once.",
            "Raise only if `aq test --aq-status` shows slots idle while "
            "agents queue for them.",
        ),
        TuningNote(
            "resources.test_wait_timeout",
            "Half an hour for a slot before `aq test` gives up with exit 75, "
            "which is retryable and not a test failure.",
            "Lower on a fleet where waiting is worse than retrying.",
        ),
        TuningNote(
            "resources.test_poll_interval",
            "Two seconds between slot polls: invisible next to a test run, "
            "cheap enough to run in every waiting session.",
            "Leave alone.",
        ),
        TuningNote(
            "resources.test_deselect_markers",
            "`aq test` deselects the slow-by-nature markers unless the caller "
            "passes its own -m, so a focused run stays focused.",
            "Pass -m yourself, or --aq-all-markers, when the change is about "
            "those suites.",
        ),
        TuningNote(
            "resources.load_warn_ratio",
            "`aq doctor` warns when the 5-minute load average exceeds one per "
            "core — the point where added agents stop adding throughput.",
            "Raise on a box that is expected to run hot.",
        ),
        TuningNote(
            "resources.max_pytest_processes",
            f"{res['max_pytest_processes']} box-wide pytest processes before "
            "`aq doctor` warns: one per core, which is what the slot and "
            "worker caps above should already produce.",
            "Raise together with test_slots.",
        ),
        TuningNote(
            "resources.cgroups.enabled",
            "Off: hard per-session limits need a one-time root step "
            "(Delegate=yes on the daemon's user slice). The env caps and nice "
            "above work without it.",
            "See scripts/setup-cgroup-delegation.sh, then set true.",
        ),
        TuningNote(
            "resources.cgroups.cpu_quota_percent",
            f"{res['cgroups']['cpu_quota_percent']}% = {machine.cpu_share} "
            "core(s), matching the CPU share each session is already told it "
            "has, so enabling cgroups enforces the same number rather than a "
            "different one.",
            "100 = one core. Inert while cgroups.enabled is false.",
        ),
        TuningNote(
            "resources.cgroups.memory_max",
            f"{res['cgroups']['memory_max']} per session — this box's RAM "
            "divided by the fleet size, floored at 2G.",
            "Inert while cgroups.enabled is false.",
        ),
        TuningNote(
            "metrics.enabled",
            "The fleet metrics sampler is what the dashboard's Metrics tab and "
            "every 'is the fleet actually working' question read.",
            "Set false to stop sampling entirely.",
        ),
        TuningNote(
            "metrics.interval_seconds",
            f"{tuned['metrics']['interval_seconds']}s between cheap samples. "
            "A per-second sampler is a per-second write whether or not "
            "anything is running, which is the single largest idle cost on a "
            "small box.",
            "1.0 gives the finest live chart; raise it to go quieter.",
        ),
        TuningNote(
            "metrics.slow_interval_seconds",
            f"{tuned['metrics']['slow_interval_seconds']}s for the samples "
            "that range-scan the token ledger and sub-agent events; their "
            "values are carried forward between ticks, so the per-sample row "
            "stays complete.",
            "Must be >= interval_seconds.",
        ),
        TuningNote(
            "metrics.flush_interval_seconds",
            f"{tuned['metrics']['flush_interval_seconds']}s of samples per "
            "commit. A commit is an fsync; batching turns a per-second fsync "
            "into one per window, and only the newest few seconds of stored "
            "history are at risk if the daemon is killed.",
            "Lower only if stored history must be current to the second.",
        ),
        TuningNote(
            "metrics.rollup_interval_seconds",
            "Minute roll-ups on the minute, which is what the 1m tier means.",
            "Leave alone.",
        ),
        TuningNote(
            "metrics.token_window_seconds",
            "Token rates are measured over five minutes and scaled to per "
            "minute: a harness flushes a whole turn's usage in one write, so "
            "a trailing-minute figure reads 0 for most seconds and spikes "
            "after each flush.",
            "Minimum 60. The unsmoothed figures remain as *_per_min_1m.",
        ),
        TuningNote(
            "metrics.subagent_window_seconds",
            "Sub-agent spawns are counted over an hour and scaled to per "
            "hour; the alternative reading — children open right now — is "
            "near zero on a pool fleet.",
            "Minimum 60.",
        ),
        TuningNote(
            "metrics.retain_seconds_1s",
            f"{tuned['metrics']['retain_seconds_1s']}s of per-second detail. "
            "This tier is the bulk of the metrics table; it answers 'what "
            "happened just now', which is a short question.",
            "Raise if you routinely debug incidents hours after the fact.",
        ),
        TuningNote(
            "metrics.retain_seconds_1m",
            f"{tuned['metrics']['retain_seconds_1m'] // _DAY} days of minutes "
            "— the resolution trend questions are actually asked at.",
            "Raise for longer-range capacity work.",
        ),
        TuningNote(
            "metrics.retain_seconds_1h",
            f"{tuned['metrics']['retain_seconds_1h'] // _DAY} days of hours, "
            "which is cheap and covers seasonal comparison.",
            "0 keeps nothing at this tier.",
        ),
        TuningNote(
            "llm_logging.enabled",
            "The JSONL record of LLM inputs and outputs is the only way to "
            "answer 'what did the model actually see' after the fact.",
            "Set false where prompt content may not be written to disk.",
        ),
        TuningNote(
            "llm_logging.retention_days",
            f"{tuned['llm_logging']['retention_days']} days, scaled down on a "
            "small box: these files are the fastest-growing thing AQ writes.",
            "Raise when debugging model behaviour over a long window.",
        ),
        TuningNote(
            "integration.default_mode",
            "pull_request: the last link of the policy chain (task override → "
            "project policy → this). It means worker output lands on a branch "
            "and a PR, never straight onto the default branch.",
            "direct only for a deployment that deliberately runs with no "
            "review at all.",
        ),
        TuningNote(
            "integration.merge_ci_policy",
            "warn: the fleet's own merge path asks GitHub for the check "
            "rollup and records the verdict, but still merges. A new install "
            "usually has no CI yet, and 'required' with an unreadable rollup "
            "fails closed — nothing would ever merge.",
            "Move to required once the default branch is reliably green; off "
            "stops asking.",
        ),
        TuningNote(
            "integration.merge_required_checks",
            "Empty means every check in the rollup must be green — the strict "
            "reading, and the right default before you know which arms of a "
            "matrix are advisory.",
            "Name checks explicitly to let advisory arms fail.",
        ),
        TuningNote(
            "integration.merge_require_up_to_date",
            "A green rollup only proves the head passed against the base as it "
            "was when the run started; two PRs green on the same old base can "
            "merge into a default branch no run has tested.",
            "Set false on a low-traffic repo where the extra CI re-runs cost "
            "more than the risk.",
        ),
        TuningNote(
            "pricing.models",
            "Published Claude list prices so the token ledger and the "
            "dashboard show dollars instead of blank cost columns. Globs, so "
            "a new model in a known family is priced on arrival.",
            "Rates change and partner platforms bill differently — check "
            "them, and add rows for any non-Claude provider you run.",
        ),
        TuningNote(
            "max_concurrent_playbook_runs",
            f"{tuned['max_concurrent_playbook_runs']}, scaled to the fleet "
            f"size ({agents}): playbook runs compete with agent sessions for "
            "the same box and the same provider quota.",
            "Raise if playbook runs queue behind each other while the box is "
            "idle.",
        ),
    ]
    return tuple(notes)


#: Keys deliberately *not* emitted, and why — surfaced by ``aq system config
#: tune --explain`` so an operator can see that the omission is a decision.
DERIVED_KEYS: tuple[TuningNote, ...] = (
    TuningNote(
        "resources.cores",
        "Omitted so it keeps resolving to os.cpu_count() at read time — "
        "writing this box's core count is exactly the machine-specific value "
        "that makes a config unportable.",
        "Set it only to pretend the box is smaller than it is.",
    ),
    TuningNote(
        "resources.per_session_cpu_share",
        "Omitted so it stays derived from cores ÷ max_concurrent_agents.",
        "Set it to decouple a session's CPU share from the fleet size.",
    ),
    TuningNote(
        "resources.test_workers",
        "Omitted so the `aq test` -n cap keeps following the CPU share.",
        "Set it to cap test parallelism independently.",
    ),
    TuningNote(
        "swarm.global_max_active",
        "Omitted so the box-wide pool ceiling inherits "
        "resources.max_concurrent_agents. An explicit 0 is refused: "
        "swarm.enabled: false is the honest way to say 'no pool workers'.",
        "Set an integer to run more or fewer pool workers than the fleet size "
        "without changing every session's test parallelism.",
    ),
    TuningNote(
        "global_token_budget_daily",
        "Omitted: a shipped spend cap is either meaninglessly high or a "
        "surprise stop mid-task. Unset means no cap.",
        "Set it once you know what a normal day costs.",
    ),
)


def emitted_keys(machine: MachineResources | None = None) -> tuple[str, ...]:
    """Every dotted key :func:`recommended_tuning` writes, in emission order."""
    keys: list[str] = []

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(f"{prefix}.{key}" if prefix else key, child)
        else:
            keys.append(prefix)

    for section, body in recommended_tuning(machine).items():
        # A list-valued key (archive.statuses, pricing.models) is one knob,
        # not one knob per element.
        walk(section, body)
    return tuple(keys)


@dataclass(frozen=True)
class SectionPlan:
    """What ``aq system config tune`` would do to one config section."""

    section: str
    action: str  # "add" | "replace" | "keep" | "unchanged"
    recommended: Any
    current: Any = None


def tuning_plan(
    current: dict[str, Any],
    machine: MachineResources | None = None,
    *,
    overwrite: bool = False,
) -> tuple[SectionPlan, ...]:
    """Reconcile the recommendation against a config the operator already has.

    A section the operator has not written is ``add``.  A section they have
    written is ``keep`` — their customisation wins — unless ``overwrite``,
    which makes it ``replace``.  A section already byte-equal to the
    recommendation is ``unchanged`` either way, so re-running the command is
    a no-op rather than a rewrite.
    """
    plans: list[SectionPlan] = []
    for section, body in recommended_tuning(machine).items():
        if section not in current:
            plans.append(SectionPlan(section, "add", body))
        elif current[section] == body:
            plans.append(SectionPlan(section, "unchanged", body, current[section]))
        else:
            plans.append(
                SectionPlan(
                    section, "replace" if overwrite else "keep", body, current[section]
                )
            )
    return tuple(plans)


def apply_tuning(
    config_path: str,
    machine: MachineResources | None = None,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Validate the tuned config, then write the sections it changes.

    Validation is the same temp-file ``load_config`` preflight a portable
    bundle import runs: the merged document must load before any of it is
    written, so a recommendation that would break the config leaves the file
    untouched.

    Only errors the tuning *introduces* count.  The config this runs against
    is often one the wizard has just written and the operator has not
    finished — a placeholder Discord channel id, an unset key — and refusing
    to tune a config that was already invalid for an unrelated reason would
    silently leave a fresh install on the code defaults, which is the exact
    outcome this module exists to avoid.  Pre-existing errors come back in
    ``preexisting_errors`` so the caller can still surface them.
    """
    from src.config_editor import read_raw_config, write_section
    from src.portable_config import validate_candidate_config

    current = read_raw_config(config_path)
    plans = tuning_plan(current, machine, overwrite=overwrite)
    writes = {p.section: p.recommended for p in plans if p.action in ("add", "replace")}

    candidate = dict(current)
    candidate.update(writes)
    errors = validate_candidate_config(config_path, candidate)
    preexisting: list[str] = []
    if errors:
        preexisting = validate_candidate_config(config_path, current)
        introduced = [error for error in errors if error not in set(preexisting)]
        if introduced:
            return {
                "applied": False,
                "validation_errors": introduced,
                "preexisting_errors": preexisting,
                "sections": [p.section for p in plans],
            }

    for section, body in writes.items():
        write_section(config_path, section, body)
    return {
        "applied": bool(writes),
        "validation_errors": [],
        "preexisting_errors": preexisting,
        "written": sorted(writes),
        "kept": sorted(p.section for p in plans if p.action == "keep"),
        "unchanged": sorted(p.section for p in plans if p.action == "unchanged"),
    }
