"""Daemon-side queue/recovery substrate, called only by CommandHandler.

Disabling admission never disables receipt recovery or cancellation. No job is
replayed from `starting`: a missing launch receipt is lost and its pin remains
until execution can be proved dead. Wait/publisher adapters are separate.
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path
from src.jobs.artifacts import OutputStore, atomic_json, job_directory, read_json
from src.jobs.identity import processes, verified
from src.jobs.policy import JobError, TERMINAL, presets, next_admission, request_hash, validate_args
from src.jobs.result import build_result
from src.resources.limits import session_env_caps
from src.sessions.env import SCRATCH_DB_SENTINEL


class JobService:
    def __init__(self, db, config):
        self.db, self.config = db, config
        self.root = Path(__file__).resolve().parents[2]
        self._tick_lock = asyncio.Lock()
        self._children = set()

    @property
    def settings(self):
        return self.config.resources.jobs

    async def submit(
        self,
        *,
        project_id,
        task_id,
        session_id,
        claim_epoch,
        workspace_id,
        generation,
        preset,
        args,
        idempotency_key,
        owner_kind="task",
        owner_id=None,
        input_mode="live",
        input_ref=None,
        trusted_band=2,
        wait_identity=None,
        queue_seconds=None,
        run_seconds=None,
        adapter_request_hash=None,
    ):
        cfg = self.settings
        if not cfg.enabled:
            raise JobError("jobs.disabled")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise JobError("jobs.idempotency_key_invalid")
        accepted = presets(self.root).get(preset)
        if not accepted or owner_kind not in {"task", "integration"}:
            raise JobError("jobs.preset_denied")
        if input_mode not in {"live", "snapshot"}:
            raise JobError("jobs.cwd_invalid")
        ws = await self.db.get_workspace(workspace_id)
        if not ws or ws.project_id != project_id:
            raise JobError("jobs.cwd_invalid")
        cwd = Path(ws.workspace_path).resolve(strict=True)
        argv = validate_args(accepted, args, cwd, self.config.resources.test_worker_cap())
        job_class = accepted.job_class
        if accepted.pytest:
            from src.cli.test_runner import _is_full_suite

            if await asyncio.to_thread(_is_full_suite, tuple(args), cwd=cwd):
                job_class = "exclusive"
        if accepted.weight > self.config.resources.test_slots:
            raise JobError("jobs.weight_invalid")
        if accepted.pytest and not any(
            a in {"-m", "--markexpr"} or a.startswith("--markexpr=") for a in args
        ):
            argv += ["-m", self.config.resources.test_deselect_markers]
        # Integration inputs must name a verified detached commit snapshot.
        if input_mode == "snapshot":
            from src.git.manager import GitManager

            git = GitManager()
            head = (await git._arun(["rev-parse", "HEAD"], cwd=str(cwd))).strip()
            dirty = await git._arun(["status", "--porcelain"], cwd=str(cwd))
            if not input_ref or head != input_ref or dirty:
                raise JobError("jobs.snapshot_invalid")
        canonical = {
            "project_id": project_id,
            "owner_kind": owner_kind,
            "owner_id": owner_id or task_id,
            "workspace_id": workspace_id,
            "generation": generation,
            "preset": preset,
            "args": args,
            "input_mode": input_mode,
            "input_ref": input_ref,
            "wait": wait_identity is not None,
            "queue_seconds": queue_seconds,
            "run_seconds": run_seconds,
            "adapter_request_hash": adapter_request_hash,
        }
        job_id, nonce, now = str(uuid.uuid4()), uuid.uuid4().hex, time.time()
        env = {
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": str(Path.home()),
            "AQ_JOB_ID": job_id,
            "AQ_JOB_NONCE": nonce,
            "AQ_TEST_RUN_ID": f"job-{job_id}",
            "AQ_TASK_ID": task_id or "",
            "AQ_PROJECT_ID": project_id,
            "AQ_DB_SCOPE": "worker",
            "AQ_DATABASE_URL": SCRATCH_DB_SENTINEL,
            "AGENT_QUEUE_DB": SCRATCH_DB_SENTINEL,
            **session_env_caps(self.config),
        }
        if accepted.pytest or preset == "e2e":
            from src.database.migration_guard import same_database

            if not cfg.test_database_url or same_database(
                cfg.test_database_url, self.config.database.url
            ):
                raise JobError("jobs.test_database_unconfigured")
            env["POSTGRES_TEST_DSN"] = cfg.test_database_url
        if preset == "e2e":
            from urllib.parse import urlsplit, unquote

            parsed = urlsplit(cfg.test_database_url)
            env.update(
                E2E_PG_HOST=parsed.hostname or "localhost",
                E2E_PG_PORT=str(parsed.port or 5432),
                E2E_PG_USER=unquote(parsed.username or ""),
                E2E_PG_PASSWORD=unquote(parsed.password or ""),
                E2E_DB_NAME=f"agent_queue_e2e_job_{job_id.replace('-', '')}",
                AQ_E2E_HOME=str(Path(self.config.data_dir) / "runs" / job_id / "e2e"),
                AQ_E2E_PORT="8199",
                AQ_E2E_TMUX_SOCKET=f"aq-e2e-job-{job_id}",
            )
        contract = {
            "cwd": str(cwd),
            "env": env,
            "capacity": self.config.resources.test_slots,
            "lock_dir": str(Path(self.config.data_dir) / "locks/test-slots"),
            "pytest": accepted.pytest,
            "nice": self.config.resources.session_nice,
            "head_bytes": cfg.head_bytes,
            "tail_bytes": cfg.tail_bytes,
            "adapter_request_hash": adapter_request_hash,
            "tool_versions": {"python": sys.version, "executable": sys.executable},
        }
        if (queue_seconds is not None or run_seconds is not None) and owner_kind != "integration":
            raise JobError("jobs.preset_denied")
        default_queue = (
            cfg.exclusive_queue_seconds if job_class == "exclusive" else cfg.shared_queue_seconds
        )
        queue_budget = min(default_queue, queue_seconds) if queue_seconds is not None else default_queue
        run_budget = min(cfg.run_seconds, run_seconds) if run_seconds is not None else cfg.run_seconds
        if queue_budget < 0 or run_budget <= 0:
            raise JobError("jobs.deadline_invalid")
        values = {
            "id": job_id,
            "project_id": project_id,
            "task_id": task_id,
            "owner_kind": owner_kind,
            "owner_id": owner_id or task_id,
            "submitter_session_id": session_id,
            "claim_epoch": claim_epoch,
            "integration_operation_id": owner_id if owner_kind == "integration" else None,
            "idempotency_key": idempotency_key,
            "request_hash": request_hash(canonical),
            "preset": preset,
            "preset_version": accepted.version,
            "argv": argv,
            "contract": contract,
            "workspace_id": workspace_id,
            "workspace_generation": generation,
            "input_mode": input_mode,
            "input_ref": input_ref,
            "job_class": job_class,
            "weight": accepted.weight,
            "priority_band": trusted_band,
            "submitted_at": now,
            "queue_deadline": now + queue_budget,
            "run_timeout": run_budget,
            "runner_nonce": nonce,
        }
        await self.sweep(reserve=True)
        return await self.db.submit_job(
            values,
            max_queued=cfg.max_queued,
            per_task_queued=cfg.per_task_queued,
            log_budget=cfg.log_budget_bytes,
            reservation=cfg.head_bytes + cfg.tail_bytes,
            wait_identity=wait_identity,
        )

    async def cancel(self, job: dict):
        if job["state"] in TERMINAL:
            return job
        if job["state"] == "queued":
            result = build_result(job, {"cancelled": True})
            return await self.db.transition_job(
                job["id"],
                job["state_version"],
                "cancelled",
                cleaned=True,
                result=result,
                result_version=1,
                result_ref=job["id"],
                output_retention="expired",
            )
        directory = await asyncio.to_thread(job_directory, Path(self.config.data_dir), job["id"])
        await asyncio.to_thread(
            atomic_json, directory / "cancel.json", {"requested_at": time.time()}
        )
        if job["state"] != "cancelling":
            return await self.db.transition_job(job["id"], job["state_version"], "cancelling")
        return job

    async def tick(self):
        if self._tick_lock.locked():
            return
        async with self._tick_lock:
            rows = await self.db.reconcilable_jobs()
            for job in rows:
                owner_finished = False
                if job["owner_kind"] == "task":
                    task = await self.db.get_task(job["task_id"])
                    owner_finished = not task or task.status.value in {
                        "COMPLETED",
                        "FAILED",
                        "BLOCKED",
                    }
                    if owner_finished:
                        await self.cancel(job)
                        job = await self.db.get_job(job["id"])
                if job["state"] != "queued":
                    await self.reconcile(job)
                if owner_finished:
                    # Release skipped by task close is retried only after the
                    # non-expiring job pin has been proved safe to remove.
                    await self.db.release_workspaces_for_task(job["task_id"])
            if self.settings.enabled:
                rows = await self.db.list_jobs(active=True, limit=1000)
                for job in rows:
                    if job["state"] == "queued" and job["queue_deadline"] <= time.time():
                        result = build_result(job, {"infra_reason": "queue_timeout"})
                        await self.db.transition_job(
                            job["id"],
                            job["state_version"],
                            "failed",
                            cleaned=True,
                            result=result,
                            result_version=1,
                            result_ref=job["id"],
                            infra_reason="queue_timeout",
                            output_retention="expired",
                        )
                rows = await self.db.list_jobs(active=True, limit=1000)
                candidate = next_admission(
                    rows,
                    time.time(),
                    self.config.resources.test_slots,
                    self.settings.per_task_active,
                )
                if candidate:
                    await self.launch(candidate)
            await self.sweep()

    async def launch(self, job):
        changed = await self.db.transition_job(
            job["id"], job["state_version"], "starting", launch_at=time.time()
        )
        if not changed:
            return
        directory = await asyncio.to_thread(job_directory, Path(self.config.data_dir), job["id"])
        await asyncio.to_thread(atomic_json, directory / "request.json", changed)
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "src.jobs.runner",
                str(directory),
                cwd=str(self.root),
                env=job["contract"]["env"],
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            reap = asyncio.create_task(proc.wait())
            self._children.add(reap)
            reap.add_done_callback(self._children.discard)
        except OSError:
            # No spawn retry, even if the caller cannot distinguish pre/post exec.
            await self.reconcile(changed)

    async def reconcile(self, job):
        nonce = job["runner_nonce"]
        try:
            directory = await asyncio.to_thread(
                job_directory, Path(self.config.data_dir), job["id"]
            )
            artifacts = []
            for name in ("completion", "started", "intent"):
                value = await asyncio.to_thread(read_json, directory / f"{name}.json")
                if value is not None and not isinstance(value, dict):
                    raise ValueError("invalid job receipt")
                artifacts.append(
                    value
                    if value and value.get("nonce") == nonce and value.get("job_id") == job["id"]
                    else None
                )
            receipt, started, intent = artifacts
            from src.jobs.identity import require_readable_identity

            for value in artifacts:
                if value:
                    await require_readable_identity(value)
            alive = await processes(nonce)
            # A just-created starting row may precede the detached exec. Bound
            # that gap without ever launching again on adoption.
            if (
                not receipt
                and not intent
                and time.time() - (job.get("launch_at") or job["submitted_at"]) < 30
            ):
                return
            if alive:
                if job["state"] == "starting" and started and await verified(started, nonce):
                    await self.db.transition_job(
                        job["id"],
                        job["state_version"],
                        "running",
                        started_at=started["started_at"],
                        run_deadline=started["started_at"] + job["run_timeout"],
                        boot_id=started["boot_id"],
                        pid=started["pid"],
                        start_ticks=started["start_ticks"],
                        input_ref=started.get("input_ref"),
                        input_fingerprint=started.get("input_fingerprint"),
                    )
                # A dead supervisor with surviving descendants cannot pump or
                # enforce a deadline. Cancel through fenced marker cleanup.
                elif not intent or not await verified(intent, nonce):
                    from src.jobs.identity import stop_tree

                    await stop_tree(nonce)
                return
            lock_dir = job["contract"].get("lock_dir")
            if lock_dir:
                from src.resources.test_runs import held_slots

                held = await asyncio.to_thread(held_slots, lock_dir)
                if any(slot.holder.get("job_nonce") == nonce for slot in held):
                    raise RuntimeError("jobs.cleanup_blocked")
            if job["state"] in TERMINAL:
                if receipt or intent:
                    await self.db.job_cleanup_verified(job["id"])
                return
            tail = b""
            if receipt:

                def read_tail():
                    if not (directory / "manifest.json").exists():
                        if receipt.get("output_bytes_seen", 0):
                            receipt["output_store_failed"] = True
                        return b""
                    store = OutputStore(
                        directory,
                        head_bytes=job["contract"]["head_bytes"],
                        tail_bytes=job["contract"]["tail_bytes"],
                        readonly=True,
                    )
                    try:
                        data = store.read(max(0, store.manifest["seen"] - 8192), 8192)
                        if store.failed:
                            receipt["output_store_failed"] = True
                        return b"".join(c["data"] for c in data["chunks"])
                    finally:
                        store.close()

                try:
                    tail = await asyncio.to_thread(read_tail)
                except (OSError, ValueError, KeyError, TypeError):
                    receipt["output_store_failed"] = True
            if job["state"] == "cancelling" and receipt:
                receipt["cancelled"] = True
            result = build_result(job, receipt, tail)
            await asyncio.to_thread(atomic_json, directory / "result.json", result)
            # starting may finish before a reconciliation cycle observes running.
            if job["state"] == "starting" and result["state"] in {"succeeded", "cancelled"}:
                job = await self.db.transition_job(
                    job["id"],
                    job["state_version"],
                    "cancelling" if result["state"] == "cancelled" else "running",
                )
                if job is None:
                    return
            await self.db.transition_job(
                job["id"],
                job["state_version"],
                result["state"],
                cleaned=bool(receipt or intent),
                cleanup_blocked=not bool(receipt or intent),
                result=result,
                result_ref=job["id"],
                result_version=1,
                exit_code=result["exit_code"],
                signal=result["signal"],
                infra_reason=result["infra_reason"],
                input_stability=result["input_stability"],
                output_retention="retained" if receipt else "expired",
            )
        except (OSError, RuntimeError, ValueError, TypeError, KeyError):
            await self.db.mark_job_cleanup_blocked(job["id"])

    async def sweep(self, *, reserve=False):
        """Bounded retention: terminal logs first, results independently at 90d."""
        cfg, now = self.settings, time.time()
        rows = await self.db.retained_terminal_jobs(
            limit=100, result_cutoff=now - cfg.result_days * 86400
        )
        reserved = await self.db.job_output_reservations()
        for job in rows:
            old = now - job["ended_at"]
            eviction = reserve and reserved + cfg.head_bytes + cfg.tail_bytes > cfg.log_budget_bytes
            if job["output_retention"] != "expired" and (old > cfg.log_days * 86400 or eviction):
                directory = await asyncio.to_thread(
                    job_directory, Path(self.config.data_dir), job["id"]
                )
                for name in ("output.head", "output.tail", "manifest.json", "junit.xml"):
                    await asyncio.to_thread((directory / name).unlink, missing_ok=True)
                await self.db.expire_job_output(job["id"])
                reserved -= job["output_reservation_bytes"]
            if old > cfg.result_days * 86400:
                import shutil

                snapshot_ws = None
                if job["owner_kind"] == "integration" and job["input_mode"] == "snapshot":
                    snapshot = Path(job["contract"]["cwd"])
                    root = Path(self.config.data_dir) / "job-snapshots"
                    ws = await self.db.get_workspace(job["workspace_id"])
                    # Only internally provisioned, disabled snapshots may be
                    # removed. Keep metadata on I/O failure so a sweep retries.
                    if (
                        ws and ws.kind_id == "job-snapshot" and not ws.enabled
                        and snapshot.parent == root
                        and job["workspace_id"] == "job-snapshot-" + snapshot.name
                    ):
                        from src.jobs.workspace import mutation_guard

                        async with mutation_guard(self.db, ws):
                            if await asyncio.to_thread(snapshot.exists):
                                await asyncio.to_thread(shutil.rmtree, snapshot)
                        snapshot_ws = ws
                if await self.db.purge_terminal_job(job["id"]):
                    if snapshot_ws:
                        await self.db.delete_workspace(snapshot_ws.id)
                    directory = await asyncio.to_thread(
                        job_directory, Path(self.config.data_dir), job["id"]
                    )
                    await asyncio.to_thread(shutil.rmtree, directory)
