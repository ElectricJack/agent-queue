"""Read-only plan content served to the standalone dashboard."""

from pydantic import BaseModel


class PlanContentResponse(BaseModel):
    task_id: str
    content: str
