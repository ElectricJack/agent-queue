"""Submit once through CommandHandler, then await durable job settlement."""
from __future__ import annotations

import base64
import uuid
from dataclasses import replace
from typing import ClassVar

from src.commands.principal import PrincipalKind, principal_context
from src.profiles.capabilities import CapabilityPolicy
from src.jobs.policy import TERMINAL
from src.playbooks.definition import SCRIPT_OUTCOMES, ScriptStep
from src.playbooks.executors.base import ExecutionMode, ExecutorResult, StepControl
from src.playbooks.expressions import ValueResolutionError, resolve_value
from src.playbooks.invocation import _invocation_context
from src.playbooks.private_values import PrivateValueError, REFERENCE_KEY
from src.playbooks.receipts import idempotency_key
from src.playbooks.waits import WaitSpec


class ScriptSubmissionInterrupted(RuntimeError):
    """Submission may have committed; retain its fence for an identical rejoin."""


def execution_context(ctx):
    """Trusted triggers derive authority from the artifact; others only narrow."""
    caller = ctx.principal
    if caller is None:
        return ctx
    policy = CapabilityPolicy.from_namespaces(aq_commands=["playbook_script_run", "job_cancel"])
    scope = ctx.artifact.scope
    if caller.kind in {PrincipalKind.LOCAL, PrincipalKind.SERVICE}:
        principal = replace(caller, policy=policy,
                            project_id=scope.project_id if scope.type == "project" else None,
                            profile_id=scope.agent_type if scope.type == "agent_type" else None)
    else:
        principal = caller.narrow(policy, reason="script-step")
    return replace(ctx, principal=replace(principal, kind=PrincipalKind.PLAYBOOK,
                                         parent_run_id=ctx.run_id, parent_step_id=ctx.step_id))


def script_projection(step, pinned, *, preview=False):
    """Only authored program identity and policy, never resolved input values."""
    return {
        "source": step.program.script,
        "source_kind": step.program.source_kind,
        "cwd": step.program.cwd,
        "program": step.program.model_dump(mode="json", exclude=set() if preview else {"stdin", "arguments", "argv"}),
        "executable": step.program.executable,
        "environment_names": sorted(step.environment),
        "limits": step.program.limits.model_dump(),
        "program_sha256": pinned.program_sha256 if pinned else None,
        "runtime_sha256": pinned.runtime_sha256 if pinned else None,
        "executable_sha256": pinned.executable_sha256 if pinned else None,
        "source_revision": pinned.source_revision if pinned else None,
        "inputs": [item.model_dump() for item in pinned.inputs] if pinned else [],
    }


def pin_for(ctx):
    metadata = ctx.artifact.trusted_execution
    return metadata.scripts.get(ctx.step_id) if metadata else None


async def dispatch(ctx, command, arguments):
    """Retain both the original principal and server-owned invocation."""
    ctx = execution_context(ctx)
    with principal_context(ctx.principal), _invocation_context(ctx):
        return await ctx.services.handler.execute(command, arguments)


class LiveScriptExecutor:
    step_type: ClassVar[str] = "script"
    mode: ClassVar[ExecutionMode] = ExecutionMode.LIVE
    no_side_effects: ClassVar[bool] = False

    async def execute(self, step: ScriptStep, ctx):
        ctx = execution_context(ctx)
        pinned = pin_for(ctx)
        projection = script_projection(step, pinned)
        key = idempotency_key(ctx.run_id, ctx.step_id,
                              -1 if ctx.iteration_index is None else ctx.iteration_index,
                              ctx.attempt, ctx.iteration_path)

        def failure(outcome, reason):
            return ExecutorResult(StepControl.ADVANCE, outcome, operation="script",
                                  diagnostics=(reason,), receipt_inputs=projection,
                                  idempotency_key=key, effective_principal=ctx.principal)

        if pinned is None or pinned.program != step.program:
            return failure("contract_violation", "script_pin_invalid")
        if ctx.principal is None or not ctx.principal.policy.allows_aq_command("playbook_script_run"):
            return failure("unauthorized", "script_capability_denied")
        try:
            nodes = step.program.argv[1:] if step.program.argv else step.program.arguments
            arguments = [resolve_value(node, ctx.scope, ctx.evaluation_budget) for node in nodes]
            environment = {name: resolve_value(node, ctx.scope, ctx.evaluation_budget)
                           for name, node in step.environment.items()}
            stdin = (resolve_value(step.program.stdin, ctx.scope, ctx.evaluation_budget)
                     if step.program.stdin is not None else "")
            if (not all(isinstance(arg, str) for arg in arguments)
                    or not all(isinstance(value, str) for value in environment.values())
                    or not isinstance(stdin, str)):
                return failure("input_resolution_failed", "script_inputs_must_be_strings")
            if ctx.services.private_values is None:
                raise PrivateValueError("private_storage_unavailable")
            await ctx.services.private_values.check_ready()
            submission = {
                "arguments": arguments, "environment": environment,
                "stdin_base64": base64.b64encode(stdin.encode()).decode(),
            }
        except ValueResolutionError:
            return failure("input_resolution_failed", "script_input_resolution_failed")
        except PrivateValueError as exc:
            return failure("unavailable", exc.code)
        try:
            response = await dispatch(ctx, "playbook_script_run", submission)
        except Exception as exc:
            raise ScriptSubmissionInterrupted("script_submission_interrupted") from exc
        if not isinstance(response, dict) or type(response.get("success")) is not bool:
            raise ScriptSubmissionInterrupted("script_submission_protocol")
        if not response["success"]:
            code = response.get("error_code")
            if code in {None, "jobs.storage_unavailable", "command_runtime_error"}:
                raise ScriptSubmissionInterrupted(code or "script_submission_unknown_error")
            return failure("unauthorized" if code == "unauthorized" else "admission_denied", code)
        job = response.get("job")
        if not isinstance(job, dict) or not job.get("id"):
            raise ScriptSubmissionInterrupted("script_job_missing")
        if ctx.cancel_event is not None and ctx.cancel_event.is_set():
            await dispatch(ctx, "job_cancel", {"job_id": job["id"]})
        return ExecutorResult(
            StepControl.SUSPEND, "awaiting_job", operation="script", idempotency_key=key,
            receipt_inputs=projection, receipt_result={"job_id": job["id"]},
            effective_principal=ctx.principal,
            wait=WaitSpec(wait_id=uuid.uuid4().hex, run_id=ctx.run_id, step_id=ctx.step_id,
                          iteration=-1 if ctx.iteration_index is None else ctx.iteration_index,
                          iteration_path=ctx.iteration_path, kind="event",
                          event_type="job.completed", match={"job_id": job["id"]},
                          created_at=ctx.services.clock()),
        )


async def settled_result(step, ctx, job_id):
    """Read durable truth, even when a completion notification is duplicated/spoofed."""
    ctx = execution_context(ctx)
    job = await ctx.services.db.get_job(job_id)
    if (job is None or job["owner_kind"] != "playbook"
            or job["playbook_run_id"] != ctx.run_id or job["playbook_step_id"] != ctx.step_id
            or job["iteration_path"] != ctx.iteration_path
            or job["attempt"] != ctx.attempt
            or job["artifact_digest"] != ctx.artifact_ref.artifact_sha256):
        raise ValueError("script_job_identity_invalid")
    if job["cleanup_blocked"] or job["state"] not in TERMINAL:
        return None
    result = job.get("result")
    if not result or not result.get("cleanup_complete"):
        raise ValueError("script_settlement_incomplete")
    value = None
    if step.save_result_as:
        from src.jobs.playbook import owner

        with _invocation_context(ctx):
            value = await ctx.services.private_values.read(
                result["binding_ref"], owner=owner(job), principal=ctx.principal)
        # A binding carries opaque stream handles, not recursively resolvable
        # private-value envelopes (which would turn binary streams into JSON).
        value = {**value, "output_refs": {
            name: reference[REFERENCE_KEY]
            for name, reference in value.get("output_refs", {}).items()
        }}
    return ExecutorResult(
        StepControl.ADVANCE, result["outcome"], value=value,
        private_fields=tuple(value) if value else None, operation="script",
        receipt_inputs=script_projection(step, pin_for(ctx)),
        receipt_result={"job_id": job_id, **result}, effective_principal=ctx.principal,
    )


class SymbolicScriptExecutor:
    step_type: ClassVar[str] = "script"
    mode: ClassVar[ExecutionMode] = ExecutionMode.DRY_RUN
    no_side_effects: ClassVar[bool] = True

    async def execute(self, step, ctx):
        return ExecutorResult(
            StepControl.UNRESOLVED, "unavailable", operation="script",
            receipt_inputs=script_projection(step, pin_for(ctx), preview=True),
            diagnostics=("Script output is unresolved; no job was submitted",),
            possible_outcomes=tuple(sorted(set(step.transitions) & SCRIPT_OUTCOMES)),
        )
