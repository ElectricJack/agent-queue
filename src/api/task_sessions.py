"""Read-only task execution history, including archived tasks."""

from fastapi import APIRouter, HTTPException, Request

from src.api import dependencies as deps
from src.api.auth import LOCAL_SCOPE
from src.api.models.task import TaskSessionsResponse


def build_task_sessions_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/tasks/{task_id}/sessions", response_model=TaskSessionsResponse)
    async def task_sessions(task_id: str, request: Request):
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        task = await orch.db.get_task(task_id)
        if task is None:
            task = await orch.db.get_archived_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        project_id = task.get("project_id") if isinstance(task, dict) else task.project_id
        scope = getattr(request.state, "scope", LOCAL_SCOPE)
        if scope.kind != "local":
            global_admin = scope.elevated and scope.project_id is None
            if not global_admin and (
                scope.project_id is None
                or scope.project_id != project_id
                or (scope.task_id is not None and scope.task_id != task_id and not scope.elevated)
            ):
                raise HTTPException(status_code=403, detail="Task is out of scope")
        created_at = task.get("created_at") if isinstance(task, dict) else task.created_at
        return {
            "task_id": task_id,
            "sessions": await orch.db.list_task_session_attempts(
                task_id,
                project_id=project_id,
                since=created_at,
            ),
        }

    return router


router = build_task_sessions_router()
