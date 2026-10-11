"""Foundation for CommandHandler-owned ask publication and packet reservation.

This package enables no entry points or external sends. Domain adapters added by
subsequent packages own approval, gates, feedback operations and finalization.
Every method takes the caller's connection; snapshot() locks the source before
publication takes an ask lock. A reservation is never an applied decision.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict

from src.asks.adapters import AdapterRegistry
from src.asks.models import (
    PREVIEW_TTL_SECONDS,
    AliasReference,
    AskError,
    DecisionActor,
    DecisionIntent,
    Publication,
    SourceIdentity,
    SourceSnapshot,
    SourceVersion,
    digest,
    validate_context,
)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def version_of(ask: dict) -> SourceVersion:
    return SourceVersion(
        ask["decision_generation"], ask["source_fingerprint"], ask["feedback_version"]
    )


def check_version(ask: dict, expected: SourceVersion) -> None:
    if ask["feedback_version"] != expected.feedback_version:
        raise AskError("ask.stale_feedback", "Feedback changed; prepare a new decision packet")
    if version_of(ask) != expected:
        raise AskError("ask.stale_source", "Source changed; reload the request")
    if ask["context_state"] != "complete":
        raise AskError("ask.context_missing", "This ask is held for context/identity repair")


class AskService:
    def __init__(self, db, adapters: AdapterRegistry, *, clock=time.time):
        self.db = db
        self.adapters = adapters
        self.clock = clock

    async def publish(
        self,
        source: SourceIdentity,
        *,
        conn,
        policy: str = "",
        aliases: tuple[AliasReference, ...] = (),
        legacy: bool = False,
    ) -> Publication:
        adapter = self.adapters.get(source.kind, policy)
        snapshot = await adapter.snapshot(source, conn=conn, lock=True)
        if snapshot.identity != source or adapter.choices(snapshot) != snapshot.choices:
            raise AskError("ask.stale_source", "Adapter changed canonical identity or choices")
        return await self.publish_snapshot(snapshot, conn=conn, aliases=aliases, legacy=legacy)

    async def publish_snapshot(
        self,
        snapshot: SourceSnapshot,
        *,
        conn,
        aliases: tuple[AliasReference, ...] = (),
        legacy: bool = False,
    ) -> Publication:
        """Internal projection port; caller MUST already hold the source lock.

        Aliases must be verified by the owning adapter, never inferred from titles.
        Conflicts return a quarantined publication so the repair record commits;
        callers must not turn that result into a transaction-rolling exception.
        """
        invalid = validate_context(snapshot)
        if invalid and not legacy:
            raise AskError("ask.context_missing", "Missing or invalid ask context", fields=invalid)
        context = snapshot.public_context()
        if invalid:
            if not snapshot.logical_owner or "links" in invalid:
                raise AskError(
                    "ask.context_missing",
                    "Legacy asks still require owner/source links",
                    fields=invalid,
                )
            context = {
                "title": "AQ needs to explain this request",
                "links": snapshot.context["links"],
                "why": "Source context requires repair before a choice can be offered.",
                "waiting_effect": "The source remains held.",
                "choices": [],
                "missing_fields": list(invalid),
            }
        now = self.clock()
        identity = snapshot.identity
        values = {
            "id": new_id("ask"),
            "project_id": identity.project_id,
            "source_kind": identity.kind,
            "source_key": identity.key,
            "source_identity": identity.reference,
            "source_fingerprint": snapshot.fingerprint,
            "generation_fingerprint": snapshot.generation_fingerprint,
            "feedback_version": snapshot.feedback_version,
            "decision_generation": 1,
            "display_version": 1,
            "context": context,
            "audience": snapshot.audience,
            "logical_owner": snapshot.logical_owner,
            "decider": snapshot.decider,
            "next_actor": "agent" if invalid else snapshot.next_actor,
            "decision_state": snapshot.decision_state,
            "application_state": snapshot.application_state,
            "context_state": "context_needed" if invalid else "complete",
            "severity": snapshot.severity,
            "created_at": now,
            "updated_at": now,
        }
        ask, created = await self.db.insert_ask(values, conn=conn)
        cases = []
        if not created:
            if ask["source_identity"] != identity.reference:
                cases.append(
                    await self._conflict(ask, "source_identity", identity.reference, conn=conn)
                )
            else:
                mutable = {
                    k: v
                    for k, v in values.items()
                    if k
                    not in {
                        "id",
                        "project_id",
                        "source_kind",
                        "source_key",
                        "source_identity",
                        "created_at",
                        "decision_generation",
                        "display_version",
                    }
                }
                changed_generation = (
                    ask["generation_fingerprint"] != snapshot.generation_fingerprint
                )
                mutable["decision_generation"] = ask["decision_generation"] + int(
                    changed_generation
                )
                if not changed_generation and await self.db.get_ask_decision_by_generation(
                    ask["id"],
                    ask["decision_generation"],
                    conn=conn,
                ):
                    # Only the application coordinator can settle a reserved
                    # decision. Refreshing presentation cannot erase applying,
                    # unknown, failed or applied receipts.
                    for key in ("application_state", "decision_state", "next_actor"):
                        mutable[key] = ask[key]
                # An ordinary projection refresh cannot clear an identity quarantine.
                if ask["context_state"] == "quarantined":
                    mutable["context_state"] = "quarantined"
                    mutable["next_actor"] = "agent"
                    mutable["context"] = {**context, "choices": []}
                display_changed = any(ask[k] != v for k, v in mutable.items() if k != "updated_at")
                mutable["display_version"] = ask["display_version"] + int(display_changed)
                ask = await self.db.update_ask_projection(ask["id"], mutable, conn=conn)
        for alias in aliases:
            if (
                alias.source_reference != identity.reference
                or not alias.provenance
                or alias.source_generation != ask["decision_generation"]
            ):
                cases.append(
                    await self._conflict(
                        ask, f"alias:{alias.kind}:{alias.key}", asdict(alias), conn=conn
                    )
                )
                continue
            existing, matches = await self.db.insert_ask_alias(
                {
                    "project_id": identity.project_id,
                    "legacy_kind": alias.kind,
                    "legacy_id": alias.key,
                    "ask_id": ask["id"],
                    "source_generation": alias.source_generation,
                    "source_reference": alias.source_reference,
                    "provenance": alias.provenance,
                    "created_at": now,
                },
                conn=conn,
            )
            if not matches:
                cases.append(
                    await self._conflict(
                        ask,
                        f"alias:{alias.kind}:{alias.key}",
                        {"existing": existing, "attempted": asdict(alias)},
                        conn=conn,
                    )
                )
        if cases:
            ask = await self.db.update_ask_projection(
                ask["id"],
                {
                    "context_state": "quarantined",
                    "display_version": ask["display_version"]
                    + int(ask["context_state"] != "quarantined"),
                    "next_actor": "agent",
                    "context": {**ask["context"], "choices": []},
                    "updated_at": now,
                },
                conn=conn,
            )
        return Publication(ask, created, tuple(case["id"] for case in cases))

    async def _conflict(self, ask: dict, key: str, evidence: dict, *, conn) -> dict:
        now = self.clock()
        return await self.db.insert_ask_reconciliation_case(
            {
                "id": new_id("ask-repair"),
                "project_id": ask["project_id"],
                "conflict_key": f"identity:{ask['id']}:{digest(key)}",
                "source_evidence": evidence,
                "affected_aliases": [key],
                "logical_owner": ask["logical_owner"],
                "reason": "Canonical source/alias disagreement; automatic coalescing refused",
                "created_at": now,
                "updated_at": now,
            },
            conn=conn,
        )

    async def _packet(self, source, actor, intent, *, policy, conn, snapshot=None):
        adapter = self.adapters.get(source.kind, policy)
        if snapshot is None:
            snapshot = await adapter.snapshot(source, conn=conn, lock=True)
        if snapshot.identity != source or adapter.choices(snapshot) != snapshot.choices:
            raise AskError("ask.stale_source", "Adapter source/choices mismatch")
        domain_packet = await adapter.validate(snapshot, actor, intent, conn=conn)
        publication = await self.publish_snapshot(snapshot, conn=conn)
        ask = publication.ask
        check_version(ask, intent.expected)
        if intent.option_key not in {choice.key for choice in snapshot.choices}:
            raise AskError("ask.stale_source", "Choice is not declared by this source")
        packet = {
            "source": asdict(source),
            "version": asdict(intent.expected),
            "adapter": source.kind,
            "policy": policy,
            "adapter_version": adapter.version,
            "actor": asdict(actor),
            "intent": asdict(intent),
            "context": ask["context"],
            "domain": domain_packet,
        }
        return ask, packet

    async def prepare_decision(
        self,
        source: SourceIdentity,
        actor: DecisionActor,
        intent: DecisionIntent,
        *,
        conn,
        policy: str = "",
    ) -> dict:
        ask, packet = await self._packet(source, actor, intent, policy=policy, conn=conn)
        now = self.clock()
        return await self.db.insert_ask_preview(
            {
                "id": new_id("ask-preview"),
                "ask_id": ask["id"],
                "decision_generation": ask["decision_generation"],
                "actor_id": actor.principal_id,
                "input_id": actor.input_id,
                "source_fingerprint": ask["source_fingerprint"],
                "feedback_version": ask["feedback_version"],
                "packet": packet,
                "packet_sha256": digest(packet),
                "created_at": now,
                "expires_at": now + PREVIEW_TTL_SECONDS,
            },
            conn=conn,
        )

    async def reserve_decision(
        self,
        source: SourceIdentity,
        actor: DecisionActor,
        intent: DecisionIntent,
        *,
        preview_id: str,
        idempotency_key: str,
        evidence_entry_id: str,
        conn,
        policy: str = "",
    ) -> tuple[dict, bool]:
        """Freeze a revalidated packet and its apply operation, without applying it.

        Domain coordinators must add obligations and their source pending fence in
        this SAME transaction before commit. H02 owns review finalization. This
        primitive is internal and deliberately not exposed as a decide command.
        """
        request_hash = digest(
            {
                "intent": asdict(intent),
                "preview_id": preview_id,
                "evidence_entry_id": evidence_entry_id,
            }
        )
        adapter = self.adapters.get(source.kind, policy)
        snapshot = await adapter.snapshot(source, conn=conn, lock=True)
        if snapshot.identity != source:
            raise AskError("ask.stale_source", "Adapter changed canonical identity")
        ask = await self.db.get_ask_by_source(
            source.project_id, source.kind, source.key, conn=conn, lock=True
        )
        existing = (
            await self.db.get_ask_decision_by_request(
                ask["id"],
                actor.principal_id,
                idempotency_key,
                conn=conn,
            )
            if ask
            else None
        )
        if existing:
            await adapter.validate(snapshot, actor, intent, conn=conn, accepted_decision=existing)
            if existing["request_sha256"] != request_hash:
                raise AskError("ask.idempotency_conflict", "Request key has a different payload")
            return existing, False
        ask, packet = await self._packet(
            source, actor, intent, policy=policy, conn=conn, snapshot=snapshot
        )
        preview = await self.db.get_ask_preview(preview_id, conn=conn)
        now = self.clock()
        if (
            not preview
            or preview["ask_id"] != ask["id"]
            or preview["actor_id"] != actor.principal_id
            or preview["input_id"] != actor.input_id
            or preview["consumed_at"] is not None
            or preview["expires_at"] <= now
            or preview["packet_sha256"] != digest(packet)
        ):
            raise AskError(
                "ask.stale_preview", "Preview is expired, changed or belongs to another input"
            )
        domain = packet["domain"]
        decision, created = await self.db.insert_ask_decision(
            {
                "id": new_id("ask-decision"),
                "ask_id": ask["id"],
                "decision_generation": ask["decision_generation"],
                "option_key": intent.option_key,
                "adapter": packet["adapter"],
                "adapter_version": packet["adapter_version"],
                "actor_id": actor.principal_id,
                "actor": asdict(actor),
                "evidence_entry_id": evidence_entry_id,
                "idempotency_key": idempotency_key,
                "request_sha256": request_hash,
                "source_fingerprint": ask["source_fingerprint"],
                "feedback_version": ask["feedback_version"],
                "overall_note": intent.overall_note,
                "accepted_dispositions": list(intent.dispositions),
                "packet": packet,
                "packet_sha256": digest(packet),
                "created_at": now,
                **{
                    key: domain[key]
                    for key in (
                        "review_id",
                        "review_revision",
                        "content_sha256",
                        "attachment_manifest_sha256",
                        "snapshot_id",
                    )
                    if key in domain
                },
            },
            conn=conn,
        )
        if not await self.db.consume_ask_preview(preview_id, now=now, conn=conn):
            raise AskError("ask.stale_preview", "Preview was already consumed")
        await self.db.insert_ask_operation(
            {
                "id": new_id("ask-op"),
                "ask_id": ask["id"],
                "decision_id": decision["id"],
                "reply_id": evidence_entry_id,
                "idempotency_key": f"apply:{decision['id']}",
                "action_kind": "ask_operation",
                "target_id": source.key,
                "operation_kind": "apply",
                "operation_key": f"apply:{decision['id']}",
                "dependency_keys": [],
                "parameters": {"packet_sha256": decision["packet_sha256"]},
                "executor": ask["logical_owner"],
                "started_revision": ask["decision_generation"],
                "status": "pending",
                "created_at": now,
            },
            conn=conn,
        )
        await self.db.update_ask_projection(
            ask["id"],
            {
                "application_state": "applying",
                "next_actor": "agent",
                "updated_at": now,
            },
            conn=conn,
        )
        return decision, created
