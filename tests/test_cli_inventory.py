"""Maintained inventory and plugin-extension startup guards for ``aq``."""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from src.cli.inventory import build_cli_inventory

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = ROOT / "docs" / "reference" / "cli-command-inventory.json"


@pytest.fixture(autouse=True)
def _pg_backend():
    """Pure CLI introspection tests never allocate a database."""


def _by_path(inventory: dict) -> dict[str, dict]:
    return {row["path"]: row for row in inventory["commands"]}


def test_committed_inventory_is_generated_from_the_live_tree():
    """No historical count is pinned; the complete artifact is regenerated."""
    from src.cli.app import cli

    expected = build_cli_inventory(cli)
    committed = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert committed == expected, (
        "CLI inventory drifted; run `python scripts/generate-cli-command-inventory.py`"
    )
    assert committed["counts"]["leaf_commands"] == len(committed["commands"])


def test_inventory_labels_registration_ownership_aliases_and_deprecations():
    from src.cli.app import cli

    rows = _by_path(build_cli_inventory(cli))
    assert rows["aq task list"]["registration"] == "handwritten"
    assert rows["aq task get"]["registration"] == "generated"

    assert rows["aq file read"]["owner_kind"] == "internal-plugin"
    assert rows["aq file read"]["owner"] == "aq-files"
    assert rows["aq memory search"]["owner_kind"] == "external-plugin"
    assert rows["aq memory search"]["owner"] == "aq-memory"
    assert "core-handler" not in rows["aq memory search"]["evidence"]

    assert rows["aq task details"]["alias_for"] == "aq task show"
    assert rows["aq reply"]["alias_for"] == "aq message reply"
    assert rows["aq plugin logs"]["support"] == "deprecated"
    assert rows["aq plugin logs"]["deprecation"]


def test_acceptance_statuses_are_conservative_and_preserve_removed_surfaces():
    from src.cli.app import cli

    inventory = build_cli_inventory(cli)
    rows = _by_path(inventory)

    assert rows["aq task create"]["acceptance_status"] == "working"
    assert rows["aq task create"]["acceptance_evidence"] == [
        "focused-behavioral-tests",
        "disposable-daemon:S3/S9",
    ]
    assert rows["aq task archive"]["acceptance_status"] == "untested"
    assert rows["aq task archive"]["acceptance_evidence"] == []
    assert rows["aq plugin logs"]["acceptance_status"] == "obsolete"

    assert inventory["counts"]["acceptance_status"] == {
        "working": 55,
        "broken": 0,
        "obsolete": 1,
        "unsupported": 0,
        "untested": 261,
    }
    historical = {row["path"]: row for row in inventory["historical_commands"]}
    assert historical["aq task ask-human"]["acceptance_status"] == "unsupported"
    assert historical["aq task tree"]["acceptance_status"] == "obsolete"


def test_deprecated_plugin_logs_command_explains_the_supported_replacement():
    from src.cli.app import cli

    result = CliRunner().invoke(cli, ["plugin", "logs", "example"])
    assert result.exit_code == 0
    assert "no longer available" in result.output
    assert "aq playbook list" in result.output


def test_unimplemented_operations_must_be_classified_and_removed_commands_stay_absent():
    from rich.console import Console

    from src.cli.auto_commands import _make_auto_command

    root = click.Group("aq")
    missing = _make_auto_command(
        "brand_new_missing_handler",
        "missing",
        {
            "name": "brand_new_missing_handler",
            "description": "An accidentally advertised operation.",
            "input_schema": {"type": "object", "properties": {}},
        },
        Console(),
    )
    root.add_command(missing)
    with pytest.raises(ValueError, match="no handler or plugin provider"):
        build_cli_inventory(root, validate_surface_ledgers=False)

    from src.cli.app import cli

    assert "aq task ask-human" not in _by_path(build_cli_inventory(cli))


class _FakeEntryPoint:
    def __init__(self, name: str, plugin_type: type) -> None:
        self.name = name
        self._plugin_type = plugin_type

    def load(self):
        return self._plugin_type


class _ExamplePlugin:
    def __init__(self) -> None:
        self.config = {"from_class": True}

    def cli_group(self):
        @click.group()
        def group():
            """Example plugin."""

        @group.command()
        def ping():
            click.echo("pong")

        return group


def test_plugin_present_and_absent_startup_and_command_behavior():
    from src.cli.app import _load_plugin_cli_groups

    root = click.Group("aq")
    absent = _load_plugin_cli_groups(
        root,
        entry_point_provider=lambda **_kwargs: [],
        config_loader=lambda _name: None,
    )
    assert absent == []
    assert root.commands == {}

    present = _load_plugin_cli_groups(
        root,
        entry_point_provider=lambda **_kwargs: [_FakeEntryPoint("example", _ExamplePlugin)],
        config_loader=lambda _name: {"saved": True},
    )
    assert present == ["example"]
    result = CliRunner().invoke(root, ["example", "ping"])
    assert result.exit_code == 0
    assert result.output == "pong\n"

    # Environment-specific extensions are visible on request but excluded
    # from the reproducible core artifact.
    assert build_cli_inventory(root, validate_surface_ledgers=False)["commands"] == []
    row = build_cli_inventory(
        root,
        include_external_extensions=True,
        validate_surface_ledgers=False,
    )["commands"][0]
    assert row["path"] == "aq example ping"
    assert row["registration"] == "plugin-extension"
    assert row["owner"] == "example"


def test_plugin_cannot_shadow_a_core_top_level_command():
    from src.cli.app import _load_plugin_cli_groups

    root = click.Group("aq")

    @click.command("task")
    def core_task():
        click.echo("core")

    root.add_command(core_task)
    mounted = _load_plugin_cli_groups(
        root,
        entry_point_provider=lambda **_kwargs: [_FakeEntryPoint("task", _ExamplePlugin)],
        config_loader=lambda _name: None,
    )
    assert mounted == []
    result = CliRunner().invoke(root, ["task"])
    assert result.exit_code == 0
    assert result.output == "core\n"
