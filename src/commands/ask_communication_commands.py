"""Scoped human communication commands; transport and model text confer no authority."""

import logging
import time
from dataclasses import asdict
from functools import wraps

from pydantic import ValidationError
from sqlalchemy import select

from src.api.models.ask import ASK_ARGS
from src.asks import discussion, intake
from src.asks.authority import live_supervisor, visible
from src.asks.models import AskError, SourceVersion, digest
from src.asks.service import check_version, version_of
from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal
from src.database.tables import conversation_inputs, escalation_messages, human_asks


def checked(name):
    def decorate(method):
        @wraps(method)
        async def invoke(self, args):
            try:
                project = args.get("project_id") if name == "ask_list" else None
                routed = (
                    {k: v for k, v in args.items() if k != "project_id"}
                    if name == "ask_list"
                    else args
                )
                clean = self._ask_decision_args(routed)
                if project is not None:
                    principal = current_principal() or TRUSTED_LOCAL
                    if principal.project_id and principal.project_id != project:
                        raise AskError("ask.not_found", "Project was not found")
                    clean["project_id"] = project
                values = ASK_ARGS[name].model_validate(clean)
                return await method(self, values.model_dump(exclude_none=True))
            except AskError as exc:
                return {
                    "success": False,
                    "error_code": exc.code,
                    "error": str(exc),
                    "fields": exc.fields,
                }
            except ValidationError:
                return {
                    "success": False,
                    "error_code": "ask.invalid_input",
                    "error": "Arguments do not match the closed ask command schema",
                }

        return invoke

    return decorate


def operator_only():
    principal = current_principal() or TRUSTED_LOCAL
    if principal.kind not in {PrincipalKind.LOCAL, PrincipalKind.SERVICE}:
        raise AskError("ask.operator_only", "This control requires the local operator or daemon")


class AskCommunicationCommandsMixin:
    async def _run_ask_delivery_cycle(self, scheduler, *, limit):
        from src.commands.principal import ExecutionPrincipal, principal_context
        with principal_context(ExecutionPrincipal.service("ask-delivery")):
            return await scheduler.pump(limit=limit)

    @checked("ask_intake")
    async def _cmd_ask_intake(self, args):
        from src.api.models.ask import AskEnvelope

        return await intake.admit_gateway(
            self, AskEnvelope(**args["envelope"]), source=args["source"]
        )

    @checked("ask_reply")
    async def _cmd_ask_reply(self, args):
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is not PrincipalKind.LOCAL:
            raise AskError("ask.human_only", "Only an authenticated human starts human discussion")
        if not args["text"].strip():
            raise AskError("ask.invalid_input", "Discussion text is required")
        async with self.db.immediate() as conn:
            ask = await discussion.locked_ask(self, args["ask_id"], conn=conn)
            values = intake.input_values(
                transport="local",
                external_id=digest([principal.describe(), args["idempotency_key"]]),
                actor="human:local-operator",
                author_id="local-operator",
                channel_id="local",
                text=args["text"],
                source="local",
                project_id=ask["project_id"],
            )
            values.update(ask_id=ask["id"], routing_evidence={"request": digest(args)})
            item, created, matches = await self.db.admit_ask_input(values, conn=conn)
            if (
                not matches
                or item["ask_id"] != ask["id"]
                or item["routing_evidence"] != values["routing_evidence"]
            ):
                return {
                    "success": False,
                    "error_code": "ask.input_conflict",
                    "error": "Retry identity already belongs to different input",
                }
            if created:
                check_version(ask, SourceVersion(**args["expected"]))
                if ask["decision_state"] != "pending" and not args["post_decision"]:
                    raise AskError(
                        "ask.post_decision", "Explicitly select post-decision discussion"
                    )
                item = await discussion.queue_discussion(self, ask, item, conn=conn)
        await self._emit_ask_event("ask.input_received", ask, input_id=item["id"])
        return {
            "success": True,
            "created": created,
            "input_id": item["id"],
            "receipt": item["receipt"],
        }

    @checked("ask_discussion_reply")
    async def _cmd_ask_discussion_reply(self, args):
        async with self.db.immediate() as conn:
            result = await discussion.reply(self, args, conn=conn)
            ask = await self.db.get_ask(args["ask_id"], conn=conn)
        await self._emit_ask_event("ask.discussion_replied", ask, input_id=args["input_id"])
        return result

    @checked("ask_apply_input")
    async def _cmd_ask_apply_input(self, args):
        return await intake.apply_input(self, args)

    @checked("ask_update_context")
    async def _cmd_ask_update_context(self, args):
        async with self.db.immediate() as conn:
            ask, source = await self._ask_source_for_caller(args["ask_id"], conn=conn)
            await live_supervisor(ask["project_id"], conn=conn)
            check_version(ask, SourceVersion(**args["expected"]))
            # Source adapters own context. A supervisor can refresh, not replace it.
            result = await self._ask_service().publish(source, legacy=True, conn=conn)
        return {"success": True, "result": {"ask": result.ask}}

    @checked("ask_show")
    async def _cmd_ask_show(self, args):
        async with self.db.immediate() as conn:
            ask = await discussion.locked_ask(self, args["ask_id"], conn=conn)
            ask['delivery'] = await self.db.ask_delivery_status(ask['id'], conn=conn)
            inputs = [
                dict(row)
                for row in (
                    await conn.execute(
                        select(conversation_inputs)
                        .where(
                            conversation_inputs.c.ask_id == ask["id"],
                        )
                        .order_by(conversation_inputs.c.created_at.desc())
                        .limit(100)
                    )
                ).mappings()
            ]
            for item in inputs:
                item["status"] = await discussion.discussion_status(self.db, item, conn=conn)
            pinned = next((i for i in inputs if i["id"] == args.get("input_id")), None)
            if args.get("input_id") and not pinned:
                pinned = await self.db.get_ask_input(args["input_id"], conn=conn)
                if not pinned or pinned["ask_id"] != ask["id"]:
                    raise AskError("ask.not_found", "Input was not found")
            document = None
            if ask["source_kind"] == "review":
                review = await self.db.get_review(ask["source_key"], conn=conn)
                revision = (pinned["source_version"].get("revision") if pinned else None) or review[
                    "current_revision"
                ]
                document = await self.db.get_review_revision(review["id"], revision, conn=conn)
                document = dict(document)
                if pinned:
                    document["attachments"] = pinned["source_version"]["attachments"]
            pending = ask["decision_state"] == "pending" and ask["context_state"] == "complete"
            actions = [
                {"action": "discuss", "enabled": True, "post_decision": not pending},
                {
                    "action": "prepare_decision",
                    "enabled": pending,
                    "disabled_reason": None if pending else "Read the current source receipt",
                },
            ]
        return {
            "success": True,
            "ask": ask,
            "expected": asdict(version_of(ask)),
            "inputs": inputs,
            "document": document,
            "actions": actions,
        }

    @checked("ask_history")
    async def _cmd_ask_history(self, args):
        async with self.db.immediate() as conn:
            await self._ask_source_for_caller(args["ask_id"], conn=conn)
            stmt = select(escalation_messages).where(escalation_messages.c.ask_id == args["ask_id"])
            if args.get("cursor"):
                cursor = await conn.scalar(
                    select(escalation_messages.c.created_at).where(
                        escalation_messages.c.id == args["cursor"],
                        escalation_messages.c.ask_id == args["ask_id"],
                    )
                )
                if cursor is None:
                    raise AskError("ask.invalid_cursor", "History cursor was not found")
                from sqlalchemy import tuple_

                stmt = stmt.where(
                    tuple_(escalation_messages.c.created_at, escalation_messages.c.id)
                    > (cursor, args["cursor"])
                )
            rows = [
                dict(r)
                for r in (
                    await conn.execute(
                        stmt.order_by(
                            escalation_messages.c.created_at,
                            escalation_messages.c.id,
                        ).limit(args["limit"] + 1)
                    )
                ).mappings()
            ]
        more = len(rows) > args["limit"]
        rows = rows[: args["limit"]]
        return {"success": True, "items": rows, "next_cursor": rows[-1]["id"] if more else None}

    @checked("ask_list")
    async def _cmd_ask_list(self, args):
        # H07 owns richer ordering and source-health presentation; this read uses
        # one snapshot and audience predicate for both count and page.
        async with self.db.immediate() as conn:
            stmt = select(human_asks).order_by(human_asks.c.created_at, human_asks.c.id)
            if args.get("project_id"):
                stmt = stmt.where(human_asks.c.project_id == args["project_id"])
            rows = [dict(r) for r in (await conn.execute(stmt)).mappings() if visible(r)]

            def matches(row):
                pending = row["decision_state"] == "pending"
                ready = (
                    pending and row["next_actor"] == "human" and row["context_state"] == "complete"
                )
                return (
                    ready
                    if args["view"] == "needs_you"
                    else pending and not ready
                    if args["view"] == "waiting"
                    else not pending
                )

            rows = [r for r in rows if matches(r)]
            count = len(rows)
            if args.get("cursor"):
                index = next((i for i, r in enumerate(rows) if r["id"] == args["cursor"]), None)
                if index is None:
                    raise AskError(
                        "ask.invalid_cursor", "List cursor is no longer available; refresh"
                    )
                rows = rows[index + 1 :]
            more = len(rows) > args["limit"]
            rows = rows[: args["limit"]]
        return {
            "success": True,
            "items": rows,
            "count": count,
            "snapshot_at": time.time(),
            "next_cursor": rows[-1]["id"] if more else None,
            "source_health": "refreshing",
        }

    @checked("ask_reconcile")
    async def _cmd_ask_reconcile(self, args):
        operator_only()
        if not args["apply"]:
            async with self.db.immediate() as conn:
                rows = await self.db.pending_ask_inputs(conn=conn, limit=args["limit"])
            return {"success": True, "result": {"dry_run": True, "pending": len(rows)}}
        async with self.db.immediate() as conn:
            imported = await intake.backfill_legacy(self, conn=conn, limit=args["limit"])
            rows = await self.db.pending_ask_inputs(conn=conn, limit=args["limit"])
        routed, delayed, failed = 0, 0, []
        for item in rows:
            try:
                if item["routing_state"] == "queued":
                    await intake.route(self, item)
                    routed += 1
                elif item["transport"] == "discord":
                    async with self.db.immediate() as conn:
                        status = await discussion.discussion_status(self.db, item, conn=conn)
                        if status["state"] == "delayed" and item["delay_notified_at"] is None:
                            ask = await self.db.get_ask(item["ask_id"], conn=conn)
                            await discussion.enqueue(
                                self,
                                item,
                                ask,
                                "Saved; the supervisor answer is delayed.",
                                key=f"ask-delay:{item['id']}",
                                conn=conn,
                            )
                            await self.db.update_ask_input(
                                item["id"], {"delay_notified_at": time.time()}, conn=conn
                            )
                            delayed += 1
            except Exception:
                logging.getLogger(__name__).exception("Input retry failed: %s", item["id"])
                failed.append(item["id"])
            # Rotate pending work so a failed consumer or an ongoing answer cannot
            # monopolize the bounded maintenance batch.
            async with self.db.immediate() as conn:
                current = await self.db.get_ask_input(item["id"], conn=conn, lock=True)
                if current and current["routing_state"] in {
                    "queued",
                    "discussion",
                    "interpretation",
                }:
                    await self.db.update_ask_input(
                        item["id"],
                        {
                            "routing_evidence": {
                                **current["routing_evidence"],
                                "last_route_attempt_at": time.time(),
                            }
                        },
                        conn=conn,
                    )
        return {
            "success": True,
            "result": {
                "imported": imported,
                "routed": routed,
                "delayed": delayed,
                "failed": failed,
            },
        }

    @checked("ask_cutover")
    async def _cmd_ask_cutover(self, args):
        operator_only()
        from src.asks.transport_controls import cutover
        return await cutover(self, args)

    @checked("ask_repair_delivery")
    async def _cmd_ask_repair_delivery(self, args):
        operator_only()
        from src.asks.transport_controls import repair
        return await repair(self, args)

    @checked("ask_button")
    async def _cmd_ask_button(self, args):
        from src.asks.buttons import decide
        return await decide(self, args)

    async def _emit_ask_event(self, name, ask, **fields):
        # Event payloads carry identities only; document reads remain scoped.
        import logging

        try:
            await self.orchestrator.bus.emit(
                name, {"ask_id": ask["id"], "project_id": ask["project_id"], **fields}
            )
        except Exception:
            logging.getLogger(__name__).exception("Ask persisted; notification failed")
