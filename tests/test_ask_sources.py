"""A01: bounded complete context, safe legacy projection and static source contracts."""

from dataclasses import replace

import pytest
from sqlalchemy import func, select

from src.asks.adapters import AdapterRegistry
from src.asks.models import AskChoice, AskError, validate_context
from src.database.tables import human_asks
from tests.ask_helpers import db as db_fixture, service as service_fixture, snapshot  # noqa: F401


async def test_complete_context_and_no_source_effect(service, db):
    async with db._engine.begin() as conn:
        result = await service.publish(snapshot().identity, conn=conn)
        assert result.created
        assert result.ask["context_state"] == "complete"
        assert result.ask["decision_state"] == "pending"
        assert result.ask["context"]["choices"][0]["effect"]
    assert (await db.get_review("rev-ask"))["state"] == "in_review"


@pytest.mark.parametrize(
    "missing", ["why", "waiting_effect", "requested_by", "blocked_work", "links"]
)
async def test_missing_context_refuses_without_creating_ask(service, db, missing):
    source = snapshot()
    context = dict(source.context)
    del context[missing]
    async with db._engine.begin() as conn:
        with pytest.raises(AskError) as err:
            await service.publish_snapshot(replace(source, context=context), conn=conn)
        assert err.value.code == "ask.context_missing"
        assert missing in err.value.fields
        assert (await conn.execute(select(func.count()).select_from(human_asks))).scalar_one() == 0
    assert (await db.get_review("rev-ask"))["state"] == "in_review"


@pytest.mark.parametrize("field,value", [("logical_owner", ""), ("decider", {}), ("audience", {})])
async def test_missing_ownership_refuses_publication(service, db, field, value):
    async with db._engine.begin() as conn:
        with pytest.raises(AskError) as err:
            await service.publish_snapshot(replace(snapshot(), **{field: value}), conn=conn)
        assert field in err.value.fields


async def test_legacy_context_needed_preserves_source_link_without_choices(service, db):
    source = snapshot()
    async with db._engine.begin() as conn:
        result = await service.publish_snapshot(
            replace(source, context={"links": source.context["links"]}), legacy=True, conn=conn
        )
        assert result.ask["context_state"] == "context_needed"
        assert result.ask["next_actor"] == "agent"
        assert result.ask["context"]["choices"] == []
        assert result.ask["context"]["links"] == source.context["links"]


@pytest.mark.parametrize("count,valid", [(5, True), (20, True), (21, False)])
def test_discord_choice_limit_is_not_domain_limit(count, valid):
    source = replace(
        snapshot(), choices=tuple(AskChoice(str(i), "Label", "Effect") for i in range(count))
    )
    assert (not validate_context(source)) is valid


@pytest.mark.parametrize("field,limit", [("title", 200), ("why", 2000), ("waiting_effect", 2000)])
def test_context_bounds_are_not_truncated(field, limit):
    source = snapshot()
    assert not validate_context(replace(source, context={**source.context, field: "x" * limit}))
    assert field in validate_context(
        replace(source, context={**source.context, field: "x" * (limit + 1)})
    )


def test_choice_effect_and_duplicate_keys_are_checked():
    source = snapshot()
    assert "choices[0]" in validate_context(replace(source, choices=(AskChoice("a", "A", ""),)))
    assert "choices[1]" in validate_context(replace(source, choices=source.choices[:1] * 2))
    assert "choices[0]" in validate_context(
        replace(source, choices=(AskChoice("a", "A", "x" * 2001),))
    )


def test_unregistered_adapter_never_falls_back_to_generic_command():
    with pytest.raises(AskError) as err:
        AdapterRegistry().get("gate", "caller_command")
    assert err.value.code == "ask.unsupported_source"


async def test_meaning_and_display_and_feedback_have_distinct_versions(service, db):
    source = snapshot()
    async with db._engine.begin() as conn:
        first = (await service.publish_snapshot(source, conn=conn)).ask
        display = replace(source, context={**source.context, "title": "Approve the retry design!"})
        second = (await service.publish_snapshot(display, conn=conn)).ask
        assert second["id"] == first["id"]
        assert second["decision_generation"] == 1
        assert second["display_version"] == 2
        feedback = replace(display, feedback_version=1, fingerprint="comment-1")
        third = (await service.publish_snapshot(feedback, conn=conn)).ask
        assert third["decision_generation"] == 1
        assert third["feedback_version"] == 1
        revised = replace(feedback, generation_token="revision-2", fingerprint="revision-2")
        fourth = (await service.publish_snapshot(revised, conn=conn)).ask
        assert fourth["decision_generation"] == 2
        assert fourth["id"] == first["id"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("waiting_effect", "Decline automatically tomorrow"),
        ("blocked_work", [{"label": "New task", "url": "/tasks/new"}]),
    ],
)
async def test_changed_waiters_or_waiting_effect_invalidates_generation(service, db, field, value):
    source = snapshot()
    async with db._engine.begin() as conn:
        await service.publish_snapshot(source, conn=conn)
        result = await service.publish_snapshot(
            replace(source, context={**source.context, field: value}), conn=conn
        )
        assert result.ask["decision_generation"] == 2
