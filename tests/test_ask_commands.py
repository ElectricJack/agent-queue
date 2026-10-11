"""Public ask reads preserve project/audience and human decision boundaries."""

import base64
import json

import pytest
from sqlalchemy import update

from src.database.tables import doc_reviews, human_asks, projects
from tests.test_ask_discussion import ask_for, discuss
from tests.test_ask_discussion import env as env  # noqa: F401
from tests.test_review_commands import _scoped
from tests.test_review_commands import env as review_env  # noqa: F401


async def test_worker_cannot_read_or_create_human_input(env):
    handler, _ = env
    _, ask = await ask_for(handler)
    for name, args in [
        ("ask_show", {"ask_id": ask["id"]}),
        ("ask_list", {}),
        ("ask_history", {"ask_id": ask["id"]}),
    ]:
        result = await _scoped(handler, name, args, session_id="worker")
        assert not result["success"], result
    denied = await _scoped(
        handler,
        "ask_reply",
        {
            "ask_id": ask["id"],
            "text": "approve",
            "expected": ask["expected"],
            "idempotency_key": "k",
        },
        session_id="supervisor",
    )
    assert not denied["success"]


async def test_audience_filter_precedes_count_and_history_access(env):
    handler, db = env
    _, public = await ask_for(handler)
    _, private = await ask_for(handler)
    async with db.immediate() as conn:
        await conn.execute(
            update(human_asks)
            .where(human_asks.c.id == private["id"])
            .values(audience={"principals": ["human:someone-else"]})
        )
    listing = await handler.execute("ask_list", {})
    assert listing["success"] and listing["count"] == 1
    assert listing["items"][0]["id"] == public["id"]
    for command in ("ask_show", "ask_history"):
        result = await handler.execute(command, {"ask_id": private["id"]})
        assert result["error_code"] == "ask.not_found"


async def test_history_keyset_and_discussion_remains_in_needs_you(env):
    handler, _ = env
    _, ask = await ask_for(handler)
    first = await discuss(handler, ask, key="first")
    await discuss(handler, ask, key="second")
    page = await handler.execute("ask_history", {"ask_id": ask["id"], "limit": 1})
    assert page["items"][0]["id"] == first["receipt"]["entry_id"]
    next_page = await handler.execute(
        "ask_history", {"ask_id": ask["id"], "limit": 1, "cursor": page["next_cursor"]}
    )
    assert next_page["items"][0]["id"] != page["items"][0]["id"]
    assert next_page["next_cursor"] is None
    listing = await handler.execute("ask_list", {})
    assert listing["count"] == 1
    assert listing["items"][0]["answer_pending"]


async def test_keyset_count_order_and_finished_cursor_row(env):
    handler, db = env
    asks = [(await ask_for(handler))[1] for _ in range(4)]
    async with db.immediate() as conn:
        for index, ask in enumerate(asks):
            await conn.execute(update(human_asks).where(human_asks.c.id == ask["id"])
                               .values(created_at=100 + index, severity="high" if index == 2 else "low"))
        await conn.execute(update(projects).where(projects.c.id == "p").values(status="PAUSED"))
    page = await handler.execute("ask_list", {"limit": 2})
    assert page["success"], page
    assert page["count"] == 4 and page["snapshot_at"] > 0
    assert [a["id"] for a in page["items"]] == [asks[2]["id"], asks[0]["id"]]
    assert all(a["held_while_paused"] for a in page["items"])
    async with db.immediate() as conn:
        await conn.execute(update(human_asks).where(human_asks.c.id == asks[0]["id"])
                           .values(decision_state="approved", next_actor="none"))
    second = await handler.execute("ask_list", {"limit": 2, "cursor": page["next_cursor"]})
    assert second["count"] == 3 and second["next_cursor"] is None
    assert [a["id"] for a in second["items"]] == [asks[1]["id"], asks[3]["id"]]
    assert second["identity_hash"] != page["identity_hash"]
    invalid = await handler.execute("ask_list", {"view": "waiting", "cursor": page["next_cursor"]})
    assert invalid["error_code"] == "ask.invalid_cursor"


@pytest.mark.parametrize("cursor", ["garbage", "e30=", base64.urlsafe_b64encode(json.dumps([]).encode()).decode()])
async def test_invalid_cursor_is_a_structured_error(env, cursor):
    handler, _ = env
    result = await handler.execute("ask_list", {"cursor": cursor})
    assert result["error_code"] == "ask.invalid_cursor"


async def test_views_health_and_alias_scope(env):
    handler, db = env
    rid, ask = await ask_for(handler)
    alias = await handler.execute("ask_show", {"ask_id": rid, "alias_kind": "review"})
    assert alias["ask"]["id"] == ask["id"]
    assert any(a["action"] == "decide" and a["enabled"] for a in alias["actions"])
    async with db.immediate() as conn:
        await conn.execute(update(doc_reviews).where(doc_reviews.c.id == rid)
                           .values(feedback_version=10))
    assert (await handler.execute("ask_list", {}))["source_health"] == "refreshing"
    async with db.immediate() as conn:
        await conn.execute(update(human_asks).where(human_asks.c.id == ask["id"])
                           .values(context_state="context_needed"))
    needs = await handler.execute("ask_list", {})
    assert needs["count"] == 0 and needs["source_health"] == "degraded"
    assert (await handler.execute("ask_list", {"view": "waiting"}))["count"] == 1
    async with db.immediate() as conn:
        await conn.execute(update(human_asks).where(human_asks.c.id == ask["id"])
                           .values(audience={"principals": ["human:someone-else"]}))
    assert (await handler.execute("ask_show", {"ask_id": rid, "alias_kind": "review"}))["error_code"] == "ask.not_found"
    assert (await handler.execute("ask_list", {"view": "waiting"}))["count"] == 0


async def test_digest_uses_same_distinct_ids_and_excludes_private_audience(env):
    handler, db = env
    _, public = await ask_for(handler)
    _, private = await ask_for(handler)
    async with db.immediate() as conn:
        await conn.execute(update(human_asks).where(human_asks.c.id == private["id"])
                           .values(audience={"private": True}))
        dashboard = await db.list_asks_snapshot(conn=conn, limit=1)
        digest_page = await db.list_asks_snapshot(conn=conn, public=True, limit=1)
    assert dashboard["count"] == 2 and digest_page["count"] == 1
    assert digest_page["items"][0]["id"] == public["id"]
    async with db.immediate() as conn:
        await conn.execute(update(human_asks).where(human_asks.c.id == public["id"])
                           .values(audience={"channel_ids": ["123"]}))
        assert (await db.list_asks_snapshot(conn=conn, public=True, channel_id="other"))["count"] == 0
        assert (await db.list_asks_snapshot(conn=conn, public=True, channel_id="123"))["count"] == 1


async def test_report_and_digest_share_audience_count_and_identity_hash(env):
    handler, db = env
    _, public = await ask_for(handler)
    _, private = await ask_for(handler)
    async with db.immediate() as conn:
        await conn.execute(update(human_asks).where(human_asks.c.id == private["id"])
                           .values(audience={"principals": ["human:someone-else"]}))
        await conn.execute(update(human_asks).where(human_asks.c.id == public["id"])
                           .values(audience={"principals": None, "channel_ids": ["123"]}))
        canonical = await db.list_asks_snapshot(conn=conn, public=True, channel_id="123")
    report = await db.collect_morning_report_sources(since=0, until=2e10, project_ids=("p",),
                                                     channel_id="123")
    window = type("Window", (), {"since": 0, "until": 2e10})()
    digest = await db.collect_digest_supervisor_facts(window, now=2e10, project_ids=("p",),
                                                     channel_id="123")
    for metadata in (report["needs_you"], digest["asks"]):
        assert metadata["count"] == canonical["count"] == 1
        assert metadata["identity_hash"] == canonical["identity_hash"]
    assert [a["id"] for a in report["sources"]["asks"]] == [public["id"]]
    assert [a["id"] for a in digest["asks"]["items"]] == [public["id"]]


async def test_missing_projection_reports_refreshing_not_empty(env):
    handler, _ = env
    # Submission has durable source context even before a projection reader runs.
    from tests.test_review_feedback import submitted
    await submitted(handler)
    result = await handler.execute("ask_list", {})
    assert result["count"] == 0 and result["source_health"] == "refreshing"
