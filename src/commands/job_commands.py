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
from src.jobs.output import read_output
from src.database.tables import workspaces
from src.jobs.matter import candidate_document
from src.object_loop.artifacts import ArtifactError, retain_document, retain_file
from src.commands.contracts.job import JobRetainArgs


def _capture_kind(relative: str) -> str:
    """What a retained capture member is, by name.

    The adapter's own transcript and its per-view completion markers stay out of
    the store — ``aq job logs`` still serves them, and a receipt names evidence.
    """
    name = relative.rsplit("/", 1)[-1]
    if name.endswith(".png.channels.bin"):
        return "capture_channels"
    if name.endswith(".png"):
        return "capture_image"
    if name == "capture.json":
        return "capture_receipt"
    return "capture_member"


def _presented_frame(view: dict) -> str | None:
    """The frame the adapter actually presented, when the receipt names it."""
    presented = ((view.get("capture") or {}).get("result") or {}).get("presented") or {}
    return presented.get("id")


class JobCommandsMixin:
    def _job_scope(self):
        principal = current_principal()
        if principal and principal.kind == PrincipalKind.SESSION:
            return dict(
                kind="session",
                project_id=principal.project_id,
                task_id=principal.task_id,
                session_id=principal.session_id,
                session_instance_token=principal.session_instance_token,
                elevated=principal.elevated,
            )
        return self._current_scope or {}

    async def _job_scope_task_id(self, scope):
        """The task a scope acts for: its fixed task, else its session's live claim.

        Pool sessions carry no fixed task identity; their owner is whatever the
        authenticated session holds now. A mismatched session instance resolves
        to nothing, so a replaced harness cannot read its predecessor's jobs.
        """
        if scope.get("task_id"):
            return scope["task_id"]
        if scope.get("kind") != "session" or not scope.get("session_id"):
            return None
        session = await self.db.get_session(scope["session_id"])
        if session is None or not session.task_id:
            return None
        token = scope.get("session_instance_token")
        if token and session.instance_token != token:
            return None
        return session.task_id

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
                # All checks in one validation attempt share a detached clone.
                # An install preset must leave its ignored dependencies for the
                # following test/build preset, while each attempt remains isolated.
                group = args.get("snapshot_group")
                snapshot_digest = (
                    hashlib.sha256(json.dumps({
                        "project_id": args["project_id"],
                        "operation_id": args["operation_id"],
                        "input_ref": args["input_ref"],
                        "snapshot_group": group,
                    }, sort_keys=True).encode()).hexdigest()
                    if group else digest
                )
                snapshot_id = "job-snapshot-" + snapshot_digest
                snapshot = Path(self.config.data_dir) / "job-snapshots" / snapshot_digest
                if group:
                    await conn.execute(select(func.pg_advisory_xact_lock(
                        109797, func.hashtext(snapshot_id)
                    )))
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
            task_id = await self._job_scope_task_id(scope)
            if job["owner_kind"] != "task" or not task_id or task_id != job["task_id"]:
                return None
        if job["owner_kind"] == "task" and not await self.db.get_task(job["task_id"]):
            return None
        return job

    async def _cmd_job_submit(self, args):
        scope = self._job_scope()
        try:
            from src.commands.contracts.job import JobSubmitArgs

            args = JobSubmitArgs.model_validate(args).model_dump(exclude_none=True)
            scope_task_id = await self._job_scope_task_id(scope)
            if scope.get("session_id") and not scope_task_id and not scope.get("elevated"):
                raise JobError("jobs.out_of_scope")
            for key, value in (
                ("project_id", scope.get("project_id")),
                ("task_id", scope_task_id),
                ("session_id", scope.get("session_id")),
            ):
                if value and args.get(key, value) != value:
                    raise JobError("jobs.out_of_scope")
            identity = await self._wait_identity(args, mutation=True) if args.get("wait") else None
            project_id = scope.get("project_id") or args["project_id"]
            task_id = scope_task_id or args["task_id"]
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
                attempt_id=args.get("attempt_id"),
            )
            wait = result.pop("wait", None)
            response = {"success": True, "job": result}
            if wait:
                from src.agent_waits import wait_next_step

                response.update(
                    wait=wait,
                    next_step=wait_next_step(wait),
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
        task_id = await self._job_scope_task_id(scope) or args.get("task_id")
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

        try:
            response = await asyncio.to_thread(
                read_output, Path(self.config.data_dir), job,
                args.get("after", 0), args.get("limit", 65536),
            )
            return {"success": True, **response}
        except FileNotFoundError:
            return {"success": False, "error": "logs_not_ready"}
        except (OSError, ValueError):
            return {"success": False, "error": "output_unavailable"}

    async def _cmd_job_reconcile(self, args):
        await self._jobs().tick()
        return {"success": True}

    async def _cmd_job_retain(self, args):
        """Mint durable artifact identities for a completed capture.

        A ``matter_render`` job keeps its capture under ``runs/<job>/capture``
        until the sweeper reclaims it, while an object-loop ``ScoreReceipt``
        names durable URIs and refuses local paths.  This is the step between
        them: each retained member is copied into the artifact store, named by
        its own digest, and refused if those bytes no longer hash to the digest
        the completion receipt recorded.  What it copies is bounded by what the
        job already admitted under its own artifact budget, so retention cannot
        amplify what a run wrote.

        What it does not invent: ``Capture.ready``/``decoded`` and the per-view
        metrics are the external scorer's to measure.  The captures reported
        here carry the image pointer and geometry, so a scorer can assemble a
        receipt from them without AQ ever asserting a measurement.
        """
        try:
            request = JobRetainArgs.model_validate(args)
        except ValidationError as exc:
            return {"success": False, "error": str(exc)}
        job = await self._job_for_scope(request.job_id)
        if not job:
            return {"success": False, "error": "not_found"}
        if job["preset"] != "matter_render":
            return {"success": False, "error": "jobs.capture_unavailable"}
        capture = (job["result"] or {}).get("capture") or {}
        if (capture.get("receipt") or {}).get("status") != "complete":
            return {"success": False, "error": "jobs.capture_unavailable"}
        try:
            response = await asyncio.to_thread(self._retain_capture, job, capture, request)
        except (ArtifactError, OSError, ValueError, KeyError, TypeError) as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, **response}

    def _retain_capture(self, job, capture, request):
        data_dir = Path(self.config.data_dir)
        origin = {"job_id": job["id"], "preset": job["preset"]}
        receipt = capture.get("receipt") or {}
        views = receipt.get("views") or {}
        wanted = list(request.views) if request.views else sorted(views)
        unknown = sorted(set(wanted) - set(views))
        if unknown:
            raise ValueError(f"unknown capture views: {', '.join(unknown)}")

        recorded = {str(member["path"]): member for member in capture.get("artifacts") or []}
        members = ["capture/capture.json"]
        for name in wanted:
            members.append(f"capture/{name}.png")
            if request.include_channels:
                members.append(f"capture/{name}.png.channels.bin")
        artifacts = {}
        for relative in members:
            member = recorded.get(relative)
            if member is None:
                raise ArtifactError(f"{relative} was not retained by this job")
            found = retain_file(
                data_dir / "runs" / job["id"] / relative,
                data_dir=data_dir,
                kind=_capture_kind(relative),
                origin={**origin, "member": relative},
            )
            if (found["sha256"], found["bytes"]) != (member["sha256"], member["bytes"]):
                raise ArtifactError(f"{relative} changed since the job retained it")
            artifacts[relative] = found

        captures = [
            {
                "view_id": name,
                "frame_id": _presented_frame(views[name]),
                "image": {"uri": artifacts[f"capture/{name}.png"]["uri"],
                          "sha256": artifacts[f"capture/{name}.png"]["sha256"]},
                "width": (views[name].get("image") or {}).get("width"),
                "height": (views[name].get("image") or {}).get("height"),
            }
            for name in wanted
        ]
        profile = (job["result"] or {}).get("render_profile") or {}
        candidate = self._retain_candidate(job, receipt, origin)
        if candidate.get("sha256"):
            # Reported on its own and inside ``artifacts``, because that list is
            # what becomes ScoreReceipt.artifacts and a receipt is refused
            # unless its candidate digest appears there.
            artifacts["candidate.json"] = candidate
        return {
            "job_id": job["id"],
            "candidate_sha256": receipt.get("candidate_sha256"),
            "rig_sha256": receipt.get("rig_sha256"),
            "candidate_artifact": candidate,
            "render_profile": profile or None,
            "render_profile_sha256": profile.get("sha256"),
            "editor_pin": (job.get("contract") or {}).get("editor_pin"),
            "artifacts": sorted(artifacts.values(), key=lambda a: a["uri"]),
            "captures": captures,
            "next_step": (
                "Quote render_profile_sha256 as ObjectLoopStartArgs.render_profile_sha256, "
                "the candidate artifact as ScoreReceipt.artifacts, and each capture image as "
                "Capture.image; the scorer supplies ready/decoded and the view metrics. "
                "Submit every later capture of this attempt with the same --attempt-id, so "
                "every capture of the attempt renders the same pinned editor build."
            ),
        }

    def _retain_candidate(self, job, receipt, origin):
        """The candidate artifact: an identity whose digest *is* ``candidate_sha256``.

        ``ScoreReceipt`` refuses a receipt whose candidate digest is absent from
        its artifacts, and the declared identity is the digest of the canonical
        candidate manifest, so minting that canonical form is what closes the
        loop.  The bundle lives in the author's workspace, so a released
        workspace reports why the artifact is missing instead of substituting a
        near-miss identity.
        """
        argv = job.get("argv") or []
        if not argv:
            return {"error": "the job records no candidate bundle path"}
        try:
            document, declared = candidate_document(Path(argv[-1]) / "candidate.json")
            return retain_document(
                document, data_dir=Path(self.config.data_dir), kind="candidate",
                origin={**origin, "member": "candidate.json"}, expect_sha256=declared,
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return {"error": str(exc)}
