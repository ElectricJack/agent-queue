"""End-to-end command execution for the K03 knowledge/record surface.

These drive ``CommandHandler.execute()`` — the single dispatch path shared by
CLI, MCP, REST and Discord — through both authorization gates and the record
services, with a ``TRUSTED_LOCAL`` principal (no ``_scope``) so the
service-layer supervisor path is what runs.  ``test_record_contracts.py`` pins
the static shape; this file pins the *behavior* those contracts dispatch to.
"""

from __future__ import annotations

import pytest

from tests.record_helpers import knowledge_config, seed_project

pytestmark = pytest.mark.usefixtures("unpooled_postgres")


@pytest.fixture
async def handler(command_handler_factory, reuse_database):
    h = await command_handler_factory()
    db = h._db
    await seed_project(db, "p")
    h.config.knowledge = knowledge_config(enabled_projects=["p"])
    return h


def ident(result):
    return f"record:{result['record_id']}"


class TestKnowledgeWriteFlow:
    async def test_create_show_list_update_history_diff(self, handler):
        created = await handler.execute(
            "knowledge_create",
            {
                "project_id": "p",
                "title": "Observed incident",
                "body": "Retained evidence.",
                "category": "incident",
                "idempotency_key": "create-1",
            },
        )
        assert created["success"] is True
        identity = ident(created)
        rev1 = created["revision_id"]

        shown = await handler.execute(
            "knowledge_show", {"project_id": "p", "identity": identity}
        )
        assert shown["success"] is True
        assert shown["record_id"] == created["record_id"]
        assert shown["sequence"] == 1
        assert shown["snapshot"]["title"] == "Observed incident"

        listed = await handler.execute("knowledge_list", {"project_id": "p"})
        assert listed["success"] is True
        assert created["record_id"] in [item["record_id"] for item in listed["items"]]

        updated = await handler.execute(
            "knowledge_update",
            {
                "project_id": "p",
                "identity": identity,
                "body": "Revised evidence.",
                "idempotency_key": "update-1",
                "if_revision": rev1,
            },
        )
        assert updated["success"] is True
        rev2 = updated["revision_id"]
        assert rev2 != rev1

        history = await handler.execute(
            "knowledge_history", {"project_id": "p", "identity": identity}
        )
        assert history["success"] is True
        sequences = [row["sequence"] for row in history["revisions"]]
        assert sequences == [2, 1] or set(sequences) == {1, 2}

        diff = await handler.execute(
            "knowledge_diff",
            {
                "project_id": "p",
                "identity": identity,
                "from_revision": rev1,
                "to_revision": rev2,
            },
        )
        assert diff["success"] is True
        fields = {c["field"] for c in diff["changes"]}
        assert "body" in fields

    async def test_create_is_idempotent_on_key(self, handler):
        args = {
            "project_id": "p",
            "title": "Stable",
            "body": "Body",
            "category": "note",
            "idempotency_key": "dup",
        }
        first = await handler.execute("knowledge_create", args)
        again = await handler.execute("knowledge_create", args)
        assert first["success"] is True
        assert again["success"] is True
        assert again["record_id"] == first["record_id"]

    async def test_retire_then_restore(self, handler):
        created = await handler.execute(
            "knowledge_create",
            {"project_id": "p", "title": "T", "body": "B", "category": "note",
             "idempotency_key": "r-1"},
        )
        identity = ident(created)
        rev1 = created["revision_id"]
        retired = await handler.execute(
            "knowledge_retire",
            {"project_id": "p", "identity": identity, "reason": "superseded",
             "if_revision": rev1, "idempotency_key": "r-2"},
        )
        assert retired["success"] is True, retired
        restored = await handler.execute(
            "knowledge_restore",
            {"project_id": "p", "identity": identity, "revision_id": rev1,
             "reason": "bring back", "if_revision": retired["revision_id"],
             "idempotency_key": "r-3"},
        )
        assert restored["success"] is True, restored


class TestRecordReads:
    async def test_record_show(self, handler):
        created = await handler.execute(
            "knowledge_create",
            {"project_id": "p", "title": "T", "body": "B", "category": "fact",
             "idempotency_key": "show-1"},
        )
        out = await handler.execute(
            "record_show", {"project_id": "p", "identity": ident(created)}
        )
        assert out["success"] is True
        assert out["record_id"] == created["record_id"]

    async def test_record_search_finds_by_term(self, handler):
        await handler.execute(
            "knowledge_create",
            {"project_id": "p", "title": "Zebra migration", "body": "body",
             "category": "note", "idempotency_key": "s-1"},
        )
        out = await handler.execute(
            "record_search", {"project_id": "p", "query": "zebra"}
        )
        assert out["success"] is True
        assert any(item["title"] == "Zebra migration" for item in out["items"])

    async def test_record_capabilities_reflects_config(self, handler):
        out = await handler.execute("record_capabilities", {"project_id": "p"})
        assert out["success"] is True
        caps = out["capabilities"]
        assert caps["enabled"] is True
        assert caps["writes_enabled"] is True
        assert caps["legacy_memory_mode"] == "disabled"


class TestLinks:
    async def test_link_create_list_remove(self, handler):
        source = await handler.execute(
            "knowledge_create",
            {"project_id": "p", "title": "src", "body": "b", "category": "note",
             "idempotency_key": "l-1"},
        )
        target = await handler.execute(
            "knowledge_create",
            {"project_id": "p", "title": "dst", "body": "b", "category": "note",
             "idempotency_key": "l-2"},
        )
        added = await handler.execute(
            "link_create",
            {
                "project_id": "p",
                "identity": ident(source),
                "if_revision": source["revision_id"],
                "operations": [
                    {"action": "add", "target": ident(target), "link_type": "references"}
                ],
                "idempotency_key": "l-3",
            },
        )
        assert added["success"] is True, added

        links = await handler.execute(
            "link_list", {"project_id": "p", "identity": ident(source)}
        )
        assert links["success"] is True, links
        live = [link for link in links["links"] if link.get("target_record_id")]
        assert any(link["target_record_id"] == target["record_id"] for link in live)

        link_id = live[0]["link_id"]
        # The source advanced to a new revision when the link was added.
        source_rev = added["revision_id"]
        removed = await handler.execute(
            "link_remove",
            {
                "project_id": "p",
                "identity": ident(source),
                "if_revision": source_rev,
                "link_id": link_id,
                "idempotency_key": "l-4",
            },
        )
        assert removed["success"] is True, removed


class TestErrorPaths:
    async def test_disabled_project_is_rejected(self, handler):
        handler.config.knowledge = knowledge_config(enabled_projects=["q"])
        out = await handler.execute(
            "knowledge_create",
            {"project_id": "p", "title": "T", "body": "B", "category": "note",
             "idempotency_key": "e-1"},
        )
        assert out["success"] is False
        assert out["error_code"] == "knowledge.disabled"

    async def test_invalid_identity_format(self, handler):
        out = await handler.execute(
            "knowledge_show", {"project_id": "p", "identity": "record:does-not-exist"}
        )
        assert out["success"] is False
        assert out["error_code"] == "record.invalid_identity"

    async def test_unknown_identity_is_not_found(self, handler):
        # Well-formed record identity, unknown to this project.
        out = await handler.execute(
            "knowledge_show",
            {"project_id": "p", "identity": "record:11111111-2222-3333-4444-555555555555"},
        )
        assert out["success"] is False
        assert out["error_code"] == "record.not_found"

    async def test_knowledge_globally_disabled(self, handler):
        from src.config import KnowledgeConfig

        handler.config.knowledge = KnowledgeConfig(enabled=False, enabled_projects=["p"])
        out = await handler.execute(
            "knowledge_list", {"project_id": "p"}
        )
        assert out["success"] is False
        assert out["error_code"] == "knowledge.disabled"
