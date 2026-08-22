"""Shared command handler for AgentQueue.

This module provides the single code path for all operational commands.
Both the Discord slash commands and the chat agent LLM tools delegate
their business logic here, keeping formatting and presentation separate.

This is the Command Pattern in action: every operation the system supports
(50+ commands) is routed through CommandHandler.execute(name, args).  The
two callers -- Discord slash commands and Supervisor LLM tool-use -- never
contain business logic themselves; they translate their inputs into a dict,
call execute(), and format the returned dict for their respective UIs.

The benefit is feature parity by construction.  A new command added here is
immediately available to both Discord and the chat agent without duplicating
any logic.

Related modules:

- ``src/tool_registry.py`` — JSON Schema definitions for every tool.
  Each tool's ``name`` maps to a ``_cmd_{name}`` method here.
- ``src/supervisor.py`` — The LLM tool-use loop that calls ``execute()``.
- ``src/discord/commands.py`` — Discord slash commands that call ``execute()``.

See ``specs/command-handler.md`` for the command reference specification.
"""

from __future__ import annotations

import contextvars
import os
from collections.abc import Callable

import logging

from src.config import AppConfig
from src.orchestrator import Orchestrator
from src.logging_config import CorrelationContext

# Per-asyncio-task state. ContextVars give each concurrent caller (Discord
# message, supervisor-platform task, hook LLM, reflection retry) its own
# value without races, instead of stomping a shared singleton attribute.
# Each new asyncio.Task inherits its parent's snapshot and can mutate freely.
_active_project_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_active_project_id_var", default=None
)
_current_conversation_context_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_conversation_context_var", default=None
)
_caller_profile_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_caller_profile_id_var", default=None
)
_plan_subtask_creation_mode_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_plan_subtask_creation_mode_var", default=False
)

# Mixin imports — each provides one domain of _cmd_* methods
from src.commands.system_commands import SystemCommandsMixin
from src.commands.project_commands import ProjectCommandsMixin
from src.commands.task_commands import TaskCommandsMixin
from src.commands.agent_commands import AgentCommandsMixin
from src.commands.profile_commands import ProfileCommandsMixin
from src.commands.mcp_commands import McpCommandsMixin
from src.commands.notes_commands import NotesCommandsMixin
from src.commands.playbook_commands import PlaybookCommandsMixin
from src.commands.workflow_commands import WorkflowCommandsMixin
from src.commands.plugin_commands import PluginCommandsMixin
from src.commands.tool_commands import ToolCommandsMixin
from src.commands.event_commands import EventCommandsMixin
from src.commands.discord_commands import DiscordCommandsMixin

# Framework-overhaul substrate mixins (Wave 0).  Empty today — registered
# here so the Wave 1/2 lanes add methods to their own module without
# touching this file.  See docs/analysis/execution-plan.md §1.1.
from src.commands.gate_commands import GateCommandsMixin
from src.commands.message_commands import MessageCommandsMixin
from src.commands.session_commands import SessionCommandsMixin
from src.commands.surface_commands import SurfaceCommandsMixin
from src.commands.ops_commands import OpsCommandsMixin
from src.commands.worktree_commands import WorktreeCommandsMixin
from src.commands.git_commands import GitCommandsMixin
from src.playbooks.validator_command import PlaybookValidateInstallMixin

logger = logging.getLogger(__name__)


# Re-export helper functions from helpers module for backward compatibility
from src.commands.helpers import (  # noqa: E402, F401
    _build_archive_note,
    _collect_tree_task_ids,
    _collect_tree_tasks,
    _count_by,
    _count_subtree,
    _count_subtree_by_status,
    _count_tree_stats,
    _dep_annotation,
    _DEP_MAX_CHARS,
    _format_interval,
    _format_status_summary,
    _format_task_dep_line,
    _format_task_tree,
    _LEVEL_PRIORITY,
    _parse_delay,
    _parse_relative_time,
    _RELATIVE_TIME_UNITS,
    _render_tree_node,
    _run_subprocess,
    _run_subprocess_shell,
    _status_emoji,
    _tail_log_lines,
    _TREE_BRANCH,
    _TREE_CHAR_BUDGET,
    _TREE_LAST,
    _TREE_PIPE,
    _TREE_SPACE,
    _tree_dep_annotation,
    format_dependency_list,
)


# ---------------------------------------------------------------------------
# Feature pauses (docs/specs/implementation/feature-pauses.md §5)
#
# Every command surface -- Discord slash commands, the embedded MCP server,
# the HTTP API and the ``aq`` CLI auto-groups -- dispatches through
# ``CommandHandler.execute()``, so one gate here covers all of them.
#
# Read-only commands are gated too, deliberately: "paused" is one crisp
# contract rather than a per-command judgement call.
# ---------------------------------------------------------------------------

#: Exact error strings.  Do not reword -- operators and docs match on these.
MEMORY_PAUSED_ERROR = "memory is paused (memory.enabled=false)"
PLAYBOOKS_PAUSED_ERROR = "playbooks are paused (playbooks.enabled=false)"

PAUSED_PLAYBOOK_COMMANDS: frozenset[str] = frozenset(
    {
        # src/commands/playbook_commands.py (16)
        "list_playbooks",
        "list_playbook_runs",
        "inspect_playbook_run",
        "resume_playbook",
        "recover_workflow",
        "compile_playbook",
        "show_playbook_graph",
        "run_playbook",
        "dry_run_playbook",
        "playbook_health",
        "playbook_graph_view",
        "get_playbook_source",
        "update_playbook_source",
        "set_playbook_enabled",
        "create_playbook",
        "delete_playbook",
        # src/commands/workflow_commands.py (5)
        "create_workflow",
        "get_workflow",
        "list_workflows",
        "advance_workflow_stage",
        "workflow_pipeline_view",
    }
)

#: Memory command names not caught by the prefix rule below.  The names are
#: owned by the external aq-memory plugin, so the prefix rule is primary and
#: this set is the escape hatch for outliers.
PAUSED_MEMORY_COMMAND_EXTRAS: frozenset[str] = frozenset({"memory", "compact_memory"})


def _is_memory_command(name: str) -> bool:
    """True when *name* belongs to the (externally owned) memory surface.

    Plugin commands are registered both bare (``memory_search``) and
    plugin-qualified (``aq-memory.memory_search``) -- see
    ``PluginContext.register_command`` -- so both shapes are matched.
    """
    return name in PAUSED_MEMORY_COMMAND_EXTRAS or name.startswith(
        ("memory_", "memory.", "aq-memory.")
    )


class CommandHandler(
    SystemCommandsMixin,
    ProjectCommandsMixin,
    TaskCommandsMixin,
    AgentCommandsMixin,
    ProfileCommandsMixin,
    McpCommandsMixin,
    NotesCommandsMixin,
    PlaybookCommandsMixin,
    WorkflowCommandsMixin,
    PluginCommandsMixin,
    ToolCommandsMixin,
    EventCommandsMixin,
    DiscordCommandsMixin,
    # -- Framework-overhaul substrate mixins (empty until their lane) ----
    GateCommandsMixin,
    MessageCommandsMixin,
    SessionCommandsMixin,
    SurfaceCommandsMixin,
    OpsCommandsMixin,
    WorktreeCommandsMixin,
    # -- dv2 phase 2 mixins -----------------------------------------------
    GitCommandsMixin,
    # -- dv2 phase 6 mixins -----------------------------------------------
    PlaybookValidateInstallMixin,
):
    """Unified command execution layer for AgentQueue (Command Pattern).

    This is the single code path for every operation in the system.  Both
    the Discord slash commands and the Supervisor LLM tools call
    ``handler.execute(name, args)`` -- neither contains business logic.

    Convention for command methods:
        Each ``_cmd_*`` method receives a flat ``dict`` of arguments and
        returns a ``dict``.  On success the dict contains domain data
        (e.g. ``{"task": {...}}``).  On failure it contains
        ``{"error": "human-readable message"}``.  Callers never need to
        catch exceptions -- ``execute()`` wraps every call in a try/except.

    Active project context:
        ``_active_project_id`` lets callers set an implicit project scope
        so users chatting in a project's Discord channel don't have to
        pass ``project_id`` on every command.  Many ``_cmd_*`` methods
        fall back to this when no explicit project_id is provided.

    Security helpers:
        ``_validate_path`` sandboxes all file operations to the workspace
        directory or a registered repo source path -- the chat agent can
        never escape to arbitrary filesystem locations.

        ``_resolve_repo_path`` centralizes the surprisingly tricky logic
        for finding the right git checkout directory given a combination
        of project_id, workspace, and the active project fallback.

    Command methods are organized into domain-specific mixins for
    maintainability.  Each mixin provides one group of ``_cmd_*`` methods:

    - :class:`SystemCommandsMixin` — config, diagnostics, orchestrator control
    - :class:`ProjectCommandsMixin` — project CRUD, channels
    - :class:`TaskCommandsMixin` — task CRUD, lifecycle, dependencies
    - :class:`AgentCommandsMixin` — agent/workspace management
    - :class:`ProfileCommandsMixin` — agent profile CRUD
    - :class:`NotesCommandsMixin` — note path helpers
    - :class:`PlaybookCommandsMixin` — playbook compile, run, health
    - :class:`WorkflowCommandsMixin` — workflow CRUD, stage advancement
    - :class:`PluginCommandsMixin` — plugin lifecycle
    - :class:`ToolCommandsMixin` — tool discovery
    - :class:`EventCommandsMixin` — events, token usage, logs
    - :class:`DiscordCommandsMixin` — Discord messaging
    """

    def __init__(
        self,
        orchestrator: Orchestrator,
        config: AppConfig,
        doctor_registry=None,
    ):
        self.orchestrator = orchestrator
        self.config = config
        # Optional DoctorRegistry override.  Normally None: the daemon-wide
        # registry is built in ``src/main.py`` and attached to the
        # orchestrator, which ``OpsCommandsMixin.doctor_registry`` falls back
        # to.  Passing one explicitly is for tests and embedded uses.
        self._doctor_registry = doctor_registry
        # Optional callback invoked after a project is deleted.
        # Signature: callback(project_id: str) -> None
        # The Discord bot registers this to clean in-memory channel caches.
        self._on_project_deleted: Callable[[str], None] | None = None
        # Optional async callback invoked after a project is created.
        # Signature: async callback(project_id: str, auto_create_channels: bool) -> None
        # The Discord bot registers this to auto-create per-project channels.
        self._on_project_created: Callable | None = None
        # Optional callback invoked after a note is written or appended.
        # Signature: async callback(project_id, note_filename, note_path) -> None
        # The Discord bot registers this to auto-refresh viewed notes.
        self.on_note_written: Callable | None = None
        # aq-surface Phase S2: server-injected RequestScope dict from
        # /api/execute (never client-supplied — stripped from args before
        # dispatch).  Handlers that need it (e.g. ``_cmd_prime``) read
        # ``self._current_scope`` explicitly; everything else ignores it.
        self._current_scope: dict | None = None

    # The following four properties are backed by module-level ContextVars
    # so concurrent callers (Discord, supervisor-platform tasks, playbook
    # nodes, reflection retries) don't stomp each other's state. Reads/
    # writes look identical to the previous instance attributes; the
    # property setter routes the assignment into the ContextVar for the
    # current asyncio task.
    @property
    def _active_project_id(self) -> str | None:
        return _active_project_id_var.get()

    @_active_project_id.setter
    def _active_project_id(self, value: str | None) -> None:
        _active_project_id_var.set(value)

    @property
    def _current_conversation_context(self) -> str | None:
        # Conversation context set by the Supervisor during its chat loop.
        # When set, _cmd_create_task stores it as task_context so agents
        # have the same thread chain context that the supervisor had when
        # creating the task.
        return _current_conversation_context_var.get()

    @_current_conversation_context.setter
    def _current_conversation_context(self, value: str | None) -> None:
        _current_conversation_context_var.set(value)

    @property
    def _plan_subtask_creation_mode(self) -> bool:
        # When True, _cmd_create_task creates tasks as DEFINED instead of
        # READY.  Set by supervisor.break_plan_into_tasks() so plan subtasks
        # are born DEFINED, eliminating the race condition that required
        # project-wide plan processing locks.
        return _plan_subtask_creation_mode_var.get()

    @_plan_subtask_creation_mode.setter
    def _plan_subtask_creation_mode(self, value: bool) -> None:
        _plan_subtask_creation_mode_var.set(value)

    @property
    def _caller_profile_id(self) -> str | None:
        # The profile id of the caller currently invoking commands.  Set by
        # the playbook runner (and, eventually, the task platform) so that
        # ``_cmd_create_task`` can:
        #   1. default-inherit the caller's profile when the LLM omits
        #      ``profile_id`` — preserves the capability sandbox by default;
        #   2. reject upward escalation when the LLM explicitly passes a
        #      ``profile_id`` whose allowed_tools / mcp_servers exceed the
        #      caller's.
        # See ``docs/specs/design/sandboxed-playbooks.md``.
        return _caller_profile_id_var.get()

    @_caller_profile_id.setter
    def _caller_profile_id(self, value: str | None) -> None:
        _caller_profile_id_var.set(value)

    @property
    def db(self):
        return self.orchestrator.db

    def set_active_project(self, project_id: str | None) -> None:
        self._active_project_id = project_id

    def set_caller_profile(self, profile_id: str | None) -> None:
        """Bind the capability profile of the caller invoking commands.

        Called by the playbook runner (and task adapters) around their
        ``supervisor.chat()`` invocations so that ``_cmd_create_task`` can
        enforce profile inheritance and prevent capability escalation.

        Pass ``None`` to clear the binding when the call completes.
        """
        self._caller_profile_id = profile_id

    async def resolve_project_id(self, raw: str) -> str:
        """Resolve a potentially malformed project ID to the correct one.

        LLMs frequently guess project IDs from context (Discord channel
        names, playbook IDs, etc.) and get them wrong.  This method
        normalises common variations:

        1. Exact match against known project IDs
        2. Strip common prefixes (``project-``, ``project_``)
        3. Underscore ↔ hyphen normalisation
        4. Match against project names (case-insensitive)
        5. Match against Discord channel names (``project-{id}``)

        Returns the original string unchanged if no match is found —
        downstream commands will produce a clear "not found" error.
        """
        # Fast path: already correct
        project = await self.db.get_project(raw)
        if project:
            return raw

        # Build lookup tables from known projects (cached per call — cheap
        # since there are typically <20 projects)
        all_projects = await self.db.list_projects()
        ids = {p.id for p in all_projects}
        name_to_id = {p.name.lower(): p.id for p in all_projects}

        # Strip common prefixes the LLM adds
        for prefix in ("project-", "project_"):
            if raw.startswith(prefix):
                stripped = raw[len(prefix) :]
                if stripped in ids:
                    return stripped

        # Underscore ↔ hyphen swap
        swapped = raw.replace("_", "-")
        if swapped in ids:
            return swapped
        swapped = raw.replace("-", "_")
        if swapped in ids:
            return swapped

        # Match by project name (case-insensitive)
        if raw.lower() in name_to_id:
            return name_to_id[raw.lower()]

        # Match against Discord channel name pattern "project-{id}"
        for pid in ids:
            if raw == f"project-{pid}" or raw == f"project_{pid}":
                return pid

        return raw

    async def _resolve_project_id_in_args(self, args: dict) -> None:
        """Normalise ``project_id`` in a command args dict in-place.

        If the caller provided an explicit ``project_id``, attempt fuzzy
        resolution (channel-name patterns, case-insensitive name matching).
        If no ``project_id`` was provided (empty or missing), fall back to
        the active project.
        """
        raw = args.get("project_id")
        if raw and isinstance(raw, str):
            args["project_id"] = await self.resolve_project_id(raw)
        elif self._active_project_id:
            args["project_id"] = self._active_project_id

    async def _validate_path(self, path: str) -> str | None:
        """Validate that a path resolves within an allowed directory.

        Allowed roots: workspace_dir, any registered repo source_path,
        and any registered workspace path.
        """
        real = os.path.realpath(path)
        workspace_real = os.path.realpath(self.config.workspace_dir)
        if real.startswith(workspace_real + os.sep) or real == workspace_real:
            return real
        repos = await self.db.list_repos()
        for repo in repos:
            if repo.source_path:
                repo_real = os.path.realpath(repo.source_path)
                if real.startswith(repo_real + os.sep) or real == repo_real:
                    return real
        # Also allow paths within any registered workspace
        workspaces = await self.db.list_workspaces()
        for ws in workspaces:
            ws_real = os.path.realpath(ws.workspace_path)
            if real.startswith(ws_real + os.sep) or real == ws_real:
                return real
        return None

    # Commands whose full args/result payloads are logged at INFO (mutating
    # operations). Everything else logs at DEBUG so routine reads don't spam.
    _MUTATING_CMD_PREFIXES: tuple[str, ...] = (
        "create_",
        "update_",
        "edit_",
        "delete_",
        "add_",
        "assign_",
        "approve_",
        "reject_",
        "archive_",
        "resume_",
        "pause_",
        "stop_",
        "transition_",
    )

    @staticmethod
    def _is_mutating(name: str) -> bool:
        return any(name.startswith(p) for p in CommandHandler._MUTATING_CMD_PREFIXES)

    @staticmethod
    def _preview(obj: object, limit: int = 600) -> str:
        """Render obj for a log line; truncate long descriptions/prompts."""
        try:
            import json as _json

            s = _json.dumps(obj, default=str)
        except Exception:
            s = repr(obj)
        if len(s) > limit:
            s = s[:limit] + f"…<truncated {len(s) - limit} chars>"
        return s

    def _paused_command_error(self, name: str) -> str | None:
        """Return the canonical paused-error string for *name*, or ``None``.

        See docs/specs/implementation/feature-pauses.md §5.  Read-only
        playbook/workflow commands are gated too -- one crisp contract.
        """
        if name in PAUSED_PLAYBOOK_COMMANDS and not self.config.playbooks.enabled:
            return PLAYBOOKS_PAUSED_ERROR
        if _is_memory_command(name) and not self.config.memory.enabled:
            return MEMORY_PAUSED_ERROR
        return None

    async def execute(self, name: str, args: dict) -> dict:
        """Execute a command by name and return a structured result dict.

        This is the single code path for all operational commands in the system.
        Both Discord slash commands and chat agent LLM tools call this method.
        """
        with CorrelationContext(command=name, component="command_handler"):
            # aq-surface Phase S2: pop the server-injected ``_scope`` off
            # BEFORE dispatch so no ``_cmd_*`` handler sees it in its
            # ``args`` unless it explicitly reads ``self._current_scope``.
            # Belt-and-braces defense — /api/execute already strips any
            # client-supplied ``_scope`` before forwarding the trusted one.
            scope = None
            if isinstance(args, dict) and "_scope" in args:
                args = dict(args)
                scope = args.pop("_scope")
            self._current_scope = scope
            mutating = self._is_mutating(name)
            if mutating:
                logger.info("cmd %s args=%s", name, self._preview(args))
            else:
                logger.debug("cmd %s args=%s", name, self._preview(args))
            try:
                # Normalise project_id in args before dispatching.
                # LLMs frequently guess wrong (channel names, underscores,
                # prefixes).  This resolves to the correct ID centrally.
                if "project_id" in args:
                    await self._resolve_project_id_in_args(args)

                # Subsystem pause gate (feature-pauses.md §5).  Runs before
                # dispatch so no paused code path is ever entered, and before
                # the plugin fallback so memory commands short-circuit even
                # when the plugin somehow is loaded.
                paused_error = self._paused_command_error(name)
                if paused_error:
                    logger.debug("cmd %s refused: %s", name, paused_error)
                    return {"success": False, "error": paused_error}

                handler = getattr(self, f"_cmd_{name}", None)
                if handler:
                    result = await handler(args)
                    if mutating:
                        logger.info("cmd %s result=%s", name, self._preview(result))
                    return result

                # Fallback to plugin registry
                if (
                    hasattr(self.orchestrator, "plugin_registry")
                    and self.orchestrator.plugin_registry
                ):
                    plugin_handler = self.orchestrator.plugin_registry.get_command(name)
                    if plugin_handler:
                        plugin_name = name.split(".")[0] if "." in name else name
                        try:
                            with CorrelationContext(plugin=plugin_name):
                                result = await plugin_handler(args)
                            self.orchestrator.plugin_registry.record_success(plugin_name)
                            if mutating:
                                logger.info(
                                    "cmd %s (plugin=%s) result=%s",
                                    name,
                                    plugin_name,
                                    self._preview(result),
                                )
                            return result
                        except Exception as e:
                            await self.orchestrator.plugin_registry.record_failure(
                                plugin_name, str(e)
                            )
                            logger.error(
                                "Plugin command %s failed: args=%s err=%s",
                                name,
                                self._preview(args),
                                e,
                                exc_info=True,
                            )
                            return {"error": f"Plugin command failed: {e}"}

                logger.warning("Unknown command requested: %s args=%s", name, self._preview(args))
                return {"error": f"Unknown command: {name}"}
            except Exception as e:
                logger.error(
                    "Command %s failed: args=%s err=%s",
                    name,
                    self._preview(args),
                    e,
                    exc_info=True,
                )
                return {"error": str(e)}
            finally:
                # Ensure scope does not leak across commands.
                self._current_scope = None
