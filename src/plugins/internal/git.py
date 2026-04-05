"""Internal plugin: git operations (status, commit, push, pull, branch, merge, PR, etc.).

Extracted from ``CommandHandler._cmd_git_*`` and related alias commands.
The largest internal plugin — 19 commands covering all git operations.
"""

from __future__ import annotations

import os

from src.plugins.base import InternalPlugin, PluginContext


# ---------------------------------------------------------------------------
# Tool definitions — loaded lazily to avoid a huge module-level constant.
# The actual definitions are in _build_tool_definitions() below.
# ---------------------------------------------------------------------------

TOOL_CATEGORY = "git"


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
            "description": "Push a branch to the remote origin.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID"},
                    "branch": {
                        "type": "string",
                        "description": "Branch to push (optional, defaults to current)",
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
                    "branch": {"type": "string", "description": "Source branch (default: current)"},
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
                        "description": "Base branch to compare (default: project default)",
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
                        "description": "Base branch to diff against (optional, defaults to working tree diff)",
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
                        "description": "Branch to push (optional, defaults to current)",
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
    for key in ("branch", "message", "pr_url", "output", "pull_output"):
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
        return await self._ws.resolve_repo_path(args, self._ctx.active_project_id)

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
        try:
            committed = await self._git.acommit_all(checkout_path, message)
        except GitError as e:
            return {"error": str(e)}
        if not committed:
            return {
                "project_id": project_id,
                "committed": False,
                "message": "Nothing to commit -- working tree clean",
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
        git = self._git
        branch = args.get("branch") or await git.aget_current_branch(checkout_path)
        if not branch:
            return {"error": "Could not determine current branch"}
        try:
            await git.apush_branch(checkout_path, branch)
        except GitError as e:
            return {"error": str(e)}
        return {"project_id": args.get("project_id", ""), "pushed": branch}

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
        branch = args.get("branch") or await git.aget_current_branch(checkout_path)
        if not branch:
            return {"error": "Could not determine current branch"}
        base = args.get("base") or (project.repo_default_branch if project else "main") or "main"
        try:
            pr_url = await git.acreate_pr(checkout_path, branch, title, body, base)
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
            committed = await self._git.acommit_all(checkout_path, message)
        except GitError as e:
            return {"error": str(e)}
        if not committed:
            return {
                "project_id": args["project_id"],
                "status": "nothing_to_commit",
                "message": "No changes to commit",
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
        git = self._git
        branch_name = args.get("branch_name")
        if not branch_name:
            branch_name = await git.aget_current_branch(checkout_path)
            if not branch_name:
                return {"error": "Could not determine current branch"}
        try:
            await git.apush_branch(checkout_path, branch_name)
        except GitError as e:
            return {"error": str(e)}
        return {"project_id": args["project_id"], "branch": branch_name, "status": "pushed"}

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
            committed = await git.acommit_all(checkout_path, "Add generated README.md")
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
            await git.apush_branch(checkout_path, branch)
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
