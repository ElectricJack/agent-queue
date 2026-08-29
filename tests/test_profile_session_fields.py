"""Named-session profile fields — supervisor-agent §3.2 / §7.

Parser validation, the ``parsed_profile_to_agent_profile`` mapping, the sync
path onto ``agent_profiles``, and the harness-existence warning.
"""

from __future__ import annotations

import pytest

from src.database import Database
from src.models import AgentProfile
from src.profiles.parser import (
    CONFIG_KNOWN_KEYS,
    VALID_LIFECYCLES,
    VALID_MODES,
    VALID_WAKE_MODES,
    parse_profile,
    parsed_profile_to_agent_profile,
)
from src.profiles.sync import sync_profile_to_db

SUPERVISOR_PROFILE = """---
id: supervisor
name: Supervisor
tags: [profile, agent-type, shipped]
---

# Supervisor

## Role
You are the supervisor for one project in Agent Queue.

## Config
```json
{
  "harness": "claude",
  "lifecycle": "named",
  "mode": "on_demand",
  "wake_mode": "resume",
  "idle_timeout": 900,
  "workspaces": ["vault", "readonly-dir"]
}
```
"""


def _profile(config_json: str, *, profile_id: str = "p") -> str:
    return f"""---
id: {profile_id}
name: {profile_id}
---

# {profile_id}

## Config
```json
{config_json}
```
"""


class TestKnownKeys:
    def test_new_keys_are_declared(self):
        assert {
            "harness",
            "lifecycle",
            "mode",
            "wake_mode",
            "idle_timeout",
            "max_session_age",
            "workspaces",
        } <= CONFIG_KNOWN_KEYS

    def test_enum_sets_match_the_spec(self):
        assert VALID_LIFECYCLES == {"task", "named", "pool"}
        assert VALID_MODES == {"always", "on_demand"}
        assert VALID_WAKE_MODES == {"resume", "fresh"}


class TestParsing:
    def test_shipped_supervisor_config_parses(self):
        parsed = parse_profile(SUPERVISOR_PROFILE)
        assert parsed.is_valid, parsed.errors
        assert parsed.config["harness"] == "claude"
        assert parsed.config["lifecycle"] == "named"

    def test_fields_map_onto_the_agent_profile_dict(self):
        parsed = parse_profile(SUPERVISOR_PROFILE)
        mapped = parsed_profile_to_agent_profile(parsed)
        assert mapped["harness"] == "claude"
        assert mapped["lifecycle"] == "named"
        assert mapped["mode"] == "on_demand"
        assert mapped["wake_mode"] == "resume"
        assert mapped["idle_timeout"] == 900
        assert mapped["workspaces"] == ["vault", "readonly-dir"]

    def test_lifecycle_defaults_to_task(self):
        parsed = parse_profile(_profile('{"model": "x"}'))
        assert parsed.is_valid
        assert "lifecycle" not in parsed_profile_to_agent_profile(parsed)
        assert AgentProfile(id="p", name="p").lifecycle == "task"

    @pytest.mark.parametrize(
        "config,fragment",
        [
            ('{"lifecycle": "forever"}', "'lifecycle' must be one of"),
            ('{"lifecycle": 3}', "'lifecycle' must be a string"),
            ('{"lifecycle": "named", "mode": "sometimes"}', "'mode' must be one of"),
            ('{"lifecycle": "named", "wake_mode": "reboot"}', "'wake_mode' must be one of"),
            ('{"harness": ""}', "'harness' must not be empty"),
            ('{"harness": 5}', "'harness' must be a string"),
            ('{"lifecycle": "named", "idle_timeout": 0}', "'idle_timeout' must be positive"),
            (
                '{"lifecycle": "named", "idle_timeout": "long"}',
                "'idle_timeout' must be a positive integer",
            ),
            ('{"workspaces": "vault"}', "'workspaces' must be a list"),
            ('{"workspaces": [3]}', "'workspaces' entries must be non-empty strings"),
        ],
    )
    def test_invalid_values_are_parse_errors(self, config, fragment):
        parsed = parse_profile(_profile(config))
        assert not parsed.is_valid
        assert any(fragment in e for e in parsed.errors), parsed.errors

    @pytest.mark.parametrize("key", ["mode", "wake_mode", "idle_timeout", "max_session_age"])
    def test_named_only_keys_rejected_on_task_lifecycle(self, key):
        """Spec §7: these are only valid with lifecycle: named."""
        value = {"mode": '"always"', "wake_mode": '"resume"'}.get(key, "600")
        parsed = parse_profile(_profile(f'{{"{key}": {value}}}'))
        assert not parsed.is_valid
        assert any("only valid with lifecycle 'named'" in e for e in parsed.errors), parsed.errors

    def test_named_only_keys_accepted_on_named_lifecycle(self):
        parsed = parse_profile(
            _profile(
                '{"lifecycle": "named", "mode": "always", "wake_mode": "fresh", '
                '"idle_timeout": 600, "max_session_age": 86400}'
            )
        )
        assert parsed.is_valid, parsed.errors


class TestSync:
    @pytest.fixture
    async def db(self, tmp_path):
        database = Database(str(tmp_path / "profiles.db"))
        await database.initialize()
        yield database
        await database.close()

    async def test_round_trips_through_agent_profiles(self, db):
        parsed = parse_profile(SUPERVISOR_PROFILE)
        result = await sync_profile_to_db(parsed, db)
        assert result.success, result.errors

        stored = await db.get_profile("supervisor")
        assert stored.harness == "claude"
        assert stored.lifecycle == "named"
        assert stored.mode == "on_demand"
        assert stored.wake_mode == "resume"
        assert stored.idle_timeout == 900
        assert stored.max_session_age is None

    async def test_update_path_carries_the_fields(self, db):
        await sync_profile_to_db(parse_profile(SUPERVISOR_PROFILE), db)
        updated = SUPERVISOR_PROFILE.replace('"idle_timeout": 900', '"idle_timeout": 60')
        result = await sync_profile_to_db(parse_profile(updated), db)
        assert result.action == "updated"
        assert (await db.get_profile("supervisor")).idle_timeout == 60

    async def test_task_lifecycle_profiles_default_cleanly(self, db):
        parsed = parse_profile(_profile('{"model": "claude-sonnet"}', profile_id="coding"))
        await sync_profile_to_db(parsed, db)
        stored = await db.get_profile("coding")
        assert stored.lifecycle == "task"
        assert stored.harness is None
        assert stored.mode is None


class TestHarnessWarning:
    def test_silent_when_no_harness_registry_exists(self, tmp_path):
        from src.profiles.sync import _check_harness_exists

        source = tmp_path / "vault" / "agent-types" / "supervisor" / "profile.md"
        source.parent.mkdir(parents=True)
        source.write_text("x", encoding="utf-8")
        assert _check_harness_exists("claude", str(source)) is None

    def test_warns_when_the_registry_exists_without_the_harness(self, tmp_path):
        from src.profiles.sync import _check_harness_exists

        vault = tmp_path / "vault"
        (vault / "harnesses").mkdir(parents=True)
        source = vault / "agent-types" / "supervisor" / "profile.md"
        source.parent.mkdir(parents=True)
        source.write_text("x", encoding="utf-8")
        warning = _check_harness_exists("claude", str(source))
        assert warning is not None
        assert "claude" in warning

    def test_silent_when_the_harness_is_defined(self, tmp_path):
        from src.profiles.sync import _check_harness_exists

        vault = tmp_path / "vault"
        (vault / "harnesses").mkdir(parents=True)
        (vault / "harnesses" / "claude.md").write_text("x", encoding="utf-8")
        source = vault / "agent-types" / "supervisor" / "profile.md"
        source.parent.mkdir(parents=True)
        source.write_text("x", encoding="utf-8")
        assert _check_harness_exists("claude", str(source)) is None

    def test_no_source_path_means_no_check(self):
        from src.profiles.sync import _check_harness_exists

        assert _check_harness_exists("claude", "") is None
        assert _check_harness_exists(None, "/some/vault/x.md") is None
