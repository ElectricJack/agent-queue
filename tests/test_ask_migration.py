"""A02: canonical identity/aliases and replayable quarantine without title merging."""

from dataclasses import replace

from sqlalchemy import func, select

from src.asks.models import AliasReference, SourceIdentity
from src.database.tables import ask_reconciliation_cases, ask_source_aliases, human_asks
from tests.ask_helpers import db as db_fixture, service as service_fixture, snapshot  # noqa: F401


def aliases(source):
    return tuple(
        AliasReference(kind, key, 1, source.identity.reference, {"verified_by": "adapter:1"})
        for kind, key in (("review", "rev-ask"), ("gate", "gate-1"), ("incident", "incident-1"))
    )


async def test_review_gate_and_bound_incident_share_one_ask_replay(service, db):
    source = snapshot()
    async with db._engine.begin() as conn:
        one = await service.publish_snapshot(source, aliases=aliases(source), conn=conn)
        two = await service.publish_snapshot(source, aliases=aliases(source), conn=conn)
        assert one.created and not two.created
        assert one.ask["id"] == two.ask["id"]
        assert not two.reconciliation_case_ids
        assert (await conn.execute(select(func.count()).select_from(human_asks))).scalar_one() == 1
        assert set((await conn.execute(select(ask_source_aliases.c.ask_id))).scalars()) == {
            one.ask["id"]
        }
        unrelated = await service.publish_snapshot(snapshot("attempt-2", "recovery"), conn=conn)
        assert (
            unrelated.ask["id"] != one.ask["id"]
        )  # identical title cannot merge distinct requests


async def test_alias_disagreement_quarantines_and_preserves_original_mapping(service, db):
    source = snapshot()
    async with db._engine.begin() as conn:
        original = await service.publish_snapshot(source, aliases=aliases(source), conn=conn)
        other = snapshot("rev-other")
        conflict = AliasReference(
            "gate", "gate-1", 1, other.identity.reference, {"verified": "other"}
        )
        result = await service.publish_snapshot(other, aliases=(conflict,), conn=conn)
        replay = await service.publish_snapshot(other, aliases=(conflict,), conn=conn)
        assert result.ask["context_state"] == "quarantined"
        assert result.ask["context"]["choices"] == []
        assert result.reconciliation_case_ids == replay.reconciliation_case_ids
        assert result.ask["display_version"] == replay.ask["display_version"]
        assert (await db.get_ask_alias("p-ask", "gate", "gate-1", conn=conn))[
            "ask_id"
        ] == original.ask["id"]
        assert (
            await conn.execute(select(func.count()).select_from(ask_reconciliation_cases))
        ).scalar() == 1
        refresh = await service.publish_snapshot(other, conn=conn)
        assert refresh.ask["context_state"] == "quarantined"


async def test_same_source_key_cannot_silently_replace_identity(service, db):
    source = snapshot()
    async with db._engine.begin() as conn:
        original = await service.publish_snapshot(source, conn=conn)
        changed = replace(
            source, identity=SourceIdentity("p-ask", "review", "rev-ask", {"id": "other"})
        )
        result = await service.publish_snapshot(changed, conn=conn)
        assert result.ask["id"] == original.ask["id"]
        assert result.ask["source_identity"] == source.identity.reference
        assert result.reconciliation_case_ids


async def test_unverified_alias_refuses_coalescing(service, db):
    source = snapshot()
    async with db._engine.begin() as conn:
        alias = AliasReference("gate", "gate-unknown", 1, {"id": "wrong"}, {})
        result = await service.publish_snapshot(source, aliases=(alias,), conn=conn)
        assert result.reconciliation_case_ids
        assert await db.get_ask_alias("p-ask", "gate", "gate-unknown", conn=conn) is None


async def reconcile_review(handler):
    from src.models import Project

    await handler.db.create_project(Project(id="p-reconcile", name="Reconciliation"))
    result = await handler.execute(
        "review_submit",
        {
            "project_id": "p-reconcile",
            "kind": "spec",
            "title": "Same title",
            "content": "# Design\n\nA bounded proposal.\n",
        },
    )
    assert result["success"], result
    return await handler.db.get_review(result["review_id"])


async def test_reconcile_dry_run_cursor_and_exact_source_aliases(command_handler_factory):
    from src.database.tables import escalations, system_config

    handler = await command_handler_factory()
    review = await reconcile_review(handler)
    async with handler.db.immediate() as conn:
        for key, kind, target in (
            ("bound", "gate", review["gate_id"]),
            ("unrelated", "core", "different-source"),
        ):
            await conn.execute(
                escalations.insert().values(
                    id=key,
                    project_id="p-reconcile",
                    source_kind=kind,
                    source_identity=target,
                    incident_key=key,
                    supervisor_owner="supervisor:p-reconcile",
                    summary="Same title",
                    investigation="Original investigation",
                    decision_requested="Decide?",
                    severity="high",
                    task_title="Archived task title",
                    created_at=1,
                    updated_at=1,
                )
            )
        count_before = await conn.scalar(select(func.count()).select_from(human_asks))
    dry = await handler.execute("ask_reconcile", {"project_id": "p-reconcile"})
    assert dry["success"] and dry["result"]["dry_run"], dry
    async with handler.db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(human_asks)) == count_before
        assert not await conn.scalar(
            select(system_config.c.value).where(
                system_config.c.key == "ask-reconcile:v1:p-reconcile"
            )
        )
    # One row at a time exercises durable resumes across source tables.
    for _ in range(20):
        result = await handler.execute(
            "ask_reconcile",
            {
                "project_id": "p-reconcile",
                "apply": True,
                "limit": 1,
            },
        )
        assert result["success"], result
        assert result["result"]["examined"] <= 1
        if result["result"]["cycle_complete"]:
            break
    else:
        raise AssertionError("Cursor never completed the bounded inventory")
    async with handler.db.immediate() as conn:
        mapped = {
            r["legacy_id"]: r["ask_id"]
            for r in (await conn.execute(select(ask_source_aliases))).mappings()
        }
        assert mapped[review["id"]] == mapped[review["gate_id"]] == mapped["bound"]
        assert mapped["unrelated"] != mapped["bound"]
        assert await conn.scalar(select(func.count()).select_from(human_asks)) == 2
    replay = await handler.execute("ask_reconcile", {"project_id": "p-reconcile", "apply": True})
    assert replay["success"], replay
    assert not any(row.get("conflicts") for row in replay["result"]["sources"])


async def test_historical_feedback_is_reported_without_reapproval(command_handler_factory):
    from sqlalchemy import update
    from src.database.tables import (
        ask_decisions,
        doc_reviews,
        doc_review_comments,
        review_feedback_obligations,
    )

    handler = await command_handler_factory()
    review = await reconcile_review(handler)
    async with handler.db.immediate() as conn:
        await conn.execute(
            doc_review_comments.insert().values(
                id="historic-comment",
                review_id=review["id"],
                revision=1,
                body="Required correction",
                heading_path=[],
                author="human:local-operator",
                created_at=1,
                created_seq=1,
                resolved_in_revision=1,
            )
        )
        await conn.execute(
            update(doc_reviews)
            .where(doc_reviews.c.id == review["id"])
            .values(
                state="approved",
                decided_at=2,
                decided_by="historical-human",
                notified_revision=1,
            )
        )
    result = await handler.execute("ask_reconcile", {"apply": True, "project_id": "p-reconcile"})
    assert result["success"], result
    async with handler.db.immediate() as conn:
        report = (await conn.execute(select(ask_reconciliation_cases))).mappings().one()
        assert report["source_evidence"]["comments"][0]["id"] == "historic-comment"
        assert report["source_evidence"]["decided_by"] == "historical-human"
        assert not await conn.scalar(select(func.count()).select_from(ask_decisions))
        assert not await conn.scalar(select(func.count()).select_from(review_feedback_obligations))
    decided = await handler.execute(
        "ask_reconcile",
        {
            "apply": True,
            "project_id": "p-reconcile",
            "case_id": report["id"],
            "disposition": "reviewed_no_action",
            "evidence": "Operator audited original acceptance and work receipt.",
        },
    )
    assert decided["success"], decided
    async with handler.db.immediate() as conn:
        saved = (await conn.execute(select(ask_reconciliation_cases))).mappings().one()
        assert saved["status"] == "resolved" and saved["repair_receipt"]["principal"]


async def test_cutover_stages_cas_and_rollback_do_not_restore_legacy(command_handler_factory):
    handler = await command_handler_factory()
    from src.models import Project
    from src.asks.models import digest
    from src.asks.reconcile import diagnostics
    from src.asks.delivery import destination

    await handler.db.create_project(Project(id="p-cutover", name="Cutover"))
    for generation, mode in enumerate(("shadow", "dashboard", "transferring")):
        status = await handler.execute("ask_cutover_status", {"project_id": "p-cutover"})
        current = status["result"]["destinations"][0]
        assert current["generation"] == generation
        assert current["first_switch_utc"] is None
        result = await handler.execute(
            "ask_cutover",
            {
                "project_id": "p-cutover",
                "mode": mode,
                "expected_generation": current["generation"],
            },
        )
        assert result["success"] and result["result"]["generation"] == generation + 1, result
    reconciled = await handler.execute("ask_reconcile", {"project_id": "p-cutover", "apply": True})
    assert reconciled["success"], reconciled
    async with handler.db._engine.connect() as conn:
        inventory = await diagnostics(
            conn, project_id="p-cutover", destination_id=destination(handler.config)
        )
    arguments = {
        "project_id": "p-cutover",
        "mode": "channel",
        "expected_generation": 3,
        "inventory_sha256": digest(inventory),
        "evidence": "Verified receipt inventory, disabled controls and redirects.",
        "legacy_controls_disabled": True,
        "aliases_redirected": True,
    }
    one = await handler.execute("ask_cutover", arguments)
    assert one["success"] and one["result"]["compatibility_deadline"], one
    status = await handler.execute("ask_cutover_status", {"project_id": "p-cutover"})
    switched = status["result"]["destinations"][0]
    assert switched["mode"] == "channel" and switched["migrated"]
    assert switched["generation"] == 4
    assert switched["compatibility_deadline"] == one["result"]["compatibility_deadline"]
    from datetime import datetime

    first_switch = datetime.fromisoformat(switched["first_switch_utc"])
    deadline = datetime.fromisoformat(switched["compatibility_deadline_utc"])
    assert first_switch.utcoffset().total_seconds() == 0
    assert deadline.utcoffset().total_seconds() == 0
    assert (deadline - first_switch).total_seconds() == 30 * 86400
    replay = await handler.execute("ask_cutover", arguments)
    assert not replay["success"]
    rollback = await handler.execute(
        "ask_cutover",
        {
            "project_id": "p-cutover",
            "mode": "delivery_disabled",
            "expected_generation": 4,
        },
    )
    assert rollback["success"], rollback
    assert rollback["result"]["compatibility_deadline"] == one["result"]["compatibility_deadline"]
    status = await handler.execute("ask_cutover_status", {"project_id": "p-cutover"})
    assert status["result"]["destinations"] == [
        {**switched, "mode": "delivery_disabled", "generation": 5}
    ]
    refused = await handler.execute(
        "ask_cutover",
        {
            "project_id": "p-cutover",
            "mode": "legacy",
            "expected_generation": 5,
        },
    )
    assert refused["error_code"] == "ask.legacy_retired"
    transfer = await handler.execute(
        "ask_cutover",
        {
            "project_id": "p-cutover",
            "mode": "transferring",
            "expected_generation": 5,
        },
    )
    assert transfer["success"], transfer
    async with handler.db._engine.connect() as conn:
        inventory = await diagnostics(
            conn, project_id="p-cutover", destination_id=destination(handler.config)
        )
    enabled = await handler.execute(
        "ask_cutover",
        {
            **arguments,
            "expected_generation": 6,
            "inventory_sha256": digest(inventory),
        },
    )
    assert enabled["success"], enabled
    status = await handler.execute("ask_cutover_status", {"project_id": "p-cutover"})
    assert status["result"]["destinations"] == [{**switched, "generation": 7}]


async def test_cutover_status_lists_only_project_destinations_without_writing(
    command_handler_factory,
):
    from src.asks.delivery import destination
    from src.database.tables import human_communication_modes
    from src.models import Project

    handler = await command_handler_factory()
    await handler.db.create_project(Project(id="p-status", name="Status"))
    await handler.db.create_project(Project(id="p-other", name="Other"))
    configured = destination(handler.config)
    async with handler.db.immediate() as conn:
        for project, dest in (("p-status", "discord:old:channel"), ("p-other", configured)):
            await handler.db.advance_ask_mode(
                {
                    "project_id": project,
                    "destination": dest,
                    "mode": "shadow",
                    "config_fingerprint": "old",
                    "updated_at": 1,
                },
                expected_generation=0,
                conn=conn,
            )
        before = (await conn.execute(select(human_communication_modes))).mappings().all()

    for _ in range(2):
        status = await handler.execute("ask_cutover_status", {"project_id": "p-status"})
        assert status["success"], status
        assert status["result"] == {
            "project_id": "p-status",
            "configured_destination": configured,
            "destinations": sorted(
                [
                    {
                        "destination": configured,
                        "mode": "legacy",
                        "generation": 0,
                        "migrated": False,
                        "first_switch_utc": None,
                        "compatibility_deadline": None,
                        "compatibility_deadline_utc": None,
                    },
                    {
                        "destination": "discord:old:channel",
                        "mode": "shadow",
                        "generation": 1,
                        "migrated": True,
                        "first_switch_utc": None,
                        "compatibility_deadline": None,
                        "compatibility_deadline_utc": None,
                    },
                ],
                key=lambda row: row["destination"],
            ),
        }
    async with handler.db._engine.connect() as conn:
        after = (await conn.execute(select(human_communication_modes))).mappings().all()
    assert before == after


async def test_reconcile_quarantines_bad_gate_and_continues(command_handler_factory):
    from sqlalchemy import update
    from src.database.tables import gates

    handler = await command_handler_factory()
    review = await reconcile_review(handler)
    async with handler.db.immediate() as conn:
        await conn.execute(
            update(gates).where(gates.c.id == review["gate_id"]).values(await_id="absent")
        )
    result = await handler.execute("ask_reconcile", {"apply": True, "project_id": "p-reconcile"})
    assert result["success"] and result["result"]["cycle_complete"], result
    assert result["result"]["inventory"]["conflicts"]
    async with handler.db.immediate() as conn:
        assert not await handler.db.get_ask_alias(
            "p-reconcile", "gate", review["gate_id"], conn=conn
        )


async def test_import_keeps_proven_receipt_and_unknown_notifier_history(command_handler_factory):
    from sqlalchemy import update
    from src.asks.delivery import destination
    from src.database.tables import (
        doc_reviews,
        escalation_deliveries,
        escalations,
        ask_external_bindings,
    )
    from src.doctor.ask_checks import check
    from types import SimpleNamespace

    handler = await command_handler_factory()
    review = await reconcile_review(handler)
    second = await handler.execute(
        "review_submit",
        {
            "project_id": "p-reconcile",
            "kind": "spec",
            "title": "No receipt",
            "content": "# Missing proof\n",
        },
    )
    async with handler.db.immediate() as conn:
        await conn.execute(
            update(doc_reviews)
            .where(doc_reviews.c.id == second["review_id"])
            .values(notified_revision=1)
        )
        await conn.execute(
            escalations.insert().values(
                id="bound-receipt",
                project_id="p-reconcile",
                source_kind="gate",
                source_identity=review["gate_id"],
                incident_key="bound-receipt",
                supervisor_owner="supervisor:p-reconcile",
                summary="Old title",
                investigation="Old investigation",
                decision_requested="Proceed?",
                severity="high",
                created_at=1,
                updated_at=1,
            )
        )
        await conn.execute(
            escalation_deliveries.insert().values(
                id="old-root",
                escalation_id="bound-receipt",
                dedup_key="old-root",
                kind="root",
                payload={"historical": True},
                status="sent",
                external_receipt_id="123",
                root_message_id="123",
                receipt_confirmed_at=2,
                thread_id="456",
                channel_id=str(handler.config.discord.channel_id),
                next_attempt_at=1,
                created_at=1,
                updated_at=2,
            )
        )
    for generation, mode in enumerate(("shadow", "dashboard", "transferring")):
        assert (
            await handler.execute(
                "ask_cutover",
                {
                    "project_id": "p-reconcile",
                    "mode": mode,
                    "expected_generation": generation,
                },
            )
        )["success"]
    applied = await handler.execute("ask_reconcile", {"project_id": "p-reconcile", "apply": True})
    assert applied["success"], applied
    assert "unverified_receipts" not in applied["result"]["inventory"]
    assert applied["result"]["inventory"]["conflicts"]
    async with handler.db.immediate() as conn:
        bindings = (await conn.execute(select(ask_external_bindings))).mappings().all()
        assert len(bindings) == 2
        proven = next(row for row in bindings if row["external_root_id"])
        unknown = next(row for row in bindings if not row["external_root_id"])
        assert proven["external_root_id"] == "123" and proven["legacy_thread_id"] == "456"
        assert proven["receipt"]["legacy_receipts"][0]["payload"] == {"historical": True}
        assert unknown["root_state"] == "unknown" and not unknown["inflight_id"]
        assert (await handler.db.get_review(second["review_id"], conn=conn))[
            "notified_revision"
        ] == 1
        assert proven["destination"] == destination(handler.config)
    doctor = await check(SimpleNamespace(db=handler.db))
    assert not doctor.data["cleanup_ready"] and not doctor.fixable
    assert "compatibility_windows" in doctor.data["blockers"]
    replay = await handler.execute("ask_reconcile", {"project_id": "p-reconcile", "apply": True})
    assert replay["success"], replay
    async with handler.db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(ask_external_bindings)) == 2


async def test_bounded_history_scan_preserves_action_evidence(command_handler_factory):
    from src.database.tables import escalations, escalation_actions, escalation_messages

    handler = await command_handler_factory()
    review = await reconcile_review(handler)
    async with handler.db.immediate() as conn:
        await conn.execute(
            escalations.insert().values(
                id="history",
                project_id="p-reconcile",
                source_kind="gate",
                source_identity=review["gate_id"],
                incident_key="history",
                supervisor_owner="supervisor:p-reconcile",
                summary="Archived title",
                investigation="Original reasoning",
                decision_requested="Proceed?",
                severity="high",
                task_title="Archived task",
                task_id="task-no-longer-live",
                created_at=1,
                updated_at=1,
            )
        )
        await conn.execute(
            escalation_messages.insert().values(
                id="history-reply",
                escalation_id="history",
                direction="inbound",
                transport="discord",
                verified_actor="discord:123",
                text="Original human words",
                external_message_id="456",
                received_at=1,
                created_at=1,
            )
        )
        await conn.execute(
            escalation_actions.insert().values(
                id="history-action",
                escalation_id="history",
                reply_id="history-reply",
                idempotency_key="original",
                action_kind="gate_resolve",
                target_id=review["gate_id"],
                parameters={"original": True},
                executor="supervisor:old",
                started_revision=0,
                status="unknown",
                outcome="unknown",
                result={"original_result": True},
                created_at=1,
                completed_at=2,
            )
        )
    for _ in range(20):
        result = await handler.execute(
            "ask_reconcile", {"project_id": "p-reconcile", "apply": True, "limit": 1}
        )
        assert result["success"] and result["result"]["examined"] <= 1, result
        if result["result"]["cycle_complete"]:
            break
    else:
        raise AssertionError("No completed cycle")
    async with handler.db.immediate() as conn:
        action = (await conn.execute(select(escalation_actions))).mappings().one()
        entry = (await conn.execute(select(escalation_messages))).mappings().one()
        incident = (await conn.execute(select(escalations))).mappings().one()
        assert action["ask_id"] == entry["ask_id"] and entry["ask_id"]
        assert action["status"] == "unknown" and action["result"] == {"original_result": True}
        assert action["executor"] == "supervisor:old" and not action["decision_id"]
        assert entry["verified_actor"] == "discord:123" and entry["text"] == "Original human words"
        assert (
            incident["task_title"] == "Archived task"
            and incident["task_id"] == "task-no-longer-live"
        )
    assert result["result"]["inventory"]["unresolved_operations"]
    assert result["result"]["inventory"]["conflicts"]


async def test_reconcile_alias_proof_uses_refreshed_generation(command_handler_factory):
    from sqlalchemy import update
    from src.database.tables import doc_reviews

    handler = await command_handler_factory()
    review = await reconcile_review(handler)
    first = await handler.execute("ask_reconcile", {"project_id": "p-reconcile", "apply": True})
    assert first["success"], first
    async with handler.db.immediate() as conn:
        await conn.execute(
            update(doc_reviews)
            .where(doc_reviews.c.id == review["id"])
            .values(
                decision_generation=doc_reviews.c.decision_generation + 1,
            )
        )
    second = await handler.execute("ask_reconcile", {"project_id": "p-reconcile", "apply": True})
    assert second["success"], second
    assert not any(row.get("conflicts") for row in second["result"]["sources"])
    async with handler.db.immediate() as conn:
        ask = (await conn.execute(select(human_asks))).mappings().one()
        assert ask["decision_generation"] > 1
        assert ask["context_state"] == "complete"
        assert (
            await handler.db.get_ask_alias("p-reconcile", "gate", review["gate_id"], conn=conn)
        )["ask_id"] == ask["id"]
