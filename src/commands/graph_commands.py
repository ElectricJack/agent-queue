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
        graph_layout = getattr(self.config, "graph_layout", None)
        driver = LayoutDriver(
            self.db,
            row_aspect=getattr(graph_layout, "row_aspect", 1.3),
        )
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
        if args.get("all"):
            if pid or args.get("variant"):
                return {
                    "success": False,
                    "error": "all cannot be combined with project_id or variant",
                }
            request = await self.db.create_layout_tidy_request(reason="operator")
            return {"success": True, "request": request}
        if not pid or await self.db.get_project(pid) is None:
            return {"success": False, "error": f"No project '{pid}'"}
        variants = [args["variant"]] if args.get("variant") in VARIANTS else list(VARIANTS)
        jobs = [await self.db.enqueue_layout_job(pid, v, "tidy") for v in variants]
        return {"success": True, "project_id": pid, "jobs": jobs}
