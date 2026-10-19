"""Static human source adapters; domain transactions and receipts own every effect.

Source identities come from producers, never from a command named in human text.
Missing producer semantics publish an owned context-needed ask with no controls.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict

from sqlalchemy import select, update

from src.asks.models import (
    AskChoice,
    AskError,
    OperationResult,
    SourceIdentity,
    SourceSnapshot,
    digest,
)
from src.asks.operations import get_operation, succeed
from src.asks.review import authenticated_actor
from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal
from src.database.tables import (
    gates,
    integration_subjects,
    task_gates,
    task_proposals,
    tasks,
)

HOLD_CHOICES = (
    AskChoice("approve", "Approve", "Resolve this exact approval gate and release its waiters.", "immediate"),
    AskChoice("decline", "Decline", "Cancel this approval; its waiters remain blocked.", "immediate"),
)


def source_snapshot(source, *, meaning, state, context, choices, owner=None, audience=None):
    """Separate source meaning from its terminal/application projection."""
    return SourceSnapshot(
        source,
        digest({"meaning": meaning, "state": state}),
        digest(meaning),
        context,
        audience or {"kind": "project", "project_id": source.project_id},
        owner or f"supervisor-{source.project_id}",
        {"kind": "human"},
        tuple(choices),
        next_actor="human" if state == "pending" else "none",
        decision_state="pending" if state == "pending" else "answered",
    )


def context_for(source, title, why, waiting, *, requested_by=None, blocked=()):
    return {
        "title": title,
        "why": why,
        "waiting_effect": waiting,
        "requested_by": requested_by or {"label": "AQ", "origin": source.kind},
        "blocked_work": [{"label": tid, "url": f"/tasks/{tid}"} for tid in blocked],
        "blocked_work_note": "The source remains held until the decision is applied.",
        "links": [{"label": "Source request", "url": f"/{source.kind}/{source.key}"}],
    }


async def authorize_source(handler, source, actor, *, conn, application=False):
    if actor.provenance.get("kind") == "verified_button":
        from src.asks.buttons import authorize_button_actor
        await authorize_button_actor(handler.db, handler.config, source, actor,
                                     conn=conn, application=application)
        return
    if actor.provenance.get("kind") == "verified_input":
        from src.asks.authority import authorize_input_actor

        await authorize_input_actor(
            handler.db, handler.config, source, actor, conn=conn, application=application
        )
        return
    principal = current_principal() or TRUSTED_LOCAL
    if principal.project_id and principal.project_id != source.project_id:
        raise AskError("ask.not_found", "Request was not found")
    if actor.provenance.get("kind") == "escalation":
        # Only the command's checked stored-input path constructs this actor.
        from src.database.tables import escalations, escalation_messages

        evidence = (
            await conn.execute(
                select(escalation_messages).where(escalation_messages.c.id == actor.input_id)
            )
        ).mappings().first()
        incident = (
            await conn.execute(
                select(escalations).where(
                    escalations.c.id == actor.provenance.get("escalation_id")
                )
            )
        ).mappings().first()
        if (
            not evidence
            or not incident
            or incident["project_id"] != source.project_id
            or evidence["escalation_id"] != incident["id"]
            or evidence["direction"] != "inbound"
            or not evidence["supervisor_message_id"]
            or evidence["verified_actor"] != actor.principal_id
            or actor.provenance.get("source") != asdict(source)
        ):
            raise AskError("ask.not_decider", "Stored human evidence is not bound to this source")
        if not (application and principal.kind in {PrincipalKind.LOCAL, PrincipalKind.SERVICE}):
            _, refusal = await handler._authorize_escalation_project(
                source.project_id,
                supervisor_required=True,
                conn=conn,
            )
            if refusal:
                raise AskError("ask.not_decider", refusal["error"])
        prefix = "human:discord:"
        if actor.principal_id.startswith(prefix) and actor.principal_id[len(prefix) :] not in {
            str(user) for user in handler.config.discord.authorized_users
        }:
            raise AskError("ask.not_decider", "Human actor is no longer in the current allowlist")
        expected_binding = {
            "question": ("question", source.key),
            "recovery": ("task_recovery", source.key),
            "gate": ("gate", source.key),
            "integration": ("gate", source.key),
        }.get(source.kind)
        if source.kind == "proposal":
            bound = await conn.scalar(
                select(gates.c.id).where(
                    gates.c.project_id == source.project_id,
                    gates.c.await_id == source.key,
                    gates.c.id == incident["source_identity"],
                )
            )
            expected_binding = ("gate", bound)
        if expected_binding != (incident["source_kind"], incident["source_identity"]):
            raise AskError("ask.not_decider", "Escalation source binding differs")
        return
    if (
        application
        and principal.kind is PrincipalKind.SERVICE
        and actor.principal_id == "human:local-operator"
        and actor.provenance == {"kind": "local", "authenticated": "operator"}
        and actor.input_id is None
    ):
        # Only application of an immutable, already accepted packet reaches
        # this port. A service cannot prepare or invent a human decision.
        return
    if principal.kind is not PrincipalKind.LOCAL or actor != authenticated_actor():
        raise AskError("ask.not_decider", "This source requires authenticated human authority")


class SourceAdapter:
    policy = ""
    version = "1"

    def __init__(self, handler):
        self.handler = handler
        self.db = handler.db

    def choices(self, snapshot):
        return snapshot.choices

    async def validate(self, snapshot, actor, intent, *, conn, accepted_decision=None):
        await authorize_source(self.handler, snapshot.identity, actor, conn=conn)
        if accepted_decision:
            return accepted_decision["packet"]["domain"]
        if snapshot.decision_state != "pending":
            raise AskError("ask.stale_source", "Source is no longer awaiting this decision")
        if intent.dispositions or intent.responder_class:
            raise AskError(
                "ask.context_missing", "This source has no document feedback dispositions"
            )
        domain = {"source": asdict(snapshot.identity), "meaning": snapshot.generation_token}
        if actor.provenance.get("kind") == "escalation":
            from src.database.tables import escalations

            revision = await conn.scalar(
                select(escalations.c.revision).where(
                    escalations.c.id == actor.provenance["escalation_id"],
                )
            )
            domain["escalation_revision"] = revision
        return domain

    async def reserve_on(self, decision, *, conn):
        result = await self.apply_on(decision, conn=conn)
        if result.state != "succeeded":
            raise AskError("ask.application_unknown", str(result.receipt))
        await finish_on(self.db, decision, result.receipt, conn=conn)


async def finish_on(db, decision, receipt, *, conn):
    op = await get_operation(conn, decision["id"], "apply", lock=True)
    await succeed(conn, op, {**receipt, "decision_id": decision["id"]})
    state = {"approve": "approved", "decline": "declined"}.get(decision["option_key"], "answered")
    await db.update_ask_projection(
        decision["ask_id"],
        {
            "application_state": "applied",
            "decision_state": state,
            "next_actor": "none",
            "updated_at": time.time(),
        },
        conn=conn,
    )


async def locked_gate(conn, source):
    row = (
        (
            await conn.execute(
                select(gates)
                .where(
                    gates.c.id == source.key,
                    gates.c.project_id == source.project_id,
                )
                .with_for_update()
            )
        )
        .mappings()
        .first()
    )
    if not row or source.reference != {"id": source.key}:
        raise AskError("ask.not_found", "Gate was not found")
    return dict(row)


async def gate_waiters(conn, gate_id):
    # Include the approval binding, not ephemeral status or presentation.
    return [
        dict(r)
        for r in (
            await conn.execute(
                select(task_gates.c.task_id, tasks.c.project_id, tasks.c.parent_task_id)
                .join(tasks, tasks.c.id == task_gates.c.task_id)
                .where(task_gates.c.gate_id == gate_id)
                .order_by(task_gates.c.task_id)
                .with_for_update()
            )
        ).mappings()
    ]


async def settle_gate(db, conn, gate, decision, *, approve):
    receipt = {
        "decision_id": decision["id"],
        "option": decision["option_key"],
        "gate_id": gate["id"],
        "evidence_entry_id": decision["evidence_entry_id"],
    }
    request = dict(gate.get("human_request") or {})
    request["receipt"] = receipt
    await conn.execute(
        update(gates)
        .where(gates.c.id == gate["id"])
        .values(
            status="resolved" if approve else "cancelled",
            resolution=decision["option_key"],
            resolved_by=decision["actor_id"],
            human_request=request,
        )
    )
    waiters = {r["task_id"] for r in await gate_waiters(conn, gate["id"])}
    flipped = await db.recompute_blocked(waiters, conn=conn) if waiters else set()
    ready = await db._note_frontier_entry(conn, set(flipped), reason="unblocked")
    return {**receipt, "flipped": sorted(flipped), "ready_task_ids": sorted(ready)}


class HumanGateAdapter(SourceAdapter):
    source_kind = "gate"

    async def snapshot(self, source, *, conn, lock=True):
        gate = await locked_gate(conn, source)
        if gate["gate_type"] != "human" or str(gate["await_id"] or "").startswith(
            ("integration-subject:", "ask-application:", "prop-")
        ):
            raise AskError("ask.unsupported_source", "This gate has a specialized domain owner")
        request = gate["human_request"] or {}
        producer_context = request.get("context")
        if not isinstance(producer_context, dict):
            producer_context = {}
        waiters = await gate_waiters(conn, gate["id"])
        context = (
            context_for(
                source, gate["title"], gate["question"], "", blocked=[r["task_id"] for r in waiters]
            )
            | producer_context
        )
        choices = HOLD_CHOICES if request.get("policy") == "approval_hold_v1" else ()
        return source_snapshot(
            source,
            meaning={
                "gate": gate["id"],
                "await_id": gate["await_id"],
                "question": gate["question"],
                "request": {
                    k: (
                        {
                            ck: cv
                            for ck, cv in producer_context.items()
                            if ck not in {"title", "links"}
                        }
                        if k == "context"
                        else v
                    )
                    for k, v in request.items()
                    if k != "receipt"
                },
                "waiters": waiters,
                "timeout_at": gate["timeout_at"],
            },
            state="pending" if gate["status"] == "open" else gate["status"],
            context=context,
            choices=choices,
        )

    async def apply_on(self, decision, *, conn):
        source = SourceIdentity(**decision["packet"]["source"])
        gate = await locked_gate(conn, source)
        if gate["status"] != "open":
            receipt = (gate["human_request"] or {}).get("receipt", {})
            if receipt.get("decision_id") == decision["id"]:
                return OperationResult("succeeded", receipt)
            raise AskError("ask.stale_source", "Gate is no longer open")
        return OperationResult(
            "succeeded",
            await settle_gate(
                self.db,
                conn,
                gate,
                decision,
                approve=decision["option_key"] == "approve",
            ),
        )


class ProposalAdapter(SourceAdapter):
    source_kind = "proposal"

    async def read(self, source, conn):
        row = (
            (
                await conn.execute(
                    select(task_proposals)
                    .where(
                        task_proposals.c.id == source.key,
                        task_proposals.c.project_id == source.project_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if not row or source.reference != {"id": source.key}:
            raise AskError("ask.not_found", "Proposal was not found")
        row = dict(row)
        row["payload"] = json.loads(row["payload"])
        bound = (
            (
                await conn.execute(
                    select(gates)
                    .where(
                        gates.c.await_id == source.key,
                        gates.c.project_id == source.project_id,
                        gates.c.gate_type == "human",
                    )
                    .order_by(gates.c.created_at.desc(), gates.c.id)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if not bound:
            raise AskError("ask.context_missing", "Proposal has no bound approval gate")
        return row, dict(bound)

    async def snapshot(self, source, *, conn, lock=True):
        row, gate = await self.read(source, conn)
        payload = {k: v for k, v in row["payload"].items() if k not in {"receipt", "ask_receipt"}}
        waiters = await gate_waiters(conn, gate["id"])
        return source_snapshot(
            source,
            meaning={
                "payload_sha256": digest(payload),
                "gate_id": gate["id"],
                "waiters": waiters,
            },
            state="pending"
            if row["status"] == "ready" and gate["status"] == "open"
            else row["status"],
            context=context_for(
                source,
                gate["title"],
                gate["question"] or f"Apply the change set from {row['source']}.",
                "The proposed changes are not applied until this exact proposal is approved.",
                requested_by={"label": "Proposal producer", "origin": row["source"]},
                blocked=[r["task_id"] for r in waiters],
            ),
            choices=(
                AskChoice(
                    "approve", "Approve", "Materialize the exact proposed tasks and graph changes."
                ),
                AskChoice(
                    "decline", "Decline", "Discard the proposal and retain the approval hold."
                ),
            ),
        )

    async def validate(self, snapshot, actor, intent, *, conn, accepted_decision=None):
        domain = await super().validate(
            snapshot, actor, intent, conn=conn, accepted_decision=accepted_decision
        )
        if accepted_decision:
            return domain
        row, _ = await self.read(snapshot.identity, conn)
        return {**domain, "proposal_id": row["id"], "payload": row["payload"]}

    async def apply_on(self, decision, *, conn):
        source = SourceIdentity(**decision["packet"]["source"])
        row, gate = await self.read(source, conn)
        receipt = row["payload"].get("ask_receipt")
        if receipt and receipt.get("decision_id") == decision["id"]:
            return OperationResult("succeeded", receipt)
        if row["status"] != "ready" or gate["status"] != "open":
            raise AskError("ask.stale_source", "Proposal or its approval gate changed")
        approve = decision["option_key"] == "approve"
        receipt = await settle_gate(self.db, conn, gate, decision, approve=approve)
        if approve:
            result, notifications = await self.handler._materialize_proposal(
                conn,
                source.key,
                gate_id=gate["id"],
                project_id=source.project_id,
            )
            if not result.get("success"):
                raise AskError("ask.stale_source", result.get("error", "Proposal refused"))
            receipt |= {"task_ids": result["task_ids"]}
            # Post-commit notification only. Durable graph/receipt is authoritative on replay.
            self.notifications = (source.key, source.project_id, result["task_ids"], notifications)
            row, _ = await self.read(source, conn)
        row["payload"]["ask_receipt"] = receipt
        await conn.execute(
            update(task_proposals)
            .where(task_proposals.c.id == source.key)
            .values(
                status="committed" if approve else "discarded",
                payload=json.dumps(row["payload"]),
                updated_at=time.time(),
            )
        )
        return OperationResult("succeeded", receipt)


class IntegrationAdapter(SourceAdapter):
    source_kind = "integration"

    async def read(self, source, conn):
        from src.integration.records import journal_entry_on
        from src.integration.runtime_contracts import Subject

        row = (
            (
                await conn.execute(
                    select(integration_subjects)
                    .where(
                        integration_subjects.c.id == source.reference.get("subject_id"),
                        integration_subjects.c.project_id == source.project_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if not row or source.key != source.reference.get("gate_id"):
            raise AskError("ask.not_found", "Integration request was not found")
        subject = Subject.from_row(dict(row))
        definition = await journal_entry_on(conn, subject.id, f"gate:{source.key}")
        if not definition:
            raise AskError("ask.stale_source", "Integration gate has no journal definition")
        return subject, definition

    async def snapshot(self, source, *, conn, lock=True):
        from src.integration.gates import GatePrimitives

        subject, entry = await self.read(source, conn)
        request = entry["payload"]["request"]
        current = subject.schedule.gate_id == source.key and GatePrimitives._same_identity(
            entry, subject
        )
        return source_snapshot(
            source,
            meaning={
                "subject_id": subject.id,
                "generation": subject.generation,
                "version": subject.version,
                "head": GatePrimitives._head(subject),
                "artifact": subject.policy.artifact_sha256,
                "definition": entry,
            },
            state="pending" if current else "stale",
            context=context_for(
                source,
                "Integration decision",
                request["question"],
                "Integration remains held; an answer preserves all product hold checks.",
            ),
            choices=[
                AskChoice(k, k, f"Record {k!r} for this exact integration head and policy.")
                for k in request["choices"]
            ],
        )

    async def apply_on(self, decision, *, conn):
        from src.integration.gates import GatePrimitives

        subject, _ = await self.read(SourceIdentity(**decision["packet"]["source"]), conn)
        result = await GatePrimitives(self.db).answer_on(
            conn,
            subject,
            decision["packet"]["source"]["key"],
            choice=decision["option_key"],
            answered_by=decision["actor_id"],
            verified_human=True,
            decision_id=decision["id"],
            evidence_entry_id=decision["evidence_entry_id"],
        )
        if result.is_unknown:
            raise AskError("ask.stale_source", result.reason)
        return OperationResult("succeeded", dict(result.detail))


class WaitAdapter(SourceAdapter):
    source_kind = "wait"

    async def snapshot(self, source, *, conn, lock=True):
        from src.database.tables import playbook_v2_runs
        from src.playbooks.run_state import deserialize_snapshot

        row = (
            (
                await conn.execute(
                    select(playbook_v2_runs)
                    .where(
                        playbook_v2_runs.c.run_id == source.reference.get("run_id"),
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if not row:
            raise AskError("ask.not_found", "Playbook run was not found")
        snap = deserialize_snapshot(row["snapshot"], version=row["snapshot_version"])
        if snap.event.get("project_id") != source.project_id:
            raise AskError("ask.not_found", "Playbook run was not found")
        engine = self.handler._v2_engine()
        artifact = engine._load(await engine._ref_for(snap))
        step = artifact.steps.get(source.reference.get("step_id"))
        from src.playbooks.definition import WaitStep

        if not isinstance(step, WaitStep) or step.wait_kind != "human":
            raise AskError("ask.unsupported_source", "This is not a human wait")
        current = (
            snap.wait
            and snap.wait.wait_id == source.key
            and snap.wait.kind == "human"
            and snap.wait.step_id == source.reference.get("step_id")
        )
        meaning = {
            "wait_id": source.key,
            "artifact_sha256": snap.artifact_sha256,
            "outcomes": list(step.outcomes),
            "step": step.model_dump(mode="json"),
        }
        # The artifact declares effects as outgoing outcome edges; the current
        # source identifies the exact visit, even on a loop back to this step.
        return source_snapshot(
            source,
            meaning=meaning,
            state="pending" if current else "consumed",
            context=context_for(
                source,
                f"Decision for {snap.playbook_id}",
                f"The playbook is waiting at step {source.reference['step_id']} for a declared human outcome.",
                "This exact visit remains suspended until an outcome is recorded.",
            ),
            choices=[
                AskChoice(
                    k,
                    k,
                    f"Take the declared {k!r} outcome from step {source.reference['step_id']}.",
                )
                for k in step.outcomes
                if k not in {"timed_out", "contract_violation"}
            ],
        )

    async def reserve_on(self, decision, *, conn):
        # Engine commits its receipt on its own serialized run boundary.
        pass

    async def start_operation(self, decision, operation):
        from src.playbooks.engine import HumanDecision
        from src.commands.principal import ExecutionPrincipal

        source = decision["packet"]["source"]
        domain = decision["packet"]["domain"]
        outcome = await self.handler._v2_engine().resume(
            source["reference"]["run_id"],
            HumanDecision(
                decision["option_key"],
                {"note": decision["overall_note"], "resolved_by": decision["actor_id"]},
                source["key"],
                domain["artifact_sha256"],
                decision["id"],
            ),
            ExecutionPrincipal.service("ask-application"),
        )
        result = await self.reconcile_operation(decision, operation)
        if result.state == "succeeded":
            return result
        if outcome.outcome in {"stale_human_decision", "undeclared_human_outcome"}:
            return OperationResult("failed", {"error": outcome.outcome})
        return result

    async def validate(self, snapshot, actor, intent, *, conn, accepted_decision=None):
        packet = await super().validate(
            snapshot, actor, intent, conn=conn, accepted_decision=accepted_decision
        )
        if accepted_decision:
            return packet
        from src.database.tables import playbook_v2_runs

        artifact = await conn.scalar(
            select(playbook_v2_runs.c.artifact_sha256).where(
                playbook_v2_runs.c.run_id == snapshot.identity.reference["run_id"],
            )
        )
        return {**packet, "artifact_sha256": artifact}

    async def reconcile_operation(self, decision, operation):
        from src.database.tables import playbook_step_receipts

        source = decision["packet"]["source"]
        async with self.db._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(playbook_step_receipts).where(
                        playbook_step_receipts.c.run_id == source["reference"]["run_id"],
                        playbook_step_receipts.c.wait_id == source["key"],
                    )
                )
            ).mappings()
            for row in rows:
                proof = json.loads(row["result"]).get("human_decision", {})
                if (
                    proof.get("decision_id") == decision["id"]
                    and proof.get("outcome") == decision["option_key"]
                    and row["artifact_sha256"] == decision["packet"]["domain"]["artifact_sha256"]
                ):
                    return OperationResult("succeeded", proof)
        return OperationResult("unknown", {"reason": "No consumed wait receipt"})


class QuestionAdapter(SourceAdapter):
    source_kind = "question"

    async def snapshot(self, source, *, conn, lock=True):
        q = await self.db.lock_question_source_on(conn, source.key)
        if not q or q["project_id"] != source.project_id or source.reference != {"id": source.key}:
            raise AskError("ask.not_found", "Question was not found")
        meaning = {
            k: q[k]
            for k in (
                "id",
                "session_id",
                "instance_token",
                "claim_epoch",
                "task_id",
                "agent_id",
                "requires_human",
                "question",
            )
        }
        current = await self.db.question_claim_current_on(conn, q)
        return source_snapshot(
            source,
            meaning=meaning,
            state="pending" if current and q["state"] in {"human", "supervisor"} else "stale",
            context=context_for(
                source,
                "Worker needs an answer",
                q["question"],
                "The worker remains waiting; a saved answer must still be delivered to its claim.",
                requested_by={"label": q["agent_id"], "origin": q["session_id"]},
                blocked=[q["task_id"]],
            ),
            choices=(
                AskChoice("answer", "Send answer", "Deliver the exact note to this worker claim."),
            ),
        )

    async def validate(self, snapshot, actor, intent, *, conn, accepted_decision=None):
        packet = await super().validate(
            snapshot, actor, intent, conn=conn, accepted_decision=accepted_decision
        )
        if not accepted_decision and not intent.overall_note.strip():
            raise AskError("ask.context_missing", "The exact answer is required in the note")
        return packet

    async def reserve_on(self, decision, *, conn):
        await self.handler.orchestrator.agent_questions.answer_on(conn, decision)

    async def start_operation(self, decision, operation):
        q = await self.db.get_agent_question(decision["packet"]["source"]["key"])
        await self.handler.orchestrator.agent_questions._deliver(q, time.time())
        return await self.reconcile_operation(decision, operation)

    async def reconcile_operation(self, decision, operation):
        q = await self.db.get_agent_question(decision["packet"]["source"]["key"])
        if not q or q["ask_decision_id"] != decision["id"]:
            return OperationResult("unknown", {"reason": "Question receipt differs"})
        receipt = {
            "question_id": q["id"],
            "decision_id": decision["id"],
            "state": q["state"],
            "submission_state": q["submission_state"],
        }
        if q["submission_state"] == "delivered":
            return OperationResult("succeeded", receipt)
        if q["state"] in {"stale", "resolved"}:
            return OperationResult("failed", receipt)
        return OperationResult("unknown", receipt)


class RecoveryAdapter(SourceAdapter):
    source_kind = "recovery"

    async def snapshot(self, source, *, conn, lock=True):
        row = (
            (
                await conn.execute(
                    select(tasks)
                    .where(
                        tasks.c.id == source.reference.get("task_id"),
                        tasks.c.project_id == source.project_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if not row:
            raise AskError("ask.not_found", "Recovery task was not found")
        meta, attempt = await self.db._recovery_context(conn, row)
        incident = meta.get("supervisor_recovery_incident") or {}
        if incident.get("id") != source.key:
            raise AskError("ask.stale_source", "Recovery attempt changed")
        from src.database.queries.task_recovery_queries import recovery_ask_meaning

        meaning = recovery_ask_meaning(
            row, incident, attempt, await self.db._recovery_owner(conn, row), meta
        )
        return source_snapshot(
            source,
            meaning=meaning,
            state="pending"
            if row["status"] == "BLOCKED" and not incident.get("decision")
            else "decided",
            context=context_for(
                source,
                "Recover a stopped worker attempt",
                incident.get("reason", ""),
                "The task stays blocked until a checked retry is applied.",
                blocked=[row["id"]],
            ),
            choices=(
                AskChoice(
                    "retry",
                    "Retry",
                    "Recheck the stopped attempt and recovery policy, then requeue.",
                ),
                AskChoice("hold", "Hold", "Record a hold and keep this task blocked."),
            ),
        )

    async def reserve_on(self, decision, *, conn):
        pass  # The recovery port records the source ID with the checked transition.

    async def start_operation(self, decision, operation):
        source = decision["packet"]["source"]
        result = await self.handler._cmd_task_recover(
            {
                "task_id": source["reference"]["task_id"],
                "incident_id": source["key"],
                "decision": decision["option_key"],
                "reason": decision["overall_note"] or decision["packet"]["context"]["why"],
            },
            ask_decision=decision,
        )
        if "error" in result:
            return OperationResult("failed", result)
        return await self.reconcile_operation(decision, operation)

    async def reconcile_operation(self, decision, operation):
        source = decision["packet"]["source"]
        incident = (
            await self.db.get_task_meta(
                source["reference"]["task_id"],
                "supervisor_recovery_incident",
            )
            or {}
        )
        if (
            incident.get("id") == source["key"]
            and incident.get("ask_decision_id") == decision["id"]
        ):
            return OperationResult(
                "succeeded",
                {
                    "decision_id": decision["id"],
                    "incident_id": source["key"],
                    "decision": incident["decision"],
                    "decided_at": incident["decided_at"],
                },
            )
        return OperationResult("unknown", {"reason": "Recovery source has no matching receipt"})


class FreeTextAdapter(SourceAdapter):
    """Persisted producer identity without authority to execute arbitrary text."""

    source_kind = "free_text"

    async def snapshot(self, source, *, conn, lock=True):
        from src.database.tables import escalations

        row = (
            (
                await conn.execute(
                    select(escalations)
                    .where(
                        escalations.c.id == source.key,
                        escalations.c.project_id == source.project_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if not row or source.reference != {"id": source.key}:
            raise AskError("ask.not_found", "Producer request was not found")
        return source_snapshot(
            source,
            meaning={
                "producer_key": [row["source_kind"], row["source_identity"], row["incident_key"]],
                "requested": row["decision_requested"],
            },
            state="decided" if row["state"] in {"resolved", "cancelled", "stale"} else "pending",
            context=context_for(
                source,
                row["summary"],
                row["investigation"],
                "The owning supervisor must supply a bound source outcome before this request is done.",
            ),
            choices=(),
            owner=row["supervisor_owner"],
        )


def source_adapters(handler):
    """Constructed only by CommandHandler, with no caller-configurable action table."""
    return [
        HumanGateAdapter(handler),
        ProposalAdapter(handler),
        IntegrationAdapter(handler),
        WaitAdapter(handler),
        QuestionAdapter(handler),
        RecoveryAdapter(handler),
        FreeTextAdapter(handler),
    ]
