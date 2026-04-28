"""Tests for the read-only config editor module."""

from __future__ import annotations


import pytest
import yaml

from src.config_editor import (
    build_config_schema,
    classify_sections,
    find_env_var_refs,
    read_raw_config,
)


class TestReadRawConfig:
    def test_preserves_env_var_placeholders(self, tmp_path, monkeypatch):
        """Raw read must NOT resolve ${ENV_VAR} — that's the whole point."""
        monkeypatch.setenv("MY_TOKEN", "should-not-appear")
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({"discord": {"bot_token": "${MY_TOKEN}"}}))
        raw = read_raw_config(str(cfg))
        assert raw["discord"]["bot_token"] == "${MY_TOKEN}"

    def test_empty_file(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("")
        assert read_raw_config(str(cfg)) == {}


class TestFindEnvVarRefs:
    def test_finds_top_level_and_nested(self, monkeypatch):
        monkeypatch.setenv("RESOLVED_VAR", "x")
        monkeypatch.delenv("MISSING_VAR", raising=False)
        raw = {
            "a": "${RESOLVED_VAR}",
            "nested": {"b": "${MISSING_VAR}"},
            "list": [{"c": "${RESOLVED_VAR}"}],
            "plain": "no-vars-here",
        }
        refs = find_env_var_refs(raw)
        paths = {r["path"]: r for r in refs}
        assert paths["a"]["var"] == "RESOLVED_VAR"
        assert paths["a"]["resolved"] is True
        assert paths["nested.b"]["resolved"] is False
        assert paths["list[0].c"]["resolved"] is True

    def test_no_refs(self):
        assert find_env_var_refs({"a": 1, "b": "literal"}) == []


class TestClassifySections:
    def test_known_sections_split(self):
        c = classify_sections()
        # Sanity — these are explicitly classified in src/config.py.
        assert "scheduling" in c["hot_reloadable"]
        assert "discord" in c["restart_required"]
        # No section appears in two buckets.
        all_sections = set(c["hot_reloadable"]) | set(c["restart_required"]) | set(c["other"])
        assert sum(len(v) for v in c.values()) == len(all_sections)


class TestBuildConfigSchema:
    def test_top_level_shape(self):
        schema = build_config_schema()
        assert schema["type"] == "object"
        # Spot-check a few fields exist with the right type.
        props = schema["properties"]
        assert props["scheduling"]["type"] == "object"
        assert props["validate_events"]["type"] == "boolean"
        assert props["env"]["type"] == "string"

    def test_nested_dataclass_resolved(self):
        schema = build_config_schema()
        discord = schema["properties"]["discord"]
        assert discord["type"] == "object"
        assert "bot_token" in discord["properties"]
        assert discord["properties"]["bot_token"]["type"] == "string"

    def test_list_field_typed(self):
        schema = build_config_schema()
        agent_profiles = schema["properties"]["agent_profiles"]
        assert agent_profiles["type"] == "array"
        # items should be a nested AgentProfileConfig schema (object).
        assert agent_profiles["items"]["type"] == "object"

    def test_reload_classification_annotated(self):
        schema = build_config_schema()
        props = schema["properties"]
        assert props["scheduling"]["x-reload"] == "hot"
        assert props["discord"]["x-reload"] == "restart"

    def test_private_fields_skipped(self):
        schema = build_config_schema()
        # AppConfig has a private _config_path that must not leak into the schema.
        assert "_config_path" not in schema["properties"]


class TestWriteSection:
    def test_round_trip_preserves_comments(self, tmp_path):
        """Comments outside the replaced section must survive."""
        from src.config_editor import write_section

        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "# top comment\n"
            "discord:\n"
            "  bot_token: original\n"
            "scheduling:\n"
            "  # this comment must survive\n"
            "  rolling_window_hours: 12\n"
        )
        write_section(str(cfg), "discord", {"bot_token": "new-value"})
        text = cfg.read_text()
        assert "# top comment" in text
        assert "# this comment must survive" in text
        assert "new-value" in text

    def test_env_var_placeholder_round_trip(self, tmp_path, monkeypatch):
        """If caller round-trips a ${VAR}, it must be written verbatim."""
        from src.config_editor import read_raw_config, write_section

        monkeypatch.setenv("MY_TOKEN", "real-secret")
        cfg = tmp_path / "config.yaml"
        cfg.write_text("discord:\n  bot_token: ${MY_TOKEN}\n  guild_id: '1'\n")

        raw = read_raw_config(str(cfg))
        # Simulate the dashboard sending back the section unchanged.
        write_section(str(cfg), "discord", raw["discord"])

        # File still references the env var literally — secret never leaked.
        assert "${MY_TOKEN}" in cfg.read_text()
        assert "real-secret" not in cfg.read_text()

    def test_section_delete(self, tmp_path):
        from src.config_editor import write_section

        cfg = tmp_path / "config.yaml"
        cfg.write_text("a: 1\nb: 2\n")
        write_section(str(cfg), "a", None)
        text = cfg.read_text()
        assert "a:" not in text
        assert "b: 2" in text

    def test_section_added(self, tmp_path):
        from src.config_editor import write_section

        cfg = tmp_path / "config.yaml"
        cfg.write_text("existing: keep\n")
        write_section(str(cfg), "scheduling", {"rolling_window_hours": 24})
        text = cfg.read_text()
        assert "existing: keep" in text
        assert "rolling_window_hours: 24" in text

    def test_other_sections_byte_identical(self, tmp_path):
        """Sections we don't touch should not be reformatted."""
        from src.config_editor import write_section

        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "discord:\n  bot_token: \"quoted-token\"\n  guild_id: '1'\n"
            "scheduling:\n  rolling_window_hours: 12\n"
        )
        before = cfg.read_text()
        write_section(str(cfg), "scheduling", {"rolling_window_hours": 99})
        after = cfg.read_text()
        # discord section preserves its original quoting style.
        assert '"quoted-token"' in after
        assert "'1'" in after
        # scheduling was actually changed.
        assert "rolling_window_hours: 99" in after
        assert before != after


class TestGetConfigCommand:
    """End-to-end tests for the _cmd_get_config handler via CommandHandler."""

    @pytest.fixture
    def handler_with_config(self, tmp_path):
        from unittest.mock import MagicMock

        from src.commands.handler import CommandHandler
        from src.config import AppConfig

        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            yaml.dump(
                {
                    "discord": {"bot_token": "${MY_TOKEN}", "guild_id": "1"},
                    "scheduling": {"rolling_window_hours": 12},
                }
            )
        )
        config = AppConfig(data_dir=str(tmp_path / "data"))
        config._config_path = str(cfg_path)
        orch = MagicMock()
        orch.config = config
        return CommandHandler(orch, config), cfg_path

    @pytest.mark.asyncio
    async def test_returns_full_raw_config(self, handler_with_config):
        handler, cfg_path = handler_with_config
        result = await handler.execute("get_config", {})
        assert result["path"] == str(cfg_path)
        # Env-var placeholder must be preserved in the response.
        assert result["config"]["discord"]["bot_token"] == "${MY_TOKEN}"
        assert "scheduling" in result["hot_reloadable"]
        assert "discord" in result["restart_required"]

    @pytest.mark.asyncio
    async def test_section_filter(self, handler_with_config):
        handler, _ = handler_with_config
        result = await handler.execute("get_config", {"section": "scheduling"})
        assert result["section"] == "scheduling"
        assert set(result["config"].keys()) == {"scheduling"}

    @pytest.mark.asyncio
    async def test_env_var_references_listed(self, handler_with_config, monkeypatch):
        handler, _ = handler_with_config
        monkeypatch.delenv("MY_TOKEN", raising=False)
        result = await handler.execute("get_config", {})
        refs = result["env_var_references"]
        assert any(
            r["path"] == "discord.bot_token" and r["var"] == "MY_TOKEN" and r["resolved"] is False
            for r in refs
        )

    @pytest.mark.asyncio
    async def test_no_path_returns_error(self, tmp_path):
        from unittest.mock import MagicMock

        from src.commands.handler import CommandHandler
        from src.config import AppConfig

        config = AppConfig(data_dir=str(tmp_path / "data"))  # _config_path defaults to ""
        orch = MagicMock()
        orch.config = config
        handler = CommandHandler(orch, config)
        result = await handler.execute("get_config", {})
        assert "error" in result


class TestUpdateConfigCommand:
    @pytest.fixture
    def handler_with_config(self, tmp_path):
        from unittest.mock import AsyncMock, MagicMock

        from src.commands.handler import CommandHandler
        from src.config import AppConfig

        db_path = tmp_path / "test.db"
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            "discord:\n"
            "  bot_token: tok\n"
            "  guild_id: '1'\n"
            "scheduling:\n"
            "  rolling_window_hours: 12\n"
            f"database_path: {db_path}\n"
        )
        config = AppConfig(data_dir=str(tmp_path / "data"))
        config._config_path = str(cfg_path)
        watcher = MagicMock()
        watcher.reload = AsyncMock(return_value={"applied": ["scheduling"]})
        orch = MagicMock()
        orch.config = config
        orch._config_watcher = watcher
        return CommandHandler(orch, config), cfg_path, watcher

    @pytest.mark.asyncio
    async def test_hot_section_writes_and_reloads(self, handler_with_config):
        handler, cfg_path, watcher = handler_with_config
        result = await handler.execute(
            "update_config",
            {"section": "scheduling", "data": {"rolling_window_hours": 99}},
        )
        assert result["changed"] is True
        assert result["requires_restart"] is False
        assert result["applied"] is True
        assert "rolling_window_hours: 99" in cfg_path.read_text()
        watcher.reload.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_restart_section_writes_but_does_not_reload(self, handler_with_config):
        handler, cfg_path, watcher = handler_with_config
        result = await handler.execute(
            "update_config",
            {"section": "discord", "data": {"bot_token": "new-tok", "guild_id": "1"}},
        )
        assert result["changed"] is True
        assert result["requires_restart"] is True
        assert result["applied"] is False
        assert "new-tok" in cfg_path.read_text()
        watcher.reload.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_data_does_not_write(self, handler_with_config, monkeypatch):
        handler, cfg_path, watcher = handler_with_config
        monkeypatch.delenv("DEFINITELY_NOT_SET_VAR", raising=False)
        before = cfg_path.read_text()
        # ${VAR} pointing at an unset env var fails load_config validation.
        result = await handler.execute(
            "update_config",
            {
                "section": "discord",
                "data": {"bot_token": "${DEFINITELY_NOT_SET_VAR}", "guild_id": "1"},
            },
        )
        assert result["changed"] is False
        assert result["validation_errors"]
        assert cfg_path.read_text() == before
        watcher.reload.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_does_not_write(self, handler_with_config):
        handler, cfg_path, watcher = handler_with_config
        before = cfg_path.read_text()
        result = await handler.execute(
            "update_config",
            {
                "section": "scheduling",
                "data": {"rolling_window_hours": 42},
                "dry_run": True,
            },
        )
        assert result["dry_run"] is True
        assert result["changed"] is True  # would change
        assert cfg_path.read_text() == before
        watcher.reload.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_args_error(self, handler_with_config):
        handler, _, _ = handler_with_config
        result = await handler.execute("update_config", {"data": {}})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_delete_section(self, handler_with_config):
        handler, cfg_path, _ = handler_with_config
        result = await handler.execute(
            "update_config",
            {"section": "scheduling", "data": None},
        )
        assert result["changed"] is True
        assert "scheduling" not in cfg_path.read_text()


class TestCliDottedSet:
    def test_creates_intermediate_dicts(self):
        from src.cli.system_config import _set_dotted

        doc: dict = {}
        _set_dotted(doc, "a.b.c", 7)
        assert doc == {"a": {"b": {"c": 7}}}

    def test_overrides_existing(self):
        from src.cli.system_config import _set_dotted

        doc = {"a": {"b": 1}}
        _set_dotted(doc, "a.b", 2)
        assert doc == {"a": {"b": 2}}

    def test_replaces_non_dict_intermediate(self):
        from src.cli.system_config import _set_dotted

        doc = {"a": "scalar"}
        _set_dotted(doc, "a.b", 3)
        assert doc == {"a": {"b": 3}}

    def test_yaml_scalar_parsing(self):
        from src.cli.system_config import _parse_yaml_scalar

        assert _parse_yaml_scalar("true") is True
        assert _parse_yaml_scalar("42") == 42
        assert _parse_yaml_scalar("3.14") == 3.14
        assert _parse_yaml_scalar("[a, b]") == ["a", "b"]
        assert _parse_yaml_scalar("hello") == "hello"


class TestGetConfigSchemaCommand:
    @pytest.mark.asyncio
    async def test_returns_schema(self, tmp_path):
        from unittest.mock import MagicMock

        from src.commands.handler import CommandHandler
        from src.config import AppConfig

        config = AppConfig(data_dir=str(tmp_path / "data"))
        orch = MagicMock()
        orch.config = config
        handler = CommandHandler(orch, config)

        result = await handler.execute("get_config_schema", {})
        schema = result["schema"]
        assert schema["type"] == "object"
        assert "scheduling" in schema["properties"]
        assert schema["properties"]["scheduling"]["x-reload"] == "hot"
