"""Task commands mixin — CRUD, lifecycle, dependencies, plans."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import PurePosixPath, PureWindowsPath

from src.models import (
    BLOCKING_DEP_TYPES,
    INTEGRATION_MODES,
    DepType,
    Task,
    TaskStatus,
    TaskType,
    VerificationType,
    WorkspaceMode,
    DEP_TYPE_VALUES,
    TASK_TYPE_VALUES,
    WORKSPACE_MODE_VALUES,
)
from src.discord.embeds import STATUS_EMOJIS, progress_bar
from src.discord.notifications import classify_error
from src.state_machine import (
    CyclicDependencyError,
    validate_dag_with_new_edge,
    validate_waits_for,
)
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.queries.task_queries import TERMINAL_BLOCKED_META_KEY
from src.task_names import (
    MAX_NAMING_DEPTH,
    MAX_STRUCTURAL_DEPTH,
    child_task_id,
    fresh_root_id,
    generate_task_id,
    naming_depth,
)

from src.commands.helpers import (
    _collect_tree_task_ids,
    _collect_tree_tasks,
    _count_subtree,
    _count_subtree_by_status,
    _format_task_tree,
    format_dependency_list,
)
from src.review_keys import REVIEW_PROFILE_IDS, is_review_completion, reviewed_task_id
from src.task_summary import write_task_summary

logger = logging.getLogger(__name__)

#: Capacity reason codes that describe the **push** scheduler's supply and so
#: cannot explain a ``lifecycle: pool`` task, whose work is claimed rather
#: than pushed (swarm-work-model §11).  Everything else
#: ``build_capacity_reasons`` produces still applies to the pull path: a
#: paused project or an exhausted budget fails ``_admission_reason`` on the
#: claim, and no free workspace starves ``_launch_pool_session``.
_PUSH_ONLY_REASON_CODES = frozenset({"no_idle_agent", "no_compatible_agent", "rate_limited"})


def _fmt_epoch(ts: float) -> str:
    """Epoch seconds → local ``YYYY-MM-DD HH:MM:SS`` for human reason text."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))


def _normalize_label_list(raw) -> list[str]:
    """Coerce a label argument into a clean list of strings.

    Accepts a list, or a comma-separated string (what Discord/CLI callers
    hand over).  Blank entries are dropped and order is preserved.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.split(",")
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        label = str(item).strip()
        if label and label not in seen:
            seen.add(label)
            out.append(label)
    return out


_DELIVERABLE_KINDS = frozenset({"file", "test", "command", "flag", "registration"})


def _path_target_error(target: str) -> str | None:
    """Explain why ``target`` is not one repo-relative file path, or ``None``."""
    if any(ch.isspace() for ch in target):
        return "contains whitespace (a command or several paths)"
    if "::" in target:
        return "contains a '::' node id"
    if PurePosixPath(target).is_absolute() or PureWindowsPath(target).is_absolute():
        return "is an absolute path"
    if ".." in PurePosixPath(target).parts:
        return "escapes the repository with '..'"
    return None


def normalize_deliverables(raw) -> tuple[list[dict[str, str]], str | None]:
    """Validate a plan-derived implementation contract.

    IDs make explicit close waivers unambiguous, while the deliberately small
    ``id``/``kind``/``target`` shape works for direct, batch, and graph task
    creation without coupling those surfaces to each other.

    A ``file`` target must be one repo-relative file path. A ``test`` target
    may instead be a command line (which close-time evidence matches against
    ``--test``), but a path-shaped test target must also be repo-relative.
    """
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return [], "deliverables must be a list"
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            return [], f"deliverables[{index}] must be an object"
        item_id = str(entry.get("id") or "").strip()
        kind = str(entry.get("kind") or "").strip().lower()
        target = str(entry.get("target") or "").strip()
        if not item_id:
            return [], f"deliverables[{index}].id is required"
        if item_id in seen:
            return [], f"duplicate deliverable id '{item_id}'"
        if kind not in _DELIVERABLE_KINDS:
            return [], (
                f"deliverables[{index}].kind must be one of {sorted(_DELIVERABLE_KINDS)}"
            )
        if not target:
            return [], f"deliverables[{index}].target is required"
        path_only = kind == "file" or (kind == "test" and not any(ch.isspace() for ch in target))
        if path_only and (why := _path_target_error(target)):
            shape = (
                "one repo-relative file path"
                if kind == "file"
                else "one repo-relative file path or a test command line"
            )
            return [], (
                f"deliverables[{index}].target {why}; a [{kind}] target must be {shape} "
                "such as 'tests/test_x.py'"
            )
        seen.add(item_id)
        result.append({"id": item_id, "kind": kind, "target": target})
    return result, None


def _check_capability_escalation(parent, child) -> str:
    """Return an explanation if ``child`` exceeds ``parent``'s capabilities.

    Returns the empty string when the child profile is a subset (i.e.
    acceptable).  Used by ``_cmd_create_task`` to reject upward escalation
    when a sandboxed caller delegates work to another profile.

    Playbook V2 Package 0 §3.9 replaced the flat ``allowed_tools`` comparison
    with a :class:`~src.profiles.capabilities.CapabilityPolicy` subset check
    across all three namespaces, so an escalation in *any* of harness tools,
    AQ commands, or plugin tools is caught and named.  ``mcp_servers`` is
    still compared separately: server *names* are not capabilities in the
    policy model, and dropping the check would have widened delegation.

    System-prompt subsetting is intentionally not enforced — there is no
    mechanical notion of "subset of prose".  The parent profile's author is
    responsible for what they delegate; the runtime ensures the delegate
    cannot reach beyond the parent's capability bounds.
    """
    from src.commands.principal import check_delegation
    from src.profiles.capabilities import capability_policy_for

    escalation = check_delegation(capability_policy_for(parent), capability_policy_for(child))
    if escalation:
        return escalation
    parent_servers = set(parent.mcp_servers or [])
    child_servers = set(child.mcp_servers or [])
    extra_servers = sorted(child_servers - parent_servers)
    if extra_servers:
        return (
            f"child references {len(extra_servers)} MCP server(s) not in "
            f"parent's list: {extra_servers}"
        )
    return ""


#: Scope refusals for worker filing (swarm work model §12).  The same text is
#: produced by the pre-check in ``_cmd_create_task`` and by the re-check
#: inside ``_create_worker_filed_task``'s transaction, so a filing that loses
#: a race to a concurrent reparent reads exactly like one that never had the
#: scope to begin with.
_DISCOVERED_FROM_SCOPE_ERROR = "discovered_from must be the held task or one of its descendants"
_REPARENT_SCOPE_ERROR = (
    "a worker may only reparent an unclaimed task it filed from the task it holds "
    "(created by a session, under the held task or discovered-from it)"
)
_PARENT_SCOPE_ERROR = (
    "parent must be the held task, one of its descendants, or the held task's own parent"
)


class _FilingScope(Exception):
    """Internal signal: the filing left the held task's authorised scope.

    Raised by the in-transaction re-check in
    :meth:`TaskCommandsMixin._create_worker_filed_task` when a concurrent
    reparent moved the held task — or the node the worker named — out from
    under the scope the pre-check saw.  The ``immediate()`` block rolls back
    nothing-written and ``_cmd_create_task`` turns it into the same refusal
    the pre-check would have returned.
    """

    def __init__(self, error: str):
        super().__init__(error)
        self.error = error


class _FilingQuota(Exception):
    """Internal signal: ``reserve_filing`` refused — held task hit its quota.

    Raised inside :meth:`TaskCommandsMixin._create_worker_filed_task`'s
    ``immediate()`` block so the transaction rolls back nothing-written;
    caught in ``_cmd_create_task`` to turn into the ``filing_quota_exceeded``
    response.
    """


class TaskCommandsMixin:
    """Task command methods mixed into CommandHandler."""

    # -----------------------------------------------------------------------
    # Task commands -- CRUD plus lifecycle operations.
    # Tasks are the unit of work assigned to agents.  Beyond basic CRUD this
    # group includes stop (cancel a running task), restart (re-queue a
    # failed/completed task), skip (mark as completed without running),
    # and chain-health diagnostics for dependency graphs.
    # -----------------------------------------------------------------------

    # Statuses considered "finished" for the include_completed / completed_only
    # filters.  Only COMPLETED is treated as finished — FAILED and BLOCKED
    # tasks still need attention (retry/fix or dependency resolution) and
    # should be visible in the default task list so the progress breakdown
    # numbers add up correctly.
    _FINISHED_STATUSES: frozenset[TaskStatus] = frozenset(
        {
            TaskStatus.COMPLETED,
        }
    )

    async def _resolve_root_task_id(self, task_id: str) -> str:
        """Walk up the parent chain to find the topmost ancestor task ID.

        Used by tree/compact display modes to determine the root task that
        should be rendered as the tree head for a given subtask.  Includes a
        cycle guard to protect against malformed parent chains.
        """
        current_id = task_id
        seen: set[str] = set()
        while True:
            if current_id in seen:
                break  # cycle guard
            seen.add(current_id)
            task = await self.db.get_task(current_id)
            if task is None or task.parent_task_id is None:
                return current_id
            current_id = task.parent_task_id
        return current_id

    async def _build_dep_map_for_tree(
        self,
        tree_data: dict,
        base_map: dict[str, dict] | None = None,
    ) -> dict[str, dict]:
        """Build a dependency map covering every task in *tree_data*.

        Starts from *base_map* (which typically comes from the pre-fetched
        ``task_list`` entries) and fills in any tree nodes that are missing,
        so that ``_tree_dep_annotation()`` can annotate every node.

        Parameters
        ----------
        tree_data:
            A tree hierarchy dict from ``Database.get_task_tree()``.
        base_map:
            Pre-existing dependency data keyed by task ID.  Entries already
            present are reused without additional DB queries.

        Returns
        -------
        dict[str, dict]
            Mapping of ``task_id`` → ``{"depends_on": [...], "blocks": [...]}``.
        """
        result = dict(base_map) if base_map else {}
        # Find tree task IDs not already in the base map
        missing_ids = [tid for tid in _collect_tree_task_ids(tree_data) if tid not in result]
        if missing_ids:
            # Batch-fetch all missing dependency data in two queries
            batch_result = await self.db.get_dependency_map_for_tasks(missing_ids)
            result.update(batch_result)
        return result

    @staticmethod
    def format_task_with_dependencies(task: dict) -> str:
        """Format a single task dict with optional dependency annotation lines.

        Produces output like::

            🔵 #12: Set up database [READY]
               ↳ depends on: #10 (COMPLETED ✅), #11 (IN_PROGRESS 🟡)

        or::

            🟡 #14: Build API endpoints [IN_PROGRESS]
               ↳ blocks: #15, #16, #17

        Tasks with no dependencies or dependents get a single line with no
        annotation.  The status emoji for the main task is looked up from
        ``STATUS_EMOJIS``; dependency references also include their status
        emoji for quick visual scanning.

        Parameters
        ----------
        task : dict
            A task dict as returned by ``_cmd_list_tasks`` when
            ``show_dependencies=True``.  Must contain at least ``id``,
            ``title``, and ``status`` keys.  May contain ``depends_on`` and
            ``blocks`` lists (each entry: ``{id, title, status}``).

        Returns
        -------
        str
            One or more lines of formatted text.
        """
        status = task.get("status", "DEFINED")
        emoji = STATUS_EMOJIS.get(status, "⚪")
        line = f"{emoji} #{task['id']}: {task['title']} [{status}]"
        lines = [line]

        # depends_on annotation
        depends_on = task.get("depends_on", [])
        if depends_on:
            parts = []
            for dep in depends_on:
                dep_emoji = STATUS_EMOJIS.get(dep["status"], "⚪")
                parts.append(f"#{dep['id']} ({dep['status']} {dep_emoji})")
            lines.append(f"   ↳ depends on: {', '.join(parts)}")

        # blocks annotation
        blocks = task.get("blocks", [])
        if blocks:
            parts = [f"#{b['id']}" for b in blocks]
            lines.append(f"   ↳ blocks: {', '.join(parts)}")

        return "\n".join(lines)

    @staticmethod
    def format_task_list_with_dependencies(tasks: list[dict]) -> str:
        """Format a full task list with dependency annotations.

        Convenience wrapper around :meth:`format_task_with_dependencies` that
        joins all task blocks with a newline separator.

        Parameters
        ----------
        tasks : list[dict]
            Task dicts as returned by ``_cmd_list_tasks`` (the ``"tasks"``
            value) when ``show_dependencies=True``.

        Returns
        -------
        str
            Multi-line formatted text ready for display.
        """
        return "\n".join(TaskCommandsMixin.format_task_with_dependencies(t) for t in tasks)

    async def _cmd_list_tasks(self, args: dict) -> dict:
        """List tasks with configurable display mode.

        Supports three ``display_mode`` values:

        ``"flat"`` (default)
            The original flat list of task dicts — every task is an
            independent row.  This is backward-compatible with all
            existing callers.

        ``"tree"``
            Group tasks by parent and render each root task's hierarchy
            using :func:`_format_task_tree` (expanded, with box-drawing
            characters).  The response includes both the pre-formatted
            text and structured data so callers can choose how to present
            it.

        ``"compact"``
            Show only root (parent) tasks with a subtask count and
            progress bar.  Uses :func:`_format_task_tree` in compact
            mode.  Ideal for dense overview lists.

        For ``"tree"`` and ``"compact"`` modes, a ``project_id`` is
        required so we can query parent tasks.  If ``project_id`` is
        missing the method silently falls back to ``"flat"``.

        When ``show_dependencies`` is ``True``, each task dict is enriched
        with ``depends_on`` (list of upstream task IDs + statuses) and
        ``blocks`` (list of downstream dependent task IDs + statuses).

        Parameters
        ----------
        args : dict
            ``project_id`` – filter by project (optional).
            ``status`` – filter by a specific TaskStatus value (optional).
            ``display_mode`` – ``"flat"``, ``"tree"``, or ``"compact"`` (default ``"flat"``).
            ``include_completed`` – if True, include terminal tasks (default False).
            ``completed_only`` – if True, show only terminal tasks (default False).
            ``show_dependencies`` – if True, enrich each task dict with
            ``depends_on`` and ``blocks`` lists and include a pre-formatted
            ``formatted`` key with the dependency-aware text representation.
        """
        # Normalize show_all → include_completed so all downstream helpers
        # only need to check include_completed.
        if args.get("show_all") and not args.get("include_completed"):
            args = {**args, "include_completed": True}

        display_mode: str = args.get("display_mode", "flat")
        show_dependencies: bool = args.get("show_dependencies", False)

        kwargs = {}
        if "project_id" in args:
            kwargs["project_id"] = args["project_id"]

        # An explicit `status` filter takes precedence over the convenience
        # boolean flags — the caller is asking for a specific status.
        explicit_status = "status" in args
        if explicit_status:
            kwargs["status"] = TaskStatus(args["status"])

        # Label filters (work-graph design §6): ``labels`` is all-of,
        # ``any_label`` is any-of.  Listing never hides ``hold:*`` tasks —
        # only the ready frontier does that.
        labels = _normalize_label_list(args.get("labels"))
        any_label = _normalize_label_list(args.get("any_label"))
        if labels:
            kwargs["labels"] = labels
        if any_label:
            kwargs["any_label"] = any_label

        # ── Flat mode (default / backward-compatible) ──────────────────
        # Also used as the fallback when tree/compact lack a project_id.
        if display_mode == "flat" or "project_id" not in args:
            return await self._list_tasks_flat(
                args,
                kwargs,
                explicit_status,
                show_dependencies=show_dependencies,
            )

        # ── Tree / Compact modes ───────────────────────────────────────
        return await self._list_tasks_hierarchical(
            args,
            kwargs,
            explicit_status,
            compact=(display_mode == "compact"),
            show_dependencies=show_dependencies,
        )

    # -- private helpers for _cmd_list_tasks display modes -------------------

    async def _list_tasks_flat(
        self,
        args: dict,
        db_kwargs: dict,
        explicit_status: bool,
        *,
        show_dependencies: bool = False,
    ) -> dict:
        """Flat list mode — the original ``_cmd_list_tasks`` behaviour."""
        tasks = await self.db.list_tasks(**db_kwargs)

        # Apply include_completed / completed_only filtering only when no
        # explicit status filter was provided.
        include_completed: bool = args.get("include_completed", False)
        hidden_count = 0
        if not explicit_status:
            completed_only: bool = args.get("completed_only", False)
            all_count = len(tasks)

            if completed_only:
                # Show only finished tasks.
                tasks = [t for t in tasks if t.status in self._FINISHED_STATUSES]
                hidden_count = all_count - len(tasks)
            elif not include_completed:
                # Default: hide finished tasks so the list shows active work.
                tasks = [t for t in tasks if t.status not in self._FINISHED_STATUSES]
                hidden_count = all_count - len(tasks)
            # else: include_completed=True — return everything unfiltered.

        # Always show all active tasks; only cap completed/finished ones so
        # that active work (DEFINED, READY, IN_PROGRESS, …) is never hidden.
        active = [t for t in tasks if t.status not in self._FINISHED_STATUSES]
        finished = [t for t in tasks if t.status in self._FINISHED_STATUSES]
        cap = max(0, 200 - len(active))
        capped_tasks = active + finished[:cap]
        task_dicts = [self._task_to_dict(t) for t in capped_tasks]

        if show_dependencies:
            await self._enrich_with_dependencies(task_dicts, capped_tasks)

        result: dict = {
            "display_mode": "flat",
            "tasks": task_dicts,
            "total": len(tasks),
            "hidden_completed": hidden_count,
            "filtered": not include_completed and "status" not in args,
        }
        if show_dependencies:
            result["dependency_display"] = format_dependency_list(task_dicts)
        return result

    async def _cmd_list_active_tasks_all_projects(self, args: dict) -> dict:
        """List active (non-terminal) tasks across ALL projects, grouped by project.

        This gives a cross-project overview of everything that is currently
        queued, in-progress, or otherwise actionable.  Only COMPLETED tasks
        are excluded by default; FAILED and BLOCKED tasks are shown since
        they still need attention.  Use ``include_completed=True`` to also
        include completed tasks.

        Uses ``Database.list_active_tasks()`` for SQL-level filtering when
        showing only active tasks, avoiding the need to fetch and discard
        potentially large numbers of completed tasks.
        """
        include_completed = args.get("include_completed", False)

        if include_completed:
            # Caller wants everything -- no status filtering.
            tasks = await self.db.list_tasks()
        else:
            # SQL-level filtering excludes terminal statuses.
            tasks = await self.db.list_active_tasks()

        # Compute how many terminal tasks were hidden (for UI hints).
        hidden_completed = 0
        if not include_completed:
            status_counts = await self.db.count_tasks_by_status()
            _terminal_values = {"COMPLETED"}
            hidden_completed = sum(
                cnt for st, cnt in status_counts.items() if st in _terminal_values
            )

        # Build a task-entry dict (reused for both grouped and flat views).
        def _entry(t: Task, *, include_project: bool = False) -> dict:
            d: dict = {
                "id": t.id,
                "title": t.title,
                "status": t.status.value,
                "priority": t.priority,
                "assigned_agent": t.assigned_agent_id,
                "profile_id": t.profile_id,
                "intelligence_class": t.intelligence_class,
                "parent_task_id": t.parent_task_id,
                "is_plan_subtask": t.is_plan_subtask,
                "task_type": t.task_type.value if t.task_type else None,
                "pr_url": t.pr_url,
                "integration_mode": t.integration_mode,
                "created_at": t.created_at,
                "updated_at": t.updated_at,
            }
            if include_project:
                d["project_id"] = t.project_id
            return d

        # Group by project_id for readability.
        by_project: dict[str, list[dict]] = {}
        for t in tasks:
            by_project.setdefault(t.project_id, []).append(_entry(t))

        # Build a flat list — always include all active tasks; only cap
        # completed/finished so active work is never hidden.
        _terminal = {TaskStatus.COMPLETED}
        active_tasks = [t for t in tasks if t.status not in _terminal]
        finished_tasks = [t for t in tasks if t.status in _terminal]
        cap = max(0, 200 - len(active_tasks))
        flat = [_entry(t, include_project=True) for t in active_tasks + finished_tasks[:cap]]

        return {
            "by_project": by_project,
            "tasks": flat,
            "total": len(tasks),
            "project_count": len(by_project),
            "hidden_completed": hidden_completed,
        }

    async def _list_tasks_hierarchical(
        self,
        args: dict,
        db_kwargs: dict,
        explicit_status: bool,
        *,
        compact: bool,
        show_dependencies: bool = False,
    ) -> dict:
        """Tree or compact list mode — groups tasks by parent hierarchy.

        Fetches root (parentless) tasks for the project, then builds the
        full subtask tree for each root task using
        ``Database.get_task_tree()``.  The caller receives both
        pre-formatted text (ready for Discord) and structured data.

        Label filters apply to the **roots**: a tree is included when its root
        carries the label, and it is then rendered whole.  Filtering the
        subtree instead would return shredded trees whose parents are missing,
        which is not what a hierarchical view is for.  Callers that want every
        matching task regardless of position use flat mode.  Silently dropping
        the filter (the previous behaviour) is the one option that is simply
        wrong — ``--labels x --display-mode tree`` returned everything.
        """
        project_id: str = db_kwargs["project_id"]
        mode_name = "compact" if compact else "tree"

        # 1. Get all root-level tasks for the project.
        root_tasks = await self.db.get_parent_tasks(
            project_id,
            labels=db_kwargs.get("labels"),
            any_label=db_kwargs.get("any_label"),
        )

        # 2. Apply status filtering to root tasks.
        if explicit_status:
            status_filter = TaskStatus(args["status"])
            root_tasks = [t for t in root_tasks if t.status == status_filter]
        else:
            root_tasks = self._apply_completion_filter(root_tasks, args)

        # 3. Build tree for each root and format.
        #    When show_dependencies is active we need two passes:
        #      a) collect all trees so we know every task in every subtree,
        #      b) build a dep_map for the full set, then re-format with
        #         annotations.  The first pass still stores a provisional
        #         ``formatted`` string (without annotations) so that if
        #         dep_map turns out empty the output is unchanged.
        trees: list[dict] = []
        included_roots: list[Task] = []  # Track Task objects for dependency enrichment
        # raw_trees stores (root, children) pairs for a second formatting pass
        raw_trees: list[tuple[Task, list[dict]]] = []
        total_tasks = 0

        # Always show all active root tasks; only cap completed ones.
        active_roots = [r for r in root_tasks if r.status not in self._FINISHED_STATUSES]
        finished_roots = [r for r in root_tasks if r.status in self._FINISHED_STATUSES]
        cap = max(0, 200 - len(active_roots))
        capped_roots = active_roots + finished_roots[:cap]

        for root in capped_roots:
            tree_data = await self.db.get_task_tree(root.id)
            if tree_data is None:
                # Shouldn't happen — root was just fetched — but be safe.
                continue

            children = tree_data.get("children", [])
            completed, subtask_total = _count_subtree(children)
            status_counts = _count_subtree_by_status(children)

            formatted = _format_task_tree(
                root,
                children,
                compact=compact,
            )

            tree_entry: dict = {
                "root": self._task_to_dict(root),
                "formatted": formatted,
                "subtask_completed": completed,
                "subtask_total": subtask_total,
                "subtask_by_status": status_counts,
            }

            # In compact mode, also include a text progress bar for
            # callers that want to display it inline.
            if compact and subtask_total > 0:
                tree_entry["progress_bar"] = progress_bar(
                    completed,
                    subtask_total,
                )

            trees.append(tree_entry)
            included_roots.append(root)
            raw_trees.append((root, children))
            # Count root + all its subtasks
            total_tasks += 1 + subtask_total

        # Enrich root task dicts with dependency info when requested.
        if show_dependencies:
            root_dicts = [entry["root"] for entry in trees]
            await self._enrich_with_dependencies(root_dicts, included_roots)

            # Build dep_map across ALL tasks in all trees and re-format
            # expanded trees with inline annotations.  Compact mode is
            # skipped — annotations are too dense for the summary format.
            if not compact:
                all_tasks: list[Task] = []
                for root, children in raw_trees:
                    all_tasks.append(root)
                    all_tasks.extend(_collect_tree_tasks(children))

                dep_map = await self._build_dep_map(all_tasks)
                if dep_map:
                    for i, (root, children) in enumerate(raw_trees):
                        trees[i]["formatted"] = _format_task_tree(
                            root,
                            children,
                            compact=False,
                            dep_map=dep_map,
                        )

        result: dict = {
            "display_mode": mode_name,
            "trees": trees,
            "total_root_tasks": len(trees),
            "total_tasks": total_tasks,
        }
        if db_kwargs.get("labels") or db_kwargs.get("any_label"):
            # Tell the caller the filter matched roots, not every node.
            result["label_filter_scope"] = "root"
        return result

    # -- shared helpers ------------------------------------------------------

    async def _emit_task_graph_change(
        self, event_type: str, task: Task, *, project_id: str | None = None
    ) -> None:
        """Publish a committed graph edit; subscriber failures cannot undo it.

        Persist the small identity-only audit row for reconnect replay. The bus
        uses the usual task base fields (never description or command arguments).
        This intentionally does not emit task.created, which triggers routing.
        """
        pid = project_id or task.project_id
        seq = None
        try:
            seq = await self.db.log_event(event_type, project_id=pid, task_id=task.id)
        except Exception:
            logger.warning(
                "Could not persist graph change %s for %s", event_type, task.id, exc_info=True
            )
        try:
            await self.orchestrator._emit_task_event(event_type, task, project_id=pid, seq=seq)
        except Exception:
            logger.warning(
                "Could not publish graph change %s for %s", event_type, task.id, exc_info=True
            )

    def _apply_completion_filter(
        self,
        tasks: list[Task],
        args: dict,
    ) -> list[Task]:
        """Filter a task list by the ``include_completed`` / ``completed_only``
        convenience flags.  Used by both flat and hierarchical modes.
        """
        include_completed: bool = args.get("include_completed", False)
        completed_only: bool = args.get("completed_only", False)

        if completed_only:
            return [t for t in tasks if t.status in self._FINISHED_STATUSES]
        if not include_completed:
            return [t for t in tasks if t.status not in self._FINISHED_STATUSES]
        # include_completed=True — return everything unfiltered.
        return tasks

    async def _enrich_with_dependencies(
        self,
        task_dicts: list[dict],
        tasks: list[Task],
    ) -> None:
        """Add ``depends_on`` and ``blocks`` keys to each task dict in-place.

        ``depends_on`` contains a list of upstream dependency dicts, each with
        ``id``, ``title``, and ``status``.  ``blocks`` contains a list of
        downstream dependent task IDs with the same shape.

        Uses the existing ``get_dependencies()`` and ``get_dependents()`` DB
        helpers.  Lookups are batched per-task but results are cached within
        the call to avoid redundant ``get_task()`` queries when the same
        dependency appears across multiple tasks.
        """
        # Local cache so repeated dependency IDs don't trigger extra DB reads.
        task_cache: dict[str, Task | None] = {}

        async def _resolve(task_id: str) -> dict | None:
            if task_id not in task_cache:
                task_cache[task_id] = await self.db.get_task(task_id)
            t = task_cache[task_id]
            if t is None:
                return None
            return {"id": t.id, "title": t.title, "status": t.status.value}

        for td, task in zip(task_dicts, tasks):
            # Upstream: tasks this task depends on
            dep_ids = await self.db.get_dependencies(task.id)
            if dep_ids:
                dep_details = []
                for dep_id in dep_ids:
                    resolved = await _resolve(dep_id)
                    if resolved:
                        dep_details.append(resolved)
                td["depends_on"] = dep_details
            else:
                td["depends_on"] = []

            # Downstream: tasks that depend on this task
            dependent_ids = await self.db.get_dependents(task.id)
            if dependent_ids:
                block_details = []
                for dep_id in dependent_ids:
                    resolved = await _resolve(dep_id)
                    if resolved:
                        block_details.append(resolved)
                td["blocks"] = block_details
            else:
                td["blocks"] = []

    async def _build_dep_map(
        self,
        tasks: list[Task],
    ) -> dict[str, dict]:
        """Build a dependency map for annotating tree nodes.

        Returns a dict mapping ``task_id`` → ``{"depends_on": [...], "blocks": [...]}``
        where each list element is ``{"id": str, "title": str, "status": str}``.

        Only tasks that have at least one dependency or dependent are included
        in the returned map — callers can treat a missing key as "no
        dependencies".

        This is similar to :meth:`_enrich_with_dependencies` but returns a
        standalone mapping suitable for passing to :func:`_format_task_tree`
        instead of mutating task dicts in-place.
        """
        # Local cache so repeated dependency IDs don't trigger extra DB reads.
        task_cache: dict[str, Task | None] = {}

        async def _resolve(task_id: str) -> dict | None:
            if task_id not in task_cache:
                task_cache[task_id] = await self.db.get_task(task_id)
            t = task_cache[task_id]
            if t is None:
                return None
            return {"id": t.id, "title": t.title, "status": t.status.value}

        dep_map: dict[str, dict] = {}

        for task in tasks:
            # Upstream: tasks this task depends on
            dep_ids = await self.db.get_dependencies(task.id)
            depends_on: list[dict] = []
            for dep_id in dep_ids:
                resolved = await _resolve(dep_id)
                if resolved:
                    depends_on.append(resolved)

            # Downstream: tasks that depend on this task
            dependent_ids = await self.db.get_dependents(task.id)
            blocks: list[dict] = []
            for dep_id in dependent_ids:
                resolved = await _resolve(dep_id)
                if resolved:
                    blocks.append(resolved)

            if depends_on or blocks:
                dep_map[task.id] = {
                    "depends_on": depends_on,
                    "blocks": blocks,
                }

        return dep_map

    @staticmethod
    def _task_to_dict(t: Task) -> dict:
        """Serialize a :class:`Task` to the standard dict used in list
        responses.  Centralises the field selection so flat and
        hierarchical modes stay consistent.
        """
        return {
            "id": t.id,
            "project_id": t.project_id,
            "title": t.title,
            "status": t.status.value,
            "priority": t.priority,
            "assigned_agent": t.assigned_agent_id,
            "profile_id": t.profile_id,
            "intelligence_class": t.intelligence_class,
            "parent_task_id": t.parent_task_id,
            "is_plan_subtask": t.is_plan_subtask,
            "task_type": t.task_type.value if t.task_type else None,
            "pr_url": t.pr_url,
            "integration_mode": t.integration_mode,
            "created_at": t.created_at,
            "updated_at": t.updated_at,
        }

    async def _cmd_get_task_tree(self, args: dict) -> dict:
        """Return the full subtask hierarchy for a single parent task.

        Fetches the task tree from the database and renders it using
        :func:`_format_task_tree`.  Returns both structured data and
        pre-formatted text so callers (Discord embeds, chat agent) can
        choose how to present it.

        Parameters (via *args*):
            task_id (str): Required.  The root task whose tree to fetch.
            compact (bool): If ``True``, render in compact mode (root +
                summary only).  Default ``False``.
            max_depth (int): Maximum nesting depth before collapsing.
                Default 4.
            show_dependencies (bool): If ``True``, annotate tree nodes with
                inline dependency arrows (e.g. ``← needs #abc``).
                Default ``False``.
        """
        task_id: str = args["task_id"]
        compact: bool = args.get("compact", False)
        max_depth: int = args.get("max_depth", 4)
        show_dependencies: bool = args.get("show_dependencies", False)

        tree_data = await self.db.get_task_tree(task_id, max_depth=max_depth)
        if tree_data is None:
            return {"error": f"Task '{task_id}' not found"}

        root_task: Task = tree_data["task"]
        children: list[dict] = tree_data.get("children", [])

        completed, subtask_total = _count_subtree(children)
        status_counts = _count_subtree_by_status(children)

        # Build dependency map for tree annotations when requested.
        dep_map: dict[str, dict] | None = None
        if show_dependencies and not compact:
            all_tasks = [root_task] + _collect_tree_tasks(children)
            dep_map = await self._build_dep_map(all_tasks)
            # Only pass dep_map if it actually contains entries.
            if not dep_map:
                dep_map = None

        formatted = _format_task_tree(
            root_task,
            children,
            compact=compact,
            max_depth=max_depth,
            dep_map=dep_map,
        )

        result: dict = {
            "root": self._task_to_dict(root_task),
            "formatted": formatted,
            "subtask_completed": completed,
            "subtask_total": subtask_total,
            "subtask_by_status": status_counts,
        }

        # In compact mode, include a text progress bar for inline display.
        if compact and subtask_total > 0:
            result["progress_bar"] = progress_bar(completed, subtask_total)

        return result

    async def _cmd_task_children(self, args: dict) -> dict:
        """Direct or recursive children of a task.  Backs ``aq task children``."""
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required"}
        task = await self.db.get_task(task_id)
        if task is None:
            return {"error": f"Task '{task_id}' not found"}
        out_of_scope = self._assert_task_in_scope(task)
        if out_of_scope:
            return out_of_scope
        children = await self.db.get_children(
            task_id,
            recursive=bool(args.get("recursive", False)),
            status=args.get("status"),
            limit=args.get("limit"),
            offset=int(args.get("offset") or 0),
        )
        return {
            "success": True,
            "task_id": task_id,
            "count": len(children),
            "children": [self._task_to_dict(t) for t in children],
        }

    async def _cmd_task_progress(self, args: dict) -> dict:
        """Computed group progress (counts, waves, max parallelism).  Backs ``aq task progress``."""
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required"}
        task = await self.db.get_task(task_id)
        if task is None:
            return {"error": f"Task '{task_id}' not found"}
        out_of_scope = self._assert_task_in_scope(task)
        if out_of_scope:
            return out_of_scope
        progress = await self.db.get_group_progress(task_id)
        return {"success": True, **progress}

    async def _cmd_reparent_task(self, args: dict) -> dict:
        """Move a task under another container, or to root.  Backs ``aq task reparent``.

        On the worker surface (a non-elevated session scope) the command is
        the *repair* half of worker filing (swarm-work-model §12): a worker
        that parented a finding under the task it holds — which then blocks
        its own close with ``hierarchy.open_children`` — moves it beside
        itself or to the project root instead of re-filing a duplicate. The
        worker may move only a task it filed (``created_by_kind='session'``)
        whose provenance leads back to the held task, that nobody has
        claimed yet, and only to a parent the filing path would have
        accepted: the held task, one of its descendants, its immediate
        parent, or root. A root move attaches the routing gate a root
        filing is born with. All of it is decided under the same locks the
        filing path takes, so a concurrent hierarchy move cannot widen it.
        """
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required"}
        if bool(args.get("root")) == bool(args.get("parent_id")):
            return {"error": "exactly one of parent_id or root is required"}
        task = await self.db.get_task(task_id)
        if task is None:
            return {"error": f"Task '{task_id}' not found"}
        new_parent = None if args.get("root") else args["parent_id"]
        old_parent = task.parent_task_id
        gate_id: str | None = None
        scope = self._current_scope or {}
        worker = scope.get("kind") == "session" and not scope.get("elevated")
        held_id: str | None = None
        if worker:
            filing_session = await self.db.get_session(scope.get("session_id") or "")
            if filing_session is None:
                return {"success": False, "error": "no session in scope"}
            if not filing_session.task_id:
                return {
                    "success": False,
                    "code": "idle_session_cannot_file",
                    "error": "idle sessions cannot reparent work; claim a task first",
                }
            held_id = filing_session.task_id
            if task.project_id != filing_session.project_id:
                return {
                    "success": False,
                    "code": "hierarchy.reparent_out_of_scope",
                    "error": _REPARENT_SCOPE_ERROR,
                }
        try:
            async with self.db.immediate() as conn:
                provenance: str | None = None
                if worker and held_id is not None:
                    refusal, provenance = await self._worker_reparent_refusal(
                        conn, held_id=held_id, task_id=task_id, new_parent=new_parent
                    )
                    if refusal is not None:
                        return refusal
                result = await self.db.set_parent(task_id, new_parent, conn=conn)
                if provenance is not None and provenance != new_parent:
                    # The filing's only provenance was the parent-child edge
                    # it just lost (a filing under the held task writes no
                    # separate ``discovered-from``, §12). Keep the edge back
                    # to the work that surfaced it so placement and
                    # provenance never disagree — and so a later move is
                    # still recognisably this worker's filing.
                    result.flipped |= (
                        await self.db.add_dependency(
                            task_id,
                            provenance,
                            DepType.DISCOVERED_FROM.value,
                            description=f"filed under {provenance}, moved by its filer",
                            conn=conn,
                        )
                        or set()
                    )
                if worker and new_parent is None:
                    # A root filing is born with a routing gate (§12) so it
                    # never runs before triage; a filing moved to root gets
                    # the same, unless it already carries an open one.
                    gate_id, _created, gate_flipped = await self.db._create_gate_on(
                        conn,
                        task.project_id,
                        "routing",
                        f"Route: {task.title}",
                        question="",
                        await_id=None,
                        timeout_at=None,
                        waiter_task_ids=[task_id],
                        caller_owns_conn=True,
                    )
                    result.flipped |= gate_flipped
        except HierarchyError as exc:
            return {"error": f"hierarchy.{exc.code}: {exc.detail}", "code": f"hierarchy.{exc.code}"}
        await self.db.log_blocked_flips(result.flipped)
        await self.db._notify_settled(result.settled)
        await self.db._notify_ready(result.ready)
        try:
            await self.orchestrator._emit_task_event(
                "task.reparented", task, old_parent=old_parent or "", new_parent=new_parent or ""
            )
        except AttributeError as e:  # orchestrator missing hook (test doubles)
            logger.warning("reparent_task: failed to emit task.reparented (missing hook): %s", e)
        except Exception as e:
            logger.error(
                "reparent_task: task.reparented emission failed (task=%s): %s",
                task_id,
                e,
                exc_info=True,
            )
        out = {
            "success": True,
            "task_id": task_id,
            "old_parent": old_parent,
            "new_parent": new_parent,
        }
        if gate_id is not None:
            out["gate_id"] = gate_id
        return out

    async def _worker_reparent_refusal(
        self, conn, *, held_id: str, task_id: str, new_parent: str | None
    ) -> tuple[dict | None, str | None]:
        """Decide a worker reparent under the filing locks.

        Returns ``(refusal, provenance)``: ``refusal`` is the error dict when
        the move leaves the filing scope (``None`` when allowed), and
        ``provenance`` is the held-subtree parent the task is about to lose
        as its *only* provenance — the caller re-records it as a
        ``discovered-from`` edge — or ``None`` when a ``discovered-from``
        edge into the held subtree already exists.

        Runs inside the caller's ``immediate()`` transaction, after
        ``lock_filing_scope`` has taken the project hierarchy lock and the
        rows the decision depends on, mirroring ``_create_worker_filed_task``.
        """
        locked = await self.db.lock_filing_scope(
            conn, [held_id, task_id] + ([new_parent] if new_parent else [])
        )
        if held_id not in locked:
            return {
                "success": False,
                "code": "hierarchy.reparent_out_of_scope",
                "error": f"held task '{held_id}' no longer exists",
            }, None
        if task_id not in locked:
            return {"error": f"Task '{task_id}' not found", "code": "hierarchy.not_found"}, None
        held_parent_id = locked[held_id]
        allowed = {held_id} | set(await self.db.subtree_ids(held_id, conn=conn))
        moved = await self.db.get_task(task_id)
        origins = set(await self.db.discovered_from_origins(task_id, conn=conn)) & allowed
        current_parent = locked[task_id]
        worker_filed = (
            moved is not None
            and moved.created_by_kind == "session"
            and (current_parent in allowed or bool(origins))
        )
        claimed = moved is None or moved.status in (
            TaskStatus.IN_PROGRESS,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
        ) or moved.assigned_agent_id is not None
        if not worker_filed or claimed or task_id == held_id:
            return {
                "success": False,
                "code": "hierarchy.reparent_out_of_scope",
                "error": _REPARENT_SCOPE_ERROR,
            }, None
        if new_parent is not None and new_parent not in (
            allowed | ({held_parent_id} if held_parent_id else set())
        ):
            return {
                "success": False,
                "code": "hierarchy.parent_out_of_scope",
                "error": _PARENT_SCOPE_ERROR,
            }, None
        provenance = None if origins else current_parent
        return None, provenance

    async def _create_worker_filed_task(
        self,
        task: Task,
        *,
        held_id: str,
        parent_id: str | None,
        parent_explicit: bool,
        explicit_root: bool,
        discovered_from: str | None,
        edges: list[tuple[str, str, str | None]],
        requirements: list[tuple[str, str | None]],
        labels: list[str],
        hierarchy_enabled: bool,
        reason: str,
        routing_policy: Callable[[Task], bool] | None = None,
        current_parent_head: str | None = None,
        repair_filing_binding: dict | None = None,
        repair_commit_proof: dict | None = None,
    ) -> tuple[str, str | None, str | None, bool, str | None]:
        """Write a worker-filed task + its edges in one ``immediate()`` txn.

        Order (swarm work model §12): the scope re-check first — a locked
        read that writes nothing — then ``reserve_filing`` (``False`` raises
        :class:`_FilingQuota` with nothing written), then the task row, then
        the parent-child edge (if any) and the ``discovered-from`` edge,
        then (root filings only) the routing gate, then the ``depends_on``
        edges. Any exception rolls the whole transaction back untouched.

        ``parent_id`` arrives already defaulted by ``_cmd_create_task``: a
        worker holding a child task files a *sibling* (parent = the held
        task's own parent) unless it named a parent itself or requested an
        explicit root. Both the default and the authorisation it rests on are
        **recomputed here under the row lock**, because the pre-check ran
        before this transaction opened and a reparent that commits in between
        would otherwise file the task under a container the held task no
        longer authorises (or under its former parent). When placement was
        unstated the default follows the held task to wherever it now lives;
        an explicit root remains a root; and a named parent that is no longer
        in scope is refused with :class:`_FilingScope`.

        Returns ``(task_id, gate_id, discovered_from_origin,
        depth_cap_fallback, parent_id)`` — the last being the parent
        actually written, which the caller reports instead of the one it
        passed in.
        ``gate_id`` is set for root filings and policy-gated parented
        filings. ``discovered_from_origin`` is the provenance origin for
        every worker filing: ``discovered_from`` or the held task, except
        for a depth-cap-fallback filing, where it is the would-be container.
        A ``discovered-from`` edge to it is written unless the origin is the
        parent itself (the parent-child edge already records that).

        The ``is_blocked`` flip set from every write below (task-row
        creation never flips anything; the edge, and the gate if any, can)
        is accumulated and logged via :meth:`log_blocked_flips` once, after
        the transaction commits — mirrors what ``add_dependency``/
        ``create_gate`` do for their own single-write callers.
        """
        gate_id: str | None = None
        # The provenance origin: the task the worker named, else the task it
        # holds. Reported on ``task.created`` for every worker filing; the
        # depth-cap branch below repoints it at the would-be container.
        origin: str | None = discovered_from or held_id
        depth_cap_fallback = False
        flipped: set[str] = set()
        async with self.db.immediate() as conn:
            # ---- scope re-check, under the lock -------------------------
            # ``_cmd_create_task``'s pre-check read the held task's parent
            # and subtree before this transaction existed. On Postgres the
            # scope helper takes the same project-scoped hierarchy lock as
            # ``set_parent``, because a named descendant can leave the
            # subtree when any intermediate ancestor is moved; on SQLite
            # ``immediate()`` already excludes concurrent writers. Derive
            # the scope again after that lock.
            locked = await self.db.lock_filing_scope(
                conn,
                [held_id]
                + ([parent_id] if parent_explicit and parent_id else [])
                + ([discovered_from] if discovered_from else []),
            )
            if held_id not in locked:
                raise _FilingScope(f"held task '{held_id}' no longer exists")
            held_parent_id = locked[held_id]
            allowed = {held_id} | set(await self.db.subtree_ids(held_id, conn=conn))
            repair_scope = await self.db.get_repair_filing_scope(
                held_id,
                session_id=(repair_filing_binding or {}).get("session_id"),
                conn=conn,
            )
            if repair_scope is not None:
                if not repair_scope["active"]:
                    raise _FilingScope("repair stage is no longer active")
                current_binding = {
                    key: repair_scope[key]
                    for key in (
                        "operation_id",
                        "stage",
                        "writer_kind",
                        "session_id",
                        "instance_token",
                        "workspace_id",
                        "fence_token",
                    )
                }
                if current_binding != repair_filing_binding:
                    raise _FilingScope("repair stage is no longer active")
                if repair_scope["target_kind"] == "parent":
                    held_parent_id = repair_scope["parent_task_id"]
                elif not explicit_root:
                    raise _FilingScope(
                        "batch repair delegates must explicitly request a root filing"
                    )
            if discovered_from and discovered_from not in allowed:
                raise _FilingScope(_DISCOVERED_FROM_SCOPE_ERROR)
            if not parent_explicit and not explicit_root:
                # The sibling default is re-resolved, not re-authorised: it
                # follows the held task to its current parent (``None`` for a
                # held task that is now a root, which files a root the same
                # way a root-held task always did).
                parent_id = held_parent_id
            elif parent_id and parent_id not in (
                allowed | ({held_parent_id} if held_parent_id else set())
            ):
                raise _FilingScope(_PARENT_SCOPE_ERROR)
            if not await self.db.reserve_filing(
                conn, held_id, max_filings=self.config.swarm.max_filings_per_task
            ):
                raise _FilingQuota()
            if hierarchy_enabled:
                integration_edges = list(edges)
                if origin != parent_id:
                    integration_edges.append(
                        (origin, DepType.DISCOVERED_FROM.value, reason)
                    )
                service = self._hierarchy_integration_service()
                if parent_id:
                    created = await service.file_prepared_child_on(
                        conn,
                        parent_id,
                        task,
                        requirements=requirements,
                        edges=integration_edges,
                        labels=labels,
                        routing_policy=routing_policy,
                        current_parent_head=current_parent_head,
                    )
                    if repair_scope is not None:
                        from src.integration.repair import RepairService

                        await RepairService(self.db).bind_current_parent_subject_on(
                            conn,
                            repair_scope["operation_id"],
                            head_sha=current_parent_head,
                            commit_proof=repair_commit_proof,
                        )
                else:
                    created = await service.file_root_on(
                        conn,
                        task,
                        requirements=requirements,
                        edges=integration_edges,
                        labels=labels,
                    )
                    gate_id, _created, gate_flipped = await self.db._create_gate_on(
                        conn,
                        task.project_id,
                        "routing",
                        f"Route: {task.title}",
                        question="",
                        await_id=None,
                        timeout_at=None,
                        waiter_task_ids=[task.id],
                        caller_owns_conn=True,
                    )
                    flipped |= gate_flipped
                gate_id = created.get("gate_id") or gate_id
                task.parent_task_id = parent_id
            else:
                if parent_id:
                    task.id, depth_cap_fallback = await child_task_id(conn, parent_id)
                else:
                    task.id = await fresh_root_id(conn)
                await self.db.create_task(task, conn=conn)
            if not hierarchy_enabled and parent_id and not depth_cap_fallback:
                result = await self.db.set_parent(
                    task.id, parent_id, conn=conn, description=reason
                )
                flipped |= result.flipped
                # Sibling filing (parent = the held task's own parent) or a
                # filing under a descendant: the parent-child edge places the
                # task, the ``discovered-from`` edge keeps provenance to the
                # work that surfaced it. When the parent *is* the origin the
                # parent-child edge (carrying ``reason``) already says so — a
                # second edge to the same task would only duplicate it.
                if origin != parent_id:
                    flipped |= (
                        await self.db.add_dependency(
                            task.id,
                            origin,
                            DepType.DISCOVERED_FROM.value,
                            description=reason,
                            conn=conn,
                        )
                        or set()
                    )
            elif not hierarchy_enabled and parent_id:
                # Naming-depth cap: child_task_id already minted a root id;
                # record provenance the same way create_task_under does,
                # instead of a parent-child edge to the (too-deep) container.
                origin = parent_id
                flipped |= (
                    await self.db.add_dependency(
                        task.id,
                        origin,
                        DepType.DISCOVERED_FROM.value,
                        description=reason,
                        conn=conn,
                    )
                    or set()
                )
            elif not hierarchy_enabled:
                flipped |= (
                    await self.db.add_dependency(
                        task.id,
                        origin,
                        DepType.DISCOVERED_FROM.value,
                        description=reason,
                        conn=conn,
                    )
                    or set()
                )
                # ``create_gate``'s own conn-path deliberately does not log
                # blocked flips (the caller's transaction hasn't committed
                # yet) — call the same underlying writer directly so we get
                # the flip set back to fold into our own post-commit log,
                # instead of discarding it.
                gate_id, _created, gate_flipped = await self.db._create_gate_on(
                    conn,
                    task.project_id,
                    "routing",
                    f"Route: {task.title}",
                    question="",
                    await_id=None,
                    timeout_at=None,
                    waiter_task_ids=[task.id],
                    caller_owns_conn=True,
                )
                flipped |= gate_flipped
            if not hierarchy_enabled:
                task.parent_task_id = parent_id if parent_id and not depth_cap_fallback else None
            if (
                not hierarchy_enabled
                and parent_id
                and routing_policy is not None
                and routing_policy(task)
            ):
                gate_id, _created, gate_flipped = await self.db._create_gate_on(
                    conn, task.project_id, "routing", "Route task",
                    question="Assign profile + intelligence class (+ workspace if profile needs one).",
                    await_id=None, timeout_at=None, waiter_task_ids=[task.id], caller_owns_conn=True,
                )
                flipped |= gate_flipped
                task.is_blocked = True
            for dep_id, dep_type, dep_reason in (() if hierarchy_enabled else edges):
                flipped |= (
                    await self.db.add_dependency(
                        task.id,
                        dep_id,
                        dep_type,
                        description=dep_reason,
                        conn=conn,
                    )
                    or set()
                )
        await self.db.log_blocked_flips(flipped)
        return task.id, gate_id, origin, depth_cap_fallback, parent_id

    def _validate_routing_class(self, class_id, profile=None) -> str | None:
        """Reject invalid explicit routing instead of launching a fallback model."""
        if class_id is None:
            return None
        if not isinstance(class_id, str) or not class_id.strip():
            return "intelligence_class must be a nonempty class id"
        from src.intelligence_classes import load_intelligence_classes, resolve_class

        # Prefer the live registry so routing validation and session launch
        # agree — including on a file that stopped parsing, where the registry
        # keeps the last good class.
        classes = self._live_intelligence_classes()
        if classes is None:
            classes = load_intelligence_classes(self.config.data_dir)
        cls = classes.get(class_id)
        if cls is None:
            return f"intelligence class '{class_id}' not found in vault"
        if profile is not None:
            provider = _harness_provider(profile.harness)
            mapping = resolve_class(cls, "codex") if profile.harness == "codex" else {}
            mapping = mapping or (resolve_class(cls, provider) if provider else {})
            if provider and not mapping.get("model"):
                return (
                    f"intelligence class '{class_id}' has no model mapping for "
                    f"provider '{provider}' (profile '{profile.id}')"
                )
        return None

    async def _cmd_create_task(self, args: dict) -> dict:
        explicit_root = bool(args.get("root"))
        if explicit_root and args.get("parent_id"):
            return {
                "success": False,
                "error": "--root and parent_id are mutually exclusive",
            }

        # ----- Worker-filed work (swarm work model §12) --------------------
        # A session-scoped, non-elevated caller is a pool worker currently
        # holding a task. Its filings are pinned to its own project, forced
        # to start DEFINED, and constrained to the held task's subtree for
        # the parent/discovered-from edge. ``filing_session``/``held_id``
        # stay ``None`` for every other caller (elevated, local, MCP without
        # session scope) — that path is completely untouched below.
        scope = self._current_scope or {}
        # Scope is authenticated by the command boundary; caller-supplied task
        # arguments must never choose creator provenance. Elevated supervisors
        # have provenance too, without entering the worker filing/quota path.
        creator_session_id = scope.get("session_id") if scope.get("kind") == "session" else None
        filing_session = None
        held_id: str | None = None
        # Whether the worker named a parent itself (vs. the sibling default).
        # Read again by ``_create_worker_filed_task``'s in-transaction check.
        parent_explicit = False
        repair_filing_head: str | None = None
        repair_filing_binding: dict | None = None
        repair_commit_proof: dict | None = None
        if scope.get("kind") == "session" and not scope.get("elevated"):
            filing_session = await self.db.get_session(scope.get("session_id") or "")
            if filing_session is None:
                return {"success": False, "error": "no session in scope"}
            if args.get("project_id") and args["project_id"] != filing_session.project_id:
                return {
                    "success": False,
                    "error": "worker-filed tasks are pinned to the session's project",
                }
            args["project_id"] = filing_session.project_id
            if not filing_session.task_id:
                return {
                    "success": False,
                    "code": "idle_session_cannot_file",
                    "error": "idle sessions cannot file work; claim a task first",
                }
            args.pop("status", None)  # worker-filed work always starts DEFINED
            held_id = filing_session.task_id
            held_task = await self.db.get_task(held_id)
            held_parent_id = held_task.parent_task_id if held_task is not None else None
            repair_scope = await self.db.get_repair_filing_scope(
                held_id, session_id=filing_session.id
            )
            if repair_scope is not None:
                if not repair_scope["active"]:
                    return {
                        "success": False,
                        "error": "repair stage is no longer active",
                    }
                repair_filing_binding = {
                    key: repair_scope[key]
                    for key in (
                        "operation_id",
                        "stage",
                        "writer_kind",
                        "session_id",
                        "instance_token",
                        "workspace_id",
                        "fence_token",
                    )
                }
                if repair_scope["target_kind"] == "parent":
                    held_parent_id = repair_scope["parent_task_id"]
                    from src.integration.hierarchy import resolve_workspace_repair_proof

                    repo = await self.db.get_repo(repair_scope["repository_id"])
                    if held_task is None or repo is None:
                        return {
                            "success": False,
                            "error": "repair filing target is no longer configured",
                        }
                    try:
                        subject = repair_scope["current_subject"]
                        repair_commit_proof = await resolve_workspace_repair_proof(
                            self.db,
                            self.orchestrator.git,
                            {
                                "id": held_task.id,
                                "repo_id": held_task.repo_id,
                                "branch_name": held_task.branch_name,
                            },
                            repo,
                            base_sha=str(subject["head_sha"]),
                        )
                        repair_filing_head = repair_commit_proof["head_sha"]
                    except HierarchyError as exc:
                        return {
                            "success": False,
                            "code": f"hierarchy.{exc.code}",
                            "error": f"hierarchy.{exc.code}: {exc.detail}",
                        }
                elif not explicit_root:
                    return {
                        "success": False,
                        "error": (
                            "batch repair delegates must explicitly request a root filing"
                        ),
                    }
            async with self.db._engine.begin() as _conn:
                allowed = {held_id} | set(await self.db.subtree_ids(held_id, conn=_conn))
            if args.get("discovered_from") and args["discovered_from"] not in allowed:
                return {"success": False, "error": _DISCOVERED_FROM_SCOPE_ERROR}
            # §12: emergent work found while holding a *child* task T is
            # organised as T's sibling — unless the caller explicitly asks
            # for a root, the new task defaults to T's own parent (the shared
            # container/epic) and keeps a
            # ``discovered-from`` edge back to T. The worker may name exactly
            # that immediate parent explicitly; nothing further up or across
            # the tree opens up. A root-held task keeps root filing.
            #
            # Everything below is a *pre-check*: it fails the obvious cases
            # cheaply, before the rest of ``create_task`` does any work. It
            # is not the authorisation — ``_create_worker_filed_task``
            # recomputes both the default and the check from rows it has
            # locked, so a reparent that commits after this read cannot land
            # the filing outside the held task's scope.
            parent_explicit = bool(args.get("parent_id"))
            if not explicit_root and not parent_explicit and held_parent_id:
                args["parent_id"] = held_parent_id
            allowed_parents = allowed | ({held_parent_id} if held_parent_id else set())
            if parent_explicit and args["parent_id"] not in allowed_parents:
                return {"success": False, "error": _PARENT_SCOPE_ERROR}
            if not str(args.get("reason") or "").strip():
                return {
                    "success": False,
                    "code": "reason_required",
                    "error": (
                        "worker-filed tasks must include a reason explaining why the "
                        "new task was spawned; pass 'reason' (CLI: --reason) so it "
                        "can be stored on the edge back to the task you hold"
                    ),
                }

        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
        project = await self.db.get_project(project_id)
        if project is None:
            return {"error": f"Project '{project_id}' not found"}
        hierarchy_enabled = project.hierarchical_integration_mode in {"hierarchy", "train"}
        if hierarchy_enabled and not project.integration_repository_id:
            return {"error": "hierarchical integration requires a designated repository"}
        deliverables, deliverables_error = normalize_deliverables(args.get("deliverables"))
        if deliverables_error:
            return {"error": deliverables_error}
        # ``task_id`` is generated *after* parent_id validation below so
        # hierarchical child ids ({parent}.{n}) can be wired.  Placeholder
        # here keeps the downstream code readable.
        task_id: str = ""
        depth_cap_fallback = False
        integration_mode = args.get("integration_mode")
        if integration_mode is not None and integration_mode not in INTEGRATION_MODES:
            return {
                "error": f"Invalid integration_mode '{integration_mode}'. "
                f"Allowed: {', '.join(sorted(INTEGRATION_MODES))} (omit to inherit "
                "the project/system policy)"
            }
        # Resolve optional task_type from string to enum.
        raw_task_type = args.get("task_type")
        task_type: TaskType | None = None
        if raw_task_type:
            if raw_task_type in TASK_TYPE_VALUES:
                task_type = TaskType(raw_task_type)
            else:
                return {
                    "error": f"Invalid task_type '{raw_task_type}'. "
                    f"Allowed: {', '.join(sorted(TASK_TYPE_VALUES))}"
                }
        # ----- Profile resolution + capability inheritance ----------------
        # When the calling context is sandboxed (the playbook runner / a
        # task adapter set ``self._caller_profile_id``), tasks created
        # without an explicit ``profile_id`` inherit the caller's profile.
        # This preserves the capability sandbox by default — a sandboxed
        # playbook delegating work doesn't accidentally hand the child
        # task broader permissions than itself.
        #
        # When ``profile_id`` IS set explicitly, we require it to be a
        # subset of the caller's capabilities (no upward escalation):
        # ``child.allowed_tools ⊆ parent.allowed_tools`` AND
        # ``child.mcp_servers ⊆ parent.mcp_servers``.  This blocks the
        # confused-deputy attack where prompt-injected text in a
        # sandboxed playbook says "create a task with profile=admin and
        # description=`rm -rf`".
        #
        # **v1 gap — recursive task→child-task escalation — CLOSED** by
        # Playbook V2 Package 0 (§1.4, §3.9).  It used to read: when
        # ``create_task`` is invoked by a task agent over HTTP/MCP there is
        # no per-task identity on the request, so ``_caller_profile_id`` is
        # unset and this check never fires.  The stated fallback — the
        # harness ``--allowedTools`` flag — never applied either, because AQ
        # command names were dropped from that flag entirely.
        #
        # ``_caller_profile_id`` is now a shim over the request-local
        # ``ExecutionPrincipal``, which ``CommandHandler.execute`` derives
        # from the *session row* keyed by the token's ``session_id``.  Every
        # real tmux session therefore arrives with a resolved profile, and
        # the subset check below fires on the path that used to bypass it.
        # See ``docs/specs/design/sandboxed-playbooks.md`` and
        # ``docs/superpowers/plans/2026-09-01-playbook-v2-phase0-security.md``.
        profile_id = args.get("profile_id")
        caller_profile_id = getattr(self, "_caller_profile_id", None)
        caller_profile = None
        # Both fail-closed branches below are conditioned on ``profile_id``
        # being **explicitly requested**.  The refusal exists to stop a grant
        # nobody can bound: without a resolved parent policy there is no
        # subset to check a named child profile against.  A caller that names
        # no profile is asking for no grant, so there is nothing to bound and
        # nothing to widen — it inherits whatever it would have inherited
        # before this package, which for an unresolvable caller is nothing.
        #
        # Refusing that case too would delete a *pre-existing* capability
        # rather than close a gap: a worker filing discovered work
        # (``aq task create`` with no ``--profile``) reached this code with
        # ``caller_profile_id is None`` before Package 0, because
        # ``_caller_profile_id`` was only ever set by the playbook runner.
        # Now that the shim resolves it from the session row, an unresolvable
        # profile row would newly strand every such filing — the exact
        # fleet-stranding outcome ``audit`` mode exists to prevent (§3.6).
        # Under ``enforce`` the question does not arise: the dispatch gate
        # denies an unresolved principal before ``create_task`` runs at all
        # (``tests/test_delegation_no_widening.py::TestFailClosed``).
        if caller_profile_id is None:
            # Fail closed: an enforced principal that could not resolve a
            # profile must not be able to delegate at all.  A trusted local
            # or service caller has no profile by design and is unaffected.
            from src.commands.principal import current_principal

            principal = current_principal()
            if principal is not None and principal.enforced and profile_id:
                return {"error": "delegation refused: caller has no resolved profile"}
        if caller_profile_id:
            caller_profile = await self.db.get_profile(caller_profile_id)
            if caller_profile is None and profile_id:
                # Caller profile is gone — fail closed.  Leaks of stale
                # caller_profile_id mid-run shouldn't widen the child's
                # scope; refuse the create until the situation is sane.
                return {
                    "error": f"Caller profile '{caller_profile_id}' not "
                    "found — refusing to create task without a resolved "
                    "capability bound."
                }
            if caller_profile is None:
                logger.warning(
                    "delegation_unbounded_shadow cmd=create_task profile=%s "
                    "reason=caller-profile-not-found; child task inherits no "
                    "profile",
                    caller_profile_id,
                )

        profile = caller_profile
        if profile_id:
            profile = await self.db.get_profile(profile_id)
            if not profile:
                return {"error": f"Profile '{profile_id}' not found"}
            if caller_profile is not None and profile.id != caller_profile.id:
                escalation = _check_capability_escalation(caller_profile, profile)
                if escalation:
                    return {
                        "error": (
                            f"Capability escalation rejected: child profile "
                            f"'{profile.id}' is not a subset of caller profile "
                            f"'{caller_profile.id}'. {escalation}"
                        )
                    }
        elif caller_profile is not None:
            # Default-inherit so the child cannot exceed the caller.
            profile_id = caller_profile.id
        class_error = self._validate_routing_class(args.get("intelligence_class"), profile)
        if class_error:
            return {"success": False, "error": class_error}
        # Validate optional preferred_workspace_id
        preferred_workspace_id = args.get("preferred_workspace_id")
        if preferred_workspace_id:
            ws = await self.db.get_workspace(preferred_workspace_id)
            if not ws:
                return {"error": f"Workspace '{preferred_workspace_id}' not found"}
            if ws.project_id != project_id:
                return {
                    "error": f"Workspace '{preferred_workspace_id}' belongs to "
                    f"project '{ws.project_id}', not '{project_id}'"
                }
        # Validate optional attachments (list of file paths)
        attachments = args.get("attachments", [])
        if attachments:
            import os

            valid_paths = []
            for path in attachments:
                if os.path.isfile(path):
                    valid_paths.append(os.path.abspath(path))
                else:
                    return {"error": f"Attachment file not found: {path}"}
            attachments = valid_paths

        # Validate optional affinity_agent_id
        affinity_agent_id = args.get("affinity_agent_id")
        if affinity_agent_id:
            agent = await self.db.get_agent(affinity_agent_id)
            if not agent:
                return {"error": f"Agent '{affinity_agent_id}' not found for affinity"}

        # Validate optional affinity_reason
        affinity_reason = args.get("affinity_reason")
        valid_affinity_reasons = {"context", "workspace", "type"}
        if affinity_reason and affinity_reason not in valid_affinity_reasons:
            return {
                "error": f"Invalid affinity_reason '{affinity_reason}'. "
                f"Allowed: {', '.join(sorted(valid_affinity_reasons))}"
            }

        # Validate optional workspace_mode
        raw_workspace_mode = args.get("workspace_mode")
        workspace_mode: WorkspaceMode | None = None
        if raw_workspace_mode:
            if raw_workspace_mode in WORKSPACE_MODE_VALUES:
                workspace_mode = WorkspaceMode(raw_workspace_mode)
            else:
                return {
                    "error": f"Invalid workspace_mode '{raw_workspace_mode}'. "
                    f"Allowed: {', '.join(sorted(WORKSPACE_MODE_VALUES))}"
                }

        # Warn if directory-isolated is set — it's accepted for storage but
        # not yet implemented.  The orchestrator will reject it at execution
        # time with a clear error.
        warn_deferred_mode = workspace_mode == WorkspaceMode.DIRECTORY_ISOLATED

        # Validate optional requires_kinds (workspaces-v2 spec §5.1).
        # Each entry is either a string ("game-repo") — sugar for
        # {"kind": "game-repo", "alias": None} — or a dict with at least
        # "kind".  Validates that every kind resolves (project-scoped row
        # or system row) per spec §5.4.  Instance availability is NOT
        # checked here — that's an acquisition-time concern.
        raw_requires_kinds = args.get("requires_kinds") or []
        normalized_requirements: list[tuple[str, str | None]] = []
        for entry in raw_requires_kinds:
            if isinstance(entry, str):
                kind_id, alias = entry, None
            elif isinstance(entry, dict):
                kind_id = entry.get("kind") or entry.get("kind_id")
                alias = entry.get("alias")
                if not kind_id:
                    return {"error": (f"requires_kinds entry {entry!r} is missing 'kind'")}
            else:
                return {
                    "error": (
                        f"requires_kinds entries must be str or dict; got "
                        f"{type(entry).__name__}: {entry!r}"
                    )
                }
            resolved = await self.db.resolve_workspace_kind(project_id, kind_id)
            if resolved is None:
                return {
                    "error": (
                        f"Kind '{kind_id}' is not defined for project "
                        f"'{project_id}' and no system default exists. "
                        "Define it in vault/[projects/<pid>/]workspace-kinds/"
                        f"{kind_id}.md or use a known kind id."
                    )
                }
            normalized_requirements.append((kind_id, alias))

        # Validate optional labels (work-graph design §6).
        labels = _normalize_label_list(args.get("labels"))

        # Validate optional graph edges.  ``depends_on`` accepts a bare id, a
        # list of ids, or a list of ``{"task_id": ..., "dep_type": ...}``
        # dicts; ``parent_id`` is sugar for a ``parent-child`` edge plus the
        # denormalised ``parent_task_id`` pointer.
        raw_depends_on = args.get("depends_on")
        if isinstance(raw_depends_on, str):
            raw_depends_on = [raw_depends_on]
        spawn_reason = str(args.get("reason") or "").strip() or None
        edges: list[tuple[str, str, str | None]] = []
        for entry in raw_depends_on or []:
            dep_reason: str | None = None
            if isinstance(entry, str):
                dep_id, dep_type = entry, DepType.BLOCKS.value
            elif isinstance(entry, dict):
                dep_id = entry.get("task_id") or entry.get("id") or ""
                dep_type = entry.get("dep_type") or DepType.BLOCKS.value
                dep_reason = (
                    str(entry.get("reason") or entry.get("description") or "").strip()
                    or None
                )
            else:
                return {
                    "error": (
                        "depends_on entries must be str or dict; got "
                        f"{type(entry).__name__}: {entry!r}"
                    )
                }
            if not dep_id:
                return {"error": f"depends_on entry {entry!r} is missing 'task_id'"}
            if dep_type not in DEP_TYPE_VALUES:
                return {
                    "error": f"Invalid dep_type '{dep_type}'. "
                    f"Allowed: {', '.join(sorted(DEP_TYPE_VALUES))}"
                }
            if filing_session is not None and dep_type == DepType.PARENT_CHILD.value:
                # §12: worker-filed parenting goes through ``parent_id`` only
                # — that's the single code path the subtree constraint above
                # guards. A ``parent-child`` entry smuggled in via
                # ``depends_on`` would otherwise bypass it entirely.
                return {
                    "success": False,
                    "error": (
                        "worker-filed tasks cannot set a parent-child edge via "
                        "'depends_on'; use 'parent_id' instead"
                    ),
                }
            if await self.db.get_task(dep_id) is None:
                return {"error": f"Dependency task '{dep_id}' not found"}
            edges.append((dep_id, dep_type, dep_reason))

        parent_id = args.get("parent_id")
        if parent_id:
            parent = await self.db.get_task(parent_id)
            if parent is None:
                return {"error": f"Parent task '{parent_id}' not found"}
        if (
            filing_session is not None
            and hierarchy_enabled
            and repair_filing_head is None
            and parent_id == held_id
        ):
            from src.integration.hierarchy import resolve_workspace_checkpoint

            held_task = await self.db.get_task(held_id)
            repo = await self.db.get_repo(project.integration_repository_id or "")
            if held_task is None or repo is None:
                return {"error": "hierarchical filing writer is no longer configured"}
            try:
                repair_filing_head = await resolve_workspace_checkpoint(
                    self.db,
                    self.orchestrator.git,
                    {
                        "id": held_task.id,
                        "repo_id": held_task.repo_id,
                        "branch_name": held_task.branch_name,
                    },
                    repo,
                )
            except HierarchyError as exc:
                return {
                    "success": False,
                    "code": f"hierarchy.{exc.code}",
                    "error": f"hierarchy.{exc.code}: {exc.detail}",
                }

        # A task created *with* blocking edges starts DEFINED so the
        # promotion cascade decides when it becomes runnable — creating it
        # READY-but-blocked would hand the scheduler a task it must not run.
        # A parented task is always withheld until ``set_parent``/
        # ``create_task_under`` recomputes it against the (possibly-DEFINED)
        # container (work-graph §7).
        has_blocking_edge = bool(parent_id) or any(
            dep_type in BLOCKING_DEP_TYPES for _, dep_type, _ in edges
        )
        internal_initial_status = (
            args.get("_initial_status") if filing_session is None else None
        )
        if internal_initial_status is not None:
            try:
                initial_status = TaskStatus(internal_initial_status)
            except ValueError:
                return {"error": f"Invalid internal initial status '{internal_initial_status}'"}
        else:
            initial_status = (
                TaskStatus.DEFINED
                if (self._plan_subtask_creation_mode or has_blocking_edge or filing_session is not None)
                else TaskStatus.READY
            )
        skip_verification = args.get("skip_verification", False)
        workflow_id = args.get("workflow_id")
        task = Task(
            id="",
            project_id=project_id,
            title=args["title"],
            description=args.get("description", args["title"]),
            priority=args.get("priority", 100),
            status=initial_status,
            integration_mode=integration_mode,
            task_type=task_type,
            profile_id=profile_id,
            preferred_workspace_id=preferred_workspace_id,
            attachments=attachments,
            deliverables=deliverables,
            skip_verification=skip_verification,
            workflow_id=workflow_id,
            affinity_agent_id=affinity_agent_id,
            affinity_reason=affinity_reason,
            workspace_mode=workspace_mode,
            parent_task_id=None,
            dedup_key=args.get("dedup_key"),
            intelligence_class=args.get("intelligence_class"),
            created_by_kind="session" if creator_session_id else None,
            created_by_id=creator_session_id,
            repo_id=project.integration_repository_id if hierarchy_enabled else None,
        )
        from src.playbooks.routing import requires_routing_gate
        manager = getattr(self.orchestrator, "playbook_manager", None)
        routing_policy = None
        if manager is not None and not profile_id and not args.get("_suppress_created_event"):
            def routing_policy(created_task: Task) -> bool:
                # Evaluate after IDs/parent edges are allocated, inside the
                # creation transaction, against the same fields as task.created.
                return requires_routing_gate(manager, created_task, {
                    "parent_task_id": created_task.parent_task_id,
                    "created_by_kind": created_task.created_by_kind if filing_session is not None else None,
                    "created_by_id": created_task.created_by_id if filing_session is not None else None,
                    "filed_by_profile_id": filing_session.profile_id if filing_session is not None else None,
                })
        gate_id: str | None = None
        discovered_from_origin: str | None = None
        depth_cap_fallback = False
        hierarchy_created = False
        if hierarchy_enabled and filing_session is None:
            try:
                async with self.db.immediate() as conn:
                    service = self._hierarchy_integration_service()
                    if parent_id:
                        created = await service.file_prepared_child_on(
                            conn,
                            parent_id,
                            task,
                            requirements=normalized_requirements,
                            edges=edges,
                            labels=labels,
                            routing_policy=routing_policy,
                        )
                        task.parent_task_id = parent_id
                    else:
                        created = await service.file_root_on(
                            conn,
                            task,
                            requirements=normalized_requirements,
                            edges=edges,
                            labels=labels,
                            routing_policy=routing_policy,
                        )
                task_id = created["task_id"]
                gate_id = created.get("gate_id")
                hierarchy_created = True
            except HierarchyError as exc:
                return {
                    "success": False,
                    "error": f"hierarchy.{exc.code}: {exc.detail}",
                    "code": f"hierarchy.{exc.code}",
                }
        elif filing_session is not None:
            try:
                (
                    task_id,
                    gate_id,
                    discovered_from_origin,
                    depth_cap_fallback,
                    # The parent actually written: the in-transaction
                    # re-check may have re-resolved the sibling default
                    # against a held task that moved. Everything below
                    # (the event, the response) reports that one.
                    parent_id,
                ) = await self._create_worker_filed_task(
                    task,
                    held_id=held_id,
                    parent_id=parent_id,
                    parent_explicit=parent_explicit,
                    explicit_root=explicit_root,
                    discovered_from=args.get("discovered_from"),
                    edges=edges,
                    requirements=normalized_requirements,
                    labels=labels,
                    hierarchy_enabled=hierarchy_enabled,
                    reason=spawn_reason or "",
                    routing_policy=routing_policy,
                    current_parent_head=repair_filing_head,
                    repair_filing_binding=repair_filing_binding,
                    repair_commit_proof=repair_commit_proof,
                )
                hierarchy_created = hierarchy_enabled
            except _FilingScope as exc:
                return {"success": False, "error": exc.error}
            except _FilingQuota:
                return {
                    "success": False,
                    "code": "filing_quota_exceeded",
                    "error": (
                        f"task {held_id} has already filed "
                        f"{self.config.swarm.max_filings_per_task} tasks "
                        "(swarm.max_filings_per_task)"
                    ),
                }
            except HierarchyError as exc:
                # e.g. ``container_closed`` — the held task's subtree
                # container closed between the earlier subtree check and
                # ``set_parent``'s write. ``immediate()`` already rolled the
                # whole transaction back (reserve_filing included), so
                # nothing was written — mirror the elevated path's shape
                # below plus ``success: False`` for the worker-filed caller.
                return {
                    "success": False,
                    "error": f"hierarchy.{exc.code}: {exc.detail}",
                    "code": f"hierarchy.{exc.code}",
                }
        elif parent_id:
            try:
                task_id, depth_cap_fallback = await self.db.create_task_under(
                    task,
                    parent_id,
                    description=spawn_reason,
                    **({"routing_policy": routing_policy} if routing_policy is not None else {}),
                )
            except HierarchyError as exc:
                return {
                    "error": f"hierarchy.{exc.code}: {exc.detail}",
                    "code": f"hierarchy.{exc.code}",
                }
        else:
            task_id = await generate_task_id(self.db)
            task.id = task_id
            await self.db.create_task(task, **({"routing_policy": routing_policy} if routing_policy is not None else {}))

        # Persist requires_kinds rows now that the FK target exists.
        if normalized_requirements and not hierarchy_created:
            await self.db.add_task_workspace_requirements(task_id, normalized_requirements)

        # Graph edges and labels, now that the FK target exists.  Each
        # ``add_dependency`` recomputes the blocked-state projection, so the
        # task's ``is_blocked`` is correct before anything can schedule it.
        # Worker-filed edges were already written inside
        # ``_create_worker_filed_task``'s transaction above — only log them
        # here so the audit trail is identical either way.
        for dep_id, dep_type, dep_reason in edges:
            if filing_session is None and not hierarchy_created:
                try:
                    await self.db.add_dependency(
                        task_id, dep_id, dep_type, description=dep_reason
                    )
                except HierarchyError as exc:
                    return {
                        "error": (
                            f"hierarchy.{exc.code}: {exc.detail} "
                            f"(task '{task_id}' was already created; fix the edge with "
                            f"'aq task deps')"
                        ),
                        "code": f"hierarchy.{exc.code}",
                        "task_created": task_id,
                    }
            await self.db.log_event(
                "dependency.added",
                project_id=project_id,
                task_id=task_id,
                payload=f"{dep_type} -> {dep_id}",
            )
        for label in labels:
            if not hierarchy_created:
                await self.db.add_task_label(task_id, label)
            await self.db.log_event(
                "label.added",
                project_id=project_id,
                task_id=task_id,
                payload=label,
            )

        # If the supervisor set conversation context (the thread chain it was
        # responding to), store it as task_context so the executing agent gets
        # the same conversational backdrop the supervisor had.
        if self._current_conversation_context:
            await self.db.add_task_context(
                task_id,
                type="conversation_context",
                label="Conversation Thread Context",
                content=self._current_conversation_context,
            )

        if routing_policy is not None or gate_id is not None:
            await self._emit_admitted_routing_gates(task_id)

        # Emit ``task.created`` for configured pipeline subscribers. The
        # bundled default no longer performs assignment here; the independent
        # assignment coordinator routes eligible tasks at the scheduler
        # boundary. A custom project pipeline may still use this event.
        #
        # NON-OBVIOUS INVARIANT: ``ensure_task`` passes ``_suppress_created_event``
        # so control-plane bookkeeping tasks do not recursively fire pipeline
        # rules against themselves.
        if not args.get("_suppress_created_event"):
            try:
                extras: dict[str, object] = {}
                if profile_id:
                    extras["profile_id"] = profile_id
                if task_type:
                    extras["task_type"] = task_type.value
                # Worker-filed work (swarm work model §12): always present,
                # ``None`` when not applicable. Custom subscribers may use
                # these provenance fields. ``parent_id``/``depth_cap_fallback``
                # reflect the real edge written above; ``task.parent_task_id``
                # itself is never updated in-memory.
                extras["created_by_kind"] = task.created_by_kind if filing_session is not None else None
                extras["created_by_id"] = task.created_by_id if filing_session is not None else None
                extras["filed_by_profile_id"] = (
                    filing_session.profile_id if filing_session is not None else None
                )
                extras["discovered_from"] = (
                    discovered_from_origin if filing_session is not None else None
                )
                extras["parent_task_id"] = (
                    parent_id if (parent_id and not depth_cap_fallback) else None
                )
                await self.orchestrator._emit_task_event("task.created", task, **extras)
            except AttributeError as e:  # orchestrator missing hook (test doubles)
                logger.warning("create_task: failed to emit task.created (missing hook): %s", e)
            except Exception as e:
                # Narrow-log the emission failure loudly for custom pipeline
                # subscribers. Assignment routing itself reconciles from the DB
                # and does not depend on this event being delivered.
                logger.error(
                    "create_task: task.created emission failed (task=%s project=%s): %s",
                    task_id,
                    project_id,
                    e,
                    exc_info=True,
                )
                # Opt-in re-raise for dev/CI: config value must be
                # explicitly truthy AND a real bool (guards against
                # MagicMock configs in tests where every attr is truthy).
                if getattr(self.config, "dev_strict", False) is True:
                    raise

        # Emit notify.task_added so the Discord layer (and other transports)
        # can post a "Task Added" notification to the project's channel — or
        # the system/global channel when the project has no dedicated one.
        # Fires for every creation path (slash command, playbook, MCP, API)
        # and is NOT suppressed by project pause/archive status.
        try:
            from src.notifications.builder import build_task_detail
            from src.notifications.events import TaskAddedEvent

            await self.orchestrator._emit_notify(
                "notify.task_added",
                TaskAddedEvent(
                    task=build_task_detail(task),
                    source=args.get("_source", ""),
                    project_id=task.project_id,
                ),
            )
        except Exception as e:
            logger.warning("create_task: failed to emit notify.task_added: %s", e)

        result = {
            "created": task_id,
            "success": True,
            "task_id": task_id,
            "gate_id": gate_id,
            "status": task.status.value,
            "title": task.title,
            "project_id": task.project_id,
        }
        if integration_mode:
            result["integration_mode"] = integration_mode
        if task_type:
            result["task_type"] = task_type.value
        if profile_id:
            result["profile_id"] = profile_id
        if task.intelligence_class:
            result["intelligence_class"] = task.intelligence_class
        if preferred_workspace_id:
            result["preferred_workspace_id"] = preferred_workspace_id
        if attachments:
            result["attachments"] = attachments
        if skip_verification:
            result["skip_verification"] = True
        if workflow_id:
            result["workflow_id"] = workflow_id
        if affinity_agent_id:
            result["affinity_agent_id"] = affinity_agent_id
        if affinity_reason:
            result["affinity_reason"] = affinity_reason
        if workspace_mode:
            result["workspace_mode"] = workspace_mode.value
        if normalized_requirements:
            result["requires_kinds"] = [{"kind": k, "alias": a} for k, a in normalized_requirements]
        if edges:
            result["depends_on"] = [
                {"task_id": dep_id, "dep_type": dep_type, "reason": dep_reason}
                for dep_id, dep_type, dep_reason in edges
            ]
        if spawn_reason:
            result["reason"] = spawn_reason
        if parent_id:
            result["parent_id"] = parent_id
        if labels:
            result["labels"] = labels
        if warn_deferred_mode:
            result["warning"] = (
                "workspace_mode='directory-isolated' is accepted but not yet implemented. "
                "The task will fail at execution time. This mode is reserved for future "
                "monorepo support. Use 'exclusive' instead ('branch-isolated' is "
                "deprecated and now behaves identically to 'exclusive')."
            )

        # Cross-project warning: if project_id was implicitly inherited from
        # the active channel context (not explicitly passed by the caller),
        # check whether the task title or description mentions another known
        # project name.  This catches the common mistake of creating a task
        # for project A while chatting in project B's channel.
        if not args.get("project_id"):
            other_projects = await self.db.list_projects()
            text_to_check = f"{task.title} {task.description}".lower()
            mentioned = [
                p.id for p in other_projects if p.id != project_id and p.id.lower() in text_to_check
            ]
            if mentioned:
                result["warning"] = (
                    f"Task was assigned to '{project_id}' (from channel context) "
                    f"but its content mentions project(s): {', '.join(mentioned)}. "
                    f"If this task belongs to a different project, update it with "
                    f"edit_task(task_id='{task_id}', project_id='<correct_project>')."
                )

        return result

    async def _validate_graph_parent(
        self, project_id: str, parent_id: str | None
    ) -> tuple[dict | None, Task | None]:
        """Checks an optional container to build a graph under (supervisor-agent §8).

        Returns ``(error, parent)`` — *error* is a ``{"error", "code"}`` dict
        or ``None`` when *parent_id* is absent or passes every check; *parent*
        is the fetched :class:`~src.models.Task` on success (``None`` on
        error or when no ``parent_id`` was given), so a caller that also
        needs the row (``_cmd_create_task_graph`` reads ``parent.title``)
        does not have to fetch it a second time.  Factored out of
        ``_cmd_create_task_graph`` so ``_cmd_formula_cook`` (swarm-work-model
        §13) shares the exact same rules.
        """
        if not parent_id:
            return None, None
        parent = await self.db.get_task(parent_id)
        if parent is None:
            return {
                "error": f"Parent task '{parent_id}' not found",
                "code": "hierarchy.not_found",
            }, None
        if parent.project_id != project_id:
            return {
                "error": "parent is in another project",
                "code": "hierarchy.cross_project",
            }, None
        if parent.status == TaskStatus.COMPLETED:
            return {"error": "parent is COMPLETED", "code": "hierarchy.container_closed"}, None
        async with self.db._engine.begin() as conn:
            depth = await self.db.structural_depth(parent_id, conn=conn)
        if depth + 1 > MAX_STRUCTURAL_DEPTH:
            return {
                "error": f"parent at structural depth {depth}; cap is {MAX_STRUCTURAL_DEPTH}",
                "code": "hierarchy.depth",
            }, None
        if naming_depth(parent_id) >= MAX_NAMING_DEPTH:
            return {
                "error": (
                    f"parent '{parent_id}' is at naming depth cap "
                    f"{MAX_NAMING_DEPTH} — a graph cannot mint further dotted children"
                ),
                "code": "hierarchy.depth",
            }, None
        return None, parent

    async def _cmd_create_task_graph(self, args: dict) -> dict:
        """Create a whole task graph in one transaction (supervisor-agent §8).

        Accepts either an inline ``graph`` document or a ``spec_path`` whose
        fenced ``aq-graph`` block defines the graph.  Validation is
        deterministic and complete — every finding is reported at once rather
        than failing on the first — and nothing is written unless there are
        zero errors.  ``dry_run`` returns the same report plus the ids that
        would be assigned.
        """
        from src.task_graph import (
            GraphParseError,
            create_graph,
            extract_graph_from_spec,
            parse_graph,
            split_findings,
            validate_graph,
        )
        from src.task_graph.validator import resolve_spec_path_checked

        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}

        raw_graph = args.get("graph")
        spec_path = args.get("spec_path")
        if bool(raw_graph) == bool(spec_path):
            return {"error": "exactly one of 'graph' or 'spec_path' is required"}

        parent_id = args.get("parent_id")
        parent = None
        if parent_id:
            parent_error, parent = await self._validate_graph_parent(project_id, parent_id)
            if parent_error is not None:
                return parent_error

        vault_root = getattr(self.config, "vault_root", None)

        try:
            if spec_path:
                resolved, reason = resolve_spec_path_checked(
                    spec_path, vault_root=vault_root, source_path=None
                )
                if reason == "outside_vault":
                    return {
                        "error": (
                            f"Spec '{spec_path}' resolves outside the vault — "
                            "spec paths must stay inside the vault root"
                        )
                    }
                if resolved is None:
                    return {"error": f"Spec '{spec_path}' not found in the vault"}
                with open(resolved, encoding="utf-8") as handle:
                    markdown = handle.read()
                graph = extract_graph_from_spec(markdown, resolved)
            else:
                graph = parse_graph(raw_graph)
        except GraphParseError as exc:
            return {
                "error": "graph document is invalid",
                "errors": [e.to_dict() for e in exc.errors],
            }
        except OSError as exc:
            return {"error": f"Could not read spec '{spec_path}': {exc}"}

        for node in graph.nodes:
            if node.profile is None and args.get("profile_id"):
                node.profile = args["profile_id"]
            if node.intelligence_class is None and args.get("intelligence_class"):
                node.intelligence_class = args["intelligence_class"]

        findings = await validate_graph(
            graph, project_id=project_id, db=self.db, vault_root=vault_root
        )
        from src.task_graph.models import GraphError
        class_errors: dict[tuple[str | None, str], str | None] = {}
        for node in graph.nodes:
            if node.intelligence_class is None:
                continue
            route = (node.profile, node.intelligence_class)
            if route not in class_errors:
                profile = await self.db.get_profile(node.profile) if node.profile else None
                class_errors[route] = self._validate_routing_class(node.intelligence_class, profile)
            if class_errors[route]:
                findings.append(GraphError(
                    rule="invalid_intelligence_class", detail=class_errors[route], node=node.key,
                ))
        errors, warnings = split_findings(findings)
        if errors:
            return {
                "error": (
                    f"graph validation failed with {len(errors)} error(s) — nothing was created"
                ),
                "errors": [e.to_dict() for e in errors],
                "warnings": [w.to_dict() for w in warnings],
            }

        dry_run = bool(args.get("dry_run", False))
        report = await create_graph(
            self, graph, project_id=project_id, dry_run=dry_run, parent_id=parent_id
        )
        if not dry_run:
            container = await self.db.get_task(report["parent_id"])
            if container is not None:
                await self._emit_task_graph_change("task.updated", container)
        report["project_id"] = project_id
        report["warnings"] = [w.to_dict() for w in warnings]
        if parent_id:
            report["parent_title"] = parent.title
        if graph.spec:
            report["spec"] = graph.spec
        return report

    async def _cmd_get_task(self, args: dict) -> dict:
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}
        if args.get("clear_needs_attention") and args.get("needs_attention") is not None:
            return {"error": "--clear-needs-attention and --needs-attention are mutually exclusive"}
        out_of_scope = self._assert_task_in_scope(task)
        if out_of_scope:
            return out_of_scope
        info = {
            "id": task.id,
            "project_id": task.project_id,
            "title": task.title,
            "description": task.description,
            "status": task.status.value,
            "priority": task.priority,
            "assigned_agent": task.assigned_agent_id,
            "retry_count": task.retry_count,
            "max_retries": task.max_retries,
            # The branch is work-state every read surface needs: `aq task show`
            # renders `task.branch_name or "—"` (cli/formatters.py), so leaving
            # it out of this payload made every task look branchless and sent a
            # PR-integration outage investigation after a persistence bug that
            # did not exist.  The row has always carried it.
            "branch_name": task.branch_name,
            "integration_mode": task.integration_mode,
            "attachments": task.attachments,
            "deliverables": task.deliverables,
            # Persisted graph blockedness (work-graph design §4).  Capacity
            # reasons (no agent, workspace busy, budget) are NOT in here —
            # those belong to `task explain`.
            "is_blocked": task.is_blocked,
            "is_plan_subtask": task.is_plan_subtask,
            "task_type": task.task_type.value if task.task_type else None,
            "parent_task_id": task.parent_task_id,
            "profile_id": task.profile_id,
            "intelligence_class": task.intelligence_class,
            "skip_verification": task.skip_verification,
            "workflow_id": task.workflow_id,
            "affinity_agent_id": task.affinity_agent_id,
            "affinity_reason": task.affinity_reason,
            "workspace_mode": task.workspace_mode.value if task.workspace_mode else None,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }
        # Unconditional, unlike the historical `if task.pr_url:` — a missing
        # key and an absent PR are the same state to a caller, and the
        # conditional only made the payload's shape vary for no gain.
        info["pr_url"] = task.pr_url

        # Effective integration policy + its source, so surfaces can show
        # where the mode comes from instead of another ambiguous flag.
        from src.models import resolve_integration_mode_with_source

        parent_mode = None
        if task.is_plan_subtask and task.parent_task_id:
            parent = await self.db.get_task(task.parent_task_id)
            parent_mode = parent.integration_mode if parent else None
        project = await self.db.get_project(task.project_id)
        effective_mode, mode_source = resolve_integration_mode_with_source(
            task.integration_mode,
            parent_task_mode=parent_mode,
            project_mode=project.integration_mode if project else None,
            default_mode=self.orchestrator.config.integration.default_mode,
        )
        info["effective_integration_mode"] = effective_mode
        info["integration_mode_source"] = mode_source

        info["needs_attention"] = await self.db.get_task_meta(task.id, "needs_attention")
        completion = await self.db.get_task_completion(task.id)
        info["completion"] = asdict(completion) if completion else None

        # Dependency visualization: show what this task depends on and blocks
        typed_edges = await self.db.get_typed_dependencies_detailed(task.id)
        blocking_edges = [edge for edge in typed_edges if edge["dep_type"] in BLOCKING_DEP_TYPES]
        if blocking_edges:
            dep_details = []
            for edge in blocking_edges:
                dep_id = edge["depends_on_task_id"]
                dep_task = await self.db.get_task(dep_id)
                if dep_task:
                    dep_details.append(
                        {
                            "id": dep_task.id,
                            "title": dep_task.title,
                            "status": dep_task.status.value,
                            "dep_type": edge["dep_type"],
                            "reason": edge["description"],
                        }
                    )
            info["depends_on"] = dep_details

        dependents = await self.db.get_dependents(task.id)
        if dependents:
            dep_details = []
            for dep_id in dependents:
                dep_task = await self.db.get_task(dep_id)
                if dep_task:
                    dep_details.append(
                        {
                            "id": dep_task.id,
                            "title": dep_task.title,
                            "status": dep_task.status.value,
                        }
                    )
            info["blocks"] = dep_details

        # Subtask info
        subtasks = await self.db.get_subtasks(task.id)
        if subtasks:
            info["subtasks"] = [
                {
                    "id": st.id,
                    "title": st.title,
                    "status": st.status.value,
                }
                for st in subtasks
            ]

        info["children"] = await self.db.get_children_summary(task.id)

        return info

    async def _cmd_task_deps(self, args: dict) -> dict:
        """Return upstream dependencies and downstream dependents for a task.

        Used by the ``/task-deps`` slash command to render a focused
        dependency view with visual status for each related task.

        Returns
        -------
        dict
            ``task_id``, ``title``, ``status``, ``depends_on``, ``blocks``
            and ``provenance``.  Each entry carries ``id``, ``title`` and
            ``status``; ``provenance`` entries add ``dep_type``.

        ``depends_on`` / ``blocks`` are the *blocking* edges — "what holds
        me back" and "what I hold back".  ``provenance`` is everything else
        the graph records about where a task came from, which today means
        ``discovered-from``: the edge ``create_task`` writes when a worker
        files work mid-task (swarm-work-model §12).  Without it that edge
        was invisible on every read surface — the row existed, the
        dashboard's graph view drew it, and no command would tell you
        which task a filing came out of.

        Like ``depends_on``, provenance edges are **outgoing** — they run
        from *task_id* toward its origin, so each entry is the task this
        one came out of, not a task that came out of it.  (That is why the
        two share ``get_typed_dependencies``; ``blocks`` is the only
        reversed list here.)
        """
        task_id = args.get("task_id", "")
        if not task_id:
            return {"error": "task_id is required"}

        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}

        # Upstream: what this task depends on
        depends_on: list[dict] = []
        detailed_edges = await self.db.get_typed_dependencies_detailed(task.id)
        for edge in detailed_edges:
            if edge["dep_type"] not in BLOCKING_DEP_TYPES:
                continue
            dep_id = edge["depends_on_task_id"]
            dep_task = await self.db.get_task(dep_id)
            if dep_task:
                depends_on.append(
                    {
                        "id": dep_task.id,
                        "title": dep_task.title,
                        "status": dep_task.status.value,
                        "dep_type": edge["dep_type"],
                        "reason": edge["description"],
                    }
                )

        # Downstream: what this task blocks
        dependent_ids = await self.db.get_dependents(task.id)
        blocks: list[dict] = []
        for dep_id in sorted(dependent_ids):
            dep_task = await self.db.get_task(dep_id)
            if dep_task:
                blocks.append(
                    {
                        "id": dep_task.id,
                        "title": dep_task.title,
                        "status": dep_task.status.value,
                    }
                )

        # Provenance: every non-blocking outgoing edge — today that is
        # ``discovered-from``, written when a worker files work mid-task.
        provenance: list[dict] = []
        for edge in detailed_edges:
            dep_id = edge["depends_on_task_id"]
            dep_type = edge["dep_type"]
            if dep_type in BLOCKING_DEP_TYPES:
                continue
            origin = await self.db.get_task(dep_id)
            if origin is None:
                continue
            provenance.append(
                {
                    "id": origin.id,
                    "title": origin.title,
                    "status": origin.status.value,
                    "dep_type": dep_type,
                    "reason": edge["description"],
                }
            )

        return {
            "task_id": task.id,
            "title": task.title,
            "status": task.status.value,
            "depends_on": depends_on,
            "blocks": blocks,
            "provenance": provenance,
        }

    async def _cmd_get_task_dependencies(self, args: dict) -> dict:
        """Alias for ``_cmd_task_deps`` — used by the Supervisor tool.

        The ``/task-deps`` slash command uses ``task_deps`` while the
        Supervisor exposes the same data as ``get_task_dependencies``.
        Both route through the same logic.
        """
        return await self._cmd_task_deps(args)

    async def _cmd_add_dependency(self, args: dict) -> dict:
        """Add a typed dependency edge: *task_id* depends on *depends_on*.

        ``dep_type`` defaults to ``blocks``.  Blocking kinds go through cycle
        detection (and, for ``waits-for``, the descendant-deadlock rule);
        provenance kinds skip the DAG check but still reject self-edges
        (work-graph design §11).  The duplicate check is per (pair, type), so
        one pair can carry e.g. ``blocks`` + ``discovered-from``.
        """
        task_id = args.get("task_id", "")
        depends_on = args.get("depends_on", "")
        if not task_id:
            return {"error": "task_id is required"}
        if not depends_on:
            return {"error": "depends_on is required"}
        if task_id == depends_on:
            return {"error": "A task cannot depend on itself"}

        dep_type = args.get("dep_type") or DepType.BLOCKS.value
        reason = str(args.get("reason") or "").strip() or None
        if dep_type not in DEP_TYPE_VALUES:
            return {
                "error": f"Invalid dep_type '{dep_type}'. "
                f"Allowed: {', '.join(sorted(DEP_TYPE_VALUES))}"
            }

        # Verify both tasks exist.
        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}
        dep_task = await self.db.get_task(depends_on)
        if not dep_task:
            return {"error": f"Dependency task '{depends_on}' not found"}

        # Check for a duplicate edge *of this type*.
        existing = await self.db.get_typed_dependencies(task_id)
        if (depends_on, dep_type) in existing:
            return {
                "error": (
                    f"Dependency already exists: '{task_id}' already has a "
                    f"'{dep_type}' edge to '{depends_on}'"
                )
            }

        if dep_type in BLOCKING_DEP_TYPES:
            # Cycle detection over the blocking subgraph only.
            all_deps = await self.db.get_all_dependencies()
            try:
                validate_dag_with_new_edge(all_deps, task_id, depends_on, dep_type)
            except CyclicDependencyError as exc:
                return {"error": f"Cannot add dependency: {exc}"}

        if dep_type == DepType.WAITS_FOR.value:
            # A waiter that is itself a descendant of the container fans in
            # over a set containing itself — never satisfiable.
            pc_edges = await self.db.get_parent_child_edges()
            try:
                validate_waits_for(pc_edges, task_id, depends_on)
            except CyclicDependencyError as exc:
                return {
                    "error": (
                        f"Cannot add waits-for dependency: '{task_id}' is a child of "
                        f"'{depends_on}' and would fan in over itself ({exc})"
                    )
                }

        try:
            await self.db.add_dependency(
                task_id, depends_on, dep_type, description=reason
            )
        except HierarchyError as exc:
            return {"error": f"hierarchy.{exc.code}: {exc.detail}", "code": f"hierarchy.{exc.code}"}
        await self.db.log_event(
            "dependency.added",
            project_id=task.project_id,
            task_id=task_id,
            payload=f"{dep_type} -> {depends_on}",
        )
        await self._emit_task_graph_change("task.updated", task)

        return {
            "ok": True,
            "task_id": task_id,
            "depends_on": depends_on,
            "dep_type": dep_type,
            "reason": reason,
            "task_title": task.title,
            "depends_on_title": dep_task.title,
        }

    async def _cmd_remove_dependency(self, args: dict) -> dict:
        """Remove a dependency edge: *task_id* no longer depends on *depends_on*.

        ``dep_type`` is optional — omitted, every edge kind between the pair
        is removed.  Returns a confirmation dict.
        """
        task_id = args.get("task_id", "")
        depends_on = args.get("depends_on", "")
        if not task_id:
            return {"error": "task_id is required"}
        if not depends_on:
            return {"error": "depends_on is required"}

        dep_type = args.get("dep_type") or None
        if dep_type is not None and dep_type not in DEP_TYPE_VALUES:
            return {
                "error": f"Invalid dep_type '{dep_type}'. "
                f"Allowed: {', '.join(sorted(DEP_TYPE_VALUES))}"
            }

        # Verify the task exists (the dependency target need not still exist).
        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}

        # Check that a matching edge actually exists.
        existing = await self.db.get_typed_dependencies(task_id)
        matching = [
            edge_type
            for target, edge_type in existing
            if target == depends_on and (dep_type is None or edge_type == dep_type)
        ]
        if not matching:
            suffix = f" with dep_type '{dep_type}'" if dep_type else ""
            return {
                "error": (
                    f"No dependency found: '{task_id}' does not depend on '{depends_on}'{suffix}"
                )
            }

        await self.db.remove_dependency(task_id, depends_on, dep_type)
        await self.db.log_event(
            "dependency.removed",
            project_id=task.project_id,
            task_id=task_id,
            payload=f"{','.join(sorted(matching))} -> {depends_on}",
        )
        await self._emit_task_graph_change("task.updated", task)

        return {
            "ok": True,
            "task_id": task_id,
            "removed_dependency": depends_on,
            "removed_dep_types": sorted(matching),
            "task_title": task.title,
        }

    async def _cmd_edit_task(self, args: dict) -> dict:
        if args.get("status") == "PAUSED":
            return {"error": "Use pause_task to pause safely and stop any running session."}
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}

        if "status" in args and task.status == TaskStatus.PAUSED and task.resume_after is None:
            return {"error": "Task is manually paused; use resume_task."}

        routing_fields = {"profile_id", "intelligence_class"} & args.keys()
        if routing_fields and (task.status == TaskStatus.IN_PROGRESS or task.assigned_agent_id):
            return {"error": "Task is running or claimed; stop the task before changing its routing."}

        VERIFICATION_VALUES = frozenset(v.value for v in VerificationType)

        # Handle status change separately — uses transition_task for logging
        status_changed = False
        if "status" in args:
            new_status_raw = args["status"]
            try:
                new_status = TaskStatus(new_status_raw)
            except ValueError:
                valid = ", ".join(s.value for s in TaskStatus)
                return {"error": f"Invalid status '{new_status_raw}'. Valid: {valid}"}
            old_status = task.status.value
            status_changed = True

        updates = {}
        if "project_id" in args:
            new_pid = args["project_id"]
            project = await self.db.get_project(new_pid)
            if not project:
                return {"error": f"Project '{new_pid}' not found"}
            updates["project_id"] = new_pid
        if "title" in args:
            updates["title"] = args["title"]
        if "description" in args:
            updates["description"] = args["description"]
        if "priority" in args:
            updates["priority"] = args["priority"]
        if "task_type" in args:
            raw_tt = args["task_type"]
            if raw_tt is None:
                updates["task_type"] = None  # allow clearing task_type
            elif raw_tt in TASK_TYPE_VALUES:
                updates["task_type"] = TaskType(raw_tt)
            else:
                return {
                    "error": f"Invalid task_type '{raw_tt}'. Allowed: {', '.join(sorted(TASK_TYPE_VALUES))}"
                }
        if "max_retries" in args:
            updates["max_retries"] = args["max_retries"]
        if "verification_type" in args:
            raw_vt = args["verification_type"]
            if raw_vt in VERIFICATION_VALUES:
                updates["verification_type"] = VerificationType(raw_vt)
            else:
                return {
                    "error": f"Invalid verification_type '{raw_vt}'. Allowed: {', '.join(sorted(VERIFICATION_VALUES))}"
                }
        if "profile_id" in args:
            pid = args["profile_id"]
            if pid is not None:
                profile = await self.db.get_profile(pid)
                if not profile:
                    return {"error": f"Profile '{pid}' not found"}
            updates["profile_id"] = pid  # None clears the profile
        if "intelligence_class" in args:
            updates["intelligence_class"] = args["intelligence_class"]
        if routing_fields:
            routed_profile_id = updates.get("profile_id", task.profile_id)
            routed_profile = await self.db.get_profile(routed_profile_id) if routed_profile_id else None
            class_error = self._validate_routing_class(
                updates.get("intelligence_class", task.intelligence_class), routed_profile
            )
            if class_error:
                return {"error": class_error}
        if "integration_mode" in args:
            mode = args["integration_mode"]
            if mode is not None and mode not in INTEGRATION_MODES:
                return {
                    "error": f"Invalid integration_mode '{mode}'. "
                    f"Allowed: {', '.join(sorted(INTEGRATION_MODES))} "
                    "(null clears the override — the task inherits the "
                    "project/system policy)"
                }
            updates["integration_mode"] = mode  # None clears the override
        if "skip_verification" in args:
            updates["skip_verification"] = bool(args["skip_verification"])
        if "workflow_id" in args:
            updates["workflow_id"] = args["workflow_id"]  # None clears the workflow
        if "affinity_agent_id" in args:
            val = args["affinity_agent_id"]
            if val is not None:
                agent = await self.db.get_agent(val)
                if not agent:
                    return {"error": f"Agent '{val}' not found for affinity"}
            updates["affinity_agent_id"] = val  # None clears affinity
        if "affinity_reason" in args:
            val = args["affinity_reason"]
            valid_reasons = {"context", "workspace", "type"}
            if val is not None and val not in valid_reasons:
                return {
                    "error": f"Invalid affinity_reason '{val}'. "
                    f"Allowed: {', '.join(sorted(valid_reasons))}"
                }
            updates["affinity_reason"] = val  # None clears affinity_reason
        if "workspace_mode" in args:
            val = args["workspace_mode"]
            if val is None:
                updates["workspace_mode"] = None
            elif val in WORKSPACE_MODE_VALUES:
                updates["workspace_mode"] = WorkspaceMode(val)
            else:
                return {
                    "error": f"Invalid workspace_mode '{val}'. "
                    f"Allowed: {', '.join(sorted(WORKSPACE_MODE_VALUES))}"
                }

        if routing_fields:
            updated = await self.db.update_task_routing(
                task.id,
                profile_id=updates.get("profile_id", task.profile_id),
                intelligence_class=updates.get("intelligence_class", task.intelligence_class),
                preferred_workspace_id=None,
                clear_intelligence_class=(
                    "intelligence_class" in updates and updates["intelligence_class"] is None
                ),
            )
            if not updated:
                return {"error": "Task is running or claimed; stop the task before changing its routing."}
        other_updates = {key: value for key, value in updates.items() if key not in routing_fields}
        if other_updates:
            await self.db.update_task(args["task_id"], **other_updates)
        if status_changed:
            await self.db.transition_task(args["task_id"], new_status, context="edit_task")

        needs_attention_cleared = (
            args.get("needs_attention") is not None and args["needs_attention"].strip() == ""
        )
        if (
            args.get("clear_needs_attention")
            or needs_attention_cleared
            or (status_changed and args.get("needs_attention") is None)
        ):
            # An explicit operator status change is a recovery decision, so
            # it dismisses the stale operational signal too. An empty (or
            # whitespace-only) needs_attention value is likewise treated as
            # a clear, not a stored empty code, so presence-based reads
            # (monitoring.py's task_ids_with_meta filter) don't mistake it
            # for an unresolved signal.
            await self.db.delete_task_meta(args["task_id"], "needs_attention")
        elif args.get("needs_attention") is not None:
            await self.db.set_task_meta(
                args["task_id"], "needs_attention", args["needs_attention"]
            )

        all_fields = list(updates.keys())
        if status_changed:
            all_fields.append("status")
        if args.get("clear_needs_attention"):
            all_fields.append("clear_needs_attention")
        elif args.get("needs_attention") is not None:
            all_fields.append("needs_attention")

        if not all_fields:
            return {
                "error": (
                    "No fields to update. Provide project_id, title, description, priority, "
                    "task_type, status, max_retries, verification_type, profile_id, "
                    "integration_mode, skip_verification, intelligence_class, affinity_agent_id, "
                    "affinity_reason, workspace_mode, needs_attention, or clear_needs_attention."
                )
            }

        result = {"updated": args["task_id"], "fields": all_fields}
        if status_changed:
            result["old_status"] = old_status
            result["new_status"] = new_status_raw
        # Warn if directory-isolated is set — deferred feature
        if updates.get("workspace_mode") == WorkspaceMode.DIRECTORY_ISOLATED:
            result["warning"] = (
                "workspace_mode='directory-isolated' is accepted but not yet implemented. "
                "The task will fail at execution time. This mode is reserved for future "
                "monorepo support. Use 'exclusive' instead ('branch-isolated' is "
                "deprecated and now behaves identically to 'exclusive')."
            )
        await self._emit_task_graph_change("task.updated", task)
        if updates.get("project_id", task.project_id) != task.project_id:
            await self._emit_task_graph_change(
                "task.updated", task, project_id=updates["project_id"]
            )
        return result

    async def _task_control_scope_error(self, task_id: str) -> dict | None:
        scope = self._current_scope or {}
        if scope.get("kind") == "session" and not scope.get("elevated"):
            return {"error": "out of scope: task Pause/Resume requires an operator"}
        task = await self.db.get_task(task_id)
        if task is not None:
            return self._task_findings_scope_error(task)
        return None

    async def _cmd_pause_task(self, args: dict) -> dict:
        task_id = args["task_id"]
        error = await self._task_control_scope_error(task_id)
        if error:
            return error
        await self.orchestrator.pause_task(task_id)
        task = await self.db.get_task(task_id)
        await self._emit_task_graph_change("task.updated", task)
        return {"task_id": task_id, "status": task.status.value}

    async def _cmd_resume_task(self, args: dict) -> dict:
        task_id = args["task_id"]
        error = await self._task_control_scope_error(task_id)
        if error:
            return error
        task = await self.orchestrator.resume_task(task_id)
        await self._emit_task_graph_change("task.updated", task)
        return {"task_id": task_id, "status": task.status.value}

    async def _cmd_stop_task(self, args: dict) -> dict:
        error = await self.orchestrator.stop_task(args["task_id"])
        if error:
            return {"error": error}
        return {"stopped": args["task_id"]}

    async def _cmd_task_recover(self, args: dict) -> dict:
        task_id = args.get("task_id")
        error = await self._task_control_scope_error(task_id)
        if error:
            return error
        decision = args.get("decision")
        reason = args.get("reason")
        if decision not in ("retry", "hold") or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 4000:
            return {"error": "Provide decision retry|hold and a reason of 1 to 4000 characters"}
        scope = self._current_scope or {}
        stopped_session = None
        if decision == "retry":
            from src.sessions.provider import SessionHandle

            incident = await self.db.get_task_meta(task_id, "supervisor_recovery_incident") or {}
            if incident.get("id") != args.get("incident_id"):
                return {"error": "Incident is stale or not found"}
            row = await self.db.get_session(incident["session_id"])
            if row is None:
                return {"error": "Cannot confirm the old worker has stopped; operator review required"}
            try:
                provider = self.orchestrator.session_providers.create(row.provider, self.config)
                stopped = await asyncio.wait_for(provider.confirm_stopped(
                    SessionHandle(row.name, row.provider, row.instance_token)
                ), timeout=10)
            except Exception:
                return {"error": "Worker liveness check unavailable; recovery was not accepted"}
            if stopped is not True:
                return {"error": "The old worker may still be running; recovery was not accepted"}
            stopped_session = {"id": row.id, "instance_token": row.instance_token}
        result = await self.db.decide_task_recovery(
            task_id, args.get("incident_id"), decision, reason.strip(),
            author_kind="supervisor" if scope.get("kind") == "session" else "user",
            author_id=scope.get("session_id") or "operator",
            project_id=scope.get("project_id"), stopped_session=stopped_session,
        )
        await self._emit_task_graph_change("task.updated", await self.db.get_task(task_id))
        return result

    async def _cmd_restart_task(self, args: dict) -> dict:
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}
        if task.status == TaskStatus.IN_PROGRESS:
            return {"error": "Task is currently in progress. Stop it first."}
        old_status = task.status.value
        await self.db.transition_task(
            args["task_id"],
            TaskStatus.READY,
            context="restart_task",
            retry_count=0,
            assigned_agent_id=None,
        )
        return {
            "restarted": args["task_id"],
            "title": task.title,
            "previous_status": old_status,
        }

    async def _cmd_reopen_with_feedback(self, args: dict) -> dict:
        """Reopen a completed/failed task with feedback appended to its description.

        Used when a completed or failed task needs to be retried because issues
        were found.  The feedback is appended to the task description so the
        agent sees it on re-execution, stored as a structured task_context
        entry for programmatic access, and the task is reset to READY.

        The PR URL is cleared so the agent can create a fresh PR on the next
        execution, and retry_count is reset to 0.

        Required args: task_id, feedback (the feedback text).
        """
        task_id = args.get("task_id")
        feedback = args.get("feedback", "").strip()
        if not task_id:
            return {"error": "task_id is required"}
        if not feedback:
            return {"error": "feedback text is required"}

        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}
        if task.status == TaskStatus.IN_PROGRESS:
            return {"error": "Task is currently in progress. Stop it first."}

        review_evidence_snapshot = None
        caller_scope = self._current_scope or {}
        if caller_scope.get("kind") == "session":
            review_task_id = caller_scope.get("task_id")
            session_id = caller_scope.get("session_id")
            review_task = await self.db.get_task(review_task_id) if review_task_id else None
            session = await self.db.get_session(session_id) if session_id else None
            if review_task is not None and review_task.profile_id in REVIEW_PROFILE_IDS:
                from src.database.queries.hierarchy_queries import HierarchyError
                from src.integration.review_evidence import ReviewEvidenceProducer

                try:
                    review_evidence_snapshot = await ReviewEvidenceProducer(
                        self.db, self._integration_promotion_service()
                    ).snapshot(
                        review_task,
                        session,
                        verdict="rejected",
                        feedback=feedback,
                        requested_subject_id=task_id,
                    )
                except HierarchyError as exc:
                    return {"error": f"integration review evidence refused: {exc}"}

        old_status = task.status.value

        # Append feedback to the task description so the agent sees it
        # when the task is re-executed.
        separator = "\n\n---\n**Reopen Feedback:**\n"
        updated_description = task.description + separator + feedback

        # ``integration_mode`` is a persisted column the transition does not
        # touch, so a reopened pull_request-mode task re-creates its PR on
        # the next completion.
        transition_values = {
            "context": "reopen_with_feedback",
            "description": updated_description,
            "retry_count": 0,
            "assigned_agent_id": None,
            "pr_url": None,
        }
        if review_evidence_snapshot is None:
            await self.db.transition_task(task_id, TaskStatus.READY, **transition_values)
        else:
            from src.integration.review_evidence import ReviewEvidenceProducer

            async with self.db.immediate() as conn:
                transition = await ReviewEvidenceProducer(
                    self.db, None
                ).reject_and_reopen_on(
                    conn,
                    task_id,
                    review_evidence_snapshot["reviewer_task_id"],
                    review_evidence_snapshot,
                    **transition_values,
                )
            await self.db.log_blocked_flips(transition.flipped)
            await self.db._notify_settled(transition.settled)
            await self.db._notify_ready(transition.ready)

        # Store feedback as a structured task_context entry so agents and
        # tooling can access individual reopen comments programmatically.
        await self.db.add_task_context(
            task_id,
            type="reopen_feedback",
            label="Reopen Feedback",
            content=feedback,
        )

        await self.db.log_event(
            "reopen_with_feedback",
            project_id=task.project_id,
            task_id=task_id,
            payload=feedback[:500],
        )

        # Cancel stale open reviews of this task (Dv2 Phase 2 rework loop).
        # A review with a ``discovered-from`` edge pointing at the reopened
        # task is by construction gating downstream on THIS task's now-stale
        # completion.  Transition every such review that is not already
        # terminal to FAILED with a distinct context — the sweep resolves
        # any ``task`` gates awaiting it (see _sweep_resolve_task_gates).
        try:
            candidates = await self.db.list_tasks(project_id=task.project_id)
        except Exception:
            candidates = []
        terminal = {"COMPLETED", "FAILED", "BLOCKED"}
        review_profile_ids = REVIEW_PROFILE_IDS
        # The review that is *doing* the rejecting is the one review here that
        # is not stale: it just produced this verdict, and the reviewer profile
        # is documented to call ``task_close(success)`` on it next.  Cancelling
        # it out from under its own live session is what made the documented
        # reject path unusable even once scope allowed the call.
        caller_scope = self._current_scope or {}
        caller_task_id = (
            caller_scope.get("task_id") if caller_scope.get("kind") == "session" else None
        )
        cancelled_reviews: list[str] = []
        for cand in candidates:
            status_val = getattr(cand.status, "value", cand.status)
            if status_val in terminal:
                continue
            if caller_task_id and cand.id == caller_task_id:
                continue
            if cand.profile_id not in review_profile_ids:
                # Defense-in-depth: only cascade-cancel review producers.
                # Non-review tasks that happen to carry a discovered-from
                # edge to the reopened task must not be cancelled.
                continue
            try:
                edges = await self.db.get_typed_dependencies(cand.id)
            except Exception:
                edges = []
            if any(
                dep_id == task_id and dep_type == "discovered-from" for dep_id, dep_type in edges
            ):
                try:
                    await self.db.transition_task(
                        cand.id,
                        TaskStatus.FAILED,
                        context="reopen_cascade:stale_review",
                    )
                    await self.db.log_event(
                        "task.transition",
                        project_id=task.project_id,
                        task_id=cand.id,
                        payload="reopen_cascade:stale_review",
                    )
                    cancelled_reviews.append(cand.id)
                except Exception:
                    logger.warning(
                        "reopen_cascade: failed to cancel stale review %s",
                        cand.id,
                        exc_info=True,
                    )

        return {
            "reopened": task_id,
            "title": task.title,
            "previous_status": old_status,
            "status": "READY",
            "feedback_added": True,
            "integration_mode": task.integration_mode,
            "cancelled_reviews": cancelled_reviews,
        }

    async def _cmd_delete_task(self, args: dict) -> dict:
        task_id = args["task_id"]
        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}
        if task.status == TaskStatus.IN_PROGRESS:
            error = await self.orchestrator.stop_task(task_id)
            if error:
                return {"error": f"Could not stop task before deleting: {error}"}
        cascade = bool(args.get("cascade", False))

        if cascade:
            # A cascade delete removes the whole subtree; refuse rather than
            # pull a live session out from under a grandchild (spec §7).
            # Check and delete in the same transaction so no session can
            # start holding a descendant between the check and the delete.
            # Both the refusal and the HierarchyError are handled OUTSIDE the
            # transaction: returning from inside ``async with`` would commit
            # it, and swallowing the error inside would commit a half-done
            # cascade instead of rolling it back.
            live: list = []
            result = None
            try:
                async with self.db.immediate() as conn:
                    live = await self.db.live_descendant_sessions(task_id, conn=conn)
                    if not live:
                        result = await self.db.delete_task(task_id, cascade=True, conn=conn)
            except HierarchyError as exc:
                return {
                    "error": f"hierarchy.{exc.code}: {exc.detail}",
                    "code": f"hierarchy.{exc.code}",
                }
            if live:
                return {
                    "success": False,
                    "code": "hierarchy.live_descendants",
                    "error": (
                        f"Cannot delete task '{task_id}': "
                        f"{len(live)} live descendant session(s) must stop first"
                    ),
                    "sessions": [{"session_id": s, "task_id": t} for s, t in live],
                }
            # Post-commit, same sequencing as delete_task's own single-
            # transaction path — a listener failure must not roll back the
            # delete.
            await self.db.log_blocked_flips(result.flipped)
            await self.db._notify_settled(result.settled)
            await self.db._notify_ready(result.ready)
        else:
            try:
                await self.db.delete_task(task_id, cascade=False)
            except HierarchyError as exc:
                return {
                    "error": f"hierarchy.{exc.code}: {exc.detail}",
                    "code": f"hierarchy.{exc.code}",
                }
        await self._emit_task_graph_change("task.deleted", task)
        return {"deleted": task_id, "title": task.title}

    # -- Archive commands -----------------------------------------------------
    # Archive moves completed tasks out of the active view into the
    # ``archived_tasks`` DB table and writes markdown notes to
    # ``~/.agent-queue/archived_tasks/<project_id>/``.  Tasks can be listed,
    # inspected, restored, or permanently deleted from the archive.

    async def _cmd_archive_task(self, args: dict) -> dict:
        """Archive tasks — single task by ID or bulk by project.

        **Single mode** (``task_id``): archives one task.  Must be in a
        terminal status (COMPLETED, FAILED, or BLOCKED).

        **Bulk mode** (``project_id``): archives all completed tasks in a
        project.  Set ``include_failed=True`` to also archive FAILED and
        BLOCKED tasks.

        Parameters
        ----------
        args : dict
            ``task_id`` – archive a single task by ID.
            ``project_id`` – bulk-archive completed tasks in this project.
            ``include_failed`` – (bulk only) also archive FAILED/BLOCKED.
        """
        task_id = args.get("task_id")
        project_id = args.get("project_id")

        if task_id:
            # --- Single-task mode ---
            task = await self.db.get_task(task_id)
            if not task:
                return {"error": f"Task '{task_id}' not found"}

            terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED}
            if task.status not in terminal:
                return {
                    "error": (
                        f"Cannot archive task in {task.status.value} status. "
                        "Only COMPLETED, FAILED, or BLOCKED tasks can be archived."
                    ),
                }

            result = await self.db.get_task_result(task_id)
            deps = await self.db.get_dependencies(task_id)
            await self._write_archive_note(task, result, deps)

            try:
                success = await self.db.archive_task(task_id)
            except HierarchyError as exc:
                return {
                    "error": f"hierarchy.{exc.code}: {exc.detail}",
                    "code": f"hierarchy.{exc.code}",
                }
            if not success:
                return {"error": f"Failed to archive task '{task_id}'"}

            await self.db.log_event(
                "task_archived",
                project_id=task.project_id,
                task_id=task_id,
            )
            await self._emit_task_graph_change("task.archived", task)
            return {
                "archived": task_id,
                "title": task.title,
                "status": task.status.value,
            }

        if not project_id and not task_id:
            return {"error": "Provide task_id or project_id."}

        # --- Bulk mode ---
        include_failed = args.get("include_failed", False)
        statuses_to_archive = [TaskStatus.COMPLETED]
        if include_failed:
            statuses_to_archive.extend([TaskStatus.FAILED, TaskStatus.BLOCKED])

        tasks_to_archive: list = []
        for status in statuses_to_archive:
            tasks_to_archive.extend(await self.db.list_tasks(project_id=project_id, status=status))

        if not tasks_to_archive:
            scope = f" in project `{project_id}`" if project_id else ""
            return {"message": f"No completed tasks to archive{scope}."}

        # Phase 1 — gather results and dependencies before any deletions.
        task_data: list[tuple] = []
        for task in tasks_to_archive:
            result = await self.db.get_task_result(task.id)
            deps = await self.db.get_dependencies(task.id)
            task_data.append((task, result, deps))

        # Phase 2 — archive each task (DB table + optional markdown note).
        #
        # A bulk selection lists every terminal task individually, but
        # ``archive_task`` archives a whole subtree atomically: a root with
        # an open descendant raises (skip it, don't abort the batch), and a
        # task already swept up as part of an earlier root's subtree comes
        # back ``False`` (skip it too — no second event, no double count).
        archived: list[dict] = []
        skipped: list[dict] = []
        for task, result, deps in task_data:
            try:
                success = await self.db.archive_task(task.id)
            except HierarchyError as exc:
                skipped.append(
                    {
                        "task_id": task.id,
                        "code": f"hierarchy.{exc.code}",
                        "detail": exc.detail,
                    }
                )
                continue
            if not success:
                continue

            archive_path = await self._write_archive_note(task, result, deps)
            await self.db.log_event(
                "task_archived",
                project_id=task.project_id,
                task_id=task.id,
            )
            await self._emit_task_graph_change("task.archived", task)
            archived.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "status": task.status.value,
                    "archive_path": archive_path,
                }
            )

        archive_dir = None
        for entry in archived:
            if entry["archive_path"]:
                archive_dir = os.path.dirname(entry["archive_path"])
                break

        return {
            "archived_count": len(archived),
            "archived_ids": [a["id"] for a in archived],
            "archived": archived,
            "skipped": skipped,
            "archive_dir": archive_dir,
            "project_id": project_id,
        }

    async def _cmd_list_archived(self, args: dict) -> dict:
        """List archived tasks, optionally scoped to a project.

        Parameters
        ----------
        args : dict
            ``project_id`` – optional project scope.
            ``limit`` – max number of results (default 50).
        """
        project_id = args.get("project_id")
        limit = int(args.get("limit", 50))
        tasks = await self.db.list_archived_tasks(
            project_id=project_id,
            limit=limit,
        )
        total = await self.db.count_archived_tasks(project_id=project_id)
        return {
            "tasks": tasks,
            "count": len(tasks),
            "total": total,
            "project_id": project_id,
        }

    async def _cmd_archive_settings(self, args: dict) -> dict:
        """Return the current auto-archive configuration.

        Reads from ``config.archive`` and includes the count of currently
        archived tasks and how many terminal tasks are eligible right now.
        """
        cfg = self.config.archive
        archived_count = await self.db.count_archived_tasks()

        # Count how many active terminal tasks would be archived now
        older_than_seconds = cfg.after_hours * 3600
        import time as _time

        cutoff = _time.time() - older_than_seconds
        eligible = 0
        if cfg.enabled and cfg.statuses:
            for status in cfg.statuses:
                tasks = await self.db.list_tasks(status=TaskStatus(status))
                eligible += sum(1 for t in tasks if t.updated_at and t.updated_at <= cutoff)

        return {
            "enabled": cfg.enabled,
            "after_hours": cfg.after_hours,
            "statuses": cfg.statuses,
            "archived_count": archived_count,
            "eligible_count": eligible,
        }

    async def _cmd_provide_input(self, args: dict) -> dict:
        """Provide a human reply to an agent question (WAITING_INPUT → READY).

        The agent's question is answered by appending the human's response to the
        task description so the agent sees it on re-execution.  The task is
        transitioned to READY so the scheduler picks it up in the next cycle.

        Required args: task_id, input (the human's response text).
        """
        task_id = args.get("task_id")
        input_text = args.get("input", "").strip()
        if not task_id:
            return {"error": "task_id is required"}
        if not input_text:
            return {"error": "input text is required"}

        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}
        if task.status != TaskStatus.WAITING_INPUT:
            return {"error": f"Task is not waiting for input (status: {task.status.value})"}

        # Append the human reply to the task description so the agent sees it
        # when the task is re-executed.
        separator = "\n\n---\n**Human Reply:**\n"
        updated_description = task.description + separator + input_text
        await self.db.update_task(task_id, description=updated_description)

        # Transition WAITING_INPUT → READY so the scheduler re-runs the task.
        await self.db.transition_task(
            task_id,
            TaskStatus.READY,
            context="human_replied",
        )
        await self.db.log_event(
            "human_replied",
            project_id=task.project_id,
            task_id=task_id,
            payload=input_text[:500],
        )
        return {
            "task_id": task_id,
            "title": task.title,
            "status": "READY",
        }

    async def _cmd_set_task_status(self, args: dict) -> dict:
        if args.get("status") == "PAUSED":
            return {"error": "Use pause_task to pause safely and stop any running session."}
        task_id = args["task_id"]
        new_status = args["status"]
        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}
        old_status = task.status.value
        if new_status == TaskStatus.COMPLETED.value:
            open_children = await self.db.open_children(task_id)
            if open_children:
                return {
                    "error": f"task {task_id} has open children: {', '.join(open_children)}",
                    "code": "hierarchy.open_children",
                    "open_children": open_children,
                }
        await self.db.transition_task(task_id, TaskStatus(new_status), context="admin_set_status")
        return {
            "task_id": task_id,
            "old_status": old_status,
            "new_status": new_status,
            "title": task.title,
        }

    async def _cmd_skip_task(self, args: dict) -> dict:
        """Skip a BLOCKED/FAILED task to unblock its dependency chain."""
        error, unblocked = await self.orchestrator.skip_task(args["task_id"])
        if error:
            return {"error": error}
        return {
            "skipped": args["task_id"],
            "unblocked_count": len(unblocked),
            "unblocked": [{"id": t.id, "title": t.title} for t in unblocked],
        }

    async def _write_archive_note(
        self,
        task,
        result: dict | None,
        dependencies: set[str],
    ) -> str | None:
        """Write a task summary note to the vault.

        Notes are stored under ``{vault}/projects/{project_id}/tasks/{category}/``.
        Returns the file path if written, or ``None`` if the project could not
        be resolved or the summary already exists.
        """
        project = await self.db.get_project(task.project_id)
        if not project:
            return None

        return write_task_summary(self.config.vault_root, task, result, dependencies)

    async def _cmd_get_chain_health(self, args: dict) -> dict:
        """Check dependency chain health for a task or project."""
        task_id = args.get("task_id")
        project_id = args.get("project_id")

        if task_id:
            task = await self.db.get_task(task_id)
            if not task:
                return {"error": f"Task '{task_id}' not found"}
            if task.status != TaskStatus.BLOCKED:
                return {
                    "task_id": task_id,
                    "status": task.status.value,
                    "stuck_downstream": [],
                    "message": "Task is not blocked — no stuck chain.",
                }
            stuck = await self.orchestrator._find_stuck_downstream(task_id)
            return {
                "task_id": task_id,
                "title": task.title,
                "status": task.status.value,
                "stuck_downstream": [
                    {"id": t.id, "title": t.title, "status": t.status.value} for t in stuck
                ],
                "stuck_count": len(stuck),
            }

        # If project_id given (or fall back to active), list all blocked tasks
        # with stuck chains.
        pid = project_id or self._active_project_id
        blocked_tasks = await self.db.list_tasks(project_id=pid, status=TaskStatus.BLOCKED)
        chains = []
        for bt in blocked_tasks:
            stuck = await self.orchestrator._find_stuck_downstream(bt.id)
            if stuck:
                chains.append(
                    {
                        "blocked_task": {"id": bt.id, "title": bt.title},
                        "stuck_downstream": [{"id": t.id, "title": t.title} for t in stuck],
                        "stuck_count": len(stuck),
                    }
                )
        return {
            "project_id": pid,
            "stuck_chains": chains,
            "total_stuck_chains": len(chains),
        }

    async def _cmd_get_task_result(self, args: dict) -> dict:
        result = await self.db.get_task_result(args["task_id"])
        if not result:
            return {"error": f"No results found for task '{args['task_id']}'"}
        return result

    async def _cmd_get_agent_error(self, args: dict) -> dict:
        task_id = args["task_id"]
        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}

        result = await self.db.get_task_result(task_id)

        info = {
            "task_id": task_id,
            "title": task.title,
            "status": task.status.value,
            "retries": f"{task.retry_count} / {task.max_retries}",
        }

        if not result:
            info["message"] = "No result recorded yet for this task"
            return info

        result_value = result.get("result", "unknown")
        error_msg = result.get("error_message") or ""
        error_type, suggestion = classify_error(error_msg)

        info["result"] = result_value
        info["error_type"] = error_type
        info["error_message"] = error_msg[:2000] if error_msg else None
        info["suggested_fix"] = suggestion
        summary = result.get("summary") or ""
        if summary:
            info["agent_summary"] = summary[:1000]

        return info

    # -- WG-4: explain + ready frontier ------------------------------------

    async def _cmd_explain_task(self, args: dict) -> dict:
        """Return the ordered list of reasons *task_id* isn't running.

        Graph reasons first (persistent blockers), then capacity reasons
        (transient — from the last scheduler tick's cached snapshot).
        Cross-project blocking deps name the other project in ``detail``.
        See docs/specs/design/work-graph.md §9.
        """
        from src.explain import Reason, build_capacity_reasons

        task_id = args.get("task_id")
        if not task_id:
            return {"success": False, "error": "task_id is required"}
        task = await self.db.get_task(str(task_id))
        if task is None:
            return {"success": False, "error": f"task '{task_id}' not found"}

        out_of_scope = self._assert_task_in_scope(task)
        if out_of_scope:
            return out_of_scope
        reasons: list[Reason] = []
        needs_attention = await self.db.get_task_meta(str(task_id), "needs_attention")
        if needs_attention:
            reasons.append(Reason(
                code="needs_attention", detail=str(needs_attention), ref=str(task_id),
            ))
        # A terminal close (hard failure, retry budget spent, pipeline stop,
        # timeout, operator stop).  The promotion cascade deliberately skips
        # it even once the graph is clear; only a restart/reopen brings it
        # back, so say so rather than answer with an empty graph.
        blocked_terminal = await self.db.get_task_meta(str(task_id), TERMINAL_BLOCKED_META_KEY)
        if blocked_terminal:
            reasons.append(Reason(
                code="blocked_terminal",
                detail=f"{blocked_terminal}; not auto-recovered, restart or reopen to retry",
                ref=str(task_id),
            ))

        # A cooling-down PAUSED task is not blocked by anything in the graph
        # — it is waiting out a backoff (rate limit, rapid crash, stalled
        # restart, session exit without close).  Without this the answer to
        # "why isn't X running" was silence for the whole cooldown.
        if task.status is TaskStatus.PAUSED:
            if task.resume_after:
                remaining = max(0.0, float(task.resume_after) - time.time())
                reasons.append(Reason(
                    code="paused_backoff",
                    detail=(
                        f"paused until {_fmt_epoch(task.resume_after)} "
                        f"({remaining:.0f}s remaining); resumes automatically"
                    ),
                    ref=str(task_id),
                ))
            else:
                reasons.append(Reason(
                    code="paused_manually",
                    detail="paused with no resume time; resume with `aq task resume`",
                    ref=str(task_id),
                ))

        # 1. hold:* labels (task is deliberately withheld).
        try:
            labels = await self.db.get_task_labels(str(task_id))
        except Exception:
            labels = []
        for lbl in labels:
            if lbl.startswith("hold:"):
                reasons.append(Reason(code="held", detail=f"label '{lbl}' withholds task", ref=lbl))

        # 2. Blocking dependencies (open gates + typed edges).
        try:
            blockers = await self.db.get_blocking_dependencies(str(task_id))
        except Exception:
            blockers = []
        for dep_id, dep_title, dep_status, dep_type, dep_project in blockers:
            if dep_project and dep_project != task.project_id:
                detail = (
                    f"blocked by {dep_type} dep '{dep_id}' ({dep_title}) "
                    f"status={dep_status} in project '{dep_project}'"
                )
            else:
                detail = f"blocked by {dep_type} dep '{dep_id}' ({dep_title}) status={dep_status}"
            reasons.append(Reason(code="blocked_dependency", detail=detail, ref=dep_id))

        # 3. Open/expired gates attached to this task.
        try:
            gates = await self.db.get_gates_for_task(str(task_id))
        except Exception:
            gates = []
        for g in gates:
            if g["status"] != "resolved":
                reasons.append(
                    Reason(
                        code="blocked_gate",
                        detail=(
                            f"gate '{g['id']}' ({g['gate_type']}: {g['title']}) "
                            f"status={g['status']}"
                        ),
                        ref=g["id"],
                    )
                )

        # 4. Assignment route state. The coordinator uses the same resolver
        # as scheduling and pool claims, so this cannot disagree with actual
        # eligibility after an edit or option-catalog change.
        assignment_route = None
        route_reason = None
        coordinator = getattr(self.orchestrator, "assignment_routing", None)
        if coordinator is not None:
            try:
                assignment_route, route_reason = await coordinator.explain(task)
            except Exception as exc:
                route_reason = Reason(
                    code="assignment_playbook_unavailable",
                    detail=f"could not inspect assignment route: {exc}",
                    ref=task.project_id,
                )
            if route_reason is not None:
                reasons.append(Reason(**route_reason))

        # 5. Pool-routed work never reaches the push scheduler at all, so the
        # capacity reasons below (which describe *that* path) would answer a
        # question this task never asks. Say what it is actually waiting on.
        pool_reason = await self._pool_wait_reason(task)
        if pool_reason is not None:
            reasons.append(pool_reason)

        # 6. Capacity reasons — only relevant when a scheduler snapshot exists.
        state = getattr(self.orchestrator, "_last_scheduler_state", None)
        if state is not None:
            ws_counts = getattr(self.orchestrator, "_last_scheduler_workspace_counts", {})
            idle = getattr(self.orchestrator, "_last_scheduler_idle_by_project", {})
            capacity = build_capacity_reasons(task, state, ws_counts, idle)
            # The coordinator above already answered the route question with
            # the richer story (playbook running, retrying, misconfigured);
            # the scheduler snapshot only knows that no route was in it.
            if route_reason is not None:
                capacity = [
                    reason
                    for reason in capacity
                    if reason["code"] != "awaiting_intelligence_route"
                ]
            if pool_reason is not None:
                # A pool-routed task is not in the push queue, so the codes
                # that describe *that* queue's supply would send an operator
                # looking for an idle worker nothing is ever going to create.
                # The rest still bite: a paused project or an exhausted budget
                # fails ``_admission_reason`` on the claim itself, and no free
                # workspace is what starves ``_launch_pool_session``.
                capacity = [r for r in capacity if r["code"] not in _PUSH_ONLY_REASON_CODES]
            reasons.extend(capacity)

        return {
            "success": True,
            "reasons": reasons,
            "reason_codes": [reason["code"] for reason in reasons],
            "assignment_route": assignment_route,
        }

    async def _pool_wait_reason(self, task):
        """``awaiting_pool_session`` for a task routed to a ``lifecycle: pool`` profile.

        ``Orchestrator._schedule`` filters these tasks out and
        ``_is_session_routed`` refuses to push-launch them (swarm-work-model
        §11), so a pool-routed task in READY is never "waiting for an idle
        agent" — it is waiting for a pool worker to claim it, and the only
        things that can stop that are the swarm flag, a quarantined pool key,
        and pool bounds.  Without this ``aq task explain`` reported
        ``no_idle_agent`` and sent operators looking in the wrong place.

        Returns ``None`` for push-routed tasks (the overwhelmingly common
        case) after one cheap profile-id lookup, so the extra work only
        happens on installs that actually run pools.
        """
        import time

        from src.explain import Reason

        orchestrator = self.orchestrator
        if orchestrator is None or not hasattr(orchestrator, "_pool_profile_ids"):
            return None
        try:
            pool_ids = await orchestrator._pool_profile_ids(task.project_id)
        except Exception:
            return None
        if not pool_ids:
            return None
        profile_id = task.profile_id
        if not profile_id:
            project = await self.db.get_project(task.project_id)
            if project is None:
                return None
            profile_id = await orchestrator._effective_default_profile_id(project)
        if profile_id not in pool_ids:
            return None

        if not getattr(self.config.swarm, "enabled", True):
            # The same condition ``pools.disabled`` reports: both push gates
            # are lifecycle-only while ``_reconcile_pools`` is flag-gated, so
            # this task is neither pushed nor claimable.
            return Reason(
                code="awaiting_pool_session",
                detail=(
                    f"routed to pool profile '{profile_id}', but swarm.enabled is false — "
                    "no pool session will ever claim it"
                ),
                ref=profile_id,
            )

        until, quarantine_reason = orchestrator._pool_quarantine_state(
            task.project_id, profile_id, time.time()
        )
        if until:
            detail = (
                f"pool '{profile_id}' is quarantined for another "
                f"{int(until - time.time())}s"
            )
            if quarantine_reason:
                detail += f": {quarantine_reason}"
            return Reason(code="awaiting_pool_session", detail=detail, ref=profile_id)

        supply, _demand, bounds, _profiles, _caps, _projects = await orchestrator._measure_pools(
            {task.project_id}
        )
        from src.scheduler import PoolKey

        sup = supply.get(PoolKey(task.project_id, profile_id))
        if sup is None:
            return Reason(
                code="awaiting_pool_session",
                detail=f"routed to pool profile '{profile_id}', which has no pool in this project",
                ref=profile_id,
            )
        _lo, hi = bounds.get(PoolKey(task.project_id, profile_id), (0, None))
        live = sup.running_idle + sup.running_busy + sup.starting
        detail = (
            f"awaiting a '{profile_id}' pool session to claim it "
            f"({sup.running_busy} busy, {sup.running_idle} idle, {sup.starting} starting"
            + (f", max_active={hi}" if hi is not None else "")
            + ")"
        )
        if hi is not None and live >= hi and sup.running_idle == 0:
            detail += " — the pool is at max_active with no idle worker"
        return Reason(code="awaiting_pool_session", detail=detail, ref=profile_id)

    async def _cmd_project_ready(self, args: dict) -> dict:
        """Ready frontier for a project + withheld tasks with reasons.

        Frontier excludes ``hold:*``-labeled tasks (design §6).  Withheld
        section lists DEFINED/BLOCKED tasks and the reasons keeping them
        out of the frontier.

        Args:
            profile_id: Restrict the frontier to tasks this profile would be
                offered.  Uses the same widening as the §10 work query
                (``select_ready_for_profile``): when *profile_id* is the
                project's ``default_profile_id``, unassigned tasks
                (``profile_id IS NULL``) count as its work too.
            brief: Project each ready task to
                ``id,title,status,priority,is_blocked,profile_id`` instead of
                the default ``task_id,title,priority`` shape.
        """
        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"success": False, "error": "project_id is required"}
        labels = args.get("labels")
        any_label = args.get("any_label")
        profile_id = args.get("profile_id")
        brief = bool(args.get("brief"))

        try:
            frontier = await self.db.get_ready_frontier(
                str(project_id), labels=labels, any_label=any_label
            )
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        if profile_id:
            project = await self.db.get_project(str(project_id))
            default_profile_id = getattr(project, "default_profile_id", None) if project else None
            frontier = [
                t
                for t in frontier
                if t.profile_id == profile_id
                or (t.profile_id is None and default_profile_id == profile_id)
            ]

        if brief:
            ready = [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status.value if hasattr(t.status, "value") else t.status,
                    "priority": t.priority,
                    "is_blocked": bool(t.is_blocked),
                    "profile_id": t.profile_id,
                }
                for t in frontier
            ]
        else:
            ready = [
                {
                    "task_id": t.id,
                    "title": t.title,
                    "priority": t.priority,
                }
                for t in frontier
            ]

        # Withheld: everything not READY-and-unblocked in DEFINED/BLOCKED.
        from src.models import TaskStatus

        withheld: list[dict] = []
        for status in (TaskStatus.DEFINED, TaskStatus.BLOCKED, TaskStatus.READY):
            for task in await self.db.list_tasks(project_id=str(project_id), status=status):
                if status == TaskStatus.READY and not task.is_blocked:
                    # Might still be withheld by a hold:* label.
                    lbls = await self.db.get_task_labels(task.id)
                    if not any(x.startswith("hold:") for x in lbls):
                        continue
                res = await self._cmd_explain_task({"task_id": task.id})
                if res.get("success") and res.get("reasons"):
                    withheld.append({"task_id": task.id, "reasons": res["reasons"]})

        return {"success": True, "ready": ready, "withheld": withheld}

    async def _cmd_ensure_task(self, args: dict) -> dict:
        """Find-or-create a task by (project_id, dedup_key).

        Returns ``{success, task_id, created}``. Non-terminal existing tasks
        with the same key are returned as-is; terminal tasks (COMPLETED,
        FAILED) are ignored so the key can be reused.

        ``profile_id`` and ``intelligence_class`` are create-time routing
        intent only: an existing task is returned untouched, so re-running the
        node never re-routes work that is already in flight.
        """
        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"success": False, "error": "project_id is required"}
        dedup_key = args.get("dedup_key")
        if not dedup_key:
            return {"success": False, "error": "dedup_key is required"}
        title = args.get("title")
        if not title:
            return {"success": False, "error": "title is required"}

        # Explicit route intent (routing design §2): a task's class comes from
        # explicit intent or a fresh assignment-playbook decision, never from a
        # profile default.  Pinning ``profile_id`` alone is therefore *not* a
        # route, so an ensuring pipeline that knows the class it wants must be
        # able to say so; dropping this argument left every ensured task
        # waiting on the router and, until it decided, refused at launch with
        # "awaiting intelligence route".
        intelligence_class = args.get("intelligence_class") or None

        if args.get("profile_id") == "triage" and dedup_key == "triage-open":
            from src.database.queries.triage_queries import ensure_triage_task

            if intelligence_class:
                class_error = self._validate_routing_class(
                    intelligence_class, await self.db.get_profile("triage")
                )
                if class_error:
                    return {"success": False, "error": class_error}
            result = await ensure_triage_task(
                self.db, str(project_id), title=str(title),
                description=str(args.get("description") or ""),
                priority=args.get("priority", 1),
                intelligence_class=intelligence_class,
            )
            if result.get("created") or result.get("restarted"):
                task = await self.db.get_task(result["task_id"])
                if task is not None:
                    await self._emit_task_graph_change("task.updated", task)
            return result

        # A review of a pipeline review is never work: refuse ``review:task:<X>``
        # when X itself carries a ``review:task:`` / ``branch-review:`` key.
        # The event-level guards (``no_code``, ``review_task`` at close and at
        # dispatch) do not reach a daemon running older code or a vault copy
        # of the pipeline whose rules predate them, and that is how
        # ``Review: Review: Review: ...`` chains ten deep reached the live
        # queue (task solid-harbor-68).  Every version of the review rules
        # routes ``on_failure`` to its terminal node, so a refusal ends the
        # run cleanly.  A reviewed id with no row is left alone: this only
        # narrows on a row the pipeline itself stamped.
        reviewed_id = reviewed_task_id(str(dedup_key))
        if reviewed_id is not None:
            reviewed = await self.db.get_task(reviewed_id)
            if reviewed is not None and is_review_completion(
                reviewed.dedup_key, reviewed.profile_id
            ):
                logger.info(
                    "ensure_task: refusing review of pipeline review task %s (dedup_key=%s)",
                    reviewed_id,
                    reviewed.dedup_key,
                )
                return {
                    "success": False,
                    "error": (
                        f"task '{reviewed_id}' is itself a pipeline review "
                        f"({reviewed.dedup_key}); reviews are not reviewed"
                    ),
                }

        existing = await self.db.find_task_by_dedup_key(str(project_id), str(dedup_key))
        if existing is not None:
            return {"success": True, "task_id": existing.id, "created": False}

        create_args = {
            "project_id": project_id,
            "title": title,
            "description": args.get("description", ""),
            "priority": args.get("priority", 100),
            "dedup_key": dedup_key,
            # Control-plane bookkeeping: suppress task.created emission so the
            # default pipeline is not re-triggered against this task itself
            # (would attach a routing gate to a task only the triage agent
            # can resolve — self-deadlock).  Routing of tasks created via
            # ensure_task is the ensuring pipeline's responsibility.
            "_suppress_created_event": True,
        }
        # Presentation tasks such as playbook-run roots must be born in their
        # projected state. Creating them READY and editing them afterward
        # leaves a window where a pull worker can claim control-plane data as
        # executable work.
        if args.get("initial_status"):
            if not str(dedup_key).startswith("playbook-run:"):
                return {
                    "success": False,
                    "error": "initial_status is reserved for playbook-run presentation tasks",
                }
            allowed = {"IN_PROGRESS", "PAUSED", "COMPLETED", "FAILED"}
            if args["initial_status"] not in allowed:
                return {
                    "success": False,
                    "error": f"Invalid playbook-run initial status '{args['initial_status']}'",
                }
            create_args["_initial_status"] = args["initial_status"]
        # Optional pre-routing: control-plane tasks skip triage, so the
        # ensuring pipeline may pin the executing profile directly (e.g.
        # the default pipeline pins 'triage' on the triage task).
        if args.get("profile_id"):
            create_args["profile_id"] = args["profile_id"]
        # Validated by ``_cmd_create_task`` against the pinned profile, so an
        # unknown class or one with no model mapping fails the node loudly
        # instead of silently producing an unroutable task.
        if intelligence_class:
            create_args["intelligence_class"] = intelligence_class
        result = await self._cmd_create_task(create_args)
        if "error" in result:
            return {"success": False, "error": result["error"]}
        created_task = await self.db.get_task(result["created"])
        if created_task is not None:
            await self._emit_task_graph_change("task.updated", created_task)
        return {"success": True, "task_id": result["created"], "created": True}

    async def _cmd_get_downstream_tasks(self, args: dict) -> dict:
        """Return transitive dependents over blocking edge types.

        Follows ``blocks``, ``waits-for``, ``conditional-blocks``, and
        ``parent-child`` edges — the set that gates readiness (see
        ``src/database/queries/blocked_state.py``). Returns ``[]`` if the
        task has no dependents.
        """
        task_id = args.get("task_id")
        if not task_id:
            return {"success": False, "error": "task_id is required"}
        seed = await self.db.get_task(str(task_id))
        if seed is None:
            return {"success": False, "error": f"task '{task_id}' not found"}
        edge_types = (
            DepType.BLOCKS.value,
            DepType.WAITS_FOR.value,
            DepType.CONDITIONAL_BLOCKS.value,
            DepType.PARENT_CHILD.value,
        )
        ids = await self.db.get_transitive_dependents(str(task_id), edge_types)
        out = []
        for tid in ids:
            t = await self.db.get_task(tid)
            if t is None:
                continue
            out.append({"id": t.id, "title": t.title, "status": t.status.value})
        return {"success": True, "tasks": out}

    async def _cmd_task_route(self, args: dict) -> dict:
        """Route a task: assign profile + intelligence class (+ workspace).

        Compatibility command and the only manual resolver for ``routing``
        gates. Writes ``profile_id``, an explicit ``intelligence_class``, and optional
        ``preferred_workspace_id`` onto the task, then resolves every open
        ``routing`` gate attached to the task via the orchestrator helper
        (so ``gate.resolved`` + blocked-flip bus events fire the same way
        the sweep path emits them).

        Args:
            task_id: Target task id (required).
            profile_id: AgentProfile id (required).
            intelligence_class: Vault class id. May be omitted only when the
                task already has an explicit class. Profile defaults never
                establish assignment eligibility. Routing a running or
                claimed task is refused.
            workspace_id: Optional workspace id.  When supplied, must belong
                to the task's project (deadlock-safe & scope-safe).

        Returns:
            ``{"success": True, "task_id", "resolved_gate_ids": [str]}`` or
            ``{"success": False, "error": str}`` on validation failure
            (unknown task/profile/class/workspace, wrong project, or a
            class with no mapping for the profile's harness provider).
        """
        task_id = args.get("task_id")
        profile_id = args.get("profile_id")
        if not task_id or not profile_id:
            return {"success": False, "error": "task_id and profile_id are required"}
        task = await self.db.get_task(str(task_id))
        if task is None:
            return {"success": False, "error": f"task '{task_id}' not found"}
        profile = await self.db.get_profile(str(profile_id))
        if profile is None:
            return {"success": False, "error": f"profile '{profile_id}' not found"}

        cls_id = args.get("intelligence_class") or task.intelligence_class or None
        if not cls_id:
            return {
                "success": False,
                "error": (
                    "intelligence_class is required when the task has no explicit class"
                ),
            }
        class_error = self._validate_routing_class(cls_id, profile)
        if class_error:
            return {"success": False, "error": class_error}

        workspace_id = args.get("workspace_id")
        if workspace_id:
            ws = await self.db.get_workspace(str(workspace_id))
            if ws is None:
                return {
                    "success": False,
                    "error": f"workspace '{workspace_id}' not found",
                }
            if ws.project_id != task.project_id:
                return {
                    "success": False,
                    "error": (
                        f"workspace '{workspace_id}' belongs to project "
                        f"'{ws.project_id}', not '{task.project_id}'"
                    ),
                }

        updated = await self.db.update_task_routing(
            str(task_id),
            profile_id=str(profile_id),
            intelligence_class=cls_id,
            preferred_workspace_id=str(workspace_id) if workspace_id else None,
        )
        if not updated:
            return {
                "success": False,
                "error": "Task is running or claimed; stop the task before changing its routing.",
            }

        resolved: list[str] = []
        for gate in await self.db.get_gates_for_task(str(task_id)):
            if gate["gate_type"] == "routing" and gate["status"] == "open":
                await self.orchestrator._resolve_gate_and_emit(
                    gate["id"],
                    resolved_by="task_route",
                    resolution=f"routed to {profile_id}",
                )
                resolved.append(gate["id"])
        return {
            "success": True,
            "task_id": str(task_id),
            "resolved_gate_ids": resolved,
        }


def _harness_provider(harness: str | None) -> str:
    """Map a profile harness id to the intelligence-class provider key."""
    from src.profiles.intelligence import provider_for_harness

    return provider_for_harness(harness)
