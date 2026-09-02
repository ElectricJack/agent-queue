"""``graph_layout_rebuild`` / ``graph_tidy`` (spatial-layout design §5.6, §10)."""

from __future__ import annotations

from src.task_graph.layout.constants import VARIANTS
from src.task_graph.layout.driver import LayoutDriver


class GraphCommandsMixin:
    async def _cmd_graph_layout_rebuild(self, args: dict) -> dict:
        scope = self._current_scope or {}
        if scope.get("kind") == "session" and not scope.get("elevated"):
            return {
                "success": False,
                "error": "graph_layout_rebuild is not available to agent sessions",
            }

        pid = args.get("project_id")
        if not pid or await self.db.get_project(pid) is None:
            return {"success": False, "error": f"No project '{pid}'"}
        driver = LayoutDriver(self.db)
        versions = {v: await driver.full_layout(pid, v) for v in VARIANTS}
        return {"success": True, "project_id": pid, "versions": versions}

    async def _cmd_graph_tidy(self, args: dict) -> dict:
        scope = self._current_scope or {}
        if scope.get("kind") == "session" and not scope.get("elevated"):
            return {
                "success": False,
                "error": "graph_tidy is not available to agent sessions",
            }

        pid = args.get("project_id")
        if not pid or await self.db.get_project(pid) is None:
            return {"success": False, "error": f"No project '{pid}'"}
        variants = [args["variant"]] if args.get("variant") in VARIANTS else list(VARIANTS)
        jobs = [await self.db.enqueue_layout_job(pid, v, "tidy") for v in variants]
        return {"success": True, "project_id": pid, "jobs": jobs}
