"""``task_batch_validate`` — run the batch automatic checks without writing.

The checks themselves live in :mod:`src.task_graph.batch_checks`; propose,
scoped edit and release call them inside their own transactions.  This
read-only command exposes the same report so an ingestion agent or reviewer
can see the findings for a change set, or for a stored batch revision,
before it asks for a write.
"""

from __future__ import annotations

from sqlalchemy import select

from src.database.tables import projects
from src.task_graph import batch_checks

_OPERATIONS = ("tasks", "edges", "edits", "remove_edges", "comments")


class BatchCheckCommandsMixin:
    """Mount on CommandHandler alongside the other command mixins."""

    async def _cmd_task_batch_validate(self, args: dict) -> dict:
        project_id = args.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            return {"success": False, "error": "project_id is required"}
        payload = {key: args.get(key) or [] for key in _OPERATIONS}
        for key, items in payload.items():
            if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                return {"success": False, "error": f"{key} must be a list of objects"}
        expected = args.get("expected_revision")
        if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
            return {"success": False, "error": "expected_revision must be an integer"}
        batch_id = args.get("batch_id") or None
        authority = batch_checks.Authority(project_id, args.get("source") or None)
        async with self.db._engine.connect() as conn:
            found = (
                await conn.execute(select(projects.c.id).where(projects.c.id == project_id))
            ).first()
            if found is None:
                return {"success": False, "error": f"project '{project_id}' not found"}
            try:
                report = await batch_checks.run(
                    conn, payload, authority=authority,
                    expected_revision=expected, batch_id=batch_id,
                )
            except batch_checks.BatchNotFound as exc:
                return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "project_id": project_id,
            "batch_id": batch_id,
            **report.to_dict(),
        }
