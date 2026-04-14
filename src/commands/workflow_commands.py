"""Workflow commands mixin — workflow CRUD, stage advancement."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class WorkflowCommandsMixin:
    """Workflow command methods mixed into CommandHandler."""

    # ------------------------------------------------------------------
    # Workflow commands (Roadmap 7.6.1)
    # ------------------------------------------------------------------

    async def _cmd_create_workflow(self, args: dict) -> dict:
        """Create a new coordination workflow record.

        Typically called by a coordination playbook's first node to register
        the workflow that will track stage progression and agent affinity.

        Args:
            workflow_id: Unique identifier for the workflow.
            playbook_id: Source coordination playbook ID.
            playbook_run_id: The PlaybookRun driving this workflow.
            project_id: Project this workflow operates in.
            current_stage: Optional initial stage name.
        """
        from src.models import Workflow

        workflow_id = args.get("workflow_id", "").strip()
        if not workflow_id:
            return {"error": "workflow_id is required"}

        playbook_id = args.get("playbook_id", "").strip()
        if not playbook_id:
            return {"error": "playbook_id is required"}

        playbook_run_id = args.get("playbook_run_id", "").strip()
        if not playbook_run_id:
            return {"error": "playbook_run_id is required"}

        project_id = args.get("project_id", "").strip()
        if not project_id:
            return {"error": "project_id is required"}

        import time

        workflow = Workflow(
            workflow_id=workflow_id,
            playbook_id=playbook_id,
            playbook_run_id=playbook_run_id,
            project_id=project_id,
            current_stage=args.get("current_stage"),
            created_at=time.time(),
        )

        await self.db.create_workflow(workflow)
        return {
            "success": True,
            "workflow_id": workflow_id,
            "status": workflow.status,
        }

    async def _cmd_get_workflow(self, args: dict) -> dict:
        """Fetch a single workflow by ID.

        Args:
            workflow_id: The workflow to retrieve.
        """
        workflow_id = args.get("workflow_id", "").strip()
        if not workflow_id:
            return {"error": "workflow_id is required"}

        workflow = await self.db.get_workflow(workflow_id)
        if not workflow:
            return {"error": f"Workflow '{workflow_id}' not found"}

        return {
            "success": True,
            "workflow_id": workflow.workflow_id,
            "playbook_id": workflow.playbook_id,
            "playbook_run_id": workflow.playbook_run_id,
            "project_id": workflow.project_id,
            "status": workflow.status,
            "current_stage": workflow.current_stage,
            "task_ids": workflow.task_ids,
            "agent_affinity": workflow.agent_affinity,
            "stages": workflow.stages,
            "created_at": workflow.created_at,
            "completed_at": workflow.completed_at,
        }

    async def _cmd_list_workflows(self, args: dict) -> dict:
        """List workflows with optional filters.

        Args:
            project_id: Optional — filter to workflows in this project.
            playbook_id: Optional — filter to workflows from this playbook.
            status: Optional — filter to workflows with this status.
            limit: Optional — max results (default 50).
        """
        project_id = args.get("project_id", "").strip() if args.get("project_id") else None
        playbook_id = args.get("playbook_id", "").strip() if args.get("playbook_id") else None
        status = args.get("status", "").strip() if args.get("status") else None
        limit = int(args.get("limit", 50))

        workflows = await self.db.list_workflows(
            project_id=project_id,
            playbook_id=playbook_id,
            status=status,
            limit=limit,
        )

        return {
            "success": True,
            "workflows": [
                {
                    "workflow_id": w.workflow_id,
                    "playbook_id": w.playbook_id,
                    "project_id": w.project_id,
                    "status": w.status,
                    "current_stage": w.current_stage,
                    "task_count": len(w.task_ids),
                    "stage_count": len(w.stages),
                    "created_at": w.created_at,
                    "completed_at": w.completed_at,
                }
                for w in workflows
            ],
            "count": len(workflows),
        }

    async def _cmd_advance_workflow_stage(self, args: dict) -> dict:
        """Advance a workflow to its next stage.

        Records the current stage as completed in the stages history and
        sets the new current_stage.  Optionally adds task IDs for the
        new stage.

        Args:
            workflow_id: The workflow to advance.
            stage_name: Name of the new stage.
            task_ids: Optional list of task IDs for the new stage.
        """
        workflow_id = args.get("workflow_id", "").strip()
        if not workflow_id:
            return {"error": "workflow_id is required"}

        stage_name = args.get("stage_name", "").strip()
        if not stage_name:
            return {"error": "stage_name is required"}

        workflow = await self.db.get_workflow(workflow_id)
        if not workflow:
            return {"error": f"Workflow '{workflow_id}' not found"}

        if workflow.status not in ("running", "paused"):
            return {
                "error": (
                    f"Cannot advance workflow in status '{workflow.status}'. "
                    "Workflow must be running or paused."
                ),
            }

        import time

        # Close out the current stage in the stages history
        stages = list(workflow.stages)
        if workflow.current_stage:
            # Find the current stage in history and mark it completed
            for stage in stages:
                if stage.get("name") == workflow.current_stage and not stage.get("completed_at"):
                    stage["status"] = "completed"
                    stage["completed_at"] = time.time()
                    break

        # Add the new stage
        new_task_ids = args.get("task_ids", [])
        if isinstance(new_task_ids, str):
            new_task_ids = [tid.strip() for tid in new_task_ids.split(",") if tid.strip()]

        stages.append({
            "name": stage_name,
            "task_ids": new_task_ids,
            "status": "active",
            "started_at": time.time(),
            "completed_at": None,
        })

        # Update the workflow
        await self.db.update_workflow(
            workflow_id,
            current_stage=stage_name,
            stages=json.dumps(stages),
        )

        # Add new task IDs to the workflow's global task list
        for tid in new_task_ids:
            await self.db.add_workflow_task(workflow_id, tid)

        return {
            "success": True,
            "workflow_id": workflow_id,
            "previous_stage": workflow.current_stage,
            "current_stage": stage_name,
            "stage_count": len(stages),
            "new_task_ids": new_task_ids,
        }

    async def _cmd_workflow_pipeline_view(self, args: dict) -> dict:
        """Return structured pipeline view data for dashboard rendering of a workflow.

        Produces a complete JSON representation of the workflow pipeline suitable
        for interactive dashboard visualization: stages as pipeline columns,
        tasks as cards within each stage, agent assignments as badges, and
        progress indicators.

        Implements spec §11 Q6 (Workflow Visualization), roadmap 7.6.1.

        Args:
            workflow_id: The workflow to visualize.
            direction: Layout direction — ``"LR"`` (left-right) or ``"TD"``
                (top-down). Default: ``"LR"``.
            include_task_details: Include individual task cards. Default: ``True``.
            include_affinity: Include agent affinity overlay. Default: ``True``.
        """
        from src.workflow_pipeline_view import build_pipeline_view

        workflow_id = args.get("workflow_id", "").strip()
        if not workflow_id:
            return {"error": "workflow_id is required"}

        workflow = await self.db.get_workflow(workflow_id)
        if not workflow:
            return {"error": f"Workflow '{workflow_id}' not found"}

        direction = args.get("direction", "LR").strip().upper()
        if direction not in ("LR", "TD"):
            return {"error": f"Invalid direction '{direction}'. Valid: LR, TD"}

        include_task_details = args.get("include_task_details", True)
        if isinstance(include_task_details, str):
            include_task_details = include_task_details.lower() in ("true", "1", "yes")

        include_affinity = args.get("include_affinity", True)
        if isinstance(include_affinity, str):
            include_affinity = include_affinity.lower() in ("true", "1", "yes")

        # Fetch all tasks in the workflow
        tasks = []
        for task_id in workflow.task_ids:
            task = await self.db.get_task(task_id)
            if task:
                tasks.append(task)

        # Fetch agent details for enrichment
        agents = None
        try:
            agent_list = await self.db.list_agents(project_id=workflow.project_id)
            if agent_list:
                agents = [
                    {
                        "id": a.id if hasattr(a, "id") else a.get("id"),
                        "name": a.name if hasattr(a, "name") else a.get("name"),
                        "state": (
                            a.state.value
                            if hasattr(a, "state") and hasattr(a.state, "value")
                            else str(a.get("state", ""))
                        ),
                    }
                    for a in agent_list
                ]
        except Exception:
            pass  # agent enrichment is optional

        result = build_pipeline_view(
            workflow,
            tasks,
            agents=agents,
            include_task_details=include_task_details,
            include_affinity=include_affinity,
            direction=direction,
        )

        result["success"] = True
        return result
