"""Command-surface invariant — every ``_cmd_*`` is placed deliberately.

Design ``trust-and-ops`` §6: a new command must be one of three things, on
purpose — explicitly typed in ``_ALL_TOOL_DEFINITIONS``, explicitly excluded
from MCP, or knowingly left to the auto-discovery safety net.  The third
bucket is a checked-in list that must **shrink, never silently grow**: adding a
name to :data:`KNOWN_AUTO_REGISTERED` is a visible act in review.
"""

from __future__ import annotations

import inspect

import pytest

from src.commands.handler import CommandHandler
from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS, get_effective_exclusions
from src.tools.definitions import _ALL_TOOL_DEFINITIONS

#: Commands intentionally left to ``register_command_tools``'s docstring-derived
#: auto-registration rather than being given a typed schema.  This list is a
#: debt ledger: entries may be removed (by writing a real definition) but new
#: entries require a deliberate edit here.
KNOWN_AUTO_REGISTERED: frozenset[str] = frozenset(
    {
        # Workflow coordination — schemas land with the workflow workstream.
        "advance_workflow_stage",
        "create_workflow",
        "get_workflow",
        "list_workflows",
        "workflow_pipeline_view",
        # Plugin lifecycle — driven from the CLI/dashboard, not the LLM.
        "plugin_config",
        "plugin_disable",
        "plugin_enable",
        "plugin_info",
        "plugin_install",
        "plugin_list",
        "plugin_prompts",
        "plugin_reload",
        "plugin_remove",
        "plugin_reset_prompts",
        "plugin_update",
        # One-shot maintenance commands.
        "migrate_profiles",
        "vault_rebuild_index",
    }
)

#: Typed definitions written here for commands whose ``_cmd_*`` method lands
#: on ``main`` in a *different* in-flight lane (Wave 1 lane 1D, aq-surface).
#: The definitions must exist in this lane or
#: ``test_every_command_is_placed_deliberately`` fails the moment the branches
#: meet; until then the method genuinely is not on ``CommandHandler``, so the
#: orphan check would fire on this branch alone.  **Delete this set once both
#: lanes are on ``main``** — with the methods present the orphan check covers
#: these names on its own, so removing the set is a no-op that costs nothing
#: and keeps the ledger honest.
PENDING_ON_ANOTHER_LANE: frozenset[str] = frozenset(
    {"get_schema", "task_show", "task_set"}
)


def _command_names() -> set[str]:
    return {
        name[len("_cmd_") :]
        for name, _ in inspect.getmembers(CommandHandler, predicate=inspect.isfunction)
        if name.startswith("_cmd_")
    }


def _defined_tool_names() -> set[str]:
    return {d["name"] for d in _ALL_TOOL_DEFINITIONS}


class TestCommandSurface:
    def test_every_command_is_placed_deliberately(self):
        commands = _command_names()
        placed = _defined_tool_names() | get_effective_exclusions() | KNOWN_AUTO_REGISTERED
        unplaced = sorted(commands - placed)
        assert not unplaced, (
            f"{len(unplaced)} CommandHandler command(s) are neither in "
            f"_ALL_TOOL_DEFINITIONS, nor excluded from MCP, nor listed in "
            f"KNOWN_AUTO_REGISTERED: {', '.join(unplaced)}. Add a typed definition "
            "in src/tools/definitions.py (preferred), exclude it in "
            "src/mcp_registration.py, or add it to KNOWN_AUTO_REGISTERED with a "
            "comment saying why."
        )

    def test_auto_registered_list_does_not_rot(self):
        """Every name in the debt ledger must still be an unplaced command."""
        commands = _command_names()
        typed = _defined_tool_names()
        stale = sorted(
            n for n in KNOWN_AUTO_REGISTERED if n not in commands or n in typed
        )
        assert not stale, (
            "KNOWN_AUTO_REGISTERED lists names that no longer need to be there "
            f"(command removed, or a typed definition now exists): {', '.join(stale)}. "
            "Delete them — the list must shrink over time."
        )

    def test_tool_definitions_have_no_orphans(self):
        """Every typed definition maps to a real command or a plugin-provided one."""
        commands = _command_names()
        # Plugin-contributed tools are registered at runtime, so a definition
        # without a _cmd_ method is only acceptable when it is tagged as such.
        orphans = sorted(
            d["name"]
            for d in _ALL_TOOL_DEFINITIONS
            if d["name"] not in commands
            and not d.get("_plugin")
            and d["name"] not in PENDING_ON_ANOTHER_LANE
        )
        assert not orphans, (
            "_ALL_TOOL_DEFINITIONS defines tools with no matching "
            f"CommandHandler._cmd_* method: {', '.join(orphans)}"
        )

    def test_tool_definitions_are_unique(self):
        names = [d["name"] for d in _ALL_TOOL_DEFINITIONS]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        assert not duplicates, f"duplicate tool definitions: {', '.join(duplicates)}"

    def test_tool_definitions_are_well_formed(self):
        for definition in _ALL_TOOL_DEFINITIONS:
            assert definition.get("name"), definition
            assert definition.get("description"), definition["name"]
            schema = definition.get("input_schema")
            assert isinstance(schema, dict), definition["name"]
            assert schema.get("type") == "object", definition["name"]


class TestOpsCommandsExposed:
    """The doctor/costs commands ship with typed schemas, and stay in MCP."""

    @pytest.mark.parametrize("name", ["doctor", "get_costs"])
    def test_has_typed_definition(self, name):
        assert name in _defined_tool_names()

    @pytest.mark.parametrize("name", ["doctor", "get_costs"])
    def test_command_exists(self, name):
        assert hasattr(CommandHandler, f"_cmd_{name}")

    @pytest.mark.parametrize("name", ["doctor", "get_costs"])
    def test_not_excluded_from_mcp(self, name):
        assert name not in DEFAULT_EXCLUDED_COMMANDS


class TestRunCommandStaysExcluded:
    """``run_command`` is a known trust-boundary violation — keep it contained.

    Design ``trust-and-ops`` §2.5: it executes an LLM-authored shell string on
    the daemon host.  Removing it from the MCP exclusion set would expose that
    to every external MCP client.
    """

    def test_still_in_default_exclusions(self):
        assert "run_command" in DEFAULT_EXCLUDED_COMMANDS

    def test_still_excluded_after_config_merge(self):
        assert "run_command" in get_effective_exclusions()

    def test_excluded_from_the_cli(self):
        from src.cli.auto_commands import EXCLUDED

        assert "run_command" in EXCLUDED

    def test_excluded_from_the_http_api(self):
        """``DEFAULT_EXCLUDED_COMMANDS`` gates MCP only — the API needs its own.

        Without this, ``POST /api/execute {"command": "run_command"}`` reaches
        ``_run_subprocess_shell`` over the network.
        """
        from src.api.codegen import API_EXCLUDED

        assert "run_command" in API_EXCLUDED

    async def test_api_execute_refuses_excluded_commands(self):
        """The generic endpoint must honour the same set as the typed routes."""
        from src.api.codegen import API_EXCLUDED
        from src.api.execute import ExecuteRequest, api_execute

        class Handler:
            calls: list = []

            async def execute(self, command, args):
                self.calls.append(command)
                return {"ok": True}

        handler = Handler()
        for command in sorted(API_EXCLUDED):
            response = await api_execute(
                ExecuteRequest(command=command, args={}), ch=handler
            )
            assert response.status_code == 403, command
        assert handler.calls == [], (
            f"excluded command(s) reached the CommandHandler: {handler.calls}"
        )

    async def test_api_execute_still_runs_ordinary_commands(self):
        from src.api.execute import ExecuteRequest, api_execute

        class Handler:
            async def execute(self, command, args):
                return {"echoed": command}

        response = await api_execute(
            ExecuteRequest(command="get_status", args={}), ch=Handler()
        )
        assert response.status_code == 200

    def test_shell_helper_has_exactly_one_caller(self):
        """No new *call* of the shell-string helper may appear — anywhere.

        Counting call sites rather than allow-listing files: a second caller
        added inside ``system_commands.py`` is just as much an R1 violation as
        one added elsewhere, and a file-level allowlist would wave it through.
        """
        from pathlib import Path

        src = Path(__file__).resolve().parent.parent / "src"
        # Every occurrence, split into the definition and the actual call
        # sites.  Bare re-export lines (``handler.py`` re-exports the helper
        # inside a multi-line import) are references, not calls.
        definitions: list[str] = []
        calls: list[str] = []
        for path in src.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "_run_subprocess_shell" not in text:
                continue
            rel = path.relative_to(src).as_posix()
            for lineno, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if "_run_subprocess_shell" not in stripped:
                    continue
                if stripped.startswith("#") or stripped.startswith('"'):
                    continue  # comment / docstring reference
                where = f"{rel}:{lineno}: {stripped}"
                if stripped.startswith(("def ", "async def ")):
                    definitions.append(where)
                elif stripped.startswith(("from ", "import ")):
                    continue  # import statement, not a call
                elif "_run_subprocess_shell(" in stripped:
                    calls.append(where)

        assert len(definitions) == 1 and definitions[0].startswith("commands/helpers.py"), (
            f"_run_subprocess_shell must be defined exactly once, in "
            f"commands/helpers.py; found: {definitions}"
        )
        assert len(calls) == 1, (
            "_run_subprocess_shell must have exactly one call site "
            f"(_cmd_run_command); found {len(calls)}: {calls}. Every caller is "
            "an R1 trust-boundary violation — see trust-and-ops §2.5."
        )
        assert calls[0].startswith("commands/system_commands.py"), calls
