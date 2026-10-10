"""Hand-crafted project CLI commands that need composite logic or UX sugar.

Simple list commands are auto-generated with Rich formatters via the
formatter registry.  This file only contains commands that compose
multiple API calls or provide friendly key aliasing.
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

import click

from .app import cli, console, _run, _get_client, _handle_errors
from .envelope import emit, reject_json_mode


def _getval(obj: Any, key: str, default: Any = None) -> Any:
    """Get a value from a typed response or dict, normalising Unset → default."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    val = getattr(obj, key, default)
    if type(val).__name__ == "Unset":
        return default
    return val


@cli.group()
def project() -> None:
    """Project management commands."""
    pass


@project.command("create")
@click.option("--name", required=True)
@click.option(
    "--from-policy", type=click.Path(exists=True), help="Open policy item selection after creation."
)
@click.option("--repo-url")
@click.option("--default-branch")
@click.option("--create-repo")
@click.option("--private/--public", default=True)
@click.option("--root-id")
@click.option("--relative-path")
@click.option("--request-id")
@click.option("--credit-weight", type=float, default=1.0)
@click.option("--max-concurrent-agents", type=int, default=2)
@click.pass_context
@_handle_errors
def project_create(ctx, name, from_policy, **options):
    from src.cli import app
    from src.cli.policy import select_and_apply

    if from_policy:
        reject_json_mode(ctx, "project create --from-policy", "policy selection is interactive")

    async def create():
        async with app._get_client((ctx.obj or {}).get("api_url")) as client:
            return await client.execute(
                "create_project",
                {
                    "name": name,
                    **{key: value for key, value in options.items() if value is not None},
                },
            )

    result = app._run(create())
    emit(
        ctx,
        result,
        render=lambda data: console.print(f"Created project {data['created']}", markup=False),
    )
    if from_policy and result.get("success") is not False:
        select_and_apply(ctx, from_policy, result["created"])


@project.command("onboard")
@click.option("--request-id", help="Durable idempotency key (generated when omitted).")
@click.option(
    "--source-mode",
    type=click.Choice(["link", "init", "github_clone"]),
    default="link",
    show_default=True,
)
@click.option("--root-id", required=True, help="Configured project root id.")
@click.option("--relative-path", required=True, help="Destination relative to the root.")
@click.option("--project-name", required=True, help="Human-readable project name.")
@click.option("--project-id", required=True, help="Normalized project slug.")
@click.option("--default-branch", help="Detected/default branch override.")
@click.option("--create-readme/--no-create-readme", default=True, show_default=True)
@click.option("--create-github/--no-create-github", default=False, show_default=True)
@click.option("--github-owner")
@click.option("--github-repo")
@click.option(
    "--github-visibility",
    type=click.Choice(["private", "public"]),
    default="private",
    show_default=True,
)
@click.option("--github-repository", help="Discovered GitHub repository as OWNER/NAME.")
@click.option("--github-url", help="GitHub HTTPS/SSH URL or OWNER/NAME shorthand.")
@click.option("--repo-url", help="init: adopt an existing empty remote.")
@click.option("--create-repo", help="init: create OWNER/NAME on GitHub.")
@click.option("--private/--public", "private_repo", default=True)
@click.pass_context
@_handle_errors
def project_onboard(
    ctx: click.Context,
    request_id: str | None,
    source_mode: str,
    root_id: str,
    relative_path: str,
    project_name: str,
    project_id: str,
    default_branch: str | None,
    create_readme: bool,
    create_github: bool,
    github_owner: str | None,
    github_repo: str | None,
    github_visibility: str,
    github_repository: str | None,
    github_url: str | None,
    repo_url: str | None,
    create_repo: str | None,
    private_repo: bool,
) -> None:
    """Link, initialize, or clone a repository and register its AQ project."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None
    args: dict[str, Any] = {
        "request_id": request_id or str(uuid4()),
        "source_mode": source_mode,
        "root_id": root_id,
        "relative_path": relative_path,
        "project_name": project_name,
        "project_id": project_id,
        "default_branch": default_branch,
    }
    if (repo_url or create_repo) and source_mode != "init":
        raise click.UsageError("--repo-url and --create-repo require --source-mode init")
    if repo_url and (create_repo or create_github):
        raise click.UsageError("Choose --repo-url or --create-repo")
    if create_repo:
        if create_repo.count("/") != 1:
            raise click.UsageError("--create-repo must be OWNER/NAME")
        github_owner, github_repo = create_repo.split("/")
        create_github = True
        github_visibility = "private" if private_repo else "public"
    if source_mode == "init":
        if repo_url:
            args["repo_url"] = repo_url
        args.update(create_readme=create_readme, create_github=create_github)
        if create_github:
            args["github_owner"] = github_owner
            if github_repo is not None:
                args["github_repo"] = github_repo
            args["github_visibility"] = github_visibility
    elif source_mode == "github_clone":
        if github_repository:
            try:
                owner, name = github_repository.split("/", 1)
            except ValueError as exc:
                raise click.UsageError("--github-repository must be OWNER/NAME") from exc
            args["github_repository"] = {"owner": owner, "name": name}
        if github_url:
            args["github_url"] = github_url

    async def _onboard():
        async with _get_client(api_url) as client:
            return await client.execute("onboard_project", args)

    result = _run(_onboard())
    emit(
        ctx,
        result,
        render=lambda data: console.print(
            "[green]Onboarded[/] "
            f"[bold cyan]{_getval(data, 'project_id')}[/] "
            f"(workspace {_getval(data, 'workspace_id')})\n"
            f"[dim]{_getval(data, 'canonical_path')}[/]"
        ),
    )


@project.command("bind-repository")
@click.argument("project_id")
@click.option("--repo-url", required=True, help="GitHub repository to authorize.")
@click.option(
    "--expected-repo-url", required=True, help="Exact stored URL; use '' for first binding.",
)
@click.option("--reason", required=True, help="Operator authorization and audit reason.")
@click.pass_context
@_handle_errors
def project_bind_repository(
    ctx: click.Context, project_id: str, repo_url: str, expected_repo_url: str, reason: str,
) -> None:
    """Bind an initialized project's first repository (operator/global supervisor only)."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _bind():
        async with _get_client(api_url) as client:
            return await client.execute("bind_project_repository", {
                "project_id": project_id, "repo_url": repo_url,
                "expected_repo_url": expected_repo_url, "reason": reason,
            })

    emit(ctx, _run(_bind()), render=lambda data: console.print(
        f"[green]Repository authorized[/] for [bold cyan]{_getval(data, 'project_id')}[/]: "
        f"{_getval(data, 'repo_url')}"
    ))


@project.command("details")
@click.argument("project_id")
@click.pass_context
@_handle_errors
def project_details(ctx: click.Context, project_id: str) -> None:
    """Show detailed information about a project with task breakdown."""
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    from .styles import STATUS_ICONS, STATUS_STYLES

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _details():
        async with _get_client(api_url) as client:
            proj_result = await client.execute("list_projects")
            task_result = await client.execute(
                "list_tasks",
                {
                    "project_id": project_id,
                    "include_completed": True,
                },
            )
            return proj_result, task_result

    proj_result, task_result = _run(_details())

    p = None
    for proj in _getval(proj_result, "projects", []):
        if _getval(proj, "id") == project_id:
            p = proj
            break

    if not p:
        from .exceptions import CommandError

        raise CommandError("project_details", f"Project not found: {project_id}")

    tasks = _getval(task_result, "tasks", [])
    data = {"project": p, "tasks": tasks}

    def _render(_data) -> None:
        status = (_getval(p, "status", "ACTIVE") or "ACTIVE").upper()
        status_style = "green" if status == "ACTIVE" else "dim"
        lines = [Text(f"Status: {status}", style=status_style), Text("")]
        fields = [
            ("Name", _getval(p, "name", "—")),
            ("Max Agents", str(_getval(p, "max_concurrent_agents", "—"))),
            ("Credit Weight", str(_getval(p, "credit_weight", "—"))),
        ]
        for label, value in fields:
            line = Text()
            line.append(f"  {label}: ", style="bold cyan")
            line.append(value, style="white")
            lines.append(line)

        counts: dict[str, int] = {}
        for t in tasks:
            s = (_getval(t, "status", "UNKNOWN") or "UNKNOWN").upper()
            counts[s] = counts.get(s, 0) + 1

        lines.append(Text(""))
        lines.append(Text("  Tasks:", style="bold cyan"))
        for status_name, count in sorted(counts.items(), key=lambda x: -x[1]):
            if count == 0:
                continue
            icon = STATUS_ICONS.get(status_name, "o")
            sty = STATUS_STYLES.get(status_name, "white")
            tl = Text()
            tl.append(f"    {icon} {status_name}: ", style=sty)
            tl.append(str(count))
            lines.append(tl)
        panel = Panel(
            Group(*lines),
            title=Text(f"Project: {project_id}", style="bold bright_white"),
            border_style="bright_magenta",
            padding=(1, 2),
        )
        console.print(panel)

    emit(ctx, data, legacy_data={"project": p, "tasks": tasks}, render=_render)


def _git_identity_args(value: str) -> dict[str, str]:
    """``Name <email>`` -> the edit_project pair; ``inherit`` clears both."""
    text = value.strip()
    if text.lower() in ("inherit", "clear", "none", "default", ""):
        return {"git_identity_name": "", "git_identity_email": ""}
    match = re.fullmatch(r"(.+?)\s*<([^<>]+)>", text)
    if not match:
        raise click.UsageError('git-identity must be "Name <email>" or inherit')
    return {"git_identity_name": match.group(1).strip(), "git_identity_email": match.group(2).strip()}


@project.command("set")
@click.argument("project_id")
@click.argument("key")
@click.argument("value")
@click.option(
    "--expected-integration-generation",
    type=click.IntRange(min=0),
    help="Required CAS generation for integration configuration changes.",
)
@click.option("--reason", help="Operator audit reason for an integration configuration change.")
@click.pass_context
@_handle_errors
def project_set(
    ctx: click.Context,
    project_id: str,
    key: str,
    value: str,
    expected_integration_generation: int | None,
    reason: str | None,
) -> None:
    """Set a project property. e.g. aq project set myproj max-agents 4

    \b
    `router <playbook-id>` re-binds the project to another routing playbook
    (local operator only). The playbook must be active and grant
    task_route_apply. A project has no default profile: its router routes
    every task.

    `git-identity "Name <email>"` overrides the Git commit identity of this
    project's AQ-authored commits; `git-identity inherit` resets it to the
    installation default (`aq system get-git-identity`).

    `promotion-flow <file>` activates a YAML or JSON promotion flow (local
    operator only; `clear` removes it). It validates every layer first and
    creates missing chain targets with create-only pushes; any refusal
    writes nothing. Check it first with `aq promote validate --file`.
    """
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    KEY_MAP = {
        "name": "name",
        "max-agents": "max_concurrent_agents",
        "credit-weight": "credit_weight",
        "budget-limit": "budget_limit",
        "branch": "default_branch",
        "router": "assignment_playbook_id",
        "integration-repository": "integration_repository",
        "integration-repository-id": "integration_repository_id",
        "integration-policy": "hierarchical_integration_policy",
        "integration-mode": "hierarchical_integration_mode",
        "integration-review-mode": "integration_mode",
        "review-delegate-to": "review_delegate_to",
        "git-identity": "git_identity",
        "promotion-flow": "promotion_flow",
    }

    field = KEY_MAP.get(key)
    if not field:
        raise click.UsageError(f"Unknown key {key!r}; allowed: {', '.join(sorted(KEY_MAP))}")

    coerced: str | int | float | dict | None = value
    if field == "max_concurrent_agents":
        coerced = int(value)
    elif field == "credit_weight":
        coerced = float(value)
    elif field == "budget_limit":
        coerced = None if value.lower() in ("none", "null", "unlimited") else int(value)
    elif field == "review_delegate_to":
        # Local-operator only; empty clears the delegation back to the default.
        lowered = value.lower()
        if lowered in ("clear", "none", "null", ""):
            coerced = None
        elif lowered in ("user", "supervisor"):
            coerced = lowered
        else:
            raise click.UsageError("review-delegate-to must be user, supervisor, or clear")
    elif field == "integration_repository_id":
        coerced = None if value.lower() in ("none", "null", "clear") else value
    elif field == "integration_repository":
        try:
            coerced = json.loads(value)
        except json.JSONDecodeError as exc:
            raise click.UsageError(
                "integration-repository must be a valid JSON object"
            ) from exc
        if not isinstance(coerced, dict):
            raise click.UsageError("integration-repository must be a valid JSON object")
    elif field == "integration_mode":
        if value != "pull_request":
            raise click.UsageError("integration-review-mode must be pull_request")
        coerced = value
    elif field == "hierarchical_integration_policy":
        if value.lower() in ("none", "null", "clear"):
            coerced = None
        else:
            try:
                coerced = json.loads(value)
            except json.JSONDecodeError as exc:
                raise click.UsageError("integration-policy must be a valid JSON object") from exc
            if not isinstance(coerced, dict):
                raise click.UsageError("integration-policy must be a valid JSON object")
    elif field == "promotion_flow":
        if value.lower() in ("none", "null", "clear"):
            coerced = None
        else:
            from pathlib import Path

            import yaml

            try:
                coerced = yaml.safe_load(Path(value).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, yaml.YAMLError) as exc:
                from .envelope import emit_error

                emit_error("flow_schema_invalid", str(exc), {"pointer": "", "layer": 1})
                raise SystemExit(1) from exc

    if field == "default_branch":
        cmd = "set_default_branch"
        args = {"project_id": project_id, "branch": value}
    elif field == "git_identity":
        cmd = "edit_project"
        args = {"project_id": project_id, **_git_identity_args(value)}
    else:
        cmd = "edit_project"
        args = {"project_id": project_id, field: coerced}

    sensitive = field in {
        "integration_repository",
        "integration_repository_id",
        "hierarchical_integration_policy",
        "hierarchical_integration_mode",
        "integration_mode",
        "promotion_flow",
    }
    if sensitive:
        if expected_integration_generation is None:
            raise click.UsageError(
                "--expected-integration-generation is required for integration configuration"
            )
        args["expected_integration_generation"] = expected_integration_generation
        if reason is not None:
            args["reason"] = reason

    async def _set():
        async with _get_client(api_url) as client:
            return await client.execute(cmd, args)

    result = _run(_set())
    if sensitive:
        emit(ctx, result, entity="integration")
        if field == "promotion_flow" and result.get("success") is False:
            raise SystemExit(1)
        return
    emit(
        ctx,
        result,
        render=lambda _data: console.print(
            f"[green]Updated[/] {project_id} [bold cyan]{key}[/] = {value}"
        ),
    )
