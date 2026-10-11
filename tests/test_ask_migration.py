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
