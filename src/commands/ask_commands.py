"""CommandHandler-owned human decision boundary. No caller-authored authority fields."""

from dataclasses import asdict
from contextlib import asynccontextmanager

from src.asks.adapters import AdapterRegistry
from src.asks.models import AskError, DecisionIntent, SourceIdentity, SourceVersion, digest
from src.asks.operations import ReviewOperations
from src.asks.review import ReviewAdapter, authenticated_actor
from src.asks.service import AskService
from src.asks.standalone import StandaloneSpecAdapter
from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal


from src.commands.ask_communication_commands import AskCommunicationCommandsMixin


class AskCommandsMixin(AskCommunicationCommandsMixin):
    def _ask_service(self):
        from src.asks.sources import source_adapters
        return AskService(self.db, AdapterRegistry([
            ReviewAdapter(self._review_service()), StandaloneSpecAdapter(self.db),
            *source_adapters(self),
        ]))

    async def _apply_ask_decision(self, decision):
        async with self.db._engine.connect() as conn:
            ask = await self.db.get_ask(decision["ask_id"], conn=conn)
        if ask and ask["context"].get("legacy_discarded"):
            raise AskError("ask.discarded", "This decision belongs to discarded legacy work")
        if decision["packet"]["source"]["kind"] not in {"review", "standalone_spec"}:
            from src.asks.source_operations import apply
            try:
                result = await apply(self, decision)
            except AskError as exc:
                # The immutable decision may already exist when a current
                # source/authority check refuses application. Keep its identity
                # and actual durable state in the response for reconciliation.
                async with self.db.immediate() as conn:
                    ask = await self.db.get_ask(decision["ask_id"], conn=conn)
                return {"decision_id": decision["id"],
                        "application_state": ask["application_state"],
                        "error_code": exc.code, "error": str(exc), "fields": exc.fields}
            if result["application_state"] == "applied":
                await self._finish_ask_escalation(decision, result)
            return result
        if not decision["snapshot_id"]:
            return await ReviewOperations(self._review_service()).apply(decision["id"])
        from src.asks.ingestion import dispatch
        receipt = await dispatch(self.db, decision, self.orchestrator.bus.emit,
                                 self.config.vault_root)
        return {"decision_id": decision["id"], "application_state": "applied",
                "ingestion_state": "acknowledged" if (receipt or {}).get("task_id") else "pending"}

    async def _publish_source_application(self, decision, result):
        """Best-effort wakeups after the domain receipt has committed."""
        import logging
        source = decision["packet"]["source"]
        if (source["kind"] not in {"gate", "proposal"}
                or result["application_state"] != "applied"):
            return
        if decision["option_key"] == "decline":
            if source["kind"] == "proposal":
                await self._emit_proposal_event("proposal.status_changed", {
                    "project_id": source["project_id"], "proposal_id": source["key"],
                    "status": "discarded",
                })
            return
        receipt = result["receipt"]
        try:
            gate = await self.db.get_gate(receipt["gate_id"])
            await self.db.log_blocked_flips(set(receipt.get("flipped", [])))
            await self.db._notify_ready([(tid, "unblocked") for tid in receipt.get("ready_task_ids", [])])
            await self.orchestrator.bus.emit("gate.resolved", {
                "gate_id": gate["id"], "project_id": source["project_id"],
                "resolved_by": decision["actor_id"], "resolution": decision["option_key"],
                "gate_type": "human", "await_id": gate["await_id"],
                "unblocked_task_ids": receipt.get("flipped", []), "decision_id": decision["id"],
            })
            await self.orchestrator._emit_blocked_flips(set(receipt.get("flipped", [])),
                                                      reason="gate:resolved")
        except Exception:
            logging.getLogger(__name__).exception("Source receipt committed; notification failed")

    async def _finish_ask_escalation(self, decision, result):
        """Project a confirmed source receipt without closing over a newer human reply."""
        actor = decision["packet"]["actor"]
        escalation_id = actor["provenance"].get("escalation_id")
        if not escalation_id:
            return
        current = await self.db.get_escalation(escalation_id)
        revision = decision["packet"]["domain"]["escalation_revision"]
        if current["state"] == "reply_received" and current["revision"] == revision:
            current = await self.db.transition_escalation(escalation_id,
                expected_revision=revision, new_state="resolving") or current
        if current["state"] == "resolving" and current["revision"] == revision + 1:
            current = await self.db.transition_escalation(escalation_id,
                expected_revision=revision + 1, new_state="resolved", outcome="human",
                terminal_outcome=f"Applied {decision['option_key']} to the bound source.",
                terminal_evidence={"decision_id": decision["id"], "receipt": result["receipt"]},
            ) or current
        result["escalation"] = current

    async def _ask_source_for_caller(self, ask_id, *, conn):
        ask = await self.db.get_ask(str(ask_id), conn=conn)
        principal = current_principal() or TRUSTED_LOCAL
        from src.asks.authority import visible
        if not ask or not visible(ask):
            raise AskError("ask.not_found", "Request was not found")
        if principal.kind is PrincipalKind.SESSION and not principal.elevated:
            raise AskError("ask.not_decider", "Workers cannot decide human requests")
        return ask, SourceIdentity(
            ask["project_id"],
            ask["source_kind"],
            ask["source_key"],
            ask["source_identity"],
        )

    @staticmethod
    def _ask_decision_args(args):
        # Scope middleware adds authenticated routing metadata. It is not decision
        # input and must match the principal before being discarded.
        args = dict(args)
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.SESSION:
            for key in ("project_id", "session_id", "task_id"):
                if key in args:
                    if args[key] != getattr(principal, key):
                        raise AskError("ask.not_decider", "Decision scope differs from principal")
                    args.pop(key)
        return args

    def _ask_intent(self, args):
        args = self._ask_decision_args(args)
        allowed = {
            "ask_id",
            "expected",
            "option_key",
            "overall_note",
            "dispositions",
            "preview_id",
            "idempotency_key",
            "responder_class",
        }
        if set(args) - allowed:
            raise AskError(
                "ask.not_decider", "Unexpected decision fields; authority is server-owned"
            )
        try:
            expected = args["expected"]
            if not isinstance(expected, dict) or set(expected) != {
                "decision_generation",
                "source_fingerprint",
                "feedback_version",
            }:
                raise ValueError()
            if any(
                type(expected[k]) is not int for k in ("decision_generation", "feedback_version")
            ):
                raise ValueError()
            if (
                not isinstance(args.get("option_key"), str)
                or not isinstance(args.get("overall_note", ""), str)
                or not isinstance(expected["source_fingerprint"], str)
            ):
                raise ValueError()
            dispositions = args.get("dispositions", [])
            if not isinstance(dispositions, list) or any(
                not isinstance(i, dict) for i in dispositions
            ):
                raise ValueError()
            responder_class = args.get("responder_class")
            if responder_class is not None:
                from src.intelligence_classes import load_intelligence_classes

                if (
                    not isinstance(responder_class, str)
                    or responder_class not in load_intelligence_classes(self.config.data_dir)
                    or self._validate_routing_class(responder_class)
                ):
                    raise AskError(
                        "invalid_responder_class", "Unknown or unavailable routing class"
                    )
            return DecisionIntent(
                args["option_key"],
                SourceVersion(**expected),
                args.get("overall_note", ""),
                tuple(dispositions),
                args.get("responder_class"),
            )
        except (KeyError, TypeError, ValueError):
            raise AskError(
                "ask.context_missing", "A complete expected version and decision are required"
            ) from None

    @asynccontextmanager
    async def _ask_transaction(self, source, service):
        if source.kind == "proposal":
            from src.commands.task_changes import boundary
            async def lock(conn):
                await service.adapters.get(source.kind).snapshot(source, conn=conn)
            async with boundary(self.db, source.project_id, source_lock=lock) as conn:
                yield conn
        else:
            async with self.db.immediate() as conn:
                yield conn

    async def _publish_source_ask(self, source):
        service = self._ask_service()
        async with self._ask_transaction(source, service) as conn:
            result = await service.publish(source, legacy=True, conn=conn)
        return result.ask

    async def _legacy_ask_decision(self, source, args, *, option_key):
        ask = await self._publish_source_ask(source)
        if not args.get("expected") or not args.get("preview_id"):
            return {"success": False, "error_code": "ask.context_missing",
                    "error": "Run aq ask show, then aq ask prepare with the returned expected version; "
                             "pass its preview_id and the same intent to aq ask decide",
                    "ask_id": ask["id"], "ask_url": f"/focus/asks/{ask['id']}",
                    "expected": {k: ask[k] for k in (
                        "decision_generation", "source_fingerprint", "feedback_version")}}
        return await self._cmd_ask_decide({
            "ask_id": ask["id"], "expected": args["expected"], "option_key": option_key,
            "overall_note": args.get("overall_note", ""), "preview_id": args["preview_id"],
            "idempotency_key": args.get("idempotency_key"),
        })

    async def _cmd_ask_prepare_decision(self, args):
        try:
            intent, actor = self._ask_intent(args), authenticated_actor()
            service = self._ask_service()
            async with self.db.immediate() as conn:
                _, source = await self._ask_source_for_caller(args.get("ask_id"), conn=conn)
            async with self._ask_transaction(source, service) as conn:
                preview = await service.prepare_decision(
                    source, actor, intent, conn=conn
                )
            return {"success": True, "preview": preview, "expected": asdict(intent.expected)}
        except AskError as exc:
            return {
                "success": False,
                "error_code": exc.code,
                "error": str(exc),
                "fields": exc.fields,
            }

    async def _cmd_ask_decide(self, args):
        try:
            intent, actor = self._ask_intent(args), authenticated_actor()
            key = args.get("idempotency_key")
            if not isinstance(key, str) or not 1 <= len(key) <= 512:
                raise AskError(
                    "ask.context_missing", "A bounded request idempotency key is required"
                )
            async with self.db.immediate() as conn:
                ask, source = await self._ask_source_for_caller(args.get("ask_id"), conn=conn)
            service = self._ask_service()
            async with self._ask_transaction(source, service) as conn:
                # Authorize before inserting any evidence, while holding source before ask.
                adapter = service.adapters.get(source.kind)
                snap = await adapter.snapshot(source, conn=conn)
                existing = await self.db.get_ask_decision_by_request(
                    ask["id"], actor.principal_id, key, conn=conn
                )
                await adapter.validate(snap, actor, intent, conn=conn, accepted_decision=existing)
                evidence_id = f"ask-input-{digest([ask['id'], actor.principal_id, key])}"
                await self.db.insert_ask_correspondence(
                    {
                        "id": evidence_id,
                        "ask_id": ask["id"],
                        "direction": "inbound",
                        "transport": "local",
                        "verified_actor": actor.principal_id,
                        "text": intent.overall_note or intent.option_key,
                        "entry_kind": "decision_evidence",
                        "received_at": service.clock(),
                        "created_at": service.clock(),
                    },
                    conn=conn,
                )
                decision, created = await service.decide(
                    source,
                    actor,
                    intent,
                    conn=conn,
                    preview_id=args.get("preview_id"),
                    idempotency_key=key,
                    evidence_entry_id=evidence_id,
                )
            if source.kind == "proposal":
                notifications = getattr(service.adapters.get("proposal"), "notifications", None)
                if notifications:
                    await self._publish_committed(*notifications)
            result = await self._apply_ask_decision(decision)
            if created:
                await self._publish_source_application(decision, result)
            return {
                "success": result["application_state"] == "applied",
                "created": created,
                **result,
            }
        except AskError as exc:
            return {
                "success": False,
                "error_code": exc.code,
                "error": str(exc),
                "fields": exc.fields,
            }

    async def _cmd_ask_retry_operation(self, args):
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind not in {PrincipalKind.LOCAL, PrincipalKind.SERVICE}:
            return {
                "success": False,
                "error_code": "ask.not_decider",
                "error": "Operator repair only",
            }
        async with self.db.immediate() as conn:
            decision = await self.db.get_ask_decision(str(args.get("decision_id")), conn=conn)
        if not decision:
            return {"success": False, "error_code": "ask.not_found", "error": "Decision not found"}
        try:
            result = await self._apply_ask_decision(decision)
            return {"success": result["application_state"] == "applied", **result}
        except AskError as exc:
            return {"success": False, "error_code": exc.code, "error": str(exc),
                    "fields": exc.fields}

    async def _cmd_review_feedback_complete(self, args):
        """Record evidence for one filed obligation; this never approves a review."""
        import time
        from sqlalchemy import select
        from src.database.tables import review_feedback_obligations

        principal = current_principal() or TRUSTED_LOCAL
        task_id = str(args.get("task_id") or "")
        evidence = args.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            return {
                "success": False,
                "error_code": "feedback.evidence_required",
                "error": "Describe the change or verification for this individual item",
            }
        if self._is_worker():
            held, error = await self._worker_held_task()
            if error:
                return error
            if held.id != task_id:
                return {"success": False, "error_code": "ask.not_found", "error": "Item not found"}
        elif principal.kind is not PrincipalKind.LOCAL:
            return {
                "success": False,
                "error_code": "ask.not_decider",
                "error": "The held worker or local operator records completion evidence",
            }
        async with self.db.immediate() as conn:
            # Read identity without a lock, then acquire the source before any obligation.
            item = (
                (
                    await conn.execute(
                        select(review_feedback_obligations).where(
                            review_feedback_obligations.c.id
                            == str(args.get("obligation_id") or ""),
                            review_feedback_obligations.c.task_id == task_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if not item:
                return {"success": False, "error_code": "ask.not_found", "error": "Item not found"}
            decision = await self.db.get_ask_decision(item["decision_id"], conn=conn)
            await ReviewOperations(self._review_service()).locked(decision, conn)
            proof = {"text": evidence, "by": principal.describe(), "task_id": task_id}
            changed = await self.db.update_review_feedback_progress(
                item["id"],
                progress="completed",
                evidence=proof,
                now=time.time(),
                conn=conn,
            )
        return {"success": True, "obligation_id": item["id"], "recorded": changed}

    async def _review_feedback_close_error(self, task_id):
        from sqlalchemy import select
        from src.database.tables import review_feedback_obligations

        async with self.db._engine.connect() as conn:
            pending = list(
                (
                    await conn.execute(
                        select(review_feedback_obligations.c.id)
                        .where(
                            review_feedback_obligations.c.task_id == task_id,
                            review_feedback_obligations.c.progress != "completed",
                        )
                        .order_by(review_feedback_obligations.c.id)
                    )
                ).scalars()
            )
        if pending:
            return {
                "success": False,
                "code": "feedback.incomplete",
                "obligation_ids": pending,
                "error": "Record per-item evidence with review_feedback_complete before passing close",
            }
        return None
