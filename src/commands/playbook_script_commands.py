"""Server-derived script identity; no authored executable/run/owner arguments."""
from __future__ import annotations

import uuid

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from src.commands.principal import PrincipalKind, current_principal
from src.jobs.playbook import ScriptSubmission, public_job
from src.jobs.policy import JobError
from src.jobs.script_sandbox import SandboxRequest
from src.playbooks.artifact_ref import ARTIFACT_SCHEMA_GENERATION
from src.playbooks.definition import ScriptStep, artifact_sha256, contract_fingerprint
from src.playbooks.invocation import current_invocation
from src.playbooks.private_values import PrivateValueError
from src.playbooks.script_policy import ScriptError


class PlaybookScriptCommandsMixin:
    async def _cmd_playbook_script_run(self, args: dict) -> dict:
        """Submit/rejoin a pinned ScriptStep. Inputs are resolved data only."""
        principal, invocation = current_principal(), current_invocation()
        command = "playbook_script_run"
        if (principal is None or principal.kind is not PrincipalKind.PLAYBOOK
                or invocation is None or not principal.policy.allows_aq_command(command)
                or command not in invocation.aq_commands):
            return {"success": False, "error_code": "unauthorized", "error": "unauthorized"}
        try:
            if set(args) - {"arguments", "environment", "stdin_base64"}:
                raise JobError("jobs.script_arguments_invalid")
            definition = self._v2_engine().services.artifact_store.load(
                invocation.artifact_ref.artifact_sha256)
            step = definition.steps.get(invocation.step_id)
            metadata = definition.trusted_execution
            pinned = metadata.scripts.get(invocation.step_id) if metadata else None
            if (not isinstance(step, ScriptStep) or pinned is None or pinned.program != step.program
                    or invocation.artifact_ref.schema_generation != ARTIFACT_SCHEMA_GENERATION
                    or contract_fingerprint(definition) != invocation.artifact_ref.contract_fingerprint
                    or artifact_sha256(definition) != invocation.artifact_ref.artifact_sha256
                    or definition.id != invocation.artifact_ref.playbook_id
                    or definition.source_hash != invocation.artifact_ref.source_digest
                    or step.rule != invocation.rule_id):
                raise JobError("jobs.script_pin_invalid")
            run = await self.db.load_run(invocation.run_id)
            if (run is None or run.dispatch_id != invocation.dispatch_id
                    or run.rule_id != invocation.rule_id
                    or run.artifact_sha256 != invocation.artifact_ref.artifact_sha256
                    or run.mode != "live"):
                raise JobError("jobs.script_identity_invalid")
            scope = definition.scope
            kind = "profile" if scope.type == "agent_type" else scope.type
            scope_owner = (scope.project_id if kind == "project" else
                           scope.agent_type if kind == "profile" else "system")
            if ((kind == "project" and principal.project_id != scope_owner)
                    or (kind != "project" and principal.project_id is not None)
                    or (kind == "profile" and principal.profile_id != scope_owner)):
                raise JobError("unauthorized")
            request = SandboxRequest(execution_id=uuid.uuid4().hex, run_id=invocation.run_id,
                                     pinned=pinned, **args)
            script = ScriptSubmission(invocation.run_id, invocation.step_id,
                                      invocation.iteration_path, invocation.attempt,
                                      invocation.artifact_ref.artifact_sha256,
                                      kind, scope_owner, request)
            job = await self._jobs().submit(
                project_id=principal.project_id, task_id=None, session_id=None, claim_epoch=None,
                workspace_id=None, generation=None, preset="playbook_script", args=[],
                idempotency_key=script.operation_key, owner_kind="playbook", script=script)
            return {"success": True, "job": public_job(job)}
        except (JobError, ScriptError, PrivateValueError) as exc:
            return {"success": False, "error_code": str(exc), "error": str(exc)}
        except (ValidationError, ValueError, KeyError, TypeError):
            return {"success": False, "error_code": "jobs.script_input_invalid",
                    "error": "jobs.script_input_invalid"}
        except SQLAlchemyError:
            return {"success": False, "error_code": "jobs.storage_unavailable",
                    "error": "jobs.storage_unavailable"}
