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
    ("playbooks", "v1_admission"): "closed",
    # ``"ollama-probe"`` is not one of the five accepted providers, and the
    # probe sets ``enabled: true``, which is when that check applies.
    ("memory", "embedding_provider"): "openai",
}

# Sections that do not read every field they declare.
#
# Empty, and meant to stay that way.  steady-ridge-97 fixed ``playbooks``;
# grand-glacier-97 closed the remaining six (``chat_analyzer`` -- since
# deleted as dead by prime-torrent-81 -- and ``streams`` were read from no
# YAML key at all; ``logging``, ``monitoring``, ``memory`` and ``metrics``
# read a subset) by deriving the loader's keyword list from
# ``dataclasses.fields()`` instead of hand-writing it.  An entry belongs here
# only with a comment saying why that field is deliberately not operator-
# settable; the comparison below is exact in both directions, so a new gap
# fails the test and so does a recorded gap fixed without deleting its entry.
KNOWN_LOADER_GAPS: dict[str, list[str]] = {}

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
    registry.  ``KNOWN_LOADER_GAPS`` is empty, so today this reads: every
    scalar field of every ``AppConfig`` section round-trips through YAML.
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


def test_streams_section_loads_from_yaml(tmp_path):
    """One of the two sections ``load_config`` read from no YAML key at all.

    Before grand-glacier-97 neither ``streams`` nor ``chat_analyzer`` had an
    ``if "<section>" in raw`` branch, so every field stayed at its code
    default no matter what was written.  ``chat_analyzer`` has since been
    deleted outright (prime-torrent-81); ``streams`` is the half that had a
    consumer, so it is the half this guards.
    """
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.dump(
            {
                "database_path": str(tmp_path / "test.db"),
                "discord": {"bot_token": "t", "guild_id": "1"},
                "streams": {
                    "buffer_max_lines": 100,
                    "buffer_max_bytes": 4096,
                    "retention_seconds": 30,
                    "kill_grace_seconds": 1.5,
                    "max_concurrent_per_session": 1,
                    "client_reconnect_attempts": 2,
                },
            }
        )
    )
    config = load_config(str(path))

    assert config.streams.buffer_max_lines == 100
    assert config.streams.buffer_max_bytes == 4096
    assert config.streams.retention_seconds == 30
    assert config.streams.kill_grace_seconds == 1.5
    assert config.streams.max_concurrent_per_session == 1
    assert config.streams.client_reconnect_attempts == 2


def test_partial_section_keeps_every_default_the_dataclass_declares(tmp_path):
    """Supplying one key must not reset its neighbours.

    The hand-written keyword lists repeated each default at the call site,
    which is how ``logging.format`` came to load as ``"text"`` while
    :class:`LoggingConfig` declared ``"dev"``.  Deriving the keywords from
    the dataclass leaves untouched keys to the dataclass by construction.
    """
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.dump(
            {
                "database_path": str(tmp_path / "test.db"),
                "discord": {"bot_token": "t", "guild_id": "1"},
                "logging": {"level": "DEBUG"},
                "memory": {"embedding_api_key": "k"},
            }
        )
    )
    config = load_config(str(path))
    defaults = AppConfig()

    assert config.logging.level == "DEBUG"
    assert config.memory.embedding_api_key == "k"
    for section in ("logging", "memory"):
        supplied = {"logging": "level", "memory": "embedding_api_key"}[section]
        loaded, default = getattr(config, section), getattr(defaults, section)
        for f in dataclasses.fields(loaded):
            if f.name != supplied:
                assert getattr(loaded, f.name) == getattr(default, f.name), f.name


def test_tuple_fields_load_from_a_yaml_list(tmp_path):
    """``tuple[str, ...]`` fields keep their declared type, not YAML's list."""
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.dump(
            {
                "database_path": str(tmp_path / "test.db"),
                "discord": {"bot_token": "t", "guild_id": "1"},
                "memory": {"knowledge_topics": ["architecture", "gotchas"]},
            }
        )
    )
    assert load_config(str(path)).memory.knowledge_topics == ("architecture", "gotchas")


def test_a_key_written_with_no_value_leaves_the_default(tmp_path):
    """``level:`` alone on its line asserts nothing, so it must not win."""
    path = tmp_path / "config.yaml"
    path.write_text(
        f"database_path: {tmp_path / 'test.db'}\n"
        "discord:\n  bot_token: t\n  guild_id: '1'\n"
        "logging:\n  level:\n  include_source: true\n"
    )
    config = load_config(str(path))
    assert config.logging.level == AppConfig().logging.level
    assert config.logging.include_source is True
