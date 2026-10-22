"""Explicit project fresh start: discard legacy state, publish only open human reviews."""

import json
import time

from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert

from src.asks.delivery import config_fingerprint, destination
from src.asks.models import AskError, SourceIdentity, digest
from src.asks.reconcile import diagnostics
from src.asks.reset_policy import reset_key, reset_state
from src.asks.service import new_id
from src.commands.principal import TRUSTED_LOCAL, current_principal
from src.database import tables as t


def selections(project):
    asks = select(t.human_asks.c.id).where(t.human_asks.c.project_id == project)
    incidents = select(t.escalations.c.id).where(t.escalations.c.project_id == project)
    return {
        t.doc_reviews: t.doc_reviews.c.project_id == project,
        t.human_asks: t.human_asks.c.project_id == project,
        t.escalations: t.escalations.c.project_id == project,
        t.escalation_messages: (
            t.escalation_messages.c.escalation_id.in_(incidents)
            | t.escalation_messages.c.ask_id.in_(asks)
        ),
        t.escalation_actions: (
            t.escalation_actions.c.escalation_id.in_(incidents)
            | t.escalation_actions.c.ask_id.in_(asks)
        ),
        t.escalation_deliveries: t.escalation_deliveries.c.escalation_id.in_(incidents),
        t.conversation_inputs: (t.conversation_inputs.c.project_id == project)
        & (
            (t.conversation_inputs.c.ask_id.in_(asks))
            | (t.conversation_inputs.c.source == "legacy")
        ),
        t.ask_source_aliases: t.ask_source_aliases.c.project_id == project,
        t.ask_reconciliation_cases: t.ask_reconciliation_cases.c.project_id == project,
        t.ask_external_bindings: t.ask_external_bindings.c.project_id == project,
        t.ask_action_tokens: t.ask_action_tokens.c.ask_id.in_(asks),
        t.ask_decision_previews: t.ask_decision_previews.c.ask_id.in_(asks),
        t.outbound_deliveries: (t.outbound_deliveries.c.project_id == project)
        & (t.outbound_deliveries.c.owner_kind == "ask"),
    }


async def discard(handler, conn, project, selected, rows, mode, stamp, evidence):
    kept = [
        r["id"]
        for r in rows["doc_reviews"]
        if r["state"] == "in_review"
        and r["decider"] in {"user", "user_or_supervisor"}
        and not r["pending_decision_id"]
    ]
    obsolete = {"legacy_discarded": True, "reset_sha256": evidence}
    # Immutable decision packets remain obsolete behind their discarded ask. No
    # approval/application is rewritten and no historical payload is copied to an audit.
    for row in rows["human_asks"]:
        values = dict(
            context=obsolete,
            context_state="quarantined",
            next_actor="none",
            decision_state="superseded",
            application_state="not_started",
            display_version=row["display_version"] + 1,
            updated_at=stamp,
        )
        if row["source_kind"] == "review" and row["source_key"] in kept:
            values["source_key"] = f"discarded:{row['id']}"
        await conn.execute(
            update(t.human_asks).where(t.human_asks.c.id == row["id"]).values(**values)
        )
    await conn.execute(
        update(t.escalations)
        .where(selected[t.escalations])
        .values(
            state="cancelled",
            terminal_at=stamp,
            terminal_outcome="legacy_discarded",
            terminal_evidence=obsolete,
            updated_at=stamp,
        )
    )
    await conn.execute(
        update(t.escalation_actions)
        .where(selected[t.escalation_actions])
        .values(
            status="cancelled",
            outcome="legacy_discarded",
            completed_at=stamp,
            lease_owner=None,
            lease_expires_at=None,
            retry_at=None,
        )
    )
    await conn.execute(
        update(t.conversation_inputs)
        .where(selected[t.conversation_inputs])
        .values(
            state="revoked",
            routing_state="quarantined",
            routing_evidence=obsolete,
        )
    )
    await conn.execute(
        update(t.outbound_deliveries)
        .where(selected[t.outbound_deliveries])
        .values(
            state="cancelled",
            lease_owner=None,
            lease_expires_at=None,
            attempt_token=None,
            updated_at=stamp,
        )
    )
    await conn.execute(
        update(t.ask_external_bindings)
        .where(selected[t.ask_external_bindings])
        .values(
            active=False,
            root_state="retired",
            inflight_id=None,
            updated_at=stamp,
            root_generation=t.ask_external_bindings.c.root_generation + 1,
        )
    )
    for table in (
        t.escalation_deliveries,
        t.ask_action_tokens,
        t.ask_decision_previews,
        t.ask_source_aliases,
        t.ask_reconciliation_cases,
    ):
        await conn.execute(delete(table).where(selected[table]))
    state = {"cutoff": stamp, "kept_reviews": kept, "evidence_sha256": evidence}
    await conn.execute(
        insert(t.system_config)
        .values(
            key=reset_key(project),
            value=json.dumps(state),
        )
        .on_conflict_do_update(index_elements=["key"], set_={"value": json.dumps(state)})
    )
    fresh = []
    for review in kept:
        ask = (
            await handler._ask_service().publish(
                SourceIdentity(project, "review", review, {"id": review}),
                conn=conn,
            )
        ).ask
        if ask["decision_state"] != "pending" or ask["next_actor"] != "human":
            raise AskError("ask.reset_review_changed", "Open review is no longer a human request")
        fresh.append({"review_id": review, "ask_id": ask["id"]})
        await handler.db.insert_ask_binding(
            {
                "id": new_id("abinding"),
                "ask_id": ask["id"],
                "project_id": project,
                "destination": mode["destination"],
                "transport": "discord",
                "guild_id": str(handler.config.discord.guild_id),
                "channel_id": str(handler.config.discord.channel_id),
                "mode_generation": mode["generation"],
                "root_generation": 1,
                "root_state": "pending",
                "active": True,
                "receipt": {"fresh_start": evidence},
                "desired_display_version": ask["display_version"],
                "applied_display_version": 0,
                "created_at": stamp,
                "updated_at": stamp,
            },
            conn=conn,
        )
    # The reset examined every selected row; no legacy backfill cycle is required.
    cursor = json.dumps({"phase": 0, "after": "", "cycle": 1})
    await conn.execute(
        insert(t.system_config)
        .values(
            key=f"ask-reconcile:v1:{project}",
            value=cursor,
        )
        .on_conflict_do_update(index_elements=["key"], set_={"value": cursor})
    )
    return state, fresh


async def reset(handler, args):
    project = args["project_id"]
    if args["apply"] and not args.get("evidence_sha256"):
        raise AskError("ask.evidence_required", "Preview and supply its evidence hash")
    selected = selections(project)
    async with handler.db._engine.connect() as conn:
        transaction = await conn.begin()
        try:
            # Short maintenance transaction. Table locks prevent phantoms and stale
            # plans while ordinary source/ask writers finish before the snapshot.
            names = sorted(table.name for table in selected)
            names += ["human_communication_modes", "system_config"]
            await conn.execute(
                text("LOCK TABLE " + ", ".join(names) + " IN SHARE ROW EXCLUSIVE MODE")
            )
            previous = await reset_state(conn, project)
            if (
                args["apply"]
                and previous
                and previous["evidence_sha256"] == args["evidence_sha256"]
            ):
                await transaction.rollback()
                return {"success": True, "result": {**previous["result"], "replayed": True}}
            mode = await handler.db.get_ask_mode(project, destination(handler.config), conn=conn)
            if not mode or not mode["migrated"]:
                raise AskError("ask.reset_unfenced", "Enter shadow before previewing the reset")
            if args["apply"] and mode["mode"] not in {"transferring", "delivery_disabled"}:
                raise AskError("ask.reset_unfenced", "Fence writers and preview again before apply")
            rows, total = {}, 0
            for table, where in selected.items():
                values = [
                    dict(r)
                    for r in (
                        await conn.execute(
                            select(table)
                            .where(where)
                            .order_by(*table.primary_key.columns)
                            .limit(args["max_rows"] - total + 1)
                        )
                    ).mappings()
                ]
                total += len(values)
                if total > args["max_rows"]:
                    raise AskError(
                        "ask.reset_bound", "Inventory exceeds max_rows; nothing was discarded"
                    )
                rows[table.name] = values
            evidence = digest(
                {
                    "project_id": project,
                    "rows": rows,
                    "mode": mode,
                    "previous": previous,
                    "config": config_fingerprint(handler.config),
                }
            )
            if args["apply"] and evidence != args["evidence_sha256"]:
                raise AskError("ask.stale_evidence", "Reset inventory changed; preview again")
            before = await diagnostics(
                conn, project_id=project, destination_id=mode["destination"], limit=500
            )
            state, fresh = await discard(
                handler, conn, project, selected, rows, mode, time.time(), evidence
            )
            after = await diagnostics(
                conn, project_id=project, destination_id=mode["destination"], limit=500
            )
            remaining = {k: v for k, v in after.items() if k != "transfers"}
            if remaining:
                raise AskError(
                    "ask.reset_blocked",
                    "Reset still has blockers; nothing was discarded",
                    fields=remaining,
                )
            result = {
                "dry_run": not args["apply"],
                "project_id": project,
                "evidence_sha256": evidence,
                "counts": {k: len(v) for k, v in rows.items()},
                "examined": total,
                "max_rows": args["max_rows"],
                "complete": True,
                "before": before,
                "projected": after,
                "zero_blockers": True,
                "kept_reviews": [r["review_id"] for r in fresh],
                "fresh_requests": fresh if args["apply"] else [],
                "mode": mode["mode"],
                "generation": mode["generation"],
                "replayed": False,
            }
            if args["apply"]:
                # One summary receipt: counts, hash, actor and current request IDs.
                summary = {k: v for k, v in result.items() if k != "before"}
                state.update(
                    result=summary, principal=(current_principal() or TRUSTED_LOCAL).describe()
                )
                await conn.execute(
                    update(t.system_config)
                    .where(
                        t.system_config.c.key == reset_key(project),
                    )
                    .values(value=json.dumps(state))
                )
                await transaction.commit()
            else:
                await transaction.rollback()
            return {"success": True, "result": result}
        except BaseException:
            if transaction.is_active:
                await transaction.rollback()
            raise
