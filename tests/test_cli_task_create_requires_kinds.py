"""``aq task create --requires-kind``: parsing, forwarding, persistence, parity.

The handwritten ``aq task create`` command is maintained by hand while the
backend's ``create_task`` capability surface grows from the contract model and
the MCP tool schema.  ``TestCreateOptionParity`` is the ratchet that keeps the
two from drifting silently: every backend argument is either reachable from a
CLI option or listed with a reason for its exclusion.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import click
import pytest
from click.testing import CliRunner

# Imported before src.cli.tasks: the tasks module is registered through app.
from src.cli.app import cli
from src.cli.exceptions import CommandError
from src.cli.tasks import _parse_requires_kinds
from src.commands.contracts.builtin import CreateTaskArgs
from src.commands.handler import CommandHandler
from src.database import Database
from src.models import Project, WorkspaceKind
from src.tools.definitions import _ALL_TOOL_DEFINITIONS
from tests.db_fixtures import lease_dsn


@pytest.fixture
def runner():
    return CliRunner()


def _capture_client(captured: dict, command: str = "create_task"):
    """A mock CLI client that records the args of ``command``."""

    async def mock_execute(name, args=None):
        if name == command:
            captured.update(args or {})
            return {"created": "task-1", "title": (args or {}).get("title", "")}
        return {}

    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    mock.execute = AsyncMock(side_effect=mock_execute)
    return mock


# ---------------------------------------------------------------------------
# Local parsing
# ---------------------------------------------------------------------------


class TestParseRequiresKinds:
    def test_bare_kind_stays_a_string(self):
        assert _parse_requires_kinds(("game-repo",)) == ["game-repo"]

    def test_kind_with_alias_becomes_a_dict(self):
        assert _parse_requires_kinds(("game-repo=primary",)) == [
            {"kind": "game-repo", "alias": "primary"}
        ]

    def test_mixed_forms_keep_their_order(self):
        assert _parse_requires_kinds(("vault", "game-repo=a", "game-repo=b")) == [
            "vault",
            {"kind": "game-repo", "alias": "a"},
            {"kind": "game-repo", "alias": "b"},
        ]

    def test_surrounding_whitespace_is_trimmed(self):
        assert _parse_requires_kinds((" game-repo = primary ",)) == [
            {"kind": "game-repo", "alias": "primary"}
        ]

    def test_no_values_is_an_empty_list(self):
        assert _parse_requires_kinds(()) == []

    @pytest.mark.parametrize(
        "value,fragment",
        [
            ("", "needs a kind id"),
            ("   ", "needs a kind id"),
            ("=primary", "missing the kind id"),
            ("game-repo=", "missing the alias"),
            ("a=b=c", "more than one '='"),
        ],
    )
    def test_malformed_values_raise_a_usage_error(self, value, fragment):
        with pytest.raises(click.UsageError) as exc:
            _parse_requires_kinds((value,))
        assert fragment in str(exc.value)


# ---------------------------------------------------------------------------
# Request capture — what the daemon actually receives
# ---------------------------------------------------------------------------


class TestRequestForwarding:
    def _invoke(self, runner, extra):
        captured: dict = {}
        with patch("src.cli.tasks._get_client", return_value=_capture_client(captured)):
            result = runner.invoke(
                cli,
                ["task", "create", "--project", "p1", "--title", "T", "--description", "D"]
                + extra,
            )
        return result, captured

    def test_bare_kind_is_forwarded_as_a_string(self, runner):
        result, captured = self._invoke(runner, ["--requires-kind", "game-repo"])
        assert result.exit_code == 0, result.output
        assert captured["requires_kinds"] == ["game-repo"]

    def test_alias_form_is_forwarded_as_a_dict(self, runner):
        result, captured = self._invoke(runner, ["--requires-kind", "game-repo=primary"])
        assert result.exit_code == 0, result.output
        assert captured["requires_kinds"] == [{"kind": "game-repo", "alias": "primary"}]

    def test_repeated_options_are_forwarded_in_order(self, runner):
        result, captured = self._invoke(
            runner,
            [
                "--requires-kind",
                "game-repo=a",
                "--requires-kind",
                "docs-repo",
            ],
        )
        assert result.exit_code == 0, result.output
        assert captured["requires_kinds"] == [
            {"kind": "game-repo", "alias": "a"},
            "docs-repo",
        ]

    def test_omitted_option_omits_the_field(self, runner):
        result, captured = self._invoke(runner, [])
        assert result.exit_code == 0, result.output
        assert "requires_kinds" not in captured

    def test_malformed_value_fails_before_any_request(self, runner):
        result, captured = self._invoke(runner, ["--requires-kind", "game-repo="])
        assert result.exit_code != 0
        assert "missing the alias" in result.output
        assert captured == {}

    def test_backend_rejection_of_an_unknown_kind_surfaces(self, runner):
        """Unknown kinds are the daemon's call; the CLI reports its error.

        The client turns a ``{"error": ...}`` response into ``CommandError``,
        so an undefined kind reaches the operator as a message, not as a
        locally invented validation rule that would go stale.
        """
        sent: dict = {}

        async def mock_execute(name, args=None):
            if name == "create_task":
                sent.update(args or {})
                raise CommandError(
                    "create_task",
                    "Kind 'nope' is not defined for project 'p1' and no system "
                    "default exists.",
                )
            return {}

        mock = AsyncMock()
        mock.__aenter__ = AsyncMock(return_value=mock)
        mock.__aexit__ = AsyncMock(return_value=False)
        mock.execute = AsyncMock(side_effect=mock_execute)

        with patch("src.cli.tasks._get_client", return_value=mock):
            result = runner.invoke(
                cli,
                [
                    "task", "create", "--project", "p1", "--title", "T",
                    "--description", "D", "--requires-kind", "nope",
                ],
            )
        # The CLI forwarded it rather than rejecting it locally...
        assert sent["requires_kinds"] == ["nope"]
        # ...and the daemon's verdict is what the operator reads.
        assert result.exit_code != 0
        assert "nope" in result.output

    def test_wizard_path_carries_the_flag(self, runner):
        """Flags mixed with the interactive wizard survive into the request."""
        captured: dict = {}
        wizard_params = {
            "project_id": "p1",
            "title": "wizard task",
            "description": "D",
            "priority": 100,
            "task_type": "feature",
        }
        with (
            patch("src.cli.tasks._get_client", return_value=_capture_client(captured)),
            patch("src.cli.menus.task_creation_wizard", return_value=dict(wizard_params)),
        ):
            # No --description, so the wizard branch runs.
            result = runner.invoke(
                cli,
                ["task", "create", "--project", "p1", "--requires-kind", "game-repo=primary"],
            )
        assert result.exit_code == 0, result.output
        assert captured["title"] == "wizard task"
        assert captured["requires_kinds"] == [{"kind": "game-repo", "alias": "primary"}]

    def test_graph_path_rejects_the_option(self, runner, tmp_path):
        graph = tmp_path / "graph.json"
        graph.write_text('{"version": 1, "nodes": [{"key": "a", "title": "A"}]}')
        captured: dict = {}
        with patch(
            "src.cli.tasks._get_client",
            return_value=_capture_client(captured, "create_task_graph"),
        ):
            result = runner.invoke(
                cli,
                [
                    "task", "create", "--project", "p1", "--graph", str(graph),
                    "--requires-kind", "game-repo",
                ],
            )
        assert result.exit_code != 0
        assert "not supported with --graph" in result.output
        assert captured == {}

    def test_from_spec_path_rejects_the_option(self, runner):
        captured: dict = {}
        with patch(
            "src.cli.tasks._get_client",
            return_value=_capture_client(captured, "create_task_graph"),
        ):
            result = runner.invoke(
                cli,
                [
                    "task", "create", "--project", "p1", "--from-spec", "docs/x.md",
                    "--requires-kind", "game-repo",
                ],
            )
        assert result.exit_code != 0
        assert "not supported with --graph" in result.output
        assert captured == {}

    def test_graph_path_without_the_option_is_unaffected(self, runner, tmp_path):
        graph = tmp_path / "graph.json"
        graph.write_text('{"version": 1, "nodes": [{"key": "a", "title": "A"}]}')
        captured: dict = {}
        mock = _capture_client(captured, "create_task_graph")

        async def mock_execute(name, args=None):
            if name == "create_task_graph":
                captured.update(args or {})
                return {
                    "parent_id": None,
                    "parent_title": None,
                    "provisional": True,
                    "task_ids": ["t.1"],
                    "nodes": [],
                    "dry_run": False,
                    "created": True,
                }
            return {}

        mock.execute = AsyncMock(side_effect=mock_execute)
        with patch("src.cli.tasks._get_client", return_value=mock):
            result = runner.invoke(
                cli, ["task", "create", "--project", "p1", "--graph", str(graph)]
            )
        assert result.exit_code == 0, result.output
        assert "requires_kinds" not in captured


# ---------------------------------------------------------------------------
# Persistence — the parsed values against a real handler and database
# ---------------------------------------------------------------------------


@pytest.fixture
async def handler_db():
    db = Database(lease_dsn("test.db"))
    await db.initialize()
    await db.create_project(Project(id="p1", name="test"))
    await db.upsert_workspace_kind(
        WorkspaceKind(
            project_id="p1",
            id="game-repo",
            description="Game repo",
            writable=True,
            lockable=True,
            is_git_repo=True,
            default_lock_mode="exclusive",
            created_at=time.time(),
            updated_at=time.time(),
        )
    )
    orch = MagicMock()
    orch.db = db
    orch._emit_notify = AsyncMock()
    config = MagicMock()
    config.workspace_dir = "/tmp/test"
    handler = CommandHandler(orch, config)
    handler._active_project_id = "p1"
    yield handler, db
    await db.close()


class TestParsedValuesPersist:
    async def test_bare_kind_persists_without_an_alias(self, handler_db):
        handler, db = handler_db
        result = await handler._cmd_create_task({
            "project_id": "p1",
            "title": "bare",
            "requires_kinds": _parse_requires_kinds(("game-repo",)),
        })
        rows = await db.fetch_task_workspace_requirements(result["created"])
        assert [(r.kind_id, r.alias) for r in rows] == [("game-repo", None)]

    async def test_alias_form_persists_the_alias(self, handler_db):
        handler, db = handler_db
        result = await handler._cmd_create_task({
            "project_id": "p1",
            "title": "aliased",
            "requires_kinds": _parse_requires_kinds(
                ("game-repo=primary", "game-repo=secondary")
            ),
        })
        rows = await db.fetch_task_workspace_requirements(result["created"])
        assert sorted(r.alias for r in rows) == ["primary", "secondary"]

    async def test_omitted_option_writes_no_requirement_rows(self, handler_db):
        handler, db = handler_db
        result = await handler._cmd_create_task({
            "project_id": "p1",
            "title": "default",
            **(
                {"requires_kinds": _parse_requires_kinds(())}
                if _parse_requires_kinds(())
                else {}
            ),
        })
        rows = await db.fetch_task_workspace_requirements(result["created"])
        assert rows == []

    async def test_unknown_kind_is_rejected_by_the_backend(self, handler_db):
        handler, _ = handler_db
        result = await handler._cmd_create_task({
            "project_id": "p1",
            "title": "unknown",
            "requires_kinds": _parse_requires_kinds(("nope=primary",)),
        })
        assert "error" in result
        assert "nope" in result["error"]


# ---------------------------------------------------------------------------
# Parity ratchet
# ---------------------------------------------------------------------------


def _create_task_tool_properties() -> set[str]:
    for tool in _ALL_TOOL_DEFINITIONS:
        if tool["name"] == "create_task":
            return set(tool["input_schema"]["properties"])
    raise AssertionError("create_task is missing from the MCP tool definitions")


def _cli_create_params() -> set[str]:
    from src.cli.tasks import task_create

    return {p.name for p in task_create.params if isinstance(p, click.Option)}


#: Backend argument -> the ``aq task create`` option parameter that reaches it.
BACKEND_ARG_TO_CLI_PARAM = {
    "title": "title",
    "project_id": "project",
    "description": "description",
    "priority": "priority",
    "task_type": "task_type",
    "profile_id": "profile_id",
    "intelligence_class": "intelligence_class",
    "integration_mode": "integration_mode",
    "parent_id": "parent_id",
    "root": "root",
    "reason": "reason",
    "deliverables": "deliverables",
    "requires_kinds": "requires_kinds",
}

#: Backend arguments deliberately not exposed by the handwritten command.
#: Adding a backend argument without either wiring it up here or recording a
#: reason fails ``test_every_backend_argument_is_wired_or_excluded``.
INTENTIONALLY_EXCLUDED = {
    "attachments": "Discord-only; local paths downloaded by the bot, not typed by hand.",
    "affinity_agent_id": "Scheduler hint set by the orchestrator, not by an operator.",
    "affinity_reason": "Only meaningful alongside affinity_agent_id.",
    "dedup_key": "Idempotent creation is 'ensure_task', a separate command.",
    "depends_on": "Edges are added after creation with 'aq task deps'.",
    "discovered_from": "Provenance stamped by worker filing, not a CLI flag.",
    "labels": "Labels are managed by the auto-generated label commands.",
    "preferred_workspace_id": (
        "Superseded by requires_kinds; workspace instances are chosen at acquisition."
    ),
    "skip_verification": "Policy switch reserved for the daemon and playbooks.",
    "workspace_mode": (
        "'directory-isolated' is unimplemented and 'branch-isolated' is a deprecated "
        "alias, so the only reachable value is the default."
    ),
}

#: CLI options with no matching backend argument, and why.
CLI_ONLY_PARAMS = {
    "agent_type": "Handler-level cascade override; not part of the contract model.",
    "graph_file": "Selects the create_task_graph command instead.",
    "from_spec": "Selects the create_task_graph command instead.",
    "dry_run": "Graph-only validation switch.",
}


class TestCreateOptionParity:
    def test_every_backend_argument_is_wired_or_excluded(self):
        backend_args = set(CreateTaskArgs.model_fields) | _create_task_tool_properties()
        accounted = set(BACKEND_ARG_TO_CLI_PARAM) | set(INTENTIONALLY_EXCLUDED)
        missing = backend_args - accounted
        assert not missing, (
            "create_task gained arguments the handwritten 'aq task create' neither "
            f"exposes nor excludes: {sorted(missing)}. Add an option, or record the "
            "reason in INTENTIONALLY_EXCLUDED."
        )

    def test_wired_arguments_have_a_real_cli_option(self):
        params = _cli_create_params()
        missing = {
            arg: param
            for arg, param in BACKEND_ARG_TO_CLI_PARAM.items()
            if param not in params
        }
        assert not missing, f"CLI options went missing for: {missing}"

    def test_exclusion_table_has_no_stale_entries(self):
        backend_args = set(CreateTaskArgs.model_fields) | _create_task_tool_properties()
        stale = set(INTENTIONALLY_EXCLUDED) - backend_args
        assert not stale, f"INTENTIONALLY_EXCLUDED names arguments the backend dropped: {stale}"

        overlap = set(INTENTIONALLY_EXCLUDED) & set(BACKEND_ARG_TO_CLI_PARAM)
        assert not overlap, f"Arguments both wired and excluded: {sorted(overlap)}"

    def test_every_cli_option_is_accounted_for(self):
        wired = set(BACKEND_ARG_TO_CLI_PARAM.values()) | set(CLI_ONLY_PARAMS)
        unexplained = _cli_create_params() - wired
        assert not unexplained, (
            f"'aq task create' grew options with no recorded backend mapping: "
            f"{sorted(unexplained)}"
        )

    def test_requires_kinds_is_reachable(self):
        """The gap this file closes: the backend field has a CLI option."""
        assert "requires_kinds" in CreateTaskArgs.model_fields
        assert "requires_kinds" in _create_task_tool_properties()
        assert BACKEND_ARG_TO_CLI_PARAM["requires_kinds"] in _cli_create_params()
