"""Task commands mixin — CRUD, lifecycle, dependencies, plans."""

from __future__ import annotations

import asyncio
import logging
import os

from src.models import (
    BLOCKING_DEP_TYPES,
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
from src.task_summary import write_task_summary

logger = logging.getLogger(__name__)


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


def _check_capability_escalation(parent, child) -> str:
    """Return an explanation if ``child`` exceeds ``parent``'s capabilities.

    Returns the empty string when the child profile is a strict subset
    (i.e. acceptable).  Used by ``_cmd_create_task`` to reject upward
    escalation when a sandboxed caller delegates work to another profile.

    Subset semantics:

    * ``child.allowed_tools ⊆ parent.allowed_tools``.  An empty parent
      list means the parent has no tools at all, so the child cannot
      have any either.  An empty child list is always OK (strictest).
    * ``child.mcp_servers ⊆ parent.mcp_servers``.

    System-prompt subsetting is intentionally not enforced — there is no
    mechanical notion of "subset of prose".  The parent profile's author
    is responsible for what they delegate; the runtime ensures the
    delegate cannot reach beyond the parent's tool/server bounds.
    """
    parent_tools = set(parent.allowed_tools or [])
    child_tools = set(child.allowed_tools or [])
    extra_tools = sorted(child_tools - parent_tools)
    if extra_tools:
        return f"child has {len(extra_tools)} tool(s) not in parent's allowlist: {extra_tools[:5]}"
    parent_servers = set(parent.mcp_servers or [])
    child_servers = set(child.mcp_servers or [])
    extra_servers = sorted(child_servers - parent_servers)
    if extra_servers:
        return (
            f"child references {len(extra_servers)} MCP server(s) not in "
            f"parent's list: {extra_servers}"
        )
    return ""


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
    # approve (accept an AWAITING_APPROVAL task's PR), and chain-health
    # diagnostics for dependency graphs.
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
                "parent_task_id": t.parent_task_id,
                "is_plan_subtask": t.is_plan_subtask,
                "task_type": t.task_type.value if t.task_type else None,
                "pr_url": t.pr_url,
                "requires_approval": t.requires_approval,
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
            "parent_task_id": t.parent_task_id,
            "is_plan_subtask": t.is_plan_subtask,
            "task_type": t.task_type.value if t.task_type else None,
            "pr_url": t.pr_url,
            "requires_approval": t.requires_approval,
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
        """Move a task under another container, or to root.  Backs ``aq task reparent``."""
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
        try:
            async with self.db._engine.begin() as conn:
                result = await self.db.set_parent(task_id, new_parent, conn=conn)
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
        return {
            "success": True,
            "task_id": task_id,
            "old_parent": old_parent,
            "new_parent": new_parent,
        }

    async def _create_worker_filed_task(
        self,
        task: Task,
        *,
        held_id: str,
        parent_id: str | None,
        discovered_from: str | None,
        edges: list[tuple[str, str]],
    ) -> tuple[str, str | None, str | None, bool]:
        """Write a worker-filed task + its edges in one ``immediate()`` txn.

        Order (swarm work model §12): ``reserve_filing`` first — ``False``
        raises :class:`_FilingQuota` with nothing written — then the task
        row, then the parent-child or ``discovered-from`` edge, then (root
        filings only) the routing gate, then the ``depends_on`` edges. Any
        exception rolls the whole transaction back untouched.

        Returns ``(task_id, gate_id, discovered_from_origin, depth_cap_fallback)``.
        ``gate_id`` is only set for root-level filings (no ``parent_id``);
        ``discovered_from_origin`` is set whenever a ``discovered-from`` edge
        was written — root filings (origin = ``discovered_from`` or the held
        task) and depth-cap-fallback child filings alike (origin = the
        would-be container).

        The ``is_blocked`` flip set from every write below (task-row
        creation never flips anything; the edge, and the gate if any, can)
        is accumulated and logged via :meth:`log_blocked_flips` once, after
        the transaction commits — mirrors what ``add_dependency``/
        ``create_gate`` do for their own single-write callers.
        """
        gate_id: str | None = None
        origin: str | None = None
        depth_cap_fallback = False
        flipped: set[str] = set()
        async with self.db.immediate() as conn:
            if not await self.db.reserve_filing(
                conn, held_id, max_filings=self.config.swarm.max_filings_per_task
            ):
                raise _FilingQuota()
            if parent_id:
                task.id, depth_cap_fallback = await child_task_id(conn, parent_id)
            else:
                task.id = await fresh_root_id(conn)
            await self.db.create_task(task, conn=conn)
            if parent_id and not depth_cap_fallback:
                result = await self.db.set_parent(task.id, parent_id, conn=conn)
                flipped |= result.flipped
            elif parent_id:
                # Naming-depth cap: child_task_id already minted a root id;
                # record provenance the same way create_task_under does,
                # instead of a parent-child edge to the (too-deep) container.
                origin = parent_id
                flipped |= await self.db.add_dependency(
                    task.id, origin, DepType.DISCOVERED_FROM.value, conn=conn
                ) or set()
            else:
                origin = discovered_from or held_id
                flipped |= await self.db.add_dependency(
                    task.id, origin, DepType.DISCOVERED_FROM.value, conn=conn
                ) or set()
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
            for dep_id, dep_type in edges:
                flipped |= await self.db.add_dependency(
                    task.id, dep_id, dep_type, conn=conn
                ) or set()
        await self.db.log_blocked_flips(flipped)
        return task.id, gate_id, origin, depth_cap_fallback

    async def _cmd_create_task(self, args: dict) -> dict:
        # ----- Worker-filed work (swarm work model §12) --------------------
        # A session-scoped, non-elevated caller is a pool worker currently
        # holding a task. Its filings are pinned to its own project, forced
        # to start DEFINED, and constrained to the held task's subtree for
        # the parent/discovered-from edge. ``filing_session``/``held_id``
        # stay ``None`` for every other caller (elevated, local, MCP without
        # session scope) — that path is completely untouched below.
        scope = self._current_scope or {}
        filing_session = None
        held_id: str | None = None
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
            async with self.db._engine.begin() as _conn:
                allowed = {held_id} | set(await self.db.subtree_ids(held_id, conn=_conn))
            if args.get("discovered_from") and args["discovered_from"] not in allowed:
                return {
                    "success": False,
                    "error": "discovered_from must be the held task or one of its descendants",
                }
            if args.get("parent_id") and args["parent_id"] not in allowed:
                return {
                    "success": False,
                    "error": "parent must be the held task or one of its descendants",
                }

        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
        # ``task_id`` is generated *after* parent_id validation below so
        # hierarchical child ids ({parent}.{n}) can be wired.  Placeholder
        # here keeps the downstream code readable.
        task_id: str = ""
        depth_cap_fallback = False
        requires_approval = args.get("requires_approval", False)
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
        # **v1 gap — recursive task→child-task escalation:** when
        # ``create_task`` is invoked by a task agent via the embedded
        # ``agent-queue`` MCP server (HTTP), there's no per-task identity
        # on the request, so ``self._caller_profile_id`` will be unset
        # and the escalation check won't fire.  The line of defense for
        # untrusted-input *tasks* is therefore the Claude CLI's
        # ``--allowed-tools`` flag — a profile that omits
        # ``mcp__agent-queue__create_task`` from its allowlist literally
        # cannot reach this code path from inside the agent.  Profiles
        # that DO whitelist ``create_task`` are trusted to delegate.
        # See ``docs/specs/design/sandboxed-playbooks.md`` for the plan
        # to plumb task identity through the MCP server.
        profile_id = args.get("profile_id")
        caller_profile_id = getattr(self, "_caller_profile_id", None)
        caller_profile = None
        if caller_profile_id:
            caller_profile = await self.db.get_profile(caller_profile_id)
            if caller_profile is None:
                # Caller profile is gone — fail closed.  Leaks of stale
                # caller_profile_id mid-run shouldn't widen the child's
                # scope; refuse the create until the situation is sane.
                return {
                    "error": f"Caller profile '{caller_profile_id}' not "
                    "found — refusing to create task without a resolved "
                    "capability bound."
                }

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
        edges: list[tuple[str, str]] = []
        for entry in raw_depends_on or []:
            if isinstance(entry, str):
                dep_id, dep_type = entry, DepType.BLOCKS.value
            elif isinstance(entry, dict):
                dep_id = entry.get("task_id") or entry.get("id") or ""
                dep_type = entry.get("dep_type") or DepType.BLOCKS.value
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
            edges.append((dep_id, dep_type))

        parent_id = args.get("parent_id")
        if parent_id:
            parent = await self.db.get_task(parent_id)
            if parent is None:
                return {"error": f"Parent task '{parent_id}' not found"}

        # A task created *with* blocking edges starts DEFINED so the
        # promotion cascade decides when it becomes runnable — creating it
        # READY-but-blocked would hand the scheduler a task it must not run.
        # A parented task is always withheld until ``set_parent``/
        # ``create_task_under`` recomputes it against the (possibly-DEFINED)
        # container (work-graph §7).
        has_blocking_edge = bool(parent_id) or any(
            dep_type in BLOCKING_DEP_TYPES for _, dep_type in edges
        )
        initial_status = (
            TaskStatus.DEFINED
            if (self._plan_subtask_creation_mode or has_blocking_edge or filing_session is not None)
            else TaskStatus.READY
        )
        auto_approve_plan = args.get("auto_approve_plan", False)
        skip_verification = args.get("skip_verification", False)
        workflow_id = args.get("workflow_id")
        task = Task(
            id="",
            project_id=project_id,
            title=args["title"],
            description=args.get("description", args["title"]),
            priority=args.get("priority", 100),
            status=initial_status,
            requires_approval=requires_approval,
            task_type=task_type,
            profile_id=profile_id,
            preferred_workspace_id=preferred_workspace_id,
            attachments=attachments,
            auto_approve_plan=auto_approve_plan,
            skip_verification=skip_verification,
            workflow_id=workflow_id,
            affinity_agent_id=affinity_agent_id,
            affinity_reason=affinity_reason,
            workspace_mode=workspace_mode,
            parent_task_id=None,
            dedup_key=args.get("dedup_key"),
            intelligence_class=args.get("intelligence_class"),
        )
        gate_id: str | None = None
        discovered_from_origin: str | None = None
        depth_cap_fallback = False
        if filing_session is not None:
            task.created_by_kind = "session"
            task.created_by_id = filing_session.id
            try:
                task_id, gate_id, discovered_from_origin, depth_cap_fallback = (
                    await self._create_worker_filed_task(
                        task,
                        held_id=held_id,
                        parent_id=parent_id,
                        discovered_from=args.get("discovered_from"),
                        edges=edges,
                    )
                )
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
                task_id, depth_cap_fallback = await self.db.create_task_under(task, parent_id)
            except HierarchyError as exc:
                return {
                    "error": f"hierarchy.{exc.code}: {exc.detail}",
                    "code": f"hierarchy.{exc.code}",
                }
        else:
            task_id = await generate_task_id(self.db)
            task.id = task_id
            await self.db.create_task(task)

        # Persist requires_kinds rows now that the FK target exists.
        if normalized_requirements:
            await self.db.add_task_workspace_requirements(task_id, normalized_requirements)

        # Graph edges and labels, now that the FK target exists.  Each
        # ``add_dependency`` recomputes the blocked-state projection, so the
        # task's ``is_blocked`` is correct before anything can schedule it.
        # Worker-filed edges were already written inside
        # ``_create_worker_filed_task``'s transaction above — only log them
        # here so the audit trail is identical either way.
        for dep_id, dep_type in edges:
            if filing_session is None:
                try:
                    await self.db.add_dependency(task_id, dep_id, dep_type)
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

        # dv2 phase 1 — emit ``task.created`` on the EventBus so the default
        # pipeline playbook (and any other subscribers) can fire the routing
        # gate + triage flow.  Uses the shared ``_emit_task_event`` helper so
        # the base triple (task_id, project_id, title) is populated and
        # matches the registered schema.
        #
        # NON-OBVIOUS INVARIANT: ``ensure_task`` passes ``_suppress_created_event``
        # so control-plane bookkeeping tasks (e.g. the triage task the default
        # pipeline creates) do NOT re-fire the pipeline against themselves.
        # Emitting there would attach a routing gate to the triage task —
        # and routing gates are only resolved BY the triage agent, so the
        # triage task would deadlock blocked on its own gate.
        if not args.get("_suppress_created_event"):
            try:
                extras: dict[str, object] = {}
                if profile_id:
                    extras["profile_id"] = profile_id
                if task_type:
                    extras["task_type"] = task_type.value
                # Worker-filed work (swarm work model §12): always present,
                # ``None`` when not applicable — the default pipeline's
                # ``worker-filed-triage`` rule keys off ``created_by_kind``
                # and ``parent_task_id``. ``parent_id``/``depth_cap_fallback``
                # reflect the real edge written above; ``task.parent_task_id``
                # itself is never updated in-memory (``set_parent``/
                # ``create_task_under`` own the DB column, not the object).
                extras["created_by_kind"] = task.created_by_kind
                extras["created_by_id"] = task.created_by_id
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
                # Narrow-log the emission failure loudly — this is the wire that
                # kicks off the default pipeline (routing gate + triage).  Losing
                # it silently means every new task will just sit in READY unrouted.
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
        if requires_approval:
            result["requires_approval"] = True
        if task_type:
            result["task_type"] = task_type.value
        if profile_id:
            result["profile_id"] = profile_id
        if preferred_workspace_id:
            result["preferred_workspace_id"] = preferred_workspace_id
        if attachments:
            result["attachments"] = attachments
        if auto_approve_plan:
            result["auto_approve_plan"] = True
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
                {"task_id": dep_id, "dep_type": dep_type} for dep_id, dep_type in edges
            ]
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

        findings = await validate_graph(
            graph, project_id=project_id, db=self.db, vault_root=vault_root
        )
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
            "requires_approval": task.requires_approval,
            # Persisted graph blockedness (work-graph design §4).  Capacity
            # reasons (no agent, workspace busy, budget) are NOT in here —
            # those belong to `task explain`.
            "is_blocked": task.is_blocked,
            "is_plan_subtask": task.is_plan_subtask,
            "task_type": task.task_type.value if task.task_type else None,
            "parent_task_id": task.parent_task_id,
            "profile_id": task.profile_id,
            "auto_approve_plan": task.auto_approve_plan,
            "skip_verification": task.skip_verification,
            "workflow_id": task.workflow_id,
            "affinity_agent_id": task.affinity_agent_id,
            "affinity_reason": task.affinity_reason,
            "workspace_mode": task.workspace_mode.value if task.workspace_mode else None,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }
        if task.pr_url:
            info["pr_url"] = task.pr_url

        # Dependency visualization: show what this task depends on and blocks
        deps = await self.db.get_dependencies(task.id)
        if deps:
            dep_details = []
            for dep_id in deps:
                dep_task = await self.db.get_task(dep_id)
                if dep_task:
                    dep_details.append(
                        {
                            "id": dep_task.id,
                            "title": dep_task.title,
                            "status": dep_task.status.value,
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
            ``task_id``, ``title``, ``status``, ``depends_on`` list, and
            ``blocks`` list.  Each entry in those lists carries ``id``,
            ``title``, and ``status``.
        """
        task_id = args.get("task_id", "")
        if not task_id:
            return {"error": "task_id is required"}

        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}

        # Upstream: what this task depends on
        dep_ids = await self.db.get_dependencies(task.id)
        depends_on: list[dict] = []
        for dep_id in sorted(dep_ids):
            dep_task = await self.db.get_task(dep_id)
            if dep_task:
                depends_on.append(
                    {
                        "id": dep_task.id,
                        "title": dep_task.title,
                        "status": dep_task.status.value,
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

        return {
            "task_id": task.id,
            "title": task.title,
            "status": task.status.value,
            "depends_on": depends_on,
            "blocks": blocks,
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
            await self.db.add_dependency(task_id, depends_on, dep_type)
        except HierarchyError as exc:
            return {"error": f"hierarchy.{exc.code}: {exc.detail}", "code": f"hierarchy.{exc.code}"}
        await self.db.log_event(
            "dependency.added",
            project_id=task.project_id,
            task_id=task_id,
            payload=f"{dep_type} -> {depends_on}",
        )

        return {
            "ok": True,
            "task_id": task_id,
            "depends_on": depends_on,
            "dep_type": dep_type,
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

        return {
            "ok": True,
            "task_id": task_id,
            "removed_dependency": depends_on,
            "removed_dep_types": sorted(matching),
            "task_title": task.title,
        }

    async def _cmd_edit_task(self, args: dict) -> dict:
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}

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
            await self.db.transition_task(
                args["task_id"],
                new_status,
                context="edit_task",
            )
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
        if "auto_approve_plan" in args:
            updates["auto_approve_plan"] = bool(args["auto_approve_plan"])
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

        if updates:
            await self.db.update_task(args["task_id"], **updates)

        all_fields = list(updates.keys())
        if status_changed:
            all_fields.append("status")

        if not all_fields:
            return {
                "error": (
                    "No fields to update. Provide project_id, title, description, priority, "
                    "task_type, status, max_retries, verification_type, profile_id, "
                    "auto_approve_plan, skip_verification, affinity_agent_id, "
                    "affinity_reason, or workspace_mode."
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
        return result

    async def _cmd_stop_task(self, args: dict) -> dict:
        error = await self.orchestrator.stop_task(args["task_id"])
        if error:
            return {"error": error}
        return {"stopped": args["task_id"]}

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

        old_status = task.status.value

        # Append feedback to the task description so the agent sees it
        # when the task is re-executed.
        separator = "\n\n---\n**Reopen Feedback:**\n"
        updated_description = task.description + separator + feedback

        # Preserve requires_approval so the agent re-creates a PR on
        # completion (the field must survive reopen cycles).
        await self.db.transition_task(
            task_id,
            TaskStatus.READY,
            context="reopen_with_feedback",
            description=updated_description,
            retry_count=0,
            assigned_agent_id=None,
            pr_url=None,
            requires_approval=task.requires_approval,
        )

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
        review_profile_ids = {"reviewer", "final-reviewer"}
        cancelled_reviews: list[str] = []
        for cand in candidates:
            status_val = getattr(cand.status, "value", cand.status)
            if status_val in terminal:
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
            "requires_approval": task.requires_approval,
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

    async def _cmd_approve_task(self, args: dict) -> dict:
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}
        if task.status != TaskStatus.AWAITING_APPROVAL:
            return {"error": f"Task is not awaiting approval (status: {task.status.value})"}
        await self.db.transition_task(
            args["task_id"],
            TaskStatus.COMPLETED,
            context="approve_task",
        )
        await self.db.log_event(
            "task_completed",
            project_id=task.project_id,
            task_id=task.id,
        )
        return {"approved": args["task_id"], "title": task.title}

    async def _cmd_process_task_completion(self, args: dict) -> dict:
        """Process task completion: discover plan files and archive them.

        Called by Supervisor after a task completes. Searches for plan files
        in the workspace, parses them, archives them to .claude/plans/, and
        stores plan data via task_context for later approval flow.

        Includes safety checks ported from the legacy
        ``Orchestrator._discover_and_store_plan()`` path:
        - Staleness check: plan file mtime vs task execution start time
        - skip_if_implemented: skip if branch has substantial non-plan changes
        - Deduplication: skip if tasks with the same titles already exist

        Returns {"plan_found": bool, "steps_count": int, ...}
        """
        import logging
        from src import plan_parser

        logger = logging.getLogger(__name__)

        task_id = args.get("task_id")
        workspace_path = args.get("workspace_path")

        logger.info(
            "process_task_completion: starting plan discovery for task %s (workspace=%s)",
            task_id,
            workspace_path,
        )

        config = self.orchestrator.config.auto_task

        # Check if auto_task is enabled
        if not config.enabled:
            logger.info(
                "process_task_completion: auto-task disabled, skipping plan discovery for task %s",
                task_id,
            )
            return {"plan_found": False, "reason": "Auto-task generation is disabled"}

        if not task_id or not workspace_path:
            return {"plan_found": False, "reason": "Missing required parameters"}

        # Look up the task to check if it's a subtask
        task = await self.db.get_task(task_id)
        if not task:
            return {"plan_found": False, "reason": f"Task {task_id} not found"}

        # Don't process plans for plan-generated subtasks (avoid recursion)
        if task.is_plan_subtask:
            return {"plan_found": False, "reason": "Task is already a plan subtask"}

        # skip_if_implemented: if the branch already has substantial code
        # changes beyond the plan file itself, the agent likely already
        # implemented the plan — no need to create subtasks from it.
        if config.skip_if_implemented:
            try:
                project = await self.db.get_project(task.project_id) if task.project_id else None
                default_branch = await self.orchestrator._get_default_branch(
                    project, workspace_path
                )
                if await self.orchestrator.git.ahas_non_plan_changes(
                    workspace_path, default_branch
                ):
                    logger.info(
                        "process_task_completion: skipping plan for task %s — "
                        "branch has substantial code changes beyond the plan file, "
                        "indicating the plan was already implemented",
                        task_id,
                    )
                    return {
                        "plan_found": False,
                        "reason": "Plan already implemented (non-plan changes detected)",
                    }
            except Exception as e:
                logger.debug(
                    "process_task_completion: skip_if_implemented check failed for task %s: %s",
                    task_id,
                    e,
                )

        # Find a plan file in the workspace
        plan_patterns = config.plan_file_patterns
        plan_file = plan_parser.find_plan_file(workspace_path, plan_patterns)

        if not plan_file:
            logger.info(
                "process_task_completion: no plan file found for task %s "
                "(searched patterns: %s in %s)",
                task_id,
                plan_patterns,
                workspace_path,
            )
            return {"plan_found": False, "reason": "No plan file found"}

        logger.info(
            "process_task_completion: found plan file %s for task %s — running validation checks",
            plan_file,
            task_id,
        )

        # Staleness check: compare the plan file's mtime against the
        # recorded execution start time.  If the plan file predates the
        # agent's execution start, it was written by a previous task and
        # should not be attributed to this one.
        exec_start = self.orchestrator._task_exec_start.get(task_id)
        if exec_start is not None:
            try:
                plan_mtime = os.path.getmtime(plan_file)
                # Use a 2-second tolerance for filesystem timestamp granularity
                if plan_mtime < exec_start - 2.0:
                    logger.warning(
                        "process_task_completion: ignoring stale plan file %s for task %s "
                        "(plan mtime %.0f < exec start %.0f — file "
                        "predates this task's execution)",
                        plan_file,
                        task_id,
                        plan_mtime,
                        exec_start,
                    )
                    # Archive stale plan so it's not rediscovered by future tasks
                    try:
                        plans_dir = os.path.join(workspace_path, ".claude", "plans")
                        os.makedirs(plans_dir, exist_ok=True)
                        stale_archive = os.path.join(plans_dir, f"stale-{task_id}-plan.md")
                        os.rename(plan_file, stale_archive)
                    except OSError:
                        pass
                    return {
                        "plan_found": False,
                        "reason": "Plan file is stale (predates task execution)",
                    }
            except OSError as e:
                logger.debug(
                    "process_task_completion: staleness check failed for %s: %s (proceeding)",
                    plan_file,
                    e,
                )

        # Read the plan file content
        try:
            content = plan_parser.read_plan_file(plan_file)
        except Exception as e:
            logger.warning("Failed to read plan file for task %s: %s", task_id, e)
            return {"plan_found": False, "reason": f"Failed to read plan file: {e}"}

        if not content or not content.strip():
            return {"plan_found": False, "reason": "Plan file is empty"}

        # Archive the plan file to .claude/plans/{task_id}-plan.md
        try:
            plans_dir = os.path.join(workspace_path, ".claude", "plans")
            os.makedirs(plans_dir, exist_ok=True)
            archived_path = os.path.join(plans_dir, f"{task_id}-plan.md")

            os.rename(plan_file, archived_path)
            logger.info("Archived plan file to %s", archived_path)
        except Exception as e:
            logger.warning("Failed to archive plan file for task %s: %s", task_id, e)
            archived_path = plan_file  # Use original path if archival fails

        # Store raw plan content in task_context for later use during approval.
        # The actual plan-to-task splitting is done by the supervisor LLM
        # at approval time (not by algorithmic parsing here).
        try:
            await self.db.add_task_context(
                task_id,
                type="plan_archived_path",
                label="Plan Archived Path",
                content=archived_path,
            )
            await self.db.add_task_context(
                task_id,
                type="plan_raw",
                label="Plan Raw Content",
                content=content,
            )
        except Exception as e:
            logger.warning("Failed to store plan context for task %s: %s", task_id, e)

        logger.info(
            "process_task_completion: plan stored for task %s — "
            "plan_found=True, archived=%s, content_length=%d",
            task_id,
            archived_path,
            len(content),
        )

        return {
            "plan_found": True,
            "plan_file": plan_file,
            "archived_path": archived_path,
        }

    async def _cmd_approve_plan(self, args: dict) -> dict:
        """Approve a plan and activate its draft subtasks.

        The task must be in AWAITING_PLAN_APPROVAL status.

        **Parse-first workflow (preferred):**
        If ``_cmd_process_plan`` already created draft subtasks (stored in
        ``plan_draft_subtasks`` context), approval transitions the parent to
        IN_PROGRESS — signaling that the plan is approved and subtasks are
        running.  The parent will auto-complete when all subtasks finish.

        **Legacy workflow (fallback):**
        If no draft subtasks exist (e.g. plan came through the old auto-
        detection path), the supervisor LLM is called to create tasks now,
        falling back to the algorithmic parser if the supervisor is
        unavailable.
        """
        import json as _json
        import logging

        logger = logging.getLogger(__name__)

        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}
        if task.status != TaskStatus.AWAITING_PLAN_APPROVAL:
            return {"error": f"Task is not awaiting plan approval (status: {task.status.value})"}

        # Retrieve stored plan content
        contexts = await self.db.get_task_contexts(task.id)
        raw_ctx = next((c for c in contexts if c["type"] == "plan_raw"), None)
        if not raw_ctx:
            return {"error": "No stored plan content found for this task"}

        raw_plan = raw_ctx["content"]
        config = self.orchestrator.config.auto_task

        # ── Check for pre-created draft subtasks (parse-first workflow) ──
        draft_ctx = next((c for c in contexts if c["type"] == "plan_draft_subtasks"), None)

        created_info: list[dict] = []

        if draft_ctx:
            # Draft subtasks were already created by _cmd_process_plan.
            # They are blocked by a dependency on this parent task.
            # Transitioning the parent to IN_PROGRESS will unblock the chain
            # (plan subtask dependency checking treats IN_PROGRESS parents as met).
            created_info = _json.loads(draft_ctx["content"])
            logger.info(
                "approve_plan: found %d pre-created draft subtasks for %s",
                len(created_info),
                task.id,
            )

            # Defense-in-depth: draft_ctx should always have subtasks, but
            # guard against empty list (e.g. data corruption, partial save).
            if not created_info:
                logger.warning(
                    "approve_plan: draft subtasks context exists but is empty "
                    "for task %s — refusing to approve",
                    task.id,
                )
                return {
                    "error": (
                        "Draft subtasks context is empty — no subtasks to activate. "
                        "Please retry or use /process-plan to manually trigger "
                        "subtask creation."
                    )
                }

            # Handle downstream dependencies: any task that depends on the
            # parent task should also depend on the final subtask so
            # downstream work waits for the full chain to finish.
            if config.chain_dependencies and created_info:
                final_subtask_id = created_info[-1]["id"]
                dependents = await self.db.get_dependents(task.id)
                # Exclude draft subtasks themselves from downstream dep wiring
                draft_ids = {t["id"] for t in created_info}
                for dep_task_id in dependents:
                    if dep_task_id in draft_ids:
                        continue
                    try:
                        await self.db.add_dependency(dep_task_id, depends_on=final_subtask_id)
                    except Exception as e:
                        logger.warning(
                            "Failed to add downstream dep %s→%s: %s",
                            dep_task_id,
                            final_subtask_id,
                            e,
                        )
        else:
            # ── Legacy workflow: create subtasks on approval ──
            ws = await self.db.get_workspace_for_task(task.id)
            workspace_id = ws.id if ws else None
            if not workspace_id and task.project_id:
                workspaces = await self.db.list_workspaces(project_id=task.project_id)
                if workspaces:
                    workspace_id = workspaces[0].id

            supervisor = getattr(self.orchestrator, "_supervisor", None)

            if supervisor and supervisor.is_ready:
                _lock_project = task.project_id or ""
                created_info = await supervisor.break_plan_into_tasks(
                    raw_plan=raw_plan,
                    parent_task_id=task.id,
                    project_id=_lock_project,
                    workspace_id=workspace_id,
                    chain_dependencies=config.chain_dependencies,
                    requires_approval=(
                        task.requires_approval if config.inherit_approval else False
                    ),
                    base_priority=task.priority,
                )

                # Treat empty result as an error — the LLM call may have
                # failed silently (rate limit, timeout, etc.).
                if not created_info:
                    logger.warning(
                        "approve_plan: supervisor returned 0 subtasks for "
                        "task %s — refusing to approve",
                        task.id,
                    )
                    return {
                        "error": (
                            "Supervisor failed to create subtasks from the plan. "
                            "The plan has not been approved. Please retry or use "
                            "/process-plan to manually trigger subtask creation."
                        )
                    }

                if config.chain_dependencies:
                    final_subtask_id = created_info[-1]["id"]
                    dependents = await self.db.get_dependents(task.id)
                    for dep_task_id in dependents:
                        try:
                            await self.db.add_dependency(dep_task_id, depends_on=final_subtask_id)
                        except Exception as e:
                            logger.warning(
                                "Failed to add downstream dep %s→%s: %s",
                                dep_task_id,
                                final_subtask_id,
                                e,
                            )
            else:
                logger.error(
                    "approve_plan: supervisor unavailable — cannot create subtasks for task %s",
                    task.id,
                )
                return {"error": "Supervisor is not available. Cannot create subtasks from plan."}

        # Note: no separate channel notification here — the PlanApprovalView
        # already updates the original embed in-place to show approval status
        # and subtask count, avoiding duplicate messages.

        # Transition to IN_PROGRESS — the plan is approved and subtasks are
        # active.  The parent will auto-complete when all subtasks finish
        # via event-driven container settlement (spec §7, hierarchy_queries.py).
        # Plan subtask dependency checking treats IN_PROGRESS plan parents
        # as satisfying the dependency.
        await self.db.transition_task(
            args["task_id"],
            TaskStatus.IN_PROGRESS,
            context="plan_approved",
        )

        # Re-check DEFINED tasks so subtasks get promoted to READY
        await self.orchestrator._check_defined_tasks()

        await self.db.log_event(
            "plan_approved",
            project_id=task.project_id,
            task_id=task.id,
            payload=f"Activated {len(created_info)} subtask(s)",
        )

        # Schedule heavy cleanup work (file deletion + git commits) in the
        # background so the approve command returns immediately and the
        # Discord UI feels responsive.
        async def _background_cleanup():
            try:
                await self._cleanup_plan_files_after_approval(task)
            except Exception as e:
                logger.warning(
                    "Background plan cleanup failed for task %s: %s",
                    task.id,
                    e,
                )

        asyncio.create_task(_background_cleanup())

        return {
            "approved": args["task_id"],
            "title": task.title,
            "subtask_count": len(created_info),
            "subtasks": created_info,
        }

    async def _cleanup_plan_files_after_approval(self, task) -> None:
        """Delete ALL plan files from all workspaces after a plan is approved/deleted.

        Removes:
        1. All plan files that were enumerated during ``_cmd_process_plan``
           (stored in ``plan_all_enumerated_paths`` task context).
        2. The archived plan file (in ``.claude/plans/``).
        3. Any original plan files matching configured patterns (safety net).

        Commits the deletions so plan files don't persist on the branch
        and get picked up by other tasks running in the same workspace.
        """
        logger = logging.getLogger(__name__)
        deleted_any = False
        workspaces_with_deletions: set[str] = set()

        contexts = await self.db.get_task_contexts(task.id)

        # 1. Delete ALL enumerated plan files (from process_plan discovery)
        enumerated_ctx = next(
            (c for c in contexts if c["type"] == "plan_all_enumerated_paths"), None
        )
        if enumerated_ctx:
            import json as _json_cleanup

            try:
                enumerated_paths = _json_cleanup.loads(enumerated_ctx["content"])
            except (ValueError, TypeError):
                enumerated_paths = []
            for plan_path in enumerated_paths:
                try:
                    if os.path.isfile(plan_path):
                        os.remove(plan_path)
                        deleted_any = True
                        # Track the workspace directory for committing
                        workspaces_with_deletions.add(
                            os.path.dirname(
                                os.path.dirname(plan_path) if ".claude" in plan_path else plan_path
                            )
                        )
                        logger.info("Plan cleanup: deleted enumerated plan file %s", plan_path)
                except OSError as e:
                    logger.warning(
                        "Plan cleanup: failed to delete enumerated plan %s: %s", plan_path, e
                    )

        # 2. Delete the archived plan file if it exists
        archived_ctx = next((c for c in contexts if c["type"] == "plan_archived_path"), None)
        if archived_ctx:
            archived_path = archived_ctx["content"]
            try:
                if os.path.isfile(archived_path):
                    os.remove(archived_path)
                    deleted_any = True
                    logger.info("Plan cleanup: deleted archived plan file %s", archived_path)
            except OSError as e:
                logger.warning(
                    "Plan cleanup: failed to delete archived plan %s: %s", archived_path, e
                )

        # 3. Safety net: also check configured plan patterns in all project workspaces
        ws = await self.db.get_workspace_for_task(task.id)
        if ws and ws.workspace_path:
            workspace = ws.workspace_path
        else:
            workspace = await self.db.get_project_workspace_path(task.project_id)

        if workspace:
            import glob as _glob

            plan_patterns = self.orchestrator.config.auto_task.plan_file_patterns
            for pattern in plan_patterns:
                full_pattern = os.path.join(workspace, pattern)
                matches = _glob.glob(full_pattern)
                if not matches:
                    if os.path.isfile(full_pattern):
                        matches = [full_pattern]
                for plan_path in matches:
                    try:
                        if os.path.isfile(plan_path):
                            os.remove(plan_path)
                            deleted_any = True
                            workspaces_with_deletions.add(workspace)
                            logger.info("Plan cleanup: deleted plan file %s", plan_path)
                    except OSError as e:
                        logger.warning("Plan cleanup: failed to delete plan %s: %s", plan_path, e)

            # Also scan all project workspaces for any remaining plan files
            all_ws = await self.db.list_workspaces(project_id=task.project_id)
            for ws_entry in all_ws:
                if not ws_entry.workspace_path or not os.path.isdir(ws_entry.workspace_path):
                    continue
                from src.plan_parser import find_all_plan_files

                remaining = find_all_plan_files(ws_entry.workspace_path)
                for pf in remaining:
                    try:
                        if os.path.isfile(pf["path"]):
                            os.remove(pf["path"])
                            deleted_any = True
                            workspaces_with_deletions.add(ws_entry.workspace_path)
                            logger.info("Plan cleanup: deleted remaining plan file %s", pf["path"])
                    except OSError as e:
                        logger.warning(
                            "Plan cleanup: failed to delete remaining plan %s: %s", pf["path"], e
                        )

        # 4. Commit the deletions in each workspace that had files deleted
        if deleted_any and task.branch_name:
            # Determine which workspaces to commit in
            commit_workspaces: set[str] = set()
            if workspace:
                commit_workspaces.add(workspace)
            commit_workspaces.update(workspaces_with_deletions)

            for ws_path in commit_workspaces:
                if not os.path.isdir(ws_path):
                    continue
                try:
                    if await self.orchestrator.git.avalidate_checkout(ws_path):
                        await self.orchestrator.git.acommit_all(
                            ws_path,
                            f"chore: delete plan files after approval\n\nTask-Id: {task.id}",
                            event_bus=self.orchestrator.bus,
                            project_id=task.project_id,
                            agent_id=task.assigned_agent_id,
                        )
                        logger.info(
                            "Plan cleanup: committed plan file deletion in %s for task %s",
                            ws_path,
                            task.id,
                        )
                except Exception as e:
                    logger.warning(
                        "Plan cleanup: failed to commit deletion in %s for task %s: %s",
                        ws_path,
                        task.id,
                        e,
                    )

    async def _delete_draft_subtasks(self, parent_task_id: str) -> int:
        """Delete draft subtasks that were pre-created by ``_cmd_process_plan``.

        Reads the ``plan_draft_subtasks`` task-context entry, deletes each
        referenced task from the database, and returns the count of deleted
        tasks.  Safe to call when no drafts exist (returns 0).
        """
        import json as _json

        contexts = await self.db.get_task_contexts(parent_task_id)
        draft_ctx = next((c for c in contexts if c["type"] == "plan_draft_subtasks"), None)
        if not draft_ctx:
            return 0

        draft_tasks: list[dict] = _json.loads(draft_ctx["content"])
        deleted = 0
        for entry in draft_tasks:
            tid = entry.get("id")
            if not tid:
                continue
            try:
                await self.db.delete_task(tid)
                deleted += 1
            except Exception as e:
                logger.warning("delete_draft_subtasks: failed to delete %s: %s", tid, e)
        logger.info(
            "delete_draft_subtasks: deleted %d/%d draft subtasks for %s",
            deleted,
            len(draft_tasks),
            parent_task_id,
        )
        return deleted

    async def _cmd_reject_plan(self, args: dict) -> dict:
        """Reject a plan and reopen the task with feedback for revision.

        The task must be in AWAITING_PLAN_APPROVAL status.  The feedback is
        appended to the task description so the agent sees it on re-execution
        and can revise the plan accordingly.  Any draft subtasks created
        during ``_cmd_process_plan`` are deleted.
        """
        task_id = args["task_id"]
        feedback = args.get("feedback", "")
        if not feedback:
            return {"error": "Feedback is required when rejecting a plan"}

        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}
        if task.status != TaskStatus.AWAITING_PLAN_APPROVAL:
            return {"error": f"Task is not awaiting plan approval (status: {task.status.value})"}

        # Delete any draft subtasks created during process_plan
        deleted_count = await self._delete_draft_subtasks(task_id)

        # Append feedback to description (similar to reopen_with_feedback)
        separator = "\n\n---\n\n**Plan Revision Requested:**\n"
        updated_description = task.description + separator + feedback

        await self.db.transition_task(
            task_id,
            TaskStatus.READY,
            context="plan_rejected",
            description=updated_description,
            retry_count=0,
            assigned_agent_id=None,
            pr_url=None,
        )

        # Store feedback as a structured task_context entry
        await self.db.add_task_context(
            task_id,
            type="plan_revision_feedback",
            label="Plan Revision Feedback",
            content=feedback,
        )

        await self.db.log_event(
            "plan_rejected",
            project_id=task.project_id,
            task_id=task_id,
            payload=feedback[:500],
        )
        return {
            "rejected": task_id,
            "title": task.title,
            "status": "READY",
            "feedback_added": True,
            "draft_subtasks_deleted": deleted_count,
        }

    async def _cmd_delete_plan(self, args: dict) -> dict:
        """Delete a plan and complete the task without creating subtasks.

        The task must be in AWAITING_PLAN_APPROVAL status.  The task is
        transitioned to COMPLETED and no subtasks are created.  Any draft
        subtasks created during ``_cmd_process_plan`` are deleted.
        """
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}
        if task.status != TaskStatus.AWAITING_PLAN_APPROVAL:
            return {"error": f"Task is not awaiting plan approval (status: {task.status.value})"}

        # Delete any draft subtasks created during process_plan
        deleted_count = await self._delete_draft_subtasks(task.id)

        # Clean up plan files from the workspace
        await self._cleanup_plan_files_after_approval(task)

        await self.db.transition_task(
            args["task_id"],
            TaskStatus.COMPLETED,
            context="plan_deleted",
        )
        await self.db.log_event(
            "plan_deleted",
            project_id=task.project_id,
            task_id=task.id,
            payload=f"Plan deleted by user — {deleted_count} draft subtask(s) removed",
        )
        return {
            "deleted": args["task_id"],
            "title": task.title,
            "status": "COMPLETED",
            "draft_subtasks_deleted": deleted_count,
        }

    async def _cmd_process_plan(self, args: dict) -> dict:
        """Manually scan project workspaces for plan files and present for approval.

        This allows users to trigger plan processing via Discord when the
        supervisor misses auto-detection (e.g. the task completed without
        a plan file being detected, or a plan was dropped into a workspace
        manually).

        If a specific task_id is provided, the plan is attached to that task.
        Otherwise a new synthetic "plan" task is created to hold the plan.

        **Workflow (parse-first):**

        1. Discover the plan file in the workspace.
        2. Use the supervisor LLM to break the plan into **draft subtasks**
           (with a dependency on the parent so they stay blocked).
        3. Present the draft subtasks for user approval.
        4. On approve → parent transitions to COMPLETED, unblocking the chain.
           On reject/delete → draft subtasks are deleted.
        """
        from src.plan_parser import find_all_plan_files, read_plan_file

        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"error": "project_id is required (no active project set)"}

        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}

        # Scan ALL workspaces for the project, collecting every plan file
        workspaces = await self.db.list_workspaces(project_id=project_id)
        if not workspaces:
            return {"error": f"No workspaces found for project '{project_id}'"}

        # Collect all plan files across all workspaces with their timestamps
        all_plan_files: list[dict] = []  # {path, ctime, workspace}
        for ws_candidate in workspaces:
            if not ws_candidate.workspace_path or not os.path.isdir(ws_candidate.workspace_path):
                continue
            ws_plans = find_all_plan_files(ws_candidate.workspace_path)
            for pf in ws_plans:
                pf["workspace"] = ws_candidate
                all_plan_files.append(pf)

        if not all_plan_files:
            return {
                "status": "no_plans_found",
                "project_id": project_id,
                "workspaces_scanned": len(workspaces),
                "message": f"No plan files found in {len(workspaces)} workspace(s) for project '{project_id}'",
            }

        # Sort all plan files by ctime (newest first) and pick the newest
        all_plan_files.sort(key=lambda f: f["ctime"], reverse=True)
        newest = all_plan_files[0]

        # Read the newest plan file
        try:
            raw = read_plan_file(newest["path"])
            if not raw or not raw.strip():
                return {
                    "status": "no_plans_found",
                    "project_id": project_id,
                    "workspaces_scanned": len(workspaces),
                    "message": f"Newest plan file is empty: {newest['path']}",
                }
        except Exception as e:
            return {"error": f"Failed to read newest plan file {newest['path']}: {e}"}

        ws = newest["workspace"]
        plan_path = newest["path"]

        logger.info(
            "process_plan: found %d plan file(s) across %d workspace(s), "
            "picking newest: %s (ctime=%.0f)",
            len(all_plan_files),
            len(workspaces),
            plan_path,
            newest["ctime"],
        )

        # Track ALL enumerated plan file paths for cleanup later
        all_enumerated_paths = [pf["path"] for pf in all_plan_files]

        # If a task_id is provided, attach the plan to that task
        task_id = args.get("task_id")
        if task_id:
            task = await self.db.get_task(task_id)
            if not task:
                return {"error": f"Task '{task_id}' not found"}
            if task.status == TaskStatus.AWAITING_PLAN_APPROVAL:
                return {"error": f"Task '{task_id}' already has a plan awaiting approval"}
        else:
            # Create a synthetic task to hold the plan
            task_id = await generate_task_id(self.db)
            # Extract a title from the plan content (first heading or filename)
            plan_title = "Manual plan processing"
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith("# "):
                    plan_title = line[2:].strip()
                    break

            task = Task(
                id=task_id,
                project_id=project_id,
                title=plan_title,
                description=f"Plan discovered via manual /process-plan command.\n\nSource: `{plan_path}`",
                priority=100,
                status=TaskStatus.AWAITING_PLAN_APPROVAL,
                task_type=TaskType.PLAN,
            )
            await self.db.create_task(task)
            logger.info(
                "process_plan: created synthetic task %s for plan in %s",
                task_id,
                plan_path,
            )

        # Archive the plan file
        archived_path = None
        try:
            plans_dir = os.path.join(ws.workspace_path, ".claude", "plans")
            os.makedirs(plans_dir, exist_ok=True)
            archived_path = os.path.join(plans_dir, f"{task_id}-plan.md")
            os.rename(plan_path, archived_path)
            logger.info("process_plan: archived plan to %s", archived_path)
        except OSError as e:
            logger.warning("process_plan: failed to archive plan %s: %s", plan_path, e)

        # Store the raw plan content as task_context
        await self.db.add_task_context(
            task_id,
            type="plan_raw",
            label="Plan Raw Content",
            content=raw,
        )
        if archived_path:
            await self.db.add_task_context(
                task_id,
                type="plan_archived_path",
                label="Plan Archived Path",
                content=archived_path,
            )

        # Store ALL enumerated plan file paths so cleanup can delete them all
        import json as _json_ctx

        await self.db.add_task_context(
            task_id,
            type="plan_all_enumerated_paths",
            label="All Enumerated Plan File Paths",
            content=_json_ctx.dumps(all_enumerated_paths),
        )

        # If we attached to an existing task, transition it to AWAITING_PLAN_APPROVAL
        if args.get("task_id"):
            await self.db.transition_task(
                task_id,
                TaskStatus.AWAITING_PLAN_APPROVAL,
                context="manual_process_plan",
            )

        await self.db.log_event(
            "plan_discovered",
            project_id=project_id,
            task_id=task_id,
            payload=f"Manual plan processing triggered — plan found at {plan_path}",
        )

        # ── Phase 1: Use supervisor LLM to break plan into draft subtasks ──
        # This happens BEFORE approval so the user sees the proper task
        # breakdown.  Draft subtasks are blocked by a dependency on the
        # parent task (which is in AWAITING_PLAN_APPROVAL), so they won't
        # execute until the plan is approved and the parent is COMPLETED.
        #
        # IMPORTANT: We hold a plan-processing lock on the project while the
        # supervisor is creating subtasks.  This prevents the orchestrator's
        # _check_defined_tasks() from promoting subtasks to READY before
        # the blocking dependency on the parent has been established.
        config = self.orchestrator.config.auto_task
        supervisor = getattr(self.orchestrator, "_supervisor", None)
        created_info: list[dict] = []

        if supervisor and supervisor.is_ready:
            workspace_id = ws.id if ws else None
            created_info = await supervisor.break_plan_into_tasks(
                raw_plan=raw,
                parent_task_id=task_id,
                project_id=project_id,
                workspace_id=workspace_id,
                chain_dependencies=config.chain_dependencies,
                requires_approval=(task.requires_approval if config.inherit_approval else False),
                base_priority=task.priority,
            )

            if created_info:
                # break_plan_into_tasks() already wired every subtask to the
                # parent with a `parent-child` edge (+ `discovered-from`
                # provenance).  The parent is AWAITING_PLAN_APPROVAL, which
                # is a *withholding* container status, so the whole set stays
                # blocked until the plan is approved — no extra `blocks` edge
                # on the first subtask needed (work-graph design §3.1).

                # Store draft subtask IDs so approve/delete/reject can find them
                import json as _json

                await self.db.add_task_context(
                    task_id,
                    type="plan_draft_subtasks",
                    label="Draft Subtask IDs",
                    content=_json.dumps(created_info),
                )

                logger.info(
                    "process_plan: supervisor created %d draft subtasks for %s",
                    len(created_info),
                    task_id,
                )

        # Build the steps list for the approval embed from supervisor-created
        # subtasks.  If supervisor didn't create any, the embed will show the
        # raw plan content only.
        parsed_steps = [{"title": t["title"], "description": ""} for t in created_info]

        # Emit plan approval notification via the event bus.
        # The DiscordNotificationHandler resolves thread URLs at delivery time.
        try:
            from src.notifications.builder import build_task_detail
            from src.notifications.events import PlanAwaitingApprovalEvent

            current_task = task if not args.get("task_id") else await self.db.get_task(task_id)
            await self.orchestrator._emit_notify(
                "notify.plan_awaiting_approval",
                PlanAwaitingApprovalEvent(
                    task=build_task_detail(current_task),
                    subtasks=parsed_steps,
                    raw_content=raw,
                    project_id=project_id,
                ),
            )
        except Exception as e:
            logger.warning("process_plan: failed to send approval notification: %s", e)

        result = {
            "status": "plan_found",
            "task_id": task_id,
            "project_id": project_id,
            "plan_path": plan_path,
            "title": task.title,
            "phases": len(parsed_steps),
            "draft_subtasks": len(created_info),
            "total_plan_files_found": len(all_plan_files),
        }
        if len(all_plan_files) > 1:
            result["note"] = (
                f"Found {len(all_plan_files)} plan file(s) total across all workspaces. "
                f"Selected the newest one. All will be cleaned up on approve/delete."
            )
        return result

    async def _cmd_set_task_status(self, args: dict) -> dict:
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

        reasons: list[Reason] = []

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

        # 4. Capacity reasons — only relevant when the task is READY.
        state = getattr(self.orchestrator, "_last_scheduler_state", None)
        if state is not None:
            ws_counts = getattr(self.orchestrator, "_last_scheduler_workspace_counts", {})
            idle = getattr(self.orchestrator, "_last_scheduler_idle_by_project", {})
            reasons.extend(build_capacity_reasons(task, state, ws_counts, idle))

        return {"success": True, "reasons": reasons}

    async def _cmd_project_ready(self, args: dict) -> dict:
        """Ready frontier for a project + withheld tasks with reasons.

        Frontier excludes ``hold:*``-labeled tasks (design §6).  Withheld
        section lists DEFINED/BLOCKED tasks and the reasons keeping them
        out of the frontier.
        """
        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"success": False, "error": "project_id is required"}
        labels = args.get("labels")
        any_label = args.get("any_label")

        try:
            frontier = await self.db.get_ready_frontier(
                str(project_id), labels=labels, any_label=any_label
            )
        except Exception as exc:
            return {"success": False, "error": str(exc)}

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
        # Optional pre-routing: control-plane tasks skip triage, so the
        # ensuring pipeline may pin the executing profile directly (e.g.
        # the default pipeline pins 'triage' on the triage task).
        if args.get("profile_id"):
            create_args["profile_id"] = args["profile_id"]
        result = await self._cmd_create_task(create_args)
        if "error" in result:
            return {"success": False, "error": result["error"]}
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

        dv2 phase 1 — the ONLY resolver for ``routing`` gates.  Writes
        ``profile_id``, optional ``intelligence_class`` and optional
        ``preferred_workspace_id`` onto the task, then resolves every open
        ``routing`` gate attached to the task via the orchestrator helper
        (so ``gate.resolved`` + blocked-flip bus events fire the same way
        the sweep path emits them).

        Args:
            task_id: Target task id (required).
            profile_id: AgentProfile id (required).
            intelligence_class: Optional vault class id.  Falls back to
                ``profile.default_class`` when omitted; ``None``/empty
                skips class validation entirely.
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

        cls_id = args.get("intelligence_class") or (profile.default_class or None)
        if cls_id:
            from src.intelligence_classes import load_intelligence_classes, resolve_class

            classes = load_intelligence_classes(self.config.data_dir)
            cls = classes.get(cls_id)
            if cls is None:
                return {
                    "success": False,
                    "error": f"intelligence class '{cls_id}' not found in vault",
                }
            provider = _harness_provider(profile.harness)
            if provider and not resolve_class(cls, provider):
                return {
                    "success": False,
                    "error": (
                        f"intelligence class '{cls_id}' has no mapping for "
                        f"provider '{provider}' (required by profile "
                        f"'{profile.id}' harness '{profile.harness}')"
                    ),
                }

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

        await self.db.update_task_routing(
            str(task_id),
            profile_id=str(profile_id),
            intelligence_class=cls_id,
            preferred_workspace_id=str(workspace_id) if workspace_id else None,
        )

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
    return {"claude": "anthropic", "codex": "openai", "gemini": "google"}.get(
        (harness or "").strip(), ""
    )
