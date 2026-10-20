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
from src.database.queries.escalation_queries import (
    TERMINAL_ESCALATION_STATES,
    EscalationConflict,
    EscalationStateError,
)
from src.escalations.sweep import SCAN_LIMIT, TARGET_OPEN_ITEMS, EscalationSweeper

logger = logging.getLogger(__name__)

_LIVE_SESSION_STATES = frozenset({"starting", "running", "draining"})
_FORBIDDEN_AUTHORITY_ARGS = frozenset(
    {"actor", "actor_id", "human", "verified_actor", "supervisor_owner", "thread_id"}
)

#: The states ``escalation_resolve`` may close.  ``resolving`` is the supervisor
#: acting on a recorded answer; ``reply_received`` is one answer ago and walks
#: the single legal step into ``resolving`` first.  ``needs_human`` is absent on
#: purpose: nothing has been answered yet, so there is no outcome to report and
#: the honest states are "wait" and "withdraw", not "resolved".
RESOLVABLE_STATES = frozenset({"reply_received", "resolving"})

#: ``escalations.outcome`` for an incident a person closed, which is what keeps
#: §5.2's collapsed row reading "Resolved" rather than "No longer needed".
HUMAN_ESCALATION_OUTCOME = "human"


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
        conn=None,
    ) -> tuple[Any | None, dict[str, Any] | None]:
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.LOCAL:
            if supervisor_required:
                return None, _error(
                    "out_of_scope", "a project or global supervisor must execute this action"
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
        row, _ = await self.db.get_session_with_profile(principal.session_id, conn=conn)
        if (
            row is None
            or row.profile_id not in {"supervisor", "supervisor-global"}
            or row.lifecycle != "named"
            or row.state not in _LIVE_SESSION_STATES
            or row.desired_state != "running"
        ):
            return None, _error("out_of_scope", "a live supervisor session is required")
        # The global supervisor's logical address can also be its installed
        # profile id. Both its authenticated scope and stored session must be
        # projectless; a project supervisor cannot gain global authority by
        # dropping a project argument.
        if row.profile_id == "supervisor-global" and (
            row.project_id is not None or principal.project_id is not None
        ):
            return None, _error("out_of_scope", "a global supervisor must have system scope")
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
        return await self._create_escalation(args)

    async def _create_escalation(self, args: dict[str, Any], *, conn=None) -> dict[str, Any]:
        """Shared creation boundary; wait deadlines supply their owned transaction."""
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
        missing = [
            name for name in required
            if not isinstance(args.get(name), str) or not args[name].strip()
        ]
        if missing:
            return _error("invalid_request", "required fields: " + ", ".join(missing))
        project_id = args["project_id"]
        principal, error = await self._authorize_escalation_project(project_id, conn=conn)
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
        if args["source_kind"] == "continuation":
            # Infrastructure holds may follow an already satisfied wait. They
            # belong to the suspension intent, rather than a pending deadline.
            if conn is None or principal.kind is not PrincipalKind.SERVICE:
                return _error("invalid_binding", "continuation incidents require the wait transaction")
            from sqlalchemy import select
            from src.database.tables import continuations, wait_records

            prefix, separator, identity = args["source_identity"].partition(":")
            continuation_id, separator2, reason = identity.partition(":")
            intent = (await conn.execute(select(continuations).join(
                wait_records, wait_records.c.id == continuations.c.wait_id,
            ).where(
                continuations.c.id == continuation_id,
                continuations.c.task_id == task_id,
                continuations.c.resume_state.not_in(("acknowledged", "failed", "cancelled")),
                wait_records.c.project_id == project_id,
                wait_records.c.owner_kind == "task",
                wait_records.c.owner_id == task_id,
            ))).mappings().first()
            if (prefix != "continuation" or not separator or not separator2 or not reason
                    or not intent or args["incident_key"] != args["source_identity"]):
                return _error("invalid_binding", "continuation incident does not match its intent")
        if args["source_kind"] == "wait":
            # Only the timer/loop command can bind an operational wait incident.
            # A caller cannot manufacture a wait escalation through the generic API.
            if conn is None or principal.kind is not PrincipalKind.SERVICE:
                return _error("invalid_binding", "wait incidents require the wait transaction")
            from sqlalchemy import select
            from src.database.tables import wait_records

            wait_id, separator, generation = args["source_identity"].rpartition(":")
            wait = (await conn.execute(select(wait_records).where(
                wait_records.c.id == wait_id, wait_records.c.project_id == project_id,
                wait_records.c.outcome == "pending",
            ))).mappings().first()
            if (not separator or not generation.isdecimal() or int(generation) < 1
                    or not wait or args["incident_key"] != f"wait:{wait_id}:{generation}"
                    or task_id != (wait["owner_id"] if wait["owner_kind"] == "task" else None)):
                return _error("invalid_binding", "wait incident does not match its generation")
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
        if not isinstance(values["task_title"], str) or not values["task_title"].strip():
            return _error("invalid_request", "task_title is required when no task supplies a title")
        try:
            incident, created = await self.db.create_escalation(conn=conn, **values)
        except EscalationConflict as exc:
            return _error("identity_conflict", str(exc))
        except ValueError as exc:
            return _error("invalid_request", str(exc))
        created_payload = {
            "version": 1,
            "escalation_id": incident["id"],
            "project_id": incident["project_id"],
            "task_id": incident.get("task_id"),
            "source_kind": incident["source_kind"],
            "source_identity": incident["source_identity"],
            "incident_key": incident["incident_key"],
            "state": incident["state"],
            "revision": incident["revision"],
        }
        if created and conn is not None:
            import json
            import time
            from sqlalchemy import insert
            from src.database.tables import events

            await conn.execute(insert(events).values(
                event_type="escalation.created.v1", project_id=project_id, task_id=task_id,
                timestamp=time.time(), payload=json.dumps(created_payload),
            ))
        elif created:
            await self._emit_escalation(
                "escalation.created.v1",
                created_payload,
            )
        if incident["source_kind"] not in {"question", "gate", "task_recovery", "wait", "continuation"}:
            from src.asks.models import SourceIdentity
            ask = await self._publish_source_ask(SourceIdentity(
                project_id, "free_text", incident["id"], {"id": incident["id"]},
            ))
            return {"success": True, "created": created, "escalation": incident,
                    "ask_id": ask["id"]}
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
        canonical = await self._cmd_ask_show({
            "ask_id": incident["id"], "alias_kind": "escalation",
        })
        alias = ({"ask_id": canonical["ask"]["id"],
                  "ask_url": f"/focus/asks/{canonical['ask']['id']}",
                  "deprecation": "Use aq ask show/reply/prepare/decide for human decisions; "
                                 "escalation get remains incident diagnostics"}
                 if canonical.get("success") else {})
        return {
            "success": True,
            **alias,
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

    async def _cmd_escalation_resolve(self, args: dict[str, Any]) -> dict[str, Any]:
        """Close an incident a human answered, with the outcome they will read.

        §5.3's other half: once Jack has answered -- from the thread, from a
        choice button, or from the dashboard -- the supervisor says how it ended
        with ``aq escalation resolve <id> --outcome "..."``, and the daemon
        collapses the one post into its one-line resolved form and archives the
        thread.  The outcome is the only text that crosses to the channel, which
        is why it is required and why it is what §5.2 prints.

        Two properties are load-bearing:

        * **The project or global supervisor may resolve.** ``supervisor_required``
          is the same fence ``escalation_update`` and ``escalation_apply_reply``
          use, so a worker or a dashboard session cannot close a question a human
          is still answering.
        * **A newer answer is never resolved over.**  The stored vocabulary
          reaches ``resolved`` only from ``resolving``, so an answered incident
          is walked through the two legal steps rather than by inventing a
          transition, and every step is a compare-and-set against the revision
          it just read.  A human reply that lands mid-walk advances the revision
          and this command reports ``stale_revision`` instead -- the same fence
          :meth:`finish_escalation_action` relies on.

        Delivery is not driven from here: the escalation tick re-plans the
        incident's deliveries from durable state, so this command only has to
        make the transition durable and announce it.
        """
        if error := self._reject_authority_args(args):
            return error
        incident, principal, error = await self._escalation_for_caller(
            args.get("escalation_id"), supervisor_required=True
        )
        if error:
            return error
        outcome = args.get("outcome")
        if incident["source_kind"] == "wait":
            return _error("wait.decide_required", "resolve a wait escalation through wait_decide")
        if not isinstance(outcome, str) or not outcome.strip() or len(outcome) > 4000:
            return _error("invalid_request", "outcome must contain 1 to 4000 characters")
        expected = args.get("expected_revision")
        if expected is None:
            expected = int(incident["revision"])
        elif not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            return _error("invalid_request", "expected_revision must be a non-negative integer")
        if str(incident["state"]) not in RESOLVABLE_STATES:
            if str(incident["state"]) in TERMINAL_ESCALATION_STATES:
                return _error(
                    "invalid_state",
                    f"escalation is already {incident['state']} and terminal states are immutable",
                )
            return _error(
                "invalid_state",
                "no verified human answer is recorded on this escalation; wait for one, or "
                "withdraw the question with aq escalation update --state cancelled",
            )
        path = (
            ("resolving", "resolved")
            if str(incident["state"]) == "reply_received"
            else ("resolved",)
        )
        current = dict(incident)
        for index, target in enumerate(path):
            terminal = target in TERMINAL_ESCALATION_STATES
            try:
                updated = await self.db.transition_escalation(
                    current["id"],
                    expected_revision=expected if index == 0 else int(current["revision"]),
                    new_state=target,
                    terminal_outcome=outcome.strip() if terminal else None,
                    terminal_evidence=(
                        {"resolved_by": principal.describe(), "command": "escalation_resolve"}
                        if terminal
                        else None
                    ),
                    outcome=HUMAN_ESCALATION_OUTCOME if terminal else None,
                )
            except EscalationStateError as exc:
                return _error("invalid_state", str(exc))
            except ValueError as exc:
                return _error("invalid_request", str(exc))
            if updated is None:
                return _error(
                    "stale_revision",
                    "escalation changed while it was being resolved; reload it and resolve again",
                )
            current = updated
        await self._emit_escalation(
            "escalation.updated.v1",
            {
                "escalation_id": current["id"],
                "project_id": current["project_id"],
                "task_id": current.get("task_id"),
                "state": current["state"],
                "revision": current["revision"],
                "terminal_outcome": current.get("terminal_outcome"),
            },
        )
        return {"success": True, "resolved": True, "escalation": current}

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
        from dataclasses import asdict
        from src.asks.models import AskError, DecisionActor, SourceIdentity, digest
        from src.database.tables import escalations
        from sqlalchemy import select

        try:
            kind, target = args["action_kind"], args["target_id"]
            if kind == "gate_resolve":
                source = await self._gate_ask_source(await self.db.get_gate(target))
                option = args.get("option_key")
            elif kind == "question_answer":
                source = SourceIdentity(incident["project_id"], "question", target, {"id": target})
                option = "answer"
            else:
                source = SourceIdentity(incident["project_id"], "recovery",
                    incident["source_identity"], {"task_id": target})
                option = parameters["decision"]
            ask = await self._publish_source_ask(source)
            if not args.get("expected"):
                return {**_error("ask.context_missing",
                    "Review the request effects and pass its expected version; use prepare first"),
                    "ask_id": ask["id"], "ask_url": f"/asks/{ask['id']}",
                    "expected": {k: ask[k] for k in (
                        "decision_generation", "source_fingerprint", "feedback_version")}}
            # No interpretation of an uncertain reply can become an approval.
            # Discussion and natural-language interpretation belong to ask_apply_input.
            if kind != "question_answer" and reply["text"].strip().casefold() != option:
                raise AskError("ask.input_ambiguous", "Stored reply must name the exact declared choice")
            intent = self._ask_intent({"ask_id": ask["id"], "expected": args["expected"],
                "option_key": option, "overall_note": reply["text"]})
            actor = DecisionActor(reply["verified_actor"], {"kind": "escalation",
                "escalation_id": incident["id"], "source": asdict(source)}, reply["id"])
            service = self._ask_service()
            async with self._ask_transaction(source, service) as conn:
                adapter = service.adapters.get(source.kind)
                snap = await adapter.snapshot(source, conn=conn)
                current = (await conn.execute(select(escalations).where(
                    escalations.c.id == incident["id"],
                ).with_for_update())).mappings().one()
                key = args.get("idempotency_key")
                existing = await self.db.get_ask_decision_by_request(
                    ask["id"], actor.principal_id, key, conn=conn,
                ) if isinstance(key, str) and not args.get("prepare") else None
                if not existing and current["revision"] != args.get("expected_revision"):
                    raise AskError("ask.stale_source", "Escalation revision changed")
                await adapter.validate(snap, actor, intent, conn=conn, accepted_decision=existing)
                if args.get("prepare"):
                    preview = await service.prepare_decision(source, actor, intent, conn=conn)
                    return {"success": True, "ask_id": ask["id"], "preview": preview,
                            "expected": args["expected"]}
                key = args.get("idempotency_key")
                if not isinstance(key, str) or not 1 <= len(key) <= 512:
                    raise AskError("ask.context_missing", "A bounded request key is required")
                evidence_id = f"ask-input-{digest([ask['id'], reply['id']])}"
                await self.db.insert_ask_correspondence({
                    "id": evidence_id, "ask_id": ask["id"], "direction": "inbound",
                    "transport": reply["transport"], "verified_actor": actor.principal_id,
                    "text": reply["text"], "entry_kind": "decision_evidence",
                    "received_at": reply["received_at"], "created_at": service.clock(),
                }, conn=conn)
                decision, created = await service.decide(source, actor, intent, conn=conn,
                    preview_id=args.get("preview_id"), idempotency_key=key,
                    evidence_entry_id=evidence_id)
            notifications = getattr(adapter, "notifications", None)
            if notifications:
                await self._publish_committed(*notifications)
            result = await self._apply_ask_decision(decision)
            if created:
                await self._publish_source_application(decision, result)
            return {"success": result["application_state"] == "applied", "created": created,
                    "ask_id": ask["id"], "applied": result["application_state"] == "applied",
                    "replayed": not created, **result}
        except AskError as exc:
            return _error(exc.code, str(exc))

    async def _cmd_escalation_sweep(self, args: dict[str, Any]) -> dict[str, Any]:
        """Plan the §5.6 back-fill sweep; apply it only when asked.

        The dry run is the default and writes nothing, so the review of the plan
        happens before any incident closes.  ``--apply`` runs the *same* plan --
        it is not a second code path -- so the two cannot disagree about what
        would happen.
        """
        if error := self._reject_authority_args(args):
            return error
        principal = current_principal() or TRUSTED_LOCAL
        project_id = args.get("project_id")
        if principal.kind in {PrincipalKind.SESSION, PrincipalKind.PLAYBOOK}:
            if principal.project_id is None and not principal.elevated:
                return _error("out_of_scope", "a project-scoped principal is required")
            if principal.project_id is not None:
                if project_id is not None and project_id != principal.project_id:
                    return _error("out_of_scope", "escalations belong to another project")
                project_id = principal.project_id
        if project_id is not None:
            _, error = await self._authorize_escalation_project(project_id)
            if error:
                return error
        apply = args.get("apply", False)
        if not isinstance(apply, bool):
            return _error("invalid_request", "apply must be a boolean")
        limit = args.get("limit", SCAN_LIMIT)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            return _error("invalid_request", "limit must be between 1 and 500")
        sweeper = EscalationSweeper(
            self.db,
            self.config,
            on_close=self._emit_sweep_close,
        )
        plan, report = await sweeper.run(
            project_id=project_id, apply_changes=apply, limit=limit
        )
        open_after = report.open_after if apply else plan.open_before
        return {
            "success": True,
            "mode": "apply" if apply else "dry_run",
            "plan": plan.to_dict(),
            "report": report.to_dict(),
            "open_before": plan.open_before,
            "open_after": open_after,
            "target_open_items": TARGET_OPEN_ITEMS,
            "within_target": open_after <= TARGET_OPEN_ITEMS,
        }

    async def _emit_sweep_close(self, incident: Mapping[str, Any]) -> None:
        """Publish the same version-1 state hint every other escalation write does.

        The sweep closes incidents through the query layer, not through a
        command, so without this the dashboard and any watcher would only learn
        about a closure on their next read.
        """
        await self._emit_escalation(
            "escalation.updated.v1",
            {
                "escalation_id": incident["id"],
                "project_id": incident["project_id"],
                "task_id": incident.get("task_id"),
                "state": incident["state"],
                "revision": incident["revision"],
                "terminal_outcome": incident.get("terminal_outcome"),
                "swept_by": "escalation_sweep",
            },
        )
