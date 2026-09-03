"""Every field a config dataclass declares must be reachable from YAML.

``load_config`` maps each YAML section onto its dataclass with an explicit
keyword list.  A field that is added to the dataclass but not to that list
loads silently at its code default: the operator writes the key, nothing
happens, and nothing says so.  That is how ``playbooks.v2_api``,
``v2_activation_writes``, ``v2_compiler_enabled``, ``contract_intent`` and
``cancellation_grace_seconds`` became unreachable (steady-ridge-97).

These tests close the loop from the other end: they write a non-default value
for every scalar field of a section, load the file, and compare.
"""

from __future__ import annotations

import dataclasses
import re

import pytest
import yaml

from src.config import AppConfig, ConfigValidationError, load_config

# YAML key for sections whose ``AppConfig`` attribute is spelled differently.
SECTION_YAML_KEY = {"agents_config": "agents"}

# Fields whose valid values are an enum: a mechanically "different" value
# would be rejected by ``validate()`` rather than exercising the loader.
NON_DEFAULT_VALUES = {
    ("playbooks", "v2_pending_event_on_overflow"): "reject_new",
    ("playbooks", "v2_pending_event_replay_on_activation"): "automatic",
}

# Sections that do not read every field they declare, as of steady-ridge-97.
# ``playbooks`` is deliberately absent — it is the section that task fixed, and
# ``test_playbooks_section_reads_every_field_it_declares`` holds it at zero.
# The rest are the same bug in other sections; the registry is exact in both
# directions, so fixing one of them fails this test until the entry is removed.
KNOWN_LOADER_GAPS: dict[str, list[str]] = {
    # Read from no YAML key at all.
    "chat_analyzer": [
        "dismiss_cooldown_seconds",
        "in_flight_min_confidence",
        "min_confidence",
    ],
    "streams": [
        "buffer_max_bytes",
        "buffer_max_lines",
        "client_reconnect_attempts",
        "kill_grace_seconds",
        "max_concurrent_per_session",
        "retention_seconds",
    ],
    # Read, but only a subset of their fields.
    "logging": [
        "console_format",
        "log_file",
        "log_file_backup_count",
        "log_file_max_bytes",
    ],
    "monitoring": ["failed_blocked_report_interval_seconds"],
    "memory": [
        "auto_generate_notes",
        "compact_archive_days",
        "compact_llm_model",
        "compact_llm_provider",
        "compact_recent_days",
        "consolidation_auto_trigger",
        "consolidation_cooldown_minutes",
        "consolidation_enabled",
        "consolidation_growth_threshold",
        "consolidation_max_batch_size",
        "consolidation_min_age_hours",
        "consolidation_model",
        "consolidation_provider",
        "consolidation_schedule",
        "consolidation_similarity_threshold",
        "context_include_recent",
        "context_max_tokens",
        "deep_consolidation_schedule",
        "fact_extraction_enabled",
        "factsheet_in_context",
        "index_docs",
        "index_knowledge",
        "index_project_docs",
        "index_specs",
        "notes_inform_profile",
        "profile_enabled",
        "profile_max_size",
        "revision_enabled",
        "revision_model",
        "revision_provider",
        "spec_watcher_enabled",
        "spec_watcher_max_excerpt_lines",
        "spec_watcher_poll_interval",
        "topic_detection_enabled",
        "topic_max_chars_per_file",
        "topic_max_knowledge_files",
        "topic_memory_budget_chars",
        "topic_memory_enabled",
        "topic_memory_max_results",
    ],
    "metrics": ["subagent_window_seconds", "token_window_seconds"],
}

_ERROR_RE = re.compile(r"\[(\w+)\] (\w+):")


def _non_default(section: str, name: str, current: object) -> object | None:
    """A value distinguishable from ``current``, or None if not comparable."""
    override = NON_DEFAULT_VALUES.get((section, name))
    if override is not None:
        return override
    if isinstance(current, bool):
        return not current
    if isinstance(current, int):
        return current + 7
    if isinstance(current, float):
        return current + 0.5
    if isinstance(current, str):
        return f"{current}-probe" if current else "probe"
    return None  # lists, dicts, None and nested dataclasses are not probed


def _round_trip(tmp_path, section: str, subconfig) -> tuple[list[str], list[str]]:
    """Return ``(dropped, unchecked)`` field names for one config section.

    ``dropped`` did not survive the round trip — the loader ignored the key.
    ``unchecked`` were withdrawn because the probe value failed validation
    (an enum without an entry in ``NON_DEFAULT_VALUES``, say); they prove
    nothing either way, so a caller that wants a strict answer asserts the
    list is empty.
    """
    wanted = {}
    for f in dataclasses.fields(subconfig):
        value = _non_default(section, f.name, getattr(subconfig, f.name))
        if value is not None:
            wanted[f.name] = value

    unchecked: list[str] = []
    for _ in range(len(wanted) + 1):
        path = tmp_path / f"config-{section}.yaml"
        path.write_text(
            yaml.dump(
                {
                    "database_path": str(tmp_path / "test.db"),
                    "discord": {"bot_token": "t", "guild_id": "1"},
                    SECTION_YAML_KEY.get(section, section): dict(wanted),
                }
            )
        )
        try:
            config = load_config(str(path))
        except ConfigValidationError as exc:
            rejected = set()
            for error in exc.errors:
                match = _ERROR_RE.match(str(error))
                if match and match.group(2) in wanted:
                    rejected.add(match.group(2))
            if not rejected:
                pytest.fail(f"{section}: config probe could not be validated: {exc}")
            for name in rejected:
                wanted.pop(name)
                unchecked.append(name)
            continue

        loaded = getattr(config, section)
        dropped = [name for name, value in wanted.items() if getattr(loaded, name) != value]
        return sorted(dropped), sorted(unchecked)

    raise AssertionError(f"{section}: validation never converged")


def test_playbooks_section_reads_every_field_it_declares(tmp_path):
    """The regression guard for steady-ridge-97.

    A new ``PlaybooksConfig`` field with no matching ``pb.get(...)`` in
    ``load_config`` fails here rather than shipping as a dead config key.
    """
    dropped, unchecked = _round_trip(tmp_path, "playbooks", AppConfig().playbooks)
    assert dropped == []
    assert unchecked == []


@pytest.mark.parametrize(
    "flag",
    [
        "v2_api",
        "v2_activation_writes",
        "v2_compiler_enabled",
        "contract_intent",
    ],
)
def test_playbooks_boolean_flags_load_from_yaml(tmp_path, flag):
    """Each flag the loader used to drop, set explicitly against its default."""
    default = getattr(AppConfig().playbooks, flag)
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.dump(
            {
                "database_path": str(tmp_path / "test.db"),
                "discord": {"bot_token": "t", "guild_id": "1"},
                "playbooks": {"enabled": True, flag: not default},
            }
        )
    )
    assert getattr(load_config(str(path)).playbooks, flag) is (not default)


def test_playbooks_cancellation_grace_seconds_loads_from_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.dump(
            {
                "database_path": str(tmp_path / "test.db"),
                "discord": {"bot_token": "t", "guild_id": "1"},
                "playbooks": {"enabled": True, "cancellation_grace_seconds": 0},
            }
        )
    )
    assert load_config(str(path)).playbooks.cancellation_grace_seconds == 0


def test_config_section_loader_gaps_match_the_recorded_registry(tmp_path):
    """Generalisation of the above over every section of ``AppConfig``.

    The comparison is exact: a section that grows a new unreachable field
    fails, and so does one whose recorded gap is fixed without updating the
    registry.  See ``KNOWN_LOADER_GAPS`` for what is outstanding today.
    """
    base = AppConfig()
    observed: dict[str, list[str]] = {}
    for f in dataclasses.fields(base):
        subconfig = getattr(base, f.name)
        if not dataclasses.is_dataclass(subconfig):
            continue
        dropped, _ = _round_trip(tmp_path, f.name, subconfig)
        if dropped:
            observed[f.name] = dropped

    assert observed == KNOWN_LOADER_GAPS
