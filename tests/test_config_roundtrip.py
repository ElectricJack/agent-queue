"""Config edit-transaction round-trip tests (platform plan 13-14).

The editor writes raw YAML (placeholders preserved) and the runtime loads
resolved, overlay-merged, validated config.  These tests drive the full
seam — ruamel writer → on-disk bytes → ``load_config`` — instead of
asserting text fragments, plus the ``update_config`` command's atomicity
when validation rejects a candidate document.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import yaml

from src.config import AppConfig, load_config
from src.config_editor import _round_trip_yaml, read_raw_config, write_full_config

_TOKEN_VAR = "AQ_TEST_ROUNDTRIP_TOKEN"


def test_editor_write_full_config_round_trips_through_load_config_with_env_overlay(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(_TOKEN_VAR, raising=False)
    monkeypatch.delenv("AGENT_QUEUE_ENV", raising=False)
    monkeypatch.delenv("AGENT_QUEUE_PROFILE", raising=False)

    for sub in ("workspaces", "edited-ws", "data"):
        (tmp_path / sub).mkdir()

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "# deployment config — do not resolve secrets here\n"
        "env: dev\n"
        f"workspace_dir: {tmp_path / 'workspaces'}\n"
        f"database_path: {tmp_path / 'aq.db'}\n"
        f"data_dir: {tmp_path / 'data'}\n"
        "discord:\n"
        f'  bot_token: "${{{_TOKEN_VAR}}}"\n'
        '  guild_id: "42"\n',
        encoding="utf-8",
    )
    (tmp_path / "config.dev.yaml").write_text(
        "global_token_budget_daily: 12345\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text(f"{_TOKEN_VAR}=resolved-secret-token\n", encoding="utf-8")

    # Edit through the round-trip writer, as `aq system config edit` does.
    yaml_rt = _round_trip_yaml()
    with open(config_path, encoding="utf-8") as f:
        doc = yaml_rt.load(f)
    doc["workspace_dir"] = str(tmp_path / "edited-ws")
    write_full_config(str(config_path), doc)

    # The written document keeps the comment and the *quoted* placeholder.
    written = config_path.read_text(encoding="utf-8")
    assert "# deployment config — do not resolve secrets here" in written
    assert f'"${{{_TOKEN_VAR}}}"' in written

    # The editor read layer still sees the unresolved reference.
    raw = read_raw_config(str(config_path))
    assert raw["discord"]["bot_token"] == f"${{{_TOKEN_VAR}}}"
    assert raw["workspace_dir"] == str(tmp_path / "edited-ws")

    # The runtime loader resolves the placeholder (via the sibling .env),
    # applies the dev overlay, and keeps the edited value.
    try:
        config = load_config(str(config_path))
    finally:
        os.environ.pop(_TOKEN_VAR, None)  # _load_env_file exports it

    assert config.env == "dev"
    assert config.discord.bot_token == "resolved-secret-token"
    assert config.global_token_budget_daily == 12345
    assert config.workspace_dir == str(tmp_path / "edited-ws")


async def test_update_config_validation_failure_is_atomic_for_full_document(tmp_path):
    from src.commands.handler import CommandHandler

    (tmp_path / "workspaces").mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.dump(
            {
                "workspace_dir": str(tmp_path / "workspaces"),
                "database_path": str(tmp_path / "test.db"),
                "discord": {"bot_token": "test-token", "guild_id": "123"},
                "scheduling": {"rolling_window_hours": 24},
            }
        ),
        encoding="utf-8",
    )
    original_bytes = config_path.read_bytes()
    original_listing = sorted(p.name for p in tmp_path.iterdir())

    config = AppConfig(data_dir=str(tmp_path / "data"))
    config._config_path = str(config_path)
    orch = MagicMock()
    orch.config = config
    handler = CommandHandler(orch, config)

    result = await handler.execute(
        "update_config",
        {
            "section": "database",
            "data": {"url": "postgresql+asyncpg://u:p@localhost:5432/aq", "pool_min_size": 0},
        },
    )

    assert result["applied"] is False
    assert result["changed"] is False
    assert result["validation_errors"]
    assert "pool_min_size" in result["validation_errors"][0]

    # The rejected update left the document byte-identical and cleaned up
    # its candidate temp file — nothing new in the directory.
    assert config_path.read_bytes() == original_bytes
    assert sorted(p.name for p in tmp_path.iterdir()) == original_listing
