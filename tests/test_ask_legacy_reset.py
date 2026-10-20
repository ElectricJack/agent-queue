"""Project fresh starts discard history without sending or replaying decisions."""

import pytest
from sqlalchemy import func, select, update

from src.api.auth import RequestScope
from src.api.scope import check_command_scope
from src.asks.delivery import destination
from src.asks.intake import backfill_legacy
from src.asks.reconcile import diagnostics, scan
from src.asks.reset_policy import reset_state
from src.database import tables as t
from src.sessions.spec import named_session_name
from tests.test_ask_discussion import env as env  # noqa: F401
from tests.test_ask_migration import reconcile_review
from tests.test_review_commands import _scoped
from tests.test_review_commands import env as review_env  # noqa: F401


async def seed(handler, *, project="p-reconcile", transfer=True):
    review = await reconcile_review(handler)
    assert (await handler.execute("review_show", {"review_id": review["id"]}))["success"]
    async with handler.db.immediate() as conn:
        old = await handler.db.get_ask_by_source(project, "review", review["id"], conn=conn)
        await conn.execute(
            t.escalations.insert().values(
                id="old-incident",
                project_id=project,
                source_kind="core",
                source_identity="old-source",
                incident_key="old",
                severity="high",
                supervisor_owner=f"supervisor:{project}",
                summary="Old",
                investigation="Old",
                decision_requested="Old",
                created_at=1,
                updated_at=1,
            )
        )
        for key, status in (("sent", "sent"), ("unknown", "unknown"), ("sending", "sending")):
            await conn.execute(
                t.escalation_deliveries.insert().values(
                    id=key,
                    escalation_id="old-incident",
                    dedup_key=key,
                    kind="root",
                    payload={"old": True},
                    status=status,
                    next_attempt_at=1,
                    lease_owner="old" if status == "sending" else None,
                    lease_expires_at=1 if status == "sending" else None,
                    external_receipt_id="123" if status == "sent" else None,
                    receipt_confirmed_at=1 if status == "sent" else None,
                    created_at=1,
                    updated_at=1,
                )
            )
        await conn.execute(
            t.escalation_messages.insert().values(
                id="old-input",
                escalation_id="old-incident",
                direction="inbound",
                transport="discord",
                verified_actor="discord:1",
                text="Old approval",
                external_message_id="123",
                received_at=1,
                created_at=1,
            )
        )
        await conn.execute(
            t.escalation_actions.insert().values(
                id="old-action",
                escalation_id="old-incident",
                reply_id="old-input",
                idempotency_key="old",
                action_kind="gate_resolve",
                target_id="old-gate",
                parameters={},
                executor="old",
                started_revision=0,
                status="unknown",
                outcome="unknown",
                completed_at=1,
                created_at=1,
            )
        )
        await handler.db.insert_ask_reconciliation_case(
            {
                "id": "old-case",
                "project_id": project,
                "conflict_key": "missing-source",
                "source_evidence": {"unknown": True},
                "logical_owner": f"supervisor:{project}",
                "reason": "Unknown",
                "created_at": 1,
                "updated_at": 1,
            },
            conn=conn,
        )
    for generation, target in enumerate(
        ("shadow", "dashboard", "transferring") if transfer else ("shadow",)
    ):
        result = await handler.execute(
            "ask_cutover",
            {
                "project_id": project,
                "mode": target,
                "expected_generation": generation,
            },
        )
        assert result["success"], result
    return review, old


async def counts(db):
    async with db._engine.connect() as conn:
        return {
            table.name: await conn.scalar(select(func.count()).select_from(table))
            for table in (
                t.human_asks,
                t.ask_decisions,
                t.ask_external_bindings,
                t.ask_reconciliation_cases,
                t.escalation_actions,
                t.escalation_deliveries,
                t.conversation_inputs,
                t.outbound_deliveries,
            )
        }


async def apply_reset(handler):
    args = {"project_id": "p-reconcile"}
    preview = await handler.execute("ask_legacy_reset", args)
    assert preview["success"] and preview["result"]["zero_blockers"], preview
    result = await handler.execute(
        "ask_legacy_reset",
        {
            **args,
            "apply": True,
            "evidence_sha256": preview["result"]["evidence_sha256"],
        },
    )
    assert result["success"], result
    return result


async def test_preview_apply_retry_fresh_requests_no_send_or_replay(command_handler_factory):
    handler = await command_handler_factory()
    review, old = await seed(handler)
    before = await counts(handler.db)
    args = {"project_id": "p-reconcile"}
    preview = await handler.execute("ask_legacy_reset", args)
    assert preview["success"] and preview["result"]["zero_blockers"], preview
    assert preview["result"]["kept_reviews"] == [review["id"]]
    assert await counts(handler.db) == before
    again = await handler.execute("ask_legacy_reset", args)
    assert again["result"]["evidence_sha256"] == preview["result"]["evidence_sha256"]
    request = {**args, "apply": True, "evidence_sha256": preview["result"]["evidence_sha256"]}
    result = await handler.execute("ask_legacy_reset", request)
    assert result["success"] and result["result"]["zero_blockers"], result
    fresh = result["result"]["fresh_requests"][0]["ask_id"]
    assert fresh != old["id"]
    assert await handler.db.get_review(review["id"]) == review
    saved = await counts(handler.db)
    assert saved["ask_decisions"] == saved["outbound_deliveries"] == 0
    assert saved["escalation_deliveries"] == saved["ask_reconciliation_cases"] == 0
    retry = await handler.execute("ask_legacy_reset", request)
    assert retry["success"] and retry["result"]["replayed"]
    assert await counts(handler.db) == saved
    listed = await handler.execute("ask_list", {"project_id": "p-reconcile"})
    assert listed["success"], listed
    assert [r["id"] for r in listed["items"]] == [fresh]
    assert not (await handler.execute("ask_show", {"ask_id": old["id"]}))["success"]
    async with handler.db.immediate() as conn:
        binding = await handler.db.active_ask_binding(fresh, destination(handler.config), conn=conn)
        assert binding["root_state"] == "pending" and binding["external_root_id"] is None
        assert await backfill_legacy(handler, conn=conn, project_id="p-reconcile") == 0
        state = await reset_state(conn, "p-reconcile")
        assert state["result"]["counts"]["escalation_deliveries"] == 3
        assert state["principal"] == "local:-"
    for scoped in ("p-reconcile", None):
        result = await scan(handler, apply=True, limit=100, project_id=scoped)
        assert not any(r.get("conflicts") for r in result["sources"]), result
    async with handler.db._engine.connect() as conn:
        inventory = await diagnostics(
            conn, project_id="p-reconcile", destination_id=destination(handler.config)
        )
    assert set(inventory) == {"transfers"}
    dry = await handler.execute("ask_reconcile", {"project_id": "p-reconcile"})
    assert dry["success"] and set(dry["result"]["inventory"]) == {"transfers"}, dry


@pytest.mark.parametrize("changed", ["row", "mode", "config"])
async def test_stale_plan_atomic(command_handler_factory, changed):
    handler = await command_handler_factory()
    await seed(handler)
    preview = await handler.execute("ask_legacy_reset", {"project_id": "p-reconcile"})
    assert preview["success"], preview
    if changed == "row":
        async with handler.db.immediate() as conn:
            await conn.execute(update(t.escalations).values(summary="Changed"))
    elif changed == "mode":
        assert (
            await handler.execute(
                "ask_cutover",
                {
                    "project_id": "p-reconcile",
                    "mode": "delivery_disabled",
                    "expected_generation": 3,
                },
            )
        )["success"]
    else:
        handler.config.discord.channel_id = 999
    before = await counts(handler.db)
    result = await handler.execute(
        "ask_legacy_reset",
        {
            "project_id": "p-reconcile",
            "apply": True,
            "evidence_sha256": preview["result"]["evidence_sha256"],
        },
    )
    assert result["error_code"] in {"ask.stale_evidence", "ask.reset_unfenced"}, result
    assert await counts(handler.db) == before


async def test_bound_and_unfenced_apply_refuse(command_handler_factory):
    handler = await command_handler_factory()
    await seed(handler, transfer=False)
    before = await counts(handler.db)
    short = await handler.execute("ask_legacy_reset", {"project_id": "p-reconcile", "max_rows": 1})
    assert short["error_code"] == "ask.reset_bound", short
    preview = await handler.execute("ask_legacy_reset", {"project_id": "p-reconcile"})
    assert preview["success"], preview
    refused = await handler.execute(
        "ask_legacy_reset",
        {
            "project_id": "p-reconcile",
            "apply": True,
            "evidence_sha256": preview["result"]["evidence_sha256"],
        },
    )
    assert refused["error_code"] == "ask.reset_unfenced", refused
    assert await counts(handler.db) == before


async def test_global_supervisor_live_instance_and_scope(env):
    handler, db = env
    args = {"project_id": "p"}
    for sid in ("worker", "supervisor"):
        result = await _scoped(handler, "ask_legacy_reset", args, session_id=sid)
        assert not result["success"], result
    await db.update_session(
        "supervisor", project_id=None, name=named_session_name("supervisor", "global")
    )
    result = await _scoped(
        handler, "ask_legacy_reset", args, session_id="supervisor", project_id=None
    )
    assert result["error_code"] == "ask.reset_unfenced", result
    await db.update_session("supervisor", desired_state="stopped")
    result = await _scoped(
        handler, "ask_legacy_reset", args, session_id="supervisor", project_id=None
    )
    assert result["error_code"] == "ask.not_supervisor", result
    for command in ("ask_legacy_reset", "ask_reconcile", "ask_cutover", "ask_cutover_status"):
        assert (
            check_command_scope(command, args.copy(), RequestScope(kind="session", elevated=True))
            is None
        )
        for elevated in (True, False):
            assert check_command_scope(
                command,
                args.copy(),
                RequestScope(
                    kind="session",
                    project_id="p",
                    elevated=elevated,
                ),
            )


async def test_full_legacy_inventory_keeps_six_live_reviews_and_other_project(
    command_handler_factory,
):
    from src.models import Project

    handler = await command_handler_factory()
    review, old = await seed(handler)
    live = [review["id"]]
    for index in range(5):
        result = await handler.execute(
            "review_submit",
            {
                "project_id": "p-reconcile",
                "kind": "spec",
                "title": f"Live {index}",
                "content": "# Live work\n\nNeeds a human decision.\n",
            },
        )
        assert result["success"], result
        live.append(result["review_id"])
    await handler.db.create_project(Project(id="other", name="Other"))
    async with handler.db.immediate() as conn:
        await conn.execute(
            t.doc_reviews.insert(),
            [
                {
                    **review,
                    "id": f"historical-{i}",
                    "vault_path": f"history/{i}.md",
                    "state": "approved",
                    "decided_by": "human:old",
                    "decided_at": 2,
                    "created_at": 1,
                    "updated_at": 2,
                }
                for i in range(82)
            ],
        )
        await conn.execute(
            t.human_asks.insert(),
            [
                {
                    **old,
                    "id": f"historical-ask-{i}",
                    "source_key": f"historical-{i}",
                    "source_identity": {"id": f"historical-{i}"},
                    "decision_state": "approved",
                    "next_actor": "none",
                    "created_at": 1,
                }
                for i in range(82)
            ],
        )
        await conn.execute(
            t.escalation_deliveries.insert(),
            [
                {
                    "id": f"history-{i:03}",
                    "escalation_id": "old-incident",
                    "dedup_key": f"history-{i:03}",
                    "kind": "root",
                    "payload": {},
                    "status": "sent",
                    "next_attempt_at": 1,
                    "external_receipt_id": str(1000 + i),
                    "receipt_confirmed_at": 1,
                    "created_at": 1,
                    "updated_at": 1,
                }
                for i in range(120)
            ],
        )
        await conn.execute(
            t.escalations.insert().values(
                id="foreign",
                project_id="other",
                source_kind="core",
                source_identity="foreign",
                incident_key="foreign",
                severity="high",
                supervisor_owner="supervisor:other",
                summary="Foreign",
                investigation="Foreign",
                decision_requested="Foreign",
                created_at=1,
                updated_at=1,
            )
        )
        foreign_before = dict(
            (
                await conn.execute(
                    select(t.escalations).where(
                        t.escalations.c.id == "foreign",
                    )
                )
            )
            .mappings()
            .one()
        )
    result = await apply_reset(handler)
    assert sorted(result["result"]["kept_reviews"]) == sorted(live)
    assert result["result"]["counts"]["escalation_deliveries"] == 123
    assert result["result"]["counts"]["doc_reviews"] == 88
    assert len(result["result"]["fresh_requests"]) == 6
    for view in ("needs_you", "history"):
        listed = await handler.execute("ask_list", {"project_id": "p-reconcile", "view": view})
        assert listed["success"], listed
        assert len(listed["items"]) == (6 if view == "needs_you" else 0)
    # A full subsequent scan cannot re-import the 82 historical review roots.
    scanned = await scan(handler, apply=True, limit=500, project_id="p-reconcile")
    assert scanned["cycle_complete"]
    assert not any(row.get("conflicts") for row in scanned["sources"]), scanned
    async with handler.db._engine.connect() as conn:
        assert (
            dict(
                (
                    await conn.execute(
                        select(t.escalations).where(
                            t.escalations.c.id == "foreign",
                        )
                    )
                )
                .mappings()
                .one()
            )
            == foreign_before
        )
        assert set(
            await diagnostics(
                conn, project_id="p-reconcile", destination_id=destination(handler.config)
            )
        ) == {"transfers"}
    new = await handler.execute(
        "review_submit",
        {
            "project_id": "p-reconcile",
            "kind": "spec",
            "title": "After reset",
            "content": "# New work\n",
        },
    )
    assert new["success"], new
    shown = await handler.execute("review_show", {"review_id": new["review_id"]})
    assert shown["success"], shown
    async with handler.db._engine.connect() as conn:
        fresh = await handler.db.get_ask(shown["ask"]["id"], conn=conn)
        assert not fresh["context"].get("legacy_discarded")


async def test_accepted_decision_is_obsolete_and_cannot_be_replayed(command_handler_factory):
    from tests.test_review_commands import decide

    handler = await command_handler_factory()
    review, old = await seed(handler)
    accepted = await decide(
        handler,
        {
            "review_id": review["id"],
            "revision": 1,
            "decision": "approve",
        },
    )
    assert accepted["success"], accepted
    async with handler.db._engine.connect() as conn:
        decisions = [dict(r) for r in (await conn.execute(select(t.ask_decisions))).mappings()]
        assert decisions
    source = await handler.db.get_review(review["id"])
    result = await apply_reset(handler)
    assert not result["result"]["kept_reviews"]
    assert await handler.db.get_review(review["id"]) == source
    refused = await handler.execute("ask_retry_operation", {"decision_id": decisions[0]["id"]})
    assert refused["error_code"] == "ask.discarded", refused
    async with handler.db._engine.connect() as conn:
        assert [
            dict(r) for r in (await conn.execute(select(t.ask_decisions))).mappings()
        ] == decisions
    listed = await handler.execute("ask_list", {"project_id": "p-reconcile", "view": "history"})
    assert listed["success"] and not listed["items"], listed


async def test_old_unknown_shared_root_is_retired_without_repost(command_handler_factory):
    handler = await command_handler_factory()
    _, old = await seed(handler)
    async with handler.db.immediate() as conn:
        await handler.db.insert_ask_binding(
            {
                "id": "old-binding",
                "ask_id": old["id"],
                "project_id": "p-reconcile",
                "destination": destination(handler.config),
                "transport": "discord",
                "guild_id": str(handler.config.discord.guild_id),
                "channel_id": str(handler.config.discord.channel_id),
                "mode_generation": 3,
                "root_generation": 1,
                "root_state": "unknown",
                "active": True,
                "inflight_id": "old-write",
                "receipt": {},
                "desired_display_version": 1,
                "applied_display_version": 0,
                "created_at": 1,
                "updated_at": 1,
            },
            conn=conn,
        )
        await conn.execute(
            t.outbound_deliveries.insert().values(
                id="old-write",
                ask_id=old["id"],
                binding_id="old-binding",
                project_id="p-reconcile",
                decision_generation=1,
                display_version=1,
                mode_generation=3,
                root_generation=1,
                send_kind="root",
                owner_kind="ask",
                owner_id=old["id"],
                dedup_key="old-write",
                destination={},
                payload={},
                payload_hash="0" * 64,
                marker="old-write",
                state="unknown",
                due_at=1,
                created_at=1,
                updated_at=1,
            )
        )
    result = await apply_reset(handler)
    assert "shared_writes" in result["result"]["before"]
    assert "inflight_bindings" in result["result"]["before"]
    async with handler.db.immediate() as conn:
        binding = dict(
            (
                await conn.execute(
                    select(t.ask_external_bindings).where(
                        t.ask_external_bindings.c.id == "old-binding",
                    )
                )
            )
            .mappings()
            .one()
        )
        assert not binding["active"] and binding["root_state"] == "retired"
        assert binding["inflight_id"] is None and binding["root_generation"] == 2
        old_write = dict((await conn.execute(select(t.outbound_deliveries))).mappings().one())
        assert old_write["state"] == "cancelled"
        # A late outcome is still obsolete and cannot become a new cutover blocker.
        await conn.execute(update(t.outbound_deliveries).values(state="unknown"))
        assert set(
            await diagnostics(
                conn, project_id="p-reconcile", destination_id=destination(handler.config)
            )
        ) == {"transfers"}
