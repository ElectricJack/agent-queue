"""CommandHandler-owned review decision boundary. No caller-authored authority fields."""

from dataclasses import asdict

from src.asks.adapters import AdapterRegistry
from src.asks.models import AskError, DecisionIntent, SourceIdentity, SourceVersion, digest
from src.asks.operations import ReviewOperations
from src.asks.review import ReviewAdapter, authenticated_actor
from src.asks.service import AskService
from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal


class AskCommandsMixin:
    def _ask_service(self):
        return AskService(self.db, AdapterRegistry([ReviewAdapter(self._review_service())]))

    async def _ask_source_for_caller(self, ask_id, *, conn):
        ask = await self.db.get_ask(str(ask_id), conn=conn)
        principal = current_principal() or TRUSTED_LOCAL
        if not ask or (principal.project_id and principal.project_id != ask["project_id"]):
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

    async def _cmd_ask_prepare_decision(self, args):
        try:
            intent, actor = self._ask_intent(args), authenticated_actor()
            async with self.db.immediate() as conn:
                _, source = await self._ask_source_for_caller(args.get("ask_id"), conn=conn)
                preview = await self._ask_service().prepare_decision(
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
                # Authorize before inserting any evidence, while holding source before ask.
                service = self._ask_service()
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
            result = await ReviewOperations(self._review_service()).apply(decision["id"])
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
        result = await ReviewOperations(self._review_service()).apply(decision["id"])
        return {"success": result["application_state"] == "applied", **result}

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
