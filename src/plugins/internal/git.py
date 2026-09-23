"""Internal plugin: git operations (status, commit, push, pull, branch, merge, PR, etc.).

Extracted from ``CommandHandler._cmd_git_*`` and related alias commands.
The largest internal plugin — 19 commands covering all git operations.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from src.commands.principal import ExecutionPrincipal, PrincipalKind, current_principal
from src.plugins.base import InternalPlugin, PluginContext


# ---------------------------------------------------------------------------
# Tool definitions — loaded lazily to avoid a huge module-level constant.
# The actual definitions are in _build_tool_definitions() below.
# ---------------------------------------------------------------------------

TOOL_CATEGORY = "git"

_EXPECTED_REMOTE_OID_DESCRIPTION = (
    "Exact remote branch OID expected before the push (40 hex digits); the push "
    "may then rewrite the branch, and is refused if the remote holds anything "
    "else. All zeros means the branch must still be absent."
)


def _build_tool_definitions() -> list[dict]:
    """Return git tool definitions (JSON Schema format)."""
    return [
        {
            "name": "get_git_status",
            "description": "Get git status for all workspaces in a project. Shows branch, uncommitted changes, recent commits, ahead/behind counts, and stash count.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID"},
                },
                "required": ["project_id"],
            },
        },
        {
            "name": "git_commit",
            "description": "Stage all changes and create a commit.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Commit message"},
                    "project_id": {"type": "string", "description": "Project ID"},
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name or ID (optional)",
                    },
                },
                "required": ["message"],
            },
        },
        {
            "name": "git_pull",
            "description": "Pull (fetch + merge) from the remote origin.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID"},
                    "branch": {
                        "type": "string",
                        "description": "Branch to pull (optional, defaults to current)",
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name or ID (optional)",
                    },
                },
            },
        },
        {
            "name": "git_push",
            "description": "Push a branch to origin; an explicit expected remote OID permits a guarded rewrite.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID"},
                    "branch": {
                        "type": "string",
                        "description": (
                            "Branch to push (optional; defaults to the held task's "
                            "branch for a worker session, else the current branch)"
                        ),
                    },
                    "expected_remote_oid": {
                        "type": "string",
                        "description": _EXPECTED_REMOTE_OID_DESCRIPTION,
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name or ID (optional)",
                    },
                },
            },
        },
        {
            "name": "git_create_branch",
            "description": "Create and switch to a new git branch.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "branch_name": {"type": "string", "description": "New branch name"},
                    "project_id": {"type": "string", "description": "Project ID"},
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name or ID (optional)",
                    },
                },
                "required": ["branch_name"],
            },
        },
        {
            "name": "git_merge",
            "description": "Merge a branch into the default branch.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "branch_name": {"type": "string", "description": "Branch to merge"},
                    "default_branch": {
                        "type": "string",
                        "description": "Target branch (default: project default)",
                    },
                    "project_id": {"type": "string", "description": "Project ID"},
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name or ID (optional)",
                    },
                },
                "required": ["branch_name"],
            },
        },
        {
            "name": "git_create_pr",
            "description": "Create a GitHub pull request using the gh CLI.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "PR title"},
                    "body": {"type": "string", "description": "PR body/description"},
                    "branch": {
                        "type": "string",
                        "description": (
                            "Source branch (default: the held task's branch for a "
                            "worker session, else the current branch)"
                        ),
                    },
                    "base": {
                        "type": "string",
                        "description": "Target branch (default: project default)",
                    },
                    "project_id": {"type": "string", "description": "Project ID"},
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name or ID (optional)",
                    },
                },
                "required": ["title"],
            },
        },
        {
            "name": "git_changed_files",
            "description": "List files changed compared to a base branch.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "base_branch": {
                        "type": "string",
                        "description": (
                            "Base revision to compare against (default: project "
                            "default branch). Accepts a branch name or a revision "
                            "expression such as 'HEAD~1', 'HEAD^' or 'main@{1}'."
                        ),
                    },
                    "project_id": {"type": "string", "description": "Project ID"},
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name or ID (optional)",
                    },
                },
            },
        },
        {
            "name": "git_log",
            "description": "Show recent commit log.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID"},
                    "count": {
                        "type": "integer",
                        "description": "Number of commits (default 10)",
                        "default": 10,
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name or ID (optional)",
                    },
                },
                "required": ["project_id"],
            },
        },
        {
            "name": "git_diff",
            "description": "Show diff of the working tree or against a base branch.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID"},
                    "base_branch": {
                        "type": "string",
                        "description": (
                            "Base revision to diff against (optional; defaults to a "
                            "working-tree diff). Accepts a branch name or a revision "
                            "expression such as 'HEAD~1', 'HEAD^' or 'main@{1}'."
                        ),
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name or ID (optional)",
                    },
                },
                "required": ["project_id"],
            },
        },
        {
            "name": "git_branch",
            "description": "List branches or create a new branch. If 'name' is provided, creates and checks out; otherwise lists branches.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID"},
                    "name": {
                        "type": "string",
                        "description": "New branch name (optional -- omit to list)",
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name or ID (optional)",
                    },
                },
                "required": ["project_id"],
            },
        },
        {
            "name": "git_checkout",
            "description": "Switch to an existing branch.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID"},
                    "branch": {"type": "string", "description": "Branch name to switch to"},
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name or ID (optional)",
                    },
                },
                "required": ["project_id", "branch"],
            },
        },
        {
            "name": "checkout_branch",
            "description": "Check out an existing branch (alias for git_checkout).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID"},
                    "branch_name": {"type": "string", "description": "Branch name"},
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name or ID (optional)",
                    },
                },
                "required": ["branch_name"],
            },
        },
        {
            "name": "create_branch",
            "description": "Create and switch to a new branch (alias for git_create_branch).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID"},
                    "branch_name": {"type": "string", "description": "New branch name"},
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name or ID (optional)",
                    },
                },
                "required": ["branch_name"],
            },
        },
        {
            "name": "commit_changes",
            "description": "Stage all changes and commit (alias for git_commit).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID"},
                    "message": {"type": "string", "description": "Commit message"},
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name or ID (optional)",
                    },
                },
                "required": ["message"],
            },
        },
        {
            "name": "push_branch",
            "description": "Push the current or specified branch to origin (alias for git_push).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID"},
                    "branch_name": {
                        "type": "string",
                        "description": (
                            "Branch to push (optional; defaults to the held task's "
                            "branch for a worker session, else the current branch)"
                        ),
                    },
                    "expected_remote_oid": {
                        "type": "string",
                        "description": _EXPECTED_REMOTE_OID_DESCRIPTION,
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name or ID (optional)",
                    },
                },
            },
        },
        {
            "name": "merge_branch",
            "description": "Merge a branch into the default branch (alias for git_merge).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID"},
                    "branch_name": {"type": "string", "description": "Branch to merge"},
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name or ID (optional)",
                    },
                },
                "required": ["branch_name"],
            },
        },
        {
            "name": "create_github_repo",
            "description": "Create a new GitHub repository via the gh CLI.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Repository name"},
                    "private": {
                        "type": "boolean",
                        "description": "Create private repo (default true)",
                        "default": True,
                    },
                    "org": {"type": "string", "description": "GitHub org (omit for personal repo)"},
                    "description": {"type": "string", "description": "Repo description"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "generate_readme",
            "description": "Generate a README.md from project metadata and commit it.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID"},
                    "name": {"type": "string", "description": "Human-readable project name"},
                    "description": {"type": "string", "description": "Project description"},
                    "tech_stack": {"type": "string", "description": "Comma-separated technologies"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "git_remote_url",
            "description": (
                "Get the git remote URL (e.g. GitHub URL) for a project's workspace. "
                "Returns the origin remote URL directly from the git repository. "
                "Use this when you need the repo/GitHub URL and it's not in project metadata."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID"},
                    "remote": {
                        "type": "string",
                        "description": "Remote name (default: origin)",
                        "default": "origin",
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name or ID (optional)",
                    },
                },
            },
        },
    ]


# ---------------------------------------------------------------------------
# CLI formatters
# ---------------------------------------------------------------------------


def _fmt_git_status(data: dict):
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    repos = data.get("repos", [])
    project = data.get("project_name", data.get("project_id", ""))
    panels = []
    for repo in repos:
        workspace = repo.get("workspace_name") or repo.get("workspace_id", "")
        branch = repo.get("branch", "—")
        ahead, behind = repo.get("ahead", 0), repo.get("behind", 0)
        stash = repo.get("stash_count", 0)
        lines = []
        bt = Text()
        bt.append("Branch: ", style="dim")
        bt.append(branch, style="bold bright_cyan")
        if ahead or behind:
            bt.append(f"  ↑{ahead} ↓{behind}", style="yellow")
        if stash:
            bt.append(f"  📦 {stash} stash(es)", style="dim")
        lines.append(bt)
        diff_stat = repo.get("diff_stat", "")
        if diff_stat:
            stat_lines = diff_stat.strip().split("\n")
            show = stat_lines[:6] if len(stat_lines) > 8 else stat_lines
            for sl in show:
                lines.append(Text(f"  {sl.strip()}", style="dim"))
            if len(stat_lines) > 8:
                lines.append(Text(f"  ... and {len(stat_lines) - 6} more files", style="dim"))
        lock = repo.get("locked_by_task_id")
        if lock:
            lines.append(Text(f"🔒 Locked by task: {lock}", style="yellow"))
        title = workspace if workspace else repo.get("path", "")
        panels.append(
            Panel(
                Group(*lines),
                title=f"[bold]{title}[/]",
                border_style="bright_black",
                padding=(0, 1),
            )
        )
    header = Text(f"  {project} — {len(repos)} workspace(s)", style="bold bright_white")
    return Group(header, *panels)


def _fmt_git_log(data: dict):
    from rich.text import Text

    log = data.get("log", "")
    branch = data.get("branch", "")
    text = Text()
    if branch:
        text.append(f"  {branch}\n", style="bold bright_cyan")
    for line in log.strip().split("\n"):
        if " " in line:
            sha, msg = line.split(" ", 1)
            text.append(f"  {sha} ", style="yellow")
            text.append(f"{msg}\n", style="white")
        else:
            text.append(f"  {line}\n", style="white")
    return text


def _fmt_git_diff(data: dict):
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.text import Text

    diff = data.get("diff", "")
    base = data.get("base_branch", "")
    if not diff.strip():
        return Panel(Text("No changes.", style="dim"), title="diff", border_style="bright_black")
    body = Syntax(diff, "diff", theme="monokai")
    title = f"diff ({base})" if base else "diff"
    return Panel(body, title=f"[bold]{title}[/]", border_style="bright_black", padding=(0, 1))


def _fmt_git_action(data: dict):
    from rich.text import Text

    status = data.get("status", "")
    text = Text()
    text.append("✅ ", style="bold")
    text.append(f"{status}", style="bold green")
    for key in ("branch", "pushed", "oid", "message", "pr_url", "output", "pull_output"):
        val = data.get(key)
        if val:
            text.append(f"\n  {key}: ", style="dim")
            text.append(str(val)[:200], style="white")
    return text


def _build_cli_formatters():
    """Return CLI formatter specs for git commands."""
    from src.cli.formatter_registry import FormatterSpec

    formatters = {
        "get_git_status": FormatterSpec(render=_fmt_git_status, extract=None, many=False),
        "git_log": FormatterSpec(render=_fmt_git_log, extract=None, many=False),
        "git_diff": FormatterSpec(render=_fmt_git_diff, extract=None, many=False),
    }
    for cmd in (
        "git_commit",
        "git_pull",
        "git_push",
        "git_create_branch",
        "git_merge",
        "git_create_pr",
        "create_branch",
        "checkout_branch",
        "commit_changes",
        "push_branch",
        "merge_branch",
        "git_checkout",
        "create_github_repo",
        "generate_readme",
        "git_branch",
        "git_changed_files",
        "git_remote_url",
    ):
        formatters[cmd] = FormatterSpec(render=_fmt_git_action, extract=None, many=False)
    return formatters


CLI_FORMATTERS = _build_cli_formatters


# ---------------------------------------------------------------------------
# Worker publication
# ---------------------------------------------------------------------------


def _worker_principal() -> ExecutionPrincipal | None:
    """The calling worker session, or ``None`` for operators and supervisors."""
    principal = current_principal()
    if (
        principal is not None
        and principal.kind == PrincipalKind.SESSION
        and not principal.elevated
    ):
        return principal
    return None


def _is_github_repository(repository_url: str) -> bool:
    """Whether a recorded repository is one the GitHub client can address."""
    from src.projects.github import GitHubError, parse_github_repository

    if repository_url.startswith(("/", "./", "../", "file://")) or Path(repository_url).exists():
        return False
    try:
        parse_github_repository(repository_url)
    except GitHubError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class _WorkerPublication:
    """What a worker session may publish, derived from persisted state only.

    The held task's branch, to the task's authorized repository, gated by
    the project's default branch.  Nothing here comes from the worker's
    checkout configuration or from a client argument that was not checked
    against the held task.
    """

    branch: str
    repository_url: str
    default_branch: str


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class GitPlugin(InternalPlugin):
    """Git operations: status, commit, push, pull, branch, merge, PR, etc."""

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._ws = ctx.get_service("workspace")
        self._db = ctx.get_service("db")
        self._git_svc = ctx.get_service("git")
        # Access raw GitManager for methods not on the Protocol surface
        self._git = self._git_svc._manager

        cmds = [
            ("get_git_status", self.cmd_get_git_status),
            ("git_commit", self.cmd_git_commit),
            ("git_pull", self.cmd_git_pull),
            ("git_push", self.cmd_git_push),
            ("git_create_branch", self.cmd_git_create_branch),
            ("git_merge", self.cmd_git_merge),
            ("git_create_pr", self.cmd_git_create_pr),
            ("git_changed_files", self.cmd_git_changed_files),
            ("git_log", self.cmd_git_log),
            ("git_diff", self.cmd_git_diff),
            ("git_branch", self.cmd_git_branch),
            ("git_checkout", self.cmd_git_checkout),
            ("checkout_branch", self.cmd_checkout_branch),
            ("create_branch", self.cmd_create_branch),
            ("commit_changes", self.cmd_commit_changes),
            ("push_branch", self.cmd_push_branch),
            ("merge_branch", self.cmd_merge_branch),
            ("create_github_repo", self.cmd_create_github_repo),
            ("generate_readme", self.cmd_generate_readme),
            ("git_remote_url", self.cmd_git_remote_url),
        ]
        for name, handler in cmds:
            ctx.register_command(name, handler)

        for tool_def in _build_tool_definitions():
            ctx.register_tool(dict(tool_def), category="git")

    async def shutdown(self, ctx: PluginContext) -> None:
        pass

    # --- Helpers ---

    async def _resolve(self, args: dict):
        """Resolve repo path with active project fallback."""
        principal = _worker_principal()
        if principal is not None:
            # The authenticated session owns the checkout.  Do not let a
            # missing command argument select the project's base workspace,
            # nor an explicit one select another checkout.
            if not principal.session_id or not principal.project_id:
                return None, None, {"error": "No active session worktree for this project"}
            if args.get("session_id") not in (None, principal.session_id):
                return None, None, {"error": "session_id does not match the active session"}
            if args.get("project_id") not in (None, principal.project_id):
                return None, None, {"error": "project_id does not match the active session"}
            if args.get("workspace") is not None:
                return None, None, {"error": "workspace is not this session's worktree"}
            args["session_id"] = principal.session_id
            args["project_id"] = principal.project_id
        return await self._ws.resolve_repo_path(args, self._ctx.active_project_id)

    async def _worker_publication(
        self,
        principal: ExecutionPrincipal,
        checkout_path: str,
        project,
        requested_branch: str | None,
    ) -> _WorkerPublication:
        """Authorize a worker's push or PR before any credential is selected.

        The HTTP scope check already confines a worker token to its task
        branch; this repeats the decision at the command so an in-process
        caller carrying a session principal is held to the same rule.
        """
        from src.api.auth import RequestScope
        from src.api.scope import held_task_for_session
        from src.git.manager import GitError

        task = await held_task_for_session(
            self._db._db,
            RequestScope(
                kind="session",
                session_id=principal.session_id,
                task_id=principal.task_id,
                project_id=principal.project_id,
            ),
        )
        if task is None:
            raise GitError("No active task branch for this session")
        own_branches = {f"aq/{task.id}"}
        if task.branch_name:
            own_branches.add(task.branch_name)
        branch = requested_branch or task.branch_name or f"aq/{task.id}"
        if branch not in own_branches:
            raise GitError(f"branch '{branch}' is not this session's task branch")
        return _WorkerPublication(
            branch=branch,
            repository_url=await self._task_repository_url(task, project, checkout_path),
            default_branch=(project.repo_default_branch if project else None) or "main",
        )

    async def _task_repository_url(self, task, project, checkout_path: str) -> str:
        """The task's authorized repository, as the daemon's own delivery resolves it.

        Mirrors ``src/orchestrator/git_ops.py``: the task's repository row
        (which must belong to the task's project), else the project's
        repository.  A task with neither may publish only to a local origin
        path — never to a network remote named by the checkout alone.
        """
        from src.git.manager import GitError

        if task.repo_id:
            repo = await self._db._db.get_repo(task.repo_id)
            if repo is None or repo.project_id != task.project_id:
                raise GitError("task repository is not authorized for this project")
            repository_url = repo.url
        else:
            repository_url = project.repo_url if project else ""
        if not repository_url:
            local_origin = await self._git.aget_remote_url(checkout_path)
            if local_origin and Path(local_origin).exists():
                repository_url = local_origin
        if not repository_url:
            raise GitError("task has no authorized repository")
        return repository_url

    async def _push(
        self,
        checkout_path: str,
        project,
        args: dict,
        requested_branch: str | None,
    ) -> tuple[str, str | None]:
        """Publish one branch and return ``(branch, pushed OID)``.

        An operator or supervisor pushes the named (or current) branch of the
        selected workspace.  A worker session publishes only its held task's
        branch, through the validated delivery gate, to its task's authorized
        repository; ``expected_remote_oid`` is the explicit lease for its
        squash/rewrite workflow in both cases.
        """
        from src.git.manager import GitError

        git = self._git
        expected_remote_oid = args.get("expected_remote_oid")
        principal = _worker_principal()
        if principal is None:
            branch = requested_branch or await git.aget_current_branch(checkout_path)
            if not branch:
                raise GitError("Could not determine current branch")
            oid = await git.apush_branch(
                checkout_path,
                branch,
                expected_remote_oid=expected_remote_oid,
                event_bus=self._ctx._bus,
                project_id=args.get("project_id"),
            )
            return branch, oid

        publication = await self._worker_publication(
            principal, checkout_path, project, requested_branch
        )
        # The same reserved-path gate the daemon applies to task delivery,
        # measured from the default branch this checkout last fetched.
        base_ref: str | None = f"refs/remotes/origin/{publication.default_branch}"
        base_exists = await git.aref_exists(checkout_path, base_ref)
        if base_exists is None:
            raise GitError("could not inspect the delivery base")
        if not base_exists:
            base_ref = None
        oid = await git.apush_validated_delivery(
            checkout_path,
            base_ref,
            publication.branch,
            publication.branch,
            expected_remote_oid=expected_remote_oid,
            repository_url=publication.repository_url,
            event_bus=self._ctx._bus,
            project_id=args.get("project_id"),
        )
        return publication.branch, oid

    async def _warn_if_in_progress(self, project_id: str) -> str | None:
        from src.models import TaskStatus

        in_progress = await self._db._db.list_tasks(
            project_id=project_id,
            status=TaskStatus.IN_PROGRESS,
        )
        if in_progress:
            return (
                f"\u26a0\ufe0f {len(in_progress)} task(s) currently IN_PROGRESS for this project -- "
                f"this operation may disrupt running agent(s)."
            )
        return None

    @staticmethod
    async def _git_ahead_behind(git, ws_path: str, branch: str) -> tuple[int, int]:
        try:
            output = await git._arun(
                ["rev-list", "--left-right", "--count", f"{branch}...@{{u}}"],
                cwd=ws_path,
            )
            parts = output.strip().split()
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
        except Exception:
            pass
        return 0, 0

    @staticmethod
    async def _git_stash_count(git, ws_path: str) -> int:
        try:
            output = await git._arun(["stash", "list"], cwd=ws_path)
            if output.strip():
                return len(output.strip().splitlines())
        except Exception:
            pass
        return 0

    @staticmethod
    async def _git_diff_stat(git, ws_path: str, branch: str) -> str:
        try:
            default_branch = await git.aget_default_branch(ws_path)
            if branch == default_branch:
                return ""
            merge_base = await git._arun(
                ["merge-base", f"origin/{default_branch}", "HEAD"],
                cwd=ws_path,
            )
            stat = await git._arun(
                ["diff", "--stat", merge_base.strip()],
                cwd=ws_path,
            )
            return stat.strip()
        except Exception:
            return ""

    # --- Commands ---

    async def cmd_get_git_status(self, args: dict) -> dict:
        from src.models import RepoSourceType

        project_id = args.get("project_id") or self._ctx.active_project_id
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
        project = await self._db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}

        git = self._git
        repo_statuses = []

        workspaces = await self._db.list_workspaces(project_id)
        if workspaces:
            for ws in workspaces:
                ws_path = ws.workspace_path
                if not os.path.isdir(ws_path):
                    repo_statuses.append(
                        {"workspace_id": ws.id, "error": f"Path not found: {ws_path}"}
                    )
                    continue
                if not await git.avalidate_checkout(ws_path):
                    repo_statuses.append(
                        {"workspace_id": ws.id, "error": f"Not a valid git repository: {ws_path}"}
                    )
                    continue
                branch = await git.aget_current_branch(ws_path)
                status_output = await git.aget_status(ws_path)
                recent_commits = await git.aget_recent_commits(ws_path, count=5)
                remote_url = await git.aget_remote_url(ws_path)
                lock_info = ""
                if ws.locked_by_agent_id:
                    lock_info = f" (locked by {ws.locked_by_agent_id})"

                ahead_behind = await self._git_ahead_behind(git, ws_path, branch)
                stash_count = await self._git_stash_count(git, ws_path)
                diff_stat = await self._git_diff_stat(git, ws_path, branch)

                current_task_title = None
                if ws.locked_by_task_id:
                    task = await self._db.get_task(ws.locked_by_task_id)
                    if task:
                        current_task_title = task.title

                ws_info: dict = {
                    "workspace_id": ws.id,
                    "workspace_name": ws.name or "",
                    "path": ws_path,
                    "branch": branch,
                    "status": status_output or "(clean)",
                    "recent_commits": recent_commits,
                    "lock": lock_info,
                    "ahead": ahead_behind[0],
                    "behind": ahead_behind[1],
                    "stash_count": stash_count,
                    "diff_stat": diff_stat,
                    "locked_by_agent_id": ws.locked_by_agent_id,
                    "locked_by_task_id": ws.locked_by_task_id,
                    "current_task_title": current_task_title,
                }
                if remote_url:
                    ws_info["remote_url"] = remote_url
                repo_statuses.append(ws_info)
        else:
            repos = await self._db.list_repos(project_id)
            if repos:
                for repo in repos:
                    if repo.source_type == RepoSourceType.LINK and repo.source_path:
                        repo_path = repo.source_path
                    elif repo.source_type == RepoSourceType.CLONE and repo.checkout_base_path:
                        repo_path = repo.checkout_base_path
                    else:
                        continue
                    if not os.path.isdir(repo_path):
                        repo_statuses.append(
                            {"repo_id": repo.id, "error": f"Path not found: {repo_path}"}
                        )
                        continue
                    if not await git.avalidate_checkout(repo_path):
                        repo_statuses.append(
                            {
                                "repo_id": repo.id,
                                "error": f"Not a valid git repository: {repo_path}",
                            }
                        )
                        continue
                    branch = await git.aget_current_branch(repo_path)
                    status_output = await git.aget_status(repo_path)
                    recent_commits = await git.aget_recent_commits(repo_path, count=5)
                    repo_statuses.append(
                        {
                            "repo_id": repo.id,
                            "path": repo_path,
                            "branch": branch,
                            "status": status_output or "(clean)",
                            "recent_commits": recent_commits,
                        }
                    )
            else:
                return {
                    "error": f"Project '{project_id}' has no workspaces. Use /add-workspace to create one."
                }

        return {"project_id": project_id, "project_name": project.name, "repos": repo_statuses}

    async def cmd_git_commit(self, args: dict) -> dict:
        from src.git.manager import GitError

        message = args["message"]
        checkout_path, project, err = await self._resolve(args)
        if err:
            return err
        project_id = args.get("project_id", "")
        agent_id = args.get("agent_id")
        try:
            committed = await self._git.acommit_all(
                checkout_path,
                message,
                event_bus=self._ctx._bus,
                project_id=project_id or None,
                agent_id=agent_id,
            )
        except GitError as e:
            return {"error": str(e)}
        if not committed:
            return {
                "project_id": project_id,
                "committed": False,
                "message": "No eligible changes to commit",
            }
        return {"project_id": project_id, "committed": True, "commit_message": message}

    async def cmd_git_pull(self, args: dict) -> dict:
        from src.git.manager import GitError

        checkout_path, project, err = await self._resolve(args)
        if err:
            return err
        branch = args.get("branch") or None
        try:
            pulled = await self._git.apull_branch(checkout_path, branch)
        except GitError as e:
            return {"error": str(e)}
        return {"project_id": args.get("project_id", ""), "pulled": pulled}

    async def cmd_git_push(self, args: dict) -> dict:
        from src.git.manager import GitError

        checkout_path, project, err = await self._resolve(args)
        if err:
            return err
        try:
            branch, oid = await self._push(checkout_path, project, args, args.get("branch"))
        except GitError as e:
            return {"error": str(e)}
        return {"project_id": args.get("project_id", ""), "pushed": branch, "oid": oid}

    async def cmd_git_create_branch(self, args: dict) -> dict:
        from src.git.manager import GitError

        branch_name = args["branch_name"]
        checkout_path, project, err = await self._resolve(args)
        if err:
            return err
        try:
            await self._git.acreate_branch(checkout_path, branch_name)
        except GitError as e:
            return {"error": str(e)}
        return {"project_id": args.get("project_id", ""), "created_branch": branch_name}

    async def cmd_git_merge(self, args: dict) -> dict:
        from src.git.manager import GitError

        branch_name = args["branch_name"]
        checkout_path, project, err = await self._resolve(args)
        if err:
            return err
        project_id = args.get("project_id", "")
        default_branch = (
            args.get("default_branch")
            or (project.repo_default_branch if project else "main")
            or "main"
        )
        try:
            success = await self._git.amerge_branch(checkout_path, branch_name, default_branch)
        except GitError as e:
            return {"error": str(e)}
        if not success:
            return {
                "project_id": project_id,
                "merged": False,
                "into": default_branch,
                "message": f"Merge conflict -- merge of '{branch_name}' into '{default_branch}' was aborted",
            }
        return {
            "project_id": project_id,
            "merged": True,
            "branch": branch_name,
            "into": default_branch,
        }

    async def cmd_git_create_pr(self, args: dict) -> dict:
        from src.git.manager import GitError

        title = args["title"]
        body = args.get("body", "")
        checkout_path, project, err = await self._resolve(args)
        if err:
            return err
        git = self._git
        principal = _worker_principal()
        try:
            if principal is None:
                branch = args.get("branch") or await git.aget_current_branch(checkout_path)
                if not branch:
                    return {"error": "Could not determine current branch"}
                base = (
                    args.get("base")
                    or (project.repo_default_branch if project else "main")
                    or "main"
                )
                if project is None or not project.repo_url:
                    return {"error": "Project has no authorized GitHub repository"}
                repository_url = project.repo_url
            else:
                # Everything a worker may name is checked against the held
                # task before the repository is bound, since binding already
                # selects the GitHub credential.
                publication = await self._worker_publication(
                    principal, checkout_path, project, args.get("branch")
                )
                branch = publication.branch
                base = args.get("base") or publication.default_branch
                if base != publication.default_branch:
                    return {"error": f"base '{base}' is not the project's default branch"}
                repository_url = publication.repository_url
                if not _is_github_repository(repository_url):
                    return {"error": "Project has no authorized GitHub repository"}
            repository = await git.bind_github_repository(repository_url)
            pr_url = await git.acreate_pr(
                checkout_path,
                branch,
                title,
                body,
                base,
                event_bus=self._ctx._bus,
                project_id=args.get("project_id", ""),
                repository=repository,
            )
        except GitError as e:
            return {"error": str(e)}
        return {
            "project_id": args.get("project_id", ""),
            "pr_url": pr_url,
            "branch": branch,
            "base": base,
        }

    async def cmd_git_changed_files(self, args: dict) -> dict:
        checkout_path, project, err = await self._resolve(args)
        if err:
            return err
        base_branch = (
            args.get("base_branch")
            or (project.repo_default_branch if project else "main")
            or "main"
        )
        files = await self._git.aget_changed_files(checkout_path, base_branch)
        return {
            "project_id": args.get("project_id", ""),
            "base_branch": base_branch,
            "files": files,
            "count": len(files),
        }

    async def cmd_git_log(self, args: dict) -> dict:
        checkout_path, project, err = await self._resolve(args)
        if err:
            return err
        count = args.get("count", 10)
        log_output = await self._git.aget_recent_commits(checkout_path, count=count)
        branch = await self._git.aget_current_branch(checkout_path)
        return {
            "project_id": args["project_id"],
            "branch": branch,
            "log": log_output or "(no commits)",
        }

    async def cmd_git_branch(self, args: dict) -> dict:
        from src.git.manager import GitError

        checkout_path, project, err = await self._resolve(args)
        if err:
            return err
        git = self._git
        new_branch = args.get("name")
        if new_branch:
            try:
                await git.acreate_branch(checkout_path, new_branch)
            except GitError as e:
                return {"error": str(e)}
            return {
                "project_id": args["project_id"],
                "created": new_branch,
                "message": f"Created and switched to branch '{new_branch}'",
            }
        else:
            branches = await git.alist_branches(checkout_path)
            current = await git.aget_current_branch(checkout_path)
            return {
                "project_id": args["project_id"],
                "current_branch": current,
                "branches": branches,
            }

    async def cmd_git_checkout(self, args: dict) -> dict:
        from src.git.manager import GitError

        checkout_path, project, err = await self._resolve(args)
        if err:
            return err
        branch = args["branch"]
        git = self._git
        old_branch = await git.aget_current_branch(checkout_path)
        try:
            await git.acheckout_branch(checkout_path, branch)
        except GitError as e:
            return {"error": str(e)}
        new_branch = await git.aget_current_branch(checkout_path)
        return {
            "project_id": args["project_id"],
            "old_branch": old_branch,
            "new_branch": new_branch,
            "message": f"Switched from '{old_branch}' to '{new_branch}'",
        }

    async def cmd_git_diff(self, args: dict) -> dict:
        from src.git.manager import GitError

        checkout_path, project, err = await self._resolve(args)
        if err:
            return err
        git = self._git
        base = args.get("base_branch")
        try:
            if base:
                diff = await git.aget_diff(checkout_path, base)
            else:
                diff = await git._arun(["diff"], cwd=checkout_path)
        except GitError as e:
            return {"error": str(e)}
        return {
            "project_id": args["project_id"],
            "base_branch": base or "(working tree)",
            "diff": diff or "(no changes)",
        }

    async def cmd_create_branch(self, args: dict) -> dict:
        from src.git.manager import GitError

        branch_name = args.get("branch_name")
        if not branch_name:
            return {"error": "branch_name is required"}
        checkout_path, project, err = await self._resolve(args)
        if err:
            return err
        try:
            await self._git.acreate_branch(checkout_path, branch_name)
        except GitError as e:
            return {"error": str(e)}
        return {"project_id": args["project_id"], "branch": branch_name, "status": "created"}

    async def cmd_checkout_branch(self, args: dict) -> dict:
        from src.git.manager import GitError

        branch_name = args.get("branch_name")
        if not branch_name:
            return {"error": "branch_name is required"}
        checkout_path, project, err = await self._resolve(args)
        if err:
            return err
        try:
            await self._git.acheckout_branch(checkout_path, branch_name)
        except GitError as e:
            return {"error": str(e)}
        result = {"project_id": args["project_id"], "branch": branch_name, "status": "checked_out"}
        warning = await self._warn_if_in_progress(args["project_id"])
        if warning:
            result["warning"] = warning
        return result

    async def cmd_commit_changes(self, args: dict) -> dict:
        from src.git.manager import GitError

        message = args.get("message")
        if not message:
            return {"error": "message is required"}
        checkout_path, project, err = await self._resolve(args)
        if err:
            return err
        try:
            committed = await self._git.acommit_all(
                checkout_path,
                message,
                event_bus=self._ctx._bus,
                project_id=args.get("project_id") or None,
                agent_id=args.get("agent_id"),
            )
        except GitError as e:
            return {"error": str(e)}
        if not committed:
            return {
                "project_id": args["project_id"],
                "status": "nothing_to_commit",
                "message": "No eligible changes to commit",
            }
        result = {
            "project_id": args["project_id"],
            "commit_message": message,
            "status": "committed",
        }
        warning = await self._warn_if_in_progress(args["project_id"])
        if warning:
            result["warning"] = warning
        return result

    async def cmd_push_branch(self, args: dict) -> dict:
        from src.git.manager import GitError

        checkout_path, project, err = await self._resolve(args)
        if err:
            return err
        try:
            branch_name, oid = await self._push(
                checkout_path, project, args, args.get("branch_name")
            )
        except GitError as e:
            return {"error": str(e)}
        return {
            "project_id": args["project_id"],
            "branch": branch_name,
            "status": "pushed",
            "oid": oid,
        }

    async def cmd_merge_branch(self, args: dict) -> dict:
        from src.git.manager import GitError

        branch_name = args.get("branch_name")
        if not branch_name:
            return {"error": "branch_name is required"}
        checkout_path, project, err = await self._resolve(args)
        if err:
            return err
        default_branch = project.repo_default_branch if project else "main"
        try:
            success = await self._git.amerge_branch(checkout_path, branch_name, default_branch)
        except GitError as e:
            return {"error": str(e)}
        warning = await self._warn_if_in_progress(args["project_id"])
        if not success:
            result = {
                "project_id": args["project_id"],
                "branch": branch_name,
                "target": default_branch,
                "status": "conflict",
                "message": "Merge conflict -- merge was aborted",
            }
            if warning:
                result["warning"] = warning
            return result
        result = {
            "project_id": args["project_id"],
            "branch": branch_name,
            "target": default_branch,
            "status": "merged",
        }
        if warning:
            result["warning"] = warning
        return result

    async def cmd_create_github_repo(self, args: dict) -> dict:
        from src.git.manager import GitError

        name = args.get("name")
        if not name:
            return {"error": "name is required"}
        private = args.get("private", True)
        org = args.get("org")
        description = args.get("description", "")
        git = self._git
        if not await git.acheck_gh_auth():
            return {
                "error": "GitHub CLI is not authenticated. Run `gh auth login` on the host to configure credentials."
            }
        try:
            url = await git.acreate_github_repo(
                name, private=private, org=org, description=description
            )
        except GitError as e:
            return {"error": str(e)}
        return {"created": True, "repo_url": url, "name": name}

    async def cmd_generate_readme(self, args: dict) -> dict:
        from src.git.manager import GitError

        project_name = args.get("name")
        if not project_name:
            return {"error": "name is required"}
        checkout_path, project, err = await self._resolve(args)
        if err:
            return err
        description = args.get("description", "").strip()
        tech_stack = args.get("tech_stack", "").strip()

        lines: list[str] = [f"# {project_name}", ""]
        if description:
            lines += [description, ""]
        if tech_stack:
            lines += ["## Tech Stack", ""]
            for tech in (t.strip() for t in tech_stack.split(",") if t.strip()):
                lines.append(f"- {tech}")
            lines.append("")
        lines += [
            "## Getting Started",
            "",
            "TODO: Add setup instructions.",
            "",
            "## License",
            "",
            "TODO: Add license information.",
            "",
        ]

        readme_content = "\n".join(lines)
        readme_path = os.path.join(checkout_path, "README.md")

        try:
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(readme_content)
        except OSError as e:
            return {"error": f"Failed to write README.md: {e}"}

        git = self._git
        try:
            committed = await git.acommit_all(
                checkout_path,
                "Add generated README.md",
                event_bus=self._ctx._bus,
                project_id=args.get("project_id") or None,
                agent_id=args.get("agent_id"),
            )
        except GitError as e:
            return {"error": f"Failed to commit README.md: {e}"}

        if not committed:
            return {
                "project_id": args.get("project_id", ""),
                "readme_path": readme_path,
                "committed": False,
                "pushed": False,
                "message": "README.md written but nothing new to commit",
            }

        pushed = False
        try:
            branch = await git.aget_current_branch(checkout_path) or "main"
            await git.apush_branch(
                checkout_path,
                branch,
                event_bus=self._ctx._bus,
                project_id=args.get("project_id"),
            )
            pushed = True
        except GitError:
            pass

        return {
            "project_id": args.get("project_id", ""),
            "readme_path": readme_path,
            "committed": True,
            "pushed": pushed,
            "status": "generated",
        }

    async def cmd_git_remote_url(self, args: dict) -> dict:
        """Return the git remote URL for a project's workspace."""
        checkout_path, project, err = await self._resolve(args)
        if err:
            return err
        remote = args.get("remote", "origin")
        git = self._git
        url = await git.aget_remote_url(checkout_path, remote)
        project_id = args.get("project_id", "")
        if not url:
            return {
                "project_id": project_id,
                "remote": remote,
                "url": None,
                "message": f"No '{remote}' remote configured in this workspace",
            }
        # Auto-populate project.repo_url if it's empty
        if project and not project.repo_url and url:
            try:
                await self._db.update_project(project_id, repo_url=url)
            except Exception:
                pass  # Non-fatal — we still return the URL
        return {
            "project_id": project_id,
            "remote": remote,
            "url": url,
        }
