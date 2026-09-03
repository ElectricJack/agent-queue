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
import json
import os
import time
from collections.abc import Callable

import logging
from typing import Any

from src.config import AppConfig
from src.orchestrator import Orchestrator
from src.logging_config import CorrelationContext

# Mixin imports — each provides one domain of _cmd_* methods
from src.commands.claim_commands import ClaimCommandsMixin
from src.commands.question_commands import QuestionCommandsMixin
from src.commands.system_commands import SystemCommandsMixin
from src.commands.project_commands import ProjectCommandsMixin
from src.commands.task_commands import TaskCommandsMixin
from src.commands.task_comment_commands import TaskCommentCommandsMixin
from src.commands.agent_commands import AgentCommandsMixin
from src.commands.profile_commands import ProfileCommandsMixin
from src.commands.mcp_commands import McpCommandsMixin
from src.commands.notes_commands import NotesCommandsMixin
from src.commands.playbook_commands import PlaybookCommandsMixin
from src.commands.playbook_cutover_commands import PlaybookCutoverCommandsMixin
from src.commands.playbook_migration_commands import PlaybookMigrationCommandsMixin
from src.commands.playbook_v2_commands import (
    PLAYBOOK_V2_ARTIFACT_COMMANDS,
    PLAYBOOK_V2_COMMANDS,
    PLAYBOOK_V2_COMPILER_COMMANDS,
    PlaybookV2CommandsMixin,
)
from src.commands.workflow_commands import WorkflowCommandsMixin
from src.commands.plugin_commands import PluginCommandsMixin
from src.commands.tool_commands import ToolCommandsMixin
from src.commands.event_commands import EventCommandsMixin
from src.commands.discord_commands import DiscordCommandsMixin
from src.commands.formula_commands import FormulaCommandsMixin
from src.commands.graph_commands import GraphCommandsMixin

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

# -- dv2 phase 6 mixins ---------------------------------------------------
from src.commands.proposal_commands import TaskProposalCommandsMixin
from src.commands.spec_commands import SpecCommandsMixin
from src.playbooks.validator_command import PlaybookValidateInstallMixin

logger = logging.getLogger(__name__)

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
#: The server-derived :class:`~src.api.auth.RequestScope`, as a dict.
#:
#: A ContextVar for the same reason as the four above, but the stakes are
#: higher: this one carries *identity*.  As a plain instance attribute it
#: was shared by every in-flight request, and ``execute``'s ``finally``
#: cleared it unconditionally — so any command that started while another
#: was awaiting (the 5s cascade, a second agent, a dashboard poll) blanked
#: the first one's scope mid-flight.  Observed as ``aq task close
#: --claim-next`` answering ``out_of_scope: task_claim needs a session in
#: scope``: ``task_close`` awaits the whole completion pipeline (git ops
#: included) before calling ``_cmd_task_claim``, which is ample time for a
#: concurrent command to land.
_current_scope_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_current_scope_var", default=None
)


from src.commands.authorization import authorize_command, denial_result  # noqa: E402
from src.commands.principal import (  # noqa: E402
    SERVER_OWNED_ARG_KEYS,
    _principal_var,
    current_principal,
)

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
        # src/commands/playbook_commands.py (17)
        "list_playbooks",
        "list_playbook_runs",
        "inspect_playbook_run",
        "resume_playbook",
        "cancel_playbook_run",
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
    # src/commands/playbook_v2_commands.py (7 + the artifact chooser) --
    # the V2 semantic-graph surface pauses with the rest of the subsystem,
    # on top of its own ``playbooks.v2_api`` /
    # ``playbooks.v2_activation_writes`` flags.
    | PLAYBOOK_V2_COMMANDS
    | PLAYBOOK_V2_ARTIFACT_COMMANDS
    | PLAYBOOK_V2_COMPILER_COMMANDS
)

#: Memory command names not caught by the prefix rule below.  The names are
#: owned by the external aq-memory plugin, so the prefix rule is primary and
#: this set is the escape hatch for outliers.
PAUSED_MEMORY_COMMAND_EXTRAS: frozenset[str] = frozenset({"memory", "compact_memory"})


#: Keys that pass through ``_summarize_args`` verbatim (short identifiers we
#: always want visible on the wire) alongside a shortened rendering.
_ARGS_SUMMARY_PASSTHROUGH: frozenset[str] = frozenset(
    {"task_id", "project_id", "session_id", "gate_id", "proposal_id"}
)

#: Keys whose value is always redacted regardless of length.  Match is
#: case-insensitive and substring-based so ``api_key`` / ``API_KEY`` /
#: ``x-api-key`` all get caught.
_ARGS_SUMMARY_REDACT_KEYS: tuple[str, ...] = (
    "body",
    "content",
    "text",
    "token",
    "password",
    "api_key",
)

#: Max total length of the rendered args summary string.
_ARGS_SUMMARY_MAX_LEN: int = 200


def _summarize_args(command: str, args: dict | None) -> str:
    """Short, redacted rendering of *args* for the ``command.invoked`` event.

    Never dumps raw values on the bus.  Rules:

    - Passthrough keys (``task_id``/``project_id``/``session_id``/``gate_id``/
      ``proposal_id``) render as ``key=value`` when present.
    - Keys matching :data:`_ARGS_SUMMARY_REDACT_KEYS` render as
      ``key=<redacted len=N>``.
    - String values >80 chars render as ``key=<...len=N>``.
    - Everything else renders as ``key=<type>`` (small ints/bools verbatim).

    Total output is truncated to :data:`_ARGS_SUMMARY_MAX_LEN` characters
    with a trailing ``…`` marker so downstream UIs get a stable ceiling.
    """
    if not args:
        return ""
    parts: list[str] = []
    for key, value in args.items():
        if key == "_scope":
            # Server-injected trust envelope — never on the wire.
            continue
        key_lower = key.lower()
        if (
            command == "task_set" and key_lower in {"description", "expected_description"}
        ) or any(needle in key_lower for needle in _ARGS_SUMMARY_REDACT_KEYS):
            n = len(value) if isinstance(value, (str, bytes, list, dict)) else 0
            parts.append(f"{key}=<redacted len={n}>")
            continue
        if key in _ARGS_SUMMARY_PASSTHROUGH and isinstance(value, str):
            parts.append(f"{key}={value}")
            continue
        if isinstance(value, str):
            if len(value) > 80:
                parts.append(f"{key}=<...len={len(value)}>")
            else:
                parts.append(f"{key}={value}")
        elif isinstance(value, bool):
            parts.append(f"{key}={value}")
        elif isinstance(value, (int, float)):
            parts.append(f"{key}={value}")
        elif value is None:
            parts.append(f"{key}=None")
        elif isinstance(value, (list, tuple)):
            parts.append(f"{key}=<list len={len(value)}>")
        elif isinstance(value, dict):
            parts.append(f"{key}=<dict len={len(value)}>")
        else:
            parts.append(f"{key}=<{type(value).__name__}>")
    rendered = ", ".join(parts)
    if len(rendered) > _ARGS_SUMMARY_MAX_LEN:
        rendered = rendered[: _ARGS_SUMMARY_MAX_LEN - 1] + "…"
    return rendered


def _classify_result(result: object) -> tuple[bool, str | None]:
    """Return ``(ok, error_summary)`` for a command result dict.

    A command reports failure via one of two shapes:
    ``{"error": "..."}`` (top-level exception path) or
    ``{"success": False, "error": "..."}`` (structured refusal, e.g. a paused
    subsystem gate).  Any other shape counts as success.  The returned error
    string is truncated to ~200 chars so bus payloads stay bounded.
    """
    if not isinstance(result, dict):
        return True, None
    err = result.get("error")
    if err is not None:
        return False, str(err)[:200]
    if result.get("success") is False:
        # Structured refusal without an ``error`` key — fall back to a stable
        # marker so the frontend can still discriminate the outcome.
        return False, "success=False"
    return True, None


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
    ClaimCommandsMixin,
    QuestionCommandsMixin,
    SystemCommandsMixin,
    ProjectCommandsMixin,
    TaskCommandsMixin,
    TaskCommentCommandsMixin,
    AgentCommandsMixin,
    ProfileCommandsMixin,
    McpCommandsMixin,
    NotesCommandsMixin,
    PlaybookCommandsMixin,
    PlaybookV2CommandsMixin,
    PlaybookMigrationCommandsMixin,
    PlaybookCutoverCommandsMixin,
    WorkflowCommandsMixin,
    PluginCommandsMixin,
    ToolCommandsMixin,
    EventCommandsMixin,
    DiscordCommandsMixin,
    FormulaCommandsMixin,
    GraphCommandsMixin,
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
    TaskProposalCommandsMixin,
    SpecCommandsMixin,
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
    - :class:`PlaybookMigrationCommandsMixin` — V1→V2 inventory and waivers
    - :class:`PlaybookCutoverCommandsMixin` — V1 drain, runtime switch, window
    - :class:`PlaybookV2CommandsMixin` — V2 semantic graph, diff, activation
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
        # Backed by ``_current_scope_var`` — see its docstring.
        self._current_scope = None

    @property
    def _current_scope(self) -> dict | None:
        return _current_scope_var.get()

    @_current_scope.setter
    def _current_scope(self, value: dict | None) -> None:
        _current_scope_var.set(value)

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
        # READY, so plan subtasks are born DEFINED rather than racing to
        # READY before their parent-blocking dependency is wired up.
        return _plan_subtask_creation_mode_var.get()

    @_plan_subtask_creation_mode.setter
    def _plan_subtask_creation_mode(self, value: bool) -> None:
        _plan_subtask_creation_mode_var.set(value)

    @property
    def _caller_profile_id(self) -> str | None:
        """Shim over :func:`~src.commands.principal.current_principal`.

        The profile id of the caller currently invoking commands, used by
        ``_cmd_create_task`` to default-inherit the caller's profile and to
        reject upward escalation.  Package 0 moved the authoritative answer
        onto the request-local ``ExecutionPrincipal``, which is derived from
        the *session row* and therefore also exists for HTTP and MCP callers
        — the v1 gap that made the escalation check unreachable for real
        agents.

        The ContextVar is still consulted as a fallback so
        ``src/playbooks/runner.py`` keeps working untouched.

        **Removal package: Package 4**, when typed executors bind principals
        directly.  See ``docs/specs/design/sandboxed-playbooks.md``.
        """
        from src.commands.principal import current_principal

        principal = current_principal()
        if principal is not None and principal.profile_id:
            return principal.profile_id
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

        Package 0: this also binds a ``PLAYBOOK``-kind principal carrying the
        profile's capability policy, so playbook-driven commands go through
        the same authorization path as session-driven ones instead of a
        parallel one.  The policy is resolved lazily on first use rather than
        here, because this setter is synchronous and profile lookup is not.
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

    def has_command(self, name: str) -> bool:
        """True when ``execute(name, ...)`` can actually dispatch *name*.

        A tool having a JSON-Schema definition in ``src/tools/definitions.py``
        does **not** imply it is executable: several names (the whole
        ``memory`` category, for instance) are declared there so MCP and the
        CLI get a tight, intentional schema, but their backing implementation
        lives in an external plugin (``aq-memory``).  When that plugin is not
        installed, ``execute()`` falls through to
        ``{"error": "Unknown command: ..."}``.

        ``load_tools`` uses this to avoid advertising tools that cannot run.
        """
        if getattr(self, f"_cmd_{name}", None):
            return True
        registry = getattr(self.orchestrator, "plugin_registry", None)
        if registry and registry.get_command(name):
            return True
        return False

    def contracted_commands(self) -> frozenset[str]:
        """Commands backed by a typed Playbook V2 contract (read-only)."""
        from src.commands.contracts import CONTRACTS

        return CONTRACTS.names()

    # -- Execution principal seam (Playbook V2 Package 0 §3.5) -------------
    #
    # This is the ONE place a principal is constructed for a request, and the
    # single thing Package 0 reverts by deleting.  Everything downstream —
    # dispatch authorization, delegation narrowing, tool-schema filtering —
    # reads ``current_principal()``.

    #: ``{session_id: (principal_inputs, expires_at)}``.  One extra
    #: ``get_session`` + ``get_profile`` per command is the cost of deriving
    #: identity rather than minting it; a short TTL keeps that off the hot
    #: path without letting a stale-wide policy linger.  Invalidated outright
    #: by ``sync_profile_to_db`` on a successful upsert, so a profile edit
    #: takes effect within one vault sync rather than one TTL.
    _PRINCIPAL_CACHE_TTL: float = 30.0

    def _invalidate_principal_cache(self, session_id: str | None = None) -> None:
        """Drop cached principal inputs — all of them, or one session's."""
        cache = getattr(self, "_principal_cache", None)
        if cache is None:
            return
        if session_id is None:
            cache.clear()
        else:
            cache.pop(session_id, None)

    @property
    def _command_resolver(self):
        """Tells :mod:`src.commands.authorization` what kind of name it has."""
        from src.commands.authorization import CommandHandlerResolver

        resolver = getattr(self, "_command_resolver_cached", None)
        if resolver is None:
            resolver = CommandHandlerResolver(self)
            self._command_resolver_cached = resolver
        return resolver

    async def _principal_from_scope(self, scope: dict | None):
        """Derive the request's principal from the server-supplied scope.

        Fails closed at every step: an unresolvable identity yields
        ``DENY_ALL`` with a provenance entry naming why, never a permissive
        default.

        ``elevated`` is carried onto the principal but is *not* a policy
        bypass — see the module docstring of ``src.commands.principal``.
        """
        from src.commands.principal import (
            TRUSTED_LOCAL,
            ExecutionPrincipal,
            PrincipalKind,
        )
        from src.profiles.capabilities import DENY_ALL, capability_policy_for

        if not scope or scope.get("kind") != "session":
            return TRUSTED_LOCAL

        session_id = scope.get("session_id")
        common = {
            "kind": PrincipalKind.SESSION,
            "session_id": session_id,
            "task_id": scope.get("task_id"),
            "project_id": scope.get("project_id"),
            "elevated": bool(scope.get("elevated")),
        }

        def _closed(reason: str):
            logger.warning(
                "principal_fail_closed reason=%s session_id=%s", reason, session_id
            )
            return ExecutionPrincipal(policy=DENY_ALL, provenance=(reason,), **common)

        if not session_id:
            return _closed("session-not-found")

        cache = getattr(self, "_principal_cache", None)
        if cache is None:
            cache = self._principal_cache = {}
        entry = cache.get(session_id)
        now = time.monotonic()
        if entry is not None and entry[1] > now:
            profile_id, policy, reason = entry[0]
        else:
            # A store that cannot answer is "we could not find out", not
            # "the caller may do anything" — and it must not be an
            # exception either.  ``execute`` is the single dispatch seam
            # for Discord, MCP, the CLI and both HTTP surfaces, so raising
            # here would turn an unresolvable identity into a 500 on every
            # session-scoped command rather than a clean denial.  Reached
            # whenever the handler is wired without a live database: early
            # startup before ``Database.initialize``, and any caller that
            # builds a CommandHandler on a stub orchestrator.
            try:
                session = await self.db.get_session(session_id)
            except AttributeError:
                return _closed("database-unavailable")
            except Exception:
                logger.exception("principal lookup failed for session %s", session_id)
                return _closed("database-unavailable")
            if session is None:
                return _closed("session-not-found")
            profile_id = getattr(session, "profile_id", None)
            if not profile_id:
                profile_id, policy, reason = None, DENY_ALL, "session-has-no-profile"
            else:
                try:
                    profile = await self.db.get_profile(profile_id)
                except Exception:
                    logger.exception("profile lookup failed for %s", profile_id)
                    return _closed("database-unavailable")
                if profile is None:
                    policy, reason = DENY_ALL, "profile-not-found"
                else:
                    policy = capability_policy_for(
                        profile, plugin_command_names=self._plugin_command_names()
                    )
                    reason = None
            cache[session_id] = ((profile_id, policy, reason), now + self._PRINCIPAL_CACHE_TTL)

        if reason is not None:
            logger.warning(
                "principal_fail_closed reason=%s session_id=%s", reason, session_id
            )
            return ExecutionPrincipal(
                policy=DENY_ALL, profile_id=profile_id, provenance=(reason,), **common
            )
        return ExecutionPrincipal(policy=policy, profile_id=profile_id, **common)

    def _plugin_command_names(self) -> frozenset[str]:
        """Names the plugin registry dispatches, for capability classification."""
        registry = getattr(self.orchestrator, "plugin_registry", None)
        names = getattr(registry, "_commands", None) if registry is not None else None
        if isinstance(names, dict):
            return frozenset(names)
        return frozenset()

    async def _record_capability_denial(self, name: str, principal: Any, decision: Any) -> None:
        """Leave a durable ``capability.denied`` row (Package 7 §3.5 measure 4).

        The rollback-window gate counts denials over a 72 h window, which an
        in-memory counter would lose at the first restart; the events table
        is the one place that survives.  Grouping key only — command, profile,
        namespace, fingerprint — and never the arguments, which may carry a
        secret.  Best-effort: a denial that fails to record must still deny.
        """
        db = getattr(self, "db", None)
        log_event = getattr(db, "log_event", None)
        if log_event is None:
            return
        try:
            policy = getattr(principal, "policy", None)
            fingerprint = policy.fingerprint() if policy is not None else None
            await log_event(
                "capability.denied",
                project_id=getattr(principal, "project_id", None),
                task_id=getattr(principal, "task_id", None),
                agent_id=getattr(principal, "session_id", None),
                payload=json.dumps(
                    {
                        "command": name,
                        "principal_kind": getattr(
                            getattr(principal, "kind", None), "value", None
                        ),
                        "profile_id": getattr(principal, "profile_id", None),
                        "namespace": getattr(decision, "namespace", None),
                        "shadow": bool(getattr(decision, "shadow", False)),
                        "fingerprint": fingerprint,
                    },
                    sort_keys=True,
                ),
            )
        except Exception:
            logger.debug("Could not record capability.denied for %s", name, exc_info=True)

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
            if isinstance(args, dict) and any(k in args for k in SERVER_OWNED_ARG_KEYS):
                args = dict(args)
                scope = args.pop("_scope", None)
                # Belt-and-braces defense, mirroring the ``_scope`` comment
                # above: /api/execute and the generated typed routes already
                # strip every server-owned key before forwarding the trusted
                # ones, so this is the second of two independent layers.  A
                # ``_policy`` claiming every command is inert — the principal
                # below is built from the session row regardless.
                for key in SERVER_OWNED_ARG_KEYS[1:]:
                    args.pop(key, None)
            # Save/restore rather than set/clear: a command can dispatch
            # another one inside its own body (``task_close --claim-next``
            # calls ``_cmd_task_claim``; the playbook runner and supervisor
            # re-enter ``execute`` outright), and an unconditional clear in
            # the ``finally`` would strip the outer command's identity the
            # moment the inner one returned.
            _scope_token = _current_scope_var.set(scope)
            # The principal follows the same save/restore discipline, and for
            # the same reason: ``execute`` is re-entrant.  An already-bound
            # principal (the playbook runner, an orchestrator service call)
            # wins — only a request that carries none derives one.
            principal = current_principal()
            if principal is None:
                principal = await self._principal_from_scope(scope)
            _principal_token = _principal_var.set(principal)
            mutating = self._is_mutating(name)
            # Terminal keystrokes may be secrets. They belong only to the
            # terminal, never to general command logs or the activity feed.
            log_args = (
                {"session_id": args.get("session_id"), "input": "<redacted>"}
                if name == "session_input" else args
            )
            if mutating:
                logger.info("cmd %s args=%s", name, self._preview(log_args))
            else:
                logger.debug("cmd %s args=%s", name, self._preview(log_args))
            # Snapshot for command.invoked emission — args may be mutated by
            # ``_resolve_project_id_in_args`` and by handler bodies (e.g. an
            # embedded body/plan being popped for storage), and the raw values
            # never appear on the bus regardless.
            _emit_started_at = time.monotonic()
            _emit_args_snapshot = dict(args) if isinstance(args, dict) else {}
            _emit_ok: bool = False
            _emit_error: str | None = None
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
                    result = {"success": False, "error": paused_error}
                    _emit_ok = False
                    _emit_error = paused_error
                    return result

                # Capability gate (Playbook V2 Package 0 §3.6).  Placed
                # BEFORE the built-in lookup so the plugin fallback below is
                # covered by the same check with no second call site — the
                # plugin path previously ran with no capability check at all.
                # It composes with, and does not replace, check_request_scope:
                # a command must pass both.
                decision = authorize_command(
                    name,
                    principal,
                    resolver=self._command_resolver,
                    mode=getattr(self.config.security, "capability_enforcement", "audit"),
                )
                if decision.shadow:
                    logger.warning(
                        "capability_denied_shadow cmd=%s principal=%s profile=%s ns=%s "
                        "fingerprint=%s derived_from_legacy=True",
                        name,
                        principal.describe(),
                        principal.profile_id,
                        decision.namespace,
                        principal.policy.fingerprint(),
                    )
                elif not decision.allowed:
                    logger.warning(
                        "capability_denied cmd=%s principal=%s session=%s profile=%s "
                        "ns=%s fingerprint=%s",
                        name,
                        principal.describe(),
                        principal.session_id,
                        principal.profile_id,
                        decision.namespace,
                        principal.policy.fingerprint(),
                    )
                    result = denial_result(name)
                    _emit_ok = False
                    _emit_error = result["error"]
                    await self._record_capability_denial(name, principal, decision)
                    return result

                handler = getattr(self, f"_cmd_{name}", None)
                if handler:
                    result = await handler(args)
                    if mutating:
                        logger.info("cmd %s result=%s", name, self._preview(result))
                    _emit_ok, _emit_error = _classify_result(result)
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
                            _emit_ok, _emit_error = _classify_result(result)
                            return result
                        except Exception as e:
                            await self.orchestrator.plugin_registry.record_failure(
                                plugin_name, str(e)
                            )
                            logger.error(
                                "Plugin command %s failed: args=%s err=%s",
                                name,
                                self._preview(log_args),
                                e,
                                exc_info=True,
                            )
                            _emit_ok = False
                            _emit_error = f"Plugin command failed: {e.__class__.__name__}"
                            return {"error": f"Plugin command failed: {e}"}

                logger.warning("Unknown command requested: %s args=%s", name, self._preview(log_args))
                _emit_ok = False
                _emit_error = f"Unknown command: {name}"
                return {"error": f"Unknown command: {name}"}
            except Exception as e:
                logger.error(
                    "Command %s failed: args=%s err=%s",
                    name,
                    self._preview(log_args),
                    e,
                    exc_info=True,
                )
                _emit_ok = False
                _emit_error = f"{e.__class__.__name__}: {e}"[:200]
                return {"error": str(e)}
            finally:
                # Ensure scope does not leak across commands.
                _current_scope_var.reset(_scope_token)
                _principal_var.reset(_principal_token)
                # Emit ``command.invoked`` for dashboard live-activity chips
                # and future observability surfaces. Gated on the config flag;
                # any failure is swallowed so a broken bus never breaks
                # command execution (spec constraint).
                try:
                    if name != "session_input" and getattr(self.config, "events", None) and (
                        self.config.events.command_invoked_enabled
                    ):
                        bus = getattr(self.orchestrator, "bus", None)
                        if bus is not None:
                            duration_ms = int(
                                (time.monotonic() - _emit_started_at) * 1000
                            )
                            payload = {
                                "command": name,
                                "ok": _emit_ok,
                                "duration_ms": duration_ms,
                                "session_id": (scope or {}).get("session_id")
                                if isinstance(scope, dict)
                                else None,
                                "task_id": (scope or {}).get("task_id")
                                if isinstance(scope, dict)
                                else None,
                                "project_id": (scope or {}).get("project_id")
                                if isinstance(scope, dict)
                                else None,
                                "args_summary": _summarize_args(name, _emit_args_snapshot),
                                "error": _emit_error,
                            }
                            await bus.emit("command.invoked", payload)
                except Exception:  # pragma: no cover -- defensive
                    logger.debug(
                        "command.invoked emit failed for %s", name, exc_info=True
                    )
