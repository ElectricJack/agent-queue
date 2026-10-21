"""Playbook adapter for the existing durable jobs queue and detached runner.

Only encrypted inputs and outputs reach disk/SQL. The detached runner receives
its input and active encryption key over stdin, which is closed before launch.
Ambiguity keeps the job and input pin alive and pauses its actual owning run.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from src.jobs.artifacts import atomic_json, job_directory, read_json
from src.jobs.identity import processes, require_readable_identity, verified
from src.jobs.policy import JobError, TERMINAL
from src.jobs.script_sandbox import BINDING_BYTES, REQUEST_BYTES, SandboxRequest
from src.playbooks.private_values import PrivateOwner, PrivateValueService
from src.playbooks.script_inputs import canonical, digest


@dataclass(frozen=True)
class ScriptSubmission:
    run_id: str
    step_id: str
    iteration_path: str
    attempt: int
    artifact_digest: str
    scope_kind: str
    scope_owner: str
    request: SandboxRequest = field(repr=False)

    @property
    def owner(self):
        return PrivateOwner("run", self.run_id, self.scope_kind, self.scope_owner)

    @property
    def operation_key(self):
        return digest(canonical([self.run_id, self.step_id, self.iteration_path, self.attempt]))


def private_service(service):
    return getattr(service.db, "_playbook_private_values", None) or PrivateValueService.from_config(
        service.db._engine, service.config.playbooks)


def owner(job):
    return PrivateOwner(**job["contract"]["private_owner"])


def public_job(job):
    """The launch contract and protected input handle are not inspection output."""
    return {key: value for key, value in job.items() if key not in {"contract", "argv"}}


async def submit(service, script):
    if not isinstance(script, ScriptSubmission) or script.request.run_id != script.run_id:
        raise JobError("jobs.script_identity_invalid")
    cfg, settings = service.settings, service.config.resources.scripts
    if not cfg.enabled or not settings.enabled:
        raise JobError("jobs.disabled")
    private = private_service(service)
    await private.check_ready()
    request = script.request.model_dump(mode="json")
    if len(canonical(request)) > REQUEST_BYTES:
        raise JobError("jobs.script_input_too_large")
    # A fresh execution id is not part of the operation's content identity.
    content = {k: v for k, v in request.items() if k != "execution_id"}
    request_digest = digest(canonical({"artifact": script.artifact_digest, "input": content}))
    batch = private.batch(script.owner)
    reference = batch.protect(request)
    now, job_id, nonce = time.time(), str(uuid.uuid4()), uuid.uuid4().hex
    values = dict(
        id=job_id, project_id=script.scope_owner if script.scope_kind == "project" else None,
        task_id=None, owner_kind="playbook", owner_id=script.run_id,
        playbook_run_id=script.run_id, playbook_step_id=script.step_id,
        iteration_path=script.iteration_path, attempt=script.attempt,
        artifact_digest=script.artifact_digest, input_root=settings.input_root,
        idempotency_key=script.operation_key, request_hash=request_digest,
        preset="playbook_script", preset_version=1, argv=[],
        workspace_id=None, workspace_generation=None, input_mode="snapshot",
        input_ref=digest(canonical(script.request.pinned.model_dump(mode="json"))),
        job_class="shared", weight=1, priority_band=2, submitted_at=now,
        queue_deadline=now + settings.queue_seconds,
        run_timeout=script.request.pinned.program.limits.wall_seconds, runner_nonce=nonce,
        contract={"private_owner": asdict(script.owner), "request_ref": reference,
                  "execution_id": request["execution_id"],
                  "script_settings": asdict(settings),
                  "capacity": service.config.resources.test_slots,
                  "lock_dir": str(Path(service.config.data_dir) / "locks/test-slots"),
                  "env": {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
                          "LANG": "C.UTF-8", "AQ_JOB_ID": job_id, "AQ_JOB_NONCE": nonce},
                  "nice": service.config.resources.session_nice},
    )
    return await service.db.submit_job(
        values, max_queued=cfg.max_queued, per_task_queued=cfg.per_task_queued,
        log_budget=cfg.log_budget_bytes,
        reservation=script.request.pinned.program.limits.stdout_bytes
        + script.request.pinned.program.limits.stderr_bytes + BINDING_BYTES,
        private_rows=batch.rows,
    )


async def launch_payload(service, job):
    from src.commands.principal import ExecutionPrincipal

    private = private_service(service)
    key_id, key = private.require_ready()
    request = await private.read(job["contract"]["request_ref"], owner=owner(job),
                                 principal=ExecutionPrincipal.service("script-launch"))
    return canonical({"request": request, "key_id": key_id,
                      "key": base64.b64encode(key).decode()})


def encode_rows(rows):
    return [{k: base64.b64encode(v).decode() if isinstance(v, bytes) else v
             for k, v in row.items()} for row in rows]


def decode_rows(rows):
    return [{k: base64.b64decode(v, validate=True) if k in {"nonce", "ciphertext"} else v
             for k, v in row.items()} for row in rows]


async def run(directory, job, intent):
    """Called under runner.py's instance lock, before generic box admission."""
    from src.config import ScriptsConfig
    from src.jobs.script_sandbox import launch

    # Pipe payload is bounded, never inherited by the script or written to disk.
    payload = await asyncio.to_thread(sys.stdin.buffer.read, 2 * 1024**2)
    data = json.loads(payload)
    private = PrivateValueService(object(), active_key_id=data["key_id"],
                                  keys={data["key_id"]: base64.b64decode(data["key"])})
    request = SandboxRequest.model_validate(data["request"])
    if (request.run_id != job["playbook_run_id"]
            or request.execution_id != job["contract"]["execution_id"]):
        raise ValueError("script_runner_identity_invalid")
    settings = ScriptsConfig(**job["contract"]["script_settings"])
    if job["queue_deadline"] <= time.time():
        from src.jobs.script_sandbox import SandboxResult

        expired = SandboxResult(request.execution_id, "admission_timed_out", reason="queue",
                                cleanup_complete=True, ended_at=time.time())
    else:
        expired = None
    settings.queue_seconds = max(1, min(settings.queue_seconds,
                                       int(job["queue_deadline"] - time.time())))
    cancelled = asyncio.Event()

    async def cancellation():
        while True:
            if (directory / "cancel.json").exists():
                cancelled.set()
                return
            await asyncio.sleep(0.05)

    monitor = asyncio.create_task(cancellation())
    try:
        await asyncio.to_thread(atomic_json, directory / "started.json",
                               {**intent, "started_at": time.time()})
        result = expired or await launch(settings, request, jobs_enabled=True, cancelled=cancelled,
                              lock_dir=job["contract"]["lock_dir"],
                              capacity=job["contract"]["capacity"])
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
    public, rows = protect_result(private, job, result)
    await asyncio.to_thread(atomic_json, directory / "completion.json",
                           {**intent, "script_result": public, "private_rows": encode_rows(rows),
                            "quota_key": result.quota_key})


def protect_result(private, job, result):
    batch = private.batch(owner(job))
    streams = {name: batch.protect(getattr(result, name), content_type="application/octet-stream")
               for name in ("stdout", "stderr")}
    value = {**result.binding_value(), "output_refs": streams, "terminal_cause": result.outcome}
    if len(canonical(value)) > BINDING_BYTES:
        value.update(stdout=None, stderr=None, inline_omitted=True)
    if len(canonical(value)) > BINDING_BYTES:
        raise ValueError("script_binding_too_large")
    binding = batch.protect(value)
    public = {k: v for k, v in value.items() if k not in {"stdout", "stderr", "stdout_json"}}
    public["binding_ref"] = binding
    return public, batch.rows


async def settle_unlaunched(service, job, outcome):
    from src.jobs.script_sandbox import SandboxResult

    result, rows = protect_result(private_service(service), job, SandboxResult(
        job["contract"]["execution_id"], outcome, reason="queue", cleanup_complete=True,
        ended_at=time.time()))
    return await service.db.transition_job(
        job["id"], job["state_version"], "cancelled" if outcome == "cancelled" else "failed",
        cleaned=True, private_rows=rows, result=result, result_ref=job["id"], result_version=1,
        output_retention="retained")



async def reconcile(service, job):
    if job["state"] in TERMINAL:
        return
    try:
        directory = await asyncio.to_thread(job_directory, Path(service.config.data_dir), job["id"])

        async def artifact(name):
            value = await asyncio.to_thread(read_json, directory / f"{name}.json")
            if value is None:
                return None
            if (not isinstance(value, dict) or value.get("nonce") != job["runner_nonce"]
                    or value.get("job_id") != job["id"]):
                raise ValueError("script_receipt_identity_invalid")
            await require_readable_identity(value)
            return value

        receipt, intent, started = await artifact("completion"), await artifact("intent"), await artifact("started")
        alive = await processes(job["runner_nonce"])
        if alive:
            if not intent or not await verified(intent, job["runner_nonce"]):
                await service.db.pause_script_job(job["id"], "script_runner_ownership_uncertain")
            elif started and job["state"] == "starting":
                await service.db.transition_job(job["id"], job["state_version"], "running",
                                                started_at=started["started_at"])
            return
        receipt = receipt or await artifact("completion")
        if receipt is None:
            if not intent and time.time() - (job.get("launch_at") or job["submitted_at"]) < 30:
                return
            await service.db.pause_script_job(job["id"], "script_execution_uncertain")
            return
        result = receipt["script_result"]
        if not result["cleanup_complete"]:
            await service.db.pause_script_job(job["id"], "script_cleanup_uncertain")
            return
        rows = decode_rows(receipt["private_rows"])
        private = private_service(service)
        decoded = {row["value_id"]: private.decode_row(row, owner=owner(job)) for row in rows}
        from src.playbooks.private_values import REFERENCE_KEY

        if len(rows) != 3 or len(decoded) != 3:
            raise ValueError("script_output_receipt_invalid")
        binding = decoded[result["binding_ref"][REFERENCE_KEY]]
        if (binding["execution_id"] != job["contract"]["execution_id"]
                or {k: v for k, v in binding.items()
                    if k not in {"stdout", "stderr", "stdout_json"}}
                != {k: v for k, v in result.items() if k != "binding_ref"}):
            raise ValueError("script_output_receipt_invalid")
        for name in ("stdout", "stderr"):
            raw = decoded[result["output_refs"][name][REFERENCE_KEY]]
            if not isinstance(raw, bytes):
                raise ValueError("script_output_receipt_invalid")
        state = {"succeeded": "succeeded", "cancelled": "cancelled"}.get(result["outcome"], "failed")
        if job["state"] == "starting" and state in {"succeeded", "cancelled"}:
            job = await service.db.transition_job(job["id"], job["state_version"], "running")
            if job is None:
                return
        await service.db.transition_job(
            job["id"], job["state_version"], state, cleaned=True, private_rows=rows,
            result=result, result_version=1, result_ref=job["id"],
            exit_code=result["exit_code"], signal=result["signal"],
            output_retention="retained", cleanup_blocked=False,
        )
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        await service.db.pause_script_job(job["id"], "script_recovery_uncertain")


async def authorize_job(service, job, principal):
    """Run ownership, independently of transport scope and handle knowledge."""
    await private_service(service).authorize(owner(job), principal)


async def read_stream(service, job, principal, *, stream="stdout", after=0, limit=65536):
    from src.playbooks.private_values import PrivateValueError

    if stream not in {"stdout", "stderr"} or type(after) is not int or after < 0:
        raise PrivateValueError("script_output_range_invalid")
    limit = max(1, min(65536, limit))
    if job["output_retention"] == "expired":
        raise PrivateValueError("script_output_expired")
    result = job.get("result")
    if not result:
        raise PrivateValueError("script_output_not_ready")
    raw = await private_service(service).read(result["output_refs"][stream], owner=owner(job),
                                              principal=principal)
    data = raw[after:after + limit]
    seen = result[f"{stream}_bytes"]
    return {"chunks": [{"offset": after, "data": base64.b64encode(data).decode(),
                        "encoding": "base64"}] if data else [],
            "gaps": [{"start": len(raw), "end": seen}] if seen > len(raw) else [],
            "next": min(len(raw), after + len(data)), "seen": seen}


async def retire(service, job):
    """Called only after terminal cleanup, at result-retention expiry."""
    from src.playbooks.script_inputs import QuotaLedger

    settings = job["contract"]["script_settings"]
    ledger = QuotaLedger(Path(settings["state_root"]) / "quotas",
                         input_bytes=settings["input_storage_bytes"],
                         output_bytes=settings["output_storage_bytes"],
                         per_run_bytes=settings["per_run_storage_bytes"])
    # Derive reservation ownership from SQL identity, never mutable receipt data.
    quota_key = digest(canonical({"execution_id": job["contract"]["execution_id"],
                                  "run_id": job["playbook_run_id"]}))
    await asyncio.to_thread(ledger.release, quota_key)
    # Keep launch intents until retirement; absence is never permission to replay.
    await asyncio.to_thread(
        (Path(settings["state_root"]) / f"intent-{job['contract']['execution_id']}").unlink,
        missing_ok=True,
    )
