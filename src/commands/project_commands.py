"""Project commands mixin — CRUD, pause/resume, channel management."""

from __future__ import annotations

import logging
import os

from src.models import (
    Project,
    ProjectStatus,
    RepoSourceType,
    TaskStatus,
    WorkspaceMode,
    WORKSPACE_MODE_VALUES,
)
from src.commands.helpers import _count_by
from src.discord.embeds import STATUS_EMOJIS, progress_bar

logger = logging.getLogger(__name__)


class ProjectCommandsMixin:
    """Project command methods mixed into CommandHandler."""

    # -----------------------------------------------------------------------
    # Project commands -- CRUD, pause/resume, and Discord channel management.
    # Projects are the top-level grouping: each project has its own workspace
    # directory, scheduling weight, and optional dedicated Discord channel.
    # -----------------------------------------------------------------------

    async def _cmd_get_status(self, args: dict) -> dict:
        filter_project = args.get("project_id")
        projects = await self.db.list_projects()
        tasks = await self.db.list_tasks(project_id=filter_project)

        in_progress = [
            {
                "id": t.id,
                "title": t.title,
                "project_id": t.project_id,
                "assigned_agent": t.assigned_agent_id,
            }
            for t in tasks
            if t.status == TaskStatus.IN_PROGRESS
        ]
        ready = [
            {"id": t.id, "title": t.title, "project_id": t.project_id}
            for t in tasks
            if t.status == TaskStatus.READY
        ]

        return {
            "projects": 1 if filter_project else len(projects),
            "tasks": {
                "total": len(tasks),
                "by_status": _count_by(tasks, lambda t: t.status.value),
                "in_progress": in_progress,
                "ready_to_work": ready,
            },
            "orchestrator_paused": self.orchestrator._paused,
        }

    async def _cmd_list_projects(self, args: dict) -> dict:
        projects = await self.db.list_projects()
        result = []
        for p in projects:
            ws_path = await self.db.get_project_workspace_path(p.id)
            info = {
                "id": p.id,
                "name": p.name,
                "status": p.status.value,
                "credit_weight": p.credit_weight,
                "max_concurrent_agents": p.max_concurrent_agents,
                "workspace": ws_path,
            }
            if p.repo_url:
                info["repo_url"] = p.repo_url
            if p.discord_channel_id:
                info["discord_channel_id"] = p.discord_channel_id
            result.append(info)
        return {"projects": result}

    async def _cmd_create_project(self, args: dict) -> dict:
        name = args.get("name") or args.get("project_id")
        if not name:
            return {"error": "'name' is required to create a project"}
        project_id = name.lower().replace(" ", "-")
        project = Project(
            id=project_id,
            name=name,
            credit_weight=args.get("credit_weight", 1.0),
            max_concurrent_agents=args.get("max_concurrent_agents", 2),
            repo_url=args.get("repo_url", ""),
            repo_default_branch=args.get("default_branch", "main"),
        )
        await self.db.create_project(project)

        # Ensure the per-project task directory exists (vault migration Phase 1).
        project_tasks_dir = os.path.join(self.config.data_dir, "tasks", project_id)
        os.makedirs(project_tasks_dir, exist_ok=True)

        # Create vault subdirectories for the new project (vault spec §2).
        from src.vault import ensure_vault_project_dirs

        ensure_vault_project_dirs(self.config.data_dir, project_id)

        # Determine whether auto-channel creation should happen.
        # An explicit ``auto_create_channels`` arg takes precedence;
        # otherwise fall back to the per-project-channels config flag.
        explicit = args.get("auto_create_channels")
        if explicit is not None:
            should_auto_create = bool(explicit)
        else:
            ppc = self.config.discord.per_project_channels
            should_auto_create = ppc.auto_create

        # Notify listeners (e.g. Discord bot) so they can create channels.
        if self._on_project_created:
            try:
                await self._on_project_created(project_id, should_auto_create)
            except Exception:
                logger.warning(
                    "on_project_created callback failed for %s", project_id, exc_info=True,
                )

        return {
            "created": project_id,
            "name": project.name,
            "auto_create_channels": should_auto_create,
        }

    async def _cmd_pause_project(self, args: dict) -> dict:
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        await self.db.update_project(pid, status=ProjectStatus.PAUSED)
        return {"paused": pid, "name": project.name}

    async def _cmd_resume_project(self, args: dict) -> dict:
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        await self.db.update_project(pid, status=ProjectStatus.ACTIVE)
        return {"resumed": pid, "name": project.name}

    async def _cmd_set_project_constraint(self, args: dict) -> dict:
        """Set a scheduling constraint on a project.

        Supports three constraint types that can be combined ("stacked"):
        - exclusive: only one agent may work on the project at a time
        - max_agents_by_type: per-agent-type concurrency limits
        - pause_scheduling: stop all new task assignments

        When called on a project that already has a constraint, the new
        fields are merged with the existing constraint — unspecified fields
        retain their previous values.
        """
        import time as _time

        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}

        # Load existing constraint (if any) for merge/stacking behavior.
        existing = await self.db.get_project_constraint(pid)

        exclusive = args.get("exclusive", existing.exclusive if existing else False)
        pause_scheduling = args.get(
            "pause_scheduling", existing.pause_scheduling if existing else False
        )
        max_agents_by_type = args.get(
            "max_agents_by_type",
            existing.max_agents_by_type if existing else {},
        )
        created_by = args.get("created_by", existing.created_by if existing else None)

        if not exclusive and not pause_scheduling and not max_agents_by_type:
            return {
                "error": (
                    "At least one constraint must be set: "
                    "exclusive, max_agents_by_type, or pause_scheduling."
                )
            }

        from src.models import ProjectConstraint

        constraint = ProjectConstraint(
            project_id=pid,
            exclusive=bool(exclusive),
            max_agents_by_type=max_agents_by_type if isinstance(max_agents_by_type, dict) else {},
            pause_scheduling=bool(pause_scheduling),
            created_by=created_by,
            created_at=_time.time(),
        )
        await self.db.set_project_constraint(constraint)

        active_fields = []
        if constraint.exclusive:
            active_fields.append("exclusive")
        if constraint.pause_scheduling:
            active_fields.append("pause_scheduling")
        if constraint.max_agents_by_type:
            active_fields.append(f"max_agents_by_type={constraint.max_agents_by_type}")

        return {
            "project_id": pid,
            "constraint_set": True,
            "active_fields": active_fields,
        }

    async def _cmd_release_project_constraint(self, args: dict) -> dict:
        """Release (remove) the scheduling constraint from a project.

        If specific fields are provided (exclusive, pause_scheduling, or
        max_agents_by_type), only those fields are cleared.  If no fields
        are specified, the entire constraint is removed.
        """
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}

        existing = await self.db.get_project_constraint(pid)
        if not existing:
            return {"error": f"No active constraint on project '{pid}'"}

        # Check if caller wants to release specific fields only.
        fields_to_release = args.get("fields", [])
        if fields_to_release:
            import time as _time

            from src.models import ProjectConstraint

            exclusive = existing.exclusive
            pause_scheduling = existing.pause_scheduling
            max_agents_by_type = dict(existing.max_agents_by_type)

            for f in fields_to_release:
                if f == "exclusive":
                    exclusive = False
                elif f == "pause_scheduling":
                    pause_scheduling = False
                elif f == "max_agents_by_type":
                    max_agents_by_type = {}

            # If all fields are now empty, remove the constraint entirely.
            if not exclusive and not pause_scheduling and not max_agents_by_type:
                await self.db.delete_project_constraint(pid)
                return {"project_id": pid, "constraint_released": True, "fields": "all"}

            updated = ProjectConstraint(
                project_id=pid,
                exclusive=exclusive,
                max_agents_by_type=max_agents_by_type,
                pause_scheduling=pause_scheduling,
                created_by=existing.created_by,
                created_at=_time.time(),
            )
            await self.db.set_project_constraint(updated)
            return {
                "project_id": pid,
                "constraint_released": False,
                "fields_released": fields_to_release,
                "remaining_fields": [
                    k
                    for k, v in [
                        ("exclusive", exclusive),
                        ("pause_scheduling", pause_scheduling),
                        ("max_agents_by_type", bool(max_agents_by_type)),
                    ]
                    if v
                ],
            }

        # Release the entire constraint.
        await self.db.delete_project_constraint(pid)
        return {"project_id": pid, "constraint_released": True, "fields": "all"}

    async def _cmd_edit_project(self, args: dict) -> dict:
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        updates = {}
        if "name" in args:
            updates["name"] = args["name"]
        if "credit_weight" in args:
            updates["credit_weight"] = args["credit_weight"]
        if "max_concurrent_agents" in args:
            updates["max_concurrent_agents"] = args["max_concurrent_agents"]
        if "budget_limit" in args:
            updates["budget_limit"] = args["budget_limit"]
        if "discord_channel_id" in args:
            updates["discord_channel_id"] = args["discord_channel_id"]
        if "default_profile_id" in args:
            dpid = args["default_profile_id"]
            if dpid is not None:
                profile = await self.db.get_profile(dpid)
                if not profile:
                    return {"error": f"Profile '{dpid}' not found"}
            updates["default_profile_id"] = dpid  # None clears it
        if "default_agent_type" in args:
            dat = args["default_agent_type"]
            if isinstance(dat, str) and dat.strip().lower() in ("none", "null", ""):
                dat = None
            if dat is not None:
                if not isinstance(dat, str):
                    return {"error": "default_agent_type must be a string"}
                dat = dat.strip()
                if not dat:
                    return {"error": "default_agent_type cannot be empty"}
            updates["default_agent_type"] = dat  # None clears it
        if "repo_default_branch" in args:
            updates["repo_default_branch"] = args["repo_default_branch"]
        if not updates:
            return {
                "error": (
                    "No fields to update. Provide name, credit_weight, "
                    "max_concurrent_agents, budget_limit, discord_channel_id, "
                    "default_profile_id, default_agent_type, or repo_default_branch."
                )
            }
        await self.db.update_project(pid, **updates)
        return {"updated": pid, "fields": list(updates.keys())}

    async def _cmd_set_project_channel(self, args: dict) -> dict:
        """Link an existing Discord channel to a project."""
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}

        channel_id = args["channel_id"]
        await self.db.update_project(pid, discord_channel_id=channel_id)

        return {
            "project_id": pid,
            "channel_id": channel_id,
            "status": "linked",
        }

    async def _cmd_set_default_branch(self, args: dict) -> dict:
        """Set (or change) a project's default branch.

        If the branch does not exist on the remote yet, it is created by
        pushing the current HEAD of the old default branch to the new name.
        """
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}

        branch = args.get("branch", "").strip()
        if not branch:
            return {"error": "branch is required"}

        old_branch = project.repo_default_branch or "main"

        # If the project has a workspace, optionally create the branch
        # on the remote when it doesn't exist yet.
        ws_path = await self.db.get_project_workspace_path(pid)
        branch_created = False
        if ws_path:
            git = self.orchestrator.git
            try:
                # Fetch latest so we know what branches exist on the remote
                await git._arun(["fetch", "origin"], cwd=ws_path)

                # Check if the branch exists on the remote
                try:
                    await git._arun(
                        ["rev-parse", "--verify", f"refs/remotes/origin/{branch}"],
                        cwd=ws_path,
                    )
                except Exception:
                    # Branch does not exist on the remote — create it from
                    # the current default branch (or HEAD).
                    try:
                        await git._arun(
                            ["branch", branch, f"origin/{old_branch}"],
                            cwd=ws_path,
                        )
                    except Exception:
                        # If old default branch ref doesn't exist, branch from HEAD
                        await git._arun(["branch", branch, "HEAD"], cwd=ws_path)
                    await git._arun(
                        ["push", "-u", "origin", branch],
                        cwd=ws_path,
                    )
                    branch_created = True
            except Exception as exc:
                logger.warning(
                    "Could not verify/create branch %s for project %s: %s",
                    branch,
                    pid,
                    exc,
                )

        await self.db.update_project(pid, repo_default_branch=branch)

        result: dict = {
            "project_id": pid,
            "default_branch": branch,
            "previous_branch": old_branch,
            "status": "updated",
        }
        if branch_created:
            result["branch_created"] = True
        return result

    async def _cmd_set_control_interface(self, args: dict) -> dict:
        """Set a project's channel by channel *name* (string lookup).

        Resolves the channel name within the guild, then delegates to
        ``_cmd_set_project_channel``.
        Requires ``guild_channels`` to be supplied by the caller (the Discord
        command layer passes the guild's text channels so this layer stays
        Discord-import-free).
        """
        pid = args.get("project_id") or args.get("project_name")
        if not pid:
            return {"error": "project_id (or project_name) is required"}
        channel_name: str | None = args.get("channel_name")
        if not channel_name:
            return {"error": "channel_name is required"}

        # Normalise: strip leading '#' if the user included one.
        channel_name = channel_name.lstrip("#").strip()

        # --- Resolve channel name → ID ---
        # Option A: The caller already looked up the ID (Discord slash command).
        channel_id: str | None = args.get("_resolved_channel_id")

        if not channel_id:
            # Option B: guild_channels list supplied (list of {id, name} dicts).
            guild_channels = args.get("guild_channels")
            if guild_channels:
                for ch in guild_channels:
                    if ch["name"] == channel_name:
                        channel_id = str(ch["id"])
                        break
                if not channel_id:
                    return {"error": f"No text channel named '{channel_name}' found in this server"}
            else:
                return {
                    "error": (
                        "Cannot resolve channel name without guild context. "
                        "Use set_project_channel with a channel_id instead, "
                        "or invoke this command from Discord."
                    )
                }

        # Delegate to the existing set_project_channel handler.
        return await self._cmd_set_project_channel(
            {
                "project_id": pid,
                "channel_id": channel_id,
            }
        )

    async def _cmd_get_project(self, args: dict) -> dict:
        """Return full details for a single project."""
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        ws_path = await self.db.get_project_workspace_path(pid)
        usage = await self.db.get_project_token_usage(pid)
        info: dict = {
            "id": project.id,
            "name": project.name,
            "status": project.status.value,
            "repo_url": project.repo_url or "",
            "repo_default_branch": project.repo_default_branch,
            "workspace": ws_path,
            "credit_weight": project.credit_weight,
            "max_concurrent_agents": project.max_concurrent_agents,
            "total_tokens_used": project.total_tokens_used,
            "tokens_used_recent": usage,
        }
        if project.budget_limit is not None:
            info["budget_limit"] = project.budget_limit
        if project.discord_channel_id:
            info["discord_channel_id"] = project.discord_channel_id
        if project.default_profile_id:
            info["default_profile_id"] = project.default_profile_id
        if project.default_agent_type:
            info["default_agent_type"] = project.default_agent_type
        return info

    async def _cmd_get_project_channels(self, args: dict) -> dict:
        """Return the Discord channel ID configured for a project."""
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        return {
            "project_id": pid,
            "channel_id": project.discord_channel_id,
        }

    async def _cmd_get_project_for_channel(self, args: dict) -> dict:
        """Reverse lookup: find which project a Discord channel belongs to.

        Scans all projects and checks ``discord_channel_id``.
        Returns the first match, or ``project_id: null`` if no project
        is linked to the channel.
        """
        channel_id = args.get("channel_id")
        if not channel_id:
            return {"error": "channel_id is required"}

        channel_id = str(channel_id)
        projects = await self.db.list_projects()
        for project in projects:
            if project.discord_channel_id == channel_id:
                return {
                    "channel_id": channel_id,
                    "project_id": project.id,
                    "project_name": project.name,
                }

        return {
            "channel_id": channel_id,
            "project_id": None,
            "project_name": None,
        }

    async def _cmd_delete_project(self, args: dict) -> dict:
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        tasks = await self.db.list_tasks(project_id=pid, status=TaskStatus.IN_PROGRESS)
        if tasks:
            return {
                "error": f"Cannot delete: {len(tasks)} task(s) currently IN_PROGRESS. "
                "Stop them first."
            }

        # Capture channel ID before the DB cascade removes it.
        channel_ids: dict[str, str] = {}
        if project.discord_channel_id:
            channel_ids["channel"] = project.discord_channel_id

        await self.db.delete_project(pid)

        # Notify listeners (e.g. Discord bot) so they can purge in-memory
        # channel caches, notes-thread mappings, etc.
        if self._on_project_deleted:
            self._on_project_deleted(pid)

        result: dict = {"deleted": pid, "name": project.name}
        if channel_ids:
            result["channel_ids"] = channel_ids
        # Pass through the caller's archive preference so the Discord layer
        # can act on it.
        archive = args.get("archive_channels", False)
        if archive:
            result["archive_channels"] = True
        return result
