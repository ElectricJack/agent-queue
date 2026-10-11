"""Bounded, audited operator work by a live project worker with a held claim."""

from __future__ import annotations

import inspect
import json
import time
from dataclasses import asdict

from jsonschema import Draft202012Validator
from sqlalchemy import select

from src.commands.operator_authority import (
    HUMAN_DECISION_COMMANDS, OPERATOR_CLASSES, OPERATOR_HARNESSES, OPERATOR_READS,
    OPERATOR_WRITES, PROJECT_SETUP_FIELDS, OperatorExecuteArgs, OperatorGrant,
    OperatorPreflightArgs, arguments_sha256, is_operator_profile, operator_grant_var,
)
from src.commands.principal import PrincipalKind, current_principal, matches_session_instance
from src.database.tables import events, operator_decisions


def refusal(message: str) -> dict:
    return {"success": False, "error_code": "operator_refused", "error": message}


class OperatorCommandsMixin:
    async def _operator_identity(self):
        from src.api.auth import RequestScope
        from src.api.scope import held_task_for_session

        principal = current_principal()
        if principal is None or principal.kind is not PrincipalKind.SESSION or principal.elevated:
            raise ValueError("a scoped operator worker session is required")
        session = await self.db.get_session(principal.session_id)
        if session is None or not matches_session_instance(principal, session.instance_token):
            raise ValueError("operator session instance is stale")
        profile = await self.db.get_profile(session.profile_id)
        if (profile is None or not profile.enabled or not is_operator_profile(profile)
                or profile.default_class not in OPERATOR_CLASSES
                or profile.harness not in OPERATOR_HARNESSES
                or session.intelligence_class != profile.default_class
                or session.harness != profile.harness):
            raise ValueError("operator requires an enabled frontier standard-high/deep-high profile")
        task = await held_task_for_session(self.db, RequestScope(
            kind="session", session_id=session.id, task_id=principal.task_id,
            project_id=principal.project_id,
        ))
        if (task is None or getattr(task.task_type, "value", None) != "operator"
                or session.last_claim_epoch != task.claim_epoch):
            raise ValueError("operator requires a live operator task and exact claim epoch")
        return task, session

    async def _operator_action(self, action, task, session) -> tuple[dict, str]:
        from src.api.target_scope import target_references, target_scope_error
        from src.commands.contracts import CONTRACTS
        from src.tools.definitions import _ALL_TOOL_DEFINITIONS, _FALLBACK_INPUT_SCHEMAS

        command = action.command
        if command not in OPERATOR_READS | OPERATOR_WRITES:
            raise ValueError(f"{command} is outside the operator setup surface")
        definition = next((d for d in _ALL_TOOL_DEFINITIONS if d["name"] == command), None)
        registration = CONTRACTS.get(command)
        schema = (definition["input_schema"] if definition else
                  registration.contract.execution.args_model.model_json_schema() if registration else
                  _FALLBACK_INPUT_SCHEMAS.get(command))
        if schema is None or not callable(getattr(self, "_cmd_" + command, None)):
            raise ValueError(f"command {command} is unavailable")
        arguments = dict(action.arguments)
        fields = set(schema.get("properties", {}))
        if set(arguments) - fields:
            raise ValueError(f"{command}: unknown options {sorted(set(arguments) - fields)}")
        if command == "edit_project" and set(arguments) - PROJECT_SETUP_FIELDS:
            raise ValueError("operator project edits are limited to integration and promotion setup")
        if "project_id" in fields:
            if arguments.get("project_id", task.project_id) != task.project_id:
                raise ValueError("operator action belongs to another project")
            arguments["project_id"] = task.project_id
        missing = set(schema.get("required", ())) - set(arguments)
        if missing:
            raise ValueError(f"{command}: missing options {sorted(missing)}")
        if list(Draft202012Validator(schema).iter_errors(arguments)):
            raise ValueError(f"{command}: invalid option types or values")
        # Playbooks are scoped by their artifact, checked again in the handler.
        if command not in {"provider_status", "playbook_v2_import", "playbook_activate",
                           "set_playbook_enabled", "playbook_delete"}:
            targets = {k: v for k, v in arguments.items() if k != "project_id"}
            if "integration_repository_id" in targets:
                targets["repository_id"] = targets.pop("integration_repository_id")
            if target_references(targets) or "project_id" not in arguments:
                error = await target_scope_error(command, targets, task.project_id, db=self.db)
                if error:
                    raise ValueError(error)
        if command == "playbook_delete" and (
            arguments.get("scope") != "project"
            or arguments.get("scope_identifier") != task.project_id
        ):
            raise ValueError("operator can delete only its project's playbooks")
        if command in {"playbook_activate", "set_playbook_enabled", "playbook_v2_import"}:
            await self._operator_playbook_preflight(command, arguments, task.project_id)
        if command in {"policy_apply", "policy_diff"}:
            preview = await self._operator_dispatch("policy_diff", arguments, task, session)
            if preview.get("error") or preview.get("success") is False:
                raise ValueError(f"policy preflight: {preview.get('error', 'failed')}")
        # Enforce authored grants even on installations running shadow capability checks.
        profile = await self.db.get_profile(session.profile_id)
        if command not in (profile.aq_commands or ()):
            raise ValueError(f"operator profile does not grant {command}")
        digest = arguments_sha256(arguments)
        if command in HUMAN_DECISION_COMMANDS:
            await self._operator_human_decision(action.decision_id, command, digest, task)
        return arguments, digest

    async def _operator_playbook_preflight(self, command, arguments, project_id):
        if not self._v2_api_enabled() or not self._v2_activation_writes_enabled():
            raise ValueError("project playbook lifecycle is disabled")
        sha = None
        definition = None
        if command == "playbook_v2_import":
            bundle = await self._v2_read_import_bundle(arguments.get("path"))
            if bundle.get("error"):
                raise ValueError(bundle["error"])
            definition = bundle["definition"]
            scope, identifier = self._v2_scope(definition)
        elif command == "playbook_activate":
            sha = arguments["artifact_sha256"]
            _ref, definition, error = await self._v2_load_artifact(sha, arguments["playbook_id"])
            if error:
                raise ValueError(error)
            scope, identifier = self._v2_scope(definition)
        else:
            rows = [dict(r) for r in await self.db.list_playbook_activations(enabled_only=False)
                    if r["playbook_id"] == arguments["playbook_id"]]
            if len(rows) != 1:
                raise ValueError("playbook activation is missing or ambiguous")
            scope, identifier = rows[0]["scope"], rows[0]["scope_identifier"]
            sha = rows[0].get("active_artifact_sha256")
        if scope != "project" or identifier != project_id:
            raise ValueError("operator can manage only its project's playbooks")
        if command != "playbook_v2_import" and arguments.get("enabled", True):
            if not sha or not await self._operator_artifact_approved(project_id, sha):
                raise ValueError("operator activation requires an approved review of the exact artifact")
            if definition is None:
                _ref, definition, error = await self._v2_load_artifact(sha, arguments["playbook_id"])
                if error:
                    raise ValueError(error)
        if definition is not None:
            from src.playbooks.validation import validate_definition

            contracts, profiles, events = await self._v2_lookups()
            diagnostics = validate_definition(definition, inventory=None, contracts=contracts,
                                              profiles=profiles, events=events)
            blockers = [d.message for d in diagnostics if d.severity in {"error", "question"}]
            if blockers:
                raise ValueError("playbook prerequisites: " + "; ".join(blockers))

    async def _operator_artifact_approved(self, project_id, sha):
        for review in await self.db.list_reviews(project_id=project_id, state="approved"):
            revision = await self.db.get_review_revision(review["id"], review["current_revision"])
            if revision and (revision.get("playbook") or {}).get("artifact_sha256") == sha:
                return True
        return False

    async def _operator_human_decision(self, decision_id, command, digest, task):
        async with self.db._engine.connect() as conn:
            row = (await conn.execute(select(operator_decisions).where(
                operator_decisions.c.id == decision_id,
                operator_decisions.c.project_id == task.project_id,
                operator_decisions.c.object_kind == "task",
                operator_decisions.c.object_id == task.id,
                operator_decisions.c.effect == "note",
                operator_decisions.c.recorded_by == "human:local-operator",
            ))).mappings().first()
        try:
            binding = json.loads(row["decision"]) if row else None
        except (ValueError, TypeError):
            binding = None
        if binding != {"command": command, "args_sha256": digest, "claim_epoch": task.claim_epoch}:
            raise ValueError(
                "a recorded human decision for this task, claim epoch and exact action is required; "
                f"binding={json.dumps(dict(command=command, args_sha256=digest, claim_epoch=task.claim_epoch))}"
            )

    async def _operator_dispatch(self, command, arguments, task, session, *,
                                 decision_id=None, preflight_id=None):
        fresh_task, fresh_session = await self._operator_identity()
        if (fresh_task.id != task.id or fresh_task.claim_epoch != task.claim_epoch
                or fresh_session.instance_token != session.instance_token):
            raise ValueError("operator claim changed before dispatch")
        audit_id = await self._operator_audit("operator.action", task, session, {
            "command": command, "args_sha256": arguments_sha256(arguments),
            "decision_id": decision_id, "preflight_id": preflight_id,
        })
        if audit_id <= 0:
            raise RuntimeError("operator audit did not return a durable event id")
        grant = OperatorGrant(command, arguments_sha256(arguments), task.project_id,
                              task.id, session.id, task.claim_epoch)
        token = operator_grant_var.set(grant)
        try:
            result = await self.execute(command, arguments)
            await self._operator_audit("operator.result", task, session, {
                "audit_id": audit_id, "ok": result.get("success", "error" not in result),
            })
            return {**result, "operator_audit_id": audit_id, "operator_task_id": task.id}
        finally:
            operator_grant_var.reset(token)

    async def _operator_audit(self, event_type, task, session, payload):
        return await self.db.log_event(
            event_type, project_id=task.project_id, task_id=task.id, agent_id=session.agent_id,
            payload=json.dumps({"session_id": session.id, "claim_epoch": task.claim_epoch, **payload}),
        )

    async def _cmd_operator_execute(self, args):
        task = session = None
        try:
            task, session = await self._operator_identity()
            request = OperatorExecuteArgs.model_validate(args)
            self._operator_request_scope(request, task, session)
            arguments, digest = await self._operator_action(request, task, session)
            if request.command in OPERATOR_WRITES:
                async with self.db._engine.connect() as conn:
                    row = (await conn.execute(select(events).where(
                        events.c.id == request.preflight_id, events.c.task_id == task.id,
                        events.c.project_id == task.project_id,
                        events.c.event_type == "operator.preflight",
                        events.c.timestamp >= time.time() - 600,
                    ))).mappings().first()
                report = json.loads(row["payload"]) if row else {}
                action = {"command": request.command, "args_sha256": digest,
                          "decision_id": request.decision_id}
                if (not report.get("ready") or report.get("session_id") != session.id
                        or report.get("claim_epoch") != task.claim_epoch
                        or action not in report.get("actions", [])):
                    raise ValueError("a ready preflight for this exact action and live claim is required")
            return await self._operator_dispatch(
                request.command, arguments, task, session,
                decision_id=request.decision_id, preflight_id=request.preflight_id,
            )
        except ValueError as exc:
            if task is not None:
                await self._operator_audit("operator.refused", task, session, {
                    "command": args.get("command"), "reason": str(exc),
                })
            return refusal(str(exc))

    async def _cmd_operator_preflight(self, args):
        try:
            request = OperatorPreflightArgs.model_validate(args)
            task, session = await self._operator_identity()
            self._operator_request_scope(request, task, session)
        except ValueError as exc:
            return refusal(str(exc))
        blockers, actions, probes = [], [], {}
        branches = list(request.branches)
        app_ids = list(request.app_ids)
        for action in request.actions:
            try:
                _arguments, digest = await self._operator_action(action, task, session)
                actions.append({"command": action.command, "args_sha256": digest,
                                "decision_id": action.decision_id})
            except ValueError as exc:
                blockers.append(str(exc))
        app_args = {"project_id": task.project_id}
        if request.policy is not None:
            app_args["policy"] = request.policy
        if request.repository_id is not None:
            app_args["repository_id"] = request.repository_id
        # Collect all independent prerequisites, including failures, in one report.
        repository_required = any(
            a.command.startswith("integration_") or a.command == "edit_project"
            for a in request.actions
        ) or bool(request.branches or request.runner_labels or request.app_ids or request.repository_id)
        commands = ["provider_status"]
        if repository_required:
            commands += ["integration_trust_manifest", "integration_app_verify"]
        for command in commands:
            try:
                result = await self._operator_dispatch(
                    command, {} if command == "provider_status" else app_args, task, session,
                )
                probes[command] = result
                if result.get("success") is False or result.get("error"):
                    blockers.append(f"{command}: {result.get('error', result)}")
                blockers.extend(f"{command}: {item}" for item in result.get("blockers", []))
                if result.get("ready") is False and not result.get("blockers"):
                    blockers.append(f"{command}: prerequisites are not ready")
            except Exception as exc:
                blockers.append(f"{command}: {type(exc).__name__}: {exc}")
        if repository_required:
            try:
                await self._operator_repository_probes(
                    request, task, probes, branches, app_ids, blockers)
            except Exception as exc:
                blockers.append(f"repository prerequisites: {type(exc).__name__}: {exc}")
        report = {"ready": not blockers, "actions": actions, "blockers": blockers,
                  "inputs": {"branches": branches, "runner_labels": request.runner_labels,
                             "app_ids": app_ids}, "probes": probes}
        body = "Operator preflight: " + (
            "ready" if not blockers else "all blockers:\n" + "\n".join(f"- {b}" for b in blockers)
        )
        scope = {"kind": "session", "session_id": session.id, "project_id": task.project_id,
                 "task_id": task.id, "session_instance_token": session.instance_token}
        comment = await self.execute("task_comment", {
            "task_id": task.id, "claim_epoch": task.claim_epoch, "body": body, "_scope": scope,
        })
        if comment.get("error") or comment.get("success") is False:
            return refusal("preflight could not record its task comment")
        if blockers:
            message = await self.execute("message_send", {
                "project_id": task.project_id, "to_kind": "user", "to_id": "dashboard", "body": body,
                "from_kind": "session", "from_id": session.id,
                "_scope": scope,
            })
            if message.get("error") or message.get("success") is False:
                report["notification_error"] = message.get("error", "message failed")
        preflight_id = await self._operator_audit("operator.preflight", task, session, report)
        if preflight_id <= 0:
            return refusal("preflight audit did not return a durable event id")
        return {"success": True, "preflight_id": preflight_id, **report}

    async def _operator_repository_probes(self, request, task, probes, branches, app_ids, blockers):
        """Append the repository's GitHub blockers; raises if the repository is unreadable."""
        project = await self.db.get_project(task.project_id)
        repo = await self.db.get_repo(request.repository_id or project.integration_repository_id)
        if repo is None or repo.project_id != task.project_id:
            raise ValueError("project integration repository is unavailable")
        binding = self.orchestrator.github_repository_binding_resolver(repo)
        if inspect.isawaitable(binding):
            binding = await binding
        client = self.orchestrator.github_client_factory(binding)
        if inspect.isawaitable(client):
            client = await client
        default_branch = getattr(repo, "default_branch", None)
        if default_branch and default_branch not in branches:
            branches.append(default_branch)
        from src.integration.promotion_steps import read_stored_promotion_flow
        flows = [await read_stored_promotion_flow(self.db, task.project_id) or []]
        flows.extend(a.arguments.get("promotion_flow") or [] for a in request.actions
                     if a.command == "edit_project" and "promotion_flow" in a.arguments)
        for flow in flows:
            for step in flow:
                for field in ("source", "target"):
                    branch = step.get(field)
                    if isinstance(branch, str) and branch not in branches:
                        branches.append(branch)
        manifest = probes.get("integration_trust_manifest", {}).get("manifest", {})
        probes["required_app_identities"] = {
            role: manifest[role] for role in ("attestation_app_id", "ci_producer_app_id")
            if role in manifest
        }
        for app_id in probes["required_app_identities"].values():
            if isinstance(app_id, int) and app_id not in app_ids:
                app_ids.append(app_id)
        for branch in branches:
            try:
                if await client.exact_head_ref(branch) is None:
                    blockers.append(f"branch {branch} does not exist or is inaccessible")
            except Exception as exc:
                blockers.append(f"branch {branch}: {exc}")
        if request.runner_labels:
            try:
                online, page = [], 1
                while True:
                    result = await client.request_json(
                        "GET", f"/repositories/{binding.repository_id}/actions/runners"
                        f"?per_page=100&page={page}",
                    )
                    runners = result.get("runners", [])
                    online.extend(r for r in runners if r.get("status") == "online")
                    if len(runners) < 100:
                        break
                    if page >= 10:
                        raise ValueError("runner inventory exceeds the 1,000-runner preflight limit")
                    page += 1
                labels = [{item["name"] for item in r.get("labels", [])} for r in online]
                if not any((required := set(request.runner_labels)) <= available
                           for available in labels):
                    blockers.append("no online runner with all labels " + ", ".join(sorted(required)))
                probes["online_runners"] = [r.get("name") for r in online]
            except Exception as exc:
                blockers.append(f"runner prerequisites: {type(exc).__name__}: {exc}")
        from src.git.github_contracts import credential_identity_from_client
        identity = credential_identity_from_client(client)
        probes["app_identity"] = asdict(identity)
        observed_apps = {identity.app_id, *probes["required_app_identities"].values()}
        for app_id in request.app_ids:
            if app_id not in observed_apps:
                blockers.append(f"required GitHub App {app_id} is not present in the verified identities")

    @staticmethod
    def _operator_request_scope(request, task, session):
        for key, expected in (("project_id", task.project_id), ("task_id", task.id),
                              ("session_id", session.id)):
            if getattr(request, key) not in {None, expected}:
                raise ValueError(f"{key} mismatch")
