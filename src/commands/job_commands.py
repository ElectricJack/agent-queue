"""Phase 2 command boundary. Wait/CLI typed contracts follow in the job adapter."""

from __future__ import annotations
import asyncio
from pathlib import Path
from sqlalchemy import select
from src.jobs.policy import JobError
from src.jobs.service import JobService
from src.jobs.artifacts import OutputStore, job_directory
from src.database.tables import workspaces


class JobCommandsMixin:
    def _jobs(self):
        service = getattr(self.orchestrator, "job_service", None)
        if service is None:
            service = self.orchestrator.job_service = JobService(self.db, self.config)
        service.config = self.config
        return service

    async def _job_for_scope(self, job_id):
        job = await self.db.get_job(job_id)
        scope = self._current_scope or {}
        if not job:
            return None
        if scope.get("project_id") and scope["project_id"] != job["project_id"]:
            return None
        if scope and scope.get("kind") != "local" and not scope.get("elevated"):
            if (
                job["owner_kind"] != "task"
                or not scope.get("task_id")
                or scope["task_id"] != job["task_id"]
            ):
                return None
        if job["owner_kind"] == "task" and not await self.db.get_task(job["task_id"]):
            return None
        return job

    async def _cmd_job_submit(self, args):
        scope = self._current_scope or {}
        if args.get("wait"):
            return {"success": False, "error": "jobs.wait_not_enabled"}
        try:
            project_id = scope.get("project_id") or args["project_id"]
            task_id = scope.get("task_id") or args["task_id"]
            task = await self.db.get_task(task_id)
            if not task or task.project_id != project_id:
                raise JobError("jobs.not_found")
            denied = await self._assert_session_owns(
                task_id, session_id=scope.get("session_id"), claim_epoch=args.get("claim_epoch")
            )
            if denied:
                return denied
            ws = await self.db.get_workspace_for_task(task_id)
            if not ws:
                raise JobError("jobs.cwd_invalid")
            async with self.db._engine.connect() as conn:
                generation = await conn.scalar(
                    select(workspaces.c.generation).where(workspaces.c.id == ws.id)
                )
            result = await self._jobs().submit(
                project_id=project_id,
                task_id=task_id,
                session_id=scope.get("session_id"),
                claim_epoch=args.get("claim_epoch", task.claim_epoch),
                workspace_id=ws.id,
                generation=generation,
                preset=args["preset"],
                args=args.get("argv", []),
                idempotency_key=args["idempotency_key"],
                trusted_band=1 if scope.get("elevated") else 2,
            )
            return {"success": True, "job": result}
        except (JobError, KeyError, ValueError) as exc:
            return {"success": False, "error": str(exc)}

    async def _cmd_job_list(self, args):
        scope = self._current_scope or {}
        project_id = scope.get("project_id") or args.get("project_id")
        task_id = scope.get("task_id") or args.get("task_id")
        if not project_id:
            return {"success": False, "error": "jobs.project_required"}
        rows = await self.db.list_jobs(
            project_id=project_id, task_id=task_id, limit=max(1, min(100, args.get("limit", 50)))
        )
        visible = []
        for row in rows:
            if await self._job_for_scope(row["id"]):
                visible.append(row)
        return {"success": True, "jobs": visible}

    async def _cmd_job_get(self, args):
        job = await self._job_for_scope(args["job_id"])
        return {"success": True, "job": job} if job else {"success": False, "error": "not_found"}

    async def _cmd_job_cancel(self, args):
        job = await self._job_for_scope(args["job_id"])
        if not job:
            return {"success": False, "error": "not_found"}
        return {"success": True, "job": await self._jobs().cancel(job)}

    async def _cmd_job_result(self, args):
        job = await self._job_for_scope(args["job_id"])
        if not job:
            return {"success": False, "error": "not_found"}
        result = job["result"]
        if result and "max_bytes" in args:
            from src.jobs.result import bounded

            result = {
                **result,
                "excerpt": bounded(result["excerpt"], max(0, min(8192, args["max_bytes"]))),
            }
        return {"success": True, "result": result}

    async def _cmd_job_logs(self, args):
        job = await self._job_for_scope(args["job_id"])
        if not job:
            return {"success": False, "error": "not_found"}
        if job["output_retention"] == "expired":
            return {"success": False, "error": "logs_expired", "result": job["result"]}

        def read():
            directory = job_directory(Path(self.config.data_dir), job["id"])
            store = OutputStore(
                directory,
                head_bytes=job["contract"]["head_bytes"],
                tail_bytes=job["contract"]["tail_bytes"],
                readonly=True,
            )
            try:
                response = store.read(args.get("after", 0), args.get("limit", 65536))
                for chunk in response["chunks"]:
                    chunk["data"] = chunk["data"].decode("utf-8", "replace")
                return response
            finally:
                store.close()

        try:
            return {"success": True, **await asyncio.to_thread(read)}
        except FileNotFoundError:
            return {"success": False, "error": "logs_not_ready"}

    async def _cmd_job_reconcile(self, args):
        await self._jobs().tick()
        return {"success": True}
