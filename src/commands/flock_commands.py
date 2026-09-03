"""Global agent registry commands; workspaces remain project resources."""
from __future__ import annotations

from uuid import uuid4

from src.agents.configuration import SUPERVISOR_AGENT_ID
from src.agents.service import list_agent_flock
from src.agents.subagents import flock_rollup
from src.models import Agent


class FlockCommandsMixin:
    def _agent_settings_scope_error(self) -> str | None:
        scope = self._current_scope
        if not scope or scope.get("kind") == "local":
            return None
        if scope.get("elevated") and scope.get("project_id") is None:
            return None
        return "out of scope: global agent settings require global admin"

    async def _cmd_list_agents(self, args: dict) -> dict:
        project_id = args.get("project_id")
        if project_id and await self.db.get_project(project_id) is None:
            return {"error": f"Project '{project_id}' not found"}
        rows = await list_agent_flock(self.orchestrator, project_id=project_id)
        rollup = flock_rollup(rows)
        return {
            "agents": rows,
            "count": len(rows),
            "project_id": project_id,
            # The flock header shows one number; computing it here means the
            # dashboard, the CLI and any API caller agree on it — and on
            # whether it is a total or a floor.
            "subagents": rollup["totals"],
            "subagents_by_profile": rollup["by_profile"],
        }

    async def _cmd_get_agent(self, args: dict) -> dict:
        agent_id = args.get("agent_id")
        if not agent_id:
            return {"error": "agent_id is required"}
        rows = await list_agent_flock(self.orchestrator, project_id=args.get("project_id"))
        for row in rows:
            if row["id"] == agent_id:
                return row
        return {"error": f"Agent '{agent_id}' not found in this scope"}

    async def _validate_agent_settings(self, args: dict, current=None) -> tuple[dict, str | None]:
        fields = {}
        for key in ("name", "profile_id", "harness", "model", "intelligence_class"):
            if key not in args:
                continue
            value = args[key]
            if value is not None and not isinstance(value, str):
                return {}, f"{key} must be a string"
            value = value.strip() if value else None
            if value and (len(value) > 200 or any(ord(c) < 32 for c in value)):
                return {}, f"{key} must be at most 200 characters without control characters"
            if key in {"name", "profile_id"} and not value:
                return {}, f"{key} is required"
            fields[key] = value
        if current is None:
            for key in ("name", "profile_id"):
                if not fields.get(key):
                    return {}, f"{key} is required"
        if "enabled" in args:
            if not isinstance(args["enabled"], bool):
                return {}, "enabled must be a boolean"
            fields["enabled"] = args["enabled"]
        name = fields.get("name")
        if name:
            for other in await self.db.list_agents():
                if current and other.id == current.id:
                    continue
                if other.name.casefold() == name.casefold():
                    return {}, f"An agent named '{name}' already exists"
        profile_id = fields.get("profile_id") or (current.profile_id if current else None)
        profile = await self.db.get_profile(profile_id) if profile_id else None
        if profile is None:
            return {}, f"Profile '{profile_id}' not found"
        if current and current.id == SUPERVISOR_AGENT_ID:
            if profile_id != "supervisor":
                return {}, "The global supervisor must use the supervisor profile"
        elif profile_id == "supervisor":
            return {}, "The global supervisor is already defined; edit Supervisor instead"

        harness_id = fields.get("harness")
        if harness_id:
            registry = getattr(self.orchestrator, "harness_registry", None)
            if registry is None or registry.get(harness_id) is None:
                return {}, f"Global harness '{harness_id}' not found"
        class_id = fields.get("intelligence_class")
        if class_id:
            # The live registry, so a class added to the vault a moment ago is
            # accepted without a daemon restart.
            classes = self._live_intelligence_classes() or {}
            if class_id not in classes:
                return {}, f"Intelligence class '{class_id}' not found"
        return fields, None

    async def _cmd_create_agent(self, args: dict) -> dict:
        if error := self._agent_settings_scope_error():
            return {"error": error}
        fields, error = await self._validate_agent_settings(args)
        if error:
            return {"error": error}
        agent = Agent(id=f"agent-{uuid4().hex[:12]}", **fields)
        await self.db.create_agent(agent)
        await self.orchestrator.bus.emit("agent.created", {
            "event_type": "agent.created", "agent_id": agent.id,
        })
        return await self._cmd_get_agent({"agent_id": agent.id})

    async def _cmd_edit_agent(self, args: dict) -> dict:
        if error := self._agent_settings_scope_error():
            return {"error": error}
        agent_id = args.get("agent_id")
        if not agent_id:
            return {"error": "agent_id is required"}
        agent = await self.db.get_agent(agent_id)
        if agent is None or agent.deleted_at is not None:
            return {"error": f"Agent '{agent_id}' not found"}
        fields, error = await self._validate_agent_settings(args, agent)
        if error:
            return {"error": error}
        if fields:
            await self.db.update_agent(agent.id, **fields)
            await self.orchestrator.bus.emit("agent.updated", {
                "event_type": "agent.updated", "agent_id": agent.id,
            })
        return await self._cmd_get_agent({"agent_id": agent.id})

    async def _cmd_delete_agent(self, args: dict) -> dict:
        if error := self._agent_settings_scope_error():
            return {"error": error}
        agent_id = args.get("agent_id")
        if not agent_id:
            return {"error": "agent_id is required"}
        agent = await self.db.get_agent(agent_id)
        if agent is None or agent.deleted_at is not None:
            return {"error": f"Agent '{agent_id}' not found"}
        if agent.id == SUPERVISOR_AGENT_ID or agent.role == "supervisor":
            return {"error": "The global supervisor cannot be deleted"}
        if not await self.db.soft_delete_agent(agent.id):
            return {
                "error": "Agent must be idle with no active task, live session, "
                "or held workspace before deletion."
            }
        await self.orchestrator.bus.emit("agent.deleted", {
            "event_type": "agent.deleted", "agent_id": agent.id,
        })
        return {"deleted": agent.id, "name": agent.name}

    async def _cmd_start_agent_terminal(self, args: dict) -> dict:
        from src.agents.terminals import TerminalStartError, start_agent_terminal

        if error := self._agent_settings_scope_error():
            return {"error": error}
        agent_id = args.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            return {"error": "agent_id is required"}
        try:
            await start_agent_terminal(self.orchestrator, agent_id, config=self.config)
        except TerminalStartError as exc:
            return {"error": str(exc)}
        return await self._cmd_get_agent({"agent_id": agent_id})
