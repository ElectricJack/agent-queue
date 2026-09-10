"""The curated default tuning: scaling, portability, and its own rationale.

Three things can silently rot here and each has a test below:

* the derived numbers stop making sense on a machine nobody develops on
  (a two-core laptop, a 64-core server),
* a value stops being *portable* — a path, a credential, a section the
  bundle allowlist does not carry — so a fresh install cannot receive it,
* a key gains a value but no explanation, so the guide and the config drift.
"""

from __future__ import annotations

import dataclasses

import pytest
import yaml

from src.config import (
    AgentsDefaultConfig,
    ArchiveConfig,
    AutoTaskConfig,
    IntegrationConfig,
    LLMLoggingConfig,
    MetricsConfig,
    MonitoringConfig,
    PauseRetryConfig,
    ResourceCgroupConfig,
    ResourcesConfig,
    SchedulingConfig,
    StateMachineConfig,
    SurfaceConfig,
    SwarmConfig,
    WorkGraphConfig,
    load_config,
)
from src.config_tuning import (
    DERIVED_KEYS,
    MAX_DERIVED_AGENTS,
    MAX_DERIVED_TEST_SLOTS,
    MachineResources,
    apply_tuning,
    emitted_keys,
    recommended_tuning,
    tuning_notes,
    tuning_plan,
)
from src.portable_config import PORTABLE_CONFIG_SECTIONS, curated_config

#: (cores, GiB) pairs spanning what people actually install on.
MACHINES = [
    MachineResources(1, 2.0),
    MachineResources(2, 4.0),
    MachineResources(4, 8.0),
    MachineResources(8, 16.0),
    MachineResources(16, 32.0),
    MachineResources(24, 64.0),
    MachineResources(64, 256.0),
]

#: Section name -> the dataclass that defines its keys.
SECTION_DATACLASSES = {
    "scheduling": SchedulingConfig,
    "pause_retry": PauseRetryConfig,
    "agents_config": AgentsDefaultConfig,
    "monitoring": MonitoringConfig,
    "archive": ArchiveConfig,
    "auto_task": AutoTaskConfig,
    "work_graph": WorkGraphConfig,
    "state_machine": StateMachineConfig,
    "surface": SurfaceConfig,
    "swarm": SwarmConfig,
    "resources": ResourcesConfig,
    "metrics": MetricsConfig,
    "llm_logging": LLMLoggingConfig,
    "integration": IntegrationConfig,
}


def _write_base_config(tmp_path) -> str:
    """A minimal config that loads — the thing a wizard has just written."""
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "database": {"url": "postgresql+asyncpg://u:p@localhost/aq"},
                "messaging_platform": "none",
                "data_dir": str(tmp_path / "data"),
            }
        ),
        encoding="utf-8",
    )
    return str(path)


# ── The machine model ────────────────────────────────────────────────────────


@pytest.mark.parametrize("machine", MACHINES, ids=lambda m: f"{m.cores}c{m.memory_gb:.0f}g")
def test_every_machine_gets_at_least_one_agent_and_one_test_slot(machine):
    """The floors are what make a small box usable rather than dead."""
    assert machine.concurrent_agents >= 1
    assert machine.test_slots >= 1
    assert machine.cpu_share >= 1


@pytest.mark.parametrize("machine", MACHINES, ids=lambda m: f"{m.cores}c{m.memory_gb:.0f}g")
def test_derived_values_stay_inside_their_caps(machine):
    assert machine.concurrent_agents <= MAX_DERIVED_AGENTS
    assert machine.test_slots <= MAX_DERIVED_TEST_SLOTS
    assert machine.size_class in {"small", "standard", "large"}


def test_concurrency_never_decreases_as_the_box_grows():
    """A bigger machine must never be told to run less work."""
    agents = [m.concurrent_agents for m in MACHINES]
    slots = [m.test_slots for m in MACHINES]
    assert agents == sorted(agents)
    assert slots == sorted(slots)


def test_ram_bounds_concurrency_independently_of_cores():
    """32 cores with 8 GiB is a memory-bound box, not a 10-agent fleet."""
    assert MachineResources(32, 8.0).concurrent_agents == 2
    assert MachineResources(4, 64.0).concurrent_agents == 1


def test_a_small_box_runs_exactly_one_agent_that_owns_the_machine():
    machine = MachineResources(2, 4.0)
    assert machine.size_class == "small"
    assert machine.concurrent_agents == 1
    # One agent, so the derived per-session share is the whole box: the
    # cap exists to divide a box between sessions, not to shrink a solo one.
    assert machine.cpu_share == machine.cores


def test_machine_rejects_impossible_values():
    with pytest.raises(ValueError):
        MachineResources(0, 8.0)
    with pytest.raises(ValueError):
        MachineResources(4, 0.0)


def test_detect_falls_back_to_a_guess_when_memory_is_unreadable(monkeypatch):
    import src.config_tuning as tuning

    monkeypatch.setattr(tuning.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(
        tuning.os, "sysconf", lambda name: (_ for _ in ()).throw(ValueError("no such conf"))
    )
    monkeypatch.setattr(tuning.platform, "system", lambda: "Linux")
    machine = MachineResources.detect()
    assert machine.cores == 8
    assert machine.memory_gb == 16.0  # 2 GiB per core


# ── The recommendation itself ────────────────────────────────────────────────


@pytest.mark.parametrize("machine", MACHINES, ids=lambda m: f"{m.cores}c{m.memory_gb:.0f}g")
def test_recommendation_only_names_real_config_keys(machine):
    """A typo'd key would be written to YAML and then silently ignored."""
    tuned = recommended_tuning(machine)
    for section, body in tuned.items():
        cls = SECTION_DATACLASSES.get(section)
        if cls is None:
            continue
        fields = {f.name for f in dataclasses.fields(cls)}
        assert set(body) <= fields, f"{section} has keys {set(body) - fields}"
    cgroup_fields = {f.name for f in dataclasses.fields(ResourceCgroupConfig)}
    assert set(tuned["resources"]["cgroups"]) <= cgroup_fields


@pytest.mark.parametrize("machine", MACHINES, ids=lambda m: f"{m.cores}c{m.memory_gb:.0f}g")
def test_recommendation_is_entirely_portable(machine):
    """It must survive the bundle allowlist with nothing excluded.

    This is the acceptance criterion in machine-checkable form: a new user
    can be handed these defaults without receiving anyone's projects,
    memory, credentials or paths.
    """
    tuned = recommended_tuning(machine)
    selected, excluded = curated_config(tuned)
    assert excluded == ()
    assert selected == tuned
    assert set(tuned) <= PORTABLE_CONFIG_SECTIONS


def test_recommendation_omits_the_keys_that_must_stay_derived():
    """Writing this box's core count is what makes a config unportable."""
    tuned = recommended_tuning(MachineResources(24, 64.0))
    assert "cores" not in tuned["resources"]
    assert "per_session_cpu_share" not in tuned["resources"]
    assert "test_workers" not in tuned["resources"]
    assert "global_max_active" not in tuned["swarm"]
    assert "global_token_budget_daily" not in tuned
    omitted = {note.key for note in DERIVED_KEYS}
    assert omitted & {"resources.cores", "swarm.global_max_active"}


@pytest.mark.parametrize("machine", MACHINES, ids=lambda m: f"{m.cores}c{m.memory_gb:.0f}g")
def test_tuned_config_loads_and_validates(tmp_path_factory, machine):
    """The recommendation has to be a config the daemon would accept."""
    tmp_path = tmp_path_factory.mktemp("tuned")
    path = _write_base_config(tmp_path)
    result = apply_tuning(path, machine)
    assert result["validation_errors"] == []
    assert result["applied"] is True

    config = load_config(path)
    assert config.resources.max_concurrent_agents == machine.concurrent_agents
    assert config.resources.test_slots == machine.test_slots
    # cores stayed unset, so the share is still derived from the real box.
    assert config.resources.cores is None
    assert config.swarm.enabled is True
    assert config.swarm.global_max_active is None
    assert config.metrics.slow_interval_seconds >= config.metrics.interval_seconds


def test_pricing_globs_match_most_specific_first(tmp_path):
    """Sonnet 4.6 bills differently from Sonnet 5; glob order decides."""
    path = _write_base_config(tmp_path)
    apply_tuning(path, MachineResources(8, 16.0))
    pricing = load_config(path).pricing
    assert pricing.match("claude-opus-5").input_per_mtok == 5.0
    assert pricing.match("claude-sonnet-5").input_per_mtok == 2.0
    assert pricing.match("claude-sonnet-4-6").input_per_mtok == 3.0
    assert pricing.match("claude-haiku-4-5").input_per_mtok == 1.0
    assert pricing.match("gpt-5") is None


def test_idle_cadences_are_slower_on_a_small_box():
    """'Quiet when idle' is the whole point of the small tier."""
    small = recommended_tuning(MachineResources(2, 4.0))
    large = recommended_tuning(MachineResources(24, 64.0))
    assert small["metrics"]["interval_seconds"] > large["metrics"]["interval_seconds"]
    assert small["metrics"]["flush_interval_seconds"] > large["metrics"]["flush_interval_seconds"]
    assert small["metrics"]["retain_seconds_1s"] < large["metrics"]["retain_seconds_1s"]
    assert (
        small["work_graph"]["gate_sweep_interval_seconds"]
        > large["work_graph"]["gate_sweep_interval_seconds"]
    )
    assert small["llm_logging"]["retention_days"] < large["llm_logging"]["retention_days"]


def test_the_repeat_failure_report_is_quieter_than_the_code_default():
    """One old failed task must not produce a notification every hour forever."""
    tuned = recommended_tuning(MachineResources(8, 16.0))
    shipped = MonitoringConfig().failed_blocked_report_interval_seconds
    assert tuned["monitoring"]["failed_blocked_report_interval_seconds"] > shipped


def test_token_exhaustion_waits_longer_than_the_code_default():
    """A spent quota window is hours; retrying every 5 min only makes noise."""
    tuned = recommended_tuning(MachineResources(8, 16.0))
    shipped = PauseRetryConfig().token_exhaustion_retry_seconds
    assert tuned["pause_retry"]["token_exhaustion_retry_seconds"] > shipped


def test_integration_defaults_never_merge_unreviewed_work_to_the_default_branch():
    tuned = recommended_tuning(MachineResources(8, 16.0))["integration"]
    assert tuned["default_mode"] == "pull_request"
    # `required` fails closed on an unreadable rollup, which on an install
    # with no CI yet means nothing would ever merge.
    assert tuned["merge_ci_policy"] == "warn"
    assert tuned["merge_require_up_to_date"] is True


# ── The rationale ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("machine", MACHINES, ids=lambda m: f"{m.cores}c{m.memory_gb:.0f}g")
def test_every_emitted_key_has_a_tuning_note(machine):
    """The guide can only stay honest if the notes are exhaustive."""
    keys = set(emitted_keys(machine))
    documented = {note.key for note in tuning_notes(machine)}
    assert keys - documented == set(), "undocumented tuning keys"
    assert documented - keys == set(), "notes for keys that are not emitted"


def test_every_note_says_both_why_and_how_to_override():
    for note in tuning_notes(MachineResources(8, 16.0)) + DERIVED_KEYS:
        assert note.why.strip(), note.key
        assert note.override.strip(), note.key


# ── Applying it ──────────────────────────────────────────────────────────────


def test_apply_keeps_sections_the_operator_already_customized(tmp_path):
    path = _write_base_config(tmp_path)
    from src.config_editor import read_raw_config, write_section

    write_section(path, "scheduling", {"rolling_window_hours": 72})

    result = apply_tuning(path, MachineResources(8, 16.0))
    assert "scheduling" in result["kept"]
    assert "scheduling" not in result["written"]
    assert read_raw_config(path)["scheduling"] == {"rolling_window_hours": 72}


def test_overwrite_replaces_a_customized_section(tmp_path):
    path = _write_base_config(tmp_path)
    from src.config_editor import read_raw_config, write_section

    write_section(path, "scheduling", {"rolling_window_hours": 72})

    result = apply_tuning(path, MachineResources(8, 16.0), overwrite=True)
    assert "scheduling" in result["written"]
    assert read_raw_config(path)["scheduling"]["rolling_window_hours"] == 24


def test_reapplying_is_a_no_op(tmp_path):
    path = _write_base_config(tmp_path)
    machine = MachineResources(8, 16.0)
    apply_tuning(path, machine)
    before = (tmp_path / "config.yaml").read_text(encoding="utf-8")

    second = apply_tuning(path, machine)
    assert second["applied"] is False
    assert second["written"] == []
    assert second["kept"] == []
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == before


def test_apply_leaves_the_file_untouched_when_the_result_would_not_validate(tmp_path, monkeypatch):
    path = _write_base_config(tmp_path)
    before = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    import src.config_tuning as tuning

    monkeypatch.setattr(
        tuning,
        "recommended_tuning",
        lambda machine=None: {"resources": {"max_concurrent_agents": -1}},
    )
    result = apply_tuning(path, MachineResources(8, 16.0))
    assert result["applied"] is False
    assert result["validation_errors"]
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == before


def test_plan_reports_add_keep_and_unchanged(tmp_path):
    machine = MachineResources(8, 16.0)
    tuned = recommended_tuning(machine)
    current = {
        "surface": tuned["surface"],  # already matches
        "scheduling": {"rolling_window_hours": 72},  # customized
    }
    plans = {p.section: p.action for p in tuning_plan(current, machine)}
    assert plans["surface"] == "unchanged"
    assert plans["scheduling"] == "keep"
    assert plans["resources"] == "add"

    overwritten = {p.section: p.action for p in tuning_plan(current, machine, overwrite=True)}
    assert overwritten["scheduling"] == "replace"
    assert overwritten["surface"] == "unchanged"


def test_apply_preserves_the_rest_of_the_document(tmp_path):
    """Tuning edits sections; it must not rewrite the connection settings."""
    path = _write_base_config(tmp_path)
    from src.config_editor import read_raw_config

    apply_tuning(path, MachineResources(8, 16.0))
    raw = read_raw_config(path)
    assert raw["database"]["url"] == "postgresql+asyncpg://u:p@localhost/aq"
    assert raw["messaging_platform"] == "none"


def test_tuning_still_applies_when_the_config_was_already_invalid(tmp_path):
    """A half-finished config must not silently forfeit its tuning.

    The wizard writes the file before the operator has necessarily filled in
    everything; refusing to tune because of an unrelated error would leave a
    fresh install on exactly the code defaults this module exists to replace.
    """
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "database": {"url": "postgresql+asyncpg://u:p@localhost/aq"},
                "messaging_platform": "discord",
                # A channel *name* where a snowflake belongs — a real thing a
                # human types during setup.
                "discord": {"bot_token": "t", "channel_id": "general"},
            }
        ),
        encoding="utf-8",
    )

    result = apply_tuning(str(path), MachineResources(8, 16.0))
    assert result["applied"] is True
    assert result["validation_errors"] == []
    assert any("channel_id" in err for err in result["preexisting_errors"])
    assert "resources" in result["written"]


# ── The CLI ──────────────────────────────────────────────────────────────────


def _run_tune(tmp_path, *args):
    from click.testing import CliRunner

    from src.cli.app import cli

    return CliRunner().invoke(
        cli, ["system", "config", "tune", "--config", str(tmp_path / "config.yaml"), *args]
    )


def test_cli_preview_writes_nothing(tmp_path):
    path = _write_base_config(tmp_path)
    before = open(path, encoding="utf-8").read()

    result = _run_tune(tmp_path, "--cores", "4", "--memory-gb", "8")
    assert result.exit_code == 0, result.output
    assert "1 concurrent agent(s)" in result.output
    assert "Nothing written" in result.output
    assert open(path, encoding="utf-8").read() == before


def test_cli_apply_writes_the_recommendation(tmp_path):
    _write_base_config(tmp_path)

    result = _run_tune(tmp_path, "--cores", "24", "--memory-gb", "64", "--apply")
    assert result.exit_code == 0, result.output
    assert load_config(str(tmp_path / "config.yaml")).resources.max_concurrent_agents == 8


def test_cli_refuses_to_invent_a_config_file(tmp_path):
    result = _run_tune(tmp_path)
    assert result.exit_code != 0
    assert "No config file at" in result.output


def test_cli_explain_prints_a_reason_and_an_override(tmp_path):
    _write_base_config(tmp_path)

    result = _run_tune(tmp_path, "--cores", "8", "--memory-gb", "16", "--explain")
    assert result.exit_code == 0, result.output
    assert "Why these values" in result.output
    assert "override:" in result.output
    assert "Deliberately left derived" in result.output
