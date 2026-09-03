"""Hand-crafted task CLI commands that require interactive features.

Simple list/detail commands are auto-generated with Rich formatters via
the formatter registry.  This file only contains commands that need
interactive prompts (wizard, confirmation dialogs, fuzzy search).
"""

from __future__ import annotations

import json
from typing import Any

import click

from .app import cli, console, _run, _get_client, _handle_errors
from .claim_epoch import claim_epoch_option, resolve_claim_epoch
from .envelope import emit


def _getval(obj: Any, key: str, default: Any = None) -> Any:
    """Get a value from a typed response or dict, normalising Unset → default."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    val = getattr(obj, key, default)
    if type(val).__name__ == "Unset":
        return default
    return val


@cli.group()
def task() -> None:
    """Task management commands."""
    pass


def _load_graph_document(graph_file: str) -> dict:
    """Read a ``--graph`` argument into a decoded document.

    ``-`` reads stdin, which is how an agent pipes a graph it just composed.
    JSON is tried first, then YAML, matching ``parse_graph(fmt="auto")``.
    """
    import json

    import yaml

    from src.task_graph.parser import MAX_GRAPH_DOCUMENT_CHARS

    if graph_file == "-":
        text = click.get_text_stream("stdin").read()
    else:
        try:
            with open(graph_file, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise click.UsageError(f"could not read graph file {graph_file!r}: {exc}") from exc

    if len(text) > MAX_GRAPH_DOCUMENT_CHARS:
        raise click.UsageError(
            f"graph document is {len(text)} characters, over the {MAX_GRAPH_DOCUMENT_CHARS} limit"
        )

    # RecursionError is caught alongside the parse errors: this is the one
    # decode that happens client-side, so a deeply nested document piped to
    # `--graph -` has no daemon-side net to fall into.
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise click.UsageError(f"graph document is neither valid JSON nor YAML: {exc}") from exc
        except RecursionError as exc:
            raise click.UsageError("graph document nesting is too deep to parse") from exc
    if not isinstance(data, dict):
        raise click.UsageError(f"graph document must be an object, got {type(data).__name__}")
    return data


def _create_task_graph(
    ctx: click.Context,
    *,
    project: str | None,
    graph_file: str | None,
    from_spec: str | None,
    dry_run: bool,
    parent_id: str | None = None,
    profile_id: str | None = None,
    intelligence_class: str | None = None,
) -> None:
    """Back ``aq task create --graph|--from-spec|--dry-run``."""
    if graph_file and from_spec:
        raise click.UsageError("--graph and --from-spec are mutually exclusive")

    api_url = ctx.obj.get("api_url") if ctx.obj else None
    params: dict[str, Any] = {"dry_run": dry_run}
    if project:
        params["project_id"] = project
    if graph_file:
        params["graph"] = _load_graph_document(graph_file)
    else:
        params["spec_path"] = from_spec
    if parent_id:
        params["parent_id"] = parent_id
    if profile_id:
        params["profile_id"] = profile_id
    if intelligence_class:
        params["intelligence_class"] = intelligence_class

    async def _create():
        async with _get_client(api_url) as client:
            return await client.execute("create_task_graph", params)

    result = _run(_create())

    def _render(data: dict) -> None:
        from rich.table import Table

        verb = "Would create" if data.get("dry_run") else "Created"
        console.print(
            f"[bold green]{verb} graph[/] under "
            f"[bold bright_cyan]{data.get('parent_id')}[/] — {data.get('parent_title', '')}"
        )
        nodes = data.get("nodes", [])
        if nodes:
            table = Table(border_style="bright_black")
            table.add_column("Key", style="bold bright_cyan")
            table.add_column("Task ID")
            table.add_column("Title")
            table.add_column("Needs")
            for node in nodes:
                needs = ", ".join(f"{n['on']}({n['dep_type']})" for n in node.get("needs", []))
                table.add_row(
                    node.get("key", ""),
                    node.get("task_id", ""),
                    node.get("title", ""),
                    needs or "-",
                )
            console.print(table)
        for warning in data.get("warnings", []):
            # No square brackets around the rule name: Rich would read them
            # as markup and swallow the text.
            where = f" ({warning['node']})" if warning.get("node") else ""
            console.print(
                f"[yellow]warning[/] {warning.get('rule')}{where}: {warning.get('detail')}"
            )

    emit(ctx, result, render=_render)


@task.command("create")
@click.option("-p", "--project", default=None, help="Project ID (skips wizard step)")
@click.option("-t", "--title", default=None, help="Task title (skips wizard step)")
@click.option("-d", "--description", default=None, help="Task description")
@click.option("--priority", default=None, type=int, help="Priority (1-300)")
@click.option("--type", "task_type", default=None, help="Task type")
@click.option(
    "--integration-mode",
    "integration_mode",
    type=click.Choice(["direct", "pull_request"]),
    default=None,
    help="Integration policy override (omit to inherit the project/system policy)",
)
@click.option(
    "-P",
    "--profile",
    "profile_id",
    default=None,
    help="Agent profile id (e.g. claude-opus, claude-sonnet, claude-code)",
)
@click.option(
    "--intelligence-class",
    default=None,
    help="Intelligence class id (e.g. deep-high); also fills missing graph node classes",
)
@click.option(
    "--agent-type",
    default=None,
    help="Agent type override (cascade falls back to the global profile of this name)",
)
@click.option(
    "--graph",
    "graph_file",
    default=None,
    help="Create a whole task graph from a JSON/YAML document ('-' reads stdin)",
)
@click.option(
    "--from-spec",
    "from_spec",
    default=None,
    help="Create a task graph from a vault spec's fenced aq-graph block",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="With --graph/--from-spec: validate and report, create nothing",
)
@click.option(
    "--parent",
    "parent_id",
    default=None,
    help=(
        "Create under this container (single task or graph). Worker filings default "
        "to the held task's own parent, so a sibling under the same epic needs no flag"
    ),
)
@click.option(
    "--root",
    is_flag=True,
    default=False,
    help="For a worker filing: create at project root instead of beside the held task",
)
@click.option(
    "--reason",
    default=None,
    help=(
        "WHY this task was spawned; required for worker-filed tasks and "
        "stored on the edge back to its origin"
    ),
)
@click.option(
    "--deliverable",
    "deliverables",
    multiple=True,
    help="Plan item JSON with id, kind, and target; repeatable.",
)
@click.pass_context
@_handle_errors
def task_create(
    ctx: click.Context,
    project: str | None,
    title: str | None,
    description: str | None,
    priority: int | None,
    task_type: str | None,
    integration_mode: str | None,
    profile_id: str | None,
    intelligence_class: str | None,
    agent_type: str | None,
    graph_file: str | None,
    from_spec: str | None,
    dry_run: bool,
    parent_id: str | None,
    root: bool,
    reason: str | None,
    deliverables: tuple[str, ...],
) -> None:
    """Create a new task (interactive wizard or via flags).

    Use ``--profile`` / ``-P`` to pin a specific agent profile (model +
    tools + system prompt). Use ``--agent-type`` to pick the scope the
    task runs under when no explicit profile is given.

    ``--graph FILE`` / ``--from-spec PATH`` create a whole dependency graph
    in one transaction instead of a single task; add ``--dry-run`` to see the
    validation report and the ids that would be assigned.
    """
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    if root and parent_id:
        raise click.UsageError("--root and --parent are mutually exclusive")
    if root and (graph_file or from_spec):
        raise click.UsageError("--root only applies to single-task creation")
    if graph_file or from_spec:
        _create_task_graph(
            ctx,
            project=project,
            graph_file=graph_file,
            from_spec=from_spec,
            dry_run=dry_run,
            parent_id=parent_id,
            profile_id=profile_id,
            intelligence_class=intelligence_class,
        )
        return
    if dry_run:
        raise click.UsageError("--dry-run only applies with --graph or --from-spec")

    if project and title and description:
        params = {
            "project_id": project,
            "title": title,
            "description": description,
            "priority": priority or 100,
            "task_type": task_type,
        }
        if integration_mode:
            params["integration_mode"] = integration_mode
        if profile_id:
            params["profile_id"] = profile_id
        if agent_type:
            params["agent_type"] = agent_type
    else:
        from .menus import task_creation_wizard

        async def _get_projects():
            async with _get_client(api_url) as client:
                result = await client.execute("list_projects")
                projects = _getval(result, "projects", [])
                return [_getval(p, "id") for p in projects]

        project_ids = _run(_get_projects())
        params = task_creation_wizard(project_ids)
        if not params:
            console.print("[dim]Task creation cancelled.[/]")
            return
        # CLI flag overrides persist through the wizard if the caller
        # mixed interactive + flag usage.
        if profile_id and "profile_id" not in params:
            params["profile_id"] = profile_id
        if agent_type and "agent_type" not in params:
            params["agent_type"] = agent_type

    if intelligence_class:
        params["intelligence_class"] = intelligence_class

    if parent_id and "parent_id" not in params:
        params["parent_id"] = parent_id
    if root:
        params["root"] = True
    if reason and "reason" not in params:
        params["reason"] = reason
    if deliverables:
        try:
            parsed = [json.loads(value) for value in deliverables]
        except json.JSONDecodeError as exc:
            raise click.UsageError(f"--deliverable must be a JSON object: {exc.msg}") from exc
        if not all(isinstance(item, dict) for item in parsed):
            raise click.UsageError("--deliverable must be a JSON object")
        params["deliverables"] = parsed

    async def _create():
        async with _get_client(api_url) as client:
            return await client.execute("create_task", params)

    result = _run(_create())
    task_id = _getval(result, "created", "?")
    console.print()
    console.print(f"[bold green]Task created:[/] [bold bright_cyan]{task_id}[/]")
    title = _getval(result, "title")
    if title:
        console.print(f"  [dim]{title}[/]")


@task.command("stop")
@click.argument("task_id")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@click.pass_context
@_handle_errors
def task_stop(ctx: click.Context, task_id: str, yes: bool) -> None:
    """Stop a running task."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    if not yes:
        from .menus import confirm

        if not confirm(f"Stop task '{task_id}'? This will mark it as FAILED."):
            console.print("[dim]Cancelled.[/]")
            return

    async def _stop():
        async with _get_client(api_url) as client:
            return await client.execute("stop_task", {"task_id": task_id})

    result = _run(_stop())
    console.print(f"[bold yellow]Task stopped:[/] {_getval(result, 'stopped', task_id)}")


@task.command("restart")
@click.argument("task_id")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@click.pass_context
@_handle_errors
def task_restart(ctx: click.Context, task_id: str, yes: bool) -> None:
    """Restart a failed or stopped task."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    if not yes:
        from .menus import confirm

        if not confirm(f"Restart task '{task_id}'?"):
            console.print("[dim]Cancelled.[/]")
            return

    async def _restart():
        async with _get_client(api_url) as client:
            return await client.execute("restart_task", {"task_id": task_id})

    result = _run(_restart())
    console.print(f"[bold green]Task restarted:[/] {_getval(result, 'restarted', task_id)}")


@task.command("search")
@click.argument("query")
@click.option("-p", "--project", default=None, help="Limit search to project")
@click.pass_context
@_handle_errors
def task_search(ctx: click.Context, query: str, project: str | None) -> None:
    """Search tasks by title or description."""
    from .adapters import task_proxy
    from .formatters import format_task_table

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _search():
        async with _get_client(api_url) as client:
            args = {"include_completed": True}
            if project:
                args["project_id"] = project
            return await client.execute("list_tasks", args)

    result = _run(_search())

    q = query.lower()
    raw_tasks = _getval(result, "tasks", [])
    matched = [
        t
        for t in raw_tasks
        if q in (_getval(t, "title", "")).lower() or q in (_getval(t, "description", "")).lower()
    ]
    tasks = [task_proxy(t) for t in matched]

    title = f"Search results for '{query}'"
    if project:
        title += f" in {project}"

    table = format_task_table(tasks, title=title)
    console.print(table)

    if not tasks:
        console.print("[dim]No tasks matched your search.[/]")


@task.command("select")
@click.option("-p", "--project", default=None, help="Filter by project")
@click.pass_context
@_handle_errors
def task_select(ctx: click.Context, project: str | None) -> None:
    """Interactively select a task and show its details."""
    from .adapters import task_proxy
    from .formatters import format_task_detail
    from .menus import fuzzy_select_task

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _select():
        async with _get_client(api_url) as client:
            args = {}
            if project:
                args["project_id"] = project
            result = await client.execute("list_tasks", args)
            raw_tasks = _getval(result, "tasks", [])
            tasks = [task_proxy(t) for t in raw_tasks]

            if not tasks:
                console.print("[dim]No active tasks found.[/]")
                return

            selected = fuzzy_select_task(tasks, prompt_text="Select task (ID or search): ")
            if not selected:
                console.print("[dim]No task selected.[/]")
                return

            detail = await client.execute("get_task", {"task_id": selected.id})
            t = task_proxy(detail)
            deps_raw = _getval(detail, "depends_on", [])
            blocks_raw = _getval(detail, "blocks", [])
            deps_on = [d.id if hasattr(d, "id") else d["id"] for d in deps_raw]
            dependents = [d.id if hasattr(d, "id") else d["id"] for d in blocks_raw]
            panel = format_task_detail(t, deps_on=deps_on, dependents=dependents)
            console.print(panel)

    _run(_select())


# ---------------------------------------------------------------------------
# show / set / list / details — aq-surface Phase S0 (output contract)
#
# Hand-crafted (not auto-generated) so they can route through `emit()` for
# the versioned JSON envelope + --brief projection. `list` shadows the
# auto-generated `list_tasks`-backed command (same backend command, nicer
# front end); `show`/`details` are new, backed by the new `task_show`
# CommandHandler command; `set` is new, backed by `task_set`. See
# docs/specs/implementation/aq-surface.md §5.3 / §9.
# ---------------------------------------------------------------------------


@task.command("list")
@click.option("-p", "--project", "project_id", default=None, help="Filter by project")
@click.option("--status", default=None, help="Filter by status (see `aq schema`)")
@click.option(
    "--include-completed",
    is_flag=True,
    default=False,
    help="Include COMPLETED/FAILED/BLOCKED tasks (hidden by default)",
)
@click.pass_context
@_handle_errors
def task_list(
    ctx: click.Context,
    project_id: str | None,
    status: str | None,
    include_completed: bool,
) -> None:
    """List tasks."""
    from .adapters import task_proxy
    from .formatters import format_task_table

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _list():
        async with _get_client(api_url) as client:
            args: dict[str, Any] = {}
            if project_id:
                args["project_id"] = project_id
            if status:
                args["status"] = status
            if include_completed:
                args["include_completed"] = True
            return await client.execute("list_tasks", args)

    result = _run(_list())
    raw_tasks = _getval(result, "tasks", [])
    total = _getval(result, "total", len(raw_tasks))

    def _render(data: list) -> None:
        proxied = [task_proxy(t) for t in data]
        table = format_task_table(proxied)
        console.print(table)
        if not proxied:
            console.print("[dim]No tasks found.[/]")

    emit(ctx, raw_tasks, entity="task", total=total, render=_render)


@task.command("show")
@click.argument("task_id")
@click.pass_context
@_handle_errors
def task_show(ctx: click.Context, task_id: str) -> None:
    """Show full task detail: fields, dependencies, context, labels."""
    from .adapters import task_proxy
    from .formatters import format_task_detail

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _show():
        async with _get_client(api_url) as client:
            return await client.execute("task_show", {"task_id": task_id})

    result = _run(_show())

    def _render(data: dict) -> None:
        t = task_proxy(data)
        deps_raw = _getval(data, "depends_on", [])
        blocks_raw = _getval(data, "blocks", [])
        deps_on = [d.get("id") if isinstance(d, dict) else d for d in deps_raw]
        dependents = [d.get("id") if isinstance(d, dict) else d for d in blocks_raw]
        panel = format_task_detail(t, deps_on=deps_on, dependents=dependents)
        console.print(panel)

        for edge in deps_raw:
            if isinstance(edge, dict) and edge.get("reason"):
                console.print(
                    f"  [dim]{edge.get('dep_type') or 'dependency'} -> "
                    f"{edge.get('id')}:[/] {edge['reason']}"
                )

        for edge in _getval(data, "provenance", []):
            if not isinstance(edge, dict):
                continue
            line = f"[dim]Spawned from:[/] {edge.get('id')} [dim]\\[{edge.get('dep_type')}][/]"
            if edge.get("reason"):
                line += f" — {edge['reason']}"
            console.print(line)

        labels = _getval(data, "labels", [])
        if labels:
            console.print(f"[dim]Labels:[/] {', '.join(labels)}")
        context = _getval(data, "context", [])
        if context:
            console.print(f"[dim]Context entries:[/] {len(context)}")

    emit(ctx, result, entity="task", render=_render)


@task.command("details")
@click.argument("task_id")
@click.pass_context
def task_details_alias(ctx: click.Context, task_id: str) -> None:
    """Alias of `aq task show` (kept for backward compatibility)."""
    ctx.invoke(task_show, task_id=task_id)


@task.command("set")
@click.argument("task_id")
@click.option("--branch", default=None, help="Set the task's branch name")
@click.option("--pr-url", default=None, help="Set the task's PR URL")
@click.option("--work-dir", default=None, help="Record the task's working directory")
@click.option("--note", default=None, help="Append a legacy note to task context")
@click.option("--description", default=None, help="Update findings while preserving the task requirements")
@click.option("--expected-description", default=None, help="Only update if the current description matches this value")
@click.option(
    "--label",
    "labels",
    multiple=True,
    metavar="+LABEL|-LABEL",
    help="Add (+label) or remove (-label) a label; repeatable.",
)
@click.option(
    "--meta",
    "meta_kv",
    multiple=True,
    metavar="KEY=VALUE",
    help="Set a metadata key; repeatable.",
)
@claim_epoch_option
@click.pass_context
@_handle_errors
def task_set(
    ctx: click.Context,
    task_id: str,
    branch: str | None,
    pr_url: str | None,
    work_dir: str | None,
    note: str | None,
    description: str | None,
    expected_description: str | None,
    labels: tuple[str, ...],
    meta_kv: tuple[str, ...],
    claim_epoch: int | None,
) -> None:
    """Work-state writes: findings, branch, PR URL, work dir, notes, labels, metadata.

    Never changes task status — use `aq task stop|restart` for that.
    """
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    labels_add: list[str] = []
    labels_remove: list[str] = []
    for entry in labels:
        if entry.startswith("-"):
            labels_remove.append(entry[1:])
        elif entry.startswith("+"):
            labels_add.append(entry[1:])
        else:
            labels_add.append(entry)

    meta: dict[str, str] = {}
    for kv in meta_kv:
        if "=" not in kv:
            console.print(f"[bold red]Error:[/] --meta expects KEY=VALUE, got '{kv}'")
            raise SystemExit(2)
        key, _, value = kv.partition("=")
        meta[key] = value

    args: dict[str, Any] = {"task_id": task_id}
    if branch is not None:
        args["branch"] = branch
    if pr_url is not None:
        args["pr_url"] = pr_url
    if work_dir is not None:
        args["work_dir"] = work_dir
    if note is not None:
        args["note"] = note
    if description is not None:
        args["description"] = description
    if expected_description is not None:
        args["expected_description"] = expected_description
    if labels_add:
        args["labels_add"] = labels_add
    if labels_remove:
        args["labels_remove"] = labels_remove
    if meta:
        args["meta"] = meta
    resolved_epoch = resolve_claim_epoch(claim_epoch)
    if resolved_epoch is not None:
        args["claim_epoch"] = resolved_epoch

    async def _set():
        async with _get_client(api_url) as client:
            return await client.execute("task_set", args)

    result = _run(_set())

    def _render(data: dict) -> None:
        changed = _getval(data, "fields_changed", [])
        console.print(f"[bold green]Task updated:[/] {task_id}")
        if changed:
            console.print(f"  [dim]Fields:[/] {', '.join(changed)}")

    emit(ctx, result, entity="task", render=_render)


@task.command("comment")
@click.argument("task_id")
@click.option("--body", required=True, help="Progress, evidence, a decision, or a blocker (up to 16,000 characters)")
@claim_epoch_option
@click.pass_context
@_handle_errors
def task_comment(ctx: click.Context, task_id: str, body: str, claim_epoch: int | None) -> None:
    """Append an attributed comment to a task's durable history."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None
    args: dict[str, Any] = {"task_id": task_id, "body": body}
    epoch = resolve_claim_epoch(claim_epoch)
    if epoch is not None:
        args["claim_epoch"] = epoch

    async def _comment():
        async with _get_client(api_url) as client:
            return await client.execute("task_comment", args)

    def _render(data: dict) -> None:
        console.print(f"Comment added to {task_id}", markup=False)

    emit(ctx, _run(_comment()), entity="task_comment", render=_render)


@task.command("comments")
@click.argument("task_id")
@click.option("--limit", default=50, type=click.IntRange(1, 200), show_default=True)
@click.option("--offset", default=0, type=click.IntRange(min=0), show_default=True)
@click.pass_context
@_handle_errors
def task_comments(ctx: click.Context, task_id: str, limit: int, offset: int) -> None:
    """Read a task's comments, newest first."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _comments():
        async with _get_client(api_url) as client:
            return await client.execute("task_comments", {"task_id": task_id, "limit": limit, "offset": offset})

    def _render(data: dict) -> None:
        from datetime import datetime, timezone

        rows = _getval(data, "comments", [])
        console.print(f"{len(rows)} comments shown; {_getval(data, 'total', len(rows))} total (offset {offset})", markup=False)
        for row in rows:
            timestamp = _getval(row, "created_at", "")
            if isinstance(timestamp, (int, float)):
                timestamp = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
            author = f"{_getval(row, 'author_kind', '')}:{_getval(row, 'author_id', '')}"
            console.print(f"\n{author} · {timestamp}", markup=False)
            console.print(_getval(row, "body", ""), markup=False, highlight=False)

    emit(ctx, _run(_comments()), entity="task_comments", render=_render)
