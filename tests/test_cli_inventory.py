"""Maintained inventory and plugin-extension startup guards for ``aq``."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from src.cli.inventory import build_cli_inventory, inventory_counts

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
    assert "counts" not in committed
    assert inventory_counts(committed)["leaf_commands"] == len(committed["commands"])


def test_start_terminal_has_the_documented_project_option():
    from src.cli.app import cli

    result = CliRunner().invoke(cli, ["agent", "start-terminal", "--help"])

    assert result.exit_code == 0
    assert "--project, --project-id TEXT" in result.output


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

    # The audited statuses are pinned: nothing may become "working" without the
    # evidence that earns it, and no command may regress into broken/unsupported.
    # "untested" is deliberately derived rather than pinned so that a CLI command
    # added after the audit lands there and does not turn this gate red on its own.
    totals = inventory_counts(inventory)
    counts = totals["acceptance_status"]
    assert {k: counts[k] for k in ("working", "broken", "obsolete", "unsupported")} == {
        # +4: `aq dashboard start|stop|restart|status`, earned by
        # tests/test_cli_dashboard_server.py against a real server process.
        "working": 59,
        "broken": 0,
        "obsolete": 1,
        "unsupported": 0,
    }
    assert counts["untested"] >= 261
    assert sum(counts.values()) == totals["leaf_commands"]
    historical = {row["path"]: row for row in inventory["historical_commands"]}
    assert historical["aq task ask-human"]["acceptance_status"] == "unsupported"
    assert historical["aq task tree"]["acceptance_status"] == "obsolete"


def test_deprecated_plugin_logs_command_explains_the_supported_replacement():
    from src.cli.app import cli

    result = CliRunner().invoke(cli, ["plugin", "logs", "example"])
    assert result.exit_code == 1
    assert "has been removed" in result.output
    assert "aq playbook list-runs" in result.output
    assert "aq playbook inspect-run --run-id <run-id>" in result.output


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
    def __init__(self, name: str, plugin_type: type, dist: str | None = None) -> None:
        self.name = name
        self._plugin_type = plugin_type
        # importlib.metadata.EntryPoint.dist: the installing Distribution, or None.
        self.dist = SimpleNamespace(name=dist) if dist is not None else None

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


class _ForbiddenImportEntryPoint(_FakeEntryPoint):
    def load(self):
        raise AssertionError(f"entry point {self.name!r} must not be imported")


def _root_with_core_memory() -> click.Group:
    root = click.Group("aq")

    @click.command("memory")
    def core_memory():
        click.echo("core memory")

    root.add_command(core_memory)
    return root


@pytest.mark.parametrize("dist", ["aq-memory", "aq_memory", "AQ.Memory"])
def test_installed_legacy_memory_plugin_is_core_surfaced_not_a_collision(caplog, dist):
    """aq-memory's ``memory`` entry point names the core-owned ``aq memory`` group.

    Core generates that group from the memory_* schemas the plugin implements
    at runtime, so the installed legacy plugin mounts nothing, is never
    imported by the CLI, and is not reported as a conflict on every command.
    """
    from src.cli.app import _load_plugin_cli_groups

    root = _root_with_core_memory()
    with caplog.at_level(logging.DEBUG, logger="src.cli.app"):
        mounted = _load_plugin_cli_groups(
            root,
            entry_point_provider=lambda **_kwargs: [
                _ForbiddenImportEntryPoint("memory", _ExamplePlugin, dist=dist)
            ],
            config_loader=lambda _name: None,
        )

    assert mounted == []
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
    result = CliRunner().invoke(root, ["memory"])
    assert result.exit_code == 0
    assert result.output == "core memory\n"


@pytest.mark.parametrize(
    ("name", "dist", "shown"),
    [
        ("memory", "someone-elses-memory", "someone-elses-memory"),
        ("memory", None, "unknown distribution"),
        ("task", "aq-memory", "aq-memory"),
    ],
    ids=["other-dist-memory", "no-dist-metadata", "aq-memory-non-memory-name"],
)
def test_unsupported_third_party_collision_is_still_reported(caplog, name, dist, shown):
    from src.cli.app import _load_plugin_cli_groups

    root = _root_with_core_memory()

    @click.command("task")
    def core_task():
        click.echo("core task")

    root.add_command(core_task)
    with caplog.at_level(logging.WARNING, logger="src.cli.app"):
        mounted = _load_plugin_cli_groups(
            root,
            entry_point_provider=lambda **_kwargs: [
                _ForbiddenImportEntryPoint(name, _ExamplePlugin, dist=dist)
            ],
            config_loader=lambda _name: None,
        )

    assert mounted == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert f"Plugin CLI entry point '{name}'" in warnings[0]
    assert shown in warnings[0]
    assert "conflicts with an existing command; skipped" in warnings[0]
    result = CliRunner().invoke(root, [name])
    assert result.exit_code == 0
    assert result.output == f"core {name}\n"
