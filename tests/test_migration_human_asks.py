"""Human ask migrations on fresh and historical shapes, disposable PostgreSQL only."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from src.database.tables import (
    approved_document_snapshots,
    ask_decisions,
    doc_reviews,
    escalation_actions,
    escalation_messages,
    escalations,
    metadata,
)
from tests.ask_helpers import db as db_fixture  # noqa: F401
from tests.ask_helpers import service as service_fixture  # noqa: F401
from tests.test_ask_decisions import reserve

pytestmark = pytest.mark.migration
ROOT = Path(__file__).resolve().parents[1]
MODULE = "a00000000096_human_ask_decisions"


def migration(module=MODULE):
    spec = importlib.util.spec_from_file_location(
        module, ROOT / "migrations" / "versions" / f"{module}.py"
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def run(*steps):
    def apply(conn):
        with Operations.context(MigrationContext.configure(conn)):
            for step in steps:
                getattr(migration(), step)()

    return apply


@pytest.fixture
async def upgrade_from_95(db):
    # Revision 95's shape is obtained by undoing 96 in an empty, leased database.
    # DDL rollback restores it even when an assertion fails, never the operator DB.
    async with db._engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(sa.text("ALTER TABLE conversation_inputs DROP COLUMN ask_id"))
            await conn.execute(sa.text("ALTER TABLE outbound_deliveries DROP COLUMN ask_id CASCADE"))
            await conn.execute(sa.text("ALTER TABLE outbound_deliveries DROP COLUMN binding_id CASCADE"))
            await conn.execute(sa.text("DROP TABLE ask_action_tokens"))
            await conn.execute(sa.text("DROP TABLE ask_external_bindings"))
            await conn.run_sync(run("downgrade"))
            assert not await conn.run_sync(lambda c: sa.inspect(c).has_table("human_asks"))
            assert "feedback_version" not in await conn.run_sync(
                lambda c: {col["name"] for col in sa.inspect(c).get_columns("doc_reviews")}
            )
            yield conn
        finally:
            await trans.rollback()


async def shape(conn):
    def inspect(sync):
        inspector = sa.inspect(sync)
        names = [table.name for table in migration().NEW_TABLES]
        return {
            name: {
                "columns": [
                    (c["name"], str(c["type"]), c["nullable"], c["default"])
                    for c in inspector.get_columns(name)
                ],
                "checks": sorted(
                    (c["name"], c["sqltext"]) for c in inspector.get_check_constraints(name)
                ),
                "unique": sorted(
                    tuple(c["column_names"]) for c in inspector.get_unique_constraints(name)
                ),
                "fks": sorted(
                    (
                        tuple(f["constrained_columns"]),
                        f["referred_table"],
                        f["options"].get("ondelete"),
                    )
                    for f in inspector.get_foreign_keys(name)
                ),
            }
            for name in names
        }

    return await conn.run_sync(inspect)


async def test_upgrade_from_95_and_replay_preserve_history(upgrade_from_95):
    conn = upgrade_from_95
    await conn.execute(
        sa.insert(escalations).values(
            id="legacy-incident",
            project_id="p-ask",
            source_kind="gate",
            source_identity="gate-1",
            incident_key="gate-1",
            supervisor_owner="supervisor:p-ask",
            summary="Explain gate",
            investigation="Checked",
            decision_requested="Approve?",
            severity="high",
            created_at=1,
            updated_at=1,
        )
    )
    await conn.execute(
        sa.text("""
        INSERT INTO escalation_messages
        (id, escalation_id, direction, transport, verified_actor, text, received_at, created_at)
        VALUES ('legacy-reply', 'legacy-incident', 'inbound', 'discord', 'discord:123', 'Yes', 1, 1)
    """)
    )
    await conn.execute(
        sa.text("""
        INSERT INTO escalation_actions
        (id, escalation_id, reply_id, idempotency_key, action_kind, target_id, parameters,
         executor, started_revision, status, outcome, result, created_at, completed_at)
        VALUES ('legacy-action', 'legacy-incident', 'legacy-reply', 'legacy-key', 'gate_resolve',
                'gate-1', '{}', 'supervisor:p-ask', 1, 'succeeded', 'resolved', '{"proof":"old"}', 1, 2)
    """)
    )
    await conn.run_sync(run("upgrade", "upgrade"))
    old = (await conn.execute(sa.select(escalation_actions))).mappings().one()
    assert old["id"] == "legacy-action" and old["result"] == {"proof": "old"}
    assert old["status"] == "succeeded" and old["ask_id"] is None
    review = (await conn.execute(sa.select(doc_reviews))).mappings().one()
    assert (
        review["feedback_version"],
        review["decision_generation"],
        review["pending_decision_id"],
    ) == (0, 1, None)
    await conn.execute(sa.delete(escalations))
    assert (await conn.execute(sa.select(escalation_messages.c.escalation_id))).scalar_one() is None
    assert (await conn.execute(sa.select(escalation_actions.c.escalation_id))).scalar_one() is None
    assert (await conn.execute(sa.select(escalation_actions.c.result))).scalar_one() == {
        "proof": "old"
    }


async def test_fresh_metadata_matches_upgrade_and_replays(db, upgrade_from_95):
    conn = upgrade_from_95
    await conn.run_sync(run("upgrade", "upgrade"))
    upgraded = await shape(conn)
    # A new schema in the disposable database exercises metadata.create_all,
    # including PostgreSQL audit triggers, without relying on the template.
    await conn.execute(sa.text("CREATE SCHEMA fresh_ask_metadata"))
    await conn.execute(sa.text("SET LOCAL search_path TO fresh_ask_metadata"))
    await conn.run_sync(metadata.create_all)
    fresh = await shape(conn)
    assert fresh == upgraded
    await conn.run_sync(run("upgrade", "upgrade"))
    assert await shape(conn) == fresh


async def test_decisions_refuse_unsafe_downgrade_and_audit_mutation(db, service):
    async with db._engine.begin() as conn:
        decision, _ = await reserve(db, service, conn=conn)
        with pytest.raises(RuntimeError, match="Unsafe human ask downgrade"):
            await conn.run_sync(run("downgrade"))
        assert await db.get_ask_decision(decision["id"], conn=conn)
        for statement in (
            sa.update(ask_decisions).values(overall_note="Rewrite evidence"),
            sa.delete(ask_decisions),
        ):
            with pytest.raises(IntegrityError, match="immutable"):
                async with conn.begin_nested():
                    await conn.execute(statement)


async def test_standalone_bytes_and_approval_provenance_are_immutable(db):
    import hashlib

    async with db._engine.begin() as conn:
        body = b"# Approved\n"
        saved = await db.insert_approved_document_snapshot(
            {
                "id": "snapshot-1",
                "project_id": "p-ask",
                "source_path": "specs/standalone.md",
                "title": "Standalone spec",
                "body": body,
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "spec_kind": "implementation",
                "approval_provenance": {"principal": "user:jack"},
                "created_at": 1,
            },
            conn=conn,
        )
        assert saved["body"] == body
        with pytest.raises(IntegrityError, match="immutable"):
            async with conn.begin_nested():
                await conn.execute(sa.update(approved_document_snapshots).values(body=b"Changed"))


def test_revision_is_single_head_with_named_checks_jsonb_and_bare_defaults():
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == ["a00000000099"]
    assert script.get_revision("a00000000099").down_revision == "a00000000098"
    assert script.get_revision("a00000000098").down_revision == "a00000000097"
    assert script.get_revision("a00000000097").down_revision == "a00000000096"
    assert script.get_revision("a00000000096").down_revision == "a00000000095"
    revision = migration()
    for table in revision.NEW_TABLES:
        assert all(c.name for c in table.constraints if isinstance(c, sa.CheckConstraint))
        for column in table.c:
            if isinstance(column.type, sa.JSON):
                assert isinstance(column.type, JSONB)
            if column.server_default is not None and isinstance(column.server_default.arg, str):
                assert not column.server_default.arg.startswith("'")


async def test_source_fences_upgrade_historical_answers_and_replay(db):
    revision = migration("a00000000097_ask_source_fences")

    def upgrade(conn):
        with Operations.context(MigrationContext.configure(conn)):
            revision.upgrade()

    async with db._engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(sa.text("ALTER TABLE gates DROP COLUMN human_request"))
            await conn.execute(sa.text("ALTER TABLE agent_questions DROP COLUMN ask_decision_id"))
            await conn.execute(sa.text("ALTER TABLE agent_questions DROP COLUMN submission_state"))
            for ident, state, delivered in (
                ("pending", "human", None), ("ambiguous", "answered", None),
                ("sent", "delivered", 2), ("resumed", "resolved", 2),
            ):
                await conn.execute(sa.text("""
                    INSERT INTO agent_questions
                    (id, session_id, session_name, instance_token, task_id, project_id,
                     agent_id, turn_id, claim_epoch, question, requires_human, state,
                     created_at, updated_at, source_ts, delivered_at)
                    VALUES (:id, 's', 'worker', 'instance', 't', 'p-ask', 'a', :id, 1,
                            'Proceed?', true, :state, 1, 1, 1, :delivered)
                """), {"id": ident, "state": state, "delivered": delivered})
            await conn.run_sync(upgrade)
            assert dict((await conn.execute(sa.text(
                "SELECT id, submission_state FROM agent_questions"
            ))).all()) == {
                "pending": "pending", "ambiguous": "unknown", "sent": "delivered",
                "resumed": "delivered",
            }
            # Re-running a guarded revision cannot mark newly accepted work ambiguous.
            await conn.execute(sa.text(
                "UPDATE agent_questions SET state = 'answered' WHERE id = 'pending'"
            ))
            await conn.run_sync(upgrade)
            assert await conn.scalar(sa.text(
                "SELECT submission_state FROM agent_questions WHERE id = 'pending'"
            )) == "pending"
            with pytest.raises(IntegrityError, match="ck_agent_question_submission_state"):
                async with conn.begin_nested():
                    await conn.execute(sa.text(
                        "UPDATE agent_questions SET submission_state = 'unchecked'"
                    ))
        finally:
            await trans.rollback()


async def test_empty_source_fences_can_downgrade_and_replay(db):
    revision = migration("a00000000097_ask_source_fences")

    def replay(conn):
        with Operations.context(MigrationContext.configure(conn)):
            revision.downgrade()
            assert "human_request" not in {
                c["name"] for c in sa.inspect(conn).get_columns("gates")
            }
            revision.downgrade()
            revision.upgrade()
            revision.upgrade()
            assert "human_request" in {
                c["name"] for c in sa.inspect(conn).get_columns("gates")
            }

    async with db._engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.run_sync(replay)
        finally:
            await trans.rollback()


@pytest.mark.parametrize("fence", ["human_request", "ask_decision_id", "unknown", "delivered"])
async def test_source_fences_refuse_downgrade_without_discarding_evidence(db, fence):
    revision = migration("a00000000097_ask_source_fences")

    def downgrade(conn):
        with Operations.context(MigrationContext.configure(conn)):
            revision.downgrade()

    async with db._engine.connect() as conn:
        trans = await conn.begin()
        try:
            if fence == "human_request":
                await conn.execute(sa.text("""
                    INSERT INTO gates (id, project_id, gate_type, title, status, created_at,
                                       human_request)
                    VALUES ('retained', 'p-ask', 'human', 'Approve?', 'open', 1,
                            '{"policy":"approval_hold_v1"}'::jsonb)
                """))
            else:
                await conn.execute(sa.text("""
                    INSERT INTO agent_questions
                    (id, session_id, session_name, instance_token, task_id, project_id,
                     agent_id, turn_id, claim_epoch, question, requires_human, state,
                     created_at, updated_at, source_ts, ask_decision_id, submission_state)
                    VALUES ('retained', 's', 'worker', 'instance', 't', 'p-ask', 'a', 'turn', 1,
                            'Proceed?', true, 'human', 1, 1, 1, :decision, :state)
                """), {"decision": "decision-1" if fence == "ask_decision_id" else None,
                       "state": "pending" if fence == "ask_decision_id" else fence})
            with pytest.raises(RuntimeError, match="forward-compatible reader"):
                await conn.run_sync(downgrade)
            # Refusal happens before any column or evidence is removed.
            assert await conn.run_sync(lambda c: "human_request" in {
                col["name"] for col in sa.inspect(c).get_columns("gates")
            })
            table = "gates" if fence == "human_request" else "agent_questions"
            assert await conn.scalar(sa.text(f"SELECT count(*) FROM {table} WHERE id = 'retained'")) == 1
        finally:
            await trans.rollback()


@pytest.fixture
async def transport_upgrade_connection(db):
    # The lease pool resets rows, not schema. Always restore historical DDL,
    # including when the upgrade or its assertions fail.
    async with db._engine.connect() as conn:
        trans = await conn.begin()
        try:
            yield conn
        finally:
            await trans.rollback()


async def test_input_upgrade_replays_and_preserves_historical_text(transport_upgrade_connection):
    conn = transport_upgrade_connection
    revision = migration("a00000000098_human_ask_transport")
    def upgrade(conn):
        with Operations.context(MigrationContext.configure(conn)):
            revision.upgrade()
    async with conn.begin_nested():
        await conn.execute(sa.text("ALTER TABLE conversation_inputs DROP COLUMN ask_id"))
        await conn.execute(sa.text("ALTER TABLE conversation_inputs DROP COLUMN project_id"))
        await conn.execute(sa.text("ALTER TABLE conversation_inputs DROP COLUMN source_version"))
        await conn.run_sync(upgrade)
        await conn.run_sync(upgrade)
        assert not await conn.run_sync(
            lambda bind: compare_metadata(MigrationContext.configure(bind), metadata)
        )
        await conn.execute(sa.text("""
            INSERT INTO conversation_inputs
            (id, transport, external_message_id, verified_actor, author_id, channel_id,
             text, text_sha256, char_count, source, received_at, created_at)
            VALUES ('historical', 'discord', 'message', 'human:discord:1', '1', 'channel',
                    :body, :hash, 16000, 'legacy', 1, 1)
        """), {"body": "x" * 16000, "hash": "a" * 64})
        assert await conn.scalar(sa.text("SELECT length(text) FROM conversation_inputs WHERE id='historical'")) == 16000
        assert await conn.scalar(sa.text("SELECT conversation_id FROM conversation_inputs WHERE id='historical'")) is None
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(sa.text("UPDATE conversation_inputs SET routing_state='guessed'"))


@pytest.mark.parametrize("missing_columns", [True, False])
async def test_transport_upgrade_from_historical_outbox_preserves_receipts(
    transport_upgrade_connection, missing_columns
):
    conn = transport_upgrade_connection
    revision = migration('a00000000098_human_ask_transport')

    def upgrade(conn):
        with Operations.context(MigrationContext.configure(conn)):
            revision.upgrade()

    async with conn.begin_nested():
        await conn.execute(sa.text("""
            INSERT INTO outbound_deliveries
            (id, owner_kind, owner_id, dedup_key, destination, payload, payload_hash, marker,
             state, due_at, external_receipt_id, receipt_confirmed_at, created_at, updated_at)
            VALUES ('old-report', 'morning', 'r', 'old-key', '{}', '{}', 'hash', 'marker',
                    'sent', 1, 'confirmed-message', 2, 1, 2)
        """))
        if missing_columns:
            for name in (
                'ask_id', 'binding_id', 'project_id', 'decision_generation', 'display_version',
                'mode_generation', 'root_generation', 'send_kind', 'write_started_at', 'attempt_token',
            ):
                await conn.execute(sa.text(
                    f'ALTER TABLE outbound_deliveries DROP COLUMN {name} CASCADE'
                ))
            for name in ('ask_action_tokens', 'ask_external_bindings', 'human_communication_modes'):
                await conn.execute(sa.text(f'DROP TABLE {name}'))
        else:
            # A partial upgrade can leave the columns present without their FKs.
            foreign_keys = await conn.run_sync(
                lambda bind: sa.inspect(bind).get_foreign_keys('outbound_deliveries')
            )
            for foreign_key in foreign_keys:
                await conn.execute(sa.text(
                    f'ALTER TABLE outbound_deliveries DROP CONSTRAINT "{foreign_key["name"]}"'
                ))
        await conn.run_sync(upgrade)
        await conn.run_sync(upgrade)
        assert not await conn.run_sync(
            lambda bind: compare_metadata(MigrationContext.configure(bind), metadata)
        )
        assert await conn.scalar(sa.text(
            "SELECT external_receipt_id FROM outbound_deliveries WHERE id='old-report'"
        )) == 'confirmed-message'
        with pytest.raises(IntegrityError, match='ck_outbound_deliveries_ask'):
            async with conn.begin_nested():
                await conn.execute(sa.text("UPDATE outbound_deliveries SET owner_kind='ask'"))
        for column in ('ask_id', 'binding_id', 'project_id'):
            with pytest.raises(IntegrityError, match='foreign key constraint'):
                async with conn.begin_nested():
                    await conn.execute(sa.text(
                        f"UPDATE outbound_deliveries SET {column} = 'missing-reference'"
                    ))
