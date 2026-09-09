"""Scoped command surface for durable supervisor-owned human escalations.

Identity is deliberately absent from every public argument model.  Dashboard
humans and external adapters are authenticated before this layer and arrive as
server-derived principals; supervisor sessions remain the executor when a
verified reply is applied through a guarded domain service.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

from src.commands.principal import PrincipalKind, TRUSTED_LOCAL, current_principal
from src.database.queries.escalation_queries import EscalationConflict, EscalationStateError

logger = logging.getLogger(__name__)

_LIVE_SESSION_STATES = frozenset({"starting", "running", "draining"})
_FORBIDDEN_AUTHORITY_ARGS = frozenset(
    {"actor", "actor_id", "human", "verified_actor", "supervisor_owner", "thread_id"}
)


def _error(code: str, message: str) -> dict[str, Any]:
    return {"success": False, "error_code": code, "error": message}


class EscalationCommandsMixin:
    """CommandHandler methods for the transport-neutral escalation API."""

    def _reject_authority_args(self, args: Mapping[str, Any]) -> dict[str, Any] | None:
        bad = sorted(_FORBIDDEN_AUTHORITY_ARGS.intersection(args))
        if bad:
            return _error(
                "spoofed_identity",
                "caller identity is server-derived; forbidden fields: " + ", ".join(bad),
            )
        return None

    async def _authorize_escalation_project(
        self,
        project_id: str,
        *,
        supervisor_required: bool = False,
        service_allowed: bool = True,
    ) -> tuple[Any | None, dict[str, Any] | None]:
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.LOCAL:
            if supervisor_required:
                return None, _error(
                    "out_of_scope", "the owning supervisor must execute this escalation action"
                )
            return principal, None
        if principal.kind is PrincipalKind.SERVICE:
            if service_allowed and not supervisor_required:
                return principal, None
            return None, _error(
                "out_of_scope", "an owning supervisor is required for this escalation action"
            )
        if principal.kind is PrincipalKind.PLAYBOOK:
            if (
                not supervisor_required
                and principal.project_id == project_id
                and not principal.unresolved
            ):
                return principal, None
            return None, _error(
                "out_of_scope", "the playbook principal does not own this escalation"
            )
        if principal.kind is not PrincipalKind.SESSION or not principal.elevated:
            return None, _error(
                "out_of_scope", "a trusted core source or elevated supervisor is required"
            )
        row = await self.db.get_session(principal.session_id)
        if (
            row is None
            or row.profile_id != "supervisor"
            or row.lifecycle != "named"
            or row.state not in _LIVE_SESSION_STATES
            or row.desired_state != "running"
        ):
            return None, _error("out_of_scope", "a live supervisor session is required")
        if principal.project_id is not None and principal.project_id != project_id:
            return None, _error("out_of_scope", "escalation belongs to another project")
        if row.project_id is not None and row.project_id != project_id:
            return None, _error("out_of_scope", "escalation belongs to another project")
        return principal, None

    async def _escalation_for_caller(
        self, escalation_id: Any, *, supervisor_required: bool = False
    ) -> tuple[dict[str, Any] | None, Any | None, dict[str, Any] | None]:
        if not isinstance(escalation_id, str) or not escalation_id:
            return None, None, _error("invalid_request", "escalation_id is required")
        incident = await self.db.get_escalation(escalation_id)
        if incident is None:
            return None, None, _error("not_found", "escalation not found")
        principal, error = await self._authorize_escalation_project(
            incident["project_id"],
            supervisor_required=supervisor_required,
            service_allowed=not supervisor_required,
        )
        return incident, principal, error

    async def _emit_escalation(self, event_type: str, payload: dict[str, Any]) -> None:
        """Publish a version-1 state hint after authoritative storage commits."""
        body = {"version": 1, **payload}
        try:
            await self.orchestrator.bus.emit(event_type, body)
        except Exception:
            logger.warning("escalation event %s failed", event_type, exc_info=True)
        try:
            await self.db.log_event(
                event_type,
                project_id=payload.get("project_id"),
                task_id=payload.get("task_id"),
                payload=str(payload.get("escalation_id") or ""),
            )
        except Exception:
            logger.debug("escalation audit event %s failed", event_type, exc_info=True)

    async def emit_escalation_delivery_status(self, delivery: Mapping[str, Any]) -> None:
        """Transport hook for the registered versioned delivery-status event."""
        incident = await self.db.get_escalation(str(delivery["escalation_id"]))
        if incident is None:
            return
        await self._emit_escalation(
            "escalation.delivery_status.v1",
            {
                "escalation_id": incident["id"],
                "project_id": incident["project_id"],
                "task_id": incident.get("task_id"),
                "delivery_id": delivery["id"],
                "status": delivery["status"],
                "attempt_count": delivery["attempt_count"],
                "generation": delivery["generation"],
            },
        )

    async def _cmd_escalation_create(self, args: dict[str, Any]) -> dict[str, Any]:
        if error := self._reject_authority_args(args):
            return error
        required = (
            "project_id",
            "source_kind",
            "source_identity",
            "incident_key",
            "summary",
            "investigation",
            "decision_requested",
            "severity",
        )
        missing = [name for name in required if not isinstance(args.get(name), str) or not args[name]]
        if missing:
            return _error("invalid_request", "required fields: " + ", ".join(missing))
        project_id = args["project_id"]
        principal, error = await self._authorize_escalation_project(project_id)
        if error:
            return error
        if args["severity"] not in {"critical", "high", "medium", "low"}:
            return _error("invalid_request", "severity must be critical, high, medium, or low")
        choices = args.get("choices")
        if choices is not None and (
            not isinstance(choices, list)
            or len(choices) > 20
            or any(not isinstance(item, str) or not item.strip() or len(item) > 500 for item in choices)
        ):
            return _error("invalid_request", "choices must be at most 20 non-empty strings")
        if await self.db.get_project(project_id) is None:
            return _error("not_found", "project not found")
        task_id = args.get("task_id")
        if args["source_kind"] == "question":
            question = await self.db.get_agent_question(args["source_identity"])
            if question is None or question["project_id"] != project_id:
                return _error("invalid_binding", "question source belongs to another project or is missing")
            if task_id is not None and task_id != question["task_id"]:
                return _error("invalid_binding", "question source belongs to another task")
            task_id = question["task_id"]
        elif args["source_kind"] == "gate":
            gate = await self.db.get_gate(args["source_identity"])
            if gate is None or gate["project_id"] != project_id:
                return _error("invalid_binding", "gate source belongs to another project or is missing")
        elif args["source_kind"] == "task_recovery" and task_id is None:
            return _error("invalid_binding", "task_recovery source requires task_id")
        task = None
        if task_id is not None:
            task = await self.db.get_task(task_id)
            if task is None or task.project_id != project_id:
                return _error("out_of_scope", "task does not belong to the escalation project")
        if args["source_kind"] == "task_recovery":
            recovery = await self.db.get_task_meta(task_id, "supervisor_recovery_incident") or {}
            if recovery.get("id") != args["source_identity"]:
                return _error("invalid_binding", "task recovery incident is stale or missing")
        values = {
            "id": args.get("escalation_id") or f"escalation-{uuid.uuid4()}",
            "project_id": project_id,
            "task_id": task_id,
            "source_kind": args["source_kind"],
            "source_identity": args["source_identity"],
            "incident_key": args["incident_key"],
            "supervisor_owner": f"supervisor-{project_id}",
            "task_title": task.title if task is not None else args.get("task_title"),
            "task_status": (
                getattr(task.status, "value", str(task.status)) if task is not None else None
            ),
            "summary": args["summary"],
            "investigation": args["investigation"],
            "decision_requested": args["decision_requested"],
            "choices": choices,
            "severity": args["severity"],
        }
        try:
            incident, created = await self.db.create_escalation(**values)
        except EscalationConflict as exc:
            return _error("identity_conflict", str(exc))
        except ValueError as exc:
            return _error("invalid_request", str(exc))
        if created:
            await self._emit_escalation(
                "escalation.created.v1",
                {
                    "escalation_id": incident["id"],
                    "project_id": incident["project_id"],
                    "task_id": incident.get("task_id"),
                    "source_kind": incident["source_kind"],
                    "source_identity": incident["source_identity"],
                    "incident_key": incident["incident_key"],
                    "state": incident["state"],
                    "revision": incident["revision"],
                },
            )
        return {"success": True, "created": created, "escalation": incident}

    async def _cmd_escalation_list(self, args: dict[str, Any]) -> dict[str, Any]:
        if error := self._reject_authority_args(args):
            return error
        principal = current_principal() or TRUSTED_LOCAL
        project_id = args.get("project_id")
        if principal.kind in {PrincipalKind.SESSION, PrincipalKind.PLAYBOOK}:
            if principal.project_id is None and not principal.elevated:
                return _error("out_of_scope", "a project-scoped principal is required")
            if principal.project_id is not None:
                if project_id is not None and project_id != principal.project_id:
                    return _error("out_of_scope", "escalation belongs to another project")
                project_id = principal.project_id
        if project_id is not None:
            _, error = await self._authorize_escalation_project(project_id)
            if error:
                return error
        limit = args.get("limit", 100)
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            return _error("invalid_request", "limit must be between 1 and 500")
        states = args.get("states")
        if states is not None and (
            not isinstance(states, list)
            or any(
                item
                not in {"needs_human", "reply_received", "resolving", "resolved", "cancelled", "stale"}
                for item in states
            )
        ):
            return _error("invalid_request", "states contains an unknown escalation state")
        rows = await self.db.list_escalations(
            project_id=project_id, states=states, task_id=args.get("task_id"), limit=limit
        )
        result = []
        for incident in rows:
            deliveries = await self.db.list_escalation_deliveries(incident["id"])
            result.append(
                {
                    **incident,
                    "delivery_statuses": [row["status"] for row in deliveries],
                    "pending_delivery": any(
                        row["status"] in {"pending", "sending", "retry", "unknown"}
                        for row in deliveries
                    ),
                }
            )
        return {"success": True, "escalations": result, "count": len(result)}

    async def _cmd_escalation_get(self, args: dict[str, Any]) -> dict[str, Any]:
        if error := self._reject_authority_args(args):
            return error
        incident, _, error = await self._escalation_for_caller(args.get("escalation_id"))
        if error:
            return error
        return {
            "success": True,
            "escalation": incident,
            "messages": await self.db.list_escalation_messages(incident["id"]),
            "deliveries": await self.db.list_escalation_deliveries(incident["id"]),
            "actions": await self.db.list_escalation_actions(incident["id"]),
        }

    def _verified_reply_identity(self) -> tuple[str, str] | None:
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.LOCAL:
            return "dashboard", "human:local-operator"
        if principal.kind is not PrincipalKind.SERVICE or not principal.service_name:
            return None
        transport, separator, actor = principal.service_name.partition(":")
        if not separator or not transport or not actor:
            return None
        return transport, f"human:{transport}:{actor}"

    async def _cmd_escalation_reply(self, args: dict[str, Any]) -> dict[str, Any]:
        if error := self._reject_authority_args(args):
            return error
        incident, _, error = await self._escalation_for_caller(args.get("escalation_id"))
        if error:
            return error
        identity = self._verified_reply_identity()
        if identity is None:
            return _error(
                "human_evidence_required",
                "replies require a dashboard human or trusted '<transport>:<actor>' adapter principal",
            )
        text = args.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            return _error("invalid_request", "text must contain 1 to 16000 characters")
        external_id = args.get("external_message_id")
        if not isinstance(external_id, str) or not external_id:
            return _error("invalid_request", "external_message_id must be a non-empty string")
        transport, actor = identity
        try:
            accepted = await self.db.accept_escalation_reply(
                incident["id"],
                transport=transport,
                external_message_id=external_id,
                verified_actor=actor,
                text=text.strip(),
                received_sequence=args.get("received_sequence"),
            )
        except EscalationConflict as exc:
            return _error("identity_conflict", str(exc))
        except ValueError as exc:
            return _error("invalid_request", str(exc))
        if accepted["created"]:
            await self._emit_escalation(
                "escalation.reply_received.v1",
                {
                    "escalation_id": incident["id"],
                    "project_id": incident["project_id"],
                    "task_id": incident.get("task_id"),
                    "reply_id": accepted["reply"]["id"],
                    "state": accepted["escalation"]["state"],
                    "revision": accepted["escalation"]["revision"],
                    "terminal": accepted["terminal"],
                    "supervisor_enqueued": accepted["supervisor_enqueued"],
                },
            )
        return {"success": True, **accepted}

    async def _cmd_escalation_update(self, args: dict[str, Any]) -> dict[str, Any]:
        if error := self._reject_authority_args(args):
            return error
        incident, _, error = await self._escalation_for_caller(
            args.get("escalation_id"), supervisor_required=True
        )
        if error:
            return error
        expected = args.get("expected_revision")
        if not isinstance(expected, int) or expected < 0:
            return _error("invalid_request", "expected_revision must be a non-negative integer")
        new_state = args.get("state", incident["state"])
        try:
            updated = await self.db.transition_escalation(
                incident["id"],
                expected_revision=expected,
                new_state=new_state,
                summary=args.get("summary"),
                investigation=args.get("investigation"),
                decision_requested=args.get("decision_requested"),
                choices=args.get("choices"),
                severity=args.get("severity"),
                terminal_outcome=args.get("terminal_outcome"),
                terminal_evidence=args.get("terminal_evidence"),
            )
        except EscalationStateError as exc:
            return _error("invalid_state", str(exc))
        except ValueError as exc:
            return _error("invalid_request", str(exc))
        if updated is None:
            return _error("stale_revision", "escalation revision changed; reload and retry")
        await self._emit_escalation(
            "escalation.updated.v1",
            {
                "escalation_id": updated["id"],
                "project_id": updated["project_id"],
                "task_id": updated.get("task_id"),
                "state": updated["state"],
                "revision": updated["revision"],
                "terminal_outcome": updated.get("terminal_outcome"),
            },
        )
        return {"success": True, "escalation": updated}

    async def _validate_apply_binding(
        self, incident: Mapping[str, Any], reply: Mapping[str, Any], args: Mapping[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if (
            reply.get("escalation_id") != incident["id"]
            or reply.get("direction") != "inbound"
            or not reply.get("supervisor_message_id")
        ):
            return None, _error(
                "human_evidence_required", "reply is not verified human evidence for this escalation"
            )
        action_kind = args.get("action_kind")
        target_id = args.get("target_id")
        if action_kind not in {"question_answer", "gate_resolve", "task_recover"}:
            return None, _error("invalid_request", "unsupported action_kind")
        if not isinstance(target_id, str) or not target_id:
            return None, _error("invalid_request", "target_id is required")
        parameters: dict[str, Any] = {}
        if action_kind == "question_answer":
            if args.get("decision") is not None:
                return None, _error("invalid_binding", "decision is valid only for task recovery")
            question = await self.db.get_agent_question(target_id)
            if (
                incident["source_kind"] != "question"
                or incident["source_identity"] != target_id
                or question is None
                or question["project_id"] != incident["project_id"]
                or not question["requires_human"]
            ):
                return None, _error("invalid_binding", "question is not the bound human-required source")
        elif action_kind == "gate_resolve":
            if args.get("decision") is not None:
                return None, _error("invalid_binding", "decision is valid only for task recovery")
            gate = await self.db.get_gate(target_id)
            if (
                incident["source_kind"] != "gate"
                or incident["source_identity"] != target_id
                or gate is None
                or gate["project_id"] != incident["project_id"]
                or gate["gate_type"] != "human"
            ):
                return None, _error("invalid_binding", "gate is not the bound human gate source")
        else:
            decision = args.get("decision")
            if decision not in {"retry", "hold"}:
                return None, _error("invalid_request", "task recovery decision must be retry or hold")
            if incident["source_kind"] != "task_recovery" or incident.get("task_id") != target_id:
                return None, _error("invalid_binding", "task recovery is not bound to this escalation")
            parameters["decision"] = decision
        return parameters, None

    async def _cmd_escalation_apply_reply(self, args: dict[str, Any]) -> dict[str, Any]:
        if error := self._reject_authority_args(args):
            return error
        incident, principal, error = await self._escalation_for_caller(
            args.get("escalation_id"), supervisor_required=True
        )
        if error:
            return error
        reply_id = args.get("reply_id")
        reply = await self.db.get_escalation_message(reply_id) if isinstance(reply_id, str) else None
        if reply is None:
            return _error("not_found", "reply not found")
        parameters, error = await self._validate_apply_binding(incident, reply, args)
        if error:
            return error
        expected = args.get("expected_revision")
        key = args.get("idempotency_key")
        if not isinstance(expected, int) or expected < 0:
            return _error("invalid_request", "expected_revision must be a non-negative integer")
        if not isinstance(key, str) or not key or len(key) > 512:
            return _error("invalid_request", "idempotency_key must contain 1 to 512 characters")
        try:
            reservation = await self.db.begin_escalation_action(
                incident["id"],
                reply_id=reply["id"],
                expected_revision=expected,
                idempotency_key=key,
                action_kind=args["action_kind"],
                target_id=args["target_id"],
                parameters=parameters,
                executor=principal.describe(),
            )
        except EscalationConflict as exc:
            return _error("identity_conflict", str(exc))
        except EscalationStateError as exc:
            code = "stale_revision" if "stale" in str(exc) else "invalid_state"
            return _error(code, str(exc))
        except ValueError as exc:
            return _error("invalid_request", str(exc))
        if not reservation["created"]:
            action = reservation["action"]
            return {
                "success": True,
                "applied": False,
                "replayed": True,
                "action": action,
                "escalation": reservation["escalation"],
            }

        action_result: dict[str, Any]
        try:
            if args["action_kind"] == "question_answer":
                action_result = await self.orchestrator.agent_questions.answer(
                    args["target_id"],
                    reply["text"],
                    actor=reply["verified_actor"],
                    human=True,
                    verified_escalation_id=incident["id"],
                )
            elif args["action_kind"] == "gate_resolve":
                flipped = await self.orchestrator._resolve_gate_and_emit(
                    args["target_id"],
                    resolved_by=principal.describe(),
                    resolution=reply["text"],
                )
                action_result = {
                    "gate_id": args["target_id"],
                    "unblocked_task_ids": sorted(flipped or set()),
                }
            else:
                action_result = await self._cmd_task_recover(
                    {
                        "task_id": args["target_id"],
                        "incident_id": incident["source_identity"],
                        "decision": parameters["decision"],
                        "reason": reply["text"],
                    }
                )
        except Exception as exc:  # the durable reservation must still be completed
            logger.exception("escalation action service failed")
            action_result = {"error": str(exc)}
        succeeded = "error" not in action_result and action_result.get("success") is not False
        outcome = (
            {
                "question_answer": "question_answered",
                "gate_resolve": "gate_resolved",
                "task_recover": "task_recovery_applied",
            }[args["action_kind"]]
            if succeeded
            else "action_failed"
        )
        finished = await self.db.finish_escalation_action(
            reservation["action"]["id"],
            succeeded=succeeded,
            outcome=outcome,
            result=action_result,
            error=None if succeeded else str(action_result.get("error") or "action refused"),
        )
        if not succeeded:
            await self.db.append_escalation_message(
                incident["id"],
                direction="outbound",
                transport="core",
                verified_actor=principal.describe(),
                text=(
                    "The requested recovery action failed and this escalation remains open: "
                    + str(action_result.get("error") or "action refused")
                ),
                external_message_id=f"action-failed:{reservation['action']['id']}",
            )
        await self._emit_escalation(
            "escalation.updated.v1",
            {
                "escalation_id": incident["id"],
                "project_id": incident["project_id"],
                "task_id": incident.get("task_id"),
                "state": finished["escalation"]["state"],
                "revision": finished["escalation"]["revision"],
                "terminal_outcome": finished["escalation"].get("terminal_outcome"),
                "action_id": finished["action"]["id"],
                "action_outcome": finished["action"]["outcome"],
            },
        )
        if not succeeded:
            return {
                **_error("action_failed", str(action_result.get("error") or "action refused")),
                "action": finished["action"],
                "escalation": finished["escalation"],
            }
        return {
            "success": True,
            "applied": True,
            "replayed": False,
            "action": finished["action"],
            "escalation": finished["escalation"],
            "action_result": action_result,
        }
