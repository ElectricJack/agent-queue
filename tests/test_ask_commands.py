"""Public ask reads preserve project/audience and human decision boundaries."""

from sqlalchemy import update

from src.database.tables import human_asks
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
