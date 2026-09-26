"""Scoped job commands and atomic submit-with-wait."""

from __future__ import annotations
import asyncio
from pathlib import Path
from sqlalchemy import select
from src.agent_waits import WaitError
from pydantic import ValidationError
from src.commands.principal import current_principal, PrincipalKind
from src.jobs.policy import JobError
from src.jobs.service import JobService
from src.jobs.artifacts import OutputStore, job_directory
from src.database.tables import workspaces


class JobCommandsMixin:
    def _job_scope(self):
        principal = current_principal()
        if principal and principal.kind == PrincipalKind.SESSION:
            return dict(
                kind="session",
                project_id=principal.project_id,
                task_id=principal.task_id,
                session_id=principal.session_id,
                elevated=principal.elevated,
            )
        return self._current_scope or {}

    def _jobs(self):
        service = getattr(self.orchestrator, "job_service", None)
        if service is None:
            service = self.orchestrator.job_service = JobService(self.db, self.config)
        service.config = self.config
        return service

    async def _cmd_job_submit_integration(self, args):
        """Internal only: provision a detached snapshot and submit at band zero.

        Disabled workspace rows keep snapshots out of worker allocation. Their
        pins and artifacts remain available across publisher/daemon restarts.
        """
        import hashlib
        import json
        from src.database.tables import jobs
        from src.git.manager import GitManager

        try:
            service = self._jobs()
            if not service.settings.enabled:
                raise JobError("jobs.disabled")
            # A replay is checked before inspecting a completed job's working
            # directory, which can contain generated validation artifacts.
            digest = hashlib.sha256(
                json.dumps(args, sort_keys=True).encode()
            ).hexdigest()
            key = args["idempotency_key"]
            async with self.db._engine.begin() as conn:
                from sqlalchemy import func

                await conn.execute(select(func.pg_advisory_xact_lock(109796, func.hashtext(key))))
                existing = (await conn.execute(select(jobs).where(
                    jobs.c.project_id == args["project_id"],
                    jobs.c.owner_kind == "integration",
                    jobs.c.owner_id == args["operation_id"],
                    jobs.c.idempotency_key == key,
                ))).mappings().first()
                if existing:
                    if existing["contract"].get("adapter_request_hash") != digest:
                        raise JobError("jobs.idempotency_conflict")
                    return {"success": True, "job": dict(existing)}
                blocked = await conn.scalar(select(jobs.c.id).where(
                    jobs.c.project_id == args["project_id"],
                    jobs.c.owner_kind == "integration", jobs.c.owner_id == args["operation_id"],
                    jobs.c.cleanup_blocked.is_(True),
                ).limit(1))
                if blocked:
                    raise JobError("jobs.cleanup_blocked")
                snapshot_id = "job-snapshot-" + digest
                snapshot = Path(self.config.data_dir) / "job-snapshots" / digest
                git = GitManager()
                if not snapshot.exists():
                    await asyncio.to_thread(snapshot.parent.mkdir, parents=True, exist_ok=True)
                    await git._arun([
                        "clone", "--shared", "--dissociate", "--no-checkout", args["store"], str(snapshot)
                    ])
                    await git._arun(["checkout", "--detach", args["input_ref"]], cwd=str(snapshot))
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                from src.models import RepoSourceType
                import time

                await conn.execute(pg_insert(workspaces).values(
                    id=snapshot_id, project_id=args["project_id"], workspace_path=str(snapshot),
                    source_type=RepoSourceType.LINK.value, kind_id="job-snapshot",
                    enabled=False, created_at=time.time(),
                ).on_conflict_do_nothing(index_elements=[workspaces.c.id]))
            job = await service.submit(
                project_id=args["project_id"], task_id=None, session_id=None,
                claim_epoch=None, workspace_id=snapshot_id, generation=0,
                owner_kind="integration", owner_id=args["operation_id"],
                input_mode="snapshot", input_ref=args["input_ref"], trusted_band=0,
                preset=args["preset"], args=args["argv"], idempotency_key=key,
                queue_seconds=args["queue_seconds"], run_seconds=args["run_seconds"],
                adapter_request_hash=digest,
            )
            return {"success": True, "job": job}
        except (JobError, ValueError, OSError) as exc:
            return {"success": False, "error_code": str(exc), "error": str(exc)}

    async def _job_for_scope(self, job_id):
        job = await self.db.get_job(job_id)
        scope = self._job_scope()
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
        scope = self._job_scope()
        try:
            from src.commands.contracts.job import JobSubmitArgs

            args = JobSubmitArgs.model_validate(args).model_dump(exclude_none=True)
            for key in ("project_id", "task_id", "session_id"):
                if scope.get(key) and args.get(key, scope[key]) != scope[key]:
                    raise JobError("jobs.out_of_scope")
            identity = await self._wait_identity(args, mutation=True) if args.get("wait") else None
            project_id = scope.get("project_id") or args["project_id"]
            task_id = scope.get("task_id") or args["task_id"]
            task = await self.db.get_task(task_id)
            if not task or task.project_id != project_id:
                raise JobError("jobs.not_found")
            denied = await self._assert_session_owns(
                task_id, session_id=scope.get("session_id") or args.get("session_id"),
                claim_epoch=args.get("claim_epoch")
            )
            if denied:
                return {
                    "success": False,
                    "error_code": "jobs.stale_claim"
                    if denied.get("result") == "stale_claim"
                    else "jobs.out_of_scope",
                    "error": denied["error"],
                }
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
                session_id=scope.get("session_id") or args.get("session_id"),
                claim_epoch=args.get("claim_epoch", task.claim_epoch),
                workspace_id=ws.id,
                generation=generation,
                preset=args["preset"],
                args=args.get("argv", []),
                idempotency_key=args["idempotency_key"],
                trusted_band=1 if scope.get("elevated") else 2,
                wait_identity=identity,
            )
            wait = result.pop("wait", None)
            response = {"success": True, "job": result}
            if wait:
                response.update(
                    wait=wait,
                    next_step=(
                        f"End this turn. Resume with aq wait show {wait['id']} --json. "
                        "The claim, workspace and seat remain held."
                    )
                    if wait["state"] == "active"
                    else f"Read aq wait show {wait['id']} --json.",
                )
            return response
        except (JobError, WaitError, ValidationError, KeyError, ValueError) as exc:
            code = (
                exc.code
                if isinstance(exc, WaitError)
                else (str(exc) if isinstance(exc, JobError) else "jobs.invalid")
            )
            return {
                "success": False,
                "error_code": code,
                "error": str(exc),
            }

    async def _cmd_job_list(self, args):
        scope = self._job_scope()
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
