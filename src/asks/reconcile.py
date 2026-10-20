"""Bounded source reconciliation and operator evidence, never inferred decisions.

The source scan and its cursor commit together. The existing system_config store
holds only the versioned maintenance cursor; audit evidence lives in the ask
ledger. A dry run executes the same projections in a rolled-back transaction and
performs no transport or decision application. Every entry point is handler-owned.
"""

import json
import time
from dataclasses import asdict

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.asks.delivery import destination
from src.asks.models import AliasReference, AskError, SourceIdentity
from src.asks.service import new_id
from src.asks.reset_policy import before_reset, skip_row
from src.database.tables import (
    agent_questions,
    ask_decisions,
    ask_external_bindings as bindings,
    ask_reconciliation_cases as cases,
    ask_source_aliases as aliases,
    doc_review_comments,
    doc_reviews,
    escalation_actions as operations,
    escalation_deliveries as legacy_outbox,
    escalation_messages,
    escalations,
    gates,
    human_asks,
    human_communication_modes as modes,
    outbound_deliveries as outbox,
    playbook_v2_runs,
    system_config,
    task_proposals,
)

PHASES = (
    doc_reviews,
    gates,
    escalations,
    agent_questions,
    playbook_v2_runs,
    human_asks,
    escalation_messages,
    operations,
)


async def case(db, conn, project, key, reason, evidence, *, affected=()):
    return await db.insert_ask_reconciliation_case(
        dict(
            id=new_id("ask-repair"),
            project_id=project,
            conflict_key=key,
            source_evidence=evidence,
            affected_aliases=list(affected),
            logical_owner=f"supervisor:{project}",
            reason=reason,
            status="open",
            created_at=time.time(),
            updated_at=time.time(),
        ),
        conn=conn,
    )


async def gate_source(handler, row, conn):
    """A review's await_id and proposal's frozen row are authoritative bindings."""
    if row["gate_type"] == "review":
        review = (
            (
                await conn.execute(
                    select(doc_reviews).where(
                        doc_reviews.c.id == row["await_id"],
                        doc_reviews.c.project_id == row["project_id"],
                    )
                )
            )
            .mappings()
            .first()
        )
        if not review or review["gate_id"] != row["id"]:
            raise AskError("ask.alias_conflict", "Review gate await_id/back-reference disagree")
        return SourceIdentity(row["project_id"], "review", review["id"], {"id": review["id"]})
    if row["gate_type"] != "human" or str(row["await_id"] or "").startswith("ask-application:"):
        return None
    proposal = (
        (
            await conn.execute(
                select(task_proposals).where(
                    task_proposals.c.id == row["await_id"],
                    task_proposals.c.project_id == row["project_id"],
                )
            )
        )
        .mappings()
        .first()
    )
    if proposal:
        return SourceIdentity(row["project_id"], "proposal", proposal["id"], {"id": proposal["id"]})
    return await handler._gate_ask_source(row)


async def identity(handler, table, row, conn):
    project, key = row.get("project_id"), row.get("id")
    if table is doc_reviews:
        return SourceIdentity(project, "review", key, {"id": key})
    if table is gates:
        return await gate_source(handler, row, conn)
    if table is escalations:
        if row["source_kind"] == "gate":
            gate = (
                (
                    await conn.execute(
                        select(gates).where(
                            gates.c.id == row["source_identity"],
                            gates.c.project_id == project,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if not gate:
                raise AskError("ask.alias_conflict", "Explicit incident gate is missing")
            return await gate_source(handler, gate, conn)
        if row["source_kind"] == "question":
            return SourceIdentity(
                project, "question", row["source_identity"], {"id": row["source_identity"]}
            )
        if row["source_kind"] == "task_recovery":
            return SourceIdentity(
                project, "recovery", row["source_identity"], {"task_id": row["task_id"]}
            )
        return SourceIdentity(project, "free_text", key, {"id": key})
    if table is agent_questions:
        if not row["requires_human"]:
            return None
        return SourceIdentity(project, "question", key, {"id": key})
    if table in (escalation_messages, operations):
        incident = (
            (
                await conn.execute(
                    select(escalations).where(
                        escalations.c.id == row["escalation_id"],
                    )
                )
            )
            .mappings()
            .first()
        )
        return await identity(handler, escalations, dict(incident), conn) if incident else None
    if table is playbook_v2_runs:
        from src.playbooks.run_state import deserialize_snapshot

        snap = deserialize_snapshot(row["snapshot"], version=row["snapshot_version"])
        if not snap.wait or snap.wait.kind != "human" or not snap.event.get("project_id"):
            return None
        return SourceIdentity(
            snap.event["project_id"],
            "wait",
            snap.wait.wait_id,
            {"run_id": snap.run_id, "step_id": snap.wait.step_id},
        )
    return SourceIdentity(project, row["source_kind"], row["source_key"], row["source_identity"])


async def historical_feedback(db, review, conn):
    """Preserve predecision evidence, including author-resolved comments, for human audit."""
    if review["state"] != "approved":
        return []
    packet = await conn.scalar(
        select(ask_decisions.c.id).where(
            ask_decisions.c.review_id == review["id"],
            ask_decisions.c.review_revision == review["current_revision"],
        )
    )
    if packet:
        return []
    comments = [
        dict(r)
        for r in (
            await conn.execute(
                select(doc_review_comments)
                .where(
                    doc_review_comments.c.review_id == review["id"],
                    doc_review_comments.c.created_at
                    <= (review["decided_at"] or review["updated_at"]),
                )
                .order_by(doc_review_comments.c.created_seq)
                .limit(1001)
            )
        ).mappings()
    ]
    if not comments:
        return []
    evidence = {
        "review_id": review["id"],
        "revision": review["current_revision"],
        "decided_by": review["decided_by"],
        "decided_at": review["decided_at"],
        "author_task_id": review["author_task_id"],
        "title": review["title"],
        "comments": comments[:1000],
        "truncated": len(comments) > 1000,
        "downstream_work": "unverified; operator must record bounded audit",
    }
    report = await case(
        db,
        conn,
        review["project_id"],
        f"historical-feedback:{review['id']}:{review['current_revision']}",
        "Historical approval has comments without accepted feedback mapping",
        evidence,
    )
    return [report["id"]]


async def project_source(handler, table, row, conn):
    if await skip_row(conn, table, row, row.get("project_id")):
        return {"skipped": True, "discarded": True}
    source = await identity(handler, table, row, conn)
    if not source:
        return {"skipped": True}
    if await skip_row(conn, table, row, source.project_id):
        return {"skipped": True, "discarded": True}
    service = handler._ask_service()
    # Acquire the canonical source first; only then alias/incident/history rows.
    snap = await service.adapters.get(source.kind).snapshot(source, conn=conn)
    refs = [(source.kind, source.key)]
    if source.kind == "review":
        review = await handler.db.get_review(source.key, conn=conn)
        bound = (
            (
                await conn.execute(
                    select(gates)
                    .where(
                        gates.c.id == review["gate_id"],
                        gates.c.project_id == source.project_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if not bound or bound["gate_type"] != "review" or bound["await_id"] != source.key:
            raise AskError("ask.alias_conflict", "Review gate await_id/back-reference disagree")
        refs.append(("gate", bound["id"]))
    if table is gates:
        refs.append(("gate", row["id"]))
    if table is escalations:
        current = (
            (
                await conn.execute(
                    select(escalations)
                    .where(
                        escalations.c.id == row["id"],
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        if any(current[k] != row[k] for k in ("source_kind", "source_identity", "project_id")):
            raise AskError("ask.stale_source", "Incident source changed during reconciliation")
        refs.append(("escalation", row["id"]))
    # Refresh first: a missed meaning change may advance the canonical generation.
    # Alias proof must name that generation, including on an existing projection.
    refreshed = await service.publish_snapshot(snap, conn=conn, legacy=True)
    publication = await service.publish_snapshot(
        snap,
        conn=conn,
        legacy=True,
        aliases=tuple(
            AliasReference(
                kind,
                key,
                refreshed.ask["decision_generation"],
                source.reference,
                {"verified_by": "source-reconcile:v1", "fingerprint": snap.fingerprint},
            )
            for kind, key in sorted(set(refs))
        ),
    )
    ask = publication.ask
    conflicts = list(publication.reconciliation_case_ids)
    if source.kind == "review":
        conflicts += await historical_feedback(handler.db, review, conn)
    if table in (escalation_messages, operations) and not publication.reconciliation_case_ids:
        # Retain original IDs, actor, text, results, task snapshots and incident links.
        current = (
            (await conn.execute(select(table).where(table.c.id == row["id"]).with_for_update()))
            .mappings()
            .one()
        )
        if current["escalation_id"] != row["escalation_id"]:
            raise AskError("ask.stale_source", "Historical incident reference changed")
        if current["ask_id"] not in (None, ask["id"]):
            report = await case(
                handler.db,
                conn,
                source.project_id,
                f"history-alias:{table.name}:{row['id']}",
                "Historical ledger already belongs to a different ask",
                {"entry": dict(current), "ask_id": ask["id"]},
            )
            conflicts.append(report["id"])
        elif current["ask_id"] is None:
            await conn.execute(
                update(table).where(table.c.id == row["id"]).values(ask_id=ask["id"])
            )
        if (
            table is operations
            and not current["decision_id"]
            and current["status"] in {"processing", "unknown"}
        ):
            report = await case(
                handler.db,
                conn,
                source.project_id,
                f"historical-action:{row['id']}",
                "Accepted historical action needs source reconciliation",
                {"ask_id": ask["id"], "action": dict(current)},
            )
            conflicts.append(report["id"])
    return {
        "source": asdict(source),
        "ask_id": ask["id"],
        "created": refreshed.created,
        "context_state": ask["context_state"],
        "conflicts": conflicts,
    }


async def scan(handler, *, apply=False, limit=100, project_id=None):
    key = "ask-reconcile:v1:" + (project_id or "*")
    async with handler.db._engine.connect() as conn:
        transaction = await conn.begin()
        try:
            # Concurrent scans cannot skip rows or overwrite another cursor.
            await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))
            saved = await conn.scalar(
                select(system_config.c.value).where(system_config.c.key == key)
            )
            cursor = json.loads(saved) if saved else {"phase": 0, "after": "", "cycle": 0}
            before = dict(cursor)
            rows, examined = [], 0
            while examined < limit and cursor["phase"] < len(PHASES):
                table = PHASES[cursor["phase"]]
                pk = table.c.run_id if table is playbook_v2_runs else table.c.id
                stmt = (
                    select(table).where(pk > cursor["after"]).order_by(pk).limit(limit - examined)
                )
                if project_id and "project_id" in table.c:
                    stmt = stmt.where(table.c.project_id == project_id)
                if project_id and table in (escalation_messages, operations):
                    stmt = stmt.where(
                        select(escalations.c.id)
                        .where(
                            escalations.c.id == table.c.escalation_id,
                            escalations.c.project_id == project_id,
                        )
                        .exists()
                    )
                page = (await conn.execute(stmt)).mappings().all()
                if not page:
                    cursor.update(phase=cursor["phase"] + 1, after="")
                    continue
                for raw in page:
                    row = dict(raw)
                    try:
                        async with conn.begin_nested():
                            reset_project = row.get("project_id") or project_id
                            if not reset_project and table in (escalation_messages, operations):
                                reset_project = await conn.scalar(select(escalations.c.project_id).where(
                                    escalations.c.id == row["escalation_id"],
                                ))
                            if await skip_row(conn, table, row, reset_project):
                                rows.append({"skipped": True, "discarded": True})
                                examined += 1
                                cursor["after"] = row[pk.name]
                                continue
                            source = await identity(handler, table, row, conn)
                            result = (
                                {"skipped": True}
                                if project_id and source and source.project_id != project_id
                                else await project_source(handler, table, row, conn)
                            )
                    except (AskError, ValueError, KeyError) as exc:
                        project = row.get("project_id") or project_id
                        if not project and table in (escalation_messages, operations):
                            project = await conn.scalar(
                                select(escalations.c.project_id).where(
                                    escalations.c.id == row["escalation_id"]
                                )
                            )
                        result = {
                            "source_table": table.name,
                            "source_id": row[pk.name],
                            "error": str(exc),
                            "error_code": getattr(exc, "code", "ask.context_missing"),
                        }
                        if project:
                            report = await case(
                                handler.db,
                                conn,
                                project,
                                f"source:{table.name}:{row[pk.name]}",
                                "Source could not be reconciled",
                                {**result, "source": row},
                            )
                            result["conflicts"] = [report["id"]]
                    rows.append(result)
                    examined += 1
                    cursor["after"] = row[pk.name]
            complete = cursor["phase"] == len(PHASES)
            if complete:
                cursor.update(phase=0, after="", cycle=cursor["cycle"] + 1)
            if apply:
                await conn.execute(
                    pg_insert(system_config)
                    .values(key=key, value=json.dumps(cursor))
                    .on_conflict_do_update(
                        index_elements=["key"], set_={"value": json.dumps(cursor)}
                    )
                )
                await transaction.commit()
            else:
                await transaction.rollback()
            return {
                "dry_run": not apply,
                "cursor_before": before,
                "cursor": cursor,
                "cycle_complete": complete,
                "examined": examined,
                "sources": rows,
            }
        except BaseException:
            await transaction.rollback()
            raise


async def diagnostics(conn, *, project_id=None, destination_id=None, limit=100, cleanup=False):
    """Read-only cleanup/transfer blockers. Unknown provenance is never discarded."""
    result = {}
    if project_id and destination_id:
        mode = (
            (
                await conn.execute(
                    select(modes).where(
                        modes.c.project_id == project_id,
                        modes.c.destination == destination_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        if mode and mode["mode"] == "transferring":
            saved = await conn.scalar(
                select(system_config.c.value).where(
                    system_config.c.key == "ask-reconcile:v1:" + project_id
                )
            )
            if not saved or json.loads(saved)["cycle"] < 1:
                result["source_scan_pending"] = {
                    "items": [{"project_id": project_id}],
                    "more": False,
                }
    statements = {
        "transfers": select(modes.c.project_id, modes.c.destination, modes.c.generation).where(
            modes.c.mode == "transferring"
        ),
        "inflight_bindings": select(bindings.c.id).where(bindings.c.inflight_id.is_not(None)),
        "unresolved_operations": select(operations.c.id).where(
            operations.c.status.in_(["pending", "processing", "failed", "unknown"]),
            ~select(human_asks.c.id).where(
                human_asks.c.id == operations.c.ask_id,
                human_asks.c.context["legacy_discarded"].as_boolean().is_(True),
            ).exists(),
            ~select(escalations.c.id).where(
                escalations.c.id == operations.c.escalation_id,
                escalations.c.terminal_outcome == "legacy_discarded",
            ).exists(),
        ),
        "unmapped_inputs": select(escalation_messages.c.id).where(
            escalation_messages.c.direction == "inbound",
            (escalation_messages.c.ask_id.is_(None) | escalation_messages.c.input_id.is_(None)),
            ~select(escalations.c.id).where(
                escalations.c.id == escalation_messages.c.escalation_id,
                (escalations.c.terminal_outcome == "legacy_discarded")
                | before_reset(escalation_messages, escalations.c.project_id),
            ).exists(),
            ~select(human_asks.c.id).where(
                human_asks.c.id == escalation_messages.c.ask_id,
                human_asks.c.context["legacy_discarded"].as_boolean().is_(True),
            ).exists(),
        ),
        "legacy_writes": select(legacy_outbox.c.id).where(
            legacy_outbox.c.status.in_(["sending", "unknown"]),
            ~select(escalations.c.id).where(
                escalations.c.id == legacy_outbox.c.escalation_id,
                escalations.c.terminal_outcome == "legacy_discarded",
            ).exists(),
        ),
        "shared_writes": select(outbox.c.id).where(
            outbox.c.owner_kind == "ask", outbox.c.state.in_(["sending", "unknown"]),
            ~select(human_asks.c.id).where(
                human_asks.c.id == outbox.c.ask_id,
                human_asks.c.context["legacy_discarded"].as_boolean().is_(True),
            ).exists(),
        ),
        "conflicts": select(cases.c.id).where(cases.c.status == "open"),
        "unverified_receipts": select(legacy_outbox.c.id).where(
            legacy_outbox.c.kind == "root",
            ~select(escalations.c.id).where(
                escalations.c.id == legacy_outbox.c.escalation_id,
                escalations.c.terminal_outcome == "legacy_discarded",
            ).exists(),
            ~select(bindings.c.id)
            .where(
                bindings.c.receipt.op("@>")(
                    func.jsonb_build_object(
                        "legacy_receipts",
                        func.jsonb_build_array(func.jsonb_build_object("id", legacy_outbox.c.id)),
                    )
                ),
            )
            .exists(),
        ),
    }
    if project_id and destination_id:
        statements["unimported_review_roots"] = select(human_asks.c.id).where(
            human_asks.c.source_kind == "review",
            human_asks.c.context["legacy_discarded"].as_boolean().is_not(True),
            ~select(bindings.c.id)
            .where(
                bindings.c.ask_id == human_asks.c.id,
                bindings.c.destination == destination_id,
                bindings.c.active,
            )
            .exists(),
        )
    if cleanup:
        statements["compatibility_windows"] = select(
            modes.c.project_id,
            modes.c.destination,
            modes.c.compatibility_deadline,
        ).where(
            modes.c.migrated,
            (
                modes.c.compatibility_deadline.is_(None)
                | (modes.c.compatibility_deadline > time.time())
            ),
        )
    for name, stmt in statements.items():
        table = {
            "transfers": modes,
            "inflight_bindings": bindings,
            "unresolved_operations": operations,
            "unmapped_inputs": escalation_messages,
            "legacy_writes": legacy_outbox,
            "shared_writes": outbox,
            "conflicts": cases,
            "unverified_receipts": legacy_outbox,
            "compatibility_windows": modes,
            "unimported_review_roots": human_asks,
        }[name]
        if project_id:
            if "project_id" in table.c:
                stmt = stmt.where(table.c.project_id == project_id)
            else:
                stmt = stmt.where(
                    select(escalations.c.id)
                    .where(
                        escalations.c.id == table.c.escalation_id,
                        escalations.c.project_id == project_id,
                    )
                    .exists()
                    | (
                        select(human_asks.c.id)
                        .where(
                            human_asks.c.id == table.c.ask_id,
                            human_asks.c.project_id == project_id,
                        )
                        .exists()
                        if "ask_id" in table.c
                        else False
                    )
                )
        if destination_id and table in (modes, bindings):
            stmt = stmt.where(table.c.destination == destination_id)
        order = (table.c.project_id, table.c.destination) if table is modes else (table.c.id,)
        values = [
            dict(r) for r in (await conn.execute(stmt.order_by(*order).limit(limit + 1))).mappings()
        ]
        if values:
            result[name] = {"items": values[:limit], "more": len(values) > limit}
    return result


async def import_roots(handler, project_id, *, limit=100):
    """Adopt proven receipts only while both writers are fenced. Never post a root."""
    dest = destination(handler.config)
    async with handler.db._engine.connect() as conn:
        mode = await handler.db.get_ask_mode(project_id, dest, conn=conn)
        if not mode or mode["mode"] != "transferring":
            return 0
        rows = (
            (
                await conn.execute(
                    select(human_asks)
                    .where(
                        human_asks.c.project_id == project_id,
                        human_asks.c.context["legacy_discarded"].as_boolean().is_not(True),
                        (human_asks.c.source_kind == "review")
                        | select(legacy_outbox.c.id)
                        .join(
                            aliases,
                            (aliases.c.legacy_id == legacy_outbox.c.escalation_id)
                            & (aliases.c.legacy_kind == "escalation"),
                        )
                        .where(aliases.c.ask_id == human_asks.c.id, legacy_outbox.c.kind == "root")
                        .exists(),
                        ~select(bindings.c.id)
                        .where(
                            bindings.c.ask_id == human_asks.c.id,
                            bindings.c.destination == dest,
                            bindings.c.active,
                        )
                        .exists(),
                    )
                    .order_by(human_asks.c.id)
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
    for ask in rows:
        async with handler.db.immediate() as conn:
            source = SourceIdentity(
                project_id, ask["source_kind"], ask["source_key"], ask["source_identity"]
            )
            try:
                await handler._ask_service().adapters.get(source.kind).snapshot(source, conn=conn)
            except AskError:
                continue  # The source scan retains the conflict; never guess a root.
            # Lock order agrees with normal delivery: source, ask, mode, binding.
            ask = await handler.db.get_ask(ask["id"], conn=conn, lock=True)
            mode = await handler.db.get_ask_mode(project_id, dest, conn=conn, lock=True)
            if mode["mode"] != "transferring":
                return 0
            if await handler.db.active_ask_binding(ask["id"], dest, conn=conn):
                continue
            receipts = [
                dict(r)
                for r in (
                    await conn.execute(
                        select(legacy_outbox)
                        .join(
                            aliases,
                            (aliases.c.legacy_id == legacy_outbox.c.escalation_id)
                            & (aliases.c.legacy_kind == "escalation"),
                        )
                        .where(aliases.c.ask_id == ask["id"], legacy_outbox.c.kind == "root")
                        .order_by(legacy_outbox.c.created_at, legacy_outbox.c.id)
                        .limit(101)
                    )
                ).mappings()
            ]
            proven = [
                r
                for r in receipts
                if r["status"] == "sent"
                and r["external_receipt_id"]
                and r["receipt_confirmed_at"]
                and r["channel_id"] == str(handler.config.discord.channel_id)
            ]
            chosen = proven[0] if len(proven) == 1 and len(receipts) == 1 else None
            # Existing reviews without root evidence remain unknown; only sources
            # first published after ownership transfer can create a fresh root.
            uncertain = bool(receipts) or ask["source_kind"] == "review"
            if not chosen and not uncertain:
                continue
            root = chosen["external_receipt_id"] if chosen else None
            occupied = (
                await conn.scalar(
                    select(bindings.c.id).where(
                        bindings.c.transport == "discord",
                        bindings.c.channel_id == str(handler.config.discord.channel_id),
                        bindings.c.external_root_id == root,
                    )
                )
                if root
                else None
            )
            if occupied:
                chosen, root = None, None
            receipt = {
                "legacy_delivery_id": chosen["id"] if chosen else None,
                "imported_at": time.time(),
                "legacy_receipts": receipts,
                "source_identity": ask["source_identity"],
            }
            await handler.db.insert_ask_binding(
                dict(
                    id=new_id("abinding"),
                    ask_id=ask["id"],
                    project_id=project_id,
                    destination=dest,
                    transport="discord",
                    guild_id=str(handler.config.discord.guild_id),
                    channel_id=str(handler.config.discord.channel_id),
                    mode_generation=mode["generation"],
                    root_generation=1,
                    external_root_id=root,
                    legacy_thread_id=chosen["thread_id"] if chosen else None,
                    receipt=receipt,
                    alternate_roots=[
                        {"id": r["external_receipt_id"], "confirmed": r in proven}
                        for r in receipts
                        if r is not chosen
                    ],
                    root_state="confirmed" if chosen else "unknown",
                    active=True,
                    desired_display_version=ask["display_version"],
                    applied_display_version=0,
                    created_at=time.time(),
                    updated_at=time.time(),
                ),
                conn=conn,
            )
            if not chosen:
                await case(
                    handler.db,
                    conn,
                    project_id,
                    f"root-inventory:{ask['id']}:{dest}",
                    "Canonical root needs receipt reconciliation; notified_revision is not proof",
                    {"ask_id": ask["id"], "receipts": receipts, "occupied_binding": occupied},
                )
    return len(rows)


async def resolve_case(handler, args):
    if not args.get("case_id"):
        return
    if not args.get("disposition") or not (args.get("evidence") or "").strip():
        raise AskError(
            "ask.evidence_required", "A reconciliation disposition and evidence are required"
        )
    async with handler.db.immediate() as conn:
        row = (
            (await conn.execute(select(cases).where(cases.c.id == args["case_id"])))
            .mappings()
            .first()
        )
        if not row or (args.get("project_id") and row["project_id"] != args["project_id"]):
            raise AskError("ask.not_found", "Reconciliation case was not found")
        if args["apply"]:
            from src.commands.principal import current_principal, TRUSTED_LOCAL

            await handler.db.resolve_ask_reconciliation_case(
                row["id"],
                {
                    "disposition": args["disposition"],
                    "evidence": args["evidence"],
                    "principal": (current_principal() or TRUSTED_LOCAL).describe(),
                    "recorded_at": time.time(),
                },
                now=time.time(),
                conn=conn,
            )
